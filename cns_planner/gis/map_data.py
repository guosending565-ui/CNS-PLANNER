"""Facade over GIS loading, metadata, mapping and rendering capabilities."""

import json
from pathlib import Path
from urllib.parse import urlparse

from qgis.core import QgsCoordinateReferenceSystem

from ..data.health import build_health
from ..data.registry import build_registry
from ..data.mapping.airspace import AirspaceGridService
from ..data.mapping.population import PopulationGridService
from ..data.mapping.terrain import TerrainGridService
from .airspace_adapter import QgisAirspaceAdapter
from .renderer import QgisMapRenderer
from .source_loader import QgisSourceLoader
from .workspace_health import workspace_health


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SETTINGS = ROOT / "projects" / "map_sources.json"
DEFAULT_CONFIG = ROOT / "cns_planner" / "config" / "defaults.json"
DEFAULT_PATHS = {
    "basemap": "D:/aaa2026project/UOM/全国适飞空域图_单省可更新.qgz",
    "population": "D:/aaa2026project/UOM/舟山/规划系统/chn_pop_2025_CN_100m_R2025A_v1.tif",
    "terrain": "D:/aaa2026project/UOM/舟山/规划系统/GLO30/output/Zhejiang_GLO30_30m.tif",
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
        for name in ("project", "population", "terrain", "raster_info", "terrain_info", "local_layers", "layers", "bounds", "layer_boxes", "layer_boxes_wgs84", "population_bbox_wgs84", "terrain_bbox_wgs84", "hard_constraints", "online_sources"):
            setattr(self, name, getattr(loaded, name))
        self.revision += 1; self.error = ""; self.renderer.clear()

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
                "token": self.token, "online_sources": online}
        try:
            base["defaults"] = json.loads(self.default_config.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            base["defaults"] = {"engineering_parameters": {}, "risk_models": {}}
        base["device_library"] = base["defaults"].get("device_library", {})
        base["project_storage"] = self.project_metadata_provider()
        base["workflow"] = self.workflow_provider()
        base["data_sources"] = build_registry(base)
        base["data_health"] = build_health(base)
        return base

    def workspace_health(self, bbox):
        if self.loaded is None:
            raise ValueError(self.error or "未加载地图")
        return workspace_health(self.loaded, bbox)

    def grid_attributes(self, grid):
        return {"population": PopulationGridService().map(grid, self.paths.get("population")),
                "terrain": TerrainGridService().map(grid, self.paths.get("terrain")),
                "airspace": AirspaceGridService().map(grid, QgisAirspaceAdapter(self.local_layers, self.project, self.paths.get("basemap")))}

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
