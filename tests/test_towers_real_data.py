"""真实通信铁塔站址（CNS 站址参考）接入契约。

本阶段铁塔的定位是**只读参考数据**：

* 它不进入 ``grid_attributes``，因此不进入 Risk V1/V2、planning exposure、
  Theta* V2 objective 或任何失效链取值计算；
* 它不生成 coverage，也不把站址转成 C/N/S existing site / candidate site；
* 位置与通信性能严格分离——源文件没有的字段一律为 ``None``。

以下测试同时锁定了这三条边界。
"""

import inspect
import json
from pathlib import Path

import pytest
from openpyxl import Workbook

from cns_planner.api.router import ApiRouter
from cns_planner.application.project_state import empty_grid_attributes
from cns_planner.application.reference_data_service import TOWERS_CONFIRMED_SOURCE_CRS
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.risk_v2 import FACTOR_DEFINITIONS, FACTOR_INPUT_ATTRIBUTES
from cns_planner.domain.layered_theta_v2 import (
    USER_DEFINED_BASELINE_WEIGHTS, default_theta_v2_objective_policy,
)
from cns_planner.domain.towers import (
    FORBIDDEN_PERFORMANCE_FIELDS, validate_coordinate,
)
from cns_planner.reference_data.towers import load_towers

DEFAULTS = Path("cns_planner/config/defaults.json")

#: 本批真实报送数据（仓库外）——存在时才跑真实文件断言。
REAL_XLSX = Path(
    r"D:\aaa2026project\UOM\舟山\基础数据\各单位报送的补充材料\各单位报送的补充材料"
    r"\航路航线规划-铁塔数据.xlsx"
)
REAL_SHA256 = "d722c111596acb4766bcc9d4cb5ce326f3a4b1f8364a5db871cb60220e2a411b"

HEADER = ["所属站址编码", "站址名称", "区域", "经度", "纬度", "铁塔细分类型", "海拔高度(m)", "塔身高度(m)"]


def workflow(tmp_path):
    return WorkflowService(tmp_path / "project.json", DEFAULTS)


def tower_xlsx(tmp_path, rows, name="towers.xlsx"):
    target = tmp_path / name
    book = Workbook()
    sheet = book.active
    sheet.title = "站点清单"
    sheet.append(HEADER)
    for row in rows:
        sheet.append(row)
    book.save(target)
    return target


def sample_rows():
    return [
        ["330903908000000816", "普陀登步", "A1", 122.295361, 29.879831, "角钢塔", 20, 25],
        ["330903908000000515", "普陀桃花盐厂", "A1、A4", 122.258063, 29.844537, "落地拉线塔", 20, 15],
    ]


class _ApiData:
    paths = {}


class _ApiContext:
    def __init__(self, workflow):
        self.workflow = workflow
        self.data = _ApiData()


# --------------------------------------------------------------- 1. parser

def test_xlsx_parser_maps_real_chinese_headers_and_keeps_source_rows(tmp_path):
    record = load_towers(tower_xlsx(tmp_path, sample_rows()))
    assert record["status"] == "passed"
    assert record["collection_id"] == "towers"
    assert record["count"] == 2
    first = record["items"][0]
    assert first["tower_id"] == "330903908000000816"
    assert first["name"] == "普陀登步"
    assert first["district"] == "A1"
    assert first["site_type"] == "角钢塔"
    assert first["elevation_m"] == 20.0
    assert first["height_m"] == 25.0
    # 源文件中不存在的列绝不杜撰
    assert first["operator"] is None
    assert first["address"] is None
    assert first["remarks"] is None
    # 每条记录必须能追溯回原始 sheet 与行号
    assert first["source"]["sheet"] == "站点清单"
    assert first["source"]["row"] == 2
    assert record["items"][1]["source"]["row"] == 3
    assert first["evidence"][0]["type"] == "file_row"


def test_csv_roundtrip_is_supported_without_geojson_crs_inference(tmp_path):
    source = tmp_path / "towers.csv"
    source.write_text(
        "站址编码,站址名称,经度,纬度\nT-1,甲站,122.1,30.1\n",
        encoding="utf-8-sig",
    )
    record = load_towers(source)
    assert record["count"] == 1
    assert record["crs"]["source_crs"]["status"] == "pending_confirmation"
    assert record["crs"]["source_crs"]["confirmed"] is False


# --------------------------------------------------------------- 2. CRS

def test_pending_crs_is_never_upgraded_by_parsing_alone(tmp_path):
    record = load_towers(tower_xlsx(tmp_path, sample_rows()))
    source_crs = record["crs"]["source_crs"]
    assert source_crs["value"] is None
    assert source_crs["status"] == "pending_confirmation"
    assert source_crs["confirmed"] is False


