"""Phase4-B4X：Planning Constraint Field 只读展示读取路径 + 约束场 UI 契约。

本批次是 **UI / Presentation / Read-only visualization** 批次，因此这里的 Python 测试
只覆盖 B4X **新增的唯一后端接口**（最薄的只读读取路径），不重复 B3X 已覆盖的生成语义：

* ``GET /api/planning-constraint-field``      → 既有 summary 集合（slim，不含 cells）
* ``GET /api/planning-constraint-field/map``  → 地图友好紧凑投影（grid_id / outcome / blocked_by）

硬契约（逐条断言，防止后续批次悄悄放宽）：

1. 绝不把逐 cell 明细塞进通用 workflow 快照（快照保持 slim）；
2. 地图接口**只**返回 ``grid_id`` / ``outcome`` / ``blocked_by`` 三个字段——
   不返回 ``unknown_reasons`` / ``evidence_refs`` / geometry，因此不重复下发 GeoJSON；
3. 24,300 格量级仍是一条紧凑记录，不允许把完整 raw artifact 无条件回传；
4. ``cells`` 不可读时返回 ``cells_unavailable``，**绝不**退化成"全部可通行"；
5. 必须显式给出 ``altitude_layer_id``（不猜一个默认高度层）；
6. 不支持 bbox 越界 / 反向 / 非数值（非法输入忽略过滤而不是猜一个范围）；
7. 只读：调用这两个接口不得改变项目状态、revision 或任何结果状态。
"""

from copy import deepcopy

import pytest

from cns_planner.application.planning_constraint_field_service import (
    PlanningConstraintFieldService,
)
from cns_planner.domain.planning_constraint_field import (
    normalize_planning_constraint_field_collection,
)


VREF = "egm2008_orthometric"


class _Session:
    """最小 session 替身：B4X 的只读路径只读 ``state`` 与 ``store_path``。"""

    def __init__(self, state, store_path=None):
        self.state = state
        self.store_path = store_path


def cell(grid_id, outcome, blocked_by=(), unknown_reasons=(), bbox=None):
    item = {
        "grid_id": grid_id, "outcome": outcome, "blocked_by": list(blocked_by),
        "unknown_reasons": list(unknown_reasons), "evidence_refs": [],
    }
    if bbox is not None:
        item["bbox"] = list(bbox)
    return item


def field(identifier="ALT-080", altitude=80.0, cells=(), **overrides):
    payload = {
        "schema_version": 1,
        "field_id": f"PCF-{identifier}-0123456789abcdef",
        "status": "completed_with_warnings",
        "altitude_layer_id": identifier,
        "nominal_altitude_m": altitude,
        "vertical_reference": VREF,
        "workspace_identity": {"workspace_id": "W1", "revision": 3, "bbox": [0, 0, 2, 2]},
        "grid_identity": "pcf-grid-test",
        "policy_fingerprint": {"terrain_vertical_clearance_m": 30.0},
        "source_fingerprints": {},
        "constraint_field_fingerprint": f"pcf-{identifier}-fingerprint",
        "unknown_policy": {
            "allow_unknown_for_provisional": False,
            "unknown_remains_unknown": True,
            "operational_adoption_allowed": False,
        },
        "counts": {"total": len(cells), "pass": 0, "blocked": 0, "unknown": 0,
                   "blocked_by": {"terrain": 0, "building": 0, "tower": 0,
                                  "airspace": 0, "critical_site": 0}},
        "warnings": [],
        "artifact_ref": None,
        "cells": list(cells),
    }
    payload.update(overrides)
    return payload


def collection(*fields):
    return {
        "schema_version": 1, "status": "passed",
        "collection_id": "planning_constraint_fields",
        "count": len(fields), "items": list(fields),
    }


def service(*fields):
    return PlanningConstraintFieldService(
        _Session({"planning_constraint_fields": collection(*fields)}), None, None
    )


# ---- 1. 地图接口：紧凑投影 --------------------------------------------------

