"""RouteRiskProfile V1 profiler：current LayeredRouteCandidate + current GridRiskV2 → 路径画像。

profiler 是**只读分析**：它不重规划、不修改 candidate、不写 ``operational_routes`` / CNS /
``RouteOperatingLayer``，也不使用 Risk Framework V2 的 ``overall``。

风险数学与 Layered Route Planner V1 完全同源：两者都调用
:mod:`cns_planner.risk.route_exposure` 的同一个积分 helper（``points = [start, *centers,
end]``、端点 connector 复用首/尾 cell index、``segment index = (left + right) / 2``、
``exposure = Σ(length * mean index)``），因此 profile 的 ``route_length_m`` 与 active domain
的 ``exposure_index_m`` 必须与 candidate 的 ``distance_m`` / ``cost_breakdown`` 在数值容差内
一致；不一致时返回 ``inconsistent_evidence``，绝不静默保存。

缺失即缺失：domain / factor 未解析的段只累加 ``unresolved_length_m``，绝不用 0 顶替。
"""

from __future__ import annotations

from copy import deepcopy
import math

from ..domain.risk_v2 import (
    DOMAIN_IDS, FACTOR_DEFINITIONS, FACTOR_IDS, NO_SOURCE_FACTOR_IDS,
    display_only_airspace,
)
from ..domain.route_risk_profile import (
    ALGORITHM_ID, ALGORITHM_VERSION, ARTIFACT_TYPE, classify_domain_index,
    domain_policy_fingerprints, empty_route_risk_profile, factor_source_fingerprints,
    grid_risk_v2_cells_fingerprint, normalize_route_risk_profile_policy, path_fingerprint,
    profile_fingerprint, route_risk_profile_policy_fingerprint,
)
from ..route_planner.risk_aware_v2 import GridGraph
from .accessors_v2 import cell_factor_record
from .route_exposure import DOMAIN_CELL_CONTAINER_KEY, integrate_path_exposure, resolve_cell_domain_indices

NUMERICAL_TOLERANCE = 1e-6

CONTRIBUTOR_SEMANTICS = "relative_engineering_contribution_not_accident_cause_probability"


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _round(value):
    return None if not _finite(value) else round(float(value), 9)


def _close(actual, expected, tolerance=NUMERICAL_TOLERANCE):
    if not _finite(actual) or not _finite(expected):
        return False
    return abs(float(actual) - float(expected)) <= max(tolerance, 1e-9 * abs(float(expected)))


