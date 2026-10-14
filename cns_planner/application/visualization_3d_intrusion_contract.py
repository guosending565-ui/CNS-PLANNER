"""T3D-3B 可复用的轨迹/保护几何事件判定合同。

几何相交、三维体进入和监视探测是三个独立语义。该模块绝不把前两者推导为
Radar/RID 已探测，也绝不让 historical/stale P17 驱动当前预警。

Round37-T3D-3B 在同一模块上**增量收紧**（不新增第二套几何判定算法）：

* 输入包络有界：节点数、轨迹时长、地理跨度、载荷字节数都有硬上限，
  防止不受限的计算或 URL 请求；
* 起点/终点已经位于保护体内部时，**不伪造**"从外进入/向外离开"的物理穿越
  事件，而是显式区分 ``initially_inside`` / ``still_inside_at_track_end``；
* 只在边界上接触（零维交集）既不能算进入，也不能断言 ``clear``，如实返回
  ``boundary_contact``；
* 轨迹参数必须与保护体实例身份（``render_instance_id`` 等）显式绑定，
  指纹不匹配一律拒绝，绝不对着另一个实例出结论。
"""

from __future__ import annotations

import json
import math

from pyproj import Transformer
from shapely.geometry import LineString, Point, shape
from shapely.ops import nearest_points, transform


class IntrusionContractInputError(ValueError):
    """模拟轨迹或保护几何不满足事件判定合同。"""


#: 模拟轨迹输入包络。全部为**硬上限**：超限直接拒绝，绝不静默截断。
TRACK_INPUT_LIMITS = {
    "min_nodes": 2,
    "max_nodes": 64,
    "max_duration_s": 43200.0,
    "max_geographic_span_deg": 5.0,
    "max_payload_bytes": 8192,
}

#: 两个输入侧接触分类：真正穿过内部 vs 只在边界上相切。
_CONTACT_INTERIOR = "interior"
_CONTACT_BOUNDARY_TOUCH = "boundary_touch"

#: 边界接触判定的米制容差。
#:
#: 保护多边形是在 EPSG:32651 里构造、再反投影到 WGS84 的，因此它的"直边"在经纬度
#: 上并不是测地直线：一个**正好落在地理边界上**的轨迹点，投影后可能落在直边外侧
#: 若干毫米（实测 UTM 51N 上 0.02° 经差的弦高约 1.2 mm）。如果只依赖
#: ``intersection`` 是否为空，这种点会被静默判成 ``clear`` —— 那是 fail-open。
#: 因此额外用一个显式、极小且**在响应中披露**的容差来识别"只在边界上接触"。
BOUNDARY_CONTACT_TOLERANCE_M = 0.25

_INTRUSION_CONTRACT_SCHEMA = "visualization-3d-intrusion-contract/v1"


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


def decode_track_payload(raw):
    """把传输载荷解码成轨迹节点数组；非 JSON / 超长一律拒绝。"""

    text = raw if isinstance(raw, str) else ""
    if not text.strip():
        raise IntrusionContractInputError("track payload is required")
    if len(text.encode("utf-8")) > TRACK_INPUT_LIMITS["max_payload_bytes"]:
        raise IntrusionContractInputError(
            "track payload exceeds the "
            f"{TRACK_INPUT_LIMITS['max_payload_bytes']} byte limit")
    try:
        decoded = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise IntrusionContractInputError("track payload must be valid JSON") from exc
    if not isinstance(decoded, list):
        raise IntrusionContractInputError(
            "track payload must be a JSON array of track nodes")
    return decoded


def _clean_track(nodes):
    if not isinstance(nodes, list) or len(nodes) < TRACK_INPUT_LIMITS["min_nodes"]:
        raise IntrusionContractInputError(
            f"track_nodes require at least {TRACK_INPUT_LIMITS['min_nodes']} nodes")
    if len(nodes) > TRACK_INPUT_LIMITS["max_nodes"]:
        raise IntrusionContractInputError(
            f"track_nodes exceed the {TRACK_INPUT_LIMITS['max_nodes']} node limit")
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
    duration = clean[-1]["relative_time_s"] - clean[0]["relative_time_s"]
    if duration > TRACK_INPUT_LIMITS["max_duration_s"]:
        raise IntrusionContractInputError(
            "track duration exceeds the "
            f"{TRACK_INPUT_LIMITS['max_duration_s']} s limit")
    longitudes = [node["longitude"] for node in clean]
    latitudes = [node["latitude"] for node in clean]
    span = max(max(longitudes) - min(longitudes), max(latitudes) - min(latitudes))
    if span > TRACK_INPUT_LIMITS["max_geographic_span_deg"]:
        raise IntrusionContractInputError(
            "track geographic span exceeds the "
            f"{TRACK_INPUT_LIMITS['max_geographic_span_deg']} degree limit")
    return clean