def test_human_confirmed_epsg4490_is_recorded_with_evidence(tmp_path):
    record = load_towers(
        tower_xlsx(tmp_path, sample_rows()),
        source_crs="EPSG:4490", crs_confirmed=True,
        crs_evidence=[{"type": "user_supplied", "note": "人工裁定"}],
    )
    source_crs = record["crs"]["source_crs"]
    assert source_crs["value"] == "EPSG:4490"
    assert source_crs["status"] == "confirmed"
    assert source_crs["confirmed"] is True
    assert source_crs["axis_order"] == "lon_lat"
    assert source_crs["evidence"][0]["note"] == "人工裁定"


# --------------------------------------------------------------- 3/4. 计数与重复

def test_record_count_and_near_duplicate_reporting_without_merging(tmp_path):
    rows = sample_rows() + [
        # 与第一行几乎同址（约 1 m 内），但站址编码不同 → 只报告，不合并
        ["330903908000000817", "普陀登步-2", "A1", 122.295362, 29.879832, "单管塔", 20, 30],
    ]
    record = load_towers(tower_xlsx(tmp_path, rows))
    assert record["count"] == 3
    groups = record["metadata"]["duplicate_coordinate_groups"]
    assert len(groups) == 1
    assert groups[0]["count"] == 2
    assert set(groups[0]["tower_ids"]) == {"330903908000000816", "330903908000000817"}
    # 三条记录都保留：绝不因为位置接近就合并物理站址
    assert len(record["items"]) == 3
    assert "near_duplicate_coordinate_groups:1" in record["warnings"]


def test_duplicate_tower_id_fails_closed(tmp_path):
    rows = [sample_rows()[0], sample_rows()[0]]
    with pytest.raises(ValueError, match="重复"):
        load_towers(tower_xlsx(tmp_path, rows))


# --------------------------------------------------------------- 5. 非法坐标

def test_invalid_coordinates_fail_closed(tmp_path):
    assert validate_coordinate([122.1, 30.1])["valid"] is True
    assert validate_coordinate([200.0, 30.1])["reason"] == "longitude_out_of_range"
    assert validate_coordinate([122.1, 95.0])["reason"] == "latitude_out_of_range"
    assert validate_coordinate([0.0, 0.0])["reason"] == "null_island_coordinate"

    rows = sample_rows() + [["T-BAD", "越界站", "A1", 999.0, 30.1, "角钢塔", 10, 10]]
    record = load_towers(tower_xlsx(tmp_path, rows))
    assert record["count"] == 2
    assert record["skipped"] == [{"sheet": "站点清单", "row": 4, "reason": "longitude_out_of_range"}]
    assert "invalid_rows_skipped:1" in record["warnings"]


def test_all_invalid_rows_raise_instead_of_returning_an_empty_collection(tmp_path):
    rows = [["T-BAD", "越界站", "A1", 999.0, 30.1, "角钢塔", 10, 10]]
    with pytest.raises(ValueError):
        load_towers(tower_xlsx(tmp_path, rows))


# --------------------------------------------------------------- 6. source audit

def test_import_towers_writes_verified_audit_with_crs_and_permissions(tmp_path):
    source = tower_xlsx(tmp_path, sample_rows())
    service = workflow(tmp_path)
    service.import_towers(source)
    audit = service.state["source_audits"]["items"]["towers"]
    assert audit["status"] == "verified"
    assert audit["sha256"] and len(audit["sha256"]) == 64
    assert audit["confirmed_crs"] == TOWERS_CONFIRMED_SOURCE_CRS == "EPSG:4490"
    assert audit["row_count"] == 2
    assert audit["feature_count"] == 2
    assert audit["geometry_health"]["status"] == "passed"
    assert audit["extent"] == [122.258063, 29.844537, 122.295361, 29.879831]
    evidence = {item["type"]: item["value"] for item in audit["evidence"]}
    assert evidence["crs_confirmation"] == "EPSG:4490"
    assert evidence["usage_permission"] == "confirmed"
    assert evidence["publication_permission"] == "confirmed"
    assert evidence["classification"] == "non_sensitive"
    assert audit["provenance"]["method"] == "read_only_table_parse_with_human_confirmed_source_crs"
    # 稳定身份不含绝对路径
    assert str(tmp_path) not in json.dumps(audit, ensure_ascii=False)


def test_import_towers_does_not_write_grid_attributes(tmp_path):
    service = workflow(tmp_path)
    service.import_towers(tower_xlsx(tmp_path, sample_rows()))
    assert service.state["towers"]["count"] == 2
    assert service.state["grid_attributes"]["towers"] == empty_grid_attributes()["towers"]


# --------------------------------------------------------------- 7. save / reopen

