"""P16 性能优化的**等价性**回归（Round 2.2）。

本轮两项优化都不得改变任何业务结论：

1. **``prepared_cells`` 复用** —— P14 的走廊单元准备对同一份 grid / terrain / surface
   facts 是常量（实测 8008 单元约 6.4 s/次）。复用后结果必须逐字段相同，且确实复用了
   同一个对象（幂等记忆化字段被原地填充）；
2. **候选几何预筛** —— 只否决"新设备几何包络根本触及不到任何走廊体素"的候选。这类
   候选的 P14/P15 重算结果与 baseline 逐字段相同，因此 confirmed 增益必然为 0、既无
   regression 也无 unknown_regression；预筛**不得**否决可能产生增益的候选。

本文件用 ``tests/test_cns_corridor.py`` 的最小走廊 fixture，并用**完整 what-if 重算**
独立证明被预筛候选确实零影响。
"""

from copy import deepcopy
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_cns_corridor import (  # noqa: E402
    GRID, ROUTE, SPATIAL, aircraft, device, facilities, policy, requirement_set,
)

from cns_planner.algorithms.corridor.v1 import CNSServiceCorridorV1  # noqa: E402
from cns_planner.algorithms.corridor_gap.v1 import CNSCorridorGapAnalyzerV1  # noqa: E402
from cns_planner.application.corridor_site_planning_service import (  # noqa: E402
    _action_provider_envelope_m, _apply_cumulative_action, _build_prefilter_context,
    _prefilter_impact, rerun_corridor_chain,
)

TERRAIN = {"terrain": {"status": "passed", "cells": {}}}


def _state():
    return {
        "operational_routes": [deepcopy(ROUTE)],
        "spatial_3d": deepcopy(SPATIAL),
        "grid": deepcopy(GRID),
        "grid_attributes": deepcopy(TERRAIN),
        "required_cns": requirement_set(),
        "aircraft_profiles": {"status": "passed", "items": [aircraft()]},
        "selected_aircraft_profile_id": "A1",
        "existing_cns_facilities": facilities(),
        "device_catalog": {"status": "passed", "items": [device()]},
        "cns_corridor_policy": policy(),
        "cns_corridor_assessment": {"parameters": {}},
        "cns_corridor_gap_assessment": {"parameters": {}},
        "cns_planning_objectives": {},
        "radar_surveillance_layout": {},
    }


def _model():
    return CNSServiceCorridorV1()


def _analyzer():
    return CNSCorridorGapAnalyzerV1({})


def _action(coordinate, *, device_id="D1", service_key=None):
    return {
        "action_id": "A-FAR", "action_type": "add_device", "reuse_class": "tower_colocation_host",
        "site_id": "SITE-1", "coordinate": coordinate, "device_id": device_id,
        "device_service_key": service_key, "subsystem": "C", "planner_family": None,
        "eligibility": {"status": "eligible", "reasons": []},
        "vertical": {"service_origin_egm2008_m": 100.0, "confirmed": True},
        "distinct_site_id": "site-1",
    }


def _voxel_signature(assessment):
    """走廊里与几何判定有关的部分（**不含**指纹，指纹会随 facilities 变化）。"""

    return [
        (voxel.get("voxel_id"), voxel.get("subsystems"))
        for route in assessment.get("routes") or []
        for voxel in route.get("voxels") or []
    ]


# ---------------------------------------------------------------------------
# 1. prepared_cells 复用等价且真的被复用
# ---------------------------------------------------------------------------

def test_reused_prepared_cells_yield_identical_corridor():
    state = _state()
    model = _model()
    baseline = model.evaluate(
        [deepcopy(ROUTE)], deepcopy(SPATIAL), deepcopy(GRID), deepcopy(TERRAIN),
        requirement_set(), aircraft(), facilities(), {"status": "passed", "items": [device()]},
        policy(),
    )
    prepared = model.prepare_cells(state["grid"], state["grid_attributes"], None)
    assert prepared, "prepare_cells 必须返回走廊单元"
    assert all(item.get("area") is None for item in prepared), "准备阶段不得预先算面积"

    reused = model.evaluate(
        [deepcopy(ROUTE)], deepcopy(SPATIAL), deepcopy(GRID), deepcopy(TERRAIN),
        requirement_set(), aircraft(), facilities(), {"status": "passed", "items": [device()]},
        policy(), prepared_cells=prepared,
    )

    assert reused == baseline, "复用预计算单元必须与自行准备的结果逐字段相同"
    #: 幂等记忆化字段被原地填充 ⇒ 证明确实复用了**同一个**对象（没有重新准备）。
    assert any(item.get("area") is not None for item in prepared)


