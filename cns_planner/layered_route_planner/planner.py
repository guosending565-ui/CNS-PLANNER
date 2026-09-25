"""Layered Risk-Aware Route Planner V1 — feasibility mask + single-layer MH/T L8 A*.

Pipeline: ``scenario/OD route -> explicit AltitudeLayer -> terrain/building feasibility
mask -> MH/T L8 A* -> Risk Framework V2 soft cost -> LayeredRouteCandidate``.

The planner is a pure algorithm module: it never imports QGIS/GDAL, never opens a raster,
GeoPackage or file, and never writes ``operational_routes`` / CNS results.  Terrain and
building facts arrive as canonical JSON produced by the GIS boundary
(``cns_planner.gis.layered_feasibility_adapter``), which reuses the existing verified
FABDEM window sampler and the existing L8 building grid facts.

Edge cost::

    d * (1 + lambda_ground * Rg + lambda_air * Ra + lambda_env * Re)
    edge_risk_domain = (index_source + index_target) / 2

Every lambda is ``>= 0``, so the straight-line metric distance stays an admissible and
consistent heuristic.  A domain with ``lambda = 0`` is *not* a planning input at all; a
domain with ``lambda > 0`` whose V2 index is missing/unresolved/pending makes the cell
non-traversable.  There is no unknown penalty and no default risk.  The Risk Framework V2
``overall`` value is never used.
"""

from __future__ import annotations

from heapq import heappop, heappush
import math

from ..algorithms.coverage.v1 import distance_m
from ..risk.route_exposure import (
    integrate_path_exposure, resolve_cell_domain_indices,
)
from ..domain.layered_route import (
    COARSE_ENVELOPE_SEMANTICS, COST_DOMAIN_IDS,
    active_cost_domains, annotate_terminal_status, candidate_fingerprint, cost_lambdas,
    cost_policy_fingerprint, cost_policy_is_runnable, default_layer_feasibility_mask,
    default_layered_route_candidate, feasibility_policy_fingerprint,
    feasibility_policy_is_runnable, mask_cell, mask_fingerprint, planning_status_semantics,
    request_fingerprint, stable_fingerprint, terminal_reason_code,
)
from ..domain.building_clearance import (
    building_roof_elevation, evaluate_vertical_clearance,
)
from ..planning.grid_graph import GridGraph

ALGORITHM_ID = "layered_route_planner_v1"
ALGORITHM_VERSION = "1.0"

#: Set on every mask produced here.
MASK_SCOPE = "coarse_strategic_vertical_envelope"

#: 能力声明（Phase 3.5，**只声明**，不改变搜索行为）。与 Theta* V2 同构。
PLANNER_CAPABILITY = {
    "algorithm_id": ALGORITHM_ID,
    "algorithm_version": ALGORITHM_VERSION,
    "horizontal_grid_levels": [8],
    "available_levels": [8],
    "preferred_level": 8,
    "level_binding": "current_workspace_grid_level_used_as_is",
    "building_fact_level": 8,
    "cross_level_aggregation_allowed": False,
    "notes": [
        "搜索发生在工作区当前网格层级的水平面上，算法本身不做层级转换。",
        "建筑垂向包线只消费 L8 building_grid 事实；正式入口的工作区恒为 L8，"
        "因此建筑约束在正式规划中始终参与。",
        "legacy / diagnostic 的非 L8 工作区（已不由正式入口产生）仍会因层级不匹配而拿不到"
        "L8 建筑事实，此时算法绝不静默降级、绝不把 unknown 当 0。",
        "层级无关的建筑事实获取是已记录的后续需求，本轮不实现，也不静默降级。",
    ],
    "future_work": "level_independent_building_fact_acquisition",
}

#: The planner only ever searches inside the explicitly selected layer.
SEARCH_SEMANTICS = {
    "state": "single_selected_altitude_layer_per_mh_t_l8_cell",
    "no_free_3d_state": True,
    "no_cross_layer_edge": True,
    "adjacency": "existing_mh_t_grid_adjacency",
    "diagonal_corner_guard": True,
    "heuristic": "pure_metric_straight_line_distance",
    "hard_constraints_still_enforced": True,
    "risk_v2_overall_not_used": True,
    "airspace_not_used": True,
}


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _round(value):
    return None if value is None else round(float(value), 9)


def _terrain_elevation(terrain_fact):
    fact = terrain_fact if isinstance(terrain_fact, dict) else {}
    value = fact.get("surface_elevation_max_egm2008_m")
    if str(fact.get("data_status") or "") == "passed" and _finite(value):
        return float(value), None
    return None, str(fact.get("reason") or "terrain_data_unavailable")


def _tower_profile_status(cells):
    """铁塔障碍物事实的整体状态（只描述事实，不做任何风险判断）。"""

    statuses = set()
    for cell in cells or []:
        if not isinstance(cell, dict):
            continue
        fact = cell.get("towers") if isinstance(cell.get("towers"), dict) else {}
        statuses.add(str(fact.get("data_status") or "no_towers"))
    if "unknown" in statuses:
        return "unresolved_tower_heights_present"
    if "passed" in statuses:
        return "resolved"
    return "no_towers_in_scope"


def _grid_audit(source_audits, role):
    """One source audit record (asset identity), or ``None`` when it is not registered."""

    items = (source_audits or {}).get("items")
    if not isinstance(items, dict):
        return None
    entry = items.get(role)
    if not isinstance(entry, dict):
        return None
    return {
        "status": entry.get("status"),
        "source_id": entry.get("source_id"),
        "version_fingerprint": entry.get("version_fingerprint"),
        "sha256": entry.get("sha256"),
        "size_bytes": entry.get("size_bytes"),
        "mtime_ns": entry.get("mtime_ns"),
    }


