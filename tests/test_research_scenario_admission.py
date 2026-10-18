"""R1 static admission: no native model execution or production snapshots."""
from copy import deepcopy
import hashlib
import importlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import pytest
from test_research_scenario import fixture, parent, api as scenario_api
from cns_planner.domain.joint_feasibility import digest


def api():return importlib.import_module("cns_planner.application.research_scenario_admission")


def inputs():
    f=fixture()
    req=[dict(constraint_id="c-gap",service_key="C:communication",max_gap_m=1000,
        source_ref="original-required",consumer=None)]
    scope=dict(required_constraint_inventory=req,included_constraint_ids=["c-gap"],
        excluded_constraints=[],unconsumed_requirements=deepcopy(req))
    f["route_scenario"]["required_scope"]=scope
    f["route_scenario"]["full_required_snapshot_ref"]["sha256"]=digest(req)
    sc=scenario_api().validate_research_scenario(**f)
    from cns_planner.application.research_scenario_builder import build_conditional_domain
    domain=build_conditional_domain(parent_domain_review=parent(),scenario=sc,
        assumption_registry=sc["policies"][0]["entries"])
    route=sc["route_scenario"]
    chain=dict(route_id=route["route_id"],path_sha256=route["path_sha256"],
        operating_layer_id=route["operating_layer_id"],altitude_value=route["altitude_value"],
        vertical_reference=route["vertical_reference"],baseline_sha256=route["baseline_ref"]["sha256"],
        validation_id="v1",adoption_id="a1",projection_fingerprint="p1",
        current_applicability="current",stale_reason=None,source_ref="test-chain")
    ev=dict(operational=dict(chain,status="passed",kind="layered_risk_aware_operational_route"),
        validation=dict(chain,status="engineering_conditionally_accepted_candidate",use_scope="engineering_demo_only"),
        adoption=dict(chain,status="published_engineering_demo"),operating_layer=dict(chain,status="confirmed"))
    return dict(scenario=sc,conditional_domain=domain,route_evidence=ev,
        consumer_capabilities=dict(consumers={},supported_action_families=[]),run_intent=dict(mode="inspect"))


def run(a=None):return api().inspect_research_admission(**(a or inputs()))


def test_stale_route_blocks():
    a=inputs();a["route_evidence"]["operational"].update(status="stale",stale_reason="workspace_changed")
    r=run(a);assert r["status"]=="blocked" and not r["execution_enabled"]
    assert "route_evidence_not_current:operational" in r["blockers"]


def test_demo_passed_with_stale_layer_chain_blocks():
    a=inputs();a["route_evidence"]["operating_layer"].update(validation_id="old-v",adoption_id="old-a",current_applicability="stale_evidence")
    r=run(a);assert r["route_evidence_status"]=="blocked"
    assert "route_chain_mismatch:operating_layer:validation_id" in r["blockers"]


def test_reference_endpoint_line_cannot_impersonate_operational_path():
    a=inputs();a["route_evidence"]["operational"]["kind"]="scenario"
    assert "operational_polyline_required" in run(a)["blockers"]


def test_1000m_unconsumed_survives_scope():
    a=inputs();before=deepcopy(a);r=run(a)
    assert a==before and r["required_inventory"]==a["scenario"]["route_scenario"]["required_scope"]["required_constraint_inventory"]
    assert r["unconsumed_requirements"][0]["max_gap_m"]==1000
    assert "communication_1000m_unconsumed" in r["evidence_gaps"]
    assert r["execution_enabled"] is False and r["scoped_model_status"]=="not_evaluated"


@pytest.mark.parametrize("family",["S:radar_noncooperative","N:navigation_integrity_monitoring"])
def test_radar_endpoint_actions_unsupported(family):
    a=inputs();f=dict(regional_bundle=a["scenario"]["regional_bundle"],route_scenario=a["scenario"]["route_scenario"],policies=a["scenario"]["policies"])
    f["route_scenario"]["requested_actions"]=[dict(action_id="new",service_key=family,physical_site_id="T1",device_id="declared-model")]
    a["scenario"]=scenario_api().validate_research_scenario(**f)
    from cns_planner.application.research_scenario_builder import build_conditional_domain
    a["conditional_domain"]=build_conditional_domain(parent_domain_review=parent(),scenario=a["scenario"],assumption_registry=a["scenario"]["policies"][0]["entries"])
    # JSON cannot add new upstream capabilities.
    a["consumer_capabilities"]["supported_action_families"]=[family]
    assert "unsupported_action_family:"+family in run(a)["blockers"]