class RouteRiskProfiler:
    algorithm_id = ALGORITHM_ID
    algorithm_version = ALGORITHM_VERSION
    numerical_tolerance = NUMERICAL_TOLERANCE

    # ------------------------------------------------------------------ public API

    def evaluate(self, *, candidate, grid, grid_risk_v2, profile_policy, expected=None):
        """分析一个 candidate；返回 JSON-safe RouteRiskProfile（可能带 blocking_reasons）。"""

        policy = normalize_route_risk_profile_policy(profile_policy)
        expected = expected if isinstance(expected, dict) else {}
        item = candidate if isinstance(candidate, dict) else {}
        profile = empty_route_risk_profile(
            "not_ready", candidate_id=item.get("candidate_id"),
            route_id=item.get("route_id"), altitude_layer_id=item.get("altitude_layer_id"),
        )
        profile["policy"] = deepcopy(policy)
        profile["candidate"].update({
            "candidate_id": item.get("candidate_id"),
            "status": item.get("status"),
            "route_id": item.get("route_id"),
            "altitude_layer_id": item.get("altitude_layer_id"),
            "lane_key": item.get("lane_key"),
            "grid_level": (grid or {}).get("level"),
            "candidate_fingerprint": item.get("candidate_fingerprint"),
            "input_fingerprint": item.get("input_fingerprint"),
            "risk_fingerprint": item.get("risk_fingerprint"),
            "feasibility_mask_fingerprint": item.get("feasibility_mask_fingerprint"),
            "current_applicability": (
                item.get("current_applicability") or expected.get("current_applicability")
            ),
        })
        profile["layer"] = {
            "altitude_layer_id": item.get("altitude_layer_id"),
            "grid_level": (grid or {}).get("level"),
        }
        profile["route"]["grid_level"] = (grid or {}).get("level")

        gate_status, reason_code, reason = _identity_gate(item, expected)
        if gate_status is not None:
            return _reject(profile, gate_status, reason_code, reason)

        grid_path = [str(value) for value in (item.get("grid_path") or [])]
        path = [list(value) for value in (item.get("path") or []) if isinstance(value, (list, tuple))]
        if not grid_path or len(path) < 2:
            return _reject(
                profile, "missing_data", "candidate_geometry_missing",
                "candidate 缺少 path/grid_path：无法积分路径暴露",
            )
        try:
            graph = GridGraph(list((grid or {}).get("cells") or []))
        except (TypeError, ValueError) as exc:
            return _reject(
                profile, "missing_data", "grid_unavailable", f"当前 MH/T 标准网格不可用：{exc}",
            )
        missing_cells = [grid_id for grid_id in grid_path if grid_id not in graph.centers]
        if missing_cells:
            return _reject(
                profile, "missing_data", "candidate_cells_not_in_current_grid",
                f"candidate 的 grid cell 不在当前标准网格内：{missing_cells[:5]}",
            )

        indices, unresolved = resolve_cell_domain_indices(grid_risk_v2, grid_path, DOMAIN_IDS)
        integral = integrate_path_exposure(
            start=path[0], end=path[-1], grid_path=grid_path, centers=graph.centers,
            indices=indices, domain_ids=DOMAIN_IDS,
        )
        route_length = integral["distance_m"]
        active_domains = _active_domains(item)
        risk_cells = (grid_risk_v2 or {}).get("cells") or {}
        risk_policy = ((grid_risk_v2 or {}).get("policy") or {})
        risk_domain_policies = risk_policy.get("domains") if isinstance(risk_policy.get("domains"), dict) else {}

        # The fingerprint only depends on the candidate/grid/risk/policy metadata, so it is
        # computed before the segments (whose ids embed the profile id).
        fingerprints = self.fingerprints(
            candidate=item, grid=grid, grid_risk_v2=grid_risk_v2, policy=policy,
            path=path, grid_path=grid_path,
        )
        profile_id = _profile_id(item, fingerprints["profile_fingerprint"])
        segments = [
            _route_risk_segment(
                segment, profile_id=profile_id, policy=policy, grid_risk_v2_cells=risk_cells,
                risk_domain_policies=risk_domain_policies,
            )
            for segment in integral["segments"]
        ]
        domain_profiles = _domain_profiles(
            domain_ids=DOMAIN_IDS, segments=segments, integral=integral,
            indices=indices, unresolved=unresolved, policy=policy,
            active_domains=active_domains, route_length=route_length, grid_path=grid_path,
        )
        factor_profiles, contributors = _factor_profiles(
            segments=segments, grid_risk_v2_cells=risk_cells,
            risk_domain_policies=risk_domain_policies, route_length=route_length,
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
                    "contributor_semantics": CONTRIBUTOR_SEMANTICS,
                }
                for factor_id, factor in factor_profiles.items()
                if factor["domain"] == domain_id
            }

        profile.update({
            "profile_id": profile_id,
            "policy": deepcopy(policy),
            "route_length_m": _round(route_length),
            "domains": domain_profiles,
            "factors": factor_profiles,
            "contributors": contributors,
            "classification": _classification_view(domain_profiles),
            "high_risk": _high_risk_view(domain_profiles),
            "segments": segments,
            "connector_semantics": deepcopy(integral["connector_semantics"]),
            "fingerprints": fingerprints,
            "route": {
                "route_id": item.get("route_id"),
                "altitude_layer_id": item.get("altitude_layer_id"),
                "grid_level": (grid or {}).get("level"),
                "cell_count": len(grid_path),
                "grid_path": list(grid_path),
                "path_fingerprint": fingerprints["path_fingerprint"],
            },
        })

        consistency = _consistency(
            candidate=item, profile=profile, integral=integral,
            active_domains=active_domains,
        )
        profile["consistency"] = consistency
        profile["provenance"] = {
            "pipeline": (
                "current_layered_route_candidate + current_grid_risk_v2 -> route_risk_profile"
            ),
            "algorithm": f"{ALGORITHM_ID}@{ALGORITHM_VERSION}",
            "integral_helper": "cns_planner.risk.route_exposure.integrate_path_exposure",
            "planner_cost_helper_shared": True,
            "domain_cell_container_key": deepcopy(DOMAIN_CELL_CONTAINER_KEY),
            "risk_semantics": "relative_engineering_index",
            "risk_v2_overall_used": False,
            "replanning": False,
            "candidate_mutated": False,
            "writes_operational_routes_or_cns": False,
            "unresolved_cells": deepcopy(unresolved),
            "airspace": display_only_airspace(),
            "notes": [
                "exposure / mean / max 与 candidate cost_breakdown 使用同一个积分 helper。",
                "缺失 domain/factor 的段只累加 unresolved 长度，绝不补 0。",
                "factor contributor 是 relative engineering contribution，不是事故原因概率。",
            ],
        }
        if consistency["status"] != "passed":
            profile["status"] = "inconsistent_evidence"
            profile["status_reason"] = "profile_candidate_metric_mismatch"
            profile["blocking_reasons"] = [
                {
                    "reason_code": check["reason_code"],
                    "reason": check["reason"],
                }
                for check in consistency["checks"] if check["status"] != "passed"
            ]
            return profile
        profile["status"] = "passed"
        profile["status_reason"] = None
        return profile

    # ------------------------------------------------------------------ fingerprints

    def fingerprints(self, *, candidate, grid, grid_risk_v2, policy, path, grid_path):
        item = candidate if isinstance(candidate, dict) else {}
        components = {
            "artifact_type": ARTIFACT_TYPE,
            "candidate_id": item.get("candidate_id"),
            "candidate_fingerprint": item.get("candidate_fingerprint"),
            "candidate_input_fingerprint": item.get("input_fingerprint"),
            "candidate_risk_fingerprint": item.get("risk_fingerprint"),
            "route_id": item.get("route_id"),
            "altitude_layer_id": item.get("altitude_layer_id"),
            "grid_level": (grid or {}).get("level"),
            "path_fingerprint": path_fingerprint(path, grid_path),
            "grid_path": list(grid_path or []),
            "grid_risk_v2_input_fingerprint": (grid_risk_v2 or {}).get("input_fingerprint"),
            "grid_risk_v2_policy_fingerprint": (grid_risk_v2 or {}).get("policy_fingerprint"),
            "grid_risk_v2_cells_fingerprint": grid_risk_v2_cells_fingerprint(grid_risk_v2),
            "profile_policy_fingerprint": route_risk_profile_policy_fingerprint(policy),
            "domain_policy_fingerprints": domain_policy_fingerprints(policy),
            "factor_source_fingerprints": factor_source_fingerprints(grid_risk_v2),
            "algorithm": f"{ALGORITHM_ID}@{ALGORITHM_VERSION}",
        }
        return {
            **deepcopy(components),
            "components": deepcopy(components),
            "profile_fingerprint": profile_fingerprint(components),
        }


