"""Read a QGIS project (``.qgz`` / ``.qgs``) into a plain, JSON-safe layer inventory.

为什么需要它：真实建筑数据是以 **QGIS 工程**（``舟山建筑数据.qgz``）交付的，工程本身不包含
几何，只引用外部的 GeoPackage/Shapefile（例如 ``./processed/zhoushan_buildings.gpkg|
layername=buildings``）。浏览器不能读 ``.qgz``，所以由后端解析工程、把引用的矢量数据源解析成
绝对路径，再交给后续的只读读取器。

本模块**只解析 XML**：不加载 QGIS、不打开数据集、不写任何文件。``.qgz`` 是 zip 归档
（内含一个 ``.qgs`` XML + 样式库），``.qgs`` 是纯 XML，两者都用标准库处理。
"""

from __future__ import annotations

from pathlib import Path
import zipfile
from xml.etree import ElementTree

from .path_resolver import QGIS_PROJECT_FORMATS, VECTOR_FORMATS

#: 远程/在线数据源特征：这些 datasource 不是本地文件，绝不能当成路径解析。
_REMOTE_MARKERS = ("type=xyz", "type=wms", "type=wcs", "type=wmts", "url=", "http://", "https://")

#: 建筑**单体足迹**图层名关键字（只用于**排序**，不做任何业务判定）。
BUILDING_LAYER_KEYWORDS = ("buildings", "footprint", "footprints", "lod1", "单体", "建筑单体", "building")
#: 建筑单体选择时要排除的图层：``building_grid`` 是**聚合事实表**，不是单体足迹。
BUILDING_LAYER_EXCLUDE_KEYWORDS = ("grid", "网格")
#: 建筑环境网格类图层名关键字。
BUILDING_GRID_LAYER_KEYWORDS = ("building_grid", "buildinggrid", "grid_l8", "建筑环境网格", "建筑网格")


def _local_name(tag) -> str:
    text = str(tag or "")
    return text.rsplit("}", 1)[-1]


def read_project_xml(project_path):
    """返回 ``(xml_bytes, member_name, project_dir)``；``.qgz`` 会自动解压出 ``.qgs``。"""

    source = Path(project_path)
    if not source.is_file():
        raise ValueError("QGIS 工程文件不存在")
    suffix = source.suffix.lower()
    if suffix not in QGIS_PROJECT_FORMATS:
        raise ValueError("QGIS 工程必须是 .qgz 或 .qgs 文件")
    if suffix == ".qgs":
        return source.read_bytes(), source.name, source.parent
    with zipfile.ZipFile(source) as archive:
        members = [name for name in archive.namelist() if name.lower().endswith(".qgs")]
        if not members:
            raise ValueError(".qgz 归档中没有 .qgs 工程文件")
        member = sorted(members, key=len)[0]
        return archive.read(member), member, source.parent


def _parse_datasource(datasource, project_dir):
    """把一个 QGIS datasource 字符串解析成本地路径 + 图层名。

    ``./processed/x.gpkg|layername=buildings`` → 路径 + ``buildings``。
    远程服务、空 datasource 返回 ``(None, None, reason)``。
    """

    text = str(datasource or "").strip()
    if not text:
        return None, None, "datasource_empty"
    lowered = text.lower()
    if any(marker in lowered for marker in _REMOTE_MARKERS):
        return None, None, "remote_or_service_datasource"
    if "|" in text:
        head, _, tail = text.partition("|")
        options = {}
        for chunk in tail.split("|"):
            key, _, value = chunk.partition("=")
            options[key.strip().lower()] = value.strip()
    else:
        head, options = text, {}
    head = head.strip()
    if not head:
        return None, None, "datasource_empty"
    if head.lower().startswith(("memory:", "point?crs=", "polygon?crs=", "linestring?crs=")):
        return None, None, "non_file_datasource"
    candidate = Path(head)
    if not candidate.is_absolute():
        candidate = (project_dir / candidate)
    try:
        resolved = candidate.resolve()
    except (OSError, ValueError):
        resolved = candidate
    return resolved, options.get("layername") or options.get("layer"), None


