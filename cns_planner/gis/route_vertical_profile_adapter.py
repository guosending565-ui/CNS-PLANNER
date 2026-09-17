"""Read-only FABDEM point sampler for route profile visualization."""

from __future__ import annotations

import math
from pathlib import Path


class FabdemRouteSampler:
    def __init__(self, path, gdal=None, osr=None):
        if gdal is None or osr is None:
            from osgeo import gdal as runtime_gdal, osr as runtime_osr
            gdal, osr = runtime_gdal, runtime_osr

        self.path = Path(path).expanduser().resolve()
        if not self.path.is_file():
            raise ValueError("terrain_dtm 必须是存在的具体文件")
        self.dataset = gdal.Open(str(self.path), gdal.GA_ReadOnly)
        if self.dataset is None or getattr(self.dataset, "RasterCount", 1) < 1:
            raise ValueError("无法以只读方式打开 FABDEM terrain_dtm")
        self.band = self.dataset.GetRasterBand(1)
        self.transform = self.dataset.GetGeoTransform()
        self.inverse = gdal.InvGeoTransform(self.transform)
        if isinstance(self.inverse, tuple) and len(self.inverse) == 2 and isinstance(self.inverse[0], (bool, int)):
            ok, self.inverse = self.inverse
            if not ok:
                raise ValueError("FABDEM DTM 仿射变换不可逆")
        self.nodata = self.band.GetNoDataValue()
        metadata = self.dataset.GetMetadata() if hasattr(self.dataset, "GetMetadata") else {}
        self.vertical_reference = str((metadata or {}).get("vertical_reference") or "unknown")
        self.vertical_status = "confirmed" if self.vertical_reference.lower() == "egm2008_orthometric" else "pending_confirmation"
        target = osr.SpatialReference()
        target.ImportFromWkt(self.dataset.GetProjection())
        source = osr.SpatialReference()
        source.ImportFromEPSG(4326)
        for item in (source, target):
            if hasattr(item, "SetAxisMappingStrategy"):
                item.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        self.to_source = osr.CoordinateTransformation(source, target)

    def sample_wgs84(self, coordinate):
        if self.vertical_status != "confirmed":
            return {"status": "missing_data", "ground_egm2008_m": None, "reason": "vertical_datum_unresolved"}
        try:
            x, y, *_ = self.to_source.TransformPoint(float(coordinate[0]), float(coordinate[1]))
            pixel, line = _apply(self.inverse, x, y)
            column, row = int(math.floor(pixel)), int(math.floor(line))
            if column < 0 or row < 0 or column >= self.dataset.RasterXSize or row >= self.dataset.RasterYSize:
                return {"status": "missing_data", "ground_egm2008_m": None, "reason": "outside_dtm_extent"}
            values = self.band.ReadAsArray(column, row, 1, 1)
            if values is None:
                return {"status": "missing_data", "ground_egm2008_m": None, "reason": "dtm_read_failed"}
            value = float(values[0][0])
            if not math.isfinite(value) or self.nodata is not None and math.isclose(value, float(self.nodata), rel_tol=0, abs_tol=1e-9):
                return {"status": "missing_data", "ground_egm2008_m": None, "reason": "dtm_nodata"}
            scale = self.band.GetScale() if hasattr(self.band, "GetScale") else None
            offset = self.band.GetOffset() if hasattr(self.band, "GetOffset") else None
            value = value * (1.0 if scale is None else float(scale)) + (0.0 if offset is None else float(offset))
            return {"status": "passed", "ground_egm2008_m": value, "reason": "FABDEM nearest source pixel"}
        except (TypeError, ValueError, RuntimeError, IndexError):
            return {"status": "missing_data", "ground_egm2008_m": None, "reason": "dtm_sample_failed"}

    def describe(self):
        stat = self.path.stat()
        return {
            "dataset": "FABDEM", "path": str(self.path),
            "mtime_ns": stat.st_mtime_ns, "size": stat.st_size,
            "vertical_reference": self.vertical_reference,
            "vertical_status": self.vertical_status,
            "sampling": "nearest_source_pixel_read_only",
        }


def _apply(transform, x, y):
    return transform[0] + transform[1] * x + transform[2] * y, transform[3] + transform[4] * x + transform[5] * y
