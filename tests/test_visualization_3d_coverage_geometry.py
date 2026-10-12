"""Round37-T3D-2A：通信三维包络 + RID 陆海非正圆 footprint 的几何回归（全部离线）。

关键判据（逐条对应任务要求）：

1. **三维斜距**：包络顶点到服务原点的**斜距**恒等于 radius；顶点不是"二维圆柱"。
   同一站点的水平切片半径与天顶高度必须同时出现，且水平半径 = radius。
2. **半球有据**：几何模型必须来自设备 ``coverage_geometry.model``，且
   ``omnidirectional = True`` / ``horizontal_coverage_deg = 360``；输出中**不得**
   出现任何 Radar 方位/扇区字段。
3. **未确认安装高度 ⇒ 无确定包络**：``origin_status != confirmed`` 时
   ``geometry.status = blocked`` 且 ``shell_rings`` 为空。
4. **RID 非正圆**：用**合成海岸线**分类器（陆地在一侧、海在另一侧）构造 footprint，
   断言半径集合同时含 2000 与 5000 量级、半径标准差 > 0、且 GeoJSON 是合法
   MultiPolygon。两个同心圆**不可能**通过这组断言。
5. **unknown fail-closed**：分类为 unknown 的方位角立即终止，且不产生几何。
6. **不冒充 Radar**：RID 输出显式携带 ``forbidden_geometry`` 与
   ``not_a_radar_sector = True``。
7. **不做垂直拉伸**：RID 只输出高度切片，``extrusion_forbidden = True``。
"""

from __future__ import annotations

import json
import math

import pytest

from cns_planner.application.visualization_3d_communication_volume import (
    COVERAGE_SEMANTICS,
    build_communication_volume,
    build_communication_volumes,
    metric_transformer,
)
from cns_planner.application.visualization_3d_rid_footprint import (
    FOOTPRINT_SEMANTICS,
    build_rid_footprint,
    build_rid_footprints,
)
from cns_planner.application.visualization_3d_sites import (
    communication_units,
    installed_service_units,
    rid_units,
)
from cns_planner.domain.cns_service_contract import (
    COMMUNICATION_NOT_EVALUATED,
    COMMUNICATION_RADIUS_BY_SURFACE,
    RID_NOT_EVALUATED,
    RID_RADIUS_BY_SURFACE,
)

METRIC_CRS = "EPSG:32651"


def unit(**overrides):
    base = {
        "site_id": "SITE-1", "facility_id": "FAC-1", "site_name": "测试站",
        "longitude": 122.191292092561, "latitude": 29.9962235778595,
        "coordinate_available": True,
        "device_id": "C-TEST", "subsystem": "C",
        "service_key": "C:communication",
        "service_contract": {
            "service_key": "C:communication", "service_surface_dependent": True,
            "policy": {
                "radius_basis": "geometric_planning_radius",
                "maturity": "engineering_planning_baseline",
                "parameter_semantics": "engineering_planning_baseline_not_verified_device_specification",
                "geometry": {"model": "hemisphere", "omnidirectional": True,
                             "horizontal_coverage_deg": 360.0},
                "missing_device_evidence": ["manufacturer", "frequency"],
                "not_evaluated": list(COMMUNICATION_NOT_EVALUATED),
            },
        },
        "geometry": {
            "model": "hemisphere", "slant_range_m": None, "model_scope": "geometric_only",
            "confirmed": True, "status": "confirmed",
            "radius_by_surface": {"land": 4000.0, "coastal_uncertain": 4000.0,
                                  "sea": 4000.0},
        },
        "geometry_usable": True,
        "service_origin_egm2008_m": 20.0,
        "origin_status": "confirmed",
        "origin_reason": None,
        "origin_source": "SYNTHETIC / ENGINEERING VALIDATION",
        "distinct_site_id": "site:SITE-1",
        "rid_type_declared": False,
    }
    base.update(overrides)
    return base


