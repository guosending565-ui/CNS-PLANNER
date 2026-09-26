"""Layered Operational Route Architecture V1 tests.

Covers the extended ``AltitudeLayer`` contract, the additive ``RouteOperatingLayer`` and
``DepartureArrivalProcedure`` contracts, legacy project backfill, the read-only
``RouteOperatingPlan`` projection, the four separately reported readiness buckets, the
minimal invalidation chain and the "no default real altitude / no inferred nominal / no
profile-driven layer matching" rules.  V1/V2/V3 behaviour is intentionally untouched: the
existing characterization suites keep locking it.
"""

from copy import deepcopy
from pathlib import Path

import pytest

from cns_planner.algorithms.grid.service import WorkspaceGridService
from cns_planner.api.router import ApiRouter
from cns_planner.application.project_state import blank_project, normalize_project
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.spatial_3d import (
    normalize_altitude_layer, normalize_departure_arrival_procedure,
    normalize_spatial_3d, procedure_missing_evidence,
)


DEFAULTS = Path("cns_planner/config/defaults.json")
LAYER_ID = "L-LOW"
ROUTE = {"route_id": "R0001", "status": "passed", "path": [[122.0, 30.0], [122.01, 30.0]]}
READINESS_BUCKETS = (
    "altitude_layer_catalog", "route_layer_assignment",
    "departure_procedure", "arrival_procedure",
)


def workflow(tmp_path, *, routes=True):
    service = WorkflowService(tmp_path / "project.json", DEFAULTS)
    if routes:
        service.state["operational_routes"] = [deepcopy(ROUTE)]
        service.state["result_statuses"]["routes"] = "passed"
    return service


def layer(**overrides):
    payload = {
        "altitude_layer_id": LAYER_ID, "name": "低层",
        "nominal_altitude_m": 100.0, "lower_altitude_m": 50.0, "upper_altitude_m": 150.0,
        "vertical_reference": "egm2008_orthometric", "source": "工程确认-测试",
        "evidence": {"note": "unit test"}, "confirmed": True,
    }
    payload.update(overrides)
    return payload


def assignment(**overrides):
    payload = {
        "route_id": "R0001", "altitude_layer_id": LAYER_ID,
        "source": "工程确认-测试", "confirmed": True,
    }
    payload.update(overrides)
    return payload


def departure(**overrides):
    payload = {
        "procedure_id": "DP-1", "procedure_type": "departure", "route_id": "R0001",
        "site_reference": "REF-1", "altitude_layer_id": LAYER_ID,
        "transition_mode": "climb_to_cruise_layer",
        "horizontal_geometry": {"turn_radius_m": 50.0},
        "vertical_profile": {"climb_rate_mps": 2.5},
        "join_leave_point": {"kind": "join", "distance_along_route_m": 200.0},
        "source": "工程确认-测试", "confirmed": True,
    }
    payload.update(overrides)
    return payload


def arrival(**overrides):
    payload = departure(
        procedure_id="AR-1", procedure_type="arrival",
        transition_mode="descend_from_cruise_layer",
        vertical_profile={"descent_rate_mps": 2.0},
        join_leave_point={"kind": "leave", "distance_along_route_m": 800.0},
    )
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------------------
# 1. legacy project backfill + migration
# --------------------------------------------------------------------------------------


def test_legacy_project_backfills_the_additive_containers_and_round_trips(tmp_path):
    project = blank_project({})
    modern_spatial = deepcopy(project["spatial_3d"])
    legacy = deepcopy(project)
    legacy["spatial_3d"].pop("route_operating_layers")
    legacy["spatial_3d"].pop("departure_arrival_procedures")
    normalized = normalize_project(legacy, WorkspaceGridService())
    assert normalized["spatial_3d"]["route_operating_layers"] == []
    assert normalized["spatial_3d"]["departure_arrival_procedures"] == []
    # backfilling a legacy project produces exactly the modern empty form (fixed point)
    assert normalized["spatial_3d"] == modern_spatial

    service = workflow(tmp_path)
    service.state["spatial_3d"].pop("route_operating_layers")
    service.state["spatial_3d"].pop("departure_arrival_procedures")
    service.save()
    restored = WorkflowService(tmp_path / "project.json", DEFAULTS)
    assert restored.state["spatial_3d"]["route_operating_layers"] == []
    assert restored.state["spatial_3d"]["departure_arrival_procedures"] == []
    restored.save()
    again = WorkflowService(tmp_path / "project.json", DEFAULTS)
    assert again.state["spatial_3d"] == restored.state["spatial_3d"]


