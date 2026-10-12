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
from math import isfinite


SCHEMA_VERSION = "layered-route-validation-v1"
COLLECTION_SCHEMA_VERSION = "layered-route-validation-collection-v1"
ENGINEERING_DEMO_POLICY_VERSION = "engineering-demo-building-acceptance-v1"
ALGORITHM_ID = "layered_candidate_continuous_validation_v1"
# 1.3 adds independent tower and restricted-area authority evidence.  The version
# participates in the validation fingerprint, so pre-B3X records become stale.
# 1.4 adds the H0–H4 building height evidence gate: ``predicted`` / ``unknown``
# heights no longer support a ``validated_candidate`` (Round35-F3).  The version bump
# makes every pre-F3 ``validated_candidate`` record stale through the fingerprint.
ALGORITHM_VERSION = "1.5"
VALIDATION_STATUSES = {
    "validated_candidate", "failed", "unresolved", "not_ready",
    "validation_incomplete", "engineering_conditionally_accepted_candidate", "stale",
}
VALIDATOR_VERSIONS = {
    "terrain": "source_native_terrain_validator_v1",
    "building": "real_footprint_building_validator_v6",
    "tower": "confirmed_tower_top_validator_v1",
    "restricted_area": "confirmed_restricted_area_validator_v2",
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


def default_engineering_demo_building_acceptance_policy():
    return {
        "schema_version": ENGINEERING_DEMO_POLICY_VERSION,
        "status": "disabled",
        "enabled": False,
        "confirmed": False,
        "effective": False,
        "scenario_route_id": None,
        "altitude_layer_id": None,
        "candidate_id": None,
        "candidate_fingerprint": None,
        "building_id": None,
        "building_fid": None,
        "building_source_sha256": None,
        "building_evidence_fingerprint": None,
        "minimum_estimated_vertical_margin_m": None,
        "authorization": {},
        "policy_fingerprint": None,
        "limitations": [
            "工程条件接受，严格验证未通过，仅供演示",
            "predicted 证据等级保持不变，不等于 verified/source_tagged/derived",
            "机器学习方差是原始来源字段，不是经校准的安全置信界",
            "operational_authorization=false",
        ],
    }


def normalize_engineering_demo_building_acceptance_policy(value=None):
    source = value if isinstance(value, dict) else {}
    result = default_engineering_demo_building_acceptance_policy()
    result["enabled"] = source.get("enabled") is True
    result["confirmed"] = source.get("confirmed") is True
    for name in (
        "scenario_route_id", "altitude_layer_id", "candidate_id",
        "candidate_fingerprint", "building_id", "building_source_sha256",
        "building_evidence_fingerprint",
    ):
        text = str(source.get(name) or "").strip()
        result[name] = text or None
    try:
        fid = int(source.get("building_fid"))
        result["building_fid"] = fid if fid >= 0 else None
    except (TypeError, ValueError, OverflowError):
        result["building_fid"] = None
    try:
        margin = float(source.get("minimum_estimated_vertical_margin_m"))
        result["minimum_estimated_vertical_margin_m"] = (
            margin if isfinite(margin) and margin >= 0 else None
        )
    except (TypeError, ValueError, OverflowError):
        result["minimum_estimated_vertical_margin_m"] = None
    raw_authorization = source.get("authorization")
    raw_authorization = raw_authorization if isinstance(raw_authorization, dict) else {}
    authorization = {
        name: str(raw_authorization.get(name) or "").strip() or None
        for name in ("authorization_id", "authorized_by", "authorized_at", "scope")
    }
    result["authorization"] = authorization
    identity_complete = all(result.get(name) not in (None, "") for name in (
        "scenario_route_id", "altitude_layer_id", "candidate_id",
        "candidate_fingerprint", "building_id", "building_fid",
        "building_source_sha256", "building_evidence_fingerprint",
        "minimum_estimated_vertical_margin_m",
    ))
    authorization_complete = all(authorization.values()) and (
        authorization.get("scope") == "engineering_demo_only"
    )
    result["effective"] = bool(
        result["enabled"] and result["confirmed"]
        and identity_complete and authorization_complete
    )
    result["status"] = (
        "disabled" if not result["enabled"]
        else "confirmed" if result["effective"]
        else "pending_confirmation"
    )
    fingerprint_components = {
        key: deepcopy(result[key]) for key in (
            "schema_version", "enabled", "confirmed", "scenario_route_id",
            "altitude_layer_id", "candidate_id", "candidate_fingerprint",
            "building_id", "building_fid", "building_source_sha256",
            "building_evidence_fingerprint", "minimum_estimated_vertical_margin_m",
            "authorization",
        )
    }
    result["policy_fingerprint"] = stable_fingerprint(
        fingerprint_components, prefix="engineeringdemobuildingpolicyv1-",
    )
    return result


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
        "building_height_evidence": {
            "height_evidence_mix": {},
            "worst_evidence_level": None,
            "requires_survey": False,
        },
        "strict_safety_validation_passed": False,
        "engineering_conditional_acceptance": False,
        "use_scope": None,
        "operational_authorization": False,
        "engineering_acceptance": {},
        "limitations": [],
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
            "building_height_evidence_gated": True,
            "predicted_height_never_supports_pass": True,
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
    "ALGORITHM_ID", "ALGORITHM_VERSION", "COLLECTION_SCHEMA_VERSION",
    "ENGINEERING_DEMO_POLICY_VERSION", "ROUTE_SEMANTICS",
    "SCHEMA_VERSION", "VALIDATION_STATUSES", "VALIDATOR_VERSIONS",
    "default_engineering_demo_building_acceptance_policy",
    "empty_layered_route_validation", "empty_layered_route_validation_collection",
    "normalize_engineering_demo_building_acceptance_policy",
    "normalize_layered_route_validation", "normalize_layered_route_validation_collection",
    "path_fingerprint", "stable_fingerprint", "utc_now", "validation_fingerprint",
]
