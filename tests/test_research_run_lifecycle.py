"""R2.2-A/C: trusted execution permit, cumulative budgets, atomic checkpoints and
recovery for the controlled research run store. Fixture only: no real model, no
production state, no service restart."""
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

STORE = "cns_planner.application.research_run_store"
SERVICE = "cns_planner.application.research_run_service"


@pytest.fixture
def tmp_path():
    # Keep this module's controlled temporary paths short; do not touch conftest.
    import uuid
    root = Path(os.environ["TEMP"]) / ("r22l_" + uuid.uuid4().hex[:8])
    root.mkdir(parents=True)
    return root


def api():
    for name in (STORE, SERVICE):
        assert importlib.util.find_spec(name) is not None, f"R2.2 module missing: {name}"
    return importlib.import_module(STORE), importlib.import_module(SERVICE)


def snapshot(actions=None, model_versions=None, scope=None):
    actions = actions if actions is not None else [{"action_id": "a0"}, {"action_id": "a1"}]
    return {
        "schema": "research_run_snapshot.v1",
        "scenario": {"scenario_id": "zs-1", "route_id": "r1", "altitude_m": 300.0},
        "conditional_domain": {"domain_id": "d1",
                               "actions": [action["action_id"] for action in actions]},
        "actions": actions,
        "source_bundle": {"bundle_id": "b1", "sources": ["s1"]},
        "objectives": {"lexicographic": ["L1", "L2", "L3"]},
        "model_versions": dict(model_versions or api()[1].TRUSTED_MODEL_VERSIONS),
        "scope": scope if scope is not None else {"scope_id": "scope-1", "actions": ["a0", "a1"]},
        "evidence_track": "synthetic_fixture",
    }


def budget(**overrides):
    value = {"time_ms": 15000, "nodes": 100, "replays": 2,
             "memory_bytes": 256 * 1024 * 1024, "output_bytes": 1024 * 1024}
    value.update(overrides)
    return value


def future(seconds=120):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def fixture_run(tmp_path, *, name="run", run_id=None, b=None, snap=None):
    storemod, servicemod = api()
    snap = snap or snapshot()
    identity = servicemod.snapshot_identity(snap)
    b = b or budget()
    root = tmp_path / name
    store = storemod.ResearchRunStore(output_root=str(root), budget=b, identity=identity,
                                      allowed_roots=[str(tmp_path)], run_id=run_id)
    auth = servicemod.issue_fixture_authorization(
        authorization_ref="auth-1", budget=b, output_root=str(root),
        deadline_utc=future(), scope_fingerprint=identity["scope_fingerprint"],
        allowed_roots=[str(tmp_path)])
    permit = servicemod.issue_fixture_permit(authorization=auth, approved_snapshot=snap)
    return storemod, servicemod, snap, identity, b, store, permit


def test_json_self_declared_permit_cannot_start_worker(tmp_path, monkeypatch):
    storemod, servicemod, snap, identity, b, store, permit = fixture_run(tmp_path)
    calls = []
    monkeypatch.setattr(servicemod, "run_worker", lambda **kw: calls.append(kw))
    fake = {"permit_id": "p-1", "approved": True, "authorized": True, "trusted": True,
            "authorization_kind": "fixture_test", "authorization_ref": "auth-1",
            "identity": identity, "budget": b, "output_root": str(tmp_path / "run"),
            "deadline_utc": future(), "allowed_calibrations": 0,
            "permit_fingerprint": hashlib.sha256(b"forged").hexdigest()}
    result = servicemod.run_controlled_research(approved_snapshot=snap,
                                                execution_permit=fake, run_store=store)
    assert result["status"] == "blocked"
    assert "execution_permit_not_trusted" in result["blockers"]
    assert calls == [] and not (store.run_dir / "ledger.jsonl").exists()
    for value in (None, object(), b, {"permit_id": "x"}):
        assert servicemod.run_controlled_research(
            approved_snapshot=snap, execution_permit=value, run_store=store)["status"] == "blocked"
    assert calls == []