def test_legacy_altitude_layer_without_a_nominal_stays_pending_unresolved():
    legacy = normalize_altitude_layer({
        "altitude_layer_id": "L1", "lower_altitude_m": 0.0, "upper_altitude_m": 120.0,
        "vertical_reference": "agl", "source": "legacy", "confirmed": True,
        "status": "confirmed",
    })
    assert legacy["nominal_altitude_m"] is None
    assert legacy["status"] == "pending_confirmation"
    # no automatic midpoint, no invented datum, no invented nominal
    assert legacy["nominal_altitude_m"] != (legacy["lower_altitude_m"] + legacy["upper_altitude_m"]) / 2
    assert legacy["vertical_reference"] == "agl"


# --------------------------------------------------------------------------------------
# 2. AltitudeLayer nominal bounds / datum / no inference
# --------------------------------------------------------------------------------------


def test_altitude_layer_nominal_altitude_must_stay_within_the_declared_bounds():
    assert normalize_altitude_layer(layer(nominal_altitude_m=50.0))["status"] == "confirmed"
    assert normalize_altitude_layer(layer(nominal_altitude_m=150.0))["status"] == "confirmed"
    for nominal in (49.9, 150.1):
        with pytest.raises(ValueError, match="nominal_altitude_m"):
            normalize_altitude_layer(layer(nominal_altitude_m=nominal))


def test_unknown_datum_missing_nominal_and_missing_source_all_stay_pending():
    unknown = normalize_altitude_layer(layer(vertical_reference="unknown"))
    assert unknown["status"] == "pending_confirmation"
    assert unknown["confirmed"] is True  # the claim is recorded, the status is not promoted
    without_nominal = normalize_altitude_layer({
        key: value for key, value in layer().items() if key != "nominal_altitude_m"
    })
    assert without_nominal["nominal_altitude_m"] is None
    assert without_nominal["status"] == "pending_confirmation"
    assert normalize_altitude_layer(layer(source=""))["status"] == "pending_confirmation"


def test_altitude_layer_write_requires_explicit_values_and_a_traceable_source(tmp_path):
    service = workflow(tmp_path)
    with pytest.raises(ValueError, match="source"):
        service.set_altitude_layer(layer(source=""))
    with pytest.raises(ValueError, match="lower_altitude_m"):
        service.set_altitude_layer({key: value for key, value in layer().items() if key != "lower_altitude_m"})
    with pytest.raises(ValueError, match="nominal_altitude_m"):
        service.set_altitude_layer(layer(nominal_altitude_m=500.0))
    snapshot = service.set_altitude_layer(layer(nominal_altitude_m=None))
    stored = snapshot["spatial_3d"]["altitude_layers"][0]
    assert stored["nominal_altitude_m"] is None
    assert stored["status"] == "pending_confirmation"
    snapshot = service.set_altitude_layer(layer())
    stored = snapshot["spatial_3d"]["altitude_layers"][0]
    assert stored["nominal_altitude_m"] == 100.0
    assert stored["status"] == "confirmed"
    assert stored["evidence"] == {"note": "unit test"}


# --------------------------------------------------------------------------------------
# 3. RouteOperatingLayer: existence, uniqueness, no profile matching
# --------------------------------------------------------------------------------------


def test_route_layer_assignment_requires_a_known_route_and_a_known_layer(tmp_path):
    service = workflow(tmp_path)
    with pytest.raises(ValueError, match="运行航路不存在"):
        service.set_route_operating_layer(assignment(route_id="R9999"))
    with pytest.raises(ValueError, match="AltitudeLayer 不存在"):
        service.set_route_operating_layer(assignment(altitude_layer_id="L-MISSING"))


