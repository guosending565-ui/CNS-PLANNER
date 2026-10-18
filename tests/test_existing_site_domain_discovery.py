"""M5-A synthetic contract tests; never load project state or real GIS."""
from copy import deepcopy
import importlib
import importlib.util
import pytest


def api(name):
    module = "cns_planner.application." + name
    assert importlib.util.find_spec(module) is not None, "M5-A behavior not implemented"
    return importlib.import_module(module)


class Reader:
    def __init__(self, snapshots): self.snapshots = snapshots
    def read_snapshot(self, *, source_spec): return deepcopy(self.snapshots[source_spec["source_id"]])


def host(rid, **extra):
    return dict(raw_id=rid, coordinates=[122.0, 30.0], existing_evidence={"status":"confirmed", "source_ref":"asset"}, **extra)


def discover(records, **overrides):
    spec=dict(source_id="towers", locator="fixture.xlsx", version="1", source_kind="tower", expected_historical_count=373)
    snapshot=dict(records=records, crs="EPSG:4326", vertical_datum=None, pagination={"complete":True,"pages_read":[1],"expected_pages":1}, filters=[], source_version="1")
    snapshot.update(overrides)
    return api("existing_site_domain_discovery").discover_existing_domain(discovery_scope={"scope_id":"fixture","evidence_track":"synthetic_fixture"}, source_manifest=[spec], reader=Reader({"towers":snapshot}))


def test_discovery_preserves_count_mismatch_and_missing_page():
    d=discover([host(str(i)) for i in range(372)], pagination={"complete":False,"pages_read":[1],"expected_pages":2})
    assert d["source_snapshots"][0]["raw_count"] == 372
    assert d["source_snapshots"][0]["expected_historical_count"] == 373
    assert d["discovery_status"] == "incomplete"
    assert len(d["physical_sites"]) == 372
    assert {x["reason"] for x in d["discovery_ledger"]} >= {"historical_count_mismatch", "missing_page"}


def test_alias_requires_source_and_existing_hosts_remain_distinct():
    rows=[host("a"),host("b"),host("c", alias_of="a", alias_source_ref="registry-map"), host("d",alias_of="a")]
    d=discover(rows)
    assert len(d["physical_sites"]) == 3
    assert d["raw_to_physical_aliases"]["towers:c"] == d["raw_to_physical_aliases"]["towers:a"]
    assert d["raw_to_physical_aliases"]["towers:d"] != d["raw_to_physical_aliases"]["towers:a"]
    spec=[dict(source_id="landing",source_kind="reference_landing_site",locator="f",version="1")]
    d=api("existing_site_domain_discovery").discover_existing_domain(discovery_scope={}, source_manifest=spec, reader=Reader({"landing":{"records":[host("p")],"pagination":{"complete":True}}}))
    assert not d["physical_sites"] and d["unknown_records"][0]["reason"] == "legal_host_evidence_missing"


def test_discovery_retains_duplicates_invalid_coordinates_and_missing_sources():
    d=discover([host("a"),host("a"),dict(raw_id="bad",coordinates=[999,30]),dict(coordinates=[122,30])])
    assert len(d["physical_sites"]) == 1
    assert len(d["unknown_records"]) == 3
    assert d["discovery_status"] == "incomplete"
    class Missing:
        def read_snapshot(self, *, source_spec): raise FileNotFoundError("source")
    d=api("existing_site_domain_discovery").discover_existing_domain(discovery_scope={},source_manifest=[{"source_id":"missing"}],reader=Missing())
    assert d["discovery_status"] == "incomplete" and d["discovery_ledger"][0]["reason"] == "missing_source"


def domain_api():
    name="cns_planner.domain.existing_site_optimization"
    return importlib.import_module(name)


def qualification_args():
    d=discover([host("a")])
    site=d["physical_sites"][0]
    fields=("legal_host","mount_height","capacity","power","backhaul","colocation","origin")
    site["qualification_evidence"]={k:{"status":"satisfied","source_ref":"survey"} for k in fields}
    site["colocation_rule"]={"max_devices":1,"source_ref":"load-survey"}
    actions=[dict(action_id="a"+str(i), action_type="add_device_to_explicit_site",physical_site_id="towers:a", distinct_site_id="towers:a",device_id="D"+str(i),device_instance_id="new"+str(i),qualification={"status":"qualified_under_model","source_ref":"permit"}) for i in range(2)]
    return dict(discovered_domain=d,installed_inventory=[],device_catalog={"version":"real-1","source_ref":"catalog","items":[{"device_id":"D0","model":"M0","source_ref":"manufacturer"},{"device_id":"D1","model":"M1","source_ref":"manufacturer"}]},qualification_policy={"version":"1","source_ref":"policy","family_action_records":actions})


