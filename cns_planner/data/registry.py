"""Single extensible registry behind the data-source center."""

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class SourceDefinition:
    id: str
    name: str
    category: str
    type: str
    formats: str
    purpose: str
    required_stage: str | None = None
    editable_now: bool = False
    path_key: str | None = None
    source_type: str = "manual"


DEFINITIONS = (
    SourceDefinition("basemap", "QGIS 项目", "基础地图", "file", "QGZ / QGS", "适飞空域及基础 GIS", "P1", True, "basemap", "real"),
    SourceDefinition("online_map", "天地图底图与中文注记", "在线服务", "service", "URL / API Key", "在线地图显示", source_type="real"),
    SourceDefinition("geocoder", "天地图地名搜索", "在线服务", "service", "API Key", "地名搜索定位", source_type="real"),
    SourceDefinition("airspace", "空域", "风险环境", "qgis_layer", "GPKG / SHP / GeoJSON / QGIS", "硬约束与空域属性", source_type="real"),
    SourceDefinition("population", "人口暴露", "风险环境", "raster", "GeoTIFF", "人员暴露与相对风险", "P1", True, "population", "real"),
    SourceDefinition("terrain", "DEM / DSM", "风险环境", "raster", "GeoTIFF", "高程、地形起伏与剖面", None, True, "terrain", "real"),
    SourceDefinition("terrain_dtm", "FABDEM DTM", "三维建筑环境", "raster", "GeoTIFF", "建筑地面正高与屋顶正高；不替换 GLO-30 DSM", None, True, "terrain_dtm", "real"),
    SourceDefinition(
        "buildings",
        "建筑单体与高度",
        "三维建筑环境",
        "vector",
        "GPKG / SHP / GeoJSON",
        "独立三维建筑障碍与建筑净空评估",
        path_key="buildings",
        source_type="real",
    ),
    SourceDefinition(
        "building_grid",
        "建筑环境网格（MH/T L8）",
        "三维建筑环境",
        "vector",
        "GPKG",
        "建筑密度、建筑高度热力图与网格风险输入",
        path_key="building_grid",
        source_type="real",
    ),
    SourceDefinition("property_exposure", "财产暴露", "风险环境", "raster_or_vector", "Raster / Vector", "财产风险接口", path_key="property_exposure"),
    SourceDefinition("obstacles", "铁塔与高塔", "设施", "vector_or_table", "CSV / GPKG / SHP", "避障与共址候选", path_key="obstacles"),
    SourceDefinition("infrastructure", "关键基础设施", "设施", "vector", "GPKG / SHP / GeoJSON", "基础设施暴露", path_key="infrastructure"),
    SourceDefinition("towers", "通信铁塔", "设施", "vector_or_table", "CSV / XLSX / GPKG / SHP", "CNS共址候选", path_key="towers"),
    SourceDefinition("traffic", "无人机运行交通", "运行", "simulation_or_trajectory", "JSON / CSV / GeoJSON", "交通暴露", path_key="traffic", source_type="synthetic"),
    SourceDefinition("vertiports", "起降设施", "设施", "vector_or_table", "CSV / GPKG / GeoJSON", "航路节点", path_key="vertiports"),
    SourceDefinition("reference_landing_sites", "参考起降点", "参考数据", "reference_collection", "XLSX / CSV", "人工选择后才能加入项目节点", path_key="reference_landing_sites", source_type="real"),
    SourceDefinition("reference_routes", "真实参考航线", "参考数据", "reference_collection", "CSV / XLSX / GeoJSON", "只读展示，不自动进入规划", path_key="reference_routes", source_type="real"),
    SourceDefinition("aircraft", "飞行器库", "业务库", "catalog", "JSON", "运行规则与可靠性", path_key="aircraft", source_type="synthetic"),
    SourceDefinition("devices", "CNS 设备库", "业务库", "catalog", "JSON", "布站与覆盖", path_key="devices", source_type="synthetic"),
    SourceDefinition("equipment_reference_catalog", "真实设备资料库", "参考数据", "reference_catalog", "Canonical JSON（离线整理自 DOCX/PDF/XLSX）", "来源参数查阅；不自动参与规划", path_key="equipment_reference_catalog", source_type="real"),
    SourceDefinition("existing_cns", "既有 CNS 设施", "设施", "vector_or_table", "JSON / CSV / GeoJSON", "改造、共址和已有覆盖", path_key="existing_cns"),
    SourceDefinition("candidate_sites", "候选站址", "设施", "vector_or_table", "JSON / CSV / GeoJSON", "CNS规划候选", path_key="candidate_sites"),
)


