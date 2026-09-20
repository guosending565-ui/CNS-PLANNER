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
from ..gis.layered_feasibility_adapter import layered_feasibility_source_status
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
        self.configure_route_planner_v3_adoption()
        self.configure_layered_route_planner_sources()
        self.configure_layered_route_validation_sources()
        self.configure_vertical_transition_validation_sources()

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

    # ------------------------------------------------- Layered Risk-Aware Route Planner V1

    def configure_layered_route_planner_sources(self):
        """Give the layered planner a read-only feasibility source-status provider.

        The provider only reports configured paths and source audit status; it never opens a
        dataset, so rendering the readiness panel is side-effect free.
        """

        def source_status():
            return layered_feasibility_source_status(
                self.workflow.state, self.data.paths.get("terrain_dtm"),
            )

        self.workflow.layered_route_planner_service.source_status = source_status
        return source_status

    def evaluate_layered_route_candidate(self, payload=None):
        """Run one Layered Route Planner V1 evaluation against the verified real sources.

        The coarse feasibility facts come from the existing verified FABDEM window sampler and
        the existing L8 building grid facts; without a configured FABDEM DTM this raises
        instead of fabricating an environment.
        """

        payload = payload if isinstance(payload, dict) else {}
        terrain_dtm = self.data.paths.get("terrain_dtm")
        if not terrain_dtm:
            raise ValueError("请先配置 verified FABDEM terrain_dtm")

        def evaluate():
            from ..gis.fine_environment_adapter import FabdemWindowTerrainSource
            from ..gis.layered_feasibility_adapter import LayeredFeasibilityAdapter

            adapter = LayeredFeasibilityAdapter(FabdemWindowTerrainSource(terrain_dtm))
            return self.workflow.evaluate_layered_route_candidate(payload, adapter=adapter)

        return self.qgis.call(evaluate)

    # ------------------------------------------------------------------ Route Planner V3

    def configure_route_planner_v3_sources(self):
        """Give the V3 service a *read-only* real-source readiness provider.

        The provider only reports configured paths and confirmed planning policy;
        it never opens a dataset, so rendering the readiness panel is side-effect
        free.
        """

        def readiness():
            policy = self.workflow.state.get("v3_planning_policy") or {}
            return real_data_source_readiness(
                self.data.paths, policy_confirmed=bool(policy.get("confirmed")),
                source_audits=self.workflow.state.get("source_audits") or {},
                terrain_profile=(self.workflow.state.get("data_source_profiles") or {}).get("terrain_dtm") or {},
            )

        self.workflow.route_planner_v3_service.source_readiness = readiness
        return readiness

    def evaluate_route_planner_v3(self, payload=None):
        """Run V3-A; configured real sources are opened only on the QGIS thread."""

        payload = payload if isinstance(payload, dict) else {}
        if str(payload.get("environment_source") or "canonical_synthetic") != "configured_real_sources":
            return self.workflow.evaluate_route_planner_v3(payload)

        def evaluate_real():
            from ..gis.fine_environment_adapter import FabdemWindowTerrainSource
            from ..gis.v3_environment_adapter import V3RealEnvironmentAdapter

            terrain_dtm = self.data.paths.get("terrain_dtm")
            if not terrain_dtm:
                raise ValueError("请先配置 verified FABDEM terrain_dtm")
            adapter = V3RealEnvironmentAdapter(FabdemWindowTerrainSource(terrain_dtm))
            return self.workflow.route_planner_v3_service.evaluate(payload, adapter=adapter)

        return self.qgis.call(evaluate_real)

    def configure_route_planner_v3_adoption(self):
        """Give the V3-D adoption service a real metric → OGC:CRS84 CRS transform.

        The resolver builds a QGIS-backed transform for the recorded projected CRS on
        demand.  It is deliberately a *resolver* rather than a default transform: a CRS
        the local runtime cannot honour leaves the publish gate blocked instead of
        silently publishing unconverted coordinates.
        """

        from ..gis.fine_environment_adapter import QgisMetricTransform

        def resolver(horizontal_crs):
            return QgisMetricTransform(str(horizontal_crs))

        self.workflow.v3_operational_adoption_service.transform_resolver = resolver
        return resolver

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

    def evaluate_route_planner_v3_continuous_validation(self, payload=None):
        """Build the V3-C evidence adapter and run one continuous validation.

        Requires QGIS/GDAL plus configured, confirmed sources; V3-C never validates
        against fabricated evidence, so a missing configuration is a hard error here
        and a reviewed ``unresolved``/``not_ready`` verdict on the service path.
        """

        payload = payload if isinstance(payload, dict) else {}
        source = str(payload.get("evidence_source") or "canonical_synthetic")
        if source != "configured_real_sources":
            return self.workflow.evaluate_route_planner_v3_continuous_validation(payload)
        adapter = self._continuous_evidence_adapter(payload)
        return self.workflow.evaluate_route_planner_v3_continuous_validation_with_evidence(
            adapter, payload,
        )

    def _continuous_evidence_adapter(self, payload):
        """Canonical V3-C ``domain_evidence`` from the real GIS sources.

        The returned callable receives the realized route (in the V3-B local metric
        frame) and returns the metric-space confirmed policy evidence, the **native**
        FABDEM pixel window and the route-corridor building footprints.  It reads
        only; nothing is resampled, filled or made valid.
        """

        from ..gis.fine_environment_adapter import (
            NativeTerrainWindowSource, QgisMetricTransform, RouteCorridorBuildingSource,
        )
        from ..route_planner_v3.continuous_raster_window import resolve_native_pixel_intervals

        terrain_dtm = self.data.paths.get("terrain_dtm")
        buildings = self.data.paths.get("buildings")
        if not terrain_dtm or not buildings:
            raise ValueError("请先配置 FABDEM terrain_dtm 与 GBA buildings GeoPackage")
        fine_policy = self.workflow.route_planner_v3_service.fine_policy_snapshot()
        crs = str(
            payload.get("horizontal_crs") or fine_policy.get("horizontal_crs") or ""
        )
        if not crs:
            raise ValueError("configured_real_sources 需要显式 horizontal_crs（局部米制 CRS）")
        transform = QgisMetricTransform(crs)
        terrain = NativeTerrainWindowSource(terrain_dtm)
        corridor = RouteCorridorBuildingSource(buildings, crs_authority=crs)
        adapter_id = "v3c_real_source_evidence_adapter"

        def build_evidence(*, refinement, policy, planning_policy, realization, payload=None):
            route = realization.get("route") or {}
            line = [
                [float(point[0]), float(point[1])]
                for point in (route.get("horizontal_geometry") or {}).get("linearized", {}).get(
                    "linestring_metric",
                ) or []
            ]
            curve_error = policy.get("curve_chord_error_m")
            terrain_evidence = terrain.native_window(
                transform=transform, metric_line=line,
                envelope_radius_m=curve_error or 0.0, spacing_m=curve_error,
            )
            terrain_evidence["pixels"] = resolve_native_pixel_intervals(
                route, terrain_evidence.get("pixels") or [],
                curve_chord_error_m=curve_error or 0.0,
            )
            terrain_evidence["available"] = True if terrain_evidence.get("available") else False
            building_evidence = corridor.query_route(
                route, transform=transform,
                horizontal_clearance_m=policy.get("building_horizontal_clearance_m") or 0.0,
                curve_error_m=curve_error or 0.0,
                terrain_source=terrain,
            )
            if building_evidence.get("available"):
                for building in building_evidence.get("buildings") or []:
                    building["source"] = building.get("source") or "GBA"
                    ring = building.get("ring_metric") or []
                    if ring:
                        building["ground_elevation_max_egm2008_m"] = terrain.sample_footprint_ground(
                            ring, transform=transform,
                        )
            sample_count = len(terrain_evidence.get("pixels") or []) + len(
                building_evidence.get("buildings") or []
            )
            return {
                "adapter_id": adapter_id,
                "source_type": "configured_real_sources",
                "to_geographic": transform.to_geographic,
                "sample_count": sample_count,
                "airspace": {
                    "available": False, "status": "not_applicable",
                    "applicability": "display_only",
                    "reason": "display_only_airspace_not_used_for_route_constraints",
                },
                "terrain": terrain_evidence,
                "buildings": building_evidence,
                "sources": {
                    "terrain_dtm": terrain.describe(),
                    "buildings": corridor.describe(),
                    "airspace": {"status": "not_applicable", "applicability": "display_only"},
                    "metric_frame": transform.describe(),
                },
            }

        return build_evidence

    def configure_layered_route_validation_sources(self):
        """Bind the production native FABDEM/real-footprint evidence adapter."""

        def adapter(**kwargs):
            return self._layered_route_validation_evidence(**kwargs)

        self.workflow.layered_route_validation_service.evidence_adapter = adapter
        return adapter

    def evaluate_layered_route_validation(self, payload=None):
        """Run production candidate validation on the QGIS thread."""

        return self.workflow.evaluate_layered_route_validation(payload)

    def _layered_route_validation_evidence(
        self, *, candidate, path, altitude_layer, nominal_altitude_m, policy, payload,
    ):
        """Build source-native evidence for the unchanged candidate centreline."""

        from ..gis.fine_environment_adapter import (
            NativeTerrainWindowSource, QgisMetricTransform, RouteCorridorBuildingSource,
        )
        from ..route_planner_v3.continuous_raster_window import resolve_native_pixel_intervals

        terrain_path = self.data.paths.get("terrain_dtm")
        building_path = self.data.paths.get("buildings")
        if not terrain_path or not building_path:
            raise ValueError("请先配置 verified FABDEM terrain_dtm 与 buildings GeoPackage")
        horizontal_crs = str((payload or {}).get("horizontal_crs") or "").strip()
        if not horizontal_crs:
            raise ValueError("production validation 需要显式 horizontal_crs（米制 CRS）")
        transform = QgisMetricTransform(horizontal_crs)
        metric_path = []
        for point in path or []:
            converted = transform.to_metric([float(point[0]), float(point[1])])
            if converted is None:
                raise ValueError("candidate CRS84 path 无法投影到显式 metric CRS")
            metric_path.append([float(converted[0]), float(converted[1])])
        route = {
            "horizontal_geometry": {"linearized": {
                "linestring_metric": metric_path, "curve_chord_error_m": 0.0,
            }},
        }
        terrain = NativeTerrainWindowSource(terrain_path)
        buildings = RouteCorridorBuildingSource(
            building_path, crs_authority=horizontal_crs,
        )
        terrain_evidence = terrain.native_window(
            transform=transform, metric_line=metric_path,
            envelope_radius_m=0.0, spacing_m=None,
        )
        terrain_evidence["pixels"] = resolve_native_pixel_intervals(
            route, terrain_evidence.get("pixels") or [], curve_chord_error_m=0.0,
        )
        building_evidence = buildings.query_route(
            route, transform=transform,
            horizontal_clearance_m=policy["building_horizontal_clearance_m"],
            curve_error_m=0.0, terrain_source=terrain,
        )
        return {
            "adapter_id": "layered_candidate_real_source_validation_adapter_v1",
            "source_type": "configured_real_sources",
            "metric_path": metric_path, "metric_crs": horizontal_crs,
            "to_geographic": transform.to_geographic,
            "terrain": terrain_evidence, "buildings": building_evidence,
            "sample_count": len(terrain_evidence.get("pixels") or []) + len(
                building_evidence.get("buildings") or []
            ),
            "sources": {
                "terrain_dtm": terrain.describe(), "buildings": buildings.describe(),
                "metric_frame": transform.describe(),
            },
            "airspace": {
                "status": "not_applicable", "applicability": "display_only",
                "used_in_validation": False,
            },
        }

    # --------------------------------------------------- Vertical Transition Validation V1

    def configure_vertical_transition_validation_sources(self):
        """Bind the production native FABDEM/real-footprint evidence adapter.

        The adapter receives one **already truncated** metric transition polyline (the
        *original* operational route polyline cut by route distance, never a straight chord)
        and returns the native pixel window plus the corridor building footprints for that
        phase only.  The building query uses ``horizontal_clearance_m = 0``: it is a pure
        **geometry intersection** test, never an engineering separation minimum.
        """

        def adapter(**kwargs):
            return self._vertical_transition_evidence(**kwargs)

        cache = {}

        def to_metric(point, *, crs=None):
            authority = str(crs or adapter.horizontal_crs or "").strip()
            if not authority:
                return None
            transform = cache.get(authority)
            if transform is None:
                from ..gis.fine_environment_adapter import QgisMetricTransform

                transform = QgisMetricTransform(authority)
                cache[authority] = transform
            return transform.to_metric(point)

        # The service projects the *original* route polyline with the same transform the
        # raster/building queries use, so geometry and evidence share one metric frame.
        adapter.to_metric = to_metric
        adapter.horizontal_crs = None
        self.workflow.vertical_transition_validation_service.evidence_adapter = adapter
        return adapter

    def evaluate_vertical_transition_validation(self, payload=None):
        """Run the climb/descent geometry validation on the QGIS thread.

        The evidence adapter opens the native FABDEM window and the buildings GeoPackage, so
        the whole evaluation runs on the QGIS thread (same shape as the production cruise
        validation entry point).  No automatic evaluation exists.
        """

        return self.qgis.call(
            lambda: self.workflow.evaluate_vertical_transition_validation(payload)
        )

    def _vertical_transition_evidence(
        self, *, phase, phase_id, route_id, profile, metric_line, metric_route,
        horizontal_crs, payload,
    ):
        """Source-native evidence for one transition phase (read-only, never resampled)."""

        from ..gis.fine_environment_adapter import (
            NativeTerrainWindowSource, QgisMetricTransform, RouteCorridorBuildingSource,
        )
        from ..route_planner_v3.continuous_raster_window import resolve_native_pixel_intervals

        terrain_path = self.data.paths.get("terrain_dtm")
        building_path = self.data.paths.get("buildings")
        if not terrain_path or not building_path:
            raise ValueError("请先配置 verified FABDEM terrain_dtm 与 buildings GeoPackage")
        crs = str(horizontal_crs or "").strip()
        if not crs:
            raise ValueError("transition validation 需要显式 horizontal_crs（米制 CRS）")
        transform = QgisMetricTransform(crs)
        line = [
            [float(point[0]), float(point[1])]
            for point in metric_line or []
            if isinstance(point, (list, tuple)) and len(point) >= 2
        ]
        if len(line) < 2:
            raise ValueError("transition phase 的 metric 顶点不足（需要 >= 2）")
        terrain = NativeTerrainWindowSource(terrain_path)
        corridor = RouteCorridorBuildingSource(building_path, crs_authority=crs)
        # The truncated polyline *is* the validation geometry: no curve error envelope.
        terrain_evidence = terrain.native_window(
            transform=transform, metric_line=line, envelope_radius_m=0.0, spacing_m=None,
        )
        terrain_evidence["pixels"] = resolve_native_pixel_intervals(
            metric_route, terrain_evidence.get("pixels") or [], curve_chord_error_m=0.0,
        )
        building_evidence = corridor.query_route(
            metric_route, transform=transform,
            horizontal_clearance_m=0.0,
            curve_error_m=0.0, terrain_source=terrain,
        )
        if building_evidence.get("available"):
            for building in building_evidence.get("buildings") or []:
                building["source"] = building.get("source") or "GBA"
                ring = building.get("ring_metric") or []
                if ring:
                    building["ground_elevation_max_egm2008_m"] = terrain.sample_footprint_ground(
                        ring, transform=transform,
                    )
        return {
            "adapter_id": "vertical_transition_real_source_evidence_adapter_v1",
            "source_type": "configured_real_sources",
            "phase_id": phase_id,
            "metric_crs": crs,
            "to_geographic": transform.to_geographic,
            "terrain": terrain_evidence,
            "buildings": building_evidence,
            "sample_count": len(terrain_evidence.get("pixels") or []) + len(
                building_evidence.get("buildings") or []
            ),
            "sources": {
                "terrain_dtm": terrain.describe(), "buildings": corridor.describe(),
                "metric_frame": transform.describe(),
            },
            "clearance_usage": {
                "terrain_clearance_m": 0.0,
                "building_horizontal_clearance_m": 0.0,
                "curve_chord_error_m": 0.0,
                "semantics": "geometry_intersection_threshold_not_an_engineering_clearance",
            },
            "airspace": {
                "status": "not_applicable", "applicability": "display_only",
                "used_in_validation": False,
            },
        }

    def _fine_environment_adapter(self, payload):
        from ..gis.fine_environment_adapter import (
            FabdemWindowTerrainSource, FineEnvironmentAdapter,
            QgisGpkgBuildingSource, QgisMetricTransform,
        )

        terrain_dtm = self.data.paths.get("terrain_dtm")
        buildings = self.data.paths.get("buildings")
        if not terrain_dtm or not buildings:
            raise ValueError("请先配置 FABDEM terrain_dtm 与 GBA buildings GeoPackage")
        state = self.workflow.state
        horizontal_crs = str(payload.get("horizontal_crs") or "")
        if not horizontal_crs:
            raise ValueError("configured_real_sources 需要显式 horizontal_crs（局部米制 CRS）")
        fine_policy = self.workflow.route_planner_v3_service.fine_policy_snapshot()
        return FineEnvironmentAdapter(
            transform=QgisMetricTransform(horizontal_crs),
            terrain_source=FabdemWindowTerrainSource(terrain_dtm),
            building_source=QgisGpkgBuildingSource(buildings),
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
