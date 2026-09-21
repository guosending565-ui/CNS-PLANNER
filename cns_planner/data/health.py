"""Stage-aware source health derived from the unified registry."""

from .registry import build_registry

LABELS = {"checking": "正在检查数据源", "ready": "数据源就绪", "warning": "数据源不完整", "error": "数据源不可用"}

#: 数据集自带的"非运行数据"标记。样例 / 阶段性验证数据必须显式声明，健康检查据此
#: 拒绝把这种数据源报成 ready，绝不因为文件可读就当作生产数据。
NON_OPERATIONAL_METADATA_KEYS = ("CNS_NOT_OPERATIONAL_DATA", "CNS_STAGE_ASSUMPTION")
NON_OPERATIONAL_VALUE = "true"

#: 100m 全国人口栅格的最小可信像元数。低于此值只可能是截取的样例瓦片，
#: 而不是真实的区域数据（样例 R0003 为 63 × 190 = 11,970 像元；真实产品为
#: 73530 × 45337）。这个下界只作用于已知产品尺寸的 population 栅格，
#: 不对 terrain / terrain_dtm 或测试用的小型夹具施加任何要求。
MIN_POPULATION_PIXELS = 1_000_000


def non_operational_markers(source_metadata) -> list:
    """数据集元数据里显式声明的非运行数据标记（无标记则返回空列表）。"""

    if not isinstance(source_metadata, dict):
        return []
    markers = []
    for key in NON_OPERATIONAL_METADATA_KEYS:
        value = source_metadata.get(key)
        if value in (None, ""):
            continue
        markers.append(f"{key}={value}")
    return markers


def raster_non_operational_reasons(source_id, raster) -> list:
    """一个栅格数据源为什么不能算作运行数据（没有问题时返回空列表）。

    只依据已加载的栅格元数据判断，不重新读取文件、不改变任何映射语义。
    """

    reasons = non_operational_markers((raster or {}).get("source_metadata"))
    if source_id == "population":
        width = (raster or {}).get("width")
        height = (raster or {}).get("height")
        if isinstance(width, int) and isinstance(height, int):
            if width * height < MIN_POPULATION_PIXELS:
                reasons.append(
                    f"像元数 {width}×{height} 低于 100m 全国人口栅格的最小可信规模"
                    f"（<{MIN_POPULATION_PIXELS}），疑似样例瓦片"
                )
    return reasons


