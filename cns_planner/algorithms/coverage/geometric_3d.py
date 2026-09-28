"""Geometric-only 3D coverage baseline, independent from CoveragePlannerV1."""

from __future__ import annotations

from hashlib import sha256
import heapq
import json
import math

from ...domain.cns_service_contract import (
    distinct_site_id_for, effective_radius_m, geometry_usable, index_max_range_m,
    normalize_surface_class, resolve_surface_class, service_contract_for,
)
from ...domain.geodesy import distance_m
from ...domain.spatial_3d import effective_route_vertical_context, resolve_egm2008_height


class GeometricCoverage3DV1:
    algorithm_id = "geometric_coverage_3d_v1"
    algorithm_version = "1.0"
    model_scope = "geometric_only"

    def __init__(self, parameters=None):
        parameters = dict(parameters or {})
        spacing = float(parameters.get("sample_spacing_m", 500.0))
        if not math.isfinite(spacing) or spacing <= 0:
            raise ValueError("sample_spacing_m 必须大于零")
        self.parameters = {
            "sample_spacing_m": spacing,
            "assumption": str(parameters.get("assumption") or "engineering_sampling_assumption"),
            "confirmed": bool(parameters.get("confirmed", False)),
        }

    @classmethod
    def empty(cls, status="not_calculated"):
        return {
            "status": status, "algorithm_id": cls.algorithm_id,
            "algorithm_version": cls.algorithm_version, "model_scope": cls.model_scope,
            "parameters": {}, "input_fingerprint": None, "route_count": 0, "routes": [],
            "not_evaluated": _not_evaluated(),
        }

    def evaluate(self, routes, spatial_3d, grid, grid_attributes, existing_facilities, device_catalog,
                 *, surface_class_provider=None, surface_facts_fingerprint=None):
        """评估 P7 几何覆盖。

        ``surface_class_provider`` 是**可选**的最薄注入点（Round 2 由唯一
        LandMask ``classify_surface`` provider 提供）：默认 ``None`` 时每个 sample 的
        ``surface_class`` 先取 terrain cell 的显式事实，否则保持 ``unknown``
        （fail-closed，绝不猜测、绝不回落 land/sea）。

        ``surface_facts_fingerprint`` 是**可序列化**的 surface 事实身份（Round 2）：
        provider 本身是 callable、不能进稳定指纹，因此显式传入事实指纹。它只参与
        ``input_fingerprint``，**不**改变任何几何数学；旧调用方不传时该字段为 ``None``。
        """
        terrain = ((grid_attributes or {}).get("terrain") or {}).get("cells") or {}
        cells = list((grid or {}).get("cells") or [])
        providers = build_geometric_providers(
            existing_facilities, device_catalog,
            (spatial_3d or {}).get("site_vertical_profiles") or {},
        )
        results = []
        for route in routes or []:
            profile = effective_route_vertical_context(spatial_3d, route.get("route_id"))
            results.append(self._route(route, profile, cells, terrain, providers, surface_class_provider))
        fingerprint_input = {
            "routes": routes or [], "spatial_3d": spatial_3d or {},
            "terrain": terrain, "providers": providers, "parameters": self.parameters,
            #: Round 2：surface 事实进入 Coverage 输入指纹（不含逐格明细本身）。
            "surface_facts_fingerprint": surface_facts_fingerprint,
        }
        fingerprint = sha256(json.dumps(fingerprint_input, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        statuses = [item["status"] for item in results]
        status = "missing_data" if not results or any(value in ("missing_data", "unresolved") for value in statuses) else "failed" if any(value == "failed" for value in statuses) else "passed"
        return {
            "status": status, "algorithm_id": self.algorithm_id,
            "algorithm_version": self.algorithm_version, "model_scope": self.model_scope,
            "parameters": dict(self.parameters), "input_fingerprint": fingerprint,
            "route_count": len(results), "routes": results, "not_evaluated": _not_evaluated(),
        }

    def _route(self, route, profile, grid_cells, terrain, providers, surface_class_provider=None):
        route_id, path = str(route.get("route_id") or ""), route.get("path") or []
        if route.get("status") != "passed" or len(path) < 2:
            return {"route_id": route_id, "status": "missing_data", "route_length_m": 0.0, "samples": [], "subsystems": []}
        if not profile:
            return {"route_id": route_id, "status": "missing_data", "route_length_m": path_length_m(path), "samples": [], "subsystems": [], "reasons": ["缺少航路高度剖面"]}
        offsets, total = _sample_offsets(path, self.parameters["sample_spacing_m"])
        samples = [self._sample(route_id, path, offset, total, profile, grid_cells, terrain, surface_class_provider) for offset in offsets]
        subsystems = [self._subsystem(code, route_id, path, offsets, total, profile, grid_cells, terrain, providers.get(code, []), surface_class_provider) for code in ("C", "N", "S")]
        statuses = [item["status"] for item in subsystems]
        status = "missing_data" if any(value == "missing_data" for value in statuses) else "failed" if any(value == "failed" for value in statuses) else "passed"
        return {
            "route_id": route_id, "status": status, "route_length_m": total,
            "altitude_profile": profile, "samples": samples, "subsystems": subsystems,
            "model_scope": self.model_scope,
        }

    def _sample(self, route_id, path, offset, total, profile, grid_cells, terrain, surface_class_provider=None):
        coordinate = route_point_at(path, offset)
        cell = find_grid_cell(grid_cells, coordinate)
        terrain_cell = terrain.get(cell.get("grid_id")) if cell else None
        surface = terrain_cell.get("surface_elevation_mean_m") if terrain_cell and terrain_cell.get("status") == "passed" else None
        input_height = route_profile_height(profile, offset, total)
        resolved = resolve_egm2008_height(
            input_height, profile.get("vertical_reference", "unknown"),
            surface_elevation_m=surface,
            geoid_undulation_m=profile.get("geoid_undulation_m"),
        )
        egm = resolved["altitude_egm2008_m"]
        agl = input_height if profile.get("vertical_reference") == "agl" else egm - surface if egm is not None and surface is not None else None
        return {
            "route_id": route_id, "distance_along_route_m": offset,
            "longitude": coordinate[0], "latitude": coordinate[1],
            "grid_id": cell.get("grid_id") if cell else None,
            "surface_elevation_m": surface, "altitude_agl_m": agl,
            "altitude_egm2008_m": egm, "vertical_status": resolved["status"],
            "vertical_reason": resolved["reason"],
            # Round 1 additive：sample 明确携带 surface_class；缺省 unknown（不猜测）。
            "surface_class": resolve_surface_class(
                explicit=(terrain_cell or {}).get("surface_class"),
                provider=surface_class_provider, coordinate=coordinate,
            ),
        }

    def _subsystem(self, code, route_id, path, offsets, total, profile, grid_cells, terrain, providers, surface_class_provider=None):
        point_samples = [self._sample(route_id, path, value, total, profile, grid_cells, terrain, surface_class_provider) for value in offsets]
        for sample in point_samples:
            sample.update(evaluate_geometry_point(sample, providers))
        if not providers:
            return _missing_subsystem(code, total, point_samples, "没有可用的明确三维几何服务提供者")
        if any(sample["vertical_status"] != "passed" for sample in point_samples):
            return _missing_subsystem(code, total, point_samples, "航路高度无法统一解析为 EGM2008")
        intervals = []
        for start, end in zip(offsets, offsets[1:]):
            midpoint = self._sample(route_id, path, (start + end) / 2, total, profile, grid_cells, terrain, surface_class_provider)
            covered = evaluate_geometry_point(midpoint, providers)
            intervals.append({"start_m": start, "end_m": end, "covered": covered["covered"]})
        covered_length = sum(item["end_m"] - item["start_m"] for item in intervals if item["covered"])
        uncovered = _uncovered_segments(path, intervals)
        return {
            "subsystem": code, "status": "passed" if not uncovered else "failed",
            "route_length_m": total, "covered_length_m": covered_length,
            "uncovered_length_m": total - covered_length,
            "covered_fraction": covered_length / total if total else None,
            "uncovered_fraction": (total - covered_length) / total if total else None,
            "samples": point_samples, "uncovered_segments": uncovered,
            "model_scope": self.model_scope, "not_evaluated": _not_evaluated(),
        }


def build_geometric_providers(collection, catalog, site_profiles=None):
    """Build the canonical P7 point-provider input from existing facilities only.

    Round 1 additive：每个 provider 额外携带

    * ``subsystem`` / ``service_key`` / ``service_contract``（显式服务契约，含
      :func:`service_contract_for` 解析出的 ``service_surface_dependent``）；
    * ``distinct_site_id``（**物理站址身份**，解析顺序见
      :func:`...domain.cns_service_contract.distinct_site_id_for`；无法确认即 ``None``）。

    legacy 设备（无 ``service_key``、无 ``radius_by_surface``）的字段与几何行为不变。
    """
    devices = {str(item.get("device_id")): item for item in (catalog or {}).get("items") or []}
    result = {"C": [], "N": [], "S": []}
    for facility in (collection or {}).get("items") or []:
        if facility.get("status") not in ("active", "passed", None):
            continue
        site_vertical = (site_profiles or {}).get(str(facility.get("site_id") or facility.get("facility_id"))) or facility.get("vertical_profile") or {}
        for installed in facility.get("devices") or []:
            device = devices.get(str(installed.get("device_id"))) or installed
            geometry = device.get("coverage_geometry") or installed.get("coverage_geometry") or {}
            candidates = [installed.get("vertical_profile"), site_vertical, device.get("vertical_profile")]
            vertical = next((item for item in candidates if isinstance(item, dict) and item.get("service_origin_egm2008_m") is not None), {})
            origin = vertical.get("service_origin_egm2008_m") if isinstance(vertical, dict) else None
            subsystem = str(installed.get("subsystem") or device.get("subsystem") or "")
            #: 几何可用性：``sphere``/``hemisphere`` 且声明了 legacy ``slant_range_m``
            #: **或** additive ``radius_by_surface``（两者都没有则不是 provider）。
            if subsystem not in result or not geometry_usable(geometry) or origin is None:
                continue
            identity = {**installed, **device}
            if isinstance(device.get("type"), dict) or isinstance(installed.get("type"), dict):
                identity["type"] = {**(installed.get("type") or {}), **(device.get("type") or {})}
            contract = service_contract_for(subsystem, identity)
            result[subsystem].append({
                "facility_id": facility.get("facility_id"), "device_id": device.get("device_id"),
                "coordinate": facility.get("coordinate"), "service_origin_egm2008_m": float(origin),
                "coverage_geometry": geometry,
                "subsystem": subsystem,
                "service_key": contract["service_key"],
                "service_contract": contract,
                "service_surface_dependent": contract["service_surface_dependent"],
                "distinct_site_id": distinct_site_id_for(facility, identity),
            })
    return result


def evaluate_geometry_point(sample, providers):
    """Evaluate one EGM2008 point with the exact P7 geometric coverage rules.

    Round 1 additive：半径改为按 sample 的 ``surface_class`` 解析
    （:func:`...domain.cns_service_contract.effective_radius_m`）。

    * legacy 设备（只有 ``slant_range_m``）行为完全不变；
    * 有 ``radius_by_surface`` 的设备：``land`` / ``coastal_uncertain`` / ``sea`` 各取
      对应**几何规划半径**；
    * ``surface_class = unknown`` 时半径不可判定 ⇒ ``covered = None``、
      ``coverage_status = "unknown"``（fail-closed，绝不 false-positive）。
    """
    if sample.get("vertical_status") != "passed":
        return {"covered": None, "providers": [], "nearest_slant_distance_m": None,
                "coverage_status": "unknown", "coverage_reason": "vertical_status_not_passed"}
    if isinstance(providers, GeometricProviderIndex):
        return providers.evaluate(sample)
    matches, nearest, unresolved = [], None, False
    point = [sample["longitude"], sample["latitude"]]
    surface_class = normalize_surface_class(sample.get("surface_class"))
    for provider in providers:
        horizontal = distance_m(point, provider["coordinate"])
        delta = sample["altitude_egm2008_m"] - provider["service_origin_egm2008_m"]
        slant = math.hypot(horizontal, delta)
        nearest = slant if nearest is None else min(nearest, slant)
        geometry = provider["coverage_geometry"]
        vertical_ok = geometry["model"] == "sphere" or delta >= 0
        if not vertical_ok:
            continue
        #: 包络（max radius_by_surface）只用于空间剪枝：超出包络的 provider 不可能覆盖，
        #: 也不可能产生 unknown 证据，从而保证 list 与 index 的逐项一致。
        envelope = index_max_range_m(geometry)
        if envelope is None or slant > envelope:
            continue
        resolution = effective_radius_m(geometry, surface_class)
        if resolution["radius_m"] is None:
            unresolved = True
            continue
        if slant <= resolution["radius_m"]:
            matches.append(_match_entry(
                provider, slant=slant, horizontal=horizontal, delta=delta,
                geometry_model=geometry["model"], resolution=resolution,
            ))
    return _coverage_result(matches, nearest, unresolved, surface_class)


def _service_dimensions(provider):
    """service 维度只附加在**显式新服务** provider 上。

    legacy provider（无 ``service_key`` 声明、无 ``radius_by_surface``）保持原有字段集：
    P14 的超大走廊结果里每个 voxel 会重复上千条 provider 证据，因此 legacy 下**不得**
    为了新维度膨胀 payload（否则 beyond-envelope 场景会 OOM）。
    """

    if provider.get("service_surface_dependent") is not True:
        return {}
    return {
        "subsystem": provider.get("subsystem"),
        "service_key": provider.get("service_key"),
        "service_surface_dependent": True,
        "distinct_site_id": provider.get("distinct_site_id"),
    }


def _match_entry(provider, *, slant, horizontal, delta, geometry_model, resolution):
    """匹配证据条目：legacy provider 的字段集与 P14 优化前逐项一致。"""

    entry = {
        "facility_id": provider.get("facility_id"), "device_id": provider.get("device_id"),
        "slant_distance_m": slant, "horizontal_distance_m": horizontal,
        "vertical_delta_m": delta, "geometry_model": geometry_model,
    }
    if resolution["source"] == "radius_by_surface":
        entry.update({
            "coverage_radius_m": resolution["radius_m"],
            "coverage_radius_source": resolution["source"],
            "effective_surface_class": resolution["surface_class"],
        })
    entry.update(_service_dimensions(provider))
    return entry


def _coverage_result(matches, nearest, unresolved, surface_class):
    """list 与 index 共用的结果整形，保证两者逐项一致。"""

    if matches:
        status, covered = "covered", True
    elif unresolved:
        status, covered = "unknown", None
    else:
        status, covered = "not_covered", False
    return {
        "covered": covered, "coverage_status": status,
        "providers": matches, "nearest_slant_distance_m": nearest,
        "surface_class": surface_class,
        "coverage_reason": (
            "surface_class_or_radius_unresolved_fail_closed" if status == "unknown" else None
        ),
    }


class GeometricProviderIndex:
    """Deterministic exact-result spatial index for P7 point providers.

    The tree only prunes a node when a conservative lower bound proves that it
    can contain neither the nearest provider nor a provider whose declared
    slant range reaches the probe.  Leaf evaluations still call the same
    ``distance_m``/``math.hypot`` operations as the list implementation, and
    matches are restored to provider input order before being returned.
    """

    _LEAF_SIZE = 16
    _EARTH_RADIUS_M = 6_371_008.8
    #: 与 ``math.radians`` 位级一致的换算常量，用于在热路径里省掉函数调用。
    _DEGREE_TO_RADIAN = math.pi / 180.0

    def __init__(self, providers):
        self.providers = list(providers or [])
        entries = list(range(len(self.providers)))
        self._root = self._build(entries) if entries else None

    def _build(self, indices):
        west = min(float(self.providers[index]["coordinate"][0]) for index in indices)
        east = max(float(self.providers[index]["coordinate"][0]) for index in indices)
        south = min(float(self.providers[index]["coordinate"][1]) for index in indices)
        north = max(float(self.providers[index]["coordinate"][1]) for index in indices)
        low = min(float(self.providers[index]["service_origin_egm2008_m"]) for index in indices)
        high = max(float(self.providers[index]["service_origin_egm2008_m"]) for index in indices)
        #: 节点包络半径 = max(radius_by_surface.values()) 或 legacy slant_range_m。
        #: 它**只**用于空间剪枝；最终覆盖仍由 effective_radius_m(surface) 判定。
        maximum_range = max(
            (index_max_range_m(self.providers[index]["coverage_geometry"]) or 0.0)
            for index in indices
        )
        # 节点纬度带内最小的 cos(|latitude|)：cos 在 [0°, 180°] 上单调递减，
        # 因此区间最小值落在 |latitude| 最大的端点上。
        node_cosine = max(min(math.cos(math.radians(south)), math.cos(math.radians(north))), 0.0)
        bounds = (west, south, east, north, low, high, maximum_range, node_cosine)
        if len(indices) <= self._LEAF_SIZE:
            return (bounds, tuple(indices), None, None)
        lon_span = east - west
        lat_span = north - south
        if lon_span >= lat_span:
            ordered = sorted(indices, key=lambda index: (float(self.providers[index]["coordinate"][0]), index))
        else:
            ordered = sorted(indices, key=lambda index: (float(self.providers[index]["coordinate"][1]), index))
        middle = len(ordered) // 2
        return (bounds, None, self._build(ordered[:middle]), self._build(ordered[middle:]))

    def _probe_context(self, sample):
        """探针在整次查询里不变的部分：预计算一次，避免每个节点重复求三角。"""

        latitude = float(sample["latitude"])
        return (
            float(sample["longitude"]), latitude, float(sample["altitude_egm2008_m"]),
            max(math.cos(math.radians(latitude)), 0.0),
        )

    def _lower_bound(self, bounds, context):
        """节点 AABB 到探针的保守距离下界（必不大于任何真实 slant 距离）。"""

        west, south, east, north, low, high, _, node_cosine = bounds
        longitude, latitude, altitude, sample_cosine = context
        if longitude < west:
            longitude_delta = west - longitude
        elif longitude > east:
            longitude_delta = longitude - east
        else:
            longitude_delta = 0.0
        if latitude < south:
            latitude_delta = south - latitude
        elif latitude > north:
            latitude_delta = latitude - north
        else:
            latitude_delta = 0.0
        # min(sample, node) 覆盖了 [min(south, latitude), max(north, latitude)] 上
        # 的最小 cos，因此经向换算只会低估、不会高估真实米制距离。
        cosine = sample_cosine if sample_cosine < node_cosine else node_cosine
        dx = (longitude_delta * self._DEGREE_TO_RADIAN) * self._EARTH_RADIUS_M * cosine
        dy = (latitude_delta * self._DEGREE_TO_RADIAN) * self._EARTH_RADIUS_M
        if altitude < low:
            vertical = low - altitude
        elif altitude > high:
            vertical = altitude - high
        else:
            vertical = 0.0
        return math.hypot(math.hypot(dx, dy), vertical)

    def evaluate(self, sample):
        if not self._root:
            return _coverage_result([], None, False, normalize_surface_class(sample.get("surface_class")))
        point = [sample["longitude"], sample["latitude"]]
        altitude = sample["altitude_egm2008_m"]
        surface_class = normalize_surface_class(sample.get("surface_class"))
        context = self._probe_context(sample)
        nearest = None
        unresolved = False
        matches = []
        sequence = 0
        pending = [(self._lower_bound(self._root[0], context), sequence, self._root)]
        while pending:
            lower_bound, _, node = heapq.heappop(pending)
            maximum_range = node[0][6]
            if nearest is not None and lower_bound > nearest and lower_bound > maximum_range:
                continue
            indices, left, right = node[1], node[2], node[3]
            if indices is None:
                for child in (left, right):
                    sequence += 1
                    heapq.heappush(pending, (self._lower_bound(child[0], context), sequence, child))
                continue
            for index in indices:
                provider = self.providers[index]
                horizontal = distance_m(point, provider["coordinate"])
                delta = altitude - provider["service_origin_egm2008_m"]
                slant = math.hypot(horizontal, delta)
                nearest = slant if nearest is None else min(nearest, slant)
                geometry = provider["coverage_geometry"]
                vertical_ok = geometry["model"] == "sphere" or delta >= 0
                if not vertical_ok:
                    continue
                envelope = index_max_range_m(geometry)
                if envelope is None or slant > envelope:
                    continue
                resolution = effective_radius_m(geometry, surface_class)
                if resolution["radius_m"] is None:
                    unresolved = True
                    continue
                if slant <= resolution["radius_m"]:
                    matches.append((index, _match_entry(
                        provider, slant=slant, horizontal=horizontal, delta=delta,
                        geometry_model=geometry["model"], resolution=resolution,
                    )))
        matches.sort(key=lambda item: item[0])
        return _coverage_result(
            [item[1] for item in matches], nearest, unresolved, surface_class,
        )


def index_geometric_providers(providers):
    """Build one reusable exact-result spatial index per C/N/S provider list."""
    return {
        code: GeometricProviderIndex((providers or {}).get(code, []))
        for code in ("C", "N", "S")
    }


def _sample_offsets(path, spacing):
    total = path_length_m(path)
    if total <= 0:
        return [0.0], 0.0
    values = [0.0]
    value = spacing
    while value < total:
        values.append(value)
        value += spacing
    values.append(total)
    return values, total


def path_length_m(path):
    return sum(distance_m(a, b) for a, b in zip(path, path[1:]))


def route_point_at(path, offset):
    remaining = max(0.0, float(offset))
    for a, b in zip(path, path[1:]):
        length = distance_m(a, b)
        if remaining <= length or b is path[-1]:
            ratio = 0.0 if length == 0 else min(1.0, remaining / length)
            return [a[0] + (b[0] - a[0]) * ratio, a[1] + (b[1] - a[1]) * ratio]
        remaining -= length
    return list(path[-1])


def route_profile_height(profile, offset, total):
    if profile.get("mode") == "constant":
        return profile.get("constant_altitude_m")
    points = profile.get("waypoints") or []
    if not points:
        return None
    if offset <= points[0]["distance_along_route_m"]:
        return points[0]["altitude_m"]
    for a, b in zip(points, points[1:]):
        if offset <= b["distance_along_route_m"]:
            span = b["distance_along_route_m"] - a["distance_along_route_m"]
            ratio = 0 if span == 0 else (offset - a["distance_along_route_m"]) / span
            return a["altitude_m"] + (b["altitude_m"] - a["altitude_m"]) * ratio
    return points[-1]["altitude_m"]


def find_grid_cell(cells, point):
    lon, lat = point
    max_east = max((cell.get("bbox") or [0, 0, 0, 0])[2] for cell in cells) if cells else None
    max_north = max((cell.get("bbox") or [0, 0, 0, 0])[3] for cell in cells) if cells else None
    for cell in cells:
        west, south, east, north = cell.get("bbox") or [0, 0, 0, 0]
        in_lon = west <= lon < east or east == max_east and math.isclose(lon, east, abs_tol=1e-12)
        in_lat = south <= lat < north or north == max_north and math.isclose(lat, north, abs_tol=1e-12)
        if in_lon and in_lat:
            return cell
    return None


def _uncovered_segments(path, intervals):
    groups = []
    for item in intervals:
        if item["covered"]:
            continue
        if groups and math.isclose(groups[-1]["route_offset_end_m"], item["start_m"], abs_tol=1e-6):
            groups[-1]["route_offset_end_m"] = item["end_m"]
        else:
            groups.append({"route_offset_start_m": item["start_m"], "route_offset_end_m": item["end_m"]})
    for group in groups:
        group["length_m"] = group["route_offset_end_m"] - group["route_offset_start_m"]
        group["start"] = route_point_at(path, group["route_offset_start_m"])
        group["end"] = route_point_at(path, group["route_offset_end_m"])
        group["path"] = [group["start"], group["end"]]
    return groups


def _missing_subsystem(code, total, samples, reason):
    return {
        "subsystem": code, "status": "missing_data", "route_length_m": total,
        "covered_length_m": None, "uncovered_length_m": None,
        "covered_fraction": None, "uncovered_fraction": None,
        "samples": samples, "uncovered_segments": [], "reasons": [reason],
        "model_scope": "geometric_only", "not_evaluated": _not_evaluated(),
    }


def _not_evaluated():
    return {name: "not_evaluated" for name in (
        "propagation", "line_of_sight", "diffraction", "interference",
        "link_budget", "sensor_detection_probability",
    )}
