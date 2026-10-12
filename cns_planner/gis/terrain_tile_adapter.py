"""Cesium ``heightmap-1.0`` multilevel tiles from read-only FABDEM + EGM2008."""

from __future__ import annotations

import hashlib
import json
import math
import struct
import threading
from collections import OrderedDict
from dataclasses import dataclass, field


HEIGHTMAP_WIDTH = 65
HEIGHT_SCALE = 0.2
HEIGHT_OFFSET = -1000.0
MAX_LEVEL = 13
GLOBAL_BACKGROUND_MAX_LEVEL = 5


class TerrainTileUnavailable(RuntimeError):
    def __init__(self, code: str, message: str, **detail):
        super().__init__(message)
        self.code = code
        self.detail = detail

    def as_dict(self):
        return {"error": str(self), "code": self.code, "detail": self.detail}


@dataclass
class TerrainTile:
    body: bytes = field(repr=False)
    etag: str
    stats: dict[str, object]
    headers: dict[str, str]


def tms_tile_bbox(z: int, x: int, y: int):
    """EPSG:4326 Cesium geographic tiling: level zero is 2×1, TMS south origin."""
    nx, ny = 2 << z, 1 << z
    width, height = 360.0 / nx, 180.0 / ny
    west, south = -180.0 + x * width, -90.0 + y * height
    return west, south, west + width, south + height


def _bbox_intersects(a, b):
    return a[2] >= b[0] and a[0] <= b[2] and a[3] >= b[1] and a[1] <= b[3]


def _range_for_extent(extent, level):
    nx, ny = 2 << level, 1 << level
    width, height = 360.0 / nx, 180.0 / ny
    west, south, east, north = extent
    epsilon = 1.0e-10
    start_x = max(0, min(nx - 1, int(math.floor((west + 180.0) / width))))
    end_x = max(0, min(nx - 1, int(math.floor((east - epsilon + 180.0) / width))))
    start_y = max(0, min(ny - 1, int(math.floor((south + 90.0) / height))))
    end_y = max(0, min(ny - 1, int(math.floor((north - epsilon + 90.0) / height))))
    return start_x, start_y, end_x, end_y


