"""Stable /api route dispatch independent from BaseHTTPRequestHandler."""

from dataclasses import dataclass
from pathlib import Path

from ..gis.online_health import check_online_services
from ..safety.event_evaluator import evaluate_safety_events
from ..safety.fault_tree import evaluate_fault_tree
from ..safety.coupling import evaluate_coupled_events
from ..safety.service_state import evaluate_service_state
from .file_browser import browse
from ..tasks.task_specs import task_type_for_endpoint
from ..compatibility.catalog import CAPABILITY_FOR_SELECTION as _CAPABILITY_FOR_SELECTION
from ..compatibility.catalog import (
    CAPABILITY_FOR_SELECTION_ALGORITHM as _CAPABILITY_FOR_SELECTION_ALGORITHM,
)
from ..compatibility.catalog import with_capability_metadata


def _wants_async(payload):
    """业务 endpoint 是否要求异步执行（``"async": true`` 或 ``async_mode``）。"""

    if not isinstance(payload, dict):
        return False
    if payload.get("async_mode") is True:
        return True
    value = payload.get("async")
    if value is True:
        return True
    return str(value or "").strip().lower() in ("1", "true", "yes", "async")


def _task_type_text(task_type):
    from ..tasks.task_specs import has_task_type, task_spec

    return task_spec(task_type).task_name if has_task_type(task_type) else str(task_type)


@dataclass
class Response:
    data: object
    content_type: str = "application/json; charset=utf-8"
    status: int = 200
    cache: bool = False


#: artifact 读取失败的**用户可见**中文提示（技术 code 另走 ``code`` 字段）。
_ARTIFACT_ERROR_TEXT = {
    "artifact_missing": "结果明细文件不可用（文件缺失或被移动）；请重新计算该结果",
    "artifact_unavailable": "结果明细不可用；请重新计算该结果",
    "artifact_sha256_mismatch": "结果明细数据已损坏，请重新计算该结果",
    "artifact_gzip_corrupt": "结果明细数据已损坏，请重新计算该结果",
    "artifact_json_corrupt": "结果明细数据已损坏，请重新计算该结果",
    "artifact_envelope_invalid": "结果明细数据已损坏，请重新计算该结果",
    "artifact_schema_unsupported": "结果明细版本不受支持；请重新计算该结果",
    "artifact_encoding_unsupported": "结果明细编码不受支持；请重新计算该结果",
    "artifact_ref_missing": "该结果尚未生成明细文件；请先运行对应计算",
    "artifact_ref_outside_project": "结果明细路径无效，已拒绝访问",
    "artifact_path_missing": "结果明细路径缺失，已拒绝访问",
    "artifact_path_outside_project": "结果明细路径无效，已拒绝访问",
    "artifact_scope_unknown": "未登记的结果明细类型",
    "artifact_publish_failed": "结果明细写入失败，未替换任何已有结果",
}


