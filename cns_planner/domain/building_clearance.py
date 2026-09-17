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


# --------------------------------------------------------------------------- shared semantics
#
# The roof elevation and the vertical-clearance test are the *single* definition of
# the EGM2008 building-clearance semantics.  ``BuildingClearanceV1`` and the V3-C
# ``BuildingPolygonValidator`` both consume them, so a third, drifting roof formula
# cannot appear.  Both helpers are fail-closed: anything unresolved stays unresolved
# and is never reported as safe.


def building_roof_elevation(ground_elevation_m, height_m):
    """Canonical EGM2008 roof elevation = ground + predicted height.

    ``ground`` is the DTM ground elevation resolved over the footprint and ``height``
    the source (for example GBA ``height_m``) value.  Missing or non-finite inputs --
    and a missing/unknown ``height_status`` semantic decided by the caller before
    calling this -- leave the roof unresolved.
    """

    ground = _finite_or_none(ground_elevation_m)
    height = _finite_or_none(height_m)
    if ground is None:
        return {
            "status": "unresolved",
            "reason": "building_footprint_ground_elevation_unresolved",
            "roof_elevation_egm2008_m": None,
            "ground_elevation_m": None,
            "height_m": height,
            "vertical_reference": "egm2008_orthometric",
        }
    if height is None:
        return {
            "status": "unresolved",
            "reason": "building_height_missing",
            "roof_elevation_egm2008_m": None,
            "ground_elevation_m": ground,
            "height_m": None,
            "vertical_reference": "egm2008_orthometric",
        }
    return {
        "status": "resolved",
        "reason": None,
        "roof_elevation_egm2008_m": ground + height,
        "ground_elevation_m": ground,
        "height_m": height,
        "vertical_reference": "egm2008_orthometric",
    }


def evaluate_vertical_clearance(
    *, minimum_altitude_egm2008_m, roof_elevation_egm2008_m, required_clearance_m,
    ground_elevation_m=None,
):
    """Compare a route's minimum altitude with ``roof + required_clearance``.

    Returns the signed ``vertical_margin_m`` (positive = clear) and a status of
    ``resolved`` / ``unresolved``.  An aircraft between the building ground and the
    roof has zero clearance from the prism; an aircraft **below** the resolved ground
    (when the caller supplies it) is not a clearance figure at all and stays
    ``unresolved`` -- fail-closed, exactly as the existing assessor reads it.
    """

    altitude = _finite_or_none(minimum_altitude_egm2008_m)
    roof = _finite_or_none(roof_elevation_egm2008_m)
    required = _finite_or_none(required_clearance_m)
    ground = _finite_or_none(ground_elevation_m)
    base = {
        "vertical_reference": "egm2008_orthometric",
        "minimum_altitude_egm2008_m": altitude,
        "roof_elevation_egm2008_m": roof,
        "ground_elevation_m": ground,
        "required_clearance_m": required,
    }
    if altitude is None:
        return {**base, "status": "unresolved", "reason": "route_altitude_unresolved",
                "vertical_margin_m": None, "required_altitude_egm2008_m": None,
                "observed_clearance_m": None}
    if roof is None:
        return {**base, "status": "unresolved", "reason": "building_roof_unresolved",
                "vertical_margin_m": None, "required_altitude_egm2008_m": None,
                "observed_clearance_m": None}
    if required is None:
        return {**base, "status": "unresolved", "reason": "vertical_clearance_unresolved",
                "vertical_margin_m": None, "required_altitude_egm2008_m": None,
                "observed_clearance_m": None}
    if ground is not None and altitude < ground:
        return {**base, "status": "unresolved", "reason": "aircraft_below_building_ground",
                "vertical_margin_m": None, "required_altitude_egm2008_m": roof + required,
                "observed_clearance_m": None}
    observed_clearance = 0.0 if altitude <= roof else altitude - roof
    return {
        **base,
        "status": "resolved",
        "reason": None,
        "observed_clearance_m": observed_clearance,
        "required_altitude_egm2008_m": roof + required,
        "vertical_margin_m": observed_clearance - required,
    }


def _finite_or_none(value):
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


__all__ = [
    "building_roof_elevation", "default_building_clearance_policy",
    "empty_building_clearance_assessment", "evaluate_vertical_clearance",
    "normalize_building_clearance_policy", "normalize_building_clearance_assessment",
]
