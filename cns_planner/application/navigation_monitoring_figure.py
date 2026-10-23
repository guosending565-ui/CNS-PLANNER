"""导航布设图的 N1/N2 导航监测图层构造（Round38-FIG）。

本模块只做一件事：把 **N1/N2 的只读导航监测仿真结果**翻译成 FigureSpec 图层，
并如实标注它的身份与时效性。

严格边界：

* **不重新执行导航设备选址算法**：仿真由组合根注入的既有服务
  （``NavigationMonitoringSimulationService.simulate``）完成，本模块只消费其结果；
* **不写状态**：不 ``save()``、不推进 revision、不触发失效传播；
* **不把中心线关联 passed 转写成全宽 CNS 保障 passed**：
  ``full_width_service_assurance_status`` 由仿真结果原样带出（当前是
  ``not_evaluated``），并在图面披露里明确"中心线关联 ≠ 左右各 500 m 全宽保障"；
* **不固定规划站址数量**：强制端点来自仿真的 ``mandatory_requirements``，
  沿途站址来自仿真真正选中的 ``selected_optional_sites``，未选中候选如实画成背景；
* 结果 ``status != "passed"`` 时不画设施与关联段，只如实登记状态与原因。

几何口径（与仿真一致）：仿真的 ``start_m`` / ``end_m`` 是**沿航路中心线的里程**，
本模块按各线段的**米制累积长度比例**在中心线上取点，从而得到关联段 / 缺口段线几何。
这是制图侧的比例换算，不是业务长度计算。
"""

from __future__ import annotations

import math
from copy import deepcopy

# ==== 图层键（与模板目录 reporting/map_templates 中的常量同值；此处独立持有，
# ==== 避免 GIS / Application 层反向 import reporting 层）=======================
NAVIGATION_MONITOR_SITES_LAYER_KEY = "cns_nav_monitor_sites"
NAVIGATION_MONITOR_SITES_DISPLAY_NAME = "导航监测强制端点设备（N1 仿真）"
NAVIGATION_MONITOR_OPTIONAL_LAYER_KEY = "cns_nav_monitor_optional"
NAVIGATION_MONITOR_OPTIONAL_DISPLAY_NAME = "导航监测选中站址（未确认）"
NAVIGATION_MONITOR_UNSELECTED_LAYER_KEY = "cns_nav_monitor_unselected"
NAVIGATION_MONITOR_UNSELECTED_DISPLAY_NAME = "导航监测候选站址（未选中）"
NAVIGATION_ASSOCIATION_LAYER_KEY = "cns_nav_association"
NAVIGATION_ASSOCIATION_DISPLAY_NAME = "中心线已关联航段 2 km"
NAVIGATION_GAP_LAYER_KEY = "cns_nav_gap"
NAVIGATION_GAP_DISPLAY_NAME = "最长监测缺口（未关联）"

#: 导航监测图层的来源角色：**只读仿真**，不是 ProjectState 容器、不是数据源文件。
NAVIGATION_MONITOR_SOURCE_ROLE = "navigation_monitoring_simulation_readonly"

#: 单条构造结果：``layer_key -> {"display_name", "data", "detail", "feature_count",
#: "status", "reason", "currentness"}``。
LayerPlan = dict


