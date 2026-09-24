from copy import deepcopy
from pathlib import Path

import pytest

from cns_planner.application.project_state import normalize_project
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.algorithms.grid.service import WorkspaceGridService
from cns_planner.domain.layered_route_validation import (
    ALGORITHM_VERSION, VALIDATOR_VERSIONS, validation_fingerprint,
)
from cns_planner.domain.spatial_3d import effective_route_vertical_context


DEFAULTS = Path(__file__).parents[1] / "cns_planner" / "config" / "defaults.json"


def prepared(tmp_path):
    service = WorkflowService(tmp_path / "project.json", DEFAULTS)
    candidate = {
        "candidate_id": "LC-1", "route_id": "R-1", "altitude_layer_id": "L-100",
        "status": "candidate", "current_applicability": "current",
        "candidate_fingerprint": "candidate-fp", "path": [[122.0, 30.0], [122.001, 30.0]],
    }
    candidates = {
        "status": "passed", "count": 1, "active_candidate_id": "LC-1",
        "items": [candidate], "masks": {},
    }
    profile = {
        "profile_id": "RRP-1", "status": "passed", "current_applicability": "current",
        "candidate": {"candidate_id": "LC-1", "candidate_fingerprint": "candidate-fp"},
    }
    service.layered_route_planner_service.result_snapshot = lambda: deepcopy(candidates)
    service.route_risk_profile_service.result_snapshot = lambda: {
        "status": "passed", "count": 1, "items": [deepcopy(profile)]
    }
    service.state["scenario_routes"] = [{
        "route_id": "R-1", "start": [122.0, 30.0], "end": [122.001, 30.0],
        "start_node_id": "A", "end_node_id": "B",
    }]
    service.state["spatial_3d"]["altitude_layers"] = [{
        "altitude_layer_id": "L-100", "name": "100m", "nominal_altitude_m": 100.0,
        "lower_altitude_m": 90.0, "upper_altitude_m": 110.0,
        "vertical_reference": "egm2008_orthometric", "source": "engineering",
        "evidence": {"ticket": "T-1"}, "confirmed": True, "status": "confirmed",
    }]
    service.state["layered_route_feasibility_policy"] = {
        "terrain_vertical_clearance_m": 10.0, "source": "engineering",
        "confirmed": True, "status": "confirmed",
    }
    service.state["building_clearance_policy"] = {
        "horizontal_clearance_m": 5.0, "vertical_clearance_m": 10.0,
        "source": "engineering", "confirmed": True, "status": "confirmed",
        "vertical_reference": "egm2008_orthometric",
    }
    service.state["source_audits"] = {
        "status": "passed", "count": 2, "items": {
            "terrain_dtm": {"role": "terrain_dtm", "status": "verified", "file_name": "dtm.tif",
                            "verification": {"sha256": "terrain-sha"}},
            "buildings": {"role": "buildings", "status": "verified", "file_name": "b.gpkg",
                          "verification": {"sha256": "building-sha"}},
        },
    }
    return service, candidate, profile


def evidence(*, terrain=80.0, nodata=False, buildings=None, source_suffix="a"):
    def build(**kwargs):
        return {
            "adapter_id": "test-real-source-adapter",
            "source_type": "configured_real_sources",
            "metric_path": [[0.0, 0.0], [100.0, 0.0]], "metric_crs": "EPSG:32651",
            "to_geographic": lambda point: [122.0 + point[0] / 100000.0, 30.0],
            "terrain": {
                "available": True,
                "source": {"crs": "EPSG:32651", "nodata": -9999, "id": source_suffix},
                "pixels": [{
                    "pixel": [1, 2], "interval": {"start_distance_m": 0.0, "end_distance_m": 100.0},
                    "data_status": "unknown" if nodata else "passed",
                    "elevation_egm2008_m": None if nodata else terrain,
                    "source_value": None if nodata else terrain,
                    "reason": "native_terrain_pixel_nodata" if nodata else None,
                }],
            },
            "buildings": {
                "available": True, "source": {"id": source_suffix},
                "buildings": deepcopy(buildings or []),
            },
            "sources": {
                "terrain_dtm": {"id": "terrain-" + source_suffix},
                "buildings": {"id": "buildings-" + source_suffix},
                "metric_frame": {"horizontal_crs": "EPSG:32651"},
            },
            "sample_count": 1 + len(buildings or []),
        }
    return build


