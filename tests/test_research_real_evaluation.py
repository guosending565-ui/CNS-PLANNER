# -*- coding: utf-8 -*-
"""P2-A2：真实 R0005 条件域 envelope、预检与评价适配器受控接入。

本文件只读取 P2-A1 已冻结的静态产物（``scenario.json`` /
``admission_report.json`` 等），不读取或写入真实 ProjectState，不运行真实
R0005 优化，不调用 P14/P15/P16/P17 生产模型，不写 artifact。

P2-A1 产物目录可用 ``CNS_P2A1_ARTIFACT_DIR`` 覆盖；目录缺失时显式 skip，而不是
用合成数据冒充真实输入。
"""
from __future__ import annotations

from copy import deepcopy
import builtins
import json
import os
from pathlib import Path

import pytest

from cns_planner.algorithms.existing_site_lexicographic import v2
from cns_planner.application import research_evaluation_adapter as adapter
from cns_planner.application import research_solver_envelope as envelope_module
from cns_planner.application.research_scenario_admission import inspect_research_admission
from cns_planner.application.research_scenario_builder import build_conditional_domain
from cns_planner.domain.joint_feasibility import digest
from cns_planner.domain.research_scenario import checked_scenario, validate_research_scenario

REPO = Path(__file__).resolve().parents[1]
P2A1 = Path(os.environ.get(
    "CNS_P2A1_ARTIFACT_DIR",
    r"D:\Projects\_cns_round38_global_recovery\p2a1_r0005_conditional_scenario_20261018"))
SCENARIO_FILE = P2A1 / "scenario.json"
ADMISSION_FILE = P2A1 / "admission_report.json"
NORMALIZED_FILE = P2A1 / "normalized_actions.json"

EXPECTED_SOURCE_SHA = "25724dae269056e438181f96035ade5918e1b8bb7125eecb3e11bf6cef81ad9c"
EXPECTED_SCENARIO_FP = "a8b6c6c2db3d626b039187dde533096b1dd179158428e834d78c7b31721b46f5"
EXPECTED_DOMAIN_FP = "c45894bf3d7b490fa91cfedf44acdf052fb4345ff900e7c0970d7365681fef13"
EXPECTED_ACTION_SET_FP = "392e80452a79e025e9f6ce2e0552072e3284e2f098f5954f9282a2d553869039"
EXPECTED_ADMISSION_FP = "228b5dff1b7b15efc7b5b3e5c36fa56f79a5ffccdbd396ea7ab6f903815e11c8"


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _registry(scenario, domain):
    """Trusted real-domain capability claim; never derived from JSON self-claims."""
    route = scenario["route_scenario"]
    return dict(
        schema_version=adapter.REGISTRY_SCHEMA,
        consumers=[dict(
            consumer_id=adapter.M4_CONSUMER,
            method_ref=adapter.M4_METHOD_REF,
            method_version=adapter.M4_METHOD_VERSION,
            supported_action_families=["C:communication"],
            input_applicability=dict(
                scenario_fingerprint=scenario["scenario_fingerprint"],
                domain_fingerprint=domain["domain_fingerprint"],
                source_bundle_fingerprint=scenario["source_bundle_fingerprint"],
                computational_dependency_fingerprint=domain[
                    "computational_dependency_fingerprint"],
                real_parent_fingerprint=digest(domain["real_qualification_snapshot"]),
                route_id=route["route_id"],
                path_sha256=route["path_sha256"],
                altitude_value=route["altitude_value"],
                vertical_reference=route["vertical_reference"],
                operating_layer_id=route["operating_layer_id"],
            ),
            assumption_approval_basis={},
            navigation_evidence=dict(
                provider_inventory_status="unknown",
                device_status="unknown",
                handover_status="unknown",
                speed_profile_status="unknown",
                backup_integrity_status="unknown",
                source_ref="scenario.route_scenario.snapshot_declarations"),
            dependent_consumers=["m2_rtk", "m3_binding"],
        )],
    )


def _frozen_inputs(scenario, domain):
    return dict(
        schema_version=adapter.INPUTS_SCHEMA,
        scenario=scenario,
        conditional_domain=domain,
        route_evidence=deepcopy(scenario["route_scenario"]["route_evidence"]),
        fixture=None,
        native_m2_result=None,
    )