def build_navigation_monitoring_layers(*, simulation, route, candidates, parameters=None):
    """把一次只读仿真结果翻译成导航监测图层计划。

    ``simulation`` 为 ``None`` 时返回空计划 + 明确原因（未提供/仿真失败），
    调用方据此如实登记"未绘制"，**绝不**补造任何站址。
    """

    metrics = _route_metrics(route)
    base_identity = _base_identity(simulation, route, candidates, metrics)
    if not isinstance(simulation, dict) or not simulation:
        return {}, {
            "reason": "navigation_monitoring_simulation_unavailable",
            "identity": base_identity,
            "layers": {},
        }
    status = str(simulation.get("status") or "")
    currentness = _currentness(simulation, metrics)
    plans, notes = {}, {}
    if status != "passed":
        # 仿真未通过（blocked / failed / undetermined）：不画设施、不画关联段。
        return {}, {
            "reason": f"navigation_monitoring_status_{status or 'absent'}",
            "identity": base_identity,
            "currentness": currentness,
            "simulation_status": status,
            "simulation_reason": simulation.get("reason"),
            "layers": {},
        }
    by_id = _candidate_index(candidates)
    mandatory_ids = [str(value) for value in (simulation.get("selected_mandatory_site_ids")
                                              or [])]
    optional_ids = [
        str((item or {}).get("site_id")) for item in (simulation.get("selected_optional_sites")
                                                      or [])
    ]
    mandatory_ids = [value for value in mandatory_ids if value]
    optional_ids = [value for value in optional_ids if value]
    mandatory_points = [_site_point(by_id, site_id, role="mandatory_endpoint")
                        for site_id in mandatory_ids]
    optional_points = [_site_point(by_id, site_id, role="selected_optional")
                       for site_id in optional_ids]
    mandatory_points = [item for item in mandatory_points if item]
    optional_points = [item for item in optional_points if item]
    missing_coordinate_ids = sorted(
        item["site_id"] for item in _site_point_records(by_id, mandatory_ids + optional_ids)
        if item.get("longitude") is None or item.get("latitude") is None
    )
    unselected_ids = [str(value) for value in (simulation.get("unselected_candidate_ids") or [])]
    unselected_points = [
        _site_point(by_id, site_id, role="unselected_candidate")
        for site_id in unselected_ids
    ]
    unselected_points = [item for item in unselected_points if item]

    if mandatory_points:
        plans[NAVIGATION_MONITOR_SITES_LAYER_KEY] = {
            "layer_key": NAVIGATION_MONITOR_SITES_LAYER_KEY,
            "display_name": NAVIGATION_MONITOR_SITES_DISPLAY_NAME,
            "data": {"points": mandatory_points},
            "feature_count": len(mandatory_points),
            "status": "available",
            "reason": "",
            "currentness": currentness,
        }
    if optional_points:
        plans[NAVIGATION_MONITOR_OPTIONAL_LAYER_KEY] = {
            "layer_key": NAVIGATION_MONITOR_OPTIONAL_LAYER_KEY,
            "display_name": NAVIGATION_MONITOR_OPTIONAL_DISPLAY_NAME,
            "data": {"points": optional_points},
            "feature_count": len(optional_points),
            "status": "available",
            "reason": "",
            "currentness": currentness,
        }
    if unselected_points:
        plans[NAVIGATION_MONITOR_UNSELECTED_LAYER_KEY] = {
            "layer_key": NAVIGATION_MONITOR_UNSELECTED_LAYER_KEY,
            "display_name": NAVIGATION_MONITOR_UNSELECTED_DISPLAY_NAME,
            "data": {"points": unselected_points},
            "feature_count": len(unselected_points),
            "status": "available",
            "reason": "",
            "currentness": currentness,
        }
    association_lines = _interval_lines(
        metrics, simulation.get("merged_intervals_m") or [], minimum_length_m=1.0,
    )
    if association_lines:
        plans[NAVIGATION_ASSOCIATION_LAYER_KEY] = {
            "layer_key": NAVIGATION_ASSOCIATION_LAYER_KEY,
            "display_name": NAVIGATION_ASSOCIATION_DISPLAY_NAME,
            "data": {"lines": association_lines},
            "feature_count": len(association_lines),
            "status": "available",
            "reason": "",
            "currentness": currentness,
        }
    longest_gap = _longest_gap(simulation.get("gaps") or [])
    gap_lines = _interval_lines(
        metrics, ([longest_gap] if longest_gap else []), minimum_length_m=1.0,
    )
    if gap_lines:
        plans[NAVIGATION_GAP_LAYER_KEY] = {
            "layer_key": NAVIGATION_GAP_LAYER_KEY,
            "display_name": NAVIGATION_GAP_DISPLAY_NAME,
            "data": {"lines": gap_lines},
            "feature_count": len(gap_lines),
            "status": "available",
            "reason": "",
            "currentness": currentness,
        }
    notes = {
        "reason": None,
        "identity": base_identity,
        "currentness": currentness,
        "simulation_status": status,
        "simulation_reason": simulation.get("reason"),
        "mandatory_site_ids": mandatory_ids,
        "selected_optional_site_ids": optional_ids,
        "unselected_candidate_ids": unselected_ids,
        "missing_coordinate_site_ids": missing_coordinate_ids,
        "association_radius_m": (simulation.get("parameters") or {}).get(
            "monitor_association_radius_m",
        ),
        "full_width_service_assurance_status": simulation.get(
            "full_width_service_assurance_status",
        ),
        "semantic_disclaimer": simulation.get("semantic_disclaimer"),
        "longest_gap_m": simulation.get("longest_gap_m"),
        "longest_gap_duration_s": simulation.get("longest_gap_duration_s"),
        "longest_gap_tolerance_status": (longest_gap or {}).get("tolerance_status"),
        "all_gaps_within_tolerance": simulation.get("all_gaps_within_tolerance"),
        "associated_ratio": simulation.get("associated_ratio"),
        "layers": sorted(plans),
    }
    return plans, notes


