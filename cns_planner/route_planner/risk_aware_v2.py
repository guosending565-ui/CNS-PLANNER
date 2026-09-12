"""Pure-Python risk-aware A* over the existing MH/T grid.

The consumed risk is a relative engineering index.  It is neither an accident
probability nor a regulatory/SORA/TLS classification.
"""

from __future__ import annotations

from hashlib import sha256
from heapq import heappop, heappush
import json
import math
import re

from ..algorithms.coverage.v1 import distance_m


_GRID_ID = re.compile(r"^MHT4063-L(?P<level>\d+)-C(?P<column>\d+)-R(?P<sign>[PM])(?P<row>\d+)$")
_COMPONENTS = ("overall", "ground", "air")
_UNKNOWN_POLICIES = ("block", "penalize")


class GridGraph:
    """Stable-id 8-neighbour graph built from canonical grid cells."""

    def __init__(self, cells):
        if not isinstance(cells, list) or not cells:
            raise ValueError("标准网格 cells 缺失")
        self.cells = {}
        self.centers = {}
        for raw in cells:
            grid_id = str((raw or {}).get("grid_id") or "")
            bbox = (raw or {}).get("bbox")
            if not grid_id or grid_id in self.cells:
                raise ValueError("标准网格 grid_id 缺失或重复")
            if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
                raise ValueError(f"网格 {grid_id} 缺少 bbox")
            west, south, east, north = (float(value) for value in bbox)
            if not all(math.isfinite(value) for value in (west, south, east, north)) or west >= east or south >= north:
                raise ValueError(f"网格 {grid_id} bbox 无效")
            cell = dict(raw)
            cell["bbox"] = [west, south, east, north]
            center = cell.get("center")
            if not isinstance(center, (list, tuple)) or len(center) < 2:
                center = [(west + east) / 2.0, (south + north) / 2.0]
            cell["center"] = [float(center[0]), float(center[1])]
            self.cells[grid_id] = cell
            self.centers[grid_id] = cell["center"]
        self._extents = (
            min(cell["bbox"][0] for cell in self.cells.values()),
            min(cell["bbox"][1] for cell in self.cells.values()),
            max(cell["bbox"][2] for cell in self.cells.values()),
            max(cell["bbox"][3] for cell in self.cells.values()),
        )
        self.indices = self._indices()
        self.adjacency = self._indexed_adjacency() if self.indices is not None else self._bbox_adjacency()

    def containing_cell(self, point):
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            return None
        lon, lat = float(point[0]), float(point[1])
        max_east, max_north = self._extents[2], self._extents[3]
        for grid_id in sorted(self.cells):
            west, south, east, north = self.cells[grid_id]["bbox"]
            in_lon = west <= lon < east or (math.isclose(east, max_east) and west <= lon <= east)
            in_lat = south <= lat < north or (math.isclose(north, max_north) and south <= lat <= north)
            if in_lon and in_lat:
                return grid_id
        return None

    def neighbors(self, grid_id):
        return self.adjacency.get(grid_id, ())

    def diagonal_guards(self, left, right):
        """Return orthogonal cells that must be traversable for a diagonal."""
        if self.indices is not None:
            a, b = self.indices[left], self.indices[right]
            if a[0] != b[0] or a[1] == b[1] or a[2] == b[2]:
                return None
            reverse = {value: key for key, value in self.indices.items()}
            candidates = ((a[0], b[1], a[2]), (a[0], a[1], b[2]))
            return tuple(reverse.get(value) for value in candidates)
        a, b = self.centers[left], self.centers[right]
        if math.isclose(a[0], b[0], abs_tol=1e-12) or math.isclose(a[1], b[1], abs_tol=1e-12):
            return None
        center_index = {
            (round(value[0], 12), round(value[1], 12)): grid_id
            for grid_id, value in self.centers.items()
        }
        candidates = (
            center_index.get((round(b[0], 12), round(a[1], 12))),
            center_index.get((round(a[0], 12), round(b[1], 12))),
        )
        return candidates

    def _indices(self):
        result = {}
        seen = set()
        for grid_id, cell in self.cells.items():
            explicit_column = cell.get("column", cell.get("col"))
            explicit_row = cell.get("row")
            if explicit_column is not None and explicit_row is not None:
                level = int(cell.get("level") or 0)
                index = level, int(explicit_column), int(explicit_row)
            else:
                match = _GRID_ID.fullmatch(grid_id)
                if match is None:
                    return None
                row = int(match.group("row")) * (1 if match.group("sign") == "P" else -1)
                index = int(match.group("level")), int(match.group("column")), row
            if index in seen:
                return None
            result[grid_id] = index
            seen.add(index)
        return result

    def _indexed_adjacency(self):
        reverse = {value: key for key, value in self.indices.items()}
        result = {grid_id: [] for grid_id in self.cells}
        for grid_id, (level, column, row) in self.indices.items():
            for dx, dy in ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)):
                other = reverse.get((level, column + dx, row + dy))
                if other is not None:
                    result[grid_id].append(other)
            result[grid_id] = tuple(sorted(result[grid_id]))
        return result

    def _bbox_adjacency(self):
        edge_index = {}
        for grid_id, cell in self.cells.items():
            west, south, east, north = cell["bbox"]
            for axis, value in (("x", west), ("x", east), ("y", south), ("y", north)):
                edge_index.setdefault((axis, round(value, 12)), []).append(grid_id)
        result = {grid_id: set() for grid_id in self.cells}
        for grid_id, cell in self.cells.items():
            west, south, east, north = cell["bbox"]
            candidates = set()
            for axis, value in (("x", west), ("x", east), ("y", south), ("y", north)):
                candidates.update(edge_index.get((axis, round(value, 12)), ()))
            for other in candidates:
                if other != grid_id and _bbox_touches(cell["bbox"], self.cells[other]["bbox"]):
                    result[grid_id].add(other)
        return {grid_id: tuple(sorted(values)) for grid_id, values in result.items()}


