"""Round 29-K：Radar 规划 authority 与 canonical P16 candidate search 的职责去重。

业务裁定（本轮正式生效）：

* ``S:radar_noncooperative`` 的规划 authority 是 ``radar_surveillance_layout``
  （真实铁塔 → Radar-I → bearing / bearing±45° → dominated pruning → MILP →
  5m validation → canonical gap verdict）；
* canonical **P16 不再枚举 Radar candidate panels**，绝不做第二次优化；
* Radar 证据继续进入 P14/P15（只读消费上游 ``selected_panels``）；
* proven infeasible 的 Radar 缺口是 **non_actionable_terminal_managed_gap**：
  事实缺口如实保留，但 ``p16_actionable=false``，不得把 P16 判成找不到方案；
* ``search_incomplete`` / ``refinement_incomplete`` / ``unresolved`` / ``stale``
  **不是** managed physical gap：P16 保持 unknown/evidence_required，
  绝不转成 confirmed infeasible，也绝不自补面阵绕过上游。
"""

from __future__ import annotations

from copy import deepcopy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cns_planner.algorithms.corridor.v1 import CNSServiceCorridorV1  # noqa: E402
from cns_planner.algorithms.corridor_gap.v1 import CNSCorridorGapAnalyzerV1  # noqa: E402
from cns_planner.application.workflow_service import WorkflowService  # noqa: E402
from cns_planner.domain.cns_service_contract import (  # noqa: E402
    SERVICE_KEY_RADAR_NONCOOPERATIVE,
)
from cns_planner.domain.cns_service_registry import planner_family_for  # noqa: E402
from cns_planner.domain.radar_service_evidence import (  # noqa: E402
    build_radar_service_evidence,
)

from test_p16_prefilter_clearance_contract import (  # noqa: E402
    DEFAULTS, _build_state, _candidate,
)
from test_radar_noncooperative_service_chain import (  # noqa: E402
    layout as radar_layout, panel as radar_panel, required as radar_required,
)

RADAR = SERVICE_KEY_RADAR_NONCOOPERATIVE
RADAR_PLANNER_FAMILY = planner_family_for(RADAR)
COMMUNICATION = "C:communication"


def _corridor():
    return {
        "status": "passed",
        "algorithm_id": CNSServiceCorridorV1.algorithm_id,
        "algorithm_version": CNSServiceCorridorV1.algorithm_version,
        "input_fingerprint": "round29k-p14",
        "routes": [],
        "endpoint_service_evidence": None,
    }


def _gap():
    """只含一个 Radar 服务缺口的 P15 fixture（手写，确保可判定）。"""

    return {
        "status": "failed",
        "algorithm_id": CNSCorridorGapAnalyzerV1.algorithm_id,
        "algorithm_version": CNSCorridorGapAnalyzerV1.algorithm_version,
        "input_fingerprint": "round29k-p15",
        "endpoint_service_gaps": {},
        "routes": [{
            "route_id": "R1", "status": "confirmed_gap",
            "voxels": [{
                "voxel_id": "V1", "grid_id": "G1", "altitude_layer_id": "L1",
                "discretized_volume_proxy_m3": 1000.0, "nearest_route_offset_m": 0.0,
                "subsystems": [{
                    "subsystem": "S", "status": "confirmed_gap",
                    "services": [{
                        "service_key": RADAR, "surface_dependent": True,
                        "supports_site_planning": True, "status": "confirmed_deficit",
                        "counting_basis": "distinct_site_id",
                        "required_distinct_site_count": 2, "distinct_site_count": 0,
                        "surface_class": "land",
                    }],
                }],
            }],
            "subsystems": [{
                "subsystem": "S", "confirmed_target_voxel_ids": ["V1"],
                "unknown_voxel_ids": [],
            }],
        }],
    }