def test_towers_survive_save_and_reopen(tmp_path):
    source = tower_xlsx(tmp_path, sample_rows())
    project = tmp_path / "project.json"
    service = WorkflowService(project, DEFAULTS)
    service.import_towers(source)
    reopened = WorkflowService(project, DEFAULTS)
    assert reopened.state["towers"]["count"] == 2
    assert reopened.state["towers"]["items"][0]["tower_id"] == "330903908000000816"
    assert reopened.towers_snapshot()["count"] == 2


def test_configured_source_restores_towers_once_without_overwriting(tmp_path):
    source = tower_xlsx(tmp_path, sample_rows())
    service = workflow(tmp_path)
    service.configure_reference_sources({"towers": str(source)})
    assert service.state["towers"]["count"] == 2
    # 已导入的事实不因"再次配置"被覆盖
    service.state["towers"]["items"][0]["name"] = "人工改过的名字"
    service.configure_reference_sources({"towers": str(source)})
    assert service.state["towers"]["items"][0]["name"] == "人工改过的名字"


def test_existing_project_without_towers_still_opens(tmp_path):
    """既有项目（state 里没有 towers 键）必须保持兼容。"""
    project = tmp_path / "legacy.json"
    service = WorkflowService(project, DEFAULTS)
    service.save()
    reopened = WorkflowService(project, DEFAULTS)
    assert reopened.towers_snapshot() == {}
    assert "towers" not in reopened.state or isinstance(reopened.state.get("towers"), dict)


# --------------------------------------------------------------- 8. Source Center API

def test_source_center_api_imports_towers(tmp_path):
    service = workflow(tmp_path)
    router = ApiRouter(_ApiContext(service))
    response = router.post("/api/towers/import", {"path": str(tower_xlsx(tmp_path, sample_rows()))})
    assert response.status == 200
    assert response.data["towers"]["count"] == 2
    assert response.data["source_audits"]["items"]["towers"]["confirmed_crs"] == "EPSG:4490"


def test_data_sources_whitelist_accepts_towers_key():
    """``/api/data-sources`` 的 clean 白名单必须接受 towers（接线缺口修复的锁定）。"""
    source = inspect.getsource(ApiRouter.post)
    assert '"towers"' in source
    body = source[source.index("clean = {"):source.index("if path == \"/api/data-sources/validate\"")]
    assert "towers" in body


def test_path_resolver_and_registry_expose_towers():
    from cns_planner.gis.path_resolver import ROLE_LABELS, supported_formats
    from cns_planner.data.registry import DEFINITIONS

    assert ROLE_LABELS["towers"] == "通信铁塔"
    formats = supported_formats("towers")
    for suffix in (".xlsx", ".csv", ".geojson", ".gpkg", ".shp"):
        assert suffix in formats, f"towers 必须接受 {suffix}"
    assert {item.id for item in DEFINITIONS} >= {"towers", "obstacles"}


# --------------------------------------------------------------- 14. invalidation

def test_towers_import_never_stales_unrelated_results(tmp_path):
    service = workflow(tmp_path)
    before = dict(service.state.get("result_statuses") or {})
    service.import_towers(tower_xlsx(tmp_path, sample_rows()))
    after = dict(service.state.get("result_statuses") or {})
    for key in ("environment_risk", "grid_risk_v2", "report", "routes", "coverage"):
        assert after.get(key) == before.get(key), f"{key} 不应因为铁塔导入而变化"


def test_towers_source_is_a_tracked_source_for_directed_derived_invalidation(tmp_path):
    """BUG-TOWERS-002 已修复：铁塔源变化走**定向**失效，不再拖累风险数学。

    旧行为（``towers`` 映射到永远为空的 ``grid_attributes["towers"]``）会让铁塔一变就把
    ``grid_risk`` / ``grid_risk_v2`` / ``environment_risk`` 置 stale —— 而风险数学从未读过
    铁塔，属于无意义重算。现在铁塔只失效真正消费它的下游：

    * 铁塔派生事实（``tower_obstacle_profiles`` / ``tower_colocation_candidates``）；
    * 航路候选（塔净空进入 feasibility）与 CNS 建站提案（共塔宿主候选变化）；
    * 当前 active report。

    Towers Operational Integration V2 之后，铁塔确实被航路净空与 CNS 共塔规划消费，
    所以"完全不影响任何下游"也不再成立 —— 变的只是**影响面**，不是"要不要失效"。
    """

    service = workflow(tmp_path)
    service.state["grid_risk"] = {"status": "passed", "cells": {}}
    service.state["grid_risk_v2"] = {"status": "passed", "cells": {}}
    service.state.setdefault("result_statuses", {})["environment_risk"] = "passed"
    service.state["result_statuses"]["grid_risk_v2"] = "passed"
    service.state["result_statuses"]["tower_obstacle_profiles"] = "passed"
    service.state["result_statuses"]["tower_colocation_candidates"] = "passed"
    service.state["result_statuses"]["cns_site_plan"] = "passed"
    service.state["cns_site_plan"] = {"status": "proposal_ready"}

    service.invalidate_grid_attributes({"towers"})

    # 风险数学：绝不因为铁塔变化而重算
    assert service.state["grid_risk"]["status"] == "passed"
    assert service.state["result_statuses"]["environment_risk"] == "passed"
    assert service.state["grid_risk_v2"]["status"] == "passed"
    # 铁塔真正消费的下游：定向失效
    assert service.state["result_statuses"]["tower_obstacle_profiles"] == "stale"
    assert service.state["result_statuses"]["tower_colocation_candidates"] == "stale"
    # B7X：P11 站址试算是只读 compatibility 结果，失效只发生在 runtime-only cache。
    assert service.state["result_statuses"]["cns_site_plan"] == "passed"
    assert service.state["cns_site_plan"]["status"] == "proposal_ready"
    # 铁塔自身的网格属性命名空间始终没有被写入（铁塔不是网格属性）。
    assert service.state["grid_attributes"]["towers"] == empty_grid_attributes()["towers"]


