"""Route Safety Evidence Assessment V2 tests.

The suite builds a deterministic published layered lineage by stubbing only the four
read-only upstream projections (candidates / validations / profiles / adoptions) and the
existing CNS result containers, then exercises the V2 evidence aggregation itself.

Covered contracts:

* lineage resolution and ``not_ready`` without a published adoption;
* the four evidence domains and their status vocabularies;
* ``hard_constraint_failed`` only for an explicit terrain/building violation or a confirmed
  regulatory violation;
* the "CNS confirmed gap == operational support deficit, never route unsafe" boundary;
* overall status as an *evidence* status (never a safety score);
* fingerprints / staleness propagation and the "never write upstream" boundary;
* display-only airspace independence, persistence/backfill and the API surface.
"""

from copy import deepcopy
from pathlib import Path

from cns_planner.algorithms.grid.service import WorkspaceGridService
from cns_planner.api.router import ApiRouter
from cns_planner.application.project_state import blank_project, normalize_project
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.regulatory_constraints import normalize_regulatory_constraints
from cns_planner.domain.route_safety_evidence_v2 import (
    ALGORITHM_ID, ALGORITHM_VERSION, DOMAIN_IDS, overall_status_for,
)

DEFAULTS = Path(__file__).parents[1] / "cns_planner" / "config" / "defaults.json"
PATH = [[122.0, 30.0], [122.02, 30.0]]
ALTITUDE = 100.0


# --------------------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------------------


def candidate(**overrides):
    payload = {
        "candidate_id": "LC-1", "route_id": "R-1", "altitude_layer_id": "L-100",
        "status": "candidate", "current_applicability": "current",
        "candidate_fingerprint": "candidate-fp", "path": deepcopy(PATH),
        "algorithm_id": "layered_risk_aware_theta_star_v2",
        "algorithm_version": "2.0",
        "planning_objective": {
            "risk_exposure_index_m": 12.5, "weighted_risk": 10.0, "turn_cost_m": 0.0,
            "distance_m": 2000.0, "total_cost": 210.0,
            "risk_weight": 0.8, "turn_weight": 0.1, "distance_weight": 0.1,
            "weights_provenance": "user_defined_baseline",
        },
        "route_risk_density": {
            "value": 0.00625, "threshold": 1.0, "margin": 0.99375, "status": "passed",
            "source": "user_defined_temporary_wide_constraint", "temporary_constraint": True,
            "objective_term": False,
        },
        "search_parameters": {
            "heading_bin_count": 8, "theta_min_deg": 5.0,
            "parameter_origin": "software_baseline", "engineering_confirmed": False,
        },
        "provenance": {"regulatory_compliance": {
            "status": "not_evaluated", "regulatory_compliance": "not_evaluated",
        }},
    }
    payload.update(overrides)
    return payload


def validation(**overrides):
    payload = {
        "validation_id": "LRV-1", "status": "validated_candidate",
        "status_reason": None, "current_applicability": "current",
        "candidate": {
            "candidate_id": "LC-1", "route_id": "R-1", "altitude_layer_id": "L-100",
            "candidate_fingerprint": "candidate-fp",
        },
        "route": {
            "path": deepcopy(PATH), "path_crs": "OGC:CRS84",
            "nominal_altitude_m": ALTITUDE, "vertical_reference": "egm2008_orthometric",
            "semantics": "strategic_route_centerline_not_aircraft_kinematic_trajectory",
        },
        "domains": {
            "terrain": {"status": "passed", "minimum_margin": 40.0},
            "building": {"status": "passed", "minimum_margin": 30.0},
        },
        "minimum_margins": {"terrain_vertical_m": 40.0, "building_vertical_m": 30.0},
        "failed_intervals": [], "unresolved_intervals": [],
        "critical_evidence": [{"domain": "terrain", "status": "passed"}],
        "resource_limits": {"limit_reached": False},
        "source_type": "configured_real_sources",
        "source_audits": {"terrain_dtm": {"status": "verified", "file_name": "dtm.tif"}},
        "validator_versions": {"terrain": "source_native_terrain_validator_v1"},
        "policies": {"terrain_vertical_clearance_m": 50.0},
        "fingerprints": {"validation_fingerprint": "validation-fp"},
        "provenance": {"source_adapter": "test"}, "semantics": {},
    }
    payload.update(overrides)
    return payload


def profile(**overrides):
    payload = {
        "profile_id": "RRP-1", "status": "passed", "current_applicability": "current",
        "candidate": {"candidate_id": "LC-1", "candidate_fingerprint": "candidate-fp"},
        "domains": {"ground": {
            "domain_id": "ground", "status": "passed",
            "exposure_index_m": 5.0, "mean_index": 0.25, "max_index": 0.5,
            "max_location": {"grid_id": "G-1"}, "resolved_length_m": 2000.0,
            "unresolved_length_m": 0.0, "coverage": 1.0,
            "high_risk": {"status": "not_configured", "length_m": None, "intervals": None},
            "classification": {"status": "not_configured", "level": None},
        }},
        "policy": {"domains": {"ground": {
            "domain_id": "ground", "medium_min": None, "high_min": None,
            "status": "not_configured", "source": None, "provenance": None,
        }}},
        "fingerprints": {"profile_fingerprint": "profile-fp"},
    }
    payload.update(overrides)
    return payload


