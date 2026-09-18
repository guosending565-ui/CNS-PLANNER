"""Route Planner V3-D: V3-C validated route -> operational adoption -> CNS bridge.

These tests pin the V3-D contract end to end:

* **publish gate** -- only a *selected and current* V3-C ``validated_route`` whose
  ``route_id`` still matches the scenario route, whose source chain is ready and whose
  CRS transform resolves may be published; production Apply refuses synthetic evidence;
* **transactional / atomic** Apply with a fingerprint TOCTOU guard, and a Revoke that
  removes only what the adoption still owns;
* **representation** -- the published path is two-dimensional ``[lon, lat]``, the
  EGM2008 orthometric altitude lives only in the locked profile, the path and the
  profile share one vertex order and one distance basis, and the V3-C validation history
  stays immutable (``operational_route=false`` / ``cns_assessed=false`` forever);
* **invalidation** -- publishing does not stale the route it just published, and only
  the downstream CNS chain plus operational-route dependents go stale;
* **CNS bridge** -- the existing P7→P8→P9→P10 services are orchestrated (never
  reimplemented), missing prerequisites give ``incomplete`` + a blocking reason, and a
  complete assessment whose requirement is unmet stays ``does_not_meet`` while the route
  remains ``validated_route``.
"""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from cns_planner.algorithms.grid.service import WorkspaceGridService
from cns_planner.application.project_state import blank_project, normalize_project
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.cns_inputs import normalize_device
from cns_planner.domain.operational_timing import normalize_operational_timing
from cns_planner.domain.spatial_3d import normalize_route_altitude_profile
from cns_planner.domain.v3_operational_adoption import (
    ADOPTION_STATUSES, ASSESSMENT_STATUSES, CNS_STAGES, PRODUCTION_EVIDENCE_SOURCE,
    REQUIREMENT_VERDICTS, ROUTE_SOURCE_TYPE, V3D_EVIDENCE_SOURCES,
    empty_v3_operational_adoptions, normalize_v3_cns_assessment_bundle,
    normalize_v3_operational_adoption, normalize_v3_operational_adoptions,
)
from cns_planner.route_planner_v3.continuous_contracts import V3C_RESULT_STATUSES
from cns_planner.route_planner_v3.continuous_geometry import realize_continuous_route

DEFAULTS = Path("cns_planner/config/defaults.json")
WORKSPACE = [122.0, 29.9, 122.02, 29.92]
SYNTHETIC = "canonical_synthetic"
REAL = PRODUCTION_EVIDENCE_SOURCE


# --------------------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------------------


class AffineMetricTransform:
    """A deterministic pure scale+translation metric -> CRS84 stand-in.

    Real runs get a ``pyproj``/QGIS transform for the recorded projected CRS; this
    harness supplies the same interface so the projection logic can be exercised without
    a GIS runtime.  It is never a silent default: the service only uses an explicitly
    injected or explicitly resolved transform.
    """

    def __init__(self, origin, scale, *, authority="TEST:32651"):
        self.origin, self.scale, self.authority = origin, scale, authority

    def to_geographic(self, point):
        return [
            self.origin[0] + float(point[0]) * self.scale[0],
            self.origin[1] + float(point[1]) * self.scale[1],
        ]

    def describe(self):
        return {
            "method": "test_affine_metric_to_crs84", "authority": self.authority,
            "geodetic": True, "target_crs": "OGC:CRS84", "transform_id": "test_affine",
        }


def v3_policy(**overrides):
    payload = {
        "min_altitude_egm2008_m": 100.0, "max_altitude_egm2008_m": 400.0,
        "vertical_step_m": 50.0, "terrain_clearance_m": 50.0,
        "building_horizontal_clearance_m": 15.0, "building_vertical_clearance_m": 30.0,
        "aircraft_min_turn_radius_m": 20.0, "max_climb_gradient": 0.5,
        "max_descent_gradient": 0.5, "planning_speed_mps": 25.0,
        "source": "integration_test", "confirmed": True,
    }
    payload.update(overrides)
    return payload


def workflow(tmp_path, name="project.json"):
    service = WorkflowService(tmp_path / name, DEFAULTS)
    service.state["workspace"] = {"status": "passed", "bbox": list(WORKSPACE)}
    service.state["nodes"] = [
        {"node_id": "N001", "name": "A", "coordinate": [122.0005, 29.9005]},
        {"node_id": "N002", "name": "B", "coordinate": [122.0195, 29.9195]},
    ]
    service.state["node_seq"] = 2
    return service


def build_validated(service, *, evidence_source=SYNTHETIC):
    """Run V3-A -> V3-B -> V3-C so a current ``validated_route`` exists."""

    policy = v3_policy()
    service.set_route_planner_v3_policy(policy)
    service.set_route_planner_v3_fine_policy({
        "horizontal_crs": "EPSG:32651", "resolution_source": "explicit_configuration",
        "resolution_m": 30.0, "max_stride_cells": 2, "source": "integration_test",
        "confirmed": True,
    })
    service.set_route_planner_v3_validation_policy({
        "curve_chord_error_m": 0.5, "max_validation_samples": 200000,
        "source": "integration_test", "confirmed": True,
    })
    service.generate_scenario_od("N001", "N002", "ab")
    route_id = service.state["scenario_routes"][0]["route_id"]
    service.evaluate_route_planner_v3({
        "environment_source": SYNTHETIC, "synthetic_spec": {"profile_id": "open_flat"},
        "policy": policy, "route_id": route_id,
    })
    service.evaluate_route_planner_v3_refinement({
        "environment_source": SYNTHETIC,
        "synthetic_fine_spec": {"profile_id": "synthetic_fine_open"},
        "refinement_cell_size_m": 30.0, "max_stride_cells": 2,
    })
    service.evaluate_route_planner_v3_continuous_validation({"evidence_source": evidence_source})
    return route_id


def validation_entry(service):
    records = service.route_planner_v3_snapshot()["records"]
    return records[0]["refinements"][0]["validations"][0]


def fit_transform(service):
    """Fit the harness transform so the metric endpoints land on the scenario route."""

    route = validation_entry(service)["result"]["continuous_route"]
    metric_line = route["horizontal_geometry"]["linearized"]["linestring_metric"]
    scenario = service.state["scenario_routes"][0]
    start, end = scenario["start"], scenario["end"]
    scale_x = (end[0] - start[0]) / (metric_line[-1][0] - metric_line[0][0])
    scale_y = (end[1] - start[1]) / (metric_line[-1][1] - metric_line[0][1])
    return AffineMetricTransform(
        [start[0] - metric_line[0][0] * scale_x, start[1] - metric_line[0][1] * scale_y],
        [scale_x, scale_y],
    )


def adoption_service(service, *, relax_production=False, ready=True):
    """Wire the harness transform and (optionally) a ready source chain."""

    adoption = service.v3_operational_adoption_service
    adoption.metric_transform = fit_transform(service)
    if relax_production:
        adoption.require_production_sources = False
    if ready:
        adoption.v3_service.real_data_readiness = lambda: {
            "status": "ready",
            "v3c": {"status": "ready", "blocking_reasons": []},
        }
    return adoption


