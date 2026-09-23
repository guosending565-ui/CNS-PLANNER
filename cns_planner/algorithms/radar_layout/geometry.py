"""Radar Surveillance Layout V1 — 米制几何与航路采样（纯标准输入输出）。

设计边界
--------

本模块**只消费米制平面坐标**（``[x_m, y_m]``）。经度/纬度的投影由 GIS 边界
（``QgisMetricTransform`` → ``EPSG:32651``）或注入的 transform 完成，算法内部
**绝不出现 degree-as-meter**。

所有距离/角度计算都在米制平面内完成；高度使用既有 ALT-080 / Route3DProfile 的
EGM2008 正高语义（``z`` 为米，正高）。
"""

from __future__ import annotations

import math

#: 米制平面坐标 -> 经纬度的投影协议。实现方可以是 GIS 边界的 QGIS/GDAL 变换，
#: 也可以是天真的等距近似；算法不关心实现，只要求单位是米。
METRIC_CRS = "EPSG:32651"


# ------------------------------------------------------------------------------ helpers


def circular_angle_delta_deg(left_deg, right_deg):
    """两个方位角之间的最短环形夹角，取值 ``[0, 180]``。正确处理 0°/360° 环绕。"""

    delta = abs(float(right_deg) - float(left_deg)) % 360.0
    return min(delta, 360.0 - delta)


def normalize_azimuth_deg(value):
    """归一化到 ``[0, 360)``；``360.0`` 与 ``0.0`` 是同一方向。"""

    result = float(value) % 360.0
    return 0.0 if result == 360.0 else result


def dedupe_azimuths(values, *, tolerance_deg=1e-9):
    """数值容差去重并排序（确定性）。"""

    kept = []
    for value in sorted(normalize_azimuth_deg(item) for item in values):
        if kept and circular_angle_delta_deg(kept[-1], value) <= tolerance_deg:
            continue
        kept.append(value)
    return kept


# ------------------------------------------------------------------------------ metric


def metric_path_length_m(points):
    """米制折线长度（米）。"""

    total = 0.0
    for left, right in zip(points or [], (points or [])[1:]):
        total += math.hypot(float(right[0]) - float(left[0]), float(right[1]) - float(left[1]))
    return total


def interpolate_metric_path(points, spacing_m):
    """按**实际里程**在米制折线上插值采样点。

    与既有 ``CoveragePlannerV1.interpolate_path`` / ``route_point_at`` 同一思想：
    沿折线逐段按里程推进，并**完整保留原始折线顶点**（拐点因此一定被采样到）。
    不是"每个 MH/T 栅格取一个点"，也不改变原始折线几何。首点与末点同样被保留。
    """

    if not points or len(points) < 2:
        return [list(point) for point in (points or [])]
    spacing = float(spacing_m)
    if spacing <= 0 or spacing != spacing:
        raise ValueError("采样间距必须是正的有限数值（米）")
    line = [[float(point[0]), float(point[1])] for point in points]
    result = [list(line[0])]
    for left, right in zip(line, line[1:]):
        dx, dy = right[0] - left[0], right[1] - left[1]
        segment = math.hypot(dx, dy)
        if segment <= 0:
            # 重复顶点：把段末点（=真实顶点）保留下来，避免几何信息丢失。
            if result[-1] != [right[0], right[1]]:
                result.append([right[0], right[1]])
            continue
        steps = max(1, int(math.ceil(segment / spacing)))
        for step in range(1, steps + 1):
            ratio = step / steps
            result.append([left[0] + dx * ratio, left[1] + dy * ratio])
        # 段末点就是真实顶点，直接写回精确坐标（消除浮点误差）。
        result[-1] = [right[0], right[1]]
    return result


def sample_offsets_m(points, spacing_m):
    """与 :func:`interpolate_metric_path` 同序的沿里程偏移（米）。"""

    samples = interpolate_metric_path(points, spacing_m)
    offsets, total = [], 0.0
    for left, right in zip(samples, samples[1:]):
        offsets.append(total)
        total += math.hypot(right[0] - left[0], right[1] - left[1])
    offsets.append(total)
    return offsets


# ------------------------------------------------------------------------------ geometry


def horizontal_distance_m(origin, target):
    return math.hypot(float(target[0]) - float(origin[0]), float(target[1]) - float(origin[1]))


def slant_distance_m(horizontal_m, vertical_delta_m):
    return math.hypot(float(horizontal_m), float(vertical_delta_m))


def elevation_deg(horizontal_m, vertical_delta_m):
    """俯仰角（度）＝ ``atan2(vertical_delta_m, horizontal_m)``。

    约定：``vertical_delta_m = sample_egm2008_m − origin_egm2008_m``，因此目标点高于
    雷达原点时俯仰角为**正**（``0 <= elevation <= 45`` 是雷达向上覆盖的范围）。
    水平距离为 0 时按正上方处理：垂直向上 = 90°，向下 = -90°。
    """

    horizontal = float(horizontal_m)
    vertical = float(vertical_delta_m)
    if horizontal <= 0.0:
        if vertical > 0:
            return 90.0
        if vertical < 0:
            return -90.0
        return 0.0
    return math.degrees(math.atan2(vertical, horizontal))


