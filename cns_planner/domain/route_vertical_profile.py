"""JSON-safe read-only route vertical profile result contracts."""

from __future__ import annotations

from copy import deepcopy


def empty_route_vertical_profiles(status="not_calculated"):
    return {
        "status": status,
        "algorithm_id": "route_vertical_profile_v1",
        "algorithm_version": "1.0",
        "input_fingerprint": None,
        "profile_geometry_status": status,
        "clearance_evidence_status": status,
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
    for profile in result["profiles"]:
        legacy = str(profile.get("status") or "unknown")
        if "profile_geometry_status" not in profile:
            profile["profile_geometry_status"] = "passed" if legacy in ("passed", "breach") else "unknown"
        if "clearance_evidence_status" not in profile:
            profile["clearance_evidence_status"] = legacy if legacy in ("passed", "breach") else "unknown"
    result["profile_count"] = len(result["profiles"])
    result["sample_count"] = sum(len(item.get("samples") or []) for item in result["profiles"])
    geometry = [item.get("profile_geometry_status") for item in result["profiles"]]
    clearance = [item.get("clearance_evidence_status") for item in result["profiles"]]
    if "profile_geometry_status" not in value:
        result["profile_geometry_status"] = _aggregate(geometry, "breach")
    if "clearance_evidence_status" not in value:
        result["clearance_evidence_status"] = _aggregate(clearance, "breach")
    result.setdefault("reasons", [])
    result.setdefault("semantics", empty_route_vertical_profiles()["semantics"])
    return result


def _aggregate(statuses, priority):
    if not statuses:
        return "missing_data"
    if priority in statuses:
        return priority
    return "passed" if all(item == "passed" for item in statuses) else "unknown"
