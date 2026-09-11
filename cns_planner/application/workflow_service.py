"""Thin six-step workflow orchestrator with backward-compatible public methods."""

from copy import deepcopy
from pathlib import Path

from ..algorithms.registry import (
    ALGORITHM_TYPES, build_default_algorithm_registry, normalize_algorithm_selection,
)
from ..risk.v1 import RiskModelV1
from ..data.mapping.conflict import ConflictGridService
from ..data.mapping.traffic import TrafficGridService
from ..algorithms.grid.service import WorkspaceGridService
from ..simulation.conflict_detector import ConflictDetector
from ..simulation.traffic_simulator import TrafficSimulator
from ..gis.cns_input_adapter import CNSInputAdapter
from ..gap.v1 import CNSGapAnalyzerV1
from .cns_input_service import CNSInputService
from .gap_analysis_service import GapAnalysisService
from .cns_planning_service import CNSPlanningService
from .export_service import ExportService
from .invalidation_service import InvalidationService
from .operation_service import OperationService
from .project_service import ProjectService
from .project_state import SCHEMA_VERSION, assessment, blank_project, empty_extension_attribute, empty_grid_attributes
from .review_service import ReviewService
from .risk_service import RiskService
from .route_service import RouteService
from .session import WorkflowSession
from .workspace_service import WorkspaceService


