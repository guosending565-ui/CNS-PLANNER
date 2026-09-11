from pathlib import Path

import pytest

from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.cns_inputs import (
    normalize_aircraft_profile, normalize_device, normalize_required_cns,
)


DEFAULTS = Path("cns_planner/config/defaults.json")


def complete_legacy_requirements():
    return {
        "project_default": {
            "communication": {"required": True, "coverage_requirement": 95, "max_gap_m": 500, "latency_ms": 250, "redundancy": 2},
            "navigation": {"required": True, "coverage_requirement": 95, "accuracy_m": 3, "integrity": "required", "redundancy": 1},
            "surveillance": {"required": True, "coverage_requirement": 90, "update_interval_s": 2, "redundancy": 1},
        }
    }


def test_legacy_required_cns_backfills_canonical_performance_without_changing_v1_fields():
    result = normalize_required_cns(complete_legacy_requirements())
    c = result["project_default"]["communication"]
    n = result["project_default"]["navigation"]
    s = result["project_default"]["surveillance"]
    assert result["status"] == "passed"
    assert c["latency_ms"] == 250
    assert c["performance"]["max_latency_s"] == 0.25
    assert n["accuracy_m"] == n["performance"]["max_horizontal_error_m"] == 3
    assert n["integrity"] == n["performance"]["integrity_required"] == "required"
    assert s["update_interval_s"] == s["performance"]["max_update_interval_s"] == 2
    assert c["performance"]["min_redundancy"] == c["redundancy"] == 2
    assert c["type"]["technology"] == "unknown"
    assert c["confirmed"] is False


def test_canonical_required_cns_creates_v1_aliases_and_preserves_taxonomy():
    value = complete_legacy_requirements()
    value["project_default"]["communication"].pop("latency_ms")
    value["project_default"]["communication"].pop("redundancy")
    value["project_default"]["communication"].update({
        "type": {"service_type": "command_control", "technology": "5g", "network_scope": "private", "interfaces": ["IP"]},
        "performance": {"max_latency_s": 0.1, "min_availability": 0.99, "min_redundancy": 2},
        "contingency": "切换卫星链路", "source": "operator", "confirmed": True,
    })
    result = normalize_required_cns(value)["project_default"]["communication"]
    assert result["latency_ms"] == 100
    assert result["redundancy"] == 2
    assert result["type"] == {"service_type": "command_control", "technology": "5g", "network_scope": "private", "interfaces": ["IP"]}
    assert result["performance"]["min_availability"] == 0.99
    assert result["contingency"] == "切换卫星链路"
    assert result["confirmed"] is True


@pytest.mark.parametrize("mutator", [
    lambda c: c.update({"type": {"technology": "6g"}}),
    lambda c: c.update({"performance": {"min_availability": 1.01}}),
    lambda c: c.update({"performance": {"min_redundancy": 0}}),
    lambda c: c.update({"performance": {"lost_link_threshold_s": -1}}),
])
def test_communication_enum_range_and_nonnegative_validation(mutator):
    value = complete_legacy_requirements()
    c = value["project_default"]["communication"]
    mutator(c)
    with pytest.raises(ValueError):
        normalize_required_cns(value)


def test_surveillance_enums_probability_and_alias_conflicts_are_rejected():
    value = complete_legacy_requirements()
    s = value["project_default"]["surveillance"]
    s["type"] = {"target_cooperation": "sometimes", "sensor_mode": "active", "technology": "radar"}
    with pytest.raises(ValueError):
        normalize_required_cns(value)
    s["type"]["target_cooperation"] = "mixed"
    s["performance"] = {"min_detection_probability": 1.2}
    with pytest.raises(ValueError):
        normalize_required_cns(value)
    s["performance"] = {"max_update_interval_s": 3}
    with pytest.raises(ValueError, match="别名值不一致"):
        normalize_required_cns(value)


