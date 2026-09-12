from copy import deepcopy
from pathlib import Path

import pytest

from cns_planner.algorithms.service_capability.v1 import (
    CNSServiceCapabilityV1, free_space_link_budget,
)
from cns_planner.api.router import ApiRouter
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.cns_inputs import normalize_device
from cns_planner.domain.cns_service_model import normalize_service_model_spec


DEFAULTS = Path("cns_planner/config/defaults.json")


def requirement(code="C", *, redundancy=1, technology=None):
    performance = {"min_redundancy": redundancy}
    type_data = {}
    if code == "C":
        performance["max_latency_s"] = 1.0
        type_data = {"technology": technology or "dedicated_radio", "interfaces": ["ip"]}
    elif code == "N":
        performance.update({"max_horizontal_error_m": 5.0, "integrity_required": "required"})
        type_data = {"technology": technology or "gnss"}
    else:
        performance.update({"min_detection_probability": 0.9, "max_update_interval_s": 2.0})
        type_data = {"target_cooperation": "non_cooperative", "sensor_mode": "active", "technology": technology or "radar"}
    return {"required": True, "status": "passed", "type": type_data, "performance": performance}


def aircraft(code="C", *, compatible=True):
    if code == "C":
        return {"confirmed": True, "status": "confirmed", "capabilities": ["radio"], "type": {"technology": "dedicated_radio" if compatible else "satellite", "interfaces": ["ip"]}, "performance": {"max_latency_s": 0.5, "min_redundancy": 1}}
    if code == "N":
        return {"confirmed": True, "status": "confirmed", "capabilities": ["pnt"], "type": {"technology": "gnss"}, "performance": {"max_horizontal_error_m": 2.0, "integrity_required": "required", "min_redundancy": 1}}
    return {"confirmed": True, "status": "confirmed", "capabilities": ["daa"], "type": {"target_cooperation": "non_cooperative", "sensor_mode": "active", "technology": "radar"}, "performance": {"min_detection_probability": 0.95, "max_update_interval_s": 1.0, "min_redundancy": 1}}


def service_model(family="declared_performance", *, confirmed=True, technology="dedicated_radio", independence=None):
    parameters = {"performance": {"max_latency_s": 0.2, "min_redundancy": 1}}
    if family == "free_space_link_budget":
        parameters.update({
            "frequency_hz": 2.4e9, "tx_power_dbm": 30, "tx_gain_dbi": 10,
            "tx_loss_db": 1, "rx_gain_dbi": 5, "rx_loss_db": 1,
            "rx_sensitivity_dbm": -100, "required_margin_db": 10,
        })
    if independence:
        parameters.update({"independence_confirmed": True, "independence_group": independence})
    return {"model_family": family, "version": "1", "technology": technology, "parameters": parameters, "source": "test", "confirmed": confirmed}


def device(code="C", *, model=None, type_data=None, performance=None, device_id="D1"):
    return normalize_device({
        "device_id": device_id, "name": device_id, "subsystem": code, "role": "existing",
        "radius_m": 5000, "mtbf_h": 1000, "type": type_data or ({"technology": "dedicated_radio", "interfaces": ["ip"]} if code == "C" else {"technology": "radar", "target_cooperation": "non_cooperative", "sensor_mode": "active"}),
        "performance": performance or ({"max_latency_s": 0.2, "min_redundancy": 1} if code == "C" else {}),
        "service_model": model if model is not None else service_model(),
    })


def coverage(samples, code="C", total=300.0):
    other = [{"subsystem": value, "status": "missing_data", "samples": deepcopy(samples)} for value in ("C", "N", "S") if value != code]
    return {"status": "passed", "routes": [{"route_id": "R1", "route_length_m": total, "samples": deepcopy(samples), "subsystems": [{"subsystem": code, "status": "passed", "samples": deepcopy(samples)}, *other]}]}


def sample(offset, covered=True, providers=None):
    return {"distance_along_route_m": offset, "longitude": offset / 100000, "latitude": 0, "grid_id": "G1", "covered": covered, "providers": providers if providers is not None else ([{"facility_id": "F1", "device_id": "D1", "slant_distance_m": 1000.0}] if covered else [])}


def required_set(code="C", required=None):
    names = {"C": "communication", "N": "navigation", "S": "surveillance"}
    values = {name: {"required": False, "status": "passed", "type": {}, "performance": {}} for name in names.values()}
    values[names[code]] = required or requirement(code)
    return {"status": "passed", "project_default": values, "route_overrides": {}}


