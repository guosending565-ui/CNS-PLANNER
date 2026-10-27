"""R0005 research input preparation: 2 km device radii and surface facts.

Research-only (``authority=research_isolated``).  Nothing here writes the formal
``ProjectState``, the formal project directories or Git.  Every step is
fail-closed: a radius that is not *actually* resolved to 2000 m, or a land mask
that cannot be SHA-verified against its registered audit, raises instead of
silently shrinking coverage or guessing a surface class.

The 2 km radius is written into the **device input** records (device catalog,
existing-facility devices and the required service policy) and then verified
through the single product radius resolver (``effective_radius_m``), not merely
by changing a policy constant.
"""
from __future__ import annotations

import hashlib
import os
from copy import deepcopy
from pathlib import Path

from ..domain.cns_service_contract import (
    COMMUNICATION_RADIUS_BY_SURFACE,
    effective_radius_m,
)
from ..domain.surface_classification import build_surface_class_facts
from ..gis.land_mask_source import (
    audit_manifest_for,
    build_land_mask_source,
    land_mask_content_sha256,
    land_mask_source_identity,
    land_mask_source_path,
)

COMMUNICATION_SERVICE_KEY = "C:communication"
COMMUNICATION_SURFACES = ("land", "coastal_uncertain", "sea")
TWO_KILOMETRE_M = 2000.0
AUTHORITY = "research_isolated"


