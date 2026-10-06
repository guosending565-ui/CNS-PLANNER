"""Round31-C 定向验证：Radar-I 优先 + 既有站址 Radar-II 分级升级。

对应本轮验证清单：

* 1  新项目默认算法版本全部可执行（回归护栏，P14/P15/P16 未被本轮改动）；
* 2  Radar-I 可行时绝不触发 Radar-II；
* 3  Radar-I 被**严格证明**不可行时才进入 Stage B；
* 4  未完成 / 未证明的求解绝不升级；
* 5  Radar-II 只使用既有物理站址，绝不新建站址；
* 6  同一物理塔的多个面阵只算一个独立站址；
* 8  Stage B 的 validation / refinement 继续有效；
* 9  Radar overlay 覆盖两种型号并如实披露未知型号；
* 7/10 Radar-II 不是 RID cooperative service，P17 仍是 planning proxy；
* 11 新项目默认启用、历史 payload 保持 I-only（无业务结果漂移）。
"""

import json
from pathlib import Path

from cns_planner.algorithms.continuous_service.v1 import (
    RADAR_DETECTION_PROXY_DISTANCE_STATUS, RADAR_DETECTION_PROXY_SEMANTICS,
    RADAR_DETECTION_PROXY_SERVICE_COVERAGE_STATUS,
)
from cns_planner.algorithms.radar_layout import milp as milp_module
from cns_planner.algorithms.radar_layout.v1 import solve_layout
from cns_planner.algorithms.registry import (
    build_default_algorithm_registry, default_algorithm_selection,
)
from cns_planner.application.map_figure_service import radar_overlay_panels
from cns_planner.application.radar_surveillance_layout_service import (
    normalize_radar_surveillance_policy,
)
from cns_planner.domain.radar_surveillance_layout import RADAR_TYPE_I, RADAR_TYPE_II

DEFAULTS_PATH = Path("cns_planner/config/defaults.json")


def tower(tower_id, xy, origin=50.0):
    return {"tower_id": tower_id, "metric": list(xy), "origin_egm2008_m": origin}


def sample(index, xy, *, required=1, surface="sea"):
    return {
        "sample_id": f"S{index}", "metric": list(xy), "egm2008_m": 80.0,
        "surface_class": surface, "required_distinct_site_count": required,
        "distance_along_route_m": float(index * 25.0),
    }


def solve(towers, samples, **kwargs):
    return solve_layout(towers=towers, samples=samples, **kwargs)


def _incomplete_solve(status):
    """构造一个"未完成/未证明"的 solver 结果（不伪造任何证明）。"""

    def fake_solve(**kwargs):
        block = milp_module.empty_solver_block(
            stage=kwargs.get("stage"), status=status, message=f"{status} (not proven)",
        )
        return {
            "solver": block, "selected_panel_indices": [], "selected_panel_ids": [],
            "selected_y": {}, "panel_count": None, "radar_ii_panel_count": None,
            "objective_name": "total_panel_count", "variable_count": 1,
            "constraint_count": 1, "infeasibility_evidence": None,
        }

    return fake_solve


# ---------------------------------------------------------------------------
# 1 — 新项目默认算法版本全部可执行
# ---------------------------------------------------------------------------


def test_new_project_default_algorithm_selection_is_executable():
    defaults = json.loads(DEFAULTS_PATH.read_text(encoding="utf-8"))
    registry = build_default_algorithm_registry(defaults)
    selection = default_algorithm_selection()
    assert {"corridor_model", "corridor_gap_analyzer", "site_planner"} <= set(selection)
    for algorithm_type, entry in selection.items():
        manifest = registry.manifest(
            algorithm_type, entry["algorithm_id"], entry["version"],
        )
        assert manifest.algorithm_id == entry["algorithm_id"]
        assert manifest.version == entry["version"]
        assert registry.create(
            algorithm_type, entry["algorithm_id"], entry["version"],
            entry.get("parameters") or {},
        ) is not None


