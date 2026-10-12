"""Round36-D3 阶段B：L8 建筑网格**高度统计完整性**只读校验的定向回归。

背景（真实缺陷，Round36-D2 定位）：``zhoushan_building_grid_L8_v3.gpkg`` 的
``valid_height_fraction`` / ``height_mean_m`` / ``height_p95_m`` 三列 143,013 行全为
NULL，而 ``centroid_building_count`` / ``height_max_m`` 等事实列完整。生产 SQL 读这三列，
于是每个含建筑格都落入 fail-closed ``unknown``
（``building_height_or_ground_elevation_unresolved``），Theta* 直接 ``no_path``。

本文件钉住三件事：

1. ``inspect_building_grid_height_statistics`` 能区分任务要求的五类情形，并给出稳定的
   ``reason_ids``；
2. 该校验**只报告**：不改写源文件、不改变 ``layered_feasibility_source_status`` 的既有
   readiness 字段语义；
3. 统计列已写入但违反聚合合同时必须报 ``height_statistics_contract_violation``。
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from cns_planner.gis.layered_feasibility_adapter import layered_feasibility_source_status
from cns_planner.gis.source_inspection import (
    BUILDING_GRID_HEIGHT_STATISTICS_CONTRACT,
    inspect_building_grid_height_statistics,
)

COLUMNS = (
    "fid", "geom", "grid_key", "grid_level", "grid_col", "grid_row", "west", "south",
    "east", "north", "centroid_building_count", "intersecting_building_count",
    "height_max_m", "height_unresolved_count", "height_evidence_summary",
    "zero_coverage_confirmed", "building_count", "valid_height_fraction",
    "height_mean_m", "height_p95_m",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _grid_gpkg(path: Path, rows):
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE gpkg_contents (
          table_name TEXT PRIMARY KEY, data_type TEXT, identifier TEXT,
          srs_id INTEGER, min_x REAL, min_y REAL, max_x REAL, max_y REAL);
        CREATE TABLE gpkg_geometry_columns (
          table_name TEXT, column_name TEXT, geometry_type_name TEXT,
          srs_id INTEGER, z INTEGER, m INTEGER);
        CREATE TABLE building_grid_L8 (
          fid INTEGER PRIMARY KEY, geom BLOB, grid_key TEXT, grid_level INTEGER,
          grid_col INTEGER, grid_row INTEGER, west REAL, south REAL, east REAL,
          north REAL, centroid_building_count INTEGER,
          intersecting_building_count INTEGER, height_max_m REAL,
          height_unresolved_count INTEGER, height_evidence_summary TEXT,
          zero_coverage_confirmed BOOLEAN, building_count INTEGER,
          valid_height_fraction REAL, height_mean_m REAL, height_p95_m REAL);
        """
    )
    for row in rows:
        connection.execute(
            f"INSERT INTO building_grid_L8 ({','.join(COLUMNS)}) VALUES "
            f"({','.join('?' for _ in COLUMNS)})", row)
    connection.execute(
        "INSERT INTO gpkg_contents VALUES ('building_grid_L8','features','building_grid_L8',"
        "4326,121.0,30.0,122.0,31.0)")
    connection.execute(
        "INSERT INTO gpkg_geometry_columns VALUES ('building_grid_L8','geom','POLYGON',"
        "4326,0,0)")
    connection.commit()
    connection.close()


def _row(fid, *, grid_key, centroid, intersecting, height_max, unresolved,
         fraction, mean, p95, zero=False):
    return (fid, None, grid_key, 8, 271000 + fid, 27000 + fid, 121.0, 30.0,
            121.0 + fid * 1e-4, 30.0 + fid * 1e-4, centroid, intersecting, height_max,
            unresolved, "{}" if centroid else None, int(zero), centroid, fraction, mean, p95)


def test_complete_statistics_pass(tmp_path):
    path = tmp_path / "grid.gpkg"
    _grid_gpkg(path, [
        _row(1, grid_key="G1", centroid=3, intersecting=3, height_max=20.0, unresolved=0,
             fraction=1.0, mean=12.0, p95=18.0),
        _row(2, grid_key="G2", centroid=0, intersecting=2, height_max=9.0, unresolved=0,
             fraction=None, mean=None, p95=None),
        _row(3, grid_key="G3", centroid=0, intersecting=0, height_max=None, unresolved=0,
             fraction=None, mean=None, p95=None, zero=True),
    ])
    report = inspect_building_grid_height_statistics(path)
    assert report["status"] == "passed"
    assert report["reason_ids"][0] == "height_statistics_complete"
    assert report["contract_id"] == BUILDING_GRID_HEIGHT_STATISTICS_CONTRACT
    assert report["read_only"] is True and report["affects_readiness"] is False
    counts = report["counts"]
    assert counts["rows_total"] == 3
    assert counts["rows_with_centroid_buildings"] == 1
    assert counts["rows_statistics_complete"] == 1
    assert counts["rows_where_all_three_statistics_null"] == 0
    assert counts["rows_footprint_only_without_centroid_height"] == 1
    assert counts["rows_confirmed_zero_coverage"] == 1
    assert report["reason_ids"][1:] == [
        "footprint_coverage_without_centroid_height",
        "confirmed_zero_building_coverage",
    ]


