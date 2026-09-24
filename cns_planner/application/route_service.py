"""Route-node, scenario and operational-route use cases.

Phase4-B2B-1 Production Write Authority（受限软隔离）：

* ``operational_routes`` 的 canonical production content owner 是
  ``LayeredOperationalAdoptionService``。本服务**不再**写 canonical ``operational_routes``。
* ``generate_operational`` 仍可运行 ``RoutePlannerV1`` / ``RiskAwareRoutePlannerV2``，但结果只进入
  当前 ``WorkflowSession`` 的 runtime-only cache，并显式携带
  ``authoritative=false / compatibility=true / deprecated=true / source_algorithm``。
  兼容试算不是正式运行航路，不驱动 canonical coverage / required_cns / facility plan /
  confirmed plan / 正式报告，也**不会**把 ``result_statuses["routes"]`` 改成 passed。
* ``generate_scenario`` / ``generate_scenario_od`` / ``delete_route`` / ``delete_node`` 仍然清空或
  回收派生运行航路（删除派生结果，不是产生权威结果），这些写点经
  :data:`DERIVED_REVOKE_ALLOWLIST` 显式白名单登记，语义保持不变。
"""

from copy import deepcopy

from .constraint_validation import validate_hard_constraints
from .production_write_authority import (
    assert_write_authority, drop_runtime_compatibility_result,
    runtime_compatibility_items, runtime_compatibility_result,
    write_runtime_compatibility_result,
)
from .project_state import assessment

#: 旧算法兼容试算结果在 runtime-only cache 下的名字。
COMPATIBILITY_OPERATIONAL_ROUTES = "operational_routes"