def _profile_id(candidate, fingerprint):
    base = str(candidate.get("candidate_id") or candidate.get("lane_key") or "candidate")
    digest = str(fingerprint).rsplit("-", 1)[-1]
    return f"RRP-{base}-{digest[:16]}"


def _active_domains(candidate):
    breakdown = candidate.get("cost_breakdown")
    breakdown = breakdown if isinstance(breakdown, dict) else {}
    active = breakdown.get("active_domains")
    if isinstance(active, (list, tuple)):
        return tuple(str(item) for item in active if str(item) in DOMAIN_IDS)
    return tuple(
        domain_id for domain_id in DOMAIN_IDS
        if _finite((breakdown.get("lambdas") or {}).get(domain_id))
        and float((breakdown.get("lambdas") or {})[domain_id]) > 0.0
    )


def _identity_gate(candidate, expected):
    status = str(candidate.get("status") or "")
    if status == "stale" or candidate.get("status") == "stale":
        return "stale", "candidate_stale", (
            f"candidate 已 stale（{candidate.get('stale_reason') or 'stale'}）：不得生成 current profile"
        )
    if status != "candidate":
        return "not_ready", "candidate_not_current", (
            f"candidate.status={status or 'missing'} 不是 candidate/current：拒绝分析"
        )
    applicability = expected.get("current_applicability") or candidate.get("current_applicability")
    if applicability is not None and applicability != "current":
        return "stale", f"candidate_{applicability}", (
            f"candidate current_applicability={applicability}：输入已变化，必须重新规划后再分析"
        )
    if expected.get("lane_key") and candidate.get("lane_key") and (
        str(candidate["lane_key"]) != str(expected["lane_key"])
    ):
        return "inconsistent_evidence", "candidate_lane_mismatch", (
            "candidate 的 lane 与当前 planning request 不一致"
        )
    if expected.get("candidate_fingerprint") and (
        candidate.get("candidate_fingerprint") != expected["candidate_fingerprint"]
    ):
        return "inconsistent_evidence", "candidate_fingerprint_mismatch", (
            "candidate_fingerprint 与当前 state 重算结果不一致：拒绝生成 profile"
        )
    if expected.get("risk_fingerprint") and (
        candidate.get("risk_fingerprint") != expected["risk_fingerprint"]
    ):
        return "inconsistent_evidence", "candidate_risk_fingerprint_mismatch", (
            "candidate.risk_fingerprint 与当前 grid_risk_v2 不一致：拒绝生成 profile"
        )
    return None, None, None


def _reject(profile, status, reason_code, reason):
    profile["status"] = status
    profile["status_reason"] = reason_code
    profile["blocking_reasons"] = [{"reason_code": reason_code, "reason": reason}]
    return profile


# --------------------------------------------------------------------------- domains