class RiskAwareRoutePlannerV2:
    algorithm_id = "risk_aware_route_planner_v2"
    algorithm_version = "2.0"
    uses_canonical_grid_risk = True
    default_parameters = {
        "risk_weight_lambda": 0.0,
        "risk_component": "overall",
        "unknown_risk_policy": "block",
        "unknown_penalty_index": None,
        "max_relative_risk_index": None,
    }

    def __init__(self, parameters=None):
        supplied = dict(parameters or {})
        self.parameters = {**self.default_parameters, **supplied}
        self._validate_parameters(supplied)

    def plan(self, route, grid, grid_risk, hard_constraints):
        fingerprint = _fingerprint(route, grid, grid_risk, hard_constraints, self.parameters)
        route_id = str((route or {}).get("route_id") or "")
        start, goal = (route or {}).get("start"), (route or {}).get("end")
        if not route_id or not _point(start) or not _point(goal):
            return self._result("missing_data", route_id, [], [], "航路端点或 route_id 缺失", fingerprint)
        if not isinstance(grid, dict) or grid.get("status") != "passed" or not grid.get("cells"):
            return self._result("missing_data", route_id, [], [], "当前 MH/T 标准网格不可用", fingerprint)
        if (
            not isinstance(grid_risk, dict)
            or grid_risk.get("status") not in ("passed", "missing_data")
            or not grid_risk.get("cells")
        ):
            return self._result("missing_data", route_id, [], [], "grid_risk 未计算或已失效", fingerprint)

        graph = GridGraph(list(grid["cells"]))
        source, target = graph.containing_cell(start), graph.containing_cell(goal)
        if source is None or target is None:
            return self._result("failed", route_id, [], [], "起点或终点不在当前标准网格内", fingerprint)

        hard_blocked = {
            grid_id for grid_id, cell in graph.cells.items()
            if any(_positive_bbox_intersection(cell["bbox"], _constraint_bbox(item)) for item in hard_constraints or [])
        }
        if _point_in_constraints(start, hard_constraints) or _point_in_constraints(goal, hard_constraints) or source in hard_blocked or target in hard_blocked:
            return self._result("failed", route_id, [], [], "起点或终点落入管制/硬约束范围", fingerprint)

        risk_values, unknown = self._risk_values(graph, grid_risk)
        threshold = self.parameters["max_relative_risk_index"]
        threshold_blocked = {
            grid_id for grid_id, value in risk_values.items()
            if threshold is not None and value > threshold
        }
        if source in unknown or target in unknown:
            return self._result("missing_data", route_id, [], [], "起点或终点风险证据不可用", fingerprint)
        if source in threshold_blocked or target in threshold_blocked:
            return self._result("failed", route_id, [], [], "起点或终点超过工程相对风险阈值", fingerprint)

        blocked = hard_blocked | threshold_blocked | unknown
        grid_path = self._astar(graph, source, target, risk_values, blocked)
        if not grid_path:
            status = "missing_data" if unknown else "failed"
            reason = "风险证据缺失阻断，未找到可用路径" if unknown else "硬约束/工程风险阈值阻断，未找到可用路径"
            return self._result(status, route_id, [], [], reason, fingerprint)

        path = _path_with_real_endpoints(start, goal, grid_path, graph.centers)
        metrics = _path_metrics(path, grid_path, graph.centers, risk_values, start, goal, self.parameters["risk_weight_lambda"])
        straight = distance_m(start, goal)
        return self._result(
            "passed", route_id, path, grid_path, "Risk-aware A* 规划完成",
            fingerprint, metrics={
                **metrics,
                "straight_line_distance_m": straight,
                "detour_factor": metrics["distance_m"] / straight if straight > 0 else 1.0,
            }, grid_risk=grid_risk,
        )

    def _risk_values(self, graph, grid_risk):
        values, unknown = {}, set()
        component = self.parameters["risk_component"]
        penalty = self.parameters["unknown_penalty_index"]
        for grid_id in graph.cells:
            value = ((grid_risk.get("cells") or {}).get(grid_id) or {}).get(component) or {}
            score = value.get("score")
            valid = value.get("status") == "passed" and _finite(score) and 0.0 <= float(score) <= 1.0
            if valid:
                values[grid_id] = float(score)
            elif self.parameters["unknown_risk_policy"] == "penalize":
                values[grid_id] = penalty
            else:
                unknown.add(grid_id)
        return values, unknown

    def _astar(self, graph, source, target, risks, blocked):
        queue = [(distance_m(graph.centers[source], graph.centers[target]), 0.0, source)]
        costs = {source: 0.0}
        previous = {}
        while queue:
            _, current_cost, current = heappop(queue)
            if current_cost > costs.get(current, math.inf) + 1e-9:
                continue
            if current == target:
                break
            for neighbour in graph.neighbors(current):
                if neighbour in blocked:
                    continue
                guards = graph.diagonal_guards(current, neighbour)
                if guards is not None and (None in guards or any(item in blocked for item in guards)):
                    continue
                length = distance_m(graph.centers[current], graph.centers[neighbour])
                edge_risk = (risks[current] + risks[neighbour]) / 2.0
                candidate = current_cost + length * (1.0 + self.parameters["risk_weight_lambda"] * edge_risk)
                known = costs.get(neighbour, math.inf)
                if candidate < known - 1e-9:
                    costs[neighbour] = candidate
                    previous[neighbour] = current
                    heuristic = distance_m(graph.centers[neighbour], graph.centers[target])
                    heappush(queue, (candidate + heuristic, candidate, neighbour))
                elif math.isclose(candidate, known, abs_tol=1e-9) and current < previous.get(neighbour, "\uffff"):
                    previous[neighbour] = current
        if target not in costs:
            return []
        result, current = [target], target
        while current != source:
            current = previous[current]
            result.append(current)
        return list(reversed(result))

    def _validate_parameters(self, supplied):
        value = self.parameters["risk_weight_lambda"]
        if not _finite(value) or float(value) < 0:
            raise ValueError("risk_weight_lambda 必须是非负数")
        self.parameters["risk_weight_lambda"] = float(value)
        component = str(self.parameters["risk_component"])
        if component not in _COMPONENTS:
            raise ValueError("risk_component 必须为 overall/ground/air")
        self.parameters["risk_component"] = component
        policy = str(self.parameters["unknown_risk_policy"])
        if policy not in _UNKNOWN_POLICIES:
            raise ValueError("unknown_risk_policy 必须为 block/penalize")
        self.parameters["unknown_risk_policy"] = policy
        penalty = self.parameters.get("unknown_penalty_index")
        if policy == "penalize":
            if "unknown_penalty_index" not in supplied or not _finite(penalty) or not 0 <= float(penalty) <= 1:
                raise ValueError("penalize 必须显式提供 0..1 unknown_penalty_index")
            self.parameters["unknown_penalty_index"] = float(penalty)
        elif penalty is not None:
            if not _finite(penalty) or not 0 <= float(penalty) <= 1:
                raise ValueError("unknown_penalty_index 必须位于 0..1")
            self.parameters["unknown_penalty_index"] = float(penalty)
        threshold = self.parameters.get("max_relative_risk_index")
        if threshold is not None:
            if not _finite(threshold) or not 0 <= float(threshold) <= 1:
                raise ValueError("max_relative_risk_index 必须位于 0..1")
            self.parameters["max_relative_risk_index"] = float(threshold)

    def _result(self, status, route_id, path, grid_path, reason, fingerprint, metrics=None, grid_risk=None):
        metrics = metrics or {}
        risk_fingerprint = _risk_fingerprint(grid_risk, self.parameters["risk_component"]) if grid_risk else None
        mean = metrics.get("mean_risk_index")
        return {
            "route_id": route_id, "status": status, "path": path, "reason": reason,
            "algorithm_id": self.algorithm_id, "algorithm_version": self.algorithm_version,
            "input_fingerprint": fingerprint, "grid_path": grid_path,
            "distance_m": metrics.get("distance_m"),
            "risk_exposure_index_m": metrics.get("risk_exposure_index_m"),
            "mean_risk_index": mean, "max_risk_index": metrics.get("max_risk_index"),
            "optimization_cost": metrics.get("optimization_cost"),
            "straight_line_distance_m": metrics.get("straight_line_distance_m"),
            "detour_factor": metrics.get("detour_factor"),
            "risk_weight_lambda": self.parameters["risk_weight_lambda"],
            "risk_component": self.parameters["risk_component"],
            "unknown_policy": self.parameters["unknown_risk_policy"],
            "unknown_penalty_index": self.parameters["unknown_penalty_index"],
            "max_relative_risk_index": self.parameters["max_relative_risk_index"],
            "risk_threshold_semantics": (
                "engineering_relative_index_threshold_not_regulatory"
                if self.parameters["max_relative_risk_index"] is not None else "not_applied"
            ),
            "risk_semantics": "relative_engineering_index_not_probability",
            "model_scope": "two_dimensional_strategic_horizontal_route",
            "altitude_profile": "not_evaluated",
            "risk_source": {
                "algorithm_id": (grid_risk or {}).get("algorithm_id"),
                "algorithm_version": (grid_risk or {}).get("algorithm_version"),
                "status": (grid_risk or {}).get("status"),
                "component": self.parameters["risk_component"],
                "fingerprint": risk_fingerprint,
            },
            "risk_fingerprint": risk_fingerprint,
            "environment_risk": {
                "status": "passed" if status == "passed" else status,
                "value": mean, "unit": "relative_index_0_1" if mean is not None else None,
                "threshold": self.parameters["max_relative_risk_index"],
                "source": "existing grid_risk; relative engineering index, not probability",
            },
        }