def _base_identity(simulation, route, candidates, metrics):
    """图面必须暴露的航路 / 结果身份（缺失即 ``None``，绝不猜）。"""

    simulation = simulation if isinstance(simulation, dict) else {}
    route_identity = simulation.get("route_identity") or {}
    route_authority = route_identity.get("route_authority") or _route_authority(route)
    return {
        "simulation_route_id": route_identity.get("route_id") or (route or {}).get("route_id"),
        "simulation_route_authority": route_authority,
        "simulation_route_geometry_fingerprint": route_identity.get(
            "route_geometry_fingerprint",
        ),
        "figure_route_geometry_fingerprint": metrics.get("route_geometry_fingerprint"),
        "route_operational_status": (route or {}).get("status"),
        "route_point_count": len((route or {}).get("path") or []),
        "candidate_count": len((candidates or {}).get("candidates") or []),
        "result_fingerprint": simulation.get("result_fingerprint"),
        "parameter_fingerprint": simulation.get("parameter_fingerprint"),
        "corridor_width_policy_version": simulation.get("corridor_width_policy_version"),
        "corridor_half_width_m": simulation.get("corridor_half_width_m"),
        "source": "navigation_monitoring_simulation_readonly",
        "recomputes_site_selection": False,
        "writes_state": False,
    }


def _currentness(simulation, metrics):
    """导航监测结果的 currentness（``current`` / ``stale`` / ``unknown`` / ``blocked``）。

    判据（全部来自既有身份字段，不新增第二套几何）：

    1. 仿真未通过（``blocked`` / ``failed``）→ 对应状态；
    2. 航路几何身份必须是**权威** ``route_geometry_fingerprint``（``layeredpathv1-`` 前缀），
       且与图件当前消费的航路几何指纹逐字符一致 → 否则 ``stale``；
    3. 仿真缺少几何身份 → ``unknown``。
    """

    from ..gis.figure_currentness import normalize_status, route_geometry_fingerprint

    simulation = simulation if isinstance(simulation, dict) else {}
    status = str(simulation.get("status") or "")
    if status in ("blocked", "failed", "undetermined"):
        return normalize_status(status)
    declared = str((simulation.get("route_identity") or {}).get(
        "route_geometry_fingerprint") or "").strip()
    if not declared:
        return "unknown"
    rebuilt = route_geometry_fingerprint(metrics.get("path") or [])
    if declared != rebuilt:
        return "stale"
    return "current"


