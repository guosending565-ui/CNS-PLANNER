"""QGIS/GDAL boundary for indexed building-clearance evidence extraction."""

from __future__ import annotations

import json
import math
from pathlib import Path

from .metric_crs import require_metric_crs
from .source_inspection import inspect_geopackage


class QgisBuildingClearanceAdapter:
    """Query only route-corridor buildings and produce exact polygon evidence."""

    def __init__(self, buildings_path, terrain_dtm_path, *, horizontal_crs=None,
                 geographic_bounds=None):
        from qgis.core import (
            QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeatureRequest,
            QgsGeometry, QgsPointXY, QgsProject, QgsRectangle, QgsVectorLayer,
        )

        self.QgsFeatureRequest = QgsFeatureRequest
        self.QgsGeometry, self.QgsPointXY, self.QgsRectangle = QgsGeometry, QgsPointXY, QgsRectangle
        self.buildings_path = str(Path(buildings_path).resolve())
        self.terrain_dtm_path = str(Path(terrain_dtm_path).resolve())
        self.building_info = inspect_geopackage(self.buildings_path, "buildings")
        self.layer = QgsVectorLayer(f"{self.buildings_path}|layername=buildings", "GBA buildings", "ogr")
        if not self.layer.isValid():
            raise ValueError("无法读取 buildings GeoPackage/buildings 图层")
        fields = {field.name() for field in self.layer.fields()}
        required = {"id", "source", "height_m", "height_var", "height_status"}
        if not required <= fields:
            raise ValueError("buildings 图层缺少关键字段：" + ", ".join(sorted(required - fields)))
        self.wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        source_crs = self.layer.crs()
        if geographic_bounds is None and source_crs.isGeographic():
            extent = self.layer.extent()
            geographic_bounds = [
                extent.xMinimum(), extent.yMinimum(), extent.xMaximum(), extent.yMaximum(),
            ]
        if horizontal_crs is None and not source_crs.isGeographic():
            horizontal_crs = source_crs.authid()
        self.horizontal_crs = require_metric_crs(
            explicit_crs=horizontal_crs, geographic_bounds=geographic_bounds,
        )
        self.metric = QgsCoordinateReferenceSystem(self.horizontal_crs)
        context = QgsProject.instance().transformContext()
        self.to_metric = QgsCoordinateTransform(self.layer.crs(), self.metric, context)
        self.metric_to_source = QgsCoordinateTransform(self.metric, self.layer.crs(), context)
        self.sampler = DtmFootprintSampler(self.terrain_dtm_path)
        self._route_cache = {}

    def provenance(self):
        return {
            "buildings": {
                "path": self.buildings_path, "layer": "buildings",
                "size_bytes": Path(self.buildings_path).stat().st_size,
                "mtime_ns": Path(self.buildings_path).stat().st_mtime_ns,
                "feature_count": self.building_info.get("feature_count"),
                "valid_height_count": self.building_info.get("valid_height_count"),
                "valid_height_fraction": self.building_info.get("valid_height_fraction"),
                "crs": self.building_info.get("crs"), "extent": self.building_info.get("extent"),
                "query": "QgsFeatureRequest.filterRect/provider_spatial_index_then_exact_geometry",
                "height_semantics": "GBA predicted height_m; height_var retained raw only",
            },
            "terrain_dtm": self.sampler.describe(),
            "roof_formula": "FABDEM_DTM_footprint_median + GBA_height_m",
            "forbidden_source": "GLO30_DSM_not_used_for_roof",
        }

    def route_candidates(self, route, horizontal_clearance_m, policy):
        route_source, route_metric = self._route_geometries(route)
        corridor_metric = route_metric.buffer(float(horizontal_clearance_m), 12)
        query_source = corridor_metric.boundingBox()
        query_rect = self.metric_to_source.transformBoundingBox(query_source)
        query_bbox = [query_rect.xMinimum(), query_rect.yMinimum(), query_rect.xMaximum(), query_rect.yMaximum()]
        if not _bbox_contains(self.building_info.get("extent"), query_bbox):
            return {
                "status": "missing_data", "candidates": [],
                "query": {"bbox": query_bbox, "reason": "route_corridor_outside_building_source_coverage"},
            }
        request = self.QgsFeatureRequest().setFilterRect(query_rect)
        candidates = []
        min_height = policy.get("min_building_height_m")
        for feature in self.layer.getFeatures(request):
            footprint_source = feature.geometry()
            if footprint_source is None or footprint_source.isEmpty():
                continue
            footprint_metric = self.QgsGeometry(footprint_source)
            footprint_metric.transform(self.to_metric)
            horizontal = route_metric.distance(footprint_metric)
            if not math.isfinite(horizontal) or horizontal > float(horizontal_clearance_m):
                continue
            height = _number_or_none(feature["height_m"])
            if min_height is not None and height is not None and height < float(min_height):
                continue
            buffered = footprint_metric.buffer(float(horizontal_clearance_m), 12)
            intersection = route_metric.intersection(buffered)
            intervals = self._intervals(route_metric, intersection)
            if not intervals:
                continue
            nearest = route_metric.nearestPoint(footprint_metric)
            nearest_distance = route_metric.lineLocatePoint(nearest)
            for interval in intervals:
                interval["closest_distance_along_route_m"] = max(
                    interval["start_distance_m"],
                    min(interval["end_distance_m"], float(nearest_distance)),
                )
            terrain = self.sampler.sample_footprint(footprint_source, self.layer.crs())
            relief_threshold = policy.get("terrain_relief_review_m")
            if (
                relief_threshold is not None and policy.get("status") == "confirmed"
                and terrain.get("ground_relief_m") is not None
                and terrain["ground_relief_m"] > float(relief_threshold)
            ):
                terrain.setdefault("warnings", []).append("terrain_relief_exceeds_confirmed_review_threshold")
                terrain["terrain_support_quality"] = "review_required"
            candidates.append({
                "building_id": str(feature["id"] or feature.id()),
                "source": str(feature["source"] or "GBA"),
                "height_m": height, "height_var": _number_or_none(feature["height_var"]),
                "height_status": str(feature["height_status"] or "unknown"),
                "horizontal_minimum_m": float(horizontal),
                "affected_intervals": intervals,
                "terrain": terrain,
                "geometry": json.loads(footprint_source.asJson()),
                "evidence": {
                    "feature_id": int(feature.id()), "layer": "buildings",
                    "geometry_method": "polygon_buffer_route_intersection",
                    "horizontal_crs": self.horizontal_crs,
                    "height_var_semantics": "raw_source_field_only",
                },
            })
        return {
            "status": "passed", "candidates": candidates,
            "query": {
                "bbox": query_bbox,
                "candidate_count": len(candidates), "spatial_index_required": True,
            },
        }

    def route_surface_elevation(self, route, distance_along_route_m):
        _, metric = self._route_geometries(route)
        point_metric = metric.interpolate(float(distance_along_route_m))
        if point_metric.isEmpty():
            return None
        point_source = self.QgsGeometry(point_metric)
        point_source.transform(self.metric_to_source)
        point = point_source.asPoint()
        return self.sampler.sample_point(point.x(), point.y(), self.layer.crs())

    def _route_geometries(self, route):
        route_id = str(route.get("route_id") or id(route))
        key = (route_id, json.dumps(route.get("path") or []))
        if key in self._route_cache:
            return self._route_cache[key]
        source = self.QgsGeometry.fromPolylineXY([
            self.QgsPointXY(float(point[0]), float(point[1])) for point in route.get("path") or []
        ])
        metric = self.QgsGeometry(source)
        metric.transform(self.to_metric)
        self._route_cache = {key: (source, metric)}
        return source, metric

    def _intervals(self, route_metric, intersection):
        if intersection is None or intersection.isEmpty():
            return []
        parts = intersection.asGeometryCollection() or [intersection]
        result = []
        for part in parts:
            vertices = list(part.vertices())
            if not vertices:
                continue
            distances = [
                float(route_metric.lineLocatePoint(
                    self.QgsGeometry.fromPointXY(self.QgsPointXY(point.x(), point.y()))
                )) for point in vertices
            ]
            start, end = min(distances), max(distances)
            if end < start:
                start, end = end, start
            path_metric = route_metric.curveSubstring(start, end)
            path_source = self.QgsGeometry(path_metric)
            path_source.transform(self.metric_to_source)
            path = [[point.x(), point.y()] for point in path_source.vertices()]
            result.append({
                "start_distance_m": start, "end_distance_m": end,
                "path": path,
            })
        return _merge_intervals(result)