def _layout(status, *, gap_classification, managed, gap_reason, proven=None, panels=None):
    """上游 layout 的 canonical verdict fixture（三个 gap 字段**原样**给出）。"""

    return {
        "status": status,
        "algorithm_id": "radar_surveillance_layout",
        "algorithm_version": "1.2",
        "items": [{
            "route_id": "R1",
            "status": status,
            "gap_reason": gap_reason,
            "gap_classification": gap_classification,
            "managed_physical_gap": managed,
            "solver": {"infeasibility_proven": proven},
            "service_evidence_inputs": {
                "selected_panels": list(panels or []),
                "candidate_panels": [
                    {"panel_id": "P1", "tower_id": "T1", "radar_type": "radar_i",
                     "azimuth_deg": 0.0, "panel_half_width_deg": 45.0},
                    {"panel_id": "P2", "tower_id": "T2", "radar_type": "radar_i",
                     "azimuth_deg": 180.0, "panel_half_width_deg": 45.0},
                ],
                "tower_records": [{"tower_id": "T1"}, {"tower_id": "T2"}],
                "samples": [],
            },
        }],
    }


def _evaluate(tmp_path, layout, *, gap=None, corridor=None):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    state = workflow.state
    state["cns_corridor_assessment"] = corridor or _corridor()
    state["cns_corridor_gap_assessment"] = gap or _gap()
    state["radar_surveillance_layout"] = layout
    state["candidate_sites"] = {"status": "passed", "items": [], "count": 0}
    state["existing_cns_facilities"] = {"status": "passed", "items": [], "count": 0}
    workflow.evaluate_cns_corridor_site_plan()
    return state.get("cns_corridor_site_plan") or {}, state


TERMINAL_LAYOUT = _layout(
    "infeasible", gap_classification="confirmed_gap", managed=True,
    gap_reason="independent_site_count_limited", proven=False,
)
INCOMPLETE_LAYOUT = _layout(
    "search_incomplete", gap_classification="unknown", managed=False,
    gap_reason="search_incomplete", proven=False,
)
REFINEMENT_INCOMPLETE_LAYOUT = _layout(
    "refinement_incomplete", gap_classification="unknown", managed=False,
    gap_reason="search_incomplete", proven=False,
)
UNRESOLVED_LAYOUT = _layout(
    "unresolved", gap_classification="unknown", managed=False,
    gap_reason="search_incomplete", proven=False,
)
STALE_LAYOUT = _layout(
    "stale", gap_classification="unknown", managed=False,
    gap_reason="search_incomplete", proven=False,
)
FEASIBLE_LAYOUT = _layout(
    "proposal_ready", gap_classification="none", managed=False, gap_reason=None,
    panels=[{"panel_id": "P0", "tower_id": "T0", "radar_type": "radar_i",
             "azimuth_deg": 90.0, "panel_half_width_deg": 45.0}],
)


def _legacy_layout():
    """旧 V1.1 layout：item 上**没有** canonical gap 三字段，但 solver 已证明不可行。

    真实权威项目 R0005 持久化的正是这种形状（``algorithm_version=1.1``、
    ``solver.infeasibility_proven=true``、``gap_classification=None``）。它由**旧算法
    语义版本**产生，P16 绝不能把它的 "infeasible" 当作 current 的 terminal managed gap。
    """

    return {
        "status": "not_calculated",
        "algorithm_id": "radar_surveillance_layout",
        "algorithm_version": "1.1",
        "items": [{
            "route_id": "R1", "status": "infeasible",
            "gap_reason": None, "gap_classification": None, "managed_physical_gap": None,
            "solver": {"status": "infeasible", "infeasibility_proven": True},
            "service_evidence_inputs": {
                "selected_panels": [],
                "candidate_panels": [
                    {"panel_id": "P1", "tower_id": "T1", "radar_type": "radar_i",
                     "azimuth_deg": 0.0, "panel_half_width_deg": 45.0},
                ],
                "tower_records": [{"tower_id": "T1"}],
                "samples": [],
            },
        }],
    }


