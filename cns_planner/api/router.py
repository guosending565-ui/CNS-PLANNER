"""Stable /api route dispatch independent from BaseHTTPRequestHandler."""

from dataclasses import dataclass
from pathlib import Path

from ..gis.online_health import check_online_services
from ..safety.event_evaluator import evaluate_safety_events
from ..safety.fault_tree import evaluate_fault_tree
from ..safety.coupling import evaluate_coupled_events
from ..safety.service_state import evaluate_service_state
from .file_browser import browse


@dataclass
class Response:
    data: object
    content_type: str = "application/json; charset=utf-8"
    status: int = 200
    cache: bool = False


class ApiRouter:
    def __init__(self, context):
        self.context = context

    def get(self, path, query, headers):
        context, workflow, data = self.context, self.context.workflow, self.context.data
        if path == "/api/health": return Response({"service": "cns-map", "ready": True, "data_error": data.error})
        if path == "/api/state": return Response(context.qgis.call(data.metadata))
        if path == "/api/data-sources": return Response(context.qgis.call(lambda: data.metadata()["data_sources"]))
        if path == "/api/data-health": return Response(context.qgis.call(lambda: data.metadata()["data_health"]))
        if path == "/api/workflow": return Response(workflow.snapshot())
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
        static = self._static(path)
        if static: return static
        return Response({"error": "未找到"}, status=404)

    def post(self, path, payload):
        context, workflow, data = self.context, self.context.workflow, self.context.data
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
            "/api/layered-route-candidates/evaluate": lambda: workflow.evaluate_layered_route_candidate(payload),
            "/api/layered-route-candidates/evaluate-real": lambda: context.qgis.call(
                lambda: context.evaluate_layered_route_candidate(payload)
            ),
            "/api/layered-route-candidates/delete": lambda: workflow.delete_layered_route_candidate(
                payload.get("candidate_id")
            ),
            "/api/coverage-3d/evaluate": lambda: workflow.evaluate_coverage_3d(payload),
            "/api/cns-service-capability/evaluate": lambda: workflow.evaluate_cns_service_capability(),
            "/api/operational-timing": lambda: workflow.set_operational_timing(payload),
            "/api/service-timeline/evaluate": lambda: workflow.evaluate_service_timeline(payload),
            "/api/protection-envelope/evaluate": lambda: workflow.evaluate_protection_envelope(payload),
            "/api/encounter-3d/evaluate": lambda: workflow.evaluate_encounter_3d(payload),
        }
        if path in resource_actions:
            return Response(resource_actions[path]())
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
                workflow.set_workspace(payload.get("bbox"), health, payload.get("grid_level"))
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
            return Response(actions[action]())
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
                "reference_landing_sites",
                "reference_routes",
            )
        }
        if path == "/api/data-sources/validate":
            from ..gis.map_data import MapData
            return Response(context.qgis.call(lambda: MapData.validate_candidate(clean, default_config=context.default_config, token=context.token)))
        return Response(context.qgis.call(lambda: context.replace_sources(clean)))

    def _save_workflow(self):
        self.context.workflow.save()
        return self.context.workflow.snapshot()

    def _static(self, path):
        relative = "index.html" if path == "/" else path.lstrip("/")
        target = (self.context.static / relative).resolve()
        static_root = self.context.static.resolve()
        if static_root not in target.parents or not target.is_file():
            return None
        mime = "text/html; charset=utf-8" if target.suffix == ".html" else "text/css; charset=utf-8" if target.suffix == ".css" else "text/javascript; charset=utf-8" if target.suffix == ".js" else "application/octet-stream"
        return Response(target.read_bytes(), mime)