def build_registry(metadata: dict) -> list[dict]:
    paths = metadata.get("paths", {})
    online = metadata.get("online_sources", [])
    workflow = metadata.get("workflow") or {}
    audits = (workflow.get("source_audits") or {}).get("items") or {}
    result = []
    for definition in DEFINITIONS:
        item = asdict(definition)
        item["source_mode"] = definition.source_type
        item["label"] = definition.name
        path = paths.get(definition.path_key or definition.id)
        item.update({
            "path": path, "location": path, "configured": bool(path),
            "required": definition.required_stage == "P1",
            "health": "not_checked", "coverage": "not_checked",
            "source_metadata": {},
            "source_type_options": ["real", "synthetic", "manual"],
        })
        audit = audits.get(definition.id) or (audits.get("basemap") if definition.id == "airspace" else {}) or {}
        item["source_audit"] = audit
        item["trust"] = {
            "configured": "passed" if item["configured"] else "missing",
            "identity": "passed" if audit.get("source_id") else "not_checked",
            "schema": "passed" if audit.get("schema") else "not_checked",
            "crs": (
                "passed" if audit.get("confirmed_crs") else
                "declared_only" if audit.get("declared_crs") else "pending_confirmation"
            ),
            "geometry": (audit.get("geometry_health") or {}).get("status", "not_checked"),
            "version": "verified" if audit.get("sha256") else audit.get("status", "not_checked"),
            "overall": audit.get("status", "not_checked"),
            "reasons": list(audit.get("reasons") or []),
        }
        if definition.id in ("online_map", "geocoder"):
            item["configured"] = bool(online)
            item["location"] = f"QGIS 项目内 {len(online)} 个 XYZ 图层" if online else None
        elif definition.id == "airspace":
            item["configured"] = bool(metadata.get("layers"))
            item["location"] = "包含在 QGIS 项目中" if item["configured"] else None
            item["path"] = paths.get("basemap")
        elif definition.id in ("population", "terrain", "terrain_dtm"):
            raster = metadata.get(definition.id) or {}
            profile = raster.get("source_profile") or (workflow.get("data_source_profiles") or {}).get(definition.id) or {}
            item["source_profile"] = profile
            item["source_metadata"] = profile.get("provenance") or {}
            for key in ("version", "quantity", "unit", "resolution", "crs", "verification"):
                item[key] = profile.get(key)
        elif definition.id in ("buildings", "building_grid"):
            vector = (metadata.get("vector_sources") or {}).get(definition.id) or {}
            item["configured"] = bool(path)
            item["source_metadata"] = vector
            item["item_count"] = vector.get("feature_count", 0)
            item["health"] = vector.get("status", "not_checked")
        elif definition.id in ("aircraft", "devices", "existing_cns", "candidate_sites", "reference_landing_sites", "reference_routes", "equipment_reference_catalog", "towers"):
            state_key = {"aircraft": "aircraft_profiles", "devices": "device_catalog", "existing_cns": "existing_cns_facilities", "candidate_sites": "candidate_sites", "reference_landing_sites": "reference_landing_sites", "reference_routes": "reference_routes", "equipment_reference_catalog": "equipment_reference_catalog", "towers": "towers"}[definition.id]
            collection = workflow.get(state_key) or (metadata.get("device_library", {}) if definition.id == "devices" else {})
            item["configured"] = bool(path or collection.get("items"))
            source = collection.get("source")
            item["location"] = path or (source.get("path") if isinstance(source, dict) else source)
            item["source_metadata"] = collection.get("metadata") or {}
            item["item_count"] = int(collection.get("count", len(collection.get("items", []))))
            item["collection_status"] = collection.get("status", "not_calculated")
            item["point_count"] = int(collection.get("point_count", len(collection.get("points", []))))
            item["warnings"] = list(collection.get("warnings") or [])
            item["source_type"] = item["source_metadata"].get("source_type", item["source_type"])
            item["source_mode"] = item["source_metadata"].get("source_mode", item["source_type"])
        item["trust"]["configured"] = "passed" if item["configured"] else "missing"
        result.append(item)
    return result
