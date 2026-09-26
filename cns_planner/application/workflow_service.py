"""Thin six-step workflow orchestrator with backward-compatible public methods."""

from copy import deepcopy
from contextlib import contextmanager
from pathlib import Path

from ..algorithms.registry import (
    ALGORITHM_TYPES, build_default_algorithm_registry, default_algorithm_selection,
    normalize_algorithm_selection,
)
from ..compatibility.catalog import capability_catalog
from ..compatibility.project_adapter import read_existing_legacy
from ..compatibility.selection import CompatibilitySelectionAdapter
from ..risk.v1 import RiskModelV1
from ..risk.model_v2 import GridRiskModelV2
from ..data.mapping.conflict import ConflictGridService
from ..data.mapping.traffic import TrafficGridService
from ..algorithms.grid.service import WorkspaceGridService
from ..simulation.conflict_detector import ConflictDetector
from ..simulation.traffic_simulator import TrafficSimulator
from ..gis.cns_input_adapter import CNSInputAdapter
from ..gap.v2 import CNSGapAnalyzerV2
from .cns_input_service import CNSInputService
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
from .planning_constraint_field_service import PlanningConstraintFieldService
from .route_risk_profile_service import RouteRiskProfileService
from .layered_route_validation_service import LayeredRouteValidationService
from .layered_operational_adoption_service import LayeredOperationalAdoptionService
from .route_safety_evidence_service import RouteSafetyEvidenceService
from .route_3d_profile_service import Route3DProfileService
from .vertical_transition_validation_service import VerticalTransitionValidationService
from .route_service import RouteService
from .safety_policy_service import SafetyPolicyService
from .session import WorkflowSession
from .artifact_read_service import ArtifactReadService
from .workspace_service import WorkspaceService
from .spatial_3d_service import Spatial3DService
from .route_operating_layer_service import RouteOperatingLayerService
from .cns_service_capability_service import CNSServiceCapabilityService
from .operational_timing_service import OperationalTimingService
from .closed_loop_service import ClosedLoopService
from .corridor_service import CNSCorridorService
from .corridor_gap_service import CNSCorridorGapService
from .corridor_site_planning_service import CorridorSitePlanningService
from .requirement_recommendation_service import RequirementRecommendationService
from .plan_review_service import PlanReviewService
from .report_service import PlanningReportService
from .reference_data_service import ReferenceDataService
from .tower_obstacle_service import TowerObstacleService
from .radar_surveillance_layout_service import RadarSurveillanceLayoutService
from .building_clearance_service import BuildingClearanceService
from ..algorithms.building_clearance import BuildingClearanceV1
from .route_vertical_profile_service import RouteVerticalProfileService
from .reference_link_service import ReferenceLinkService
from .route_planner_v3_service import RoutePlannerV3ExperimentService, record_summary as _v3_record_summary
from .source_audit_service import SourceAuditService
from .v3_operational_adoption_service import V3OperationalAdoptionService
from ..algorithms.route_vertical_profile import RouteVerticalProfileV1
from .encounter_3d_service import Encounter3DService
from .production_write_authority import runtime_compatibility_result
from ..algorithms.encounter_3d import EncounterAssessment3DV1
from ..domain.airspace import normalize_airspace_policies
from ..site_planner.corridor_reuse_first_v2 import CorridorReuseFirstSitePlannerV2


# ---------------------------------------------------------------------------
# 轻量 workflow 投影（P0 显示/性能修复）
#
# ``/api/state`` 与 ``/api/workflow`` 只承载"页面装配 + 面板摘要"所需的字段。
# 逐 cell 的大结果（grid_risk_v2.cells / grid_attributes.*.cells /
# layered_route_candidates.masks[*].cells 的诊断字段 / _population_shelter_cache
# 等派生缓存）不再随通用状态返回；它们仍由既有专用接口按需提供：
#
#   * grid_attributes            -> GET /api/workspace/grid/attributes
#   * grid_risk_v2               -> GET /api/grid-risk-v2
#   * layered_route_candidates   -> GET /api/layered-route-candidates
#   * population_shelter         -> GET /api/population-shelter
#
# 投影只做"去细节"，不新增、不重算、不改写任何业务语义：状态、计数、指纹、
# 状态机与 readiness 一律原样透传，缺失值继续保持 None/null（绝不补 0）。
# ---------------------------------------------------------------------------

#: 地图专题需要的 V2 因子字段（与前端 RISK_V2_FACTOR_IDS 一致）。
_V2_FACTOR_SUMMARY_KEYS = (
    "factor_id", "domain", "status", "resolved", "raw_value", "raw_unit",
    "normalized_index", "source_role", "coverage", "reason",
)
_V2_DOMAIN_SUMMARY_KEYS = (
    "domain_id", "status", "index", "index_scope", "data_completeness",
    "unresolved", "reason", "aggregation_policy_fingerprint",
)
_MASK_CELL_SUMMARY_KEYS = ("grid_id", "status", "reason", "reason_code")

#: 派生缓存：不进入 workflow 快照，也不随项目迁移（可由现有输入重建）。
_SNAPSHOT_OMITTED_STATE_KEYS = (
    "_population_shelter_cache", "_planning_exposure_cache",
)

#: 逐 cell 大结果：快照以只读投影共享引用，不做整树深拷贝。
#: 这些容器只会被整体替换（copy-on-write），快照序列化后即结束生命周期。
_SNAPSHOT_SHARED_STATE_KEYS = (
    "grid", "grid_attributes", "grid_risk", "grid_risk_v2", "layered_route_candidates",
    "planning_constraint_fields",
    # Towers Operational Integration V2：373 条派生事实，整体替换、只读消费。
    "tower_obstacle_profiles", "tower_colocation_candidates",
)


def _snapshot_state_value(key, value):
    """快照取值：小对象深拷贝（保持既有隔离语义），大结果只共享只读引用。"""

    return value if key in _SNAPSHOT_SHARED_STATE_KEYS else deepcopy(value)


def _short_text(value, limit=200):
    if not isinstance(value, str):
        return value
    return value if len(value) <= limit else value[:limit] + "…"


def _option_value(option):
    if isinstance(option, dict):
        for key in ("value", "resolved_value", "default"):
            candidate = option.get(key)
            if candidate not in (None, ""):
                return candidate
        return None
    return option


