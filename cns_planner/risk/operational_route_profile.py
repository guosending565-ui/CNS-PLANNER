"""既有 operational route 的独立风险画像（无 candidate、不伪造搜索成本）。

契约目标（R7-D4）
-----------------
``operational_routes`` 里的既有航路没有 current ``LayeredRouteCandidate``，因此不能走
``RouteRiskProfiler`` 的 candidate identity gate（它要求 ``status=candidate``、
``current_applicability=current``、``grid_path`` 与 ``distance_m``/``cost_breakdown``）。
本模块对**真实原始折线**做同一套 domain/factor 暴露积分，但把一致性判据换成：

* ``route_length_m`` 必须等于原始折线在显式投影下的长度（不是历史搜索距离）；
* 每个采样点必须落在当前 MH/T 标准网格内；
* 只使用 current ``grid_risk_v2`` 的 cell domain index，缺失即 unresolved（绝不补 0）；
* 不读取、不构造、不推断 candidate / 历史 cost_breakdown / Theta* 搜索成本。

数学与 planner 同源：``exposure = Σ(length_m × mean_index)``、``mean_index =
(left_index + right_index) / 2``、缺失 index 的段只累加 unresolved 长度。区别只在于点列
来自原始折线的等距重采样（端点 connector 不适用），不是 cell 中心。
"""

from __future__ import annotations

from copy import deepcopy
from math import ceil, hypot

from ..domain.risk_v2 import DOMAIN_IDS
from ..domain.route_risk_profile import (
    ALGORITHM_ID,
    ALGORITHM_VERSION,
    SCHEMA_VERSION,
    domain_policy_fingerprints,
    factor_source_fingerprints,
    grid_risk_v2_cells_fingerprint,
    normalize_route_risk_profile_policy,
    path_fingerprint,
    profile_fingerprint,
    route_risk_profile_policy_fingerprint,
)
from ..planning.grid_graph import GridGraph
from .route_exposure import resolve_cell_domain_indices
from .route_profile import (
    NUMERICAL_TOLERANCE,
    _classification_view,
    _domain_profiles,
    _factor_profiles,
    _high_risk_view,
    _route_risk_segment,
)

CONTRACT_VERSION = "operational-route-risk-profile-v1"
ARTIFACT_TYPE = "operational_route_risk_profile"
SAMPLING_VERSION = "uniform_polyline_resampling_v1"
CONNECTOR_SEMANTICS = "operational_polyline_sampled_segments_no_cell_center_connector"
DEFAULT_SAMPLING_SPACING_M = 20.0

SEMANTICS = {
    "subject": "existing_operational_route_geometry",
    "candidate_required": False,
    "search_cost_used": False,
    "replanning": False,
    "writes_operational_routes_or_cns": False,
    "relative_engineering_index": True,
    "not_accident_probability": True,
    "not_sora_grc": True,
    "not_sora_arc": True,
    "not_absolute_safety_risk": True,
    "missing_is_not_zero": True,
    "unresolved_length_preserved": True,
    "endpoint_connector_not_used": True,
    "operational_authorization": False,
}


def _round(value):
    if value is None:
        return None
    try:
        return round(float(value), 9)
    except (TypeError, ValueError):
        return None


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def geometry_length_metric(metric_path):
    """原始折线的投影平面长度（EPSG:32651 米），与 R7-D2 的 9404.409 m 同口径。"""

    points = [list(point) for point in metric_path or []]
    return sum(hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(points, points[1:]))


def sample_metric_polyline(metric_path, spacing_m):
    """按固定间距重采样原始折线；返回 ``(metric_points, segment_index_per_point)``。"""

    points = [list(point) for point in metric_path or []]
    if len(points) < 2:
        raise ValueError("原始折线至少需要两个顶点")
    spacing = float(spacing_m)
    if not (spacing > 0.0):
        raise ValueError("采样间距必须为正数")
    sampled = []
    segment_index = []
    for index, (a, b) in enumerate(zip(points, points[1:])):
        length = hypot(b[0] - a[0], b[1] - a[1])
        count = max(1, int(ceil(length / spacing)))
        for step in range(count):
            ratio = step / count
            sampled.append([a[0] + (b[0] - a[0]) * ratio, a[1] + (b[1] - a[1]) * ratio])
            segment_index.append(index)
    sampled.append(list(points[-1]))
    segment_index.append(len(points) - 2)
    return sampled, segment_index