def test_centroid_buildings_with_all_statistics_null_is_incomplete(tmp_path):
    """任务核心情形：有数值高度，但统计字段漏写。"""

    path = tmp_path / "grid.gpkg"
    _grid_gpkg(path, [
        _row(1, grid_key="G1", centroid=4, intersecting=4, height_max=20.0, unresolved=0,
             fraction=None, mean=None, p95=None),
        _row(2, grid_key="G2", centroid=1, intersecting=1, height_max=6.0, unresolved=0,
             fraction=1.0, mean=6.0, p95=6.0),
    ])
    report = inspect_building_grid_height_statistics(path)
    assert report["status"] == "incomplete"
    assert "height_statistics_missing_with_centroid_buildings" in report["reason_ids"]
    counts = report["counts"]
    assert counts["rows_where_all_three_statistics_null"] == 1
    assert counts["rows_with_usable_height_but_all_statistics_null"] == 1
    assert counts["rows_statistics_complete"] == 1
    assert counts["rows_statistics_partially_written"] == 0
    assert "building_height_or_ground_elevation_unresolved" in report["reason"]


def test_mandated_null_mean_is_not_reported_as_partial(tmp_path):
    """没有任何可用源高度时 mean/p95 必须为 NULL —— 这是合同要求，不是"漏写"。"""

    path = tmp_path / "grid.gpkg"
    _grid_gpkg(path, [
        # 质心建筑全部高度未知：fraction=0.0 已写出，mean/p95 强制 NULL
        _row(1, grid_key="G1", centroid=3, intersecting=3, height_max=None, unresolved=3,
             fraction=0.0, mean=None, p95=None),
    ])
    report = inspect_building_grid_height_statistics(path)
    assert report["status"] == "passed"
    assert report["reason_ids"][0] == "height_statistics_complete"
    assert "building_height_unknown_in_source" in report["reason_ids"]
    counts = report["counts"]
    assert counts["rows_statistics_complete"] == 1
    assert counts["rows_statistics_partially_written"] == 0
    assert counts["rows_where_all_three_statistics_null"] == 0
    assert counts["rows_with_centroid_buildings_but_no_source_height"] == 1

    # 反向：有可用源高度却漏写 mean/p95 ⇒ 必须报部分写入
    other = tmp_path / "grid2.gpkg"
    _grid_gpkg(other, [
        _row(1, grid_key="G1", centroid=3, intersecting=3, height_max=9.0, unresolved=0,
             fraction=1.0, mean=None, p95=None),
    ])
    second = inspect_building_grid_height_statistics(other)
    assert second["status"] == "incomplete"
    assert "height_statistics_partially_written" in second["reason_ids"]
    assert second["counts"]["rows_statistics_partially_written"] == 1


def test_partial_write_and_no_source_height_are_separated(tmp_path):
    path = tmp_path / "grid.gpkg"
    _grid_gpkg(path, [
        # 只写了 fraction，mean/p95 缺失 => partially written
        _row(1, grid_key="G1", centroid=2, intersecting=2, height_max=11.0, unresolved=0,
             fraction=1.0, mean=None, p95=None),
        # 有质心建筑但源高度本身未知（height_max_m 为 NULL）
        _row(2, grid_key="G2", centroid=3, intersecting=3, height_max=None, unresolved=3,
             fraction=None, mean=None, p95=None),
    ])
    report = inspect_building_grid_height_statistics(path)
    assert report["status"] == "incomplete"
    assert "height_statistics_partially_written" in report["reason_ids"]
    assert "height_statistics_missing_with_centroid_buildings" in report["reason_ids"]
    assert "building_height_unknown_in_source" in report["reason_ids"]
    counts = report["counts"]
    assert counts["rows_statistics_partially_written"] == 1
    assert counts["rows_with_centroid_buildings_but_no_source_height"] == 1
    # 源高度未知的格**不得**被误算成"有数值高度但漏写"
    assert counts["rows_with_usable_height_but_all_statistics_null"] == 0


