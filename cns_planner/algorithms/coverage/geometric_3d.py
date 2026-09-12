"""Geometric-only 3D coverage baseline, independent from CoveragePlannerV1."""

from __future__ import annotations

from hashlib import sha256
import json
import math

from .v1 import distance_m
from ...domain.spatial_3d import resolve_egm2008_height


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

    def evaluate(self, routes, spatial_3d, grid, grid_attributes, existing_facilities, device_catalog):
        profiles = (spatial_3d or {}).get("route_altitude_profiles") or {}
        terrain = ((grid_attributes or {}).get("terrain") or {}).get("cells") or {}
        cells = list((grid or {}).get("cells") or [])
        providers = build_geometric_providers(
            existing_facilities, device_catalog,
            (spatial_3d or {}).get("site_vertical_profiles") or {},
        )
        results = []
        for route in routes or []:
            profile = profiles.get(str(route.get("route_id")))
            results.append(self._route(route, profile, cells, terrain, providers))
        fingerprint_input = {
            "routes": routes or [], "spatial_3d": spatial_3d or {},
            "terrain": terrain, "providers": providers, "parameters": self.parameters,
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

    def _route(self, route, profile, grid_cells, terrain, providers):
        route_id, path = str(route.get("route_id") or ""), route.get("path") or []
        if route.get("status") != "passed" or len(path) < 2:
            return {"route_id": route_id, "status": "missing_data", "route_length_m": 0.0, "samples": [], "subsystems": []}
        if not profile:
            return {"route_id": route_id, "status": "missing_data", "route_length_m": path_length_m(path), "samples": [], "subsystems": [], "reasons": ["缺少航路高度剖面"]}
        offsets, total = _sample_offsets(path, self.parameters["sample_spacing_m"])
        samples = [self._sample(route_id, path, offset, total, profile, grid_cells, terrain) for offset in offsets]
        subsystems = [self._subsystem(code, route_id, path, offsets, total, profile, grid_cells, terrain, providers.get(code, [])) for code in ("C", "N", "S")]
        statuses = [item["status"] for item in subsystems]
        status = "missing_data" if any(value == "missing_data" for value in statuses) else "failed" if any(value == "failed" for value in statuses) else "passed"
        return {
            "route_id": route_id, "status": status, "route_length_m": total,
            "altitude_profile": profile, "samples": samples, "subsystems": subsystems,
            "model_scope": self.model_scope,
        }

    def _sample(self, route_id, path, offset, total, profile, grid_cells, terrain):
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
        }

    def _subsystem(self, code, route_id, path, offsets, total, profile, grid_cells, terrain, providers):
        point_samples = [self._sample(route_id, path, value, total, profile, grid_cells, terrain) for value in offsets]
        for sample in point_samples:
            sample.update(evaluate_geometry_point(sample, providers))
        if not providers:
            return _missing_subsystem(code, total, point_samples, "没有可用的明确三维几何服务提供者")
        if any(sample["vertical_status"] != "passed" for sample in point_samples):
            return _missing_subsystem(code, total, point_samples, "航路高度无法统一解析为 EGM2008")
        intervals = []
        for start, end in zip(offsets, offsets[1:]):
            midpoint = self._sample(route_id, path, (start + end) / 2, total, profile, grid_cells, terrain)
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
    """Build the canonical P7 point-provider input from existing facilities only."""
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
            if subsystem not in result or geometry.get("model") not in ("sphere", "hemisphere") or geometry.get("slant_range_m") is None or origin is None:
                continue
            result[subsystem].append({
                "facility_id": facility.get("facility_id"), "device_id": device.get("device_id"),
                "coordinate": facility.get("coordinate"), "service_origin_egm2008_m": float(origin),
                "coverage_geometry": geometry,
            })
    return result


def evaluate_geometry_point(sample, providers):
    """Evaluate one EGM2008 point with the exact P7 geometric coverage rules."""
    if sample.get("vertical_status") != "passed":
        return {"covered": None, "providers": [], "nearest_slant_distance_m": None}
    matches, nearest = [], None
    point = [sample["longitude"], sample["latitude"]]
    for provider in providers:
        horizontal = distance_m(point, provider["coordinate"])
        delta = sample["altitude_egm2008_m"] - provider["service_origin_egm2008_m"]
        slant = math.hypot(horizontal, delta)
        nearest = slant if nearest is None else min(nearest, slant)
        geometry = provider["coverage_geometry"]
        vertical_ok = geometry["model"] == "sphere" or delta >= 0
        if vertical_ok and slant <= float(geometry["slant_range_m"]):
            matches.append({
                "facility_id": provider.get("facility_id"), "device_id": provider.get("device_id"),
                "slant_distance_m": slant, "horizontal_distance_m": horizontal,
                "vertical_delta_m": delta, "geometry_model": geometry["model"],
            })
    return {"covered": bool(matches), "providers": matches, "nearest_slant_distance_m": nearest}


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
