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
    DEFAULT_CRS, GEOMETRY_LINE, GEOMETRY_POINT, GEOMETRY_POLYGON, SOURCE_AVAILABLE,
    SOURCE_UNKNOWN, SOURCE_UNAVAILABLE, ExtentSpec, FigureSpec, LabelSpec, LayerSpec,
    LegendItem,
)
from ..gis.figure_style import LEGEND_GROUP_OF, legend_order_key
from ..gis.source_inspection import inspect_cartographic_land
from ..reporting.map_templates import (
    ROUTE_OVERVIEW_V1, catalog as template_catalog, is_available, parameters as template_parameters,
    template as template_definition,
)

#: 权威运行航路的 canonical state 容器。
OPERATIONAL_ROUTE_SOURCE = "operational_routes"
#: 制图模块内部使用的米制 CRS（与项目其它真实几何一致，可被模板参数覆盖）。
DEFAULT_METRIC_CRS = "EPSG:32651"
#: 起点 / 终点名称的最大长度（避免版面被超长名称撑开）。
MAX_LABEL_CHARS = 14
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


@dataclass
class MaterializationResult:
    layers: list = field(default_factory=list)
    labels: list = field(default_factory=list)
    legend_items: list = field(default_factory=list)
    source_status: dict = field(default_factory=dict)
    omitted_layers: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


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


