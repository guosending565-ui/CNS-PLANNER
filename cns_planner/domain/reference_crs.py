"""Explicit source-CRS vs representation-CRS semantics for reference facts.

A source workbook that does not declare a CRS cannot be assumed to be WGS84 (or
CGCS2000, or anything else).  Two separate questions therefore have to be answered
and recorded independently:

``source_crs``
    What CRS the source file itself declares or what a human confirmed for the
    original data.  Absent evidence keeps this ``pending_confirmation`` forever;
    parsing a CSV/XLSX never creates evidence.

``representation_crs``
    How the in-memory coordinates are currently being interpreted/rendered.  A
    RFC 7946 GeoJSON file does declare ``OGC:CRS84`` for its coordinates, which is
    recorded here — but that only describes the representation, it never proves the
    original survey/source CRS.

Only a *resolved* record may be used for metric measurements.  ``is_resolved`` is
deliberately conservative: it requires ``confirmed`` and a named CRS.
"""

from __future__ import annotations

from copy import deepcopy

SOURCE_CRS_STATUSES = ("pending_confirmation", "confirmed", "unknown", "not_applicable")
REPRESENTATION_CRS_STATUSES = ("declared", "pending_confirmation", "unknown", "not_applicable")

WGS84_GEOGRAPHIC = "EPSG:4326"
CRS84 = "OGC:CRS84"


def empty_crs_record(*, note=None):
    """A fully unresolved CRS record; nothing may be measured against it."""

    return {
        "source_crs": {
            "value": None,
            "status": "pending_confirmation",
            "axis_order": None,
            "confirmed": False,
            "source": None,
            "evidence": [],
        },
        "representation_crs": {
            "value": None,
            "status": "pending_confirmation",
            "axis_order": None,
            "declared_by_format": False,
            "source": None,
            "evidence": [],
        },
        "note": note,
    }


def normalize_crs_record(value, *, default=None):
    """Normalize a CRS record; unknown/absent input stays explicitly unresolved."""

    result = deepcopy(default) if default else empty_crs_record()
    if value is None:
        return result
    if not isinstance(value, dict):
        raise ValueError("crs 必须是对象")
    legacy = str(value.get("crs_status") or "").strip()
    for name in ("source_crs", "representation_crs"):
        raw = value.get(name)
        if raw is None:
            continue
        if not isinstance(raw, dict):
            raise ValueError(f"crs.{name} 必须是对象")
        current = result[name]
        if raw.get("value") is not None:
            current["value"] = str(raw.get("value"))
        if raw.get("status") is not None:
            allowed = SOURCE_CRS_STATUSES if name == "source_crs" else REPRESENTATION_CRS_STATUSES
            status = str(raw["status"]).strip()
            if status not in allowed:
                raise ValueError(f"crs.{name}.status 必须为 {'/'.join(allowed)}")
            current["status"] = status
        if raw.get("axis_order") is not None:
            current["axis_order"] = str(raw["axis_order"])
        if raw.get("source") is not None:
            current["source"] = deepcopy(raw["source"])
        evidence = raw.get("evidence")
        if evidence is not None:
            if not isinstance(evidence, list):
                raise ValueError(f"crs.{name}.evidence 必须是数组")
            current["evidence"] = [deepcopy(item) for item in evidence]
        if name == "source_crs":
            current["confirmed"] = raw.get("confirmed") is True
        else:
            current["declared_by_format"] = raw.get("declared_by_format") is True
    if value.get("note") is not None:
        result["note"] = str(value["note"])
    if legacy and result["source_crs"]["value"] is None and not result["source_crs"]["evidence"]:
        # Legacy projects only ever persisted a pending/unknown status flag.  Keep it
        # visible as evidence instead of upgrading it into a CRS value.
        result["source_crs"]["status"] = legacy if legacy in SOURCE_CRS_STATUSES else "unknown"
        result["source_crs"]["evidence"].append({
            "type": "legacy_crs_status_field",
            "value": legacy,
            "note": "旧字段 crs_status 不含 CRS 值；不得据此推断坐标系。",
        })
    return result


def is_resolved(record, *, role="source_crs"):
    """True only when the named CRS role is confirmed and carries an actual CRS."""

    if not isinstance(record, dict):
        return False
    entry = record.get(role) or {}
    if not entry.get("value"):
        return False
    if role == "source_crs":
        return entry.get("status") == "confirmed" and entry.get("confirmed") is True
    return entry.get("status") in ("confirmed", "declared") and entry.get("value") is not None


def unresolved_reason(record, *, role="source_crs"):
    """Machine-readable reason why the role cannot be measured against yet."""

    if not isinstance(record, dict):
        return "crs_record_missing"
    entry = record.get(role) or {}
    if not entry.get("value"):
        if entry.get("status") == "not_applicable":
            return "crs_not_applicable"
        return "source_crs_pending_confirmation" if role == "source_crs" else "representation_crs_pending"
    if role == "source_crs" and entry.get("confirmed") is not True:
        return "source_crs_value_not_confirmed"
    return None


def resolved_geographic(record, *, role="source_crs"):
    """Return the CRS value only when it is resolved for metric use, else ``None``."""

    if not is_resolved(record, role=role):
        return None
    value = str((record or {}).get(role, {}).get("value") or "").strip()
    return value or None
