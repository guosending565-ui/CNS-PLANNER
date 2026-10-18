"""Lightweight read-only source inspection used before expensive GIS loading."""

from __future__ import annotations

import sqlite3
from pathlib import Path


BUILDING_FIELDS = {
    "height_m", "height_var", "height_status", "id", "source",
}
BUILDING_GRID_FIELDS = {
    "grid_level", "building_count", "building_area_m2",
    "building_coverage_ratio", "height_mean_m", "height_p95_m",
    "height_max_m", "valid_height_fraction", "west", "south", "east", "north",
}

# --------------------------------------------------------------- 高度统计完整性校验
#
# Round36-D3 阶段B：**只读**完整性校验，只报告、不改变任何 readiness 语义。
#
# 背景（真实缺陷）：L8 v3 网格的 ``valid_height_fraction`` / ``height_mean_m`` /
# ``height_p95_m`` 三列曾经 143,013 行全为 NULL，而 ``centroid_building_count`` /
# ``height_max_m`` 等事实列完整。生产 SQL 读这三列，于是每个含建筑格都落入
# fail-closed ``unknown``（``building_height_or_ground_elevation_unresolved``），
# Theta* 直接 ``no_path``。该缺陷在数据侧已由版本化数据集修复，但**没有**任何校验能
# 在读取时发现"事实列有值、统计列漏写"这种形态，因此本轮补上。

#: 校验的合同标识（版本化；语义变更必须改这个字符串）。
BUILDING_GRID_HEIGHT_STATISTICS_CONTRACT = "building_grid_height_statistics_completeness_v1"

#: 该合同对 ``centroid_building_count > 0`` 的格要求（v1）：
#:
#: * ``valid_height_fraction`` 必须写出且在 ``[0, 1]``；
#: * 有可用源高度（``height_max_m`` 非 NULL）⇒ ``height_mean_m`` / ``height_p95_m`` 必须写出；
#: * 无可用源高度（``height_max_m`` 为 NULL）⇒ ``height_mean_m`` / ``height_p95_m`` 必须为 NULL
#:   （**强制** NULL：禁止用 0 填充未知高度），此时该格仍算"合同完整"；
#: * ``height_mean_m <= height_p95_m <= height_max_m``（容差内）。
#:
#: 由此，"三列全缺" 才是真实缺陷形态；而"fraction=0 且 mean/p95 为 NULL" 是合法结论。
HEIGHT_STATISTICS_CONTRACT_NOTES = (
    "fraction_required_when_centroid_buildings_present",
    "mean_and_p95_required_when_a_usable_source_height_exists",
    "mean_and_p95_must_be_null_when_no_usable_source_height_exists",
    "monotonicity_mean_le_p95_le_max",
)

#: 必须同时存在的三个统计列。
HEIGHT_STATISTICS_FIELDS = ("valid_height_fraction", "height_mean_m", "height_p95_m")

#: 判定"该格有建筑质心分配"所需的列。
HEIGHT_STATISTICS_REQUIRED_FACTS = (
    "centroid_building_count", "intersecting_building_count", "height_unresolved_count",
)

#: 分类 / 原因的稳定 id（前端与报告可直接消费）。
HEIGHT_STATISTICS_REASON_IDS = (
    "height_statistics_complete",
    "height_statistics_missing_with_centroid_buildings",
    "height_statistics_partially_written",
    "height_statistics_contract_violation",
    "height_statistics_unavailable",
    "legacy_building_grid_schema_without_centroid_columns",
    "footprint_coverage_without_centroid_height",
    "confirmed_zero_building_coverage",
    "building_height_unknown_in_source",
)

#: 浮点比较容差（统计量之间只做量级校验，不做位级相等）。
_STATISTICS_TOLERANCE = 1e-9