def slim_grid_risk_v2(result):
    """Risk Framework V2 结果摘要：保留状态/指纹/domains 与逐 cell 的因子指数。

    去掉逐 cell 的 normalization reference / provenance / source 诊断块。
    逐 cell 的 ``status`` 与 ``normalized_index`` 必须保留：地图 V2 专题直接消费
    它们，且 pending/unknown 仍按"无数据"绘制，绝不补 0。
    """

    if not isinstance(result, dict):
        return result
    slim = {
        key: deepcopy(value) for key, value in result.items() if key != "cells"
    }
    cells = {}
    for grid_id, cell in (result.get("cells") or {}).items():
        if not isinstance(cell, dict):
            continue
        factors = {}
        for factor_id, factor in (cell.get("factors") or {}).items():
            if not isinstance(factor, dict):
                continue
            normalized = (factor.get("normalization") or {}).get("reference") or {}
            reference = normalized.get("reference") if isinstance(normalized, dict) else None
            factors[factor_id] = {
                key: deepcopy(factor.get(key)) for key in _V2_FACTOR_SUMMARY_KEYS
            }
            factors[factor_id]["reference_value"] = _option_value(reference)
        slim_cell = {
            key: deepcopy(value) for key, value in cell.items() if key != "factors"
        }
        slim_cell["factors"] = factors
        slim_cell["domains"] = {
            domain_id: {
                key: deepcopy(domain.get(key)) for key in _V2_DOMAIN_SUMMARY_KEYS
            }
            for domain_id, domain in (cell.get("domains") or {}).items()
            if isinstance(domain, dict)
        }
        # _V2_FACTOR_SUMMARY_KEYS 已经覆盖 status / normalized_index / factor_id；
        # 这里再确保地图专题必需的两个字段存在（缺失仍是 None，不补 0）。
        cells[str(grid_id)] = slim_cell
    slim["cells"] = cells
    return slim


def slim_grid_attributes(attributes):
    """grid_attributes 摘要：只保留每个命名空间的状态/来源/计数，不含逐 cell 明细。

    每次调用都返回新的顶层容器，但命名空间容器（以及其中的 ``cells``）共享只读引用；
    因此快照不会就地改写服务状态，也不需要为上万 cell 做一次深拷贝。
    """

    if not isinstance(attributes, dict):
        return attributes
    slim = {}
    for name, attribute in attributes.items():
        if not isinstance(attribute, dict):
            slim[name] = deepcopy(attribute)
            continue
        summary = {
            key: value for key, value in attribute.items() if key != "cells"
        }
        summary["detail_available"] = bool(attribute.get("cells"))
        slim[name] = summary
    return slim


def slim_layered_route_candidates(collection):
    """候选容器摘要：候选记录原样保留，feasibility mask 只保留逐 cell 的判定。

    mask 的 ``counts`` / 指纹 / 状态与 ``items`` 全部原样透传，只把每个 cell 的
    诊断字段（terrain / building 明细、adapter、reason 文本）收敛为
    grid_id + status + reason(code)，使前端覆盖层与图例所需信息不丢失。
    """

    if not isinstance(collection, dict):
        return collection
    slim = deepcopy(collection)
    masks = {}
    for key, mask in (collection.get("masks") or {}).items():
        if not isinstance(mask, dict):
            masks[key] = deepcopy(mask)
            continue
        slim_mask = {name: deepcopy(value) for name, value in mask.items() if name != "cells"}
        slim_mask["cells"] = {
            str(cell_id): {
                name: _short_text(cell.get(name)) for name in _MASK_CELL_SUMMARY_KEYS
                if name in cell
            }
            for cell_id, cell in (mask.get("cells") or {}).items()
            if isinstance(cell, dict)
        }
        slim_mask["cells_detail"] = "GET /api/layered-route-candidates"
        masks[key] = slim_mask
    slim["masks"] = masks
    return slim


def slim_population_shelter(attribute):
    """population_shelter 摘要：保留状态/指纹/计数，不含逐 cell 的 shelter 系数。

    只做顶层投影：不深拷贝万级 cell 的派生场，也不改写任何字段语义。
    """

    if not isinstance(attribute, dict):
        return attribute
    slim = {key: deepcopy(value) for key, value in attribute.items() if key != "cells"}
    slim["cell_count"] = len(attribute.get("cells") or {})
    slim["cells_detail"] = "GET /api/population-shelter"
    return slim


def slim_planning_exposure(attribute):
    """BUG-ROUTE-005 规划用暴露度层摘要：状态/计数/策略原样，逐 cell 由专用接口提供。"""

    if not isinstance(attribute, dict):
        return attribute
    slim = {key: deepcopy(value) for key, value in attribute.items() if key != "cells"}
    slim["cell_count"] = len(attribute.get("cells") or {})
    slim["cells_detail"] = "GET /api/planning-exposure"
    return slim


# ---------------------------------------------------------------------------
# Phase4-B5X：外置型 canonical 结果在通用快照中的**有界投影**
#
# B5X 把逐 cell / 逐 sample / 逐 voxel 明细统一外置成 content-addressed artifact
# （见 ``persistence/project_compaction.py``）。通用 workflow 快照因此**不再承载**
# 这些明细：它只保留 status / counts / fingerprint / warnings / active ids /
# ``artifact_ref`` 与"明细可用 + 专用读取入口"的声明。
#
# 这条投影只做"去细节"：不新增、不重算、不改写任何业务语义，也不解压任何
# artifact。（前端需要明细时走下面登记的专用 GET 接口。）
# ---------------------------------------------------------------------------

#: 逐 cell / 逐 sample / 逐 voxel 明细字段名：快照里一律替换为计数与来源声明。
_SNAPSHOT_DETAIL_FIELD_NAMES = (
    "cells", "samples", "voxels", "evidence", "included_grid_ids",
    "deficit_voxel_ids", "unknown_voxel_ids", "voxel_ids", "continuous_deficit_segments",
    "uncovered_segments", "under_redundant_segments", "unknown_segments",
    "selected_panels", "optimisation_samples", "refinement_rounds", "entries",
    "presolve", "stage_a", "stage_b", "coverage_profile", "result", "context_basis",
    "iteration_trace", "candidate_impacts", "final_hypothetical_evidence",
    "included_grid_ids",
)

