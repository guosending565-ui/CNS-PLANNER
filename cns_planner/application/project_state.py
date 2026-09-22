"""Authoritative schema-v2 project-state construction and compatibility loading."""

from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

from ..risk.v1 import RiskModelV1
from ..domain.risk_v2 import (
    default_risk_policy_v2, empty_grid_risk_v2, normalize_grid_risk_v2,
    normalize_risk_policy_v2,
)
from ..data.mapping.airspace import AirspaceGridService
from ..data.mapping.airspace_eligibility import empty_airspace_eligibility
from ..data.mapping.conflict import ConflictGridService
from ..data.mapping.population import PopulationGridService
from ..data.mapping.terrain import TerrainGridService
from ..data.mapping.traffic import TrafficGridService
from ..data.source_profiles import default_source_profiles
from ..algorithms.registry import default_algorithm_selection, normalize_algorithm_selection
from ..domain.cns_inputs import (
    normalize_candidate_site, normalize_existing_facility, pending_required_cns,
)
from ..domain.site_planning import default_site_planning_policy, normalize_site_planning_policy
from ..domain.safety_policy import default_safety_policy, normalize_safety_policy
from ..gap.v1 import CNSGapAnalyzerV1
from ..gap.v2 import CNSGapAnalyzerV2
from ..algorithms.coverage.geometric_3d import GeometricCoverage3DV1
from ..domain.spatial_3d import empty_spatial_3d, normalize_spatial_3d
from ..domain.altitude_layer_defaults import INITIALIZED_KEY as ALTITUDE_LAYER_INITIALIZED_KEY
from ..algorithms.service_capability.v1 import CNSServiceCapabilityV1
from ..algorithms.timeline.v1 import RouteServiceTimelineV1
from ..algorithms.protection.v1 import TacticalProtectionEnvelopeV1
from ..algorithms.route_vertical_profile import RouteVerticalProfileV1
from ..algorithms.encounter_3d import EncounterAssessment3DV1
from ..domain.encounter_3d import normalize_encounter_3d_assessment
from ..domain.route_vertical_profile import normalize_route_vertical_profiles
from ..domain.operational_timing import empty_operational_timing, normalize_operational_timing
from ..site_planner.reuse_first_v1 import ReuseFirstSitePlannerV1
from ..domain.closed_loop import empty_closed_loop_assessment
from ..domain.cns_corridor import (
    default_cns_corridor_policy, empty_cns_corridor_assessment,
    normalize_cns_corridor_policy,
)
from ..domain.cns_planning_objectives import (
    default_cns_planning_objectives, empty_cns_corridor_gap_assessment,
    normalize_cns_planning_objectives,
)
from ..domain.corridor_site_planning import (
    default_corridor_site_planning_policy, empty_cns_corridor_site_plan,
    normalize_corridor_site_planning_policy,
)
from ..domain.requirement_policy import (
    empty_operation_context, empty_required_cns_adoption,
    empty_required_cns_recommendation, empty_requirement_policies,
    normalize_operation_context, normalize_requirement_policies,
)
from ..domain.plan_review import empty_confirmed_plan, empty_plan_review
from ..domain.reporting import empty_report_collection
from ..domain.airspace import empty_airspace_policies, normalize_airspace_policies
from ..domain.source_audit import empty_source_audits, normalize_source_audits
from ..domain.experiment import empty_experiments, normalize_experiments
from ..route_planner_v3.contracts import (
    empty_v3_experimental_session, normalize_v3_experiments, normalize_v3_planning_policy,
)
from ..route_planner_v3.fine_contracts import (
    default_v3_fine_refinement_policy, normalize_v3_fine_refinement_policy,
)
from ..route_planner_v3.continuous_contracts import (
    default_v3_validation_policy, normalize_v3_validation_policy,
)
from ..domain.v3_operational_adoption import (
    empty_v3_cns_assessment_bundle, empty_v3_operational_adoptions,
    normalize_v3_cns_assessment_bundle, normalize_v3_operational_adoptions,
)
from ..domain.reference_route_link import (
    empty_reference_route_links, normalize_reference_route_links,
)
from ..domain.building_clearance import (
    default_building_clearance_policy, empty_building_clearance_assessment,
    normalize_building_clearance_assessment, normalize_building_clearance_policy,
)
from ..domain.layered_route import (
    default_layered_route_cost_policy, default_layered_route_feasibility_policy,
    default_layered_route_request, empty_layered_route_candidate_collection,
    normalize_layered_route_candidate_collection, normalize_layered_route_cost_policy,
    normalize_layered_route_feasibility_policy, normalize_layered_route_request,
)
from ..domain.communication_planning_field import (
    normalize_communication_planning_field,
)
from ..domain.layered_theta_v2 import (
    default_risk_density_constraint, default_theta_v2_objective_policy,
    normalize_risk_density_constraint, normalize_theta_v2_objective_policy,
)
from ..domain.population_shelter import (
    normalize_population_shelter_attribute, normalize_shelter_coefficient_policy,
    user_defined_baseline_policy,
)
from ..domain.population_nodata import (
    default_population_nodata_policy, normalize_population_nodata_policy,
)
from ..domain.planning_exposure import (
    default_planning_exposure_policy, normalize_planning_exposure_policy,
)
from ..domain.regulatory_constraints import (
    default_regulatory_constraints, normalize_regulatory_constraints,
)
from ..domain.route_risk_profile import (
    default_route_risk_profile_collection, normalize_route_risk_profile_collection,
    normalize_route_risk_profile_policy,
)
from ..domain.layered_route_validation import (
    empty_layered_route_validation_collection,
    normalize_layered_route_validation_collection,
)
from ..domain.layered_operational_adoption import (
    empty_layered_operational_adoptions, normalize_layered_operational_adoptions,
)
from ..domain.route_safety_evidence_v2 import (
    empty_route_safety_evidence_v2_collection,
    normalize_route_safety_evidence_v2_collection,
)
from ..domain.vertical_transition_validation import (
    empty_vertical_transition_validation_collection,
    normalize_vertical_transition_validation_collection,
)
from ..reference_data import (
    empty_equipment_reference_catalog, empty_reference_landing_sites, empty_reference_routes,
    normalize_equipment_reference_catalog,
)
from ..reference_data.landing_sites import backfill_reference_landing_sites
from ..reference_data.routes import backfill_reference_routes


