"""Radar 地形视线（terrain LOS）与逐 probe 三维高度的**纯计算**契约。

为什么是本模块
--------------
Round34-T05 已经把 373 座真实铁塔的原点解析链做成分层证据
（设备声明 → 已解析塔顶 → 策略授权的源表估算 → unknown），但**没有**任何一个
模块回答下面两个物理问题：

1. 某个 probe 在**真实航路高度层**上的三维位置是什么（而不是走廊体素的
   代表性高度）；
2. 雷达原点到该 probe 的直线传播路径上，**FABDEM 地形**是否遮挡。

本模块只回答"几何上能否直射"：它**不是**探测概率、不是雷达方程、不是杂波或
多路径结论。建筑遮挡、杂波、多路径、雷达方程与 Pd/RCS 一律保持
``NOT_EVALUATED``（见 :data:`RADAR_TERRAIN_LOS_NOT_EVALUATED`）。

硬边界（违反即返工）
--------------------
* **绝不**把 corridor probe 的代表高度（``overlap_height`` 中点、
  ``probe.altitude_egm2008_m``）当作 Radar 的评估高度；Radar 评估高度只能来自
  权威高度层目录的 ``nominal_altitude_m``。
* **绝不**在高度缺失时静默用 0；缺失即 ``unknown``。
* **绝不**混用 AGL / 椭球高 / EGM2008 正高：本模块的内部 canonical 垂向基准恒为
  :data:`EGM2008_ORTHOMETRIC`，其它基准的取值一律不接受。
* **绝不**在 DEM NoData、采样失败或垂向基准不明时判 ``clear``：一律
  :data:`LOS_STATUS_UNKNOWN`。
* 地球曲率/折射修正**只允许**让几何结论更保守（抬高地形相对射线的高度）。任何
  绕过 :func:`effective_curvature_drop_m` 单调性的一次性经验修正都不得加入。
* 本模块**不** import QGIS/GDAL、不读文件、不写 ``ProjectState``。
"""

from __future__ import annotations

import math
import platform
from numbers import Real

# ---------------------------------------------------------------- 常量与词表

#: 内部 canonical 垂向基准（与 AltitudeLayer / TowerObstacleProfile / Radar 同一语义）。
EGM2008_ORTHOMETRIC = "egm2008_orthometric"

#: Radar 地形 LOS 的算法与语义版本。任何影响结论的语义变化必须同时改这里，
#: 从而改变 input fingerprint 并使旧结果 stale。
RADAR_TERRAIN_LOS_ALGORITHM_VERSION = "radar_terrain_los_v1_straight_ray_sampled_profile"

#: 地形剖面的读取模式（与 V3-C native pixel 读取同源：单次只读、绝不重采样）。
RADAR_TERRAIN_LOS_READ_MODE = "single_read_only_native_window_no_resample_per_request"

#: LOS 三态。``unknown`` **不是** pass，也**不是** blocked。
LOS_STATUS_CLEAR = "clear"
LOS_STATUS_BLOCKED = "blocked"
LOS_STATUS_UNKNOWN = "unknown"
LOS_STATUSES = (LOS_STATUS_CLEAR, LOS_STATUS_BLOCKED, LOS_STATUS_UNKNOWN)

#: 单个剖面采样格的判定（比三态更细，供诊断）。
TERRAIN_SAMPLE_CLEAR = "clear"
TERRAIN_SAMPLE_BLOCKED = "blocked"
TERRAIN_SAMPLE_MARGINAL = "marginal"
TERRAIN_SAMPLE_UNKNOWN = "unknown"
TERRAIN_SAMPLE_STATUSES = (
    TERRAIN_SAMPLE_CLEAR, TERRAIN_SAMPLE_BLOCKED,
    TERRAIN_SAMPLE_MARGINAL, TERRAIN_SAMPLE_UNKNOWN,
)

#: 原点可用性（**物理仿真输入**诊断，与既有 ``origin_state`` 词表**正交**）。
#:
#: ``origin_state`` 回答"原点数值是怎么解析出来的"；本词表回答"该数值是否与地形
#: 自洽、能否作为物理仿真输入"。两者都不改写对方。
ORIGIN_AVAILABILITY_USABLE = "usable"
ORIGIN_AVAILABILITY_UNKNOWN = "unknown"
ORIGIN_AVAILABILITY_NOT_EVALUABLE = "not_evaluable"
ORIGIN_AVAILABILITY_STATES = (
    ORIGIN_AVAILABILITY_USABLE, ORIGIN_AVAILABILITY_UNKNOWN,
    ORIGIN_AVAILABILITY_NOT_EVALUABLE,
)

#: 地表一致性分类。
TERRAIN_CONSISTENCY_ABOVE = "above_terrain"
TERRAIN_CONSISTENCY_COINCIDENT = "coincident_with_terrain"
TERRAIN_CONSISTENCY_BELOW = "below_terrain"
TERRAIN_CONSISTENCY_UNKNOWN = "unknown_terrain"
TERRAIN_CONSISTENCY_STATES = (
    TERRAIN_CONSISTENCY_ABOVE, TERRAIN_CONSISTENCY_COINCIDENT,
    TERRAIN_CONSISTENCY_BELOW, TERRAIN_CONSISTENCY_UNKNOWN,
)