def _domain_profiles(*, domain_ids, segments, integral, indices, unresolved, policy,
                     active_domains, route_length, grid_path):
    profiles = {}
    for domain_id in domain_ids:
        domain_policy = (policy.get("domains") or {}).get(domain_id) or {}
        resolved_length = 0.0
        unresolved_length = 0.0
        exposure = 0.0
        resolved_segments = 0
        unresolved_segments = 0
        length_by_level = {"low": 0.0, "medium": 0.0, "high": 0.0}
        unclassified_length = 0.0
        segment_levels = []
        for segment in segments:
            record = segment["domains"][domain_id]
            if record["resolved"]:
                resolved_length += segment["length_m"]
                exposure += record["exposure_index_m"] or 0.0
                resolved_segments += 1
                classification = classify_domain_index(record["mean_index"], domain_policy)
                level = classification.get("level")
                segment_levels.append((segment["index"], level, classification))
                if level in length_by_level:
                    length_by_level[level] += segment["length_m"]
                else:
                    unclassified_length += segment["length_m"]
            else:
                unresolved_length += segment["length_m"]
                unresolved_segments += 1
                segment_levels.append((segment["index"], None, None))
        max_index, max_location = _max_index(domain_id, integral)
        # The domain-level classification index is the path-length weighted mean index,
        # i.e. exactly ``cost_breakdown.mean_domain_index`` when the domain is an active
        # cost domain.  Per-segment levels drive the high-risk intervals; the domain level
        # is the path-level summary of the same confirmed thresholds.
        classification = classify_domain_index(
            exposure / resolved_length if resolved_length > 0 else None, domain_policy,
        )
        status = (
            "unresolved" if resolved_length == 0
            else "partial" if unresolved_length > 0
            else "resolved"
        )
        profile = {
            "domain_id": domain_id,
            "label": domain_policy.get("label"),
            "status": status,
            "active_cost_domain": domain_id in active_domains,
            "exposure_index_m": _round(exposure) if resolved_length > 0 else None,
            "mean_index": (
                round(exposure / resolved_length, 9) if resolved_length > 0 else None
            ),
            "max_index": _round(max_index),
            "max_location": max_location,
            "resolved_length_m": _round(resolved_length),
            "unresolved_length_m": _round(unresolved_length),
            "coverage": (
                round(resolved_length / route_length, 9) if route_length > 0 else None
            ),
            "resolved_segment_count": resolved_segments,
            "unresolved_segment_count": unresolved_segments,
            "unresolved_cells": _unresolved_cells(domain_id, grid_path, indices),
            "classification": classification,
            "high_risk": _domain_high_risk(
                domain_id, domain_policy, segments, segment_levels, resolved_length,
            ),
            "contributors": {},
            "length_by_level_m": {key: _round(value) for key, value in length_by_level.items()},
            "unclassified_length_m": _round(unclassified_length),
            "provenance": {
                "index_source": "grid_risk_v2_cells_domain_container",
                "container_key": DOMAIN_CELL_CONTAINER_KEY[domain_id],
                "integral_helper": "cns_planner.risk.route_exposure.integrate_path_exposure",
                "unresolved_never_zero": True,
                "unresolved_reason_codes": sorted({
                    reason for cell in _unresolved_cells(domain_id, grid_path, indices)
                    for reason in (unresolved.get(cell) or [])
                }),
            },
            "semantics": {
                "domain_id": domain_id,
                "relative_engineering_index": True,
                "not_accident_probability": True,
                "not_sora_grc": True,
                "not_sora_arc": True,
                "not_absolute_safety_risk": True,
                "missing_is_not_zero": True,
                "unresolved_length_preserved": True,
                "classification_per_domain_only": True,
            },
        }
        profiles[domain_id] = profile
    return profiles


def _max_index(domain_id, integral):
    points = integral["points"]
    point_indices = integral["point_domain_indices"]
    source_ids = integral["index_source_grid_ids"]
    offsets = [segment["start_cumulative_distance_m"] for segment in integral["segments"]]
    offsets.append(integral["segments"][-1]["end_cumulative_distance_m"])
    best, location = None, None
    for position, point_indices_at in enumerate(point_indices):
        value = point_indices_at.get(domain_id)
        if not _finite(value):
            continue
        if best is None or float(value) > best:
            best = float(value)
            location = {
                "coordinate": list(points[position]),
                "grid_id": source_ids[position],
                "role": (
                    "route_start_endpoint" if position == 0
                    else "route_end_endpoint" if position == len(points) - 1
                    else "grid_cell"
                ),
                "cumulative_distance_m": _round(offsets[position]),
            }
    return best, location


def _unresolved_cells(domain_id, grid_path, indices):
    return sorted({
        grid_id for grid_id in set(grid_path)
        if not _finite((indices.get(grid_id) or {}).get(domain_id))
    })


