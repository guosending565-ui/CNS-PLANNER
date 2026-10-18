"""R2.1 controlled fixture-only conditional evaluation adapter tests.

All model inputs below are invented micro fixtures. They are not R0005, not a real
GIS read, and not evidence that any real conditional action is executable.
"""
from copy import deepcopy
import builtins
import importlib
import importlib.util
import io

import pytest

from cns_planner.application.candidate_ue_acceptability import candidate_action_set_fingerprint
from cns_planner.domain.joint_feasibility import digest


def api():
    name = "cns_planner.application.research_evaluation_adapter"
    assert importlib.util.find_spec(name) is not None, "R2.1 evaluation adapter not implemented"
    return importlib.import_module(name)


def m4_version():
    return api().M4_METHOD_VERSION


_CACHE = {}


def m4_fixture():
    if "domain" not in _CACHE:
        from test_joint_feasibility_evaluation import model_fixture
        _CACHE["domain"], _CACHE["context"] = model_fixture()
    return _CACHE["domain"], _CACHE["context"]


def source_row(**extra):
    row = dict(source_id="towers", locator="X:/registered/fixture-towers.json", version="v1",
        sha256="a"*64, bytes=12, horizontal_crs="OGC:CRS84", axis_order="lon_lat",
        vertical_reference="unknown", validity="registered", source_ref="test-inventory:row1",
        license_status="registered_internal", reader_version="v1", processing_lineage=[],
        role="computational", evidence_class="A")
    row.update(extra)
    return row


def policy_entries(site_id="tower:T1", approval_ref="review:test:1"):
    install = dict(parameter_id="install", parameter_kind="installation_qualification",
        site_id=site_id, evidence_class="C", source_ref="declared-test-policy",
        value="assumed_eligible", range=None, units="eligibility", confidence="unassessed",
        evidence_tier="declared_assumption", validity="approved", consumer="m4_joint",
        policy_version="v1", applicability="fixture-only", approval_ref=approval_ref)
    provider = dict(parameter_id="provider-inventory", parameter_kind="operation_segment_evidence",
        evidence_class="D", source_ref="declared-test-policy", value=None, range=None,
        units="status", confidence="unassessed", evidence_tier="declared_unknown",
        validity="pending_review", consumer="m4_joint", policy_version="v1",
        applicability="fixture-only", approval_ref=None,
        missing_reason="provider inventory/device/handover evidence unavailable")
    coverage = dict(parameter_id="c-coverage", parameter_kind="equipment_capability",
        evidence_class="B", source_ref="code:algorithms/coverage/geometric_3d.py",
        method_ref="cns_planner.algorithms.coverage.geometric_3d", method_version="1.0",
        value=0.85, range=None, units="ratio", confidence="model_default",
        evidence_tier="engineering_proxy", validity="model_scope_only", consumer="m4_joint",
        policy_version="v1", applicability="fixture-only")
    return [install, provider, coverage]


def micro_parent():
    return dict(evidence_track="engineering_research",
        site_qualification_counts=dict(Q=0, U=1, E=0),
        site_ledger=[dict(physical_site_id="tower:T1",
            qualification=dict(status="needs_evidence"), baseline_activation_status="unknown")])


def real_parent():
    return dict(evidence_track="engineering_research",
        site_qualification_counts=dict(Q=0, U=373, E=0),
        site_ledger=[dict(physical_site_id="T"+str(i),
            qualification=dict(status="needs_evidence"), baseline_activation_status="unknown")
            for i in range(1, 374)])


def research_actions(domain):
    rows = []
    for action in domain["installable_actions"]:
        rows.append(dict(action_id=action["action_id"], service_key=action["service_key"],
            planner_family=action["planner_family"], physical_site_id=action["physical_site_id"],
            device_id=action["device_id"], device_instance_id=action["device_instance_id"],
            slot_id="slot-"+action["action_id"], dependency_action_ids=[],
            applicable_models=["m4_joint"],
            source_class="C" if action["service_key"] == "C:communication" else "B",
            assumption_refs=["install"] if action["service_key"] == "C:communication" else [],
            research_qualification=dict(status="conditional_qualified_under_policy",
                parameter_id="install")))
    return rows


