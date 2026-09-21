"""BUG-DATA-001 / BUG-DATA-002 回归：数据源与真实参考航线的恢复。

覆盖三条链路：

1. 舟山真实航线 xlsx 解析 → 47 条航线 / 175 个航路点 / EPSG:4490（源文件自带
   ``crs_confirmed=true``）。
2. 导入成功后立即生成正式 ``reference_routes`` 业务航线对象，而不是只留下预览。
3. 项目重新打开（``configure_reference_sources``）时按已保存的数据源自动恢复，
   不再要求用户重新配置或重新确认坐标系。

真实文件不在本机时相关用例 ``skip``；别名与坐标系声明的行为另用合成文件覆盖，
因此任何机器都能运行。
"""

import json
import os
from pathlib import Path

import pytest
from openpyxl import Workbook

from cns_planner.application.workflow_service import WorkflowService
from cns_planner.reference_data import declared_source_crs, load_reference_routes


#: 人工验收使用的真实文件；可用环境变量指向其它副本。
REAL_ROUTES = Path(os.environ.get(
    "CNS_ZHONGSHAN_REFERENCE_ROUTES",
    r"D:\aaa2026project\UOM\舟山\基础数据\空域航线起降点\空域航线起降点"
    r"\舟山真实航线_CNS最终导入.xlsx",
))
REAL_ROUTE_COUNT = 47
REAL_POINT_COUNT = 175
REAL_SOURCE_CRS = "EPSG:4490"


def real_routes():
    if not REAL_ROUTES.is_file():
        pytest.skip(f"舟山真实航线 xlsx 不在本机：{REAL_ROUTES}")
    return REAL_ROUTES