def test_fixture_permit_runs_controlled_worker_and_completes(tmp_path, monkeypatch):
    storemod, servicemod, snap, identity, b, store, permit = fixture_run(tmp_path)
    result = servicemod.run_controlled_research(approved_snapshot=snap,
                                                execution_permit=permit, run_store=store)
    assert result["status"] == "completed", result
    assert result["worker_status"] == "completed"
    assert result["fixture_only"] is True and result["real_execution_authorized"] is False
    assert result["native_model_calls"] == 0 and result["optimal_proven"] is False
    assert result["persisted_to_project_state"] is False and result["final_eligible"] is False
    assert result["run_id"] == store.run_id
    ledger = [json.loads(line) for line in (store.run_dir / "ledger.jsonl").read_text(
        encoding="utf-8").splitlines()]
    kinds = [row["kind"] for row in ledger]
    assert kinds[0] == "begin" and "worker_start" in kinds and "worker_finished" in kinds
    assert "finalize:completed" in kinds and store.verify_chain()["valid"] is True
    assert (store.run_dir / "result.json").exists() and (store.run_dir / "complete.json").exists()
    request = json.loads((store.run_dir / "worker" / "request.json").read_text(encoding="utf-8"))
    assert "permit" not in json.dumps(request) and "authorization_ref" not in json.dumps(request)
    assert request["handler"] == "identity_digest"
    assert store.high_water()["consumed"]["time_ms"] > 0
    assert store.high_water()["consumed"]["output_bytes"] == sum(
        p.stat().st_size for p in (store.run_dir / "worker").iterdir())


def test_tampered_or_copied_permit_cannot_expand_scope(tmp_path, monkeypatch):
    storemod, servicemod, snap, identity, b, store, permit = fixture_run(tmp_path)
    monkeypatch.setattr(servicemod, "run_worker", lambda **kw: (_ for _ in ()).throw(AssertionError("worker started")))
    object.__setattr__(permit, "budget", dict(b, nodes=10 ** 9))
    first = servicemod.run_controlled_research(approved_snapshot=snap,
                                               execution_permit=permit, run_store=store)
    assert first["status"] == "blocked"
    assert any(b.startswith("execution_permit") for b in first["blockers"])
    import dataclasses
    copied = dataclasses.replace(permit, output_root=str(tmp_path / "elsewhere"))
    second = servicemod.run_controlled_research(approved_snapshot=snap,
                                                execution_permit=copied, run_store=store)
    assert second["status"] == "blocked"
    dumped = json.loads(json.dumps({k: v for k, v in permit.__dict__.items()}))
    dumped["run_id"] = "another-run"
    dumped["permit_fingerprint"] = hashlib.sha256(json.dumps(dumped, sort_keys=True).encode()).hexdigest()
    third = servicemod.run_controlled_research(approved_snapshot=snap,
                                               execution_permit=dumped, run_store=store)
    assert third["status"] == "blocked"


def test_expired_permit_is_rejected_before_worker(tmp_path, monkeypatch):
    storemod, servicemod = api()
    snap = snapshot()
    identity = servicemod.snapshot_identity(snap)
    b = budget()
    root = tmp_path / "run"
    store = storemod.ResearchRunStore(output_root=str(root), budget=b, identity=identity,
                                      allowed_roots=[str(tmp_path)])
    auth = servicemod.issue_fixture_authorization(
        authorization_ref="auth-old", budget=b, output_root=str(root),
        deadline_utc=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        scope_fingerprint=identity["scope_fingerprint"], allowed_roots=[str(tmp_path)])
    permit = servicemod.issue_fixture_permit(authorization=auth, approved_snapshot=snap)
    calls = []
    monkeypatch.setattr(servicemod, "run_worker", lambda **kw: calls.append(kw))
    result = servicemod.run_controlled_research(approved_snapshot=snap,
                                                execution_permit=permit, run_store=store)
    assert result["status"] == "blocked"
    assert "execution_permit_expired" in result["blockers"] and calls == []


