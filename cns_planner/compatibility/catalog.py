"""Machine-readable lifecycle catalog for B7 compatibility and research code."""

from __future__ import annotations

from copy import deepcopy


_CAPABILITIES = (
    {
        "capability_id": "RoutePlannerV1", "algorithm_id": "route_planner_v1",
        "lifecycle": "compatibility", "deprecated": True,
        "read_existing": True, "allow_runtime_compute": True,
        "persistent_write": False, "production_authority": False,
        "replacement": "LayeredRiskAwareThetaStarV2",
        "namespace": "/api/compatibility/route-planner",
        "delete_gate_id": "B7-DELETE-ROUTE-PLANNER-V1",
    },
    {
        "capability_id": "RiskAwareRoutePlannerV2", "algorithm_id": "risk_aware_route_planner_v2",
        "lifecycle": "compatibility", "deprecated": True,
        "read_existing": True, "allow_runtime_compute": True,
        "persistent_write": False, "production_authority": False,
        "replacement": "LayeredRiskAwareThetaStarV2",
        "namespace": "/api/compatibility/route-planner",
        "delete_gate_id": "B7-DELETE-RISK-AWARE-ROUTE-PLANNER-V2",
    },
    {
        "capability_id": "LayeredRoutePlannerV1", "algorithm_id": "layered_route_planner_v1",
        "lifecycle": "compatibility", "deprecated": True,
        "read_existing": True, "allow_runtime_compute": True,
        "persistent_write": False, "production_authority": False,
        "replacement": "LayeredRiskAwareThetaStarV2",
        "namespace": "/api/compatibility/layered-route-planner",
        "delete_gate_id": "B7-DELETE-LAYERED-ROUTE-PLANNER-V1",
    },
    {
        "capability_id": "CoveragePlannerV1", "algorithm_id": "coverage_planner_v1",
        "lifecycle": "compatibility", "deprecated": True,
        "read_existing": True, "allow_runtime_compute": True,
        "persistent_write": False, "production_authority": False,
        "replacement": "GeometricCoverage3D",
        "namespace": "/api/compatibility/coverage",
        "delete_gate_id": "B7-DELETE-COVERAGE-PLANNER-V1",
    },
    {
        "capability_id": "CNSGapAnalyzerV1", "algorithm_id": "cns_gap_analysis_v1",
        "lifecycle": "compatibility", "deprecated": True,
        "read_existing": True, "allow_runtime_compute": True,
        "persistent_write": False, "production_authority": False,
        "replacement": "CNSCorridorGapAnalyzerV1",
        "namespace": "/api/compatibility/cns-gap-analysis-v1",
        "delete_gate_id": "B7-DELETE-CNS-GAP-ANALYZER-V1",
    },
    {
        "capability_id": "CNSGapAnalyzerV2", "algorithm_id": "cns_gap_analysis_v2",
        "lifecycle": "advanced", "deprecated": False,
        "read_existing": True, "allow_runtime_compute": True,
        "persistent_write": False, "production_authority": False,
        "replacement": "CNSCorridorGapAnalyzerV1",
        "namespace": "/api/compatibility/cns-gap-analysis-v2",
        "delete_gate_id": "B7-ARCHIVE-CNS-GAP-ANALYZER-V2",
    },
    {
        "capability_id": "ReuseFirstSitePlannerV1", "algorithm_id": "reuse_first_site_planner_v1",
        "lifecycle": "compatibility", "deprecated": True,
        "read_existing": True, "allow_runtime_compute": True,
        "persistent_write": False, "production_authority": False,
        "replacement": "CorridorReuseFirstSitePlannerV2",
        "namespace": "/api/compatibility/site-plan",
        "delete_gate_id": "B7-DELETE-REUSE-FIRST-SITE-PLANNER-V1",
    },
    {
        "capability_id": "RoutePlannerV3", "algorithm_id": "route_planner_v3_a_b_c_d",
        "lifecycle": "research", "deprecated": False,
        "read_existing": True, "allow_runtime_compute": True,
        "persistent_write": False, "production_authority": False,
        "replacement": "LayeredRiskAwareThetaStarV2",
        "namespace": "/api/research/route-planner-v3",
        "delete_gate_id": "B7-DELETE-ROUTE-PLANNER-V3",
    },
)

_BY_ID = {item["capability_id"]: item for item in _CAPABILITIES}

#: compatibility selection 类型 → 该类型默认（冻结基线）对应的 capability_id。
#: 路由只把它用作响应 metadata 的默认能力标识。
CAPABILITY_FOR_SELECTION = {
    "route_planner": "RoutePlannerV1",
    "coverage_planner": "CoveragePlannerV1",
    "cns_gap_analyzer": "CNSGapAnalyzerV1",
    "site_planner": "ReuseFirstSitePlannerV1",
}

#: 显式指定 algorithm_id 时更精确的 capability 归属（同一 algorithm_type 下有多个实现）。
CAPABILITY_FOR_SELECTION_ALGORITHM = {
    "route_planner_v1": "RoutePlannerV1",
    "risk_aware_route_planner_v2": "RiskAwareRoutePlannerV2",
    "layered_route_planner_v1": "LayeredRoutePlannerV1",
    "coverage_planner_v1": "CoveragePlannerV1",
    "cns_gap_analysis_v1": "CNSGapAnalyzerV1",
    "cns_gap_analysis_v2": "CNSGapAnalyzerV2",
    "reuse_first_site_planner_v1": "ReuseFirstSitePlannerV1",
}


def capability_catalog():
    """Return a detached, stable audit view; never a workflow input."""

    return {
        "schema_version": "phase4-b7x-compatibility-catalog-v1",
        "read_only": True,
        "production_workflow_input": False,
        "items": deepcopy(list(_CAPABILITIES)),
    }


def capability_metadata(capability_id, *, alias=False):
    item = deepcopy(_BY_ID[str(capability_id)])
    return {
        "capability_id": item["capability_id"],
        "lifecycle": item["lifecycle"],
        "compatibility": item["lifecycle"] == "compatibility" or bool(alias),
        "research": item["lifecycle"] == "research",
        "deprecated": bool(item["deprecated"] or alias),
        "authoritative": False,
        "read_existing": item["read_existing"],
        "allow_runtime_compute": item["allow_runtime_compute"],
        "persistent_write": False,
        "production_authority": False,
        "replacement": item["replacement"],
        "namespace": item["namespace"],
        "delete_gate_id": item["delete_gate_id"],
    }


def with_capability_metadata(payload, capability_id, *, alias=False):
    result = deepcopy(payload) if isinstance(payload, dict) else {"result": deepcopy(payload)}
    result.update(capability_metadata(capability_id, alias=alias))
    return result