def inspect_building_grid_height_statistics(path, *, layer=None):
    """**只读**校验 L8 建筑网格的高度统计完整性（永不放宽 fail-closed，永不改写源）。

    返回的 ``status``：

    * ``passed``      —— 所有"有质心建筑"的格三列齐全且满足聚合合同；
    * ``incomplete``  —— 存在质心建筑数 > 0 但统计列漏写／部分漏写／违反合同的格；
    * ``not_applicable`` —— 该网格没有质心口径列（旧 v1 事实表），本校验不适用；
    * ``unavailable`` —— 文件/图层/列不可读。

    ``reason_ids`` 是稳定的机器可读原因列表；``counts`` 给出每一类的格数，且各类
    **不是互斥的**（同一格可同时"有质心建筑"又"高度本身未知"），因此同时给出按显式
    优先级的 ``classification_counts``。

    合同细节（v1，见 :data:`HEIGHT_STATISTICS_CONTRACT_NOTES`）：当 ``height_max_m`` 为
    NULL（该格没有任何可用源高度）时，``height_mean_m`` / ``height_p95_m`` **必须**为
    NULL —— 这是"禁止用 0 填充未知高度"的强制结果，因此这种格算合同完整，不得被误报成
    "统计字段漏写"；真正要报的是"有数值高度但三列全缺"。

    本函数只做只读 SQL（``mode=ro``），**不改写、不修复**任何数据集，也**不**参与
    ``layered_feasibility_source_status`` 的历史 readiness 语义（调用方只按报告消费）。
    """

    source = Path(path) if path else None
    base = {
        "contract_id": BUILDING_GRID_HEIGHT_STATISTICS_CONTRACT,
        "role": "building_grid_height_statistics",
        "read_only": True,
        "affects_readiness": False,
        "path": str(source) if source else None,
        "layer": layer,
        "status": "unavailable",
        "reason_ids": ["height_statistics_unavailable"],
        "reason": "building_grid_source_missing",
    }
    if source is None or not source.is_file():
        return base

    try:
        connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    except sqlite3.Error as exc:  # pragma: no cover - 只读打开失败
        return {**base, "reason": f"{type(exc).__name__}: {exc}"}
    try:
        try:
            table, columns = _height_statistics_table(connection, layer)
        except ValueError as exc:
            return {**base, "status": "not_applicable", "reason": str(exc),
                    "reason_ids": ["legacy_building_grid_schema_without_centroid_columns"]}
        missing = [name for name in (*HEIGHT_STATISTICS_REQUIRED_FACTS,
                                    *HEIGHT_STATISTICS_FIELDS) if name not in columns]
        if missing:
            return {
                **base, "layer": table, "status": "not_applicable",
                "reason": f"building_grid 缺少高度统计列：{', '.join(sorted(missing))}",
                "reason_ids": ["legacy_building_grid_schema_without_centroid_columns"],
                "missing_columns": sorted(missing),
            }
        quoted = _quote(table)
        has_zero_flag = "zero_coverage_confirmed" in columns
        zero_expression = (
            f'COALESCE("{_escape("zero_coverage_confirmed")}", 0)' if has_zero_flag else "0"
        )
        row = connection.execute(
            f"SELECT COUNT(*), "
            f"SUM(CASE WHEN COALESCE(\"centroid_building_count\",0)>0 THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN COALESCE(\"centroid_building_count\",0)=0 "
            f"AND COALESCE(\"intersecting_building_count\",0)>0 THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN COALESCE(\"centroid_building_count\",0)=0 "
            f"AND COALESCE(\"intersecting_building_count\",0)=0 THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN COALESCE(\"centroid_building_count\",0)>0 "
            f"AND \"height_max_m\" IS NULL THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN COALESCE(\"centroid_building_count\",0)>0 "
            f"AND \"valid_height_fraction\" IS NULL AND \"height_mean_m\" IS NULL "
            f"AND \"height_p95_m\" IS NULL THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN COALESCE(\"centroid_building_count\",0)>0 "
            f"AND \"height_max_m\" IS NOT NULL AND \"valid_height_fraction\" IS NULL "
            f"AND \"height_mean_m\" IS NULL AND \"height_p95_m\" IS NULL THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN COALESCE(\"centroid_building_count\",0)=0 "
            f"AND {zero_expression} THEN 1 ELSE 0 END), "
            # 满足聚合合同的格：三列齐全；或该格**没有任何可用源高度**时
            # （height_max_m 为 NULL）mean/p95 必须为 NULL、而 fraction 必须已写出。
            # 后者是**强制**的 NULL（"不得用 0 填充未知高度"），不能算作"漏写"。
            f"SUM(CASE WHEN COALESCE(\"centroid_building_count\",0)>0 "
            f"AND \"valid_height_fraction\" IS NOT NULL AND ("
            f"(\"height_max_m\" IS NOT NULL AND \"height_mean_m\" IS NOT NULL "
            f"AND \"height_p95_m\" IS NOT NULL) OR (\"height_max_m\" IS NULL "
            f"AND \"height_mean_m\" IS NULL AND \"height_p95_m\" IS NULL)) "
            f"THEN 1 ELSE 0 END), "
            # 部分写入：既不是合同完整格，也不是"三列全缺"的漏写格。
            f"SUM(CASE WHEN COALESCE(\"centroid_building_count\",0)>0 AND NOT ("
            f"\"valid_height_fraction\" IS NOT NULL AND ("
            f"(\"height_max_m\" IS NOT NULL AND \"height_mean_m\" IS NOT NULL "
            f"AND \"height_p95_m\" IS NOT NULL) OR (\"height_max_m\" IS NULL "
            f"AND \"height_mean_m\" IS NULL AND \"height_p95_m\" IS NULL))) "
            f"AND NOT (\"valid_height_fraction\" IS NULL AND \"height_mean_m\" IS NULL "
            f"AND \"height_p95_m\" IS NULL) THEN 1 ELSE 0 END) "
            f"FROM {quoted}"
        ).fetchone()
        (total, with_centroid, footprint_only, neither, centroid_without_height,
         centroid_all_null, present_height_all_null, zero_confirmed, complete,
         partial) = (int(value or 0) for value in row)
        violations = _height_statistics_violations(connection, quoted)
        counts = {
            "rows_total": total,
            "rows_with_centroid_buildings": with_centroid,
            "rows_statistics_complete": complete,
            "rows_where_all_three_statistics_null": centroid_all_null,
            "rows_with_usable_height_but_all_statistics_null": present_height_all_null,
            "rows_statistics_partially_written": partial,
            "rows_contract_violation": violations,
            "rows_footprint_only_without_centroid_height": footprint_only,
            "rows_confirmed_zero_coverage": zero_confirmed,
            "rows_with_centroid_buildings_but_no_source_height": centroid_without_height,
            "rows_with_no_building_and_no_confirmation": max(0, neither - zero_confirmed),
        }
        reason_ids = []
        if centroid_all_null:
            reason_ids.append("height_statistics_missing_with_centroid_buildings")
        if partial:
            reason_ids.append("height_statistics_partially_written")
        if violations:
            reason_ids.append("height_statistics_contract_violation")
        if not reason_ids:
            reason_ids.append("height_statistics_complete")
        if footprint_only:
            reason_ids.append("footprint_coverage_without_centroid_height")
        if zero_confirmed:
            reason_ids.append("confirmed_zero_building_coverage")
        if centroid_without_height:
            reason_ids.append("building_height_unknown_in_source")
        incomplete = bool(centroid_all_null or partial or violations)
        reason = None
        if incomplete:
            reason = (
                "建筑质心数 > 0 但高度统计列不完整："
                f"{centroid_all_null} 格三列全缺失、{partial} 格部分缺失、"
                f"{violations} 格违反聚合合同。生产侧会因此把这些格判为 fail-closed "
                "unknown（building_height_or_ground_elevation_unresolved）。"
            )
        return {
            **base,
            "layer": table,
            "status": "incomplete" if incomplete else "passed",
            "reason_ids": reason_ids,
            "reason": reason,
            "fields_present": sorted(
                name for name in (*HEIGHT_STATISTICS_REQUIRED_FACTS, *HEIGHT_STATISTICS_FIELDS)
                if name in columns),
            "counts": counts,
            "classification_precedence": [
                "height_statistics_contract_violation",
                "height_statistics_partially_written",
                "height_statistics_missing_with_centroid_buildings",
                "building_height_unknown_in_source",
                "height_statistics_complete",
                "footprint_coverage_without_centroid_height",
                "confirmed_zero_building_coverage",
            ],
        }
    except sqlite3.DatabaseError as exc:
        return {**base, "reason": f"{type(exc).__name__}: {exc}"}
    finally:
        connection.close()