def _path_metrics(path, grid_path, centers, risks, start, goal, risk_lambda):
    points = [list(start), *[centers[item] for item in grid_path], list(goal)]
    point_risks = [risks[grid_path[0]], *[risks[item] for item in grid_path], risks[grid_path[-1]]]
    distance = exposure = 0.0
    for left, right, risk_left, risk_right in zip(points, points[1:], point_risks, point_risks[1:]):
        length = distance_m(left, right)
        distance += length
        exposure += length * (risk_left + risk_right) / 2.0
    return {
        "distance_m": distance,
        "risk_exposure_index_m": exposure,
        "mean_risk_index": exposure / distance if distance > 0 else risks[grid_path[0]],
        "max_risk_index": max(risks[item] for item in grid_path),
        "optimization_cost": distance + risk_lambda * exposure,
    }


def _path_with_real_endpoints(start, goal, grid_path, centers):
    values = [list(start), *[list(centers[item]) for item in grid_path], list(goal)]
    result = []
    for point in values:
        if not result or point != result[-1]:
            result.append(point)
    return result


def _fingerprint(route, grid, grid_risk, constraints, parameters):
    relevant_risk = {
        "status": (grid_risk or {}).get("status"),
        "algorithm_id": (grid_risk or {}).get("algorithm_id"),
        "algorithm_version": (grid_risk or {}).get("algorithm_version"),
        "component": parameters["risk_component"],
        "cells": {
            grid_id: ((value or {}).get(parameters["risk_component"]) or {})
            for grid_id, value in sorted(((grid_risk or {}).get("cells") or {}).items())
        },
    }
    return _hash([
        route,
        [{"grid_id": cell.get("grid_id"), "level": cell.get("level"), "bbox": cell.get("bbox"), "center": cell.get("center")} for cell in (grid or {}).get("cells") or []],
        relevant_risk, constraints or [], parameters,
    ])


