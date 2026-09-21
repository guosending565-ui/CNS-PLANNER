"""建筑 footprint 的几何质量检查与 ``make_valid`` 修复（Phase 3.5，无新算法）。

背景（真实舟山案例定位到的根因）
--------------------------------

`zhoushan_buildings.gpkg` 的原始 WGS84 polygon 在投影到米制 CRS（EPSG:32651）后，
QGIS ``QgsGeometry.transform`` 会把 **每一个** 单部件 polygon 表示成 *MultiPolygon*
（实测走廊内 1481/1481）。既有提取函数只调用 ``asPolygon()``，对 MultiPolygon 抛
``TypeError`` 并被吞掉，于是 ``ring_metric`` 变成空列表，连续验证把它记成
``building_footprint_geometry_invalid`` ⇒ 建筑域 ``unresolved`` ⇒ 运行航路无法发布。

本模块只做**几何质量**这件事，且遵守三条硬约束：

1. **原始数据永不修改**：只读取环坐标，绝不回写 GeoPackage；
2. **优先 ``shapely.make_valid()`` 修复**：修复结果必须自身有效且面积为正才被采用；
3. **修复失败必须保持 unknown**：返回 ``status="invalid"`` 让调用方继续 fail-closed，
   绝不把无法解释的几何当成"没有建筑"或"已验证"。

修复是**保守**的：一个 footprint 的每个部件都会被保留并逐个参与净空判定，因此修复
不会丢掉任何一个可能被穿透的屋顶。多部件只在报告里记录，不改变判定语义。
"""

from __future__ import annotations

from copy import deepcopy

#: 单个 footprint 的几何质量判定。
GEOMETRY_QUALITY_STATUSES = ("passed", "repaired", "invalid")
#: 修复方式 vocabulary。
REPAIR_METHODS = ("not_needed", "shapely_make_valid", "unrepairable")

#: ``ring_metric`` 的语义说明，随每条记录一起输出。
RING_SEMANTICS = {
    "crs": "explicit_local_metric_crs",
    "ring_vertex_source": "source_footprint_ring_vertices_transformed",
    "source_geometry_modified": False,
    "no_simplification": True,
    "no_resampling": True,
    "no_buffer": True,
    "no_interpolation": True,
    "geometry_quality": "checked_and_made_valid_in_memory_only",
}

BUILDING_GEOMETRY_SEMANTICS = {
    "source_modified": False,
    "repair_is_in_memory_only": True,
    "prefer_shapely_make_valid": True,
    "repair_result_must_be_valid_and_positive_area": True,
    "unrepairable_geometry_stays_unknown": True,
    "unknown_is_never_safe": True,
    "every_part_is_evaluated": True,
    "vertex_count_threshold": 3,
}

#: 报告里出现一次就够的解释。
GEOMETRY_QUALITY_NOTES = (
    "几何质量检查只读取源 footprint；不修改 GeoPackage、不写回源几何。",
    "无效几何优先用 shapely.make_valid() 在内存中修复；修复结果自身无效或面积为 0 时保持 unknown。",
    "多部件 footprint 的每个部件都参与净空判定，绝不因为取最大部件而漏判穿透。",
    "unknown != safe：无法解释的几何既不当作没有建筑，也不当作已通过。",
)


def _coordinates(ring):
    points = []
    for point in ring or []:
        if isinstance(point, (list, tuple)) and len(point) >= 2:
            try:
                points.append((float(point[0]), float(point[1])))
            except (TypeError, ValueError):
                return []
        else:
            return []
    return points


def empty_geometry_quality_report():
    return {
        "status": "not_evaluated",
        "evaluated_footprint_count": 0,
        "counts": {"passed": 0, "repaired": 0, "invalid": 0},
        "repair": {
            "applied_count": 0, "failed_count": 0, "method": "shapely_make_valid",
            "repaired_area_ratio_min": None, "repaired_area_ratio_max": None,
            "multi_part_result_count": 0,
        },
        "invalid": [],
        "semantics": deepcopy(BUILDING_GEOMETRY_SEMANTICS),
        "notes": list(GEOMETRY_QUALITY_NOTES),
    }