def adoption(**overrides):
    payload = {
        "adoption_id": "LRA-1", "route_id": "R-1", "status": "published",
        "current_applicability": "current", "validation_id": "LRV-1",
        "validation_fingerprint": "validation-fp", "candidate_id": "LC-1",
        "candidate_fingerprint": "candidate-fp", "projection_fingerprint": "projection-fp",
        "altitude_layer_id": "L-100",
        "source_type": "layered_candidate_operational_adoption_v1",
    }
    payload.update(overrides)
    return payload


def gap_route(subsystems):
    return {
        "route_id": "R-1", "route_length_m": 2000.0, "status": "satisfied",
        "subsystems": subsystems,
    }


def gap_subsystem(code, status="satisfied", **overrides):
    payload = {
        "subsystem": code, "status": status, "required": True,
        "gap_length_m": 0.0 if status != "confirmed_gap" else 400.0,
        "gap_fraction": 0.0 if status != "confirmed_gap" else 0.2,
        "unknown_length_m": 0.0 if status not in ("unknown", "missing_data") else 2000.0,
        "satisfied_length_m": 2000.0 if status == "satisfied" else 1600.0,
        "max_continuous_gap_length_m": 0.0 if status != "confirmed_gap" else 400.0,
        "max_continuous_gap_duration_s": 0.0 if status != "confirmed_gap" else 40.0,
        "gap_segment_count": 0 if status != "confirmed_gap" else 1,
        "planning_assessment": {"status": "satisfied"},
        "operational_assessment": {"status": "satisfied"},
        "required_length_m": 2000.0,
    }
    payload.update(overrides)
    return payload


def subsystem(code, status="passed", **overrides):
    payload = {"subsystem": code, "status": status}
    payload.update(overrides)
    return payload


def harness(tmp_path, *, name="project.json", adoptions=None, validations=None,
            candidates=None, profiles=None, upstream=None, regulatory=None, route=None,
            airspace=None):
    service = WorkflowService(tmp_path / name, DEFAULTS)
    state = service.state
    state["operational_routes"] = [route or {
        "route_id": "R-1", "status": "passed", "path": deepcopy(PATH),
        "provenance": {"source_type": "layered_candidate_operational_adoption_v1"},
    }]
    candidate_items = candidates if candidates is not None else [candidate()]
    validation_items = validations if validations is not None else [validation()]
    profile_items = profiles if profiles is not None else [profile()]
    adoption_items = adoptions if adoptions is not None else [adoption()]
    service.layered_route_planner_service.result_snapshot = lambda: {
        "status": "candidate", "count": len(candidate_items),
        "active_candidate_id": "LC-1", "items": deepcopy(candidate_items), "masks": {},
    }
    service.layered_route_validation_service.result_snapshot = lambda: {
        "status": "passed", "count": len(validation_items),
        "active_validation_id": "LRV-1", "items": deepcopy(validation_items),
    }
    service.route_risk_profile_service.result_snapshot = lambda: {
        "status": "passed", "count": len(profile_items), "items": deepcopy(profile_items),
    }
    service.layered_operational_adoption_service.result_snapshot = lambda: {
        "status": "passed", "count": len(adoption_items), "items": deepcopy(adoption_items),
    }
    state["regulatory_constraints"] = normalize_regulatory_constraints(regulatory)
    if upstream:
        for name, value in upstream.items():
            state[name] = deepcopy(value)
    if airspace is not None:
        state["airspace_policies"] = deepcopy(airspace)
    return service


def evaluate(service, payload=None):
    collection = service.evaluate_route_safety_evidence_v2(payload or {})
    return collection["items"][-1]


def passing_cns(**overrides):
    """A CNS support chain where every subsystem is met and no gap exists."""

    payload = {
        "coverage_3d": {
            "status": "passed", "algorithm_id": "geometric_coverage_3d_v1",
            "algorithm_version": "1.0", "input_fingerprint": "coverage-fp",
            "routes": [{
                "route_id": "R-1", "route_length_m": 2000.0, "status": "passed",
                "subsystems": [subsystem(code, "passed", covered_length_m=2000.0,
                                         uncovered_length_m=0.0, covered_fraction=1.0)
                               for code in ("C", "N", "S")],
            }],
        },
        "cns_service_capability": {
            "status": "passed", "algorithm_id": "cns_service_capability_v1",
            "algorithm_version": "1.0", "input_fingerprint": "capability-fp",
            "routes": [{
                "route_id": "R-1", "status": "passed",
                "subsystems": [subsystem(code, "meets_under_model", meets_length_m=2000.0,
                                         fail_length_m=0.0, unknown_length_m=0.0,
                                         meets_fraction=1.0)
                               for code in ("C", "N", "S")],
            }],
        },
        "cns_gap_analysis_v2": {
            "status": "passed", "algorithm_id": "cns_gap_analysis_v2",
            "algorithm_version": "2.0", "input_fingerprint": "gap-fp",
            "routes": [gap_route([gap_subsystem(code, "satisfied") for code in ("C", "N", "S")])],
        },
    }
    payload.update(overrides)
    return payload