def cns_device(device_id="C-CONFIRMED", *, model=None):
    return normalize_device({
        "device_id": device_id, "name": device_id, "subsystem": "C", "role": "existing",
        "radius_m": 5000, "mtbf_h": 1000,
        "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
        "performance": {"max_latency_s": 0.2, "min_redundancy": 1},
        "coverage_geometry": {
            "model": "sphere", "slant_range_m": 5000.0,
            "source": "test", "confirmed": True,
        },
        "service_model": model if model is not None else {
            "model_family": "declared_performance", "version": "1",
            "technology": "dedicated_radio",
            "parameters": {
                "performance": {"max_latency_s": 0.2, "min_redundancy": 1},
                "independence_confirmed": True, "independence_group": "GROUND-C",
            },
            "source": "test", "confirmed": True,
        },
    })


def cns_facility(coordinate, *, device_id="C-CONFIRMED", origin_m=100.0, radius_m=20000.0):
    return {
        "facility_id": "FAC-C", "site_id": "SITE-C", "name": "C station",
        "coordinate": list(coordinate), "status": "active", "source": "test",
        "vertical_profile": {
            "service_origin_egm2008_m": float(origin_m),
            "vertical_reference": "egm2008_orthometric",
            "source": "test", "confirmed": True, "status": "confirmed",
        },
        "devices": [{
            "device_id": device_id, "subsystem": "C",
            "coverage_geometry": {
                "model": "sphere", "slant_range_m": float(radius_m),
                "source": "test", "confirmed": True,
            },
            "status": "active",
        }],
    }


def c_requirement(*, latency_s=1.0):
    return {
        "required": True, "confirmed": True, "source": "test",
        "coverage_requirement": 5000.0, "max_gap_m": 0.0,
        "latency_ms": latency_s * 1000.0, "redundancy": 1,
        "max_latency_s": latency_s, "min_redundancy": 1,
        "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
        "performance": {"max_latency_s": latency_s, "min_redundancy": 1},
    }


def prepare_cns(service, route, *, latency_s=1.0, timing=True):
    """Every real prerequisite the P7-P10 chain needs (no invented defaults)."""

    state = service.state
    for item in (state.setdefault("aircraft_profiles", {}).get("items") or []):
        if item.get("aircraft_id") != "AIRCRAFT-CNS-DEMO-01":
            continue
        item["communication"] = {
            "confirmed": True, "status": "confirmed", "capabilities": ["radio"],
            "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
            "performance": {"max_latency_s": 0.5, "min_redundancy": 1}, "fallbacks": [],
        }
    state["selected_aircraft_profile_id"] = "AIRCRAFT-CNS-DEMO-01"
    state["required_cns"] = {
        "status": "passed", "source": "test",
        "project_default": {
            "communication": c_requirement(latency_s=latency_s),
            "navigation": {"required": False, "status": "passed", "type": {}, "performance": {}},
            "surveillance": {"required": False, "status": "passed", "type": {}, "performance": {}},
        },
        "route_overrides": {}, "metadata": {},
    }
    state["device_catalog"] = {
        "status": "passed", "catalog_id": "cns-device-catalog", "source": "test",
        "count": 1, "items": [cns_device()],
    }
    state["existing_cns_facilities"] = {
        "status": "passed", "collection_id": "existing-cns-facilities", "source": "test",
        "count": 1, "items": [cns_facility(route["start"])], "metadata": {},
    }
    state["spatial_3d"]["altitude_layers"] = [{
        "altitude_layer_id": "L100", "name": "100 m", "lower_altitude_m": 0.0,
        "upper_altitude_m": 500.0, "vertical_reference": "egm2008_orthometric",
        "source": "test", "confirmed": True, "status": "confirmed",
    }]
    timing_payload = normalize_operational_timing({})
    if timing:
        timing_payload["route_motion_profiles"][route["route_id"]] = {
            "route_id": route["route_id"], "mode": "constant_ground_speed_mps",
            "constant_ground_speed_mps": 25.0, "source": "test",
            "confirmed": True, "status": "confirmed",
        }
        timing_payload["service_scenarios"][route["route_id"]] = {
            "route_id": route["route_id"], "scenario_id": "SC-AVAILABLE", "source": "test",
            "confirmed": True,
            "events": [{
                "event_id": "E1", "subsystem": "C", "start_s": 0.0, "end_s": 100000.0,
                "external_state": "available",
                "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
                "performance": {"latency_s": 0.2}, "source": "test", "confirmed": True,
            }],
        }
    state["operational_timing"] = timing_payload
    service.save()


def preview_then_apply(service, adoption, *, relax=False, confirmed=True):
    payload = {"evidence_source": REAL}
    preview = service.preview_v3_operational_adoption(payload)
    projection = preview["projections"][0]
    return preview, service.apply_v3_operational_adoption({
        "confirmed": confirmed, "evidence_source": REAL,
        "validation_ids": [projection["validation_id"]],
    })


def publish(service, **kwargs):
    """Preview + Apply a synthetic validation (harness-only production relaxation)."""

    adoption = adoption_service(service, relax_production=True)
    preview = service.preview_v3_operational_adoption({"evidence_source": REAL})
    assert preview["projections"], preview["blocked"]
    service.apply_v3_operational_adoption({
        "confirmed": True, "evidence_source": REAL,
        "validation_ids": [preview["projections"][0]["validation_id"]],
    })
    return adoption, preview["projections"][0]


# --------------------------------------------------------------------------------------
# 1. state container
# --------------------------------------------------------------------------------------


def test_blank_project_carries_the_additive_v3d_containers():
    project = blank_project({})
    assert project["v3_operational_adoptions"] == empty_v3_operational_adoptions()
    assert project["v3_operational_adoptions"]["items"] == []
    assert project["v3_cns_assessment_bundle"]["items"] == []
    assert project["v3_cns_assessment_bundle"]["status"] == "not_calculated"


def test_legacy_project_backfills_the_v3d_containers_without_touching_other_fields():
    legacy = blank_project({})
    before_routes = deepcopy(legacy["operational_routes"])
    before_selection = deepcopy(legacy["algorithm_selection"])
    before_spatial = deepcopy(legacy["spatial_3d"])
    legacy.pop("v3_operational_adoptions")
    legacy.pop("v3_cns_assessment_bundle")
    normalized = normalize_project(legacy, WorkspaceGridService())
    assert normalized["v3_operational_adoptions"]["count"] == 0
    assert normalized["v3_cns_assessment_bundle"]["items"] == []
    assert normalized["operational_routes"] == before_routes
    assert normalized["algorithm_selection"] == before_selection
    assert normalized["spatial_3d"] == before_spatial


def test_v3d_containers_survive_save_and_reload(tmp_path):
    service = workflow(tmp_path)
    build_validated(service)
    publish(service)
    reloaded = WorkflowService(tmp_path / "project.json", DEFAULTS)
    adoptions = reloaded.v3_operational_adoptions_snapshot()
    assert adoptions["count"] == 1
    assert adoptions["items"][0]["route_id"] == reloaded.state["operational_routes"][0]["route_id"]
    assert adoptions["items"][0]["status"] == "published"


# --------------------------------------------------------------------------------------
# 2. publish gate
# --------------------------------------------------------------------------------------