def bearing_deg(origin, target):
    """米制平面方位角，正北为 0°、顺时针为正、取值 ``[0, 360)``。

    平面（投影）方位角与真实指北方位角的差异由显式米制 CRS 承担；本模型不混用
    地理坐标下的球面方位角。
    """

    dx = float(target[0]) - float(origin[0])
    dy = float(target[1]) - float(origin[1])
    if dx == 0.0 and dy == 0.0:
        return 0.0
    return normalize_azimuth_deg(math.degrees(math.atan2(dx, dy)))


def vertical_delta_m(*, origin_egm2008_m, sample_egm2008_m):
    """雷达原点相对目标点的**高度优势**（米）：``origin − sample``。

    正值表示雷达原点**高于**被覆盖点（目标在雷达下方），这对应正的俯仰角
    ``0 <= elevation_deg <= 45``；负值表示雷达原点低于目标点（不可覆盖）。
    """

    return float(origin_egm2008_m) - float(sample_egm2008_m)


def panel_coverage(*, origin_egm2008_m, sample_egm2008_m, sample_metric, tower_metric,
                   panel_azimuth_deg, parameters):
    """单面阵覆盖判定：同时满足斜距、俯仰、方位三个条件才算覆盖。

    俯仰角约定（与用户给定的 ``0 <= elevation_deg <= 45`` 一致）::

        vertical_delta_m = origin_egm2008_m − sample_egm2008_m
        elevation_deg    = atan2(vertical_delta_m, horizontal_distance_m)

    因此雷达原点高于被覆盖点 ⇒ 正仰角；原点低于目标点 ⇒ 负仰角（不可覆盖）。
    斜距同样使用该高度差的绝对值。

    ``parameters`` 是 :func:`cns_planner.domain.radar_surveillance_layout.
    radar_geometry_parameters` 的返回（含 ``min_slant_range_m`` / ``max_slant_range_m`` /
    ``elevation_min_deg`` / ``elevation_max_deg`` / ``azimuth_half_width_deg``）。
    """

    horizontal = horizontal_distance_m(tower_metric, sample_metric)
    vertical = vertical_delta_m(
        origin_egm2008_m=origin_egm2008_m, sample_egm2008_m=sample_egm2008_m,
    )
    slant = slant_distance_m(horizontal, vertical)
    elevation = elevation_deg(horizontal, vertical)
    bearing = bearing_deg(tower_metric, sample_metric)
    azimuth_delta = circular_angle_delta_deg(bearing, panel_azimuth_deg)
    within_range = (
        float(parameters["min_slant_range_m"]) <= slant <= float(parameters["max_slant_range_m"])
    )
    within_elevation = (
        float(parameters["elevation_min_deg"]) <= elevation <= float(parameters["elevation_max_deg"])
    )
    within_azimuth = azimuth_delta <= float(parameters["azimuth_half_width_deg"])
    return {
        "horizontal_distance_m": horizontal,
        "vertical_delta_m": vertical,
        "slant_distance_m": slant,
        "elevation_deg": elevation,
        "bearing_deg": bearing,
        "panel_azimuth_deg": normalize_azimuth_deg(panel_azimuth_deg),
        "azimuth_delta_deg": azimuth_delta,
        "within_range": within_range,
        "within_elevation": within_elevation,
        "within_azimuth": within_azimuth,
        "covered": bool(within_range and within_elevation and within_azimuth),
    }


def geometry_evaluation(*, origin_egm2008_m, sample_egm2008_m, sample_metric, tower_metric):
    """不做任何阈值判定，只输出 sample × tower 的几何量。"""

    horizontal = horizontal_distance_m(tower_metric, sample_metric)
    vertical = vertical_delta_m(
        origin_egm2008_m=origin_egm2008_m, sample_egm2008_m=sample_egm2008_m,
    )
    return {
        "horizontal_distance_m": horizontal,
        "vertical_delta_m": vertical,
        "slant_distance_m": slant_distance_m(horizontal, vertical),
        "elevation_deg": elevation_deg(horizontal, vertical),
        "bearing_deg": bearing_deg(tower_metric, sample_metric),
    }


def physically_reachable(*, geometry, parameters):
    """只按 range / elevation 过滤（候选方向生成的第一步）。"""

    within_range = (
        float(parameters["min_slant_range_m"])
        <= geometry["slant_distance_m"]
        <= float(parameters["max_slant_range_m"])
    )
    within_elevation = (
        float(parameters["elevation_min_deg"])
        <= geometry["elevation_deg"]
        <= float(parameters["elevation_max_deg"])
    )
    return bool(within_range and within_elevation)


__all__ = [
    "METRIC_CRS",
    "bearing_deg", "circular_angle_delta_deg", "dedupe_azimuths", "elevation_deg",
    "geometry_evaluation", "horizontal_distance_m", "interpolate_metric_path",
    "metric_path_length_m", "normalize_azimuth_deg", "panel_coverage",
    "physically_reachable", "sample_offsets_m", "slant_distance_m", "vertical_delta_m",
]