REGULATORY_BLOCKING = {
    "status": "configured", "source": "test-dataset", "confirmed": True,
    "items": [{
        "constraint_id": "RC-1", "constraint_type": "no_fly_zone",
        "geometry": {"kind": "bbox", "bbox": [121.99, 29.99, 122.03, 30.01]},
        "vertical_scope": {
            "vertical_reference": "egm2008_orthometric", "lower_egm2008_m": 0.0,
            "upper_egm2008_m": 200.0, "unbounded": False, "source": "test",
        },
        "source": "test", "evidence": {"ticket": "T-1"}, "confirmed": True,
        "status": "confirmed",
    }],
}

#: A *configured* regulatory dataset that the published route does not intersect.
REGULATORY_CLEAR = {
    "status": "configured", "source": "test-dataset", "confirmed": True,
    "items": [{
        **REGULATORY_BLOCKING["items"][0],
        "geometry": {"kind": "bbox", "bbox": [130.0, 40.0, 131.0, 41.0]},
    }],
}


# --------------------------------------------------------------------------------------
# 9-11. lineage, not_ready and the geometry evidence
# --------------------------------------------------------------------------------------


def test_complete_current_lineage_can_be_evaluated(tmp_path):
    service = harness(tmp_path, upstream=passing_cns(), regulatory=REGULATORY_CLEAR)
    readiness = service.route_safety_evidence_v2_readiness()
    assert readiness["status"] == "ready"
    assert readiness["lineage"]["complete"] is True
    record = evaluate(service)
    assert record["status"] == "evidence_complete"
    assert record["status_reason"] is None
    assert record["route_id"] == "R-1"
    assert record["adoption_id"] == "LRA-1"
    assert record["assessment_id"].startswith("RSE-")
    assert record["created_at"]
    assert record["current_applicability"] == "current"
    assert record["lineage"]["complete"] is True
    assert record["lineage"]["validation_id"] == "LRV-1"
    assert record["lineage"]["candidate_id"] == "LC-1"
    assert record["lineage"]["route_risk_profile_id"] == "RRP-1"
    assert set(record["domains"]) == set(DOMAIN_IDS)
    # The result is auditable and carries the declared dependency references.
    for key in (
        "assessment_fingerprint", "operational_route_adoption_fingerprint",
        "validation_fingerprint", "candidate_fingerprint",
        "route_risk_profile_fingerprint", "regulatory_dataset_fingerprint",
        "coverage_3d_fingerprint", "cns_service_capability_fingerprint",
        "cns_gap_v2_fingerprint", "evaluator_version",
    ):
        assert key in record["fingerprints"], key
    assert record["fingerprints"]["evaluator_version"] == f"{ALGORITHM_ID}@{ALGORITHM_VERSION}"
    assert record["fingerprints"]["validation_fingerprint"] == "validation-fp"
    assert record["provenance"]["upstream_modified"] is False
    assert record["provenance"]["recomputed_upstream_algorithms"] == []


def test_no_published_adoption_is_not_ready_and_never_a_failure(tmp_path):
    service = harness(tmp_path, adoptions=[])
    readiness = service.route_safety_evidence_v2_readiness()
    assert readiness["status"] == "not_ready"
    assert "published_layered_operational_adoption" in (
        readiness["lineage"]["missing_links"]
    )
    record = evaluate(service)
    assert record["status"] == "not_ready"
    assert record["status_reason"] == "no_published_layered_operational_adoption"
    assert record["current_applicability"] == "not_ready"
    assert all(
        record["domains"][domain_id]["status"] == "not_ready" for domain_id in DOMAIN_IDS
    )
    assert record["evidence_summary"]["hard_constraint_domains"] == []
    assert "not_ready" in record["limitations"][0]


