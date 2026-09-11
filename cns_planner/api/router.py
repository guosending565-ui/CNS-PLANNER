"""Stable /api route dispatch independent from BaseHTTPRequestHandler."""

from dataclasses import dataclass
from pathlib import Path

from ..gis.map_data import MapData
from ..gis.online_health import check_online_services
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
        if path == "/api/required-cns": return Response(workflow.required_cns_snapshot())
        if path == "/api/existing-cns": return Response(workflow.existing_cns_snapshot())
        if path == "/api/candidate-sites": return Response(workflow.candidate_sites_snapshot())
        if path == "/api/cns-gaps": return Response(workflow.cns_gap_snapshot())
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
        if path == "/api/project/save-as": return Response(context.qgis.call(lambda: context.save_project_as(payload.get("project_dir"))))
        if path == "/api/project/open": return Response(context.qgis.call(lambda: context.open_project(payload.get("project_dir"))))
        resource_actions = {
            "/api/aircraft-profiles/select": lambda: workflow.select_aircraft_profile(payload.get("aircraft_id")),
            "/api/aircraft-profiles/import": lambda: workflow.import_aircraft_catalog(payload.get("path")),
            "/api/device-catalog/import": lambda: workflow.import_device_catalog(payload.get("path")),
            "/api/required-cns": lambda: workflow.set_required_cns(payload),
            "/api/existing-cns/import": lambda: workflow.import_existing_cns(payload),
            "/api/candidate-sites/import": lambda: workflow.import_candidate_sites(payload),
            "/api/candidate-sites/from-existing": workflow.candidate_sites_from_existing,
            "/api/cns-gaps/analyze": workflow.analyze_cns_gaps,
        }
        if path in resource_actions:
            return Response(resource_actions[path]())
        if path.startswith("/api/workflow/"):
            action = path.rsplit("/", 1)[-1]
            if action == "project": return Response(workflow.set_project(payload))
            if action == "workspace":
                health = context.qgis.call(lambda: data.workspace_health(payload.get("bbox")))
                workflow.set_workspace(payload.get("bbox"), health)
                results = context.qgis.call(lambda: data.grid_attributes(workflow.grid_snapshot()))
                return Response(workflow.apply_grid_attributes(results))
            actions = {
                "workspace-clear": lambda: workflow.clear_workspace(),
                "traffic-simulate": lambda: workflow.run_traffic_simulation(payload),
                "node": lambda: workflow.add_node(payload.get("coordinate", []), payload.get("name")),
                "node-delete": lambda: workflow.delete_node(payload.get("node_id")),
                "scenario": lambda: workflow.generate_scenario(payload.get("direction", "both")),
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
        clean = {key: payload.get(key, "") for key in ("basemap", "population", "terrain")}
        if path == "/api/data-sources/validate":
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
