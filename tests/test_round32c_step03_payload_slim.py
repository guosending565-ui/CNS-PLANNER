"""Round32-C：Step03 candidate 明细的 read projection / transport 瘦身回归。

事实（clean 3ac69d8 + Round32-B 实测）：``GET /api/layered-route-candidates`` 39,247,432 B，
其中 ``masks.<lane>.cells``（9,752 格）占 37,316,503 B = **95.10%**；candidate ``items``
只有 205,675 B。因此逐 cell mask 明细必须外置，并且**只改 read projection / transport**：

1. 交通层投影（``/api/layered-route-candidates``）去掉 cells，保留有界摘要与 items；
2. 明细按需读取（``/api/layered-route-candidates/masks``）可完整 hydrate（可选单车车道）；
3. 权威 state 与后端业务读路径完全不变 —— validation / RRP / adoption 继续直接读
   canonical service，绝不经过 slim transport。
"""

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cns_planner.application.workflow_service import WorkflowService  # noqa: E402

DEFAULTS = ROOT / "cns_planner" / "config" / "defaults.json"
MASK_ENDPOINT = "/api/layered-route-candidates/masks"
LANE = "R1@ALT-100"
CANDIDATE_ID = "LRC-R1-ALT-100-abcdef"
CELL_COUNT = 2000


def _collection(cells=CELL_COUNT):
    """一条真实形状的 candidate collection：1 个 candidate + 1 个含逐 cell 明细的 mask。"""

    return {
        "schema_version": "layered-route-candidates-v1",
        "status": "passed",
        "count": 1,
        "artifact_type": "layered_route_candidate",
        "active_candidate_id": CANDIDATE_ID,
        "current_key": LANE,
        "current_candidate_fingerprint": "layeredcandv1-" + "a" * 64,
        "items": [{
            "candidate_id": CANDIDATE_ID,
            "status": "candidate",
            "route_id": "R1",
            "altitude_layer_id": "ALT-100",
            "lane_key": LANE,
            "distance_m": 1234.5,
            "optimization_cost": 0.42,
            "turn_count": 3,
            "input_fingerprint": "layeredinputv2-" + "b" * 64,
            "mask_fingerprint": "layeredmaskv1-" + "c" * 64,
        }],
        "masks": {LANE: {
            "schema_version": "layered-feasibility-mask-v1",
            "status": "passed",
            "altitude_layer_id": "ALT-100",
            "grid_level": 8,
            "cell_count": cells,
            "counts": {"feasible": cells - 10, "blocked": 6, "unknown": 4},
            "mask_fingerprint": "layeredmaskv1-" + "c" * 64,
            "feasibility_policy_fingerprint": "layeredfeasibilityv2-" + "d" * 64,
            "cells": {
                f"grid-{index:05d}": {
                    "grid_id": f"grid-{index:05d}",
                    "status": "feasible" if index % 3 else "unknown",
                    "ground_elevation_m": 12.5 + index * 0.001,
                    "building_vertical_clearance_m": 50.0,
                }
                for index in range(cells)
            },
        }},
    }


def _service(tmp_path, cells=CELL_COUNT):
    service = WorkflowService(tmp_path / "project.json", DEFAULTS)
    service.layered_route_planner_service.ensure_state()
    service.state["layered_route_candidates"] = _collection(cells)
    return service


def _bytes(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def test_transport_projection_drops_mask_cells_but_detail_still_hydrates(tmp_path):
    """交通层投影去掉 cells（保留有界摘要），明细端点仍能完整/按车道 hydrate。"""

    service = _service(tmp_path)
    slim = service.layered_route_candidates()
    mask = slim["masks"][LANE]
    assert "cells" not in mask, "交通层投影不得携带逐 cell 明细"
    assert mask["cells_count"] == CELL_COUNT, "格数必须如实转印（缺失 ≠ 0）"
    assert mask["cells_detail"] == MASK_ENDPOINT, "必须声明明细读取入口"
    for key in ("status", "counts", "mask_fingerprint", "feasibility_policy_fingerprint",
                "grid_level", "altitude_layer_id", "current_applicability"):
        assert key in mask, f"有界摘要字段 {key} 不得被顺手删掉"
    assert mask["counts"] == {"feasible": CELL_COUNT - 10, "blocked": 6, "unknown": 4}
    assert [item["candidate_id"] for item in slim["items"]] == [CANDIDATE_ID]
    assert slim["items"][0]["distance_m"] == 1234.5
    # 权威读路径（后端业务用）完全不变：canonical 仍然带 cells。
    full = service.layered_route_planner_service.result_snapshot()
    assert len(full["masks"][LANE]["cells"]) == CELL_COUNT
    # 目标：普通刷新传输至少减少 80%（这里以"纯逐 cell 明细"上界验证投影确实只做减法）。
    slim_bytes, full_bytes = _bytes(slim), _bytes(full)
    assert slim_bytes * 5 < full_bytes, f"slim={slim_bytes} 必须远小于 full={full_bytes}"
    # 明细端点：必须能完整 hydrate（全部车道），也能只取所选车道（地图只画一个 mask）。
    everything = service.layered_route_masks()
    assert list(everything) == [LANE]
    assert len(everything[LANE]["cells"]) == CELL_COUNT, "完整 hydrate 必须带回逐 cell 明细"
    single = service.layered_route_masks(LANE)
    assert list(single) == [LANE] and len(single[LANE]["cells"]) == CELL_COUNT
    assert service.layered_route_masks("NOPE@ALT-100") == {}, "未知车道必须如实返回空，不猜"


def test_backend_business_reads_bypass_the_slim_transport(tmp_path, monkeypatch):
    """validation / RRP 等后端业务读路径不得依赖 slim transport。"""

    service = _service(tmp_path)

    def _boom(*args, **kwargs):  # pragma: no cover - 只在被误用时触发
        raise AssertionError("后端业务读路径不得经过 slim transport")

    monkeypatch.setattr(WorkflowService, "layered_route_candidates", _boom)
    monkeypatch.setattr(WorkflowService, "layered_route_masks", _boom)
    # 唯一允许的读取方式：canonical service。即使交通层投影已不可用，这些读路径也必须照常
    # 工作（走到这里没有抛 AssertionError 就是证据）。
    service.layered_route_planner_service.current_candidate_snapshot()
    assert len(service.layered_route_planner_service.mask_snapshot(
        route_id="R1", altitude_layer_id="ALT-100",
    )["cells"]) == CELL_COUNT
    # RRP / validation 各自持有的 layered 读源必须是 canonical collection（带 cells）。
    for owner in (service.route_risk_profile_service, service.layered_route_validation_service):
        collection = owner.layered.result_snapshot()
        assert len(collection["masks"][LANE]["cells"]) == CELL_COUNT
    with pytest.raises(AssertionError):
        WorkflowService.layered_route_candidates(service)