#: 需要在通用快照里去明细的结果容器 -> 专用只读读取入口（前端按需拉取）。
_SNAPSHOT_DETAIL_ENDPOINTS = {
    "grid": "/api/workspace/grid",
    "grid_risk_v2": "/api/grid-risk-v2",
    "layered_route_candidates": "/api/layered-route-candidates",
    "planning_constraint_fields": "/api/planning-constraint-field",
    "coverage_3d": "/api/coverage-3d",
    "cns_service_capability": "/api/cns-service-capability",
    "cns_corridor_assessment": "/api/cns-service-corridor",
    "cns_corridor_gap_assessment": "/api/cns-corridor-gap",
    "cns_corridor_site_plan": "/api/cns-corridor-site-plan",
    "cns_gap_analysis_v2": "/api/cns-gap-analysis-v2",
    "radar_surveillance_layout": "/api/radar-surveillance-layout",
    "route_planning_experiments": "/api/route-experiments",
}

#: 顶层容器**额外**的明细字段（只对这些结果生效，不做全局字段名匹配）。
_SNAPSHOT_ROOT_DETAIL_FIELDS = {
    # 分层候选：mask 是逐 cell 明细（上万个 cell × 每个 cell 的净空诊断），必须外置；
    # 候选记录本身（items：state/成本/航路点）是主界面需要的**有界**权威记录，保留。
    "layered_route_candidates": ("masks",),
}


def _strip_snapshot_detail(value, *, root_key=None, depth=0):
    """递归把大型明细字段替换成 ``<name>_count`` + ``<name>_detail`` 声明。

    ``root_key`` 只用于顶层容器的**额外**明细字段（例如 layered candidates 的
    ``items`` / ``masks``）：它们不是通用明细名，只在对应结果上外置。
    """

    if isinstance(value, dict):
        extra = _SNAPSHOT_ROOT_DETAIL_FIELDS.get(root_key, ()) if depth == 0 else ()
        slim = {}
        for name, item in value.items():
            if (name in _SNAPSHOT_DETAIL_FIELD_NAMES or name in extra) and isinstance(
                item, (dict, list)
            ):
                slim[f"{name}_count"] = len(item)
                slim[f"{name}_detail"] = "artifact"
                continue
            slim[name] = _strip_snapshot_detail(item, depth=depth + 1)
        return slim
    if isinstance(value, list):
        # 列表本身保留（候选列表 / 记录列表是主界面需要的有界结构），逐项去明细。
        return [_strip_snapshot_detail(item, depth=depth + 1) for item in value]
    return value


