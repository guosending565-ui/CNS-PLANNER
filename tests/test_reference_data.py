import json

import pytest
from openpyxl import Workbook

from cns_planner.api.router import ApiRouter
from cns_planner.api.file_browser import browse
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.data.health import build_health
from cns_planner.reference_data import (
    load_equipment_reference_catalog,
    load_reference_landing_sites,
    load_reference_routes,
    parse_coordinate,
)


def _defaults(tmp_path):
    target = tmp_path / "defaults.json"
    target.write_text(
        open("cns_planner/config/defaults.json", encoding="utf-8").read(),
        encoding="utf-8",
    )
    return target


def _xlsx(tmp_path):
    target = tmp_path / "landing-sites.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "舟山测试"
    sheet.append(["序号", "起降设施分类", "所属县区", "具体位置", "经纬度信息", "面积", "建成", "备注"])
    sheet.append([1, "起降点", "定海区", "明确点", "东经122°6′21.362″,北纬30°0′22.878″"])
    sheet.append([2, "临时起降点", "普陀区", "估算点", "估：g122°18'33.5556\",30°02'11.8644\""])
    sheet.append([3, "无人机起降点", "岱山县", "不确定点", "g122°20'01.2180\",30°30'59.6313\""])
    sheet.append([4, "起降点", "定海区", "无效点", "东经222°6′21″,北纬30°0′22″"])
    sheet.append([5, "起降点", "定海区", "近邻疑似重复", "E122°06′21.36″N30°00′22.88″"])
    workbook.save(target)
    return target


def test_coordinate_parser_preserves_dms_estimated_uncertain_and_invalid():
    parsed = parse_coordinate("东经122°6′21.362″,北纬30°0′22.878″")
    assert parsed["quality"] == "parsed"
    assert parsed["coordinate"] == [122.1059338889, 30.006355]

    estimated = parse_coordinate("估：g122°18'33.5556\",30°02'11.8644\"")
    assert estimated["quality"] == "estimated"
    assert "source_marks_coordinate_estimated" in estimated["warnings"]

    uncertain = parse_coordinate("g122°20'01.2180\",30°30'59.6313\"")
    assert uncertain["quality"] == "uncertain"
    assert "hemisphere_inferred_from_coordinate_order" in uncertain["warnings"]

    assert parse_coordinate("东经222°0′0″,北纬30°0′0″")["quality"] == "invalid"


def test_xlsx_import_keeps_raw_source_crs_and_marks_duplicates(tmp_path):
    source = _xlsx(tmp_path)
    first = load_reference_landing_sites(source)
    second = load_reference_landing_sites(source)

    assert first["status"] == "passed"
    assert first["count"] == 5
    assert first["metadata"]["crs_status"] == "pending_confirmation"
    assert first["metadata"]["quality_counts"] == {
        "parsed": 2, "estimated": 1, "uncertain": 1, "invalid": 1,
    }
    assert first["items"][0]["raw"]["column_5"].startswith("东经")
    assert first["items"][0]["source"] == {
        "file": source.name, "sheet": "舟山测试", "row": 2,
    }
    assert first["items"][0]["reference_site_id"] == second["items"][0]["reference_site_id"]
    assert first["items"][0]["possible_duplicate"] is True
    assert first["items"][4]["possible_duplicate"] is True
    assert first["count"] == len(first["items"]), "疑似重复不得静默合并"


def test_et_is_detected_and_requires_conversion(tmp_path):
    source = tmp_path / "legacy.et"
    source.write_text("converted file required", encoding="utf-8")
    result = load_reference_landing_sites(source)
    assert result["status"] == "requires_xlsx_or_csv_conversion"
    assert result["warnings"] == ["requires_xlsx_or_csv_conversion"]
    assert result["items"] == []
    assert load_reference_routes(source)["status"] == "requires_xlsx_or_csv_conversion"


def test_reference_source_center_reports_counts_and_et_reason(tmp_path):
    landing = _xlsx(tmp_path)
    route = tmp_path / "routes.et"
    route.write_text("conversion required", encoding="utf-8")
    metadata = {
        "paths": {"reference_landing_sites": str(landing), "reference_routes": str(route)},
        "workflow": {
            "reference_landing_sites": load_reference_landing_sites(landing),
            "reference_routes": load_reference_routes(route),
        },
        "layers": [],
    }
    health = {item["id"]: item for item in build_health(metadata)["items"]}
    assert health["reference_landing_sites"]["status"] == "ready"
    assert "5 条" in health["reference_landing_sites"]["message"]
    assert health["reference_routes"]["status"] == "warning"
    assert "requires_xlsx_or_csv_conversion" in health["reference_routes"]["message"]
    assert "routes.et" in {entry["name"] for entry in browse(tmp_path, "reference_routes")["entries"] if not entry["directory"]}