def test_partial_inventory_native_positive_stays_unknown():
    a=inputs();a["run_intent"]["native_m2_result"]={"navigation_status":"satisfied"}
    a["run_intent"]["provider_inventory_status"]="unknown"
    r=run(a);assert r["navigation_status"]=="unknown"
    assert r["native_m2_result"]==a["run_intent"]["native_m2_result"]


def test_independent_status_not_promoted_by_json():
    a=inputs();a["run_intent"].update(approved=True,trusted=True,independent_verification_status="verified")
    r=run(a);assert r["independent_verification_status"]=="not_verified" and not r["final_eligible"]


def test_static_contract_never_grants_execution():
    r=run();assert r["status"]=="static_contract_passed" and not r["execution_enabled"]
    assert r["complete_required_cns_status"]=="unknown" and r["real_domain_proof_status"]=="not_verified"


@pytest.mark.parametrize("key",["path_sha256","operating_layer_id","projection_fingerprint","baseline_sha256"])
def test_each_route_chain_identity_is_bound(key):
    a=inputs();a["route_evidence"]["validation"][key]="wrong"
    assert "route_chain_mismatch:validation:"+key in run(a)["blockers"]


def test_required_scope_cannot_drop_or_duplicate_constraints():
    a=inputs();a["scenario"]["route_scenario"]["required_scope"]["required_constraint_inventory"]=[]
    with pytest.raises(ValueError):run(a)


def test_domain_cannot_be_resealed_for_another_scenario():
    a=inputs();a["conditional_domain"]["scenario_fingerprint"]="wrong"
    from cns_planner.domain.joint_feasibility import seal
    a["conditional_domain"]=seal(a["conditional_domain"],"domain_fingerprint")
    assert "conditional_domain_scenario_mismatch" in run(a)["blockers"]


def test_d1_and_v1_still_reject_real_or_conditional_track():
    from test_existing_site_readonly_evidence import inputs as legacy_inputs
    from cns_planner.application.existing_site_optimization_evidence import build_readonly_run
    from cns_planner.algorithms.existing_site_lexicographic.v1 import identity
    a=legacy_inputs("engineering_research_conditional")
    r=build_readonly_run(**a)
    assert r["status"]=="blocked" and "real_execution_disabled_in_d1" in r["blockers"]
    with pytest.raises(ValueError):identity(a["domain_review"]["frozen_domain"],a["frozen_context"])


def test_controlled_input_sha_and_target(tmp_path):
    p=tmp_path/"input.json";p.write_text("{}",encoding="utf-8");h=hashlib.sha256(p.read_bytes()).hexdigest()
    result=api().validate_input_path(str(p),allowed_roots=[str(tmp_path)],expected_sha256=h)
    assert result["resolved_path"]==str(p.resolve()) and result["sha256"]==h
    with pytest.raises(ValueError):api().validate_input_path(str(p),allowed_roots=[str(tmp_path)],expected_sha256="0"*64)
    with pytest.raises(ValueError):api().validate_input_path(str(p),allowed_roots=[str(tmp_path/"other")],expected_sha256=h)


def test_output_root_rejects_traversal_and_unauthorized(tmp_path):
    inspect=api().validate_output_root
    assert inspect(str(tmp_path/"future"),allowed_roots=[str(tmp_path)])["path_status"]=="static_path_checked"
    assert not (tmp_path/"future").exists()
    for p in [str(tmp_path/".."/"escape"),str(tmp_path/"projects"/"proposal")]:
        with pytest.raises(ValueError):inspect(p,allowed_roots=[str(tmp_path)])
    with pytest.raises(ValueError):inspect(str(tmp_path),allowed_roots=[])
    with pytest.raises(ValueError):inspect(str(tmp_path/"source"/"new"),allowed_roots=[str(tmp_path)],forbidden_roots=[str(tmp_path/"source")])


