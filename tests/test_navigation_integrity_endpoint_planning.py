from copy import deepcopy

from cns_planner.domain.cns_inputs import normalize_required_cns
from cns_planner.domain.cns_service_contract import SERVICE_KEY_NAVIGATION_INTEGRITY_MONITORING
from cns_planner.domain.navigation_integrity_monitoring import (
    build_navigation_integrity_endpoint_evidence,
    endpoint_gap_evidence,
    navigation_integrity_monitor_actions,
)


KEY = SERVICE_KEY_NAVIGATION_INTEGRITY_MONITORING
ROUTE = {
    "route_id": "R1", "status": "passed", "start_node_id": "A", "end_node_id": "B",
    "path": [[122.0, 30.0], [122.5, 30.0]],
}


def requirement():
    return normalize_required_cns({"project_default": {
        "communication": {"required": True, "confirmed": True},
        "navigation": {"required": True, "confirmed": True, "services": {KEY: {
            "required": True, "confirmed": True,
        }}},
        "surveillance": {"required": False},
    }})


def facility(site, installed=True):
    return {
        "facility_id": f"F-{site}", "site_id": site,
        "distinct_site_id": f"takeoff_landing_site:{site}",
        "coordinate": ROUTE["path"][0 if site == "A" else -1], "status": "active",
        "devices": ([{"device_id": f"NAV-{site}", "service_key": KEY, "status": "active"}]
                    if installed else []),
    }


def delivery(origin="satisfied", destination="satisfied"):
    return {"R1": {"origin": origin, "destination": destination}}


def evidence(items, *, route=ROUTE, comm=None):
    return build_navigation_integrity_endpoint_evidence(
        requirement(), [route], existing_facilities={"items": items},
        communication_delivery=delivery() if comm is None else comm,
    )


def test_two_endpoints_each_require_one_distinct_monitor():
    result = evidence([facility("A"), facility("B")])
    endpoints = result["routes"][0]["endpoints"]
    assert result["status"] == "satisfied"
    assert endpoints["origin"]["required_count"] == endpoints["destination"]["required_count"] == 1
    assert endpoints["origin"]["distinct_site_ids"] != endpoints["destination"]["distinct_site_ids"]


def test_one_physical_site_cannot_satisfy_both_endpoints_even_when_contexts_overlap():
    same = {**deepcopy(ROUTE), "end_node_id": "A"}
    result = evidence([facility("A")], route=same)
    assert result["status"] == "confirmed_deficit"
    assert all(item["status"] == "confirmed_deficit"
               for item in result["routes"][0]["endpoints"].values())


def test_only_missing_origin_produces_only_origin_action():
    actions = navigation_integrity_monitor_actions(endpoint_gap_evidence(evidence([facility("B")])))
    assert [item["endpoint_role"] for item in actions] == ["origin"]
    assert actions[0]["takeoff_landing_site_id"] == "A"


def test_only_missing_destination_produces_only_destination_action():
    actions = navigation_integrity_monitor_actions(endpoint_gap_evidence(evidence([facility("A")])))
    assert [item["endpoint_role"] for item in actions] == ["destination"]
    assert actions[0]["takeoff_landing_site_id"] == "B"


def test_corridor_middle_never_generates_navigation_gap_or_continuous_gap():
    gap = endpoint_gap_evidence(evidence([]))
    route = gap["routes"][0]
    assert route["continuous_gap_m"] is None
    assert {item["endpoint_role"] for item in route["confirmed_endpoint_gaps"]} == {
        "origin", "destination",
    }


def test_communication_delivery_dependency_is_fail_closed():
    result = evidence([facility("A"), facility("B")], comm={})
    assert result["status"] == "unknown"
    assert all(item["delivery_status"] == "unknown"
               for item in result["routes"][0]["endpoints"].values())


def test_10km_is_monitoring_context_never_rtk_baseline():
    planning = result = requirement()["project_default"]["navigation"]["services"][KEY]["planning"]
    assert planning["local_monitoring_radius_m"] == 10000.0
    assert "not_rtk_baseline" in planning["local_monitoring_radius_semantics"]
    assert "max_reference_baseline_m" not in planning
    assert result["corridor_monitoring_required"] is False
