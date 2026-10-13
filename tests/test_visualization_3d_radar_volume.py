"""Round37-T3D-2B：Radar 方向性三维覆盖的定向回归（全部离线）。

覆盖任务明确要求的门禁：

1. 无选定面阵 ⇒ ``blocked`` 且**不生成任何几何**；
2. 有真实面阵 ⇒ 方位角/型号/斜距与权威参数**逐项一致**；
3. Radar-I 与 Radar-II 的斜距**不混用**（120–3000 / 200–5000）；
4. 水平方位窗口跨越 0°/360° 时窗口仍然正确；
5. 每个顶点的 ``斜距``、``水平距离 = 斜距×cos(仰角)``、``相对高度 = 斜距×sin(仰角)``
   三项在 EPSG:32651 米制帧内逐点闭合；
6. 未确认 / 未知安装高程一律 fail-closed，且**不以 0 m 代替**；
7. 响应只输出 EGM2008 正高，且明示禁止 EGM96/EGM84 替代；
8. Radar 与通信/RID 的几何语义严格分离（全向 / 方向性、``not_a_rid_footprint``）；
9. LOS 恒为 ``unknown``（``terrain_los_not_persisted``），绝不冒充"已确认无遮挡"；
10. 真实模式与 ``device_spec_preview`` **完全隔离**；
11. 三种几何（物理楔体 / 航路局部显示裁剪 / LOS 可见区域）不得混用；
12. 端点是只读的：``read_only`` / ``writes_performed = 0`` / ``algorithms_invoked = []``
    且不改变 ProjectState（revision 与状态哈希不变）。
"""

from __future__ import annotations

import hashlib
import json
import math
from types import SimpleNamespace

import pytest

from cns_planner.api.router import ApiRouter
from cns_planner.application.visualization_3d_communication_volume import metric_transformer
from cns_planner.application.visualization_3d_radar_volume import (
    CROSS_SECTION_ELEVATIONS_DEG,
    FORBIDDEN_GEOMETRY,
    azimuth_window_deg,
    build_radar_volumes,
    cross_section_table,
)
from cns_planner.application.visualization_3d_service import Visualization3DService
from cns_planner.application.visualization_3d_sites import radar_units
from cns_planner.domain.radar_surveillance_layout import (
    AZIMUTH_HALF_WIDTH_DEG,
    ELEVATION_CENTER_DEG,
    ELEVATION_MAX_DEG,
    ELEVATION_MIN_DEG,
    radar_geometry_parameters,
)

METRIC_CRS = "EPSG:32651"
TOWER_ID = "330903908000000137"
TOWER_LON, TOWER_LAT = 122.258063, 29.844537
TOWER_TOP_M = 21.275962829148927


def tower_profile(*, confirmed=False, tower_top=TOWER_TOP_M, longitude=TOWER_LON,
                  latitude=TOWER_LAT, status="resolved"):
    """``tower_obstacle_profiles.items[]`` 的真实形态（含确认语义）。"""

    return {
        "tower_id": TOWER_ID,
        "name": "普陀测试塔",
        "longitude": longitude,
        "latitude": latitude,
        "terrain_elevation_m": 6.88,
        "terrain_vertical_reference": "egm2008_orthometric",
        "building_height_m": 5.395962715148926,
        "tower_structure_height_m": 9.0,
        "tower_top_orthometric_m": tower_top,
        "tower_top_status": "resolved" if tower_top is not None else "unknown",
        "confirmed": confirmed,
        "base_type": "rooftop",
        "status": status,
        "reason": None if tower_top is not None else "站址细分类型无法判定地面/楼面",
    }


def panel(*, radar_type="radar_i", azimuth=49.381777852157725, panel_id=None,
          tower_id=TOWER_ID):
    return {
        "panel_id": panel_id or f"P-{tower_id}-{radar_type}-{azimuth:.6f}",
        "tower_id": tower_id,
        "radar_type": radar_type,
        "azimuth_deg": azimuth,
        "panel_half_width_deg": AZIMUTH_HALF_WIDTH_DEG,
    }


