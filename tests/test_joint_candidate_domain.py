"""M4 A: frozen identity, scoped constraints and independent physical accounting."""
from copy import deepcopy
import importlib
import importlib.util
import pytest


def api():
    name = "cns_planner.application.joint_candidate_domain"
    assert importlib.util.find_spec(name) is not None, "M4 A domain API not implemented"
    return importlib.import_module(name)


def domain_args(activation="inactive_verified", n=2):
    return dict(discovery_manifest={"source_id":"fixture-sites", "version":"1", "digest":"sites-v1",
        "completeness":"controlled_subset", "evidence_track":"synthetic_fixture"},
        site_records=[dict(physical_site_id="tower:T1", aliases=["site:S1","facility:F1"],
            alias_source_ref="fixture-alias", existing_evidence={"status":"confirmed","source_ref":"asset-1"},
            baseline_activation_status=activation, activation_source_ref="baseline-1" if activation != "unknown" else None,
            qualification={"status":"qualified_under_model","source_ref":"q1"})],
        installed_inventory=[], device_catalog={"items":[{"device_id":f"D{i}"} for i in range(n)]},
        family_action_records=[dict(action_id=f"a{i}", action_type="add_device_to_explicit_site",
            device_id=f"D{i}", device_instance_id=f"new-{i}", physical_site_id="tower:T1",
            distinct_site_id="tower:T1", site_id="S1", planner_family="omnidirectional_site",
            subsystem="C", service_key="C:communication", reuse_class="tower_colocation_host",
            qualification={"status":"qualified_under_model","source_ref":"q-action"}) for i in range(n)],
        qualification_policy={"version":"fixture-v1"}, scenario_scope={"evidence_track":"synthetic_fixture"})


def fixture_domain(activation="inactive_verified", n=2):
    return api().freeze_existing_domain(**domain_args(activation, n))


def fixture_context(domain=None, scope=None):
    a=api(); domain=domain or fixture_domain()
    snapshot={"schema_version":"frozen_ue_rules.v1", "rules":[{"scenario_rule_id":"C-rule"}],
        "requirement_consumption":[], "input_fingerprint":"fixture-rule"}
    scope=scope or a.freeze_constraint_scope(frozen_rule_snapshot=snapshot,
        required_constraint_inventory=[dict(constraint_id="C-rule",field="C",source_ref="fixture",hard=True,applicable=True)],
        scope_declaration=dict(scope_id="scope-1",version="1",source_ref="fixture",kind="complete_required_cns",
            included_constraint_ids=["C-rule"],excluded_constraints=[]))
    return a.build_joint_search_context(frozen_rule_snapshot=snapshot, frozen_domain=domain,
        baseline_inputs={},evaluator_versions={"test":"1"},assessment_scope=scope)


@pytest.mark.parametrize("activation,want",[("active_verified",0),("inactive_verified",1),("unknown",None)])
def test_baseline_activation_counts(activation,want):
    a=api(); d=fixture_domain(activation)
    r=a.count_joint_plan_sites(actions=d["installable_actions"],frozen_domain=d,
        post_activation_evidence={"tower:T1":{"status":"active_verified","source_ref":"joint"}},
        participation_evidence={})
    assert r["N_newly_activated_existing_sites"] == want
    assert r["N_plan_sites"] == 1
    assert r["N_new_device_instances"] == 2
    assert r["N_participating_existing_sites"] is None
    assert r["activation_count_bounds"] == ([0,1] if want is None else [want,want])


def test_alias_identity_conflict_rejected():
    args=domain_args(); other=deepcopy(args["site_records"][0]); other["physical_site_id"]="tower:T2"
    args["site_records"].append(other)
    with pytest.raises(ValueError, match="identity"):
        api().freeze_existing_domain(**args)


def test_missing_qualification_not_proven_disqualified():
    args=domain_args(); original=deepcopy(args)
    args["family_action_records"][0]["qualification"]={"status":"ineligible","reasons":["missing permission"]}
    expected=deepcopy(args); r=api().freeze_existing_domain(**args)
    assert len(r["installable_actions"]) == 1
    assert r["unknown_candidates"][0]["action_id"] == "a0"
    assert not r["completeness"]["proof_eligible"]
    assert args == expected and original != args


def test_constraint_inventory_partition_and_scope_fingerprint():
    a=api(); snap={"rules":[{"scenario_rule_id":"C-rule"},{"scenario_rule_id":"Radar"}]}
    inv=[dict(constraint_id=x,field=x,source_ref="fixture",hard=True,applicable=True) for x in ("C-rule","Radar")]
    dec=dict(scope_id="s",version="1",source_ref="f",kind="declared_engineering_subset",
        included_constraint_ids=["C-rule"],excluded_constraints=[dict(constraint_id="Radar",reason="not evaluated",source_ref="fixture")])
    r=a.freeze_constraint_scope(frozen_rule_snapshot=snap,required_constraint_inventory=inv,scope_declaration=dec)
    dec["included_constraint_ids"].append("Radar"); dec["excluded_constraints"]=[]
    r2=a.freeze_constraint_scope(frozen_rule_snapshot=snap,required_constraint_inventory=inv,scope_declaration=dec)
    assert r["scope_fingerprint"] != r2["scope_fingerprint"]
    dec["included_constraint_ids"]=[]
    with pytest.raises(ValueError,match="partition"):
        a.freeze_constraint_scope(frozen_rule_snapshot=snap,required_constraint_inventory=inv,scope_declaration=dec)


def test_incomplete_discovery_blocks_domain_proof_and_inputs_are_detached():
    args=domain_args(); old=deepcopy(args); d=api().freeze_existing_domain(**args)
    assert not d["completeness"]["proof_eligible"]
    d["physical_sites"][0]["aliases"].append("changed")
    assert args == old


def test_context_domain_and_scope_changes_invalidate_identity():
    d=fixture_domain(); c=fixture_context(d)
    d2=fixture_domain("active_verified"); c2=fixture_context(d2)
    assert c["context_fingerprint"] != c2["context_fingerprint"]


def test_json_runtime_and_unproven_existing_site_rejected_or_unknown():
    args=domain_args(); args["site_records"][0]["existing_evidence"]={}
    r=api().freeze_existing_domain(**args)
    assert r["installable_actions"] == [] and len(r["unknown_candidates"]) == 2
    args["qualification_policy"]["runtime"]=lambda:None
    with pytest.raises(ValueError):api().freeze_existing_domain(**args)


def test_new_instance_identity_cannot_be_duplicated_or_already_installed():
    args=domain_args();args["family_action_records"][1]["device_instance_id"]="new-0"
    with pytest.raises(ValueError,match="instance"):api().freeze_existing_domain(**args)
    args=domain_args();args["installed_inventory"]=[{"device_instance_id":"new-0","physical_site_id":"tower:T1","device_id":"D0"}]
    with pytest.raises(ValueError,match="installed"):api().freeze_existing_domain(**args)


@pytest.mark.parametrize("alias",["site:S1","facility:F1"])
def test_installed_inventory_alias_cannot_hide_same_device_target(alias):
    args=domain_args();args["installed_inventory"]=[dict(device_instance_id="old-D0",physical_site_id=alias,device_id="D0")]
    with pytest.raises(ValueError,match="installed"):api().freeze_existing_domain(**args)
