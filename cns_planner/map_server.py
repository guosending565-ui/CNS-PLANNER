"""Local-only QGIS viewport renderer. Source files are never modified.

HTTP requests run independently; GIS operations are queued to the Qt owner thread.
"""
import json
import math
import os
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import Future
from queue import Queue, Empty
import socket
import threading
from urllib.parse import urlparse, parse_qs, urlencode
from urllib.request import Request, urlopen
from collections import OrderedDict
import secrets
import time
from tile_cache import TileCache
from services.data_health import build_health
from services.data_registry import build_registry
from services.airspace_grid_service import AirspaceGridService
from services.population_grid_service import PopulationGridService
from services.qgis_airspace_adapter import QgisAirspaceAdapter
from services.terrain_grid_service import TerrainGridService
from services.workflow import WorkflowService
from persistence.data_source_repository import DataSourceRepository
from persistence.project_repository import ProjectRepository

from qgis.core import (QgsApplication, QgsProject, QgsCoordinateReferenceSystem,
    QgsCoordinateTransform, QgsRectangle, QgsMapSettings, QgsMapRendererParallelJob,
    QgsRasterLayer, QgsRasterShader, QgsColorRampShader, QgsSingleBandPseudoColorRenderer)
from qgis.PyQt.QtCore import QSize, QBuffer, QIODevice
from qgis.PyQt.QtGui import QColor
from osgeo import gdal

gdal.UseExceptions()

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "cns_planner" / "web"
SETTINGS = ROOT / "projects" / "map_sources.json"
DEFAULT_CONFIG = ROOT / "cns_planner" / "config" / "defaults.json"
AUTO_PROJECT_FILE = ROOT / "projects" / "current_project.json"
ACTIVE_PROJECT_FILE = AUTO_PROJECT_FILE
DEFAULTS = {
    "basemap": "D:/aaa2026project/UOM/全国适飞空域图_单省可更新.qgz",

    "population":
        "D:/aaa2026project/UOM/舟山/规划系统/"
        "chn_pop_2025_CN_100m_R2025A_v1.tif",

    "terrain":
        "D:/aaa2026project/UOM/舟山/规划系统/"
        "GLO30/output/Zhejiang_GLO30_30m.tif",
}
CRS = QgsCoordinateReferenceSystem("EPSG:3857")
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
TOKEN = secrets.token_urlsafe(24)
TASKS = Queue()
TILES = TileCache()
LATEST = {}
LATEST_LOCK = threading.Lock()


def obsolete(query):
    client = query.get("client", [""])[0]
    seq = int(query.get("seq", ["0"])[0])
    with LATEST_LOCK:
        return bool(client) and seq < LATEST.get(client, 0)


def gis_call(action):
    future = Future()
    TASKS.put((future, action))
    return future.result(timeout=90)


class LocalServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(15)
        return connection, address