class WorkflowService:
    schema_version = SCHEMA_VERSION
    mapped_grid_attribute_names = RiskService.MAPPED_ATTRIBUTES
    extension_grid_attribute_names = RiskService.EXTENSION_ATTRIBUTES

    def __init__(self, store_path: Path, defaults_path: Path, risk_model=None, gap_analyzer=None, algorithm_registry=None):
        self.grid_service = WorkspaceGridService()
        self.session = WorkflowSession(store_path, defaults_path, self.grid_service)
        self.store_path, self.defaults_path = self.session.store_path, self.session.defaults_path
        self.repository, self.defaults, self.state = self.session.repository, self.session.defaults, self.session.state
        self.algorithm_registry = algorithm_registry or build_default_algorithm_registry(self.defaults)
        self.route_planner = self._selected_algorithm("route_planner")
        self.coverage_planner = self._selected_algorithm("coverage_planner")
        self.risk_model = risk_model or self._selected_algorithm("risk_model")
        self.gap_analyzer = gap_analyzer or self._selected_algorithm("cns_gap_analyzer")
        self.traffic_simulator, self.conflict_detector = TrafficSimulator(), ConflictDetector()
        self.traffic_grid_service, self.conflict_grid_service = TrafficGridService(), ConflictGridService()
        self.invalidation_service = InvalidationService(self.session)
        snapshot = self.snapshot
        self.cns_input_service = CNSInputService(self.session, self.invalidation_service, CNSInputAdapter(), snapshot)
        self.cns_input_service.ensure_catalogs()
        self.gap_analysis_service = GapAnalysisService(self.session, self.gap_analyzer, snapshot)
        self.project_service = ProjectService(self.session, snapshot)
        self.workspace_service = WorkspaceService(self.session, self.grid_service, self.invalidation_service, snapshot)
        self.route_service = RouteService(self.session, self.route_planner, self.invalidation_service, snapshot)
        self.operation_service = OperationService(self.session, self.invalidation_service, snapshot)
        self.risk_service = RiskService(
            self.session, self.risk_model, self.traffic_simulator, self.conflict_detector,
            self.traffic_grid_service, self.conflict_grid_service, self.invalidation_service,
            snapshot, lambda: deepcopy(self.state["algorithm_selection"]["risk_model"]["parameters"]),
        )
        self.cns_planning_service = CNSPlanningService(self.session, self.coverage_planner, self.invalidation_service, snapshot)
        self.export_service = ExportService(self.session, snapshot)

    def save(self): self.session.save()

    def snapshot(self):
        result = deepcopy(self.state)
        result["steps"] = self._steps()
        result["defaults"] = deepcopy(self.defaults)
        result["device_source"] = self.state.get("device_catalog", {}).get("source") or self.defaults.get("device_library", {}).get("source", "demo/default")
        result["aircraft_source"] = self.state.get("aircraft_profiles", {}).get("source") or self.defaults.get("aircraft_library", {}).get("source", "demo/default")
        result["algorithm_catalog"] = self.algorithm_registry.catalog()
        result["review"] = self.review()
        return result

    def grid_snapshot(self): return deepcopy(self.state.get("grid") or self.grid_service.empty())
    def grid_attributes_snapshot(self): return deepcopy(self.state.get("grid_attributes") or empty_grid_attributes())
    def grid_risk_snapshot(self): return deepcopy(self.state.get("grid_risk") or RiskModelV1.empty())
    def aircraft_profiles_snapshot(self): return deepcopy(self.state.get("aircraft_profiles") or {})
    def device_catalog_snapshot(self): return deepcopy(self.state.get("device_catalog") or {})
    def required_cns_snapshot(self): return deepcopy(self.state.get("required_cns") or {})
    def existing_cns_snapshot(self): return deepcopy(self.state.get("existing_cns_facilities") or {})
    def candidate_sites_snapshot(self): return deepcopy(self.state.get("candidate_sites") or {})
    def cns_gap_snapshot(self): return deepcopy(self.state.get("cns_gap_analysis") or CNSGapAnalyzerV1.empty())
    def algorithms_snapshot(self):
        return {
            "status": "passed", "selection": deepcopy(self.state["algorithm_selection"]),
            "items": self.algorithm_registry.catalog(),
        }
    def _blank(self): return blank_project(self.defaults)
    _assessment = staticmethod(assessment)
    _empty_grid_attributes = staticmethod(empty_grid_attributes)
    _empty_extension_attribute = staticmethod(empty_extension_attribute)

    def _selected_algorithm(self, algorithm_type):
        selection = self.state["algorithm_selection"][algorithm_type]
        return self.algorithm_registry.create(
            selection["algorithm_type"], selection["algorithm_id"],
            selection["version"], selection["parameters"],
        )

    def select_algorithm(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("算法选择格式无效")
        algorithm_type = str(payload.get("algorithm_type") or "")
        if algorithm_type not in ALGORITHM_TYPES:
            raise ValueError(f"不支持的算法类型：{algorithm_type}")
        version = payload.get("version", payload.get("algorithm_version"))
        candidate = normalize_algorithm_selection({
            **self.state["algorithm_selection"],
            algorithm_type: {
                "algorithm_type": algorithm_type,
                "algorithm_id": payload.get("algorithm_id"),
                "version": version,
                "parameters": payload.get("parameters", {}),
            },
        })[algorithm_type]
        instance = self.algorithm_registry.create(
            candidate["algorithm_type"], candidate["algorithm_id"],
            candidate["version"], candidate["parameters"],
        )
        if candidate == self.state["algorithm_selection"][algorithm_type]:
            return self.snapshot()
        self.state["algorithm_selection"][algorithm_type] = candidate
        self._bind_algorithm(algorithm_type, instance)
        if algorithm_type == "risk_model":
            self.invalidation_service.risk()
        else:
            changed = {
                "route_planner": "route_algorithm",
                "coverage_planner": "coverage_algorithm",
                "cns_gap_analyzer": "gap_algorithm",
            }[algorithm_type]
            self.invalidation_service.workflow(changed)
        self.session.save()
        return self.snapshot()

    def _bind_algorithm(self, algorithm_type, instance):
        if algorithm_type == "route_planner":
            self.route_planner = self.route_service.planner = instance
        elif algorithm_type == "coverage_planner":
            self.coverage_planner = self.cns_planning_service.planner = instance
        elif algorithm_type == "cns_gap_analyzer":
            self.gap_analyzer = self.gap_analysis_service.analyzer = instance
        elif algorithm_type == "risk_model":
            self.risk_model = self.risk_service.risk_model = instance

    def _steps(self):
        state = self.state
        workspace_ok = bool(state["workspace"] and state["workspace"].get("status") == "passed")
        scenario_ok = bool(state["scenario_routes"])
        routes_ok = scenario_ok and state["result_statuses"].get("routes") == "passed" and bool(state["operational_routes"]) and all(item["status"] == "passed" for item in state["operational_routes"])
        rules_ok = bool(state["rules"] and state["rules"].get("status") == "passed" and state["aircraft"])
        coverage_ok = bool(state["result_statuses"].get("coverage") == "passed" and state["coverage"] and state["coverage"].get("status") == "passed")
        return {"1": True, "2": workspace_ok, "3": routes_ok, "4": rules_ok, "5": coverage_ok, "6": coverage_ok}

    def set_project(self, payload): return self.project_service.set_project(payload)
    def set_workspace(self, bbox, health): return self.workspace_service.set_workspace(bbox, health)
    def clear_workspace(self): return self.workspace_service.clear_workspace()
    def add_node(self, coordinate, name=None): return self.route_service.add_node(coordinate, name)
    def delete_node(self, node_id): return self.route_service.delete_node(node_id)
    def generate_scenario(self, direction): return self.route_service.generate_scenario(direction)
    def delete_route(self, route_id): return self.route_service.delete_route(route_id)
    def generate_operational(self, constraints): return self.route_service.generate_operational(constraints)
    def set_rules(self, payload): return self.operation_service.set_rules(payload)
    def select_aircraft_profile(self, aircraft_id): return self.cns_input_service.select_aircraft(aircraft_id)
    def import_aircraft_catalog(self, path): return self.cns_input_service.import_aircraft_catalog(path)
    def set_required_cns(self, payload): return self.cns_input_service.set_required_cns(payload)
    def import_device_catalog(self, path): return self.cns_input_service.import_device_catalog(path)
    def import_existing_cns(self, payload): return self.cns_input_service.import_existing(payload)
    def import_candidate_sites(self, payload): return self.cns_input_service.import_candidates(payload)
    def candidate_sites_from_existing(self): return self.cns_input_service.candidates_from_existing()
    def analyze_cns_gaps(self): return self.gap_analysis_service.analyze()
    def select_registered_algorithm(self, payload): return self.select_algorithm(payload)
    def set_devices(self, devices): return self.cns_planning_service.set_devices(devices)
    def plan_coverage(self): return self.cns_planning_service.plan_coverage()
    def apply_grid_attributes(self, results): return self.risk_service.apply_grid_attributes(results)
    def update_data_source_profiles(self, profiles): return self.risk_service.update_source_profiles(profiles)
    def evaluate_grid_risk(self, parameters=None): return self.risk_service.evaluate(parameters)
    def run_traffic_simulation(self, parameters): return self.risk_service.run_traffic_simulation(parameters)
    def invalidate_grid_attributes(self, changed): return self.risk_service.invalidate_grid_attributes(changed)
    def _apply_grid_risk(self, result): return self.risk_service.apply_result(result)
    def _invalidate_grid_risk(self): return self.invalidation_service.risk()
    def invalidate(self, changed): return self.invalidation_service.workflow(changed)
    def review(self): return ReviewService().review(self.state)
    def export_project(self): return self.export_service.project()
    def export_routes(self): return self.export_service.routes()
    def export_sites(self): return self.export_service.sites()