def test_aircraft_capability_and_device_performance_are_separate_compatible_contracts():
    aircraft = normalize_aircraft_profile({
        "aircraft_id": "A1", "name": "A1", "source": "manual",
        "communication": {"status": "confirmed", "capabilities": ["voice"], "type": {"technology": "satellite", "network_scope": "managed_service"}, "performance": {"max_latency_s": 0.4}, "confirmed": True},
        "navigation": ["gnss"], "surveillance": {"capabilities": []},
    })
    device = normalize_device({
        "device_id": "C1", "name": "C1", "subsystem": "C", "role": "primary",
        "radius_m": 5000, "latency_ms": 400, "mtbf_h": 1000, "enabled": True,
        "type": {"technology": "dedicated_radio", "network_scope": "dedicated"},
    })
    assert aircraft["communication"]["capabilities"] == ["voice"]
    assert aircraft["communication"]["performance"]["max_latency_s"] == 0.4
    assert device["performance"]["max_latency_s"] == 0.4
    assert device["latency_ms"] == 400
    assert device["radius_m"] == 5000
    assert aircraft["communication"] is not device


def test_navigation_and_surveillance_device_legacy_aliases_are_retained():
    navigation = normalize_device({
        "device_id": "N1", "name": "N1", "subsystem": "N", "role": "primary",
        "radius_m": 3000, "mtbf_h": 1000, "enabled": True,
        "accuracy_m": 2.5, "integrity": "confirmed",
    })
    surveillance = normalize_device({
        "device_id": "S1", "name": "S1", "subsystem": "S", "role": "primary",
        "radius_m": 3000, "mtbf_h": 1000, "enabled": True,
        "performance": {"max_update_interval_s": 1.5, "min_detection_probability": 0.9},
    })
    assert navigation["accuracy_m"] == navigation["performance"]["max_horizontal_error_m"] == 2.5
    assert navigation["integrity"] == navigation["performance"]["integrity_required"] == "confirmed"
    assert surveillance["update_interval_s"] == surveillance["performance"]["max_update_interval_s"] == 1.5
    assert surveillance["performance"]["min_detection_probability"] == 0.9


def test_route_override_and_project_round_trip_preserve_p3_contract(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    workflow.state["scenario_routes"] = [{"route_id": "R1"}]
    requirements = complete_legacy_requirements()["project_default"]
    requirements["navigation"].update({
        "type": {"technology": "hybrid"},
        "performance": {"max_horizontal_error_m": 3, "integrity_required": "required", "min_availability": 0.98, "min_redundancy": 1},
        "source": "route assessment", "confirmed": True,
    })
    workflow.set_required_cns({"scope": "route", "route_id": "R1", "requirements": requirements})
    restored = WorkflowService(tmp_path / "project.json", DEFAULTS).snapshot()
    route = restored["required_cns"]["route_overrides"]["R1"]["navigation"]
    assert route["type"]["technology"] == "hybrid"
    assert route["performance"]["max_horizontal_error_m"] == route["accuracy_m"] == 3
    assert route["source"] == "route assessment"


def test_old_project_catalog_items_receive_additive_contract_backfill(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    workflow.state["aircraft_profiles"] = {"status": "passed", "count": 1, "items": [{
        "aircraft_id": "OLD", "name": "Old", "communication": ["radio"],
        "navigation": ["gnss"], "surveillance": [],
    }]}
    workflow.state["device_catalog"] = {"status": "passed", "count": 1, "items": [{
        "device_id": "OLD-C", "subsystem": "C", "radius_m": 1000,
        "latency_ms": 50, "enabled": True,
    }]}
    workflow.session.save()
    restored = WorkflowService(tmp_path / "project.json", DEFAULTS).snapshot()
    assert restored["aircraft_profiles"]["items"][0]["communication"]["type"]["technology"] == "unknown"
    assert restored["device_catalog"]["items"][0]["performance"]["max_latency_s"] == 0.05
    assert restored["device_catalog"]["items"][0]["radius_m"] == 1000