def _domain_high_risk(domain_id, domain_policy, segments, segment_levels, resolved_length):
    levels = {index: (level, classification) for index, level, classification in segment_levels}
    if str(domain_policy.get("status") or "") != "confirmed":
        return {
            "status": "not_configured",
            "length_m": None,
            "interval_count": None,
            "intervals": None,
            "reason": str(domain_policy.get("status_reason") or "thresholds_not_configured"),
            "semantics": "high_risk_requires_confirmed_thresholds",
        }
    intervals = []
    current = None
    for segment in segments:
        level = levels.get(segment["index"], (None, None))[0]
        if level == "high":
            mean = segment["domains"][domain_id]["mean_index"]
            cell_ids = sorted({
                value for value in (segment["start_index_grid_id"], segment["end_index_grid_id"])
                if value
            })
            if current is None:
                current = {
                    "start_cumulative_distance_m": segment["start_cumulative_distance_m"],
                    "end_cumulative_distance_m": segment["end_cumulative_distance_m"],
                    "length_m": segment["length_m"],
                    "exposure_index_m": (segment["length_m"] * mean) if _finite(mean) else 0.0,
                    "max_index": mean,
                    "segment_ids": [segment["segment_id"]],
                    "cell_ids": set(cell_ids),
                }
            else:
                current["end_cumulative_distance_m"] = segment["end_cumulative_distance_m"]
                current["length_m"] += segment["length_m"]
                if _finite(mean):
                    current["exposure_index_m"] += segment["length_m"] * mean
                    current["max_index"] = (
                        mean if current["max_index"] is None else max(current["max_index"], mean)
                    )
                current["segment_ids"].append(segment["segment_id"])
                current["cell_ids"].update(cell_ids)
        elif current is not None:
            intervals.append(_finalize_interval(domain_id, current, len(intervals)))
            current = None
    if current is not None:
        intervals.append(_finalize_interval(domain_id, current, len(intervals)))
    length = math.fsum(item["length_m"] for item in intervals)
    return {
        "status": "available",
        "length_m": _round(length),
        "interval_count": len(intervals),
        "intervals": intervals,
        "resolved_length_m": _round(resolved_length),
        "unclassified_length_m": _round(max(0.0, resolved_length - length)),
        "semantics": "continuous_high_segments_merged_per_domain_only",
    }


def _finalize_interval(domain_id, current, position):
    length = current["length_m"]
    return {
        "interval_id": f"{domain_id}-HR-{position + 1:04d}",
        "domain_id": domain_id,
        "start_distance_m": _round(current["start_cumulative_distance_m"]),
        "end_distance_m": _round(current["end_cumulative_distance_m"]),
        "length_m": _round(length),
        "max_index": _round(current["max_index"]),
        "mean_index": (
            round(current["exposure_index_m"] / length, 9) if length > 0 else None
        ),
        "segment_ids": list(current["segment_ids"]),
        "cell_ids": sorted(current["cell_ids"]),
    }


# --------------------------------------------------------------------------- factors


def _cell_factor_record(grid_risk_v2_cells, grid_id, factor_id):
    """canonical factor 读取：``grid_risk_v2.cells[gid]["factors"][factor_id]``。

    读取规则与 domain 读取共用同一 accessor 模块（``risk/accessors_v2.py``），不在此再写
    第二套路径。
    """

    record = (grid_risk_v2_cells or {}).get(str(grid_id)) or {}
    return cell_factor_record(record, factor_id)


def _record_resolved(record):
    index = (record or {}).get("normalized_index")
    if not _finite(index):
        return False
    if "resolved" in (record or {}):
        return bool(record.get("resolved"))
    return str(record.get("status") or "") == "passed"


