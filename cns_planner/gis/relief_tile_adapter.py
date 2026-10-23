"""FABDEM 派生地形晕渲瓦片（XYZ PNG），用于无影像时的真实起伏表达。

为什么需要它
------------
heightmap-1.0 瓦片**不含顶点法线**（Cesium 的 heightmap 解析路径只读 u16 高程 +
childTileMask + waterMask，没有 normals 段），因此 ``scene.globe.enableLighting``
在本项目实测**不产生任何可见坡面明暗**（A/B 实测 stdev 比值 1.0000）。当合法卫星
影像凭证缺失时，三维地表会退化成单色平面，无法人工审查山体形态。

本模块用**权威 FABDEM 30 m** 直接计算晕渲（hillshade），提供真实起伏表达：

* 数据来源只有 FABDEM 与 EGM2008（与地形瓦片同源、同转换），**不引入**任何在线
  影像、不抓取 Google/其他商业瓦片、不绘制假道路假港口；
* 它是**地形晕渲**，不是卫星影像：图层 id 与版权标注都显式区分，绝不用它冒充
  "高清卫星影像"，也不因此把影像验收标为 PASS；
* 只读：不写 ``ProjectState``，不参与规划、碰撞或风险判定。

高程基准
--------
与 :mod:`cns_planner.gis.terrain_tile_adapter` 完全一致：FABDEM 正高 ``H`` 经
EGM2008 逐点 ``N`` 转为椭球高 ``h = H + N``。晕渲使用**正高**计算坡度（坡度是
垂直差分量，基准常数在差分中抵消，但这里仍显式取正高以保证语义清晰）。

陆海判定
--------
沿用本仓**已记录**的制图约定（见 ``cns_planner/gis/map_data.py`` 的
``cartographic_land`` 注释）：FABDEM 正高 ``> 0 m`` 视为陆域。该判据**只**用于
决定"是否绘制晕渲"，不参与 surface classification，也不替换 canonical ``land_mask``。
"""

from __future__ import annotations

import math
import struct
import zlib

from .terrain_tile_adapter import (
    GLOBAL_BACKGROUND_MAX_LEVEL,
    MAX_LEVEL,
)


def _xyz_tile_bbox(z: int, x: int, y: int):
    """顶原点（XYZ / slippy）地理瓦片 bbox：层级 0 为 2×1，**y=0 在最北**。

    与 :func:`cns_planner.gis.terrain_tile_adapter.tms_tile_bbox` 的 y 轴方向相反：
    地形走 layer.json 的 ``"scheme": "tms"``，影像走 Cesium 影像图层的顶原点网格。
    """

    nx, ny = 2 << z, 1 << z
    width, height = 360.0 / nx, 180.0 / ny
    west = -180.0 + x * width
    north = 90.0 - y * height
    return west, north - height, west + width, north


RELIEF_TILE_SIZE = 256
#: 光源方位角（度，自北顺时针）与高度角（度）。315°/45° 是制图晕渲的通用约定，
#: 让西北向坡面偏亮、东南向偏暗，符合人眼对地形的默认解读。
LIGHT_AZIMUTH_DEG = 315.0
LIGHT_ALTITUDE_DEG = 45.0
#: 垂直夸张**固定为 1.0**：不得用放大起伏伪造地形（本轮硬约束）。
VERTICAL_EXAGGERATION = 1.0

#: 海面统一灰度。取值刻意偏暗，与陆地灰度区间拉开间隔，使海岸线清晰可读。
SEA_LEVEL_GRAY = 88
#: 陆地灰度区间。上限**刻意不取 255**：实测若上限为 255，朝阳坡面会整片打成纯白、
#: 丢失等高细节（近景截图里陆地是一片死白）。这里保留约 40 级余量。
LAND_MIN_GRAY = 118
LAND_MAX_GRAY = 215


class ReliefTileUnavailable(RuntimeError):
    def __init__(self, code: str, message: str, **detail):
        super().__init__(message)
        self.code = code
        self.detail = detail

    def as_dict(self):
        return {"error": str(self), "code": self.code, "detail": self.detail}


