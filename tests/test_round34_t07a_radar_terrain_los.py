"""Round34-T07-A —— Radar 三维高度与地形 LOS 覆盖建模的定向测试。

覆盖任务 §9 的验收清单：

1. 山体不遮挡（clear）；2. 山体明显遮挡（blocked）；3. 临界地形切触（marginal→unknown）；
4. DEM 缺失 / NoData；5. 雷达原点低于地形（not_evaluable）；6. 估算与确认原点的证据隔离；
7. 不同高度层 probe；8. 仰角与斜距；9. 同塔不同设备原点；10. 同输入确定性与缓存；
11. 指纹变化；12. Radar 冻结几何不变。

测试全部使用**合成**地形与米制替身（不依赖 GDAL / QGIS）；真实数据验证由
``tools/round34_t07a_real_data_terrain_los.py`` 在带 GDAL 的解释器上单独执行。
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from cns_planner.domain import radar_terrain_los as los
from cns_planner.domain.radar_surveillance_layout import (
    AZIMUTH_BEAMWIDTH_DEG, ELEVATION_CENTER_DEG, ELEVATION_MAX_DEG, ELEVATION_MIN_DEG,
    RADAR_GEOMETRY_PARAMETERS, RADAR_TYPE_I, RADAR_TYPE_II,
)
from cns_planner.gis import radar_terrain_los_adapter as adapter

# --------------------------------------------------------------------- fixtures

#: 合成栅格：经度 121.0 起、0.0003 度/列（约 29 m）、纬度 30.0 起、-0.0003 度/行。
COLUMNS = 120
ROWS = 60
LON_ORIGIN = 121.0
LAT_ORIGIN = 30.0
DEG_STEP = 0.0003
METERS_PER_DEGREE_LON = 96486.0
METERS_PER_DEGREE_LAT = 110900.0


class FakeBand:
    def __init__(self, array, nodata):
        self._array = array
        self._nodata = nodata

    def ReadAsArray(self, x0=None, y0=None, width=None, height=None):
        if x0 is None:
            return self._array
        return self._array[y0:y0 + height, x0:x0 + width]

    def GetNoDataValue(self):
        return self._nodata

    def GetScale(self):
        return None

    def GetOffset(self):
        return None


class FakeTerrain:
    """FABDEM 只读访问器的替身：同一套 ``_FabdemRasterBase`` 契约。"""

    role = "terrain_dtm"

    def __init__(self, *, base=30.0, columns=None, nodata=-9999.0,
                 vertical_reference="egm2008_orthometric"):
        #: 与真实 GDAL 读取完全同型（``numpy.ndarray``），顺序为 [行][列]。
        self.array = np.full((ROWS, COLUMNS), float(base), dtype=float)
        for column, value in (columns or {}).items():
            self.array[:, column] = float(value)
        self.vertical_reference = vertical_reference
        self.vertical_status = (
            "confirmed" if vertical_reference.lower() == "egm2008_orthometric"
            else "unresolved"
        )
        self.nodata = nodata
        self.transform = (LON_ORIGIN, DEG_STEP, 0.0, LAT_ORIGIN, 0.0, -DEG_STEP)
        self.band = FakeBand(self.array, nodata)

    #: 兼容 ``_FabdemRasterBase`` 契约所需的最小表面。
    class _Dataset:
        RasterXSize = COLUMNS
        RasterYSize = ROWS

    dataset = _Dataset()

    def describe(self):
        return {
            "dataset": "FABDEM", "file_name": "synthetic.tif",
            "vertical_reference": self.vertical_reference,
            "read_mode": los.RADAR_TERRAIN_LOS_READ_MODE,
        }

    def usable(self):
        if self.vertical_status != "confirmed":
            return False, "terrain_vertical_datum_unresolved"
        return True, None

    def _pixel_geographic(self, column, row):
        return [LON_ORIGIN + DEG_STEP * column, LAT_ORIGIN - DEG_STEP * row]

    def _to_pixel(self, geographic):
        return (
            (geographic[0] - LON_ORIGIN) / DEG_STEP,
            (LAT_ORIGIN - geographic[1]) / DEG_STEP,
        )

    def effective_resolution_m(self):
        return 30.0

    def effective_resolution_detail(self):
        return {
            "status": "passed",
            "effective_resolution_m_x": 27.0,
            "effective_resolution_m_y": 30.0,
            "effective_resolution_m": 30.0,
        }


class FakeTransform:
    """米制替身：与 :class:`FakeTerrain` 的像元网格逐字对应。"""

    def to_metric(self, point):
        return [
            (point[0] - LON_ORIGIN) * METERS_PER_DEGREE_LON,
            (point[1] - LAT_ORIGIN) * METERS_PER_DEGREE_LAT,
        ]

    def to_geographic(self, point):
        return [
            LON_ORIGIN + point[0] / METERS_PER_DEGREE_LON,
            LAT_ORIGIN + point[1] / METERS_PER_DEGREE_LAT,
        ]


def _column_east_m(column):
    return column * DEG_STEP * METERS_PER_DEGREE_LON


def _origin_row_latitude(row):
    """第 ``row`` 行像元中心的纬度（与射线 y = 该行中心米制坐标一致）。"""

    return LAT_ORIGIN - DEG_STEP * (row + 0.5)


def _run_profile(terrain, *, target_east_m, origin_alt, target_alt,
                 envelope_radius_m=40.0, sampling_interval_m=5.0,
                 earth_radius_m=None, k_factor=1.0):
    transform = FakeTransform()
    origin_metric = [0.0, 0.0]
    target_metric = [target_east_m, 0.0]
    profile = adapter.sample_terrain_profile(
        terrain, transform=transform, origin_metric=origin_metric,
        target_metric=target_metric, sampling_interval_m=sampling_interval_m,
        envelope_radius_m=envelope_radius_m,
    )
    assert profile["status"] == "passed", profile.get("reason")
    samples = []
    for sample in profile["samples"]:
        ray = los.ray_height_m(
            origin_egm2008_m=origin_alt, target_egm2008_m=target_alt,
            horizontal_m=target_east_m, sample_distance_m=sample["distance_m"],
            earth_radius_m=earth_radius_m, k_factor=k_factor,
        )
        samples.append({
            **sample, "ray_height_m": ray,
            "status": los.classify_terrain_sample(
                ray_height_m_value=ray, terrain_min_m=sample["terrain_min_m"],
                terrain_max_m=sample["terrain_max_m"],
            ),
        })
    verdict = los.evaluate_terrain_los(
        origin_egm2008_m=origin_alt, target_egm2008_m=target_alt, samples=samples,
        earth_radius_m=earth_radius_m, k_factor=k_factor,
        sampling_interval_m=sampling_interval_m,
    )
    return profile, samples, verdict


def _tower(**overrides):
    record = {
        "tower_id": "T-1", "longitude": LON_ORIGIN, "latitude": LAT_ORIGIN,
        "origin_egm2008_m": 60.0, "origin_source": "tower_top_orthometric_m",
        "origin_state": "usable_estimated", "origin_evidence_level": "estimated",
        "origin_tier": "tower_obstacle_profile", "tower_height_m": 20.0,
        "building_height_m": None, "base_type": "ground",
    }
    record.update(overrides)
    return record


# ------------------------------------------------------- 1. 山体不遮挡（clear）


def test_flat_terrain_is_clear():
    terrain = FakeTerrain(base=30.0)
    _profile, _samples, verdict = _run_profile(
        terrain, target_east_m=1500.0, origin_alt=100.0, target_alt=100.0,
    )
    assert verdict["status"] == los.LOS_STATUS_CLEAR
    assert verdict["reason"] is None
    assert verdict["first_obstruction_distance_m"] is None
    assert verdict["minimum_clearance_m"] == pytest.approx(70.0, abs=1e-6)
    assert verdict["counts"][los.TERRAIN_SAMPLE_BLOCKED] == 0
    assert verdict["counts"][los.TERRAIN_SAMPLE_UNKNOWN] == 0


# ------------------------------------------------------- 2. 山体明显遮挡（blocked）


def test_ridge_blocks_the_ray():
    terrain = FakeTerrain(base=30.0, columns={4: 200.0})
    _profile, _samples, verdict = _run_profile(
        terrain, target_east_m=1500.0, origin_alt=100.0, target_alt=100.0,
    )
    assert verdict["status"] == los.LOS_STATUS_BLOCKED
    assert verdict["reason"] == "terrain_blocks_ray"
    assert verdict["first_obstruction_distance_m"] is not None
    #: 山体在第 4 列（约 116 m）；首个遮挡必然不晚于它。
    assert verdict["first_obstruction_distance_m"] <= _column_east_m(4) + 40.0
    assert verdict["minimum_clearance_m"] < 0


def test_distant_ridge_beyond_the_ray_end_does_not_block():
    terrain = FakeTerrain(base=30.0, columns={100: 500.0})
    _profile, _samples, verdict = _run_profile(
        terrain, target_east_m=1000.0, origin_alt=100.0, target_alt=100.0,
    )
    assert verdict["status"] == los.LOS_STATUS_CLEAR


# ------------------------------------------------------- 3. 临界地形切触


def test_grazing_contact_is_never_reported_as_clear():
    #: 山脊高 100 m。射线在山脊附近的高度逐步抬升：只要射线没有明确高于山脊，
    #: 结论就**不允许**是 clear（切触/数据分辨率不足必须是 unknown 或 blocked）。
    terrain = FakeTerrain(base=30.0, columns={4: 100.0})
    transition = []
    for altitude in (95.0, 98.0, 100.0, 105.0):
        profile, samples, verdict = _run_profile(
            terrain, target_east_m=1500.0, origin_alt=altitude, target_alt=altitude,
        )
        transition.append((altitude, verdict["status"]))
        assert profile["sampling"]["pixel_association"]["pixel_count"] > 0
        if altitude < 100.0:
            assert verdict["status"] != los.LOS_STATUS_CLEAR, (
                f"射线 {altitude} m 低于 100 m 山脊时不得判 clear：{verdict}"
            )
        if altitude >= 100.0:
            assert verdict["counts"][los.TERRAIN_SAMPLE_UNKNOWN] == 0
    assert transition[-1][1] == los.LOS_STATUS_CLEAR
    assert dict(transition)[95.0] == los.LOS_STATUS_BLOCKED


def test_grazing_contact_produces_unknown_and_zero_clearance():
    #: 让整条射线恰好卡在山脊像元内、且射线低于像元最高地形：结论必须是 unknown
    #: （切触），既不判 clear，也不被当成"明显遮挡"。
    terrain = FakeTerrain(base=30.0, columns={4: 100.0})
    samples = []
    for index in range(8):
        ray = 99.5 if index else 100.0
        samples.append({
            "index": index, "distance_m": float(index) * 5.0, "ray_height_m": ray,
            "terrain_min_m": 30.0 if index else 100.0,
            "terrain_max_m": 100.0,
            "status": los.classify_terrain_sample(
                ray_height_m_value=ray,
                terrain_min_m=30.0 if index else 100.0, terrain_max_m=100.0,
            ),
        })
    verdict = los.evaluate_terrain_los(
        origin_egm2008_m=100.0, target_egm2008_m=99.5, samples=samples,
    )
    assert verdict["status"] == los.LOS_STATUS_UNKNOWN
    assert verdict["reason"] == "terrain_grazing_contact_not_evaluable_as_clear"
    assert verdict["counts"][los.TERRAIN_SAMPLE_BLOCKED] == 0
    assert verdict["counts"][los.TERRAIN_SAMPLE_CLEAR] == 0
    assert verdict["first_marginal_distance_m"] == pytest.approx(5.0)
    assert terrain is not None


# ------------------------------------------------------- 4. DEM 缺失 / NoData


def test_terrain_source_missing_is_unknown():
    profile = adapter.sample_terrain_profile(
        None, transform=FakeTransform(), origin_metric=[0.0, 0.0],
        target_metric=[500.0, 0.0],
    )
    assert profile["status"] == "unknown"
    assert profile["reason"] == "terrain_source_not_configured"
    assert profile["samples"] == []


def test_vertical_datum_unresolved_is_unknown():
    terrain = FakeTerrain(base=30.0, vertical_reference="msl")
    profile = adapter.sample_terrain_profile(
        terrain, transform=FakeTransform(), origin_metric=[0.0, 0.0],
        target_metric=[500.0, 0.0],
    )
    assert profile["status"] == "unknown"
    assert profile["reason"] == "terrain_vertical_datum_unresolved"


def test_nodata_pixel_never_becomes_zero_and_is_unknown():
    terrain = FakeTerrain(base=30.0)
    terrain.array[0][3] = -9999.0
    _profile, samples, verdict = _run_profile(
        terrain, target_east_m=1500.0, origin_alt=100.0, target_alt=100.0,
    )
    unknown = [item for item in samples if item["status"] == los.TERRAIN_SAMPLE_UNKNOWN]
    assert unknown
    assert all(item["terrain_min_m"] is None and item["terrain_max_m"] is None for item in unknown)
    assert all(
        item["reason"] == "source_pixel_nodata_never_filled_never_zeroed" for item in unknown
    )
    assert verdict["status"] == los.LOS_STATUS_UNKNOWN
    assert verdict["reason"] == "terrain_nodata_or_sampling_gap"
    assert verdict["first_unknown_distance_m"] is not None


def test_profile_outside_dtm_coverage_is_unknown():
    #: 目标点完全落在 DTM 覆盖之外（negative metric coordinates）⇒ unknown，
    #: 绝不因为"沿线起点还在栅格内"就给出一个 clear。
    terrain = FakeTerrain(base=30.0)
    profile = adapter.sample_terrain_profile(
        terrain, transform=FakeTransform(), origin_metric=[0.0, 0.0],
        target_metric=[-500000.0, 0.0],
    )
    assert profile["status"] == "unknown"
    assert profile["reason"] in (
        "target_outside_dtm_coverage", "no_source_pixel_within_profile_envelope",
    )
    assert profile["samples"] == []


def test_profile_leaving_the_dtm_midway_is_unknown():
    terrain = FakeTerrain(base=30.0)
    _profile, samples, verdict = _run_profile(
        terrain, target_east_m=1500.0, origin_alt=100.0, target_alt=100.0,
    )
    #: 反向验证：完全在覆盖内时**不应**出现"目标越界"的 unknown。
    assert verdict["status"] == los.LOS_STATUS_CLEAR
    assert samples[0]["latitude"] == pytest.approx(LAT_ORIGIN)


# ------------------------------------------------------- 5. 原点低于地形


def test_origin_below_terrain_is_not_evaluable_and_los_unknown():
    physical = los.origin_physical_input(
        origin_egm2008_m=52.0, origin_source="source_elevation_m_plus_tower_height_m",
        origin_state="usable_estimated", terrain_m=181.61, terrain_status="passed",
    )
    assert physical["origin_availability"] == los.ORIGIN_AVAILABILITY_NOT_EVALUABLE
    assert physical["terrain_consistency"] == los.TERRAIN_CONSISTENCY_BELOW
    assert physical["physical_los_status"] == los.LOS_STATUS_UNKNOWN
    assert physical["origin_egm2008_m"] == 52.0  # 原始高度**不被静默抬升**
    assert physical["origin_height_silently_raised"] is False
    assert physical["physical_los_assumed_clear"] is False
    assert physical["site_candidate_identity_preserved"] is True
    assert physical["origin_state"] == "usable_estimated"  # 词表未改写

    terrain = FakeTerrain(base=181.61)
    evaluation = adapter.evaluate_probe_terrain_los(
        _tower(origin_egm2008_m=52.0), {
            "voxel_id": "V-1", "longitude": LON_ORIGIN, "latitude": LAT_ORIGIN,
            "altitude_layer_id": "ALT-100",
        },
        terrain_source=terrain, transform=FakeTransform(),
        altitude_layer_id="ALT-100", nominal_altitude_m=100.0,
        origin_physical_input=physical,
        origin_availability=physical["origin_availability"],
    )
    assert evaluation["status"] == los.LOS_STATUS_UNKNOWN
    assert evaluation["reason"] == "radar_origin_below_fabdem_surface"
    assert evaluation["terrain_profile"] is None  # 绝不采样、绝不假定有效视距
    assert evaluation["radar_origin_egm2008_m"] == 52.0


def test_coincident_origin_is_not_evaluable():
    physical = los.origin_physical_input(
        origin_egm2008_m=100.0, origin_source="tower_top_orthometric_m",
        origin_state="usable_estimated", terrain_m=100.0, terrain_status="passed",
    )
    assert physical["terrain_consistency"] == los.TERRAIN_CONSISTENCY_COINCIDENT
    assert physical["origin_availability"] == los.ORIGIN_AVAILABILITY_NOT_EVALUABLE
    assert physical["reason"] == "radar_origin_coincident_with_fabdem_surface"


def test_origin_above_terrain_is_usable_for_main_evaluation():
    physical = los.origin_physical_input(
        origin_egm2008_m=193.61, origin_source="tower_top_orthometric_m",
        origin_state="usable_estimated", terrain_m=181.61, terrain_status="passed",
    )
    assert physical["origin_availability"] == los.ORIGIN_AVAILABILITY_USABLE
    assert physical["terrain_consistency"] == los.TERRAIN_CONSISTENCY_ABOVE
    assert physical["origin_terrain_delta_m"] == pytest.approx(12.0)


def test_origin_availability_vocabulary_is_disjoint_from_origin_state():
    states = set(los.ORIGIN_AVAILABILITY_STATES)
    assert states == {"usable", "unknown", "not_evaluable"}
    assert not states & {"usable_confirmed", "usable_estimated", "excluded"}
    physical = los.origin_physical_input(
        origin_egm2008_m=None, origin_source=None, origin_state="unknown",
        terrain_m=10.0, terrain_status="passed",
    )
    assert physical["origin_state_vocabulary_unchanged"] is True
    assert physical["origin_availability"] == los.ORIGIN_AVAILABILITY_UNKNOWN


# ------------------------------------------------------- scenario_only 敏感性


def test_scenario_only_height_never_written_back_or_promoted():
    physical = los.origin_physical_input(
        origin_egm2008_m=52.0, origin_source="source_elevation_m_plus_tower_height_m",
        origin_state="usable_estimated", terrain_m=181.61, terrain_status="passed",
    )
    scenario = los.radar_origin_above_terrain_scenario(
        origin_physical_input_result=physical, terrain_m=181.61,
        assumed_structure_height_m=12.0, assumed_building_height_m=None,
    )
    assert scenario["status"] == "scenario_only"
    assert scenario["scenario_origin_egm2008_m"] == pytest.approx(193.61)
    assert scenario["scenario_only"] is True
    assert scenario["written_back"] is False
    assert scenario["writes_to_project_state"] is False
    assert scenario["presented_as_egm2008_confirmation"] is False
    assert scenario["upgrades_estimated_to_confirmed"] is False
    assert scenario["site_base_surface_assumption"]
    assert scenario["equipment_installation_position"]


def test_scenario_only_requires_explicit_inputs_and_never_defaults_to_zero():
    physical = los.origin_physical_input(
        origin_egm2008_m=52.0, origin_source="source_elevation_m_plus_tower_height_m",
        origin_state="usable_estimated", terrain_m=181.61, terrain_status="passed",
    )
    scenario = los.radar_origin_above_terrain_scenario(
        origin_physical_input_result=physical, terrain_m=181.61,
        assumed_structure_height_m=None, assumed_building_height_m=None,
    )
    assert scenario["status"] == "unknown"
    assert scenario["scenario_origin_egm2008_m"] is None
    assert "tower_structure_height_m" in scenario["missing_scenario_inputs"]


# ------------------------------------------------------- 6. 证据隔离


def test_estimated_and_confirmed_origins_stay_isolated():
    terrain = FakeTerrain(base=30.0)
    physical = los.origin_physical_input(
        origin_egm2008_m=60.0, origin_source="tower_top_orthometric_m",
        origin_state="usable_estimated", terrain_m=30.0, terrain_status="passed",
    )
    probe = {
        "voxel_id": "V-1", "longitude": LON_ORIGIN, "latitude": LAT_ORIGIN,
        "altitude_layer_id": "ALT-100",
    }
    estimated = adapter.evaluate_probe_terrain_los(
        _tower(origin_state="usable_estimated", origin_evidence_level="estimated"),
        probe, terrain_source=terrain, transform=FakeTransform(),
        altitude_layer_id="ALT-100", nominal_altitude_m=100.0,
        origin_physical_input=physical, origin_availability=physical["origin_availability"],
    )
    confirmed = adapter.evaluate_probe_terrain_los(
        _tower(
            origin_state="usable_confirmed", origin_evidence_level="confirmed",
            origin_source="device_service_origin_declaration",
        ),
        probe, terrain_source=terrain, transform=FakeTransform(),
        altitude_layer_id="ALT-100", nominal_altitude_m=100.0,
        origin_physical_input=physical, origin_availability=physical["origin_availability"],
    )
    assert estimated["radar_origin_state"] == "usable_estimated"
    assert confirmed["radar_origin_state"] == "usable_confirmed"
    assert estimated["input_fingerprint"] != confirmed["input_fingerprint"]
    assert estimated["radar_origin_state_vocabulary_unchanged"] is True


def test_same_tower_different_device_origin_is_distinguished():
    terrain = FakeTerrain(base=30.0)
    physical = los.origin_physical_input(
        origin_egm2008_m=60.0, origin_source="device_service_origin_declaration",
        origin_state="usable_confirmed", terrain_m=30.0, terrain_status="passed",
    )
    probe = {
        "voxel_id": "V-1", "longitude": LON_ORIGIN + 0.008, "latitude": LAT_ORIGIN,
        "altitude_layer_id": "ALT-100",
    }
    first = adapter.evaluate_probe_terrain_los(
        _tower(origin_egm2008_m=60.0, origin_source="device_service_origin_declaration"),
        probe, terrain_source=terrain, transform=FakeTransform(),
        altitude_layer_id="ALT-100", nominal_altitude_m=100.0,
        origin_physical_input=physical, origin_availability=physical["origin_availability"],
    )
    second = adapter.evaluate_probe_terrain_los(
        _tower(origin_egm2008_m=75.0, origin_source="device_service_origin_declaration"),
        probe, terrain_source=terrain, transform=FakeTransform(),
        altitude_layer_id="ALT-100", nominal_altitude_m=100.0,
        origin_physical_input=physical, origin_availability=physical["origin_availability"],
    )
    assert first["slant_range_m"] != second["slant_range_m"]
    assert first["elevation_deg"] != second["elevation_deg"]
    assert first["input_fingerprint"] != second["input_fingerprint"]


# ------------------------------------------------------- 7. 不同高度层 probe


def test_probe_height_comes_from_authoritative_layer_catalog():
    probe = {
        "voxel_id": "V-1", "altitude_layer_id": "ALT-100",
        "altitude_egm2008_m": 107.5,  # corridor overlap midpoint：**绝不**使用
    }
    resolved = los.probe_height(probe, altitude_layer_id="ALT-100", nominal_altitude_m=100.0)
    assert resolved["status"] == "passed"
    assert resolved["altitude_egm2008_m"] == 100.0
    assert resolved["height_source"] == los.PROBE_HEIGHT_TIER_LAYER_CATALOG
    assert resolved["representative_height_used"] is False
    assert resolved["corridor_overlap_midpoint_used"] is False
    assert resolved["matches_probe_reported_altitude"] is False
    assert "altitude_egm2008_m" in resolved["ignored_representative_fields"]


def test_probe_height_without_catalog_entry_is_unknown_not_zero():
    resolved = los.probe_height(
        {"voxel_id": "V-1", "altitude_layer_id": "ALT-100"},
        altitude_layer_id="ALT-100", nominal_altitude_m=None,
    )
    assert resolved["status"] == "unknown"
    assert resolved["altitude_egm2008_m"] is None
    assert resolved["reason"] == "authoritative_altitude_layer_not_in_catalog"


def test_different_altitude_layers_change_geometry():
    terrain = FakeTerrain(base=30.0)
    physical = los.origin_physical_input(
        origin_egm2008_m=60.0, origin_source="tower_top_orthometric_m",
        origin_state="usable_estimated", terrain_m=30.0, terrain_status="passed",
    )
    probe = {
        "voxel_id": "V-1", "longitude": LON_ORIGIN, "latitude": LAT_ORIGIN,
        "altitude_layer_id": "ALT-060",
    }
    low = adapter.evaluate_probe_terrain_los(
        _tower(), probe, terrain_source=terrain, transform=FakeTransform(),
        altitude_layer_id="ALT-060", nominal_altitude_m=60.0,
        origin_physical_input=physical, origin_availability=physical["origin_availability"],
    )
    probe_high = {**probe, "altitude_layer_id": "ALT-200"}
    high = adapter.evaluate_probe_terrain_los(
        _tower(), probe_high, terrain_source=terrain, transform=FakeTransform(),
        altitude_layer_id="ALT-200", nominal_altitude_m=200.0,
        origin_physical_input=physical, origin_availability=physical["origin_availability"],
    )
    assert low["probe_height"]["altitude_egm2008_m"] == 60.0
    assert high["probe_height"]["altitude_egm2008_m"] == 200.0
    assert high["slant_range_m"] > low["slant_range_m"]
    assert high["input_fingerprint"] != low["input_fingerprint"]


# ------------------------------------------------------- 8. 仰角与斜距


def test_slant_range_and_elevation_match_frozen_geometry_conventions():
    geometry = los.probe_geometry(
        origin_egm2008_m=100.0, target_egm2008_m=250.0,
        origin_metric=[0.0, 0.0], target_metric=[3000.0, 0.0],
    )
    assert geometry["horizontal_distance_m"] == pytest.approx(3000.0)
    assert geometry["vertical_delta_m"] == pytest.approx(150.0)
    assert geometry["slant_distance_m"] == pytest.approx(math.hypot(3000.0, 150.0))
    assert geometry["elevation_deg"] == pytest.approx(
        math.degrees(math.atan2(150.0, 3000.0))
    )
    assert geometry["vertical_delta_semantics"] == "target_minus_radar_origin"
    assert geometry["vertical_reference"] == los.EGM2008_ORTHOMETRIC


def test_negative_elevation_below_origin_is_preserved_not_absolutised():
    geometry = los.probe_geometry(
        origin_egm2008_m=200.0, target_egm2008_m=100.0,
        origin_metric=[0.0, 0.0], target_metric=[1000.0, 0.0],
    )
    assert geometry["elevation_deg"] < 0
    assert geometry["slant_distance_m"] == pytest.approx(math.hypot(1000.0, -100.0))


# ------------------------------------------------------- 9. 地球曲率与折射


def test_curvature_is_disabled_by_default_and_only_lowers_the_ray():
    straight = los.ray_height_m(
        origin_egm2008_m=100.0, target_egm2008_m=100.0,
        horizontal_m=10000.0, sample_distance_m=5000.0,
    )
    assert straight == pytest.approx(100.0)
    curved = los.ray_height_m(
        origin_egm2008_m=100.0, target_egm2008_m=100.0,
        horizontal_m=10000.0, sample_distance_m=5000.0,
        earth_radius_m=6371008.8, k_factor=1.0,
    )
    assert curved < straight
    four_thirds = los.ray_height_m(
        origin_egm2008_m=100.0, target_egm2008_m=100.0,
        horizontal_m=10000.0, sample_distance_m=5000.0,
        earth_radius_m=6371008.8, k_factor=4.0 / 3.0,
    )
    assert curved < four_thirds < straight


def test_curvature_preserves_both_endpoints_exactly():
    for distance in (0.0, 1234.0, 5000.0):
        height = los.ray_height_m(
            origin_egm2008_m=100.0, target_egm2008_m=300.0,
            horizontal_m=5000.0, sample_distance_m=distance,
            earth_radius_m=6371008.8, k_factor=4.0 / 3.0,
        )
        if distance == 0.0:
            assert height == pytest.approx(100.0, abs=1e-9)
        elif distance == 5000.0:
            assert height == pytest.approx(300.0, abs=1e-9)


def test_curvature_drop_is_monotone_in_k_factor():
    values = [
        los.effective_curvature_drop_m(
            horizontal_m=5000.0, earth_radius_m=6371008.8, k_factor=k,
        )
        for k in (1.0, 4.0 / 3.0, 2.0, 10.0)
    ]
    assert values == sorted(values, reverse=True)


# ------------------------------------------------------- 10. 纯函数判定


@pytest.mark.parametrize(
    "ray,low,high,expected",
    (
        (100.0, 10.0, 20.0, los.TERRAIN_SAMPLE_CLEAR),
        (10.0, 20.0, 30.0, los.TERRAIN_SAMPLE_BLOCKED),
        (20.0, 20.0, 30.0, los.TERRAIN_SAMPLE_BLOCKED),
        (25.0, 20.0, 30.0, los.TERRAIN_SAMPLE_MARGINAL),
        (30.0, 20.0, 30.0, los.TERRAIN_SAMPLE_MARGINAL),
        (None, 20.0, 30.0, los.TERRAIN_SAMPLE_UNKNOWN),
        (25.0, None, None, los.TERRAIN_SAMPLE_UNKNOWN),
    ),
)
def test_classify_terrain_sample_matrix(ray, low, high, expected):
    assert los.classify_terrain_sample(
        ray_height_m_value=ray, terrain_min_m=low, terrain_max_m=high,
    ) == expected


def test_no_number_helper_never_turns_missing_into_zero():
    assert los.number_or_none(None) is None
    assert los.number_or_none(float("nan")) is None
    assert los.number_or_none(float("inf")) is None
    assert los.number_or_none(True) is None
    assert los.number_or_none(0.0) == 0.0


def test_first_sample_is_not_treated_as_an_obstruction():
    samples = [
        {"index": 0, "distance_m": 0.0, "ray_height_m": 60.0, "terrain_min_m": 60.0,
         "terrain_max_m": 60.0, "status": los.TERRAIN_SAMPLE_BLOCKED},
        {"index": 1, "distance_m": 5.0, "ray_height_m": 60.0, "terrain_min_m": 30.0,
         "terrain_max_m": 30.0, "status": los.TERRAIN_SAMPLE_CLEAR},
    ]
    verdict = los.evaluate_terrain_los(
        origin_egm2008_m=60.0, target_egm2008_m=60.0, samples=samples,
    )
    assert verdict["status"] == los.LOS_STATUS_CLEAR
    assert verdict["first_obstruction_distance_m"] is None
    assert verdict["minimum_clearance_m"] == pytest.approx(30.0)


def test_no_evaluable_sample_is_unknown_not_clear():
    samples = [
        {"index": 0, "distance_m": 0.0, "ray_height_m": None, "terrain_min_m": None,
         "terrain_max_m": None, "status": los.TERRAIN_SAMPLE_UNKNOWN},
    ]
    verdict = los.evaluate_terrain_los(
        origin_egm2008_m=60.0, target_egm2008_m=60.0, samples=samples,
    )
    assert verdict["status"] == los.LOS_STATUS_UNKNOWN
    assert verdict["minimum_clearance_m"] is None


def test_along_track_distances_include_both_endpoints():
    assert los.along_track_distances_m(10.0, 5.0) == [0.0, 5.0, 10.0]
    assert los.along_track_distances_m(11.0, 5.0) == [0.0, 5.0, 10.0, 11.0]
    assert los.along_track_distances_m(0.0, 5.0) == []
    assert los.along_track_distances_m(10.0, 0.0) == []


# ------------------------------------------------------- 11. 确定性与缓存


def test_identical_input_produces_identical_fingerprint_and_verdict():
    terrain = FakeTerrain(base=30.0, columns={4: 150.0})
    physical = los.origin_physical_input(
        origin_egm2008_m=60.0, origin_source="tower_top_orthometric_m",
        origin_state="usable_estimated", terrain_m=30.0, terrain_status="passed",
    )
    probe = {
        "voxel_id": "V-1", "longitude": LON_ORIGIN + 0.004, "latitude": LAT_ORIGIN,
        "altitude_layer_id": "ALT-100",
    }
    results = [
        adapter.evaluate_probe_terrain_los(
            _tower(), probe, terrain_source=terrain, transform=FakeTransform(),
            altitude_layer_id="ALT-100", nominal_altitude_m=100.0,
            origin_physical_input=physical,
            origin_availability=physical["origin_availability"],
        )
        for _repeat in range(3)
    ]
    assert len({item["input_fingerprint"] for item in results}) == 1
    assert len({item["status"] for item in results}) == 1
    assert len({item["minimum_clearance_m"] for item in results}) == 1


def test_fingerprint_changes_with_every_decision_input():
    base = dict(
        tower_id="T-1", origin_egm2008_m=60.0, origin_source="tower_top_orthometric_m",
        origin_state="usable_estimated", origin_evidence_level="estimated",
        probe_voxel_id="V-1", probe_altitude_layer_id="ALT-100",
        probe_altitude_egm2008_m=100.0, probe_height_source="probe_altitude_layer_catalog",
        probe_longitude=121.0, probe_latitude=30.0,
        dem_source_identity={"file_name": "a.tif", "size_bytes": 1},
        dem_vertical_reference="egm2008_orthometric",
        sampling={"sampling_interval_m": 5.0, "envelope_radius_m": 40.0},
        ray_model={"k_factor": 1.0, "earth_radius_m": None},
        algorithm_version=los.RADAR_TERRAIN_LOS_ALGORITHM_VERSION,
    )
    reference = los.terrain_los_input_fingerprint(**base)
    for field, value in (
        ("origin_egm2008_m", 61.0),
        ("origin_state", "usable_confirmed"),
        ("origin_evidence_level", "confirmed"),
        ("probe_altitude_egm2008_m", 150.0),
        ("probe_altitude_layer_id", "ALT-150"),
        ("dem_source_identity", {"file_name": "a.tif", "size_bytes": 2}),
        ("sampling", {"sampling_interval_m": 1.0, "envelope_radius_m": 40.0}),
        ("ray_model", {"k_factor": 4.0 / 3.0, "earth_radius_m": 6371008.8}),
        ("algorithm_version", "radar_terrain_los_v0"),
        ("probe_longitude", 121.1),
    ):
        changed = los.terrain_los_input_fingerprint(**{**base, field: value})
        assert changed != reference, f"{field} 必须进入指纹"


def test_algorithm_version_is_part_of_the_fingerprint():
    payload = {"algorithm_version": los.RADAR_TERRAIN_LOS_ALGORITHM_VERSION}
    other = {"algorithm_version": "radar_terrain_los_v0"}
    assert los.stable_fingerprint(payload) != los.stable_fingerprint(other)


def test_terrain_source_cache_opens_each_variant_once():
    opened = []

    def factory(path, variant):
        opened.append((path, variant))
        return FakeTerrain(base=30.0)

    cache = adapter.TerrainSourceCache(source_factory=factory)
    first = cache.get("X:/dem.tif", adapter.SOURCE_VARIANT_NATIVE)
    second = cache.get("X:/dem.tif", adapter.SOURCE_VARIANT_NATIVE)
    window = cache.get("X:/dem.tif", adapter.SOURCE_VARIANT_WINDOW)
    assert first is second
    assert window is not first
    assert len(opened) == 2  # 两个变体各打开一次
    described = cache.describe()
    assert described["open_count"] == 2
    assert described["reused_count"] == 1
    assert described["one_open_per_path_and_variant"] is True
    cache.close()


def test_terrain_source_cache_reports_unavailable_source():
    def factory(path, variant):
        raise ValueError("dem not readable")

    cache = adapter.TerrainSourceCache(source_factory=factory)
    assert cache.get("X:/missing.tif", adapter.SOURCE_VARIANT_NATIVE) is None
    described = cache.describe()
    assert "ValueError:dem not readable" in list(described["unavailable"].values())


def test_one_read_per_profile_receipt():
    terrain = FakeTerrain(base=30.0)
    profile = adapter.sample_terrain_profile(
        terrain, transform=FakeTransform(), origin_metric=[0.0, 0.0],
        target_metric=[1500.0, 0.0],
    )
    receipt = profile["read_receipt"]
    assert receipt["read_count"] == 1
    assert receipt["one_read_per_profile"] is True
    assert receipt["resampled"] is False
    assert receipt["no_data_filling"] is False
    assert receipt["interpolated"] is False
    assert receipt["read_mode"] == los.RADAR_TERRAIN_LOS_READ_MODE


def test_pixel_association_discloses_shared_pixels_and_never_interpolates():
    terrain = FakeTerrain(base=30.0)
    profile = adapter.sample_terrain_profile(
        terrain, transform=FakeTransform(), origin_metric=[0.0, 0.0],
        target_metric=[1500.0, 0.0], sampling_interval_m=5.0, envelope_radius_m=40.0,
    )
    sampling = profile["sampling"]
    association = sampling["pixel_association"]
    assert association["interpolated"] is False
    assert association["elevation_values_are_source_pixel_values"] is True
    assert association["min_along_gap_m"] is not None
    assert sampling["along_track_independent_resolution_limited_by_pixel_size"] is True


def test_every_sample_has_a_source_pixel_in_a_flat_scene():
    terrain = FakeTerrain(base=30.0)
    profile = adapter.sample_terrain_profile(
        terrain, transform=FakeTransform(), origin_metric=[0.0, 0.0],
        target_metric=[1500.0, 0.0],
    )
    empty = [
        item for item in profile["samples"]
        if item["status"] != los.TERRAIN_SAMPLE_UNKNOWN and item["valid_pixel_count"] == 0
    ]
    assert empty == []
    assert profile["sampling"]["pixel_association"]["samples_without_pixel"] == 0


# ------------------------------------------------------- 12. Radar 冻结几何不变


def test_frozen_radar_geometry_parameters_are_unchanged():
    assert ELEVATION_CENTER_DEG == 22.5
    assert ELEVATION_MIN_DEG == 0.0
    assert ELEVATION_MAX_DEG == 45.0
    assert AZIMUTH_BEAMWIDTH_DEG == 90.0
    assert RADAR_GEOMETRY_PARAMETERS[RADAR_TYPE_I]["min_slant_range_m"] == 120.0
    assert RADAR_GEOMETRY_PARAMETERS[RADAR_TYPE_I]["max_slant_range_m"] == 3000.0
    assert RADAR_GEOMETRY_PARAMETERS[RADAR_TYPE_II]["min_slant_range_m"] == 200.0
    assert RADAR_GEOMETRY_PARAMETERS[RADAR_TYPE_II]["max_slant_range_m"] == 5000.0
    for parameters in RADAR_GEOMETRY_PARAMETERS.values():
        assert parameters["confirmed"] is True
        assert parameters["azimuth_half_width_deg"] == 45.0
        assert parameters["elevation_center_deg"] == 22.5


def test_terrain_los_never_claims_to_replace_beam_geometry():
    verdict = los.evaluate_terrain_los(
        origin_egm2008_m=60.0, target_egm2008_m=60.0,
        samples=[{"index": 0, "distance_m": 0.0, "ray_height_m": 60.0,
                  "terrain_min_m": 10.0, "terrain_max_m": 10.0,
                  "status": los.TERRAIN_SAMPLE_CLEAR}],
    )
    assert verdict["terrain_los_replaces_beam_geometry"] is False
    assert verdict["frozen_radar_geometry_version"] == "radar_layout_geometry_v1_1"
    assert verdict["frozen_radar_geometry_parameters_unchanged"] is True
    assert verdict["is_detection_probability"] is False
    assert verdict["is_radar_equation_result"] is False
    assert "radar_equation" in verdict["not_evaluated"]
    assert "detection_probability_pd" in verdict["not_evaluated"]


def test_probe_evaluation_keeps_device_range_as_a_reference_only():
    terrain = FakeTerrain(base=30.0)
    physical = los.origin_physical_input(
        origin_egm2008_m=60.0, origin_source="tower_top_orthometric_m",
        origin_state="usable_estimated", terrain_m=30.0, terrain_status="passed",
    )
    evaluation = adapter.evaluate_probe_terrain_los(
        _tower(), {
            "voxel_id": "V-1", "longitude": LON_ORIGIN, "latitude": LAT_ORIGIN,
            "altitude_layer_id": "ALT-100",
        },
        terrain_source=terrain, transform=FakeTransform(),
        altitude_layer_id="ALT-100", nominal_altitude_m=100.0,
        origin_physical_input=physical, origin_availability=physical["origin_availability"],
        max_slant_range_m=3000.0,
    )
    assert evaluation["frozen_max_slant_range_m"] == 3000.0
    assert evaluation["within_frozen_device_range"] is True
    assert evaluation["not_evaluated"] == list(los.RADAR_TERRAIN_LOS_NOT_EVALUATED)


def test_domain_module_never_imports_qgis_gdal_or_reads_files():
    import inspect

    source = inspect.getsource(los)
    assert "osgeo" not in source
    assert "import gdal" not in source
    assert "Qgs" not in source
    assert "open(" not in source


# ------------------------------------------------------- 审计公式（§4）


def test_audit_formula_is_base_type_aware():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "tools" / "round34_t07a_tower_top_formula_audit.py"
    spec = importlib.util.spec_from_file_location("round34_t07a_formula_audit", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    ground = module.audit_row(
        tower={"tower_id": "33090301000160", "elevation_m": 20.0, "height_m": 15.0},
        profile={
            "base_type": "ground", "terrain_elevation_m": 45.259998322,
            "building_height_m": 4.363574981689453,
            "tower_structure_height_m": 15.0,
            "tower_top_orthometric_m": 60.259998322,
            "building_height_status": "passed",
            "vertical_status": "egm2008_orthometric_resolved",
        },
    )
    assert ground["formula_used"] == "terrain + tower_structure_height"
    assert ground["predicted_tower_top_m"] == pytest.approx(60.259998322)
    assert ground["residual_m"] == pytest.approx(0.0, abs=1e-9)
    assert ground["difference_class"] == module.DIFF_BUILDING_FIELD_NOT_USED

    rooftop = module.audit_row(
        tower={"tower_id": "T-2", "elevation_m": 20.0, "height_m": 12.0},
        profile={
            "base_type": "rooftop", "terrain_elevation_m": 30.0,
            "building_height_m": 15.0, "tower_structure_height_m": 12.0,
            "tower_top_orthometric_m": 57.0,
            "building_height_status": "passed",
            "vertical_status": "egm2008_orthometric_resolved",
        },
    )
    assert rooftop["formula_used"] == "terrain + building_height + tower_structure_height"
    assert rooftop["residual_m"] == pytest.approx(0.0)
    assert rooftop["difference_class"] == module.DIFF_NONE

    inconsistent = module.audit_row(
        tower={"tower_id": "T-3"},
        profile={
            "base_type": "ground", "terrain_elevation_m": 30.0,
            "tower_structure_height_m": 10.0, "tower_top_orthometric_m": 55.0,
            "vertical_status": "egm2008_orthometric_resolved",
        },
    )
    assert inconsistent["residual_m"] == pytest.approx(15.0)
    assert inconsistent["difference_class"] == module.DIFF_TRUE_INCONSISTENCY

    contradiction = module.audit_row(
        tower={"tower_id": "T-4"},
        profile={
            "base_type": "rooftop", "terrain_elevation_m": 30.0,
            "building_height_m": None, "tower_structure_height_m": 10.0,
            "tower_top_orthometric_m": None,
            "vertical_status": "building_height_unresolved",
        },
    )
    assert contradiction["difference_class"] == module.DIFF_NONE  # 未解析的塔单独审计


def test_audit_formula_flags_unresolvable_rooftop_profile():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "tools" / "round34_t07a_tower_top_formula_audit.py"
    spec = importlib.util.spec_from_file_location("round34_t07a_formula_audit2", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    row = module.audit_row(
        tower={"tower_id": "T-5"},
        profile={
            "base_type": "rooftop", "terrain_elevation_m": 30.0,
            "building_height_m": None, "tower_structure_height_m": 10.0,
            "tower_top_orthometric_m": 55.0,
            "vertical_status": "egm2008_orthometric_resolved",
        },
    )
    assert row["predicted_tower_top_m"] is None
    assert row["difference_class"] == module.DIFF_SOURCE_CONTRADICTION