def rid_unit(**overrides):
    """RID 服务单元：契约来自冻结的 ``S:rid_cooperative`` policy。"""

    return unit(
        device_id="S-RID", subsystem="S", service_key="S:rid_cooperative",
        rid_type_declared=True,
        declaration_basis="explicit_rid_declaration",
        declaration_note="设备显式声明 RID",
        service_contract={
            "service_key": "S:rid_cooperative", "service_surface_dependent": True,
            "policy": {
                "radius_basis": "geometric_planning_radius",
                "maturity": "engineering_planning_baseline",
                "parameter_semantics": "engineering_planning_baseline_not_verified_device_specification",
                "geometry": {"model": "hemisphere", "omnidirectional": True,
                             "horizontal_coverage_deg": 360.0},
                "urban_enabled": False,
                "urban_classification": "disabled",
                "forbidden_geometry": ["radar_90_degree_panel", "sector",
                                       "horizontal_semicircle", "panel_azimuth"],
                "missing_device_evidence": ["manufacturer", "frequency"],
                "not_evaluated": list(RID_NOT_EVALUATED),
            },
        },
        geometry={
            "model": "hemisphere", "slant_range_m": None, "model_scope": "geometric_only",
            "confirmed": True, "status": "confirmed",
            "radius_by_surface": dict(RID_RADIUS_BY_SURFACE),
            "urban_radius_m": 1000.0, "urban_enabled": False,
        },
        **overrides)


class StraitClassifier:
    """合成**海峡**分类器：站心西侧是大陆、东侧隔海还有一条远岸。

    为什么必须是海峡（而不是一条海岸线）：如果只有一条海岸线，那么沿任意方位角
    向外走，只会遇到"该地表超出其策略半径"这一种终止条件，于是轮廓半径只可能取
    ``{land_radius, sea_radius}`` 两个离散值 —— 与"两个同心圆"在数值上不可区分，
    回归就失去意义。海峡情形下，向东的方位角会依次遇到
    陆（近岸）→ 海（水道）→ 陆（远岸）；远岸落在 2 km 之外时该方位角立即终止，
    终止半径**就是远岸的真实距离**，随纬度连续变化。

    海岸线本身是**斜线加正弦扰动**，因此终止半径既不是常数、也不与任何同心圆重合。

    它只用于**回归**：验证 footprint 的轮廓确实由分类结果驱动。
    生产路径**绝不**使用它（生产一律用权威 ``LandMaskSource``）。
    """

    def __init__(self, *, near_coast_lon=122.1950, far_coast_lon=122.2260,
                 base_lat=29.9962, slope=0.45, amplitude_deg=0.0016,
                 buffer_deg=0.00015, unknown_beyond=None):
        self.near_coast_lon = near_coast_lon
        self.far_coast_lon = far_coast_lon
        self.base_lat = base_lat
        self.slope = slope
        self.amplitude_deg = amplitude_deg
        self.buffer_deg = buffer_deg
        self.unknown_beyond = unknown_beyond
        self.calls = []

    def _offset(self, latitude):
        return (self.slope * (latitude - self.base_lat)
                + self.amplitude_deg * math.sin((latitude - self.base_lat) * 380.0))

    def near_lon(self, latitude):
        return self.near_coast_lon + self._offset(latitude)

    def far_lon(self, latitude):
        return self.far_coast_lon + self._offset(latitude) * 0.6

    def classify_many(self, points):
        self.calls.append(len(points))
        result = []
        for longitude, latitude in points:
            if self.unknown_beyond is not None and longitude > self.unknown_beyond:
                result.append("unknown")
                continue
            near, far = self.near_lon(latitude), self.far_lon(latitude)
            if abs(longitude - near) <= self.buffer_deg:
                result.append("coastal_uncertain")
            elif longitude < near:
                result.append("land")
            elif abs(longitude - far) <= self.buffer_deg:
                result.append("coastal_uncertain")
            elif longitude > far:
                result.append("land")
            else:
                result.append("sea")
        return result


class AllUnknownClassifier:
    def classify_many(self, points):
        return ["unknown"] * len(points)