def _legends(layers):
    """按稳定语义顺序生成图例条目（只含真实存在且可用的图层）。"""

    items = [
        LegendItem(
            layer_key=layer.layer_key, style_key=layer.style_key,
            display_name=layer.display_name,
            legend_group=LEGEND_GROUP_OF.get(layer.layer_key, ""),
        )
        for layer in layers
        if layer.legend_visible and layer.feature_count > 0
        and layer.source_status == SOURCE_AVAILABLE
    ]
    items.sort(key=lambda item: legend_order_key(item.layer_key))
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
    points = []
    axis_conflicts = []
    for tower in towers:
        if not isinstance(tower, dict):
            continue
        longitude, latitude = _tower_coordinate(tower, axis_conflicts)
        if longitude is None or latitude is None:
            continue
        if not viewport.contains(longitude, latitude):
            continue
        points.append({
            "longitude": longitude, "latitude": latitude,
            "name": _short(tower.get("name")),
            "tower_id": str(tower.get("tower_id") or ""),
            "height_m": _finite(tower.get("height_m")),
            "site_type": tower.get("site_type"),
            "coordinate_source": tower.get("coordinate_source") or "tower_record",
        })
    tower_detail = {
        "role": "towers",
        "collection_id": collection.get("collection_id"),
        "total_count": len(towers),
        "within_extent": len(points),
        "out_of_extent": max(0, len(towers) - len(points)),
        "source_crs": ((collection.get("crs") or {}).get("source_crs") or {}).get("value"),
        "source_crs_status": ((collection.get("crs") or {}).get("source_crs") or {}).get("status"),
        "crs_confirmed": bool(((collection.get("crs") or {}).get("source_crs") or {}).get("confirmed")),
        "axis_order_conflicts": axis_conflicts[:5],
        "axis_order_conflict_count": len(axis_conflicts),
        "coordinate_field": (
            "coordinate" if any(item.get("coordinate_source") == "coordinate_field"
                                for item in points) else "longitude/latitude"
        ),
        "semantics": "confirmed_tower_records_from_project_state",
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
                detail={**tower_detail, "note": "已导入铁塔，但当前图面范围内没有铁塔"},
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


def _node_name(ctx, node_id):
    if not node_id:
        return ""
    for node in ctx.state.get("nodes") or []:
        if isinstance(node, dict) and str(node.get("node_id")) == str(node_id):
            return _short(node.get("name"))
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
    """

    value = route.get(key)
    name = ""
    if isinstance(value, dict):
        name = _short(value.get("name"))
    elif isinstance(value, str):
        name = _short(value)
    if name:
        return name
    return _short(_node_name(ctx, route.get(node_field)))


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
                    kind="place", text=_short(name, 10),
                    longitude=coordinate[0], latitude=coordinate[1],
                    priority=30, style_key="label_place",
                ))
    return labels


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
        elif layer.feature_count <= 0:
            omitted.append({
                "layer_key": layer.layer_key, "display_name": layer.display_name,
                "status": "empty_within_extent",
                "reason": "数据源可读，但当前图面范围内没有要素",
            })
    warnings = [f"{item['display_name']}：{item['reason']}" for item in omitted]
    return MaterializationResult(
        layers=layers, labels=labels, legend_items=legend_items,
        source_status=source_status, omitted_layers=omitted, warnings=warnings,
    )


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
        parameters = template_parameters(template_id, parameter_overrides)
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
        probe = materialize(MaterializationContext(
            state=self.session.state, paths=paths, route=route, extent=extent,
            parameters=parameters, project_revision=revision,
            source_audits=self.session.state.get("source_audits") or {},
        ))
        layout = _layout_plan(parameters, legend_items=_legend_layout_entries(probe.legend_items))
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
        result = materialize(MaterializationContext(
            state=self.session.state, paths=paths, route=route, extent=extent,
            parameters=parameters, project_revision=revision,
            source_audits=self.session.state.get("source_audits") or {},
            surface_classification_policy=(
                self.session.state.get("surface_classification_policy") or {}
            ),
        ))
        path_points = [
            list(point) for point in (route.get("path") or [])
            if isinstance(point, (list, tuple)) and len(point) >= 2
        ]
        spec = FigureSpec(
            template_id=template["template_id"],
            template_version=int(template["template_version"]),
            title=_figure_title(template["display_name"], route),
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
            },
            layout=layout,
            parameters=deepcopy(parameters),
            warnings=list(result.warnings),
            boundaries={
                "presentation_only": True,
                "writes_operational_routes_or_cns": False,
                "recomputes_business_results": False,
                "unknown_is_not_zero": True,
                "missing_is_not_empty": True,
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


def _legend_layout_entries(legend_items):
    """把图例条目转成版面几何需要的形状（分组 + 显示名）。"""

    return [
        {"group": item.legend_group, "text": item.display_name, "style_key": item.style_key}
        for item in legend_items
    ]


def _layout_plan(parameters, legend_items=()):
    """A4 竖版版面（毫米）：标题带 → 地图 →（薄审计条）→ 图例框 → 页脚。

    分配目标（用户要求）：标题 4~5% · 地图 68~72% · 间距 1~2% · 图例 18~22% · 最小页边距。
    做法：先用 :mod:`cns_planner.gis.figure_legend` 的**同一套分列算法**算出图例框
    真实高度（贴合内容），再把剩余高度全部给地图——因此图例条目少时地图自动变大，
    **不会出现"图例框很高、下方大片空白"**。

    地图框与图例框之间保留一条独立的**薄审计条**（坐标系 / 项目 revision / 未显示图层）：
    三段间距固定（``footer_map_gap`` + ``footer_strip`` + ``footer_legend_gap``），
    因此这行小字绝不会压到地图边框、比例尺或图例标题上。
    """

    from ..gis.figure_legend import legend_geometry
    from ..gis.figure_style import LAYOUT, LEGEND_GROUP_COLUMNS

    width = float(parameters.get("document_width_mm") or 210.0)
    height = float(parameters.get("document_height_mm") or 297.0)
    margin = float(parameters.get("margin_mm") or LAYOUT["page_margin_mm"])
    map_fraction = float(parameters.get("map_fraction") or 0.72)
    columns = max(1, min(int(LAYOUT.get("max_legend_columns", 3)),
                         int(parameters.get("legend_columns") or 2)))
    title_band = float(LAYOUT["title_band_mm"])
    title_gap = float(LAYOUT["title_gap_mm"])
    title_map_gap = float(LAYOUT["title_map_gap_mm"])
    subtitle_height = float(LAYOUT["subtitle_band_mm"])
    # 标题带内部：主标题 → 固定间距 → 副标题 → 固定间距 → 地图框。
    # 因此 title_band 必须把"副标题与地图框之间的间距"也算进去，否则副标题会贴住上边框。
    title_main_height = max(
        4.0, title_band - title_gap - subtitle_height - title_map_gap,
    )
    footer_band = float(LAYOUT["footer_band_mm"])
    footer_map_gap = float(LAYOUT["footer_map_gap_mm"])
    footer_strip = float(LAYOUT["footer_strip_mm"])
    footer_legend_gap = float(LAYOUT["footer_legend_gap_mm"])
    gap = footer_map_gap + footer_strip + footer_legend_gap
    map_width = width - 2 * margin
    map_top = margin + title_band
    total_height = height - margin - footer_band
    row_height = float(LAYOUT["legend_row_mm"])
    group_row = float(LAYOUT["legend_group_row_mm"])
    group_gap = float(LAYOUT["legend_group_gap_mm"])
    group_item_gap = float(LAYOUT["legend_group_item_gap_mm"])
    top_padding = float(LAYOUT["legend_top_padding_mm"])
    header = float(LAYOUT["legend_header_mm"])

    def geometry_for(row, group):
        return legend_geometry(
            list(legend_items or ()), row_height=row, group_row=group,
            columns=columns, header_height=header, group_gap=group_gap,
            group_item_gap=group_item_gap, group_columns=LEGEND_GROUP_COLUMNS,
            top_padding=top_padding,
        )

    geometry = geometry_for(row_height, group_row)
    # 下界：按**最小可用行距**重算一次，保证图例一定放得下（宁可地图小一点，也不截断图例）。
    compact = geometry_for(float(LAYOUT["legend_min_row_mm"]),
                           float(LAYOUT["legend_min_group_row_mm"]))
    needed = max(float(geometry["box_height_mm"]), float(compact["box_height_mm"]))
    effective_columns = int(geometry["columns"])
    # 地图是主体：先按 map_fraction（0.72）拿高度，再为图例让出必要空间；
    # 图例框最终**贴合内容**，因此不会出现"框很高、内容只占左上角 + 下方大片空白"。
    preferred_map = total_height * map_fraction
    map_height = min(preferred_map, total_height - map_top - needed - gap)
    map_height = max(150.0, map_height)
    footer_strip_top = map_top + map_height + footer_map_gap
    legend_top = footer_strip_top + footer_strip + footer_legend_gap
    available_legend = max(18.0, height - margin - footer_band - legend_top)
    legend_height = min(needed, available_legend)
    # 只有在空间确实不足时才压缩行距（下限 3.9 mm 仍可读），绝不截断图例条目。
    if float(geometry["box_height_mm"]) > legend_height:
        ratio = max(0.60, legend_height / float(geometry["box_height_mm"]))
        row_height = max(float(LAYOUT["legend_min_row_mm"]), row_height * ratio)
        group_row = max(float(LAYOUT["legend_min_group_row_mm"]), group_row * ratio)
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
        "footer_map_gap_mm": footer_map_gap,
        "footer_legend_gap_mm": footer_legend_gap,
        "footer_strip_top_mm": footer_strip_top,
        "title_top_mm": margin,
        "map_top_mm": map_top,
        "map_left_mm": margin,
        "map_width_mm": map_width,
        "map_height_mm": map_height,
        "map_legend_gap_mm": gap,
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
        "legend_side_padding_mm": float(LAYOUT["legend_side_padding_mm"]),
        "legend_column_gap_mm": float(LAYOUT["legend_column_gap_mm"]),
        "legend_text_gutter_mm": float(LAYOUT["legend_text_gutter_mm"]),
        "legend_symbol_box_mm": float(LAYOUT["legend_symbol_box_mm"]),
        "scale_bar_margin_mm": float(LAYOUT["scale_bar_margin_mm"]),
        "north_arrow_size_mm": float(LAYOUT["north_arrow_size_mm"]),
        "north_arrow_margin_mm": float(LAYOUT["north_arrow_margin_mm"]),
        "fixed_layout": "a4_portrait_map_above_legend_below_with_audit_strip",
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


def _figure_title(display_name, route):
    """图名：正式图面标题固定为「航路周边状况图」，**不**把 route_id / revision 塞进标题。

    航路标识只作为 FigureSpec 字段（``route_id``）与记录保存，供审计与检索使用。
    """

    return str(display_name or "").strip() or "航路周边状况图"


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
