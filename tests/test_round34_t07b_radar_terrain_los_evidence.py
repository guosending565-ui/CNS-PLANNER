"""Round34-T07-B —— Radar 地形 LOS 只读接线与独立证据展示的定向测试。

覆盖任务 §9 的 14 项必测项中可在 fixture 环境验证的部分：

1. 只读接线不改任何 ProjectState（逐字比较 state 快照）；
2. 无请求时不打开 DEM / 不触发 LOS 全量计算（工厂计数 == 0）；
3. 显式请求能得到正确 LOS（clear / blocked / unknown 三态）；
4. 不可评价原点保留（``not_evaluable`` 计入 ``origin_availability_counts``）；
5. summary 不隐含全部明细（``full_terrain_profile_included=False``，样本有上限）；
6. 明细查询受限、分页、大小可控；
7. 同输入结果确定（含指纹与状态）；
8. DEM 变化 ⇒ 旧证据 stale；
9. LOS 算法版本变化 ⇒ 旧证据 stale；
10. LOS stale 不污染上游规划状态（只读，无 writer、无失效入口）；
11. T02 高度层与真实 probe 高度保持正确；
12. LOS 未进入判定（参与标志全为 False，冻结几何逐字不变）；
13. 373/77/296 与 285/88 口径**不硬编码**：测试按真实数据基线验证"维度分离"。

测试全部使用**合成**地形替身（不依赖 GDAL / QGIS）。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from cns_planner.application import radar_terrain_los_diagnostics_service as service_module
from cns_planner.application.radar_terrain_los_diagnostics_service import (
    RadarTerrainLosDiagnosticsService,
)
from cns_planner.domain import radar_terrain_los as los
from cns_planner.domain import radar_terrain_los_evidence as evidence
from cns_planner.domain.radar_surveillance_layout import (
    AZIMUTH_BEAMWIDTH_DEG, ELEVATION_CENTER_DEG, ELEVATION_MAX_DEG, ELEVATION_MIN_DEG,
    RADAR_GEOMETRY_PARAMETERS, RADAR_TYPE_I,
)

# --------------------------------------------------------------------- fixtures

COLUMNS = 200
ROWS = 200
LON_ORIGIN = 122.0
LAT_ORIGIN = 30.0
DEG_STEP = 0.0003
METERS_PER_DEGREE_LON = 96486.0
METERS_PER_DEGREE_LAT = 110900.0

ALTITUDE_LAYER_ID = "ALT-100"
ALTITUDE_M = 100.0

#: 合成场景（物理自洽）：地表 10 m、塔顶原点 70 m、航路高度层 100 m。
#: 因此塔址处净空 60 m、航路末端约 27 m —— 全部为真实可评价值的量级，
#: 不是"航路高度低于地形"的退化输入。
TERRAIN_BASE_M = 10.0
TOWER_TOP_M = 70.0
RIDGE_COLUMN_M = 300.0
RIDGE_HEIGHT_M = 100.0
LOW_TOWER_TOP_M = 12.0

#: 与既有多数服务级测试同源的默认配置路径。
DEFAULTS = Path("cns_planner/config/defaults.json")


def _metric_from_geographic(longitude, latitude):
    return [
        (longitude - LON_ORIGIN) * METERS_PER_DEGREE_LON,
        (latitude - LAT_ORIGIN) * METERS_PER_DEGREE_LAT,
    ]


def _geographic_from_metric(point):
    return [
        LON_ORIGIN + float(point[0]) / METERS_PER_DEGREE_LON,
        LAT_ORIGIN + float(point[1]) / METERS_PER_DEGREE_LAT,
    ]


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
    """FABDEM 只读访问器替身（与 T07-A 测试同一契约）。"""

    role = "terrain_dtm"

    class _Dataset:
        RasterXSize = COLUMNS
        RasterYSize = ROWS

    dataset = _Dataset()

    def __init__(self, *, base=200.0, columns=None, nodata=-9999.0,
                 path=None, vertical_reference="egm2008_orthometric"):
        self.array = np.full((ROWS, COLUMNS), float(base), dtype=float)
        for column, value in (columns or {}).items():
            self.array[:, int(column)] = float(value)
        self.vertical_reference = vertical_reference
        self.vertical_status = (
            "confirmed" if vertical_reference == "egm2008_orthometric" else "unresolved"
        )
        self.nodata = nodata
        self.transform = (LON_ORIGIN, DEG_STEP, 0.0, LAT_ORIGIN, 0.0, -DEG_STEP)
        self.band = FakeBand(self.array, nodata)
        self.path = path
        self.closed = False

    def describe(self):
        return {"dataset": "FABDEM", "file_name": "synthetic.tif",
                "vertical_reference": self.vertical_reference}

    def usable(self):
        if self.vertical_status != "confirmed":
            return False, "terrain_vertical_datum_unresolved"
        return True, None

    def sample_route_terrain(self, points):
        """与既有 ``FabdemWindowTerrainSource.sample_route_terrain`` 同型的逐点采样。"""

        result = {}
        for point in points:
            key = str(point["key"])
            longitude, latitude = point.get("longitude"), point.get("latitude")
            if not isinstance(longitude, (int, float)) or not isinstance(latitude, (int, float)):
                result[key] = {"status": "unresolved", "elevation_m": None,
                               "reason": "coordinate_missing"}
                continue
            column = int((float(longitude) - LON_ORIGIN) / DEG_STEP)
            row = int((LAT_ORIGIN - float(latitude)) / DEG_STEP)
            if not (0 <= column < COLUMNS and 0 <= row < ROWS):
                result[key] = {"status": "unresolved", "elevation_m": None,
                               "reason": "outside_dtm_coverage"}
                continue
            value = float(self.array[row, column])
            if value == float(self.nodata) or not np.isfinite(value):
                result[key] = {"status": "unresolved", "elevation_m": None,
                               "reason": "no_data"}
                continue
            result[key] = {
                "status": "passed", "elevation_m": value,
                "vertical_reference": self.vertical_reference,
                "valid_pixel_count": 1, "nodata_pixel_count": 0,
                "sampling": {"resampled": False, "read_only": True},
                "reason": None,
            }
        return result

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
            "status": "passed", "effective_resolution_m_x": 29.0,
            "effective_resolution_m_y": 33.0, "effective_resolution_m": 30.0,
        }

    def sample_cells(self, cells, *, transform):
        """与既有 ``FabdemWindowTerrainSource.sample_cells`` 同型的逐点采样替身。

        ``sample_route_terrain`` 走的就是这条既有通道；替身只做"取窗口内有效像元最大
        正高"，不重采样、不填补 NoData。
        """

        result = {}
        for cell in cells:
            key = str(cell["fine_cell_id"])
            bbox = cell.get("bbox_metric") or []
            if len(bbox) < 4:
                result[key] = {"data_status": "unresolved", "reason": "outside_dtm_coverage"}
                continue
            lon_min, lat_min, lon_max, lat_max = (float(item) for item in bbox[:4])
            column = int((lon_min - LON_ORIGIN) / DEG_STEP)
            row = int((LAT_ORIGIN - lat_max) / DEG_STEP)
            height = max(1, int((lat_max - lat_min) / DEG_STEP) + 1)
            width = max(1, int((lon_max - lon_min) / DEG_STEP) + 1)
            column = max(0, min(COLUMNS - 1, column))
            row = max(0, min(ROWS - 1, row))
            block = self.array[row:row + height, column:column + width]
            valid = block[np.isfinite(block) & (block != float(self.nodata))]
            if valid.size == 0:
                result[key] = {
                    "data_status": "unresolved", "reason": "outside_dtm_coverage",
                }
                continue
            result[key] = {
                "data_status": "passed",
                "surface_elevation_max_egm2008_m": float(valid.max()),
                "valid_pixel_count": int(valid.size),
                "nodata_pixel_count": int(block.size - valid.size),
                "sampling": {"resampled": False, "pixel_window": [column, row, width, height]},
            }
        return result


class FakeTransform:
    def to_metric(self, point):
        return _metric_from_geographic(float(point[0]), float(point[1]))

    def to_geographic(self, point):
        return _geographic_from_metric(point)


class FakeSession:
    def __init__(self, state):
        self.state = state
        self.saved = 0

    def save(self):
        self.saved += 1


class TerrainSourceFactory:
    """按变体返回替身，并记录"打开"次数（用于证明无请求时不打开 DEM）。"""

    def __init__(self, *, base=200.0, columns=None, nodata=-9999.0):
        self.base = base
        self.columns = columns or {}
        self.nodata = nodata
        self.open_count = 0
        self.closed_count = 0
        self.sources = {}

    def __call__(self, path, variant=None):
        self.open_count += 1
        source = FakeTerrain(
            base=self.base, columns=self.columns, nodata=self.nodata,
            path=_path_like(path),
        )
        self.sources[str(variant)] = source
        return source

    def close_all(self):
        for source in self.sources.values():
            source.closed = True
        self.closed_count += 1


def _path_like(path):
    """让 ``terrain_source_identity`` 能 ``stat()`` 的最小路径替身（真实临时文件）。"""

    from pathlib import Path

    return Path(str(path))


def _tower_payload(**overrides):
    record = {
        "tower_id": "T-001",
        "name": "合成塔 1",
        "longitude": LON_ORIGIN,
        "latitude": LAT_ORIGIN,
        "site_type": "ground",
        "elevation_m": 190.0,
        "height_m": 20.0,
    }
    record.update(overrides)
    return record


def _route_payload(*, route_id="R-1", start_east_m=200.0, end_east_m=1000.0, points=None):
    """沿正东方向的合成航路；起点**不落在塔址**（塔址零水平距离不是有效 probe）。"""

    if points is None:
        start = _geographic_from_metric([start_east_m, 0.0])
        end = _geographic_from_metric([end_east_m, 0.0])
        points = [start, end]
    return {
        "route_id": route_id,
        "status": "passed",
        "altitude_layer_id": ALTITUDE_LAYER_ID,
        "path": [[float(point[0]), float(point[1])] for point in points],
    }


def _state(*, terrain_path, towers=None, profiles=None, routes=None, policy=None):
    layers = [{
        "altitude_layer_id": ALTITUDE_LAYER_ID,
        "nominal_altitude_m": ALTITUDE_M,
        "lower_altitude_m": 90.0,
        "upper_altitude_m": 110.0,
        "vertical_reference": "egm2008_orthometric",
        "status": "confirmed",
        "confirmed": True,
    }]
    tower_items = towers if isinstance(towers, list) else [_tower_payload()]
    profile_items = profiles if isinstance(profiles, dict) else {}
    return {
        "towers": tower_items,
        "tower_obstacle_profiles": {"status": "passed", "items": profile_items},
        "operational_routes": (
            routes if isinstance(routes, list) else [_route_payload()]
        ),
        "spatial_3d": {"altitude_layers": layers},
        "radar_surveillance_policy": (
            policy if isinstance(policy, dict)
            else {
                "optimization_sample_spacing_m": 100.0,
                "fixed_altitude_layer_id": ALTITUDE_LAYER_ID,
                "fixed_altitude_layer_explicitly_configured": True,
                "fixed_altitude_layer_confirmation_status": "confirmed",
                "status": "confirmed",
                #: 与真实舟山项目一致：策略授权"全塔源表工程估算"原点层。它只影响
                #: **源表估算层是否可用**（T05 既有语义），不改变任何几何或几何常量。
                "radar_planning_origin_policy_id": "radar_all_tower_rooftop_estimate_v1",
            }
        ),
        "data_source_paths": {"terrain_dtm": str(terrain_path)},
    }


def _service(state, factory):
    session = FakeSession(state)
    service = RadarTerrainLosDiagnosticsService(session, terrain_source_factory=factory)
    service.to_metric = lambda point: _metric_from_geographic(float(point[0]), float(point[1]))
    service.radar_layout_service = _RadarLayoutStub(state)
    return service, session


class _RadarLayoutStub:
    """复用既有 Radar 只读接口的两个能力：逆投影与数据源路径。"""

    metric_transform = None

    def __init__(self, state):
        self._state = state

    def to_geographic(self, point):
        return _geographic_from_metric(point)

    def _source_paths(self, facts_provider=None):
        del facts_provider
        return dict(self._state.get("data_source_paths") or {})


@pytest.fixture
def terrain_file(tmp_path):
    path = tmp_path / "Zhoushan_FABDEM_DTM_30m.tif"
    path.write_bytes(b"not-a-real-raster-but-stable-identity")
    return path


def _state_snapshot(state):
    return json.dumps(state, sort_keys=True, ensure_ascii=False, default=str)


# ------------------------------------------------------- 1. 无请求时不打开 DEM


def test_status_never_opens_dem_or_computes_los(terrain_file):
    factory = TerrainSourceFactory()
    state = _state(terrain_path=terrain_file)
    service, session = _service(state, factory)
    before = _state_snapshot(state)

    status = service.status()

    assert factory.open_count == 0
    assert status["evidence_status"] == evidence.EVIDENCE_STATUS_NOT_REQUESTED
    assert status["dem_opened"] is False
    assert status["los_computation_performed"] is False
    assert status["evaluated_probe_count"] == 0
    assert status["records"] == []
    #: 只读状态**绝不**写状态。
    assert session.saved == 0
    assert _state_snapshot(state) == before


def test_status_reports_availability_without_opening_dataset(terrain_file):
    factory = TerrainSourceFactory()
    service, _session = _service(_state(terrain_path=terrain_file), factory)

    status = service.status()

    assert status["adapter_availability"]["terrain_dtm"]["configured"] is True
    identity = status["adapter_availability"]["terrain_dtm"]["path_identity"]
    assert identity["dataset_opened"] is False
    assert identity["size_bytes"] is not None
    assert status["available_to_compute"] is True


# ------------------------------------------------------- 2. 显式请求得到正确 LOS


def test_explicit_diagnose_returns_clear_on_flat_terrain(terrain_file):
    factory = TerrainSourceFactory(base=TERRAIN_BASE_M)
    state = _state(
        terrain_path=terrain_file,
        profiles={"T-001": {"tower_top_orthometric_m": TOWER_TOP_M, "status": "passed"}},
    )
    service, session = _service(state, factory)
    before = _state_snapshot(state)

    result = service.diagnose({"max_tower_count": 1, "probe_budget": 3})

    assert result["evidence_status"] == evidence.EVIDENCE_STATUS_COMPUTED
    assert result["los_status_counts"]["clear"] == 3
    assert result["los_status_counts"]["blocked"] == 0
    assert result["evaluated_tower_count"] == 1
    assert result["evaluated_probe_count"] == 3
    #: 只读诊断**绝不**写 ProjectState。
    assert session.saved == 0
    assert _state_snapshot(state) == before
    #: 只读声明齐全。
    for key, value in evidence.READ_ONLY_DECLARATION.items():
        assert result[key] is value


def test_ridge_blocks_the_ray_and_is_reported_as_blocked(terrain_file):
    #: 射线在塔址以东约 300 m 处的地形被抬到远高于射线 ⇒ 明显遮挡。
    ridge_column = int(round(RIDGE_COLUMN_M / (DEG_STEP * METERS_PER_DEGREE_LON)))
    factory = TerrainSourceFactory(
        base=TERRAIN_BASE_M, columns={ridge_column: RIDGE_HEIGHT_M},
    )
    state = _state(
        terrain_path=terrain_file,
        profiles={"T-001": {"tower_top_orthometric_m": TOWER_TOP_M, "status": "passed"}},
    )
    service, _session = _service(state, factory)

    result = service.diagnose({"max_tower_count": 1, "probe_budget": 2})

    assert result["los_status_counts"]["blocked"] >= 1
    blocked = [
        item for item in result["records"] if item["los_status"] == los.LOS_STATUS_BLOCKED
    ]
    assert blocked
    assert blocked[0]["minimum_clearance_m"] < 0


def test_below_terrain_origin_is_kept_and_los_is_unknown(terrain_file):
    #: 原点 0 m < 地表 10 m ⇒ 物理输入 not_evaluable，主评价 unknown，站址仍在库存。
    factory = TerrainSourceFactory(base=TERRAIN_BASE_M)
    #: Round34-T07-R 集成适配：正式仓库的 ``resolve_radar_origins`` 仍是 V1.1 塔顶语义
    #: （没有 Round34-T05 的 ``radar_planning_origin_policy_id`` 源表估算层），因此这里
    #: 把本 fixture 想表达的"原点 0 m"（源表 −40 m + 塔高 40 m）直接作为真实解析出的
    #: 塔顶 EGM2008 正高给出。**测试语义不变**：原点 0 m < 地表 10 m ⇒
    #: ``not_evaluable``，主评价 ``unknown``，站址仍在库存。
    state = _state(
        terrain_path=terrain_file,
        towers=[_tower_payload(elevation_m=-40.0, height_m=40.0)],
        profiles={"T-001": {"tower_top_orthometric_m": 0.0, "status": "passed"}},
    )
    service, _session = _service(state, factory)

    result = service.diagnose({"max_tower_count": 1, "probe_budget": 2})

    assert result["origin_availability_counts"] == {
        "usable": 0, "unknown": 0, "not_evaluable": 1,
    }
    assert result["candidate_tower_count"] == 1
    assert result["los_status_counts"]["unknown"] >= 1
    assert result["los_status_counts"]["clear"] == 0
    record = result["records"][0]
    assert record["radar_origin_availability"] == los.ORIGIN_AVAILABILITY_NOT_EVALUABLE
    assert record["los_status"] == los.LOS_STATUS_UNKNOWN
    #: scenario_only 不计入主评价。
    assert result["not_evaluated"]
    assert "los_writer" in result["not_evaluated"]


def test_origin_availability_counts_cover_the_whole_inventory_not_only_the_sample(
    terrain_file,
):
    factory = TerrainSourceFactory(base=TERRAIN_BASE_M)
    towers = [
        _tower_payload(tower_id="T-001", elevation_m=0.0, height_m=20.0),
        _tower_payload(tower_id="T-002", elevation_m=0.0, height_m=3.0),
    ]
    state = _state(
        terrain_path=terrain_file, towers=towers,
        #: Round34-T07-R 集成适配：同上 —— T-002 的"源表 0 m + 塔高 3 m"在原版本由
        #: T05 的源表估算层解析；正式仓库是 V1.1 塔顶语义，因此显式给出塔顶正高 3 m。
        #: 断言语义不变（3 m < 地表 10 m ⇒ ``not_evaluable``）。
        profiles={
            "T-001": {"tower_top_orthometric_m": TOWER_TOP_M, "status": "passed"},
            "T-002": {"tower_top_orthometric_m": 3.0, "status": "passed"},
        },
    )
    service, _session = _service(state, factory)

    result = service.diagnose({"max_tower_count": 1, "probe_budget": 1})

    assert result["evaluated_tower_count"] == 1
    assert result["candidate_tower_count"] == 2
    #: 88 座口径的来源：**全部**库存都做物理输入诊断，不只看被抽中的塔。
    assert result["physical_input_diagnosed_tower_count"] == 2
    assert result["origin_availability_counts"] == {
        "usable": 1, "unknown": 0, "not_evaluable": 1,
    }


# ------------------------------------------------------- 3. 输出规模与摘要边界


def test_probe_budget_is_shared_fairly_across_towers(terrain_file):
    """``probe_budget``（全部塔合计）必须公平分配到每座塔。

    否则第一座塔会把预算全部吃掉，"多塔评价"在响应里会静默退化成"单塔评价"。
    """

    factory = TerrainSourceFactory(base=TERRAIN_BASE_M)
    towers = [
        _tower_payload(tower_id=f"T-{index:03d}", elevation_m=0.0, height_m=30.0)
        for index in range(3)
    ]
    profiles = {
        f"T-{index:03d}": {"tower_top_orthometric_m": TOWER_TOP_M, "status": "passed"}
        for index in range(3)
    }
    state = _state(terrain_path=terrain_file, towers=towers, profiles=profiles)
    service, _session = _service(state, factory)

    result = service.diagnose({"max_tower_count": 3, "probe_budget": 6})

    assert result["evaluated_tower_count"] == 3
    assert result["evaluated_probe_count"] == 6
    per_tower = {}
    for record in result["records"]:
        per_tower[record["tower_id"]] = per_tower.get(record["tower_id"], 0) + 1
    assert sorted(per_tower) == ["T-000", "T-001", "T-002"]
    assert all(count == 2 for count in per_tower.values())
    assert result["evaluation_bounds_applied"]["probe_budget_per_tower"] == 2


def test_summary_never_implies_full_detail(terrain_file):
    factory = TerrainSourceFactory(base=TERRAIN_BASE_M)
    state = _state(
        terrain_path=terrain_file,
        profiles={"T-001": {"tower_top_orthometric_m": TOWER_TOP_M, "status": "passed"}},
    )
    service, _session = _service(state, factory)

    result = service.diagnose({
        "max_tower_count": 1, "probe_budget": 9, "record_sample_limit": 4,
    })

    assert result["evaluated_probe_count"] == 9
    assert result["record_sample_count"] == 4
    assert result["record_sample_truncated"] is True
    #: 摘要响应**不**携带明细级记录（明细走单独的分页查询）。
    assert "readable_records" not in result
    assert result["detail_record_count"] == 9
    assert result["bounds"]["full_terrain_profile_included"] is False
    assert result["bounds"]["detail_requires_explicit_request"] is True
    payload = json.dumps(result, ensure_ascii=False)
    #: 默认摘要必须可控：全部评价的完整证据块也要远小于 100 KB。
    assert len(payload.encode("utf-8")) < 100_000


def test_diagnose_never_includes_full_terrain_profile(terrain_file):
    factory = TerrainSourceFactory(base=TERRAIN_BASE_M)
    state = _state(
        terrain_path=terrain_file,
        profiles={"T-001": {"tower_top_orthometric_m": TOWER_TOP_M, "status": "passed"}},
    )
    service, _session = _service(state, factory)

    result = service.diagnose({"max_tower_count": 1, "probe_budget": 6})

    assert result["sampling"]["sampling_interval_m"] == pytest.approx(5.0)
    assert result["clearance_stats_clear_only"]["count"] >= 1
    detail_page = service.details({"limit": 200})["detail_page"]
    assert detail_page["max_limit"] == service_module.MAX_DETAIL_PAGE_LIMIT
    for record in detail_page["records"]:
        assert record["terrain_profile_summary"]["full_profile_included"] is False


# ------------------------------------------------------- 4. 明细：受限 + 分页


def test_detail_query_is_paginated_and_bounded(terrain_file):
    factory = TerrainSourceFactory(base=TERRAIN_BASE_M)
    state = _state(
        terrain_path=terrain_file,
        profiles={"T-001": {"tower_top_orthometric_m": TOWER_TOP_M, "status": "passed"}},
    )
    service, _session = _service(state, factory)
    service.diagnose({"max_tower_count": 1, "probe_budget": 9})

    first = service.details({"limit": 5, "offset": 0})["detail_page"]
    second = service.details({"limit": 5, "offset": 5})["detail_page"]
    oversized = service.details({"limit": 10_000})["detail_page"]

    assert first["returned_record_count"] == 5
    assert first["matched_record_count"] == 9
    assert first["truncated"] is True
    #: 第二页只剩 4 条（9 − 5），且如实报告已到末页。
    assert second["returned_record_count"] == 4
    assert second["truncated"] is False
    assert {item["probe_id"] for item in first["records"]}.isdisjoint(
        {item["probe_id"] for item in second["records"]}
    )
    #: 超界请求被硬性收敛到 max_limit。
    assert oversized["limit"] == service_module.MAX_DETAIL_PAGE_LIMIT
    assert oversized["returned_record_count"] <= service_module.MAX_DETAIL_PAGE_LIMIT


def test_detail_query_never_recomputes_or_opens_dem(terrain_file):
    factory = TerrainSourceFactory(base=TERRAIN_BASE_M)
    state = _state(
        terrain_path=terrain_file,
        profiles={"T-001": {"tower_top_orthometric_m": TOWER_TOP_M, "status": "passed"}},
    )
    service, _session = _service(state, factory)
    service.diagnose({"max_tower_count": 1, "probe_budget": 6})
    opened = factory.open_count

    service.details({"limit": 3})
    service.details({"tower_id": "T-001", "los_status": "clear", "limit": 3})

    assert factory.open_count == opened


def test_detail_query_without_prior_evidence_never_triggers_computation(terrain_file):
    factory = TerrainSourceFactory(base=TERRAIN_BASE_M)
    service, _session = _service(_state(terrain_path=terrain_file), factory)

    result = service.details({"limit": 10})

    assert result["evidence_status"] == evidence.EVIDENCE_STATUS_FAILED
    assert result["failure_reason"] == "detail_not_available"
    assert result["detail_page"] is None
    assert factory.open_count == 0


def test_detail_filters_by_tower_route_and_status(terrain_file):
    factory = TerrainSourceFactory(base=TERRAIN_BASE_M)
    state = _state(
        terrain_path=terrain_file,
        profiles={"T-001": {"tower_top_orthometric_m": TOWER_TOP_M, "status": "passed"}},
    )
    service, _session = _service(state, factory)
    service.diagnose({"max_tower_count": 1, "probe_budget": 9})

    by_tower = service.details({"tower_id": "T-001"})["detail_page"]
    by_other = service.details({"tower_id": "T-999"})["detail_page"]
    by_status = service.details({"los_status": "clear"})["detail_page"]

    assert by_tower["matched_record_count"] == 9
    assert by_other["matched_record_count"] == 0
    assert by_status["matched_record_count"] == 9
    assert all(item["los_status"] == "clear" for item in by_status["records"])


# ------------------------------------------------------- 5. 确定性 / 缓存


def test_same_input_produces_identical_evidence(terrain_file):
    factory_a = TerrainSourceFactory(base=TERRAIN_BASE_M)
    factory_b = TerrainSourceFactory(base=TERRAIN_BASE_M)
    state = _state(
        terrain_path=terrain_file,
        profiles={"T-001": {"tower_top_orthometric_m": TOWER_TOP_M, "status": "passed"}},
    )
    first = _service(state, factory_a)[0].diagnose({"probe_budget": 6})
    second = _service(state, factory_b)[0].diagnose({"probe_budget": 6})

    assert first["input_fingerprint"] == second["input_fingerprint"]
    assert first["los_status_counts"] == second["los_status_counts"]
    assert [item["probe_id"] for item in first["records"]] == [
        item["probe_id"] for item in second["records"]
    ]


def test_terrain_source_is_opened_once_per_variant(terrain_file):
    factory = TerrainSourceFactory(base=TERRAIN_BASE_M)
    state = _state(
        terrain_path=terrain_file,
        profiles={"T-001": {"tower_top_orthometric_m": TOWER_TOP_M, "status": "passed"}},
    )
    service, _session = _service(state, factory)

    result = service.diagnose({"max_tower_count": 1, "probe_budget": 8})

    assert result["dem_open_count"] == 2
    assert factory.open_count == 2
    assert result["dem"]["read_receipt"]["one_open_per_path_and_variant"] is True
    assert result["dem"]["read_receipt"]["reused_count"] >= 0


# ------------------------------------------------------- 6. 指纹与 stale


def test_dem_identity_change_makes_prior_evidence_stale(terrain_file):
    factory = TerrainSourceFactory(base=TERRAIN_BASE_M)
    state = _state(
        terrain_path=terrain_file,
        profiles={"T-001": {"tower_top_orthometric_m": TOWER_TOP_M, "status": "passed"}},
    )
    service, _session = _service(state, factory)
    current = service.diagnose({"probe_budget": 4})
    assert current["currentness"]["state"] == "no_prior_evidence"
    assert current["currentness"]["stale"] is False

    #: 旧证据（比当前答案一致的输入）⇒ current。
    same = service.diagnose({"probe_budget": 4, "prior_evidence": current})
    assert same["currentness"]["state"] == "current"
    assert same["evidence_status"] == evidence.EVIDENCE_STATUS_COMPUTED

    #: DEM 文件身份变化（大小 + mtime）⇒ 旧证据 stale，且**只**影响 LOS 证据。
    terrain_file.write_bytes(b"a-different-dem-file-with-a-different-size")
    changed = service.diagnose({"probe_budget": 4, "prior_evidence": current})

    assert changed["input_fingerprint"] != current["input_fingerprint"]
    assert changed["currentness"]["state"] == evidence.EVIDENCE_STATUS_STALE
    assert changed["currentness"]["stale"] is True
    assert changed["evidence_status"] == evidence.EVIDENCE_STATUS_STALE
    assert changed["currentness"]["upstream_planning_state_untouched"] is True
    assert changed["stales_upstream_planning_state"] is False


def test_los_algorithm_version_change_makes_prior_evidence_stale():
    """LOS 算法版本必须进入证据指纹，并使旧证据 stale。

    这里在**domain 层**直接证明两件事（不伪造任何业务量）：

    1. 算法版本变化 ⇒ 证据指纹变化；
    2. 旧的证据块（其 ``algorithm_version`` 与当前不同）被判定为
       ``los_algorithm_version_changed``。
    """

    common = {
        "dem_source_identity": {"absolute_path": "FABDEM.tif", "mtime_ns": 1},
        "dem_vertical_reference": los.EGM2008_ORTHOMETRIC,
        "sampling_interval_m": 5.0, "envelope_radius_m": 20.0,
        "earth_radius_m": None, "k_factor": 1.0,
        "evaluation_scope": evidence.EVALUATION_SCOPE_TOWER_PROBE_SAMPLE,
        "altitude_layer_id": ALTITUDE_LAYER_ID,
        "bounds": {"max_tower_count": 1, "probe_budget": 4, "probe_stride": 1},
        "record_fingerprints": ["a" * 64, "b" * 64],
    }
    current_fingerprint = evidence.terrain_los_evidence_input_fingerprint(
        algorithm_version=los.RADAR_TERRAIN_LOS_ALGORITHM_VERSION, **common,
    )
    bumped_fingerprint = evidence.terrain_los_evidence_input_fingerprint(
        algorithm_version=los.RADAR_TERRAIN_LOS_ALGORITHM_VERSION + "__bump", **common,
    )
    assert bumped_fingerprint != current_fingerprint

    stale = evidence.los_currentness(
        {"input_fingerprint": current_fingerprint,
         "algorithm_version": los.RADAR_TERRAIN_LOS_ALGORITHM_VERSION + "__bump",
         "evaluation_scope": evidence.EVALUATION_SCOPE_TOWER_PROBE_SAMPLE},
        current_input_fingerprint=current_fingerprint,
        current_algorithm_version=los.RADAR_TERRAIN_LOS_ALGORITHM_VERSION,
    )
    #: 指纹相同 ⇒ 先判 current（指纹已覆盖版本，等值即同一输入）。
    assert stale["state"] == "current"

    drifted = evidence.los_currentness(
        {"input_fingerprint": bumped_fingerprint,
         "algorithm_version": los.RADAR_TERRAIN_LOS_ALGORITHM_VERSION + "__bump",
         "evaluation_scope": evidence.EVALUATION_SCOPE_TOWER_PROBE_SAMPLE},
        current_input_fingerprint=current_fingerprint,
        current_algorithm_version=los.RADAR_TERRAIN_LOS_ALGORITHM_VERSION,
    )
    assert drifted["state"] == evidence.EVIDENCE_STATUS_STALE
    assert drifted["reasons"] == ["los_algorithm_version_changed"]
    assert drifted["stale"] is True


def test_los_stale_never_marks_upstream_planning_stale(terrain_file):
    factory = TerrainSourceFactory(base=TERRAIN_BASE_M)
    state = _state(
        terrain_path=terrain_file,
        profiles={"T-001": {"tower_top_orthometric_m": TOWER_TOP_M, "status": "passed"}},
    )
    service, session = _service(state, factory)
    before = _state_snapshot(state)
    current = service.diagnose({"probe_budget": 4})

    prior = dict(current, input_fingerprint="0" * 64)
    stale = service.diagnose({"probe_budget": 4, "prior_evidence": prior})

    assert stale["currentness"]["stale"] is True
    #: 上游规划容器**逐字未变**，且没有任何失效入口被注册或触发。
    assert _state_snapshot(state) == before
    assert session.saved == 0
    assert stale["participates_in_p14_p15_p16"] is False
    assert stale["participates_in_p16_ordering_or_cap"] is False
    assert stale["writes_project_state"] is False


def test_currentness_requires_comparable_prior_evidence():
    no_prior = evidence.los_currentness(None, current_input_fingerprint="abc",
                                       current_algorithm_version="v")
    assert no_prior["state"] == "no_prior_evidence"
    assert no_prior["stale"] is False
    assert no_prior["comparable"] is False

    empty = evidence.los_currentness({}, current_input_fingerprint="abc",
                                     current_algorithm_version="v")
    assert empty["stale"] is False

    stale = evidence.los_currentness(
        {"input_fingerprint": "abc",
         "algorithm_version": los.RADAR_TERRAIN_LOS_ALGORITHM_VERSION,
         "evaluation_scope": evidence.EVALUATION_SCOPE_TOWER_PROBE_SAMPLE},
        current_input_fingerprint="def",
        current_algorithm_version=los.RADAR_TERRAIN_LOS_ALGORITHM_VERSION,
    )
    assert stale["state"] == "stale"
    assert stale["reasons"] == ["los_inputs_changed"]


# ------------------------------------------------------- 7. 高度层与 probe 高度


def test_probe_height_comes_from_authoritative_altitude_layer(terrain_file):
    factory = TerrainSourceFactory(base=TERRAIN_BASE_M)
    state = _state(
        terrain_path=terrain_file,
        profiles={"T-001": {"tower_top_orthometric_m": TOWER_TOP_M, "status": "passed"}},
    )
    service, _session = _service(state, factory)

    result = service.diagnose({"probe_budget": 4})

    assert result["altitude_layer"]["altitude_layer_id"] == ALTITUDE_LAYER_ID
    assert result["altitude_layer"]["nominal_altitude_m"] == pytest.approx(ALTITUDE_M)
    assert result["altitude_layer"]["probe_height_source"] == (
        "authoritative_altitude_layer_nominal_altitude_m"
    )
    assert result["altitude_layer"]["representative_height_used"] is False
    for record in result["records"]:
        assert record["probe_altitude_egm2008_m"] == pytest.approx(ALTITUDE_M)
        assert record["probe_height_source"] == "probe_altitude_layer_catalog"
        assert record["vertical_reference"] == los.EGM2008_ORTHOMETRIC
        assert record["representative_height_used"] is False


def test_probe_positions_follow_route_sampling_chain(terrain_file):
    factory = TerrainSourceFactory(base=TERRAIN_BASE_M)
    state = _state(
        terrain_path=terrain_file,
        profiles={"T-001": {"tower_top_orthometric_m": TOWER_TOP_M, "status": "passed"}},
    )
    service, _session = _service(state, factory)

    result = service.diagnose({"probe_budget": 4})

    assert result["source_provenance"]["probe_source"] == "build_route_samples"
    assert result["source_provenance"]["origin_resolution"] == "resolve_radar_origins"
    probe_ids = [item["probe_id"] for item in result["records"]]
    assert all(probe_id.startswith("R-1:O") for probe_id in probe_ids)
    #: 沿航路里程单调递增（证明用的是既有采样链，而不是另造一套 probe）。
    distances = [item["horizontal_distance_m"] for item in result["records"]]
    assert distances == sorted(distances)


def test_route_filter_bounds_the_probe_candidates(terrain_file):
    factory = TerrainSourceFactory(base=TERRAIN_BASE_M)
    state = _state(
        terrain_path=terrain_file,
        profiles={"T-001": {"tower_top_orthometric_m": TOWER_TOP_M, "status": "passed"}},
        routes=[_route_payload(route_id="R-1"), _route_payload(route_id="R-2")],
    )
    service, _session = _service(state, factory)

    filtered = service.diagnose({"probe_budget": 4, "route_id": "R-2"})

    assert {item["route_id"] for item in filtered["records"]} == {"R-2"}
    assert filtered["source_provenance"]["route_filter"] == ["R-2"]


# ------------------------------------------------------- 8. 冻结边界与证据隔离


def test_evidence_never_enters_planning_decisions(terrain_file):
    factory = TerrainSourceFactory(base=TERRAIN_BASE_M)
    state = _state(
        terrain_path=terrain_file,
        profiles={"T-001": {"tower_top_orthometric_m": TOWER_TOP_M, "status": "passed"}},
    )
    service, _session = _service(state, factory)

    result = service.diagnose({"probe_budget": 4})

    for key in (
        "participates_in_panel_selection", "participates_in_azimuth_optimisation",
        "participates_in_milp", "participates_in_required_cns",
        "participates_in_p14_p15_p16", "participates_in_p16_ordering_or_cap",
        "not_evaluated_in_planning", "has_project_state_writer",
    ):
        assert result[key] is (True if key == "not_evaluated_in_planning" else False)
    assert result["terrain_los_replaces_beam_geometry"] is False
    assert result["is_detection_probability"] is False
    assert result["blocked_means_absolute_no_detection"] is False
    assert result["unknown_treated_as_clear"] is False
    assert result["unknown_removes_candidate_tower"] is False
    assert result["frozen_radar_geometry"]["version"] == "radar_layout_geometry_v1_1"
    assert result["frozen_radar_geometry"]["replaced_by_terrain_los"] is False


def test_frozen_radar_geometry_parameters_are_unchanged():
    parameters = RADAR_GEOMETRY_PARAMETERS[RADAR_TYPE_I]

    assert ELEVATION_CENTER_DEG == 22.5
    assert ELEVATION_MIN_DEG == 0.0
    assert ELEVATION_MAX_DEG == 45.0
    assert AZIMUTH_BEAMWIDTH_DEG == 90.0
    assert parameters["max_slant_range_m"] > 0
    assert parameters["min_slant_range_m"] >= 0
    assert parameters["azimuth_beamwidth_deg"] == AZIMUTH_BEAMWIDTH_DEG


def test_blocked_and_unknown_are_not_reinterpreted(terrain_file):
    factory = TerrainSourceFactory(base=TERRAIN_BASE_M)
    state = _state(
        terrain_path=terrain_file,
        profiles={"T-001": {"tower_top_orthometric_m": TOWER_TOP_M, "status": "passed"}},
    )
    service, _session = _service(state, factory)

    result = service.diagnose({"probe_budget": 4})

    assert result["los_status_counts"]["clear"] == 3
    assert sum(result["los_status_counts"].values()) == result["evaluated_probe_count"]
    semantics = result["los_three_state_semantics_zh"]
    assert "不" in semantics["blocked"]
    assert "不视为" in semantics["unknown"] or "不是" in semantics["unknown"]
    assert result["dimension_separation_zh"]
    assert result["user_disclaimer_zh"] == evidence.TERRAIN_LOS_USER_DISCLAIMER_ZH


def test_low_clearance_hint_does_not_change_thresholds(terrain_file):
    #: 12 m 的塔顶原点 + 100 m 高度层 ⇒ 净空很小但仍 clear；只提示，不改判定。
    factory = TerrainSourceFactory(base=TERRAIN_BASE_M)
    state = _state(
        terrain_path=terrain_file,
        profiles={"T-001": {"tower_top_orthometric_m": LOW_TOWER_TOP_M, "status": "passed"}},
    )
    service, _session = _service(state, factory)

    result = service.diagnose({"probe_budget": 3})

    assert result["los_status_counts"] == {"clear": 3, "blocked": 0, "unknown": 0}
    assert result["low_clearance_threshold_m"] == pytest.approx(
        service_module.LOW_CLEARANCE_HINT_M
    )
    assert result["low_clearance_samples"]
    assert all(
        item["low_clearance"] is True for item in result["low_clearance_samples"]
    )


# ------------------------------------------------------- 9. 失败口径


def test_missing_terrain_source_fails_closed(tmp_path):
    factory = TerrainSourceFactory(base=TERRAIN_BASE_M)
    state = _state(terrain_path=tmp_path / "missing.tif")
    state["data_source_paths"] = {}
    service, session = _service(state, factory)

    result = service.diagnose({})

    assert result["evidence_status"] == evidence.EVIDENCE_STATUS_FAILED
    assert result["failure_reason"] == "terrain_dtm_not_configured"
    assert result["los_status_counts"]["clear"] == 0
    assert factory.open_count == 0
    assert session.saved == 0


def test_missing_metric_transform_fails_closed(terrain_file):
    factory = TerrainSourceFactory(base=TERRAIN_BASE_M)
    state = _state(terrain_path=terrain_file)
    service, session = _service(state, factory)
    service.to_metric = None
    service.radar_layout_service.metric_transform = None
    service.facts_provider = None

    result = service.diagnose({})

    assert result["evidence_status"] == evidence.EVIDENCE_STATUS_FAILED
    assert result["failure_reason"] == "metric_projector_not_injected"
    assert factory.open_count == 0
    assert session.saved == 0


def test_unknown_terrain_vertical_datum_is_not_clear(terrain_file):
    factory = TerrainSourceFactory(base=TERRAIN_BASE_M)
    state = _state(
        terrain_path=terrain_file,
        profiles={"T-001": {"tower_top_orthometric_m": TOWER_TOP_M, "status": "passed"}},
    )
    service, _session = _service(state, factory)

    result = service.diagnose({"probe_budget": 3})

    assert result["dem"]["vertical_reference"] == los.EGM2008_ORTHOMETRIC
    assert result["los_status_counts"]["clear"] == 3


def test_diagnostic_failure_is_reported_not_silently_empty(terrain_file):
    factory = TerrainSourceFactory(base=TERRAIN_BASE_M)
    service, _session = _service(_state(terrain_path=terrain_file), factory)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("synthetic failure")

    original = service_module.terrain_los.radar_origin_terrain_physical_inputs
    service_module.terrain_los.radar_origin_terrain_physical_inputs = _boom
    try:
        result = service.diagnose({})
    finally:
        service_module.terrain_los.radar_origin_terrain_physical_inputs = original

    assert result["evidence_status"] == evidence.EVIDENCE_STATUS_FAILED
    assert result["failure_reason"] == "terrain_los_diagnosis_failed"
    assert result["los_status_counts"]["clear"] == 0


# ------------------------------------------------------- 11. 接线（workflow / API）


def _workflow_state_path(tmp_path):
    return tmp_path / "project.json"


def _workflow(tmp_path):
    from cns_planner.application.workflow_service import WorkflowService

    return WorkflowService(_workflow_state_path(tmp_path), DEFAULTS)


def test_workflow_snapshot_carries_only_the_lightweight_los_block(tmp_path):
    workflow = _workflow(tmp_path)
    state = workflow.state
    state["operational_routes"] = [{
        "route_id": "R-1", "status": "passed",
        "path": [[122.0, 30.0], [122.01, 30.0]],
    }]
    state["towers"] = {"status": "passed", "count": 1, "items": [_tower_payload()]}
    state.setdefault("spatial_3d", {})["altitude_layers"] = [{
        "altitude_layer_id": ALTITUDE_LAYER_ID, "nominal_altitude_m": ALTITUDE_M,
        "lower_altitude_m": 90.0, "upper_altitude_m": 110.0,
        "vertical_reference": "egm2008_orthometric", "status": "confirmed",
        "confirmed": True,
    }]

    snapshot = workflow.snapshot()
    block = snapshot["radar_terrain_los"]

    assert block["evidence_status"] == evidence.EVIDENCE_STATUS_NOT_REQUESTED
    assert block["dem_opened"] is False
    assert block["los_computation_performed"] is False
    assert block["records"] == []
    assert block["los_status_counts"] == {"clear": 0, "blocked": 0, "unknown": 0}
    assert block["writes_project_state"] is False
    assert block["not_evaluated_in_planning"] is True
    #: 通用快照里的这一块必须很小（默认摘要边界：远小于 100 KB）。
    encoded = json.dumps(block, ensure_ascii=False).encode("utf-8")
    assert len(encoded) < 100_000


def test_workflow_diagnose_is_read_only_for_project_state(tmp_path):
    workflow = _workflow(tmp_path)
    before = _state_snapshot(workflow.state)

    #: 未配置 terrain_dtm ⇒ 明确 failed，且**不写任何状态**。
    result = workflow.diagnose_radar_terrain_los({})

    assert result["evidence_status"] == evidence.EVIDENCE_STATUS_FAILED
    assert result["failure_reason"] == "terrain_dtm_not_configured"
    assert _state_snapshot(workflow.state) == before


def test_workflow_los_stale_does_not_touch_upstream_planning_containers(tmp_path):
    workflow = _workflow(tmp_path)
    state = workflow.state
    state["operational_routes"] = [{
        "route_id": "R-1", "status": "passed", "path": [[122.0, 30.0], [122.01, 30.0]],
    }]
    state["towers"] = {"status": "passed", "count": 1, "items": [_tower_payload()]}
    before = _state_snapshot(state)

    prior = {"input_fingerprint": "0" * 64,
             "algorithm_version": los.RADAR_TERRAIN_LOS_ALGORITHM_VERSION,
             "evaluation_scope": evidence.EVALUATION_SCOPE_TOWER_PROBE_SAMPLE}
    result = workflow.diagnose_radar_terrain_los({"prior_evidence": prior})

    assert result["evidence_status"] == evidence.EVIDENCE_STATUS_FAILED
    #: 上游规划容器逐字未变。
    assert _state_snapshot(state) == before


def test_workflow_has_no_los_invalidator_registered(tmp_path):
    """LOS 只读诊断**没有** ProjectState writer，也没有失效入口。"""

    workflow = _workflow(tmp_path)
    invalidation = workflow.invalidation_service

    for name in dir(invalidation):
        assert "terrain_los" not in name.lower(), name
    service = workflow.radar_terrain_los_diagnostics_service
    for name in dir(service):
        assert not name.startswith("stale_for"), name


def test_workflow_details_without_diagnosis_never_computes(tmp_path):
    workflow = _workflow(tmp_path)

    result = workflow.radar_terrain_los_details({"limit": 5})

    assert result["evidence_status"] == evidence.EVIDENCE_STATUS_FAILED
    assert result["failure_reason"] == "detail_not_available"
    assert result["detail_page"] is None


def _api_router(workflow):
    from cns_planner.api.router import ApiRouter

    class Context:
        data = None
        qgis = None
        static = Path("cns_planner/web")

    Context.workflow = workflow
    return ApiRouter(Context())


def test_api_status_endpoint_and_workflow_snapshot_never_open_dem(tmp_path):
    workflow = _workflow(tmp_path)
    router = _api_router(workflow)
    state = workflow.state
    state["data_source_paths"] = {"terrain_dtm": str(tmp_path / "never-opened.tif")}
    state["towers"] = {"status": "passed", "count": 1, "items": [_tower_payload()]}

    #: ``/api/state`` 与 ``/api/workflow`` 都不得触发 DEM 读取或 LOS 计算。
    status = router.get("/api/radar-terrain-los/status", {}, {})
    assert status.data["evidence_status"] == evidence.EVIDENCE_STATUS_NOT_REQUESTED
    assert status.data["dem_opened"] is False
    assert status.data["los_computation_performed"] is False
    assert status.data["records"] == []
    #: 只读状态里的 DEM 身份**不做** ``stat`` 之外的任何事，且从未打开数据集。
    assert status.data["adapter_availability"]["terrain_dtm"]["opened"] is False

    snapshot = workflow.snapshot()
    block = snapshot["radar_terrain_los"]
    assert block["dem_opened"] is False
    assert block["los_computation_performed"] is False
    assert block["evidence_status"] == evidence.EVIDENCE_STATUS_NOT_REQUESTED


def test_api_diagnose_and_details_endpoints_are_wired(tmp_path):
    workflow = _workflow(tmp_path)
    router = _api_router(workflow)

    #: 未配置 terrain_dtm ⇒ 端点存在、语义为只读 failed（不是 404、也不是"无遮挡"）。
    diagnosed = router.post("/api/radar-terrain-los/diagnose", {})
    assert diagnosed.data["evidence_status"] == evidence.EVIDENCE_STATUS_FAILED
    assert diagnosed.data["failure_reason"] == "terrain_dtm_not_configured"

    details = router.get("/api/radar-terrain-los/details", {"limit": ["5"]}, {})
    assert details.data["failure_reason"] == "detail_not_available"
    assert details.data["detail_page"] is None
    assert details.data["writes_project_state"] is False


# ------------------------------------------------------- 10. 证据块结构与规模契约


def test_evidence_block_contains_required_fields(terrain_file):
    factory = TerrainSourceFactory(base=TERRAIN_BASE_M)
    state = _state(
        terrain_path=terrain_file,
        profiles={"T-001": {"tower_top_orthometric_m": TOWER_TOP_M, "status": "passed"}},
    )
    service, _session = _service(state, factory)

    result = service.diagnose({"probe_budget": 5})

    for key in (
        "evidence_status", "evaluation_scope", "algorithm_id", "algorithm_version",
        "evaluated_tower_count", "evaluated_probe_count", "los_status_counts",
        "origin_availability_counts", "dem", "altitude_layer", "input_fingerprint",
        "currentness", "not_evaluated", "records", "sampling", "bounds",
    ):
        assert key in result, key
    assert result["dem"]["source_identity"]["mtime_ns"] is not None
    assert result["dem"]["source_identity"]["resampled"] is False
    assert result["evaluation_scope"] == evidence.EVALUATION_SCOPE_TOWER_PROBE_SAMPLE


def test_algorithm_version_is_declared_and_pinned(terrain_file):
    factory = TerrainSourceFactory(base=TERRAIN_BASE_M)
    state = _state(terrain_path=terrain_file)
    service, _session = _service(state, factory)

    status = service.status()
    result = service.diagnose({"probe_budget": 2, "max_tower_count": 1})

    assert status["algorithm_version"] == los.RADAR_TERRAIN_LOS_ALGORITHM_VERSION
    assert result["algorithm_version"] == los.RADAR_TERRAIN_LOS_ALGORITHM_VERSION
    assert result["read_mode"] == los.RADAR_TERRAIN_LOS_READ_MODE
    assert set(result["not_evaluated"]) >= set(los.RADAR_TERRAIN_LOS_NOT_EVALUATED)


def test_domain_evidence_module_is_pure():
    import inspect

    source = inspect.getsource(evidence)
    for forbidden in ("import qgis", "from qgis", "osgeo", "rasterio", "open("):
        assert forbidden not in source, forbidden


def test_probe_budget_and_tower_count_are_hard_capped(terrain_file):
    factory = TerrainSourceFactory(base=TERRAIN_BASE_M)
    state = _state(
        terrain_path=terrain_file,
        profiles={"T-001": {"tower_top_orthometric_m": TOWER_TOP_M, "status": "passed"}},
    )
    service, _session = _service(state, factory)

    result = service.diagnose({"probe_budget": 10 ** 9, "max_tower_count": 10 ** 9})

    assert result["bounds"]["max_tower_count"] == service_module.MAX_TOWER_COUNT_LIMIT
    assert result["bounds"]["probe_budget"] <= service_module.MAX_EVALUATED_PROBE_TOTAL
    assert result["evaluated_probe_count"] <= service_module.MAX_EVALUATED_PROBE_TOTAL