def state_with_layout(*, panels=(panel(),), towers=None, item_status="stale",
                      stale_reason="layered_validation_evidence_outdated",
                      container_status="not_calculated", selected_tower_ids=None,
                      with_container=True):
    profile = towers if towers is not None else tower_profile()
    index = {str(row["tower_id"]): row for row in ([profile] if profile else [])}
    state = {
        "revision": 468,
        "tower_obstacle_profiles": {
            "status": "passed", "count": len(index), "items": index,
        },
    }
    if with_container:
        state["radar_surveillance_layout"] = {
            "status": container_status,
            "count": 1 if panels else 0,
            "collection_id": "radar_surveillance_layout",
            "proposal_only": True,
            "model_scope": "geometric_initial_radar_layout",
            "algorithm_version": "1.3",
            "items": [{
                "route_id": "R0005",
                "status": item_status,
                "stale_reason": stale_reason,
                "proposal_only": True,
                "algorithm_version": "1.3",
                "demo_preview_only": False,
                "operationally_adopted": False,
                "altitude_layer_id": "ALT-100",
                "altitude_m": 100.0,
                "selected_panel_count": len(panels),
                "selected_tower_ids": sorted(
                    selected_tower_ids
                    if selected_tower_ids is not None
                    else {str(row["tower_id"]) for row in panels}),
                "selected_panels": [dict(row) for row in panels],
                "radar_origin": {"semantics": "radar_origin_egm2008_equals_tower_top_orthometric_m"},
            }],
        }
    return state


def service_for(state):
    return Visualization3DService(state, data=SimpleNamespace(paths={}))


def router_for(service, monkeypatch):
    from cns_planner.application import visualization_3d_service as module

    monkeypatch.setattr(module, "service_for", lambda context: service)
    router = ApiRouter.__new__(ApiRouter)
    router._visualization_3d = ApiRouter._visualization_3d.__get__(router, ApiRouter)
    return router, SimpleNamespace(_visualization_3d_service=service)


def radar_payload(router, context, query=None):
    response = router._visualization_3d(
        context, "/api/visualization-3d/radar-volumes", query or {}, {})
    assert response.status == 200
    return response, response.data


def vertex_metric(point, to_metric, origin_z):
    """把 Cesium 顶点 ``[lon, lat, H_egm2008]`` 反算回米制帧的 (horizontal, dz)。"""

    x, y = to_metric(point[0], point[1])
    return x, y, float(point[2]) - float(origin_z)


# ---------------------------------------------------------------- 1/2/3 参数


def test_no_selected_panels_is_blocked_without_any_geometry(monkeypatch):
    state = state_with_layout(panels=())
    router, context = router_for(service_for(state), monkeypatch)
    _response, payload = radar_payload(router, context)

    assert payload["panels_total"] == 0
    assert payload["geometry_count"] == 0
    assert payload["status"] == "blocked"
    assert payload["volumes"] == []
    assert payload["required_evidence"]  # 缺什么必须逐条列清楚
    assert any("selected_panels" in row for row in payload["required_evidence"])
    # 绝不自动补默认方向，也绝不生成全向覆盖圆。
    assert payload["omnidirectional"] is False
    assert "omnidirectional_full_circle" in payload["forbidden_geometry"]
    assert payload["project_layout"]["selected_panel_count"] == 0


def test_real_panel_geometry_matches_authoritative_parameters(monkeypatch):
    state = state_with_layout(towers=tower_profile(confirmed=True))
    router, context = router_for(service_for(state), monkeypatch)
    _response, payload = radar_payload(router, context)

    assert payload["panels_total"] == 1
    volume = payload["volumes"][0]
    parameters = radar_geometry_parameters("radar_i")
    assert volume["azimuth_deg"] == pytest.approx(panel()["azimuth_deg"], abs=1e-12)
    assert volume["radar_type"] == "radar_i"
    assert volume["layout_item_status"] == "stale"
    assert volume["layout_stale_reason"] == "layered_validation_evidence_outdated"
    window = volume["azimuth_window"]
    assert window["half_width_deg"] == AZIMUTH_HALF_WIDTH_DEG
    assert window["span_deg"] == pytest.approx(90.0)
    assert window["center_deg"] == pytest.approx(panel()["azimuth_deg"], abs=1e-12)
    geometry = volume["geometry"]
    assert geometry["azimuth_center_deg"] == pytest.approx(panel()["azimuth_deg"], abs=1e-12)
    assert geometry["elevation_min_deg"] == ELEVATION_MIN_DEG
    assert geometry["elevation_max_deg"] == ELEVATION_MAX_DEG
    assert geometry["elevation_center_deg"] == ELEVATION_CENTER_DEG
    assert geometry["min_slant_range_m"] == parameters["min_slant_range_m"] == 120.0
    assert geometry["max_slant_range_m"] == parameters["max_slant_range_m"] == 3000.0
    # 斜距不是水平半径：只有 elevation=0 的截面才等于 3000 m。
    assert geometry["horizontal_radius_at_elevation_zero_m"] == 3000.0
    assert geometry["horizontal_radius_is_not_the_slant_range"] is True
    assert geometry["two_dimensional_disk_is_not_the_volume"] is True


