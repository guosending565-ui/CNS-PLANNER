"""专题图装配与应用服务（Application 层）：canonical state + 真实 GIS → FigureSpec → PNG。

职责边界
========

* :func:`materialize` 只读消费项目 state 与已配置数据源，把业务事实整理成
  :class:`~cns_planner.gis.figure_spec.FigureSpec`（JSON-safe）。
* :class:`MapFigureService` 负责业务编排：航路选择、范围计算、FigureSpec 构建、
  调用注入的渲染器（QGIS 制图）、产物落盘与记录写回。
* :class:`MapFigureStore` 负责受控产物目录下的二进制读写与路径安全；客户端**永远**
  不能传入输出路径。
* 本模块**不创建任何 QGIS 版面对象**（那是 :mod:`cns_planner.gis.qgis_figure_renderer`），
  也**不修改任何 canonical 结果**：没有风险计算、没有航路规划、没有 validation/adoption，
  专题图结果也绝不回写 operational route / CNS planning state。

缺失数据一律如实登记：``unavailable`` / ``unknown`` + 中文原因，**绝不**补 0 或伪造要素。
几何计算（buffer / km / 阈值 / extent margin）在**米制**口径下完成；WGS84 只用于最终地图
显示与经纬网。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re

from ..gis.figure_spec import (
    DEFAULT_CRS, GEOMETRY_LINE, GEOMETRY_NONE, GEOMETRY_POINT, GEOMETRY_POLYGON,
    SOURCE_AVAILABLE, SOURCE_UNKNOWN, SOURCE_UNAVAILABLE, AnnotationSpec, ExtentSpec,
    FigureSpec, LabelSpec, LayerSpec, LegendItem,
)
from ..gis.figure_style import (
    CNS_LABEL_CARD_ORDER, LABEL_STYLES, LEGEND_GROUP_OF, legend_order_key,
)
from ..gis.land_mask_source import build_land_mask_source, land_mask_source_path
from ..gis.source_inspection import inspect_cartographic_land
from ..reporting.map_templates import (
    CNS_COMBINED_V1, CNS_COVERAGE_POLICY, CNS_EXTENT_POLICY_ID, CNS_LAYOUT_PROFILE,
    CNS_LEGEND_ORDER, COMMUNICATION_LAYOUT_V1, NAVIGATION_LAYOUT_V1, ROUTE_OVERVIEW_V1,
    SURVEILLANCE_LAYOUT_V1, SURVEILLANCE_SERVICE_PARAMETER, SURVEILLANCE_SERVICE_RADAR,
    SURVEILLANCE_SERVICE_RID, SURVEILLANCE_SERVICE_VALUES, MapFigureParameterInvalid,
    catalog as template_catalog, is_available, parameters as template_parameters,
    required_parameters as template_required, template as template_definition,
)
from .cns_facility_assembler import (
    IDENTITY_EXISTING, IDENTITY_PROPOSAL, RADAR_LAYOUT_KEY, SERVICE_COMMUNICATION,
    SERVICE_LABEL_SUFFIX, SERVICE_NAVIGATION_INTEGRITY, SERVICE_RADAR, SERVICE_RID,
    assemble as assemble_cns_facilities, coverage_circle_ring, coverage_radius_for,
    rid_radius_pair,
)
from .rid_surface_footprint import build_surface_aware_rid_footprint

#: 权威运行航路的 canonical state 容器。
OPERATIONAL_ROUTE_SOURCE = "operational_routes"
#: 制图模块内部使用的米制 CRS（与项目其它真实几何一致，可被模板参数覆盖）。
DEFAULT_METRIC_CRS = "EPSG:32651"
#: 起点 / 终点名称的最大长度（放长以容纳完整站名；正式图由标签卡自动换行）。
MAX_LABEL_CHARS = 14
#: 起终点名称的硬上限：比普通标注宽松得多，避免"正式站名被截断成省略号"。
MAX_ENDPOINT_LABEL_CHARS = 48
#: 普通地名的硬上限：正式图也由标签卡换行，这里只防极端超长。
MAX_PLACE_LABEL_CHARS = 24
#: 受控产物目录（相对 active project 目录，与既有 reports/ 同级）。
MAP_FIGURE_DIRECTORY = "artifacts/map_figures"
#: **route-first** 归档子目录：``artifacts/map_figures/routes/<route>/<template>/<figure>/``。
#:
#: 同一条航路以后会有多张专题图（route_overview_v1 / route_detail_v1 /
#: communication_layout_v1 / navigation_layout_v1 / surveillance_layout_v1 /
#: cns_combined_v1），按航路归档才便于人工查找与后续"按航路浏览"的界面。
#: 旧的平铺布局 ``artifacts/map_figures/<figure_id>/`` **只读兼容**：已有历史产物
#: 原地保留，不自动搬迁、不删除；新产物一律写入 route-first 结构。
MAP_FIGURE_ROUTES_DIRECTORY = "routes"
#: 图件索引文件名（位于受控目录内）。
#:
#: 专题图记录**不进入 ProjectState**：ProjectState 的 ``revision`` 是业务语义的乐观锁，
#: 而"渲染了一张专题图"不改变任何业务事实。因此记录写在受控目录的索引文件里，
#: ``session.save()`` 在这条链路上一次都不会被调用（业务 revision 因此完全不变）。
MAP_FIGURE_INDEX_NAME = "index.json"
#: 记录里"由产物本身决定"的固定审计字段：metadata.json 与 index.json 必须逐字段一致。
MAP_FIGURE_AUDIT_FIELDS = (
    "figure_id", "template_id", "template_version", "route_id", "format", "dpi",
    "generated_at", "project_revision", "spec_fingerprint", "image_bytes",
    "image_sha256", "relative_path", "spec_relative_path", "record_relative_path",
    "artifact_ref",
)
#: index.json 额外允许存在的**动态投影**字段（不写盘、只读计算）。
MAP_FIGURE_PROJECTION_FIELDS = ("current_applicability",)
#: state 中登记专题图记录的容器键。
#:
#: **只读兼容**：早期版本把记录写进这个容器（并 ``session.save()``，因此会推进 revision）。
#: 现在仍然读取它以便旧项目继续显示历史图件，但**不再写入**。
MAP_FIGURE_COLLECTION = "map_figures"
#: 允许的输出格式（本轮只有 PNG；PDF/SVG 属后续轮次，未实现前明确拒绝）。
SUPPORTED_FORMATS = ("png",)
#: 预览尺寸上限（像素）：真正约束"宽 × 高"的总像素，而不是只约束宽度。
MAX_PREVIEW_PIXELS = 12_000_000
#: 单个专题图渲染的**绝对**总像素上限（宽 × 高）。A4 竖版在 600 dpi 下约 34.8 MP，
#: 再往上没有制图意义，只会让 QGIS 版面渲染的内存/时间失控。所有路径（预览与 300/600
#: dpi 导出）都必须过这一关。
MAX_FIGURE_PIXELS = 40_000_000
#: 导出 DPI 的硬范围（低于 72 不可读，高于 600 无意义且会撞像素上限）。
MIN_EXPORT_DPI = 72.0
MAX_EXPORT_DPI = 600.0
_FIGURE_ID_PATTERN = re.compile(r"^MF-[0-9a-f]{32}$")
#: 模板 id 白名单：route-first 目录的第二层目录名只能来自这个字符集。
_TEMPLATE_ID_PATTERN = re.compile(r"^[0-9a-z_]{1,64}$")
#: 航路 id 里不允许出现在目录名中的字符（其余一律替换为 ``_``）。
_UNSAFE_ROUTE_CHARS = re.compile(r"[^0-9A-Za-z_\-]+")
#: 清洗后可读前缀的最大长度（后面还会拼接一个稳定摘要，保证不碰撞）。
MAX_ROUTE_DIRECTORY_PREFIX = 48


class MapFigureError(Exception):
    """专题图相关的业务错误基类：携带稳定 ``code`` 供 API 层映射状态码。"""

    code = "map_figure_error"
    status = 400

    def __init__(self, message, *, code=None, detail=None):
        super().__init__(message)
        if code:
            self.code = code
        self.detail = detail or {}


class MapFigureRouteError(MapFigureError):
    """航路选择问题：不存在 / 需要显式选择 / 几何不可用。"""

    code = "map_figure_route_unavailable"


class MapFigureRouteSelectionRequired(MapFigureRouteError):
    code = "map_figure_route_selection_required"
    status = 409


class MapFigureTemplateUnavailable(MapFigureError):
    code = "map_figure_template_unavailable"
    status = 409


class MapFigureNotFound(MapFigureError):
    code = "map_figure_not_found"
    status = 404


class MapFigurePathDenied(MapFigureError):
    code = "map_figure_path_denied"
    status = 400


class MapFigureRenderFailed(MapFigureError):
    code = "map_figure_render_failed"
    status = 500


class MapFigureFormatUnsupported(MapFigureError):
    code = "map_figure_format_unsupported"


class MapFigureParameterError(MapFigureError):
    """模板参数非法或必需参数缺失（客户端可修正，因此是 400 而不是 500）。"""

    code = "map_figure_parameter_invalid"
    status = 400


def utc_now():
    return datetime.now(timezone.utc).isoformat()


# =========================================================================== 数据装配

@dataclass
class MaterializationContext:
    """装配所需的只读事实（全部来自组合根，制图模块不自己去读项目文件）。"""

    state: dict
    paths: dict
    route: dict
    extent: ExtentSpec
    parameters: dict
    project_revision: int = 0
    source_audits: dict = field(default_factory=dict)
    surface_classification_policy: dict = field(default_factory=dict)
    #: 当前模板 id（route_overview_v1 / communication_layout_v1 / ...）。
    #: 装配层据此选择模板专属的图层与图例；缺省即历史模板，行为不变。
    template_id: str = ROUTE_OVERVIEW_V1


@dataclass
class MaterializationResult:
    layers: list = field(default_factory=list)
    labels: list = field(default_factory=list)
    legend_items: list = field(default_factory=list)
    source_status: dict = field(default_factory=dict)
    omitted_layers: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    annotations: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    boundaries: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- 工具箱

def _finite(value, default=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _short(text, limit=MAX_LABEL_CHARS):
    value = str(text or "").strip()
    if not value:
        return ""
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _source_audit(ctx, role):
    items = (ctx.source_audits or {}).get("items") or {}
    audit = items.get(role)
    return audit if isinstance(audit, dict) else {}


def _source_state(ctx, role):
    """一个数据源角色的可用性：路径 + 是否存在 + source audit 结论。"""

    path = str((ctx.paths or {}).get(role) or "")
    audit = _source_audit(ctx, role)
    status = str(audit.get("status") or "")
    verified = str((audit.get("verification") or {}).get("status") or "")
    detail = {
        "role": role, "path": path or None,
        "source_audit_status": status or None,
        "verified_status": verified or None,
        "sha256": (audit.get("verification") or {}).get("sha256"),
        "size_bytes": audit.get("size_bytes"),
    }
    if not path:
        return SOURCE_UNKNOWN, "本机未配置该数据源", detail
    if not os.path.isfile(path):
        return SOURCE_UNAVAILABLE, "配置的数据源文件不存在", detail
    if status in ("missing", "failed") or verified in ("missing", "failed"):
        return SOURCE_UNAVAILABLE, "来源审计未通过（文件缺失或校验失败）", detail
    return SOURCE_AVAILABLE, "", detail


def _layer(ctx, layer_key, *, display_name, geometry_type, source_role, status, reason,
           detail=None, feature_count=0, data=None, legend_visible=True):
    return LayerSpec(
        layer_key=layer_key,
        display_name=display_name,
        style_key=layer_key,
        geometry_type=geometry_type,
        source_role=source_role,
        source_path=str((ctx.paths or {}).get(source_role) or "") or None,
        source_status=status,
        source_reason=reason,
        source_detail=detail or {},
        legend_group=LEGEND_GROUP_OF.get(layer_key, ""),
        legend_visible=legend_visible,
        feature_count=int(feature_count or 0),
        data=data or {},
    )


def _legends(layers, order=None):
    """按模板声明的稳定语义顺序生成图例条目（只含真实存在且可用的图层）。

    **声明型图层**（``geometry_type=GEOMETRY_NONE``，例如"非合作监视能力限制"）
    没有几何要素，但必须出现在图例里说明图上没有画什么；它们不画任何假要素。
    """

    items = [
        LegendItem(
            layer_key=layer.layer_key, style_key=layer.style_key,
            display_name=layer.display_name,
            legend_group=LEGEND_GROUP_OF.get(layer.layer_key, ""),
        )
        for layer in layers
        if layer.legend_visible and layer.source_status == SOURCE_AVAILABLE
        and (layer.feature_count > 0 or layer.geometry_type == GEOMETRY_NONE)
    ]
    items.sort(key=lambda item: legend_order_key(item.layer_key, order))
    return items


# --------------------------------------------------------------------------- 范围（米制）

def _expand_extent(route_geometry, *, buffer_km, max_padding_km, aspect, metric_crs):
    """航路几何 bbox + 可配置 buffer → 图面范围（米制口径，按版面长宽比扩展）。

    半径为米的近似在航路平均纬度上用等距圆柱换算；``metric_crs`` 记录本次使用的
    米制参考系，作为可见证据写进 FigureSpec（不改变换算口径）。
    """

    points = []
    for point in route_geometry or []:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        longitude, latitude = _finite(point[0]), _finite(point[1])
        if longitude is None or latitude is None:
            continue
        points.append((longitude, latitude))
    if len(points) < 2:
        return None, {"reason": "route_geometry_insufficient_points"}
    mean_latitude = sum(item[1] for item in points) / len(points)
    metres_per_degree_lat = 111_320.0
    metres_per_degree_lon = 111_320.0 * math.cos(math.radians(mean_latitude))
    xs = [item[0] * metres_per_degree_lon for item in points]
    ys = [item[1] * metres_per_degree_lat for item in points]
    west_m, east_m = min(xs), max(xs)
    south_m, north_m = min(ys), max(ys)
    centre_x, centre_y = (west_m + east_m) / 2.0, (south_m + north_m) / 2.0
    buffer_m = max(0.0, float(buffer_km) * 1000.0)
    half_width = (east_m - west_m) / 2.0 + buffer_m
    half_height = (north_m - south_m) / 2.0 + buffer_m
    ratio = float(aspect) if _finite(aspect, 0.0) > 0 else 1.0
    if half_width / half_height < ratio:
        half_width = half_height * ratio
    else:
        half_height = half_width / ratio
    ceiling_m = buffer_m + float(max_padding_km) * 1000.0
    half_width = min(half_width, ceiling_m)
    half_height = min(half_height, ceiling_m)
    west_deg = (centre_x - half_width) / metres_per_degree_lon
    east_deg = (centre_x + half_width) / metres_per_degree_lon
    south_deg = (centre_y - half_height) / metres_per_degree_lat
    north_deg = (centre_y + half_height) / metres_per_degree_lat
    extent = ExtentSpec(
        west=west_deg, south=south_deg, east=east_deg, north=north_deg,
        width_km=(east_deg - west_deg) * metres_per_degree_lon / 1000.0,
        height_km=(north_deg - south_deg) * metres_per_degree_lat / 1000.0,
    )
    evidence = {
        "mode": "route_bbox_plus_configurable_buffer_metric",
        "route_bbox_metric_m": [west_m, south_m, east_m, north_m],
        "buffer_km": float(buffer_km),
        "max_padding_km": float(max_padding_km),
        "aspect_ratio": ratio,
        "metric_crs": str(metric_crs),
        "conversion": "local_equirectangular_at_route_mean_latitude",
        "metres_per_degree_lon": metres_per_degree_lon,
        "metres_per_degree_lat": metres_per_degree_lat,
        "padding_km": {
            "west": (west_m - west_deg * metres_per_degree_lon) / 1000.0,
            "east": (east_deg * metres_per_degree_lon - east_m) / 1000.0,
            "south": (south_m - south_deg * metres_per_degree_lat) / 1000.0,
            "north": (north_deg * metres_per_degree_lat - north_m) / 1000.0,
        },
    }
    return extent, evidence


def _turn_points(geometry):
    """从权威航路折线派生**仅用于展示**的中间折点（起终点不算转弯点）。"""

    points = []
    for point in geometry or []:
        if isinstance(point, (list, tuple)) and len(point) >= 2:
            longitude, latitude = _finite(point[0]), _finite(point[1])
            if longitude is not None and latitude is not None:
                points.append((longitude, latitude))
    if len(points) <= 2:
        return []
    deduped = [points[0]]
    for point in points[1:]:
        if abs(point[0] - deduped[-1][0]) > 1e-9 or abs(point[1] - deduped[-1][1]) > 1e-9:
            deduped.append(point)
    return deduped[1:-1]


# --------------------------------------------------------------------------- 矢量读取

def _qgis_available():
    """本解释器是否可用 QGIS（纯数据单元测试可在没有 QGIS 时运行）。"""

    import importlib.util

    return importlib.util.find_spec("qgis") is not None


_QGIS_MISSING_REASON = "当前解释器没有 QGIS 运行时，无法读取真实 GIS 数据源"


def _bbox_wkt(viewport):
    return (
        f"POLYGON(({viewport.xMinimum()} {viewport.yMinimum()}, "
        f"{viewport.xMaximum()} {viewport.yMinimum()}, "
        f"{viewport.xMaximum()} {viewport.yMaximum()}, "
        f"{viewport.xMinimum()} {viewport.yMaximum()}, "
        f"{viewport.xMinimum()} {viewport.yMinimum()}))"
    )


def _supports_subset(layer):
    """GPKG/OGR 的 SQL subset 在 OGR SQLite 方言里**没有** ``geom_from_wkt`` 等函数。

    实测在真实 GeoPackage 上使用 ``intersects(geom, geom_from_wkt(...))`` 会导致
    ``Failed to prepare SQL``，图层直接读不出来（陆地/空域会整层消失）。因此这里统一
    返回 ``False``：范围过滤改由逐要素 bbox 判断完成，绝不用不可用的 SQL 方言。
    """

    return False


def _geometry_to_wgs84_polygons(geometry, layer):
    """QGIS 几何（任意 CRS）→ WGS84 **结构化多边形**列表。

    真实结构逐层保留，绝不平坦化：

        [{"exterior": [[lon, lat], ...], "holes": [[[lon, lat], ...], ...]}, ...]

    * MultiPolygon 的每个 part 都是一个**独立多边形**（不合并、不丢 part）；
    * Polygon 的第一个环是外环，其余是**内环（洞）**；内环绝不作为独立陆地多边形
      出现在结果里 —— 历史 bug 正是把 exterior 与 holes 拍平成多个平级 ring，
      于是湖面 / 内湾被当成"独立陆地"填上陆色。

    本函数是制图几何链的**唯一**入口：source reader → materialize → FigureSpec →
    renderer 全程传递同一个结构，洞只在渲染器里被重建为 QGIS 内环。
    """

    if geometry is None or geometry.isEmpty():
        return []
    source_crs = layer.crs()
    if source_crs is not None and source_crs.isValid() and source_crs.authid() != "EPSG:4326":
        from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject

        transform = QgsCoordinateTransform(
            source_crs, QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance(),
        )
        geometry = _transformed(geometry, transform)
        if geometry is None or geometry.isEmpty():
            return []
    parts = geometry.asMultiPolygon() if geometry.isMultipart() else [geometry.asPolygon()]
    polygons = []
    for part in parts:
        rings = [ring for ring in (part or []) if ring]
        if not rings:
            continue
        exterior = _ring_points(rings[0])
        if len(exterior) < 4:
            continue
        holes = []
        for ring in rings[1:]:
            hole = _ring_points(ring)
            if len(hole) >= 4:
                holes.append(hole)
        polygons.append({"exterior": exterior, "holes": holes})
    return polygons


def _ring_points(ring):
    return [[float(point.x()), float(point.y())] for point in ring]


def _polygon_rings(polygons):
    """结构化多边形 → 平级外环列表（只用于确实不区分内环的场合）。

    **不要**用它构建陆地/海域：那会把内环丢掉。它只服务于"以区域集合表达、不需要
    洞语义"的图层（如空域格网），并且调用点必须明说这一点。
    """

    return [item["exterior"] for item in polygons or [] if item.get("exterior")]


def _transformed(geometry, transform):
    from qgis.core import QgsGeometry

    clone = QgsGeometry(geometry)
    clone.transform(transform)
    return clone


def _read_polygons(path, layer_name, viewport, *, max_features=4000):
    """按视图范围读取 Polygon/MultiPolygon → **结构化**多边形列表。

    返回 ``([{"exterior": [...], "holes": [[...], ...]}, ...], reason)``：
    外环与内环（洞）从数据源开始就被分开保存，MultiPolygon 的 part 也各自独立。
    """

    from qgis.core import QgsVectorLayer

    if not path:
        return [], "数据源路径缺失"
    source = f"{path}|layername={layer_name}" if layer_name else str(path)
    layer = QgsVectorLayer(source, "figure_read", "ogr")
    if not layer.isValid():
        return [], "图层无法打开（格式或图层名不匹配）"
    if int(layer.geometryType()) != 2:  # 2 == PolygonGeometry
        return [], "图层不是面几何，无法作为区域绘制"
    if _supports_subset(layer):
        layer.setSubsetString(f"intersects(geom, geom_from_wkt('{_bbox_wkt(viewport)}'))")
    polygons = []
    for feature in layer.getFeatures():
        geometry = feature.geometry()
        if geometry is None or geometry.isEmpty():
            continue
        if not geometry.boundingBox().intersects(viewport):
            continue
        polygons.extend(_geometry_to_wgs84_polygons(geometry, layer))
        if len(polygons) >= max_features:
            break
    if not polygons:
        return [], "视图范围内没有面要素"
    return polygons, ""


def _land_polygons(ctx, viewport):
    """canonical ``land_mask`` 的多边形（**业务**陆域掩膜，供其它模板复用）。

    ``route_overview_v1`` 的地图表达**不再**使用它（见
    :func:`_cartographic_land_polygons`）：这份数据是省域行政边界，在舟山群岛
    （尤其嵊泗列岛）局部残缺，直接当成陆地底图会制造"站址位于海上"的假象。
    它的**业务语义完全不变**：surface classification 仍然只消费它。

    返回结构化多边形（``exterior`` + ``holes``），与制图陆地面同一条几何链。
    """

    from ..gis.qgis_project_layers import resolve_vector_layer_source

    path = (ctx.paths or {}).get("land_mask")
    if not path:
        return [], "未配置 land_mask"
    resolved = resolve_vector_layer_source(path, role="land_mask")
    if not resolved.get("ok"):
        return [], resolved.get("reason") or "陆域掩膜无法解析成可读取的图层"
    return _read_polygons(resolved.get("path"), resolved.get("layer_name"), viewport)


#: 制图陆地面数据源的元数据文件名（与数据文件同目录，由派生工具写出）。
CARTOGRAPHIC_LAND_METADATA_SUFFIX = ".json"


def _cartographic_land_metadata(path):
    """读取制图陆地面的派生元数据（**只读**；缺失时返回空字典，绝不猜）。"""

    text = str(path or "")
    if not text:
        return {}
    candidate = Path(text)
    if candidate.suffix.lower() == ".json":
        return {}
    metadata_path = candidate.with_suffix(CARTOGRAPHIC_LAND_METADATA_SUFFIX)
    if not metadata_path.is_file():
        return {}
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


#: 制图陆地面派生元数据的**字段契约**（``cartographic_land_v1.json``）。
#:
#: 每一项都必须是数据源可核验的派生事实；缺失时写入 ``None`` 而不是编造值。
#: ``coverage_reason`` 只能来自数据源自己的记录，**不得**在图代码里硬编码结论。
CARTOGRAPHIC_LAND_METADATA_FIELDS = (
    "role", "semantics", "not_replacing", "derivation",
    "source_dem", "source_dem_origin", "crosscheck_dem", "crosscheck_dem_origin",
    "source_buildings", "source_buildings_origin",
    "dem_threshold_m", "morphology_structure", "closing_iterations",
    "building_dilation_px", "building_raster_resolution_deg",
    "polygonize_connectivity", "min_polygon_pixels", "hole_policy", "simplify_deg",
    "decimation", "grid", "bbox", "pixel_size_deg", "land_pixel_ratio",
    "polygon_count", "output_geojson", "output_gpkg", "crs",
    "coverage_reason", "validation",
)


def _cartographic_land_metadata_contract(metadata):
    """按字段契约投影派生元数据：**只**取数据源真实写出的字段，缺失即 ``None``。"""

    recorded = metadata if isinstance(metadata, dict) else {}
    return {key: deepcopy(recorded.get(key)) for key in CARTOGRAPHIC_LAND_METADATA_FIELDS}


def _cartographic_land_state(ctx):
    """制图专用的陆海表达数据源状态（**不是**业务 land_mask）。

    可用性的判定是**严格**的（code review 修复项）：文件存在还不够，必须

        ``inspection["status"] == "passed"`` **且** ``inspection["is_polygon"] is True``

    才允许报告 ``SOURCE_AVAILABLE``。"文件在、但读不出来 / 不是矢量 / 不是面 / CRS 或
    图层异常"一律是 ``SOURCE_UNAVAILABLE`` + 自省给出的中文原因，绝不降格成
    "当前范围内没有面要素"（后者会让"数据坏了"看起来像"这里本来就没有陆地"）。
    """

    base = {
        "role": "cartographic_land",
        "path": None,
        "metadata_contract": list(CARTOGRAPHIC_LAND_METADATA_FIELDS),
        "metadata_recorded": False,
        "semantics": "cartographic_presentation_only_not_surface_classification",
        "not_replacing": "canonical land_mask (business surface classification unchanged)",
        "is_business_surface_evidence": False,
        "availability_rule": "inspection.status == passed AND inspection.is_polygon is True",
    }
    path = str((ctx.paths or {}).get("cartographic_land") or "")
    if not path:
        return SOURCE_UNKNOWN, "本机未配置制图陆地面（cartographic_land）", base
    if not os.path.isfile(path):
        return SOURCE_UNAVAILABLE, "配置的制图陆地面文件不存在", {**base, "path": path}

    metadata = _cartographic_land_metadata(path)
    contract = _cartographic_land_metadata_contract(metadata)
    # 矢量数据集的客观事实（图层名 / 几何类型 / CRS / 要素数 / 范围）由**独立**的矢量
    # 自省提供，绝不套用 buildings 的 GeoPackage schema（见 gis/source_inspection）。
    inspection = inspect_cartographic_land(path)
    detail = {
        **base,
        **contract,
        "path": path,
        "metadata_recorded": bool(metadata),
        "inspection": inspection,
        # 元数据里的 CRS 只是声明；以自省读到的图层 CRS 为准（读不到才回退声明）。
        "crs": (inspection or {}).get("crs") or contract.get("crs"),
    }
    detail["semantics"] = str(
        contract.get("semantics")
        or "cartographic_presentation_only_not_surface_classification"
    )
    detail["not_replacing"] = str(
        contract.get("not_replacing")
        or "canonical land_mask (business surface classification unchanged)"
    )
    passed = str((inspection or {}).get("status") or "") == "passed"
    is_polygon = (inspection or {}).get("is_polygon") is True
    if not passed or not is_polygon:
        reason = str((inspection or {}).get("reason") or "").strip() or (
            "制图陆地面自省未通过（必须是通过自省的面矢量数据集："
            f"status={((inspection or {}).get('status'))}，"
            f"is_polygon={((inspection or {}).get('is_polygon'))}）"
        )
        detail["unavailable_reason"] = reason
        return SOURCE_UNAVAILABLE, reason, detail
    return SOURCE_AVAILABLE, "", detail


def _read_geojson_polygons(path, viewport, *, max_features=4000):
    """读取制图陆地面（GeoPackage / Shapefile / GeoJSON）→ 视图范围内的**结构化**多边形。

    读取沿用与建筑、陆域掩膜相同的 OGR 路径（逐要素 bbox 过滤），因此覆盖舟山全域的
    制图陆地面（637 个多边形）不会把整份几何塞进 FigureSpec。每个多边形都保留自己的
    外环与内环（洞），MultiPolygon 的 part 也彼此独立。
    """

    from ..gis.qgis_project_layers import resolve_vector_layer_source

    resolved = resolve_vector_layer_source(path, role="cartographic_land")
    if not resolved.get("ok"):
        return [], resolved.get("reason") or "制图陆地面无法解析成可读取的图层"
    return _read_polygons(resolved.get("path"), resolved.get("layer_name"),
                          viewport, max_features=max_features)


def _cartographic_land_polygons(ctx, viewport):
    """制图陆地面（``cartographic_land``）→ 视图范围内的结构化多边形。"""

    path = (ctx.paths or {}).get("cartographic_land")
    if not path:
        return [], "未配置制图陆地面"
    return _read_geojson_polygons(path, viewport)


def _holes_of_polygons(polygons):
    """结构化多边形列表 → ``{外环下标: [hole, ...]}``（渲染器的内环契约）。"""

    result = {}
    for index, item in enumerate(polygons or []):
        holes = [hole for hole in (item.get("holes") or []) if len(hole) >= 4]
        if holes:
            result[str(index)] = holes
    return result


def _sea_complement_polygons(extent, land_polygons):
    """海域 = **地图画布矩形 − cartographic_land**（真正的多边形面积差集）。

    实现方式：用可靠的多边形差集库（shapely，QGIS 环境自带）做
    ``canvas_rectangle.difference(land_union)``，并把结果的 exterior **与内环（holes）
    一并保留**。陆地多边形带洞时，洞会被原样保留为独立的海域环，而不是被填实。

    输入是**结构化**多边形（``{"exterior": [...], "holes": [[...], ...]}``，见
    :func:`_geometry_to_wgs84_polygons`）：陆地自己的内环会参与差集，因此"岛中湖"不会被
    当成陆地面积填掉。为兼容只给外环的历史调用点，裸环列表也被接受（此时视为无洞）。

    为什么不再自己实现扫描线：自制扫描线/网格采样只是为了绕开"没依赖多边形库"，
    但它会把海域近似成矩形条带（面积与拓扑都不精确，也无法表达内环）。差集是
    几何精确运算，且直接给出正确的 exterior + holes。

    找不到多边形库时**如实失败**（返回不可用理由），绝不用近似几何冒充面积差集。
    """

    if not land_polygons:
        return [], [], 0.0, "制图陆地面在范围内没有可用于差集的环"
    try:
        import shapely
        from shapely.geometry import Polygon
        from shapely.ops import unary_union
    except ImportError:
        return [], [], 0.0, (
            "缺少多边形几何库（shapely），无法计算"
            "「画布矩形 − 制图陆地面」的真实面积差集；本图不表达海域"
        )

    canvas = Polygon([
        (extent.west, extent.south), (extent.east, extent.south),
        (extent.east, extent.north), (extent.west, extent.north),
        (extent.west, extent.south),
    ])
    shapes = []
    for item in land_polygons:
        candidate = _land_shape(item, Polygon)
        if candidate is None:
            continue
        if not candidate.is_valid:
            # 自相交/重复点等：修复一次；仍无效则跳过该环（不参与差集，也不伪造）。
            candidate = shapely.make_valid(candidate)
            if candidate.is_empty:
                continue
            candidates = (
                list(candidate.geoms) if candidate.geom_type == "MultiPolygon"
                else [candidate]
            )
            shapes.extend(
                entry for entry in candidates
                if entry.geom_type == "Polygon" and not entry.is_empty
            )
            continue
        shapes.append(candidate)

    if not shapes:
        return [], [], 0.0, "制图陆地面的环都无法构成有效多边形"
    land_union = unary_union(shapes)
    complement = canvas.difference(land_union)
    if complement.is_empty:
        return [], [], 1.0, ""

    parts = list(complement.geoms) if complement.geom_type == "MultiPolygon" else [complement]
    parts = sorted(
        (item for item in parts if item.geom_type == "Polygon" and not item.is_empty),
        key=lambda item: -item.area,
    )
    total_deg2 = canvas.area
    sea_deg2 = 0.0
    records = []
    exteriors = []
    for part in parts:
        sea_deg2 += part.area
        exterior = [[float(x), float(y)] for x, y in part.exterior.coords]
        holes = [
            [[float(x), float(y)] for x, y in interior.coords]
            for interior in part.interiors
        ]
        exteriors.append(exterior)
        if holes:
            records.append({"exterior": exterior, "holes": holes})
    sea_ratio = (sea_deg2 / total_deg2) if total_deg2 else 0.0
    return exteriors, records, sea_ratio, ""


def _land_shape(item, polygon_class):
    """结构化多边形（或裸外环）→ shapely ``Polygon``；不可用时返回 ``None``。

    内环（``holes``）在构造时就进入 shapely Polygon，因此它参与后续的面积差集 ——
    这是"洞不被填实"的**唯一**正确做法（只在渲染末尾补洞无法修正面积与拓扑）。
    """

    if isinstance(item, dict):
        exterior_ring = item.get("exterior")
        hole_rings = item.get("holes") or []
    else:
        exterior_ring, hole_rings = item, []
    if not exterior_ring or len(exterior_ring) < 4:
        return None
    try:
        shell = [(float(point[0]), float(point[1])) for point in exterior_ring]
        holes = [
            [(float(point[0]), float(point[1])) for point in hole]
            for hole in hole_rings if hole and len(hole) >= 4
        ]
        return polygon_class(shell, holes)
    except (TypeError, ValueError):
        return None


def _land_attributes(ctx):
    """陆域图层的分级名称（只取真实字段值），用于地名标注候选。"""

    from ..gis.qgis_project_layers import resolve_vector_layer_source

    path = (ctx.paths or {}).get("land_mask")
    if not path:
        return {}
    resolved = resolve_vector_layer_source(path, role="land_mask")
    if not resolved.get("ok"):
        return {}
    from qgis.core import QgsVectorLayer

    source = str(resolved.get("path") or "")
    layer_name = resolved.get("layer_name")
    layer = QgsVectorLayer(
        f"{source}|layername={layer_name}" if layer_name else source, "land_probe", "ogr",
    )
    if not layer.isValid():
        return {}
    result = {}
    for feature in layer.getFeatures():
        geometry = feature.geometry()
        if geometry is None or geometry.isEmpty():
            continue
        point = geometry.pointOnSurface().asPoint()
        for field in layer.fields():
            value = feature[field.name()]
            if value in (None, ""):
                continue
            result.setdefault(str(value), [float(point.x()), float(point.y())])
        if len(result) >= 12:
            break
    return result


def _airspace_polygons(ctx, viewport):
    """适飞空域：从已配置的 QGIS 工程中挑出本地面图层并按视图范围过滤。

    返回**结构化**多边形（外环 + 内环）以及命中的图层名，绝不把内环拍平成独立面。
    """

    from ..gis.qgis_project_layers import read_project_layers

    path = (ctx.paths or {}).get("basemap")
    if not path:
        return [], None, "未配置适飞空域工程"
    try:
        inventory = read_project_layers(path)
    except (OSError, ValueError) as exc:
        return [], None, f"适飞空域工程无法解析：{exc}"
    candidates = [
        layer for layer in inventory["layers"]
        if layer.get("is_local_vector_file") and layer.get("is_polygon") and layer.get("exists")
    ]
    if not candidates:
        return [], None, "工程中没有本地可读取的面状矢量图层"
    polygons, names = [], []
    for layer in candidates:
        rings, _ = _read_polygons(layer.get("path"), layer.get("layer_name"), viewport,
                                  max_features=2000)
        names.append(layer.get("name") or layer.get("layer_name") or "")
        polygons.extend(rings)
        if len(polygons) >= 4000:
            break
    if not polygons:
        return [], ", ".join(sorted({name for name in names if name})[:6]) or None, \
            "工程面图层在当前范围内没有要素"
    return polygons, ", ".join(sorted({name for name in names if name})[:6]), ""


# --------------------------------------------------------------------------- 陆域 / 空域图层

def _polygon_layers(ctx, extent):
    """海域 / 陆地区域（``route_overview_v1`` 的地理底图）。

    海域 = **地图画布矩形 − ``cartographic_land``**；陆地 = ``cartographic_land`` 本体。
    两者都来自**制图专用**数据源，与业务 ``land_mask`` 完全分离：

    * ``cartographic_land`` 只负责这张图上的陆海表达，不参与 surface classification、
      风险计算或航路规划，也不会写回任何 canonical 结果；
    * 业务 ``land_mask``（surface classification 的判据）语义与取值一律不变。

    制图陆地面缺失时**如实报告不可用**（不静默回退到省域边界、不伪造海岸线）；
    数据源可用但当前解释器没有 QGIS 时，原因写"缺少 QGIS 运行时"而不是"未配置数据源"。
    """

    if not _qgis_available():
        status, reason, detail = _cartographic_land_state(ctx)
        land_reason = reason or _QGIS_MISSING_REASON
        sea_detail = {
            **detail,
            "derivation": "canvas_rectangle_minus_cartographic_land",
            "geometry_method": "polygon_difference_with_holes",
            "coverage_reason": _coverage_reason(detail),
            "is_business_surface_evidence": False,
        }
        return [
            _layer(ctx, key, display_name=name, geometry_type=GEOMETRY_POLYGON,
                   source_role="cartographic_land",
                   status=status if status != SOURCE_AVAILABLE else SOURCE_UNKNOWN,
                   reason=land_reason, detail=sea_detail, feature_count=0)
            for key, name in (("sea", "海域"), ("land", "陆地区域"))
        ]
    from qgis.core import QgsRectangle

    viewport = QgsRectangle(extent.west, extent.south, extent.east, extent.north)
    status, reason, detail = _cartographic_land_state(ctx)
    land_polygons, land_reason = ([], reason) if status != SOURCE_AVAILABLE \
        else _cartographic_land_polygons(ctx, viewport)
    # 陆地自己的内环（洞）从**数据源**一路带到这里，既不丢也不拍平成独立陆地。
    land_exteriors = _polygon_rings(land_polygons)
    land_holes = _holes_of_polygons(land_polygons)
    # 覆盖结论只能来自数据源的派生记录，**不得**在图代码里硬编码"覆盖全部岛群"这类结论。
    coverage_reason = _coverage_reason(detail)

    sea_detail = deepcopy(detail)
    sea_detail.update({
        "derivation": "canvas_rectangle_minus_cartographic_land",
        "geometry_method": "polygon_difference_with_holes",
        "coverage_reason": coverage_reason,
        "semantics": (
            "海域由地图画布矩形与制图陆地面的多边形差集表达（真实面积差，不是叠色、"
            "也不是矩形条带近似）；陆地多边形带洞时洞按原样保留。陆地数据缺失处"
            "不会被视为海面 —— 此时海域图层整体标记为不可用"
        ),
        "is_business_surface_evidence": False,
    })
    land_detail = deepcopy(detail)
    land_detail.update({
        "coverage_reason": coverage_reason,
        "feature_count": len(land_exteriors),
        "hole_ring_count": sum(len(holes) for holes in land_holes.values()),
        "geometry_structure": "exterior_with_interior_rings_from_source",
    })

    layers = []
    if status == SOURCE_AVAILABLE and land_exteriors:
        sea_exteriors, sea_records, sea_ratio, sea_reason = _sea_complement_polygons(
            extent, land_polygons,
        )
        if sea_exteriors:
            holes_in_sea = sum(len(item.get("holes") or []) for item in sea_records)
            layers.append(_layer(
                ctx, "sea", display_name="海域", geometry_type=GEOMETRY_POLYGON,
                source_role="cartographic_land", status=SOURCE_AVAILABLE, reason="",
                detail={**sea_detail, "polygon_count": len(sea_exteriors),
                        "hole_count": holes_in_sea,
                        "sea_area_ratio": round(sea_ratio, 6)},
                feature_count=len(sea_exteriors),
                data={"polygons": sea_exteriors,
                      "polygon_holes": _holes_by_exterior(sea_records, sea_exteriors),
                      "derivation": "canvas_rectangle_minus_cartographic_land",
                      "land_polygon_count": len(land_exteriors)},
            ))
        else:
            layers.append(_layer(
                ctx, "sea", display_name="海域", geometry_type=GEOMETRY_POLYGON,
                source_role="cartographic_land", status=SOURCE_UNAVAILABLE,
                reason=sea_reason or "图面范围内没有可表达的海域",
                detail=sea_detail, feature_count=0,
            ))
        layers.append(_layer(
            ctx, "land", display_name="陆地区域", geometry_type=GEOMETRY_POLYGON,
            source_role="cartographic_land", status=SOURCE_AVAILABLE, reason="",
            detail=land_detail, feature_count=len(land_exteriors),
            data={"polygons": land_exteriors,
                  "polygon_holes": land_holes,
                  "feature_attributes": {}},
        ))
    else:
        layers.append(_layer(
            ctx, "sea", display_name="海域", geometry_type=GEOMETRY_POLYGON,
            source_role="cartographic_land", status=status,
            reason=land_reason or "缺少制图陆地面，无法表达海域",
            detail=sea_detail, feature_count=0,
        ))
        layers.append(_layer(
            ctx, "land", display_name="陆地区域", geometry_type=GEOMETRY_POLYGON,
            source_role="cartographic_land", status=status,
            reason=land_reason or "缺少制图陆地面，无法表达陆地",
            detail=land_detail, feature_count=0,
        ))
    return layers


def _holes_by_exterior(records, exteriors):
    """把带洞记录整理成 ``{exterior_index: [hole, ...]}``（渲染器据此重建内环）。"""

    if not records:
        return {}
    lookup = {}
    for item in records:
        exterior = item.get("exterior")
        if not exterior:
            continue
        lookup[_ring_key(exterior)] = item.get("holes") or []
    result = {}
    for index, exterior in enumerate(exteriors):
        holes = lookup.get(_ring_key(exterior))
        if holes:
            result[str(index)] = holes
    return result


def _ring_key(ring):
    if not ring:
        return ""
    first, last = ring[0], ring[-1]
    return "%.7f,%.7f|%.7f,%.7f|%d" % (
        float(first[0]), float(first[1]), float(last[0]), float(last[1]), len(ring),
    )


def _coverage_reason(detail):
    """陆地覆盖说明：只引用**数据源自己记录**的派生事实，绝不硬编码结论。

    数据源的 ``cartographic_land_v1.json`` 若给了 ``coverage_reason`` 就原样使用；
    否则只叙述记录下来的校验事实（校验点命中数 / 建筑与站址落陆率），既不声称
    "覆盖全部岛群"，也不声称"不覆盖"。完全没有记录时如实返回"未记录"。
    """

    recorded = str((detail or {}).get("coverage_reason") or "").strip()
    if recorded:
        return recorded
    validation = (detail or {}).get("validation") or {}
    if not isinstance(validation, dict) or not validation:
        return "数据源未记录覆盖校验结论（无法据此声称覆盖或不覆盖任何区域）"
    probes = validation.get("probes") if isinstance(validation.get("probes"), dict) else {}
    passed = sum(1 for value in probes.values() if value is True)
    parts = []
    if probes:
        parts.append(f"陆地覆盖探针 {passed}/{len(probes)} 命中（数据源派生时记录）")
    for label, ratio_key, inside_key, total_key in (
        ("建筑", "buildings_inside_ratio", "buildings_inside", "buildings_total"),
        ("通信站址", None, "towers_inside", "towers_total"),
    ):
        ratio = validation.get(ratio_key) if ratio_key else None
        # 注意：bool 是 int 的子类，绝不能把 True 当成 1 个计数。
        if (isinstance(ratio, (int, float)) and not isinstance(ratio, bool)
                and math.isfinite(float(ratio))):
            parts.append(f"{label}落陆率 {float(ratio) * 100:.2f}%")
        inside = validation.get(inside_key)
        total = validation.get(total_key)
        if (isinstance(inside, int) and not isinstance(inside, bool)
                and isinstance(total, int) and not isinstance(total, bool)):
            parts.append(f"{label}落陆 {inside}/{total}")
    if not parts:
        return "数据源未记录覆盖校验结论（无法据此声称覆盖或不覆盖任何区域）"
    return "；".join(parts) + "（由数据源派生记录提供，非本图结论）"


def _airspace_layer(ctx, extent):
    """适飞空域图层（供**其它模板**按需复用；route_overview_v1 不再调用）。"""

    if not _qgis_available():
        return _layer(
            ctx, "airspace", display_name="已确认适飞空域", geometry_type=GEOMETRY_POLYGON,
            source_role="basemap", status=SOURCE_UNKNOWN, reason=_QGIS_MISSING_REASON,
            feature_count=0,
        )
    from qgis.core import QgsRectangle

    viewport = QgsRectangle(extent.west, extent.south, extent.east, extent.north)
    status, reason, detail = _source_state(ctx, "basemap")
    if status != SOURCE_AVAILABLE:
        return _layer(
            ctx, "airspace", display_name="已确认适飞空域", geometry_type=GEOMETRY_POLYGON,
            source_role="basemap", status=status,
            reason=reason or "未配置适飞空域来源工程", detail=detail, feature_count=0,
        )
    polygons, layer_names, polygon_reason = _airspace_polygons(ctx, viewport)
    detail = {**detail, "layer_name": layer_names}
    if not polygons:
        return _layer(
            ctx, "airspace", display_name="已确认适飞空域", geometry_type=GEOMETRY_POLYGON,
            source_role="basemap", status=SOURCE_UNAVAILABLE,
            reason=polygon_reason or "工程中没有可用的适飞空域图层",
            detail=detail, feature_count=0,
        )
    return _layer(
        ctx, "airspace", display_name="已确认适飞空域", geometry_type=GEOMETRY_POLYGON,
        source_role="basemap", status=SOURCE_AVAILABLE, reason="", detail=detail,
        feature_count=len(polygons),
        data={"polygons": _polygon_rings(polygons),
              "polygon_holes": _holes_of_polygons(polygons),
              "layer_name": layer_names,
              "semantics": "configured_airspace_grid_cells_from_qgis_project"},
    )


# --------------------------------------------------------------------------- 障碍物图层

def _raster_crs_authority(dataset):
    from osgeo import osr

    projection = dataset.GetProjection()
    if not projection:
        return None
    spatial = osr.SpatialReference()
    try:
        spatial.ImportFromWkt(projection)
        spatial.AutoIdentifyEPSG()
        authority = spatial.GetAuthorityCode(None)
    except (RuntimeError, ValueError):
        return None
    return f"EPSG:{authority}" if authority else None


def _world_to_pixel(transform, x, y):
    origin_x, pixel_w, rot_x, origin_y, rot_y, pixel_h = transform
    if rot_x or rot_y:
        determinant = pixel_w * pixel_h - rot_x * rot_y
        if abs(determinant) < 1e-12:
            return 0.0, 0.0
        dx, dy = x - origin_x, y - origin_y
        return ((pixel_h * dx - rot_x * dy) / determinant,
                (-rot_y * dx + pixel_w * dy) / determinant)
    return (x - origin_x) / pixel_w, (y - origin_y) / pixel_h


def _pixel_to_world(transform, col, row):
    origin_x, pixel_w, rot_x, origin_y, rot_y, pixel_h = transform
    return (origin_x + col * pixel_w + row * rot_x,
            origin_y + col * rot_y + row * pixel_h)


def _raster_window(transform, extent, width, height, authority):
    """WGS84 范围 → 栅格像素窗口 ``(x0, y0, x1, y1)``（含 1 像素余量）。"""

    if authority and authority != "EPSG:4326":
        from pyproj import CRS, Transformer

        try:
            transformer = Transformer.from_crs(
                CRS.from_epsg(4326), CRS.from_user_input(authority), always_xy=True,
            )
            x_min, y_min = transformer.transform(extent.west, extent.south)
            x_max, y_max = transformer.transform(extent.east, extent.north)
        except (ValueError, RuntimeError):
            return None
    else:
        x_min, y_min = extent.west, extent.south
        x_max, y_max = extent.east, extent.north
    col_a, row_a = _world_to_pixel(transform, x_min, y_max)
    col_b, row_b = _world_to_pixel(transform, x_max, y_min)
    x0 = max(0, int(math.floor(min(col_a, col_b))) - 1)
    x1 = min(width, int(math.ceil(max(col_a, col_b))) + 2)
    y0 = max(0, int(math.floor(min(row_a, row_b))) - 1)
    y1 = min(height, int(math.ceil(max(row_a, row_b))) + 2)
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None
    return x0, y0, x1, y1


def _pixel_run_box(transform, start_col, start_row, end_col, end_row, shapely_box):
    left, top = _pixel_to_world(transform, start_col, start_row)
    right, bottom = _pixel_to_world(transform, end_col, end_row)
    return shapely_box(min(left, right), min(top, bottom), max(left, right), max(top, bottom))


def _component_to_polygon(sub, slices, transform, x0, y0, shapely_box, unary_union):
    """把一个二值连通区域（像素块）按逐行 run-length 转成多边形并集。"""

    row_offset, col_offset = slices[0].start, slices[1].start
    geometries = []
    rows, cols = sub.shape
    for row in range(rows):
        run_start = None
        for col in range(cols + 1):
            inside = col < cols and bool(sub[row, col])
            if inside and run_start is None:
                run_start = col
            elif not inside and run_start is not None:
                geometries.append(_pixel_run_box(
                    transform, x0 + col_offset + run_start, y0 + row_offset + row,
                    x0 + col_offset + col, y0 + row_offset + row + 1, shapely_box,
                ))
                run_start = None
    if not geometries:
        return None
    return unary_union(geometries)


def _shapely_to_polygons_wgs84(geometry, authority):
    """源 CRS 多边形 → WGS84 **结构化**多边形（只做坐标换算，不做几何简化）。

    与 :func:`_geometry_to_wgs84_polygons` 同一契约：外环与内环分开保留，
    MultiPolygon 的每个 part 各自独立。
    """

    from pyproj import CRS, Transformer

    if getattr(geometry, "geom_type", "") == "Polygon":
        parts = [geometry]
    else:
        parts = list(getattr(geometry, "geoms", []) or [])
    if not parts:
        return []
    transformer = None
    if authority and authority != "EPSG:4326":
        transformer = Transformer.from_crs(
            CRS.from_user_input(authority), CRS.from_epsg(4326), always_xy=True,
        )

    def convert(ring):
        converted = []
        for x, y in ring:
            if transformer is not None:
                longitude, latitude = transformer.transform(float(x), float(y))
            else:
                longitude, latitude = float(x), float(y)
            if math.isfinite(longitude) and math.isfinite(latitude):
                converted.append([float(longitude), float(latitude)])
        return converted if len(converted) >= 4 else None

    polygons = []
    for part in parts:
        exterior = getattr(part, "exterior", None)
        if exterior is None:
            continue
        shell = convert(list(exterior.coords))
        if shell is None:
            continue
        holes = []
        for interior in getattr(part, "interiors", None) or []:
            hole = convert(list(interior.coords))
            if hole is not None:
                holes.append(hole)
        polygons.append({"exterior": shell, "holes": holes})
    return polygons


def _raster_threshold_polygons(path, extent, threshold_m):
    """DEM → 阈值连通区域多边形（纯 GDAL + shapely，不做任何业务判定）。"""

    try:
        import numpy as np
        from osgeo import gdal
        from scipy import ndimage
        from shapely.geometry import box as shapely_box
        from shapely.ops import unary_union
    except ImportError as exc:  # pragma: no cover - QGIS 环境已自带
        return [], {}, f"缺少栅格读取依赖：{exc}"
    if not path:
        return [], {}, "缺少地形数据源路径"
    dataset = gdal.Open(path, gdal.GA_ReadOnly)
    if dataset is None:
        return [], {}, "地形栅格无法打开"
    try:
        transform = dataset.GetGeoTransform()
        authority = _raster_crs_authority(dataset)
        band = dataset.GetRasterBand(1)
        width, height = dataset.RasterXSize, dataset.RasterYSize
        window = _raster_window(transform, extent, width, height, authority)
        if window is None:
            return [], {"raster_crs": authority, "window_px": [0, 0]}, None
        x0, y0, x1, y1 = window
        data = band.ReadAsArray(x0, y0, x1 - x0, y1 - y0)
        if data is None:
            return [], {}, "地形栅格窗口读取失败"
        array = np.asarray(data, dtype="float64")
        nodata = band.GetNoDataValue()
        valid = np.isfinite(array)
        if nodata is not None:
            valid &= array != float(nodata)
        mask = valid & (array >= float(threshold_m))
        labeled, count = ndimage.label(mask, structure=np.ones((3, 3), dtype="int"))
        stats = {
            "window_px": [int(x1 - x0), int(y1 - y0)],
            "threshold_m": float(threshold_m),
            "raster_crs": authority,
            "pixel_size_source_units": abs(float(transform[1])),
            "nodata_value": None if nodata is None else float(nodata),
            "vertical_reference": "source_declared",
            "semantics": "display_only_figure_threshold_derived_from_dem",
        }
        if count == 0:
            stats["component_count"] = 0
            stats["max_elevation_m"] = float(np.nanmax(array[valid])) if valid.any() else None
            return [], stats, None
        min_pixels = 4  # 去掉单像素噪点，避免版面出现无意义碎点。
        geometries = []
        for label_id, slices in enumerate(ndimage.find_objects(labeled), start=1):
            if slices is None:
                continue
            sub = labeled[slices] == label_id
            if int(sub.sum()) < min_pixels:
                continue
            geometry = _component_to_polygon(
                sub, slices, transform, x0, y0, shapely_box, unary_union,
            )
            if geometry is not None and not geometry.is_empty:
                geometries.append(geometry)
        stats["component_count"] = int(len(geometries))
        stats["max_elevation_m"] = (
            float(np.nanmax(array[valid & (array >= float(threshold_m))])) if mask.any() else None
        )
        if not geometries:
            stats["note"] = "所有连通区域都小于最小显示像素数"
            return [], stats, None
        return _shapely_to_polygons_wgs84(unary_union(geometries), authority), stats, None
    finally:
        dataset = None


def _terrain_obstacle_layer(ctx, extent, threshold_m):
    """地形障碍：真实 DEM 中 ``高程 ≥ 显示阈值`` 的连通区域。"""

    status, reason, detail = _source_state(ctx, "terrain_dtm")
    detail = {**detail, "display_threshold_m": float(threshold_m)}
    if not _qgis_available():
        return _layer(
            ctx, "terrain_obstacle", display_name="地形障碍（≥ 显示阈值）",
            geometry_type=GEOMETRY_POLYGON, source_role="terrain_dtm",
            status=SOURCE_UNKNOWN, reason=_QGIS_MISSING_REASON, detail=detail,
            feature_count=0,
        )
    if status != SOURCE_AVAILABLE:
        return _layer(
            ctx, "terrain_obstacle", display_name="地形障碍（≥ 显示阈值）",
            geometry_type=GEOMETRY_POLYGON, source_role="terrain_dtm", status=status,
            reason=reason or "缺少地形数据源，无法判定地形障碍", detail=detail,
            feature_count=0,
        )
    polygons, stats, failure = _raster_threshold_polygons(
        str((ctx.paths or {}).get("terrain_dtm")), extent, threshold_m,
    )
    if failure:
        return _layer(
            ctx, "terrain_obstacle", display_name="地形障碍（≥ 显示阈值）",
            geometry_type=GEOMETRY_POLYGON, source_role="terrain_dtm",
            status=SOURCE_UNAVAILABLE, reason=failure, detail=detail, feature_count=0,
        )
    detail = {**detail, **stats}
    if not polygons:
        return _layer(
            ctx, "terrain_obstacle", display_name="地形障碍（≥ 显示阈值）",
            geometry_type=GEOMETRY_POLYGON, source_role="terrain_dtm",
            status=SOURCE_AVAILABLE, reason="",
            detail={**detail, "note": "数据源可读，但当前范围内没有达到显示阈值的地形单元"},
            feature_count=0,
        )
    return _layer(
        ctx, "terrain_obstacle", display_name="地形障碍（≥ 显示阈值）",
        geometry_type=GEOMETRY_POLYGON, source_role="terrain_dtm",
        status=SOURCE_AVAILABLE, reason="", detail=detail, feature_count=len(polygons),
        data={"polygons": _polygon_rings(polygons),
              "polygon_holes": _holes_of_polygons(polygons),
              "semantics": "display_only_figure_threshold_derived_from_dem",
              "not_a_planning_constraint": True,
              "not_used_for_route_search": True},
    )


def _building_grid_obstacles(ctx, extent, threshold_m):
    """建筑环境网格（L8 聚合事实）→ 超过显示阈值的格（每格按其 bbox 成面）。

    返回结构化多边形（格网矩形没有内环，因此 ``holes`` 为空列表）——与建筑足迹、
    制图陆地面共用同一个"外环 + 内环"契约，调用点无需区分两种来源。
    """

    from qgis.core import QgsVectorLayer

    path = str((ctx.paths or {}).get("building_grid") or "")
    layer = QgsVectorLayer(path, "building_grid", "ogr")
    if not layer.isValid():
        return [], {}, "建筑环境网格无法打开"
    field_names = {field.name() for field in layer.fields()}
    if "height_max_m" not in field_names:
        return [], {}, "建筑环境网格缺少 height_max_m 字段，无法按阈值判定"
    features = []
    stats = {
        "threshold_m": float(threshold_m), "field": "height_max_m",
        "evaluated_cells": 0, "valid_height_cells": 0, "unresolved_height_cells": 0,
    }
    for feature in layer.getFeatures():
        west, south = _finite(feature["west"]), _finite(feature["south"])
        east, north = _finite(feature["east"]), _finite(feature["north"])
        if None in (west, south, east, north):
            continue
        if east < extent.west or west > extent.east or north < extent.south or south > extent.north:
            continue
        stats["evaluated_cells"] += 1
        height = _finite(feature["height_max_m"])
        if height is None:
            stats["unresolved_height_cells"] += 1
            continue
        stats["valid_height_cells"] += 1
        if height >= float(threshold_m):
            features.append({"exterior": [
                [west, south], [east, south], [east, north], [west, north], [west, south],
            ], "holes": []})
    stats["obstacle_cells"] = len(features)
    return features, stats, None


def _building_footprint_obstacles(ctx, extent, threshold_m):
    """建筑单体足迹（层级无关的精确几何）→ 超过显示阈值的足迹。"""

    from ..gis.qgis_project_layers import resolve_vector_layer_source

    path = (ctx.paths or {}).get("buildings")
    resolved = resolve_vector_layer_source(path, role="buildings")
    if not resolved.get("ok"):
        return [], {}, resolved.get("reason") or "建筑足迹无法解析成可读取的图层"
    from qgis.core import QgsRectangle, QgsVectorLayer

    source = str(resolved.get("path") or "")
    layer_name = resolved.get("layer_name")
    layer = QgsVectorLayer(
        f"{source}|layername={layer_name}" if layer_name else source, "buildings", "ogr",
    )
    if not layer.isValid():
        return [], {}, "建筑足迹图层无法打开"
    field_names = {field.name() for field in layer.fields()}
    if "height_m" not in field_names:
        return [], {}, "建筑足迹缺少 height_m 字段，无法按阈值判定"
    viewport = QgsRectangle(extent.west, extent.south, extent.east, extent.north)
    if _supports_subset(layer):
        layer.setSubsetString(f"intersects(geom, geom_from_wkt('{_bbox_wkt(viewport)}'))")
    features = []
    stats = {
        "threshold_m": float(threshold_m), "field": "height_m",
        "evaluated_footprints": 0, "valid_height_footprints": 0,
        "unresolved_height_footprints": 0, "height_status_field": "height_status",
    }
    for feature in layer.getFeatures():
        geometry = feature.geometry()
        if geometry is None or geometry.isEmpty():
            continue
        if not geometry.boundingBox().intersects(viewport):
            continue
        stats["evaluated_footprints"] += 1
        status_text = str(feature["height_status"]) if "height_status" in field_names else ""
        height = _finite(feature["height_m"])
        if height is None or (status_text and status_text != "valid"):
            stats["unresolved_height_footprints"] += 1
            continue
        stats["valid_height_footprints"] += 1
        if height < float(threshold_m):
            continue
        features.extend(_geometry_to_wgs84_polygons(geometry, layer))
        if len(features) >= 6000:
            stats["truncated"] = True
            break
    stats["obstacle_footprint_rings"] = len(features)
    return features, stats, None


def _building_obstacle_layer(ctx, extent, threshold_m):
    """建筑障碍：真实建筑数据中 ``顶部高度 ≥ 显示阈值`` 的足迹。"""

    status, reason, detail = _source_state(ctx, "building_grid")
    footprint_status, footprint_reason, footprint_detail = _source_state(ctx, "buildings")
    detail = {
        **detail, "display_threshold_m": float(threshold_m),
        "footprint_source": footprint_detail,
    }
    if not _qgis_available():
        return _layer(
            ctx, "building_obstacle", display_name="建筑障碍（≥ 显示阈值）",
            geometry_type=GEOMETRY_POLYGON, source_role="building_grid",
            status=SOURCE_UNKNOWN, reason=_QGIS_MISSING_REASON, detail=detail,
            feature_count=0,
        )
    if status != SOURCE_AVAILABLE and footprint_status != SOURCE_AVAILABLE:
        return _layer(
            ctx, "building_obstacle", display_name="建筑障碍（≥ 显示阈值）",
            geometry_type=GEOMETRY_POLYGON, source_role="building_grid", status=status,
            reason=reason or footprint_reason or "缺少建筑数据源，无法判定建筑障碍",
            detail=detail, feature_count=0,
        )
    if status == SOURCE_AVAILABLE:
        features, stats, failure = _building_grid_obstacles(ctx, extent, threshold_m)
        source_role, strategy = "building_grid", "aggregated_height_max_m_per_grid_cell"
    else:
        features, stats, failure = _building_footprint_obstacles(ctx, extent, threshold_m)
        source_role, strategy = "buildings", "per_footprint_height_m"
    if failure:
        return _layer(
            ctx, "building_obstacle", display_name="建筑障碍（≥ 显示阈值）",
            geometry_type=GEOMETRY_POLYGON, source_role=source_role,
            status=SOURCE_UNAVAILABLE, reason=failure, detail=detail, feature_count=0,
        )
    detail = {**detail, **stats, "strategy": strategy}
    if not features:
        return _layer(
            ctx, "building_obstacle", display_name="建筑障碍（≥ 显示阈值）",
            geometry_type=GEOMETRY_POLYGON, source_role=source_role,
            status=SOURCE_AVAILABLE, reason="",
            detail={**detail, "note": "数据源可读，但当前范围内没有达到显示阈值的建筑"},
            feature_count=0,
        )
    return _layer(
        ctx, "building_obstacle", display_name="建筑障碍（≥ 显示阈值）",
        geometry_type=GEOMETRY_POLYGON, source_role=source_role,
        status=SOURCE_AVAILABLE, reason="", detail=detail, feature_count=len(features),
        data={"polygons": _polygon_rings(features),
              "polygon_holes": _holes_of_polygons(features),
              "semantics": "display_only_figure_threshold_on_real_building_heights",
              "not_a_planning_constraint": True,
              "not_used_for_route_search": True},
    )


# --------------------------------------------------------------------------- 铁塔 / 机场

def _tower_coordinate(tower, conflicts=None):
    """铁塔坐标解析（**唯一入口**）：``coordinate`` 优先，字段不一致时以权威顺序为准。

    真实集合的约定是 ``coordinate = [longitude, latitude]``（见
    :mod:`cns_planner.domain.towers` 的 ``_coordinate``）。本函数：

    1. 优先读 ``coordinate``，回退到 ``longitude`` / ``latitude``；
    2. 若两种表达同时存在且不一致，**按经度/纬度语义判定**，并把冲突记入
       ``conflicts``（供 FigureSpec 审计），绝不静默改写坐标；
    3. 只接受中国范围内的合理经纬度，超出范围一律丢弃（不猜、不偏移、不对调）。
    """

    coordinate = tower.get("coordinate")
    field_longitude = _finite(tower.get("longitude"))
    field_latitude = _finite(tower.get("latitude"))
    source = None
    longitude = latitude = None
    if isinstance(coordinate, (list, tuple)) and len(coordinate) >= 2:
        longitude, latitude = _finite(coordinate[0]), _finite(coordinate[1])
        source = "coordinate_field"
    if longitude is None or latitude is None:
        longitude, latitude = field_longitude, field_latitude
        source = "longitude_latitude_fields"
    if longitude is None or latitude is None:
        return None, None
    if field_longitude is not None and field_latitude is not None and source == "coordinate_field":
        if (abs(field_longitude - longitude) > 1e-9 or abs(field_latitude - latitude) > 1e-9):
            if conflicts is not None:
                conflicts.append({
                    "tower_id": str(tower.get("tower_id") or ""),
                    "coordinate": [longitude, latitude],
                    "longitude_latitude": [field_longitude, field_latitude],
                    "resolved_by": "coordinate_field_is_authoritative",
                })
    if not (-180.0 <= longitude <= 180.0 and -90.0 <= latitude <= 90.0):
        return None, None
    if longitude == 0.0 and latitude == 0.0:
        return None, None
    # 轴序保护：中国的经度必为 73~136、纬度必为 3~54；若"经度"落在纬度范围而"纬度"
    # 落在经度范围，说明记录发生了轴序错误——此时**拒绝**该点并交给审计，绝不静默对调。
    looks_swapped = (
        73.0 <= latitude <= 136.0 and 3.0 <= longitude <= 54.0
    )
    if looks_swapped and not (73.0 <= longitude <= 136.0 and 3.0 <= latitude <= 54.0):
        if conflicts is not None:
            conflicts.append({
                "tower_id": str(tower.get("tower_id") or ""),
                "reason": "axis_order_looks_swapped",
                "coordinate": [longitude, latitude],
            })
        return None, None
    return longitude, latitude


def _sites_within_route_buffer(points, route_path, *, buffer_km, metric_crs=DEFAULT_METRIC_CRS):
    """返回距航路线不超过展示缓冲的站址（只用于专题图 DISPLAY）。

    输入、输出都保留 WGS84 站址记录；距离唯一在显式米制 CRS 中计算。该函数不读取、
    不修改 canonical ``state["towers"]``，也不参与通信/CNS 规划或业务 land-mask。
    """

    route = [
        (_finite(point[0]), _finite(point[1]))
        for point in (route_path or [])
        if isinstance(point, (list, tuple)) and len(point) >= 2
    ]
    route = [(longitude, latitude) for longitude, latitude in route
             if longitude is not None and latitude is not None]
    if not points or len(route) < 2:
        return []
    from pyproj import CRS, Transformer
    from shapely.geometry import LineString, Point
    transformer = Transformer.from_crs(
        CRS.from_epsg(4326), CRS.from_user_input(metric_crs), always_xy=True,
    )
    route_metric = LineString([transformer.transform(*point) for point in route])
    limit_m = float(buffer_km) * 1000.0
    kept = []
    for item in points:
        x, y = transformer.transform(float(item["longitude"]), float(item["latitude"]))
        if Point(x, y).distance(route_metric) <= limit_m:
            kept.append(item)
    return kept


def _tower_layers(ctx, extent, threshold_m):
    """既有**通信站址**（真实站址清单）与铁塔障碍（已确认障碍物高度 ≥ 显示阈值）。

    命名口径：源 Excel 的"铁塔细分类型"含楼面抱杆、楼面拉线塔、美化外罩、落地塔、
    H 杆塔、单管塔等，373 个点实际是**通信站址集合**，不全部是独立铁塔，因此图层与
    图例统一称"既有通信站址"（位置与坐标一个字都不改）。
    """

    collection = ctx.state.get("towers") or {}
    towers = collection.get("items") or []
    if not _qgis_available():
        return [
            _layer(ctx, "tower_existing", display_name="既有通信站址",
                   geometry_type=GEOMETRY_POINT, source_role="towers",
                   status=SOURCE_UNKNOWN, reason=_QGIS_MISSING_REASON, feature_count=0),
            _layer(ctx, "tower_obstacle", display_name="铁塔障碍",
                   geometry_type=GEOMETRY_POINT, source_role="tower_obstacle_profiles",
                   status=SOURCE_UNKNOWN, reason=_QGIS_MISSING_REASON, feature_count=0),
        ]
    from qgis.core import QgsRectangle

    viewport = QgsRectangle(extent.west, extent.south, extent.east, extent.north)
    extent_points = []
    axis_conflicts = []
    for tower in towers:
        if not isinstance(tower, dict):
            continue
        longitude, latitude = _tower_coordinate(tower, axis_conflicts)
        if longitude is None or latitude is None:
            continue
        if not viewport.contains(longitude, latitude):
            continue
        extent_points.append({
            "longitude": longitude, "latitude": latitude,
            "name": _short(tower.get("name")),
            "tower_id": str(tower.get("tower_id") or ""),
            "height_m": _finite(tower.get("height_m")),
            "site_type": tower.get("site_type"),
            "coordinate_source": tower.get("coordinate_source") or "tower_record",
        })
    site_buffer_km = _finite(ctx.parameters.get("site_display_buffer_km"), 10.0)
    site_metric_crs = "EPSG:32651"
    points = _sites_within_route_buffer(
        extent_points, ctx.route.get("path") or [], buffer_km=site_buffer_km,
        metric_crs=site_metric_crs,
    )
    tower_detail = {
        "role": "towers",
        "collection_id": collection.get("collection_id"),
        "total_count": len(towers),
        "within_extent": len(extent_points),
        "within_route_buffer": len(points),
        "omitted_by_route_distance": max(0, len(extent_points) - len(points)),
        "out_of_extent": max(0, len(towers) - len(extent_points)),
        "site_display_buffer_km": float(site_buffer_km),
        "distance_crs": site_metric_crs,
        "source_crs": ((collection.get("crs") or {}).get("source_crs") or {}).get("value"),
        "source_crs_status": ((collection.get("crs") or {}).get("source_crs") or {}).get("status"),
        "crs_confirmed": bool(((collection.get("crs") or {}).get("source_crs") or {}).get("confirmed")),
        "axis_order_conflicts": axis_conflicts[:5],
        "axis_order_conflict_count": len(axis_conflicts),
        "coordinate_field": (
            "coordinate" if any(item.get("coordinate_source") == "coordinate_field"
                                for item in extent_points) else "longitude/latitude"
        ),
        "semantics": "confirmed_tower_records_filtered_for_route_overview_display_only",
        "affects_state_towers": False,
        "affects_communication_or_cns_planning": False,
    }
    layers = []
    if towers:
        if points:
            layers.append(_layer(
                ctx, "tower_existing", display_name="既有通信站址",
                geometry_type=GEOMETRY_POINT, source_role="towers",
                status=SOURCE_AVAILABLE, reason="", detail=tower_detail,
                feature_count=len(points), data={"points": points},
            ))
        else:
            layers.append(_layer(
                ctx, "tower_existing", display_name="既有通信站址",
                geometry_type=GEOMETRY_POINT, source_role="towers",
                status=SOURCE_AVAILABLE, reason="",
                detail={**tower_detail,
                        "note": f"已导入通信站址，但当前图面范围内没有距航路 "
                                f"{site_buffer_km:g} km 内的站址；未显示不代表站址不存在"},
                feature_count=0,
            ))
    else:
        layers.append(_layer(
            ctx, "tower_existing", display_name="既有通信站址",
            geometry_type=GEOMETRY_POINT, source_role="towers",
            status=SOURCE_UNKNOWN,
            reason="项目尚未导入铁塔数据（未导入 ≠ 现实中不存在铁塔）",
            detail=tower_detail, feature_count=0,
        ))

    profile_collection = ctx.state.get("tower_obstacle_profiles") or {}
    profiles = profile_collection.get("items") or {}
    obstacle_points, resolved, unresolved = [], 0, 0
    for profile in profiles.values() if isinstance(profiles, dict) else []:
        if not isinstance(profile, dict):
            continue
        longitude, latitude = _finite(profile.get("longitude")), _finite(profile.get("latitude"))
        if longitude is None or latitude is None or not viewport.contains(longitude, latitude):
            continue
        height = _finite(profile.get("obstacle_height_m")) or _finite(
            profile.get("obstacle_height_egm2008_m"),
        )
        status_text = str(profile.get("status") or "")
        if height is None or status_text not in ("", "resolved", "resolved_unconfirmed"):
            unresolved += 1
            continue
        resolved += 1
        if height >= float(threshold_m):
            obstacle_points.append({
                "longitude": longitude, "latitude": latitude,
                "name": _short(profile.get("name")),
                "tower_id": str(profile.get("tower_id") or ""),
                "obstacle_height_m": height,
                "status": status_text or None,
            })
    obstacle_detail = {
        "role": "tower_obstacle_profiles",
        "collection_id": profile_collection.get("collection_id"),
        "collection_status": profile_collection.get("status"),
        "resolved_within_extent": resolved,
        "unresolved_within_extent": unresolved,
        "display_threshold_m": float(threshold_m),
        "semantics": "confirmed_obstacle_height_facts_from_project_state",
    }
    if resolved or unresolved:
        if obstacle_points:
            layers.append(_layer(
                ctx, "tower_obstacle", display_name="铁塔障碍",
                geometry_type=GEOMETRY_POINT, source_role="tower_obstacle_profiles",
                status=SOURCE_AVAILABLE, reason="", detail=obstacle_detail,
                feature_count=len(obstacle_points), data={"points": obstacle_points},
            ))
        else:
            layers.append(_layer(
                ctx, "tower_obstacle", display_name="铁塔障碍",
                geometry_type=GEOMETRY_POINT, source_role="tower_obstacle_profiles",
                status=SOURCE_AVAILABLE, reason="",
                detail={**obstacle_detail,
                        "note": "障碍物高度事实存在，但当前范围内没有达到显示阈值的铁塔障碍"},
                feature_count=0,
            ))
    else:
        layers.append(_layer(
            ctx, "tower_obstacle", display_name="铁塔障碍",
            geometry_type=GEOMETRY_POINT, source_role="tower_obstacle_profiles",
            status=SOURCE_UNKNOWN,
            reason="尚未派生出已确认的铁塔障碍物高度事实（未派生 ≠ 塔上没有障碍物）",
            detail=obstacle_detail, feature_count=0,
        ))
    return layers


def _airport_layers(ctx, extent):
    """机场与机场净空/保护范围。

    本轮项目没有配置机场数据集，也不存在 canonical 机场保护范围容器，因此这里如实
    返回 ``unknown`` 并给出原因；绝不用参考起降点冒充机场，也不推断净空范围。
    """

    status, reason, detail = _source_state(ctx, "airports")
    reason = reason or "项目未配置机场数据集，且不存在 canonical 机场保护范围容器"
    layer_status = SOURCE_UNKNOWN if status != SOURCE_AVAILABLE else SOURCE_UNAVAILABLE
    if status == SOURCE_AVAILABLE:
        reason = "机场数据源已配置但尚无读取实现（本轮不伪造机场要素）"
    return [
        _layer(ctx, "airport", display_name="机场", geometry_type=GEOMETRY_POINT,
               source_role="airports", status=layer_status, reason=reason,
               detail=detail, feature_count=0),
        _layer(ctx, "airport_protection", display_name="机场净空/保护范围",
               geometry_type=GEOMETRY_POLYGON, source_role="airports",
               status=layer_status,
               reason=reason if status == SOURCE_AVAILABLE
               else "项目未配置机场数据集，无法得到净空/保护范围（不做推断）",
               detail=detail, feature_count=0),
    ]


# --------------------------------------------------------------------------- 航路与标注

def _route_layers(ctx):
    """规划航路 / 转弯点 / 起点 / 终点：全部来自权威运行航路几何。"""

    geometry = [
        [float(point[0]), float(point[1])]
        for point in (ctx.route.get("path") or [])
        if isinstance(point, (list, tuple)) and len(point) >= 2
        and _finite(point[0]) is not None and _finite(point[1]) is not None
    ]
    turns = _turn_points(geometry)
    start = geometry[0] if geometry else []
    end = geometry[-1] if geometry else []
    route_detail = {
        "route_id": ctx.route.get("route_id"),
        "route_source": OPERATIONAL_ROUTE_SOURCE,
        "route_kind": ctx.route.get("kind"),
        "path_crs": ctx.route.get("path_crs") or DEFAULT_CRS,
        "point_count": len(geometry),
        "provenance": deepcopy(ctx.route.get("provenance") or {}),
    }
    layers = [
        _layer(
            ctx, "planned_route", display_name="规划航路", geometry_type=GEOMETRY_LINE,
            source_role=OPERATIONAL_ROUTE_SOURCE,
            status=SOURCE_AVAILABLE if len(geometry) >= 2 else SOURCE_UNAVAILABLE,
            reason="" if len(geometry) >= 2 else "权威运行航路几何顶点不足（< 2）",
            detail=route_detail, feature_count=1 if len(geometry) >= 2 else 0,
            data={"geometry": geometry, "path_crs": ctx.route.get("path_crs") or DEFAULT_CRS},
        ),
        _layer(
            ctx, "turn_point", display_name="航路转弯点", geometry_type=GEOMETRY_POINT,
            source_role=OPERATIONAL_ROUTE_SOURCE, status=SOURCE_AVAILABLE, reason="",
            detail={**route_detail, "derived_for_display_only": True},
            feature_count=len(turns),
            data={"points": [{"longitude": point[0], "latitude": point[1], "index": index + 1}
                             for index, point in enumerate(turns)],
                  "derivation": "authoritative_route_polyline_vertices_excluding_endpoints",
                  "not_route_algorithm_turn_cost": True},
        ),
        _layer(
            ctx, "start_point", display_name="起点", geometry_type=GEOMETRY_POINT,
            source_role=OPERATIONAL_ROUTE_SOURCE,
            status=SOURCE_AVAILABLE if start else SOURCE_UNAVAILABLE,
            reason="" if start else "航路缺少起点", detail=route_detail,
            feature_count=1 if start else 0,
            data={"points": ([{"longitude": start[0], "latitude": start[1]}] if start else [])},
        ),
        _layer(
            ctx, "end_point", display_name="终点", geometry_type=GEOMETRY_POINT,
            source_role=OPERATIONAL_ROUTE_SOURCE,
            status=SOURCE_AVAILABLE if end else SOURCE_UNAVAILABLE,
            reason="" if end else "航路缺少终点", detail=route_detail,
            feature_count=1 if end else 0,
            data={"points": ([{"longitude": end[0], "latitude": end[1]}] if end else [])},
        ),
    ]
    return layers, geometry, turns


def _node_name(ctx, node_id, limit=MAX_LABEL_CHARS):
    """节点名（只读 canonical ``nodes``）；``limit`` 控制是否截断。

    Round30-B1.1：起终点标签改用更宽的上限（:data:`MAX_ENDPOINT_LABEL_CHARS`），
    使正式站名能完整交给标签卡**自动换行**，而不是在图面上留下省略号。
    """

    if not node_id:
        return ""
    for node in ctx.state.get("nodes") or []:
        if isinstance(node, dict) and str(node.get("node_id")) == str(node_id):
            return _short(node.get("name"), limit)
    return ""


def _endpoint_label_name(ctx, route, key, node_field):
    """终点名称解析：只读真实字段，绝不因为 schema 形态不同而抛异常。

    canonical ``operational_routes`` 的 ``start`` / ``end`` 是**坐标数组**
    ``[lon, lat]``（见 :mod:`cns_planner.application.route_service` /
    :mod:`cns_planner.domain.v3_operational_adoption`），**没有** ``name`` 字段；
    历史/兼容记录里也可能写成 ``{"name": ...}`` 字典或纯字符串。这里按三种真实形态
    解析，任何一种都不是错误，因此不抛异常、也不猜名字：

    1. ``{"name": ...}`` → 用其中的名称；
    2. 字符串 → 直接当名称；
    3. 坐标数组 / 其它 → 退回节点名（``start_node_id`` / ``end_node_id``）。

    Round30-B1.1：起终点名称**不再截断**（放长到 :data:`MAX_ENDPOINT_LABEL_CHARS`），
    正式图由标签卡**自动换行**展示完整名称；省略号只在极端超长时才会出现。
    """

    value = route.get(key)
    name = ""
    if isinstance(value, dict):
        name = _short(value.get("name"), MAX_ENDPOINT_LABEL_CHARS)
    elif isinstance(value, str):
        name = _short(value, MAX_ENDPOINT_LABEL_CHARS)
    if name:
        return name
    return _short(
        _node_name(ctx, route.get(node_field), MAX_ENDPOINT_LABEL_CHARS),
        MAX_ENDPOINT_LABEL_CHARS,
    )


def _labels(ctx, geometry, turns, layers):
    """中文标注：起终点必标，转弯点少量标注，地名来自真实字段而非硬编码。"""

    route = ctx.route or {}
    labels = []
    if geometry:
        start_name = _endpoint_label_name(ctx, route, "start", "start_node_id")
        end_name = _endpoint_label_name(ctx, route, "end", "end_node_id")
        labels.append(LabelSpec(
            kind="start", text=start_name or "起点",
            longitude=geometry[0][0], latitude=geometry[0][1],
            priority=100, style_key="label_endpoint",
        ))
        labels.append(LabelSpec(
            kind="end", text=end_name or "终点",
            longitude=geometry[-1][0], latitude=geometry[-1][1],
            priority=100, style_key="label_endpoint",
        ))
    if turns and len(turns) <= 6 and ctx.parameters.get("label_turn_points", True):
        for index, point in enumerate(turns):
            labels.append(LabelSpec(
                kind="turn", text=f"转弯点 {index + 1}",
                longitude=point[0], latitude=point[1],
                priority=70, style_key="label_turn",
            ))
    if ctx.parameters.get("show_place_labels", True):
        for layer in layers:
            if layer.layer_key != "land":
                continue
            for name, coordinate in (layer.data.get("feature_attributes") or {}).items():
                if not coordinate:
                    continue
                labels.append(LabelSpec(
                    kind="place", text=_short(name, MAX_PLACE_LABEL_CHARS),
                    longitude=coordinate[0], latitude=coordinate[1],
                    priority=30, style_key="label_place",
                ))
    return labels


# --------------------------------------------------------------------------- CNS 专题图装配

#: CNS 专题图共用的模板分组（决定"哪张图取哪些服务"）。
def _cns_services_for_template(template_id, parameters):
    """模板 → 该图允许出现的服务集合。

    **唯一分派点**：任何模板都不可能画出自己服务集合之外的设施。
    ``surveillance_layout_v1`` 必须由 ``surveillance_service`` 决定是 RID 还是 Radar。
    """

    if template_id == COMMUNICATION_LAYOUT_V1:
        return (SERVICE_COMMUNICATION,)
    if template_id == NAVIGATION_LAYOUT_V1:
        return (SERVICE_NAVIGATION_INTEGRITY,)
    if template_id == SURVEILLANCE_LAYOUT_V1:
        variant = str(parameters.get(SURVEILLANCE_SERVICE_PARAMETER) or "")
        if variant == SURVEILLANCE_SERVICE_RID:
            return (SERVICE_RID,)
        if variant == SURVEILLANCE_SERVICE_RADAR:
            return (SERVICE_RADAR,)
        # 到这里说明参数校验被绕过；绝不静默回退到任一 variant。
        raise MapFigureTemplateUnavailable(
            "监视布设图必须显式给出 surveillance_service 且只能取 "
            + " / ".join(SURVEILLANCE_SERVICE_VALUES),
            code="map_figure_surveillance_variant_required",
            detail={"surveillance_service": variant or None,
                    "allowed": list(SURVEILLANCE_SERVICE_VALUES)},
        )
    if template_id == CNS_COMBINED_V1:
        return (SERVICE_COMMUNICATION, SERVICE_RID, SERVICE_NAVIGATION_INTEGRITY,
                SERVICE_RADAR)
    return ()


#: 服务键 → 该服务的提案图层键与图例名。
_PROPOSAL_LAYER_OF_SERVICE = {
    SERVICE_COMMUNICATION: ("cns_comm_proposal", "通信规划提案（未确认）"),
    SERVICE_RID: ("cns_rid_proposal", "RID 规划提案（未确认）"),
    SERVICE_NAVIGATION_INTEGRITY: ("cns_nav_proposal", "导航完整性监测点提案（未确认）"),
}

#: 图例标签里追加的服务后缀（只有真实拥有对应 action 的站才追加）。
_SERVICE_TAG = {
    SERVICE_COMMUNICATION: "通信",
    SERVICE_RID: "RID",
    SERVICE_NAVIGATION_INTEGRITY: "导航",
}

#: 服务键 → 服务家族（背景卡分块配色按家族取色）。
_SERVICE_FAMILY = {
    SERVICE_COMMUNICATION: "communication",
    SERVICE_RID: "rid",
    SERVICE_RADAR: "radar",
    SERVICE_NAVIGATION_INTEGRITY: "navigation",
}

#: 服务家族 → 站名后缀（与 :data:`_SERVICE_TAG` 同源，避免两处漂移）。
_FAMILY_TAG = {
    "communication": _SERVICE_TAG[SERVICE_COMMUNICATION],
    "rid": _SERVICE_TAG[SERVICE_RID],
    "radar": "雷达",
    "navigation": _SERVICE_TAG[SERVICE_NAVIGATION_INTEGRITY],
}

#: Round30-B1.2：允许被**并入起终点主卡**的服务家族。
#:
#: 当前唯一成员是 ``navigation``：N005 / N006 的导航完整性监测点与起降点完全同址，
#: 单独出卡只会产生"两个文字卡 + 两条引线"的重复表达。通信 / RID 同址仍按既有
#: 分色分块方案处理（它们的服务语义与"起降点"不是同一个对象）。
ENDPOINT_MERGE_SERVICES = ("navigation",)

#: 同址判定容差（度）：约 0.1 mm 量级，只吸收浮点表示差异，不做任何近似匹配。
ENDPOINT_MERGE_TOLERANCE_DEG = 1e-9

#: 合并进起降点主卡第二行时的显示文本（只影响**文字**，不改任何业务语义）。
_ENDPOINT_MERGE_LINE = {
    "navigation": "导航监测提案（未确认）",
}


def _cns_frame_detail(assembly, *, service_key="", extra=None):
    """所有 CNS 图层共用的审计字段（确认语义 / 指纹 / 只读边界）。"""

    evidence = assembly.evidence or {}
    confirmation = evidence.get("confirmation") or {}
    detail = {
        "confirmation": deepcopy(confirmation),
        "step6_gate": deepcopy(evidence.get("step6_gate") or {}),
        "proposal_not_confirmed": not bool(confirmation.get("confirmed_plan_status") == "confirmed"),
        "p16_input_fingerprint": (evidence.get("p16") or {}).get("input_fingerprint"),
        "p16_algorithm_version": (evidence.get("p16") or {}).get("algorithm_version"),
        "p17_algorithm_version": (evidence.get("step6_gate") or {}).get("p17_algorithm_version"),
        "p17_input_fingerprint": (evidence.get("step6_gate") or {}).get("p17_input_fingerprint"),
        "radar_algorithm_version": (evidence.get("radar") or {}).get("algorithm_version"),
        "radar_input_fingerprint": (evidence.get("radar") or {}).get("input_fingerprint"),
        "read_only": True,
        "writes_state": False,
        "recomputes_business_results": False,
        "semantics": "cns_figure_reads_canonical_results_verbatim",
    }
    if service_key:
        detail["service_key"] = service_key
    if extra:
        detail.update(extra)
    return detail


def _cns_facility_layer(ctx, layer_key, display_name, records, assembly, *, service_key):
    """提案 / 既有设施图层（点）。空记录时如实登记为不可用，绝不补点。"""

    detail = _cns_frame_detail(assembly, service_key=service_key, extra={
        "record_count": len(records),
        "identities": sorted({record.identity for record in records}),
        "action_ids": [record.action_id for record in records if record.action_id][:32],
        "facility_ids": [record.facility_id for record in records if record.facility_id][:32],
        "site_ids": [record.site_id for record in records if record.site_id][:32],
        "tower_ids": [record.tower_id for record in records if record.tower_id][:32],
        "distinct_site_ids": [
            record.distinct_site_id for record in records if record.distinct_site_id
        ][:32],
        "coordinates": [
            [record.longitude, record.latitude] for record in records
        ][:32],
        "source_result_fingerprints": sorted({
            record.source_result_fingerprint for record in records
            if record.source_result_fingerprint
        }),
        "confirmed_flags": sorted({bool(record.confirmed) for record in records}),
        "source_confirmed_flags": sorted({bool(record.source_confirmed) for record in records}),
        "source_confirmed_semantics": (
            "上游记录的 confirmed 只表示规划宿主 / 设备档案层面，"
            "方案确认状态以 confirmed_cns_plan 为准"
        ),
    })
    if not records:
        return _layer(
            ctx, layer_key, display_name=display_name, geometry_type=GEOMETRY_POINT,
            source_role="cns_corridor_site_plan", status=SOURCE_AVAILABLE, reason="",
            detail={**detail, "note": "该服务在当前图面范围内没有选中动作（未选中 ≠ 不存在需求）"},
            feature_count=0,
        )
    return _layer(
        ctx, layer_key, display_name=display_name, geometry_type=GEOMETRY_POINT,
        source_role="cns_corridor_site_plan", status=SOURCE_AVAILABLE, reason="",
        detail=detail, feature_count=len(records),
        data={"points": [
            {
                "longitude": record.longitude, "latitude": record.latitude,
                "record_id": record.record_id, "identity": record.identity,
                "service_key": record.service_key, "site_id": record.site_id,
                "tower_id": record.tower_id or None, "display_name": record.display_name,
            }
            for record in records
        ]},
    )


def _cns_record_set_layer(ctx, layer_key, display_name, records, assembly, *, service_key):
    """既有 CNS 设施图层：集合非空才画，空集合如实登记为范围内 0 条。"""

    detail = _cns_frame_detail(assembly, service_key=service_key, extra={
        "record_count": len(records),
        "facility_ids": [record.facility_id for record in records if record.facility_id][:32],
        "identities": sorted({record.identity for record in records}),
        "existing_cns_evidence": deepcopy(assembly.evidence.get("existing_cns") or {}),
        "semantics": "real_world_existing_cns_baseline_facility_only",
    })
    if not records:
        return _layer(
            ctx, layer_key, display_name=display_name, geometry_type=GEOMETRY_POINT,
            source_role="existing_cns_facilities", status=SOURCE_AVAILABLE, reason="",
            detail={**detail, "note": (
                "当前图面范围内没有可作为现实既有设施的记录；"
                "项目内合成 / 工程验证设施不在此表达"
            )},
            feature_count=0,
        )
    return _layer(
        ctx, layer_key, display_name=display_name, geometry_type=GEOMETRY_POINT,
        source_role="existing_cns_facilities", status=SOURCE_AVAILABLE, reason="",
        detail=detail, feature_count=len(records),
        data={"points": [
            {
                "longitude": record.longitude, "latitude": record.latitude,
                "record_id": record.record_id, "identity": record.identity,
                "service_key": record.service_key, "site_id": record.site_id,
                "tower_id": record.tower_id or None, "display_name": record.display_name,
            }
            for record in records
        ]},
    )


def _cns_coverage_layer(ctx, layer_key, display_name, records, radius_m, assembly, *,
                        service_key, metric_crs, semantics):
    """规划服务半径圆（**只用于图面表达**，不参与任何业务重算）。"""

    rings = []
    for record in records:
        ring = coverage_circle_ring(
            record.longitude, record.latitude, radius_m, metric_crs=metric_crs,
        )
        if ring:
            rings.append(ring)
    detail = _cns_frame_detail(assembly, service_key=service_key, extra={
        "radius_m": float(radius_m),
        "circle_count": len(rings),
        "metric_crs": str(metric_crs),
        "geometry_method": "true_metric_circle_buffered_in_metric_crs_then_reprojected",
        "radius_semantics": semantics,
        "is_measured_propagation_contour": False,
        "is_business_evidence": False,
        "affects_planning": False,
    })
    if not rings:
        return _layer(
            ctx, layer_key, display_name=display_name, geometry_type=GEOMETRY_POLYGON,
            source_role="cns_corridor_site_plan", status=SOURCE_AVAILABLE, reason="",
            detail={**detail, "note": "没有可绘制覆盖圈的选中站址"},
            feature_count=0,
        )
    return _layer(
        ctx, layer_key, display_name=display_name, geometry_type=GEOMETRY_POLYGON,
        source_role="cns_corridor_site_plan", status=SOURCE_AVAILABLE, reason="",
        detail=detail, feature_count=len(rings),
        data={"polygons": rings, "radius_m": float(radius_m)},
    )


def _cns_surface_aware_rid_layers(ctx, records, assembly, *, metric_crs):
    """RID map-only footprint: 2 km base + confirmed-sea part of the 2--5 km annulus."""

    land_radius, sea_radius = rid_radius_pair()
    facts = ctx.state.get("surface_class_facts")
    policy = ctx.state.get("surface_classification_policy") or {}
    path = land_mask_source_path(ctx.state, (ctx.paths or {}).get("land_mask"))
    try:
        land_mask = build_land_mask_source(path, policy)
    except (OSError, ValueError):
        land_mask = None

    base_records, extension_records, extension_lines, per_site = [], [], [], []
    for record in records:
        footprint = build_surface_aware_rid_footprint(
            record.longitude, record.latitude,
            land_radius_m=land_radius, sea_radius_m=sea_radius,
            land_mask_source=land_mask, surface_class_facts=facts,
            metric_crs=metric_crs,
        )
        base_records.extend(footprint["base_polygons"])
        extension_records.extend(footprint["sea_extension_polygons"])
        extension_lines.extend(footprint["sea_extension_boundary_lines"])
        per_site.append({
            "record_id": record.record_id,
            "site_id": record.site_id,
            "extension_polygon_count": len(footprint["sea_extension_polygons"]),
            **deepcopy(footprint["metadata"]),
        })

    shared = _cns_frame_detail(assembly, service_key=SERVICE_RID, extra={
        "surface_aware_visualization": True,
        "affects_planning": False,
        "is_business_evidence": False,
        "is_measured_propagation_contour": False,
        "land_radius_m": float(land_radius),
        "sea_radius_m": float(sea_radius),
        "registered_radii_unchanged": True,
        "surface_class_facts_status": facts.get("status") if isinstance(facts, dict) else None,
        "surface_class_facts_input_fingerprint": (
            facts.get("input_fingerprint") if isinstance(facts, dict) else None
        ),
        "land_mask_path_configured": bool(path),
        "unknown_is_fail_closed_to_2km": True,
        "site_footprints": per_site,
    })
    base = _layer(
        ctx, "cns_coverage_rid_land",
        display_name="RID 陆地/沿海规划范围 2 km",
        geometry_type=GEOMETRY_POLYGON, source_role="cns_corridor_site_plan",
        status=SOURCE_AVAILABLE, reason="", feature_count=len(base_records),
        detail={
            **shared,
            "geometry_method": "true_metric_2km_omnidirectional_base",
            "radius_m": float(land_radius),
            "polygon_count": len(base_records),
            "radius_semantics": "rid_land_coastal_unknown_maximum_planning_range_2km",
        },
        data={
            "polygons": _polygon_rings(base_records),
            "polygon_holes": _holes_of_polygons(base_records),
            "radius_m": float(land_radius),
        },
    )
    extension = _layer(
        ctx, "cns_coverage_rid_sea",
        display_name="RID 海上延伸规划范围 2–5 km",
        geometry_type=GEOMETRY_LINE, source_role="cns_corridor_site_plan",
        status=SOURCE_AVAILABLE, reason="", feature_count=len(extension_lines),
        detail={
            **shared,
            "geometry_method": (
                "metric_annulus_2_to_5km_intersection_confirmed_sea_from_authoritative_land_mask"
            ),
            "radius_m": float(sea_radius),
            "inner_radius_m": float(land_radius),
            "outer_radius_m": float(sea_radius),
            "polygon_count": len(extension_records),
            "outer_boundary_line_count": len(extension_lines),
            "radius_semantics": "rid_confirmed_sea_extension_planning_range_2_to_5km",
        },
        data={
            "lines": extension_lines,
            # Retain the complete clipped footprint in FigureSpec for audit/inspection;
            # the renderer draws only the supplied 5 km boundary arcs.
            "polygons": _polygon_rings(extension_records),
            "polygon_holes": _holes_of_polygons(extension_records),
            "radius_m": float(sea_radius),
            "inner_radius_m": float(land_radius),
            "outer_radius_m": float(sea_radius),
        },
    )
    return [base, extension], {
        "surface_aware_visualization": True,
        "affects_planning": False,
        "rid_footprint_semantics": "2km_omnidirectional_plus_2_to_5km_confirmed_sea",
        "rid_extension_polygon_count": len(extension_records),
        "rid_extension_outer_boundary_line_count": len(extension_lines),
        "rid_extension_fail_closed": not bool(extension_records),
    }


def _cns_declared_layer(ctx, layer_key, display_name, assembly, *, service_key, detail_extra,
                        source_role="radar_surveillance_layout"):
    """**声明型图层**：只在图例中出现，不画任何几何，因此不可能伪装成要素。"""

    return _layer(
        ctx, layer_key, display_name=display_name, geometry_type=GEOMETRY_NONE,
        source_role=source_role, status=SOURCE_AVAILABLE, reason="",
        detail=_cns_frame_detail(assembly, service_key=service_key, extra=detail_extra),
        feature_count=0,
    )


def _radar_context_points(ctx, assembly, radar):
    """Radar-I **评估过的候选塔**上下文（蓝色），绝不是已建 / 已选站点。

    坐标只从 state 的真实站址清单取；候选塔 id 来自 Radar 结果自身。
    若某个候选 id 在站址清单里找不到，就**跳过并在 detail 里登记**，绝不猜坐标。
    """

    candidate_ids = set(radar.get("candidate_tower_ids") or [])
    selected_ids = set(radar.get("selected_tower_ids") or [])
    if not candidate_ids:
        return [], {"candidate_tower_count": 0, "reason": "radar_item_has_no_candidate_towers"}
    towers = (ctx.state.get("towers") or {}).get("items") or []
    points, missing = [], []
    for tower in towers:
        if not isinstance(tower, dict):
            continue
        tower_id = str(tower.get("tower_id") or "")
        if tower_id not in candidate_ids:
            continue
        longitude, latitude = _tower_coordinate(tower)
        if longitude is None or latitude is None:
            missing.append(tower_id)
            continue
        points.append({
            "longitude": longitude, "latitude": latitude,
            "tower_id": tower_id, "name": _short(tower.get("name")),
            "radar_selected": tower_id in selected_ids,
        })
    not_found = sorted(candidate_ids - {item["tower_id"] for item in points} - set(missing))
    return points, {
        "candidate_tower_count": len(candidate_ids),
        "drawn_candidate_tower_count": len(points),
        "selected_tower_count": len(selected_ids),
        "coordinate_missing": missing[:8],
        "tower_id_not_in_tower_collection": not_found[:8],
        "selected_tower_ids": sorted(selected_ids)[:8],
        "semantics": "radar_evaluated_candidate_tower_context_never_a_built_or_selected_site",
    }


def _radar_sector_ring(panel, metric_crs=DEFAULT_METRIC_CRS, *, arc_steps=24):
    """Selected Radar panel -> WGS84 annular-sector ring for display only."""

    longitude = _finite(panel.get("longitude"))
    latitude = _finite(panel.get("latitude"))
    azimuth = _finite(panel.get("azimuth_deg"))
    half_width = _finite(panel.get("panel_half_width_deg"))
    display = panel.get("display_geometry") if isinstance(
        panel.get("display_geometry"), dict
    ) else {}
    outer = _finite(
        display.get("display_outer_radius_m")
        or panel.get("horizontal_outer_radius_m")
    )
    inner = _finite(
        display.get("display_inner_radius_m")
        or panel.get("horizontal_inner_radius_m")
        or 0.0
    )
    if None in (longitude, latitude, azimuth, half_width, outer) or outer <= 0:
        return []
    inner = max(0.0, min(float(inner or 0.0), float(outer)))
    def point(radius, bearing):
        # WGS84 display geometry via the spherical forward-geodesic formula.
        # Distance is in metres throughout; there is no degree-as-metre shortcut.
        earth_radius_m = 6_378_137.0
        angular = float(radius) / earth_radius_m
        angle = math.radians(bearing)
        lat1, lon1 = math.radians(latitude), math.radians(longitude)
        lat = math.asin(
            math.sin(lat1) * math.cos(angular)
            + math.cos(lat1) * math.sin(angular) * math.cos(angle)
        )
        lon = lon1 + math.atan2(
            math.sin(angle) * math.sin(angular) * math.cos(lat1),
            math.cos(angular) - math.sin(lat1) * math.sin(lat),
        )
        lon, lat = math.degrees(lon), math.degrees(lat)
        return [round(float(lon), 9), round(float(lat), 9)]

    start, end = azimuth - half_width, azimuth + half_width
    bearings = [start + (end - start) * index / arc_steps for index in range(arc_steps + 1)]
    ring = [point(float(outer), bearing) for bearing in bearings]
    if inner > 0:
        ring.extend(point(inner, bearing) for bearing in reversed(bearings))
    else:
        ring.append([longitude, latitude])
    ring.append(ring[0])
    return ring


def _radar_selected_layers(ctx, assembly, radar, *, metric_crs):
    """Materialize upstream-selected Radar sites and 90-degree sectors as proposals."""

    panels = [
        item for item in (radar.get("selected_panels") or [])
        if isinstance(item, dict) and str(item.get("radar_type")) == "radar_i"
    ]
    polygons, points_by_tower = [], {}
    for panel in panels:
        ring = _radar_sector_ring(panel, metric_crs)
        if ring:
            polygons.append(ring)
        longitude, latitude = _finite(panel.get("longitude")), _finite(panel.get("latitude"))
        tower_id = str(panel.get("tower_id") or "")
        if longitude is not None and latitude is not None and tower_id:
            points_by_tower.setdefault(tower_id, {
                "longitude": longitude, "latitude": latitude, "tower_id": tower_id,
                "name": _short(panel.get("tower_name") or tower_id),
                "proposal_only": True, "engineering_confirmed": False,
            })
    detail = _cns_frame_detail(assembly, service_key=SERVICE_RADAR, extra={
        "selected_panel_count": len(panels),
        "selected_tower_count": len(points_by_tower),
        "drawn_radar_sectors": len(polygons),
        "proposal_only": True,
        "engineering_confirmed": False,
        "semantics": "selected_radar_i_planning_proposal_not_confirmed_facility",
    })
    layers = []
    if polygons:
        layers.append(_layer(
            ctx, "cns_radar_sector", display_name="Radar-I 90° 规划扇区（未确认）",
            geometry_type=GEOMETRY_POLYGON, source_role="radar_surveillance_layout",
            status=SOURCE_AVAILABLE, reason="", detail=detail,
            feature_count=len(polygons), data={"polygons": polygons},
        ))
    if points_by_tower:
        layers.append(_layer(
            ctx, "cns_radar_proposal", display_name="Radar 规划站址（未确认）",
            geometry_type=GEOMETRY_POINT, source_role="radar_surveillance_layout",
            status=SOURCE_AVAILABLE, reason="", detail=detail,
            feature_count=len(points_by_tower), data={"points": list(points_by_tower.values())},
        ))
    return layers


def _cns_labels(ctx, assembly, services, geometry, *, parameters):
    """CNS 站点标签：同址多业务合并成一条标签，并带上**服务家族列表**。

    ``LabelSpec.services`` 是"同址多业务分块背景"的数据来源：同一坐标上的
    Communication + RID 会得到 ``["communication", "rid"]``，渲染器据此把标签卡的
    背景按服务数**等分**（左半浅绿 / 右半浅橙），顺序固定、可复现。

    服务家族列表由 :func:`cns_planner.application.cns_facility_assembler.assemble`
    的真实记录导出，**不猜测**任何服务：图上没有的服务永远不会出现在标签里。

    Round30-B1.2：与**起降点完全同址**的导航完整性监测点**不再单独出卡**，而是并入
    起降点主卡的第二行（``LabelSpec.secondary_text``）。图层 / 符号 / 图例 / metadata
    全部不变，只是文字表达从"两张卡 + 两条引线"收成"一张卡 + 一条引线"。
    """

    labels = []
    if geometry:
        start_name = _endpoint_label_name(ctx, ctx.route or {}, "start", "start_node_id")
        end_name = _endpoint_label_name(ctx, ctx.route or {}, "end", "end_node_id")
        labels.append(LabelSpec(
            kind="start", text=start_name or "起点",
            longitude=geometry[0][0], latitude=geometry[0][1],
            priority=100, style_key="label_endpoint",
        ))
        labels.append(LabelSpec(
            kind="end", text=end_name or "终点",
            longitude=geometry[-1][0], latitude=geometry[-1][1],
            priority=100, style_key="label_endpoint",
        ))
    if not parameters.get("show_cns_site_labels", True):
        return labels
    records = _cns_label_records(assembly, services)
    if geometry:
        records = _merge_endpoint_colocated_records(labels, records)
    for record, kind, style_key, priority in records:
        labels.append(LabelSpec(
            kind=kind, text=_short(record["text"], 22),
            longitude=record["longitude"], latitude=record["latitude"],
            priority=priority, style_key=style_key, services=list(record["services"]),
            secondary_text=str(record.get("secondary_text") or ""),
            secondary_service=str(record.get("secondary_service") or ""),
        ))
    return labels


def _merge_endpoint_colocated_records(endpoint_labels, records):
    """把与起降点**完全同址**的导航提案并入起终点卡，并从记录表里移除。

    ``endpoint_labels`` 是刚构造好的起终点 :class:`LabelSpec`（起点的经度/纬度就是
    航路首点、终点是末点），``records`` 是 :func:`_cns_label_records` 的结果。

    合并规则（确定性、可单测）：

    * 只有**允许合并的服务家族**（:data:`ENDPOINT_MERGE_SERVICES`，当前 = navigation）
      才参与；通信 / RID 同址仍按既有分色方案独立出卡；
    * 坐标必须在 :data:`ENDPOINT_MERGE_TOLERANCE_DEG` 内**完全一致**，不做任何近似；
    * 合并后：起终点卡的 ``services`` 追加该家族（第二行据此取浅黄底纹），
      ``secondary_text`` 为该家族的服务标签行，被合并的记录从记录表里删除。

    这样**Navigation 提案的业务数据一条都没少**：它只是从一张独立卡片变成了主卡的
    第二行，图层、符号、图例项与 metadata 全部照旧。
    """

    matched = {}
    for label in endpoint_labels:
        if label.longitude is None or label.latitude is None:
            continue
        for index, record in enumerate(records):
            item = record[0]
            if index in matched:
                continue
            families = [family for family in item["services"]
                        if family in ENDPOINT_MERGE_SERVICES]
            if not families:
                continue
            if abs(float(item["longitude"]) - float(label.longitude)) \
                    > ENDPOINT_MERGE_TOLERANCE_DEG:
                continue
            if abs(float(item["latitude"]) - float(label.latitude)) \
                    > ENDPOINT_MERGE_TOLERANCE_DEG:
                continue
            family = families[0]
            line = _ENDPOINT_MERGE_LINE.get(family)
            if not line:
                continue
            label.secondary_service = family
            label.secondary_text = line
            if family not in label.services:
                label.services = _cns_label_service_order([*label.services, family])
            matched[index] = family
    if not matched:
        return records
    return [
        record for index, record in enumerate(records) if index not in matched
    ]


def _cns_label_records(assembly, services):
    """装配结果 → 去重合并后的标签记录（同址同身份同名的多业务合成一条）。

    返回值是 ``(record, kind, style_key, priority)`` 列表，顺序稳定（按坐标、名称排）。
    提案与既有设施**分别**成组：既有设施是已建基线，不能与规划提案共用一张卡片。
    """

    grouped = {}
    for identity, records, kind, style_key, priority in (
        ("proposal", assembly.proposals, "cns_proposal", "label_cns_proposal", 80),
        ("existing", assembly.existing, "cns_existing", "label_cns_facility", 90),
    ):
        for record in records:
            if services and record.service_key not in services:
                continue
            key = (
                identity, round(record.longitude, 9), round(record.latitude, 9),
                record.display_name,
            )
            entry = grouped.setdefault(key, {
                "kind": kind, "style_key": style_key, "priority": priority,
                "longitude": record.longitude, "latitude": record.latitude,
                "name": record.display_name, "services": set(),
            })
            family = _SERVICE_FAMILY.get(record.service_key) or str(record.family or "")
            if family:
                entry["services"].add(family)
    result = []
    for key in sorted(grouped):
        entry = grouped[key]
        families = _cns_label_service_order(entry["services"])
        tags = [_FAMILY_TAG.get(family, "") for family in families]
        # 站名**不在这里截断**：正式图由标签卡自动换行（最多 max_lines 行）展示完整名称，
        # 省略号只在极端超长时才由渲染器的最后手段产生。
        text = str(entry["name"] or "CNS 规划提案").strip() or "CNS 规划提案"
        tags = [tag for tag in tags if tag]
        if tags:
            text = f"{text} /{'/'.join(tags)}"
        result.append((
            {
                "text": text, "longitude": entry["longitude"],
                "latitude": entry["latitude"], "services": families,
            },
            entry["kind"], entry["style_key"], entry["priority"],
        ))
    return result


def _cns_label_service_order(families):
    """服务家族按 :data:`CNS_LABEL_CARD_ORDER` 的固定顺序输出（分块顺序唯一来源）。"""

    values = {str(value) for value in (families or ())}
    ordered = [family for family in CNS_LABEL_CARD_ORDER if family in values]
    ordered.extend(sorted(values - set(ordered)))
    return ordered


def _cns_disclosures(template_id, assembly, services, parameters, radar):
    """CNS 图面披露文本（**FigureSpec.metadata**，不再画进地图主体）。

    Round30-B1：CNS 五图原先在地图左下角画一块大说明框，它会遮住地图内容。本轮改为

    * ``FigureSpec.metadata["disclosures"]``：完整的分条披露（服务半径语义 / 确认状态 /
      Radar 能力限制 / 导航交付赤字 / 综合图配色说明）；
    * ``FigureSpec.metadata["map_disclosure"]``：允许放在**地图框之外**的一句话短披露
      （Radar 图给"当前既有站址与模型约束下无可行布设"）；
    * ``FigureSpec.metadata["layout_profile"]``：版式档位（``cns_five_figure_v2``），
      使 Round30-A 与 Round30-B 的图件在 metadata 中可区分。

    披露内容与以前**逐字一致**：它们只是从图面移到了 metadata，没有删掉任何限制说明。
    """

    confirmation = assembly.evidence.get("confirmation") or {}
    step6 = assembly.evidence.get("step6_gate") or {}
    step6_line = (
        "规划设施来自 P16 proposal_ready；因 P17 连续服务仍为 "
        f"{step6.get('continuous_service_status') or 'unacceptable'}，"
        "方案未通过 Step6 正式确认。"
    )
    entries = []
    map_disclosure = ""
    if template_id == COMMUNICATION_LAYOUT_V1:
        entries.append({
            "disclosure_id": "communication_radius_disclosure",
            "title": "通信规划服务半径",
            "lines": [
                "绿色圆圈为 omnidirectional 规划服务半径 4 km（陆 / 海 / 沿海一致）。",
                "它是 planning service radius，不是实测无线传播等值线。",
                "设施为 P16 规划提案（未确认），未通过 Step6 正式确认。",
            ],
        })
    elif template_id == SURVEILLANCE_LAYOUT_V1 and _surveillance_variant(parameters) == \
            SURVEILLANCE_SERVICE_RID:
        entries.append({
            "disclosure_id": "rid_radius_disclosure",
            "title": "RID surface-aware 规划范围",
            "lines": [
                "RID footprint = 2 km 全向基础区 +（2–5 km 环带 ∩ confirmed sea）。",
                "陆地 / 沿海不确定 / unknown 最大 2 km；仅 confirmed sea 延伸至 5 km。",
                "RID 是合作监视（omnidirectional），不是 Radar，图上没有任何扇区 / panel。",
                "设施为 P16 规划提案（未确认）。",
            ],
        })
    elif template_id == SURVEILLANCE_LAYOUT_V1 and _surveillance_variant(parameters) == \
            SURVEILLANCE_SERVICE_RADAR and radar.get("selected_panel_count"):
        entries.append({
            "disclosure_id": "radar_planning_proposal",
            "title": "Radar 规划提案（未确认）",
            "lines": [
                f"Radar-I 已选 {radar.get('selected_tower_count')} 座规划站址 / "
                f"{radar.get('selected_panel_count')} 个 90° panel；",
                "固定约束：3 km · 90° panel · Radar-II 未启用 · 未放宽 range。",
                "站址与扇区均为 planning proposal，engineering_confirmed=false。",
                "需现场勘察与用户确认后方可形成最终方案。",
            ],
        })
        map_disclosure = "Radar-I：规划提案（未确认），需现场勘察与用户确认"
    elif template_id == SURVEILLANCE_LAYOUT_V1 and _surveillance_variant(parameters) == \
            SURVEILLANCE_SERVICE_RADAR:
        entries.append({
            "disclosure_id": "radar_capability_limitation",
            "title": "Radar-I 能力限制",
            "lines": [
                "Radar-I 在当前既有站址与模型约束下未形成可行布设；",
                "非合作监视能力为已证明的补充能力限制。",
                f"selected_panels={radar.get('selected_panel_count')} · "
                f"gap_reason={radar.get('gap_reason')} · "
                f"gap_classification={radar.get('gap_classification')}",
                "约束：3 km · 90° panel · Radar-II 未启用 · 未放宽 range。",
                "图中不表示任何 Radar 站址、panel 或覆盖扇区。",
            ],
        })
        # 地图框**之外**的一句话披露（能力限制不允许因为删除说明框而消失）。
        map_disclosure = "Radar-I：当前既有站址与模型约束下无可行布设"
    elif template_id == NAVIGATION_LAYOUT_V1:
        entries.append({
            "disclosure_id": "navigation_delivery_deficit",
            "title": "导航完整性监测点",
            "lines": [
                "N005 / N006 为真实起降点上的导航完整性监测点规划提案（未确认）。",
                "monitor placement 已满足规划位置要求；",
                "但 Communication delivery 仍 residual deficit，",
                "因此 endpoint overall 仍为 confirmed_deficit / integrity_monitor_delivery_deficit。",
                "该监测点不是 RTK station、不是 RTK base station、不是 GBAS。",
            ],
        })
    elif template_id == CNS_COMBINED_V1:
        radar_selected = bool(radar.get("selected_panel_count"))
        entries.append({
            "disclosure_id": "cns_combined_disclosure",
            "title": "CNS 综合布设",
            "lines": [
                "绿 = 通信 · 橙 = RID · 黄 = 导航完整性 · 蓝 = Radar。",
                "所有设施均为 existing baseline 或规划提案（未确认）。",
                (
                    f"Radar-I 含 {radar.get('selected_tower_count')} 座规划站址 / "
                    f"{radar.get('selected_panel_count')} 个 90° panel，均未确认。"
                    if radar_selected else
                    "Radar 在 R0005 无选中站址：已证明的补充能力限制，未新增任何 Radar 设施。"
                ),
            ],
        })
        if SERVICE_RADAR in services:
            map_disclosure = (
                "Radar-I：规划提案（未确认）" if radar_selected else
                "Radar-I：当前既有站址与模型约束下无可行布设"
            )
    for entry in entries:
        entry["lines"] = list(entry["lines"]) + [step6_line]
    return entries, map_disclosure


def _surveillance_variant(parameters):
    return str((parameters or {}).get(SURVEILLANCE_SERVICE_PARAMETER) or "")


def materialize_cns(ctx):
    """CNS 专题图（communication / navigation / surveillance / combined）的装配。

    与 ``route_overview_v1`` 共用**同一套**底图原语（海陆、100 m 障碍、既有站址、
    航路与标注），只在设施层面按模板取不同的服务集合；设施全部来自
    :mod:`cns_planner.application.cns_facility_assembler` 这一份 authority。
    """

    template_id = str(ctx.template_id or "")
    parameters = ctx.parameters or {}
    services = _cns_services_for_template(template_id, parameters)
    terrain_threshold = _finite(parameters.get("terrain_threshold_m"), 100.0)
    building_threshold = _finite(parameters.get("building_threshold_m"), 100.0)
    metric_crs = str(parameters.get("extent_source_crs") or DEFAULT_METRIC_CRS)

    assembly = assemble_cns_facilities(
        ctx.state, ctx.route.get("route_id"), extent=ctx.extent, parameters=parameters,
    )
    radar = assembly.evidence.get("radar") or {}

    layers = []
    layers.extend(_polygon_layers(ctx, ctx.extent))
    layers.append(_terrain_obstacle_layer(ctx, ctx.extent, terrain_threshold))
    layers.append(_building_obstacle_layer(ctx, ctx.extent, building_threshold))
    layers.extend(_tower_layers(ctx, ctx.extent, terrain_threshold))

    existing_by_service = {}
    for record in assembly.existing:
        existing_by_service.setdefault(record.service_key, []).append(record)

    # ---- 既有 CNS 设施（真实基线；当前项目范围内为 0 条） -----------------------
    if SERVICE_COMMUNICATION in services:
        existing_records = existing_by_service.get(SERVICE_COMMUNICATION, [])
        if existing_records:
            layers.append(_cns_record_set_layer(
                ctx, "cns_existing", "既有 CNS 设施（已建）", existing_records, assembly,
                service_key=SERVICE_COMMUNICATION,
            ))

    # ---- P16 规划提案 -----------------------------------------------------------
    for service_key in services:
        layer_key, display_name = _PROPOSAL_LAYER_OF_SERVICE.get(service_key, (None, None))
        if layer_key is None:
            continue
        records = [item for item in assembly.proposals if item.service_key == service_key]
        layers.append(_cns_facility_layer(
            ctx, layer_key, display_name, records, assembly, service_key=service_key,
        ))

    # ---- 覆盖 / 能力圆 ----------------------------------------------------------
    if SERVICE_COMMUNICATION in services:
        comm_records = [
            record for record in assembly.proposals if record.service_key == SERVICE_COMMUNICATION
        ]
        radius = coverage_radius_for(
            comm_records[0], surface_class="land",
        ) if comm_records else None
        if comm_records and radius:
            layers.append(_cns_coverage_layer(
                ctx, "cns_coverage_comm", "通信规划服务半径 4 km", comm_records, radius,
                assembly, service_key=SERVICE_COMMUNICATION, metric_crs=metric_crs,
                semantics="communication_omnidirectional_planning_service_radius_4km",
            ))
    rid_footprint_metadata = {}
    if SERVICE_RID in services:
        rid_records = [
            record for record in assembly.proposals if record.service_key == SERVICE_RID
        ]
        if rid_records:
            rid_layers, rid_footprint_metadata = _cns_surface_aware_rid_layers(
                ctx, rid_records, assembly, metric_crs=metric_crs,
            )
            layers.extend(rid_layers)

    # ---- Radar：有 selected panels 时画未确认提案；0 panel 时保持 limitation。------
    if SERVICE_RADAR in services or template_id == CNS_COMBINED_V1:
        context_points, context_detail = _radar_context_points(ctx, assembly, radar)
        if template_id == SURVEILLANCE_LAYOUT_V1 and context_points:
            layers.append(_layer(
                ctx, "cns_radar_context", display_name="Radar-I 评估候选站址（未选中）",
                geometry_type=GEOMETRY_POINT, source_role="radar_surveillance_layout",
                status=SOURCE_AVAILABLE, reason="",
                detail=_cns_frame_detail(assembly, service_key=SERVICE_RADAR,
                                         extra=context_detail),
                feature_count=len(context_points), data={"points": context_points},
            ))
        if radar.get("selected_panel_count"):
            layers.extend(_radar_selected_layers(
                ctx, assembly, radar, metric_crs=metric_crs,
            ))
        else:
            layers.append(_cns_declared_layer(
                ctx, "cns_radar_limitation", "非合作监视能力限制（无可行布设）", assembly,
                service_key=SERVICE_RADAR,
                detail_extra={
                    "algorithm_version": radar.get("algorithm_version"),
                    "status": radar.get("status"),
                    "gap_reason": radar.get("gap_reason"),
                    "gap_classification": radar.get("gap_classification"),
                    "managed_physical_gap": radar.get("managed_physical_gap"),
                    "infeasibility_proven": radar.get("infeasibility_proven"),
                    "selected_panel_count": radar.get("selected_panel_count"),
                    "selected_tower_count": radar.get("selected_tower_count"),
                    "radar_ii_panel_count": radar.get("radar_ii_panel_count"),
                    "candidate_tower_count": radar.get("candidate_tower_count"),
                    "manufactured_radar_sites": 0,
                    "manufactured_radar_panels": 0,
                    "drawn_radar_sectors": 0,
                    "other_route_items_used": False,
                    "declared_only_no_geometry": True,
                    "limitations": deepcopy(radar.get("limitations") or []),
                },
            ))

    route_layers, geometry, turns = _route_layers(ctx)
    layers.extend(route_layers)

    labels = _cns_labels(ctx, assembly, services, geometry, parameters=parameters)
    # 图面说明从"地图主体里的大白框"改为 **FigureSpec.metadata**：地图正文只留给地图，
    # 完整披露（服务半径语义 / 确认状态 / 能力限制）逐字保留在 metadata 里。
    disclosures, map_disclosure = _cns_disclosures(
        template_id, assembly, services, parameters, radar,
    )
    legend_items = _legends(layers, CNS_LEGEND_ORDER)

    source_status, omitted = {}, []
    for layer in layers:
        source_status[layer.layer_key] = {
            "display_name": layer.display_name,
            "source_role": layer.source_role,
            "source_path": layer.source_path,
            "status": layer.source_status,
            "reason": layer.source_reason or None,
            "feature_count": int(layer.feature_count),
            "detail": deepcopy(layer.source_detail),
        }
        if layer.source_status != SOURCE_AVAILABLE:
            omitted.append({
                "layer_key": layer.layer_key, "display_name": layer.display_name,
                "status": layer.source_status,
                "reason": layer.source_reason or "数据不可用",
            })
        elif layer.feature_count <= 0 and layer.geometry_type != GEOMETRY_NONE:
            omitted.append({
                "layer_key": layer.layer_key, "display_name": layer.display_name,
                "status": "empty_within_extent",
                "reason": "数据源可读，但当前图面范围内没有要素",
            })
    warnings = [f"{item['display_name']}：{item['reason']}" for item in omitted]
    warnings.extend(assembly.warnings)
    return MaterializationResult(
        layers=layers, labels=labels, legend_items=legend_items,
        source_status=source_status, omitted_layers=omitted, warnings=warnings,
        # CNS 五图**不再**产生地图内部的说明框（annotations 恒为空）；
        # 完整披露进入 metadata，短披露进入地图框之外的一条薄带。
        annotations=[],
        metadata={
            "layout_profile": CNS_LAYOUT_PROFILE,
            "disclosures": disclosures,
            "map_disclosure": map_disclosure,
            **rid_footprint_metadata,
        },
        boundaries=deepcopy(assembly.boundaries),
    )


# --------------------------------------------------------------------------- 装配主入口

def materialize(ctx: MaterializationContext):
    """装配 ``route_overview_v1`` 的图层 / 标注 / 图例 / 来源状态。

    本模板**不 materialize 适飞空域**（按产品要求：航路周边状况图不表达空域）。
    空域的业务能力、数据源与其它模板的使用不受影响（见 :func:`_airspace_layer`）。
    """

    parameters = ctx.parameters or {}
    terrain_threshold = _finite(parameters.get("terrain_threshold_m"), 100.0)
    building_threshold = _finite(parameters.get("building_threshold_m"), 100.0)
    layers = []
    layers.extend(_polygon_layers(ctx, ctx.extent))
    layers.append(_terrain_obstacle_layer(ctx, ctx.extent, terrain_threshold))
    layers.append(_building_obstacle_layer(ctx, ctx.extent, building_threshold))
    layers.extend(_tower_layers(ctx, ctx.extent, terrain_threshold))
    layers.extend(_airport_layers(ctx, ctx.extent))
    route_layers, geometry, turns = _route_layers(ctx)
    layers.extend(route_layers)

    labels = _labels(ctx, geometry, turns, layers)
    legend_items = _legends(layers)

    source_status, omitted = {}, []
    for layer in layers:
        source_status[layer.layer_key] = {
            "display_name": layer.display_name,
            "source_role": layer.source_role,
            "source_path": layer.source_path,
            "status": layer.source_status,
            "reason": layer.source_reason or None,
            "feature_count": int(layer.feature_count),
            "detail": deepcopy(layer.source_detail),
        }
        if layer.source_status != SOURCE_AVAILABLE:
            omitted.append({
                "layer_key": layer.layer_key, "display_name": layer.display_name,
                "status": layer.source_status,
                "reason": layer.source_reason or "数据不可用",
            })
        elif layer.feature_count <= 0 and layer.geometry_type != GEOMETRY_NONE:
            omitted.append({
                "layer_key": layer.layer_key, "display_name": layer.display_name,
                "status": "empty_within_extent",
                "reason": "数据源可读，但当前图面范围内没有要素",
            })
    warnings = [f"{item['display_name']}：{item['reason']}" for item in omitted]
    return MaterializationResult(
        layers=layers, labels=labels, legend_items=legend_items,
        source_status=source_status, omitted_layers=omitted, warnings=warnings,
        boundaries={
            "presentation_only": True,
            "writes_operational_routes_or_cns": False,
            "recomputes_business_results": False,
            "unknown_is_not_zero": True,
            "missing_is_not_empty": True,
        },
    )


#: 使用 CNS 装配路径的模板（其余一律走 :func:`materialize`）。
CNS_TEMPLATE_DISPATCH = (
    COMMUNICATION_LAYOUT_V1, NAVIGATION_LAYOUT_V1, SURVEILLANCE_LAYOUT_V1, CNS_COMBINED_V1,
)


def materialize_for_template(ctx: MaterializationContext):
    """按 ``ctx.template_id`` 分派到对应装配路径（**唯一**分派点）。"""

    template_id = str(ctx.template_id or ROUTE_OVERVIEW_V1)
    if template_id in CNS_TEMPLATE_DISPATCH:
        return materialize_cns(ctx)
    return materialize(ctx)


# =========================================================================== 产物存储

class MapFigureStore:
    """受控专题图产物目录。

    布局（route-first，见 :data:`MAP_FIGURE_ROUTES_DIRECTORY`）::

        <active project>/artifacts/map_figures/
          index.json                                   # 跨 route 的总索引（保留）
          routes/<sanitized_route_id>/
            index.json                                 # route 级索引（人工查找 / 按航路浏览）
            <template_id>/<figure_id>/
              figure.png  figure_spec.json  metadata.json

    旧布局 ``artifacts/map_figures/<figure_id>/`` 继续**只读兼容**：已有历史产物原地保留，
    不自动搬迁、不删除；读取时新结构优先、旧结构兜底。

    安全契约：**只有服务端**能决定路径；客户端提供的任何字符串都不参与路径拼接。
    图号必须匹配 ``MF-<32 hex>``；航路目录名由 :meth:`sanitize_route_id` 从 route_id
    派生（清洗 + 稳定摘要），模板目录名必须是白名单字符集，因此客户端无法注入任意路径。
    """

    def __init__(self, project_directory):
        self.root = Path(project_directory).resolve()
        self.directory = self.root / MAP_FIGURE_DIRECTORY

    # ---- 路径 -----------------------------------------------------------------

    @staticmethod
    def valid_figure_id(figure_id):
        return bool(_FIGURE_ID_PATTERN.fullmatch(str(figure_id or "").strip()))

    @staticmethod
    def sanitize_route_id(route_id):
        """把任意 ``route_id`` 转成**安全且稳定**的目录名（服务端唯一决定）。

        规则：非 ``[0-9A-Za-z_-]`` 的字符（含路径分隔符、``..``、中文标点等）一律替换为
        ``_``，再截断到 :data:`MAX_ROUTE_DIRECTORY_PREFIX`，最后拼接 route_id 原文的
        sha256 前 8 位。因此：

        * 结果只含安全字符，不可能逃出受控目录；
        * 结果对同一个 route_id 稳定（同一航路的多张图永远进同一个目录）；
        * 清洗后同名的不同 route_id 也不会碰撞（摘要不同）。
        """

        text = str(route_id or "").strip()
        prefix = _UNSAFE_ROUTE_CHARS.sub("_", text).strip("._-")
        if len(prefix) > MAX_ROUTE_DIRECTORY_PREFIX:
            prefix = prefix[:MAX_ROUTE_DIRECTORY_PREFIX].strip("._-")
        if not prefix:
            prefix = "route"
        digest = sha256(text.encode("utf-8")).hexdigest()[:8]
        return f"{prefix}-{digest}"

    @property
    def routes_directory(self):
        return self.directory / MAP_FIGURE_ROUTES_DIRECTORY

    def _contained(self, candidate, parent, message):
        target = Path(candidate).resolve()
        try:
            target.relative_to(Path(parent).resolve())
        except ValueError as exc:
            raise MapFigurePathDenied(message) from exc
        return target

    def route_directory(self, route_id, *, create=False):
        """``routes/<sanitized_route_id>/``（目录名由服务端从 route_id 派生）。"""

        name = self.sanitize_route_id(route_id)
        target = self._contained(
            self.routes_directory / name, self.routes_directory,
            "专题图航路目录超出受控目录，已拒绝访问",
        )
        if create:
            target.mkdir(parents=True, exist_ok=True)
        return target

    def template_directory(self, route_id, template_id, *, create=False):
        """``routes/<route>/<template_id>/``（模板 id 必须匹配白名单）。"""

        text = str(template_id or "").strip()
        if not _TEMPLATE_ID_PATTERN.fullmatch(text):
            raise MapFigurePathDenied("专题图模板目录名无效，已拒绝访问")
        base = self.route_directory(route_id, create=create)
        target = self._contained(base / text, base, "专题图模板目录超出受控目录，已拒绝访问")
        if create:
            target.mkdir(parents=True, exist_ok=True)
        return target

    def figure_directory(self, figure_id, *, route_id=None, template_id=None, create=False):
        """图件产物目录。

        给了 ``route_id`` 与 ``template_id`` 时使用 route-first 结构；否则使用旧的
        平铺结构（只读兼容路径，供历史调用点与既有测试使用）。
        """

        if not self.valid_figure_id(figure_id):
            raise MapFigurePathDenied("专题图编号无效，已拒绝访问")
        if route_id and template_id:
            base = self.template_directory(route_id, template_id, create=create)
        else:
            base = self.directory
        target = self._contained(
            base / str(figure_id), base, "专题图路径超出受控目录，已拒绝访问",
        )
        if create:
            target.mkdir(parents=True, exist_ok=True)
        return target

    def locate_directory(self, figure_id):
        """找到图件目录：**新结构优先**，旧平铺结构兜底；找不到返回 ``None``。"""

        if not self.valid_figure_id(figure_id):
            return None
        if self.routes_directory.is_dir():
            for candidate in self.routes_directory.glob(f"*/*/{figure_id}"):
                if candidate.is_dir():
                    return candidate.resolve()
        flat = self.directory / str(figure_id)
        if flat.is_dir():
            return flat.resolve()
        return None

    def artifact_path(self, figure_id, name):
        """受控目录内的白名单文件名（``name`` 只允许单层、无分隔符）。

        未指定布局时按**旧平铺结构**解析（历史产物读取路径）；新结构的读取请用
        :meth:`artifact_path_in` 配合 :meth:`figure_directory`。
        """

        return self.artifact_path_in(self.figure_directory(figure_id), name)

    def artifact_path_in(self, directory, name):
        if not name or "/" in str(name) or "\\" in str(name) or str(name).startswith("."):
            raise MapFigurePathDenied("专题图产物文件名无效，已拒绝访问")
        base = Path(directory).resolve()
        return self._contained(
            base / str(name), base, "专题图产物路径超出受控目录，已拒绝访问",
        )

    def relative_path(self, path):
        """受控目录内路径 → 相对 active project 的 POSIX 相对路径（记录里只用它）。"""

        return Path(path).resolve().relative_to(self.root).as_posix()

    # ---- 读写 -----------------------------------------------------------------

    def write(self, figure_id, *, image_bytes, spec_payload, record,
              route_id=None, template_id=None):
        """一次性写出 PNG / FigureSpec / metadata.json，并返回**完整记录**。

        code review 修复项：以前先把 record 写进 metadata.json，之后才往 index 里的
        记录补 ``image_sha256`` / ``relative_path`` / ``artifact_ref``，于是
        metadata.json 与 index.json 的审计事实不一致。现在先确定全部产物事实
        （摘要、相对路径、artifact 引用），形成**最终完整记录**，再落三份文件：

            metadata.json 里的记录 == index.json 里对应记录的固定审计字段

        index 允许额外存在动态投影字段（``current_applicability``），但产物本身的
        固定审计事实（见 :data:`MAP_FIGURE_AUDIT_FIELDS`）必须逐字段相同。
        """

        directory = self.figure_directory(
            figure_id, route_id=route_id, template_id=template_id, create=True,
        )
        image_path = directory / "figure.png"
        spec_path = directory / "figure_spec.json"
        record_path = directory / "metadata.json"
        image_digest = sha256(image_bytes).hexdigest()
        relative_image = self.relative_path(image_path)
        complete = {
            **deepcopy(record),
            "figure_id": str(figure_id),
            "relative_path": relative_image,
            "spec_relative_path": self.relative_path(spec_path),
            "record_relative_path": self.relative_path(record_path),
            "image_bytes": len(image_bytes),
            "image_sha256": image_digest,
            "artifact_ref": {
                "artifact_id": image_digest,
                "relative_path": relative_image,
                "figure_id": str(figure_id),
                "content_type": "image/png",
            },
        }
        if route_id and template_id:
            complete["route_directory"] = self.sanitize_route_id(route_id)
        _atomic_write(image_path, image_bytes)
        _atomic_write(spec_path, json.dumps(
            spec_payload, ensure_ascii=False, indent=2, allow_nan=False,
        ).encode("utf-8"))
        # metadata.json **最后**写：此时 record 已经包含全部固定审计事实。
        _atomic_write(record_path, json.dumps(
            complete, ensure_ascii=False, indent=2, allow_nan=False,
        ).encode("utf-8"))
        return complete

    def read_image(self, figure_id, record=None):
        return self._read(figure_id, "figure.png", "专题图文件缺失；请重新生成该图", record)

    def read_spec(self, figure_id, record=None):
        return self._read(figure_id, "figure_spec.json", "专题图规格文件缺失；请重新生成该图",
                          record)

    def _directory_for(self, figure_id, record=None):
        """按记录里的相对路径定位（校验仍在受控目录内），否则自动查找。"""

        candidates = []
        if isinstance(record, dict) and record.get("relative_path"):
            recorded = Path(str(record["relative_path"]))
            if not recorded.is_absolute():
                candidates.append(self.root / recorded.parent)
        located = self.locate_directory(figure_id)
        if located is not None:
            candidates.append(located)
        for candidate in candidates:
            try:
                resolved = self._contained(
                    candidate, self.directory, "专题图路径超出受控目录，已拒绝访问",
                )
            except MapFigurePathDenied:
                continue
            if resolved.is_dir():
                return resolved
        return None

    def _read(self, figure_id, name, missing_text, record=None):
        if not self.valid_figure_id(figure_id):
            raise MapFigurePathDenied("专题图编号无效，已拒绝访问")
        directory = self._directory_for(figure_id, record)
        if directory is None:
            raise MapFigureNotFound(missing_text)
        path = self.artifact_path_in(directory, name)
        if not path.is_file():
            raise MapFigureNotFound(missing_text)
        return path.read_bytes()

    def exists(self, figure_id, record=None):
        if not self.valid_figure_id(figure_id):
            return False
        try:
            directory = self._directory_for(figure_id, record)
            if directory is None:
                return False
            return self.artifact_path_in(directory, "figure.png").is_file()
        except MapFigureError:
            return False

    def list_relative(self):
        """只读盘点**旧平铺结构**下的图号目录清单（不打开任何产物）。"""

        if not self.directory.is_dir():
            return []
        return sorted(
            item.name for item in self.directory.iterdir()
            if item.is_dir() and self.valid_figure_id(item.name)
        )

    # ---- 图件索引（不进入 ProjectState，也不推进业务 revision） -------------------

    @property
    def index_path(self):
        """跨 route 的总索引（``artifacts/map_figures/index.json``）。"""

        return self.directory / MAP_FIGURE_INDEX_NAME

    def read_index(self):
        """读取图件索引；损坏时**如实失败**，绝不静默重建成空索引。"""

        path = self.index_path
        if not path.is_file():
            return {"schema_version": 1, "items": [], "active_figure_id": None}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise MapFigureNotFound(
                "专题图索引文件损坏；请重新生成专题图（原索引不会被自动覆盖）"
            ) from exc
        if not isinstance(payload, dict):
            raise MapFigureNotFound("专题图索引文件格式无效；请重新生成专题图")
        items = [item for item in (payload.get("items") or []) if isinstance(item, dict)]
        return {
            "schema_version": int(payload.get("schema_version") or 1),
            "items": items,
            "active_figure_id": payload.get("active_figure_id"),
        }

    def write_index(self, items, active_figure_id):
        """原子写入图件索引（只写制图模块自己的受控文件）。"""

        payload = {
            "schema_version": 1,
            "active_figure_id": active_figure_id,
            "count": len(items),
            "items": list(items),
        }
        _atomic_write(self.index_path, json.dumps(
            payload, ensure_ascii=False, indent=2, allow_nan=False,
        ).encode("utf-8"))

    # ---- route 级索引（人工查找 / 后续按航路浏览） --------------------------------

    def route_index_path(self, route_id):
        return self.route_directory(route_id) / MAP_FIGURE_INDEX_NAME

    def read_route_index(self, route_id):
        """读取 route 级索引；缺失返回空骨架，损坏则**如实失败**（与总索引一致）。"""

        path = self.route_index_path(route_id)
        empty = {
            "schema_version": 1,
            "route_id": str(route_id or ""),
            "sanitized_route_id": self.sanitize_route_id(route_id),
            "templates": {},
        }
        if not path.is_file():
            return empty
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise MapFigureNotFound(
                "航路专题图索引文件损坏；请重新生成专题图（原索引不会被自动覆盖）"
            ) from exc
        if not isinstance(payload, dict):
            raise MapFigureNotFound("航路专题图索引文件格式无效；请重新生成专题图")
        templates = payload.get("templates")
        return {
            "schema_version": int(payload.get("schema_version") or 1),
            "route_id": str(payload.get("route_id") or empty["route_id"]),
            "sanitized_route_id": str(
                payload.get("sanitized_route_id") or empty["sanitized_route_id"]
            ),
            "templates": {
                str(key): value for key, value in (templates or {}).items()
                if isinstance(value, dict)
            },
        }

    def write_route_index(self, route_id, templates):
        """原子写入 route 级索引：每个模板一个 active 图号 + 该模板的图件清单。"""

        directory = self.route_directory(route_id, create=True)
        payload = {
            "schema_version": 1,
            "route_id": str(route_id or ""),
            "sanitized_route_id": self.sanitize_route_id(route_id),
            "count": sum(
                len(entry.get("items") or []) for entry in (templates or {}).values()
                if isinstance(entry, dict)
            ),
            "templates": deepcopy(templates or {}),
        }
        _atomic_write(directory / MAP_FIGURE_INDEX_NAME, json.dumps(
            payload, ensure_ascii=False, indent=2, allow_nan=False,
        ).encode("utf-8"))
        return payload


def _atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


# =========================================================================== 应用服务

class MapFigureService:
    """专题成果图的业务编排：选择航路 → FigureSpec → QGIS 制图 → 受控产物 + 记录。

    ``qgis_call`` 是**唯一**允许接触 QGIS 对象的通道（应用上下文注入）；本服务本身
    不 import QGIS，因此纯逻辑部分可以在没有 QGIS 的解释器里被单元测试覆盖。
    """

    def __init__(self, session, paths_provider, qgis_call, *, renderer_factory=None,
                 project_directory=None, clock=utc_now):
        self.session = session
        self.paths_provider = paths_provider
        self.qgis_call = qgis_call
        self.renderer_factory = renderer_factory or _default_renderer_factory
        self.project_directory = Path(
            project_directory or Path(session.store_path).parent
        ).resolve()
        self.store = MapFigureStore(self.project_directory)
        self.clock = clock
        self._preview_cache = {}
        self._preview_cache_order = []

    # ---- 只读：模板目录与可用航路 ---------------------------------------------

    def catalog(self):
        payload = template_catalog()
        payload["route_options"] = self.route_options()
        payload["route_selection"] = self.route_selection_state()
        return payload

    def routes(self):
        return (self.session.state.get(OPERATIONAL_ROUTE_SOURCE) or [])

    def available_routes(self):
        """可制图的权威运行航路（含几何完整性判定，缺失即为不可用而不是空几何）。"""

        items = []
        for route in self.routes():
            if not isinstance(route, dict):
                continue
            path = route.get("path") or []
            items.append({
                "route_id": str(route.get("route_id") or ""),
                "route_source": OPERATIONAL_ROUTE_SOURCE,
                "kind": route.get("kind"),
                "status": route.get("status"),
                "path_crs": route.get("path_crs") or DEFAULT_CRS,
                "point_count": len(path) if isinstance(path, list) else 0,
                "plottable": bool(isinstance(path, list) and len(path) >= 2),
                "start": route.get("start"),
                "end": route.get("end"),
            })
        items.sort(key=lambda item: item["route_id"])
        return items

    def route_options(self):
        return [item for item in self.available_routes() if item["plottable"]]

    def route_selection_state(self):
        options = self.route_options()
        return {
            "count": len(options),
            "auto_selectable": len(options) == 1,
            "requires_explicit_route_id": len(options) != 1,
            "unplottable": [
                item for item in self.available_routes() if not item["plottable"]
            ],
        }

    # ---- 只读：已生成图件 -----------------------------------------------------

    def records(self):
        """图件记录（受控索引为主，旧版 state 容器只读兼容）+ **只读**适用性投影。

        ``current_applicability`` 是**每次调用现算**的投影，不写回索引：

        * 图件的 ``project_revision`` 与当前 ``session.state["revision"]`` 不一致
          → ``stale_revision``（图件来源于旧项目 revision，必须重新生成）；
        * revision 一致且是当前 active 图件 → ``current``；
        * revision 一致但不是 active → ``superseded`` / ``inactive``（按已存语义稳定处理）。

        **禁止**：改 index、``session.save()``、推进业务 revision。本方法只读。
        """

        index = self.store.read_index()
        items = index["items"]
        active = index["active_figure_id"]
        legacy = self._legacy_records()
        known = {str(item.get("figure_id")) for item in items}
        merged = list(items) + [
            item for item in legacy["items"] if str(item.get("figure_id")) not in known
        ]
        if active is None:
            active = legacy["active_figure_id"]
        return {
            "schema_version": 1,
            "items": self._project_applicability(merged, active),
            "active_figure_id": active,
            "count": len(merged),
            "project_revision": self._current_revision(),
        }

    def _current_revision(self):
        """当前业务 revision（只读；缺失即 0，不推断、不写入）。"""

        return int(self.session.state.get("revision") or 0)

    def _project_applicability(self, items, active_figure_id):
        """把适用性投影到记录副本上（不触碰磁盘上的索引）。"""

        revision = self._current_revision()
        projected = []
        for item in items:
            entry = deepcopy(item)
            stored = str(entry.get("current_applicability") or "")
            try:
                item_revision = int(entry.get("project_revision"))
            except (TypeError, ValueError):
                item_revision = None
            if item_revision != revision:
                # revision 不一致（含缺失 project_revision）：一律是旧 revision 的图件。
                entry["current_applicability"] = "stale_revision"
            elif str(entry.get("figure_id")) == str(active_figure_id):
                entry["current_applicability"] = "current"
            elif stored in ("current", "superseded"):
                entry["current_applicability"] = "superseded"
            else:
                entry["current_applicability"] = stored or "inactive"
            projected.append(entry)
        return projected

    def _legacy_records(self):
        """只读兼容：早期版本写在 ``state["map_figures"]`` 里的记录。"""

        collection = self.session.state.get(MAP_FIGURE_COLLECTION)
        if not isinstance(collection, dict):
            return {"items": [], "active_figure_id": None}
        items = [item for item in (collection.get("items") or []) if isinstance(item, dict)]
        return {"items": items, "active_figure_id": collection.get("active_figure_id")}

    def _record(self, figure_id):
        for item in self.records()["items"]:
            if isinstance(item, dict) and str(item.get("figure_id")) == str(figure_id):
                return item
        return None

    def figures_snapshot(self):
        """给前端的只读投影：模板目录 + 图件记录（含动态适用性）。

        这是 ``GET /api/map-figures/state`` 的唯一数据来源；它**不**把图件索引写回
        workflow state，因此读取它不会改变任何业务 revision。
        """

        return {
            "catalog": self.catalog(),
            "records": self.records(),
        }

    def artifact(self, figure_id, kind="png"):
        """读取已发布产物：图片字节 + 记录（按记录做白名单校验）。"""

        record = self._record(figure_id)
        if record is None:
            raise MapFigureNotFound("专题图记录不存在；请先生成该图")
        wanted = str(kind or "png").lower()
        if wanted == "spec":
            return {
                "image": self.store.read_spec(figure_id, record),
                "record": deepcopy(record),
                "content_type": "application/json; charset=utf-8",
                "filename": f"{figure_id}.spec.json",
            }
        if wanted not in ("png", ""):
            raise MapFigureFormatUnsupported(
                f"暂不支持读取 {kind} 产物；本轮仅支持 PNG 与规格 JSON"
            )
        return {
            "image": self.store.read_image(figure_id, record),
            "record": deepcopy(record),
            "content_type": "image/png",
            "filename": f"{figure_id}.png",
        }

    # ---- 生成 -----------------------------------------------------------------

    def build_figure(self, *, template_id, route_id=None, parameter_overrides=None,
                     dpi=None, format_name="png"):
        """构建 FigureSpec（只读；不写 state、不创建 QGIS 对象）。"""

        template = template_definition(template_id)
        if template is None:
            raise MapFigureTemplateUnavailable(f"未知的专题图模板：{template_id}")
        if not is_available(template_id):
            raise MapFigureTemplateUnavailable(
                f"模板「{template['display_name']}」尚未实现（状态：{template['status']}）；"
                "本轮只提供类型预留，不会生成占位结果"
            )
        if str(format_name or "png").lower() not in SUPPORTED_FORMATS:
            raise MapFigureFormatUnsupported(
                f"暂不支持输出格式 {format_name}；本轮仅支持 "
                + "、".join(SUPPORTED_FORMATS)
            )
        try:
            parameters = template_parameters(template_id, parameter_overrides)
        except MapFigureParameterInvalid as exc:
            raise MapFigureParameterError(
                str(exc), detail={"parameter": exc.parameter, "reason": exc.reason},
            ) from exc
        _require_template_parameters(template_id, parameters, parameter_overrides)
        route = self.select_route(route_id)
        revision = int(self.session.state.get("revision") or 0)
        paths = dict(self.paths_provider() or {})
        # 先用与版面无关的临时范围装配（只用于取图层与图例条目），
        # 再据实际图例条目定版面，最后按版面长宽比重算范围并重装配一次。
        layout = _layout_plan(parameters)
        extent, extent_evidence = _expand_extent(
            route.get("path") or [],
            buffer_km=parameters.get("extent_buffer_km", 10.0),
            max_padding_km=parameters.get("extent_max_padding_km", 30.0),
            aspect=layout["map_width_mm"] / layout["map_height_mm"],
            metric_crs=parameters.get("extent_source_crs") or DEFAULT_METRIC_CRS,
        )
        if extent is None:
            raise MapFigureRouteError(
                f"航路 {route.get('route_id')} 的几何不足以计算图面范围（需要至少 2 个顶点）",
                detail=extent_evidence,
            )
        probe = materialize_for_template(MaterializationContext(
            state=self.session.state, paths=paths, route=route, extent=extent,
            parameters=parameters, project_revision=revision,
            source_audits=self.session.state.get("source_audits") or {},
            template_id=template["template_id"],
        ))
        layout = _layout_plan(
            parameters, legend_items=_legend_layout_entries(probe.legend_items),
            layout_disclosure=str((probe.metadata or {}).get("map_disclosure") or ""),
        )
        aspect = layout["map_width_mm"] / layout["map_height_mm"]
        extent, extent_evidence = _expand_extent(
            route.get("path") or [],
            buffer_km=parameters.get("extent_buffer_km", 10.0),
            max_padding_km=parameters.get("extent_max_padding_km", 30.0),
            aspect=aspect, metric_crs=parameters.get("extent_source_crs") or DEFAULT_METRIC_CRS,
        )
        if extent is None:
            raise MapFigureRouteError(
                f"航路 {route.get('route_id')} 的几何不足以计算图面范围（需要至少 2 个顶点）",
                detail=extent_evidence,
            )
        extent_evidence = {
            **extent_evidence,
            "layout_profile": layout.get("layout_profile"),
            "extent_policy_id": parameters.get("extent_policy_id"),
            "extent_aspect_locked": bool(parameters.get("extent_aspect_locked")),
            "layout_map_width_mm": layout.get("map_width_mm"),
            "layout_map_height_mm": layout.get("map_height_mm"),
            "aspect_used": aspect,
            "cns_five_figure_shared_extent": bool(
                parameters.get("layout_profile") == CNS_LAYOUT_PROFILE
            ),
        }
        result = materialize_for_template(MaterializationContext(
            state=self.session.state, paths=paths, route=route, extent=extent,
            parameters=parameters, project_revision=revision,
            source_audits=self.session.state.get("source_audits") or {},
            surface_classification_policy=(
                self.session.state.get("surface_classification_policy") or {}
            ),
            template_id=template["template_id"],
        ))
        # 第二次装配可能带来不同的 map_disclosure（图层/图例已定稿），这里同步一次。
        # 版面依赖"是否真的有披露句"与审计条开关，因此带披露句**二次规划**一次版面，
        # 保证图例能利用页面底部剩余空间（地图框高度固定，不受影响）。
        _sync_layout_after_disclosure(layout, parameters, result)
        aspect = layout["map_width_mm"] / layout["map_height_mm"]
        extent, extent_evidence = _expand_extent(
            route.get("path") or [],
            buffer_km=parameters.get("extent_buffer_km", 10.0),
            max_padding_km=parameters.get("extent_max_padding_km", 30.0),
            aspect=aspect, metric_crs=parameters.get("extent_source_crs") or DEFAULT_METRIC_CRS,
        )
        if extent is None:
            raise MapFigureRouteError(
                f"航路 {route.get('route_id')} 的几何不足以计算图面范围（需要至少 2 个顶点）",
                detail=extent_evidence,
            )
        extent_evidence = {
            **extent_evidence,
            "layout_profile": layout.get("layout_profile"),
            "extent_policy_id": parameters.get("extent_policy_id"),
            "extent_aspect_locked": bool(parameters.get("extent_aspect_locked")),
            "layout_map_width_mm": layout.get("map_width_mm"),
            "layout_map_height_mm": layout.get("map_height_mm"),
            "aspect_used": aspect,
            "cns_five_figure_shared_extent": bool(
                parameters.get("layout_profile") == CNS_LAYOUT_PROFILE
            ),
        }
        extent_evidence = _annotate_extent_evidence(extent_evidence, result, extent, parameters)
        path_points = [
            list(point) for point in (route.get("path") or [])
            if isinstance(point, (list, tuple)) and len(point) >= 2
        ]
        subtitle = _figure_subtitle(self.session.state, route.get("route_id"))
        spec = FigureSpec(
            template_id=template["template_id"],
            template_version=int(template["template_version"]),
            title=_figure_title(
                template["display_name"], route, template["template_id"], parameters,
            ),
            subtitle=subtitle,
            route_id=str(route.get("route_id") or ""),
            route_source=OPERATIONAL_ROUTE_SOURCE,
            route_crs=str(route.get("path_crs") or DEFAULT_CRS),
            route_geometry=deepcopy(route.get("path") or []),
            route_start=deepcopy(route.get("start") or (path_points[0] if path_points else [])),
            route_end=deepcopy(route.get("end") or (path_points[-1] if path_points else [])),
            turn_points=path_points[1:-1],
            extent=extent,
            extent_mode="route_bbox_plus_configurable_buffer_metric",
            extent_evidence=extent_evidence,
            layers=result.layers,
            labels=result.labels,
            annotations=result.annotations,
            legend_items=result.legend_items,
            source_status=result.source_status,
            omitted_layers=result.omitted_layers,
            generated_from_revision=revision,
            display_thresholds={
                "terrain_threshold_m": parameters.get("terrain_threshold_m"),
                "building_threshold_m": parameters.get("building_threshold_m"),
                "basis": parameters.get("terrain_threshold_basis"),
                "semantics": "figure_display_threshold_only",
                "affects_planning_constraint_field": False,
                "affects_route_safety_decision": False,
                "is_business_gate": False,
            },
            label_policy={
                "language": "zh-CN",
                "halo": "white_buffer",
                "start_end_always": True,
                "turn_point_label_limit": 6,
                "place_labels_from_data_only": True,
                "priority_order": [
                    "start_end", "selected_cns_proposal", "existing_cns_facility",
                    "tower_context", "ordinary_context",
                ],
                "tower_labels_are_suppressed_not_relocated": True,
                # Round30-B1：标签带**正式浅色背景卡**；同址多业务按服务数等分分块。
                "label_background_card": True,
                "colocated_multi_service_split_background": True,
                "label_service_order": list(CNS_LABEL_CARD_ORDER),
                "start_end_never_suppressed": True,
                "selected_proposal_suppresses_ordinary_labels": True,
            },
            layout=layout,
            parameters=deepcopy(parameters),
            warnings=list(result.warnings),
            metadata={
                # 版面档位 + 完整图面披露（Round30-A 与 Round30-B 的图件据此可区分）。
                "layout_profile": layout.get("layout_profile"),
                "layout_profile_version": _layout_profile_version(layout.get("layout_profile")),
                "layout_engine": "cns_canonical_figurespec_to_qgis_printlayout",
                "map_crs": layout.get("map_crs"),
                "grid_crs": layout.get("grid_crs"),
                "map_disclosure": layout.get("map_disclosure") or "",
                "map_internal_annotations": False,
                "disclosures": deepcopy((result.metadata or {}).get("disclosures") or []),
                **({
                    "surface_aware_visualization": True,
                    "affects_planning": False,
                    "rid_footprint_semantics": (
                        (result.metadata or {}).get("rid_footprint_semantics")
                    ),
                    "rid_extension_polygon_count": (
                        (result.metadata or {}).get("rid_extension_polygon_count")
                    ),
                    "rid_extension_outer_boundary_line_count": (
                        (result.metadata or {}).get("rid_extension_outer_boundary_line_count")
                    ),
                    "rid_extension_fail_closed": (
                        (result.metadata or {}).get("rid_extension_fail_closed")
                    ),
                } if (result.metadata or {}).get("surface_aware_visualization") else {}),
            },
            boundaries={
                "presentation_only": True,
                "writes_operational_routes_or_cns": False,
                "recomputes_business_results": False,
                "unknown_is_not_zero": True,
                "missing_is_not_empty": True,
                "writes_state": False,
                "advances_project_revision": False,
                **deepcopy(result.boundaries or {}),
            },
        )
        return spec

    def select_route(self, route_id=None):
        """航路选择：显式 id 优先；不传时只有**唯一**可绘图航路可自动选择。"""

        options = self.route_options()
        wanted = str(route_id or "").strip()
        if wanted:
            for item in options:
                if item["route_id"] == wanted:
                    return self._route_record(wanted)
            all_routes = self.available_routes()
            if any(item["route_id"] == wanted for item in all_routes):
                raise MapFigureRouteError(
                    f"运行航路 {wanted} 的几何顶点不足，无法制图",
                    code="map_figure_route_geometry_incomplete",
                )
            raise MapFigureRouteError(f"运行航路 {wanted} 不存在")
        if not options:
            if self.routes():
                raise MapFigureRouteError(
                    "当前项目的运行航路都没有可用于制图的几何；请先完成 Step03 发布",
                    code="map_figure_route_geometry_incomplete",
                )
            raise MapFigureRouteError(
                "当前项目还没有权威运行航路（operational_routes）；"
                "请先在 Step03 完成航路验证、采纳与发布",
                code="map_figure_route_missing",
            )
        if len(options) == 1:
            return self._route_record(options[0]["route_id"])
        raise MapFigureRouteSelectionRequired(
            "当前项目有 %d 条可制图的运行航路，请显式选择 route_id：%s" % (
                len(options), "、".join(item["route_id"] for item in options),
            ),
            detail={"route_ids": [item["route_id"] for item in options]},
        )

    def _route_record(self, route_id):
        for route in self.routes():
            if isinstance(route, dict) and str(route.get("route_id")) == str(route_id):
                return route
        raise MapFigureRouteError(f"运行航路 {route_id} 不存在")

    # ---- 渲染 -----------------------------------------------------------------

    def render_preview(self, *, template_id, route_id=None, parameter_overrides=None,
                       width_px=None, format_name="png"):
        spec = self.build_figure(
            template_id=template_id, route_id=route_id,
            parameter_overrides=parameter_overrides, format_name=format_name,
        )
        layout = spec.layout
        parameters = spec.parameters
        px_per_mm = _finite(parameters.get("preview_px_per_mm"), 3.0)
        width = _preview_width(width_px, layout, px_per_mm)
        width = _bounded_preview_width(width, layout)
        dpi = width / (layout["document_width_mm"] / 25.4)
        cache_key = (spec.fingerprint(), int(width))
        cached = self._preview_cache.get(cache_key)
        if cached is not None:
            return {"image": cached, "spec": spec, "record": None, "reused": True}
        image = self._render(spec, dpi=float(dpi))
        self._remember_preview(cache_key, image)
        return {"image": image, "spec": spec, "record": None, "reused": False}

    def export(self, *, template_id, route_id=None, format_name="png", dpi=None,
               parameter_overrides=None):
        """生成正式产物（300 DPI 默认）并登记记录；幂等：相同内容复用同一图号。"""

        spec = self.build_figure(
            template_id=template_id, route_id=route_id,
            parameter_overrides=parameter_overrides, format_name=format_name,
        )
        parameters = spec.parameters
        export_dpi = _finite(dpi, None) or _finite(parameters.get("export_dpi"), 300.0)
        if not (MIN_EXPORT_DPI <= export_dpi <= MAX_EXPORT_DPI):
            raise MapFigureFormatUnsupported(
                f"导出 DPI {export_dpi} 超出允许范围 "
                f"（{MIN_EXPORT_DPI:g} ~ {MAX_EXPORT_DPI:g}）"
            )
        # 真正的总像素上限：宽 × 高（A4 竖版 600 dpi ≈ 34.8 MP）。
        width_px = float(spec.layout["document_width_mm"]) / 25.4 * export_dpi
        height_px = float(spec.layout["document_height_mm"]) / 25.4 * export_dpi
        if width_px * height_px > MAX_FIGURE_PIXELS:
            raise MapFigureFormatUnsupported(
                f"该 DPI 下图面为 {int(width_px)}×{int(height_px)} = "
                f"{int(width_px * height_px)} 像素，超过上限 {MAX_FIGURE_PIXELS}"
            )
        figure_id = _figure_id(spec, export_dpi, format_name)
        record = self._record(figure_id)
        if record is not None and self.store.exists(figure_id, record):
            # 幂等复用：同一业务 revision + 同一 FigureSpec（含 DPI / 格式）必然同图号。
            # 这里只把索引里的 active 指向它 —— **不** 写 ProjectState，也 **不** 调用
            # ``session.save()``，因此业务 revision 不会因为重复导出而变化。
            self._activate(figure_id)
            return {
                "figure_id": figure_id, "record": self._record(figure_id) or deepcopy(record),
                "spec": spec.to_dict(), "reused": True, "image_bytes": 0,
            }
        image = self._render(spec, dpi=float(export_dpi))
        record = self._publish(spec, image, figure_id, dpi=export_dpi, format_name=format_name)
        # 注意：正式导出**不**把 PNG 字节放进 JSON 响应（图件通过 artifact 端点按需读取）。
        return {
            "figure_id": figure_id, "record": record, "spec": spec.to_dict(),
            "reused": False, "image_bytes": len(image),
        }

    def _render(self, spec, *, dpi):
        renderer = self.renderer_factory()
        try:
            image = self.qgis_call(lambda: renderer.render(spec, dpi=dpi))
        except MapFigureError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一转成业务错误
            raise MapFigureRenderFailed(f"专题图渲染失败：{exc}") from exc
        if not isinstance(image, (bytes, bytearray)) or not bytes(image).startswith(b"\x89PNG"):
            raise MapFigureRenderFailed("专题图渲染没有返回有效 PNG")
        return bytes(image)

    def _publish(self, spec, image, figure_id, *, dpi, format_name):
        record = {
            "figure_id": figure_id,
            "template_id": spec.template_id,
            "template_version": spec.template_version,
            "title": spec.title,
            "route_id": spec.route_id,
            "route_source": spec.route_source,
            "format": str(format_name).lower(),
            "dpi": float(dpi),
            "generated_at": self.clock(),
            "project_revision": int(spec.generated_from_revision),
            "spec_fingerprint": spec.fingerprint(),
            "extent": spec.extent.to_dict(),
            "display_thresholds": deepcopy(spec.display_thresholds),
            "source_status": deepcopy(spec.source_status),
            "omitted_layers": deepcopy(spec.omitted_layers),
            "legend_items": [item.to_dict() for item in spec.legend_items],
            "parameters": deepcopy(spec.parameters),
            "layout": deepcopy(spec.layout),
            "boundaries": deepcopy(spec.boundaries),
            "spec_summary": spec.summary(),
        }
        # 产物目录是 **route-first**：同一条航路的所有专题图归档在同一个 route 目录下。
        # 目录名由 store 从 route_id 派生（客户端无法控制任何路径）。
        complete = self.store.write(
            figure_id, image_bytes=image, spec_payload=spec.to_dict(), record=record,
            route_id=spec.route_id, template_id=spec.template_id,
        )
        # metadata.json 与 index 里的记录是**同一个**完整记录：固定审计字段逐字段一致。
        self._register(complete, spec)
        return complete

    def _register(self, record, spec):
        """把图件记录写进制图模块**自己的受控索引**（总索引 + route 级索引）。

        关键契约（本轮的 code review 修复）：**不调用 ``session.save()``**。
        ProjectState 的 ``revision`` 是业务语义的乐观锁；"渲染了一张专题图"不改变任何
        业务事实，因此既不动 ``state["map_figures"]``（旧版本会写，现已只读兼容），
        也不动 ``artifact_manifest`` —— 图件索引与元数据都在
        ``artifacts/map_figures/`` 内，业务 revision 逐字节不变。
        """

        index = self.store.read_index()
        previous_active = index["active_figure_id"]
        items = [
            item for item in index["items"]
            if isinstance(item, dict) and str(item.get("figure_id")) != record["figure_id"]
        ]
        for item in items:
            if item.get("figure_id") == previous_active:
                item["current_applicability"] = "superseded"
        record["current_applicability"] = "current"
        items.append(record)
        items.sort(key=lambda item: str(item.get("generated_at") or ""))
        self.store.write_index(items, record["figure_id"])
        self._write_route_index(record.get("route_id"), items, record["figure_id"])

    def _write_route_index(self, route_id, items, active_figure_id):
        """重建并写入该航路的 route 级索引（人工查找 + 后续按航路浏览）。"""

        if not str(route_id or "").strip():
            return None
        return self.store.write_route_index(
            route_id, self._route_templates(items, route_id, active_figure_id),
        )

    @staticmethod
    def _route_templates(items, route_id, active_figure_id):
        """按模板归集某条航路的图件（每个模板一个 active 图号 + 图件清单）。"""

        templates = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            if str(item.get("route_id") or "") != str(route_id):
                continue
            template_id = str(item.get("template_id") or "")
            if not template_id:
                continue
            entry = templates.setdefault(
                template_id,
                {"template_id": template_id, "active_figure_id": None, "items": []},
            )
            entry["items"].append({
                "figure_id": str(item.get("figure_id") or ""),
                "template_id": template_id,
                "title": item.get("title"),
                "generated_at": item.get("generated_at"),
                "project_revision": item.get("project_revision"),
                "relative_path": item.get("relative_path"),
                "spec_relative_path": item.get("spec_relative_path"),
                "record_relative_path": item.get("record_relative_path"),
                "image_sha256": item.get("image_sha256"),
                "image_bytes": item.get("image_bytes"),
                "dpi": item.get("dpi"),
                "format": item.get("format"),
            })
        for entry in templates.values():
            chosen = next(
                (item["figure_id"] for item in entry["items"]
                 if item["figure_id"] == str(active_figure_id or "")),
                None,
            )
            entry["active_figure_id"] = chosen or (
                entry["items"][-1]["figure_id"] if entry["items"] else None
            )
        return templates

    def _remember_preview(self, key, image):
        self._preview_cache[key] = image
        self._preview_cache_order.append(key)
        while len(self._preview_cache_order) > 8:
            oldest = self._preview_cache_order.pop(0)
            self._preview_cache.pop(oldest, None)

    def _activate(self, figure_id):
        """把索引里的 active 图号指向该图件（只写受控索引，不碰 ProjectState）。"""

        index = self.store.read_index()
        items = list(index["items"])
        route_id = None
        for item in items:
            if isinstance(item, dict) and str(item.get("figure_id")) == str(figure_id):
                item["current_applicability"] = "current"
                route_id = item.get("route_id")
            elif item.get("current_applicability") == "current":
                item["current_applicability"] = "superseded"
        self.store.write_index(items, figure_id)
        if route_id:
            self._write_route_index(route_id, items, figure_id)


def _sync_layout_after_disclosure(layout, parameters, result):
    """把"最终披露句"同步进版面，并保持所有几何字段自洽。

    为什么需要它：披露句要等图例/图层定稿（第二次装配）才知道，而"有没有披露句"决定
    图例框的位置与高度。这里在最终 ``map_disclosure`` 确定后重算一次版面，
    并保证 ``footer_disclosure_top_mm`` / ``footer_strip_top_mm`` / ``legend_top_mm`` /
    ``legend_height_mm`` 与之一致 —— 正式图（无审计条、无披露）时它们分别是合理值或
    ``None``，绝不出现"参数里为 None、报告里又是数值"的自相矛盾。
    """

    from ..gis.figure_legend import legend_geometry
    from ..gis.figure_style import CNS_LEGEND_GROUP_COLUMNS, LAYOUT

    disclosure = str((result.metadata or {}).get("map_disclosure") or "")
    return _layout_plan(
        parameters, legend_items=_legend_layout_entries(result.legend_items),
        layout_disclosure=disclosure,
    )


def _layout_profile_version(profile):
    """从版式档位名里取版本号（``cns_five_figure_v2`` → ``2``）；取不到时为 ``1``。"""

    tail = str(profile or "").rsplit("_v", 1)[-1]
    return int(tail) if tail.isdigit() else 1


def _legend_layout_entries(legend_items):
    """把图例条目转成版面几何需要的形状（分组 + 显示名）。"""

    return [
        {"group": item.legend_group, "text": item.display_name, "style_key": item.style_key}
        for item in legend_items
    ]


def _layout_plan(parameters, legend_items=(), layout_disclosure=""):
    """A4 竖版版面（毫米）：标题带 → 地图 →（披露条 / 审计条）→ 图例框 → 页脚。

    Round30-B1.1 的硬约束与做法：

    * **地图框尺寸不因图例而变**：CNS 档位使用固定地图高度
      （``LAYOUT['cns_fixed_map_height_mm']`` = 176.2 mm），五图严格一致；
    * **图例用页面底部剩余空间放大**，只在真的放不下时才按比例压缩行距，
      绝不反向挤压地图框；
    * 地图框之外的两条薄带都可开关：

      - **图面披露条**（Radar 能力限制等业务披露）→ 正式图也保留；
      - **工程审计条**（地图 CRS / 经纬网 CRS / revision / 未显示图层）→
        ``audit_footer`` 控制，**正式图默认关闭**（信息仍完整保存在 FigureSpec.metadata
        与导出报告里）。

    版式档位：``cns_five_figure_v2``（B1.1 沿用同一档位，字号/网格/图例几何在档位内演进）。
    """

    from ..gis.figure_legend import CNS_LEGEND_BALANCE_TOLERANCE, legend_geometry
    from ..gis.figure_style import (
        CNS_LEGEND_GROUP_COLUMNS, LAYOUT, LEGEND_GROUP_COLUMNS,
    )

    width = float(parameters.get("document_width_mm") or 210.0)
    height = float(parameters.get("document_height_mm") or 297.0)
    margin = float(parameters.get("margin_mm") or LAYOUT["page_margin_mm"])
    map_fraction = float(parameters.get("map_fraction") or 0.72)
    columns = max(1, min(int(LAYOUT.get("max_legend_columns", 4)),
                         int(parameters.get("legend_columns") or 3)))
    title_band = float(LAYOUT["title_band_mm"])
    title_gap = float(LAYOUT["title_gap_mm"])
    title_map_gap = float(LAYOUT["title_map_gap_mm"])
    subtitle_height = float(LAYOUT["subtitle_band_mm"])
    title_main_height = max(
        4.0, title_band - title_gap - subtitle_height - title_map_gap,
    )
    footer_band = float(LAYOUT["footer_band_mm"])
    footer_map_gap = float(LAYOUT["footer_map_gap_mm"])
    footer_disclosure = float(LAYOUT["footer_disclosure_mm"])
    footer_strip = float(LAYOUT["footer_strip_mm"])
    footer_legend_gap = float(LAYOUT["footer_legend_gap_mm"])
    show_audit = bool(parameters.get("audit_footer", LAYOUT.get("audit_footer_default", False)))
    disclosure_text = str(
        parameters.get("map_disclosure") or layout_disclosure or ""
    ).strip()
    show_disclosure = bool(disclosure_text)
    band_total = footer_map_gap + footer_legend_gap
    if show_disclosure:
        band_total += footer_disclosure
    if show_audit:
        band_total += footer_strip
    map_width = width - 2 * margin
    map_top = margin + title_band
    row_height = float(LAYOUT["legend_row_mm"])
    group_row = float(LAYOUT["legend_group_row_mm"])
    group_gap = float(LAYOUT["legend_group_gap_mm"])
    group_item_gap = float(LAYOUT["legend_group_item_gap_mm"])
    top_padding = float(LAYOUT["legend_top_padding_mm"])
    header = float(LAYOUT["legend_header_mm"])
    profile = str(parameters.get("layout_profile") or "")
    cns_profile = profile == CNS_LAYOUT_PROFILE
    group_columns = CNS_LEGEND_GROUP_COLUMNS if cns_profile else LEGEND_GROUP_COLUMNS

    def geometry_for(row, group):
        return legend_geometry(
            list(legend_items or ()), row_height=row, group_row=group,
            columns=columns, header_height=header, group_gap=group_gap,
            group_item_gap=group_item_gap, group_columns=group_columns,
            top_padding=top_padding,
            # route_overview_v1 保持纯语义分列（历史版式不变）；
            # CNS 五图允许在语义分列明显失衡时回退到自动均衡。
            balance_tolerance=(
                CNS_LEGEND_BALANCE_TOLERANCE if cns_profile else None
            ),
        )

    geometry = geometry_for(row_height, group_row)
    compact = geometry_for(float(LAYOUT["legend_min_row_mm"]),
                           float(LAYOUT["legend_min_group_row_mm"]))
    actual_needed = max(float(geometry["box_height_mm"]), float(compact["box_height_mm"]))
    # 地图高度：CNS 档位**固定**（五图一致、不随图例变化）；其它模板沿用比例分配。
    if cns_profile:
        map_height = float(LAYOUT["cns_fixed_map_height_mm"])
    else:
        preferred = (height - footer_band) * map_fraction
        map_height = min(preferred, height - footer_band - map_top - actual_needed - band_total)
    map_height = max(float(LAYOUT["cns_min_map_height_mm"]), map_height)
    cursor = map_top + map_height
    disclosure_top = None
    if show_disclosure:
        disclosure_top = cursor + footer_map_gap
        cursor = disclosure_top + footer_disclosure
    footer_strip_top = None
    if show_audit:
        footer_strip_top = cursor + (footer_map_gap if not show_disclosure else 0.0)
        cursor = footer_strip_top + footer_strip
    legend_top = cursor + footer_legend_gap
    # 图例可用高度 = 页面下沿（页脚上沿）− 图例框上沿：图例**绝不允许溢出页面**。
    available_legend = max(18.0, (height - footer_band) - legend_top)
    legend_height = min(actual_needed, available_legend)
    if float(geometry["box_height_mm"]) > legend_height:
        ratio = max(0.60, legend_height / float(geometry["box_height_mm"]))
        row_height = max(float(LAYOUT["legend_min_row_mm"]), row_height * ratio)
        group_row = max(float(LAYOUT["legend_min_group_row_mm"]), group_row * ratio)
        geometry = geometry_for(row_height, group_row)
        actual_needed = float(geometry["box_height_mm"])
        legend_height = min(actual_needed, available_legend)
    effective_columns = int(geometry["columns"])
    return {
        "document_width_mm": width,
        "document_height_mm": height,
        "margin_mm": margin,
        "title_band_mm": title_band,
        "title_gap_mm": title_gap,
        "title_map_gap_mm": float(LAYOUT["title_map_gap_mm"]),
        "title_main_height_mm": title_main_height,
        "subtitle_height_mm": subtitle_height,
        "subtitle_top_mm": margin + title_main_height + title_gap,
        "footer_band_mm": footer_band,
        "footer_strip_mm": footer_strip,
        "footer_disclosure_mm": footer_disclosure,
        "footer_map_gap_mm": footer_map_gap,
        "footer_legend_gap_mm": footer_legend_gap,
        "footer_disclosure_top_mm": disclosure_top,
        "footer_strip_top_mm": footer_strip_top,
        "map_disclosure": disclosure_text,
        "audit_footer": show_audit,
        "disclosure_visible": show_disclosure,
        "title_top_mm": margin,
        "map_top_mm": map_top,
        "map_left_mm": margin,
        "map_width_mm": map_width,
        "map_height_mm": map_height,
        # 地图画布 CRS：本地米制。几何 / extent / 标注锚点都由渲染器投影到这个 CRS，
        # 因此"按米制 buffer 生成的覆盖圈"在纸面上是正圆。
        "map_crs": DEFAULT_METRIC_CRS,
        "grid_crs": "EPSG:4326",
        "map_legend_gap_mm": band_total,
        "legend_top_mm": legend_top,
        "legend_left_mm": margin,
        "legend_width_mm": map_width,
        "legend_height_mm": legend_height,
        "legend_columns": effective_columns,
        "legend_row_mm": row_height,
        "legend_group_row_mm": group_row,
        "legend_group_gap_mm": group_gap,
        "legend_group_item_gap_mm": group_item_gap,
        "legend_top_padding_mm": top_padding,
        "legend_header_mm": header,
        "legend_side_padding_mm": float(LAYOUT["legend_side_padding_mm"]),
        "legend_column_gap_mm": float(LAYOUT["legend_column_gap_mm"]),
        "legend_text_gutter_mm": float(LAYOUT["legend_text_gutter_mm"]),
        "legend_symbol_box_mm": float(LAYOUT["legend_symbol_box_mm"]),
        "legend_title_font_size": float(LAYOUT["legend_title_font_size"]),
        "legend_group_font_size": float(LAYOUT["legend_group_font_size"]),
        "legend_item_font_size": float(LAYOUT["legend_item_font_size"]),
        "map_title_font_size": float(LAYOUT["map_title_font_size"]),
        "subtitle_font_size": float(LAYOUT["subtitle_font_size"]),
        "grid_font_size": float(LAYOUT["grid_font_size"]),
        "scalebar_font_size": float(LAYOUT["scalebar_font_size"]),
        "label_font_sizes": {
            key: float(value["font_size"]) for key, value in LABEL_STYLES.items()
        },
        "scale_bar_margin_mm": float(LAYOUT["scale_bar_margin_mm"]),
        "north_arrow_size_mm": float(LAYOUT["north_arrow_size_mm"]),
        "north_arrow_margin_mm": float(LAYOUT["north_arrow_margin_mm"]),
        "layout_profile": profile or "route_overview_v1",
        "legend_budget_height_mm": (
            float(LAYOUT["cns_legend_budget_height_mm"]) if cns_profile else None
        ),
        "legend_actual_height_mm": actual_needed,
        "extent_aspect": map_width / map_height if map_height else 1.0,
        "fixed_layout": (
            "a4_portrait_fixed_map_frame_legend_below_cns_shared_map_frame"
            if cns_profile else
            "a4_portrait_map_above_legend_below_with_audit_strip"
        ),
    }


def _preview_width(requested, layout, px_per_mm):
    target = _finite(requested, None)
    if target is None:
        target = layout["document_width_mm"] * _finite(px_per_mm, 3.0)
    return int(max(320, min(4000, round(float(target)))))


def _bounded_preview_width(width_px, layout):
    """把预览宽度压进**总像素**上限（宽 × 高 <= :data:`MAX_PREVIEW_PIXELS`）。

    code review 修复项：以前只把 ``width × aspect`` 与上限比较（量纲不对，等于只约束了
    高度），总像素仍可能远超上限。现在按真实定义计算：

        height_px = width_px * document_height_mm / document_width_mm
        total_px  = width_px * height_px

    超限时反解 ``width_px = floor(sqrt(MAX_PREVIEW_PIXELS / aspect))``，其中
    ``aspect = document_height_mm / document_width_mm``。这样缩放是**等比**的，
    长宽比不发生变化；preview 与 export 各自保留自己的上限
    （preview: :data:`MAX_PREVIEW_PIXELS`，export: :data:`MAX_FIGURE_PIXELS`）。
    """

    try:
        aspect = float(layout["document_height_mm"]) / float(layout["document_width_mm"])
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return int(width_px)
    width = int(width_px)
    if width <= 0 or aspect <= 0:
        return width
    if width * (width * aspect) <= MAX_PREVIEW_PIXELS:
        return width
    return max(1, int(math.floor(math.sqrt(MAX_PREVIEW_PIXELS / aspect))))


#: 正式图标题：按模板语义区分（不把 route_id / revision 塞进标题）。
#:
#: ``surveillance_layout_v1`` 有两种 variant，标题必须区分 RID（合作监视）与
#: Radar（非合作监视评估），否则用户无法从图名判断这是哪一种能力。
CNS_FORMAL_TITLES = {
    COMMUNICATION_LAYOUT_V1: "通信设施布设图",
    SURVEILLANCE_LAYOUT_V1: "监视设施布设图",
    NAVIGATION_LAYOUT_V1: "导航完整性监测点布设图",
    CNS_COMBINED_V1: "CNS 综合设施布设图",
}
CNS_SURVEILLANCE_TITLES = {
    SURVEILLANCE_SERVICE_RID: "RID 合作监视设施布设图",
    SURVEILLANCE_SERVICE_RADAR: "Radar 非合作监视评估图",
}


def _figure_title(display_name, route, template_id=None, parameters=None):
    """图名：正式图面标题按模板语义给出，**不**把 route_id / revision 塞进标题。

    航路标识只作为 FigureSpec 字段（``route_id``）与记录保存，供审计与检索使用。
    """

    template = str(template_id or "").strip()
    if template == SURVEILLANCE_LAYOUT_V1:
        variant = str((parameters or {}).get(SURVEILLANCE_SERVICE_PARAMETER) or "")
        formal = CNS_SURVEILLANCE_TITLES.get(variant)
        if formal:
            return formal
    formal = CNS_FORMAL_TITLES.get(template)
    if formal:
        return formal
    return str(display_name or "").strip() or "航路周边状况图"


def _altitude_fact(state, route_id):
    """route 的**固定高度层 canonical 事实**：``(altitude_layer_id, altitude_m)``。

    只读 ``radar_surveillance_layout`` 里属于本 route 的那一条；取不到时返回
    ``(None, None)``——此时副标题**不会**硬编码任何高度语义。
    """

    collection = (state or {}).get(RADAR_LAYOUT_KEY)
    if not isinstance(collection, dict):
        return None, None
    for item in collection.get("items") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("route_id") or "") != str(route_id or ""):
            continue
        layer_id = str(item.get("altitude_layer_id") or "").strip() or None
        altitude = _finite(item.get("altitude_m"))
        return layer_id, altitude
    return None, None


def _figure_subtitle(state, route_id):
    """副标题：``<route_id> · <altitude_layer_id>[ · 固定巡航高度 N m]``。

    全部取自 canonical facts：

    * ``route_id`` 来自权威运行航路；
    * ``altitude_layer_id`` / ``altitude_m`` 来自 ``radar_surveillance_layout`` 中
      **本 route** 的固定高度层记录。

    缺少高度层事实时只写 route_id（**绝不硬编码**"ALT-100 / 100 m"）。
    """

    code = str(route_id or "").strip()
    if not code:
        return ""
    layer_id, altitude = _altitude_fact(state, code)
    parts = [code]
    if layer_id:
        parts.append(layer_id)
    if altitude is not None and altitude > 0:
        parts.append(f"固定巡航高度 {altitude:g} m")
    return " · ".join(parts)


def _figure_subtitle_for(state, route_id):
    """兼容入口：由 ``(state, route_id)`` 生成副标题。"""

    return _figure_subtitle(state, route_id)


def _require_template_parameters(template_id, parameters, overrides):
    """模板声明的**必需参数**缺失时明确拒绝（绝不用默认值静默兜底）。

    当前唯一的必需参数是 ``surveillance_layout_v1`` 的 ``surveillance_service``：
    RID 与 Radar 是两种完全不同的监视能力，任何"默认取一个"的行为都会让用户看到
    一张并非自己要求的能力图，因此这里必须硬失败。
    """

    required = template_required(template_id)
    if not required:
        return parameters
    missing = [
        name for name in required
        if parameters.get(name) in (None, "", [], {})
    ]
    if missing:
        raise MapFigureParameterError(
            f"模板「{template_id}」必须显式提供参数：{'、'.join(missing)}",
            detail={
                "template_id": template_id,
                "missing_parameters": missing,
                "provided_overrides": sorted((overrides or {}).keys()),
                "allowed": {
                    SURVEILLANCE_SERVICE_PARAMETER: list(SURVEILLANCE_SERVICE_VALUES),
                }.get(missing[0]),
            },
        )
    return parameters


def _annotate_extent_evidence(evidence, result, extent, parameters):
    """把"覆盖圈是否完全落在统一 extent 内"写进 extent 证据（越界即如实登记）。

    这一步**只读**：越界时不会偷偷扩大范围。第 16 节要求五图 extent 统一，因此
    正确做法是把结论记下来供审计，而不是为某一类圆单独放大地图。
    """

    metres_per_degree_lat = 111_320.0
    metres_per_degree_lon = metres_per_degree_lat * math.cos(
        math.radians((float(extent.south) + float(extent.north)) / 2.0)
    )
    circles = []
    for layer in result.layers:
        if not str(layer.layer_key).startswith("cns_coverage_"):
            continue
        data = layer.data or {}
        rings = data.get("polygons") or []
        if not rings:
            continue
        longitudes = [point[0] for ring in rings for point in ring]
        latitudes = [point[1] for ring in rings for point in ring]
        if not longitudes or not latitudes:
            continue
        margins = {
            "west": (min(longitudes) - float(extent.west)) * metres_per_degree_lon / 1000.0,
            "east": (float(extent.east) - max(longitudes)) * metres_per_degree_lon / 1000.0,
            "south": (min(latitudes) - float(extent.south)) * metres_per_degree_lat / 1000.0,
            "north": (float(extent.north) - max(latitudes)) * metres_per_degree_lat / 1000.0,
        }
        circles.append({
            "layer_key": layer.layer_key,
            "radius_m": data.get("radius_m"),
            "circle_count": len(rings),
            "min_margin_km": round(min(margins.values()), 3),
            "margins_km": {key: round(value, 3) for key, value in margins.items()},
            "inside_extent": all(value >= 0.0 for value in margins.values()),
        })
    return {
        **evidence,
        "coverage_circles": circles,
        "coverage_circles_inside_extent": all(item["inside_extent"] for item in circles)
        if circles else None,
        "coverage_display_margin_km": parameters.get("coverage_display_margin_km"),
        "extent_uniform_across_cns_templates": bool(
            parameters.get("layout_profile") == CNS_LAYOUT_PROFILE
        ),
    }


def _figure_id(spec, dpi, format_name):
    raw = json.dumps({
        "spec_fingerprint": spec.fingerprint(),
        "template_id": spec.template_id,
        "route_id": spec.route_id,
        "revision": int(spec.generated_from_revision),
        "dpi": float(dpi),
        "format": str(format_name).lower(),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "MF-" + sha256(raw).hexdigest()[:32]


def _default_renderer_factory():
    from ..gis.qgis_figure_renderer import QgisFigureRenderer

    return QgisFigureRenderer()


__all__ = [
    "DEFAULT_METRIC_CRS", "MAP_FIGURE_COLLECTION", "MAP_FIGURE_DIRECTORY",
    "MAX_PREVIEW_PIXELS", "MapFigureError", "MapFigureFormatUnsupported",
    "MapFigureNotFound", "MapFigurePathDenied", "MapFigureRenderFailed",
    "MapFigureRouteError", "MapFigureRouteSelectionRequired", "MapFigureService",
    "MapFigureStore", "MapFigureTemplateUnavailable", "MaterializationContext",
    "MaterializationResult", "OPERATIONAL_ROUTE_SOURCE", "SUPPORTED_FORMATS",
    "materialize", "utc_now",
]
