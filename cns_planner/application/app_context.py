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
from ..tasks.service import HeavyTaskService
from ..gis.building_clearance_adapter import QgisBuildingClearanceAdapter
from ..gis.fine_environment_adapter import real_data_source_readiness
from ..gis.layered_feasibility_adapter import layered_feasibility_source_status
from ..gis.route_vertical_profile_adapter import FabdemRouteSampler
from ..process_identity import build_identity


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
        self.mutation_lock = threading.RLock()
        self.workflow = WorkflowService(self.active_project_file, self.default_config)
        # Phase4-B6X：把 mutation_lock 交给当前 workflow（heavy task 的短 publish
        # 阶段必须与 HTTP 写路径共用同一把锁；worker 进程全程不持有它）。
        self.workflow.mutation_lock = self.mutation_lock
        self.heavy_tasks = HeavyTaskService(self.workflow, self.active_project_file)
        self.workflow.heavy_tasks = self.heavy_tasks
        self.heavy_tasks.start()
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
        # 启动即把 runtime provider/adapter 绑定到当前 workflow（与 open/save-as 同一条路径）。
        self._activate_workflow(self.workflow, self.active_project_file)

    @property
    def health_identity(self):
        """本后端进程的最小运行身份（BUG-STARTUP-001）。

        ``/api/health`` 用它向启动器声明"这个服务属于哪个项目"，启动器据此
        拒绝跨项目复用，而不是仅凭 service 名就接管 8765 上的进程。
        """
        return build_identity(self.root)

    def _source_details(self):
        details = {}
        for role, info in (getattr(self.data, "vector_info", {}) or {}).items():
            # ``fields`` 有两种合法形态：既有 GeoPackage 检查返回 {名称: 类型}，
            # 通用矢量发现（.shp/.geojson/由 qgz 解析出的图层）返回名称列表。
            fields = info.get("fields") or {}
            field_names = sorted(fields.keys()) if isinstance(fields, dict) else sorted(str(name) for name in fields)
            details[role] = {
                "schema": {"fields": field_names},
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

    def _activate_workflow(self, workflow, target):
        """切换当前 ``WorkflowService``，并把全部 runtime 绑定重新指向它。

        ``open_project`` / ``save_project_as`` 会用一个**新的** ``WorkflowService`` 替换
        当前实例。runtime 级 provider/adapter 挂在**具体实例**的 service 上（不是模块级、
        也不是随 ``self.workflow`` 动态查找的），所以每次切换都必须重新绑定：

        1. 先把 ``self.workflow`` / ``self.active_project_file`` 指向新对象——下面两步都
           通过 ``self.workflow`` 取得目标实例；
        2. 再 ``configure_reference_sources`` 与 ``_bind_runtime_services()``。
        """

        self.workflow, self.active_project_file = workflow, target
        # Phase4-B6X：项目切换后 heavy task store 与 mutation_lock 都要重新绑定到
        # 新的 workflow 实例上，否则任务会发布到上一个项目的 state 里。
        # 锁按需补建：``_activate_workflow`` 在常规构造路径之外也可能被调用
        # （例如只替换 workflow 的轻量上下文），此时不能因为缺少锁属性而失败。
        lock = getattr(self, "mutation_lock", None)
        if lock is None:
            lock = self.mutation_lock = threading.RLock()
        self.workflow.mutation_lock = lock
        heavy_tasks = getattr(self, "heavy_tasks", None)
        if heavy_tasks is not None:
            heavy_tasks.bind_project(target)
            self.workflow.heavy_tasks = heavy_tasks
        self.workflow.configure_reference_sources(self.data.paths)
        self._bind_runtime_services()
        return workflow

    def _bind_runtime_services(self):
        """把全部 runtime 级 GIS provider/adapter 统一绑定到当前 ``self.workflow``。

        只做绑定，不做业务判断：每个 provider 都在被调用时才读取真实来源（source audit /
        ``grid_attributes``），因此这里既不会打开数据集，也不会把任何 available 硬设为 true。
        """

        self.configure_route_planner_v3_sources()
        self.configure_route_planner_v3_adoption()
        self.configure_layered_route_planner_sources()
        self.configure_layered_route_validation_sources()
        self.configure_vertical_transition_validation_sources()
        self.configure_radar_surveillance_layout_sources()
        return self.workflow

    def save_project_as(self, project_dir):
        workflow, target = self.project_directories.save_as(
            project_dir, self.workflow, self.active_project_file, self.data
        )
        self._activate_workflow(workflow, target)
        return self.data.metadata()

    def open_project(self, project_dir):
        workflow, target = self.project_directories.open(project_dir, self.data)
        self._activate_workflow(workflow, target)
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
        adapter = QgisBuildingClearanceAdapter(
            buildings, terrain_dtm,
            geographic_bounds=((self.workflow.session.state.get("workspace") or {}).get("bbox")),
        )
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

    # ------------------------------------------------- Towers Operational Integration V2

    def evaluate_tower_obstacle_profiles(self, payload=None):
        """派生铁塔障碍物高度事实 + 共塔宿主候选（仅 QGIS 线程读取真实源）。

        地形必须是已确认垂直基准的 FABDEM DTM；建筑高度直接在真实建筑足迹上按塔坐标
        做包含查询（层级无关的精确几何，不经过 L7/L8 建筑网格）。任何一步不可用都让
        对应塔保持 ``unresolved``，绝不补 0。
        """

        from ..gis.fine_environment_adapter import FabdemWindowTerrainSource
        from ..gis.tower_obstacle_adapter import build_tower_obstacle_facts

        terrain_dtm = self.data.paths.get("terrain_dtm")
        if not terrain_dtm:
            raise ValueError("请先配置 verified FABDEM terrain_dtm")
        terrain_source = FabdemWindowTerrainSource(terrain_dtm)
        building_source = self.data.vector_role_source("buildings")

        def facts_provider(towers, state):
            return build_tower_obstacle_facts(
                towers, terrain_source=terrain_source, building_source=building_source,
            )

        return self.workflow.evaluate_tower_obstacle_profiles(
            payload, facts_provider=facts_provider,
        )

    def evaluate_layered_route_candidate(self, payload=None):
        """Run one Layered Route Planner V1 evaluation against the verified real sources.

        The coarse feasibility facts come from the existing verified FABDEM window sampler and
        the existing L8 building grid facts; without a configured FABDEM DTM this raises
        instead of fabricating an environment.

        The API layer already marshals this call onto the QGIS owner thread
        (``api/router.py`` -> ``context.qgis.call(...)``).  Wrapping it a *second* time here
        made the QGIS thread queue a task to itself and then block on ``future.result()``:
        the request never returned and the whole workbench appeared hung.  This mirrors the
        single-level shape the other ``*-real`` entry points already use.
        """

        payload = payload if isinstance(payload, dict) else {}
        terrain_dtm = self.data.paths.get("terrain_dtm")
        if not terrain_dtm:
            raise ValueError("请先配置 verified FABDEM terrain_dtm")

        from ..gis.fine_environment_adapter import FabdemWindowTerrainSource
        from ..gis.layered_feasibility_adapter import LayeredFeasibilityAdapter

        adapter = LayeredFeasibilityAdapter(FabdemWindowTerrainSource(terrain_dtm))
        return self.workflow.evaluate_layered_route_candidate(payload, adapter=adapter)

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
        from ..gis.planning_constraint_field_adapter import restricted_area_continuous_evidence

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
                    rings = building.get("ring_parts_metric") or []
                    if rings:
                        values = terrain.sample_footprint_ground_parts(
                            rings, transform=transform,
                        )
                        building["ground_elevation_by_part_egm2008_m"] = values
                        building["ground_elevation_max_egm2008_m"] = (
                            max(values) if values and all(value is not None for value in values)
                            else None
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
        tower_collection = self.workflow.session.state.get("tower_obstacle_profiles") or {}
        tower_items = []
        for profile in (tower_collection.get("items") or {}).values():
            if not isinstance(profile, dict):
                continue
            point = transform.to_metric([profile.get("longitude"), profile.get("latitude")])
            if point is None:
                continue
            tower_items.append({**deepcopy(profile), "point_metric": [float(point[0]), float(point[1])]})
        tower_evidence = {
            "status": "passed" if tower_collection.get("status") == "passed" else "unresolved",
            "applicability": "applicable",
            "towers": tower_items,
            "source": deepcopy(tower_collection.get("source")),
        }
        raw_restricted = (payload or {}).get("restricted_areas")
        if raw_restricted is None:
            restricted_evidence = {
                "status": "unresolved", "applicability": "applicable", "areas": [],
                "source": None,
            }
        else:
            restricted_evidence = restricted_area_continuous_evidence(
                raw_restricted, transform=transform,
            )
        return {
            "adapter_id": "layered_candidate_real_source_validation_adapter_v1",
            "source_type": "configured_real_sources",
            "metric_path": metric_path, "metric_crs": horizontal_crs,
            "to_geographic": transform.to_geographic,
            "terrain": terrain_evidence, "buildings": building_evidence,
            "towers": tower_evidence, "restricted_areas": restricted_evidence,
            "sample_count": len(terrain_evidence.get("pixels") or []) + len(
                building_evidence.get("buildings") or []
            ) + len(tower_items) + len(restricted_evidence.get("areas") or []),
            "sources": {
                "terrain_dtm": terrain.describe(), "buildings": buildings.describe(),
                "towers": {
                    "collection_id": tower_collection.get("collection_id"),
                    "status": tower_collection.get("status"),
                    "count": tower_collection.get("count"),
                    "confirmed_count": tower_collection.get("confirmed_count"),
                },
                "restricted_areas": deepcopy(restricted_evidence.get("source")),
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

    # ------------------------------------------------- Radar Surveillance Layout V1

    def configure_radar_surveillance_layout_sources(self):
        """Bind the read-only real-source facts provider of the radar layout model.

        The provider only exposes *facts*: an explicit metric projector
        (``EPSG:32651``, the same frame the project already uses for terrain/building
        queries), per-point FABDEM DTM orthometric height, and an explicit **land mask**
        point classifier.  It never opens a dataset while merely rendering a panel:
        ``sample_terrain`` / ``classify_surface`` read the sources only when an explicit
        evaluate runs on the QGIS thread.

        **陆域掩膜独立于 DEM**：本 provider 不含任何"用 NoData 推断海洋"的路径；未配置
        陆域源时 ``classify_surface`` 返回 ``unknown``（fail-closed）。
        """

        from ..gis.radar_layout_adapter import (
            LandMaskSource, land_mask_hint, land_mask_readiness, sample_route_terrain,
        )

        transform_cache = {}

        def to_metric(point):
            from ..gis.fine_environment_adapter import QgisMetricTransform

            transform = transform_cache.get("EPSG:32651")
            if transform is None:
                transform = QgisMetricTransform("EPSG:32651")
                transform_cache["EPSG:32651"] = transform
            return transform.to_metric(point)

        def to_geographic(point):
            from ..gis.fine_environment_adapter import QgisMetricTransform

            transform = transform_cache.get("EPSG:32651")
            if transform is None:
                transform = QgisMetricTransform("EPSG:32651")
                transform_cache["EPSG:32651"] = transform
            return transform.to_geographic(point)

        land_mask_cache = {}

        def _land_policy():
            """陆域判定的显式工程参数（图层名 + 海岸不确定带）来自当前 policy。"""

            try:
                policy = self.workflow.radar_surveillance_layout_service.policy_snapshot()
            except Exception:
                policy = {}
            return (
                policy.get("land_mask_layer_name"),
                policy.get("coastal_uncertainty_buffer_m"),
            )

        def land_mask_source():
            path = self.data.paths.get("land_mask")
            layer_name, buffer_m = _land_policy()
            key = (str(path) if path else None, str(layer_name), str(buffer_m))
            if key not in land_mask_cache:
                land_mask_cache.clear()
                land_mask_cache[key] = (
                    LandMaskSource(
                        path, layer_name=layer_name, coastal_uncertainty_buffer_m=buffer_m,
                    )
                    if path else None
                )
            return land_mask_cache[key]

        def sample_terrain(points):
            from ..gis.radar_layout_adapter import route_terrain_source

            terrain_path = self.data.paths.get("terrain_dtm")
            source = route_terrain_source(terrain_path) if terrain_path else None
            return sample_route_terrain(points, terrain_source=source)

        def classify_surface(points):
            source = land_mask_source()
            if source is None:
                return ["unknown"] * len(points or [])
            return source.classify_many(points)

        def classify_surface_detailed(longitude, latitude):
            """V1.1：单点**独立**分类 + 需求语义（含 coastal_uncertain）。

            每个真实采样点都在自己的位置调用一次；绝不做最近邻继承。
            """

            source = land_mask_source()
            if source is None:
                return {
                    "surface_class": "unknown",
                    "effective_requirement_class": None,
                    "required_distinct_site_count": None,
                    "classification_confidence": "unknown",
                    "evidence": {"reason": "land_mask_not_configured"},
                }
            return source.classify_detailed(longitude, latitude)

        def land_mask_readiness_bundle():
            layer_name, buffer_m = _land_policy()
            return land_mask_readiness(
                self.workflow.state if hasattr(self.workflow, "state") else {},
                {"land_mask": self.data.paths.get("land_mask")},
                layer_name=layer_name,
                coastal_uncertainty_buffer_m=buffer_m,
            )

        policy_layer_name, policy_buffer_m = _land_policy()
        provider = {
            "adapter_id": "radar_surveillance_layout_real_source_facts_v1",
            "source_type": "configured_real_sources",
            "metric_crs": "EPSG:32651",
            "to_metric": to_metric,
            "to_geographic": to_geographic,
            "sample_terrain": sample_terrain,
            "classify_surface": classify_surface,
            "classify_surface_detailed": classify_surface_detailed,
            "land_mask": {
                "ok": land_mask_hint(
                    self.data.paths.get("land_mask"), layer_name=policy_layer_name,
                )["ok"],
                "readiness": land_mask_hint(
                    self.data.paths.get("land_mask"), layer_name=policy_layer_name,
                ),
            },
            "land_mask_readiness": land_mask_readiness_bundle,
            "land_mask_layer_name": policy_layer_name,
            "coastal_uncertainty_buffer_m": policy_buffer_m,
            "paths": {
                "terrain_dtm": self.data.paths.get("terrain_dtm"),
                "land_mask": self.data.paths.get("land_mask"),
            },
            "dem_nodata_used_to_infer_sea": False,
            "backend_hardcoded_mount_height": False,
            # V1.1：航路采样高度恒为 ALT-080 的 80 m，与地形采样无关。
            "terrain_used_for_route_sample_height": False,
        }
        self.workflow.radar_surveillance_layout_service.facts_provider = provider
        return provider

    def evaluate_radar_surveillance_layout(self, payload=None):
        """Run one explicit radar layout evaluation (reads FABDEM + land mask).

        The evaluation opens the FABDEM window and the land-mask polygons, so it runs on the
        QGIS thread — the same shape as the other real-source entry points.  There is **no**
        automatic evaluation and **no** automatic apply of the resulting proposal.
        """

        return self.qgis.call(
            lambda: self.workflow.evaluate_radar_surveillance_layout(payload)
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
                rings = building.get("ring_parts_metric") or []
                if rings:
                    values = terrain.sample_footprint_ground_parts(
                        rings, transform=transform,
                    )
                    building["ground_elevation_by_part_egm2008_m"] = values
                    building["ground_elevation_max_egm2008_m"] = (
                        max(values) if values and all(value is not None for value in values)
                        else None
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
            building_source=QgisGpkgBuildingSource(
                buildings, crs_authority=horizontal_crs,
            ),
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
