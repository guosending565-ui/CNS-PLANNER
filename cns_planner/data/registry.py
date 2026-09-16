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
    SourceDefinition("buildings", "建筑轮廓与高度", "风险环境", "vector", "GPKG / SHP / GeoJSON", "障碍和建筑暴露", path_key="buildings"),
    SourceDefinition("property_exposure", "财产暴露", "风险环境", "raster_or_vector", "Raster / Vector", "财产风险接口", path_key="property_exposure"),
    SourceDefinition("obstacles", "铁塔与高塔", "设施", "vector_or_table", "CSV / GPKG / SHP", "避障与共址候选", path_key="obstacles"),
    SourceDefinition("infrastructure", "关键基础设施", "设施", "vector", "GPKG / SHP / GeoJSON", "基础设施暴露", path_key="infrastructure"),
    SourceDefinition("towers", "通信铁塔", "设施", "vector_or_table", "CSV / GPKG / SHP", "CNS共址候选", path_key="towers"),
    SourceDefinition("traffic", "无人机运行交通", "运行", "simulation_or_trajectory", "JSON / CSV / GeoJSON", "交通暴露", path_key="traffic", source_type="synthetic"),
    SourceDefinition("vertiports", "起降设施", "设施", "vector_or_table", "CSV / GPKG / GeoJSON", "航路节点", path_key="vertiports"),
    SourceDefinition("reference_landing_sites", "参考起降点", "参考数据", "reference_collection", "XLSX / CSV", "人工选择后才能加入项目节点", path_key="reference_landing_sites", source_type="real"),
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
        if definition.id in ("online_map", "geocoder"):
            item["configured"] = bool(online)
            item["location"] = f"QGIS 项目内 {len(online)} 个 XYZ 图层" if online else None
        elif definition.id == "airspace":
            item["configured"] = bool(metadata.get("layers"))
            item["location"] = "包含在 QGIS 项目中" if item["configured"] else None
        elif definition.id in ("population", "terrain"):
            raster = metadata.get(definition.id) or {}
            profile = raster.get("source_profile") or (workflow.get("data_source_profiles") or {}).get(definition.id) or {}
            item["source_profile"] = profile
            item["source_metadata"] = profile.get("provenance") or {}
            for key in ("version", "quantity", "unit", "resolution", "crs", "verification"):
                item[key] = profile.get(key)
        elif definition.id in ("aircraft", "devices", "existing_cns", "candidate_sites", "reference_landing_sites", "equipment_reference_catalog"):
            state_key = {"aircraft": "aircraft_profiles", "devices": "device_catalog", "existing_cns": "existing_cns_facilities", "candidate_sites": "candidate_sites", "reference_landing_sites": "reference_landing_sites", "equipment_reference_catalog": "equipment_reference_catalog"}[definition.id]
            collection = workflow.get(state_key) or (metadata.get("device_library", {}) if definition.id == "devices" else {})
            item["configured"] = bool(collection.get("items"))
            source = collection.get("source")
            item["location"] = path or (source.get("path") if isinstance(source, dict) else source)
            item["source_metadata"] = collection.get("metadata") or {}
            item["item_count"] = int(collection.get("count", len(collection.get("items", []))))
            item["source_type"] = item["source_metadata"].get("source_type", item["source_type"])
            item["source_mode"] = item["source_metadata"].get("source_mode", item["source_type"])
        result.append(item)
    return result