def _escape(identifier):
    return str(identifier).replace('"', '""')


def _height_statistics_table(connection, layer):
    """定位建筑网格图层：显式层名优先，其次 ``buildings`` 匹配，最后唯一 features 图层。"""

    rows = connection.execute(
        "SELECT c.table_name FROM gpkg_contents c "
        "JOIN gpkg_geometry_columns g ON g.table_name=c.table_name "
        "WHERE c.data_type='features'"
    ).fetchall()
    names = [str(row[0]) for row in rows]
    if not names:
        raise ValueError("GeoPackage 中没有 features 图层")
    if layer:
        chosen = next((name for name in names if name == str(layer)), None)
        if chosen is None:
            raise ValueError(f"GeoPackage 中没有图层 {layer}")
    else:
        chosen = next((name for name in names if "building_grid" in name.lower()), None)
        chosen = chosen or next((name for name in names if "building" in name.lower()), None)
        if chosen is None:
            chosen = names[0] if len(names) == 1 else None
        if chosen is None:
            raise ValueError("GeoPackage 中有多个图层且无法确定建筑网格图层")
    columns = {
        str(row[1]) for row in connection.execute(f"PRAGMA table_info({_quote(chosen)})")
    }
    if not columns:
        raise ValueError(f"图层 {chosen} 不可读")
    return chosen, columns


