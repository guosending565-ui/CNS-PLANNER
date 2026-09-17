"""Route-node, scenario and operational-route use cases."""

from .constraint_validation import validate_hard_constraints
from .project_state import assessment


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
        state["operational_routes"] = [
            item for item in state["operational_routes"] if item["route_id"] not in retired
        ]
        self.invalidation.workflow("route")
        return self._save()

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
        state["scenario_routes"], state["operational_routes"] = created, []
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
        state["scenario_routes"], state["operational_routes"] = created, []
        state["result_statuses"]["routes"] = "not_calculated"
        self.invalidation.workflow("route")
        return self._save()

    def delete_route(self, route_id):
        state = self.session.state
        if route_id not in {item["route_id"] for item in state["scenario_routes"]}:
            raise ValueError("航路不存在")
        state["scenario_routes"] = [item for item in state["scenario_routes"] if item["route_id"] != route_id]
        state["operational_routes"] = [item for item in state["operational_routes"] if item["route_id"] != route_id]
        if route_id not in state["retired_route_ids"]:
            state["retired_route_ids"].append(route_id)
        self.invalidation.workflow("route")
        return self._save()

    def generate_operational(self, hard_constraints):
        state, workspace = self.session.state, self.session.state.get("workspace")
        if not workspace or not state["scenario_routes"]:
            raise ValueError("请先保存工作区并生成场景航路")
        # Fail closed before any planner runs: a malformed constraint is never
        # dropped, repaired or silently downgraded to "no constraint".
        constraints = validate_hard_constraints(hard_constraints)
        if getattr(self.planner, "uses_canonical_grid_risk", False):
            results = [
                self.planner.plan(
                    route, state.get("grid") or {}, state.get("grid_risk") or {},
                    constraints,
                    ((state.get("grid_attributes") or {}).get("airspace") or {}).get("airspace_eligibility"),
                )
                for route in state["scenario_routes"]
            ]
        else:
            results = [
                self.planner.plan(route, workspace["bbox"], constraints)
                for route in state["scenario_routes"]
            ]
        state["operational_routes"] = results
        statuses = {item.get("status") for item in results}
        state["result_statuses"]["routes"] = (
            "passed" if statuses == {"passed"}
            else "failed" if "failed" in statuses
            else "missing_data" if "missing_data" in statuses
            else "failed"
        )
        if not getattr(self.planner, "uses_canonical_grid_risk", False):
            state["risks"]["environment"] = assessment(
                "pending_confirmation", "GRC 环境风险接口已接入，正式模型待确认"
            )
        self.invalidation.workflow("route")
        return self._save()

    def _save(self):
        self.session.save()
        return self.snapshot()