# ---------------------------------------------------------------------------
# 2 — Radar-I 可行时不触发 Radar-II
# ---------------------------------------------------------------------------


def test_radar_i_feasible_never_escalates():
    samples = [sample(index, [index * 250.0, 1000.0]) for index in range(5)]
    towers = [tower("T1", [500.0, 0.0]), tower("T2", [-400.0, 500.0])]
    policy = normalize_radar_surveillance_policy(None)
    result = solve(towers, samples, allow_mixed=bool(policy["allow_mixed_radar_types"]))

    assert result["status"] == "optimal_coverage"
    assert result["stage"] == "radar_i_only"
    assert result["radar_ii_panel_count"] == 0
    assert all(item["radar_type"] == RADAR_TYPE_I for item in result["selected_panels"])
    escalation = result["escalation"]
    assert escalation["escalated"] is False
    assert escalation["stage_a_status"] == "optimal"
    assert escalation["escalation_blocked_reason"] == "radar_i_sufficient"
    assert escalation["radar_ii_site_count"] == 0
    assert result["stage_b"] is None


# ---------------------------------------------------------------------------
# 3 / 5 — 证明不可行才升级，且只用既有站址
# ---------------------------------------------------------------------------


def test_proven_infeasibility_escalates_on_existing_sites_only():
    samples = [sample(index, [0.0, 3600.0 + index * 200.0]) for index in range(3)]
    towers = [tower("T1", [0.0, 0.0]), tower("T2", [4000.0, 4000.0])]
    policy = normalize_radar_surveillance_policy(None)
    result = solve(towers, samples, allow_mixed=bool(policy["allow_mixed_radar_types"]))

    assert result["stage"] == "radar_i_plus_radar_ii"
    escalation = result["escalation"]
    assert escalation["escalated"] is True
    assert escalation["stage_a_infeasibility_proven"] is True
    assert escalation["stage_a_status"] == "infeasible"
    assert escalation["stage_a_reason"]
    #: "Radar-II 是既有站址的设备升级方案"这条业务事实必须可审计。
    assert escalation["existing_sites_only"] is True
    assert escalation["new_sites_created"] is False
    assert escalation["lexicographic_objectives"] == [
        "total_panel_count", "radar_ii_panel_count",
    ]

    known_sites = {item["tower_id"] for item in towers}
    selected = result["selected_panels"]
    assert selected
    assert {panel["tower_id"] for panel in selected} <= known_sites
    assert {panel["site_id"] for panel in selected} == {
        f"tower:{panel['tower_id']}" for panel in selected
    }
    assert result["candidate_tower_count"] == len(towers)
    #: II 型面阵确实出现在方案里（否则 Stage B 无意义）。
    assert result["radar_ii_panel_count"] >= 1


# ---------------------------------------------------------------------------
# 4 — 未完成 / 未证明的求解绝不升级
# ---------------------------------------------------------------------------


def test_unfinished_stage_a_never_escalates(monkeypatch):
    samples = [sample(0, [0.0, 1000.0])]
    towers = [tower("T1", [0.0, 0.0])]

    for status in ("time_limit", "node_limit", "solver_error"):
        monkeypatch.setattr(milp_module, "solve", _incomplete_solve(status))
        monkeypatch.setattr(
            milp_module, "solve_with_total_panel_count", _incomplete_solve(status),
        )
        result = solve(towers, samples, allow_mixed=True)
        assert result["stage"] == "radar_i_only", status
        assert result["escalation"]["escalated"] is False, status
        assert result["escalation"]["stage_a_infeasibility_proven"] is False, status
        assert result["radar_ii_panel_count"] == 0, status


