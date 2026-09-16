"""Airspace source facts and explicit route-eligibility policies."""

from __future__ import annotations

from copy import deepcopy


ELIGIBILITY_VALUES = {"allowed", "blocked", "unknown"}


def normalize_airspace_feature(value):
    if not isinstance(value, dict):
        raise ValueError("AirspaceFeature 必须是对象")
    feature_id = str(value.get("feature_id") or "").strip()
    geometry = deepcopy(value.get("geometry"))
    if not feature_id:
        raise ValueError("AirspaceFeature.feature_id 缺失")
    if not isinstance(geometry, dict) or geometry.get("type") not in ("Polygon", "MultiPolygon"):
        raise ValueError("AirspaceFeature.geometry 必须是 Polygon/MultiPolygon")
    return {
        "feature_id": feature_id,
        "geometry": geometry,
        "category": value.get("category"),
        "type": value.get("type"),
        "lower_altitude": value.get("lower_altitude"),
        "upper_altitude": value.get("upper_altitude"),
        "vertical_reference": value.get("vertical_reference"),
        "valid_time": deepcopy(value.get("valid_time")),
        "crs": deepcopy(value.get("crs")),
        "source": deepcopy(value.get("source")),
        "provenance": deepcopy(value.get("provenance")),
    }


def empty_airspace_policies():
    return {
        "status": "pending_confirmation",
        "collection_id": "airspace-policies",
        "count": 0,
        "items": [],
    }


def normalize_airspace_policies(value):
    """Keep policy semantics explicit; never infer eligibility from layer styling."""
    result = empty_airspace_policies()
    if value is None:
        return result
    if isinstance(value, list):
        items = value
    elif isinstance(value, dict):
        items = value.get("items") or []
        result.update({key: deepcopy(current) for key, current in value.items() if key != "items"})
    else:
        raise ValueError("airspace_policies 必须是对象或数组")
    normalized = []
    seen = set()
    for raw in items:
        if not isinstance(raw, dict):
            raise ValueError("AirspacePolicy 必须是对象")
        feature_id = str(raw.get("feature_id") or "").strip()
        if not feature_id:
            raise ValueError("AirspacePolicy.feature_id 缺失")
        eligibility = str(raw.get("route_eligibility") or "unknown").strip().lower()
        if eligibility not in ELIGIBILITY_VALUES:
            raise ValueError("route_eligibility 必须为 allowed/blocked/unknown")
        if feature_id in seen:
            raise ValueError(f"AirspacePolicy.feature_id 重复：{feature_id}")
        source = deepcopy(raw.get("source"))
        if source in (None, ""):
            raise ValueError("AirspacePolicy.source 缺失")
        seen.add(feature_id)
        normalized.append({
            "feature_id": feature_id,
            "route_eligibility": eligibility,
            "confirmed": raw.get("confirmed") is True,
            "source": source,
        })
    result["items"] = normalized
    result["count"] = len(normalized)
    result["status"] = "passed" if normalized else "pending_confirmation"
    return result