def test_snapshot_identity_mismatch_and_model_version_drift_block(tmp_path, monkeypatch):
    storemod, servicemod, snap, identity, b, store, permit = fixture_run(tmp_path)
    calls = []
    monkeypatch.setattr(servicemod, "run_worker", lambda **kw: calls.append(kw))
    changed = snapshot(actions=[{"action_id": "a0"}, {"action_id": "a2"}])
    result = servicemod.run_controlled_research(approved_snapshot=changed,
                                                execution_permit=permit, run_store=store)
    assert result["status"] == "blocked" and "permit_identity_mismatch" in result["blockers"]
    drifted = snapshot(model_versions={"m4_joint": "p14:forged"})
    result = servicemod.run_controlled_research(approved_snapshot=drifted,
                                                execution_permit=permit, run_store=store)
    assert result["status"] == "blocked"
    assert any(b.startswith("approved_snapshot_invalid") for b in result["blockers"])
    assert calls == []


def test_store_root_and_identity_must_match_permit(tmp_path, monkeypatch):
    storemod, servicemod, snap, identity, b, store, permit = fixture_run(tmp_path)
    calls = []
    monkeypatch.setattr(servicemod, "run_worker", lambda **kw: calls.append(kw))
    other = storemod.ResearchRunStore(output_root=str(tmp_path / "other"), budget=b,
                                      identity=identity, allowed_roots=[str(tmp_path)])
    result = servicemod.run_controlled_research(approved_snapshot=snap,
                                                execution_permit=permit, run_store=other)
    assert "output_root_mismatch" in result["blockers"]
    assert servicemod.run_controlled_research(
        approved_snapshot=snap, execution_permit=permit, run_store=object())["status"] == "blocked"
    assert calls == []


def test_production_authorization_entry_is_closed(tmp_path):
    storemod, servicemod = api()
    assert servicemod.PRODUCTION_AUTHORIZATION_ENABLED is False
    with pytest.raises(PermissionError):
        servicemod.issue_production_authorization(
            authorization_ref="real-1", budget=budget(), output_root=str(tmp_path / "run"),
            deadline_utc=future(), scope_fingerprint="0" * 64, allowed_roots=[str(tmp_path)])


def test_each_budget_dimension_is_measured_separately(tmp_path):
    storemod, servicemod = api()
    snap = snapshot()
    identity = servicemod.snapshot_identity(snap)
    b = budget(time_ms=1000, nodes=10, replays=2, memory_bytes=1024, output_bytes=500)
    store = storemod.ResearchRunStore(output_root=str(tmp_path / "run"), budget=b,
                                      identity=identity, allowed_roots=[str(tmp_path)])
    store.begin()
    store.consume_time(120)
    store.consume_nodes(3)
    store.reserve_replay(1)
    store.observe_memory(700)
    store.reserve_output(50)
    high = store.high_water()
    assert high["consumed"] == {"time_ms": 120, "nodes": 3, "replays": 1,
                                "memory_bytes": 700, "output_bytes": 50}
    assert high["remaining"] == {"time_ms": 880, "nodes": 7, "replays": 1,
                                 "memory_bytes": 324, "output_bytes": 450}
    assert store.verify_chain()["seq"] == 6