def test_unproven_infeasible_status_never_escalates(monkeypatch):
    """``status == "infeasible"`` 但 ``infeasibility_proven is False`` ⇒ 绝不升级。"""

    monkeypatch.setattr(milp_module, "solve", _incomplete_solve("infeasible"))
    monkeypatch.setattr(
        milp_module, "solve_with_total_panel_count", _incomplete_solve("infeasible"),
    )
    result = solve([tower("T1", [0.0, 0.0])], [sample(0, [0.0, 1000.0])], allow_mixed=True)

    assert result["stage"] == "radar_i_only"
    assert result["escalation"]["escalated"] is False
    assert result["escalation"]["stage_a_infeasibility_proven"] is False
    assert result["escalation"]["escalation_blocked_reason"] == (
        "stage_a_infeasible_not_proven_infeasible"
    )


# ---------------------------------------------------------------------------
# 6 — 同一物理塔的多个面阵只算一个独立站址
# ---------------------------------------------------------------------------


def test_two_radar_ii_panels_on_one_tower_are_one_upgraded_site():
    samples = [sample(0, [3500.0, 0.0]), sample(1, [-3500.0, 0.0])]
    towers = [tower("T1", [0.0, 0.0])]
    result = solve(towers, samples, allow_mixed=True)

    assert result["stage"] == "radar_i_plus_radar_ii"
    assert result["radar_ii_panel_count"] == 2
    assert result["selected_panel_count"] == 2
    #: 两个面阵同塔 ⇒ 只有 1 个独立站址、1 座需要升级的铁塔。
    assert result["selected_tower_count"] == 1
    assert result["selected_tower_ids"] == ["T1"]
    assert result["escalation"]["radar_ii_site_count"] == 1
    assert result["escalation"]["radar_ii_site_ids"] == ["T1"]


def test_distinct_site_count_never_double_counts_one_tower():
    samples = [
        sample(0, [1000.0, 0.0]), sample(1, [-1000.0, 0.0]),
        sample(2, [0.0, 1000.0]), sample(3, [0.0, -1000.0]),
    ]
    result = solve([tower("T1", [0.0, 0.0])], samples)

    #: 四个方向只需 2 个 90° 面阵（相邻方向可由同一面阵覆盖），但无论如何
    #: **多个面阵都挂在同一座塔上** ⇒ 独立站址仍然只有 1 个。
    assert result["selected_panel_count"] >= 2
    assert result["selected_panel_count"] > result["selected_tower_count"]
    assert result["selected_tower_count"] == 1
    #: 逐点独立站址数按 tower_id 去重：4 个面阵仍只是 1 个站址
    #: （否则 land 的"2 个不同站址"要求会被同塔多面阵虚假满足）。
    for entry in result["validation"]["samples"]:
        assert entry["actual_distinct_site_count"] == 1


# ---------------------------------------------------------------------------
# 8 — Stage B 的 validation / refinement 继续有效
# ---------------------------------------------------------------------------


def test_stage_b_keeps_validation_and_refinement_chain():
    samples = [sample(index, [0.0, 3600.0 + index * 200.0]) for index in range(3)]
    result = solve([tower("T1", [0.0, 0.0])], samples, allow_mixed=True)

    assert result["stage"] == "radar_i_plus_radar_ii"
    validation = result["validation"]
    assert validation is not None
    assert validation["samples"]
    assert result["refinement_rounds"]
    assert result["stage_b"]["b1"]["solver"]["status"] == "optimal"
    assert result["stage_b"]["b2"]["solver"]["status"] == "optimal"
    assert result["stage_b"]["b2"]["solver"]["lexicographic_constraint"][
        "total_panel_count_fixed_to"
    ] == float(result["stage_b"]["b1"]["panel_count"])
    assert result["stage_b"]["authoritative"] is not None


# ---------------------------------------------------------------------------
# 9 — Radar overlay 覆盖两种型号并如实披露未知型号
# ---------------------------------------------------------------------------


