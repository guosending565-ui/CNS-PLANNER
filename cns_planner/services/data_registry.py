"""The single registry behind the data-source center."""
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class SourceDefinition:
    id: str
    category: str
    label: str
    formats: str
    purpose: str
    required_stage: str | None = None
    editable_now: bool = False


DEFINITIONS = (
    SourceDefinition("basemap", "基础地图", "QGIS 项目", "QGZ / QGS", "适飞空域及基础 GIS", "P1", True),
    SourceDefinition("online_map", "在线服务", "天地图底图与中文注记", "URL / API Key", "在线地图显示", None),
    SourceDefinition("geocoder", "在线服务", "天地图地名搜索", "API Key", "地名搜索定位", None),
    SourceDefinition("population", "风险环境", "人口密度", "GeoTIFF", "人员暴露与工程风险评分", "P1", True),
    SourceDefinition("terrain", "风险环境", "DEM / DSM", "GeoTIFF", "高程、障碍与剖面"),
    SourceDefinition("buildings", "风险环境", "建筑轮廓与高度", "GPKG / SHP / GeoJSON", "障碍和暴露"),
    SourceDefinition("airspace", "风险环境", "独立空域数据", "GPKG / QGIS", "硬约束与环境风险"),
    SourceDefinition("obstacles", "设施", "铁塔与高塔", "CSV / GPKG / SHP", "避障与共址候选"),
    SourceDefinition("property", "风险环境", "财产暴露", "Raster / Vector", "财产风险接口"),
    SourceDefinition("vertiports", "设施", "起降设施", "CSV / GPKG / GeoJSON", "航路节点"),
    SourceDefinition("aircraft", "业务库", "飞行器库", "JSON / CSV / Excel", "运行规则与可靠性"),
    SourceDefinition("devices", "业务库", "CNS 设备库", "JSON / CSV / Excel", "布站与覆盖"),
    SourceDefinition("existing_cns", "设施", "既有 CNS 设施", "CSV / GPKG", "改造、共址和已有覆盖"),
)


def build_registry(metadata: dict) -> list[dict]:
    paths = metadata.get("paths", {})
    online = metadata.get("online_sources", [])
    result = []
    for definition in DEFINITIONS:
        item = asdict(definition)
        item["configured"] = False
        item["location"] = None
        if definition.id in ("basemap", "population"):
            item["location"] = paths.get(definition.id)
            item["configured"] = bool(item["location"])
        elif definition.id in ("online_map", "geocoder"):
            item["configured"] = bool(online)
            item["location"] = f"QGIS 项目内 {len(online)} 个 XYZ 图层" if online else None
        elif definition.id == "airspace":
            item["configured"] = bool(metadata.get("layers"))
            item["location"] = "包含在 QGIS 项目中" if item["configured"] else None
        elif definition.id == "devices":
            library = metadata.get("device_library", {})
            item["configured"] = bool(library.get("items"))
            item["location"] = library.get("source") if item["configured"] else None
        result.append(item)
    return result