def test_one_route_keeps_exactly_one_active_assignment_and_never_two(tmp_path):
    service = workflow(tmp_path)
    service.set_altitude_layer(layer())
    service.set_altitude_layer(layer(
        altitude_layer_id="L-HIGH", name="高层", nominal_altitude_m=200.0,
        lower_altitude_m=150.0, upper_altitude_m=250.0,
    ))
    first = service.set_route_operating_layer(assignment())
    assert len(first["spatial_3d"]["route_operating_layers"]) == 1
    second = service.set_route_operating_layer(assignment(altitude_layer_id="L-HIGH"))
    items = second["spatial_3d"]["route_operating_layers"]
    assert len(items) == 1
    assert items[0]["altitude_layer_id"] == "L-HIGH"
    assert items[0]["operating_mode"] == "fixed_cruise_layer"
    assert items[0]["active"] is True
    assert items[0]["status"] == "confirmed"
    with pytest.raises(ValueError, match="最多只能有一个 active"):
        normalize_spatial_3d({"route_operating_layers": [
            assignment(), assignment(altitude_layer_id="L-HIGH"),
        ]})


def test_assignment_datum_must_match_its_altitude_layer_and_cannot_be_invented(tmp_path):
    service = workflow(tmp_path)
    service.set_altitude_layer(layer())
    with pytest.raises(ValueError, match="vertical_reference"):
        service.set_route_operating_layer(assignment(vertical_reference="agl"))
    # omitted datum is inherited from the referenced layer, never inferred from a profile
    stored = service.set_route_operating_layer({
        "route_id": "R0001", "altitude_layer_id": LAYER_ID,
        "source": "工程确认-测试", "confirmed": True,
    })["spatial_3d"]["route_operating_layers"][0]
    assert stored["vertical_reference"] == "egm2008_orthometric"


def test_a_constant_route_altitude_profile_never_creates_or_matches_a_cruise_layer(tmp_path):
    service = workflow(tmp_path)
    service.set_altitude_layer(layer())
    service.set_route_altitude_profile({
        "route_id": "R0001", "mode": "constant", "vertical_reference": "egm2008_orthometric",
        "constant_altitude_m": 100.0, "source": "user_configuration", "confirmed": True,
    })
    snapshot = service.snapshot()
    assert snapshot["spatial_3d"]["route_operating_layers"] == []
    readiness = snapshot["route_operating_readiness"]
    entry = readiness["route_layer_assignment"]["routes"][0]
    assert readiness["route_layer_assignment"]["status"] == "pending_confirmation"
    assert entry["altitude_layer_id"] is None
    assert entry["nominal_altitude_m"] is None
    route = snapshot["route_operating_plan"]["routes"][0]
    assert route["cruise_layer"]["status"] == "pending_confirmation"
    assert route["cruise_layer"]["nominal_altitude_m"] is None
    assert route["advanced_variable_profile"]["present"] is True
    assert route["advanced_variable_profile"]["semantics"] == "advanced_variable_profile"
    assert route["advanced_variable_profile"]["is_production_cruise_layer"] is False


# --------------------------------------------------------------------------------------
# 4. departure / arrival procedure contract, readiness and CRUD
# --------------------------------------------------------------------------------------


