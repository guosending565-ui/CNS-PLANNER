"""Read-only operational polyline obstacle evidence; no candidate or state writer."""
from copy import deepcopy
from hashlib import sha256
import json
from math import hypot

from ..risk.operational_route_profile import (
    DEFAULT_SAMPLING_SPACING_M,
    assess_operational_route_risk,
)
from ..validation.continuous_validators import (
    MetricRoute, validate_terrain, validate_buildings, validate_towers,
)

CONTRACT_VERSION = "operational-route-clearance-v2"


def fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                             allow_nan=False).encode("utf-8")).hexdigest()


def revalidate_existing_route(state, *, route_id, altitude_layer_id,
                              expected_geometry_fingerprint, evidence, policy,
                              source_identity, to_geographic=None,
                              grid=None, grid_risk_v2=None, profile_policy=None,
                              sampling_spacing_m=DEFAULT_SAMPLING_SPACING_M):
    """Return independent research evidence, preserving original authorization.

    Evidence is supplied by native production adapters.  ``risk`` is the independent
    existing-route risk profile: it consumes the original polyline and the current
    ``grid_risk_v2`` only, never a candidate / search cost.  PCF readiness and
    operational admission remain outside this evidence contract.
    """
    routes = [r for r in state.get("operational_routes", []) if r.get("route_id") == route_id]
    if len(routes) != 1:
        raise ValueError("需要唯一既有航路")
    route = deepcopy(routes[0])
    path = route.get("path")
    if not path or len(path) < 2 or fingerprint(path) != expected_geometry_fingerprint:
        raise ValueError("原始航路几何缺失或指纹变化")
    layers = state["spatial_3d"]["altitude_layers"]
    if isinstance(layers, dict):
        layers = layers.get("items", [])
    layer = next(l for l in layers if l.get("layer_id", l.get("altitude_layer_id")) == altitude_layer_id)
    from ..domain.layered_route import resolve_cruise_altitude
    cruise = resolve_cruise_altitude(layer)
    if cruise.get("status") != "confirmed":
        raise ValueError("高度层必须为已确认 EGM2008 正高")
    z = float(layer["nominal_altitude_m"])
    points = evidence["metric_path"]
    if len(points) != len(path):
        raise ValueError("必须保留全部原始顶点")
    if to_geographic is not None:
        for expected, point in zip(path, points):
            actual = to_geographic(point)
            if any(abs(float(a)-float(b)) > 1e-8 for a, b in zip(expected, actual)):
                raise ValueError("投影几何与原始折线不一致")
    length = sum(hypot(b[0]-a[0], b[1]-a[1]) for a, b in zip(points, points[1:]))
    metric = MetricRoute({"horizontal_geometry": {"linearized": {
        "linestring_metric": points, "curve_chord_error_m": 0.0}},
        "primitives": [{"distance_start_m": 0, "distance_end_m": length,
                        "z_start_egm2008_m": z, "z_end_egm2008_m": z}]})
    domains = {}
    for name, validator in (("terrain", validate_terrain), ("buildings", validate_buildings),
                            ("towers", validate_towers)):
        domains[name] = validator(metric, evidence=deepcopy(evidence.get(name, {"available": False,
            "status": "unresolved", "towers": []})), policy=policy, to_geographic=to_geographic)
    risk = assess_operational_route_risk(
        route=route, metric_path=points, altitude_layer_id=altitude_layer_id,
        grid=grid, grid_risk_v2=grid_risk_v2, profile_policy=profile_policy,
        to_geographic=to_geographic, sampling_spacing_m=sampling_spacing_m,
    )
    identity = {"contract": CONTRACT_VERSION, "subject": route, "layer": layer,
                "policy": policy, "sources": source_identity, "evidence": evidence,
                "risk": risk.get("identity")}
    obstacle_blocked = any(d['violations'] for d in domains.values())
    risk_blocked = risk.get("status") == "inconsistent_evidence"
    obstacle_unknown = any(d['status'] != 'passed' for d in domains.values())
    risk_unknown = risk.get("status") != "passed"
    if obstacle_blocked or risk_blocked:
        status = "blocked"
    elif obstacle_unknown or risk_unknown:
        status = "unknown"
    else:
        status = "confirmed"
    return {"contract": CONTRACT_VERSION, "status": status, "subject": {"kind": "operational_route",
        "route_id": route_id, "geometry_sha256": expected_geometry_fingerprint,
        "provenance": route.get("provenance"), "path": path},
        "altitude_layer": layer, "policy": policy, "domains": domains, "risk": risk,
        "fingerprint": fingerprint(identity), "source_identity": source_identity,
        "research_only": True, "operational_authorization": False,
        "formal_admission_evaluated": False}