def test_budget_exhaustion_preserves_incumbent_frontier_and_no_false_optimal(tmp_path):
    storemod, servicemod = api()
    snap = snapshot()
    identity = servicemod.snapshot_identity(snap)
    b = budget(nodes=5, replays=1, output_bytes=1024)
    store = storemod.ResearchRunStore(output_root=str(tmp_path / "run"), budget=b,
                                      identity=identity, allowed_roots=[str(tmp_path)])
    store.begin()
    store.checkpoint({"incumbent": {"actions": ["a0"], "cost": 3.0},
                      "completed_layers": ["L1", "L2"], "frontier": [["a0", "a1"]],
                      "optimal_proven": False})
    with pytest.raises(storemod.BudgetExhausted) as caught:
        store.consume_nodes(6)
    assert caught.value.dimension == "nodes"
    final = store.finalize("budget_exhausted", payload={"incumbent": {"actions": ["a0"], "cost": 3.0},
                                                        "completed_layers": ["L1", "L2"],
                                                        "frontier": [["a0", "a1"]],
                                                        "optimal_proven": False,
                                                        "proof_status": "not_proven"})
    assert final["complete"] is False
    assert store.high_water()["consumed"]["nodes"] == 0
    assert (store.run_dir / "aborted.json").exists() and not (store.run_dir / "complete.json").exists()
    saved = store.load_checkpoint()
    assert saved["payload"]["incumbent"]["actions"] == ["a0"]
    assert saved["payload"]["frontier"] == [["a0", "a1"]]
    assert saved["payload"]["completed_layers"] == ["L1", "L2"]


def test_calibration_is_counted_and_capped(tmp_path):
    storemod, servicemod = api()
    snap = snapshot()
    identity = servicemod.snapshot_identity(snap)
    store = storemod.ResearchRunStore(output_root=str(tmp_path / "run"), budget=budget(time_ms=1000),
                                      identity=identity, allowed_roots=[str(tmp_path)],
                                      max_calibrations=1)
    store.begin()
    store.consume_calibration(time_ms=80)
    assert store.high_water()["consumed"]["time_ms"] == 80
    assert store.high_water()["calibrations"] == 1
    with pytest.raises(storemod.BudgetExhausted):
        store.consume_calibration(time_ms=10)


def test_recovery_keeps_cumulative_budget_and_time(tmp_path):
    storemod, servicemod = api()
    snap = snapshot()
    identity = servicemod.snapshot_identity(snap)
    b = budget()
    store = storemod.ResearchRunStore(output_root=str(tmp_path / "run"), budget=b,
                                      identity=identity, allowed_roots=[str(tmp_path)],
                                      run_id="resume01")
    store.begin()
    store.consume_time(100)
    store.consume_nodes(3)
    store.checkpoint({"incumbent": {"actions": ["a0"]}, "frontier": [], "optimal_proven": False})
    run_id = store.run_id
    resumed = storemod.ResearchRunStore(output_root=str(tmp_path / "run"), budget=b,
                                        identity=identity, allowed_roots=[str(tmp_path)],
                                        run_id=run_id)
    recovered = resumed.recover(identity=identity, budget=b, time_ms=7)
    assert recovered["status"] == "recovered" and recovered["checkpoint"]["payload"]["incumbent"]
    assert recovered["high_water"]["consumed"]["time_ms"] == 107
    assert recovered["high_water"]["consumed"]["nodes"] == 3
    resumed.consume_nodes(1)
    assert resumed.high_water()["consumed"] == {"time_ms": 107, "nodes": 4, "replays": 0,
                                                "memory_bytes": 0, "output_bytes": 0}


def test_recovery_rejects_wrong_source_scope_and_budget(tmp_path):
    storemod, servicemod = api()
    snap = snapshot()
    identity = servicemod.snapshot_identity(snap)
    b = budget()
    store = storemod.ResearchRunStore(output_root=str(tmp_path / "run"), budget=b,
                                      identity=identity, allowed_roots=[str(tmp_path)],
                                      run_id="resume02")
    store.begin()
    store.consume_time(10)
    store.consume_nodes(4)
    store.checkpoint({"incumbent": None, "frontier": [["a0"]], "optimal_proven": False})
    run_id = store.run_id

    def reopened():
        return storemod.ResearchRunStore(output_root=str(tmp_path / "run"), budget=b,
                                         identity=identity, allowed_roots=[str(tmp_path)],
                                         run_id=run_id)

    wrong_source = dict(identity, source_bundle_fingerprint="f" * 64)
    with pytest.raises(storemod.RecoveryRejected, match="identity_mismatch:source_bundle_fingerprint"):
        reopened().recover(identity=wrong_source, budget=b)
    wrong_scope = dict(identity, scope_fingerprint="e" * 64)
    with pytest.raises(storemod.RecoveryRejected, match="identity_mismatch:scope_fingerprint"):
        reopened().recover(identity=wrong_scope, budget=b)
    with pytest.raises(storemod.RecoveryRejected, match="budget_regression:nodes"):
        reopened().recover(identity=identity, budget=dict(b, nodes=2))