def _height_statistics_violations(connection, quoted):
    """统计列已写入但违反聚合合同的格数（只数量级校验，不做位级相等）。"""

    tolerance = _STATISTICS_TOLERANCE
    sql = (
        "SELECT COUNT(*) FROM {table} WHERE "
        '("valid_height_fraction" IS NOT NULL AND ("valid_height_fraction"<0 '
        'OR "valid_height_fraction">1)) '
        'OR ("height_mean_m" IS NOT NULL AND "height_mean_m"<0) '
        'OR ("height_p95_m" IS NOT NULL AND "height_p95_m"<0) '
        'OR ("height_mean_m" IS NOT NULL AND "height_p95_m" IS NOT NULL '
        'AND "height_p95_m" + {tolerance} < "height_mean_m") '
        'OR ("height_max_m" IS NOT NULL AND "height_mean_m" IS NOT NULL '
        'AND "height_mean_m" > "height_max_m" + {tolerance}) '
        'OR ("height_max_m" IS NOT NULL AND "height_p95_m" IS NOT NULL '
        'AND "height_p95_m" > "height_max_m" + {tolerance}) '
        'OR ("height_max_m" IS NULL AND ("height_mean_m" IS NOT NULL '
        'OR "height_p95_m" IS NOT NULL))'
    ).format(table=quoted, tolerance=tolerance)
    return int(connection.execute(sql).fetchone()[0])


