"""Thin six-step workflow orchestrator with backward-compatible public methods."""

from copy import deepcopy
from pathlib import Path

from ..algorithms.registry import (
    ALGORITHM_TYPES, build_default_algorithm_registry, normalize_algorithm_selection,
)
from ..risk.v1 import RiskModelV1
from ..risk.model_v2 import GridRiskModelV2
from ..data.mapping.conflict import ConflictGridService
from ..data.mapping.traffic import TrafficGridService
from ..algorithms.grid.service import WorkspaceGridService
from ..simulation.conflict_detector import ConflictDetector
from ..simulation.traffic_simulator import TrafficSimulator
from ..gis.cns_input_adapter import CNSInputAdapter
from ..gap.v1 import CNSGapAnalyzerV1
from ..gap.v2 import CNSGapAnalyzerV2
from .cns_input_service import CNSInputService
from .gap_analysis_service import GapAnalysisService
from .gap_analysis_v2_service import GapAnalysisV2Service
from .cns_planning_service import CNSPlanningService
from .export_service import ExportService
from .invalidation_service import InvalidationService
from .operation_service import OperationService
from .project_service import ProjectService
from .project_state import SCHEMA_VERSION, assessment, blank_project, empty_extension_attribute, empty_grid_attributes
from .review_service import ReviewService
from .risk_service import RiskService
from .risk_v2_service import RiskFrameworkV2Service
from .layered_route_planner_service import LayeredRoutePlannerService
from .route_risk_profile_service import RouteRiskProfileService
from .route_service import RouteService
from .safety_policy_service import SafetyPolicyService
from .session import WorkflowSession
from .workspace_service import WorkspaceService
from .spatial_3d_service import Spatial3DService
from .route_operating_layer_service import RouteOperatingLayerService
from .cns_service_capability_service import CNSServiceCapabilityService
from .operational_timing_service import OperationalTimingService
from .site_planning_service import SitePlanningService
from .closed_loop_service import ClosedLoopService
from .corridor_service import CNSCorridorService
from .corridor_gap_service import CNSCorridorGapService
from .corridor_site_planning_service import CorridorSitePlanningService
from .requirement_recommendation_service import RequirementRecommendationService
from .plan_review_service import PlanReviewService
from .report_service import PlanningReportService
from .reference_data_service import ReferenceDataService
from .building_clearance_service import BuildingClearanceService
from ..algorithms.building_clearance import BuildingClearanceV1
from .route_vertical_profile_service import RouteVerticalProfileService
from .reference_link_service import ReferenceLinkService
from .route_experiment_service import RoutePlanningExperimentService
from .route_planner_v3_service import RoutePlannerV3ExperimentService, record_summary as _v3_record_summary
from .source_audit_service import SourceAuditService
from .v3_operational_adoption_service import V3OperationalAdoptionService
from ..algorithms.route_vertical_profile import RouteVerticalProfileV1
from .encounter_3d_service import Encounter3DService
from ..algorithms.encounter_3d import EncounterAssessment3DV1
from ..domain.airspace import normalize_airspace_policies
from ..site_planner.reuse_first_v1 import ReuseFirstSitePlannerV1
from ..site_planner.corridor_reuse_first_v2 import CorridorReuseFirstSitePlannerV2


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
        selected_gap_analyzer = gap_analyzer or self._selected_algorithm("cns_gap_analyzer")
        self.gap_analyzer = (
            selected_gap_analyzer
            if getattr(selected_gap_analyzer, "algorithm_id", None) != CNSGapAnalyzerV2.algorithm_id
            else self.algorithm_registry.create(
                "cns_gap_analyzer", CNSGapAnalyzerV1.algorithm_id,
                CNSGapAnalyzerV1.algorithm_version, {},
            )
        )
        self.gap_analyzer_v2 = (
            selected_gap_analyzer
            if getattr(selected_gap_analyzer, "algorithm_id", None) == CNSGapAnalyzerV2.algorithm_id
            else self.algorithm_registry.create(
                "cns_gap_analyzer", CNSGapAnalyzerV2.algorithm_id,
                CNSGapAnalyzerV2.algorithm_version,
                (self.state.get("cns_gap_analysis_v2") or {}).get("parameters") or {},
            )
        )
        self.coverage_model_3d = self._selected_algorithm("coverage_model")
        self.cns_service_model = self._selected_algorithm("service_model")
        self.timeline_model = self._selected_algorithm("timeline_model")
        self.protection_model = self._selected_algorithm("protection_model")
        selected_site_planner = self._selected_algorithm("site_planner")
        self.site_planner = (
            selected_site_planner
            if getattr(selected_site_planner, "algorithm_id", None) == ReuseFirstSitePlannerV1.algorithm_id
            else self.algorithm_registry.create(
                "site_planner", ReuseFirstSitePlannerV1.algorithm_id,
                ReuseFirstSitePlannerV1.algorithm_version, {},
            )
        )
        self.corridor_site_planner = (
            selected_site_planner
            if getattr(selected_site_planner, "algorithm_id", None) == CorridorReuseFirstSitePlannerV2.algorithm_id
            else self.algorithm_registry.create(
                "site_planner", CorridorReuseFirstSitePlannerV2.algorithm_id,
                CorridorReuseFirstSitePlannerV2.algorithm_version,
                (self.state.get("cns_corridor_site_plan") or {}).get("parameters") or {},
            )
        )
        self.corridor_model = self._selected_algorithm("corridor_model")
        self.corridor_gap_analyzer = self._selected_algorithm("corridor_gap_analyzer")
        self.requirement_model = self._selected_algorithm("requirement_model")
        self.traffic_simulator, self.conflict_detector = TrafficSimulator(), ConflictDetector()
        self.traffic_grid_service, self.conflict_grid_service = TrafficGridService(), ConflictGridService()
        self.invalidation_service = InvalidationService(self.session)
        snapshot = self.snapshot
        self.safety_policy_service = SafetyPolicyService(
            self.session, self.invalidation_service, snapshot
        )
        self.cns_input_service = CNSInputService(self.session, self.invalidation_service, CNSInputAdapter(), snapshot)
        self.cns_input_service.ensure_catalogs()
        self.requirement_recommendation_service = RequirementRecommendationService(
            self.session, self.requirement_model, self.invalidation_service, snapshot,
        )
        self.gap_analysis_service = GapAnalysisService(self.session, self.gap_analyzer, snapshot)
        self.gap_analysis_v2_service = GapAnalysisV2Service(
            self.session, self.gap_analyzer_v2, self.invalidation_service, snapshot
        )
        self.project_service = ProjectService(self.session, snapshot)
        self.workspace_service = WorkspaceService(self.session, self.grid_service, self.invalidation_service, snapshot)
        self.route_service = RouteService(self.session, self.route_planner, self.invalidation_service, snapshot)
        self.reference_data_service = ReferenceDataService(
            self.session, self.route_service, snapshot,
        )
        self.reference_data_service.ensure_equipment_catalog()
        self.reference_link_service = ReferenceLinkService(
            self.session, self.invalidation_service, snapshot,
        )
        self.route_experiment_service = RoutePlanningExperimentService(
            self.session, self.route_service, self.algorithm_registry,
            self.invalidation_service, snapshot,
        )
        self.route_planner_v3_service = RoutePlannerV3ExperimentService(
            self.session, None, self.invalidation_service, snapshot, self.grid_service,
        )
        self.source_audit_service = SourceAuditService(
            self.session, self.invalidation_service, snapshot,
        )
        self.operation_service = OperationService(self.session, self.invalidation_service, snapshot)
        self.risk_service = RiskService(
            self.session, self.risk_model, self.traffic_simulator, self.conflict_detector,
            self.traffic_grid_service, self.conflict_grid_service, self.invalidation_service,
            snapshot, lambda: deepcopy(self.state["algorithm_selection"]["risk_model"]["parameters"]),
        )
        # Additive Risk Framework V2: factor → ground/air_traffic/environment_obstacle
        # domains.  It owns ``grid_risk_v2`` / ``risk_policy_v2`` only; the current
        # planner keeps consuming the legacy Risk V1 ``grid_risk``.
        self.risk_v2_service = RiskFrameworkV2Service(
            self.session, GridRiskModelV2(), self.invalidation_service, snapshot,
        )
        # Layered Risk-Aware Route Planner V1 (production main line): explicit altitude layer
        # selection, terrain/building coarse feasibility mask, single-layer MH/T L8 A* with
        # Risk Framework V2 soft cost, and an independent candidate container.  It never
        # writes ``operational_routes`` / CNS results and never switches the project's default
        # route planner.
        self.layered_route_planner_service = LayeredRoutePlannerService(
            self.session, self.invalidation_service, snapshot,
        )
        # A Risk Framework V2 / layer / terrain-building / policy change stales only the
        # layered candidates and masks, never legacy routes, V3 or CNS results.
        self.invalidation_service.layered_route_invalidator = (
            self.layered_route_planner_service.refresh_for_reason
        )
        # Resolve the registered layered planner (its own algorithm type; the project's
        # ``route_planner`` default stays ``route_planner_v1``).
        self.layered_route_planner = self._selected_algorithm("layered_route_planner")
        self.layered_route_planner_service.planner = self.layered_route_planner
        # RouteRiskProfile V1 (additive, analysis only): current LayeredRouteCandidate +
        # current GridRiskV2 → path risk profile.  It never replans, never mutates the
        # candidate and never writes operational_routes / CNS / RouteOperatingLayer.
        self.route_risk_profile_service = RouteRiskProfileService(
            self.session, self.invalidation_service, snapshot,
            self.layered_route_planner_service,
        )
        # A candidate / grid_risk_v2 / profile-policy change stales only the additive
        # route_risk_profiles; legacy routes, V3 and CNS results stay untouched.
        self.invalidation_service.route_risk_profile_invalidator = (
            self.route_risk_profile_service.refresh_for_reason
        )
        self.cns_planning_service = CNSPlanningService(self.session, self.coverage_planner, self.invalidation_service, snapshot)
        self.spatial_3d_service = Spatial3DService(
            self.session, self.coverage_model_3d, self.invalidation_service, snapshot
        )
        # Layered Operational Route Architecture V1: fixed cruise layer catalogue, explicit
        # route → layer assignments and departure/arrival procedure contracts.
        self.route_operating_layer_service = RouteOperatingLayerService(
            self.session, self.invalidation_service, snapshot
        )
        self.cns_service_capability_service = CNSServiceCapabilityService(
            self.session, self.cns_service_model, self.invalidation_service, snapshot
        )
        self.operational_timing_service = OperationalTimingService(
            self.session, self.timeline_model, self.protection_model,
            self.invalidation_service, snapshot,
        )
        self.site_planning_service = SitePlanningService(
            self.session, self.site_planner, self.coverage_model_3d,
            self.cns_service_model, self.invalidation_service, snapshot,
        )
        self.closed_loop_service = ClosedLoopService(
            self.session, self.coverage_model_3d, self.cns_service_model,
            self.timeline_model, self.gap_analyzer_v2, snapshot,
        )
        self.corridor_service = CNSCorridorService(
            self.session, self.corridor_model, self.invalidation_service, snapshot,
        )
        self.corridor_gap_service = CNSCorridorGapService(
            self.session, self.corridor_gap_analyzer, self.invalidation_service, snapshot,
        )
        self.corridor_site_planning_service = CorridorSitePlanningService(
            self.session, self.corridor_site_planner, self.corridor_model,
            self.corridor_gap_analyzer, self.invalidation_service, snapshot,
        )
        self.plan_review_service = PlanReviewService(
            self.session, self.coverage_model_3d, self.cns_service_model,
            self.timeline_model, self.gap_analyzer_v2, self.corridor_model,
            self.corridor_gap_analyzer, snapshot,
        )
        self.export_service = ExportService(self.session, snapshot)
        self.report_service = PlanningReportService(
            self.session, self.export_service, self.algorithm_registry.catalog, snapshot,
        )
        self.building_clearance_service = BuildingClearanceService(
            self.session, BuildingClearanceV1(), self.invalidation_service, snapshot,
        )
        self.route_vertical_profile_service = RouteVerticalProfileService(
            self.session, RouteVerticalProfileV1(), self.invalidation_service, snapshot,
        )
        self.encounter_3d_service = Encounter3DService(
            self.session, EncounterAssessment3DV1(), self.invalidation_service, snapshot,
        )
        # P20 V3-D: the bridge that publishes a current V3-C validated route into the
        # existing operational route surface and reuses P7-P10 for CNS assessment.  It is
        # constructed last because it orchestrates the services above.
        self.v3_operational_adoption_service = V3OperationalAdoptionService(
            self.session, self.route_planner_v3_service, self.invalidation_service, snapshot,
            spatial_3d_service=self.spatial_3d_service,
            cns_service_capability_service=self.cns_service_capability_service,
            operational_timing_service=self.operational_timing_service,
            gap_analysis_v2_service=self.gap_analysis_v2_service,
        )
        # Source changes must stale a V3 adoption (and only V3 adoptees).
        self.source_audit_service.v3_adoption_invalidator = (
            self.v3_operational_adoption_service.stale_for_sources
        )

    def save(self): self.session.save()

    def snapshot(self):
        result = deepcopy(self.state)
        result["steps"] = self._steps()
        result["defaults"] = deepcopy(self.defaults)
        result["device_source"] = self.state.get("device_catalog", {}).get("source") or self.defaults.get("device_library", {}).get("source", "demo/default")
        result["aircraft_source"] = self.state.get("aircraft_profiles", {}).get("source") or self.defaults.get("aircraft_library", {}).get("source", "demo/default")
        result["algorithm_catalog"] = self.algorithm_registry.catalog()
        if hasattr(self, "requirement_recommendation_service"):
            result["required_cns_recommendation"] = self.requirement_recommendation_service.result_snapshot()
        if hasattr(self, "route_experiment_service"):
            result["route_planning_experiments"] = self.route_experiment_service.result_snapshot()
            result["route_planning_diagnostics"] = self.route_experiment_service.diagnostics_snapshot()
        if hasattr(self, "route_planner_v3_service"):
            # Bounded, read-only projection: the full V3 state path is served by
            # GET /api/route-planner-v3-experiments so the snapshot stays small.
            collection = self.route_planner_v3_service.result_snapshot()
            result["route_planner_v3_experiments"] = {
                "status": collection["status"],
                "count": collection["count"],
                "active_experiment_id": collection["active_experiment_id"],
                "active_experiment": _v3_record_summary(collection.get("active_experiment")),
                "records": [
                    summary for summary in
                    (_v3_record_summary(item) for item in collection.get("records") or [])
                    if summary is not None
                ],
                "architecture": collection["architecture"],
                "v3b_architecture": collection["v3b_architecture"],
                "v3c_architecture": collection.get("v3c_architecture"),
                "note": collection["note"],
                "v3b_note": collection["v3b_note"],
                "v3c_note": collection.get("v3c_note"),
                "allowed_result_statuses": collection["allowed_result_statuses"],
                "allowed_refinement_statuses": collection["allowed_refinement_statuses"],
                "allowed_validation_statuses": collection.get("allowed_validation_statuses"),
                "operational_routes_untouched": True,
                "algorithm_selection_untouched": True,
                "detail_endpoint": "/api/route-planner-v3-experiments",
            }
            result["route_planner_v3_readiness"] = self.route_planner_v3_service.readiness_snapshot()
            result["route_planner_v3_refinement_readiness"] = (
                self.route_planner_v3_service.refinement_readiness_snapshot()
            )
            result["route_planner_v3_refinements"] = (
                self.route_planner_v3_service.refinement_snapshot()
            )
            result["route_planner_v3_continuous_readiness"] = (
                self.route_planner_v3_service.continuous_readiness_snapshot()
            )
            result["route_planner_v3_validations"] = (
                self.route_planner_v3_service.continuous_validation_snapshot()
            )
            if hasattr(self, "v3_operational_adoption_service"):
                result["v3_operational_adoptions"] = (
                    self.v3_operational_adoption_service.adoptions_snapshot()
                )
                result["v3_operational_publish_status"] = (
                    self.v3_operational_adoption_service.publish_status()
                )
                result["v3_cns_assessment"] = (
                    self.v3_operational_adoption_service.cns_bundle_snapshot()
                )
        if hasattr(self, "reference_link_service"):
            result["data_readiness"] = self.reference_link_service.data_readiness()
        if hasattr(self, "route_operating_layer_service"):
            # Layered Operational Route Architecture V1: production route projection and
            # the four separately reported readiness buckets.
            result["route_operating_readiness"] = (
                self.route_operating_layer_service.readiness_snapshot()
            )
            result["route_operating_plan"] = self.route_operating_layer_service.plan_snapshot()
        if hasattr(self, "source_audit_service"):
            result["source_audits"] = self.source_audit_service.result_snapshot()
        if hasattr(self, "risk_v2_service"):
            result["grid_risk_v2"] = self.risk_v2_service.result_snapshot()
            result["risk_policy_v2"] = self.risk_v2_service.policy_snapshot()
            result["risk_framework_v2_readiness"] = self.risk_v2_service.readiness_snapshot()
        if hasattr(self, "layered_route_planner_service"):
            # Layered Risk-Aware Route Planner V1: only the bounded readiness, the explicit
            # request/policies and the candidate summary travel in the snapshot; the full mask
            # detail is served by the dedicated read-only endpoint.
            result["layered_route_planning_request"] = (
                self.layered_route_planner_service.request_snapshot()
            )
            result["layered_route_feasibility_policy"] = (
                self.layered_route_planner_service.feasibility_policy_snapshot()
            )
            result["layered_route_cost_policy"] = (
                self.layered_route_planner_service.cost_policy_snapshot()
            )
            result["layered_route_planner_readiness"] = (
                self.layered_route_planner_service.readiness_snapshot()
            )
            result["layered_route_candidates"] = (
                self.layered_route_planner_service.result_snapshot()
            )
        if hasattr(self, "route_risk_profile_service"):
            # RouteRiskProfile V1: the explicit per-domain thresholds, the bounded readiness
            # and the independent profile container travel in the snapshot.
            result["route_risk_profile_policy"] = (
                self.route_risk_profile_service.policy_snapshot()
            )
            result["route_risk_profiles"] = (
                self.route_risk_profile_service.result_snapshot()
            )
            result["route_risk_profile_readiness"] = (
                self.route_risk_profile_service.readiness_snapshot()
            )
        result["review"] = self.review()
        return result

    def grid_snapshot(self): return deepcopy(self.state.get("grid") or self.grid_service.empty())
    def grid_attributes_snapshot(self): return deepcopy(self.state.get("grid_attributes") or empty_grid_attributes())
    def grid_risk_snapshot(self): return deepcopy(self.state.get("grid_risk") or RiskModelV1.empty())
    # ---- Risk Framework V2 (additive) -----------------------------------------
    def grid_risk_v2_snapshot(self): return self.risk_v2_service.result_snapshot()
    def risk_policy_v2_snapshot(self): return self.risk_v2_service.policy_snapshot()
    def risk_framework_v2_readiness(self): return self.risk_v2_service.readiness_snapshot()
    def set_risk_policy_v2(self, payload): return self.risk_v2_service.set_policy(payload)
    def evaluate_grid_risk_v2(self, payload=None): return self.risk_v2_service.evaluate(payload)
    def aircraft_profiles_snapshot(self): return deepcopy(self.state.get("aircraft_profiles") or {})
    def device_catalog_snapshot(self): return deepcopy(self.state.get("device_catalog") or {})
    def reference_landing_sites_snapshot(self): return self.reference_data_service.landing_sites_snapshot()
    def reference_routes_snapshot(self): return self.reference_data_service.routes_snapshot()
    def airspace_policies_snapshot(self): return deepcopy(self.state.get("airspace_policies") or {})
    def equipment_reference_catalog_snapshot(self): return self.reference_data_service.equipment_catalog_snapshot()
    def required_cns_snapshot(self): return deepcopy(self.state.get("required_cns") or {})
    def cns_operation_context_snapshot(self): return self.requirement_recommendation_service.context_snapshot()
    def cns_requirement_policies_snapshot(self): return self.requirement_recommendation_service.policies_snapshot()
    def required_cns_recommendation_snapshot(self): return self.requirement_recommendation_service.result_snapshot()
    def existing_cns_snapshot(self): return deepcopy(self.state.get("existing_cns_facilities") or {})
    def candidate_sites_snapshot(self): return deepcopy(self.state.get("candidate_sites") or {})
    def cns_gap_snapshot(self): return deepcopy(self.state.get("cns_gap_analysis") or CNSGapAnalyzerV1.empty())
    def cns_gap_v2_snapshot(self): return self.gap_analysis_v2_service.result_snapshot()
    def spatial_3d_snapshot(self): return self.spatial_3d_service.spatial_snapshot()
    def coverage_3d_snapshot(self): return self.spatial_3d_service.coverage_snapshot()
    def cns_service_capability_snapshot(self): return self.cns_service_capability_service.capability_snapshot()
    def operational_timing_snapshot(self): return self.operational_timing_service.timing_snapshot()
    def service_timeline_snapshot(self): return self.operational_timing_service.timeline_snapshot()
    def protection_envelope_snapshot(self): return self.operational_timing_service.protection_snapshot()
    def cns_site_plan_snapshot(self): return self.site_planning_service.result_snapshot()
    def closed_loop_snapshot(self): return self.closed_loop_service.result_snapshot()
    def cns_corridor_snapshot(self): return self.corridor_service.result_snapshot()
    def cns_planning_objectives_snapshot(self): return self.corridor_gap_service.objectives_snapshot()
    def cns_corridor_gap_snapshot(self): return self.corridor_gap_service.result_snapshot()
    def cns_corridor_site_plan_snapshot(self): return self.corridor_site_planning_service.result_snapshot()
    def cns_plan_review_snapshot(self): return self.plan_review_service.snapshot_result()
    def cns_planning_report_snapshot(self): return self.report_service.result_snapshot()
    def safety_policy_snapshot(self): return self.safety_policy_service.policy_snapshot()
    def building_clearance_policy_snapshot(self): return self.building_clearance_service.policy_snapshot()
    def building_clearance_snapshot(self): return self.building_clearance_service.assessment_snapshot()
    def route_vertical_profiles_snapshot(self): return self.route_vertical_profile_service.result_snapshot()
    def reference_route_links_snapshot(self): return self.reference_link_service.links_snapshot()
    def reference_endpoint_candidates_snapshot(self): return self.reference_link_service.endpoint_candidates_snapshot()
    def route_experiments_snapshot(self): return self.route_experiment_service.result_snapshot()
    def route_planner_v3_snapshot(self): return self.route_planner_v3_service.result_snapshot()
    def route_planner_v3_readiness(self): return self.route_planner_v3_service.readiness_snapshot()
    def route_planner_v3_refinement_readiness(self):
        return self.route_planner_v3_service.refinement_readiness_snapshot()
    def route_planner_v3_refinement_snapshot(self):
        return self.route_planner_v3_service.refinement_snapshot()
    def set_route_planner_v3_policy(self, payload): return self.route_planner_v3_service.set_policy(payload)
    def set_route_planner_v3_fine_policy(self, payload):
        return self.route_planner_v3_service.set_fine_policy(payload)
    def evaluate_route_planner_v3(self, payload=None): return self.route_planner_v3_service.evaluate(payload)
    def evaluate_route_planner_v3_refinement(self, payload=None):
        return self.route_planner_v3_service.evaluate_refinement(payload)
    def evaluate_route_planner_v3_refinement_with_adapter(self, adapter, payload=None):
        """GIS-wired entry point used by ApplicationContext (needs QGIS/GDAL)."""

        return self.route_planner_v3_service.evaluate_refinement(payload, adapter=adapter)
    def delete_route_planner_v3_experiment(self, experiment_id):
        return self.route_planner_v3_service.delete_experiment(experiment_id)

    # ---- V3-C -----------------------------------------------------------------
    def route_planner_v3_continuous_readiness(self):
        return self.route_planner_v3_service.continuous_readiness_snapshot()

    def route_planner_v3_validation_policy(self):
        return self.route_planner_v3_service.validation_policy_snapshot()

    def route_planner_v3_validation_snapshot(self):
        return self.route_planner_v3_service.continuous_validation_snapshot()

    def set_route_planner_v3_validation_policy(self, payload):
        return self.route_planner_v3_service.set_validation_policy(payload)

    def evaluate_route_planner_v3_continuous_validation(self, payload=None):
        return self.route_planner_v3_service.evaluate_continuous_validation(payload)

    def evaluate_route_planner_v3_continuous_validation_with_evidence(self, evidence_adapter, payload=None):
        return self.route_planner_v3_service.evaluate_continuous_validation(
            payload, evidence_adapter=evidence_adapter,
        )

    # ---- V3-D: operational adoption + CNS assessment bridge --------------------
    def v3_operational_adoptions_snapshot(self):
        return self.v3_operational_adoption_service.adoptions_snapshot()

    def v3_operational_publish_status(self):
        return self.v3_operational_adoption_service.publish_status()

    def preview_v3_operational_adoption(self, payload=None):
        return self.v3_operational_adoption_service.preview(payload)

    def apply_v3_operational_adoption(self, payload=None):
        return self.v3_operational_adoption_service.apply(payload)

    def revoke_v3_operational_adoption(self, payload=None):
        return self.v3_operational_adoption_service.revoke(payload)

    def v3_cns_assessment_snapshot(self):
        return self.v3_operational_adoption_service.cns_bundle_snapshot()

    def assess_v3_adopted_route(self, payload=None):
        return self.v3_operational_adoption_service.assess_route(payload)
    def data_readiness_snapshot(self): return self.reference_link_service.data_readiness()
    def source_audits_snapshot(self): return self.source_audit_service.result_snapshot()
    def encounter_3d_snapshot(self): return self.encounter_3d_service.result_snapshot()
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
        elif algorithm_type == "cns_gap_analyzer" and getattr(instance, "algorithm_id", None) == CNSGapAnalyzerV2.algorithm_id:
            self.invalidation_service.cns_gap_v2()
        else:
            changed = {
                "route_planner": "route_algorithm",
                "layered_route_planner": "layered_route_planner_algorithm",
                "coverage_planner": "coverage_algorithm",
                "cns_gap_analyzer": "gap_algorithm",
                "coverage_model": "coverage_model",
                "service_model": "service_model",
                "timeline_model": "timeline_model",
                "protection_model": "protection_model",
                "site_planner": "site_planner",
                "corridor_model": "corridor_model",
                "corridor_gap_analyzer": "corridor_gap_analyzer",
                "requirement_model": "requirement_model",
            }[algorithm_type]
            self.invalidation_service.workflow(changed)
        self.session.save()
        return self.snapshot()

    def _bind_algorithm(self, algorithm_type, instance):
        if algorithm_type == "route_planner":
            self.route_planner = self.route_service.planner = instance
        elif algorithm_type == "layered_route_planner":
            # The layered planner owns its own candidate/mask products; it never becomes the
            # project's ``route_planner`` and never writes operational routes.
            self.layered_route_planner = instance
            self.layered_route_planner_service.planner = instance
        elif algorithm_type == "coverage_planner":
            self.coverage_planner = self.cns_planning_service.planner = instance
        elif algorithm_type == "cns_gap_analyzer":
            if getattr(instance, "algorithm_id", None) == CNSGapAnalyzerV2.algorithm_id:
                self.gap_analyzer_v2 = self.gap_analysis_v2_service.analyzer = instance
                self.closed_loop_service.gap_model = instance
                self.plan_review_service.gap_model = instance
            else:
                self.gap_analyzer = self.gap_analysis_service.analyzer = instance
        elif algorithm_type == "risk_model":
            self.risk_model = self.risk_service.risk_model = instance
        elif algorithm_type == "coverage_model":
            self.coverage_model_3d = self.spatial_3d_service.model = instance
            self.site_planning_service.coverage_model = instance
            self.closed_loop_service.coverage_model = instance
            self.plan_review_service.coverage_model = instance
        elif algorithm_type == "service_model":
            self.cns_service_model = self.cns_service_capability_service.model = instance
            self.site_planning_service.capability_model = instance
            self.closed_loop_service.capability_model = instance
            self.plan_review_service.capability_model = instance
        elif algorithm_type == "timeline_model":
            self.timeline_model = self.operational_timing_service.timeline_model = instance
            self.closed_loop_service.timeline_model = instance
            self.plan_review_service.timeline_model = instance
        elif algorithm_type == "protection_model":
            self.protection_model = self.operational_timing_service.protection_model = instance
        elif algorithm_type == "site_planner":
            if getattr(instance, "algorithm_id", None) == CorridorReuseFirstSitePlannerV2.algorithm_id:
                self.corridor_site_planner = self.corridor_site_planning_service.planner = instance
            else:
                self.site_planner = self.site_planning_service.planner = instance
        elif algorithm_type == "corridor_model":
            self.corridor_model = self.corridor_service.model = instance
            self.corridor_site_planning_service.corridor_model = instance
            self.plan_review_service.corridor_model = instance
        elif algorithm_type == "corridor_gap_analyzer":
            self.corridor_gap_analyzer = self.corridor_gap_service.analyzer = instance
            self.corridor_site_planning_service.corridor_gap_analyzer = instance
            self.plan_review_service.corridor_gap_analyzer = instance
        elif algorithm_type == "requirement_model":
            self.requirement_model = self.requirement_recommendation_service.model = instance

    def _steps(self):
        state = self.state
        workspace_ok = bool(state["workspace"] and state["workspace"].get("status") == "passed")
        scenario_ok = bool(state["scenario_routes"])
        routes_ok = scenario_ok and state["result_statuses"].get("routes") == "passed" and bool(state["operational_routes"]) and all(item["status"] == "passed" for item in state["operational_routes"])
        rules_ok = bool(state["rules"] and state["rules"].get("status") == "passed" and state["aircraft"])
        coverage_ok = bool(state["result_statuses"].get("coverage") == "passed" and state["coverage"] and state["coverage"].get("status") == "passed")
        return {"1": True, "2": workspace_ok, "3": routes_ok, "4": rules_ok, "5": coverage_ok, "6": coverage_ok}

    def set_project(self, payload): return self.project_service.set_project(payload)
    def set_workspace(self, bbox, health, preferred_grid_level=None): return self.workspace_service.set_workspace(bbox, health, preferred_grid_level)
    def clear_workspace(self): return self.workspace_service.clear_workspace()
    def add_node(self, coordinate, name=None): return self.route_service.add_node(coordinate, name)
    def import_reference_landing_sites(self, path): return self.reference_data_service.import_landing_sites(path)
    def import_reference_routes(self, path): return self.reference_data_service.import_routes(path)
    def preview_reference_routes(self, path, conversion=None):
        return self.reference_data_service.preview_routes(path, conversion)
    def confirm_reference_routes_import(self, path, preview_id):
        return self.reference_data_service.confirm_routes_import(path, preview_id)
    def confirm_reference_crs(self, role, payload):
        return self.reference_data_service.confirm_crs(role, payload)
    def verify_source(self, role, path, details=None):
        return self.source_audit_service.verify(role, path, details)
    def register_source_paths(self, paths, details=None, save=False):
        for role, path in (paths or {}).items():
            if path:
                self.source_audit_service.register_quick(
                    role, path, (details or {}).get(role), save=False,
                )
        if save:
            self.session.save()
        return self.snapshot()
    def set_airspace_policies(self, payload, *, require_evidence=True):
        previous = self.state.get("airspace_policies")
        current = normalize_airspace_policies(payload, require_evidence=require_evidence)
        if current != previous:
            self.state["airspace_policies"] = current
            airspace = (self.state.get("grid_attributes") or {}).get("airspace") or {}
            airspace["planning_applicability"] = "display_only"
            self.session.save()
        return self.snapshot()
    def set_airspace_policy(self, payload):
        if not isinstance(payload, dict) or not payload.get("feature_id"):
            raise ValueError("单项 AirspacePolicy 缺少 feature_id")
        normalized = normalize_airspace_policies(
            {"items": [payload]}, require_evidence=True,
        )["items"][0]
        existing = list((self.state.get("airspace_policies") or {}).get("items") or [])
        items = [item for item in existing if item.get("feature_id") != payload.get("feature_id")]
        items.append(normalized)
        # Untouched legacy policies may predate the evidence field.  The newly
        # saved item is validated above; backfill compatibility must not make a
        # single-item edit impossible.
        return self.set_airspace_policies({"items": items}, require_evidence=False)
    def batch_set_airspace_policies(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("批量 AirspacePolicy 请求必须是对象")
        feature_ids = payload.get("feature_ids") or []
        if not isinstance(feature_ids, list) or not feature_ids:
            raise ValueError("批量设置必须由用户明确选择 feature_ids")
        template = {
            key: deepcopy(payload.get(key)) for key in (
                "route_eligibility", "confirmed", "source", "evidence",
            )
        }
        normalized_selected = normalize_airspace_policies({"items": [
            {"feature_id": str(feature_id), **deepcopy(template)}
            for feature_id in feature_ids
        ]}, require_evidence=True)["items"]
        existing = {
            item.get("feature_id"): item
            for item in (self.state.get("airspace_policies") or {}).get("items") or []
        }
        for item in normalized_selected:
            existing[item["feature_id"]] = item
        return self.set_airspace_policies(
            {"items": list(existing.values())}, require_evidence=False,
        )
    def add_reference_landing_site(self, reference_site_id): return self.reference_data_service.add_landing_site_to_project(reference_site_id)
    def configure_reference_sources(self, paths, save=False):
        landing_path = (paths or {}).get("reference_landing_sites")
        route_path = (paths or {}).get("reference_routes")
        if landing_path:
            audit = self.source_audit_service.register_quick(
                "reference_landing_sites", landing_path,
            )
            current = self.state.get("reference_landing_sites") or {}
            # Bootstrap an empty legacy project once.  Existing imported facts
            # (and their manual CRS confirmation) are never recomputed merely
            # because the application reopened or the source file changed.
            if not current.get("items") and audit.get("status") == "configured_unverified":
                self.reference_data_service.import_landing_sites(landing_path, save=False)
        if route_path:
            self.source_audit_service.register_quick("reference_routes", route_path)
            # Configuring a converted file never replaces reference_routes.
            # The user must request a preview and explicitly confirm it.
        if save and (landing_path or route_path):
            self.session.save()
        return self.snapshot()
    def delete_node(self, node_id): return self.route_service.delete_node(node_id)
    def generate_scenario(self, direction): return self.route_service.generate_scenario(direction)
    def generate_scenario_od(self, start_node_id, end_node_id, direction="both"):
        return self.route_service.generate_scenario_od(start_node_id, end_node_id, direction)
    def delete_route(self, route_id): return self.route_service.delete_route(route_id)
    def generate_operational(self, constraints): return self.route_service.generate_operational(constraints)
    def set_rules(self, payload): return self.operation_service.set_rules(payload)
    def select_aircraft_profile(self, aircraft_id): return self.cns_input_service.select_aircraft(aircraft_id)
    def import_aircraft_catalog(self, path): return self.cns_input_service.import_aircraft_catalog(path)
    def set_required_cns(self, payload): return self.cns_input_service.set_required_cns(payload)
    def set_cns_operation_context(self, payload): return self.requirement_recommendation_service.set_context(payload)
    def set_cns_requirement_policies(self, payload): return self.requirement_recommendation_service.set_policies(payload)
    def evaluate_required_cns_recommendation(self, payload=None): return self.requirement_recommendation_service.evaluate(payload)
    def adopt_required_cns_recommendation(self, payload=None): return self.requirement_recommendation_service.adopt(payload)
    def import_device_catalog(self, path): return self.cns_input_service.import_device_catalog(path)
    def import_existing_cns(self, payload): return self.cns_input_service.import_existing(payload)
    def import_candidate_sites(self, payload): return self.cns_input_service.import_candidates(payload)
    def candidate_sites_from_existing(self): return self.cns_input_service.candidates_from_existing()
    def analyze_cns_gaps(self): return self.gap_analysis_service.analyze()
    def analyze_cns_gaps_v2(self, payload=None): return self.gap_analysis_v2_service.evaluate(payload)
    def evaluate_cns_site_plan(self, payload=None): return self.site_planning_service.evaluate(payload)
    def evaluate_closed_loop(self, payload=None): return self.closed_loop_service.evaluate(payload)
    def apply_closed_loop(self, payload=None): return self.closed_loop_service.apply(payload)
    def evaluate_cns_corridor(self, payload=None): return self.corridor_service.evaluate(payload)
    def set_cns_planning_objectives(self, payload): return self.corridor_gap_service.set_objectives(payload)
    def evaluate_cns_corridor_gap(self, payload=None): return self.corridor_gap_service.evaluate(payload)
    def evaluate_cns_corridor_site_plan(self, payload=None): return self.corridor_site_planning_service.evaluate(payload)
    def initialize_cns_plan_review(self, payload=None): return self.plan_review_service.initialize(payload)
    def create_cns_plan_variant(self, payload): return self.plan_review_service.create_variant(payload)
    def evaluate_cns_plan_variant(self, payload=None): return self.plan_review_service.evaluate(payload)
    def select_cns_plan_variant(self, payload): return self.plan_review_service.select(payload)
    def confirm_cns_plan(self, payload): return self.plan_review_service.confirm(payload)
    def apply_confirmed_cns_plan(self, payload): return self.plan_review_service.apply(payload)
    def preview_cns_planning_report(self, payload=None): return self.report_service.preview(payload)
    def generate_cns_planning_report(self, payload=None): return self.report_service.generate(payload)
    def cns_planning_report_artifact(self, report_id, kind): return self.report_service.artifact(report_id, kind)
    def set_safety_policy(self, payload): return self.safety_policy_service.set_policy(payload)
    def set_building_clearance_policy(self, payload): return self.building_clearance_service.set_policy(payload)
    def evaluate_building_clearance(self, adapter): return self.building_clearance_service.evaluate(adapter)
    def evaluate_route_vertical_profiles(self, sampler, payload=None): return self.route_vertical_profile_service.evaluate(sampler, payload)
    def evaluate_route_experiment(self, payload=None): return self.route_experiment_service.evaluate(payload)
    def delete_route_experiment(self, experiment_id): return self.route_experiment_service.delete_experiment(experiment_id)
    def create_reference_route_link(self, payload): return self.reference_link_service.create_link(payload)
    def delete_reference_route_link(self, link_id): return self.reference_link_service.delete_link(link_id)
    def select_registered_algorithm(self, payload): return self.select_algorithm(payload)
    def set_devices(self, devices): return self.cns_planning_service.set_devices(devices)
    def plan_coverage(self): return self.cns_planning_service.plan_coverage()
    def set_altitude_layers(self, payload): return self.spatial_3d_service.set_altitude_layers(payload)
    def set_route_altitude_profile(self, payload): return self.spatial_3d_service.set_route_profile(payload)
    # ---- Layered Operational Route Architecture V1 ---------------------------------
    def set_altitude_layer(self, payload):
        return self.route_operating_layer_service.set_altitude_layer(payload)
    def delete_altitude_layer(self, payload):
        return self.route_operating_layer_service.delete_altitude_layer(payload)
    def set_route_operating_layer(self, payload):
        return self.route_operating_layer_service.set_route_operating_layer(payload)
    def delete_route_operating_layer(self, payload):
        return self.route_operating_layer_service.delete_route_operating_layer(payload)
    def set_departure_arrival_procedure(self, payload):
        return self.route_operating_layer_service.set_procedure(payload)
    def delete_departure_arrival_procedure(self, payload):
        return self.route_operating_layer_service.delete_procedure(payload)
    def route_operating_readiness(self):
        return self.route_operating_layer_service.readiness_snapshot()
    def route_operating_plan(self):
        return self.route_operating_layer_service.plan_snapshot()
    # ---- Layered Risk-Aware Route Planner V1 ---------------------------------------
    def layered_route_planner_readiness(self):
        return self.layered_route_planner_service.readiness_snapshot()
    def layered_route_planning_request(self):
        return self.layered_route_planner_service.request_snapshot()
    def layered_route_feasibility_policy(self):
        return self.layered_route_planner_service.feasibility_policy_snapshot()
    def layered_route_cost_policy(self):
        return self.layered_route_planner_service.cost_policy_snapshot()
    def layered_route_candidates(self):
        return self.layered_route_planner_service.result_snapshot()
    def set_layered_route_planning_request(self, payload):
        return self.layered_route_planner_service.set_planning_request(payload)
    def set_layered_route_feasibility_policy(self, payload):
        return self.layered_route_planner_service.set_feasibility_policy(payload)
    def set_layered_route_cost_policy(self, payload):
        return self.layered_route_planner_service.set_cost_policy(payload)
    def evaluate_layered_route_candidate(self, payload=None, adapter=None):
        self.layered_route_planner_service.evaluate(payload, adapter=adapter)
        # A new candidate run may have replaced/staled the previous record of a lane, so the
        # existing RouteRiskProfiles are re-checked: only profiles whose candidate is gone,
        # replaced or stale are invalidated (old profiles are kept as audit evidence).
        if hasattr(self, "route_risk_profile_service"):
            return self.route_risk_profile_service.reconcile()
        return self.snapshot()
    def delete_layered_route_candidate(self, candidate_id):
        self.layered_route_planner_service.delete_candidate(candidate_id)
        if hasattr(self, "route_risk_profile_service"):
            return self.route_risk_profile_service.reconcile("candidate_deleted")
        return self.snapshot()
    # ---- RouteRiskProfile V1 (additive analysis of a current layered candidate) -----
    def route_risk_profile_policy(self):
        return self.route_risk_profile_service.policy_snapshot()
    def route_risk_profiles(self):
        return self.route_risk_profile_service.result_snapshot()
    def route_risk_profile_readiness(self):
        return self.route_risk_profile_service.readiness_snapshot()
    def set_route_risk_profile_policy(self, payload):
        return self.route_risk_profile_service.set_policy(payload)
    def evaluate_route_risk_profile(self, payload=None):
        return self.route_risk_profile_service.evaluate(payload)
    def delete_route_risk_profile(self, profile_id):
        return self.route_risk_profile_service.delete_profile(profile_id)
    def evaluate_coverage_3d(self, payload=None): return self.spatial_3d_service.evaluate(payload)
    def evaluate_cns_service_capability(self): return self.cns_service_capability_service.evaluate()
    def set_operational_timing(self, payload): return self.operational_timing_service.set_timing(payload)
    def evaluate_service_timeline(self, payload=None): return self.operational_timing_service.evaluate_timeline(payload)
    def evaluate_protection_envelope(self, payload=None): return self.operational_timing_service.evaluate_protection(payload)
    def evaluate_encounter_3d(self, payload=None): return self.encounter_3d_service.evaluate(payload)
    def apply_grid_attributes(self, results):
        self.risk_service.apply_grid_attributes(results)
        # The additive V2 result recomputes from the same canonical attributes.
        return self.risk_v2_service.evaluate()
    def update_data_source_profiles(self, profiles): return self.risk_service.update_source_profiles(profiles)
    def evaluate_grid_risk(self, parameters=None): return self.risk_service.evaluate(parameters)
    def run_traffic_simulation(self, parameters):
        self.risk_service.run_traffic_simulation(parameters)
        return self.risk_v2_service.evaluate()
    def invalidate_grid_attributes(self, changed): return self.risk_service.invalidate_grid_attributes(changed)
    def _apply_grid_risk(self, result): return self.risk_service.apply_result(result)
    def _invalidate_grid_risk(self): return self.invalidation_service.risk()
    def invalidate(self, changed): return self.invalidation_service.workflow(changed)
    def review(self): return ReviewService().review(self.state)
    def export_project(self): return self.export_service.project()
    def export_routes(self): return self.export_service.routes()
    def export_sites(self): return self.export_service.sites()
    def export_confirmed_facilities(self): return self.export_service.confirmed_facilities()
