"""R1 pure offline contracts; invented small records are test evidence only."""
from copy import deepcopy
import importlib
import math
import pytest
from cns_planner.domain.joint_feasibility import digest


def api():
    return importlib.import_module("cns_planner.domain.research_scenario")


def fixture(route_id="route-a"):
    source = dict(source_id="towers", locator="X:/registered/towers.json", version="v1",
        sha256="a"*64, bytes=12, horizontal_crs="OGC:CRS84", axis_order="lon_lat",
        vertical_reference="unknown", validity="registered", source_ref="test-inventory:row1",
        license_status="internal", reader_version="v1", processing_lineage=[],
        role="computational", evidence_class="A")
    bundle = dict(bundle_id="shared-region", version="v1", sources=[source])
    route = dict(scenario_id=route_id+"-scenario", version="v1", evidence_track="engineering_research_conditional",
        region_bundle_ref=dict(bundle_id="shared-region", version="v1"),
        baseline_ref=dict(revision=1, sha256="b"*64, source_ref="test-baseline"),
        route_id=route_id, endpoint_refs=["start", "end"], path=[[1,1],[1.1,1.2],[2,2]],
        path_crs="OGC:CRS84", path_locator="fixture:operational", path_kind="operational",
        operating_layer_id="test-layer", altitude_value=100, altitude_units="m",
        vertical_reference="egm2008_orthometric", phase="cruise", scope="engineering_demo_only",
        route_evidence_refs=dict(validation_id="v1", adoption_id="a1", projection_fingerprint="p1"),
        policy_refs=[dict(policy_id="qualification", policy_version="v1")],
        full_required_snapshot_ref=dict(sha256="c"*64, source_ref="test-requirements"),
        consumer_dependencies=[dict(consumer_id="qualification", model_version="v1",
            source_ids=["towers"], parameter_ids=["install"])], requested_actions=[])
    route["path_sha256"] = digest(route["path"])
    entry = dict(parameter_id="install", parameter_kind="installation_qualification", site_id="T1",
        evidence_class="C", source_ref="declared-test-policy", value=None, range=None,
        units="eligibility", confidence="unassessed", evidence_tier="declared_assumption_pending",
        validity="pending_review", consumer="qualification", policy_version="v1",
        applicability="test-only", approval_ref=None, missing_reason="pending approval")
    policies=[dict(scenario_id=route["scenario_id"], policy_id="qualification", policy_version="v1", entries=[entry])]
    return dict(regional_bundle=bundle, route_scenario=route, policies=policies)


def validated(**changes):
    f=fixture(); f.update(changes); return api().validate_research_scenario(**f)


def parent():
    return dict(evidence_track="engineering_research", site_qualification_counts=dict(Q=0,U=373,E=0),
        site_ledger=[dict(physical_site_id="T"+str(i), qualification=dict(status="needs_evidence"),
            baseline_activation_status="unknown") for i in range(1,374)])


def build(scenario=None, entries=None):
    from cns_planner.application.research_scenario_builder import build_conditional_domain
    scenario=scenario or validated()
    if entries is None: entries=scenario["policies"][0]["entries"]
    return build_conditional_domain(parent_domain_review=parent(),scenario=scenario,assumption_registry=entries)


def test_conditional_domain_preserves_real_qualification():
    from cns_planner.application.research_scenario_builder import build_conditional_domain
    p=parent(); before=deepcopy(p); sc=validated()
    d=build_conditional_domain(parent_domain_review=p, scenario=sc, assumption_registry=sc["policies"][0]["entries"])
    assert p==before and d["real_qualification_snapshot"]==p
    assert d["real_qualification_snapshot"]["site_qualification_counts"]==dict(Q=0,U=373,E=0)
    assert d["execution_enabled"] is False


def test_unknown_baseline_not_zero():
    d=build()
    assert d["real_baseline_activation"] is None
    assert d["objective_measurement"] is None
    assert d["conditional_qualification"][0]["status"]=="unknown"
    assert d["conditional_qualification"][0]["value"] is None


@pytest.mark.parametrize("key",["source_ref","consumer","policy_version"])
def test_assumption_requires_scenario_policy_and_consumer(key):
    f=fixture(); f["policies"][0]["entries"][0][key]=None
    assert api().validate_research_scenario(**f)["status"]=="blocked"


def test_two_routes_share_bundle_without_data_copy():
    f=fixture(); g=fixture("route-b"); g["route_scenario"]["altitude_value"]=120
    a=api().validate_research_scenario(**f); b=api().validate_research_scenario(**g)
    assert a["source_bundle_fingerprint"]==b["source_bundle_fingerprint"]
    assert a["computational_dependency_fingerprint"]!=b["computational_dependency_fingerprint"]
    assert a["regional_bundle"]==b["regional_bundle"]==f["regional_bundle"]
    assert a["execution_enabled"] is False


def test_new_policy_preserves_old_result_identity():
    f=fixture(); before=deepcopy(f); a=api().validate_research_scenario(**f)
    g=deepcopy(f); g["policies"][0]["policy_version"]="v2"
    g["policies"][0]["entries"][0]["policy_version"]="v2"
    g["route_scenario"]["policy_refs"][0]["policy_version"]="v2"
    b=api().validate_research_scenario(**g)
    assert f==before and a["policies"]==before["policies"]
    assert a["computational_dependency_fingerprint"]!=b["computational_dependency_fingerprint"]