def test_gate_accepts_a_current_validated_route_with_a_ready_source_chain(tmp_path):
    service = workflow(tmp_path)
    build_validated(service)
    adoption = adoption_service(service)
    options = adoption.eligible_validation_options()
    assert len(options) == 1
    assert options[0]["status"] == "validated_route"
    assert options[0]["eligible"] is True
    assert options[0]["production_eligible"] is False
    assert "production_apply_requires_configured_real_sources" in options[0]["production_reasons"]


def test_gate_rejects_a_validation_that_is_not_validated_route(tmp_path):
    service = workflow(tmp_path)
    build_validated(service)
    adoption = adoption_service(service)
    entry = adoption.validation_index()[list(adoption.validation_index())[0]]
    entry["result"]["status"] = "failed"
    gate = adoption.publish_gate(entry, require_production=False)
    assert gate["eligible"] is False
    assert any(reason.startswith("validation_status_not_validated_route") for reason in gate["reasons"])


def test_gate_rejects_unresolved_and_not_ready_validations(tmp_path):
    for status in ("unresolved", "not_ready", "validation_incomplete"):
        service = workflow(tmp_path, f"gate-{status}.json")
        build_validated(service)
        adoption = adoption_service(service)
        entry = adoption.validation_index()[list(adoption.validation_index())[0]]
        entry["result"]["status"] = status
        gate = adoption.publish_gate(entry, require_production=False)
        assert gate["eligible"] is False
        assert any(
            reason.startswith("validation_status_not_validated_route") for reason in gate["reasons"]
        )


def test_gate_rejects_a_stale_validation(tmp_path):
    service = workflow(tmp_path)
    build_validated(service)
    adoption = adoption_service(service)
    entry = adoption.validation_index()[list(adoption.validation_index())[0]]
    # A changed curve tolerance makes the stored validation stale.
    service.set_route_planner_v3_validation_policy({
        "curve_chord_error_m": 0.01, "max_validation_samples": 200000,
        "source": "integration_test", "confirmed": True,
    })
    gate = adoption.publish_gate(entry, require_production=False)
    assert gate["eligible"] is False
    assert "validation_is_stale" in gate["reasons"]
    assert "current_validation_status:validated_route" not in gate["reasons"]


def test_gate_rejects_a_route_id_that_is_not_a_current_scenario_route(tmp_path):
    service = workflow(tmp_path)
    build_validated(service)
    adoption = adoption_service(service)
    entry = adoption.validation_index()[list(adoption.validation_index())[0]]
    service.state["scenario_routes"] = []
    gate = adoption.publish_gate(entry, require_production=False)
    assert gate["eligible"] is False
    assert "route_id_is_not_a_current_scenario_route" in gate["reasons"]


def test_gate_rejects_a_missing_source_chain_and_unresolvable_crs(tmp_path):
    service = workflow(tmp_path)
    build_validated(service)
    adoption = adoption_service(service, ready=False)
    service.v3_operational_adoption_service.v3_service.real_data_readiness = lambda: {
        "status": "blocked", "v3c": {"status": "blocked", "blocking_reasons": ["terrain_dtm_missing"]},
    }
    entry = adoption.validation_index()[list(adoption.validation_index())[0]]
    gate = adoption.publish_gate(entry, require_production=True)
    assert "configured_real_source_chain_not_ready" in gate["reasons"]
    # A transform that cannot be resolved blocks the same gate.
    adoption.metric_transform = None
    adoption.transform_resolver = None
    blocked = adoption.publish_gate(
        adoption.validation_index()[list(adoption.validation_index())[0]],
        require_production=False,
    )
    assert "crs_transform_unavailable" in blocked["reasons"]


def test_production_apply_refuses_canonical_synthetic_evidence(tmp_path):
    service = workflow(tmp_path)
    build_validated(service)
    adoption = adoption_service(service)  # production enforcement stays ON
    entry = adoption.validation_index()[list(adoption.validation_index())[0]]
    assert entry["validation"]["evidence_source"] == SYNTHETIC
    with pytest.raises(ValueError, match="configured_real_sources"):
        service.apply_v3_operational_adoption({
            "confirmed": True, "evidence_source": REAL,
            "validation_ids": [entry["validation_id"]],
        })
    assert service.state["operational_routes"] == []
    assert service.state["v3_operational_adoptions"]["items"] == []


def test_synthetic_preview_is_allowed_and_writes_nothing(tmp_path):
    service = workflow(tmp_path)
    build_validated(service)
    adoption_service(service, relax_production=True)
    before_routes = deepcopy(service.state["operational_routes"])
    before_spatial = deepcopy(service.state["spatial_3d"])
    preview = service.preview_v3_operational_adoption({"evidence_source": SYNTHETIC})
    assert preview["status"] == "ready"
    assert preview["synthetic_test_only"] is True
    assert preview["publication_allowed"] is False
    assert "不可正式发布" in preview["synthetic_notice"]
    assert preview["operational_routes_untouched"] is True
    assert preview["spatial_3d_untouched"] is True
    assert preview["cns_not_run"] is True
    assert service.state["operational_routes"] == before_routes
    assert service.state["spatial_3d"] == before_spatial
    assert service.state["v3_operational_adoptions"]["items"] == []


def test_apply_requires_explicit_confirmation(tmp_path):
    service = workflow(tmp_path)
    build_validated(service)
    adoption, _ = publish(service)
    # A second Apply without the explicit flag is refused.
    service.state["operational_routes"] = []
    entry = adoption.validation_index()[list(adoption.validation_index())[0]]
    with pytest.raises(ValueError, match="confirmed=true"):
        service.apply_v3_operational_adoption({
            "evidence_source": REAL, "validation_ids": [entry["validation_id"]],
        })


def test_apply_rejects_a_changed_fingerprint_to_avoid_toctou(tmp_path):
    service = workflow(tmp_path)
    build_validated(service)
    adoption_service(service, relax_production=True)
    entry = validation_entry(service)
    with pytest.raises(ValueError, match="TOCTOU"):
        service.apply_v3_operational_adoption({
            "confirmed": True, "evidence_source": REAL,
            "validation_ids": [entry["validation_id"]],
            "expected_validation_fingerprint": "V3CVALID-STALE-FINGERPRINT",
        })
    assert service.state["operational_routes"] == []
    assert service.state["v3_operational_adoptions"]["items"] == []


def test_apply_accepts_the_matching_fingerprint(tmp_path):
    service = workflow(tmp_path)
    build_validated(service)
    adoption_service(service, relax_production=True)
    entry = validation_entry(service)
    fingerprint = entry["result"]["validation_fingerprint"]
    assert fingerprint
    outcome = service.apply_v3_operational_adoption({
        "confirmed": True, "evidence_source": REAL,
        "validation_ids": [entry["validation_id"]],
        "expected_validation_fingerprint": fingerprint,
    })
    assert outcome["status"] == "passed"
    assert outcome["count"] == 1
    assert outcome["v3c_validation_history_unchanged"] is True


