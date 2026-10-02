"""BUG-SHOT-008 回归：Radar 固定高度层必须**数据驱动**，且求解必须有硬时间预算。

修复前的两个 production 缺陷：

A. **架构硬编码 ALT-080**：``domain/radar_surveillance_layout.py`` 的
   ``FIXED_ALTITUDE_LAYER_ID = "ALT-080"`` / ``FIXED_ALTITUDE_M = 80.0`` 被
   ``algorithms/radar_layout/v1.py``、``geometry.py``、``application/app_context.py``
   与 readiness 直接消费，因此 Radar **无法**跟随项目当前正式运行航路的高度层
   （本轮真实案例是 R0005 + ALT-100）。
B. **超时无效**：``solver_time_limit_s`` 只作为单次 MILP 的 time_limit 下发，
   而 refinement 最多再解 3 轮、每轮独立复核整条航路，实测 >45 min 不返回。

本测试锁定修复后的语义：
1. 高度层解析优先级（策略 → 权威运行航路 → 候选 → 工程默认）与"ALT-080 默认行为
   逐字不变"的兼容性；
2. 换层后语义指纹必然变化（旧 layout 因此 stale），ALT-080 的四项分量不变；
3. ``solve_layout`` 在整体预算内返回，并如实报告 ``time_budget.exhausted``，
   绝不无限等待。
"""

import math
import time

import pytest

from cns_planner.algorithms.radar_layout.v1 import (
    RADAR_DEFAULT_TOTAL_TIME_BUDGET_S, parameters_block, required_count_for,
    solve_layout,
)
from cns_planner.domain.radar_surveillance_layout import (
    ALTITUDE_LAYER_SOURCE_DEFAULT, ALTITUDE_LAYER_SOURCE_POLICY,
    ALTITUDE_LAYER_SOURCE_ROUTE, FIXED_ALTITUDE_LAYER_ID, FIXED_ALTITUDE_M,
    altitude_layer_semantics, resolve_radar_altitude_layer_id,
    route_sample_height_semantics, semantics_fingerprint,
)

LAYERS = {
    "ALT-080": {"altitude_layer_id": "ALT-080", "nominal_altitude_m": 80.0, "confirmed": True},
    "ALT-100": {"altitude_layer_id": "ALT-100", "nominal_altitude_m": 100.0, "confirmed": True},
}


def state_with(*, policy_layer=None, route_layer=None, layers=None, candidate_layer=None,
               policy_explicit=True, policy_status="confirmed",
               operating_layer_status="confirmed"):
    state = {"spatial_3d": {"altitude_layers": list((layers or LAYERS).values())}}
    if policy_layer is not None:
        state["radar_surveillance_policy"] = {
            "fixed_altitude_layer_id": policy_layer,
            #: 策略里的 ALT-080 是**软件默认值**；只有"用户显式配置过该字段"且
            #: "整份策略 confirmed"两个条件同时成立，才算用户选择。
            "fixed_altitude_layer_id_explicitly_configured": bool(policy_explicit),
            "status": policy_status,
            "confirmed": policy_status == "confirmed",
        }
    if route_layer is not None:
        state["operational_routes"] = [
            {"route_id": "R0005", "status": "passed"}
        ]
        #: 运行航路的高度层分配实际存放在 spatial_3d.route_operating_layers。
        state["spatial_3d"]["route_operating_layers"] = [{
            "route_id": "R0005", "altitude_layer_id": route_layer,
            "status": operating_layer_status,
            "confirmed": operating_layer_status == "confirmed",
            "operating_mode": "fixed_cruise_layer",
        }]
    if candidate_layer is not None:
        state["current_layered_candidate"] = {"altitude_layer_id": candidate_layer}
    return state


# ---- A1 解析优先级 ---------------------------------------------------------------


def test_operational_route_layer_wins_over_the_alt_080_engineering_default():
    """策略里是**默认值** ALT-080 时，权威运行航路的 ALT-100 必须生效。"""

    state = state_with(
        policy_layer="ALT-080", route_layer="ALT-100", policy_explicit=False,
    )
    layer_id, altitude_m, source, layer, ambiguous = resolve_radar_altitude_layer_id(state)
    assert layer_id == "ALT-100"
    assert altitude_m == pytest.approx(100.0)
    assert source == ALTITUDE_LAYER_SOURCE_ROUTE
    assert layer["nominal_altitude_m"] == pytest.approx(100.0)
    assert ambiguous is False


def test_policy_layer_wins_and_reports_its_source():
    state = state_with(policy_layer="ALT-100", route_layer="ALT-080")
    layer_id, altitude_m, source, _, _ = resolve_radar_altitude_layer_id(state)
    assert (layer_id, altitude_m, source) == ("ALT-100", pytest.approx(100.0), ALTITUDE_LAYER_SOURCE_POLICY)