class RouteService:
    def __init__(self, session, planner, invalidation, snapshot):
        self.session = session
        self.planner = planner
        self.invalidation = invalidation
        self.snapshot = snapshot

    def add_node(self, coordinate, name=None):
        state, workspace = self.session.state, self.session.state.get("workspace")
        if not workspace:
            raise ValueError("请先保存工作区")
        lon, lat = self._coordinate_in_workspace(coordinate, workspace)
        state["node_seq"] += 1
        state["nodes"].append({
            "node_id": f"N{state['node_seq']:03d}",
            "name": str(name or f"起降点 {state['node_seq']}"),
            "coordinate": [lon, lat],
        })
        self.invalidation.workflow("route")
        return self._save()

    def add_reference_site(self, site):
        state, workspace = self.session.state, self.session.state.get("workspace")
        if not workspace:
            raise ValueError("请先保存工作区")
        reference_site_id = str(site.get("reference_site_id") or "")
        if not reference_site_id:
            raise ValueError("参考起降点缺少稳定 ID")
        if any(item.get("reference_site_id") == reference_site_id for item in state["nodes"]):
            return self.snapshot()
        lon, lat = self._coordinate_in_workspace(site.get("coordinate") or [], workspace)
        state["node_seq"] += 1
        state["nodes"].append({
            "node_id": f"N{state['node_seq']:03d}",
            "name": str(site.get("name") or f"起降点 {state['node_seq']}"),
            "coordinate": [lon, lat],
            "reference_site_id": reference_site_id,
            "provenance": {
                "source_type": "real_reference",
                "reference_site_id": reference_site_id,
                "source": dict(site.get("source") or {}),
                "coordinate_quality": site.get("quality"),
                "crs_status": site.get("crs_status", "pending_confirmation"),
                "coordinate_usage": "source_numeric_lon_lat_pending_crs_confirmation",
            },
        })
        self.invalidation.workflow("route")
        return self._save()

    @staticmethod
    def _coordinate_in_workspace(coordinate, workspace):
        if not isinstance(coordinate, (list, tuple)) or len(coordinate) != 2:
            raise ValueError("起降点坐标无效")
        lon, lat = (float(value) for value in coordinate)
        west, south, east, north = workspace["bbox"]
        if not (west <= lon <= east and south <= lat <= north):
            raise ValueError("起降点必须位于工作区内")
        return lon, lat

    def delete_node(self, node_id):
        state = self.session.state
        before = len(state["nodes"])
        state["nodes"] = [item for item in state["nodes"] if item["node_id"] != node_id]
        if len(state["nodes"]) == before:
            raise ValueError("起降点不存在")
        retired = [
            item["route_id"] for item in state["scenario_routes"]
            if node_id in (item["start_node_id"], item["end_node_id"])
        ]
        state["retired_route_ids"].extend(
            item for item in retired if item not in state["retired_route_ids"]
        )
        state["scenario_routes"] = [
            item for item in state["scenario_routes"] if item["route_id"] not in retired
        ]
        self._revoke_derived_operational_routes(state, retired)
        self.invalidation.workflow("route")
        return self._save()

    def _revoke_derived_operational_routes(self, state, retired_ids=()):
        """派生回收：删除自己场景派生的运行航路及其兼容试算副本。

        这不是产生权威结果，只是删除失效派生结果，因此经
        ``DERIVED_REVOKE_ALLOWLIST`` 显式登记；canonical 与 compatibility 两侧必须
        同步回收，避免兼容试算副本残留成"看起来还存在的航路"。
        """

        assert_write_authority(RouteService, "operational_routes", operation="revoke")
        retired = {str(item) for item in retired_ids}
        if not retired:
            state["operational_routes"] = []
            drop_runtime_compatibility_result(self.session, COMPATIBILITY_OPERATIONAL_ROUTES)
            return
        state["operational_routes"] = [
            item for item in state.get("operational_routes") or []
            if str(item.get("route_id")) not in retired
        ]
        record = runtime_compatibility_result(self.session, COMPATIBILITY_OPERATIONAL_ROUTES)
        if record:
            record["items"] = [
                item for item in runtime_compatibility_items(
                    self.session, COMPATIBILITY_OPERATIONAL_ROUTES
                )
                if str(item.get("route_id")) not in retired
            ]
            record["count"] = len(record["items"])

    def generate_scenario(self, direction):
        state = self.session.state
        if len(state["nodes"]) < 2:
            raise ValueError("至少需要两个起降点")
        if direction not in ("both", "ab", "ba"):
            raise ValueError("航路方向无效")
        old = {
            (item["start_node_id"], item["end_node_id"]): item
            for item in state["scenario_routes"]
        }
        created = []
        for left_index in range(len(state["nodes"]) - 1):
            for right_index in range(left_index + 1, len(state["nodes"])):
                a, b = state["nodes"][left_index], state["nodes"][right_index]
                pairs = [(a, b)] if direction == "ab" else [(b, a)] if direction == "ba" else [(a, b), (b, a)]
                for start, end in pairs:
                    previous = old.get((start["node_id"], end["node_id"]))
                    if previous:
                        created.append(previous)
                        continue
                    state["route_seq"] += 1
                    created.append({
                        "route_id": f"R{state['route_seq']:04d}",
                        "start_node_id": start["node_id"], "end_node_id": end["node_id"],
                        "start": start["coordinate"], "end": end["coordinate"],
                        "direction": f"{start['node_id']}→{end['node_id']}",
                        "status": "passed", "path": [start["coordinate"], end["coordinate"]],
                        "kind": "scenario",
                    })
        retained_ids = {route["route_id"] for route in created}
        removed = [item["route_id"] for item in state["scenario_routes"] if item["route_id"] not in retained_ids]
        state["retired_route_ids"].extend(
            item for item in removed if item not in state["retired_route_ids"]
        )
        state["scenario_routes"] = created
        self._revoke_derived_operational_routes(state)
        state["result_statuses"]["routes"] = "not_calculated"
        self.invalidation.workflow("route")
        return self._save()

    def generate_scenario_od(self, start_node_id, end_node_id, direction="both"):
        """Create explicit start→end scenario route(s) for the two named nodes.

        Additive to :meth:`generate_scenario` (all-pairs), which stays available for
        existing projects.  This never derives routes from the number of reference
        points, and it never creates an all-pairs mesh.  Like the existing pair
        generator it replaces the current scenario route set, reuses the route_id of
        an identical surviving direction and retires the rest; operational routes are
        cleared and must be re-planned.
        """
        state = self.session.state
        if direction not in ("both", "ab", "ba"):
            raise ValueError("航路方向无效")
        nodes = {item["node_id"]: item for item in state["nodes"]}
        start_id, end_id = str(start_node_id or ""), str(end_node_id or "")
        if not start_id or start_id not in nodes:
            raise ValueError("起点 node_id 无效或不存在")
        if not end_id or end_id not in nodes:
            raise ValueError("终点 node_id 无效或不存在")
        if start_id == end_id:
            raise ValueError("起点与终点不能是同一个 node")
        start, end = nodes[start_id], nodes[end_id]
        pairs = (
            [(start, end), (end, start)] if direction == "both"
            else [(end, start)] if direction == "ba"
            else [(start, end)]
        )
        existing = {
            (item["start_node_id"], item["end_node_id"]): item
            for item in state["scenario_routes"]
        }
        created = []
        for left, right in pairs:
            previous = existing.get((left["node_id"], right["node_id"]))
            if previous:
                created.append(previous)
                continue
            state["route_seq"] += 1
            created.append({
                "route_id": f"R{state['route_seq']:04d}",
                "start_node_id": left["node_id"], "end_node_id": right["node_id"],
                "start": left["coordinate"], "end": right["coordinate"],
                "direction": f"{left['node_id']}→{right['node_id']}",
                "status": "passed", "path": [left["coordinate"], right["coordinate"]],
                "kind": "scenario",
            })
        retained_ids = {route["route_id"] for route in created}
        removed = [item["route_id"] for item in state["scenario_routes"] if item["route_id"] not in retained_ids]
        state["retired_route_ids"].extend(
            item for item in removed if item not in state["retired_route_ids"]
        )
        state["scenario_routes"] = created
        self._revoke_derived_operational_routes(state)
        state["result_statuses"]["routes"] = "not_calculated"
        self.invalidation.workflow("route")
        return self._save()

    def delete_route(self, route_id):
        state = self.session.state
        if route_id not in {item["route_id"] for item in state["scenario_routes"]}:
            raise ValueError("航路不存在")
        state["scenario_routes"] = [item for item in state["scenario_routes"] if item["route_id"] != route_id]
        self._revoke_derived_operational_routes(state, [route_id])
        if route_id not in state["retired_route_ids"]:
            state["retired_route_ids"].append(route_id)
        self.invalidation.workflow("route")
        return self._save()

    def planner_context(self, hard_constraints):
        """Frozen, read-only planner inputs for the current project state.

        Shared verbatim by the operational path and by comparison experiments so both
        feed the planners exactly the same input view.  Building this mapping never
        mutates project state.
        """
        state, workspace = self.session.state, self.session.state.get("workspace")
        if not workspace:
            raise ValueError("请先保存工作区")
        return {
            "workspace_bbox": list(workspace["bbox"]),
            "grid": state.get("grid") or {},
            "grid_risk": state.get("grid_risk") or {},
            "uses_canonical_grid_risk": bool(
                getattr(self.planner, "uses_canonical_grid_risk", False)
            ),
            "hard_constraints": list(hard_constraints or []),
        }

    @staticmethod
    def plan_routes(planner, routes, context, constraints):
        """Dispatch one planner over routes using an explicit, frozen input view.

        The planner's own search core, cost function and output contract are not
        touched here; only the argument shape is selected from its declared capability.
        """
        if getattr(planner, "uses_canonical_grid_risk", False):
            return [
                planner.plan(
                    route, context.get("grid") or {}, context.get("grid_risk") or {},
                    constraints,
                )
                for route in routes
            ]
        return [
            planner.plan(route, context.get("workspace_bbox"), constraints)
            for route in routes
        ]

    def generate_operational(self, hard_constraints):
        """旧版 RoutePlannerV1 / RiskAwareRoutePlannerV2 兼容试算（受限软隔离）。

        仍可运行旧算法以兼容旧项目、测试与对照，但结果**只**写入
        当前会话的 runtime-only compatibility cache：
        canonical ``operational_routes`` 完全不变，``result_statuses["routes"]`` 也不改
        （否则 canonical workflow readiness 会把兼容试算误认为正式运行航路已完成）。
        """

        state, workspace = self.session.state, self.session.state.get("workspace")
        if not workspace or not state["scenario_routes"]:
            raise ValueError("请先保存工作区并生成场景航路")
        # Fail closed before any planner runs: a malformed constraint is never
        # dropped, repaired or silently downgraded to "no constraint".
        constraints = validate_hard_constraints(hard_constraints)
        context = self.planner_context(constraints)
        results = self.plan_routes(self.planner, state["scenario_routes"], context, constraints)
        statuses = {item.get("status") for item in results}
        status = (
            "passed" if statuses == {"passed"}
            else "failed" if "failed" in statuses
            else "missing_data" if "missing_data" in statuses
            else "failed"
        )
        record = write_runtime_compatibility_result(
            self.session, COMPATIBILITY_OPERATIONAL_ROUTES,
            {
                "status": status,
                "stale_reason": None,
                "count": len(results),
                "items": deepcopy(results),
                "canonical_operational_routes_unchanged": True,
                "consumed_by_canonical_workflow": False,
                "drives_canonical_coverage": False,
                "drives_required_cns": False,
                "drives_facility_plan": False,
                "drives_confirmed_plan": False,
            },
            source_algorithm=self._planner_identity(),
            note=(
                "旧版兼容试算（旧 RoutePlannerV1 / RiskAwareRoutePlannerV2）："
                "不是正式运行航路，不发布、不驱动 canonical 下游、不进入正式报告。"
            ),
        )
        if not getattr(self.planner, "uses_canonical_grid_risk", False):
            state["risks"]["environment"] = assessment(
                "pending_confirmation", "GRC 环境风险接口已接入，正式模型待确认"
            )
        self.session.save()
        response = self.snapshot()
        response["compatibility_operational_routes"] = deepcopy(record)
        response["compatibility_write"] = True
        response["authoritative_operational_routes_unchanged"] = True
        return response

    def _planner_identity(self):
        return {
            "algorithm_type": "route_planner",
            "algorithm_id": getattr(self.planner, "algorithm_id", None),
            "algorithm_version": getattr(self.planner, "algorithm_version", None),
            "class": type(self.planner).__name__,
            "uses_canonical_grid_risk": bool(
                getattr(self.planner, "uses_canonical_grid_risk", False)
            ),
        }

    def _save(self):
        self.session.save()
        return self.snapshot()
