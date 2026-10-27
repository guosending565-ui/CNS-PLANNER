"""Authoritative 2D map projection built once, inside the research run.

The projection is embedded in the run manifest.  The browser therefore draws the
route, hosts, planned sites, four N actions and the C/RID/Radar/S/N layers from
the same manifest (and therefore the same run identity and result SHAs) that the
report and stage panels consume.  It never derives a coverage verdict: every
layer verdict is copied from ``manifest.business_state``.
"""
from __future__ import annotations

from copy import deepcopy

PROJECTION_VERSION = "c3b-map-projection-1"
SERVICE_SUBSYSTEM = {
    "C": ("C",),
    "RID": ("S",),
    "Radar": ("S",),
    "S": ("S",),
    "N": ("N",),
}


def _coordinate(value):
    if not (isinstance(value, (list, tuple)) and len(value) >= 2):
        return None
    try:
        lon, lat = float(value[0]), float(value[1])
    except (TypeError, ValueError):
        return None
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        return None
    return [round(lon, 7), round(lat, 7)]


def _radius(device):
    geometry = device.get("coverage_geometry") or {}
    mapping = geometry.get("radius_by_surface")
    if isinstance(mapping, dict) and isinstance(mapping.get("resolved"), dict):
        mapping = mapping["resolved"]
    if isinstance(mapping, dict):
        values = [float(value) for value in mapping.values()
                  if isinstance(value, (int, float)) and float(value) > 0]
        if values:
            return round(max(values), 3), "radius_by_surface"
    for key in ("radius_m", "slant_range_m"):
        value = geometry.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return round(float(value), 3), key
    return None, None


def _site(facility, index):
    metadata = facility.get("metadata") or {}
    profile = facility.get("planning_profile") or {}
    coordinate = _coordinate(facility.get("coordinate"))
    if coordinate is None:
        return None
    devices = []
    for device in facility.get("devices") or []:
        subsystem = str(device.get("subsystem") or device.get("service_key") or "").upper()
        if not subsystem:
            continue
        radius, radius_source = _radius(device)
        devices.append({
            "device_id": device.get("device_id"),
            "service_key": device.get("service_key"),
            "subsystem": subsystem[:1],
            "radius_m": radius,
            "radius_source": radius_source,
            "actual_delivery": "unknown",
        })
    planned_new = metadata.get("new_physical_site") is True
    subsystems = sorted({item["subsystem"] for item in devices})
    return {
        "site_id": facility.get("site_id"),
        "facility_id": facility.get("facility_id"),
        "label": facility.get("name") or facility.get("site_id"),
        "coordinate": coordinate,
        "kind": "planned_new_site" if planned_new else "existing_host",
        "reuse_class": profile.get("reuse_class") or metadata.get("source_identity", {}).get(
            "reuse_class"),
        "subsystems": subsystems,
        "devices": devices,
        "device_count": len(devices),
        "actual_delivery": "unknown",
        "proposal_only": bool(metadata.get("proposal_only")),
        "order": index,
    }


def _action(action, index):
    coordinate = _coordinate(action.get("coordinate"))
    action_id = str(action.get("action_id") or "")
    action_type = str(action.get("action_type") or "")
    kind = ("endpoint" if "endpoint" in action_type
            else "middle" if "middle" in action_type
            else "other")
    return {
        "action_id": action_id,
        "label": action_id,
        "site_id": action.get("site_id"),
        "coordinate": coordinate,
        "action_type": action_type,
        "kind": kind,
        "segment_index": action.get("segment_index"),
        "planner_family": action.get("planner_family"),
        "planning_status": "proposal_only",
        "authority": "research_isolated",
        "actual_delivery": "unknown",
        "order": index,
    }


def _coverage_layer(layer_id, label, sites, status, sites_with_subsystem, *,
                    radar=False):
    features = []
    for site in sites:
        if site["site_id"] not in sites_with_subsystem:
            continue
        radius = next((item["radius_m"] for item in site["devices"]
                       if item["subsystem"] in SERVICE_SUBSYSTEM[layer_id]
                       and item["radius_m"]), None)
        features.append({
            "kind": "coverage_circle",
            "site_id": site["site_id"],
            "label": site["label"],
            "center": site["coordinate"],
            "radius_m": radius,
            "status": status,
            "status_source": "manifest.business_state",
        })
    return {
        "id": layer_id,
        "label": label,
        "status": status,
        "conclusion": "unknown" if status in ("unknown", "not_calculated") else status,
        "features": features,
        "feature_count": len(features),
        "evidence_role": ("input_radius_projection_not_business_result"
                          if not radar else "no_geometry_formal_status_only"),
    }


