"""Round 2.8 —— P16 连续服务停止条件与收益模型的定向回归。

本轮修复的根因（真实项目 R0005 复现）：

* P16 的停止条件只看 P15 的**离散** objective（体积分数 / 冗余满足分数 / unknown 比例）。
  真实项目里这三个目标在投影态全部满足，于是 P16 打印
  ``all_evaluable_confirmed_objectives_met`` 并停止；
* 同一份投影态的**最长连续缺口**仍是 143.54 m ＝ 9.57 s ＞ 用户显式登记的 3.0 s，
  P17 因此判 ``unacceptable``、Step6 ``confirmation_allowed=false``。
* 两者不是矛盾：P16 的停止条件**根本不消费**连续服务这一目标。

本模块覆盖用户要求的后端 8 项：

1. residual C gap 时 P16 不能错误停止；
2. 连续服务缺口改善必须被登记为正向收益（``continuous_service_gain``）；
3. 跨阈值动作必须在候选评分中被优先；
4. 没有正向候选时停止原因必须可区分；
5. reuse-first 层序保持；
6. RID 的 satisfied 结论不因 Communication 修改而退化；
7. 投影态（facilities / 上游 P14/P15）绝不被 P16 改写；
8. P17 / Step6 门禁在投影态不合格时保持 fail-closed。
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from cns_planner.application.corridor_site_planning_service import (
    _continuous_service_acceptance, _continuous_service_gain, _impact,
    _intervention_selection_limit, _no_positive_candidate_stop_reason,
)
from cns_planner.domain.corridor_site_planning import default_corridor_site_planning_policy
from cns_planner.site_planner.corridor_reuse_first_v2 import CorridorReuseFirstSitePlannerV2

from test_corridor_site_planner_v2 import (
    configured, device, existing, prepare_p17, p17_evidence, vertical,
)


OBJECTIVES_VOLUME_ONLY = {"routes": {"R1": {"subsystems": {"C": {"objectives": {
    "min_satisfied_volume_fraction": {
        "value": 0.01, "operator": ">=", "source": "test", "confirmed": True,
    },
}}}}}}


def _targets(workflow):
    """P16 消费的 target 集合（与正式 evaluate 同一来源：P15 confirmed target voxel）。"""

    plan = workflow.evaluate_cns_corridor_site_plan()["cns_corridor_site_plan"]
    return plan["targets"], plan


def _crossing_fixture(tmp_path):
    """构造"一次动作就能跨过连续服务阈值"的最小合成场景。

    合成航路 R1 长 222.39 m、单走廊体素（半对角线 ≈ 71.2 m），P15 保守投影出的
    连续缺口约 142.4 m。把 C 全失联阈值登记为 3.0 s（航路速度 20 m/s ⇒ 60 m）后，
    基线必然不合格；半径 500 m 的候选塔把它降到 0 m ⇒ 跨过阈值。
    """

    workflow = configured(tmp_path, redundancy=1, objectives=OBJECTIVES_VOLUME_ONLY)
    prepare_p17(workflow, outage_limit_s=3.0, degradation_limit_s=20.0)
    return workflow


def test_continuous_service_threshold_comes_from_explicit_evidence(tmp_path):
    """阈值必须来自用户显式登记的证据 × 航路速度，且与 P17 同源。"""

    workflow = _crossing_fixture(tmp_path)
    inputs = workflow.continuous_service_service.compute_inputs()
    threshold_m = workflow.continuous_service_service.continuous_service_threshold_m(inputs)
    #: 3.0 s × 20 m/s（fixture 的机载 cruise_speed_mps）
    assert threshold_m == pytest.approx(60.0)

    #: 阈值参数被显式登记为 engineering_assumption（不是设备 failsafe 事实）。
    from cns_planner.domain.planning_evidence import resolve_continuous_parameter

    resolved = resolve_continuous_parameter(
        workflow.state["planning_evidence"], "c_full_outage_max_s",
    )
    assert resolved["value"] == pytest.approx(3.0)
    assert resolved["authority"] == "explicit_evidence"
    assert resolved["source_type"] == "engineering_assumption"


def test_residual_gap_does_not_let_p16_stop_with_discrete_only_objectives(tmp_path):
    """① 有残差 + 阈值证据时，P16 不能因为"离散目标满足"就停止。"""

    workflow = _crossing_fixture(tmp_path)
    plan = workflow.evaluate_cns_corridor_site_plan()["cns_corridor_site_plan"]

    #: 新词表：离散与连续**都**达标才允许 "all_required_services_acceptable"。
    assert plan["stop_reason"] in (
        "all_required_services_acceptable",
        "continuous_service_acceptable_no_remaining_improvement",
    )
    assert plan["continuous_service_acceptable"] is True
    assert plan["continuous_service_threshold_m"] == pytest.approx(60.0)
    assert plan["final_continuous_service_acceptance"]["longest_deficit_m"] == pytest.approx(0.0)
    #: 旧词表绝不允许再出现（它正是"P16 停止但 P17 不合格"的措辞）。
    assert plan["stop_reason"] != "all_evaluable_confirmed_objectives_met"

    #: 对照：同一个 fixture 若**不**登记连续服务阈值证据，连续服务不是判据，
    #: 停止原因必须逐字保持既有措辞（绝不凭空多出一条工程结论）。
    plain = configured(tmp_path / "plain", redundancy=1, objectives=OBJECTIVES_VOLUME_ONLY)
    plain_plan = plain.evaluate_cns_corridor_site_plan()["cns_corridor_site_plan"]
    assert plain_plan["continuous_service_threshold_m"] is None
    assert plain_plan["continuous_service_acceptable"] is None
    assert plain_plan["stop_reason"] != (
        "no_positive_remaining_candidate_continuous_service_threshold_unknown"
    )


def test_continuous_service_gain_is_registered_on_the_action(tmp_path):
    """② 连续缺口缩短必须作为**独立**收益登记，绝不混进离散体积增益。"""

    workflow = _crossing_fixture(tmp_path)
    targets, plan = _targets(workflow)
    action = next(
        item for item in plan["candidate_actions"]
        if item["action_id"] == "candidate_site:S1:C1"
    )
    before = workflow.state["cns_corridor_gap_assessment"]
    services = workflow.corridor_site_planning_service

    impact, _, after_gap = services._what_if(
        action, deepcopy(workflow.state["existing_cns_facilities"]), before, targets,
        iteration=1,
        continuous_threshold_m=60.0, route_speed_mps=20.0, aircraft_id="A1",
    )
    gain = impact["continuous_service_gain"]
    assert gain["threshold_available"] is True
    assert gain["before_longest_deficit_m"] > 0
    assert gain["after_longest_deficit_m"] == pytest.approx(0.0)
    assert gain["longest_outage_reduction_m"] == pytest.approx(
        gain["before_longest_deficit_m"]
    )
    assert gain["longest_outage_reduction_s"] == pytest.approx(
        gain["longest_outage_reduction_m"] / 20.0
    )
    assert gain["threshold_crossed"] is True
    assert gain["acceptability_improved"] is True
    assert gain["semantics"] == (
        "continuous_service_gain_is_longest_projected_outage_reduction_on_the_same_"
        "conservative_longitudinal_projection_used_by_p17"
    )
    #: 与 P17 的 event 长度逐字段一致（同一物理量、同一投影）。
    from cns_planner.algorithms.continuous_service.v1 import (
        ContinuousServiceAcceptabilityV1,
    )

    state = workflow.session.state
    p17 = ContinuousServiceAcceptabilityV1().evaluate(
        corridor_assessment=workflow.state["cns_corridor_assessment"],
        corridor_gap_assessment=workflow.state["cns_corridor_gap_assessment"],
        site_plan=workflow.state["cns_corridor_site_plan"],
        aircraft_profile=workflow.continuous_service_service.compute_inputs()["aircraft_profile"],
        planning_evidence=workflow.state["planning_evidence"],
        operation_scenario=workflow.continuous_service_service.operation_scenario_snapshot(),
        continuous_service_policy=workflow.continuous_service_service.policy_snapshot(),
        device_catalog=workflow.state["device_catalog"],
        radar_surveillance_layout=workflow.state["radar_surveillance_layout"],
    )
    baseline_event = next(
        event for route in p17["routes"] for sub in route["subsystems"]
        if sub["subsystem"] == "C" for event in sub["events"]
        if event["kind"] == "service_outage"
    )
    assert baseline_event["length_m"] == pytest.approx(gain["before_longest_deficit_m"])
    assert state is not None


def test_threshold_crossing_action_outranks_partial_reduction():
    """③ 跨阈值动作必须在连续服务候选评分中优先于只做部分缩短的动作。"""

    planner = CorridorReuseFirstSitePlannerV2()
    actions = [
        {"action_id": "crossing", "reuse_class": "candidate_site",
         "eligibility": {"status": "eligible"}},
        {"action_id": "partial", "reuse_class": "candidate_site",
         "eligibility": {"status": "eligible"}},
    ]
    impacts = [
        {"action_id": "partial", "status": "eligible",
         "confirmed_requirement_unit_volume_gain": 0.0, "regressions": [],
         "continuous_service_gain": {
             "threshold_available": True, "threshold_crossed": False,
             "longest_outage_reduction_m": 90.0, "total_projection_reduction_m": 90.0,
         }},
        {"action_id": "crossing", "status": "eligible",
         "confirmed_requirement_unit_volume_gain": 0.0, "regressions": [],
         "continuous_service_gain": {
             "threshold_available": True, "threshold_crossed": True,
             "longest_outage_reduction_m": 60.0, "total_projection_reduction_m": 60.0,
         }},
    ]
    ranked = planner.rank_continuous_service(actions, impacts)
    assert [item["action"]["action_id"] for item in ranked] == ["crossing", "partial"]
    assert ranked[0]["threshold_crossed"] is True
    assert ranked[0]["score_semantics"] == (
        "longest_continuous_service_outage_reduction_m_per_action_count_proxy"
    )
    assert ranked[0]["benefit_semantics"] == (
        "longest_continuous_service_outage_reduction_m_same_projection_metric_as_p17"
    )
    #: 离散通道绝不因为连续收益而放行（两条通道互不越权）。
    assert planner.rank(actions, impacts) == []


def test_continuous_channel_is_fail_closed_without_threshold_evidence():
    """阈值证据缺失时，连续收益不得升级为合格动作（UNKNOWN ≠ PASS）。"""

    before = {
        "routes": [{"route_id": "R1", "subsystems": [{
            "subsystem": "C", "max_continuous_deficit_projection_m": 100.0,
            "objective_results": [],
        }]}],
    }
    after = {
        "routes": [{"route_id": "R1", "subsystems": [{
            "subsystem": "C", "max_continuous_deficit_projection_m": 10.0,
            "objective_results": [],
        }]}],
    }
    action = {
        "action_id": "A", "eligibility": {"status": "eligible"},
    }
    impact = _impact(action, before, after, [], continuous_threshold_m=None,
                     route_speed_mps=20.0, aircraft_id="A1")
    assert impact["continuous_service_gain"]["longest_outage_reduction_m"] == pytest.approx(90.0)
    assert impact["continuous_service_gain"]["threshold_available"] is False
    assert impact["status"] == "unknown"
    assert impact["evidence_status"] == "unknown_only"


def test_no_positive_candidate_stop_reason_is_distinguishable():
    """④ 停止原因必须能区分"离散达标但连续不合格"与"阈值证据缺失"。"""

    assert _no_positive_candidate_stop_reason(
        {"acceptable": False}, "proposal_ready", threshold_available=True,
    ) == "coverage_objectives_met_but_continuous_service_unacceptable"
    assert _no_positive_candidate_stop_reason(
        {"acceptable": None}, "proposal_ready", threshold_available=True,
    ) == "no_positive_remaining_candidate_continuous_service_threshold_unknown"
    assert _no_positive_candidate_stop_reason(
        {"acceptable": None}, "proposal_ready", threshold_available=False,
    ) == "no_positive_confirmed_marginal_gain"
    assert _no_positive_candidate_stop_reason(
        {"acceptable": True}, "proposal_ready", threshold_available=True,
    ) == "continuous_service_acceptable_no_remaining_improvement"
    assert _no_positive_candidate_stop_reason(
        {"acceptable": True}, "no_eligible_proposal", threshold_available=True,
    ) == "no_positive_remaining_candidate"


def test_default_stop_reason_vocabulary_no_longer_says_all_objectives_met():
    """旧措辞必须从默认策略里消失，也不得再被**赋值**为停止原因。"""

    policy = default_corridor_site_planning_policy()
    assert "confirmed_objectives_met_else_no_positive_marginal_gain" not in policy["stop_policy"]
    assert "continuous_service_projection_within" in policy["stop_policy"]
    source = Path(
        "cns_planner/application/corridor_site_planning_service.py"
    ).read_text(encoding="utf-8")
    #: 旧措辞只允许出现在"为什么不再这样写"的注释里，**绝不**允许被赋值。
    assert 'stop_reason = "all_evaluable_confirmed_objectives_met"' not in source
    assert '"all_evaluable_confirmed_objectives_met",' not in source


def test_reuse_first_tier_order_is_preserved(tmp_path):
    """⑤ reuse-first：同一次评估里必须先复用既有站址，再考虑候选新站。"""

    workflow = configured(
        tmp_path, redundancy=1, facilities=[existing()], candidates=None,
    )
    result = workflow.evaluate_cns_corridor_site_plan()["cns_corridor_site_plan"]
    assert result["selected_actions"][0]["reuse_class"] == "existing_shared_site"
    assert result["selected_actions"][0]["action_id"] == "existing_shared_site:F1:C1"
    assert len(result["selected_actions"]) == 1
    #: 连续服务阈值未登记 ⇒ 连续通道不参与，选站序列与 Round 2.7 逐字段一致。
    assert result["continuous_service_threshold_m"] is None
    assert result["continuous_service_acceptable"] is None
    assert result["stop_reason"] == "no_positive_confirmed_marginal_gain"
    limit = _intervention_selection_limit(result["planning_policy"])
    assert result["intervention_selection_limit"] == limit


def test_rid_satisfaction_is_not_degraded_by_communication_selection(tmp_path):
    """⑥ Communication 的连续服务通道绝不能让 RID 的 satisfied 结论退化。"""

    workflow = configured(tmp_path, redundancy=1, objectives=OBJECTIVES_VOLUME_ONLY)
    prepare_p17(workflow, outage_limit_s=3.0, degradation_limit_s=20.0)
    plan = workflow.evaluate_cns_corridor_site_plan()["cns_corridor_site_plan"]
    groups = {item["service_key"]: item for item in plan["target_service_groups"]}
    assert groups["C:communication"]["confirmed_gap"] == 0
    #: RID 在本合成场景没有 target（无 S 缺口）⇒ 不得凭空出现残差或退化。
    assert "confirmed_gap" not in {
        key: value["confirmed_gap"] for key, value in groups.items()
        if value["confirmed_gap"] > 0 and key.startswith("S:")
    }
    rid = groups.get("S:rid_cooperative")
    if rid is not None:
        assert rid["confirmed_gap"] == 0


def test_projected_state_is_never_written_back(tmp_path):
    """⑦ P16 只做提案：facilities 与上游 P14/P15 绝不被投影态改写。"""

    workflow = _crossing_fixture(tmp_path)
    before = deepcopy({key: workflow.state[key] for key in (
        "existing_cns_facilities", "cns_corridor_assessment",
        "cns_corridor_gap_assessment",
    )})
    plan = workflow.evaluate_cns_corridor_site_plan()["cns_corridor_site_plan"]
    assert plan["proposal_only"] is True
    assert plan["requires_user_confirmation_and_apply"] is True
    #: Phase4-B5X：完整 P14+P15 假想证据已外置到专用只读接口，仍必须声明"未写入上游"。
    snapshot = workflow.cns_corridor_site_plan_snapshot()
    evidence = snapshot.get("final_hypothetical_evidence")
    if evidence is not None:
        assert evidence["persisted_as_upstream"] is False
    assert snapshot.get("final_hypothetical_evidence_persisted_as_upstream") is not True
    for key, value in before.items():
        assert workflow.state[key] == value, f"{key} 被 P16 投影态改写了"


def test_p17_step6_gate_stays_fail_closed_while_continuous_service_is_unacceptable(tmp_path):
    """⑧ P17 / Step6 门禁在投影态不合格时必须保持 fail-closed。"""

    workflow = configured(tmp_path, redundancy=1, objectives=OBJECTIVES_VOLUME_ONLY)
    #: 阈值 3.0 s 且**没有**任何可补 gap 的候选 ⇒ 投影态必然仍不合格。
    prepare_p17(workflow, outage_limit_s=3.0, degradation_limit_s=20.0)
    workflow.state["candidate_sites"] = {"status": "passed", "items": [], "count": 0}
    workflow.state["device_catalog"] = {"status": "passed", "items": [], "count": 0}
    workflow.evaluate_cns_corridor()
    workflow.evaluate_cns_corridor_gap()
    plan = workflow.evaluate_cns_corridor_site_plan()["cns_corridor_site_plan"]
    assert plan["continuous_service_acceptable"] is False
    assert plan["stop_reason"] == (
        "coverage_objectives_met_but_continuous_service_unacceptable"
    )
    workflow.evaluate_cns_continuous_service()
    gate = workflow.cns_continuous_service_step6_gate()
    assert gate["confirmation_allowed"] is False
    assert gate["status"] == "unacceptable"