def test_geometry_domain_transcribes_the_existing_validation_evidence(tmp_path):
    service = harness(tmp_path, upstream=passing_cns(), regulatory=REGULATORY_CLEAR)
    record = evaluate(service)
    geometry = record["domains"]["geometry_obstacle"]
    assert geometry["status"] == "validated"
    assert geometry["hard_constraint_failure"] is False
    assert geometry["metrics"]["terrain_status"] == "passed"
    assert geometry["metrics"]["building_status"] == "passed"
    assert geometry["metrics"]["terrain_minimum_margin_m"] == 40.0
    assert geometry["metrics"]["building_minimum_margin_m"] == 30.0
    assert geometry["metrics"]["fixed_cruise_altitude_m"] == ALTITUDE
    assert geometry["metrics"]["altitude_layer_id"] == "L-100"
    assert geometry["metrics"]["unresolved_interval_count"] == 0
    assert geometry["metrics"]["resource_limited"] is False
    assert geometry["metrics"]["validation_fingerprint"] == "validation-fp"
    assert geometry["evidence"]["validation_id"] == "LRV-1"
    assert geometry["evidence"]["source_type"] == "configured_real_sources"
    assert geometry["evidence"]["provenance"]["source_adapter"] == "test"
    assert geometry["sources"][0]["role"] == "terrain_dtm"
    assert geometry["semantics"]["consumes_existing_layered_route_validation"] is True
    assert geometry["semantics"]["does_not_reimplement_terrain_or_building_validator"] is True
    assert geometry["semantics"]["only_explicit_clearance_violation_is_a_failure"] is True
    assert geometry["semantics"]["validated_route_is_not_a_safe_route"] is True
    assert record["status"] == "evidence_complete"


# --------------------------------------------------------------------------------------
# 12. hard geometry failure
# --------------------------------------------------------------------------------------


def test_terrain_or_building_violation_is_the_only_geometry_hard_failure(tmp_path):
    service = harness(tmp_path, upstream=passing_cns(), validations=[validation(
        status="failed", status_reason="failed_domain_evidence",
        domains={
            "terrain": {"status": "failed", "minimum_margin": -5.0,
                        "violations": [{"reason_id": "terrain_clearance_violation"}]},
            "building": {"status": "passed", "minimum_margin": 30.0},
        },
        minimum_margins={"terrain_vertical_m": -5.0, "building_vertical_m": 30.0},
        failed_intervals=[{"domain": "terrain", "reason_id": "terrain_clearance_violation"}],
    )])
    record = evaluate(service)
    geometry = record["domains"]["geometry_obstacle"]
    assert geometry["status"] == "failed"
    assert geometry["hard_constraint_failure"] is True
    assert geometry["metrics"]["terrain_minimum_margin_m"] == -5.0
    assert geometry["metrics"]["failed_interval_count"] == 1
    assert record["status"] == "hard_constraint_failed"
    assert record["evidence_summary"]["hard_constraint_domains"] == ["geometry_obstacle"]


def test_unresolved_or_incomplete_geometry_is_never_a_failure_and_never_a_pass(tmp_path):
    for status, expected in (
        ("unresolved", "evidence_unresolved"),
        ("validation_incomplete", "evidence_unresolved"),
        ("not_ready", "evidence_incomplete"),
    ):
        service = harness(
            tmp_path, name=f"{status}.json", upstream=passing_cns(),
            validations=[validation(status=status, domains={}, failed_intervals=[])],
        )
        record = evaluate(service)
        geometry = record["domains"]["geometry_obstacle"]
        assert geometry["hard_constraint_failure"] is False, status
        assert record["status"] == expected, (status, record["status"])


# --------------------------------------------------------------------------------------
# 13. no fabricated overall risk score
# --------------------------------------------------------------------------------------


def test_theta_objective_and_profile_domains_are_never_merged_into_one_score(tmp_path):
    service = harness(tmp_path, upstream=passing_cns())
    record = evaluate(service)
    ground = record["domains"]["ground_exposure"]
    assert ground["status"] == "assessed"
    evidence = ground["evidence"]
    # Two separate, independently auditable blocks.
    assert evidence["theta_star_planning_objective"]["used_in_search"] is True
    assert evidence["theta_star_planning_objective"]["post_hoc"] is False
    assert evidence["theta_star_planning_objective"]["risk_exposure_index_m"] == 12.5
    assert evidence["theta_star_planning_objective"]["weights"] == {
        "risk": 0.8, "turn": 0.1, "distance": 0.1,
    }
    assert evidence["route_risk_density"]["objective_term"] is False
    assert evidence["route_risk_density"]["value"] == 0.00625
    assert ground["metrics"]["profile_ground_exposure_index_m"] == 5.0
    assert ground["metrics"]["profile_ground_mean_index"] == 0.25
    assert ground["metrics"]["profile_ground_max_index"] == 0.5
    assert ground["metrics"]["profile_ground_high_risk"]["status"] == "not_configured"
    assert ground["metrics"]["profile_thresholds"]["medium_min"] is None
    # No fabricated combined score anywhere.
    assert ground["metrics"]["combined_overall_risk_score"] is None
    assert ground["metrics"]["combined_overall_not_computed"] is True
    assert ground["semantics"]["no_combined_overall_risk_score"] is True
    assert ground["semantics"]["theta_objective_and_post_hoc_domains_kept_separate"] is True
    assert ground["semantics"]["low_risk_is_not_safe"] is True
    assert "combined" not in str(record["evidence_summary"]["domains"]).lower()
    assert record["evidence_summary"]["produces_safety_score"] is False
    assert record["evidence_summary"]["produces_safety_ranking"] is False
    assert record["evidence_summary"]["is_safety_certification"] is False