def _sample_points_geographic(sampled_metric, to_geographic):
    return [list(to_geographic(point)) for point in sampled_metric]


def _segment_record(*, index, length_m, start, end, start_distance, end_distance,
                    start_cell, end_cell, cell_count, domain_indices, domains):
    record = {
        "index": index,
        "length_m": float(length_m),
        "start_cumulative_distance_m": start_distance,
        "end_cumulative_distance_m": end_distance,
        "start_coordinate": list(start),
        "end_coordinate": list(end),
        "start_index_grid_id": start_cell,
        "end_index_grid_id": end_cell,
        "from_grid_id": start_cell if start_cell else None,
        "to_grid_id": end_cell if end_cell else None,
        "from_grid_role": "operational_route_vertex" if index == 0 else "grid_cell",
        "to_grid_role": (
            "operational_route_vertex" if index == cell_count - 2 else "grid_cell"
        ),
        "connector": CONNECTOR_SEMANTICS,
        "domains": domains,
    }
    return record


def assess_operational_route_risk(
    *,
    route,
    metric_path,
    altitude_layer_id,
    grid,
    grid_risk_v2,
    profile_policy,
    to_geographic,
    sampling_spacing_m=DEFAULT_SAMPLING_SPACING_M,
):
    """对既有航路真实几何做独立风险画像（只读、无 candidate）。

    返回独立 evidence dict，含 ``status`` / ``profile`` / ``identity`` / ``blocking_reasons``。
    任何输入缺失都 fail-closed 为 ``unknown`` 或 ``not_available``，绝不生成 candidate 身份。
    """

    path = [list(value) for value in (route or {}).get("path") or []]
    metric = [list(value) for value in metric_path or []]
    policy = normalize_route_risk_profile_policy(profile_policy)
    base = {
        "contract": CONTRACT_VERSION,
        "artifact_type": ARTIFACT_TYPE,
        "subject": {
            "kind": "operational_route",
            "route_id": (route or {}).get("route_id"),
            "altitude_layer_id": altitude_layer_id,
            "geometry_sha256": None,
            "path": path,
            "vertex_count": len(path),
            "provenance": deepcopy((route or {}).get("provenance")),
        },
        "altitude_layer_id": altitude_layer_id,
        "policy": policy,
        "profile": None,
        "identity": {},
        "blocking_reasons": [],
        "research_only": True,
        "operational_authorization": False,
        "formal_admission_evaluated": False,
        "writes_state": False,
        "semantics": deepcopy(SEMANTICS),
    }

    def reject(status, reason_code, reason):
        base["status"] = status
        base["status_reason"] = reason_code
        base["blocking_reasons"] = [{"reason_code": reason_code, "reason": reason}]
        return base

    if len(path) < 2 or len(metric) != len(path):
        return reject(
            "inconsistent_evidence", "route_geometry_missing",
            "既有航路缺少 path 或 metric_path 与顶点数不一致",
        )
    if to_geographic is None:
        return reject(
            "not_available", "geographic_transform_missing",
            "缺少显式 to_geographic 适配器：无法把原始折线映射到经纬度网格",
        )
    grid_cells = (grid or {}).get("cells") if isinstance(grid, dict) else None
    if not grid_cells:
        return reject(
            "not_available", "grid_cells_missing",
            "当前项目没有可用的 MH/T 标准网格 cells",
        )
    try:
        graph = GridGraph(list(grid_cells))
    except (TypeError, ValueError) as exc:
        return reject("not_available", "grid_unavailable", f"标准网格不可用：{exc}")

    sampled_metric, sampled_segments = sample_metric_polyline(metric, sampling_spacing_m)
    sampled_geographic = _sample_points_geographic(sampled_metric, to_geographic)
    cells = [graph.containing_cell(point) for point in sampled_geographic]
    outside = [index for index, cell in enumerate(cells) if not cell]
    if outside:
        return reject(
            "inconsistent_evidence", "route_point_outside_grid",
            f"原始折线有 {len(outside)} 个采样点不在当前标准网格内：{outside[:5]}",
        )

    unique_cells = sorted(set(cells))
    indices, unresolved = resolve_cell_domain_indices(grid_risk_v2, unique_cells, DOMAIN_IDS)
    risk_cells = (grid_risk_v2 or {}).get("cells") or {}
    risk_policy = ((grid_risk_v2 or {}).get("policy") or {})
    risk_domain_policies = (
        risk_policy.get("domains") if isinstance(risk_policy.get("domains"), dict) else {}
    )

    point_domain_indices = [
        {domain_id: (indices.get(cell) or {}).get(domain_id) for domain_id in DOMAIN_IDS}
        for cell in cells
    ]
    segments = []
    exposures = {domain_id: 0.0 for domain_id in DOMAIN_IDS}
    cumulative = 0.0
    integral_segments = []
    for index, (left, right) in enumerate(zip(sampled_geographic, sampled_geographic[1:])):
        length = hypot(
            sampled_metric[index + 1][0] - sampled_metric[index][0],
            sampled_metric[index + 1][1] - sampled_metric[index][1],
        )
        start_distance = cumulative
        cumulative += length
        domain_record = {}
        for domain_id in DOMAIN_IDS:
            left_index = point_domain_indices[index][domain_id]
            right_index = point_domain_indices[index + 1][domain_id]
            resolved = _finite(left_index) and _finite(right_index)
            mean = (float(left_index) + float(right_index)) / 2.0 if resolved else None
            contribution = length * mean if mean is not None else None
            if contribution is not None:
                exposures[domain_id] += contribution
            domain_record[domain_id] = {
                "start_index": left_index,
                "end_index": right_index,
                "mean_index": mean,
                "exposure_index_m": contribution,
                "resolved": bool(resolved),
            }
        segments.append(_segment_record(
            index=index, length_m=length,
            start=sampled_geographic[index], end=sampled_geographic[index + 1],
            start_distance=start_distance, end_distance=cumulative,
            start_cell=cells[index], end_cell=cells[index + 1],
            cell_count=len(cells), domain_indices=point_domain_indices[index],
            domains=domain_record,
        ))
        integral_segments.append(segments[-1])

    integral = {
        "distance_m": cumulative,
        "domain_exposure_index_m": exposures,
        "segments": integral_segments,
        "points": sampled_geographic,
        "point_domain_indices": point_domain_indices,
        "index_source_grid_ids": cells,
        "connector_semantics": {
            "connector": CONNECTOR_SEMANTICS,
            "segment_index": "(left_index + right_index) / 2",
            "exposure": "sum_over_segments(length_m * mean_index)",
            "missing_index_is_never_zero": True,
        },
    }
    original_length = geometry_length_metric(metric)
    length_mismatch = abs(cumulative - original_length) > max(
        NUMERICAL_TOLERANCE, 1e-9 * abs(original_length)
    )
    vertex_points = [list(to_geographic(point)) for point in metric]
    vertices_mapped = all(graph.containing_cell(point) for point in vertex_points)

    checks = [
        {
            "check_id": "integral_length_matches_original_polyline",
            "status": "failed" if length_mismatch else "passed",
            "reason_code": "geometry_length_mismatch" if length_mismatch else None,
            "reason": (
                f"积分长度 {_round(cumulative)} m 与原始折线 {_round(original_length)} m 不一致"
                if length_mismatch else None
            ),
            "profile_value": _round(cumulative),
            "source_value": _round(original_length),
        },
        {
            "check_id": "all_original_vertices_inside_grid",
            "status": "passed" if vertices_mapped else "failed",
            "reason_code": None if vertices_mapped else "route_vertex_outside_grid",
            "reason": None if vertices_mapped else "原始顶点不在当前标准网格内",
            "profile_value": len(vertex_points),
            "source_value": len(vertex_points),
        },
        {
            "check_id": "sampled_segments_cover_original_polyline",
            "status": "passed" if len(segments) >= len(metric) - 1 else "failed",
            "reason_code": None,
            "reason": None,
            "profile_value": len(segments),
            "source_value": len(metric) - 1,
        },
    ]
    consistency_status = "passed" if all(
        check["status"] == "passed" for check in checks
    ) else "inconsistent"

    fingerprints = _fingerprints(
        route=route, path=path, grid=grid, grid_risk_v2=grid_risk_v2,
        policy=policy, cells=cells, metric_length=original_length,
        sampling_spacing_m=float(sampling_spacing_m),
    )
    profile_id = f"ORP-{(route or {}).get('route_id') or 'route'}-{fingerprints['profile_fingerprint'].rsplit('-', 1)[-1][:16]}"

    active_domains = ()
    domain_profiles = _domain_profiles(
        domain_ids=DOMAIN_IDS, segments=segments, integral=integral, indices=indices,
        unresolved=unresolved, policy=policy, active_domains=active_domains,
        route_length=cumulative, grid_path=cells,
    )
    factor_profiles, contributors = _factor_profiles(
        segments=segments, grid_risk_v2_cells=risk_cells,
        risk_domain_policies=risk_domain_policies, route_length=cumulative,
    )
    for domain_id, domain_profile in domain_profiles.items():
        domain_profile["contributors"] = {
            factor_id: {
                "factor_id": factor_id,
                "normalized_exposure_index_m": factor["normalized_exposure_index_m"],
                "weighted_contribution_index_m": factor["weighted_contribution_index_m"],
                "weight": factor["weight"],
                "contributor_rank": factor["contributor_rank"],
                "contribution_status": factor["contribution_status"],
                "contributor_semantics": "relative_engineering_contribution_not_accident_cause_probability",
            }
            for factor_id, factor in factor_profiles.items()
            if factor["domain"] == domain_id
        }

    profile = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": ARTIFACT_TYPE,
        "profile_id": profile_id,
        "status": "not_calculated",
        "subject": deepcopy(base["subject"]),
        "altitude_layer_id": altitude_layer_id,
        "policy": deepcopy(policy),
        "route_length_m": _round(cumulative),
        "geometry": {
            "vertex_count": len(metric),
            "segment_count": len(metric) - 1,
            "original_length_m": _round(original_length),
            "sampled_point_count": len(sampled_geographic),
            "sampling_spacing_m": float(sampling_spacing_m),
            "sampling_version": SAMPLING_VERSION,
            "distance_basis": "explicit_metric_crs_of_source_adapter",
            "unique_cell_count": len(unique_cells),
        },
        "domains": domain_profiles,
        "factors": factor_profiles,
        "contributors": contributors,
        "classification": _classification_view(domain_profiles),
        "high_risk": _high_risk_view(domain_profiles),
        "segments": [
            _route_risk_segment(
                segment, profile_id=profile_id, policy=policy,
                grid_risk_v2_cells=risk_cells,
                risk_domain_policies=risk_domain_policies,
                connector_semantics=CONNECTOR_SEMANTICS,
            )
            for segment in segments
        ],
        "connector_semantics": deepcopy(integral["connector_semantics"]),
        "fingerprints": fingerprints,
        "consistency": {
            "status": consistency_status,
            "tolerance": NUMERICAL_TOLERANCE,
            "checks": checks,
            "semantics": (
                "operational_profile_length_must_equal_original_polyline_length_"
                "and_all_vertices_must_map_to_current_grid"
            ),
        },
        "provenance": {
            "pipeline": (
                "existing_operational_route_geometry + current_grid_risk_v2 "
                "-> operational_route_risk_profile"
            ),
            "algorithm": f"{ALGORITHM_ID}@{ALGORITHM_VERSION}",
            "integral_helper": "cns_planner.risk.route_exposure (same index semantics)",
            "candidate_required": False,
            "search_cost_used": False,
            "candidate_mutated": False,
            "writes_operational_routes_or_cns": False,
            "replanning": False,
            "unresolved_cells": deepcopy(unresolved),
            "risk_v2_overall_used": False,
            "risk_semantics": "relative_engineering_index",
            "grid_mapping": {
                "method": "grid_graph_containing_cell",
                "sampling": SAMPLING_VERSION,
                "spacing_m": float(sampling_spacing_m),
            },
        },
        "semantics": deepcopy(SEMANTICS),
    }
    profile["subject"]["geometry_sha256"] = fingerprints.get("geometry_sha256")

    if consistency_status != "passed":
        profile["status"] = "inconsistent_evidence"
        profile["status_reason"] = "operational_geometry_inconsistent"
        base.update({"status": "inconsistent_evidence",
                     "status_reason": "operational_geometry_inconsistent", "profile": profile})
        base["blocking_reasons"] = [
            {"reason_code": check["reason_code"], "reason": check["reason"]}
            for check in checks if check["status"] != "passed"
        ]
        return base

    statuses = {item["status"] for item in domain_profiles.values()}
    if not risk_cells:
        status, reason_code = "unknown", "grid_risk_v2_cells_missing"
    elif statuses == {"resolved"}:
        status, reason_code = "passed", None
    elif "resolved" in statuses:
        status, reason_code = "unknown", "risk_index_partial"
    else:
        status, reason_code = "unknown", "risk_index_unavailable"
    profile["status"] = status
    profile["status_reason"] = reason_code
    profile["blocking_reasons"] = (
        [] if status == "passed" else [{
            "reason_code": reason_code,
            "reason": (
                "grid_risk_v2 明细缺失：风险指数不可用（不补 0）"
                if reason_code == "grid_risk_v2_cells_missing"
                else "风险域 index 未解析：只保留 unresolved 长度"
            ),
        }]
    )

    base["status"] = status
    base["status_reason"] = reason_code
    base["profile"] = profile
    if status != "passed":
        base["blocking_reasons"] = [{
            "reason_code": reason_code,
            "reason": (
                "grid_risk_v2 明细缺失：风险指数不可用（不补 0）"
                if reason_code == "grid_risk_v2_cells_missing"
                else "风险域 index 未解析（策略未确认或 cell 缺失）：只保留 unresolved 长度，不补 0"
            ),
        }]
    base["identity"] = {
        "grid_level": (grid or {}).get("level"),
        "grid_cell_count": len(grid_cells),
        "grid_risk_v2_input_fingerprint": (grid_risk_v2 or {}).get("input_fingerprint"),
        "grid_risk_v2_policy_fingerprint": (grid_risk_v2 or {}).get("policy_fingerprint"),
        "grid_risk_v2_cells_fingerprint": grid_risk_v2_cells_fingerprint(grid_risk_v2),
        "profile_policy_fingerprint": route_risk_profile_policy_fingerprint(policy),
        "profile_fingerprint": fingerprints["profile_fingerprint"],
        "geometry_sha256": fingerprints.get("geometry_sha256"),
    }
    return base


