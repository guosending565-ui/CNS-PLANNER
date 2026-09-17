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