def aircraft_profile(code="C", capability=None):
    values = {"communication": {}, "navigation": {}, "surveillance": {}}
    values[{"C": "communication", "N": "navigation", "S": "surveillance"}[code]] = capability or aircraft(code)
    return {"aircraft_id": "A1", **values}


def evaluate(samples, *, code="C", required=None, capability=None, devices=None, total=300.0):
    return CNSServiceCapabilityV1().evaluate(
        coverage(samples, code, total), required_set(code, required),
        aircraft_profile(code, capability), {}, {"items": devices if devices is not None else [device(code)]},
    )["routes"][0]["subsystems"][{"C": 0, "N": 1, "S": 2}[code]]


def test_service_model_contract_does_not_promote_legacy_demo_data():
    assert normalize_service_model_spec(None)["status"] == "missing_data"
    legacy = normalize_device({"device_id": "D", "name": "D", "subsystem": "C", "role": "existing", "radius_m": 1000, "mtbf_h": 1000})
    assert legacy["service_model"]["model_family"] == "unsupported"
    assert legacy["service_model"]["confirmed"] is False
    with pytest.raises(ValueError, match="model_family"):
        normalize_service_model_spec({"model_family": "invented"})


def test_geometry_gate_and_unconfirmed_model_are_conservative():
    assert evaluate([sample(0, False), sample(300, False)])["status"] == "does_not_meet_under_model"
    pending = device(model=service_model(confirmed=False))
    result = evaluate([sample(0), sample(300)], devices=[pending])
    assert result["status"] == "unknown"
    assert result["unknown_length_m"] == 300


def test_free_space_link_budget_fixture_and_invalid_distance():
    parameters = service_model("free_space_link_budget")["parameters"]
    result = free_space_link_budget(1000, parameters)
    assert result["path_loss_db"] == pytest.approx(100.052, abs=0.01)
    assert result["received_power_dbm"] == pytest.approx(-57.052, abs=0.01)
    assert result["link_margin_db"] == pytest.approx(32.948, abs=0.01)
    assert result["status"] == "meets_under_model"
    with pytest.raises(ValueError, match="clamp"):
        free_space_link_budget(0, parameters)


def test_4g_free_space_model_is_reference_only():
    model = service_model("free_space_link_budget", technology="4g")
    c_device = device(model=model, type_data={"technology": "4g", "interfaces": ["ip"]})
    req = requirement("C", technology="4g")
    cap = {**aircraft("C"), "type": {"technology": "4g", "interfaces": ["ip"]}}
    result = evaluate([sample(0), sample(300)], required=req, capability=cap, devices=[c_device])
    provider = result["samples"][0]["provider_evaluations"][0]
    assert provider["model_scope"] == "free_space_reference"
    assert provider["reference_only"] is True
    assert provider["link_budget"]["not_evaluated"]["handover"] == "not_evaluated"


def test_navigation_uses_confirmed_aircraft_performance_without_ground_sphere():
    samples = [sample(0, False, []), sample(300, False, [])]
    result = evaluate(samples, code="N", devices=[])
    assert result["status"] == "meets_under_model"
    assert result["model_scope"] == "aircraft_declared_performance"
    gnss = normalize_device({"device_id": "N1", "name": "GNSS", "subsystem": "N", "role": "existing", "radius_m": 1000, "mtbf_h": 1000, "type": {"technology": "gnss"}})
    assert gnss["coverage_geometry"]["model"] == "none"
    poor = deepcopy(aircraft("N"))
    poor["performance"]["max_horizontal_error_m"] = 20
    assert evaluate(samples, code="N", capability=poor, devices=[])["status"] == "does_not_meet_under_model"


def test_surveillance_cooperation_mismatch_and_missing_pd():
    cooperative = device("S", model=service_model("declared_performance", technology="adsb"), type_data={"technology": "adsb", "target_cooperation": "cooperative", "sensor_mode": "passive"}, performance={"min_detection_probability": 0.99, "max_update_interval_s": 1, "min_redundancy": 1})
    mismatch = evaluate([sample(0), sample(300)], code="S", devices=[cooperative])
    assert mismatch["status"] == "does_not_meet_under_model"
    radar_missing_pd = device("S", model={**service_model("declared_performance", technology="radar"), "parameters": {"performance": {"max_update_interval_s": 1}}}, performance={"max_update_interval_s": 1})
    unknown = evaluate([sample(0), sample(300)], code="S", devices=[radar_missing_pd])
    assert unknown["status"] == "unknown"