#: 逐采样格判定中"雷达脚下那一格"的容差（米）：距离不超过它即视为塔基采样。
_ORIGIN_SAMPLE_TOLERANCE_M = 1e-6

#: ``origin - terrain`` 在什么容差内算"恰好贴地"。这是**数值容差**，不是业务阈值。
TERRAIN_CONSISTENCY_TOLERANCE_M = 1e-6

#: 逐 probe 高度解析层级（数据驱动，按优先级从高到低）。
PROBE_HEIGHT_TIER_LAYER_CATALOG = "probe_altitude_layer_catalog"
PROBE_HEIGHT_TIER_EXPLICIT_EGM2008 = "probe_explicit_egm2008"
PROBE_HEIGHT_TIER_NONE = "none"
PROBE_HEIGHT_TIERS = (
    PROBE_HEIGHT_TIER_LAYER_CATALOG,
    PROBE_HEIGHT_TIER_EXPLICIT_EGM2008,
    PROBE_HEIGHT_TIER_NONE,
)

#: 明确声明"本模块不评估"的物理效应（防止把纯几何 LOS 冒充探测能力）。
RADAR_TERRAIN_LOS_NOT_EVALUATED = (
    "building_occlusion", "clutter", "multipath", "radar_equation",
    "detection_probability_pd", "rcs", "diffraction", "atmospheric_loss",
    "antenna_gain_pattern", "terrain_scattering_coefficient",
)

#: 冻结几何的引用版本（地形 LOS 只作为独立几何可见性证据，绝不替换既有波束几何）。
FROZEN_RADAR_GEOMETRY_VERSION = "radar_layout_geometry_v1_1"
FROZEN_RADAR_GEOMETRY_PARAMETERS_UNCHANGED = True
FROZEN_RADAR_GEOMETRY_REPLACED_BY_TERRAIN_LOS = False


