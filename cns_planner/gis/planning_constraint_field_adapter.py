"""Pure spatial mapping helpers for Planning Constraint Field generation."""

from __future__ import annotations

from copy import deepcopy

from ..domain.restricted_area import (
    normalize_restricted_area, normalize_restricted_area_collection,
)
#: 水平净空换算复用既有塔障碍适配器的实现：绝不新造第二套空间模型。
from .layered_feasibility_adapter import horizontal_half_degrees


def restricted_areas_by_cell(grid_cells, restricted_areas, *, grid_crs="OGC:CRS84"):
    result = {str(cell.get("grid_id")): [] for cell in grid_cells or [] if cell.get("grid_id")}
    for raw in restricted_areas or []:
        try:
            area = normalize_restricted_area(raw)
        except ValueError as exc:
            area = {
                "feature_id": str((raw or {}).get("feature_id") or "unidentified"),
                "geometry_status": "unknown", "planning_geometry": None,
                "geometry": (raw or {}).get("geometry"), "geometry_crs": (raw or {}).get("geometry_crs"),
                "normalization_error": str(exc), "confirmed": False,
                "constraint_type": str((raw or {}).get("constraint_type") or "conditional"),
                "domain": str((raw or {}).get("domain") or "critical_site"),
            }
        for cell in grid_cells or []:
            grid_id, bbox = str(cell.get("grid_id") or ""), cell.get("bbox")
            if not grid_id or not _bbox(bbox):
                continue
            geometry = area.get("planning_geometry")
            source_point_only = area.get("geometry_status") == "source_point_only_no_protection_geometry"
            candidate = area.get("geometry") if source_point_only else geometry
            if str(area.get("geometry_crs") or "") != str(grid_crs or ""):
                # Preserve possible applicability without pretending two CRS are equal.
                if _geometry_bbox(candidate) is not None and _bbox_intersects(bbox, _geometry_bbox(candidate)):
                    result[grid_id].append({**area, "spatial_status": "unknown_crs_mismatch"})
                continue
            if geometry is not None and geometry_intersects_bbox(geometry, bbox):
                result[grid_id].append({**area, "spatial_status": "intersects"})
            elif source_point_only and geometry_intersects_bbox(candidate, bbox):
                result[grid_id].append({**area, "spatial_status": "source_point_only"})
    return result


def towers_by_cell(
    grid_cells, tower_obstacle_profiles, *, towers=None, tower_clearance_policy=None,
):
    """把**真实塔点**归到它们可能影响到的 grid cell（网格只是加速索引）。

    为什么不能按"派生层状态"封死全域
    --------------------------------
    铁塔是点几何事实。``tower_obstacle_profiles`` 只是**派生**高度层：它没算出来
    只说明"该塔的高度未知"，**不说明**工作区里每一格都有塔。因此这里的判定必须是
    **空间相关**的：

    * profile 已生成 → 用 profile 的经纬度与塔顶派生事实；
    * profile 未生成（``not_calculated``）→ **仍然**用 ``state["towers"]`` 的原始
      点坐标确定相关 cell，并如实标记 ``evidence_source = "source_point_only"``；
    * 与任何塔点都不相关的 cell → 返回空列表，调用方**不得**因为塔派生层的全局状态
      把它判成 unknown；
    * 相关 cell 的塔高度无法解析 → 由调用方如实判 unknown（fail-closed）。

    水平影响范围来自显式配置的 ``tower_clearance_policy.tower_horizontal_clearance_m``；
    未配置时为 0（只影响塔点所在 cell），绝不假设任何裕度。塔坐标本身**从不**被网格
    聚合或取整——网格只回答"哪些 cell 可能受该塔影响"。
    """

    policy = tower_clearance_policy if isinstance(tower_clearance_policy, dict) else {}
    entries = _tower_evidence_entries(
        tower_obstacle_profiles, towers, policy.get("tower_horizontal_clearance_m"),
    )
    result = {str(cell.get("grid_id")): [] for cell in grid_cells or [] if cell.get("grid_id")}
    if not entries:
        return result
    for cell in grid_cells or []:
        grid_id, bbox = str(cell.get("grid_id") or ""), cell.get("bbox")
        if not grid_id or not _bbox(bbox):
            continue
        for entry in entries:
            if _tower_entry_intersects_bbox(entry, bbox):
                result[grid_id].append(deepcopy(entry))
    return result


def _profile_items(tower_obstacle_profiles):
    """``tower_obstacle_profiles`` 的 ``{tower_id: profile}`` 视图（兼容三种既有形态）。"""

    raw = tower_obstacle_profiles
    if isinstance(raw, dict) and isinstance(raw.get("items"), (dict, list, tuple)):
        raw = raw.get("items")
    if isinstance(raw, dict):
        return {str(key): value for key, value in raw.items() if isinstance(value, dict)}
    if isinstance(raw, (list, tuple)):
        return {
            str(item.get("tower_id") or ""): item
            for item in raw if isinstance(item, dict) and item.get("tower_id")
        }
    return {}