class MapData:
    def __init__(self):
        self.project = None
        self.population = None
        self.terrain = None
        self.cache = OrderedDict()
        self.revision = 0
        self.error = ""
        paths = DEFAULTS.copy()
        settings_repository = DataSourceRepository(SETTINGS)
        if settings_repository.exists():
            try:
                paths.update(settings_repository.load())
            except (ValueError, OSError):
                pass
        self.paths = paths
        try:
            self.load(paths, persist=False)
        except Exception as exc:
            self.error = str(exc)

    def load(self, paths, persist=True):
        qgz = Path(paths["basemap"])
        pop_tif = Path(paths["population"])
        terrain_tif = Path(paths["terrain"])
        if not qgz.is_file() or qgz.suffix.lower() not in (".qgz", ".qgs"):
            raise ValueError("底图请选择存在的 QGZ/QGS 项目文件")
        if not pop_tif.is_file() or pop_tif.suffix.lower() not in (".tif", ".tiff"):
            raise ValueError("人口数据请选择存在的 GeoTIFF 文件")
        if not terrain_tif.is_file() or terrain_tif.suffix.lower() not in (".tif", ".tiff"):
            raise ValueError("地形数据请选择存在的 GeoTIFF 文件")
        candidate = QgsProject()
        # Read layers/styles only. QGIS project macros are never executed.
        if not candidate.read(str(qgz)):
            raise ValueError("无法读取 QGIS 项目")
        local = [l for l in candidate.layerTreeRoot().layerOrder() if l.providerType() not in ("wms", "wfs", "arcgismapserver", "arcgisfeatureserver")]
        bad = [l.name() for l in local if not l.isValid()]
        if bad:
            raise ValueError("项目引用的数据缺失：" + "、".join(bad))
        if not local:
            raise ValueError("项目中没有可用本地图层")
        raster = QgsRasterLayer(str(pop_tif), "人口密度")
        if not raster.isValid() or not raster.crs().isValid():
            raise ValueError("人口 GeoTIFF 无效或缺少 CRS")
        dataset = gdal.Open(str(pop_tif), gdal.GA_ReadOnly)
        if dataset is None:
            raise ValueError("无法读取人口栅格")
        band = dataset.GetRasterBand(1)
        info = {
            "width": dataset.RasterXSize,
            "height": dataset.RasterYSize,
            "bands": dataset.RasterCount,
            "crs": raster.crs().authid() or raster.crs().description(),
            "nodata": str(band.GetNoDataValue()),
            "pixel_size": list(dataset.GetGeoTransform()[1:6:4]),
            "unit_metadata": band.GetUnitType() or "未标注",
            "unit": "原始：人/像元；地图展示：人/km²"
        }
        # ---------------------------------------------------------
        # WorldPop 人口栅格：
        # 原始值 = 每个像元中的人口数 people / pixel
        # 网页展示希望使用 = 人 / km²
        #
        # 因为数据是 EPSG:4326，经纬度像元实际面积随纬度变化，
        # 这里采用整幅栅格中心纬度估算像元面积，用于显示着色。
        # 后续进行正式风险计算时，应使用更精确的逐像元面积。
        # ---------------------------------------------------------

        gt = dataset.GetGeoTransform()

        pixel_width_deg = abs(gt[1])
        pixel_height_deg = abs(gt[5])

        # 栅格中心纬度
        center_lat = gt[3] + gt[5] * dataset.RasterYSize / 2.0

        # 近似换算：1纬度约111.32 km，
        # 1经度对应距离随纬度按 cos(latitude) 缩小
        km_per_deg_lat = 111.32
        km_per_deg_lon = 111.32 * math.cos(math.radians(center_lat))

        pixel_area_km2 = (
                pixel_width_deg * km_per_deg_lon *
                pixel_height_deg * km_per_deg_lat
        )

        # 网页展示的人口密度分级（人/km²）。
        # 0人口必须透明，否则整张底图会被浅黄色/白色覆盖，看起来“发白”。
        # 其余等级提高饱和度和不透明度，使100 m人口纹理更清晰。
        density_breaks = [
            (0,     "#fff7ec", 0),
            (100,   "#fee391", 110),
            (1000,  "#fec44f", 175),
            (5000,  "#f03b20", 230),
            (20000, "#99000d", 255),
        ]

        # WorldPop 原始值是 人/像元：
        # 人/km² × 像元面积(km²) = 人/像元
        ramp_items = []

        for density, color_hex, alpha in density_breaks:
            raw_value = density * pixel_area_km2
            color = QColor(color_hex)
            color.setAlpha(alpha)

            ramp_items.append(
                QgsColorRampShader.ColorRampItem(
                    raw_value,
                    color,
                    str(density)
                )
            )

        ramp = QgsColorRampShader()
        ramp.setColorRampType(QgsColorRampShader.Interpolated)
        ramp.setColorRampItemList(ramp_items)

        shader = QgsRasterShader(
            0,
            20000 * pixel_area_km2
        )
        shader.setRasterShaderFunction(ramp)

        raster.setRenderer(
            QgsSingleBandPseudoColorRenderer(
                raster.dataProvider(),
                1,
                shader
            )
        )

        # 前面的面积计算和样式构造完成后再关闭GDAL dataset。
        dataset = None

        # ---------------------------------------------------------
        # Copernicus GLO-30 地形高程栅格
        # ---------------------------------------------------------
        terrain = QgsRasterLayer(str(terrain_tif), "地形高程")
        if not terrain.isValid() or not terrain.crs().isValid():
            raise ValueError("地形 DEM 无效或缺少 CRS")

        terrain_ds = gdal.Open(str(terrain_tif), gdal.GA_ReadOnly)
        if terrain_ds is None:
            raise ValueError("无法读取地形 DEM")
        terrain_band = terrain_ds.GetRasterBand(1)
        terrain_info = {
            "width": terrain_ds.RasterXSize,
            "height": terrain_ds.RasterYSize,
            "bands": terrain_ds.RasterCount,
            "crs": terrain.crs().authid() or terrain.crs().description(),
            "nodata": str(terrain_band.GetNoDataValue()),
            "pixel_size": [abs(terrain_ds.GetGeoTransform()[1]), abs(terrain_ds.GetGeoTransform()[5])],
            "unit": "m",
            "source": "Copernicus DEM GLO-30",
        }

        terrain_ramp = QgsColorRampShader()
        terrain_ramp.setColorRampType(QgsColorRampShader.Interpolated)
        terrain_ramp.setColorRampItemList([
            QgsColorRampShader.ColorRampItem(0, QColor("#2c7bb6"), "0 m"),
            QgsColorRampShader.ColorRampItem(100, QColor("#abd9e9"), "100 m"),
            QgsColorRampShader.ColorRampItem(300, QColor("#ffffbf"), "300 m"),
            QgsColorRampShader.ColorRampItem(600, QColor("#fdae61"), "600 m"),
            QgsColorRampShader.ColorRampItem(1200, QColor("#d7191c"), "1200 m"),
        ])
        terrain_shader = QgsRasterShader(0, 1500)
        terrain_shader.setRasterShaderFunction(terrain_ramp)
        terrain.setRenderer(
            QgsSingleBandPseudoColorRenderer(
                terrain.dataProvider(),
                1,
                terrain_shader
            )
        )
        terrain_ds = None

        boxes = []
        layers = []
        for layer in local:
            if not layer.crs().isValid():
                raise ValueError("图层缺少 CRS：" + layer.name())
            extent = QgsCoordinateTransform(layer.crs(), CRS, candidate).transformBoundingBox(layer.extent())
            bbox = [extent.xMinimum(), extent.yMinimum(), extent.xMaximum(), extent.yMaximum()]
            if all(math.isfinite(v) for v in bbox) and not extent.isEmpty():
                boxes.append(bbox)
                layers.append({"id": layer.id(), "name": layer.name(), "bbox": bbox})
        if not boxes:
            raise ValueError("项目图层没有有效范围")
        bounds = [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]
        clean_paths = {"basemap": str(qgz.resolve()), "population": str(pop_tif.resolve()), "terrain": str(terrain_tif.resolve())}
        if persist:
            DataSourceRepository(SETTINGS).save(clean_paths)
        self.project, self.population, self.terrain = candidate, raster, terrain
        pop_extent = QgsCoordinateTransform(raster.crs(), WGS84, candidate).transformBoundingBox(raster.extent())
        self.population_bbox_wgs84 = [pop_extent.xMinimum(), pop_extent.yMinimum(), pop_extent.xMaximum(), pop_extent.yMaximum()]
        terrain_extent = QgsCoordinateTransform(terrain.crs(), WGS84, candidate).transformBoundingBox(terrain.extent())
        self.terrain_bbox_wgs84 = [terrain_extent.xMinimum(), terrain_extent.yMinimum(), terrain_extent.xMaximum(), terrain_extent.yMaximum()]
        self.local_layers = local
        self.layer_boxes = {l["id"]: QgsRectangle(*l["bbox"]) for l in layers}
        self.layer_boxes_wgs84 = {}
        self.hard_constraints = []
        hard_words = ("管制", "禁飞", "限制区", "硬约束", "no-fly", "restricted")
        for layer in local:
            extent = QgsCoordinateTransform(layer.crs(), WGS84, candidate).transformBoundingBox(layer.extent())
            layer_bbox = [extent.xMinimum(), extent.yMinimum(), extent.xMaximum(), extent.yMaximum()]
            self.layer_boxes_wgs84[layer.id()] = layer_bbox
            if any(word in layer.name().lower() for word in hard_words):
                self.hard_constraints.append({"layer_id": layer.id(), "name": layer.name(), "bbox": layer_bbox})
        self.online_sources = {}
        for layer in candidate.layerTreeRoot().layerOrder():
            if layer.providerType() == "wms":
                values = parse_qs(layer.source())
                template = values.get("url", [""])[0]
                if all(marker in template for marker in ("{z}", "{x}", "{y}")):
                    self.online_sources[layer.id()] = {"name": layer.name(), "template": template,
                        "zmin": int(values.get("zmin", ["0"])[0]), "zmax": int(values.get("zmax", ["18"])[0])}
        self.paths, self.raster_info, self.terrain_info, self.layers, self.bounds = clean_paths, info, terrain_info, layers, bounds
        self.revision += 1
        self.error = ""
        self.cache.clear()

    def metadata(self):
        online = []
        for key, value in getattr(self, "online_sources", {}).items():
            item = {"id": key, **{k: v for k, v in value.items() if k != "template"}}
            # Tianditu browser keys must be used by the user's browser, not a server proxy.
            if (urlparse(value["template"]).hostname or "").endswith(".tianditu.gov.cn"):
                item["browser_url"] = value["template"]
            online.append(item)
        base = {"paths": self.paths, "error": self.error, "revision": self.revision,
                "layers": getattr(self, "layers", []), "bounds": getattr(self, "bounds", None),
                "population": getattr(self, "raster_info", {}),
                "terrain": getattr(self, "terrain_info", {}),
                "token": TOKEN, "online_sources": online}
        try:
            base["defaults"] = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            base["defaults"] = {"engineering_parameters": {}, "risk_models": {}}
        base["device_library"] = base["defaults"].get("device_library", {})
        base["project_storage"] = project_storage_metadata()
        base["data_sources"] = build_registry(base)
        base["data_health"] = build_health(base)

        # 当前 data_registry/data_health 仍以人口和底图为主；
        # 在不改动其它服务文件的前提下，把 DEM 状态补充进统一数据源中心。
        terrain_path = base.get("paths", {}).get("terrain", "")
        terrain_ready = bool(base.get("terrain", {}).get("width"))
        terrain_source = {
            "id": "terrain",
            "name": "地形高程",
            "label": "地形高程 / Copernicus GLO-30",
            "category": "环境 / 地形",
            "type": "file",
            "path": terrain_path,
            "formats": "GeoTIFF (.tif / .tiff)",
            "required": False,
        }
        if isinstance(base.get("data_sources"), list):
            base["data_sources"] = [
                item for item in base["data_sources"]
                if item.get("id") != "terrain"
            ] + [terrain_source]

        if isinstance(base.get("data_health"), dict):
            terrain_health = {
                "id": "terrain",
                "label": "地形高程 / Copernicus GLO-30",
                "status": "ready" if terrain_ready else "warning",
                "message": (
                    f"已加载 {base['terrain'].get('width', 0)} × {base['terrain'].get('height', 0)} 像元，"
                    f"CRS：{base['terrain'].get('crs', '未知')}，单位：m"
                    if terrain_ready else "DEM 尚未加载"
                ),
                "category": "环境 / 地形",
                "formats": "GeoTIFF (.tif / .tiff)",
                "required": False,
            }
            items = list(base["data_health"].get("items", []))
            items = [item for item in items if item.get("id") != "terrain"]
            items.append(terrain_health)
            base["data_health"]["items"] = items

        base["workflow"] = WORKFLOW.snapshot()
        return base

    @staticmethod
    def _intersects(left, right):
        return left[0] <= right[2] and left[2] >= right[0] and left[1] <= right[3] and left[3] >= right[1]

    def workspace_health(self, bbox):
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise ValueError("工作区范围无效")
        bbox = [float(value) for value in bbox]
        population_ok = self._intersects(bbox, self.population_bbox_wgs84)
        terrain_ok = self._intersects(bbox, self.terrain_bbox_wgs84)
        covered_layers = [
            layer for layer in self.layers
            if self._intersects(bbox, self.layer_boxes_wgs84.get(layer["id"], [-180, -90, 180, 90]))
        ]
        return {
            "status": "passed" if population_ok and covered_layers else "missing_data",
            "population": {"status": "passed" if population_ok else "missing_data", "message": "人口数据覆盖工作区" if population_ok else "人口数据不覆盖工作区"},
            "airspace": {"status": "passed" if covered_layers else "missing_data", "message": f"{len(covered_layers)} 个空域/本地图层覆盖工作区" if covered_layers else "空域数据不覆盖工作区"},
            "terrain": {"status": "passed" if terrain_ok else "missing_data", "message": "GLO-30 DEM 覆盖工作区" if terrain_ok else "GLO-30 DEM 不覆盖工作区"},
            "buildings": {"status": "missing_data", "message": "建筑数据尚未接入"},
            "property": {"status": "missing_data", "message": "财产暴露数据尚未接入"},
            "loaded_layer_count": len(self.layers),
            "covered_layer_count": len(covered_layers),
        }

    def grid_attributes(self, grid):
        return {
            "population": PopulationGridService().map(grid, self.paths.get("population")),
            "terrain": TerrainGridService().map(grid, self.paths.get("terrain")),
            "airspace": AirspaceGridService().map(
                grid,
                QgisAirspaceAdapter(self.local_layers, self.project, self.paths.get("basemap")),
            ),
        }

    @classmethod
    def validate_candidate(cls, paths):
        candidate = cls.__new__(cls)
        candidate.project = None
        candidate.population = None
        candidate.terrain = None
        candidate.cache = OrderedDict()
        candidate.revision = 0
        candidate.error = ""
        candidate.paths = dict(paths)
        candidate.load(paths, persist=False)
        return candidate.metadata()

    def render(self, query):
        if obsolete(query):
            raise ValueError("过期视图已取消")
        if self.project is None:
            raise ValueError(self.error or "未加载地图")
        bbox = [float(x) for x in query["bbox"][0].split(",")]
        if len(bbox) != 4 or not all(math.isfinite(v) and abs(v) <= 1e9 for v in bbox) or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
            raise ValueError("无效地图范围")
        width, height = int(query["w"][0]), int(query["h"][0])
        if not (1 <= width <= 2560 and 1 <= height <= 1800):
            raise ValueError("图像尺寸超限")
        opacity = float(query.get("opacity", ["0.65"])[0])
        terrain_opacity = float(query.get("terrainOpacity", ["0.55"])[0])
        if not math.isfinite(opacity) or not 0 <= opacity <= 1:
            raise ValueError("人口透明度无效")
        if not math.isfinite(terrain_opacity) or not 0 <= terrain_opacity <= 1:
            raise ValueError("地形透明度无效")
        key = (
            self.revision, tuple(bbox), width, height,
            query.get("pop", ["0"])[0],
            query.get("air", ["1"])[0],
            query.get("terrain", ["0"])[0],
            opacity, terrain_opacity
        )
        if key in self.cache:
            return self.cache[key]
        # QGIS 后端只渲染人口密度和本地图层。
        # 天地图等在线瓦片由浏览器端独立负责。
        layers = []

        # 人口密度
        if query.get("pop", ["0"])[0] == "1":
            self.population.renderer().setOpacity(opacity)
            layers.append(self.population)

        # 适飞空域等本地图层
        if query.get("air", ["1"])[0] == "1":
            viewport = QgsRectangle(*bbox)
            for layer in self.local_layers:
                layer_box = self.layer_boxes.get(layer.id())
                if layer.isValid() and (layer_box is None or layer_box.intersects(viewport)):
                    layers.append(layer)

        # 地形放在本地 QGIS 图层栈底部，避免遮挡人口和空域。
        if query.get("terrain", ["0"])[0] == "1":
            self.terrain.renderer().setOpacity(terrain_opacity)
            layers.append(self.terrain)

        settings = QgsMapSettings()
        settings.setDestinationCrs(CRS)
        settings.setTransformContext(self.project.transformContext())
        settings.setExtent(QgsRectangle(*bbox))
        settings.setOutputSize(QSize(width, height))
        settings.setBackgroundColor(QColor(0, 0, 0, 0))
        settings.setLayers(layers)
        job = QgsMapRendererParallelJob(settings)
        job.start()
        # Only local layers enter QGIS. Online tiles are served independently.
        deadline = time.monotonic() + 60
        while job.isActive() and time.monotonic() < deadline:
            if obsolete(query):
                job.cancel()
                raise ValueError("过期视图已取消")
            QgsApplication.processEvents()
            time.sleep(0.005)
        if job.isActive():
            job.cancel()
            raise ValueError("地图渲染超时，可关闭在线底图后重试")
        errors = job.errors()
        if errors:
            raise ValueError("部分图层渲染失败，请检查数据或关闭在线底图")
        buffer = QBuffer()
        buffer.open(QIODevice.WriteOnly)
        job.renderedImage().save(buffer, "PNG")
        result = bytes(buffer.data())
        self.cache[key] = result
        while len(self.cache) > 32:
            self.cache.popitem(last=False)
        return result