def test_junction_parent_rejected_for_read_and_write(tmp_path):
    target=tmp_path/"real";target.mkdir();(target/"x.json").write_text("{}")
    link=tmp_path/"junction"
    result=subprocess.run(["cmd","/c","mklink","/J",str(link),str(target)],capture_output=True)
    assert result.returncode==0, result.stderr
    try:
        with pytest.raises(ValueError):api().validate_output_root(str(link/"new"),allowed_roots=[str(tmp_path)])
        with pytest.raises(ValueError):api().validate_input_path(str(link/"x.json"),allowed_roots=[str(tmp_path)],expected_sha256=hashlib.sha256(b"{}").hexdigest())
    finally:link.rmdir()


def cli():
    p=Path(__file__).resolve().parents[1]/"tools"/"inspect_research_scenario.py"
    spec=importlib.util.spec_from_file_location("research_cli_test",p);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m


def test_cli_never_dispatches_model(tmp_path,monkeypatch):
    a=inputs();p=tmp_path/"snapshot.json";p.write_text(json.dumps(a),encoding="utf-8")
    h=hashlib.sha256(p.read_bytes()).hexdigest();m=cli();stream=io.StringIO();monkeypatch.setattr(sys,"stdout",stream)
    before={k for k in sys.modules if k.startswith("qgis")}
    assert m.main(["--snapshot",str(p),"--sha256",h,"--input-root",str(tmp_path)])==0
    r=json.loads(stream.getvalue());assert not r["execution_enabled"]
    assert before=={k for k in sys.modules if k.startswith("qgis")}
    assert list(tmp_path.iterdir())==[p]


def test_cli_rejects_missing_hash_or_tampered_input(tmp_path,monkeypatch):
    p=tmp_path/"snapshot.json";p.write_text("{}",encoding="utf-8")
    m=cli();stream=io.StringIO();monkeypatch.setattr(sys,"stdout",stream)
    assert m.main(["--snapshot",str(p),"--sha256","0"*64,"--input-root",str(tmp_path)])==2
    assert json.loads(stream.getvalue())["execution_enabled"] is False


def rebuild(a):
    from cns_planner.application.research_scenario_builder import build_conditional_domain
    sc=a["scenario"]
    a["scenario"]=scenario_api().validate_research_scenario(regional_bundle=sc["regional_bundle"],route_scenario=sc["route_scenario"],policies=sc["policies"])
    a["conditional_domain"]=build_conditional_domain(parent_domain_review=a["conditional_domain"]["real_qualification_snapshot"],scenario=a["scenario"],assumption_registry=a["scenario"]["policies"][0]["entries"])
    return a


def test_original_required_scope_is_not_amended_by_derived_gaps():
    a=inputs();a["scenario"]["route_scenario"]["required_scope"]["unconsumed_requirements"]=[];rebuild(a)
    before=deepcopy(a);r=run(a)
    assert a==before and r["original_required_scope"]==before["scenario"]["route_scenario"]["required_scope"]
    assert r["unconsumed_requirements"][0]["max_gap_m"]==1000


def test_unconsumed_row_cannot_rewrite_original_requirement():
    a=inputs();a["scenario"]["route_scenario"]["required_scope"]["unconsumed_requirements"][0]["max_gap_m"]=999;rebuild(a)
    assert "unconsumed_requirement_content_mismatch" in run(a)["blockers"]


@pytest.mark.parametrize("section",["operational","validation","adoption","operating_layer"])
def test_malformed_route_record_fails_closed(section):
    a=inputs();a["route_evidence"][section]=[]
    r=run(a);assert r["status"]=="blocked" and not r["execution_enabled"]


def test_input_receipt_is_also_non_executable(tmp_path):
    p=tmp_path/"r.json";p.write_bytes(b"{}")
    assert api().validate_input_path(str(p),allowed_roots=[str(tmp_path)],expected_sha256=hashlib.sha256(b"{}").hexdigest())["execution_enabled"] is False


def test_output_allowed_root_cannot_itself_be_protected(tmp_path):
    for name in ["projects","local_recovery_20261018_100555"]:
        root=tmp_path/name;root.mkdir()
        with pytest.raises(ValueError):api().validate_output_root(str(root/"new"),allowed_roots=[str(root)])
