"""专题制图的原语与 FigureSpec（业务数据 ↔ 制图器之间的稳定边界）。

设计要点
========

* **JSON-safe**：:class:`FigureSpec` 只包含字符串 / 数字 / 布尔 / 列表 / 字典与
  规范化几何（``[lon, lat]`` 数值对）。**绝不**把 ``QgsLayer`` / ``QgsGeometry`` /
  ``QgsSymbol`` 等 QGIS 对象塞进 FigureSpec；渲染器通过 :mod:`figure_style` 的样式键
  重建符号。
* **可审计**：``source_status`` / ``omitted_layers`` 逐图层记录
  available / unavailable / unknown 与中文原因；``display_thresholds`` 记录本图使用的
  显示阈值及其依据（这是 **figure display threshold**，不是业务约束）。
* **无业务语义**：本模块不计算风险、不规划航路、不判断安全，也不写任何 canonical 结果。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from hashlib import sha256
import json

FIGURE_SPEC_SCHEMA_VERSION = 1

#: 图层来源状态：缺失 ≠ 空，unknown ≠ 0。
SOURCE_AVAILABLE = "available"
SOURCE_UNAVAILABLE = "unavailable"
SOURCE_UNKNOWN = "unknown"

#: 几何类型（渲染器据此选择图层构造方式）。
GEOMETRY_POLYGON = "polygon"
GEOMETRY_LINE = "line"
GEOMETRY_POINT = "point"
GEOMETRY_FOOTPRINT = "footprint"      # 真实建筑足迹（Polygon 集合，由聚合服务写入）
GEOMETRY_GRID_CELLS = "grid_cells"    # 网格单元矩形（由 west/south/east/north 生成）
#: **声明型图层**：本身没有几何要素，只用于登记一条必须在图例中出现的事实
#: （例如"非合作监视能力限制"）。渲染器不会为它建图层，因此它既不会画出假要素，
#: 也不会被静默丢掉。
GEOMETRY_NONE = "none"

DEFAULT_CRS = "OGC:CRS84"


@dataclass
class LayerSpec:
    """一个专题图层的**声明**（数据 + 样式键 + 来源说明）。"""

    layer_key: str
    display_name: str
    style_key: str
    geometry_type: str
    source_role: str
    source_path: str | None = None
    source_status: str = SOURCE_UNKNOWN
    source_reason: str = ""
    source_detail: dict = field(default_factory=dict)
    legend_group: str = ""
    legend_visible: bool = True
    feature_count: int = 0
    data: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            "layer_key": str(self.layer_key),
            "display_name": str(self.display_name),
            "style_key": str(self.style_key),
            "geometry_type": str(self.geometry_type),
            "source_role": str(self.source_role),
            "source_path": self.source_path,
            "source_status": str(self.source_status),
            "source_reason": str(self.source_reason),
            "source_detail": deepcopy(self.source_detail),
            "legend_group": str(self.legend_group),
            "legend_visible": bool(self.legend_visible),
            "feature_count": int(self.feature_count or 0),
            "data": deepcopy(self.data),
        }

    @classmethod
    def from_dict(cls, payload):
        payload = payload if isinstance(payload, dict) else {}
        return cls(
            layer_key=str(payload.get("layer_key") or ""),
            display_name=str(payload.get("display_name") or ""),
            style_key=str(payload.get("style_key") or ""),
            geometry_type=str(payload.get("geometry_type") or ""),
            source_role=str(payload.get("source_role") or ""),
            source_path=payload.get("source_path"),
            source_status=str(payload.get("source_status") or SOURCE_UNKNOWN),
            source_reason=str(payload.get("source_reason") or ""),
            source_detail=deepcopy(payload.get("source_detail") or {}),
            legend_group=str(payload.get("legend_group") or ""),
            legend_visible=bool(payload.get("legend_visible", True)),
            feature_count=int(payload.get("feature_count") or 0),
            data=deepcopy(payload.get("data") or {}),
        )


@dataclass
class LabelSpec:
    """一个地图标注（中文 + 白色 halo 由渲染器统一施加）。"""

    kind: str
    text: str
    longitude: float | None = None
    latitude: float | None = None
    geometry: list = field(default_factory=list)
    priority: int = 50
    style_key: str = "label_place"

    def to_dict(self):
        return {
            "kind": str(self.kind),
            "text": str(self.text),
            "longitude": None if self.longitude is None else float(self.longitude),
            "latitude": None if self.latitude is None else float(self.latitude),
            "geometry": deepcopy(self.geometry),
            "priority": int(self.priority),
            "style_key": str(self.style_key),
        }

    @classmethod
    def from_dict(cls, payload):
        payload = payload if isinstance(payload, dict) else {}
        longitude, latitude = payload.get("longitude"), payload.get("latitude")
        return cls(
            kind=str(payload.get("kind") or ""),
            text=str(payload.get("text") or ""),
            longitude=None if longitude is None else float(longitude),
            latitude=None if latitude is None else float(latitude),
            geometry=deepcopy(payload.get("geometry") or []),
            priority=int(payload.get("priority") or 50),
            style_key=str(payload.get("style_key") or "label_place"),
        )


@dataclass
class LegendItem:
    """图例条目：``style_key`` 必须与地图上使用的样式键**完全一致**。"""

    layer_key: str
    style_key: str
    display_name: str
    legend_group: str = ""

    def to_dict(self):
        return {
            "layer_key": str(self.layer_key),
            "style_key": str(self.style_key),
            "display_name": str(self.display_name),
            "legend_group": str(self.legend_group),
        }

    @classmethod
    def from_dict(cls, payload):
        payload = payload if isinstance(payload, dict) else {}
        return cls(
            layer_key=str(payload.get("layer_key") or ""),
            style_key=str(payload.get("style_key") or ""),
            display_name=str(payload.get("display_name") or ""),
            legend_group=str(payload.get("legend_group") or ""),
        )


@dataclass
class AnnotationSpec:
    """图面说明框（多行文本 + 可选边框），相对**地图框**定位。

    用途：CNS 专题图必须把"服务半径只是规划值""Radar 在当前约束下无可行布设""监测点
    只是规划提案"这类**能力限制声明**写在图上，而不是只藏在元数据里。说明框是纯文本
    版面项，不含任何几何，因此不会伪装成要素、也不参与任何业务计算。
    """

    annotation_id: str
    title: str
    lines: list = field(default_factory=list)
    anchor: str = "map_bottom_left"
    width_mm: float = 82.0
    offset_mm: float = 3.4
    border: bool = True
    style_key: str = "annotation_limitation"

    def to_dict(self):
        return {
            "annotation_id": str(self.annotation_id),
            "title": str(self.title),
            "lines": [str(line) for line in (self.lines or [])],
            "anchor": str(self.anchor),
            "width_mm": float(self.width_mm),
            "offset_mm": float(self.offset_mm),
            "border": bool(self.border),
            "style_key": str(self.style_key),
        }

    @classmethod
    def from_dict(cls, payload):
        payload = payload if isinstance(payload, dict) else {}
        return cls(
            annotation_id=str(payload.get("annotation_id") or ""),
            title=str(payload.get("title") or ""),
            lines=[str(line) for line in (payload.get("lines") or [])],
            anchor=str(payload.get("anchor") or "map_bottom_left"),
            width_mm=float(payload.get("width_mm") or 82.0),
            offset_mm=float(payload.get("offset_mm") or 3.4),
            border=bool(payload.get("border", True)),
            style_key=str(payload.get("style_key") or "annotation_limitation"),
        )


@dataclass
class ExtentSpec:
    """图面范围：WGS84 经纬度（渲染器据此设置地图 CRS 与网格）。"""

    west: float
    south: float
    east: float
    north: float
    width_km: float = 0.0
    height_km: float = 0.0

    def as_list(self):
        return [float(self.west), float(self.south), float(self.east), float(self.north)]

    def to_dict(self):
        return {
            "west": float(self.west), "south": float(self.south),
            "east": float(self.east), "north": float(self.north),
            "width_km": float(self.width_km), "height_km": float(self.height_km),
        }

    @classmethod
    def from_dict(cls, payload):
        payload = payload if isinstance(payload, dict) else {}
        return cls(
            west=float(payload.get("west") or 0.0), south=float(payload.get("south") or 0.0),
            east=float(payload.get("east") or 0.0), north=float(payload.get("north") or 0.0),
            width_km=float(payload.get("width_km") or 0.0),
            height_km=float(payload.get("height_km") or 0.0),
        )


@dataclass
class FigureSpec:
    """一张专题成果图的完整、JSON-safe、可审计规格。"""

    template_id: str
    template_version: int
    title: str
    route_id: str
    route_source: str
    extent: ExtentSpec
    extent_mode: str
    layers: list = field(default_factory=list)
    labels: list = field(default_factory=list)
    annotations: list = field(default_factory=list)
    legend_items: list = field(default_factory=list)
    source_status: dict = field(default_factory=dict)
    omitted_layers: list = field(default_factory=list)
    generated_from_revision: int = 0
    schema_version: int = FIGURE_SPEC_SCHEMA_VERSION
    route_geometry: list = field(default_factory=list)
    route_crs: str = DEFAULT_CRS
    route_start: list = field(default_factory=list)
    route_end: list = field(default_factory=list)
    turn_points: list = field(default_factory=list)
    display_thresholds: dict = field(default_factory=dict)
    label_policy: dict = field(default_factory=dict)
    layout: dict = field(default_factory=dict)
    parameters: dict = field(default_factory=dict)
    extent_evidence: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    boundaries: dict = field(default_factory=dict)

    # ---- 序列化 ---------------------------------------------------------------

    def to_dict(self):
        """确定性字典（图层 / 图例 / 标注顺序稳定，便于指纹与审计）。"""

        return {
            "schema_version": int(self.schema_version),
            "template_id": str(self.template_id),
            "template_version": int(self.template_version),
            "title": str(self.title),
            "route_id": str(self.route_id),
            "route_source": str(self.route_source),
            "route_crs": str(self.route_crs),
            "route_geometry": deepcopy(self.route_geometry),
            "route_start": deepcopy(self.route_start),
            "route_end": deepcopy(self.route_end),
            "turn_points": deepcopy(self.turn_points),
            "extent": self.extent.to_dict(),
            "extent_mode": str(self.extent_mode),
            "extent_evidence": deepcopy(self.extent_evidence),
            "layers": [layer.to_dict() for layer in self.layers],
            "labels": [label.to_dict() for label in self.labels],
            "annotations": [item.to_dict() for item in self.annotations],
            "legend_items": [item.to_dict() for item in self.legend_items],
            "source_status": deepcopy(self.source_status),
            "omitted_layers": deepcopy(self.omitted_layers),
            "display_thresholds": deepcopy(self.display_thresholds),
            "label_policy": deepcopy(self.label_policy),
            "layout": deepcopy(self.layout),
            "parameters": deepcopy(self.parameters),
            "warnings": deepcopy(self.warnings),
            "boundaries": deepcopy(self.boundaries),
            "generated_from_revision": int(self.generated_from_revision),
        }

    @classmethod
    def from_dict(cls, payload):
        payload = payload if isinstance(payload, dict) else {}
        return cls(
            schema_version=int(payload.get("schema_version") or FIGURE_SPEC_SCHEMA_VERSION),
            template_id=str(payload.get("template_id") or ""),
            template_version=int(payload.get("template_version") or 0),
            title=str(payload.get("title") or ""),
            route_id=str(payload.get("route_id") or ""),
            route_source=str(payload.get("route_source") or ""),
            route_crs=str(payload.get("route_crs") or DEFAULT_CRS),
            route_geometry=deepcopy(payload.get("route_geometry") or []),
            route_start=deepcopy(payload.get("route_start") or []),
            route_end=deepcopy(payload.get("route_end") or []),
            turn_points=deepcopy(payload.get("turn_points") or []),
            extent=ExtentSpec.from_dict(payload.get("extent")),
            extent_mode=str(payload.get("extent_mode") or ""),
            extent_evidence=deepcopy(payload.get("extent_evidence") or {}),
            layers=[LayerSpec.from_dict(item) for item in payload.get("layers") or []],
            labels=[LabelSpec.from_dict(item) for item in payload.get("labels") or []],
            annotations=[
                AnnotationSpec.from_dict(item) for item in payload.get("annotations") or []
            ],
            legend_items=[LegendItem.from_dict(item) for item in payload.get("legend_items") or []],
            source_status=deepcopy(payload.get("source_status") or {}),
            omitted_layers=deepcopy(payload.get("omitted_layers") or []),
            display_thresholds=deepcopy(payload.get("display_thresholds") or {}),
            label_policy=deepcopy(payload.get("label_policy") or {}),
            layout=deepcopy(payload.get("layout") or {}),
            parameters=deepcopy(payload.get("parameters") or {}),
            warnings=deepcopy(payload.get("warnings") or []),
            boundaries=deepcopy(payload.get("boundaries") or {}),
            generated_from_revision=int(payload.get("generated_from_revision") or 0),
        )

    # ---- 派生 ---------------------------------------------------------------

    def available_layer_keys(self):
        return [
            layer.layer_key for layer in self.layers
            if layer.source_status == SOURCE_AVAILABLE and layer.feature_count > 0
        ]

    def fingerprint(self):
        """规格指纹：同一份 FigureSpec 必然得到同一指纹（内容寻址的输入）。"""

        raw = json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return sha256(raw).hexdigest()

    def summary(self):
        """写入项目 state 的精简摘要（不含逐要素几何，避免快照膨胀）。"""

        return {
            "schema_version": int(self.schema_version),
            "template_id": self.template_id,
            "template_version": int(self.template_version),
            "title": self.title,
            "route_id": self.route_id,
            "route_source": self.route_source,
            "extent": self.extent.to_dict(),
            "extent_mode": self.extent_mode,
            "legend_items": [item.to_dict() for item in self.legend_items],
            "source_status": deepcopy(self.source_status),
            "omitted_layers": deepcopy(self.omitted_layers),
            "display_thresholds": deepcopy(self.display_thresholds),
            "layout": deepcopy(self.layout),
            "parameters": deepcopy(self.parameters),
            "warnings": deepcopy(self.warnings),
            "route_point_count": len(self.route_geometry),
            "turn_point_count": len(self.turn_points),
            "label_count": len(self.labels),
            "annotation_count": len(self.annotations),
            "generated_from_revision": int(self.generated_from_revision),
            "spec_fingerprint": self.fingerprint(),
        }


__all__ = [
    "AnnotationSpec", "DEFAULT_CRS", "FIGURE_SPEC_SCHEMA_VERSION", "ExtentSpec", "FigureSpec",
    "GEOMETRY_FOOTPRINT", "GEOMETRY_GRID_CELLS", "GEOMETRY_LINE", "GEOMETRY_NONE",
    "GEOMETRY_POINT", "GEOMETRY_POLYGON", "LabelSpec", "LayerSpec", "LegendItem",
    "SOURCE_AVAILABLE", "SOURCE_UNAVAILABLE", "SOURCE_UNKNOWN",
]