def test_radar_i_and_radar_ii_slant_ranges_are_not_mixed():
    i = radar_geometry_parameters("radar_i")
    ii = radar_geometry_parameters("radar_ii")
    assert (i["min_slant_range_m"], i["max_slant_range_m"]) == (120.0, 3000.0)
    assert (ii["min_slant_range_m"], ii["max_slant_range_m"]) == (200.0, 5000.0)

    confirmed = tower_profile(confirmed=True)
    panels = [
        {**panel(radar_type="radar_i", azimuth=10.0, panel_id="P-I"),
         "parameter_source": "test"},
        {**panel(radar_type="radar_ii", azimuth=200.0, panel_id="P-II"),
         "parameter_source": "test"},
    ]
    units = radar_units(state_with_layout(panels=tuple(panels), towers=confirmed))
    result = build_radar_volumes(units, metric_crs=METRIC_CRS)
    by_id = {volume["panel_id"]: volume for volume in result["volumes"]}
    assert by_id["P-I"]["geometry"]["max_slant_range_m"] == 3000.0
    assert by_id["P-II"]["geometry"]["max_slant_range_m"] == 5000.0
    assert by_id["P-II"]["radar_ii_explicitly_declared"] is True
    assert by_id["P-II"]["radar_ii_is_not_a_formal_plan_escalation"] is True
    assert "radar_ii_explicitly_declared" not in by_id["P-I"]


def test_unknown_radar_type_is_blocked():
    confirmed = tower_profile(confirmed=True)
    units = radar_units(state_with_layout(
        panels=({**panel(radar_type="radar_ix"), "parameter_source": "t"},),
        towers=confirmed))
    result = build_radar_volumes(units, metric_crs=METRIC_CRS)
    geometry = result["volumes"][0]["geometry"]
    assert geometry["status"] == "blocked"
    assert geometry["reason"] == "radar_type_not_declared_or_unknown"
    assert geometry["required_evidence"]
    assert geometry["outer_surface_rings"] == []


# ------------------------------------------------------- 4/5 方位与仰角几何


def test_azimuth_window_wraps_across_north():
    start, end, wrapped = azimuth_window_deg(350.0, 45.0)
    assert (round(start, 6), round(end, 6)) == (305.0, 35.0)
    assert wrapped is True
    start, end, wrapped = azimuth_window_deg(170.0, 45.0)
    assert (round(start, 6), round(end, 6)) == (125.0, 215.0)
    assert wrapped is False

    confirmed = tower_profile(confirmed=True)
    units = radar_units(state_with_layout(
        panels=({**panel(azimuth=350.0), "parameter_source": "t"},), towers=confirmed))
    volume = build_radar_volumes(units, metric_crs=METRIC_CRS)["volumes"][0]
    geometry = volume["geometry"]
    assert geometry["status"] == "available"
    assert geometry["azimuth_start_deg"] == pytest.approx(305.0, abs=1e-9)
    assert geometry["azimuth_end_deg"] == pytest.approx(35.0, abs=1e-9)
    assert geometry["azimuth_wrapped_across_north"] is True
    # 顶点必须真的围绕 350° 展开（含跨越正北的那一段）：
    # 全部落在 [305°,360°) ∪ [0°,35°]，且两段都出现，中间方向(90°–300°)绝不出现。
    to_metric, _ = metric_transformer(METRIC_CRS)
    center_x, center_y = to_metric(TOWER_LON, TOWER_LAT)
    azimuths = []
    for ring in geometry["outer_surface_rings"]:
        for point in ring:
            x, y = to_metric(point[0], point[1])
            azimuths.append(math.degrees(math.atan2(x - center_x, y - center_y)) % 360.0)
    assert all((value >= 305.0 - 1e-6) or (value <= 35.0 + 1e-6) for value in azimuths)
    assert any(value >= 305.0 for value in azimuths)
    assert any(value <= 35.0 for value in azimuths)
    assert not any(90.0 < value < 300.0 for value in azimuths)


