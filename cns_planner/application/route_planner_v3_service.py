"""V3-A/V3-B planning experiments: read-only w.r.t. every existing result.

This service is deliberately outside the operational route path:

* it never changes ``algorithm_selection``;
* it never writes ``operational_routes`` or ``result_statuses["routes"]``;
* it never writes ``spatial_3d``/``route_altitude_profiles`` (V3 altitude lives in
  its own contract);
* it stores everything in the additive ``route_planner_v3_experiments`` container.

V3-A ships **no** real-data adapter: the canonical ``V3CellEnvironment`` is built
from an explicit synthetic specification so the 3D + heading search and the
hard-constraint kernel can be exercised deterministically.

V3-B adds corridor-local fine refinement.  It runs only on a *selected and
current* V3-A ``strategic_candidate`` and its recorded refinement corridor.  The
fine environment comes either from an **injected** GIS adapter
(``gis.fine_environment_adapter``, wired in ``ApplicationContext`` because it
needs QGIS/GDAL) or from the explicit synthetic builder.  When the real sources
are not ready the service records an explicit ``not_ready`` refinement with the
blocking reasons -- it never fabricates an environment.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json

from ..algorithms.grid.service import WorkspaceGridService
from ..gis.fine_environment_adapter import real_data_source_readiness
from ..route_planner_v3 import V3StrategicPlanner
from ..route_planner_v3.continuous_contracts import (
    VALIDATION_FINGERPRINT_COMPONENTS, VALIDATOR_VERSIONS, V3C_DISCLAIMER,
    V3C_MODEL_SCOPE, V3C_RESULT_STATUSES, contract_fingerprint,
    default_v3_validation_policy, evaluate_validation_applicability,
    normalize_v3_validation_policy, validation_fingerprint_components,
)
from ..route_planner_v3.continuous_synthetic import (
    build_synthetic_continuous_evidence, default_synthetic_continuous_spec,
    normalize_synthetic_continuous_spec, synthetic_validation_policy,
)
from ..route_planner_v3.continuous_validation import V3ContinuousValidator
from ..route_planner_v3.contracts import (
    V3_RESULT_STATUSES, default_v3_cost_model, normalize_aircraft_motion_limits,
    normalize_v3_experiments, normalize_v3_planning_policy,
    normalize_v3_planning_problem,
)
from ..route_planner_v3.fine_contracts import (
    FINE_MODEL_SCOPE, REFINEMENT_FINGERPRINT_COMPONENTS, V3B_DISCLAIMER,
    V3B_RESULT_STATUSES, contract_fingerprint, default_v3_fine_refinement_policy,
    evaluate_refinement_applicability, normalize_v3_fine_refinement_policy,
    normalize_v3_refinement_problem, normalize_v3_refinement_result,
    refinement_fingerprint_components,
)
from ..route_planner_v3.fine_search import V3RefinementPlanner
from ..route_planner_v3.fine_synthetic import build_synthetic_fine_environment
from ..route_planner_v3.motion import kinematic_readiness
from ..route_planner_v3.readiness import readiness_overall
from ..route_planner_v3.synthetic import (
    BUILDING_PROFILES, TERRAIN_PROFILES, build_synthetic_environment,
    normalize_synthetic_spec,
)

V3_COLLECTION_ID = "route-planner-v3-experiments"
V3_ENVIRONMENT_SOURCES = ("canonical_synthetic",)
#: V3-B environment sources: the explicit synthetic builder, or the injected real
#: adapter.  Neither path ever fabricates a fine environment.
V3B_ENVIRONMENT_SOURCES = ("canonical_synthetic", "configured_real_sources")
#: V3-C domain evidence sources.  The synthetic builder produces an explicit metric
#: exercise; the real path reads the **native** raster and the real footprints through
#: the GIS adapter.  Neither fabricates evidence.
V3C_EVIDENCE_SOURCES = ("canonical_synthetic", "configured_real_sources")
MAX_V3_EXPERIMENTS = 12
MAX_V3_REFINEMENTS_PER_EXPERIMENT = 6
#: V3-C validations kept per refinement (newest first).
MAX_V3C_VALIDATIONS_PER_REFINEMENT = 4
V3_EXPERIMENT_NOTE = (
    "V3-A 战略规划实验 ≠ operational route：只写入 route_planner_v3_experiments，"
    "不切换 algorithm_selection，也不覆盖 operational_routes、spatial_3d 或 V1/V2 结果。"
)
V3B_EXPERIMENT_NOTE = (
    "V3-B corridor-local 精化候选 ≠ validated route：只在选定且 current 的 V3-A "
    "strategic_candidate 的 corridor 内做米制细网格工程精化；未做 V3-C exact polygon/"
    "terrain/continuous clearance 验证，也不写 operational_routes、algorithm_selection 或 spatial_3d。"
)
V3_REAL_DATA_ADAPTER_STATUS = "not_implemented_v3a"
V3_REAL_DATA_ADAPTER_REASON = (
    "V3-A 不提供真实 terrain/building adapter，也不做局部细化；"
    "真实数据必须先在 V3-B/C 阶段转换为 canonical V3CellEnvironment 后才允许进入搜索。"
)
V3B_REAL_DATA_ADAPTER_STATUS = "implemented_v3b_gis_adapter"
V3B_REAL_DATA_ADAPTER_REASON = (
    "V3-B 的 FineEnvironmentAdapter 位于 GIS 边界（FABDEM 窗口只读 + GPKG RTree 建筑查询 + "
    "confirmed AirspacePolicy）；数据未配置或未确认时明确返回 blocked，不构造假环境。"
)
V3_ARCHITECTURE_SUMMARY = (
    "V3 原生 3D 战略规划：state = grid_id + altitude_index + heading_bin，"
    "hard constraints（allowed/restricted airspace、terrain clearance、building clearance、"
    "altitude bounds、turn/climb/descent capability）进入 edge 生成与验证；"
    "soft cost 输出 distance/population_risk/traffic_risk/building_exposure/energy 向量；"
    "L8 战略搜索 → corridor →（V3-B）corridor-local 米制细网格精化 →（V3-C）exact polygon/"
    "terrain/continuous clearance 验证 →（V3-D）validated route → operational adapter → CNS Assessment；"
    "Route–CNS 联合优化是更晚期的未来项。"
)
V3B_ARCHITECTURE_SUMMARY = (
    "V3-B：只在 V3-A corridor support cells 的米制窗口内构造局部 fine grid；"
    "horizontal resolution 来自显式配置或 DTM 有效分辨率（禁止写死 30 m）；"
    "terrain hard floor = 相交 FABDEM 有效像元最大 EGM2008 高程 + explicit terrain_clearance；"
    "building = footprint 按 explicit horizontal clearance 的保守包络，required floor = "
    "ground + height + vertical_clearance；airspace 只消费 confirmed policy；"
    "multi-cell stride primitive 记录 traversed_cell_ids 并按路径进度插值高度逐格检查。"
)
V3C_EXPERIMENT_NOTE = (
    "V3-C continuous validated route ≠ operational route：连续几何实现 + confirmed 源几何/"
    "原生栅格验证；即使 status=validated_route，也强制 operational_route=false、cns_assessed=false，"
    "不写 operational_routes、algorithm_selection 或 spatial_3d，也不产生任何失效传播。"
)
V3C_ARCHITECTURE_SUMMARY = (
    "V3-C：把 V3-B refined candidate 的米制轨迹实现为 C1（position+heading 连续）几何——"
    "内部转弯用 explicit aircraft_min_turn_radius_m 的解析圆弧 fillet（R 绝不减小）；"
    "圆弧保留 analytic geometry 并按 explicit curve_chord_error_m 线性化（禁止隐式精度假设）；"
    "随后用逐 domain 的源几何验证：confirmed allowed/blocked polygon 的 route uncertainty envelope "
    "覆盖率与不相交、native FABDEM 像元 terrain clearance、真实 footprint 的 roof+vertical_clearance、"
    "高度带与 kinematics（analytic R、tangent heading、climb/descent gradient）。"
    "vector predicate 对 linearized representation（含显式 chord-error envelope）精确；"
    "terrain 是 source-native raster evidence，不声称真实世界地形数学连续精确。"
)
V3C_REAL_DATA_ADAPTER_STATUS = "implemented_v3c_gis_adapter"
V3C_REAL_DATA_ADAPTER_REASON = (
    "V3-C 的真实数据路径位于 GIS 边界：confirmed AirspacePolicy polygon 转到米制帧、"
    "FABDEM native 像元窗口只读、以及 provider RTree 建筑查询（复用 BuildingClearance 的 roof 语义）；"
    "数据未配置/未确认时明确 blocked/unresolved，不构造假证据。"
)


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(value):
    return sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


class RoutePlannerV3ExperimentService:
    def __init__(self, session, planner, invalidation, snapshot, grid_service=None,
                 refinement_planner=None, source_readiness=None, continuous_validator=None):
        self.session = session
        self.planner = planner or V3StrategicPlanner()
        self.refinement_planner = refinement_planner or V3RefinementPlanner()
        self.continuous_validator = continuous_validator or V3ContinuousValidator()
        self.invalidation = invalidation
        self.snapshot = snapshot
        self.grid_service = grid_service or WorkspaceGridService()
        #: Optional injected provider for the *current* real-data readiness of the
        #: V3-B fine sources.  It must not read data; it reports configured paths
        #: and confirmed airspace evidence (see
        #: ``gis.fine_environment_adapter.real_data_source_readiness``).
        self.source_readiness = source_readiness

    # ------------------------------------------------------------------ queries

    def result_snapshot(self):
        collection = deepcopy(self.session.state.get("route_planner_v3_experiments") or {})
        records = list(collection.get("records") or [])
        active_id = collection.get("active_experiment_id")
        active = next((item for item in records if item.get("experiment_id") == active_id), None)
        if active is None and records:
            active = records[0]
        return {
            "status": "passed" if records else "not_calculated",
            "collection_id": V3_COLLECTION_ID,
            "schema_version": 1,
            "count": len(records),
            "active_experiment_id": (active or {}).get("experiment_id"),
            "active_experiment": active,
            "records": records,
            "architecture": V3_ARCHITECTURE_SUMMARY,
            "v3b_architecture": V3B_ARCHITECTURE_SUMMARY,
            "v3c_architecture": V3C_ARCHITECTURE_SUMMARY,
            "note": V3_EXPERIMENT_NOTE,
            "v3b_note": V3B_EXPERIMENT_NOTE,
            "v3c_note": V3C_EXPERIMENT_NOTE,
            "operational_routes_untouched": True,
            "algorithm_selection_untouched": True,
            "spatial_3d_untouched": True,
            "disclaimer": "V3-A 结果只允许 status=strategic_candidate/failed/missing_data/pending_confirmation/not_ready/search_incomplete。",
            "allowed_result_statuses": list(V3_RESULT_STATUSES),
            "allowed_refinement_statuses": list(V3B_RESULT_STATUSES),
            "allowed_validation_statuses": list(V3C_RESULT_STATUSES),
            "validation_boundaries": {
                "operational_route_always_false": True,
                "cns_assessed_always_false": True,
                "never_repairs_or_replans": True,
            },
        }

    def policy_snapshot(self):
        return deepcopy(self.session.state.get("v3_planning_policy") or normalize_v3_planning_policy(None))

    def fine_policy_snapshot(self):
        return deepcopy(
            self.session.state.get("v3_fine_refinement_policy")
            or default_v3_fine_refinement_policy()
        )

    def refinement_snapshot(self):
        """Current-applicability of every stored refinement against current evidence."""

        records = (self.session.state.get("route_planner_v3_experiments") or {}).get("records") or []
        items = []
        for record in records:
            for refinement in record.get("refinements") or []:
                recorded = refinement.get("evidence_components") or {}
                current = self._current_evidence_components(refinement, recorded)
                verdict = evaluate_refinement_applicability(recorded, current)
                items.append({
                    "refinement_id": refinement.get("refinement_id"),
                    "experiment_id": record.get("experiment_id"),
                    "status": (refinement.get("result") or {}).get("status"),
                    "recorded_applicability": refinement.get("current_applicability"),
                    "current_applicability": verdict["status"],
                    "changed_components": verdict["changed_components"],
                    "reasons": verdict["reasons"],
                    "refinement_fingerprint": (refinement.get("result") or {}).get("refinement_fingerprint"),
                    "evidence_components": recorded,
                })
        return {
            "status": "passed" if items else "not_calculated",
            "count": len(items),
            "items": items,
            "stale_count": sum(1 for item in items if item["current_applicability"] == "stale"),
            "semantics": "stale_when_strategic_corridor_policy_or_source_audit_changes",
            "components": list(REFINEMENT_FINGERPRINT_COMPONENTS),
        }

    def refinement_readiness_snapshot(self):
        """V3-B readiness: selected strategic candidate, fine config, real sources."""

        state = self.session.state
        records = (state.get("route_planner_v3_experiments") or {}).get("records") or []
        with_corridor = [
            record for record in records
            if ((record.get("result") or {}).get("candidate_refinement_corridor") or {}).get("center_grid_ids")
        ]
        selected = with_corridor[0] if with_corridor else None
        fine_policy = self.fine_policy_snapshot()
        readiness = self._real_source_readiness()
        blocking = list(readiness.get("blocking_reasons") or [])
        if selected is None:
            blocking.append("no_current_v3a_strategic_candidate_with_corridor")
        if fine_policy.get("status") != "confirmed":
            blocking.extend(fine_policy.get("reasons") or ["fine_refinement_policy_not_confirmed"])
        return {
            "status": "passed" if not blocking else "blocked",
            "stage": "V3-B",
            "model_scope": FINE_MODEL_SCOPE,
            "architecture": V3B_ARCHITECTURE_SUMMARY,
            "note": V3B_EXPERIMENT_NOTE,
            "stage_scope": {
                "implemented": [
                    "corridor_local_metric_fine_grid", "fine_environment_adapter_gis_boundary",
                    "terrain_intersecting_pixel_max_floor", "building_conservative_envelope",
                    "confirmed_airspace_only", "coarse_soft_field_upsampling_with_provenance",
                    "multi_cell_stride_refinement_search", "traversed_cell_interpolated_checks",
                    "refinement_fingerprint_and_staleness",
                ],
                "not_implemented": [
                    "exact_polygon_membership", "exact_terrain_profile_clearance",
                    "continuous_clearance_along_full_trajectory", "operational_adapter",
                    "cns_joint_optimization", "energy_model",
                ],
            },
            "algorithm": {
                "algorithm_id": self.refinement_planner.algorithm_id,
                "algorithm_version": self.refinement_planner.algorithm_version,
                "model_scope": self.refinement_planner.model_scope,
                "registered_in_algorithm_registry": False,
                "turn_model": self.refinement_planner.turn_model,
            },
            "selected_strategic_candidate": None if selected is None else {
                "experiment_id": selected.get("experiment_id"),
                "result_status": (selected.get("result") or {}).get("status"),
                "route_id": selected.get("route_id"),
                "corridor_id": ((selected.get("result") or {}).get("candidate_refinement_corridor") or {}).get("corridor_id"),
                "support_cell_count": len(
                    ((selected.get("result") or {}).get("candidate_refinement_corridor") or {}).get("support_grid_ids") or []
                ),
                "refinement_count": len(selected.get("refinements") or []),
            },
            "fine_policy": fine_policy,
            "v3_policy_readiness": {
                "status": (
                    "ready" if (state.get("v3_planning_policy") or {}).get("confirmed")
                    and not (state.get("v3_planning_policy") or {}).get("missing_parameters")
                    else "blocked"
                ),
                "missing_parameters": list((state.get("v3_planning_policy") or {}).get("missing_parameters") or []),
            },
            "real_data_readiness": readiness,
            "blocking_reasons": blocking,
            "environment_sources": list(V3B_ENVIRONMENT_SOURCES),
            "resolution_policy": "explicit_configuration_or_dtm_effective_resolution_never_a_30m_constant",
            "allowed_refinement_statuses": list(V3B_RESULT_STATUSES),
        }

    def evaluate_refinement(self, payload=None, adapter=None):
        """Run one corridor-local refinement on a selected current V3-A candidate.

        ``adapter`` is an injected ``FineEnvironmentAdapter`` (wired in
        ``ApplicationContext``).  Without one, the configured real sources are
        reported as blocked and an explicit ``not_ready`` refinement is recorded --
        no environment is fabricated.
        """

        payload = payload if isinstance(payload, dict) else {}
        state = self.session.state
        record = self._selected_candidate(payload)
        if record is None:
            raise ValueError("请先运行 V3-A 战略规划实验并选中一个 strategic_candidate")
        result = record.get("result") or {}
        if result.get("status") != "strategic_candidate":
            raise ValueError(
                f"V3-B 只能在 strategic_candidate 上运行，当前 V3-A 结果 status={result.get('status')}"
            )
        corridor = result.get("candidate_refinement_corridor") or {}
        if not corridor.get("center_grid_ids"):
            raise ValueError("选定的 V3-A 结果没有 refinement corridor，无法精化")
        policy = record.get("policy") or self.policy_snapshot()
        if not policy.get("confirmed") and not payload.get("allow_unconfirmed_policy"):
            raise ValueError("V3 policy 未确认：V3-B 不允许在未确认的规划/安全参数上运行")
        fine = self._fine_config(payload)
        if fine.get("confirmed") is not True and not payload.get("allow_unconfirmed_fine_policy"):
            raise ValueError(
                "V3-B fine refinement policy 未确认：local metric CRS / resolution source 与 "
                "max_stride_cells 必须由项目工程依据显式确认；V3-B 禁止猜默认分辨率"
            )
        source = str(payload.get("environment_source") or "canonical_synthetic")
        if source not in V3B_ENVIRONMENT_SOURCES:
            raise ValueError(
                "V3-B environment_source 只支持 canonical_synthetic 或 configured_real_sources"
            )
        synthetic_spec = normalize_synthetic_spec(
            payload.get("synthetic_fine_spec") or payload.get("synthetic_spec")
        )
        if source == "canonical_synthetic":
            built = self._synthetic_fine_environment(record, corridor, fine, synthetic_spec)
        else:
            built = self._real_fine_environment(adapter, record, corridor, policy, fine, payload)
        refinement = self._refinement_record(record, fine, source, synthetic_spec, built)
        refinements = [
            item for item in (record.get("refinements") or [])
            if item.get("refinement_id") != refinement["refinement_id"]
        ]
        record["refinements"] = [refinement, *refinements][:MAX_V3_REFINEMENTS_PER_EXPERIMENT]
        # V3-B keeps the same hard boundary as V3-A: only its own container is written.
        self.session.save()
        return self.snapshot()

    # ------------------------------------------------------------------ V3-C
    def validation_policy_snapshot(self):
        return deepcopy(
            self.session.state.get("v3_continuous_validation_policy")
            or default_v3_validation_policy()
        )

    def set_validation_policy(self, payload):
        policy = normalize_v3_validation_policy(payload)
        self.session.state["v3_continuous_validation_policy"] = policy
        self.session.save()
        return self.continuous_readiness_snapshot()

    def continuous_readiness_snapshot(self):
        """V3-C readiness: a current refined candidate, explicit policy, source evidence."""

        state = self.session.state
        refinement = self._selected_refinement({})
        policy = self.validation_policy_snapshot()
        planning = state.get("v3_planning_policy") or {}
        blocking = []
        if refinement is None:
            blocking.append("no_current_v3b_refined_candidate")
        if policy.get("status") != "confirmed":
            blocking.extend(policy.get("reasons") or ["v3_continuous_validation_policy_not_confirmed"])
        if planning.get("aircraft_min_turn_radius_m") is None:
            blocking.append("aircraft_min_turn_radius_m_missing_from_v3_planning_policy")
        return {
            "status": "passed" if not blocking else "blocked",
            "stage": "V3-C",
            "model_scope": V3C_MODEL_SCOPE,
            "architecture": V3C_ARCHITECTURE_SUMMARY,
            "note": V3C_EXPERIMENT_NOTE,
            "stage_scope": {
                "implemented": [
                    "c1_tangent_continuous_geometry_realization",
                    "explicit_circular_arc_fillets_radius_never_reduced",
                    "explicit_curve_chord_error_linearization",
                    "realized_vertical_profile_altitude_and_gradient",
                    "exact_airspace_route_envelope_validation",
                    "native_raster_terrain_validation",
                    "real_footprint_building_clearance_validation",
                    "kinematic_analytic_radius_and_tangent_heading_validation",
                    "validation_fingerprint_and_staleness",
                ],
                "not_implemented": [
                    "clothoid_or_continuous_curvature_transitions",
                    "operational_adapter", "cns_assessment", "route_cns_joint_optimization",
                    "energy_model",
                ],
            },
            "algorithm": {
                "algorithm_id": self.continuous_validator.algorithm_id,
                "algorithm_version": self.continuous_validator.algorithm_version,
                "model_scope": self.continuous_validator.model_scope,
                "registered_in_algorithm_registry": False,
                "validator_versions": dict(VALIDATOR_VERSIONS),
            },
            "selected_refinement": None if refinement is None else {
                "experiment_id": refinement.get("experiment_id"),
                "refinement_id": refinement.get("refinement_id"),
                "result_status": (refinement.get("result") or {}).get("status"),
                "current_applicability": refinement.get("current_applicability"),
                "validation_count": len(refinement.get("validations") or []),
            },
            "validation_policy": policy,
            "v3_planning_policy": planning,
            "real_data_readiness": self._real_source_readiness(),
            "blocking_reasons": blocking,
            "evidence_sources": list(V3C_EVIDENCE_SOURCES),
            "allowed_result_statuses": list(V3C_RESULT_STATUSES),
            "fingerprint_components": list(VALIDATION_FINGERPRINT_COMPONENTS),
            "boundaries": {
                "operational_route_always_false": True,
                "cns_assessed_always_false": True,
                "never_repairs_or_replans": True,
                "curve_error_has_no_default": True,
            },
        }

    def continuous_validation_snapshot(self):
        """Current applicability of every stored V3-C validation."""

        records = (self.session.state.get("route_planner_v3_experiments") or {}).get("records") or []
        items = []
        for record in records:
            for refinement in record.get("refinements") or []:
                for validation in refinement.get("validations") or []:
                    recorded = validation.get("evidence_components") or {}
                    current = self._current_validation_components(refinement, validation)
                    verdict = evaluate_validation_applicability(recorded, current)
                    result = validation.get("result") or {}
                    items.append({
                        "validation_id": validation.get("validation_id"),
                        "refinement_id": refinement.get("refinement_id"),
                        "experiment_id": record.get("experiment_id"),
                        "status": result.get("status"),
                        "domain_statuses": result.get("domain_statuses") or {},
                        "recorded_applicability": validation.get("current_applicability"),
                        "current_applicability": verdict["status"],
                        "changed_components": verdict["changed_components"],
                        "reasons": verdict["reasons"],
                        "validation_fingerprint": result.get("validation_fingerprint"),
                        "evidence_components": recorded,
                    })
        return {
            "status": "passed" if items else "not_calculated",
            "count": len(items),
            "items": items,
            "stale_count": sum(1 for item in items if item["current_applicability"] == "stale"),
            "validated_route_count": sum(1 for item in items if item["status"] == "validated_route"),
            "semantics": (
                "stale_when_refinement_policy_curve_tolerance_source_or_validator_changes"
            ),
            "components": list(VALIDATION_FINGERPRINT_COMPONENTS),
        }

    def evaluate_continuous_validation(self, payload=None, *, evidence_adapter=None):
        """Run V3-C on a selected, current V3-B ``refined_candidate``.

        ``evidence_adapter`` is an injected callable that returns the canonical
        ``domain_evidence`` for the realized route (wired in ``ApplicationContext``;
        it needs QGIS/GDAL).  Without one, or with ``evidence_source`` set to
        ``canonical_synthetic``, the explicit synthetic builder is used -- never a
        fabricated real environment.
        """

        payload = payload if isinstance(payload, dict) else {}
        state = self.session.state
        refinement = self._selected_refinement(payload)
        if refinement is None:
            raise ValueError("请先运行 V3-B corridor-local 精化并选中一个 refined_candidate")
        result = refinement.get("result") or {}
        if result.get("status") != "refined_candidate":
            raise ValueError(
                f"V3-C 只能在 V3-B refined_candidate 上运行，当前 refinement status={result.get('status')}"
            )
        policy = self._continuous_policy(payload)
        if policy.get("status") != "confirmed" and not payload.get("allow_unconfirmed_validation_policy"):
            raise ValueError(
                "V3-C validation policy 未确认：curve_chord_error_m 必须由项目工程依据显式确认，"
                "V3-C 禁止安全默认值"
            )
        source = str(payload.get("evidence_source") or "canonical_synthetic")
        if source not in V3C_EVIDENCE_SOURCES:
            raise ValueError(
                "V3-C evidence_source 只支持 canonical_synthetic 或 configured_real_sources"
            )
        planning = self.session.state.get("v3_planning_policy") or {}
        if planning.get("aircraft_min_turn_radius_m") is None:
            raise ValueError(
                "缺少显式 aircraft_min_turn_radius_m：V3-C 圆弧 fillet 的 R 必须有工程依据，"
                "禁止猜默认转弯半径"
            )
        synthetic_spec = normalize_synthetic_continuous_spec(
            payload.get("synthetic_continuous_spec") or payload.get("synthetic_spec")
        )
        realization = self._realize_for_validation(refinement, policy, planning, synthetic_spec)
        if source == "canonical_synthetic":
            evidence = build_synthetic_continuous_evidence(
                synthetic_spec, continuous_route=realization["route"],
            )
        else:
            evidence = self._real_continuous_evidence(
                evidence_adapter, refinement, policy, planning, realization, payload,
            )
        problem = self._continuous_problem(
            refinement, policy, planning, synthetic_spec, evidence,
            source=source, realization=realization,
        )
        validation = self.continuous_validator.validate(problem)
        record = self._validation_record(
            refinement, policy, source, synthetic_spec, problem, validation, realization,
        )
        validations = [
            item for item in (refinement.get("validations") or [])
            if item.get("validation_id") != record["validation_id"]
        ]
        refinement["validations"] = [record, *validations][:MAX_V3C_VALIDATIONS_PER_REFINEMENT]
        # Same hard boundary as V3-A/V3-B: only the V3 container is written.
        self.session.save()
        return self.snapshot()

    def _selected_refinement(self, payload):
        records = (self.session.state.get("route_planner_v3_experiments") or {}).get("records") or []
        experiment_id = str(payload.get("experiment_id") or "")
        refinement_id = str(payload.get("refinement_id") or "")
        for record in records:
            if experiment_id and str(record.get("experiment_id")) != experiment_id:
                continue
            for refinement in record.get("refinements") or []:
                if refinement_id and str(refinement.get("refinement_id")) != refinement_id:
                    continue
                return refinement
        if experiment_id or refinement_id:
            return None
        for record in records:
            for refinement in record.get("refinements") or []:
                if (refinement.get("result") or {}).get("status") == "refined_candidate":
                    return refinement
        return None

    def _continuous_policy(self, payload):
        """Effective V3-C policy for one run.

        A payload policy is a *complete* statement for that run; the stored policy only
        supplies the provenance fields a payload usually omits (``source`` and
        ``confirmed``) when the payload does not state them.  ``confirmed: false`` in the
        payload is therefore honoured and blocks the run -- it is never upgraded from the
        stored policy.
        """

        stored = self.validation_policy_snapshot()
        supplied = payload.get("validation_policy")
        if isinstance(supplied, dict):
            merged = dict(supplied)
            if merged.get("source") in (None, "") and stored.get("source"):
                merged["source"] = stored["source"]
            if "confirmed" not in merged and stored.get("confirmed"):
                merged["confirmed"] = True
            return normalize_v3_validation_policy(merged)
        return normalize_v3_validation_policy(stored)

    def _planning_policy_payload(self, refinement):
        """The planning policy the refinement actually ran with (per-run override wins)."""

        recorded = (refinement or {}).get("policy")
        if isinstance(recorded, dict) and recorded:
            return deepcopy(recorded)
        return self.policy_snapshot()

    def _realize_for_validation(self, refinement, policy, planning, synthetic_spec):
        from ..route_planner_v3.continuous_geometry import realize_continuous_route

        result = refinement.get("result") or {}
        return realize_continuous_route(
            metric_points=result.get("metric_projection") or [],
            altitudes=[
                state.get("altitude_egm2008_m") for state in result.get("state_path") or []
            ],
            min_turn_radius_m=policy.get("aircraft_min_turn_radius_m")
            or planning.get("aircraft_min_turn_radius_m"),
            curve_chord_error_m=policy.get("curve_chord_error_m"),
            route_id=refinement.get("route_id"),
            frame=result.get("frame") or {},
        )

    def _real_continuous_evidence(self, evidence_adapter, refinement, policy, planning,
                                  realization, payload):
        if evidence_adapter is None:
            return {
                "source_type": "configured_real_sources",
                "available": False,
                "reason": "configured_real_sources_unavailable",
                "sample_count": 0,
                "airspace": {"confirmed": False, "allowed": [], "blocked": [], "unconfirmed": []},
                "terrain": {"available": False, "pixels": []},
                "buildings": {"available": False, "buildings": []},
            }
        return evidence_adapter(
            refinement=refinement, policy=policy, planning_policy=planning,
            realization=realization, payload=payload,
        )

    def _continuous_problem(self, refinement, policy, planning, synthetic_spec, evidence,
                            *, source, realization):
        from ..route_planner_v3.continuous_contracts import (
            normalize_v3_continuous_validation_problem,
        )

        result = refinement.get("result") or {}
        route = realization["route"]
        problem = normalize_v3_continuous_validation_problem({
            "problem_id": f"v3c-{refinement.get('refinement_id')}",
            "route_id": refinement.get("route_id"),
            "refinement": {
                "experiment_id": refinement.get("experiment_id"),
                "refinement_id": refinement.get("refinement_id"),
                "status": (refinement.get("result") or {}).get("status"),
                "current_applicability": refinement.get("current_applicability"),
                "refinement_fingerprint": refinement.get("refinement_fingerprint"),
                "metric_projection": result.get("metric_projection") or [],
                "state_path": result.get("state_path") or [],
                "frame": result.get("frame") or {},
                "fine_grid": result.get("fine_grid") or {},
                "source_audit": result.get("source_audit") or {},
                "evidence_components": refinement.get("evidence_components") or {},
            },
            "planning_policy": planning,
            "validation_policy": policy,
            "aircraft_motion_limits": (
                refinement.get("aircraft_motion_limits")
                or self._aircraft_limits(planning)
            ),
            "domain_evidence": evidence,
            "source_audit": {
                "refinement_source_audit": result.get("source_audit") or {},
                "validation_evidence_source": source,
                "validator_versions": dict(VALIDATOR_VERSIONS),
                "curve_error": realization.get("curve_error"),
                "synthetic_spec": synthetic_spec if source == "canonical_synthetic" else None,
                "adapter_id": (evidence or {}).get("adapter_id"),
            },
            "provenance": {
                "evidence_source": source,
                "realized_continuous_route": True,
                "operational_routes_untouched": True,
                "algorithm_selection_untouched": True,
                "spatial_3d_untouched": True,
            },
        })
        problem["fingerprint_components"] = validation_fingerprint_components(problem)
        return problem

    def _validation_record(self, refinement, policy, source, synthetic_spec, problem,
                           validation, realization):
        """Build the stored V3-C validation record (the caller installs it)."""

        result = refinement.get("result") or {}
        identity = _hash({
            "experiment_id": refinement.get("experiment_id"),
            "refinement_id": refinement.get("refinement_id"),
            "evidence_source": source,
            "validation_policy": {
                "curve_chord_error_m": policy.get("curve_chord_error_m"),
                "max_validation_samples": policy.get("max_validation_samples"),
                "use_curve_error_envelope": policy.get("use_curve_error_envelope"),
            },
            "synthetic_spec": synthetic_spec if source == "canonical_synthetic" else None,
            "validation_fingerprint": problem.get("validation_fingerprint"),
        })
        return {
            "validation_id": "V3C-" + identity[:12].upper(),
            "experiment_id": refinement.get("experiment_id"),
            "refinement_id": refinement.get("refinement_id"),
            "route_id": refinement.get("route_id"),
            "created_at": utc_now(),
            "evidence_source": source,
            "synthetic_continuous_spec": synthetic_spec if source == "canonical_synthetic" else None,
            "validation_fingerprint": problem.get("validation_fingerprint"),
            #: The evidence a *re-run* would compare against (same shape as
            #: ``continuous_validation_snapshot`` recomputes), plus the raw problem
            #: components kept for audit.
            "problem_fingerprint_components": deepcopy(problem.get("fingerprint_components") or {}),
            "evidence_components": self._validation_components(
                refinement, source=source, policy=policy,
            ),
            "curve_error": deepcopy(realization.get("curve_error")),
            "source_audit": deepcopy(problem.get("source_audit") or {}),
            "result": deepcopy(validation),
            "current_applicability": "current",
            "provenance": {
                "recorded_at": utc_now(),
                "validator": {
                    "algorithm_id": self.continuous_validator.algorithm_id,
                    "algorithm_version": self.continuous_validator.algorithm_version,
                    "model_scope": self.continuous_validator.model_scope,
                    "validator_versions": dict(VALIDATOR_VERSIONS),
                },
                "refinement_fingerprint": result.get("refinement_fingerprint"),
                "operational_routes_untouched": True,
                "algorithm_selection_untouched": True,
                "spatial_3d_untouched": True,
                "exact_validation_is_v3c": True,
                "operational_route_always_false": True,
                "cns_assessed_always_false": True,
            },
            "verdicts": {
                "validated_route": (validation.get("status") == "validated_route"),
                "operational_route": False,
                "cns_assessed": False,
                "automatic_repair_performed": False,
                "automatic_replan_performed": False,
                "automatic_ranking": False,
                "automatically_scored": False,
            },
            "note": V3C_EXPERIMENT_NOTE,
        }

    def _validation_components(self, refinement, *, source, policy):
        """The V3-C fingerprint components, built exactly one way.

        Both the write path (with the policy that actually ran) and the applicability
        path (with the currently stored policy) use this single construction, so a
        fresh validation is ``current`` and becomes ``stale`` only when the explicit
        curve tolerance, the policy, the tracked sources or the validator versions
        actually change.  ``refinement_fingerprint`` is copied from the refinement's own
        evidence, which is where a changed V3-B refinement shows up.
        """

        result = (refinement.get("result") or {})
        policy = policy or {}
        return {
            "refinement_fingerprint": (
                refinement.get("refinement_fingerprint")
                or result.get("refinement_fingerprint")
            ),
            "continuous_policy_fingerprint": contract_fingerprint(policy, prefix="V3CPOL-"),
            "curve_tolerance_fingerprint": contract_fingerprint(
                {
                    "curve_chord_error_m": policy.get("curve_chord_error_m"),
                    "use_curve_error_envelope": policy.get("use_curve_error_envelope"),
                },
                prefix="V3CCURVE-",
            ),
            "source_fingerprint": self._validation_source_fingerprint(
                source=source, refinement=refinement, policy=policy,
            ),
            "crs_fingerprint": contract_fingerprint(
                {
                    "horizontal_crs": (result.get("frame") or {}).get("horizontal_crs"),
                    "horizontal_crs_source": (result.get("frame") or {}).get("horizontal_crs_source"),
                    "local_to_geographic": (
                        (result.get("frame") or {}).get("local_to_geographic") or {}
                    ).get("method"),
                    "frame_id": (result.get("frame") or {}).get("frame_id"),
                    "vertical_reference": (result.get("frame") or {}).get("vertical_reference"),
                },
                prefix="V3CCRS-",
            ),
            "validator_versions_fingerprint": contract_fingerprint(
                VALIDATOR_VERSIONS, prefix="V3CVAL-",
            ),
        }

    def _current_validation_components(self, refinement, validation):
        """Current evidence for a stored validation, in the same shape."""

        return self._validation_components(
            refinement,
            source=validation.get("evidence_source"),
            policy=self._continuous_policy({}),
        )

    def _validation_source_fingerprint(self, *, source, refinement, policy):
        """Scope-aware source fingerprint, matching the V3-B convention."""

        state = self.session.state
        if source == "configured_real_sources":
            audits = (state.get("source_audits") or {}).get("items") or {}
            relevant = {}
            for role in ("terrain", "terrain_dtm", "buildings", "building_grid", "airspace"):
                item = audits.get(role)
                if not isinstance(item, dict):
                    continue
                relevant[role] = {
                    "status": item.get("status"),
                    "version_fingerprint": item.get("version_fingerprint"),
                    "sha256": item.get("sha256"),
                    "size_bytes": item.get("size_bytes"),
                    "mtime_ns": item.get("mtime_ns"),
                }
            eligibility = ((state.get("grid_attributes") or {}).get("airspace") or {}).get(
                "airspace_eligibility",
            ) or {}
            return contract_fingerprint(
                {
                    "scope": "configured_real_sources",
                    "source_audits": relevant,
                    "airspace_eligibility": {
                        "fingerprint": eligibility.get("fingerprint"),
                        "status": eligibility.get("status"),
                        "allowed_grid_cell_count": len(eligibility.get("allowed_grid_ids") or []),
                    },
                    "environment_audit_fingerprint": (
                        ((refinement.get("result") or {}).get("source_audit") or {}).get("fingerprint")
                    ),
                },
                prefix="V3CSRC-",
            )
        grid = state.get("grid") or {}
        workspace = state.get("workspace") or {}
        return contract_fingerprint(
            {
                "scope": "canonical_synthetic",
                "workspace_bbox": workspace.get("bbox"),
                "grid_level": grid.get("level"),
                "grid_fingerprint": contract_fingerprint(
                    [
                        {"grid_id": cell.get("grid_id"), "bbox": cell.get("bbox")}
                        for cell in grid.get("cells") or []
                    ],
                    prefix="V3CGRIDSRC-",
                ),
                "policy_fingerprint": contract_fingerprint(policy or {}, prefix="V3CPOL-"),
            },
            prefix="V3CSRC-",
        )

    def readiness_snapshot(self):
        """Readiness of the *current project* for V3-A, plus the real-data verdict.

        The domain-level airspace/terrain/building readiness is only meaningful
        against a canonical environment.  V3-A has no real adapter, so this
        snapshot reports the policy/aircraft readiness that *is* project state and
        states explicitly that environment readiness is evaluated per synthetic
        run instead of being faked here.
        """

        state = self.session.state
        workspace = state.get("workspace") or {}
        workspace_ready = bool(workspace and workspace.get("status") == "passed")
        grid = state.get("grid") or {}
        grid_ready = bool(grid.get("status") == "passed" and grid.get("cells"))
        level = grid.get("level")
        recorded_policy = self.policy_snapshot()
        generated = None
        if workspace_ready and (not grid_ready or int(level or 0) != 8):
            generated = self.grid_service.generate(list(workspace["bbox"]), 8)
            grid_for_problem = generated
        else:
            grid_for_problem = grid
        kinematics = kinematic_readiness(recorded_policy, self._aircraft_limits(recorded_policy))
        policy_missing = list(recorded_policy.get("missing_parameters") or [])
        return {
            "status": "passed" if workspace_ready else "missing_data",
            "architecture": V3_ARCHITECTURE_SUMMARY,
            "stage": "V3-A",
            "stage_scope": {
                "implemented": [
                    "confirmation_contracts", "hard_constraint_kernel",
                    "3d_heading_state_space", "l8_strategic_search",
                    "soft_cost_vector", "refinement_corridor_proposal",
                ],
                "not_implemented": [
                    "corridor_local_fine_refinement_in_this_stage",
                    "exact_polygon_terrain_continuous_clearance_validation",
                    "operational_adapter", "cns_joint_optimization", "energy_model",
                ],
                "implemented_in_other_stages": {
                    "corridor_local_fine_refinement": "V3-B",
                    "exact_validation": "V3-C",
                    "validated_route_operational_adapter_and_cns_assessment": "V3-D",
                },
            },
            "grid": {
                "status": "ready" if grid_ready and int(level or 0) == 8 else (
                    "regenerable_at_l8" if generated else "missing_data"
                ),
                "level": level,
                "cell_count": len(grid.get("cells") or []),
                "l8_cell_count": len((grid_for_problem or {}).get("cells") or []),
                "reason": None if grid_ready and int(level or 0) == 8 else (
                    "当前网格不是 L8：实验会按工作区 bbox 临时生成 L8 战略网格，不写入项目网格"
                    if generated else "请先保存工作区"
                ),
            },
            "algorithm": {
                "algorithm_id": self.planner.algorithm_id,
                "algorithm_version": self.planner.algorithm_version,
                "model_scope": self.planner.model_scope,
                "registered_in_algorithm_registry": False,
                "default_route_planner": "route_planner_v1",
                "semantics": "additive_experimental_container_not_the_default_planner",
            },
            "policy": recorded_policy,
            "policy_readiness": {
                "status": "blocked" if policy_missing else ("ready" if recorded_policy.get("confirmed") else "pending"),
                "missing_parameters": policy_missing,
                "confirmed": bool(recorded_policy.get("confirmed")),
                "source": recorded_policy.get("source"),
                "reasons": (
                    ["缺少必需安全参数：" + ", ".join(policy_missing)] if policy_missing
                    else [] if recorded_policy.get("confirmed")
                    else ["policy 未经项目工程依据确认（confirmed=false）"]
                ),
                "semantics": "no_safety_parameter_has_a_default_value",
            },
            "aircraft_readiness": {
                "status": "ready" if (
                    kinematics["resolved"] and recorded_policy.get("aircraft_min_turn_radius_m") is not None
                ) else "blocked",
                "min_turn_radius_m": recorded_policy.get("aircraft_min_turn_radius_m"),
                "turn_radius_source": (
                    "policy.aircraft_min_turn_radius_m"
                    if recorded_policy.get("aircraft_min_turn_radius_m") is not None
                    else "unresolved_no_turn_radius_is_ever_guessed"
                ),
                "reasons": (
                    [] if kinematics["resolved"] and recorded_policy.get("aircraft_min_turn_radius_m") is not None
                    else ["climb/descent 能力或最小转弯半径未解析；只有 rate 而没有 explicit "
                          "planning_speed_mps 时禁止换算"]
                ),
                **kinematics,
            },
            "environment_readiness": {
                "status": "pending" if "canonical_synthetic" in V3_ENVIRONMENT_SOURCES else "blocked",
                "reason": (
                    "真实数据 adapter 未实现；readiness 在每次实验时对 canonical synthetic 环境逐格评估"
                ),
                "domains_evaluated": ["airspace", "terrain", "building", "policy", "aircraft", "cost_model"],
                "evaluated_in": "run_result.readiness",
            },
            "synthetic_environment_options": {
                "terrain_profiles": list(TERRAIN_PROFILES),
                "buildings_profiles": list(BUILDING_PROFILES),
                "sources": list(V3_ENVIRONMENT_SOURCES),
            },
            "real_data_readiness": self.real_data_readiness(),
            "note": V3_EXPERIMENT_NOTE,
        }

    def real_data_readiness(self):
        """Explicit verdict on real-data readiness for V3-A and V3-B."""

        state = self.session.state
        airspace = ((state.get("grid_attributes") or {}).get("airspace") or {})
        eligibility = airspace.get("airspace_eligibility") or {}
        profiles = state.get("data_source_profiles") or {}
        terrain_dtm_profile = profiles.get("terrain_dtm") or {}
        terrain_dtm_crs = terrain_dtm_profile.get("crs") or {}

        audits = (state.get("source_audits") or {}).get("items") or {}
        building_grid_audit = audits.get("building_grid") or {}

        fine = self._real_source_readiness()
        return {
            "status": "blocked",
            "adapter_status": V3_REAL_DATA_ADAPTER_STATUS,
            "reason": V3_REAL_DATA_ADAPTER_REASON,
            "terrain_source_declared": bool(
                terrain_dtm_profile.get("source_id")
                or terrain_dtm_profile.get("name")
            ),
            "terrain_vertical_reference": (
                    terrain_dtm_crs.get("observed_vertical")
                    or terrain_dtm_crs.get("vertical")
            ),
            ...
                "building_grid_source": (
                building_grid_audit.get("status") == "verified"
        ),
            "v3a_status": "blocked",
            "v3b": {
                "status": fine.get("status"),
                "adapter_status": V3B_REAL_DATA_ADAPTER_STATUS,
                "reason": V3B_REAL_DATA_ADAPTER_REASON,
                "blocking_reasons": list(fine.get("blocking_reasons") or []),
                "resolution_policy": fine.get("resolution_policy"),
                "terrain_dtm": fine.get("terrain_dtm"),
                "buildings": fine.get("buildings"),
            },
            "v3c": {
                "status": fine.get("status"),
                "adapter_status": V3C_REAL_DATA_ADAPTER_STATUS,
                "reason": V3C_REAL_DATA_ADAPTER_REASON,
                "blocking_reasons": list(fine.get("blocking_reasons") or []),
                "confirmed_allowed_grid_cells": len(eligibility.get("allowed_grid_ids") or []),
                "resolution_policy": fine.get("resolution_policy"),
                "terrain_dtm": fine.get("terrain_dtm"),
                "buildings": fine.get("buildings"),
                "requires": [
                    "confirmed AirspacePolicy polygons in the local metric frame",
                    "FABDEM native pixel window with confirmed egm2008_orthometric vertical reference",
                    "buildings GeoPackage with provider spatial index and height field",
                    "explicit confirmed V3-C validation policy (curve_chord_error_m, sample budget)",
                ],
            },
            "required_before_real_run": [
                "canonical V3CellEnvironment adapter (terrain surface floor, building required "
                "clearance, confirmed airspace classification) with audited provenance",
                "explicit confirmed V3 planning policy and confirmed fine refinement policy "
                "(local metric CRS + resolution source)",
                "explicit confirmed V3-C validation policy (curve_chord_error_m has no default)",
                "FABDEM terrain_dtm + buildings GeoPackage with provider spatial index",
                "V3-D operational adapter + CNS assessment (V3-C validated route is still not operational)",
            ],
        }

    # ------------------------------------------------------------------ policy

    def set_policy(self, payload):
        policy = normalize_v3_planning_policy(payload)
        self.session.state["v3_planning_policy"] = policy
        self.session.save()
        return self.readiness_snapshot()

    def set_fine_policy(self, payload):
        policy = normalize_v3_fine_refinement_policy(payload)
        self.session.state["v3_fine_refinement_policy"] = policy
        self.session.save()
        return self.refinement_readiness_snapshot()

    # ------------------------------------------------------------------ V3-B internals

    def _selected_candidate(self, payload):
        records = (self.session.state.get("route_planner_v3_experiments") or {}).get("records") or []
        experiment_id = str(payload.get("experiment_id") or "")
        if experiment_id:
            return next(
                (item for item in records if item.get("experiment_id") == experiment_id), None,
            )
        route_id = str(payload.get("route_id") or "")
        candidates = [
            item for item in records
            if (item.get("result") or {}).get("status") == "strategic_candidate"
            and (not route_id or str(item.get("route_id")) == route_id)
        ]
        return candidates[0] if candidates else None

    def _fine_config(self, payload):
        """Effective V3-B fine configuration (resolution + metric CRS), never defaulted."""

        stored = self.fine_policy_snapshot()
        supplied = payload.get("fine_policy")
        if isinstance(supplied, dict):
            merged = dict(supplied)
            if merged.get("source") in (None, "") and stored.get("source"):
                merged["source"] = stored["source"]
            if "confirmed" not in merged and stored.get("confirmed"):
                merged["confirmed"] = True
            configured = normalize_v3_fine_refinement_policy(merged)
        else:
            configured = normalize_v3_fine_refinement_policy(stored)
        cell_size = payload.get("refinement_cell_size_m")
        if cell_size not in (None, ""):
            configured["resolution_m"] = float(cell_size)
            configured["resolution_source"] = "explicit_configuration"
            configured["reasons"] = [
                reason for reason in configured.get("reasons") or []
                if "resolution_source" not in reason and "resolution_m" not in reason
            ]
        configured["usable_resolution"] = (
            configured.get("resolution_source") == "dtm_effective_resolution"
            or (
                configured.get("resolution_source") == "explicit_configuration"
                and configured.get("resolution_m") is not None
            )
        )
        configured["stride"] = int(payload.get("max_stride_cells") or configured.get("max_stride_cells") or 1)
        return configured

    def _synthetic_fine_environment(self, record, corridor, fine, spec):
        """Deterministically rebuild the coarse synthetic environment, then refine it.

        The synthetic path has no DTM, so the resolution must come from an explicit
        configuration: ``dtm_effective_resolution`` cannot be resolved here and is
        reported as blocked rather than replaced by an assumed constant.
        """

        if not fine.get("usable_resolution") or fine.get("resolution_m") is None:
            return {
                "status": "blocked",
                "reason": "fine_resolution_unresolved",
                "readiness": {},
                "frame": None, "fine_grid": None, "environment": None,
                "source_audit": {},
            }
        if fine.get("resolution_source") != "explicit_configuration":
            return {
                "status": "blocked",
                "reason": "synthetic_source_requires_explicit_configuration_resolution",
                "readiness": {},
                "frame": None, "fine_grid": None, "environment": None,
                "source_audit": {},
            }
        policy = record.get("policy") or self.policy_snapshot()
        grid = self._l8_grid()
        parent_environment = build_synthetic_environment(
            grid.get("cells") or [], policy, record.get("environment_spec") or {},
            source_detail={"requested_by": "route_planner_v3_refinement_service"},
        )
        built = build_synthetic_fine_environment(
            parent_environment=parent_environment,
            corridor=corridor,
            policy=policy,
            spec={
                **{key: value for key, value in spec.items() if key != "profile_id"},
                "profile_id": str(spec.get("profile_id") or "synthetic_fine_open"),
                "resolution_m": float(fine["resolution_m"]),
                "max_stride_cells": int(fine.get("stride") or 1),
            },
            source_detail={"requested_by": "route_planner_v3_refinement_service"},
        )
        built["resolution"] = {
            "resolution_m": float(fine["resolution_m"]),
            "resolution_source": "explicit_configuration",
            "requested_resolution_m": float(fine["resolution_m"]),
            "effective_source_resolution_m": None,
            "source": "synthetic_service_explicit_configuration",
        }
        built["parent_environment_fingerprint"] = _hash(parent_environment)
        return {"status": "passed", "reason": None, **built}

    def _real_fine_environment(self, adapter, record, corridor, policy, fine, payload):
        readiness = self._real_source_readiness()
        if adapter is None:
            return {
                "status": "blocked",
                "reason": "configured_real_sources_unavailable",
                "readiness": readiness,
                "frame": None, "fine_grid": None, "environment": None,
                "source_audit": {},
            }
        if not fine.get("usable_resolution"):
            return {
                "status": "blocked",
                "reason": "fine_resolution_unresolved",
                "readiness": readiness,
                "frame": None, "fine_grid": None, "environment": None,
                "source_audit": {},
            }
        if fine.get("horizontal_crs") and getattr(adapter, "transform", None) is None:
            return {
                "status": "blocked",
                "reason": "metric_transform_unavailable",
                "readiness": readiness,
                "frame": None, "fine_grid": None, "environment": None,
                "source_audit": {},
            }
        adapter.resolution_m = fine.get("resolution_m")
        adapter.resolution_source = fine.get("resolution_source")
        adapter.max_stride_cells = fine.get("stride") or 1
        parent_cells = self._parent_cells_for_corridor(corridor)
        built = adapter.build(
            policy=policy, corridor=corridor, parent_cells=parent_cells,
            source_detail={"requested_by": "route_planner_v3_refinement_service"},
        )
        return built

    def _parent_cells_for_corridor(self, corridor):
        """Coarse canonical cells for the corridor, rebuilt deterministically."""

        state = self.session.state
        grid = state.get("grid") or {}
        cells = grid.get("cells") or []
        if not cells or int(grid.get("level") or 0) != 8:
            cells = self.grid_service.generate(
                list((state.get("workspace") or {}).get("bbox") or []), 8,
            ).get("cells") or []
        wanted = set(str(item) for item in corridor.get("support_grid_ids") or [])
        selected = [cell for cell in cells if str(cell.get("grid_id")) in wanted]
        eligibility = ((state.get("grid_attributes") or {}).get("airspace") or {}).get(
            "airspace_eligibility",
        ) or {}
        allowed = set(str(item) for item in eligibility.get("allowed_grid_ids") or [])
        for cell in selected:
            grid_id = str(cell["grid_id"])
            cell["airspace"] = {
                "status": "confirmed_allowed" if grid_id in allowed else "unknown",
                "feature_id": None,
                "policy_confirmed": bool(eligibility.get("status") == "passed"),
            }
        return selected

    def _refinement_record(self, record, fine, source, synthetic_spec, built):
        result = built.get("environment")
        policy = record.get("policy") or self.policy_snapshot()
        if built.get("status") != "passed" or result is None:
            refinement_result = normalize_v3_refinement_result({
                "status": "not_ready",
                "problem_id": f"v3b-{record.get('experiment_id')}",
                "route_id": record.get("route_id"),
                "reason": built.get("reason") or "fine environment unavailable",
                "readiness": built.get("readiness") or {},
                "refinements_blocked": True,
            })
            fingerprint_components = {}
            refinement_fingerprint = None
        else:
            problem = self._refinement_problem(record, fine, built)
            refinement_result = self.refinement_planner.plan(problem)
            refinement_result = normalize_v3_refinement_result(refinement_result)
            fingerprint_components = refinement_fingerprint_components(problem)
            refinement_fingerprint = problem.get("refinement_fingerprint")
        identity = _hash({
            "experiment_id": record.get("experiment_id"),
            "environment_source": source,
            "fine_policy": {
                "resolution_m": fine.get("resolution_m"),
                "resolution_source": fine.get("resolution_source"),
                "horizontal_crs": fine.get("horizontal_crs"),
                "max_stride_cells": fine.get("stride"),
            },
            "synthetic_spec": synthetic_spec if source == "canonical_synthetic" else None,
            "refinement_fingerprint": refinement_fingerprint,
            "blocked_reason": None if built.get("status") == "passed" else built.get("reason"),
        })
        source_audit = (
            (built.get("environment") or {}).get("source_audit") or {}
        )
        # Evidence components: the immutable stored evidence (strategic candidate,
        # corridor, frame, fine grid) plus the *live project state* at run time.
        # ``refinement_snapshot`` recomputes exactly the same shape from the live
        # project, so a fresh refinement is ``current`` and becomes ``stale`` only
        # when the policy or the scope-relevant sources actually change.
        evidence_components = dict(fingerprint_components)
        evidence_components.update(self._evidence_fingerprints(
            environment_source=source,
            environment_fingerprint=source_audit.get("fingerprint"),
        ))
        return {
            "refinement_id": "V3B-" + identity[:12].upper(),
            "experiment_id": record.get("experiment_id"),
            "route_id": record.get("route_id"),
            "created_at": utc_now(),
            "environment_source": source,
            "grounding": "corridor_local_fine_grid",
            "fine_policy": deepcopy(fine),
            "synthetic_fine_spec": synthetic_spec if source == "canonical_synthetic" else None,
            "refinement_fingerprint": refinement_fingerprint,
            "fingerprint_components": fingerprint_components,
            "evidence_components": evidence_components,
            "source_audit": deepcopy(source_audit),
            "source_audit_fingerprint": source_audit.get("fingerprint"),
            "resolution": deepcopy(built.get("resolution") or (built.get("fine_grid") or {}).get("resolution_m")),
            "result": refinement_result,
            "readiness": deepcopy(refinement_result.get("readiness")),
            "readiness_overall": readiness_overall(refinement_result.get("readiness") or {}),
            "current_applicability": "current",
            "provenance": {
                "recorded_at": utc_now(),
                "planner": {
                    "algorithm_id": self.refinement_planner.algorithm_id,
                    "algorithm_version": self.refinement_planner.algorithm_version,
                    "model_scope": self.refinement_planner.model_scope,
                    "turn_model": self.refinement_planner.turn_model,
                },
                "strategic_experiment_id": record.get("experiment_id"),
                "strategic_fingerprint": (record.get("result") or {}).get("input_fingerprint"),
                "operational_routes_untouched": True,
                "algorithm_selection_untouched": True,
                "spatial_3d_untouched": True,
                "not_final_validation": True,
                "exact_validation_is_v3c": True,
            },
            "verdicts": {
                "operational_route": False,
                "final_validation_performed": False,
                "exact_validation_performed": False,
                "requires_v3c_exact_validation": True,
                "automatic_ranking": False,
                "automatically_scored": False,
            },
            "note": V3B_EXPERIMENT_NOTE,
        }

    def _refinement_problem(self, record, fine, built):
        result = record.get("result") or {}
        state_path = result.get("state_path") or []
        strategic = result.get("trajectory_summary") or {}
        environment = built.get("environment") or {}
        source_audit = environment.get("source_audit") or {}
        strategic_candidate = {
            "status": result.get("status"),
            "experiment_id": record.get("experiment_id"),
            "strategic_fingerprint": result.get("input_fingerprint"),
            "current_applicability": record.get("current_applicability") or "current",
            "corridor_id": (result.get("candidate_refinement_corridor") or {}).get("corridor_id"),
            "start_metric": None,
            "goal_metric": None,
            "explicit_goal_altitude": bool(
                (result.get("effective_policy") or {}) and strategic.get("endpoint_altitude_semantics", "").startswith(
                    "problem.goal.z 为显式硬约束"
                )
            ),
            "start_altitude_egm2008_m": state_path[0].get("altitude_egm2008_m") if state_path else None,
            "goal_altitude_egm2008_m": state_path[-1].get("altitude_egm2008_m") if state_path else None,
            "state_count": len(state_path),
        }
        if state_path:
            strategic_candidate["start_point"] = [state_path[0].get("x"), state_path[0].get("y")]
            strategic_candidate["goal_point"] = [state_path[-1].get("x"), state_path[-1].get("y")]
        problem = normalize_v3_refinement_problem({
            "problem_id": f"v3b-{record.get('experiment_id')}",
            "route_id": record.get("route_id"),
            "corridor": result.get("candidate_refinement_corridor"),
            "policy": record.get("policy"),
            "aircraft_motion_limits": record.get("aircraft_motion_limits"),
            "frame": built.get("frame"),
            "fine_grid": built.get("fine_grid"),
            "environment": environment,
            "max_stride_cells": fine.get("stride") or 1,
            "source_audit": source_audit,
            "strategic_candidate": strategic_candidate,
            "provenance": {
                "environment_source": built.get("environment_source") or "canonical_synthetic",
                "fine_policy": {
                    "resolution_m": fine.get("resolution_m"),
                    "resolution_source": fine.get("resolution_source"),
                    "horizontal_crs": fine.get("horizontal_crs"),
                },
            },
        })
        return problem

    def _real_source_readiness(self):
        if callable(self.source_readiness):
            return self.source_readiness()
        # No injected provider (for example a headless test workflow): report what
        # project state can tell us.  Configured file paths live in MapData, which
        # only ApplicationContext can see, so they read as not configured here
        # rather than being guessed.
        state = self.session.state
        eligibility = ((state.get("grid_attributes") or {}).get("airspace") or {}).get(
            "airspace_eligibility",
        ) or {}
        policy = state.get("v3_planning_policy") or {}
        return real_data_source_readiness(
            {}, airspace_eligibility=eligibility,
            policy_confirmed=bool(policy.get("confirmed")),
        )

    def _current_evidence_components(self, refinement, recorded):
        """Current evidence a stored refinement depends on, in the *same shape*.

        ``policy_fingerprint`` / ``source_fingerprint`` are recomputed from the live
        project; the stored strategic candidate, corridor, local frame and fine grid
        are immutable evidence and carry through unchanged (merged from
        ``recorded``).  Comparing like for like is what makes the verdict meaningful:
        at write time both sides are computed from the same state, so a fresh
        refinement is ``current``, and it becomes ``stale`` only when the policy or
        the scope-relevant sources actually change.
        """

        result = dict(recorded or {})
        result.update(self._evidence_fingerprints(
            environment_source=refinement.get("environment_source"),
            environment_fingerprint=(
                ((refinement.get("result") or {}).get("fine_grid_evidence") or {}).get(
                    "source_audit_fingerprint",
                )
            ),
        ))
        return result

    def _evidence_fingerprints(self, *, environment_source, environment_fingerprint=None):
        """Live-state evidence fingerprints for the *scope* a refinement depends on.

        * ``configured_real_sources``: the tracked source audits (terrain / DTM /
          buildings), the confirmed airspace eligibility and the existing risk
          model identity;
        * ``canonical_synthetic``: the synthetic environment is fully described by
          its own spec, so only the workspace/grid it was rebuilt inside and the
          recorded environment fingerprint are scope-relevant -- unrelated real
          source files must not mark a synthetic exercise stale.
        """

        state = self.session.state
        audits = (state.get("source_audits") or {}).get("items") or {}
        relevant_audits = {}
        for role in ("terrain", "terrain_dtm", "buildings", "building_grid", "airspace"):
            item = audits.get(role)
            if not isinstance(item, dict):
                continue
            relevant_audits[role] = {
                "status": item.get("status"),
                "version_fingerprint": item.get("version_fingerprint"),
                "sha256": item.get("sha256"),
                "size_bytes": item.get("size_bytes"),
                "mtime_ns": item.get("mtime_ns"),
            }
        eligibility = ((state.get("grid_attributes") or {}).get("airspace") or {}).get(
            "airspace_eligibility",
        ) or {}
        risk = state.get("grid_risk") or {}
        policy_fingerprint = contract_fingerprint(
            {
                "scope": "project_state_policy",
                "v3_planning_policy": state.get("v3_planning_policy") or {},
                "v3_fine_refinement_policy": state.get("v3_fine_refinement_policy") or {},
            },
            prefix="V3BPOL-",
        )
        if environment_source == "configured_real_sources":
            source_fingerprint = contract_fingerprint(
                {
                    "scope": "configured_real_sources",
                    "source_audits": relevant_audits,
                    "airspace_eligibility": {
                        "fingerprint": eligibility.get("fingerprint"),
                        "status": eligibility.get("status"),
                        "allowed_grid_cell_count": len(eligibility.get("allowed_grid_ids") or []),
                    },
                    "risk_model": {
                        "algorithm_id": risk.get("algorithm_id"),
                        "algorithm_version": risk.get("algorithm_version"),
                        "status": risk.get("status"),
                        "grid_level": risk.get("grid_level"),
                    },
                    "environment_audit_fingerprint": environment_fingerprint,
                },
                prefix="V3BSRC-",
            )
        else:
            grid = state.get("grid") or {}
            workspace = state.get("workspace") or {}
            source_fingerprint = contract_fingerprint(
                {
                    "scope": "canonical_synthetic",
                    "workspace_bbox": workspace.get("bbox"),
                    "grid_level": grid.get("level"),
                    "grid_fingerprint": contract_fingerprint(
                        [
                            {"grid_id": cell.get("grid_id"), "bbox": cell.get("bbox")}
                            for cell in grid.get("cells") or []
                        ],
                        prefix="V3BGRIDSRC-",
                    ),
                    "environment_audit_fingerprint": environment_fingerprint,
                    "airspace_eligibility_fingerprint": eligibility.get("fingerprint"),
                },
                prefix="V3BSRC-",
            )
        return {
            "policy_fingerprint": policy_fingerprint,
            "source_fingerprint": source_fingerprint,
        }

    # ------------------------------------------------------------------ evaluate

    def evaluate(self, payload=None):
        payload = payload if isinstance(payload, dict) else {}
        state = self.session.state
        workspace = state.get("workspace") or {}
        if not workspace or workspace.get("status") != "passed":
            raise ValueError("请先保存工作区，再运行 V3 战略规划实验")
        source = str(payload.get("environment_source") or "canonical_synthetic")
        if source not in V3_ENVIRONMENT_SOURCES:
            raise ValueError(
                "V3-A 只支持 canonical_synthetic 环境；真实数据 adapter 在 V3-B/C 之前不可用"
            )
        policy = self._policy(payload)
        if policy.get("status") != "confirmed" and not payload.get("allow_unconfirmed_policy"):
            raise ValueError(
                "V3 policy 未确认：缺少显式安全参数或 confirmed=false。"
                "V3-A 禁止猜默认安全值；请先保存已确认 policy。"
            )
        spec = normalize_synthetic_spec(payload.get("synthetic_spec"))
        grid = self._l8_grid()
        environment = build_synthetic_environment(
            grid.get("cells") or [], policy, spec,
            source_detail={"requested_by": "route_planner_v3_experiment_service"},
        )
        start, goal = self._endpoints(payload, environment)
        problem = normalize_v3_planning_problem({
            "problem_id": str(payload.get("problem_id") or "v3-experiment"),
            "route_id": str(payload.get("route_id") or ""),
            "start": start,
            "goal": goal,
            "policy": policy,
            "aircraft_motion_limits": self._aircraft_limits(policy),
            "environment": environment,
            "provenance": {
                "environment_source": source,
                "synthetic_spec": spec,
                "corridor_ring_n": payload.get("corridor_ring_n") or 0,
                "refinement_cell_size_m": payload.get("refinement_cell_size_m"),
                "corridor_altitude_margin_m": payload.get("corridor_altitude_margin_m"),
                "grid_level": grid.get("level"),
            },
        })
        result = self.planner.plan(problem)
        record = self._record(payload, policy, spec, environment, problem, result)
        existing = (state.get("route_planner_v3_experiments") or {}).get("records") or []
        retained = [item for item in existing if item.get("experiment_id") != record["experiment_id"]]
        state["route_planner_v3_experiments"] = normalize_v3_experiments({
            "records": [record, *retained],
            "active_experiment_id": record["experiment_id"],
        })
        # V3-A writes only its own container: no operational route, no algorithm
        # selection, no spatial_3d and therefore no V1/V2/P7-P19 invalidation.
        self.session.save()
        return self.snapshot()

    def delete_experiment(self, experiment_id):
        state = self.session.state
        collection = state.get("route_planner_v3_experiments") or {}
        records = collection.get("records") or []
        retained = [item for item in records if item.get("experiment_id") != experiment_id]
        if len(retained) == len(records):
            raise ValueError("V3 实验记录不存在")
        active = collection.get("active_experiment_id")
        if active == experiment_id:
            active = retained[0]["experiment_id"] if retained else None
        state["route_planner_v3_experiments"] = normalize_v3_experiments({
            "records": retained, "active_experiment_id": active,
        })
        self.session.save()
        return self.snapshot()

    # ------------------------------------------------------------------ internals

    def _policy(self, payload):
        """Effective policy for one run.

        A payload policy is a *complete* safety parameter set for that run; the
        stored policy only supplies the two provenance fields a payload usually
        omits (``source`` and ``confirmed``) when the payload does not state them.
        Safety parameters are never merged across the two, and never defaulted.
        """

        recorded = self.session.state.get("v3_planning_policy") or {}
        supplied = payload.get("policy")
        if supplied is None:
            return normalize_v3_planning_policy(recorded)
        merged = dict(supplied)
        if merged.get("source") in (None, "") and recorded.get("source"):
            merged["source"] = recorded["source"]
        if "confirmed" not in merged and recorded.get("confirmed"):
            merged["confirmed"] = True
        return normalize_v3_planning_policy(merged)

    def _aircraft_limits(self, policy):
        """Motion limits resolved only from explicitly supplied policy values."""

        return normalize_aircraft_motion_limits({
            "aircraft_id": str(policy.get("policy_id") or ""),
            "max_climb_gradient": policy.get("max_climb_gradient"),
            "max_descent_gradient": policy.get("max_descent_gradient"),
            "max_climb_rate_mps": policy.get("max_climb_rate_mps"),
            "max_descent_rate_mps": policy.get("max_descent_rate_mps"),
            "planning_speed_mps": policy.get("planning_speed_mps"),
            "min_turn_radius_m": policy.get("aircraft_min_turn_radius_m"),
            "confirmed": bool(policy.get("confirmed")),
            "source": policy.get("source"),
        })

    def _l8_grid(self):
        state = self.session.state
        grid = state.get("grid") or {}
        if grid.get("status") == "passed" and grid.get("cells") and int(grid.get("level") or 0) == 8:
            return grid
        workspace = state.get("workspace") or {}
        generated = self.grid_service.generate(list(workspace["bbox"]), 8)
        # The generated strategic grid is used for this experiment only; the
        # project's own grid is never replaced by this service.
        return generated

    def _endpoints(self, payload, environment):
        cells = environment.get("cells") or []
        route = self._scenario_route(payload)
        if route:
            start = route.get("start") or route.get("path", [None, None])[0]
            goal = route.get("end") or route.get("path", [None, None])[-1]
            if _point(start) and _point(goal):
                return list(start[:2]), list(goal[:2])
        explicit_start, explicit_goal = payload.get("start"), payload.get("goal")
        if _point(explicit_start) and _point(explicit_goal):
            return list(explicit_start[:2]), list(explicit_goal[:2])
        ordered = sorted(
            cells,
            key=lambda cell: (
                cell.get("row") if cell.get("row") is not None else 0,
                cell.get("column") if cell.get("column") is not None else 0,
                str(cell["grid_id"]),
            ),
        )
        if not ordered:
            raise ValueError("没有可用于 V3 实验的 canonical cell")
        return list(ordered[0]["center"]), list(ordered[-1]["center"])

    def _scenario_route(self, payload):
        state = self.session.state
        route_id = str(payload.get("route_id") or "")
        routes = state.get("scenario_routes") or []
        if route_id:
            for route in routes:
                if str(route.get("route_id")) == route_id:
                    return route
            for route in state.get("operational_routes") or []:
                if str(route.get("route_id")) == route_id:
                    return route
            return None
        return routes[0] if routes else None

    def _record(self, payload, policy, spec, environment, problem, result):
        grid = self.session.state.get("grid") or {}
        identity = _hash({
            "grid": {
                "level": problem["provenance"].get("grid_level"),
                "cells": [
                    {"grid_id": cell["grid_id"], "bbox": cell.get("bbox")}
                    for cell in environment.get("cells") or []
                ],
            },
            "policy": policy,
            "synthetic_spec": spec,
            "route_id": problem["route_id"],
            "start": problem["start"],
            "goal": problem["goal"],
            "corridor": {
                "ring_n": problem["provenance"].get("corridor_ring_n"),
                "cell_size_m": problem["provenance"].get("refinement_cell_size_m"),
            },
        })
        return {
            "experiment_id": "V3-" + identity[:12].upper(),
            "created_at": utc_now(),
            "source_type": "synthetic",
            "grounding": "canonical_synthetic_environment",
            "environment_source": "canonical_synthetic",
            "environment_spec": deepcopy(spec),
            "environment_fingerprint": _hash(environment),
            "policy": deepcopy(policy),
            "policy_fingerprint": _hash(policy),
            "aircraft_motion_limits": deepcopy(problem["aircraft_motion_limits"]),
            "route_id": problem["route_id"] or None,
            "problem_fingerprint": result.get("input_fingerprint"),
            "result": deepcopy(result),
            "readiness": deepcopy(result.get("readiness")),
            "readiness_overall": readiness_overall(result.get("readiness") or {}),
            "current_applicability": "current",
            #: V3-B corridor-local refinements of this strategic candidate.
            "refinements": [],
            "provenance": {
                "recorded_at": utc_now(),
                "planner": {
                    "algorithm_id": self.planner.algorithm_id,
                    "algorithm_version": self.planner.algorithm_version,
                    "model_scope": self.planner.model_scope,
                    "motion_model_id": (result.get("semantics") or {}).get("motion_model_id"),
                },
                "project_grid_level": grid.get("level"),
                "operational_routes_untouched": True,
                "algorithm_selection_untouched": True,
                "spatial_3d_untouched": True,
                "environment_semantics": "canonical V3CellEnvironment built from an explicit synthetic spec; not real data",
                "not_final_validation": True,
            },
            "verdicts": {
                "operational_route": False,
                "final_validation_performed": False,
                "automatic_ranking": False,
                "automatically_scored": False,
            },
            "note": V3_EXPERIMENT_NOTE,
        }


def _point(value):
    return (
        isinstance(value, (list, tuple)) and len(value) >= 2
        and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value[:2])
    )


def record_summary(record):
    """Small read-only projection used by the Step 03 panel (kept next to the contract)."""

    if not isinstance(record, dict):
        return None
    result = record.get("result") or {}
    corridor = result.get("candidate_refinement_corridor") or {}
    refinements = record.get("refinements") or []
    active_refinement = refinements[0] if refinements else None
    refinement_result = (active_refinement or {}).get("result") or {}
    evidence = refinement_result.get("fine_grid_evidence") or {}
    return {
        "experiment_id": record.get("experiment_id"),
        "created_at": record.get("created_at"),
        "status": result.get("status"),
        "readiness_overall": record.get("readiness_overall"),
        "distance_m": result.get("distance_m"),
        "state_count": len(result.get("state_path") or []),
        "expanded_states": (result.get("search_statistics") or {}).get("expanded_states"),
        "runtime_ms": (result.get("search_statistics") or {}).get("runtime_ms"),
        "scalar_cost": (result.get("cost_vector") or {}).get("scalar_cost"),
        "corridor_center_count": len(corridor.get("center_grid_ids") or []),
        "corridor_support_count": len(corridor.get("support_grid_ids") or []),
        "corridor_ring_n": corridor.get("ring_n"),
        # ---- V3-B refinement projection ------------------------------------
        "refinement_count": len(refinements),
        "refinement_id": (active_refinement or {}).get("refinement_id"),
        "refinement_status": refinement_result.get("status"),
        "refinement_distance_m": refinement_result.get("distance_m"),
        "refinement_resolution_m": evidence.get("resolution_m"),
        "refinement_resolution_source": evidence.get("resolution_source"),
        "refinement_cell_count": evidence.get("cell_count"),
        "refinement_environment_cell_count": evidence.get("environment_cell_count"),
        "refinement_expanded_states": (refinement_result.get("search_statistics") or {}).get("expanded_states"),
        "refinement_scalar_cost": (refinement_result.get("cost_vector") or {}).get("scalar_cost"),
        "refinement_final_validation_performed": False,
        "refinement_environment_source": (active_refinement or {}).get("environment_source"),
        # ---- V3-C validation projection -------------------------------------
        "validation_count": len((active_refinement or {}).get("validations") or []),
        "validation_id": _active_validation(active_refinement, "validation_id"),
        "validation_status": _active_validation_result(active_refinement).get("status"),
        "validation_domain_statuses": _active_validation_result(active_refinement).get("domain_statuses") or {},
        "validation_operational_route": False,
        "validation_cns_assessed": False,
    }


def _active_validation(refinement, field):
    validations = (refinement or {}).get("validations") or []
    return (validations[0] or {}).get(field) if validations else None


def _active_validation_result(refinement):
    validations = (refinement or {}).get("validations") or []
    return (validations[0] or {}).get("result") or {} if validations else {}


__all__ = [
    "V3_COLLECTION_ID", "V3_ENVIRONMENT_SOURCES", "MAX_V3_EXPERIMENTS",
    "MAX_V3_REFINEMENTS_PER_EXPERIMENT", "MAX_V3C_VALIDATIONS_PER_REFINEMENT",
    "V3_EXPERIMENT_NOTE", "V3_ARCHITECTURE_SUMMARY",
    "V3_REAL_DATA_ADAPTER_STATUS", "V3_REAL_DATA_ADAPTER_REASON",
    "V3B_ARCHITECTURE_SUMMARY", "V3B_ENVIRONMENT_SOURCES", "V3B_EXPERIMENT_NOTE",
    "V3B_REAL_DATA_ADAPTER_REASON", "V3B_REAL_DATA_ADAPTER_STATUS",
    "V3B_RESULT_STATUSES", "V3B_DISCLAIMER",
    "V3C_ARCHITECTURE_SUMMARY", "V3C_DISCLAIMER", "V3C_EVIDENCE_SOURCES",
    "V3C_EXPERIMENT_NOTE", "V3C_MODEL_SCOPE", "V3C_REAL_DATA_ADAPTER_REASON",
    "V3C_REAL_DATA_ADAPTER_STATUS", "V3C_RESULT_STATUSES",
    "RoutePlannerV3ExperimentService", "record_summary",
    "utc_now", "default_v3_cost_model", "default_v3_fine_refinement_policy",
    "default_v3_validation_policy",
]