def _finite(value):
    return (
        isinstance(value, Real) and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def number_or_none(value):
    """有限数值（含 0.0）→ ``float``；其余（``None``/``NaN``/``inf``/``bool``）→ ``None``。

    这是本模块**唯一**的数值入口，因此"缺失"永远不会静默变成 0 之外的任何缺省值，
    也永远不会把 ``False`` 当成 0。
    """

    return float(value) if _finite(value) else None


# ---------------------------------------------------------------- 地球曲率与折射


def curvature_radius_scale(k_factor):
    """有效地球半径倍率 ``k``：``k = 1`` 是纯几何（等效半径 = 真实半径）。

    折射的地表等效写法是 ``R_eff = k · R``（``k = 4/3`` 对应标准大气）。本函数只
    做**输入规范化**：非法或非正的 ``k`` 一律视为 ``1.0``（不引入任何修正），并在
    结果里如实记录，绝不悄悄换成一个"看起来合理"的经验值。
    """

    value = number_or_none(k_factor)
    if value is None or value <= 0.0:
        return 1.0
    return value


def effective_curvature_radius_m(earth_radius_m, k_factor=1.0):
    """有效地球半径 ``R_eff = k · R``（米）。"""

    radius = number_or_none(earth_radius_m) or 0.0
    if radius <= 0.0:
        return 0.0
    return curvature_radius_scale(k_factor) * radius


def effective_curvature_drop_m(*, horizontal_m, earth_radius_m=0.0, k_factor=1.0):
    """地表在**水平距起点 d**处相对切平面的下降量（米，非负）。

    ``drop(d) = d² / (2 · R_eff)``。三个性质必须保持（测试逐条覆盖）：

    1. ``earth_radius_m`` 缺省或非正 ⇒ 返回 0.0，因此射线退化为**直线**；
    2. ``d = 0`` ⇒ 0.0（起点高度不被修正），且 ``drop`` 随 ``d`` 单调不减；
    3. ``k_factor`` 增大 ⇒ ``R_eff`` 增大 ⇒ 返回**非增**（折射把有效半径放大，
       地表弯曲变缓）。注意修正方向：地表下弯会让射线净空**变大**（更乐观），
       因此实验室必须显式给出 ``k_factor`` 才启用它，绝不默认套用 4/3 经验值。
    """

    distance = number_or_none(horizontal_m)
    if distance is None or distance == 0.0:
        return 0.0
    radius = effective_curvature_radius_m(earth_radius_m, k_factor)
    if radius <= 0.0:
        return 0.0
    return distance * distance / (2.0 * radius)


def ray_height_m(*, origin_egm2008_m, target_egm2008_m, horizontal_m, sample_distance_m,
                 earth_radius_m=None, k_factor=1.0):
    """直线（可选曲率修正）射线在距起点 ``sample_distance_m`` 处的 EGM2008 正高。

    纯几何（``earth_radius_m`` 为 ``None`` / 非正）时等价于端点线性插值：

        ``h(d) = h0 + (h1 − h0) · d / D``

    启用曲率修正时，地球表面沿射线方向**向下弯曲**，而射线仍近似直线传播，因此射线
    相对地表的高度**增加**同样的量（这是保守方向：净空变大 ⇒ 结论更容易为 clear）。
    为保持两个端点仍然精确通过，扣除两端点下降量的线性项：

        ``h(d) = h0 + (h1 − h0) · d / D + drop(d) − drop(D) · d / D``

    由此 ``h(0) = h0``、``h(D) = h1`` 恒成立；两端点与形变无关，因此曲率修正
    只作用于中间点。
    """

    origin = number_or_none(origin_egm2008_m)
    target = number_or_none(target_egm2008_m)
    total = number_or_none(horizontal_m)
    distance = number_or_none(sample_distance_m)
    if origin is None or target is None or total is None or distance is None:
        return None
    if total <= 0.0:
        return None
    radius = number_or_none(earth_radius_m) or 0.0
    if radius <= 0.0:
        return origin + (target - origin) * (distance / total)
    target_drop = effective_curvature_drop_m(
        horizontal_m=total, earth_radius_m=radius, k_factor=k_factor,
    )
    sample_drop = effective_curvature_drop_m(
        horizontal_m=distance, earth_radius_m=radius, k_factor=k_factor,
    )
    return (
        origin + (target - origin) * (distance / total)
        + sample_drop - target_drop * (distance / total)
    )


def ray_launch_elevation_deg(*, origin_egm2008_m, target_egm2008_m, horizontal_m,
                             earth_radius_m=None, k_factor=1.0):
    """射线在**雷达原点处的切向仰角**（度）。

    定义与既有冻结几何一致（``target − origin`` 为正、本模型不下倾）：

        ``elevation_deg = atan2(target − origin, horizontal)``

    启用曲率修正时，射线相对地表向上凸出 ``drop(d)``，因此发射切向仰角比弦仰角
    **多**一个量 ``θ_curv = D / (2 · R_eff)``（弧度），即恰为射线高度函数在起点的
    斜率：

        ``θ = atan2(Δh, D) + θ_curv``

    纯几何（``earth_radius_m`` 缺省/非正）时 ``θ_curv = 0``，弦仰角与切向发射角
    逐字相同，因此本函数不改变任何既有几何语义。
    """

    origin = number_or_none(origin_egm2008_m)
    target = number_or_none(target_egm2008_m)
    total = number_or_none(horizontal_m)
    if origin is None or target is None or total is None or total <= 0.0:
        return None
    chord = math.atan2(target - origin, total)
    radius = number_or_none(earth_radius_m) or 0.0
    if radius <= 0.0:
        return math.degrees(chord)
    effective = effective_curvature_radius_m(radius, k_factor)
    if effective <= 0.0:
        return math.degrees(chord)
    return math.degrees(chord + total / (2.0 * effective))


def horizontal_distance_m(origin_metric, target_metric):
    """米制平面内的水平距离（两点同在 EPSG:32651 平面时即为真实地面距离）。"""

    if not (
        isinstance(origin_metric, (list, tuple)) and len(origin_metric) == 2
        and isinstance(target_metric, (list, tuple)) and len(target_metric) == 2
        and all(_finite(value) for value in (*origin_metric, *target_metric))
    ):
        return None
    dx = float(target_metric[0]) - float(origin_metric[0])
    dy = float(target_metric[1]) - float(origin_metric[1])
    return math.hypot(dx, dy)


def slant_distance_m(horizontal_m, vertical_delta_m):
    """三维斜距 ``sqrt(horizontal² + vertical²)``（与冻结几何同一公式）。"""

    horizontal = number_or_none(horizontal_m)
    vertical = number_or_none(vertical_delta_m)
    if horizontal is None or vertical is None:
        return None
    return math.hypot(horizontal, vertical)


def along_track_distances_m(horizontal_m, spacing_m):
    """从 0 到 ``horizontal_m`` 的等距采样位置（含两端点）。

    返回 ``[]`` 表示无法分层（非法输入或跨距非正），调用方必须据此返回 unknown。
    """

    total = number_or_none(horizontal_m)
    step = number_or_none(spacing_m)
    if total is None or step is None or total <= 0.0 or step <= 0.0:
        return []
    count = int(math.floor(total / step)) + 1
    distances = [index * step for index in range(count)]
    if distances[-1] < total:
        distances.append(total)
    else:
        distances[-1] = total
    return distances


# ---------------------------------------------------------------- 逐采样格判定


def classify_terrain_sample(*, ray_height_m_value, terrain_min_m, terrain_max_m,
                            ray_height_at_sample_m=None):
    """一个剖面采样格的四种判定（**保守**优先）。

    * 射线低于格内**最低**地形（或与之等高）⇒ ``blocked``（最好情况也已遮挡）；
    * 射线高于格内**最高**地形 ⇒ ``clear``；
    * 射线夹在最低与最高之间 ⇒ ``marginal``（**切触**：源像元尺度内的真实起伏
      无法解析，因此既不能判 clear，也不该直接判 blocked）；
    * 地形 NoData / 未采样 / 任一数值缺失 ⇒ ``unknown``。

    ``ray_height_at_sample_m`` 是该采样格中心处的射线高度，仅作为诊断附带信息使用
    （采样格共享同一像元时，格中心与像元中心并不重合，这两个高度会有差异）。
    """

    ray = number_or_none(ray_height_m_value)
    low = number_or_none(terrain_min_m)
    high = number_or_none(terrain_max_m)
    if ray is None or low is None or high is None:
        return TERRAIN_SAMPLE_UNKNOWN
    if ray <= low:
        return TERRAIN_SAMPLE_BLOCKED
    if ray > high:
        return TERRAIN_SAMPLE_CLEAR
    return TERRAIN_SAMPLE_MARGINAL


def evaluate_terrain_los(*, origin_egm2008_m, target_egm2008_m, samples,
                         earth_radius_m=None, k_factor=1.0,
                         sampling_interval_m=None, max_slant_range_m=None,
                         origin_evidence_level=None):
    """把地形剖面样本聚合成 LOS 结论。

    ``samples`` 是 :func:`sample_terrain_profile` 的产出（逐采样格的地形
    min/max 与状态）。本函数**不**读文件、**不**做 IO，只做判定聚合：

    1. 起点采样格（d = 0，雷达原点正下方）**不参与**遮挡判定：塔基与地形一致是
       正常事实，不能把塔自己脚下的地面当成遮挡物；该格仍原样保留在剖面里便于审计。
    2. 任一采样格 ``blocked`` ⇒ :data:`LOS_STATUS_BLOCKED`（记录首个遮挡距离）；
    3. 无 blocked 但有 ``marginal`` 或 ``unknown`` ⇒ :data:`LOS_STATUS_UNKNOWN`
       （切触与数据缺口**不得**判 clear）；
    4. 全部 ``clear`` ⇒ :data:`LOS_STATUS_CLEAR`。

    附加输出：

    * ``first_obstruction_distance_m``：首个 blocked 采样格的距离（无则 ``None``）；
    * ``minimum_clearance_m``：有效采样格上 ``射线高度 − 地形最高`` 的最小值
      （负值表示射线穿地）。**没有**有效采样格时为 ``None``（不是 0，也不是
      一个看似合理的缺省净空）；
    * ``clearance_is_discrete_and_sampling_bounded = True`` 与
      ``resolution_limitation``：如实声明最小净空受采样间距限制，采样格之间更高
      的地形不会被看到。
    """

    origin = number_or_none(origin_egm2008_m)
    target = number_or_none(target_egm2008_m)
    items = [item for item in (samples or []) if isinstance(item, dict)]
    base = {
        "algorithm_version": RADAR_TERRAIN_LOS_ALGORITHM_VERSION,
        "origin_egm2008_m": origin,
        "target_egm2008_m": target,
        "sample_count": len(items),
        "sampling_interval_m": number_or_none(sampling_interval_m),
        "earth_radius_m": number_or_none(earth_radius_m),
        "k_factor": curvature_radius_scale(k_factor),
        "origin_evidence_level": origin_evidence_level,
        "frozen_radar_geometry_version": FROZEN_RADAR_GEOMETRY_VERSION,
        "frozen_radar_geometry_parameters_unchanged": FROZEN_RADAR_GEOMETRY_PARAMETERS_UNCHANGED,
        "terrain_los_replaces_beam_geometry": FROZEN_RADAR_GEOMETRY_REPLACED_BY_TERRAIN_LOS,
        "vertical_reference": EGM2008_ORTHOMETRIC,
        "clearance_is_discrete_and_sampling_bounded": True,
        "not_evaluated": list(RADAR_TERRAIN_LOS_NOT_EVALUATED),
        "is_detection_probability": False,
        "is_radar_equation_result": False,
    }
    if origin is None or target is None or not items:
        return {
            **base, "status": LOS_STATUS_UNKNOWN, "reason": "los_inputs_incomplete",
            "first_obstruction_distance_m": None, "minimum_clearance_m": None,
            "resolution_limitation": "no_evaluable_terrain_profile_sample",
            "counts": {status: 0 for status in TERRAIN_SAMPLE_STATUSES},
        }
    if (
        max_slant_range_m is not None
        and number_or_none(max_slant_range_m) is not None
        and _slant_beyond_range(
            origin, target, items, float(max_slant_range_m),
            earth_radius_m=earth_radius_m, k_factor=k_factor,
        )
    ):
        #: 冻结设备参数仍被如实引用：超出最大斜距的几何本来就不在设备作用范围内。
        #: 这里只做**记录**，结论仍由地形剖面决定（本模块不替换波束几何模型）。
        pass

    counts = {status: 0 for status in TERRAIN_SAMPLE_STATUSES}
    first_blocked = None
    minimum_clearance = None
    first_unknown = None
    first_marginal = None
    origin_sample_status = None
    participating = 0
    for index, sample in enumerate(items):
        status = str(sample.get("status") or TERRAIN_SAMPLE_UNKNOWN)
        if status not in counts:
            status = TERRAIN_SAMPLE_UNKNOWN
        distance = number_or_none(sample.get("distance_m"))
        if index == 0 or (distance is not None and distance <= _ORIGIN_SAMPLE_TOLERANCE_M):
            #: 雷达脚下那一格不参与遮挡判定（见 docstring 第 1 条），因此**不进入**
            #: ``counts``：counts 只统计真正参与判定的采样格。用距离而非仅用下标判定，
            #: 避免调用方给出的首格距离非 0 时把塔基误判成遮挡物。
            origin_sample_status = status
            continue
        participating += 1
        counts[status] += 1
        if status == TERRAIN_SAMPLE_BLOCKED and first_blocked is None:
            first_blocked = distance
        if status == TERRAIN_SAMPLE_UNKNOWN and first_unknown is None:
            first_unknown = distance
        if status == TERRAIN_SAMPLE_MARGINAL and first_marginal is None:
            first_marginal = distance
        maximum = number_or_none(sample.get("terrain_max_m"))
        ray = number_or_none(sample.get("ray_height_m"))
        if maximum is not None and ray is not None:
            clearance = ray - maximum
            if minimum_clearance is None or clearance < minimum_clearance:
                minimum_clearance = clearance

    if counts[TERRAIN_SAMPLE_BLOCKED] > 0:
        status, reason = LOS_STATUS_BLOCKED, "terrain_blocks_ray"
    elif counts[TERRAIN_SAMPLE_MARGINAL] > 0 or counts[TERRAIN_SAMPLE_UNKNOWN] > 0:
        status = LOS_STATUS_UNKNOWN
        reason = (
            "terrain_grazing_contact_not_evaluable_as_clear"
            if counts[TERRAIN_SAMPLE_MARGINAL] > 0
            else "terrain_nodata_or_sampling_gap"
        )
    elif participating > 0:
        status, reason = LOS_STATUS_CLEAR, None
    else:
        status, reason = LOS_STATUS_UNKNOWN, "no_evaluable_terrain_profile_sample"

    return {
        **base,
        "status": status,
        "reason": reason,
        "origin_sample_excluded_from_verdict": True,
        "origin_sample_status": origin_sample_status,
        "participating_sample_count": participating,
        "first_obstruction_distance_m": first_blocked,
        "first_marginal_distance_m": first_marginal,
        "first_unknown_distance_m": first_unknown,
        "minimum_clearance_m": minimum_clearance,
        "resolution_limitation": (
            "minimum_clearance_and_first_obstruction_are_bounded_by_the_stated_sampling_interval"
        ),
        "counts": counts,
    }


def _slant_beyond_range(origin, target, items, max_slant_range_m, *, earth_radius_m,
                        k_factor):
    """射线上任一采样的斜距是否超过给定最大斜距（仅用于记录，不改变判定）。"""

    total = None
    for sample in items:
        distance = number_or_none(sample.get("distance_m"))
        if distance is not None:
            total = max(total or 0.0, distance)
    if total is None:
        return False
    #: 斜距始终不超过射线全长（弦长），用全长做一个便宜的保守上界判定。
    return slant_distance_m(total, abs(target - origin)) > max_slant_range_m


# ---------------------------------------------------------------- 逐 probe 高度


def probe_height(probe, *, altitude_layer_id=None, nominal_altitude_m=None):
    """解析一个 probe 的 EGM2008 正高（**数据驱动**，绝不用代表高度兜底）。

    优先级（每一级都只能来自权威数据）：

    1. ``altitude_layer_id``（由既有 :func:`resolve_radar_altitude_layer_id` 解析出的
       权威高度层）在该项目高度层目录里的 ``nominal_altitude_m``；
    2. probe 自带的显式 ``altitude_egm2008_m``（**仅在**它与所需高度层一致、或调用方
       没有给出高度层时的独立证据）；
    3. 都没有 ⇒ ``unknown``。

    明确**不**接受：corridor voxel 的 ``overlap_height`` 中点、``surface_elevation_m``、
    任何 AGL 值、任何非 EGM2008 基准的高度。probe 携带这些字段的事实会被记录在
    ``ignored_representative_fields`` 里，便于审计"没有拿代表高度顶替"。
    """

    item = probe if isinstance(probe, dict) else {}
    catalog_altitude = number_or_none(nominal_altitude_m)
    explicit_altitude = number_or_none(item.get("altitude_egm2008_m"))
    probe_layer_id = item.get("altitude_layer_id")
    ignored = sorted(
        key for key in ("altitude_egm2008_m", "surface_elevation_m")
        if key in item and item.get(key) is not None
    )
    common = {
        "probe_voxel_id": item.get("voxel_id"),
        "probe_grid_id": item.get("grid_id"),
        "probe_altitude_layer_id": probe_layer_id,
        "authoritative_altitude_layer_id": altitude_layer_id,
        "catalog_nominal_altitude_m": catalog_altitude,
        "probe_reported_altitude_egm2008_m": explicit_altitude,
        "vertical_reference": EGM2008_ORTHOMETRIC,
        "representative_height_used": False,
        "corridor_overlap_midpoint_used": False,
        "ignored_representative_fields": ignored,
    }
    if catalog_altitude is not None:
        return {
            **common,
            "status": "passed",
            "altitude_egm2008_m": catalog_altitude,
            "height_source": PROBE_HEIGHT_TIER_LAYER_CATALOG,
            "reason": None,
            "matches_probe_reported_altitude": (
                None if explicit_altitude is None
                else abs(explicit_altitude - catalog_altitude) <= 1e-6
            ),
        }
    if explicit_altitude is not None and altitude_layer_id in (None, ""):
        return {
            **common,
            "status": "passed",
            "altitude_egm2008_m": explicit_altitude,
            "height_source": PROBE_HEIGHT_TIER_EXPLICIT_EGM2008,
            "reason": None,
            "matches_probe_reported_altitude": True,
        }
    reason = (
        "authoritative_altitude_layer_not_in_catalog"
        if altitude_layer_id not in (None, "")
        else "probe_altitude_evidence_missing"
    )
    return {
        **common,
        "status": "unknown",
        "altitude_egm2008_m": None,
        "height_source": PROBE_HEIGHT_TIER_NONE,
        "reason": reason,
    }


def probe_geometry(*, origin_egm2008_m, target_egm2008_m, origin_metric, target_metric,
                   earth_radius_m=None, k_factor=1.0):
    """逐 probe 三维几何：水平距离、三维斜距、仰角、高差。

    水平距离严格为 0（同点垂直剖面）时仰角按几何定义取 ±90°；这不是数值缺省，而是
    ``atan2(Δh, 0)`` 的精确值。
    """

    horizontal = horizontal_distance_m(origin_metric, target_metric)
    origin = number_or_none(origin_egm2008_m)
    target = number_or_none(target_egm2008_m)
    vertical = (
        None if origin is None or target is None else target - origin
    )
    if horizontal == 0.0 and vertical is not None:
        elevation = None if vertical == 0.0 else math.degrees(
            math.copysign(math.pi / 2.0, vertical)
        )
    else:
        elevation = ray_launch_elevation_deg(
            origin_egm2008_m=origin, target_egm2008_m=target, horizontal_m=horizontal,
            earth_radius_m=earth_radius_m, k_factor=k_factor,
        )
    return {
        "horizontal_distance_m": horizontal,
        "vertical_delta_m": vertical,
        "slant_distance_m": slant_distance_m(horizontal, vertical),
        "elevation_deg": elevation,
        "vertical_delta_semantics": "target_minus_radar_origin",
        "vertical_reference": EGM2008_ORTHOMETRIC,
    }


# ---------------------------------------------------------------- 原点物理输入


def origin_physical_input(*, origin_egm2008_m, origin_source, origin_state, terrain_m,
                          terrain_status, tolerance_m=TERRAIN_CONSISTENCY_TOLERANCE_M):
    """Radar 原点作为**物理仿真输入**的独立诊断（不改写 ``origin_state`` 词表）。

    对每个原点回答三件事：

    1. 地形是否可用（``terrain_status == passed`` 且数值有限）；
    2. 原点与地形的**一致性分类**（``above`` / ``coincident`` / ``below``）；
    3. 该原点能否作为 LOS 的**主评价**输入：地形不可用 ⇒ ``unknown``；
       原点数值缺失 ⇒ ``unknown``；原点**不高于**地表 ⇒ ``not_evaluable``
       （**绝不**假定存在有效视距、**绝不**静默抬升原始高度）。

    ``origin_state`` / ``origin_source`` 原样记录，只做引用、不做映射。
    """

    origin = number_or_none(origin_egm2008_m)
    terrain = number_or_none(terrain_m)
    tolerance = number_or_none(tolerance_m)
    tolerance = TERRAIN_CONSISTENCY_TOLERANCE_M if tolerance is None else abs(tolerance)
    record = {
        "origin_egm2008_m": origin,
        "origin_source": origin_source,
        "origin_state": origin_state,
        "origin_state_vocabulary_unchanged": True,
        "terrain_egm2008_m": terrain,
        "terrain_status": terrain_status,
        "vertical_reference": EGM2008_ORTHOMETRIC,
        "tolerance_m": tolerance,
        "site_candidate_identity_preserved": True,
        "origin_height_silently_raised": False,
        "physical_los_assumed_clear": False,
    }
    if str(terrain_status) != "passed" or terrain is None:
        return {
            **record,
            "terrain_consistency": TERRAIN_CONSISTENCY_UNKNOWN,
            "origin_terrain_delta_m": None,
            "origin_availability": ORIGIN_AVAILABILITY_UNKNOWN,
            "physical_los_status": LOS_STATUS_UNKNOWN,
            "reason": "terrain_elevation_unavailable",
            "scenario_offered": False,
        }
    if origin is None:
        return {
            **record,
            "terrain_consistency": TERRAIN_CONSISTENCY_UNKNOWN,
            "origin_terrain_delta_m": None,
            "origin_availability": ORIGIN_AVAILABILITY_UNKNOWN,
            "physical_los_status": LOS_STATUS_UNKNOWN,
            "reason": "radar_origin_unresolved",
            "scenario_offered": False,
        }
    delta = origin - terrain
    if delta > tolerance:
        consistency = TERRAIN_CONSISTENCY_ABOVE
    elif delta < -tolerance:
        consistency = TERRAIN_CONSISTENCY_BELOW
    else:
        consistency = TERRAIN_CONSISTENCY_COINCIDENT
    if consistency in (TERRAIN_CONSISTENCY_BELOW, TERRAIN_CONSISTENCY_COINCIDENT):
        return {
            **record,
            "terrain_consistency": consistency,
            "origin_terrain_delta_m": delta,
            "origin_availability": ORIGIN_AVAILABILITY_NOT_EVALUABLE,
            "physical_los_status": LOS_STATUS_UNKNOWN,
            "reason": (
                "radar_origin_below_fabdem_surface"
                if consistency == TERRAIN_CONSISTENCY_BELOW
                else "radar_origin_coincident_with_fabdem_surface"
            ),
            "scenario_offered": True,
        }
    return {
        **record,
        "terrain_consistency": consistency,
        "origin_terrain_delta_m": delta,
        "origin_availability": ORIGIN_AVAILABILITY_USABLE,
        "physical_los_status": LOS_STATUS_CLEAR,  # 仅表示"可进入主评价"，不是遮挡结论
        "reason": None,
        "scenario_offered": False,
    }


def radar_origin_above_terrain_scenario(*, origin_physical_input_result, terrain_m,
                                        site_base_surface="FABDEM_bare_earth_DTM",
                                        assumed_structure_height_m=None,
                                        assumed_building_height_m=None):
    """**情景专用**的敏感性高度：仅在原点不高于地表时逐个显式给出。

    它明确是 ``scenario_only``：

    * 不写回任何塔记录、不写回 ``ProjectState``；
    * 不冒充 EGM2008 确认事实、不把 ``estimated`` 升级为 ``confirmed``；
    * 站址基面、建筑高度与设备安装位置的假设必须显式记录，缺失即 ``unknown``
      （**绝不**用 0 补齐）。

    ``scenario_origin_m = terrain + 建筑高度（若该站址为楼面塔且已解析） + 塔身高度``。
    """

    record = origin_physical_input_result if isinstance(origin_physical_input_result, dict) else {}
    terrain = number_or_none(terrain_m)
    structure = number_or_none(assumed_structure_height_m)
    building = number_or_none(assumed_building_height_m)
    missing = []
    if terrain is None:
        missing.append("terrain_egm2008_m")
    if structure is None:
        missing.append("tower_structure_height_m")
    base = {
        "scenario_only": True,
        "written_back": False,
        "writes_to_project_state": False,
        "written_to_tower_record": False,
        "presented_as_egm2008_confirmation": False,
        "upgrades_estimated_to_confirmed": False,
        "applies_to_origin_state": record.get("origin_state"),
        "origin_state_unchanged": True,
        "site_base_surface_assumption": site_base_surface,
        "assumed_structure_height_m": structure,
        "assumed_building_height_m": building,
        "equipment_installation_position": "tower_top_equals_phase_center_engineering_assumption",
        "missing_scenario_inputs": missing,
        "basis": (
            "FABDEM_bare_earth_DTM + resolved building height (rooftop only) "
            "+ source tower structure height"
        ),
    }
    if missing:
        return {**base, "status": "unknown", "scenario_origin_egm2008_m": None,
                "reason": "scenario_inputs_missing"}
    total = terrain + structure + (building or 0.0)
    if building is None:
        base["building_height_treated_as_absent_not_zero_for_ground_sites"] = True
    return {
        **base,
        "status": "scenario_only",
        "scenario_origin_egm2008_m": total,
        "reason": None,
    }


# ---------------------------------------------------------------- 指纹


def terrain_los_input_fingerprint(*, tower_id, origin_egm2008_m, origin_source,
                                  origin_state, origin_evidence_level,
                                  probe_voxel_id, probe_altitude_layer_id,
                                  probe_altitude_egm2008_m, probe_height_source,
                                  probe_longitude, probe_latitude,
                                  dem_source_identity, dem_vertical_reference,
                                  sampling, ray_model, algorithm_version, extra=None):
    """LOS 的输入指纹：任何影响结论的输入都必须落在其中。

    直接调 :func:`stable_fingerprint` 之前先把"参与判断的真实配置"逐项列清：
    原点与其证据等级、probe 真实高度与其分辨率来源、DEM 来源与版本与垂向基准、
    采样间距与包络半径、射线模型（曲率/折射）、LOS 算法版本。``extra`` 允许调用方
    追加**显式**条目（例如陆域掩膜版本），绝不隐式读取全局状态。
    """

    record = {
        "fingerprint_schema_version": "radar_terrain_los_input_fingerprint_v1",
        "algorithm_version": algorithm_version,
        "tower_id": None if tower_id is None else str(tower_id),
        "origin": {
            "origin_egm2008_m": number_or_none(origin_egm2008_m),
            "origin_source": origin_source,
            "origin_state": origin_state,
            "origin_evidence_level": origin_evidence_level,
            "origin_state_vocabulary_unchanged": True,
        },
        "probe": {
            "voxel_id": None if probe_voxel_id is None else str(probe_voxel_id),
            "altitude_layer_id": probe_altitude_layer_id,
            "altitude_egm2008_m": number_or_none(probe_altitude_egm2008_m),
            "height_source": probe_height_source,
            "longitude": number_or_none(probe_longitude),
            "latitude": number_or_none(probe_latitude),
            "vertical_reference": EGM2008_ORTHOMETRIC,
            "representative_height_used": False,
        },
        "dem": {
            "source_identity": dem_source_identity,
            "vertical_reference": dem_vertical_reference,
            "read_mode": RADAR_TERRAIN_LOS_READ_MODE,
        },
        "sampling": sampling,
        "ray_model": ray_model,
        "not_evaluated": list(RADAR_TERRAIN_LOS_NOT_EVALUATED),
        "frozen_radar_geometry": {
            "version": FROZEN_RADAR_GEOMETRY_VERSION,
            "parameters_unchanged": FROZEN_RADAR_GEOMETRY_PARAMETERS_UNCHANGED,
            "replaced_by_terrain_los": FROZEN_RADAR_GEOMETRY_REPLACED_BY_TERRAIN_LOS,
        },
    }
    if isinstance(extra, dict):
        for key in sorted(extra):
            record[key] = extra[key]
    return stable_fingerprint(record)


def stable_fingerprint(payload):
    """确定性指纹：对规范化 JSON 文本取 SHA-256（跨进程、跨平台稳定）。

    不依赖 ``dict`` 的插入顺序（键排序）、不依赖浮点表示差异（``repr`` 固定），
    因此同一输入在同一台机器上恒得同一指纹。
    """

    import hashlib
    import json

    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=_fingerprint_default)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fingerprint_default(value):
    if isinstance(value, Real) and not isinstance(value, bool):
        return float(value)
    return str(value)