def test_procedure_stays_pending_until_every_explicit_evidence_exists(tmp_path):
    service = workflow(tmp_path)
    service.set_altitude_layer(layer())
    pending = service.set_departure_arrival_procedure(departure(
        altitude_layer_id=None, transition_mode="not_specified",
        vertical_profile={}, horizontal_geometry={}, join_leave_point=None,
    ))
    stored = pending["spatial_3d"]["departure_arrival_procedures"][0]
    assert stored["status"] == "pending_confirmation"
    assert set(stored["missing_evidence"]) == {
        "altitude_layer_id", "transition_mode", "climb_rate_mps", "turn_radius_m", "join_point",
    }
    # nothing was defaulted in
    assert stored["vertical_profile"] == {}
    assert stored["horizontal_geometry"] == {}
    assert stored["join_leave_point"] is None

    missing_rate = service.set_departure_arrival_procedure(departure(vertical_profile={}))
    assert missing_rate["spatial_3d"]["departure_arrival_procedures"][0]["missing_evidence"] == ["climb_rate_mps"]

    wrong_kind = normalize_departure_arrival_procedure(departure(
        join_leave_point={"kind": "leave", "distance_along_route_m": 200.0},
    ))
    assert wrong_kind["missing_evidence"] == ["join_point"]

    confirmed = service.set_departure_arrival_procedure(arrival())
    procedures = confirmed["spatial_3d"]["departure_arrival_procedures"]
    arrival_procedure = [item for item in procedures if item["procedure_type"] == "arrival"][0]
    assert arrival_procedure["status"] == "confirmed"
    assert arrival_procedure["missing_evidence"] == []
    assert confirmed["route_operating_readiness"]["arrival_procedure"]["status"] == "confirmed"


def test_procedure_requires_an_explicit_type_route_node_and_known_layer(tmp_path):
    service = workflow(tmp_path)
    service.set_altitude_layer(layer())
    with pytest.raises(ValueError, match="procedure_type"):
        service.set_departure_arrival_procedure(departure(procedure_type="cruise"))
    with pytest.raises(ValueError, match="运行航路不存在"):
        service.set_departure_arrival_procedure(departure(route_id="R9999"))
    with pytest.raises(ValueError, match="AltitudeLayer 不存在"):
        service.set_departure_arrival_procedure(departure(altitude_layer_id="L-MISSING"))
    with pytest.raises(ValueError, match="node 不存在"):
        service.set_departure_arrival_procedure(departure(node_id="N-MISSING"))
    with pytest.raises(ValueError, match="transition_mode"):
        service.set_departure_arrival_procedure(departure(transition_mode="teleport"))
    with pytest.raises(ValueError, match="procedure_type"):
        normalize_departure_arrival_procedure({"procedure_id": "X", "route_id": "R0001"})


def test_confirmed_requires_an_explicit_traceable_source_for_every_contract(tmp_path):
    service = workflow(tmp_path)
    with pytest.raises(ValueError, match="source"):
        service.set_altitude_layer(layer(source=""))
    service.set_altitude_layer(layer())
    with pytest.raises(ValueError, match="source"):
        service.set_route_operating_layer(assignment(source=""))
    service.set_route_operating_layer(assignment())
    with pytest.raises(ValueError, match="source"):
        service.set_departure_arrival_procedure(departure(source=""))


def test_procedure_missing_evidence_helper_is_pure_and_explicit():
    assert procedure_missing_evidence({
        "procedure_type": "arrival", "altitude_layer_id": "L1", "node_id": "N1",
        "transition_mode": "level_transition", "vertical_profile": {"descent_rate_mps": 1.0},
        "horizontal_geometry": {"turn_radius_m": 10.0},
        "join_leave_point": {"kind": "leave"},
    }) == []


# --------------------------------------------------------------------------------------
# 5. V3-D locked / advanced profile semantics survive
# --------------------------------------------------------------------------------------


