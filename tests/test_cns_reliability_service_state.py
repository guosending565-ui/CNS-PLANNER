from copy import deepcopy
from math import exp
from pathlib import Path

import pytest

from cns_planner.api.router import ApiRouter
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.cns_inputs import normalize_aircraft_profile
from cns_planner.domain.cns_inputs import normalize_required_cns
from cns_planner.domain.cns_reliability import normalize_reliability_spec
from cns_planner.safety.reliability import evaluate_reliability
from cns_planner.safety.service_state import evaluate_service_state


DEFAULTS = Path("cns_planner/config/defaults.json")


def requirement(required=True):
    return {
        "status": "passed", "required": required,
        "type": {"service_type": "command_control", "technology": "5g", "network_scope": "private", "interfaces": ["IP"]},
        "performance": {"max_latency_s": 0.2, "max_continuous_outage_s": 10, "min_availability": 0.95, "min_redundancy": 1},
    }


def aircraft(fallbacks=None):
    return {
        "status": "confirmed", "confirmed": True, "capabilities": ["command_control"],
        "type": {"service_type": "command_control", "technology": "5g", "network_scope": "private", "interfaces": ["IP"]},
        "performance": {"max_latency_s": 0.15, "min_availability": 0.99, "min_redundancy": 1},
        "fallbacks": fallbacks or [],
    }


def external(status="available", **updates):
    result = {
        "status": status,
        "type": {"service_type": "command_control", "technology": "5g", "network_scope": "private", "interfaces": ["IP"]},
        "performance": {"latency_s": 0.1, "availability": 0.98},
        "redundancy": 1, "source": "fixture",
    }
    result.update(updates)
    return result


def fallback(confirmed=True, bridge=10):
    return {
        "type": {"service_type": "command_control", "technology": "5g", "network_scope": "private", "interfaces": ["IP"]},
        "performance": {"max_latency_s": 0.1, "min_availability": 0.99, "min_redundancy": 1},
        "max_bridge_time_s": bridge, "source": "tested fallback", "confirmed": confirmed,
    }


def test_reliability_validation_unknown_and_source_contract():
    empty = normalize_reliability_spec(None)
    assert empty["status"] == "missing_data"
    assert empty["mtbf_h"] is None
    pending = normalize_reliability_spec({"mtbf_h": 1000, "source": "demo/default", "confirmed": False})
    assert pending["source"] == "demo"
    assert pending["status"] == "pending_confirmation"
    assert pending["model"] == "unknown"
    with pytest.raises(ValueError):
        normalize_reliability_spec({"availability": 1.01})
    with pytest.raises(ValueError):
        normalize_reliability_spec({"mttr_h": -1})
    with pytest.raises(ValueError):
        normalize_reliability_spec({"sample_size": 1.5})
    with pytest.raises(ValueError):
        normalize_reliability_spec({"availability": 0.9, "availability_type": "combined"})
    empirical = normalize_reliability_spec({
        "model": "empirical", "empirical_failure_probability": 0.02,
        "availability": 0.97, "availability_type": "empirical",
        "source": "observations", "confirmed": True,
    })
    assert empirical["status"] == "passed"
    assert empirical["availability_type"] == "empirical"


def test_constant_rate_exponential_and_inherent_availability_formulas():
    result = evaluate_reliability({
        "model": "constant_rate_exponential", "mtbf_h": 1000, "mttr_h": 10,
        "source": "test report", "confirmed": True,
    }, 20)
    assert result["failure_rate_per_h"] == pytest.approx(0.001)
    assert result["reliability"] == pytest.approx(exp(-0.02))
    assert result["failure_probability"] == pytest.approx(1 - exp(-0.02))
    assert result["inherent_availability"] == pytest.approx(1000 / 1010)
    assert result["direct_availability"] is None


def test_exponential_requires_explicit_model_and_rates_must_agree():
    result = evaluate_reliability({"mtbf_h": 1000, "source": "manual", "confirmed": True}, 20)
    assert result["model"] == "unknown"
    assert result["failure_rate_per_h"] is None
    assert result["reliability"] is None
    with pytest.raises(ValueError, match="冲突"):
        evaluate_reliability({"model": "constant_rate_exponential", "mtbf_h": 1000, "failure_rate_per_h": 0.002}, 1)


def test_operational_availability_remains_separate_from_inherent_availability():
    result = evaluate_reliability({
        "mtbf_h": 100, "mttr_h": 10, "availability": 0.8,
        "availability_type": "operational", "confirmed": True,
    })
    assert result["inherent_availability"] == pytest.approx(100 / 110)
    assert result["direct_availability"] == {"value": 0.8, "availability_type": "operational"}
    assert result["reported_availability"] is result["direct_availability"]


