from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cns_planner.algorithms.registry import default_algorithm_selection
from cns_planner.api.router import ApiRouter
from cns_planner.application.project_state import blank_project, normalize_project
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.compatibility.catalog import capability_catalog
from cns_planner.compatibility.delete_gates import validate_delete_gate_report
from cns_planner.compatibility.project_adapter import read_existing_legacy


DEFAULTS = Path("cns_planner/config/defaults.json")
CASES = Path("tests/fixtures/compatibility/legacy_archive_cases.json")
LEGACY_SELECTION_TYPES = {"route_planner", "coverage_planner", "cns_gap_analyzer"}
LEGACY_RESULT_KEYS = {"coverage", "cns_gap_analysis", "cns_gap_analysis_v2", "cns_site_plan"}


class NoopGrid:
    def generate(self, bbox):
        return {"status": "passed", "bbox": bbox, "cells": []}


def _cases():
    return json.loads(CASES.read_text(encoding="utf-8"))


def _apply_case(workflow, overlay):
    for key, value in overlay.items():
        if key == "algorithm_selection":
            workflow.state[key].update(deepcopy(value))
        else:
            workflow.state[key] = deepcopy(value)
    workflow.save()


def test_new_project_has_only_canonical_selection_and_no_legacy_result_state():
    selection = default_algorithm_selection()
    assert not (LEGACY_SELECTION_TYPES & set(selection))
    assert selection["layered_route_planner"]["algorithm_id"] == "layered_risk_aware_theta_star_v2"
    assert selection["site_planner"]["algorithm_id"] == "corridor_reuse_first_site_planner_v2"
    state = blank_project(json.loads(DEFAULTS.read_text(encoding="utf-8")))
    assert not (LEGACY_RESULT_KEYS & set(state))
    assert not ({"coverage", "cns_gap", "cns_gap_v2", "cns_site_plan"} & set(state["result_statuses"]))


def test_normalize_preserves_explicit_legacy_selection_and_results_without_backfill():
    original = blank_project(json.loads(DEFAULTS.read_text(encoding="utf-8")))
    case = _cases()["C_layered_route_planner_v1"]
    original["algorithm_selection"].update(deepcopy(case["algorithm_selection"]))
    original["cns_site_plan"] = {"status": "proposal_ready", "sentinel": "keep"}
    normalized = normalize_project(deepcopy(original), NoopGrid())
    assert normalized["algorithm_selection"]["layered_route_planner"] == case["algorithm_selection"]["layered_route_planner"]
    assert normalized["cns_site_plan"] == {"status": "proposal_ready", "sentinel": "keep"}
    missing = blank_project({})
    normalize_project(missing, NoopGrid())
    assert not (LEGACY_RESULT_KEYS & set(missing))


def test_all_legacy_archive_fixtures_open_read_export_and_roundtrip(tmp_path):
    for name, overlay in _cases().items():
        target = tmp_path / name / "project.json"
        target.parent.mkdir()
        workflow = WorkflowService(target, DEFAULTS)
        _apply_case(workflow, overlay)

        reopened = WorkflowService(target, DEFAULTS)
        exported = json.loads(reopened.export_project())
        reopened.save()
        again = WorkflowService(target, DEFAULTS)

        for key, expected in overlay.items():
            if key == "algorithm_selection":
                for algorithm_type, selection in expected.items():
                    assert again.state[key][algorithm_type] == selection
            else:
                assert key in again.state
        assert exported["project"]["project_id"] == reopened.state["project"]["project_id"]
        if name.startswith("A_"):
            assert exported["operational_routes"][0]["authoritative"] is False
            route_export = json.loads(reopened.export_routes())
            assert route_export["features"][0]["properties"]["compatibility"] is True

        if "coverage" in overlay:
            view = read_existing_legacy(reopened.state, "coverage", "CoveragePlannerV1")
            assert view["existing"] is True and view["authoritative"] is False
            assert exported["compatibility_views"]["coverage"]["authoritative"] is False
        if "cns_gap_analysis" in overlay:
            view = read_existing_legacy(reopened.state, "cns_gap_analysis", "CNSGapAnalyzerV1")
            assert view["authoritative"] is False
        if "cns_site_plan" in overlay:
            view = read_existing_legacy(reopened.state, "cns_site_plan", "ReuseFirstSitePlannerV1")
            assert view["authoritative"] is False

    empty = WorkflowService(tmp_path / "H_empty_existing_cns_facilities" / "project.json", DEFAULTS)
    assert empty.state["cns_existing_baseline"]["knowledge_status"] == "not_declared"


