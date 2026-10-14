"""P17 航路保护走廊的只读三维投影。

本模块只消费已持久化的 ``continuous_service_acceptability`` 结果，并把其水平
走廊转换成可视化几何。它不调用 P17 算法、不写 ProjectState，也不把工程示意
或软件默认高度升级为权威保护空域。
"""

from __future__ import annotations

import math
from copy import deepcopy

from pyproj import Geod, Transformer
from shapely.geometry import LineString, mapping
from shapely.ops import transform

try:  # Shapely 2.x
    from shapely import make_valid
except ImportError:  # pragma: no cover - supported legacy fallback
    make_valid = None

from ..domain.layered_route import stable_fingerprint


CONSTRUCTION_CRS = "EPSG:32651"
SOURCE_CRS = "EPSG:4326"
PREVIEW_DISCLAIMER = "自定义保护区示意，非已确认 P17 结果"

#: 投影长度 / 真实测地长度 的允许比值。UTM 51N 只在自身分带内近似等距；航路一旦
#: 落在分带之外（例如跨 180° 经线），投影长度会放大成百上千倍并生成毫无意义的
#: 缓冲区。超过该比值即视为区域约束不满足，直接 fail-closed。
PROJECTION_DISTORTION_LIMIT = 2.0

_GEOD = Geod(ellps="WGS84")


#: 渲染实例类型：同一业务 ``object_id`` 下历史投影与用户示意**永不**共用实例键。
INSTANCE_KIND_PROJECT = "historical_project_result"
INSTANCE_KIND_PREVIEW = "engineering_scenario_preview"


class ProtectionVolumeInputError(ValueError):
    """工程示意输入不满足数值或几何安全约束。"""


def protection_render_instance_id(*, object_id, instance_kind, route_authority,
                                  route_geometry_identity,
                                  protection_geometry_identity):
    """稳定唯一的**渲染实例键**（frontend 选择/动画/事件/开关/清除的唯一凭据）。

    业务 ``object_id`` 是冻结的（``protection-corridor:R0005:ALT-100`` 会同时对应
    若干份并存几何），所以实例身份必须另外携带：实例类型、航路权威、航路几何身份
    与保护几何身份。数组下标只是响应内的排列顺序，**绝不**充当长期标识。
    """

    identity = route_geometry_identity if isinstance(route_geometry_identity, dict) else {}
    return stable_fingerprint(
        {
            "object_id": object_id,
            "instance_kind": instance_kind,
            "route_authority": route_authority,
            "route_geometry_fingerprint": identity.get("fingerprint"),
            "route_geometry_point_count": identity.get("point_count"),
            "protection_geometry_identity": protection_geometry_identity,
        },
        prefix="protectioninstancev1-",
    )


def _items(container):
    if isinstance(container, list):
        return container
    if isinstance(container, dict):
        values = container.get("items")
        if isinstance(values, list):
            return values
        if isinstance(values, dict):
            return list(values.values())
    return []


def _clean_path(path):
    if not isinstance(path, list):
        raise ProtectionVolumeInputError("route_path must be a list")
    clean = []
    for point in path:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            raise ProtectionVolumeInputError("route_path contains an invalid point")
        lon, lat = float(point[0]), float(point[1])
        if not math.isfinite(lon) or not math.isfinite(lat):
            raise ProtectionVolumeInputError("route_path coordinates must be finite")
        if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
            raise ProtectionVolumeInputError("route_path coordinates are outside WGS84")
        clean.append([lon, lat])
    if len(clean) < 2 or len({tuple(point) for point in clean}) < 2:
        raise ProtectionVolumeInputError("route_path requires two distinct points")
    return clean


def _geodesic_length_m(path):
    total = 0.0
    for start, end in zip(path, path[1:]):
        _, _, distance = _GEOD.inv(start[0], start[1], end[0], end[1])
        total += float(distance)
    return total


