"""层级无关建筑事实 / 统一路径解析 / 建筑轮廓 GeoJSON（Phase 3.5 人工验收修复）。

覆盖四件事：

1. **任务 1**：``building_grid_l8`` 可用时，不论工作区网格层级都能生成
   ``BuildingEnvironmentMappingResult``（``total_cells`` / ``covered_cells`` /
   ``unresolved_cells`` / ``status``），且事实由**原始 footprint 精确几何聚合**得到
   （不是跨层级平均、不是插值）；L8 直接映射的既有行为完全不变。
2. **任务 2**：``PopulationMappingResult`` 统一输出带
   ``coverage_ratio`` / ``full_cells`` / ``partial_cells`` / ``missing_cells``。
3. **任务 3**：统一 path resolver 支持 ``.qgz`` / ``.gpkg`` / ``.shp`` / ``.geojson``，
   并对 ``exists`` / ``is_file`` 给出确定结论。
4. **任务 4**：``.qgz`` → 后端解析 → 按 bbox 返回建筑轮廓 GeoJSON（含数量上限与简化）。

真实数据用例在源文件缺失时自动 skip（不会假装通过）。
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import zipfile

import pytest

from cns_planner.algorithms.grid.service import WorkspaceGridService
from cns_planner.data.mapping.buildings import BuildingGridService
from cns_planner.data.mapping.population import PopulationGridService
from cns_planner.gis.building_footprint_aggregation import (
    aggregate_footprint_facts, footprints_geojson, metric_crs_for_bbox,
)
from cns_planner.gis.path_resolver import (
    STATUS_NOT_A_FILE, STATUS_OK, STATUS_PATH_MISSING, STATUS_UNSUPPORTED_FORMAT,
    check_source_paths, resolve_source_path,
)
from cns_planner.gis.qgis_project_layers import (
    find_polygon_layer, read_project_layers, resolve_vector_layer_source,
)

#: 真实舟山数据（缺失则相关用例 skip）。
REAL_BUILDINGS = Path("D:/aaa2026project/UOM/舟山/规划系统/building/processed/zhoushan_buildings.gpkg")
REAL_BUILDING_GRID = Path("D:/aaa2026project/UOM/舟山/规划系统/building/processed/zhoushan_building_grid_L8.gpkg")
REAL_QGZ = Path("D:/aaa2026project/UOM/舟山/规划系统/building/舟山建筑数据.qgz")

#: 单格 L8 工作区（1/900 度）与落在其中的一个小 footprint。
CELL_SIZE = 1 / 900
WORKSPACE = [10.0, 10.0, 10.0 + CELL_SIZE, 10.0 + CELL_SIZE]


def _footprint_wkb(west, south, east, north):
    shapely = pytest.importorskip("shapely")
    from shapely.geometry import box

    return shapely.to_wkb(box(west, south, east, north))


def _write_footprint_gpkg(path, rows, *, layer="buildings", with_rtree=True, extent_override=None):
    """写入一个最小的建筑足迹 GeoPackage（只读路径可解析即可，不做 schema 全量校验）。"""

    connection = sqlite3.connect(str(path))
    try:
        connection.executescript(
            "CREATE TABLE gpkg_contents (table_name TEXT, data_type TEXT, identifier TEXT, "
            "srs_id INTEGER, min_x REAL, min_y REAL, max_x REAL, max_y REAL);"
            "CREATE TABLE gpkg_geometry_columns (table_name TEXT, column_name TEXT, "
            "geometry_type_name TEXT, srs_id INTEGER, z INTEGER, m INTEGER);"
            f"CREATE TABLE {layer} (fid INTEGER PRIMARY KEY, geom BLOB, id TEXT, height_m REAL);"
        )
        if with_rtree:
            connection.execute(
                f"CREATE VIRTUAL TABLE rtree_{layer}_geom USING rtree(id,minx,maxx,miny,maxy)"
            )
        for index, (wkb, height, identifier, bounds) in enumerate(rows, start=1):
            connection.execute(
                f"INSERT INTO {layer} VALUES (?,?,?,?)", (index, wkb, identifier, height),
            )
            if with_rtree:
                connection.execute(
                    f"INSERT INTO rtree_{layer}_geom VALUES (?,?,?,?,?)",
                    (index, bounds[0], bounds[2], bounds[1], bounds[3]),
                )
        bounds_all = [
            [min(b[0] for b in (row[3] for row in rows)), min(b[1] for b in (row[3] for row in rows)),
             max(b[2] for b in (row[3] for row in rows)), max(b[3] for b in (row[3] for row in rows))]
        ] if rows else [[0.0, 0.0, 0.0, 0.0]]
        declared_extent = list(extent_override) if extent_override else bounds_all[0]
        connection.execute(
            "INSERT INTO gpkg_contents VALUES (?,?,?,?,?,?,?,?)",
            (layer, "features", layer, 4326, *declared_extent),
        )
        connection.execute(
            "INSERT INTO gpkg_geometry_columns VALUES (?,?,?,?,0,0)",
            (layer, "geom", "POLYGON", 4326),
        )
        connection.commit()
    finally:
        connection.close()
    return path


def _single_l8_workspace():
    return WorkspaceGridService(preferred_level=8).generate(WORKSPACE)


# ============================================================================
# 任务 1：层级无关建筑事实
# ============================================================================

def test_metric_crs_uses_the_workspace_utm_zone():
    assert metric_crs_for_bbox(WORKSPACE) == "EPSG:32632"
    # 舟山（122°E / 30°N）必须落在 UTM 51N，与既有 L8 事实表生成脚本使用的 AREA_CRS 一致。
    assert metric_crs_for_bbox([122.268, 29.835, 122.300, 29.955]) == "EPSG:32651"


def test_aggregation_matches_l8_fact_table_semantics_on_the_same_cell(tmp_path):
    """同一 footprint 在 L8 下的聚合结果必须与 L8 事实表语义一致。"""

    footprint = (10.0004, 10.0004, 10.0005, 10.0005)
    path = _write_footprint_gpkg(tmp_path / "f.gpkg", [
        (_footprint_wkb(*footprint), 20.0, "B1", footprint),
        # 高度缺失的第二个 footprint：count + 1，valid_height_fraction 下降到 0.5。
        (_footprint_wkb(10.0006, 10.0006, 10.0007, 10.0007), None, "B2",
         (10.0006, 10.0006, 10.0007, 10.0007)),
    ])
    grid = _single_l8_workspace()
    assert grid["count"] == 1 and grid["level"] == 8

    result = aggregate_footprint_facts(grid["cells"], path, grid_level=8)
    assert result["status"] == "passed"
    assert result["algorithm_id"] == "building-grid-footprint-aggregation"
    cell = result["cells"][grid["cells"][0]["grid_id"]]
    assert cell["status"] == "passed"
    # 质心分配：两个 footprint 的质心都落在同一格 ⇒ count = 2（一个建筑只计一次）。
    assert cell["building_count"] == 2
    assert cell["height_max_m"] == pytest.approx(20.0)
    # height_valid = height_m 非空且 > 0（与 L8 事实表脚本同一规则）。
    assert cell["valid_height_fraction"] == pytest.approx(0.5)
    assert 0 < cell["building_coverage_ratio"] < 1
    assert result["metadata"]["area_crs"] == "EPSG:32632"
    assert result["metadata"]["count_allocation"] == "building_centroid_in_cell"
    assert result["metadata"]["cross_level_interpolation"] is False
    assert result["metadata"]["cross_level_averaging"] is False


def test_building_mapping_is_level_independent_when_footprints_are_configured(tmp_path):
    """工作区被 coarsen 到 L7 时，只要原始 footprint 可用就必须给出可用的建筑事实。"""

    footprint = (10.0004, 10.0004, 10.0005, 10.0005)
    footprints = _write_footprint_gpkg(tmp_path / "f.gpkg", [
        (_footprint_wkb(*footprint), 33.0, "B1", footprint),
    ],
        # 源覆盖整个工作区：源 extent 内的空格是"已知无建筑"，只有 extent 之外才是 unknown。
        extent_override=[9.9, 9.9, 10.1, 10.1],
    )
    # 一个 L7 工作区：cell 尺寸是 L8 的 3 倍。
    grid = WorkspaceGridService(preferred_level=7).generate([10.0, 10.0, 10.004, 10.004])
    assert grid["level"] == 7 and grid["count"] == 4 and grid["coarsened"] is False

    without = BuildingGridService().map(grid, None)
    assert without["status"] == "unsupported"
    orphan = without["environment_mapping"]
    assert orphan["status"] == "unsupported"
    assert orphan["total_cells"] == 4
    assert orphan["covered_cells"] == 0
    assert orphan["unresolved_cells"] == 4
    assert orphan["coverage_ratio"] == 0.0
    assert orphan["facts_available"] is False
    assert orphan["participates_in_planner"] is False
    assert orphan["level_aligned"] is False
    assert orphan["level_independent_facts"] is False

    with_footprints = BuildingGridService().map(
        grid, None, footprint_source=footprints, footprint_aggregator=aggregate_footprint_facts,
    )
    mapping = with_footprints["environment_mapping"]
    assert with_footprints["status"] == "passed"
    assert mapping["status"] == "passed"
    assert mapping["total_cells"] == 4
    assert mapping["covered_cells"] == 4
    assert mapping["unresolved_cells"] == 0
    assert mapping["coverage_ratio"] == 1.0
    assert mapping["level_aligned"] is False
    assert mapping["level_independent_facts"] is True
    assert mapping["participates_in_planner"] is True
    assert mapping["mapping_basis"] == "exact_footprint_intersection_and_centroid_allocation"
    # 下游（Layered feasibility adapter / Risk V2）需要的键与 L8 直接映射完全一致。
    with_buildings = [
        cell for cell in with_footprints["cells"].values()
        if isinstance(cell, dict) and (cell.get("building_count") or 0) > 0
    ]
    assert len(with_buildings) == 1
    cell = with_buildings[0]
    assert set(cell) == {
        "status", "building_count", "building_area_m2", "building_coverage_ratio",
        "height_mean_m", "height_p95_m", "height_max_m", "valid_height_fraction",
        "building_exposure",
    }
    assert cell["building_count"] == 1
    assert cell["height_max_m"] == pytest.approx(33.0)


def test_building_mapping_without_any_source_stays_unsupported_and_never_fakes_zero():
    grid = _single_l8_workspace()
    result = BuildingGridService().map(grid, None)
    mapping = result["environment_mapping"]
    assert mapping["status"] == "unsupported"
    assert mapping["covered_cells"] == 0
    assert mapping["unresolved_cells"] == 1
    assert mapping["facts_available"] is False
    assert result["cells"][grid["cells"][0]["grid_id"]]["building_count"] is None


def test_environment_mapping_status_is_three_valued():
    passed = BuildingGridService.environment_mapping(
        {"status": "passed", "count": 2, "grid_level": 8, "source": {"path": "x"},
         "cells": {"a": {"status": "passed"}, "b": {"status": "passed"}}}
    )
    partial = BuildingGridService.environment_mapping(
        {"status": "missing_data", "count": 2, "grid_level": 8, "source": {"path": "x"},
         "cells": {"a": {"status": "passed"}, "b": {"status": "missing_data", "reason": "outside_coverage"}}}
    )
    unsupported = BuildingGridService.environment_mapping(
        {"status": "unsupported", "count": 2, "grid_level": 7, "source": {"path": "x"},
         "cells": {"a": {"status": "missing_data"}, "b": {"status": "missing_data"}}}
    )
    assert (passed["status"], passed["unresolved_cells"]) == ("passed", 0)
    assert partial["status"] == "partial"
    assert partial["unresolved_reasons"] == {"outside_coverage": 1}
    assert unsupported["status"] == "unsupported"
    assert unsupported["level_aligned"] is False


def test_building_grid_capability_declares_footprint_aggregation():
    capability = BuildingGridService.capabilities(declared_level=7)
    aggregation = capability["footprint_aggregation"]
    assert aggregation["available"] is True
    assert aggregation["cross_level_averaging"] is False
    assert aggregation["cross_level_interpolation"] is False
    assert capability["future_work_status"] == "implemented_via_exact_footprint_aggregation"


def test_aggregation_without_spatial_index_fails_closed(tmp_path):
    footprint = (10.0004, 10.0004, 10.0005, 10.0005)
    path = _write_footprint_gpkg(
        tmp_path / "no_rtree.gpkg",
        [(_footprint_wkb(*footprint), 20.0, "B1", footprint)],
        with_rtree=False,
    )
    grid = _single_l8_workspace()
    result = aggregate_footprint_facts(grid["cells"], path, grid_level=8)
    assert result["status"] == "missing_data"
    assert "空间索引" in result["message"]
    assert result["cells"][grid["cells"][0]["grid_id"]]["building_count"] is None


# ============================================================================
# 任务 2：统一的 PopulationMappingResult
# ============================================================================

def test_population_mapping_exposes_coverage_ratio_and_cell_counts():
    grid = {"level": 8, "cells": [
        {"grid_id": "a", "bbox": [10.0, 10.0, 10.001, 10.001]},
        {"grid_id": "b", "bbox": [10.001, 10.0, 10.002, 10.001]},
        {"grid_id": "c", "bbox": [10.002, 10.0, 10.003, 10.001]},
    ]}
    result = PopulationGridService.empty()
    result.update({
        "count": 3, "status": "missing_data", "value_status": "missing_data",
        "coverage_summary": {"source_coverage_fraction": 0.5},
        "cells": {
            "a": {"status": "passed", "value_status": "passed", "coverage_status": "full"},
            "b": {"status": "passed", "value_status": "passed", "coverage_status": "partial"},
            "c": {"status": "missing_data", "value_status": "missing_data", "coverage_status": "outside_extent"},
        },
    })
    mapping = PopulationGridService.population_mapping(result)
    assert mapping["status"] == "partial"
    assert mapping["total_cells"] == 3
    assert mapping["covered_cells"] == 2
    assert mapping["unresolved_cells"] == 1
    assert mapping["full_cells"] == 1
    assert mapping["partial_cells"] == 1
    assert mapping["missing_cells"] == 0
    assert mapping["outside_cells"] == 1
    assert mapping["coverage_ratio"] == 0.5
    assert mapping["cell_coverage_ratio"] == pytest.approx(2 / 3)
    assert mapping["participates_in_planner"] is False
    assert grid["cells"][0]["grid_id"] == "a"  # 归一化不修改输入


def test_population_mapping_reports_confirmed_zero_as_a_known_value():
    result = PopulationGridService.empty()
    result.update({
        "count": 2, "status": "passed", "value_status": "passed",
        "cells": {
            "a": {"status": "passed", "value_status": "passed", "coverage_status": "full"},
            "b": {"status": "passed", "value_status": "passed",
                  "coverage_status": "nodata_confirmed_zero_population"},
        },
    })
    mapping = PopulationGridService.population_mapping(result)
    assert mapping["status"] == "passed"
    assert mapping["covered_cells"] == 2
    assert mapping["confirmed_zero_cells"] == 1


def test_population_mapping_alias_keys_survive_the_empty_state():
    empty = PopulationGridService.empty()
    assert empty["coverage_ratio"] is None
    assert (empty["full_cells"], empty["partial_cells"], empty["missing_cells"]) == (0, 0, 0)
    assert empty["population_mapping"]["status"] == "unsupported"


# ============================================================================
# 任务 3：统一路径解析
# ============================================================================

def test_path_resolver_accepts_qgz_gpkg_shp_and_geojson_for_buildings(tmp_path):
    for name in ("a.qgz", "b.gpkg", "c.shp", "d.geojson"):
        target = tmp_path / name
        target.write_bytes(b"x")
        record = resolve_source_path(target, role="buildings")
        assert record["status"] == STATUS_OK, name
        assert record["exists"] is True and record["is_file"] is True
        assert record["suffix"] == Path(name).suffix


def test_path_resolver_reports_missing_directory_and_unsupported_format(tmp_path):
    missing = resolve_source_path(tmp_path / "nope.gpkg", role="buildings")
    assert missing["status"] == STATUS_PATH_MISSING

    directory = resolve_source_path(tmp_path, role="buildings")
    assert directory["status"] == STATUS_NOT_A_FILE

    raster_for_buildings = tmp_path / "dem.tif"
    raster_for_buildings.write_bytes(b"x")
    assert resolve_source_path(raster_for_buildings, role="buildings")["status"] == STATUS_UNSUPPORTED_FORMAT
    # 同一路径对人口角色是合法的：格式判定按角色走。
    assert resolve_source_path(raster_for_buildings, role="population")["status"] == STATUS_OK


def test_path_resolver_handles_empty_and_unset_values():
    assert resolve_source_path(None, role="buildings")["status"] == "not_configured"
    assert resolve_source_path("   ", role="buildings")["status"] == "not_configured"


def test_check_source_paths_never_caches_previous_findings(tmp_path):
    target = tmp_path / "b.gpkg"
    target.write_bytes(b"x")
    first = check_source_paths({"buildings": str(target)})
    assert first["status"] == "ok" and first["problem_count"] == 0
    target.unlink()
    second = check_source_paths({"buildings": str(target)})
    assert second["status"] == "warning"
    assert second["problems"][0]["role"] == "buildings"
    assert second["problems"][0]["status"] == STATUS_PATH_MISSING


def _write_qgz(path, qgs_xml):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("project.qgs", qgs_xml)
    return path


QGS_TEMPLATE = """<?xml version="1.0"?>
<qgis version="3.44">
 <layer-tree-group>
  <layer-tree-layer id="v1" name="zhoushan_buildings" source="./data/buildings.gpkg|layername=buildings"/>
  <layer-tree-layer id="v2" name="L8 density" source="./data/grid.gpkg|layername=building_grid_L8"/>
 </layer-tree-group>
 <projectlayers>
  <maplayer type="vector" geometry="Polygon" wkbType="MultiPolygon" id="v1">
   <datasource>./data/buildings.gpkg|layername=buildings</datasource>
   <layername>zhoushan_buildings</layername><provider>ogr</provider>
  </maplayer>
  <maplayer type="vector" geometry="Polygon" wkbType="Polygon" id="v2">
   <datasource>./data/grid.gpkg|layername=building_grid_L8</datasource>
   <layername>L8 - Building Density</layername><provider>ogr</provider>
  </maplayer>
  <maplayer type="raster" id="r1">
   <datasource>../FABDEM/dtm.tif</datasource><layername>dtm</layername><provider>gdal</provider>
  </maplayer>
  <maplayer type="raster" id="w1">
   <datasource>type=xyz&amp;url=https://t0.tianditu.gov.cn/vec_w/wmts?tk=x</datasource>
   <layername>online</layername><provider>wms</provider>
  </maplayer>
 </projectlayers>
