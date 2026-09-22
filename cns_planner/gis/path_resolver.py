"""Unified read-only source-path resolution, existence and format validation.

Data Source Center、source loader、建筑足迹读取共用这一套规则，避免出现三套互相矛盾的
路径判断（历史缺陷：建筑源配置成存在的 ``.qgz``/``.shp``/``.geojson`` 时，加载器只接受
``.gpkg`` 并直接抛错，于是"路径存在但状态异常"）。

本模块只回答四件事：

* 这个配置值展开后指向哪个本地路径（``~`` 与环境变量会被展开）；
* 该路径是否存在、是否是**具体文件**（目录不算）；
* 该扩展名是否被这个来源角色接受；
* 不可用时给出可直接展示给用户的中文原因。

它**不读取文件内容**、**不缓存结果**、**不改写配置**；每次调用都重新 stat 文件，
因此不会把上一次的旧状态带进当前界面。
"""

from __future__ import annotations

import os
from pathlib import Path

#: QGIS 工程文件（.qgz 是 zip 归档，.qgs 是纯 XML）。
QGIS_PROJECT_FORMATS = (".qgz", ".qgs")
#: 栅格来源（人口 / DSM / FABDEM DTM）。
RASTER_FORMATS = (".tif", ".tiff")
#: 矢量来源：GeoPackage / Shapefile / GeoJSON。``.json`` 只作为 GeoJSON 的别名接受。
VECTOR_FORMATS = (".gpkg", ".shp", ".geojson", ".json")
#: 建筑类来源既可以是矢量数据本身，也可以是一个引用了建筑图层的 QGIS 工程。
BUILDING_FORMATS = (".gpkg", ".shp", ".geojson", ".json", ".qgz", ".qgs")

#: 每个来源角色接受的文件格式。``()`` 表示"不是单一本地文件"（在线服务 / 目录型来源）。
SOURCE_FORMATS: dict[str, tuple[str, ...]] = {
    "basemap": QGIS_PROJECT_FORMATS,
    "population": RASTER_FORMATS,
    "terrain": RASTER_FORMATS,
    "terrain_dtm": RASTER_FORMATS,
    "buildings": BUILDING_FORMATS,
    "building_grid": BUILDING_FORMATS,
    "property_exposure": RASTER_FORMATS + VECTOR_FORMATS,
    "obstacles": VECTOR_FORMATS + (".csv",),
    "infrastructure": VECTOR_FORMATS,
    "towers": VECTOR_FORMATS + (".csv", ".xlsx", ".xlsm"),
    "traffic": VECTOR_FORMATS + (".csv",),
    "vertiports": VECTOR_FORMATS + (".csv",),
    "reference_landing_sites": (".xlsx", ".csv", ".et"),
    "reference_routes": (".csv", ".xlsx", ".geojson", ".json", ".et"),
    "existing_cns": (".json", ".csv", ".geojson"),
    "candidate_sites": (".json", ".csv", ".geojson"),
    "equipment_reference_catalog": (),
}

#: 角色 → 界面语言标签（只用于拼装提示文本）。
ROLE_LABELS = {
    "basemap": "基础地图 / QGIS 项目",
    "population": "人口 GeoTIFF",
    "terrain": "地形 DSM",
    "terrain_dtm": "FABDEM DTM",
    "buildings": "建筑单体",
    "building_grid": "建筑环境网格",
    "property_exposure": "财产暴露",
    "obstacles": "铁塔与高塔",
    "infrastructure": "关键基础设施",
    "towers": "通信铁塔",
    "traffic": "运行交通",
    "vertiports": "起降设施",
    "reference_landing_sites": "参考起降点",
    "reference_routes": "真实参考航线",
    "existing_cns": "既有 CNS 设施",
    "candidate_sites": "候选站址",
}

#: 状态取值（稳定词汇，前端与健康检查直接读）。
STATUS_NOT_CONFIGURED = "not_configured"
STATUS_PATH_MISSING = "path_missing"
STATUS_NOT_A_FILE = "not_a_file"
STATUS_UNSUPPORTED_FORMAT = "unsupported_format"
STATUS_OK = "ok"


def role_label(role) -> str:
    key = str(role or "")
    return ROLE_LABELS.get(key, key or "数据源")


def supported_formats(role) -> tuple[str, ...]:
    """该角色接受的文件格式（未知角色不做格式限制，返回空元组）。"""

    return tuple(SOURCE_FORMATS.get(str(role or ""), ()))


def format_hint(role) -> str:
    formats = supported_formats(role)
    if not formats:
        return "文件类型不限"
    return "文件类型：" + " / ".join(formats)


