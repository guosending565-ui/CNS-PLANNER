"""V3-A strategic planning experiments: read-only w.r.t. every existing result.

This service is deliberately outside the operational route path:

* it never changes ``algorithm_selection``;
* it never writes ``operational_routes`` or ``result_statuses["routes"]``;
* it never writes ``spatial_3d``/``route_altitude_profiles`` (V3 altitude lives in
  its own contract);
* it stores everything in the additive ``route_planner_v3_experiments`` container.

V3-A ships **no** real-data adapter: the canonical ``V3CellEnvironment`` is built
from an explicit synthetic specification so the 3D + heading search and the
hard-constraint kernel can be exercised deterministically.  Real terrain /
building / airspace readiness is reported separately and stays blocked until an
audited adapter exists.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json

from ..algorithms.grid.service import WorkspaceGridService
from ..route_planner_v3 import V3StrategicPlanner
from ..route_planner_v3.contracts import (
    V3_RESULT_STATUSES, default_v3_cost_model, normalize_aircraft_motion_limits,
    normalize_v3_experiments, normalize_v3_planning_policy,
    normalize_v3_planning_problem,
)
from ..route_planner_v3.motion import kinematic_readiness
from ..route_planner_v3.readiness import readiness_overall
from ..route_planner_v3.synthetic import (
    BUILDING_PROFILES, TERRAIN_PROFILES, build_synthetic_environment,
    normalize_synthetic_spec,
)

V3_COLLECTION_ID = "route-planner-v3-experiments"
V3_ENVIRONMENT_SOURCES = ("canonical_synthetic",)
MAX_V3_EXPERIMENTS = 12
V3_EXPERIMENT_NOTE = (
    "V3-A 战略规划实验 ≠ operational route：只写入 route_planner_v3_experiments，"
    "不切换 algorithm_selection，也不覆盖 operational_routes、spatial_3d 或 V1/V2 结果。"
)
V3_REAL_DATA_ADAPTER_STATUS = "not_implemented_v3a"
V3_REAL_DATA_ADAPTER_REASON = (
    "V3-A 不提供真实 terrain/building adapter，也不做 30 m 细化；"
    "真实数据必须先在 V3-B/C 阶段转换为 canonical V3CellEnvironment 后才允许进入搜索。"
)
V3_ARCHITECTURE_SUMMARY = (
    "V3 原生 3D 战略规划：state = grid_id + altitude_index + heading_bin，"
    "hard constraints（allowed/restricted airspace、terrain clearance、building clearance、"
    "altitude bounds、turn/climb/descent capability）进入 edge 生成与验证；"
    "soft cost 输出 distance/population_risk/traffic_risk/building_exposure/energy 向量；"
    "L8 战略搜索 → corridor → （V3-B）30 m 局部精化 → （V3-B）exact polygon/terrain 最终判定；"
    "V3 第一阶段 CNS 不进搜索，Route Planning → CNS Assessment。"
)


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(value):
    return sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


class RoutePlannerV3ExperimentService:
    def __init__(self, session, planner, invalidation, snapshot, grid_service=None):
        self.session = session
        self.planner = planner or V3StrategicPlanner()
        self.invalidation = invalidation
        self.snapshot = snapshot
        self.grid_service = grid_service or WorkspaceGridService()

    # ------------------------------------------------------------------ queries

    def result_snapshot(self):
        collection = deepcopy(self.session.state.get("route_planner_v3_experiments") or {})
        records = list(collection.get("records") or [])
        active_id = collection.get("active_experiment_id")
        active = next((item for item in records if item.get("experiment_id") == active_id), None)
        if active is None and records:
            active = records[0]
        return {
            "status": "passed" if records else "not_calculated",
            "collection_id": V3_COLLECTION_ID,
            "schema_version": 1,
            "count": len(records),
            "active_experiment_id": (active or {}).get("experiment_id"),
            "active_experiment": active,
            "records": records,
            "architecture": V3_ARCHITECTURE_SUMMARY,
            "note": V3_EXPERIMENT_NOTE,
            "operational_routes_untouched": True,
            "algorithm_selection_untouched": True,
            "disclaimer": "V3-A 结果只允许 status=strategic_candidate/failed/missing_data/pending_confirmation。",
            "allowed_result_statuses": list(V3_RESULT_STATUSES),
        }

    def policy_snapshot(self):
        return deepcopy(self.session.state.get("v3_planning_policy") or normalize_v3_planning_policy(None))

    def readiness_snapshot(self):
        """Readiness of the *current project* for V3-A, plus the real-data verdict.

        The domain-level airspace/terrain/building readiness is only meaningful
        against a canonical environment.  V3-A has no real adapter, so this
        snapshot reports the policy/aircraft readiness that *is* project state and
        states explicitly that environment readiness is evaluated per synthetic
        run instead of being faked here.
        """

        state = self.session.state
        workspace = state.get("workspace") or {}
        workspace_ready = bool(workspace and workspace.get("status") == "passed")
        grid = state.get("grid") or {}
        grid_ready = bool(grid.get("status") == "passed" and grid.get("cells"))
        level = grid.get("level")
        recorded_policy = self.policy_snapshot()
        generated = None
        if workspace_ready and (not grid_ready or int(level or 0) != 8):
            generated = self.grid_service.generate(list(workspace["bbox"]), 8)
            grid_for_problem = generated
        else:
            grid_for_problem = grid
        kinematics = kinematic_readiness(recorded_policy, self._aircraft_limits(recorded_policy))
        policy_missing = list(recorded_policy.get("missing_parameters") or [])
        return {
            "status": "passed" if workspace_ready else "missing_data",
            "architecture": V3_ARCHITECTURE_SUMMARY,
            "stage": "V3-A",
            "stage_scope": {
                "implemented": [
                    "confirmation_contracts", "hard_constraint_kernel",
                    "3d_heading_state_space", "l8_strategic_search",
                    "soft_cost_vector", "refinement_corridor_proposal",
                ],
                "not_implemented": [
                    "30m_local_refinement", "exact_polygon_terrain_final_validation",
                    "cns_joint_optimization", "energy_model", "real_data_adapter",
                ],
            },
            "grid": {
                "status": "ready" if grid_ready and int(level or 0) == 8 else (
                    "regenerable_at_l8" if generated else "missing_data"
                ),
                "level": level,
                "cell_count": len(grid.get("cells") or []),
                "l8_cell_count": len((grid_for_problem or {}).get("cells") or []),
                "reason": None if grid_ready and int(level or 0) == 8 else (
                    "当前网格不是 L8：实验会按工作区 bbox 临时生成 L8 战略网格，不写入项目网格"
                    if generated else "请先保存工作区"
                ),
            },
            "algorithm": {
                "algorithm_id": self.planner.algorithm_id,
                "algorithm_version": self.planner.algorithm_version,
                "model_scope": self.planner.model_scope,
                "registered_in_algorithm_registry": False,
                "default_route_planner": "route_planner_v1",
                "semantics": "additive_experimental_container_not_the_default_planner",
            },
            "policy": recorded_policy,
            "policy_readiness": {
                "status": "blocked" if policy_missing else ("ready" if recorded_policy.get("confirmed") else "pending"),
                "missing_parameters": policy_missing,
                "confirmed": bool(recorded_policy.get("confirmed")),
                "source": recorded_policy.get("source"),
                "reasons": (
                    ["缺少必需安全参数：" + ", ".join(policy_missing)] if policy_missing
                    else [] if recorded_policy.get("confirmed")
                    else ["policy 未经项目工程依据确认（confirmed=false）"]
                ),
                "semantics": "no_safety_parameter_has_a_default_value",
            },
            "aircraft_readiness": {
                "status": "ready" if (
                    kinematics["resolved"] and recorded_policy.get("aircraft_min_turn_radius_m") is not None
                ) else "blocked",
                "min_turn_radius_m": recorded_policy.get("aircraft_min_turn_radius_m"),
                "turn_radius_source": (
                    "policy.aircraft_min_turn_radius_m"
                    if recorded_policy.get("aircraft_min_turn_radius_m") is not None
                    else "unresolved_no_turn_radius_is_ever_guessed"
                ),
                "reasons": (
                    [] if kinematics["resolved"] and recorded_policy.get("aircraft_min_turn_radius_m") is not None
                    else ["climb/descent 能力或最小转弯半径未解析；只有 rate 而没有 explicit "
                          "planning_speed_mps 时禁止换算"]
                ),
                **kinematics,
            },
            "environment_readiness": {
                "status": "pending" if "canonical_synthetic" in V3_ENVIRONMENT_SOURCES else "blocked",
                "reason": (
                    "真实数据 adapter 未实现；readiness 在每次实验时对 canonical synthetic 环境逐格评估"
                ),
                "domains_evaluated": ["airspace", "terrain", "building", "policy", "aircraft", "cost_model"],
                "evaluated_in": "run_result.readiness",
            },
            "synthetic_environment_options": {
                "terrain_profiles": list(TERRAIN_PROFILES),
                "buildings_profiles": list(BUILDING_PROFILES),
                "sources": list(V3_ENVIRONMENT_SOURCES),
            },
            "real_data_readiness": self.real_data_readiness(),
            "note": V3_EXPERIMENT_NOTE,
        }

    def real_data_readiness(self):
        """Explicit verdict that the real-data adapter does not exist yet."""

        state = self.session.state
        airspace = ((state.get("grid_attributes") or {}).get("airspace") or {})
        eligibility = airspace.get("airspace_eligibility") or {}
        profiles = state.get("data_source_profiles") or {}
        terrain_profile = profiles.get("terrain") or {}
        return {
            "status": "blocked",
            "adapter_status": V3_REAL_DATA_ADAPTER_STATUS,
            "reason": V3_REAL_DATA_ADAPTER_REASON,
            "terrain_source_declared": bool(terrain_profile.get("source_id") or terrain_profile.get("name")),
            "terrain_vertical_reference": terrain_profile.get("vertical_reference"),
            "confirmed_allowed_grid_cells": len(eligibility.get("allowed_grid_ids") or []),
            "airspace_eligibility_status": eligibility.get("status"),
            "building_grid_source": bool((profiles.get("building_grid") or {}).get("name")),
            "required_before_real_run": [
                "canonical V3CellEnvironment adapter (terrain surface floor, building required "
                "clearance, confirmed airspace classification) with audited provenance",
                "explicit confirmed V3 planning policy for the operation",
                "V3-B 30 m refinement and exact polygon/terrain final validation",
            ],
        }

    # ------------------------------------------------------------------ policy

    def set_policy(self, payload):
        policy = normalize_v3_planning_policy(payload)
        self.session.state["v3_planning_policy"] = policy
        self.session.save()
        return self.readiness_snapshot()

    # ------------------------------------------------------------------ evaluate

    def evaluate(self, payload=None):
        payload = payload if isinstance(payload, dict) else {}
        state = self.session.state
        workspace = state.get("workspace") or {}
        if not workspace or workspace.get("status") != "passed":
            raise ValueError("请先保存工作区，再运行 V3 战略规划实验")
        source = str(payload.get("environment_source") or "canonical_synthetic")
        if source not in V3_ENVIRONMENT_SOURCES:
            raise ValueError(
                "V3-A 只支持 canonical_synthetic 环境；真实数据 adapter 在 V3-B/C 之前不可用"
            )
        policy = self._policy(payload)
        if policy.get("status") != "confirmed" and not payload.get("allow_unconfirmed_policy"):
            raise ValueError(
                "V3 policy 未确认：缺少显式安全参数或 confirmed=false。"
                "V3-A 禁止猜默认安全值；请先保存已确认 policy。"
            )
        spec = normalize_synthetic_spec(payload.get("synthetic_spec"))
        grid = self._l8_grid()
        environment = build_synthetic_environment(
            grid.get("cells") or [], policy, spec,
            source_detail={"requested_by": "route_planner_v3_experiment_service"},
        )
        start, goal = self._endpoints(payload, environment)
        problem = normalize_v3_planning_problem({
            "problem_id": str(payload.get("problem_id") or "v3-experiment"),
            "route_id": str(payload.get("route_id") or ""),
            "start": start,
            "goal": goal,
            "policy": policy,
            "aircraft_motion_limits": self._aircraft_limits(policy),
            "environment": environment,
            "provenance": {
                "environment_source": source,
                "synthetic_spec": spec,
                "corridor_ring_n": payload.get("corridor_ring_n") or 0,
                "refinement_cell_size_m": payload.get("refinement_cell_size_m"),
                "corridor_altitude_margin_m": payload.get("corridor_altitude_margin_m"),
                "grid_level": grid.get("level"),
            },
        })
        result = self.planner.plan(problem)
        record = self._record(payload, policy, spec, environment, problem, result)
        existing = (state.get("route_planner_v3_experiments") or {}).get("records") or []
        retained = [item for item in existing if item.get("experiment_id") != record["experiment_id"]]
        state["route_planner_v3_experiments"] = normalize_v3_experiments({
            "records": [record, *retained],
            "active_experiment_id": record["experiment_id"],
        })
        # V3-A writes only its own container: no operational route, no algorithm
        # selection, no spatial_3d and therefore no V1/V2/P7-P19 invalidation.
        self.session.save()
        return self.snapshot()

    def delete_experiment(self, experiment_id):
        state = self.session.state
        collection = state.get("route_planner_v3_experiments") or {}
        records = collection.get("records") or []
        retained = [item for item in records if item.get("experiment_id") != experiment_id]
        if len(retained) == len(records):
            raise ValueError("V3 实验记录不存在")
        active = collection.get("active_experiment_id")
        if active == experiment_id:
            active = retained[0]["experiment_id"] if retained else None
        state["route_planner_v3_experiments"] = normalize_v3_experiments({
            "records": retained, "active_experiment_id": active,
        })
        self.session.save()
        return self.snapshot()

    # ------------------------------------------------------------------ internals

    def _policy(self, payload):
        """Effective policy for one run.

        A payload policy is a *complete* safety parameter set for that run; the
        stored policy only supplies the two provenance fields a payload usually
        omits (``source`` and ``confirmed``) when the payload does not state them.
        Safety parameters are never merged across the two, and never defaulted.
        """

        recorded = self.session.state.get("v3_planning_policy") or {}
        supplied = payload.get("policy")
        if supplied is None:
            return normalize_v3_planning_policy(recorded)
        merged = dict(supplied)
        if merged.get("source") in (None, "") and recorded.get("source"):
            merged["source"] = recorded["source"]
        if "confirmed" not in merged and recorded.get("confirmed"):
            merged["confirmed"] = True
        return normalize_v3_planning_policy(merged)

    def _aircraft_limits(self, policy):
        """Motion limits resolved only from explicitly supplied policy values."""

        return normalize_aircraft_motion_limits({
            "aircraft_id": str(policy.get("policy_id") or ""),
            "max_climb_gradient": policy.get("max_climb_gradient"),
            "max_descent_gradient": policy.get("max_descent_gradient"),
            "max_climb_rate_mps": policy.get("max_climb_rate_mps"),
            "max_descent_rate_mps": policy.get("max_descent_rate_mps"),
            "planning_speed_mps": policy.get("planning_speed_mps"),
            "min_turn_radius_m": policy.get("aircraft_min_turn_radius_m"),
            "confirmed": bool(policy.get("confirmed")),
            "source": policy.get("source"),
        })

    def _l8_grid(self):
        state = self.session.state
        grid = state.get("grid") or {}
        if grid.get("status") == "passed" and grid.get("cells") and int(grid.get("level") or 0) == 8:
            return grid
        workspace = state.get("workspace") or {}
        generated = self.grid_service.generate(list(workspace["bbox"]), 8)
        # The generated strategic grid is used for this experiment only; the
        # project's own grid is never replaced by this service.
        return generated

    def _endpoints(self, payload, environment):
        cells = environment.get("cells") or []
        route = self._scenario_route(payload)
        if route:
            start = route.get("start") or route.get("path", [None, None])[0]
            goal = route.get("end") or route.get("path", [None, None])[-1]
            if _point(start) and _point(goal):
                return list(start[:2]), list(goal[:2])
        explicit_start, explicit_goal = payload.get("start"), payload.get("goal")
        if _point(explicit_start) and _point(explicit_goal):
            return list(explicit_start[:2]), list(explicit_goal[:2])
        ordered = sorted(
            cells,
            key=lambda cell: (
                cell.get("row") if cell.get("row") is not None else 0,
                cell.get("column") if cell.get("column") is not None else 0,
                str(cell["grid_id"]),
            ),
        )
        if not ordered:
            raise ValueError("没有可用于 V3 实验的 canonical cell")
        return list(ordered[0]["center"]), list(ordered[-1]["center"])

    def _scenario_route(self, payload):
        state = self.session.state
        route_id = str(payload.get("route_id") or "")
        routes = state.get("scenario_routes") or []
        if route_id:
            for route in routes:
                if str(route.get("route_id")) == route_id:
                    return route
            for route in state.get("operational_routes") or []:
                if str(route.get("route_id")) == route_id:
                    return route
            return None
        return routes[0] if routes else None

    def _record(self, payload, policy, spec, environment, problem, result):
        grid = self.session.state.get("grid") or {}
        identity = _hash({
            "grid": {
                "level": problem["provenance"].get("grid_level"),
                "cells": [
                    {"grid_id": cell["grid_id"], "bbox": cell.get("bbox")}
                    for cell in environment.get("cells") or []
                ],
            },
            "policy": policy,
            "synthetic_spec": spec,
            "route_id": problem["route_id"],
            "start": problem["start"],
            "goal": problem["goal"],
            "corridor": {
                "ring_n": problem["provenance"].get("corridor_ring_n"),
                "cell_size_m": problem["provenance"].get("refinement_cell_size_m"),
            },
        })
        return {
            "experiment_id": "V3-" + identity[:12].upper(),
            "created_at": utc_now(),
            "source_type": "synthetic",
            "grounding": "canonical_synthetic_environment",
            "environment_source": "canonical_synthetic",
            "environment_spec": deepcopy(spec),
            "environment_fingerprint": _hash(environment),
            "policy": deepcopy(policy),
            "policy_fingerprint": _hash(policy),
            "route_id": problem["route_id"] or None,
            "problem_fingerprint": result.get("input_fingerprint"),
            "result": deepcopy(result),
            "readiness": deepcopy(result.get("readiness")),
            "readiness_overall": readiness_overall(result.get("readiness") or {}),
            "current_applicability": "current",
            "provenance": {
                "recorded_at": utc_now(),
                "planner": {
                    "algorithm_id": self.planner.algorithm_id,
                    "algorithm_version": self.planner.algorithm_version,
                    "model_scope": self.planner.model_scope,
                    "motion_model_id": (result.get("semantics") or {}).get("motion_model_id"),
                },
                "project_grid_level": grid.get("level"),
                "operational_routes_untouched": True,
                "algorithm_selection_untouched": True,
                "spatial_3d_untouched": True,
                "environment_semantics": "canonical V3CellEnvironment built from an explicit synthetic spec; not real data",
                "not_final_validation": True,
            },
            "verdicts": {
                "operational_route": False,
                "final_validation_performed": False,
                "automatic_ranking": False,
                "automatically_scored": False,
            },
            "note": V3_EXPERIMENT_NOTE,
        }


def _point(value):
    return (
        isinstance(value, (list, tuple)) and len(value) >= 2
        and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value[:2])
    )


def record_summary(record):
    """Small read-only projection used by the Step 03 panel (kept next to the contract)."""

    if not isinstance(record, dict):
        return None
    result = record.get("result") or {}
    corridor = result.get("candidate_refinement_corridor") or {}
    return {
        "experiment_id": record.get("experiment_id"),
        "created_at": record.get("created_at"),
        "status": result.get("status"),
        "readiness_overall": record.get("readiness_overall"),
        "distance_m": result.get("distance_m"),
        "state_count": len(result.get("state_path") or []),
        "expanded_states": (result.get("search_statistics") or {}).get("expanded_states"),
        "runtime_ms": (result.get("search_statistics") or {}).get("runtime_ms"),
        "scalar_cost": (result.get("cost_vector") or {}).get("scalar_cost"),
        "corridor_center_count": len(corridor.get("center_grid_ids") or []),
        "corridor_support_count": len(corridor.get("support_grid_ids") or []),
        "corridor_ring_n": corridor.get("ring_n"),
    }


__all__ = [
    "V3_COLLECTION_ID", "V3_ENVIRONMENT_SOURCES", "MAX_V3_EXPERIMENTS",
    "V3_EXPERIMENT_NOTE", "V3_ARCHITECTURE_SUMMARY", "V3_REAL_DATA_ADAPTER_STATUS",
    "V3_REAL_DATA_ADAPTER_REASON", "RoutePlannerV3ExperimentService", "record_summary",
    "utc_now", "default_v3_cost_model",
]