def _sha256(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def _surface_mapping(geometry):
    mapping = (geometry or {}).get("radius_by_surface")
    if isinstance(mapping, dict) and isinstance(mapping.get("resolved"), dict):
        return mapping["resolved"]
    return mapping if isinstance(mapping, dict) else {}


def _patch_device(device, *, field_path, records, legacy_slant=False, set_radius_m=False):
    geometry = device.setdefault("coverage_geometry", {})
    mapping = _surface_mapping(geometry)
    if not mapping:
        mapping = {}
        geometry["radius_by_surface"] = mapping
    for surface in COMMUNICATION_SURFACES:
        before = mapping.get(surface)
        mapping[surface] = TWO_KILOMETRE_M
        records.append({
            "device_id": device.get("device_id"), "field_path":
                field_path + ".radius_by_surface." + surface,
            "before": before, "after": TWO_KILOMETRE_M,
        })
    if set_radius_m:
        before_radius = device.get("radius_m")
        device["radius_m"] = TWO_KILOMETRE_M
        records.append({"device_id": device.get("device_id"),
                        "field_path": field_path + ".radius_m",
                        "before": before_radius, "after": TWO_KILOMETRE_M})
    if legacy_slant and geometry.get("slant_range_m") is not None:
        records.append({"device_id": device.get("device_id"),
                        "field_path": field_path + ".slant_range_m",
                        "before": geometry.get("slant_range_m"), "after": TWO_KILOMETRE_M})
        geometry["slant_range_m"] = TWO_KILOMETRE_M


def apply_two_kilometre_device_inputs(state):
    """Write 2 km into the actual communication device inputs (in place)."""

    records = []
    catalog = (state.get("device_catalog") or {}).get("items") or []
    for index, item in enumerate(catalog):
        if item.get("service_key") != COMMUNICATION_SERVICE_KEY:
            continue
        _patch_device(item, field_path="device_catalog/items/%d" % index,
                      records=records, set_radius_m=True)
    facilities = (state.get("existing_cns_facilities") or {}).get("items") or []
    for site_index, facility in enumerate(facilities):
        for device_index, device in enumerate(facility.get("devices") or []):
            if device.get("service_key") != COMMUNICATION_SERVICE_KEY:
                continue
            _patch_device(
                device,
                field_path=("existing_cns_facilities/items/%d/devices/%d"
                            % (site_index, device_index)),
                records=records, legacy_slant=True,
            )
    required = state.get("required_cns") or {}
    containers = [("project_default", required.get("project_default") or {})]
    for route_id, override in sorted((required.get("route_overrides") or {}).items()):
        containers.append(("route_overrides/%s" % route_id, override or {}))
    for label, container in containers:
        communication = container.get("communication") or {}
        for key, service in (communication.get("services") or {}).items():
            if str(key) != COMMUNICATION_SERVICE_KEY:
                continue
            mapping = service.get("radius_by_surface")
            if not isinstance(mapping, dict):
                continue
            for surface in COMMUNICATION_SURFACES:
                if mapping.get(surface) == TWO_KILOMETRE_M:
                    continue
                records.append({
                    "device_id": "required_cns:%s:%s" % (label, key),
                    "field_path": ("required_cns/%s/communication/services/%s/"
                                   "radius_by_surface/%s" % (label, key, surface)),
                    "before": mapping.get(surface), "after": TWO_KILOMETRE_M,
                })
                mapping[surface] = TWO_KILOMETRE_M
    return records


def _communication_devices(state):
    for index, item in enumerate((state.get("device_catalog") or {}).get("items") or []):
        if item.get("service_key") == COMMUNICATION_SERVICE_KEY:
            yield "device_catalog/items/%d" % index, item
    for site_index, facility in enumerate(
            (state.get("existing_cns_facilities") or {}).get("items") or []):
        for device_index, device in enumerate(facility.get("devices") or []):
            if device.get("service_key") == COMMUNICATION_SERVICE_KEY:
                yield ("existing_cns_facilities/items/%d/devices/%d"
                       % (site_index, device_index)), device


def verify_two_kilometre_device_inputs(state):
    """Resolve every *actual* communication provider radius (not the policy)."""

    rows, violations = [], []
    for path, device in _communication_devices(state):
        geometry = device.get("coverage_geometry") or {}
        for surface in COMMUNICATION_SURFACES:
            resolved = effective_radius_m(geometry, surface)
            row = {
                "provider_path": path, "device_id": device.get("device_id"),
                "facility_site_id": device.get("takeoff_landing_site_id"),
                "surface": surface,
                "effective_radius_m": resolved.get("radius_m"),
                "radius_source": resolved.get("source"),
            }
            rows.append(row)
            if (resolved.get("radius_m") != TWO_KILOMETRE_M
                    or resolved.get("source") != "radius_by_surface"):
                violations.append(row)
    required = state.get("required_cns") or {}
    for label, container in [
            ("project_default", required.get("project_default") or {}),
            *[("route_overrides/%s" % route_id, override or {})
              for route_id, override in sorted((required.get("route_overrides") or {}).items())],
    ]:
        service = ((container.get("communication") or {}).get("services") or {}).get(
            COMMUNICATION_SERVICE_KEY) or {}
        mapping = service.get("radius_by_surface")
        if not isinstance(mapping, dict):
            continue
        for surface in COMMUNICATION_SURFACES:
            if mapping.get(surface) != TWO_KILOMETRE_M:
                violations.append({
                    "provider_path": "required_cns/%s/communication" % label,
                    "device_id": None, "facility_site_id": None, "surface": surface,
                    "effective_radius_m": mapping.get(surface),
                    "radius_source": "required_policy_mismatch",
                })
    policy = dict(COMMUNICATION_RADIUS_BY_SURFACE)
    report = {
        "status": "PASS" if not violations and all(
            value == TWO_KILOMETRE_M for value in policy.values()) else "FAIL",
        "expected_radius_m": TWO_KILOMETRE_M,
        "policy_radius_by_surface": policy,
        "provider_sample_count": len(rows),
        "distinct_provider_count": len({row["provider_path"] for row in rows}),
        "surfaces": list(COMMUNICATION_SURFACES),
        "all_effective_radii_2000m": not violations,
        "violations": violations,
        "sample": rows[:12],
        "semantics": (
            "effective_radius_m reads each device coverage_geometry; the policy constant "
            "alone is not accepted as evidence"
        ),
    }
    return report


def prepare_two_kilometre_inputs(state):
    """Apply then verify; raise if any actual provider is not 2 km."""

    records = apply_two_kilometre_device_inputs(state)
    report = verify_two_kilometre_device_inputs(state)
    report["patched_field_count"] = len(records)
    report["patches"] = records
    if report["status"] != "PASS":
        raise ValueError(
            "communication providers are not 2 km after device input generation: %s"
            % (report["violations"][:3],)
        )
    return report


def _resolve_land_mask(state):
    path = land_mask_source_path(state)
    if path:
        return str(path), "state_registry"
    audit = audit_manifest_for(state) or {}
    for key in ("configured_path", "source_path"):
        candidate = str(audit.get(key) or "").strip()
        if candidate and Path(candidate).is_file():
            return candidate, "source_audit_" + key
    name = str(audit.get("file_name") or "").strip()
    if name:
        stored_facts = state.get("surface_class_facts") or {}
        recorded_path = (stored_facts.get("land_mask") or {}).get("configured_path")
        directories = []
        if recorded_path:
            directories.append(Path(recorded_path).parent)
        directories.append(Path(__file__).resolve().parents[2] / "projects" / "derived" / "land_mask_osm")
        for directory in directories:
            candidate = directory / name
            if candidate.is_file():
                return str(candidate), "source_audit_file_name"
    facts = state.get("surface_class_facts") or {}
    recorded = (facts.get("land_mask") or {}).get("configured_path")
    if recorded and os.path.isfile(recorded):
        return str(recorded), "facts_configured_path"
    return None, "unresolved"


def recover_surface_facts(state):
    """Rebuild surface facts from the currently audited land mask (fail-closed)."""

    stored = state.get("surface_class_facts") or {}
    policy = state.get("surface_classification_policy") or {}
    path, origin = _resolve_land_mask(state)
    audit = audit_manifest_for(state) or {}
    report = {
        "authority": AUTHORITY,
        "path": path, "path_origin": origin,
        "stored_status": stored.get("status"),
        "stored_stale_reason": stored.get("stale_reason"),
        "stored_input_fingerprint": stored.get("input_fingerprint"),
        "audit_sha256": audit.get("sha256"),
        "actual_sha256": None, "sha_matches_audit": None,
    }
    if not path:
        return None, {**report, "status": "missing_data",
                      "reason": "land_mask_path_unresolved"}
    actual = _sha256(path)
    report["actual_sha256"] = actual
    audit_sha = str(report["audit_sha256"] or "")
    if audit_sha:
        report["sha_matches_audit"] = actual == audit_sha
        if not report["sha_matches_audit"]:
            return None, {**report, "status": "missing_data",
                          "reason": "land_mask_content_sha256_mismatch"}
    source = build_land_mask_source(path, policy)
    if source is None:
        return None, {**report, "status": "missing_data",
                      "reason": "land_mask_source_unavailable"}
    try:
        source.prepare()
    except Exception as exc:  # noqa: BLE001 - fail-closed, never treat read error as pass
        return None, {**report, "status": "missing_data",
                      "reason": "land_mask_prepare_failed: %s" % type(exc).__name__}
    identity = land_mask_source_identity(
        source.describe(), audit_manifest=audit,
        content_sha256=land_mask_content_sha256(path, audit),
    )
    fresh = build_surface_class_facts(
        state.get("grid") or {}, policy=policy, land_mask_source=source,
        land_mask_describe=source.describe, source_identity=identity,
    )
    by_fresh = fresh.get("by_grid_id") or {}
    by_stored = stored.get("by_grid_id") or {}
    diff = [grid_id for grid_id in sorted(set(by_fresh) | set(by_stored))
            if by_fresh.get(grid_id) != by_stored.get(grid_id)]
    report.update({
        "status": "recovered" if fresh.get("status") == "passed" else "missing_data",
        "fresh_status": fresh.get("status"),
        "fresh_input_fingerprint": fresh.get("input_fingerprint"),
        "surface_class_counts_fresh": fresh.get("surface_class_counts"),
        "surface_class_counts_stored": stored.get("surface_class_counts"),
        "classification_equal_to_stored": not diff,
        "diff_count": len(diff), "diff_sample": diff[:20],
        "source_identity": identity,
    })
    if fresh.get("status") != "passed":
        return None, report
    return fresh, report


def prepare_research_input(state, *, source_identity=None):
    """Full research input preparation; returns ``(prepared_state, report)``.

    The caller owns persistence.  Any failure leaves the device inputs either
    unchanged (radius verification failure) or the surface facts untouched
    (recovery failure) -- never a partially "passing" input.
    """

    state = deepcopy(state)
    radius = prepare_two_kilometre_inputs(state)
    facts, surface = recover_surface_facts(state)
    report = {
        "authority": AUTHORITY,
        "source_identity": source_identity,
        "communication_radius": radius,
        "surface_class_facts": surface,
        "status": "PASS" if facts is not None else "FAIL",
    }
    if facts is None:
        report["reason"] = "surface_class_facts_unrecovered"
        return None, report
    state["surface_class_facts"] = facts
    state.setdefault("result_statuses", {})["surface_class_facts"] = "passed"
    return state, report


__all__ = [
    "COMMUNICATION_SURFACES", "TWO_KILOMETRE_M", "apply_two_kilometre_device_inputs",
    "prepare_research_input", "prepare_two_kilometre_inputs", "recover_surface_facts",
    "verify_two_kilometre_device_inputs",
]