def test_vertices_obey_slant_range_and_elevation_law():
    confirmed = tower_profile(confirmed=True)
    units = radar_units(state_with_layout(
        panels=({**panel(azimuth=49.381777852157725), "parameter_source": "t"},),
        towers=confirmed))
    volume = build_radar_volumes(units, metric_crs=METRIC_CRS)["volumes"][0]
    geometry = volume["geometry"]
    assert geometry["status"] == "available"
    to_metric, _ = metric_transformer(METRIC_CRS)
    origin_z = volume["service_origin_egm2008_m"]

    checked = 0
    for slant, rings in ((geometry["max_slant_range_m"], geometry["outer_surface_rings"]),
                         (geometry["min_slant_range_m"], geometry["inner_surface_rings"])):
        for ring in rings:
            for point in ring:
                x, y, dz = vertex_metric(point, to_metric, origin_z)
                horizontal = math.hypot(x - to_metric(TOWER_LON, TOWER_LAT)[0],
                                        y - to_metric(TOWER_LON, TOWER_LAT)[1])
                slant_actual = math.hypot(horizontal, dz)
                assert slant_actual == pytest.approx(slant, abs=1e-3)
                elevation = math.degrees(math.atan2(dz, horizontal))
                # 顶点按 9 位小数（≈1e-5 m）序列化，反投影后仰角有 ~1e-4° 的往返
                # 误差；因此这里的容差取 1e-3°，仍远小于任何几何尺度。
                assert -1e-3 <= elevation <= ELEVATION_MAX_DEG + 1e-3
                # 水平距离 = 斜距 × cos(仰角)；相对高度 = 斜距 × sin(仰角)
                assert horizontal == pytest.approx(
                    slant * math.cos(math.radians(elevation)), abs=1e-3)
                assert dz == pytest.approx(
                    slant * math.sin(math.radians(elevation)), abs=1e-3)
                checked += 1
    assert checked > 0

    # 截面表逐行核对：elevation=0 才是唯一等于斜距的水平半径。
    rows = cross_section_table(radar_geometry_parameters("radar_i"))
    assert [row["elevation_deg"] for row in rows] == list(CROSS_SECTION_ELEVATIONS_DEG)
    zero = rows[0]
    assert zero["max_horizontal_distance_m"] == 3000.0
    assert zero["max_relative_height_m"] == 0.0
    top = rows[-1]
    assert top["max_horizontal_distance_m"] == pytest.approx(
        3000.0 * math.cos(math.radians(45.0)), abs=1e-6)
    assert top["max_relative_height_m"] == pytest.approx(
        3000.0 * math.sin(math.radians(45.0)), abs=1e-6)
    for row in rows:
        assert row["max_horizontal_distance_m"] < 3000.0 + 1e-9
        if row["elevation_deg"] > 0:
            assert row["max_horizontal_distance_m"] < 3000.0


# -------------------------------------------------------- 6/7 fail-closed


def test_unconfirmed_installation_height_is_fail_closed(monkeypatch):
    state = state_with_layout(towers=tower_profile(confirmed=False))
    router, context = router_for(service_for(state), monkeypatch)
    _response, payload = radar_payload(router, context)

    assert payload["panels_total"] == 1
    assert payload["geometry_count"] == 0
    assert payload["status"] == "blocked"
    volume = payload["volumes"][0]
    geometry = volume["geometry"]
    assert geometry["status"] == "blocked"
    assert geometry["reason"] == "tower_top_orthometric_m_not_confirmed"
    assert geometry["required_evidence"]
    assert geometry["outer_surface_rings"] == []
    assert volume["origin_confirmed"] is False
    # 未确认高度仍然**如实回显**，但绝不被当作已确认几何的基础。
    assert volume["service_origin_egm2008_m"] == pytest.approx(TOWER_TOP_M)