def test_aircraft_incompatibility_prevents_meet():
    result = evaluate([sample(0), sample(300)], capability=aircraft("C", compatible=False))
    assert result["status"] == "does_not_meet_under_model"


def test_provider_multiplicity_is_not_independent_redundancy_without_evidence():
    providers = [{"facility_id": "F1", "device_id": "D1", "slant_distance_m": 100}, {"facility_id": "F2", "device_id": "D2", "slant_distance_m": 100}]
    req = requirement("C", redundancy=2)
    result = evaluate([sample(0, providers=providers), sample(300, providers=providers)], required=req, devices=[device(device_id="D1"), device(device_id="D2")])
    assert result["status"] == "unknown"
    evidence = result["samples"][0]["evidence"][-1]
    assert evidence["provider_count"] == 2
    assert evidence["independent_redundancy_count"] is None


def test_only_compatible_confirmed_independent_providers_count_as_redundancy():
    providers = [{"facility_id": "F1", "device_id": "D1", "slant_distance_m": 100}, {"facility_id": "F2", "device_id": "D2", "slant_distance_m": 100}]
    req = requirement("C", redundancy=2)
    independent = [
        device(device_id="D1", model=service_model(independence="power-a")),
        device(device_id="D2", model=service_model(independence="power-b")),
    ]
    result = evaluate([sample(0, providers=providers), sample(300, providers=providers)], required=req, devices=independent)
    assert result["status"] == "meets_under_model"

    incompatible = device(
        device_id="D2", model=service_model(technology="satellite", independence="power-b"),
        type_data={"technology": "satellite", "interfaces": ["ip"]},
    )
    result = evaluate([sample(0, providers=providers), sample(300, providers=providers)], required=req, devices=[independent[0], incompatible])
    assert result["status"] == "does_not_meet_under_model"
    assert result["samples"][0]["provider_evaluations"][1]["stage"] == "provider_type_compatibility"


def test_length_weighted_summary_keeps_unknown_separate_from_fail():
    samples = [sample(0, True), sample(100, False), sample(300, False)]
    result = evaluate(samples, total=300)
    assert result["meets_length_m"] == 0
    assert result["unknown_length_m"] == 100
    assert result["fail_length_m"] == 200
    assert result["meets_length_m"] + result["fail_length_m"] + result["unknown_length_m"] == 300


class ApiContext:
    def __init__(self, workflow):
        self.workflow = workflow
        self.data = object()


def test_registry_project_persistence_api_and_directed_invalidation(tmp_path):
    path = tmp_path / "project.json"
    workflow = WorkflowService(path, DEFAULTS)
    assert workflow.algorithm_registry.manifest("service_model", "cns_service_capability_v1", "1.0")
    workflow.state["coverage_3d"] = coverage([sample(0), sample(300)])
    workflow.state["required_cns"] = required_set()
    workflow.state["aircraft_profiles"] = {"status": "passed", "items": [aircraft_profile()]}
    workflow.state["selected_aircraft_profile_id"] = "A1"
    workflow.state["device_catalog"] = {"status": "passed", "items": [device()]}
    result = ApiRouter(ApiContext(workflow)).post("/api/cns-service-capability/evaluate", {}).data
    assert result["cns_service_capability"]["algorithm_id"] == "cns_service_capability_v1"
    assert ApiRouter(ApiContext(workflow)).get("/api/cns-service-capability", {}, {}).data["model_scope"] == "static_capability"
    restored = WorkflowService(path, DEFAULTS)
    assert restored.cns_service_capability_snapshot()["input_fingerprint"] == result["cns_service_capability"]["input_fingerprint"]
    restored.state["result_statuses"].update({"cns_service_capability": "passed", "grid": "passed", "routes": "passed", "coverage": "passed", "cns_gap": "passed"})
    restored.state["cns_service_capability"]["status"] = "meets_under_model"
    restored.invalidation_service.cns_service_capability()
    assert restored.state["result_statuses"]["cns_service_capability"] == "stale"
    assert {name: restored.state["result_statuses"][name] for name in ("grid", "routes", "coverage", "cns_gap")} == {name: "passed" for name in ("grid", "routes", "coverage", "cns_gap")}