def loose_actions(site_id="T1"):
    return [
        dict(action_id="comm", service_key="C:communication", planner_family="omnidirectional_site",
            physical_site_id=site_id, device_id="C-fixture", device_instance_id="new-c",
            slot_id="slot-c", dependency_action_ids=[], applicable_models=["m4_joint"],
            source_class="C", assumption_refs=["install"]),
        dict(action_id="rtk", service_key="N:rtk_augmentation",
            planner_family="navigation_reference_station", physical_site_id=site_id,
            device_id="RTK-fixture", device_instance_id="new-rtk", slot_id="slot-rtk",
            dependency_action_ids=[], applicable_models=["m4_joint"],
            source_class="B", assumption_refs=[])]


def build_scenario(actions, entries, route_id="R1", altitude=100, route_extra=None,
                   source_extra=None):
    path = [[122.0, 30.0], [122.05, 30.05], [122.1, 30.1]]
    inventory = [
        dict(constraint_id="C:communication:max_gap_m", service_key="C:communication",
            max_gap_m=1000, hard=True, source_ref="original-required",
            consumer={"consumer_id": "m4_joint", "version": m4_version()}),
        dict(constraint_id="N:rtk_augmentation", service_key="N:rtk_augmentation",
            kind="engineering_geometry", hard=True, source_ref="original-required",
            consumer={"consumer_id": "m4_joint", "version": m4_version()}),
    ]
    route = dict(scenario_id=route_id+"-scenario", version="v1",
        evidence_track="engineering_research_conditional",
        region_bundle_ref=dict(bundle_id="shared-region", version="v1"),
        baseline_ref=dict(revision=1, sha256="b"*64, source_ref="test-baseline"),
        route_id=route_id, endpoint_refs=["N005", "N006"], path=path, path_crs="OGC:CRS84",
        path_locator="fixture:operational", path_kind="operational",
        path_sha256=digest(path), operating_layer_id="ALT-080",
        altitude_value=altitude, altitude_units="m", vertical_reference="egm2008_orthometric",
        phase="cruise", scope="engineering_demo_only",
        route_evidence_refs=dict(validation_id="LRV-FIXTURE", adoption_id="LRA-FIXTURE",
            projection_fingerprint="proj-fixture"),
        policy_refs=[dict(policy_id="qualification", policy_version="v1")],
        full_required_snapshot_ref=dict(sha256=digest(inventory),
            source_ref="test-requirements"),
        consumer_dependencies=[dict(consumer_id="m4_joint", model_version=m4_version(),
            source_ids=["towers"],
            parameter_ids=["install", "provider-inventory", "c-coverage"])],
        requested_actions=actions,
        required_scope=dict(required_constraint_inventory=inventory,
            included_constraint_ids=[r["constraint_id"] for r in inventory],
            excluded_constraints=[], unconsumed_requirements=deepcopy(inventory)))
    if route_extra:
        route.update(route_extra)
    source = source_row(**(source_extra or {}))
    bundle = dict(bundle_id="shared-region", version="v1", sources=[source])
    policies = [dict(scenario_id=route["scenario_id"], policy_id="qualification",
        policy_version="v1", entries=entries)]
    return dict(regional_bundle=bundle, route_scenario=route, policies=policies)


def route_evidence(route):
    chain = dict(route_id=route["route_id"], path_sha256=route["path_sha256"],
        operating_layer_id=route["operating_layer_id"], altitude_value=route["altitude_value"],
        vertical_reference=route["vertical_reference"],
        baseline_sha256=route["baseline_ref"]["sha256"],
        validation_id="LRV-FIXTURE", adoption_id="LRA-FIXTURE",
        projection_fingerprint="proj-fixture", current_applicability="current",
        stale_reason=None, source_ref="fixture-chain")
    return dict(
        operational=dict(chain, status="passed",
            kind="layered_risk_aware_operational_route"),
        validation=dict(chain, status="engineering_conditionally_accepted_candidate",
            use_scope="engineering_demo_only"),
        adoption=dict(chain, status="published_engineering_demo"),
        operating_layer=dict(chain, status="confirmed"))


