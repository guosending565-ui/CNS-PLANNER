"""T3D-3B 可复用的轨迹/保护几何事件判定合同。

几何相交、三维体进入和监视探测是三个独立语义。该模块绝不把前两者推导为
Radar/RID 已探测，也绝不让 historical/stale P17 驱动当前预警。
"""

from __future__ import annotations

import math

from pyproj import Transformer
from shapely.geometry import LineString, Point, shape
from shapely.ops import transform


class IntrusionContractInputError(ValueError):
    """模拟轨迹或保护几何不满足事件判定合同。"""


def _items(container):
    if isinstance(container, list):
        return container
    if isinstance(container, dict):
        values = container.get("items")
        if isinstance(values, list):
            return values
        if isinstance(values, dict):
            return list(values.values())
    return []


def _clean_track(nodes):
    if not isinstance(nodes, list) or len(nodes) < 2:
        raise IntrusionContractInputError("track_nodes require at least two nodes")
    clean = []
    previous_time = None
    for raw in nodes:
        if not isinstance(raw, dict):
            raise IntrusionContractInputError("each track node must be an object")
        values = {}
        for field in ("longitude", "latitude", "altitude_egm2008_m", "relative_time_s"):
            if raw.get(field) in (None, ""):
                raise IntrusionContractInputError(f"track node {field} is required")
            try:
                values[field] = float(raw[field])
            except (TypeError, ValueError):
                raise IntrusionContractInputError(f"track node {field} must be numeric") from None
            if not math.isfinite(values[field]):
                raise IntrusionContractInputError(f"track node {field} must be finite")
        if not (-180 <= values["longitude"] <= 180 and -90 <= values["latitude"] <= 90):
            raise IntrusionContractInputError("track node is outside WGS84")
        if previous_time is not None and values["relative_time_s"] <= previous_time:
            raise IntrusionContractInputError("track relative_time_s must be strictly increasing")
        previous_time = values["relative_time_s"]
        clean.append(values)
    return clean


def _line_parts(geometry):
    if geometry.is_empty:
        return []
    if geometry.geom_type == "LineString":
        return [geometry]
    if geometry.geom_type in {"MultiLineString", "GeometryCollection"}:
        parts = []
        for item in geometry.geoms:
            parts.extend(_line_parts(item))
        return parts
    return []


def _horizontal_intervals(nodes, polygon):
    to_metric = Transformer.from_crs("EPSG:4326", "EPSG:32651", always_xy=True)
    metric_polygon = transform(to_metric.transform, polygon)
    intervals = []
    for index, (start, end) in enumerate(zip(nodes, nodes[1:])):
        segment = LineString([
            to_metric.transform(start["longitude"], start["latitude"]),
            to_metric.transform(end["longitude"], end["latitude"]),
        ])
        if segment.length <= 0:
            # 悬停或纯垂直升降时水平段退化成一点，必须按点包含判定；直接跳过会把
            # 正上方的入侵漏报成 clear。落在边界上同样按占据处理（fail-closed）。
            if metric_polygon.covers(Point(segment.coords[0])):
                intervals.append((index, 0.0, 1.0))
            continue
        for part in _line_parts(segment.intersection(metric_polygon)):
            first = Point(part.coords[0])
            last = Point(part.coords[-1])
            f0 = max(0.0, min(1.0, segment.project(first) / segment.length))
            f1 = max(0.0, min(1.0, segment.project(last) / segment.length))
            if f1 < f0:
                f0, f1 = f1, f0
            if f1 - f0 > 1e-12:
                intervals.append((index, f0, f1))
    return intervals


def _vertical_interval(start_altitude, end_altitude, lower, upper):
    delta = end_altitude - start_altitude
    if abs(delta) <= 1e-12:
        return (0.0, 1.0) if lower <= start_altitude <= upper else None
    first = (lower - start_altitude) / delta
    second = (upper - start_altitude) / delta
    lo, hi = sorted((first, second))
    lo, hi = max(0.0, lo), min(1.0, hi)
    return (lo, hi) if hi - lo > 1e-12 else None


def _point_at(nodes, index, fraction):
    start, end = nodes[index], nodes[index + 1]
    return {
        "longitude": start["longitude"] + fraction * (
            end["longitude"] - start["longitude"]),
        "latitude": start["latitude"] + fraction * (
            end["latitude"] - start["latitude"]),
        "altitude_egm2008_m": start["altitude_egm2008_m"] + fraction * (
            end["altitude_egm2008_m"] - start["altitude_egm2008_m"]),
        "relative_time_s": start["relative_time_s"] + fraction * (
            end["relative_time_s"] - start["relative_time_s"]),
    }


def _events(nodes, intervals):
    time_intervals = []
    for index, start_f, end_f in intervals:
        start = _point_at(nodes, index, start_f)
        end = _point_at(nodes, index, end_f)
        if time_intervals and abs(time_intervals[-1][1]["relative_time_s"] -
                                  start["relative_time_s"]) <= 1e-7:
            time_intervals[-1] = (time_intervals[-1][0], end)
        else:
            time_intervals.append((start, end))
    result = []
    for start, end in time_intervals:
        result.append({"kind": "entry", **start})
        result.append({"kind": "exit", **end})
    return result


