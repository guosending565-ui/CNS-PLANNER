"""Pure ProjectState snapshot to canonical P19 ReportDataModel."""

from __future__ import annotations

from copy import deepcopy

from ..domain.reporting import report_source_snapshot, stable_fingerprint


class ReportBuilder:
    schema_version = "1.0"

    def build(self, state, algorithm_catalog, generated_at, final=False):
        source = report_source_snapshot(state, algorithm_catalog)
        source_fingerprint = stable_fingerprint(source)
        plan = source.get("confirmed_cns_plan") or {}
        if final and plan.get("status") not in ("confirmed", "applied"):
            raise ValueError("生成正式报告前，必须先在方案审查中确认一个规划方案")
        plan_label = {
            "confirmed": "已确认方案（尚未应用）", "applied": "已确认并应用",
        }.get(plan.get("status"), "草稿预览（尚无已确认方案）")
        p15 = source.get("cns_corridor_gap_assessment") or {}
        model = {
            "report_schema_version": self.schema_version,
            "template_version": "cns-planning-report-zh-v1",
            "language": "zh-CN", "generated_at": generated_at,
            "report_mode": "final" if final else "draft_preview",
            "source": {
                "plan_id": plan.get("plan_id"), "plan_status": plan.get("status"),
                "fingerprint": source_fingerprint,
            },
            "plan_status_label": plan_label,
            "disclaimers": [
                "本报告是CNS规划工程交付物，不构成安全认证、运行批准或自动法规合规结论。",
                "未知（unknown）表示证据不足或尚无法判断，不代表通过或失败。",
                "P14体积为离散体积代理并采用代表点评价，不是精确三维体积保证。",
                "P15连续缺口为空间连续缺口投影，不是运行时连续性或可用性概率。",
                "RiskModelV1输出为相对工程风险指数，不是事故概率、SORA GRC或TLS。",
            ],
            "sections": {
                "project_overview": source.get("project") or {},
                "operation_and_requirement_basis": {
                    "operation_context": source.get("cns_operation_context") or {},
                    "recommendation": source.get("required_cns_recommendation") or {},
                    "adoption": source.get("required_cns_adoption") or {},
                    "policies": source.get("cns_requirement_policies") or {},
                },
                "data_foundation": source.get("data_source_profiles") or {},
                "building_environment_and_clearance": _building_section(source),
                "routes_altitude_corridor": {
                    "routes": source.get("operational_routes") or [],
                    "spatial_3d": source.get("spatial_3d") or {},
                    "corridor_policy": source.get("cns_corridor_policy") or {},
                },
                "required_cns": source.get("required_cns") or {},
                "centerline_gap_p10": source.get("cns_gap_analysis_v2") or {},
                "spatial_service_p14": source.get("cns_corridor_assessment") or {},
                "corridor_gap_objectives_p15": p15,
                "plan_review_p18": {
                    "review": source.get("cns_plan_review") or {},
                    "confirmed_plan": plan,
                    "p16_decision_evidence": source.get("cns_corridor_site_plan") or {},
                },
                "final_facility_plan": {
                    "plan_status": plan.get("status"),
                    "existing_facilities": source.get("existing_cns_facilities") or {},
                    "confirmed_actions": plan.get("confirmed_actions") or [],
                },
                "before_after_residual": {
                    "before": plan.get("before_p15"), "after": plan.get("after_p15"),
                    "residual": _residuals(p15), "unknown": _unknowns(p15),
                },
                "audit": {
                    "algorithm_selection": source.get("algorithm_selection") or {},
                    "algorithm_manifests": source.get("algorithm_manifests") or [],
                    "fingerprints": _fingerprints(source),
                    "data_sources": source.get("data_source_profiles") or {},
                    "provenance_chain": _provenance(source),
                },
                "limitations": {
                    "common_cause": "not_evaluated", "shared_power": "not_evaluated",
                    "shared_backhaul": "not_evaluated", "tower_failure": "not_evaluated",
                    "safety_certification": "not_evaluated",
                    "automatic_regulatory_compliance": "not_evaluated",
                    "fabdem": "FABDEM is not survey-grade DTM",
                    "gba_height": "GBA predicted height is not measured truth",
                    "building_clearance": "engineering assessment only; not certification or regulatory compliance",
                },
            },
        }
        model["statistics"] = _statistics(p15)
        model["report_data_fingerprint"] = stable_fingerprint(model)
        return model


