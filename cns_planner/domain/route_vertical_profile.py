"""JSON-safe read-only route vertical profile result contracts."""

from __future__ import annotations

from copy import deepcopy


def empty_route_vertical_profiles(status="not_calculated"):
    return {
        "status": status,
        "algorithm_id": "route_vertical_profile_v1",
        "algorithm_version": "1.0",
        "input_fingerprint": None,
        "profile_count": 0,
        "sample_count": 0,
        "profiles": [],
        "reasons": [],
        "semantics": {
            "scope": "visualization_only",
            "safety_authority": "existing_building_clearance_assessment",
            "profile_samples_do_not_decide_clearance": True,
        },
    }


def normalize_route_vertical_profiles(value):
    if not isinstance(value, dict):
        return empty_route_vertical_profiles()
    result = empty_route_vertical_profiles(str(value.get("status") or "not_calculated"))
    result.update(deepcopy(value))
    result["profiles"] = list(value.get("profiles") or [])
    result["profile_count"] = len(result["profiles"])
    result["sample_count"] = sum(len(item.get("samples") or []) for item in result["profiles"])
    result.setdefault("reasons", [])
    result.setdefault("semantics", empty_route_vertical_profiles()["semantics"])
    return result