def test_map_projection_returns_only_grid_id_outcome_and_blocked_by():
    svc = service(field(cells=[
        cell("G0", "pass"),
        cell("G1", "blocked", ["terrain", "building"]),
        cell("G2", "unknown", unknown_reasons=[
            {"domain": "tower", "reason": "tower_dataset_unresolved"},
        ]),
    ]))
    result = svc.field_map("ALT-080")
    assert result["status"] == "passed"
    assert [item["grid_id"] for item in result["cells"]] == ["G0", "G1", "G2"]
    for item in result["cells"]:
        assert set(item) == {"grid_id", "outcome", "blocked_by"}, (
            "地图接口只允许下发 grid_id / outcome / blocked_by；"
            "evidence 明细与几何一律不进 HTTP（几何复用前端标准网格索引）"
        )
    assert sorted(result["cells"][1]["blocked_by"]) == ["building", "terrain"]
    assert result["counts"]["blocked"] == 1
    assert result["counts"]["unknown"] == 1
    # 几何由前端的标准网格索引提供：后端只声明来源，不下发任何坐标。
    # `bbox` 是**请求用的视口**（未请求时为 None），不是单元几何。
    assert result["geometry_source"] == "frontend_grid_index"
    assert result["bbox"] is None


def test_map_projection_never_ships_the_raw_artifact_or_evidence():
    svc = service(field(cells=[cell("G0", "blocked", ["terrain"], bbox=[0, 0, 1, 1])]))
    result = svc.field_map("ALT-080")
    # 逐格记录是"证据不外泄"的契约边界：只允许三个业务字段。
    text = repr(result["cells"])
    for forbidden in ("unknown_reasons", "evidence_refs", "geometry", "polygon",
                      "FeatureCollection", "bbox", "center"):
        assert forbidden not in text, f"地图接口不得下发 {forbidden}"
    assert set(result["cells"][0]) == {"grid_id", "outcome", "blocked_by"}


def test_map_projection_stays_compact_for_a_realistic_grid_size():
    cells = [cell(f"G{index}", "pass" if index % 3 else "blocked",
                  ["terrain"] if index % 3 == 0 else []) for index in range(24300)]
    svc = service(field(cells=cells))
    result = svc.field_map("ALT-080")
    assert result["total_count"] == 24300
    assert len(result["cells"]) == 24300
    assert result["truncated"] is False
    # 每格只有 3 个字段：紧凑投影必须显著小于带 evidence 的 raw artifact（数量级断言）。
    assert len(repr(result["cells"])) < 24300 * 90


# ---- 2. 视口过滤 ------------------------------------------------------------

def test_map_projection_filters_by_viewport_bbox_without_guessing():
    svc = service(field(cells=[
        cell("G0", "blocked", ["terrain"], bbox=[0.0, 0.0, 1.0, 1.0]),
        cell("G1", "pass", bbox=[10.0, 10.0, 11.0, 11.0]),
    ]))
    inside = svc.field_map("ALT-080", "0,0,1.5,1.5")
    assert [item["grid_id"] for item in inside["cells"]] == ["G0"]
    assert inside["bbox"] == [0.0, 0.0, 1.5, 1.5]
    assert inside["total_count"] == 2, "total_count 报告完整约束场规模，不因视口过滤而变小"


@pytest.mark.parametrize("raw", ["1,2,3", "a,b,c,d", "4,3,2,1", "", None, "1,2,3,4,5"])
def test_invalid_bbox_is_ignored_instead_of_guessed(raw):
    svc = service(field(cells=[cell("G0", "blocked", ["terrain"], bbox=[0, 0, 1, 1])]))
    result = svc.field_map("ALT-080", raw)
    assert result["status"] == "passed"
    assert len(result["cells"]) == 1, "非法 bbox 不得过滤掉任何格子（宁可多返回）"
    assert result["bbox"] is None


def test_cells_without_bbox_are_kept_when_a_viewport_is_given():
    svc = service(field(cells=[
        cell("G0", "blocked", ["terrain"]),
        cell("G1", "pass", bbox=[10.0, 10.0, 11.0, 11.0]),
    ]))
    result = svc.field_map("ALT-080", "0,0,1,1")
    ids = [item["grid_id"] for item in result["cells"]]
    assert ids == ["G0"], "缺少 bbox 的 blocked 格绝不允许因为'没有几何'而从结果里消失"


# ---- 3. 失败与缺失：绝不伪装成可通行 ----------------------------------------

def test_missing_altitude_layer_id_is_an_explicit_error_not_a_default_layer():
    svc = service(field(cells=[cell("G0", "pass")]))
    result = svc.field_map("")
    assert result["status"] == "altitude_layer_required"
    assert result["cells"] == []
    assert "altitude_layer_id" in result["reason"]


def test_unknown_altitude_layer_reports_not_calculated_without_cells():
    svc = service(field(cells=[cell("G0", "pass")]))
    result = svc.field_map("ALT-200")
    assert result["status"] == "not_calculated"
    assert result["cells"] == []
    assert result["counts"] is None


