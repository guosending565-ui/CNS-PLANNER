"""GIS boundary for :mod:`cns_planner.domain.tower_obstacle` facts.

本模块只产出**事实**，不做任何净空判定：

* **地形**：复用既有已确认的 FABDEM DTM 窗口采样器
  （:class:`cns_planner.gis.fine_environment_adapter.FabdemWindowTerrainSource`）。
  每个铁塔用**自己的**一个小窗口采样，绝不按工作区网格粗化位置。只有
  ``vertical_reference == "egm2008_orthometric"`` 才被接受；
* **建筑高度**：对楼面塔，直接在**真实建筑足迹数据源**上按塔坐标做包含查询，
  取该点的建筑高度。这是层级无关的精确几何查询，不依赖 L7/L8 建筑网格事实，
  因此工作区降级不会让楼面塔的障碍高度被粗化；
* 任何一步不可用都是 ``unresolved``（fail-closed），**绝不**把建筑高度当 0、
  也绝不把源数据 ``elevation_m`` 当成 EGM2008 正高。

两个 ``*_HALF_DEG`` 常量只是**查询窗口半径**（≈ 22 m / ≈ 66 m），不是任何业务
净空值；真正的垂直/水平净空只在 ``tower_clearance_policy`` 里显式配置。
"""

from __future__ import annotations

from pathlib import Path

#: 地形采样窗口半径（度）。只是采样实现细节，不是净空。
TOWER_TERRAIN_SAMPLE_HALF_DEG = 0.0002
#: 建筑足迹搜索窗口半径（度）。只是空间索引查询范围，不是净空。
TOWER_BUILDING_SEARCH_HALF_DEG = 0.0006

TOWER_OBSTACLE_FACT_METHOD = (
    "fabdem_dtm_point_window_max_plus_real_building_footprint_height"
)


class _GeographicIdentityTransform:
    """塔坐标与其周围 bbox 都已经是 OGC:CRS84，不需要再投影。"""

    authority = "OGC:CRS84"

    @staticmethod
    def to_geographic(point):
        return [float(point[0]), float(point[1])]


def _number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if number == number and number not in (float("inf"), float("-inf")):
            return number
    return None


def _bbox_around(longitude, latitude, half_deg):
    return [
        longitude - half_deg, latitude - half_deg,
        longitude + half_deg, latitude + half_deg,
    ]


def _terrain_facts(towers, terrain_source):
    """逐塔采样 FABDEM DTM；基准未确认时全部 unresolved（绝不猜基准）。"""

    facts = {}
    if terrain_source is None:
        return {
            tower_id: {"status": "unresolved", "elevation_m": None,
                       "vertical_reference": "unknown", "reason": "terrain_source_not_configured"}
            for tower_id in towers
        }
    usable = getattr(terrain_source, "usable", None)
    if callable(usable):
        ok, reason = usable()
        if not ok:
            reference = getattr(terrain_source, "vertical_reference", None) or "unknown"
            return {
                tower_id: {"status": "unresolved", "elevation_m": None,
                           "vertical_reference": reference, "reason": str(reason)}
                for tower_id in towers
            }
    reference = getattr(terrain_source, "vertical_reference", None) or "unknown"
    inputs = []
    order = []
    for tower_id, tower in towers.items():
        longitude = _number(tower.get("longitude"))
        latitude = _number(tower.get("latitude"))
        if longitude is None or latitude is None:
            continue
        inputs.append({
            "fine_cell_id": tower_id,
            "bbox_metric": _bbox_around(longitude, latitude, TOWER_TERRAIN_SAMPLE_HALF_DEG),
        })
        order.append(tower_id)
    if not inputs:
        return {}
    try:
        sampled = terrain_source.sample_cells(inputs, transform=_GeographicIdentityTransform())
    except Exception as exc:  # pragma: no cover - GIS 运行时失败
        return {
            tower_id: {"status": "unresolved", "elevation_m": None,
                       "vertical_reference": reference, "reason": f"terrain_sampling_failed:{exc}"}
            for tower_id in order
        }
    sampled = sampled if isinstance(sampled, dict) else {}
    for tower_id in order:
        fact = sampled.get(tower_id) or {}
        elevation = _number(fact.get("surface_elevation_max_egm2008_m"))
        passed = str(fact.get("data_status") or "") == "passed" and elevation is not None
        facts[tower_id] = {
            "status": "passed" if passed else "unresolved",
            "elevation_m": elevation,
            "vertical_reference": reference,
            "reason": None if passed else str(
                fact.get("reason") or "terrain_elevation_unavailable"
            ),
            "sampling": fact.get("sampling"),
            "valid_pixel_count": fact.get("valid_pixel_count"),
            "nodata_pixel_count": fact.get("nodata_pixel_count"),
        }
    return facts


def _source_epsg(info):
    try:
        import pyproj
    except Exception:  # pragma: no cover - pyproj 不可用
        return None
    srs_id = info.get("srs_id")
    crs_text = info.get("crs")
    for candidate in (f"EPSG:{srs_id}" if srs_id else None, crs_text):
        if not candidate:
            continue
        try:
            crs = pyproj.CRS.from_user_input(candidate)
        except Exception:
            continue
        if crs.is_geographic:
            return 4326
        code = crs.to_epsg()
        if code:
            return code
    return None