def test_contract_violation_is_reported(tmp_path):
    path = tmp_path / "grid.gpkg"
    _grid_gpkg(path, [
        # p95 < mean：违反聚合合同
        _row(1, grid_key="G1", centroid=5, intersecting=5, height_max=40.0, unresolved=0,
             fraction=1.0, mean=30.0, p95=10.0),
        _row(2, grid_key="G2", centroid=1, intersecting=1, height_max=8.0, unresolved=0,
             fraction=None, mean=8.0, p95=8.0),
    ])
    report = inspect_building_grid_height_statistics(path)
    assert report["status"] == "incomplete"
    assert "height_statistics_contract_violation" in report["reason_ids"]
    assert report["counts"]["rows_contract_violation"] >= 1


def test_legacy_schema_without_centroid_columns_is_not_applicable(tmp_path):
    path = tmp_path / "legacy.gpkg"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE gpkg_contents (
          table_name TEXT PRIMARY KEY, data_type TEXT, identifier TEXT,
          srs_id INTEGER, min_x REAL, min_y REAL, max_x REAL, max_y REAL);
        CREATE TABLE gpkg_geometry_columns (
          table_name TEXT, column_name TEXT, geometry_type_name TEXT,
          srs_id INTEGER, z INTEGER, m INTEGER);
        CREATE TABLE building_grid_L8 (
          fid INTEGER PRIMARY KEY, geom BLOB, building_count INTEGER,
          height_max_m REAL, valid_height_fraction REAL, height_mean_m REAL,
          height_p95_m REAL);
        INSERT INTO gpkg_contents VALUES
          ('building_grid_L8','features','building_grid_L8',4326,121,30,122,31);
        INSERT INTO gpkg_geometry_columns VALUES
          ('building_grid_L8','geom','POLYGON',4326,0,0);
        """
    )
    connection.commit()
    connection.close()
    report = inspect_building_grid_height_statistics(path)
    assert report["status"] == "not_applicable"
    assert report["reason_ids"] == ["legacy_building_grid_schema_without_centroid_columns"]
    assert report["status"] != "passed"


def test_missing_source_is_unavailable_and_never_raises(tmp_path):
    report = inspect_building_grid_height_statistics(tmp_path / "absent.gpkg")
    assert report["status"] == "unavailable"
    assert report["reason_ids"] == ["height_statistics_unavailable"]
    assert inspect_building_grid_height_statistics(None)["status"] == "unavailable"


def test_inspection_never_writes_the_source(tmp_path):
    path = tmp_path / "grid.gpkg"
    _grid_gpkg(path, [
        _row(1, grid_key="G1", centroid=1, intersecting=1, height_max=7.0, unresolved=0,
             fraction=None, mean=None, p95=None),
    ])
    before = _sha256(path)
    stat_before = path.stat().st_mtime_ns
    inspect_building_grid_height_statistics(path)
    assert _sha256(path) == before
    assert path.stat().st_mtime_ns == stat_before


def test_source_status_keeps_history_and_only_adds_the_report(tmp_path):
    """只报告：默认不打开数据集；显式传入报告时既有 readiness 字段一字不变。"""

    grid = tmp_path / "grid.gpkg"
    _grid_gpkg(grid, [
        _row(1, grid_key="G1", centroid=2, intersecting=2, height_max=15.0, unresolved=0,
             fraction=None, mean=None, p95=None),
    ])
    state = {
        "source_audits": {"items": {
            "buildings": {"status": "verified"},
            "building_grid": {"status": "verified"},
            "terrain_dtm": {"status": "verified"},
        }},
        "grid_attributes": {"population": {"status": "passed"}},
    }
    base = layered_feasibility_source_status(state, terrain_path="D:/dtm.tif")
    assert base["building_grid"]["height_statistics"]["status"] == "not_checked"
    assert base["building_grid"]["height_statistics"]["affects_readiness"] is False
    assert base["building_grid"]["audit_status"] == "verified"
    assert base["terrain"]["available"] is True

    report = inspect_building_grid_height_statistics(grid)
    assert report["status"] == "incomplete"
    enriched = layered_feasibility_source_status(
        state, terrain_path="D:/dtm.tif", height_statistics=report)
    # 既有字段逐键不变
    for key in ("terrain", "terrain_dtm", "population", "buildings"):
        assert enriched[key] == base[key]
    assert enriched["building_grid"]["role"] == base["building_grid"]["role"]
    assert enriched["building_grid"]["audit_status"] == base["building_grid"]["audit_status"]
    # 只有新增的报告子键不同
    assert enriched["building_grid"]["height_statistics"]["status"] == "incomplete"
    assert enriched["building_grid"]["height_statistics"]["contract_id"] == (
        BUILDING_GRID_HEIGHT_STATISTICS_CONTRACT)