def fixture_declaration(domain, context, route):
    manifest = dict(fixture_id="r21-m4-micro", fixture_version="v1",
        evidence_track="synthetic_fixture",
        context_fingerprint=context["context_fingerprint"],
        domain_fingerprint=domain["domain_fingerprint"],
        scope_fingerprint=context["scope_fingerprint"],
        scope_id=context["assessment_scope"]["scope_id"],
        action_set_fingerprint=candidate_action_set_fingerprint(
            actions=domain["installable_actions"]),
        route_id=route["route_id"], operating_layer_id=route["operating_layer_id"])
    return dict(fixture_id="r21-m4-micro", fixture_version="v1",
        evidence_track="synthetic_fixture", manifest=manifest,
        fixture_sha256=digest(manifest))


def applicability(scenario, domain):
    route = scenario["route_scenario"]
    return dict(scenario_fingerprint=scenario["scenario_fingerprint"],
        domain_fingerprint=domain["domain_fingerprint"],
        source_bundle_fingerprint=scenario["source_bundle_fingerprint"],
        computational_dependency_fingerprint=domain["computational_dependency_fingerprint"],
        real_parent_fingerprint=digest(domain["real_qualification_snapshot"]),
        route_id=route["route_id"], path_sha256=route["path_sha256"],
        altitude_value=route["altitude_value"],
        vertical_reference=route["vertical_reference"],
        operating_layer_id=route["operating_layer_id"], scope_id="geometry-subset")


def consumer_registry(scenario, domain, fixture_block, native_domain, context):
    entry = dict(consumer_id="m4_joint", method_ref=api().M4_METHOD_REF,
        method_version=m4_version(),
        supported_action_families=["C:communication", "N:rtk_augmentation"],
        input_applicability=applicability(scenario, domain),
        assumption_approval_basis={"install": "review:test:1"},
        navigation_evidence=dict(provider_inventory_status="unknown",
            device_status="satisfied", handover_status="unknown",
            speed_profile_status="unknown", backup_integrity_status="unknown",
            source_ref="trusted-fixture:navigation"),
        dependent_consumers=["m2_rtk", "m3_binding"])
    if fixture_block is not None:
        entry["fixture"] = dict(fixture_id=fixture_block["fixture_id"],
            fixture_version=fixture_block["fixture_version"],
            fixture_sha256=fixture_block["fixture_sha256"])
        entry["fixture_material"] = dict(frozen_context=deepcopy(context),
            frozen_domain=deepcopy(native_domain), m3_evidence_registry={})
    return dict(schema_version=api().REGISTRY_SCHEMA, consumers=[entry])


def components(*, actions=None, parent=None, site_id="tower:T1", entries=None,
               fixture=True, route_id="R1", altitude=100, route_extra=None,
               source_extra=None, native_m2=None):
    native_domain, context = m4_fixture()
    entries = entries if entries is not None else policy_entries(site_id)
    actions = actions if actions is not None else research_actions(native_domain)
    scenario = build_scenario(actions, entries, route_id=route_id, altitude=altitude,
        route_extra=route_extra, source_extra=source_extra)
    from cns_planner.application.research_scenario_builder import build_conditional_domain
    from cns_planner.domain.research_scenario import validate_research_scenario
    validated = validate_research_scenario(**scenario)
    domain = build_conditional_domain(parent_domain_review=parent or micro_parent(),
        scenario=validated, assumption_registry=validated["policies"][0]["entries"])
    block = fixture_declaration(native_domain, context, validated["route_scenario"]) if fixture else None
    return dict(frozen=dict(schema_version=api().INPUTS_SCHEMA, scenario=validated,
        conditional_domain=domain, route_evidence=route_evidence(validated["route_scenario"]),
        fixture=block, native_m2_result=native_m2),
        actions=actions, registry=consumer_registry(validated, domain, block,
            native_domain, context), scenario=validated, domain=domain, context=context)


def run(comp):
    return api().evaluate_conditional_actions(frozen_inputs=comp["frozen"],
        actions=comp["actions"], consumer_registry=comp["registry"])


def block_model(monkeypatch, calls):
    def bomb(*args, **kwargs):
        calls.append(True)
        raise AssertionError("model must not be called for this input")
    monkeypatch.setattr(api(), "JointModelEvaluator", bomb)