def formal_intrusion_contract(state, protection_volumes):
    """披露正式模式就绪性；真实轨迹为空时严格 blocked。"""

    state = state if isinstance(state, dict) else {}
    encounter = state.get("encounter_3d_assessment") or {}
    tracks = _items(encounter.get("tracks"))
    no_tracks = not tracks
    return {
        "schema_version": "visualization-3d-intrusion-contract/v1",
        "mode": "project_result",
        "status": "blocked" if no_tracks else "evidence_required",
        "reason": "real_intrusion_tracks_empty" if no_tracks else (
            "formal_track_projection_not_executed_in_t3d_3a"),
        "simulated": False,
        "tracks": tracks,
        "events": [],
        "protection_geometry_statuses": [
            {
                "object_id": row.get("object_id"),
                "status": row.get("status"),
                "vertical_extent_status": row.get("vertical_extent_status"),
                "eligible_for_current_warning": False,
            }
            for row in protection_volumes or [] if isinstance(row, dict)
        ],
        "surveillance_detection": {
            "status": "not_evaluated",
            "reason": "real_surveillance_detection_evidence_missing",
            "detection_evidence": None,
        },
        "required_evidence": [
            "encounter_3d_assessment.tracks",
            "current_non_stale_protection_geometry",
            "real_surveillance_detection_evidence",
        ],
        "read_only": True,
        "writes_performed": 0,
    }


def assess_simulated_track(*, track_id, track_nodes, protection_volume,
                           detection_evidence=None):
    """判定用户配置模拟轨迹；结果永不进入正式 P17、监视或运行许可。"""

    if not str(track_id or "").strip():
        raise IntrusionContractInputError("track_id is required")
    nodes = _clean_track(track_nodes)
    volume = protection_volume if isinstance(protection_volume, dict) else {}
    geometry_block = volume.get("geometry") or {}
    raw_geometry = geometry_block.get("geometry")
    if geometry_block.get("status") != "available" or not isinstance(raw_geometry, dict):
        raise IntrusionContractInputError("protection geometry is not available")
    try:
        polygon = shape(raw_geometry)
    except Exception as exc:
        raise IntrusionContractInputError("protection geometry is invalid") from exc
    if polygon.is_empty or polygon.geom_type not in {"Polygon", "MultiPolygon"}:
        raise IntrusionContractInputError("protection geometry must be polygonal")

    horizontal_intervals = _horizontal_intervals(nodes, polygon)
    horizontal_events = _events(nodes, horizontal_intervals)
    horizontal = {
        "status": "intersects" if horizontal_events else "clear",
        "events": horizontal_events,
    }

    lower = volume.get("lower_altitude_m")
    upper = volume.get("upper_altitude_m")
    if (volume.get("vertical_extent_status") == "vertical_extent_unconfirmed"
            or lower is None or upper is None):
        intrusion = {
            "status": "blocked",
            "reason": "vertical_extent_unconfirmed",
            "events": [],
        }
    else:
        lower, upper = float(lower), float(upper)
        three_d_intervals = []
        for index, horizontal_start, horizontal_end in horizontal_intervals:
            vertical = _vertical_interval(
                nodes[index]["altitude_egm2008_m"],
                nodes[index + 1]["altitude_egm2008_m"], lower, upper)
            if vertical is None:
                continue
            start = max(horizontal_start, vertical[0])
            end = min(horizontal_end, vertical[1])
            if end - start > 1e-12:
                three_d_intervals.append((index, start, end))
        three_d_events = _events(nodes, three_d_intervals)
        intrusion = {
            "status": "intrudes" if three_d_events else "clear",
            "reason": None if three_d_events else (
                "track_does_not_enter_confirmed_vertical_interval"),
            "events": three_d_events,
        }

    historical_or_stale = bool(volume.get("historical") or volume.get("stale"))
    return {
        "schema_version": "visualization-3d-intrusion-contract/v1",
        "mode": "engineering_scenario_preview",
        "status": "evaluated_simulation",
        "simulated": True,
        "track_id": str(track_id),
        "track_nodes": nodes,
        "route_geometry_identity": volume.get("route_geometry_identity"),
        "protection_geometry_identity": volume.get("protection_geometry_identity"),
        "protected_volume_geometry_status": {
            "horizontal": geometry_block.get("status"),
            "vertical": volume.get("vertical_extent_status"),
        },
        "horizontal_projection_intersection": horizontal,
        "three_dimensional_volume_intrusion": intrusion,
        "surveillance_detection": {
            "status": "not_evaluated",
            "reason": "real_surveillance_detection_evidence_missing",
            "detection_evidence": detection_evidence,
        },
        "eligible_for_current_warning": False,
        "engineering_assumptions": {
            "track_is_user_configured": True,
            "altitude_reference": "EGM2008",
            "protection_mode": volume.get("mode"),
        },
        "isolation": {
            "historical_p17_excluded_from_current_warning": historical_or_stale,
            "enters_formal_p17": False,
            "enters_cns_statistics": False,
            "enters_radar_or_rid_detection": False,
            "operational_authorization": False,
        },
    }