def inspect_geopackage(path, kind, *, deep_geometry=False, cancel_check=None, on_progress=None):
    source = Path(path)
    if not source.is_file() or source.suffix.lower() != ".gpkg":
        raise ValueError(f"{kind} 必须是存在的 GeoPackage 文件")
    connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    if cancel_check:
        def check_sql_cancel():
            cancel_check()
            return 0
        connection.set_progress_handler(check_sql_cancel, 10000)
    try:
        rows = connection.execute(
            "SELECT c.table_name, c.srs_id, c.min_x, c.min_y, c.max_x, c.max_y, "
            "g.column_name, g.geometry_type_name "
            "FROM gpkg_contents c JOIN gpkg_geometry_columns g ON g.table_name=c.table_name "
            "WHERE c.data_type='features'"
        ).fetchall()
        expected = "building_grid" if kind == "building_grid" else "buildings"
        match = next((row for row in rows if expected in str(row[0]).lower()), rows[0] if len(rows) == 1 else None)
        if match is None:
            raise ValueError(f"GeoPackage 中没有可识别的 {kind} 图层")
        table, srs_id, min_x, min_y, max_x, max_y, geometry_column, geometry_type = match
        if "POLYGON" not in str(geometry_type).upper():
            raise ValueError(f"{kind} 图层必须是 Polygon/MultiPolygon")
        columns = {
            row[1]: row[2] for row in connection.execute(
                f"PRAGMA table_info({_quote(table)})"
            ).fetchall()
        }
        required = BUILDING_GRID_FIELDS if kind == "building_grid" else BUILDING_FIELDS
        missing = sorted(required - set(columns))
        if missing:
            raise ValueError(f"{kind} 图层缺少字段：{', '.join(missing)}")
        count = connection.execute(f"SELECT COUNT(*) FROM {_quote(table)}").fetchone()[0]
        rtree = f"rtree_{table}_{geometry_column}"
        has_rtree = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (rtree,)
        ).fetchone() is not None
        if kind == "buildings" and not has_rtree:
            raise ValueError("buildings GeoPackage 缺少空间索引，拒绝全表运行时扫描")
        valid_height = None
        if kind == "buildings":
            valid_height = connection.execute(
                f"SELECT COUNT(*) FROM {_quote(table)} WHERE height_m IS NOT NULL"
            ).fetchone()[0]
        geometry_health = _geopackage_geometry_health(
            connection, table, geometry_column, geometry_type,
            [min_x, min_y, max_x, max_y], int(count), deep=deep_geometry,
            cancel_check=cancel_check, on_progress=on_progress,
        )
        return {
            "status": "passed", "path": str(source.resolve()), "driver": "GPKG",
            "size_bytes": source.stat().st_size, "mtime_ns": source.stat().st_mtime_ns,
            "layer": table, "geometry_column": geometry_column,
            "geometry_type": geometry_type, "crs": f"EPSG:{srs_id}" if srs_id else None,
            "srs_id": srs_id, "extent": [min_x, min_y, max_x, max_y],
            "feature_count": int(count), "fields": columns,
            "spatial_index": has_rtree, "rtree_table": rtree if has_rtree else None,
            "valid_height_count": int(valid_height) if valid_height is not None else None,
            "valid_height_fraction": (valid_height / count) if valid_height is not None and count else None,
            "geometry_health": geometry_health,
        }
    except sqlite3.DatabaseError as exc:
        if cancel_check:
            cancel_check()
        raise ValueError(f"无法读取 {kind} GeoPackage：{exc}") from exc
    finally:
        connection.close()


