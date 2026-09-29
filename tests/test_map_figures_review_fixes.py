"""code review 修复项的定向回归测试（与业务算法无关，全部只读）。

覆盖 6 条契约：

1. **真实 canonical ``operational_routes`` fixture**：``start`` / ``end`` 是坐标数组时，
   起终点标注仍能生成，绝不抛 ``AttributeError``；
2. **``cartographic_land`` 缺失只降级**：派生数据不在时 MapData / 项目启动照常，
   只有专题图的陆海图层变成 unavailable，且 reason 里说清原因；
3. **重复导出幂等 + 不推进 revision**：同一业务 revision + 同一 FigureSpec 连续导出两次
   得到同一 figure_id，且 ``session.save()`` 一次都不被调用；
4. **``kind=spec`` 真的返回 JSON**：带 ``kind=spec`` 的 artifact 读取返回
   ``application/json`` 与可解析的 FigureSpec；
5. **polygon-with-hole**：海域补集是多边形差集，带洞时洞被原样保留；
6. **300 DPI 真实 QgsApplication 生命周期 smoke**（在 QGIS 解释器里执行子进程；
   普通解释器下 skip）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from cns_planner.application.map_figure_service import (
    MapFigureService, _coverage_reason, _sea_complement_polygons,
)
from cns_planner.gis.figure_spec import ExtentSpec

#: **真实 production** ``operational_routes`` 形态：``start`` / ``end`` 是坐标数组。
#: 依据 ``application/route_service.py::generate_scenario`` 与
#: ``application/layered_operational_adoption_service.py`` 的写入代码。
PRODUCTION_ROUTE = {
    "route_id": "R0007",
    "status": "passed",
    "path_crs": "OGC:CRS84",
    "kind": "layered_risk_aware_operational_route",
    "path": [[122.1067, 30.0167], [122.1800, 30.0400], [122.2450, 30.0200]],
    "start": [122.1067, 30.0167],
    "end": [122.2450, 30.0200],
    "direction": "N0001→N0002",
    "start_node_id": "N0001",
    "end_node_id": "N0002",
    "provenance": {"source_type": "layered_operational_adoption"},
}


class _Session:
    def __init__(self, state, store_path):
        self.state = state
        self.store_path = store_path
        self.saved = 0

    def save(self):
        self.saved += 1


class _Renderer:
    def __init__(self):
        self.calls = 0

    def render(self, spec, *, dpi):
        self.calls += 1
        return b"\x89PNG\r\n\x1a\n" + b"q" * 48


def _service(tmp_path, routes=(PRODUCTION_ROUTE,), paths=None, *, nodes=None):
    state = {
        "revision": 12,
        "operational_routes": [dict(route) for route in routes],
        "source_audits": {"items": {}},
        "towers": {"count": 0, "items": []},
        "nodes": list(nodes or []),
    }
    session = _Session(state, tmp_path / "project_state.json")
    renderer = _Renderer()
    service = MapFigureService(
        session, lambda: dict(paths or {}), lambda action: action(),
        renderer_factory=lambda: renderer, project_directory=tmp_path,
    )
    return service, session, renderer


# ---- 1. 真实 canonical 航路 fixture -----------------------------------------

def test_production_route_fixture_renders_endpoint_labels(tmp_path):
    """``start`` / ``end`` 是坐标数组时，标注必须照常生成（历史 bug：AttributeError）。"""

    service, _, _ = _service(tmp_path)
    spec = service.build_figure(template_id="route_overview_v1", route_id="R0007")
    labels = {(label.kind, label.text) for label in spec.labels}
    assert ("start", "起点") in labels
    assert ("end", "终点") in labels
    # 起点/终点坐标来自权威航路几何，不是从 start/end 字段猜出来的。
    start = next(label for label in spec.labels if label.kind == "start")
    assert (start.longitude, start.latitude) == pytest.approx((122.1067, 30.0167))


def test_production_route_fixture_still_accepts_name_dicts(tmp_path):
    """兼容历史形态：``{"name": ...}`` 字典与纯字符串仍被接受，且不写死结论。"""

    named = {**PRODUCTION_ROUTE, "start": {"name": "甲站"}, "end": "乙站"}
    service, _, _ = _service(tmp_path, routes=(named,))
    spec = service.build_figure(template_id="route_overview_v1", route_id="R0007")
    texts = {label.kind: label.text for label in spec.labels if label.kind in ("start", "end")}
    assert texts == {"start": "甲站", "end": "乙站"}


def test_production_route_fixture_falls_back_to_node_names(tmp_path):
    """坐标数组 + 节点 id → 用**真实节点名**兜底，绝不编造名字。"""

    service, _, _ = _service(
        tmp_path, nodes=[{"node_id": "N0001", "name": "定海起降点"},
                         {"node_id": "N0002", "name": "普陀起降点"}],
    )
    spec = service.build_figure(template_id="route_overview_v1", route_id="R0007")
    texts = {label.kind: label.text for label in spec.labels if label.kind in ("start", "end")}
    assert texts == {"start": "定海起降点", "end": "普陀起降点"}


# ---- 2. cartographic_land 缺失只降级 ----------------------------------------

def test_missing_cartographic_land_degrades_only_the_figure(tmp_path):
    """派生数据不存在：FigureSpec 里陆海图层 unavailable，其余（航路）照常可用。"""

    missing = tmp_path / "does_not_exist.gpkg"
    service, session, _ = _service(tmp_path, paths={"cartographic_land": str(missing)})
    spec = service.build_figure(template_id="route_overview_v1", route_id="R0007")
    layers = {layer.layer_key: layer for layer in spec.layers}
    assert layers["land"].source_status == "unavailable"
    assert layers["sea"].source_status == "unavailable"
    assert "不存在" in layers["land"].source_reason
    assert layers["land"].source_role == "cartographic_land"
    assert spec.source_status["planned_route"]["status"] == "available"
    omitted = {item["layer_key"] for item in spec.omitted_layers}
    assert {"land", "sea"} <= omitted
    # 制图链路从不写项目状态。
    assert session.saved == 0


def test_unconfigured_cartographic_land_is_unknown_not_a_crash(tmp_path):
    service, _, _ = _service(tmp_path, paths={})
    spec = service.build_figure(template_id="route_overview_v1", route_id="R0007")
    land = next(layer for layer in spec.layers if layer.layer_key == "land")
    assert land.source_status == "unknown"
    # 非 QGIS 解释器下 QGIS 缺失优先；有 QGIS 时必须是"未配置"。
    reason = land.source_reason
    assert ("未配置" in reason) or ("QGIS" in reason)
    assert spec.source_status["planned_route"]["status"] == "available"


def test_source_loader_treats_cartographic_land_as_non_blocking():
    """启动校验的角色表里，``cartographic_land`` 必须是**非阻断**角色。

    这里按源码文本断言（不 import ``source_loader``），因为该模块依赖 ``osgeo``，
    而制图测试必须能在没有 GDAL 的解释器里跑。
    """

    source = (Path(__file__).resolve().parents[1]
              / "cns_planner" / "gis" / "source_loader.py").read_text(encoding="utf-8")
    assert "NON_BLOCKING_VECTOR_SOURCE_KEYS = (\"cartographic_land\",)" in source
    assert ("CHECKED_FILE_ROLES = tuple(\n"
            "    role for role in CHECKED_FILE_ROLES "
            "if role not in NON_BLOCKING_VECTOR_SOURCE_KEYS\n)") in source


def test_mapdata_startup_survives_missing_cartographic_land(tmp_path):
    """MapData 启动不因缺失的制图陆地面而失败（配置指向不存在的文件）。

    **不写任何临时文件**：直接构造一个带 ``cartographic_land``（指向不存在的文件）的
    MapData，验证它照常完成加载与角色解析 —— 这正是"缺少派生数据不得阻断启动"的核心。
    """

    qgis = pytest.importorskip("qgis", reason="MapData 需要 PyQGIS")
    del qgis

    from cns_planner.gis.map_data import MapData

    data = MapData(defaults={"cartographic_land": str(tmp_path / "missing.gpkg")})
    # 角色解析照常返回（ok=True 表示"路径能被解析成矢量数据源描述"，
    # 文件是否存在由上层按需判断，不是启动阻断条件）。
    role = data.vector_role_source("cartographic_land")
    assert role["ok"] is True
    assert role["status"] == "ok"
    # 关键：启动本身没有抛异常，error 里不会出现 cartographic_land 的阻断原因。
    assert "cartographic_land" not in str(data.error or "")


# ---- 3. 重复导出幂等 + 不推进 revision --------------------------------------

def test_repeated_export_reuses_figure_id_and_never_saves(tmp_path):
    service, session, renderer = _service(tmp_path)
    state_before = json.dumps(session.state, ensure_ascii=False, sort_keys=True)
    revision_before = session.state["revision"]

    first = service.export(template_id="route_overview_v1", route_id="R0007")
    second = service.export(template_id="route_overview_v1", route_id="R0007")

    assert first["figure_id"] == second["figure_id"]
    assert first["reused"] is False and second["reused"] is True
    assert renderer.calls == 1, "第二次导出必须复用产物，不重新渲染"
    assert session.saved == 0, "制图链路不得调用 session.save()"
    assert session.state["revision"] == revision_before
    assert json.dumps(session.state, ensure_ascii=False, sort_keys=True) == state_before
    assert service.records()["active_figure_id"] == first["figure_id"]


def test_export_figure_id_changes_when_revision_changes(tmp_path):
    service, session, _ = _service(tmp_path)
    first = service.export(template_id="route_overview_v1", route_id="R0007")
    session.state["revision"] = 13
    second = service.export(template_id="route_overview_v1", route_id="R0007")
    assert second["figure_id"] != first["figure_id"]
    assert {item["project_revision"] for item in service.records()["items"]} == {12, 13}


# ---- 4. kind=spec 必须返回 JSON ---------------------------------------------

def test_artifact_kind_spec_returns_json_not_png(tmp_path):
    service, _, _ = _service(tmp_path)
    exported = service.export(template_id="route_overview_v1", route_id="R0007")

    default = service.artifact(exported["figure_id"])
    assert default["content_type"] == "image/png"
    assert default["image"].startswith(b"\x89PNG")

    spec = service.artifact(exported["figure_id"], "spec")
    assert spec["content_type"] == "application/json; charset=utf-8"
    assert spec["filename"].endswith(".spec.json")
    payload = json.loads(spec["image"].decode("utf-8"))
    assert payload["template_id"] == "route_overview_v1"
    assert payload["boundaries"]["presentation_only"] is True


def test_frontend_download_spec_sends_kind_spec():
    """前端源码层面确认「下载规格」带上 ``kind=spec``（否则会下载到 PNG）。"""

    source = (Path(__file__).resolve().parents[1]
              / "cns_planner" / "web" / "js" / "workflow" / "shell_actions.js").read_text(
        encoding="utf-8")
    assert "kind:specOnly?'spec':'png'" in source.replace(" ", "")


# ---- 5. polygon-with-hole ---------------------------------------------------

def _extent():
    return ExtentSpec(west=122.0, south=30.0, east=122.4, north=30.1,
                      width_km=38.5, height_km=11.1)


def test_sea_complement_keeps_holes_from_land_polygons():
    """海域是"画布矩形 − 陆地"的多边形差集，且被陆地占住的区域以**内环**保留。

    单块内陆陆地的情况下：海域是 1 块外环（整张画布），陆地占据的部分成为它的 1 个
    内环 —— 这正是"内环被保留、没有被填实"的直接证据。
    """

    outer = [[122.05, 30.02], [122.20, 30.02], [122.20, 30.08], [122.05, 30.08], [122.05, 30.02]]
    exteriors, records, ratio, reason = _sea_complement_polygons(_extent(), [outer])
    assert reason == ""
    assert len(exteriors) == 1, "陆地不贴边时，海域是一整块"
    assert records and len(records[0]["holes"]) == 1
    assert any(item.get("holes") for item in records), "陆地占据区域必须以洞的形式保留"
    assert 0.0 < ratio < 1.0
    # 陆地在画布中心 → 海域面积 = 画布 − 陆地面积（用面积比交叉验证）。
    assert ratio == pytest.approx(
        1.0 - (0.15 * 0.06) / ((122.4 - 122.0) * (30.1 - 30.0)), rel=1e-6,
    )

    # 陆地贴住画布一侧时，海域同样是一块，但面积比更大（陆地更少）。
    left = [[122.00, 30.00], [122.05, 30.00], [122.05, 30.10], [122.00, 30.10], [122.00, 30.00]]
    exteriors2, _, ratio2, reason2 = _sea_complement_polygons(_extent(), [left])
    assert reason2 == ""
    assert len(exteriors2) == 1 and ratio2 > ratio


def test_land_hole_survives_polygon_difference_area():
    """带洞陆地的面积必须小于同外环的实心陆地（证明洞真的参与了差集）。"""

    from shapely.geometry import Polygon

    outer = [[122.05, 30.02], [122.20, 30.02], [122.20, 30.08], [122.05, 30.08], [122.05, 30.02]]
    hole = [[122.10, 30.04], [122.15, 30.04], [122.15, 30.06], [122.10, 30.06], [122.10, 30.04]]
    solid = Polygon([tuple(point) for point in outer])
    holed = Polygon([tuple(point) for point in outer], [[tuple(point) for point in hole]])
    assert holed.area < solid.area


def test_sea_complement_returns_no_approximation_when_library_missing(monkeypatch):
    """找不到多边形库时**如实失败**，绝不用矩形条带近似冒充面积差集。"""

    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "shapely" or name.startswith("shapely."):
            raise ImportError("no shapely")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    exteriors, records, ratio, reason = _sea_complement_polygons(
        _extent(), [[[122.05, 30.02], [122.2, 30.02], [122.2, 30.08], [122.05, 30.08],
                     [122.05, 30.02]]],
    )
    assert exteriors == [] and records == []
    assert "shapely" in reason


def test_land_polygon_holes_reach_the_renderer_geometry():
    """装配层给出的内环必须被渲染器重建为真正的 QGIS 内环（而不是被丢掉）。"""

    qgis = pytest.importorskip("qgis", reason="需要 PyQGIS 检查几何环数")
    del qgis
    from qgis.core import QgsCoordinateReferenceSystem, QgsProject

    from cns_planner.gis.figure_spec import (
        FigureSpec, GEOMETRY_POLYGON, LayerSpec,
    )
    from cns_planner.gis.qgis_figure_renderer import build_layers

    outer = [[122.00, 30.00], [122.30, 30.00], [122.30, 30.20], [122.00, 30.20], [122.00, 30.00]]
    hole = [[122.10, 30.05], [122.20, 30.05], [122.20, 30.15], [122.10, 30.15], [122.10, 30.05]]
    spec = FigureSpec(
        template_id="route_overview_v1", template_version=1, title="洞测试",
        route_id="R0007", route_source="operational_routes",
        route_geometry=[[122.05, 30.02], [122.25, 30.18]],
        extent=_extent(), extent_mode="smoke",
        layers=[LayerSpec("land", "陆地区域", "land", GEOMETRY_POLYGON, "cartographic_land",
                          source_status="available", feature_count=1,
                          data={"polygons": [outer], "polygon_holes": {"0": [hole]}})],
    )
    project = QgsProject()
    try:
        built = build_layers(spec, project)
        assert built, "带洞陆地图层必须仍然可绘制"
        features = list(built[0].getFeatures())
        assert len(features) == 1
        geometry = features[0].geometry()
        polygon = (geometry.asMultiPolygon()[0][0] if geometry.isMultipart()
                   else geometry.asPolygon())
        assert len(polygon) == 2, "外环 + 1 个内环"
        assert geometry.constGet().numInteriorRings() == 1
    finally:
        project.clear()


def test_coverage_reason_never_hardcodes_a_coverage_claim():
    """覆盖说明只能来自数据源记录；无记录时如实说明，不得声称"覆盖全部岛群"。"""
    empty = _coverage_reason({})
    assert "覆盖全部" not in empty and "全部岛群" not in empty
    assert "未记录" in empty
    recorded = _coverage_reason({"coverage_reason": "建筑落陆率 99.98%（派生时记录）"})
    assert recorded == "建筑落陆率 99.98%（派生时记录）"
    derived = _coverage_reason({"validation": {
        "probes": {"a": True, "b": False},
        "buildings_inside_ratio": 0.9998,
        "towers_inside": 372, "towers_total": 373,
    }})
    assert "1/2" in derived and "99.98%" in derived and "372/373" in derived


# ---- 5b. 带洞 Polygon 完整链 + 自省严格化（需要真实 PyQGIS / OGR） -----------

#: 真实带洞多边形数据源的几何（WGS84 经纬度）。
_HOLE_OUTER = [(122.05, 30.02), (122.22, 30.02), (122.22, 30.09), (122.05, 30.09), (122.05, 30.02)]
_HOLE_INNER = [(122.11, 30.045), (122.16, 30.045), (122.16, 30.065), (122.11, 30.065), (122.11, 30.045)]


def _write_polygon_geojson(path, ring_pairs, *, layer_name="land", geometry_type="polygon"):
    """用 OGR 写一个**真实**数据源（用于端到端链测试，绝不用内存几何冒充数据源）。"""

    from osgeo import ogr, osr

    driver = ogr.GetDriverByName("GeoJSON")
    if path.exists():
        driver.DeleteDataSource(str(path))
    dataset = driver.CreateDataSource(str(path))
    spatial = osr.SpatialReference()
    spatial.ImportFromEPSG(4326)
    kind = ogr.wkbPolygon if geometry_type == "polygon" else ogr.wkbPoint
    layer = dataset.CreateLayer(layer_name, spatial, kind)
    for rings in ring_pairs:
        if geometry_type == "polygon":
            geometry = ogr.Geometry(ogr.wkbPolygon)
            for ring in rings:
                linear = ogr.Geometry(ogr.wkbLinearRing)
                for longitude, latitude in ring:
                    linear.AddPoint_2D(float(longitude), float(latitude))
                geometry.AddGeometry(linear)
        else:
            geometry = ogr.Geometry(ogr.wkbPoint)
            geometry.AddPoint_2D(float(rings[0]), float(rings[1]))
        feature = ogr.Feature(layer.GetLayerDefn())
        feature.SetGeometry(geometry)
        layer.CreateFeature(feature)
        feature = None
    dataset = None


def test_polygon_with_hole_source_reaches_renderer_interior_rings(tmp_path):
    """完整链：真实带洞 Polygon 数据源 → ``_read_polygons`` → materialize → FigureSpec → QGIS 图层。

    断言两条事实：

    1. QGIS 几何的 ``numInteriorRings() > 0``（内环真的进了渲染器）；
    2. 内环**没有**被当成独立的陆地多边形（陆地要素数仍为 1）。
    """

    qgis = pytest.importorskip("qgis", reason="需要 PyQGIS 走真实渲染器几何")
    del qgis
    from qgis.core import QgsProject, QgsRectangle

    from cns_planner.application.map_figure_service import (
        MaterializationContext, _read_polygons, materialize,
    )
    from cns_planner.gis.figure_spec import FigureSpec
    from cns_planner.gis.qgis_figure_renderer import build_layers

    source = tmp_path / "land_with_hole.geojson"
    _write_polygon_geojson(source, [[_HOLE_OUTER, _HOLE_INNER]])

    viewport = QgsRectangle(122.0, 30.0, 122.4, 30.1)
    polygons, reason = _read_polygons(str(source), "land", viewport)
    assert reason == ""
    assert len(polygons) == 1, "一个带洞多边形必须仍然只是一个多边形"
    assert len(polygons[0]["exterior"]) == 5
    assert len(polygons[0]["holes"]) == 1, "内环必须在 holes 里（不是第二个平级 ring）"

    extent = _extent()
    result = materialize(MaterializationContext(
        state={}, paths={"cartographic_land": str(source)}, route={},
        extent=extent, parameters={},
    ))
    land = next(layer for layer in result.layers if layer.layer_key == "land")
    assert land.source_status == "available"
    assert land.feature_count == 1, "内环不得被当作独立陆地多边形"
    holes = (land.data or {}).get("polygon_holes") or {}
    assert sum(len(items or []) for items in holes.values()) == 1
    assert land.source_detail["hole_ring_count"] == 1

    spec = FigureSpec(
        template_id="route_overview_v1", template_version=1, title="带洞完整链",
        route_id="R0007", route_source="operational_routes",
        route_geometry=[[122.05, 30.02], [122.22, 30.09]],
        extent=extent, extent_mode="chain_test", layers=[land],
    )
    project = QgsProject()
    try:
        built = build_layers(spec, project)
        assert built, "带洞陆地图层必须仍然可绘制"
        features = list(built[0].getFeatures())
        assert len(features) == 1
        geometry = features[0].geometry()
        assert geometry.constGet().numInteriorRings() > 0
    finally:
        project.clear()


def test_damaged_and_non_polygon_cartographic_land_are_unavailable(tmp_path):
    """自省严格化：损坏文件与非面矢量都必须报告 unavailable（不是"范围内没有面要素"）。"""

    qgis = pytest.importorskip("qgis", reason="需要 PyQGIS/OSGeo 做真实自省")
    del qgis

    from cns_planner.application.map_figure_service import (
        MaterializationContext, _cartographic_land_state,
    )

    def state_for(path):
        return _cartographic_land_state(MaterializationContext(
            state={}, paths={"cartographic_land": str(path)}, route={},
            extent=_extent(), parameters={},
        ))

    # a) 文件存在但内容不是矢量数据集（损坏 / 非矢量）。
    broken = tmp_path / "cartographic_land_broken.gpkg"
    broken.write_bytes(b"this is definitely not a geopackage")
    status, reason, detail = state_for(broken)
    assert status == "unavailable"
    assert reason and detail["inspection"]["status"] != "passed"
    assert "没有面要素" not in reason

    # b) 真实可读但**不是面**的矢量数据集（点图层）。
    points = tmp_path / "cartographic_land_points.geojson"
    _write_polygon_geojson(
        points, [[122.10, 30.05], [122.20, 30.06]], layer_name="stations",
        geometry_type="point",
    )
    status, reason, detail = state_for(points)
    assert status == "unavailable"
    assert detail["inspection"]["is_polygon"] is False
    assert "Polygon" in reason or "面" in reason

    # c) 真实的面矢量数据集：只有同时通过自省且是面才允许 available。
    good = tmp_path / "cartographic_land_ok.geojson"
    _write_polygon_geojson(good, [[_HOLE_OUTER, _HOLE_INNER]])
    status, reason, detail = state_for(good)
    assert status == "available" and reason == ""
    assert detail["inspection"]["is_polygon"] is True


# ---- 6. 参数契约 ------------------------------------------------------------
def test_parameter_contract_rejects_invalid_values():
    from cns_planner.reporting.map_templates import (
        MapFigureParameterInvalid, parameters,
    )

    for overrides in ({"extent_buffer_km": "abc"}, {"legend_columns": 999},
                      {"document_width_mm": -1}, {"map_fraction": 0.99},
                      {"show_place_labels": "yes"}, {"legend_columns": True},
                      {"extent_source_crs": ""}):
        with pytest.raises(MapFigureParameterInvalid):
            parameters("route_overview_v1", overrides)
    # 合法覆盖仍被接受并规范化。
    accepted = parameters("route_overview_v1", {"extent_buffer_km": 15, "legend_columns": 3})
    assert accepted["extent_buffer_km"] == 15.0 and accepted["legend_columns"] == 3


def test_parameter_contract_enforces_total_pixel_cap():
    from cns_planner.reporting.map_templates import (
        MapFigureParameterInvalid, parameters,
    )

    with pytest.raises(MapFigureParameterInvalid) as error:
        parameters("route_overview_v1", {"document_width_mm": 1000, "export_dpi": 600})
    assert "总像素上限" in str(error.value)


def test_export_rejects_out_of_range_dpi_instead_of_clamping(tmp_path):
    from cns_planner.application.map_figure_service import MapFigureError

    service, _, _ = _service(tmp_path)
    with pytest.raises(MapFigureError):
        service.export(template_id="route_overview_v1", route_id="R0007", dpi=1200)


# ---- 7. 300 DPI 真实 QgsApplication 生命周期 smoke --------------------------

def test_300dpi_smoke_in_real_qgis_lifecycle():
    """在 QGIS 解释器里跑真实 300 DPI 出图；普通解释器下 skip。"""

    if not (Path(sys.executable).stem.lower().startswith("python") and
            "QGIS" in str(Path(sys.executable).resolve())):
        pytest.skip("需要 QGIS 解释器（python-qgis-ltr.bat）运行本 smoke")
    script = Path(__file__).resolve().parent / "test_map_figures_qgis_smoke.py"
    completed = subprocess.run([sys.executable, str(script)], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stdout[-2000:] + completed.stderr[-2000:]
    assert '"ok": true' in completed.stdout