def fingerprint_environment():
    """指纹配套的环境信息（诊断用，**不参与**指纹本身）。"""

    return {
        "python_version": platform.python_version(),
        "implementation": platform.python_implementation(),
        "algorithm_version": RADAR_TERRAIN_LOS_ALGORITHM_VERSION,
        "read_mode": RADAR_TERRAIN_LOS_READ_MODE,
    }


__all__ = [
    "EGM2008_ORTHOMETRIC",
    "FROZEN_RADAR_GEOMETRY_PARAMETERS_UNCHANGED", "FROZEN_RADAR_GEOMETRY_REPLACED_BY_TERRAIN_LOS",
    "FROZEN_RADAR_GEOMETRY_VERSION",
    "LOS_STATUSES", "LOS_STATUS_BLOCKED", "LOS_STATUS_CLEAR", "LOS_STATUS_UNKNOWN",
    "ORIGIN_AVAILABILITY_NOT_EVALUABLE", "ORIGIN_AVAILABILITY_STATES",
    "ORIGIN_AVAILABILITY_UNKNOWN", "ORIGIN_AVAILABILITY_USABLE",
    "PROBE_HEIGHT_TIERS", "PROBE_HEIGHT_TIER_EXPLICIT_EGM2008",
    "PROBE_HEIGHT_TIER_LAYER_CATALOG", "PROBE_HEIGHT_TIER_NONE",
    "RADAR_TERRAIN_LOS_ALGORITHM_VERSION", "RADAR_TERRAIN_LOS_NOT_EVALUATED",
    "RADAR_TERRAIN_LOS_READ_MODE",
    "TERRAIN_CONSISTENCY_ABOVE", "TERRAIN_CONSISTENCY_BELOW",
    "TERRAIN_CONSISTENCY_COINCIDENT", "TERRAIN_CONSISTENCY_STATES",
    "TERRAIN_CONSISTENCY_TOLERANCE_M", "TERRAIN_CONSISTENCY_UNKNOWN",
    "TERRAIN_SAMPLE_BLOCKED", "TERRAIN_SAMPLE_CLEAR", "TERRAIN_SAMPLE_MARGINAL",
    "TERRAIN_SAMPLE_STATUSES", "TERRAIN_SAMPLE_UNKNOWN",
    "along_track_distances_m", "classify_terrain_sample", "curvature_radius_scale",
    "effective_curvature_drop_m", "effective_curvature_radius_m",
    "evaluate_terrain_los", "fingerprint_environment", "horizontal_distance_m",
    "number_or_none", "origin_physical_input", "probe_geometry", "probe_height",
    "radar_origin_above_terrain_scenario", "ray_height_m", "ray_launch_elevation_deg",
    "slant_distance_m", "stable_fingerprint", "terrain_los_input_fingerprint",
]