def _track_bounding_box(nodes):
    longitudes = [node["longitude"] for node in nodes]
    latitudes = [node["latitude"] for node in nodes]
    return {
        "west": min(longitudes),
        "south": min(latitudes),
        "east": max(longitudes),
        "north": max(latitudes),
    }


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


def _point_coordinates(geometry):
    """取出零维交集（Point / MultiPoint / GeometryCollection）里的坐标。"""

    if geometry.is_empty:
        return []
    if geometry.geom_type == "Point":
        return [geometry.coords[0]]
    if geometry.geom_type in {"MultiPoint", "GeometryCollection"}:
        rows = []
        for item in geometry.geoms:
            rows.extend(_point_coordinates(item))
        return rows
    if geometry.geom_type in {"LineString", "LinearRing"}:
        return [geometry.coords[0], geometry.coords[-1]]
    return []


def _side_of(metric_polygon, point):
    """点在保护多边形中的位置；边界既非"内部"也非"外部"。"""

    if metric_polygon.contains(point):
        return "inside"
    if metric_polygon.covers(point):
        return "boundary"
    return "outside"


def _horizontal_intervals(nodes, polygon):
    to_metric = Transformer.from_crs("EPSG:4326", "EPSG:32651", always_xy=True)
    metric_polygon = transform(to_metric.transform, polygon)
    intervals = []
    for index, (start, end) in enumerate(zip(nodes, nodes[1:])):
        first_point = Point(to_metric.transform(start["longitude"], start["latitude"]))
        segment = LineString([
            to_metric.transform(start["longitude"], start["latitude"]),
            to_metric.transform(end["longitude"], end["latitude"]),
        ])
        if segment.length <= 0:
            # 悬停或纯垂直升降时水平段退化成一点，必须按点包含判定；直接跳过会把
            # 正上方的入侵漏报成 clear。落在边界上同样按占据处理（fail-closed）。
            if metric_polygon.covers(first_point):
                contact = (_CONTACT_INTERIOR if metric_polygon.contains(first_point)
                           else _CONTACT_BOUNDARY_TOUCH)
                intervals.append((index, 0.0, 1.0, contact))
            elif metric_polygon.distance(first_point) <= BOUNDARY_CONTACT_TOLERANCE_M:
                intervals.append((index, 0.0, 1.0, _CONTACT_BOUNDARY_TOUCH))
            continue
        overlap = segment.intersection(metric_polygon)
        touched = False
        if not overlap.is_empty:
            for part in _line_parts(overlap):
                first = Point(part.coords[0])
                last = Point(part.coords[-1])
                f0 = max(0.0, min(1.0, segment.project(first) / segment.length))
                f1 = max(0.0, min(1.0, segment.project(last) / segment.length))
                if f1 < f0:
                    f0, f1 = f1, f0
                if f1 - f0 > 1e-12:
                    intervals.append((index, f0, f1, _CONTACT_INTERIOR))
                    touched = True
            if not touched:
                # 交集是零维几何（切点 / 端点相切）：既没有进入内部，也不能断言 clear。
                for coordinate in _point_coordinates(overlap):
                    fraction = max(0.0, min(
                        1.0, segment.project(Point(coordinate)) / segment.length))
                    intervals.append((index, fraction, fraction, _CONTACT_BOUNDARY_TOUCH))
                    touched = True
        if touched:
            continue
        if metric_polygon.distance(segment) <= BOUNDARY_CONTACT_TOLERANCE_M:
            # 投影直边与地理边界之间的毫米级差异会让"正好贴边"的段得不到交集；
            # 这里按最近点补一条零维接触，绝不放行成 clear。
            _on_segment, on_polygon = nearest_points(segment, metric_polygon)
            fraction = max(0.0, min(1.0, segment.project(on_polygon) / segment.length))
            intervals.append((index, fraction, fraction, _CONTACT_BOUNDARY_TOUCH))
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
    """把进入区间整理成 entry/exit 事件，并标注它是否为**真实穿越**。

    起点已在内部时，t=0 的 entry 不是"从外进入"；终点仍在内部时，t=end 的 exit
    也不是"向外离开"。两者都必须如实标注，不能伪造物理穿越。
    """

    last_index = len(nodes) - 2
    merged = []
    for index, start_f, end_f, contact in intervals:
        start = _point_at(nodes, index, start_f)
        end = _point_at(nodes, index, end_f)
        if merged and abs(merged[-1]["end"]["relative_time_s"]
                          - start["relative_time_s"]) <= 1e-7:
            merged[-1]["end"] = end
            merged[-1]["end_index"] = index
            merged[-1]["end_fraction"] = end_f
            if contact == _CONTACT_INTERIOR:
                merged[-1]["contact"] = _CONTACT_INTERIOR
        else:
            merged.append({
                "start": start, "end": end,
                "start_index": index, "start_fraction": start_f,
                "end_index": index, "end_fraction": end_f,
                "contact": contact,
            })

    events = []
    for row in merged:
        if row["contact"] == _CONTACT_BOUNDARY_TOUCH:
            entry_crossing, entry_basis = False, "boundary_touch"
            exit_crossing, exit_basis = False, "boundary_touch"
        else:
            starts_at_track_origin = (
                row["start_index"] == 0 and row["start_fraction"] <= 1e-9)
            ends_at_track_terminus = (
                row["end_index"] == last_index and row["end_fraction"] >= 1.0 - 1e-9)
            entry_crossing = not starts_at_track_origin
            entry_basis = ("crossed_protected_volume_boundary_from_outside" if entry_crossing
                           else "initially_inside")
            exit_crossing = not ends_at_track_terminus
            exit_basis = ("crossed_protected_volume_boundary_from_inside" if exit_crossing
                          else "still_inside_at_track_end")
        events.append({
            "kind": "entry", **row["start"],
            "crossing": entry_crossing, "crossing_basis": entry_basis,
            "contact": row["contact"],
        })
        events.append({
            "kind": "exit", **row["end"],
            "crossing": exit_crossing, "crossing_basis": exit_basis,
            "contact": row["contact"],
        })
    return events