def defaults(tmp_path):
    target = tmp_path / "defaults.json"
    target.write_text(
        Path("cns_planner/config/defaults.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return target


def service(tmp_path, name="project.json"):
    return WorkflowService(tmp_path / name, defaults(tmp_path))


def source_crs(collection):
    return ((collection.get("crs") or {}).get("source_crs") or {})


def _rounded(path):
    """坐标比较前收敛浮点误差（解析过程不做任何取整）。"""

    return [[round(point[0], 6), round(point[1], 6)] for point in path]


def alias_csv(tmp_path, *, crs=REAL_SOURCE_CRS, confirmed="true"):
    """同表头结构的合成源文件：route_id / point_order / lon / lat。"""

    target = tmp_path / f"alias-{confirmed}.csv"
    rows = ["route_id,route_name,point_order,lon,lat,source_crs,crs_confirmed"]
    for route in ("R-1", "R-2"):
        for order in (2, 1):
            rows.append(
                f"{route},航线{route},{order},"
                f"{122.1 + order / 100},{30.1 + order / 100},{crs or ''},{confirmed}"
            )
    target.write_text("\n".join(rows) + "\n", encoding="utf-8-sig")
    return target


# --------------------------------------------------------------------------- 真实文件


def test_zhoushan_real_xlsx_parses_to_47_routes_and_175_points():
    parsed = load_reference_routes(real_routes())

    assert parsed["status"] == "passed"
    assert parsed["count"] == REAL_ROUTE_COUNT
    assert parsed["point_count"] == REAL_POINT_COUNT
    assert parsed["metadata"]["invalid_row_count"] == 0
    assert len(parsed["items"]) == REAL_ROUTE_COUNT
    assert sum(len(route["path"]) for route in parsed["items"]) == REAL_POINT_COUNT


def test_zhoushan_real_xlsx_declares_confirmed_source_crs():
    parsed = load_reference_routes(real_routes())
    declaration = parsed["declared_source_crs"]

    assert declaration["value"] == REAL_SOURCE_CRS
    assert declaration["confirmed"] is True
    assert declared_source_crs(real_routes()) == declaration
    assert parsed["data_source"]["crs"] == REAL_SOURCE_CRS
    assert parsed["data_source"]["crs_confirmed"] is True
    # 解析本身绝不把声明升级成已确认记录。
    assert (parsed["crs"]["source_crs"]["value"], parsed["crs"]["source_crs"]["confirmed"]) == (None, False)


def test_zhoushan_real_xlsx_import_creates_formal_reference_routes_not_only_preview(tmp_path):
    pytest.importorskip("pyproj")
    project = service(tmp_path)

    snapshot = project.preview_reference_routes(real_routes())

    routes = snapshot["reference_routes"]
    assert routes["count"] == REAL_ROUTE_COUNT
    assert routes["point_count"] == REAL_POINT_COUNT
    assert source_crs(routes)["value"] == REAL_SOURCE_CRS
    assert source_crs(routes)["confirmed"] is True
    assert all(route["length_m"] is not None for route in routes["items"])
    # 正式对象已生成，项目里不再残留孤立预览。
    assert snapshot["reference_route_import_preview"] is None
    # 数据源身份与导入状态随项目保存。
    state = routes["data_source"]
    assert state["path"] == str(real_routes())
    assert state["imported"] is True
    assert state["crs"] == REAL_SOURCE_CRS
    assert state["crs_confirmed"] is True
    assert state["route_count"] == REAL_ROUTE_COUNT
    assert state["point_count"] == REAL_POINT_COUNT


def test_saved_project_reopens_with_reference_routes_and_source_state(tmp_path):
    pytest.importorskip("pyproj")
    source = real_routes()
    project = service(tmp_path, "saved.json")
    project.preview_reference_routes(source)
    project.save()

    reopened = WorkflowService(project.store_path, defaults(tmp_path))

    routes = reopened.state["reference_routes"]
    assert routes["count"] == REAL_ROUTE_COUNT
    assert routes["point_count"] == REAL_POINT_COUNT
    assert source_crs(routes)["value"] == REAL_SOURCE_CRS
    assert source_crs(routes)["confirmed"] is True
    assert routes["data_source"]["imported"] is True
    assert routes["data_source"]["path"] == str(source)


def test_opening_project_restores_reference_routes_from_saved_source(tmp_path):
    pytest.importorskip("pyproj")
    source = real_routes()
    # 人工验收现场：项目里只有一份待确认预览，没有任何正式业务航线。
    project = service(tmp_path)
    project.state["reference_route_import_preview"] = {
        "status": "ready_for_confirmation", "preview_id": "RIP-LEGACY",
        "route_count": REAL_ROUTE_COUNT, "point_count": REAL_POINT_COUNT,
    }

    snapshot = project.configure_reference_sources({"reference_routes": source})

    routes = snapshot["reference_routes"]
    assert routes["count"] == REAL_ROUTE_COUNT
    assert routes["point_count"] == REAL_POINT_COUNT
    assert source_crs(routes)["value"] == REAL_SOURCE_CRS
    assert source_crs(routes)["confirmed"] is True
    assert routes["data_source"]["imported_from"] == "project_open_restore"
    # 恢复是幂等的：再次打开同一个项目不会改变结果。
    again = project.configure_reference_sources({"reference_routes": source})["reference_routes"]
    assert again["count"] == REAL_ROUTE_COUNT
    assert source_crs(again)["confirmed"] is True


def test_restore_never_replaces_existing_reference_routes(tmp_path):
    source = real_routes()
    project = service(tmp_path)
    existing = load_reference_routes(source)
    existing["data_source"] = {**existing["data_source"], "imported": True}
    project.state["reference_routes"] = existing
    before = json.loads(json.dumps(project.state["reference_routes"], ensure_ascii=False, default=str))

    project.configure_reference_sources({"reference_routes": source})

    assert project.state["reference_routes"]["count"] == before["count"]
    assert project.state["reference_routes"]["items"][0]["reference_route_id"] == before["items"][0]["reference_route_id"]


# --------------------------------------------------------------------------- 别名与声明行为


def test_route_aliases_accept_route_id_point_order_lon_lat(tmp_path):
    parsed = load_reference_routes(alias_csv(tmp_path))

    assert parsed["count"] == 2
    assert parsed["point_count"] == 4
    assert {route["route_number"] for route in parsed["items"]} == {"R-1", "R-2"}
    first = next(route for route in parsed["items"] if route["route_number"] == "R-1")
    assert [point["sequence"] for point in first["ordered_points"]] == [1, 2]
    assert _rounded(first["path"]) == [[122.11, 30.11], [122.12, 30.12]]


def test_xlsx_route_aliases_are_detected_from_header(tmp_path):
    target = tmp_path / "alias.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "routes"
    sheet.append(["route_id", "point_order", "longitude", "latitude"])
    for order in (1, 2, 3):
        sheet.append(["R-9", order, 122 + order / 100, 30 + order / 100])
    workbook.save(target)

    parsed = load_reference_routes(target)

    assert parsed["count"] == 1
    assert parsed["point_count"] == 3
    assert _rounded(parsed["items"][0]["path"]) == [[122.01, 30.01], [122.02, 30.02], [122.03, 30.03]]


def test_unconfirmed_source_declaration_keeps_preview_and_pending_crs(tmp_path):
    pytest.importorskip("pyproj")
    project = service(tmp_path)

    snapshot = project.preview_reference_routes(alias_csv(tmp_path, confirmed="false"))

    assert snapshot["reference_routes"]["count"] == 0
    preview = snapshot["reference_route_import_preview"]
    assert preview["status"] == "ready_for_confirmation"
    assert preview["declared_source_crs"]["value"] == REAL_SOURCE_CRS
    assert preview["declared_source_crs"]["confirmed"] is False


def test_invalid_declared_crs_keeps_routes_without_confirming(tmp_path):
    pytest.importorskip("pyproj")
    project = service(tmp_path)

    snapshot = project.preview_reference_routes(alias_csv(tmp_path, crs="NOT-A-CRS", confirmed="true"))

    routes = snapshot["reference_routes"]
    # 航线本身仍然可用；只有坐标系保持未确认并留下显式警告。
    assert routes["count"] == 2
    assert source_crs(routes)["confirmed"] is False
    assert "declared_crs_auto_confirm_failed" in routes["warnings"]
    assert routes["metadata"]["declared_crs_auto_confirm_error"]


def test_project_without_saved_data_source_is_left_untouched(tmp_path):
    """既没有数据源路径、也没有保存来源的旧项目保持原样。"""

    project = service(tmp_path)
    assert project.state["reference_routes"]["data_source"] is None

    snapshot = project.configure_reference_sources({})

    assert snapshot["reference_routes"]["count"] == 0
    assert snapshot["reference_route_import_preview"] is None


def test_configure_without_source_path_falls_back_to_saved_data_source(tmp_path):
    """数据源路径丢失（自动项目）时，用项目状态里保存的来源恢复业务航线。"""

    pytest.importorskip("pyproj")
    source = real_routes()
    project = service(tmp_path)
    # 人工验收现场：路径只在项目状态里，数据源配置里没有该条目。
    project.state["reference_routes"] = {
        "status": "not_calculated", "collection_id": "reference-routes",
        "count": 0, "point_count": 0, "items": [], "points": [],
        "declared_source_crs": None,
        "data_source": {
            "path": str(source), "file_name": source.name, "format": "xlsx",
            "crs": REAL_SOURCE_CRS, "crs_confirmed": True, "imported": False,
            "route_count": 0, "point_count": 0,
        },
    }

    routes = project.configure_reference_sources({})["reference_routes"]

    assert routes["count"] == REAL_ROUTE_COUNT
    assert routes["point_count"] == REAL_POINT_COUNT
    assert source_crs(routes)["value"] == REAL_SOURCE_CRS
    assert source_crs(routes)["confirmed"] is True
    assert routes["data_source"]["imported_from"] == "project_open_restore"


def test_explicit_empty_source_path_is_not_reverted(tmp_path):
    """显式清空数据源时不回退到项目状态里的旧路径。"""

    project = service(tmp_path)
    project.state["reference_routes"] = {
        **project.state["reference_routes"],
        "data_source": {"path": "D:/not-configured/routes.xlsx", "imported": False},
    }

    snapshot = project.configure_reference_sources({"reference_routes": ""})

    assert snapshot["reference_routes"]["count"] == 0
    assert snapshot["source_audits"]["items"].get("reference_routes") is None


def test_plain_source_without_declaration_still_requires_confirmation(tmp_path):
    source = tmp_path / "plain.csv"
    source.write_text(
        "航线编号,点序号,经度,纬度\n1,1,122.1,30.1\n1,2,122.2,30.2\n",
        encoding="utf-8-sig",
    )
    project = service(tmp_path)

    # 既有契约：配置一个数据源文件本身永不导入，仍需用户显式预览 + 确认。
    configured = project.configure_reference_sources({"reference_routes": source})
    assert configured["reference_routes"]["count"] == 0
    assert configured["reference_route_import_preview"] is None

    snapshot = project.preview_reference_routes(source)

    assert snapshot["reference_routes"]["count"] == 0
    assert snapshot["reference_route_import_preview"]["status"] == "ready_for_confirmation"
    confirmed = project.confirm_reference_routes_import(
        source, snapshot["reference_route_import_preview"]["preview_id"],
    )
    assert confirmed["reference_routes"]["count"] == 1
    assert source_crs(confirmed["reference_routes"])["confirmed"] is False