def test_batch_apply_is_atomic(tmp_path):
    service = workflow(tmp_path)
    build_validated(service)
    adoption, _ = publish(service)
    # A second, ineligible validation id forces the whole batch to be rejected.
    entry = adoption.validation_index()[list(adoption.validation_index())[0]]
    records = service.state["route_planner_v3_experiments"]["records"]
    refinement = records[0]["refinements"][0]
    broken = deepcopy(refinement["validations"][0])
    broken["validation_id"] = "V3C-BROKEN"
    broken["result"]["status"] = "failed"
    refinement["validations"] = [*refinement["validations"], broken]
    service.save()
    before_routes = deepcopy(service.state["operational_routes"])
    with pytest.raises(ValueError, match="batch atomic"):
        service.apply_v3_operational_adoption({
            "confirmed": True, "evidence_source": REAL,
            "validation_ids": [entry["validation_id"], "V3C-BROKEN"],
        })
    assert service.state["operational_routes"] == before_routes


def test_apply_rejects_an_unknown_evidence_source(tmp_path):
    service = workflow(tmp_path)
    build_validated(service)
    adoption_service(service, relax_production=True)
    with pytest.raises(ValueError, match="未知 evidence_source"):
        service.apply_v3_operational_adoption({
            "confirmed": True, "evidence_source": "guessed_real_data",
        })


def test_apply_rejects_an_unknown_validation_id(tmp_path):
    service = workflow(tmp_path)
    build_validated(service)
    adoption_service(service, relax_production=True)
    preview = service.preview_v3_operational_adoption({
        "evidence_source": REAL, "validation_ids": ["V3C-NOT-A-REAL-ID"],
    })
    assert preview["status"] == "blocked"
    assert preview["reason"] == "validation_id 不存在：V3C-NOT-A-REAL-ID"
    assert preview["projections"] == []
    assert preview["operational_routes_untouched"] is True


# --------------------------------------------------------------------------------------
# 3. projection representation
# --------------------------------------------------------------------------------------


def test_projection_converts_the_metric_geometry_to_crs84_endpoints(tmp_path):
    service = workflow(tmp_path)
    build_validated(service)
    adoption_service(service, relax_production=True)
    preview = service.preview_v3_operational_adoption({"evidence_source": REAL})
    projection = preview["projections"][0]
    scenario = service.state["scenario_routes"][0]
    path = projection["route"]["path"]
    assert path[0] == pytest.approx(scenario["start"], abs=1e-9)
    assert path[-1] == pytest.approx(scenario["end"], abs=1e-9)
    assert projection["path_crs"] == "OGC:CRS84"
    assert projection["transform"]["target_crs"] == "OGC:CRS84"
    assert projection["transform"]["method"] == "test_affine_metric_to_crs84"
    assert projection["transform"]["geodetic"] is True
    # The horizontal CRS travels with the data (here the V3-B synthetic local frame).
    assert projection["horizontal_crs"] == "synthetic:local_equirectangular_m"
    assert projection["route"]["provenance"]["crs_transform"]["method"] == (
        "test_affine_metric_to_crs84"
    )
    assert projection["compatibility"]["crs_mixing"] is False


def test_published_path_is_two_dimensional_and_never_carries_egm2008(tmp_path):
    service = workflow(tmp_path)
    build_validated(service)
    route = publish(service)[1]["route"]
    assert route["path"]
    assert all(len(point) == 2 for point in route["path"])
    assert route["kind"] == "operational"
    assert route["status"] == "passed"
    assert route["provenance"]["source_type"] == ROUTE_SOURCE_TYPE
    assert route["provenance"]["vertical_reference"] == "egm2008_orthometric"
    assert route["provenance"]["horizontal_representation"] == "two_dimensional_lon_lat_only"
    assert route["provenance"]["vertical_representation"] == "locked_route_altitude_profile_waypoints"
    assert route["provenance"]["cns_excluded_from_search_cost"] is True
    # The GeoJSON export must stay twice as long as it has vertices (no z anywhere).
    features = json.loads(service.export_routes().decode("utf-8"))
    coordinates = features["features"][0]["geometry"]["coordinates"]
    assert all(len(point) == 2 for point in coordinates)
    assert len(coordinates) == len(route["path"])
    # The full analytic geometry is never copied into operational_routes.
    serialized = json.dumps(route)
    assert "center_metric" not in serialized
    assert "circular_arc" not in serialized
    assert "linearization" not in serialized


def test_route_identity_is_preserved_from_the_scenario_route(tmp_path):
    service = workflow(tmp_path)
    build_validated(service)
    scenario = service.state["scenario_routes"][0]
    route = publish(service)[1]["route"]
    assert route["route_id"] == scenario["route_id"]
    assert route["start"] == pytest.approx(scenario["start"])
    assert route["end"] == pytest.approx(scenario["end"])
    assert route["start_node_id"] == scenario["start_node_id"]
    assert route["end_node_id"] == scenario["end_node_id"]
    assert route["direction"] == scenario["direction"]


def test_apply_upserts_only_the_adopted_route_id(tmp_path):
    service = workflow(tmp_path)
    build_validated(service)
    foreign = {"route_id": "R-LEGACY", "status": "passed", "kind": "operational",
               "path": [[122.0, 29.9], [122.001, 29.901]]}
    service.state["operational_routes"] = [deepcopy(foreign)]
    publish(service)
    ids = [route["route_id"] for route in service.state["operational_routes"]]
    assert "R-LEGACY" in ids
    assert service.state["scenario_routes"][0]["route_id"] in ids
    legacy = next(route for route in service.state["operational_routes"]
                  if route["route_id"] == "R-LEGACY")
    assert legacy == foreign


# --------------------------------------------------------------------------------------
# 4. locked EGM2008 derived profile
# --------------------------------------------------------------------------------------


def test_derived_profile_is_locked_confirmed_and_egm2008(tmp_path):
    service = workflow(tmp_path)
    build_validated(service)
    projection = publish(service)[1]
    profile = projection["profile"]
    assert profile["mode"] == "waypoint_linear"
    assert profile["vertical_reference"] == "egm2008_orthometric"
    assert profile["confirmed"] is True
    assert profile["source"] == ROUTE_SOURCE_TYPE
    assert profile["derived"] is True
    assert profile["locked"] is True
    assert profile["locked_by_adoption"] is True
    assert profile["requested_by_user"] is False
    assert profile["status"] == "confirmed"
    stored = service.state["spatial_3d"]["route_altitude_profiles"][projection["route_id"]]
    assert stored["locked_by_adoption"] is True
    assert stored["v3_metric_length_m"] == pytest.approx(projection["path_metrics"]["v3_metric_length_m"])
    assert stored["legacy_geodesic_length_m"] == pytest.approx(
        projection["path_metrics"]["legacy_geodesic_length_m"]
    )
    assert stored["length_delta_m"] == pytest.approx(projection["path_metrics"]["length_delta_m"])
    assert stored["distance_basis"] == projection["path_metrics"]["distance_basis"]


def test_path_and_profile_share_vertex_order_and_distance_basis(tmp_path):
    service = workflow(tmp_path)
    build_validated(service)
    projection = publish(service)[1]
    path = projection["route"]["path"]
    waypoints = projection["profile"]["waypoints"]
    assert len(path) == len(waypoints)
    distances = [point["distance_along_route_m"] for point in waypoints]
    assert distances == sorted(distances)
    assert distances[0] == pytest.approx(0.0)
    # The profile's own distance basis is the path's cumulative distance, vertex by vertex.
    cumulative = 0.0
    for index in range(1, len(path)):
        cumulative += (
            (path[index][0] - path[index - 1][0]) ** 2
            + (path[index][1] - path[index - 1][1]) ** 2
        ) ** 0.5
        assert distances[index] == pytest.approx(cumulative, abs=1e-9)
    assert projection["compatibility"]["path_and_profile_share_vertex_order"] is True
    assert projection["compatibility"]["path_and_profile_share_distance_basis"] is True


