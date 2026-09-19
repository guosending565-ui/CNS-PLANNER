"""JSON-safe contracts for the additive ``regulatory_constraints`` interface.

This is a **new, additive** contract.  It deliberately does *not* reuse the current
``display_only`` airspace product, and the current airspace product stays exactly what it
is: a display-only reference layer that never enters the search, a hard gate or any
optimization fingerprint.

What this module ships today:

* the constraint schema (``constraint_id`` / ``constraint_type`` / ``geometry`` /
  ``vertical_scope`` / ``source`` / ``evidence`` / ``confirmed`` / ``status``);
* a conservative horizontal geometry test (polygon or bbox) and a vertical-scope test
  against the selected fixed cruise altitude ``H``;
* a fail-closed resolver: an unconfigured dataset is ``not_configured`` (planning may
  run, but the candidate must record ``regulatory_compliance = not_evaluated``), and a
  *configured* constraint whose evidence is unresolved is never treated as safe.

Boundaries:

* no real regulatory dataset exists in this project yet, so nothing is fabricated here.
  ``default_regulatory_constraints()`` ships ``not_configured`` with zero constraints;
* "the route does not cross a confirmed no-fly polygon" is **not** a statement of
  regulatory compliance, and no output of this module ever claims it is.
"""

from __future__ import annotations

from copy import deepcopy
import math
from numbers import Real

from .layered_route import stable_fingerprint

SCHEMA_VERSION = "regulatory-constraints-v1"

CONSTRAINT_TYPES = ("no_fly_zone", "restricted_zone", "corridor_reservation")

#: ``confirmed`` is the only status that can block; everything else is never "safe".
CONSTRAINT_STATUSES = ("confirmed", "pending_confirmation", "unresolved", "revoked")

NOT_CONFIGURED = "not_configured"

VERTICAL_SCOPE_REFERENCES = ("egm2008_orthometric", "agl", "unknown")

#: Result vocabulary of the search-time evaluation.
REGULATORY_COMPLIANCE_STATES = (
    "not_evaluated", "no_confirmed_constraint_intersected", "blocked_by_confirmed_no_fly_zone",
    "unresolved_constraint_evidence",
)


def _finite(value):
    return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(float(value))


def _optional_number(value):
    return float(value) if _finite(value) else None


def default_regulatory_constraints():
    """No regulatory dataset is configured: planning may run, compliance is not evaluated."""

    record = {
        "schema_version": SCHEMA_VERSION,
        "status": NOT_CONFIGURED,
        "status_reason": "no_confirmed_regulatory_constraint_dataset_configured",
        "source": None,
        "evidence": None,
        "confirmed": False,
        "count": 0,
        "items": [],
        "dataset_fingerprint": None,
        "provenance": {
            "interface": "additive_regulatory_constraints",
            "reuses_display_only_airspace": False,
            "display_only_airspace_is_a_search_input": False,
            "real_dataset_configured": False,
        },
        "semantics": {
            "interface_only_no_real_data_yet": True,
            "unconfigured_means_compliance_not_evaluated": True,
            "unconfigured_never_means_compliant": True,
            "unresolved_evidence_is_never_safe": True,
            "confirmed_polygon_intersection_blocks_los": True,
            "horizontal_geometry_is_polygon_or_bbox": True,
            "display_only_airspace_is_not_reused": True,
        },
    }
    # An unconfigured dataset carries a *constant* fingerprint: declaring the interface does
    # not by itself change any candidate's optimization fingerprint.
    record["dataset_fingerprint"] = regulatory_constraints_fingerprint(record)
    return record


def normalize_regulatory_constraints(value):
    if value in (None, ""):
        return default_regulatory_constraints()
    if not isinstance(value, dict):
        raise ValueError("regulatory_constraints 必须是对象")
    source = value
    result = default_regulatory_constraints()
    items = []
    for index, raw in enumerate(source.get("items") or []):
        items.append(normalize_regulatory_constraint(raw, index=index))
    result.update({
        "status": str(source.get("status") or (NOT_CONFIGURED if not items else "configured")),
        "source": source.get("source"),
        "evidence": source.get("evidence") if isinstance(source.get("evidence"), dict) else None,
        "confirmed": bool(source.get("confirmed")),
        "items": items,
        "count": len(items),
    })
    if not items:
        result["status"] = NOT_CONFIGURED
        result["status_reason"] = "no_confirmed_regulatory_constraint_dataset_configured"
    else:
        result["status_reason"] = None
    result["dataset_fingerprint"] = regulatory_constraints_fingerprint(result)
    return result