def test_fixture_evaluation_projects_complete_action_set():
    comp = components()
    out = run(comp)
    assert out["status"] == "evaluated_fixture_only"
    assert out["execution_enabled"] is False
    assert out["persisted_to_project_state"] is False
    assert out["new_site_stage_unlocked"] is False
    assert out["final_eligible"] is False
    assert out["applied_action_ids"] == ["comm", "rtk"]
    assert out["missing_action_ids"] == []
    assert out["scoped_model_status"] == "model_feasible"
    assert out["complete_required_cns_status"] == "unknown"
    assert out["navigation_status"] == "unknown"
    assert out["evidence_status"] == "fixture_only"
    assert out["independent_verification_status"] == "not_verified"
    assert all(out["native_parent_chain"].values()), out["native_parent_chain"]
    assert out["native_model_results"]["m4_joint"]["evaluation_status"] == "evaluated"
    assert out["native_model_results"]["m3_binding"]["input_status"] == "consistent"
    assert out["native_input_fingerprints"]["context_fingerprint"] == comp["context"]["context_fingerprint"]
    assert {r["consumer_id"] for r in out["consumer_identities"]} >= {"m4_joint", "m2_rtk", "m3_binding"}


def test_action_projection_records_site_model_instance_slot_family_dependencies():
    out = run(components())
    rows = {r["action_id"]: r for r in out["action_results"]}
    for action_id in ("comm", "rtk"):
        row = rows[action_id]
        assert row["status"] == "supported"
        assert row["physical_site_id"] == "tower:T1"
        assert row["device_model_id"] in ("C-fixture", "RTK-fixture")
        assert row["device_instance_id"] in ("new-c", "new-rtk")
        assert row["slot_id"] == "slot-"+action_id
        assert row["family"] in ("C:communication", "N:rtk_augmentation")
        assert row["dependency_action_ids"] == []
        assert row["applicable_models"] == ["m4_joint"]
        assert len(row["projection_digest"]) == 64
    description = out["model_inputs"]
    assert description["research_input_origin"] == "engineering_research_conditional"
    assert description["model_input_origin"] == "synthetic_fixture"
    assert description["consumer_id"] == "m4_joint"
    assert description["method_version"] == m4_version()
    assert description["route"]["route_id"] == "R1"
    assert description["route"]["altitude_value"] == 100
    assert {action["action_id"] for action in description["actions"]} == {"comm", "rtk"}
    assert {action["action_id"] for action in description["action_manifest"]} == {"comm", "rtk"}
    assert description["sources"][0]["source_id"] == "towers"


def test_real_parent_qualification_ledger_is_preserved():
    parent = real_parent()
    before = deepcopy(parent)
    comp = components(actions=loose_actions("T1"), parent=parent, site_id="T1", fixture=False)
    out = run(comp)
    assert comp["frozen"]["conditional_domain"]["real_qualification_snapshot"] == before
    assert out["real_qualification_snapshot"] == before
    assert out["real_qualification_snapshot"]["site_qualification_counts"] == dict(Q=0, U=373, E=0)
    assert out["status"] == "not_evaluated_real_inputs"


def test_non_fixture_real_inputs_do_not_call_model(monkeypatch):
    comp = components(fixture=False)
    calls = []
    block_model(monkeypatch, calls)
    out = run(comp)
    assert out["status"] == "not_evaluated_real_inputs"
    assert out["model_call_status"] == "refused_non_fixture_inputs"
    assert out["native_model_results"] == {}
    assert calls == []


def test_json_self_approval_without_trusted_basis_cannot_unlock_model(monkeypatch):
    comp = components()
    del comp["registry"]["consumers"][0]["assumption_approval_basis"]
    calls = []
    block_model(monkeypatch, calls)
    out = run(comp)
    assert out["status"] == "blocked"
    assert "assumption_approval_basis_missing:install" in out["blockers"]
    assert comp["scenario"]["policies"][0]["entries"][0]["approval_ref"] == "review:test:1"
    assert out["model_inputs"] is None
    assert calls == []


def test_json_self_inflated_approval_ref_cannot_match_trusted_basis(monkeypatch):
    comp = components(entries=policy_entries(approval_ref="self-approved"))
    calls = []
    block_model(monkeypatch, calls)
    out = run(comp)
    assert "assumption_approval_basis_mismatch:install" in out["blockers"]
    assert calls == []


def test_json_trusted_verified_flags_do_not_promote_status(monkeypatch):
    comp = components(route_extra=dict(trusted=True, verified=True,
        independent_verification_status="verified", approved=True))
    out = run(comp)
    assert out["independent_verification_status"] == "not_verified"
    assert out["final_eligible"] is False
    assert out["execution_enabled"] is False