def test_recovery_rejects_stale_checkpoint(tmp_path):
    storemod, servicemod = api()
    snap = snapshot()
    identity = servicemod.snapshot_identity(snap)
    b = budget()
    store = storemod.ResearchRunStore(output_root=str(tmp_path / "run"), budget=b,
                                      identity=identity, allowed_roots=[str(tmp_path)],
                                      run_id="resume03")
    store.begin()
    store.checkpoint({"incumbent": {"actions": ["a0"]}})
    store.append_receipt("post_checkpoint_note", {"reason": "ledger advanced"})
    resumed = storemod.ResearchRunStore(output_root=str(tmp_path / "run"), budget=b,
                                        identity=identity, allowed_roots=[str(tmp_path)],
                                        run_id=store.run_id)
    with pytest.raises(storemod.RecoveryRejected, match="stale_checkpoint"):
        resumed.recover(identity=identity, budget=b)


def test_hash_chain_detects_tampered_receipt(tmp_path):
    storemod, servicemod = api()
    snap = snapshot()
    identity = servicemod.snapshot_identity(snap)
    store = storemod.ResearchRunStore(output_root=str(tmp_path / "run"), budget=budget(),
                                      identity=identity, allowed_roots=[str(tmp_path)])
    store.begin()
    store.consume_time(5)
    store.consume_nodes(1)
    assert store.verify_chain()["valid"] is True
    ledger = store.run_dir / "ledger.jsonl"
    rows = ledger.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(rows[1])
    tampered["payload"]["amount"] = 999
    rows[1] = json.dumps(tampered, ensure_ascii=False, sort_keys=True)
    ledger.write_text("\n".join(rows) + "\n", encoding="utf-8")
    with pytest.raises(storemod.StoreError, match="ledger_hash_chain_invalid"):
        store.verify_chain()


def test_checkpoint_atomic_failure_preserves_last_valid(tmp_path, monkeypatch):
    storemod, servicemod = api()
    snap = snapshot()
    identity = servicemod.snapshot_identity(snap)
    store = storemod.ResearchRunStore(output_root=str(tmp_path / "run"), budget=budget(),
                                      identity=identity, allowed_roots=[str(tmp_path)])
    store.begin()
    store.checkpoint({"value": 1})
    latest = store.run_dir / "checkpoints" / "checkpoint.latest.json"
    before = latest.read_bytes()
    checkpoint_files = sorted(p.name for p in (store.run_dir / "checkpoints").iterdir())
    baseline = lambda *a, **k: (_ for _ in ()).throw(OSError("atomic replace failed"))
    monkeypatch.setattr(storemod, "_replace", baseline)
    with pytest.raises(storemod.StoreError):
        store.checkpoint({"value": 2})
    assert latest.read_bytes() == before
    assert sorted(p.name for p in (store.run_dir / "checkpoints").iterdir()) == checkpoint_files
    assert not [p for p in (store.run_dir / "checkpoints").iterdir() if p.suffix == ".tmp"]