def test_missing_profile_keeps_ground_exposure_not_calculated(tmp_path):
    service = harness(tmp_path, upstream=passing_cns(), profiles=[])
    record = evaluate(service)
    assert record["domains"]["ground_exposure"]["status"] == "not_calculated"
    # An incomplete lineage is reported as stale evidence, never as a current assessment.
    assert record["status"] == "stale"
    assert record["current_applicability"] == "stale"
    assert "current_route_risk_profile" in record["lineage"]["missing_links"]


# --------------------------------------------------------------------------------------
# 14-15. regulatory
# --------------------------------------------------------------------------------------


def test_unconfigured_regulatory_dataset_is_never_passed(tmp_path):
    service = harness(tmp_path, upstream=passing_cns(), regulatory=None)
    record = evaluate(service)
    regulatory = record["domains"]["regulatory"]
    assert regulatory["status"] == "not_configured"
    assert regulatory["hard_constraint_failure"] is False
    assert regulatory["metrics"]["configured"] is False
    assert regulatory["metrics"]["evaluated"] is False
    assert regulatory["evidence"]["display_only_airspace_used"] is False
    assert regulatory["semantics"]["not_configured_is_not_passed"] is True
    assert regulatory["semantics"]["display_only_airspace_is_never_reused"] is True
    # Not configured is *incomplete evidence*, never a pass and never a hard failure.
    assert record["status"] == "evidence_incomplete"
    assert "regulatory" in record["evidence_summary"]["incomplete_domains"]
    assert "passed" not in regulatory["status"]


def test_configured_but_unresolved_regulatory_evidence_is_unresolved(tmp_path):
    service = harness(tmp_path, upstream=passing_cns(), regulatory={
        "status": "configured", "source": "test", "confirmed": True,
        "items": [{
            **REGULATORY_BLOCKING["items"][0],
            "status": "pending_confirmation", "confirmed": False,
        }],
    })
    record = evaluate(service)
    regulatory = record["domains"]["regulatory"]
    assert regulatory["status"] == "unresolved"
    assert regulatory["hard_constraint_failure"] is False
    assert record["status"] == "evidence_unresolved"
    assert record["evidence_summary"]["unresolved_domains"] == ["regulatory"]


def test_confirmed_regulatory_violation_is_a_hard_constraint_failure(tmp_path):
    service = harness(tmp_path, upstream=passing_cns(), regulatory=REGULATORY_BLOCKING)
    record = evaluate(service)
    regulatory = record["domains"]["regulatory"]
    assert regulatory["status"] == "blocked_by_confirmed_constraint"
    assert regulatory["hard_constraint_failure"] is True
    assert regulatory["metrics"]["blocked_constraints"] == [
        {"constraint_id": "RC-1", "segment_index": 0},
    ]
    assert record["status"] == "hard_constraint_failed"
    assert record["evidence_summary"]["hard_constraint_domains"] == ["regulatory"]
    # Geometry evidence is untouched by a regulatory violation.
    assert record["domains"]["geometry_obstacle"]["status"] == "validated"


def test_evaluated_without_a_confirmed_intersection_is_explicitly_not_compliance(tmp_path):
    service = harness(tmp_path, upstream=passing_cns(), regulatory={
        "status": "configured", "source": "test", "confirmed": True,
        "items": [{**REGULATORY_BLOCKING["items"][0],
                   "geometry": {"kind": "bbox", "bbox": [130.0, 40.0, 131.0, 41.0]}}],
    })
    record = evaluate(service)
    regulatory = record["domains"]["regulatory"]
    assert regulatory["status"] == "evaluated_no_confirmed_intersection"
    assert regulatory["metrics"]["evaluated"] is True
    assert regulatory["semantics"][
        "no_confirmed_intersection_is_not_regulatory_compliance"
    ] is True
    assert record["status"] == "evidence_complete"


# --------------------------------------------------------------------------------------
# 16-18. CNS operational support
# --------------------------------------------------------------------------------------


def test_cns_meets_reports_support_per_subsystem(tmp_path):
    service = harness(tmp_path, upstream=passing_cns(), regulatory=REGULATORY_CLEAR)
    record = evaluate(service)
    cns = record["domains"]["cns_operational_support"]
    assert cns["status"] == "supported"
    assert cns["metrics"]["operational_support_deficit"] is False
    assert [item["subsystem"] for item in cns["metrics"]["subsystems"]] == ["C", "N", "S"]
    for item in cns["metrics"]["subsystems"]:
        assert item["coverage"]["status"] == "passed"
        assert item["coverage"]["covered_fraction"] == 1.0
        assert item["capability"]["status"] == "meets_under_model"
        assert item["confirmed_gap"]["status"] == "satisfied"
        assert item["operational_support_verdict"] == "supported_under_model"
        assert item["corridor"]["consumed"] is False
    assert record["status"] == "evidence_complete"