def _tower_points(towers):
    """``state["towers"]`` → ``{tower_id: (longitude, latitude)}``（原始点坐标，权威事实）。"""

    raw = towers
    if isinstance(raw, dict):
        raw = raw.get("items") or []
    points = {}
    for tower in raw if isinstance(raw, (list, tuple)) else []:
        if not isinstance(tower, dict):
            continue
        tower_id = str(tower.get("tower_id") or "")
        longitude, latitude = _number(tower.get("longitude")), _number(tower.get("latitude"))
        if tower_id and longitude is not None and latitude is not None:
            points[tower_id] = (longitude, latitude)
    return points


def _tower_evidence_entries(tower_obstacle_profiles, towers, horizontal_clearance_m):
    """塔证据条目：profile 优先，缺失时退回**源点**（并如实标注证据来源）。"""

    profiles = _profile_items(tower_obstacle_profiles)
    points = _tower_points(towers)
    entries, seen = [], set()
    for tower_id, profile in profiles.items():
        longitude, latitude = _number(profile.get("longitude")), _number(profile.get("latitude"))
        if longitude is None or latitude is None:
            point = points.get(tower_id)
            if point is None:
                continue
            longitude, latitude = point
        seen.add(tower_id)
        entries.append(_tower_evidence_entry(
            tower_id, longitude, latitude, profile=profile,
            evidence_source="tower_obstacle_profile",
            horizontal_clearance_m=horizontal_clearance_m,
        ))
    for tower_id, (longitude, latitude) in points.items():
        if tower_id in seen:
            continue
        entries.append(_tower_evidence_entry(
            tower_id, longitude, latitude, profile=None,
            evidence_source="source_point_only",
            horizontal_clearance_m=horizontal_clearance_m,
        ))
    return entries


def _tower_evidence_entry(
    tower_id, longitude, latitude, *, profile, evidence_source, horizontal_clearance_m,
):
    """一个塔证据条目：保留 profile 的派生事实，缺失时**如实**留空（绝不猜高度）。"""

    base = deepcopy(profile) if isinstance(profile, dict) else {}
    base.update({
        "tower_id": tower_id,
        "longitude": float(longitude),
        "latitude": float(latitude),
        # 派生层未生成时，塔顶事实一律为 None：绝不用源 elevation_m 或任何默认值代替。
        "tower_top_orthometric_m": (
            _number(base.get("tower_top_orthometric_m"))
            if evidence_source == "tower_obstacle_profile" else None
        ),
        "confirmed": bool(
            evidence_source == "tower_obstacle_profile" and base.get("confirmed") is True
        ),
        "status": (
            str(base.get("status") or "unresolved")
            if evidence_source == "tower_obstacle_profile" else "not_calculated"
        ),
        "evidence_source": evidence_source,
    })
    half_lat, half_lon = horizontal_half_degrees(horizontal_clearance_m, latitude)
    base["horizontal_clearance_half_degrees"] = [half_lon, half_lat]
    return base


def _tower_entry_intersects_bbox(entry, bbox):
    """塔的水平影响范围（点 + 显式水平净空包络）是否与 grid cell 相交。"""

    half_lon, half_lat = entry.get("horizontal_clearance_half_degrees") or (0.0, 0.0)
    longitude, latitude = float(entry["longitude"]), float(entry["latitude"])
    west, south = longitude - float(half_lon or 0.0), latitude - float(half_lat or 0.0)
    east, north = longitude + float(half_lon or 0.0), latitude + float(half_lat or 0.0)
    return not (
        east < float(bbox[0]) or west > float(bbox[2])
        or north < float(bbox[1]) or south > float(bbox[3])
    )


def _number(value):
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def restricted_area_continuous_evidence(value, *, transform):
    """Project audited protection geometry for an independent route-level validation."""

    collection = normalize_restricted_area_collection(value)
    states = collection.get("domain_states") or {}
    resolved = all(
        (states.get(domain) or {}).get("status") in ("confirmed_none", "confirmed_present")
        for domain in ("airspace", "critical_site")
    )
    items = []
    for area in collection.get("items") or []:
        projected = deepcopy(area)
        geometry = area.get("planning_geometry")
        if geometry is not None:
            projected["planning_geometry_metric"] = _project_geometry(geometry, transform)
        elif area.get("geometry_status") == "source_point_only_no_protection_geometry":
            point = (area.get("geometry") or {}).get("coordinates")
            projected["point_metric"] = transform.to_metric(point) if point else None
        items.append(projected)
    return {
        "status": "passed" if resolved else "unresolved",
        "applicability": "applicable", "areas": items,
        "source": deepcopy(collection.get("source")),
        "domain_states": deepcopy(states),
    }


