"""Replaceable grid A* planner used by the first end-to-end workflow."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from heapq import heappop, heappush
import json
import math


@dataclass(frozen=True)
class RoutePlan:
    status: str
    path: list[list[float]]
    reason: str
    algorithm_id: str = "route_planner_v1"
    algorithm_version: str = "1.0"

class RoutePlannerV1:
    """Small-area A*. Hard constraints are blocked; failure never falls back to a line."""

    def __init__(self, grid_size: int = 56):
        self.grid_size = grid_size

    def plan(self, route: dict, workspace: list[float], hard_constraints: list[dict]) -> dict:
        west, south, east, north = workspace
        start, goal = route["start"], route["end"]
        if not (west <= start[0] <= east and south <= start[1] <= north and west <= goal[0] <= east and south <= goal[1] <= north):
            return self._result("failed", [], "起降点位于工作区外", route, workspace, hard_constraints)
        width = height = self.grid_size

        def cell(point):
            x = min(width - 1, max(0, round((point[0] - west) / (east - west) * (width - 1))))
            y = min(height - 1, max(0, round((point[1] - south) / (north - south) * (height - 1))))
            return x, y

        def point(cell_value):
            x, y = cell_value
            return [west + x / (width - 1) * (east - west), south + y / (height - 1) * (north - south)]

        blocked = set()
        for constraint in hard_constraints:
            cw, cs, ce, cn = constraint["bbox"]
            x0, y0 = cell([max(west, cw), max(south, cs)])
            x1, y1 = cell([min(east, ce), min(north, cn)])
            if ce < west or cw > east or cn < south or cs > north:
                continue
            for x in range(min(x0, x1), max(x0, x1) + 1):
                for y in range(min(y0, y1), max(y0, y1) + 1):
                    blocked.add((x, y))
        source, target = cell(start), cell(goal)
        if source in blocked or target in blocked:
            return self._result("failed", [], "起点或终点落入管制/硬约束范围", route, workspace, hard_constraints)

        queue = [(0.0, source)]
        cost = {source: 0.0}
        previous = {}
        moves = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1))
        while queue:
            _, current = heappop(queue)
            if current == target:
                break
            for dx, dy in moves:
                nxt = current[0] + dx, current[1] + dy
                if not (0 <= nxt[0] < width and 0 <= nxt[1] < height) or nxt in blocked:
                    continue
                if dx and dy and ((current[0] + dx, current[1]) in blocked or (current[0], current[1] + dy) in blocked):
                    continue
                candidate = cost[current] + math.hypot(dx, dy)
                if candidate < cost.get(nxt, float("inf")):
                    cost[nxt] = candidate
                    previous[nxt] = current
                    heuristic = math.hypot(target[0] - nxt[0], target[1] - nxt[1])
                    heappush(queue, (candidate + heuristic, nxt))
        if target not in cost:
            return self._result("failed", [], "管制/硬约束阻断，未找到可用路径", route, workspace, hard_constraints)
        cells, current = [target], target
        while current != source:
            current = previous[current]
            cells.append(current)
        cells.reverse()
        path = [list(start)] + [point(value) for value in cells[1:-1]] + [list(goal)]
        path = self._simplify(path)
        return self._result("passed", path, "A* 规划完成；正式风险代价模型待接入", route, workspace, hard_constraints)

    @staticmethod
    def _simplify(path):
        if len(path) < 3:
            return path
        result = [path[0]]
        for index in range(1, len(path) - 1):
            a, b, c = result[-1], path[index], path[index + 1]
            if abs((b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])) > 1e-10:
                result.append(b)
        result.append(path[-1])
        return result

    def _result(self, status, path, reason, route, workspace, hard_constraints):
        fingerprint = sha256(json.dumps([route, workspace, hard_constraints], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return {
            "route_id": route["route_id"], "status": status, "path": path, "reason": reason,
            "algorithm_id": "route_planner_v1", "algorithm_version": "1.0", "input_fingerprint": fingerprint,
            "environment_risk": {"status": "pending_confirmation", "value": None, "unit": None, "threshold": None, "source": "GRC 接口待正式模型"},
        }
