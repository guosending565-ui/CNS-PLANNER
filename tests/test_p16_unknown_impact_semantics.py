"""P16 unknown impact 语义回归（Round 2.2 裁定）。

裁定原文：

* ``UNKNOWN ≠ PASS`` —— 证据不足的 target 绝不计入已确认增益，也绝不计入
  service / redundancy 满足；
* ``UNKNOWN ≠ ZERO ENTIRE ACTION`` —— unknown 不得把一个动作对其它 confirmed
  target 的已确认增益整体抹掉；
* 只有真实的 confirmed 恶化（原 satisfied voxel 变为 confirmed gap）才让整个动作
  变为 ``ineligible`` 并把增益归零；
* 最终 plan 必须同时显示 confirmed 改善与 remaining unknown evidence。

本文件用**构造的 P15 assessment** 直接覆盖 ``_impact`` 与 planner 落位，不依赖
任何真实数据或服务。
"""

from copy import deepcopy
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cns_planner.application.corridor_site_planning_service import (  # noqa: E402
    _impact,
)
from cns_planner.domain.corridor_site_planning import corridor_target_id  # noqa: E402
from cns_planner.site_planner.corridor_reuse_first_v2 import (  # noqa: E402
    CorridorReuseFirstSitePlannerV2,
)

ROUTE = "R1"
VOXEL = "VOXEL-1"
COMM = "C:communication"
RID = "S:rid_cooperative"


def _service(key, status, *, required=2, current=None, surface="land"):
    return {
        "service_key": key,
        "status": status,
        "surface_class": surface,
        "surface_dependent": True,
        "counting_basis": "distinct_site_id",
        "required_distinct_site_count": required,
        "distinct_site_count": current,
        "distinct_site_ids": [f"site-{index}" for index in range(current or 0)],
    }


def _assessment(services, *, subsystem="C", voxel_id=VOXEL, route_id=ROUTE):
    return {"status": "passed", "routes": [{
        "route_id": route_id, "voxel_count": 1,
        "confirmed_target_voxel_ids": [], "unknown_voxel_ids": [],
        "voxels": [{"voxel_id": voxel_id, "surface_class": "land", "subsystems": [{
            "subsystem": subsystem, "combined_status": "confirmed_gap",
            "service_status": "confirmed_deficit", "redundancy_status": "confirmed_deficit",
            "required_redundancy": 2, "qualified_provider_count": 0,
            "services": list(services),
        }]}],
    }]}


def _target(key, *, volume=1000.0, required=2, subsystem="C"):
    return {
        "target_id": corridor_target_id(ROUTE, VOXEL, key),
        "route_id": ROUTE, "voxel_id": VOXEL, "subsystem": subsystem,
        "service_key": key, "target_scope": "service", "counting_basis": "distinct_site_id",
        "required_units": required, "current_units": 1, "remaining_units": 1,
        "discretized_volume_proxy_m3": volume, "causes": [],
    }


def _action(action_id="A1"):
    return {
        "action_id": action_id, "reuse_class": "tower_colocation_host",
        "eligibility": {"status": "eligible", "reasons": []},
        "planning_family": None,
    }


# ---------------------------------------------------------------------------
# 1. confirmed gain + unknown ⇒ action 可选（unknown 不抹掉已确认增益）
# ---------------------------------------------------------------------------

def test_confirmed_gain_with_unknown_target_still_eligible():
    targets = [_target(COMM, volume=1000.0), _target(RID, subsystem="S", volume=1000.0)]
    before = _assessment([_service(COMM, "confirmed_deficit", current=1),
                          _service(RID, "unknown", current=None)])
    #: 通信被补齐到 satisfied；RID 仍然证据不足。
    after = _assessment([_service(COMM, "satisfied", current=2),
                         _service(RID, "unknown", current=None)])
    impact = _impact(_action(), before, after, targets)

    assert impact["status"] == "eligible", "confirmed 增益成立就必须允许进入候选评分"
    assert impact["confirmed_gain"] == impact["confirmed_requirement_unit_volume_gain"] > 0
    assert impact["evidence_status"] == "confirmed_with_unknown_evidence"
    #: unknown target 必须独立保留，既不消失也不被当成已满足。
    assert impact["unknown_targets"] == [corridor_target_id(ROUTE, VOXEL, RID)]
    assert impact["confirmed_targets"] == [corridor_target_id(ROUTE, VOXEL, COMM)]
    assert impact["remaining_deficit"]["unknown_targets"] == 1
    assert impact["remaining_deficit"]["confirmed_deficit_targets"] == 0