def test_profile_altitudes_come_from_the_v3c_realized_vertical(tmp_path):
    service = workflow(tmp_path)
    build_validated(service)
    projection = publish(service)[1]
    entry = validation_entry(service)
    route = entry["result"]["continuous_route"]
    metric_line = route["horizontal_geometry"]["linearized"]["linestring_metric"]
    values = [point["altitude_m"] for point in projection["profile"]["waypoints"]]
    assert len(values) == len(metric_line)
    # The realized V3-C vertical is flat in the synthetic open scenario.
    realized = {primitive["z_start_egm2008_m"] for primitive in route["primitives"]}
    assert len(realized) == 1
    assert set(values) == realized
    assert projection["compatibility"]["min_altitude_egm2008_m"] == pytest.approx(min(values))
    assert projection["compatibility"]["max_altitude_egm2008_m"] == pytest.approx(max(values))


def test_metric_and_geodesic_lengths_are_both_recorded_and_consistent(tmp_path):
    service = workflow(tmp_path)
    build_validated(service)
    metrics = publish(service)[1]["path_metrics"]
    assert metrics["v3_metric_length_m"] > 0
    assert metrics["legacy_geodesic_length_m"] > 0
    assert metrics["length_delta_m"] == pytest.approx(
        metrics["legacy_geodesic_length_m"] - metrics["v3_metric_length_m"]
    )
    assert metrics["simplification_applied"] is False
    assert metrics["metric_vertex_count"] == metrics["published_vertex_count"]
    assert "v3_metric_length_m" in metrics["length_semantics"]


def test_locked_v3_profile_rejects_a_manual_altitude_edit(tmp_path):
    service = workflow(tmp_path)
    build_validated(service)
    projection = publish(service)[1]
    route_id = projection["route_id"]
    with pytest.raises(ValueError, match="锁定"):
        service.spatial_3d_service.set_route_profile({
            "route_id": route_id, "mode": "constant", "constant_altitude_m": 500.0,
            "vertical_reference": "egm2008_orthometric", "confirmed": True, "source": "user",
        })
    stored = service.state["spatial_3d"]["route_altitude_profiles"][route_id]
    assert stored["mode"] == "waypoint_linear"
    # After revoking the adoption the manual profile is allowed again.
    adoption = service.v3_operational_adoptions_snapshot()["items"][0]
    service.revoke_v3_operational_adoption({
        "confirmed": True, "adoption_id": adoption["adoption_id"],
    })
    assert route_id not in (service.state["spatial_3d"]["route_altitude_profiles"] or {})
    legacy_route = {"route_id": route_id, "status": "passed", "path": [[122.0, 29.9], [122.001, 29.901]]}
    service.state["operational_routes"] = [legacy_route]
    service.spatial_3d_service.set_route_profile({
        "route_id": route_id, "mode": "constant", "constant_altitude_m": 500.0,
        "vertical_reference": "egm2008_orthometric", "confirmed": True, "source": "user",
    })
    assert service.state["spatial_3d"]["route_altitude_profiles"][route_id]["mode"] == "constant"


def test_profile_normalization_keeps_v3d_provenance_additively():
    profile = normalize_route_altitude_profile({
        "route_id": "R1", "mode": "waypoint_linear", "vertical_reference": "egm2008_orthometric",
        "waypoints": [
            {"distance_along_route_m": 0.0, "altitude_m": 100.0},
            {"distance_along_route_m": 10.0, "altitude_m": 110.0},
        ],
        "source": ROUTE_SOURCE_TYPE, "confirmed": True, "derived": True, "locked": True,
        "locked_by_adoption": True, "adoption_owned": True,
        "distance_basis": "cumulative_2d_baseline_path_distance",
        "v3_metric_length_m": 1000.0, "legacy_geodesic_length_m": 1001.0, "length_delta_m": 1.0,
        "curve_chord_error_m": 0.5,
    })
    assert profile["derived"] is True
    assert profile["locked"] is True
    assert profile["locked_by_adoption"] is True
    assert profile["adoption_owned"] is True
    assert profile["v3_metric_length_m"] == pytest.approx(1000.0)
    assert profile["legacy_geodesic_length_m"] == pytest.approx(1001.0)
    assert profile["length_delta_m"] == pytest.approx(1.0)
    assert profile["distance_basis"] == "cumulative_2d_baseline_path_distance"
    # A plain user profile gains none of the V3 provenance.
    plain = normalize_route_altitude_profile({
        "route_id": "R2", "mode": "constant", "constant_altitude_m": 100.0,
        "vertical_reference": "egm2008_orthometric", "confirmed": True, "source": "user",
    })
    assert plain["derived"] is False
    assert plain["locked"] is False
    assert plain["locked_by_adoption"] is False
    assert plain["v3_metric_length_m"] is None


def test_v1_route_profiles_are_unaffected_by_the_lock_guard(tmp_path):
    service = workflow(tmp_path)
    service.state["operational_routes"] = [{
        "route_id": "R0001", "status": "passed", "path": [[122.001, 29.901], [122.002, 29.902]],
    }]
    service.save()
    service.spatial_3d_service.set_route_profile({
        "route_id": "R0001", "mode": "constant", "constant_altitude_m": 120.0,
        "vertical_reference": "egm2008_orthometric", "confirmed": True, "source": "user",
    })
    assert service.state["spatial_3d"]["route_altitude_profiles"]["R0001"]["mode"] == "constant"


# --------------------------------------------------------------------------------------
# 5. revocation ownership
# --------------------------------------------------------------------------------------


def test_revoke_removes_only_the_adoption_owned_route_and_profile(tmp_path):
    service = workflow(tmp_path)
    build_validated(service)
    projection = publish(service)[1]
    adoption = service.v3_operational_adoptions_snapshot()["items"][0]
    outcome = service.revoke_v3_operational_adoption({
        "confirmed": True, "adoption_id": adoption["adoption_id"],
    })
    assert outcome["removed_route"] is True
    assert outcome["removed_profile"] is True
    assert outcome["preserved_foreign_routes"] is True
    assert service.state["operational_routes"] == []
    assert projection["route_id"] not in service.state["spatial_3d"]["route_altitude_profiles"]
    statuses = {item["status"] for item in service.v3_operational_adoptions_snapshot()["items"]}
    assert statuses == {"revoked"}


def test_revoke_never_deletes_a_route_another_planner_regenerated(tmp_path):
    service = workflow(tmp_path)
    build_validated(service)
    projection = publish(service)[1]
    route_id = projection["route_id"]
    adoption = service.v3_operational_adoptions_snapshot()["items"][0]
    # Another planner regenerates the same route_id: provenance is no longer V3's.
    foreign = {
        "route_id": route_id, "status": "passed", "kind": "operational",
        "path": [[122.0, 29.9], [122.001, 29.901]], "algorithm_id": "route_planner_v1",
        "algorithm_version": "1.0",
    }
    service.state["operational_routes"] = [deepcopy(foreign)]
    service.save()
    outcome = service.revoke_v3_operational_adoption({
        "confirmed": True, "adoption_id": adoption["adoption_id"],
    })
    assert outcome["removed_route"] is False
    remaining = service.state["operational_routes"]
    assert len(remaining) == 1
    assert remaining[0]["algorithm_id"] == "route_planner_v1"