def test_source_self_authentication_flag_is_not_authentication(monkeypatch):
    comp = components(source_extra=dict(license_status="approved", trusted=True, verified=True))
    out = run(comp)
    row = out["source_classification"][0]
    assert row["license_status"] == "approved"
    assert row["authentication_status"] == "not_authenticated"
    assert out["source_authentication_status"] == "not_authenticated"
    assert "source_authentication_not_verified" in out["source_gaps"]
    # A self-declared source licence cannot substitute for a registered fixture.
    real = components(fixture=False,
        source_extra=dict(license_status="approved", trusted=True, verified=True))
    calls = []
    block_model(monkeypatch, calls)
    blocked = run(real)
    assert blocked["status"] == "not_evaluated_real_inputs"
    assert blocked["model_inputs"] is None
    assert calls == []


@pytest.mark.parametrize("field,value", [
    ("route_id", "other-route"), ("altitude_value", 120),
    ("source_bundle_fingerprint", "0"*64), ("real_parent_fingerprint", "0"*64),
    ("operating_layer_id", "OTHER-LAYER"), ("path_sha256", "0"*64),
    ("scenario_fingerprint", "0"*64), ("domain_fingerprint", "0"*64),
    ("computational_dependency_fingerprint", "0"*64)])
def test_consumer_input_applicability_is_bound(field, value, monkeypatch):
    comp = components()
    comp["registry"]["consumers"][0]["input_applicability"][field] = value
    calls = []
    block_model(monkeypatch, calls)
    out = run(comp)
    assert out["status"] == "blocked"
    assert "consumer_input_inapplicable:m4_joint:"+field in out["blockers"]
    assert calls == []


def test_consumer_method_identity_is_an_allowlist(monkeypatch):
    comp = components()
    comp["registry"]["consumers"][0]["method_version"] = "v0"
    calls = []
    block_model(monkeypatch, calls)
    out = run(comp)
    assert "consumer_method_version_mismatch:m4_joint" in out["blockers"]
    comp = components()
    comp["registry"]["consumers"][0]["method_ref"] = "arbitrary.module.Class"
    out = run(comp)
    assert "consumer_method_unsupported:m4_joint" in out["blockers"]
    assert calls == []


def test_wrong_route_parent_chain_blocks():
    comp = components()
    comp["frozen"]["route_evidence"]["validation"]["validation_id"] = "old-validation"
    out = run(comp)
    assert out["status"] == "blocked"
    assert "route_chain_mismatch:validation:validation_id" in out["blockers"]
    assert out["native_model_results"] == {}


def test_corrupted_scenario_fingerprint_is_rejected():
    comp = components()
    comp["frozen"]["scenario"]["scenario_fingerprint"] = "0"*64
    with pytest.raises(ValueError):
        run(comp)


def test_incomplete_x_is_blocked_and_model_not_called(monkeypatch):
    comp = components()
    comp["actions"] = comp["actions"][:1]
    calls = []
    block_model(monkeypatch, calls)
    out = run(comp)
    assert out["status"] == "blocked"
    assert "frozen_action_set_incomplete:rtk" in out["blockers"]
    assert calls == []


def test_action_outside_frozen_x_is_blocked_and_retained(monkeypatch):
    comp = components()
    extra = dict(comp["actions"][0], action_id="extra", device_instance_id="new-extra")
    comp["actions"] = comp["actions"] + [extra]
    calls = []
    block_model(monkeypatch, calls)
    out = run(comp)
    assert out["status"] == "blocked"
    assert "action_outside_frozen_x:extra" in out["blockers"]
    assert "extra" in {r["action_id"] for r in out["action_results"]}
    assert calls == []


def test_radar_new_action_is_rejected_not_dropped(monkeypatch):
    radar = dict(action_id="radar", service_key="S:radar_noncooperative",
        planner_family="directional_radar", physical_site_id="tower:T1",
        device_id="RADAR-fixture", device_instance_id="new-radar", slot_id="slot-radar",
        dependency_action_ids=[], applicable_models=["m4_joint"], source_class="C",
        assumption_refs=[])
    comp = components(actions=research_actions(m4_fixture()[0]) + [radar])
    calls = []
    block_model(monkeypatch, calls)
    out = run(comp)
    row = {r["action_id"]: r for r in out["action_results"]}["radar"]
    assert row["status"] == "unsupported"
    assert "unsupported_action_family:S:radar_noncooperative" in row["reason_codes"]
    assert out["radar_diagnostic_status"] == "rejected_new_action_not_supported"
    assert "radar_new_action_not_supported" in out["unsupported_dimensions"]
    assert len(out["action_results"]) == 3
    assert calls == []


