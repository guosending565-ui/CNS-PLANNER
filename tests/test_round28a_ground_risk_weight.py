"""Round 2.8-A —— 地面风险权重（land_relative_risk_baseline）的定向回归。

覆盖用户要求的 10 项中的参数语义部分：

1. 参数**真正进入** planner（不是只改源码常量）；
2. 参数修改**改变 fingerprint**；
3. 保存 / 重开保持；
6. 提高风险权重**不修改禁飞区语义**（不是把陆地设为禁行）；
9. 航路变化按既有 invalidation 规则使 CNS 下游 stale。
10. C/RID 可基于新航路重算（由 Round 2.8 的 P16/P17 用例覆盖，这里只验证失效链）。
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.planning_exposure import (
    ENGINEERING_SUGGESTED_LAND_RELATIVE_RISK_BASELINE,
    PLANNING_FACTOR_FORMULA,
    planning_exposure_is_active,
    planning_exposure_policy_fingerprint,
    normalize_planning_exposure_policy,
)
from cns_planner.domain.cns_service_contract import SERVICE_KEY_COMMUNICATION

DEFAULTS = Path("cns_planner/config/defaults.json")


def policy(baseline: float, *, confirmed: bool = True):
    return {
        "enabled": True,
        "confirmed": confirmed,
        "land_relative_risk_baseline": baseline,
        "land_min_surface_elevation_m": 0.0,
        "source": "round28a_test_fixture",
        "evidence": {"kind": "test", "value": baseline},
    }


def test_suggested_value_and_formula_are_the_documented_ones():
    """首轮建议值仍是 0.08，且只作为 UI 预填；公式只有一种写法。"""

    assert ENGINEERING_SUGGESTED_LAND_RELATIVE_RISK_BASELINE == pytest.approx(0.08)
    assert PLANNING_FACTOR_FORMULA == "planning_population_factor = (1-b)*p + b*L"
    #: 未确认的 policy 绝不生效（0.08 不是默认值）。
    assert planning_exposure_is_active(normalize_planning_exposure_policy(None)) is False
    assert planning_exposure_is_active(
        normalize_planning_exposure_policy(policy(0.08, confirmed=False))
    ) is False


def test_weight_enters_planner_and_changes_the_policy_fingerprint():
    """① 参数真正进入 planner；② 参数变化改变 fingerprint。"""

    baseline = normalize_planning_exposure_policy(policy(0.08))
    raised = normalize_planning_exposure_policy(policy(0.30))
    assert planning_exposure_is_active(baseline) is True
    assert planning_exposure_is_active(raised) is True
    assert planning_exposure_policy_fingerprint(baseline) != (
        planning_exposure_policy_fingerprint(raised)
    )


def test_weight_is_consumed_as_the_population_factor_input(tmp_path):
    """① 权重确实改变了"规划用人口因子"这一 planner 输入，并进入派生风险场。

    直接把派生场算出来比较（而不是依赖 P16 合成 fixture 的 surface 事实）：
    同一份因子/分类输入下，b 从 0.08 提到 0.30，逐格 ``planning_population_factor``
    必须按 ``(1-b)*p + b*L`` 改变；陆地格抬升、海面格下降。
    """

    from cns_planner.domain.planning_exposure import resolve_planning_exposure

    grid = {"status": "passed", "level": 1, "cells": [
        {"grid_id": "LAND", "bbox": [0.0, 0.0, 0.001, 0.001]},
        {"grid_id": "SEA", "bbox": [0.0, 0.001, 0.001, 0.002]},
    ]}
    population = {"status": "passed", "cells": {
        "LAND": {"normalized_population_factor": 0.2},
        "SEA": {"normalized_population_factor": 0.8},
    }}
    surface = {"LAND": "land", "SEA": "sea"}

    def factors(baseline):
        attribute = resolve_planning_exposure(
            grid=grid, policy=policy(baseline), population_attribute=population,
            terrain_attribute={},
            normalized_population_factors={"LAND": 0.2, "SEA": 0.8},
            surface_class_by_grid_id=surface,
        )
        return {grid_id: (cell or {}).get("planning_population_factor")
                for grid_id, cell in (attribute.get("cells") or {}).items()}

    low = factors(0.08)
    high = factors(0.30)
    assert low["LAND"] == pytest.approx(0.92 * 0.2 + 0.08)
    assert low["SEA"] == pytest.approx(0.92 * 0.8)
    assert high["LAND"] == pytest.approx(0.70 * 0.2 + 0.30)
    assert high["SEA"] == pytest.approx(0.70 * 0.8)
    assert high["LAND"] > low["LAND"], "陆地格的规划人口因子必须随权重上升"
    assert high["SEA"] < low["SEA"], "海面格的人口相对差异按 (1-b) 收缩，绝不设为 0"
    assert high["SEA"] > 0.0, "海面风险绝不被设为绝对 0"


def test_save_and_reopen_keeps_the_weight(tmp_path):
    """③ 保存 / 重开保持该参数。"""

    store = tmp_path / "project.json"
    workflow = WorkflowService(store, DEFAULTS)
    workflow.set_planning_exposure_policy({"planning_exposure_policy": policy(0.30)})
    workflow.save()
    reopened = WorkflowService(store, DEFAULTS)
    saved = reopened.planning_exposure_policy()
    assert saved.get("land_relative_risk_baseline") == pytest.approx(0.30)
    assert saved.get("confirmed") is True
    assert saved.get("enabled") is True


def test_policy_change_stales_the_layered_route_chain(tmp_path):
    """⑨ 参数变化按既有 invalidation 规则使 layered candidate / profile / validation stale。"""

    from test_corridor_site_planner_v2 import configured

    workflow = configured(tmp_path)
    workflow.set_planning_exposure_policy({"planning_exposure_policy": policy(0.08)})
    workflow.state["layered_route_candidates"] = {
        "items": [{"candidate_id": "LRC-TEST", "status": "candidate"}],
        "active_candidate_id": "LRC-TEST",
    }
    workflow.state["route_risk_profiles"] = {
        "items": [{"profile_id": "RRP-TEST", "status": "passed"}],
    }
    workflow.set_planning_exposure_policy({"planning_exposure_policy": policy(0.30)})
    statuses = workflow.state.get("result_statuses") or {}
    assert statuses.get("layered_route_candidate") == "stale"
    assert statuses.get("route_risk_profile") == "stale"


def test_raising_the_weight_never_makes_land_a_no_go_area():
    """⑥ 提高地面风险权重只改**代价**，绝不改禁飞区 / 可行性语义。"""

    from test_corridor_site_planner_v2 import configured

    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        workflow = configured(Path(folder))
        before = deepcopy({
            key: workflow.state.get(key) for key in (
                "regulatory_constraints", "restricted_area_declarations",
                "restricted_areas", "planning_constraint_field_policy",
            )
        })
        workflow.set_planning_exposure_policy({"planning_exposure_policy": policy(0.40)})
        after = {
            key: workflow.state.get(key) for key in before
        }
        assert after == before, "地面风险权重绝不能改写禁飞区 / 受限空域 / 可行性语义"
        semantics = (workflow.planning_exposure_policy() or {}).get("semantics") or {}
        assert semantics.get("land_sea_from_canonical_surface_facts_first") is True
        assert semantics.get("coastal_uncertain_is_never_guessed_as_sea") is True
        assert semantics.get("unknown_surface_is_unresolved") is True


def test_communication_service_identity_is_untouched_by_the_weight():
    """参数只影响风险代价，不改任何服务身份 / 候选语义。"""

    from test_corridor_site_planner_v2 import configured

    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        workflow = configured(Path(folder))
        plan_before = workflow.evaluate_cns_corridor_site_plan()["cns_corridor_site_plan"]
        keys_before = {
            (item.get("service_key") or item.get("device_service_key"))
            for item in plan_before.get("candidate_actions") or []
        }
        assert SERVICE_KEY_COMMUNICATION in keys_before
        workflow.set_planning_exposure_policy({"planning_exposure_policy": policy(0.40)})
        workflow.evaluate_cns_corridor()
        workflow.evaluate_cns_corridor_gap()
        plan_after = workflow.evaluate_cns_corridor_site_plan()["cns_corridor_site_plan"]
        keys_after = {
            (item.get("service_key") or item.get("device_service_key"))
            for item in plan_after.get("candidate_actions") or []
        }
        assert keys_after == keys_before