# ---------------------------------------------------------------------------
# 1. canonical P16 不再产生 Radar 候选面板
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "terminal", "incomplete", "refinement_incomplete", "unresolved", "stale", "feasible",
])
def test_canonical_p16_never_emits_directional_radar_candidate(tmp_path, name):
    layout = {
        "terminal": TERMINAL_LAYOUT, "incomplete": INCOMPLETE_LAYOUT,
        "refinement_incomplete": REFINEMENT_INCOMPLETE_LAYOUT,
        "unresolved": UNRESOLVED_LAYOUT, "stale": STALE_LAYOUT,
        "feasible": FEASIBLE_LAYOUT,
    }[name]
    plan, _state = _evaluate(tmp_path, deepcopy(layout))

    candidates = plan.get("candidate_actions") or []
    assert [
        item for item in candidates if item.get("service_key") == RADAR
    ] == [], "canonical P16 绝不再枚举 Radar candidate panels"
    assert [
        item for item in candidates
        if item.get("planner_family") == RADAR_PLANNER_FAMILY
    ] == []
    assert plan["radar_planning_authority"] == {
        "service_key": RADAR,
        "planning_owner": "radar_surveillance_layout",
        "canonical_p16_candidate_family": False,
        "p16_reads_upstream_selected_panels": True,
        "semantics": (
            "radar_candidate_panels_are_enumerated_and_optimized_only_by_"
            "radar_surveillance_layout_p16_never_reruns_that_optimization"
        ),
    }


# ---------------------------------------------------------------------------
# 2. Radar 证据仍由 P14 只读消费上游 selected_panels
# ---------------------------------------------------------------------------

def test_p14_consumes_upstream_layout_selected_panels_verbatim():
    selected = [radar_panel("A1", "A")]
    layout = radar_layout(surface="sea", selected=selected)
    evidence = build_radar_service_evidence(radar_required(), layout, route_ids=["R1"])
    assert evidence is not None, "显式要求 Radar 时证据必须存在"
    inputs = evidence["routes"][0]["service_evidence_inputs"]
    assert inputs["selected_panels"] == selected, (
        "P14 必须原样消费上游 layout 的 selected_panels，绝不追加第二份面板"
    )
    assert evidence["planner_family"] == RADAR_PLANNER_FAMILY


# ---------------------------------------------------------------------------
# 3. proven infeasible ⇒ terminal managed gap（事实保留、P16 不可行动）
# ---------------------------------------------------------------------------

def test_proven_infeasible_radar_is_terminal_managed_non_actionable_gap(tmp_path):
    plan, state = _evaluate(tmp_path, deepcopy(TERMINAL_LAYOUT))

    terminal = plan["terminal_managed_gaps"]
    assert len(terminal) == 1
    entry = terminal[0]
    assert entry["service_key"] == RADAR
    assert entry["source"] == "radar_surveillance_layout"
    assert entry["planning_owner"] == "radar_surveillance_layout"
    assert entry["gap_reason"] == "independent_site_count_limited"
    assert entry["gap_classification"] == "confirmed_gap"
    assert entry["managed_physical_gap"] is True
    assert entry["p16_actionable"] is False
    assert entry["gap_kind"] == "non_actionable_terminal_managed_gap"
    #: 事实缺口绝不被改写成 satisfied。
    assert entry["factual_gap"] is True
    assert entry["satisfied"] is False
    assert plan["terminal_managed_gap_count"] == 1
    assert plan["p16_actionable_target_count"] == 0

    #: P16 不得因此判成"找不到方案"，也不得继续重评雷达面板。
    assert plan["status"] == "no_action_required"
    assert plan["stop_reason"] == "only_terminal_managed_gaps_outside_p16_authority"
    assert state["result_statuses"]["cns_corridor_site_plan"] == "passed"
    assert plan["candidate_actions"] == []
    #: 该 factual gap 仍原样留在 P16 的 target 集里，供 P17 / Step6 / report 消费。
    assert plan["target_voxel_count"] == 1