def _png_gray(width: int, height: int, pixels: bytes) -> bytes:
    """把 8 位灰度像素编码为 PNG（无第三方依赖）。

    手写 PNG 而不是引入 PIL：本模块运行在 QGIS Python 里，Pillow 不保证存在；
    PNG 的灰度编码足够简单（IHDR + IDAT(zlib) + IEND），且无额外依赖风险。
    """

    if len(pixels) != width * height:
        raise ReliefTileUnavailable("relief_pixel_count_invalid", "晕渲像素数量不符")

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)  # 8-bit grayscale
    # 每行前置 filter 类型 0（None）
    raw = bytearray()
    for row in range(height):
        raw.append(0)
        raw.extend(pixels[row * width:(row + 1) * width])
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b""))


class ReliefTileSource:
    """从只读 FABDEM 生成晕渲瓦片；与地形瓦片共用 TMS 网格与高程转换。"""

    def __init__(self, terrain_source, geoid, *, size=RELIEF_TILE_SIZE, source_lock=None):
        if terrain_source is None or geoid is None:
            raise ReliefTileUnavailable(
                "relief_sources_unavailable", "地形晕渲需要 FABDEM 与 EGM2008 geoid")
        self.terrain_source = terrain_source
        self.geoid = geoid
        self.size = max(32, int(size))
        self.terrain_description = terrain_source.describe()
        self.geoid_description = geoid.describe()
        self.extent = tuple(float(v) for v in self.terrain_description["extent"])
        # 必须使用装配层注入的**同一把**源锁：GDAL/PROJ 句柄在本运行时不支持并发
        # 方法调用，而地形瓦片与晕渲瓦片共享同一批句柄（同一个 TerrainWindowSource
        # 与 Egm2008Geoid 实例）。早期实现此处各自 ``threading.Lock()``，注释却写着
        # "与地形瓦片共用同一把源锁"——实测两把锁不是同一对象，等于没有互斥。
        import threading
        self._source_lock = source_lock if source_lock is not None else threading.Lock()

    def _available(self, z, x, y):
        if z <= GLOBAL_BACKGROUND_MAX_LEVEL:
            return False        # 全球背景层没有 FABDEM 数据，不伪造晕渲
        west, south, east, north = _xyz_tile_bbox(z, x, y)
        return (east >= self.extent[0] and west <= self.extent[2]
                and north >= self.extent[1] and south <= self.extent[3])

    def tile(self, z, x, y):
        """返回 ``(png_bytes, stats)``。

        **瓦片 y 采用顶原点（XYZ / slippy）约定**，与 Cesium
        ``UrlTemplateImageryProvider`` + ``GeographicTilingScheme`` 一致：y=0 在最北。
        这与地形瓦片的 TMS（y=0 在南）**不同**，是刻意的：地形由 layer.json 的
        ``"scheme": "tms"`` 显式声明，而影像图层走 Cesium 默认的顶原点网格。混用会
        导致行列号落在另一个半球，服务端一律判越界、底图空白。
        """

        try:
            z, x, y = int(z), int(x), int(y)
        except (TypeError, ValueError) as exc:
            raise ReliefTileUnavailable(
                "relief_tile_coordinate_invalid", "晕渲瓦片坐标无效") from exc
        if not 0 <= z <= MAX_LEVEL:
            raise ReliefTileUnavailable("relief_zoom_out_of_range", "晕渲缩放级别超出范围")
        limit_x, limit_y = 2 << z, 1 << z
        if not (0 <= x < limit_x and 0 <= y < limit_y):
            raise ReliefTileUnavailable("relief_tile_coordinate_invalid", "晕渲瓦片坐标无效")
        if not self._available(z, x, y):
            raise ReliefTileUnavailable("relief_tile_unavailable",
                                        "该层级瓦片不在 FABDEM 可用范围")

        bbox = _xyz_tile_bbox(z, x, y)
        size = self.size
        # 多取 1 行 1 列用于单侧差分，避免边缘出现人为坡向。
        sample = size + 1
        with self._source_lock:
            terrain = self.terrain_source.window(bbox, sample, sample)
            geoid = self.geoid.window(bbox, sample, sample)
        orthometric = terrain.get("values") or []
        undulation = geoid.get("values") or []
        if len(orthometric) != sample or len(undulation) != sample:
            raise ReliefTileUnavailable("relief_window_invalid", "晕渲采样窗口无效")

        # 像元地面尺寸（米）：纬向按 cos(lat) 收缩。
        west, south, east, north = bbox
        mean_lat = math.radians((south + north) / 2.0)
        cell_x = (east - west) * 111320.0 * math.cos(mean_lat) / (sample - 1)
        cell_y = (north - south) * 110540.0 / (sample - 1)
        if not (cell_x > 0 and cell_y > 0):
            raise ReliefTileUnavailable("relief_cell_size_invalid", "晕渲像元尺寸非法")

        azimuth = math.radians(LIGHT_AZIMUTH_DEG)
        altitude = math.radians(LIGHT_ALTITUDE_DEG)
        lx = math.cos(altitude) * math.sin(azimuth)
        ly = math.cos(altitude) * math.cos(azimuth)
        lz = math.sin(altitude)

        pixels = bytearray(size * size)
        land_cells = 0
        shaded_cells = 0
        for row in range(size):
            for col in range(size):
                here = orthometric[row][col]
                if here is None or not math.isfinite(float(here)):
                    # NoData 在 FABDEM 覆盖内通常即海面：画成平坦水面，不伪造陆地。
                    pixels[row * size + col] = SEA_LEVEL_GRAY
                    continue
                height = float(here)
                if height <= 0.0:
                    # 海面：统一平色，避免把测深噪声画成假山脊。
                    pixels[row * size + col] = SEA_LEVEL_GRAY
                    continue
                land_cells += 1
                east = orthometric[row][col + 1]
                south = orthometric[row + 1][col]
                if east is None or not math.isfinite(float(east)):
                    east = here
                if south is None or not math.isfinite(float(south)):
                    south = here
                # 采样窗口的行序是**北→南**（terrain_window_adapter.window 的既定契约），
                # 因此 row+1 是**南**邻点：dzdy_south = (f_south - f_here)/cell_y = -∂f/∂y。
                dzdx = (float(east) - height) / cell_x          # ∂f/∂x，x 轴朝东
                dzdy_south = (float(south) - height) / cell_y   # = -∂f/∂y，y 轴朝北
                # 表面法线 ∝ (-∂f/∂x, -∂f/∂y, 1)，故 y 分量 = +dzdy_south。
                # 早期实现写成 ``-dzdy_south``（即 +∂f/∂y），把南北光照整体镜像成
                # "光源在西南 225°"：单轴实测朝南坡 lambert 0.8557 / 朝北坡 0.1443，
                # 与声明的 315°（西北）正好相反。东西轴当时是对的，所以对角坡面上
                # x/y 贡献部分抵消，真实截图里肉眼无法察觉——必须有单轴断言守住。
                # 垂直夸张恒为 1.0（法线 z 分量固定为 1），不做任何起伏放大。
                nx, ny, nz = -dzdx, dzdy_south, 1.0
                norm = math.sqrt(nx * nx + ny * ny + nz * nz)
                if norm <= 0:
                    pixels[row * size + col] = LAND_MIN_GRAY
                    continue
                lambert = (nx * lx + ny * ly + nz * lz) / norm
                lambert = max(0.0, min(1.0, lambert))
                shaded_cells += 1
                # 陆地灰度必须整体**高于**海面灰度，否则海岸线会被冲淡成一片灰
                # （实测近景截图里海陆同色、轮廓不可读）。这里把陆地压进
                # [LAND_MIN_GRAY, LAND_MAX_GRAY]，与 SEA_LEVEL_GRAY 拉开明确间隔。
                value = LAND_MIN_GRAY + (LAND_MAX_GRAY - LAND_MIN_GRAY) * lambert
                pixels[row * size + col] = max(0, min(255, int(round(value))))

        body = _png_gray(size, size, bytes(pixels))
        stats = {
            "z": z, "x": x, "y": y, "bbox": list(bbox), "size": size,
            "land_cells": land_cells, "shaded_cells": shaded_cells,
            "vertical_exaggeration": VERTICAL_EXAGGERATION,
            "light_azimuth_deg": LIGHT_AZIMUTH_DEG,
            "light_altitude_deg": LIGHT_ALTITUDE_DEG,
            "vertical_reference": "egm2008_orthometric",
            "derived_from": "fabdem_30m",
            "is_satellite_imagery": False,
            "fabdem_sha256": self.terrain_description.get("sha256"),
            "egm2008_sha256": self.geoid_description.get("sha256"),
            "source_modified": False,
        }
        return body, stats
