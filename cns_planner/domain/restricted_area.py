"""Auditable restricted-area / protected-site domain contract."""

from __future__ import annotations

from copy import deepcopy


CONSTRAINT_TYPES = ("hard_exclusion", "conditional", "advisory")
GEOMETRY_TYPES = ("Point", "Polygon", "MultiPolygon")
DATASET_STATES = ("not_provided", "confirmed_none", "confirmed_present", "unresolved")


def normalize_restricted_area(value):
    if not isinstance(value, dict):
        raise ValueError("RestrictedArea 必须是对象")
    feature_id = str(value.get("feature_id") or "").strip()
    if not feature_id:
        raise ValueError("RestrictedArea.feature_id 缺失")
    constraint_type = str(value.get("constraint_type") or "").strip()
    if constraint_type not in CONSTRAINT_TYPES:
        raise ValueError("RestrictedArea.constraint_type 必须为 hard_exclusion/conditional/advisory")
    geometry = deepcopy(value.get("geometry"))
    geometry_type = str((geometry or {}).get("type") or "") if isinstance(geometry, dict) else ""
    if geometry_type not in GEOMETRY_TYPES:
        raise ValueError("RestrictedArea.geometry 必须为 Point/Polygon/MultiPolygon GeoJSON")
    protection = deepcopy(value.get("protection_geometry"))
    if protection is not None:
        protection_type = str((protection or {}).get("type") or "")
        if protection_type not in ("Polygon", "MultiPolygon"):
            raise ValueError("protection_geometry 必须为 Polygon/MultiPolygon")
    lower = _number(value.get("lower_altitude_m"))
    upper = _number(value.get("upper_altitude_m"))
    if lower is not None and upper is not None and lower > upper:
        raise ValueError("RestrictedArea 垂向下限不能高于上限")
    source = deepcopy(value.get("source"))
    evidence = deepcopy(value.get("evidence") or [])
    confirmed = bool(value.get("confirmed") is True and source and evidence)
    final_geometry = protection or (geometry if geometry_type in ("Polygon", "MultiPolygon") else None)
    geometry_status = (
        "resolved" if final_geometry is not None else
        "source_point_only_no_protection_geometry" if geometry_type == "Point" else "unknown"
    )
    vertical_unbounded = value.get("vertical_unbounded") is True
    vertical_status = (
        "resolved"
        if value.get("vertical_reference") and (
            lower is not None or upper is not None or vertical_unbounded
        )
        else "unknown"
    )
    return {
        "feature_id": feature_id,
        "name": str(value.get("name") or feature_id),
        "category": str(value.get("category") or "unspecified"),
        "domain": str(value.get("domain") or "critical_site"),
        "geometry": geometry,
        "geometry_crs": str(value.get("geometry_crs") or "").strip() or None,
        "protection_geometry": protection,
        "planning_geometry": final_geometry,
        "geometry_status": geometry_status,
        "constraint_type": constraint_type,
        "lower_altitude_m": lower,
        "upper_altitude_m": upper,
        "vertical_reference": str(value.get("vertical_reference") or "").strip() or None,
        "vertical_unbounded": vertical_unbounded,
        "vertical_status": vertical_status,
        "source": source,
        "evidence": evidence,
        "confirmed": confirmed,
    }


def normalize_restricted_area_collection(value=None):
    """Normalize dataset knowledge without interpreting an empty list as confirmed-none."""

    raw = value if isinstance(value, dict) else {}
    items = [
        normalize_restricted_area(item)
        for item in raw.get("items") or [] if isinstance(item, dict)
    ]
    source = deepcopy(raw.get("source"))
    evidence = deepcopy(raw.get("evidence") or [])
    authority_ready = bool(source and evidence)
    domain_states = {}
    supplied = raw.get("domain_states") if isinstance(raw.get("domain_states"), dict) else {}
    for domain in ("airspace", "critical_site"):
        state = supplied.get(domain)
        if isinstance(state, dict):
            state_source = state.get("source") or source
            state_evidence = state.get("evidence") or evidence
            status = str(state.get("status") or "not_provided")
            ready = bool(state_source and state_evidence)
        else:
            state_source, state_evidence = source, evidence
            status, ready = str(state or "not_provided"), authority_ready
        if status not in DATASET_STATES:
            status = "unresolved"
        if status in ("confirmed_none", "confirmed_present") and not ready:
            status = "unresolved"
        domain_states[domain] = {
            "status": status, "source": deepcopy(state_source),
            "evidence": deepcopy(state_evidence or []),
        }
    return {
        "schema_version": 1, "status": "resolved" if all(
            item["status"] in ("confirmed_none", "confirmed_present")
            for item in domain_states.values()
        ) else "unresolved",
        "source": source, "evidence": evidence, "domain_states": domain_states,
        "count": len(items), "items": items,
    }


def altitude_applicability(area, *, altitude_m, vertical_reference):
    """Return applicable/not_applicable/unknown without inventing a vertical scope."""

    item = area if isinstance(area, dict) else {}
    if item.get("vertical_status") != "resolved":
        return "unknown"
    if str(item.get("vertical_reference") or "") != str(vertical_reference or ""):
        return "unknown"
    lower, upper = item.get("lower_altitude_m"), item.get("upper_altitude_m")
    if lower is None and upper is None and item.get("vertical_unbounded") is not True:
        return "unknown"
    if lower is not None and float(altitude_m) < float(lower):
        return "not_applicable"
    if upper is not None and float(altitude_m) > float(upper):
        return "not_applicable"
    return "applicable"


def _number(value):
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


__all__ = [
    "CONSTRAINT_TYPES", "DATASET_STATES", "GEOMETRY_TYPES", "altitude_applicability",
    "normalize_restricted_area", "normalize_restricted_area_collection",
]
