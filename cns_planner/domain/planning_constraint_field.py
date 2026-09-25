"""Three-state fixed-cruise-layer Planning Constraint Field contract."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json


CONSTRAINT_OUTCOMES = ("pass", "blocked", "unknown")
BLOCKER_DOMAINS = ("terrain", "building", "tower", "airspace", "critical_site")
SCHEMA_VERSION = 1


def stable_constraint_fingerprint(value, *, prefix="pcf-"):
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return prefix + sha256(raw).hexdigest()


def normalize_unknown_policy(value=None):
    raw = value if isinstance(value, dict) else {}
    return {
        "allow_unknown_for_provisional": raw.get("allow_unknown_for_provisional") is True,
        "unknown_remains_unknown": True,
        "operational_adoption_allowed": False,
    }


def empty_planning_constraint_field_collection():
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "not_calculated",
        "collection_id": "planning_constraint_fields",
        "count": 0,
        "items": [],
    }


def normalize_constraint_cell(value):
    raw = value if isinstance(value, dict) else {}
    outcome = str(raw.get("outcome") or "unknown")
    if outcome not in CONSTRAINT_OUTCOMES:
        outcome = "unknown"
    blocked_by = sorted({
        str(item) for item in raw.get("blocked_by") or [] if str(item) in BLOCKER_DOMAINS
    })
    reasons = [deepcopy(item) for item in raw.get("unknown_reasons") or []]
    if blocked_by:
        outcome = "blocked"
    elif reasons:
        outcome = "unknown"
    return {
        "grid_id": str(raw.get("grid_id") or ""),
        "outcome": outcome,
        "blocked_by": blocked_by,
        "unknown_reasons": reasons,
        "evidence_refs": [deepcopy(item) for item in raw.get("evidence_refs") or []],
    }


def summarize_constraint_cells(cells):
    values = [normalize_constraint_cell(item) for item in cells or []]
    counts = {name: sum(1 for item in values if item["outcome"] == name) for name in CONSTRAINT_OUTCOMES}
    blockers = {
        domain: sum(1 for item in values if domain in item["blocked_by"])
        for domain in BLOCKER_DOMAINS
    }
    return {"total": len(values), **counts, "blocked_by": blockers}


def normalize_planning_constraint_field(value):
    raw = value if isinstance(value, dict) else {}
    cells = [normalize_constraint_cell(item) for item in raw.get("cells") or []]
    summary = deepcopy(raw.get("counts")) if isinstance(raw.get("counts"), dict) else summarize_constraint_cells(cells)
    return {
        "schema_version": SCHEMA_VERSION,
        "field_id": str(raw.get("field_id") or ""),
        "status": str(raw.get("status") or "not_calculated"),
        "altitude_layer_id": str(raw.get("altitude_layer_id") or ""),
        "nominal_altitude_m": raw.get("nominal_altitude_m"),
        "vertical_reference": str(raw.get("vertical_reference") or ""),
        "workspace_identity": deepcopy(raw.get("workspace_identity")),
        "grid_identity": deepcopy(raw.get("grid_identity")),
        "policy_fingerprint": raw.get("policy_fingerprint"),
        "source_fingerprints": deepcopy(raw.get("source_fingerprints") or {}),
        "constraint_field_fingerprint": raw.get("constraint_field_fingerprint"),
        "unknown_policy": normalize_unknown_policy(raw.get("unknown_policy")),
        "counts": summary,
        "warnings": [deepcopy(item) for item in raw.get("warnings") or []],
        "stale_reason": raw.get("stale_reason"),
        "artifact_ref": deepcopy(raw.get("artifact_ref")),
        "cells": cells,
    }


def normalize_planning_constraint_field_collection(value):
    raw = value if isinstance(value, dict) else {}
    items = [
        normalize_planning_constraint_field(item)
        for item in raw.get("items") or [] if isinstance(item, dict)
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "status": str(raw.get("status") or ("passed" if items else "not_calculated")),
        "collection_id": "planning_constraint_fields",
        "count": len(items),
        "items": items,
    }


def planning_constraint_field_summary(field, *, artifact_ref=None):
    item = normalize_planning_constraint_field(field)
    item.pop("cells", None)
    if artifact_ref is not None:
        item["artifact_ref"] = deepcopy(artifact_ref)
    return item


__all__ = [
    "BLOCKER_DOMAINS", "CONSTRAINT_OUTCOMES", "SCHEMA_VERSION",
    "empty_planning_constraint_field_collection", "normalize_constraint_cell",
    "normalize_planning_constraint_field", "normalize_planning_constraint_field_collection",
    "normalize_unknown_policy", "planning_constraint_field_summary",
    "stable_constraint_fingerprint", "summarize_constraint_cells",
]