def test_confirmed_cns_gap_is_an_operational_support_deficit_not_route_unsafe(tmp_path):
    upstream = passing_cns()
    upstream["cns_gap_analysis_v2"]["routes"][0]["subsystems"] = [
        gap_subsystem("C", "confirmed_gap", gap_length_m=400.0, gap_fraction=0.2),
        gap_subsystem("N", "satisfied"),
        gap_subsystem("S", "satisfied"),
    ]
    service = harness(tmp_path, upstream=upstream, regulatory=REGULATORY_CLEAR)
    record = evaluate(service)
    cns = record["domains"]["cns_operational_support"]
    assert cns["status"] == "operational_support_deficit"
    assert cns["metrics"]["operational_support_deficit"] is True
    assert cns["metrics"]["confirmed_gap_subsystems"] == ["C"]
    assert cns["metrics"]["subsystems"][0]["operational_support_verdict"] == (
        "operational_support_deficit"
    )
    assert cns["metrics"]["subsystems"][0]["confirmed_gap"]["gap_length_m"] == 400.0
    assert cns["metrics"]["subsystems"][0]["confirmed_gap"]["continuous_deficit"] == {
        "max_continuous_gap_length_m": 400.0,
        "max_continuous_gap_duration_s": 40.0,
        "gap_segment_count": 1,
    }
    # The hard boundary: a confirmed gap is *operational support deficit* only.
    assert cns["hard_constraint_failure"] is False
    assert cns["semantics"]["confirmed_cns_gap_is_operational_support_deficit"] is True
    assert cns["semantics"]["confirmed_cns_gap_is_not_route_unsafe"] is True
    assert cns["semantics"]["confirmed_cns_gap_never_fails_geometry_validation"] is True
    assert cns["semantics"]["confirmed_cns_gap_never_sets_hard_constraint_failed"] is True
    assert record["domains"]["geometry_obstacle"]["status"] == "validated"
    assert "operational_support_deficit" not in (
        record["evidence_summary"]["hard_constraint_domains"]
    )
    # Evidence is complete: "all domains evaluated" is not "everything is satisfied".
    assert record["status"] == "evidence_complete"
    assert record["evidence_summary"]["status"] == "evidence_complete"
    assert "safe" not in record["status"] and "unsafe" not in record["status"]


def test_cns_unknown_or_missing_evidence_is_incomplete_or_unresolved(tmp_path):
    unknown = passing_cns()
    unknown["cns_gap_analysis_v2"]["routes"][0]["subsystems"] = [
        gap_subsystem("C", "unknown"), gap_subsystem("N", "satisfied"),
        gap_subsystem("S", "satisfied"),
    ]
    record = evaluate(harness(tmp_path, name="unknown.json", upstream=unknown))
    cns = record["domains"]["cns_operational_support"]
    assert cns["status"] == "unknown"
    assert record["status"] == "evidence_unresolved"
    assert "cns_operational_support" in record["evidence_summary"]["unresolved_domains"]

    # Never run at all: not_run, and the overall status is an incomplete *evidence* status.
    record = evaluate(harness(tmp_path, name="notrun.json"))
    cns = record["domains"]["cns_operational_support"]
    assert cns["status"] == "not_run"
    assert cns["hard_constraint_failure"] is False
    assert record["status"] == "evidence_incomplete"
    assert "cns_operational_support" in record["evidence_summary"]["incomplete_domains"]


def test_current_corridor_assessment_is_consumed_when_available(tmp_path):
    upstream = passing_cns()
    upstream["cns_corridor_assessment"] = {
        "status": "passed", "algorithm_id": "cns_service_corridor_v1",
        "algorithm_version": "1.0", "input_fingerprint": "corridor-fp",
        "corridor_geometry_fingerprint": "corridor-geom-fp",
        "routes": [{
            "route_id": "R-1", "status": "passed",
            "subsystems": [{
                "subsystem": code, "status": "passed", "required_voxel_count": 10,
                "deficit_voxel_ids": [], "unknown_voxel_ids": [],
            } for code in ("C", "N", "S")],
        }],
    }
    service = harness(tmp_path, upstream=upstream)
    record = evaluate(service)
    cns = record["domains"]["cns_operational_support"]
    assert cns["evidence"]["cns_corridor_assessment"]["consumed"] is True
    assert record["fingerprints"]["cns_corridor_fingerprint"] == "corridor-fp"
    assert any(
        item["role"] == "cns_corridor_assessment" for item in cns["sources"]
    )
    assert cns["metrics"]["subsystems"][0]["corridor"]["consumed"] is True


# --------------------------------------------------------------------------------------
# 19. staleness
# --------------------------------------------------------------------------------------