def assess_footprint_geometry(building_id, ring_metric):
    """评估**一个** footprint 的几何质量，必要时用 ``make_valid`` 修复。

    返回 ``{"status", "parts", "record"}``：

    * ``status="passed"`` —— 原始环直接构成有效多边形；
    * ``status="repaired"`` —— 原始几何无效但 ``make_valid`` 给出了有效结果；
    * ``status="invalid"`` —— 无法解释（顶点不足、面积为零、修复失败）：调用方必须
      保持 ``unresolved``，不得当成 0 或 safe。

    ``parts`` 是**全部**有效部件（修复后的几何若为 MultiPolygon 则逐部件返回），
    因此调用方对每个部件都做一次净空判定即可，不需要再选representative。
    """

    record = {
        "building_id": building_id,
        "status": "invalid",
        "repair_method": "not_needed",
        "repair_applied": False,
        "make_valid_applied": False,
        # ``source_modified`` / ``source_geometry_modified`` are the same fact reported twice:
        # the GeoPackage and the in-memory source ring are never rewritten.
        "source_modified": False,
        "source_geometry_modified": False,
        "source_ring_vertex_count": len(ring_metric or []),
        "part_count": 0,
        "source_area_m2": None,
        "repaired_area_m2": None,
        "repaired_area_ratio": None,
        "multi_part_result": False,
        "validity_reason": None,
    }
    points = _coordinates(ring_metric)
    if len(points) < 3:
        record["validity_reason"] = "ring_vertex_count_below_3"
        return {"status": "invalid", "parts": [], "record": record}

    try:
        from shapely.geometry import Polygon
        from shapely.validation import explain_validity, make_valid
    except ImportError:  # pragma: no cover - shapely is a hard dependency in practice
        record["validity_reason"] = "shapely_unavailable"
        return {"status": "invalid", "parts": [], "record": record}

    polygon = Polygon(points)
    if polygon.is_empty:
        record["validity_reason"] = "empty_polygon"
        return {"status": "invalid", "parts": [], "record": record}
    if polygon.is_valid and polygon.area > 0:
        parts = [polygon]
        record.update({
            "status": "passed", "part_count": 1,
            "source_area_m2": float(polygon.area),
        })
        return {"status": "passed", "parts": parts, "record": record}

    record["validity_reason"] = explain_validity(polygon)
    record["source_area_m2"] = float(polygon.area) if polygon.area == polygon.area else None
    record["repair_method"] = "shapely_make_valid"
    try:
        repaired = make_valid(polygon)
    except Exception as exc:  # noqa: BLE001 -任何修复异常都必须降级为 unknown
        record["validity_reason"] = f"make_valid_raised:{type(exc).__name__}"
        return {"status": "invalid", "parts": [], "record": record}

    parts = _polygonal_parts(repaired)
    parts = [item for item in parts if not item.is_empty and item.area > 0]
    if not parts:
        # make_valid 只返回了线/点（退化几何）：这是"无法解释"，不是"没有建筑"。
        record["repair_method"] = "unrepairable"
        record["validity_reason"] = f"make_valid_result_not_polygonal:{record['validity_reason']}"
        return {"status": "invalid", "parts": [], "record": record}

    repaired_area = float(sum(item.area for item in parts))
    source_area = record["source_area_m2"]
    record.update({
        "status": "repaired",
        "repair_applied": True,
        "make_valid_applied": True,
        "part_count": len(parts),
        "repaired_area_m2": repaired_area,
        "multi_part_result": len(parts) > 1,
        "repaired_area_ratio": (
            None if not source_area else round(repaired_area / source_area, 9)
        ),
    })
    return {"status": "repaired", "parts": parts, "record": record}


def _polygonal_parts(geometry):
    if geometry is None:
        return []
    kind = getattr(geometry, "geom_type", "")
    if kind == "Polygon":
        return [geometry]
    if kind == "MultiPolygon":
        return list(getattr(geometry, "geoms", []) or [])
    if kind == "GeometryCollection":
        parts = []
        for item in getattr(geometry, "geoms", []) or []:
            parts.extend(_polygonal_parts(item))
        return parts
    return []