def normalize_regulatory_constraint(value, *, index=0):
    raw = value if isinstance(value, dict) else {}
    constraint_type = str(raw.get("constraint_type") or "no_fly_zone")
    if constraint_type not in CONSTRAINT_TYPES:
        raise ValueError(f"未知 regulatory constraint_type：{constraint_type!r}")
    geometry = _normalize_geometry(raw.get("geometry"))
    vertical = _normalize_vertical_scope(raw.get("vertical_scope"))
    status = str(raw.get("status") or "pending_confirmation")
    if status not in CONSTRAINT_STATUSES:
        raise ValueError(f"未知 regulatory constraint status：{status!r}")
    return {
        "constraint_id": str(raw.get("constraint_id") or f"RC-{index + 1:04d}"),
        "constraint_type": constraint_type,
        "geometry": geometry,
        "vertical_scope": vertical,
        "source": raw.get("source"),
        "evidence": raw.get("evidence") if isinstance(raw.get("evidence"), dict) else None,
        "confirmed": bool(raw.get("confirmed")),
        "status": status,
        "authority": raw.get("authority"),
        "effective_from": raw.get("effective_from"),
        "effective_to": raw.get("effective_to"),
        "notes": list(raw.get("notes") or []),
        "provenance": {
            "contract": "regulatory_constraints",
            "not_display_only_airspace": True,
            "confirmed_requires_explicit_source": True,
            "geometry_kind": geometry.get("kind") if geometry else None,
        },
    }


def _normalize_geometry(value):
    """``polygon`` (list of ``[lon, lat]`` rings) or ``bbox`` (``[w, s, e, n]``)."""

    raw = value if isinstance(value, dict) else {}
    kind = str(raw.get("kind") or "")
    if kind == "bbox" or (not kind and isinstance(raw.get("bbox"), (list, tuple))):
        bbox = raw.get("bbox")
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            return None
        values = [_optional_number(item) for item in bbox]
        if any(item is None for item in values) or values[0] >= values[2] or values[1] >= values[3]:
            return None
        return {"kind": "bbox", "bbox": values}
    if kind == "polygon" or (not kind and raw.get("polygon")):
        rings = []
        for ring in raw.get("polygon") or []:
            points = [
                [float(point[0]), float(point[1])]
                for point in ring or []
                if isinstance(point, (list, tuple)) and len(point) >= 2
                and _finite(point[0]) and _finite(point[1])
            ]
            if len(points) >= 3:
                rings.append(points)
        if not rings:
            return None
        return {
            "kind": "polygon", "polygon": rings,
            "bbox": polygon_bbox(rings[0]),
        }
    return None


def _normalize_vertical_scope(value):
    raw = value if isinstance(value, dict) else {}
    reference = str(raw.get("vertical_reference") or "unknown")
    if reference not in VERTICAL_SCOPE_REFERENCES:
        reference = "unknown"
    return {
        "vertical_reference": reference,
        "lower_egm2008_m": _optional_number(raw.get("lower_egm2008_m")),
        "upper_egm2008_m": _optional_number(raw.get("upper_egm2008_m")),
        "unbounded": bool(raw.get("unbounded")),
        "source": raw.get("source"),
    }


def polygon_bbox(ring):
    xs = [point[0] for point in ring]
    ys = [point[1] for point in ring]
    return [min(xs), min(ys), max(xs), max(ys)]


def regulatory_constraints_fingerprint(value):
    """Fingerprint of a **configured** regulatory dataset.

    An unconfigured dataset carries ``items == []`` and a constant fingerprint, so
    planning with and without an empty interface produces the identical optimization
    fingerprint — the interface exists but is not yet a planning input.
    """

    item = value if isinstance(value, dict) else {}
    return stable_fingerprint({
        "schema_version": SCHEMA_VERSION,
        "status": item.get("status"),
        "confirmed": bool(item.get("confirmed")),
        "source": item.get("source"),
        "items": [
            {
                "constraint_id": entry.get("constraint_id"),
                "constraint_type": entry.get("constraint_type"),
                "geometry": entry.get("geometry"),
                "vertical_scope": entry.get("vertical_scope"),
                "source": entry.get("source"),
                "confirmed": bool(entry.get("confirmed")),
                "status": entry.get("status"),
            }
            for entry in sorted(
                item.get("items") or [], key=lambda entry: str(entry.get("constraint_id"))
            )
        ],
    }, prefix="regulatoryv1-")