def test_revoke_requires_confirmation_and_a_known_adoption(tmp_path):
    service = workflow(tmp_path)
    build_validated(service)
    publish(service)
    adoption = service.v3_operational_adoptions_snapshot()["items"][0]
    with pytest.raises(ValueError, match="confirmed=true"):
        service.revoke_v3_operational_adoption({"adoption_id": adoption["adoption_id"]})
    with pytest.raises(ValueError, match="不存在或已撤销"):
        service.revoke_v3_operational_adoption({"confirmed": True, "adoption_id": "V3D-NOPE"})


def test_applying_twice_is_idempotent_for_the_adoption_identity(tmp_path):
    service = workflow(tmp_path)
    build_validated(service)
    adoption, _ = publish(service)
    first = service.v3_operational_adoptions_snapshot()["items"][0]["adoption_id"]
    entry = adoption.validation_index()[list(adoption.validation_index())[0]]
    adoption.metric_transform = fit_transform(service)
    service.apply_v3_operational_adoption({
        "confirmed": True, "evidence_source": REAL,
        "validation_ids": [entry["validation_id"]],
    })
    items = service.v3_operational_adoptions_snapshot()["items"]
    assert len(items) == 1
    assert items[0]["adoption_id"] == first


# --------------------------------------------------------------------------------------
# 6. invalidation
# --------------------------------------------------------------------------------------


def test_publish_does_not_stale_the_route_it_just_published(tmp_path):
    service = workflow(tmp_path)
    build_validated(service)
    projection = publish(service)[1]
    routes = service.state["operational_routes"]
    assert len(routes) == 1
    assert routes[0]["status"] == "passed"
    assert "stale_reason" not in routes[0]
    assert service.state["result_statuses"]["routes"] == "passed"


def test_publish_stales_only_the_downstream_cns_chain(tmp_path):
    service = workflow(tmp_path)
    build_validated(service)
    # Pre-existing upstream results that must survive the publish.
    service.state["grid_risk"] = {"status": "passed", "algorithm_id": "risk-model-v1-relative-index"}
    service.state["result_statuses"]["environment_risk"] = "passed"
    service.state["v3_planning_policy"] = {**service.state["v3_planning_policy"], "status": "confirmed"}
    service.state["route_planner_v3_experiments"]["status"] = "passed"
    service.save()
    publish(service)
    statuses = service.state["result_statuses"]
    assert statuses.get("environment_risk") == "passed"
    assert statuses.get("routes") == "passed"
    # Nothing V3 was staled.
    assert service.state["route_planner_v3_experiments"]["status"] == "passed"
    assert service.state["v3_planning_policy"]["status"] == "confirmed"
    assert service.route_planner_v3_validation_snapshot()["stale_count"] == 0
    # The published adoptions stay current.
    assert service.v3_operational_adoptions_snapshot()["current_count"] == 1


def test_publish_reports_the_downstream_invalidation_set(tmp_path):
    from cns_planner.application.v3_operational_adoption_service import V3_DOWNSTREAM_RESULTS

    service = workflow(tmp_path)
    build_validated(service)
    preview = None
    adoption = adoption_service(service, relax_production=True)
    preview = service.preview_v3_operational_adoption({"evidence_source": REAL})
    assert preview["downstream_invalidation"] == list(V3_DOWNSTREAM_RESULTS)
    outcome = service.apply_v3_operational_adoption({
        "confirmed": True, "evidence_source": REAL,
        "validation_ids": [preview["projections"][0]["validation_id"]],
    })
    assert outcome["downstream_invalidation"] == list(V3_DOWNSTREAM_RESULTS)
    # coverage_3d and every later CNS stage must not silently stay "passed".
    assert service.state["result_statuses"].get("coverage_3d") != "passed"


def test_a_source_change_stales_only_v3_adoptees(tmp_path):
    from cns_planner.application.v3_operational_adoption_service import V3_DOWNSTREAM_RESULTS

    service = workflow(tmp_path)
    build_validated(service)
    projection = publish(service)[1]
    legacy = {"route_id": "R-LEGACY", "status": "passed", "kind": "operational",
              "path": [[122.0, 29.9], [122.001, 29.901]]}
    service.state["operational_routes"] = [*deepcopy(service.state["operational_routes"]), legacy]
    service.save()
    outcome = service.v3_operational_adoption_service.stale_for_sources(["terrain_dtm"])
    assert outcome["status"] == "passed"
    assert outcome["stale_route_ids"] == [projection["route_id"]]
    routes = {route["route_id"]: route for route in service.state["operational_routes"]}
    assert routes[projection["route_id"]]["status"] == "stale"
    assert routes["R-LEGACY"]["status"] == "passed"
    # Ignoring an untracked role changes nothing.
    before = deepcopy(service.state["v3_operational_adoptions"])
    assert service.v3_operational_adoption_service.stale_for_sources(["population"])["status"] == (
        "not_applicable"
    )
    assert service.state["v3_operational_adoptions"] == before


def test_source_verification_propagates_to_the_v3_adoption(tmp_path):
    service = workflow(tmp_path)
    build_validated(service)
    publish(service)
    source = tmp_path / "fabdem.tif"
    source.write_bytes(b"synthetic-inventory")
    # The first verification only records the baseline digest.
    service.source_audit_service.verify("terrain_dtm", source)
    assert service.v3_operational_adoptions_snapshot()["items"][0]["status"] == "published"
    # A changed digest is a source change and must stale the V3 adoptee.
    source.write_bytes(b"changed-inventory")
    service.source_audit_service.verify("terrain_dtm", source)
    items = service.v3_operational_adoptions_snapshot()["items"]
    assert items[0]["status"] == "stale"
    assert items[0]["current_applicability"] == "stale"
    assert "terrain_dtm" in (items[0]["stale_reason"] or "")
    routes = {route["route_id"]: route for route in service.state["operational_routes"]}
    assert routes[items[0]["route_id"]]["status"] == "stale"


def test_a_stale_adoption_assesses_as_stale_instead_of_a_verdict(tmp_path):
    service = workflow(tmp_path)
    build_validated(service)
    publish(service)
    service.v3_operational_adoption_service.stale_for_sources(["buildings"])
    bundle = service.assess_v3_adopted_route({})
    assert bundle["assessment_status"] == "stale"
    assert bundle["requirement_verdict"] == "unknown"
    assert bundle["blocking_reasons"]


# --------------------------------------------------------------------------------------
# 7. CNS assessment bridge
# --------------------------------------------------------------------------------------


def test_bridge_is_incomplete_with_a_blocking_reason_when_prerequisites_are_missing(tmp_path):
    service = workflow(tmp_path)
    build_validated(service)
    publish(service)
    bundle = service.assess_v3_adopted_route({})
    assert bundle["assessment_status"] == "incomplete"
    assert bundle["requirement_verdict"] == "unknown"
    blocking = set(bundle["blocking_reasons"])
    assert "missing_required_cns" in blocking
    assert "missing_selected_aircraft_profile" in blocking
    assert "missing_existing_cns_facilities" in blocking
    for name in CNS_STAGES:
        assert bundle["stage_results"][name]["status"] == "not_run"
    assert bundle["route_validation_status"] == "validated_route"
    assert bundle["route_validation_unchanged"] is True