# ---------------------------------------------------------------------------
# 2. 预筛只否决"几何上不可能触及走廊"的候选，且完整重算证明零影响
# ---------------------------------------------------------------------------

def test_prefilter_skips_far_action_and_full_recompute_proves_zero_impact():
    state = _state()
    model, analyzer = _model(), _analyzer()
    prepared = model.prepare_cells(state["grid"], state["grid_attributes"], None)
    prefilter = _build_prefilter_context(state, prepared)
    assert prefilter is not None, "本 fixture 的静态事实足以做安全判定"

    far = _action([0.5, 0.5])            # 距航路约 55 km，远超 40 m 设备包络
    verdict = _prefilter_impact(far, 1, prefilter)
    assert verdict is not None, "远到不可能触及走廊的候选必须被预筛否决"
    assert verdict["status"] == "ineligible"
    assert verdict["confirmed_gain"] == 0.0
    assert verdict["confirmed_targets"] == [] and verdict["unknown_targets"] == []
    assert verdict["evidence_status"] == "prefiltered_no_corridor_interaction"
    assert verdict["prefilter"]["applied"] is True
    assert verdict["prefilter"]["nearest_route_distance_m"] > verdict["prefilter"]["required_clearance_m"]

    #: 独立证据：**完整重算**（不预筛）后走廊体素逐字段不变 ⇒ 增益必然为 0。
    before_corridor, before_gap = rerun_corridor_chain(
        state, model, analyzer, facilities(), prepared_cells=prepared,
    )
    after_corridor, after_gap = rerun_corridor_chain(
        state, model, analyzer, _apply_cumulative_action(facilities(), far),
        prepared_cells=prepared,
    )
    assert _voxel_signature(after_corridor) == _voxel_signature(before_corridor)
    assert [
        (voxel["voxel_id"], voxel["subsystems"])
        for route in after_gap.get("routes") or [] for voxel in route.get("voxels") or []
    ] == [
        (voxel["voxel_id"], voxel["subsystems"])
        for route in before_gap.get("routes") or [] for voxel in route.get("voxels") or []
    ], "被预筛候选的 P15 结果必须与 baseline 逐字段相同"


def test_prefilter_does_not_skip_action_inside_the_corridor():
    state = _state()
    model = _model()
    prepared = model.prepare_cells(state["grid"], state["grid_attributes"], None)
    prefilter = _build_prefilter_context(state, prepared)

    near = _action([0.0, 0.0])           # 就在航路上
    assert _prefilter_impact(near, 1, prefilter) is None, "可能产生增益的候选绝不能被预筛否决"


def test_prefilter_is_disabled_when_facts_are_insufficient():
    state = _state()
    model = _model()
    prepared = model.prepare_cells(state["grid"], state["grid_attributes"], None)

    #: 没有走廊半宽事实 ⇒ 无法安全判定 ⇒ 完全不预筛。
    no_spec = deepcopy(state)
    no_spec["cns_corridor_policy"] = {"routes": {}}
    assert _build_prefilter_context(no_spec, prepared) is None

    #: 没有走廊单元 ⇒ 无法安全判定。
    assert _build_prefilter_context(state, []) is None

    #: 没有可用航路 ⇒ 无法安全判定。
    no_route = deepcopy(state)
    no_route["operational_routes"] = []
    assert _build_prefilter_context(no_route, prepared) is None

    #: 设备几何未知（device_id 不在目录里、也没有 service policy）⇒ 不预筛。
    unknown_device = _action([0.5, 0.5], device_id="NOT-IN-CATALOG")
    assert _action_provider_envelope_m(unknown_device, state) is None
    prefilter = _build_prefilter_context(state, prepared)
    assert _prefilter_impact(unknown_device, 1, prefilter) is None

    #: 挂在既有设施上的动作（复用既有站址）坐标不取自 action ⇒ 保守不预筛。
    attached = {**_action([0.5, 0.5]), "facility_id": "F1"}
    assert _prefilter_impact(attached, 1, prefilter) is None

    #: 没有坐标 ⇒ 不预筛。
    assert _prefilter_impact(_action(None), 1, prefilter) is None


def test_prefilter_uses_device_geometry_envelope_not_service_label():
    state = _state()
    far = _action([0.5, 0.5], service_key="C:communication")
    #: 设备目录给出 40 m 包络；service policy 给出 4000 m 上界。取**较大者**（保守）。
    assert _action_provider_envelope_m(far, state) == 4000.0
