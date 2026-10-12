"""FABDEM 地形窗口的**只读**采样适配（供 ``visualization_3d_service`` 使用）。

约定（与 `cns-gis-data-chain` 一致）：

* 只以 ``GA_ReadOnly`` 打开源栅格，永不改写源数据（``source_modified=false``）；
* **NoData 不填补、不当 0**：窗口里保留为 ``None`` 并计数，状态降级为
  ``partial`` / ``nodata``，由服务显式披露；
* ``resolution_m`` 不由度数臆测：本适配器只报告 ``pixel_size_deg`` 与由
  ``Geod`` 实测的相邻像元地面距离（``pixel_size_m``），不做 degree-as-meter；
* 垂向基准直接回显栅格元数据自述；不是 ``egm2008_orthometric`` 时状态为
  ``unresolved``，调用方**不得**把它当工程基准使用。

本模块不 import QGIS：只依赖 GDAL(Python)。
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

#: 单次窗口采样的硬上限（宽×高），防止一次请求把整幅 FABDEM 拉进内存。
MAX_WINDOW_CELLS = 1024 * 1024


class TerrainWindowUnavailable(RuntimeError):
    def __init__(self, code: str, message: str, **detail):
        super().__init__(message)
        self.code = code
        self.detail = detail or {}

    def as_dict(self):
        return {"code": self.code, "message": str(self), "detail": self.detail}


class TerrainWindowSource:
    """FABDEM DTM 的只读窗口采样器。"""

    def __init__(self, path):
        from osgeo import gdal

        self.gdal = gdal
        self.path = Path(path).resolve()
        if not self.path.is_file():
            raise TerrainWindowUnavailable(
                "terrain_dtm_missing", f"FABDEM DTM 不存在：{self.path}"
            )
        self.dataset = gdal.Open(str(self.path), gdal.GA_ReadOnly)
        if self.dataset is None or self.dataset.RasterCount < 1:
            raise TerrainWindowUnavailable(
                "terrain_dtm_unreadable", f"无法以只读方式打开 FABDEM DTM：{self.path}"
            )
        self.band = self.dataset.GetRasterBand(1)
        self.transform = self.dataset.GetGeoTransform()
        inverse = gdal.InvGeoTransform(self.transform)
        if isinstance(inverse, tuple) and len(inverse) == 2 and isinstance(inverse[0], (bool, int)):
            ok, inverse = inverse
            if not ok:
                raise TerrainWindowUnavailable("terrain_dtm_affine_not_invertible",
                                               "FABDEM 仿射变换不可逆")
        self.inverse = inverse
        self.width = int(self.dataset.RasterXSize)
        self.height = int(self.dataset.RasterYSize)
        self.nodata = self.band.GetNoDataValue()
        self.metadata = self.dataset.GetMetadata() or {}
        self.vertical_reference = str(self.metadata.get("vertical_reference") or "unknown")
        self.vertical_status = (
            "confirmed" if self.vertical_reference.lower() == "egm2008_orthometric"
            else "unresolved"
        )

    # ------------------------------------------------------------------ 元数据

    def _extent(self):
        west, north = self.transform[0], self.transform[3]
        east = west + self.transform[1] * self.width
        south = north + self.transform[5] * self.height
        return [min(west, east), min(north, south), max(west, east), max(north, south)]

    def describe(self):
        extent = self._extent()
        return {
            "path": str(self.path),
            "filename": self.path.name,
            "sha256": _sha256_of(self.path),
            "size_bytes": self.path.stat().st_size,
            "mtime_ns": self.path.stat().st_mtime_ns,
            "width": self.width,
            "height": self.height,
            "dtype": self.gdal.GetDataTypeName(self.band.DataType),
            "nodata": self.nodata,
            "pixel_size_deg": [abs(self.transform[1]), abs(self.transform[5])],
            "pixel_size_m": _pixel_size_m(self.gdal, self.dataset.GetProjection(),
                                          self.transform),
            "extent": extent,
            "vertical_reference": self.vertical_reference,
            "vertical_status": self.vertical_status,
            "source_modified": False,
        }

    # ------------------------------------------------------------------- 窗口

    def window(self, bbox, width, height):
        """按 bbox 采样高度窗口（行序北→南）。NoData 保留 ``None``，绝不填 0。"""

        west, south, east, north = (float(v) for v in bbox)
        width, height = int(width), int(height)
        if not (1 <= width <= 4096 and 1 <= height <= 4096):
            raise TerrainWindowUnavailable("terrain_window_size_invalid",
                                           f"窗口尺寸非法：{width}x{height}")
        if width * height > MAX_WINDOW_CELLS:
            raise TerrainWindowUnavailable(
                "terrain_window_too_large",
                f"窗口 {width}x{height} 超过单次上限 {MAX_WINDOW_CELLS} 像元",
            )
        if not (west < east and south < north):
            raise TerrainWindowUnavailable("terrain_window_bbox_invalid",
                                           "bbox 非法（要求 west<east、south<north）")
        extent = self._extent()
        if east <= extent[0] or west >= extent[2] or north <= extent[1] or south >= extent[3]:
            raise TerrainWindowUnavailable(
                "terrain_window_outside_source",
                "请求 bbox 与 FABDEM 覆盖范围没有交集",
                source_extent=extent, requested=[west, south, east, north],
            )

        px0, py0 = self.gdal.ApplyGeoTransform(self.inverse, west, north)
        px1, py1 = self.gdal.ApplyGeoTransform(self.inverse, east, south)
        c0 = max(int(math.floor(min(px0, px1))) - 1, 0)
        r0 = max(int(math.floor(min(py0, py1))) - 1, 0)
        # 多读 2 行 2 列：``_bilinear`` 需要 ``col+1`` / ``row+1`` 参与插值。若块刚好
        # 结束在瓦片公共边上，最后一列/行的双线性会因越界而返回 ``None``，表现为接缝
        # 处出现 NoData 空洞（实测公共边整列取不到值）。多读一圈让边界采样有定义。
        c1 = min(int(math.ceil(max(px0, px1))) + 2, self.width)
        r1 = min(int(math.ceil(max(py0, py1))) + 2, self.height)
        if c1 - c0 < 2 or r1 - r0 < 2:
            raise TerrainWindowUnavailable("terrain_window_degenerate",
                                           "请求 bbox 在源栅格上不足 2×2 像元")
        block = self.band.ReadAsArray(c0, r0, c1 - c0, r1 - r0)
        if block is None:
            raise TerrainWindowUnavailable("terrain_window_read_failed", "FABDEM 窗口读取失败")
        rows = block.tolist()
        block_transform = (
            self.transform[0] + c0 * self.transform[1], self.transform[1], self.transform[2],
            self.transform[3] + r0 * self.transform[5], self.transform[4], self.transform[5],
        )
        scale = self.band.GetScale()
        offset = self.band.GetOffset()
        scale = 1.0 if scale is None else float(scale)
        offset = 0.0 if offset is None else float(offset)

        values = []
        nodata_count = 0
        minimum = maximum = None
        total = 0.0
        valid = 0
        for row in range(height):
            lat = north + (south - north) * (row / max(height - 1, 1))
            line = []
            for col in range(width):
                lon = west + (east - west) * (col / max(width - 1, 1))
                sampled = _bilinear(block_transform, rows, self.gdal, lon, lat,
                                    len(rows), len(rows[0]) if rows else 0, self.nodata)
                if sampled is None:
                    nodata_count += 1
                    line.append(None)
                else:
                    value = sampled * scale + offset
                    valid += 1
                    total += value
                    minimum = value if minimum is None else min(minimum, value)
                    maximum = value if maximum is None else max(maximum, value)
                    line.append(round(value, 4))
            values.append(line)

        cells = width * height
        status = "passed" if nodata_count == 0 else ("nodata" if nodata_count == cells else "partial")
        return {
            "status": status,
            "width": width,
            "height": height,
            "bbox": [west, south, east, north],
            "row_order": "north_to_south",
            "nodata_count": nodata_count,
            "nodata_value": self.nodata,
            "nodata_policy": "nodata_preserved_as_null_never_zero",
            "valid_count": valid,
            "min_m": minimum,
            "max_m": maximum,
            "mean_m": (total / valid) if valid else None,
            "vertical_reference": self.vertical_reference,
            "vertical_status": self.vertical_status,
            "values": values,
        }

    def sample_point(self, lon, lat, *, method="bilinear"):
        """单点采样（椭球高之外的**正高**）。

        默认用 **双线性**，与 :meth:`window` 保持同一口径 —— 否则"直采"与"窗口抽样"
        在陡坡处会差几米（实测 3.65 m），把采样方法差异误读成数据传输误差。
        ``method="nearest"`` 保留最近邻语义，仅用于对照。
        """

        px, py = self.gdal.ApplyGeoTransform(self.inverse, float(lon), float(lat))
        scale = self.band.GetScale()
        offset = self.band.GetOffset()
        scale = 1.0 if scale is None else float(scale)
        offset = 0.0 if offset is None else float(offset)

        if method == "nearest":
            col, row = int(math.floor(px)), int(math.floor(py))
            if not (0 <= col < self.width and 0 <= row < self.height):
                return None
            block = self.band.ReadAsArray(col, row, 1, 1)
            if block is None:
                return None
            value = float(block[0][0])
            if not math.isfinite(value):
                return None
            if self.nodata is not None and value == float(self.nodata):
                return None
            return value * scale + offset

        col, row = int(math.floor(px)), int(math.floor(py))
        if not (0 <= col < self.width - 1 and 0 <= row < self.height - 1):
            return None
        block = self.band.ReadAsArray(col, row, 2, 2)
        if block is None:
            return None
        rows = block.tolist()
        corners = [float(rows[r][c]) for r in range(2) for c in range(2)]
        for value in corners:
            if not math.isfinite(value):
                return None
            if self.nodata is not None and value == float(self.nodata):
                return None
        fx, fy = px - col, py - row
        sampled = (
            corners[0] * (1 - fx) * (1 - fy) + corners[1] * fx * (1 - fy)
            + corners[2] * (1 - fx) * fy + corners[3] * fx * fy
        )
        return sampled * scale + offset


def sample_point_geodetic(source, lon, lat):
    """模块级便捷入口（保持适配器无状态调用风格）。"""

    return source.sample_point(lon, lat)


def _snap_pixel_index(value, tolerance=1e-6):
    """把浮点像素坐标吸附到最近的整数（容差内），消除接缝处的下标抖动。

    为什么必须做：``InvGeoTransform`` 是逐块重建的，同一个经纬度在相邻两块里会算出
    数学上相等、浮点上相差 ~1e-10 的值。例如公共边经度在两块里分别得到 ``634.0`` 与
    ``0.9999999999417923``（块内坐标）；``floor`` 之后分别取列 ``634`` 与 ``0``，
    对应到全幅栅格就是像元 ``4838`` 与 ``4837``——**同一位置取到了不同的像元对**，
    于是在瓦片接缝处产生可见裂缝（实测某行差 35.2 m，而该处真实地形梯度并不支持
    这种跳变）。

    这里用容差吸附把"几乎整数"的坐标拉回整数，使相邻瓦片在公共边上必然选中同一
    像元对。容差取 1e-6 像元（远小于 30 m 像元，也远大于浮点噪声 1e-10）。
    """

    nearest = round(value)
    return float(nearest) if abs(value - nearest) <= tolerance else value


def _bilinear(transform, rows, gdal, lon, lat, nrows, ncols, nodata):
    if nrows < 2 or ncols < 2:
        return None
    px, py = gdal.ApplyGeoTransform(gdal.InvGeoTransform(transform), lon, lat)
    px = _snap_pixel_index(px)
    py = _snap_pixel_index(py)
    col, row = int(math.floor(px)), int(math.floor(py))
    if not (0 <= col < ncols - 1 and 0 <= row < nrows - 1):
        return None
    flat = []
    for value in (rows[row][col], rows[row][col + 1], rows[row + 1][col], rows[row + 1][col + 1]):
        value = float(value)
        if not math.isfinite(value):
            return None
        if nodata is not None and value == float(nodata):
            return None
        flat.append(value)
    fx, fy = px - col, py - row
    return (
        flat[0] * (1 - fx) * (1 - fy) + flat[1] * fx * (1 - fy)
        + flat[2] * (1 - fx) * fy + flat[3] * fx * fy
    )


def _pixel_size_m(gdal, projection, transform):
    """按 CRS 真实线性单位换算像元地面尺寸。

    投影 CRS 用它自身的 linear unit；地理 CRS 用 PROJ ``Geod`` 实测**相邻像元
    中心**的大地线距离。两者都拿不到时返回 ``None``（``unresolved``），
    **绝不把度当米**。
    """

    if not projection:
        return None
    try:
        from osgeo import osr
    except ImportError:  # pragma: no cover
        return None
    spatial = osr.SpatialReference()
    spatial.ImportFromWkt(projection)
    if spatial.IsProjected():
        unit = spatial.GetLinearUnits()
        return [abs(transform[1]) * unit, abs(transform[5]) * unit]
    return [_geodesic_arc_m(gdal, transform, axis="x"), _geodesic_arc_m(gdal, transform, axis="y")]


def _geodesic_arc_m(gdal, transform, axis):
    """相邻像元中心的地面距离（米），用 PROJ Geod 计算。"""

    try:
        from pyproj import Geod
    except ImportError:
        # 无 pyproj 时不做度数当米的换算：显式返回 None（unresolved）。
        return None
    geod = Geod(ellps="WGS84")
    west, north = transform[0], transform[3]
    lon0, lat0 = west, north
    if axis == "x":
        lon1, lat1 = west + transform[1], north
    else:
        lon1, lat1 = west, north + transform[5]
    _, _, distance = geod.inv(lon0, lat0, lon1, lat1)
    return abs(distance)


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 22), b""):
            digest.update(block)
    return digest.hexdigest()
