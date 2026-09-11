"""Persistent six-step workflow state and replaceable planning orchestration."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from uuid import uuid4

try:
    from ..algorithms.coverage_planner import CoveragePlannerV1
    from ..algorithms.route_planner import RoutePlannerV1
    from ..models.status import ResultStatus
    from ..persistence.project_repository import ProjectRepository
    from ..risk.model import RiskModel
    from ..risk.v1 import RiskModelV1
    from ..simulation.conflict_detector import ConflictDetector
    from ..simulation.traffic_simulator import TrafficSimulator
    from .airspace_grid_service import AirspaceGridService
    from .conflict_grid_service import ConflictGridService
    from .grid_service import WorkspaceGridService
    from .invalidation import ResultLedger
    from .population_grid_service import PopulationGridService
    from .terrain_grid_service import TerrainGridService
    from .traffic_grid_service import TrafficGridService
except ImportError:  # map_server.py runs with cns_planner on sys.path.
    from algorithms.coverage_planner import CoveragePlannerV1
    from algorithms.route_planner import RoutePlannerV1
    from models.status import ResultStatus
    from persistence.project_repository import ProjectRepository
    from risk.model import RiskModel
    from risk.v1 import RiskModelV1
    from simulation.conflict_detector import ConflictDetector
    from simulation.traffic_simulator import TrafficSimulator
    from services.airspace_grid_service import AirspaceGridService
    from services.conflict_grid_service import ConflictGridService
    from services.grid_service import WorkspaceGridService
    from services.invalidation import ResultLedger
    from services.population_grid_service import PopulationGridService
    from services.terrain_grid_service import TerrainGridService
    from services.traffic_grid_service import TrafficGridService


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class WorkflowService:
    schema_version = 2
    mapped_grid_attribute_names = ("population", "terrain", "airspace")
    extension_grid_attribute_names = (
        "buildings", "property_exposure", "infrastructure", "towers",
        "traffic", "conflict",
    )

    def __init__(
        self,
        store_path: Path,
        defaults_path: Path,
        risk_model: RiskModel | None = None,
    ):
        self.store_path = store_path
        self.repository = ProjectRepository(store_path)
        self.defaults_path = defaults_path
        self.defaults = json.loads(defaults_path.read_text(encoding="utf-8"))
        self.route_planner = RoutePlannerV1()
        self.coverage_planner = CoveragePlannerV1(self.defaults)
        self.grid_service = WorkspaceGridService()
        self.risk_model: RiskModel = risk_model or RiskModelV1()
        self.traffic_simulator = TrafficSimulator()
        self.conflict_detector = ConflictDetector()
        self.traffic_grid_service = TrafficGridService()
        self.conflict_grid_service = ConflictGridService()
        self.state = self._load()

    def _blank(self):
        risks = {
            "environment": self._assessment("not_calculated", "GRC 环境/航路规划风险接口"),
            "technical": self._assessment("pending_confirmation", "MTBF 技术失效风险接口"),
            "life": self._assessment("pending_confirmation", "生命风险模型、单位和阈值待确认"),
            "property": self._assessment("missing_data", "财产暴露数据未接入"),
        }
        return {
            "schema_version": self.schema_version,
            "project": {"project_id": str(uuid4()), "name": "CNS 规划项目", "created_at": utc_now(), "updated_at": utc_now()},
            "workspace": None,
            "grid": None,
            "grid_attributes": self._empty_grid_attributes(),
            "grid_risk": RiskModelV1.empty(),
            "traffic_simulation": None,
            "nodes": [],
            "node_seq": 0,
            "route_seq": 0,
            "retired_route_ids": [],
            "scenario_routes": [],
            "operational_routes": [],
            "aircraft": None,
            "rules": None,
            "devices": deepcopy(self.defaults.get("device_library", {}).get("items", [])),
            "coverage": None,
            "risks": risks,
            "result_statuses": {name: "not_calculated" for name in ("workspace", "grid", "environment_risk", "routes", "coverage", "technical_risk", "report")},
            "last_saved_at": None,
        }

    @staticmethod
    def _assessment(status, reason):
        return {"status": status, "value": None, "unit": None, "threshold": None, "source": reason}

    def _load(self):
        if not self.repository.exists():
            return self._blank()
        try:
            value = self.repository.load()
            if value.get("schema_version") != self.schema_version:
                return self._blank()
            if "grid" not in value:
                workspace = value.get("workspace")
                value["grid"] = self.grid_service.generate(workspace["bbox"]) if workspace else None
            attributes = value.setdefault("grid_attributes", self._empty_grid_attributes())
            for name, empty in self._empty_grid_attributes().items():
                attributes.setdefault(name, empty)
            value.setdefault("grid_risk", RiskModelV1.empty())
            value.setdefault("traffic_simulation", None)
            value.setdefault("result_statuses", {}).setdefault(
                "grid", "passed" if value.get("grid") else "not_calculated"
            )
            return value
        except (OSError, ValueError, AttributeError):
            return self._blank()

    def save(self):
        self.state["project"]["updated_at"] = utc_now()
        self.state["last_saved_at"] = utc_now()
        self.repository.save(self.state)

    def snapshot(self):
        result = deepcopy(self.state)
        result["steps"] = self._steps()
        result["defaults"] = deepcopy(self.defaults)
        result["device_source"] = self.defaults.get("device_library", {}).get("source", "demo/default")
        result["aircraft_source"] = self.defaults.get("aircraft_library", {}).get("source", "demo/default")
        result["review"] = self.review()
        return result

    def grid_snapshot(self):
        return deepcopy(self.state.get("grid") or self.grid_service.empty())

    def grid_attributes_snapshot(self):
        return deepcopy(self.state.get("grid_attributes") or self._empty_grid_attributes())

    def grid_risk_snapshot(self):
        return deepcopy(self.state.get("grid_risk") or RiskModelV1.empty())

    @staticmethod
    def _empty_grid_attributes():
        return {
            "population": PopulationGridService.empty(),
            "terrain": TerrainGridService.empty(),
            "airspace": AirspaceGridService.empty(),
            "buildings": WorkflowService._empty_extension_attribute("buildings"),
            "property_exposure": WorkflowService._empty_extension_attribute("property_exposure"),
            "infrastructure": WorkflowService._empty_extension_attribute("infrastructure"),
            "towers": WorkflowService._empty_extension_attribute("towers"),
            "traffic": TrafficGridService.empty(),
            "conflict": ConflictGridService.empty(),
        }

    @staticmethod
    def _empty_extension_attribute(name):
        return {
            "status": "not_calculated",
            "source": None,
            "algorithm_id": None,
            "algorithm_version": None,
            "namespace": name,
            "grid_level": None,
            "count": 0,
            "cells": {},
        }

    def apply_grid_attributes(self, results):
        grid = self.state.get("grid")
        if not grid:
            raise ValueError("请先生成工作区标准网格")
        expected_ids = {cell["grid_id"] for cell in grid.get("cells", [])}
        clean = {}
        incoming = results or {}
        current = self.state.get("grid_attributes") or self._empty_grid_attributes()
        for kind in self.mapped_grid_attribute_names:
            result = deepcopy(incoming.get(kind))
            if not isinstance(result, dict):
                raise ValueError(f"{kind} 网格映射结果缺失")
            if result.get("grid_level") != grid.get("level"):
                raise ValueError(f"{kind} 网格层级与当前标准网格不一致")
            if set((result.get("cells") or {}).keys()) != expected_ids:
                raise ValueError(f"{kind} 网格属性与当前 grid_id 不一致")
            clean[kind] = result
        for kind in self.extension_grid_attribute_names:
            result = deepcopy(incoming.get(kind, current.get(kind)))
            if not isinstance(result, dict):
                result = self._empty_extension_attribute(kind)
            cells = result.get("cells") or {}
            if result.get("status") != "not_calculated" or cells:
                if result.get("grid_level") != grid.get("level"):
                    raise ValueError(f"{kind} 网格层级与当前标准网格不一致")
                if set(cells) != expected_ids:
                    raise ValueError(f"{kind} 网格属性与当前 grid_id 不一致")
            clean[kind] = result
        self.state["grid_attributes"] = clean
        self._apply_grid_risk(self.risk_model.evaluate(grid, clean, None))
        self.save()
        return self.snapshot()

    def evaluate_grid_risk(self, parameters=None):
        grid = self.state.get("grid")
        result = self.risk_model.evaluate(
            grid, self.state.get("grid_attributes") or {}, parameters
        )
        self._apply_grid_risk(result)
        self.save()
        return self.snapshot()

    def run_traffic_simulation(self, parameters):
        grid = self.state.get("grid")
        if not grid:
            raise ValueError("请先生成工作区标准网格")
        payload = parameters or {}
        simulation = self.traffic_simulator.simulate(payload.get("simulation") or payload)
        traffic = self.traffic_grid_service.map(
            grid, simulation, payload.get("traffic") or {}
        )
        detection = self.conflict_detector.detect(
            simulation["trajectories"], payload.get("conflict") or {}
        )
        conflict = self.conflict_grid_service.map(
            grid, detection, simulation["simulation_seconds"],
            payload.get("conflict_grid") or {},
        )
        attributes = self.state.setdefault(
            "grid_attributes", self._empty_grid_attributes()
        )
        attributes["traffic"] = traffic
        attributes["conflict"] = conflict
        self.state["traffic_simulation"] = {
            **simulation,
            "conflict_detection": detection,
        }
        risk_parameters = payload.get("risk")
        if risk_parameters is None:
            risk_parameters = (self.state.get("grid_risk") or {}).get("parameters")
        self._apply_grid_risk(
            self.risk_model.evaluate(grid, attributes, risk_parameters)
        )
        self.save()
        return self.snapshot()

    def _apply_grid_risk(self, result):
        if not isinstance(result, dict):
            raise ValueError("风险模型未返回有效结果")
        grid = self.state.get("grid")
        expected_ids = {cell["grid_id"] for cell in (grid or {}).get("cells", [])}
        result_cells = result.get("cells") or {}
        if expected_ids and set(result_cells) != expected_ids:
            raise ValueError("风险结果与当前 grid_id 不一致")
        if any("geometry" in cell for cell in result_cells.values()):
            raise ValueError("风险结果不得复制基础网格 geometry")
        self.state["grid_risk"] = deepcopy(result)
        status = result.get("status", "not_calculated")
        self.state["result_statuses"]["environment_risk"] = status
        self.state["risks"]["environment"] = self._assessment(
            status,
            f"{result.get('algorithm_id', 'risk-model')}@{result.get('algorithm_version', 'unknown')}",
        )

    def invalidate_grid_attributes(self, changed_sources):
        attributes = self.state.setdefault("grid_attributes", self._empty_grid_attributes())
        mapping = {
            "population": ("population",),
            "terrain": ("terrain",),
            "basemap": ("airspace",),
            "airspace": ("airspace",),
            "buildings": ("buildings",),
            "property": ("property_exposure",),
            "property_exposure": ("property_exposure",),
            "infrastructure": ("infrastructure",),
            "obstacles": ("towers",),
            "towers": ("towers",),
            "traffic_simulation": ("traffic", "conflict"),
            "traffic": ("traffic", "conflict"),
            "conflict": ("conflict",),
        }
        invalidated = False
        for source_name in changed_sources:
            kinds = mapping.get(source_name)
            if not kinds:
                continue
            invalidated = True
            for kind in kinds:
                if attributes.get(kind, {}).get("status") != "not_calculated":
                    attributes[kind]["status"] = "stale"
            if source_name in ("traffic_simulation", "traffic") and self.state.get("traffic_simulation"):
                self.state["traffic_simulation"]["status"] = "stale"
        if invalidated:
            self._invalidate_grid_risk()

    def _invalidate_grid_risk(self):
        result = self.state.setdefault("grid_risk", RiskModelV1.empty())
        if result.get("status") == "not_calculated":
            return
        result["status"] = "stale"
        self.state["result_statuses"]["environment_risk"] = "stale"
        self.state["risks"]["environment"] = self._assessment(
            "stale", "网格风险输入属性已变化"
        )

    def _steps(self):
        workspace_ok = bool(self.state["workspace"] and self.state["workspace"].get("status") == "passed")
        scenario_ok = bool(self.state["scenario_routes"])
        routes_ok = (
            scenario_ok
            and self.state["result_statuses"].get("routes") == "passed"
            and bool(self.state["operational_routes"])
            and all(item["status"] == "passed" for item in self.state["operational_routes"])
        )
        rules_ok = bool(self.state["rules"] and self.state["rules"].get("status") == "passed" and self.state["aircraft"])
        coverage_ok = bool(
            self.state["result_statuses"].get("coverage") == "passed"
            and self.state["coverage"]
            and self.state["coverage"].get("status") == "passed"
        )
        return {"1": True, "2": workspace_ok, "3": routes_ok, "4": rules_ok, "5": coverage_ok, "6": coverage_ok}

    def set_project(self, payload):
        name = str(payload.get("name", "")).strip()
        if not name or len(name) > 120:
            raise ValueError("项目名称须为 1–120 个字符")
        self.state["project"]["name"] = name
        self.save()
        return self.snapshot()

    def set_workspace(self, bbox, health):
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise ValueError("工作区必须包含西、南、东、北四个坐标")
        values = [float(value) for value in bbox]
        west, south, east, north = values
        if not (-180 <= west < east <= 180 and -85 < south < north < 85):
            raise ValueError("工作区范围无效")
        width = math.radians(east - west) * 6371008.8 * math.cos(math.radians((south + north) / 2))
        height = math.radians(north - south) * 6371008.8
        grid = self.grid_service.generate(values)
        self.state["workspace"] = {"bbox": values, "area_km2": round(width * height / 1_000_000, 3), "health": health, "status": "passed"}
        self.state["result_statuses"]["workspace"] = "passed"
        self.invalidate("workspace")
        self.state["grid"] = grid
        self.state["grid_attributes"] = self._empty_grid_attributes()
        self.state["grid_risk"] = RiskModelV1.empty()
        self.state["traffic_simulation"] = None
        self.state["risks"]["environment"] = self._assessment(
            "not_calculated", "等待当前网格属性风险评估"
        )
        self.state["result_statuses"]["environment_risk"] = "not_calculated"
        self.state["result_statuses"]["grid"] = "passed"
        self.save()
        return self.snapshot()

    def clear_workspace(self):
        self.invalidate("workspace")
        self.state["workspace"] = None
        self.state["grid"] = None
        self.state["grid_attributes"] = self._empty_grid_attributes()
        self.state["grid_risk"] = RiskModelV1.empty()
        self.state["traffic_simulation"] = None
        self.state["risks"]["environment"] = self._assessment(
            "not_calculated", "GRC 环境/航路规划风险接口"
        )
        self.state["nodes"] = []
        self.state["scenario_routes"] = []
        self.state["operational_routes"] = []
        self.state["coverage"] = None
        self.state["result_statuses"]["workspace"] = "not_calculated"
        self.state["result_statuses"]["grid"] = "not_calculated"
        self.state["result_statuses"]["environment_risk"] = "not_calculated"
        self.save()
        return self.snapshot()

    def add_node(self, coordinate, name=None):
        workspace = self.state.get("workspace")
        if not workspace:
            raise ValueError("请先保存工作区")
        lon, lat = (float(value) for value in coordinate)
        west, south, east, north = workspace["bbox"]
        if not (west <= lon <= east and south <= lat <= north):
            raise ValueError("起降点必须位于工作区内")
        self.state["node_seq"] += 1
        node = {"node_id": f"N{self.state['node_seq']:03d}", "name": str(name or f"起降点 {self.state['node_seq']}"), "coordinate": [lon, lat]}
        self.state["nodes"].append(node)
        self.invalidate("route")
        self.save()
        return self.snapshot()

    def delete_node(self, node_id):
        before = len(self.state["nodes"])
        self.state["nodes"] = [item for item in self.state["nodes"] if item["node_id"] != node_id]
        if len(self.state["nodes"]) == before:
            raise ValueError("起降点不存在")
        retired = [item["route_id"] for item in self.state["scenario_routes"] if node_id in (item["start_node_id"], item["end_node_id"])]
        self.state["retired_route_ids"].extend(item for item in retired if item not in self.state["retired_route_ids"])
        self.state["scenario_routes"] = [item for item in self.state["scenario_routes"] if item["route_id"] not in retired]
        self.state["operational_routes"] = [item for item in self.state["operational_routes"] if item["route_id"] not in retired]
        self.invalidate("route")
        self.save()
        return self.snapshot()

    def generate_scenario(self, direction):
        if len(self.state["nodes"]) < 2:
            raise ValueError("至少需要两个起降点")
        if direction not in ("both", "ab", "ba"):
            raise ValueError("航路方向无效")
        old = {(item["start_node_id"], item["end_node_id"]): item for item in self.state["scenario_routes"]}
        created = []
        for left_index in range(len(self.state["nodes"]) - 1):
            for right_index in range(left_index + 1, len(self.state["nodes"])):
                a, b = self.state["nodes"][left_index], self.state["nodes"][right_index]
                pairs = [(a, b)] if direction == "ab" else [(b, a)] if direction == "ba" else [(a, b), (b, a)]
                for start, end in pairs:
                    previous = old.get((start["node_id"], end["node_id"]))
                    if previous:
                        created.append(previous)
                        continue
                    self.state["route_seq"] += 1
                    created.append({
                        "route_id": f"R{self.state['route_seq']:04d}",
                        "start_node_id": start["node_id"], "end_node_id": end["node_id"],
                        "start": start["coordinate"], "end": end["coordinate"], "direction": f"{start['node_id']}→{end['node_id']}",
                        "status": "passed", "path": [start["coordinate"], end["coordinate"]], "kind": "scenario",
                    })
        removed = [item["route_id"] for item in self.state["scenario_routes"] if item["route_id"] not in {route["route_id"] for route in created}]
        self.state["retired_route_ids"].extend(item for item in removed if item not in self.state["retired_route_ids"])
        self.state["scenario_routes"] = created
        self.state["operational_routes"] = []
        self.state["result_statuses"]["routes"] = "not_calculated"
        self.invalidate("route")
        self.save()
        return self.snapshot()

    def delete_route(self, route_id):
        if route_id not in {item["route_id"] for item in self.state["scenario_routes"]}:
            raise ValueError("航路不存在")
        self.state["scenario_routes"] = [item for item in self.state["scenario_routes"] if item["route_id"] != route_id]
        self.state["operational_routes"] = [item for item in self.state["operational_routes"] if item["route_id"] != route_id]
        if route_id not in self.state["retired_route_ids"]:
            self.state["retired_route_ids"].append(route_id)
        self.invalidate("route")
        self.save()
        return self.snapshot()

    def generate_operational(self, hard_constraints):
        workspace = self.state.get("workspace")
        if not workspace or not self.state["scenario_routes"]:
            raise ValueError("请先保存工作区并生成场景航路")
        results = [self.route_planner.plan(route, workspace["bbox"], hard_constraints) for route in self.state["scenario_routes"]]
        self.state["operational_routes"] = results
        self.state["result_statuses"]["routes"] = "passed" if all(item["status"] == "passed" for item in results) else "failed"
        self.state["risks"]["environment"] = self._assessment("pending_confirmation", "GRC 环境风险接口已接入，正式模型待确认")
        self.invalidate("route")
        self.save()
        return self.snapshot()

    def set_rules(self, payload):
        required = ("manufacturer", "model", "cruise_speed", "max_speed", "mtbf", "height_ab", "height_ba", "delay_sensor", "delay_command")
        if any(payload.get(name) in (None, "") for name in required):
            raise ValueError("请完整填写飞行器、运行高度和两段时延")
        cruise, maximum, mtbf = (float(payload[name]) for name in ("cruise_speed", "max_speed", "mtbf"))
        height_ab, height_ba = float(payload["height_ab"]), float(payload["height_ba"])
        delay_sensor, delay_command = float(payload["delay_sensor"]), float(payload["delay_command"])
        if min(cruise, maximum, mtbf, height_ab, height_ba) <= 0 or min(delay_sensor, delay_command) < 0:
            raise ValueError("速度、可靠性和高度须大于零，时延不得为负")
        total_delay_ms = delay_sensor + delay_command
        reaction_distance_m = cruise * total_delay_ms / 1000
        same_level = payload.get("height_mode") == "same"
        separation = float(payload.get("horizontal_separation") or 0)
        clearance = float(self.defaults["engineering_parameters"]["vertical_clearance_m"]["value"])
        valid = separation >= 2 * reaction_distance_m if same_level else abs(height_ab - height_ba) >= clearance
        message = "规则校验通过" if valid else ("同高度层水平间隔不足" if same_level else f"双向高度差须至少 {clearance:g} m")
        self.state["aircraft"] = {
            "manufacturer": str(payload["manufacturer"]), "model": str(payload["model"]), "cruise_speed_mps": cruise,
            "max_speed_mps": maximum, "mtbf_h": mtbf, "lambda_per_hour": 1 / mtbf,
            "route_id": payload.get("route_id") or None, "source": payload.get("source") or "用户输入",
        }
        self.state["rules"] = {
            "height_ab_m": height_ab, "height_ba_m": height_ba, "height_mode": payload.get("height_mode", "different"),
            "horizontal_separation_m": separation, "direction_rule": payload.get("direction_rule", "按航向"),
            "delay_sensor_to_platform_ms": delay_sensor, "delay_platform_to_aircraft_ms": delay_command,
            "total_delay_ms": total_delay_ms, "reaction_distance_m": round(reaction_distance_m, 3),
            "status": "passed" if valid else "failed", "message": message,
        }
        self.state["risks"]["technical"] = self._assessment("pending_confirmation", "MTBF 与 λ 已计算，技术风险模型和阈值待确认")
        self.invalidate("rules")
        self.save()
        return self.snapshot()

    def set_devices(self, devices):
        if not isinstance(devices, list):
            raise ValueError("设备清单格式无效")
        clean = []
        for item in devices:
            if item.get("subsystem") not in ("C", "N", "S") or item.get("role") not in ("primary", "gap"):
                raise ValueError("设备分系统或角色无效")
            radius, mtbf = float(item["radius_m"]), float(item["mtbf"])
            if radius <= 0 or mtbf <= 0:
                raise ValueError("设备覆盖半径和 MTBF 必须大于零")
            clean.append({**item, "radius_m": radius, "mtbf": mtbf, "mtbf_h": mtbf, "enabled": bool(item.get("enabled", True))})
        self.state["devices"] = clean
        self.invalidate("devices")
        self.save()
        return self.snapshot()

    def plan_coverage(self):
        if not self.state["rules"] or self.state["rules"]["status"] != "passed":
            raise ValueError("请先保存并通过运行规则校验")
        if self.state["result_statuses"].get("routes") != "passed":
            raise ValueError("运行航路已失效，请先重新生成运行航路")
        if not self.state["operational_routes"] or any(item.get("status") != "passed" for item in self.state["operational_routes"]):
            raise ValueError("没有可用于布站的有效运行航路")
        self.state["coverage"] = self.coverage_planner.plan(self.state["operational_routes"], self.state["devices"])
        self.state["result_statuses"]["coverage"] = self.state["coverage"]["status"]
        self.state["result_statuses"]["report"] = "not_calculated"
        self.save()
        return self.snapshot()

    def invalidate(self, changed):
        ledger = ResultLedger()
        for name, status in self.state["result_statuses"].items():
            try:
                ledger.statuses[name] = ResultStatus(status)
            except ValueError:
                ledger.statuses[name] = ResultStatus.NOT_CALCULATED
        affected = ledger.invalidate(changed)
        for name in affected:
            if name in self.state["result_statuses"]:
                self.state["result_statuses"][name] = ledger.statuses[name].value
        if "coverage" in affected and self.state.get("coverage"):
            self.state["coverage"]["status"] = "stale"

    def review(self):
        risks = self.state["risks"]
        statuses = [risks[name]["status"] for name in ("environment", "technical", "life", "property")]
        if "failed" in statuses:
            overall = "failed"
        elif all(value in ("passed", "not_applicable") for value in statuses):
            overall = "passed"
        elif "stale" in statuses:
            overall = "stale"
        elif "missing_data" in statuses:
            overall = "missing_data"
        elif "pending_confirmation" in statuses:
            overall = "pending_confirmation"
        else:
            overall = "not_calculated"
        return {"risks": deepcopy(risks), "overall_status": overall, "overall_pass": True if overall == "passed" else False if overall == "failed" else None}

    def export_project(self):
        return json.dumps(self.snapshot(), ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")

    def export_routes(self):
        features = [{"type": "Feature", "properties": {key: route.get(key) for key in ("route_id", "status", "reason", "algorithm_id", "algorithm_version")},
                     "geometry": {"type": "LineString", "coordinates": route.get("path", [])}} for route in self.state["operational_routes"]]
        return json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False, indent=2).encode("utf-8")

    def export_sites(self):
        layers = (self.state.get("coverage") or {}).get("layers", {})
        features = []
        for subsystem, layer in layers.items():
            for station in layer.get("stations", []):
                properties = {key: value for key, value in station.items() if key != "coordinate"}
                properties["subsystem"] = subsystem
                features.append({"type": "Feature", "properties": properties, "geometry": {"type": "Point", "coordinates": station["coordinate"]}})
        return json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False, indent=2).encode("utf-8")
