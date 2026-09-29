"""从项目已有数据派生正式 ``cartographic_land``（制图专用陆地面，只读源数据）。

为什么要派生而不是直接用现有矢量
================================
现有**矢量**陆地/岸线数据只有省界、国界、Natural Earth 1:10m —— 它们在嵊泗列岛是残缺的
（枸杞、嵊山落在几何之外 26.6 km / 32.2 km），不能作为舟山群岛专题图的陆地底图。

因此本工具从项目**已有**的两份真实数据派生制图陆地面（不联网、不虚构要素）：

1. **DEM / DTM**（source role ``terrain_dtm``，回退 ``terrain``）：高程 > 阈值的像元
   视为陆地，闭运算填补单像元空洞；
2. **建筑足迹**（source role ``buildings``，可用 ``--no-buildings`` 关闭）：栅格化 +
   膨胀 + 闭运算，作为**陆地独立旁证**补进掩膜 —— 建筑只能建在陆地上，因此这一步
   修正了 DEM 在低洼地、港区、填海区（实测某陆岛交通码头 DEM 全为 0 m）的"假海面"。

输出：GeoJSON（EPSG:4326）+ 派生元数据 JSON，并立即用同一批站址/建筑/探针做校验。

语义边界（写入每一份 FigureSpec）
================================
``cartographic_land`` **只**用于地图表达，**不**参与 CNS 规划、风险计算或
surface classification；canonical ``land_mask`` 的业务语义不受本工具影响。

用法（不内置任何本机绝对路径）：
    python tools/derive_cartographic_land.py \\
        --project-root . --sources projects/map_sources.json \\
        --out-geojson projects/derived/cartographic_land/cartographic_land_v1.geojson \\
        --out-metadata projects/derived/cartographic_land/cartographic_land_v1.json

源路径按 source role 从项目数据源配置读取（``projects/map_sources.json``，回退
``projects/data_sources.json``）；命令行 ``--dem`` / ``--crosscheck-dem`` /
``--buildings`` 可显式覆盖。需要 GDAL / numpy / scipy / shapely。
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

#: 源数据角色的搜索顺序（第一个"配置存在且文件存在"的角色生效）。
DEM_ROLES = ("terrain_dtm", "terrain")
CROSSCHECK_DEM_ROLES = ("terrain",)
BUILDING_ROLES = ("buildings",)

#: 默认数据源配置文件（相对 ``--project-root``），按顺序合并。
DEFAULT_SOURCE_FILES = ("projects/map_sources.json", "projects/data_sources.json")
#: 默认项目状态文件（站址落陆校验；缺失时跳过站址校验并如实说明）。
DEFAULT_STATE_FILE = "projects/current_project.json"
DEFAULT_OUTPUT_DIRECTORY = "projects/derived/cartographic_land"
#: **运行时真正消费的文件名**。默认输出与它必须一致（code review 修复项）：
#: ``cns_planner/gis/map_data.py::DEFAULT_PATHS["cartographic_land"]`` 指向的正是
#: 这个 GPKG，所以"跑一次 derive"就等于"装好了运行时要读的数据源"，不需要人工改名。
DEFAULT_OUTPUT_BASENAME = "cartographic_land_v1"
DEFAULT_OUTPUT_EXTENSION = ".gpkg"
#: 允许的输出矢量格式（GeoJSON 保留给调试与人工检查）。
SUPPORTED_OUTPUT_EXTENSIONS = (".gpkg", ".geojson")
DEFAULT_BBOX = (121.40, 29.45, 123.00, 31.20)

#: 派生参数默认值（全部写进元数据，可被命令行覆盖）。
DEFAULT_THRESHOLD_M = 0.0
DEFAULT_CLOSING_ITERATIONS = 1
DEFAULT_BUILDING_DILATION_PX = 3
DEFAULT_MIN_PIXELS = 2
DEFAULT_MAX_PIXELS = 12_000_000
#: 形态学结构元（scipy.ndimage 的 structure）：3×3 全连接。
MORPHOLOGY_STRUCTURE = "3x3_full"
#: gdal.Polygonize 的连通性：4 = 4-连通（上下左右）。
POLYGONIZE_CONNECTIVITY = 4
#: 孔洞（内环）策略：保持栅格连通性原样，不做填洞。
HOLE_POLICY = "preserve_raster_holes_no_fill"
OUTPUT_CRS = "EPSG:4326"

PROBES = {
    "舟山本岛(定海)": (122.1065, 30.0160),
    "普陀沈家门": (122.3010, 29.9450),
    "桃花岛": (122.3000, 29.8280),
    "岱山高亭": (122.2050, 30.2500),
    "嵊泗泗礁": (122.4500, 30.7250),
    "嵊山": (122.8200, 30.7200),
    "朱家尖": (122.3900, 29.9200),
    "六横岛": (122.1300, 29.7200),
    "金塘岛": (121.8500, 30.0500),
    "海面对照(嵊泗以东)": (122.9000, 30.6600),
    "海面对照(东海)": (122.9000, 30.1000),
}

SUSPECT_TOWERS = [
    ("330903500010001628", "普陀虾峙东白莲罐区", 122.187705, 29.822895),
    ("330903500010001595", "普陀西轩岛", 122.298800, 29.895670),
    ("330921908000000377", "岱山新城陆岛交通码头", 122.225296, 30.243900),
]


def resolve_source_paths(project_root, source_files):
    """按 source role 读项目数据源配置 → ``(配置字典, 实际读取的文件列表)``。"""

    root = Path(project_root).resolve()
    merged, used = {}, []
    for relative in source_files:
        candidate = (root / relative).resolve()
        if not candidate.is_file():
            continue
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"  [警告] 数据源配置无法解析：{candidate}（{exc}）", flush=True)
            continue
        if isinstance(payload, dict):
            merged.update(payload.get("paths") or payload)
            used.append(str(candidate))
    return merged, used


def pick_source(config, roles, *, project_root, explicit=None):
    """在角色列表里取第一个"配置存在且文件存在"的路径；``explicit`` 优先。"""

    root = Path(project_root).resolve()
    if explicit:
        candidate = Path(explicit)
        if not candidate.is_absolute():
            candidate = root / candidate
        return candidate, "command_line"
    for role in roles:
        value = config.get(role)
        if not value:
            continue
        candidate = Path(str(value))
        if not candidate.is_absolute():
            candidate = root / candidate
        if candidate.is_file():
            return candidate, f"source_role:{role}"
        print(f"  [警告] 角色 {role} 配置的路径不存在：{candidate}", flush=True)
    return None, None


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="从项目已配置的 DEM 与建筑足迹派生 cartographic_land（制图陆地面）",
    )
    parser.add_argument("--project-root", default=".",
                        help="项目根目录（默认当前目录；用于解析相对路径）")
    parser.add_argument("--sources", action="append", default=None,
                        help="数据源配置文件（可重复；相对 --project-root）")
    parser.add_argument("--state", default=DEFAULT_STATE_FILE,
                        help="项目状态文件（用于站址落陆校验；相对 --project-root）")
    parser.add_argument("--dem", default=None, help="显式指定 DEM（覆盖角色解析）")
    parser.add_argument("--crosscheck-dem", default=None, help="显式指定交叉校验 DEM")
    parser.add_argument("--buildings", default=None, help="显式指定建筑足迹数据集")
    # 默认输出与**运行时消费路径**一致：同一目录、同一基名、GPKG 格式。
    parser.add_argument("--out-geojson",
                        default=(f"{DEFAULT_OUTPUT_DIRECTORY}/"
                                 f"{DEFAULT_OUTPUT_BASENAME}{DEFAULT_OUTPUT_EXTENSION}"),
                        help="输出矢量陆地面（默认 GPKG，即运行时消费的文件；"
                             "可选 .geojson 供人工检查）")
    parser.add_argument("--out-metadata",
                        default=f"{DEFAULT_OUTPUT_DIRECTORY}/{DEFAULT_OUTPUT_BASENAME}.json")
    parser.add_argument("--bbox", default=",".join(str(value) for value in DEFAULT_BBOX),
                        help="派生窗口 west,south,east,north（EPSG:4326）")
    parser.add_argument("--max-pixels", type=int, default=DEFAULT_MAX_PIXELS)
    parser.add_argument("--threshold-m", type=float, default=DEFAULT_THRESHOLD_M)
    parser.add_argument("--closing", type=int, default=DEFAULT_CLOSING_ITERATIONS)
    parser.add_argument("--building-dilation-px", type=int,
                        default=DEFAULT_BUILDING_DILATION_PX)
    parser.add_argument("--min-pixels", type=int, default=DEFAULT_MIN_PIXELS)
    parser.add_argument("--no-buildings", action="store_true")
    parser.add_argument("--skip-validation", action="store_true")
    return parser.parse_args()


def grid_for(path, bbox, max_pixels):
    """返回 (width, height, actual_bbox, decimation) —— 与 gdal.Translate 的取整方式一致。"""

    from osgeo import gdal

    dataset = gdal.Open(path)
    transform = dataset.GetGeoTransform()
    lon_min, lat_max = transform[0], transform[3]
    lon_max = lon_min + transform[1] * dataset.RasterXSize
    lat_min = lat_max + transform[5] * dataset.RasterYSize
    dataset = None
    west, south = max(bbox[0], lon_min), max(bbox[1], lat_min)
    east, north = min(bbox[2], lon_max), min(bbox[3], lat_max)
    width = int(round((east - west) / transform[1]))
    height = int(round((north - south) / abs(transform[5])))
    scale = max(1, int(math.ceil(math.sqrt(width * height / max_pixels))))
    return max(1, width // scale), max(1, height // scale), (west, south, east, north), scale


def read_dem_grid(path, bbox, width, height):
    import numpy as np
    from osgeo import gdal

    gdal.UseExceptions()
    dataset = gdal.Open(path)
    array = gdal.Translate(
        "", dataset, format="MEM", projWin=[bbox[0], bbox[3], bbox[2], bbox[1]],
        width=width, height=height,
    ).ReadAsArray()
    array = np.asarray(array, dtype="float64")
    nodata = dataset.GetRasterBand(1).GetNoDataValue()
    if nodata is not None:
        array = np.where(array == nodata, math.nan, array)
    dataset = None
    return array


def building_grid(buildings_path, bbox, width, height):
    """建筑足迹 → 布尔栅格（与 DEM 同一网格）。"""

    import numpy as np
    from osgeo import gdal, ogr, osr

    gdal.UseExceptions()
    memory = gdal.GetDriverByName("MEM").Create("", width, height, 1, gdal.GDT_Byte)
    band = memory.GetRasterBand(1)
    band.Fill(0)
    spatial = osr.SpatialReference()
    spatial.ImportFromEPSG(4326)
    memory.SetProjection(spatial.ExportToWkt())
    west, south, east, north = bbox
    memory.SetGeoTransform(
        (west, (east - west) / width, 0.0, north, 0.0, -(north - south) / height)
    )
    dataset = ogr.Open(buildings_path)
    layer = dataset.GetLayerByIndex(0)
    layer.SetSpatialFilterRect(west, south, east, north)
    gdal.RasterizeLayer(memory, [1], layer, burn_values=[1])
    array = band.ReadAsArray()
    dataset = None
    memory = None
    return np.asarray(array) > 0


def build_mask(dem_array, buildings, *, threshold_m, closing, building_radius_px, use_buildings):
    import numpy as np
    from scipy import ndimage

    structure = np.ones((3, 3))
    dem_land = np.nan_to_num(dem_array, nan=-9999.0) > threshold_m
    if closing > 0:
        dem_land = ndimage.binary_closing(dem_land, structure=structure, iterations=closing)
    mask = dem_land
    if use_buildings and buildings is not None and buildings.any():
        grown = buildings
        if building_radius_px > 0:
            grown = ndimage.binary_dilation(grown, structure=structure,
                                            iterations=building_radius_px)
        grown = ndimage.binary_closing(grown, structure=structure, iterations=closing or 1)
        mask = np.logical_or(mask, grown)
    return mask, dem_land


def polygonize(mask, bbox, *, min_pixels):
    from osgeo import gdal, ogr, osr
    from shapely.geometry import shape
    from shapely.ops import unary_union

    height, width = mask.shape
    raster = gdal.GetDriverByName("MEM").Create("", width, height, 1, gdal.GDT_Byte)
    band = raster.GetRasterBand(1)
    band.WriteArray(mask.astype("uint8"))
    band.SetNoDataValue(0)
    spatial = osr.SpatialReference()
    spatial.ImportFromEPSG(4326)
    raster.SetProjection(spatial.ExportToWkt())
    west, south, east, north = bbox
    raster.SetGeoTransform(
        (west, (east - west) / width, 0.0, north, 0.0, -(north - south) / height)
    )
    dataset = ogr.GetDriverByName("MEM").CreateDataSource("mem")
    layer = dataset.CreateLayer("land", spatial, ogr.wkbPolygon)
    layer.CreateField(ogr.FieldDefn("value", ogr.OFTInteger))
    # 连通性：GDAL 的 Polygonize 在未传 options 时使用 4-连通（上下左右），
    # 与元数据里的 POLYGONIZE_CONNECTIVITY 一致；这里不额外传参以避免
    # 不同 GDAL 小版本对 options 关键字的兼容差异。
    gdal.Polygonize(band, band, layer, 0, [], callback=None)
    pixel_area = ((east - west) / width) * ((north - south) / height)
    keep = []
    for feature in layer:
        if int(feature.GetField("value")) != 1:
            continue
        geometry = feature.GetGeometryRef()
        if geometry is None or geometry.GetGeometryName().upper() != "POLYGON":
            continue
        if geometry.GetArea() / pixel_area < min_pixels:
            continue
        keep.append(shape(json.loads(geometry.ExportToJson())))
    dataset = None
    if not keep:
        return []
    merged = unary_union(keep)
    if merged.is_empty:
        return []
    parts = list(merged.geoms) if merged.geom_type == "MultiPolygon" else [merged]
    return [poly for poly in parts if poly.geom_type == "Polygon" and not poly.is_empty]


def tower_points(state_path):
    from cns_planner.application.map_figure_service import _tower_coordinate

    state = json.loads(Path(state_path).read_text(encoding="utf-8"))
    collection = state.get("towers") or {}
    points = []
    for tower in collection.get("items") or []:
        if not isinstance(tower, dict):
            continue
        longitude, latitude = _tower_coordinate(tower)
        if longitude is not None:
            points.append((longitude, latitude))
    return points


def building_centroids(path):
    """建筑面质心坐标 ``(lon, lat)`` 列表（不构造 shapely 对象：53 万点下省一个数量级）。"""

    from osgeo import ogr

    dataset = ogr.Open(path)
    layer = dataset.GetLayerByIndex(0)
    points = []
    for feature in layer:
        geometry = feature.GetGeometryRef()
        if geometry is None:
            continue
        centroid = geometry.Centroid()
        points.append((centroid.GetX(), centroid.GetY()))
    dataset = None
    return points


def validate(lands, *, buildings_path, state_path):
    """校验（向量化：并集几何 prepared + 批量 intersects_xy）。"""

    import numpy as np
    from shapely import STRtree, intersects_xy
    from shapely.geometry import Point
    from shapely.ops import unary_union

    prepared = unary_union(lands)
    tree = STRtree(lands)

    def inside_many(points):
        if not len(points):
            return np.zeros(0, dtype=bool)
        lon = np.array([item[0] for item in points], dtype="float64")
        lat = np.array([item[1] for item in points], dtype="float64")
        return np.asarray(intersects_xy(prepared, lon, lat))

    def inside(point):
        return bool(intersects_xy(prepared, [point[0]], [point[1]])[0])

    def coast_distance_m(point):
        target = Point(point[0], point[1])
        return round(lands[int(tree.nearest(target))].distance(target) * 111320.0, 1)

    result = {
        "probes": {label: bool(inside((lon, lat)))
                   for label, (lon, lat) in PROBES.items()},
    }
    buildings = building_centroids(buildings_path)
    hits = inside_many(buildings)
    result["buildings_total"] = len(buildings)
    result["buildings_inside"] = int(hits.sum())
    result["buildings_inside_ratio"] = round(float(hits.mean()) if len(hits) else 0.0, 6)

    towers = tower_points(state_path)
    tower_hits = inside_many(towers)
    outside = []
    within_30m = 0
    for (longitude, latitude), hit in zip(towers, tower_hits):
        if hit:
            continue
        distance = coast_distance_m((longitude, latitude))
        if distance <= 30.0:
            within_30m += 1
        outside.append({"lon": round(longitude, 6), "lat": round(latitude, 6),
                        "coast_distance_m": distance})
    result["towers_total"] = len(towers)
    result["towers_inside"] = int(tower_hits.sum())
    result["towers_within_30m_coast"] = within_30m
    result["towers_outside"] = outside
    result["suspect_towers"] = []
    for tower_id, name, lon, lat in SUSPECT_TOWERS:
        hit = bool(inside((lon, lat)))
        result["suspect_towers"].append({
            "tower_id": tower_id, "name": name, "inside": hit,
            "coast_distance_m": 0.0 if hit else coast_distance_m((lon, lat)),
        })
    return result


def write_land_dataset(path, lands):
    """把陆地面写成矢量数据集：``.geojson`` 直接写，``.gpkg`` 经 OGR 写（带空间索引）。

    运行时消费的是 GeoPackage（``map_data.DEFAULT_PATHS["cartographic_land"]``），
    因此默认输出 GPKG；GeoJSON 仍可用于人工检查与调试。
    """

    payload = json.dumps({
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {"fid": index, "area_deg2": polygon.area},
             "geometry": polygon.__geo_interface__}
            for index, polygon in enumerate(lands)
        ],
    }, ensure_ascii=False)
    suffix = Path(path).suffix.lower()
    if suffix == ".geojson":
        Path(path).write_text(payload, encoding="utf-8")
        return
    from osgeo import gdal, ogr, osr

    gdal.UseExceptions()
    spatial = osr.SpatialReference()
    spatial.ImportFromEPSG(4326)
    driver = ogr.GetDriverByName("GPKG")
    if Path(path).exists():
        driver.DeleteDataSource(str(path))
    dataset = driver.CreateDataSource(str(path))
    if dataset is None:
        raise ValueError(f"无法创建 GeoPackage：{path}")
    try:
        # 几何类型用 wkbUnknown：本数据集同时含 Polygon 与 MultiPolygon，
        # 声明成 MultiPolygon 会让驱动对 Polygon 要素报"不合规"警告。
        layer = dataset.CreateLayer("cartographic_land", spatial, ogr.wkbUnknown)
        layer.CreateField(ogr.FieldDefn("fid", ogr.OFTInteger))
        layer.CreateField(ogr.FieldDefn("area_deg2", ogr.OFTReal))
        for index, polygon in enumerate(lands):
            geometry = ogr.CreateGeometryFromJson(json.dumps(polygon.__geo_interface__))
            if geometry is None:
                continue
            feature = ogr.Feature(layer.GetLayerDefn())
            feature.SetField("fid", index)
            feature.SetField("area_deg2", float(polygon.area))
            feature.SetGeometry(geometry)
            layer.CreateFeature(feature)
            feature = None
        # 空间索引让运行时的逐要素 bbox 过滤走 rtree（637 个多边形也要保证出图速度）。
        layer = None
    finally:
        dataset = None
    if not Path(path).is_file():
        raise ValueError(f"GeoPackage 写入失败：{path}")


def derive_coverage_reason(validation):
    """由**校验结论**生成覆盖说明（供制图侧直接引用，不硬编码任何结论）。"""

    validation = validation if isinstance(validation, dict) else {}
    probes = validation.get("probes") if isinstance(validation.get("probes"), dict) else {}
    passed = sum(1 for value in probes.values() if value is True)
    parts = []
    if probes:
        parts.append(f"陆地覆盖探针 {passed}/{len(probes)} 命中")
    ratio = validation.get("buildings_inside_ratio")
    if isinstance(ratio, (int, float)):
        parts.append(f"建筑落陆率 {float(ratio) * 100:.2f}%")
    inside, total = validation.get("towers_inside"), validation.get("towers_total")
    if isinstance(inside, int) and isinstance(total, int):
        parts.append(f"通信站址落陆 {inside}/{total}")
    if not parts:
        return "派生时未记录覆盖校验结论"
    return "；".join(parts) + "（派生时记录，仅供参考）"


def main():
    arguments = parse_arguments()
    bbox = tuple(float(value) for value in arguments.bbox.split(","))
    project_root = Path(arguments.project_root).resolve()
    import numpy as np

    source_files = arguments.sources or list(DEFAULT_SOURCE_FILES)
    config, used_configs = resolve_source_paths(project_root, source_files)
    print(f"项目根目录：{project_root}", flush=True)
    print(f"读取的数据源配置：{used_configs or '（未找到，全部路径需由命令行显式给出）'}",
          flush=True)

    dem_path, dem_origin = pick_source(config, DEM_ROLES, project_root=project_root,
                                       explicit=arguments.dem)
    if dem_path is None or not Path(dem_path).is_file():
        print("**找不到可用的 DEM**：请在数据源配置里设置 terrain_dtm / terrain，"
              "或用 --dem 显式指定。", flush=True)
        return 3
    crosscheck_path, crosscheck_origin = pick_source(
        config, CROSSCHECK_DEM_ROLES, project_root=project_root,
        explicit=arguments.crosscheck_dem,
    )
    buildings_path, buildings_origin = pick_source(
        config, BUILDING_ROLES, project_root=project_root, explicit=arguments.buildings,
    )
    if not arguments.no_buildings and (buildings_path is None
                                       or not Path(buildings_path).is_file()):
        print("**找不到建筑足迹数据集**：请在数据源配置里设置 buildings，"
              "或用 --buildings 显式指定（也可用 --no-buildings 关闭该旁证）。", flush=True)
        return 3

    state_path = Path(arguments.state)
    if not state_path.is_absolute():
        state_path = project_root / state_path

    print(f"DEM      = {dem_path}（来源：{dem_origin}）", flush=True)
    print(f"交叉校验 = {crosscheck_path or '（未配置）'}（来源：{crosscheck_origin or '-'}）",
          flush=True)
    print(f"建筑足迹 = {buildings_path if not arguments.no_buildings else '（已关闭）'}"
          f"（来源：{buildings_origin or '-'}）", flush=True)

    width, height, actual_bbox, scale = grid_for(str(dem_path), bbox, arguments.max_pixels)
    dem_array = read_dem_grid(str(dem_path), actual_bbox, width, height)
    print(f"网格：{width}x{height} 降采样={scale} 像元≈"
          f"{(actual_bbox[2] - actual_bbox[0]) / width * 111320 * 0.866:.0f}m x "
          f"{(actual_bbox[3] - actual_bbox[1]) / height * 111320:.0f}m", flush=True)

    buildings = None
    if not arguments.no_buildings:
        buildings = building_grid(str(buildings_path), actual_bbox, width, height)
        print(f"建筑栅格命中像元={int(buildings.sum())}", flush=True)

    mask, dem_land = build_mask(
        dem_array, buildings, threshold_m=arguments.threshold_m, closing=arguments.closing,
        building_radius_px=arguments.building_dilation_px,
        use_buildings=not arguments.no_buildings,
    )
    print(f"DEM 陆地像元比例={float(dem_land.mean()) * 100:.2f}%  "
          f"合成后={float(mask.mean()) * 100:.2f}%", flush=True)

    lands = polygonize(mask, actual_bbox, min_pixels=arguments.min_pixels)
    print(f"多边形数={len(lands)}", flush=True)
    if not lands:
        print("**没有生成任何陆地面**", flush=True)
        return 2

    output = Path(arguments.out_geojson)
    suffix = output.suffix.lower()
    if suffix not in SUPPORTED_OUTPUT_EXTENSIONS:
        print(f"**不支持的输出格式 {suffix}**：支持 "
              f"{'、'.join(SUPPORTED_OUTPUT_EXTENSIONS)}", flush=True)
        return 3
    output.parent.mkdir(parents=True, exist_ok=True)
    write_land_dataset(output, lands)
    print(f"矢量陆地面={output.resolve()} ({output.stat().st_size / 1024:.0f} KB)"
          f"  格式={suffix.lstrip('.')}", flush=True)
    if suffix != DEFAULT_OUTPUT_EXTENSION:
        print(f"  [提示] 运行时默认消费 {DEFAULT_OUTPUT_EXTENSION}；"
              f"调试用的 {suffix} 不会被自动读取。", flush=True)

    metadata = {
        "role": "cartographic_land",
        "semantics": "cartographic_presentation_only_not_surface_classification",
        "not_replacing": "canonical land_mask (business surface classification unchanged)",
        "derivation": (
            "dem_threshold_gt_zero_closing "
            + ("" if arguments.no_buildings else "plus_building_footprint_dilation ")
            + "then_polygonize"
        ),
        "project_root": str(project_root),
        "source_config_files": used_configs,
        "source_dem": str(Path(dem_path).resolve()),
        "source_dem_origin": dem_origin,
        "crosscheck_dem": None if crosscheck_path is None else str(Path(crosscheck_path).resolve()),
        "crosscheck_dem_origin": crosscheck_origin,
        "source_buildings": (
            None if arguments.no_buildings else str(Path(buildings_path).resolve())
        ),
        "source_buildings_origin": None if arguments.no_buildings else buildings_origin,
        "dem_threshold_m": arguments.threshold_m,
        "morphology_structure": MORPHOLOGY_STRUCTURE,
        "closing_iterations": arguments.closing,
        "building_dilation_px": arguments.building_dilation_px,
        "building_raster_resolution_deg": [
            (actual_bbox[2] - actual_bbox[0]) / width,
            (actual_bbox[3] - actual_bbox[1]) / height,
        ],
        "polygonize_connectivity": POLYGONIZE_CONNECTIVITY,
        "min_polygon_pixels": arguments.min_pixels,
        "hole_policy": HOLE_POLICY,
        "simplify_deg": 0.0,
        "decimation": scale,
        "grid": [width, height],
        "bbox": list(actual_bbox),
        "pixel_size_deg": [(actual_bbox[2] - actual_bbox[0]) / width,
                           (actual_bbox[3] - actual_bbox[1]) / height],
        "land_pixel_ratio": float(mask.mean()),
        "polygon_count": len(lands),
        # 输出路径：``output_gpkg`` 是运行时真正消费的文件（缺省就是它）；
        # ``output_geojson`` 保留字段名以兼容早期元数据，内容为实际写出的文件。
        "output_gpkg": str(output.resolve()) if suffix == ".gpkg" else None,
        "output_geojson": str(output.resolve()) if suffix == ".geojson" else None,
        "output_path": str(output.resolve()),
        "output_format": suffix.lstrip("."),
        "consumed_by": "cns_planner/gis/map_data.py::DEFAULT_PATHS['cartographic_land']",
        "crs": OUTPUT_CRS,
    }
    if not arguments.skip_validation:
        validation = validate(lands, buildings_path=str(buildings_path),
                              state_path=str(state_path))
        metadata["validation"] = validation
        # ``coverage_reason`` 必须由**数据源自己**的校验结论生成，供制图侧直接引用；
        # 制图代码不硬编码任何"覆盖全部岛群"之类的结论。
        metadata["coverage_reason"] = derive_coverage_reason(validation)
        print(f"建筑落陆={validation['buildings_inside']}/{validation['buildings_total']} "
              f"= {validation['buildings_inside_ratio'] * 100:.2f}%", flush=True)
        print(f"通信站址落陆={validation['towers_inside']}/{validation['towers_total']} "
              f"陆外={len(validation['towers_outside'])} "
              f"其中 30m 内={validation['towers_within_30m_coast']}", flush=True)
        for item in validation["suspect_towers"]:
            print(f"  {item['tower_id']} {item['name']} inside={item['inside']} "
                  f"离岸={item['coast_distance_m']}m", flush=True)
        misses = [label for label, hit in validation["probes"].items() if not hit]
        print(f"探针未命中：{misses or '无'}", flush=True)

    metadata_path = Path(arguments.out_metadata)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"元数据={metadata_path.resolve()}", flush=True)
    print("\nDERIVE_DONE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
