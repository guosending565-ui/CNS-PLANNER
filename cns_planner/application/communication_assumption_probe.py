"""Read-only C engineering probe; never a ProjectState writer."""
from copy import deepcopy
from hashlib import sha256
import json
import math
from ..algorithms.coverage.geometric_3d import build_geometric_providers, evaluate_geometry_point
from ..algorithms.service_capability.v1 import build_provider_devices, evaluate_capability_point


def probe_communication_assumptions(catalog, aircraft, facilities, required, probes, *, radius_m, interface, max_latency_s):
    """Reuse declared_performance on private copies, retaining the real P8 result."""
    if not isinstance(interface, str) or not interface.strip():
        raise ValueError("必须显式声明通信接口假设")
    for value in (radius_m, max_latency_s):
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
            raise ValueError("半径和延迟必须是有限正数")
    original = [catalog, aircraft, facilities, required, probes]
    c, a = deepcopy(catalog), deepcopy(aircraft)
    devices = [d for d in c.get("items", []) if d.get("device_id") == "COMM-BASELINE-2KM"]
    if len(devices) != 1 or devices[0].get("enabled") is not True:
        raise ValueError("需要唯一启用的2km研究设备")
    d = devices[0]
    d["radius_m"] = radius_m
    d["coverage_geometry"]["radius_by_surface"] = {k: radius_m for k in d["coverage_geometry"].get("radius_by_surface", {})}
    d["type"] = {"technology": "dedicated_radio", "network_scope": "dedicated", "interfaces": [interface]}
    d["service_model"] = {"model_family": "declared_performance", "version": "r7d23-assumption-v1", "technology": "dedicated_radio", "confirmed": True, "status": "confirmed", "source": "engineering_assumption", "parameters": {"performance": {"max_latency_s": max_latency_s}}, "assumptions": ["hypothetical compatible airborne and ground interfaces"], "limitations": ["not manufacturer evidence", "no LOS/interference/runtime reliability evaluation"]}
    a["communication"]["type"] = deepcopy(d["type"])
    a["communication"]["performance"]["max_latency_s"] = max_latency_s
    def evaluate(cat, air):
        providers = build_geometric_providers(facilities, cat)["C"]
        dev = build_provider_devices(cat, facilities)
        return [evaluate_capability_point("C", {**p, **evaluate_geometry_point(p, providers)}, required, air, dev) for p in probes]
    fingerprint = sha256(json.dumps([original, radius_m, interface, max_latency_s, "r7d23-assumption-v1"], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {"status": "simulated", "assumption_only": True, "real_world_confirmed": False, "writes_project_state": False, "input_fingerprint": fingerprint, "assumptions": {"radius_m": radius_m, "interface": interface, "max_latency_s": max_latency_s}, "real_p8": evaluate(catalog, aircraft), "simulated_p8": evaluate(c, a)}
