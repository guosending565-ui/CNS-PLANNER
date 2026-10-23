"""Radar 地形 LOS 与逐 probe 高度的 **GIS 只读适配层**。

职责边界
--------
* 本模块是唯一允许打开 FABDEM 的地方；它只做**只读**窗口采样，绝不写回任何数据源、
  绝不写 ``ProjectState``、绝不新增 writer。
* 地形访问**复用**既有已验证实现：:class:`~cns_planner.gis.fine_environment_adapter.
  NativeTerrainWindowSource` 提供 native 像素窗口读取、CRS/单位解析与 NoData 语义；
  本模块只在其上做"沿射线的逐采样格 min/max"，**不**重新实现文件访问、重采样或
  插值。
* 原点解析**复用**既有 :func:`cns_planner.gis.radar_layout_adapter.resolve_radar_origins`
  与 :class:`~cns_planner.domain.device_service_origin.DeviceServiceOrigin` 统一解析器；
  本模块**不**开发第二套原点解析器。
* 高度层解析**复用**既有
  :func:`cns_planner.domain.radar_surveillance_layout.resolve_radar_altitude_layer_id`；
  probe 高度只取自该权威高度层目录的 ``nominal_altitude_m``，**绝不**使用 corridor
  voxel 的 ``overlap_height`` 中点或 ``probe.altitude_egm2008_m`` 代表值。

性能纪律
--------
* 一次 :func:`sample_terrain_profile` 调用**只**读一次 FABDEM 窗口（
  ``single_read_only_native_window_no_resample_per_request``），逐采样格在该内存数组
  上取 min/max，因此采样点数与文件 IO 次数解耦。
* 打开的 terrain source 必须由调用方在用完后 :func:`close_terrain_sources`；批量塔
  场景使用 :class:`TerrainSourceCache` 复用同一 source，避免逐塔重复打开文件。
"""

from __future__ import annotations

import math
from copy import deepcopy

from ..domain import radar_terrain_los as los
from ..domain.radar_surveillance_layout import radar_geometry_parameters
from ..algorithms.radar_layout.terrain_los_lookup import (
    DEFAULT_MAX_RAY_COUNT, TERRAIN_LOS_MODE_DISABLED, TerrainLosLookup,
)

#: 沿射线的水平采样间距（米）。工程基线：FABDEM 舟山切片原生像元约 30 m，
#: 5 m 间距意味着单个像元内约有 6 个采样位置，像元级地形起伏不会被跳过。
#: 该值是**工程采样参数**（不是业务安全阈值），并进入 input fingerprint。
DEFAULT_PROFILE_SAMPLING_INTERVAL_M = 5.0

#: 每个采样格在航线**横向**的搜索半宽（米）：用于在同一像元/邻域内取地形最高值，
#: 因此"沿射线的最高地形"不可能因为只取沿线单点像元而被低估。
DEFAULT_PROFILE_ENVELOPE_RADIUS_M = 20.0

#: 地球平均半径（米，IUGG 平均半径）。仅当调用方显式给出时才进入射线模型。
EARTH_MEAN_RADIUS_M = 6371008.8

#: 默认折射倍率：``1.0`` 表示**不引入任何折射修正**（纯几何）。标准大气写作 4/3，
#: 但那是本模块之外的显式选择，绝不默认套用。
DEFAULT_REFRACTION_K_FACTOR = 1.0


#: 复用的既有 FABDEM 只读访问器变体。
#:
#: * ``window`` = :class:`FabdemWindowTerrainSource`：既有逐点/逐格窗口采样器
#:   （``sample_cells``），与既有 Radar / TowerObstacleProfile 链路**同一实现**；
#: * ``native`` = :class:`NativeTerrainWindowSource`：既有 native 像素窗口读取器
#:   （``native_window``），供本模块的沿射线剖面使用。
SOURCE_VARIANT_WINDOW = "window"
SOURCE_VARIANT_NATIVE = "native"
SOURCE_VARIANTS = (SOURCE_VARIANT_WINDOW, SOURCE_VARIANT_NATIVE)


class TerrainSourceCache:
    """按 ``(路径, 变体)`` 复用 FABDEM terrain source 的只读缓存。

    "不能为每个 probe 重复打开 FABDEM 文件"是硬要求：本类保证同一路径的同一变体在
    一次批量计算里只打开一次，并提供 :meth:`close` 显式释放。缓存键包含解析后的绝对
    路径与访问器变体，因此换源或换访问器语义都必然换实例（不会把两个数据源混用）。
    """

    def __init__(self, source_factory=None):
        self._factory = source_factory or _default_source_factory
        self._sources = {}
        self.open_count = 0
        self.reused_count = 0
        self.unavailable = {}

    def get(self, path, variant=SOURCE_VARIANT_NATIVE):
        key = (str(path) if path else None, str(variant))
        if not key[0]:
            return None
        if key in self._sources:
            self.reused_count += 1
            return self._sources[key]
        if key in self.unavailable:
            return None
        try:
            source = self._factory(key[0], key[1])
        except Exception as exc:  # noqa: BLE001 - 数据源不可用必须如实降级为 unknown
            self.unavailable[key] = f"{type(exc).__name__}:{exc}"
            return None
        if source is None:
            self.unavailable[key] = "terrain_source_unavailable"
            return None
        self._sources[key] = source
        self.open_count += 1
        return source

    def describe(self):
        return {
            "cache_key_count": len(self._sources) + len(self.unavailable),
            "open_count": self.open_count,
            "reused_count": self.reused_count,
            "unavailable": {
                f"{key[0]}|{key[1]}": reason for key, reason in self.unavailable.items()
            },
            "one_open_per_path_and_variant": True,
            "variants": list(SOURCE_VARIANTS),
        }

    def close(self):
        for source in self._sources.values():
            dataset = getattr(source, "dataset", None)
            if dataset is not None and hasattr(dataset, "FlushCache"):
                try:
                    dataset.FlushCache()
                except Exception:  # noqa: BLE001 - 关闭失败不影响结论
                    pass
            if dataset is not None:
                try:
                    del source.dataset
                except Exception:  # noqa: BLE001
                    pass
        self._sources.clear()