def is_configured(value):
    item = value if isinstance(value, dict) else {}
    return bool(item.get("items"))


# --------------------------------------------------------------------------- geometry


def point_in_ring(point, ring):
    """Even-odd ray casting.  Boundary contact counts as inside (conservative)."""

    lon, lat = float(point[0]), float(point[1])
    inside = False
    count = len(ring)
    for index in range(count):
        x1, y1 = ring[index][0], ring[index][1]
        x2, y2 = ring[(index + 1) % count][0], ring[(index + 1) % count][1]
        if _on_segment(lon, lat, x1, y1, x2, y2):
            return True
        if (y1 > lat) != (y2 > lat):
            crossing = (x2 - x1) * (lat - y1) / (y2 - y1) + x1
            if lon < crossing:
                inside = not inside
    return inside


def _on_segment(x, y, x1, y1, x2, y2, tolerance=1e-12):
    cross = (x2 - x1) * (y - y1) - (y2 - y1) * (x - x1)
    if abs(cross) > tolerance * max(1.0, abs(x2 - x1) + abs(y2 - y1)):
        return False
    return (
        min(x1, x2) - tolerance <= x <= max(x1, x2) + tolerance
        and min(y1, y2) - tolerance <= y <= max(y1, y2) + tolerance
    )


def segment_intersects_ring(start, end, ring):
    """Conservative: an endpoint inside, or any edge crossing, counts as intersecting."""

    if point_in_ring(start, ring) or point_in_ring(end, ring):
        return True
    count = len(ring)
    for index in range(count):
        edge_start = ring[index]
        edge_end = ring[(index + 1) % count]
        if _segments_intersect(start, end, edge_start, edge_end):
            return True
    return False


