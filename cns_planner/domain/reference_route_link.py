"""Explicit, user-confirmed links between reference routes and scenario routes.

A reference route is source *fact*: the system may compute an endpoint-distance
candidate hint once the reference CRS is resolved, but it must never decide that a
reference route "is" a planned OD.  Only a user confirmation creates a link, and the
link records who/what provided the evidence for it.

Links are additive project state and never feed the planners.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import re

LINK_COLLECTION_ID = "reference-route-links"
LINK_SCHEMA_VERSION = 1

#: Candidate hints are advisory only.  They never become links automatically.
CANDIDATE_STATES = ("suggested", "rejected")
LINK_ORIGINS = ("user", "imported")

_LINK_ID = re.compile(r"^RRL-[0-9A-F]{12}$")

CANDIDATE_DISTANCE_TOLERANCE_NOTE = (
    "endpoint-distance 只是候选提示，必须由用户确认后才成为 link；系统不得自动认定。"
)


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def empty_reference_route_links():
    return {
        "status": "not_calculated",
        "collection_id": LINK_COLLECTION_ID,
        "schema_version": LINK_SCHEMA_VERSION,
        "count": 0,
        "items": [],
        "note": (
            "reference route ↔ scenario/OD 关联必须由用户显式确认；"
            "系统只提供候选提示，不自动建立关联。"
        ),
    }


def _hash(value):
    return sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def _required_text(value, field):
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"ReferenceRouteLink.{field} 缺失")
    return text


def normalize_reference_route_link(raw):
    if not isinstance(raw, dict):
        raise ValueError("ReferenceRouteLink 必须是对象")
    reference_route_id = _required_text(raw.get("reference_route_id"), "reference_route_id")
    scenario_route_id = _required_text(raw.get("scenario_route_id"), "scenario_route_id")
    if raw.get("confirmed") is not True:
        raise ValueError("ReferenceRouteLink 必须显式 confirmed=true；候选提示不得作为关联")
    source = deepcopy(raw.get("source"))
    if source in (None, "", {}, []):
        raise ValueError("ReferenceRouteLink.source 缺失")
    origin = str(raw.get("origin") or "user")
    if origin not in LINK_ORIGINS:
        raise ValueError("ReferenceRouteLink.origin 必须为 user/imported")
    evidence = raw.get("evidence")
    if evidence is not None and not isinstance(evidence, list):
        raise ValueError("ReferenceRouteLink.evidence 必须是数组")
    identity = _hash({
        "reference_route_id": reference_route_id,
        "scenario_route_id": scenario_route_id,
        "origin": origin,
        "source": source,
    })
    link_id = str(raw.get("link_id") or f"RRL-{identity[:12].upper()}")
    if not _LINK_ID.match(link_id):
        raise ValueError(f"ReferenceRouteLink.link_id 无效：{link_id}")
    return {
        "link_id": link_id,
        "reference_route_id": reference_route_id,
        "scenario_route_id": scenario_route_id,
        "start_node_id": raw.get("start_node_id"),
        "end_node_id": raw.get("end_node_id"),
        "confirmed": True,
        "confirmed_at": str(raw.get("confirmed_at") or utc_now()),
        "origin": origin,
        "source": source,
        "evidence": [deepcopy(item) for item in (evidence or [])],
        "note": None if raw.get("note") in (None, "") else str(raw.get("note")),
        "candidate_hint": deepcopy(raw.get("candidate_hint")),
        "usage": "explicit_reference_route_association_for_review_only",
    }


def endpoint_candidate(
    reference_route_id, scenario_route_id, *, start_offset_m, end_offset_m,
    reference_crs, scenario_crs, method, threshold_m,
):
    """Advisory, non-binding hint that two routes may describe the same OD pair.

    Returns ``None`` unless both endpoint offsets are finite numbers, so an
    unresolved CRS can never silently produce a candidate.
    """

    values = (start_offset_m, end_offset_m)
    if any(value is None or not isinstance(value, (int, float)) for value in values):
        return None
    within = all(float(value) <= float(threshold_m) for value in values)
    return {
        "reference_route_id": str(reference_route_id),
        "scenario_route_id": str(scenario_route_id),
        "state": "suggested" if within else "rejected",
        "start_offset_m": float(start_offset_m),
        "end_offset_m": float(end_offset_m),
        "threshold_m": float(threshold_m),
        "reference_crs": deepcopy(reference_crs),
        "scenario_crs": deepcopy(scenario_crs),
        "method": method,
        "requires_user_confirmation": True,
        "note": CANDIDATE_DISTANCE_TOLERANCE_NOTE,
    }


def normalize_reference_route_links(value):
    result = empty_reference_route_links()
    if value is None:
        return result
    if isinstance(value, list):
        items = value
    elif isinstance(value, dict):
        items = value.get("items") or []
    else:
        raise ValueError("reference_route_links 必须是对象或数组")
    normalized, seen = [], set()
    for raw in items:
        item = normalize_reference_route_link(raw)
        key = (item["reference_route_id"], item["scenario_route_id"])
        if key in seen:
            raise ValueError(
                f"ReferenceRouteLink 重复：{item['reference_route_id']} ↔ {item['scenario_route_id']}"
            )
        seen.add(key)
        normalized.append(item)
    result["items"] = normalized
    result["count"] = len(normalized)
    result["status"] = "passed" if normalized else "not_calculated"
    return result


def endpoint_candidates(reference_routes, scenario_routes, links, *, threshold_m=500.0):
    """Candidate hints for every unlinked reference/scenario pair (advisory only).

    Requires resolved CRS on both sides; otherwise returns an explicit
    ``blocked`` entry explaining the missing prerequisite instead of a guess.
    """

    from ..benchmark.geodesy import geodesic_distance_m

    linked = {
        (item["reference_route_id"], item["scenario_route_id"]) for item in links or []
    }
    candidates = []
    for reference in reference_routes or []:
        reference_path = reference.get("path") or []
        if len(reference_path) < 2:
            continue
        for scenario in scenario_routes or []:
            if (reference.get("reference_route_id"), scenario.get("route_id")) in linked:
                continue
            scenario_path = [scenario.get("start"), scenario.get("end")]
            if any(point is None for point in scenario_path):
                continue
            candidates.append(endpoint_candidate(
                reference.get("reference_route_id"), scenario.get("route_id"),
                start_offset_m=geodesic_distance_m(reference_path[0], scenario_path[0]),
                end_offset_m=geodesic_distance_m(reference_path[-1], scenario_path[1]),
                reference_crs=reference.get("crs"),
                scenario_crs={"source_crs": {"value": "EPSG:4326"}},
                method="geodesic_endpoint_offset_m",
                threshold_m=threshold_m,
            ))
    return [item for item in candidates if item is not None]


def blocked_endpoint_candidates(reference_routes, reason):
    """Explain why candidate hints are unavailable (never fabricate them)."""

    return {
        "status": "blocked",
        "reason": reason,
        "candidate_count": 0,
        "candidates": [],
        "requires_user_confirmation": True,
        "note": CANDIDATE_DISTANCE_TOLERANCE_NOTE,
        "reference_route_count": len(reference_routes or []),
    }