def browse(path, kind):
    folder_only = kind == "project"
    if kind == "basemap":
        extensions = (".qgz", ".qgs")
    elif kind in ("population", "terrain"):
        extensions = (".tif", ".tiff")
    else:
        extensions = ()
    if not path:
        return {"path": "", "parent": "", "entries": [{"name": f"{d}:\\", "path": f"{d}:/", "directory": True} for d in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if Path(f"{d}:/").exists()]}
    directory = Path(path).expanduser().resolve()
    if not directory.is_dir():
        directory = directory.parent
    entries = []
    for item in directory.iterdir():
        try:
            folder = item.is_dir()
            if folder or (not folder_only and item.suffix.lower() in extensions):
                entries.append({"name": item.name, "path": str(item), "directory": folder})
        except OSError:
            continue
    entries.sort(key=lambda e: (not e["directory"], e["name"].casefold()))
    return {"path": str(directory), "parent": str(directory.parent) if directory.parent != directory else "", "entries": entries}


def project_storage_metadata():
    path = Path(ACTIVE_PROJECT_FILE)
    automatic = path.resolve() == AUTO_PROJECT_FILE.resolve()
    return {
        "automatic": automatic,
        "file": str(path),
        "directory": "" if automatic else str(path.parent),
    }


def _active_data_sources_path():
    if Path(ACTIVE_PROJECT_FILE).resolve() == AUTO_PROJECT_FILE.resolve():
        return None
    return Path(ACTIVE_PROJECT_FILE).parent / "data_sources.json"


