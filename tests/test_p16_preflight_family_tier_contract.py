"""Round29-K1：P16 preflight **审计计数口径**契约（all_tiers vs reuse tier）。

业务裁定：

* ``planner_family`` 是 **all_tiers** 维度：一个 family 的计数跨越全部 reuse tier；
* ``reuse_class`` 是 **tier** 维度：一个 tier 的计数跨越全部 planner family；
* 两者**绝不可互相顶替**。真实权威项目（R0005）的 before 口径是：

  - all_tiers family：``omnidirectional_site=750``（其中 **tier 内分解**
    ``tower_colocation_host`` 746 + ``existing_cns_facility`` 2 + ``candidate_site`` 2）、
    ``endpoint_integrity_monitor=2``、``directional_radar=535``，total ``1287``；
  - Round29-K after：``omnidirectional_site=750``、``endpoint_integrity_monitor=2``、
    ``directional_radar=0``，total ``752``；
  - 共塔 tier 自身跨 family（before 746 个 omnidirectional + 535 个 Radar），
    因此 tier 计数既不是 746 也不是 750。

  即**真正的变化只有 Radar 535 → 0**。把 ``tower_colocation_host`` 的 746 写成全局
  omnidirectional before 数是错误口径，本文件负责阻止它回归。

本文件只读统计，不运行最终权威链，也不修改任何权威项目状态。
"""

from __future__ import annotations

from copy import deepcopy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cns_planner.application.corridor_site_planning_service import (  # noqa: E402
    _action_planner_family, _candidate_shape_statistics,
)
from cns_planner.domain.cns_service_registry import planner_family_for  # noqa: E402
from cns_planner.domain.site_planning import REUSE_TIERS  # noqa: E402

from test_p16_prefilter_clearance_contract import (  # noqa: E402
    _build_state, OMNIDIRECTIONAL_SITE_FAMILY,
)


COMMUNICATION = "C:communication"
ENDPOINT_MONITOR = "N:navigation_integrity_monitoring"
RADAR = "S:radar_noncooperative"

ENDPOINT_MONITOR_FAMILY = planner_family_for(ENDPOINT_MONITOR)
RADAR_FAMILY = planner_family_for(RADAR)


def _action(service_key, reuse_class):
    return {"service_key": service_key, "reuse_class": reuse_class}


def _authoritative_before_actions():
    """真实权威项目 before 的候选形状（Round29-I/J 实测：1287 个 canonical 候选）。

    Radar candidate panel 的 reuse tier 与普通站点动作一样是共塔宿主
    （``tower_colocation_host``），因此 tier 计数**跨 family**。
    """

    actions = [
        _action(COMMUNICATION, "tower_colocation_host") for _ in range(746)
    ]
    actions += [_action(COMMUNICATION, "existing_cns_facility") for _ in range(2)]
    actions += [_action(COMMUNICATION, "candidate_site") for _ in range(2)]
    actions += [_action(ENDPOINT_MONITOR, "existing_shared_site") for _ in range(2)]
    actions += [_action(RADAR, "tower_colocation_host") for _ in range(535)]
    return actions


def _authoritative_after_actions():
    """Round29-K after 的候选形状（Radar candidate panels 已撤销）。"""

    return [
        item for item in _authoritative_before_actions()
        if item["service_key"] != RADAR
    ]


# ---------------------------------------------------------------------------
# 1. 两个维度必须分开报：family 跨 tier、tier 跨 family
# ---------------------------------------------------------------------------

def test_family_and_tier_dimensions_are_never_substituted():
    shape = _candidate_shape_statistics(_authoritative_before_actions())

    assert shape["all_tiers_candidate_total"] == 1287
    assert shape["all_tiers_planner_family_counts"] == {
        OMNIDIRECTIONAL_SITE_FAMILY: 750,
        ENDPOINT_MONITOR_FAMILY: 2,
        RADAR_FAMILY: 535,
    }
    by_reuse = shape["all_tiers_family_by_reuse_class"]
    #: omnidirectional family 的 750 个动作分布在三个 reuse tier 上：
    #: 746（共塔）+ 2（已有 CNS 设施）+ 2（候选站址）。
    assert by_reuse["tower_colocation_host"][OMNIDIRECTIONAL_SITE_FAMILY] == 746
    assert by_reuse["existing_cns_facility"][OMNIDIRECTIONAL_SITE_FAMILY] == 2
    assert by_reuse["candidate_site"][OMNIDIRECTIONAL_SITE_FAMILY] == 2
    assert 746 + 2 + 2 == shape["all_tiers_planner_family_counts"][
        OMNIDIRECTIONAL_SITE_FAMILY
    ] == 750
    #: tier 维度**跨 family**：同一个共塔 tier 还含 535 个 Radar 候选，
    #: 因此 1281（tier）既不是 746（tier 内的 omnidirectional）也不是 750（family）。
    assert shape["all_tiers_reuse_class_counts"]["tower_colocation_host"] == 746 + 535
    assert by_reuse["tower_colocation_host"][RADAR_FAMILY] == 535
    assert shape["all_tiers_reuse_class_counts"]["existing_cns_facility"] == 2
    assert shape["all_tiers_reuse_class_counts"]["candidate_site"] == 2
    assert shape["all_tiers_reuse_class_counts"]["existing_shared_site"] == 2
    #: 两个维度都是**完备划分**：各自求和都必须回到同一个 total（口径自洽）。
    assert sum(shape["all_tiers_reuse_class_counts"].values()) == 1287
    assert sum(shape["all_tiers_planner_family_counts"].values()) == 1287
    assert shape["semantics"].startswith("planner_family_is_an_all_tiers_dimension")


