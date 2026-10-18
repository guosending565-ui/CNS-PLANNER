"""Fixture regression and strict frozen-input acceptance remain separate."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import pytest
from cns_planner.application.research_real_c_evaluator import (
    freeze_research_inputs, prepare_inputs, project_facilities, evaluate_c,
    EvaluationBudget, EvaluationStopped, frozen_code_identity, inspect_real_state,
    SCENARIO_SHA, STATE_SHA, G4F_SHA,
)
from test_cns_corridor import ROUTE, GRID, SPATIAL, requirement_set, aircraft, device, facilities, policy

ROOT = Path(os.environ.get("CNS_RESEARCH_RECOVERY_ROOT",
    r"D:\Projects\_cns_round38_global_recovery"))
WORKTREE = Path(os.environ.get("CNS_RESEARCH_SOURCE_WORKTREE",
    r"D:\Projects\_cns_workspaces\integration\round38_acceptance\CNS-PLANNER"))


def fixture_inputs():
    state = dict(operational_routes=[dict(ROUTE, path_crs="OGC:CRS84")],
        spatial_3d=dict(SPATIAL, canonical_vertical_reference="egm2008_orthometric"),
        grid=dict(GRID, count=2), grid_attributes={"terrain": {"status": "passed", "cells": {}}},
        required_cns=requirement_set(), aircraft_profile=aircraft(),
        device_catalog={"items": [device()]}, existing_cns_facilities=facilities(),
        cns_corridor_policy=policy(), cns_planning_objectives={})
    return prepare_inputs(state, route_id="R1", source_identity={"kind": "synthetic_fixture"})


def action():
    return dict(action_id="a", physical_site_id="tower:t", device_instance_id="condinst:a",
        device_id="D1", service_key="C:communication", route_ids=["R1"], coordinate=[0.,0.])


def source_action():
    return dict(action_id="a", site_id="tower:t", device_id="D1", subsystem="C",
        service_key="C:communication", coordinate=[0.,0.],
        vertical={"service_origin_egm2008_m": 100., "confirmed": True},
        distinct_site_id="tower:t", device_service_key=None, device_type=None)


def test_projection_is_conditional_and_keeps_unknown_inventory():
    inputs=fixture_inputs(); before=deepcopy(inputs)
    projected, layers = project_facilities(inputs["state"]["existing_cns_facilities"],
        [action()], {"a":source_action()}, policy="conditional_assumed_installation")
    assert len(projected["items"]) == 2
    assert layers["installed_inventory_status"] == "unknown"
    assert layers["activation_baseline_status"] == "unknown"
    assert layers["qualification_status"] == "unknown"
    assert layers["baseline_facilities"] == inputs["state"]["existing_cns_facilities"]
    assert layers["conditional_facilities"][0]["devices"][0]["device_instance_id"] == "condinst:a"
    assert inputs == before
    with pytest.raises(ValueError): project_facilities({},[action()],{},policy="H1")
    with pytest.raises(ValueError): project_facilities({},[action()],{},policy="conditional_assumed_installation")


def test_real_p14_p15_chain_on_synthetic_fixture_keeps_source_and_scope():
    inputs=fixture_inputs(); before=deepcopy(inputs)
    result=evaluate_c(inputs, [], {}, budget=EvaluationBudget())
    assert result["status"] == "evaluated_synthetic_fixture"
    assert result["algorithm_versions"] == {"p14":"1.4", "p15":"1.3"}
    assert result["observations"]["p14"][0]["route_id"] == "R1"
    assert result["model_feasible"] is None
    assert result["final_eligible"] is False and result["optimal_proven"] is False
    assert result["unknown"]["installed_inventory"] == "unknown"
    assert inputs == before


def test_budget_has_a0_call_cap_and_no_retry():
    budget=EvaluationBudget()
    for _ in range(8): budget.consume([])
    with pytest.raises(EvaluationStopped, match="evaluation_limit"): budget.consume([])
    budget=EvaluationBudget(); budget.consume([action()])
    with pytest.raises(EvaluationStopped, match="a0_only"): budget.consume([action(),action()])
    budget.stop("cancelled")
    with pytest.raises(EvaluationStopped, match="cancelled"): budget.consume([])


def test_exception_stops_batch_without_retry_or_state_alias():
    inputs=fixture_inputs(); before=deepcopy(inputs); budget=EvaluationBudget()
    def fail(state,*args,**kwargs):
        state["grid"]["cells"].clear()
        raise RuntimeError("fixture failure")
    r=evaluate_c(inputs,[],{},budget=budget,chain=fail)
    assert r["status"] == "evaluation_failed" and budget.calls == 1
    assert inputs == before
    with pytest.raises(EvaluationStopped): evaluate_c(inputs,[],{},budget=budget,chain=fail)


@pytest.mark.parametrize("bad",["stale","not_evaluated","missing_data"])
def test_unusable_output_never_becomes_pass(bad):
    b=EvaluationBudget()
    def chain(*a,**kw):return {"status":bad,"algorithm_version":"1.4"},{"status":bad,"algorithm_version":"1.3"}
    r=evaluate_c(fixture_inputs(),[],{},budget=b,chain=chain)
    assert r["status"] == "evaluation_failed" and b.stopped
    assert r["scoped_c_corridor_status"] == "unknown"


def test_version_mismatch_and_missing_c_output_fail_closed():
    for version in ["1.3","1.4"]:
        def chain(*a,**kw):return {"status":"passed","algorithm_version":version,"routes":[]},{"status":"passed","algorithm_version":"1.3","routes":[]}
        r=evaluate_c(fixture_inputs(),[],{},budget=EvaluationBudget(),chain=chain)
        assert r["status"] == "evaluation_failed"


def test_empty_grid_is_blocked_before_model_call():
    inputs=fixture_inputs(); inputs["state"]["grid"]["cells"]=[]
    inputs=prepare_inputs(inputs["state"],route_id="R1",source_identity={"kind":"synthetic_fixture"})
    assert "grid_cells_not_materialized" in inputs["blockers"]
    b=EvaluationBudget(); r=evaluate_c(inputs,[],{},budget=b,chain=lambda *a,**kw:pytest.fail("model called"))
    assert r["status"] == "blocked" and b.calls == 0


def test_code_identity_binds_exact_g4f_bytes_and_head():
    rows={k:v for k,v in G4F_SHA.items()}
    a=frozen_code_identity("bbb07557c03358d728abceee525a0dc5024a2b53",rows)
    assert len(a["code_snapshot_fingerprint"]) == 64
    rows[next(iter(rows))]="0"*64
    with pytest.raises(ValueError):frozen_code_identity("bbb07557c03358d728abceee525a0dc5024a2b53",rows)


def frozen():
    s=ROOT/"p2a1_r0005_conditional_scenario_20261018/scenario.json"
    u=ROOT/"p2a3a_first_experiment_preparation_20261018/04_candidate_subdomain_A_C_only.json"
    if not s.exists() or not u.exists():
        if os.environ.get("CNS_RESEARCH_STRICT_ACCEPTANCE")=="1":pytest.fail("frozen real inputs missing")
        pytest.skip("frozen real inputs unavailable")
    return s.read_bytes(),u.read_bytes()


def test_frozen_seven_actions_original_scenario_and_envelope_strict():
    s,u=frozen(); f=freeze_research_inputs(s,u)
    assert f["scenario_sha256"] == SCENARIO_SHA
    assert len(f["actions"]) == 7 and len(f["excluded_actions"]) == 35
    assert f["envelope"]["source_identity"]["source_state_sha256"] == STATE_SHA
    assert f["envelope"]["execution_enabled"] is False
    assert f["sampling_rule"] == "p2a3a-subsample-v1"
    assert f["strict_equal_mileage_sampling"] is False
    with pytest.raises(ValueError): freeze_research_inputs(s+b" ",u)
    with pytest.raises(ValueError): freeze_research_inputs(s,u+b" ")


def test_real_rev503_strict_readonly_input_acceptance():
    s,u=frozen(); f=freeze_research_inputs(s,u)
    p=WORKTREE/"projects/round38_cns_experiment_b/current_project.json"
    if not p.exists():
        if os.environ.get("CNS_RESEARCH_STRICT_ACCEPTANCE")=="1":pytest.fail("rev503 missing")
        pytest.skip("rev503 unavailable")
    original=p.read_bytes()
    report=inspect_real_state(original,f)
    assert report["source_identity"]["state_sha256"] == STATE_SHA
    assert "grid_cells_not_materialized" in report["blockers"]
    assert report["route_ids"] == ["R0005"]
    assert report["projection_action_count"] == 7
    assert report["activation_policy"] == "H0"
    assert hashlib.sha256(p.read_bytes()).hexdigest() == STATE_SHA


@pytest.mark.parametrize("field,value,blocker", [
    ("path_crs", "EPSG:3857", "path_crs_unsupported"),
    ("vertical_reference", "ellipsoidal", "vertical_reference_unsupported"),
])
def test_crs_and_vertical_mismatch_block(field,value,blocker):
    state=fixture_inputs()["state"]
    if field=="path_crs":state["operational_routes"][0][field]=value
    else:state["spatial_3d"]["canonical_vertical_reference"]=value
    r=prepare_inputs(state,route_id="R1",source_identity={"kind":"synthetic_fixture"})
    assert blocker in r["blockers"]


def test_real_source_never_accepts_inprocess_safety_claim():
    state=fixture_inputs()["state"]
    r=prepare_inputs(state,route_id="R1",source_identity={"kind":"real_rev503", "hard_isolation_verified":True})
    assert "verified_hard_isolation_launcher_unavailable" in r["blockers"]
    r["blockers"]=[]
    b=EvaluationBudget()
    assert evaluate_c(r,[],{},budget=b)["status"] == "blocked"
    assert b.calls==0


def test_no_write_open_or_gc_global_change(monkeypatch):
    import builtins, gc
    # Import the production chain before instrumenting file access.
    from cns_planner.application.corridor_site_planning_service import rerun_corridor_chain
    inputs=fixture_inputs(); threshold=gc.get_threshold(); original=builtins.open
    def guarded(file,mode="r",*a,**kw):
        assert not any(c in mode for c in "wax+"), "unexpected filesystem write"
        return original(file,mode,*a,**kw)
    monkeypatch.setattr(builtins,"open",guarded)
    result=evaluate_c(inputs,[],{},budget=EvaluationBudget())
    assert result["status"]=="evaluated_synthetic_fixture"
    assert gc.get_threshold()==threshold


def test_source_origin_unknown_is_not_zero():
    source=source_action(); source["vertical"]["service_origin_egm2008_m"]=None
    with pytest.raises(ValueError,match="origin missing"):
        project_facilities(facilities(),[action()],{"a":source},policy="conditional_assumed_installation")


def test_node_time_and_output_logical_stop():
    import time
    b=EvaluationBudget(); b.nodes=64
    with pytest.raises(EvaluationStopped,match="node_limit"):b.consume([])
    b=EvaluationBudget(); b.started=time.monotonic()-601
    with pytest.raises(EvaluationStopped,match="time_limit"):b.consume([])
    b=EvaluationBudget()
    b.record_output(8388608)
    with pytest.raises(EvaluationStopped,match="output_limit"):b.record_output(1)


def test_summary_drops_large_voxel_lists_and_never_defaults_unknown_to_zero():
    def chain(*a,**kw):
        c={"subsystem":"C","status":"pending_confirmation", "satisfied_volume_fraction":None,
           "unknown_voxel_count":2,"unknown_voxel_ids":["x"]*100000,
           "full_width_continuity":{"status":"unknown","max_gap_m":None}}
        return ({"status":"pending_confirmation","algorithm_version":"1.4","routes":[{"route_id":"R1","subsystems":[c]}]},
                {"status":"pending_confirmation","algorithm_version":"1.3","routes":[{"route_id":"R1","subsystems":[c]}]})
    r=evaluate_c(fixture_inputs(),[],{},budget=EvaluationBudget(),chain=chain)
    c=r["observations"]["p14"][0]
    assert c["satisfied_volume_fraction"] is None
    assert "unknown_voxel_ids" not in c
    assert r["scoped_c_corridor_status"]=="unknown"
    assert len(json.dumps(r))<10000


def test_spoofed_fixture_label_or_mutation_cannot_call_chain():
    inputs=fixture_inputs()
    inputs["state"]["operational_routes"][0]["route_id"]="R0005"
    inputs["route_ids"]=["R0005"]
    result=evaluate_c(inputs,[],{},budget=EvaluationBudget(),chain=lambda *a,**kw:pytest.fail("unsafe model invocation"))
    assert result["status"]=="blocked"
    assert "unregistered_synthetic_fixture" in result["blockers"]
    prepared=prepare_inputs(inputs["state"],route_id="R0005",source_identity={"kind":"synthetic_fixture"})
    assert "unregistered_synthetic_fixture" in prepared["blockers"]


def test_empty_or_forged_route_scope_never_calls_model_or_passes():
    for scope in ([],["R0005"],["R1","R1"]):
        inputs=fixture_inputs(); inputs["route_ids"]=scope
        r=evaluate_c(inputs,[],{},budget=EvaluationBudget(),chain=lambda *a,**kw:pytest.fail("invalid route scope called model"))
        assert r["status"]=="blocked" and r["scoped_c_corridor_status"]=="unknown"
        assert "route_scope_mismatch" in r["blockers"]