SCHEMA_VERSION = 2
EXTENSION_ATTRIBUTES = (
    "buildings", "property_exposure", "infrastructure", "towers",
)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def assessment(status, reason):
    return {
        "status": status, "value": None, "unit": None,
        "threshold": None, "source": reason,
    }


def empty_extension_attribute(name):
    return {
        "status": "not_calculated", "source": None,
        "algorithm_id": None, "algorithm_version": None,
        "namespace": name, "grid_level": None, "count": 0, "covered_count": 0,
        "metadata": {}, "cells": {},
    }


def empty_grid_attributes():
    result = {
        "population": PopulationGridService.empty(),
        "terrain": TerrainGridService.empty(),
        "airspace": AirspaceGridService.empty(),
        "traffic": TrafficGridService.empty(),
        "conflict": ConflictGridService.empty(),
    }
    result.update({name: empty_extension_attribute(name) for name in EXTENSION_ATTRIBUTES})
    return result


def empty_catalog(catalog_id):
    return {"status": "not_calculated", "catalog_id": catalog_id, "source": None, "metadata": {}, "count": 0, "items": []}


def empty_collection(collection_id):
    return {"status": "not_calculated", "collection_id": collection_id, "source": None, "metadata": {}, "count": 0, "items": []}


def empty_safety_assessment():
    return {
        "status": "not_calculated",
        "source": None,
        "input_fingerprint": None,
        "results": [],
    }