class TerrainLosExecutionContext:
    """Own one lazy DEM cache and one bounded lookup for an isolated execution.

    Construction never opens the DEM.  Frozen geometry classification happens in
    :class:`TerrainLosLookup`; only an in-range/elevation-valid cache miss reaches the existing
    :func:`evaluate_probe_terrain_los` adapter below.
    """

    def __init__(self, *, terrain_path, mode=TERRAIN_LOS_MODE_DISABLED,
                 route_id=None, transform=None, source_factory=None,
                 sampling_interval_m=DEFAULT_PROFILE_SAMPLING_INTERVAL_M,
                 envelope_radius_m=DEFAULT_PROFILE_ENVELOPE_RADIUS_M,
                 max_ray_count=DEFAULT_MAX_RAY_COUNT, cancel_check=None):
        self.terrain_path = None if terrain_path in (None, "") else str(terrain_path)
        self.transform = transform
        self.sampling_interval_m = float(sampling_interval_m)
        self.envelope_radius_m = float(envelope_radius_m)
        self.cache = TerrainSourceCache(source_factory=source_factory)
        self.lookup = TerrainLosLookup(
            mode=mode,
            evaluator=self._evaluate,
            source_identity=(
                None if self.terrain_path is None
                else terrain_source_identity_for_path(self.terrain_path)
            ),
            route_id=route_id,
            sampling={
                "sampling_interval_m": self.sampling_interval_m,
                "envelope_radius_m": self.envelope_radius_m,
                "source_variant": SOURCE_VARIANT_NATIVE,
                "profile_detail": PROFILE_DETAIL_SUMMARY,
            },
            max_ray_count=max_ray_count,
            cancel_check=cancel_check,
        )

    def _evaluate(self, *, tower, sample, radar_type, geometry):
        source = self.cache.get(self.terrain_path, SOURCE_VARIANT_NATIVE)
        if source is None:
            return {
                "status": los.LOS_STATUS_UNKNOWN,
                "reason": "terrain_source_unavailable",
                "terrain_sample_count": 0,
            }
        probe = {
            **sample,
            "voxel_id": sample.get("voxel_id") or sample.get("sample_id"),
            "altitude_egm2008_m": sample.get("egm2008_m"),
        }
        return evaluate_probe_terrain_los(
            tower,
            probe,
            terrain_source=source,
            transform=self.transform,
            sampling_interval_m=self.sampling_interval_m,
            envelope_radius_m=self.envelope_radius_m,
            origin_availability=(
                tower.get("origin_availability") or los.ORIGIN_AVAILABILITY_UNKNOWN
            ),
            max_slant_range_m=radar_geometry_parameters(radar_type)["max_slant_range_m"],
            profile_detail=PROFILE_DETAIL_SUMMARY,
        )

    def snapshot(self):
        return {
            **self.lookup.snapshot(),
            "dem_open_count": self.cache.describe()["open_count"],
            "dem_cache": self.cache.describe(),
        }

    def close(self):
        self.cache.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False


def _default_source_factory(path, variant=SOURCE_VARIANT_NATIVE):
    if str(variant) == SOURCE_VARIANT_WINDOW:
        from .fine_environment_adapter import FabdemWindowTerrainSource

        return FabdemWindowTerrainSource(path)
    from .fine_environment_adapter import NativeTerrainWindowSource

    return NativeTerrainWindowSource(path)


def terrain_source_identity(source):
    """FABDEM 数据源的**可指纹化身份**（路径 + 大小 + mtime + 尺寸 + 垂向基准）。

    指纹必须能区分"同一路径的文件被换掉"与"只是重新读了一次"，因此包含文件大小与
    ``mtime_ns``；同时如实带上垂向基准与读取模式，避免"基准不明的 DEM 静默参与结论"。
    """

    if source is None:
        return None
    path = getattr(source, "path", None)
    identity = {
        "role": getattr(source, "role", "terrain_dtm"),
        "file_name": None if path is None else path.name,
        "absolute_path": None if path is None else str(path),
        "vertical_reference": getattr(source, "vertical_reference", None),
        "vertical_status": getattr(source, "vertical_status", None),
        "read_mode": los.RADAR_TERRAIN_LOS_READ_MODE,
        "resampled": False,
        "no_data_filling": False,
    }
    try:
        stat = path.stat()
        identity["size_bytes"] = stat.st_size
        identity["mtime_ns"] = stat.st_mtime_ns
    except (AttributeError, OSError):
        identity["size_bytes"] = None
        identity["mtime_ns"] = None
    dataset = getattr(source, "dataset", None)
    if dataset is not None:
        identity["width"] = int(dataset.RasterXSize)
        identity["height"] = int(dataset.RasterYSize)
    identity["nodata"] = getattr(source, "nodata", None)
    identity["effective_resolution_m"] = _safe_resolution_m(source)
    return identity


def terrain_source_identity_for_path(path):
    """**不打开数据集**的 DEM 身份摘要（供只读状态接口使用）。

    与 :func:`terrain_source_identity` 同一口径（路径 + 大小 + ``mtime_ns`` +
    读取模式 + ``resampled=False``），但**只做 ``stat``**，因此可以在"尚未请求
    计算"的路径上安全调用而不打开任何栅格。数据集级字段（宽高、分辨率、NoData）
    在未打开时如实为 ``None``，绝不猜测。
    """

    from pathlib import Path as _Path

    if not path:
        return None
    resolved = _Path(str(path))
    identity = {
        "role": "terrain_dtm",
        "file_name": resolved.name,
        "absolute_path": str(resolved),
        "vertical_reference": None,
        "vertical_status": "not_resolved_dataset_not_opened",
        "read_mode": los.RADAR_TERRAIN_LOS_READ_MODE,
        "resampled": False,
        "no_data_filling": False,
        "dataset_opened": False,
        "exists": False,
    }
    try:
        stat = resolved.stat()
        identity["size_bytes"] = stat.st_size
        identity["mtime_ns"] = stat.st_mtime_ns
        identity["exists"] = True
    except OSError as exc:
        identity["size_bytes"] = None
        identity["mtime_ns"] = None
        identity["stat_error"] = f"{type(exc).__name__}:{exc}"
    identity["width"] = None
    identity["height"] = None
    identity["nodata"] = None
    identity["effective_resolution_m"] = None
    return identity


def _safe_resolution_m(source):
    try:
        if hasattr(source, "usable"):
            ok, _reason = source.usable()
            if not ok:
                return None
        return source.effective_resolution_m()
    except Exception:  # noqa: BLE001 - 分辨率解析失败不阻塞，但如实返回 None
        return None


