"""Facade over GIS loading, metadata, mapping and rendering capabilities."""

import json
from pathlib import Path
from urllib.parse import urlparse

from qgis.core import QgsCoordinateReferenceSystem

from ..data.health import build_health
from ..data.registry import build_registry
from ..domain.source_audit import source_manifest
from ..data.mapping.airspace import AirspaceGridService
from ..data.mapping.population import PopulationGridService
from ..data.mapping.terrain import TerrainGridService
from ..data.mapping.buildings import BuildingGridService
from .airspace_adapter import QgisAirspaceAdapter
from .building_footprint_aggregation import aggregate_footprint_facts
from .path_resolver import check_source_paths
from .qgis_project_layers import resolve_vector_layer_source
from .renderer import QgisMapRenderer
from .source_loader import QgisSourceLoader
from .workspace_health import workspace_health


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SETTINGS = ROOT / "projects" / "map_sources.json"
DEFAULT_CONFIG = ROOT / "cns_planner" / "config" / "defaults.json"
DEFAULT_PATHS = {
    "basemap": "D:/aaa2026project/UOM/全国适飞空域图_单省可更新.qgz",
    "population": "D:/aaa2026project/UOM/舟山/规划系统/chn_pop_2025_CN_100m_R2025A_v1.tif",
    "terrain": "D:/aaa2026project/UOM/舟山/规划系统/GLO30/output/Zhejiang_GLO30_mosaic_30m.tif",
    "terrain_dtm": "D:/aaa2026project/UOM/舟山/规划系统/FABDEM/processed/Zhoushan_FABDEM_DTM_30m.tif",
    "buildings": "D:/aaa2026project/UOM/舟山/规划系统/building/processed/zhoushan_buildings.gpkg",
    "building_grid": "D:/aaa2026project/UOM/舟山/规划系统/building/processed/zhoushan_building_grid_L8.gpkg",
    # 陆域掩膜（真实数据，只读来源）：浙江省级行政边界 Polygon/MultiPolygon GeoPackage，
    # 来源 QGIS 工程 ``D:\aaa2026project\UOM\舟山\规划系统\GLO30\11.qgz`` 的
    # ``zhejiang_boundary`` 图层，source CRS ``EPSG:4326``。
    #
    # 语义边界（不得夸大）：这是**工程化/简化的省域边界**，可以表达浙江陆块与舟山岛间海域，
    # 但**不是** 5 m 高精度海岸线；因此海岸附近用显式 ``coastal_uncertainty_buffer_m``
    # （工程假设，未确认）单独成类，绝不把它当成数据真实精度。
    #
    # 这里只是**本机默认建议值**：算法侧一律从 ``data_sources["land_mask"]`` 消费，
    # radar algorithm / adapter 里不出现任何绝对路径。
    "land_mask": "D:/aaa2026project/UOM/舟山/规划系统/GLO30/boundary/zhejiang_boundary.gpkg",
}