def inspect_vector_dataset(path, *, expect_polygon=True):
    """**通用**矢量数据集自省（不套用任何业务 schema）。

    为什么需要它：``inspect_geopackage`` 是**建筑专用**契约——它要求
    ``height_m`` / ``source`` 等 buildings 字段，并强制要求空间索引。把制图陆地面
    （``cartographic_land``）塞进那套 schema 会得到"缺少字段 height_m"这种与数据无关
    的失败。本函数只回答与"能不能当矢量面用"直接相关的事实：

    * 驱动 / 图层名 / 几何类型 / 要素数 / 图层范围 / 字段名 / CRS；
    * 是否面几何（``expect_polygon`` 时非面即失败）；
    * 是否带空间索引（仅作**事实**报告，不强制——小体量 GeoJSON 没有 rtree 也完全可用）。

    只用 OGR 读取元数据（不遍历几何），失败即抛 ``ValueError``（调用方负责翻译）。
    """

    from osgeo import ogr, osr

    source = Path(path)
    if not source.is_file():
        raise ValueError("矢量数据源不存在")
    try:
        dataset = ogr.Open(str(source), 0)
    except Exception as exc:  # noqa: BLE001 - OGR 对损坏文件会抛 RuntimeError
        raise ValueError(f"矢量数据源无法打开：{exc}") from exc
    if dataset is None:
        raise ValueError("矢量数据源无法打开")
    try:
        if dataset.GetLayerCount() < 1:
            raise ValueError("矢量数据源中没有图层")
        layer = dataset.GetLayerByIndex(0)
        name = str(layer.GetName() or "")
        spatial = layer.GetSpatialRef()
        authority = None
        if spatial is not None:
            code = spatial.GetAuthorityCode(None)
            if code:
                authority = f"EPSG:{code}"
            elif spatial.ExportToProj4():
                authority = spatial.ExportToProj4().strip()
        definition = layer.GetLayerDefn()
        fields = [
            str(definition.GetFieldDefn(index).GetName())
            for index in range(definition.GetFieldCount())
        ]
        declared = layer.GetGeomType() or ogr.wkbUnknown
        geometry_type = ogr.GeometryTypeToName(declared)
        is_polygon = "POLYGON" in str(geometry_type).upper()
        if not is_polygon:
            # 图层可能声明为 wkbUnknown（混合 Polygon / MultiPolygon 数据集的常见写法），
            # 这时按**第一个非空要素**的真实几何类型判定，而不是按声明字符串。
            layer.ResetReading()
            for feature in layer:
                geometry = feature.GetGeometryRef()
                if geometry is None or geometry.IsEmpty():
                    continue
                actual = ogr.GeometryTypeToName(geometry.GetGeometryType())
                geometry_type = f"{geometry_type} (first feature: {actual})"
                is_polygon = "POLYGON" in str(actual).upper()
                break
            layer.ResetReading()
        extent = layer.GetExtent(force=True)
        options = []
        try:
            options = list(layer.GetMetadataItem("GEOMETRY_INDEX") or [])
        except (AttributeError, TypeError):
            options = []
        return {
            "status": "passed",
            "path": str(source.resolve()),
            "format": source.suffix.lower().lstrip("."),
            "layer": name,
            "geometry_type": geometry_type,
            "is_polygon": is_polygon,
            "feature_count": int(layer.GetFeatureCount(force=True)),
            "crs": authority,
            "extent": (
                [float(extent[0]), float(extent[2]), float(extent[1]), float(extent[3])]
                if extent else None
            ),
            "fields": fields,
            "has_geometry_index": bool(layer.TestCapability(ogr.OLCFastSpatialFilter)),
            "geometry_index_metadata": options,
            "driver": str(dataset.GetDriver().GetDescription() or ""),
            "read_only": True,
        }
    finally:
        dataset = None


def inspect_cartographic_land(path):
    """制图陆地面（``cartographic_land``）的**独立**自省：只读，失败不抛异常。

    与 :func:`inspect_geopackage` 的 buildings schema **完全无关**：它不要求任何业务
    字段、也不要求空间索引，只判断"是不是一个可用的面矢量数据集"。
    """

    try:
        record = inspect_vector_dataset(path, expect_polygon=True)
    except ImportError as exc:
        # 没有 OGR/GDAL 运行时：**不能**报告可用（那会让上层在真正读取时才发现失败）。
        return {
            "status": "unavailable",
            "path": str(path) if path else None,
            "reason": f"缺少 OGR/GDAL 运行时，无法自省制图陆地面：{exc}",
            "is_polygon": None,
            "feature_count": None,
        }
    except (ValueError, OSError, RuntimeError) as exc:
        return {
            "status": "unavailable",
            "path": str(path) if path else None,
            "reason": f"制图陆地面矢量自省失败：{exc}",
            "is_polygon": None,
            "feature_count": None,
        }
    if record is None:  # pragma: no cover - 上面的分支已覆盖
        return {"status": "unavailable", "path": str(path), "reason": "制图陆地面无法自省"}
    if not record["is_polygon"]:
        return {
            **record,
            "status": "not_polygon",
            "reason": f"制图陆地面必须是 Polygon/MultiPolygon，实际为 {record['geometry_type']}",
        }
    return record


def _quote(identifier):
    return '"' + str(identifier).replace('"', '""') + '"'