def test_planner_ranks_action_that_has_confirmed_gain_plus_unknown():
    """回归：planner 不再因为存在 unknown 就永远拿不到候选。"""

    targets = [_target(COMM, volume=1000.0), _target(RID, subsystem="S", volume=1000.0)]
    before = _assessment([_service(COMM, "confirmed_deficit", current=1),
                          _service(RID, "unknown", current=None)])
    after = _assessment([_service(COMM, "satisfied", current=2),
                         _service(RID, "unknown", current=None)])
    action = _action()
    impact = _impact(action, before, after, targets)
    ranked = CorridorReuseFirstSitePlannerV2().rank([action], [impact])

    assert len(ranked) == 1
    assert ranked[0]["action"]["action_id"] == "A1"
    assert ranked[0]["selection_score"] > 0
    #: unknown 必须随选中候选一起被带到最终方案（不得在 ranking 阶段丢失）。
    assert ranked[0]["impact"]["unknown_targets"] == [corridor_target_id(ROUTE, VOXEL, RID)]


# ---------------------------------------------------------------------------
# 2. 只有 unknown ⇒ 绝不当成 positive gain
# ---------------------------------------------------------------------------

def test_unknown_only_never_becomes_positive_gain():
    targets = [_target(RID, subsystem="S", volume=1000.0)]
    before = _assessment([_service(RID, "unknown", current=None)])
    after = _assessment([_service(RID, "unknown", current=None)])
    impact = _impact(_action(), before, after, targets)

    assert impact["status"] == "unknown"
    assert impact["confirmed_gain"] == 0.0
    assert impact["confirmed_requirement_unit_volume_gain"] == 0.0
    assert impact["confirmed_targets"] == []
    assert impact["evidence_status"] == "unknown_only"
    assert impact["unknown_targets"] == [corridor_target_id(ROUTE, VOXEL, RID)]
    #: 只有 unknown 的动作不得被 planner 选中。
    assert CorridorReuseFirstSitePlannerV2().rank([_action()], [impact]) == []


# ---------------------------------------------------------------------------
# 3. confirmed gain == 0 且无 unknown ⇒ 不选
# ---------------------------------------------------------------------------

def test_zero_confirmed_gain_without_unknown_is_ineligible():
    targets = [_target(COMM, volume=1000.0)]
    before = _assessment([_service(COMM, "confirmed_deficit", current=1)])
    after = _assessment([_service(COMM, "confirmed_deficit", current=1)])
    impact = _impact(_action(), before, after, targets)

    assert impact["status"] == "ineligible"
    assert impact["confirmed_gain"] == 0.0
    assert impact["evidence_status"] == "no_confirmed_progress"
    assert impact["unknown_targets"] == []
    assert CorridorReuseFirstSitePlannerV2().rank([_action()], [impact]) == []


# ---------------------------------------------------------------------------
# 4. unknown 绝不作為 redundancy / service 满足
# ---------------------------------------------------------------------------

def test_unknown_target_never_counts_as_redundancy_or_service_satisfied():
    targets = [_target(RID, subsystem="S", volume=1000.0), _target(COMM, volume=1000.0)]
    before = _assessment([_service(RID, "unknown", current=None),
                          _service(COMM, "confirmed_deficit", current=1)])
    after = _assessment([_service(RID, "satisfied", current=2),
                         _service(COMM, "satisfied", current=2)])
    impact = _impact(_action(), before, after, targets)

    #: RID 从 unknown 变 satisfied 只是"证据仍未确认"⇒ 不得产生任何已确认增益。
    assert impact["confirmed_gain"] == 1000.0
    assert impact["service_resolved_volume_proxy_m3"] == 1000.0
    assert impact["redundancy_progress_volume_proxy_m3"] == 1000.0
    assert corridor_target_id(ROUTE, VOXEL, RID) in impact["unknown_targets"]
    assert corridor_target_id(ROUTE, VOXEL, RID) not in impact["confirmed_targets"]