def persist_active_data_sources():
    target = _active_data_sources_path()
    if target is None:
        return
    clean = {key: DATA.paths.get(key, "") for key in ("basemap", "population", "terrain")}
    DataSourceRepository(target).save(clean)


def save_project_as(project_dir):
    global WORKFLOW, ACTIVE_PROJECT_FILE
    raw = str(project_dir or "").strip()
    if not raw:
        raise ValueError("请选择项目数据存储位置")
    folder = Path(raw).expanduser()
    if not folder.is_absolute():
        raise ValueError("项目存储位置必须使用完整路径")
    folder.mkdir(parents=True, exist_ok=True)
    folder = folder.resolve()
    target = folder / "project_state.json"

    WORKFLOW.save()
    source = ProjectRepository(ACTIVE_PROJECT_FILE)
    if not source.is_file():
        raise ValueError("当前项目尚未形成可保存的项目状态文件")
    if source.path.resolve() != target.resolve():
        source.copy_to(target)

    ACTIVE_PROJECT_FILE = target
    WORKFLOW = WorkflowService(ACTIVE_PROJECT_FILE, DEFAULT_CONFIG)
    persist_active_data_sources()
    return DATA.metadata()


def open_project(project_dir):
    global WORKFLOW, ACTIVE_PROJECT_FILE
    raw = str(project_dir or "").strip()
    if not raw:
        raise ValueError("请选择项目文件夹")
    folder = Path(raw).expanduser().resolve()
    if not folder.is_dir():
        raise ValueError("项目文件夹不存在")
    target = folder / "project_state.json"
    if not target.is_file():
        legacy = folder / "current_project.json"
        if legacy.is_file():
            target = legacy
        else:
            raise ValueError("该目录不是有效项目：缺少 project_state.json")

    sources_repository = DataSourceRepository(folder / "data_sources.json")
    if sources_repository.is_file():
        saved = sources_repository.load()
        clean = {key: saved.get(key) or DATA.paths.get(key) or DEFAULTS.get(key, "") for key in ("basemap", "population", "terrain")}
        DATA.load(clean, persist=False)

    ACTIVE_PROJECT_FILE = target
    WORKFLOW = WorkflowService(ACTIVE_PROJECT_FILE, DEFAULT_CONFIG)
    return DATA.metadata()