def test_reference_source_must_be_a_concrete_file(tmp_path):
    with pytest.raises(ValueError, match="具体文件"):
        load_reference_landing_sites(tmp_path)
    with pytest.raises(ValueError, match="具体文件"):
        load_reference_routes(tmp_path)


def test_landing_site_id_survives_source_row_insert_and_reorder(tmp_path):
    first_path = _xlsx(tmp_path)
    first = load_reference_landing_sites(first_path)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "舟山测试"
    sheet.append(["说明", "插入的非数据行"])
    sheet.append(["序号", "起降设施分类", "所属县区", "具体位置", "经纬度信息"])
    source_items = list(reversed(first["items"]))
    for index, item in enumerate(source_items, 1):
        sheet.append([index, item["site_type"], item["region"], item["name"], item["coordinate_raw"]])
    reordered = tmp_path / "reordered.xlsx"
    workbook.save(reordered)
    second = load_reference_landing_sites(reordered)
    assert {item["name"]: item["reference_site_id"] for item in first["items"]} == {
        item["name"]: item["reference_site_id"] for item in second["items"]
    }


def test_old_row_based_reference_site_node_is_migrated_on_reimport(tmp_path):
    source = _xlsx(tmp_path)
    imported = load_reference_landing_sites(source)
    site = imported["items"][0]
    service = WorkflowService(tmp_path / "legacy-project.json", _defaults(tmp_path))
    legacy = dict(site)
    legacy["reference_site_id"] = "RLS-LEGACY-ROW-2"
    service.state["reference_landing_sites"] = {**imported, "items": [legacy]}
    service.state["nodes"] = [{
        "node_id": "N001", "name": site["name"], "coordinate": site["coordinate"],
        "reference_site_id": legacy["reference_site_id"],
        "provenance": {"reference_site_id": legacy["reference_site_id"]},
    }]
    snapshot = service.import_reference_landing_sites(source)
    node = snapshot["nodes"][0]
    assert node["reference_site_id"] == site["reference_site_id"]
    assert node["provenance"]["legacy_reference_site_ids"] == ["RLS-LEGACY-ROW-2"]


def test_reference_routes_group_and_sort_all_intermediate_points_without_planning_side_effects(tmp_path):
    source = tmp_path / "舟山16条航线点位核对表.csv"
    source.write_text(
        "航线编号,航线名称,航线类别,点序号,点位名称,点位类型,经度,纬度\n"
        "16,海岛线,验证,3,终点,起降点,122.3,30.3\n"
        "16,海岛线,验证,1,起点,起降点,122.1,30.1\n"
        "16,海岛线,验证,2,中间点,航路点,122.2,30.2\n"
        "2,短线,运输,2,B,起降点,122.5,30.5\n"
        "2,短线,运输,1,A,起降点,122.4,30.4\n",
        encoding="utf-8-sig",
    )
    first = load_reference_routes(source)
    reordered = tmp_path / "reordered.csv"
    lines = source.read_text(encoding="utf-8-sig").splitlines()
    reordered.write_text("\n".join([lines[0], *reversed(lines[1:])]), encoding="utf-8-sig")
    second = load_reference_routes(reordered)
    route = next(item for item in first["items"] if item["route_number"] == "16")
    points = [item for item in first["points"] if item["route_id"] == route["reference_route_id"]]
    assert [item["sequence"] for item in points] == [1, 2, 3]
    assert [item["position"] for item in points] == ["endpoint", "intermediate", "endpoint"]
    assert route["path"] == [[122.1, 30.1], [122.2, 30.2], [122.3, 30.3]]
    assert {item["reference_route_id"] for item in first["items"]} == {
        item["reference_route_id"] for item in second["items"]
    }
    assert {item["reference_route_point_id"] for item in first["points"]} == {
        item["reference_route_point_id"] for item in second["points"]
    }
    service = WorkflowService(tmp_path / "route-project.json", _defaults(tmp_path))
    snapshot = service.import_reference_routes(source)
    assert snapshot["reference_routes"]["count"] == 2
    assert snapshot["reference_routes"]["point_count"] == 5
    assert snapshot["nodes"] == []
    assert snapshot["scenario_routes"] == [] and snapshot["operational_routes"] == []