def test_summary_without_readable_cells_is_cells_unavailable_never_all_pass():
    summary = field(cells=[])
    summary.pop("cells")
    summary["counts"] = {"total": 24300, "pass": 0, "blocked": 9918, "unknown": 14382,
                         "blocked_by": {"terrain": 9496, "building": 1090, "tower": 0,
                                        "airspace": 0, "critical_site": 0}}
    svc = service(summary)
    result = svc.field_map("ALT-080")
    assert result["status"] == "cells_unavailable"
    assert result["cells"] == [], "读不到明细时绝不允许把结果当成'全部可通行'"
    assert result["counts"]["blocked"] == 9918, "摘要计数仍然如实转印"
    assert "不可用" in result["reason"] or "不可读" in result["reason"]


def test_unreadable_sidecar_degrades_to_empty_without_raising(tmp_path):
    state = {"planning_constraint_fields": collection(field(cells=[])),
             "result_index": {"schema_version": 1, "artifact": ".cns-results/missing.json.gz",
                              "sha256": "0" * 64}}
    state["planning_constraint_fields"]["items"][0].pop("cells")
    svc = PlanningConstraintFieldService(_Session(state, tmp_path / "project.json"), None, None)
    result = svc.field_map("ALT-080")
    assert result["status"] == "cells_unavailable"


# ---- 4. summary 通道保持 slim ------------------------------------------------

def test_summary_snapshot_drops_cells_and_reads_the_existing_collection():
    svc = service(field(cells=[cell("G0", "blocked", ["terrain"])]))
    snapshot = svc.result_snapshot("ALT-080")
    assert snapshot["count"] == 1
    item = snapshot["items"][0]
    assert "cells" not in item, "summary 通道必须保持 slim（逐 cell 明细只走地图接口）"
    assert item["counts"]["total"] == 1
    assert item["altitude_layer_id"] == "ALT-080"


def test_summary_snapshot_filters_by_altitude_layer_without_adding_one():
    svc = service(field("ALT-080", 80.0, cells=[]), field("ALT-100", 100.0, cells=[]))
    assert svc.result_snapshot()["count"] == 2
    assert svc.result_snapshot("ALT-100")["count"] == 1
    assert svc.result_snapshot("ALT-100")["items"][0]["altitude_layer_id"] == "ALT-100"
    assert svc.result_snapshot("ALT-999")["count"] == 0


# ---- 5. 只读性 --------------------------------------------------------------

def test_read_paths_never_mutate_the_project_state():
    svc = service(field(cells=[cell("G0", "blocked", ["terrain"], bbox=[0, 0, 1, 1])]))
    before = deepcopy(svc.session.state)
    svc.result_snapshot("ALT-080")
    svc.field_map("ALT-080")
    svc.field_map("ALT-080", "0,0,2,2")
    svc.field_map("ALT-404")
    assert svc.session.state == before, "B4X 读取路径必须是只读的：不得改状态、revision 或结果状态"


def test_normalized_collection_contract_keeps_three_state_semantics():
    normalized = normalize_planning_constraint_field_collection(collection(
        field(cells=[cell("G0", "blocked", ["terrain", "not_a_domain"])]),
    ))
    item = normalized["items"][0]
    assert item["cells"][0]["blocked_by"] == ["terrain"], "未登记的 blocker 域必须被丢弃"
    assert item["cells"][0]["outcome"] == "blocked"


# ---- 6. 多高度层参数化（绝不写死 ALT-080） -----------------------------------

@pytest.mark.parametrize(
    "identifier,altitude",
    [("ALT-060", 60.0), ("ALT-080", 80.0), ("ALT-100", 100.0),
     ("ALT-150", 150.0), ("ALT-200", 200.0), ("CUSTOM-LAYER-42", 42.0)],
)
def test_map_projection_is_parameterized_by_any_altitude_layer(identifier, altitude):
    svc = service(field(identifier, altitude, cells=[cell("G0", "blocked", ["terrain"])]))
    result = svc.field_map(identifier)
    assert result["status"] == "passed"
    assert result["altitude_layer_id"] == identifier
    assert result["nominal_altitude_m"] == altitude
    assert result["cells"][0]["outcome"] == "blocked"


def test_map_projection_does_not_hardcode_alt_080_in_source():
    from pathlib import Path

    source = (
        Path(__file__).parents[1]
        / "cns_planner" / "application" / "planning_constraint_field_service.py"
    ).read_text(encoding="utf-8")
    assert "ALT-080" not in source, "B4X 读取路径不得写死任何高度层 id"
    assert "80.0" not in source and "80 m" not in source