def merge_geometry_quality(report, record):
    """把一条 footprint 质量记录并入报告（就地累加）。"""

    status = str((record or {}).get("status") or "invalid")
    if status not in GEOMETRY_QUALITY_STATUSES:
        status = "invalid"
    report["evaluated_footprint_count"] = int(report.get("evaluated_footprint_count") or 0) + 1
    counts = report.setdefault("counts", {"passed": 0, "repaired": 0, "invalid": 0})
    counts[status] = int(counts.get(status) or 0) + 1
    repair = report.setdefault("repair", empty_geometry_quality_report()["repair"])
    if record.get("repair_applied"):
        repair["applied_count"] = int(repair.get("applied_count") or 0) + 1
    if status == "invalid":
        repair["failed_count"] = int(repair.get("failed_count") or 0) + 1
        invalid = report.setdefault("invalid", [])
        if len(invalid) < 50:
            invalid.append(dict(record))
    if record.get("multi_part_result"):
        repair["multi_part_result_count"] = int(repair.get("multi_part_result_count") or 0) + 1
    ratio = record.get("repaired_area_ratio")
    if isinstance(ratio, (int, float)) and not isinstance(ratio, bool):
        current_min = repair.get("repaired_area_ratio_min")
        current_max = repair.get("repaired_area_ratio_max")
        repair["repaired_area_ratio_min"] = (
            ratio if current_min is None else min(current_min, ratio)
        )
        repair["repaired_area_ratio_max"] = (
            ratio if current_max is None else max(current_max, ratio)
        )
    report["status"] = (
        "invalid_geometry_present" if int(counts["invalid"] or 0) else
        "repaired_in_memory" if int(counts["repaired"] or 0) else
        "passed"
    )
    return report


def summarize_geometry_quality(records):
    """由逐 footprint 记录生成独立的质量报告（可单独导出/展示）。"""

    report = empty_geometry_quality_report()
    for record in records or []:
        merge_geometry_quality(report, record)
    return report


def _annotation(building_id, ring_metric, quality):
    """把几何质量结果写回 footprint（additive，不改变既有键的语义）。"""

    annotation = {
        "status": quality["status"],
        "radius_basis": None,
        "repair_method": quality["repair_method"],
        "repair_applied": quality["repair_applied"],
        "validity_reason": quality["validity_reason"],
        "part_count": quality["part_count"],
        "multi_part_result": quality["multi_part_result"],
        "repaired_area_ratio": quality["repaired_area_ratio"],
        "source_geometry_modified": False,
        "make_valid_applied": quality["repair_applied"],
    }
    ring_count = len(ring_metric or [])
    annotation["radius_basis"] = "polygon_ring" if ring_count >= 3 else "ring_unavailable"
    annotation["building_id"] = building_id
    return annotation


def _annotation(building_id, part_records, overall_status):
    """把几何质量结果写回 footprint（additive，不改变既有键的语义）。"""

    applied = [record for record in part_records if record["repair_applied"]]
    invalid = [record for record in part_records if record["status"] == "invalid"]
    ratios = [
        record["repaired_area_ratio"] for record in part_records
        if isinstance(record["repaired_area_ratio"], (int, float))
        and not isinstance(record["repaired_area_ratio"], bool)
    ]
    return {
        "status": overall_status,
        "source_ring_count": len(part_records),
        "source_ring_vertex_counts": [record["source_ring_vertex_count"] for record in part_records],
        "valid_part_count": sum(record["part_count"] for record in part_records),
        "invalid_part_count": len(invalid),
        "repair_method": (
            "unrepairable" if invalid else
            "shapely_make_valid" if applied else "not_needed"
        ),
        "repair_applied": bool(applied),
        "make_valid_applied": bool(applied),
        "prefer_shapely_make_valid": True,
        "repair_succeeded": bool(applied) and not invalid,
        "unrepairable_geometry_stays_unknown": True,
        "multi_part_result": any(record["multi_part_result"] for record in part_records),
        "repaired_area_ratio": (
            None if not ratios else round(min(ratios), 9)
        ),
        "validity_reasons": sorted({
            str(record["validity_reason"]) for record in invalid if record["validity_reason"]
        }),
        "source_modified": False,
        "source_geometry_modified": False,
        "radius_basis": "polygon_ring" if part_records else "ring_unavailable",
        "building_id": building_id,
        "part_records": part_records,
    }


