"""Round 29-Q —— P17 非合作监视能力限制的 route-aware 收口回归。

本轮语义收口（窄 hotfix，不重跑任何上游）：

* ``radar_surveillance_layout`` 是 **collection**（顶层 ``status`` 只描述整批评估，
  权威项目实测为 ``pending_confirmation``），真正的逐航路 canonical 裁决在 ``items[]``；
* 只有「已证明的 managed physical gap」（``status=infeasible`` **且**
  ``gap_classification=confirmed_gap`` **且** ``managed_physical_gap is True`` **且**
  ``solver.infeasibility_proven is True``）才登记为**补充威胁能力限制**；
* 其余（``proposal_ready`` / ``search_incomplete`` / ``refinement_incomplete`` /
  ``unresolved`` / ``solver_error`` / ``solver_unavailable`` / ``not_ready`` /
  ``stale`` / ``missing`` / 无证明）一律保持 ``unknown``；
* 限制必须 **route-aware**：R0003 的历史 ``stale`` item 绝不污染 R0005；
* Radar 的 ``managed_physical_gap`` 与 P17 连续服务的 ``managed_gap_count``
  是**两个不同概念**，能力限制绝不伪造连续事件来把计数抬高。

覆盖任务 H 的 15 项专项要求。
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from cns_planner.algorithms.continuous_service.v1 import ContinuousServiceAcceptabilityV1
from cns_planner.application.result_currentness import (
    apply_algorithm_semantics_stale, effective_result_status,
    RESULT_ALGORITHM_SEMANTICS,
)
from cns_planner.domain.cns_continuous_service import (
    CONTINUOUS_SERVICE_ALGORITHM_ID, CONTINUOUS_SERVICE_ALGORITHM_VERSION,
    NONCOOPERATIVE_LIMITATION_DISCLOSURE, empty_continuous_service_acceptability,
    limitation_disclosure_lines, noncooperative_limitations,
    proven_managed_physical_radar_gap, radar_layout_item_for_route,
)
from test_round26_post_plan_cns import (
    _evaluate, _p15, _subsystem, _surveillance_device_catalog, _surveillance_route,
)

MODEL = ContinuousServiceAcceptabilityV1()

R0005_PROVEN_ITEM = {
    "route_id": "R0005",
    "algorithm_id": "radar_surveillance_layout",
    "algorithm_version": "1.3",
    "status": "infeasible",
    "altitude_layer_id": "ALT-100",
    "gap_reason": "independent_site_count_limited",
    "gap_classification": "confirmed_gap",
    "managed_physical_gap": True,
    "selected_panels": [],
    "solver": {
        "name": "scipy.optimize.milp", "library": "HiGHS", "stage": "radar_i_only",
        "status": "infeasible", "infeasibility_proven": True,
    },
}

#: 权威 collection 里 R0003 的历史结论：V1.1、stale、7 块面板、**没有**任何证明字段。
R0003_STALE_ITEM = {
    "route_id": "R0003",
    "algorithm_id": "radar_surveillance_layout",
    "algorithm_version": "1.1",
    "status": "stale",
    "altitude_layer_id": "ALT-080",
    "stale_reason": "algorithm_semantics_changed",
    "selected_panels": [{"panel_id": f"P{index}"} for index in range(7)],
    "solver": {"status": "optimal", "optimality_proven": True, "infeasibility_proven": False},
}


def _layout(*items, status="pending_confirmation"):
    return {
        "status": status,
        "collection_id": "radar_surveillance_layout",
        "algorithm_id": "radar_surveillance_layout",
        "algorithm_version": "1.3",
        "items": list(items),
    }


def _proven(route_id="R0005", **overrides):
    item = deepcopy(R0005_PROVEN_ITEM)
    item["route_id"] = route_id
    item.update(overrides)
    return item


# ---------------------------------------------------------------------------
# 1–3：collection 形态、route 精确匹配、历史结果隔离
# ---------------------------------------------------------------------------


def test_pending_confirmation_collection_with_proven_route_item_is_a_limitation():
    """① collection 顶层 pending_confirmation + R0005 已证明 infeasible ⇒ 登记限制。"""

    layout = _layout(R0003_STALE_ITEM, R0005_PROVEN_ITEM)
    assert layout["status"] == "pending_confirmation"
    limitations = noncooperative_limitations(layout, route_id="R0005")
    assert len(limitations) == 1
    item = limitations[0]
    assert item["limitation_id"] == "noncooperative_surveillance_limitation"
    assert item["route_id"] == "R0005"
    assert item["layer"] == "noncooperative"
    assert item["status"] == "limitation"
    assert item["blocking_primary_threat"] is False
    assert item["capability"]


def test_stale_r0003_item_never_pollutes_r0005():
    """② R0003 的历史 stale/旧版本结论绝不污染 R0005。"""

    layout = _layout(R0003_STALE_ITEM, R0005_PROVEN_ITEM)
    #: R0003 自己没有 canonical 证明 ⇒ 它自己也没有限制。
    assert noncooperative_limitations(layout, route_id="R0003") == []
    #: 只含 R0003 的 collection（模拟"只有历史结果"）不可以给任何 route 造出限制。
    only_stale = _layout(R0003_STALE_ITEM)
    assert noncooperative_limitations(only_stale, route_id="R0005") == []
    #: R0005 的限制来源必须逐字指向 R0005 自己的 item。
    item = noncooperative_limitations(layout, route_id="R0005")[0]
    assert item["source"]["algorithm_version"] == "1.3"
    assert item["source"]["gap_reason"] == "independent_site_count_limited"


def test_route_id_must_match_exactly():
    """③ route_id 精确匹配：未知航路一律 fail-closed。"""

    layout = _layout(R0005_PROVEN_ITEM)
    assert noncooperative_limitations(layout, route_id="R0003") == []
    assert noncooperative_limitations(layout, route_id="R0005 ") == []
    assert noncooperative_limitations(layout, route_id="") == []
    assert noncooperative_limitations(layout, route_id=None) == []  # 顶层非 infeasible
    assert radar_layout_item_for_route(layout, "R0005") is R0005_PROVEN_ITEM
    assert radar_layout_item_for_route(layout, "R0003") is None


# ---------------------------------------------------------------------------
# 4–8：绝不升级为 limitation 的状态
# ---------------------------------------------------------------------------


def test_proposal_ready_is_not_a_limitation():
    """④ proposal_ready（已提出方案）不是能力限制。"""

    item = _proven(status="proposal_ready")
    assert proven_managed_physical_radar_gap(item) is False
    assert noncooperative_limitations(_layout(item), route_id="R0005") == []


@pytest.mark.parametrize(
    "status", ["search_incomplete", "refinement_incomplete", "unresolved",
               "solver_error", "solver_unavailable", "not_ready", "stale", "missing"],
)
def test_incomplete_or_failed_states_are_never_upgraded(status):
    """⑤⑥：搜索/细化未完成、求解器错误、未就绪、stale、missing 一律保持 unknown。"""

    item = _proven(status=status)
    assert proven_managed_physical_radar_gap(item) is False
    assert noncooperative_limitations(_layout(item), route_id="R0005") == []


def test_infeasible_without_proof_is_not_a_limitation():
    """⑦ infeasible 但 solver.infeasibility_proven=false ⇒ 不是能力限制。"""

    item = _proven(solver={"status": "infeasible", "infeasibility_proven": False})
    assert proven_managed_physical_radar_gap(item) is False
    assert noncooperative_limitations(_layout(item), route_id="R0005") == []
    #: 连 solver 块整体缺失也必须 fail-closed。
    without_solver = _proven()
    without_solver.pop("solver")
    assert noncooperative_limitations(_layout(without_solver), route_id="R0005") == []


def test_infeasible_with_proof_but_not_managed_physical_gap_is_not_a_limitation():
    """⑧ infeasible + proof 但 managed_physical_gap=false ⇒ 不是能力限制。"""

    item = _proven(managed_physical_gap=False)
    assert proven_managed_physical_radar_gap(item) is False
    assert noncooperative_limitations(_layout(item), route_id="R0005") == []
    #: gap_classification 不是 confirmed_gap 同样不成立。
    for classification in (None, "possible_gap", "unconfirmed"):
        item = _proven(gap_classification=classification)
        assert noncooperative_limitations(_layout(item), route_id="R0005") == []


def test_confirmed_managed_gap_with_proof_is_a_limitation():
    """⑨ confirmed_gap + managed + proof ⇒ 能力限制。"""

    assert proven_managed_physical_radar_gap(R0005_PROVEN_ITEM) is True
    limitations = noncooperative_limitations(
        _layout(R0005_PROVEN_ITEM), route_id="R0005")
    assert len(limitations) == 1
    assert limitations[0]["must_disclose_in_report"] is True


# ---------------------------------------------------------------------------
# 10 / 14：来源转印与强制披露
# ---------------------------------------------------------------------------


def test_proof_source_fields_are_transcribed_verbatim():
    """⑩ gap_reason 等证明来源原样转印，不改写上游判定。"""

    limitations = noncooperative_limitations(
        _layout(R0005_PROVEN_ITEM), route_id="R0005")
    source = limitations[0]["source"]
    assert source["algorithm_id"] == "radar_surveillance_layout"
    assert source["algorithm_version"] == "1.3"
    assert source["source_status"] == "infeasible"
    assert source["solver_status"] == "infeasible"
    assert source["infeasibility_proven"] is True
    assert source["gap_reason"] == "independent_site_count_limited"
    assert source["gap_classification"] == "confirmed_gap"
    assert source["managed_physical_gap"] is True
    #: 兼容字段与 source 同源（不是第二个真值）。
    assert limitations[0]["source_status"] == source["source_status"]
    assert limitations[0]["solver_status"] == source["solver_status"]


def test_limitation_disclosure_lines_use_the_single_canonical_text():
    """⑭ 能力限制披露必须复用既有唯一文案，出现在 disclosure_lines 里。"""

    limitations = noncooperative_limitations(
        _layout(R0005_PROVEN_ITEM), route_id="R0005")
    lines = limitation_disclosure_lines(limitations)
    joined = "\n".join(lines)
    assert "能力限制" in joined
    assert NONCOOPERATIVE_LIMITATION_DISCLOSURE in joined
    #: 绝不出现"监视已完全满足"这类把限制说成通过的说法。
    #: （"不得把本方案表述为全覆盖或已满足"是**强制披露句本身**，因此不能据此断言。）
    assert "监视已完全满足" not in joined
    assert "非合作无人机补充监视能力因 Radar 布局不可行尚未闭合" in joined
    assert "不得把本方案表述为全覆盖或已满足" in joined


# ---------------------------------------------------------------------------
# 11–13：端到端 P17 总体语义
# ---------------------------------------------------------------------------


def _gap_route(*, rid_status="satisfied", radar_status=None, length_m=0.0):
    """构造一条 S 子系统：既带监视冗余证据，也可带超阈值的连续服务缺口。"""

    entry = _surveillance_route(rid_status=rid_status, radar_status=radar_status)
    if length_m:
        entry["causes"] = ["service_deficit"]
        entry["continuous_deficit_segments"] = [{
            "segment_id": "SEG-S", "route_id": "R0005", "causes": ["service_deficit"],
            "start_offset_m": 0.0, "end_offset_m": length_m, "length_m": length_m,
            "voxel_ids": ["V0"],
        }]
    return entry


def test_primary_unacceptable_plus_radar_limitation_stays_unacceptable():
    """⑪ primary（RID）真实 unacceptable + supplementary limitation ⇒ 总体仍 unacceptable。"""

    result = _evaluate(
        corridor_gap_assessment=_p15([{
            "route_id": "R0005", "route_length_m": 10000.0, "status": "failed",
            "subsystems": [
                _subsystem("C", causes=[], length_m=0.0),
                _subsystem("N", causes=[], length_m=0.0),
                _gap_route(rid_status="satisfied", radar_status="confirmed_deficit",
                           length_m=9904.0),
            ],
        }]),
        device_catalog=_surveillance_device_catalog(),
        radar_surveillance_layout=_layout(R0003_STALE_ITEM, R0005_PROVEN_ITEM),
    )
    assert result["status"] == "unacceptable"
    assert result["primary_threat_status"] == "unacceptable"
    assert result["supplementary_threat_status"] == "limitation"
    assert result["unacceptable_count"] >= 1
    route = result["routes"][0]
    assert route["supplementary_threat_status"] == "limitation"
    assert route["supplementary_threat_is_limitation"] is True
    assert any(
        item.get("limitation_id") == "noncooperative_surveillance_limitation"
        for item in result["limitations"]
    )
    #: 能力限制绝不把主要威胁的严重度拉低。
    assert route["unacceptable_subsystems"]
    assert any(
        "unacceptable" in str(item.get("status"))
        for item in route["subsystems"]
    )


def test_primary_satisfied_plus_radar_limitation_never_claims_full_coverage():
    """⑫ primary satisfied + supplementary limitation ⇒ 既有 Round 2.6 裁定不变。"""

    result = _evaluate(
        corridor_gap_assessment=_p15([{
            "route_id": "R0005", "route_length_m": 10000.0, "status": "failed",
            "subsystems": [
                _subsystem("C", causes=[], length_m=0.0),
                _subsystem("N", causes=[], length_m=0.0),
                _surveillance_route(rid_status="satisfied", radar_status="confirmed_deficit"),
            ],
        }]),
        device_catalog=_surveillance_device_catalog(),
        radar_surveillance_layout=_layout(R0003_STALE_ITEM, R0005_PROVEN_ITEM),
    )
    assert result["primary_threat_status"] == "satisfied"
    assert result["supplementary_threat_status"] == "limitation"
    limitations = result["limitations"]
    assert len(limitations) == 1
    assert limitations[0]["route_id"] == "R0005"
    assert limitations[0]["blocking_primary_threat"] is False
    joined = "\n".join(result["disclosure_lines"])
    assert NONCOOPERATIVE_LIMITATION_DISCLOSURE in joined
    assert "监视已完全满足" not in joined
    #: 补充威胁的限制绝不写成"非合作监视已满足"。
    assert result["supplementary_threat_status"] != "satisfied"


def test_capability_limitation_never_forges_a_managed_gap():
    """⑬⑯ Radar 能力限制绝不伪造连续服务 managed gap（两个概念严格分离）。"""

    result = _evaluate(
        corridor_gap_assessment=_p15([{
            "route_id": "R0005", "route_length_m": 10000.0, "status": "failed",
            "subsystems": [
                _subsystem("C", causes=[], length_m=0.0),
                _subsystem("N", causes=[], length_m=0.0),
                _surveillance_route(rid_status="satisfied", radar_status="confirmed_deficit"),
            ],
        }]),
        device_catalog=_surveillance_device_catalog(),
        radar_surveillance_layout=_layout(R0005_PROVEN_ITEM),
    )
    assert result["limitations"], "限制必须存在"
    assert result["managed_gap_count"] == 0
    assert all(not route.get("managed_gaps") for route in result["routes"])


# ---------------------------------------------------------------------------
# 15：currentness —— 旧 P17 2.0 / 2.1 必须 effective stale
# ---------------------------------------------------------------------------


def test_p17_algorithm_version_is_registered_and_bumped():
    assert CONTINUOUS_SERVICE_ALGORITHM_VERSION == "2.2"
    assert RESULT_ALGORITHM_SEMANTICS["continuous_service_acceptability"] == (
        CONTINUOUS_SERVICE_ALGORITHM_ID, CONTINUOUS_SERVICE_ALGORITHM_VERSION,
    )
    assert empty_continuous_service_acceptability()["algorithm_version"] == "2.2"


def test_old_p17_2_0_result_is_effective_stale():
    """⑮ 旧 P17 2.0 结果必须 effective stale，绝不继续充当 current。"""

    stored = {
        "status": "unacceptable",
        "algorithm_id": CONTINUOUS_SERVICE_ALGORITHM_ID,
        "algorithm_version": "2.0",
        "supplementary_threat_status": "unknown",
        "limitations": [],
    }
    projected = apply_algorithm_semantics_stale(
        stored, CONTINUOUS_SERVICE_ALGORITHM_ID, CONTINUOUS_SERVICE_ALGORITHM_VERSION)
    assert projected["status"] == "stale"
    assert projected["stale_reason"] == "algorithm_semantics_changed"
    assert projected["algorithm_semantics_stale"]["stored_algorithm_version"] == "2.0"
    assert projected["algorithm_semantics_stale"]["current_algorithm_version"] == "2.2"
    #: stored payload 原样保留（只有只读投影改变结论）。
    assert stored["status"] == "unacceptable"
    assert stored["algorithm_version"] == "2.0"

    state = {"continuous_service_acceptability": stored, "result_statuses": {}}
    assert effective_result_status(state, "continuous_service_acceptability") == "stale"

    #: Round 30-C2A.2/C2A.3 之前的 2.1 结果同样必须 effective stale。
    stored_2_1 = deepcopy(stored)
    stored_2_1["algorithm_version"] = "2.1"
    assert apply_algorithm_semantics_stale(
        stored_2_1, CONTINUOUS_SERVICE_ALGORITHM_ID, CONTINUOUS_SERVICE_ALGORITHM_VERSION,
    )["status"] == "stale"

    #: 已经是当前版本（2.2）的结果不受影响。
    current = deepcopy(stored)
    current["algorithm_version"] = "2.2"
    assert apply_algorithm_semantics_stale(
        current, CONTINUOUS_SERVICE_ALGORITHM_ID, CONTINUOUS_SERVICE_ALGORITHM_VERSION,
    ) is current
