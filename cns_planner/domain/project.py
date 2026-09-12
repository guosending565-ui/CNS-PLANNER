"""Typed schema-v2 project-state contract used at application boundaries."""

from typing import Any, TypedDict


class ProjectMetadata(TypedDict):
    project_id: str
    name: str
    created_at: str
    updated_at: str


class ProjectState(TypedDict, total=False):
    schema_version: int
    project: ProjectMetadata
    workspace: dict[str, Any] | None
    grid: dict[str, Any] | None
    algorithm_selection: dict[str, dict[str, Any]]
    data_source_profiles: dict[str, dict[str, Any]]
    grid_attributes: dict[str, dict[str, Any]]
    grid_risk: dict[str, Any]
    spatial_3d: dict[str, Any]
    coverage_3d: dict[str, Any]
    cns_service_capability: dict[str, Any]
    operational_timing: dict[str, Any]
    service_timeline: dict[str, Any]
    protection_envelope: dict[str, Any]
    nodes: list[dict[str, Any]]
    scenario_routes: list[dict[str, Any]]
    operational_routes: list[dict[str, Any]]
    aircraft: dict[str, Any] | None
    aircraft_profiles: dict[str, Any]
    selected_aircraft_profile_id: str | None
    required_cns: dict[str, Any]
    rules: dict[str, Any] | None
    device_catalog: dict[str, Any]
    devices: list[dict[str, Any]]
    existing_cns_facilities: dict[str, Any]
    candidate_sites: dict[str, Any]
    cns_gap_analysis: dict[str, Any]
    cns_gap_analysis_v2: dict[str, Any]
    site_planning_policy: dict[str, Any]
    cns_site_plan: dict[str, Any]
    closed_loop_assessment: dict[str, Any]
    safety_policy: dict[str, Any]
    safety_assessment: dict[str, Any]
    coverage: dict[str, Any] | None
    risks: dict[str, dict[str, Any]]
    result_statuses: dict[str, str]