def _route_metrics(route):
    """中心线的米制累积长度表 + 权威几何指纹（纯计算，只读）。"""

    path = []
    for point in (route or {}).get("path") or []:
        if isinstance(point, (list, tuple)) and len(point) >= 2:
            try:
                path.append([float(point[0]), float(point[1])])
            except (TypeError, ValueError):
                continue
    from ..gis.figure_currentness import route_geometry_fingerprint

    if len(path) < 2:
        return {"path": path, "cumulative": [], "length_m": 0.0,
                "route_geometry_fingerprint": route_geometry_fingerprint(path)}
    mean_latitude = sum(item[1] for item in path) / len(path)
    metres_per_degree_lat = 111_320.0
    metres_per_degree_lon = 111_320.0 * math.cos(math.radians(mean_latitude))
    cumulative = [0.0]
    for index in range(1, len(path)):
        delta_lon = (path[index][0] - path[index - 1][0]) * metres_per_degree_lon
        delta_lat = (path[index][1] - path[index - 1][1]) * metres_per_degree_lat
        cumulative.append(cumulative[-1] + math.hypot(delta_lon, delta_lat))
    return {
        "path": path,
        "cumulative": cumulative,
        "length_m": cumulative[-1],
        "metres_per_degree_lon": metres_per_degree_lon,
        "metres_per_degree_lat": metres_per_degree_lat,
        "route_geometry_fingerprint": route_geometry_fingerprint(path),
    }


def _interval_lines(metrics, intervals, *, minimum_length_m=1.0):
    """按里程区间在中心线上取点，得到关联 / 缺口段的线几何。"""

    cumulative = metrics.get("cumulative") or []
    if len(cumulative) < 2:
        return []
    total = float(cumulative[-1])
    lines = []
    for interval in intervals or []:
        bounds = _interval_bounds(interval)
        if bounds is None:
            continue
        start, end = max(0.0, bounds[0]), min(total, bounds[1])
        if end - start < minimum_length_m:
            continue
        points = [_point_at(metrics, start)]
        for index, value in enumerate(cumulative):
            if start < value < end:
                points.append(list(metrics["path"][index]))
        points.append(_point_at(metrics, end))
        deduplicated = []
        for point in points:
            if not deduplicated or deduplicated[-1] != point:
                deduplicated.append(point)
        if len(deduplicated) >= 2:
            lines.append(deduplicated)
    return lines


def _point_at(metrics, distance_m):
    """里程距离 → 中心线上的坐标（线性插值；比例换算只用于绘图）。"""

    cumulative = metrics["cumulative"]
    path = metrics["path"]
    total = cumulative[-1]
    value = max(0.0, min(float(distance_m), total))
    for index in range(1, len(cumulative)):
        if value <= cumulative[index] or index == len(cumulative) - 1:
            span = cumulative[index] - cumulative[index - 1]
            ratio = 0.0 if span <= 0 else (value - cumulative[index - 1]) / span
            start, end = path[index - 1], path[index]
            return [
                start[0] + (end[0] - start[0]) * ratio,
                start[1] + (end[1] - start[1]) * ratio,
            ]
    return list(path[-1])


def _interval_bounds(interval):
    """区间 → ``(start_m, end_m)``。

    仿真的 ``merged_intervals_m`` 是 ``[[start, end], ...]``，而 ``gaps`` 是
    ``[{"start_m": ..., "end_m": ...}, ...]``：两种形态都必须被如实解析，
    **绝不**因为形态不同就把缺口静默丢掉（丢了缺口等于把"未关联"画成了"已关联"）。
    """

    if isinstance(interval, dict):
        start, end = interval.get("start_m"), interval.get("end_m")
    elif isinstance(interval, (list, tuple)) and len(interval) >= 2:
        start, end = interval[0], interval[1]
    else:
        return None
    try:
        return float(start), float(end)
    except (TypeError, ValueError):
        return None


