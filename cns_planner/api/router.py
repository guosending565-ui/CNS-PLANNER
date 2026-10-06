"""Stable /api route dispatch independent from BaseHTTPRequestHandler."""

from dataclasses import dataclass
from pathlib import Path

from ..gis.online_health import check_online_services
from ..safety.event_evaluator import evaluate_safety_events
from ..safety.fault_tree import evaluate_fault_tree
from ..safety.coupling import evaluate_coupled_events
from ..safety.service_state import evaluate_service_state
from .file_browser import browse
from ..application.map_figure_service import MapFigureError
from ..tasks.task_specs import task_type_for_endpoint
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
        if path == "/api/project/active":
            # A3：同项目再次打开 / 启动恢复的**轻量**只读投影。
            #
            # 它只回答一组装配事实：当前 active project 是谁、workflow revision 是多少、
            # 写请求需要的会话 token 是什么、工作区 / 标准网格是否已生成。它**刻意**不走
            # ``data.metadata()`` / ``workflow.snapshot()`` / 任何 ``result_snapshot()``，
            # 也不打开任何 artifact：那条路径每次都会重新投影完整 workflow（实测
            # 11.5-15 s、约 3.8 MB），正是"同一项目再次打开仍然很慢"的根因。
            #
            # 取值只有三处，全部是 O(1) 的既有轻量事实：
            #   * ``data.token`` —— 会话令牌（写请求的 X-CNS-Token）；
            #   * ``data.project_metadata_provider`` —— 与 ``/api/state`` 的
            #     ``project_storage`` **同一个** provider（存储身份）；
            #   * ``workflow.state`` 里已经存在的 project / workspace / grid 摘要与 revision。
            #
            # 不返回 ``grid_risk_v2``、候选 / 雷达明细、任何 artifact body，也不返回完整
            # workflow snapshot —— 那些仍由各自的专用只读接口按需提供。
            return Response(self._project_active())
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
        # ---- 专题成果图（Presentation / Cartographic Export，只读 catalog / preview） -----
        # 只暴露模板目录、预览与受控产物读取；**没有**通用 QGIS 执行接口，也不接受任何
        # 客户端提供的脚本、表达式或输出路径。
        if path == "/api/map-figures/catalog":
            try:
                return Response(self._map_figures().catalog())
            except MapFigureError as exc:
                return Response(self._map_figure_error(exc), status=exc.status)
        if path == "/api/map-figures/preview":
            try:
                result = self._map_figures().render_preview(
                    template_id=self._first(query, "template", "") or "route_overview_v1",
                    route_id=self._first(query, "route_id", "") or None,
                    width_px=self._first(query, "width", "") or None,
                    # 只接受**已登记**的模板参数（当前是监视布设图的 variant）。
                    # 非法值由模板层明确拒绝，这里不做任何回退或猜测。
                    parameter_overrides=self._map_figure_preview_parameters(query),
                )
            except MapFigureError as exc:
                return Response(self._map_figure_error(exc), status=exc.status)
            return Response(result["image"], "image/png")
        if path == "/api/map-figures/artifact":
            try:
                artifact = self._map_figures().artifact(
                    self._first(query, "figure_id", ""),
                    self._first(query, "kind", "png") or "png",
                )
            except MapFigureError as exc:
                return Response(self._map_figure_error(exc), status=exc.status)
            return Response(artifact["image"], artifact["content_type"])
        if path == "/api/map-figures/state":
            # 只读投影：图件记录来自**外部**受控索引 artifacts/map_figures/index.json，
            # 绝不写回 workflow state，也不推进业务 revision。前端用它在首次进入、
            # 页面刷新与导出成功后重新读取真实图件状态（不再依赖 flow.map_figures）。
            try:
                return Response(self._map_figures().figures_snapshot())
            except MapFigureError as exc:
                return Response(self._map_figure_error(exc), status=exc.status)
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
        if path == "/api/cns-existing-baseline": return Response(workflow.cns_existing_baseline_snapshot())
        if path == "/api/candidate-sites": return Response(workflow.candidate_sites_snapshot())
        # ---- Round 2.4：人工工程证据 / 规划假设（独立容器；只读投影 + 字段清单） ----
        if path == "/api/planning-evidence": return Response(workflow.planning_evidence_snapshot())
        if path == "/api/planning-evidence/fields": return Response(workflow.planning_evidence_fields())
        # ---- Towers Operational Integration V2（铁塔派生事实；只读投影） --------------
        if path == "/api/tower-obstacle-profiles": return Response(workflow.tower_obstacle_profiles_snapshot())
        if path == "/api/tower-colocation-candidates": return Response(workflow.tower_colocation_candidates_snapshot())
        if path == "/api/tower-integration-policies": return Response(workflow.tower_integration_policies_snapshot())
        if path == "/api/cns-closed-loop": return Response(workflow.closed_loop_snapshot())
        if path == "/api/cns-service-corridor": return Response(workflow.cns_corridor_snapshot())
        if path == "/api/cns-planning-objectives": return Response(workflow.cns_planning_objectives_snapshot())
        if path == "/api/cns-corridor-gap": return Response(workflow.cns_corridor_gap_snapshot())
        if path == "/api/cns-corridor-site-plan": return Response(workflow.cns_corridor_site_plan_snapshot())
        # ---- Round 2.5：P17 连续服务可接受性（只读消费者） --------------------
        if path == "/api/cns-continuous-service": return Response(workflow.cns_continuous_service_snapshot())
        if path == "/api/cns-continuous-service/parameters": return Response(workflow.cns_continuous_service_parameters())
        if path == "/api/cns-continuous-service/policy": return Response(workflow.cns_continuous_service_policy())
        if path == "/api/cns-continuous-service/scenario": return Response(workflow.cns_operation_scenario())
        if path == "/api/cns-continuous-service/step6-gate": return Response(workflow.cns_continuous_service_step6_gate())
        if path == "/api/fc30-profile": return Response(workflow.fc30_profile_facts())
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
        # Round32-C：逐 cell coarse feasibility mask 明细的**按需**读取入口。
        # 通用刷新路径只带 mask 摘要（cells 已外置）；只有地图真的要画该图层、
        # 或审计显式要求时才传输 cells。可选 lane_key 只取所选车道。
        if path == "/api/layered-route-candidates/masks":
            return Response(workflow.layered_route_masks(self._first(query, "lane_key", "") or None))
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
        # PCF 前置配置：provisional 穿越策略（默认 false，需显式确认 + source/evidence）
        # 与受限空域 / 保护要地的 confirmed_none 显式声明。只读。
        if path == "/api/planning-constraint-field/configuration":
            return Response(workflow.planning_constraint_field_configuration())
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
        # ---- Step5 共用 surface classification（Round 2，中立 / 独立于 Radar） ----------
        # Communication / RID 的 surface 事实不要求先生成 Radar layout，因此由这三个
        # 中立端点正式服务；逐格明细走 facts 容器本体（``by_grid_id``）。
        if path == "/api/surface-classification-policy":
            return Response(workflow.surface_classification_policy_snapshot())
        if path == "/api/surface-class-facts":
            return Response(workflow.surface_class_facts_snapshot())
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

    # ---- 专题成果图（Presentation / Cartographic Export） -------------------------

    def _map_figures(self):
        """当前运行时装配的专题图服务；未启用时给出明确中文错误。"""

        service = getattr(self.context, "map_figures", None)
        if service is None:
            raise MapFigureError("服务端未启用专题成果图模块", code="map_figure_disabled")
        return service

    #: 预览查询串里允许携带的模板参数白名单（只有**已登记**的参数能进来）。
    MAP_FIGURE_PREVIEW_PARAMETERS = ("surveillance_service",)

    @classmethod
    def _map_figure_preview_parameters(cls, query):
        """从查询串取模板参数覆盖（白名单之外的一律忽略，绝不透传任意键）。"""

        overrides = {}
        for key in cls.MAP_FIGURE_PREVIEW_PARAMETERS:
            value = cls._first(query, key, "")
            if value:
                overrides[key] = value
        return overrides or None

    @staticmethod
    def _map_figure_error(exc):
        """把业务错误翻译成统一的 JSON 结构（中文提示 + 技术 code）。"""

        return {
            "error": str(exc),
            "code": getattr(exc, "code", "map_figure_error"),
            "detail": getattr(exc, "detail", None),
        }

    def _project_active(self):
        """``GET /api/project/active`` 的轻量投影（见 GET 分派处的完整说明）。

        只做"直接取值"：读当前 session state 与 active project 的存储身份，不做任何
        业务重算、不深拷贝大容器、不打开文件、不调用 ``snapshot()``。因此它的代价与
        项目规模无关（实测毫秒级），可以安全地放在"同一项目再次打开"与启动首屏上。
        """

        import hashlib

        context = self.context
        data = context.data
        workflow = context.workflow
        state = getattr(workflow, "state", None)
        state = state if isinstance(state, dict) else {}
        project = state.get("project")
        project = project if isinstance(project, dict) else {}
        workspace = state.get("workspace")
        workspace = workspace if isinstance(workspace, dict) else None
        grid = state.get("grid")
        grid = grid if isinstance(grid, dict) else None
        cells = grid.get("cells") if grid else None
        try:
            revision = int(state.get("revision") or 0)
        except (TypeError, ValueError):
            revision = 0
        try:
            project_revision = int(project.get("revision") or 0)
        except (TypeError, ValueError):
            project_revision = 0
        # 与 ``/api/state`` 的 ``project_storage`` 完全同一个 provider：前端用它做项目
        # 身份判定（file / directory / automatic），因此这里绝不能另算一套。
        provider = getattr(data, "project_metadata_provider", None)
        storage = provider() if callable(provider) else {}
        storage = storage if isinstance(storage, dict) else {}
        # 轻量身份指纹：只由"已经存在的标量事实"派生，供前端校验"我拿到的 active 与
        # 我正在渲染的项目是不是同一个版本"。它不替代任何业务 fingerprint。
        fingerprint = hashlib.sha256(
            "\x1f".join(str(item) for item in (
                project.get("project_id"), revision, project_revision,
                (workspace or {}).get("revision"), (grid or {}).get("level"),
                (grid or {}).get("status"), storage.get("file"),
            )).encode("utf-8")
        ).hexdigest()
        return {
            "token": getattr(data, "token", None) or getattr(context, "token", ""),
            "revision": revision,
            "project_storage": {
                "automatic": bool(storage.get("automatic")),
                "directory": storage.get("directory") or "",
                "file": storage.get("file") or "",
            },
            "project": {
                "project_id": project.get("project_id"),
                "name": project.get("name"),
                "revision": project_revision,
            },
            "workspace": {
                "present": bool(workspace),
                "revision": (workspace or {}).get("revision"),
                "area_km2": (workspace or {}).get("area_km2"),
            },
            "grid": {
                "generated": bool(cells),
                "level": (grid or {}).get("level"),
                "count": len(cells) if isinstance(cells, (list, dict)) else 0,
            },
            "workflow_revision": revision,
            "workflow_fingerprint": fingerprint,
            "semantics": (
                "lightweight_active_project_projection_excluding_workflow_snapshot_"
                "artifact_bodies_grid_risk_v2_and_candidate_or_radar_detail"
            ),
        }

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
        original_path = path
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
        # ---- 专题成果图正式导出（POST：需要 token + revision 契约） ------------------
        # payload 只接受 template_id / route_id / format / dpi / 受控模板参数覆盖；
        # 输出路径永远由服务端在 active project 的受控目录内决定，客户端不能指定路径。
        if path == "/api/map-figures/export":
            try:
                return Response(self._map_figures().export(
                    template_id=payload.get("template_id") or payload.get("template")
                    or "route_overview_v1",
                    route_id=payload.get("route_id") or None,
                    format_name=payload.get("format") or "png",
                    dpi=payload.get("dpi"),
                    parameter_overrides=payload.get("parameters") or None,
                ))
            except MapFigureError as exc:
                return Response(self._map_figure_error(exc), status=exc.status)
        # 任何其它 /api/map-figures/* POST 都明确不存在（没有通用 QGIS 执行入口）。
        if path.startswith("/api/map-figures/"):
            return Response(
                {"error": "专题成果图没有该操作；本模块只提供 catalog / preview / export / artifact",
                 "code": "map_figure_unknown_operation"},
                status=404,
            )
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
        # F-03：新建**空白**项目（不是 Save As）。目录冲突 / 非空目录一律 fail-closed，
        # 由 ``ProjectDirectoryService.create_blank`` 单一裁决，不在这里做任何文件判断。
        if path == "/api/project/create": return Response(context.qgis.call(lambda: context.create_project(payload.get("project_dir"), payload.get("project_name"))))
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
            "/api/cns-existing-baseline": lambda: workflow.set_cns_existing_baseline(payload),
            "/api/candidate-sites/import": lambda: workflow.import_candidate_sites(payload),
            "/api/candidate-sites/from-existing": workflow.candidate_sites_from_existing,
            # ---- Round 2.4：人工工程证据 / 规划假设的正式写入口 ----------------------
            # 只写 ``project_state.planning_evidence``（独立容器），**绝不**写
            # device catalog / aircraft profile 源文件；每条记录必须显式声明
            # source_type，且 engineering_assumption 必须带 statement / source /
            # confirmed_by_user / report_disclosure。缺证据时普通用户由此完成补充。
            "/api/planning-evidence": lambda: workflow.add_planning_evidence(payload),
            "/api/planning-evidence/withdraw": lambda: workflow.withdraw_planning_evidence(payload),
            # ---- Round D：navigation_site_suitability（既有站址上的显式声明） ----------
            # 最薄的写入口：只把规范化后的 suitability 写到**既有**站址条目
            # （existing_cns_facilities / candidate_sites / towers）的 metadata 上，
            # 继续走既有 normalization / invalidation / session.save 链路；
            # 前端**不能**指定任意 session JSON 路径，也没有第二个站址容器。
            "/api/navigation-site-suitability": lambda: workflow.set_navigation_site_suitability(payload),
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
            "/api/cns-closed-loop/evaluate": lambda: workflow.evaluate_closed_loop(payload),
            "/api/cns-closed-loop/apply": lambda: workflow.apply_closed_loop(payload),
            "/api/cns-service-corridor/evaluate": lambda: workflow.evaluate_cns_corridor(payload),
            "/api/cns-planning-objectives": lambda: workflow.set_cns_planning_objectives(payload),
            "/api/cns-corridor-gap/evaluate": lambda: workflow.evaluate_cns_corridor_gap(payload),
            "/api/cns-corridor-site-plan/evaluate": lambda: workflow.evaluate_cns_corridor_site_plan(payload),
            # ---- Round 2.5：P17 连续服务可接受性 ------------------------------
            "/api/cns-continuous-service/evaluate": lambda: workflow.evaluate_cns_continuous_service(payload),
            "/api/cns-continuous-service/policy": lambda: workflow.set_cns_continuous_service_policy(payload),
            "/api/cns-continuous-service/scenario": lambda: workflow.set_cns_operation_scenario(payload),
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
            "/api/planning-constraint-field/configuration": lambda: workflow.set_planning_constraint_field_configuration(payload),
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
            # ---- Step5 共用 surface classification（Round 2） -------------------------
            # 中立策略写入不需要数据源读取；事实生成读真实陆域掩膜，因此走 QGIS 线程入口。
            "/api/surface-classification-policy": lambda: (
                context.set_surface_classification_policy(payload)
                if hasattr(context, "set_surface_classification_policy")
                else workflow.set_surface_classification_policy(payload)
            ),
            "/api/surface-class-facts/evaluate": lambda: (
                context.update_surface_class_facts(payload)
                if hasattr(context, "update_surface_class_facts")
                else workflow.update_surface_class_facts(payload)
            ),
            "/api/cns-service-capability/evaluate": lambda: workflow.evaluate_cns_service_capability(),
            "/api/operational-timing": lambda: workflow.set_operational_timing(payload),
            "/api/service-timeline/evaluate": lambda: workflow.evaluate_service_timeline(payload),
            "/api/protection-envelope/evaluate": lambda: workflow.evaluate_protection_envelope(payload),
            "/api/encounter-3d/evaluate": lambda: workflow.evaluate_encounter_3d(payload),
        }
        if path in resource_actions:
            result = resource_actions[path]()
            if research_request:
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
                "node": lambda: workflow.add_node(
                    payload.get("coordinate", []), payload.get("name"),
                    payload.get("coordinate_source"),
                ),
                "node-delete": lambda: workflow.delete_node(payload.get("node_id")),
                "scenario": lambda: workflow.generate_scenario(payload.get("direction", "both")),
                "scenario-od": lambda: workflow.generate_scenario_od(
                    payload.get("start_node_id"), payload.get("end_node_id"),
                    payload.get("direction", "both"),
                ),
                "route-delete": lambda: workflow.delete_route(payload.get("route_id")),
                "rules": lambda: workflow.set_rules(payload),
                "aircraft-profile": lambda: workflow.select_aircraft_profile(payload.get("aircraft_id")),
                "required-cns": lambda: workflow.set_required_cns(payload),
                "candidate-sites-from-existing": workflow.candidate_sites_from_existing,
                "devices": lambda: workflow.set_devices(payload.get("devices")),
                "save": self._save_workflow,
            }
            if action not in actions: return Response({"error": "工作流操作不存在"}, status=404)
            result = actions[action]()
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

    #: 需要把主进程已解析的运行期真实来源冻结进快照的后台任务类型（见 _freeze_runtime_sources）。
    RUNTIME_SOURCE_KEYS_BY_TASK = {
        "layered_route_candidate_evaluate": ("terrain_dtm",),
        "layered_route_validation_evaluate": ("terrain_dtm", "buildings"),
    }
    #: 兼容 Round32-C 对“哪些 task 需要 runtime sources”的只读合同。
    RUNTIME_SOURCE_TASK_TYPES = tuple(RUNTIME_SOURCE_KEYS_BY_TASK)

    def _task_service(self, *, drive=False):
        """任务运行时访问器。

        ``drive=True`` 只用于**写路径**（提交 / 取消）：它们需要立刻推进一次编排。
        只读查询（``GET /api/tasks*``）**不**驱动 —— ``maybe_drive()`` 会在
        ``HeavyTaskService._lock`` 内跑一次完整编排，而 publish 阶段持该锁执行 canonical
        写入（真实项目 ``session.save()`` 是数百 MB 级原子替换，实测十秒级），于是
        "看进度"的请求反而会整段排队甚至读超时。发布本身由 ``HeavyTaskService`` 自己的
        编排线程按 ``poll_interval`` 完成，不需要只读请求去推动。
        """

        service = getattr(self.context, "heavy_tasks", None)
        if service is None:
            raise RuntimeError("服务端未启用重任务运行时")
        if drive:
            service.maybe_drive()
        return service

    def _task_submit(self, task_type, payload, *, task_id=None):
        from ..tasks.handlers import plan_submission  # noqa: F401  (契约可读性)
        from ..tasks.service import CONFLICT_REJECT, CONFLICT_RETURN_EXISTING
        from ..tasks.task_store import TaskConflictError

        payload = payload if isinstance(payload, dict) else {}
        payload = self._freeze_runtime_sources(task_type, payload)
        if not task_type:
            return Response({"error": "请求未指明任务类型"}, status=400)
        policy = (
            CONFLICT_REJECT if str(payload.get("conflict_policy") or "") == "conflict"
            else CONFLICT_RETURN_EXISTING
        )
        service = self._task_service(drive=True)
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

    def _freeze_runtime_sources(self, task_type, payload):
        """把**主进程已解析**的运行期真实来源冻结进后台任务输入（Round32-C）。

        为什么必须在提交侧做：候选规划在 worker 里复算 coarse 可行性掩膜，需要 FABDEM
        ``terrain_dtm``；但该路径只存在于主进程的 ``MapData.paths`` —— canonical state 里
        没有 ``data_source_paths`` 容器的写入者（实测为 None）。worker 不能自行解析项目文件，
        因此由这里把它并入 payload，再由任务的 ``input_snapshot`` 冻结进不可变快照。
        这样 worker 用的就是与同步路径**同一个**装配函数，不存在第二套来源解析。
        """

        source_keys = self.RUNTIME_SOURCE_KEYS_BY_TASK.get(task_type)
        if not source_keys:
            return payload
        paths = getattr(getattr(getattr(self, "context", None), "data", None), "paths", None) or {}
        #: 只冻结该任务**真正消费**的运行期来源（最小面）：coarse 可行性掩膜的 FABDEM DTM。
        #: 建筑/人口等事实来自 canonical state，不在这里冻结，避免无关路径变化让任务假 stale。
        resolved = {key: paths.get(key) for key in source_keys if paths.get(key)}
        if not resolved:
            return payload
        merged = dict(payload)
        merged["data_source_paths"] = {**resolved, **dict(payload.get("data_source_paths") or {})}
        return merged

    def _task_cancel(self, task_id):
        from ..tasks.task_store import TaskNotFoundError

        identifier = str(task_id or "").strip()
        if not identifier:
            return Response({"error": "请求未指明任务"}, status=400)
        service = self._task_service(drive=True)
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