def test_unknown_installation_height_never_falls_back_to_zero(monkeypatch):
    state = state_with_layout(towers=tower_profile(tower_top=None, status="unresolved"))
    router, context = router_for(service_for(state), monkeypatch)
    _response, payload = radar_payload(router, context)

    volume = payload["volumes"][0]
    assert volume["service_origin_egm2008_m"] is None
    assert volume["origin_status"] == "missing"
    assert volume["geometry"]["status"] == "blocked"
    assert volume["geometry"]["outer_surface_rings"] == []
    assert payload["geometry_count"] == 0


def test_missing_tower_profile_is_fail_closed(monkeypatch):
    state = state_with_layout(towers=None)
    state["tower_obstacle_profiles"]["items"] = {}
    router, context = router_for(service_for(state), monkeypatch)
    _response, payload = radar_payload(router, context)

    volume = payload["volumes"][0]
    assert volume["tower_profile_found"] is False
    assert volume["geometry"]["status"] == "blocked"
    assert volume["geometry"]["required_evidence"]


def test_origin_is_egm2008_orthometric_and_substitutes_are_forbidden(monkeypatch):
    service = service_for(state_with_layout(towers=tower_profile(confirmed=True)))
    payload = service.radar_volumes({})
    assert payload["vertical_reference"]["canonical"] == "egm2008_orthometric"
    assert payload["vertical_reference"]["canonical_is_ellipsoidal"] is False
    conversion = payload["vertical_reference"]["ellipsoidal_conversion"]
    assert conversion["method"] == "egm2008_geoid_undulation"
    assert "EGM96" in conversion["substitutes_forbidden"]
    assert "EGM84" in conversion["substitutes_forbidden"]
    volume = payload["volumes"][0]
    assert volume["vertical_reference"] == "egm2008_orthometric"
    # 顶点第三坐标就是 EGM2008 正高（椭球高转换由前端按 geoid N 逐点完成）。
    rings = volume["geometry"]["outer_surface_rings"]
    heights = {point[2] for ring in rings for point in ring}
    assert max(heights) > TOWER_TOP_M  # 仰角上限处必然高于雷达原点
    assert all(height >= TOWER_TOP_M - 1e-6 for height in heights)


# -------------------------------------------------- 8/9/11 语义严格分离


def test_radar_geometry_semantics_are_separated_from_rid_and_communication(monkeypatch):
    router, context = router_for(service_for(state_with_layout()), monkeypatch)
    _response, payload = radar_payload(router, context)

    assert payload["omnidirectional"] is False
    assert payload["not_a_rid_footprint"] is True
    assert "omnidirectional_full_circle" in payload["forbidden_geometry"]
    assert "hemisphere_360" in payload["forbidden_geometry"]
    assert set(FORBIDDEN_GEOMETRY) <= set(payload["forbidden_geometry"])
    volume = payload["volumes"][0]
    assert volume["not_a_rid_footprint"] is True
    assert volume["omnidirectional"] is False
    assert volume["target_cooperation"] == "non_cooperative"
    assert volume["technology"] == "radar"
    assert volume["geometry"]["omnidirectional"] is False
    assert volume["geometry"]["horizontal_coverage_deg"] == pytest.approx(90.0)
    # 三种几何必须显式区分，且本服务**不做**航路局部显示裁剪。
    separation = payload["geometry_semantics_separation"]
    assert separation["physical_maximum_coverage_wedge"] is True
    assert separation["route_local_display_clipping_applied"] is False
    assert separation["route_local_display_geometry_used"] is False
    assert separation["los_confirmed_visible_region_used"] is False


def test_los_stays_unknown_and_is_never_rendered_as_clear(monkeypatch):
    router, context = router_for(service_for(state_with_layout()), monkeypatch)
    _response, payload = radar_payload(router, context)

    los = payload["los"]
    assert los["status"] == "unknown"
    assert los["reason"] == "terrain_los_not_persisted"
    assert los["visible_region_available"] is False
    assert los["terrain_los_evaluated"] is False
    volume = payload["volumes"][0]
    assert volume["los"]["status"] == "unknown"
    assert volume["los"]["visible_region_available"] is False
    # 未评估项必须非空（空数组会被读成"没有未评估因素"）。
    not_evaluated = payload["not_evaluated"]
    assert not_evaluated
    assert not_evaluated["terrain_LOS"] == "not_evaluated"
    assert not_evaluated["radar_equation"] == "not_evaluated"
    assert volume["not_evaluated"]["terrain_LOS"] == "not_evaluated"


