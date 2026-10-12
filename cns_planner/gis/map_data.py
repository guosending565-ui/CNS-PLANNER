"""Facade over GIS loading, metadata, mapping and rendering capabilities."""

import json
from copy import deepcopy
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
    # 建筑单体与高度证据（Round35-F4/B3X 版本化数据）：``buildings_v3`` 在 538,228 个
    # footprint 上带 ``height_evidence_level`` H0–H4 证据列与独立的 ``height_evidence``
    # 表（fid 主键 1:1）。它与 ``building_grid`` 的 L8 v3 是**同一证据链**，必须成对使用。
    #
    # 这里只是**本机默认建议值**：项目侧的权威 locator 由 ``data_sources.json`` /
    # ``projects/map_sources.json`` 决定；算法侧一律从 ``data_sources`` 消费，gis 适配层
    # 里不出现第二套路径来源。旧 ``zhoushan_buildings.gpkg`` 仍然留在盘上作为历史数据，
    # 但**不再**是本机默认建议值（不得出现新旧混用）。
    "buildings": (
        "D:/aaa2026project/UOM/舟山/规划系统/building/processed/zhoushan_buildings_v3.gpkg"
    ),
    # L8 建筑网格 v3：``centroid_building_count`` 与 ``intersecting_building_count`` 分离，
    # ``height_max_m`` 改为相交建筑最大 ``height_estimate_m``；全库 20,491 个 footprint-only
    # 格由此取得真实相交高度（旧 L8 在这些格上误判为"无建筑"）。
    "building_grid": (
        "D:/aaa2026project/UOM/舟山/规划系统/building/processed"
        "/zhoushan_building_grid_L8_v3.gpkg"
    ),
    # 陆域掩膜（真实数据，只读来源）：Round35-A 版本化 OSM land polygons v2，
    # 来源 ``osmdata.openstreetmap.de`` 的 ``land-polygons-split-4326.zip``
    # （zip sha256 ``18afc502…c9da``，与 v1 登记值逐位一致），按业务 bbox
    # ``121.0–123.2 E / 29.0–31.5 N`` 裁剪，source CRS ``EPSG:4326``，图层 ``land``。
    #
    # 为什么换到 v2：v1 只裁剪到 ``121.8–122.7 E / 29.6–30.5 N``，而当时分类器把
    # "所有多边形之外"一律判 ``sea``，导致嵊泗列岛（枸杞、嵊山）、金塘、宁波北仑等
    # 真实陆域被判为海面（审计 D-01）。v2 扩到覆盖全部业务范围并留 ≥0.15° 余量。
    #
    # ``LandMaskSource`` 现在有 **footprint 守卫**：查询点落在数据有效覆盖范围之外时
    # 返回 ``unknown``（fail-closed），绝不判 ``sea``。因此即使未来业务范围超出该 bbox，
    # 结果也只会是 unknown，不会变成假海面。
    #
    # 语义边界（不得夸大）：这是**工程化/简化的海岸线陆域面**，可以表达浙江陆块与舟山岛间
    # 海域，但**不是** 5 m 高精度海岸线；因此海岸附近用显式
    # ``coastal_uncertainty_buffer_m``（工程假设，未确认）单独成类，绝不把它当成数据真实精度。
    #
    # 这里只是**本机默认建议值**：算法侧一律从 ``data_sources["land_mask"]`` 消费，
    # radar algorithm / adapter 里不出现任何绝对路径。
    "land_mask": "projects/derived/land_mask_osm/zhoushan_land_osm_v2.gpkg",
    # 制图陆地面（cartographic_land）：**只**用于专题地图的陆海表达。
    #
    # 为什么不能直接用 land_mask：本机现有的矢量陆地/岸线数据只有省界、国界与
    # Natural Earth 1:10m，它们在嵊泗列岛是残缺的（枸杞、嵊山落在几何之外 26.6 km /
    # 32.2 km），会在地图上制造大量"通信站址位于海上"的假象。
    #
    # 该文件由 ``tools/derive_cartographic_land.py`` 从项目**已有**数据派生：
    # FABDEM 30 m DTM（高程 > 0 m）+ 建筑足迹（538 228 个真实建筑面，栅格化后膨胀），
    # 经闭运算与 Polygonize 得到 EPSG:4326 多边形；派生参数与校验结论记录在同目录
    # ``cartographic_land_v1.json``。它**不**参与 surface classification、风险计算或
    # 航路规划，也不会替换 canonical ``land_mask`` 的业务语义。
    "cartographic_land": (
        "projects/derived/cartographic_land/cartographic_land_v1.gpkg"
    ),
}


