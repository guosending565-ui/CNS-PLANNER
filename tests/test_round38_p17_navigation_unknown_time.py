from copy import deepcopy

from cns_planner.application.continuous_service_service import ContinuousServiceService
from cns_planner.application.result_currentness import (
    apply_algorithm_semantics_stale,
    effective_result_status,
)
from cns_planner.domain.cns_continuous_service import (
    CONTINUOUS_SERVICE_ALGORITHM_ID,
    CONTINUOUS_SERVICE_ALGORITHM_VERSION,
)
from test_continuous_service_acceptability import _navigation_result, _subsystem_entry


def test_gnss_precision_acceptance_survives_but_unknown_time_is_excluded_from_entire_acceptance_chain():
    result = _navigation_result(
        rtk="not_available", gnss="available", accuracy=8.0, containment=10.0,
    )
    route = result["routes"][0]
    subsystem = _subsystem_entry(result, "N")
    event = subsystem["events"][0]

    assert subsystem["navigation_state"] == "acceptable_degraded"
    assert subsystem["gnss_accuracy_m"] == 8.0
    assert subsystem["route_containment_accuracy_m"] == 10.0
    assert event["duration_s"] is None
    assert event["limit_s"] is None
    assert event["exceeds_limit"] is None

    assert subsystem["status"] == "unknown"
    assert subsystem["managed_gaps"] == []
    assert all(item["event_id"] != event["event_id"] for item in route["managed_gaps"])
    assert result["managed_gap_count"] == len(route["managed_gaps"])
    assert all(item.get("kind") != "navigation_degradation" for item in route["managed_gaps"])
    assert all("N:route_containment" not in line for line in result["disclosure_lines"])
    assert route["status"] == "unknown"
    assert result["status"] == "unknown"

    gate_service = object.__new__(ContinuousServiceService)
    gate_service.result_snapshot = lambda: deepcopy(result)
    assert gate_service.step6_gate()["confirmation_allowed"] is False
    assert gate_service.projected_step6_gate(result)["confirmation_allowed"] is False


def test_p17_2_3_is_read_only_stale_and_2_5_is_current():
    assert CONTINUOUS_SERVICE_ALGORITHM_VERSION == "2.5"
    stored = {
        "status": "acceptable_with_managed_gap",
        "algorithm_id": CONTINUOUS_SERVICE_ALGORITHM_ID,
        "algorithm_version": "2.3",
        "managed_gap_count": 1,
    }
    original = deepcopy(stored)
    projected = apply_algorithm_semantics_stale(
        stored, CONTINUOUS_SERVICE_ALGORITHM_ID, CONTINUOUS_SERVICE_ALGORITHM_VERSION,
    )
    assert projected["status"] == "stale"
    assert projected["algorithm_semantics_stale"]["stored_algorithm_version"] == "2.3"
    assert projected["algorithm_semantics_stale"]["current_algorithm_version"] == "2.5"
    assert stored == original
    state = {"continuous_service_acceptability": stored, "result_statuses": {"continuous_service_acceptability": "passed"}}
    assert effective_result_status(state, "continuous_service_acceptability") == "stale"
    assert state["continuous_service_acceptability"] == original

    current = {**stored, "algorithm_version": "2.5"}
    assert apply_algorithm_semantics_stale(
        current, CONTINUOUS_SERVICE_ALGORITHM_ID, CONTINUOUS_SERVICE_ALGORITHM_VERSION,
    ) is current