@pytest.fixture(scope="module")
def real():
    if not (SCENARIO_FILE.exists() and ADMISSION_FILE.exists() and NORMALIZED_FILE.exists()):
        pytest.skip("P2-A1 frozen R0005 static artifacts unavailable")
    scenario = checked_scenario(_load(SCENARIO_FILE))
    route = scenario["route_scenario"]
    domain = build_conditional_domain(
        parent_domain_review=route["parent_real_domain"],
        scenario=scenario,
        assumption_registry=route["assumption_registry"])
    report = _load(ADMISSION_FILE)
    admission = inspect_research_admission(
        scenario=scenario, conditional_domain=domain,
        route_evidence=route["route_evidence"],
        consumer_capabilities=report["consumer_capabilities"],
        run_intent=dict(mode="inspect"))
    envelope = envelope_module.build_real_solver_envelope(
        scenario=scenario, conditional_domain=domain,
        expected_source_sha256=EXPECTED_SOURCE_SHA)
    preflight = v2.preflight_real_domain(real_domain_envelope=envelope)
    output = adapter.evaluate_real_conditional_actions(
        frozen_inputs=_frozen_inputs(scenario, domain),
        actions=domain["finite_actions"],
        consumer_registry=_registry(scenario, domain))
    return dict(scenario=scenario, domain=domain, admission=admission,
                report=report, normalized=_load(NORMALIZED_FILE),
                envelope=envelope, preflight=preflight, evaluation=output)


def test_frozen_p2a1_artifacts_reproduce_exactly(real):
    """P2-A1 冻结产物逐项复算一致，P2-A2 不重写历史身份。"""
    assert real["scenario"]["scenario_fingerprint"] == EXPECTED_SCENARIO_FP
    assert real["domain"]["domain_fingerprint"] == EXPECTED_DOMAIN_FP
    assert real["domain"]["action_set_fingerprint"] == EXPECTED_ACTION_SET_FP
    assert real["scenario"]["route_scenario"]["baseline_ref"]["sha256"] == EXPECTED_SOURCE_SHA
    assert real["admission"]["admission_fingerprint"] == EXPECTED_ADMISSION_FP
    assert real["report"]["admission_fingerprint"] == EXPECTED_ADMISSION_FP
    assert real["admission"]["execution_blockers"] == real["report"]["execution_blockers"]
    assert real["admission"]["evidence_gaps"] == real["report"]["evidence_gaps"]
    assert real["admission"]["static_contract_passed"] is True
    assert real["admission"]["final_eligible"] is False
    assert len(real["admission"]["execution_blockers"]) == 16


def test_envelope_keeps_all_42_actions_and_route_ids(real):
    actions = real["envelope"]["actions"]
    assert len(actions) == 42
    assert len({row["action_id"] for row in actions}) == 42
    assert [row["action_id"] for row in actions] == sorted(
        row["action_id"] for row in actions)
    assert all(row["route_ids"] == ["R0005"] for row in actions)
    assert {row["route_relation_source"] for row in actions} == {
        "explicit_action_route_id", "derived_from_verified_plan_scope"}
    assert all(EXPECTED_SOURCE_SHA in row["route_relation_evidence_ref"]
               for row in actions)
    assert len({row["device_instance_id"] for row in actions}) == 42


def test_action_sites_and_375_parent_sites_stay_distinct(real):
    envelope = real["envelope"]
    action_sites = {row["physical_site_id"] for row in envelope["actions"]}
    parent_sites = {row["physical_site_id"] for row in envelope["real_parent_domain"]["site_ledger"]}
    assert len(action_sites) == 22
    assert len(parent_sites) == 375
    assert action_sites < parent_sites
    assert len(envelope["conditional_sites"]) == 22
    preflight = real["preflight"]
    assert preflight["action_count"] == 42
    assert preflight["conditional_site_count"] == 22
    assert preflight["parent_site_count"] == 375
    assert preflight["action_site_count"] == 22


def test_action_identity_instance_and_site_are_preserved(real):
    normalized = {row["action_id"]: row for row in real["normalized"]["actions"]}
    assert len(normalized) == 42
    for row in real["envelope"]["actions"]:
        source = normalized[row["action_id"]]
        for field in ("action_id", "physical_site_id", "device_id",
                      "device_instance_id", "service_key", "route_ids"):
            assert row[field] == source[field]


def test_no_truncation_and_no_selected_substitution(real):
    envelope = real["envelope"]
    assert len(envelope["actions"]) == 42
    selected = real["normalized"]["selected_action_refs"]
    assert len(selected) == 6
    assert set(selected) < {row["action_id"] for row in envelope["actions"]}
    assert real["normalized"]["stats"]["no_implicit_action_truncation"] is True
    assert envelope["enumeration"]["action_count"] == 42
    assert envelope["enumeration"]["subset_count"] == 2 ** 42
    assert envelope["conditional_domain"]["completeness_axes"]["action_enumeration"] == \
        "complete_from_frozen_candidate_snapshot"
    assert envelope["final_eligible"] is False