def read_project_layers(project_path) -> dict:
    """解析 QGIS 工程，返回只读图层清单（顺序与工程图层树一致）。"""

    payload, member, project_dir = read_project_xml(project_path)
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise ValueError(f"无法解析 QGIS 工程 XML：{exc}") from exc

    tree_names = {}
    for node in root.iter():
        if _local_name(node.tag) != "layer-tree-layer":
            continue
        layer_id = node.get("id") or ""
        if layer_id:
            tree_names[layer_id] = node.get("name") or ""

    layers = []
    for node in root.iter():
        if _local_name(node.tag) != "maplayer":
            continue
        fields = {}
        for child in node:
            fields[_local_name(child.tag)] = (child.text or "").strip()
        layer_type = (node.get("type") or "").strip().lower()
        geometry = (node.get("geometry") or "").strip()
        wkb_type = (node.get("wkbType") or "").strip()
        datasource = fields.get("datasource") or ""
        path, layer_name, reason = _parse_datasource(datasource, project_dir)
        suffix = path.suffix.lower() if path else None
        is_vector = layer_type == "vector"
        is_local_file = bool(path) and suffix in VECTOR_FORMATS
        layers.append({
            "id": node.get("id") or fields.get("id") or None,
            "name": fields.get("layername") or tree_names.get(node.get("id") or "", "") or "",
            "tree_name": tree_names.get(node.get("id") or "", ""),
            "layer_type": layer_type or "unknown",
            "provider": fields.get("provider") or None,
            "geometry_type": geometry or None,
            "wkb_type": wkb_type or None,
            "datasource": datasource or None,
            "path": str(path) if path else None,
            "layer_name": layer_name,
            "format": suffix,
            "is_local_vector_file": is_local_file,
            "is_polygon": bool(is_vector and "POLYGON" in (geometry or wkb_type).upper()),
            "datasource_reason": reason,
            "exists": bool(path and path.is_file()) if path else False,
        })
    return {
        "path": str(Path(project_path).resolve()),
        "member": member,
        "project_dir": str(project_dir),
        "type_counts": {
            kind: sum(1 for item in layers if item["layer_type"] == kind)
            for kind in sorted({item["layer_type"] for item in layers})
        },
        "layers": layers,
    }


def _layer_haystack(layer) -> str:
    return " ".join(
        str(layer.get(key) or "") for key in ("name", "tree_name", "layer_name", "path")
    ).lower()


def _keyword_rank(layer, keywords):
    haystack = _layer_haystack(layer)
    for index, keyword in enumerate(keywords):
        if keyword.lower() in haystack:
            return index
    return len(keywords) + 1


def _matches_any(layer, keywords) -> bool:
    haystack = _layer_haystack(layer)
    return any(keyword.lower() in haystack for keyword in keywords)


def find_polygon_layer(project_path, keywords=BUILDING_LAYER_KEYWORDS, *,
                       exclude_keywords=(), require_exists=True) -> dict | None:
    """在工程里挑一个本地面状矢量图层（按关键字排序，只是选层启发式）。

    ``exclude_keywords`` 用来把**聚合事实表**（``building_grid``）从**单体足迹**里排除掉：
    两者都是 Polygon，只按 "building" 匹配会选错图层。

    工程文件不存在 / 无法解析时返回 ``None``（这是"挑不出图层"，不是调用错误）；
    需要区分原因时请用 :func:`resolve_vector_layer_source`。
    """

    try:
        inventory = read_project_layers(project_path)
    except (OSError, ValueError, zipfile.BadZipFile):
        return None
    candidates = [
        layer for layer in inventory["layers"]
        if layer["is_local_vector_file"] and layer["is_polygon"]
        and (layer["exists"] or not require_exists)
    ]
    if exclude_keywords:
        preferred = [layer for layer in candidates if not _matches_any(layer, exclude_keywords)]
        # 只有排除后仍有候选才真正排除，避免把唯一可用的图层排除掉。
        candidates = preferred or candidates
    if not candidates:
        return None
    candidates.sort(key=lambda layer: (_keyword_rank(layer, keywords), str(layer.get("path"))))
    selected = dict(candidates[0])
    selected["project_path"] = inventory["path"]
    selected["selection_reason"] = (
        "keyword_match" if _keyword_rank(selected, keywords) <= len(keywords) else "first_polygon_layer"
    )
    return selected


