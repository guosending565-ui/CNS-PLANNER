import pytest
from cns_planner.application.communication_assumption_probe import probe_communication_assumptions

@pytest.mark.parametrize("radius,interface,latency", [(0,"ip",1),(float("nan"),"ip",1),(2000,"",1),(2000,"ip",None)])
def test_explicit_finite_assumptions_required(radius,interface,latency):
    with pytest.raises(ValueError):
        probe_communication_assumptions({}, {}, {}, {}, [], radius_m=radius,interface=interface,max_latency_s=latency)

def test_isolation_fingerprint_and_real_channel(monkeypatch):
    from copy import deepcopy
    import cns_planner.application.communication_assumption_probe as m
    catalog = {"items": [{"device_id": "COMM-BASELINE-2KM", "enabled": True, "coverage_geometry": {"radius_by_surface": {"land": 2000}}, "service_model": {"model_family": "unsupported"}}]}
    aircraft = {"communication": {"performance": {}}}
    original = deepcopy([catalog, aircraft])
    monkeypatch.setattr(m, "build_geometric_providers", lambda f,c: {"C": []})
    monkeypatch.setattr(m, "evaluate_geometry_point", lambda p,g: {"covered": True})
    monkeypatch.setattr(m, "build_provider_devices", lambda c,f: c)
    monkeypatch.setattr(m, "evaluate_capability_point", lambda code,p,r,a,c: {"status": "unknown" if c["items"][0]["service_model"]["model_family"] == "unsupported" else "meets_under_model"})
    def run(latency):
        return m.probe_communication_assumptions(catalog,aircraft,{}, {}, [{}],radius_m=2000,interface="ip",max_latency_s=latency)
    result = run(1)
    assert result["real_p8"] == [{"status": "unknown"}]
    assert result["simulated_p8"] == [{"status": "meets_under_model"}]
    assert result["status"] == "simulated" and result["assumption_only"]
    assert result["writes_project_state"] is False and result["real_world_confirmed"] is False
    assert [catalog, aircraft] == original
    assert result["input_fingerprint"] == run(1)["input_fingerprint"]
    assert result["input_fingerprint"] != run(2)["input_fingerprint"]