def test_envelope_is_not_a_projectstate_copy(real):
    envelope = real["envelope"]
    assert envelope["source_identity"]["source_state_bytes"] == 281900343
    assert "raw_project_state" not in envelope
    assert "candidate_actions" not in envelope
    assert len(json.dumps(envelope, ensure_ascii=False).encode("utf-8")) < 2 * 1024 * 1024


def test_unknown_activation_and_inventory_are_not_zero(real):
    envelope = real["envelope"]
    policy = envelope["activation_policy"]
    l1 = policy["l1_newly_activated_site_count"]
    l2 = policy["l2_new_device_instance_count"]
    assert policy["status"] == "unknown"
    assert l1["status"] == "unknown" and l1["value"] is None
    assert l2["status"] == "unknown" and l2["value"] is None
    assert l1["value"] != 0 and l2["value"] != 0
    assert envelope["installed_inventory_status"] == "unknown"
    assert envelope["installed_device_instances"] == []
    assert envelope["installed_device_instances_status"] == "unknown_not_enumerated"
    assert all(row["conditional_activation_baseline"] == "unknown"
               for row in envelope["conditional_sites"])
    preflight = real["preflight"]
    assert "real_activation_baseline_unknown" in preflight["blockers"]
    assert "real_installed_inventory_unknown" in preflight["blockers"]


def test_unknown_qualification_never_becomes_qualified(real):
    qualification = real["domain"]["conditional_qualification"]
    assert len(qualification) == 22
    assert all(row["status"] == "unknown" for row in qualification)
    assert all(row["conditional_qualification"] == "unknown"
               for row in real["envelope"]["conditional_sites"])
    assert not any(row["conditional_qualification"] == "qualified_under_policy"
                   for row in real["envelope"]["conditional_sites"])
    assert "device_qualification_unknown" in real["envelope"]["execution_blockers"]


def test_unknown_dependency_is_not_claimed_absent(real):
    envelope = real["envelope"]
    assert all(row["dependency_action_ids"] == [] for row in envelope["actions"])
    assert all(row["dependency_evidence_status"] == "unknown_not_declared_in_source"
               for row in envelope["actions"])
    assert envelope["dependency_evidence_status"] == "unknown"
    assert envelope["dependency_closure_proven"] is False
    assert "unknown" in envelope["pruning_evidence_status"]
    assert envelope["pruning_rules"]["incompatible_action_pairs"] == []
    assert "action_dependency_evidence_unknown" in envelope["execution_blockers"]


def test_unsupported_consumers_block_real_evaluation(real):
    output = real["evaluation"]
    assert output["status"] == "evaluation_blocked"
    assert output["evaluation_status"] == "not_evaluated"
    assert output["model_call_status"] == "not_invoked_real_domain_binding_unavailable"
    assert output["native_model_results"] == {}
    assert output["applied_action_ids"] == []
    assert len(output["action_results"]) == 42
    rid = [row for row in output["action_results"]
           if row["family"] == "S:rid_cooperative"]
    endpoints = [row for row in output["action_results"]
                 if row["family"] == "N:navigation_integrity_monitoring"]
    assert len(rid) == 20 and len(endpoints) == 2
    assert all(row["status"] == "unsupported" for row in rid + endpoints)
    assert all("rid_figure_only_not_a_service_consumer" in row["reason_codes"]
               for row in rid)
    assert all("endpoint_new_action_unsupported" in row["reason_codes"]
               for row in endpoints)
    assert "real_evaluator_binding_unavailable" in output["blockers"]
    assert output["branch_identification"]["evaluated_real"]["status"] == "false"
    assert output["branch_identification"]["unsupported_service_present"]["status"] == "true"


def test_unconsumed_required_cannot_enter_complete_proof(real):
    output = real["evaluation"]
    assert output["complete_required_cns_status"] == "unknown"
    assert output["complete_required_proof_status"] == "not_proven"
    assert output["final_eligible"] is False
    assert output["optimal_proven"] is False
    assert output["model_feasible"] is None
    partition = output["required_partition"]
    assert (partition["inventory_count"], partition["included_count"],
            partition["excluded_count"], partition["unconsumed_count"]) == (15, 11, 4, 3)
    assert partition["partition_complete"] is True
    assert "REQ-C-GAP-1000" in [row["constraint_id"]
                                for row in partition["excluded_constraints"]]
    assert "REQ-C-GAP-1000" in partition["unconsumed_constraint_ids"]
    assert "REQ-C-CONT-45" in partition["included_constraint_ids"]
    assert partition["c1000m"]["independent_of_45m"] is True
    assert "REQ-N-ACCURACY-10M" in partition["unconsumed_constraint_ids"]
    assert "REQ-ENDPOINT-DELIVERY-C" in partition["unconsumed_constraint_ids"]


