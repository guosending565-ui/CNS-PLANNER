"""Visualization-only vertical sampling for passed operational routes."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import math

from ...domain.route_vertical_profile import empty_route_vertical_profiles
from ...domain.spatial_3d import resolve_egm2008_height
from ..coverage.geometric_3d import path_length_m, route_point_at, route_profile_height


class RouteVerticalProfileV1:
    algorithm_id = "route_vertical_profile_v1"
    algorithm_version = "1.0"

    def __init__(self, parameters=None):
        raw = dict(parameters or {})
        spacing = float(raw.get("target_spacing_m", 100.0))
        maximum = int(raw.get("max_samples", 500))
        if not math.isfinite(spacing) or spacing <= 0:
            raise ValueError("target_spacing_m 必须大于零")
        if maximum < 2 or maximum > 5000:
            raise ValueError("max_samples 必须在 2..5000")
        self.parameters = {"target_spacing_m": spacing, "max_samples": maximum}

    @classmethod
    def empty(cls, status="not_calculated"):
        return empty_route_vertical_profiles(status)

    def evaluate(self, routes, spatial_3d, building_assessment, dtm_sampler, route_id=None):
        candidates = [item for item in (routes or []) if route_id in (None, "", str(item.get("route_id")))]
        profiles = []
        for route in candidates:
            profile = ((spatial_3d or {}).get("route_altitude_profiles") or {}).get(str(route.get("route_id")))
            profiles.append(self._route(route, profile, building_assessment or {}, dtm_sampler))
        fingerprint_input = {
            "routes": candidates,
            "altitude_profiles": (spatial_3d or {}).get("route_altitude_profiles") or {},
            "dtm": dtm_sampler.describe(),
            "building_assessment": {
                "status": (building_assessment or {}).get("status"),
                "input_fingerprint": (building_assessment or {}).get("input_fingerprint"),
                "routes": (building_assessment or {}).get("routes") or [],
                "breach_segments": (building_assessment or {}).get("breach_segments") or [],
                "critical_buildings": (building_assessment or {}).get("critical_buildings") or [],
            },
            "parameters": self.parameters,
        }
        fingerprint = _fingerprint(fingerprint_input)
        statuses = [item["status"] for item in profiles]
        if not profiles:
            status, reasons = "missing_data", ["没有可生成剖面的运行航路"]
        elif any(item == "breach" for item in statuses):
            status, reasons = "breach", []
        elif any(item in ("unknown", "missing_data") for item in statuses):
            status, reasons = "unknown", []
        else:
            status, reasons = "passed", []
        result = empty_route_vertical_profiles(status)
        result.update({
            "parameters": deepcopy(self.parameters), "input_fingerprint": fingerprint,
            "profile_geometry_status": _aggregate_status(
                [item.get("profile_geometry_status") for item in profiles]
            ),
            "clearance_evidence_status": _aggregate_status(
                [item.get("clearance_evidence_status") for item in profiles], breach=True
            ),
            "profile_count": len(profiles),
            "sample_count": sum(len(item["samples"]) for item in profiles),
            "profiles": profiles, "reasons": reasons,
            "provenance": {"terrain_dtm": dtm_sampler.describe()},
        })
        return result

    def _route(self, route, altitude_profile, building_assessment, dtm_sampler):
        route_id, path = str(route.get("route_id") or ""), route.get("path") or []
        total = path_length_m(path) if len(path) >= 2 else 0.0
        base = {
            "route_id": route_id, "status": "missing_data",
            "profile_geometry_status": "missing_data",
            "clearance_evidence_status": "missing_data",
            "vertical_reference": (altitude_profile or {}).get("vertical_reference", "unknown"),
            "route_length_m": total,
            "sampling": self._sampling(total, 0), "samples": [],
            "building_intervals": [], "breach_intervals": [], "critical_buildings": [],
            "required_vertical_clearance_m": None, "reasons": [],
            "semantics": "visualization_only; exact building breach comes from BuildingClearanceV1",
            "provenance": {
                "operational_route_status": route.get("status"),
                "altitude_profile_source": (altitude_profile or {}).get("source"),
                "terrain_dtm": dtm_sampler.describe(),
                "building_clearance_algorithm": building_assessment.get("algorithm_id"),
                "building_clearance_fingerprint": building_assessment.get("input_fingerprint"),
                "building_clearance_status": building_assessment.get("status"),
            },
        }
        base["fingerprint"] = _fingerprint({"route": route, "profile": altitude_profile, "provenance": base["provenance"], "parameters": self.parameters})
        if route.get("status") != "passed" or len(path) < 2:
            base["reasons"] = ["仅 passed operational route 可生成纵剖面"]
            return base
        if not altitude_profile or altitude_profile.get("status") != "confirmed":
            base.update(status="unknown", profile_geometry_status="unknown", clearance_evidence_status="unknown", reasons=["缺少已确认的航路高度剖面"])
            return base
        if altitude_profile.get("vertical_reference") == "unknown":
            base.update(status="unknown", profile_geometry_status="unknown", clearance_evidence_status="unknown", reasons=["航路 vertical reference 未确认"])
            return base
        offsets, actual_spacing = self._offsets(total)
        samples = [self._sample(path, offset, total, altitude_profile, dtm_sampler) for offset in offsets]
        clearance_route = next((item for item in building_assessment.get("routes") or [] if str(item.get("route_id")) == route_id), None)
        building_intervals = _building_intervals(clearance_route)
        breach_intervals = [
            deepcopy(item) for item in building_assessment.get("breach_segments") or []
            if str(item.get("route_id")) == route_id
        ]
        critical = [
            deepcopy(item) for item in building_assessment.get("critical_buildings") or []
            if str(item.get("route_id")) == route_id
        ]
        evidence_current = building_assessment.get("status") not in (None, "not_calculated", "stale") and clearance_route is not None
        sample_complete = bool(samples) and all(item["status"] == "passed" for item in samples)
        geometry_status = "passed" if sample_complete else "unknown"
        clearance_status = "breach" if evidence_current and breach_intervals else "passed" if evidence_current and clearance_route.get("status") == "passed" else "unknown"
        status = "breach" if clearance_status == "breach" else "passed" if geometry_status == clearance_status == "passed" else "unknown"
        reasons = []
        if not sample_complete:
            reasons.append("部分 FABDEM/高度样本不可解析")
        if not evidence_current:
            reasons.append("缺少当前有效的 BuildingClearanceV1 航路证据")
        elif clearance_route.get("status") not in ("passed", "failed"):
            reasons.append("BuildingClearanceV1 结论为 unknown")
        base.update({
            "status": status, "profile_geometry_status": geometry_status,
            "clearance_evidence_status": clearance_status,
            "sampling": self._sampling(total, len(samples), actual_spacing),
            "samples": samples, "building_intervals": building_intervals,
            "breach_intervals": breach_intervals, "critical_buildings": critical,
            "required_vertical_clearance_m": ((building_assessment.get("policy") or {}).get("vertical_clearance_m")),
            "reasons": reasons,
        })
        base["fingerprint"] = _fingerprint({"route": route, "profile": altitude_profile, "dtm": dtm_sampler.describe(), "building": base["provenance"], "parameters": self.parameters})
        return base

    def _sample(self, path, offset, total, profile, dtm_sampler):
        coordinate = route_point_at(path, offset)
        ground = dtm_sampler.sample_wgs84(coordinate)
        ground_value = ground.get("ground_egm2008_m") if ground.get("status") == "passed" else None
        try:
            input_height = route_profile_height(profile, offset, total)
            resolved = resolve_egm2008_height(
                input_height, profile.get("vertical_reference"),
                surface_elevation_m=ground_value,
                geoid_undulation_m=profile.get("geoid_undulation_m"),
            )
        except (TypeError, ValueError):
            input_height = None
            resolved = {"status": "missing_data", "altitude_egm2008_m": None, "reason": "route_altitude_unparseable"}
        flight = resolved.get("altitude_egm2008_m")
        agl = input_height if profile.get("vertical_reference") == "agl" and resolved.get("status") == "passed" else flight - ground_value if flight is not None and ground_value is not None else None
        clearance = flight - ground_value if flight is not None and ground_value is not None else None
        return {
            "distance_m": offset, "coordinate": coordinate,
            "ground_egm2008_m": ground_value, "flight_egm2008_m": flight,
            "flight_agl_m": agl, "ground_clearance_m": clearance,
            "status": "passed" if ground.get("status") == "passed" and resolved.get("status") == "passed" else "unknown",
            "reason": ground.get("reason") if ground.get("status") != "passed" else resolved.get("reason"),
        }

    def _offsets(self, total):
        maximum, requested = self.parameters["max_samples"], self.parameters["target_spacing_m"]
        actual = max(requested, total / (maximum - 1)) if total > 0 else requested
        count = max(2, min(maximum, int(math.ceil(total / actual)) + 1))
        if count == 2:
            return [0.0, total], total
        spacing = total / (count - 1)
        return [spacing * index for index in range(count)], spacing

    def _sampling(self, total, count, actual=None):
        return {
            "strategy": "adaptive_distance_sampling_for_visualization",
            "target_spacing_m": self.parameters["target_spacing_m"],
            "actual_spacing_m": actual,
            "max_samples": self.parameters["max_samples"], "sample_count": count,
            "route_length_m": total, "safety_decision": "not_evaluated_from_samples",
        }


def _building_intervals(route_result):
    result = []
    for building in (route_result or {}).get("buildings") or []:
        for interval in building.get("intervals") or []:
            result.append({
                **deepcopy(interval), "building_id": building.get("building_id"),
                "roof_elevation_m": building.get("roof_elevation_m"),
                "ground_elevation_m": building.get("ground_elevation_m"),
                "building_height_m": building.get("height_m"),
                "status": interval.get("status") or building.get("vertical_status") or "unknown",
            })
    return result


def _fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _aggregate_status(statuses, breach=False):
    if not statuses:
        return "missing_data"
    if breach and "breach" in statuses:
        return "breach"
    return "passed" if all(item == "passed" for item in statuses) else "unknown"
