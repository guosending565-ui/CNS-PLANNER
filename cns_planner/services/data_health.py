"""Stage-aware health checks. Workspace coverage is pending until P2 defines it."""
from .data_registry import build_registry

LABELS = {"checking": "正在检查数据源", "ready": "数据源就绪", "warning": "数据源不完整", "error": "数据源不可用"}


def build_health(metadata: dict, workspace_bbox=None) -> dict:
    items = []
    for source in build_registry(metadata):
        status, message, checks = "warning", "尚未配置", []
        if source["id"] == "basemap":
            ok = bool(metadata.get("layers")) and not metadata.get("error")
            status, message = ("ready", f"已读取 {len(metadata.get('layers', []))} 个本地图层") if ok else ("error", metadata.get("error") or "QGIS 项目不可用")
            checks = [{"name": "文件与引用可读", "status": "passed" if ok else "failed"}, {"name": "CRS 有效", "status": "passed" if ok else "not_calculated"}, {"name": "覆盖当前工作区", "status": "pending_workspace" if workspace_bbox is None else "not_calculated"}]
        elif source["id"] == "population":
            raster = metadata.get("population", {})
            ok = bool(raster.get("width") and raster.get("bands") and raster.get("crs"))
            status, message = ("ready", f"{raster.get('width')} × {raster.get('height')}，{raster.get('crs')}") if ok else ("error", metadata.get("error") or "人口 GeoTIFF 不可用")
            checks = [{"name": "有效波段", "status": "passed" if raster.get("bands") else "failed"}, {"name": "CRS 有效", "status": "passed" if raster.get("crs") else "failed"}, {"name": "NoData 已识别", "status": "passed" if raster.get("nodata") not in (None, "None") else "warning"}, {"name": "单位与来源时间", "status": "pending_confirmation"}, {"name": "覆盖当前工作区", "status": "pending_workspace" if workspace_bbox is None else "not_calculated"}]
        elif source["id"] == "online_map":
            status, message = ("warning", "已配置；可用性由浏览器瓦片状态单独报告") if source["configured"] else ("warning", "未配置；不阻塞离线核心功能")
        elif source["id"] == "geocoder":
            status, message = ("warning", "已配置 Key；地名搜索权限需独立验证") if source["configured"] else ("warning", "未配置；保留经纬度定位")
        elif source["id"] == "airspace" and source["configured"]:
            status, message = "ready", "由 QGIS 项目提供；语义分类待 P2 检查"
        required = source["required_stage"] == "P1"
        items.append({**source, "status": status, "message": message, "required": required, "checks": checks})
    required_errors = [item for item in items if item["required"] and item["status"] == "error"]
    overall = "error" if required_errors else "warning" if any(item["status"] == "warning" or any(c["status"] != "passed" for c in item["checks"]) for item in items) else "ready"
    return {"status": overall, "label": LABELS[overall], "stage": "P1", "workspace_defined": workspace_bbox is not None, "items": items,
            "notes": ["在线地图和地名服务分别检查，均不阻塞离线核心功能", "工作区尚未定义，空间覆盖状态保留为待检查"] if workspace_bbox is None else []}