# ---------------------------------------------------------------------------
# 4. search_incomplete / stale 等不是 managed gap，也不被 P16 转 confirmed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["incomplete", "refinement_incomplete", "unresolved", "stale"])
def test_non_proven_radar_status_is_evidence_required_not_managed_gap(tmp_path, name):
    layout = {
        "incomplete": INCOMPLETE_LAYOUT,
        "refinement_incomplete": REFINEMENT_INCOMPLETE_LAYOUT,
        "unresolved": UNRESOLVED_LAYOUT,
        "stale": STALE_LAYOUT,
    }[name]
    plan, state = _evaluate(tmp_path, deepcopy(layout))

    assert plan["terminal_managed_gaps"] == [], (
        "搜索未完成 / 证据不足绝不是 managed physical gap"
    )
    assert plan["terminal_managed_gap_count"] == 0
    unknowns = plan["unknown_evidence_required"]
    radar_unknowns = [
        item for item in unknowns if item.get("kind") == "radar_planning_authority_upstream"
    ]
    assert len(radar_unknowns) == 1
    assert radar_unknowns[0]["gap_classification"] != "confirmed_gap"
    assert radar_unknowns[0]["p16_actionable"] is False
    assert plan["status"] == "evidence_required"
    assert state["result_statuses"]["cns_corridor_site_plan"] == "pending_confirmation"


# ---------------------------------------------------------------------------
# 5. 旧算法语义版本的 layout 不是 terminal managed gap
# ---------------------------------------------------------------------------

def test_legacy_layout_algorithm_semantics_is_not_a_terminal_managed_gap(tmp_path):
    plan, _state = _evaluate(tmp_path, _legacy_layout())

    assert plan["terminal_managed_gaps"] == [], (
        "旧算法语义版本的 layout 结论必须先用当前算法重算，绝不作为 current 的 "
        "terminal managed gap"
    )
    unknowns = [
        item for item in plan["unknown_evidence_required"]
        if item.get("kind") == "radar_planning_authority_upstream"
    ]
    assert unknowns
    assert all(item["layout_algorithm_semantics_stale"] is True for item in unknowns)
    assert all(item["p16_actionable"] is False for item in unknowns)
    assert plan["status"] == "evidence_required"
    assert [
        item for item in plan["candidate_actions"] or []
        if item.get("service_key") == RADAR
    ] == []


# ---------------------------------------------------------------------------
# 6. C / RID / N 候选不受 Radar 事实缺口影响
# ---------------------------------------------------------------------------

def test_communication_candidates_unaffected_by_terminal_radar_gap(tmp_path):
    workflow = _build_state(tmp_path, candidates=[_candidate("S1")])
    state = workflow.state
    gap = deepcopy(state["cns_corridor_gap_assessment"])
    route = gap["routes"][0]
    voxel = route["voxels"][0]
    voxel_id = voxel["voxel_id"]
    voxel.setdefault("subsystems", []).append({
        "subsystem": "S", "status": "confirmed_gap",
        "services": [{
            "service_key": RADAR, "surface_dependent": True,
            "supports_site_planning": True, "status": "confirmed_deficit",
            "counting_basis": "distinct_site_id",
            "required_distinct_site_count": 2, "distinct_site_count": 0,
            "surface_class": "land",
        }],
    })
    route.setdefault("subsystems", []).append({
        "subsystem": "S", "confirmed_target_voxel_ids": [voxel_id], "unknown_voxel_ids": [],
    })
    state["cns_corridor_gap_assessment"] = gap
    state["radar_surveillance_layout"] = deepcopy(TERMINAL_LAYOUT)
    workflow.evaluate_cns_corridor_site_plan()
    plan = state.get("cns_corridor_site_plan") or {}

    assert plan["terminal_managed_gaps"], "Radar 的 proven managed gap 必须被登记"
    communication = [
        item for item in plan.get("candidate_actions") or []
        if item.get("service_key") == COMMUNICATION
    ]
    assert communication, "C:communication 的候选绝不被 Radar 事实缺口取消"
    assert [
        item for item in plan.get("candidate_actions") or []
        if item.get("service_key") == RADAR
    ] == []
