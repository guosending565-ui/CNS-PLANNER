"""Engineering CNS service requirement corridor and voxel-probe assessment V1."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import math
import time

from ..coverage.geometric_3d import (
    GeometricCoverage3DV1,
    build_geometric_providers,
    evaluate_geometry_point,
    index_geometric_providers,
    path_length_m,
    route_profile_height,
)
from ..service_capability.v1 import (
    CNSServiceCapabilityV1,
    SUBSYSTEM_NAMES,
    build_provider_devices,
    evaluate_capability_point,
    prepare_capability_context,
)
from ...domain.cns_corridor import corridor_disclaimers, empty_cns_corridor_assessment
from ...domain.geodesy import distance_m
from ...domain.spatial_3d import (
    effective_route_vertical_context, resolve_egm2008_height, voxel_ref,
)


class CNSServiceCorridorV1:
    algorithm_id = "cns_service_corridor_v1"
    algorithm_version = "1.0"
    model_scope = "engineering_cns_service_requirement_corridor"

    def __init__(self, parameters=None):
        self.parameters = dict(parameters or {})

    @classmethod
    def empty(cls, status="not_calculated"):
        return empty_cns_corridor_assessment(status)

    def evaluate(
        self, routes, spatial_3d, grid, grid_attributes, required_cns,
        aircraft_profile, existing_facilities, device_catalog, corridor_policy,
        *, coverage_parameters=None, capability_parameters=None,
        cancel_check=None, progress_callback=None, preflight=None,
        allow_beyond_validated_envelope=False,
    ):
        """评估服务走廊。

        ``cancel_check`` / ``progress_callback`` 是**可选**的协调钩子，默认 ``None``：
        同步调用路径不传它们，因此数学结果、result schema 与调用序列完全不变。
        长循环（每个 route 与每 ``_CHECKPOINT_CELLS`` 个走廊单元）按约
        ``_CHECKPOINT_INTERVAL_S`` 的节流频率调用一次，避免每内层运算都做 I/O。

        ``preflight`` 是**可选**的规模估算结果（``estimate_corridor_complexity``
        的返回值），默认 ``None`` 时本方法不做任何准入判定、也不额外估算（调用方
        若需要准入，应先自行估算并把结果传进来，避免重复准备 cell）。

        传入 ``preflight`` 时：

        * ``tier == beyond_safety_ceiling`` 且未显式 ``allow_beyond_validated_envelope``
          → 立即抛 :class:`CorridorComplexityBlocked`，**在构造任何结果之前**阻断；
        * 其余情况照常计算，数学与 result schema 完全不变。

        准入判定只决定"当前版本是否承担这份工作量"，不改变任何计算结果。
        """
        if (
            isinstance(preflight, dict)
            and preflight.get("tier") == COMPLEXITY_TIER_CEILING
            and not allow_beyond_validated_envelope
        ):
            raise CorridorComplexityBlocked(preflight)
        cells = list((grid or {}).get("cells") or [])
        terrain = ((grid_attributes or {}).get("terrain") or {}).get("cells") or {}
        layers = list((spatial_3d or {}).get("altitude_layers") or [])
        specs = (corridor_policy or {}).get("routes") or {}
        geometric_providers = build_geometric_providers(
            existing_facilities, device_catalog,
            (spatial_3d or {}).get("site_vertical_profiles") or {},
        )
        geometric_provider_indexes = index_geometric_providers(geometric_providers)
        provider_devices = build_provider_devices(device_catalog, existing_facilities)
        prepared_cells = _prepare_cells(cells, terrain)
        route_results = []
        total_routes = len(routes or [])
        tick = _ProgressThrottle(cancel_check, progress_callback)
        for index, route in enumerate(routes or []):
            route_id = str(route.get("route_id") or "")
            tick.progress(
                (5.0 + 90.0 * index / total_routes) if total_routes else 5.0,
                f"正在计算服务走廊 {route_id}",
            )
            route_results.append(self._route(
                route, effective_route_vertical_context(spatial_3d, route_id),
                specs.get(route_id), prepared_cells,
                layers, required_cns or {}, aircraft_profile or {},
                geometric_provider_indexes, provider_devices, tick,
            ))
        tick.progress(96.0, "服务走廊评估完成")
        fingerprint_input = {
            "routes": routes or [], "spatial_3d": spatial_3d or {}, "grid": grid or {},
            "terrain": terrain, "required_cns": required_cns or {},
            "aircraft_profile": aircraft_profile or {},
            "existing_facilities": existing_facilities or {}, "device_catalog": device_catalog or {},
            "corridor_policy": corridor_policy or {}, "parameters": self.parameters,
            "coverage_parameters": coverage_parameters or {},
            "capability_parameters": capability_parameters or {},
        }
        input_fingerprint = _fingerprint(fingerprint_input)
        geometry_fingerprint = _fingerprint([
            {"route_id": item.get("route_id"), "path": item.get("path")} for item in routes or []
        ] + [corridor_policy or {}, [
            {"grid_id": cell.get("grid_id"), "bbox": cell.get("bbox")} for cell in cells
        ], layers])
        deficit_ids = sorted({
            voxel["voxel_id"] for route in route_results for voxel in route.get("voxels") or []
            if any(item.get("planning_status") == "confirmed_deficit" for item in voxel.get("subsystems") or [])
        })
        unknown_ids = sorted({
            voxel["voxel_id"] for route in route_results for voxel in route.get("voxels") or []
            if any(item.get("planning_status") == "unknown" for item in voxel.get("subsystems") or [])
        })
        statuses = [item["status"] for item in route_results]
        status = _aggregate_status(statuses)
        return {
            "status": status, "algorithm_id": self.algorithm_id,
            "algorithm_version": self.algorithm_version, "model_scope": self.model_scope,
            "parameters": deepcopy(self.parameters), "input_fingerprint": input_fingerprint,
            "corridor_geometry_fingerprint": geometry_fingerprint,
            "route_count": len(route_results), "routes": route_results,
            "deficit_voxel_ids": deficit_ids, "unknown_voxel_ids": unknown_ids,
            "provenance": {
                "coverage_algorithm_id": GeometricCoverage3DV1.algorithm_id,
                "coverage_algorithm_version": GeometricCoverage3DV1.algorithm_version,
                "coverage_parameters": deepcopy(coverage_parameters or {}),
                "capability_algorithm_id": CNSServiceCapabilityV1.algorithm_id,
                "capability_algorithm_version": CNSServiceCapabilityV1.algorithm_version,
                "capability_parameters": deepcopy(capability_parameters or {}),
            },
            "disclaimers": corridor_disclaimers(),
        }

    def _route(
        self, route, profile, spec, cells, layers, required_cns,
        aircraft, geometric_providers, provider_devices, tick=None,
    ):
        route_id, path = str(route.get("route_id") or ""), route.get("path") or []
        metric_route = _MetricRoute(path) if len(path) >= 2 else None
        total = metric_route.total if metric_route else 0.0
        base = {"route_id": route_id, "route_length_m": total, "voxels": [], "subsystems": []}
        if route.get("status") != "passed" or len(path) < 2:
            return {**base, "status": "missing_data", "reasons": ["航路 path 不可用"]}
        if not spec or spec.get("status") != "confirmed" or spec.get("confirmed") is not True:
            return {**base, "status": "pending_confirmation", "reasons": ["CNSCorridorSpec 缺失或未确认"]}
        if not profile or profile.get("status") != "confirmed" or profile.get("confirmed") is not True:
            return {**base, "status": "pending_confirmation", "reasons": ["RouteAltitudeProfile 缺失或未确认"]}
        confirmed_layers = [
            layer for layer in layers
            if layer.get("confirmed") is True and layer.get("status") == "confirmed"
        ]
        if not confirmed_layers:
            return {**base, "status": "pending_confirmation", "reasons": ["没有 confirmed altitude layer"]}
        candidate_layers = _candidate_layers(profile, spec, confirmed_layers)
        horizontal = []
        half_width = float(spec["horizontal_half_width_m"])
        for prepared in cells:
            if not _route_bbox_candidate(metric_route, prepared["center"], half_width + prepared["half_diagonal"]):
                continue
            nearest = metric_route.nearest(prepared["center"])
            if nearest["distance_m"] <= half_width + prepared["half_diagonal"]:
                horizontal.append((prepared, nearest))
        requirements = ((required_cns.get("route_overrides") or {}).get(route_id)
                        or required_cns.get("project_default") or {})
        capability_contexts = {
            code: prepare_capability_context(code, requirements.get(name) or {}, aircraft)
            for code, name in SUBSYSTEM_NAMES.items()
        }
        voxels = []
        total_horizontal = len(horizontal)
        for position, (prepared, nearest) in enumerate(horizontal):
            if position % _CHECKPOINT_CELLS == 0:
                progress = 10.0 + 85.0 * position / total_horizontal if total_horizontal else 10.0
                message = f"正在评估走廊单元 {position}/{total_horizontal}"
                tick.checkpoint(message, progress)
                tick.progress(progress, message)
            voxels.extend(self._cell_voxels(
                route_id, total, profile, spec, prepared, nearest,
                candidate_layers, requirements, aircraft,
                geometric_providers, provider_devices, capability_contexts,
            ))
        summaries = [_summary(code, voxels) for code in ("C", "N", "S")]
        statuses = [item["status"] for item in summaries]
        status = _aggregate_status(statuses)
        corridor_geometry = {
            "route_path": deepcopy(path),
            "included_grid_ids": [str(item[0]["cell"].get("grid_id")) for item in horizontal],
            "horizontal_half_width_m": half_width,
            "horizontal_discretization": "conservative_grid_cell_inclusion_not_exact_buffer",
        }
        return {
            **base, "status": status, "spec": deepcopy(spec), "voxels": voxels,
            "voxel_count": len(voxels), "subsystems": summaries,
            "horizontal_cell_count": len(horizontal),
            "corridor_geometry": corridor_geometry,
            "corridor_geometry_fingerprint": _fingerprint(corridor_geometry),
            "horizontal_discretization": "conservative_grid_cell_inclusion_not_exact_buffer",
            "evaluation_semantics": "representative_voxel_probe_not_entire_voxel_guarantee",
        }

    def _cell_voxels(
        self, route_id, total, profile, spec, prepared, nearest,
        layers, requirements, aircraft, geometric_providers, provider_devices,
        capability_contexts,
    ):
        cell, center = prepared["cell"], prepared["center"]
        surface = prepared["surface"]
        route_height = route_profile_height(profile, nearest["route_offset_m"], total)
        resolved_route = resolve_egm2008_height(
            route_height, profile.get("vertical_reference", "unknown"),
            surface_elevation_m=surface,
            geoid_undulation_m=profile.get("geoid_undulation_m"),
        )
        band = None
        if resolved_route["status"] == "passed":
            altitude = resolved_route["altitude_egm2008_m"]
            band = [
                altitude - float(spec["vertical_lower_margin_m"]),
                altitude + float(spec["vertical_upper_margin_m"]),
            ]
        output = []
        for layer in layers:
            lower, upper = _resolved_layer_bounds(prepared, layer)
            vertical_status = "passed" if band and lower["status"] == upper["status"] == "passed" else (
                "missing_data" if "missing_data" in (resolved_route["status"], lower["status"], upper["status"])
                else "unresolved"
            )
            overlap = None
            if vertical_status == "passed":
                overlap = [max(band[0], lower["altitude_egm2008_m"]), min(band[1], upper["altitude_egm2008_m"])]
                if overlap[1] <= overlap[0]:
                    continue
            ref = voxel_ref(cell.get("grid_id"), layer.get("altitude_layer_id"))
            probe_altitude = sum(overlap) / 2 if overlap else None
            probe = {
                "route_id": route_id, "distance_along_route_m": nearest["route_offset_m"],
                "longitude": center[0], "latitude": center[1], "grid_id": cell.get("grid_id"),
                "surface_elevation_m": surface, "altitude_egm2008_m": probe_altitude,
                "vertical_status": vertical_status,
                "vertical_reason": resolved_route["reason"] if resolved_route["status"] != "passed" else (
                    lower["reason"] if lower["status"] != "passed" else upper["reason"]
                ),
            }
            subsystems = []
            for code, name in SUBSYSTEM_NAMES.items():
                geometry = evaluate_geometry_point(probe, geometric_providers.get(code, []))
                capability_input = {**probe, **geometry}
                capability = evaluate_capability_point(
                    code, capability_input, requirements.get(name) or {}, aircraft,
                    provider_devices, context=capability_contexts[code],
                )
                subsystems.append({
                    "subsystem": code,
                    "planning_status": _planning_status(capability["status"]),
                    "p8_status": capability["status"],
                    "geometry": geometry,
                    "providers": _clone_fresh(geometry.get("providers") or []),
                    "provider_evaluations": _clone_fresh(capability.get("provider_evaluations") or []),
                    "reasons": _clone_fresh(capability.get("reasons") or []),
                    "evidence": _clone_fresh(capability.get("evidence") or []),
                })
            area = _prepared_cell_area(prepared)
            thickness = overlap[1] - overlap[0] if overlap else None
            output.append({
                **ref, "route_id": route_id, "nearest_route_offset_m": nearest["route_offset_m"],
                "nearest_route_distance_m": nearest["distance_m"],
                "nearest_route_coordinate": nearest["coordinate"],
                "cell_half_diagonal_m": prepared["half_diagonal"],
                "corridor_height_egm2008_m": band,
                "layer_height_egm2008_m": [lower["altitude_egm2008_m"], upper["altitude_egm2008_m"]],
                "overlap_height_egm2008_m": overlap, "vertical_status": vertical_status,
                "vertical_reasons": [resolved_route["reason"], lower["reason"], upper["reason"]],
                "probe": probe, "cell_area_proxy_m2": area,
                "vertical_overlap_thickness_m": thickness,
                "discretized_volume_proxy_m3": area * thickness if thickness is not None else None,
                "evaluation_semantics": "representative_voxel_probe_not_entire_voxel_guarantee",
                "subsystems": subsystems,
            })
        return output


class _ProgressThrottle:
    """把 P14 长循环的检查点收敛成"低频、可选"的协调调用。

    设计约束：
    * ``cancel_check`` / ``progress_callback`` 都是**可选**的；两者皆空时本对象
      的每个方法都是纯分支判断，不产生任何 I/O，也不改变数学结果；
    * 调用点固定在"每 ``_CHECKPOINT_CELLS`` 个走廊单元"，避免每内层运算都回调；
    * 取消检查额外叠加 ``_CHECKPOINT_INTERVAL_S`` 时间窗口（它每次都要读 task
      store），进度上报不叠加时间窗口（否则快循环下进度会长时间停在旧值）；
    * 取消经 ``cancel_check`` 的原生异常传播（不在此处吞掉），保证调用方
      （B6X worker）能把它映射成 task ``cancelled`` 且不发布 canonical result。
    """

    __slots__ = ("_cancel_check", "_progress_callback", "_last", "_pending",
                 "_last_progress", "_last_message")

    def __init__(self, cancel_check=None, progress_callback=None):
        self._cancel_check = cancel_check
        self._progress_callback = progress_callback
        self._last = time.monotonic() if cancel_check is not None else 0.0
        self._pending = 0
        self._last_progress = None
        self._last_message = None

    def checkpoint(self, message, progress):
        """取消检查：计数 + 时间双重节流，兼顾"快循环也能取消"与"低 I/O"。

        调用点本身已每 ``_CHECKPOINT_CELLS`` 个单元一次。若只看时间窗口，夹具/
        小规模下整个循环可能在窗口内跑完，取消将完全不生效（实测 192 单元夹具
        DID NOT RAISE）；若只看计数，大规模下每 N 个单元就要读一次 task store。
        因此两者取"或"：累计 N 次调用**或**超过时间窗口，就真正检查一次。
        """

        self._last_progress = progress
        self._last_message = message
        checker = self._cancel_check
        if checker is None:
            return
        self._pending += 1
        now = time.monotonic()
        if self._pending >= _CHECKPOINT_MAX_SKIPS or now - self._last >= _CHECKPOINT_INTERVAL_S:
            self._pending = 0
            self._last = now
            checker()

    def progress(self, progress, message):
        """进度上报：调用方已按"每 N 个单元"确定性节流。

        这里不再叠加时间窗口——时间节流在单元耗时远小于窗口时会让进度长时间
        停在旧值（12k 规模实测只上报 1 次）。调用点最多 ``len(horizontal)/N`` 次，
        与取消检查同量级，因此对 wall time 无可见影响。
        """

        self._last_progress = progress
        self._last_message = message
        callback = self._progress_callback
        if callback is not None:
            callback(progress, message)


#: 长循环检查点粒度：每这么多个走廊单元检查一次取消并上报一次进度。
_CHECKPOINT_CELLS = 16
#: 取消检查的时间节流窗口（秒）：窗口内不重复读 task store。
#: 取值依据：B6X worker 自身心跳间隔为 0.2s，0.3s 的取消响应粒度与之同量级。
_CHECKPOINT_INTERVAL_S = 0.3
#: 取消检查的调用计数上限：即使没到时间窗口，累计这么多次检查点也强制检查一次，
#: 保证"整个循环耗时小于时间窗口"的快速循环里取消依然生效（最多 2×16=32 个
#: 走廊单元的延迟）。正常规模下由时间窗口主导，磁盘读频率不受影响。
_CHECKPOINT_MAX_SKIPS = 2


def nearest_route_position(path, point):
    """Return a deterministic nearest point/offset using local metric projection per segment."""
    return _MetricRoute(path).nearest(point) if len(path) >= 2 else {
        "distance_m": 0.0, "route_offset_m": 0.0,
        "coordinate": list(point), "segment_index": 0,
    }


class _MetricRoute:
    """Route geometry whose invariant segment metrics are computed exactly once."""

    _EARTH_RADIUS_M = 6_371_008.8

    def __init__(self, path):
        self.path = path
        self.segments = []
        cumulative = 0.0
        longitudes, latitudes = [], []
        for index, (left, right) in enumerate(zip(path, path[1:])):
            left_lon, left_lat = float(left[0]), float(left[1])
            right_lon, right_lat = float(right[0]), float(right[1])
            segment_length = distance_m(left, right)
            self.segments.append((
                index, left_lon, left_lat, right_lon, right_lat,
                right_lon - left_lon, right_lat - left_lat,
                segment_length, cumulative,
            ))
            cumulative += segment_length
            longitudes.append(left_lon)
            latitudes.append(left_lat)
        longitudes.append(float(path[-1][0]))
        latitudes.append(float(path[-1][1]))
        self.total = cumulative
        self.bounds = (min(longitudes), min(latitudes), max(longitudes), max(latitudes))

    def nearest(self, point):
        best = None
        point_lon, point_lat = float(point[0]), float(point[1])
        latitude = math.radians(point_lat)
        sx = 111_320.0 * max(math.cos(latitude), 1e-12)
        sy = 110_574.0
        for (index, left_lon, left_lat, right_lon, right_lat, lon_delta,
             lat_delta, segment_length, cumulative) in self.segments:
            ax, ay = (left_lon - point_lon) * sx, (left_lat - point_lat) * sy
            bx, by = (right_lon - point_lon) * sx, (right_lat - point_lat) * sy
            dx, dy = bx - ax, by - ay
            denominator = dx * dx + dy * dy
            t = 0.0 if denominator == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / denominator))
            coordinate = [left_lon + lon_delta * t, left_lat + lat_delta * t]
            candidate = {
                "distance_m": distance_m(point, coordinate),
                "route_offset_m": cumulative + segment_length * t,
                "coordinate": coordinate, "segment_index": index,
            }
            if best is None or (candidate["distance_m"], candidate["route_offset_m"], index) < (best["distance_m"], best["route_offset_m"], best["segment_index"]):
                best = candidate
        return best or {
            "distance_m": 0.0, "route_offset_m": 0.0,
            "coordinate": list(point), "segment_index": 0,
        }


def _prepare_cells(cells, terrain):
    prepared = []
    for cell in cells:
        bbox = cell.get("bbox") or []
        if len(bbox) != 4:
            continue
        center = [
            (float(bbox[0]) + float(bbox[2])) / 2,
            (float(bbox[1]) + float(bbox[3])) / 2,
        ]
        terrain_cell = terrain.get(str(cell.get("grid_id"))) or {}
        prepared.append({
            "cell": cell, "center": center,
            "half_diagonal": distance_m([bbox[0], bbox[1]], [bbox[2], bbox[3]]) / 2,
            "surface": (
                terrain_cell.get("surface_elevation_mean_m")
                if terrain_cell.get("status") == "passed" else None
            ),
            "layer_bounds": {}, "area": None,
        })
    return prepared


def _route_bbox_candidate(route, point, limit_m):
    west, south, east, north = route.bounds
    longitude, latitude = float(point[0]), float(point[1])
    latitude_margin = math.degrees(limit_m / route._EARTH_RADIUS_M)
    if latitude < south - latitude_margin or latitude > north + latitude_margin:
        return False
    minimum_cosine = min(
        math.cos(math.radians((latitude + south) / 2.0)),
        math.cos(math.radians((latitude + north) / 2.0)),
    )
    if minimum_cosine <= 1e-12:
        return True
    longitude_margin = math.degrees(limit_m / (route._EARTH_RADIUS_M * minimum_cosine))
    return west - longitude_margin <= longitude <= east + longitude_margin


def _candidate_layers(profile, spec, layers):
    if profile.get("mode") == "constant":
        heights = [profile.get("constant_altitude_m")]
    else:
        heights = [item.get("altitude_m") for item in profile.get("waypoints") or []]
    resolved_heights = [
        resolve_egm2008_height(
            value, profile.get("vertical_reference", "unknown"),
            surface_elevation_m=None,
            geoid_undulation_m=profile.get("geoid_undulation_m"),
        ) for value in heights
    ]
    if not resolved_heights or any(item["status"] != "passed" for item in resolved_heights):
        return layers
    altitudes = [item["altitude_egm2008_m"] for item in resolved_heights]
    overall_band = [
        min(altitudes) - float(spec["vertical_lower_margin_m"]),
        max(altitudes) + float(spec["vertical_upper_margin_m"]),
    ]
    candidates = []
    for layer in layers:
        lower = resolve_egm2008_height(
            layer.get("lower_altitude_m"), layer.get("vertical_reference", "unknown"),
            surface_elevation_m=None, geoid_undulation_m=layer.get("geoid_undulation_m"),
        )
        upper = resolve_egm2008_height(
            layer.get("upper_altitude_m"), layer.get("vertical_reference", "unknown"),
            surface_elevation_m=None, geoid_undulation_m=layer.get("geoid_undulation_m"),
        )
        if lower["status"] != "passed" or upper["status"] != "passed" or (
            upper["altitude_egm2008_m"] > overall_band[0]
            and lower["altitude_egm2008_m"] < overall_band[1]
        ):
            candidates.append(layer)
    return candidates


def _resolved_layer_bounds(prepared, layer):
    key = id(layer)
    cached = prepared["layer_bounds"].get(key)
    if cached is not None:
        return cached
    surface = prepared["surface"]
    cached = (
        resolve_egm2008_height(
            layer.get("lower_altitude_m"), layer.get("vertical_reference", "unknown"),
            surface_elevation_m=surface, geoid_undulation_m=layer.get("geoid_undulation_m"),
        ),
        resolve_egm2008_height(
            layer.get("upper_altitude_m"), layer.get("vertical_reference", "unknown"),
            surface_elevation_m=surface, geoid_undulation_m=layer.get("geoid_undulation_m"),
        ),
    )
    prepared["layer_bounds"][key] = cached
    return cached


def _prepared_cell_area(prepared):
    if prepared["area"] is None:
        prepared["area"] = cell_area_m2(prepared["cell"].get("bbox"))
    return prepared["area"]


_ATOMIC_TYPES = (type(None), bool, int, float, str, bytes)


def _clone_value(value, memo=None):
    """JSON 结构的深拷贝，语义与 ``copy.deepcopy`` 一致但省掉通用分派开销。

    P14 的每个体素都要把 P7/P8 证据复制进输出（否则输出会别名到
    RequiredCNS / DeviceCatalog 的输入 dict，外部修改会反噬输入）。证据体量
    随 voxel × provider 线性增长，通用 ``deepcopy`` 的 memo、``_keep_alive``
    与类型分派是实测最大单项热点。

    本函数只把 ``dict`` / ``list`` / 原子标量走快速路径，**保留 memo**（因此
    重复引用仍然共享同一个副本，别名结构与 ``deepcopy`` 相同），任何其他类型
    一律回退到 ``copy.deepcopy``，行为不做任何猜测。
    """

    if memo is None:
        memo = {}
    kind = value.__class__
    if kind is dict:
        identifier = id(value)
        cached = memo.get(identifier)
        if cached is not None:
            return cached
        result = {}
        memo[identifier] = result
        for key, item in value.items():
            result[key if key.__class__ is str else _clone_value(key, memo)] = _clone_value(item, memo)
        return result
    if kind is list:
        identifier = id(value)
        cached = memo.get(identifier)
        if cached is not None:
            return cached
        result = []
        memo[identifier] = result
        append = result.append
        for item in value:
            append(_clone_value(item, memo))
        return result
    if kind in _ATOMIC_TYPES:
        return value
    return deepcopy(value, memo)


def _clone_fresh(value):
    """``_clone_value`` 的快速路径：整棵结构都是本次新建的 JSON 容器。

    ``_cell_voxels`` 复制进输出的 ``providers`` / ``provider_evaluations`` /
    ``reasons`` / ``evidence`` 全部是同一轮评估刚构造出来的新对象，树内**不存在
    共享引用**（``_dsh_prof/b9/check_clone_aliasing.py`` 在 12k 规模上实测重复引用
    数为 0）。既然 memo 永远不会命中，就不必为每个节点维护 memo：省掉
    ``id()`` + 两次 ``dict`` 操作，输出字节与 ``_clone_value`` 完全一致。

    只对 ``dict`` / ``list`` / 原子标量走快速路径，其余类型仍回退到
    ``copy.deepcopy``（与 ``_clone_value`` 同一策略）。需要保留别名结构时请继续
    使用带 memo 的 ``_clone_value``。
    """

    kind = value.__class__
    if kind is dict:
        return {
            key if key.__class__ is str else _clone_fresh(key): _clone_fresh(item)
            for key, item in value.items()
        }
    if kind is list:
        return [_clone_fresh(item) for item in value]
    if kind in _ATOMIC_TYPES:
        return value
    return deepcopy(value)


# ---- B9R：operational performance preflight ---------------------------------
#
# 这一节**只做规模估算与准入判定**：它不改变任何数学、不近似任何判定、
# 不写 result schema 的业务字段。估算值一律显式标注为 "estimated"（上界），
# 绝不把经验字节数伪装成精确内存值。

#: 已验证的 operational workload envelope（工程量级，不是空间语义参数）。
#:
#: 代理量是 ``estimated_evaluation_upper_bound``：保守假设"水平位置落在 provider
#: 服务外接方形内的每个探针格 × 每个参与层"都要评估一次。它是**上界**，实测覆盖率
#: 通常只有它的 1/9 ~ 1/18。
#:
#: | 实测配置（B9） | 估算代理 | 实测评估次数 | wall | peak RSS | 逻辑结果 |
#: |---|---|---|---|---|---|
#: | 12k(L7) / 900 provider | 1.6e5 | 1.0e5 | 2.35 s | 282 MB | 108 MB |
#: | 30k(L7) / 9000 provider | 4.3e6 | 2.5e6 | 63.7 s | 5.2 GB | 2.6 GB |
#: | 100k(L7) / 9000 provider | 1.3e7 | — | >600 s | OOM | >4 GB |
#:
#: 阈值按**实测峰值 RSS 反推**，并承认这个代理量对真实覆盖是保守上界：
#: 实测 12k(L7)/900 的代理量 1.6e5 对应真实 1.0e5 次评估、2.35 s、282 MB；
#: 实测 30k(L7)/9000 的代理量 4.3e6 对应真实 2.5e6 次评估、63.7 s、5.2 GB。
#: 即代理量约为真实评估次数的 1.1 ~ 1.7 倍，因此
#: * ``VALIDATED_EVALUATION_LIMIT = 3e6`` —— 约等于"实测 2.5e6 次评估 / 2.6 GB"
#:   的包线（本轮 production-typical / upper-bound 两档的代理量远低于它）；
#: * ``SAFETY_EVALUATION_CEILING = 1e7`` —— 约等于"实测 5.2 GB"的包线，超过即在
#:   构造数 GB 结果之前阻断。
#: 两者都只由评估次数决定，与路线长度、工作区网格总量无关。
VALIDATED_EVALUATION_LIMIT = 3_000_000
SAFETY_EVALUATION_CEILING = 10_000_000

#: 判定分档（供 API / UI 如实展示，不解释为数据错误或安全失败）。
COMPLEXITY_TIER_VALIDATED = "within_validated_envelope"
COMPLEXITY_TIER_BEYOND = "beyond_validated_envelope"
COMPLEXITY_TIER_CEILING = "beyond_safety_ceiling"

COMPLEXITY_MESSAGE_BEYOND = (
    "规模超过当前已验证性能范围，结果可能需要数分钟且占用数 GB 内存；"
    "异步任务可显式继续，同步执行默认拒绝。"
)
COMPLEXITY_MESSAGE_CEILING = (
    "规模超过当前版本可安全承担的硬上限，已在计算前阻断；"
    "请缩小工作区/航路范围或提高资源上限后重试。"
)

#: provider 覆盖半径换算成度时使用的**保守下限**米/度（纬度 1°≈110 574 m）；
#: 取更小的米/度会让换算出的度半径更大，从而高估覆盖、不会低估评估量。
_PROVIDER_COVERAGE_DEGREE_M = 110_000.0


class CorridorComplexityBlocked(RuntimeError):
    """计算前的准入阻断：当前版本无法安全承担这份工作量。

    这不是计算结果失败，也不是数据错误或安全失败——它只表示"规模超出已验证
    性能包线且超过硬安全天花板"。``estimate`` 携带结构化规模事实供 API/UI 如实展示。
    """

    def __init__(self, estimate):
        self.estimate = dict(estimate or {})
        super().__init__(self.estimate.get("message") or COMPLEXITY_MESSAGE_CEILING)


def estimate_corridor_complexity(
    routes, spatial_3d, grid, grid_attributes, corridor_policy,
    existing_facilities, device_catalog,
):
    """纯估算 helper：计算前的规模上界与准入分档。

    返回的每个量都显式命名 ``estimated_*``，并附带 ``estimate_method`` 说明口径。
    它**不**运行 P14 的体素探测与 C/N/S 判定，因此不会产生任何结果、缓存或副作用。

    口径（全部为**上界**，不会低估）：
    * ``estimated_corridor_cells``：与 ``_route`` 完全相同的保守包含条件
      （中心点在航路 bbox 外扩 ``half_width + half_diagonal`` 内）；
    * ``estimated_voxels``：对每个走廊格、每个 confirmed 层调用与 ``_route``
      相同的 ``resolve_egm2008_height`` 判定"垂直带是否与层重叠"；
    * ``estimated_evaluation_upper_bound``：假设每个体素都覆盖到全部 provider，
      即 ``voxels × Σ provider_count``。真实覆盖通常远小于它。
    """

    cells = list((grid or {}).get("cells") or [])
    terrain = ((grid_attributes or {}).get("terrain") or {}).get("cells") or {}
    layers = list((spatial_3d or {}).get("altitude_layers") or [])
    specs = (corridor_policy or {}).get("routes") or {}

    geometric_providers = build_geometric_providers(
        existing_facilities, device_catalog,
        (spatial_3d or {}).get("site_vertical_profiles") or {},
    )
    provider_counts = {
        code: len(geometric_providers.get(code) or []) for code in SUBSYSTEM_NAMES
    }

    prepared_cells = _prepare_cells(cells, terrain)
    confirmed_layers = [
        layer for layer in layers
        if layer.get("confirmed") is True and layer.get("status") == "confirmed"
    ]

    corridor_cells = 0
    voxels = 0
    route_count = 0
    reasons = []
    probe_points = []
    layers_in_play = {}
    for route in routes or []:
        route_id = str(route.get("route_id") or "")
        path = route.get("path") or []
        spec = specs.get(route_id)
        profile = effective_route_vertical_context(spatial_3d, route_id)
        route_count += 1
        if route.get("status") != "passed" or len(path) < 2:
            reasons.append(f"{route_id}: 航路 path 不可用，未计入估算")
            continue
        if not spec or spec.get("status") != "confirmed" or spec.get("confirmed") is not True:
            reasons.append(f"{route_id}: CNSCorridorSpec 缺失或未确认，未计入估算")
            continue
        if not profile or profile.get("status") != "confirmed" or profile.get("confirmed") is not True:
            reasons.append(f"{route_id}: RouteAltitudeProfile 缺失或未确认，未计入估算")
            continue
        metric_route = _MetricRoute(path)
        half_width = float(spec["horizontal_half_width_m"])
        candidate_layers = _candidate_layers(profile, spec, confirmed_layers)
        for layer in candidate_layers:
            layers_in_play[id(layer)] = layer
        route_cells = []
        for prepared in prepared_cells:
            if not _route_bbox_candidate(
                metric_route, prepared["center"], half_width + prepared["half_diagonal"]
            ):
                continue
            nearest = metric_route.nearest(prepared["center"])
            if nearest["distance_m"] <= half_width + prepared["half_diagonal"]:
                route_cells.append((prepared, nearest))
        corridor_cells += len(route_cells)
        total = metric_route.total
        for prepared, nearest in route_cells:
            cell_has_voxel = False
            center_lon, center_lat = prepared["center"]
            route_height = route_profile_height(profile, nearest["route_offset_m"], total)
            resolved_route = resolve_egm2008_height(
                route_height, profile.get("vertical_reference", "unknown"),
                surface_elevation_m=prepared["surface"],
                geoid_undulation_m=profile.get("geoid_undulation_m"),
            )
            if resolved_route["status"] != "passed":
                continue
            altitude = resolved_route["altitude_egm2008_m"]
            band = [
                altitude - float(spec["vertical_lower_margin_m"]),
                altitude + float(spec["vertical_upper_margin_m"]),
            ]
            for layer in candidate_layers:
                lower, upper = _resolved_layer_bounds(prepared, layer)
                if lower["status"] != "passed" or upper["status"] != "passed":
                    continue
                if min(band[1], upper["altitude_egm2008_m"]) > max(band[0], lower["altitude_egm2008_m"]):
                    voxels += 1
                    cell_has_voxel = True
            if cell_has_voxel:
                probe_points.append((center_lon, center_lat))

    provider_total = sum(provider_counts.values())
    # 每个体素会评估"其水平位置落在 provider 服务半径内"的全部 provider；用保守的
    # 外接方形（半边长 = slant_range 换算的度）估算该覆盖集合的上界。
    per_voxel_upper = 0
    for code in SUBSYSTEM_NAMES:
        for provider in geometric_providers.get(code) or []:
            coordinate = provider.get("coordinate")
            if not isinstance(coordinate, (list, tuple)) or len(coordinate) < 2:
                continue
            radius_m = (provider.get("coverage_geometry") or {}).get("slant_range_m")
            if radius_m is None:
                continue
            delta = float(radius_m) / _PROVIDER_COVERAGE_DEGREE_M
            west, east = float(coordinate[0]) - delta, float(coordinate[0]) + delta
            south, north = float(coordinate[1]) - delta, float(coordinate[1]) + delta
            for longitude, latitude in probe_points:
                if west <= longitude <= east and south <= latitude <= north:
                    per_voxel_upper += 1
    evaluation_upper_bound = per_voxel_upper * len(layers_in_play)
    grid_cell_count = len(cells)

    if evaluation_upper_bound > SAFETY_EVALUATION_CEILING:
        tier, blocking, message = COMPLEXITY_TIER_CEILING, True, COMPLEXITY_MESSAGE_CEILING
    elif evaluation_upper_bound > VALIDATED_EVALUATION_LIMIT:
        tier, blocking, message = COMPLEXITY_TIER_BEYOND, False, COMPLEXITY_MESSAGE_BEYOND
    else:
        tier, blocking, message = COMPLEXITY_TIER_VALIDATED, False, ""

    return {
        "status": "estimated",
        "estimate_method": (
            "conservative_upper_bound_without_voxel_probe_or_capability_evaluation"
        ),
        "estimate_basis": (
            "corridor cells use the same conservative inclusion test as evaluation; "
            "evaluations assume every provider whose service square contains a probe "
            "cell centre is evaluated at every layer in play"
        ),
        "is_precise_measurement": False,
        "grid_cell_count": grid_cell_count,
        "confirmed_altitude_layer_count": len(confirmed_layers),
        "candidate_altitude_layer_count": len(layers_in_play),
        "route_count": route_count,
        "estimated_corridor_cells": corridor_cells,
        "estimated_voxels": voxels,
        "estimated_probe_cells": len(probe_points),
        "provider_count_by_subsystem": provider_counts,
        "provider_count_total": provider_total,
        "estimated_evaluation_upper_bound": evaluation_upper_bound,
        "validated_evaluation_limit": VALIDATED_EVALUATION_LIMIT,
        "safety_evaluation_ceiling": SAFETY_EVALUATION_CEILING,
        "tier": tier,
        "blocking": blocking,
        "beyond_validated_envelope": tier != COMPLEXITY_TIER_VALIDATED,
        "message": message,
        "notes": reasons,
    }


def cell_area_m2(bbox):
    if not bbox or len(bbox) != 4:
        return 0.0
    center_lat = (float(bbox[1]) + float(bbox[3])) / 2
    width = distance_m([bbox[0], center_lat], [bbox[2], center_lat])
    height = distance_m([bbox[0], bbox[1]], [bbox[0], bbox[3]])
    return width * height


def _planning_status(p8_status):
    return {
        "meets_under_model": "satisfied",
        "does_not_meet_under_model": "confirmed_deficit",
        "not_applicable": "not_applicable",
        "unknown": "unknown", "unsupported_model": "unknown",
    }.get(p8_status, "unknown")


def _summary(code, voxels):
    classes = ("satisfied", "confirmed_deficit", "unknown")
    selected = [
        (voxel, next(item for item in voxel["subsystems"] if item["subsystem"] == code))
        for voxel in voxels
    ]
    applicable = [(voxel, item) for voxel, item in selected if item["planning_status"] != "not_applicable"]
    if selected and not applicable:
        return {"subsystem": code, "status": "not_applicable", "required_voxel_count": 0,
                **{f"{name}_voxel_count": 0 for name in classes},
                "required_volume_proxy_m3": 0.0}
    counts = {name: sum(item["planning_status"] == name for _, item in applicable) for name in classes}
    volumes = {
        name: sum(float(voxel.get("discretized_volume_proxy_m3") or 0.0) for voxel, item in applicable if item["planning_status"] == name)
        for name in classes
    }
    all_known = all(voxel.get("discretized_volume_proxy_m3") is not None for voxel, _ in applicable)
    required_volume = sum(volumes.values()) if all_known else None
    status = "failed" if counts["confirmed_deficit"] else "pending_confirmation" if counts["unknown"] else "passed" if applicable else "missing_data"
    result = {
        "subsystem": code, "status": status, "required_voxel_count": len(applicable),
        "required_volume_proxy_m3": required_volume,
        "known_required_volume_proxy_m3": sum(volumes.values()),
        "deficit_voxel_ids": [voxel["voxel_id"] for voxel, item in applicable if item["planning_status"] == "confirmed_deficit"],
        "unknown_voxel_ids": [voxel["voxel_id"] for voxel, item in applicable if item["planning_status"] == "unknown"],
    }
    for name in classes:
        result[f"{name}_voxel_count"] = counts[name]
        result[f"{name}_volume_proxy_m3"] = volumes[name]
        result[f"{name}_volume_fraction"] = volumes[name] / required_volume if required_volume not in (None, 0) else None
    return result


def _aggregate_status(statuses):
    if not statuses:
        return "missing_data"
    if any(status == "failed" for status in statuses):
        return "failed"
    if any(status in ("pending_confirmation", "missing_data", "unresolved") for status in statuses):
        return "pending_confirmation"
    if all(status == "not_applicable" for status in statuses):
        return "not_applicable"
    return "passed"


def _fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
