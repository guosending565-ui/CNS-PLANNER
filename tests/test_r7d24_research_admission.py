from copy import deepcopy
import pytest
from cns_planner.application import p16_research_admission as m

A = {"radius_m": 2000, "interface": "ip", "max_latency_s": 1}

def inputs():
    site = {"scenario_site_id": "one", "physical_site_key": [122, 30], "coordinate": [122, 30]}
    action = {"action_id": "c", "scenario_site_id": "one", "subsystem": "C", "eligibility": {"status": "ineligible", "reasons": ["unsupported"]}, "confirmed": False}
    return ({"scenario": {}, "sites": [site], "actions": [action], "counts": {}},
            {"rows": [{"action_id": "c", "geometry_hit_indices": [0]}]},
            {"operational_routes": [{"route_id": "R0005"}], "source_audits": {"items": {"land_mask": {"status": "verified", "verification": {"sha256": "source"}}}}})

@pytest.mark.parametrize("surface,digest,kept", [("land", "hash", True), ("unknown", "hash", False), ("sea", "hash", False), ("land", None, False)])
def test_bounded_pool_isolation_and_surface_gate(monkeypatch, surface, digest, kept):
    monkeypatch.setattr(m, "dedupe_candidate_pool_by_physical_site", lambda p: {"actions": p["actions"]})
    monkeypatch.setattr(m, "validate_scenario_p16_inputs", lambda *args: {"status": "passed"})
    pool, matrix, state = inputs()
    before = deepcopy([pool, matrix, state])
    import json, hashlib
    evidence = {"surface_class": surface, "coordinate": [122, 30], "source_sha256": "source", "coastal_buffer_m": None}
    evidence["input_fingerprint"] = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest() if digest else None
    result = m.build_research_pool(pool, matrix, state, {"one": evidence}, communication_assumptions=A)
    assert bool(result["pool"]["actions"]) is kept
    assert [pool, matrix, state] == before
    if kept:
        action = result["pool"]["actions"][0]
        assert action["real_eligibility"]["status"] == "ineligible"
        assert action["confirmed"] is False
        assert action["research_status"] == "simulated"
        assert result["pool"]["research_scope"]["global_optimum_claim"] is False

@pytest.mark.parametrize("assumptions", [{}, {**A, "radius_m": 4000}, {**A, "max_latency_s": float("nan")}])
def test_missing_assumptions_block(assumptions):
    with pytest.raises(ValueError):
        m.build_research_pool(*inputs(), {}, communication_assumptions=assumptions)

def test_probe_cannot_escape_pool_or_change_assumptions():
    ad = {"validation": {"status": "passed"}, "pool": {"actions": [{"action_id": "c", "subsystem": "C"}], "research_scope": {"communication_assumptions": A}}}
    for action, assumptions in [("outside", A), ("c", {**A, "radius_m": 4000})]:
        with pytest.raises(ValueError):
            m.probe_research_c(ad, action, {}, {}, {}, {}, [], **assumptions)


def test_classification_verifies_source_and_keeps_coast_unknown(tmp_path, monkeypatch):
    import hashlib
    import cns_planner.gis.land_mask_source as land
    source = tmp_path / "land.gpkg"
    source.write_bytes(b"fixture")
    state = {"source_audits": {"items": {"land_mask": {"status": "verified", "verification": {"sha256": hashlib.sha256(b"fixture").hexdigest()}}}}, "surface_classification_policy": {"coastal_uncertainty_buffer_m": 30}}
    class Mask:
        def classify(self, x, y):
            return "land", {"distance_to_land_boundary_m": x}
    monkeypatch.setattr(land, "build_land_mask_source", lambda *args: Mask())
    sites = [{"scenario_site_id": "coast", "coordinate": [20, 30]}, {"scenario_site_id": "land", "coordinate": [100, 30]}]
    result = m.classify_research_sites(state, sites, source)
    assert result["coast"]["surface_class"] == "unknown"
    assert result["land"]["surface_class"] == "land"
    assert result["land"]["input_fingerprint"]
    source.write_bytes(b"changed")
    with pytest.raises(ValueError):
        m.classify_research_sites(state, sites, source)


def test_other_route_cannot_be_relabeled():
    pool, matrix, state = inputs()
    pool["scenario"]["route_id"] = "R0006"
    with pytest.raises(ValueError):
        m.build_research_pool(pool, matrix, state, {}, communication_assumptions=A)
