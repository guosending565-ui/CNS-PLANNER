"""Read-only QGIS project and GeoTIFF loading with existing styles preserved."""

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs

from osgeo import gdal
from qgis.PyQt.QtGui import QColor
from qgis.core import (
    QgsColorRampShader, QgsCoordinateTransform, QgsProject, QgsRasterLayer,
    QgsRasterShader, QgsRectangle, QgsSingleBandPseudoColorRenderer,
)

from ..persistence.data_source_repository import DataSourceRepository
from ..data.source_profiles import COPERNICUS_GLO30, FABDEM_V12, WORLDPOP_R2025A
from .building_footprint_aggregation import discover_vector_layer
from .path_resolver import STATUS_OK, resolve_source_path
from .qgis_project_layers import resolve_vector_layer_source
from .source_inspection import inspect_geopackage
from .constraints import hard_constraints, layer_extents

gdal.UseExceptions()

REFERENCE_SOURCE_KEYS = (
    "reference_landing_sites", "reference_routes", "equipment_reference_catalog", "towers",
)
OPTIONAL_VECTOR_SOURCE_KEYS = (
    "buildings",
    "building_grid",
    # 陆域掩膜：与建筑类不同，**不**要求"能解析出建筑图层"，
    # 只需要是一个存在的、受支持的矢量文件（逐点判定在 radar adapter 里做）。
    "land_mask",
)
#: 建筑类来源必须在加载时确认"能解析出可用建筑图层"（陆域掩膜不适用此规则）。
BUILDING_VECTOR_SOURCE_KEYS = ("buildings", "building_grid")
#: 必须是具体文件的来源角色（其余角色缺失只表示"未配置"）。
REQUIRED_FILE_ROLES = ("basemap", "population", "terrain")
#: 启动时执行统一 exists/is_file/格式校验的角色。
CHECKED_FILE_ROLES = REQUIRED_FILE_ROLES + ("terrain_dtm",) + OPTIONAL_VECTOR_SOURCE_KEYS

