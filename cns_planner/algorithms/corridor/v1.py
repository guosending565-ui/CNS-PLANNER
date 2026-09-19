"""Engineering CNS service requirement corridor and voxel-probe assessment V1."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import math

from ..coverage.geometric_3d import (
    GeometricCoverage3DV1,
    build_geometric_providers,
    evaluate_geometry_point,
    path_length_m,
    route_profile_height,
)
from ..coverage.v1 import distance_m
from ..service_capability.v1 import (
    CNSServiceCapabilityV1,
    SUBSYSTEM_NAMES,
    build_provider_devices,
    evaluate_capability_point,
)
from ...domain.cns_corridor import corridor_disclaimers, empty_cns_corridor_assessment
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
    ):
        cells = list((grid or {}).get("cells") or [])
        terrain = ((grid_attributes or {}).get("terrain") or {}).get("cells") or {}
        layers = list((spatial_3d or {}).get("altitude_layers") or [])
        specs = (corridor_policy or {}).get("routes") or {}
        geometric_providers = build_geometric_providers(
            existing_facilities, device_catalog,
            (spatial_3d or {}).get("site_vertical_profiles") or {},
        )
        provider_devices = build_provider_devices(device_catalog, existing_facilities)
        route_results = []
        for route in routes or []:
            route_id = str(route.get("route_id") or "")
            route_results.append(self._route(
                route, effective_route_vertical_context(spatial_3d, route_id),
                specs.get(route_id), cells, terrain,
                layers, required_cns or {}, aircraft_profile or {},
                geometric_providers, provider_devices,
            ))
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
        self, route, profile, spec, cells, terrain, layers, required_cns,
        aircraft, geometric_providers, provider_devices,
    ):
        route_id, path = str(route.get("route_id") or ""), route.get("path") or []
        total = path_length_m(path) if len(path) >= 2 else 0.0
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
        horizontal = []
        half_width = float(spec["horizontal_half_width_m"])
        for cell in cells:
            bbox = cell.get("bbox") or []
            if len(bbox) != 4:
                continue
            center = [(float(bbox[0]) + float(bbox[2])) / 2, (float(bbox[1]) + float(bbox[3])) / 2]
            nearest = nearest_route_position(path, center)
            half_diagonal = distance_m([bbox[0], bbox[1]], [bbox[2], bbox[3]]) / 2
            if nearest["distance_m"] <= half_width + half_diagonal:
                horizontal.append((cell, center, nearest, half_diagonal))
        requirements = ((required_cns.get("route_overrides") or {}).get(route_id)
                        or required_cns.get("project_default") or {})
        voxels = []
        for cell, center, nearest, half_diagonal in horizontal:
            voxels.extend(self._cell_voxels(
                route_id, total, profile, spec, cell, center, nearest, half_diagonal,
                terrain, confirmed_layers, requirements, aircraft,
                geometric_providers, provider_devices,
            ))
        summaries = [_summary(code, voxels) for code in ("C", "N", "S")]
        statuses = [item["status"] for item in summaries]
        status = _aggregate_status(statuses)
        corridor_geometry = {
            "route_path": deepcopy(path),
            "included_grid_ids": [str(item[0].get("grid_id")) for item in horizontal],
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
        self, route_id, total, profile, spec, cell, center, nearest, half_diagonal,
        terrain, layers, requirements, aircraft, geometric_providers, provider_devices,
    ):
        terrain_cell = terrain.get(str(cell.get("grid_id"))) or {}
        surface = terrain_cell.get("surface_elevation_mean_m") if terrain_cell.get("status") == "passed" else None
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
            lower = resolve_egm2008_height(
                layer.get("lower_altitude_m"), layer.get("vertical_reference", "unknown"),
                surface_elevation_m=surface, geoid_undulation_m=layer.get("geoid_undulation_m"),
            )
            upper = resolve_egm2008_height(
                layer.get("upper_altitude_m"), layer.get("vertical_reference", "unknown"),
                surface_elevation_m=surface, geoid_undulation_m=layer.get("geoid_undulation_m"),
            )
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
                    code, capability_input, requirements.get(name) or {}, aircraft, provider_devices
                )
                subsystems.append({
                    "subsystem": code,
                    "planning_status": _planning_status(capability["status"]),
                    "p8_status": capability["status"],
                    "geometry": geometry,
                    "providers": deepcopy(geometry.get("providers") or []),
                    "provider_evaluations": deepcopy(capability.get("provider_evaluations") or []),
                    "reasons": deepcopy(capability.get("reasons") or []),
                    "evidence": deepcopy(capability.get("evidence") or []),
                })
            area = cell_area_m2(cell.get("bbox"))
            thickness = overlap[1] - overlap[0] if overlap else None
            output.append({
                **ref, "route_id": route_id, "nearest_route_offset_m": nearest["route_offset_m"],
                "nearest_route_distance_m": nearest["distance_m"],
                "nearest_route_coordinate": nearest["coordinate"],
                "cell_half_diagonal_m": half_diagonal,
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


def nearest_route_position(path, point):
    """Return a deterministic nearest point/offset using local metric projection per segment."""
    cumulative, best = 0.0, None
    latitude = math.radians(float(point[1]))
    sx = 111_320.0 * max(math.cos(latitude), 1e-12)
    sy = 110_574.0
    for index, (left, right) in enumerate(zip(path, path[1:])):
        ax, ay = (float(left[0]) - point[0]) * sx, (float(left[1]) - point[1]) * sy
        bx, by = (float(right[0]) - point[0]) * sx, (float(right[1]) - point[1]) * sy
        dx, dy = bx - ax, by - ay
        denominator = dx * dx + dy * dy
        t = 0.0 if denominator == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / denominator))
        coordinate = [float(left[0]) + (float(right[0]) - float(left[0])) * t,
                      float(left[1]) + (float(right[1]) - float(left[1])) * t]
        segment_length = distance_m(left, right)
        candidate = {
            "distance_m": distance_m(point, coordinate),
            "route_offset_m": cumulative + segment_length * t,
            "coordinate": coordinate, "segment_index": index,
        }
        if best is None or (candidate["distance_m"], candidate["route_offset_m"], index) < (best["distance_m"], best["route_offset_m"], best["segment_index"]):
            best = candidate
        cumulative += segment_length
    return best or {"distance_m": 0.0, "route_offset_m": 0.0, "coordinate": list(point), "segment_index": 0}


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