def test_default_policy_value_never_shadows_the_authoritative_route_layer():
    """策略里的 ``fixed_altitude_layer_id`` 默认值就是 ALT-080。

    若把它当成"用户显式配置"，Radar 会永远停在 ALT-080 —— 这正是 BUG-SHOT-008 的
    真实成因（R0005 运行航路是 ALT-100，Radar 却按 80 m 求解）。因此默认值必须让位给
    当前权威运行航路，只有用户**真的配置过**该字段时才由策略优先。
    """

    not_explicit = state_with(
        policy_layer="ALT-080", route_layer="ALT-100", policy_explicit=False,
    )
    layer_id, altitude_m, source, _, _ = resolve_radar_altitude_layer_id(not_explicit)
    assert (layer_id, altitude_m, source) == (
        "ALT-100", pytest.approx(100.0), ALTITUDE_LAYER_SOURCE_ROUTE,
    )

    explicit = state_with(
        policy_layer="ALT-080", route_layer="ALT-100", policy_explicit=True,
    )
    layer_id, altitude_m, source, _, _ = resolve_radar_altitude_layer_id(explicit)
    assert (layer_id, altitude_m, source) == (
        "ALT-080", pytest.approx(80.0), ALTITUDE_LAYER_SOURCE_POLICY,
    ), "用户显式确认 ALT-080 时必须尊重用户配置"


def test_unconfirmed_policy_never_shadows_the_route_layer():
    """未确认的策略只是软件基线：即使旧项目残留 explicit 标记也不得遮蔽运行航路。"""

    pending = state_with(
        policy_layer="ALT-080", route_layer="ALT-100",
        policy_explicit=True, policy_status="pending_confirmation",
    )
    layer_id, altitude_m, source, _, _ = resolve_radar_altitude_layer_id(pending)
    assert (layer_id, altitude_m, source) == (
        "ALT-100", pytest.approx(100.0), ALTITUDE_LAYER_SOURCE_ROUTE,
    )


def test_explicit_layer_wins_over_everything():
    state = state_with(policy_layer="ALT-100", route_layer="ALT-100")
    layer_id, _, _, _, _ = resolve_radar_altitude_layer_id(state, explicit="ALT-080")
    assert layer_id == "ALT-080"


def test_engineering_default_remains_alt_080_with_identical_semantics():
    """没有任何数据线索时，默认行为必须与修复前**逐字相同**（ALT-080 / 80 m）。"""

    layer_id, altitude_m, source, layer, _ = resolve_radar_altitude_layer_id({"spatial_3d": {}})
    assert layer_id == FIXED_ALTITUDE_LAYER_ID == "ALT-080"
    assert altitude_m == pytest.approx(FIXED_ALTITUDE_M)
    assert source == ALTITUDE_LAYER_SOURCE_DEFAULT
    assert layer is None
    # 兼容性：默认层下的语义字符串与修复前完全一致。
    assert altitude_layer_semantics("ALT-080") == "fixed_alt_080_egm2008"
    assert route_sample_height_semantics("ALT-080") == (
        "fixed_alt_080_egm2008_constant_for_every_sample"
    )


def test_explicit_missing_layer_never_silently_falls_back_to_80m():
    """显式要求的高度层不在目录里时，绝不默默套用 80 m。"""

    layer_id, altitude_m, source, layer, _ = resolve_radar_altitude_layer_id(
        {"spatial_3d": {}}, explicit="ALT-150",
    )
    assert layer_id == "ALT-150"
    assert altitude_m is None, "目录里没有 ALT-150 ⇒ 必须如实返回 None，而不是 80 m"
    assert layer is None


def test_mixed_operational_routes_are_reported_as_ambiguous():
    state = {
        "spatial_3d": {
            "altitude_layers": list(LAYERS.values()),
            "route_operating_layers": [
                {"route_id": "A", "altitude_layer_id": "ALT-080",
                 "status": "confirmed", "confirmed": True},
                {"route_id": "B", "altitude_layer_id": "ALT-100",
                 "status": "confirmed", "confirmed": True},
            ],
        },
        "operational_routes": [
            {"route_id": "A", "status": "passed"},
            {"route_id": "B", "status": "passed"},
        ],
    }
    layer_id, _, source, _, ambiguous = resolve_radar_altitude_layer_id(state)
    assert layer_id == "ALT-080", "不一致时取第一条已发布航路"
    assert source == ALTITUDE_LAYER_SOURCE_ROUTE
    assert ambiguous is True, "歧义必须如实上报，不得静默"


# ---- A2 语义指纹随高度层变化 -------------------------------------------------------