def test_radar_overlay_draws_both_types_and_reports_unknown_ones():
    radar = {
        "selected_panels": [
            {"panel_id": "P1", "tower_id": "T1", "radar_type": "radar_i", "azimuth_deg": 0.0},
            {"panel_id": "P2", "tower_id": "T2", "radar_type": "radar_ii", "azimuth_deg": 90.0},
            {"panel_id": "P3", "tower_id": "T3", "radar_type": "radar_ii", "azimuth_deg": 180.0},
            {"panel_id": "P4", "tower_id": "T4", "radar_type": "unknown_type", "azimuth_deg": 270.0},
        ],
        "stage": "radar_i_plus_radar_ii",
        "radar_ii_panel_count": 2,
    }
    panels, skipped = radar_overlay_panels(radar)

    assert {panel["radar_type"] for panel in panels} == {RADAR_TYPE_I, RADAR_TYPE_II}
    assert len(panels) == 3
    #: 未知型号绝不静默计入，也不静默丢掉：如实计数。
    assert skipped == 1
    assert radar_overlay_panels({}) == ([], 0)


# ---------------------------------------------------------------------------
# 7 / 10 — Radar-II 不是 RID cooperative，P17 仍是 planning proxy
# ---------------------------------------------------------------------------


def test_radar_planning_proxy_semantics_are_unchanged():
    assert "not_measured" in RADAR_DETECTION_PROXY_SEMANTICS
    assert RADAR_DETECTION_PROXY_DISTANCE_STATUS == "planning_proxy_validated"
    assert RADAR_DETECTION_PROXY_SERVICE_COVERAGE_STATUS == "not_declared_by_layout_proxy"


def test_radar_ii_is_never_a_rid_cooperative_service():
    from cns_planner.domain.cns_service_registry import (
        SERVICE_KEY_RADAR_NONCOOPERATIVE, SERVICE_KEY_RID_COOPERATIVE, SERVICE_REGISTRY,
    )

    assert SERVICE_KEY_RADAR_NONCOOPERATIVE != SERVICE_KEY_RID_COOPERATIVE
    radar = SERVICE_REGISTRY[SERVICE_KEY_RADAR_NONCOOPERATIVE]
    rid = SERVICE_REGISTRY[SERVICE_KEY_RID_COOPERATIVE]
    assert radar["provider_model"] == "directional_sensor"
    assert rid["provider_model"] != radar["provider_model"]
    #: Radar 侧的分级策略绝不进入 RID 条目：RID 是合作监视，没有 panel / 型号概念。
    assert "planning_policy" not in rid
    assert radar["planning_policy"]["allowed_radar_types"] == ["radar_i", "radar_ii"]
    assert radar["planning_policy"]["allow_range_relaxation"] is False


# ---------------------------------------------------------------------------
# 11 — 新项目默认启用，历史 payload 保持 I-only
# ---------------------------------------------------------------------------


def test_new_project_policy_enables_escalation_while_legacy_payload_stays_i_only():
    fresh = normalize_radar_surveillance_policy(None)
    assert fresh["allow_mixed_radar_types"] is True
    assert fresh["allow_automatic_radar_ii_escalation"] is True
    assert fresh["allowed_radar_types"] == ["radar_i", "radar_ii"]

    legacy = normalize_radar_surveillance_policy({
        "status": "confirmed", "confirmed": True,
        "allowed_radar_types": ["radar_i"], "allow_mixed_radar_types": True,
        "optimization_sample_spacing_m": 25.0, "validation_sample_spacing_m": 5.0,
        "max_refinement_rounds": 3,
    })
    assert legacy["allow_mixed_radar_types"] is False
    assert legacy["allow_automatic_radar_ii_escalation"] is False
    assert legacy["allowed_radar_types"] == ["radar_i"]
    assert legacy["escalation_policy_id"] == "radar_i_only_frozen"

    opt_out = normalize_radar_surveillance_policy({
        "allow_automatic_radar_ii_escalation": False,
        "allowed_radar_types": ["radar_i"],
    })
    assert opt_out["allow_mixed_radar_types"] is False
    assert opt_out["escalation_policy_id"] == "radar_i_only_frozen"