class DtmFootprintSampler:
    def __init__(self, path):
        from osgeo import gdal, ogr, osr

        self.gdal, self.ogr, self.osr = gdal, ogr, osr
        self.path = str(Path(path).resolve())
        self.dataset = gdal.Open(self.path, gdal.GA_ReadOnly)
        if self.dataset is None or self.dataset.RasterCount < 1:
            raise ValueError("无法读取 FABDEM DTM")
        self.band = self.dataset.GetRasterBand(1)
        self.transform = self.dataset.GetGeoTransform()
        self.inverse = gdal.InvGeoTransform(self.transform)
        if isinstance(self.inverse, tuple) and len(self.inverse) == 2 and isinstance(self.inverse[0], (bool, int)):
            ok, self.inverse = self.inverse
            if not ok:
                raise ValueError("FABDEM DTM 仿射变换不可逆")
        self.projection = self.dataset.GetProjection()
        if not self.projection:
            raise ValueError("FABDEM DTM 缺少 CRS")
        metadata = self.dataset.GetMetadata() or {}
        self.vertical_reference = str(metadata.get("vertical_reference") or "unknown")
        self.vertical_status = (
            "confirmed" if self.vertical_reference.lower() == "egm2008_orthometric" else "unresolved"
        )

    def describe(self):
        band = self.band
        metadata = self.dataset.GetMetadata() or {}
        return {
            "path": self.path, "driver": self.dataset.GetDriver().ShortName,
            "size_bytes": Path(self.path).stat().st_size,
            "mtime_ns": Path(self.path).stat().st_mtime_ns,
            "width": int(self.dataset.RasterXSize), "height": int(self.dataset.RasterYSize),
            "bands": int(self.dataset.RasterCount), "dtype": self.gdal.GetDataTypeName(band.DataType),
            "nodata": band.GetNoDataValue(),
            "pixel_size": [abs(self.transform[1]), abs(self.transform[5])],
            "extent": self._extent(), "crs_wkt": self.projection,
            "vertical_reference": self.vertical_reference,
            "vertical_status": self.vertical_status,
            "source_metadata": metadata,
        }

    def sample_footprint(self, geometry, geometry_crs):
        if self.vertical_status != "confirmed":
            return self._unresolved("vertical_datum_unresolved")
        transformed = self._transform_qgis_geometry(geometry, geometry_crs)
        bbox = transformed.boundingBox()
        window = self._window([bbox.xMinimum(), bbox.yMinimum(), bbox.xMaximum(), bbox.yMaximum()])
        if window is None:
            return self._unresolved("outside_dtm_coverage")
        x0, y0, x1, y1 = window
        values = self.band.ReadAsArray(x0, y0, x1 - x0, y1 - y0)
        if values is None:
            return self._unresolved("dtm_read_failed")
        mask_dataset = self.gdal.GetDriverByName("MEM").Create("", x1 - x0, y1 - y0, 1, self.gdal.GDT_Byte)
        origin = self.gdal.ApplyGeoTransform(self.transform, x0, y0)
        mask_dataset.SetGeoTransform((origin[0], self.transform[1], self.transform[2], origin[1], self.transform[4], self.transform[5]))
        mask_dataset.SetProjection(self.projection)
        memory = self.ogr.GetDriverByName("Memory").CreateDataSource("")
        srs = self.osr.SpatialReference(); srs.ImportFromWkt(self.projection)
        layer = memory.CreateLayer("footprint", srs=srs, geom_type=self.ogr.wkbUnknown)
        feature = self.ogr.Feature(layer.GetLayerDefn())
        feature.SetGeometry(self.ogr.CreateGeometryFromWkt(transformed.asWkt()))
        layer.CreateFeature(feature)
        self.gdal.RasterizeLayer(mask_dataset, [1], layer, burn_values=[1], options=["ALL_TOUCHED=TRUE"])
        mask = mask_dataset.GetRasterBand(1).ReadAsArray()
        valid = self._valid_masked_values(values, mask, self.band.GetNoDataValue())
        if not valid:
            return self._unresolved("dtm_nodata_under_footprint")
        scale, offset = self.band.GetScale(), self.band.GetOffset()
        scale = 1.0 if scale is None else float(scale)
        offset = 0.0 if offset is None else float(offset)
        valid = [value * scale + offset for value in valid]
        valid.sort()
        middle = len(valid) // 2
        median = valid[middle] if len(valid) % 2 else (valid[middle - 1] + valid[middle]) / 2
        return {
            "dtm_status": "passed", "terrain_support_quality": "supported",
            "ground_elevation_median_m": median,
            "ground_elevation_min_m": valid[0], "ground_elevation_max_m": valid[-1],
            "ground_relief_m": valid[-1] - valid[0],
            "dtm_valid_pixel_count": len(valid), "warnings": [],
            "vertical_reference": "egm2008_orthometric",
            "sampling": "footprint_mask_median",
        }

    def sample_point(self, x, y, geometry_crs):
        if self.vertical_status != "confirmed":
            return None
        from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY, QgsProject
        target = QgsCoordinateReferenceSystem.fromWkt(self.projection)
        point = QgsCoordinateTransform(geometry_crs, target, QgsProject.instance().transformContext()).transform(QgsPointXY(x, y))
        px, py = self.gdal.ApplyGeoTransform(self.inverse, point.x(), point.y())
        column, row = int(math.floor(px)), int(math.floor(py))
        if not (0 <= column < self.dataset.RasterXSize and 0 <= row < self.dataset.RasterYSize):
            return None
        values = self.band.ReadAsArray(column, row, 1, 1)
        if values is None:
            return None
        value = float(values[0][0])
        nodata = self.band.GetNoDataValue()
        if not math.isfinite(value) or (nodata is not None and value == float(nodata)):
            return None
        scale, offset = self.band.GetScale(), self.band.GetOffset()
        return value * (1.0 if scale is None else scale) + (0.0 if offset is None else offset)

    @staticmethod
    def _valid_masked_values(values, mask, nodata):
        value_rows = values.tolist() if hasattr(values, "tolist") else values
        mask_rows = mask.tolist() if hasattr(mask, "tolist") else mask
        result = []
        for value_row, mask_row in zip(value_rows, mask_rows):
            for raw, selected in zip(value_row, mask_row):
                value = float(raw)
                if not selected or not math.isfinite(value):
                    continue
                if nodata is not None and value == float(nodata):
                    continue
                result.append(value)
        return result

    def _transform_qgis_geometry(self, geometry, geometry_crs):
        from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry, QgsProject
        target = QgsCoordinateReferenceSystem.fromWkt(self.projection)
        result = QgsGeometry(geometry)
        if geometry_crs != target:
            result.transform(QgsCoordinateTransform(geometry_crs, target, QgsProject.instance().transformContext()))
        return result

    def _window(self, bbox):
        points = [
            self.gdal.ApplyGeoTransform(self.inverse, bbox[0], bbox[1]),
            self.gdal.ApplyGeoTransform(self.inverse, bbox[0], bbox[3]),
            self.gdal.ApplyGeoTransform(self.inverse, bbox[2], bbox[1]),
            self.gdal.ApplyGeoTransform(self.inverse, bbox[2], bbox[3]),
        ]
        x0 = max(0, math.floor(min(point[0] for point in points)))
        y0 = max(0, math.floor(min(point[1] for point in points)))
        x1 = min(self.dataset.RasterXSize, math.ceil(max(point[0] for point in points)))
        y1 = min(self.dataset.RasterYSize, math.ceil(max(point[1] for point in points)))
        return None if x0 >= x1 or y0 >= y1 else (x0, y0, x1, y1)

    def _extent(self):
        corners = [
            self.gdal.ApplyGeoTransform(self.transform, 0, 0),
            self.gdal.ApplyGeoTransform(self.transform, self.dataset.RasterXSize, 0),
            self.gdal.ApplyGeoTransform(self.transform, 0, self.dataset.RasterYSize),
            self.gdal.ApplyGeoTransform(self.transform, self.dataset.RasterXSize, self.dataset.RasterYSize),
        ]
        return [min(p[0] for p in corners), min(p[1] for p in corners), max(p[0] for p in corners), max(p[1] for p in corners)]

    @staticmethod
    def _unresolved(reason):
        return {
            "dtm_status": "unresolved", "terrain_support_quality": "unknown",
            "ground_elevation_median_m": None, "ground_elevation_min_m": None,
            "ground_elevation_max_m": None, "ground_relief_m": None,
            "dtm_valid_pixel_count": 0, "warnings": [reason],
            "vertical_reference": "unknown", "sampling": "footprint_mask_median",
        }


def _merge_intervals(items):
    result = []
    for item in sorted(items, key=lambda value: value["start_distance_m"]):
        if result and item["start_distance_m"] <= result[-1]["end_distance_m"] + 0.01:
            result[-1]["end_distance_m"] = max(result[-1]["end_distance_m"], item["end_distance_m"])
            if item.get("path"):
                result[-1]["path"] = [*result[-1].get("path", []), *item["path"]]
        else:
            result.append(dict(item))
    return result


def _number_or_none(value):
    if value in (None, ""):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _bbox_contains(outer, inner):
    return bool(
        outer and all(value is not None for value in outer)
        and outer[0] <= inner[0] and outer[1] <= inner[1]
        and outer[2] >= inner[2] and outer[3] >= inner[3]
    )