def build_layer_feasibility_mask(
    *, request, cruise_altitude, cells, feasibility_policy, building_clearance_policy,
    source_audits=None, grid_level=None, adapter=None, tower_clearance_policy=None,
):
    """Build the coarse strategic vertical envelope for one explicitly selected layer.

    ``cells`` items are GIS-boundary canonical facts::

        {"grid_id": str, "terrain": {...}, "buildings": {...}, "towers": {...}}

    Rules (fail-closed, missing is never zero):

    * no confirmed ``terrain_vertical_clearance_m`` ⇒ every cell is ``unknown``;
    * terrain elevation missing / NoData ⇒ ``unknown``;
    * ``building_count == 0`` ⇒ no vertical building constraint at all;
    * a building cell with ``valid_height_fraction < 1`` or a missing height / missing
      terrain elevation ⇒ ``unknown``, never 0;
    * otherwise ``building_floor = terrain_cell_max + height_max + existing confirmed
      building vertical clearance`` (the existing ``BuildingClearanceV1`` semantics);
    * a cell containing real towers with a confirmed ``tower_clearance_policy`` ⇒
      ``tower_floor = max(tower_top_egm2008) + explicit tower vertical clearance``; a cell
      with towers but an unconfirmed policy or an unresolved tower top is ``unknown``
      (never "no obstacle"), and it is **never** a risk-factor input.
    """

    cruise = cruise_altitude if isinstance(cruise_altitude, dict) else {}
    layer_id = cruise.get("altitude_layer_id") or (request or {}).get("altitude_layer_id")
    altitude = cruise.get("altitude_egm2008_m") if cruise.get("status") == "confirmed" else None
    terrain_clearance = (feasibility_policy or {}).get("terrain_vertical_clearance_m")
    runnable, policy_reason = feasibility_policy_is_runnable(feasibility_policy)
    building_policy = building_clearance_policy if isinstance(building_clearance_policy, dict) else {}
    building_confirmed = (
        str(building_policy.get("status") or "") == "confirmed"
        and building_policy.get("vertical_clearance_m") is not None
    )
    building_vertical = (
        float(building_policy["vertical_clearance_m"]) if building_confirmed else None
    )
    tower_policy = tower_clearance_policy if isinstance(tower_clearance_policy, dict) else {}
    tower_clearance_vertical = tower_policy.get("tower_vertical_clearance_m")
    tower_clearance_horizontal = tower_policy.get("tower_horizontal_clearance_m")
    tower_policy_confirmed = (
        str(tower_policy.get("status") or "") == "confirmed"
        and tower_clearance_vertical is not None
        and tower_clearance_horizontal is not None
    )
    tower_vertical = float(tower_clearance_vertical) if tower_policy_confirmed else None
    records = {}
    for raw in cells or []:
        item = raw if isinstance(raw, dict) else {}
        grid_id = str(item.get("grid_id") or "")
        if not grid_id:
            continue
        records[grid_id] = item

    provenance = {
        "adapter": adapter,
        "feasibility_policy_fingerprint": feasibility_policy_fingerprint(feasibility_policy),
        "building_clearance_policy": {
            "status": building_policy.get("status"),
            "vertical_clearance_m": building_policy.get("vertical_clearance_m"),
            "horizontal_clearance_m": building_policy.get("horizontal_clearance_m"),
            "source": building_policy.get("source"),
            "confirmed": bool(building_policy.get("confirmed")),
            "definition": "domain/building_clearance.py::building_roof_elevation",
            "semantics": "existing_confirmed_building_clearance_policy_reused_not_redefined",
        },
        "terrain_sampling": COARSE_ENVELOPE_SEMANTICS["terrain_floor"],
        "cruise_altitude_conversion": cruise.get("conversion"),
    }

    result_cells = {}
    counts = {"feasible": 0, "blocked": 0, "unknown": 0}
    for grid_id in sorted(records):
        item = records[grid_id]
        elevation, terrain_reason = _terrain_elevation(item.get("terrain"))
        terrain_floor = (
            None if elevation is None or terrain_clearance is None
            else elevation + float(terrain_clearance)
        )
        building_fact = item.get("buildings") if isinstance(item.get("buildings"), dict) else {}
        building_status = str(building_fact.get("data_status") or "unknown")
        building_count = building_fact.get("building_count")
        building_count = int(building_count) if isinstance(building_count, (int, float)) and not isinstance(building_count, bool) else None
        building_height = building_fact.get("height_max_m")
        building_height = float(building_height) if _finite(building_height) else None
        fraction = building_fact.get("valid_height_fraction")
        fraction = float(fraction) if _finite(fraction) else None
        building_floor = None
        reason_code, reason = None, None

        if not runnable:
            status, reason_code, reason = "unknown", "terrain_clearance_not_confirmed", (
                "terrain_vertical_clearance_m 未确认（无默认值）：该格垂向可行性未知，既不是 "
                "feasible 也不是 blocked"
            )
        elif altitude is None:
            status, reason_code, reason = "unknown", "terrain_clearance_not_confirmed", (
                "selected AltitudeLayer 无法解析为 canonical EGM2008 巡航高度"
            )
        elif elevation is None or terrain_floor is None:
            status, reason_code, reason = "unknown", "terrain_data_unavailable", (
                f"FABDEM cell max 不可用：{terrain_reason or 'terrain_data_unavailable'}"
            )
        elif altitude < terrain_floor:
            status = "blocked"
            reason_code = "altitude_below_terrain_floor"
            reason = (
                f"巡航高度 {_round(altitude)} m 低于 terrain floor {_round(terrain_floor)} m "
                f"（= FABDEM cell max {_round(elevation)} + 显式 terrain clearance "
                f"{_round(terrain_clearance)}）"
            )
        elif building_status != "passed" or building_count is None:
            status, reason_code, reason = "unknown", "building_grid_missing_or_outside_coverage", (
                "L8 building grid fact 缺失或超出覆盖范围：建筑垂向约束未知，绝不当 0"
            )
        elif building_count == 0:
            status = "feasible"
            reason_code = None
            reason = "该 L8 格 building_count=0：没有建筑垂向约束"
        elif fraction is None or fraction < 1.0 or building_height is None:
            status, reason_code, reason = (
                "unknown", "building_height_or_ground_elevation_unresolved",
                "valid_height_fraction<1 或 height_max/terrain 缺失：建筑垂向约束未知，绝不当 0",
            )
        elif not building_confirmed:
            status, reason_code, reason = (
                "unknown", "building_vertical_clearance_not_confirmed",
                "building_clearance_policy 未确认：复用既有确认策略，不提供第二套默认净空",
            )
        else:
            roof = building_roof_elevation(elevation, building_height)
            roof_elevation = (
                roof.get("roof_elevation_egm2008_m")
                if roof.get("status") == "resolved" else None
            )
            if roof_elevation is None:
                status, reason_code, reason = (
                    "unknown", "building_height_or_ground_elevation_unresolved",
                    "building roof elevation 无法解析（既有 BuildingClearanceV1 语义）：未知，绝不当 0",
                )
            else:
                building_floor = float(roof_elevation) + building_vertical
                clearance = evaluate_vertical_clearance(
                    minimum_altitude_egm2008_m=altitude,
                    roof_elevation_egm2008_m=roof_elevation,
                    required_clearance_m=building_vertical,
                    ground_elevation_m=elevation,
                )
                if (
                    clearance.get("status") == "resolved"
                    and clearance.get("vertical_margin_m") is not None
                    and float(clearance["vertical_margin_m"]) >= 0.0
                ):
                    status, reason_code, reason = "feasible", None, "满足 coarse 建筑垂向包络"
                elif clearance.get("reason") == "aircraft_below_building_ground":
                    status = "blocked"
                    reason_code = "altitude_below_terrain_floor"
                    reason = (
                        f"巡航高度 {_round(altitude)} m 低于该格建筑地面高程 "
                        f"{_round(elevation)} m"
                    )
                else:
                    status = "blocked"
                    reason_code = "altitude_below_building_clearance_floor"
                    reason = (
                        f"巡航高度 {_round(altitude)} m 低于 building required clearance "
                        f"floor {_round(building_floor)} m（= terrain cell max + height_max + "
                        "既有建筑垂直净空）"
                    )

        # ---- 真实铁塔净空（Obstacle / Clearance，**不是** risk factor） --------------
        # 顺序与 COARSE_ENVELOPE_SEMANTICS 一致：terrain → building → tower。
        # 塔是点几何事实，网格只把它索引到相关 cell；同一 cell 内取塔顶最高值（保守）。
        # 有任何一塔高度未解析 ⇒ unknown（绝不当作"没有塔"）。
        tower_fact = item.get("towers") if isinstance(item.get("towers"), dict) else {}
        tower_count = tower_fact.get("tower_count")
        tower_count = int(tower_count) if _finite(tower_count) else 0
        tower_unresolved = tower_fact.get("unresolved_count")
        tower_unresolved = int(tower_unresolved) if _finite(tower_unresolved) else 0
        tower_top = tower_fact.get("tower_top_max_egm2008_m")
        tower_top = float(tower_top) if _finite(tower_top) else None
        tower_status, tower_reason_code, tower_reason = "not_applicable", None, None
        if tower_count > 0:
            if not tower_policy_confirmed:
                tower_status = "unknown"
                tower_reason_code = "tower_clearance_not_configured"
                tower_reason = (
                    f"该格有 {tower_count} 个真实铁塔，但 tower_clearance_policy 未确认"
                    "（垂直/水平净空都没有默认值）：塔净空未知，既不是 feasible 也不是 blocked"
                )
            elif tower_fact.get("data_status") != "passed" or tower_top is None:
                # resolved 只是解析状态；**未确认**的塔顶（或未解析）不构成 hard obstacle：
                # 该格保持 unknown 并在搜索中 fail-closed，绝不静默当作"净空足够"。
                tower_status = "unknown"
                tower_reason_code = str(
                    tower_fact.get("reason") or "tower_height_unresolved"
                )
                if tower_reason_code == "tower_top_not_confirmed":
                    tower_reason = (
                        f"该格有 {tower_count} 个真实铁塔，塔顶正高虽已解析（最高 "
                        f"{_round(tower_top)} m 仅作诊断）但**未确认**："
                        "未确认塔顶不是硬障碍证据，该格保持 unknown，fail-closed"
                    )
                else:
                    tower_reason = (
                        f"该格有 {tower_count} 个真实铁塔，其中 {tower_unresolved} 个塔顶 EGM2008 "
                        "正高未解析：不生成具体 tower clearance floor，该格保持 unknown，"
                        "在路径搜索中 fail-closed，不得作为已验证安全可通行区域"
                    )
            else:
                tower_floor = tower_top + tower_clearance_vertical
                if altitude is None:
                    tower_status = "unknown"
                    tower_reason_code = "tower_clearance_not_configured"
                    tower_reason = "巡航高度无法解析为 canonical EGM2008，塔净空未知"
                elif altitude < tower_floor:
                    tower_status = "blocked"
                    tower_reason_code = "altitude_below_tower_clearance_floor"
                    tower_reason = (
                        f"巡航高度 {_round(altitude)} m 低于 tower clearance floor "
                        f"{_round(tower_floor)} m（= 该格真实塔顶最高 "
                        f"{_round(tower_top)} m + 显式 tower 垂直净空 "
                        f"{_round(tower_clearance_vertical)}）"
                    )
                else:
                    tower_status = "passed"
        tower_resolved_top = tower_top if tower_status in ("passed", "blocked") else None
        if tower_status == "blocked" and status != "blocked":
            status, reason_code, reason = "blocked", tower_reason_code, tower_reason
        elif tower_status == "unknown" and status == "feasible":
            status, reason_code, reason = "unknown", tower_reason_code, tower_reason

        counts[status] += 1
        cell = mask_cell(
            grid_id, status=status, cruise_altitude_egm2008_m=altitude,
            terrain_elevation_m=_round(elevation), terrain_floor_egm2008_m=_round(terrain_floor),
            terrain_clearance_m=None if terrain_clearance is None else float(terrain_clearance),
            building_count=building_count,
            building_height_max_m=_round(building_height),
            building_required_clearance_egm2008_m=_round(building_floor),
            reason_code=reason_code, reason=reason,
            tower_count=(tower_count or None),
            tower_unresolved_count=(tower_unresolved or None),
            tower_top_max_egm2008_m=_round(tower_resolved_top),
            tower_required_clearance_egm2008_m=(
                _round(tower_resolved_top + tower_clearance_vertical)
                if tower_resolved_top is not None and tower_policy_confirmed else None
            ),
            provenance={
                **provenance,
                "terrain_data_status": (item.get("terrain") or {}).get("data_status"),
                "building_data_status": building_fact.get("data_status"),
                "valid_height_fraction": fraction,
                "building_clearance_policy_status": building_policy.get("status"),
                "tower_data_status": tower_fact.get("data_status"),
                "tower_clearance_policy_status": tower_policy.get("status"),
            },
        )
        result_cells[grid_id] = cell

    mask = default_layer_feasibility_mask(layer_id)
    mask.update({
        "status": "passed" if result_cells else "missing_data",
        "altitude_layer_id": layer_id,
        "grid_level": grid_level,
        "cell_count": len(result_cells),
        "counts": counts,
        "cells": result_cells,
        "terrain_vertical_clearance_m": (
            None if terrain_clearance is None else float(terrain_clearance)
        ),
        "building_vertical_clearance_m": building_vertical,
        "building_clearance_policy_status": building_policy.get("status"),
        "building_clearance_source": building_policy.get("source"),
        # ---- 真实铁塔净空（障碍物，不是风险因子） --------------------------------
        "tower_vertical_clearance_m": tower_vertical,
        "tower_horizontal_clearance_m": (
            float(tower_clearance_horizontal) if tower_policy_confirmed else None
        ),
        "tower_clearance_policy_status": tower_policy.get("status") or "not_configured",
        "tower_clearance_source": tower_policy.get("source"),
        "tower_obstacle_profile_status": _tower_profile_status(cells),
        "tower_cell_count": sum(
            1 for cell in result_cells.values() if (cell.get("tower_count") or 0) > 0
        ),
        "tower_unresolved_cell_count": sum(
            1 for cell in result_cells.values() if (cell.get("tower_unresolved_count") or 0) > 0
        ),
        "tower_obstacle_semantics": "obstacle_clearance_not_a_risk_factor",
        "cruise_altitude": {
            "status": cruise.get("status"),
            "altitude_egm2008_m": altitude,
            "vertical_reference": cruise.get("vertical_reference"),
            "nominal_altitude_m": cruise.get("nominal_altitude_m"),
            "conversion": cruise.get("conversion"),
            "reason": cruise.get("reason"),
        },
        "feasibility_policy_fingerprint": feasibility_policy_fingerprint(feasibility_policy),
        "request_fingerprint": request_fingerprint(request),
        "input_fingerprint": stable_fingerprint({
            "request_fingerprint": request_fingerprint(request),
            "feasibility_policy": feasibility_policy_fingerprint(feasibility_policy),
            "building_clearance_policy": {
                "status": building_policy.get("status"),
                "vertical_clearance_m": building_policy.get("vertical_clearance_m"),
                "source": building_policy.get("source"),
            },
            "tower_clearance_policy": {
                "status": tower_policy.get("status"),
                "tower_vertical_clearance_m": tower_policy.get("tower_vertical_clearance_m"),
                "tower_horizontal_clearance_m": tower_policy.get("tower_horizontal_clearance_m"),
                "source": tower_policy.get("source"),
            },
            "cruise_altitude": cruise.get("altitude_egm2008_m"),
            "cells": {
                grid_id: {
                    "terrain": (records[grid_id].get("terrain") or {}),
                    "buildings": (records[grid_id].get("buildings") or {}),
                    "towers": (records[grid_id].get("towers") or {}),
                }
                for grid_id in sorted(records)
            },
        }, prefix="layeredmaskinputv1-"),
        "source_audits": {
            role: _grid_audit(source_audits, role)
            for role in ("terrain_dtm", "buildings", "building_grid")
        },
        "adapter": adapter,
        "feasibility_policy_reason": policy_reason,
    })
    mask["mask_fingerprint"] = mask_fingerprint(mask)
    return mask


