"""专题成果图 API 契约测试（transport 层，不接触 QGIS）。

覆盖：

* ``GET /api/map-figures/catalog`` 的模板状态与航路可选项；
* ``GET /api/map-figures/preview`` 在缺少航路时返回明确中文错误，而不是空图；
* ``POST /api/map-figures/export`` 走既有 token/revision 契约并写回记录；
* ``GET /api/map-figures/artifact`` 按记录白名单读回 PNG；
* **不存在**任何通用 QGIS 执行入口（script_path / python_code / output_path 一律无效）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cns_planner.api.router import ApiRouter
from cns_planner.application.map_figure_service import MapFigureService

ROUTE = {
    "route_id": "R0001", "status": "passed", "path_crs": "OGC:CRS84",
    "path": [[122.05, 29.95], [122.20, 30.01], [122.35, 29.98]],
    "start": {"name": "起点甲"}, "end": {"name": "终点乙"},
}


class _Session:
    def __init__(self, state, store_path):
        self.state = state
        self.store_path = store_path

    def save(self):
        pass


class _Renderer:
    def render(self, spec, *, dpi):
        return b"\x89PNG\r\n\x1a\n" + b"z" * 32


class _Workflow:
    """最小 workflow 替身：专题图分支只用 ``state``，其余调用为不可达的占位。"""

    def __init__(self, state):
        self.state = state

    def snapshot(self):
        return {"revision": self.state.get("revision")}

    def __getattr__(self, name):
        raise AssertionError(f"专题图 API 分支不应调用 workflow.{name}")


class _Data:
    paths = {}


class _Context:
    """ApiRouter 需要的最小上下文（专题图分支只读 ``map_figures``）。"""

    def __init__(self, service, state=None):
        self.map_figures = service
        self.token = "token"
        self.workflow = _Workflow(state if state is not None else {"revision": 3})
        self.data = _Data()
        self.static = Path(__file__).resolve().parent / "_no_static"


def _router(tmp_path, routes):
    state = {"revision": 3, "operational_routes": list(routes), "source_audits": {"items": {}},
             "towers": {"count": 0, "items": []}}
    service = MapFigureService(
        _Session(state, tmp_path / "project_state.json"), lambda: {},
        lambda action: action(), renderer_factory=lambda: _Renderer(),
        project_directory=tmp_path,
    )
    return ApiRouter(_Context(service, state)), state


def test_catalog_reports_available_and_planned_templates(tmp_path):
    router, _ = _router(tmp_path, [ROUTE])
    response = router.get("/api/map-figures/catalog", {}, {})
    assert response.status == 200
    payload = response.data
    assert payload["available_template_ids"] == ["route_overview_v1"]
    statuses = {item["template_id"]: item["status"] for item in payload["templates"]}
    assert statuses["cns_combined_v1"] == "planned"
    assert payload["route_options"][0]["route_id"] == "R0001"
    assert payload["route_selection"]["auto_selectable"] is True


def test_preview_returns_png_when_a_route_exists(tmp_path):
    router, _ = _router(tmp_path, [ROUTE])
    response = router.get(
        "/api/map-figures/preview",
        {"template": ["route_overview_v1"], "route_id": ["R0001"], "width": ["600"]}, {},
    )
    assert response.status == 200
    assert response.content_type == "image/png"
    assert bytes(response.data).startswith(b"\x89PNG")


def test_preview_without_route_is_a_clear_chinese_error(tmp_path):
    router, _ = _router(tmp_path, [])
    response = router.get("/api/map-figures/preview", {"template": ["route_overview_v1"]}, {})
    assert response.status == 400
    assert "运行航路" in response.data["error"]
    assert response.data["code"] == "map_figure_route_missing"


def test_preview_with_ambiguous_routes_requires_selection(tmp_path):
    second = {**ROUTE, "route_id": "R0002", "path": [[122.0, 29.9], [122.3, 30.05]]}
    router, _ = _router(tmp_path, [ROUTE, second])
    response = router.get("/api/map-figures/preview", {}, {})
    assert response.status == 409
    assert response.data["code"] == "map_figure_route_selection_required"
    assert sorted(response.data["detail"]["route_ids"]) == ["R0001", "R0002"]


def test_export_registers_record_and_artifact_reads_it_back(tmp_path):
    router, state = _router(tmp_path, [ROUTE])
    response = router.post("/api/map-figures/export", {
        "template_id": "route_overview_v1", "route_id": "R0001", "format": "png", "dpi": 300,
    })
    assert response.status == 200
    figure_id = response.data["figure_id"]
    assert response.data["record"]["project_revision"] == 3
    assert state["map_figures"]["active_figure_id"] == figure_id
    png = router.get("/api/map-figures/artifact", {"figure_id": [figure_id]}, {})
    assert png.status == 200 and bytes(png.data).startswith(b"\x89PNG")
    spec = router.get(
        "/api/map-figures/artifact", {"figure_id": [figure_id], "kind": ["spec"]}, {},
    )
    assert spec.status == 200
    payload = json.loads(bytes(spec.data).decode("utf-8"))
    assert payload["template_id"] == "route_overview_v1"
    assert payload["boundaries"]["presentation_only"] is True


def test_export_rejects_planned_template_and_bad_format(tmp_path):
    router, _ = _router(tmp_path, [ROUTE])
    planned = router.post("/api/map-figures/export", {
        "template_id": "route_detail_v1", "route_id": "R0001",
    })
    assert planned.status == 409
    assert planned.data["code"] == "map_figure_template_unavailable"
    bad_format = router.post("/api/map-figures/export", {
        "template_id": "route_overview_v1", "route_id": "R0001", "format": "pdf",
    })
    assert bad_format.status == 400
    assert bad_format.data["code"] == "map_figure_format_unsupported"


def test_artifact_rejects_invalid_figure_id(tmp_path):
    router, _ = _router(tmp_path, [ROUTE])
    for figure_id in ("../../etc/passwd", "MF-zz", ""):
        response = router.get("/api/map-figures/artifact", {"figure_id": [figure_id]}, {})
        assert response.status in (400, 404)
        assert "error" in response.data


def test_export_ignores_client_supplied_paths_and_scripts(tmp_path):
    """不存在通用 QGIS 执行接口：客户端提供的脚本/表达式/输出路径一律不生效。"""

    router, state = _router(tmp_path, [ROUTE])
    response = router.post("/api/map-figures/export", {
        "template_id": "route_overview_v1", "route_id": "R0001",
        "script_path": "C:/evil.py", "python_code": "import os",
        "qgis_expression": "1+1", "output_path": "C:/windows/system32/out.png",
    })
    assert response.status == 200
    record = response.data["record"]
    assert record["relative_path"].startswith("artifacts/map_figures/")
    assert "C:" not in record["relative_path"]
    assert "evil" not in json.dumps(response.data, ensure_ascii=False)
    # 受控目录之外不得出现任何新文件。
    stray = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert all("artifacts" in str(path).replace("\\", "/") for path in stray)


def test_map_figure_endpoints_are_not_a_generic_qgis_runner(tmp_path):
    """源码层面确认：不存在通用 QGIS 执行接口，也不接受脚本/任意输出路径参数。"""

    source = (Path(__file__).resolve().parents[1] / "cns_planner" / "api" / "router.py").read_text(
        encoding="utf-8",
    )
    for forbidden in ("/api/qgis/", "script_path", "python_code", "qgis_expression",
                      "output_path"):
        assert forbidden not in source, forbidden
    router, _ = _router(tmp_path, [ROUTE])
    for path in ("/api/map-figures/run", "/api/map-figures/exec", "/api/map-figures/evaluate"):
        assert router.get(path, {}, {}).status == 404
    # POST 的未知专题图操作也明确不存在（不是通用执行入口）。
    context = router.context
    context.workflow = _PermissiveWorkflow()
    assert ApiRouter(context).post("/api/map-figures/run", {"script_path": "x.py"}).status == 404


class _PermissiveWorkflow:
    """POST 分派会构造资源动作表，这里给一个不会命中任何动作的宽松替身。"""

    state = {"revision": 3}

    def __getattr__(self, name):
        def _unreachable(*args, **kwargs):
            raise AssertionError(f"未预期的 workflow 调用：{name}")

        return _unreachable


def test_preview_does_not_write_project_state(tmp_path):
    router, state = _router(tmp_path, [ROUTE])
    before = json.dumps(state, ensure_ascii=False, sort_keys=True)
    router.get("/api/map-figures/preview", {"route_id": ["R0001"], "width": ["500"]}, {})
    assert json.dumps(state, ensure_ascii=False, sort_keys=True) == before
    assert "map_figures" not in state


def test_missing_service_is_reported_not_silently_empty(tmp_path):
    router = ApiRouter(_Context(None))
    response = router.get("/api/map-figures/catalog", {}, {})
    assert response.status == 400
    assert response.data["code"] == "map_figure_disabled"
    assert "专题成果图" in response.data["error"]


@pytest.mark.parametrize("dpi", [None, 300, 150])
def test_export_dpi_is_bounded_and_recorded(tmp_path, dpi):
    router, _ = _router(tmp_path, [ROUTE])
    response = router.post("/api/map-figures/export", {
        "template_id": "route_overview_v1", "route_id": "R0001", "dpi": dpi,
    })
    assert response.status == 200
    assert response.data["record"]["dpi"] > 0