# ------------------------------------------------- 10 预览与真实模式隔离


def test_project_layout_and_device_spec_preview_are_isolated(monkeypatch):
    state = state_with_layout(towers=tower_profile(confirmed=False))
    router, context = router_for(service_for(state), monkeypatch)

    _response, project = radar_payload(router, context)
    assert project["mode"] == "project_layout"
    assert project["device_spec_preview"] is False
    assert "disclaimer" not in project
    assert "isolation" not in project
    assert "preview_tower_options" not in project

    # 未确认原点 + 未显式承认 ⇒ 预览也必须 blocked（不渲染确定几何）。
    _response, gated = radar_payload(router, context, {
        "mode": "device_spec_preview", "tower_id": TOWER_ID,
        "radar_type": "radar_i", "azimuth_deg": "30",
    })
    assert gated["device_spec_preview"] is True
    assert gated["geometry_count"] == 0
    assert gated["status"] == "blocked"
    assert gated["reason"] == "preview_origin_not_confirmed_needs_explicit_acknowledgement"

    # 用户显式承认"使用未确认塔顶派生正高（仅示意）"后才渲染，且逐项标注。
    _response, preview = radar_payload(router, context, {
        "mode": "device_spec_preview", "tower_id": TOWER_ID,
        "radar_type": "radar_i", "azimuth_deg": "30",
        "preview_origin_mode": "tower_top_orthometric_preview",
    })
    assert preview["device_spec_preview"] is True
    assert preview["status"] == "passed"
    assert preview["geometry_count"] == 1
    assert preview["disclaimer"] == "设备参数示意，非已采纳布设，非已确认探测覆盖"
    isolation = preview["isolation"]
    assert isolation["isolated_from_project_results"] is True
    assert isolation["written_to_project_state"] is False
    assert isolation["enters_planning_results"] is False
    assert isolation["enters_service_coverage_statistics"] is False
    assert isolation["enters_los_acceptance"] is False
    assert isolation["selected_panels_derived"] is False
    assert preview["preview_origin_confirmed"] is False
    assert preview["preview_origin_acknowledged_by_user"] is True
    volume = preview["volumes"][0]
    assert volume["geometry"]["status"] == "available"
    assert volume["geometry"]["origin_confirmed"] is False
    assert volume["geometry"]["preview_origin_not_confirmed"] is True
    assert volume["panel_id"].startswith("PREVIEW-")
    # 预览**绝不**进入真实模式的数据面。
    assert preview["panels_total"] == 0
    assert "project_layout" not in preview
    assert all(row.get("panel_id", "").startswith("PREVIEW-")
               for row in preview["volumes"])


def test_preview_requires_explicit_user_selection(monkeypatch):
    router, context = router_for(service_for(state_with_layout()), monkeypatch)
    _response, payload = radar_payload(router, context, {"mode": "device_spec_preview"})
    assert payload["status"] == "not_calculated"
    assert payload["selection_required"] is True
    assert payload["geometry_count"] == 0
    assert payload["volumes"] == []
    assert payload["required_evidence"]
    assert payload["preview_tower_options"]
    option = payload["preview_tower_options"][0]
    assert option["tower_id"] == TOWER_ID
    assert option["tower_top_orthometric_m"] == pytest.approx(TOWER_TOP_M)
    assert option["origin_status"] == "unconfirmed"


def test_preview_rejects_unknown_tower_and_radar_type(monkeypatch):
    router, context = router_for(service_for(state_with_layout()), monkeypatch)
    _response, payload = radar_payload(router, context, {
        "mode": "device_spec_preview", "tower_id": "NO-SUCH-TOWER",
        "radar_type": "radar_i", "azimuth_deg": "10",
    })
    assert payload["status"] == "blocked"
    assert payload["reason"] == "preview_tower_not_found"

    _response, payload = radar_payload(router, context, {
        "mode": "device_spec_preview", "tower_id": TOWER_ID,
        "radar_type": "radar_x", "azimuth_deg": "10",
    })
    assert payload["status"] == "not_calculated"
    assert payload["selection_required"] is True
    assert any("radar_type" in row for row in payload["required_evidence"])