def route_path_fingerprint(path):
    """返回只与 OGC:CRS84 二维折线有关的稳定身份。"""

    clean = _clean_path(path)
    return stable_fingerprint(
        {"path_crs": "OGC:CRS84", "path": clean}, prefix="routepathv1-"
    )


def _json_coordinates(value):
    if isinstance(value, (tuple, list)):
        return [_json_coordinates(item) for item in value]
    return float(value)


def build_horizontal_corridor(route_path, half_width_m):
    """在 EPSG:32651 中构造带圆端/圆连接的真实米制缓冲区。"""

    path = _clean_path(route_path)
    try:
        width = float(half_width_m)
    except (TypeError, ValueError):
        raise ProtectionVolumeInputError("half_width_m must be numeric") from None
    if not math.isfinite(width) or width <= 0.0:
        raise ProtectionVolumeInputError("half_width_m must be finite and greater than zero")

    to_metric = Transformer.from_crs(SOURCE_CRS, CONSTRUCTION_CRS, always_xy=True)
    to_wgs84 = Transformer.from_crs(CONSTRUCTION_CRS, SOURCE_CRS, always_xy=True)
    for start, end in zip(path, path[1:]):
        # 单段经度跳变 ≥180° 说明该折线隐含反经线穿越。EPSG:32651 只有一个中央
        # 经线，投影/反投影会把这样的顶点放到断层两侧，缓冲区会变成横跨整个经度
        # 范围的假几何，因此必须在构造前 fail-closed。
        if abs(end[0] - start[0]) >= 180.0:
            raise ProtectionVolumeInputError(
                "route_path crosses the antimeridian, which cannot be represented in "
                "the EPSG:32651 construction zone")
    metric_line = transform(to_metric.transform, LineString(path))
    if metric_line.is_empty or metric_line.length <= 0.0:
        raise ProtectionVolumeInputError("route_path has no measurable horizontal length")
    geodesic_length = _geodesic_length_m(path)
    if geodesic_length <= 0.0:
        raise ProtectionVolumeInputError("route_path has no measurable geodesic length")
    distortion = metric_line.length / geodesic_length
    if not math.isfinite(distortion) or not (
            1.0 / PROJECTION_DISTORTION_LIMIT <= distortion <= PROJECTION_DISTORTION_LIMIT):
        raise ProtectionVolumeInputError(
            "route_path is outside the EPSG:32651 construction zone: projected/geodesic "
            f"length ratio {distortion:.3f} exceeds {PROJECTION_DISTORTION_LIMIT}")
    polygon = metric_line.buffer(width, quad_segs=16, cap_style=1, join_style=1)
    if not polygon.is_valid:
        polygon = make_valid(polygon) if make_valid is not None else polygon.buffer(0)
    if polygon.is_empty or polygon.geom_type not in {"Polygon", "MultiPolygon"}:
        raise ProtectionVolumeInputError("route buffer did not produce polygonal geometry")
    wgs84 = transform(to_wgs84.transform, polygon)
    if wgs84.is_empty or not wgs84.is_valid:
        raise ProtectionVolumeInputError("route buffer is not valid in the output CRS")
    if not all(math.isfinite(value) for value in wgs84.bounds):
        raise ProtectionVolumeInputError("route buffer produced non-finite WGS84 bounds")
    encoded = mapping(wgs84)
    ring_count = (len(polygon.interiors) + 1 if polygon.geom_type == "Polygon" else
                  sum(len(item.interiors) + 1 for item in polygon.geoms))
    return {
        "status": "available",
        "construction_crs": CONSTRUCTION_CRS,
        "output_crs": "OGC:CRS84",
        "buffer_half_width_m": width,
        "cap_style": "round",
        "join_style": "round",
        "route_length_projected_m": float(metric_line.length),
        "route_length_geodesic_m": float(geodesic_length),
        "projected_to_geodesic_length_ratio": float(distortion),
        "construction_zone_checked": True,
        "projected_area_m2": float(polygon.area),
        "projected_perimeter_m": float(polygon.length),
        "projected_bounds_m": [float(value) for value in polygon.bounds],
        "boundary_ring_count": int(ring_count),
        "is_valid": bool(polygon.is_valid and wgs84.is_valid),
        "geometry": {
            "type": encoded["type"],
            "coordinates": _json_coordinates(encoded["coordinates"]),
        },
    }