def test_device_demo_mtbf_backfill_and_aircraft_top_level_mtbf_is_not_copied(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    device = workflow.state["device_catalog"]["items"][0]
    profile = workflow.state["aircraft_profiles"]["items"][0]
    assert device["reliability"]["mtbf_h"] == device["mtbf_h"]
    assert device["reliability"]["source"] == "demo"
    assert device["reliability"]["confirmed"] is False
    assert device["reliability"]["model"] == "unknown"
    assert profile["mtbf_h"] == 10000
    for name in ("communication", "navigation", "surveillance"):
        assert profile[name]["reliability"]["mtbf_h"] is None


def test_aircraft_fallback_and_reliability_round_trip(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    profile = normalize_aircraft_profile({
        "aircraft_id": "A-P4", "name": "P4", "mtbf_h": 5000,
        "communication": {
            "status": "confirmed", "capabilities": ["command_control"],
            "reliability": {"model": "constant_rate_exponential", "mtbf_h": 2000, "source": "test", "confirmed": True},
            "fallbacks": [fallback()],
        },
        "navigation": [], "surveillance": [],
    })
    workflow.state["aircraft_profiles"] = {"status": "passed", "count": 1, "items": [profile]}
    workflow.session.save()
    restored = WorkflowService(tmp_path / "project.json", DEFAULTS).state["aircraft_profiles"]["items"][0]
    assert restored["communication"]["reliability"]["mtbf_h"] == 2000
    assert restored["communication"]["fallbacks"][0]["max_bridge_time_s"] == 10
    assert restored["navigation"]["reliability"]["mtbf_h"] is None


def test_service_state_available_and_available_degraded():
    assert evaluate_service_state(requirement(), aircraft(), external())["service_state"] == "available"
    degraded = evaluate_service_state(requirement(), aircraft(), external("degraded", degradation_duration_s=5))
    assert degraded["service_state"] == "available_degraded"
    assert degraded["fallback_used"] is None
    timed_out = evaluate_service_state(requirement(), aircraft(), external("degraded", degradation_duration_s=11))
    assert timed_out["service_state"] == "lost"


def test_confirmed_fallback_transitions_to_contingency_without_random_reliability():
    result = evaluate_service_state(requirement(), aircraft([fallback()]), external("unavailable", outage_duration_s=5))
    assert result["service_state"] == "contingency"
    assert result["fallback_used"]["source"] == "tested fallback"
    assert all(item["kind"] != "reliability_sample" for item in result["evidence"])


@pytest.mark.parametrize("candidate,outage", [(fallback(False), 5), (fallback(True, bridge=4), 5)])
def test_unconfirmed_or_timed_out_fallback_is_lost(candidate, outage):
    result = evaluate_service_state(requirement(), aircraft([candidate]), external("unavailable", outage_duration_s=outage))
    assert result["service_state"] == "lost"
    assert result["fallback_used"] is None


def test_unknown_not_applicable_and_text_contingency_is_not_fallback():
    assert evaluate_service_state(requirement(False), None, None)["service_state"] == "not_applicable"
    assert evaluate_service_state(requirement(), aircraft(), {"status": "unknown"})["service_state"] == "unknown"
    only_text = {**aircraft(), "contingency": "use another link"}
    assert evaluate_service_state(requirement(), only_text, external("unavailable", outage_duration_s=1))["service_state"] == "lost"


class ApiContext:
    workflow = object()
    data = object()


def test_service_state_preview_api_is_pure_and_additive():
    router = ApiRouter(ApiContext())
    result = router.post("/api/cns/service-state/evaluate", {
        "required": requirement(), "aircraft": aircraft(), "external_service": external(),
    }).data
    assert result["service_state"] == "available"
    assert result["fallback_used"] is None


def test_gap_v1_compatibility_view_excludes_p4_reliability_from_fingerprint(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    workflow.state["operational_routes"] = [{
        "route_id": "R1", "status": "passed", "path": [[120.0, 30.0], [120.01, 30.0]],
    }]
    workflow.state["required_cns"] = normalize_required_cns({"project_default": {
        "communication": {"required": True, "coverage_requirement": 90, "max_gap_m": 1000, "latency_ms": 200, "redundancy": 1},
        "navigation": {"required": False}, "surveillance": {"required": False},
    }})
    workflow.state["aircraft_profiles"] = {"status": "passed", "count": 1, "items": [normalize_aircraft_profile({
        "aircraft_id": "A1", "name": "A1",
        "communication": {"status": "confirmed", "capabilities": ["radio"]},
        "navigation": [], "surveillance": [],
    })]}
    workflow.state["selected_aircraft_profile_id"] = "A1"
    workflow.state["existing_cns_facilities"] = {"status": "passed", "items": []}
    workflow.state["device_catalog"] = {"status": "passed", "items": []}
    before = deepcopy(workflow.analyze_cns_gaps()["cns_gap_analysis"])
    workflow.state["aircraft_profiles"]["items"][0]["communication"]["reliability"] = normalize_reliability_spec({
        "model": "constant_rate_exponential", "mtbf_h": 1000, "source": "test", "confirmed": True,
    })
    workflow.state["aircraft_profiles"]["items"][0]["communication"]["fallbacks"] = [fallback()]
    after = workflow.analyze_cns_gaps()["cns_gap_analysis"]
    assert after == before


def test_service_state_preview_api_is_pure_and_forwards_contract():
    router = ApiRouter(ApiContext())
    payload = {"required_cns": requirement(False), "aircraft_capability": None, "external_service_snapshot": None}
    response = router.post("/api/cns/service-state/evaluate", deepcopy(payload))
    assert response.data["service_state"] == "not_applicable"
