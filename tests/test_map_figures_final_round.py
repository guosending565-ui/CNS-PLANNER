"""第三轮 code review 收口（A2–A6 / B4–B5）的定向回归测试。

全部只读、不跑任何业务算法、不接触真实项目状态。覆盖：

* **A2** 面向对象的多边形结构：source reader → materialize → FigureSpec 全程保留
  ``exterior`` + ``holes``（内环绝不被拍平成独立陆地多边形），MultiPolygon 的 part 各自独立；
* **A3** preview 的**总像素**上限（宽 × 高），等比缩放、长宽比不变，export 另用自己的上限；
* **A4** 旧 revision 图件的**只读动态投影**：``stale_revision``，且绝不改 index、绝不 save；
* **A5** ``cartographic_land`` 自省不通过时**不得**报告 available（unknown / unavailable / empty 严格区分）；
* **A6** ``metadata.json`` 与 ``index.json`` 的固定审计字段逐字段一致；
* **B4 / B5** route-first 产物归档 + route 级 ``index.json``（全局索引与旧平铺产物继续可读）。

需要真实 PyQGIS 的端到端链测试在 ``test_map_figures_review_fixes.py`` 里（由
``tests/run_qgis_map_figure_tests.py`` 在 QGIS 解释器下执行）。
"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest

from cns_planner.application import map_figure_service as service_module
from cns_planner.application.map_figure_service import (
    MAP_FIGURE_AUDIT_FIELDS, MAX_FIGURE_PIXELS, MAX_PREVIEW_PIXELS, MapFigurePathDenied,
    MapFigureService, MapFigureStore, MaterializationContext, _bounded_preview_width,
    _cartographic_land_state, _geometry_to_wgs84_polygons, _layout_plan, _polygon_rings,
    _sea_complement_polygons, _sites_within_route_buffer, _tower_layers, materialize,
)
from cns_planner.gis.figure_spec import ExtentSpec, FigureSpec

ROUTE = {
    "route_id": "R0001", "status": "passed", "path_crs": "OGC:CRS84",
    "kind": "layered_risk_aware_operational_route",
    "path": [[122.05, 29.95], [122.20, 30.01], [122.35, 29.98]],
    "start": [122.05, 29.95], "end": [122.35, 29.98],
    "start_node_id": "N0001", "end_node_id": "N0002",
}
ROUTE_TWO = {**ROUTE, "route_id": "R0002", "path": [[122.00, 29.90], [122.30, 30.05]]}


class _Session:
    def __init__(self, state, store_path):
        self.state = state
        self.store_path = store_path
        self.saved = 0

    def save(self):
        self.saved += 1


class _RecordingRenderer:
    def __init__(self):
        self.dpis = []

    def render(self, spec, *, dpi):
        self.dpis.append(float(dpi))
        return b"\x89PNG\r\n\x1a\n" + b"r" * 64


def _service(tmp_path, routes=(ROUTE,), paths=None, revision=9):
    state = {
        "revision": revision,
        "operational_routes": [dict(route) for route in routes],
        "source_audits": {"items": {}},
        "towers": {"count": 0, "items": []},
    }
    session = _Session(state, tmp_path / "project_state.json")
    renderer = _RecordingRenderer()
    service = MapFigureService(
        session, lambda: dict(paths or {}), lambda action: action(),
        renderer_factory=lambda: renderer, project_directory=tmp_path,
    )
    return service, session, renderer


def _extent():
    return ExtentSpec(west=122.0, south=30.0, east=122.4, north=30.1,
                      width_km=38.5, height_km=11.1)


def _stub_pyqgis(monkeypatch):
    """注入最小 ``qgis.core.QgsRectangle``（装配层只在范围过滤时用到它）。"""

    import sys
    import types

    core = types.ModuleType("qgis.core")

    class QgsRectangle:
        def __init__(self, x_min=0.0, y_min=0.0, x_max=0.0, y_max=0.0):
            self._box = (x_min, y_min, x_max, y_max)

        def contains(self, x, y):
            x_min, y_min, x_max, y_max = self._box
            return x_min <= x <= x_max and y_min <= y <= y_max

    core.QgsRectangle = QgsRectangle
    package = types.ModuleType("qgis")
    package.core = core
    monkeypatch.setitem(sys.modules, "qgis", package)
    monkeypatch.setitem(sys.modules, "qgis.core", core)
    return QgsRectangle


def test_route_overview_sites_use_10km_metric_route_buffer_and_report_counts(monkeypatch):
    """A4 纵向扩展只扩底图；通信站址仍按 EPSG:32651 航路距离筛选。"""

    pytest.importorskip("pyproj")
    pytest.importorskip("shapely")
    _stub_pyqgis(monkeypatch)
    monkeypatch.setattr(service_module, "_qgis_available", lambda: True)
    route = {"path": [[122.00, 30.00], [122.40, 30.00]]}
    towers = [
        {"tower_id": "ON", "coordinate": [122.10, 30.00]},
        {"tower_id": "NEAR", "coordinate": [122.20, 30.05]},
        {"tower_id": "FAR", "coordinate": [122.20, 30.15]},
    ]
    extent = ExtentSpec(west=121.9, south=29.8, east=122.5, north=30.2,
                        width_km=58.0, height_km=44.0)
    ctx = MaterializationContext(
        state={"towers": {"items": towers, "count": 3}}, paths={}, route=route,
        extent=extent, parameters={"site_display_buffer_km": 10.0},
    )

    existing = next(layer for layer in _tower_layers(ctx, extent, 100.0)
                    if layer.layer_key == "tower_existing")

    assert [item["tower_id"] for item in existing.data["points"]] == ["ON", "NEAR"]
    assert existing.source_detail["total_count"] == 3
    assert existing.source_detail["within_extent"] == 3
    assert existing.source_detail["within_route_buffer"] == 2
    assert existing.source_detail["omitted_by_route_distance"] == 1
    assert existing.source_detail["distance_crs"] == "EPSG:32651"
    assert existing.source_detail["affects_communication_or_cns_planning"] is False


def test_route_buffer_filter_keeps_near_site_and_omits_far_site():
    pytest.importorskip("pyproj")
    pytest.importorskip("shapely")
    points = [
        {"tower_id": "near", "longitude": 122.2, "latitude": 30.05},
        {"tower_id": "far", "longitude": 122.2, "latitude": 30.15},
    ]
    kept = _sites_within_route_buffer(
        points, [[122.0, 30.0], [122.4, 30.0]], buffer_km=10.0,
    )
    assert [item["tower_id"] for item in kept] == ["near"]


# =========================================================================== A2

class _Point:
    def __init__(self, x, y):
        self._x, self._y = float(x), float(y)

    def x(self):
        return self._x

    def y(self):
        return self._y


class _StubGeometry:
    """最小 QGIS 几何替身：只需要 isEmpty / isMultipart / asMultiPolygon / asPolygon。"""

    def __init__(self, parts):
        self._parts = [[[_Point(x, y) for x, y in ring] for ring in part] for part in parts]

    def isEmpty(self):
        return not self._parts

    def isMultipart(self):
        return len(self._parts) > 1

    def asMultiPolygon(self):
        return self._parts

    def asPolygon(self):
        return self._parts[0]


class _StubLayer:
    """WGS84 图层：``crs()`` 返回 None 时不做坐标变换（本测试只验证结构保真）。"""

    def crs(self):
        return None


def test_geometry_to_wgs84_polygons_keeps_exterior_and_holes():
    """Polygon 的内环必须留在 ``holes`` 里，绝不作为独立多边形出现。"""

    outer = [(122.05, 30.02), (122.20, 30.02), (122.20, 30.08), (122.05, 30.08), (122.05, 30.02)]
    hole = [(122.10, 30.04), (122.15, 30.04), (122.15, 30.06), (122.10, 30.06), (122.10, 30.04)]
    # parts = [part]，part = [外环, 内环]
    polygons = _geometry_to_wgs84_polygons(_StubGeometry([[outer, hole]]), _StubLayer())
    assert len(polygons) == 1
    assert len(polygons[0]["exterior"]) == 5
    assert len(polygons[0]["holes"]) == 1
    assert polygons[0]["holes"][0][0] == [122.10, 30.04]
    # 关键断言：内环**没有**被当成第二个陆地多边形（历史 bug 的表现）。
    assert len(_polygon_rings(polygons)) == 1


def test_geometry_to_wgs84_polygons_keeps_multipolygon_parts_independent():
    first = [(122.00, 30.00), (122.05, 30.00), (122.05, 30.02), (122.00, 30.02), (122.00, 30.00)]
    second_outer = [(122.10, 30.00), (122.20, 30.00), (122.20, 30.02), (122.10, 30.02), (122.10, 30.00)]
    second_hole = [(122.13, 30.005), (122.17, 30.005), (122.17, 30.015), (122.13, 30.015), (122.13, 30.005)]
    geometry = _StubGeometry([[first], [second_outer, second_hole]])
    polygons = _geometry_to_wgs84_polygons(geometry, _StubLayer())
    assert len(polygons) == 2
    assert polygons[0]["holes"] == []
    assert len(polygons[1]["holes"]) == 1


def test_geometry_to_wgs84_polygons_drops_degenerate_rings_only():
    good = [(122.00, 30.00), (122.05, 30.00), (122.05, 30.02), (122.00, 30.02), (122.00, 30.00)]
    degenerate = [(122.10, 30.00), (122.11, 30.00), (122.11, 30.01)]
    polygons = _geometry_to_wgs84_polygons(_StubGeometry([[good, degenerate]]), _StubLayer())
    assert len(polygons) == 1
    assert polygons[0]["holes"] == []


def test_sea_complement_uses_land_holes_in_the_difference():
    """带洞陆地的差集：洞区域属于海域，因此海域会多出一块**岛屿内部**的水面。"""

    outer = [[122.05, 30.02], [122.20, 30.02], [122.20, 30.08], [122.05, 30.08], [122.05, 30.02]]
    hole = [[122.10, 30.04], [122.15, 30.04], [122.15, 30.06], [122.10, 30.06], [122.10, 30.04]]
    extent = _extent()

    solid_exteriors, _, solid_ratio, _ = _sea_complement_polygons(
        extent, [{"exterior": outer, "holes": []}],
    )
    holed_exteriors, holed_records, holed_ratio, reason = _sea_complement_polygons(
        extent, [{"exterior": outer, "holes": [hole]}],
    )
    assert reason == ""
    # 实心陆地：海域是整张画布（陆地在其中，成为内环）。
    assert len(solid_exteriors) == 1
    # 带洞陆地：海域外多出一块独立的湖面，因此海域面积更大、多边形更多。
    assert len(holed_exteriors) >= len(solid_exteriors)
    assert holed_ratio > solid_ratio
    lake_area = (0.05 * 0.02)
    canvas_area = (extent.east - extent.west) * (extent.north - extent.south)
    assert holed_ratio - solid_ratio == pytest.approx(lake_area / canvas_area, rel=1e-6)
    # 陆地在画布内部 → 它以**内环**的形式出现在海域里（而不是被填实）。
    assert any(item.get("holes") for item in holed_records)


def test_materialize_land_layer_carries_holes_from_structured_polygons(monkeypatch):
    """装配层：制图陆地面的内环必须原样进入 FigureSpec 的 ``polygon_holes``。"""

    outer = [[122.05, 30.02], [122.20, 30.02], [122.20, 30.08], [122.05, 30.08], [122.05, 30.02]]
    hole = [[122.10, 30.04], [122.15, 30.04], [122.15, 30.06], [122.10, 30.06], [122.10, 30.04]]
    _stub_pyqgis(monkeypatch)
    monkeypatch.setattr(service_module, "_qgis_available", lambda: True)
    monkeypatch.setattr(
        service_module, "_cartographic_land_state",
        lambda ctx: ("available", "", {"role": "cartographic_land"}),
    )
    monkeypatch.setattr(
        service_module, "_cartographic_land_polygons",
        lambda ctx, viewport: ([{"exterior": outer, "holes": [hole]},
                                {"exterior": outer, "holes": []}], ""),
    )
    result = materialize(MaterializationContext(
        state={}, paths={}, route={}, extent=_extent(), parameters={},
    ))
    land = next(layer for layer in result.layers if layer.layer_key == "land")
    assert land.feature_count == 2
    assert land.data["polygon_holes"] == {"0": [hole]}
    assert land.source_detail["hole_ring_count"] == 1
    # 海域的差集同样带着陆地内环（湖面不会被填实）。
    sea = next(layer for layer in result.layers if layer.layer_key == "sea")
    assert sea.source_detail["derivation"] == "canvas_rectangle_minus_cartographic_land"


# =========================================================================== A3

def test_preview_total_pixels_are_bounded_without_changing_aspect():
    """``width_px = 4000`` 时最终 ``width × height <= MAX_PREVIEW_PIXELS`` 且长宽比不变。"""

    layout = _layout_plan({})
    width = _bounded_preview_width(4000, layout)
    aspect = layout["document_height_mm"] / layout["document_width_mm"]
    height = width * aspect
    assert width < 4000
    assert width * height <= MAX_PREVIEW_PIXELS
    assert height / width == pytest.approx(aspect, rel=1e-9)
    # 边界内不缩放（保持请求值）。
    assert _bounded_preview_width(1200, layout) == 1200


def test_render_preview_uses_the_bounded_width(tmp_path):
    service, _, renderer = _service(tmp_path, routes=(ROUTE,))
    result = service.render_preview(
        template_id="route_overview_v1", route_id="R0001", width_px=4000,
    )
    layout = result["spec"].layout
    dpi = renderer.dpis[-1]
    width = dpi * layout["document_width_mm"] / 25.4
    height = width * layout["document_height_mm"] / layout["document_width_mm"]
    assert width * height <= MAX_PREVIEW_PIXELS
    assert width < 4000


def test_export_keeps_its_own_pixel_cap(tmp_path):
    """preview 与 export 各用各的上限：300 DPI 的 A4 竖版仍在导出上限之内。"""

    service, _, renderer = _service(tmp_path, routes=(ROUTE,))
    service.export(template_id="route_overview_v1", route_id="R0001", dpi=300)
    layout = _layout_plan({})
    width = layout["document_width_mm"] / 25.4 * 300
    height = layout["document_height_mm"] / 25.4 * 300
    assert width * height <= MAX_FIGURE_PIXELS
    assert renderer.dpis[-1] == 300.0
    assert MAX_PREVIEW_PIXELS < MAX_FIGURE_PIXELS


# =========================================================================== A4

def test_old_revision_record_is_projected_stale_without_touching_the_index(tmp_path):
    service, session, _ = _service(tmp_path, routes=(ROUTE,), revision=9)
    exported = service.export(template_id="route_overview_v1", route_id="R0001")
    figure_id = exported["figure_id"]
    index_before = service.store.index_path.read_bytes()
    state_before = json.dumps(session.state, ensure_ascii=False, sort_keys=True)

    # 业务 revision 前进（模拟项目被真正修改），**不写任何索引**。
    session.state["revision"] = 10
    records = service.records()
    item = next(entry for entry in records["items"] if entry["figure_id"] == figure_id)
    assert item["current_applicability"] == "stale_revision"
    assert records["project_revision"] == 10
    assert item["project_revision"] == 9

    # 只读投影：索引文件逐字节不变，也没有任何 ProjectState 写入。
    assert service.store.index_path.read_bytes() == index_before
    assert session.saved == 0
    assert json.dumps(session.state, ensure_ascii=False, sort_keys=True) != state_before
    assert "map_figures" not in session.state
    # 审计字段本身没有被投影改写：metadata.json 里**没有**动态投影字段。
    metadata = json.loads((tmp_path / exported["record"]["record_relative_path"]).read_text(
        encoding="utf-8"))
    assert "current_applicability" not in metadata
    index_after = json.loads(service.store.index_path.read_text(encoding="utf-8"))
    stored = next(item for item in index_after["items"] if item["figure_id"] == figure_id)
    assert stored["current_applicability"] == "current"


def test_applicability_projection_covers_current_and_superseded(tmp_path):
    service, session, _ = _service(tmp_path, routes=(ROUTE,), revision=9)
    first = service.export(template_id="route_overview_v1", route_id="R0001", dpi=300)
    session.state["revision"] = 10
    second = service.export(template_id="route_overview_v1", route_id="R0001", dpi=300)

    def projected():
        return {entry["figure_id"]: entry["current_applicability"]
                for entry in service.records()["items"]}

    # revision=10：第二条是当前图件；第一条（revision 9）是旧 revision 图件。
    assert projected() == {second["figure_id"]: "current",
                           first["figure_id"]: "stale_revision"}
    # revision 回到 9：第一条 revision 相等但不是 active → superseded；
    # 第二条 revision 不等 → stale_revision。
    session.state["revision"] = 9
    assert projected() == {first["figure_id"]: "superseded",
                           second["figure_id"]: "stale_revision"}


# =========================================================================== A5

def _land_context(tmp_path, path):
    return MaterializationContext(
        state={}, paths={"cartographic_land": str(path)}, route={},
        extent=_extent(), parameters={},
    )


@pytest.mark.parametrize("inspection", [
    {"status": "unavailable", "reason": "制图陆地面矢量自省失败：矢量数据源无法打开",
     "is_polygon": None},
    {"status": "not_polygon", "reason": "制图陆地面必须是 Polygon/MultiPolygon，实际为 Point",
     "is_polygon": False},
    {"status": "passed", "is_polygon": False},
    {"status": "passed"},
    {"status": "blocked", "reason": "invalid_geometry_fail_closed", "is_polygon": True},
])
def test_cartographic_land_requires_passed_polygon_inspection(tmp_path, monkeypatch, inspection):
    path = tmp_path / "cartographic_land_v1.gpkg"
    path.write_bytes(b"definitely not a geopackage")
    monkeypatch.setattr(
        service_module, "inspect_cartographic_land", lambda _path: dict(inspection),
    )
    status, reason, detail = _cartographic_land_state(_land_context(tmp_path, path))
    assert status == "unavailable"
    assert reason, "不可用时必须给出中文原因，绝不能静默降级成'范围内没有面要素'"
    assert detail["inspection"]["status"] == inspection["status"]
    # 规则本身也写进审计细节，便于事后核对判定依据。
    assert detail["availability_rule"] == (
        "inspection.status == passed AND inspection.is_polygon is True"
    )


def test_cartographic_land_available_only_when_inspection_passes(tmp_path, monkeypatch):
    path = tmp_path / "cartographic_land_v1.gpkg"
    path.write_bytes(b"fake but reported as valid")
    monkeypatch.setattr(service_module, "inspect_cartographic_land", lambda _path: {
        "status": "passed", "is_polygon": True, "geometry_type": "MultiPolygon",
        "crs": "EPSG:4326", "feature_count": 637,
    })
    status, reason, detail = _cartographic_land_state(_land_context(tmp_path, path))
    assert status == "available" and reason == ""
    assert detail["crs"] == "EPSG:4326"


def test_inspected_but_unusable_land_is_unavailable_not_empty(tmp_path, monkeypatch):
    """自省失败的陆地：图层状态必须是 unavailable（不是 empty_within_extent）。"""

    path = tmp_path / "cartographic_land_v1.gpkg"
    path.write_bytes(b"broken")
    _stub_pyqgis(monkeypatch)
    monkeypatch.setattr(service_module, "_qgis_available", lambda: True)
    monkeypatch.setattr(service_module, "inspect_cartographic_land", lambda _path: {
        "status": "unavailable", "reason": "制图陆地面矢量自省失败：矢量数据源无法打开",
        "is_polygon": None,
    })
    result = materialize(MaterializationContext(
        state={}, paths={"cartographic_land": str(path)}, route={},
        extent=_extent(), parameters={},
    ))
    layers = {layer.layer_key: layer for layer in result.layers}
    assert layers["land"].source_status == "unavailable"
    assert layers["sea"].source_status == "unavailable"
    assert "自省失败" in layers["land"].source_reason
    statuses = {item["layer_key"]: item["status"] for item in result.omitted_layers}
    assert statuses["land"] == "unavailable"
    assert statuses["sea"] == "unavailable"
    # 关键区分：陆地 / 海域是 unavailable，**不是** "范围内没有要素"。
    assert statuses["land"] != "empty_within_extent"
    assert statuses["sea"] != "empty_within_extent"


# =========================================================================== A6

def test_metadata_json_matches_index_audit_facts(tmp_path):
    service, _, _ = _service(tmp_path, routes=(ROUTE,))
    exported = service.export(template_id="route_overview_v1", route_id="R0001")
    record = exported["record"]
    figure_id = exported["figure_id"]

    metadata_path = tmp_path / record["record_relative_path"]
    assert metadata_path.is_file()
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    index = json.loads(service.store.index_path.read_text(encoding="utf-8"))
    index_record = next(item for item in index["items"] if item["figure_id"] == figure_id)

    for field in MAP_FIGURE_AUDIT_FIELDS:
        assert field in metadata, field
        assert field in index_record, field
        assert metadata[field] == index_record[field], field
    # 产物本身的摘要必须与磁盘上的 PNG 一致。
    png = (tmp_path / record["relative_path"]).read_bytes()
    assert metadata["image_sha256"] == sha256(png).hexdigest()
    assert metadata["artifact_ref"]["artifact_id"] == metadata["image_sha256"]
    assert metadata["artifact_ref"]["relative_path"] == metadata["relative_path"]
    assert metadata["image_bytes"] == len(png)
    # index 额外允许存在的只有动态投影字段。
    assert set(index_record) - set(metadata) <= {"current_applicability"}


def test_route_index_survives_a_fresh_read(tmp_path):
    """route 级索引落盘后可重新读取（人工按航路查找图件的入口）。"""

    service, _, _ = _service(tmp_path, routes=(ROUTE,))
    exported = service.export(template_id="route_overview_v1", route_id="R0001")
    reloaded = MapFigureStore(tmp_path).read_route_index("R0001")
    assert reloaded["templates"]["route_overview_v1"]["active_figure_id"] == exported["figure_id"]


# ======================================================================= B4 / B5

def test_artifacts_are_archived_route_first(tmp_path):
    service, _, _ = _service(tmp_path, routes=(ROUTE,))
    exported = service.export(template_id="route_overview_v1", route_id="R0001")
    record = exported["record"]
    assert record["relative_path"].startswith("artifacts/map_figures/routes/")
    directory = (tmp_path / record["relative_path"]).parent
    assert directory.name == exported["figure_id"]
    assert directory.parent.name == "route_overview_v1"
    assert directory.parent.parent.parent.name == "routes"
    for name in ("figure.png", "figure_spec.json", "metadata.json"):
        assert (directory / name).is_file()
    # 旧平铺目录不得被创建。
    assert not (tmp_path / "artifacts" / "map_figures" / exported["figure_id"]).exists()


@pytest.mark.parametrize("raw", [
    "../../etc/passwd", "C:/windows/system32", "R0001/../../evil", "舟山 航路/../x", "",
    "a" * 300, "....", "\u0000null",
])
def test_sanitized_route_id_is_server_side_and_path_safe(raw):
    name = MapFigureStore.sanitize_route_id(raw)
    assert name
    assert "/" not in name and "\\" not in name and ".." not in name
    assert all(character.isalnum() or character in "_-" for character in name)
    assert len(name) <= 80
    # 稳定：同一个 route_id 永远进同一个目录。
    assert MapFigureStore.sanitize_route_id(raw) == name


def test_sanitized_route_ids_do_not_collide(tmp_path):
    store = MapFigureStore(tmp_path)
    first = store.route_directory("R0001", create=True)
    second = store.route_directory("R0002", create=True)
    assert first != second
    # 清洗后同名的不同 route_id 也必须落在不同目录（摘要参与命名）。
    assert store.route_directory("a/b", create=True) != store.route_directory("a\\b", create=True)


def test_template_directory_rejects_unsafe_template_ids(tmp_path):
    store = MapFigureStore(tmp_path)
    for template_id in ("../../evil", "route/overview", "", "Route-Overview", "x" * 65):
        with pytest.raises(MapFigurePathDenied):
            store.template_directory("R0001", template_id)


def test_route_index_lists_templates_with_required_fields(tmp_path):
    service, session, _ = _service(tmp_path, routes=(ROUTE, ROUTE_TWO))
    exported = service.export(template_id="route_overview_v1", route_id="R0001")
    route_index = service.store.read_route_index("R0001")
    assert route_index["schema_version"] == 1
    assert route_index["route_id"] == "R0001"
    assert route_index["sanitized_route_id"] == MapFigureStore.sanitize_route_id("R0001")
    entry = route_index["templates"]["route_overview_v1"]
    assert entry["active_figure_id"] == exported["figure_id"]
    assert entry["items"], "route 索引必须记录该模板下的图件清单"
    item = entry["items"][0]
    for field in ("figure_id", "template_id", "generated_at", "project_revision",
                  "relative_path", "spec_relative_path", "image_sha256", "dpi"):
        assert item.get(field) is not None, field
    assert service.store.route_index_path("R0001").is_file()
    # 两个索引都不推进业务 revision。
    assert session.saved == 0
    assert "map_figures" not in session.state


def test_global_index_is_still_written(tmp_path):
    service, _, _ = _service(tmp_path, routes=(ROUTE,))
    exported = service.export(template_id="route_overview_v1", route_id="R0001")
    index = json.loads(service.store.index_path.read_text(encoding="utf-8"))
    assert index["active_figure_id"] == exported["figure_id"]
    assert index["count"] == 1
    assert index["items"][0]["figure_id"] == exported["figure_id"]


def test_legacy_flat_artifacts_stay_readable_and_are_not_migrated(tmp_path):
    """旧布局 ``artifacts/map_figures/<figure_id>/`` 只读兼容：不搬迁、不删除。"""

    store = MapFigureStore(tmp_path)
    figure_id = "MF-" + "e" * 32
    written = store.write(
        figure_id, image_bytes=b"\x89PNG\r\n\x1a\n" + b"legacy" * 8,
        spec_payload={"schema_version": 1, "template_id": "route_overview_v1"},
        record={
            "figure_id": figure_id, "template_id": "route_overview_v1",
            "template_version": 1, "title": "航路周边状况图", "route_id": "R0001",
            "route_source": "operational_routes", "format": "png", "dpi": 300.0,
            "generated_at": "2026-01-01T00:00:00+00:00", "project_revision": 9,
            "spec_fingerprint": "legacy", "current_applicability": "current",
        },
    )
    store.write_index([written], figure_id)
    flat = tmp_path / "artifacts" / "map_figures" / figure_id
    assert flat.is_dir()

    service, session, _ = _service(tmp_path, routes=(ROUTE,), revision=9)
    artifact = service.artifact(figure_id, "png")
    assert artifact["image"].startswith(b"\x89PNG")
    spec = service.artifact(figure_id, "spec")
    assert json.loads(spec["image"].decode("utf-8"))["template_id"] == "route_overview_v1"
    # 没有自动搬迁：旧目录仍在原处，且没有为它新建 route-first 目录。
    assert flat.is_dir()
    assert not (tmp_path / "artifacts" / "map_figures" / "routes").exists()
    # 记录照常出现在只读投影里（并且 revision 相等 → current）。
    items = {entry["figure_id"]: entry for entry in service.records()["items"]}
    assert items[figure_id]["current_applicability"] == "current"
    assert session.saved == 0


# ==================================================================== B1 / B3

class _Box:
    """最小 QgsRectangle 替身（比例尺几何只需要 width/height/isEmpty）。"""

    def __init__(self, width, height):
        self._width, self._height = float(width), float(height)

    def isEmpty(self):
        return self._width <= 0 or self._height <= 0

    def width(self):
        return self._width

    def height(self):
        return self._height


def _figure_for_layout(layout):
    return FigureSpec(
        template_id="route_overview_v1", template_version=1, title="航路周边状况图",
        route_id="R0001", route_source="operational_routes",
        route_geometry=[[122.05, 29.95], [122.35, 29.98]],
        extent=ExtentSpec(west=122.0, south=30.0, east=122.43, north=30.16,
                          width_km=48.0, height_km=17.8),
        extent_mode="test", layout=layout,
    )


def test_scale_bar_geometry_is_exact_and_stays_inside_the_frame():
    """黑白分段比例尺：段宽由真实渲染范围换算，且整体（含标签）都在地图框内。

    Round30-B1 起地图画布是**米制**（``layout['map_crs'] = EPSG:32651``），
    因此这里传入的渲染范围也必须是米（QGIS 交回来的就是这个 CRS 下的矩形）。
    """

    from cns_planner.gis.figure_style import LAYOUT
    from cns_planner.gis.qgis_figure_renderer import DEFAULT_MAP_CRS, scale_bar_geometry

    layout = _layout_plan({})
    spec = _figure_for_layout(layout)
    assert layout["map_crs"] == DEFAULT_MAP_CRS
    # 与 FigureSpec.extent（122.00~122.43°E ≈ 42 km）等价的米制渲染范围。
    geometry = scale_bar_geometry(spec, _Box(42_000.0, 17_800.0), layout)

    # 条宽与"真实公里数"严格一致：width_mm / map_width_mm == total_km / rendered_width_km
    assert geometry["width_mm"] / layout["map_width_mm"] == pytest.approx(
        geometry["total_km"] / geometry["rendered_width_km"], rel=1e-9,
    )
    assert geometry["segment_width_mm"] * geometry["segments"] == pytest.approx(
        geometry["width_mm"], rel=1e-9,
    )
    assert geometry["total_km"] > 0 and geometry["rendered_width_km"] > 0
    assert (LAYOUT["scalebar_target_min_mm"] <= geometry["width_mm"]
            <= LAYOUT["scalebar_target_max_mm"])
    # Round30-B1.1：分段比例尺每段一个刻度 + 数字（0 | 2 | 4 km），而不是只给两端。
    assert int(geometry["segments"]) >= 3
    total_km = float(geometry["total_km"])
    segment_km = float(geometry["segment_km"])
    assert segment_km > 0
    assert total_km == pytest.approx(segment_km * int(geometry["segments"]), rel=1e-9)

    # 与左边框 / 下边框保留内距，且不越出地图框。
    map_left, map_top = layout["map_left_mm"], layout["map_top_mm"]
    assert geometry["left_mm"] == pytest.approx(map_left + geometry["margin_mm"])
    assert geometry["margin_mm"] >= 4.0
    assert geometry["left_mm"] + geometry["width_mm"] <= map_left + layout["map_width_mm"]
    assert geometry["top_mm"] + geometry["height_mm"] <= map_top + layout["map_height_mm"]

    # 标签在条上方且与条有固定间隙（"10 km" 不贴条），仍在地图框内。
    label_top = geometry["top_mm"] - geometry["label_gap_mm"] - geometry["label_height_mm"]
    assert geometry["label_gap_mm"] > 0
    assert label_top > map_top
    assert label_top + geometry["label_height_mm"] < geometry["top_mm"]


def test_title_band_keeps_two_fixed_gaps():
    """主标题 → 固定间距 → 副标题 → 固定间距 → 地图框（副标题绝不贴地图上边框）。"""

    layout = _layout_plan({})
    assert layout["subtitle_top_mm"] - (
        layout["title_top_mm"] + layout["title_main_height_mm"]
    ) == pytest.approx(layout["title_gap_mm"])
    subtitle_bottom = layout["subtitle_top_mm"] + layout["subtitle_height_mm"]
    assert layout["map_top_mm"] - subtitle_bottom == pytest.approx(layout["title_map_gap_mm"])
    assert layout["title_map_gap_mm"] > 0


def test_audit_strip_sits_between_map_and_legend_without_overlap():
    """审计条严格位于地图框之下、图例框之上，三段互不重叠且都不压图例标题。

    Round30-B1.1 起审计条由 ``audit_footer`` 参数控制（**正式图默认关闭**），
    因此这里显式打开它来验证几何关系仍然成立。
    """

    for legend_items in ((), _legend_entries_fixture()):
        layout = _layout_plan({"audit_footer": True}, legend_items)
        map_bottom = layout["map_top_mm"] + layout["map_height_mm"]
        strip_top = layout["footer_strip_top_mm"]
        strip_bottom = strip_top + layout["footer_strip_mm"]
        assert strip_top >= map_bottom
        assert strip_bottom <= layout["legend_top_mm"]
        # 图例标题独占一行且位于框内（框顶 + 少量内边距）。
        assert layout["legend_top_mm"] < strip_bottom + layout["map_legend_gap_mm"]
        assert layout["legend_height_mm"] > 0
        # 正式图默认关闭审计条：同一套图例条目的正式版面里它是 None。
        formal = _layout_plan({}, legend_items)
        assert formal["audit_footer"] is False
        assert formal["footer_strip_top_mm"] is None


def _legend_entries_fixture():
    """route_overview_v1 的真实图例条目（分组顺序与线上一致）。"""

    return [
        {"group": "environment", "text": "海域", "style_key": "sea"},
        {"group": "environment", "text": "陆地区域", "style_key": "land"},
        {"group": "obstacle", "text": "地形障碍（≥ 显示阈值）", "style_key": "terrain_obstacle"},
        {"group": "obstacle", "text": "建筑障碍（≥ 显示阈值）", "style_key": "building_obstacle"},
        {"group": "facility", "text": "既有通信站址", "style_key": "tower_existing"},
        {"group": "route", "text": "规划航路", "style_key": "planned_route"},
        {"group": "route", "text": "航路转弯点", "style_key": "turn_point"},
        {"group": "route", "text": "起点", "style_key": "start_point"},
        {"group": "route", "text": "终点", "style_key": "end_point"},
    ]


def test_legend_is_two_balanced_columns_with_semantic_groups():
    """图例：2 列、左右条目数均衡（4 : 5）、分组名是「既有设施」（当前没有机场）。"""

    from collections import Counter

    from cns_planner.gis.figure_legend import legend_geometry
    from cns_planner.gis.figure_style import LAYOUT, LEGEND_GROUP_COLUMNS

    entries = _legend_entries_fixture()
    layout = _layout_plan({}, entries)
    geometry = legend_geometry(
        entries, row_height=float(layout["legend_row_mm"]),
        group_row=float(layout["legend_group_row_mm"]),
        columns=int(layout["legend_columns"]),
        header_height=float(LAYOUT["legend_header_mm"]),
        group_gap=float(layout["legend_group_gap_mm"]),
        group_item_gap=float(layout["legend_group_item_gap_mm"]),
        group_columns=LEGEND_GROUP_COLUMNS,
        top_padding=float(layout["legend_top_padding_mm"]),
    )
    assert int(geometry["columns"]) == 2
    per_column = Counter(column for column, _y, kind, _t, _s in geometry["rows"]
                         if kind == "item")
    counts = [per_column.get(index, 0) for index in range(2)]
    assert sorted(counts) == [4, 5], counts
    groups = {text: column for column, _y, kind, text, _s in geometry["rows"]
              if kind == "group"}
    assert groups["地理环境"] == 0 and groups["障碍物"] == 0
    assert groups["既有设施"] == 1 and groups["规划航路"] == 1
    assert "既有设施与机场" not in groups
    # 框高贴合内容：没有"框很高、内容只占左上角"的大块空白。
    assert abs(float(layout["legend_height_mm"])
               - float(geometry["box_height_mm"])) <= 0.6
    # 图例标题与首行之间只留很小的内边距（不是大块空白）。
    first_row_y = min(y for _c, y, _k, _t, _s in geometry["rows"])
    assert first_row_y - float(geometry["title_height_mm"]) <= float(
        LAYOUT["legend_top_padding_mm"]) + 1e-9
