"""Use cases for the layered risk-aware route planner (Theta* V2 default, V1 legacy).

Owns exactly three additive products:

* ``layered_route_planning_request`` — the explicit scenario/OD + AltitudeLayer request;
* ``layered_route_feasibility_policy`` / ``layered_route_cost_policy`` — the explicit
  engineering policies (no default clearance and no default lambda);
* ``layered_route_candidates`` — the independent candidate container (candidates +
  per-(route, layer) feasibility masks).

The service is additive by construction: it never writes ``operational_routes``,
``algorithm_selection``, ``spatial_3d``, ``route_operating_layers``, ``grid_risk_v2`` or any
CNS result, and it never switches the project's default route planner.
"""

from __future__ import annotations

from copy import deepcopy

from ..domain.layered_route import (
    COST_DOMAIN_IDS, active_cost_domains, cost_lambdas, cost_policy_fingerprint,
    cost_policy_is_runnable, default_layered_route_candidate,
    feasibility_policy_fingerprint, feasibility_policy_is_runnable,
    normalize_layered_route_candidate_collection, normalize_layered_route_cost_policy,
    normalize_layered_route_feasibility_policy, normalize_layered_route_request,
    request_fingerprint, resolve_cruise_altitude, stable_fingerprint,
)
from ..domain.communication_planning_field import (
    communication_readiness, normalize_communication_planning_field,
)
from ..domain.layered_theta_v2 import (
    default_risk_density_constraint, default_theta_v2_objective_policy,
    normalize_risk_density_constraint, normalize_theta_v2_objective_policy,
    theta_v2_search_parameter_view,
)
from ..domain.population_shelter import (
    default_shelter_coefficient_policy, normalize_population_shelter_attribute,
    normalize_shelter_coefficient_policy, population_shelter_fingerprint,
    resolve_population_shelter, shelter_policy_fingerprint, user_defined_baseline_policy,
)
from ..domain.regulatory_constraints import (
    default_regulatory_constraints, is_configured as regulatory_is_configured,
    normalize_regulatory_constraints, regulatory_compliance_record,
)

from ..algorithms.grid.service import WorkspaceGridService
from ..data.mapping.buildings import BuildingGridService
from ..layered_route_planner.planner import (
    ALGORITHM_ID, ALGORITHM_VERSION, PLANNER_CAPABILITY, LayeredRoutePlannerV1,
    build_layer_feasibility_mask,
)
from ..layered_route_planner.theta_star_v2 import (
    POPULATION_FACTOR_ID, grid_bearing_deg,
)
from ..risk.accessors_v2 import cell_factor_index

POLICY_SEMANTICS = {
    "no_default_clearance_or_lambda": True,
    "null_is_not_zero": True,
    "explicit_zero_is_legal": True,
    "confirmed_requires_explicit_source": True,
    "altitude_layer_must_be_explicitly_selected": True,
    "airspace_is_display_only": True,
}

REQUEST_SEMANTICS = {
    "altitude_layer_is_explicit": True,
    "never_inferred_from_route_altitude_profile": True,
    "never_inferred_from_route_operating_layer": True,
    "route_operating_layer_is_an_operational_contract_not_a_planning_input": True,
}


def _route_ids(state):
    return [str(item.get("route_id")) for item in state.get("operational_routes") or []]


def _scenario_routes(state):
    return list(state.get("scenario_routes") or [])


def _candidate_key(route_id, altitude_layer_id):
    return f"{route_id or 'od'}@{altitude_layer_id or 'layer'}"


def _shelter_input_fingerprint(grid, policy, grid_risk_v2):
    """Fingerprint of everything the derived per-grid shelter field depends on.

    Population source/normalization, the grid identity and the confirmed shelter policy —
    so a change in any of them rebuilds the field instead of silently reusing it.
    """

    return stable_fingerprint({
        "grid_level": (grid or {}).get("level"),
        "grid_cells": sorted(
            str(cell.get("grid_id")) for cell in (grid or {}).get("cells") or []
            if isinstance(cell, dict)
        ),
        "grid_risk_v2_input_fingerprint": (grid_risk_v2 or {}).get("input_fingerprint"),
        "grid_risk_v2_policy_fingerprint": (grid_risk_v2 or {}).get("policy_fingerprint"),
        "shelter_policy_fingerprint": shelter_policy_fingerprint(policy),
    }, prefix="shelterinputv1-")


def _fingerprint_planner(planner):
    """A planner that exposes the declared dependency fingerprint (registry seam).

    A different registered ``layered_route_planner`` implementation may not expose
    ``fingerprints``; the canonical V1 fingerprint definition is then used, so the staleness
    semantics stay identical instead of silently degrading.
    """

    return planner if callable(getattr(planner, "fingerprints", None)) else LayeredRoutePlannerV1()


def _existing_candidate(collection, key, request):
    """The most recent record for one (route, layer) lane, if this run repeats its inputs."""

    lane = [
        item for item in collection.get("items") or []
        if item.get("lane_key") == key
    ]
    candidate = lane[-1] if lane else None
    return {
        "candidate": candidate,
        "fingerprint": (candidate or {}).get("candidate_fingerprint"),
        "request_fingerprint": (candidate or {}).get("request_fingerprint"),
        "request_fingerprint_expected": request_fingerprint(request),
    }