# ------------------------------------------------- 12 只读：状态与哈希不变


def _state_fingerprint(state):
    return hashlib.sha256(json.dumps(
        state, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def test_radar_endpoints_are_read_only_and_do_not_mutate_state(monkeypatch):
    state = state_with_layout(towers=tower_profile(confirmed=True))
    before = _state_fingerprint(state)
    revision_before = state["revision"]
    service = service_for(state)
    router, context = router_for(service, monkeypatch)

    for query in ({}, {"azimuth_segments": "6", "elevation_segments": "2"},
                  {"mode": "device_spec_preview", "tower_id": TOWER_ID,
                   "radar_type": "radar_i", "azimuth_deg": "120",
                   "preview_origin_mode": "tower_top_orthometric_preview"}):
        _response, payload = radar_payload(router, context, query)
        assert payload["read_only"] is True
        assert payload["writes_performed"] == 0
        assert payload["algorithms_invoked"] == []
        assert payload["revision"] == revision_before
        assert payload["operational_authorization"] is False
        assert payload["strict_safety_validation_passed"] is False
    assert state["revision"] == revision_before
    assert _state_fingerprint(state) == before


def test_radar_endpoint_supports_etag_and_304(monkeypatch):
    router, context = router_for(service_for(state_with_layout()), monkeypatch)
    first = router._visualization_3d(
        context, "/api/visualization-3d/radar-volumes", {}, {})
    etag = first.data["etag"]
    assert etag.startswith("vis3dradarvolumesv1-")
    second = router._visualization_3d(
        context, "/api/visualization-3d/radar-volumes", {},
        {"If-None-Match": f'"{etag}"'})
    assert second.status == 304
    assert second.data == b""
    assert second.headers["ETag"] == f'"{etag}"'
    # 304 的缓存策略必须与 200 完全一致（否则浏览器会丢弃已缓存的 ETag 条目）。
    assert second.headers["Cache-Control"] == first.headers["Cache-Control"] == "no-store"


def test_radar_container_status_is_disclosed_without_optimism(monkeypatch):
    state = state_with_layout(item_status="stale",
                              stale_reason="radar_surveillance_algorithm_semantics_changed",
                              container_status="stale")
    router, context = router_for(service_for(state), monkeypatch)
    _response, payload = radar_payload(router, context)
    layout = payload["project_layout"]
    assert layout["container_status"] == "stale"
    assert layout["item_stale"] is True
    assert layout["item_statuses"] == ["stale"]
    assert layout["item_stale_reasons"] == [
        "radar_surveillance_algorithm_semantics_changed"]
    assert layout["proposal_only"] is True
    assert layout["operationally_adopted"] is False
    assert layout["operationally_adopted_field_conflicts_with_proposal_only"] is False
    assert any("已采纳布设" in row for row in layout["must_not_be_read_as"])


def test_operationally_adopted_field_is_disclosed_without_being_read_as_adoption(monkeypatch):
    """权威状态里 ``operationally_adopted=true`` 与 ``proposal_only=true`` 并存。

    本服务只**原样回显**并显式标注这一张力；当前性一律以 item ``status`` 为准，
    绝不因为该布尔字段为真就把 stale 的 proposal 读成"当前有效采纳"。
    """

    state = state_with_layout()
    state["radar_surveillance_layout"]["items"][0]["operationally_adopted"] = True
    router, context = router_for(service_for(state), monkeypatch)
    _response, payload = radar_payload(router, context)

    layout = payload["project_layout"]
    assert layout["operationally_adopted"] is True
    assert layout["proposal_only"] is True
    assert layout["operationally_adopted_field_conflicts_with_proposal_only"] is True
    assert "不**" in layout["operationally_adopted_note"] or "不" in layout[
        "operationally_adopted_note"]
    # 当前性仍然如实来自 item status（stale），不被该字段改写。
    assert layout["item_statuses"] == ["stale"]
    assert layout["item_stale"] is True
    assert payload["status"] == "blocked"