def _snapshot_detail_projection(result):
    """构造外置型结果的快照投影（不解压、不深拷贝任何大型明细）。

    投影以**快照自身的值**为基础（服务已做的有界投影会先安装进 result），
    因此这里只做"减法"：去掉逐 cell / 逐 sample / 逐 voxel 明细，保留
    status / counts / fingerprint / warnings / active ids / artifact_ref。
    """

    projection = {}
    for key, endpoint in _SNAPSHOT_DETAIL_ENDPOINTS.items():
        value = result.get(key)
        if not isinstance(value, dict):
            continue
        slim = _strip_snapshot_detail(value, root_key=key)
        if not isinstance(slim, dict):
            continue
        if key == "grid":
            # grid 的既有消费方读 ``count`` 表达格数；这里同时给出 cell_count，
            # 使"明细已外置、格数仍然权威"这一事实对新旧调用点都成立。
            slim["cell_count"] = slim.get("cells_count", value.get("count"))
        slim["detail_available"] = True
        slim["detail_endpoint"] = endpoint
        projection[key] = slim
    return projection


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
        self.compatibility_selection = CompatibilitySelectionAdapter(
            self.state, self.algorithm_registry, self.session,
        )
        self.route_planner = None
        self.coverage_planner = None
        self.risk_model = risk_model or self._selected_algorithm("risk_model")
        self.gap_analyzer_v2 = self.algorithm_registry.create(
            "cns_gap_analyzer", CNSGapAnalyzerV2.algorithm_id,
            CNSGapAnalyzerV2.algorithm_version,
            (self.state.get("cns_gap_analysis_v2") or {}).get("parameters") or {},
        )
        self.coverage_model_3d = self._selected_algorithm("coverage_model")
        self.cns_service_model = self._selected_algorithm("service_model")
        self.timeline_model = self._selected_algorithm("timeline_model")
        self.protection_model = self._selected_algorithm("protection_model")
        self.corridor_site_planner = self.algorithm_registry.create(
            "site_planner", CorridorReuseFirstSitePlannerV2.algorithm_id,
            CorridorReuseFirstSitePlannerV2.algorithm_version,
            (self.state.get("cns_corridor_site_plan") or {}).get("parameters") or {},
        )
        self.corridor_model = self._selected_algorithm("corridor_model")
        self.corridor_gap_analyzer = self._selected_algorithm("corridor_gap_analyzer")
        self.requirement_model = self._selected_algorithm("requirement_model")
        self.traffic_simulator, self.conflict_detector = TrafficSimulator(), ConflictDetector()
        self.traffic_grid_service, self.conflict_grid_service = TrafficGridService(), ConflictGridService()
        self.invalidation_service = InvalidationService(self.session)
        # Phase4-B5X：canonical artifact 的唯一只读读取入口（summary / bounded
        # content / dry-run GC）。router 只调用它，绝不自己打开 gzip 或解析 artifact。
        self.artifact_read_service = ArtifactReadService(self.session)
        snapshot = self.snapshot
        self.safety_policy_service = SafetyPolicyService(
            self.session, self.invalidation_service, snapshot
        )
        self.cns_input_service = CNSInputService(self.session, self.invalidation_service, CNSInputAdapter(), snapshot)
        self.cns_input_service.ensure_catalogs()
        self.requirement_recommendation_service = RequirementRecommendationService(
            self.session, self.requirement_model, self.invalidation_service, snapshot,
            self.cns_input_service,
        )
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
        # Layered route planning: the default ``layered_route_planner`` is now Theta* V2;
        # V1 remains registered as the legacy/baseline planner.  Explicit altitude layer
        # selection, terrain/building coarse feasibility mask and an independent candidate
        # container are shared.  It never writes ``operational_routes`` / CNS results and
        # never switches the project's default ``route_planner`` (still ``route_planner_v1``).
        self.layered_route_planner_service = LayeredRoutePlannerService(
            self.session, self.invalidation_service, snapshot,
        )
        self.planning_constraint_field_service = PlanningConstraintFieldService(
            self.session, self.invalidation_service, snapshot,
        )
        # Towers Operational Integration V2：真实铁塔的派生事实（障碍物高度 + 共塔宿主候选）。
        # 两个派生层都只服务自己那条链路 —— 塔高只进航路净空，共塔候选只进 CNS 规划；
        # 谁都不进入 population×shelter 或 Risk Framework V2 数学。
        self.tower_obstacle_service = TowerObstacleService(
            self.session, self.invalidation_service, snapshot,
        )
        # Radar Surveillance Layout V1（additive，proposal-only）：「80m固定高度航路方向性
        # 雷达几何初步划设方案」。它只写 ``radar_surveillance_policy`` /
        # ``radar_surveillance_layout``，绝不写 existing CNS / coverage_3d / 走廊与站址提案。
        self.radar_surveillance_layout_service = RadarSurveillanceLayoutService(
            self.session, self.invalidation_service, snapshot,
            self.layered_route_planner_service,
        )
        self.invalidation_service.radar_surveillance_layout_invalidator = (
            self.radar_surveillance_layout_service.stale_for_reason
        )
        # A Risk Framework V2 / layer / terrain-building / policy change stales only the
        # layered candidates and masks, never legacy routes, V3 or CNS results.
        self.invalidation_service.layered_route_invalidator = (
            self.layered_route_planner_service.refresh_for_reason
        )
        # Production execution is pinned to the canonical Theta* V2 selection.  An old
        # saved V1 selection remains untouched for passive compatibility/audit, but it is
        # never instantiated and never used as a fallback.
        production_layered = default_algorithm_selection()["layered_route_planner"]
        self.layered_route_planner = self.algorithm_registry.create(
            production_layered["algorithm_type"], production_layered["algorithm_id"],
            production_layered["version"], production_layered["parameters"],
        )
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
        self.radar_surveillance_layout_service.route_risk_profile_service = (
            self.route_risk_profile_service
        )
        self.layered_route_validation_service = LayeredRouteValidationService(
            self.session, self.invalidation_service, snapshot,
            self.layered_route_planner_service, self.route_risk_profile_service,
        )
        self.invalidation_service.layered_route_validation_invalidator = (
            self.layered_route_validation_service.stale_for_reason
        )
        self.radar_surveillance_layout_service.layered_route_validation_service = (
            self.layered_route_validation_service
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
        self.closed_loop_service = ClosedLoopService(
            self.session, self.coverage_model_3d, self.cns_service_model,
            self.timeline_model, self.gap_analyzer_v2, snapshot,
            self.invalidation_service,
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
            self.corridor_gap_analyzer, snapshot, self.invalidation_service,
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
        self.layered_operational_adoption_service = LayeredOperationalAdoptionService(
            self.session, self.layered_route_validation_service,
            self.invalidation_service, snapshot,
        )
        self.layered_route_validation_service.adoption_invalidator = (
            self.layered_operational_adoption_service.stale_for_validations
        )
        # Route Safety Evidence V2: additive post-planning evidence aggregation over the
        # published layered operational adoption lineage.  It never replans, never adopts and
        # never writes any upstream container; it is only ever staled *by* its dependencies.
        self.route_safety_evidence_service = RouteSafetyEvidenceService(
            self.session, self.invalidation_service, snapshot,
            self.layered_route_planner_service, self.layered_route_validation_service,
            self.layered_operational_adoption_service, self.route_risk_profile_service,
        )
        self.invalidation_service.route_safety_evidence_invalidator = (
            self.route_safety_evidence_service.stale_for_reason
        )
        # Production Route3DProfile V1 (additive, thin derivation layer): derives the published
        # fixed-H layered route into a distance-parameterised climb → cruise → descent profile
        # for the existing RouteVerticalProfile / Coverage3D consumers.  It never performs a 3D
        # search, never invents a Dubins/motion-primitive trajectory and never validates
        # kinematics.  It is created last because it reads the full published lineage.
        self.route_3d_profile_service = Route3DProfileService(
            self.session, self.invalidation_service, snapshot,
        )
        self.invalidation_service.route_3d_profile_invalidator = (
            self.route_3d_profile_service.stale_for_reason
        )
        # Vertical Transition Continuous Validation V1 (additive, thin geometry validation).
        # It closes the *only* geometry gap of the Route3DProfile stage: the existing
        # LayeredRouteValidation validates a fixed cruise altitude only, so the climb and the
        # descent are validated here against the native FABDEM pixels and the real building
        # footprints.  Together they form the complete 3D geometry evidence of one route.  It
        # never rewrites the stored Route3DProfile / LayeredRouteValidation and it is strictly
        # downstream: a transition change only stales the Safety Evidence and the report.
        self.vertical_transition_validation_service = VerticalTransitionValidationService(
            self.session, self.invalidation_service, snapshot,
            layered_service=self.layered_route_planner_service,
            validation_service=self.layered_route_validation_service,
            risk_profile_service=self.route_risk_profile_service,
            profile_service=self.route_3d_profile_service,
        )
        self.invalidation_service.vertical_transition_validation_invalidator = (
            self.vertical_transition_validation_service.stale_for_reason
        )
        # The Route3DProfile *projection* reports the current transition verdict next to the
        # unchanged cruise-only boundary.  The stored profile record is never rewritten.
        self.route_3d_profile_service.transition_validation_resolver = (
            self._current_transition_validation
        )

    def _current_transition_validation(self, route_id):
        """Read-only resolver used by the Route3DProfile projection."""

        wanted = str(route_id or "")
        if not wanted:
            return None
        collection = self.vertical_transition_validation_service.result_snapshot()
        return next((
            item for item in reversed(collection.get("items") or [])
            if str(item.get("route_id") or "") == wanted
            and item.get("current_applicability") == "current"
        ), None)

    def save(self): self.session.save()

    @contextmanager
    def deferred_save(self):
        """Collect every ``save()`` inside the block into a single commit at the end.

        One user action may legitimately consist of several internal steps (re-map the
        workspace, then recompute its grid attributes).  Without this, the disk — and the
        ``revision`` counter the API compares against — would expose the *intermediate*
        state, so a concurrent GET could report a workspace whose attributes look unset.
        """

        session = self.session
        previous = session.defer_save
        session.defer_save = True
        try:
            yield
        finally:
            session.defer_save = previous
            if not previous:
                # Committing even on failure keeps the in-memory state and the persisted
                # document consistent; the individual steps themselves are unchanged.
                session.commit_deferred()

    def snapshot(self):
        # 轻量 workflow 状态：逐 cell 大结果与派生缓存不随通用状态返回，
        # 由既有专用接口按需提供（见模块顶部说明）。业务语义完全不变。
        #
        # 读取性能：顶层逐项浅拷贝。只有小对象做 deepcopy；逐 cell 的大结果
        # （grid_risk.cells / grid_risk_v2 / layered_route_candidates.masks）以只读
        # 投影共享引用，既省掉整树深拷贝，也不改变任何字段或状态机。
        result = {
            key: _snapshot_state_value(key, value)
            for key, value in self.state.items()
            if key not in _SNAPSHOT_OMITTED_STATE_KEYS
        }
        result["grid_attributes"] = slim_grid_attributes(self.state.get("grid_attributes"))
        result["steps"] = self._steps()
        result["defaults"] = deepcopy(self.defaults)
        result["device_source"] = self.state.get("device_catalog", {}).get("source") or self.defaults.get("device_library", {}).get("source", "demo/default")
        result["aircraft_source"] = self.state.get("aircraft_profiles", {}).get("source") or self.defaults.get("aircraft_library", {}).get("source", "demo/default")
        result["algorithm_catalog"] = self.algorithm_registry.catalog()
        # 只读兼容投影：新项目不再保存 legacy selection，但旧项目 / 运行期 compatibility
        # 覆盖的实际生效 selection 必须可被高级区如实展示（不写回 ProjectState）。
        result["compatibility_selection"] = self.compatibility_selection.selection_snapshot()
        if hasattr(self, "requirement_recommendation_service"):
            result["required_cns_recommendation"] = self.requirement_recommendation_service.result_snapshot()
        result["route_planning_experiments"] = deepcopy(
            self.state.get("route_planning_experiments") or {}
        )
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
            # 轻量投影：逐 cell 的 V2 因子指数保留（地图专题消费），
            # normalization reference / provenance / source 诊断块由 /api/grid-risk-v2 提供。
            # 投影以只读引用复用服务快照，不再二次深拷贝上万 cell 的派生结果。
            result["grid_risk_v2"] = slim_grid_risk_v2(
                self.risk_v2_service.result_snapshot()
            )
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
            result["layered_route_candidates"] = slim_layered_route_candidates(
                self.layered_route_planner_service.result_snapshot()
            )
            # Additive Theta* V2 planning inputs and the derived per-grid population ×
            # shelter field.  These are read-only projections; the candidates carry the
            # objective/evaluation evidence themselves.
            result["shelter_coefficient_policy"] = (
                self.layered_route_planner_service.shelter_policy_snapshot()
            )
            result["population_shelter"] = slim_population_shelter(
                self.layered_route_planner_service.population_shelter_snapshot()
            )
            result["regulatory_constraints"] = deepcopy(
                self.state.get("regulatory_constraints") or {}
            )
            result["communication_planning_field"] = (
                self.layered_route_planner_service.communication_field_snapshot()
            )
            result["theta_v2_objective_policy"] = deepcopy(
                self.state.get("theta_v2_objective_policy") or {}
            )
            result["max_route_risk_density"] = deepcopy(
                self.state.get("max_route_risk_density") or {}
            )
            # BUG-ROUTE-005：规划用暴露度层（策略本体 + 只读派生摘要）。它**不**进入人口报告、
            # 数据审计或 NoData 语义；默认未配置时不产生任何影响。
            result["planning_exposure_policy"] = deepcopy(
                self.state.get("planning_exposure_policy") or {}
            )
            result["planning_exposure"] = slim_planning_exposure(
                self.layered_route_planner_service.planning_exposure_snapshot()
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
        if hasattr(self, "layered_route_validation_service"):
            result["layered_route_validations"] = (
                self.layered_route_validation_service.result_snapshot()
            )
            result["layered_route_validation_readiness"] = (
                self.layered_route_validation_service.readiness_snapshot()
            )
        if hasattr(self, "layered_operational_adoption_service"):
            result["layered_operational_adoptions"] = (
                self.layered_operational_adoption_service.result_snapshot()
            )
            result["layered_operational_adoption_readiness"] = (
                self.layered_operational_adoption_service.readiness_snapshot()
            )
        if hasattr(self, "route_safety_evidence_service"):
            # Route Safety Evidence V2: only the read-only assessment projection and the
            # bounded readiness travel in the snapshot.  Nothing here is ever recomputed by
            # the snapshot itself.
            result["route_safety_evidence_v2"] = (
                self.route_safety_evidence_service.result_snapshot()
            )
            result["route_safety_evidence_v2_readiness"] = (
                self.route_safety_evidence_service.readiness_snapshot()
            )
        if hasattr(self, "route_3d_profile_service"):
            # Production Route3DProfile V1: the read-only projection (with recomputed
            # ``current_applicability``) and the bounded readiness travel in the snapshot.  The
            # stored container itself stays inside ``spatial_3d.route_3d_profiles``; nothing is
            # ever derived by the snapshot.
            result["route_3d_profiles"] = self.route_3d_profile_service.result_snapshot()
            result["route_3d_profile_readiness"] = (
                self.route_3d_profile_service.readiness_snapshot()
            )
        if hasattr(self, "vertical_transition_validation_service"):
            # Vertical Transition Continuous Validation V1: the read-only projection (with a
            # recomputed ``current_applicability``) and the bounded readiness travel in the
            # snapshot.  The stored container is ``state["vertical_transition_validations"]``;
            # nothing is ever evaluated by the snapshot itself.
            result["vertical_transition_validations"] = (
                self.vertical_transition_validation_service.result_snapshot()
            )
            result["vertical_transition_validation_readiness"] = (
                self.vertical_transition_validation_service.readiness_snapshot()
            )
        # Radar Surveillance Layout V1（additive，proposal-only）：通用快照只带**有界摘要**
        # （状态 / 计数 / 求解器 / 覆盖率），逐 sample 明细由专用接口按需获取 —— 与
        # layered_route_candidates / grid_attributes 的既有做法一致。
        if hasattr(self, "radar_surveillance_layout_service"):
            result["radar_surveillance_layout"] = (
                self.radar_surveillance_layout_service.summary_snapshot()
            )
            result["radar_surveillance_layout_readiness"] = (
                self.radar_surveillance_layout_service.readiness_snapshot()
            )
        result["review"] = self.review()
        # B5X：外置型大型明细绝不随通用快照下发（只保留 summary + artifact_ref +
        # 专用读取入口声明）。这是最后一步覆盖，避免任何分支把水合后的明细带出去。
        result.update(_snapshot_detail_projection(result))
        return result

    # ---- Phase4-B5X：canonical artifact 只读读取（summary / content / GC） ----

    def artifact_manifest(self):
        return self.artifact_read_service.manifest()

    def artifact_summaries(self):
        return self.artifact_read_service.summaries()

    def artifact_summary(self, logical_key):
        return self.artifact_read_service.summary(logical_key)

    def artifact_content(self, payload=None):
        payload = payload if isinstance(payload, dict) else {}
        return self.artifact_read_service.content(
            payload.get("logical_key"),
            route_id=payload.get("route_id"),
            subsystem=payload.get("subsystem"),
            grid_ids=payload.get("grid_ids"),
            bbox=payload.get("bbox"),
            offset=payload.get("offset") or 0,
            limit=payload.get("limit"),
        )

    def artifact_inventory(self):
        return self.artifact_read_service.inventory()

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
    def towers_snapshot(self): return self.reference_data_service.towers_snapshot()
    def tower_obstacle_profiles_snapshot(self): return self.tower_obstacle_service.result_snapshot()
    def tower_colocation_candidates_snapshot(self): return self.tower_obstacle_service.colocation_snapshot()
    def tower_integration_policies_snapshot(self): return self.tower_obstacle_service.policy_snapshot()
    # ---- Radar Surveillance Layout V1（proposal-only，additive） -------------------
    def radar_surveillance_policy(self):
        return self.radar_surveillance_layout_service.policy_snapshot()
    def set_radar_surveillance_policy(self, payload=None):
        return self.radar_surveillance_layout_service.set_policy(payload)
    def radar_surveillance_layout(self, route_id=None):
        return self.radar_surveillance_layout_service.result_snapshot(route_id)
    def radar_surveillance_layout_readiness(self, payload=None):
        return self.radar_surveillance_layout_service.readiness_snapshot(payload)
    def evaluate_radar_surveillance_layout(self, payload=None, *, facts_provider=None):
        result = self.radar_surveillance_layout_service.evaluate(
            payload, facts_provider=facts_provider,
        )
        self.invalidation_service.report("radar_surveillance_layout_evaluated")
        return result
    def evaluate_tower_obstacle_profiles(self, payload=None, *, facts_provider=None):
        return self.tower_obstacle_service.evaluate(payload, facts_provider=facts_provider)
    def set_tower_clearance_policy(self, payload=None):
        return self.tower_obstacle_service.set_clearance_policy(payload)
    def import_towers(self, path):
        result = self.reference_data_service.import_towers(path)
        # 铁塔源真的换了：派生事实与其下游必须过时（绝不触发 Risk V2）。
        self.invalidation_service.tower_data_changed("tower_source_imported", include_derived=True)
        return result
    def airspace_policies_snapshot(self): return deepcopy(self.state.get("airspace_policies") or {})
    def equipment_reference_catalog_snapshot(self): return self.reference_data_service.equipment_catalog_snapshot()
    def required_cns_snapshot(self): return deepcopy(self.state.get("required_cns") or {})
    def cns_operation_context_snapshot(self): return self.requirement_recommendation_service.context_snapshot()
    def cns_requirement_policies_snapshot(self): return self.requirement_recommendation_service.policies_snapshot()
    def required_cns_recommendation_snapshot(self): return self.requirement_recommendation_service.result_snapshot()
    def existing_cns_snapshot(self): return deepcopy(self.state.get("existing_cns_facilities") or {})
    def candidate_sites_snapshot(self): return deepcopy(self.state.get("candidate_sites") or {})
    def compatibility_catalog_snapshot(self): return capability_catalog()
    def compatibility_route_snapshot(self):
        runtime = runtime_compatibility_result(self.session, "operational_routes")
        if runtime:
            return deepcopy(runtime)
        selection = (self.state.get("algorithm_selection") or {}).get("route_planner") or {}
        if selection.get("algorithm_id") in {"route_planner_v1", "risk_aware_route_planner_v2"}:
            view = read_existing_legacy(self.state, "operational_routes", "RoutePlannerV1")
            if "value" in view:
                view["items"] = view.pop("value")
                view["count"] = len(view["items"])
            return view
        return read_existing_legacy({}, "operational_routes", "RoutePlannerV1")
    def compatibility_coverage_snapshot(self):
        return deepcopy(
            runtime_compatibility_result(self.session, "coverage")
            or read_existing_legacy(self.state, "coverage", "CoveragePlannerV1")
        )
    def cns_gap_snapshot(self):
        return deepcopy(
            runtime_compatibility_result(self.session, "cns_gap_analysis")
            or read_existing_legacy(self.state, "cns_gap_analysis", "CNSGapAnalyzerV1")
        )
    def cns_gap_v2_snapshot(self): return self.gap_analysis_v2_service.result_snapshot()
    def spatial_3d_snapshot(self): return self.spatial_3d_service.spatial_snapshot()
    def coverage_3d_snapshot(self): return self.spatial_3d_service.coverage_snapshot()
    def cns_service_capability_snapshot(self): return self.cns_service_capability_service.capability_snapshot()
    def operational_timing_snapshot(self): return self.operational_timing_service.timing_snapshot()
    def service_timeline_snapshot(self): return self.operational_timing_service.timeline_snapshot()
    def protection_envelope_snapshot(self): return self.operational_timing_service.protection_snapshot()
    def cns_site_plan_snapshot(self):
        return deepcopy(read_existing_legacy(
            self.state, "cns_site_plan", "ReuseFirstSitePlannerV1",
        ))
    def closed_loop_snapshot(self): return self.closed_loop_service.result_snapshot()
    def cns_corridor_snapshot(self): return self.corridor_service.result_snapshot()
    # Phase4-B6X：heavy task 与同步 use case 共用同一条写入路径与输入组装函数。
    # 这两组方法只是转发，不构成第二个 production owner。
    def cns_corridor_apply_computed(self, result, *, progress=None):
        return self.corridor_service.apply_computed(result, progress=progress)
    def cns_corridor_compute_input(self, payload=None):
        return self.corridor_service.compute_input(payload)
    def cns_corridor_result_artifact_ref(self):
        return self.corridor_service.result_artifact_ref()
    def planning_constraint_field_apply_field(self, field):
        return self.planning_constraint_field_service.apply_field(field)
    def planning_constraint_field_generation_arguments(self, payload=None):
        from .planning_constraint_field_service import generation_arguments
        return generation_arguments(self.state, payload)
    def planning_constraint_field_result_artifact_ref(self, altitude_layer_id=None):
        return self.planning_constraint_field_service.result_artifact_ref(altitude_layer_id)
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
    def route_experiments_snapshot(self):
        # B8X：legacy 实验服务已删除，但"参考航线 ↔ 已发布运行航路"的只读对比仍是
        # 通用审计能力（不依赖任何已删除算法），由 reference link service 现场计算。
        return {
            **deepcopy(self.state.get("route_planning_experiments") or {}),
            "reference_comparisons": self.reference_link_service.reference_comparisons_snapshot(),
        }
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
        compatibility_only = algorithm_type in {
            "route_planner", "coverage_planner", "cns_gap_analyzer",
        } or candidate["algorithm_id"] in {
            "layered_route_planner_v1", "reuse_first_site_planner_v1",
        }
        if compatibility_only and candidate != self.state["algorithm_selection"].get(algorithm_type):
            raise ValueError(
                "Compatibility/Archive 算法不能写入新的 algorithm_selection；"
                "旧项目中的已保存选择仅供只读解释"
            )
        instance = self.algorithm_registry.create(
            candidate["algorithm_type"], candidate["algorithm_id"],
            candidate["version"], candidate["parameters"],
        )
        if candidate == self.state["algorithm_selection"].get(algorithm_type):
            return self.snapshot()
        self.state["algorithm_selection"][algorithm_type] = candidate
        self._bind_algorithm(algorithm_type, instance)
        if algorithm_type == "risk_model":
            self.invalidation_service.risk()
        else:
            changed = {
                "layered_route_planner": "layered_route_planner_algorithm",
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
        if algorithm_type == "layered_route_planner":
            # The layered planner owns its own candidate/mask products; it never becomes the
            # project's ``route_planner`` and never writes operational routes.
            self.layered_route_planner = instance
            self.layered_route_planner_service.planner = instance
        elif algorithm_type == "cns_gap_analyzer":
            self.gap_analyzer_v2 = self.gap_analysis_v2_service.analyzer = instance
            self.closed_loop_service.gap_model = instance
            self.plan_review_service.gap_model = instance
        elif algorithm_type == "risk_model":
            self.risk_model = self.risk_service.risk_model = instance
        elif algorithm_type == "coverage_model":
            self.coverage_model_3d = self.spatial_3d_service.model = instance
            self.closed_loop_service.coverage_model = instance
            self.plan_review_service.coverage_model = instance
        elif algorithm_type == "service_model":
            self.cns_service_model = self.cns_service_capability_service.model = instance
            self.closed_loop_service.capability_model = instance
            self.plan_review_service.capability_model = instance
        elif algorithm_type == "timeline_model":
            self.timeline_model = self.operational_timing_service.timeline_model = instance
            self.closed_loop_service.timeline_model = instance
            self.plan_review_service.timeline_model = instance
        elif algorithm_type == "protection_model":
            self.protection_model = self.operational_timing_service.protection_model = instance
        elif algorithm_type == "site_planner":
            self.corridor_site_planner = self.corridor_site_planning_service.planner = instance
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
        def current_result(name):
            value = state.get(name) or {}
            return bool(value) and value.get("status") not in (
                None, "not_calculated", "missing_data", "stale",
            )

        facility_plan = state.get("cns_corridor_site_plan") or {}
        facility_plan_ok = facility_plan.get("status") in (
            "proposal_ready", "no_action_required", "no_eligible_proposal",
            "evidence_required",
        )
        canonical_cns_ok = all(current_result(name) for name in (
            "coverage_3d", "cns_service_capability",
            "cns_corridor_assessment", "cns_corridor_gap_assessment",
        )) and facility_plan_ok
        return {
            "1": True, "2": workspace_ok, "3": routes_ok, "4": rules_ok,
            "5": canonical_cns_ok, "6": canonical_cns_ok,
        }

    def set_project(self, payload): return self.project_service.set_project(payload)
    def set_workspace(self, bbox, health, preferred_grid_level=None, max_cells=None): return self.workspace_service.set_workspace(bbox, health, preferred_grid_level, max_cells)
    def population_nodata_policy(self): return self.workspace_service.population_nodata_policy_snapshot()
    def set_population_nodata_policy(self, payload): return self.workspace_service.set_population_nodata_policy(payload)
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
        tower_path = (paths or {}).get("towers")
        if not route_path and "reference_routes" not in (paths or {}):
            # 数据源路径本身丢失时（自动项目不写 data_sources.json）回退到项目状态里
            # 保存的来源，避免"项目重新打开后还需要重新配置数据源"。显式传入空值表示
            # 用户主动清空该数据源，此时不回退。
            route_path = ((self.state.get("reference_routes") or {}).get("data_source") or {}).get("path")
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
            # Configuring a converted file never replaces existing reference_routes.
            # The user must request a preview and explicitly confirm it.
            # 例外：项目里还没有任何正式业务航线对象时，按已保存的数据源自动恢复；
            # 是否真正恢复仍取决于源文件自身的 source_crs + crs_confirmed=true 声明。
            if not (self.state.get("reference_routes") or {}).get("items"):
                self.reference_data_service.restore_routes_from_source(route_path, save=False)
        if tower_path:
            # 真实通信铁塔站址（只读参考数据）：只登记来源与审计，首次配置时按源恢复一次。
            # 绝不写入 grid_attributes、绝不生成 CNS 覆盖或把站址转成 C/N/S existing site。
            self.source_audit_service.register_quick("towers", tower_path)
            if not (self.state.get("towers") or {}).get("items"):
                self.reference_data_service.restore_towers_from_source(tower_path, save=False)
                self.invalidation_service.tower_data_changed(
                    "tower_source_restored", include_derived=True,
                )
        if save and (landing_path or route_path or tower_path):
            self.session.save()
        return self.snapshot()
    def delete_node(self, node_id): return self.route_service.delete_node(node_id)
    def generate_scenario(self, direction): return self.route_service.generate_scenario(direction)
    def generate_scenario_od(self, start_node_id, end_node_id, direction="both"):
        return self.route_service.generate_scenario_od(start_node_id, end_node_id, direction)
    def delete_route(self, route_id): return self.route_service.delete_route(route_id)
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
    def analyze_cns_gaps_v2(self, payload=None): return self.gap_analysis_v2_service.evaluate(payload)
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
    def create_reference_route_link(self, payload): return self.reference_link_service.create_link(payload)
    def delete_reference_route_link(self, link_id): return self.reference_link_service.delete_link(link_id)
    def select_registered_algorithm(self, payload): return self.select_algorithm(payload)
    def set_devices(self, devices): return self.cns_planning_service.set_devices(devices)
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
    def planning_constraint_fields(self, altitude_layer_id=None):
        return self.planning_constraint_field_service.result_snapshot(altitude_layer_id)
    def planning_constraint_field_map(self, altitude_layer_id=None, bbox=None):
        """B4X 只读地图读取路径：紧凑 cell 投影，不返回 evidence / 完整 artifact。"""

        return self.planning_constraint_field_service.field_map(altitude_layer_id, bbox)
    def generate_planning_constraint_field(self, payload=None):
        return self.planning_constraint_field_service.generate(payload)
    def set_layered_route_planning_request(self, payload):
        return self.layered_route_planner_service.set_planning_request(payload)
    def set_layered_route_feasibility_policy(self, payload):
        return self.layered_route_planner_service.set_feasibility_policy(payload)
    def set_layered_route_cost_policy(self, payload):
        return self.layered_route_planner_service.set_cost_policy(payload)
    def shelter_coefficient_policy(self):
        return self.layered_route_planner_service.shelter_policy_snapshot()
    def set_shelter_coefficient_policy(self, payload):
        return self.layered_route_planner_service.set_shelter_policy(payload)
    def population_shelter(self):
        return self.layered_route_planner_service.population_shelter_snapshot()
    def regulatory_constraints(self):
        return deepcopy(self.state.get("regulatory_constraints") or {})
    def set_regulatory_constraints(self, payload):
        return self.layered_route_planner_service.set_regulatory_constraints(payload)
    def communication_planning_field(self):
        return self.layered_route_planner_service.communication_field_snapshot()
    def set_communication_planning_field(self, payload):
        return self.layered_route_planner_service.set_communication_field(payload)
    def theta_v2_objective_policy(self):
        return deepcopy(self.state.get("theta_v2_objective_policy") or {})
    def set_theta_v2_objective_policy(self, payload):
        return self.layered_route_planner_service.set_objective_policy(payload)
    def max_route_risk_density(self):
        return deepcopy(self.state.get("max_route_risk_density") or {})
    def set_max_route_risk_density(self, payload):
        return self.layered_route_planner_service.set_risk_density_constraint(payload)
    # ---- BUG-ROUTE-005：规划用暴露度层（planning exposure floor） ---------------------
    def planning_exposure_policy(self):
        return deepcopy(self.state.get("planning_exposure_policy") or {})
    def set_planning_exposure_policy(self, payload):
        return self.layered_route_planner_service.set_planning_exposure_policy(payload)
    def planning_exposure(self):
        return self.layered_route_planner_service.planning_exposure_snapshot()
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

    # ---- Production layered candidate continuous validation/adoption ----------
    def layered_route_validation_readiness(self):
        return self.layered_route_validation_service.readiness_snapshot()

    def layered_route_validations(self):
        return self.layered_route_validation_service.result_snapshot()

    def evaluate_layered_route_validation(self, payload=None, evidence_adapter=None):
        return self.layered_route_validation_service.validate(
            payload, evidence_adapter=evidence_adapter,
        )

    def layered_operational_adoptions(self):
        return self.layered_operational_adoption_service.result_snapshot()

    def layered_operational_adoption_readiness(self):
        return self.layered_operational_adoption_service.readiness_snapshot()

    def project_layered_operational_adoption(self, payload=None):
        return self.layered_operational_adoption_service.projection(payload)

    def preview_layered_operational_adoption(self, payload=None):
        return self.layered_operational_adoption_service.preview(payload)

    def apply_layered_operational_adoption(self, payload=None):
        return self.layered_operational_adoption_service.apply(payload)

    def revoke_layered_operational_adoption(self, payload=None):
        return self.layered_operational_adoption_service.revoke(payload)

    # ---- Route Safety Evidence V2 (additive post-planning evidence aggregation) -----
    def route_safety_evidence_v2(self, payload=None):
        return self.route_safety_evidence_service.result_snapshot(payload)

    def route_safety_evidence_v2_readiness(self, payload=None):
        return self.route_safety_evidence_service.readiness_snapshot(payload)

    def evaluate_route_safety_evidence_v2(self, payload=None):
        return self.route_safety_evidence_service.evaluate(payload)

    # ---- Production Route3DProfile V1 (additive thin 3D-profile derivation) ---------
    def route_3d_profiles(self):
        return self.route_3d_profile_service.result_snapshot()

    def route_3d_profile_readiness(self):
        return self.route_3d_profile_service.readiness_snapshot()

    def evaluate_route_3d_profile(self, payload=None):
        """Explicit user-triggered derivation.  Nothing is ever generated in background."""

        result = self.route_3d_profile_service.evaluate(payload)
        # The profile now governs the effective vertical context of the route, so its existing
        # consumers are stale.  This propagation is strictly downstream.
        self.invalidation_service.route_3d_profile_changed("route_3d_profile_evaluated")
        self.session.save()
        return result

    def delete_route_3d_profile(self, payload=None):
        data = payload if isinstance(payload, dict) else {}
        result = self.route_3d_profile_service.delete(
            data.get("profile_id"), route_id=data.get("route_id"),
        )
        # Removing the profile changes the effective vertical context back to the constant
        # cruise layer, so the same downstream propagation applies.
        self.invalidation_service.route_3d_profile_changed("route_3d_profile_deleted")
        self.session.save()
        return result

    # ---- Vertical Transition Continuous Validation V1 (climb/descent source-native geometry) ---
    def vertical_transition_validations(self):
        return self.vertical_transition_validation_service.result_snapshot()

    def vertical_transition_validation_readiness(self, payload=None):
        return self.vertical_transition_validation_service.readiness_snapshot(payload)

    def evaluate_vertical_transition_validation(self, payload=None, evidence_adapter=None):
        """Explicit user-triggered climb/descent geometry validation.

        Nothing is ever evaluated in the background, and the stored ``Route3DProfile`` /
        ``LayeredRouteValidation`` are never rewritten.  A new record only makes the
        downstream Safety Evidence / report stale.
        """

        result = self.vertical_transition_validation_service.evaluate(
            payload, evidence_adapter=evidence_adapter,
        )
        self.invalidation_service.vertical_transition_validation_changed(
            "vertical_transition_validation_evaluated"
        )
        self.session.save()
        return result

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
    def apply_population_grid_attribute(self, result):
        """Targeted apply：只替换 ``grid_attributes.population``（population-only remap）。

        与 :meth:`apply_grid_attributes` 不同，这里**不**重算 terrain / buildings /
        airspace，也**不**自动重跑 Risk Framework V2：人口属性的变化只按既有
        invalidation 把依赖它的结果标为 stale，由用户显式决定下一步。
        """

        return self.risk_service.apply_population_grid_attribute(result)
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