def test_write_failure_never_marks_complete(tmp_path, monkeypatch):
    storemod, servicemod = api()
    snap = snapshot()
    identity = servicemod.snapshot_identity(snap)
    store = storemod.ResearchRunStore(output_root=str(tmp_path / "run"), budget=budget(),
                                      identity=identity, allowed_roots=[str(tmp_path)])
    store.begin()
    monkeypatch.setattr(storemod, "_atomic_write",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk write failed")))
    with pytest.raises(storemod.StoreError):
        store.finalize("completed", payload={"result": "partial"})
    assert not (store.run_dir / "complete.json").exists()
    assert not (store.run_dir / "result.json").exists()
    assert "finalize:completed" not in (store.run_dir / "ledger.jsonl").read_text(encoding="utf-8")


def test_disk_space_failure_is_rejected_before_writing(tmp_path, monkeypatch):
    storemod, servicemod = api()
    snap = snapshot()
    identity = servicemod.snapshot_identity(snap)
    root = tmp_path / "run"
    store = storemod.ResearchRunStore(output_root=str(root), budget=budget(), identity=identity,
                                      allowed_roots=[str(tmp_path)])
    monkeypatch.setattr(storemod.shutil, "disk_usage",
                        lambda *a, **k: type("U", (), {"total": 10, "used": 10, "free": 1024})())
    with pytest.raises(storemod.StoreError, match="insufficient disk space"):
        store.begin()
    assert not (root / "ledger.jsonl").exists()


def test_abnormal_worker_exit_never_marks_complete(tmp_path, monkeypatch):
    storemod, servicemod, snap, identity, b, store, permit = fixture_run(tmp_path)
    fake = {"status": "crashed", "exit_code": 9, "elapsed_ms": 5, "pid": 1,
            "peak_memory_bytes": 1024, "output_bytes": 0, "process_tree_reclaimed": True,
            "job_processes": [], "termination_reason": None, "blockers": []}
    monkeypatch.setattr(servicemod, "run_worker", lambda **kw: fake)
    result = servicemod.run_controlled_research(approved_snapshot=snap,
                                                execution_permit=permit, run_store=store)
    assert result["status"] == "crashed" and result["complete"] is False
    assert not (store.run_dir / "complete.json").exists()
    assert (store.run_dir / "aborted.json").exists()
    assert "finalize:completed" not in (store.run_dir / "ledger.jsonl").read_text(encoding="utf-8")


def test_no_p14_p15_p17_or_radar_model_call_without_real_authorization(tmp_path, monkeypatch):
    storemod, servicemod, snap, identity, b, store, permit = fixture_run(tmp_path)
    calls = {"p14": 0, "p15": 0, "p17": 0, "radar": 0, "los": 0}
    corridor = importlib.import_module("cns_planner.algorithms.corridor.v1").CNSServiceCorridorV1
    gap = importlib.import_module("cns_planner.algorithms.corridor_gap.v1").CNSCorridorGapAnalyzerV1
    continuous = importlib.import_module(
        "cns_planner.algorithms.continuous_service.v1").ContinuousServiceAcceptabilityV1
    radar = importlib.import_module("cns_planner.algorithms.radar_layout.v1")
    lookup = importlib.import_module("cns_planner.algorithms.radar_layout.terrain_los_lookup")
    counter = lambda key: (lambda *a, **k: calls.__setitem__(key, calls[key] + 1))
    monkeypatch.setattr(corridor, "evaluate", counter("p14"))
    monkeypatch.setattr(gap, "evaluate", counter("p15"))
    monkeypatch.setattr(continuous, "evaluate", counter("p17"))
    monkeypatch.setattr(radar, "solve_layout", counter("radar"))
    monkeypatch.setattr(lookup.TerrainLosLookup, "evaluate", counter("los"))
    result = servicemod.run_controlled_research(approved_snapshot=snap,
                                                execution_permit=permit, run_store=store)
    assert result["status"] == "completed" and result["native_model_calls"] == 0
    assert calls == {"p14": 0, "p15": 0, "p17": 0, "radar": 0, "los": 0}


def test_output_root_rejects_traversal_protected_and_junction(tmp_path):
    storemod, servicemod = api()
    snap = snapshot()
    identity = servicemod.snapshot_identity(snap)
    b = budget()
    with pytest.raises(storemod.StoreError):
        storemod.ResearchRunStore(output_root=str(tmp_path / ".." / "escape"), budget=b,
                                  identity=identity, allowed_roots=[str(tmp_path)])
    with pytest.raises(storemod.StoreError):
        storemod.ResearchRunStore(output_root=str(tmp_path / "run"), budget=b,
                                  identity=identity, allowed_roots=[])
    source = Path(servicemod.__file__).resolve().parents[2]
    for protected in (source / "projects" / "round38_real_acceptance" / "current_project.json",
                      source / "artifacts" / "proposal", source / ".acceptance_setup" /
                      "round38_real" / "stop"):
        with pytest.raises(storemod.StoreError):
            storemod.approve_output_root(str(protected), allowed_roots=[str(source)])
    fake_recovery = tmp_path / "local_recovery_20261018_100555"
    fake_recovery.mkdir()
    with pytest.raises(storemod.StoreError):
        storemod.approve_output_root(str(fake_recovery / "run"), allowed_roots=[str(tmp_path)])
    protected = tmp_path / "protected"
    protected.mkdir()
    with pytest.raises(storemod.StoreError):
        storemod.approve_output_root(str(protected / "run"), allowed_roots=[str(tmp_path)],
                                    protected_roots=[str(protected)])
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "junction"
    created = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                             capture_output=True)
    assert created.returncode == 0, created.stderr
    try:
        with pytest.raises(storemod.StoreError):
            storemod.approve_output_root(str(link / "run"), allowed_roots=[str(tmp_path)])
    finally:
        link.rmdir()