def sample_terrain_profile(source, *, transform, origin_metric, target_metric,
                           sampling_interval_m=None, envelope_radius_m=None):
    """沿米制射线的地形剖面采样（**单次**窗口读取，逐采样格地形 min/max）。

    几何定义（必须能被逐条审计）
    ---------------------------
    1. 在**米制平面**（Radar 既有的 EPSG:32651 平面）上按
       :data:`DEFAULT_PROFILE_SAMPLING_INTERVAL_M` 把射线分成等长小段，每段是一个
       "采样格"，其沿射线距离取该段中点（第一个格固定在 d = 0，即雷达脚下）。
    2. 一个 FABDEM 像元进入某采样格的条件：该像元**四角到射线段的距离全部**
       ≤ ``envelope_radius_m``。判据取四角的最大值，因此是保守包含判据。
    3. 采样格的地形取值是该格内**全部有效像元**的 ``min`` 与 ``max``；
       射线的判定用 ``max``（最高地形），``min`` 用于区分"最好情况也已遮挡"与
       "切触"。因此地形起伏不可能因为"只取沿线单点像元"而被系统性低估。
    4. 该格内出现**任一** NoData 像元 ⇒ 该格 :data:`los.TERRAIN_SAMPLE_UNKNOWN`。
       NoData 绝不被填补、绝不当 0、绝不外包插。

    返回::

        {"status": "passed"|"unknown", "reason": ..., "samples": [...],
         "source": {...}, "sampling": {...}, "read_receipt": {...}}

    每个采样格::

        {"index", "distance_m", "fraction", "longitude", "latitude",
         "terrain_min_m", "terrain_max_m", "valid_pixel_count", "nodata_pixel_count",
         "status"}

    无法给出结果时返回 ``status = "unknown"`` 且 ``samples = []``；调用方必须据此
    返回 unknown，**不允许**自动判 clear。
    """

    import numpy as np

    interval = los.number_or_none(sampling_interval_m)
    interval = (
        DEFAULT_PROFILE_SAMPLING_INTERVAL_M if interval is None or interval <= 0.0
        else interval
    )
    envelope = los.number_or_none(envelope_radius_m)
    envelope = (
        DEFAULT_PROFILE_ENVELOPE_RADIUS_M if envelope is None or envelope < 0.0
        else envelope
    )
    origin = _metric_pair(origin_metric)
    target = _metric_pair(target_metric)
    source_identity = terrain_source_identity(source)
    sampling = {
        "sampling_interval_m": interval,
        "envelope_radius_m": envelope,
        "sampling_mode": "along_track_equal_segments_with_pixel_containment_envelope",
        "along_track_direction": "radar_origin_to_probe",
        "pixel_inclusion_rule": (
            "all_four_pixel_corners_within_envelope_radius_of_the_ray_segment"
        ),
        "cross_track_extreme_used": "maximum_valid_elevation_within_segment_envelope",
        "read_mode": los.RADAR_TERRAIN_LOS_READ_MODE,
    }
    blocked = {
        "status": "unknown", "samples": [], "source": source_identity,
        "sampling": sampling, "read_receipt": None,
    }
    if source is None:
        return {**blocked, "reason": "terrain_source_not_configured"}
    if origin is None or target is None:
        return {**blocked, "reason": "profile_endpoints_missing"}
    if transform is None or not hasattr(transform, "to_geographic"):
        return {**blocked, "reason": "metric_transform_unavailable"}
    usable = getattr(source, "usable", None)
    if callable(usable):
        ok, reason = usable()
        if not ok:
            return {**blocked, "reason": str(reason or "terrain_source_not_usable")}
    horizontal_total = _metric_length(origin, target)
    if horizontal_total <= 0.0:
        return {**blocked, "reason": "profile_horizontal_length_zero"}
    index_count = int(math.floor(horizontal_total / interval)) + 1
    distances = np.linspace(0.0, horizontal_total, index_count, dtype=float)
    along_m = distances[:-1] + (distances[1:] - distances[:-1]) / 2.0
    if along_m.size:
        along_m[0] = 0.0
    direction = np.array(
        [(target[0] - origin[0]) / horizontal_total, (target[1] - origin[1]) / horizontal_total],
        dtype=float,
    )

    # ---- 1. 粗采样确定像元窗口（只读窗口的切法，不进入结论） --------------------
    pixel_size_m = _pixel_size_native_m(source)
    if pixel_size_m is None:
        return {**blocked, "reason": "pixel_size_unresolved"}
    pixel_diagonal_m = math.hypot(pixel_size_m[0], pixel_size_m[1])
    coarse_geographic = []
    coarse_count = 33
    for step in range(coarse_count):
        fraction = step / (coarse_count - 1)
        point_metric = [
            origin[0] + (target[0] - origin[0]) * fraction,
            origin[1] + (target[1] - origin[1]) * fraction,
        ]
        geographic = transform.to_geographic(point_metric)
        if geographic is None:
            return {**blocked, "reason": "profile_geographic_transform_failed"}
        coarse_geographic.append([float(geographic[0]), float(geographic[1])])
    coarse_pixels = _line_pixel_indices(source, coarse_geographic)
    if coarse_pixels is None:
        return {**blocked, "reason": "profile_outside_dtm_coverage"}
    window = _envelope_pixel_window(
        source, coarse_pixels,
        lateral_tolerance_m=envelope + pixel_diagonal_m / 2.0,
        along_tolerance_m=interval / 2.0 + pixel_diagonal_m / 2.0,
        pixel_size_m=pixel_size_m,
    )
    if window is None:
        return {**blocked, "reason": "profile_window_empty"}
    read = _read_native_window(source, window)
    if read is None:
        return {**blocked, "reason": "dtm_read_failed"}
    values, x0, y0, x1, y1, nodata_mask, scale, offset = read

    # ---- 2. 像元 → 采样格（纯 numpy 向量化，逐格聚合） -------------------------
    apex = source._pixel_geographic(0, 0) or [0.0, 0.0]
    px_step = _pixel_step_geographic(source)
    if px_step is None:
        return {**blocked, "reason": "pixel_size_unresolved"}
    corner_lon, corner_lat = np.meshgrid(
        apex[0] + px_step[0] * np.arange(int(x0), int(x1) + 1, dtype=float),
        apex[1] + px_step[1] * np.arange(int(y0), int(y1) + 1, dtype=float),
    )
    #: ``corner_lon`` / ``corner_lat`` 的形状是 (像素行数 + 1, 像素列数 + 1) 的角点网格；
    #: 每个像元的四个角分别是 [r, c]、[r, c+1]、[r+1, c]、[r+1, c+1] 四个切片，
    #: 因此每个切片都恰好是"一个角 × 一个像元"，四个切片拼起来才是完整的角点集。
    row_count, column_count = corner_lon.shape
    corner_slices = (
        (slice(0, row_count - 1), slice(0, column_count - 1)),
        (slice(0, row_count - 1), slice(1, column_count)),
        (slice(1, row_count), slice(0, column_count - 1)),
        (slice(1, row_count), slice(1, column_count)),
    )
    ox, oy = origin[0], origin[1]
    #: 判据用的局部米制坐标系：以**射线起点**为原点，用该纬度处的 WGS84 局部因子把
    #: 经纬差换算成米。难点在于 :func:`transform` 的逆变换返回的是**经纬度**，而像元
    #: 中心本来是经纬度——两者必须落在同一坐标系里才能做投影分解。因此这里先把射线
    #: 端点逆变换回经纬度、再映射进同一个局部米制坐标系，绝不把投影坐标与局部米制
    #: 坐标混算（那会让所有像元被判到包络之外）。参考纬度取射线起点的真实纬度，
    #: 在数十公里内该近似的相对误差约 1e-4，只影响"哪些像元进入包络"这一保守判据，
    #: **不参与**任何高度、里程或斜距的数值计算。
    origin_geographic = transform.to_geographic([ox, oy])
    target_geographic = transform.to_geographic(
        [ox + direction[0] * horizontal_total, oy + direction[1] * horizontal_total]
    )
    if origin_geographic is None or target_geographic is None:
        return {**blocked, "reason": "profile_geographic_transform_failed"}
    reference_latitude = float(origin_geographic[1])
    meters_per_degree_lon = _longitudinal_m_per_degree(reference_latitude)
    meters_per_degree_lat = _meridional_m_per_degree(reference_latitude)
    frame_x = (corner_lon - float(origin_geographic[0])) * meters_per_degree_lon
    frame_y = (corner_lat - float(origin_geographic[1])) * meters_per_degree_lat
    target_x = (float(target_geographic[0]) - float(origin_geographic[0])) * meters_per_degree_lon
    target_y = (float(target_geographic[1]) - float(origin_geographic[1])) * meters_per_degree_lat
    frame_length = math.hypot(target_x, target_y)
    if frame_length <= 0.0:
        return {**blocked, "reason": "profile_horizontal_length_zero"}
    frame_direction = [target_x / frame_length, target_y / frame_length]
    corner_x_list = [frame_x[rows, columns].ravel() for rows, columns in corner_slices]
    corner_y_list = [frame_y[rows, columns].ravel() for rows, columns in corner_slices]
    #: 四个角点 → **像元中心**（坐标平均）。像元是否进入包络只有"中心到射线的垂直
    #: 距离"一个判据，因此一个像元只会落进一个采样格，不会重复计数。
    pixel_x = (corner_x_list[0] + corner_x_list[1] + corner_x_list[2] + corner_x_list[3]) / 4.0
    pixel_y = (corner_y_list[0] + corner_y_list[1] + corner_y_list[2] + corner_y_list[3]) / 4.0

    ox, oy = origin[0], origin[1]
    rel_x = pixel_x
    rel_y = pixel_y
    along_raw = rel_x * frame_direction[0] + rel_y * frame_direction[1]
    perpendicular = np.abs(rel_x * frame_direction[1] - rel_y * frame_direction[0])    #: 一个像元参与某采样格的判据（两条**都必须**满足）：
    #:   1. 横向：像元中心到射线的垂直距离 ≤ 包络半径 + 半个像元对角线
    #:      （像元与包络管相交即算，不会因为像元比包络大而漏掉沿线像元）；
    #:   2. 沿射线：像元中心沿射线的投影距离与该格中心之差 ≤ 半个格长 + 半个像元对角线。
    #:
    #: 判据 2 里"半个像元对角线"是**必须**的：FABDEM 像元（舟山切片约 30 m）远大于本
    #: 模块默认的 5 m 采样间距。若只允许半个格长，30 m 级像元会让约 95% 的采样格取不到
    #: 任何像元，结论退化 unknown —— 那不是更保守，而是**信息丢失**。代价是相邻采样格
    #: 会共享同一像元；这**不是**插值或填补（取样值始终是源像元原值），并且真实像元
    #: 间距与共享程度会由 ``sampling.pixel_association`` 如实披露：沿射线的独立分辨率
    #: 上限就是像元尺度，而不是采样间距。
    within = perpendicular <= (envelope + pixel_diagonal_m / 2.0)
    if not within.any():
        return {**blocked, "reason": "no_source_pixel_within_profile_envelope"}
    selected_along = along_raw[within]
    selected_values = values.ravel()[within]
    selected_invalid = nodata_mask.ravel()[within]
    selected_delta_tolerance = interval / 2.0 + pixel_diagonal_m / 2.0
    aggregation, association = aggregate_samples_to_pixels(
        values=selected_values, invalid=selected_invalid, along_m=selected_along,
        sample_distances_m=along_m, tolerance_m=selected_delta_tolerance,
        scale=scale, offset=offset,
    )
    terrain_min = aggregation["terrain_min_m"]
    terrain_max = aggregation["terrain_max_m"]
    valid_count = aggregation["valid_pixel_count"]
    nodata_count = aggregation["nodata_pixel_count"]
    pixel_coverage = aggregation["pixel_count"]

    association = association if isinstance(association, dict) else {}
    if along_m.size and association.get("samples_without_pixel", 0) >= along_m.size:
        #: 整条射线的所有采样格都没有关联到任何源像元 ⇒ 没有可用的地形证据。
        #: 这必须返回 unknown：既不允许自动判 clear，也不允许返回"passed + 全 unknown
        #: 样本"让调用方误以为地形剖面可用。
        return {**blocked, "reason": "no_source_pixel_within_profile_envelope"}
    if along_m.size and \
            int(pixel_coverage[-1]) == 0 and int(valid_count[-1]) == 0 and \
            int(nodata_count[-1]) == 0:
        #: 射线**终点**所在采样格完全没有源像元 ⇒ 目标点落在 DTM 覆盖之外。
        #: 这是明确的 unknown（不是"沿线有缺口"），调用方必须按未评估处理。
        return {**blocked, "reason": "target_outside_dtm_coverage"}

    samples = []
    for slot_index in range(along_m.size):
        distance = float(along_m[slot_index])
        point = [ox + direction[0] * distance, oy + direction[1] * distance]
        geographic = transform.to_geographic(point)
        if geographic is None:
            return {**blocked, "reason": "profile_geographic_transform_failed"}
        unknown = valid_count[slot_index] == 0 or nodata_count[slot_index] > 0
        samples.append({
            "index": slot_index,
            "distance_m": distance,
            "fraction": distance / horizontal_total,
            "metric": [float(point[0]), float(point[1])],
            "longitude": float(geographic[0]),
            "latitude": float(geographic[1]),
            "terrain_min_m": None if unknown else float(terrain_min[slot_index]),
            "terrain_max_m": None if unknown else float(terrain_max[slot_index]),
            "valid_pixel_count": int(valid_count[slot_index]),
            "nodata_pixel_count": int(nodata_count[slot_index]),
            "envelope_pixel_count": int(pixel_coverage[slot_index]),
            "status": los.TERRAIN_SAMPLE_UNKNOWN if unknown else "sampled",
            "reason": (
                "source_pixel_nodata_never_filled_never_zeroed"
                if unknown else None
            ),
        })
    sampling = {
        **sampling,
        "envelope_pixel_diagonal_m": pixel_diagonal_m,
        "lateral_tolerance_m": envelope + pixel_diagonal_m / 2.0,
        "along_tolerance_m": selected_delta_tolerance,
        "pixel_association": association,
        "along_track_independent_resolution_limited_by_pixel_size": True,
        "adjacent_samples_share_source_pixels": bool(
            association.get("max_samples_per_pixel", 0) > 1
        ),
    }
    return {
        "status": "passed",
        "reason": None,
        "samples": samples,
        "source": source_identity,
        "sampling": sampling,
        "read_receipt": _read_receipt(values, (x0, y0, x1, y1), len(samples)),
    }