def _geopackage_geometry_health(connection, table, geometry_column, geometry_type,
                                extent, feature_count, *, deep=False, cancel_check=None, on_progress=None):
    """Read-only geometry audit; never repairs or rewrites a GeoPackage."""

    null_count = int(connection.execute(
        f"SELECT COUNT(*) FROM {_quote(table)} WHERE {_quote(geometry_column)} IS NULL"
    ).fetchone()[0])
    base = {
        "status": "not_fully_checked", "feature_count": int(feature_count),
        "null": null_count, "empty": None, "invalid": None,
        "unsupported": 0 if "POLYGON" in str(geometry_type).upper() else int(feature_count - null_count),
        "extent": list(extent), "repair_applied": False,
        "method": "gpkg_schema_and_null_scan", "topology_checked": False,
    }
    if not deep:
        return base
    try:
        import shapely
    except ImportError:
        base["reason"] = "shapely_required_for_explicit_geometry_verification"
        return base

    counts = {"null": null_count, "empty": 0, "invalid": 0, "unsupported": 0}
    cursor = connection.execute(
        f"SELECT {_quote(geometry_column)} FROM {_quote(table)} "
        f"WHERE {_quote(geometry_column)} IS NOT NULL"
    )
    processed = null_count
    while True:
        if cancel_check:
            cancel_check()
        rows = cursor.fetchmany(1024)
        if not rows:
            break
        processed += len(rows)
        if on_progress:
            on_progress(processed, feature_count)
        wkbs, parse_failures = [], 0
        for (blob,) in rows:
            try:
                wkbs.append(_gpkg_wkb(blob))
            except (TypeError, ValueError, IndexError):
                wkbs.append(None)
                parse_failures += 1
        counts["invalid"] += parse_failures
        valid_wkbs = [value for value in wkbs if value is not None]
        if not valid_wkbs:
            continue
        geometries = shapely.from_wkb(valid_wkbs, on_invalid="ignore")
        for geometry in geometries:
            if geometry is None:
                counts["invalid"] += 1
            elif geometry.geom_type not in ("Polygon", "MultiPolygon"):
                counts["unsupported"] += 1
            elif geometry.is_empty:
                counts["empty"] += 1
            elif not geometry.is_valid:
                counts["invalid"] += 1
    unhealthy = sum(counts.values())
    return {
        "status": "passed" if unhealthy == 0 else "blocked",
        "feature_count": int(feature_count), **counts, "extent": list(extent),
        "repair_applied": False, "method": "gpkg_wkb_shapely_is_valid_scan",
        "topology_checked": True,
        "reason": None if unhealthy == 0 else "invalid_geometry_fail_closed",
    }


def _gpkg_wkb(blob):
    """Strip the GeoPackage binary header and return its embedded WKB."""

    value = bytes(blob)
    if value[:2] != b"GP":
        return value
    if len(value) < 8:
        raise ValueError("truncated GeoPackage geometry header")
    envelope_indicator = (value[3] >> 1) & 0b111
    envelope_bytes = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}.get(envelope_indicator)
    if envelope_bytes is None:
        raise ValueError("unsupported GeoPackage envelope indicator")
    offset = 8 + envelope_bytes
    if len(value) <= offset:
        raise ValueError("missing WKB payload")
    return value[offset:]


# --------------------------------------------------------------- audit geometry probe
#
# Round35-D：source audit 的 `verify()` 过去只算 SHA-256，不探测几何事实，于是刷新后
# `feature_count` / `extent` / `declared_crs` 只能是 null（源签名变化时既有的派生事实
# 会被刻意丢弃，见 `domain/source_audit.source_manifest`）。下面这个探测器只读元数据，
# **永不抛异常**，并用多后端依次兜底（pyogrio → GDAL/OGR → GeoPackage sqlite 元数据），
# 因为产品运行环境（QGIS Python 有 osgeo 无 pyogrio）与本机分析环境（有 pyogrio 无
# osgeo）恰好互补。


