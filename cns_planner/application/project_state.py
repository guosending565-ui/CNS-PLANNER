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
from ..domain.cns_inputs import pending_required_cns
from ..gap.v1 import CNSGapAnalyzerV1


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
        "nodes": [], "node_seq": 0, "route_seq": 0,
        "retired_route_ids": [], "scenario_routes": [],
        "operational_routes": [], "aircraft": None, "rules": None,
        "aircraft_profiles": empty_catalog("aircraft-cns-profile-catalog"),
        "selected_aircraft_profile_id": None,
        "required_cns": pending_required_cns(),
        "device_catalog": empty_catalog("cns-device-catalog"),
        "existing_cns_facilities": empty_collection("existing-cns-facilities"),
        "candidate_sites": empty_collection("candidate-sites"),
        "cns_gap_analysis": CNSGapAnalyzerV1.empty(),
        "devices": deepcopy(defaults.get("device_library", {}).get("items", [])),
        "coverage": None, "risks": risks,
        "result_statuses": {
            name: "not_calculated" for name in (
                "workspace", "grid", "environment_risk", "routes",
                "coverage", "cns_gap", "technical_risk", "report",
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
    value.setdefault("aircraft_profiles", empty_catalog("aircraft-cns-profile-catalog"))
    value.setdefault("selected_aircraft_profile_id", None)
    value.setdefault("required_cns", pending_required_cns())
    value.setdefault("device_catalog", empty_catalog("cns-device-catalog"))
    value.setdefault("existing_cns_facilities", empty_collection("existing-cns-facilities"))
    value.setdefault("candidate_sites", empty_collection("candidate-sites"))
    value.setdefault("cns_gap_analysis", CNSGapAnalyzerV1.empty())
    value.setdefault("result_statuses", {}).setdefault("cns_gap", "not_calculated")
    value.setdefault("result_statuses", {}).setdefault(
        "grid", "passed" if value.get("grid") else "not_calculated"
    )
    return value
