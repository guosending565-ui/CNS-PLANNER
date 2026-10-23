"""固定 1000 m CNS 保障宽带的共享米制几何构造器（GIS 适配层，唯一实现）。

Round38-W1A：航路横向保障宽带不再是"按航路可编辑的半宽"，而是唯一契约
``fixed-1000m-v1``（中心线左右各 500 m）。本模块是该契约在**几何**上的唯一构造
与身份来源，供 P14/P15 及后续 W1B/FIG/T3D4 复用；不得在别处再写第二套 buffer。

严格口径（与 W0 设计一致）：

* 流程固定为 源 CRS → 米制 construction CRS → ``buffer(500, cap=round, join=round)``
  → 合法性修复/检查 → 需要展示时再回投影；
* **禁止**直接对经纬度加减数值来近似宽度；
* 米制 CRS 由 :func:`cns_planner.gis.metric_crs.resolve_metric_crs` 从航路地理范围
  解析（或由调用方显式给出），解析失败、非投影、非米制或投影畸变超限一律
  fail-closed 返回 ``status="blocked"``；
* 反经线穿越、空段、少于两个不同点、非有限/越界坐标、非面结果同样阻断；
* 几何身份（fingerprint）显式绑定：完整中心线坐标与点数、route/adoption authority、
  source CRS、construction CRS、固定宽度策略版本、cap/join 规则、构造算法版本。
"""

from __future__ import annotations

from hashlib import sha256
import json
import math

from pyproj import Geod, Transformer
from shapely.geometry import LineString, mapping
from shapely.ops import transform

try:  # Shapely 2.x
    from shapely import make_valid
except ImportError:  # pragma: no cover - supported legacy fallback
    make_valid = None

from ..domain.cns_corridor import (
    CORRIDOR_HALF_WIDTH_M,
    CORRIDOR_TOTAL_WIDTH_M,
    GEOMETRY_ALGORITHM_ID,
    GEOMETRY_ALGORITHM_VERSION,
    WIDTH_CAP_STYLE,
    WIDTH_JOIN_STYLE,
    WIDTH_POLICY_VERSION,
    WIDTH_SEMANTICS,
    corridor_geometry_identity_summary as domain_corridor_geometry_identity_summary,
)
from .metric_crs import resolve_metric_crs


#: 源坐标口径：航路中心线在 ProjectState 中一律是 OGC:CRS84 二维经纬度。
SOURCE_CRS = "OGC:CRS84"

#: 投影长度 / 真实测地长度 的允许比值。米制 CRS 只在自身适用区内近似等距；一旦
#: 航路落在适用区外，投影长度会放大成百上千倍并生成毫无意义的缓冲区。
PROJECTION_DISTORTION_LIMIT = 2.0

#: buffer 圆弧离散段数。只影响 cap/join 的圆弧离散；(x, y) 坐标与 buffer 距离本身
#: 是精确的，因此它不改变"左右各 500 m"这一契约。
QUAD_SEGS = 16

#: 几何指纹前缀（与 ``routepathv1-`` / ``protectiongeometryv1-`` 家族并列）。
FINGERPRINT_PREFIX = "corridorgeometryv1-"

_GEOD = Geod(ellps="WGS84")


class CorridorGeometryError(ValueError):
    """固定宽带几何的输入或构造不满足数值/几何/CRS 安全约束。"""