def blank_project(defaults):
    risks = {
        "environment": assessment("not_calculated", "GRC 环境/航路规划风险接口"),
        "technical": assessment("pending_confirmation", "MTBF 技术失效风险接口"),
        "life": assessment("pending_confirmation", "生命风险模型、单位和阈值待确认"),
        "property": assessment("missing_data", "财产暴露数据未接入"),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": 0,
        "project": {
            "project_id": str(uuid4()), "name": "CNS 规划项目",
            "created_at": utc_now(), "updated_at": utc_now(), "revision": 0,
        },
        "workspace": None, "grid": None,
        "algorithm_selection": default_algorithm_selection(),
        "data_source_profiles": default_source_profiles(),
        "grid_attributes": empty_grid_attributes(),
        "grid_risk": RiskModelV1.empty(), "traffic_simulation": None,
        # Additive Risk Framework V2 (factor → ground/air_traffic/environment_obstacle
        # domains).  ``grid_risk`` (legacy Risk V1) stays authoritative for the
        # current RiskAwareRoutePlannerV2; ``grid_risk_v2`` ships with no confirmed
        # aggregation policy and therefore no production risk weight.
        "risk_policy_v2": default_risk_policy_v2(),
        "grid_risk_v2": empty_grid_risk_v2(),
        "spatial_3d": empty_spatial_3d(),
        # 工程默认高度层目录（ALT-060/080/100/150/200）的**一次性**初始化标记：目录首次被补建或
        # 被用户显式管理后置位，据此绝不把用户刻意清空的目录再次填满。
        ALTITUDE_LAYER_INITIALIZED_KEY: False,
        "coverage_3d": GeometricCoverage3DV1.empty(),
        "cns_service_capability": CNSServiceCapabilityV1.empty(),
        "operational_timing": empty_operational_timing(),
        "service_timeline": RouteServiceTimelineV1.empty(),
        "protection_envelope": TacticalProtectionEnvelopeV1.empty(),
        "route_vertical_profiles": RouteVerticalProfileV1.empty(),
        "encounter_3d_assessment": EncounterAssessment3DV1.empty(),
        "nodes": [], "node_seq": 0, "route_seq": 0,
        "retired_route_ids": [], "scenario_routes": [],
        "operational_routes": [], "aircraft": None, "rules": None,
        "reference_landing_sites": empty_reference_landing_sites(),
        "reference_routes": empty_reference_routes(),
        "reference_route_links": empty_reference_route_links(),
        "route_planning_experiments": empty_experiments(),
        "v3_planning_policy": normalize_v3_planning_policy(None),
        "v3_fine_refinement_policy": default_v3_fine_refinement_policy(),
        "v3_continuous_validation_policy": default_v3_validation_policy(),
        "route_planner_v3_experiments": empty_v3_experimental_session(),
        "v3_operational_adoptions": empty_v3_operational_adoptions(),
        "v3_cns_assessment_bundle": {
            "schema_version": "3.3-cns-assessment-bundle-collection",
            "status": "not_calculated",
            "count": 0,
            "items": [],
        },
        "airspace_policies": empty_airspace_policies(),
        "source_audits": empty_source_audits(),
        "reference_route_import_preview": None,
        "equipment_reference_catalog": empty_equipment_reference_catalog(),
        "aircraft_profiles": empty_catalog("aircraft-cns-profile-catalog"),
        "selected_aircraft_profile_id": None,
        "required_cns": pending_required_cns(),
        "cns_operation_context": empty_operation_context(),
        "cns_requirement_policies": empty_requirement_policies(),
        "required_cns_recommendation": empty_required_cns_recommendation(),
        "required_cns_adoption": empty_required_cns_adoption(),
        "device_catalog": empty_catalog("cns-device-catalog"),
        "existing_cns_facilities": empty_collection("existing-cns-facilities"),
        "candidate_sites": empty_collection("candidate-sites"),
        "cns_gap_analysis": CNSGapAnalyzerV1.empty(),
        "cns_gap_analysis_v2": CNSGapAnalyzerV2.empty(),
        "site_planning_policy": default_site_planning_policy(),
        "cns_site_plan": ReuseFirstSitePlannerV1.empty(),
        "closed_loop_assessment": empty_closed_loop_assessment(),
        "cns_corridor_policy": default_cns_corridor_policy(),
        "cns_corridor_assessment": empty_cns_corridor_assessment(),
        "cns_planning_objectives": default_cns_planning_objectives(),
        "cns_corridor_gap_assessment": empty_cns_corridor_gap_assessment(),
        "corridor_site_planning_policy": default_corridor_site_planning_policy(),
        "cns_corridor_site_plan": empty_cns_corridor_site_plan(),
        "cns_plan_review": empty_plan_review(),
        "confirmed_cns_plan": empty_confirmed_plan(),
        "cns_planning_reports": empty_report_collection(),
        "safety_policy": default_safety_policy(),
        "safety_assessment": empty_safety_assessment(),
        "building_clearance_policy": default_building_clearance_policy(),
        "building_clearance_assessment": empty_building_clearance_assessment(),
        # Layered route planning (Theta* V2 is the default layered_route_planner; V1 stays
        # the explicitly selectable legacy/baseline planner): explicit altitude
        # layer selection + explicit feasibility/cost policy + independent candidate
        # container.  No default clearance and no default lambda ships here.
        "layered_route_planning_request": default_layered_route_request(),
        "layered_route_feasibility_policy": default_layered_route_feasibility_policy(),
        "layered_route_cost_policy": default_layered_route_cost_policy(),
        "layered_route_candidates": empty_layered_route_candidate_collection(),
        # RouteRiskProfile V1 (additive analysis of a current layered candidate): per-domain
        # thresholds ship unconfirmed (no default), and the profile container starts empty.
        "route_risk_profile_policy": normalize_route_risk_profile_policy(None),
        "route_risk_profiles": default_route_risk_profile_collection(),
        # Production source-native validation and explicit operational adoption remain
        # independent from both the candidate and the V3 experiment containers.
        "layered_route_validations": empty_layered_route_validation_collection(),
        "layered_operational_adoptions": empty_layered_operational_adoptions(),
        # Route Safety Evidence V2 (additive post-planning evidence aggregation over the
        # published layered adoption lineage).  It ships empty: nothing is ever evaluated in
        # the background, and the container never feeds back into any upstream result.
        "route_safety_evidence_v2": empty_route_safety_evidence_v2_collection(),
        # Vertical Transition Continuous Validation V1 (climb/descent source-native geometry
        # of a current Production Route3DProfile).  It ships empty: no background evaluation
        # ever exists, and the container never feeds back into any upstream result.
        "vertical_transition_validations": (
            empty_vertical_transition_validation_collection()
        ),
        # Layered Risk-Aware Theta* V2 additive planning inputs.  The shelter coefficient is
        # the user-confirmed 1.0 baseline and lives as real per-grid data in
        # ``grid_attributes.population_shelter``; the two interfaces ship not configured.
        "shelter_coefficient_policy": user_defined_baseline_policy(),
        # 真实人口产品的 NoData 语义默认未确认：来源范围内的 NoData 保持 missing_data。
        "population_nodata_policy": default_population_nodata_policy(),
        "regulatory_constraints": default_regulatory_constraints(),
        "communication_planning_field": normalize_communication_planning_field(None),
        "theta_v2_objective_policy": default_theta_v2_objective_policy(),
        "max_route_risk_density": default_risk_density_constraint(),
        # BUG-ROUTE-005：规划用暴露度层默认**关闭**。开启需要显式工程确认的下限与陆地判据
        # 阈值；未确认时它绝不生效，也绝不改变人口报告 / NoData 语义。
        "planning_exposure_policy": default_planning_exposure_policy(),
        "devices": deepcopy(defaults.get("device_library", {}).get("items", [])),
        "coverage": None, "risks": risks,
        "result_statuses": {
            name: "not_calculated" for name in (
                "workspace", "grid", "environment_risk", "routes",
                "coverage", "cns_gap", "cns_gap_v2", "cns_site_plan",
                "grid_risk_v2",
                "closed_loop_assessment", "safety_assessment",
                "building_clearance",
                "cns_corridor_assessment",
                "cns_corridor_gap_assessment",
                "cns_corridor_site_plan",
                "cns_plan_review",
                "required_cns_recommendation",
                "coverage_3d",
                "cns_service_capability",
                "service_timeline", "protection_envelope", "route_vertical_profiles",
                "encounter_3d_assessment",
                "technical_risk", "report",
                "layered_route_candidate",
                "route_risk_profile",
                "layered_route_validation",
                "route_safety_evidence_v2",
                "route_3d_profiles",
                "vertical_transition_validation",
            )
        },
        "last_saved_at": None,
    }