class SectorUnknownClassifier(StraitClassifier):
    """在给定方位角扇区内返回 ``unknown`` 的分类器（用于 fail-closed 回归）。

    方位角以**站心正东为 0°、逆时针为正**（与实现里的 ``theta`` 一致）。
    """

    def __init__(self, *, unknown_from_deg, unknown_to_deg, **kwargs):
        super().__init__(**kwargs)
        self.unknown_from_deg = unknown_from_deg
        self.unknown_to_deg = unknown_to_deg

    def classify_many(self, points):
        self.calls.append(len(points))
        result = []
        for longitude, latitude in points:
            # 用站点经纬度近似方位角（回归用，精度足够区分扇区）。
            delta_lon = longitude - 122.191292092561
            delta_lat = latitude - 29.9962235778595
            bearing = math.degrees(math.atan2(delta_lat, delta_lon)) % 360.0
            if self.unknown_from_deg <= bearing <= self.unknown_to_deg:
                result.append("unknown")
                continue
            near, far = self.near_lon(latitude), self.far_lon(latitude)
            if abs(longitude - near) <= self.buffer_deg:
                result.append("coastal_uncertain")
            elif longitude < near:
                result.append("land")
            elif abs(longitude - far) <= self.buffer_deg:
                result.append("coastal_uncertain")
            elif longitude > far:
                result.append("land")
            else:
                result.append("sea")
        return result


class AllSeaClassifier:
    def classify_many(self, points):
        return ["sea"] * len(points)


# ------------------------------------------------------------------ 通信包络


def test_communication_volume_shell_points_are_at_constant_slant_range():
    volume = build_communication_volume(unit(), metric_crs=METRIC_CRS)
    geometry = volume["geometry"]
    assert geometry["status"] == "available"
    assert geometry["slant_range_m"] == 4000.0
    assert geometry["slant_distance_is_criterion"] is True
    assert geometry["two_dimensional_disk_is_not_the_volume"] is True

    to_metric, _ = metric_transformer(METRIC_CRS)
    center = to_metric(volume["longitude"], volume["latitude"])
    origin_z = volume["service_origin_egm2008_m"]
    assert center is not None

    # 逐顶点复算斜距：水平距离（米制）与垂直差（EGM2008 正高差）的 hypot。
    checked = 0
    for ring in geometry["shell_rings"]:
        for longitude, latitude, height in ring:
            point = to_metric(longitude, latitude)
            assert point is not None
            horizontal = math.hypot(point[0] - center[0], point[1] - center[1])
            delta = height - origin_z
            assert delta >= -1e-6                       # 上半球
            slant = math.hypot(horizontal, delta)
            assert slant == pytest.approx(4000.0, abs=0.75)
            checked += 1
    assert checked > 500

    # 天顶与水平面都必须存在：证明这不是"二维 4 km 圆柱"。
    apex_ring = geometry["shell_rings"][0]
    apex_heights = {row[2] for row in apex_ring}
    assert max(apex_heights) == pytest.approx(origin_z + 4000.0, abs=0.01)
    last_ring = geometry["shell_rings"][-1]
    for row in last_ring:
        assert row[2] == pytest.approx(origin_z, abs=1e-6)
    assert geometry["apex_egm2008_m"] == pytest.approx(origin_z + 4000.0, abs=0.01)


def test_communication_volume_is_omnidirectional_and_never_a_radar_sector():
    volume = build_communication_volume(unit(), metric_crs=METRIC_CRS)
    geometry = volume["geometry"]
    assert geometry["omnidirectional"] is True
    assert geometry["horizontal_coverage_deg"] == 360.0
    assert geometry["model"] == "hemisphere"
    blob = json.dumps(volume, ensure_ascii=False).lower()
    for forbidden in ("azimuth_deg", "panel", "sector", "beamwidth", "boresight"):
        assert forbidden not in blob


def test_communication_volume_requires_confirmed_installation_height():
    blocked = build_communication_volume(
        unit(origin_status="unconfirmed", origin_reason="service_origin_not_confirmed"),
        metric_crs=METRIC_CRS)
    assert blocked["geometry"]["status"] == "blocked"
    assert blocked["geometry"]["reason"] == "service_origin_not_confirmed"
    assert blocked["geometry"]["shell_rings"] == []
    assert blocked["geometry"]["horizontal_section"] == []
    assert blocked["origin_confirmed"] is False
    assert blocked["geometry"]["required_evidence"]

    missing = build_communication_volume(
        unit(service_origin_egm2008_m=None, origin_status="missing",
             origin_reason="service_origin_egm2008_m_not_recorded"),
        metric_crs=METRIC_CRS)
    assert missing["geometry"]["status"] == "blocked"
    assert missing["geometry"]["shell_rings"] == []