def _contact_summary(intervals):
    if not intervals:
        return "none"
    if all(row[3] == _CONTACT_BOUNDARY_TOUCH for row in intervals):
        return _CONTACT_BOUNDARY_TOUCH
    return _CONTACT_INTERIOR


def _event_summary(events):
    return {
        "entry_count": sum(1 for row in events if row["kind"] == "entry"),
        "exit_count": sum(1 for row in events if row["kind"] == "exit"),
        "true_crossing_count": sum(1 for row in events if row["crossing"]),
        "initially_inside_entries": sum(
            1 for row in events
            if row["kind"] == "entry" and row["crossing_basis"] == "initially_inside"),
        "still_inside_exits": sum(
            1 for row in events
            if row["kind"] == "exit" and row["crossing_basis"] == "still_inside_at_track_end"),
    }


def _binding_mismatch(binding, volume):
    """轨迹参数与保护体实例身份的显式绑定校验。"""

    if not isinstance(binding, dict):
        return None
    expected_instance = binding.get("render_instance_id")
    actual_instance = volume.get("render_instance_id")
    if expected_instance not in (None, "") and str(expected_instance) != str(actual_instance):
        return {
            "field": "render_instance_id",
            "requested": str(expected_instance),
            "actual": actual_instance,
        }
    expected_protection = binding.get("protection_geometry_identity")
    actual_protection = volume.get("protection_geometry_identity")
    if (expected_protection not in (None, "")
            and str(expected_protection) != str(actual_protection)):
        return {
            "field": "protection_geometry_identity",
            "requested": str(expected_protection),
            "actual": actual_protection,
        }
    expected_route = binding.get("route_geometry_fingerprint")
    identity = volume.get("route_geometry_identity")
    actual_route = identity.get("fingerprint") if isinstance(identity, dict) else None
    if expected_route not in (None, "") and str(expected_route) != str(actual_route):
        return {
            "field": "route_geometry_fingerprint",
            "requested": str(expected_route),
            "actual": actual_route,
        }
    return None