def test_unresolvable_saved_selection_falls_back_to_frozen_baseline_without_write(tmp_path):
    """旧项目里 adapter 无法解析的旧 selection 不能被改写，也不能让项目打不开。"""

    target = tmp_path / "unresolvable" / "project.json"
    target.parent.mkdir()
    workflow = WorkflowService(target, DEFAULTS)
    workflow.state["algorithm_selection"]["route_planner"] = {
        "algorithm_type": "route_planner", "algorithm_id": "route_planner_v9",
        "version": "9.0", "parameters": {},
    }
    workflow.state["operational_routes"] = [{"route_id": "R-OLD", "status": "passed"}]
    workflow.save()
    saved_selection = deepcopy(workflow.state["algorithm_selection"]["route_planner"])

    # 仍可 open：assembly 用兼容 adapter 解析出冻结基线，而不是抛 AlgorithmNotFoundError。
    reopened = WorkflowService(target, DEFAULTS)
    selection = reopened.compatibility_selection.selection("route_planner")
    assert selection["algorithm_id"] == "route_planner_v1"
    assert selection["selection_source"] == "frozen_compatibility_baseline_unresolvable_saved_selection"
    # 旧 selection 与旧结果原样保留，绝不被覆盖。
    assert reopened.state["algorithm_selection"]["route_planner"] == saved_selection
    assert reopened.state["operational_routes"] == [{"route_id": "R-OLD", "status": "passed"}]
    view = reopened.compatibility_route_snapshot()
    # 不可解析的旧 selection 只产出 empty compatibility view：它既不改写旧值，
    # 也不把旧 operational_routes 冒充成当前 compatibility 结果。
    assert view["existing"] is False and view["authoritative"] is False
    assert view["source"] == "empty_compatibility_view"
    assert read_existing_legacy(reopened.state, "operational_routes", "RoutePlannerV1")["existing"] is True


def test_compatibility_compute_is_runtime_only_and_aliases_are_marked(tmp_path):
    workflow = WorkflowService(tmp_path / "runtime.json", DEFAULTS)
    algorithm_note = (workflow.state.get("algorithm_selection") or {}).get("route_planner")
    assert algorithm_note is None, "新项目不得持久化 legacy route_planner selection"
    workflow.snapshot()  # materialise pre-existing runtime-only derived caches
    before = deepcopy(workflow.state)
    response = workflow.analyze_cns_gaps()
    assert workflow.state == before
    assert response["cns_gap_analysis"]["persistent_write"] is False
    assert "cns_gap_analysis" not in workflow.state

    context = SimpleNamespace(workflow=workflow, data=SimpleNamespace(hard_constraints=[]))
    router = ApiRouter(context)
    catalog = router.get("/api/compatibility/catalog", {}, {}).data
    assert catalog == capability_catalog()
    alias = router.get("/api/cns-gaps", {}, {}).data
    assert alias["compatibility"] is True
    assert alias["deprecated"] is True
    assert alias["authoritative"] is False
    assert alias["persistent_write"] is False
    assert alias["namespace"] == "/api/compatibility/cns-gap-analysis-v1"


def test_runtime_compatibility_selection_updates_only_the_session(tmp_path):
    """归档规划器参数只能写运行期 compatibility selection，绝不写 ProjectState。"""

    target = tmp_path / "selection" / "project.json"
    target.parent.mkdir()
    workflow = WorkflowService(target, DEFAULTS)
    context = SimpleNamespace(workflow=workflow, data=SimpleNamespace(hard_constraints=[]))
    router = ApiRouter(context)
    before = deepcopy(workflow.state)

    response = router.post("/api/compatibility/selection", {
        "algorithm_type": "route_planner",
        "algorithm_id": "risk_aware_route_planner_v2",
        "parameters": {"risk_weight_lambda": 1.5, "unknown_risk_policy": "penalize",
                       "unknown_penalty_index": 0.5},
    }).data
    assert response["capability_id"] == "RiskAwareRoutePlannerV2"
    assert response["persistent_write"] is False
    assert response["authoritative"] is False
    assert workflow.state == before, "运行期 compatibility 参数不得写项目状态"
    assert "route_planner" not in workflow.state.get("algorithm_selection", {})
    selection = workflow.compatibility_selection.selection("route_planner")
    assert selection["selection_source"] == "runtime_compatibility_selection"
    assert selection["parameters"]["risk_weight_lambda"] == 1.5
    # 后续 compatibility 试算必须按运行期覆盖现取实例，而不是沿用进程启动时的基线。
    assert workflow.compatibility_selection.create("route_planner").algorithm_id == "risk_aware_route_planner_v2"
    assert workflow.compatibility_selection.create("route_planner").parameters["risk_weight_lambda"] == 1.5

    updated = WorkflowService(target, DEFAULTS)
    assert updated.compatibility_selection.selection("route_planner")["algorithm_id"] == "route_planner_v1"
    assert updated.compatibility_selection.selection("route_planner")["selection_source"] == "frozen_compatibility_baseline"

    with pytest.raises(ValueError, match="compatibility capability"):
        router.post("/api/compatibility/selection", {
            "algorithm_type": "route_planner", "algorithm_id": "route_planner_v9",
            "parameters": {},
        })


def test_catalog_and_delete_gate_report_are_complete():
    ids = {item["capability_id"] for item in capability_catalog()["items"]}
    assert ids == {
        "RoutePlannerV1", "RiskAwareRoutePlannerV2", "LayeredRoutePlannerV1",
        "CoveragePlannerV1", "CNSGapAnalyzerV1", "CNSGapAnalyzerV2",
        "ReuseFirstSitePlannerV1", "RoutePlannerV3",
    }
    report = validate_delete_gate_report()
    assert report["mode"] == "report_only_no_deletion"
    assert {item["capability_id"] for item in report["items"]} >= {
        "RoutePlannerV1", "RiskAwareRoutePlannerV2", "LayeredRoutePlannerV1",
        "RoutePlannerV3", "CoveragePlannerV1", "CNSGapAnalyzerV1",
        "ReuseFirstSitePlannerV1", "legacy facade/UI",
    }