def _fingerprints(*, route, path, grid, grid_risk_v2, policy, cells, metric_length,
                  sampling_spacing_m):
    geometry_sha = path_fingerprint(path, cells)
    components = {
        "artifact_type": ARTIFACT_TYPE,
        "contract": CONTRACT_VERSION,
        "subject_kind": "operational_route",
        "route_id": (route or {}).get("route_id"),
        "altitude_layer_id": (route or {}).get("altitude_layer_id"),
        "geometry_sha256": geometry_sha,
        "path_fingerprint": path_fingerprint(path, cells),
        "grid_path": list(cells),
        "grid_level": (grid or {}).get("level"),
        "grid_risk_v2_input_fingerprint": (grid_risk_v2 or {}).get("input_fingerprint"),
        "grid_risk_v2_policy_fingerprint": (grid_risk_v2 or {}).get("policy_fingerprint"),
        "grid_risk_v2_cells_fingerprint": grid_risk_v2_cells_fingerprint(grid_risk_v2),
        "profile_policy_fingerprint": route_risk_profile_policy_fingerprint(policy),
        "domain_policy_fingerprints": domain_policy_fingerprints(policy),
        "factor_source_fingerprints": factor_source_fingerprints(grid_risk_v2),
        "geometry_length_m": _round(metric_length),
        "sampling": {"version": SAMPLING_VERSION, "spacing_m": sampling_spacing_m},
        "algorithm": f"{ALGORITHM_ID}@{ALGORITHM_VERSION}",
    }
    return {
        **deepcopy(components),
        "components": deepcopy(components),
        "profile_fingerprint": profile_fingerprint(components),
        "geometry_sha256": geometry_sha,
    }


__all__ = [
    "ARTIFACT_TYPE", "CONNECTOR_SEMANTICS", "CONTRACT_VERSION",
    "DEFAULT_SAMPLING_SPACING_M", "SAMPLING_VERSION", "SEMANTICS",
    "assess_operational_route_risk", "geometry_length_metric", "sample_metric_polyline",
]