def test_bridge_runs_the_existing_p7_p8_p9_stages(tmp_path):
    service = workflow(tmp_path)
    build_validated(service)
    adoption, projection = publish(service)
    prepare_cns(service, projection["route"])
    bundle = service.assess_v3_adopted_route({})
    stages = bundle["stage_results"]
    # The P7 stage entry keeps the container's own status while the *required-subsystem*
    # route status is what the bridge gates on: an unrequired N/S subsystem may be
    # missing_data without blocking the adoption.
    assert stages["P7"]["status"] in ("passed", "missing_data")
    assert stages["P7"]["route_statuses"] == {projection["route_id"]: "passed"}
    # P8 runs through the real static-capability engine.  With only one confirmed ground
    # provider and no verified provider independence it deliberately reports an
    # evidence-limited (non-meets) status -- never a fabricated pass.
    assert stages["P8"]["status"] in ("meets_under_model", "unknown")
    assert stages["P8"]["route_statuses"][projection["route_id"]] in (
        "meets_under_model", "unknown",
    )
    assert stages["P9"]["status"] == "passed"
    assert stages["P9"]["route_statuses"] == {projection["route_id"]: "passed"}
    assert stages["P7"]["algorithm_id"] == "geometric_coverage_3d_v1"
    assert stages["P8"]["algorithm_id"] == "cns_service_capability_v1"
    assert stages["P9"]["algorithm_id"] == "route_service_timeline_v1"
    # The bridge reused the existing persisted containers instead of recomputing.
    assert service.state["coverage_3d"]["route_count"] == 1
    assert service.state["cns_service_capability"]["route_count"] == 1


def test_bridge_reports_missing_timing_as_incomplete(tmp_path):
    service = workflow(tmp_path)
    build_validated(service)
    adoption, projection = publish(service)
    prepare_cns(service, projection["route"], timing=False)
    bundle = service.assess_v3_adopted_route({})
    assert bundle["assessment_status"] == "incomplete"
    assert bundle["stage_results"]["P9"]["status"] in ("missing_data", "unknown")
    assert "unknown" in bundle["stage_results"]["P9"]["route_statuses"].values()
    assert bundle["requirement_verdict"] == "unknown"


def test_an_unmet_requirement_is_complete_does_not_meet_and_leaves_the_route_validated(tmp_path):
    service = workflow(tmp_path)
    build_validated(service)
    adoption, projection = publish(service)
    # The device is confirmed at 200 ms; the requirement demands 50 ms.
    prepare_cns(service, projection["route"], latency_s=0.05)
    bundle = service.assess_v3_adopted_route({})
    assert bundle["assessment_status"] == "complete"
    assert bundle["requirement_verdict"] == "does_not_meet"
    assert bundle["blocking_reasons"] == []
    assert bundle["stage_results"]["P8"]["status"] == "does_not_meet_under_model"
    # The route validation is untouched: CNS compliance never rewrites route safety.
    assert bundle["route_validation_status"] == "validated_route"
    validation = validation_entry(service)["result"]
    assert validation["status"] == "validated_route"
    assert validation["operational_route"] is False
    assert validation["cns_assessed"] is False
    assert service.route_planner_v3_validation_snapshot()["stale_count"] == 0


def test_bundle_is_linked_to_the_adoption_and_persisted(tmp_path):
    service = workflow(tmp_path)
    build_validated(service)
    adoption, projection = publish(service)
    prepare_cns(service, projection["route"])
    bundle = service.assess_v3_adopted_route({})
    assert bundle["route_id"] == projection["route_id"]
    assert bundle["adoption_id"] == (
        service.v3_operational_adoptions_snapshot()["items"][0]["adoption_id"]
    )
    assert bundle["validation_id"] == projection["validation_id"]
    assert bundle["bundle_id"].startswith("V3CNS-")
    assert bundle["provenance"]["no_formula_copied"] is True
    assert bundle["semantics"]["reuses_existing_p7_p8_p9_p10_only"] is True
    assert bundle["semantics"]["assessment_completeness_is_not_requirement_verdict"] is True
    snapshot = service.v3_cns_assessment_snapshot()
    assert snapshot["count"] == 1
    assert snapshot["items"][0]["bundle_id"] == bundle["bundle_id"]
    items = service.v3_operational_adoptions_snapshot()["items"]
    assert items[0]["cns_assessment"]["bundle_id"] == bundle["bundle_id"]
    assert items[0]["cns_assessment"]["requirement_verdict"] == bundle["requirement_verdict"]


def test_assess_requires_a_published_adoption(tmp_path):
    service = workflow(tmp_path)
    build_validated(service)
    with pytest.raises(ValueError, match="V3 operational adoption"):
        service.assess_v3_adopted_route({})


def test_bundle_normalization_forces_the_separation_semantics():
    bundle = normalize_v3_cns_assessment_bundle({
        "route_id": "R1", "assessment_status": "complete", "requirement_verdict": "does_not_meet",
        "route_validation_unchanged": False,
        "stage_results": {"P7": {"status": "passed"}},
    })
    assert bundle["assessment_status"] == "complete"
    assert bundle["requirement_verdict"] == "does_not_meet"
    assert bundle["route_validation_unchanged"] is True
    assert bundle["semantics"]["cns_gap_never_rewrites_route_validation"] is True
    assert bundle["semantics"]["unknown_is_never_a_pass"] is True
    for name in CNS_STAGES:
        assert name in bundle["stage_results"]


def test_assessment_status_vocabulary_is_closed():
    assert ASSESSMENT_STATUSES == ("not_started", "incomplete", "complete", "stale")
    assert REQUIREMENT_VERDICTS == ("meets", "does_not_meet", "unknown")
    assert ADOPTION_STATUSES == ("published", "stale", "revoked")
    assert CNS_STAGES == ("P7", "P8", "P9", "P10")
    assert V3D_EVIDENCE_SOURCES == ("canonical_synthetic", "configured_real_sources")
    for status in V3C_RESULT_STATUSES:
        assert status not in ASSESSMENT_STATUSES


# --------------------------------------------------------------------------------------
# 8. P7 compatibility (altitude parity)
# --------------------------------------------------------------------------------------


