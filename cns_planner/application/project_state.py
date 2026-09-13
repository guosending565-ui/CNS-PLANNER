"""Authoritative schema-v2 project-state construction and compatibility loading."""

from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

from ..risk.v1 import RiskModelV1
from ..data.mapping.airspace import AirspaceGridService
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
from ..algorithms.service_capability.v1 import CNSServiceCapabilityV1
from ..algorithms.timeline.v1 import RouteServiceTimelineV1
from ..algorithms.protection.v1 import TacticalProtectionEnvelopeV1
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
        "namespace": name, "grid_level": None, "count": 0, "cells": {},
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
        "project": {
            "project_id": str(uuid4()), "name": "CNS 规划项目",
            "created_at": utc_now(), "updated_at": utc_now(),
        },
        "workspace": None, "grid": None,
        "algorithm_selection": default_algorithm_selection(),
        "data_source_profiles": default_source_profiles(),
        "grid_attributes": empty_grid_attributes(),
        "grid_risk": RiskModelV1.empty(), "traffic_simulation": None,
        "spatial_3d": empty_spatial_3d(),
        "coverage_3d": GeometricCoverage3DV1.empty(),
        "cns_service_capability": CNSServiceCapabilityV1.empty(),
        "operational_timing": empty_operational_timing(),
        "service_timeline": RouteServiceTimelineV1.empty(),
        "protection_envelope": TacticalProtectionEnvelopeV1.empty(),
        "nodes": [], "node_seq": 0, "route_seq": 0,
        "retired_route_ids": [], "scenario_routes": [],
        "operational_routes": [], "aircraft": None, "rules": None,
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
        "safety_policy": default_safety_policy(),
        "safety_assessment": empty_safety_assessment(),
        "devices": deepcopy(defaults.get("device_library", {}).get("items", [])),
        "coverage": None, "risks": risks,
        "result_statuses": {
            name: "not_calculated" for name in (
                "workspace", "grid", "environment_risk", "routes",
                "coverage", "cns_gap", "cns_gap_v2", "cns_site_plan",
                "closed_loop_assessment", "safety_assessment",
                "cns_corridor_assessment",
                "cns_corridor_gap_assessment",
                "cns_corridor_site_plan",
                "cns_plan_review",
                "required_cns_recommendation",
                "coverage_3d",
                "cns_service_capability",
                "service_timeline", "protection_envelope",
                "technical_risk", "report",
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
    value.setdefault("grid_risk", RiskModelV1.empty())
    value.setdefault("traffic_simulation", None)
    value["spatial_3d"] = normalize_spatial_3d(value.get("spatial_3d"))
    value.setdefault("coverage_3d", GeometricCoverage3DV1.empty())
    value.setdefault("cns_service_capability", CNSServiceCapabilityV1.empty())
    value["operational_timing"] = normalize_operational_timing(value.get("operational_timing"))
    value.setdefault("service_timeline", RouteServiceTimelineV1.empty())
    value.setdefault("protection_envelope", TacticalProtectionEnvelopeV1.empty())
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
    value["safety_policy"] = normalize_safety_policy(value.get("safety_policy"))
    value.setdefault("safety_assessment", empty_safety_assessment())
    value.setdefault("result_statuses", {}).setdefault("cns_gap", "not_calculated")
    value.setdefault("result_statuses", {}).setdefault("cns_gap_v2", "not_calculated")
    value.setdefault("result_statuses", {}).setdefault("cns_site_plan", "not_calculated")
    value.setdefault("result_statuses", {}).setdefault("closed_loop_assessment", "not_calculated")
    value.setdefault("result_statuses", {}).setdefault("cns_corridor_assessment", "not_calculated")
    value.setdefault("result_statuses", {}).setdefault("cns_corridor_gap_assessment", "not_calculated")
    value.setdefault("result_statuses", {}).setdefault("cns_corridor_site_plan", "not_calculated")
    value.setdefault("result_statuses", {}).setdefault("cns_plan_review", "not_calculated")
    value.setdefault("result_statuses", {}).setdefault("required_cns_recommendation", "not_calculated")
    value.setdefault("result_statuses", {}).setdefault("safety_assessment", "not_calculated")
    value.setdefault("result_statuses", {}).setdefault("coverage_3d", "not_calculated")
    value.setdefault("result_statuses", {}).setdefault("cns_service_capability", "not_calculated")
    value.setdefault("result_statuses", {}).setdefault("service_timeline", "not_calculated")
    value.setdefault("result_statuses", {}).setdefault("protection_envelope", "not_calculated")
    value.setdefault("result_statuses", {}).setdefault(
        "grid", "passed" if value.get("grid") else "not_calculated"
    )
    return value