def _building_facts(towers, building_source):
    """对每个楼面塔在真实建筑足迹上做包含查询（层级无关的精确几何）。

    ``building_source`` 是 ``{"ok","path","layer_name","reason"}``（来自
    ``MapData.vector_role_source("buildings")``）。
    """

    unresolved = {}
    for tower_id in towers:
        unresolved[tower_id] = {
            "status": "unresolved", "building_height_m": None,
            "valid_height_fraction": None, "source": None,
            "reason": "building_source_not_configured",
        }
    source = building_source if isinstance(building_source, dict) else {}
    if not source.get("ok") or not source.get("path"):
        return unresolved
    try:
        from .building_footprint_aggregation import (
            discover_vector_layer, geometry_available, read_footprints,
        )
    except Exception as exc:  # pragma: no cover
        return {
            tower_id: dict(fact, reason=f"building_adapter_unavailable:{exc}")
            for tower_id, fact in unresolved.items()
        }
    available, reason = geometry_available()
    if not available:
        return {
            tower_id: dict(fact, reason=str(reason)) for tower_id, fact in unresolved.items()
        }
    try:
        from shapely.geometry import Point
    except Exception as exc:  # pragma: no cover
        return {
            tower_id: dict(fact, reason=f"geometry_libraries_unavailable:{exc}")
            for tower_id, fact in unresolved.items()
        }
    path = str(source["path"])
    layer_name = source.get("layer_name")
    try:
        info = dict(discover_vector_layer(path, layer_name))
    except Exception as exc:
        return {
            tower_id: dict(fact, reason=f"building_source_unreadable:{exc}")
            for tower_id, fact in unresolved.items()
        }
    source_epsg = _source_epsg(info)
    if source_epsg is None:
        return {
            tower_id: dict(fact, reason="building_source_crs_unknown")
            for tower_id, fact in unresolved.items()
        }
    if source_epsg == 4326:
        transformer = None
    else:
        try:
            import pyproj
            transformer = pyproj.Transformer.from_crs(4326, source_epsg, always_xy=True)
        except Exception as exc:  # pragma: no cover
            return {
                tower_id: dict(fact, reason=f"building_source_crs_transform_unavailable:{exc}")
                for tower_id, fact in unresolved.items()
            }

    facts = {}
    for tower_id, tower in towers.items():
        longitude = _number(tower.get("longitude"))
        latitude = _number(tower.get("latitude"))
        if longitude is None or latitude is None:
            facts[tower_id] = unresolved[tower_id]
            continue
        bbox = _bbox_around(longitude, latitude, TOWER_BUILDING_SEARCH_HALF_DEG)
        try:
            rows, _ = read_footprints(path, bbox, layer_name=layer_name, info=info)
        except Exception as exc:
            facts[tower_id] = dict(unresolved[tower_id], reason=f"building_query_failed:{exc}")
            continue
        if transformer is not None:
            x, y = transformer.transform(longitude, latitude)
        else:
            x, y = longitude, latitude
        point = Point(x, y)
        heights, seen = [], 0
        for geometry, height, _identifier in rows:
            if geometry is None or geometry.is_empty:
                continue
            try:
                if not geometry.intersects(point):
                    continue
            except Exception:
                continue
            seen += 1
            number = _number(height)
            heights.append(number)
        if not seen:
            facts[tower_id] = {
                "status": "unresolved", "building_height_m": None,
                "valid_height_fraction": None,
                "source": "real_building_footprint_at_tower_coordinate",
                "reason": "tower_coordinate_matches_no_building_footprint",
            }
            continue
        if any(height is None for height in heights):
            # 命中建筑但高度字段缺失：这是"未知"，绝不当作 0。
            facts[tower_id] = {
                "status": "unresolved", "building_height_m": None,
                "valid_height_fraction": 0.0,
                "source": "real_building_footprint_at_tower_coordinate",
                "reason": "building_height_attribute_missing",
            }
            continue
        facts[tower_id] = {
            "status": "passed",
            # 一个坐标命中多个足迹时取最大高度：更保守（塔顶更高 ⇒ 净空判定更严格）。
            "building_height_m": max(heights),
            "valid_height_fraction": 1.0,
            "source": "real_building_footprint_at_tower_coordinate",
            "matched_footprint_count": seen,
            "reason": None,
        }
    return facts


def build_tower_obstacle_facts(towers, *, terrain_source=None, building_source=None):
    """为全部铁塔产出 ``{"terrain": {...}, "buildings": {...}}`` 事实。

    ``towers`` 是 ``{tower_id: tower_record}``；每一项都独立解析，一个塔失败不影响其它塔。
    """

    items = {
        str(tower_id): tower for tower_id, tower in (towers or {}).items()
        if isinstance(tower, dict) and str(tower_id)
    }
    if not items:
        return {"terrain": {}, "buildings": {}, "method": TOWER_OBSTACLE_FACT_METHOD}
    return {
        "terrain": _terrain_facts(items, terrain_source),
        "buildings": _building_facts(items, building_source),
        "method": TOWER_OBSTACLE_FACT_METHOD,
        "terrain_sample_half_deg": TOWER_TERRAIN_SAMPLE_HALF_DEG,
        "building_search_half_deg": TOWER_BUILDING_SEARCH_HALF_DEG,
        "building_source": (
            str(building_source.get("path")) if isinstance(building_source, dict)
            and building_source.get("path") else None
        ),
    }


def building_source_hint(paths, role="buildings"):
    """只读提示：建筑源是否配置（不打开数据集）。"""

    value = (paths or {}).get(role)
    if not value:
        return {"ok": False, "reason": "building_source_not_configured", "path": None}
    return {"ok": True, "path": str(Path(str(value))), "reason": None}


__all__ = [
    "TOWER_BUILDING_SEARCH_HALF_DEG", "TOWER_OBSTACLE_FACT_METHOD",
    "TOWER_TERRAIN_SAMPLE_HALF_DEG", "build_tower_obstacle_facts", "building_source_hint",
]
