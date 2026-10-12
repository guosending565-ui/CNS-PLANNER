"""Cesium water mask from the read-only land-mask vector source.

为什么需要这一层：``TerrainTileSource`` 的 ``water_mask_source`` 契约是
``window(bbox, width, height)``，而本仓的 :class:`LandMaskSource` 只提供**逐点**判定。
两者之间必须有一个适配器，否则：

* 逐点调用 ``classify`` 在 256×256 = 65 536 个点上实测 **约 10 s/瓦片**，
  交互式浏览完全不可用；
* 直接复用会把**陆地真值**误当成 heightmap 的**水面真值**（语义反转），
  把海洋反光刷到陆地上。

本模块用 STRtree 预筛 + ``shapely.contains_xy`` 向量化把单瓦片降到约 0.1–0.6 s，
并用 STRtree 的 ``dwithin`` 查询实现**海岸不确定带**，严格保留
:class:`LandMaskSource` 的语义：

* ``land`` → 陆地；
* ``coastal_uncertain``（距陆地几何 ``coastal_uncertainty_buffer_m`` 以内的非陆地点）
  → **陆地**（fail toward land，绝不把不确定区域画成海面）；只有调用方显式传
  ``coastal_is_water=True`` 时才按水面处理。该缓冲带**必须**真实实现：``land_mask``
  的权威常量 ``LAND_MASK_SEMANTICS`` 声明的是"显式多边形包含 **+ 海岸不确定带**"，
  只做多边形包含会让缓冲带落进"明确水面"分支，把海面反光刷到滩涂/堤坝上；
* ``unknown`` / 数据覆盖范围之外 → **陆地**（Round35-A D-01 footprint 守卫：
  范围外是"未知"，不是"海"）；
* 只有明确的 ``sea`` 才是水面。

零写入：只读矢量源，不触碰 ``ProjectState``，不参与规划判定。
"""

from __future__ import annotations

import math
import threading

from .radar_layout_adapter import LAND_MASK_SEMANTICS


class WaterMaskUnavailable(RuntimeError):
    """水掩膜不可用；调用方必须降级为「无 water mask」，不得猜。"""

    def __init__(self, code: str, message: str, **detail):
        super().__init__(message)
        self.code = code
        self.detail = detail


