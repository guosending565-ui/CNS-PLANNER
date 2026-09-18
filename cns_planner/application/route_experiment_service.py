"""Route-planning experiments: run V1/V2 for expert comparison, read-only w.r.t. state.

This service is deliberately *not* part of the operational route path:

* it never changes ``algorithm_selection``;
* it never writes ``operational_routes`` or ``result_statuses["routes"]``;
* it stores its output in the additive ``route_planning_experiments`` collection.

It reuses :class:`~cns_planner.application.route_service.RouteService` for the planner
dispatch so the exact same planner signatures, parameters and input views are used as
the live path — the search core, cost function and output contract are untouched.
"""

from __future__ import annotations

from copy import deepcopy
import statistics
import time

from ..benchmark.quality import evaluate_route_quality
from ..domain.experiment import (
    build_experiment_record, experiment_collection, planner_context_fingerprint,
    scenario_fingerprint, utc_now,
)
from .constraint_validation import validate_hard_constraints

PLANNER_TYPES = ("route_planner",)
EXPERIMENT_NOTE = (
    "experiment ≠ current operational route：实验只写入 route_planning_experiments，"
    "不切换 algorithm_selection，也不覆盖 operational_routes。"
)


class RoutePlanningExperimentService:
    def __init__(self, session, route_service, algorithm_registry, invalidation, snapshot):
        self.session = session
        self.route_service = route_service
        self.registry = algorithm_registry
        self.invalidation = invalidation
        self.snapshot = snapshot

    # ------------------------------------------------------------------ queries

    def result_snapshot(self):
        collection = deepcopy(self.session.state.get("route_planning_experiments") or {})
        records = []
        for record in collection.get("records") or []:
            records.append(self._with_applicability(record))
        collection["records"] = records
        collection["note"] = EXPERIMENT_NOTE
        collection["active_experiment"] = next(
            (item for item in records if item["experiment_id"] == collection.get("active_experiment_id")),
            records[0] if records else None,
        )
        # Reference comparisons are derived read-only from the frozen experiment plus
        # the current reference data; nothing is persisted by this call.
        collection["reference_comparisons"] = self.reference_comparisons(collection)
        return collection

    def diagnostics_snapshot(self):
        """Derived diagnostics for current operational routes; never persisted."""

        state = self.session.state
        try:
            workspace = state.get("workspace") or {}
            constraints = validate_hard_constraints(
                workspace.get("hard_constraints") or [], field="workspace.hard_constraints",
            )
            context = self.route_service.planner_context(constraints)
        except (TypeError, ValueError) as exc:
            return {
                "status": "missing_data", "reason": str(exc), "routes": [],
                "automatic_ranking": False,
            }
        scenarios = {item.get("route_id"): item for item in state.get("scenario_routes") or []}
        diagnostics = []
        for result in state.get("operational_routes") or []:
            route = scenarios.get(result.get("route_id"), {"route_id": result.get("route_id")})
            diagnostics.append(evaluate_route_quality(
                route, result, constraints,
                grid=context.get("grid"),
            ))
        selection = (state.get("algorithm_selection") or {}).get("route_planner") or {}
        manifest = None
        try:
            manifest = self.registry.manifest(
                "route_planner", selection.get("algorithm_id"), str(selection.get("version")),
            ).to_dict()
        except (KeyError, TypeError, ValueError):
            pass
        return {
            "status": "passed" if diagnostics else "not_calculated",
            "planner": {
                "algorithm_id": selection.get("algorithm_id"),
                "version": selection.get("version"),
                "manifest_limitations": (manifest or {}).get("limitations") or [],
            },
            "routes": diagnostics,
            "measurement_scope": "derived_read_only_from_current_operational_routes",
            "automatic_ranking": False,
            "automatic_scoring": False,
        }

    def _with_applicability(self, record):
        updated = deepcopy(record)
        current = scenario_fingerprint(self.session.state.get("scenario_routes") or [])
        scenario_changed = record.get("scenario_fingerprint") != current
        stored_context = record.get("planner_context_fingerprint")
        current_context = None
        context_error = None
        try:
            basis = record.get("planner_context_basis") or {}
            if basis.get("hard_constraints_source") == "payload_override":
                constraints = validate_hard_constraints(basis.get("hard_constraints_snapshot") or [])
            else:
                workspace = self.session.state.get("workspace") or {}
                constraints = validate_hard_constraints(
                    workspace.get("hard_constraints") or [], field="workspace.hard_constraints",
                )
            current_context = planner_context_fingerprint(
                self.route_service.planner_context(constraints)
            )
        except (TypeError, ValueError) as exc:
            context_error = str(exc)
        context_changed = stored_context is None or current_context != stored_context
        if scenario_changed:
            applicability = "stale_scenario_inputs"
        elif stored_context is None:
            applicability = "unknown_legacy_context_inputs"
        elif context_changed:
            applicability = "stale_context_inputs"
        else:
            applicability = "current"
        updated["current_applicability"] = applicability
        updated["current_scenario_fingerprint"] = current
        updated["current_planner_context_fingerprint"] = current_context
        updated["scenario_inputs_changed"] = scenario_changed
        updated["context_inputs_changed"] = context_changed
        updated["context_comparison_error"] = context_error
        return updated

    def reference_comparisons(self, collection=None):
        """Read-only reference ↔ planned comparisons for linked pairs (never stored)."""

        from ..benchmark.reference_comparison import (
            compute_reference_comparison, reference_comparison_readiness,
        )
        from ..domain.reference_crs import is_resolved

        state = self.session.state
        collection = collection or (state.get("route_planning_experiments") or {})
        reference_routes = {
            item.get("reference_route_id"): item
            for item in ((state.get("reference_routes") or {}).get("items") or [])
        }
        links = (state.get("reference_route_links") or {}).get("items") or []
        scenario_routes = {
            item.get("route_id"): item for item in (state.get("scenario_routes") or [])
        }
        active = collection.get("active_experiment") or {}
        comparisons, blocked = [], []
        for link in links:
            reference = reference_routes.get(link.get("reference_route_id"))
            scenario = scenario_routes.get(link.get("scenario_route_id"))
            readiness = reference_comparison_readiness(reference, link, scenario)
            if not readiness["ready"]:
                blocked.append({
                    "link_id": link.get("link_id"),
                    "reference_route_id": link.get("reference_route_id"),
                    "scenario_route_id": link.get("scenario_route_id"),
                    "status": "not_ready",
                    "reasons": readiness["reasons"],
                    "requires_source_crs_confirmed": True,
                    "requires_explicit_user_link": True,
                })
                continue
            comparisons.append(compute_reference_comparison(
                reference, scenario, link, planned_path=self._planned_path(active, scenario),
            ))
        return {
            "status": "passed" if comparisons else ("blocked" if links else "not_calculated"),
            "comparison_count": len(comparisons),
            "blocked_count": len(blocked),
            "comparisons": comparisons,
            "blocked": blocked,
            "reference_route_count": len(reference_routes),
            "link_count": len(links),
            "reference_source_crs_resolved": bool(
                is_resolved((state.get("reference_routes") or {}).get("crs"), role="source_crs")
            ),
            "automatic_association": False,
            "note": "仅描述差异；不产生 similarity score、排名或“更好”结论。",
        }

    def _planned_path(self, active_experiment, scenario_route):
        if not active_experiment or not scenario_route:
            return None
        for run in active_experiment.get("runs") or []:
            result = run.get("result") or {}
            if result.get("route_id") == scenario_route.get("route_id") and result.get("path"):
                return result.get("path")
            for item in result.get("results") or []:
                if item.get("route_id") == scenario_route.get("route_id") and item.get("path"):
                    return item.get("path")
        return None

    # ------------------------------------------------------------------ mutating

    def evaluate(self, payload=None):
        payload = payload if isinstance(payload, dict) else {}
        state = self.session.state
        scenario_routes = state.get("scenario_routes") or []
        if not scenario_routes:
            raise ValueError("请先生成场景航路，再运行规划器比较实验")
        specs = self._run_specs(payload)
        if not specs:
            raise ValueError("至少要选择一个 route planner 才能运行实验")
        constraints = self._constraints(payload)
        constraint_source = (
            "payload_override"
            if "hard_constraints" in payload and payload["hard_constraints"] is not None
            else "current_workspace"
        )
        grounding = str(payload.get("grounding") or "current_scenario_routes")
        if grounding != "current_scenario_routes":
            raise ValueError("当前只支持基于 current_scenario_routes 的实验；合成算例请使用 tools/route_planning_baseline.py")

        context = self.route_service.planner_context(constraints)
        context_fingerprint_value = planner_context_fingerprint(context)
        runs = []
        for spec in specs:
            runs.append(self._execute(spec, scenario_routes, context, constraints))

        record = build_experiment_record(
            scenario_fingerprint_value=scenario_fingerprint(scenario_routes),
            planners=runs,
            grounding=grounding,
            source_type="project",
            source_detail={
                "scenario_route_ids": [item.get("route_id") for item in scenario_routes],
                "workspace_bbox": (state.get("workspace") or {}).get("bbox"),
                "hard_constraint_count": len(constraints),
                "planner_source": "registry_manifest_and_factory",
                "dispatch": "route_service_planner_context_and_plan",
            },
            context_fingerprint_value=context_fingerprint_value,
            context_basis={
                "hard_constraints_source": constraint_source,
                "hard_constraints_snapshot": deepcopy(constraints),
                "semantics": "scenario_and_non_scenario_planner_inputs_compared_independently",
            },
        )
        record["algorithm_selection_snapshot"] = deepcopy(state.get("algorithm_selection"))
        record["note"] = EXPERIMENT_NOTE
        existing = (state.get("route_planning_experiments") or {}).get("records") or []
        retained = [item for item in existing if item.get("experiment_id") != record["experiment_id"]]
        state["route_planning_experiments"] = experiment_collection(
            [record, *retained], active_experiment_id=record["experiment_id"],
        )
        # Experiments do not invalidate the formal route result: operational_routes and
        # result_statuses["routes"] are untouched by design.
        self.session.save()
        return self.snapshot()

    def delete_experiment(self, experiment_id):
        state = self.session.state
        collection = state.get("route_planning_experiments") or {}
        records = collection.get("records") or []
        retained = [item for item in records if item.get("experiment_id") != experiment_id]
        if len(retained) == len(records):
            raise ValueError("实验记录不存在")
        active = collection.get("active_experiment_id")
        if active == experiment_id:
            active = retained[0]["experiment_id"] if retained else None
        state["route_planning_experiments"] = experiment_collection(retained, active_experiment_id=active)
        self.session.save()
        return self.snapshot()

    # ------------------------------------------------------------------ internals

    def _constraints(self, payload):
        if "hard_constraints" in payload and payload["hard_constraints"] is not None:
            return validate_hard_constraints(payload["hard_constraints"])
        workspace = self.session.state.get("workspace") or {}
        return validate_hard_constraints(workspace.get("hard_constraints") or [],
                                         field="workspace.hard_constraints")

    def _run_specs(self, payload):
        requested = payload.get("planners")
        if requested is None:
            requested = [
                {"algorithm_id": "route_planner_v1", "version": "1.0"},
                {"algorithm_id": "risk_aware_route_planner_v2", "version": "2.0"},
            ]
        if not isinstance(requested, list) or not requested:
            raise ValueError("planners 必须是非空数组")
        selection = self.session.state.get("algorithm_selection") or {}
        specs = []
        for index, raw in enumerate(requested):
            if not isinstance(raw, dict):
                raise ValueError("planner 运行定义必须是对象")
            algorithm_id = str(raw.get("algorithm_id") or "")
            version = str(raw.get("version") or "")
            if not algorithm_id or not version:
                raise ValueError("planner 运行定义缺少 algorithm_id/version")
            manifest = self.registry.manifest("route_planner", algorithm_id, version)
            default_selection = (selection.get("route_planner") or {})
            if raw.get("parameters") is not None:
                parameters = deepcopy(raw["parameters"])
            elif (default_selection.get("algorithm_id") == algorithm_id
                  and str(default_selection.get("version")) == version):
                # Only the current selection's parameters are reused, and only read-only.
                parameters = deepcopy(default_selection.get("parameters") or {})
            else:
                parameters = {}
            run_id = str(raw.get("run_id") or f"{algorithm_id}:{version}#{index + 1}")
            specs.append({
                "run_id": run_id, "algorithm_id": algorithm_id, "version": version,
                "parameters": parameters, "manifest": manifest.to_dict(),
            })
        if len({spec["run_id"] for spec in specs}) != len(specs):
            raise ValueError("planner run_id 必须唯一")
        return specs

    def _execute(self, spec, scenario_routes, context, constraints):
        planner = self.registry.create(
            "route_planner", spec["algorithm_id"], spec["version"], spec["parameters"],
        )
        started = time.perf_counter()
        failure = None
        try:
            results = self.route_service.plan_routes(planner, scenario_routes, context, constraints)
        except ValueError as exc:
            results, failure = [], str(exc)
        wall_ms = (time.perf_counter() - started) * 1000.0
        per_route_ms = []
        snapshots, qualities = [], []
        for route, result in zip(scenario_routes, results):
            route_start = time.perf_counter()
            # The measured runtime of a single route is captured by running the same
            # planner on the same inputs again; results are compared for stability.
            try:
                repeat = self.route_service.plan_routes(planner, [route], context, constraints)
            except ValueError:
                repeat = []
            per_route_ms.append((time.perf_counter() - route_start) * 1000.0)
            repeated = repeat[0] if repeat else None
            evaluation = evaluate_route_quality(
                route, result, constraints, per_route_ms[-1],
                grid=context.get("grid"),
            )
            evaluation["deterministic_consistency"] = (
                None if repeated is None else repeated.get("input_fingerprint") == result.get("input_fingerprint")
                and repeated.get("path") == result.get("path")
            )
            snapshots.append(deepcopy(result))
            qualities.append(evaluation)
        status = "failed" if failure and not results else None
        return {
            "run_id": spec["run_id"],
            "algorithm_id": spec["algorithm_id"],
            "algorithm_version": spec["version"],
            "manifest": spec["manifest"],
            "effective_parameters": deepcopy(spec["parameters"]),
            "applicable": failure is None or bool(results),
            "status": status,
            "reason": failure,
            "result": (
                snapshots[0] if len(snapshots) == 1 else {"results": snapshots}
            ),
            "quality": qualities[0] if len(qualities) == 1 else {"results": qualities},
            "runtime": {
                "route_count": len(scenario_routes),
                "wall_ms": wall_ms,
                "per_route_ms": per_route_ms,
                "per_route_ms_stats": _stats(per_route_ms),
                "measurement": "perf_counter_wall_clock_including_application_dispatch",
                "used_for_ranking": False,
            },
            "recorded_at": utc_now(),
        }


def _stats(values):
    numbers = [float(value) for value in values if isinstance(value, (int, float))]
    if not numbers:
        return {"count": 0, "min": None, "median": None, "p95": None, "max": None}
    ordered = sorted(numbers)
    index = min(len(ordered) - 1, max(0, int(round(0.95 * (len(ordered) - 1)))))
    return {
        "count": len(ordered),
        "min": ordered[0],
        "median": statistics.median(ordered),
        "p95": ordered[index],
        "max": ordered[-1],
    }
