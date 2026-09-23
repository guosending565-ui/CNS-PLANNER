"""Radar Surveillance Layout V1.1 — GIS 边界（只读事实，不判定业务结论）。

职责严格限定为**读取事实**：

1. **地形正高**：复用既有已确认的 FABDEM DTM 窗口采样器
   （:class:`cns_planner.gis.fine_environment_adapter.FabdemWindowTerrainSource`），
   只有 ``vertical_reference == "egm2008_orthometric"`` 才被接受。
   **V1.1：它只用于既有 tower_obstacle_profiles 派生塔底/塔顶，以及未来 terrain LOS；
   不再作为航路采样点高度**（见 ``domain.RADAR_LAYOUT`` 的 ALT-080 固定高度语义）。
2. **陆域 polygon**：从**显式配置**的 ``land_mask`` 矢量源读取 Polygon/MultiPolygon，
   用于逐点 ``land | coastal_uncertain | sea | unknown`` 判定；
3. **雷达原点高度**：``radar_origin_egm2008_m = tower_top_orthometric_m``
   （V1.1 固定简化语义：雷达相位中心位于塔顶；**不再**叠加任何挂高）。

陆域判定规则（V1.1，保守且 CRS 安全）
-------------------------------------

真实来源：``zhejiang_boundary.gpkg`` / ``layer_name = zhejiang_boundary`` /
``source_crs = EPSG:4326`` / ``Polygon|MultiPolygon``。

* 点在陆域 polygon 内部 **或落在边界上** ⇒ ``land``；
* 点在 polygon 外、但到陆域边界距离 ``<= coastal_uncertainty_buffer_m`` ⇒
  ``coastal_uncertain``（``effective_requirement_class = land``，要求 2 个站址）；
* 点在 polygon 外且距离 ``> buffer`` ⇒ ``sea``；
* 陆域源未配置 / 不可读 / 几何库缺失 / **CRS 不可解析或转换失败** ⇒ ``unknown``
  （**fail-closed**）。

**绝不用 DEM NoData 推断海洋**：本模块没有任何 DEM-derived 海洋/陆地结论。

**绝不用 degree-as-meter**：所有距离分类都在显式**米制** CRS 中完成。
源 CRS 从数据集真实读取（GPKG 用 GeoPackage 元数据，OGR 数据源用 GDAL），
未知或转换失败一律 ``unknown``。
"""

from __future__ import annotations

import json
import math
from copy import deepcopy
from pathlib import Path

from ..domain.radar_surveillance_layout import (
    COASTAL_UNCERTAINTY_BUFFER_CONFIRMED, COASTAL_UNCERTAINTY_BUFFER_ORIGIN,
    COASTAL_UNCERTAINTY_BUFFER_SEMANTICS, DEFAULT_COASTAL_UNCERTAINTY_BUFFER_M,
    EFFECTIVE_REQUIREMENT_CLASS, INSTALLATION_ASSUMPTION,
    INSTALLATION_ENGINEERING_CONFIRMED, RADAR_ORIGIN_BASIS, RADAR_ORIGIN_SEMANTICS,
    REQUIRED_DISTINCT_SITE_COUNT,
)

#: 陆地/海洋判定来源标识（V1.1：显式 polygon 包含 + 海岸不确定带）。
LAND_MASK_SEMANTICS = (
    "explicit_land_polygon_containment_plus_coastal_uncertainty_buffer"
)

#: 分类判定基（provenance 用）。
LAND_MASK_CLASSIFICATION_BASIS = "explicit_polygon"

#: 允许的矢量格式（与既有 :mod:`cns_planner.gis.path_resolver` 一致的最小集合）。
LAND_MASK_SUFFIXES = (".gpkg", ".shp", ".geojson", ".json")

#: 海岸不确定带使用的**米制** CRS（必须与航路/塔的米制框架一致）。
COASTAL_BUFFER_METRIC_CRS = "EPSG:32651"