</qgis>
"""


def test_qgz_project_is_parsed_without_qgis(tmp_path):
    project = _write_qgz(tmp_path / "project.qgz", QGS_TEMPLATE)
    inventory = read_project_layers(project)
    assert inventory["member"] == "project.qgs"
    assert inventory["project_dir"] == str(tmp_path)
    polygon = [layer for layer in inventory["layers"] if layer["is_polygon"] and layer["is_local_vector_file"]]
    assert len(polygon) == 2
    # 相对数据源被解析成绝对路径；远程瓦片 datasource 绝不当成本地文件。
    assert all(Path(layer["path"]).name in ("buildings.gpkg", "grid.gpkg") for layer in polygon)
    remote = [layer for layer in inventory["layers"] if layer["datasource_reason"]]
    assert remote


def test_qgz_resolution_prefers_footprints_over_the_grid_layer(tmp_path):
    (tmp_path / "data").mkdir()
    for name in ("buildings.gpkg", "grid.gpkg"):
        (tmp_path / "data" / name).write_bytes(b"x")
    project = _write_qgz(tmp_path / "project.qgz", QGS_TEMPLATE)

    footprints = resolve_vector_layer_source(project, role="buildings")
    assert footprints["ok"] is True
    assert footprints["source"] == "qgis_project"
    assert Path(footprints["path"]).name == "buildings.gpkg"
    assert footprints["layer_name"] == "buildings"

    grid = resolve_vector_layer_source(project, role="building_grid")
    assert grid["ok"] is True
    assert Path(grid["path"]).name == "grid.gpkg"
    assert grid["layer_name"] == "building_grid_L8"


def test_direct_vector_dataset_is_accepted_without_a_project(tmp_path):
    target = tmp_path / "buildings.geojson"
    target.write_text(json.dumps({"type": "FeatureCollection", "features": []}), encoding="utf-8")
    resolved = resolve_vector_layer_source(target, role="buildings")
    assert resolved["ok"] is True
    assert resolved["source"] == "vector_dataset"
    assert resolved["format"] == ".geojson"


def test_missing_project_file_is_reported_not_raised(tmp_path):
    resolved = resolve_vector_layer_source(tmp_path / "gone.qgz", role="buildings")
    assert resolved["ok"] is False
    assert resolved["status"] == "path_missing"
    assert find_polygon_layer(tmp_path / "gone.qgz") is None


# ============================================================================
# 任务 4：建筑轮廓 GeoJSON
# ============================================================================

def test_footprints_geojson_respects_bbox_limit_and_tolerance(tmp_path):
    rows = [
        (_footprint_wkb(10.0004, 10.0004, 10.0005, 10.0005), 12.0, "B1",
         (10.0004, 10.0004, 10.0005, 10.0005)),
        (_footprint_wkb(10.0006, 10.0006, 10.0007, 10.0007), 18.0, "B2",
         (10.0006, 10.0006, 10.0007, 10.0007)),
        (_footprint_wkb(11.0, 11.0, 11.0001, 11.0001), 30.0, "B3",
         (11.0, 11.0, 11.0001, 11.0001)),
    ]
    path = _write_footprint_gpkg(tmp_path / "f.gpkg", rows)

    result = footprints_geojson(path, [10.0, 10.0, 10.001, 10.001])
    assert result["status"] == "passed"
    assert result["type"] == "FeatureCollection"
    assert result["count"] == 2
    assert result["truncated"] is False
    properties = {feature["properties"]["id"]: feature["properties"] for feature in result["features"]}
    assert set(properties) == {"B1", "B2"}
    assert properties["B1"]["height_m"] == pytest.approx(12.0)
    assert result["features"][0]["geometry"]["type"] in ("Polygon", "MultiPolygon")
    assert result["source"]["layer"] == "buildings"

    limited = footprints_geojson(path, [10.0, 10.0, 10.001, 10.001], limit=1)
    assert limited["count"] == 1
    assert limited["truncated"] is True


def test_footprints_geojson_rejects_invalid_bbox_and_reports_unavailable_source(tmp_path):
    path = _write_footprint_gpkg(tmp_path / "f.gpkg", [])
    assert footprints_geojson(path, [])["status"] == "invalid_request"
    assert footprints_geojson(path, [10.0, 10.0, 10.0, 10.0])["status"] == "invalid_request"
    missing = footprints_geojson(tmp_path / "gone.gpkg", [10.0, 10.0, 10.001, 10.001])
    assert missing["status"] == "unavailable"
    assert missing["features"] == []


# ============================================================================
# 真实舟山数据（缺失自动 skip）
# ============================================================================

requires_real_data = pytest.mark.skipif(
    not (REAL_BUILDINGS.is_file() and REAL_BUILDING_GRID.is_file()),
    reason="真实舟山建筑数据不在本机",
)


@requires_real_data
def test_real_qgz_resolves_to_the_footprint_layer():
    if not REAL_QGZ.is_file():
        pytest.skip("真实 qgz 不在本机")
    resolved = resolve_vector_layer_source(REAL_QGZ, role="buildings")
    assert resolved["ok"] is True
    assert Path(resolved["path"]).name == "zhoushan_buildings.gpkg"
    assert resolved["layer_name"] == "buildings"


@requires_real_data
def test_real_aggregation_agrees_with_the_l8_fact_table():
    """真实数据回归：聚合到 L8 的结果必须与预聚合事实表一致（计数与最大高度）。"""

    workspace = [122.27, 29.84, 122.28, 29.85]
    grid = WorkspaceGridService(preferred_level=8).generate(workspace)
    assert grid["level"] == 8

    table = BuildingGridService().map(grid, str(REAL_BUILDING_GRID))
    assert table["status"] == "passed"
    assert table["metadata"]["mapping"] == "exact_bounds_not_grid_key"

    aggregated = aggregate_footprint_facts(grid["cells"], str(REAL_BUILDINGS), grid_level=8)
    mismatched_counts, mismatched_heights = [], []
    for cell in grid["cells"]:
        grid_id = cell["grid_id"]
        left = table["cells"].get(grid_id) or {}
        right = aggregated["cells"].get(grid_id) or {}
        if right.get("building_count") != left.get("building_count"):
            mismatched_counts.append((grid_id, left.get("building_count"), right.get("building_count")))
        left_height, right_height = left.get("height_max_m"), right.get("height_max_m")
        if left_height is None and right_height is None:
            continue
        if left_height is None or right_height is None or abs(left_height - right_height) > 1e-6:
            mismatched_heights.append((grid_id, left_height, right_height))
    assert not mismatched_counts, f"building_count 与 L8 事实表不一致：{mismatched_counts[:5]}"
    assert not mismatched_heights, f"height_max_m 与 L8 事实表不一致：{mismatched_heights[:5]}"


@requires_real_data
def test_real_l7_workspace_gets_usable_building_facts():
    """真实缺陷回归：L7 工作区过去恒为 unsupported 0/3528，现在必须有可用事实。"""

    workspace = [122.27, 29.84, 122.41, 30.12]
    grid = WorkspaceGridService(preferred_level=7).generate(workspace)
    assert grid["level"] == 7 and grid["count"] == 3528

    legacy = BuildingGridService().map(grid, str(REAL_BUILDING_GRID))
    assert legacy["status"] == "unsupported"
    assert legacy["environment_mapping"]["status"] == "unsupported"
    assert legacy["environment_mapping"]["covered_cells"] == 0

    fixed = BuildingGridService().map(
        grid, str(REAL_BUILDING_GRID), footprint_source=str(REAL_BUILDINGS),
        footprint_aggregator=aggregate_footprint_facts,
    )
    mapping = fixed["environment_mapping"]
    assert fixed["status"] == "passed"
    assert mapping["status"] == "passed"
    assert mapping["total_cells"] == 3528
    assert mapping["covered_cells"] == 3528
    assert mapping["unresolved_cells"] == 0
    assert mapping["level_independent_facts"] is True
    assert mapping["participates_in_planner"] is True
    with_buildings = [
        cell for cell in fixed["cells"].values()
        if isinstance(cell, dict) and (cell.get("building_count") or 0) > 0
    ]
    assert with_buildings, "真实工作区内必须至少有一格拿到建筑事实"
    assert all(cell["height_max_m"] is not None for cell in with_buildings)


@requires_real_data
def test_real_building_footprint_geojson_is_bounded():
    result = footprints_geojson(str(REAL_BUILDINGS), [122.28, 29.85, 122.30, 29.87], limit=40)
    assert result["status"] == "passed"
    assert result["count"] <= 40
    assert result["features"], "真实走廊内应有建筑轮廓"
    assert all(feature["geometry"] for feature in result["features"])
