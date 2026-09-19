"""Building-footprint and vertical-prism clearance assessment.

GIS candidate selection and exact polygon intersection remain behind the adapter;
this model owns policy gating, vertical semantics, event classification and the
JSON-safe result contract.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import math

from ...domain.building_clearance import (
    building_roof_elevation, empty_building_clearance_assessment,
    evaluate_vertical_clearance,
)
from ...domain.spatial_3d import effective_route_vertical_context, resolve_egm2008_height
from ..coverage.geometric_3d import path_length_m, route_profile_height


class BuildingClearanceV1:
    algorithm_id = "building_clearance_v1"
    algorithm_version = "1.0"

    @classmethod
    def empty(cls, status="not_calculated"):
        return empty_building_clearance_assessment(status)

    def evaluate(self, routes, spatial_3d, policy, adapter):
        horizontal = policy.get("horizontal_clearance_m")
        vertical = policy.get("vertical_clearance_m")
        if horizontal is None or vertical is None:
            result = self.empty("pending_confirmation")
            result["message"] = "缺少显式水平/垂直净空参数"
            result["provenance"] = adapter.provenance()
            return result

        route_results = []
        for route in routes or []:
            route_results.append(self._route(
                route, effective_route_vertical_context(spatial_3d, route.get("route_id")),
                policy, adapter
            ))
        breaches = [
            segment for route in route_results
            for segment in route.get("breach_segments") or []
        ]
        critical = _critical_buildings(route_results)
        for route in route_results:
            for building in route.get("buildings") or []:
                building.pop("geometry", None)
                for interval in building.get("intervals") or []:
                    interval.pop("path", None)
        unresolved = sum(
            item.get("unresolved_building_count", 0) for item in route_results
        )
        if not route_results:
            status = "missing_data"
        elif policy.get("status") != "confirmed":
            status = "pending_confirmation"
        elif any(item["status"] in ("unknown", "missing_data", "unresolved") for item in route_results):
            status = "unresolved"
        elif breaches:
            status = "failed"
        else:
            status = "passed"
        fingerprint_input = {
            "routes": routes or [], "spatial_3d": spatial_3d or {},
            "policy": policy, "sources": adapter.provenance(),
        }
        fingerprint = sha256(json.dumps(
            fingerprint_input, ensure_ascii=False, sort_keys=True, allow_nan=False,
        ).encode("utf-8")).hexdigest()
        result = self.empty(status)
        result.update({
            "input_fingerprint": fingerprint,
            "route_count": len(route_results),
            "routes": route_results,
            "breach_segments": breaches,
            "critical_buildings": critical,
            "statistics": {
                "breach_count": len(breaches),
                "safe_route_count": sum(item["status"] == "passed" for item in route_results),
                "unknown_route_count": sum(item["status"] in ("unknown", "missing_data", "unresolved") for item in route_results),
                "unresolved_building_count": unresolved,
            },
            "policy": dict(policy),
            "provenance": adapter.provenance(),
        })
        return result

    def _route(self, route, profile, policy, adapter):
        route_id = str(route.get("route_id") or "")
        path = route.get("path") or []
        if route.get("status") != "passed" or len(path) < 2:
            return _missing_route(route_id, "运行航路不存在或状态不是 passed")
        if not profile or profile.get("status") not in ("passed", "confirmed"):
            return _missing_route(route_id, "缺少已确认 effective route vertical context")
        evidence = adapter.route_candidates(route, float(policy["horizontal_clearance_m"]), policy)
        candidates = evidence.get("candidates") or []
        segments, buildings, unresolved = [], [], 0
        total = path_length_m(path)
        minimum_horizontal = None
        minimum_vertical = None
        for candidate in candidates:
            height = _finite_or_none(candidate.get("height_m"))
            terrain = candidate.get("terrain") or {}
            ground = _finite_or_none(terrain.get("ground_elevation_median_m"))
            vertical_status = "resolved"
            reason = None
            if height is None:
                vertical_status, reason = "unresolved", "building_height_missing"
            elif terrain.get("dtm_status") != "passed" or ground is None:
                vertical_status, reason = "unresolved", "building_footprint_dtm_unresolved"
            # Shared canonical roof semantics (the same helper V3-C consumes).
            roof_result = building_roof_elevation(ground, height)
            roof = roof_result["roof_elevation_egm2008_m"]
            if vertical_status == "resolved" and roof_result["status"] != "resolved":
                vertical_status, reason = "unresolved", roof_result.get("reason")
            interval_results = []
            for interval in candidate.get("affected_intervals") or []:
                offsets = sorted(set([
                    float(interval["start_distance_m"]),
                    float(interval["end_distance_m"]),
                    float(interval.get("closest_distance_along_route_m", (
                        float(interval["start_distance_m"]) + float(interval["end_distance_m"])
                    ) / 2)),
                ]))
                vertical_values = []
                vertical_reasons = []
                if vertical_status == "resolved":
                    for offset in offsets:
                        input_height = route_profile_height(profile, offset, total)
                        surface = adapter.route_surface_elevation(route, offset)
                        resolved = resolve_egm2008_height(
                            input_height, profile.get("vertical_reference", "unknown"),
                            surface_elevation_m=surface,
                            geoid_undulation_m=profile.get("geoid_undulation_m"),
                        )
                        aircraft = resolved.get("altitude_egm2008_m")
                        if resolved.get("status") != "passed" or aircraft is None:
                            vertical_reasons.append(resolved.get("reason") or "route_altitude_unresolved")
                            continue
                        evaluation = evaluate_vertical_clearance(
                            minimum_altitude_egm2008_m=aircraft,
                            roof_elevation_egm2008_m=roof,
                            required_clearance_m=float(policy["vertical_clearance_m"]),
                            ground_elevation_m=ground,
                        )
                        if evaluation["status"] != "resolved":
                            vertical_reasons.append(
                                evaluation.get("reason") or "vertical_clearance_unresolved"
                            )
                            continue
                        vertical_values.append(evaluation["observed_clearance_m"])
                    if not vertical_values or vertical_reasons:
                        vertical_status = "unresolved"
                        reason = ";".join(sorted(set(vertical_reasons))) or "route_altitude_unresolved"
                vertical_minimum = min(vertical_values) if vertical_status == "resolved" else None
                horizontal_minimum = float(candidate.get("horizontal_minimum_m", 0.0))
                minimum_horizontal = horizontal_minimum if minimum_horizontal is None else min(minimum_horizontal, horizontal_minimum)
                if vertical_minimum is not None:
                    minimum_vertical = vertical_minimum if minimum_vertical is None else min(minimum_vertical, vertical_minimum)
                breach = (
                    horizontal_minimum < float(policy["horizontal_clearance_m"])
                    and vertical_status == "resolved"
                    and vertical_minimum < float(policy["vertical_clearance_m"])
                )
                item = {
                    "route_id": route_id,
                    "building_id": candidate.get("building_id"),
                    "building_source": candidate.get("source"),
                    "building_height_m": height,
                    "height_var": candidate.get("height_var"),
                    "ground_elevation_m": ground,
                    "roof_elevation_m": roof,
                    "terrain": terrain,
                    "horizontal_minimum_m": horizontal_minimum,
                    "vertical_minimum_m": vertical_minimum,
                    "distance_3d_minimum_m": math.hypot(horizontal_minimum, vertical_minimum) if vertical_minimum is not None else None,
                    "start_distance_m": float(interval["start_distance_m"]),
                    "end_distance_m": float(interval["end_distance_m"]),
                    "breach_length_m": max(0.0, float(interval["end_distance_m"]) - float(interval["start_distance_m"])),
                    "path": interval.get("path") or [],
                    "vertical_status": vertical_status,
                    "status": "breach" if breach and policy.get("status") == "confirmed" else "safe" if vertical_status == "resolved" and policy.get("status") == "confirmed" else "unknown",
                    "unresolved_reason": reason,
                    "evidence": candidate.get("evidence") or {},
                }
                interval_results.append(item)
                if item["status"] == "breach":
                    segments.append(item)
            if vertical_status != "resolved":
                unresolved += 1
            buildings.append({
                "building_id": candidate.get("building_id"),
                "source": candidate.get("source"),
                "height_m": height, "height_var": candidate.get("height_var"),
                "ground_elevation_m": ground, "roof_elevation_m": roof,
                "horizontal_minimum_m": candidate.get("horizontal_minimum_m"),
                "vertical_status": vertical_status, "unresolved_reason": reason,
                "geometry": candidate.get("geometry"), "intervals": interval_results,
            })
        if evidence.get("status") != "passed":
            route_status = "missing_data"
        elif unresolved:
            route_status = "unresolved"
        elif segments:
            route_status = "failed"
        elif policy.get("status") == "confirmed":
            route_status = "passed"
        else:
            route_status = "unknown"
        return {
            "route_id": route_id, "status": route_status,
            "route_length_m": total, "candidate_building_count": len(candidates),
            "unresolved_building_count": unresolved,
            "minimum_horizontal_clearance_m": minimum_horizontal,
            "minimum_vertical_clearance_m": minimum_vertical,
            "breach_segments": segments, "buildings": buildings,
            "query": evidence.get("query") or {},
        }


def _missing_route(route_id, reason):
    return {
        "route_id": route_id, "status": "missing_data", "route_length_m": 0.0,
        "candidate_building_count": 0, "unresolved_building_count": 0,
        "minimum_horizontal_clearance_m": None, "minimum_vertical_clearance_m": None,
        "breach_segments": [], "buildings": [], "reasons": [reason],
    }


def _critical_buildings(routes):
    rows = [item for route in routes for item in route.get("buildings") or []]
    rows.sort(key=lambda item: (
        not any(interval.get("status") == "breach" for interval in item.get("intervals") or []),
        item.get("vertical_status") != "resolved",
        float("inf") if item.get("horizontal_minimum_m") is None else item["horizontal_minimum_m"],
        str(item.get("building_id")),
    ))
    return deepcopy(rows[:100])


def _finite_or_none(value):
    if value in (None, ""):
        return None
    number = float(value)
    return number if math.isfinite(number) else None