def test_qualification_missing_is_not_disqualified():
    args=qualification_args(); q=api("existing_site_qualification")
    original=deepcopy(args); r=q.qualify_existing_actions(**args)
    assert len(r["Q"]) == 2 and not r["U"] and not r["E"]
    assert r["combination_constraints"][0]["max_devices"] == 1
    assert args == original
    args["discovered_domain"]["physical_sites"][0]["qualification_evidence"].pop("power")
    r=q.qualify_existing_actions(**args)
    assert len(r["U"]) == 2 and not r["E"]
    args["qualification_policy"]["family_action_records"][0]["qualification"]={"status":"disqualified_with_proof","source_ref":"rule","necessary_condition_evidence":"ban"}
    r=q.qualify_existing_actions(**args)
    assert len(r["E"]) == 1 and len(r["U"]) == 1


def objective_inputs(activation="inactive_verified", n=2):
    from test_joint_candidate_domain import fixture_domain
    d=fixture_domain(activation,n)
    policy={"schema_version":"existing_site_objective_policy.v1","decision_id":"DEC-M5-ACTIVATION-001","version":"1","source_ref":"approved-policy","prediction_policy_ref":"model-policy-1","model_version":"1.0","dependency_fingerprint":"dependencies-1","cost_basis":None}
    return dict(actions=deepcopy(d["installable_actions"]),frozen_domain=d,objective_policy=policy,activation_predictions={},participation_evidence={})


def prediction(args,status="predicted_active_under_model"):
    from cns_planner.application.candidate_ue_acceptability import candidate_action_set_fingerprint
    return {"tower:T1":dict(status=status,domain_fingerprint=args["frozen_domain"]["domain_fingerprint"],action_set_fp=candidate_action_set_fingerprint(actions=args["actions"]),model_version="1.0",prediction_policy_ref="model-policy-1",source_ref="model-transition",dependency_fingerprint="dependencies-1",conditions={k:{"status":"satisfied","source_ref":"model-input"} for k in ("power","backhaul","equipment","qualification")})}


@pytest.mark.parametrize("baseline,pred,want,bounds",[("active_verified",False,0,[0,0]),("inactive_verified",True,1,[1,1]),("inactive_verified",False,None,[0,1]),("unknown",True,None,[0,1])])
def test_activation_real_baseline_and_prediction_are_separate(baseline,pred,want,bounds):
    args=objective_inputs(baseline)
    if pred: args["activation_predictions"]=prediction(args)
    old=deepcopy(args); r=domain_api().measure_objectives(**args)
    assert r["values"]["activation"] == want and r["values"]["devices"] == 2
    assert r["activation_count_bounds"] == bounds
    assert r["activation_evidence_identity"] == ("real_baseline" if baseline == "active_verified" else "model_prediction")
    assert args == old
    assert r["persisted_to_project_state"] is False and r["new_site_stage_unlocked"] is False


def test_stale_or_reality_named_prediction_is_not_activation_evidence():
    args=objective_inputs(); args["activation_predictions"]=prediction(args)
    for change in ({"action_set_fp":"stale"},{"status":"active_verified"},{"model_version":"old"},{"conditions":{}}):
        attempt=deepcopy(args); attempt["activation_predictions"]["tower:T1"].update(change)
        assert domain_api().measure_objectives(**attempt)["values"]["activation"] is None


def test_device_instance_semantics():
    args=objective_inputs(); args["actions"][0]["service_keys"]=["C","N"]
    # Frozen membership must not be bypassed by altered action content.
    with pytest.raises(ValueError): domain_api().measure_objectives(**args)
    args=objective_inputs(n=1)
    assert domain_api().measure_objectives(**args)["values"]["devices"] == 1
    from cns_planner.domain.joint_feasibility import seal
    args["frozen_domain"]["installable_actions"][0]["action_type"]="reconfigure_device"
    args["frozen_domain"]["installed_device_instances"]=[dict(device_instance_id="new-0",physical_site_id="tower:T1",device_id="D0")]
    args["frozen_domain"]=seal(args["frozen_domain"],"domain_fingerprint")
    args["actions"]=deepcopy(args["frozen_domain"]["installable_actions"])
    assert domain_api().measure_objectives(**args)["values"]["devices"] == 0
    args=objective_inputs(); args["frozen_domain"]["installable_actions"][1]["device_instance_id"]="new-0"
    args["frozen_domain"]=seal(args["frozen_domain"],"domain_fingerprint"); args["actions"]=deepcopy(args["frozen_domain"]["installable_actions"])
    with pytest.raises(ValueError,match="instance"): domain_api().measure_objectives(**args)


def test_combination_capacity_cannot_be_overridden_by_qualified_single_actions():
    args=objective_inputs()
    args["objective_policy"]["combination_constraints"]=[{"physical_site_id":"tower:T1","max_devices":1,"source_ref":"survey"}]
    r=domain_api().measure_objectives(**args)
    assert r["input_status"] == "combination_conflict"
    assert r["values"]["activation"] is None