def test_airspace_display_only_excluded():
    f=fixture(); a=api().validate_research_scenario(**f)
    x=deepcopy(f["regional_bundle"]["sources"][0]);x.update(source_id="airspace",role="display_only_reference_layer")
    f["regional_bundle"]["sources"].append(x)
    b=api().validate_research_scenario(**f)
    assert a["source_bundle_fingerprint"]!=b["source_bundle_fingerprint"]
    assert a["computational_dependency_fingerprint"]==b["computational_dependency_fingerprint"]
    f["regional_bundle"]["sources"][-1]["sha256"]="d"*64
    c=api().validate_research_scenario(**f)
    assert b["computational_dependency_fingerprint"]==c["computational_dependency_fingerprint"]


def test_display_source_cannot_be_declared_computational():
    f=fixture();f["regional_bundle"]["sources"][0]["role"]="display_only_reference_layer"
    assert api().validate_research_scenario(**f)["status"]=="blocked"


@pytest.mark.parametrize("value",[float("nan"),float("inf"),object(),lambda:None,{"module_path":"arbitrary"}])
def test_non_finite_or_executable_values_rejected(value):
    f=fixture(); f["route_scenario"]["extra"]=value
    with pytest.raises(ValueError):api().validate_research_scenario(**f)


def test_json_resource_limits_and_cycles():
    f=fixture();v=[];v.append(v);f["route_scenario"]["extra"]=v
    with pytest.raises(ValueError):api().validate_research_scenario(**f)
    f=fixture();f["route_scenario"]["extra"]="x"*20000
    with pytest.raises(ValueError):api().validate_research_scenario(**f)


def test_pending_assumption_value_quarantined():
    f=fixture();f["policies"][0]["entries"][0]["value"]="assumed_eligible"
    sc=api().validate_research_scenario(**f)
    e=sc["policies"][0]["entries"][0]
    assert e["value"] is None and e["declared_value"]=="assumed_eligible"
    assert build(sc)["conditional_qualification"][0]["status"]=="unknown"


def test_approved_assumption_is_only_conditional():
    f=fixture();f["policies"][0]["entries"][0].update(value="assumed_eligible",approval_ref="review:test:1",validity="approved",evidence_tier="declared_assumption")
    d=build(api().validate_research_scenario(**f))
    assert d["conditional_qualification"][0]["status"]=="conditional_qualified_under_policy"
    assert d["real_qualification_snapshot"]["site_qualification_counts"]["Q"]==0


def test_registry_cannot_override_policy_or_fingerprint():
    sc=validated();entries=deepcopy(sc["policies"][0]["entries"]);entries[0]["value"]=99
    assert build(sc,entries)["status"]=="blocked"
    sc["route_scenario"]["altitude_value"]=90
    with pytest.raises(ValueError):build(sc)


@pytest.mark.parametrize("kind",["A","B","C","D"])
def test_evidence_classes_remain_separate(kind):
    f=fixture();e=f["policies"][0]["entries"][0];e["evidence_class"]=kind
    if kind=="B":e.update(method_ref="code:method",method_version="v1")
    sc=api().validate_research_scenario(**f)
    assert sc["evidence_register"][kind][0]["evidence_class"]==kind
    assert all(not sc["evidence_register"][k] for k in "ABCD" if k!=kind)


def test_synthetic_relabeling_rejected():
    f=fixture();f["route_scenario"]["evidence_track"]="synthetic_fixture"
    assert api().validate_research_scenario(**f)["status"]=="blocked"


def test_synthetic_equipment_marker_rejected():
    f=fixture();f["regional_bundle"]["sources"][0]["equipment_selection_status"]="synthetic_fixture_selected"
    assert api().validate_research_scenario(**f)["status"]=="blocked"


def test_json_byte_limit_checked_before_large_serialization(monkeypatch):
    module=api();original=module.json.dumps;called=[]
    def guarded(value,**kwargs):
        called.append(True);return original(value,**kwargs)
    monkeypatch.setattr(module.json,"dumps",guarded)
    with pytest.raises(ValueError):module.bounded_json(["x"*16000]*140)
    assert not called, "oversized JSON must be rejected before materializing its serialization"


@pytest.mark.parametrize("field,value",[("baseline_ref",[1]),("route_evidence_refs",[]),("policy_refs",{})])
def test_malformed_nested_contract_fails_closed(field,value):
    f=fixture();f["route_scenario"][field]=value
    try:r=api().validate_research_scenario(**f)
    except ValueError:return
    assert r["status"]=="blocked"


@pytest.mark.parametrize("field,value",[("policies",[1]),("consumer_dependencies",[1])])
def test_malformed_container_never_leaks_attribute_error(field,value):
    f=fixture()
    if field=="policies":f[field]=value
    else:f["route_scenario"][field]=value
    try:r=api().validate_research_scenario(**f)
    except ValueError:return
    assert r["status"]=="blocked"