def aggregate_samples_to_pixels(*, values, invalid, along_m, sample_distances_m,
                                tolerance_m, scale=1.0, offset=0.0):
    """把（已通过横向包络判据的）像元按沿射线距离关联到采样格，并逐格取 min/max。

    关联规则：像元中心沿射线的投影距离与该采样格中心之差 ≤ ``tolerance_m``。
    一个采样格可以关联多个像元（取它们的 min/max），一个像元也可以被多个相邻采样格
    共享——这是"地形像元尺度大于采样间距"的必然结果，并**不是**插值或填补：每个
    取值都是源像元的原值。

    返回 ``(聚合结果, 关联统计)``；聚合结果是五个长度为采样格数的数组。
    """

    import numpy as np

    sample_distances = np.asarray(sample_distances_m, dtype=float)
    pixel_along = np.asarray(along_m, dtype=float)
    slot_count = sample_distances.size
    output = {
        "terrain_min_m": np.full(slot_count, np.inf, dtype=float),
        "terrain_max_m": np.full(slot_count, -np.inf, dtype=float),
        "valid_pixel_count": np.zeros(slot_count, dtype=int),
        "nodata_pixel_count": np.zeros(slot_count, dtype=int),
        "pixel_count": np.zeros(slot_count, dtype=int),
    }
    if slot_count == 0 or pixel_along.size == 0:
        return output, {"pixel_count": 0, "shared_sample_count": 0,
                        "max_pixels_per_sample": 0, "min_along_gap_m": None}
    delta = np.abs(pixel_along[:, None] - sample_distances[None, :])
    link = delta <= float(tolerance_m)
    output["pixel_count"] = link.sum(axis=0)
    output["nodata_pixel_count"] = (link & invalid[:, None]).sum(axis=0)
    finite = np.isfinite(values)
    valid_link = link & finite[:, None]
    output["valid_pixel_count"] = valid_link.sum(axis=0)
    elevations = np.where(finite, np.asarray(values, dtype=float) * scale + offset, np.nan)
    with np.errstate(invalid="ignore"):
        minimum = np.where(valid_link, elevations[:, None], np.inf).min(axis=0)
        maximum = np.where(valid_link, elevations[:, None], -np.inf).max(axis=0)
    output["terrain_min_m"] = minimum
    output["terrain_max_m"] = maximum
    overlaps = link.sum(axis=1)
    gaps = np.diff(np.sort(pixel_along))
    association = {
        "pixel_count": int(pixel_along.size),
        "shared_sample_count": int((output["pixel_count"] > 1).sum()),
        "max_pixels_per_sample": int(output["pixel_count"].max()) if slot_count else 0,
        "samples_without_pixel": int((output["pixel_count"] == 0).sum()),
        "min_along_gap_m": None if gaps.size == 0 else float(gaps.min()),
        "mean_along_gap_m": None if gaps.size == 0 else float(gaps.mean()),
        "max_samples_per_pixel": int(overlaps.max()) if overlaps.size else 0,
        "association_tolerance_m": float(tolerance_m),
        "interpolated": False,
        "elevation_values_are_source_pixel_values": True,
    }
    return output, association