def _clean_path(path):
    """校验并归一化中心线；**绝不**猜测或修补坐标。"""

    if not isinstance(path, (list, tuple)):
        raise CorridorGeometryError("route_path must be a list of lon/lat points")
    clean = []
    for point in path:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            raise CorridorGeometryError("route_path contains an invalid point")
        lon, lat = float(point[0]), float(point[1])
        if not math.isfinite(lon) or not math.isfinite(lat):
            raise CorridorGeometryError("route_path coordinates must be finite")
        if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
            raise CorridorGeometryError("route_path coordinates are outside WGS84")
        clean.append([lon, lat])
    if len(clean) < 2 or len({tuple(point) for point in clean}) < 2:
        raise CorridorGeometryError("route_path requires two distinct points")
    for start, end in zip(clean, clean[1:]):
        # 单段经度跳变 ≥180° 说明该折线隐含反经线穿越。任何单一中央经线的投影
        # 都会把这样的顶点放到断层两侧，缓冲区会变成横跨整个经度范围的假几何。
        if abs(end[0] - start[0]) >= 180.0:
            raise CorridorGeometryError(
                "route_path crosses the antimeridian and cannot be buffered in a "
                "single metric construction zone")
    return clean


def _geodesic_length_m(path):
    total = 0.0
    for start, end in zip(path, path[1:]):
        _, _, distance = _GEOD.inv(start[0], start[1], end[0], end[1])
        total += float(distance)
    return total


def _json_coordinates(value):
    if isinstance(value, (tuple, list)):
        return [_json_coordinates(item) for item in value]
    return float(value)


def route_geometry_bounds(path):
    """航路地理范围（用于米制 CRS 解析）；输入必须是已校验的中心线。"""

    longitudes = [point[0] for point in path]
    latitudes = [point[1] for point in path]
    return [min(longitudes), min(latitudes), max(longitudes), max(latitudes)]


#: 退化 extent（正南正北或正东正西航路，bbox 零宽/零高）在解析米制 CRS 时使用的
#: 最小扩展量。它**只**影响"该 extent 落在哪个 UTM 分带 / 是否落在 CRS 适用区"的
#: 判定，量级为 10⁻⁶ 度（约 0.1 m），远小于任何航路坐标精度与 500 m 半宽，因此不
#: 改变任何投影结果，也不改变 buffer 距离。
_DEGENERATE_EXTENT_EPSILON_DEG = 1e-6


def resolution_bounds(bounds):
    """把退化 extent 扩展成**最小非退化** extent，用于米制 CRS 解析。

    真实项目里存在严格正南正北 / 正东正西的航路，其经纬 bbox 宽度或高度为 0。
    ``resolve_metric_crs`` 会如实把这种 extent 判为不可用，从而让一条完全正常的
    航路 fail-closed。

    扩展**只作用于退化的那一维**，且只向**正方向**推进一个极小量：这样扩展后的
    extent 必然完整落在中心点所属的那个 UTM 分带内，不会因为"对称扩展"而跨过
    分带边界（经度 0 / ±6° 的整数倍、纬度 0 等都是真实存在的边界）。全部航路坐标
    与 buffer 距离均不受影响。
    """

    if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
        return bounds
    west, south, east, north = (float(value) for value in bounds)
    epsilon = _DEGENERATE_EXTENT_EPSILON_DEG
    if west >= east:
        centre = (west + east) / 2.0
        west, east = min(centre, 180.0 - 2 * epsilon), min(centre + 2 * epsilon, 180.0)
        if west >= east:  # pragma: no cover - 仅当 centre 已在经度上界
            west, east = centre - 2 * epsilon, centre
    if south >= north:
        centre = (south + north) / 2.0
        south, north = max(centre, -90.0 + 2 * epsilon), min(centre + 2 * epsilon, 90.0)
        if south >= north:  # pragma: no cover - 仅当 centre 已在纬度上界
            south, north = centre - 2 * epsilon, centre
    return [west, south, east, north]


def resolve_corridor_metric_crs(path, *, explicit_crs=None):
    """解析本次构造使用的米制 construction CRS（只读，不猜测区域码）。"""

    bounds = route_geometry_bounds(path)
    return resolve_metric_crs(
        explicit_crs=explicit_crs, geographic_bounds=resolution_bounds(bounds),
    )