def test_reparse_parent_is_rechecked_before_write(tmp_path):
    storemod, servicemod = api()
    target = tmp_path / "swapped"
    target.mkdir()
    link = tmp_path / "junction"
    created = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                             capture_output=True)
    assert created.returncode == 0, created.stderr
    try:
        with pytest.raises(storemod.StoreError):
            storemod._assert_safe_write_path(str(link / "payload.json"))
    finally:
        link.rmdir()


def test_output_quota_stays_within_64mib_policy(tmp_path):
    storemod, servicemod = api()
    snap = snapshot()
    identity = servicemod.snapshot_identity(snap)
    assert storemod.DEFAULT_OUTPUT_LIMIT_BYTES == 64 * 1024 * 1024
    with pytest.raises(storemod.StoreError):
        storemod.ResearchRunStore(output_root=str(tmp_path / "run"),
                                  budget=budget(output_bytes=64 * 1024 * 1024 + 1),
                                  identity=identity, allowed_roots=[str(tmp_path)])
    small = storemod.ResearchRunStore(output_root=str(tmp_path / "small"),
                                      budget=budget(output_bytes=64 * 1024),
                                      identity=identity, allowed_roots=[str(tmp_path)])
    small.begin()
    with pytest.raises(storemod.BudgetExhausted):
        small.reserve_output(65 * 1024)
    assert small.high_water()["consumed"]["output_bytes"] == 0


def test_proof_replay_requires_reserved_budget(tmp_path):
    storemod, servicemod = api()
    snap = snapshot()
    identity = servicemod.snapshot_identity(snap)
    store = storemod.ResearchRunStore(output_root=str(tmp_path / "run"),
                                      budget=budget(replays=1), identity=identity,
                                      allowed_roots=[str(tmp_path)])
    store.begin()
    store.reserve_replay(1)
    assert store.high_water()["consumed"]["replays"] == 1
    with pytest.raises(storemod.BudgetExhausted) as caught:
        store.reserve_replay(1)
    assert caught.value.dimension == "replays"


def test_snapshot_with_executable_fields_is_rejected(tmp_path):
    storemod, servicemod = api()
    snap = snapshot()
    snap["actions"] = [{"action_id": "a0", "runtime": {"module": "os"}}]
    with pytest.raises(ValueError):
        servicemod.snapshot_identity(snap)
    snap = snapshot()
    snap["scenario"]["callback"] = "os.system"
    with pytest.raises(ValueError):
        servicemod.snapshot_identity(snap)
