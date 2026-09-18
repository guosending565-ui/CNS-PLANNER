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
                    "encounter_3d_summary": _encounter_summary(source.get("encounter_3d_assessment") or {}),
                },
                "v3_validated_route_provenance": _v3_provenance(source),
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


def _v3_provenance(source):
    """The V3-C/V3-D chain, kept strictly separate from the CNS requirement verdict."""

    adoption_collection = source.get("v3_operational_adoptions") or {}
    bundles = (source.get("v3_cns_assessment_bundle") or {}).get("items") or []
    bundle_by_adoption = {
        str(item.get("adoption_id")): item for item in bundles if isinstance(item, dict)
    }
    routes = []
    for adoption in adoption_collection.get("items") or []:
        if not isinstance(adoption, dict):
            continue
        provenance = adoption.get("route_provenance") or {}
        metrics = adoption.get("path_metrics") or {}
        compatibility = adoption.get("compatibility") or {}
        bundle = bundle_by_adoption.get(str(adoption.get("adoption_id"))) or {}
        stage_statuses = {
            name: ((bundle.get("stage_results") or {}).get(name) or {}).get("status")
            for name in ("P7", "P8", "P9", "P10")
        }
        routes.append({
            "route_id": adoption.get("route_id"),
            "adoption_id": adoption.get("adoption_id"),
            "adoption_status": adoption.get("status"),
            "current_applicability": adoption.get("current_applicability"),
            "applied_at": adoption.get("applied_at"),
            "evidence_source": adoption.get("evidence_source"),
            "validated_route": {
                "validation_id": provenance.get("validation_id"),
                "validation_fingerprint": provenance.get("validation_fingerprint"),
                "refinement_id": provenance.get("refinement_id"),
                "refinement_fingerprint": provenance.get("refinement_fingerprint"),
                "curve_chord_error_m": provenance.get("curve_chord_error_m"),
                "horizontal_crs": provenance.get("horizontal_crs"),
                "crs_transform": provenance.get("crs_transform"),
            },
            "representation": {
                "horizontal": provenance.get("horizontal_representation"),
                "vertical": provenance.get("vertical_representation"),
                "vertical_reference": provenance.get("vertical_reference"),
                "two_dimensional_path_only": True,
                "egm2008_in_geojson_third_coordinate": False,
                "v3_metric_length_m": metrics.get("v3_metric_length_m"),
                "legacy_geodesic_length_m": metrics.get("legacy_geodesic_length_m"),
                "length_delta_m": metrics.get("length_delta_m"),
                "distance_basis": metrics.get("distance_basis"),
                "profile_locked": bool((adoption.get("profile") or {}).get("locked_by_adoption")),
                "path_and_profile_share_vertex_order": compatibility.get(
                    "path_and_profile_share_vertex_order"
                ),
                "simplification_applied": compatibility.get("simplification_applied"),
            },
            "cns_assessment": {
                "bundle_id": bundle.get("bundle_id"),
                "assessment_status": bundle.get("assessment_status") or "not_started",
                "requirement_verdict": bundle.get("requirement_verdict") or "unknown",
                "stage_statuses": stage_statuses,
                "blocking_reasons": bundle.get("blocking_reasons") or [],
                "computed_at": bundle.get("computed_at"),
            },
        })
    return {
        "stage_chain": [
            "V3-A strategic", "V3-B refinement", "V3-C continuous validation",
            "V3-D operational adoption",
        ],
        "adoption_count": len(routes),
        "routes": routes,
        "semantics": {
            "validated_route_is_not_operational_route_until_adopted": True,
            "cns_excluded_from_v3_search_cost": True,
            "route_planning_then_cns_assessment_is_serial": True,
            "route_safety_is_not_cns_compliance": True,
            "assessment_completeness_is_not_requirement_verdict": True,
            "cns_gap_never_rewrites_route_validation": True,
            "horizontal_representation": "two_dimensional_lon_lat_only",
            "vertical_representation": "locked_route_altitude_profile_egm2008_orthometric",
        },
    }


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


def _encounter_summary(value):
    geometry, machine = value.get("geometry") or {}, value.get("state_machine") or {}
    return {
        "status": value.get("status"), "current_state": machine.get("current_state"),
        "engineering_predicted_conflict": value.get("engineering_predicted_conflict"),
        "horizontal_cpa_m": geometry.get("horizontal_cpa_m"),
        "vertical_separation_at_cpa_m": geometry.get("vertical_separation_at_cpa_m"),
        "regulatory_well_clear": "not_evaluated",
        "scope": "engineering_simulation_only",
    }


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
