"""Read-only, extensible ``CommunicationPlanningField`` contract/provider interface.

This module is an **interface only**.  In this round:

* ``communication.used_in_cost = False``;
* ``communication.used_as_constraint = False``;
* a present, absent or changed communication field must not change the Theta* path or its
  cost by even one unit of distance — the field is recorded for readiness/provenance only.

What it is for: a future confirmed mapping from the existing ``DeviceCatalog`` /
``Coverage3D`` / ``CNSServiceCapability`` products onto a per-grid communication field.  It
exists now so that the interface (and its fingerprint separation) is fixed before any data
is attached.

Explicitly forbidden (and therefore absent from this contract):

* no invented communication weight, and no new objective term;
* converting a vendor reference coverage distance into a ``radius_m`` planning radius;
* using the not-yet-formally-mapped ``equipment_reference_catalog`` as a search constraint.
"""

from __future__ import annotations

from copy import deepcopy
import math
from numbers import Real

from .layered_route import stable_fingerprint

SCHEMA_VERSION = "communication-planning-field-v1"

NOT_CONFIGURED = "not_configured"

#: Per-grid coverage status vocabulary.
COVERAGE_STATUSES = (
    "covered", "partial", "uncovered", "unknown", "not_evaluated",
)

STATUSES = ("available", "partial", "not_configured", "unavailable")

#: The provider ids such a field may be mapped from, once that mapping is confirmed.
ALLOWED_PROVIDER_SOURCES = (
    "device_catalog", "coverage_3d", "cns_service_capability",
)


def _finite(value):
    return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(float(value))


def _optional_number(value):
    return round(float(value), 9) if _finite(value) else None


def default_communication_planning_field():
    """Ships ``not_configured``: no communication data is attached to this round."""

    return {
        "schema_version": SCHEMA_VERSION,
        "status": NOT_CONFIGURED,
        "status_reason": "no_confirmed_communication_field_provider_configured",
        "provider": None,
        "source": None,
        "source_fingerprint": None,
        "field_fingerprint": None,
        "provenance": {
            "interface": "communication_planning_field",
            "read_only": True,
            "used_in_cost": False,
            "used_as_constraint": False,
            "affects_path_or_cost_this_round": False,
            "informational_fingerprint_only": True,
            "allowed_provider_sources": list(ALLOWED_PROVIDER_SOURCES),
        },
        "semantics": {
            "interface_only_no_confirmed_mapping_yet": True,
            "no_communication_weight_is_invented": True,
            "vendor_reference_coverage_distance_is_not_a_radius_m": True,
            "equipment_reference_catalog_is_not_a_search_constraint": True,
            "present_absent_or_changed_must_not_change_the_path": True,
        },
        "count": 0,
        "cells": {},
    }


def communication_cell(
    grid_id, *, status, coverage_status, link_margin_db, latency_ms, provider_count,
    source, source_fingerprint, provenance=None,
):
    return {
        "grid_id": str(grid_id),
        "status": str(status),
        "coverage_status": str(coverage_status),
        "link_margin_db": _optional_number(link_margin_db),
        "latency_ms": _optional_number(latency_ms),
        "provider_count": (
            int(provider_count) if isinstance(provider_count, (int, float))
            and not isinstance(provider_count, bool) else None
        ),
        "unit": {"link_margin_db": "dB", "latency_ms": "ms"},
        "source": source,
        "source_fingerprint": source_fingerprint,
        "provenance": deepcopy(provenance or {}),
    }


def normalize_communication_planning_field(value):
    if value in (None, ""):
        return default_communication_planning_field()
    if not isinstance(value, dict):
        raise ValueError("communication_planning_field 必须是对象")
    result = default_communication_planning_field()
    result.update(deepcopy(value))
    result["schema_version"] = SCHEMA_VERSION
    cells = result.get("cells")
    result["cells"] = cells if isinstance(cells, dict) else {}
    result["count"] = len(result["cells"])
    if not result["cells"]:
        result["status"] = NOT_CONFIGURED
        result.setdefault(
            "status_reason", "no_confirmed_communication_field_provider_configured",
        )
    result.setdefault("provenance", default_communication_planning_field()["provenance"])
    # The interface contract is enforced unconditionally: no consumer may flip these.
    result["provenance"]["used_in_cost"] = False
    result["provenance"]["used_as_constraint"] = False
    result["provenance"]["affects_path_or_cost_this_round"] = False
    result.setdefault("semantics", default_communication_planning_field()["semantics"])
    return result


def communication_field_fingerprint(value):
    """**Informational** fingerprint.

    It is never a component of the candidate/optimization fingerprint in this round: the
    field does not participate in the cost or a constraint.
    """

    item = value if isinstance(value, dict) else {}
    return stable_fingerprint({
        "schema_version": SCHEMA_VERSION,
        "status": item.get("status"),
        "provider": item.get("provider"),
        "source": item.get("source"),
        "source_fingerprint": item.get("source_fingerprint"),
        "cells": {
            str(grid_id): {
                "status": (cell or {}).get("status"),
                "coverage_status": (cell or {}).get("coverage_status"),
                "link_margin_db": (cell or {}).get("link_margin_db"),
                "latency_ms": (cell or {}).get("latency_ms"),
                "provider_count": (cell or {}).get("provider_count"),
            }
            for grid_id, cell in sorted((item.get("cells") or {}).items())
        },
    }, prefix="commsfieldv1-")


def communication_readiness(value):
    """Readiness view recorded on every candidate (present, absent or changed)."""

    item = normalize_communication_planning_field(value)
    return {
        "interface": "communication_planning_field",
        "status": item.get("status"),
        "provider": item.get("provider"),
        "source": item.get("source"),
        "cell_count": int(item.get("count") or 0),
        "informational_fingerprint": communication_field_fingerprint(item),
        "used_in_cost": False,
        "used_as_constraint": False,
        "affected_path_or_cost": False,
        "readiness": (
            "field_present_readiness_only" if item.get("count")
            else "interface_declared_no_field_configured"
        ),
        "semantics": deepcopy(item.get("semantics") or {}),
    }


__all__ = [
    "ALLOWED_PROVIDER_SOURCES", "COVERAGE_STATUSES", "NOT_CONFIGURED", "SCHEMA_VERSION",
    "STATUSES", "communication_cell", "communication_field_fingerprint",
    "communication_readiness", "default_communication_planning_field",
    "normalize_communication_planning_field",
]