def test_communication_volume_uses_authoritative_surface_radius():
    # 半径必须经 effective_radius_m 解析；land/coastal/sea 三档各取各的值。
    geometry = {"model": "hemisphere", "slant_range_m": None,
                "radius_by_surface": {"land": 4000.0, "coastal_uncertain": 4000.0,
                                      "sea": 5000.0}}
    land = build_communication_volume(unit(geometry=geometry), metric_crs=METRIC_CRS,
                                      surface_class="land")
    sea = build_communication_volume(unit(geometry=geometry), metric_crs=METRIC_CRS,
                                     surface_class="sea")
    assert land["geometry"]["slant_range_m"] == 4000.0
    assert sea["geometry"]["slant_range_m"] == 5000.0
    assert land["geometry"]["radius_source"] == "radius_by_surface"
    assert land["geometry"]["radius_basis"] == "per_surface_resolution"
    assert land["geometry"]["envelope_over_surfaces"] is False
    # 未给定 / unknown 地表 ⇒ 只显示**地表包络**（各档最大值），且必须显式标注：
    # 它绝不是"unknown 地表也已覆盖"的判定。
    for value in (None, "unknown"):
        envelope = build_communication_volume(unit(geometry=geometry),
                                              metric_crs=METRIC_CRS, surface_class=value)
        assert envelope["geometry"]["status"] == "available"
        assert envelope["geometry"]["slant_range_m"] == 5000.0
        assert envelope["geometry"]["radius_basis"] == "surface_envelope_max"
        assert envelope["geometry"]["envelope_over_surfaces"] is True
        assert "不是**任何单一地表的覆盖判定" in envelope["geometry"]["envelope_note"]
    assert land["radius_by_surface_m"] == {"land": 4000.0, "coastal_uncertain": 4000.0,
                                           "sea": 5000.0}


def test_rid_footprint_never_leaves_not_evaluated_empty():
    """真实缺陷回归：未评估项**不得**为空数组。

    当设备没有显式声明 RID 类型时，``service_contract_for`` 的 service_key 会回落到
    legacy ``S:surveillance``，而冻结策略表里没有这个 key ⇒ ``policy`` 为空 ⇒
    ``not_evaluated`` 变成 ``[]``，前端弹窗显示成"未评估 —"。那是**漏披露**，
    等于默认宣称"没有未评估因素"。本端点声明的服务就是 RID，必须回落到冻结的
    ``RID_NOT_EVALUATED``。
    """

    # 构造"服务契约回落到 legacy（policy 为空）"的单元，模拟真实状态。
    legacy_unit = rid_unit()
    legacy_unit["service_contract"] = {
        "service_key": "S:surveillance", "service_surface_dependent": False,
        "policy": None,
    }
    footprint = build_rid_footprint(legacy_unit, AllSeaClassifier(),
                                    metric_crs=METRIC_CRS)
    assert footprint["not_evaluated"], "未评估项不得为空"
    assert set(footprint["not_evaluated"]) == set(RID_NOT_EVALUATED)
    assert footprint["not_evaluated_source"] == (
        "frozen_contract_fallback_device_service_key_is_legacy")
    # 禁止几何清单同样不得为空（legacy 回落时曾一并变空）。
    assert footprint["service_contract"]["forbidden_geometry"] == [
        "radar_90_degree_panel", "sector", "horizontal_semicircle", "panel_azimuth"]
    assert footprint["service_contract"]["maturity"] == "engineering_planning_baseline"
    assert footprint["service_contract"]["radius_basis"] == "geometric_planning_radius"

    # 有显式 policy 时用 policy 自己的清单，并标明来源。
    explicit = build_rid_footprint(rid_unit(), AllSeaClassifier(), metric_crs=METRIC_CRS)
    assert explicit["not_evaluated"]
    assert explicit["not_evaluated_source"] == "service_contract_policy"


def test_communication_volume_discloses_not_evaluated_factors():
    volume = build_communication_volume(unit(), metric_crs=METRIC_CRS)
    assert volume["coverage_semantics"] == COVERAGE_SEMANTICS
    assert "terrain_LOS" in volume["not_evaluated"]
    assert "interference" in volume["not_evaluated"]
    assert "link_budget" in volume["not_evaluated"]
    assert "not_evaluated ≠ 通过" in volume["not_evaluated_semantics"]
    assert volume["model_scope"] == "geometric_only"
    assert volume["vertical_reference"] == "egm2008_orthometric"
    # 冻结契约里通信的 not_evaluated 必须**完整**投递（缺一项就是"未披露"）。
    from cns_planner.domain.cns_service_contract import COMMUNICATION_NOT_EVALUATED

    assert set(volume["not_evaluated"]) >= set(COMMUNICATION_NOT_EVALUATED)


