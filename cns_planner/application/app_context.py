"""Composition root for mutable runtime collaborators."""

from copy import deepcopy
from pathlib import Path
import secrets
import threading

from ..gis.map_data import DEFAULT_PATHS, MapData
from ..gis.qgis_runtime import QgisRuntime
from ..tile_cache import TileCache
from .project_directory_service import ProjectDirectoryService
from .workflow_service import WorkflowService
from ..gis.building_clearance_adapter import QgisBuildingClearanceAdapter
from ..gis.fine_environment_adapter import real_data_source_readiness
from ..gis.route_vertical_profile_adapter import FabdemRouteSampler


class RenderRequestTracker:
    def __init__(self):
        self.latest, self.lock = {}, threading.Lock()

    def record(self, query):
        client = query.get("client", [""])[0][:100]
        if client:
            with self.lock:
                if len(self.latest) > 100:
                    self.latest.clear()
                self.latest[client] = max(int(query.get("seq", ["0"])[0]), self.latest.get(client, 0))

    def obsolete(self, query):
        client, seq = query.get("client", [""])[0], int(query.get("seq", ["0"])[0])
        with self.lock:
            return bool(client) and seq < self.latest.get(client, 0)


class ApplicationContext:
    def __init__(self, root=None):
        self.root = Path(root or Path(__file__).resolve().parents[2])
        self.static = self.root / "cns_planner" / "web"
        self.default_config = self.root / "cns_planner" / "config" / "defaults.json"
        self.automatic_project_file = self.root / "projects" / "current_project.json"
        self.active_project_file = self.automatic_project_file
        self.token = secrets.token_urlsafe(24)
        self.qgis = QgisRuntime()
        self.tiles = TileCache()
        self.render_requests = RenderRequestTracker()
        self.workflow = WorkflowService(self.active_project_file, self.default_config)
        self.project_directories = ProjectDirectoryService(
            self.automatic_project_file, self.default_config, DEFAULT_PATHS, WorkflowService
        )
        self.data = MapData(
            settings_path=self.root / "projects" / "map_sources.json",
            defaults=DEFAULT_PATHS, default_config=self.default_config, token=self.token,
            workflow_provider=lambda: self.workflow.snapshot(),
            project_metadata_provider=lambda: self.project_directories.storage_metadata(self.active_project_file),
            stale_checker=self.render_requests.obsolete,
        )
        self.workflow.update_data_source_profiles({
            "population": self.data.raster_info.get("source_profile"),
            "terrain": self.data.terrain_info.get("source_profile"),
            "terrain_dtm": self.data.terrain_dtm_info.get("source_profile"),
        })
        self.workflow.register_source_paths(self.data.paths, self._source_details())
        self.workflow.configure_reference_sources(self.data.paths)
        self.configure_route_planner_v3_sources()

    def _source_details(self):
        details = {}
        for role, info in (getattr(self.data, "vector_info", {}) or {}).items():
            details[role] = {
                "schema": {"fields": sorted((info.get("fields") or {}).keys())},
                "feature_count": info.get("feature_count"), "extent": info.get("extent"),
                "declared_crs": info.get("crs"),
                "geometry_health": deepcopy(info.get("geometry_health") or {
                    "status": "not_fully_checked", "feature_count": info.get("feature_count"),
                    "null": None, "empty": None, "invalid": None, "unsupported": None,
                    "extent": info.get("extent"), "crs": info.get("crs"),
                    "repair_applied": False,
                }),
            }
        for role, info in (
            ("population", getattr(self.data, "raster_info", {}) or {}),
            ("terrain", getattr(self.data, "terrain_info", {}) or {}),
            ("terrain_dtm", getattr(self.data, "terrain_dtm_info", {}) or {}),
        ):
            details[role] = {
                "schema": {"width": info.get("width"), "height": info.get("height"),
                           "bands": info.get("bands"), "dtype": info.get("dtype")},
                "extent": info.get("extent"), "declared_crs": info.get("crs"),
                "geometry_health": {"status": "not_applicable", "feature_count": None,
                                    "null": 0, "empty": 0, "invalid": 0, "unsupported": 0},
            }
        return details

    def save_project_as(self, project_dir):
        workflow, target = self.project_directories.save_as(
            project_dir, self.workflow, self.active_project_file, self.data
        )
        self.workflow, self.active_project_file = workflow, target
        return self.data.metadata()

    def open_project(self, project_dir):
        workflow, target = self.project_directories.open(project_dir, self.data)
        workflow.configure_reference_sources(self.data.paths)
        self.workflow, self.active_project_file = workflow, target
        return self.data.metadata()

    def replace_sources(self, paths):
        previous = dict(self.data.paths)
        previous_signatures = {
            name: (getattr(self.data, "source_signatures", {}) or {}).get(name)
            for name in paths
        }
        self.data.load({**self.data.paths, **paths})
        self.workflow.register_source_paths(self.data.paths, self._source_details())
        self.workflow.configure_reference_sources(self.data.paths)
        self.workflow.update_data_source_profiles({
            "population": self.data.raster_info.get("source_profile"),
            "terrain": self.data.terrain_info.get("source_profile"),
            "terrain_dtm": self.data.terrain_dtm_info.get("source_profile"),
        })
        changed = {
            name for name in paths
            if previous.get(name) != self.data.paths.get(name)
            or previous_signatures.get(name) != self.data.source_signatures.get(name)
        }
        self.workflow.invalidate_grid_attributes(changed)
        if "basemap" in changed:
            self.workflow.invalidate("data")
        self.workflow.save()
        self.project_directories.persist_sources(self.active_project_file, self.data)
        return self.data.metadata()

    def evaluate_building_clearance(self):
        buildings = self.data.paths.get("buildings")
        terrain_dtm = self.data.paths.get("terrain_dtm")
        if not buildings or not terrain_dtm:
            raise ValueError("请先配置 GBA buildings 与 FABDEM terrain_dtm")
        adapter = QgisBuildingClearanceAdapter(buildings, terrain_dtm)
        return self.workflow.evaluate_building_clearance(adapter)

    def evaluate_route_vertical_profiles(self, payload=None):
        terrain_dtm = self.data.paths.get("terrain_dtm")
        if not terrain_dtm:
            raise ValueError("请先配置 FABDEM terrain_dtm")
        return self.workflow.evaluate_route_vertical_profiles(FabdemRouteSampler(terrain_dtm), payload)

    # ------------------------------------------------------------------ Route Planner V3

    def configure_route_planner_v3_sources(self):
        """Give the V3 service a *read-only* real-source readiness provider.

        The provider only reports configured paths and confirmed airspace evidence;
        it never opens a dataset, so rendering the readiness panel is side-effect
        free.
        """

        def readiness():
            eligibility = (
                ((self.workflow.state.get("grid_attributes") or {}).get("airspace") or {})
                .get("airspace_eligibility") or {}
            )
            policy = self.workflow.state.get("v3_planning_policy") or {}
            return real_data_source_readiness(
                self.data.paths, airspace_eligibility=eligibility,
                policy_confirmed=bool(policy.get("confirmed")),
            )

        self.workflow.route_planner_v3_service.source_readiness = readiness
        return readiness

    def evaluate_route_planner_v3_refinement(self, payload=None):
        """Build the GIS fine-environment adapter and run one V3-B refinement.

        Requires QGIS/GDAL plus configured, confirmed sources; V3-B never runs on a
        fabricated environment, so a missing configuration is a hard error here and
        a reviewed ``not_ready`` verdict on the service path.
        """

        payload = payload if isinstance(payload, dict) else {}
        source = str(payload.get("environment_source") or "canonical_synthetic")
        if source != "configured_real_sources":
            return self.workflow.evaluate_route_planner_v3_refinement(payload)
        adapter = self._fine_environment_adapter(payload)
        return self.workflow.evaluate_route_planner_v3_refinement_with_adapter(adapter, payload)

    def _fine_environment_adapter(self, payload):
        from ..gis.fine_environment_adapter import (
            ConfirmedAirspacePolygonSource, FabdemWindowTerrainSource,
            FineEnvironmentAdapter, QgisGpkgBuildingSource, QgisMetricTransform,
        )

        terrain_dtm = self.data.paths.get("terrain_dtm")
        buildings = self.data.paths.get("buildings")
        if not terrain_dtm or not buildings:
            raise ValueError("请先配置 FABDEM terrain_dtm 与 GBA buildings GeoPackage")
        state = self.workflow.state
        eligibility = (
            ((state.get("grid_attributes") or {}).get("airspace") or {})
            .get("airspace_eligibility") or {}
        )
        if eligibility.get("status") != "passed":
            raise ValueError("空域 eligibility 未就绪：V3-B 只消费 confirmed AirspacePolicy")
        horizontal_crs = str(payload.get("horizontal_crs") or "")
        if not horizontal_crs:
            raise ValueError("configured_real_sources 需要显式 horizontal_crs（局部米制 CRS）")
        fine_policy = self.workflow.route_planner_v3_service.fine_policy_snapshot()
        return FineEnvironmentAdapter(
            transform=QgisMetricTransform(horizontal_crs),
            terrain_source=FabdemWindowTerrainSource(terrain_dtm),
            building_source=QgisGpkgBuildingSource(buildings),
            airspace_source=ConfirmedAirspacePolygonSource(eligibility),
            resolution_m=payload.get("refinement_cell_size_m") or fine_policy.get("resolution_m"),
            resolution_source=(
                payload.get("resolution_source") or fine_policy.get("resolution_source")
            ),
            max_stride_cells=(
                payload.get("max_stride_cells") or fine_policy.get("max_stride_cells") or 1
            ),
            risk_result=state.get("grid_risk") or {},
            source_resolution_m=(
                (state.get("data_source_profiles") or {}).get("population") or {}
            ).get("resolution"),
            lineage={
                "configured_via": "ApplicationContext.evaluate_route_planner_v3_refinement",
                "project_grid_level": (state.get("grid") or {}).get("level"),
            },
        )
