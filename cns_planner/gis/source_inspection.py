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


def inspect_geopackage(path, kind, *, deep_geometry=False):
    source = Path(path)
    if not source.is_file() or source.suffix.lower() != ".gpkg":
        raise ValueError(f"{kind} 必须是存在的 GeoPackage 文件")
    connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
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
    dataset = ogr.Open(str(source), 0)
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
    except (ValueError, OSError) as exc:
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
                                extent, feature_count, *, deep=False):
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
    while True:
        rows = cursor.fetchmany(1024)
        if not rows:
            break
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