def test_v3d_waypoint_locked_profile_keeps_its_semantics_and_is_never_converted(tmp_path):
    service = workflow(tmp_path)
    service.state["spatial_3d"]["route_altitude_profiles"]["R0001"] = {
        "route_id": "R0001", "mode": "waypoint_linear", "vertical_reference": "egm2008_orthometric",
        "constant_altitude_m": None,
        "waypoints": [
            {"distance_along_route_m": 0.0, "altitude_m": 100.0},
            {"distance_along_route_m": 1000.0, "altitude_m": 120.0},
        ],
        "source": "v3c_validated_route", "confirmed": True, "derived": True, "locked": True,
        "locked_by_adoption": True, "adoption_owned": True,
        "profile_semantics": "v3c_validated_route",
        "distance_basis": "cumulative_2d_baseline_path_distance_matching_path_vertex_order",
    }
    service.save()
    restored = WorkflowService(tmp_path / "project.json", DEFAULTS)
    profile = restored.state["spatial_3d"]["route_altitude_profiles"]["R0001"]
    assert profile["mode"] == "waypoint_linear"
    assert profile["derived"] is True
    assert profile["locked"] is True
    assert profile["locked_by_adoption"] is True
    assert profile["adoption_owned"] is True
    with pytest.raises(ValueError, match="锁定"):
        restored.set_route_altitude_profile({
            "route_id": "R0001", "mode": "constant", "vertical_reference": "egm2008_orthometric",
            "constant_altitude_m": 100.0, "source": "user_configuration", "confirmed": True,
        })
    # the additive containers never turn an advanced/V3-D profile into a cruise layer
    assert restored.state["spatial_3d"]["route_operating_layers"] == []
    route = restored.route_operating_plan()["routes"][0]
    assert route["advanced_variable_profile"]["v3c_validated_route"] is True
    assert route["advanced_variable_profile"]["locked_by_adoption"] is True
    assert route["advanced_variable_profile"]["is_production_cruise_layer"] is False
    assert route["cruise_layer"]["status"] == "pending_confirmation"
    assert route["cruise_layer"]["nominal_altitude_m"] is None


# --------------------------------------------------------------------------------------
# 6. reference consistency on delete/edit + readiness downgrade
# --------------------------------------------------------------------------------------


def test_delete_and_edit_of_a_referenced_layer_stay_reference_consistent(tmp_path):
    service = workflow(tmp_path)
    service.set_altitude_layer(layer())
    service.set_route_operating_layer(assignment())
    service.set_departure_arrival_procedure(departure())
    with pytest.raises(ValueError, match="仍被引用"):
        service.delete_altitude_layer({"altitude_layer_id": LAYER_ID})
    with pytest.raises(ValueError, match="vertical_reference"):
        service.set_altitude_layer(layer(vertical_reference="agl"))
    # explicit reference removal is the only way to delete the layer
    service.delete_departure_arrival_procedure({"procedure_id": "DP-1"})
    service.delete_route_operating_layer({"route_id": "R0001"})
    snapshot = service.delete_altitude_layer({"altitude_layer_id": LAYER_ID})
    assert snapshot["spatial_3d"]["altitude_layers"] == []
    with pytest.raises(ValueError, match="不存在"):
        service.delete_altitude_layer({"altitude_layer_id": LAYER_ID})
    with pytest.raises(ValueError, match="尚未配置"):
        service.delete_route_operating_layer({"route_id": "R0001"})
    with pytest.raises(ValueError, match="不存在"):
        service.delete_departure_arrival_procedure({"procedure_id": "DP-1"})


def test_a_layer_that_loses_its_nominal_downgrades_every_reference(tmp_path):
    service = workflow(tmp_path)
    service.set_altitude_layer(layer())
    service.set_route_operating_layer(assignment())
    service.set_departure_arrival_procedure(departure())
    assert service.route_operating_readiness()["route_layer_assignment"]["status"] == "confirmed"
    snapshot = service.set_altitude_layer(layer(nominal_altitude_m=None))
    spatial = snapshot["spatial_3d"]
    assert spatial["altitude_layers"][0]["nominal_altitude_m"] is None
    assert spatial["altitude_layers"][0]["status"] == "pending_confirmation"
    assert spatial["route_operating_layers"][0]["status"] == "pending_confirmation"
    assert spatial["departure_arrival_procedures"][0]["status"] == "pending_confirmation"
    assert snapshot["route_operating_readiness"]["route_layer_assignment"]["status"] == "pending_confirmation"
    assert snapshot["route_operating_readiness"]["departure_procedure"]["status"] == "pending_confirmation"


# --------------------------------------------------------------------------------------
# 7. readiness buckets + read-only plan projection
# --------------------------------------------------------------------------------------