def normalize_project(value, grid_service):
    """Validate the schema and backfill fields added without a schema bump."""
    if not isinstance(value, dict):
        raise ValueError("项目状态必须是 JSON 对象")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"不支持的项目 schema：{value.get('schema_version')}")
    if not isinstance(value.get("project"), dict):
        raise ValueError("项目状态缺少 project")
    value["revision"] = max(0, int(value.get("revision") or 0))
    value["project"]["revision"] = max(
        0, int(value["project"].get("revision") or value["revision"])
    )
    if isinstance(value.get("workspace"), dict):
        value["workspace"]["revision"] = max(
            0, int(value["workspace"].get("revision") or 0)
        )
    if "grid" not in value:
        workspace = value.get("workspace")
        value["grid"] = grid_service.generate(workspace["bbox"]) if workspace else None
    value["algorithm_selection"] = normalize_algorithm_selection(value.get("algorithm_selection"))
    profiles = value.setdefault("data_source_profiles", default_source_profiles())
    if not isinstance(profiles, dict):
        raise ValueError("data_source_profiles 格式无效")
    for name, profile in default_source_profiles().items():
        current = profiles.setdefault(name, profile)
        if not isinstance(current, dict):
            profiles[name] = profile
            continue
        for field, default in profile.items():
            current.setdefault(field, deepcopy(default))
    attributes = value.setdefault("grid_attributes", empty_grid_attributes())
    if not isinstance(attributes, dict):
        raise ValueError("grid_attributes 格式无效")
    for name, empty in empty_grid_attributes().items():
        attributes.setdefault(name, empty)
    attributes["population"] = PopulationGridService.backfill_legacy(attributes.get("population"))
    airspace = attributes.get("airspace")
    if isinstance(airspace, dict):
        airspace.setdefault("features", [])
        airspace.setdefault(
            "airspace_eligibility",
            empty_airspace_eligibility("missing_data", "旧项目未包含 confirmed allowed eligibility"),
        )
    value.setdefault("grid_risk", RiskModelV1.empty())
    # Additive Risk Framework V2 backfill: legacy schema-v2 projects get the
    # pending policy (no default weights) and an uncalculated V2 result.
    value["risk_policy_v2"] = normalize_risk_policy_v2(value.get("risk_policy_v2"))
    value["grid_risk_v2"] = normalize_grid_risk_v2(value.get("grid_risk_v2"))
    value.setdefault("traffic_simulation", None)
    value["spatial_3d"] = normalize_spatial_3d(value.get("spatial_3d"))
    # 旧项目没有目录初始化标记：保持 False，让工作区设置/项目恢复路径决定是否补建
    # 默认工程高度层（ALT-060/080/100/150/200）。这里只回填标记本身，绝不在此处写入高度层。
    value[ALTITUDE_LAYER_INITIALIZED_KEY] = bool(
        value.get(ALTITUDE_LAYER_INITIALIZED_KEY, False)
    )
    value.setdefault("coverage_3d", GeometricCoverage3DV1.empty())
    value.setdefault("cns_service_capability", CNSServiceCapabilityV1.empty())
    value["operational_timing"] = normalize_operational_timing(value.get("operational_timing"))
    value.setdefault("service_timeline", RouteServiceTimelineV1.empty())
    value.setdefault("protection_envelope", TacticalProtectionEnvelopeV1.empty())
    value["route_vertical_profiles"] = normalize_route_vertical_profiles(
        value.get("route_vertical_profiles")
    )
    value["encounter_3d_assessment"] = normalize_encounter_3d_assessment(
        value.get("encounter_3d_assessment")
    )
    value.setdefault("reference_landing_sites", empty_reference_landing_sites())
    value["reference_landing_sites"] = backfill_reference_landing_sites(
        value.get("reference_landing_sites") or empty_reference_landing_sites()
    )
    value["reference_routes"] = backfill_reference_routes(
        value.get("reference_routes") or empty_reference_routes()
    )
    value["reference_route_links"] = normalize_reference_route_links(
        value.get("reference_route_links")
    )
    value["route_planning_experiments"] = normalize_experiments(
        value.get("route_planning_experiments")
    )
    value["v3_planning_policy"] = normalize_v3_planning_policy(value.get("v3_planning_policy"))
    value["v3_fine_refinement_policy"] = normalize_v3_fine_refinement_policy(
        value.get("v3_fine_refinement_policy")
    )
    value["v3_continuous_validation_policy"] = normalize_v3_validation_policy(
        value.get("v3_continuous_validation_policy")
    )
    value["route_planner_v3_experiments"] = normalize_v3_experiments(
        value.get("route_planner_v3_experiments")
    )
    value["v3_operational_adoptions"] = normalize_v3_operational_adoptions(
        value.get("v3_operational_adoptions")
    )
    bundles = value.get("v3_cns_assessment_bundle") or {}
    if not isinstance(bundles, dict):
        bundles = {}
    value["v3_cns_assessment_bundle"] = {
        "schema_version": str(
            bundles.get("schema_version") or "3.3-cns-assessment-bundle-collection"
        ),
        "count": len([
            item for item in bundles.get("items") or [] if isinstance(item, dict)
        ]),
        "status": str(bundles.get("status") or ("passed" if bundles.get("items") else "not_calculated")),
        "items": [
            normalize_v3_cns_assessment_bundle(item)
            for item in bundles.get("items") or [] if isinstance(item, dict)
        ],
    }
    value["airspace_policies"] = normalize_airspace_policies(value.get("airspace_policies"))
    value["source_audits"] = normalize_source_audits(value.get("source_audits"))
    value.setdefault("reference_route_import_preview", None)
    equipment_reference = value.setdefault(
        "equipment_reference_catalog", empty_equipment_reference_catalog()
    )
    if equipment_reference.get("items"):
        value["equipment_reference_catalog"] = normalize_equipment_reference_catalog(
            equipment_reference
        )
    value.setdefault("aircraft_profiles", empty_catalog("aircraft-cns-profile-catalog"))
    value.setdefault("selected_aircraft_profile_id", None)
    value.setdefault("required_cns", pending_required_cns())
    value["cns_operation_context"] = normalize_operation_context(value.get("cns_operation_context"))
    value["cns_requirement_policies"] = normalize_requirement_policies(value.get("cns_requirement_policies"))
    value.setdefault("required_cns_recommendation", empty_required_cns_recommendation())
    value.setdefault("required_cns_adoption", empty_required_cns_adoption())
    value.setdefault("device_catalog", empty_catalog("cns-device-catalog"))
    value.setdefault("existing_cns_facilities", empty_collection("existing-cns-facilities"))
    value.setdefault("candidate_sites", empty_collection("candidate-sites"))
    value["existing_cns_facilities"]["items"] = [
        normalize_existing_facility(item, index)
        for index, item in enumerate(value["existing_cns_facilities"].get("items") or [])
    ]
    value["existing_cns_facilities"]["count"] = len(value["existing_cns_facilities"]["items"])
    value["candidate_sites"]["items"] = [
        normalize_candidate_site(item, index)
        for index, item in enumerate(value["candidate_sites"].get("items") or [])
    ]
    value["candidate_sites"]["count"] = len(value["candidate_sites"]["items"])
    value.setdefault("cns_gap_analysis", CNSGapAnalyzerV1.empty())
    value.setdefault("cns_gap_analysis_v2", CNSGapAnalyzerV2.empty())
    value["site_planning_policy"] = normalize_site_planning_policy(value.get("site_planning_policy"))
    value.setdefault("cns_site_plan", ReuseFirstSitePlannerV1.empty())
    value.setdefault("closed_loop_assessment", empty_closed_loop_assessment())
    value["cns_corridor_policy"] = normalize_cns_corridor_policy(value.get("cns_corridor_policy"))
    value.setdefault("cns_corridor_assessment", empty_cns_corridor_assessment())
    value["cns_planning_objectives"] = normalize_cns_planning_objectives(value.get("cns_planning_objectives"))
    value.setdefault("cns_corridor_gap_assessment", empty_cns_corridor_gap_assessment())
    value["corridor_site_planning_policy"] = normalize_corridor_site_planning_policy(value.get("corridor_site_planning_policy"))
    value.setdefault("cns_corridor_site_plan", empty_cns_corridor_site_plan())
    value.setdefault("cns_plan_review", empty_plan_review())
    value.setdefault("confirmed_cns_plan", empty_confirmed_plan())
    value.setdefault("cns_planning_reports", empty_report_collection())
    value["safety_policy"] = normalize_safety_policy(value.get("safety_policy"))
    value.setdefault("safety_assessment", empty_safety_assessment())
    value["building_clearance_policy"] = normalize_building_clearance_policy(
        value.get("building_clearance_policy")
    )
    value["building_clearance_assessment"] = normalize_building_clearance_assessment(
        value.get("building_clearance_assessment")
    )
    # Layered Risk-Aware Route Planner V1 additive backfill.  A legacy project gets the
    # pending request, the blocked feasibility policy (no default clearance), the pending
    # cost policy (no default lambda) and an empty candidate container.
    value["layered_route_planning_request"] = normalize_layered_route_request(
        value.get("layered_route_planning_request")
    )
    value["layered_route_feasibility_policy"] = normalize_layered_route_feasibility_policy(
        value.get("layered_route_feasibility_policy")
    )
    value["layered_route_cost_policy"] = normalize_layered_route_cost_policy(
        value.get("layered_route_cost_policy")
    )
    value["layered_route_candidates"] = normalize_layered_route_candidate_collection(
        value.get("layered_route_candidates")
    )
    # RouteRiskProfile V1 additive backfill: a legacy project gets the unconfirmed per-domain
    # thresholds (no default) and an empty profile container.
    value["route_risk_profile_policy"] = normalize_route_risk_profile_policy(
        value.get("route_risk_profile_policy")
    )
    value["route_risk_profiles"] = normalize_route_risk_profile_collection(
        value.get("route_risk_profiles")
    )
    value["layered_route_validations"] = normalize_layered_route_validation_collection(
        value.get("layered_route_validations")
    )
    value["layered_operational_adoptions"] = normalize_layered_operational_adoptions(
        value.get("layered_operational_adoptions")
    )
    # Route Safety Evidence V2 additive backfill: a legacy project gets an empty collection,
    # never a synthesized "current" assessment.
    value["route_safety_evidence_v2"] = normalize_route_safety_evidence_v2_collection(
        value.get("route_safety_evidence_v2")
    )
    # Vertical Transition Continuous Validation V1 additive backfill: a legacy project gets an
    # empty collection, never a synthesized "validated" transition.
    value["vertical_transition_validations"] = (
        normalize_vertical_transition_validation_collection(
            value.get("vertical_transition_validations")
        )
    )
    # Layered Risk-Aware Theta* V2 additive backfill.  A legacy project gets the explicit
    # user-confirmed shelter_coefficient = 1.0 baseline, the confirmed 0.8/0.1/0.1 objective,
    # the deliberately wide temporary max_route_risk_density = 1.0 evaluation constraint, and
    # the two additive interfaces in their ``not_configured`` state.  The per-grid
    # ``population_shelter`` field itself is *derived* from the canonical population factor
    # plus this policy on demand, so it is deliberately not written into ``grid_attributes``.
    value["shelter_coefficient_policy"] = normalize_shelter_coefficient_policy(
        value.get("shelter_coefficient_policy") or user_defined_baseline_policy()
    )
    # 显式的人口来源 NoData 语义确认：缺失时回到 not_configured（绝不视为已确认）。
    value["population_nodata_policy"] = normalize_population_nodata_policy(
        value.get("population_nodata_policy")
    )
    value["regulatory_constraints"] = normalize_regulatory_constraints(
        value.get("regulatory_constraints")
    )
    value["max_route_risk_density"] = normalize_risk_density_constraint(
        value.get("max_route_risk_density")
    )
    value["communication_planning_field"] = normalize_communication_planning_field(
        value.get("communication_planning_field")
    )
    value["theta_v2_objective_policy"] = normalize_theta_v2_objective_policy(
        value.get("theta_v2_objective_policy")
    )
    value["max_route_risk_density"] = normalize_risk_density_constraint(
        value.get("max_route_risk_density")
    )
    # BUG-ROUTE-005：规划用暴露度层是 additive 的**规划专用**策略，旧项目回填时保持未配置。
    value["planning_exposure_policy"] = normalize_planning_exposure_policy(
        value.get("planning_exposure_policy") or default_planning_exposure_policy()
    )
    value.setdefault("result_statuses", {}).setdefault("layered_route_candidate", "not_calculated")
    value.setdefault("result_statuses", {}).setdefault("route_risk_profile", "not_calculated")
    value.setdefault("result_statuses", {}).setdefault(
        "layered_route_validation", "not_calculated"
    )
    value.setdefault("result_statuses", {}).setdefault(
        "route_safety_evidence_v2", "not_calculated"
    )
    value.setdefault("result_statuses", {}).setdefault(
        "vertical_transition_validation", "not_calculated"
    )
    # Production Route3DProfile V1 ships inside ``spatial_3d`` (additive, backfilled empty for
    # a legacy project by ``normalize_spatial_3d``); only its result status is registered here.
    value.setdefault("result_statuses", {}).setdefault("route_3d_profiles", "not_calculated")
    value.setdefault("result_statuses", {}).setdefault("cns_gap", "not_calculated")
    value.setdefault("result_statuses", {}).setdefault("grid_risk_v2", "not_calculated")
    value.setdefault("result_statuses", {}).setdefault("cns_gap_v2", "not_calculated")
    value.setdefault("result_statuses", {}).setdefault("cns_site_plan", "not_calculated")
    value.setdefault("result_statuses", {}).setdefault("closed_loop_assessment", "not_calculated")
    value.setdefault("result_statuses", {}).setdefault("cns_corridor_assessment", "not_calculated")
    value.setdefault("result_statuses", {}).setdefault("cns_corridor_gap_assessment", "not_calculated")
    value.setdefault("result_statuses", {}).setdefault("cns_corridor_site_plan", "not_calculated")
    value.setdefault("result_statuses", {}).setdefault("cns_plan_review", "not_calculated")
    value.setdefault("result_statuses", {}).setdefault("required_cns_recommendation", "not_calculated")
    value.setdefault("result_statuses", {}).setdefault("safety_assessment", "not_calculated")
    value.setdefault("result_statuses", {}).setdefault("building_clearance", "not_calculated")
    value.setdefault("result_statuses", {}).setdefault("coverage_3d", "not_calculated")
    value.setdefault("result_statuses", {}).setdefault("cns_service_capability", "not_calculated")
    value.setdefault("result_statuses", {}).setdefault("service_timeline", "not_calculated")
    value.setdefault("result_statuses", {}).setdefault("protection_envelope", "not_calculated")
    value.setdefault("result_statuses", {}).setdefault("route_vertical_profiles", "not_calculated")
    value.setdefault("result_statuses", {}).setdefault("encounter_3d_assessment", "not_calculated")
    value.setdefault("result_statuses", {}).setdefault("report", "not_calculated")
    value.setdefault("result_statuses", {}).setdefault(
        "grid", "passed" if value.get("grid") else "not_calculated"
    )
    return value