class MapData:
    def __init__(self, settings_path=DEFAULT_SETTINGS, defaults=None, default_config=DEFAULT_CONFIG,
                 token="", workflow_provider=None, project_metadata_provider=None, stale_checker=None):
        self.map_crs = QgsCoordinateReferenceSystem("EPSG:3857")
        self.wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        self.loader = QgisSourceLoader(self.map_crs, self.wgs84, settings_path)
        self.renderer = QgisMapRenderer(self.map_crs, stale_checker)
        self.default_config, self.token = Path(default_config), token
        self.workflow_provider = workflow_provider or (lambda: {})
        self.project_metadata_provider = project_metadata_provider or (lambda: {})
        self.revision, self.error, self.loaded = 0, "", None
        self.paths = self.loader.initial_paths(defaults or DEFAULT_PATHS)
        try:
            self.load(self.paths, persist=False)
        except Exception as exc:
            self.error = str(exc)

    def load(self, paths, persist=True):
        loaded = self.loader.load(paths, persist)
        self.loaded, self.paths = loaded, loaded.paths
        self.source_signatures = {
            name: self._source_signature(path) for name, path in self.paths.items()
        }
        # 每次加载都丢弃上一次的矢量角色解析结果：路径可能在磁盘上被替换/删除/移动，
        # 复用旧解析会把"上一个文件的图层"带进当前界面（缓存旧状态）。
        self._vector_role_cache = {}
        for name in ("project", "population", "terrain", "terrain_dtm", "raster_info", "terrain_info", "terrain_dtm_info", "vector_info", "local_layers", "layers", "bounds", "layer_boxes", "layer_boxes_wgs84", "population_bbox_wgs84", "terrain_bbox_wgs84", "terrain_dtm_bbox_wgs84", "hard_constraints", "online_sources"):
            setattr(self, name, getattr(loaded, name))
        self.revision += 1; self.error = ""; self.renderer.clear()

    def _resolve_vector_role(self, role):
        """把建筑类来源（.qgz / .gpkg / .shp / .geojson）解析成可读取的矢量图层。

        结果按当前 ``load()`` 缓存；``load()`` 会清空缓存，因此不会跨加载复用旧解析。
        """

        cache = getattr(self, "_vector_role_cache", None)
        if cache is None:
            cache = {}
            self._vector_role_cache = cache
        if role in cache:
            return cache[role]
        value = self.paths.get(role)
        if not value:
            record = {
                "ok": False, "status": "not_configured",
                "reason": "尚未配置", "value": None, "path": None, "layer_name": None,
            }
        else:
            record = resolve_vector_layer_source(value, role=role)
        cache[role] = record
        return record

    def vector_role_source(self, role):
        """公开的只读入口：建筑类来源（.qgz / .gpkg / .shp / .geojson）解析成了什么。"""

        return self._resolve_vector_role(role)

    def building_source_summary(self):
        """数据源中心用的只读摘要：建筑单体 / 建筑环境网格各自解析到了什么。"""

        summary = {}
        for role in ("buildings", "building_grid"):
            resolved = self._resolve_vector_role(role)
            summary[role] = {
                "configured_path": self.paths.get(role),
                "ok": bool(resolved.get("ok")),
                "status": resolved.get("status"),
                "reason": resolved.get("reason"),
                "source": resolved.get("source"),
                "resolved_path": resolved.get("path"),
                "layer_name": resolved.get("layer_name"),
                "format": resolved.get("format"),
                "layer_title": resolved.get("layer_title"),
                "selection_reason": resolved.get("selection_reason"),
            }
        return summary

    @staticmethod
    def _source_signature(path):
        try:
            stat = Path(path).stat()
            return str(Path(path).resolve()), stat.st_size, stat.st_mtime_ns
        except (OSError, TypeError):
            return str(path or ""), None, None

    def metadata(self):
        online = []
        for key, value in getattr(self, "online_sources", {}).items():
            item = {"id": key, **{name: current for name, current in value.items() if name != "template"}}
            if (urlparse(value["template"]).hostname or "").endswith(".tianditu.gov.cn"):
                item["browser_url"] = value["template"]
            online.append(item)
        base = {"paths": self.paths, "error": self.error, "revision": self.revision,
                "layers": getattr(self, "layers", []), "bounds": getattr(self, "bounds", None),
                "population": getattr(self, "raster_info", {}), "terrain": getattr(self, "terrain_info", {}),
                "terrain_dtm": getattr(self, "terrain_dtm_info", {}),
                "vector_sources": getattr(self, "vector_info", {}),
                "token": self.token, "online_sources": online}
        try:
            base["defaults"] = json.loads(self.default_config.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            base["defaults"] = {"engineering_parameters": {}, "risk_models": {}}
        base["device_library"] = base["defaults"].get("device_library", {})
        base["project_storage"] = self.project_metadata_provider()
        base["workflow"] = self.workflow_provider()
        # 统一路径校验：每次 metadata() 都重新 exists/is_file，绝不缓存上一次的判定。
        base["path_checks"] = check_source_paths(self.paths)
        base["building_sources"] = self.building_source_summary()
        audits = (base["workflow"].get("source_audits") or {}).setdefault("items", {})
        for role, path in self.paths.items():
            audits[role] = source_manifest(role, path, previous=audits.get(role))
        base["data_sources"] = build_registry(base)
        base["data_health"] = build_health(base)
        return base

    def workspace_health(self, bbox):
        if self.loaded is None:
            raise ValueError(self.error or "未加载地图")
        return workspace_health(self.loaded, bbox)

    def population_grid_attribute(self, grid, nodata_semantics=None):
        """当前人口源 + 当前 NoData 语义确认 → 人口网格属性（**唯一**的人口映射入口）。

        ``grid_attributes()`` 与 ``POST /api/workspace/grid/population/remap`` 都调用它，
        因此"全量映射"与"仅重算人口映射"永远使用同一份人口算法与同一份 policy 读取路径：
        这里只读取当前 workflow 快照里的 ``population_nodata_policy``，不复制、不改写算法。

        ``nodata_semantics`` 允许调用方传入**已经取好的**同一份 policy（``grid_attributes``
        为避免重复读取 workflow 快照而这样做）；省略时本方法自己读取。两种路径的语义完全
        相同：都只从项目状态里读，绝不发明确认。
        """

        if nodata_semantics is None:
            snapshot = self.workflow_provider() or {}
            # The population NoData semantics is an *explicit engineering confirmation* held
            # in the project state; when it is not confirmed the mapping keeps its historical
            # behaviour (source NoData stays missing_data and is never filled with zero).
            nodata_semantics = snapshot.get("population_nodata_policy")
        return PopulationGridService().map(grid, self.paths.get("population"), nodata_semantics)

    def grid_attributes(self, grid):
        snapshot = self.workflow_provider() or {}
        policies = snapshot.get("airspace_policies") or {}
        # 建筑事实有两个来源，各自解析成"实际可读取的矢量图层"：
        #  * building_grid：预先聚合好的 L8 事实表（level == 8 时最快的权威路径，也是正式工作流）；
        #  * buildings：原始建筑足迹（.gpkg / .shp / .geojson，或引用它们的 .qgz 工程），
        #    供**层级无关的精确足迹聚合**使用 —— 这样即便是 legacy / diagnostic 的 L7/L6
        #    网格（正式工作流已不再产生），建筑约束依然能进入 Layered Theta* V2 的
        #    coarse 战略垂向包线。
        grid_role = self._resolve_vector_role("building_grid")
        footprint_role = self._resolve_vector_role("buildings")
        buildings = BuildingGridService().map(
            grid,
            grid_role.get("path") if grid_role.get("ok") else self.paths.get("building_grid"),
            footprint_source=footprint_role.get("path") if footprint_role.get("ok") else None,
            footprint_aggregator=aggregate_footprint_facts,
            footprint_layer_name=footprint_role.get("layer_name"),
        )
        buildings["sources"] = {
            "building_grid": self._vector_role_record(grid_role),
            "buildings": self._vector_role_record(footprint_role),
        }
        return {"population": self.population_grid_attribute(
                    grid, snapshot.get("population_nodata_policy")),
                "terrain": TerrainGridService().map(grid, self.paths.get("terrain")),
                "buildings": buildings,
                "airspace": AirspaceGridService().map(
                    grid,
                    QgisAirspaceAdapter(self.local_layers, self.project, self.paths.get("basemap")),
                    policies,
                )}

    @staticmethod
    def _vector_role_record(record):
        """只保留可展示的字段（绝对路径与解析结论），不含任何几何/内容。"""

        record = record if isinstance(record, dict) else {}
        return {
            "ok": bool(record.get("ok")), "status": record.get("status"),
            "reason": record.get("reason"), "source": record.get("source"),
            "value": record.get("value"), "resolved_path": record.get("path"),
            "layer_name": record.get("layer_name"), "format": record.get("format"),
            "layer_title": record.get("layer_title"),
            "selection_reason": record.get("selection_reason"),
        }

    def render(self, query):
        if self.loaded is None:
            raise ValueError(self.error or "未加载地图")
        return self.renderer.render(self.loaded, self.revision, query)

    @classmethod
    def validate_candidate(cls, paths, **kwargs):
        candidate = cls.__new__(cls)
        candidate.map_crs = QgsCoordinateReferenceSystem("EPSG:3857")
        candidate.wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        candidate.loader = QgisSourceLoader(candidate.map_crs, candidate.wgs84, kwargs.get("settings_path", DEFAULT_SETTINGS))
        candidate.renderer = QgisMapRenderer(candidate.map_crs)
        candidate.default_config = Path(kwargs.get("default_config", DEFAULT_CONFIG))
        candidate.token = kwargs.get("token", "")
        candidate.workflow_provider = lambda: {}
        candidate.project_metadata_provider = lambda: {}
        candidate.revision, candidate.error, candidate.loaded, candidate.paths = 0, "", None, dict(paths)
        candidate.load(paths, persist=False)
        return candidate.metadata()