def _risk_fingerprint(grid_risk, component):
    if not grid_risk:
        return None
    return _hash({
        "status": grid_risk.get("status"),
        "algorithm_id": grid_risk.get("algorithm_id"),
        "algorithm_version": grid_risk.get("algorithm_version"),
        "source_versions": grid_risk.get("source_versions") or {},
        "component": component,
        "cells": {
            grid_id: ((value or {}).get(component) or {})
            for grid_id, value in sorted((grid_risk.get("cells") or {}).items())
        },
    })


def _hash(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _bbox_touches(a, b):
    x_overlap = min(a[2], b[2]) - max(a[0], b[0])
    y_overlap = min(a[3], b[3]) - max(a[1], b[1])
    tolerance = 1e-11
    return x_overlap >= -tolerance and y_overlap >= -tolerance and (abs(x_overlap) <= tolerance or abs(y_overlap) <= tolerance)


def _positive_bbox_intersection(a, b):
    if not isinstance(b, (list, tuple)) or len(b) != 4:
        return False
    try:
        return min(a[2], float(b[2])) > max(a[0], float(b[0])) and min(a[3], float(b[3])) > max(a[1], float(b[1]))
    except (TypeError, ValueError):
        return False


def _point_in_constraints(point, constraints):
    for item in constraints or []:
        bbox = item.get("bbox") if isinstance(item, dict) else None
        if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
            if float(bbox[0]) <= float(point[0]) <= float(bbox[2]) and float(bbox[1]) <= float(point[1]) <= float(bbox[3]):
                return True
    return False


def _constraint_bbox(item):
    return item.get("bbox") if isinstance(item, dict) else None


def _point(value):
    return isinstance(value, (list, tuple)) and len(value) >= 2 and all(_finite(item) for item in value[:2])


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))