def formal_intrusion_contract(state, protection_volumes):
    """披露正式模式就绪性；真实轨迹为空时严格 blocked。"""

    state = state if isinstance(state, dict) else {}
    encounter = state.get("encounter_3d_assessment") or {}
    tracks = _items(encounter.get("tracks"))
    no_tracks = not tracks
    return {
        "schema_version": _INTRUSION_CONTRACT_SCHEMA,
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
                "render_instance_id": row.get("render_instance_id"),
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
                           detection_evidence=None, binding=None):
    """判定用户配置模拟轨迹；结果永不进入正式 P17、监视或运行许可。"""

    if not str(track_id or "").strip():
        raise IntrusionContractInputError("track_id is required")
    nodes = _clean_track(track_nodes)
    volume = protection_volume if isinstance(protection_volume, dict) else {}
    mismatch = _binding_mismatch(binding, volume)
    if mismatch is not None:
        raise IntrusionContractInputError(
            "track is not bound to the requested protection instance: "
            f"{mismatch['field']} requested {mismatch['requested']!r} "
            f"but the resolved instance is {mismatch['actual']!r}")
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

    to_metric = Transformer.from_crs("EPSG:4326", "EPSG:32651", always_xy=True)
    metric_polygon = transform(to_metric.transform, polygon)
    start_side = _side_of(
        metric_polygon, Point(to_metric.transform(
            nodes[0]["longitude"], nodes[0]["latitude"])))
    end_side = _side_of(
        metric_polygon, Point(to_metric.transform(
            nodes[-1]["longitude"], nodes[-1]["latitude"])))
    initially_inside_horizontal = start_side != "outside"
    finally_inside_horizontal = end_side != "outside"

    horizontal_intervals = _horizontal_intervals(nodes, polygon)
    horizontal_events = _events(nodes, horizontal_intervals)
    horizontal_contact = _contact_summary(horizontal_intervals)
    if horizontal_contact == _CONTACT_BOUNDARY_TOUCH:
        horizontal_status = "boundary_contact"
    elif horizontal_events:
        horizontal_status = "intersects"
    else:
        horizontal_status = "clear"
    horizontal = {
        "status": horizontal_status,
        "boundary_contact": horizontal_contact == _CONTACT_BOUNDARY_TOUCH,
        "penetration": horizontal_contact,
        "events": horizontal_events,
        "event_summary": _event_summary(horizontal_events),
    }

    lower = volume.get("lower_altitude_m")
    upper = volume.get("upper_altitude_m")
    vertical_unconfirmed = (
        volume.get("vertical_extent_status") == "vertical_extent_unconfirmed"
        or lower is None or upper is None)
    historical_or_stale = bool(volume.get("historical") or volume.get("stale"))
    initially_inside_volume = None
    finally_inside_volume = None
    if vertical_unconfirmed:
        intrusion = {
            "status": "blocked",
            "reason": "vertical_extent_unconfirmed",
            "events": [],
        }
    elif historical_or_stale:
        # 历史/失效保护几何**永远**不能参与当前预警，也不能被模拟轨迹升级成
        # "当前三维入侵"。如实报告排除原因。
        intrusion = {
            "status": "blocked",
            "reason": "historical_protection_geometry_excluded_from_current_assessment",
            "events": [],
        }
    else:
        lower, upper = float(lower), float(upper)
        start_altitude = nodes[0]["altitude_egm2008_m"]
        end_altitude = nodes[-1]["altitude_egm2008_m"]
        initially_inside_volume = bool(
            initially_inside_horizontal and lower <= start_altitude <= upper)
        finally_inside_volume = bool(
            finally_inside_horizontal and lower <= end_altitude <= upper)
        if horizontal_contact == _CONTACT_BOUNDARY_TOUCH:
            intrusion = {
                "status": "boundary_contact",
                "reason": "boundary_contact_only_not_a_volume_entry",
                "events": [],
            }
        else:
            three_d_intervals = []
            for index, horizontal_start, horizontal_end, contact in horizontal_intervals:
                vertical = _vertical_interval(
                    nodes[index]["altitude_egm2008_m"],
                    nodes[index + 1]["altitude_egm2008_m"], lower, upper)
                if vertical is None:
                    continue
                start = max(horizontal_start, vertical[0])
                end = min(horizontal_end, vertical[1])
                if end - start > 1e-12:
                    three_d_intervals.append((index, start, end, _CONTACT_INTERIOR))
            three_d_events = _events(nodes, three_d_intervals)
            intrusion = {
                "status": "intrudes" if three_d_events else "clear",
                "reason": None if three_d_events else (
                    "track_does_not_enter_confirmed_vertical_interval"),
                "events": three_d_events,
            }

    return {
        "schema_version": _INTRUSION_CONTRACT_SCHEMA,
        "mode": "engineering_scenario_preview",
        "status": "evaluated_simulation",
        "simulated": True,
        "track_id": str(track_id),
        "track_nodes": nodes,
        "render_instance_id": volume.get("render_instance_id"),
        "route_geometry_identity": volume.get("route_geometry_identity"),
        "protection_geometry_identity": volume.get("protection_geometry_identity"),
        "track_geometry": {
            "node_count": len(nodes),
            "duration_s": nodes[-1]["relative_time_s"] - nodes[0]["relative_time_s"],
            "bounding_box": _track_bounding_box(nodes),
            "start_boundary_state": start_side,
            "end_boundary_state": end_side,
            "initially_inside_horizontal": initially_inside_horizontal,
            "finally_inside_horizontal": finally_inside_horizontal,
            "initially_inside_volume": initially_inside_volume,
            "finally_inside_volume": finally_inside_volume,
        },
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
        "input_limits": dict(TRACK_INPUT_LIMITS),
        "boundary_contact_tolerance_m": BOUNDARY_CONTACT_TOLERANCE_M,
    }