def _project_geometry(geometry, transform):
    kind = str((geometry or {}).get("type") or "")
    coordinates = (geometry or {}).get("coordinates")

    def ring(value):
        result = []
        for point in value or []:
            projected = transform.to_metric(point)
            if projected is None:
                return None
            result.append([float(projected[0]), float(projected[1])])
        return result

    if kind == "Polygon":
        rings = [ring(item) for item in coordinates or []]
        return None if any(item is None for item in rings) else {"type": kind, "coordinates": rings}
    if kind == "MultiPolygon":
        polygons = [[ring(item) for item in polygon or []] for polygon in coordinates or []]
        if any(item is None for polygon in polygons for item in polygon):
            return None
        return {"type": kind, "coordinates": polygons}
    return None


def geometry_intersects_bbox(geometry, bbox):
    if not isinstance(geometry, dict) or not _bbox(bbox):
        return False
    kind, coordinates = geometry.get("type"), geometry.get("coordinates")
    if kind == "Point":
        return _point(coordinates) and _point_in_bbox(coordinates, bbox)
    if kind == "Polygon":
        return _polygon_intersects_bbox(coordinates, bbox)
    if kind == "MultiPolygon":
        return any(_polygon_intersects_bbox(polygon, bbox) for polygon in coordinates or [])
    return False


def _polygon_intersects_bbox(polygon, bbox):
    outer = polygon[0] if isinstance(polygon, (list, tuple)) and polygon else []
    if not outer or not _bbox_intersects(_ring_bbox(outer), bbox):
        return False
    corners = [
        [bbox[0], bbox[1]], [bbox[2], bbox[1]], [bbox[2], bbox[3]], [bbox[0], bbox[3]],
    ]
    if any(_point_in_ring(corner, outer) for corner in corners):
        return True
    if any(_point_in_bbox(point, bbox) for point in outer if _point(point)):
        return True
    edges = list(zip(outer, outer[1:] + outer[:1]))
    box_edges = list(zip(corners, corners[1:] + corners[:1]))
    return any(_segments_intersect(a, b, c, d) for a, b in edges for c, d in box_edges)


def _geometry_bbox(geometry):
    if not isinstance(geometry, dict):
        return None
    points = []

    def collect(value):
        if _point(value):
            points.append(value)
        elif isinstance(value, (list, tuple)):
            for item in value:
                collect(item)

    collect(geometry.get("coordinates"))
    return _ring_bbox(points) if points else None


def _ring_bbox(ring):
    points = [point for point in ring or [] if _point(point)]
    if not points:
        return None
    return [
        min(float(point[0]) for point in points), min(float(point[1]) for point in points),
        max(float(point[0]) for point in points), max(float(point[1]) for point in points),
    ]


def _point_in_ring(point, ring):
    x, y = float(point[0]), float(point[1])
    inside = False
    points = [item for item in ring or [] if _point(item)]
    if len(points) < 3:
        return False
    previous = points[-1]
    for current in points:
        x1, y1, x2, y2 = float(previous[0]), float(previous[1]), float(current[0]), float(current[1])
        if ((y1 > y) != (y2 > y)) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
        previous = current
    return inside


def _segments_intersect(a, b, c, d):
    def orientation(p, q, r):
        return (float(q[1]) - float(p[1])) * (float(r[0]) - float(q[0])) - (
            float(q[0]) - float(p[0])
        ) * (float(r[1]) - float(q[1]))

    return orientation(a, b, c) * orientation(a, b, d) <= 0 and orientation(c, d, a) * orientation(c, d, b) <= 0


def _point_in_bbox(point, bbox):
    return float(bbox[0]) <= float(point[0]) <= float(bbox[2]) and float(bbox[1]) <= float(point[1]) <= float(bbox[3])


def _point(value):
    try:
        return isinstance(value, (list, tuple)) and len(value) >= 2 and all(
            float(item) == float(item) for item in value[:2]
        )
    except (TypeError, ValueError):
        return False


def _bbox(value):
    return isinstance(value, (list, tuple)) and len(value) == 4


def _bbox_intersects(left, right):
    return bool(left and right) and min(float(left[2]), float(right[2])) >= max(float(left[0]), float(right[0])) and min(float(left[3]), float(right[3])) >= max(float(left[1]), float(right[1]))


__all__ = [
    "geometry_intersects_bbox", "restricted_area_continuous_evidence",
    "restricted_areas_by_cell", "towers_by_cell",
]