def test_semantics_fingerprint_changes_with_the_altitude_layer():
    base = semantics_fingerprint("ALT-080")
    lifted = semantics_fingerprint("ALT-100")
    assert base["route_altitude_semantics"] == "fixed_alt_080_egm2008"
    assert lifted["route_altitude_semantics"] == "fixed_alt_100_egm2008"
    assert base != lifted, "换高度层后指纹必须不同，否则旧 layout 不会被判 stale"
    # 四项非高度分量必须逐字不变。
    for key in (
        "vertical_delta_semantics", "radar_origin_semantics",
        "land_mask_semantics", "geometry_version",
    ):
        assert base[key] == lifted[key], key


def test_parameters_block_defaults_are_unchanged_and_follow_explicit_layer():
    default_block = parameters_block()
    assert default_block["fixed_altitude_layer_id"] == "ALT-080"
    assert default_block["fixed_altitude_m"] == pytest.approx(80.0)
    assert default_block["route_altitude_semantics"] == "fixed_alt_080_egm2008"

    lifted = parameters_block(fixed_altitude_m=100.0, altitude_layer_id="ALT-100")
    assert lifted["fixed_altitude_layer_id"] == "ALT-100"
    assert lifted["fixed_altitude_m"] == pytest.approx(100.0)
    assert lifted["route_altitude_semantics"] == "fixed_alt_100_egm2008"
    assert lifted["route_sample_height_semantics"] == (
        "fixed_alt_100_egm2008_constant_for_every_sample"
    )


# ---- B 求解时间预算 ---------------------------------------------------------------


def _samples(count=6, *, spacing=500.0, altitude=100.0):
    return [
        {
            "sample_index": index, "sample_id": f"S{index:04d}",
            "metric": [index * spacing, 0.0], "egm2008_m": float(altitude),
            "distance_along_route_m": index * spacing, "surface_class": "sea",
            "required_distinct_site_count": required_count_for("sea"),
        }
        for index in range(count)
    ]


def _towers():
    return [
        {
            "tower_id": "T1", "name": "塔T1", "metric": [0.0, 0.0],
            "origin_egm2008_m": 50.0, "longitude": None, "latitude": None,
        },
        {
            "tower_id": "T2", "name": "塔T2", "metric": [2500.0, 0.0],
            "origin_egm2008_m": 50.0, "longitude": None, "latitude": None,
        },
    ]


def test_solve_layout_reports_a_time_budget_and_honours_it():
    result = solve_layout(
        towers=_towers(), samples=_samples(),
        options={"time_limit_s": 0.001, "fixed_altitude_m": 100.0, "altitude_layer_id": "ALT-100"},
    )
    budget = result["time_budget"]
    assert budget["budget_s"] == pytest.approx(0.001)
    assert budget["budget_source"] == "policy.solver_time_limit_s"
    assert budget["exhausted"] is True, "预算必须真的生效（而不是只写在参数里）"
    assert budget["remaining_s"] == pytest.approx(0.0)
    assert budget["returns_best_feasible_so_far"] is True
    assert "预算" in result["message"], "超预算必须出现在可见结论里"


def test_solve_layout_uses_the_software_default_budget_when_policy_is_silent():
    result = solve_layout(towers=_towers(), samples=_samples())
    budget = result["time_budget"]
    assert budget["budget_s"] == pytest.approx(RADAR_DEFAULT_TOTAL_TIME_BUDGET_S)
    assert budget["budget_source"] == "software_default_total_budget"
    assert "整体求解预算" in (
        result["message"] if budget["exhausted"] else "整体求解预算"
    )


def test_solve_layout_returns_within_the_budget_for_a_small_problem():
    started = time.monotonic()
    result = solve_layout(
        towers=_towers(), samples=_samples(), options={"time_limit_s": 30.0},
    )
    elapsed = time.monotonic() - started
    assert elapsed < 30.0
    assert result["status"] in (
        "optimal_coverage", "refinement_incomplete", "search_incomplete",
        "infeasible", "solver_unavailable",
    )
    assert result["time_budget"]["exhausted"] is False


def test_selected_panels_carry_the_resolved_altitude_plane():
    """水平交截半径必须按**本次高度层**的平面计算，而不是固定 80 m。"""

    result = solve_layout(
        towers=_towers(), samples=_samples(count=3, spacing=600.0),
        options={"time_limit_s": 30.0, "fixed_altitude_m": 100.0, "altitude_layer_id": "ALT-100"},
    )
    for panel in result["selected_panels"]:
        assert panel["altitude_plane_egm2008_m"] == pytest.approx(100.0)
        assert panel["altitude_layer_id"] == "ALT-100"
        plane = panel["altitude_plane_geometry"]
        if plane and plane.get("horizontal_outer_radius_m") is not None:
            origin = panel["radar_origin_egm2008_m"]
            dz = 100.0 - origin
            assert plane["dz_m"] == pytest.approx(dz)
            if dz >= 0:
                assert plane["horizontal_outer_radius_m"] == pytest.approx(
                    math.sqrt(3000.0 ** 2 - dz ** 2), abs=1e-6,
                )