#: 常见陆域/海岸线图层名关键词（仅用于在 gpkg 中**兜底**挑层；显式 layer_name 优先）。
LAND_LAYER_KEYWORDS = (
    "land", "landmass", "coastline", "coast", "shoreline", "island",
    "boundary", "border", "province", "region", "admin",
    "陆", "陆域", "陆地", "海岸", "岸线", "岛屿", "省界", "行政", "边界",
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


def normalized_coastal_buffer_m(value):
    """规范化海岸不确定带（米）。

    ``None`` / 空 ⇒ 工程默认 30 m；非负有限数直接接受（显式 0 合法，表示"不带不确定带"）。
    负值/非数值 ⇒ ``ValueError``（绝不静默改成 0，也绝不用一个假值继续算）。
    """

    if value in (None, ""):
        return float(DEFAULT_COASTAL_UNCERTAINTY_BUFFER_M)
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("coastal_uncertainty_buffer_m 必须是数值") from exc
    if number != number or abs(number) == float("inf"):
        raise ValueError("coastal_uncertainty_buffer_m 必须是有限数值")
    if number < 0:
        raise ValueError("coastal_uncertainty_buffer_m 不能为负")
    return number


def coastal_uncertainty_buffer_provenance(value=None):
    """海岸不确定带的 provenance（**不得**被描述成数据真实精度）。"""

    return {
        "coastal_uncertainty_buffer_m": normalized_coastal_buffer_m(value),
        "parameter_origin": COASTAL_UNCERTAINTY_BUFFER_ORIGIN,
        "confirmed": COASTAL_UNCERTAINTY_BUFFER_CONFIRMED,
        "semantics": COASTAL_UNCERTAINTY_BUFFER_SEMANTICS,
        "detail": (
            "简化工程边界的海岸带不能代表确定的海洋；该缓冲是**本轮工程保守假设**，"
            "不是边界数据真实精度，也不等同于 5 m validation resolution。"
        ),
        "metric_crs": COASTAL_BUFFER_METRIC_CRS,
        "degree_as_meter_used": False,
    }


# ------------------------------------------------------------------ land mask readiness


def land_mask_hint(path, *, layer_name=None):
    """只读提示：陆域源是否配置（不打开数据集）。"""

    if not path:
        return {
            "ok": False,
            "path": None,
            "layer_name": layer_name,
            "reason": "land_mask_not_configured",
            "detail": (
                "项目当前没有任何明确可用的陆域 Polygon 来源；"
                "surface_class 保持 unknown（fail-closed），绝不自动按 sea 处理，"
                "也绝不用 DEM NoData 推断海洋。"
            ),
            "semantics": LAND_MASK_SEMANTICS,
            "classification_basis": LAND_MASK_CLASSIFICATION_BASIS,
        }
    resolved = Path(str(path))
    if not resolved.is_file():
        return {
            "ok": False,
            "path": str(resolved),
            "layer_name": layer_name,
            "reason": "land_mask_source_missing",
            "detail": "配置的陆域源路径不存在",
            "semantics": LAND_MASK_SEMANTICS,
            "classification_basis": LAND_MASK_CLASSIFICATION_BASIS,
        }
    if resolved.suffix.lower() not in LAND_MASK_SUFFIXES:
        return {
            "ok": False,
            "path": str(resolved),
            "layer_name": layer_name,
            "reason": "land_mask_format_unsupported",
            "detail": f"仅支持 {'/'.join(LAND_MASK_SUFFIXES)}",
            "semantics": LAND_MASK_SEMANTICS,
            "classification_basis": LAND_MASK_CLASSIFICATION_BASIS,
        }
    available, reason = geometry_available()
    if not available:
        return {
            "ok": False, "path": str(resolved), "layer_name": layer_name, "reason": reason,
            "detail": "缺少几何库，无法做点在多边形内判定",
            "semantics": LAND_MASK_SEMANTICS,
            "classification_basis": LAND_MASK_CLASSIFICATION_BASIS,
        }
    return {
        "ok": True, "path": str(resolved), "layer_name": layer_name, "reason": None,
        "detail": None, "semantics": LAND_MASK_SEMANTICS,
        "classification_basis": LAND_MASK_CLASSIFICATION_BASIS,
    }


# ------------------------------------------------------------------ CRS resolution


def gpkg_layer_crs(path, table):
    """从 GeoPackage 元数据读取某要素图层的真实 CRS（``srs_id`` → ``EPSG:xxxx``）。

    读不到时返回 ``None`` —— 调用方必须 fail-closed（``unknown``），绝不假定 4326。
    """

    import sqlite3

    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except Exception:  # pragma: no cover - 不可读
        return None
    try:
        rows = connection.execute(
            "SELECT srs_id FROM gpkg_geometry_columns WHERE table_name=?", (str(table),)
        ).fetchall()
    except Exception:
        return None
    finally:
        connection.close()
    if not rows or rows[0][0] in (None, ""):
        return None
    try:
        srs_id = int(rows[0][0])
    except (TypeError, ValueError):
        return None
    # GeoPackage 的 srs_id 对 EPSG 轴序编码就是 EPSG code（0/undefined 除外）。
    if srs_id <= 0:
        return None
    return f"EPSG:{srs_id}"


def ogr_layer_crs(path):
    """非 GeoPackage 矢量源：用 GDAL/OGR 读取真实 CRS authority。"""

    try:  # pragma: no cover - 需要 GDAL 环境
        from osgeo import osr

        try:
            import pyogrio
            info = pyogrio.read_info(str(path))
            crs = info.get("crs")
            if crs:
                spatial = osr.SpatialReference()
                if spatial.SetFromUserInput(str(crs)) == 0:
                    authority = spatial.GetAuthorityName(None)
                    code = spatial.GetAuthorityCode(None)
                    if authority and code:
                        return f"{authority}:{code}"
                    return str(crs)
        except Exception:
            pass
    except Exception:
        return None
    return None


# ------------------------------------------------------------------ land polygons
#
# 这里**不复用**建筑足迹读取路径：``read_footprints`` 需要 bbox、优先挑 Polygon 图层并
# 且对缺失 RTree 的 gpkg 直接拒绝，这三条假设都只对建筑成立。陆域掩膜需要"整层任意
# 点包含判定"，因此本模块自行做只读全层读取，并把每个多边形的**完整几何**（含洞）
# 与 bbox 缓存下来，用 bbox 预筛避免逐点全量几何运算。
#
# 注意：V1.1 起缓存的是 shapely 几何对象（而不是只取外环的坐标列表），
# 因为海岸不确定带需要真实的**边界距离**（``boundary.distance``），
# 而"只取外环、不挖洞"会给出错误的边界距离。


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


def _polygon_parts(geometry):
    """展开成单个 ``Polygon`` 列表（**保留洞**，不做任何"只取外环"简化）。"""

    if geometry is None or getattr(geometry, "is_empty", True):
        return []
    geometry_type = getattr(geometry, "geom_type", "")
    if geometry_type == "Polygon":
        return [geometry]
    if geometry_type == "MultiPolygon":
        return [part for part in geometry.geoms if getattr(part, "geom_type", "") == "Polygon"]
    if geometry_type == "GeometryCollection":
        parts = []
        for part in geometry.geoms:
            parts.extend(_polygon_parts(part))
        return parts
    return []


def _layer_looks_like_land(name):
    text = str(name or "").strip().lower()
    if not text:
        return False
    if any(marker in text for marker in LAND_LAYER_EXCLUSIONS):
        return False
    return any(marker in text for marker in LAND_LAYER_KEYWORDS)


def _crs_is_geographic(source_crs):
    """源 CRS 是否地理（度）坐标系；无法解析时返回 ``False``（保守）。"""

    try:
        from pyproj import CRS

        return bool(CRS.from_user_input(str(source_crs)).is_geographic)
    except Exception:
        return False


class _IdentityMetricTransform:
    """恒等变换：源几何本身已经处于目标米制 CRS 中。"""

    @staticmethod
    def transform(first, second):
        return float(first), float(second)


def _resolve_transformers(source_crs, metric_crs):
    """构造并**自校验**一对互逆变换：``(source -> metric, metric -> source)``。

    pyproj 在 axis order / pipeline 方向上的行为会随 CRS 定义与版本变化，直接把
    ``from_crs(source, target)`` 当成"source→target"使用是不可靠的。这里显式构造两个
    方向的变换，并用一个已知点做**往返自校验**：只有真正互逆的那一对才会被接受。

    返回 ``(to_metric, to_source, reason)``；失败时前两项为 ``None``。
    """

    from pyproj import CRS, Transformer

    try:
        source = CRS.from_user_input(str(source_crs))
        target = CRS.from_user_input(str(metric_crs))
    except Exception as exc:
        return None, None, f"land_mask_crs_unresolvable:{exc}"
    if not target.is_projected or not target.axis_info or not target.axis_info[0].unit_name:
        return None, None, "metric_crs_not_projected"
    unit = str(target.axis_info[0].unit_name).lower()
    if unit not in ("metre", "meter", "m"):
        return None, None, f"metric_crs_unit_not_metre:{unit}"

    if source.equals(target):
        # 源几何已经在米制 CRS 中：几何侧**恒等**（不得再做任何投影）；
        # 查询点由调用方用独立的"经纬度 → metric"变换处理。
        identity = _IdentityMetricTransform()
        return identity, identity, None
    try:
        forward = Transformer.from_crs(source, target, always_xy=True)
        backward = Transformer.from_crs(target, source, always_xy=True)
    except Exception as exc:
        return None, None, f"land_mask_crs_transform_unavailable:{exc}"

    # 自校验：只有在 metric 侧产生**米级**坐标、且往返误差极小的一对才被接受。
    probe_lon, probe_lat = 122.2, 29.95
    try:
        metric_x, metric_y = forward.transform(probe_lon, probe_lat)
    except Exception as exc:
        return None, None, f"land_mask_crs_forward_probe_failed:{exc}"
    if not (math.isfinite(metric_x) and math.isfinite(metric_y)):
        return None, None, "land_mask_crs_forward_probe_non_finite"
    # metric 侧坐标必须是"米级"（舟山 UTM 东坐标 ~4e5、北坐标 ~3.3e6）。
    # 若 forward 实际是反向 pipeline，这里会得到 0~360 度的量级，直接拒绝。
    if abs(metric_x) < 1000.0 or abs(metric_y) < 1000.0:
        return None, None, "land_mask_crs_direction_ambiguous_not_metric_magnitude"
    try:
        back_lon, back_lat = backward.transform(metric_x, metric_y)
    except Exception as exc:
        return None, None, f"land_mask_crs_backward_probe_failed:{exc}"
    if abs(back_lon - probe_lon) > 1e-6 or abs(back_lat - probe_lat) > 1e-6:
        return None, None, "land_mask_crs_round_trip_mismatch"
    return forward, backward, None


def _explode_to_metric(polygons, source_crs, metric_crs):
    """把源 CRS 下的陆域多边形**显式转换**到米制 CRS。

    返回 ``(metric_polygons, to_metric, to_source, status, reason)``：

    * ``status == "passed"``：``metric_polygons`` 可用于米制距离/包含判定，
      且 ``to_metric`` / ``to_source`` 是同一对**已自校验**的互逆变换；
    * 否则全部为 ``None``，调用方必须 fail-closed。

    源 CRS 未知 / 转换失败 / 结果仍非米制 ⇒ 一律失败。**禁止 degree-as-meter**。
    """

    if not polygons:
        return None, None, None, "no_usable_polygon", "land_mask_no_usable_polygon_layer"
    if not source_crs:
        return None, None, None, "unknown_crs", "land_mask_source_crs_unknown"
    try:
        from shapely.ops import transform as shapely_transform
    except Exception as exc:  # pragma: no cover - 缺 pyproj
        return None, None, None, "crs_library_unavailable", f"crs_library_unavailable:{exc}"

    forward, backward, reason = _resolve_transformers(source_crs, metric_crs)
    if forward is None:
        return None, None, None, "crs_transform_unavailable", reason

    metric_polygons = []
    for polygon in polygons:
        try:
            converted = shapely_transform(forward.transform, polygon)
        except Exception as exc:
            return None, None, None, "crs_transform_failed", f"land_mask_crs_transform_failed:{exc}"
        if converted is None or getattr(converted, "is_empty", True):
            continue
        metric_polygons.extend(_polygon_parts(converted))
    if not metric_polygons:
        return (
            None, None, None, "crs_transform_empty",
            "land_mask_crs_transform_produced_empty_geometry",
        )
    # 几何自校验：米制几何的坐标量级必须与"米"一致。
    xs = [
        coordinate[0]
        for polygon in metric_polygons for coordinate in polygon.exterior.coords
    ]
    ys = [
        coordinate[1]
        for polygon in metric_polygons for coordinate in polygon.exterior.coords
    ]
    if not xs or max(abs(value) for value in xs) < 1000.0 or max(
        abs(value) for value in ys
    ) < 1000.0:
        return (
            None, None, None, "crs_geometry_not_metric_magnitude",
            "land_mask_metric_geometry_magnitude_not_metres",
        )
    return metric_polygons, forward, backward, "passed", None


def _point_to_metric(longitude, latitude, transformer):
    """``(lon, lat)`` → 米制平面坐标。

    ``transformer`` 的方向必须是 **源 CRS（经纬度）→ metric_crs（米）**，即
    ``LandMaskSource._transformer`` 本身。绝不把经纬度直接当米使用（禁止 degree-as-meter）。
    """

    x, y = transformer.transform(float(longitude), float(latitude))
    return float(x), float(y)


class LandMaskSource:
    """只读陆域掩膜：逐点 ``land | coastal_uncertain | sea | unknown``。

    证据不可用（源未配置/不可读/CRS 不可解析/转换失败/几何库缺失）时一律返回
    ``unknown``，且 ``required_distinct_site_count`` 为 ``None``（fail-closed）。
    """

    def __init__(self, path, *, layer_name=None, coastal_uncertainty_buffer_m=None,
                 metric_crs=COASTAL_BUFFER_METRIC_CRS):
        self.path = Path(str(path)) if path else None
        self.layer_name = layer_name
        self.coastal_uncertainty_buffer_m = normalized_coastal_buffer_m(
            coastal_uncertainty_buffer_m
        )
        self.metric_crs = str(metric_crs or COASTAL_BUFFER_METRIC_CRS)
        self._polygons = None
        self._prepared = False
        self._reason = None
        self._source_crs = None
        self._layer_resolution = None
        self._crs_status = "not_prepared"
        self._crs_reason = None
        self._transformer = None
        #: 几何侧：米制几何 → 源 CRS（诊断用）；懒构造。
        self._inverse_transformer = None
        #: 查询侧：**经纬度 → metric_crs**。与几何 CRS 解耦：即使源数据集的 CRS 已是投影
        #: 坐标系，"查询点"的语义依然是地理经纬度（扫描/选点都来自经纬度输入）。
        self._query_forward = None
        self._query_backward = None

    def metric_polygons(self):
        """只读访问：源几何经**显式 CRS 变换**后的米制 polygon 列表 ``[(polygon, bounds)]``。

        给诊断 / 测试直接消费（避免重复实现 CRS 换算）；返回的是同一份缓存对象，
        调用方不得修改。未就绪时返回 ``None``。
        """

        return self.prepare()

    def to_metric(self, longitude, latitude):
        """查询点 ``(lon, lat)`` → 米制平面坐标（与陆域几何同一 CRS）。

        查询侧使用"经纬度 → metric_crs"的独立变换（``_query_forward``）；它与几何侧的源
        CRS 解耦，因此即使陆域数据集本身已是投影坐标系，传进来的经纬度依然被正确投影。
        未就绪或变换失败时返回 ``None``。
        """

        polygons = self.prepare()
        if polygons is None or self._query_forward is None:
            return None
        try:
            x, y = self._query_forward.transform(float(longitude), float(latitude))
        except Exception:
            return None
        if not (math.isfinite(x) and math.isfinite(y)):
            return None
        return float(x), float(y)

    def to_geographic(self, x, y):
        """米制平面坐标 → 经纬度（``_query_backward``，与 :meth:`to_metric` 严格互逆）。"""

        polygons = self.prepare()
        if polygons is None or self._query_backward is None:
            return None
        try:
            first, second = self._query_backward.transform(float(x), float(y))
        except Exception:
            return None
        if not (math.isfinite(first) and math.isfinite(second)):
            return None
        return float(first), float(second)

    # ---- preparation ---------------------------------------------------------------
    def _resolve_layer(self):
        """决定读哪一层：显式 ``layer_name`` 优先，其次既有保守关键词兜底。"""

        if self.layer_name:
            return str(self.layer_name), "explicit_layer_name"
        for table, _column, kind in _gpkg_land_layers(self.path):
            if "POLYGON" not in kind.upper():
                continue
            if _layer_looks_like_land(table):
                return table, "keyword_auto_selection"
        return None, "no_matching_land_layer"

    def _crs_looks_geographic(self):
        """源 CRS 是否地理（度）坐标系。"""

        if not self._source_crs:
            return None
        try:
            from pyproj import CRS

            return bool(CRS.from_user_input(str(self._source_crs)).is_geographic)
        except Exception:
            return None

    def prepare(self):
        if self._prepared:
            return self._polygons
        self._prepared = True
        hint = land_mask_hint(self.path, layer_name=self.layer_name)
        if not hint["ok"]:
            self._reason = hint["reason"]
            self._crs_status = "source_unavailable"
            self._crs_reason = hint["reason"]
            return None
        suffix = self.path.suffix.lower()
        if suffix == ".gpkg":
            table, resolution = self._resolve_layer()
            self._layer_resolution = resolution
            if table is None:
                self._reason = "land_mask_no_usable_polygon_layer"
                self._crs_status = "source_unavailable"
                self._crs_reason = self._reason
                return None
            column = None
            kind = ""
            for candidate_table, candidate_column, candidate_kind in _gpkg_land_layers(self.path):
                if candidate_table == table:
                    column, kind = candidate_column, candidate_kind
                    break
            if column is None or "POLYGON" not in kind.upper():
                self._reason = "land_mask_no_usable_polygon_layer"
                self._crs_status = "source_unavailable"
                self._crs_reason = self._reason
                return None
            self._source_crs = gpkg_layer_crs(self.path, table)
            geometries = _gpkg_land_geometries(self.path, table, column)
        elif suffix in (".geojson", ".json"):
            self._layer_resolution = "geojson_file"
            # GeoJSON 语义是 OGC:CRS84（lon/lat 度）。
            try:
                from pyproj import CRS
                self._source_crs = CRS.from_user_input("OGC:CRS84").to_string()
            except Exception:  # pragma: no cover
                self._source_crs = "OGC:CRS84"
            geometries = _geojson_land_geometries(self.path)
        else:
            self._layer_resolution = "ogr_dataset"
            self._source_crs = ogr_layer_crs(self.path)
            geometries = _ogr_land_geometries(self.path)

        polygons = []
        for geometry in geometries:
            polygons.extend(_polygon_parts(geometry))
        polygons = [polygon for polygon in polygons if polygon.area > 0]
        if not polygons:
            self._reason = "land_mask_no_usable_polygon_layer"
            self._crs_status = "source_unavailable"
            self._crs_reason = self._reason
            return None

        # CRS 合理性守卫（**禁止 degree-as-meter** 的第二道闸）：若源 CRS 是**投影**坐标系，
        # 但几何坐标仍在度级（|x| 很小、|y| 很小），说明源 CRS 声明与几何不符。
        # 此时绝不继续（那会把度当米），显式 fail-closed 并记录原因。
        if self._crs_looks_geographic() is False:
            xs = [coordinate[0] for polygon in polygons for coordinate in polygon.exterior.coords]
            ys = [coordinate[1] for polygon in polygons for coordinate in polygon.exterior.coords]
            if xs and max(abs(value) for value in xs) < 1000 and max(
                abs(value) for value in ys
            ) < 1000:
                self._reason = "land_mask_geometry_does_not_match_declared_projected_crs"
                self._crs_status = "crs_geometry_mismatch"
                self._crs_reason = self._reason
                return None

        metric_polygons, to_metric_xform, to_source_xform, status, reason = _explode_to_metric(
            polygons, self._source_crs, self.metric_crs
        )
        self._crs_status = status
        self._crs_reason = reason
        if metric_polygons is None:
            self._reason = reason
            return None
        # 几何侧变换（源几何 → 米制几何）：已过往返自校验。
        self._transformer = to_metric_xform
        self._inverse_transformer = to_source_xform
        # 查询侧变换（经纬度 → 米制平面）：与几何 CRS 解耦，始终是 OGC:CRS84 → metric。
        if self._source_crs and _crs_is_geographic(self._source_crs):
            # 源本身就是经纬度 ⇒ 几何变换即可直接用于查询点。
            self._query_forward = to_metric_xform
            self._query_backward = to_source_xform
        else:
            query_forward, query_backward, query_reason = _resolve_transformers(
                "OGC:CRS84", self.metric_crs
            )
            if query_forward is None:
                self._crs_status = "query_crs_transform_unavailable"
                self._crs_reason = f"land_mask_query_crs_unavailable:{query_reason}"
                self._reason = self._crs_reason
                return None
            self._query_forward = query_forward
            self._query_backward = query_backward

        prepared = []
        for polygon in metric_polygons:
            bounds = polygon.bounds
            if len(bounds) != 4:
                continue
            prepared.append((polygon, tuple(float(value) for value in bounds)))
        if not prepared:
            self._reason = "land_mask_no_usable_polygon_layer"
            self._crs_status = "source_unavailable"
            self._crs_reason = self._reason
            return None
        self._polygons = prepared
        return self._polygons

    def describe(self):
        return {
            "configured_path": str(self.path) if self.path else None,
            "layer_name": self.layer_name,
            "layer_resolution": self._layer_resolution,
            "readiness": land_mask_hint(self.path, layer_name=self.layer_name),
            "polygon_count": len(self._polygons) if self._polygons else None,
            "reason": self._reason,
            "semantics": LAND_MASK_SEMANTICS,
            "classification_basis": LAND_MASK_CLASSIFICATION_BASIS,
            "source_type": "real",
            "source_role": "land_mask",
            "source_crs": self._source_crs,
            "metric_crs": self.metric_crs,
            "crs_status": self._crs_status,
            "crs_reason": self._crs_reason,
            "coastal_uncertainty_buffer": coastal_uncertainty_buffer_provenance(
                self.coastal_uncertainty_buffer_m
            ),
            "dem_nodata_used_to_infer_sea": False,
            "degree_as_meter_used": False,
        }

    # ---- queries -------------------------------------------------------------------
    #: 判定"落在边界上"的米制容差：投影变换的浮点误差不应把边界点挤出 land 语义。
    #: 这是**数值容差**，不是任何业务缓冲。
    BOUNDARY_TOLERANCE_M = 1e-6

    def _classify_metric_point(self, x, y, polygons):
        """米制点分类：``(surface_class, evidence)``。

        语义（V1.1）：

        * 点在 polygon 内**或落在边界上** ⇒ ``land``；
        * 点在 polygon 外、但 ``0 < distance <= buffer`` ⇒ ``coastal_uncertain``
          （按 land 处理，要求 2 个站址）；
        * 点在 polygon 外且 ``distance > buffer`` ⇒ ``sea``。
        """

        from shapely.geometry import Point

        point = Point(x, y)
        buffer_m = float(self.coastal_uncertainty_buffer_m)
        nearest = None
        on_boundary = False
        inside = False
        for polygon, bounds in polygons:
            minx, miny, maxx, maxy = bounds
            # 预筛必须把 buffer 与边界容差扩进去，否则"外侧但在 buffer 内"的点会被跳过。
            margin = buffer_m + self.BOUNDARY_TOLERANCE_M
            if (x < minx - margin or x > maxx + margin
                    or y < miny - margin or y > maxy + margin):
                continue
            try:
                boundary_distance = float(polygon.boundary.distance(point))
            except Exception:
                continue
            if boundary_distance <= self.BOUNDARY_TOLERANCE_M:
                # 落在边界上（保守按 land 处理；不依赖 covers 的浮点边界行为）。
                on_boundary = True
            elif polygon.covers(point):
                inside = True
            if nearest is None or boundary_distance < nearest:
                nearest = boundary_distance
        if on_boundary or inside:
            return "land", {
                "reason": (
                    "point_inside_or_on_land_polygon_boundary" if on_boundary
                    else "point_inside_land_polygon"
                ),
                "distance_to_land_boundary_m": (
                    0.0 if on_boundary else nearest
                ),
                "boundary_counts_as_land": True,
            }
        if nearest is not None and nearest <= buffer_m:
            return "coastal_uncertain", {
                "reason": "point_outside_land_polygon_within_coastal_uncertainty_buffer",
                "distance_to_land_boundary_m": nearest,
                "coastal_uncertainty_buffer_m": buffer_m,
            }
        if nearest is None:
            return "sea", {
                "reason": "point_outside_all_configured_land_polygons",
                "distance_to_land_boundary_m": None,
            }
        return "sea", {
            "reason": "point_outside_coastal_uncertainty_buffer",
            "distance_to_land_boundary_m": nearest,
            "coastal_uncertainty_buffer_m": buffer_m,
        }

    def _unavailable(self):
        polygons = self.prepare()
        if polygons is None:
            return None, {
                "reason": self._reason or "land_mask_unavailable",
                "semantics": LAND_MASK_SEMANTICS,
                "crs_status": self._crs_status,
                "crs_reason": self._crs_reason,
            }
        return polygons, None

    def classify(self, longitude, latitude):
        """单点判定。返回 ``(surface_class, evidence)``。"""

        available, reason = geometry_available()
        if not available:
            return "unknown", {"reason": reason}
        polygons, failure = self._unavailable()
        if polygons is None:
            return "unknown", failure
        if longitude is None or latitude is None:
            return "unknown", {"reason": "query_point_missing_coordinate"}
        x, y = self.to_metric(longitude, latitude)
        if x is None or y is None:
            return "unknown", {"reason": "query_point_transform_unavailable"}
        surface, evidence = self._classify_metric_point(x, y, polygons)
        evidence["semantics"] = LAND_MASK_SEMANTICS
        return surface, evidence

    def classify_many(self, points):
        """批量判定：``points`` = ``[(lon, lat), ...]`` -> ``[surface_class, ...]``。"""

        if not points:
            return []
        polygons, _failure = self._unavailable()
        if polygons is None:
            return ["unknown" for _ in points]
        prepared = [(polygon, bounds) for polygon, bounds in polygons]
        result = []
        for longitude, latitude in points:
            if longitude is None or latitude is None:
                result.append("unknown")
                continue
            metric = self.to_metric(longitude, latitude)
            if metric is None:
                result.append("unknown")
                continue
            result.append(self._classify_metric_point(metric[0], metric[1], prepared)[0])
        return result

    def classify_detailed(self, longitude, latitude):
        """单点判定 + V1.1 需求语义（供逐个 sample 独立分类使用）。

        返回 ``{surface_class, effective_requirement_class, required_distinct_site_count,
        classification_confidence, evidence}``。
        """

        surface, evidence = self.classify(longitude, latitude)
        effective = EFFECTIVE_REQUIREMENT_CLASS.get(surface)
        return {
            "surface_class": surface,
            "effective_requirement_class": effective,
            "required_distinct_site_count": (
                REQUIRED_DISTINCT_SITE_COUNT.get(surface) if effective is not None else None
            ),
            "classification_confidence": (
                "uncertain" if surface == "coastal_uncertain"
                else "unknown" if surface == "unknown"
                else "confirmed"
            ),
            "evidence": evidence,
        }


# ------------------------------------------------------------------ terrain sampling


def route_terrain_source(path):
    """构造逐点地形采样器；不可用时返回 ``None``（调用方 fail-closed）。

    V1.1：该采样器**不再**用于航路采样高度（航路高度恒为 80 m），
    只保留给既有 ``tower_obstacle_profiles`` 派生与未来 terrain LOS。
    """

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
    """对航路点批量采样 FABDEM DTM 正高（**V1.1 不再作为航路高度**）。

    本函数保留为既有能力（塔底/塔顶派生与未来 terrain LOS 的证据通道），
    V1.1 的航路 sample ``egm2008_m`` 恒为 80 m，不消费这里的返回值。

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


def resolve_radar_origins(*, towers, obstacle_profiles, mount_assumption=None):
    """逐塔解析**雷达原点** EGM2008 正高（V1.1 固定简化语义）。

        radar_origin_egm2008_m = tower_top_orthometric_m

    其中 ``tower_top_orthometric_m`` 已经代表真实塔顶 EGM2008 正高：

    * ground tower：FABDEM 地形 + 塔身结构高度；
    * rooftop tower：FABDEM 地形 + 楼面建筑高度 + 塔身结构高度。

    V1.1 明确**删除**"塔顶以上再叠一层统一工程示例挂高"这一步
    （BUG-RADAR-ORIGIN-003）。传入的 ``mount_assumption`` 只被**兼容读取**并原样记录为
    ``legacy_not_used_by_v1_1``，**不影响**任何 V1.1 几何。

    ``tower_top_orthometric_m`` 未解析的塔继续**不可作为候选**，**绝不填 0**。
    """

    mount = mount_assumption if isinstance(mount_assumption, dict) else {}
    legacy_mount_height = mount.get("radar_mount_height_m")
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
            "origin_basis": RADAR_ORIGIN_BASIS,
            "origin_confirmed": False,
            "origin_parameter_origin": "source_tower_top_orthometric",
            "origin_reason": None,
            "installation_assumption": INSTALLATION_ASSUMPTION,
            "installation_engineering_confirmed": INSTALLATION_ENGINEERING_CONFIRMED,
            # legacy 字段：兼容读取、显式标记、**不影响** V1.1 几何。
            "legacy_radar_mount_height_m": (
                float(legacy_mount_height)
                if isinstance(legacy_mount_height, (int, float))
                and not isinstance(legacy_mount_height, bool)
                else None
            ),
            "legacy_mount_height_status": "legacy_not_used_by_v1_1",
            "legacy_mount_height_affects_v1_1_geometry": False,
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
        record["origin_egm2008_m"] = float(top)
        record["origin_source"] = "tower_top_orthometric_m"
        records.append(record)

    return {
        "status": "passed" if any(
            item["origin_egm2008_m"] is not None for item in records
        ) else "missing_data",
        # 兼容读取的 legacy 挂高仍原样回显（便于审计），但语义上**不参与** V1.1 几何。
        "legacy_mount_assumption": deepcopy(mount),
        "legacy_mount_assumption_status": "legacy_not_used_by_v1_1",
        "legacy_mount_height_used": False,
        "semantics": RADAR_ORIGIN_SEMANTICS,
        "origin_basis": RADAR_ORIGIN_BASIS,
        "installation_assumption": INSTALLATION_ASSUMPTION,
        "engineering_confirmed": INSTALLATION_ENGINEERING_CONFIRMED,
        "records": records,
        "by_tower": {str(item["tower_id"]): item for item in records},
        "unresolved": unresolved,
        "unresolved_count": len(unresolved),
        "backend_hardcoded_mount_height": False,
    }


# ------------------------------------------------------------------ readiness bundle


def radar_layout_source_status(state, paths, *, layer_name=None,
                               coastal_uncertainty_buffer_m=None):
    """只读 readiness：地形 / 陆域 / 挂高是否就绪（不打开大数据集）。"""

    state = state if isinstance(state, dict) else {}
    paths = paths if isinstance(paths, dict) else {}
    terrain_path = paths.get("terrain_dtm")
    terrain = {
        "configured_path": str(terrain_path) if terrain_path else None,
        "available": bool(terrain_path) and Path(str(terrain_path)).is_file(),
        "role": "terrain_dtm",
        "vertical_reference_requirement": "egm2008_orthometric",
        "used_for_route_sample_height": False,
        "used_for": "tower_obstacle_profiles_and_future_terrain_los",
        "reason": None if terrain_path else "terrain_dtm_not_configured",
    }
    if terrain_path and not terrain["available"]:
        terrain["reason"] = "terrain_dtm_source_missing"
    land = land_mask_hint(paths.get("land_mask"), layer_name=layer_name)
    land["coastal_uncertainty_buffer"] = coastal_uncertainty_buffer_provenance(
        coastal_uncertainty_buffer_m
    )
    land["source_type"] = "real" if land.get("ok") else "not_configured"
    land["source_role"] = "land_mask"
    land["dem_nodata_used_to_infer_sea"] = False
    return {
        "terrain": terrain,
        "land_mask": land,
        "radar_origin": radar_origin_assumption_status(state),
        "radar_mount_height": radar_mount_assumption_status(state),
        "semantics": "read_only_source_status_no_dataset_opened",
    }


def radar_origin_assumption_status(state):
    """雷达原点假设（V1.1：塔顶即相位中心，**不再要求挂高**）。"""

    return {
        "radar_origin_basis": RADAR_ORIGIN_BASIS,
        "installation_assumption": INSTALLATION_ASSUMPTION,
        "engineering_confirmed": INSTALLATION_ENGINEERING_CONFIRMED,
        "radar_mount_height_required": False,
        "semantics": RADAR_ORIGIN_SEMANTICS,
        "detail": (
            "V1.1 固定简化语义：radar_origin_egm2008_m = tower_top_orthometric_m"
            "（ground tower = FABDEM + 塔身；rooftop tower = FABDEM + 建筑高度 + 塔身）。"
            "不再叠加任何统一工程示例挂高。"
        ),
    }


def radar_mount_assumption_status(state):
    """legacy 挂高状态（V1.1 只兼容读取，**不作为 readiness 门控**）。"""

    assumption = (state or {}).get("radar_surveillance_policy") or {}
    mount = (assumption.get("radar_mount_height") or {}) if isinstance(assumption, dict) else {}
    if not isinstance(mount, dict) or mount.get("radar_mount_height_m") is None:
        return {
            "status": "not_configured",
            "radar_mount_height_m": None,
            "confirmed": False,
            "parameter_origin": "not_configured",
            "source": None,
            "legacy_not_used_by_v1_1": True,
            "used_by_algorithm_version": None,
            "required_for_v1_1": False,
            "detail": (
                "V1.1 不再需要挂高：雷达原点等于 tower_top_orthometric_m。"
                "该字段只作历史兼容读取，绝不影响 V1.1 几何。"
            ),
        }
    return {
        "status": str(mount.get("status") or "pending_confirmation"),
        "radar_mount_height_m": mount.get("radar_mount_height_m"),
        "mount_height_basis": mount.get("mount_height_basis"),
        "confirmed": bool(mount.get("confirmed") is True),
        "parameter_origin": str(mount.get("parameter_origin") or "engineering_assumption"),
        "source": mount.get("source"),
        "legacy_not_used_by_v1_1": True,
        "used_by_algorithm_version": None,
        "required_for_v1_1": False,
        "detail": "legacy 字段：V1.1 只兼容读取，不参与几何。",
    }


def land_mask_readiness(state, paths, *, layer_name=None,
                        coastal_uncertainty_buffer_m=None):
    """V1.1 land mask **深度** readiness：文件 / 图层 / 几何 / CRS / 米制变换。

    只有全部通过才 ``status == "passed"``。任何一项失败都给出明确 reason，
    因此 readiness 不会先显示 passed、运行时才全 unknown。
    """

    configured = (paths or {}).get("land_mask")
    hint = land_mask_hint(configured, layer_name=layer_name)
    available, geometry_reason = geometry_available()
    checks = {
        "configured": bool(configured),
        "file_exists": bool(hint.get("ok")) or hint.get("reason") not in (
            "land_mask_not_configured", "land_mask_source_missing",
        ),
        "format_supported": hint.get("reason") != "land_mask_format_unsupported",
        "geometry_library_available": bool(available),
        "layer_name_explicit": bool(layer_name),
    }
    record = {
        "status": "not_ready",
        "configured_path": str(configured) if configured else None,
        "layer_name": layer_name,
        "reason": hint.get("reason") or geometry_reason,
        "checks": checks,
        "semantics": LAND_MASK_SEMANTICS,
        "classification_basis": LAND_MASK_CLASSIFICATION_BASIS,
        "dem_nodata_used_to_infer_sea": False,
        "degree_as_meter_used": False,
        "coastal_uncertainty_buffer": coastal_uncertainty_buffer_provenance(
            coastal_uncertainty_buffer_m
        ),
    }
    if not configured:
        return record
    if not hint.get("ok"):
        return record
    source = LandMaskSource(
        configured, layer_name=layer_name,
        coastal_uncertainty_buffer_m=coastal_uncertainty_buffer_m,
    )
    polygons = source.prepare()
    described = source.describe()
    record.update({
        "layer_resolution": described.get("layer_resolution"),
        "source_crs": described.get("source_crs"),
        "metric_crs": described.get("metric_crs"),
        "crs_status": described.get("crs_status"),
        "crs_reason": described.get("crs_reason"),
        "polygon_count": described.get("polygon_count"),
    })
    checks["polygon_geometry_present"] = bool(polygons)
    checks["source_crs_resolved"] = bool(described.get("source_crs"))
    checks["classification_transform_available"] = described.get("crs_status") == "passed"
    if polygons is None:
        record["reason"] = source._reason or "land_mask_no_usable_polygon_layer"
        return record
    if described.get("crs_status") != "passed":
        record["reason"] = described.get("crs_reason") or "land_mask_crs_unavailable"
        return record
    record["status"] = "passed"
    record["reason"] = None
    return record


__all__ = [
    "COASTAL_BUFFER_METRIC_CRS", "LAND_LAYER_EXCLUSIONS", "LAND_LAYER_KEYWORDS",
    "LAND_MASK_CLASSIFICATION_BASIS", "LAND_MASK_SEMANTICS", "LAND_MASK_SUFFIXES",
    "ROUTE_TERRAIN_SAMPLE_HALF_DEG", "LandMaskSource",
    "coastal_uncertainty_buffer_provenance", "geometry_available", "gpkg_layer_crs",
    "land_mask_hint", "land_mask_readiness", "normalized_coastal_buffer_m",
    "ogr_layer_crs", "radar_layout_source_status", "radar_mount_assumption_status",
    "radar_origin_assumption_status", "resolve_radar_origins", "route_terrain_source",
    "sample_route_terrain",
]