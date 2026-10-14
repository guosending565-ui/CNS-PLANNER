"""Pure metric geometry for read-only navigation-monitoring simulations."""

from hashlib import sha256
from math import hypot, isfinite, sqrt
import json


_DEFAULTS = {
    "monitor_association_radius_m": 2000.0,
    "gap_tolerance_s": 10.0,
    "route_speed_mps": 15.0,
    "risk_priority_weight": 3.0,
    "max_optional_monitor_sites": 4,
    "force_endpoint_monitors": True,
}
_POSITIVE_PARAMETERS = (
    "monitor_association_radius_m", "gap_tolerance_s", "route_speed_mps",
    "risk_priority_weight",
)


def validate_navigation_monitoring_parameters(payload):
    payload = {} if payload is None else payload
    if not isinstance(payload, dict):
        raise ValueError("导航监测参数必须是对象")
    unknown = set(payload) - set(_DEFAULTS)
    if unknown:
        raise ValueError(f"导航监测参数未知：{'', ''.join(sorted(unknown))}")
    result = {}
    sources = {}
    for key in _POSITIVE_PARAMETERS:
        value = payload[key] if key in payload else _DEFAULTS[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value <= 0:
            raise ValueError(f"导航监测参数 {key} 必须是有限正数")
        result[key] = float(value)
        sources[key] = "request" if key in payload else "default"
    limit = payload.get("max_optional_monitor_sites", _DEFAULTS["max_optional_monitor_sites"])
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("导航监测参数 max_optional_monitor_sites 必须是非负整数")
    result["max_optional_monitor_sites"] = limit
    sources["max_optional_monitor_sites"] = "request" if "max_optional_monitor_sites" in payload else "default"
    if "force_endpoint_monitors" in payload and payload["force_endpoint_monitors"] is not True:
        raise ValueError("强制端点导航监测设备不可关闭")
    result["force_endpoint_monitors"] = True
    sources["force_endpoint_monitors"] = "fixed_constraint"
    result["parameter_sources"] = sources
    return result


def _fingerprint(value):
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return "navmon-" + sha256(body.encode("utf-8")).hexdigest()


def _metric_route(route):
    if not isinstance(route, (list, tuple)) or len(route) < 2:
        raise ValueError("航路至少需要两个米制坐标点")
    points = []
    for point in route:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ValueError("航路米制坐标无效")
        x, y = float(point[0]), float(point[1])
        if not (isfinite(x) and isfinite(y)):
            raise ValueError("航路米制坐标无效")
        points.append((x, y))
    segments, offset = [], 0.0
    for start, end in zip(points, points[1:]):
        length = hypot(end[0] - start[0], end[1] - start[1])
        if length <= 0:
            continue
        segments.append((start, end, offset, length))
        offset += length
    if offset <= 0:
        raise ValueError("航路长度必须大于零")
    return points, segments, offset


def _merge(intervals, lower=0.0, upper=None):
    clipped = []
    for start, end in intervals or []:
        start, end = float(start), float(end)
        if upper is not None:
            start, end = max(lower, start), min(upper, end)
        if end + 1e-9 < start:
            continue
        clipped.append([start, end])
    clipped.sort()
    merged = []
    for start, end in clipped:
        if not merged or start > merged[-1][1] + 1e-9:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return merged


def _circle_intervals(segments, center, radius, route_length):
    cx, cy = float(center[0]), float(center[1])
    intervals = []
    for (x1, y1), (x2, y2), offset, length in segments:
        dx, dy = x2 - x1, y2 - y1
        fx, fy = x1 - cx, y1 - cy
        a = dx * dx + dy * dy
        b = 2 * (fx * dx + fy * dy)
        c = fx * fx + fy * fy - radius * radius
        discriminant = b * b - 4 * a * c
        if discriminant < -1e-9:
            continue
        root = sqrt(max(0.0, discriminant))
        low = max(0.0, (-b - root) / (2 * a))
        high = min(1.0, (-b + root) / (2 * a))
        if low <= high + 1e-9:
            intervals.append([offset + low * length, offset + high * length])
    return _merge(intervals, 0.0, route_length)


def _measure(intervals):
    return sum(max(0.0, end - start) for start, end in intervals)


def _intersection(left, right):
    output = []
    for a, b in left:
        for c, d in right:
            start, end = max(a, c), min(b, d)
            if end > start + 1e-9:
                output.append([start, end])
    return _merge(output)


def _complement(intervals, length):
    gaps, cursor = [], 0.0
    for start, end in _merge(intervals, 0.0, length):
        if start > cursor + 1e-9:
            gaps.append([cursor, start])
        cursor = max(cursor, end)
    if cursor < length - 1e-9:
        gaps.append([cursor, length])
    return gaps


def _gap_duration(start, end, segment_speeds, fallback_speed):
    if segment_speeds is None:
        return (end - start) / fallback_speed, "route_speed_assumption"
    pieces = []
    for item in segment_speeds:
        left, right = float(item.get("start_m", 0)), float(item.get("end_m", 0))
        overlap_start, overlap_end = max(start, left), min(end, right)
        if overlap_end <= overlap_start + 1e-9:
            continue
        speed = item.get("speed_mps")
        if (item.get("authority") != "reliable" or isinstance(speed, bool)
                or not isinstance(speed, (int, float)) or not isfinite(speed) or speed <= 0):
            return None, "segment_speed_evidence_unreliable"
        pieces.append((overlap_start, overlap_end, float(speed)))
    pieces.sort()
    cursor, duration = start, 0.0
    for left, right, speed in pieces:
        if left > cursor + 1e-9:
            return None, "segment_speed_evidence_incomplete"
        effective_start = max(cursor, left)
        if right > effective_start:
            duration += (right - effective_start) / speed
            cursor = max(cursor, right)
        if cursor >= end - 1e-9:
            break
    if cursor < end - 1e-9:
        return None, "segment_speed_evidence_incomplete"
    return duration, "reliable_segment_speeds"


def _gap_records(intervals, route_length, parameters, segment_speeds):
    records = []
    for start, end in _complement(intervals, route_length):
        duration, speed_source = _gap_duration(
            start, end, segment_speeds, parameters["route_speed_mps"],
        )
        if duration is None:
            tolerance = "undetermined"
        elif duration <= parameters["gap_tolerance_s"] + 1e-9:
            tolerance = "satisfied"
        else:
            tolerance = "exceeded"
        records.append({
            "start_m": start, "end_m": end, "length_m": end - start,
            "duration_s": duration, "speed_source": speed_source,
            "gap_tolerance_s": parameters["gap_tolerance_s"],
            "tolerance_status": tolerance,
        })
    return records


def _overall_status(gaps):
    if any(item["tolerance_status"] == "exceeded" for item in gaps):
        return "failed"
    if any(item["tolerance_status"] == "undetermined" for item in gaps):
        return "undetermined"
    return "passed"


def _device_for(site_id, reusable_devices, used_device_ids):
    for item in reusable_devices or []:
        device_id = str(item.get("device_id") or "")
        if (item.get("service_key") == "N" and item.get("site_id") == site_id
                and item.get("status") in {"installed", "active", "passed"}
                and device_id and device_id not in used_device_ids):
            used_device_ids.add(device_id)
            return {"device_id": device_id, "site_id": site_id, "service_key": "N", "provisioning": "reused_existing"}
    device_id = f"simulation:N:{site_id}"
    used_device_ids.add(device_id)
    return {"device_id": device_id, "site_id": site_id, "service_key": "N", "provisioning": "simulation_planned"}


def simulate_navigation_monitoring(*, route, candidates, endpoint_site_ids, parameters,
                                   risk_intervals=None, segment_speeds=None,
                                   reusable_devices=None):
    normalized = validate_navigation_monitoring_parameters(parameters)
    points, segments, route_length = _metric_route(route)
    by_id = {str(item.get("candidate_id")): item for item in candidates or [] if item.get("candidate_id")}
    endpoint_ids = [str(value) for value in endpoint_site_ids or []]
    if len(endpoint_ids) != 2:
        raise ValueError("必须提供起点和终点两个稳定站址身份")
    mandatory_requirements = []
    for position, site_id in zip(("start", "end"), endpoint_ids):
        item = by_id.get(site_id)
        usable = bool(item and item.get("spatial_use_status") == "eligible" and item.get("coordinate"))
        mandatory_requirements.append({
            "position": position, "site_id": site_id,
            "status": "satisfied" if usable else "blocked",
            "reason": None if usable else "spatial_evidence_unavailable",
        })
    base_contract = {
        "corridor_width_policy_version": "fixed-1000m-v1",
        "corridor_half_width_m": 500,
        "full_width_service_assurance_status": "not_evaluated",
        "semantic_disclaimer": "中心线关联仿真，不代表左右各500米全宽CNS保障能力",
        "parameters": normalized,
        "parameter_fingerprint": _fingerprint(normalized),
        "route_length_m": route_length,
        "mandatory_requirements": mandatory_requirements,
        "writes_performed": 0,
    }
    if any(item["status"] == "blocked" for item in mandatory_requirements):
        result = dict(base_contract, status="blocked", reason="mandatory_endpoint_spatial_evidence_unavailable")
        result["result_fingerprint"] = _fingerprint(result)
        return result

    radius = normalized["monitor_association_radius_m"]
    candidate_intervals = {
        site_id: _circle_intervals(segments, item["coordinate"], radius, route_length)
        for site_id, item in by_id.items()
        if item.get("spatial_use_status") == "eligible" and item.get("coordinate")
    }
    selected_ids = list(dict.fromkeys(endpoint_ids))
    current = _merge([interval for site_id in selected_ids for interval in candidate_intervals.get(site_id, [])])
    risks = _merge(risk_intervals or [], 0.0, route_length)
    optional = [site_id for site_id in by_id if site_id not in selected_ids and site_id in candidate_intervals]
    optional.sort()
    selected_optional = []
    gains = {}
    while len(selected_optional) < normalized["max_optional_monitor_sites"]:
        choices = []
        current_length = _measure(current)
        current_risk = _measure(_intersection(current, risks))
        for site_id in optional:
            if site_id in selected_optional:
                continue
            combined = _merge(current + candidate_intervals[site_id], 0.0, route_length)
            new_length = _measure(combined) - current_length
            new_risk = _measure(_intersection(combined, risks)) - current_risk
            score = new_length + (normalized["risk_priority_weight"] - 1.0) * new_risk
            landing_priority = 0 if by_id[site_id].get("candidate_kind") in {"project_node", "reference_landing_site"} else 1
            choices.append((-score, landing_priority, site_id, score, combined))
        if not choices:
            break
        choices.sort(key=lambda value: (value[0], value[1], value[2]))
        _, _, site_id, score, combined = choices[0]
        if score <= 1e-9:
            break
        selected_optional.append(site_id)
        gains[site_id] = score
        current = combined

    unselected = sorted(set(optional) - set(selected_optional))
    if len(selected_optional) >= normalized["max_optional_monitor_sites"] and unselected:
        stop_reason = "optional_site_limit_reached"
    elif unselected:
        stop_reason = "no_positive_incremental_association"
    else:
        stop_reason = "candidate_pool_exhausted"

    pruned = []
    final_status = _overall_status(_gap_records(current, route_length, normalized, segment_speeds))
    final_risk = _measure(_intersection(current, risks))
    for site_id in list(reversed(selected_optional)):
        trial_ids = selected_ids + [value for value in selected_optional if value != site_id]
        trial = _merge([interval for value in trial_ids for interval in candidate_intervals[value]], 0.0, route_length)
        if (_overall_status(_gap_records(trial, route_length, normalized, segment_speeds)) == final_status
                and abs(_measure(_intersection(trial, risks)) - final_risk) <= 1e-9):
            selected_optional.remove(site_id)
            pruned.append(site_id)
            current = trial

    gaps = _gap_records(current, route_length, normalized, segment_speeds)
    status = _overall_status(gaps)
    used_devices = set()
    devices = [_device_for(site_id, reusable_devices, used_devices) for site_id in selected_ids + selected_optional]
    result = dict(base_contract)
    result.update({
        "status": status,
        "reason": None if status == "passed" else "continuous_gap_tolerance_not_satisfied",
        "selected_mandatory_site_ids": selected_ids,
        "selected_optional_sites": [{"site_id": site_id, "gain_score": gains[site_id]} for site_id in selected_optional],
        "pruned_optional_site_ids": sorted(pruned),
        "unselected_candidate_ids": sorted(set(unselected) | set(pruned)),
        "selection_stop_reason": stop_reason,
        "devices": devices,
        "merged_intervals_m": current,
        "associated_length_m": _measure(current),
        "associated_ratio": _measure(current) / route_length,
        "risk_associated_length_m": _measure(_intersection(current, risks)),
        "gaps": gaps,
        "longest_gap_m": max((item["length_m"] for item in gaps), default=0.0),
        "longest_gap_duration_s": max((item["duration_s"] for item in gaps if item["duration_s"] is not None), default=0.0),
        "all_gaps_within_tolerance": status == "passed",
    })
    result["result_fingerprint"] = _fingerprint({
        "route": points, "candidates": [(key, by_id[key].get("coordinate")) for key in sorted(by_id)],
        "parameters": normalized, "risk_intervals": risks, "segment_speeds": segment_speeds,
        "selected_sites": selected_ids + selected_optional, "gaps": gaps,
    })
    return result