def resolve_vector_layer_source(value, *, role="buildings", keywords=None,
                                exclude_keywords=None) -> dict:
    """把配置值解析成一个"可读取的矢量图层"描述（qgz / gpkg / shp / geojson 都接受）。

    返回 ``{"ok": bool, ...}``；失败时带 ``status`` 与中文 ``reason``，绝不抛异常给调用方。
    """

    if keywords is None:
        keywords = BUILDING_GRID_LAYER_KEYWORDS if role == "building_grid" else BUILDING_LAYER_KEYWORDS
    if exclude_keywords is None:
        exclude_keywords = () if role == "building_grid" else BUILDING_LAYER_EXCLUDE_KEYWORDS
    path = Path(str(value)) if value else None
    if path is None or not str(value).strip():
        return {"ok": False, "status": "not_configured", "reason": f"{role} 尚未配置", "value": None}
    suffix = path.suffix.lower()
    if not path.is_file():
        return {"ok": False, "status": "path_missing", "reason": "配置的路径不存在", "value": str(path)}
    if suffix in (".qgz", ".qgs"):
        try:
            layer = find_polygon_layer(path, keywords, exclude_keywords=exclude_keywords)
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            return {
                "ok": False, "status": "project_unreadable",
                "reason": f"无法解析 QGIS 工程：{exc}", "value": str(path),
            }
        if layer is None:
            return {
                "ok": False, "status": "no_polygon_layer",
                "reason": "QGIS 工程中没有可用的本地 Polygon 矢量图层",
                "value": str(path),
            }
        return {
            "ok": True, "status": "ok", "reason": None, "value": str(path),
            "source": "qgis_project",
            "project_path": str(path), "path": layer.get("path"),
            "layer_name": layer.get("layer_name"), "format": layer.get("format"),
            "geometry_type": layer.get("geometry_type"), "layer_title": layer.get("name"),
            "selection_reason": layer.get("selection_reason"),
            "exists": bool(layer.get("exists")),
        }
    if suffix in VECTOR_FORMATS:
        return {
            "ok": True, "status": "ok", "reason": None, "value": str(path),
            "source": "vector_dataset",
            "project_path": None, "path": str(path), "layer_name": None, "format": suffix,
            "geometry_type": None, "layer_title": path.name, "selection_reason": "configured_dataset",
            "exists": True,
        }
    return {
        "ok": False, "status": "unsupported_format",
        "reason": f"不支持 {suffix or '无扩展名'}；支持 {', '.join(sorted(VECTOR_FORMATS | set(QGIS_PROJECT_FORMATS)))}",
        "value": str(path),
    }


def project_layer_summary(project_path, *, role="buildings") -> dict:
    """数据源中心用的只读摘要：工程里有哪些本地矢量图层、选中了哪一个。"""

    try:
        inventory = read_project_layers(project_path)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        return {"status": "unreadable", "reason": str(exc), "layers": []}
    local = [layer for layer in inventory["layers"] if layer["is_local_vector_file"]]
    return {
        "status": "passed",
        "member": inventory["member"],
        "project_dir": inventory["project_dir"],
        "type_counts": inventory["type_counts"],
        "local_vector_count": len(local),
        "local_vector_layers": [
            {
                "name": layer["name"], "geometry_type": layer["geometry_type"],
                "path": layer["path"], "layer_name": layer["layer_name"],
                "format": layer["format"], "exists": layer["exists"],
            }
            for layer in local
        ],
        "selection": resolve_vector_layer_source(project_path, role=role),
    }


__all__ = [
    "BUILDING_GRID_LAYER_KEYWORDS", "BUILDING_LAYER_EXCLUDE_KEYWORDS",
    "BUILDING_LAYER_KEYWORDS", "find_polygon_layer", "project_layer_summary",
    "read_project_layers", "read_project_xml", "resolve_vector_layer_source",
]
