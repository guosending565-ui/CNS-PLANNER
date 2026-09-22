"""Layer-independent building facts by **exact footprint aggregation**.

背景（真实缺陷）：``zhoushan_building_grid_L8.gpkg`` 是预先按 L8 聚合好的事实表，系统禁止跨
层级平均/插值，因此工作区网格一旦被 ``WorkspaceGridService`` coarsen 到 L7/L6，建筑事实就整表
``unsupported``（前端显示 "建筑环境映射 unsupported 0/3528"），建筑约束实际不参与 Layered
Risk-Aware Theta* V2 的 coarse 战略垂向包线。用户明确要求：**不论工作区层级，需要建筑事实时
都必须能直接取得**。

本模块实现文档 ``docs/10-Phase3已知限制与待办.md`` §1 记录的方向 1：
**按当前层级对原始 footprint 做精确几何聚合**（不是跨层级平均、不是插值）。

聚合语义与既有的 L8 事实表生成脚本 ``scripts/build_mht_building_grid.py`` **逐条对齐**，
不引入第二套建筑数学：

===============================  ==========================================================
既有 L8 事实表                    本模块（任意层级）
===============================  ==========================================================
计数 / 高度统计按**建筑质心**分配   同样按质心分配（一个建筑只计数一次）
``height_valid = height_m notna``  同样 ``height_m is not None and height_m > 0``
米制面积用 ``EPSG:32651``          按工作区中心自动选 UTM 带（舟山 ⇒ EPSG:32651）
覆盖度 = footprint ∩ cell 精确面积  同样精确相交面积，``ratio`` 裁剪到 ``[0, 1]``
只生成被建筑触及的 cell            所有工作区 cell 都给出明确结果（命中 / 已知 0 / outside）
===============================  ==========================================================

边界与不变量：

* **绝不改写源数据**：GeoPackage 全程 ``mode=ro``，Shapefile/GeoJSON 只读；
* **unknown ≠ 0**：源 extent 之外的 cell 保持 ``missing_data``，绝不当成"没有建筑"；
* **失败不伪造**：几何库缺失、源不可读、无空间索引、无效几何全部显式记录原因；
* **provenance 完整**：聚合方法、源 CRS、米制 CRS、质心分配规则、相交面积方法、跳过计数。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import sqlite3
import time

try:  # 几何/投影是**可选**依赖：缺失时明确降级，绝不伪造建筑事实。
    import numpy as _numpy
    import pyproj
    import shapely
    from shapely.geometry import shape as _shape
    _GEOMETRY_IMPORT_ERROR = None
except Exception as _exc:  # pragma: no cover - 依赖缺失时的显式降级分支
    _GEOMETRY_IMPORT_ERROR = f"{type(_exc).__name__}: {_exc}"

ALGORITHM_ID = "building-grid-footprint-aggregation"
ALGORITHM_VERSION = "1.0"

#: 面积/长度计算所用的米制 CRS 选取方式（与 L8 事实表脚本的 UTM 51N 一致）。
AREA_CRS_BASIS = "auto_utm_zone_from_workspace_centroid"
#: 计数与高度统计的分配规则。
COUNT_ALLOCATION = "building_centroid_in_cell"
#: 有效高度判定规则（与 L8 事实表脚本完全一致）。
HEIGHT_VALID_RULE = "height_m_is_not_null_and_positive"
#: 覆盖度算法。
COVERAGE_METHOD = "exact_polygon_cell_intersection_area"
#: 源 extent 之外的语义。
OUTSIDE_SEMANTICS = "missing_data"

DEFAULT_HEIGHT_FIELD = "height_m"
#: 高度字段候选（真实源字段不一致时按顺序探测，找不到就保持"高度未知"）。
HEIGHT_FIELD_CANDIDATES = ("height_m", "height", "Height", "HEIGHT", "h_m", "building_height_m")

GPKG_WKB_MAGIC = b"GP"


def geometry_available():
    """几何/投影依赖是否可用（不可用时返回原因，调用方必须显式降级）。"""

    if _GEOMETRY_IMPORT_ERROR is None:
        return True, None
    return False, f"geometry_libraries_unavailable:{_GEOMETRY_IMPORT_ERROR}"


def metric_crs_for_bbox(bbox):
    """按工作区中心经度/纬度选择 UTM 带（舟山 ⇒ ``EPSG:32651``，与既有脚本一致）。"""

    west, south, east, north = (float(value) for value in bbox)
    center_lon = (west + east) / 2.0
    center_lat = (south + north) / 2.0
    zone = int(math.floor((center_lon + 180.0) / 6.0)) + 1
    zone = max(1, min(60, zone))
    epsg = (32600 if center_lat >= 0 else 32700) + zone
    return f"EPSG:{epsg}"


# ============================================================================
# 矢量源发现与读取（只读）
# ============================================================================

def _quote(identifier):
    return '"' + str(identifier).replace('"', '""') + '"'


def _gpkg_wkb(blob):
    """剥掉 GeoPackage 二进制头，返回内嵌 WKB（与 ``source_inspection`` 同一规则）。"""

    value = bytes(blob)
    if value[:2] != GPKG_WKB_MAGIC:
        return value
    if len(value) < 8:
        raise ValueError("truncated GeoPackage geometry header")
    indicator = (value[3] >> 1) & 0b111
    envelope_bytes = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}.get(indicator)
    if envelope_bytes is None:
        raise ValueError("unsupported GeoPackage envelope indicator")
    offset = 8 + envelope_bytes
    if len(value) <= offset:
        raise ValueError("missing WKB payload")
    return value[offset:]


def discover_vector_layer(source_path, layer_name=None):
    """只读发现矢量图层的基本事实：格式、图层、几何列、CRS、空间索引、要素数。"""

    path = Path(str(source_path))
    if not path.is_file():
        raise ValueError("建筑足迹来源不存在")
    suffix = path.suffix.lower()
    if suffix == ".gpkg":
        return _discover_geopackage(path, layer_name)
    if suffix in (".geojson", ".json"):
        return _discover_geojson(path)
    if suffix == ".shp":
        return _discover_ogr(path, layer_name)
    raise ValueError(f"不支持的建筑足迹格式：{suffix or '无扩展名'}")


def _discover_geopackage(path, layer_name):
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT c.table_name, c.srs_id, c.min_x, c.min_y, c.max_x, c.max_y, "
            "g.column_name, g.geometry_type_name "
            "FROM gpkg_contents c JOIN gpkg_geometry_columns g ON g.table_name=c.table_name "
            "WHERE c.data_type='features'"
        ).fetchall()
        if not rows:
            raise ValueError("GeoPackage 中没有要素图层")
        if layer_name:
            match = next((row for row in rows if str(row[0]) == str(layer_name)), None)
            if match is None:
                raise ValueError(f"GeoPackage 中没有图层 {layer_name}")
        else:
            # 优先选择 Polygon 图层；建筑足迹与建筑环境网格都是 Polygon。
            match = next(
                (row for row in rows if "POLYGON" in str(row[7]).upper()), rows[0],
            )
        table, srs_id, min_x, min_y, max_x, max_y, geometry_column, geometry_type = match
        columns = [row[1] for row in connection.execute(f"PRAGMA table_info({_quote(table)})").fetchall()]
        rtree = f"rtree_{table}_{geometry_column}"
        has_rtree = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (rtree,),
        ).fetchone() is not None
        count = connection.execute(f"SELECT COUNT(*) FROM {_quote(table)}").fetchone()[0]
        return {
            "format": "GPKG", "path": str(path.resolve()), "layer": table,
            "geometry_column": geometry_column, "geometry_type": geometry_type,
            "crs": f"EPSG:{srs_id}" if srs_id else None, "srs_id": srs_id,
            "extent": [min_x, min_y, max_x, max_y], "feature_count": int(count),
            "fields": columns, "rtree_table": rtree if has_rtree else None,
            "spatial_index": has_rtree, "read_method": "sqlite_read_only_rtree_bbox_query",
        }
    finally:
        connection.close()


def _discover_geojson(path):
    try:
        with path.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, ValueError) as exc:
        raise ValueError(f"无法读取 GeoJSON：{exc}") from exc
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        raise ValueError("GeoJSON 必须是 FeatureCollection")
    features = payload.get("features") or []
    geometry_types = set()
    extent = None
    for feature in features:
        if not isinstance(feature, dict):
            continue
        geometry = feature.get("geometry") or {}
        geometry_types.add(str(geometry.get("type")))
        coordinates = geometry.get("coordinates")
        bounds = _coordinates_bounds(coordinates)
        if bounds is None:
            continue
        extent = bounds if extent is None else [
            min(extent[0], bounds[0]), min(extent[1], bounds[1]),
            max(extent[2], bounds[2]), max(extent[3], bounds[3]),
        ]
    return {
        "format": "GeoJSON", "path": str(path.resolve()), "layer": path.stem,
        "geometry_column": "geometry",
        "geometry_type": ",".join(sorted(geometry_types - {"None"})) or None,
        "crs": "OGC:CRS84", "srs_id": 4326, "extent": extent,
        "feature_count": len(features),
        "fields": sorted({
            key for feature in features[:200] if isinstance(feature, dict)
            for key in (feature.get("properties") or {})
        }),
        "rtree_table": None, "spatial_index": False,
        "read_method": "geojson_full_parse",
        "_features": features,
    }


def _coordinates_bounds(coordinates):
    """递归求 GeoJSON coordinates 的包围盒（None 表示没有可用坐标）。"""

    box = [math.inf, math.inf, -math.inf, -math.inf]
    stack = [coordinates]
    seen = False
    while stack:
        item = stack.pop()
        if not isinstance(item, (list, tuple)):
            continue
        if item and all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in item[:2]):
            if len(item) >= 2:
                box[0] = min(box[0], float(item[0]))
                box[1] = min(box[1], float(item[1]))
                box[2] = max(box[2], float(item[0]))
                box[3] = max(box[3], float(item[1]))
                seen = True
            continue
        stack.extend(item)
    return box if seen else None


def _discover_ogr(path, layer_name):
    """Shapefile（以及 GPKG/GeoJSON 的兜底路径）通过 OGR/pyogrio 只读发现。"""

    errors = []
    try:
        return _discover_pyogrio(path, layer_name)
    except Exception as exc:  # pragma: no cover - 依赖存在性取决于运行环境
        errors.append(f"pyogrio:{type(exc).__name__}: {exc}")
    try:
        return _discover_osgeo(path, layer_name)
    except Exception as exc:  # pragma: no cover
        errors.append(f"osgeo:{type(exc).__name__}: {exc}")
    raise ValueError("无法读取 Shapefile（pyogrio / GDAL OGR 均不可用）：" + "；".join(errors))


def _discover_pyogrio(path, layer_name):
    import pyogrio

    info = pyogrio.read_info(str(path), layer=layer_name)
    crs = info.get("crs")
    srs_id = None
    if crs is not None:
        try:
            srs_id = crs.to_epsg()
        except AttributeError:
            srs_id = None
    bounds = info.get("total_bounds")
    return {
        "format": "SHP" if path.suffix.lower() == ".shp" else path.suffix.upper().lstrip("."),
        "path": str(path.resolve()), "layer": layer_name or info.get("layer_name") or path.stem,
        "geometry_column": "geometry", "geometry_type": info.get("geometry_type"),
        "crs": f"EPSG:{srs_id}" if srs_id else None, "srs_id": srs_id,
        "extent": [float(value) for value in bounds] if bounds is not None else None,
        "feature_count": int(info.get("features") or 0),
        "fields": list(info.get("fields") or []), "rtree_table": None,
        "spatial_index": False, "read_method": "pyogrio_read_info",
        "_backend": "pyogrio",
    }


def _discover_osgeo(path, layer_name):  # pragma: no cover - 需要运行在 QGIS/GDAL 环境
    from osgeo import ogr

    dataset = ogr.Open(str(path), 0)
    if dataset is None:
        raise ValueError("OGR 无法打开数据集")
    try:
        layer = dataset.GetLayerByName(layer_name) if layer_name else dataset.GetLayer(0)
        if layer is None:
            raise ValueError("OGR 数据集中没有图层")
        extent = layer.GetExtent()
        return {
            "format": "SHP" if path.suffix.lower() == ".shp" else path.suffix.upper().lstrip("."),
            "path": str(path.resolve()), "layer": layer.GetName(), "geometry_column": "geometry",
            "geometry_type": ogr.GeometryTypeToName(layer.GetGeomType()),
            "crs": None, "srs_id": None, "extent": list(extent) if extent else None,
            "feature_count": int(layer.GetFeatureCount()), "fields": [], "rtree_table": None,
            "spatial_index": False, "read_method": "osgeo_ogr_read_only",
            "_backend": "osgeo",
        }
    finally:
        dataset = None


def _detect_height_field(info):
    fields = {str(name) for name in (info.get("fields") or [])}
    lowered = {name.lower(): name for name in fields}
    for candidate in HEIGHT_FIELD_CANDIDATES:
        if candidate in fields:
            return candidate
        if candidate.lower() in lowered:
            return lowered[candidate.lower()]
    return None


def read_footprints(source_path, bbox, *, layer_name=None, info=None, max_features=None):
    """读取与 ``bbox`` 相交的建筑足迹。

    返回 ``(footprints, info)``，其中 ``footprints`` 是 ``[(shapely_geometry_wgs84, height_or_None)]``。
    几何保持源坐标系下的原始几何（**不做任何修复或简化**），坐标变换由调用方完成。
    """

    available, reason = geometry_available()
    if not available:
        raise ValueError(reason)
    info = dict(info or discover_vector_layer(source_path, layer_name))
    west, south, east, north = (float(value) for value in bbox)
    height_field = _detect_height_field(info)
    if info["format"] == "GPKG":
        rows = _read_gpkg_footprints(info, (west, south, east, north), height_field, max_features)
    elif info["format"] == "GeoJSON":
        rows = _read_geojson_footprints(info, (west, south, east, north), height_field, max_features)
    else:
        rows = _read_ogr_footprints(info, (west, south, east, north), height_field, max_features)
    info = {**info, "height_field": height_field, "footprints_read": len(rows)}
    info.pop("_features", None)
    return rows, info


def _read_gpkg_footprints(info, bbox, height_field, max_features):
    west, south, east, north = bbox
    table = _quote(info["layer"])
    geometry_column = _quote(info["geometry_column"])
    connection = sqlite3.connect(f"file:{info['path']}?mode=ro", uri=True)
    try:
        fields = [row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()]
        id_field = next((name for name in ("id", "gba_key", "building_id", "fid") if name in fields), None)
        select_height = f",b.{_quote(height_field)}" if height_field else ""
        select_id = f",b.{_quote(id_field)}" if id_field else ""
        limit = f" LIMIT {int(max_features)}" if max_features else ""
        if not info.get("rtree_table"):
            raise ValueError("建筑足迹 GeoPackage 缺少空间索引，拒绝全表运行时扫描")
        rtree = _quote(info["rtree_table"])
        sql = (
            f"SELECT b.{geometry_column}{select_height}{select_id} FROM {table} b "
            f"JOIN {rtree} r ON r.id=b.fid "
            "WHERE r.maxx>? AND r.minx<? AND r.maxy>? AND r.miny<?"
            f"{limit}"
        )
        height_index = 1 if height_field else None
        id_index = (2 if height_field else 1) if id_field else None
        rows, invalid = [], 0
        for row in connection.execute(sql, (west, east, south, north)):
            geometry = _geometry_from_gpkg(row[0])
            if geometry is None:
                invalid += 1
                continue
            height = _finite_or_none(row[height_index]) if height_index is not None else None
            identifier = row[id_index] if id_index is not None else None
            rows.append((geometry, height, identifier))
        info["invalid_geometry_count"] = invalid
        info["id_field"] = id_field
        return rows
    finally:
        connection.close()


def _geometry_from_gpkg(blob):
    if blob is None:
        return None
    try:
        return shapely.from_wkb(_gpkg_wkb(blob), on_invalid="ignore")
    except Exception:
        return None


def _read_geojson_footprints(info, bbox, height_field, max_features):
    features = info.get("_features")
    if features is None:
        with Path(info["path"]).open("r", encoding="utf-8") as stream:
            features = (json.load(stream).get("features") or [])
    west, south, east, north = bbox
    rows, invalid = [], 0
    for feature in features:
        if not isinstance(feature, dict):
            continue
        geometry = feature.get("geometry")
        if not geometry:
            continue
        try:
            parsed = _shape(geometry)
        except Exception:
            invalid += 1
            continue
        minx, miny, maxx, maxy = parsed.bounds
        if maxx < west or minx > east or maxy < south or miny > north:
            continue
        properties = feature.get("properties") or {}
        height = _finite_or_none(properties.get(height_field)) if height_field else None
        identifier = next(
            (properties.get(name) for name in ("id", "gba_key", "building_id") if properties.get(name) is not None),
            None,
        )
        rows.append((parsed, height, identifier))
        if max_features and len(rows) >= int(max_features):
            break
    info["invalid_geometry_count"] = invalid
    info["id_field"] = None
    return rows


def _read_ogr_footprints(info, bbox, height_field, max_features):
    backend = info.get("_backend")
    if backend == "pyogrio":
        return _read_pyogrio_footprints(info, bbox, height_field, max_features)
    return _read_osgeo_footprints(info, bbox, height_field, max_features)


def _read_pyogrio_footprints(info, bbox, height_field, max_features):
    import pyogrio

    frame = pyogrio.read_dataframe(
        info["path"], layer=info.get("layer"), columns=([height_field] if height_field else None),
        bbox=tuple(bbox), read_geometry=True, use_arrow=False,
    )
    values = frame[height_field].to_numpy() if height_field and height_field in frame.columns else [None] * len(frame)
    rows, invalid = [], 0
    for geometry, value in zip(frame.geometry.to_numpy(), values):
        if geometry is None or geometry.is_empty:
            invalid += 1
            continue
        rows.append((geometry, _finite_or_none(value), None))
        if max_features and len(rows) >= int(max_features):
            break
    info["invalid_geometry_count"] = invalid
    info["id_field"] = None
    return rows


def _read_osgeo_footprints(info, bbox, height_field, max_features):  # pragma: no cover - QGIS/GDAL 环境
    from osgeo import ogr

    dataset = ogr.Open(info["path"], 0)
    if dataset is None:
        raise ValueError("OGR 无法打开数据集")
    try:
        layer = dataset.GetLayerByName(info["layer"]) if info.get("layer") else dataset.GetLayer(0)
        layer.SetSpatialFilterRect(bbox[0], bbox[1], bbox[2], bbox[3])
        rows, invalid = [], 0
        for feature in layer:
            geometry = feature.GetGeometryRef()
            if geometry is None:
                invalid += 1
                continue
            parsed = shapely.from_wkb(bytes(geometry.ExportToWkb()))
            if parsed is None or parsed.is_empty:
                invalid += 1
                continue
            height = None
            if height_field:
                try:
                    height = _finite_or_none(feature.GetField(height_field))
                except Exception:
                    height = None
            rows.append((parsed, height, None))
            if max_features and len(rows) >= int(max_features):
                break
        info["invalid_geometry_count"] = invalid
        info["id_field"] = None
        return rows
    finally:
        dataset = None


# ============================================================================
# 精确聚合（与 L8 事实表生成脚本语义一致）
# ============================================================================

def aggregate_footprint_facts(cells, source_path, *, grid_level, metric_crs=None,
                              layer_name=None, source_descriptor=None, max_features=None):
    """把原始建筑足迹精确聚合到**当前工作区网格层级**上。

    返回结构与 ``BuildingGridService.map()`` 完全一致，因此下游（``grid_attributes.buildings``
    → Layered feasibility adapter → Layered Theta* V2 coarse 包线）无需任何改动即可消费。
    """

    started = time.perf_counter()
    available, reason = geometry_available()
    if not available:
        return _failed(cells, grid_level, {"path": str(source_path)}, reason)
    cell_list = [dict(cell) for cell in (cells or []) if isinstance(cell, dict)]
    if not cell_list:
        return _failed(cells, grid_level, {"path": str(source_path)}, "no_grid_cells")

    workspace_bbox = _union_bbox(cell_list)
    if workspace_bbox is None:
        return _failed(cells, grid_level, {"path": str(source_path)}, "grid_cells_without_bbox")
    metric_crs = metric_crs or metric_crs_for_bbox(workspace_bbox)

    try:
        info = dict(source_descriptor or discover_vector_layer(source_path, layer_name))
        footprints, info = read_footprints(
            source_path, workspace_bbox, layer_name=layer_name, info=info,
            max_features=max_features,
        )
    except (OSError, ValueError, RuntimeError, sqlite3.DatabaseError) as exc:
        return _failed(cells, grid_level, {"path": str(source_path)}, str(exc))

    source_crs = _epsg_code(info.get("srs_id")) or _crs_to_epsg(info.get("crs"))
    if source_crs is None:
        return _failed(
            cells, grid_level, {"path": str(source_path)}, "source_crs_unknown",
            source_info=_source_info(info),
        )

    try:
        to_metric = pyproj.Transformer.from_crs(source_crs, metric_crs, always_xy=True)
        to_wgs84 = pyproj.Transformer.from_crs(metric_crs, "EPSG:4326", always_xy=True)
    except Exception as exc:  # pragma: no cover - pyproj 数据缺失
        return _failed(cells, grid_level, {"path": str(source_path)}, f"crs_transform_unavailable:{exc}",
                       source_info=_source_info(info))

    # ---- 1) footprint 米制几何 + 质心（质心规则与 L8 事实表脚本一致） --------------
    metric_geometries, heights, identifiers = [], [], []
    unparseable = 0
    for geometry, height, identifier in footprints:
        projected = _project(to_metric, geometry)
        if projected is None or projected.is_empty:
            unparseable += 1
            continue
        metric_geometries.append(projected)
        heights.append(height)
        identifiers.append(identifier)

    if not metric_geometries:
        return _empty_result(cells, grid_level, info, metric_crs, cell_list, started)

    centroids = [geometry.centroid for geometry in metric_geometries]
    centroid_lon, centroid_lat = _project_points(to_wgs84, centroids)

    stats, centroids_outside = _centroid_allocation(
        cell_list, centroid_lon, centroid_lat, heights,
    )

    # ---- 2) 网格 cell 米制 polygon + 精确相交面积 --------------------------------
    cell_metric, cell_area = _cell_metric_polygons(cell_list, to_metric)
    tree = shapely.STRtree(metric_geometries)
    area_sums = {}
    invalid_intersections = 0
    touched = set()
    for index, cell in enumerate(cell_list):
        polygon = cell_metric.get(cell.get("grid_id"))
        if polygon is None:
            continue
        try:
            candidates = tree.query(polygon, predicate="intersects")
        except Exception:
            candidates = []
        total = 0.0
        for candidate in candidates:
            target = metric_geometries[int(candidate)]
            try:
                intersection = polygon.intersection(target)
            except Exception:
                repaired = _make_valid(target)
                if repaired is None:
                    invalid_intersections += 1
                    continue
                try:
                    intersection = polygon.intersection(repaired)
                except Exception:
                    invalid_intersections += 1
                    continue
            if intersection.is_empty:
                continue
            total += float(intersection.area)
        if candidates is not None and len(candidates):
            touched.add(cell.get("grid_id"))
        area_sums[cell.get("grid_id")] = total

    mapped, covered = {}, 0
    for cell in cell_list:
        grid_id = cell.get("grid_id")
        inside = _contains(info.get("extent"), cell.get("bbox"))
        entry = stats.get(grid_id)
        area = float(area_sums.get(grid_id) or 0.0)
        cell_area_m2 = float(cell_area.get(grid_id) or 0.0)
        if entry is None and not area:
            if inside:
                mapped[grid_id] = _zero_cell()
                covered += 1
            else:
                mapped[grid_id] = _missing_cell("outside_coverage")
            continue
        mapped[grid_id] = _aggregated_cell(entry, area, cell_area_m2)
        covered += 1

    elapsed = time.perf_counter() - started
    status = "passed" if covered == len(cell_list) else "missing_data"
    return {
        "status": status,
        "source": _source_info(info, metric_crs, workspace_bbox),
        "algorithm_id": ALGORITHM_ID,
        "algorithm_version": ALGORITHM_VERSION,
        "grid_level": grid_level,
        "count": len(cell_list),
        "covered_count": covered,
        "metadata": {
            "mapping": COVERAGE_METHOD,
            "aggregation_method": "exact_footprint_intersection_and_centroid_allocation",
            "source_grid_level": None,
            "count_allocation": COUNT_ALLOCATION,
            "height_valid_rule": HEIGHT_VALID_RULE,
            "coverage_method": COVERAGE_METHOD,
            "area_crs": metric_crs,
            "area_crs_basis": AREA_CRS_BASIS,
            "source_crs": source_crs,
            "source_height_field": info.get("height_field"),
            "source_format": info.get("format"),
            "source_layer": info.get("layer"),
            "footprint_count_scanned": len(footprints),
            "footprint_count_projected": len(metric_geometries),
            "footprint_count_touched_cells": len(touched),
            "centroid_outside_workspace_count": centroids_outside,
            "unparseable_geometry_count": unparseable,
            "invalid_geometry_count": int(info.get("invalid_geometry_count") or 0),
            "invalid_intersection_count": invalid_intersections,
            "zero_semantics": "inside_declared_source_extent_without_intersecting_footprint",
            "outside_semantics": OUTSIDE_SEMANTICS,
            "elapsed_s": round(elapsed, 3),
            "fields": sorted(info.get("fields") or []),
            "not_l8_native": True,
            "cross_level_interpolation": False,
            "cross_level_averaging": False,
        },
        "cells": mapped,
    }


def _project(transformer, geometry):
    try:
        return shapely.transform(
            geometry,
            lambda coords: _transform_array(transformer, coords),
        )
    except Exception:
        return None


def _transform_array(transformer, coords):
    x, y = transformer.transform(coords[:, 0], coords[:, 1])
    return _numpy.column_stack([x, y])


def _project_points(transformer, points):
    xs = _numpy.array([point.x for point in points], dtype=float)
    ys = _numpy.array([point.y for point in points], dtype=float)
    lon, lat = transformer.transform(xs, ys)
    return lon, lat


def _make_valid(geometry):
    try:
        repaired = shapely.make_valid(geometry)
    except Exception:
        return None
    if repaired is None or repaired.is_empty:
        return None
    return repaired


def _cell_metric_polygons(cell_list, transformer):
    """一次性把全部 cell 环投影到米制（批量调用 pyproj，避免逐 cell 开销）。"""

    rings = []
    for cell in cell_list:
        west, south, east, north = (float(value) for value in cell["bbox"])
        rings.append([[west, south], [east, south], [east, north], [west, north], [west, south]])
    flat = _numpy.array(rings, dtype=float).reshape(-1, 2)
    x, y = transformer.transform(flat[:, 0], flat[:, 1])
    projected = _numpy.column_stack([x, y]).reshape(len(cell_list), 5, 2)
    polygons = shapely.polygons(projected)
    result, areas = {}, {}
    for cell, polygon in zip(cell_list, polygons):
        grid_id = cell.get("grid_id")
        result[grid_id] = polygon
        areas[grid_id] = float(polygon.area)
    return result, areas


def _centroid_allocation(cell_list, centroid_lon, centroid_lat, heights):
    """按质心把建筑分配到 cell（一个建筑只计数一次），并计算高度统计。"""

    index = _build_locate_index(cell_list)
    stats = {}
    outside = 0
    for position in range(len(centroid_lon)):
        lon, lat = float(centroid_lon[position]), float(centroid_lat[position])
        grid_id = _locate_cell(index, lon, lat)
        if grid_id is None:
            outside += 1
            continue
        entry = stats.setdefault(grid_id, {"count": 0, "valid_height_count": 0, "heights": []})
        entry["count"] += 1
        height = heights[position]
        if _height_valid(height):
            entry["valid_height_count"] += 1
            entry["heights"].append(float(height))
    return stats, outside


def _build_locate_index(cell_list):
    """为点定位构建索引。

    不使用"cell 尺寸推算列号"：cell 的经纬度边界是 ``float(Fraction)`` 的结果，相邻边界的
    差值带有浮点误差，用它反推列号会在边界附近错位（真实数据回归实测：整格质心全部落到
    不存在的邻居上）。这里改成对实际边界值做二分查找，完全避免浮点反推。
    """

    columns, rows = set(), set()
    lookup = {}
    for cell in cell_list:
        west, south, east, north = (float(value) for value in cell["bbox"])
        key = _bbox_key(west, south)
        lookup[key] = (cell.get("grid_id"), east, north)
        columns.add(key[0])
        rows.add(key[1])
    return sorted(columns), sorted(rows), lookup


def _locate_cell(index, lon, lat):
    import bisect

    columns, rows, lookup = index
    column = bisect.bisect_right(columns, round(lon, 9)) - 1
    row = bisect.bisect_right(rows, round(lat, 9)) - 1
    if column < 0 or row < 0:
        return None
    entry = lookup.get((columns[column], rows[row]))
    if entry is None:
        return None
    grid_id, east, north = entry
    if round(lon, 9) > round(east, 9) or round(lat, 9) > round(north, 9):
        return None
    return grid_id


def _bbox_key(west, south):
    return (round(float(west), 9), round(float(south), 9))


def _height_valid(height):
    return isinstance(height, (int, float)) and not isinstance(height, bool) and math.isfinite(height) and height > 0


def _aggregated_cell(entry, area_m2, cell_area_m2):
    entry = entry or {"count": 0, "valid_height_count": 0, "heights": []}
    count = int(entry["count"])
    heights = sorted(entry["heights"])
    ratio = (area_m2 / cell_area_m2) if cell_area_m2 > 0 else 0.0
    ratio = max(0.0, min(1.0, ratio))
    finite = _numpy.array(heights, dtype=float) if heights else None
    # 键集合与 L8 事实表直接映射（BuildingGridService._cell）完全一致：下游消费者
    # （Layered feasibility adapter、Risk Framework V2）不需要区分事实是怎么聚合出来的。
    return {
        "status": "passed",
        "building_count": count,
        "building_area_m2": area_m2,
        "building_coverage_ratio": ratio,
        "height_mean_m": float(finite.mean()) if finite is not None else None,
        "height_p95_m": float(_numpy.percentile(finite, 95)) if finite is not None else None,
        "height_max_m": float(finite.max()) if finite is not None else None,
        "valid_height_fraction": (entry["valid_height_count"] / count) if count else None,
        "building_exposure": ratio,
    }


def _zero_cell():
    return {
        "status": "passed", "building_count": 0, "building_area_m2": 0.0,
        "building_coverage_ratio": 0.0, "height_mean_m": None, "height_p95_m": None,
        "height_max_m": None, "valid_height_fraction": None, "building_exposure": 0.0,
    }


def _missing_cell(reason):
    return {
        "status": "missing_data", "reason": reason,
        "building_count": None, "building_area_m2": None,
        "building_coverage_ratio": None, "height_mean_m": None,
        "height_p95_m": None, "height_max_m": None,
        "valid_height_fraction": None, "building_exposure": None,
    }


def _empty_result(cells, grid_level, info, metric_crs, cell_list, started):
    """源里没有任何可投影的 footprint：extent 内的 cell 是**已知 0**，extent 外是 unknown。"""

    mapped, covered = {}, 0
    for cell in cell_list:
        grid_id = cell.get("grid_id")
        if _contains(info.get("extent"), cell.get("bbox")):
            mapped[grid_id] = _zero_cell()
            covered += 1
        else:
            mapped[grid_id] = _missing_cell("outside_coverage")
    return {
        "status": "passed" if covered == len(cell_list) else "missing_data",
        "source": _source_info(info, metric_crs, _union_bbox(cell_list)),
        "algorithm_id": ALGORITHM_ID, "algorithm_version": ALGORITHM_VERSION,
        "grid_level": grid_level, "count": len(cell_list), "covered_count": covered,
        "metadata": {
            "mapping": COVERAGE_METHOD,
            "aggregation_method": "exact_footprint_intersection_and_centroid_allocation",
            "count_allocation": COUNT_ALLOCATION, "height_valid_rule": HEIGHT_VALID_RULE,
            "coverage_method": COVERAGE_METHOD, "area_crs": metric_crs,
            "area_crs_basis": AREA_CRS_BASIS, "source_crs": _epsg_code(info.get("srs_id")),
            "footprint_count_scanned": 0, "footprint_count_projected": 0,
            "zero_semantics": "inside_declared_source_extent_without_intersecting_footprint",
            "outside_semantics": OUTSIDE_SEMANTICS,
            "empty_footprint_set": True,
            "elapsed_s": round(time.perf_counter() - started, 3),
            "not_l8_native": True, "cross_level_interpolation": False,
            "cross_level_averaging": False,
        },
        "cells": mapped,
    }


def _failed(cells, grid_level, source, message, status="missing_data", source_info=None):
    cell_list = [cell for cell in (cells or []) if isinstance(cell, dict)]
    result = {
        "status": status, "source": source_info or dict(source or {}),
        "algorithm_id": ALGORITHM_ID, "algorithm_version": ALGORITHM_VERSION,
        "grid_level": grid_level, "count": len(cell_list), "covered_count": 0,
        "message": str(message),
        "metadata": {
            "mapping": COVERAGE_METHOD, "aggregation_failed": True,
            "aggregation_failure_reason": str(message),
            "count_allocation": COUNT_ALLOCATION, "height_valid_rule": HEIGHT_VALID_RULE,
            "coverage_method": COVERAGE_METHOD, "area_crs_basis": AREA_CRS_BASIS,
            "not_l8_native": True, "cross_level_interpolation": False,
            "cross_level_averaging": False,
        },
        "cells": {
            cell.get("grid_id"): _missing_cell("footprint_aggregation_unavailable")
            for cell in cell_list
        },
    }
    return result


def _source_info(info, metric_crs=None, workspace_bbox=None):
    return {
        "path": info.get("path"), "layer": info.get("layer"), "format": info.get("format"),
        "crs": info.get("crs"), "extent": info.get("extent"),
        "feature_count": info.get("feature_count"), "geometry_type": info.get("geometry_type"),
        "spatial_index": info.get("spatial_index"), "read_method": info.get("read_method"),
        "height_field": info.get("height_field"), "area_crs": metric_crs,
        "workspace_bbox": list(workspace_bbox) if workspace_bbox else None,
    }


def _epsg_code(srs_id):
    try:
        code = int(srs_id)
    except (TypeError, ValueError):
        return None
    return f"EPSG:{code}" if code > 0 else None


def _crs_to_epsg(crs):
    if crs is None:
        return None
    if isinstance(crs, int):
        return f"EPSG:{crs}"
    text = str(crs).strip()
    if not text:
        return None
    if text.upper().startswith("EPSG:"):
        return text.upper()
    if text.upper() in ("OGC:CRS84", "CRS84", "WGS84"):
        return "EPSG:4326"
    return None


def _union_bbox(cell_list):
    boxes = [cell["bbox"] for cell in cell_list if cell.get("bbox") and len(cell["bbox"]) == 4]
    if not boxes:
        return None
    return [
        min(float(box[0]) for box in boxes), min(float(box[1]) for box in boxes),
        max(float(box[2]) for box in boxes), max(float(box[3]) for box in boxes),
    ]


def _contains(outer, inner):
    return bool(
        outer and all(value is not None for value in outer)
        and inner and len(inner) == 4
        and outer[0] <= float(inner[0]) and outer[1] <= float(inner[1])
        and outer[2] >= float(inner[2]) and outer[3] >= float(inner[3])
    )


def _finite_or_none(value):
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


# ============================================================================
# 前端建筑轮廓图层（只读 GeoJSON）
# ============================================================================

def footprints_geojson(source_path, bbox, *, limit=2000, tolerance_deg=None,
                       layer_name=None, include_attributes=False) -> dict:
    """按视图 bbox 返回建筑轮廓 GeoJSON（供前端"建筑"图层按需拉取）。

    * 只返回与 ``bbox`` 相交的足迹，并把数量限制在 ``limit``（超出时 ``truncated=true``）；
    * ``tolerance_deg`` 由前端按当前缩放级别给出（低 zoom 给更大的容差，轮廓更粗），
      这里只做几何简化，**不改变任何业务数据**；
    * 无几何库、源不可读、缺空间索引等情况全部以 ``status`` + 中文 ``reason`` 返回，绝不伪造。
    """

    available, reason = geometry_available()
    if not available:
        return {"status": "unsupported", "reason": reason, "type": "FeatureCollection", "features": []}
    if not bbox or len(bbox) != 4:
        return {"status": "invalid_request", "reason": "bbox 必须包含四个坐标", "type": "FeatureCollection", "features": []}
    try:
        box = [float(value) for value in bbox]
    except (TypeError, ValueError):
        return {"status": "invalid_request", "reason": "bbox 坐标必须是数值", "type": "FeatureCollection", "features": []}
    if box[0] >= box[2] or box[1] >= box[3]:
        return {"status": "invalid_request", "reason": "bbox 范围无效", "type": "FeatureCollection", "features": []}
    limit = max(1, min(int(limit or 2000), 20000))
    try:
        rows, info = read_footprints(source_path, box, layer_name=layer_name, max_features=limit)
    except (OSError, ValueError, RuntimeError, sqlite3.DatabaseError) as exc:
        return {
            "status": "unavailable", "reason": str(exc), "type": "FeatureCollection",
            "features": [], "source": {"path": str(source_path)},
        }

    features = []
    for geometry, height, identifier in rows:
        render = geometry
        if tolerance_deg:
            try:
                simplified = geometry.simplify(float(tolerance_deg), preserve_topology=True)
                if simplified is not None and not simplified.is_empty and simplified.is_valid:
                    render = simplified
            except Exception:
                render = geometry
        properties = {"source": info.get("format")}
        if identifier is not None:
            properties["id"] = identifier
        if height is not None:
            properties["height_m"] = height
        if include_attributes:
            properties["geometry_type"] = render.geom_type
        features.append({
            "type": "Feature",
            "id": str(identifier) if identifier is not None else None,
            "geometry": _geometry_mapping(render),
            "properties": properties,
        })
    return {
        "status": "passed",
        "type": "FeatureCollection",
        "features": features,
        "count": len(features),
        "truncated": len(rows) >= limit,
        "source": _source_info(info, workspace_bbox=box),
        "query": {"bbox": box, "limit": limit, "tolerance_deg": tolerance_deg},
    }


def _geometry_mapping(geometry):
    try:
        from shapely.geometry import mapping as _mapping
        return _mapping(geometry)
    except Exception:  # pragma: no cover
        return None


__all__ = [
    "ALGORITHM_ID", "ALGORITHM_VERSION", "AREA_CRS_BASIS", "COUNT_ALLOCATION",
    "COVERAGE_METHOD", "DEFAULT_HEIGHT_FIELD", "HEIGHT_VALID_RULE", "OUTSIDE_SEMANTICS",
    "aggregate_footprint_facts", "discover_vector_layer", "footprints_geojson",
    "geometry_available", "metric_crs_for_bbox", "read_footprints",
]