def test_communication_volumes_counts_available_and_blocked():
    result = build_communication_volumes(
        [unit(), unit(device_id="C-BLOCKED", origin_status="missing",
                      service_origin_egm2008_m=None)],
        metric_crs=METRIC_CRS)
    assert result["volume_count"] == 2
    assert result["available_count"] == 1
    assert result["blocked_count"] == 1
    assert result["truncated"] is False


# ------------------------------------------------------------------ RID footprint


def test_rid_footprint_is_non_circular_and_driven_by_surface_classification():
    classifier = StraitClassifier()
    footprint = build_rid_footprint(
        rid_unit(),
        classifier, metric_crs=METRIC_CRS,
        land_radius_m=RID_RADIUS_BY_SURFACE["land"],
        sea_radius_m=RID_RADIUS_BY_SURFACE["sea"],
        azimuth_segments=72, radial_step_m=25.0)
    geometry = footprint["geometry"]
    assert geometry["status"] == "available"

    radii = [row["boundary_radius_m"] for row in geometry["radial_profile"]]
    assert len(radii) == 72
    # 陆侧 ≈2000、海侧 ≈5000 —— 两个量级必须**同时**出现。
    assert min(radii) == pytest.approx(2000.0, abs=60.0)
    assert max(radii) == pytest.approx(5000.0, abs=60.0)
    assert geometry["boundary_radius_m"]["stddev"] > 500.0
    assert geometry["boundary_radius_m"]["distinct_values"] > 10
    evidence = geometry["non_circular_evidence"]
    assert evidence["radius_varies_with_azimuth"] is True
    assert evidence["both_land_and_sea_radii_present"] is True

    # 同心圆冒充检测：若轮廓是两个同心圆，半径集合只会有 2 个不同值。
    rounded = {round(radius) for radius in radii}
    assert len(rounded) > 2

    geojson = geometry["geojson"]
    assert geojson["type"] == "MultiPolygon"
    assert geometry["validity"]["is_valid"] is True
    assert geometry["validity"]["geom_type"] == "MultiPolygon"
    assert classifier.calls, "必须真的调用权威分类器"
    assert geometry["unknown_is_fail_closed"] is True


def test_rid_footprint_has_distinct_land_and_sea_parts_with_surface_class():
    classifier = StraitClassifier()
    footprint = build_rid_footprint(
        rid_unit(), classifier, metric_crs=METRIC_CRS,
        azimuth_segments=72, radial_step_m=25.0)
    geometry = footprint["geometry"]
    parts = geometry["surface_parts"]
    classes = {part["surface_class"] for part in parts}
    assert "sea" in classes
    assert classes & {"land_or_coastal_uncertain"}
    for part in parts:
        assert part["policy_radius_m"] in (2000.0, 5000.0)
        assert part["is_valid"] is True
        # 每个分区都必须自带**合法**的独立 GeoJSON（可单独着色）。
        assert part["geojson"]["type"] == "MultiPolygon"
        assert part["geojson"]["coordinates"]
    # 海侧部分必须比陆侧部分大（5 km 外沿 > 2 km 陆侧）。
    sea = next(part for part in parts if part["surface_class"] == "sea")
    land = next(part for part in parts if part["surface_class"] != "sea")
    assert sea["area_deg2"] > land["area_deg2"]
    # 相邻分区共享海岸线边 ⇒ 必须显式说明，避免被误读成"两个同心圆"。
    assert "共享海岸线边" in geometry["surface_parts_semantics"]
    assert geometry["geojson_semantics"].startswith("整体 footprint")