# --------------------------------------------------------------------------- risk indices


def resolve_lambda_domain_indices(grid_risk_v2, active_domains, cell_ids):
    """Resolve the per-cell Risk Framework V2 domain indices needed by the cost.

    Only the domains whose lambda is ``> 0`` are resolved.  A domain that is precisely
    ``0`` is not a planning input and is never required.  A cell missing a required domain
    index is reported in ``unresolved`` — it is never penalized with a default risk.

    The reading rule itself lives in :mod:`cns_planner.risk.route_exposure` so the planner
    cost and the RouteRiskProfile integral can never drift apart.
    """

    return resolve_cell_domain_indices(grid_risk_v2, cell_ids, tuple(active_domains))


# --------------------------------------------------------------------------- planner


class LayeredRoutePlannerV1:
    algorithm_id = ALGORITHM_ID
    algorithm_version = ALGORITHM_VERSION
    #: The cruise altitude layer is always an explicit planning request input.
    uses_explicit_altitude_layer = True
    uses_risk_framework_v2_domains = True
    uses_risk_v2_overall = False
    operational_route = False

    def __init__(self, parameters=None):
        supplied = dict(parameters or {})
        extra = set(supplied) - {"max_expanded_states"}
        if extra:
            raise ValueError(f"Layered Route Planner V1 不接受参数：{sorted(extra)}")
        self.parameters = {"max_expanded_states": None}
        if "max_expanded_states" in supplied and supplied["max_expanded_states"] is not None:
            value = supplied["max_expanded_states"]
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError("max_expanded_states 必须是正整数或 null")
            self.parameters["max_expanded_states"] = value

    # ------------------------------------------------------------------ public API

    def plan(
        self, *, request, scenario_route, grid, layer_mask, grid_risk_v2,
        feasibility_policy, cost_policy, hard_constraints=None,
        building_clearance_policy=None, source_audits=None,
        population_shelter=None, shelter_policy=None, regulatory_constraints=None,
        communication_field=None, objective_policy=None, risk_density_constraint=None,
    ):
        """Plan with the layered A* baseline.

        The V2-only keywords (``population_shelter`` / ``shelter_policy`` /
        ``regulatory_constraints`` / ``communication_field`` / ``objective_policy`` /
        ``risk_density_constraint``) are accepted and **ignored** on purpose: the registry
        and the application service drive both layered planners through one call shape, and
        V1's behaviour, cost and fingerprints must stay exactly as they were.
        """
        grid = grid if isinstance(grid, dict) else {}
        mask = layer_mask if isinstance(layer_mask, dict) else {}
        fingerprints = self.fingerprints(
            request=request, scenario_route=scenario_route, grid=grid,
            layer_mask=mask, grid_risk_v2=grid_risk_v2, cost_policy=cost_policy,
            feasibility_policy=feasibility_policy, hard_constraints=hard_constraints,
            building_clearance_policy=building_clearance_policy,
            source_audits=source_audits,
        )
        def blocked(status, reason, code, **extra):
            """Structured non-path result (same terminal-status contract as Theta* V2).

            ``no_path`` = a completed search without a traversable path;
            ``search_incomplete`` = the expansion budget stopped the search, so reachability
            is **unproven** and the result must not be read as "the airspace is infeasible";
            ``invalid_input`` = the planning input itself cannot be interpreted.
            """

            return annotate_terminal_status(self._result(
                status=status, request=request, fingerprints=fingerprints, reason=reason,
                blocking_reasons=[{
                    "reason_code": terminal_reason_code(status, code),
                    "reason": reason,
                    "terminal_status": str(status),
                    "terminal_status_semantics": planning_status_semantics(status),
                }],
                **extra,
            ), status)
        if not isinstance(request, dict) or request.get("status") != "confirmed":
            return blocked(
                "not_ready", "显式 LayeredRoutePlanningRequest 未确认：必须显式选择 scenario/OD 与 "
                "AltitudeLayer，不从 profile 推断高度层",
                str((request or {}).get("status_reason") or "planning_request_not_confirmed"),
            )
        runnable, reason = cost_policy_is_runnable(cost_policy)
        if not runnable:
            return blocked(
                "not_ready",
                "LayeredRouteCostPolicy 未确认（λ 无默认值，null != 0）：无法构建 edge cost",
                reason or "cost_policy_not_confirmed",
            )
        runnable, reason = feasibility_policy_is_runnable(feasibility_policy)
        if not runnable:
            return blocked(
                "not_ready",
                "LayeredRouteFeasibilityPolicy 未确认（terrain_vertical_clearance_m 无默认值）",
                reason or "feasibility_policy_not_confirmed",
            )
        if not grid.get("cells"):
            return blocked(
                "invalid_input", "当前 MH/T 标准网格不可用（cells 为空）", "grid_unavailable",
            )
        if str(mask.get("status") or "") != "passed" or not mask.get("cells"):
            return blocked(
                "invalid_input", "LayerFeasibilityMask 缺失或不可用",
                "feasibility_mask_unavailable",
            )
        stale_reason = self._mask_staleness(
            mask, request=request, feasibility_policy=feasibility_policy,
        )
        if stale_reason:
            return blocked(
                "stale", "LayerFeasibilityMask 已过期，必须按当前 request / policy 重新计算",
                stale_reason, mask_status="stale",
            )

        route = scenario_route if isinstance(scenario_route, dict) else {}
        route_id = str(route.get("route_id") or "")
        start, end = route.get("start"), route.get("end")
        if not route_id or not _point(start) or not _point(end):
            return blocked(
                "invalid_input", "scenario/OD 航路端点或 route_id 缺失",
                "scenario_route_endpoints_missing",
            )

        graph = GridGraph(list(grid["cells"]))
        source, target = graph.containing_cell(start), graph.containing_cell(end)
        if source is None or target is None:
            return blocked(
                "invalid_input", "起点或终点不在当前标准网格内", "endpoints_outside_grid",
            )

        candidates = sorted(set(mask["cells"]) & set(graph.cells))
        active_domains = active_cost_domains(cost_policy)
        lambdas = cost_lambdas(cost_policy)
        indices, unresolved = resolve_lambda_domain_indices(
            grid_risk_v2, active_domains, candidates,
        )

        hard_blocked = {
            grid_id for grid_id, cell in graph.cells.items()
            if any(
                _positive_bbox_intersection(cell["bbox"], _constraint_bbox(item))
                for item in hard_constraints or []
            )
        }
        if (
            _point_in_constraints(start, hard_constraints)
            or _point_in_constraints(end, hard_constraints)
            or source in hard_blocked or target in hard_blocked
        ):
            return blocked(
                "blocked", "起点或终点落入显式硬约束范围", "endpoint_in_hard_constraint",
            )

        traversable = set()
        excluded = {}
        for grid_id in candidates:
            cell = mask["cells"][grid_id]
            if str(cell.get("status")) != "feasible":
                excluded[grid_id] = "mask_not_feasible"
            elif grid_id in hard_blocked:
                excluded[grid_id] = "hard_constraint"
            elif grid_id in unresolved:
                excluded[grid_id] = "risk_domain_unresolved"
            else:
                traversable.add(grid_id)

        domain_reason = {
            "ground": "risk_domain_unresolved:ground（λ_ground>0 但该格 V2 domain index 缺失/未解析/未确认）",
            "air_traffic": "risk_domain_unresolved:air_traffic（λ_air>0 但该格 V2 domain index 缺失/未解析/未确认）",
            "environment_obstacle": "risk_domain_unresolved:environment_obstacle（λ_env>0 但该格 V2 domain index 缺失/未解析/未确认）",
        }
        for grid_id, code in excluded.items():
            if code == "risk_domain_unresolved":
                reasons = unresolved.get(grid_id) or []
                domains = sorted({item.split(":", 1)[0] for item in reasons})
                reason = "；".join(domain_reason.get(item, item) for item in domains)
                self._mark_cell(mask, grid_id, "risk_domain_unresolved", reason)
            elif code == "hard_constraint":
                self._mark_cell(mask, grid_id, "hard_constraint", "该格与显式硬约束相交")

        if source in excluded or target in excluded:
            # The endpoints are checked before any search runs, so they get the same
            # no-path vocabulary the search would have produced.
            codes = {
                "mask_not_feasible": "endpoint_not_feasible",
                "hard_constraint": "endpoint_in_hard_constraint",
                "risk_domain_unresolved": "no_traversable_path",
            }
            code = codes.get(excluded.get(source) or excluded.get(target), "endpoint_not_traversable")
            blocked_at = source if source in excluded else target
            reason = (
                f"起点或终点不可遍历：{excluded.get(blocked_at)}"
                + (f" · {blocked_at}" if blocked_at else "")
            )
            return blocked(
                "no_path", reason, code,
                mask=mask,
                statistics={
                    "mask_counts": deepcopy_counts(mask),
                    "traversable_count": len(traversable),
                    "active_cost_domains": list(active_domains),
                },
            )
        if not traversable:
            return blocked(
                "no_path",
                "没有任何可行 cell：selected layer 在该 MH/T 网格内没有 feasible 垂向包络",
                "no_feasible_cell_in_selected_layer",
                mask=mask,
                statistics={"mask_counts": deepcopy_counts(mask), "traversable_count": 0},
            )

        expansion = _astar(
            graph, source, target, traversable, indices, lambdas, active_domains,
            self.parameters["max_expanded_states"],
        )
        if expansion["grid_path"] is None:
            # A budget-limited search stops before reachability is decided: it is
            # ``search_incomplete`` and never evidence that the airspace is infeasible.
            incomplete = bool(expansion["cap_reached"])
            reason = (
                "搜索预算耗尽（达到 max_expanded_states）：可达性未被证明，这不是空域不可行"
                if incomplete else
                "风险证据缺失阻断，未找到可用路径"
                if any(code == "risk_domain_unresolved" for code in excluded.values()) else
                "feasible cell 不连通或显式硬约束阻断，未找到可用路径"
            )
            return blocked(
                "search_incomplete" if incomplete else "no_path",
                reason,
                "search_budget_exhausted" if incomplete else "no_traversable_path",
                mask=mask,
                statistics={
                    "mask_counts": deepcopy_counts(mask),
                    "traversable_count": len(traversable),
                    "active_cost_domains": list(active_domains),
                    "expanded_states": expansion["expanded_states"],
                    "search_completeness": (
                        "expansion_cap_reached_optimality_not_proven"
                        if incomplete else "search_exhausted_no_traversable_path"
                    ),
                },
                search_incomplete=incomplete,
            )
        grid_path = expansion["grid_path"]
        path = _path_with_real_endpoints(start, end, grid_path, graph.centers)
        metrics = _path_metrics(
            path, grid_path, graph.centers, indices, lambdas, active_domains, start, end,
        )
        straight = distance_m(start, end)
        return annotate_terminal_status(self._result(
            status="candidate", request=request, fingerprints=fingerprints,
            reason="Layered Route Planner V1 candidate 规划完成",
            path=path, grid_path=grid_path, mask=mask,
            distance_m=metrics["distance_m"],
            straight_line_distance_m=straight,
            detour_factor=metrics["distance_m"] / straight if straight > 0 else 1.0,
            optimization_cost=metrics["optimization_cost"],
            cost_breakdown=metrics["cost_breakdown"],
            statistics={
                "mask_counts": deepcopy_counts(mask),
                "traversable_count": len(traversable),
                "active_cost_domains": list(active_domains),
                "expanded_states": expansion["expanded_states"],
                "search_completeness": (
                    "expansion_cap_reached_optimality_not_proven"
                    if expansion["cap_reached"] else "optimal_path_found"
                ),
            },
            search_incomplete=expansion["cap_reached"],
        ), "candidate")

    # ------------------------------------------------------------------ helpers

    def fingerprints(
        self, *, request, scenario_route, grid, layer_mask, grid_risk_v2, cost_policy,
        feasibility_policy, hard_constraints, building_clearance_policy, source_audits,
        population_shelter=None, shelter_policy=None, regulatory_constraints=None,
        communication_field=None, objective_policy=None, risk_density_constraint=None,
    ):
        """V1's declared dependency fingerprint.

        The V2-only keywords are accepted and ignored so both layered planners share one
        call shape; V1's components — and therefore its ``candidate_fingerprint`` — are
        unchanged.
        """
        route = scenario_route if isinstance(scenario_route, dict) else {}
        grid_cells = (grid or {}).get("cells") or []
        components = {
            "scenario_route_id": route.get("route_id"),
            "grid_identity": stable_fingerprint(
                [
                    {"grid_id": cell.get("grid_id"), "level": cell.get("level"), "bbox": cell.get("bbox")}
                    for cell in sorted(grid_cells, key=lambda item: str(item.get("grid_id")))
                ],
                prefix="layeredgridv1-",
            ),
            "altitude_layer_id": (request or {}).get("altitude_layer_id"),
            "request_fingerprint": request_fingerprint(request),
            "hard_constraints": list(hard_constraints or []),
            "terrain_source_audit": _grid_audit(source_audits, "terrain_dtm"),
            "building_source_audit": _grid_audit(source_audits, "buildings"),
            "building_grid_source_audit": _grid_audit(source_audits, "building_grid"),
            "building_clearance_policy": {
                "status": (building_clearance_policy or {}).get("status"),
                "vertical_clearance_m": (building_clearance_policy or {}).get("vertical_clearance_m"),
                "horizontal_clearance_m": (building_clearance_policy or {}).get("horizontal_clearance_m"),
                "source": (building_clearance_policy or {}).get("source"),
            },
            "risk_framework_v2_input_fingerprint": (grid_risk_v2 or {}).get("input_fingerprint"),
            "risk_framework_v2_policy_fingerprint": (grid_risk_v2 or {}).get("policy_fingerprint"),
            "cost_policy": cost_policy_fingerprint(cost_policy),
            "feasibility_policy": feasibility_policy_fingerprint(feasibility_policy),
            "feasibility_mask_fingerprint": (layer_mask or {}).get("mask_fingerprint"),
            "planner_version": f"{ALGORITHM_ID}@{ALGORITHM_VERSION}",
        }
        return {
            "components": components,
            "candidate_fingerprint": candidate_fingerprint(components),
            "input_fingerprint": stable_fingerprint(components, prefix="layeredinputv1-"),
            "feasibility_fingerprint": stable_fingerprint({
                "feasibility_policy": components["feasibility_policy"],
                "mask": components["feasibility_mask_fingerprint"],
                "terrain_source_audit": components["terrain_source_audit"],
                "building_source_audit": components["building_source_audit"],
                "building_grid_source_audit": components["building_grid_source_audit"],
                "building_clearance_policy": components["building_clearance_policy"],
            }, prefix="layeredfeasibilityv1-"),
            "risk_fingerprint": (
                None if not (grid_risk_v2 or {}).get("input_fingerprint") else
                stable_fingerprint({
                    "input_fingerprint": (grid_risk_v2 or {}).get("input_fingerprint"),
                    "policy_fingerprint": (grid_risk_v2 or {}).get("policy_fingerprint"),
                    "active_domains": list(active_cost_domains(cost_policy)),
                }, prefix="layeredriskv1-")
            ),
            "policy_fingerprint": stable_fingerprint({
                "cost_policy": cost_policy_fingerprint(cost_policy),
                "feasibility_policy": feasibility_policy_fingerprint(feasibility_policy),
            }, prefix="layeredpolicyv1-"),
            "request_fingerprint": components["request_fingerprint"],
        }

    @staticmethod
    def _mask_staleness(mask, *, request, feasibility_policy):
        expected = request_fingerprint(request)
        if mask.get("request_fingerprint") not in (None, expected):
            return "request_changed"
        if mask.get("feasibility_policy_fingerprint") not in (
            None, feasibility_policy_fingerprint(feasibility_policy),
        ):
            return "feasibility_policy_changed"
        if mask.get("altitude_layer_id") != (request or {}).get("altitude_layer_id"):
            return "altitude_layer_changed"
        return None

    @staticmethod
    def _mark_cell(mask, grid_id, reason_code, reason):
        cell = (mask.get("cells") or {}).get(grid_id)
        if not isinstance(cell, dict):
            return
        if cell.get("status") == "feasible":
            cell["status"] = "blocked"
            counts = mask.setdefault("counts", {})
            counts["feasible"] = max(0, int(counts.get("feasible", 0)) - 1)
            counts["blocked"] = int(counts.get("blocked", 0)) + 1
        cell["reason_code"] = reason_code
        cell["reason"] = reason

    def _result(
        self, *, status, request, fingerprints, reason, blocking_reasons=None, path=None,
        grid_path=None, mask=None, distance_m=None, straight_line_distance_m=None,
        detour_factor=None, optimization_cost=None, cost_breakdown=None, statistics=None,
        search_incomplete=False, mask_status=None,
    ):
        candidate = default_layered_route_candidate(
            (request or {}).get("scenario_route_id") or _od_label(request),
            (request or {}).get("altitude_layer_id"), status,
        )
        candidate.update({
            "candidate_id": _candidate_id(fingerprints, request),
            "path": list(path or []),
            "grid_path": list(grid_path or []),
            "cell_count": len(grid_path or []),
            "distance_m": _round(distance_m),
            "straight_line_distance_m": _round(straight_line_distance_m),
            "detour_factor": None if detour_factor is None else round(float(detour_factor), 9),
            "optimization_cost": _round(optimization_cost),
            "cost_breakdown": cost_breakdown or default_layered_route_candidate()["cost_breakdown"],
            "reason": reason,
            "blocking_reasons": list(blocking_reasons or []),
            "input_fingerprint": fingerprints["input_fingerprint"],
            "feasibility_fingerprint": fingerprints["feasibility_fingerprint"],
            "risk_fingerprint": fingerprints["risk_fingerprint"],
            "policy_fingerprint": fingerprints["policy_fingerprint"],
            "request_fingerprint": fingerprints["request_fingerprint"],
            "candidate_fingerprint": fingerprints["candidate_fingerprint"],
            "feasibility_mask_fingerprint": (mask or {}).get("mask_fingerprint"),
            "statistics": statistics or {},
            "search_incomplete": bool(search_incomplete),
            "mask_status": mask_status,
            "provenance": {
                "pipeline": (
                    "scenario_or_od_route -> explicit_altitude_layer -> terrain_building_"
                    "feasibility_mask -> mh_t_l8_astar -> risk_framework_v2_soft_cost -> "
                    "layered_route_candidate"
                ),
                "fingerprint_components": fingerprints["components"],
                "search_semantics": SEARCH_SEMANTICS,
                "feasibility_semantics": COARSE_ENVELOPE_SEMANTICS,
                "airspace": {
                    "status": "not_applicable", "applicability": "display_only",
                    "used_in_search": False, "used_in_fingerprint": False,
                },
            },
        })
        return candidate


