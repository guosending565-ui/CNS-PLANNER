"""Read-only GDAL adapter for sampling WGS84 grid-cell bounding boxes."""

from __future__ import annotations

import math
from pathlib import Path

from ..domain.quantities import geographic_bbox_area_m2


class GdalRasterAdapter:
    """Expose raster metadata and valid values without leaking GDAL into services."""

    def __init__(self, source_path, gdal_module=None, osr_module=None):
        if gdal_module is None or osr_module is None:
            from osgeo import gdal, osr

            gdal_module = gdal_module or gdal
            osr_module = osr_module or osr
        self.gdal = gdal_module
        self.osr = osr_module
        self.path = Path(source_path)
        self.dataset = self.gdal.Open(str(self.path), self.gdal.GA_ReadOnly)
        if self.dataset is None:
            raise ValueError(f"无法读取栅格：{self.path}")
        if self.dataset.RasterCount < 1:
            raise ValueError("栅格没有可采样波段")
        self.band = self.dataset.GetRasterBand(1)
        self.transform = self.dataset.GetGeoTransform()
        self.inverse_transform = self._inverse(self.transform)
        projection = self.dataset.GetProjection()
        if not projection:
            raise ValueError("栅格缺少 CRS")
        self.source_srs = self.osr.SpatialReference()
        if self.source_srs.ImportFromWkt(projection) != 0:
            raise ValueError("无法解析栅格 CRS")
        self.wgs84_srs = self.osr.SpatialReference()
        self.wgs84_srs.ImportFromEPSG(4326)
        axis_strategy = getattr(self.osr, "OAMS_TRADITIONAL_GIS_ORDER", None)
        if axis_strategy is not None:
            self.source_srs.SetAxisMappingStrategy(axis_strategy)
            self.wgs84_srs.SetAxisMappingStrategy(axis_strategy)
        self.to_source = self.osr.CoordinateTransformation(self.wgs84_srs, self.source_srs)
        self.to_wgs84 = self.osr.CoordinateTransformation(self.source_srs, self.wgs84_srs)
        self.nodata = self.band.GetNoDataValue()
        self.scale = self.band.GetScale()
        self.offset = self.band.GetOffset()

    def describe(self):
        authority_name = getattr(self.source_srs, "GetAuthorityName", lambda _: None)(None)
        authority_code = getattr(self.source_srs, "GetAuthorityCode", lambda _: None)(None)
        crs = f"{authority_name}:{authority_code}" if authority_name and authority_code else self.dataset.GetProjection()
        unit = (self.band.GetUnitType() or "").strip()
        return {
            "path": str(self.path),
            "crs": crs,
            "band": 1,
            "width": int(self.dataset.RasterXSize),
            "height": int(self.dataset.RasterYSize),
            "pixel_size": [abs(float(self.transform[1])), abs(float(self.transform[5]))],
            "nodata": self.nodata,
            "unit_metadata": unit or None,
            "scale": 1.0 if self.scale is None else float(self.scale),
            "offset": 0.0 if self.offset is None else float(self.offset),
        }

    def read_population_count(self, bbox):
        """Conservatively allocate source-pixel population counts to a WGS84 bbox.

        Counts are distributed by the geodesic-area fraction of each source pixel
        intersecting the target cell.  No interpolation is used.  The footprint
        bbox is exact for the configured north-up WGS84 WorldPop product and is a
        documented approximation for transformed/rotated source rasters.
        """
        target = [float(value) for value in bbox]
        target_area = geographic_bbox_area_m2(target)
        window = self._window(target)
        outside = {
            "status": "missing_data", "value_status": "missing_data",
            "coverage_status": "outside_extent", "population_count_people": None,
            "target_area_m2": target_area, "valid_covered_area_m2": 0.0,
            "source_coverage_fraction": 0.0, "source_pixel_count": 0,
            "quality_flags": ["outside_source_extent"],
        }
        if window is None or target_area <= 0:
            return outside
        x0, y0, x1, y1 = window
        values = self.band.ReadAsArray(x0, y0, x1 - x0, y1 - y0)
        if values is None:
            return {
                **outside,
                "coverage_status": "nodata_only",
                "quality_flags": ["source_read_failed_or_nodata_only"],
            }
        rows = values.tolist() if hasattr(values, "tolist") else values
        scale = 1.0 if self.scale is None else float(self.scale)
        offset = 0.0 if self.offset is None else float(self.offset)
        count, covered_area, valid_pixels = 0.0, 0.0, 0
        for row_offset, row in enumerate(rows):
            for column_offset, raw in enumerate(row):
                value = float(raw)
                if not math.isfinite(value) or self._is_nodata(value):
                    continue
                pixel_bbox = self._pixel_wgs84_bbox(x0 + column_offset, y0 + row_offset)
                overlap = self._intersection_bbox(target, pixel_bbox)
                if overlap is None:
                    continue
                pixel_area = geographic_bbox_area_m2(pixel_bbox)
                overlap_area = geographic_bbox_area_m2(overlap)
                if pixel_area <= 0 or overlap_area <= 0:
                    continue
                adjusted = value * scale + offset
                if adjusted < 0:
                    continue
                count += adjusted * min(1.0, overlap_area / pixel_area)
                covered_area += overlap_area
                valid_pixels += 1
        if not valid_pixels:
            return {
                **outside,
                "coverage_status": "nodata_only",
                "quality_flags": ["no_valid_source_pixels"],
            }
        coverage = min(1.0, covered_area / target_area)
        coverage_status = "full" if coverage >= 0.999999 else "partial"
        return {
            # ``status`` is retained for old project/schema consumers and now
            # follows value validity. Coverage completeness is independent.
            "status": "passed" if coverage_status == "full" else "missing_data",
            "value_status": "passed",
            "coverage_status": coverage_status,
            "population_count_people": count,
            "target_area_m2": target_area,
            "valid_covered_area_m2": min(target_area, covered_area),
            "source_coverage_fraction": coverage,
            "source_pixel_count": valid_pixels,
            "quality_flags": [] if coverage_status == "full" else ["partial_source_coverage", "not_extrapolated"],
        }

    def read_values(self, bbox):
        west, south, east, north = (float(value) for value in bbox)
        window = self._window((west, south, east, north))
        if window is None:
            return []
        x0, y0, x1, y1 = window
        values = self.band.ReadAsArray(x0, y0, x1 - x0, y1 - y0)
        if values is None:
            return []
        rows = values.tolist() if hasattr(values, "tolist") else values
        centers = [
            self._apply(self.transform, column + 0.5, row + 0.5)
            for row in range(y0, y1)
            for column in range(x0, x1)
        ]
        wgs84_centers = self.to_wgs84.TransformPoints(centers)
        valid = []
        flat_values = [value for row in rows for value in row]
        scale = 1.0 if self.scale is None else float(self.scale)
        offset = 0.0 if self.offset is None else float(self.offset)
        for raw, point in zip(flat_values, wgs84_centers):
            value = float(raw)
            lon, lat = point[0], point[1]
            if not (west <= lon < east and south <= lat < north):
                continue
            if not math.isfinite(value) or self._is_nodata(value):
                continue
            valid.append(value * scale + offset)
        return valid

    def _window(self, bbox):
        west, south, east, north = (float(value) for value in bbox)
        border = []
        for step in range(5):
            ratio = step / 4
            lon = west + (east - west) * ratio
            lat = south + (north - south) * ratio
            border.extend(((lon, south), (lon, north), (west, lat), (east, lat)))
        projected = self.to_source.TransformPoints(border)
        pixels = [self._apply(self.inverse_transform, point[0], point[1]) for point in projected]
        x0 = max(0, math.floor(min(point[0] for point in pixels)))
        y0 = max(0, math.floor(min(point[1] for point in pixels)))
        x1 = min(self.dataset.RasterXSize, math.ceil(max(point[0] for point in pixels)))
        y1 = min(self.dataset.RasterYSize, math.ceil(max(point[1] for point in pixels)))
        if x0 >= x1 or y0 >= y1:
            return None
        return x0, y0, x1, y1

    def _pixel_wgs84_bbox(self, column, row):
        corners = [
            self._apply(self.transform, column, row),
            self._apply(self.transform, column + 1, row),
            self._apply(self.transform, column, row + 1),
            self._apply(self.transform, column + 1, row + 1),
        ]
        points = self.to_wgs84.TransformPoints(corners)
        longitudes = [point[0] for point in points]
        latitudes = [point[1] for point in points]
        return [min(longitudes), min(latitudes), max(longitudes), max(latitudes)]

    @staticmethod
    def _intersection_bbox(first, second):
        bbox = [max(first[0], second[0]), max(first[1], second[1]),
                min(first[2], second[2]), min(first[3], second[3])]
        return bbox if bbox[0] < bbox[2] and bbox[1] < bbox[3] else None

    def _is_nodata(self, value):
        if self.nodata is None:
            return False
        nodata = float(self.nodata)
        if math.isnan(nodata):
            return math.isnan(value)
        return value == nodata

    def _inverse(self, transform):
        inverse = self.gdal.InvGeoTransform(transform)
        if isinstance(inverse, tuple) and len(inverse) == 2 and isinstance(inverse[0], (bool, int)):
            success, inverse = inverse
            if not success:
                raise ValueError("栅格仿射变换不可逆")
        if not inverse or len(inverse) != 6:
            raise ValueError("栅格仿射变换不可逆")
        return inverse

    @staticmethod
    def _apply(transform, x, y):
        return (
            transform[0] + transform[1] * x + transform[2] * y,
            transform[3] + transform[4] * x + transform[5] * y,
        )