class LayeredRoutePlannerService:
    def __init__(self, session, invalidation, snapshot, adapter=None, source_status=None):
        self.session = session
        self.invalidation = invalidation
        self.snapshot = snapshot
        #: Optional GIS boundary (needs QGIS/GDAL).  Without it the feasibility mask is
        #: reported blocked instead of being fabricated.
        self.adapter = adapter
        self.source_status = source_status
        #: Bound to the registry-selected ``layered_route_planner`` algorithm.  The default
        #: is now the production Theta* V2 baseline; a project that explicitly saved V1 keeps
        #: running V1.  The project's ``route_planner`` selection is untouched either way.
        self.planner = LayeredRoutePlannerV1()
        #: Optional read-only communication field provider (interface only in this round).
        self.communication_provider = None
        self.ensure_state()

    # ------------------------------------------------------------------ state

    def ensure_state(self):
        state = self.session.state
        state["layered_route_planning_request"] = normalize_layered_route_request(
            state.get("layered_route_planning_request")
        )
        state["layered_route_feasibility_policy"] = normalize_layered_route_feasibility_policy(
            state.get("layered_route_feasibility_policy")
        )
        state["layered_route_cost_policy"] = normalize_layered_route_cost_policy(
            state.get("layered_route_cost_policy")
        )
        state["layered_route_candidates"] = normalize_layered_route_candidate_collection(
            state.get("layered_route_candidates")
        )
        # Additive Theta* V2 inputs.  Each ships *not configured* rather than with an assumed
        # value, except the shelter policy: the user explicitly confirmed the 1.0 baseline,
        # and that value is stored as real per-grid data in ``grid_attributes``.
        state.setdefault(
            "shelter_coefficient_policy", user_defined_baseline_policy()
        )
        state["shelter_coefficient_policy"] = normalize_shelter_coefficient_policy(
            state.get("shelter_coefficient_policy")
        )
        # The per-grid ``population_shelter`` field is derived data: it is rebuilt from the
        # canonical population factor plus this policy on demand and is deliberately kept out
        # of the persisted ``grid_attributes`` so it can never drift from its inputs.
        state["grid_attributes"].pop("population_shelter", None)
        state.setdefault("regulatory_constraints", default_regulatory_constraints())
        state["regulatory_constraints"] = normalize_regulatory_constraints(
            state.get("regulatory_constraints")
        )
        state.setdefault(
            "communication_planning_field", normalize_communication_planning_field(None)
        )
        state["communication_planning_field"] = normalize_communication_planning_field(
            state.get("communication_planning_field")
        )
        state.setdefault("theta_v2_objective_policy", default_theta_v2_objective_policy())
        state["theta_v2_objective_policy"] = normalize_theta_v2_objective_policy(
            state.get("theta_v2_objective_policy")
        )
        state.setdefault("max_route_risk_density", default_risk_density_constraint())
        state["max_route_risk_density"] = normalize_risk_density_constraint(
            state.get("max_route_risk_density")
        )
        state.setdefault("result_statuses", {}).setdefault("layered_route_candidate", "not_calculated")
        return state

    # ------------------------------------------------------------------ snapshots

    def request_snapshot(self):
        self.ensure_state()
        return deepcopy(self.session.state["layered_route_planning_request"])

    def feasibility_policy_snapshot(self):
        self.ensure_state()
        return deepcopy(self.session.state["layered_route_feasibility_policy"])

    def cost_policy_snapshot(self):
        self.ensure_state()
        return deepcopy(self.session.state["layered_route_cost_policy"])

    def result_snapshot(self):
        """Read-only projection: the persisted state never carries a derived applicability."""

        state = self.ensure_state()
        collection = normalize_layered_route_candidate_collection(
            state.get("layered_route_candidates")
        )
        current_key, current_fingerprint = self._current_identity()
        projected = []
        for item in collection["items"]:
            entry = deepcopy(item)
            entry["current_applicability"] = self._applicability(
                item, current_key, current_fingerprint,
            )
            projected.append(entry)
        collection["items"] = projected
        collection["masks"] = {
            key: {
                **deepcopy(mask),
                "current_applicability": (
                    "current" if key == current_key and mask.get("status") != "stale" else "stale"
                ),
            }
            for key, mask in (collection.get("masks") or {}).items()
        }
        collection["current_key"] = current_key
        collection["current_candidate_fingerprint"] = current_fingerprint
        return collection

    def mask_snapshot(self, route_id=None, altitude_layer_id=None):
        collection = self.result_snapshot()
        return deepcopy((collection.get("masks") or {}).get(
            _candidate_key(route_id, altitude_layer_id)
        ))

    # ------------------------------------------------------------------ readiness

    @staticmethod
    def _workspace_grid_capability():
        """工作区网格的**软件基线**能力声明（只读，不改变任何工作区行为）。

        这里报告的是 ``WorkspaceGridService`` 的默认参数（L7 / max_cells 上限），不是当前
        项目网格的实际层级；项目实际层级始终以 ``state["grid"]["level"]`` 为准。
        """

        service = WorkspaceGridService()
        capability = service.capabilities()
        capability["capability_scope"] = "software_baseline_defaults_not_project_grid"
        capability["project_grid_level_authority"] = "project_state.grid.level"
        return capability

    def readiness_snapshot(self):
        state = self.ensure_state()
        request = state["layered_route_planning_request"]
        feasibility = state["layered_route_feasibility_policy"]
        cost = state["layered_route_cost_policy"]
        scenario_routes = _scenario_routes(state)
        layers = ((state.get("spatial_3d") or {}).get("altitude_layers") or [])
        route = self._resolve_scenario_route(request, scenario_routes)
        layer_ids = [str(item.get("altitude_layer_id")) for item in layers]
        selected_layer = next(
            (item for item in layers if str(item.get("altitude_layer_id")) == request.get("altitude_layer_id")),
            None,
        )
        cruise = resolve_cruise_altitude(selected_layer)
        source_status = self._source_status()
        collection = normalize_layered_route_candidate_collection(
            state.get("layered_route_candidates")
        )
        mask = (collection.get("masks") or {}).get(
            _candidate_key((route or {}).get("route_id"), request.get("altitude_layer_id"))
        )

        blockers = []
        if request.get("status") != "confirmed":
            blockers.append({
                "reason_code": request.get("status_reason") or "planning_request_not_confirmed",
                "reason": "尚未确认显式规划请求（scenario/OD + 显式 AltitudeLayer）",
            })
        if selected_layer is None:
            blockers.append({
                "reason_code": "altitude_layer_not_found",
                "reason": f"selected AltitudeLayer 不存在：{request.get('altitude_layer_id')}",
            })
        elif cruise.get("status") != "confirmed":
            blockers.append({
                "reason_code": str(cruise.get("reason") or "altitude_layer_not_resolvable"),
                "reason": (
                    "selected AltitudeLayer 无法解析为 canonical EGM2008 巡航高度："
                    f"{cruise.get('reason')}；V1 不做 datum/geoid 猜测或伪转换"
                ),
            })
        runnable, reason = feasibility_policy_is_runnable(feasibility)
        if not runnable:
            blockers.append({
                "reason_code": reason,
                "reason": "terrain_vertical_clearance_m 未确认（无默认值）",
            })
        runnable, reason = cost_policy_is_runnable(cost)
        if not runnable:
            blockers.append({
                "reason_code": reason,
                "reason": "LayeredRouteCostPolicy 的 λ 未全部显式确认（null != 0）",
            })
        terrain_status = (source_status or {}).get("terrain") or {}
        if not terrain_status.get("available"):
            blockers.append({
                "reason_code": "terrain_source_unavailable",
                "reason": f"verified FABDEM terrain source 不可用：{terrain_status.get('reason')}",
            })
        building_policy = state.get("building_clearance_policy") or {}
        if str(building_policy.get("status") or "") != "confirmed":
            blockers.append({
                "reason_code": "building_clearance_policy_not_confirmed",
                "reason": "既有 building_clearance_policy 未确认：不提供第二套建筑净空定义",
            })
        if not scenario_routes:
            blockers.append({
                "reason_code": "scenario_route_not_available",
                "reason": "当前项目没有 scenario/OD 航路",
            })
        if route is None and request.get("status") == "confirmed":
            blockers.append({
                "reason_code": "scenario_route_not_found",
                "reason": "planning request 指向的 scenario/OD 航路不存在",
            })
        if not (state.get("grid") or {}).get("cells"):
            blockers.append({
                "reason_code": "grid_unavailable",
                "reason": "当前项目没有 MH/T 标准网格",
            })

        return {
            "status": "ready" if not blockers else "blocked",
            # The *effective* planner bound to this project's ``layered_route_planner``
            # selection.  The frontend selects its panel from exactly this algorithm_id, so a
            # project running Theta* V2 must never be reported as the legacy V1 baseline.
            "algorithm": {
                "algorithm_id": getattr(self.planner, "algorithm_id", ALGORITHM_ID),
                "algorithm_version": getattr(
                    self.planner, "algorithm_version", ALGORITHM_VERSION
                ),
                "uses_theta_star": self._uses_theta_star(),
                "role": (
                    "production_layered_planner" if self._uses_theta_star()
                    else "legacy_baseline_layered_planner"
                ),
            },
            "request": deepcopy(request),
            "request_semantics": deepcopy(REQUEST_SEMANTICS),
            # Capability declaration (Phase 3.5, additive): which horizontal grid levels this
            # planner is declared to work on, which one it prefers, and the known gap between
            # the workspace default and the only level the L8 building facts can be mapped to.
            # Declaring this changes no search behaviour.
            "capabilities": {
                "planner": deepcopy(PLANNER_CAPABILITY),
                "building_grid": deepcopy(BuildingGridService.capabilities(
                    declared_level=(state.get("grid") or {}).get("level"),
                )),
                "workspace_grid": self._workspace_grid_capability(),
            },
            "altitude_layer_catalog": {
                "status": "configured" if layers else "not_configured",
                "count": len(layers),
                "altitude_layer_ids": layer_ids,
                "selected_altitude_layer_id": request.get("altitude_layer_id"),
                "cruise_altitude": cruise,
            },
            "scenario_route": {
                "status": "resolved" if route else "not_resolved",
                "route_id": (route or {}).get("route_id"),
                "count": len(scenario_routes),
            },
            "feasibility_policy": {
                "status": feasibility.get("status"),
                "fingerprint": feasibility_policy_fingerprint(feasibility),
                "terrain_vertical_clearance_m": feasibility.get("terrain_vertical_clearance_m"),
                "parameter_status": feasibility.get("parameter_status"),
                "source": feasibility.get("source"),
            },
            "cost_policy": {
                "status": cost.get("status"),
                "fingerprint": cost_policy_fingerprint(cost),
                "lambdas": cost_lambdas(cost),
                "active_domains": list(active_cost_domains(cost)),
                "parameter_status": cost.get("parameter_status"),
                "source": cost.get("source"),
                "domains": {
                    domain_id: {
                        "lambda": cost_lambdas(cost)[domain_id],
                        "enabled": domain_id in active_cost_domains(cost),
                        "configured": cost_lambdas(cost)[domain_id] is not None,
                    }
                    for domain_id in COST_DOMAIN_IDS
                },
            },
            "risk_framework_v2": self._risk_readiness(state),
            "theta_star_v2": self._theta_v2_readiness(state, request, cruise),
            "building_clearance_policy": {
                "status": building_policy.get("status"),
                "vertical_clearance_m": building_policy.get("vertical_clearance_m"),
                "horizontal_clearance_m": building_policy.get("horizontal_clearance_m"),
                "source": building_policy.get("source"),
                "reused_not_redefined": True,
            },
            "sources": deepcopy(source_status or {}),
            "feasibility_mask": {
                "status": (mask or {}).get("status", "not_calculated"),
                # selected-layer mask 必须能自报属于哪一层：缺了它前端只能显示 "—"，
                # 用户无法判断「selected layer mask」是哪一层的 mask。
                "altitude_layer_id": (mask or {}).get("altitude_layer_id"),
                "counts": deepcopy((mask or {}).get("counts") or {}),
                "mask_fingerprint": (mask or {}).get("mask_fingerprint"),
                "current_applicability": (mask or {}).get("current_applicability"),
            },
            "blockers": blockers,
            "semantics": deepcopy(POLICY_SEMANTICS),
            "airspace": {
                "status": "not_applicable", "applicability": "display_only",
                "used_in_mask_search_or_fingerprint": False,
            },
            "notes": [
                "高度层必须显式选择；不从 profile 或 RouteOperatingLayer 推断。",
                "没有 confirmed clearance / λ 时一律 blocked，不提供任何默认高度、净空或权重。",
                "candidate 不是 operational route，也不自动创建 RouteOperatingLayer。",
            ],
        }

    # ------------------------------------------------------------------ writes

    def set_planning_request(self, payload):
        raw = payload.get("layered_route_planning_request", payload) if isinstance(payload, dict) else payload
        candidate = normalize_layered_route_request(raw)
        if candidate != self.session.state["layered_route_planning_request"]:
            self.session.state["layered_route_planning_request"] = candidate
            self.invalidation.layered_route("layered_route_planning_request_changed")
            self.session.save()
        return self.snapshot()

    def set_feasibility_policy(self, payload):
        raw = payload.get("layered_route_feasibility_policy", payload) if isinstance(payload, dict) else payload
        candidate = normalize_layered_route_feasibility_policy(raw)
        if candidate != self.session.state["layered_route_feasibility_policy"]:
            self.session.state["layered_route_feasibility_policy"] = candidate
            self.invalidation.layered_route("layered_route_feasibility_policy_changed")
            self.session.save()
        return self.snapshot()

    def set_cost_policy(self, payload):
        raw = payload.get("layered_route_cost_policy", payload) if isinstance(payload, dict) else payload
        candidate = normalize_layered_route_cost_policy(raw)
        if candidate != self.session.state["layered_route_cost_policy"]:
            self.session.state["layered_route_cost_policy"] = candidate
            self.invalidation.layered_route("layered_route_cost_policy_changed")
            self.session.save()
        return self.snapshot()

    def delete_candidate(self, candidate_id):
        state = self.ensure_state()
        collection = normalize_layered_route_candidate_collection(state["layered_route_candidates"])
        remaining = [item for item in collection["items"] if item.get("candidate_id") != candidate_id]
        if len(remaining) == len(collection["items"]):
            raise ValueError(f"未找到 layered route candidate：{candidate_id}")
        collection["items"] = remaining
        collection["count"] = len(remaining)
        if collection.get("active_candidate_id") == candidate_id:
            collection["active_candidate_id"] = None
        collection["status"] = "passed" if remaining else "not_calculated"
        state["layered_route_candidates"] = collection
        state.setdefault("result_statuses", {})["layered_route_candidate"] = collection["status"]
        self.invalidation.layered_route_validation("candidate_deleted")
        self.session.save()
        return self.snapshot()

    # ------------------------------------------------------------------ evaluate

    def evaluate(self, payload=None, *, adapter=None):
        state = self.ensure_state()
        payload = payload if isinstance(payload, dict) else {}
        if isinstance(payload.get("request"), dict):
            state["layered_route_planning_request"] = normalize_layered_route_request(
                payload["request"]
            )
        request = state["layered_route_planning_request"]
        feasibility = state["layered_route_feasibility_policy"]
        cost = state["layered_route_cost_policy"]
        grid = state.get("grid") or {}
        scenario_routes = _scenario_routes(state)
        route = self._resolve_scenario_route(request, scenario_routes)
        active_adapter = adapter or self.adapter
        collection = normalize_layered_route_candidate_collection(state["layered_route_candidates"])
        key = _candidate_key((route or {}).get("route_id"), request.get("altitude_layer_id"))
        prior = _existing_candidate(collection, key, request)

        runnable, reason = feasibility_policy_is_runnable(feasibility)
        if not runnable:
            return self._blocked(
                state, collection, request, route, "blocked", prior,
                "LayeredRouteFeasibilityPolicy 未确认：terrain_vertical_clearance_m 无默认值",
                reason or "feasibility_policy_not_confirmed", key,
            )
        if self._uses_theta_star():
            # Theta* V2 prices population × shelter risk, not the legacy Risk Framework V2
            # domain lambdas, so it does not require a LayeredRouteCostPolicy at all.
            pass
        else:
            runnable, reason = cost_policy_is_runnable(cost)
            if not runnable:
                return self._blocked(
                    state, collection, request, route, "blocked", prior,
                    "LayeredRouteCostPolicy 未确认：λ 无默认值（null != 0）",
                    reason or "cost_policy_not_confirmed", key,
                )
        if request.get("status") != "confirmed":
            return self._blocked(
                state, collection, request, route, "blocked", prior,
                "显式 planning request 未确认",
                str(request.get("status_reason") or "planning_request_not_confirmed"), key,
            )
        if route is None:
            return self._blocked(
                state, collection, request, route, "missing_data", prior,
                "planning request 指向的 scenario/OD 航路不存在", "scenario_route_not_found", key,
            )
        if not grid.get("cells"):
            return self._blocked(
                state, collection, request, route, "missing_data", prior,
                "当前 MH/T 标准网格不可用", "grid_unavailable", key,
            )
        layers = ((state.get("spatial_3d") or {}).get("altitude_layers") or [])
        selected_layer = next(
            (item for item in layers if str(item.get("altitude_layer_id")) == request.get("altitude_layer_id")),
            None,
        )
        cruise = resolve_cruise_altitude(selected_layer)
        if cruise.get("status") != "confirmed":
            return self._blocked(
                state, collection, request, route, "blocked", prior,
                "selected AltitudeLayer 无法解析为 canonical EGM2008 巡航高度；不做 datum/geoid 猜测",
                str(cruise.get("reason") or "altitude_layer_not_resolvable"), key,
            )
        if active_adapter is None:
            return self._blocked(
                state, collection, request, route, "missing_data", prior,
                "未配置 GIS 可行性数据源（verified FABDEM + L8 building facts）：不构造假环境",
                "feasibility_adapter_not_configured", key,
            )

        try:
            cells = active_adapter.build_cells(list(grid["cells"]), state)
        except (TypeError, ValueError, RuntimeError) as exc:
            return self._blocked(
                state, collection, request, route, "missing_data", prior,
                f"读取 coarse 地形/建筑事实失败：{exc}", "feasibility_source_read_failed", key,
            )
        building_policy = state.get("building_clearance_policy") or {}
        describe = getattr(active_adapter, "describe", None)
        mask = build_layer_feasibility_mask(
            request=request, cruise_altitude=cruise, cells=cells,
            feasibility_policy=feasibility, building_clearance_policy=building_policy,
            source_audits=state.get("source_audits") or {}, grid_level=grid.get("level"),
            adapter=describe() if callable(describe) else None,
        )
        hard_constraints = payload.get("hard_constraints")
        if hard_constraints is None:
            hard_constraints = (state.get("route_constraints") or []) if isinstance(
                state.get("route_constraints"), list
            ) else []
        candidate = self.planner.plan(
            request=request, scenario_route=route, grid=grid, layer_mask=mask,
            grid_risk_v2=state.get("grid_risk_v2") or {},
            feasibility_policy=feasibility, cost_policy=cost,
            hard_constraints=hard_constraints,
            building_clearance_policy=building_policy,
            source_audits=state.get("source_audits") or {},
            **self._planner_specific_inputs(state, grid),
        )
        collection["masks"][key] = mask
        self._store_candidate(collection, key, request, candidate, prior)
        state["layered_route_candidates"] = collection
        state.setdefault("result_statuses", {})["layered_route_candidate"] = collection["status"]
        # Replacing the current candidate invalidates the frozen validation evidence only;
        # the newly created candidate itself remains current.
        self.invalidation.layered_route_validation("candidate_recomputed")
        self.session.save()
        return self.snapshot()

    # ------------------------------------------------------------------ helpers

    def _uses_theta_star(self):
        return bool(getattr(self.planner, "uses_theta_star", False))

    def _search_parameter_view(self):
        """Effective Theta* V2 search parameters + provenance for readiness/audit.

        The V1 legacy baseline has no heading/theta search parameter at all, so it reports
        ``applicable=False`` instead of inventing a value for an algorithm that never reads
        one.
        """

        if not self._uses_theta_star():
            return {
                "applicable": False,
                "algorithm_id": getattr(self.planner, "algorithm_id", ALGORITHM_ID),
                "algorithm_version": getattr(
                    self.planner, "algorithm_version", ALGORITHM_VERSION
                ),
                "reason": "legacy_layered_planner_has_no_theta_search_parameters",
                "engineering_confirmed": False,
            }
        return {
            "applicable": True,
            **theta_v2_search_parameter_view(getattr(self.planner, "search_parameters", None)),
        }

    def _planner_specific_inputs(self, state, grid):
        """The Theta* V2-only planning inputs; empty for the V1 A* baseline.

        Returning an empty mapping for V1 keeps a single call shape while guaranteeing that
        the V1 baseline never sees (and therefore can never be changed by) these inputs.
        """

        if not self._uses_theta_star():
            return {}
        return {
            "population_shelter": self.population_shelter_snapshot(),
            "shelter_policy": self.shelter_policy_snapshot(),
            "regulatory_constraints": state.get("regulatory_constraints")
            or default_regulatory_constraints(),
            "communication_field": self.communication_field_snapshot(),
            "objective_policy": state.get("theta_v2_objective_policy")
            or default_theta_v2_objective_policy(),
            "risk_density_constraint": state.get("max_route_risk_density")
            or default_risk_density_constraint(),
        }

    def shelter_policy_snapshot(self):
        return deepcopy(
            self.ensure_state()["shelter_coefficient_policy"]
        )

    def set_shelter_policy(self, payload):
        raw = payload.get("shelter_coefficient_policy", payload) if isinstance(payload, dict) else payload
        candidate = normalize_shelter_coefficient_policy(raw)
        state = self.ensure_state()
        if candidate != state["shelter_coefficient_policy"]:
            state["shelter_coefficient_policy"] = candidate
            # The per-grid field is derived data: it is rebuilt from the policy below so the
            # stored coefficients can never drift from the confirmed policy.
            state["grid_attributes"]["population_shelter"] = normalize_population_shelter_attribute(None)
            self.invalidation.layered_route("shelter_coefficient_policy_changed")
            self.session.save()
        return self.snapshot()

    def population_shelter_snapshot(self):
        """The per-grid ``population_shelter`` field, rebuilt from current canonical inputs.

        The field is *derived*: it reuses the existing canonical Risk Framework V2
        population factor and the confirmed shelter policy, and it stores a real
        ``shelter_coefficient`` per grid cell that a future confirmed shelter dataset can
        replace wholesale.  Nothing is hardcoded inside the planner.
        """

        state = self.ensure_state()
        grid = state.get("grid") or {}
        policy = state["shelter_coefficient_policy"]
        grid_risk_v2 = state.get("grid_risk_v2") or {}
        attribute = state.get("_population_shelter_cache") or {}
        cells = attribute.get("cells") or {}
        derived_from = attribute.get("derived_from_fingerprint")
        if cells and derived_from == _shelter_input_fingerprint(grid, policy, grid_risk_v2):
            return deepcopy(attribute)
        factors = {}
        for grid_id, cell in (grid_risk_v2.get("cells") or {}).items():
            index, status = cell_factor_index(cell, POPULATION_FACTOR_ID)
            if status == "passed" and index is not None:
                factors[str(grid_id)] = index
        attribute = resolve_population_shelter(
            grid=grid,
            population_attribute=state["grid_attributes"].get("population") or {},
            normalized_population_factors=factors,
            policy=policy,
        )
        attribute["derived_from_fingerprint"] = _shelter_input_fingerprint(
            grid, policy, grid_risk_v2
        )
        state["_population_shelter_cache"] = attribute
        return deepcopy(attribute)

    def set_regulatory_constraints(self, payload):
        raw = payload.get("regulatory_constraints", payload) if isinstance(payload, dict) else payload
        candidate = normalize_regulatory_constraints(raw)
        state = self.ensure_state()
        if candidate != state["regulatory_constraints"]:
            state["regulatory_constraints"] = candidate
            self.invalidation.layered_route("regulatory_constraints_changed")
            self.session.save()
        return self.snapshot()

    def communication_field_snapshot(self):
        provider = self.communication_provider
        if callable(provider):
            try:
                return normalize_communication_planning_field(provider())
            except (TypeError, ValueError, RuntimeError):
                return normalize_communication_planning_field(None)
        state = self.ensure_state()
        return deepcopy(state.get("communication_planning_field"))

    def set_communication_field(self, payload):
        raw = payload.get("communication_planning_field", payload) if isinstance(payload, dict) else payload
        candidate = normalize_communication_planning_field(raw)
        state = self.ensure_state()
        if candidate != state["communication_planning_field"]:
            state["communication_planning_field"] = candidate
            # The communication field is not a planning input in this round, so it does not
            # stale the candidates: only the readiness record changes.
            self.session.save()
        return self.snapshot()

    def set_objective_policy(self, payload):
        raw = payload.get("theta_v2_objective_policy", payload) if isinstance(payload, dict) else payload
        candidate = normalize_theta_v2_objective_policy(raw)
        state = self.ensure_state()
        if candidate != state["theta_v2_objective_policy"]:
            state["theta_v2_objective_policy"] = candidate
            self.invalidation.layered_route("theta_v2_objective_policy_changed")
            self.session.save()
        return self.snapshot()

    def set_risk_density_constraint(self, payload):
        raw = payload.get("max_route_risk_density", payload) if isinstance(payload, dict) else payload
        candidate = normalize_risk_density_constraint(raw)
        state = self.ensure_state()
        if candidate != state["max_route_risk_density"]:
            state["max_route_risk_density"] = candidate
            # A threshold change does not touch population/terrain/building raw data; it makes
            # the derived candidate evaluation stale through the layered invalidation chain.
            self.invalidation.layered_route("max_route_risk_density_changed")
            self.session.save()
        return self.snapshot()

    def refresh_for_reason(self, reason):
        """Stale the layered candidates/masks; never touch legacy routes or V3/CNS."""

        state = self.ensure_state()
        collection = normalize_layered_route_candidate_collection(state["layered_route_candidates"])
        if collection.get("status") == "not_calculated" and not collection.get("items"):
            return
        for mask in (collection.get("masks") or {}).values():
            mask["status"] = "stale"
            mask["stale_reason"] = str(reason)
        for item in collection["items"]:
            if item.get("status") in (
                "candidate", "blocked", "missing_data",
                # Phase 3.5 terminal statuses: a stale input must also stale a result that
                # ended as no_path / search_incomplete / invalid_input.
                "no_path", "search_incomplete", "invalid_input",
            ):
                item["status"] = "stale"
            item["stale_reason"] = str(reason)
        collection["status"] = "stale"
        collection["stale_reason"] = str(reason)
        state["layered_route_candidates"] = collection
        state.setdefault("result_statuses", {})["layered_route_candidate"] = "stale"

    @staticmethod
    def _resolve_scenario_route(request, scenario_routes):
        item = request if isinstance(request, dict) else {}
        route_id = item.get("scenario_route_id")
        if route_id:
            return next(
                (route for route in scenario_routes if str(route.get("route_id")) == str(route_id)),
                None,
            )
        start, end = item.get("start_node_id"), item.get("end_node_id")
        if not start or not end:
            return None
        for route in scenario_routes:
            if (
                str(route.get("start_node_id")) == str(start)
                and str(route.get("end_node_id")) == str(end)
            ):
                return route
        return None

    def _current_identity(self):
        identity = self.current_identity()
        return identity["lane_key"], identity["candidate_fingerprint"]

    def current_identity(self):
        """Re-computed identity of the current ``(route, layer)`` lane.

        Additive read-only seam for downstream analyses (e.g. RouteRiskProfile): it exposes
        the same declared-dependency fingerprints the candidate carries, so a consumer can
        prove a candidate is still current without re-deriving the fingerprint definition.
        """

        state = self.ensure_state()
        request = state["layered_route_planning_request"]
        collection = normalize_layered_route_candidate_collection(state["layered_route_candidates"])
        route = self._resolve_scenario_route(request, _scenario_routes(state))
        route_id = (route or {}).get("route_id")
        if route_id is None:
            route_id = request.get("scenario_route_id") or (
                f"{request.get('start_node_id')}->{request.get('end_node_id')}"
                if request.get("start_node_id") and request.get("end_node_id") else None
            )
        key = _candidate_key(route_id, request.get("altitude_layer_id"))
        mask = (collection.get("masks") or {}).get(key) or {}
        grid_risk_v2 = state.get("grid_risk_v2") or {}
        fingerprints = _fingerprint_planner(self.planner).fingerprints(
            request=request, scenario_route=route, grid=state.get("grid") or {},
            layer_mask=mask, grid_risk_v2=grid_risk_v2,
            cost_policy=state["layered_route_cost_policy"],
            feasibility_policy=state["layered_route_feasibility_policy"],
            hard_constraints=[], building_clearance_policy=state.get("building_clearance_policy") or {},
            source_audits=state.get("source_audits") or {},
            **self._planner_specific_inputs(state, state.get("grid") or {}),
        )
        return {
            "lane_key": key,
            "candidate_fingerprint": fingerprints["candidate_fingerprint"],
            "risk_fingerprint": fingerprints["risk_fingerprint"],
            "policy_fingerprint": fingerprints["policy_fingerprint"],
            "input_fingerprint": fingerprints["input_fingerprint"],
            "request_fingerprint": fingerprints["request_fingerprint"],
            "feasibility_mask_fingerprint": mask.get("mask_fingerprint"),
        }

    @staticmethod
    def _applicability(candidate, current_key, current_fingerprint):
        if candidate.get("status") == "stale":
            return "stale"
        if not candidate.get("lane_key"):
            return "stale"
        key = _candidate_key(candidate.get("route_id"), candidate.get("altitude_layer_id"))
        if key != current_key:
            return "stale_request_or_scenario_changed"
        if candidate.get("candidate_fingerprint") != current_fingerprint:
            return "stale_inputs_changed"
        return "current"

    def _source_status(self):
        provider = self.source_status
        if callable(provider):
            try:
                return deepcopy(provider())
            except (TypeError, ValueError, RuntimeError):
                return None
        if self.adapter is not None:
            status = getattr(self.adapter, "source_status", None)
            if callable(status):
                return deepcopy(status())
        return None

    def _theta_v2_readiness(self, state, request, cruise):
        """Readiness of the population × shelter risk and the additive interfaces."""

        policy = state["shelter_coefficient_policy"]
        attribute = self.population_shelter_snapshot()
        cells = attribute.get("cells") or {}
        unresolved = sorted(
            grid_id for grid_id, cell in cells.items()
            if (cell or {}).get("status") != "passed"
        )
        regulatory = state.get("regulatory_constraints") or default_regulatory_constraints()
        objective = state.get("theta_v2_objective_policy") or default_theta_v2_objective_policy()
        constraint = state.get("max_route_risk_density") or default_risk_density_constraint()
        blockers = []
        if self._uses_theta_star():
            if policy.get("status") != "confirmed":
                blockers.append({
                    "reason_code": str(policy.get("status_reason") or "shelter_coefficient_policy_not_confirmed"),
                    "reason": "shelter_coefficient_policy 未确认：不假设任何遮盖系数",
                })
            if not cells:
                blockers.append({
                    "reason_code": "population_shelter_field_missing",
                    "reason": "population_shelter 场缺失：risk weight>0 时 fail-closed，绝不补 0",
                })
            elif unresolved:
                blockers.append({
                    "reason_code": "population_shelter_risk_unresolved",
                    "reason": (
                        "部分 L8 cell 的 population_shelter risk_index 未解析（"
                        f"{len(unresolved)} 格）：fail-closed，绝不补 0"
                    ),
                })
            if cruise.get("status") != "confirmed":
                blockers.append({
                    "reason_code": "fixed_cruise_altitude_not_confirmed",
                    "reason": "Theta* V2 固定 z(x, y) = H，必须能解析 confirmed EGM2008 巡航高度",
                })
        return {
            "algorithm": {
                "algorithm_id": getattr(self.planner, "algorithm_id", ALGORITHM_ID),
                "algorithm_version": getattr(self.planner, "algorithm_version", ALGORITHM_VERSION),
                "uses_theta_star": self._uses_theta_star(),
            },
            "status": "ready" if not blockers else "not_ready",
            "blockers": blockers,
            # The effective search parameters *and* their provenance.  The two values are no
            # longer invisible planner constants: the software baseline is reported as such,
            # and an explicit algorithm selection override is reported as an override that is
            # still not engineering-confirmed.
            "search_parameters": self._search_parameter_view(),
            "population_shelter": {
                "status": attribute.get("status", "not_calculated"),
                "cell_count": len(cells),
                "resolved_cell_count": len(cells) - len(unresolved),
                "field_fingerprint": population_shelter_fingerprint(attribute),
                "shelter_coefficient_policy": {
                    "status": policy.get("status"),
                    "default_coefficient": policy.get("default_coefficient"),
                    "source": policy.get("source"),
                    "provenance": policy.get("provenance"),
                    "confirmed": bool(policy.get("confirmed")),
                },
                "raw_exposure_definition": "population_density_people_km2 * shelter_coefficient",
                "risk_index_definition": "normalized_population_factor * shelter_coefficient",
                "risk_index_range": [0.0, 1.0],
            },
            "objective": {
                "formula": objective.get("formula"),
                "risk_weight": objective.get("risk_weight"),
                "turn_weight": objective.get("turn_weight"),
                "distance_weight": objective.get("distance_weight"),
                "provenance": objective.get("provenance"),
                "objective_population_shelter_only": True,
            },
            "evaluation_constraint": {
                "metric": constraint.get("metric"),
                "threshold": constraint.get("threshold"),
                "source": constraint.get("source"),
                "temporary": constraint.get("temporary"),
                "role": constraint.get("role"),
                "objective_term": False,
            },
            "regulatory_constraints": {
                **regulatory_compliance_record(regulatory),
                "configured": regulatory_is_configured(regulatory),
            },
            "communication": communication_readiness(self.communication_field_snapshot()),
            "airspace": {
                "status": "not_applicable", "applicability": "display_only",
                "used_in_search": False, "used_in_hard_gate": False,
                "used_in_fingerprint": False,
            },
        }

    @staticmethod
    def _risk_readiness(state):
        result = state.get("grid_risk_v2") or {}
        policy = state.get("risk_policy_v2") or {}
        domains = {}
        for domain_id in COST_DOMAIN_IDS:
            domains[domain_id] = {
                "domain_id": domain_id,
                "status": (result.get("domains") or {}).get(domain_id, {}).get("status", "not_calculated"),
                "index_scope": "per_cell_only",
                "policy_status": (policy.get("domains") or {}).get(domain_id, {}).get(
                    "status", "pending_confirmation"
                ),
            }
        return {
            "status": result.get("status", "not_calculated"),
            "input_fingerprint": result.get("input_fingerprint"),
            "policy_fingerprint": result.get("policy_fingerprint"),
            "policy_status": policy.get("status", "pending_confirmation"),
            "domains": domains,
            "overall_used": False,
            "note": (
                "edge cost 只读取每格 domain index；domain `overall` 不使用。λ>0 的 domain 在任一"
                "候选格 missing/unresolved/pending 时该格不可遍历；λ=0 的 domain 不作为规划输入。"
            ),
        }

    def _blocked(
        self, state, collection, request, route, status, prior, reason, reason_code, key,
    ):
        candidate = default_layered_route_candidate(
            request.get("scenario_route_id") or (
                f"{request.get('start_node_id')}->{request.get('end_node_id')}"
                if request.get("start_node_id") and request.get("end_node_id") else None
            ),
            request.get("altitude_layer_id"), status,
        )
        candidate.update({
            "reason": reason,
            "blocking_reasons": [{"reason_code": reason_code, "reason": reason}],
            "candidate_id": None,
            "request_fingerprint": request_fingerprint(request),
            "feasibility_mask_fingerprint": ((collection.get("masks") or {}).get(key) or {}).get(
                "mask_fingerprint"
            ),
            "provenance": {
                "pipeline": "blocked_before_or_during_layered_planning",
                "blocked_at": reason_code,
                "airspace": {"status": "not_applicable", "applicability": "display_only"},
            },
        })
        # A blocked attempt carries the same declared dependency fingerprint as a run, so the
        # same inputs stay idempotent and a changed input both replaces it and marks the
        # previous record ``stale``.
        fingerprints = _fingerprint_planner(self.planner).fingerprints(
            request=request, scenario_route=route, grid=state.get("grid") or {},
            layer_mask=(collection.get("masks") or {}).get(key) or {},
            grid_risk_v2=state.get("grid_risk_v2") or {},
            cost_policy=state["layered_route_cost_policy"],
            feasibility_policy=state["layered_route_feasibility_policy"],
            hard_constraints=[], building_clearance_policy=state.get("building_clearance_policy") or {},
            source_audits=state.get("source_audits") or {},
            **self._planner_specific_inputs(state, state.get("grid") or {}),
        )
        candidate.update({
            "input_fingerprint": fingerprints["input_fingerprint"],
            "feasibility_fingerprint": fingerprints["feasibility_fingerprint"],
            "risk_fingerprint": fingerprints["risk_fingerprint"],
            "policy_fingerprint": fingerprints["policy_fingerprint"],
            "candidate_fingerprint": fingerprints["candidate_fingerprint"],
        })
        collection.setdefault("masks", {})
        self._store_candidate(collection, key, request, candidate, prior)
        state["layered_route_candidates"] = collection
        state.setdefault("result_statuses", {})["layered_route_candidate"] = collection["status"]
        self.invalidation.layered_route_validation("candidate_attempt_replaced_current_candidate")
        self.session.save()
        return self.snapshot()

    @staticmethod
    def _store_candidate(collection, key, request, candidate, prior):
        """Keep the result idempotent while preserving a stale record after an input change.

        A candidate is identified by its **input** fingerprint.  Re-running with unchanged
        inputs replaces that record in place; when an input changed, the previous record is
        kept and marked ``stale`` (its frozen evidence is never deleted) and its mask is
        marked ``stale`` too.
        """

        fingerprint = candidate.get("candidate_fingerprint") or candidate.get("input_fingerprint")
        candidate["lane_key"] = key
        repeated = bool(fingerprint) and prior.get("fingerprint") == fingerprint
        previous = prior.get("candidate")
        if previous is not None and not repeated:
            previous["status"] = "stale"
            previous["stale_reason"] = "inputs_changed"
        items = [
            item for item in collection["items"]
            if not (item.get("lane_key") == key and item.get("candidate_fingerprint") == fingerprint)
        ]
        items.append(candidate)
        collection["items"] = items
        collection["count"] = len(items)
        if candidate.get("status") == "candidate":
            collection["active_candidate_id"] = candidate.get("candidate_id")
        elif collection.get("active_candidate_id") == (prior.get("candidate") or {}).get(
            "candidate_id"
        ):
            collection["active_candidate_id"] = None
        collection["status"] = (
            "passed" if candidate.get("status") == "candidate" else candidate.get("status")
        )


# ``GridGraph`` is the existing MH/T adjacency/corner-guard implementation; the planner
# consumes it directly, so it is not re-implemented here.

__all__ = ["LayeredRoutePlannerService", "POLICY_SEMANTICS", "REQUEST_SEMANTICS"]