def test_rid_footprint_unknown_surface_is_fail_closed():
    # 全 unknown ⇒ 不产生任何几何，**绝不**按陆地或海上处理。
    blocked = build_rid_footprint(rid_unit(), AllUnknownClassifier(),
                                  metric_crs=METRIC_CRS)
    assert blocked["geometry"]["status"] == "blocked"
    assert blocked["geometry"]["reason"] == "no_classified_surface_within_policy_radius"
    assert blocked["geometry"]["geojson"] is None
    assert blocked["geometry"]["unknown_azimuth_count"] > 0

    # 只在一个方位角方向 unknown：其余方位角仍可用，但该方向立即终止且**不猜**地表。
    partial = build_rid_footprint(
        rid_unit(),
        SectorUnknownClassifier(unknown_from_deg=60.0, unknown_to_deg=120.0),
        metric_crs=METRIC_CRS, azimuth_segments=72, radial_step_m=25.0)
    geometry = partial["geometry"]
    assert geometry["status"] == "available"
    reasons = {row["stopped_reason"] for row in geometry["radial_profile"]}
    assert "unknown_surface_class_fail_closed" in reasons
    assert geometry["unknown_azimuth_count"] > 0
    # 被 unknown 截断的方位角半径必须**明显小于**同方位角在没有 unknown 时的半径。
    reference = build_rid_footprint(
        rid_unit(), StraitClassifier(), metric_crs=METRIC_CRS,
        azimuth_segments=72, radial_step_m=25.0)
    reference_by_azimuth = {
        row["bearing_deg"]: row["boundary_radius_m"]
        for row in reference["geometry"]["radial_profile"]
    }
    truncated = [
        row for row in geometry["radial_profile"]
        if row["stopped_reason"] == "unknown_surface_class_fail_closed"
    ]
    assert truncated
    for row in truncated:
        assert row["boundary_radius_m"] < reference_by_azimuth[row["bearing_deg"]]


def test_rid_footprint_is_never_a_radar_sector_and_never_extruded():
    footprint = build_rid_footprint(
        rid_unit(), StraitClassifier(),
        metric_crs=METRIC_CRS)
    assert footprint["coverage_semantics"] == FOOTPRINT_SEMANTICS
    assert footprint["not_a_radar_sector"] is True
    assert footprint["omnidirectional"] is True
    assert footprint["forbidden_geometry"] == [
        "radar_90_degree_panel", "sector", "horizontal_semicircle", "panel_azimuth"]
    assert footprint["vertical"]["status"] == "slice_only"
    assert footprint["vertical"]["vertical_effective_range"] == "not_modelled"
    assert footprint["vertical"]["extrusion_forbidden"] is True
    assert footprint["geometry"]["two_dimensional_only"] is True
    # GeoJSON 是**二维**的：任何顶点都不得携带高度分量（即不做垂直拉伸）。
    for polygon in footprint["geometry"]["geojson"]["coordinates"]:
        for ring in polygon:
            for coordinate in ring:
                assert len(coordinate) == 2
    assert footprint["vertical"]["slice_egm2008_m"] == 20.0
    assert "receiver_sensitivity" in footprint["not_evaluated"]
    assert "packet_collision" in footprint["not_evaluated"]


def test_rid_footprint_requires_confirmed_origin_and_classifier():
    no_origin = build_rid_footprint(
        unit(device_id="S-RID", origin_status="missing",
             service_origin_egm2008_m=None),
        StraitClassifier(), metric_crs=METRIC_CRS)
    assert no_origin["geometry"]["status"] == "blocked"
    assert no_origin["geometry"]["reason"] == "service_origin_egm2008_m_not_recorded"

    no_classifier = build_rid_footprint(rid_unit(), None,
                                        metric_crs=METRIC_CRS)
    assert no_classifier["geometry"]["status"] == "blocked"
    assert no_classifier["geometry"]["reason"] == "land_mask_classifier_unavailable"


def test_rid_footprints_batch_reports_counts():
    result = build_rid_footprints(
        [unit(device_id="S-RID-1"), unit(device_id="S-RID-2")],
        StraitClassifier(), metric_crs=METRIC_CRS)
    assert result["footprint_count"] == 2
    assert result["available_count"] == 2
    assert result["blocked_count"] == 0


def test_all_sea_site_yields_single_circular_part_but_is_labelled_as_such():
    # 全部海：轮廓**确实**是半径 5000 的圆 —— 这不是错误，但必须被如实标注为
    # "该站点周围没有可判定陆地"，而不是伪装成陆海分区结果。
    footprint = build_rid_footprint(
        rid_unit(), AllSeaClassifier(), metric_crs=METRIC_CRS,
        azimuth_segments=36, radial_step_m=50.0)
    geometry = footprint["geometry"]
    assert geometry["status"] == "available"
    radii = [row["boundary_radius_m"] for row in geometry["radial_profile"]]
    assert min(radii) == max(radii) == pytest.approx(5000.0, abs=1.0)
    assert geometry["non_circular_evidence"]["radius_varies_with_azimuth"] is False
    assert geometry["non_circular_evidence"]["both_land_and_sea_radii_present"] is False
    classes = {part["surface_class"] for part in geometry["surface_parts"]}
    assert classes == {"sea"}