def annotate_footprint_geometry(building_id, source_rings):
    """一次调用同时得到质量记录与**全部**有效部件环（GIS 边界与验证器共用）。

    这是"增加 geometry quality 检查"的唯一实现：环提取、``make_valid`` 修复、
    多部件展开都在这里完成，调用方不再各自实现一套。

    ``source_rings`` 可以是单个环（``[[x, y], ...]``）或由多个环组成的 footprint
    （``[[[x, y], ...], ...]``）。源几何本身由调用方（GIS 边界）从 GeoPackage 读出，
    本函数**不改写它**，只返回内存中的有效部件。
    """

    rings = _normalize_source_rings(source_rings)
    part_records = []
    parts_metric = []
    for index, ring in enumerate(rings):
        seen_id = f"{building_id}#part{index}"
        quality = assess_footprint_geometry(seen_id, ring)
        part_records.append({**quality["record"], "seen_building_id": seen_id})
        parts_metric.extend([
            [[float(x), float(y)] for x, y in polygon.exterior.coords]
            for polygon in quality["parts"]
        ])
    status = (
        "invalid" if any(record["status"] == "invalid" for record in part_records) else
        "repaired" if any(record["status"] == "repaired" for record in part_records) else
        "passed"
    )
    return {
        "quality": part_records if len(part_records) > 1 else (part_records[0] if part_records else None),
        "part_records": part_records,
        "status": status,
        "parts_metric": parts_metric,
        "annotation": _annotation(building_id, part_records, status),
    }


def _normalize_source_rings(source_rings):
    """把"单个环"与"多个环的 footprint"统一成 ``[[ring, ...]]``，并保持输入不变。"""

    value = source_rings or []
    if not value:
        return []
    first = value[0]
    nested = (
        isinstance(first, (list, tuple)) and len(first) > 0
        and isinstance(first[0], (list, tuple))
    )
    return [list(ring) for ring in value] if nested else [list(value)]


def prepare_footprint_polygons(footprint):
    """把一条 footprint 证据（含 ``ring_metric`` / ``ring_parts_metric``）变成有效多边形部件。

    这是验证器侧的唯一入口：``Ring 顶点 → shapely polygon → (必要时) make_valid →
    全部有效部件``。返回值语义：

    * ``polygons`` —— 所有必须参与净空判定的部件（多部件一个不漏）；
    * ``quality``  —— ``"passed"`` / ``"repaired"`` / ``"invalid"``；
    * ``record``   —— 可并入 quality report 的单条质量记录。

    ``quality="invalid"`` 时 ``polygons`` 为空，调用方必须继续 fail-closed（unknown），
    绝不允许把它当成"没有建筑"或"已验证"。
    """

    source = footprint.get("ring_parts_metric") or footprint.get("ring_metric") or []
    annotated = annotate_footprint_geometry(
        footprint.get("building_id"), source,
    )
    polygons = []
    try:
        from shapely.geometry import Polygon

        for ring in annotated["parts_metric"]:
            polygon = Polygon(ring)
            if polygon.is_empty or polygon.area <= 0 or not polygon.is_valid:
                continue
            polygons.append(polygon)
    except ImportError:  # pragma: no cover
        polygons = []
    quality = annotated["status"]
    if quality != "invalid" and not polygons:
        quality = "invalid"
    return {
        "parts_metric": annotated["parts_metric"],
        "polygons": polygons,
        "quality": quality,
        "annotation": annotated["annotation"],
        "records": annotated["part_records"],
    }


__all__ = [
    "BUILDING_GEOMETRY_SEMANTICS", "GEOMETRY_QUALITY_NOTES", "GEOMETRY_QUALITY_STATUSES",
    "REPAIR_METHODS", "RING_SEMANTICS", "annotate_footprint_geometry",
    "assess_footprint_geometry", "empty_geometry_quality_report", "merge_geometry_quality",
    "prepare_footprint_polygons", "summarize_geometry_quality",
]