def test_no_authorization_means_no_search(real):
    envelope = real["envelope"]
    preflight = real["preflight"]
    assert envelope["execution_enabled"] is False
    assert envelope["execution_authorization"] == "not_authorized"
    assert preflight["execution_status"] == "execution_not_authorized"
    assert preflight["search_started"] is False
    policy = dict(algorithm_version=v2.VERSION, order_policy="action_id",
                  max_visited_nodes=0, max_joint_replays=0,
                  retain_feasible_limit=0, max_storage_bytes=0,
                  max_elapsed_seconds=1, max_memory_mb=1)
    events = list(v2.iter_conditional_lexicographic(
        frozen_conditional_domain=envelope, optimization_context={},
        search_policy=policy, runtime=None))
    assert len(events) == 1
    result = events[0]["result"]
    assert result["status"] == "blocked"
    assert result["termination_reason"] == \
        "non_fixture_conditional_execution_not_authorized"
    assert result["execution_enabled"] is False
    assert result["optimal_proven"] is False


def test_no_approved_budget_means_no_real_solve(real):
    envelope = real["envelope"]
    assert envelope["search_budget_status"] == "not_approved"
    assert envelope["search_budget"]["status"] == "not_approved"
    assert envelope["search_budget"]["policy"] is None
    assert envelope["search_budget"]["approved_by"] is None
    assert "search_budget_not_approved" in real["preflight"]["blockers"]


def test_changed_source_sha_rejects_old_envelope(real):
    envelope = real["envelope"]
    tampered = deepcopy(envelope)
    tampered["source_identity"]["source_state_sha256"] = "f" * 64
    with pytest.raises(ValueError):
        envelope_module.verify_real_solver_envelope(tampered)
    changed = deepcopy(real["scenario"])
    changed["route_scenario"]["baseline_ref"]["sha256"] = "f" * 64
    changed = validate_research_scenario(
        regional_bundle=changed["regional_bundle"],
        route_scenario=changed["route_scenario"],
        policies=changed["policies"])
    assert changed["scenario_fingerprint"] != real["scenario"]["scenario_fingerprint"]
    with pytest.raises(ValueError):
        envelope_module.verify_real_solver_envelope(envelope, scenario=changed)
    with pytest.raises(ValueError):
        envelope_module.build_real_solver_envelope(
            scenario=real["scenario"], conditional_domain=real["domain"],
            expected_source_sha256="f" * 64)


def test_synthetic_fixture_behavior_is_unchanged():
    import test_research_evaluation_adapter as fixture_adapter

    comp = fixture_adapter.components()
    output = fixture_adapter.run(comp)
    assert output["status"] == "evaluated_fixture_only"
    assert output["fixture_only"] is True
    assert output["execution_enabled"] is False
    assert output["final_eligible"] is False
    assert output["model_call_status"] == "invoked_fixture_only"


def test_real_branch_does_not_write_state_or_artifact(monkeypatch, real):
    original_open = builtins.open

    def guarded_open(file, mode="r", *args, **kwargs):
        if any(flag in mode for flag in ("w", "a", "x", "+")):
            raise AssertionError("real domain branch must not open a file for writing")
        return original_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    envelope = envelope_module.build_real_solver_envelope(
        scenario=real["scenario"], conditional_domain=real["domain"],
        expected_source_sha256=EXPECTED_SOURCE_SHA)
    envelope_module.verify_real_solver_envelope(envelope)
    preflight = v2.preflight_real_domain(real_domain_envelope=envelope)
    output = adapter.evaluate_real_conditional_actions(
        frozen_inputs=_frozen_inputs(real["scenario"], real["domain"]),
        actions=real["domain"]["finite_actions"],
        consumer_registry=_registry(real["scenario"], real["domain"]))
    assert envelope["envelope_fingerprint"] == real["envelope"]["envelope_fingerprint"]
    assert preflight["status"] == "real_domain_contract_ready"
    assert output["status"] == "evaluation_blocked"
    assert output["persisted_to_project_state"] is False
    assert output["new_site_stage_unlocked"] is False