def latest(service):
    return service.layered_route_validation_service.result_snapshot()["items"][-1]


def test_constant_egm2008_candidate_native_terrain_pass_fail_and_nodata(tmp_path):
    service, _, _ = prepared(tmp_path)
    service.evaluate_layered_route_validation({}, evidence_adapter=evidence(terrain=80.0))
    passed = latest(service)
    assert passed["status"] == "validated_candidate"
    assert passed["route"]["nominal_altitude_m"] == 100.0
    assert passed["route"]["semantics"] == "strategic_route_centerline_not_aircraft_kinematic_trajectory"
    assert passed["operational_route"] is False and passed["cns_assessed"] is False

    service.evaluate_layered_route_validation({}, evidence_adapter=evidence(terrain=95.0))
    assert latest(service)["status"] == "failed"
    assert latest(service)["domains"]["terrain"]["minimum_margin"] == -5.0

    service.evaluate_layered_route_validation({}, evidence_adapter=evidence(nodata=True))
    unresolved = latest(service)
    assert unresolved["status"] == "unresolved"
    assert unresolved["unresolved_intervals"][0]["reason_id"] == "native_terrain_pixel_nodata"


def test_real_building_footprint_pass_fail_missing_height_and_no_l8_verdict(tmp_path):
    service, _, _ = prepared(tmp_path)
    ring = [[40, -2], [60, -2], [60, 2], [40, 2], [40, -2]]
    service.state["grid_attributes"] = {"buildings": {"height_max": 99999}}
    service.evaluate_layered_route_validation({}, evidence_adapter=evidence(buildings=[{
        "building_id": "B1", "ring_metric": ring, "ground_elevation_max_egm2008_m": 50.0,
        "height_m": 20.0, "source": "real-footprint",
    }]))
    assert latest(service)["status"] == "validated_candidate"
    assert latest(service)["semantics"]["coarse_l8_building_height_not_a_final_verdict"] is True

    service.evaluate_layered_route_validation({}, evidence_adapter=evidence(buildings=[{
        "building_id": "B2", "ring_metric": ring, "ground_elevation_max_egm2008_m": 80.0,
        "height_m": 15.0, "source": "real-footprint",
    }]))
    assert latest(service)["status"] == "failed"

    service.evaluate_layered_route_validation({}, evidence_adapter=evidence(buildings=[{
        "building_id": "B3", "ring_metric": ring, "ground_elevation_max_egm2008_m": 50.0,
        "height_m": None, "source": "real-footprint",
    }]))
    assert latest(service)["status"] == "unresolved"


def test_airspace_and_profile_thresholds_are_outside_validation_fingerprint(tmp_path):
    service, _, _ = prepared(tmp_path)
    service.evaluate_layered_route_validation({}, evidence_adapter=evidence())
    first = latest(service)["fingerprints"]["validation_fingerprint"]
    service.state["airspace_policies"] = {"status": "changed", "items": [{"id": "NO"}]}
    service.state["route_risk_profile_policy"] = {"domains": {"ground": {"high_min": 0.9}}}
    assert latest(service)["fingerprints"]["validation_fingerprint"] == first
    assert latest(service)["current_applicability"] == "current"


def test_resource_limit_is_incomplete_not_failed(tmp_path):
    service, _, _ = prepared(tmp_path)
    service.evaluate_layered_route_validation(
        {"resource_limits": {"max_evidence_items": 1}},
        evidence_adapter=evidence(buildings=[{
            "building_id": "outside", "ring_metric": [[200, 10], [210, 10], [210, 20], [200, 10]],
            "ground_elevation_max_egm2008_m": 0.0, "height_m": 1.0,
        }]),
    )
    assert latest(service)["status"] == "validation_incomplete"


def test_resource_limit_from_adapter_is_incomplete(tmp_path):
    service, _, _ = prepared(tmp_path)
    base = evidence()
    def limited(**kwargs):
        result = base(**kwargs)
        result["resource_limit_exceeded"] = True
        return result
    service.evaluate_layered_route_validation({}, evidence_adapter=limited)
    assert latest(service)["status"] == "validation_incomplete"