def _read_receipt(values, window, sample_count):
    x0, y0, x1, y1 = window
    return {
        "read_mode": los.RADAR_TERRAIN_LOS_READ_MODE,
        "read_count": 1,
        "window_pixels": [int(x0), int(y0), int(x1) - int(x0), int(y1) - int(y0)],
        "native_pixel_count": int(values.size),
        "sample_count": int(sample_count),
        "one_read_per_profile": True,
        "resampled": False,
        "no_data_filling": False,
        "interpolated": False,
    }


def _metric_pair(point):
    if (
        isinstance(point, (list, tuple)) and len(point) == 2
        and all(los.number_or_none(value) is not None for value in point)
    ):
        return [float(point[0]), float(point[1])]
    return None


def _metric_length(first, second):
    return los.horizontal_distance_m(first, second) or 0.0


def _meridional_deg_per_meter(latitude_deg):
    """纬向 ``度/米``（子午圈）。用于把纬度差换算成局部的南北向米数（仅判据用）。"""

    return 1.0 / _meridional_m_per_degree(latitude_deg)


def _longitudinal_deg_per_meter(latitude_deg):
    """经向 ``度/米``（卯酉圈 × cos φ）。"""

    return 1.0 / _longitudinal_m_per_degree(latitude_deg)


def _meridional_m_per_degree(latitude_deg):
    """一个纬度对应的地面米数（子午圈曲率半径，WGS84 解析式）。"""

    latitude = los.number_or_none(latitude_deg) or 0.0
    phi = math.radians(max(-89.9, min(89.9, latitude)))
    a = 6378137.0
    f = 1.0 / 298.257223563
    e2 = f * (2.0 - f)
    sin_phi = math.sin(phi)
    denominator = 1.0 - e2 * sin_phi * sin_phi
    meridional = a * (1.0 - e2) / (denominator ** 1.5)
    return math.pi / 180.0 * meridional


def _longitudinal_m_per_degree(latitude_deg):
    """一个经度对应的地面米数（卯酉圈曲率半径 × cos φ）。"""

    latitude = los.number_or_none(latitude_deg) or 0.0
    phi = math.radians(max(-89.9, min(89.9, latitude)))
    a = 6378137.0
    f = 1.0 / 298.257223563
    e2 = f * (2.0 - f)
    sin_phi = math.sin(phi)
    denominator = 1.0 - e2 * sin_phi * sin_phi
    normal = a / math.sqrt(denominator)
    return math.pi / 180.0 * normal * max(1e-9, math.cos(phi))


def source_scale_deg_per_meter(latitude_deg):
    """局部 ``度/米`` 因子（取经纬两个方向的较小地面距离，保证采样不过于稀疏）。

    用 WGS84 子午圈/卯酉圈曲率半径的解析式给出，不依赖任何外部投影库；这不是
    ``degree-as-meter``（那会把 1 度当 1 米），而是**测地**换算。
    """

    lat = _meridional_deg_per_meter(latitude_deg)
    lon = _longitudinal_deg_per_meter(latitude_deg)
    return max(lat, lon)


def _pixel_step_geographic(source):
    """栅格仿射变换的 (Δlon/列, Δlat/行)（度）。不可得返回 ``None``。

    优先读既有 :class:`_FabdemRasterBase` 已解析并缓存的 ``transform``（它来自
    ``dataset.GetGeoTransform()``），因此不会重复打开数据集或重复解析仿射变换。
    """

    transform = getattr(source, "transform", None)
    if transform is None:
        dataset = getattr(source, "dataset", None)
        getter = getattr(dataset, "GetGeoTransform", None)
        if not callable(getter):
            return None
        try:
            transform = getter()
        except (TypeError, RuntimeError, ValueError):
            return None
    try:
        return float(transform[1]), float(transform[5])
    except (TypeError, IndexError, ValueError):
        return None