def check_online_services():
    sources = list(getattr(DATA, "online_sources", {}).values())
    if not sources:
        return {"ok": False, "message": "当前 QGIS 项目中没有识别到在线瓦片服务", "results": []}

    results = []
    tianditu_key = ""
    for source in sources:
        name = source.get("name", "在线服务")
        template = source.get("template", "")
        try:
            zmin = int(source.get("zmin", 0)); zmax = int(source.get("zmax", 18)); z = max(zmin, min(6, zmax))
            lon, lat = 120.0, 30.0
            n = 2 ** z
            x = int((lon + 180.0) / 360.0 * n)
            y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
            test_url = template.format(z=z, x=x, y=y)
            request = Request(test_url, headers={"User-Agent": "CNS-Planner/1.0", "Cache-Control": "no-cache"})
            with urlopen(request, timeout=8) as response:
                mime = (response.headers.get("Content-Type") or "").lower()
                sample = response.read(128)
                ok = getattr(response, "status", 200) == 200 and "image" in mime and bool(sample)
            results.append({"name": name, "ok": ok, "message": "服务正常" if ok else "返回内容不是有效地图图片"})
            parsed = urlparse(template)
            if (parsed.hostname or "").endswith(".tianditu.gov.cn") and not tianditu_key:
                tianditu_key = parse_qs(parsed.query).get("tk", [""])[0]
        except Exception as exc:
            results.append({"name": name, "ok": False, "message": str(exc)})

    if tianditu_key:
        try:
            post_str = {"keyWord": "北京", "level": 12, "mapBound": "73,18,135,54", "queryType": 7, "start": 0, "count": 1}
            url = "https://api.tianditu.gov.cn/v2/search?" + urlencode({"postStr": json.dumps(post_str, ensure_ascii=False), "type": "query", "tk": tianditu_key})
            request = Request(url, headers={"User-Agent": "CNS-Planner/1.0", "Cache-Control": "no-cache"})
            with urlopen(request, timeout=8) as response:
                payload = json.loads(response.read().decode("utf-8"))
            ok = isinstance(payload, dict) and not payload.get("msg")
            results.append({"name": "天地图地名搜索", "ok": ok, "message": "服务正常" if ok else str(payload.get("msg") or "返回异常")})
        except Exception as exc:
            results.append({"name": "天地图地名搜索", "ok": False, "message": str(exc)})

    return {"ok": bool(results) and all(item["ok"] for item in results), "results": results}