class TerrainTileSource:
    """Read-only, bounded terrain pyramid with explicit non-authoritative background."""

    def __init__(self, terrain_source, geoid, *, water_mask_source=None, cache_size=128,
                 source_lock=None):
        if terrain_source is None or geoid is None:
            raise TerrainTileUnavailable(
                "terrain_sources_unavailable", "多级地形需要 FABDEM 与 EGM2008 geoid")
        self.terrain_source = terrain_source
        self.geoid = geoid
        self.water_mask_source = water_mask_source
        self.terrain_description = terrain_source.describe()
        self.geoid_description = geoid.describe()
        self.extent = tuple(float(v) for v in self.terrain_description["extent"])
        self.cache_size = max(1, int(cache_size))
        self._cache = OrderedDict()
        self._lock = threading.RLock()
        # GDAL Dataset/Band and the canonical PROJ transformer are shared read-only
        # handles but are not safe for concurrent method calls in this runtime.
        # ``source_lock`` 由装配层注入：地形瓦片与晕渲瓦片共享**同一批** GDAL/PROJ
        # 句柄（同一个 TerrainWindowSource / Egm2008Geoid 实例），因此必须共享
        # **同一把**锁。各自 ``threading.Lock()`` 等于没有互斥——实测两个 provider
        # 的锁不是同一对象，而底层句柄是同一对象。
        self._source_lock = source_lock if source_lock is not None else threading.Lock()
        self._inflight = {}
        #: 水掩膜可用性探测结果（``None`` = 尚未探测）。
        self._water_mask_probe = None

    def layer_json(self, base_url: str):
        base = str(base_url).rstrip("/")
        # 只有在「水 mask 源存在 **且** 该层级确实可采样」时才对外宣称提供 watermask。
        # 采样源为 None 时直接为空；存在但不可用的情况由 _water_mask_status() 判定，
        # 与瓦片实际写入行为保持一致（对外声明必须与响应体同真）。
        water_status = self._water_mask_status()
        available_extensions = water_status == "available_from_cartographic_mask"
        available = []
        for level in range(MAX_LEVEL + 1):
            if level <= GLOBAL_BACKGROUND_MAX_LEVEL:
                available.append([{
                    "startX": 0, "startY": 0,
                    "endX": (2 << level) - 1, "endY": (1 << level) - 1,
                }])
            else:
                start_x, start_y, end_x, end_y = _range_for_extent(self.extent, level)
                available.append([{
                    "startX": start_x, "startY": start_y,
                    "endX": end_x, "endY": end_y,
                }])
        extensions = ["watermask"] if available_extensions else []
        return {
            "tilejson": "2.1.0",
            "name": "CNS FABDEM 30 m + EGM2008 ellipsoidal terrain",
            "description": "Read-only visual terrain; FABDEM remains the planning authority",
            "version": "1.0.0",
            "format": "heightmap-1.0",
            "projection": "EPSG:4326",
            "scheme": "tms",
            "tiles": [f"{base}/{{z}}/{{x}}/{{y}}.terrain"],
            "minzoom": 0,
            "maxzoom": MAX_LEVEL,
            "heightmapWidth": HEIGHTMAP_WIDTH,
            "heightmapStructure": {
                "heightScale": HEIGHT_SCALE,
                "heightOffset": HEIGHT_OFFSET,
                "elementsPerHeight": 1,
                "stride": 1,
                "elementMultiplier": 256.0,
                "isBigEndian": False,
                "lowestEncodedHeight": 0,
                "highestEncodedHeight": 65535,
            },
            "extensions": extensions,
            "available": available,
            "attribution": "FABDEM 30 m; EGM2008 geoid conversion; CNS local read-only terrain",
            "source": {
                "fabdem_sha256": self.terrain_description.get("sha256"),
                "egm2008_sha256": self.geoid_description.get("sha256"),
                "fabdem_vertical_reference": self.terrain_description.get("vertical_reference"),
                "output_vertical_reference": "wgs84_ellipsoidal",
                "conversion": "h = H + N",
                "background": "H=0 orthometric converted with EGM2008 N; visual only",
                "background_authoritative_for_planning": False,
                "water_mask_status": water_status,
            },
        }

    def _available(self, z, x, y):
        if z <= GLOBAL_BACKGROUND_MAX_LEVEL:
            return True
        return _bbox_intersects(tms_tile_bbox(z, x, y), self.extent)

    def _validate(self, z, x, y):
        try:
            z, x, y = int(z), int(x), int(y)
        except (TypeError, ValueError) as exc:
            raise TerrainTileUnavailable("terrain_tile_coordinate_invalid",
                                         "地形瓦片坐标无效") from exc
        if not 0 <= z <= MAX_LEVEL:
            raise TerrainTileUnavailable("terrain_tile_zoom_out_of_range", "地形缩放级别超出范围")
        nx, ny = 2 << z, 1 << z
        if not (0 <= x < nx and 0 <= y < ny):
            raise TerrainTileUnavailable("terrain_tile_coordinate_invalid", "地形瓦片坐标无效")
        if not self._available(z, x, y):
            raise TerrainTileUnavailable("terrain_tile_unavailable", "该层级瓦片不在 FABDEM 可用范围")
        return z, x, y

    def tile(self, z: int, x: int, y: int) -> TerrainTile:
        key = self._validate(z, x, y)
        while True:
            with self._lock:
                cached = self._cache.get(key)
                if cached is not None:
                    self._cache.move_to_end(key)
                    return cached
                event = self._inflight.get(key)
                if event is None:
                    event = threading.Event()
                    self._inflight[key] = event
                    producer = True
                else:
                    producer = False
            if producer:
                break
            event.wait(timeout=30.0)
        try:
            result = self._build_tile(*key)
            with self._lock:
                self._cache[key] = result
                self._cache.move_to_end(key)
                while len(self._cache) > self.cache_size:
                    self._cache.popitem(last=False)
            return result
        finally:
            with self._lock:
                pending = self._inflight.pop(key, None)
                if pending is not None:
                    pending.set()

    def _build_tile(self, z, x, y):
        bbox = tms_tile_bbox(z, x, y)
        should_sample_fabdem = z > GLOBAL_BACKGROUND_MAX_LEVEL and _bbox_intersects(bbox, self.extent)
        with self._source_lock:
            geoid = self.geoid.window(bbox, HEIGHTMAP_WIDTH, HEIGHTMAP_WIDTH)
            geoid_values = geoid.get("values") or []
            if should_sample_fabdem:
                terrain = self.terrain_source.window(bbox, HEIGHTMAP_WIDTH, HEIGHTMAP_WIDTH)
                orthometric = terrain.get("values") or []
            else:
                orthometric = [[None] * HEIGHTMAP_WIDTH for _ in range(HEIGHTMAP_WIDTH)]
        if len(geoid_values) != HEIGHTMAP_WIDTH:
            raise TerrainTileUnavailable("terrain_geoid_window_invalid", "EGM2008 窗口无效")
        if len(orthometric) != HEIGHTMAP_WIDTH:
            raise TerrainTileUnavailable("terrain_fabdem_window_invalid", "FABDEM 窗口无效")

        encoded = []
        nodata_background = 0
        minimum = maximum = None
        for row in range(HEIGHTMAP_WIDTH):
            if len(orthometric[row]) != HEIGHTMAP_WIDTH or len(geoid_values[row]) != HEIGHTMAP_WIDTH:
                raise TerrainTileUnavailable("terrain_window_shape_invalid", "地形窗口尺寸不一致")
            for col in range(HEIGHTMAP_WIDTH):
                n_value = geoid_values[row][col]
                if n_value is None or not math.isfinite(float(n_value)):
                    raise TerrainTileUnavailable("terrain_geoid_nodata", "EGM2008 网格存在不可用值")
                h_value = orthometric[row][col]
                if h_value is None or not math.isfinite(float(h_value)):
                    h_value = 0.0
                    nodata_background += 1
                ellipsoidal = float(h_value) + float(n_value)
                minimum = ellipsoidal if minimum is None else min(minimum, ellipsoidal)
                maximum = ellipsoidal if maximum is None else max(maximum, ellipsoidal)
                quantized = int(round((ellipsoidal - HEIGHT_OFFSET) / HEIGHT_SCALE))
                encoded.append(max(0, min(65535, quantized)))

        child_mask = self._child_mask(z, x, y)
        body = struct.pack("<" + "H" * len(encoded), *encoded) + bytes([child_mask])
        water_body, water_status = self._water_mask_body(bbox)
        if water_body is not None:
            body += water_body

        identity = {
            "z": z, "x": x, "y": y,
            "fabdem": self.terrain_description.get("sha256"),
            "egm2008": self.geoid_description.get("sha256"),
            "body": hashlib.sha256(body).hexdigest(),
        }
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()
        stats = {
            "z": z, "x": x, "y": y, "bbox": list(bbox),
            "width": HEIGHTMAP_WIDTH, "height": HEIGHTMAP_WIDTH,
            "min_ellipsoidal_m": minimum, "max_ellipsoidal_m": maximum,
            "nodata_background_count": nodata_background,
            "background_orthometric_m": 0.0,
            "background_authoritative_for_planning": False,
            "fabdem_sha256": self.terrain_description.get("sha256"),
            "egm2008_sha256": self.geoid_description.get("sha256"),
            "conversion": "h = H + N",
            "source_modified": False,
            "water_mask_status": water_status,
        }
        return TerrainTile(
            body=body,
            etag=f"terrain-heightmap-v1-{digest}",
            stats=stats,
            headers={
                "Cache-Control": "private, max-age=86400",
                "X-CNS-Terrain-Cache": "memory-lru",
                "X-CNS-Terrain-NoData-Background": str(nodata_background),
            },
        )

    def _water_mask_status(self):
        """探测水掩膜是否真的可用（结果缓存），供 ``layer.json`` 与实际写入共用。

        判定方式是**在 FABDEM 覆盖范围内实采**，并且要求该窗口**确实同时含有陆地
        与水面**：

        * 只看 ``water_mask_source is not None`` 不够——源可能返回 dict、尺寸不符或
          直接抛错，那样 ``layer.json`` 会对外宣称提供 ``watermask`` 而瓦片里根本
          没有，属于契约违约；
        * 只探测"覆盖范围之外"更不够：那种窗口会被 footprint 守卫整片判成不确定
          （按陆地），结果**恒为单色**，于是"完全未知"也会被读成"可用"。早期实现
          用 ``tms_tile_bbox(MAX_LEVEL, 0, 0)``（实测 = -180,-90,-179.978,-89.978，
          南半球西端、远离舟山）做探测，三种语义完全不同的源（全 None / 全陆 /
          半海半陆）**都**返回 ``available_from_cartographic_mask``，等于没有探测；
        * 因此这里**要求**探测窗口内同时出现陆地与水面，否则如实报告不可用
          （未评估 ≠ 通过）。
        """

        if self.water_mask_source is None:
            return "blocked_no_cartographic_mask"
        with self._lock:
            cached = self._water_mask_probe
        if cached is not None:
            return cached
        status = self._probe_water_mask()
        with self._lock:
            self._water_mask_probe = status
        return status

    def _probe_water_mask(self):
        """在 FABDEM 覆盖范围内挑一个"确有海也有陆"的瓦片窗口做探测。"""

        candidates = []
        for level in (10, 8, MAX_LEVEL):
            start_x, start_y, end_x, end_y = _range_for_extent(self.extent, level)
            if start_x <= end_x and start_y <= end_y:
                # 取覆盖范围中心附近的瓦片，避免边界瓦片只压到一小角数据。
                candidates.append(
                    (level, (start_x + end_x) // 2, (start_y + end_y) // 2))
        inconclusive = None
        for level, x, y in candidates:
            try:
                body, status = self._water_mask_body(tms_tile_bbox(level, x, y))
            except Exception:
                inconclusive = inconclusive or "blocked_water_mask_source_failed"
                continue
            if status != "available_from_cartographic_mask" or body is None:
                # 结构性失败（源报错 / 形状不可识别）是确定性的否定结论，优先报告。
                inconclusive = inconclusive or status
                continue
            if len(body) > 1 and len(set(body)) > 1:
                # 单字节 = 全陆或全海（Cesium 简写），多字节才说明掩膜有判别力。
                return "available_from_cartographic_mask"
        return inconclusive or "blocked_water_mask_probe_inconclusive"

    def _water_mask_body(self, bbox):
        """构建 Cesium water mask 字节串；任何异常都降级为「无 mask」。

        heightmap-1.0 的 water mask 是 **water-truthy**（255 = 水面），而本仓现有
        陆域掩膜是 **land-truthy**，因此必须显式取反；``coastal_uncertain`` 与
        ``unknown`` 一律按陆地处理（fail toward land），绝不把不确定区域刷成海面。

        返回值 ``(bytes | None, status)``。``None`` 表示本瓦片不含 water mask，
        调用方必须同时让 ``layer.json`` 的 ``extensions`` 保持为空——对外宣称提供
        水掩膜却不写入是更严重的契约违约。水掩膜只是显示增强，其失败**不得**让
        地形瓦片整体不可用。
        """

        if self.water_mask_source is None:
            return None, "blocked_no_cartographic_mask"
        try:
            raw = self.water_mask_source.window(bbox, 256, 256)
        except Exception:
            return None, "blocked_water_mask_source_failed"
        # 采样源既可能返回裸嵌套 list，也可能返回 {"values": ...}（本仓地形/大地
        # 水准面窗口就是后者）。先归一化再校验形状，绝不能直接迭代 dict。
        values = raw.get("values") if isinstance(raw, dict) else raw
        if not isinstance(values, (list, tuple)) or len(values) != 256:
            return None, "blocked_water_mask_shape_unrecognized"
        flat = bytearray(256 * 256)
        for row_index, row in enumerate(values):
            if not isinstance(row, (list, tuple)) or len(row) != 256:
                return None, "blocked_water_mask_shape_unrecognized"
            base = row_index * 256
            for col_index, value in enumerate(row):
                # land-truthy -> water-truthy：真值代表陆地，故取反。
                # None / 非数值 / 不确定类别一律按陆地（0）处理。
                try:
                    is_land = bool(int(value))
                except (TypeError, ValueError):
                    is_land = True
                flat[base + col_index] = 0 if is_land else 255
        if len(set(flat)) <= 1:
            # 全陆或全海：Cesium 允许单字节简写。
            return bytes(flat[:1]), "available_from_cartographic_mask"
        return bytes(flat), "available_from_cartographic_mask"

    def _child_mask(self, z, x, y):
        if z >= MAX_LEVEL:
            return 0
        mask = 0
        # TMS: south-west, south-east, north-west, north-east.
        for bit, child_x, child_y in (
            (1, x * 2, y * 2), (2, x * 2 + 1, y * 2),
            (4, x * 2, y * 2 + 1), (8, x * 2 + 1, y * 2 + 1),
        ):
            if self._available(z + 1, child_x, child_y):
                mask |= bit
        return mask
