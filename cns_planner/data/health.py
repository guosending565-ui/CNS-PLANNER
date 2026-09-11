"""Stage-aware source health derived from the unified registry."""

from .registry import build_registry

LABELS = {"checking": "正在检查数据源", "ready": "数据源就绪", "warning": "数据源不完整", "error": "数据源不可用"}


def build_health(metadata: dict, workspace_bbox=None) -> dict:
    items = []
    for source in build_registry(metadata):
        status, message, checks = "warning", "尚未配置", []
        source_id = source["id"]
        if source_id == "basemap":
            ok = bool(metadata.get("layers")) and not metadata.get("error")
            status, message = (("ready", f"已读取 {len(metadata.get('layers', []))} 个本地图层") if ok else ("error", metadata.get("error") or "QGIS 项目不可用"))
            checks = _spatial_checks(ok, workspace_bbox)
        elif source_id in ("population", "terrain"):
            raster = metadata.get(source_id, {})
            ok = bool(raster.get("width") and raster.get("bands") and raster.get("crs"))
            label = "人口 GeoTIFF" if source_id == "population" else "地形 DEM"
            status, message = (("ready", f"{raster.get('width')} × {raster.get('height')}，{raster.get('crs')}") if ok else ("error" if source["required"] else "warning", metadata.get("error") or f"{label} 不可用"))
            checks = [
                {"name": "有效波段", "status": "passed" if raster.get("bands") else "failed"},
                {"name": "CRS 有效", "status": "passed" if raster.get("crs") else "failed"},
                {"name": "NoData 已识别", "status": "passed" if raster.get("nodata") not in (None, "None") else "warning"},
                {"name": "单位与来源", "status": _verification_status(source)},
                {"name": "覆盖当前工作区", "status": "pending_workspace" if workspace_bbox is None else "not_calculated"},
            ]
        elif source_id == "online_map":
            status, message = ("warning", "已配置；可用性由浏览器瓦片状态单独报告") if source["configured"] else ("warning", "未配置；不阻塞离线核心功能")
        elif source_id == "geocoder":
            status, message = ("warning", "已配置 Key；地名搜索权限需独立验证") if source["configured"] else ("warning", "未配置；保留经纬度定位")
        elif source_id == "airspace" and source["configured"]:
            status, message = "ready", "由 QGIS 项目提供；语义分类需按图层属性确认"
        elif source_id in ("aircraft", "devices", "existing_cns", "candidate_sites") and source["configured"]:
            status, message = "ready", f"已加载 {source.get('item_count', 0)} 条标准记录"
        item = {**source, "status": status, "message": message, "checks": checks}
        item["health"], item["coverage"] = status, "pending_workspace" if workspace_bbox is None else "not_calculated"
        items.append(item)
    required_errors = [item for item in items if item["required"] and item["status"] == "error"]
    overall = "error" if required_errors else "warning" if any(item["status"] == "warning" or any(check["status"] != "passed" for check in item["checks"]) for item in items) else "ready"
    return {
        "status": overall, "label": LABELS[overall], "stage": "P1",
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
