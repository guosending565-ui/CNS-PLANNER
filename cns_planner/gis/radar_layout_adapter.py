"""Radar Surveillance Layout V1 — GIS 边界（只读事实，不判定业务结论）。

职责严格限定为**读取事实**：

1. **地形正高**：复用既有已确认的 FABDEM DTM 窗口采样器
   （:class:`cns_planner.gis.fine_environment_adapter.FabdemWindowTerrainSource`），
   只有 ``vertical_reference == "egm2008_orthometric"`` 才被接受；
2. **陆域 polygon**：从**显式配置**的 ``land_mask`` 矢量源读取 Polygon/MultiPolygon，
   用于逐点 ``land | sea | unknown`` 判定；
3. **雷达原点高度**：``terrain_elevation_egm2008 + radar_mount_height_m`` ——
   挂高只能来自显式策略（前端工程示例参数或来源证据），**绝不在后端写死**。

陆域判定规则（保守）
--------------------

* 点在陆域 polygon 内部 **或落在边界上** ⇒ ``land``；
* 点在 polygon 外 ⇒ ``sea``；
* 陆域源未配置 / 不可读 / 几何库缺失 ⇒ ``unknown``（**fail-closed**）。

**绝不用 DEM NoData 推断海洋**：本模块没有任何 DEM-derived 海洋/陆地结论。
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

#: 陆地/海洋判定来源标识。
LAND_MASK_SEMANTICS = "explicit_land_polygon_containment_boundary_counts_as_land"

#: 允许的矢量格式（与既有 :mod:`cns_planner.gis.path_resolver` 一致的最小集合）。
LAND_MASK_SUFFIXES = (".gpkg", ".shp", ".geojson", ".json")

#: 常见陆域/海岸线图层名关键词（仅用于在 gpkg 中自动挑层；不改变任何业务语义）。
LAND_LAYER_KEYWORDS = (
    "land", "landmass", "coastline", "coast", "shoreline", "island",
    "陆", "陆域", "陆地", "海岸", "岸线", "岛屿", "省界", "行政",
)

#: 明显的"非陆域"图层名：这些图层即便包含 Polygon 也不得当作陆域掩膜。
LAND_LAYER_EXCLUSIONS = (
    "适飞空域", "uom", "空域", "building", "建筑", "population", "人口",
    "route", "航路", "航线", "grid", "网格", "tower", "铁塔", "站点",
)


def geometry_available():
    """shapely 是否可用（不可用时陆域判定为 ``unknown``，绝不猜）。"""

    try:
        import shapely  # noqa: F401
        from shapely.prepared import prep  # noqa: F401
    except Exception as exc:  # pragma: no cover - 环境缺库
        return False, f"geometry_libraries_unavailable:{exc}"
    return True, None


# ------------------------------------------------------------------ land mask readiness


def land_mask_hint(path):
    """只读提示：陆域源是否配置（不打开数据集）。"""

    if not path:
        return {
            "ok": False,
            "path": None,
            "reason": "land_mask_not_configured",
            "detail": (
                "项目当前没有任何明确可用的陆域 Polygon 来源；"
                "surface_class 保持 unknown（fail-closed），绝不自动按 sea 处理，"
                "也绝不用 DEM NoData 推断海洋。"
            ),
            "semantics": LAND_MASK_SEMANTICS,
        }
    resolved = Path(str(path))
    if not resolved.is_file():
        return {
            "ok": False,
            "path": str(resolved),
            "reason": "land_mask_source_missing",
            "detail": "配置的陆域源路径不存在",
            "semantics": LAND_MASK_SEMANTICS,
        }
    if resolved.suffix.lower() not in LAND_MASK_SUFFIXES:
        return {
            "ok": False,
            "path": str(resolved),
            "reason": "land_mask_format_unsupported",
            "detail": f"仅支持 {'/'.join(LAND_MASK_SUFFIXES)}",
            "semantics": LAND_MASK_SEMANTICS,
        }
    available, reason = geometry_available()
    if not available:
        return {
            "ok": False, "path": str(resolved), "reason": reason,
            "detail": "缺少几何库，无法做点在多边形内判定",
            "semantics": LAND_MASK_SEMANTICS,
        }
    return {
        "ok": True, "path": str(resolved), "reason": None,
        "detail": None, "semantics": LAND_MASK_SEMANTICS,
    }


# ------------------------------------------------------------------ land polygons
#
# 这里**不复用**建筑足迹读取路径：``read_footprints`` 需要 bbox、优先挑 Polygon 图层并
# 且对缺失 RTree 的 gpkg 直接拒绝，这三条假设都只对建筑成立。陆域掩膜需要"整层任意
# 点包含判定"，因此本模块自行做只读全层读取，并把每个多边形的外环与 bbox 缓存下来，
# 用 bbox 预筛避免逐点全量几何运算。


def _gpkg_land_layers(path):
    """枚举 GeoPackage 的要素图层（只读）。"""

    import sqlite3

    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except Exception:  # pragma: no cover - 不可读
        return []
    try:
        rows = connection.execute(
            "SELECT c.table_name, g.column_name, g.geometry_type_name "
            "FROM gpkg_contents c JOIN gpkg_geometry_columns g "
            "ON g.table_name = c.table_name WHERE c.data_type='features'"
        ).fetchall()
    except Exception:
        return []
    finally:
        connection.close()
    return [(str(table), str(column), str(kind)) for table, column, kind in rows]


def _gpkg_land_geometries(path, table, column):
    import sqlite3

    try:
        from .building_footprint_aggregation import _geometry_from_gpkg
    except Exception:  # pragma: no cover - defensive
        return []
    from .building_footprint_aggregation import _quote

    geometries = []
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except Exception:  # pragma: no cover - 不可读
        return []
    try:
        cursor = connection.execute(
            f"SELECT {_quote(column)} FROM {_quote(table)}"
        )
        for row in cursor:
            geometry = _geometry_from_gpkg(row[0])
            if geometry is not None and not geometry.is_empty:
                geometries.append(geometry)
    except Exception:
        return geometries
    finally:
        connection.close()
    return geometries


def _geojson_land_geometries(path):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return []
    from shapely.geometry import shape

    geometries = []
    for feature in (payload.get("features") if isinstance(payload, dict) else None) or []:
        geometry = (feature or {}).get("geometry")
        if not geometry:
            continue
        try:
            parsed = shape(geometry)
        except Exception:
            continue
        if getattr(parsed, "geom_type", "") in ("Polygon", "MultiPolygon"):
            geometries.append(parsed)
    return geometries


def _ogr_land_geometries(path):
    """shapefile / 其它 OGR 格式：先 pyogrio，再 osgeo（QGIS/GDAL 环境）。"""

    try:
        import pyogrio
        frame = pyogrio.read_dataframe(str(path))
        return [
            geometry for geometry in list(frame.geometry)
            if geometry is not None and not geometry.is_empty
        ]
    except Exception:
        pass
    try:  # pragma: no cover - 需要 QGIS/GDAL 环境
        from osgeo import ogr
        from shapely import wkb as shapely_wkb

        dataset = ogr.Open(str(path), 0)
        if dataset is None:
            return []
        layer = dataset.GetLayer(0)
        geometries = []
        for feature in layer:
            geometry = feature.GetGeometryRef()
            if geometry is None:
                continue
            try:
                parsed = shapely_wkb.loads(bytes(geometry.ExportToWkb()))
            except Exception:
                continue
            if not parsed.is_empty:
                geometries.append(parsed)
        dataset = None
        return geometries
    except Exception:
        return []


def _rings_and_bounds(geometry):
    """``[(ring_xy, (minx, miny, maxx, maxy))]``；只取外环（保守：不挖洞）。"""

    results = []
    if geometry is None or geometry.is_empty:
        return results
    geometry_type = getattr(geometry, "geom_type", "")
    parts = [geometry] if geometry_type == "Polygon" else list(
        getattr(geometry, "geoms", [])
    )
    for part in parts:
        if getattr(part, "geom_type", "") != "Polygon":
            continue
        exterior = getattr(part, "exterior", None)
        if exterior is None:
            continue
        ring = [(float(x), float(y)) for x, y in exterior.coords]
        if len(ring) < 4:
            continue
        xs = [point[0] for point in ring]
        ys = [point[1] for point in ring]
        results.append((ring, (min(xs), min(ys), max(xs), max(ys))))
    return results


def _layer_looks_like_land(name):
    text = str(name or "").strip().lower()
    if not text:
        return False
    if any(marker in text for marker in LAND_LAYER_EXCLUSIONS):
        return False
    return any(marker in text for marker in LAND_LAYER_KEYWORDS)


class LandMaskSource:
    """只读陆域掩膜：逐点 ``land | sea``，证据不可用时返回 ``unknown``。"""

    def __init__(self, path, *, layer_name=None):
        self.path = Path(str(path)) if path else None
        self.layer_name = layer_name
        self._polygons = None
        self._reason = None
        self._prepared = False

    # ---- preparation ---------------------------------------------------------------
    def prepare(self):
        if self._prepared:
            return self._polygons
        self._prepared = True
        hint = land_mask_hint(self.path)
        if not hint["ok"]:
            self._reason = hint["reason"]
            return None
        suffix = self.path.suffix.lower()
        geometries = []
        if suffix in (".geojson", ".json"):
            geometries = _geojson_land_geometries(self.path)
        elif suffix == ".gpkg":
            for table, column, kind in _gpkg_land_layers(self.path):
                if self.layer_name and table != self.layer_name:
                    continue
                if not _layer_looks_like_land(table):
                    continue
                if "POLYGON" not in kind.upper():
                    continue
                geometries.extend(_gpkg_land_geometries(self.path, table, column))
        else:
            geometries = _ogr_land_geometries(self.path)

        polygons = []
        for geometry in geometries:
            polygons.extend(_rings_and_bounds(geometry))
        if not polygons:
            self._reason = "land_mask_no_usable_polygon_layer"
            return None
        self._polygons = polygons
        return self._polygons

    def describe(self):
        return {
            "configured_path": str(self.path) if self.path else None,
            "readiness": land_mask_hint(self.path),
            "polygon_count": len(self._polygons) if self._polygons else None,
            "reason": self._reason,
            "semantics": LAND_MASK_SEMANTICS,
            "dem_nodata_used_to_infer_sea": False,
        }

    # ---- queries -------------------------------------------------------------------
    def classify(self, longitude, latitude):
        """单点判定。返回 ``(surface_class, evidence)``。"""

        available, reason = geometry_available()
        if not available:
            return "unknown", {"reason": reason}
        polygons = self.prepare()
        if polygons is None:
            return "unknown", {
                "reason": self._reason or "land_mask_unavailable",
                "semantics": LAND_MASK_SEMANTICS,
            }
        from shapely.geometry import Point, Polygon

        x, y = float(longitude), float(latitude)
        point = Point(x, y)
        for ring, bounds in polygons:
            minx, miny, maxx, maxy = bounds
            if x < minx or x > maxx or y < miny or y > maxy:
                continue
            try:
                polygon = Polygon(ring)
                # 保守：落在边界上按 land 处理（海岸线/陆域边界保守按 land）。
                if polygon.covers(point):
                    return "land", {
                        "reason": "point_inside_or_on_land_polygon_boundary",
                        "semantics": LAND_MASK_SEMANTICS,
                        "boundary_counts_as_land": True,
                    }
            except Exception:
                continue
        return "sea", {
            "reason": "point_outside_all_configured_land_polygons",
            "semantics": LAND_MASK_SEMANTICS,
        }

    def classify_many(self, points):
        """批量判定：``points`` = ``[(lon, lat), ...]`` -> ``[surface_class, ...]``。"""

        if not points:
            return []
        polygons = self.prepare() if geometry_available()[0] else None
        if polygons is None:
            return ["unknown" for _ in points]
        from shapely.geometry import Point, Polygon
        from shapely.prepared import prep

        prepared = []
        for ring, bounds in polygons:
            try:
                prepared.append((prep(Polygon(ring)), bounds))
            except Exception:
                continue
        result = []
        for longitude, latitude in points:
            if longitude is None or latitude is None:
                result.append("unknown")
                continue
            x, y = float(longitude), float(latitude)
            point = Point(x, y)
            found = "sea"
            for predicate, bounds in prepared:
                minx, miny, maxx, maxy = bounds
                if x < minx or x > maxx or y < miny or y > maxy:
                    continue
                try:
                    if predicate.covers(point):
                        found = "land"
                        break
                except Exception:
                    continue
            result.append(found)
        return result


# ------------------------------------------------------------------ terrain sampling


def route_terrain_source(path):
    """构造逐点地形采样器；不可用时返回 ``None``（调用方 fail-closed）。"""

    if not path:
        return None
    try:
        from .fine_environment_adapter import FabdemWindowTerrainSource
    except Exception:  # pragma: no cover - defensive
        return None
    try:
        return FabdemWindowTerrainSource(str(path))
    except Exception:  # pragma: no cover - defensive
        return None


#: 逐点地形采样窗口半径（度）。只是采样实现细节，不是任何业务阈值。
ROUTE_TERRAIN_SAMPLE_HALF_DEG = 0.0002


class _IdentityTransform:
    authority = "OGC:CRS84"

    @staticmethod
    def to_geographic(point):
        return [float(point[0]), float(point[1])]


def sample_route_terrain(points, *, terrain_source):
    """对航路点批量采样 FABDEM DTM 正高。

    ``points``：``[{key, longitude, latitude}]``。
    返回 ``{key: {"status", "elevation_m", "vertical_reference", "reason"}}``，
    任一点不可用即 ``unresolved``（**绝不补 0**）。
    """

    unresolved = {
        str(item["key"]): {
            "status": "unresolved", "elevation_m": None,
            "vertical_reference": "unknown",
            "reason": "terrain_source_not_configured",
        }
        for item in points or []
    }
    if terrain_source is None:
        return unresolved
    reference = getattr(terrain_source, "vertical_reference", None) or "unknown"
    usable = getattr(terrain_source, "usable", None)
    if callable(usable):
        ok, reason = usable()
        if not ok:
            return {
                key: {
                    "status": "unresolved", "elevation_m": None,
                    "vertical_reference": reference, "reason": str(reason),
                }
                for key in unresolved
            }
    inputs, order = [], []
    for item in points or []:
        key = str(item["key"])
        longitude, latitude = item.get("longitude"), item.get("latitude")
        if not isinstance(longitude, (int, float)) or not isinstance(latitude, (int, float)):
            continue
        inputs.append({
            "fine_cell_id": key,
            "bbox_metric": [
                longitude - ROUTE_TERRAIN_SAMPLE_HALF_DEG,
                latitude - ROUTE_TERRAIN_SAMPLE_HALF_DEG,
                longitude + ROUTE_TERRAIN_SAMPLE_HALF_DEG,
                latitude + ROUTE_TERRAIN_SAMPLE_HALF_DEG,
            ],
        })
        order.append(key)
    if not inputs:
        return unresolved
    try:
        sampled = terrain_source.sample_cells(inputs, transform=_IdentityTransform())
    except Exception as exc:  # pragma: no cover - GIS 运行时失败
        return {
            key: {
                "status": "unresolved", "elevation_m": None,
                "vertical_reference": reference,
                "reason": f"terrain_sampling_failed:{exc}",
            }
            for key in unresolved
        }
    sampled = sampled if isinstance(sampled, dict) else {}
    result = deepcopy(unresolved)
    for key in order:
        fact = sampled.get(key) or {}
        elevation = fact.get("surface_elevation_max_egm2008_m")
        passed = (
            str(fact.get("data_status") or "") == "passed"
            and isinstance(elevation, (int, float)) and not isinstance(elevation, bool)
        )
        result[key] = {
            "status": "passed" if passed else "unresolved",
            "elevation_m": float(elevation) if passed else None,
            "vertical_reference": reference,
            "reason": None if passed else str(
                fact.get("reason") or "terrain_elevation_unavailable"
            ),
            "valid_pixel_count": fact.get("valid_pixel_count"),
            "nodata_pixel_count": fact.get("nodata_pixel_count"),
            "sampling": fact.get("sampling"),
        }
    return result


# ------------------------------------------------------------------ radar origin


def resolve_radar_origins(*, towers, obstacle_profiles, mount_assumption):
    """逐塔解析**雷达原点** EGM2008 正高。

    ``radar_origin_egm2008 = terrain_elevation_egm2008 + radar_mount_height_m``

    其中地形正高优先取真实已知站址/安装高度：
    ``obstacle_profile.tower_top_orthometric_m``（= FABDEM 地形 + 楼面建筑高度 + 源数据塔身高度）；
    挂高只能来自显式策略。两者任一缺失即 ``unresolved``（**绝不填 0、绝不写死**）。
    """

    mount = mount_assumption if isinstance(mount_assumption, dict) else {}
    mount_height = mount.get("radar_mount_height_m")
    mount_status = str(mount.get("status") or "not_configured")
    profiles = obstacle_profiles if isinstance(obstacle_profiles, dict) else {}
    items = profiles.get("items") if isinstance(profiles.get("items"), dict) else profiles

    records, unresolved = [], []
    for tower in towers or []:
        tower_id = str(tower.get("tower_id") or "")
        if not tower_id:
            continue
        profile = items.get(tower_id) if isinstance(items, dict) else None
        profile = profile if isinstance(profile, dict) else {}
        top = profile.get("tower_top_orthometric_m")
        record = {
            "tower_id": tower_id,
            "name": tower.get("name") or tower_id,
            "longitude": tower.get("longitude"),
            "latitude": tower.get("latitude"),
            "site_type": tower.get("site_type"),
            "tower_top_orthometric_m": top if isinstance(top, (int, float)) else None,
            "tower_obstacle_status": profile.get("status"),
            "origin_egm2008_m": None,
            "origin_source": None,
            "origin_confirmed": False,
            "origin_parameter_origin": "not_configured",
            "origin_reason": None,
            "source": deepcopy(tower.get("source")),
        }
        if not isinstance(top, (int, float)) or isinstance(top, bool):
            record["origin_reason"] = (
                "tower_top_egm2008_unresolved:"
                f"{profile.get('vertical_status') or 'tower_obstacle_profile_missing'}"
            )
            unresolved.append(record)
            records.append(record)
            continue
        if not isinstance(mount_height, (int, float)) or isinstance(mount_height, bool):
            record["origin_reason"] = "radar_mount_height_not_configured"
            unresolved.append(record)
            records.append(record)
            continue
        record["origin_egm2008_m"] = float(top) + float(mount_height)
        record["origin_source"] = (
            "tower_top_orthometric_m_plus_explicit_radar_mount_height_m"
        )
        record["origin_confirmed"] = bool(mount.get("confirmed") is True)
        record["origin_parameter_origin"] = str(
            mount.get("parameter_origin") or "engineering_assumption"
        )
        record["mount_height_m"] = float(mount_height)
        record["mount_height_source"] = mount.get("source")
        record["mount_height_confirmed"] = bool(mount.get("confirmed") is True)
        records.append(record)

    return {
        "status": "passed" if any(
            item["origin_egm2008_m"] is not None for item in records
        ) else "missing_data",
        "mount_assumption": deepcopy(mount),
        "mount_assumption_status": mount_status,
        "records": records,
        "by_tower": {str(item["tower_id"]): item for item in records},
        "unresolved": unresolved,
        "unresolved_count": len(unresolved),
        "semantics": "radar_origin_egm2008_from_tower_top_plus_explicit_mount_height",
        "backend_hardcoded_mount_height": False,
    }


# ------------------------------------------------------------------ readiness bundle


def radar_layout_source_status(state, paths):
    """只读 readiness：地形 / 陆域 / 挂高是否就绪（不打开大数据集）。"""

    state = state if isinstance(state, dict) else {}
    paths = paths if isinstance(paths, dict) else {}
    terrain_path = paths.get("terrain_dtm")
    terrain = {
        "configured_path": str(terrain_path) if terrain_path else None,
        "available": bool(terrain_path) and Path(str(terrain_path)).is_file(),
        "role": "terrain_dtm",
        "vertical_reference_requirement": "egm2008_orthometric",
        "reason": None if terrain_path else "terrain_dtm_not_configured",
    }
    if terrain_path and not terrain["available"]:
        terrain["reason"] = "terrain_dtm_source_missing"
    land = land_mask_hint(paths.get("land_mask"))
    profile = radar_mount_assumption_status(state)
    return {
        "terrain": terrain,
        "land_mask": land,
        "radar_mount_height": profile,
        "semantics": "read_only_source_status_no_dataset_opened",
    }


def radar_mount_assumption_status(state):
    assumption = (state or {}).get("radar_surveillance_policy") or {}
    mount = (assumption.get("radar_mount_height") or {}) if isinstance(assumption, dict) else {}
    if not isinstance(mount, dict) or mount.get("radar_mount_height_m") is None:
        return {
            "status": "not_configured",
            "radar_mount_height_m": None,
            "confirmed": False,
            "parameter_origin": "not_configured",
            "source": None,
            "detail": (
                "缺少真实安装高度证据。后端绝不写死虚假塔高/安装高度；"
                "前端可提供统一工程示例参数，但会保存为 "
                "source/confirmed=false/parameter_origin=engineering_assumption，"
                "并保持 missing_data/pending_confirmation。"
            ),
        }
    return {
        "status": str(mount.get("status") or "pending_confirmation"),
        "radar_mount_height_m": mount.get("radar_mount_height_m"),
        "mount_height_basis": mount.get("mount_height_basis"),
        "confirmed": bool(mount.get("confirmed") is True),
        "parameter_origin": str(mount.get("parameter_origin") or "engineering_assumption"),
        "source": mount.get("source"),
        "detail": None,
    }


__all__ = [
    "LAND_LAYER_EXCLUSIONS", "LAND_LAYER_KEYWORDS", "LAND_MASK_SEMANTICS",
    "LAND_MASK_SUFFIXES", "ROUTE_TERRAIN_SAMPLE_HALF_DEG", "LandMaskSource",
    "geometry_available", "land_mask_hint", "radar_layout_source_status",
    "radar_mount_assumption_status", "resolve_radar_origins", "route_terrain_source",
    "sample_route_terrain",
]