def test_p7_altitude_parity_with_the_derived_profile(tmp_path):
    service = workflow(tmp_path)
    build_validated(service)
    adoption, projection = publish(service)
    prepare_cns(service, projection["route"])
    service.spatial_3d_service.evaluate()
    coverage = service.state["coverage_3d"]["routes"][0]
    profile = service.state["spatial_3d"]["route_altitude_profiles"][projection["route_id"]]
    path = service.state["operational_routes"][0]["path"]
    assert coverage["altitude_profile"]["route_id"] == projection["route_id"]
    # The profile P7 consumed is the V3-derived one, not a user-typed substitute.
    assert coverage["altitude_profile"]["source"] == ROUTE_SOURCE_TYPE
    assert coverage["altitude_profile"]["vertical_reference"] == "egm2008_orthometric"
    # P7 samples report EGM2008 altitude with a resolved vertical status.
    assert coverage["samples"]
    assert all(sample["vertical_status"] == "passed" for sample in coverage["samples"])
    altitudes = [sample["altitude_egm2008_m"] for sample in coverage["samples"]]
    assert all(value is not None for value in altitudes)
    # Parity: P7's profile interpolation reproduces the derived waypoint altitudes.
    from cns_planner.algorithms.coverage.geometric_3d import route_profile_height
    for waypoint in profile["waypoints"]:
        offset = waypoint["distance_along_route_m"]
        interpolated = route_profile_height(profile, offset, projection["path_metrics"]["profile_length_m"])
        assert interpolated == pytest.approx(waypoint["altitude_m"], abs=1e-9)
    # No AGL/WGS84 mixing: the container records one vertical reference only.
    assert {sample["vertical_status"] for sample in coverage["samples"]} == {"passed"}
    assert "ellipsoidal" not in json.dumps(coverage["samples"])
    assert all(len(point) == 2 for point in path)


def test_projection_records_the_compatibility_semantics_and_fingerprint(tmp_path):
    service = workflow(tmp_path)
    build_validated(service)
    projection = publish(service)[1]
    compatibility = projection["compatibility"]
    assert compatibility["projection_semantics"].startswith("validated_v3c_linearized_metric_geometry")
    assert compatibility["profile_derivation"].startswith("v3c_z_s_sampled")
    assert compatibility["simplification_applied"] is False
    assert compatibility["crs_mixing"] is False
    assert compatibility["vertex_count"] == len(projection["route"]["path"])
    assert projection["projection_fingerprint"].startswith("V3DPROJ-")
    assert projection["projection_id"].endswith(projection["projection_fingerprint"][-20:])


# --------------------------------------------------------------------------------------
# 9. result isolation / no regression
# --------------------------------------------------------------------------------------


def test_v3d_result_is_json_safe(tmp_path):
    service = workflow(tmp_path)
    build_validated(service)
    adoption, projection = publish(service)
    prepare_cns(service, projection["route"])
    service.assess_v3_adopted_route({})
    snapshot = service.snapshot()
    json.dumps(snapshot, allow_nan=False, ensure_ascii=False)
    assert snapshot["v3_operational_adoptions"]["count"] == 1
    assert snapshot["v3_cns_assessment"]["count"] == 1
    assert snapshot["v3_operational_publish_status"]["stage"] == "V3-D"


def test_publish_status_lists_the_validation_options(tmp_path):
    service = workflow(tmp_path)
    build_validated(service)
    adoption_service(service)
    status = service.v3_operational_publish_status()
    assert status["stage"] == "V3-D"
    assert status["algorithm"]["registered_in_algorithm_registry"] is False
    assert len(status["options"]) == 1
    option = status["options"][0]
    assert option["status"] == "validated_route"
    assert option["eligible"] is True
    assert option["production_eligible"] is False
    assert status["boundaries"]["production_apply_requires_configured_real_sources"] is True
    assert status["boundaries"]["synthetic_preview_only"] is True
    assert status["boundaries"]["cns_excluded_from_route_cost"] is True


def test_publish_status_is_blocked_without_an_eligible_validation(tmp_path):
    service = workflow(tmp_path)
    status = service.v3_operational_publish_status()
    assert status["status"] == "blocked"
    assert "no_eligible_current_v3c_validated_route" in status["blocking_reasons"]


def test_v1_and_v2_route_planners_are_untouched_by_v3d(tmp_path):
    from cns_planner.algorithms.registry import build_default_algorithm_registry
    from cns_planner.route_planner.risk_aware_v2 import RiskAwareRoutePlannerV2

    catalog = build_default_algorithm_registry({}).catalog()
    route_entries = {
        (entry["algorithm_id"], entry["version"])
        for entry in catalog
        if entry.get("algorithm_type") == "route_planner"
    }
    assert ("route_planner_v1", "1.0") in route_entries
    assert ("risk_aware_route_planner_v2", "2.0") in route_entries
    assert all("v3" not in algorithm_id for algorithm_id, _ in route_entries)
    assert RiskAwareRoutePlannerV2.algorithm_id == "risk_aware_route_planner_v2"

    service = workflow(tmp_path)
    build_validated(service)
    publish(service)
    assert service.state["algorithm_selection"]["route_planner"]["algorithm_id"] == "route_planner_v1"


def test_report_exposes_the_v3_provenance_without_merging_verdicts(tmp_path):
    service = workflow(tmp_path)
    build_validated(service)
    adoption, projection = publish(service)
    prepare_cns(service, projection["route"], latency_s=0.05)
    bundle = service.assess_v3_adopted_route({})
    from cns_planner.reporting.builder import ReportBuilder

    model = ReportBuilder().build(service.state, [], "2026-01-01T00:00:00Z")
    section = model["sections"]["v3_validated_route_provenance"]
    assert section["adoption_count"] == 1
    route = section["routes"][0]
    assert route["route_id"] == projection["route_id"]
    assert route["validated_route"]["validation_id"] == projection["validation_id"]
    assert route["validated_route"]["curve_chord_error_m"] == pytest.approx(0.5)
    assert route["representation"]["vertical_reference"] == "egm2008_orthometric"
    assert route["representation"]["egm2008_in_geojson_third_coordinate"] is False
    assert route["representation"]["two_dimensional_path_only"] is True
    assert route["representation"]["profile_locked"] is True
    assert route["cns_assessment"]["assessment_status"] == "complete"
    assert route["cns_assessment"]["requirement_verdict"] == "does_not_meet"
    assert route["cns_assessment"]["stage_statuses"]["P8"] == "does_not_meet_under_model"
    semantics = section["semantics"]
    assert semantics["cns_excluded_from_v3_search_cost"] is True
    assert semantics["route_safety_is_not_cns_compliance"] is True
    assert semantics["cns_gap_never_rewrites_route_validation"] is True
    assert semantics["assessment_completeness_is_not_requirement_verdict"] is True
    # The report never presents a single merged red/green status.
    assert "overall_status" not in section
    assert "status" not in route


def test_adoption_normalization_pins_the_representation_boundaries():
    adoption = normalize_v3_operational_adoption({
        "adoption_id": "V3D-1", "route_id": "R1", "status": "published",
        "validation_ids": ["V3C-1"],
        "route_provenance": {"source_type": "something_else", "planner_family": "other"},
    })
    assert adoption["route_provenance"]["source_type"] == ROUTE_SOURCE_TYPE
    assert adoption["route_provenance"]["planner_family"] == "route_planner_v3"
    assert adoption["route_provenance"]["vertical_reference"] == "egm2008_orthometric"
    assert adoption["note"]
    assert adoption["cns_assessment"]["assessment_status"] == "not_started"
    assert adoption["cns_assessment"]["requirement_verdict"] == "unknown"


def test_adoptions_collection_normalization_is_defensive():
    collection = normalize_v3_operational_adoptions({
        "items": ["not-a-dict", {"adoption_id": "V3D-1", "status": "invented"}],
    })
    assert collection["count"] == 1
    assert collection["items"][0]["status"] == "published"
    assert normalize_v3_operational_adoptions(None) == empty_v3_operational_adoptions()
