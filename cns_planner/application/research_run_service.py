"""R2.2-A: trusted execution permits and controlled research run orchestration.

An ``ExecutionPermit`` is an in-memory, HMAC-sealed object issued only from an
explicit fixture authorization.  A JSON document that claims ``approved``,
``trusted``, ``permit_id`` or a recomputed fingerprint cannot become a permit:
no parser, no copy and no new ``run_id`` can reproduce the process-local seal.
The production authorization entry is deliberately closed in this round.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import sys
import uuid

from ..domain.joint_feasibility import digest, json_value
from ..domain.research_scenario import bounded_json, sha256_text
from .research_evaluation_adapter import M3_METHOD_VERSION, M4_METHOD_VERSION
from .research_run_store import (
    BudgetExhausted, ResearchRunStore, StoreError, approve_output_root,
    checked_budget, checked_identity)
from .research_worker import RESULT_SCHEMA, SOURCE_ROOT, WORKER_SCHEMA, minimal_environment, run_worker

SNAPSHOT_SCHEMA = "research_run_snapshot.v1"
RESULT_SCHEMA_SERVICE = "research_run_service_result.v1"
AUTHORIZATION_KIND_FIXTURE = "fixture_test"
PRODUCTION_AUTHORIZATION_ENABLED = False
TRUSTED_MODEL_VERSIONS = {"m4_joint": M4_METHOD_VERSION, "m3_binding": M3_METHOD_VERSION}
_SECRET = secrets.token_bytes(32)


class ResearchAuthorizationError(PermissionError):
    """Authorization object is not trusted or the production entry is closed."""


def _seal_fields(fields):
    payload = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    return hmac.new(_SECRET, payload, hashlib.sha256).hexdigest()


def _parse_deadline(value):
    if type(value) is not str or not value:
        raise ValueError("timezone-aware deadline required")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise ValueError("timezone-aware deadline required")
    return moment.astimezone(timezone.utc)


def _remaining_ms(deadline):
    return int((deadline - datetime.now(timezone.utc)).total_seconds() * 1000)


@dataclass(frozen=True)
class ResearchAuthorization:
    authorization_ref: str
    kind: str
    budget: dict
    output_root: str
    deadline_utc: str
    scope_fingerprint: str
    allowed_roots: tuple
    allowed_calibrations: int
    seal: str = ""


@dataclass(frozen=True)
class ExecutionPermit:
    permit_id: str
    authorization_ref: str
    authorization_kind: str
    identity: dict
    budget: dict
    output_root: str
    deadline_utc: str
    allowed_calibrations: int
    seal: str = ""


def _authorization_fields(authorization):
    return {"authorization_ref": authorization.authorization_ref, "kind": authorization.kind,
            "budget": authorization.budget, "output_root": authorization.output_root,
            "deadline_utc": authorization.deadline_utc,
            "scope_fingerprint": authorization.scope_fingerprint,
            "allowed_roots": list(authorization.allowed_roots),
            "allowed_calibrations": authorization.allowed_calibrations}


def _permit_fields(permit):
    return {"permit_id": permit.permit_id,
            "authorization_ref": permit.authorization_ref,
            "authorization_kind": permit.authorization_kind,
            "identity": permit.identity, "budget": permit.budget,
            "output_root": permit.output_root, "deadline_utc": permit.deadline_utc,
            "allowed_calibrations": permit.allowed_calibrations}


def _verify_authorization(authorization):
    if type(authorization) is not ResearchAuthorization:
        raise ResearchAuthorizationError("authorization_not_trusted")
    if authorization.kind != AUTHORIZATION_KIND_FIXTURE:
        raise ResearchAuthorizationError("production_authorization_closed")
    expected = _seal_fields(_authorization_fields(authorization))
    if not hmac.compare_digest(expected, authorization.seal):
        raise ResearchAuthorizationError("authorization_tampered")


def issue_fixture_authorization(*, authorization_ref, budget, output_root, deadline_utc,
                                scope_fingerprint, allowed_roots, allowed_calibrations=0):
    """Fixture-test-only authorization. Production code must not call this."""
    if type(authorization_ref) is not str or not authorization_ref:
        raise ResearchAuthorizationError("authorization reference required")
    checked = checked_budget(budget)
    approval = approve_output_root(output_root, allowed_roots=allowed_roots)
    if not sha256_text(scope_fingerprint):
        raise ResearchAuthorizationError("scope fingerprint required")
    _parse_deadline(deadline_utc)
    if type(allowed_calibrations) is not int or allowed_calibrations < 0:
        raise ResearchAuthorizationError("invalid calibration allowance")
    authorization = ResearchAuthorization(
        authorization_ref=authorization_ref, kind=AUTHORIZATION_KIND_FIXTURE,
        budget=checked, output_root=approval["resolved_path"], deadline_utc=deadline_utc,
        scope_fingerprint=scope_fingerprint, allowed_roots=tuple(str(Path(root)) for root in allowed_roots),
        allowed_calibrations=allowed_calibrations)
    object.__setattr__(authorization, "seal", _seal_fields(_authorization_fields(authorization)))
    return authorization


def issue_production_authorization(**_kwargs):
    """The real research authorization entry is intentionally closed in R2.2."""
    raise ResearchAuthorizationError("production_authorization_closed")


def snapshot_identity(snapshot):
    value = bounded_json(snapshot)
    if type(value) is not dict:
        raise ValueError("snapshot mapping required")
    if value.get("schema") != SNAPSHOT_SCHEMA:
        raise ValueError("snapshot schema")
    if value.get("evidence_track") != "synthetic_fixture":
        raise ValueError("fixture_only_snapshot_required")
    for field in ("scenario", "conditional_domain", "source_bundle", "objectives", "scope",
                  "model_versions"):
        if type(value.get(field)) is not dict or not value[field]:
            raise ValueError(f"snapshot field required: {field}")
    actions = value.get("actions")
    if type(actions) is not list or not actions:
        raise ValueError("frozen action set required")
    action_ids = []
    for action in actions:
        if type(action) is not dict or type(action.get("action_id")) is not str or not action["action_id"]:
            raise ValueError("frozen action identity required")
        action_ids.append(action["action_id"])
    if len(set(action_ids)) != len(action_ids):
        raise ValueError("duplicate frozen action")
    declared = value["conditional_domain"].get("actions")
    if declared is not None and sorted(declared) != sorted(action_ids):
        raise ValueError("conditional domain action set mismatch")
    if value["model_versions"] != TRUSTED_MODEL_VERSIONS:
        raise ValueError("model_version_mismatch")
    return {"source_bundle_fingerprint": digest(value["source_bundle"]),
            "scenario_fingerprint": digest(value["scenario"]),
            "domain_fingerprint": digest(value["conditional_domain"]),
            "action_set_fingerprint": digest(actions),
            "objective_fingerprint": digest(value["objectives"]),
            "model_versions": dict(value["model_versions"]),
            "scope_fingerprint": digest(value["scope"])}


def issue_fixture_permit(*, authorization, approved_snapshot):
    """Bind one trusted authorization to a recomputed snapshot identity."""
    _verify_authorization(authorization)
    identity = snapshot_identity(approved_snapshot)
    if identity["scope_fingerprint"] != authorization.scope_fingerprint:
        raise ResearchAuthorizationError("authorization_scope_mismatch")
    permit = ExecutionPermit(
        permit_id=uuid.uuid4().hex, authorization_ref=authorization.authorization_ref,
        authorization_kind=authorization.kind, identity=identity,
        budget=json_value(authorization.budget), output_root=authorization.output_root,
        deadline_utc=authorization.deadline_utc,
        allowed_calibrations=authorization.allowed_calibrations)
    object.__setattr__(permit, "seal", _seal_fields(_permit_fields(permit)))
    return permit


def _verify_permit(permit):
    if type(permit) is not ExecutionPermit:
        raise ResearchAuthorizationError("execution_permit_not_trusted")
    if permit.authorization_kind != AUTHORIZATION_KIND_FIXTURE:
        raise ResearchAuthorizationError("execution_permit_not_trusted")
    expected = _seal_fields(_permit_fields(permit))
    if not hmac.compare_digest(expected, permit.seal):
        raise ResearchAuthorizationError("execution_permit_tampered")
    checked_identity(permit.identity)
    checked_budget(permit.budget)
    if _remaining_ms(_parse_deadline(permit.deadline_utc)) <= 0:
        raise ResearchAuthorizationError("execution_permit_expired")


def _blocked(blockers, **extra):
    result = {"schema": RESULT_SCHEMA_SERVICE, "status": "blocked", "blockers": list(blockers),
              "complete": False, "run_id": None, "worker_status": None, "fixture_only": True,
              "real_execution_authorized": False,
              "production_authorization_enabled": PRODUCTION_AUTHORIZATION_ENABLED,
              "native_model_calls": 0, "persisted_to_project_state": False,
              "final_eligible": False, "new_site_stage_unlocked": False,
              "optimal_proven": False, "proof_status": "not_proven"}
    result.update(extra)
    return result


def _check_result_file(run_store, request):
    try:
        raw = (run_store.worker_root / "result.json").read_bytes()
        if not raw or len(raw) > 1024 * 1024:
            return None
        data = bounded_json(json.loads(raw.decode("utf-8")))
    except (OSError, ValueError):
        return None
    if type(data) is not dict or data.get("schema") != RESULT_SCHEMA:
        return None
    if data.get("handler") != "identity_digest" or data.get("model_calls") != 0:
        return None
    if data.get("request_digest") != digest(request):
        return None
    return data


def run_controlled_research(*, approved_snapshot, execution_permit, run_store):
    try:
        _verify_permit(execution_permit)
    except ResearchAuthorizationError as exc:
        return _blocked([str(exc)])
    try:
        identity = snapshot_identity(approved_snapshot)
    except ValueError as exc:
        return _blocked([f"approved_snapshot_invalid:{exc}"])
    if identity != execution_permit.identity:
        return _blocked(["permit_identity_mismatch"])
    if not isinstance(run_store, ResearchRunStore):
        return _blocked(["trusted_run_store_required"])
    if run_store.output_root != execution_permit.output_root:
        return _blocked(["output_root_mismatch"])
    if run_store.identity != identity:
        return _blocked(["store_identity_mismatch"])
    if run_store.budget != execution_permit.budget:
        return _blocked(["store_budget_mismatch"])
    if run_store.max_calibrations != execution_permit.allowed_calibrations:
        return _blocked(["calibration_scope_mismatch"])
    remaining = _remaining_ms(_parse_deadline(execution_permit.deadline_utc))
    if remaining <= 0:
        return _blocked(["execution_permit_expired"])
    try:
        run_store.begin(label=execution_permit.authorization_ref)
    except StoreError as exc:
        return _blocked([f"run_store_unavailable:{exc}"])
    try:
        high = run_store.high_water()
        time_budget = min(remaining, high["remaining"]["time_ms"])
        if time_budget <= 0:
            final = run_store.finalize("budget_exhausted", payload={"dimension": "time_ms"})
            return _blocked(["time_budget_exhausted"], run_id=run_store.run_id,
                            high_water=final["high_water"])
        request = {"schema": WORKER_SCHEMA, "handler": "identity_digest",
                   "payload": {"identity": identity,
                               "snapshot_digest": digest(approved_snapshot),
                               "run_id": run_store.run_id,
                               "purpose": "controlled_research_identity"}}
        written = run_store.write_worker_json("request.json", request)
        run_store.append_receipt("worker_start",
                                 {"deadline_ms": time_budget,
                                  "memory_budget_bytes": high["remaining"]["memory_bytes"],
                                  "output_budget_bytes": high["remaining"]["output_bytes"]})
        worker_limit = min(run_store.max_output_bytes,
                           high["remaining"]["output_bytes"] + written["bytes"])
        worker = run_worker(
            argv=[sys.executable, str(SOURCE_ROOT / "cns_planner" / "application" /
                                      "research_worker.py"),
                  "--request", str(run_store.worker_root / "request.json"),
                  "--result", str(run_store.worker_root / "result.json")],
            cwd=str(SOURCE_ROOT), deadline_ms=time_budget,
            memory_bytes=high["remaining"]["memory_bytes"],
            output_root=str(run_store.worker_root), output_bytes_limit=worker_limit,
            poll_ms=25, env=minimal_environment())
        run_store.append_receipt("worker_finished",
                                 {"status": worker["status"], "exit_code": worker["exit_code"],
                                  "elapsed_ms": worker["elapsed_ms"],
                                  "peak_memory_bytes": worker["peak_memory_bytes"],
                                  "output_bytes": worker["output_bytes"],
                                  "process_tree_reclaimed": worker["process_tree_reclaimed"]})
        status = worker["status"]
        blockers = list(worker["blockers"])
        try:
            run_store.consume_time(max(0, int(worker["elapsed_ms"])))
            run_store.observe_memory(max(0, int(worker["peak_memory_bytes"])))
            extra = max(0, int(worker["output_bytes"]) - written["bytes"])
            run_store.reserve_output(extra)
        except BudgetExhausted as exc:
            status = "budget_exhausted"
            blockers.append(f"budget_exhausted:{exc.dimension}")
        if status == "completed":
            data = _check_result_file(run_store, request)
            if data is None:
                status = "blocked"
                blockers.append("worker_result_invalid")
            else:
                payload = {"worker_result_digest": digest(data["result"]),
                           "identity_fingerprint": digest(identity),
                           "incumbent": None, "frontier": [], "completed_layers": [],
                           "optimal_proven": False, "proof_status": "not_proven",
                           "fixture_only": True}
                final = run_store.finalize("completed", payload=payload)
                return {**_blocked([], run_id=run_store.run_id), "status": "completed",
                        "complete": True, "worker_status": worker["status"],
                        "worker": _worker_summary(worker),
                        "high_water": final["high_water"],
                        "result_fingerprint": final["result_fingerprint"]}
        final = run_store.finalize(status, payload={"worker_status": worker["status"],
                                                    "blockers": blockers,
                                                    "optimal_proven": False,
                                                    "proof_status": "not_proven"})
        return {**_blocked(blockers, run_id=run_store.run_id), "status": status,
                "complete": final["complete"], "worker_status": worker["status"],
                "worker": _worker_summary(worker), "high_water": final["high_water"]}
    except (StoreError, OSError, ValueError, PermissionError) as exc:
        try:
            final = run_store.finalize("aborted", payload={"error": type(exc).__name__})
        except Exception:
            final = {"complete": False, "high_water": None}
        return _blocked([f"run_failed:{type(exc).__name__}"], run_id=run_store.run_id,
                        high_water=final.get("high_water"))


def _worker_summary(worker):
    return {"pid": worker["pid"], "exit_code": worker["exit_code"],
            "elapsed_ms": worker["elapsed_ms"],
            "peak_memory_bytes": worker["peak_memory_bytes"],
            "output_bytes": worker["output_bytes"],
            "process_tree_reclaimed": worker["process_tree_reclaimed"],
            "job_processes": list(worker["job_processes"]),
            "termination_reason": worker["termination_reason"]}


__all__ = ["run_controlled_research", "snapshot_identity", "issue_fixture_authorization",
           "issue_fixture_permit", "issue_production_authorization", "ExecutionPermit",
           "ResearchAuthorization", "TRUSTED_MODEL_VERSIONS",
           "PRODUCTION_AUTHORIZATION_ENABLED"]