def deepcopy_counts(mask):
    counts = (mask or {}).get("counts") or {}
    return {key: int(counts.get(key) or 0) for key in ("feasible", "blocked", "unknown")}


def _od_label(request):
    item = request if isinstance(request, dict) else {}
    start, end = item.get("start_node_id"), item.get("end_node_id")
    return f"{start}->{end}" if start and end else None


def _candidate_id(fingerprints, request):
    item = request if isinstance(request, dict) else {}
    route_id = item.get("scenario_route_id") or _od_label(request) or "route"
    epoch = (item.get("evidence") or {}).get("evaluated_at")
    suffix = str(epoch) if epoch else fingerprints["candidate_fingerprint"][:16]
    return f"LRC-{route_id}-{item.get('altitude_layer_id') or 'layer'}-{suffix}"


# --------------------------------------------------------------------------- A*


def _astar(
    graph, source, target, traversable, indices, lambdas, active_domains, max_expanded_states,
):
    """A* over the single selected layer.

    ``traversable`` already excludes non-feasible cells, hard-constraint cells and cells
    with an unresolved index for an enabled (``lambda > 0``) domain.  The heuristic is the
    pure metric straight-line distance: every lambda is ``>= 0``, so the edge cost is never
    below the edge length.  There is no cross-layer edge and no free 3D state.
    """

    def edge_cost(left, right, length):
        risk = 0.0
        for domain_id in active_domains:
            lam = lambdas.get(domain_id)
            left_index, right_index = indices[left][domain_id], indices[right][domain_id]
            mean = (left_index + right_index) / 2.0
            risk += float(lam) * mean
        return length * (1.0 + risk)

    queue = [(distance_m(graph.centers[source], graph.centers[target]), 0.0, source)]
    costs = {source: 0.0}
    previous = {}
    expanded = 0
    cap_reached = False
    while queue:
        _, current_cost, current = heappop(queue)
        if current_cost > costs.get(current, math.inf) + 1e-9:
            continue
        if current == target:
            break
        if max_expanded_states is not None and expanded >= max_expanded_states:
            cap_reached = True
            break
        expanded += 1
        for neighbour in graph.neighbors(current):
            if neighbour not in traversable:
                continue
            guards = graph.diagonal_guards(current, neighbour)
            if guards is not None and (None in guards or any(item not in traversable for item in guards)):
                continue
            length = distance_m(graph.centers[current], graph.centers[neighbour])
            candidate = current_cost + edge_cost(current, neighbour, length)
            known = costs.get(neighbour, math.inf)
            if candidate < known - 1e-9:
                costs[neighbour] = candidate
                previous[neighbour] = current
                heuristic = distance_m(graph.centers[neighbour], graph.centers[target])
                heappush(queue, (candidate + heuristic, candidate, neighbour))
            elif math.isclose(candidate, known, abs_tol=1e-9) and current < previous.get(neighbour, "\uffff"):
                previous[neighbour] = current
    if target not in costs:
        return {
            "grid_path": None, "expanded_states": expanded, "cap_reached": cap_reached,
            "unresolved_blocked": False,
        }
    path, current = [target], target
    while current != source:
        current = previous[current]
        path.append(current)
    path.reverse()
    return {
        "grid_path": path, "expanded_states": expanded, "cap_reached": cap_reached,
        "unresolved_blocked": False,
    }


