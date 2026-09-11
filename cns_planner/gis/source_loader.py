"""Read-only QGIS project and GeoTIFF loading with existing styles preserved."""

from collections import OrderedDict
from dataclasses import dataclass
import math
from pathlib import Path
from urllib.parse import parse_qs

from osgeo import gdal
from qgis.PyQt.QtGui import QColor
from qgis.core import (
    QgsColorRampShader, QgsCoordinateTransform, QgsProject, QgsRasterLayer,
    QgsRasterShader, QgsRectangle, QgsSingleBandPseudoColorRenderer,
)

from ..persistence.data_source_repository import DataSourceRepository
from .constraints import hard_constraints, layer_extents

gdal.UseExceptions()


@dataclass
class LoadedSources:
    project: object
    population: object
    terrain: object
    paths: dict
    raster_info: dict
    terrain_info: dict
    local_layers: list
    layers: list
    bounds: list
    layer_boxes: dict
    layer_boxes_wgs84: dict
    population_bbox_wgs84: list
    terrain_bbox_wgs84: list
    hard_constraints: list
    online_sources: dict


class QgisSourceLoader:
    def __init__(self, map_crs, wgs84, settings_path):
        self.map_crs, self.wgs84 = map_crs, wgs84
        self.settings_path = Path(settings_path)

    def initial_paths(self, defaults):
        paths = dict(defaults)
        repository = DataSourceRepository(self.settings_path)
        if repository.exists():
            try:
                paths.update(repository.load())
            except (ValueError, OSError):
                pass
        return paths

    def load(self, paths, persist=True):
        qgz, pop_tif, terrain_tif = self._validate_paths(paths)
        project = QgsProject()
        if not project.read(str(qgz)):
            raise ValueError("无法读取 QGIS 项目")
        local = [layer for layer in project.layerTreeRoot().layerOrder()
                 if layer.providerType() not in ("wms", "wfs", "arcgismapserver", "arcgisfeatureserver")]
        bad = [layer.name() for layer in local if not layer.isValid()]
        if bad:
            raise ValueError("项目引用的数据缺失：" + "、".join(bad))
        if not local:
            raise ValueError("项目中没有可用本地图层")
        population, population_info = self._load_population(pop_tif)
        terrain, terrain_info = self._load_terrain(terrain_tif)
        projected_extents = layer_extents(local, project, self.map_crs)
        layers = [{"id": layer.id(), "name": layer.name(), "bbox": projected_extents[layer.id()]}
                  for layer in local if layer.id() in projected_extents]
        if not layers:
            raise ValueError("项目图层没有有效范围")
        boxes = [item["bbox"] for item in layers]
        bounds = [min(box[0] for box in boxes), min(box[1] for box in boxes),
                  max(box[2] for box in boxes), max(box[3] for box in boxes)]
        clean_paths = {"basemap": str(qgz.resolve()), "population": str(pop_tif.resolve()), "terrain": str(terrain_tif.resolve())}
        if persist:
            DataSourceRepository(self.settings_path).save(clean_paths)
        wgs84_extents = layer_extents(local, project, self.wgs84)
        pop_extent = QgsCoordinateTransform(population.crs(), self.wgs84, project).transformBoundingBox(population.extent())
        terrain_extent = QgsCoordinateTransform(terrain.crs(), self.wgs84, project).transformBoundingBox(terrain.extent())
        online_sources = self._online_sources(project)
        return LoadedSources(
            project=project, population=population, terrain=terrain,
            paths=clean_paths, raster_info=population_info, terrain_info=terrain_info,
            local_layers=local, layers=layers, bounds=bounds,
            layer_boxes={item["id"]: QgsRectangle(*item["bbox"]) for item in layers},
            layer_boxes_wgs84=wgs84_extents,
            population_bbox_wgs84=self._bbox(pop_extent),
            terrain_bbox_wgs84=self._bbox(terrain_extent),
            hard_constraints=hard_constraints(local, wgs84_extents),
            online_sources=online_sources,
        )

    @staticmethod
    def _validate_paths(paths):
        qgz, population, terrain = (Path(paths[key]) for key in ("basemap", "population", "terrain"))
        if not qgz.is_file() or qgz.suffix.lower() not in (".qgz", ".qgs"):
            raise ValueError("底图请选择存在的 QGZ/QGS 项目文件")
        if not population.is_file() or population.suffix.lower() not in (".tif", ".tiff"):
            raise ValueError("人口数据请选择存在的 GeoTIFF 文件")
        if not terrain.is_file() or terrain.suffix.lower() not in (".tif", ".tiff"):
            raise ValueError("地形数据请选择存在的 GeoTIFF 文件")
        return qgz, population, terrain

    @staticmethod
    def _load_population(path):
        raster = QgsRasterLayer(str(path), "人口密度")
        if not raster.isValid() or not raster.crs().isValid():
            raise ValueError("人口 GeoTIFF 无效或缺少 CRS")
        dataset = gdal.Open(str(path), gdal.GA_ReadOnly)
        if dataset is None:
            raise ValueError("无法读取人口栅格")
        band, transform = dataset.GetRasterBand(1), dataset.GetGeoTransform()
        info = {
            "width": dataset.RasterXSize, "height": dataset.RasterYSize,
            "bands": dataset.RasterCount,
            "crs": raster.crs().authid() or raster.crs().description(),
            "nodata": str(band.GetNoDataValue()),
            "pixel_size": list(transform[1:6:4]),
            "unit_metadata": band.GetUnitType() or "未标注",
            "unit": "原始：人/像元；地图展示：人/km²",
        }
        center_lat = transform[3] + transform[5] * dataset.RasterYSize / 2.0
        pixel_area = abs(transform[1]) * 111.32 * math.cos(math.radians(center_lat)) * abs(transform[5]) * 111.32
        items = []
        for density, color_hex, alpha in ((0, "#fff7ec", 0), (100, "#fee391", 110), (1000, "#fec44f", 175), (5000, "#f03b20", 230), (20000, "#99000d", 255)):
            color = QColor(color_hex); color.setAlpha(alpha)
            items.append(QgsColorRampShader.ColorRampItem(density * pixel_area, color, str(density)))
        ramp = QgsColorRampShader(); ramp.setColorRampType(QgsColorRampShader.Interpolated); ramp.setColorRampItemList(items)
        shader = QgsRasterShader(0, 20000 * pixel_area); shader.setRasterShaderFunction(ramp)
        raster.setRenderer(QgsSingleBandPseudoColorRenderer(raster.dataProvider(), 1, shader))
        dataset = None
        return raster, info

    @staticmethod
    def _load_terrain(path):
        raster = QgsRasterLayer(str(path), "地形高程")
        if not raster.isValid() or not raster.crs().isValid():
            raise ValueError("地形 DEM 无效或缺少 CRS")
        dataset = gdal.Open(str(path), gdal.GA_ReadOnly)
        if dataset is None:
            raise ValueError("无法读取地形 DEM")
        band, transform = dataset.GetRasterBand(1), dataset.GetGeoTransform()
        info = {"width": dataset.RasterXSize, "height": dataset.RasterYSize, "bands": dataset.RasterCount,
                "crs": raster.crs().authid() or raster.crs().description(), "nodata": str(band.GetNoDataValue()),
                "pixel_size": [abs(transform[1]), abs(transform[5])], "unit": "m", "source": "Copernicus DEM GLO-30"}
        ramp = QgsColorRampShader(); ramp.setColorRampType(QgsColorRampShader.Interpolated)
        ramp.setColorRampItemList([QgsColorRampShader.ColorRampItem(value, QColor(color), label) for value, color, label in (
            (0, "#2c7bb6", "0 m"), (100, "#abd9e9", "100 m"), (300, "#ffffbf", "300 m"),
            (600, "#fdae61", "600 m"), (1200, "#d7191c", "1200 m"))])
        shader = QgsRasterShader(0, 1500); shader.setRasterShaderFunction(ramp)
        raster.setRenderer(QgsSingleBandPseudoColorRenderer(raster.dataProvider(), 1, shader))
        dataset = None
        return raster, info

    @staticmethod
    def _online_sources(project):
        result = {}
        for layer in project.layerTreeRoot().layerOrder():
            if layer.providerType() != "wms":
                continue
            values = parse_qs(layer.source())
            template = values.get("url", [""])[0]
            if all(marker in template for marker in ("{z}", "{x}", "{y}")):
                result[layer.id()] = {
                    "name": layer.name(), "template": template,
                    "zmin": int(values.get("zmin", ["0"])[0]),
                    "zmax": int(values.get("zmax", ["18"])[0]),
                }
        return result

    @staticmethod
    def _online_sources(project):
        result = {}
        for layer in project.layerTreeRoot().layerOrder():
            if layer.providerType() != "wms":
                continue
            values = parse_qs(layer.source())
            template = values.get("url", [""])[0]
            if all(marker in template for marker in ("{z}", "{x}", "{y}")):
                result[layer.id()] = {"name": layer.name(), "template": template,
                                      "zmin": int(values.get("zmin", ["0"])[0]), "zmax": int(values.get("zmax", ["18"])[0])}
        return result

    @staticmethod
    def _bbox(extent):
        return [extent.xMinimum(), extent.yMinimum(), extent.xMaximum(), extent.yMaximum()]