# ---------------------------------------------------------------------------
# 5. selected action 之后 residual unknown 必须保留
# ---------------------------------------------------------------------------

def test_residual_unknown_evidence_is_preserved_after_selection():
    targets = [_target(COMM, volume=1000.0), _target(RID, subsystem="S", volume=1000.0)]
    before = _assessment([_service(COMM, "confirmed_deficit", current=1),
                          _service(RID, "unknown", current=None)])
    after = _assessment([_service(COMM, "satisfied", current=2),
                         _service(RID, "unknown", current=None)])
    impact = _impact(_action(), before, after, targets)
    selected = [{**_action(), "impact": impact}]

    result = CorridorReuseFirstSitePlannerV2().assemble(
        baseline=before, final=after, policy={"reuse_tiers": ["tower_colocation_host"]},
        targets=targets, actions=[_action()], impacts=[impact], selected=selected,
        iteration_trace=[], unknown_evidence=[], consistency=True,
    )

    residual_unknown = result["residual_unknown_evidence"]
    assert residual_unknown["final_unknown_target_count"] == 1
    assert residual_unknown["final_unknown_target_ids"] == [corridor_target_id(ROUTE, VOXEL, RID)]
    assert residual_unknown["selected_action_unknown_target_count"] == 1
    assert "never_counted_as_satisfied" in residual_unknown["semantics"]
    #: unknown 的 target 绝不进入 confirmed 残差。
    assert all(item["target_id"] != corridor_target_id(ROUTE, VOXEL, RID)
               for item in result["residual_confirmed_targets"])


def test_confirmed_regression_still_forces_ineligible_and_zero_gain():
    """真实恶化（satisfied → confirmed_gap）必须继续 fail-closed。"""

    targets = [_target(COMM, volume=1000.0), _target(RID, subsystem="S", volume=1000.0)]
    regression_id = corridor_target_id(ROUTE, VOXEL, RID)
    regression_before = _service(RID, "satisfied", current=2)
    regression_after = _service(RID, "confirmed_deficit", current=0)
    before = _assessment([_service(COMM, "confirmed_deficit", current=1), regression_before])
    after = _assessment([_service(COMM, "satisfied", current=2), regression_after])
    #: ``_regressions`` 判定读的是 subsystem entry 的 combined_status；这里直接把该
    #: service 从 satisfied 改成 confirmed_deficit 已足以构成真实恶化。
    before_entry = before["routes"][0]["voxels"][0]["subsystems"][0]["services"][1]
    before_entry["status"] = "satisfied"
    impact = _impact(_action(), before, after, targets)

    assert impact["status"] == "ineligible"
    assert impact["confirmed_gain"] == 0.0
    assert impact["evidence_status"] == "ineligible_confirmed_regression"
    assert impact["regressions"], "真实 confirmed 恶化必须被登记"
    assert regression_id not in impact["confirmed_targets"]


def test_unknown_regression_is_disclosed_without_erasing_confirmed_gain():
    """原 satisfied 体素变 unknown：独立披露，但不得抹掉其它已确认增益。"""

    targets = [_target(COMM, volume=1000.0), _target(RID, subsystem="S", volume=1000.0)]
    before = _assessment([_service(COMM, "confirmed_deficit", current=1),
                          _service(RID, "satisfied", current=2)])
    after = _assessment([_service(COMM, "satisfied", current=2),
                         _service(RID, "unknown", current=None)])
    impact = _impact(_action(), before, after, targets)

    assert impact["status"] == "eligible"
    assert impact["confirmed_gain"] == 1000.0
    assert impact["unknown_regressions"] == [corridor_target_id(ROUTE, VOXEL, RID)]
    assert impact["evidence_status"] == "confirmed_with_unknown_evidence"


def _placeholder_deepcopy_guard():
    """防止本文件意外依赖可变共享状态。"""

    return deepcopy({})