def _path_metrics(path, grid_path, centers, indices, lambdas, active_domains, start, end):
    """Integrate the candidate path with the shared, backend-only exposure helper.

    ``cns_planner.risk.route_exposure.integrate_path_exposure`` is the single integral
    definition also used by the RouteRiskProfile profiler: ``points = [start, *centers,
    end]``, endpoint connectors reuse the first/last cell index, ``segment index =
    (left + right) / 2`` and ``exposure = Σ(length * mean index)``.  Only the ``λ > 0``
    domains are integrated, exactly as before the helper was extracted.
    """

    integral = integrate_path_exposure(
        start=start, end=end, grid_path=grid_path, centers=centers, indices=indices,
        domain_ids=COST_DOMAIN_IDS, integration_domains=active_domains,
    )
    distance = integral["distance_m"]
    domain_exposure = integral["domain_exposure_index_m"]
    weighted = {
        domain_id: (
            None if domain_id not in active_domains
            else float(lambdas[domain_id]) * domain_exposure[domain_id]
        )
        for domain_id in COST_DOMAIN_IDS
    }
    optimization_cost = distance + math.fsum(
        value for value in weighted.values() if value is not None
    )
    return {
        "distance_m": distance,
        "optimization_cost": optimization_cost,
        "cost_breakdown": {
            "distance_contribution_m": _round(distance),
            "lambda_weighted_contributions_m": {
                domain_id: _round(weighted[domain_id]) for domain_id in COST_DOMAIN_IDS
            },
            "domain_exposure_index_m": {
                domain_id: (
                    None if domain_id not in active_domains
                    else _round(domain_exposure[domain_id])
                )
                for domain_id in COST_DOMAIN_IDS
            },
            "mean_domain_index": {
                domain_id: (
                    None if domain_id not in active_domains or distance <= 0
                    else round(domain_exposure[domain_id] / distance, 9)
                )
                for domain_id in COST_DOMAIN_IDS
            },
            "lambdas": {
                domain_id: (
                    None if lambdas.get(domain_id) is None else float(lambdas[domain_id])
                )
                for domain_id in COST_DOMAIN_IDS
            },
            "active_domains": list(active_domains),
            "formula": "edge_cost = d * (1 + Σ λ_domain · mean_domain_index)",
            "heuristic": "pure_metric_straight_line_distance_admissible_because_all_lambda_nonnegative",
            "risk_v2_overall_used": False,
        },
    }