def review(kind="frozen_model_action_domain"):
    from test_joint_candidate_domain import fixture_domain
    return dict(schema_version="existing_site_domain_review.v1", frozen_domain=fixture_domain(),
        proof_scope={"kind":kind,"domain_scope":{"id":"fixture"},"constraint_scope":{"id":"C-only"}},
        dimensions={k:{"status":"complete","verified":True,"source_ref":"self"} for k in domain_api().DIMENSIONS},
        complete=True,verified=True,review_complete=True)


def test_self_declared_manifest_cannot_override_m4_proof_gate():
    d=review(); old=deepcopy(d)
    r=api("existing_site_domain_discovery").review_domain_completeness(domain_review=d,independent_review_receipts=[{"verified":True,"sha256":"self","source_ref":"self-signed"}])
    assert r["real_domain_proof_status"] == "not_verified"
    assert r["frozen_domain"]["completeness"]["proof_eligible"] is False
    assert r["dimensions"]["source_authentication"]["verification_status"] == "not_verified"
    assert d == old


@pytest.mark.parametrize("kind",["finite_synthetic_domain","frozen_model_action_domain","reviewed_real_existing_domain","complete_required_cns"])
def test_four_proof_scopes_do_not_promote_each_other(kind):
    d=review(kind)
    r=api("existing_site_domain_discovery").review_domain_completeness(domain_review=d,independent_review_receipts=[])
    assert r["proof_scope"]["kind"] == kind
    assert r["real_domain_proof_status"] == "not_verified"
    assert r["complete_required_cns_status"] == "unknown"
    assert r["new_site_stage_unlocked"] is False
    assert {x["reason"] for x in r["evidence_gaps"]} >= {"communication_1000m_unconsumed","rtk_segment_evidence_missing","radar_actions_unsupported","endpoint_actions_unsupported"}
    if kind in ("reviewed_real_existing_domain","complete_required_cns"):
        assert r["requested_scope_status"] == "blocked"


def test_invalid_scope_and_nonfinite_inputs_rejected():
    d=review("anything")
    with pytest.raises(ValueError): api("existing_site_domain_discovery").review_domain_completeness(domain_review=d,independent_review_receipts=[])
    args=objective_inputs(); args["objective_policy"]["cost_basis"]={"amount":float("nan")}
    with pytest.raises(ValueError): domain_api().measure_objectives(**args)


def test_prediction_fingerprint_matches_m3_and_inactive_prediction_counts_zero():
    args=objective_inputs(); args["activation_predictions"]=prediction(args,"predicted_inactive_under_model")
    r=domain_api().measure_objectives(**args)
    assert r["values"]["activation"] == 0 and r["activation_count_bounds"] == [0,0]
    assert r["action_set_fp"] == args["activation_predictions"]["tower:T1"]["action_set_fp"]


def test_prediction_must_bind_domain_and_dependency_snapshot():
    args=objective_inputs(); args["objective_policy"]["dependency_fingerprint"]="deps-current"
    args["activation_predictions"]=prediction(args)
    args["activation_predictions"]["tower:T1"].update(domain_fingerprint="wrong",dependency_fingerprint="deps-old")
    assert domain_api().measure_objectives(**args)["values"]["activation"] is None


def test_inconsistent_source_identity_cannot_qualify_actions():
    args=qualification_args(); args["discovered_domain"]["discovery_status"]="input_inconsistent"
    with pytest.raises(ValueError,match="identity"):
        api("existing_site_qualification").qualify_existing_actions(**args)


def test_empty_actions_do_not_close_unknown_site_qualification():
    args=qualification_args(); args["qualification_policy"]["family_action_records"]=[]
    args["discovered_domain"]["physical_sites"][0]["qualification_evidence"]={}
    r=api("existing_site_qualification").qualify_existing_actions(**args)
    assert r["qualification_status"] == "evidence_incomplete"


def test_frozen_site_capacity_is_enforced_without_optional_policy_copy():
    from cns_planner.domain.joint_feasibility import seal
    args=objective_inputs(); args["frozen_domain"]["physical_sites"][0]["colocation_rule"]={"max_devices":1,"source_ref":"survey"}
    args["frozen_domain"]=seal(args["frozen_domain"],"domain_fingerprint")
    assert domain_api().measure_objectives(**args)["input_status"] == "combination_conflict"


def test_missing_capacity_limit_is_needs_evidence():
    args=qualification_args(); args["discovered_domain"]["physical_sites"][0]["colocation_rule"]={"source_ref":"survey"}
    r=api("existing_site_qualification").qualify_existing_actions(**args)
    assert len(r["U"]) == 2 and not r["Q"]


def test_unsupported_same_model_slots_remain_unknown():
    args=qualification_args(); actions=args["qualification_policy"]["family_action_records"]
    actions[1]["device_id"]=actions[0]["device_id"]; actions[0]["slot_id"]="S1"; actions[1]["slot_id"]="S2"
    r=api("existing_site_qualification").qualify_existing_actions(**args)
    assert len(r["U"]) == 1 and r["U"][0]["reason"] == "unsupported_multiplicity"
    assert r["qualification_status"] == "evidence_incomplete"