def _pixel_size_native_m(source):
    """像元在两个栅格方向上的米制长度 ``(Δlon_m, Δlat_m)``；不可得返回 ``None``。

    优先复用既有 :meth:`_FabdemRasterBase.effective_resolution_detail`（它已包含
    "地理 CRS 用测地相邻像元中心距离"的完整口径）；只在既有解析不可用时，才用
    WGS84 解析式的局部换算作为**同一语义**的等价实现。
    """

    step = _pixel_step_geographic(source)
    if step is None:
        return None
    apex = None
    try:
        apex = source._pixel_geographic(0, 0)
    except Exception:  # noqa: BLE001
        apex = None
    if apex is None:
        return None
    try:
        detail = source.effective_resolution_detail()
        if isinstance(detail, dict) and detail.get("status") == "passed":
            resolution_x = detail.get("effective_resolution_m_x")
            resolution_y = detail.get("effective_resolution_m_y")
            if resolution_x and resolution_y:
                return (float(resolution_x), float(resolution_y))
    except Exception:  # noqa: BLE001 - 既有解析不可用时按同一语义自行换算
        pass
    return (
        abs(step[0]) * _longitudinal_m_per_degree(apex[1]),
        abs(step[1]) * _meridional_m_per_degree(apex[1]),
    )


def _line_pixel_indices(source, line_geographic):
    pixels = []
    for geographic in line_geographic:
        pixel = source._to_pixel(geographic)
        if pixel is None:
            return None
        pixels.append((float(pixel[0]), float(pixel[1])))
    return pixels


def _envelope_pixel_window(source, pixel_points, *, lateral_tolerance_m,
                           along_tolerance_m, pixel_size_m):
    """覆盖"射线 ± 横向容差、端点外扩沿射线容差"的像元窗口（只影响读取范围）。

    窗口必须**不小于**后面格-像元关联所用的容差，否则会把本应参与的像元裁掉，
    让结论无谓地退化为 unknown。因此这里用同一组容差反推边距，而不是另取一个
    看似合理的经验边距。
    """

    extent_m = float(lateral_tolerance_m) + float(along_tolerance_m)
    margin_columns = int(math.ceil(extent_m / max(1e-9, pixel_size_m[0]))) + 1
    margin_rows = int(math.ceil(extent_m / max(1e-9, pixel_size_m[1]))) + 1
    columns = [item[0] for item in pixel_points]
    rows = [item[1] for item in pixel_points]
    x0 = max(0, int(math.floor(min(columns))) - margin_columns)
    y0 = max(0, int(math.floor(min(rows))) - margin_rows)
    x1 = min(int(source.dataset.RasterXSize), int(math.floor(max(columns))) + margin_columns + 1)
    y1 = min(int(source.dataset.RasterYSize), int(math.floor(max(rows))) + margin_rows + 1)
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1


def _read_native_window(source, window):
    """**唯一**的 FABDEM 读取点：一次 ``ReadAsArray`` + scale/offset + NoData 掩膜。"""

    import numpy as np

    x0, y0, x1, y1 = window
    array = source.band.ReadAsArray(x0, y0, x1 - x0, y1 - y0)
    if array is None:
        return None
    values = np.asarray(array, dtype=float)
    if values.ndim != 2:
        return None
    nodata = getattr(source, "nodata", None)
    mask = ~np.isfinite(values)
    if nodata is not None:
        mask |= values == float(nodata)
    scale = source.band.GetScale()
    offset = source.band.GetOffset()
    return (
        values, int(x0), int(y0), int(x1), int(y1), mask,
        1.0 if scale is None else float(scale),
        0.0 if offset is None else float(offset),
    )


# ------------------------------------------------------- 批量塔地形（一次一塔、缓存复用）


def sample_tower_terrain(towers, *, terrain_source):
    """对塔坐标批量采样 FABDEM 正高（**单点**，用于原点与地表的物理一致性判定）。

    复用既有 :func:`cns_planner.gis.radar_layout_adapter.sample_route_terrain` 的语义与
    采样窗口半径常量，因此不与既有实现产生第二套地形读取口径。
    """

    from .radar_layout_adapter import sample_route_terrain

    points = []
    for tower in towers or []:
        if not isinstance(tower, dict):
            continue
        tower_id = str(tower.get("tower_id") or "")
        longitude, latitude = tower.get("longitude"), tower.get("latitude")
        if not tower_id or longitude is None or latitude is None:
            continue
        points.append({"key": tower_id, "longitude": longitude, "latitude": latitude})
    return sample_route_terrain(points, terrain_source=terrain_source)


def describe_terrain_source(source):
    """既有 FABDEM source 的描述 + 本模块的指纹身份（只读，不打开新数据）。"""

    identity = terrain_source_identity(source)
    described = None
    if source is not None and hasattr(source, "describe"):
        try:
            described = deepcopy(source.describe())
        except Exception:  # noqa: BLE001 - 描述失败不阻塞，指纹身份仍可用
            described = None
    return {"identity": identity, "described": described}


# ------------------------------------------------------- 逐塔物理输入与逐 probe LOS


def radar_origin_terrain_physical_inputs(towers, *, terrain_source, point_terrain_source=None):
    """逐塔读出**FABDEM 地表**并与原点数值做物理一致性判定（只读、不写回）。

    原点数值与证据等级**只**来自入参（生产链路上它们是
    :func:`cns_planner.gis.radar_layout_adapter.resolve_radar_origins` 的逐塔记录），
    本函数不重新解析原点、不改写 ``origin_state`` 词表。

    ``terrain_source`` 是逐点/逐格窗口采样器（``FabdemWindowTerrainSource``，与既有
    Radar 链路同一实现）；``point_terrain_source`` 仅为兼容旧调用，缺省时使用
    ``terrain_source`` 本身。

    返回 ``{tower_id: 物理输入诊断}``；地形不可用的塔如实给出 ``unknown``。
    """

    sampler = point_terrain_source if point_terrain_source is not None else terrain_source
    sampled = sample_tower_terrain(towers, terrain_source=sampler)
    result = {}
    for tower in towers or []:
        if not isinstance(tower, dict):
            continue
        tower_id = str(tower.get("tower_id") or "")
        if not tower_id:
            continue
        terrain = sampled.get(tower_id) or {}
        result[tower_id] = los.origin_physical_input(
            origin_egm2008_m=tower.get("origin_egm2008_m"),
            origin_source=tower.get("origin_source"),
            origin_state=tower.get("origin_state"),
            terrain_m=terrain.get("elevation_m"),
            terrain_status=terrain.get("status"),
        ) | {
            "tower_id": tower_id,
            "name": tower.get("name"),
            "site_type": tower.get("site_type"),
            "terrain_sampling": {
                "status": terrain.get("status"),
                "reason": terrain.get("reason"),
                "vertical_reference": terrain.get("vertical_reference"),
                "sampling": terrain.get("sampling"),
                "valid_pixel_count": terrain.get("valid_pixel_count"),
                "nodata_pixel_count": terrain.get("nodata_pixel_count"),
            },
            "tower_obstacle_profile": {
                "tower_top_orthometric_m": tower.get("tower_top_orthometric_m"),
                "building_height_m": tower.get("building_height_m"),
                "tower_structure_height_m": tower.get("tower_height_m"),
                "vertical_status": tower.get("tower_obstacle_vertical_status"),
                "base_type": tower.get("base_type"),
            },
        }
    return result