class ApiRouter:
    def __init__(self, context):
        self.context = context

    def get(self, path, query, headers):
        context, workflow, data = self.context, self.context.workflow, self.context.data
        if path == "/api/compatibility/catalog":
            return Response(workflow.compatibility_catalog_snapshot())
        compatibility_reads = {
            "/api/compatibility/route-planner": (
                "RoutePlannerV1", "compatibility_route_snapshot"
            ),
            "/api/compatibility/coverage": (
                "CoveragePlannerV1", "compatibility_coverage_snapshot"
            ),
            "/api/compatibility/cns-gap-analysis-v1": (
                "CNSGapAnalyzerV1", "cns_gap_snapshot"
            ),
            "/api/compatibility/cns-gap-analysis-v2": (
                "CNSGapAnalyzerV2", "cns_gap_v2_snapshot"
            ),
            "/api/compatibility/site-plan": (
                "ReuseFirstSitePlannerV1", "cns_site_plan_snapshot"
            ),
        }
        if path in compatibility_reads:
            capability_id, reader = compatibility_reads[path]
            value = reader() if callable(reader) else getattr(workflow, reader)()
            return Response(with_capability_metadata(value, capability_id))
        research_gets = {
            "/api/research/route-planner-v3-experiments": "route_planner_v3_snapshot",
            "/api/research/route-planner-v3/readiness": "route_planner_v3_readiness",
            "/api/research/route-planner-v3/refinement-readiness": "route_planner_v3_refinement_readiness",
            "/api/research/route-planner-v3-refinements": "route_planner_v3_refinement_snapshot",
            "/api/research/route-planner-v3/continuous-readiness": "route_planner_v3_continuous_readiness",
            "/api/research/route-planner-v3-validations": "route_planner_v3_validation_snapshot",
            "/api/research/route-planner-v3-operational-adoptions": "v3_operational_adoptions_snapshot",
            "/api/research/route-planner-v3-operational-publish": "v3_operational_publish_status",
            "/api/research/v3-cns-assessment": "v3_cns_assessment_snapshot",
        }
        if path in research_gets:
            return Response(with_capability_metadata(getattr(workflow, research_gets[path])(), "RoutePlannerV3"))
        legacy_get_aliases = {
            "/api/cns-gaps": ("CNSGapAnalyzerV1", "cns_gap_snapshot"),
            "/api/cns-gap-analysis-v2": ("CNSGapAnalyzerV2", "cns_gap_v2_snapshot"),
            "/api/cns-site-plan": ("ReuseFirstSitePlannerV1", "cns_site_plan_snapshot"),
            "/api/route-planner-v3-experiments": ("RoutePlannerV3", "route_planner_v3_snapshot"),
            "/api/route-planner-v3/readiness": ("RoutePlannerV3", "route_planner_v3_readiness"),
            "/api/route-planner-v3/refinement-readiness": ("RoutePlannerV3", "route_planner_v3_refinement_readiness"),
            "/api/route-planner-v3-refinements": ("RoutePlannerV3", "route_planner_v3_refinement_snapshot"),
            "/api/route-planner-v3/continuous-readiness": ("RoutePlannerV3", "route_planner_v3_continuous_readiness"),
            "/api/route-planner-v3-validations": ("RoutePlannerV3", "route_planner_v3_validation_snapshot"),
            "/api/route-planner-v3-operational-adoptions": ("RoutePlannerV3", "v3_operational_adoptions_snapshot"),
            "/api/route-planner-v3-operational-publish": ("RoutePlannerV3", "v3_operational_publish_status"),
            "/api/v3-cns-assessment": ("RoutePlannerV3", "v3_cns_assessment_snapshot"),
        }
        if path in legacy_get_aliases:
            capability_id, reader = legacy_get_aliases[path]
            return Response(with_capability_metadata(getattr(workflow, reader)(), capability_id, alias=True))
        # ---- Phase4-B6X：持久 Heavy Task 查询（只读，不触发任何长计算） --------
        # 读取路径会按需"驱动"编排（收集已结束 worker、领取 queued 任务、心跳收尾），
        # 但绝不等待任何计算，也不持有 mutation_lock。
        if path == "/api/tasks":
            return Response(self._task_service().list(
                status=(query.get("status", [""])[0] or None),
                limit=int(query.get("limit", ["50"])[0] or 50),
            ))
        if path == "/api/tasks/catalog":
            return Response({"items": self._task_service().task_catalog()})
        if path.startswith("/api/tasks/"):
            task_id = path[len("/api/tasks/"):].strip("/")
            if task_id and "/" not in task_id:
                task = self._task_service().find(task_id)
                if task is None:
                    return Response({"error": "任务不存在"}, status=404)
                return Response(task)
        if path == "/api/health":
            # 保留原有 field 契约（service/ready/data_error），只附加最小运行身份：
            # 启动器据此判断 8765 上的服务是否属于本项目，而不是仅凭 service 名复用。
            payload = {"service": "cns-map", "ready": True, "data_error": data.error}
            identity = getattr(context, "health_identity", None)
            if identity:
                payload.update(identity)
            return Response(payload)
        if path == "/api/state": return Response(context.qgis.call(data.metadata))
        if path == "/api/data-sources": return Response(context.qgis.call(lambda: data.metadata()["data_sources"]))
        if path == "/api/data-health": return Response(context.qgis.call(lambda: data.metadata()["data_health"]))
        if path == "/api/workflow": return Response(workflow.snapshot())
        # ---- Phase4-B5X：canonical artifact 只读读取（summary / bounded / GC） ----
        # 全部经 Application 层的 ArtifactReadService；router 不打开 gzip、不读文件、
        # 不解析 artifact、不改 state。失败一律翻译成中文业务提示 + 技术 code。
        if path == "/api/artifacts": return Response(workflow.artifact_summaries())
        if path == "/api/artifacts/manifest": return Response(workflow.artifact_manifest())
        if path == "/api/artifacts/inventory": return Response(workflow.artifact_inventory())
        if path == "/api/artifacts/summary":
            return self._artifact_response(
                lambda: workflow.artifact_summary(self._first(query, "logical_key", ""))
            )
        if path == "/api/artifacts/content":
            return self._artifact_response(lambda: workflow.artifact_content({
                "logical_key": self._first(query, "logical_key", ""),
                "route_id": self._first(query, "route_id", "") or None,
                "subsystem": self._first(query, "subsystem", "") or None,
                "grid_ids": self._first(query, "grid_ids", "") or None,
                "bbox": self._first(query, "bbox", "") or None,
                "offset": self._first(query, "offset", "") or 0,
                "limit": self._first(query, "limit", "") or None,
            }))
        if path == "/api/workspace/grid": return Response(workflow.grid_snapshot())
        if path == "/api/workspace/grid/attributes": return Response(workflow.grid_attributes_snapshot())
        if path == "/api/aircraft-profiles": return Response(workflow.aircraft_profiles_snapshot())
        if path == "/api/device-catalog": return Response(workflow.device_catalog_snapshot())
        if path == "/api/reference-landing-sites": return Response(workflow.reference_landing_sites_snapshot())
        if path == "/api/reference-routes": return Response(workflow.reference_routes_snapshot())
        if path == "/api/airspace-policies": return Response(workflow.airspace_policies_snapshot())
        if path == "/api/equipment-reference-catalog": return Response(workflow.equipment_reference_catalog_snapshot())
        if path == "/api/required-cns": return Response(workflow.required_cns_snapshot())
        if path == "/api/cns-operation-context": return Response(workflow.cns_operation_context_snapshot())
        if path == "/api/cns-requirement-policies": return Response(workflow.cns_requirement_policies_snapshot())
        if path == "/api/cns-required-recommendation": return Response(workflow.required_cns_recommendation_snapshot())
        if path == "/api/existing-cns": return Response(workflow.existing_cns_snapshot())
        if path == "/api/candidate-sites": return Response(workflow.candidate_sites_snapshot())
        # ---- Towers Operational Integration V2（铁塔派生事实；只读投影） --------------
        if path == "/api/tower-obstacle-profiles": return Response(workflow.tower_obstacle_profiles_snapshot())
        if path == "/api/tower-colocation-candidates": return Response(workflow.tower_colocation_candidates_snapshot())
        if path == "/api/tower-integration-policies": return Response(workflow.tower_integration_policies_snapshot())
        if path == "/api/cns-gaps": return Response(workflow.cns_gap_snapshot())
        if path == "/api/cns-gap-analysis-v2": return Response(workflow.cns_gap_v2_snapshot())
        if path == "/api/cns-site-plan": return Response(workflow.cns_site_plan_snapshot())
        if path == "/api/cns-closed-loop": return Response(workflow.closed_loop_snapshot())
        if path == "/api/cns-service-corridor": return Response(workflow.cns_corridor_snapshot())
        if path == "/api/cns-planning-objectives": return Response(workflow.cns_planning_objectives_snapshot())
        if path == "/api/cns-corridor-gap": return Response(workflow.cns_corridor_gap_snapshot())
        if path == "/api/cns-corridor-site-plan": return Response(workflow.cns_corridor_site_plan_snapshot())
        if path == "/api/cns-plan-review": return Response(workflow.cns_plan_review_snapshot())
        if path == "/api/cns-planning-report": return Response(workflow.cns_planning_report_snapshot())
        if path == "/api/cns-planning-report/artifact":
            report_id = query.get("report_id", [""])[0]
            kind = query.get("kind", [""])[0]
            content, content_type = workflow.cns_planning_report_artifact(report_id, kind)
            return Response(content, content_type)
        if path == "/api/spatial-3d": return Response(workflow.spatial_3d_snapshot())
        if path == "/api/spatial-3d/readiness": return Response(workflow.route_operating_readiness())
        if path == "/api/route-operating-plan": return Response(workflow.route_operating_plan())
        if path == "/api/layered-route-planner/readiness": return Response(workflow.layered_route_planner_readiness())
        if path == "/api/layered-route-planning-request": return Response(workflow.layered_route_planning_request())
        if path == "/api/layered-route-feasibility-policy": return Response(workflow.layered_route_feasibility_policy())
        if path == "/api/layered-route-cost-policy": return Response(workflow.layered_route_cost_policy())
        if path == "/api/layered-route-candidates": return Response(workflow.layered_route_candidates())
        if path == "/api/planning-constraint-fields":
            return Response(workflow.planning_constraint_fields(
                self._first(query, "altitude_layer_id", "") or None
            ))
        # B4X 只读展示读取路径：地图友好的紧凑约束结果（grid_id / outcome / blocked_by）。
        # 浏览器不接触任何文件系统路径；这里不返回 evidence、也不返回完整 raw artifact。
        # 几何复用前端已有的 grid_id → cell.bbox 索引，因此不重复下发 GeoJSON。
        if path == "/api/planning-constraint-field":
            return Response(workflow.planning_constraint_fields(
                self._first(query, "altitude_layer_id", "") or None
            ))
        if path == "/api/planning-constraint-field/map":
            return Response(workflow.planning_constraint_field_map(
                self._first(query, "altitude_layer_id", "") or None,
                self._first(query, "bbox", "") or None,
            ))
        # ---- Layered Risk-Aware Theta* V2 additive interfaces ------------------------
        if path == "/api/shelter-coefficient-policy": return Response(workflow.shelter_coefficient_policy())
        if path == "/api/population-nodata-policy": return Response(workflow.population_nodata_policy())
        if path == "/api/population-shelter": return Response(workflow.population_shelter())
        if path == "/api/regulatory-constraints": return Response(workflow.regulatory_constraints())
        if path == "/api/communication-planning-field": return Response(workflow.communication_planning_field())
        if path == "/api/theta-v2-objective-policy": return Response(workflow.theta_v2_objective_policy())
        if path == "/api/max-route-risk-density": return Response(workflow.max_route_risk_density())
        # BUG-ROUTE-005：规划用暴露度层（规划专用，不进人口报告 / 审计）。
        if path == "/api/planning-exposure-policy": return Response(workflow.planning_exposure_policy())
        if path == "/api/planning-exposure": return Response(workflow.planning_exposure())
        # ---- RouteRiskProfile V1 (additive; analysis of a current layered candidate) ----
        if path == "/api/route-risk-profile/readiness": return Response(workflow.route_risk_profile_readiness())
        if path == "/api/route-risk-profile-policy": return Response(workflow.route_risk_profile_policy())
        if path == "/api/route-risk-profiles": return Response(workflow.route_risk_profiles())
        if path == "/api/layered-route-validation/readiness": return Response(workflow.layered_route_validation_readiness())
        if path == "/api/layered-route-validations": return Response(workflow.layered_route_validations())
        if path == "/api/layered-operational-adoption/readiness": return Response(workflow.layered_operational_adoption_readiness())
        if path == "/api/layered-operational-adoptions": return Response(workflow.layered_operational_adoptions())
        if path == "/api/route-safety-evidence-v2/readiness": return Response(workflow.route_safety_evidence_v2_readiness())
        if path == "/api/route-safety-evidence-v2": return Response(workflow.route_safety_evidence_v2())
        # ---- Production Route3DProfile V1 (additive thin 3D-profile derivation) ---------
        if path == "/api/route-3d-profiles/readiness": return Response(workflow.route_3d_profile_readiness())
        if path == "/api/route-3d-profiles": return Response(workflow.route_3d_profiles())
        # ---- Radar Surveillance Layout V1 (additive, proposal-only) ---------------------
        # 「80m固定高度航路方向性雷达几何初步划设方案」。完整结果（含逐 sample 明细）走
        # 专用接口；通用快照只带上有界摘要。
        if path == "/api/radar-surveillance-layout/readiness":
            demo_preview_only = str(
                self._first(query, "demo_preview_only", "false")
            ).lower() in ("1", "true", "yes", "on")
            payload = (
                {
                    "demo_preview_only": True,
                    "route_source": self._first(
                        query, "route_source", "current_layered_candidate",
                    ),
                }
                if demo_preview_only else None
            )
            return Response(workflow.radar_surveillance_layout_readiness(payload))
        if path == "/api/radar-surveillance-policy":
            return Response(workflow.radar_surveillance_policy())
        if path == "/api/radar-surveillance-layout":
            route_id = self._first(query, "route_id", "") or None
            return Response(workflow.radar_surveillance_layout(route_id))
        # ---- Vertical Transition Continuous Validation V1 (climb/descent geometry) ------
        if path == "/api/vertical-transition-validation/readiness":
            return Response(workflow.vertical_transition_validation_readiness(query))
        if path == "/api/vertical-transition-validations":
            return Response(workflow.vertical_transition_validations())
        if path == "/api/coverage-3d": return Response(workflow.coverage_3d_snapshot())
        if path == "/api/cns-service-capability": return Response(workflow.cns_service_capability_snapshot())
        if path == "/api/operational-timing": return Response(workflow.operational_timing_snapshot())
        if path == "/api/service-timeline": return Response(workflow.service_timeline_snapshot())
        if path == "/api/protection-envelope": return Response(workflow.protection_envelope_snapshot())
        if path == "/api/cns/safety-policy": return Response(workflow.safety_policy_snapshot())
        if path == "/api/building-clearance/policy": return Response(workflow.building_clearance_policy_snapshot())
        if path == "/api/building-clearance": return Response(workflow.building_clearance_snapshot())
        # ---- Risk Framework V2 (additive; legacy grid_risk stays authoritative) ----
        if path == "/api/grid-risk-v2": return Response(workflow.grid_risk_v2_snapshot())
        if path == "/api/risk-policy-v2": return Response(workflow.risk_policy_v2_snapshot())
        if path == "/api/risk-framework-v2/readiness": return Response(workflow.risk_framework_v2_readiness())
        if path == "/api/route-vertical-profiles": return Response(workflow.route_vertical_profiles_snapshot())
        if path == "/api/route-experiments": return Response(workflow.route_experiments_snapshot())
        if path == "/api/route-planner-v3-experiments": return Response(workflow.route_planner_v3_snapshot())
        if path == "/api/route-planner-v3/readiness": return Response(workflow.route_planner_v3_readiness())
        if path == "/api/route-planner-v3/refinement-readiness": return Response(workflow.route_planner_v3_refinement_readiness())
        if path == "/api/route-planner-v3-refinements": return Response(workflow.route_planner_v3_refinement_snapshot())
        if path == "/api/route-planner-v3/continuous-readiness": return Response(workflow.route_planner_v3_continuous_readiness())
        if path == "/api/route-planner-v3-validations": return Response(workflow.route_planner_v3_validation_snapshot())
        if path == "/api/route-planner-v3-operational-adoptions": return Response(workflow.v3_operational_adoptions_snapshot())
        if path == "/api/route-planner-v3-operational-publish": return Response(workflow.v3_operational_publish_status())
        if path == "/api/v3-cns-assessment": return Response(workflow.v3_cns_assessment_snapshot())
        if path == "/api/reference-route-links": return Response(workflow.reference_route_links_snapshot())
        if path == "/api/reference-endpoint-candidates": return Response(workflow.reference_endpoint_candidates_snapshot())
        if path == "/api/data-readiness": return Response(workflow.data_readiness_snapshot())
        if path == "/api/source-audits": return Response(workflow.source_audits_snapshot())
        if path == "/api/encounter-3d": return Response(workflow.encounter_3d_snapshot())
        if path == "/api/algorithms": return Response(workflow.algorithms_snapshot())
        if path == "/api/online-health": return Response(check_online_services(data))
        if path == "/api/export/project": return Response(workflow.export_project())
        if path == "/api/export/routes": return Response(workflow.export_routes(), "application/geo+json; charset=utf-8")
        if path == "/api/export/sites": return Response(workflow.export_sites(), "application/geo+json; charset=utf-8")
        if path == "/api/render":
            context.render_requests.record(query)
            return Response(context.qgis.call(lambda: data.render(query)), "image/png")
        if path == "/api/tile":
            source = data.online_sources.get(query.get("source", [""])[0])
            if not source: raise ValueError("在线底图来源不存在")
            z, x, y = (int(query[key][0]) for key in ("z", "x", "y"))
            if not source["zmin"] <= z <= source["zmax"]: raise ValueError("底图缩放级别超出范围")
            tile, mime = context.tiles.get(source["template"], z, x, y)
            return Response(tile, mime, cache=True)
        if path == "/api/browse": return Response(browse(query.get("path", [""])[0], query.get("kind", ["basemap"])[0]))
        # ---- 建筑轮廓图层（只读）：qgz/gpkg/shp/geojson → GeoJSON，按视图 bbox 按需拉取 ----
        if path == "/api/building-footprints":
            return Response(context.qgis.call(lambda: self._building_footprints(query)))
        static = self._static(path)
        if static: return static
        return Response({"error": "未找到"}, status=404)

    @staticmethod
    def _first(query, key, default=""):
        value = query.get(key)
        if isinstance(value, (list, tuple)):
            return value[0] if value else default
        return default if value is None else value

    @staticmethod
    def _artifact_response(producer):
        """artifact 读取的统一出口：失败变成中文业务提示 + 技术 code。

        普通用户只看到"明细不可用 / 需要重新计算"；``code``（artifact_missing /
        artifact_sha256_mismatch / artifact_gzip_corrupt …）供前端的"高级/审计"
        区域展示。绝不把"读不到"降级成空结果或"全部可通行"。
        """

        from ..persistence.artifact_store import ArtifactError

        try:
            return Response(producer())
        except ArtifactError as exc:
            return Response(
                {
                    "error": _ARTIFACT_ERROR_TEXT.get(
                        exc.code, "结果明细不可用，请重新计算该结果"
                    ),
                    "code": exc.code,
                    "detail": exc.detail,
                    "recompute_required": True,
                },
                status=409,
            )

    def _building_footprints(self, query):
        """只读建筑轮廓 GeoJSON。

        浏览器不能读 ``.qgz``：这里由后端解析工程 → 定位建筑 Polygon 图层 → 按当前视图
        bbox 走空间索引查询 → 返回 GeoJSON。**不写任何项目状态**，也不参与净空判定。
        """

        from ..gis.building_footprint_aggregation import footprints_geojson

        data = self.context.data
        role = str(self._first(query, "role", "buildings") or "buildings")
        if role not in ("buildings", "building_grid"):
            role = "buildings"
        resolved = data.vector_role_source(role)
        if not resolved.get("ok"):
            return {
                "status": "unavailable", "role": role,
                "reason": resolved.get("reason") or "建筑数据源不可用",
                "type": "FeatureCollection", "features": [], "count": 0,
            }
        raw_bbox = str(self._first(query, "bbox", "") or "")
        try:
            box = [float(value) for value in raw_bbox.split(",")]
        except (TypeError, ValueError):
            box = []
        try:
            limit = int(float(self._first(query, "limit", 2000) or 2000))
        except (TypeError, ValueError):
            limit = 2000
        raw_tolerance = self._first(query, "tolerance", "")
        try:
            tolerance = float(raw_tolerance) if str(raw_tolerance).strip() else None
        except (TypeError, ValueError):
            tolerance = None
        result = footprints_geojson(
            resolved.get("path"), box, limit=limit, tolerance_deg=tolerance,
            layer_name=resolved.get("layer_name"),
        )
        result["role"] = role
        result["resolved_via"] = resolved.get("source")
        result["layer_title"] = resolved.get("layer_title")
        return result

    def post(self, path, payload):
        context, workflow, data = self.context, self.context.workflow, self.context.data
        compatibility_actions = {
            "/api/compatibility/route-planner/evaluate": (
                "RoutePlannerV1", lambda: workflow.generate_operational(data.hard_constraints)
            ),
            "/api/compatibility/coverage/evaluate": (
                "CoveragePlannerV1", lambda: workflow.plan_coverage()
            ),
            "/api/compatibility/cns-gap-analysis-v1/evaluate": (
                "CNSGapAnalyzerV1", lambda: workflow.analyze_cns_gaps()
            ),
            "/api/compatibility/cns-gap-analysis-v2/evaluate": (
                "CNSGapAnalyzerV2", lambda: workflow.analyze_cns_gaps_v2(payload)
            ),
            "/api/compatibility/site-plan/evaluate": (
                "ReuseFirstSitePlannerV1", lambda: workflow.evaluate_cns_site_plan(payload)
            ),
        }
        if path in compatibility_actions:
            capability_id, action = compatibility_actions[path]
            return Response(with_capability_metadata(action(), capability_id))
        if path == "/api/compatibility/selection":
            # 运行期 compatibility 参数覆盖：只影响后续 compatibility 试算，绝不写
            # ProjectState.algorithm_selection（旧 /api/algorithms/select 通道已关闭）。
            selection = workflow.set_compatibility_selection(payload)
            return Response(with_capability_metadata(
                selection,
                _CAPABILITY_FOR_SELECTION_ALGORITHM.get(
                    str(selection.get("algorithm_id")),
                    _CAPABILITY_FOR_SELECTION.get(selection["algorithm_type"], "RoutePlannerV1"),
                ),
            ))

        original_path = path
        if path.startswith("/api/research/route-planner-v3"):
            path = "/api/" + path[len("/api/research/"):]
        elif path.startswith("/api/research/v3-cns-assessment"):
            path = "/api/" + path[len("/api/research/"):]
        research_request = original_path != path
        # ---- Phase4-B6X：heavy task 提交（HTTP 202 + task_id） ------------------
        # 真正重的动作不再在 HTTP 线程与 mutation_lock 内跑完：
        #   * 业务 endpoint 带 ``async: true`` 时只登记任务；
        #   * 轻任务完全不受影响（不传 ``async`` 时仍是原来的同步语义）。
        if path == "/api/tasks" or path == "/api/tasks/submit":
            return self._task_submit(payload.get("task_type"), payload)
        if path == "/api/tasks/cancel":
            return self._task_cancel(payload.get("task_id"))
        if path.startswith("/api/tasks/") and path.endswith("/cancel"):
            return self._task_cancel(path[len("/api/tasks/"):-len("/cancel")].strip("/"))
        if path.startswith("/api/tasks/"):
            task_id = path[len("/api/tasks/"):].strip("/")
            if task_id and "/" not in task_id:
                return self._task_submit(payload.get("task_type"), payload, task_id=task_id)
        task_type = task_type_for_endpoint(path)
        if task_type and _wants_async(payload):
            return self._task_submit(task_type, payload)
        if path == "/api/cns/service-state/evaluate":
            return Response(evaluate_service_state(
                payload.get("required_cns", payload.get("required")),
                payload.get("aircraft_capability", payload.get("aircraft")),
                payload.get("external_service_snapshot", payload.get("external_service")),
                payload.get("confirmed_fallback"),
            ))
        if path == "/api/cns/events/evaluate":
            return Response(evaluate_safety_events(
                payload.get("failure_condition"),
                payload.get("service_state"),
                payload.get("operational_context"),
                payload.get("unacceptable_event"),
            ))
        if path == "/api/cns/fault-tree/evaluate":
            return Response(evaluate_fault_tree(
                payload.get("fault_tree", payload.get("tree")),
                payload.get("event_states"),
            ))
        if path == "/api/cns/coupled-events/evaluate":
            return Response(evaluate_coupled_events(
                payload.get("functional_dependency"),
                payload.get("coupled_condition"),
                payload.get("observations"),
                payload.get("operational_context"),
                payload.get("coupled_unacceptable_event"),
            ))
        if path == "/api/project/save-as": return Response(context.qgis.call(lambda: context.save_project_as(payload.get("project_dir"))))
        if path == "/api/project/open": return Response(context.qgis.call(lambda: context.open_project(payload.get("project_dir"))))
        resource_actions = {
            "/api/aircraft-profiles/select": lambda: workflow.select_aircraft_profile(payload.get("aircraft_id")),
            "/api/aircraft-profiles/import": lambda: workflow.import_aircraft_catalog(payload.get("path")),
            "/api/device-catalog/import": lambda: workflow.import_device_catalog(payload.get("path")),
            "/api/reference-landing-sites/import": lambda: workflow.import_reference_landing_sites(payload.get("path") or data.paths.get("reference_landing_sites")),
            "/api/reference-routes/import": lambda: workflow.confirm_reference_routes_import(payload.get("path") or data.paths.get("reference_routes"), payload.get("preview_id")),
            "/api/reference-routes/preview": lambda: workflow.preview_reference_routes(payload.get("path") or data.paths.get("reference_routes"), {"conversion_method": payload.get("conversion_method"), "evidence": payload.get("evidence")} if payload.get("conversion_method") or payload.get("evidence") else None),
            "/api/reference-routes/import-confirm": lambda: workflow.confirm_reference_routes_import(payload.get("path") or data.paths.get("reference_routes"), payload.get("preview_id")),
            "/api/reference-crs/confirm": lambda: workflow.confirm_reference_crs(payload.get("role"), payload),
            "/api/towers/import": lambda: workflow.import_towers(payload.get("path") or data.paths.get("towers")),
            "/api/airspace-policies": lambda: workflow.set_airspace_policies(payload),
            "/api/airspace-policies/item": lambda: workflow.set_airspace_policy(payload),
            "/api/airspace-policies/batch": lambda: workflow.batch_set_airspace_policies(payload),
            "/api/reference-landing-sites/add-to-project": lambda: workflow.add_reference_landing_site(payload.get("reference_site_id")),
            "/api/required-cns": lambda: workflow.set_required_cns(payload),
            "/api/cns-operation-context": lambda: workflow.set_cns_operation_context(payload),
            "/api/cns-requirement-policies": lambda: workflow.set_cns_requirement_policies(payload),
            "/api/cns-required-recommendation/evaluate": lambda: workflow.evaluate_required_cns_recommendation(payload),
            "/api/cns-required-recommendation/adopt": lambda: workflow.adopt_required_cns_recommendation(payload),
            "/api/existing-cns/import": lambda: workflow.import_existing_cns(payload),
            "/api/candidate-sites/import": lambda: workflow.import_candidate_sites(payload),
            "/api/candidate-sites/from-existing": workflow.candidate_sites_from_existing,
            # 铁塔派生事实（障碍物高度 + 共塔宿主候选）：真实源只在 QGIS 线程读取。
            "/api/tower-obstacle-profiles/evaluate": lambda: (
                context.evaluate_tower_obstacle_profiles(payload)
                if hasattr(context, "evaluate_tower_obstacle_profiles")
                else context.qgis.call(
                    lambda: workflow.evaluate_tower_obstacle_profiles(payload)
                )
            ),
            # Tower Clearance Policy（Step03 航路净空配置）：只写既有 state 字段，
            # 没有默认值，未配置时 readiness 明确 not_configured 且 mask 对含塔格 fail-closed。
            "/api/tower-clearance-policy": lambda: workflow.set_tower_clearance_policy(payload),
            "/api/cns-gaps/analyze": workflow.analyze_cns_gaps,
            "/api/cns-gap-analysis-v2": lambda: workflow.analyze_cns_gaps_v2(payload),
            "/api/cns-site-plan": lambda: workflow.evaluate_cns_site_plan(payload),
            "/api/cns-closed-loop/evaluate": lambda: workflow.evaluate_closed_loop(payload),
            "/api/cns-closed-loop/apply": lambda: workflow.apply_closed_loop(payload),
            "/api/cns-service-corridor/evaluate": lambda: workflow.evaluate_cns_corridor(payload),
            "/api/cns-planning-objectives": lambda: workflow.set_cns_planning_objectives(payload),
            "/api/cns-corridor-gap/evaluate": lambda: workflow.evaluate_cns_corridor_gap(payload),
            "/api/cns-corridor-site-plan/evaluate": lambda: workflow.evaluate_cns_corridor_site_plan(payload),
            "/api/cns-plan-review/initialize": lambda: workflow.initialize_cns_plan_review(payload),
            "/api/cns-plan-review/variant": lambda: workflow.create_cns_plan_variant(payload),
            "/api/cns-plan-review/evaluate": lambda: workflow.evaluate_cns_plan_variant(payload),
            "/api/cns-plan-review/select": lambda: workflow.select_cns_plan_variant(payload),
            "/api/cns-plan-review/confirm": lambda: workflow.confirm_cns_plan(payload),
            "/api/cns-plan-review/apply": lambda: workflow.apply_confirmed_cns_plan(payload),
            "/api/cns-planning-report/preview": lambda: workflow.preview_cns_planning_report(payload),
            "/api/cns-planning-report/generate": lambda: workflow.generate_cns_planning_report(payload),
            "/api/cns/safety-policy": lambda: workflow.set_safety_policy(payload),
            "/api/building-clearance/policy": lambda: workflow.set_building_clearance_policy(payload),
            "/api/risk-policy-v2": lambda: workflow.set_risk_policy_v2(payload),
            "/api/grid-risk-v2/evaluate": lambda: workflow.evaluate_grid_risk_v2(payload),
            "/api/building-clearance/evaluate": lambda: context.qgis.call(context.evaluate_building_clearance),
            "/api/route-vertical-profiles/evaluate": lambda: context.qgis.call(lambda: context.evaluate_route_vertical_profiles(payload)),
            "/api/route-experiments/evaluate": lambda: workflow.evaluate_route_experiment(payload),
            "/api/route-experiments/delete": lambda: workflow.delete_route_experiment(payload.get("experiment_id")),
            "/api/route-planner-v3/policy": lambda: workflow.set_route_planner_v3_policy(payload),
            "/api/route-planner-v3/fine-policy": lambda: workflow.set_route_planner_v3_fine_policy(payload),
            "/api/route-planner-v3-experiments/evaluate": lambda: (
                context.evaluate_route_planner_v3(payload)
                if hasattr(context, "evaluate_route_planner_v3")
                else workflow.evaluate_route_planner_v3(payload)
            ),
            "/api/route-planner-v3-experiments/delete": lambda: workflow.delete_route_planner_v3_experiment(payload.get("experiment_id")),
            "/api/route-planner-v3-refinements/evaluate": lambda: workflow.evaluate_route_planner_v3_refinement(payload),
            # GIS-wired variant: builds the real fine-environment adapter (needs QGIS/GDAL).
            "/api/route-planner-v3-refinements/evaluate-real": lambda: context.qgis.call(
                lambda: context.evaluate_route_planner_v3_refinement(payload)
            ),
            # ---- V3-C: continuous geometry + source-native validation ----------
            "/api/route-planner-v3/validation-policy": lambda: workflow.set_route_planner_v3_validation_policy(payload),
            "/api/route-planner-v3-validations/evaluate": lambda: workflow.evaluate_route_planner_v3_continuous_validation(payload),
            # GIS-wired variant: builds the V3-C evidence adapter (needs QGIS/GDAL).
            "/api/route-planner-v3-validations/evaluate-real": lambda: context.qgis.call(
                lambda: context.evaluate_route_planner_v3_continuous_validation(payload)
            ),
            # ---- V3-D: operational adoption + CNS assessment bridge ------------
            "/api/route-planner-v3-operational-adoptions/preview": lambda: workflow.preview_v3_operational_adoption(payload),
            "/api/route-planner-v3-operational-adoptions/apply": lambda: workflow.apply_v3_operational_adoption(payload),
            "/api/route-planner-v3-operational-adoptions/revoke": lambda: workflow.revoke_v3_operational_adoption(payload),
            "/api/v3-cns-assessment/evaluate": lambda: workflow.assess_v3_adopted_route(payload),
            "/api/reference-route-links/create": lambda: workflow.create_reference_route_link(payload),
            "/api/reference-route-links/delete": lambda: workflow.delete_reference_route_link(payload.get("link_id")),
            "/api/algorithms/select": lambda: workflow.select_registered_algorithm(payload),
            "/api/spatial-3d/altitude-layers": lambda: workflow.set_altitude_layers(payload),
            "/api/spatial-3d/altitude-layer": lambda: workflow.set_altitude_layer(payload),
            "/api/spatial-3d/altitude-layer/delete": lambda: workflow.delete_altitude_layer(payload),
            "/api/spatial-3d/route-operating-layer": lambda: workflow.set_route_operating_layer(payload),
            "/api/spatial-3d/route-operating-layer/delete": lambda: workflow.delete_route_operating_layer(payload),
            "/api/spatial-3d/departure-arrival-procedure": lambda: workflow.set_departure_arrival_procedure(payload),
            "/api/spatial-3d/departure-arrival-procedure/delete": lambda: workflow.delete_departure_arrival_procedure(payload),
            "/api/spatial-3d/route-profile": lambda: workflow.set_route_altitude_profile(payload),
            # ---- Layered Risk-Aware Route Planner V1 --------------------------------
            "/api/layered-route-planning-request": lambda: workflow.set_layered_route_planning_request(payload),
            "/api/layered-route-feasibility-policy": lambda: workflow.set_layered_route_feasibility_policy(payload),
            "/api/layered-route-cost-policy": lambda: workflow.set_layered_route_cost_policy(payload),
            "/api/shelter-coefficient-policy": lambda: workflow.set_shelter_coefficient_policy(payload),
            "/api/population-nodata-policy": lambda: workflow.set_population_nodata_policy(payload),
            "/api/regulatory-constraints": lambda: workflow.set_regulatory_constraints(payload),
            "/api/communication-planning-field": lambda: workflow.set_communication_planning_field(payload),
            "/api/theta-v2-objective-policy": lambda: workflow.set_theta_v2_objective_policy(payload),
            "/api/max-route-risk-density": lambda: workflow.set_max_route_risk_density(payload),
            "/api/planning-exposure-policy": lambda: workflow.set_planning_exposure_policy(payload),
            "/api/layered-route-candidates/evaluate": lambda: workflow.evaluate_layered_route_candidate(payload),
            "/api/planning-constraint-fields/evaluate": lambda: workflow.generate_planning_constraint_field(payload),
            "/api/layered-route-candidates/evaluate-real": lambda: context.qgis.call(
                lambda: context.evaluate_layered_route_candidate(payload)
            ),
            "/api/layered-route-candidates/delete": lambda: workflow.delete_layered_route_candidate(
                payload.get("candidate_id")
            ),
            # ---- RouteRiskProfile V1 -------------------------------------------------
            "/api/route-risk-profile-policy": lambda: workflow.set_route_risk_profile_policy(payload),
            "/api/route-risk-profiles/evaluate": lambda: workflow.evaluate_route_risk_profile(payload),
            "/api/route-risk-profiles/delete": lambda: workflow.delete_route_risk_profile(
                payload.get("profile_id")
            ),
            # ---- Production layered candidate continuous validation/adoption --------
            "/api/layered-route-validations/evaluate": lambda: workflow.evaluate_layered_route_validation(payload),
            "/api/layered-route-validations/evaluate-real": lambda: context.qgis.call(
                lambda: context.evaluate_layered_route_validation(payload)
            ),
            "/api/layered-operational-adoptions/project": lambda: workflow.project_layered_operational_adoption(payload),
            "/api/layered-operational-adoptions/preview": lambda: workflow.preview_layered_operational_adoption(payload),
            "/api/layered-operational-adoptions/apply": lambda: workflow.apply_layered_operational_adoption(payload),
            "/api/layered-operational-adoptions/revoke": lambda: workflow.revoke_layered_operational_adoption(payload),
            # ---- Route Safety Evidence V2 (additive evidence aggregation) -----------
            # No background evaluation exists: one explicit POST evaluates one assessment.
            "/api/route-safety-evidence-v2/evaluate": lambda: workflow.evaluate_route_safety_evidence_v2(payload),
            # ---- Production Route3DProfile V1 ---------------------------------------
            # No automatic generation: the user explicitly evaluates one route (or "all").
            "/api/route-3d-profiles/evaluate": lambda: workflow.evaluate_route_3d_profile(payload),
            "/api/route-3d-profiles/delete": lambda: workflow.delete_route_3d_profile(payload),
            # ---- Radar Surveillance Layout V1（proposal-only，additive） --------------
            # 一次显式用户动作 = 一次求解；绝不自动生成，也绝不自动 apply proposal。
            "/api/radar-surveillance-policy": lambda: workflow.set_radar_surveillance_policy(payload),
            "/api/radar-surveillance-layout/evaluate": lambda: (
                context.evaluate_radar_surveillance_layout(payload)
                if hasattr(context, "evaluate_radar_surveillance_layout")
                else workflow.evaluate_radar_surveillance_layout(payload)
            ),
            # ---- Vertical Transition Continuous Validation V1 -----------------------
            # No automatic evaluation: one explicit POST evaluates the two transitions of one
            # route against the configured real FABDEM/buildings sources.
            "/api/vertical-transition-validations/evaluate-real": lambda: (
                context.evaluate_vertical_transition_validation(payload)
                if hasattr(context, "evaluate_vertical_transition_validation")
                else context.qgis.call(
                    lambda: workflow.evaluate_vertical_transition_validation(payload)
                )
            ),
            "/api/coverage-3d/evaluate": lambda: workflow.evaluate_coverage_3d(payload),
            "/api/cns-service-capability/evaluate": lambda: workflow.evaluate_cns_service_capability(),
            "/api/operational-timing": lambda: workflow.set_operational_timing(payload),
            "/api/service-timeline/evaluate": lambda: workflow.evaluate_service_timeline(payload),
            "/api/protection-envelope/evaluate": lambda: workflow.evaluate_protection_envelope(payload),
            "/api/encounter-3d/evaluate": lambda: workflow.evaluate_encounter_3d(payload),
        }
        if path in resource_actions:
            result = resource_actions[path]()
            compatibility_aliases = {
                "/api/cns-gaps/analyze": "CNSGapAnalyzerV1",
                "/api/cns-gap-analysis-v2": "CNSGapAnalyzerV2",
                "/api/cns-site-plan": "ReuseFirstSitePlannerV1",
            }
            capability_id = compatibility_aliases.get(path)
            if capability_id:
                result = with_capability_metadata(result, capability_id, alias=True)
            elif research_request or path.startswith("/api/route-planner-v3") or path.startswith("/api/v3-cns-assessment"):
                result = with_capability_metadata(
                    result, "RoutePlannerV3", alias=not research_request,
                )
            return Response(result)
        if path == "/api/workspace/grid/population/remap":
            # Population-only remap（BUG-POP-001）：在 QGIS 线程上读取**当前** grid、
            # **当前**人口源与**当前** NoData 语义确认，只执行 PopulationGridService.map。
            # 不重算 terrain / buildings / airspace，不重新生成网格，也不动 nodes / scenario_routes。
            # 读取与写入是一次用户动作 → 用 deferred_save 折叠成**一次**提交（无中间态）。
            with workflow.deferred_save():
                result = context.qgis.call(
                    lambda: data.population_grid_attribute(workflow.grid_snapshot())
                )
                return Response(workflow.apply_population_grid_attribute(result))
        if path == "/api/source-audits/verify":
            role = str(payload.get("role") or "")
            path_role = "basemap" if role == "airspace" else role
            source_path = data.paths.get(path_role)
            if not source_path:
                raise ValueError("数据源尚未在本机配置")
            details = None
            if path_role in ("buildings", "building_grid"):
                from ..gis.source_inspection import inspect_geopackage
                info = inspect_geopackage(source_path, path_role, deep_geometry=True)
                details = {
                    "schema": {"fields": sorted((info.get("fields") or {}).keys())},
                    "feature_count": info.get("feature_count"),
                    "extent": info.get("extent"), "declared_crs": info.get("crs"),
                    "geometry_health": info.get("geometry_health"),
                }
            return Response(workflow.verify_source(path_role, source_path, details))
        if path.startswith("/api/workflow/"):
            action = path.rsplit("/", 1)[-1]
            if action == "project": return Response(workflow.set_project(payload))
            if action == "workspace":
                health = context.qgis.call(lambda: data.workspace_health(payload.get("bbox")))
                # Mapping the workspace and recomputing its attributes is ONE user action and
                # therefore ONE commit: a concurrent reader must never observe the workspace
                # with its attributes already cleared but not yet recomputed.
                with workflow.deferred_save():
                    workflow.set_workspace(
                        payload.get("bbox"), health, payload.get("grid_level"),
                        payload.get("max_cells"),
                    )
                    results = context.qgis.call(lambda: data.grid_attributes(workflow.grid_snapshot()))
                    return Response(workflow.apply_grid_attributes(results))
            actions = {
                "workspace-clear": lambda: workflow.clear_workspace(),
                "traffic-simulate": lambda: workflow.run_traffic_simulation(payload),
                "node": lambda: workflow.add_node(payload.get("coordinate", []), payload.get("name")),
                "node-delete": lambda: workflow.delete_node(payload.get("node_id")),
                "scenario": lambda: workflow.generate_scenario(payload.get("direction", "both")),
                "scenario-od": lambda: workflow.generate_scenario_od(
                    payload.get("start_node_id"), payload.get("end_node_id"),
                    payload.get("direction", "both"),
                ),
                "route-delete": lambda: workflow.delete_route(payload.get("route_id")),
                "operational": lambda: workflow.generate_operational(data.hard_constraints),
                "rules": lambda: workflow.set_rules(payload),
                "aircraft-profile": lambda: workflow.select_aircraft_profile(payload.get("aircraft_id")),
                "required-cns": lambda: workflow.set_required_cns(payload),
                "candidate-sites-from-existing": workflow.candidate_sites_from_existing,
                "gap-analysis": workflow.analyze_cns_gaps,
                "devices": lambda: workflow.set_devices(payload.get("devices")),
                "coverage": lambda: workflow.plan_coverage(),
                "save": self._save_workflow,
            }
            if action not in actions: return Response({"error": "工作流操作不存在"}, status=404)
            result = actions[action]()
            legacy_action_capabilities = {
                "operational": "RoutePlannerV1",
                "coverage": "CoveragePlannerV1",
                "gap-analysis": "CNSGapAnalyzerV1",
            }
            capability_id = legacy_action_capabilities.get(action)
            if capability_id:
                result = with_capability_metadata(result, capability_id, alias=True)
            return Response(result)
        if path not in ("/api/sources", "/api/data-sources", "/api/data-sources/validate"):
            return Response({"error": "未找到"}, status=404)
        clean = {
            key: payload.get(key, "")
            for key in (
                "basemap",
                "population",
                "terrain",
                "terrain_dtm",
                "buildings",
                "building_grid",
                # 陆域掩膜：Radar Surveillance Layout V1.1 的 land|sea 判定来源，
                # 必须与其它空间来源一样可以被显式配置、校验与持久化。
                "land_mask",
                "reference_landing_sites",
                "reference_routes",
                "towers",
            )
        }
        if path == "/api/data-sources/validate":
            from ..gis.map_data import MapData
            return Response(context.qgis.call(lambda: MapData.validate_candidate(clean, default_config=context.default_config, token=context.token)))
        return Response(context.qgis.call(lambda: context.replace_sources(clean)))

    def _save_workflow(self):
        self.context.workflow.save()
        return self.context.workflow.snapshot()

    # ---- Phase4-B6X：task 路由辅助 -------------------------------------------------

    def _task_service(self):
        service = getattr(self.context, "heavy_tasks", None)
        if service is None:
            raise RuntimeError("服务端未启用重任务运行时")
        service.maybe_drive()
        return service

    def _task_submit(self, task_type, payload, *, task_id=None):
        from ..tasks.handlers import plan_submission  # noqa: F401  (契约可读性)
        from ..tasks.service import CONFLICT_REJECT, CONFLICT_RETURN_EXISTING
        from ..tasks.task_store import TaskConflictError

        payload = payload if isinstance(payload, dict) else {}
        if not task_type:
            return Response({"error": "请求未指明任务类型"}, status=400)
        policy = (
            CONFLICT_REJECT if str(payload.get("conflict_policy") or "") == "conflict"
            else CONFLICT_RETURN_EXISTING
        )
        service = self._task_service()
        try:
            record, created = service.submit(task_type, payload, conflict_policy=policy)
        except TaskConflictError as exc:
            return Response({
                "error": "该范围已有进行中的同类任务，请等待它完成或先取消它",
                "detail": {"code": "task_conflict", "message": str(exc)},
            }, status=409)
        except ValueError as exc:
            return Response({
                "error": "无法提交该任务，请检查所需输入是否齐备",
                "detail": {"code": getattr(exc, "code", "task_submit_failed"),
                           "message": str(exc)},
            }, status=400)
        view = service.find(record["task_id"])
        message = (
            f"{_task_type_text(task_type)}已提交，正在后台计算"
            if created else "该范围已有进行中的任务，已返回现有任务"
        )
        return Response({
            "task": view,
            "task_id": record["task_id"],
            "created": created,
            "message": message,
        }, status=202 if created else 200)

    def _task_cancel(self, task_id):
        from ..tasks.task_store import TaskNotFoundError

        identifier = str(task_id or "").strip()
        if not identifier:
            return Response({"error": "请求未指明任务"}, status=400)
        service = self._task_service()
        try:
            view = service.cancel(identifier)
        except TaskNotFoundError:
            return Response({"error": "任务不存在"}, status=404)
        outcome = view.pop("cancel_outcome", "cancel_requested")
        message = {
            "cancelled_queued": "任务已取消（尚未开始计算）",
            "cancel_requested": "已请求取消，正在停止计算",
            "already_finished": f"任务已经结束（{view.get('status_text')}），无需取消",
        }.get(outcome, "已请求取消")
        return Response({"task": view, "message": message, "cancel_outcome": outcome})

    def _static(self, path):
        relative = "index.html" if path == "/" else path.lstrip("/")
        target = (self.context.static / relative).resolve()
        static_root = self.context.static.resolve()
        if static_root not in target.parents or not target.is_file():
            return None
        mime = "text/html; charset=utf-8" if target.suffix == ".html" else "text/css; charset=utf-8" if target.suffix == ".css" else "text/javascript; charset=utf-8" if target.suffix == ".js" else "application/octet-stream"
        return Response(target.read_bytes(), mime)