def _segments_intersect(a, b, c, d):
    def orient(p, q, r):
        value = (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
        if abs(value) <= 1e-14:
            return 0
        return 1 if value > 0 else -1

    def on(p, q, r):
        return (
            min(p[0], r[0]) - 1e-12 <= q[0] <= max(p[0], r[0]) + 1e-12
            and min(p[1], r[1]) - 1e-12 <= q[1] <= max(p[1], r[1]) + 1e-12
        )

    o1, o2 = orient(a, b, c), orient(a, b, d)
    o3, o4 = orient(c, d, a), orient(c, d, b)
    if o1 != o2 and o3 != o4:
        return True
    if o1 == 0 and on(a, c, b):
        return True
    if o2 == 0 and on(a, d, b):
        return True
    if o3 == 0 and on(c, a, d):
        return True
    if o4 == 0 and on(c, b, d):
        return True
    return False


def segment_intersects_constraint(start, end, constraint):
    """Horizontal intersection of a segment with one constraint geometry."""

    geometry = (constraint or {}).get("geometry") or {}
    if geometry.get("kind") == "bbox":
        bbox = geometry.get("bbox")
        if not bbox:
            return False
        return _segment_intersects_bbox(start, end, bbox)
    for ring in geometry.get("polygon") or []:
        if segment_intersects_ring(start, end, ring):
            return True
    return False


def _segment_intersects_bbox(start, end, bbox):
    west, south, east, north = (float(value) for value in bbox)
    box = [[west, south], [east, south], [east, north], [west, north]]
    return segment_intersects_ring(start, end, box)


def vertical_scope_contains(constraint, altitude_egm2008_m):
    """``True`` / ``False`` / ``None`` (unresolved) for a fixed cruise altitude ``H``."""

    scope = (constraint or {}).get("vertical_scope") or {}
    if not _finite(altitude_egm2008_m):
        return None
    altitude = float(altitude_egm2008_m)
    if scope.get("unbounded"):
        return True
    if str(scope.get("vertical_reference") or "unknown") != "egm2008_orthometric":
        # No datum conversion is ever guessed for a regulatory scope.
        return None
    lower = scope.get("lower_egm2008_m")
    upper = scope.get("upper_egm2008_m")
    if lower is None and upper is None:
        return None
    if lower is not None and altitude < float(lower):
        return False
    if upper is not None and altitude > float(upper):
        return False
    return True


def evaluate_regulatory_intersection(constraints, *, start, end, altitude_egm2008_m):
    """Evaluation of one LOS shortcut against the (possibly unconfigured) constraints.

    Returns ``status`` in :data:`REGULATORY_COMPLIANCE_STATES` plus the matched
    constraint ids.  Only a ``confirmed`` constraint blocks; a configured but unresolved
    constraint is reported separately and is **never** treated as safe.
    """

    item = constraints if isinstance(constraints, dict) else default_regulatory_constraints()
    result = {
        "status": "not_evaluated",
        "evaluated": False,
        "blocked": False,
        "blocked_by": [],
        "unresolved": [],
        "skipped_not_intersecting": [],
        "altitude_egm2008_m": _optional_number(altitude_egm2008_m),
    }
    if not is_configured(item):
        result["status"] = "not_evaluated"
        result["reason"] = "regulatory_constraints_not_configured"
        return result
    result["evaluated"] = True
    blocked, unresolved, skipped = [], [], []
    for constraint in item.get("items") or []:
        if not isinstance(constraint, dict):
            continue
        constraint_id = str(constraint.get("constraint_id"))
        if not segment_intersects_constraint(start, end, constraint):
            skipped.append(constraint_id)
            continue
        contains = vertical_scope_contains(constraint, altitude_egm2008_m)
        if str(constraint.get("status")) != "confirmed" or not constraint.get("confirmed"):
            unresolved.append({
                "constraint_id": constraint_id,
                "reason": "constraint_not_confirmed",
                "status": constraint.get("status"),
            })
            continue
        if contains is None:
            unresolved.append({
                "constraint_id": constraint_id,
                "reason": "vertical_scope_unresolved",
                "status": constraint.get("status"),
            })
            continue
        if contains:
            blocked.append(constraint_id)
        else:
            skipped.append(constraint_id)
    result.update({
        "blocked_by": sorted(blocked),
        "unresolved": unresolved,
        "skipped_not_intersecting": sorted(skipped),
        "blocked": bool(blocked),
        "status": (
            "blocked_by_confirmed_no_fly_zone" if blocked
            else "unresolved_constraint_evidence" if unresolved
            else "no_confirmed_constraint_intersected"
        ),
    })
    return result


def regulatory_compliance_record(constraints):
    """Candidate-level compliance record.  Never claims compliance when not evaluated."""

    item = constraints if isinstance(constraints, dict) else default_regulatory_constraints()
    configured = is_configured(item)
    return {
        "status": "evaluated" if configured else "not_evaluated",
        "regulatory_compliance": (
            "evaluated_against_configured_constraints" if configured else "not_evaluated"
        ),
        "constraint_dataset_status": item.get("status"),
        "constraint_count": int(item.get("count") or 0),
        "confirmed_constraint_count": sum(
            1 for entry in item.get("items") or []
            if isinstance(entry, dict) and entry.get("confirmed")
            and str(entry.get("status")) == "confirmed"
        ),
        "dataset_fingerprint": item.get("dataset_fingerprint"),
        "statement": (
            "已按已配置的 regulatory_constraints 评估航路是否与已确认禁飞多边形水平相交；"
            "这不构成法规符合性结论。"
            if configured else
            "未配置任何 regulatory constraint 数据集：本轮 regulatory_compliance=not_evaluated，"
            "不得表述为“符合禁飞要求”。"
        ),
        "semantics": {
            "not_a_regulatory_compliance_verdict": True,
            "unconfigured_is_not_evaluated_not_passed": True,
            "unresolved_evidence_is_never_safe": True,
            "display_only_airspace_not_used": True,
        },
    }


__all__ = [
    "CONSTRAINT_STATUSES", "CONSTRAINT_TYPES", "NOT_CONFIGURED",
    "REGULATORY_COMPLIANCE_STATES", "SCHEMA_VERSION", "VERTICAL_SCOPE_REFERENCES",
    "default_regulatory_constraints", "evaluate_regulatory_intersection", "is_configured",
    "normalize_regulatory_constraint", "normalize_regulatory_constraints",
    "point_in_ring", "polygon_bbox", "regulatory_compliance_record",
    "regulatory_constraints_fingerprint", "segment_intersects_constraint",
    "segment_intersects_ring", "vertical_scope_contains",
]