def test_rid_figure_only_footprint_is_not_a_service_consumer(monkeypatch):
    rid = dict(action_id="rid", service_key="S:rid_cooperative",
        planner_family="omnidirectional_site", physical_site_id="tower:T1",
        device_id="RID-fixture", device_instance_id="new-rid", slot_id="slot-rid",
        dependency_action_ids=[], applicable_models=["m4_joint"], source_class="C",
        assumption_refs=[])
    comp = components(actions=[rid])
    calls = []
    block_model(monkeypatch, calls)
    out = run(comp)
    row = out["action_results"][0]
    assert "rid_figure_only_not_a_service_consumer" in row["reason_codes"]
    assert "rid_figure_only_service_consumer" in out["unsupported_dimensions"]
    assert calls == []


def test_endpoint_new_action_is_rejected(monkeypatch):
    endpoint = dict(action_id="endpoint", service_key="N:navigation_integrity_monitoring",
        planner_family="endpoint_integrity_monitor", physical_site_id="tower:T1",
        device_id="ENDPOINT-fixture", device_instance_id="new-endpoint",
        slot_id="slot-endpoint", dependency_action_ids=[], applicable_models=["m4_joint"],
        source_class="C", assumption_refs=[])
    comp = components(actions=[endpoint])
    calls = []
    block_model(monkeypatch, calls)
    out = run(comp)
    assert "endpoint_new_action_unsupported" in out["action_results"][0]["reason_codes"]
    assert "endpoint_new_action_not_supported" in out["unsupported_dimensions"]
    assert calls == []


def test_same_site_same_model_multi_instance_is_unsupported(monkeypatch):
    comp = components()
    second = dict(comp["actions"][0], action_id="comm-2", device_instance_id="new-c2",
        slot_id="slot-c2")
    comp["actions"] = comp["actions"] + [second]
    calls = []
    block_model(monkeypatch, calls)
    out = run(comp)
    assert "unsupported_multiplicity" in out["blockers"]
    assert {"comm", "comm-2"} <= {r["action_id"] for r in out["action_results"]}
    assert calls == []


def test_real_rtk_selection_is_blocked(monkeypatch):
    actions = research_actions(m4_fixture()[0])
    actions[1]["equipment_selection_status"] = "not_selected"
    comp = components(actions=actions)
    calls = []
    block_model(monkeypatch, calls)
    out = run(comp)
    assert "rtk_real_selection_unavailable" in out["blockers"]
    assert calls == []


def test_rtk_and_c_local_satisfaction_keeps_navigation_unknown():
    out = run(components())
    native = out["native_model_results"]["m4_joint"]
    post = native["native_results"]["post_plan"]["p14"]
    entries = [service for route in post["routes"] for voxel in route["voxels"]
        for subsystem in voxel["subsystems"] if subsystem.get("subsystem") == "N"
        for service in (subsystem.get("service_redundancy") or subsystem.get("services") or [])
        if service.get("service_key") == "N:rtk_augmentation"]
    assert entries and entries[0]["geometry_status"] == "satisfied"
    assert entries[0]["delivery_status"] == "satisfied"
    assert out["navigation_status"] == "unknown"
    for gap in ("provider_inventory", "handover", "speed_profile", "backup_integrity"):
        assert gap in out["navigation_evidence_gaps"]
    assert "device_status" not in out["navigation_evidence_gaps"]
    for dimension in ("per_segment_provider_inventory", "per_segment_handover",
            "per_segment_speed_profile", "per_segment_backup_integrity"):
        assert dimension in out["unsupported_dimensions"]
    assert any(e.get("effect") == "unsupported_dimensions"
        for e in out["conditional_effects"])
    assert any(e.get("effect") == "navigation_unknown" for e in out["conditional_effects"])