def _p17_route(container, route_id):
    for item in _items((container or {}).get("routes")):
        if isinstance(item, dict) and str(item.get("route_id")) == str(route_id):
            return item
    return None


def _blocked_geometry(reason):
    return {
        "status": "blocked",
        "reason": reason,
        "construction_crs": CONSTRUCTION_CRS,
        "output_crs": "OGC:CRS84",
        "geometry": None,
    }


def _p17_evidence(corridor):
    fields = (
        "cns_requirement_corridor_half_width_m", "D_protection_m",
        "outer_half_width_m", "V_relative_mps", "relative_speed_basis", "T_chain_s",
        "T_chain_components_s", "D_separation_m", "D_separation_authority",
        "D_uncertainty_m", "D_uncertainty_authority", "D_maneuver_m",
        "D_maneuver_authority", "formula", "status", "route_length_m",
    )
    return {field: deepcopy(corridor.get(field)) for field in fields}


def project_p17_protection_volumes(state, route_entries, *, current_algorithm_version):
    """把已持久化 P17 corridor 严格绑定到同一路线几何后投影。"""

    state = state if isinstance(state, dict) else {}
    p17 = state.get("continuous_service_acceptability") or {}
    volumes = []
    for route in route_entries or []:
        if not isinstance(route, dict):
            continue
        route_id = str(route.get("route_id") or "")
        p17_route = _p17_route(p17, route_id)
        corridor = (p17_route or {}).get("corridor") or {}
        p17_path = corridor.get("route_path")
        p17_identity = None
        if isinstance(p17_path, list):
            try:
                p17_identity = {
                    "route_id": route_id,
                    "point_count": len(p17_path),
                    "fingerprint": route_path_fingerprint(p17_path),
                    "path_crs": "OGC:CRS84",
                }
            except ProtectionVolumeInputError:
                p17_identity = None
        identity = deepcopy(route.get("identity") or {})
        altitude = route.get("altitude") or {}
        route_path = route.get("path")
        route_anchor = None
        if isinstance(route_path, list) and route_path:
            anchor_point = route_path[len(route_path) // 2]
            if isinstance(anchor_point, (list, tuple)) and len(anchor_point) >= 2:
                route_anchor = [float(anchor_point[0]), float(anchor_point[1])]
        layer_id = str(altitude.get("altitude_layer_id") or "unconfirmed")
        required = []
        common = {
            "object_id": f"protection-corridor:{route_id}:{layer_id}",
            "mode": "project_result",
            "route_id": route_id,
            "altitude_layer_id": altitude.get("altitude_layer_id"),
            "route_authority": route.get("authority"),
            "route_status": route.get("status"),
            "route_geometry_identity": identity,
            "route_anchor": route_anchor,
            "p17_route_geometry_identity": p17_identity,
            "protection_input_fingerprint": p17.get("input_fingerprint"),
            "p17_status": p17.get("status") or "not_calculated",
            "p17_stale_reason": p17.get("stale_reason"),
            "p17_algorithm_id": p17.get("algorithm_id"),
            "p17_algorithm_version": p17.get("algorithm_version"),
            "current_code_algorithm_version": current_algorithm_version,
            "algorithm_version_conflict": (
                str(p17.get("algorithm_version") or "") != str(current_algorithm_version)
            ),
            "geometry_dimension": "none",
            "vertical_reference": altitude.get("vertical_reference") or "EGM2008",
            "vertical_extent_status": "vertical_extent_unconfirmed",
            "nominal_altitude_slice_egm2008_m": altitude.get("nominal_altitude_m"),
            "lower_altitude_m": None,
            "upper_altitude_m": None,
            "evidence_status": "insufficient",
            "current": False,
            "historical": True,
            "stale": True,
            "required_evidence": required,
            "source": {
                "container": "continuous_service_acceptability.routes[].corridor",
                "route_path_field": "corridor.route_path",
                "half_width_field": "corridor.outer_half_width_m",
            },
        }
        matches = bool(
            p17_identity
            and isinstance(route_path, list)
            and route_path_fingerprint(route_path) == p17_identity["fingerprint"]
        )
        if not p17_route or not p17_identity:
            required.append("continuous_service_acceptability.routes[].corridor.route_path")
            common.update({
                "status": "evidence_required",
                "geometry": _blocked_geometry("p17_corridor_missing"),
            })
        elif not matches:
            required.append("matching_route_geometry_identity")
            common.update({
                "status": "geometry_version_conflict",
                "geometry": _blocked_geometry("geometry_version_conflict"),
            })
        elif corridor.get("outer_half_width_m") in (None, ""):
            required.append(
                "continuous_service_acceptability.routes[].corridor.outer_half_width_m"
            )
            common.update({
                "status": "evidence_required",
                "geometry": _blocked_geometry("outer_half_width_m_missing"),
            })
        else:
            try:
                geometry = build_horizontal_corridor(
                    p17_path, corridor.get("outer_half_width_m"))
            except ProtectionVolumeInputError as exc:
                required.append("valid_p17_horizontal_corridor_geometry")
                common.update({
                    "status": "evidence_required",
                    "geometry": _blocked_geometry(str(exc)),
                })
            else:
                required.append("confirmed_vertical_lower_and_upper_interfaces")
                common.update({
                    "status": "historical_stale",
                    "geometry_dimension": "horizontal_boundary_with_altitude_slice",
                    "evidence_status": "horizontal_confirmed_vertical_unconfirmed",
                    "geometry": geometry,
                    "outer_half_width_m": float(corridor["outer_half_width_m"]),
                    "route_length_m": corridor.get("route_length_m"),
                    "corridor_evidence": _p17_evidence(corridor),
                    "protection_geometry_identity": stable_fingerprint(
                        {
                            "route_path_fingerprint": p17_identity["fingerprint"],
                            "outer_half_width_m": float(corridor["outer_half_width_m"]),
                            "vertical": "nominal_slice_only",
                            "nominal_altitude_m": altitude.get("nominal_altitude_m"),
                            "p17_input_fingerprint": p17.get("input_fingerprint"),
                        },
                        prefix="protectiongeometryv1-",
                    ),
                })
        # 实例键在状态判定之后统一计算：无论该实例当前是 available 还是缺证，
        # 同一个（航路权威 + 航路几何 + 保护几何）组合只有一个键。
        common["render_instance_id"] = protection_render_instance_id(
            object_id=common["object_id"],
            instance_kind=INSTANCE_KIND_PROJECT,
            route_authority=common.get("route_authority"),
            route_geometry_identity=common.get("route_geometry_identity"),
            protection_geometry_identity=common.get("protection_geometry_identity"),
        )
        volumes.append(common)

    available = [row for row in volumes if row.get("status") == "historical_stale"]
    blocked = [row for row in volumes if row.get("geometry", {}).get("status") == "blocked"]
    if available:
        status = "historical_only"
    elif blocked:
        status = "blocked"
    else:
        status = "not_calculated"
    instance_ids = [row["render_instance_id"] for row in volumes]
    duplicate_ids = sorted({
        value for value in instance_ids if instance_ids.count(value) > 1
    })
    return {
        "schema_version": "visualization-3d-protection-volumes/v1",
        "mode": "project_result",
        "status": status,
        "stored_algorithm_version": p17.get("algorithm_version"),
        "current_code_algorithm_version": current_algorithm_version,
        "algorithm_version_conflict": (
            str(p17.get("algorithm_version") or "") != str(current_algorithm_version)
        ),
        "protection_input_fingerprint": p17.get("input_fingerprint"),
        "volume_count": len(volumes),
        "available_horizontal_count": len(available),
        "blocked_count": len(blocked),
        "render_instance_ids": instance_ids,
        "render_instance_id_unique": not duplicate_ids,
        "duplicate_render_instance_ids": duplicate_ids,
        "instance_identity_rule": (
            "实例键 = 业务 object_id + 实例类型 + route_authority + 航路几何身份 + "
            "保护几何身份；同一冻结 object_id 下的多份几何必须得到不同键，"
            "前端选择/动画/事件/开关/清除一律以该键为准，数组下标不得充当长期标识"
        ),
        "volumes": volumes,
        "read_only": True,
        "writes_performed": 0,
        "algorithms_invoked": [],
    }


def _finite_number(value, field):
    if value in (None, ""):
        raise ProtectionVolumeInputError(f"{field} is required")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ProtectionVolumeInputError(f"{field} must be numeric") from None
    if not math.isfinite(number):
        raise ProtectionVolumeInputError(f"{field} must be finite")
    return number


def build_engineering_preview(route_entry, *, half_width_m, lower_altitude_m,
                              upper_altitude_m):
    """构造瞬时工程示意；所有尺寸均必须由调用者显式给出。"""

    width = _finite_number(half_width_m, "half_width_m")
    lower = _finite_number(lower_altitude_m, "lower_altitude_m")
    upper = _finite_number(upper_altitude_m, "upper_altitude_m")
    if width <= 0.0 or width > 100000.0:
        raise ProtectionVolumeInputError(
            "half_width_m must be greater than zero and no more than 100000")
    if lower >= upper:
        raise ProtectionVolumeInputError(
            "lower_altitude_m must be less than upper_altitude_m")
    if lower < -1000.0 or upper > 20000.0:
        raise ProtectionVolumeInputError(
            "lower/upper altitude must stay within the visualization safety range")
    route = route_entry if isinstance(route_entry, dict) else {}
    path = route.get("path")
    geometry = build_horizontal_corridor(path, width)
    identity = deepcopy(route.get("identity") or {})
    route_fp = identity.get("fingerprint") or route_path_fingerprint(path)
    protection_fp = stable_fingerprint(
        {
            "mode": "engineering_scenario_preview",
            "route_geometry_fingerprint": route_fp,
            "half_width_m": width,
            "lower_altitude_m": lower,
            "upper_altitude_m": upper,
            "vertical_reference": "EGM2008",
        },
        prefix="protectionpreviewv1-",
    )
    object_id = f"protection-preview:{protection_fp}"
    return {
        "object_id": object_id,
        "render_instance_id": protection_render_instance_id(
            object_id=object_id,
            instance_kind=INSTANCE_KIND_PREVIEW,
            route_authority=route.get("authority"),
            route_geometry_identity=identity,
            protection_geometry_identity=protection_fp,
        ),
        "mode": "engineering_scenario_preview",
        "status": "engineering_preview",
        "route_id": route.get("route_id"),
        "route_authority": route.get("authority"),
        "route_geometry_identity": identity,
        "protection_geometry_identity": protection_fp,
        "geometry_dimension": "three_dimensional_volume",
        "geometry": geometry,
        "horizontal_half_width_m": width,
        "vertical_reference": "EGM2008",
        "vertical_extent_status": "user_supplied_engineering_preview",
        "lower_altitude_m": lower,
        "upper_altitude_m": upper,
        "evidence_status": "user_supplied_non_authoritative",
        "required_evidence": [],
        "disclaimer": PREVIEW_DISCLAIMER,
        "isolation": {
            "written_to_project_state": False,
            "enters_formal_p17": False,
            "enters_cns_statistics": False,
            "enters_radar_los": False,
            "operational_authorization": False,
        },
        "input_validation": {
            "all_values_explicit": True,
            "numeric_and_finite": True,
            "route_geometry_checked": True,
            "region_checked": True,
            "construction_zone_checked": True,
            "bounds_order_checked": True,
        },
    }