def test_zhoushan_sixteen_route_shape_restores_by_number_and_sequence(tmp_path):
    source = tmp_path / "舟山16条航线点位核对表.csv"
    rows = ["航线编号,航线名称,点序号,航点名称,经度(E),纬度(N)"]
    for route_number in range(1, 17):
        for sequence in (3, 1, 2):
            rows.append(
                f"{route_number},航线{route_number},{sequence},R{route_number}-P{sequence},"
                f"{122 + route_number / 100 + sequence / 1000},{30 + sequence / 1000}"
            )
    source.write_text("\n".join(rows), encoding="utf-8-sig")
    result = load_reference_routes(source)
    assert result["count"] == 16
    assert result["point_count"] == 48
    assert all(len(route["path"]) == 3 for route in result["items"])
    for route in result["items"]:
        route_points = [point for point in result["points"] if point["route_id"] == route["reference_route_id"]]
        assert [point["sequence"] for point in route_points] == [1, 2, 3]
        assert route_points[1]["position"] == "intermediate"


def test_reference_site_only_becomes_node_after_explicit_add_and_manual_shape_stays(tmp_path):
    service = WorkflowService(tmp_path / "project.json", _defaults(tmp_path))
    service.set_workspace(
        [121.9, 29.9, 122.5, 30.6],
        {"status": "passed", "population": {"status": "passed"}, "airspace": {"status": "passed"}},
    )
    imported = service.import_reference_landing_sites(_xlsx(tmp_path))
    assert imported["nodes"] == []
    site = next(item for item in imported["reference_landing_sites"]["items"] if item["quality"] == "parsed")

    added = service.add_reference_landing_site(site["reference_site_id"])
    assert len(added["nodes"]) == 1
    assert added["nodes"][0]["reference_site_id"] == site["reference_site_id"]
    assert added["nodes"][0]["provenance"]["crs_status"] == "pending_confirmation"
    assert added["nodes"][0]["provenance"]["source"]["row"] == 2
    assert len(service.add_reference_landing_site(site["reference_site_id"])["nodes"]) == 1

    manual = service.add_node([122.2, 30.2], "手工点")["nodes"][-1]
    assert manual == {"node_id": "N002", "name": "手工点", "coordinate": [122.2, 30.2]}


def test_equipment_reference_allows_missing_facts_and_never_maps_to_device_catalog(tmp_path):
    catalog = load_equipment_reference_catalog()
    assert catalog["status"] == "passed"
    assert catalog["count"] >= 25
    assert all(item["planning_mapping"]["status"] != "mapped" for item in catalog["items"])
    assert all("radius_m" not in item and "cost" not in item for item in catalog["items"])
    management = next(item for item in catalog["items"] if item["equipment_id"] == "EQREF-TA-VS-0ZHGL02-M")
    assert management["performance"] == {}
    assert management["reliability"] == {}
    five_ga = next(item for item in catalog["items"] if item["equipment_id"] == "EQREF-5GA-INTEGRATED-01")
    assert five_ga["manufacturer"] is None
    assert five_ga["manufacturer_status"] == "pending_confirmation"
    assert "54" not in json.dumps(five_ga, ensure_ascii=False)

    service = WorkflowService(tmp_path / "project.json", _defaults(tmp_path))
    snapshot = service.snapshot()
    assert snapshot["equipment_reference_catalog"]["count"] == catalog["count"]
    assert snapshot["device_catalog"]["catalog_id"] == "cns-device-catalog"
    assert {item.get("device_id") for item in snapshot["device_catalog"]["items"]}.isdisjoint(
        {item["equipment_id"] for item in catalog["items"]}
    )


class _ApiData:
    paths = {}


class _ApiContext:
    def __init__(self, workflow):
        self.workflow = workflow
        self.data = _ApiData()


def test_reference_data_minimal_api(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", _defaults(tmp_path))
    router = ApiRouter(_ApiContext(workflow))
    assert router.get("/api/reference-landing-sites", {}, {}).status == 200
    assert router.get("/api/reference-routes", {}, {}).status == 200
    assert router.get("/api/airspace-policies", {}, {}).data["status"] == "pending_confirmation"
    assert router.get("/api/equipment-reference-catalog", {}, {}).data["count"] >= 25
    response = router.post("/api/reference-landing-sites/import", {"path": str(_xlsx(tmp_path))})
    assert response.data["reference_landing_sites"]["count"] == 5
    route_source = tmp_path / "routes.csv"
    route_source.write_text(
        "航线编号,点序号,航点名称,经度,纬度\n1,1,A,122.1,30.1\n1,2,B,122.2,30.2\n",
        encoding="utf-8-sig",
    )
    response = router.post("/api/reference-routes/preview", {"path": str(route_source)})
    preview_id = response.data["reference_route_import_preview"]["preview_id"]
    response = router.post("/api/reference-routes/import", {"path": str(route_source), "preview_id": preview_id})
    assert response.data["reference_routes"]["count"] == 1
    response = router.post("/api/airspace-policies", {"items": [{
        "feature_id": "ASF-1", "route_eligibility": "unknown",
        "confirmed": False, "source": "test", "evidence": [{"type": "test"}],
    }]})
    assert response.data["airspace_policies"]["count"] == 1