@dataclass
class LoadedSources:
    project: object
    population: object
    terrain: object
    terrain_dtm: object
    paths: dict
    raster_info: dict
    terrain_info: dict
    terrain_dtm_info: dict
    vector_info: dict
    local_layers: list
    layers: list
    bounds: list
    layer_boxes: dict
    layer_boxes_wgs84: dict
    population_bbox_wgs84: list
    terrain_bbox_wgs84: list
    terrain_dtm_bbox_wgs84: list
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
        qgz, pop_tif, terrain_tif, terrain_dtm_tif = self._validate_paths(paths)
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
        terrain_dtm, terrain_dtm_info = self._load_terrain_dtm(terrain_dtm_tif) if terrain_dtm_tif else (None, {})
        vector_info = {
            key: self.vector_source_info(paths[key], key)
            for key in OPTIONAL_VECTOR_SOURCE_KEYS if paths.get(key)
        }
        projected_extents = layer_extents(local, project, self.map_crs)
        layers = [{"id": layer.id(), "name": layer.name(), "bbox": projected_extents[layer.id()]}
                  for layer in local if layer.id() in projected_extents]
        if not layers:
            raise ValueError("项目图层没有有效范围")
        boxes = [item["bbox"] for item in layers]
        bounds = [min(box[0] for box in boxes), min(box[1] for box in boxes),
                  max(box[2] for box in boxes), max(box[3] for box in boxes)]
        clean_paths = {"basemap": str(qgz.resolve()), "population": str(pop_tif.resolve()), "terrain": str(terrain_tif.resolve())}
        if terrain_dtm_tif:
            clean_paths["terrain_dtm"] = str(terrain_dtm_tif.resolve())
        clean_paths.update({
            key: str(Path(paths[key]).resolve()) for key in REFERENCE_SOURCE_KEYS if paths.get(key)
        })
        for key in OPTIONAL_VECTOR_SOURCE_KEYS:
            if paths.get(key):
                clean_paths[key] = str(
                    Path(paths[key]).resolve()
                )
        if persist:
            DataSourceRepository(self.settings_path).save(clean_paths)
        wgs84_extents = layer_extents(local, project, self.wgs84)
        pop_extent = QgsCoordinateTransform(population.crs(), self.wgs84, project).transformBoundingBox(population.extent())
        terrain_extent = QgsCoordinateTransform(terrain.crs(), self.wgs84, project).transformBoundingBox(terrain.extent())
        terrain_dtm_extent = QgsCoordinateTransform(terrain_dtm.crs(), self.wgs84, project).transformBoundingBox(terrain_dtm.extent()) if terrain_dtm else None
        online_sources = self._online_sources(project)
        return LoadedSources(
            project=project, population=population, terrain=terrain, terrain_dtm=terrain_dtm,
            paths=clean_paths, raster_info=population_info, terrain_info=terrain_info,
            terrain_dtm_info=terrain_dtm_info, vector_info=vector_info,
            local_layers=local, layers=layers, bounds=bounds,
            layer_boxes={item["id"]: QgsRectangle(*item["bbox"]) for item in layers},
            layer_boxes_wgs84=wgs84_extents,
            population_bbox_wgs84=self._bbox(pop_extent),
            terrain_bbox_wgs84=self._bbox(terrain_extent),
            terrain_dtm_bbox_wgs84=self._bbox(terrain_dtm_extent) if terrain_dtm_extent else [],
            # The configured QGIS project is the current display-only airspace source.
            # Its layer names/colors must never manufacture planning constraints.
            hard_constraints=hard_constraints(
                local, wgs84_extents, excluded_layer_ids={layer.id() for layer in local},
            ),
            online_sources=online_sources,
        )

    @staticmethod
    def vector_source_info(value, role):
        """建筑单体 / 建筑环境网格的只读来源描述。

        配置值可以是 ``.gpkg`` / ``.shp`` / ``.geojson``，也可以是**引用建筑图层的 QGIS
        工程**（``.qgz`` / ``.qgs``）。GeoPackage 走既有的严格 schema 校验；其它格式走通用
        矢量发现并显式标注"深层字段未校验"，绝不把未校验的源说成已验证。
        """

        resolved = resolve_vector_layer_source(value, role=role)
        if not resolved.get("ok"):
            return {
                "status": "failed", "reason": resolved.get("reason"),
                "path": str(value), "configured_path": str(value),
                "via": resolved.get("source"), "schema_verified": False,
            }
        resolved_path = resolved.get("path")
        layer_name = resolved.get("layer_name")
        suffix = str(resolved.get("format") or "").lower()
        common = {
            "configured_path": str(value), "via": resolved.get("source"),
            "project_path": resolved.get("project_path"), "layer_name": layer_name,
        }
        if suffix == ".gpkg":
            try:
                info = dict(inspect_geopackage(resolved_path, role))
            except (OSError, ValueError) as exc:
                return {
                    **common, "status": "failed", "reason": str(exc),
                    "path": resolved_path, "schema_verified": False,
                }
            return {**info, **common, "schema_verified": True}
        try:
            info = dict(discover_vector_layer(resolved_path, layer_name))
        except (OSError, ValueError, RuntimeError) as exc:
            return {
                **common, "status": "failed", "reason": str(exc),
                "path": resolved_path, "schema_verified": False,
            }
        info.pop("_features", None)
        return {
            **info, **common, "status": "passed", "schema_verified": False,
            "schema_note": f"{role} 的深层字段校验只在 GeoPackage 上执行",
        }

    @staticmethod
    def _validate_paths(paths):
        """统一路径校验：exists + is_file + 角色允许的格式（支持 .qgz/.gpkg/.shp/.geojson）。"""

        for role in CHECKED_FILE_ROLES:
            record = resolve_source_path(paths.get(role), role=role)
            if record["path"] is None:
                if role in REQUIRED_FILE_ROLES:
                    raise ValueError(f"{record['label']}必须配置")
                continue
            if record["status"] != STATUS_OK:
                raise ValueError(record["reason"])
        # 建筑类来源还要在启动时确认"能解析出可用图层"：路径存在但工程里没有可用矢量图层，
        # 同样属于"路径存在但状态异常"，必须在加载阶段就说清楚。
        for role in BUILDING_VECTOR_SOURCE_KEYS:
            if not paths.get(role):
                continue
            resolved = resolve_vector_layer_source(paths[role], role=role)
            if not resolved.get("ok"):
                raise ValueError(
                    f"{'建筑单体' if role == 'buildings' else '建筑环境网格'}："
                    f"{resolved.get('reason')}"
                )
        reference_formats = {
            "reference_landing_sites": (".xlsx", ".csv", ".et"),
            "reference_routes": (".csv", ".xlsx", ".geojson", ".json", ".et"),
        }
        for key, suffixes in reference_formats.items():
            value = paths.get(key)
            if not value:
                continue
            source_path = Path(value)
            if not source_path.is_file():
                raise ValueError(f"{key} 请选择存在的具体文件")
            if source_path.suffix.lower() not in suffixes:
                raise ValueError(f"{key} 文件格式不支持")
        qgz = Path(paths["basemap"])
        population = Path(paths["population"])
        terrain = Path(paths["terrain"])
        terrain_dtm = Path(paths["terrain_dtm"]) if paths.get("terrain_dtm") else None
        return qgz, population, terrain, terrain_dtm

    @staticmethod
    def _load_terrain_dtm(path):
        raster = QgsRasterLayer(str(path), "FABDEM bare-earth DTM")
        if not raster.isValid() or not raster.crs().isValid():
            raise ValueError("FABDEM DTM 无效或缺少 CRS")
        dataset = gdal.Open(str(path), gdal.GA_ReadOnly)
        if dataset is None or dataset.RasterCount < 1:
            raise ValueError("无法读取 FABDEM DTM 栅格")
        band, transform = dataset.GetRasterBand(1), dataset.GetGeoTransform()
        metadata = dataset.GetMetadata() or {}
        profile = deepcopy(FABDEM_V12)
        observed_crs = raster.crs().authid() or raster.crs().description()
        observed_pixel_size = [abs(transform[1]), abs(transform[5])]
        profile["crs"] = {
            **profile["crs"], "observed": observed_crs,
            "observed_vertical": metadata.get("vertical_reference") or "unknown",
        }
        profile["resolution"] = {**profile["resolution"], "observed_pixel_size": observed_pixel_size}
        profile["provenance"] = {**profile["provenance"], "path": str(path), "metadata": metadata}
        profile["verification"] = {
            **profile["verification"],
            "status": "verified_from_raster_metadata" if metadata.get("vertical_reference") == "EGM2008_orthometric" else "pending_confirmation",
        }
        corners = [
            gdal.ApplyGeoTransform(transform, 0, 0),
            gdal.ApplyGeoTransform(transform, dataset.RasterXSize, 0),
            gdal.ApplyGeoTransform(transform, 0, dataset.RasterYSize),
            gdal.ApplyGeoTransform(transform, dataset.RasterXSize, dataset.RasterYSize),
        ]
        info = {
            "width": dataset.RasterXSize, "height": dataset.RasterYSize,
            "bands": dataset.RasterCount, "band": 1,
            "crs": observed_crs, "nodata": band.GetNoDataValue(),
            "pixel_size": observed_pixel_size,
            "dtype": gdal.GetDataTypeName(band.DataType),
            "extent": [min(p[0] for p in corners), min(p[1] for p in corners), max(p[0] for p in corners), max(p[1] for p in corners)],
            "unit": band.GetUnitType() or metadata.get("vertical_unit") or "m",
            "quantity": "bare_earth_elevation", "surface_model": "DTM",
            "vertical_reference": metadata.get("vertical_reference") or "unknown",
            "vertical_status": "confirmed" if metadata.get("vertical_reference") == "EGM2008_orthometric" else "pending_confirmation",
            "source_metadata": metadata, "source_profile": profile,
        }
        dataset = None
        return raster, info

    @staticmethod
    def _load_population(path):
        raster = QgsRasterLayer(str(path), "WorldPop 人口数（源像元）")
        if not raster.isValid() or not raster.crs().isValid():
            raise ValueError("人口 GeoTIFF 无效或缺少 CRS")
        dataset = gdal.Open(str(path), gdal.GA_ReadOnly)
        if dataset is None:
            raise ValueError("无法读取人口栅格")
        band, transform = dataset.GetRasterBand(1), dataset.GetGeoTransform()
        profile = deepcopy(WORLDPOP_R2025A)
        observed_crs = raster.crs().authid() or raster.crs().description()
        observed_pixel_size = [abs(transform[1]), abs(transform[5])]
        metadata = dataset.GetMetadata() or {}
        profile["crs"] = {**profile["crs"], "observed": observed_crs}
        profile["resolution"] = {**profile["resolution"], "observed_pixel_size": observed_pixel_size}
        profile["provenance"] = {**profile["provenance"], "path": str(path), "metadata": metadata}
        info = {
            "width": dataset.RasterXSize, "height": dataset.RasterYSize,
            "bands": dataset.RasterCount,
            "crs": observed_crs,
            "nodata": str(band.GetNoDataValue()),
            "pixel_size": observed_pixel_size,
            "unit_metadata": band.GetUnitType() or "未标注",
            "unit": "person/source_pixel",
            "quantity": "population_count_per_source_pixel",
            "version": profile["version"],
            "resolution": profile["resolution"],
            "verification": profile["verification"],
            "source_profile": profile,
            "source_metadata": metadata,
            "rendering_semantics": "source_pixel_count",
        }
        items = []
        for count, color_hex, alpha in ((0, "#fff7ec", 0), (1, "#fee391", 110), (10, "#fec44f", 175), (50, "#f03b20", 230), (200, "#99000d", 255)):
            color = QColor(color_hex); color.setAlpha(alpha)
            items.append(QgsColorRampShader.ColorRampItem(count, color, str(count)))
        ramp = QgsColorRampShader(); ramp.setColorRampType(QgsColorRampShader.Interpolated); ramp.setColorRampItemList(items)
        shader = QgsRasterShader(0, 200); shader.setRasterShaderFunction(ramp)
        raster.setRenderer(QgsSingleBandPseudoColorRenderer(raster.dataProvider(), 1, shader))
        dataset = None
        return raster, info

    @staticmethod
    def _load_terrain(path):
        raster = QgsRasterLayer(str(path), "Copernicus GLO-30 DSM")
        if not raster.isValid() or not raster.crs().isValid():
            raise ValueError("地形 DEM 无效或缺少 CRS")
        dataset = gdal.Open(str(path), gdal.GA_ReadOnly)
        if dataset is None:
            raise ValueError("无法读取地形 DEM")
        band, transform = dataset.GetRasterBand(1), dataset.GetGeoTransform()
        metadata = dataset.GetMetadata() or {}
        profile = deepcopy(COPERNICUS_GLO30)
        observed_crs = raster.crs().authid() or raster.crs().description()
        observed_pixel_size = [abs(transform[1]), abs(transform[5])]
        profile["crs"] = {**profile["crs"], "observed": observed_crs}
        profile["resolution"] = {**profile["resolution"], "observed_pixel_size": observed_pixel_size}
        profile["provenance"] = {**profile["provenance"], "path": str(path), "metadata": metadata}
        info = {"width": dataset.RasterXSize, "height": dataset.RasterYSize, "bands": dataset.RasterCount,
                "crs": observed_crs, "nodata": str(band.GetNoDataValue()),
                "pixel_size": observed_pixel_size, "unit": "m", "quantity": "surface_elevation",
                "source": "Copernicus DEM GLO-30", "surface_model": "DSM", "version": profile["version"],
                "resolution": profile["resolution"], "verification": profile["verification"],
                "vertical_crs": "EPSG:3855", "vertical_datum": "EGM2008", "source_profile": profile,
                "source_metadata": metadata}
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