def test_preview_apply_assignment_conflict_rollback_and_revoke_ownership(tmp_path, monkeypatch):
    service, _, _ = prepared(tmp_path)
    service.evaluate_layered_route_validation({}, evidence_adapter=evidence())
    validation = latest(service)
    validation_id = validation["validation_id"]
    before = deepcopy(service.state)
    preview = service.preview_layered_operational_adoption({"validation_id": validation_id})
    assert preview["status"] == "ready" and preview["side_effects"] is False
    assert service.state == before

    applied = service.apply_layered_operational_adoption({
        "validation_id": validation_id, "confirmed": True,
        "expected_validation_fingerprint": validation["fingerprints"]["validation_fingerprint"],
    })
    route = next(item for item in service.state["operational_routes"] if item["route_id"] == "R-1")
    assert all(len(point) == 2 for point in route["path"])
    assignment = service.state["spatial_3d"]["route_operating_layers"][0]
    assert assignment["altitude_layer_id"] == "L-100"
    assert assignment["operating_mode"] == "fixed_cruise_layer"
    assert service.state["spatial_3d"]["route_altitude_profiles"] == {}
    assert service.state["spatial_3d"]["departure_arrival_procedures"] == []
    assert validation["status"] == "validated_candidate"

    # A later manual edit breaks ownership; revoke must preserve it.
    route["provenance"] = {"source_type": "manual"}
    service.revoke_layered_operational_adoption({
        "adoption_id": applied["adoption_id"], "confirmed": True,
    })
    assert next(item for item in service.state["operational_routes"] if item["route_id"] == "R-1")[
        "provenance"
    ]["source_type"] == "manual"


def test_existing_foreign_route_requires_explicit_replace_and_restores_on_revoke(tmp_path):
    service, _, _ = prepared(tmp_path)
    foreign = {"route_id": "R-1", "status": "passed", "path": [[1, 2], [3, 4]],
               "provenance": {"source_type": "manual"}}
    service.state["operational_routes"] = [deepcopy(foreign)]
    service.evaluate_layered_route_validation({}, evidence_adapter=evidence())
    validation_id = latest(service)["validation_id"]
    preview = service.preview_layered_operational_adoption({"validation_id": validation_id})
    assert preview["projection"]["conflict"] is not None
    assert preview["publication_allowed"] is False
    with pytest.raises(ValueError, match="replace_existing"):
        service.apply_layered_operational_adoption({
            "validation_id": validation_id, "confirmed": True,
        })
    applied = service.apply_layered_operational_adoption({
        "validation_id": validation_id, "confirmed": True, "replace_existing": True,
    })
    service.revoke_layered_operational_adoption({
        "adoption_id": applied["adoption_id"], "confirmed": True,
    })
    assert next(item for item in service.state["operational_routes"] if item["route_id"] == "R-1") == foreign