#: ``load()`` 成功时必须从 :class:`LoadedSources` 投影到 ``MapData`` 上的**全部**属性。
#:
#: 这份清单同时是启动失败时的接口契约：``MapData.__init__`` 捕获加载异常后，仍会按它
#: 建立"未加载"缺省值，因此任何调用方（``ApplicationContext``、``metadata()``、
#: ``project_directory_service``）都不会再遇到"加载器半途抛错 → 对象只投影了一半属性
#: → ``AttributeError`` 掩盖真实数据源原因"的启动阻塞（BUG-STARTUP-006）。
#:
#: 缺省值只表示**未加载**，不表示"空数据"：真实失败原因一律保留在 ``self.error`` 里并
#: 由 :meth:`MapData.metadata` 如实上报，绝不把加载失败伪装成"已加载但内容为空"。
LOADED_SOURCE_ATTRIBUTES = (
    "project", "population", "terrain", "terrain_dtm", "raster_info", "terrain_info",
    "terrain_dtm_info", "vector_info", "local_layers", "layers", "bounds", "layer_boxes",
    "layer_boxes_wgs84", "population_bbox_wgs84", "terrain_bbox_wgs84",
    "terrain_dtm_bbox_wgs84", "hard_constraints", "online_sources",
)

#: 上述属性在"未加载"状态下的缺省值（标量/列表/字典三类）。
LOAD_FAILED_ATTRIBUTE_DEFAULTS = {
    "project": None, "population": None, "terrain": None, "terrain_dtm": None,
    "raster_info": {}, "terrain_info": {}, "terrain_dtm_info": {}, "vector_info": {},
    "local_layers": [], "layers": [], "bounds": [], "layer_boxes": {},
    "layer_boxes_wgs84": {}, "population_bbox_wgs84": [], "terrain_bbox_wgs84": [],
    "terrain_dtm_bbox_wgs84": [], "hard_constraints": [], "online_sources": {},
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
            # 加载失败**不吞掉原因**：``self.error`` 保留原始信息，供 ``metadata()``、
            # ``/api/health`` 与数据源中心如实展示；同时按 LOADED_SOURCE_ATTRIBUTES
            # 建立完整缺省属性，让对象保持"可查询但未加载"的**完整**接口契约。
            self.error = str(exc)
            self._apply_unloaded_defaults()

    def _apply_unloaded_defaults(self):
        """建立"未加载"状态的完整属性契约（失败路径专用，不伪造任何事实）。

        只写缺省值，不写任何路径/身份/内容推断：调用方看到的是"这三个字典为空"，
        而不是"这个数据源是空的"。真正的原因在 ``self.error``。
        """

        for name in LOADED_SOURCE_ATTRIBUTES:
            default = LOAD_FAILED_ATTRIBUTE_DEFAULTS.get(name)
            setattr(self, name, deepcopy(default))
        self.source_signatures = {}
        self._vector_role_cache = {}

    def load(self, paths, persist=True):
        loaded = self.loader.load(paths, persist)
        if loaded is None:
            # 加载器返回空对象：这是"未加载"，不是"已加载且为空"。绝不静默当成成功。
            raise ValueError("数据源加载未返回结果")
        self.loaded, self.paths = loaded, loaded.paths
        self.source_signatures = {
            name: self._source_signature(path) for name, path in self.paths.items()
        }
        # 每次加载都丢弃上一次的矢量角色解析结果：路径可能在磁盘上被替换/删除/移动，
        # 复用旧解析会把"上一个文件的图层"带进当前界面（缓存旧状态）。
        self._vector_role_cache = {}
        for name in LOADED_SOURCE_ATTRIBUTES:
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

        Round35-D：当策略确认为**模式二（开阔海域工程假设）**时，这里额外用 canonical
        陆域掩膜为该批网格逐格探测"陆域缺席证据"并交给映射层。**未确认该模式时完全不
        做这一步**，因此普通请求不会因此多扫描任何 GIS 数据。
        """

        snapshot = self.workflow_provider() or {}
        if nodata_semantics is None:
            # The population NoData semantics is an *explicit engineering confirmation* held
            # in the project state; when it is not confirmed the mapping keeps its historical
            # behaviour (source NoData stays missing_data and is never filled with zero).
            nodata_semantics = snapshot.get("population_nodata_policy")
        open_sea_evidence = self._open_sea_evidence(grid, nodata_semantics, snapshot)
        # 未确认模式二时**不传**该关键字参数：保持历史调用形状（也兼容只实现三参数的
        # 适配器/替身），并且普通请求不会因此多扫描任何 GIS 数据。
        extra = (
            {} if open_sea_evidence is None
            else {"open_sea_evidence": open_sea_evidence}
        )
        return PopulationGridService().map(
            grid, self.paths.get("population"), nodata_semantics, **extra
        )

    def _open_sea_evidence(self, grid, nodata_semantics, snapshot):
        """模式二所需的**逐格**陆域缺席证据；未确认该模式时返回 ``None``（零开销）。

        证据来自**同一个** canonical ``LandMaskSource``（与 surface classification 完全同源），
        因此不存在"第二套陆海判定"。任一格拿不到证据时该格缺席，映射层会 fail-closed 拒绝判 0。
        """

        from .population_nodata_evidence import build_open_sea_evidence

        return build_open_sea_evidence(
            grid,
            land_mask_path=self.paths.get("land_mask"),
            surface_policy=(snapshot or {}).get("surface_classification_policy"),
            nodata_policy=nodata_semantics,
        )

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