def _probe_facts_via_pyogrio(source, layer_name):
    import pyogrio

    info = pyogrio.read_info(str(source), layer=layer_name)
    bounds = info.get("total_bounds")
    return {
        "layer": info.get("layer") or layer_name,
        "geometry_type": info.get("geometry_type"),
        "feature_count": (
            int(info["features"]) if info.get("features") is not None else None
        ),
        "extent": (
            [float(bounds[0]), float(bounds[1]), float(bounds[2]), float(bounds[3])]
            if bounds is not None and len(bounds) == 4 else None
        ),
        "declared_crs": str(info.get("crs")) if info.get("crs") else None,
    }


def _probe_facts_via_ogr(source, layer_name):
    facts = inspect_vector_dataset(str(source))
    return {
        "layer": facts.get("layer"),
        "geometry_type": facts.get("geometry_type"),
        "feature_count": facts.get("feature_count"),
        "extent": facts.get("extent"),
        "declared_crs": facts.get("crs"),
    }


def _probe_facts_via_gpkg_sqlite(source, layer_name):
    """GeoPackage 兜底：只读 ``gpkg_contents`` / ``gpkg_spatial_ref_sys`` + 行数。"""

    if source.suffix.lower() != ".gpkg":
        return None
    connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT table_name, srs_id, min_x, min_y, max_x, max_y "
            "FROM gpkg_contents WHERE data_type = 'features'"
        ).fetchall()
        if not rows:
            return None
        chosen = None
        for row in rows:
            if layer_name and str(row[0]) == str(layer_name):
                chosen = row
                break
        chosen = chosen or rows[0]
        table, srs_id, min_x, min_y, max_x, max_y = chosen
        if not layer_name and len(rows) > 1:
            return None  # 多图层且未指定层名：不猜，交给上层显式指定
        safe_table = str(table).replace('"', '""')
        count = connection.execute(f'SELECT COUNT(*) FROM "{safe_table}"').fetchone()[0]
        declared_crs = None
        if srs_id is not None:
            ref = connection.execute(
                "SELECT organization, organization_coordsys_id FROM gpkg_spatial_ref_sys "
                "WHERE srs_id = ?", (int(srs_id),),
            ).fetchone()
            if ref and str(ref[0]).upper() == "EPSG" and ref[1] is not None:
                declared_crs = f"EPSG:{int(ref[1])}"
        extent = None
        if None not in (min_x, min_y, max_x, max_y):
            extent = [float(min_x), float(min_y), float(max_x), float(max_y)]
        return {
            "layer": str(table),
            "feature_count": int(count),
            "extent": extent,
            "declared_crs": declared_crs,
        }
    finally:
        connection.close()


def probe_vector_source_facts(path, *, layer_name=None):
    """只读探测矢量源的**登记几何事实**（要素数 / 范围 / 声明 CRS）。

    与 :func:`inspect_vector_dataset` 的区别：本函数**永不抛异常**，探测失败返回
    ``{"status": "not_probed", ...}``。它服务于 source audit 刷新时补齐几何事实，
    因此探测失败**不得**阻断验证流程，也**绝不**改写或修复源文件。
    """

    source = Path(path) if path else None
    if source is None or not source.is_file():
        return {"status": "not_probed", "reason": "source_file_missing"}
    attempts = []
    for backend, probe in (
        ("pyogrio", _probe_facts_via_pyogrio),
        ("osgeo_ogr", _probe_facts_via_ogr),
        ("gpkg_sqlite", _probe_facts_via_gpkg_sqlite),
    ):
        try:
            facts = probe(source, layer_name)
        except Exception as exc:  # noqa: BLE001 - 后端缺失/文件损坏都只记录不抛出
            attempts.append(f"{backend}:{type(exc).__name__}")
            continue
        if facts:
            return {
                "status": "passed",
                "backend": backend,
                "read_only": True,
                "attempts": attempts,
                "path_suffix": source.suffix.lower(),
                **facts,
            }
        attempts.append(f"{backend}:unavailable")
    return {"status": "not_probed", "reason": "no_readable_vector_backend", "attempts": attempts}