def _path_with_real_endpoints(start, end, grid_path, centers):
    values = [list(start), *[list(centers[item]) for item in grid_path], list(end)]
    result = []
    for point in values:
        if not result or point != result[-1]:
            result.append(point)
    return result


def _point(value):
    return isinstance(value, (list, tuple)) and len(value) >= 2 and all(
        _finite(item) for item in value[:2]
    )


def _positive_bbox_intersection(a, b):
    if not isinstance(b, (list, tuple)) or len(b) != 4:
        return False
    try:
        return (
            min(a[2], float(b[2])) > max(a[0], float(b[0]))
            and min(a[3], float(b[3])) > max(a[1], float(b[1]))
        )
    except (TypeError, ValueError):
        return False


def _point_in_constraints(point, constraints):
    for item in constraints or []:
        bbox = item.get("bbox") if isinstance(item, dict) else None
        if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
            if (
                float(bbox[0]) <= float(point[0]) <= float(bbox[2])
                and float(bbox[1]) <= float(point[1]) <= float(bbox[3])
            ):
                return True
    return False


def _constraint_bbox(item):
    return item.get("bbox") if isinstance(item, dict) else None


__all__ = [
    "ALGORITHM_ID", "ALGORITHM_VERSION", "MASK_SCOPE", "SEARCH_SEMANTICS",
    "LayeredRoutePlannerV1", "build_layer_feasibility_mask",
    "resolve_lambda_domain_indices",
]
