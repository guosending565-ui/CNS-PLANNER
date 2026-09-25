"""Production contracts for LayeredRouteCandidate continuous validation.

This module deliberately contains no V3-C/V3-D business contract.  Production validation
keeps the three lifecycle objects separate::

    LayeredRouteCandidate != LayeredRouteValidation != operational route

The validation checks the candidate's original two-dimensional centreline at the explicitly
selected, constant EGM2008 orthometric cruise altitude.  Airspace, CNS and RouteRiskProfile
classification thresholds are outside the validation value and fingerprint.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json


SCHEMA_VERSION = "layered-route-validation-v1"
COLLECTION_SCHEMA_VERSION = "layered-route-validation-collection-v1"
ALGORITHM_ID = "layered_candidate_continuous_validation_v1"
# 1.3 adds independent tower and restricted-area authority evidence.  The version
# participates in the validation fingerprint, so pre-B3X records become stale.
ALGORITHM_VERSION = "1.3"
VALIDATION_STATUSES = {
    "validated_candidate", "failed", "unresolved", "not_ready",
    "validation_incomplete", "stale",
}
VALIDATOR_VERSIONS = {
    "terrain": "source_native_terrain_validator_v1",
    "building": "real_footprint_building_validator_v4",
    "tower": "confirmed_tower_top_validator_v1",
    "restricted_area": "confirmed_restricted_area_validator_v1",
    "native_pixel_intervals": "native_pixel_interval_v1",
    "constant_vertical_context": "production_fixed_cruise_egm2008_v1",
}
ROUTE_SEMANTICS = "strategic_route_centerline_not_aircraft_kinematic_trajectory"


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stable_fingerprint(value, *, prefix=""):
    return prefix + sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


def path_fingerprint(path):
    return stable_fingerprint(
        [[float(point[0]), float(point[1])] for point in path or []],
        prefix="layeredpathv1-",
    )


def validation_fingerprint(components):
    """Fingerprint the closed safety-evidence dependency set.

    Callers must pass only the declared dependencies.  In particular airspace,
    RouteRiskProfile thresholds/classification and CNS are not accepted here by convention
    and never added by the production service.
    """

    return stable_fingerprint(components, prefix="layeredvalidationv1-")


def empty_layered_route_validation(status="not_ready"):
    return {
        "schema_version": SCHEMA_VERSION,
        "validation_id": None,
        "status": status if status in VALIDATION_STATUSES else "not_ready",
        "status_reason": None,
        "blocking_reasons": [],
        "validated_at": None,
        "candidate": {
            "candidate_id": None, "route_id": None, "altitude_layer_id": None,
            "candidate_fingerprint": None, "path_fingerprint": None,
            "status": None, "current_applicability": None,
        },
        "route": {
            "path_crs": "OGC:CRS84", "path": [], "metric_path": [],
            "metric_crs": None, "nominal_altitude_m": None,
            "vertical_reference": "egm2008_orthometric",
            "altitude_model": "constant_cruise_altitude",
            "semantics": ROUTE_SEMANTICS,
            "two_dimensional_source_path": True,
        },
        "domains": {
            "terrain": {}, "building": {}, "tower": {}, "restricted_area": {},
            "airspace": {
                "status": "not_applicable", "applicability": "display_only",
                "used_in_validation": False, "used_in_fingerprint": False,
            },
        },
        "minimum_margins": {
            "terrain_vertical_m": None, "building_vertical_m": None,
            "tower_vertical_m": None,
        },
        "failed_intervals": [],
        "unresolved_intervals": [],
        "critical_evidence": [],
        "source_type": None,
        "source_audits": {},
        "policies": {
            "terrain_vertical_clearance_m": None,
            "building_horizontal_clearance_m": None,
            "building_vertical_clearance_m": None,
            "tower_horizontal_clearance_m": None,
            "tower_vertical_clearance_m": None,
            "conditional_hard_exclusion_feature_ids": [],
        },
        "resource_limits": {
            "max_evidence_items": None, "observed_evidence_items": 0,
            "limit_reached": False, "safety_parameter": False,
        },
        "validator_versions": deepcopy(VALIDATOR_VERSIONS),
        "fingerprints": {
            "validation_fingerprint": None, "candidate_fingerprint": None,
            "path_fingerprint": None, "components": {},
        },
        "operational_route": False,
        "cns_assessed": False,
        "current_applicability": "not_evaluated",
        "stale_reason": None,
        "provenance": {},
        "semantics": {
            "candidate_is_not_validation": True,
            "validation_is_not_operational_route": True,
            "original_candidate_polyline_preserved": True,
            "constant_cruise_altitude": True,
            "no_rounding_refinement_replan_or_layer_change": True,
            "turn_radius_and_climb_gradient_not_evaluated": True,
            "terrain_source_native_pixels": True,
            "building_real_footprints": True,
            "tower_confirmed_top_required": True,
            "restricted_area_protection_geometry_required": True,
            "search_constraint_field_is_not_authority_evidence": True,
            "coarse_l8_building_height_not_a_final_verdict": True,
            "airspace_not_applicable_display_only": True,
            "resource_limit_is_computational_not_safety": True,
        },
    }


def normalize_layered_route_validation(value):
    if not isinstance(value, dict):
        return empty_layered_route_validation()
    status = str(value.get("status") or "not_ready")
    result = empty_layered_route_validation(status)
    result.update(deepcopy(value))
    result["schema_version"] = SCHEMA_VERSION
    if result.get("status") not in VALIDATION_STATUSES:
        result["status"] = "not_ready"
    # These flags are contract invariants, not mutable conclusions.
    result["operational_route"] = False
    result["cns_assessed"] = False
    result.setdefault("domains", {})
    for domain in ("terrain", "building", "tower", "restricted_area", "airspace"):
        result["domains"].setdefault(
            domain, deepcopy(empty_layered_route_validation()["domains"].get(domain) or {})
        )
    result.setdefault("semantics", {}).update(empty_layered_route_validation()["semantics"])
    result.setdefault("validator_versions", deepcopy(VALIDATOR_VERSIONS))
    return result


def empty_layered_route_validation_collection():
    return {
        "schema_version": COLLECTION_SCHEMA_VERSION,
        "status": "not_calculated", "count": 0, "active_validation_id": None,
        "items": [],
        "notes": [
            "LayeredRouteCandidate、LayeredRouteValidation 与 operational route 是三个独立对象。",
            "validated_candidate 仍强制 operational_route=false、cns_assessed=false。",
        ],
    }


def normalize_layered_route_validation_collection(value=None):
    source = value if isinstance(value, dict) else {}
    items = [
        normalize_layered_route_validation(item)
        for item in source.get("items") or [] if isinstance(item, dict)
    ]
    result = empty_layered_route_validation_collection()
    result.update(deepcopy(source))
    result["schema_version"] = COLLECTION_SCHEMA_VERSION
    result["items"] = items
    result["count"] = len(items)
    active = result.get("active_validation_id")
    if active not in {item.get("validation_id") for item in items}:
        result["active_validation_id"] = None
    if not items:
        result["status"] = "not_calculated"
    return result


__all__ = [
    "ALGORITHM_ID", "ALGORITHM_VERSION", "COLLECTION_SCHEMA_VERSION", "ROUTE_SEMANTICS",
    "SCHEMA_VERSION", "VALIDATION_STATUSES", "VALIDATOR_VERSIONS",
    "empty_layered_route_validation", "empty_layered_route_validation_collection",
    "normalize_layered_route_validation", "normalize_layered_route_validation_collection",
    "path_fingerprint", "stable_fingerprint", "utc_now", "validation_fingerprint",
]