def build_health(metadata: dict, workspace_bbox=None) -> dict:
    items = []
    for source in build_registry(metadata):
        status, message, checks = "warning", "尚未配置", []
        source_id = source["id"]
        if source_id == "basemap":
            ok = bool(metadata.get("layers")) and not metadata.get("error")
            status, message = (("ready", f"已读取 {len(metadata.get('layers', []))} 个本地图层") if ok else ("error", metadata.get("error") or "QGIS 项目不可用"))
            checks = _spatial_checks(ok, workspace_bbox)
        elif source_id in ("population", "terrain", "terrain_dtm"):
            raster = metadata.get(source_id, {})
            ok = bool(raster.get("width") and raster.get("bands") and raster.get("crs"))
            label = "人口 GeoTIFF" if source_id == "population" else "FABDEM DTM" if source_id == "terrain_dtm" else "地形 DSM"
            # 样例 / 阶段性验证数据永远不能报 ready：文件可读不代表它是运行数据。
            non_operational = raster_non_operational_reasons(source_id, raster) if ok else []
            if non_operational:
                status = "warning"
                message = f"{label} 不是运行数据：" + "；".join(non_operational)
            else:
                status, message = (("ready", f"{raster.get('width')} × {raster.get('height')}，{raster.get('crs')}") if ok else ("error" if source["required"] else "warning", metadata.get("error") or f"{label} 不可用"))
            checks = [
                {"name": "有效波段", "status": "passed" if raster.get("bands") else "failed"},
                {"name": "CRS 有效", "status": "passed" if raster.get("crs") else "failed"},
                {"name": "NoData 已识别", "status": "passed" if raster.get("nodata") not in (None, "None") else "warning"},
                {"name": "单位与来源", "status": _verification_status(source)},
                {"name": "运行数据（非样例）", "status": "failed" if non_operational else ("passed" if raster.get("source_metadata") else "not_calculated")},
                {"name": "覆盖当前工作区", "status": "pending_workspace" if workspace_bbox is None else "not_calculated"},
            ]
            if source_id == "terrain_dtm":
                checks.extend([
                    {"name": "DTM 数据类型", "status": "passed" if raster.get("dtype") else "failed"},
                    {"name": "范围有效", "status": "passed" if raster.get("extent") else "failed"},
                    {"name": "垂向基准", "status": "passed" if raster.get("vertical_status") == "confirmed" else "pending_confirmation"},
                ])
        elif source_id in ("buildings", "building_grid"):
            vector = (metadata.get("vector_sources") or {}).get(source_id) or {}
            ok = vector.get("status") == "passed" and vector.get("feature_count") is not None
            status, message = (
                ("ready", f"{vector.get('layer')} · {vector.get('feature_count', 0):,} 条 · {vector.get('crs')}")
                if ok else ("warning", "未配置或内容校验未通过")
            )
            checks = [
                {"name": "GeoPackage 可读", "status": "passed" if ok else "failed"},
                {"name": "Polygon geometry", "status": "passed" if "POLYGON" in str(vector.get("geometry_type", "")).upper() else "failed"},
                {"name": "CRS 有效", "status": "passed" if vector.get("crs") else "failed"},
                {"name": "关键字段", "status": "passed" if vector.get("fields") else "failed"},
                {"name": "空间索引", "status": "passed" if vector.get("spatial_index") else "warning"},
                {"name": "覆盖当前工作区", "status": "pending_workspace" if workspace_bbox is None else "not_calculated"},
            ]
        elif source_id == "online_map":
            status, message = ("warning", "已配置；可用性由浏览器瓦片状态单独报告") if source["configured"] else ("warning", "未配置；不阻塞离线核心功能")
        elif source_id == "geocoder":
            status, message = ("warning", "已配置 Key；地名搜索权限需独立验证") if source["configured"] else ("warning", "未配置；保留经纬度定位")
        elif source_id == "airspace" and source["configured"]:
            status, message = "ready", "由 QGIS 项目提供；语义分类需按图层属性确认"
        elif source_id in ("reference_landing_sites", "reference_routes"):
            collection_status = source.get("collection_status", "not_calculated")
            if str(source.get("location") or "").lower().endswith(".et"):
                status, message = "warning", "已检测到 ET；requires_xlsx_or_csv_conversion"
            elif collection_status == "passed":
                suffix = f"；航路点 {source.get('point_count', 0)} 个" if source_id == "reference_routes" else ""
                status, message = "ready", f"已加载 {source.get('item_count', 0)} 条{suffix}"
            elif source["configured"]:
                status, message = "warning", f"已配置但可用记录为 0；{collection_status}"
            else:
                status, message = "warning", "未配置参考来源"
        elif source_id in ("aircraft", "devices", "existing_cns", "candidate_sites") and source["configured"]:
            status, message = "ready", f"已加载 {source.get('item_count', 0)} 条标准记录"
        item = {**source, "status": status, "message": message, "checks": checks}
        item["health"], item["coverage"] = status, "pending_workspace" if workspace_bbox is None else "not_calculated"
        items.append(item)
    required_errors = [item for item in items if item["required"] and item["status"] == "error"]
    overall = "error" if required_errors else "warning" if any(item["status"] == "warning" or any(check["status"] != "passed" for check in item["checks"]) for item in items) else "ready"
    return {
        "status": overall, "label": LABELS[overall], "stage": "数据配置",
        "workspace_defined": workspace_bbox is not None, "items": items,
        "notes": ["在线地图和地名服务分别检查，均不阻塞离线核心功能", "工作区尚未定义，空间覆盖状态保留为待检查"] if workspace_bbox is None else [],
    }


def _spatial_checks(ok, workspace_bbox):
    return [
        {"name": "文件与引用可读", "status": "passed" if ok else "failed"},
        {"name": "CRS 有效", "status": "passed" if ok else "not_calculated"},
        {"name": "覆盖当前工作区", "status": "pending_workspace" if workspace_bbox is None else "not_calculated"},
    ]


def _verification_status(source):
    verification = source.get("verification") or {}
    status = verification.get("status") if isinstance(verification, dict) else None
    return "passed" if str(status or "").startswith("verified") else "pending_confirmation"