def build_map_projection(state, p16_result, manifest):
    sites = [item for item in (
        _site(facility, index)
        for index, facility in enumerate(
            (state.get("existing_cns_facilities") or {}).get("items") or [])
    ) if item]
    by_subsystem = {}
    for site in sites:
        for subsystem in site["subsystems"]:
            by_subsystem.setdefault(subsystem, set()).add(site["site_id"])
    n_actions = [item for item in (
        _action(action, index)
        for index, action in enumerate((p16_result or {}).get("selected_actions") or [])
        if "navigation_integrity" in str(action.get("action_id") or "")
        or "navigation_integrity" in str(action.get("planner_family") or "")
    ) if item]
    business = manifest.get("business_state") or {}
    c_status = (business.get("communication_C") or {}).get("verdict") or "unknown"
    rid_status = (business.get("navigation_RID") or {}).get("verdict") or "unknown"
    radar = business.get("surveillance_Radar") or {}
    s_status = (business.get("surveillance_S") or {}).get("verdict") or "unknown"
    n_status = (business.get("navigation_N") or {}).get("verdict") or "unknown"
    route = deepcopy(((state.get("operational_routes") or [None])[0]))
    route_view = None
    if isinstance(route, dict):
        path = [_coordinate(point) for point in route.get("path") or []]
        path = [point for point in path if point]
        route_view = {
            "route_id": route.get("route_id"),
            "label": "R0005 研究投影航路",
            "name": route.get("name"),
            "path": path,
            "start": _coordinate(route.get("start")),
            "end": _coordinate(route.get("end")),
            "altitude_layer_id": route.get("altitude_layer_id"),
            "nominal_altitude_m": route.get("nominal_altitude_m"),
            "operational_route": False,
            "operational_authorization": False,
            "authority": "research_isolated",
        }
    layers = [
        _coverage_layer("C", "Communication C", sites, c_status,
                        by_subsystem.get("C", set())),
        _coverage_layer("RID", "RID cooperative", sites, rid_status,
                        by_subsystem.get("S", set())),
        {
            "id": "Radar", "label": "Radar non-cooperative",
            "status": radar.get("formal_status") or "not_calculated",
            "conclusion": radar.get("verdict") or "unknown",
            "formal_status": radar.get("formal_status") or "not_calculated",
            "reason": radar.get("reason") or "radar_layout_missing",
            "features": [], "feature_count": 0,
            "evidence_role": "no_layout_no_geometry",
            "display_rule": "unknown/not_calculated_never_satisfied",
        },
        _coverage_layer("S", "Surveillance S（RID + Radar）", sites, s_status,
                        by_subsystem.get("S", set())),
        {
            "id": "N", "label": "Navigation N actions",
            "status": n_status, "conclusion": "unknown" if n_status == "unknown" else n_status,
            "features": [{"kind": "n_action", **action} for action in n_actions],
            "feature_count": len(n_actions),
            "evidence_role": "p16_selected_actions_proposal_only",
            "display_rule": "proposal_only_never_installed",
        },
    ]
    return {
        "schema_version": PROJECTION_VERSION,
        "run_id": manifest.get("run_id"),
        "route": route_view,
        "sites": sites,
        "n_actions": n_actions,
        "layers": layers,
        "counts": {
            "sites": len(sites),
            "devices": sum(site["device_count"] for site in sites),
            "existing_hosts": sum(site["kind"] == "existing_host" for site in sites),
            "planned_new_sites": sum(site["kind"] == "planned_new_site" for site in sites),
            "n_actions": len(n_actions),
        },
        "authority": "research_isolated",
        "actual_delivery": "unknown",
        "semantics": (
            "projection of the frozen run input plus manifest statuses; coverage rings are "
            "input-radius display, not a re-derived business verdict"),
    }


__all__ = ["PROJECTION_VERSION", "build_map_projection"]