#: 逐 probe 评价的剖面明细级别。
#:
#: * ``full``：返回逐采样格的完整剖面（诊断/单点复核用）；
#: * ``summary``：只返回判定计数与关键采样格（批量场景用，避免把成千上万采样格
#:   全量序列化进 JSON —— 一个 3 km 射线 × 5 m 采样就是 600+ 格，批量很容易上百 MB）。
PROFILE_DETAIL_FULL = "full"
PROFILE_DETAIL_SUMMARY = "summary"
PROFILE_DETAIL_LEVELS = (PROFILE_DETAIL_FULL, PROFILE_DETAIL_SUMMARY)


def _summarise_profile_samples(samples):
    """把逐格剖面压缩为可审计的摘要（保留极端格，不丢失判定依据）。"""

    def _row(item):
        return {
            "index": item.get("index"), "distance_m": item.get("distance_m"),
            "terrain_min_m": item.get("terrain_min_m"),
            "terrain_max_m": item.get("terrain_max_m"),
            "ray_height_m": item.get("ray_height_m"),
            "clearance_to_terrain_max_m": item.get("clearance_to_terrain_max_m"),
            "status": item.get("status"),
            "valid_pixel_count": item.get("valid_pixel_count"),
            "nodata_pixel_count": item.get("nodata_pixel_count"),
        }

    extremes = []
    for status in (los.TERRAIN_SAMPLE_BLOCKED, los.TERRAIN_SAMPLE_MARGINAL,
                   los.TERRAIN_SAMPLE_UNKNOWN):
        first = next((item for item in samples if item.get("status") == status), None)
        if first is not None:
            extremes.append(_row(first))
    measured = [
        item for item in samples
        if item.get("clearance_to_terrain_max_m") is not None
    ]
    if measured:
        extremes.append(_row(min(measured, key=lambda item: item["clearance_to_terrain_max_m"])))
    return extremes