def test_readiness_separates_four_buckets_and_never_reads_missing_as_zero_or_unsafe(tmp_path):
    service = workflow(tmp_path)
    readiness = service.route_operating_readiness()
    assert set(READINESS_BUCKETS) <= set(readiness)
    for bucket in READINESS_BUCKETS:
        assert readiness[bucket]["status"] == "pending_confirmation"
        assert readiness[bucket]["reasons"]
    assert readiness["semantics"]["missing_value_is_pending_not_unsafe_and_not_zero"] is True
    assert readiness["semantics"]["no_default_altitude_or_vertical_datum_is_invented"] is True
    entry = readiness["route_layer_assignment"]["routes"][0]
    assert entry["altitude_layer_id"] is None
    assert entry["nominal_altitude_m"] is None
    assert entry["nominal_altitude_m"] != 0
    assert all("unsafe" not in reason for reason in entry["reasons"])


def test_route_operating_plan_is_a_read_only_projection_with_separated_contracts(tmp_path):
    service = workflow(tmp_path)
    service.set_altitude_layer(layer())
    service.set_route_operating_layer(assignment())
    service.set_departure_arrival_procedure(departure())
    service.set_departure_arrival_procedure(arrival())
    before = deepcopy(service.state["spatial_3d"])
    plan = service.route_operating_plan()
    assert service.state["spatial_3d"] == before
    assert plan["production_route_definition"].startswith("DepartureProcedure")
    assert plan["semantics"]["cruise_and_terminal_transition_are_separate_contracts"] is True
    assert plan["semantics"]["read_only_projection"] is True
    route = plan["routes"][0]
    assert set(route) >= {"horizontal_route", "cruise_layer", "terminal_transition", "advanced_variable_profile"}
    assert route["cruise_layer"]["altitude_layer_id"] == LAYER_ID
    assert route["cruise_layer"]["operating_mode"] == "fixed_cruise_layer"
    assert route["cruise_layer"]["nominal_altitude_m"] == 100.0
    assert route["terminal_transition"]["departure"]["procedure_id"] == "DP-1"
    assert route["terminal_transition"]["arrival"]["procedure_id"] == "AR-1"
    assert route["status"] == "confirmed"
    assert plan["status"] == "passed"
    assert plan["altitude_layer_catalog"][0]["altitude_layer_id"] == LAYER_ID


# --------------------------------------------------------------------------------------
# 8. API surface
# --------------------------------------------------------------------------------------


class ApiContext:
    def __init__(self, workflow):
        self.workflow = workflow
        self.data = object()


def test_api_exposes_explicit_crud_readiness_and_plan(tmp_path):
    service = workflow(tmp_path)
    router = ApiRouter(ApiContext(service))
    router.post("/api/spatial-3d/altitude-layer", layer())
    router.post("/api/spatial-3d/route-operating-layer", assignment())
    router.post("/api/spatial-3d/departure-arrival-procedure", departure())
    readiness = router.get("/api/spatial-3d/readiness", {}, {}).data
    assert set(READINESS_BUCKETS) <= set(readiness)
    assert readiness["altitude_layer_catalog"]["status"] == "confirmed"
    plan = router.get("/api/route-operating-plan", {}, {}).data
    assert plan["routes"][0]["cruise_layer"]["altitude_layer_id"] == LAYER_ID
    spatial = router.get("/api/spatial-3d", {}, {}).data
    assert spatial["route_operating_layers"][0]["route_id"] == "R0001"
    assert spatial["departure_arrival_procedures"][0]["procedure_id"] == "DP-1"
    snapshot = router.get("/api/workflow", {}, {}).data
    assert "route_operating_readiness" in snapshot
    assert "route_operating_plan" in snapshot
    router.post("/api/spatial-3d/departure-arrival-procedure/delete", {"procedure_id": "DP-1"})
    router.post("/api/spatial-3d/route-operating-layer/delete", {"route_id": "R0001"})
    router.post("/api/spatial-3d/altitude-layer/delete", {"altitude_layer_id": LAYER_ID})
    snapshot = router.get("/api/workflow", {}, {}).data
    assert snapshot["spatial_3d"]["altitude_layers"] == []
    assert snapshot["spatial_3d"]["route_operating_layers"] == []
    assert snapshot["spatial_3d"]["departure_arrival_procedures"] == []