def test_stale_validation_or_adoption_makes_the_assessment_not_current(tmp_path):
    service = harness(tmp_path, upstream=passing_cns(), validations=[
        validation(current_applicability="stale"),
    ])
    record = evaluate(service)
    assert record["status"] == "stale"
    assert record["current_applicability"] == "stale"
    assert record["lineage"]["complete"] is False
    assert "validation:stale" in record["lineage"]["stale_links"]
    assert evaluate(service)["status"] == "stale"

    service = harness(tmp_path, name="adoption.json", upstream=passing_cns(), adoptions=[
        adoption(current_applicability="superseded"),
    ])
    record = evaluate(service)
    assert record["status"] == "stale"
    assert "adoption:superseded" in record["lineage"]["stale_links"]


def test_dependency_change_only_stales_the_safety_evidence_v2(tmp_path):
    service = harness(tmp_path, upstream=passing_cns(), regulatory=REGULATORY_CLEAR)
    record = evaluate(service)
    assert record["status"] == "evidence_complete"
    before = deepcopy(service.state)
    # Re-evaluating with a changed RouteRiskProfile fingerprint yields a different
    # assessment; the previously stored record can no longer claim to be current.
    changed = profile(fingerprints={"profile_fingerprint": "profile-fp-2"})
    service.route_risk_profile_service.result_snapshot = lambda: {
        "status": "passed", "count": 1, "items": [deepcopy(changed)],
    }
    collection = service.route_safety_evidence_v2()
    stored = collection["items"][-1]
    assert stored["current_applicability"] == "stale_inputs_changed"

    # The invalidation chain is strictly downstream: only the evidence artifact is staled.
    service.invalidation_service.route_safety_evidence("route_risk_profile_changed")
    state = service.state
    assert state["result_statuses"]["route_safety_evidence_v2"] == "stale"
    assert state["route_safety_evidence_v2"]["items"][-1]["status"] == "stale"
    assert state["layered_route_validations"] == before["layered_route_validations"]
    assert state["layered_route_candidates"] == before["layered_route_candidates"]
    assert state["coverage_3d"] == before["coverage_3d"]
    assert state["cns_service_capability"] == before["cns_service_capability"]
    assert state["cns_gap_analysis_v2"] == before["cns_gap_analysis_v2"]


def test_regulatory_dataset_change_stales_the_safety_evidence(tmp_path):
    service = harness(tmp_path, upstream=passing_cns(), regulatory=REGULATORY_CLEAR)
    record = evaluate(service)
    assert record["status"] == "evidence_complete"
    assert record["current_applicability"] == "current"
    # The formal regulatory dataset is a declared dependency: changing it stales the evidence.
    service.set_regulatory_constraints(REGULATORY_BLOCKING)
    assert service.state["result_statuses"]["route_safety_evidence_v2"] == "stale"
    assert service.state["route_safety_evidence_v2"]["items"][-1]["status"] == "stale"
    # A stale Safety Evidence V2 never stales the formal regulatory dataset itself.
    assert service.state["regulatory_constraints"]["items"][0]["constraint_id"] == "RC-1"


def test_stale_cns_upstream_marks_the_cns_domain_stale(tmp_path):
    upstream = passing_cns()
    upstream["cns_gap_analysis_v2"]["status"] = "stale"
    record = evaluate(harness(tmp_path, upstream=upstream))
    assert record["domains"]["cns_operational_support"]["status"] == "stale"
    assert record["status"] == "stale"


# --------------------------------------------------------------------------------------
# 20-21. boundaries
# --------------------------------------------------------------------------------------


def test_evaluation_never_modifies_any_upstream_result(tmp_path):
    service = harness(tmp_path, upstream=passing_cns(), regulatory=REGULATORY_BLOCKING)
    state = service.state
    snapshot = {
        "operational_routes": deepcopy(state["operational_routes"]),
        "layered_route_validations": deepcopy(state["layered_route_validations"]),
        "layered_route_candidates": deepcopy(state["layered_route_candidates"]),
        "layered_operational_adoptions": deepcopy(state["layered_operational_adoptions"]),
        "coverage_3d": deepcopy(state["coverage_3d"]),
        "cns_service_capability": deepcopy(state["cns_service_capability"]),
        "cns_gap_analysis_v2": deepcopy(state["cns_gap_analysis_v2"]),
        "regulatory_constraints": deepcopy(state["regulatory_constraints"]),
    }
    before = deepcopy(snapshot)
    evaluate(service)
    for name, value in snapshot.items():
        assert state[name] == value, name
    assert state["operational_routes"] == before["operational_routes"]
    # And the projection never mutates the stored record either.
    first = service.route_safety_evidence_v2()
    second = service.route_safety_evidence_v2()
    assert first == second