def test_native_m2_positive_with_unknown_inventory_cannot_promote():
    native_m2 = dict(navigation_status="satisfied", provider_inventory_status="unknown",
        source_ref="native-m2-fixture")
    out = run(components(native_m2=native_m2))
    assert out["native_m2_result"] == native_m2
    assert out["native_m2_result_fingerprint"] == digest(native_m2)
    assert out["navigation_status"] == "unknown"
    assert any(e.get("effect") == "native_m2_positive_not_promoted"
        for e in out["conditional_effects"])


def test_1000m_remains_unconsumed_despite_registry_claims():
    comp = components()
    comp["registry"]["consumers"][0]["supported_constraint_ids"] = ["C:communication:max_gap_m"]
    out = run(comp)
    assert any(row.get("max_gap_m") == 1000 for row in out["unconsumed_requirements"])
    assert "communication_1000m_unconsumed" in out["evidence_gaps"]
    assert out["consumer_mapping"]["C:communication:max_gap_m"]["consumer_id"] == "m4_joint"


def test_m3_stays_not_verified_and_complete_scope_unknown():
    out = run(components())
    assert out["native_model_results"]["m3_binding"]["formal_ue_status"] == "unresolved"
    assert out["native_model_results"]["m3_binding"]["independent_verification_status"] == "not_verified"
    assert out["m3_status"]["independent_verification_status"] == "not_verified"
    assert out["complete_required_cns_status"] == "unknown"
    assert out["evidence_status"] == "fixture_only"


def test_inputs_are_immutable_and_no_file_is_written(monkeypatch):
    comp = components()
    before = deepcopy((comp["frozen"], comp["actions"], comp["registry"]))
    original_open, original_io_open = builtins.open, io.open

    def deny(file, mode="r", *args, **kwargs):
        if any(flag in mode for flag in "wax+"):
            raise AssertionError("adapter attempted a file write")
        return original_open(file, mode, *args, **kwargs)

    def deny_io(file, mode="r", *args, **kwargs):
        if any(flag in mode for flag in "wax+"):
            raise AssertionError("adapter attempted a pathlib write")
        return original_io_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", deny)
    monkeypatch.setattr(io, "open", deny_io)
    out = run(comp)
    assert out["status"] == "evaluated_fixture_only"
    assert (comp["frozen"], comp["actions"], comp["registry"]) == before


def test_native_and_conditional_results_are_isolated():
    out = run(components())
    native = out["native_model_results"]["m4_joint"]
    assert "conditional_effects" not in native
    assert "research_projection" not in native
    assert native["complete_required_cns_status"] == "unknown"
    assert out["native_model_results"]["m3_binding"]["complete_required_cns_status"] == "unacceptable"
    assert out["complete_required_cns_status"] == "unknown"
    assert native["persisted_to_project_state"] is False


def test_two_routes_share_sources_but_computational_identity_differs():
    first = run(components(route_id="R1", altitude=100, fixture=False))
    second = run(components(route_id="R2", altitude=120, fixture=False))
    assert first["source_bundle_fingerprint"] == second["source_bundle_fingerprint"]
    assert first["computational_dependency_fingerprint"] != second["computational_dependency_fingerprint"]
    assert first["native_model_results"] == second["native_model_results"] == {}


@pytest.mark.parametrize("value", [float("nan"), object(), lambda: None])
def test_malformed_or_executable_inputs_are_rejected(value):
    comp = components()
    comp["frozen"]["scenario"]["route_scenario"]["extra"] = value
    with pytest.raises(ValueError):
        run(comp)


def test_fixture_manifest_fingerprint_tamper_is_rejected():
    comp = components()
    comp["frozen"]["fixture"]["manifest"]["route_id"] = "tampered"
    with pytest.raises(ValueError, match="fixture manifest"):
        run(comp)


def test_fixture_material_identity_tamper_is_rejected():
    comp = components()
    comp["registry"]["consumers"][0]["fixture_material"]["frozen_context"]["context_fingerprint"] = "0"*64
    with pytest.raises(ValueError):
        run(comp)


def test_registry_cannot_declare_unsupported_capability(monkeypatch):
    comp = components()
    comp["registry"]["consumers"][0]["supported_action_families"].append("S:radar_noncooperative")
    calls = []
    block_model(monkeypatch, calls)
    out = run(comp)
    assert "consumer_claims_unsupported_capability:S:radar_noncooperative" in out["blockers"]
    assert calls == []
