# -*- coding: utf-8 -*-
"""P2-A1：rev503 真实 R0005 条件研究场景与静态准入合同。

本文件只读真实 ``projects/round38_cns_experiment_b/current_project.json``
的顶层键切片，不整文件 json.load、不写回 State、不运行 solver/P14/P15/P16/P17。
若工作树没有该真实状态文件，测试显式 skip，而不是用合成数据冒充真实输入。

P2-A3-A：端点站址由规范化动作的 canonical ``node:`` 站址重建（不再读取规范化
动作不存在的 ``takeoff_landing_site_id``），父域因此是 373 塔 + 2 端点 = 375。
设置 ``CNS_RESEARCH_STRICT_ACCEPTANCE=1`` 时，缺真实输入或缺冻结产物即失败而不是
skip；默认行为保持既有兼容语义。
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path

import pytest

from cns_planner.application.research_scenario_builder import (
    build_conditional_domain,
    build_real_parent_domain,
    derive_plan_route_scope,
    normalize_conditional_actions,
)
from cns_planner.application.research_scenario_admission import inspect_research_admission
from cns_planner.domain.joint_feasibility import digest
from cns_planner.domain.research_scenario import checked_scenario, validate_research_scenario

REPO = Path(__file__).resolve().parents[1]
STATE = REPO / "projects/round38_cns_experiment_b/current_project.json"
STATE_SHA = "25724dae269056e438181f96035ade5918e1b8bb7125eecb3e11bf6cef81ad9c"
TOWER_SHA = "d722c111596acb4766bcc9d4cb5ce326f3a4b1f8364a5db871cb60220e2a411b"
REQUIRED_SHA = "504bf87e5966da3753a2d44545dc08a29d21c04e42d07226885e86ef2a370aa4"
SCENARIO_ID = "r0005-conditional-rev503-v1"
# R0005 的两个端点在冻结 Scenario 中就是 N005/N006；父域端点身份必须由
# 规范化动作的 canonical ``node:`` 站址重建，而不是假定源动作里有额外字段。
ENDPOINT_REFS = ("N005", "N006")
# P2-A1 冻结产物身份（scenario_identity.json / artifact_manifest.json）。
P2A1 = Path(os.environ.get(
    "CNS_P2A1_ARTIFACT_DIR",
    r"D:\Projects\_cns_round38_global_recovery\p2a1_r0005_conditional_scenario_20261018"))
STRICT_ACCEPTANCE = os.environ.get("CNS_RESEARCH_STRICT_ACCEPTANCE") == "1"
FROZEN_SCENARIO_SHA = "a8b6c6c2db3d626b039187dde533096b1dd179158428e834d78c7b31721b46f5"
FROZEN_PARENT_SHA = "58a2e209235678541f2626cb7682e85e7e3412c552f4eb7d2bb7242d01c0ef55"
MODELS = dict(p14="1.4", p15="1.3", p16="2.3", p17="2.2")
M4_VERSION = "p14:1.4;p15:1.3;p17:2.2;m2:rtk_joint_acceptance.v1"

_REAL = None


def _extract_top_level(text: str, key: str, predicate=None):
    """Locate a top-level key by value shape, tolerating nested same-name keys."""
    decoder = json.JSONDecoder()
    marker = '"%s"' % key
    index = text.find(marker)
    while index != -1:
        cursor = index - 1
        while cursor >= 0 and text[cursor] in " \t\r\n":
            cursor -= 1
        if cursor >= 0 and text[cursor] in "{,":
            start = index + len(marker)
            while start < len(text) and text[start] in " \t\r\n":
                start += 1
            if start < len(text) and text[start] == ":":
                start += 1
                while start < len(text) and text[start] in " \t\r\n":
                    start += 1
                try:
                    value, _ = decoder.raw_decode(text, start)
                except json.JSONDecodeError:
                    value = None
                if predicate is None or predicate(value):
                    return value
        index = text.find(marker, index + 1)
    raise KeyError(key)


def _real_state():
    global _REAL
    if _REAL is not None:
        return _REAL
    if not STATE.exists():
        if STRICT_ACCEPTANCE:
            pytest.fail("strict acceptance requires the real rev503 ProjectState: " + str(STATE))
        pytest.skip("real rev503 ProjectState unavailable in this worktree")
    raw = STATE.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != STATE_SHA:
        pytest.fail("rev503 ProjectState SHA changed: " + actual)
    text = raw.decode("utf-8")
    _REAL = dict(
        revision=_extract_top_level(text, "revision", lambda v: type(v) is int),
        plan=_extract_top_level(text, "cns_corridor_site_plan",
                                lambda v: type(v) is dict and "candidate_actions" in v),
        routes=_extract_top_level(text, "operational_routes", lambda v: type(v) is list),
        validations=_extract_top_level(text, "layered_route_validations",
                                       lambda v: type(v) is dict and "items" in v),
        adoptions=_extract_top_level(text, "layered_operational_adoptions",
                                     lambda v: type(v) is dict and "items" in v),
        towers=_extract_top_level(text, "towers",
                                  lambda v: type(v) is dict and "items" in v and "count" in v),
        p14=_extract_top_level(text, "cns_corridor_assessment",
                               lambda v: type(v) is dict and "algorithm_id" in v),
        p15=_extract_top_level(text, "cns_corridor_gap_assessment",
                               lambda v: type(v) is dict and "algorithm_id" in v),
        p17=_extract_top_level(text, "continuous_service_acceptability",
                               lambda v: type(v) is dict and "service_acceptability_limits" in v),
    )
    return _REAL


def _required_inventory():
    """P1-A 15 行 Required 矩阵的最小真实字段快照（不含外部文件依赖）。"""
    rows = [
        ("REQ-C-GAP-1000", "C:communication", True, 1000, None, "excluded"),
        ("REQ-C-CONT-45", "C:communication", True, 45, ("p17_continuous", "2.2"), "consumed"),
        ("REQ-C-RADIUS-4KM", "C:communication", True, None, ("p14_corridor", "1.4"), "consumed"),
        ("REQ-C-COVERAGE-095", "C:communication", True, None, ("p14_corridor", "1.4"), "consumed"),
        ("REQ-C-REDUNDANCY", "C:communication", True, None, ("p14_corridor", "1.4"), "consumed"),
        ("REQ-N-INTEGRITY-ENDPOINT", "N:navigation_integrity_monitoring", True, None,
         ("p16_site_plan", "2.3"), "consumed"),
        ("REQ-N-ACCURACY-10M", "N:navigation_integrity_monitoring", True, None, None, "unknown"),
        ("REQ-N-RTK-AUG", "N:rtk_augmentation", False, None, None, "excluded"),
        ("REQ-RID-COOP", "S:rid_cooperative", True, None, ("m4_joint", M4_VERSION), "consumed"),
        ("REQ-RADAR-NONCOOP", "S:radar_noncooperative", False, None, None, "unsupported"),
        ("REQ-ENDPOINT-DELIVERY-C", "C:communication", True, None, None, "unknown"),
        ("REQ-P17-GATE", "P17:continuous_service", True, None, ("p17_continuous", "2.2"), "consumed"),
        ("REQ-CORRIDOR-1000M", "C:communication", True, None, ("p14_corridor", "1.4"), "consumed"),
        ("REQ-OBJ-SATISFIED", "C:communication/S:rid_cooperative", True, None,
         ("m4_joint", M4_VERSION), "consumed"),
        ("REQ-L3-COST", "L3:cost", False, None, None, "unknown"),
    ]
    return [dict(constraint_id=cid, service_key=service, is_required=required,
                 max_gap_m=gap,
                 consumer=(dict(consumer_id=consumer[0], version=consumer[1]) if consumer else None),
                 source_doc="P1-A/04_required_consumption_matrix.csv",
                 source_version="p1a-v1", status=status)
            for cid, service, required, gap, consumer, status in rows]


def _scope():
    inventory = _required_inventory()
    excluded = []
    for row in inventory:
        if row["constraint_id"] in ("REQ-C-GAP-1000", "REQ-N-RTK-AUG",
                                    "REQ-RADAR-NONCOOP", "REQ-L3-COST"):
            excluded.append(dict(constraint_id=row["constraint_id"],
                                 reason="explicit_research_scope_exclusion",
                                 approval_status="research_scope_declared_not_business_approval",
                                 scope_approval_ref="P1-A/04 + P2-A1 taskbook §5"))
    included = [row["constraint_id"] for row in inventory
                if row["constraint_id"] not in {row["constraint_id"] for row in excluded}]
    unconsumed = [row for row in inventory
                  if row["constraint_id"] in ("REQ-C-GAP-1000", "REQ-N-ACCURACY-10M",
                                              "REQ-ENDPOINT-DELIVERY-C")]
    return dict(
        required_constraint_inventory=inventory,
        included_constraint_ids=included,
        excluded_constraints=excluded,
        unconsumed_requirements=unconsumed,
        scope_approval_ref="P1-A/04 + P2-A1 taskbook §5",
        scope_approval_status="research_scope_declared_not_business_approval",
        c1000m_policy=dict(constraint_id="REQ-C-GAP-1000", excluded=True,
                           replaced_by_45m_gate=False),
        c45m_policy=dict(constraint_id="REQ-C-CONT-45", max_gap_m=45,
                         role="post_gate_only_not_pre_gap_conversion"),
    )


def _sources():
    return [
        dict(source_id="rev503_project_state",
             locator="projects/round38_cns_experiment_b/current_project.json",
             version="rev503", sha256=STATE_SHA, bytes=STATE.stat().st_size,
             horizontal_crs="OGC:CRS84", axis_order="lon_lat",
             vertical_reference="egm2008_orthometric", validity="frozen_snapshot",
             source_ref="rev503 ProjectState", license_status="internal",
             reader_version="p2a1_test_slice_v1", processing_lineage=[],
             role="computational", evidence_class="A", evidence_status="sha256_verified"),
        dict(source_id="tower_inventory", locator="towers.source.path",
             version="rev503_towers", sha256=TOWER_SHA, bytes=37154,
             horizontal_crs="EPSG:4490", axis_order="lon_lat",
             vertical_reference="unknown", validity="declared",
             source_ref="rev503.towers", license_status="internal",
             reader_version="xlsx_reference", processing_lineage=[],
             role="computational", evidence_class="A",
             evidence_status="declared_sha_crs_per_row_unconfirmed", confirmed=False),
        dict(source_id="p1a_required_matrix", locator="P1-A/04_required_consumption_matrix.csv",
             version="p1a-v1", sha256=REQUIRED_SHA, bytes=3611,
             horizontal_crs="not_applicable", axis_order="not_applicable",
             vertical_reference="not_applicable", validity="frozen",
             source_ref="P1-A Required matrix", license_status="internal",
             reader_version="csv", processing_lineage=[],
             role="computational", evidence_class="A", evidence_status="sha256_verified"),
    ]


def _policies(normalized):
    qual = []
    for site in sorted({row["physical_site_id"] for row in normalized["actions"]}):
        qual.append(dict(
            parameter_id="qual:" + site.replace(":", "-"), parameter_kind="installation_qualification",
            site_id=site, evidence_class="C", source_ref="rev503.towers+candidate_actions",
            value=None, range=None, units="eligibility", confidence="unassessed",
            evidence_tier="declared_assumption_pending", validity="pending_review",
            consumer="conditional_qualification", policy_version="p2a1-rev503-v1",
            applicability="research_static_contract_only", approval_ref=None,
            missing_reason="no_real_site_or_device_qualification_evidence"))
    assumptions = [
        dict(parameter_id="ASM-ROUTE-DERIVED", parameter_kind="route_relation",
             evidence_class="A", source_ref="rev503.plan.targets+before+after",
             value="R0005", range=None, units="route_id", confidence="source_derived",
             evidence_tier="source_derived", validity="frozen_snapshot",
             consumer="route_relation_consumer", policy_version="p2a1-rev503-v1",
             applicability="all candidate actions"),
        dict(parameter_id="ASM-DEVICE-INSTANCE-CONDITIONAL", parameter_kind="device_instance",
             evidence_class="C", source_ref="research_assigned_instance",
             value=None, range=None, units="instance_id", confidence="unassessed",
             evidence_tier="declared_assumption_pending", validity="pending_review",
             consumer="conditional_instance_consumer", policy_version="p2a1-rev503-v1",
             applicability="all candidate actions", approval_ref=None,
             missing_reason="no_installed_asset_or_slot_inventory"),
        dict(parameter_id="ASM-INVENTORY-UNKNOWN", parameter_kind="inventory",
             evidence_class="D", source_ref="rev503.device_catalog", value=None, range=None,
             units="count", confidence="unknown", evidence_tier="unknown", validity="unknown",
             consumer="inventory_consumer", policy_version="p2a1-rev503-v1",
             applicability="conditional_inventory", missing_reason="no_installed_inventory"),
        dict(parameter_id="ASM-CAPACITY-UNKNOWN", parameter_kind="capacity",
             evidence_class="D", source_ref="rev503.device_catalog", value=None, range=None,
             units="capacity", confidence="unknown", evidence_tier="unknown", validity="unknown",
             consumer="inventory_consumer", policy_version="p2a1-rev503-v1",
             applicability="conditional_inventory", missing_reason="no_capacity_evidence"),
        dict(parameter_id="ASM-COST-UNKNOWN", parameter_kind="cost",
             evidence_class="D", source_ref="candidate_actions.cost_unit", value=None, range=None,
             units="cost_unit", confidence="unknown", evidence_tier="unknown", validity="unknown",
             consumer="cost_consumer", policy_version="p2a1-rev503-v1",
             applicability="L3_cost", missing_reason="cost_unit_null"),
    ]
    entries = qual + assumptions
    qual_ids = [row["parameter_id"] for row in qual]
    dependencies = [
        dict(consumer_id="conditional_qualification", model_version="p2a1.qualification.static.v1",
             source_ids=["rev503_project_state", "tower_inventory"], parameter_ids=qual_ids),
        dict(consumer_id="route_relation_consumer", model_version="p2a1.route_relation.v1",
             source_ids=["rev503_project_state"], parameter_ids=["ASM-ROUTE-DERIVED"]),
        dict(consumer_id="conditional_instance_consumer", model_version="p2a1.instance.v1",
             source_ids=["rev503_project_state"], parameter_ids=["ASM-DEVICE-INSTANCE-CONDITIONAL"]),
        dict(consumer_id="inventory_consumer", model_version="p2a1.inventory.v1",
             source_ids=["rev503_project_state"],
             parameter_ids=["ASM-INVENTORY-UNKNOWN", "ASM-CAPACITY-UNKNOWN"]),
        dict(consumer_id="cost_consumer", model_version="p2a1.cost.v1",
             source_ids=["rev503_project_state"], parameter_ids=["ASM-COST-UNKNOWN"]),
    ]
    registry = [
        dict(consumer_id="m4_joint", method_ref="JointModelEvaluator", method_version=M4_VERSION,
             supported_action_families=["C:communication"], input_applicability="static_only"),
        dict(consumer_id="p14_corridor", method_ref="CNSServiceCorridorV1", method_version="1.4",
             supported_action_families=["C:communication"], input_applicability="geometry_only"),
        dict(consumer_id="p15_corridor_gap", method_ref="CNSCorridorGapAnalyzerV1", method_version="1.3",
             supported_action_families=["C:communication"], input_applicability="gap_only"),
        dict(consumer_id="p16_site_plan", method_ref="corridor_site_planning", method_version="2.3",
             supported_action_families=["N:navigation_integrity_monitoring"],
             input_applicability="planning_only"),
        dict(consumer_id="p17_continuous", method_ref="ContinuousServiceAcceptabilityV1",
             method_version="2.2",
             supported_action_families=["C:communication", "S:rid_cooperative"],
             input_applicability="post_gate_only"),
        dict(consumer_id="m2_rtk", method_ref="evaluate_p17_rtk_joint",
             method_version="rtk_joint_acceptance.v1",
             supported_action_families=["N:rtk_augmentation"], input_applicability="not_required"),
    ]
    policy = dict(scenario_id=SCENARIO_ID, policy_id="p2a1_assumption_registry",
                  policy_version="p2a1-rev503-v1", entries=entries)
    capabilities = dict(consumers={row["consumer_id"]: row["method_version"] for row in registry},
                        supported_action_families=["C:communication"],
                        note="static mapping only")
    return policy, entries, dependencies, registry, capabilities


def _route_evidence(state, route, path_sha):
    validation = [row for row in state["validations"]["items"]
                  if row.get("validation_id") == "LRV-24519150DE6E"][0]
    adoption = [row for row in state["adoptions"]["items"]
                if row.get("adoption_id") == "LRA-B28DAAB9310E"][0]
    chain = dict(route_id="R0005", path_sha256=path_sha, operating_layer_id="ALT-100",
                 altitude_value=100, vertical_reference="egm2008_orthometric",
                 baseline_sha256=STATE_SHA, validation_id="LRV-24519150DE6E",
                 adoption_id="LRA-B28DAAB9310E",
                 projection_fingerprint="layeredprojectionv1-7f3159bb439c837740319e4419162dcea993453333ff6f8fa64206ef4245c48c")
    return {
        "operational": dict(chain, kind="layered_risk_aware_operational_route",
            status="stale", current_applicability="stale",
            stale_reason="experiment_adaptation_simulated_status_only",
            observed_source_status=route.get("status"), observed_current_applicability="current",
            source_ref="rev503.operational_routes[R0005]", operational_authorization=False),
        "validation": dict(chain, status="stale", current_applicability="stale",
            stale_reason="engineering_demo_only_scope_no_operational_authorization",
            observed_source_status=validation.get("status"),
            observed_current_applicability=validation.get("current_applicability"),
            use_scope=validation.get("use_scope"), operational_authorization=False,
            source_ref="rev503.layered_route_validations[LRV-24519150DE6E]"),
        "adoption": dict(chain, status="stale", current_applicability="stale",
            stale_reason="published_engineering_demo_not_operational",
            observed_source_status=adoption.get("status"),
            observed_current_applicability=adoption.get("current_applicability"),
            published_to_operational_routes=False, operational_authorization=False,
            source_ref="rev503.layered_operational_adoptions[LRA-B28DAAB9310E]"),
        "operating_layer": dict(chain, status="stale", current_applicability="stale",
            stale_reason="derived_from_engineering_demo_adoption",
            observed_source_status="engineering_demo_only", observed_current_applicability="current",
            altitude_layer_id="ALT-100",
            source_ref="rev503.layered_operational_adoptions[LRA-B28DAAB9310E].after.route_operating_layer"),
    }


def _endpoint_site_ids(normalized):
    """从规范化动作的 canonical physical site 重建端点站址。

    规范化动作没有 ``takeoff_landing_site_id`` 字段；端点身份只来自
    ``physical_site_id`` 的 ``node:`` 前缀（源 ``distinct_site_id`` 的
    ``takeoff_landing_site:`` 前缀经 ``canonical_physical_site_id`` 映射）。
    端点动作无法映射到 ``node:`` 时显式失败，不得静默丢弃这 2 个站址。
    """
    endpoints = set()
    for row in normalized["actions"]:
        site = row.get("physical_site_id")
        source = row.get("source_distinct_site_id")
        if type(source) is str and source.startswith("takeoff_landing_site:"):
            expected = "node:" + source[len("takeoff_landing_site:"):]
            if site != expected:
                raise ValueError("endpoint site is not canonical: " +
                                 str(row.get("action_id")))
        if type(site) is str and site.startswith("node:"):
            endpoint = site[len("node:"):]
            if not endpoint:
                raise ValueError("endpoint identity is empty: " + str(row.get("action_id")))
            endpoints.add(endpoint)
    return sorted(endpoints)


def _frozen_scenario():
    """读取 P2-A1 冻结 Scenario，并按其 manifest 复核字节身份。

    ``CNS_RESEARCH_STRICT_ACCEPTANCE=1`` 时缺产物视为失败；默认沿用本模块既有
    语义（缺外部产物即 skip），避免把兼容性运行误报为验收通过。
    """
    path = P2A1 / "scenario.json"
    manifest = P2A1 / "artifact_manifest.json"
    if not path.exists() or not manifest.exists():
        if STRICT_ACCEPTANCE:
            pytest.fail("strict acceptance requires frozen P2-A1 artifacts at " + str(P2A1))
        pytest.skip("frozen P2-A1 scenario artifact unavailable")
    declared = json.loads(manifest.read_text(encoding="utf-8"))
    expected = declared["artifacts"]["scenario.json"]["sha256"]
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        pytest.fail("frozen scenario.json does not match its manifest SHA: " + actual)
    frozen = json.loads(path.read_text(encoding="utf-8"))
    if frozen.get("scenario_fingerprint") != FROZEN_SCENARIO_SHA:
        pytest.fail("frozen scenario_fingerprint changed: " +
                    str(frozen.get("scenario_fingerprint")))
    return frozen


def build():
    state = _real_state()
    plan = state["plan"]
    route = [row for row in state["routes"] if row.get("route_id") == "R0005"][0]
    plan_scope = derive_plan_route_scope(
        targets=plan.get("targets") or [],
        before_routes=(plan.get("before") or {}).get("routes", []),
        after_routes=(plan.get("after") or {}).get("routes", []),
        source_ref="rev503:cns_corridor_site_plan",
    )
    normalized = normalize_conditional_actions(
        candidate_actions=plan.get("candidate_actions") or [],
        plan_scope=plan_scope, scenario_id=SCENARIO_ID, state_sha256=STATE_SHA,
        state_locator=str(STATE), selected_action_ids=[
            row["action_id"] for row in plan.get("selected_actions") or []])
    endpoints = _endpoint_site_ids(normalized)
    if endpoints != sorted(ENDPOINT_REFS):
        raise ValueError("endpoint identity drift: %r != %r"
                         % (endpoints, list(ENDPOINT_REFS)))
    parent = build_real_parent_domain(towers=state["towers"]["items"], endpoints=endpoints,
                                      state_sha256=STATE_SHA, state_locator=str(STATE),
                                      source_ref="rev503:cns_corridor_site_plan")
    policy, entries, dependencies, registry, capabilities = _policies(normalized)
    scope = _scope()
    path_sha = digest(route["path"])
    route_evidence = _route_evidence(state, route, path_sha)
    declarations = {
        "operational": dict(status="stale", reason="experiment_adaptation_simulated"),
        "validation": dict(status="stale", reason="engineering_demo_only"),
        "adoption": dict(status="stale", reason="not_operational"),
        "operating_layer": dict(status="stale", reason="derived_from_demo"),
        "p14": dict(status="failed", reason="rev503_failed"),
        "p15": dict(status="failed", reason="rev503_failed"),
        "p16": dict(status="proposal_ready", reason="proposal_only"),
        "p17": dict(status="stale", reason="rev503_stale"),
    }
    route_scenario = dict(
        scenario_id=SCENARIO_ID, version="v1", evidence_track="engineering_research_conditional",
        region_bundle_ref=dict(bundle_id="zhoushan-rev503-r0005", version="v1"),
        baseline_ref=dict(revision=503, sha256=STATE_SHA, source_ref=str(STATE)),
        route_id="R0005", route_ids=["R0005"], endpoint_refs=list(ENDPOINT_REFS),
        path=route["path"], path_crs="OGC:CRS84",
        path_locator="rev503.operational_routes[R0005].path", path_kind="operational",
        path_sha256=path_sha, operating_layer_id="ALT-100", altitude_value=100,
        altitude_units="m", vertical_reference="egm2008_orthometric", phase="cruise",
        scope="engineering_demo_only",
        route_evidence_refs=dict(validation_id="LRV-24519150DE6E",
            adoption_id="LRA-B28DAAB9310E",
            projection_fingerprint="layeredprojectionv1-7f3159bb439c837740319e4419162dcea993453333ff6f8fa64206ef4245c48c"),
        policy_refs=[dict(policy_id=policy["policy_id"], policy_version=policy["policy_version"])],
        full_required_snapshot_ref=dict(sha256=digest(scope["required_constraint_inventory"]),
                                        source_ref="P1-A/04"),
        consumer_dependencies=dependencies, requested_actions=[],
        conditional_actions=normalized["actions"],
        candidate_action_count=42, selected_action_refs=normalized["selected_action_ids"],
        selected_actions_are_reference_only=True,
        action_contract_stats=normalized["stats"],
        route_scope=plan_scope, snapshot_mode="stale_declared",
        snapshot_declarations=declarations, route_evidence=route_evidence,
        parent_real_domain=parent, parent_real_domain_fingerprint=digest(parent),
        assumption_registry=entries, consumer_registry=registry,
        model_versions=dict(MODELS, m4_joint=M4_VERSION),
        source_evidence_status=[dict(source_id=row["source_id"], sha256=row["sha256"],
                                     evidence_status=row["evidence_status"])
                                for row in _sources()],
        required_scope=scope,
    )
    scenario = validate_research_scenario(
        regional_bundle=dict(bundle_id="zhoushan-rev503-r0005", version="v1", sources=_sources()),
        route_scenario=route_scenario, policies=[policy])
    domain = build_conditional_domain(parent_domain_review=parent, scenario=scenario,
                                      assumption_registry=entries)
    admission = inspect_research_admission(
        scenario=scenario, conditional_domain=domain,
        route_evidence=route_evidence, consumer_capabilities=capabilities,
        run_intent=dict(mode="inspect"))
    return dict(state=state, scenario=scenario, domain=domain, admission=admission,
                normalized=normalized, policy=policy, entries=entries,
                route_evidence=route_evidence, parent=parent, scope=scope)


@pytest.fixture(scope="module")
def built():
    return build()


# 1. 42 个候选动作完整读取并维持唯一 ID
def test_rev503_candidate_actions_are_complete_and_unique(built):
    actions = built["normalized"]["actions"]
    assert built["state"]["revision"] == 503
    assert len(actions) == 42
    assert len({row["action_id"] for row in actions}) == 42
    assert built["normalized"]["stats"]["no_implicit_action_truncation"] is True
    assert built["normalized"]["stats"]["candidate_count"] == 42


# 2. 历史 6 个 selected actions 只作参考，不混入候选全集
def test_selected_actions_are_reference_only(built):
    selected = built["normalized"]["selected_action_ids"]
    assert len(selected) == 6
    assert len(set(selected)) == 6
    assert built["scenario"]["route_scenario"]["selected_actions_are_reference_only"] is True
    assert built["normalized"]["stats"]["candidate_count"] == 42
    assert all(row["historical_selected_action"] is (row["action_id"] in set(selected))
               for row in built["normalized"]["actions"])


# 3. 单航线计划证据支持研究侧动作 route_ids=["R0005"]
def test_plan_scope_gives_every_action_a_traceable_route_id(built):
    assert built["scenario"]["route_scenario"]["route_scope"]["plan_scope_unique"] is True
    for row in built["normalized"]["actions"]:
        assert row["route_ids"] == ["R0005"]
        assert row["route_relation_source"] in ("explicit_action_route_id",
                                                "derived_from_verified_plan_scope")
        assert STATE_SHA in row["route_relation_evidence_ref"]
    counts = built["normalized"]["stats"]["route_relation_counts"]
    assert counts["explicit_action_route_id"] == 2
    assert counts["derived_from_verified_plan_scope"] == 40


# 4. 同塔多设备共享 physical site，但实例分开
def test_same_tower_shares_physical_site_but_instances_are_separate(built):
    tower = "tower-colocation:33090300000028"
    rows = [row for row in built["normalized"]["actions"] if row["physical_site_id"] == tower]
    assert len(rows) == 2
    assert {row["device_id"] for row in rows} == {"COMM-BASELINE-4KM", "RID-BASELINE-2-5KM"}
    assert len({row["device_instance_id"] for row in rows}) == 2
    assert all(row["device_instance_id"].startswith("condinst:") for row in rows)
    assert built["normalized"]["stats"]["physical_site_count"] == 22
    assert built["normalized"]["stats"]["device_instance_count"] == 42


# 5. route_ids=[]、多航线歧义、父子冲突均 blocked
def test_ambiguous_or_empty_route_relation_fails_closed():
    base = dict(action_id="tower_colocation_host:tower-colocation:T:COMM",
                device_id="COMM", service_key="C:communication",
                distinct_site_id="tower:T", coordinate=[122.0, 30.0])
    plan = dict(route_ids=["R0005"], plan_scope_unique=True, plan_scope_conflict=False)
    explicit_empty = normalize_conditional_actions(
        candidate_actions=[dict(base, route_ids=[])], plan_scope=plan,
        scenario_id=SCENARIO_ID, state_sha256=STATE_SHA, state_locator="state")["actions"][0]
    assert explicit_empty["route_ids"] == []
    assert explicit_empty["route_relation_status"] == "blocked"
    multi = normalize_conditional_actions(
        candidate_actions=[dict(base, route_ids=["R0005", "R0006"])], plan_scope=plan,
        scenario_id=SCENARIO_ID, state_sha256=STATE_SHA, state_locator="state")["actions"][0]
    assert multi["route_ids"] == [] and multi["route_relation_status"] == "blocked"
    assert "R0006" in multi["route_relation_reason"]
    conflicting = dict(plan, route_ids=["R0005", "R0006"], plan_scope_unique=False,
                       plan_scope_conflict=True)
    derived = normalize_conditional_actions(
        candidate_actions=[base], plan_scope=conflicting,
        scenario_id=SCENARIO_ID, state_sha256=STATE_SHA, state_locator="state")["actions"][0]
    assert derived["route_ids"] == [] and derived["route_relation_status"] == "blocked"
    # targets 与 before/after 父子不一致：即使三者并集仍只有一个值，也不得继承。
    child_conflict = dict(plan, plan_scope_unique=True, plan_scope_conflict=True)
    conflicted = normalize_conditional_actions(
        candidate_actions=[base], plan_scope=child_conflict,
        scenario_id=SCENARIO_ID, state_sha256=STATE_SHA, state_locator="state")["actions"][0]
    assert conflicted["route_ids"] == []
    assert conflicted["route_relation_status"] == "blocked"
    assert conflicted["route_relation_reason"] == "plan_route_scope_conflict"


# 6. 无真实设备资格时保持 unknown
def test_device_qualification_stays_unknown(built):
    qualifications = built["domain"]["conditional_qualification"]
    assert qualifications
    assert all(row["status"] == "unknown" and row["value"] is None for row in qualifications)
    assert "device_qualification_unknown" in built["admission"]["execution_blockers"]
    assert built["admission"]["final_eligible"] is False


# 7. 缺库存、设备容量、成本时不自动填 0
def test_missing_inventory_capacity_and_cost_are_not_zero(built):
    inventory = built["domain"]["conditional_inventory"]
    assert len(inventory) == 42
    assert all(row["inventory_status"] == "unknown" for row in inventory)
    assert all(row["capacity_status"] == "unknown" for row in inventory)
    assert all(row["cost_status"] == "unknown" for row in inventory)
    assert all(row["cost_unit"] is None for row in inventory)
    for blocker in ("installed_inventory_unknown", "device_capacity_unknown", "cost_unknown"):
        assert blocker in built["admission"]["execution_blockers"]


# 8. stale_declared 记录所有历史失效状态
def test_stale_declared_records_all_historical_states(built):
    scenario = built["scenario"]
    assert scenario["route_scenario"]["snapshot_mode"] == "stale_declared"
    declarations = scenario["route_scenario"]["snapshot_declarations"]
    for layer in ("operational", "validation", "adoption", "operating_layer"):
        assert declarations[layer]["status"] == "stale"
    assert declarations["p14"]["status"] == "failed"
    assert declarations["p15"]["status"] == "failed"
    assert declarations["p16"]["status"] == "proposal_ready"
    assert declarations["p17"]["status"] == "stale"
    assert built["admission"]["route_evidence_status"] == "stale_declared_snapshot"
    for layer in ("operational", "validation", "adoption", "operating_layer"):
        assert "route_evidence_stale_declared:" + layer in built["admission"]["execution_blockers"]


# 9. C 1000 m gap 与 45 m 明确分离
def test_c1000m_gap_and_c45m_post_gate_are_separate(built):
    inventory = {row["constraint_id"]: row for row in built["scope"]["required_constraint_inventory"]}
    assert inventory["REQ-C-GAP-1000"]["max_gap_m"] == 1000
    assert inventory["REQ-C-CONT-45"]["max_gap_m"] == 45
    assert inventory["REQ-C-GAP-1000"]["constraint_id"] != inventory["REQ-C-CONT-45"]["constraint_id"]
    assert inventory["REQ-C-GAP-1000"]["consumer"] is None
    assert inventory["REQ-C-CONT-45"]["consumer"]["consumer_id"] == "p17_continuous"
    excluded = {row["constraint_id"] for row in built["admission"]["excluded_constraints"]}
    included = set(built["admission"]["included_constraint_ids"])
    assert "REQ-C-GAP-1000" in excluded and "REQ-C-GAP-1000" not in included
    assert "REQ-C-CONT-45" in included and "REQ-C-CONT-45" not in excluded


# 10. Required inventory 完整分区，unconsumed 不参与完整最优证明
def test_required_inventory_partition_is_complete_and_unconsumed_not_proven(built):
    admission = built["admission"]
    inventory = {row["constraint_id"] for row in admission["required_inventory"]}
    included = set(admission["included_constraint_ids"])
    excluded = {row["constraint_id"] for row in admission["excluded_constraints"]}
    assert included | excluded == inventory
    assert not (included & excluded)
    assert admission["required_scope_status"] == "partition_complete"
    assert admission["complete_required_cns_status"] == "unknown"
    assert admission["complete_required_proof_status"] == "not_proven"
    assert {row["constraint_id"] for row in admission["unconsumed_requirements"]} >= {
        "REQ-C-GAP-1000", "REQ-N-ACCURACY-10M", "REQ-ENDPOINT-DELIVERY-C"}


# 11. scenario_fingerprint 在相同输入下可重建
def test_scenario_fingerprint_rebuilds_from_identical_inputs(built):
    first = built["scenario"]
    again = validate_research_scenario(
        regional_bundle=first["regional_bundle"],
        route_scenario=first["route_scenario"], policies=first["policies"])
    checked = checked_scenario(deepcopy(first))
    assert again == checked == first
    assert again["scenario_fingerprint"] == first["scenario_fingerprint"]


# 12. 源 SHA 变化引起场景 fingerprint 改变或准入拒绝
def test_source_sha_change_changes_scenario_or_is_rejected(built):
    original = built["scenario"]
    tampered = deepcopy(original)
    tampered["regional_bundle"]["sources"][0]["sha256"] = "0" * 64
    changed = validate_research_scenario(
        regional_bundle=tampered["regional_bundle"],
        route_scenario=tampered["route_scenario"], policies=tampered["policies"])
    assert changed["scenario_fingerprint"] != original["scenario_fingerprint"]
    mismatch = deepcopy(built["route_evidence"])
    mismatch["validation"]["baseline_sha256"] = "0" * 64
    rejected = inspect_research_admission(
        scenario=original, conditional_domain=built["domain"], route_evidence=mismatch,
        consumer_capabilities=dict(consumers={}, supported_action_families=[]),
        run_intent=dict(mode="inspect"))
    assert rejected["status"] == "blocked"
    assert "route_chain_mismatch:validation:baseline_sha256" in rejected["blockers"]


# 13. 无越界路径、无对正式 State 的写入
def test_contract_is_non_executable_and_state_is_not_written(built):
    admission = built["admission"]
    assert built["scenario"]["execution_enabled"] is False
    assert admission["execution_enabled"] is False
    assert admission["final_eligible"] is False
    assert admission["execution_authorization"] == "not_authorized"
    assert admission["persisted_to_project_state"] is False
    assert built["scenario"]["route_scenario"]["source_evidence_status"]


# 14. 旧 synthetic fixture 相关测试不回归：由既有 test_research_scenario*.py 覆盖。
# 15. P1-B 消费端显式航线关系回归：由 tests/test_round38_f2_route_relation.py 覆盖。


# 16. P2-A3-A：父域必须重建为 373 座塔 + N005/N006 两个端点 = 375 个不同站址
def test_parent_domain_rebuilds_373_towers_plus_two_endpoints(built):
    parent = built["parent"]
    ledger = parent["site_ledger"]
    assert parent["tower_count"] == 373
    assert parent["endpoint_count"] == 2
    assert len(ledger) == 375
    site_ids = [row["physical_site_id"] for row in ledger]
    assert len(set(site_ids)) == 375
    endpoint_ids = [row["physical_site_id"] for row in ledger if row["site_type"] == "endpoint"]
    assert endpoint_ids == ["node:N005", "node:N006"]
    conditional_sites = {row["physical_site_id"] for row in built["normalized"]["actions"]}
    assert len(conditional_sites) == 22
    assert conditional_sites <= set(site_ids)
    assert built["normalized"]["stats"]["physical_site_count"] == 22
    assert built["normalized"]["stats"]["device_instance_count"] == 42
    # 旧缺陷路径会在此处留下 conditional_site_missing；修复后必须为空。
    assert built["domain"]["blockers"] == []
    assert built["admission"]["static_contract_passed"] is True
    assert built["admission"]["static_contract_blockers"] == []
    assert built["admission"]["final_eligible"] is False


# 17. P2-A3-A：端点身份只来自 canonical node: 站址，不再依赖不存在的字段
def test_endpoint_identity_comes_from_canonical_node_sites(built):
    actions = built["normalized"]["actions"]
    endpoint_actions = [row for row in actions if row["physical_site_id"].startswith("node:")]
    assert sorted(row["physical_site_id"] for row in endpoint_actions) == \
        ["node:N005", "node:N006"]
    assert all(row["physical_site_source"] == "distinct_site_id"
               for row in endpoint_actions)
    assert sorted(row["source_distinct_site_id"] for row in endpoint_actions) == \
        ["takeoff_landing_site:N005", "takeoff_landing_site:N006"]
    assert sorted(row["source_site_id"] for row in endpoint_actions) == ["N005", "N006"]
    # 规范化动作没有 takeoff_landing_site_id 字段；不得再以它作为端点来源。
    assert all("takeoff_landing_site_id" not in row for row in actions)
    assert built["scenario"]["route_scenario"]["endpoint_refs"] == list(ENDPOINT_REFS)


# 18. P2-A3-A：重建的父域与 42 动作身份必须与 P2-A1 冻结产物一致
def test_rebuild_identity_matches_frozen_p2a1_artifact(built):
    frozen_route = _frozen_scenario()["route_scenario"]
    assert frozen_route["parent_real_domain_fingerprint"] == FROZEN_PARENT_SHA
    assert built["parent"] == frozen_route["parent_real_domain"]
    rebuilt_route = built["scenario"]["route_scenario"]
    assert rebuilt_route["parent_real_domain_fingerprint"] == FROZEN_PARENT_SHA
    assert rebuilt_route["action_contract_stats"] == frozen_route["action_contract_stats"]
    rebuilt_actions = built["normalized"]["actions"]
    frozen_actions = frozen_route["conditional_actions"]
    assert len(rebuilt_actions) == len(frozen_actions) == 42
    for rebuilt_row, frozen_row in zip(rebuilt_actions, frozen_actions):
        differing = {key for key in set(rebuilt_row) | set(frozen_row)
                     if rebuilt_row.get(key) != frozen_row.get(key)}
        # 唯一允许的差异：evidence_ref 内嵌绝对 state 路径，冻结产物用仓库相对定位。
        assert differing <= {"route_relation_evidence_ref"}, differing


# 19. P2-A3-A：严格验收入口——真实冻结输入、SHA 与端点重建必须真实执行
def test_strict_acceptance_real_inputs_are_present_and_hashed(built):
    assert STATE.exists(), "strict acceptance requires " + str(STATE)
    assert hashlib.sha256(STATE.read_bytes()).hexdigest() == STATE_SHA
    assert built["state"]["revision"] == 503
    frozen_route = _frozen_scenario()["route_scenario"]
    assert frozen_route["parent_real_domain_fingerprint"] == FROZEN_PARENT_SHA
    assert len(built["parent"]["site_ledger"]) == 375
    assert built["domain"]["blockers"] == []
    assert built["admission"]["final_eligible"] is False