def test_towers_source_content_change_is_reported_by_existing_audit_chain(tmp_path):
    """实测既有失效语义：源内容变化 → needs_revalidation（不修改 invalidation）。"""
    source = tower_xlsx(tmp_path, sample_rows())
    service = workflow(tmp_path)
    service.register_source_paths({"towers": source})
    service.verify_source("towers", source)
    assert service.state["source_audits"]["items"]["towers"]["status"] == "verified"

    source.write_bytes(source.read_bytes() + b"\x00")
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "站点清单"
    sheet.append(HEADER)
    for row in sample_rows() + [["T-3", "新增站", "A1", 122.2, 30.2, "角钢塔", 10, 10]]:
        sheet.append(row)
    workbook.save(source)

    snapshot = service.register_source_paths({"towers": source})
    assert snapshot["source_audits"]["items"]["towers"]["status"] == "needs_revalidation"


# --------------------------------------------------------------- 数学未变

def test_theta_star_v2_objective_weights_are_untouched():
    assert USER_DEFINED_BASELINE_WEIGHTS == {"risk": 0.8, "turn": 0.1, "distance": 0.1}
    policy = default_theta_v2_objective_policy()
    assert policy["risk_weight"] == 0.8
    assert policy["turn_weight"] == 0.1
    assert policy["distance_weight"] == 0.1


def test_risk_framework_v2_has_no_tower_factor():
    assert "towers" not in FACTOR_INPUT_ATTRIBUTES
    assert all(
        definition.get("source_role") != "towers"
        for definition in FACTOR_DEFINITIONS.values()
    )


def test_risk_v1_ground_formula_never_reads_towers():
    from cns_planner.risk.v1 import RiskModelV1

    body = inspect.getsource(RiskModelV1._ground_risk)
    assert "towers" not in body, "Risk V1 ground 公式不得引入铁塔因子"


def test_tower_contract_forbids_performance_fields(tmp_path):
    record = load_towers(tower_xlsx(tmp_path, sample_rows()))
    keys = set(record["items"][0])
    for forbidden in ("coverage", "coverage_radius", "transmit_power", "frequency",
                      "antenna_height", "capacity", "cost", "reliability",
                      "available_subsystems"):
        assert forbidden not in keys
    assert "available_subsystems" in FORBIDDEN_PERFORMANCE_FIELDS
    assert record["metadata"]["cns_capability_derived"] is False
    assert record["metadata"]["planning_integration"] == "reference_only"


# --------------------------------------------------------------- 真实报送数据

@pytest.mark.skipif(not REAL_XLSX.is_file(), reason="真实铁塔报送数据不在本机")
def test_real_report_xlsx_matches_the_audited_facts():
    import hashlib

    assert hashlib.sha256(REAL_XLSX.read_bytes()).hexdigest() == REAL_SHA256
    record = load_towers(
        REAL_XLSX, source_crs=TOWERS_CONFIRMED_SOURCE_CRS, crs_confirmed=True,
    )
    assert record["count"] == 373
    assert record["skipped"] == []
    assert len({item["tower_id"] for item in record["items"]}) == 373
    longitudes = [item["longitude"] for item in record["items"]]
    latitudes = [item["latitude"] for item in record["items"]]
    assert 122.0 < min(longitudes) and max(longitudes) < 123.0
    assert 29.7 < min(latitudes) and max(latitudes) < 30.9
    rows = sorted(item["source"]["row"] for item in record["items"])
    assert rows[0] == 2 and rows[-1] == 374 and len(set(rows)) == 373
    assert record["crs"]["source_crs"]["value"] == "EPSG:4490"
    assert record["metadata"]["source_file"] == "航路航线规划-铁塔数据.xlsx"