def _factor_profiles(*, segments, grid_risk_v2_cells, risk_domain_policies, route_length):
    profiles = {}
    weighted_available = {}
    for factor_id in FACTOR_IDS:
        definition = FACTOR_DEFINITIONS[factor_id]
        domain_id = definition["domain"]
        domain_policy = (risk_domain_policies or {}).get(domain_id) or {}
        confirmed = str(domain_policy.get("status") or "") == "confirmed"
        weight = (domain_policy.get("weights") or {}).get(factor_id)
        weight = float(weight) if confirmed and _finite(weight) else None
        resolved_length = 0.0
        unresolved_length = 0.0
        normalized_exposure = 0.0
        raw_exposure = 0.0
        raw_complete = True
        weighted = 0.0 if weight is not None else None
        statuses = []
        source_ids, source_fingerprints, reference_fingerprints = set(), set(), set()
        for segment in segments:
            record = _segment_factor(
                segment, factor_id, grid_risk_v2_cells, weight=weight,
            )
            statuses.append(record["status"])
            if record["resolved"]:
                resolved_length += segment["length_m"]
                normalized_exposure += record["normalized_exposure_index_m"] or 0.0
                if weighted is not None:
                    weighted += (record["normalized_exposure_index_m"] or 0.0) * weight
                if _finite(record["mean_raw_value"]):
                    raw_exposure += segment["length_m"] * record["mean_raw_value"]
                else:
                    raw_complete = False
            else:
                unresolved_length += segment["length_m"]
                raw_complete = False
            for side in (record["from"], record["to"]):
                if side.get("source_id"):
                    source_ids.add(str(side["source_id"]))
                if side.get("source_fingerprint"):
                    source_fingerprints.add(str(side["source_fingerprint"]))
                if side.get("normalization_reference_fingerprint"):
                    reference_fingerprints.add(str(side["normalization_reference_fingerprint"]))
        status = _combine_status(statuses)
        profile = {
            "factor_id": factor_id,
            "domain": domain_id,
            "raw_unit": definition.get("raw_unit"),
            "label": definition.get("label"),
            "canonical_source_available": factor_id not in NO_SOURCE_FACTOR_IDS,
            "status": status,
            "resolved": bool(resolved_length > 0 and unresolved_length == 0),
            "resolved_length_m": _round(resolved_length),
            "unresolved_length_m": _round(unresolved_length),
            "coverage": round(resolved_length / route_length, 9) if route_length > 0 else None,
            "raw_exposure": (
                {"value": _round(raw_exposure), "unit": definition.get("raw_unit")}
                if raw_complete and resolved_length > 0 else None
            ),
            "normalized_exposure_index_m": (
                _round(normalized_exposure) if resolved_length > 0 else None
            ),
            "weighted_contribution_index_m": (
                _round(weighted) if (weight is not None and resolved_length > 0) else None
            ),
            "weight": weight,
            "contribution_status": "available" if weight is not None else "not_available",
            "contributor_rank": None,
            "contributor_semantics": CONTRIBUTOR_SEMANTICS,
            "source_ids": sorted(source_ids),
            "source_fingerprints": sorted(source_fingerprints),
            "normalization_reference_fingerprints": sorted(reference_fingerprints),
            "provenance": {
                "canonical_field": definition.get("source_field"),
                "source_role": definition.get("source_role"),
                "normalization_method": _first_normalization_method(segments, factor_id, grid_risk_v2_cells),
                "contributor_semantics": CONTRIBUTOR_SEMANTICS,
                "not_accident_cause_probability": True,
                "unresolved_never_zero": True,
            },
        }
        profiles[factor_id] = profile
        if profile["weighted_contribution_index_m"] is not None:
            weighted_available[factor_id] = profile["weighted_contribution_index_m"]

    if weighted_available:
        ranking = sorted(
            weighted_available.items(), key=lambda pair: (-pair[1], pair[0]),
        )
        for rank, (factor_id, _value) in enumerate(ranking, start=1):
            profiles[factor_id]["contributor_rank"] = rank
        contributors = {
            "status": "available",
            "ranking": [
                {
                    "rank": rank,
                    "factor_id": factor_id,
                    "domain": FACTOR_DEFINITIONS[factor_id]["domain"],
                    "weight": profiles[factor_id]["weight"],
                    "weighted_contribution_index_m": profiles[factor_id][
                        "weighted_contribution_index_m"
                    ],
                    "normalized_exposure_index_m": profiles[factor_id][
                        "normalized_exposure_index_m"
                    ],
                }
                for rank, (factor_id, _value) in enumerate(ranking, start=1)
            ],
            "reason": None,
            "semantics": CONTRIBUTOR_SEMANTICS,
        }
    else:
        contributors = {
            "status": "not_available",
            "ranking": [],
            "reason": "risk_v2_aggregation_policy_has_no_confirmed_contributor_weight",
            "semantics": CONTRIBUTOR_SEMANTICS,
        }
    return profiles, contributors


def _first_normalization_method(segments, factor_id, grid_risk_v2_cells):
    for segment in segments:
        record = _cell_factor_record(grid_risk_v2_cells, segment["start_index_grid_id"], factor_id)
        normalization = record.get("normalization")
        if isinstance(normalization, dict) and normalization.get("method"):
            return normalization.get("method")
    return None


def _segment_factor(segment, factor_id, grid_risk_v2_cells, *, weight):
    from_id, to_id = segment["start_index_grid_id"], segment["end_index_grid_id"]
    from_record = _cell_factor_record(grid_risk_v2_cells, from_id, factor_id)
    to_record = _cell_factor_record(grid_risk_v2_cells, to_id, factor_id)
    from_index = from_record.get("normalized_index")
    to_index = to_record.get("normalized_index")
    resolved = _record_resolved(from_record) and _record_resolved(to_record)
    mean = (float(from_index) + float(to_index)) / 2.0 if resolved else None
    from_raw, to_raw = from_record.get("raw_value"), to_record.get("raw_value")
    mean_raw = (
        (float(from_raw) + float(to_raw)) / 2.0
        if _finite(from_raw) and _finite(to_raw) else None
    )
    return {
        "factor_id": factor_id,
        "status": _combine_status([from_record.get("status"), to_record.get("status")]),
        "resolved": bool(resolved),
        "start_index": float(from_index) if _finite(from_index) else None,
        "end_index": float(to_index) if _finite(to_index) else None,
        "mean_index": None if mean is None else round(mean, 9),
        "normalized_exposure_index_m": (
            None if mean is None else round(segment["length_m"] * mean, 9)
        ),
        "mean_raw_value": None if mean_raw is None else round(mean_raw, 9),
        "weight": weight,
        "from": _factor_side(from_id, from_record),
        "to": _factor_side(to_id, to_record),
        "provenance": {
            "source_roles": sorted({
                str(value) for value in (
                    from_record.get("source_role"), to_record.get("source_role")
                ) if value
            }),
            "source_ids": sorted({
                str(value) for value in (
                    from_record.get("source_id"), to_record.get("source_id")
                ) if value
            }),
            "source_fingerprints": sorted({
                str(value) for value in (
                    from_record.get("source_fingerprint"), to_record.get("source_fingerprint")
                ) if value
            }),
            "normalization_reference_fingerprints": sorted({
                str(value) for value in (
                    (from_record.get("normalization") or {}).get("reference_fingerprint")
                    if isinstance(from_record.get("normalization"), dict) else None,
                    (to_record.get("normalization") or {}).get("reference_fingerprint")
                    if isinstance(to_record.get("normalization"), dict) else None,
                ) if value
            }),
            "unresolved_never_zero": True,
        },
    }