def test_display_only_airspace_changes_never_change_the_assessment(tmp_path):
    service = harness(tmp_path, upstream=passing_cns(), airspace={"items": []})
    first = evaluate(service, {"adoption_id": "LRA-1"})
    service.state["airspace_policies"] = {
        "status": "changed", "items": [{"airspace_id": "NO-FLY-DISPLAY"}],
    }
    second = evaluate(service, {"adoption_id": "LRA-1"})
    assert second["status"] == first["status"]
    assert second["fingerprints"]["assessment_fingerprint"] == (
        first["fingerprints"]["assessment_fingerprint"]
    )
    assert second["domains"]["geometry_obstacle"]["status"] == (
        first["domains"]["geometry_obstacle"]["status"]
    )
    assert second["domains"]["regulatory"]["status"] == first["domains"]["regulatory"]["status"]


# --------------------------------------------------------------------------------------
# 22. persistence / backfill / API
# --------------------------------------------------------------------------------------


def test_assessment_survives_save_restore_and_legacy_backfill(tmp_path):
    service = harness(tmp_path, upstream=passing_cns())
    record = evaluate(service)
    service.save()
    restored = WorkflowService(tmp_path / "project.json", DEFAULTS)
    stored = restored.route_safety_evidence_v2()
    assert stored["count"] == 1
    assert stored["items"][0]["assessment_id"] == record["assessment_id"]
    assert stored["items"][0]["status"] == record["status"]

    legacy = blank_project({})
    legacy.pop("route_safety_evidence_v2", None)
    legacy["result_statuses"].pop("route_safety_evidence_v2", None)
    normalized = normalize_project(deepcopy(legacy), WorkspaceGridService())
    assert normalized["route_safety_evidence_v2"] == {
        "schema_version": "route-safety-evidence-v2-collection",
        "status": "not_calculated", "count": 0, "active_assessment_id": None,
        "items": [],
        "notes": [
            "Route Safety Evidence V2 只聚合已发布 layered operational adoption 的既有证据。",
            "不产生 safety score、排名、事故概率、SORA GRC/ARC 或自动安全等级。",
            "CNS confirmed gap 记为 operational support deficit，不代表 route unsafe。",
        ],
    }
    assert normalized["result_statuses"]["route_safety_evidence_v2"] == "not_calculated"
    assert normalize_project(deepcopy(normalized), WorkspaceGridService())[
        "route_safety_evidence_v2"
    ] == normalized["route_safety_evidence_v2"]


class ApiWorkflow:
    def route_safety_evidence_v2_readiness(self, payload=None):
        return {"status": "ready", "target": {"scope": "current"}}

    def route_safety_evidence_v2(self, payload=None):
        return {"status": "evidence_complete", "count": 1}

    def evaluate_route_safety_evidence_v2(self, payload=None):
        return {"evaluated": payload}

    def __getattr__(self, name):
        return lambda *args, **kwargs: {"unsupported": name}


class ApiContext:
    workflow = ApiWorkflow()
    data = object()


def test_route_safety_evidence_v2_api_is_additive_and_forwards_the_payload():
    router = ApiRouter(ApiContext())
    assert router.get("/api/route-safety-evidence-v2/readiness", {}, {}).data["status"] == "ready"
    assert router.get("/api/route-safety-evidence-v2", {}, {}).data["status"] == (
        "evidence_complete"
    )
    payload = {"adoption_id": "LRA-1"}
    assert router.post("/api/route-safety-evidence-v2/evaluate", payload).data == {
        "evaluated": payload,
    }


def test_no_background_evaluation_happens_on_readiness_or_state_read(tmp_path):
    service = harness(tmp_path, upstream=passing_cns())
    service.route_safety_evidence_v2_readiness()
    service.route_safety_evidence_v2()
    service.snapshot()
    assert service.state["route_safety_evidence_v2"]["count"] == 0
    assert service.state["result_statuses"]["route_safety_evidence_v2"] == "not_calculated"


# --------------------------------------------------------------------------------------
# overall status rules
# --------------------------------------------------------------------------------------


def test_overall_status_rules_are_evidence_rules_only():
    assert overall_status_for({domain_id: "not_ready" for domain_id in DOMAIN_IDS}) == (
        "not_ready"
    )
    assert overall_status_for({
        "geometry_obstacle": "stale", "ground_exposure": "assessed",
        "regulatory": "not_configured", "cns_operational_support": "not_run",
    }) == "stale"
    assert overall_status_for({
        "geometry_obstacle": "validated", "ground_exposure": "assessed",
        "regulatory": "blocked_by_confirmed_constraint", "cns_operational_support": "supported",
    }) == "hard_constraint_failed"
    assert overall_status_for({
        "geometry_obstacle": "unresolved", "ground_exposure": "assessed",
        "regulatory": "not_configured", "cns_operational_support": "supported",
    }) == "evidence_unresolved"
    assert overall_status_for({
        "geometry_obstacle": "validated", "ground_exposure": "assessed",
        "regulatory": "not_configured", "cns_operational_support": "supported",
    }) == "evidence_incomplete"
    assert overall_status_for({
        "geometry_obstacle": "validated", "ground_exposure": "assessed",
        "regulatory": "evaluated_no_confirmed_intersection",
        "cns_operational_support": "operational_support_deficit",
    }) == "evidence_complete"