class Handler(BaseHTTPRequestHandler):
    def respond(self, data, content_type="application/json; charset=utf-8", status=200, cache=False):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=86400" if cache else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "frame-ancestors 'self' http://127.0.0.1:8501 http://localhost:8501")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def allowed(self):
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        return host in ("127.0.0.1:8765", "localhost:8765") and (not origin or origin in ("http://127.0.0.1:8765", "http://localhost:8765"))

    def do_GET(self):
        if not self.allowed():
            return self.respond({"error": "仅允许本机同源访问"}, status=403)
        url = urlparse(self.path)
        q = parse_qs(url.query)
        try:
            if url.path == "/api/health":
                return self.respond({"service": "cns-map", "ready": True, "data_error": DATA.error})
            if url.path == "/api/state":
                return self.respond(gis_call(DATA.metadata))
            if url.path == "/api/data-sources":
                return self.respond(gis_call(lambda: DATA.metadata()["data_sources"]))
            if url.path == "/api/data-health":
                return self.respond(gis_call(lambda: DATA.metadata()["data_health"]))
            if url.path == "/api/workflow":
                return self.respond(WORKFLOW.snapshot())
            if url.path == "/api/workspace/grid":
                return self.respond(WORKFLOW.grid_snapshot())
            if url.path == "/api/workspace/grid/attributes":
                return self.respond(WORKFLOW.grid_attributes_snapshot())
            if url.path == "/api/online-health":
                if self.headers.get("X-CNS-Token") != TOKEN:
                    return self.respond({"error": "无效会话"}, status=403)
                return self.respond(check_online_services())
            if url.path == "/api/export/project":
                return self.respond(WORKFLOW.export_project(), "application/json; charset=utf-8")
            if url.path == "/api/export/routes":
                return self.respond(WORKFLOW.export_routes(), "application/geo+json; charset=utf-8")
            if url.path == "/api/export/sites":
                return self.respond(WORKFLOW.export_sites(), "application/geo+json; charset=utf-8")
            if url.path == "/api/render":
                client = q.get("client", [""])[0][:100]
                if client:
                    with LATEST_LOCK:
                        if len(LATEST) > 100:
                            LATEST.clear()
                        LATEST[client] = max(int(q.get("seq", ["0"])[0]), LATEST.get(client, 0))
                return self.respond(gis_call(lambda: DATA.render(q)), "image/png")
            if url.path == "/api/tile":
                source = DATA.online_sources.get(q.get("source", [""])[0])
                if not source:
                    raise ValueError("在线底图来源不存在")
                z, x, y = (int(q[k][0]) for k in ("z", "x", "y"))
                if not source["zmin"] <= z <= source["zmax"]:
                    raise ValueError("底图缩放级别超出范围")
                data, mime = TILES.get(source["template"], z, x, y)
                return self.respond(data, mime, cache=True)
            if url.path == "/api/browse":
                if self.headers.get("X-CNS-Token") != TOKEN:
                    return self.respond({"error": "无效会话"}, status=403)
                return self.respond(browse(q.get("path", [""])[0], q.get("kind", ["basemap"])[0]))
            files = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8")}
            files["/tiles.js"] = ("tiles.js", "text/javascript; charset=utf-8")
            files["/grid_theme.js"] = ("grid_theme.js", "text/javascript; charset=utf-8")
            if url.path in files:
                name, mime = files[url.path]
                return self.respond((STATIC / name).read_bytes(), mime)
            self.respond({"error": "未找到"}, status=404)
        except Exception as exc:
            self.respond({"error": str(exc)}, status=400)

    def do_POST(self):
        if not self.allowed() or self.headers.get("X-CNS-Token") != TOKEN:
            return self.respond({"error": "无效会话或来源"}, status=403)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length < 16384:
                raise ValueError("请求大小无效")
            paths = json.loads(self.rfile.read(length))
            if not isinstance(paths, dict):
                raise ValueError("请求必须是 JSON 对象")
            if self.path == "/api/project/save-as":
                return self.respond(gis_call(lambda: save_project_as(paths.get("project_dir"))))
            if self.path == "/api/project/open":
                return self.respond(gis_call(lambda: open_project(paths.get("project_dir"))))
            if self.path.startswith("/api/workflow/"):
                action = self.path.rsplit("/", 1)[-1]
                if action == "project":
                    return self.respond(WORKFLOW.set_project(paths))
                if action == "workspace":
                    health = gis_call(lambda: DATA.workspace_health(paths.get("bbox")))
                    WORKFLOW.set_workspace(paths.get("bbox"), health)
                    results = gis_call(lambda: DATA.grid_attributes(WORKFLOW.grid_snapshot()))
                    return self.respond(WORKFLOW.apply_grid_attributes(results))
                if action == "workspace-clear":
                    return self.respond(WORKFLOW.clear_workspace())
                if action == "traffic-simulate":
                    return self.respond(WORKFLOW.run_traffic_simulation(paths))
                if action == "node":
                    return self.respond(WORKFLOW.add_node(paths.get("coordinate", []), paths.get("name")))
                if action == "node-delete":
                    return self.respond(WORKFLOW.delete_node(paths.get("node_id")))
                if action == "scenario":
                    return self.respond(WORKFLOW.generate_scenario(paths.get("direction", "both")))
                if action == "route-delete":
                    return self.respond(WORKFLOW.delete_route(paths.get("route_id")))
                if action == "operational":
                    return self.respond(WORKFLOW.generate_operational(DATA.hard_constraints))
                if action == "rules":
                    return self.respond(WORKFLOW.set_rules(paths))
                if action == "devices":
                    return self.respond(WORKFLOW.set_devices(paths.get("devices")))
                if action == "coverage":
                    return self.respond(WORKFLOW.plan_coverage())
                if action == "save":
                    WORKFLOW.save()
                    return self.respond(WORKFLOW.snapshot())
                return self.respond({"error": "工作流操作不存在"}, status=404)
            if self.path not in ("/api/sources", "/api/data-sources", "/api/data-sources/validate"):
                return self.respond({"error": "未找到"}, status=404)
            clean = {key: paths.get(key, "") for key in ("basemap", "population", "terrain")}
            if self.path == "/api/data-sources/validate":
                return self.respond(gis_call(lambda: MapData.validate_candidate(clean)))
            def replace_sources():
                previous = dict(DATA.paths)
                DATA.load(clean)
                changed = {name for name in clean if previous.get(name) != DATA.paths.get(name)}
                WORKFLOW.invalidate_grid_attributes(changed)
                if "basemap" in changed:
                    WORKFLOW.invalidate("data")
                WORKFLOW.save()
                persist_active_data_sources()
                return DATA.metadata()
            self.respond(gis_call(replace_sources))
        except Exception as exc:
            self.respond({"error": str(exc)}, status=400)

    def log_message(self, format, *args):
        # Avoid writing project URLs / datasource credentials to logs.
        pass


if __name__ == "__main__":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    application = QgsApplication([], False)
    application.initQgis()
    WORKFLOW = WorkflowService(ACTIVE_PROJECT_FILE, DEFAULT_CONFIG)
    DATA = MapData()
    print("地图数据加载完成" if not DATA.error else DATA.error, flush=True)
    server = LocalServer(("127.0.0.1", 8765), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        while True:
            QgsApplication.processEvents()
            try:
                future, action = TASKS.get(timeout=0.03)
            except Empty:
                continue
            if future.set_running_or_notify_cancel():
                try:
                    future.set_result(action())
                except Exception as exc:
                    future.set_exception(exc)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