def _statistics(p15):
    rows = []
    for route in p15.get("routes") or []:
        for item in route.get("subsystems") or []:
            rows.append({
                "route_id": route.get("route_id"), "subsystem": item.get("subsystem"),
                "service": deepcopy(item.get("service") or {}),
                "redundancy": deepcopy(item.get("redundancy") or {}),
                "combined": deepcopy(item.get("combined") or {}),
                "total_confirmed_deficit_projection_m": item.get("total_confirmed_deficit_projection_m"),
                "max_continuous_deficit_projection_m": item.get("max_continuous_deficit_projection_m"),
                "objective_status": item.get("objective_status"),
                "objective_results": deepcopy(item.get("objective_results") or []),
            })
    return {"rows": rows, "classification_semantics": "unknown_is_neither_pass_nor_fail"}


def _building_section(source):
    profiles = source.get("data_source_profiles") or {}
    grid = ((source.get("grid_attributes") or {}).get("buildings") or {})
    assessment = source.get("building_clearance_assessment") or {}
    provenance = assessment.get("provenance") or {}
    return {
        "data_sources": {
            "fabdem_dtm": profiles.get("terrain_dtm") or provenance.get("terrain_dtm") or {},
            "gba_lod1": provenance.get("buildings") or {},
            "building_l8_grid": grid.get("source") or {},
        },
        "data_quality": {
            "dtm": provenance.get("terrain_dtm") or {},
            "building_grid_metadata": grid.get("metadata") or {},
            "valid_height_coverage": ((provenance.get("buildings") or {}).get("valid_height_fraction")),
            "uncertainty_semantics": "height_var retained as raw source field only",
        },
        "algorithm": {
            "id": assessment.get("algorithm_id"), "version": assessment.get("algorithm_version"),
            "ground": "FABDEM footprint-mask median",
            "roof": "DTM ground median + GBA height_m",
            "policy": source.get("building_clearance_policy") or {},
            "vertical_datum": "EGM2008 orthometric when source/profile evidence resolves it",
        },
        "result": assessment,
        "limitations": [
            "FABDEM ≠ survey-grade DTM", "GBA height ≠ measured truth",
            "engineering assessment only; no safety certification or regulatory conclusion",
        ],
    }


def _residuals(p15):
    return [{"route_id": route.get("route_id"), "subsystem": item.get("subsystem"),
             "voxel_ids": deepcopy(item.get("confirmed_target_voxel_ids") or []),
             "segments": deepcopy(item.get("continuous_deficit_segments") or [])}
            for route in p15.get("routes") or [] for item in route.get("subsystems") or []
            if item.get("confirmed_target_voxel_ids")]


def _unknowns(p15):
    return [{"route_id": route.get("route_id"), "subsystem": item.get("subsystem"),
             "voxel_ids": deepcopy(item.get("unknown_voxel_ids") or [])}
            for route in p15.get("routes") or [] for item in route.get("subsystems") or []
            if item.get("unknown_voxel_ids")]


def _fingerprints(source):
    result = {}
    for key in ("required_cns_recommendation", "cns_gap_analysis_v2", "cns_corridor_assessment", "cns_corridor_gap_assessment", "cns_corridor_site_plan", "cns_plan_review", "confirmed_cns_plan"):
        value = source.get(key) or {}
        result[key] = {name: value.get(name) for name in ("algorithm_id", "algorithm_version", "input_fingerprint", "assessment_fingerprint", "baseline_fingerprint", "plan_id") if value.get(name) is not None}
    return result


def _provenance(source):
    def stage(name, entity):
        value=source.get(entity) or {}
        return {"stage":name,"entity":entity,"algorithm_id":value.get("algorithm_id"),
                "algorithm_version":value.get("algorithm_version"),
                "input_fingerprint":value.get("input_fingerprint"),
                "output_fingerprint":value.get("assessment_fingerprint") or value.get("baseline_fingerprint") or value.get("plan_id")}
    return {
        "semantics": "w3c_prov_inspired_not_full_prov_compliance",
        "derivation": [
            stage("P17", "required_cns_recommendation"),
            stage("P14", "cns_corridor_assessment"),
            stage("P15", "cns_corridor_gap_assessment"),
            stage("P16", "cns_corridor_site_plan"),
            stage("P18", "confirmed_cns_plan"),
            {"stage": "P19", "entity": "cns_planning_report", "algorithm_id": None,
             "algorithm_version": None, "input_fingerprint": None, "output_fingerprint": None},
        ],
    }
