"""R0005 bounded research admission; no planner or state writer is invoked."""
from copy import deepcopy
from collections import Counter
import math
import json
from hashlib import sha256
from .corridor_site_planning_service import _apply_cumulative_action
from .engineering_assumption_p16_adapter import dedupe_candidate_pool_by_physical_site, validate_scenario_p16_inputs
from .planning_evidence_service import device_catalog_with_evidence
from .communication_assumption_probe import probe_communication_assumptions


def build_research_pool(pool, matrix, state, classifications, *, communication_assumptions):
    if not any(r.get("route_id") == "R0005" for r in state.get("operational_routes", [])):
        raise ValueError("R0005 authority missing")
    if (pool.get("scenario") or {}).get("route_id") not in (None, "R0005"):
        raise ValueError("pool belongs to another route")
    assumptions = communication_assumptions
    if assumptions.get("radius_m") != 2000 or not str(assumptions.get("interface") or "").strip():
        raise ValueError("explicit 2km/interface assumptions required")
    latency = assumptions.get("max_latency_s")
    if isinstance(latency, bool) or not isinstance(latency, (int, float)) or not math.isfinite(latency) or latency <= 0:
        raise ValueError("explicit finite latency required")
    positive = {r["action_id"] for r in matrix["rows"] if r["geometry_hit_indices"]}
    result = deepcopy(pool)
    result["scenario"]["route_id"] = "R0005"
    result["research_scope"] = {"route_id": "R0005", "status": "simulated", "assumption_only": True, "global_optimum_claim": False}
    result["research_scope"]["communication_assumptions"] = deepcopy(assumptions)
    by_id = {s["scenario_site_id"]: s for s in result["sites"]}
    kept, blocked = [], []
    for action in result["actions"]:
        if action["action_id"] not in positive:
            continue
        site = by_id[action["scenario_site_id"]]
        evidence = classifications.get(site["scenario_site_id"], {})
        payload = {k: v for k, v in evidence.items() if k != "input_fingerprint"}
        registered_hash = (((state.get("source_audits") or {}).get("items") or {}).get("land_mask") or {}).get("verification", {}).get("sha256")
        manifest = (((state.get("source_audits") or {}).get("items") or {}).get("land_mask") or {})
        verified = (manifest.get("status") == "verified"
                    and evidence.get("coastal_buffer_m") == (state.get("surface_classification_policy") or {}).get("coastal_uncertainty_buffer_m")
                    and evidence.get("coordinate") == site.get("coordinate")
                    and bool(registered_hash) and evidence.get("source_sha256") == registered_hash
                    and evidence.get("input_fingerprint") == sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest())
        if evidence.get("surface_class") != "land" or not verified:
            blocked.append({"action_id": action["action_id"], "subsystem": action["subsystem"], "reason": "surface_evidence_not_land", "evidence": evidence})
            continue
        site["surface_class"] = "land"
        site["surface_class_evidence"] = deepcopy(evidence)
        if action["subsystem"] == "C":
            action["real_eligibility"] = deepcopy(action["eligibility"])
            action["eligibility"] = {"status": "eligible", "reasons": [], "basis": "D23_declared_performance_assumption_only"}
            action["scenario_eligible"] = True
        action["research_status"] = "simulated"
        kept.append(action)
    ids = {a["scenario_site_id"] for a in kept}
    result["sites"] = [s for s in result["sites"] if s["scenario_site_id"] in ids]
    result["actions"] = kept
    for site in result["sites"]:
        site["actions"] = [deepcopy(a) for a in kept if a["scenario_site_id"] == site["scenario_site_id"]]
    result["counts"]["canonical_site_count"] = len(ids)
    result["counts"]["unique_coordinate_count"] = len({tuple(s["physical_site_key"]) for s in result["sites"]})
    deduped = dedupe_candidate_pool_by_physical_site(result) if kept else None
    research_state = deepcopy(state)
    catalog = device_catalog_with_evidence(state)
    for device in catalog.get("items", []):
        if device.get("device_id") == "COMM-BASELINE-2KM":
            geometry = device.get("coverage_geometry") or {}
            if geometry.get("model") != "hemisphere" or geometry.get("slant_range_m") != 2000:
                raise ValueError("2km hemisphere declaration missing")
            geometry.update({"confirmed": True, "status": "confirmed", "source": "explicit_research_assumption", "assumption_only": True})
    research_state["device_catalog"] = catalog
    validation = validate_scenario_p16_inputs(result, deduped, research_state) if kept else {"status": "blocked", "errors": [{"code": "empty_input"}]}
    return {"status": "simulated", "assumption_only": True, "writes_project_state": False,
            "pool": result, "deduped": deduped, "validation": validation, "blocked": blocked,
            "counts": dict(Counter(a["subsystem"] for a in kept))}


def probe_research_c(admission, action_id, catalog, aircraft, facilities, required, probes, **assumptions):
    if admission["validation"]["status"] != "passed":
        raise ValueError("research admission blocked")
    action = next((a for a in admission["pool"]["actions"] if a["action_id"] == action_id and a["subsystem"] == "C"), None)
    if action is None:
        raise ValueError("action outside bounded C pool")
    if assumptions != admission["pool"]["research_scope"]["communication_assumptions"]:
        raise ValueError("assumptions differ from admitted fingerprint inputs")
    expected_facilities = _apply_cumulative_action({}, action)
    if facilities != expected_facilities:
        raise ValueError("facilities differ from admitted candidate")
    return probe_communication_assumptions(catalog, aircraft, facilities, required, probes, **assumptions)


def classify_research_sites(state, sites, source_path):
    """Read registered land polygons, keeping coastal ambiguity unknown."""
    from hashlib import sha256
    import json
    from pathlib import Path
    from ..gis.land_mask_source import build_land_mask_source, audit_manifest_for
    manifest = audit_manifest_for(state)
    digest = sha256(Path(source_path).read_bytes()).hexdigest()
    if manifest.get("status") != "verified" or digest != (manifest.get("verification") or {}).get("sha256"):
        raise ValueError("registered land source verification failed")
    policy = state.get("surface_classification_policy") or {}
    buffer_m = policy.get("coastal_uncertainty_buffer_m")
    if not isinstance(buffer_m, (int, float)) or not math.isfinite(buffer_m) or buffer_m < 0:
        raise ValueError("coastal policy missing")
    source = build_land_mask_source(source_path, policy)
    records = {}
    for site in sites:
        x, y = site["coordinate"]
        surface, boundary = source.classify(x, y)
        distance = boundary.get("distance_to_land_boundary_m")
        if surface == "coastal_uncertain" or (distance is not None and distance <= buffer_m):
            surface = "unknown"
        record = {"surface_class": surface, "coordinate": [x, y], "source_sha256": digest,
                  "source_id": manifest.get("source_id"), "boundary": boundary, "coastal_buffer_m": buffer_m}
        record["input_fingerprint"] = sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
        records[site["scenario_site_id"]] = record
    return records