# ------------------------------------------------------------------ 站址解析


def test_installed_units_resolution_matches_p7_order_and_flags_unconfirmed():
    state = {
        "device_catalog": {"items": [
            {"device_id": "C-1", "subsystem": "C", "service_key": "C:communication",
             "coverage_geometry": {"model": "hemisphere", "slant_range_m": None,
                                   "radius_by_surface": dict(COMMUNICATION_RADIUS_BY_SURFACE)},
             "vertical_profile": {"service_origin_egm2008_m": 30.0, "confirmed": True,
                                  "status": "confirmed"}},
        ]},
        "existing_cns_facilities": {"items": [
            {"facility_id": "F-1", "site_id": "S-1", "coordinate": [122.19, 29.99],
             "status": "active",
             "vertical_profile": {"service_origin_egm2008_m": 20.0, "confirmed": True,
                                  "status": "confirmed"},
             "devices": [{"device_id": "C-1", "subsystem": "C"}]},
        ]},
    }
    units = installed_service_units(state)
    assert len(units) == 1
    # 安装记录自身没有 vertical_profile ⇒ 回落到站址的 20.0（与 P7 候选顺序一致）。
    assert units[0]["service_origin_egm2008_m"] == 20.0
    assert units[0]["origin_status"] == "confirmed"
    assert units[0]["service_key"] == "C:communication"
    assert units[0]["distinct_site_id"] == "site:S-1"
    assert communication_units(state)[0]["device_id"] == "C-1"


def test_rid_units_require_explicit_declaration_or_matching_surface_policy():
    state = {
        "device_catalog": {"items": [
            {"device_id": "S-RID-EXPLICIT", "subsystem": "S",
             "service_key": "S:rid_cooperative",
             "type": {"technology": "network_remote_id"},
             "coverage_geometry": {"model": "hemisphere",
                                   "radius_by_surface": dict(RID_RADIUS_BY_SURFACE)}},
            {"device_id": "S-SURFACE-ONLY", "subsystem": "S",
             "coverage_geometry": {"model": "hemisphere",
                                   "radius_by_surface": dict(RID_RADIUS_BY_SURFACE)}},
            {"device_id": "S-OTHER", "subsystem": "S",
             "coverage_geometry": {"model": "hemisphere",
                                   "radius_by_surface": {"land": 3000.0,
                                                         "coastal_uncertain": 3000.0,
                                                         "sea": 3000.0}}},
        ]},
        "existing_cns_facilities": {"items": [
            {"facility_id": "F-1", "site_id": "S-1", "coordinate": [122.19, 29.99],
             "status": "active",
             "vertical_profile": {"service_origin_egm2008_m": 20.0, "confirmed": True,
                                  "status": "confirmed"},
             "devices": [{"device_id": "S-RID-EXPLICIT", "subsystem": "S"},
                         {"device_id": "S-SURFACE-ONLY", "subsystem": "S"},
                         {"device_id": "S-OTHER", "subsystem": "S"}]},
        ]},
    }
    units = rid_units(state, rid_radius_by_surface=RID_RADIUS_BY_SURFACE)
    by_id = {item["device_id"]: item for item in units}
    assert by_id["S-RID-EXPLICIT"]["declaration_basis"] == "explicit_rid_declaration"
    assert by_id["S-RID-EXPLICIT"]["rid_type_declared"] is True
    # 仅半径策略一致：必须如实标注"不是设备类型确认"。
    assert by_id["S-SURFACE-ONLY"]["declaration_basis"] == "surface_radius_matches_rid_policy"
    assert by_id["S-SURFACE-ONLY"]["rid_type_declared"] is False
    assert "不是设备类型确认" in by_id["S-SURFACE-ONLY"]["declaration_note"]
    # 半径策略不一致的 S 设备**不得**被算作 RID。
    assert "S-OTHER" not in by_id