def test_legacy_multi_layer_endpoint_still_works_and_resyncs_references(tmp_path):
    service = workflow(tmp_path)
    service.set_altitude_layer(layer())
    service.set_route_operating_layer(assignment())
    snapshot = service.set_altitude_layers({"altitude_layers": [{
        "altitude_layer_id": LAYER_ID, "lower_altitude_m": 50.0, "upper_altitude_m": 150.0,
        "vertical_reference": "egm2008_orthometric", "source": "legacy_endpoint",
        "confirmed": True,
    }]})
    assert snapshot["spatial_3d"]["altitude_layers"][0]["nominal_altitude_m"] is None
    assert snapshot["spatial_3d"]["route_operating_layers"][0]["status"] == "pending_confirmation"


# --------------------------------------------------------------------------------------
# 9. invalidation: minimal chain, never grid risk and never routes
# --------------------------------------------------------------------------------------


def test_layer_assignment_and_procedure_changes_stale_only_the_minimal_chain(tmp_path):
    service = workflow(tmp_path)
    results = (
        "coverage_3d", "building_clearance", "route_vertical_profiles",
        "cns_service_capability", "service_timeline", "cns_corridor_assessment",
    )
    # B7X：Gap V2 是只读 compatibility 结果，失效只在 runtime-only cache 里，
    # 不再改写遗留的 result_statuses。
    compatibility_only = "cns_gap_v2"
    service.state["coverage_3d"] = {"status": "passed"}
    service.state["building_clearance_assessment"] = {"status": "passed"}
    service.state["route_vertical_profiles"] = {"status": "passed"}
    service.state["cns_service_capability"] = {"status": "passed"}
    service.state["service_timeline"] = {"status": "passed"}
    service.state["cns_gap_analysis_v2"] = {"status": "passed"}
    service.state["cns_corridor_assessment"] = {"status": "passed"}
    service.state["grid_risk"] = {"status": "passed"}
    service.state["result_statuses"].update(
        {name: "passed" for name in results}
    )
    service.state["result_statuses"].update({"environment_risk": "passed", "routes": "passed"})

    service.set_altitude_layer(layer())
    for name in results:
        assert service.state["result_statuses"][name] == "stale", name
    # B7X：Gap V2 是只读 compatibility 结果，失效只发生在 runtime-only cache，
    # 遗留的 result_statuses 记录不会被创建或改写。
    assert compatibility_only not in service.state["result_statuses"]
    assert service.state["grid_risk"]["status"] == "passed"
    assert service.state["result_statuses"]["environment_risk"] == "passed"
    assert service.state["result_statuses"]["routes"] == "passed"
    assert service.state["operational_routes"][0]["status"] == "passed"

    service.state["result_statuses"].update(
        {name: "passed" for name in results}
    )
    service.set_route_operating_layer(assignment())
    for name in results:
        assert service.state["result_statuses"][name] == "stale", name
    assert service.state["grid_risk"]["status"] == "passed"


def test_frontend_sources_never_ship_a_default_real_altitude():
    workflow_dir = Path("cns_planner/web/js/workflow")
    sources = {
        name: (workflow_dir / name).read_text(encoding="utf-8")
        for name in ("step02_workspace.js", "step03_routes.js", "route_operating_layer.js")
    }
    for name, source in sources.items():
        for forbidden in ('value="80"', 'value="100"', 'value="120"', 'value="150"'):
            assert forbidden not in source, f"{name} ships {forbidden}"
    panel = sources["route_operating_layer.js"]
    assert "待工程确认" in panel
    assert "fixed_cruise_layer" in panel
    assert "route_operating_readiness" in panel
    step03 = sources["step03_routes.js"]
    assert "renderCruiseLayerPanel(flow)" in step03
    assert "route_operating_layer.js" in step03
    assert "advanced_variable_profile" in step03
    step02 = sources["step02_workspace.js"]
    assert "nominal 高度" in step02
