"""专题成果图（Presentation / Cartographic Export）定向测试。

覆盖本轮的四条硬契约：

1. **模板目录**：只有 ``route_overview_v1`` 可用，其余 5 个模板如实标记 ``planned``；
2. **航路选择**：显式 ``route_id`` 优先；唯一可绘图航路可自动选择；多条时必须要求显式选择；
   没有航路 / 几何不足时给出明确中文原因，绝不随便取第一条；
3. **缺失数据**：图层不可用时图件仍能生成，但 FigureSpec / 记录里写明 omitted + 原因
   （``unknown ≠ 0``、``missing ≠ empty``）；
4. **产物路径安全**：客户端不能指定输出路径，图号必须匹配受控格式，越界一律拒绝。

这些测试**不重跑**任何业务算法，也不修改 ProjectState：所有 session 都是最小替身。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cns_planner.application.map_figure_service import (
    MAP_FIGURE_COLLECTION, MapFigureError, MapFigureFormatUnsupported,
    MapFigureNotFound, MapFigurePathDenied, MapFigureRouteError,
    MapFigureRouteSelectionRequired, MapFigureService, MapFigureStore,
    MapFigureTemplateUnavailable,
)
from cns_planner.gis.figure_spec import FigureSpec
from cns_planner.reporting.map_templates import catalog


#: **真实 canonical** ``operational_routes`` 记录形态（务必与生产 writer 一致）：
#: ``route_service.generate_scenario`` 与 ``LayeredOperationalAdoptionService._apply_working``
#: 写出的 ``start`` / ``end`` 是**坐标数组** ``[lon, lat]``，**不是** ``{"name": ...}`` 字典；
#: 起终点名称只存在于 ``scenario_routes`` / ``nodes`` 一侧，制图必须容忍这种形态。
ROUTE = {
    "route_id": "R0001", "status": "passed", "path_crs": "OGC:CRS84",
    "kind": "layered_risk_aware_operational_route",
    "path": [[122.05, 29.95], [122.20, 30.01], [122.35, 29.98]],
    "start": [122.05, 29.95], "end": [122.35, 29.98],
    "start_node_id": "N0001", "end_node_id": "N0002",
    "provenance": {"source_type": "layered_operational_adoption"},
}
ROUTE_TWO = {
    **ROUTE, "route_id": "R0002",
    "path": [[122.00, 29.90], [122.30, 30.05]],
}


class _Session:
    """最小 session 替身：专题图只读 ``state`` 与 ``store_path``。"""

    def __init__(self, state, store_path):
        self.state = state
        self.store_path = store_path
        self.saved = 0

    def save(self):
        self.saved += 1


class _FakeRenderer:
    """假渲染器：只产出可识别的 PNG 头，不接触 QGIS。"""

    def __init__(self, payload=b"\x89PNG\r\n\x1a\n" + b"0" * 64):
        self.payload = payload
        self.calls = []

    def render(self, spec, *, dpi):
        self.calls.append((spec.template_id, spec.route_id, float(dpi)))
        return self.payload


def _state(routes=(), **overrides):
    state = {"revision": 7, "operational_routes": list(routes),
             "source_audits": {"items": {}}, "towers": {"count": 0, "items": []}}
    state.update(overrides)
    return state


def _service(tmp_path, routes=(), paths=None, renderer=None, state_overrides=None):
    state = _state(routes, **(state_overrides or {}))
    session = _Session(state, tmp_path / "project_state.json")
    renderer = renderer or _FakeRenderer()
    service = MapFigureService(
        session, lambda: dict(paths or {}), lambda action: action(),
        renderer_factory=lambda: renderer, project_directory=tmp_path,
    )
    return service, session, renderer


# ---- 1. 模板目录 ------------------------------------------------------------

def test_catalog_marks_only_route_overview_available():
    payload = catalog()
    assert payload["available_template_ids"] == ["route_overview_v1"]
    statuses = {item["template_id"]: item["status"] for item in payload["templates"]}
    assert statuses["route_overview_v1"] == "available"
    for template_id in (
        "route_detail_v1", "communication_layout_v1", "navigation_layout_v1",
        "surveillance_layout_v1", "cns_combined_v1",
    ):
        assert statuses[template_id] == "planned", template_id


def test_planned_templates_are_refused_instead_of_faked(tmp_path):
    service, _, _ = _service(tmp_path, routes=[ROUTE])
    with pytest.raises(MapFigureTemplateUnavailable) as error:
        service.build_figure(template_id="route_detail_v1", route_id="R0001")
    assert "尚未实现" in str(error.value)
    assert "planned" in str(error.value)


def test_service_catalog_exposes_route_selection_state(tmp_path):
    service, _, _ = _service(tmp_path, routes=[ROUTE])
    payload = service.catalog()
    assert payload["route_selection"]["count"] == 1
    assert payload["route_selection"]["auto_selectable"] is True
    assert payload["route_options"][0]["route_id"] == "R0001"


# ---- 2. 航路选择 ------------------------------------------------------------

def test_missing_route_reports_chinese_reason(tmp_path):
    service, _, _ = _service(tmp_path, routes=[])
    with pytest.raises(MapFigureRouteError) as error:
        service.build_figure(template_id="route_overview_v1")
    assert "还没有权威运行航路" in str(error.value)
    assert error.value.code == "map_figure_route_missing"


def test_unplottable_geometry_is_reported_not_used(tmp_path):
    broken = {**ROUTE, "path": [[122.05, 29.95]]}
    service, _, _ = _service(tmp_path, routes=[broken])
    assert service.route_options() == []
    assert service.available_routes()[0]["plottable"] is False
    with pytest.raises(MapFigureRouteError) as error:
        service.build_figure(template_id="route_overview_v1")
    assert error.value.code == "map_figure_route_geometry_incomplete"


def test_single_route_is_auto_selected(tmp_path):
    service, _, _ = _service(tmp_path, routes=[ROUTE])
    spec = service.build_figure(template_id="route_overview_v1")
    assert spec.route_id == "R0001"
    assert spec.route_source == "operational_routes"


def test_multiple_routes_require_explicit_selection(tmp_path):
    service, _, _ = _service(tmp_path, routes=[ROUTE, ROUTE_TWO])
    with pytest.raises(MapFigureRouteSelectionRequired) as error:
        service.build_figure(template_id="route_overview_v1")
    assert error.value.status == 409
    assert set(error.value.detail["route_ids"]) == {"R0001", "R0002"}
    explicit = service.build_figure(template_id="route_overview_v1", route_id="R0002")
    assert explicit.route_id == "R0002"


def test_unknown_route_id_is_rejected(tmp_path):
    service, _, _ = _service(tmp_path, routes=[ROUTE])
    with pytest.raises(MapFigureRouteError):
        service.build_figure(template_id="route_overview_v1", route_id="NOPE")


# ---- 3. FigureSpec 契约 ------------------------------------------------------

def test_figure_spec_is_json_safe_and_has_no_qgis_objects(tmp_path):
    service, _, _ = _service(tmp_path, routes=[ROUTE])
    spec = service.build_figure(template_id="route_overview_v1")
    payload = spec.to_dict()
    text = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    assert "Qgs" not in text
    for key in (
        "schema_version", "template_id", "template_version", "title", "route_id",
        "route_source", "extent", "extent_mode", "layers", "labels", "legend_items",
        "source_status", "omitted_layers", "generated_from_revision",
    ):
        assert key in payload, key
    assert payload["display_thresholds"]["semantics"] == "figure_display_threshold_only"
    assert payload["boundaries"]["presentation_only"] is True
    assert payload["boundaries"]["unknown_is_not_zero"] is True


def test_turn_points_exclude_endpoints_and_start_end_are_marked(tmp_path):
    service, _, _ = _service(tmp_path, routes=[ROUTE])
    spec = service.build_figure(template_id="route_overview_v1")
    assert spec.turn_points == [[122.20, 30.01]]
    layers = {layer.layer_key: layer for layer in spec.layers}
    assert layers["start_point"].data["points"][0]["longitude"] == pytest.approx(122.05)
    assert layers["end_point"].data["points"][0]["longitude"] == pytest.approx(122.35)
    assert layers["turn_point"].data["not_route_algorithm_turn_cost"] is True


def test_extent_uses_configurable_buffer_around_the_route(tmp_path):
    service, _, _ = _service(tmp_path, routes=[ROUTE])
    spec = service.build_figure(
        template_id="route_overview_v1", parameter_overrides={"extent_buffer_km": 20.0},
    )
    assert spec.extent.west < 122.05 and spec.extent.east > 122.35
    assert spec.extent.south < 29.95 and spec.extent.north > 30.01
    evidence = spec.extent_evidence
    assert evidence["buffer_km"] == 20.0
    assert evidence["mode"].startswith("route_bbox_plus_configurable_buffer")
    assert evidence["metric_crs"] == "EPSG:32651"
    # 版面长宽比已把范围扩展到不贴边：宽度/高度比接近地图框比例。
    aspect = evidence["aspect_ratio"]
    assert spec.extent.width_km / spec.extent.height_km == pytest.approx(aspect, rel=0.05)


def test_missing_sources_are_recorded_not_faked(tmp_path):
    service, _, _ = _service(tmp_path, routes=[ROUTE], paths={})
    spec = service.build_figure(template_id="route_overview_v1")
    keys = {item["layer_key"] for item in spec.omitted_layers}
    # 未配置数据源的图层必须被如实省略并写明原因。
    # 注意：``route_overview_v1`` **不再** materialize 适飞空域（产品要求图1不表达空域），
    # 因此 airspace 既不出现在图层里，也不出现在 omitted_layers 里。
    for layer_key in ("sea", "land", "terrain_obstacle", "building_obstacle",
                      "tower_existing", "airport", "airport_protection"):
        assert layer_key in keys, layer_key
    assert "airspace" not in {layer.layer_key for layer in spec.layers}
    assert "airspace" not in keys
    statuses = {item["layer_key"]: item["status"] for item in spec.omitted_layers}
    assert statuses["terrain_obstacle"] in ("unknown", "unavailable")
    assert statuses["airport"] == "unknown"
    reasons = {item["layer_key"]: item["reason"] for item in spec.omitted_layers}
    assert reasons["airport"]
    # 地理底图（海域 / 陆地）来自制图专用的 cartographic_land，且**不**是业务陆域掩膜。
    roles = {layer.layer_key: layer.source_role for layer in spec.layers}
    assert roles["sea"] == "cartographic_land"
    assert roles["land"] == "cartographic_land"
    assert "land_mask" not in set(roles.values())
    # 航路自身来自 canonical state，因此始终可用。
    assert spec.source_status["planned_route"]["status"] == "available"
    legend_keys = [item.layer_key for item in spec.legend_items]
    assert "airport" not in legend_keys
    assert legend_keys == ["planned_route", "turn_point", "start_point", "end_point"]


def test_legend_order_is_stable_semantic_order(tmp_path):
    service, _, _ = _service(tmp_path, routes=[ROUTE])
    spec = service.build_figure(template_id="route_overview_v1")
    order = [item.layer_key for item in spec.legend_items]
    assert order == ["planned_route", "turn_point", "start_point", "end_point"]


def test_unsupported_format_is_rejected(tmp_path):
    service, _, _ = _service(tmp_path, routes=[ROUTE])
    with pytest.raises(MapFigureFormatUnsupported):
        service.build_figure(template_id="route_overview_v1", format_name="pdf")


# ---- 4. 预览 / 导出 / 记录 ---------------------------------------------------

def test_preview_is_read_only_and_not_registered(tmp_path):
    service, session, renderer = _service(tmp_path, routes=[ROUTE])
    before = json.dumps(session.state, ensure_ascii=False, sort_keys=True)
    result = service.render_preview(template_id="route_overview_v1", route_id="R0001",
                                    width_px=800)
    assert result["image"].startswith(b"\x89PNG")
    assert result["record"] is None
    assert session.saved == 0
    assert json.dumps(session.state, ensure_ascii=False, sort_keys=True) == before
    assert MAP_FIGURE_COLLECTION not in session.state
    assert renderer.calls


def test_export_registers_record_and_reuses_same_figure_id(tmp_path):
    service, session, renderer = _service(tmp_path, routes=[ROUTE])
    saved_before = session.saved
    first = service.export(template_id="route_overview_v1", route_id="R0001")
    assert first["reused"] is False
    record = first["record"]
    assert record["project_revision"] == 7
    assert record["format"] == "png" and record["dpi"] == 300.0
    assert record["artifact_ref"]["relative_path"].startswith("artifacts/map_figures/")
    assert (tmp_path / record["relative_path"]).is_file()
    assert (tmp_path / record["spec_relative_path"]).is_file()
    # 图件记录写在受控索引里，**不**进入 ProjectState、也**不**推进业务 revision。
    assert session.saved == saved_before
    assert MAP_FIGURE_COLLECTION not in session.state
    assert "artifact_manifest" not in session.state
    assert service.records()["active_figure_id"] == first["figure_id"]
    assert service.store.index_path.is_file()
    calls_after_first = len(renderer.calls)
    second = service.export(template_id="route_overview_v1", route_id="R0001")
    assert second["reused"] is True
    assert second["figure_id"] == first["figure_id"]
    assert len(renderer.calls) == calls_after_first  # 复用不重新渲染
    assert session.saved == saved_before  # 重复导出同样不写 ProjectState


def test_records_are_revision_tagged_not_assumed_current(tmp_path):
    service, session, _ = _service(tmp_path, routes=[ROUTE])
    service.export(template_id="route_overview_v1", route_id="R0001")
    session.state["revision"] = 8
    service.export(template_id="route_overview_v1", route_id="R0001",
                   dpi=150)
    items = service.records()["items"]
    revisions = sorted(item["project_revision"] for item in items)
    assert revisions == [7, 8]
    assert all("spec_fingerprint" in item for item in items)


def test_artifact_read_returns_record_and_is_whitelisted(tmp_path):
    service, _, _ = _service(tmp_path, routes=[ROUTE])
    exported = service.export(template_id="route_overview_v1", route_id="R0001")
    artifact = service.artifact(exported["figure_id"])
    assert artifact["image"].startswith(b"\x89PNG")
    assert artifact["record"]["figure_id"] == exported["figure_id"]
    with pytest.raises(MapFigureNotFound):
        service.artifact("MF-" + "0" * 32)


def test_do_not_touch_operational_routes_on_publish(tmp_path):
    service, session, _ = _service(tmp_path, routes=[ROUTE])
    before = json.dumps(session.state["operational_routes"], ensure_ascii=False)
    service.export(template_id="route_overview_v1", route_id="R0001")
    assert json.dumps(session.state["operational_routes"], ensure_ascii=False) == before
    # 导出前后 ProjectState 的键集合与 revision 逐字节不变（图件记录在受控索引里）。
    assert set(session.state) - {"revision", "operational_routes", "source_audits",
                                "towers"} == set()


# ---- 5. 产物路径安全 --------------------------------------------------------

@pytest.mark.parametrize("figure_id", [
    "", "..", "../etc", "MF-zz", "MF-" + "0" * 31, "MF-" + "0" * 33,
    "artifacts/map_figures/MF-" + "0" * 32, "C:/windows/MF-" + "0" * 32,
], ids=["empty", "dotdot", "dotdot-slash", "short-hex", "31-hex", "33-hex",
        "nested-slash", "absolute-windows"])
def test_invalid_figure_ids_are_denied(tmp_path, figure_id):
    store = MapFigureStore(tmp_path)
    assert store.valid_figure_id(figure_id) is False
    with pytest.raises(MapFigurePathDenied):
        store.figure_directory(figure_id)


@pytest.mark.parametrize("name", [
    "", "../figure.png", "sub/figure.png", "sub\\figure.png", ".hidden",
], ids=["empty", "parent", "slash", "backslash", "hidden"])
def test_artifact_names_are_whitelisted(tmp_path, name):
    store = MapFigureStore(tmp_path)
    figure_id = "MF-" + "a" * 32
    with pytest.raises(MapFigurePathDenied):
        store.artifact_path(figure_id, name)


def test_store_paths_stay_inside_the_controlled_directory(tmp_path):
    store = MapFigureStore(tmp_path)
    figure_id = "MF-" + "b" * 32
    store.write(figure_id, image_bytes=b"\x89PNG", spec_payload={"a": 1}, record={"a": 1})
    path = store.artifact_path(figure_id, "figure.png")
    assert path.parent.parent == (tmp_path / "artifacts" / "map_figures").resolve()
    assert store.list_relative() == [figure_id]


def test_client_cannot_inject_output_paths(tmp_path):
    """Service 层不接受任何路径参数：图号与目录全部由服务端派生。"""

    service, _, _ = _service(tmp_path, routes=[ROUTE])
    exported = service.export(template_id="route_overview_v1", route_id="R0001")
    record = exported["record"]
    assert record["relative_path"].endswith("figure.png")
    assert ".." not in record["relative_path"]
    assert Path(record["relative_path"]).is_absolute() is False
    assert (tmp_path / record["relative_path"]).resolve().parent.parent == (
        tmp_path / "artifacts" / "map_figures"
    ).resolve()
