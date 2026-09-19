"""Production LayeredRouteValidation -> operational adoption contracts."""

from __future__ import annotations

from copy import deepcopy

from .layered_route_validation import stable_fingerprint


SCHEMA_VERSION = "layered-operational-adoption-v1"
COLLECTION_SCHEMA_VERSION = "layered-operational-adoption-collection-v1"
ALGORITHM_ID = "layered_operational_adoption_v1"
ALGORITHM_VERSION = "1.0"
SOURCE_TYPE = "layered_candidate_operational_adoption_v1"
ADOPTION_STATUSES = {"published", "stale", "revoked"}


def adoption_fingerprint(value):
    return stable_fingerprint(value, prefix="layeredadoptionv1-")


def empty_layered_operational_adoption():
    return {
        "schema_version": SCHEMA_VERSION,
        "adoption_id": None, "route_id": None, "status": "published",
        "current_applicability": "current", "applied_at": None, "revoked_at": None,
        "validation_id": None, "validation_fingerprint": None,
        "candidate_id": None, "candidate_fingerprint": None,
        "projection_fingerprint": None, "altitude_layer_id": None,
        "source_type": SOURCE_TYPE, "replace_existing": False,
        "before": {"route": None, "route_operating_layer": None},
        "after": {"route": None, "route_operating_layer": None},
        "ownership": {"route_owned": True, "route_operating_layer_owned": True},
        "stale_reason": None, "provenance": {},
    }


def normalize_layered_operational_adoption(value):
    result = empty_layered_operational_adoption()
    if isinstance(value, dict):
        result.update(deepcopy(value))
    result["schema_version"] = SCHEMA_VERSION
    if result.get("status") not in ADOPTION_STATUSES:
        result["status"] = "published"
    result["source_type"] = SOURCE_TYPE
    return result


def empty_layered_operational_adoptions():
    return {
        "schema_version": COLLECTION_SCHEMA_VERSION,
        "status": "not_calculated", "count": 0, "items": [],
    }


def normalize_layered_operational_adoptions(value=None):
    source = value if isinstance(value, dict) else {}
    items = [
        normalize_layered_operational_adoption(item)
        for item in source.get("items") or [] if isinstance(item, dict)
    ]
    return {
        "schema_version": COLLECTION_SCHEMA_VERSION,
        "status": str(source.get("status") or ("passed" if items else "not_calculated")),
        "count": len(items), "items": items,
    }


__all__ = [
    "ADOPTION_STATUSES", "ALGORITHM_ID", "ALGORITHM_VERSION", "COLLECTION_SCHEMA_VERSION",
    "SCHEMA_VERSION", "SOURCE_TYPE", "adoption_fingerprint",
    "empty_layered_operational_adoption", "empty_layered_operational_adoptions",
    "normalize_layered_operational_adoption", "normalize_layered_operational_adoptions",
]