def _factor_side(grid_id, record):
    normalization = record.get("normalization") if isinstance(record.get("normalization"), dict) else {}
    index = record.get("normalized_index")
    return {
        "grid_id": grid_id,
        "status": record.get("status"),
        "resolved": _record_resolved(record),
        "normalized_index": float(index) if _finite(index) else None,
        "raw_value": float(record["raw_value"]) if _finite(record.get("raw_value")) else None,
        "coverage": record.get("coverage"),
        "reason": record.get("reason"),
        "quality_flags": list(record.get("quality_flags") or []),
        "source_id": record.get("source_id"),
        "source_role": record.get("source_role"),
        "source_fingerprint": record.get("source_fingerprint"),
        "normalization_method": normalization.get("method"),
        "normalization_reference_fingerprint": normalization.get("reference_fingerprint"),
    }


_STATUS_PRIORITY = ("stale", "missing_data", "not_available", "unknown", "partial", "passed")


def _combine_status(statuses):
    unique = {str(value) for value in statuses if value}
    if not unique:
        return "not_available"
    for candidate in _STATUS_PRIORITY:
        if candidate in unique:
            return candidate
    return sorted(unique)[0]


# --------------------------------------------------------------------------- segments


def _route_risk_segment(segment, *, profile_id, policy, grid_risk_v2_cells, risk_domain_policies):
    domains = {}
    for domain_id, record in segment["domains"].items():
        domain_policy = (policy.get("domains") or {}).get(domain_id) or {}
        classification = classify_domain_index(record["mean_index"], domain_policy)
        domains[domain_id] = {
            **deepcopy(record),
            "mean_index": _round(record["mean_index"]),
            "start_index": _round(record["start_index"]),
            "end_index": _round(record["end_index"]),
            "exposure_index_m": _round(record["exposure_index_m"]),
            "index_source": DOMAIN_CELL_CONTAINER_KEY[domain_id],
            "classification": {
                "status": classification["status"],
                "level": classification["level"],
                "reason": classification["reason"],
            },
        }
    return {
        "segment_id": f"{profile_id}-S{segment['index']:04d}",
        "index": segment["index"],
        "start_cumulative_distance_m": _round(segment["start_cumulative_distance_m"]),
        "end_cumulative_distance_m": _round(segment["end_cumulative_distance_m"]),
        "length_m": _round(segment["length_m"]),
        "start_coordinate": list(segment["start_coordinate"]),
        "end_coordinate": list(segment["end_coordinate"]),
        "from_grid_id": segment["from_grid_id"],
        "to_grid_id": segment["to_grid_id"],
        "from_grid_role": segment["from_grid_role"],
        "to_grid_role": segment["to_grid_role"],
        "start_index_grid_id": segment["start_index_grid_id"],
        "end_index_grid_id": segment["end_index_grid_id"],
        "connector": segment["connector"],
        "domains": domains,
        "factors": {
            factor_id: _segment_factor(segment, factor_id, grid_risk_v2_cells, weight=None)
            for factor_id in FACTOR_IDS
        },
        "provenance": {
            "connector_semantics": SEGMENT_CONNECTOR_SEMANTICS,
            "index_source_grid_ids": [
                segment["start_index_grid_id"], segment["end_index_grid_id"],
            ],
            "unresolved_never_zero": True,
        },
    }


#: 每段的 index 来源与 connector 语义（与 planner 的积分定义一致）。
SEGMENT_CONNECTOR_SEMANTICS = "endpoint_connector_reuses_first_last_cell_index"


# --------------------------------------------------------------------------- consistency


