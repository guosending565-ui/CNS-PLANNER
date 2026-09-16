"""JSON-safe contracts for independent building-clearance engineering assessment."""

from __future__ import annotations

from copy import deepcopy
from math import isfinite


def default_building_clearance_policy():
    return {
        "status": "pending_confirmation",
        "horizontal_clearance_m": None,
        "vertical_clearance_m": None,
        "min_building_height_m": None,
        "terrain_relief_review_m": None,
        "source": "未配置；必须由项目工程依据确认",
        "confirmed": False,
        "vertical_reference": "egm2008_orthometric",
    }


def normalize_building_clearance_policy(value):
    source = value if isinstance(value, dict) else {}
    result = default_building_clearance_policy()
    source_text = str(source.get("source") or "").strip()
    result.update({
        "horizontal_clearance_m": _optional_nonnegative(
            source.get("horizontal_clearance_m"), "horizontal_clearance_m"
        ),
        "vertical_clearance_m": _optional_nonnegative(
            source.get("vertical_clearance_m"), "vertical_clearance_m"
        ),
        "min_building_height_m": _optional_nonnegative(
            source.get("min_building_height_m"), "min_building_height_m"
        ),
        "terrain_relief_review_m": _optional_nonnegative(
            source.get("terrain_relief_review_m"), "terrain_relief_review_m"
        ),
        "source": source_text or result["source"],
        "confirmed": bool(source.get("confirmed", False)),
    })
    complete = (
        result["horizontal_clearance_m"] is not None
        and result["vertical_clearance_m"] is not None
        and bool(source_text)
    )
    result["status"] = "confirmed" if complete and result["confirmed"] else "pending_confirmation"
    return result


def empty_building_clearance_assessment(status="not_calculated"):
    return {
        "status": status,
        "algorithm_id": "building_clearance_v1",
        "algorithm_version": "1.0",
        "input_fingerprint": None,
        "route_count": 0,
        "routes": [],
        "breach_segments": [],
        "critical_buildings": [],
        "statistics": {
            "breach_count": 0,
            "safe_route_count": 0,
            "unknown_route_count": 0,
            "unresolved_building_count": 0,
        },
        "provenance": {},
        "semantics": {
            "scope": "engineering_building_clearance_only",
            "not_accident_probability": True,
            "not_regulatory_compliance": True,
            "unknown_is_never_safe": True,
        },
    }


def normalize_building_clearance_assessment(value):
    if not isinstance(value, dict):
        return empty_building_clearance_assessment()
    result = empty_building_clearance_assessment(str(value.get("status") or "not_calculated"))
    result.update(deepcopy(value))
    result.setdefault("routes", [])
    result.setdefault("breach_segments", [])
    result.setdefault("critical_buildings", [])
    result.setdefault("statistics", empty_building_clearance_assessment()["statistics"])
    result.setdefault("provenance", {})
    result.setdefault("semantics", empty_building_clearance_assessment()["semantics"])
    return result


def _optional_nonnegative(value, field):
    if value in (None, ""):
        return None
    number = float(value)
    if not isfinite(number) or number < 0:
        raise ValueError(f"{field} 必须是有限非负数")
    return number