class LandMaskWaterMaskSource:
    """把 :class:`LandMaskSource` 适配成 ``window(bbox, width, height) -> dict``。

    返回结构与 ``TerrainWindowSource.window`` 保持一致（``{"values": ...}``，
    行序北→南，``None`` 表示不确定），因此 ``TerrainTileSource`` 可以统一处理。

    ``values`` 中 ``1`` = 陆地，``0`` = 水面；``None`` = 不确定（按陆地处理）。
    """

    def __init__(self, land_mask_source, *, coastal_is_water=False):
        if land_mask_source is None:
            raise WaterMaskUnavailable("water_mask_source_missing", "未配置陆域掩膜来源")
        self.source = land_mask_source
        #: ``coastal_uncertain`` 是否算水面。默认 ``False``（算陆地），因为把不确定
        #: 海岸带画成海面会产生"海上反光盖住滩涂/堤坝"的假象，而画成陆地只是少了
        #: 一点反光，属于可接受的保守降级。
        self.coastal_is_water = bool(coastal_is_water)
        self._lock = threading.RLock()
        self._tree = None
        self._geometries = None

    # ------------------------------------------------------------ 几何与索引

    def _ensure_index(self):
        """懒构建 STRtree；几何与索引都是只读缓存。"""

        with self._lock:
            if self._tree is not None:
                return self._tree, self._geometries
            polygons = self.source.prepare()
            if not polygons:
                raise WaterMaskUnavailable(
                    "water_mask_geometry_unavailable", "陆域掩膜几何不可用")
            geometries = []
            for entry in polygons:
                geometry = entry[0] if isinstance(entry, (list, tuple)) else entry
                if geometry is not None:
                    geometries.append(geometry)
            if not geometries:
                raise WaterMaskUnavailable(
                    "water_mask_geometry_unavailable", "陆域掩膜几何为空")
            try:
                from shapely.strtree import STRtree
            except Exception as exc:  # pragma: no cover - 环境级失败
                raise WaterMaskUnavailable(
                    "water_mask_spatial_index_unavailable", "空间索引不可用") from exc
            self._geometries = geometries
            self._tree = STRtree(geometries)
            return self._tree, self._geometries

    # ------------------------------------------------------------------ 窗口

    def window(self, bbox, width, height):
        """返回 ``{"values": [[...]], ...}``；行序北→南。"""

        west, south, east, north = (float(v) for v in bbox)
        width, height = int(width), int(height)
        if not (west < east and south < north):
            raise WaterMaskUnavailable("water_mask_bbox_invalid", "水掩膜 bbox 非法")
        if not (1 <= width <= 1024 and 1 <= height <= 1024):
            raise WaterMaskUnavailable("water_mask_size_invalid", "水掩膜窗口尺寸非法")

        import numpy as np
        import shapely

        tree, geometries = self._ensure_index()

        # 1) 采样点经纬度网格（行序北→南，与地形窗口一致）
        longitudes = np.linspace(west, east, width)
        latitudes = np.linspace(north, south, height)
        grid_lon, grid_lat = np.meshgrid(longitudes, latitudes)
        flat_lon = grid_lon.ravel()
        flat_lat = grid_lat.ravel()

        # 2) 经纬度 → 米制（与陆域几何同一 CRS）。逐点变换是 C 调用，成本可接受。
        metric = [self.source.to_metric(lon, lat)
                  for lon, lat in zip(flat_lon.tolist(), flat_lat.tolist())]
        xs = np.array([m[0] if m else np.nan for m in metric], dtype=float)
        ys = np.array([m[1] if m else np.nan for m in metric], dtype=float)

        # 3) footprint 守卫：覆盖范围之外一律「不确定」= 陆地，绝不判海。
        footprint = self.source.footprint_bounds()
        margin = float(getattr(self.source, "data_footprint_margin_m", 0.0) or 0.0)
        if footprint is not None:
            minx, miny, maxx, maxy = footprint
            outside = ((xs < minx - margin) | (xs > maxx + margin)
                       | (ys < miny - margin) | (ys > maxy + margin))
        else:
            # 覆盖范围未知 => 无法证明"范围外是海"，全部按不确定处理。
            outside = np.ones(xs.shape, dtype=bool)

        valid = ~np.isnan(xs) & ~np.isnan(ys) & ~outside

        # 4) STRtree 预筛：只对与查询范围相交的几何做精确判定。
        land = np.zeros(xs.shape, dtype=bool)
        if valid.any():
            vx, vy = xs[valid], ys[valid]
            query_box = shapely.box(float(np.nanmin(vx)), float(np.nanmin(vy)),
                                   float(np.nanmax(vx)), float(np.nanmax(vy)))
            try:
                candidates = tree.query(query_box)
            except Exception:
                candidates = range(len(geometries))
            for index in candidates:
                geometry = geometries[int(index)]
                if geometry is None or geometry.is_empty:
                    continue
                # 只对该几何自身 bbox 内的点做精确判定：粗层级瓦片会命中数百个
                # 候选多边形，若每个都对全部 65 536 点调用 contains_xy，代价是
                # 候选数 × 点数；按 bbox 收窄后实际参与判定的点数大幅下降。
                try:
                    gminx, gminy, gmaxx, gmaxy = geometry.bounds
                except Exception:
                    continue
                subset = ((vx >= gminx) & (vx <= gmaxx)
                          & (vy >= gminy) & (vy <= gmaxy))
                if not subset.any():
                    continue
                try:
                    hit = shapely.contains_xy(geometry, vx[subset], vy[subset])
                except Exception:
                    continue
                # 把子集结果写回全局 valid 位置
                target = np.flatnonzero(valid)[subset]
                land[target] |= hit

        # 4b) 海岸不确定带：与权威 ``LandMaskSource`` 的 ``LAND_MASK_SEMANTICS`` 对齐。
        #     该常量声明"显式多边形包含 **+ 海岸不确定带**"，权威逐点分类把 buffer 内的
        #     点判为 ``coastal_uncertain``（按陆地对齐）。本适配器早期只做 contains_xy，
        #     缓冲带内的点因此落进"明确水面"分支——海岸带被刷上海面反光，与模块自身
        #     的 fail-toward-land 承诺正好相反。这里用 STRtree 的 dwithin 查询补上该语义。
        buffer_m = float(
            getattr(self.source, "coastal_uncertainty_buffer_m", 0.0) or 0.0)
        coastal_uncertain = np.zeros(xs.shape, dtype=bool)
        if buffer_m > 0.0 and valid.any():
            sea_only = valid & ~land
            if sea_only.any():
                sea_x, sea_y = xs[sea_only], ys[sea_only]
                try:
                    pairs = np.asarray(tree.query(
                        shapely.points(sea_x, sea_y),
                        predicate="dwithin", distance=buffer_m))
                    hit_positions = (sorted({int(v) for v in pairs[0]})
                                     if pairs.size else [])
                except Exception:
                    # 空间索引不支持 dwithin 时不做猜测：缓冲带整体保持"未知"，
                    # 由调用方按陆地处理（保守方向），绝不静默当成明确海面。
                    hit_positions = list(range(int(sea_only.sum())))
                if hit_positions:
                    positions = np.flatnonzero(sea_only)
                    coastal_uncertain[positions[hit_positions]] = True

        # 5) 组装：land -> 1，明确 sea -> 0，其余（范围外 / 海岸不确定带）-> None
        values = []
        coastal_cells = 0
        for row in range(height):
            base = row * width
            line = []
            for col in range(width):
                index = base + col
                if not valid[index]:
                    line.append(None)          # 范围外 / 变换失败：未知，按陆地处理
                elif coastal_uncertain[index]:
                    coastal_cells += 1
                    # 默认按陆地（fail toward land）；只有调用方显式要求才算水面。
                    line.append(0 if self.coastal_is_water else None)
                elif land[index]:
                    line.append(1)             # 明确陆地
                else:
                    line.append(0)             # 明确水面
            values.append(line)

        return {
            "values": values,
            "status": "passed",
            "row_order": "north_to_south",
            "semantics": "1=land, 0=sea, None=uncertain_treated_as_land",
            "land_mask_semantics": LAND_MASK_SEMANTICS,
            "coastal_is_water": self.coastal_is_water,
            "coastal_uncertainty_buffer_m": buffer_m,
            "coastal_uncertain_cells": coastal_cells,
            "footprint_guard": footprint is not None,
            "footprint_outside_is_unknown_not_sea": True,
            "source_modified": False,
        }


def build_water_mask_source(land_mask_path, **kwargs):
    """从只读陆域掩膜 gpkg 装配水掩膜源；失败返回 ``(None, error_dict)``。"""

    try:
        from .radar_layout_adapter import LandMaskSource
        land = LandMaskSource(land_mask_path, **kwargs)
        if land.prepare() is None:
            return None, {
                "code": "water_mask_geometry_unavailable",
                "message": "陆域掩膜几何不可用",
            }
        return LandMaskWaterMaskSource(land), None
    except WaterMaskUnavailable as exc:
        return None, {"code": exc.code, "message": str(exc)}
    except Exception as exc:  # pragma: no cover - 环境级失败
        return None, {"code": "water_mask_source_unavailable", "message": str(exc)}