def test_after_shape_differs_from_before_only_by_radar():
    before = _candidate_shape_statistics(_authoritative_before_actions())
    after = _candidate_shape_statistics(_authoritative_after_actions())

    assert after["all_tiers_candidate_total"] == 752
    assert after["all_tiers_planner_family_counts"] == {
        OMNIDIRECTIONAL_SITE_FAMILY: 750,
        ENDPOINT_MONITOR_FAMILY: 2,
    }
    #: 唯一变化是 Radar 535 → 0；omnidirectional / endpoint 逐字不变。
    assert before["all_tiers_planner_family_counts"][RADAR_FAMILY] == 535
    assert RADAR_FAMILY not in after["all_tiers_planner_family_counts"]
    for family in (OMNIDIRECTIONAL_SITE_FAMILY, ENDPOINT_MONITOR_FAMILY):
        assert (
            after["all_tiers_planner_family_counts"][family]
            == before["all_tiers_planner_family_counts"][family]
        )
    #: 撤销 Radar 后，共塔 tier 从 1281 回落到 746 —— 746 是 **tier** 数，
    #: 绝不等于 omnidirectional 的 all_tiers 数 750。
    assert before["all_tiers_reuse_class_counts"]["tower_colocation_host"] == 1281
    assert after["all_tiers_reuse_class_counts"]["tower_colocation_host"] == 746
    assert after["all_tiers_reuse_class_counts"]["tower_colocation_host"] != (
        after["all_tiers_planner_family_counts"][OMNIDIRECTIONAL_SITE_FAMILY]
    )


def test_tier_shape_is_a_strict_subset_of_the_all_tiers_shape():
    """tier 维度统计永远是 all_tiers 统计的子集（只筛选，不放大）。"""

    actions = _authoritative_before_actions()
    all_tiers = _candidate_shape_statistics(actions)
    tower = _candidate_shape_statistics([
        item for item in actions if item["reuse_class"] == "tower_colocation_host"
    ])

    assert tower["all_tiers_candidate_total"] == 1281
    assert tower["all_tiers_reuse_class_counts"] == {"tower_colocation_host": 1281}
    assert tower["all_tiers_planner_family_counts"] == {
        OMNIDIRECTIONAL_SITE_FAMILY: 746,
        RADAR_FAMILY: 535,
    }
    for family, count in tower["all_tiers_planner_family_counts"].items():
        assert count <= all_tiers["all_tiers_planner_family_counts"][family]


def test_planner_family_resolution_still_uses_the_canonical_registry():
    """统计口径依赖的 family 解析必须仍来自 canonical registry（无第二份映射）。"""

    assert _action_planner_family(_action(COMMUNICATION, "candidate_site")) == (
        OMNIDIRECTIONAL_SITE_FAMILY
    )
    assert _action_planner_family(_action(ENDPOINT_MONITOR, "existing_shared_site")) == (
        ENDPOINT_MONITOR_FAMILY
    )
    assert _action_planner_family(_action(RADAR, "tower_colocation_host")) == RADAR_FAMILY
    #: 显式字段优先（canonical action 自带 planner_family 时原样使用）。
    assert _action_planner_family({
        "service_key": COMMUNICATION, "planner_family": "explicit_family",
    }) == "explicit_family"


# ---------------------------------------------------------------------------
# 2. 真实 preflight 入口同样分开报两个口径（只读）
# ---------------------------------------------------------------------------

def test_preflight_reports_both_counting_dimensions_read_only(tmp_path):
    workflow = _build_state(tmp_path)
    state = workflow.state
    before = deepcopy(state)

    tier_index = REUSE_TIERS.index("candidate_site")
    report = workflow.corridor_site_planning_service.preflight_first_reuse_iteration(
        tier_index=tier_index,
    )

    assert report["candidate_shape_all_tiers"]["all_tiers_candidate_total"] == (
        report["candidate_total"]
    )
    assert report["candidate_family_counts"] == (
        report["candidate_shape_all_tiers"]["all_tiers_planner_family_counts"]
    )
    assert report["candidate_reuse_class_counts"] == (
        report["candidate_shape_all_tiers"]["all_tiers_reuse_class_counts"]
    )
    assert report["tier_candidate_shape"]["tier"] == "candidate_site"
    assert report["tier_candidate_shape"]["all_tiers_candidate_total"] == (
        report["tier_candidate_total"]
    )
    assert report["candidate_counting_semantics"].startswith(
        "all_tiers_planner_family_counts_counts_every_reuse_tier"
    )
    #: 每个 tier 的 family 计数之和必须等于 all_tiers 的 family 计数（口径自洽）。
    summed = {}
    for entry in report["tier_statistics"]:
        for family, count in entry["planner_family_counts"].items():
            summed[family] = summed.get(family, 0) + count
    assert summed == report["candidate_shape_all_tiers"][
        "all_tiers_planner_family_counts"
    ]
    #: 只读：绝不写 state、绝不 save。
    assert state == before
    assert workflow.session.pending_save is False