def corridor_geometry_fingerprint(identity):
    """几何身份的稳定指纹；``identity`` 必须显式包含全部绑定项。"""

    return FINGERPRINT_PREFIX + sha256(
        json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def geometry_identity(
    *, route_path, route_id=None, route_authority=None, source_crs=SOURCE_CRS,
    construction_crs=None, half_width_m=None,
):
    """构造几何身份字典（指纹的**唯一**输入口径）。

    任何一项缺失或与 persisted 结果不一致，都必须被下游判为 geometry/version
    conflict 或 stale —— 因此这里把全部身份项都写进同一份可序列化字典。
    """

    path = _clean_path(route_path)
    return {
        "geometry_algorithm_id": GEOMETRY_ALGORITHM_ID,
        "geometry_algorithm_version": GEOMETRY_ALGORITHM_VERSION,
        "width_policy_version": WIDTH_POLICY_VERSION,
        "width_semantics": WIDTH_SEMANTICS,
        "corridor_half_width_m": (
            CORRIDOR_HALF_WIDTH_M if half_width_m is None else float(half_width_m)
        ),
        "corridor_total_width_m": (
            CORRIDOR_TOTAL_WIDTH_M if half_width_m is None else float(half_width_m) * 2.0
        ),
        "cap_style": WIDTH_CAP_STYLE,
        "join_style": WIDTH_JOIN_STYLE,
        "quad_segs": QUAD_SEGS,
        "source_crs": str(source_crs or SOURCE_CRS),
        "construction_crs": str(construction_crs) if construction_crs else None,
        "route_id": None if route_id is None else str(route_id),
        "route_authority": route_authority,
        "point_count": len(path),
        "route_path": path,
    }


def build_fixed_corridor_geometry(
    route_path, *, route_id=None, route_authority=None, source_crs=SOURCE_CRS,
    construction_crs=None, half_width_m=None, quad_segs=QUAD_SEGS,
):
    """构造固定 1000 m 保障宽带的精确米制缓冲区。

    返回 ``status="available"`` 时携带：几何身份指纹、construction/source CRS、
    投影畸变比、投影面积与范围、OGC:CRS84 边界几何。返回 ``status="blocked"`` 时
    携带 ``reason`` 且**不**给出任何几何 —— 调用方必须 fail-closed，不得用近似
    几何继续计算。
    """

    width = CORRIDOR_HALF_WIDTH_M if half_width_m is None else float(half_width_m)
    if not math.isfinite(width) or width <= 0.0:
        return _blocked("half_width_m must be finite and greater than zero")
    if half_width_m is not None and width != CORRIDOR_HALF_WIDTH_M:
        # 固定契约下不允许调用方按航路改宽度：这必须是显式违约，而不是被静默接受。
        return _blocked(
            f"half_width_m violates the fixed width policy {WIDTH_POLICY_VERSION}: "
            f"expected {CORRIDOR_HALF_WIDTH_M}, got {width}")

    try:
        path = _clean_path(route_path)
    except CorridorGeometryError as exc:
        return _blocked(str(exc))

    resolution = resolve_corridor_metric_crs(path, explicit_crs=construction_crs)
    if resolution.get("status") != "resolved":
        return _blocked(
            "metric construction CRS unresolved: "
            f"{resolution.get('reason') or 'unknown_reason'}")
    authority = str(resolution.get("authority") or "")
    if not authority:
        return _blocked("metric construction CRS resolved without an authority")

    try:
        to_metric = Transformer.from_crs(source_crs, authority, always_xy=True)
        to_source = Transformer.from_crs(authority, source_crs, always_xy=True)
    except Exception:  # noqa: BLE001 - pyproj 的失败类型不统一，一律 fail-closed
        return _blocked("crs transform unavailable for the declared source/construction CRS")

    try:
        metric_line = transform(to_metric.transform, LineString(path))
    except Exception:  # noqa: BLE001
        return _blocked("route_path could not be projected into the construction CRS")
    if metric_line.is_empty or not math.isfinite(metric_line.length) or metric_line.length <= 0.0:
        return _blocked("route_path has no measurable horizontal length")

    geodesic_length = _geodesic_length_m(path)
    if geodesic_length <= 0.0:
        return _blocked("route_path has no measurable geodesic length")
    distortion = metric_line.length / geodesic_length
    if not math.isfinite(distortion) or not (
            1.0 / PROJECTION_DISTORTION_LIMIT <= distortion <= PROJECTION_DISTORTION_LIMIT):
        return _blocked(
            "route_path is outside the construction CRS area of use: projected/geodesic "
            f"length ratio {distortion:.3f} exceeds {PROJECTION_DISTORTION_LIMIT}")

    try:
        polygon = metric_line.buffer(
            width, quad_segs=int(quad_segs),
            cap_style=1 if WIDTH_CAP_STYLE == "round" else 2,
            join_style=1 if WIDTH_JOIN_STYLE == "round" else 2,
        )
        if not polygon.is_valid:
            polygon = make_valid(polygon) if make_valid is not None else polygon.buffer(0)
    except Exception:  # noqa: BLE001
        return _blocked("route buffer construction failed")
    if polygon.is_empty or polygon.geom_type not in {"Polygon", "MultiPolygon"}:
        return _blocked("route buffer did not produce polygonal geometry")
    if not polygon.is_valid:
        return _blocked("route buffer is not valid in the construction CRS")

    try:
        wgs84 = transform(to_source.transform, polygon)
    except Exception:  # noqa: BLE001
        return _blocked("route buffer could not be reprojected to the source CRS")
    if wgs84.is_empty or not wgs84.is_valid:
        return _blocked("route buffer is not valid in the output CRS")
    if not all(math.isfinite(value) for value in wgs84.bounds):
        return _blocked("route buffer produced non-finite bounds")

    encoded = mapping(wgs84)
    ring_count = (len(polygon.interiors) + 1 if polygon.geom_type == "Polygon" else
                  sum(len(item.interiors) + 1 for item in polygon.geoms))
    identity = geometry_identity(
        route_path=path, route_id=route_id, route_authority=route_authority,
        source_crs=source_crs, construction_crs=authority, half_width_m=width,
    )
    return {
        "status": "available",
        "reason": None,
        "geometry_algorithm_id": GEOMETRY_ALGORITHM_ID,
        "geometry_algorithm_version": GEOMETRY_ALGORITHM_VERSION,
        "width_policy_version": WIDTH_POLICY_VERSION,
        "width_semantics": WIDTH_SEMANTICS,
        "corridor_half_width_m": float(width),
        "corridor_total_width_m": float(width) * 2.0,
        "cap_style": WIDTH_CAP_STYLE,
        "join_style": WIDTH_JOIN_STYLE,
        "quad_segs": int(quad_segs),
        "source_crs": str(source_crs),
        "construction_crs": authority,
        "metric_crs_source": resolution.get("source"),
        "metric_crs_unit": resolution.get("unit"),
        "route_id": None if route_id is None else str(route_id),
        "route_authority": route_authority,
        "point_count": len(path),
        "route_path": path,
        "route_length_projected_m": float(metric_line.length),
        "route_length_geodesic_m": float(geodesic_length),
        "projected_to_geodesic_length_ratio": float(distortion),
        "projection_distortion_limit": PROJECTION_DISTORTION_LIMIT,
        "construction_zone_checked": True,
        "projected_area_m2": float(polygon.area),
        "projected_perimeter_m": float(polygon.length),
        "projected_bounds_m": [float(value) for value in polygon.bounds],
        "boundary_ring_count": int(ring_count),
        "is_valid": bool(polygon.is_valid and wgs84.is_valid),
        "geometry_fingerprint": corridor_geometry_fingerprint(identity),
        "fingerprint_inputs": sorted(identity),
        "geometry": {
            "type": encoded["type"],
            "coordinates": _json_coordinates(encoded["coordinates"]),
        },
    }


def build_fixed_corridor_geometries(routes, corridor_policy=None, *, construction_crs=None):
    """为**全部**权威航路构造固定宽带几何，返回 ``{route_id: 身份摘要}``。

    这是 P14/P15 及后续消费者的唯一共享入口：所有航路消费**同一个**宽度契约与
    **同一份**构造算法，绝不按航路各造一套 buffer。

    返回的是 :func:`corridor_geometry_identity_summary` 的摘要（不含完整 ring 坐标）：
    canonical 结果只需身份即可判定 current/stale，完整边界坐标留给展示层按需重建。
    """

    specs = (corridor_policy or {}).get("routes") or {}
    result = {}
    for route in routes or []:
        if not isinstance(route, dict):
            continue
        route_id = str(route.get("route_id") or "")
        if not route_id:
            continue
        spec = specs.get(route_id) if isinstance(specs, dict) else None
        result[route_id] = corridor_geometry_identity_summary(
            build_fixed_corridor_geometry(
                route.get("path") or [],
                route_id=route_id,
                route_authority=_route_authority(route),
                construction_crs=construction_crs or _route_construction_crs(route, spec),
            )
        )
    return result


def _route_construction_crs(route, spec):
    """显式米制 CRS 的解析顺序：航路显式声明 → 走廊规格 → 由地理范围推导。

    **绝不**猜测区域 EPSG 码；三级都不可用时返回 ``None``，由
    :func:`cns_planner.gis.metric_crs.resolve_metric_crs` fail-closed。
    """

    for candidate in (
        (route or {}).get("construction_crs"),
        (route or {}).get("metric_crs"),
        (spec or {}).get("construction_crs"),
        (spec or {}).get("metric_crs"),
    ):
        value = str(candidate or "").strip()
        if value:
            return value
    return None


def _route_authority(route):
    """航路权威身份（route authority / adoption identity），缺失即显式 ``None``。"""

    authority = (route or {}).get("authority")
    if isinstance(authority, dict):
        return authority
    provenance = (route or {}).get("provenance")
    if isinstance(provenance, dict):
        return {
            "kind": provenance.get("source_type") or (route or {}).get("source_type"),
            "adoption_fingerprint": provenance.get("adoption_fingerprint"),
            "candidate_fingerprint": provenance.get("candidate_fingerprint"),
            "validation_fingerprint": provenance.get("validation_fingerprint"),
        }
    return None


def _blocked(reason):
    return {
        "status": "blocked",
        "reason": reason,
        "geometry_algorithm_id": GEOMETRY_ALGORITHM_ID,
        "geometry_algorithm_version": GEOMETRY_ALGORITHM_VERSION,
        "width_policy_version": WIDTH_POLICY_VERSION,
        "corridor_half_width_m": CORRIDOR_HALF_WIDTH_M,
        "corridor_total_width_m": CORRIDOR_TOTAL_WIDTH_M,
        "cap_style": WIDTH_CAP_STYLE,
        "join_style": WIDTH_JOIN_STYLE,
        "source_crs": SOURCE_CRS,
        "construction_crs": None,
        "geometry_fingerprint": None,
        "geometry": None,
    }


def corridor_geometry_identity_summary(geometry):
    """从一次构造结果提取**可序列化身份摘要**（不含完整 ring 坐标）。

    唯一实现在 Domain 层（"哪些字段构成几何身份"本身就是契约），这里只做再导出，
    保证 GIS 层与算法层永远消费**同一份**身份字段清单。
    """

    return domain_corridor_geometry_identity_summary(geometry)


__all__ = [
    "CorridorGeometryError", "FINGERPRINT_PREFIX", "PROJECTION_DISTORTION_LIMIT",
    "QUAD_SEGS", "SOURCE_CRS", "build_fixed_corridor_geometry",
    "build_fixed_corridor_geometries", "corridor_geometry_fingerprint",
    "corridor_geometry_identity_summary", "geometry_identity",
    "resolve_corridor_metric_crs", "route_geometry_bounds",
]