def evaluate_probe_terrain_los(tower, probe, *, terrain_source, transform,
                               altitude_layer_id=None, nominal_altitude_m=None,
                               sampling_interval_m=None, envelope_radius_m=None,
                               earth_radius_m=None, k_factor=DEFAULT_REFRACTION_K_FACTOR,
                               origin_physical_input=None,
                               origin_availability=None,
                               max_slant_range_m=None,
                               profile_detail=PROFILE_DETAIL_FULL):
    """单个 (塔, probe) 的逐 probe 三维高度与地形 LOS 评价。

    **强制 fail-closed**：只要满足下面任一条，本函数**不调用任何地形采样**并直接返回
    ``status = unknown``：

    * 原点可用性是 :data:`los.ORIGIN_AVAILABILITY_NOT_EVALUABLE`
      （估算原点不高于 FABDEM 地表 —— 绝不假定存在有效视距，也绝不静默抬升高度）；
    * 原点可用性是 :data:`los.ORIGIN_AVAILABILITY_UNKNOWN`（原点或地形证据缺失）；
    * probe 无法解析出权威高度层高度（高度缺失**绝不**当 0，**绝不**用代表高度兜底）。

    返回结构包含：水平位置、雷达原点 EGM2008 正高、probe EGM2008 正高、水平距离、
    三维斜距、仰角、高度解析来源与状态、LOS 三态与逐采样地形剖面、输入指纹。
    """

    tower_record = tower if isinstance(tower, dict) else {}
    probe_record = probe if isinstance(probe, dict) else {}
    tower_id = str(tower_record.get("tower_id") or "")
    availability = (
        origin_availability
        or (origin_physical_input or {}).get("origin_availability")
        or los.ORIGIN_AVAILABILITY_UNKNOWN
    )
    height = los.probe_height(
        probe_record, altitude_layer_id=altitude_layer_id,
        nominal_altitude_m=nominal_altitude_m,
    )
    origin_egm2008 = los.number_or_none(tower_record.get("origin_egm2008_m"))
    origin_metric = _tower_metric(tower_record, transform)
    target_metric = _probe_metric(probe_record, transform)
    base = {
        "tower_id": tower_id,
        "probe_voxel_id": probe_record.get("voxel_id"),
        "probe_grid_id": probe_record.get("grid_id"),
        "horizontal_position": {
            "longitude": probe_record.get("longitude"),
            "latitude": probe_record.get("latitude"),
            "metric": target_metric,
        },
        "radar_origin_egm2008_m": origin_egm2008,
        "radar_origin_metric": origin_metric,
        "radar_origin_source": tower_record.get("origin_source"),
        "radar_origin_state": tower_record.get("origin_state"),
        "radar_origin_state_vocabulary_unchanged": True,
        "radar_origin_availability": availability,
        "origin_physical_input": deepcopy(origin_physical_input),
        "probe_height": height,
        "authoritative_altitude_layer_id": altitude_layer_id,
        "vertical_reference": los.EGM2008_ORTHOMETRIC,
        "representative_height_used": False,
        "earth_radius_m": los.number_or_none(earth_radius_m),
        "k_factor": los.curvature_radius_scale(k_factor),
        "gravity_direction": "target_minus_radar_origin",
        "not_evaluated": list(los.RADAR_TERRAIN_LOS_NOT_EVALUATED),
        "is_detection_probability": False,
    }
    geometry = los.probe_geometry(
        origin_egm2008_m=origin_egm2008,
        target_egm2008_m=height.get("altitude_egm2008_m"),
        origin_metric=origin_metric, target_metric=target_metric,
        earth_radius_m=earth_radius_m, k_factor=k_factor,
    )
    base["probe_geometry"] = geometry
    base["slant_range_m"] = geometry.get("slant_distance_m")
    base["elevation_deg"] = geometry.get("elevation_deg")
    base["frozen_max_slant_range_m"] = los.number_or_none(max_slant_range_m)
    base["within_frozen_device_range"] = _within_range(
        geometry.get("slant_distance_m"), max_slant_range_m,
    )
    base["input_fingerprint"] = los.terrain_los_input_fingerprint(
        tower_id=tower_id,
        origin_egm2008_m=origin_egm2008,
        origin_source=tower_record.get("origin_source"),
        origin_state=tower_record.get("origin_state"),
        origin_evidence_level=tower_record.get("origin_evidence_level"),
        probe_voxel_id=probe_record.get("voxel_id"),
        probe_altitude_layer_id=altitude_layer_id,
        probe_altitude_egm2008_m=height.get("altitude_egm2008_m"),
        probe_height_source=height.get("height_source"),
        probe_longitude=probe_record.get("longitude"),
        probe_latitude=probe_record.get("latitude"),
        dem_source_identity=terrain_source_identity(terrain_source),
        dem_vertical_reference=getattr(terrain_source, "vertical_reference", None),
        sampling={
            "sampling_interval_m": los.number_or_none(sampling_interval_m)
            or DEFAULT_PROFILE_SAMPLING_INTERVAL_M,
            "envelope_radius_m": los.number_or_none(envelope_radius_m)
            if envelope_radius_m is not None else DEFAULT_PROFILE_ENVELOPE_RADIUS_M,
            "read_mode": los.RADAR_TERRAIN_LOS_READ_MODE,
            "algorithm_version": los.RADAR_TERRAIN_LOS_ALGORITHM_VERSION,
        },
        ray_model={
            "earth_radius_m": los.number_or_none(earth_radius_m),
            "k_factor": los.curvature_radius_scale(k_factor),
            "model": "straight_ray_with_optional_bounded_curvature_correction",
            "endpoints_preserved": True,
        },
        algorithm_version=los.RADAR_TERRAIN_LOS_ALGORITHM_VERSION,
    )

    def _unknown(reason):
        return {
            **base,
            "status": los.LOS_STATUS_UNKNOWN,
            "reason": reason,
            "terrain_profile": None,
            "first_obstruction_distance_m": None,
            "minimum_clearance_m": None,
        }

    if availability == los.ORIGIN_AVAILABILITY_NOT_EVALUABLE:
        return _unknown(
            (origin_physical_input or {}).get("reason")
            or "radar_origin_not_evaluable_as_physical_emitter"
        )
    if availability == los.ORIGIN_AVAILABILITY_UNKNOWN:
        return _unknown(
            (origin_physical_input or {}).get("reason") or "radar_origin_not_evaluable"
        )
    if origin_egm2008 is None:
        return _unknown("radar_origin_unresolved")
    if height.get("status") != "passed":
        return _unknown(str(height.get("reason") or "probe_altitude_unresolved"))
    if origin_metric is None or target_metric is None:
        return _unknown("metric_coordinate_unresolved")

    profile = sample_terrain_profile(
        terrain_source, transform=transform, origin_metric=origin_metric,
        target_metric=target_metric, sampling_interval_m=sampling_interval_m,
        envelope_radius_m=envelope_radius_m,
    )
    if profile.get("status") != "passed":
        return _unknown(str(profile.get("reason") or "terrain_profile_unavailable"))
    samples = []
    for sample in profile["samples"]:
        ray = los.ray_height_m(
            origin_egm2008_m=origin_egm2008,
            target_egm2008_m=height["altitude_egm2008_m"],
            horizontal_m=geometry.get("horizontal_distance_m"),
            sample_distance_m=sample.get("distance_m"),
            earth_radius_m=earth_radius_m, k_factor=k_factor,
        )
        status = los.classify_terrain_sample(
            ray_height_m_value=ray,
            terrain_min_m=sample.get("terrain_min_m"),
            terrain_max_m=sample.get("terrain_max_m"),
        )
        samples.append({
            **sample,
            "ray_height_m": ray,
            "clearance_to_terrain_max_m": (
                None if ray is None or sample.get("terrain_max_m") is None
                else ray - sample["terrain_max_m"]
            ),
            "status": status,
            "source_sample_status": sample.get("status"),
        })
    verdict = los.evaluate_terrain_los(
        origin_egm2008_m=origin_egm2008,
        target_egm2008_m=height["altitude_egm2008_m"],
        samples=samples, earth_radius_m=earth_radius_m, k_factor=k_factor,
        sampling_interval_m=(profile.get("sampling") or {}).get("sampling_interval_m"),
        max_slant_range_m=max_slant_range_m,
        origin_evidence_level=tower_record.get("origin_evidence_level"),
    )
    detail = str(profile_detail or PROFILE_DETAIL_FULL)
    if detail not in PROFILE_DETAIL_LEVELS:
        detail = PROFILE_DETAIL_FULL
    return {
        **base,
        "status": verdict["status"],
        "reason": verdict.get("reason"),
        "profile_detail": detail,
        "terrain_profile": {
            "status": profile["status"],
            "source": profile.get("source"),
            "sampling": profile.get("sampling"),
            "read_receipt": profile.get("read_receipt"),
            "samples": (
                samples if detail == PROFILE_DETAIL_FULL
                else _summarise_profile_samples(samples)
            ),
            "sample_count": len(samples),
        },
        "los_verdict": verdict,
        "first_obstruction_distance_m": verdict.get("first_obstruction_distance_m"),
        "first_marginal_distance_m": verdict.get("first_marginal_distance_m"),
        "first_unknown_distance_m": verdict.get("first_unknown_distance_m"),
        "minimum_clearance_m": verdict.get("minimum_clearance_m"),
        "sample_status_counts": verdict.get("counts"),
        "resolution_limitation": verdict.get("resolution_limitation"),
    }


def _within_range(slant_range_m, max_slant_range_m):
    slant = los.number_or_none(slant_range_m)
    limit = los.number_or_none(max_slant_range_m)
    if slant is None or limit is None:
        return None
    return bool(slant <= limit)


def _tower_metric(tower, transform):
    metric = _metric_pair(tower.get("metric"))
    if metric is not None:
        return metric
    point = _metric_pair([tower.get("longitude"), tower.get("latitude")])
    if point is None or transform is None or not hasattr(transform, "to_metric"):
        return None
    converted = transform.to_metric(point)
    return None if converted is None else [float(converted[0]), float(converted[1])]


def _probe_metric(probe, transform):
    metric = _metric_pair(probe.get("metric"))
    if metric is not None:
        return metric
    point = _metric_pair([probe.get("longitude"), probe.get("latitude")])
    if point is None or transform is None or not hasattr(transform, "to_metric"):
        return None
    converted = transform.to_metric(point)
    return None if converted is None else [float(converted[0]), float(converted[1])]


__all__ = [
    "DEFAULT_PROFILE_ENVELOPE_RADIUS_M", "DEFAULT_PROFILE_SAMPLING_INTERVAL_M",
    "DEFAULT_REFRACTION_K_FACTOR", "EARTH_MEAN_RADIUS_M",
    "PROFILE_DETAIL_FULL", "PROFILE_DETAIL_LEVELS", "PROFILE_DETAIL_SUMMARY",
    "SOURCE_VARIANTS", "SOURCE_VARIANT_NATIVE", "SOURCE_VARIANT_WINDOW",
    "TerrainLosExecutionContext", "TerrainSourceCache", "describe_terrain_source",
    "evaluate_probe_terrain_los", "radar_origin_terrain_physical_inputs",
    "sample_terrain_profile", "sample_tower_terrain",
    "source_scale_deg_per_meter", "terrain_source_identity",
    "terrain_source_identity_for_path",
]