def _consistency(*, candidate, profile, integral, active_domains):
    tolerance = NUMERICAL_TOLERANCE
    checks = []
    route_length = integral["distance_m"]
    candidate_distance = candidate.get("distance_m")
    if not _finite(candidate_distance):
        checks.append({
            "check_id": "route_length_matches_candidate_distance_m",
            "status": "failed",
            "reason_code": "candidate_distance_missing",
            "reason": "candidate.distance_m 缺失：无法证明 profile route_length 与 candidate 一致",
            "profile_value": _round(route_length),
            "candidate_value": None,
        })
    elif not _close(route_length, float(candidate_distance), tolerance):
        checks.append({
            "check_id": "route_length_matches_candidate_distance_m",
            "status": "failed",
            "reason_code": "route_length_mismatch",
            "reason": (
                f"profile route_length_m={_round(route_length)} 与 candidate.distance_m="
                f"{candidate_distance} 不一致"
            ),
            "profile_value": _round(route_length),
            "candidate_value": _round(candidate_distance),
        })
    else:
        checks.append({
            "check_id": "route_length_matches_candidate_distance_m",
            "status": "passed",
            "reason_code": None,
            "reason": None,
            "profile_value": _round(route_length),
            "candidate_value": _round(candidate_distance),
        })

    breakdown = candidate.get("cost_breakdown")
    breakdown = breakdown if isinstance(breakdown, dict) else {}
    exposures = breakdown.get("domain_exposure_index_m")
    exposures = exposures if isinstance(exposures, dict) else {}
    for domain_id in active_domains:
        expected = exposures.get(domain_id)
        actual = profile["domains"][domain_id]["exposure_index_m"]
        if not _finite(expected):
            checks.append({
                "check_id": f"active_domain_exposure_matches_cost_breakdown:{domain_id}",
                "status": "failed",
                "reason_code": "candidate_domain_exposure_missing",
                "reason": (
                    f"active domain {domain_id} 在 candidate.cost_breakdown."
                    "domain_exposure_index_m 中缺失：无法证明一致性"
                ),
                "profile_value": actual,
                "candidate_value": None,
            })
        elif not _close(actual, float(expected), tolerance):
            checks.append({
                "check_id": f"active_domain_exposure_matches_cost_breakdown:{domain_id}",
                "status": "failed",
                "reason_code": "active_domain_exposure_mismatch",
                "reason": (
                    f"domain {domain_id} 的 profile exposure_index_m={actual} 与 candidate "
                    f"cost_breakdown={expected} 不一致"
                ),
                "profile_value": actual,
                "candidate_value": _round(expected),
            })
        else:
            checks.append({
                "check_id": f"active_domain_exposure_matches_cost_breakdown:{domain_id}",
                "status": "passed",
                "reason_code": None,
                "reason": None,
                "profile_value": actual,
                "candidate_value": _round(expected),
            })
    status = "passed" if all(item["status"] == "passed" for item in checks) else "inconsistent"
    return {
        "status": status,
        "tolerance": tolerance,
        "active_domains": list(active_domains),
        "checks": checks,
        "semantics": "profile_integral_must_equal_candidate_cost_breakdown_within_tolerance",
    }


# --------------------------------------------------------------------------- views


def _classification_view(domain_profiles):
    domains = {
        domain_id: {
            "domain_id": domain_id,
            "status": item["classification"]["status"],
            "level": item["classification"]["level"],
            "thresholds": deepcopy(item["classification"]["thresholds"]),
            "length_by_level_m": deepcopy(item["length_by_level_m"]),
            "unclassified_length_m": item["unclassified_length_m"],
            "reason": item["classification"]["reason"],
        }
        for domain_id, item in domain_profiles.items()
    }
    statuses = {item["status"] for item in domains.values()}
    return {
        "status": (
            "passed" if statuses == {"passed"}
            else "not_configured" if statuses <= {"not_configured", "not_available"}
            else "partial"
        ),
        "index_scope": "per_domain_only",
        "cross_domain_overall": "not_computed",
        "domains": domains,
        "semantics": "classification_is_per_domain_only_no_cross_domain_overall",
    }


def _high_risk_view(domain_profiles):
    domains = {
        domain_id: {
            "domain_id": domain_id,
            "status": item["high_risk"]["status"],
            "length_m": item["high_risk"]["length_m"],
            "interval_count": item["high_risk"]["interval_count"],
            "intervals": deepcopy(item["high_risk"]["intervals"]),
            "reason": item["high_risk"].get("reason"),
        }
        for domain_id, item in domain_profiles.items()
    }
    statuses = {item["status"] for item in domains.values()}
    return {
        "status": (
            "available" if statuses == {"available"}
            else "partial" if "available" in statuses
            else "not_configured"
        ),
        "index_scope": "per_domain_only",
        "cross_domain_high_risk": "not_computed",
        "domains": domains,
        "semantics": "continuous_high_segments_merged_per_domain_only",
    }


def build_route_risk_profile(*, candidate, grid, grid_risk_v2, profile_policy, expected=None):
    """Convenience wrapper around :class:`RouteRiskProfiler`."""

    return RouteRiskProfiler().evaluate(
        candidate=candidate, grid=grid, grid_risk_v2=grid_risk_v2,
        profile_policy=profile_policy, expected=expected,
    )


__all__ = ["CONTRIBUTOR_SEMANTICS", "NUMERICAL_TOLERANCE", "SEGMENT_CONNECTOR_SEMANTICS",
           "RouteRiskProfiler", "build_route_risk_profile"]
