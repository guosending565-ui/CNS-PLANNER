"""Round30-C3B（收口）：P16 干预选择上限的三态停止语义与只读 existence probe。

本轮裁决（代码契约，不写进 AI_DEV_CONTEXT）：

* ``intervention_selection_limit_reached`` **只在**达选择上限且**真的**还有正向候选时成立；
  "cap 已满 + 剩余候选实际无正向增益"必须保持旧 ``no_positive_*`` 词表语义；
* 达上限时不做旧版"完整 candidate impacts / ranking"，改为一次**只读** existence probe：
  按原 REUSE_TIERS / candidate 顺序（从当前 tier 起）探测，找到第一个真正正向的候选即
  短路；判据直接复用**正式排名器**（``rank`` / ``rank_continuous_service``），不另立一套；
* probe 不改候选排序、选择结果、P15/P17、distinct-site / Radar / cap 默认值 6；
* 探测不到任何正向候选时回到旧路径（完整评估 → 自然耗尽），并把 probe 已算出的 impacts
  复用给完整评估，因此 ``stop_reason`` 与逐字段输出与旧实现一致，且不重复计算。

覆盖：

1. cap 满 + 后续存在 positive → ``intervention_selection_limit_reached``；
2. cap 满 + pending 非空但全部 zero gain → 与旧实现相同的 ``no_positive_*``（逐字段等价）；
3. cap 满 + candidate pool empty → 原停止语义（逐字段等价）；
4. ``stop_diagnostics`` 三态互斥；
5. probe 的 tier / candidate 顺序与短路计数契约。
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from cns_planner.application.corridor_site_planning_service import (
    STOP_DIAGNOSTICS_SEMANTICS, _continuous_service_acceptance,
)
from cns_planner.domain.corridor_site_planning import default_corridor_site_planning_policy

from test_corridor_site_planner_v2 import candidate, configured, device


#: 旧实现（C3B 之前）在"没有正向候选"时的停止原因词表。cap 满但确实无正向增益时，
#: 必须是这一族，而不是 ``intervention_selection_limit_reached``。
LEGACY_NO_POSITIVE_STOP_REASONS = (
    "no_positive_confirmed_marginal_gain",
    "no_positive_remaining_candidate",
    "no_positive_remaining_candidate_continuous_service_threshold_unknown",
    "coverage_objectives_met_but_continuous_service_unacceptable",
    "continuous_service_acceptable_no_remaining_improvement",
)

#: 与 P16 选择结果无关、允许在"达上限"与"未达上限"两次运行之间不同的字段。
CAP_VARIANT_KEYS = ("intervention_selection_limit", "stop_diagnostics", "planning_policy")

#: 选择语义的逐字段契约面（cap 只允许改变"是否继续排名"，不允许改变这些字段）。
CONTRACT_KEYS = (
    "status", "stop_reason", "selected_actions", "iteration_trace",
    "candidate_impacts", "continuous_service_candidate_impacts",
    "before", "after", "reuse_counts", "target_service_groups", "targets",
    "confirmed_requirement_unit_volume_gain",
    "cumulative_predicted_requirement_unit_volume_gain",
    "combined_what_if_gain_difference",
    "baseline_continuous_service_acceptance", "final_continuous_service_acceptance",
    "continuous_service_acceptable", "continuous_service_threshold_m",
    "residual_confirmed_targets", "residual_unknown_evidence",
    "baseline_corridor_fingerprint", "baseline_corridor_gap_fingerprint",
    "final_hypothetical_corridor_fingerprint", "final_hypothetical_corridor_gap_fingerprint",
    "authoritative_combined_what_if_consistent",
)

OBJECTIVES_VOLUME_ONLY = {"routes": {"R1": {"subsystems": {"C": {"objectives": {
    "min_satisfied_volume_fraction": {
        "value": 0.01, "operator": ">=", "source": "test", "confirmed": True,
    },
}}}}}}


def evaluate_with_cap(workflow, cap):
    """以显式 ``max_selected_interventions`` 运行 P16（与生产同一入口）。

    返回 :meth:`WorkflowService.cns_corridor_site_plan_snapshot` 的**全量**只读结果
    （``evaluate`` 的返回值是 slim 投影，``candidate_impacts`` 已外置到 artifact）。
    """

    policy = deepcopy(
        workflow.state.get("corridor_site_planning_policy")
        or default_corridor_site_planning_policy()
    )
    policy.setdefault("parameters", {})["max_selected_interventions"] = cap
    workflow.evaluate_cns_corridor_site_plan(
        {"corridor_site_planning_policy": policy},
    )
    return workflow.cns_corridor_site_plan_snapshot()


def assert_three_state_mutual_exclusion(plan):
    """``stop_diagnostics`` 三态互斥（只读披露），且与 ``stop_reason`` 一致。"""

    diagnostics = plan["stop_diagnostics"]
    assert diagnostics["version"] == 2
    assert diagnostics["semantics"] == STOP_DIAGNOSTICS_SEMANTICS
    assert diagnostics["selection_limit"] == plan["intervention_selection_limit"]
    #: 三态互斥：达上限 / 候选池自然耗尽 / 全部服务可接受，绝不同时成立。
    assert not (
        diagnostics["selection_limit_reached"]
        and diagnostics["candidate_pool_exhausted"]
    )
    assert diagnostics["converged_without_positive_candidate"] is bool(
        diagnostics["candidate_pool_exhausted"]
    )
    #: "跳过完整评估"与"probe 找到正向候选"都只在达上限成立时才有意义。
    if diagnostics["full_candidate_evaluation_skipped_at_limit"]:
        assert diagnostics["selection_limit_reached"] is True
    if diagnostics["limit_positive_probe_found"]:
        assert diagnostics["selection_limit_reached"] is True
    if diagnostics["candidate_pool_exhausted"]:
        #: 自然耗尽 ⇒ 旧 no_positive_* 词表；停止点已无待处理候选。
        assert plan["stop_reason"] in LEGACY_NO_POSITIVE_STOP_REASONS
        assert diagnostics["selection_limit_reached"] is False
        assert diagnostics["full_candidate_evaluation_skipped_at_limit"] is False
        assert diagnostics["pending_candidate_count"] is None
    elif diagnostics["selection_limit_reached"]:
        assert plan["stop_reason"] == "intervention_selection_limit_reached"
        assert diagnostics["limit_positive_probe_performed"] is True
        assert diagnostics["limit_positive_probe_found"] is True
        assert diagnostics["limit_positive_probe_evaluated_count"] >= 1
        assert diagnostics["pending_candidate_count"] > 0
    else:
        assert plan["stop_reason"] == "all_required_services_acceptable"
        assert diagnostics["limit_positive_probe_performed"] is False
    return diagnostics


def test_cap_reached_with_a_remaining_positive_candidate_stops_at_the_limit(tmp_path):
    """① cap 满 + 后续存在 positive ⇒ ``intervention_selection_limit_reached``。"""

    workflow = configured(
        tmp_path, redundancy=2,
        devices=[device("C1", "group-a"), device("C2", "group-b")],
        candidates=[candidate("S1")],
    )
    plan = evaluate_with_cap(workflow, 1)
    diagnostics = assert_three_state_mutual_exclusion(plan)

    assert plan["intervention_selection_limit"] == 1
    assert plan["stop_reason"] == "intervention_selection_limit_reached"
    assert diagnostics["selection_limit_reached"] is True
    #: 未做旧版完整 impacts / ranking，因此候选评估必须在 probe 命中的第一个候选就短路。
    assert diagnostics["full_candidate_evaluation_skipped_at_limit"] is True
    assert diagnostics["limit_positive_probe_performed"] is True
    assert diagnostics["limit_positive_probe_found"] is True
    assert diagnostics["limit_positive_probe_evaluated_count"] == 1
    assert diagnostics["limit_positive_probe_seconds"] is not None
    assert diagnostics["pending_candidate_count"] == 1
    assert diagnostics["candidate_pool_exhausted"] is False
    assert diagnostics["converged_without_positive_candidate"] is False
    #: 选择结果只到 cap，且没有为"注定不会被采用"的候选生成第二轮 candidate_impacts。
    assert len(plan["selected_actions"]) == 1
    assert len(plan["candidate_impacts"]) == 2

    #: 反向证明 probe 的结论不是假阳性：同一场景 cap=2 时那个 pending 候选**确实**被选中
    #: 且边际增益 > 0（即它真的是正向候选）。
    uncapped = evaluate_with_cap(
        configured(
            tmp_path / "cap2", redundancy=2,
            devices=[device("C1", "group-a"), device("C2", "group-b")],
            candidates=[candidate("S1")],
        ),
        2,
    )
    assert len(uncapped["selected_actions"]) == 2
    assert (
        uncapped["selected_actions"][1]["marginal_confirmed_requirement_unit_volume_gain"]
        > 0
    )


def test_cap_reached_with_only_zero_gain_pending_keeps_legacy_no_positive_reason(tmp_path):
    """② cap 满 + pending 非空但全部 zero gain ⇒ 与旧实现相同的 ``no_positive_*``。"""

    def build(root):
        return configured(
            root, redundancy=2,
            #: 同一独立组 ⇒ 第二个 provider 不产生任何已确认增益（zero gain）。
            devices=[device("C1", "same"), device("C2", "same")],
            candidates=[candidate("S1")],
        )

    capped = evaluate_with_cap(build(tmp_path / "cap1"), 1)
    uncapped = evaluate_with_cap(build(tmp_path / "cap6"), 6)
    diagnostics = assert_three_state_mutual_exclusion(capped)

    assert capped["stop_reason"] in LEGACY_NO_POSITIVE_STOP_REASONS
    assert capped["stop_reason"] == uncapped["stop_reason"]
    assert capped["stop_reason"] != "intervention_selection_limit_reached"
    assert diagnostics["candidate_pool_exhausted"] is True
    assert diagnostics["converged_without_positive_candidate"] is True
    assert diagnostics["selection_limit_reached"] is False
    assert diagnostics["full_candidate_evaluation_skipped_at_limit"] is False
    #: probe 真的做了（pending 非空），只是结论是"没有正向候选"，因此不下 cap 结论。
    assert diagnostics["limit_positive_probe_performed"] is True
    assert diagnostics["limit_positive_probe_found"] is False
    assert diagnostics["limit_positive_probe_evaluated_count"] == 1
    assert diagnostics["pending_candidate_count"] is None

    #: 逐字段等价：cap 只改变"是否继续排名"，不改变任何选择 / P15 / P17 输出。
    for key in CONTRACT_KEYS:
        assert capped[key] == uncapped[key], f"{key} 在达上限与未达上限两次运行之间不一致"
    #: 除诊断字段本身之外，允许不同的只有三类：cap 参数本身（``planning_policy`` 与
    #: 由此派生的 ``input_fingerprint``）、只读性能画像（达上限那一轮由 probe 计算并由
    #: 完整评估复用，probe 耗时单独登记，因此不再计入选择循环画像）。
    differing = {
        key for key in set(capped) | set(uncapped)
        if capped.get(key) != uncapped.get(key)
    }
    assert differing <= set(CAP_VARIANT_KEYS) | {
        "parameters", "input_fingerprint", "performance_profile",
    }, differing


def test_cap_reached_with_empty_candidate_pool_keeps_legacy_path(tmp_path):
    """③ cap 满 + candidate pool empty ⇒ 原停止语义（不做 probe，不下 cap 结论）。"""

    def build(root):
        #: redundancy=3 而只有 2 个可行动作 ⇒ 动作全部选中后目标仍未满足，池已空。
        return configured(
            root, redundancy=3,
            devices=[device("C1", "group-a"), device("C2", "group-b")],
            candidates=[candidate("S1")],
        )

    capped = evaluate_with_cap(build(tmp_path / "cap2"), 2)
    uncapped = evaluate_with_cap(build(tmp_path / "cap6"), 6)
    diagnostics = assert_three_state_mutual_exclusion(capped)

    assert capped["stop_reason"] in LEGACY_NO_POSITIVE_STOP_REASONS
    assert capped["stop_reason"] == uncapped["stop_reason"]
    assert diagnostics["candidate_pool_exhausted"] is True
    assert diagnostics["selection_limit_reached"] is False
    #: 停止点没有待处理候选 ⇒ 连 probe 都不做（连"未评估"都不需要披露）。
    assert diagnostics["limit_positive_probe_performed"] is False
    assert diagnostics["limit_positive_probe_evaluated_count"] == 0
    assert diagnostics["pending_candidate_count"] is None

    for key in CONTRACT_KEYS:
        assert capped[key] == uncapped[key], f"{key} 在达上限与未达上限两次运行之间不一致"


def test_third_state_is_all_required_services_acceptable_without_any_limit_claim(tmp_path):
    """④ 三态互斥的第三态：目标达成停止，三个 flag 都不成立。"""

    workflow = configured(
        tmp_path, redundancy=1, objectives=OBJECTIVES_VOLUME_ONLY,
        devices=[device("C1", "group-a")], candidates=[candidate("S1")],
    )
    plan = evaluate_with_cap(workflow, 1)
    diagnostics = assert_three_state_mutual_exclusion(plan)

    assert plan["stop_reason"] == "all_required_services_acceptable"
    assert diagnostics["selection_limit_reached"] is False
    assert diagnostics["candidate_pool_exhausted"] is False
    assert diagnostics["full_candidate_evaluation_skipped_at_limit"] is False
    assert diagnostics["limit_positive_probe_performed"] is False
    assert diagnostics["limit_positive_probe_found"] is False
    assert len(plan["selected_actions"]) == 1


def test_probe_scans_from_current_tier_in_candidate_order_and_short_circuits(tmp_path):
    """⑤ probe 契约：从当前 tier 起、按 candidate 顺序探测，命中第一个正向即停。

    早于当前 tier 的候选**绝不**回看（旧实现同样不回看，回看会凭空多出"达上限"结论），
    命中之后的候选**绝不**继续探测（因此不生成完整 candidate_impacts）。
    """

    workflow = configured(tmp_path, devices=[device("C1", "group-a")], candidates=[])
    service = workflow.corridor_site_planning_service

    def action(action_id, reuse_class, gain):
        return {
            "action_id": action_id, "reuse_class": reuse_class,
            "eligibility": {"status": "eligible"},
            "_expected_gain": gain,
        }

    actions = [
        action("existing_cns_facility:A:C1", "existing_cns_facility", 5.0),
        action("existing_shared_site:B:C1", "existing_shared_site", 5.0),
        action("tower_colocation_host:C:C1", "tower_colocation_host", 0.0),
        action("tower_colocation_host:D:C1", "tower_colocation_host", 2.0),
        action("tower_colocation_host:E:C1", "tower_colocation_host", 7.0),
        action("candidate_site:F:C1", "candidate_site", 9.0),
    ]
    probed = []

    def fake_what_if(candidate_action, *_args, **_kwargs):
        probed.append(candidate_action["action_id"])
        return ({
            "action_id": candidate_action["action_id"], "status": "eligible",
            "confirmed_requirement_unit_volume_gain": candidate_action["_expected_gain"],
            "regressions": [], "continuous_service_gain": None,
        }, None, None)

    service._what_if = fake_what_if
    result = service._probe_positive_remaining_candidates(
        actions, set(), "tower_colocation_host", {}, {}, [], 1,
        [], [], None, None, None, None, None, None, None, [],
    )

    #: 早期 tier 绝不回看；命中后立即短路。
    assert result["found"] is True
    assert result["tier"] == "tower_colocation_host"
    assert result["action_id"] == "tower_colocation_host:D:C1"
    assert probed == [
        "tower_colocation_host:C:C1", "tower_colocation_host:D:C1",
    ]
    assert result["evaluated"] == 2
    assert result["impacts"]["tower_colocation_host"].get(
        "tower_colocation_host:E:C1"
    ) is None
    #: 命中 tier 之后的 tier 一个候选都没评估。
    assert "candidate_site" not in result["impacts"]

    #: 已选中的候选绝不重复进入 probe。
    service._what_if = fake_what_if
    probed.clear()
    again = service._probe_positive_remaining_candidates(
        actions, {"tower_colocation_host:D:C1"}, "tower_colocation_host",
        {}, {}, [], 1, [], [], None, None, None, None, None, None, None, [],
    )
    assert again["action_id"] == "tower_colocation_host:E:C1"
    assert probed == ["tower_colocation_host:C:C1", "tower_colocation_host:E:C1"]


def test_probe_positive_judgement_reuses_the_formal_rankers(tmp_path):
    """⑤b probe 的"正向"判据必须是正式排名器本身（含连续服务通道门禁）。"""

    workflow = configured(tmp_path, devices=[device("C1", "group-a")], candidates=[])
    service = workflow.corridor_site_planning_service
    assert _continuous_service_acceptance({}, None, None, None).get("acceptable") is not True

    action = {"action_id": "A", "eligibility": {"status": "eligible"}}
    discrete_positive = {
        "action_id": "A", "status": "eligible",
        "confirmed_requirement_unit_volume_gain": 1.0, "regressions": [],
        "continuous_service_gain": None,
    }
    zero_gain = dict(discrete_positive, confirmed_requirement_unit_volume_gain=0.0)
    ineligible = dict(discrete_positive, status="unknown")
    regressed = dict(discrete_positive, regressions=[{"reason": "regression"}])
    crossing = dict(
        zero_gain,
        continuous_service_gain={
            "threshold_available": True, "threshold_crossed": True,
            "longest_outage_reduction_m": 60.0, "total_projection_reduction_m": 60.0,
        },
    )
    partial = dict(
        zero_gain,
        continuous_service_gain={
            "threshold_available": True, "threshold_crossed": False,
            "longest_outage_reduction_m": 60.0, "total_projection_reduction_m": 60.0,
        },
    )

    assert service._positive_gain_candidate(action, discrete_positive, False) is True
    assert service._positive_gain_candidate(action, zero_gain, False) is False
    assert service._positive_gain_candidate(action, ineligible, False) is False
    assert service._positive_gain_candidate(action, regressed, False) is False
    #: 连续服务通道只在"当前投影态明确不合格"时启用；跨阈值才算正向候选。
    assert service._positive_gain_candidate(action, crossing, False) is False
    assert service._positive_gain_candidate(action, crossing, True) is True
    assert service._positive_gain_candidate(action, partial, True) is False


@pytest.mark.parametrize("cap", [1, 2, 3, 6])
def test_cap_never_changes_the_selected_sequence_or_the_targets(tmp_path, cap):
    """回归面：任何 cap 下选中序列都必须是"无上限运行"的前缀，且目标集合不变。"""

    def build(root):
        return configured(
            root, redundancy=2,
            devices=[device("C1", "group-a"), device("C2", "group-b")],
            candidates=[candidate("S1")],
        )

    uncapped = evaluate_with_cap(build(tmp_path / "uncapped"), 6)
    capped = evaluate_with_cap(build(tmp_path / f"cap{cap}"), cap)
    assert_three_state_mutual_exclusion(capped)
    selected = capped["selected_actions"]
    assert len(selected) <= cap
    assert [item["action_id"] for item in selected] == [
        item["action_id"] for item in uncapped["selected_actions"][:len(selected)]
    ]
    assert capped["targets"] == uncapped["targets"]