def _longest_gap(gaps):
    """最长监测缺口（按 ``length_m`` 取最大；缺失即 ``None``）。"""

    best, best_length = None, -1.0
    for item in gaps or []:
        if not isinstance(item, dict):
            continue
        try:
            length = float(item.get("length_m"))
        except (TypeError, ValueError):
            continue
        if length > best_length:
            best, best_length = deepcopy(item), length
    return best


def _candidate_index(candidates):
    result = {}
    for item in (candidates or {}).get("candidates") or []:
        if isinstance(item, dict) and item.get("candidate_id"):
            result[str(item["candidate_id"])] = item
    return result


def _site_point_records(by_id, site_ids):
    return [_candidate_record(by_id, site_id) for site_id in site_ids]


def _candidate_record(by_id, site_id):
    item = by_id.get(str(site_id)) or {}
    coordinate = item.get("coordinate")
    if isinstance(coordinate, dict):
        longitude = _finite(coordinate.get("longitude"))
        latitude = _finite(coordinate.get("latitude"))
    elif isinstance(coordinate, (list, tuple)) and len(coordinate) >= 2:
        # CnsSharedSiteCandidateService 的 canonical 坐标契约是 [lon, lat]。
        # 旧制图夹具曾使用 {longitude, latitude}；两种只读表示都接受，但绝不换序猜测。
        longitude, latitude = _finite(coordinate[0]), _finite(coordinate[1])
    else:
        longitude = latitude = None
    if longitude is None or latitude is None:
        pair = item.get("coordinate_wgs84") or item.get("coordinate_crs84")
        if isinstance(pair, (list, tuple)) and len(pair) >= 2:
            longitude, latitude = _finite(pair[0]), _finite(pair[1])
    if longitude is None or latitude is None:
        pair = item.get("metric_coordinate")
        if isinstance(pair, (list, tuple)) and len(pair) >= 2:
            longitude, latitude = _finite(pair[0]), _finite(pair[1])
    return {
        "site_id": str(site_id),
        "candidate_id": str(item.get("candidate_id") or site_id),
        "candidate_kind": item.get("candidate_kind"),
        "spatial_use_status": item.get("spatial_use_status"),
        "longitude": longitude,
        "latitude": latitude,
    }


def _site_point(by_id, site_id, *, role):
    record = _candidate_record(by_id, site_id)
    if record["longitude"] is None or record["latitude"] is None:
        return None
    return {
        "longitude": record["longitude"],
        "latitude": record["latitude"],
        "site_id": record["site_id"],
        "candidate_id": record["candidate_id"],
        "candidate_kind": record["candidate_kind"],
        "role": role,
        "planned_not_installed": role != "unselected_candidate",
        "confirmed_installed": False,
    }


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _route_authority(route):
    authority = (route or {}).get("authority")
    if isinstance(authority, dict):
        return deepcopy(authority)
    provenance = (route or {}).get("provenance") or {}
    if isinstance(provenance, dict):
        summary = {
            key: provenance.get(key) for key in (
                "source_type", "adoption_fingerprint", "candidate_fingerprint",
                "validation_fingerprint",
            )
        }
        return summary if any(value is not None for value in summary.values()) else None
    return None


__all__ = [
    "NAVIGATION_ASSOCIATION_DISPLAY_NAME", "NAVIGATION_ASSOCIATION_LAYER_KEY",
    "NAVIGATION_GAP_DISPLAY_NAME", "NAVIGATION_GAP_LAYER_KEY",
    "NAVIGATION_MONITOR_OPTIONAL_DISPLAY_NAME", "NAVIGATION_MONITOR_OPTIONAL_LAYER_KEY",
    "NAVIGATION_MONITOR_SITES_DISPLAY_NAME", "NAVIGATION_MONITOR_SITES_LAYER_KEY",
    "NAVIGATION_MONITOR_SOURCE_ROLE", "NAVIGATION_MONITOR_UNSELECTED_DISPLAY_NAME",
    "NAVIGATION_MONITOR_UNSELECTED_LAYER_KEY", "build_navigation_monitoring_layers",
]