def expand_path_value(value):
    """把配置值展开成路径字符串；空值返回 ``None``。

    只做纯字符串展开（``~``、``$VAR``、``${VAR}``、引号），不解析符号链接、不访问文件系统。
    """

    if value is None:
        return None
    text = str(value).strip().strip('"').strip("'")
    if not text:
        return None
    expanded = os.path.expandvars(os.path.expanduser(text))
    return expanded or None


def _format_ok(suffix, formats):
    if not formats:
        return True
    return suffix in formats


def resolve_source_path(value, *, role=None) -> dict:
    """解析一个来源配置值，返回**只读**判定结果（每次调用都重新 stat）。"""

    formats = supported_formats(role)
    record = {
        "role": str(role) if role else None,
        "label": role_label(role),
        "raw": None if value is None else str(value),
        "path": None,
        "resolved": None,
        "exists": False,
        "is_file": False,
        "is_directory": False,
        "suffix": None,
        "supported_formats": list(formats),
        "format_supported": None,
        "status": STATUS_NOT_CONFIGURED,
        "reason": "尚未配置",
    }
    text = expand_path_value(value)
    if text is None:
        return record

    record["path"] = text
    try:
        candidate = Path(text)
    except (OSError, ValueError) as exc:
        record["status"] = STATUS_PATH_MISSING
        record["reason"] = f"路径无法解析：{exc}"
        return record

    try:
        record["resolved"] = str(candidate.resolve())
    except (OSError, ValueError):
        record["resolved"] = str(candidate)

    try:
        record["is_directory"] = candidate.is_dir()
        record["exists"] = candidate.exists()
        record["is_file"] = candidate.is_file()
    except OSError as exc:
        record["status"] = STATUS_PATH_MISSING
        record["reason"] = f"路径不可访问：{exc}"
        return record

    suffix = candidate.suffix.lower()
    record["suffix"] = suffix or None
    record["format_supported"] = _format_ok(suffix, formats)

    if not record["exists"]:
        record["status"] = STATUS_PATH_MISSING
        record["reason"] = f"{role_label(role)}：配置的路径不存在"
        return record
    if not record["is_file"]:
        record["status"] = STATUS_NOT_A_FILE
        record["reason"] = f"{role_label(role)}：必须指向具体文件，当前是目录"
        return record
    if not record["format_supported"]:
        record["status"] = STATUS_UNSUPPORTED_FORMAT
        record["reason"] = (
            f"{role_label(role)}：不支持 {suffix or '无扩展名'}，"
            f"支持 {', '.join(formats)}"
        )
        return record

    record["status"] = STATUS_OK
    record["reason"] = None
    return record


def check_source_paths(paths) -> dict:
    """启动 / 重新加载时对全部已配置路径做一次 ``exists`` + ``is_file`` 校验。

    返回值只描述"当前这次检查"看到的事实，不含任何历史状态，因此可以直接替换旧结果，
    不会把上一次的判定缓存到界面里。
    """

    source = paths if isinstance(paths, dict) else {}
    items = {}
    problems = []
    configured = 0
    ok = 0
    for role, value in source.items():
        record = resolve_source_path(value, role=role)
        items[str(role)] = record
        if record["path"] is None:
            continue
        configured += 1
        if record["status"] == STATUS_OK:
            ok += 1
        else:
            problems.append({
                "role": str(role),
                "label": record["label"],
                "status": record["status"],
                "path": record["path"],
                "reason": record["reason"],
            })
    status = "ok" if not problems else ("empty" if not configured else "warning")
    return {
        "status": status,
        "configured_count": configured,
        "ok_count": ok,
        "problem_count": len(problems),
        "problems": problems,
        "items": items,
    }


def vector_format_supported(path) -> bool:
    """给定路径是否是受支持的矢量格式（供建筑足迹读取入口复用）。"""

    suffix = Path(str(path or "")).suffix.lower()
    return suffix in VECTOR_FORMATS


__all__ = [
    "BUILDING_FORMATS", "QGIS_PROJECT_FORMATS", "RASTER_FORMATS", "ROLE_LABELS",
    "SOURCE_FORMATS", "STATUS_NOT_CONFIGURED", "STATUS_NOT_A_FILE", "STATUS_OK",
    "STATUS_PATH_MISSING", "STATUS_UNSUPPORTED_FORMAT", "VECTOR_FORMATS",
    "check_source_paths", "expand_path_value", "format_hint", "resolve_source_path",
    "role_label", "supported_formats", "vector_format_supported",
]