def test_apply_transaction_rolls_back_and_publication_does_not_stale_candidate_or_v3(tmp_path, monkeypatch):
    service, candidate, _ = prepared(tmp_path)
    service.state["route_planner_v3_experiments"] = {"status": "passed", "records": [{"status": "passed"}]}
    service.evaluate_layered_route_validation({}, evidence_adapter=evidence())
    validation_id = latest(service)["validation_id"]
    before = deepcopy(service.state)
    monkeypatch.setattr(service.layered_operational_adoption_service, "_normalize", lambda state: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        service.apply_layered_operational_adoption({"validation_id": validation_id, "confirmed": True})
    assert service.state == before


def test_evidence_change_stales_owned_route_but_profile_threshold_change_does_not(tmp_path):
    service, _, _ = prepared(tmp_path)
    service.evaluate_layered_route_validation({}, evidence_adapter=evidence())
    validation_id = latest(service)["validation_id"]
    service.apply_layered_operational_adoption({"validation_id": validation_id, "confirmed": True})
    service.invalidation_service.route_risk_profile("threshold_changed")
    assert service.state["layered_operational_adoptions"]["items"][-1]["status"] == "published"
    service.invalidation_service.layered_route_validation("terrain_source_changed")
    assert service.state["layered_operational_adoptions"]["items"][-1]["status"] == "stale"
    assert next(item for item in service.state["operational_routes"] if item["route_id"] == "R-1")["status"] == "stale"
    assert service.state["route_planner_v3_experiments"].get("status") != "stale"


def test_effective_vertical_context_prefers_production_and_falls_back_to_profile():
    spatial = {
        "altitude_layers": [{
            "altitude_layer_id": "L", "nominal_altitude_m": 120.0,
            "vertical_reference": "egm2008_orthometric", "confirmed": True,
            "status": "confirmed", "evidence": {},
        }],
        "route_operating_layers": [{
            "route_id": "R", "altitude_layer_id": "L", "operating_mode": "fixed_cruise_layer",
            "vertical_reference": "egm2008_orthometric", "confirmed": True,
            "status": "confirmed", "active": True, "evidence": {},
        }],
        "route_altitude_profiles": {"R": {
            "route_id": "R", "mode": "constant", "constant_altitude_m": 999.0,
            "vertical_reference": "egm2008_orthometric", "confirmed": True, "status": "confirmed",
        }},
    }
    production = effective_route_vertical_context(spatial, "R")
    assert production["constant_altitude_m"] == 120.0
    assert production["source_kind"] == "production_fixed_cruise_layer"
    spatial["route_operating_layers"] = []
    fallback = effective_route_vertical_context(spatial, "R")
    assert fallback["constant_altitude_m"] == 999.0
    assert fallback["fallback_profile_used"] is True


def test_legacy_backfill_is_idempotent(tmp_path):
    service = WorkflowService(tmp_path / "legacy.json", DEFAULTS)
    legacy = deepcopy(service.state)
    legacy.pop("layered_route_validations", None)
    legacy.pop("layered_operational_adoptions", None)
    legacy.get("result_statuses", {}).pop("layered_route_validation", None)
    first = normalize_project(legacy, WorkspaceGridService())
    second = normalize_project(deepcopy(first), WorkspaceGridService())
    assert first["layered_route_validations"] == second["layered_route_validations"]
    assert first["layered_operational_adoptions"] == second["layered_operational_adoptions"]


# --------------------------------------------------------------------------- invalidation


def test_aircraft_profile_change_preserves_layered_adoption_owned_route(tmp_path):
    service = WorkflowService(tmp_path / "project.json", DEFAULTS)
    route = {
        "route_id": "R0003", "status": "passed",
        "path": [[122.0, 30.0], [122.001, 30.0]],
        "provenance": {
            "source_type": "layered_candidate_operational_adoption_v1",
            "adoption_id": "LRA-TEST",
            "adoption_fingerprint": "layeredadoptionv1-test",
        },
    }
    service.state["operational_routes"] = [route]
    original_path = deepcopy(route["path"])
    original_provenance = deepcopy(route["provenance"])

    service.invalidation_service.workflow("aircraft_profile")

    assert route["status"] == "passed"
    assert route["path"] == original_path
    assert route["provenance"] == original_provenance
    assert "stale_reason" not in route

    route["status"] = "stale"
    route["stale_reason"] = "terrain_source_changed"
    service.invalidation_service.workflow("aircraft_profile")
    assert route["status"] == "stale"
    assert route["stale_reason"] == "terrain_source_changed"


def test_aircraft_profile_change_stales_legacy_route(tmp_path):
    service = WorkflowService(tmp_path / "project.json", DEFAULTS)
    route = {
        "route_id": "R-LEGACY", "status": "passed",
        "path": [[122.0, 30.0], [122.001, 30.0]],
        "provenance": {"source_type": "manual"},
    }
    service.state["operational_routes"] = [route]

    service.invalidation_service.workflow("aircraft_profile")

    assert route["status"] == "stale"
    assert route["stale_reason"] == "aircraft_profile_changed"


def test_aircraft_profile_change_keeps_layered_route_but_stales_downstream(tmp_path):
    service = WorkflowService(tmp_path / "project.json", DEFAULTS)
    route = {
        "route_id": "R0003", "status": "passed",
        "path": [[122.0, 30.0], [122.001, 30.0]],
        "provenance": {
            "source_type": "layered_candidate_operational_adoption_v1",
            "adoption_id": "LRA-TEST",
            "adoption_fingerprint": "layeredadoptionv1-test",
        },
    }
    service.state["operational_routes"] = [route]
    service.state["coverage"] = {"status": "passed"}
    service.state["cns_gap_analysis"] = {"status": "passed"}
    service.state["cns_service_capability"] = {"status": "passed"}
    service.state["service_timeline"] = {"status": "passed"}
    service.state["cns_gap_analysis_v2"] = {"status": "passed"}
    service.state["cns_site_plan"] = {"status": "passed"}
    service.state["cns_corridor_assessment"] = {"status": "passed"}
    service.state["result_statuses"].update({
        "routes": "passed", "coverage": "passed", "cns_gap": "passed",
        "cns_service_capability": "passed", "service_timeline": "passed",
        "cns_gap_v2": "passed", "cns_site_plan": "passed",
        "cns_corridor_assessment": "passed", "technical_risk": "passed",
        "report": "passed",
    })

    service.invalidation_service.workflow("aircraft_profile")

    assert route["status"] == "passed"
    for name in (
        "coverage", "cns_gap", "cns_service_capability", "service_timeline",
        "cns_gap_v2", "cns_site_plan", "cns_corridor_assessment",
        "technical_risk", "report",
    ):
        assert service.state["result_statuses"][name] == "stale"


def test_current_fingerprint_change_supersedes_prior_validation(tmp_path):
    """candidate fingerprint 变化后，旧 validation 必须立即变为 stale，而不是继续 current."""

    service, candidate, profile = prepared(tmp_path)
    service.evaluate_layered_route_validation({}, evidence_adapter=evidence())
    first_id = latest(service)["validation_id"]

    changed = deepcopy(candidate)
    changed["candidate_fingerprint"] = "candidate-fp-2"
    changed["path"] = [[122.0, 30.0], [122.005, 30.0]]
    service.layered_route_planner_service.result_snapshot = lambda: {
        "status": "passed", "count": 1, "active_candidate_id": "LC-1",
        "items": [deepcopy(changed)], "masks": {},
    }
    # 风险剖面仍然绑在旧指纹上：validation 必须 not_ready，且旧记录不得继续 current。
    service.evaluate_layered_route_validation({}, evidence_adapter=evidence())

    items = service.layered_route_validation_service.result_snapshot()["items"]
    by_id = {item["validation_id"]: item for item in items}
    assert by_id[first_id]["status"] == "stale"
    assert by_id[first_id]["current_applicability"] == "stale"
    assert latest(service)["status"] == "not_ready"
    codes = {item["reason_code"] for item in latest(service)["blocking_reasons"]}
    assert "current_route_risk_profile_missing" in codes

    # 风险剖面跟上新指纹后即可重新验证。
    service.route_risk_profile_service.result_snapshot = lambda: {
        "status": "passed", "count": 1, "items": [{
            "profile_id": "RRP-2", "status": "passed", "current_applicability": "current",
            "candidate": {"candidate_id": "LC-1", "candidate_fingerprint": "candidate-fp-2"},
        }],
    }
    service.evaluate_layered_route_validation({}, evidence_adapter=evidence())
    assert latest(service)["status"] == "validated_candidate"
    assert latest(service)["current_applicability"] == "current"


def test_altitude_layer_change_stales_validation_adoption_and_owned_route(tmp_path):
    """AltitudeLayer 变化（走真实 API）→ validation/adoption/owned route/RouteOperatingLayer stale."""

    service, _, _ = prepared(tmp_path)
    service.evaluate_layered_route_validation({}, evidence_adapter=evidence())
    validation_id = latest(service)["validation_id"]
    service.apply_layered_operational_adoption({"validation_id": validation_id, "confirmed": True})

    # 通过真实的高度层写入路径（会触发 route_operating_layer 失效链）。
    service.set_altitude_layer({
        "altitude_layer_id": "L-100", "name": "100m", "nominal_altitude_m": 130.0,
        "lower_altitude_m": 120.0, "upper_altitude_m": 140.0,
        "vertical_reference": "egm2008_orthometric", "source": "engineering",
        "evidence": {"ticket": "T-2"}, "confirmed": True,
    })

    live = service.layered_route_validation_service.result_snapshot()
    stale = next(item for item in live["items"] if item["validation_id"] == validation_id)
    assert stale["status"] == "stale"
    assert stale["current_applicability"] == "stale"

    adoption = service.state["layered_operational_adoptions"]["items"][-1]
    assert adoption["status"] == "stale"
    assert adoption["stale_reason"] == "altitude_layer_changed"

    route = next(item for item in service.state["operational_routes"] if item["route_id"] == "R-1")
    assert route["status"] == "stale"
    assignment = service.state["spatial_3d"]["route_operating_layers"][0]
    assert assignment["status"] == "stale"
    assert assignment["current_applicability"] == "stale_evidence"
    # 语义是 evidence outdated，不是 unsafe。
    assert "unsafe" not in str(route.get("stale_reason"))
    assert effective_route_vertical_context(service.state["spatial_3d"], "R-1")["status"] == (
        "unresolved"
    )


def test_clearance_policy_change_makes_validation_stale(tmp_path):
    """clearance policy 变化属于 validation 依赖，必须让旧 validation 失效（source 不变）。"""

    service, _, _ = prepared(tmp_path)
    service.evaluate_layered_route_validation({}, evidence_adapter=evidence())
    first = latest(service)
    assert first["current_applicability"] == "current"

    service.state["layered_route_feasibility_policy"]["terrain_vertical_clearance_m"] = 40.0
    live = service.layered_route_validation_service.result_snapshot()
    updated = next(item for item in live["items"] if item["validation_id"] == first["validation_id"])
    assert updated["current_applicability"] == "stale_inputs_changed"

    # 重新验证后必须使用新的 clearance（80m 地形 + 40m 净空 > 100m 巡航高 → failed），
    # 且与旧指纹不同；旧记录被 supersede 成 stale。
    service.evaluate_layered_route_validation({}, evidence_adapter=evidence())
    second = latest(service)
    assert second["status"] == "failed"
    assert second["policies"]["terrain_vertical_clearance_m"] == 40.0
    assert second["domains"]["terrain"]["minimum_margin"] == -20.0
    assert (
        second["fingerprints"]["validation_fingerprint"]
        != first["fingerprints"]["validation_fingerprint"]
    )
    assert service.state["layered_route_validations"]["items"][0]["status"] == "stale"


def test_new_validation_fingerprint_carries_building_validator_v3(tmp_path):
    """BUG-VALIDATION-BUILDING-003 审计修复：新执行的 validation fingerprint 必须含 building v2."""

    service, _, _ = prepared(tmp_path)
    service.evaluate_layered_route_validation({}, evidence_adapter=evidence())
    record = latest(service)
    versions = record["fingerprints"]["components"]["validator_versions"]
    assert versions["building"] == "real_footprint_building_validator_v3"
    assert versions == VALIDATOR_VERSIONS
    # 本轮只升级 building 语义：schema 与其余 domain 版本保持不变。
    assert record["schema_version"] == "layered-route-validation-v1"
    assert versions["terrain"] == "source_native_terrain_validator_v1"
    assert versions["native_pixel_intervals"] == "native_pixel_interval_v1"
    assert versions["constant_vertical_context"] == "production_fixed_cruise_egm2008_v1"
    assert ALGORITHM_VERSION == "1.2"
    assert service.layered_route_validation_readiness()["algorithm"]["algorithm_version"] == "1.2"
    # 版本是 fingerprint 的组成部分：building 回退到 v1 必然得到不同的 fingerprint。
    legacy_versions = deepcopy(versions)
    legacy_versions["building"] = "real_footprint_building_validator_v2"
    legacy_components = deepcopy(record["fingerprints"]["components"])
    legacy_components["validator_versions"] = legacy_versions
    assert (
        validation_fingerprint(legacy_components)
        != record["fingerprints"]["validation_fingerprint"]
    )


def test_legacy_building_validator_v2_validation_is_stale_inputs_changed(tmp_path):
    """旧 building-validator-v1 validation 在当前 v2 下必须变为 stale_inputs_changed，不得继续 current."""

    service, _, _ = prepared(tmp_path)
    service.evaluate_layered_route_validation({}, evidence_adapter=evidence())
    stored = service.state["layered_route_validations"]["items"][-1]
    assert stored["status"] == "validated_candidate"
    assert stored["current_applicability"] == "current"

    # 还原一条 v1 时代的记录：components 记录 v1 版本，fingerprint 也按 v1 计算。
    legacy_versions = deepcopy(VALIDATOR_VERSIONS)
    legacy_versions["building"] = "real_footprint_building_validator_v2"
    legacy_components = deepcopy(stored["fingerprints"]["components"])
    legacy_components["validator_versions"] = legacy_versions
    legacy_fingerprint = validation_fingerprint(legacy_components)
    assert legacy_fingerprint != stored["fingerprints"]["validation_fingerprint"]
    stored["fingerprints"]["components"] = legacy_components
    stored["fingerprints"]["validation_fingerprint"] = legacy_fingerprint

    projected = next(
        item for item in service.layered_route_validation_service.result_snapshot()["items"]
        if item["validation_id"] == stored["validation_id"]
    )
    assert projected["current_applicability"] == "stale_inputs_changed"
    assert projected["current_applicability"] != "current"


def test_source_change_stales_evidence_chain_without_touching_v3(tmp_path):
    """terrain/buildings 源变化 → validation/adoption/owned route stale；V3 试验不受影响."""

    service, _, _ = prepared(tmp_path)
    service.state["_v3_probe"] = {"records": [{"experiment_id": "V3-PROBE", "status": "passed"}]}
    service.evaluate_layered_route_validation({}, evidence_adapter=evidence())
    service.apply_layered_operational_adoption(
        {"validation_id": latest(service)["validation_id"], "confirmed": True}
    )

    service.invalidation_service.grid_sources({"terrain_dtm"})

    assert latest(service)["status"] == "stale"
    assert service.state["layered_operational_adoptions"]["items"][-1]["status"] == "stale"
    assert next(
        item for item in service.state["operational_routes"] if item["route_id"] == "R-1"
    )["status"] == "stale"
    assert service.state["_v3_probe"] == {"records": [{"experiment_id": "V3-PROBE", "status": "passed"}]}
    assert service.state["layered_route_validations"]["items"][-1]["status"] == "stale"


def test_publication_stales_only_downstream_and_keeps_published_state(tmp_path):
    """Publish 使用专用传播：不 stale 刚发布的 route / candidate / V3，只 stale 下游 CNS."""

    service, candidate, _ = prepared(tmp_path)
    service.state["_v3_probe"] = {"records": [{"experiment_id": "V3-PROBE", "status": "passed"}]}
    for name in ("coverage_3d", "building_clearance_assessment", "cns_site_plan",
                 "cns_corridor_assessment"):
        service.state[name] = {**(service.state.get(name) or {}), "status": "passed"}
    service.state.setdefault("result_statuses", {}).update({
        "coverage_3d": "passed", "building_clearance": "passed",
        "cns_site_plan": "passed", "cns_corridor_assessment": "passed",
    })

    service.evaluate_layered_route_validation({}, evidence_adapter=evidence())
    validation_id = latest(service)["validation_id"]
    service.apply_layered_operational_adoption({"validation_id": validation_id, "confirmed": True})

    route = next(item for item in service.state["operational_routes"] if item["route_id"] == "R-1")
    assert route["status"] == "passed"
    assert "stale_reason" not in route
    assert latest(service)["status"] == "validated_candidate"
    assert latest(service)["current_applicability"] == "current"
    assert service.state["layered_route_validations"]["items"][-1]["status"] == "validated_candidate"
    assert service.state["_v3_probe"] == {"records": [{"experiment_id": "V3-PROBE", "status": "passed"}]}
    assert service.state["result_statuses"]["coverage_3d"] == "stale"
    assert service.state["result_statuses"]["building_clearance"] == "stale"
    assert service.state["result_statuses"]["cns_site_plan"] == "stale"
    assert service.state["result_statuses"]["cns_corridor_assessment"] == "stale"


# --------------------------------------------------------------------------- resolver


def test_effective_vertical_context_after_publish_and_after_stale(tmp_path):
    """发布后 production 优先；证据过期后必须 unresolved，绝不回落到 legacy profile."""

    service, _, _ = prepared(tmp_path)
    service.state["spatial_3d"]["route_altitude_profiles"] = {"R-1": {
        "route_id": "R-1", "mode": "constant", "constant_altitude_m": 888.0,
        "vertical_reference": "egm2008_orthometric", "confirmed": True, "status": "confirmed",
    }}
    service.evaluate_layered_route_validation({}, evidence_adapter=evidence())
    service.apply_layered_operational_adoption(
        {"validation_id": latest(service)["validation_id"], "confirmed": True}
    )
    spatial = service.state["spatial_3d"]
    context = effective_route_vertical_context(spatial, "R-1")
    assert context["constant_altitude_m"] == 100.0
    assert context["source_kind"] == "production_fixed_cruise_layer"
    assert context["fallback_profile_used"] is False

    service.invalidation_service.layered_route_validation("evidence_outdated")
    stale_context = effective_route_vertical_context(service.state["spatial_3d"], "R-1")
    assert stale_context["status"] == "unresolved"
    assert stale_context["fallback_profile_used"] is False


# --------------------------------------------------------------------------- state / api


def test_snapshot_exposes_validation_and_adoption_and_api_routes_exist(tmp_path):
    from cns_planner.api.router import ApiRouter

    service = WorkflowService(tmp_path / "project.json", DEFAULTS)
    snapshot = service.snapshot()
    assert "layered_route_validations" in snapshot
    assert "layered_route_validation_readiness" in snapshot
    assert "layered_operational_adoptions" in snapshot
    assert "layered_operational_adoption_readiness" in snapshot
    assert set(service.state["result_statuses"]) >= {"layered_route_validation"}

    class _Context:
        workflow = service
        data = type("D", (), {"error": None})()

        class qgis:  # noqa: N801 - mirrors the real attribute name
            @staticmethod
            def call(fn):
                return fn()

    router = ApiRouter(_Context())
    for path in (
        "/api/layered-route-validation/readiness",
        "/api/layered-route-validations",
        "/api/layered-operational-adoption/readiness",
        "/api/layered-operational-adoptions",
    ):
        response = router.get(path, {}, {})
        assert response is not None and response.status == 200
    # 写接口：通过真实调用确认“已注册”且守卫生效（无证据时不得写 operational state）。
    assert router.post("/api/layered-route-validations/evaluate", {}).data is not None
    for path in (
        "/api/layered-operational-adoptions/project",
        "/api/layered-operational-adoptions/preview",
    ):
        assert router.post(path, {}).data["status"] == "not_ready"
    with pytest.raises(ValueError):
        router.post("/api/layered-operational-adoptions/apply", {"confirmed": True})
    with pytest.raises(ValueError):
        router.post("/api/layered-operational-adoptions/revoke", {"confirmed": True})
    assert service.state.get("operational_routes") == []


def test_readiness_reports_missing_prerequisites_without_side_effects(tmp_path):
    service, _, _ = prepared(tmp_path)
    service.state["layered_route_feasibility_policy"] = {"status": "pending_confirmation"}
    service.state["building_clearance_policy"] = {"status": "pending_confirmation"}
    service.state["source_audits"] = {"status": "not_calculated", "items": {}}
    readiness = service.layered_route_validation_readiness()
    assert readiness["status"] == "not_ready"
    codes = {item["reason_code"] for item in readiness["blockers"]}
    assert "terrain_clearance_not_confirmed" in codes
    assert "building_clearance_not_confirmed" in codes
    assert "terrain_dtm_source_not_verified" in codes
    assert "buildings_source_not_verified" in codes
    assert service.state["layered_route_validations"]["items"] == []


def test_apply_requires_current_candidate_and_real_sources(tmp_path):
    """Apply gate：explicit confirmed、real source、current validation 与 current candidate 缺一不可."""

    service, candidate, _ = prepared(tmp_path)

    # 1) 只有 explicit confirmed=true 才能 Apply。
    service.evaluate_layered_route_validation({}, evidence_adapter=evidence())
    validation_id = latest(service)["validation_id"]
    with pytest.raises(ValueError, match="confirmed"):
        service.apply_layered_operational_adoption({"validation_id": validation_id})
    assert service.state["operational_routes"] == []

    # 2) 非 configured_real_sources 的证据在验证阶段就被拒绝，不会产生可 Apply 的 validation。
    synthetic = evidence()
    def synthetic_adapter(**kwargs):
        payload = synthetic(**kwargs)
        payload["source_type"] = "synthetic_test_evidence"
        return payload
    service.evaluate_layered_route_validation({}, evidence_adapter=synthetic_adapter)
    rejected = latest(service)
    assert rejected["status"] == "not_ready"
    codes = {item["reason_code"] for item in rejected["blocking_reasons"]}
    assert "configured_real_sources_required" in codes

    # 3) candidate 不再是 current 时，已存在的 validated_candidate 不可 Apply。
    stale_candidate = deepcopy(candidate)
    stale_candidate["status"] = "stale"
    stale_candidate["current_applicability"] = "stale"
    service.layered_route_planner_service.result_snapshot = lambda: {
        "status": "stale", "count": 1, "active_candidate_id": "LC-1",
        "items": [deepcopy(stale_candidate)], "masks": {},
    }
    with pytest.raises(ValueError):
        service.apply_layered_operational_adoption({"validation_id": validation_id, "confirmed": True})
    assert service.state["operational_routes"] == []
