"""Benchmark fixture and independent RouteQualityEvaluator tests.

These tests treat the planners as immutable: where a case cannot be satisfied, the
expectation is an explicit ``not_applicable`` / rejection, never a planner change.
"""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest

from cns_planner.algorithms.route_planner import RoutePlannerV1
from cns_planner.benchmark import fixtures
from cns_planner.benchmark.quality import (
    evaluate_constraint_feasibility, evaluate_route_quality, polyline_metrics,
)
from cns_planner.route_planner.risk_aware_v2 import RiskAwareRoutePlannerV2


TOOL_PATH = Path("tools/route_planning_baseline.py")
REQUIRED_CASES = {
    "open_space",
    "single_obstacle",
    "concave_obstacle",
    "narrow_passage",
    "disconnected_allowed_airspace",
    "risk_tradeoff",
    "endpoint_near_boundary",
    "malformed_constraint",
    "zigzag_open_grid_bias",
    "bbox_overblocking_demo",
}


def load_tool():
    spec = importlib.util.spec_from_file_location("route_planning_baseline_tool", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tool():
    return load_tool()


@pytest.fixture(scope="module")
def pack(tool):
    return tool.build_pack()


def segment(lon0, lat0, lon1, lat1):
    return [[lon0, lat0], [lon1, lat1]]


# --------------------------------------------------------------------------------------
# RouteQualityEvaluator
# --------------------------------------------------------------------------------------


def test_evaluator_measures_every_required_metric_for_a_simple_polyline():
    from cns_planner.benchmark.geodesy import geodesic_distance_m

    path = [[0.0, 0.0], [0.001, 0.0], [0.001, 0.001]]
    evaluation = evaluate_route_quality(
        {"route_id": "R1"}, {"route_id": "R1", "status": "passed", "path": path},
    )
    quality = evaluation["quality"]
    legs = [geodesic_distance_m(a, b) for a, b in zip(path, path[1:])]
    # Values come from the geodesic backend, not a spherical approximation.
    assert quality["path_length_m"] == pytest.approx(sum(legs))
    assert quality["segment_count"] == 2
    assert quality["turn_count"] == 1
    assert quality["total_heading_change_deg"] == pytest.approx(90.0, abs=0.01)
    assert quality["max_heading_change_deg"] == pytest.approx(90.0, abs=0.01)
    assert quality["min_segment_m"] == pytest.approx(min(legs))
    assert quality["detour_factor"] == pytest.approx(
        sum(legs) / geodesic_distance_m(path[0], path[-1]), rel=1e-6,
    )


def test_straight_path_has_zero_turns_and_unit_detour():
    evaluation = evaluate_route_quality(
        {"route_id": "R1"},
        {"route_id": "R1", "status": "passed", "path": segment(0.0, 0.0, 0.01, 0.0)},
    )
    assert evaluation["quality"]["turn_count"] == 0
    assert evaluation["quality"]["total_heading_change_deg"] == 0
    assert evaluation["quality"]["max_heading_change_deg"] == 0
    assert evaluation["quality"]["detour_factor"] == pytest.approx(1.0)


def test_heading_change_normalizes_reversal_to_one_eighty_and_ignores_wrap():
    # North then east then north: both turns are 90 degrees, not 270.
    path = [[0.0, 0.0], [0.0, 0.001], [0.001, 0.001], [0.001, 0.002]]
    metrics = polyline_metrics(path)
    assert metrics["turn_count"] == 2
    assert metrics["total_heading_change_deg"] == pytest.approx(180.0)
    # West then east is a true 180-degree reversal, not a 0-degree turn.
    reversal = polyline_metrics([[0.0, 0.0], [0.001, 0.0], [0.0, 0.0]])
    assert reversal["segment_count"] == 2
    assert reversal["max_heading_change_deg"] == pytest.approx(180.0)


def test_failed_result_is_measured_without_inventing_quality():
    evaluation = evaluate_route_quality(
        {"route_id": "R1"}, {"route_id": "R1", "status": "failed", "path": []},
    )
    assert evaluation["status"] == "failed"
    assert evaluation["quality"]["path_length_m"] == 0
    assert evaluation["quality"]["detour_factor"] == 1.0
    assert evaluation["allowed_airspace_feasibility"]["status"] == "failed"
    assert evaluation["allowed_airspace_feasibility"]["source"] == "planner_result_status_only_not_rejudged"


def test_evaluator_reads_v2_risk_metrics_and_never_recomputes_risk():
    path = [[0.0, 0.0], [0.001, 0.0]]
    result = {
        "route_id": "R1", "status": "passed", "path": path,
        "algorithm_id": "risk_aware_route_planner_v2", "algorithm_version": "2.0",
        "distance_m": 111.19, "risk_exposure_index_m": 5.5, "mean_risk_index": 0.05,
        "max_risk_index": 0.2, "optimization_cost": 122.3, "risk_component": "overall",
        "risk_weight_lambda": 2.0,
    }
    evaluation = evaluate_route_quality({"route_id": "R1"}, result)
    assert evaluation["risk_metrics"]["risk_exposure_index_m"] == 5.5
    assert evaluation["risk_metrics"]["max_risk_index"] == 0.2
    assert evaluation["risk_metrics"]["risk_weight_lambda"] == 2.0
    assert evaluation["risk_recomputed"] is False
    assert evaluation["risk_metrics_source"] == "read_from_existing_planner_output"


def test_evaluator_never_writes_into_the_planner_result_or_ranks():
    path = [[0.0, 0.0], [0.001, 0.0]]
    result = {"route_id": "R1", "status": "passed", "path": path}
    snapshot = deepcopy(result)
    evaluation = evaluate_route_quality({"route_id": "R1"}, result)
    assert result == snapshot
    assert evaluation["verdicts"] == {
        "automatically_ranked": False, "automatically_scored": False, "preferred_algorithm": None,
    }
    assert "quality" not in result


def test_runtime_is_reported_only_and_never_affects_metrics():
    path = [[0.0, 0.0], [0.001, 0.0]]
    first = evaluate_route_quality({"route_id": "R1"}, {"route_id": "R1", "status": "passed", "path": path}, runtime_ms=1.0)
    second = evaluate_route_quality({"route_id": "R1"}, {"route_id": "R1", "status": "passed", "path": path}, runtime_ms=999.0)
    assert first["quality"] == second["quality"]
    assert first["runtime_ms"] != second["runtime_ms"]
    assert "仅用于报告" in first["runtime_note"]


def test_constraint_feasibility_uses_the_authoritative_validator():
    assert evaluate_constraint_feasibility([])["status"] == "accepted"
    assert evaluate_constraint_feasibility(None)["status"] == "accepted"
    well_formed = evaluate_constraint_feasibility([{"bbox": [0.1, 0.1, 0.2, 0.2]}])
    assert well_formed["status"] == "well_formed"
    assert "constraint_validation" in well_formed["validator"]
    rejected = evaluate_constraint_feasibility([{"bbox": [0.2, 0.2, 0.1, 0.1]}])
    assert rejected["status"] == "rejected"
    assert rejected["detail"]


def test_evaluator_handles_malformed_path_without_crashing():
    for path in (None, [], [[0.0, 0.0]], [["a", "b"]], [[0.0, 0.0], [0.0, 0.0]]):
        evaluation = evaluate_route_quality({"route_id": "R1"}, {"route_id": "R1", "status": "passed", "path": path})
        assert evaluation["quality"]["segment_count"] == 0
        assert evaluation["quality"]["path_length_m"] == 0


# --------------------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------------------


def test_every_required_case_exists_and_is_deterministic():
    assert set(fixtures.case_ids()) == REQUIRED_CASES
    assert fixtures.cases() == fixtures.cases()
    copy = fixtures.cases()
    copy[0]["start"][0] = 999.0
    assert fixtures.case("open_space")["start"][0] != 999.0


def test_fixtures_declare_applicability_instead_of_forcing_cases():
    for case in fixtures.cases():
        assert case["expert_question"]
        assert case["planner_changes_allowed"] is False
        assert set(case["applicability"]) == {"route_planner_v1", "risk_aware_route_planner_v2"}
        for entry in case["applicability"].values():
            assert "applicable" in entry
            if entry["applicable"] is False:
                assert entry.get("note")
    malformed = fixtures.case("malformed_constraint")
    assert malformed["applicability"]["route_planner_v1"]["applicable"] is False
    assert malformed["applicability"]["risk_aware_route_planner_v2"]["applicable"] is False


def test_malformed_payloads_are_all_rejected_by_the_validator():
    from cns_planner.application.constraint_validation import validate_hard_constraints

    payloads = fixtures.malformed_payloads()
    assert len(payloads) >= 8
    for name, payload in payloads:
        with pytest.raises(ValueError):
            validate_hard_constraints(payload)


# --------------------------------------------------------------------------------------
# Evidence pack
# --------------------------------------------------------------------------------------


def test_pack_covers_every_case_and_both_planners(pack):
    assert [item["case_id"] for item in pack["cases"]] == list(fixtures.case_ids())
    assert set(pack["planner_manifests"]) == {"route_planner_v1", "risk_aware_route_planner_v2"}
    for item in pack["cases"]:
        assert item["planners"], item["case_id"]
        for block in item["planners"].values():
            assert "status" in block and "reason" in block
            assert block["verdicts"]["automatically_ranked"] is False


def test_pack_reports_not_applicable_for_the_malformed_case_without_invoking_planners(pack):
    item = next(entry for entry in pack["cases"] if entry["case_id"] == "malformed_constraint")
    assert item["input_summary"]["input_boundary_rejection"]
    for block in item["planners"].values():
        assert block["planner_invoked"] is False
        assert block["status"] == "rejected_at_input_boundary"
        assert block["quality"] is None


def test_pack_reports_quality_metrics_and_failure_reasons(pack):
    for item in pack["cases"]:
        for run_id, block in item["planners"].items():
            if not block["planner_invoked"]:
                continue
            quality = block["quality"]
            for name in (
                "path_length_m", "detour_factor", "segment_count", "turn_count",
                "total_heading_change_deg", "max_heading_change_deg", "min_segment_m",
            ):
                assert name in quality, (item["case_id"], run_id, name)
            assert quality["detour_factor_source"] == "measured_from_published_path"
            assert block["risk_recomputed"] is False
            if block["status"] != "passed":
                assert block["reason"]


def test_pack_lists_manifest_limitations_and_case_input_summaries(pack):
    v1 = pack["planner_manifests"]["route_planner_v1"]
    assert v1["parameter_schema"]["properties"]["grid_size"]["default"] == 56
    assert any("BBOX" in item for item in v1["limitations"])
    v2 = pack["planner_manifests"]["risk_aware_route_planner_v2"]
    assert "airspace_eligibility" in v2["inputs"]
    for item in pack["cases"]:
        summary = item["input_summary"]
        assert summary["workspace_bbox"] and summary["start"] and summary["end"]
        assert "hard_constraint_count" in summary


def test_pack_declares_scope_and_never_recommends_an_algorithm(pack):
    assert pack["planner_changes_allowed"] is False
    assert pack["data_provenance"]["real_world_data"] is False
    assert pack["data_provenance"]["deterministic"] is True
    assert "不自动排名" in pack["scope_statement"]
    assert pack["not_done"]


def test_disconnected_allowed_airspace_makes_v2_fail_without_planner_changes(pack):
    item = next(entry for entry in pack["cases"] if entry["case_id"] == "disconnected_allowed_airspace")
    block = item["planners"]["risk_aware_route_planner_v2"]
    assert block["planner_invoked"] is True
    assert block["status"] == "failed"
    assert block["quality"]["path_length_m"] == 0
    observation = block["quality_expectations"]["observations"]
    assert any(entry["expectation"] == "planner_status==failed" and entry["observed"] is True for entry in observation)


def test_risk_tradeoff_reports_both_lambda_variants_without_preferring_one(pack):
    item = next(entry for entry in pack["cases"] if entry["case_id"] == "risk_tradeoff")
    zero = item["planners"]["risk_aware_route_planner_v2_lambda_0"]
    high = item["planners"]["risk_aware_route_planner_v2_lambda_8"]
    assert zero["effective_parameters"]["risk_weight_lambda"] == 0.0
    assert high["effective_parameters"]["risk_weight_lambda"] == 8.0
    # The λ=8 route trades length for exposure; the pack states both, and ranks neither.
    assert high["quality"]["path_length_m"] > zero["quality"]["path_length_m"]
    assert high["risk_metrics"]["risk_exposure_index_m"] < zero["risk_metrics"]["risk_exposure_index_m"]
    assert zero["verdicts"]["preferred_algorithm"] is None
    assert high["verdicts"]["preferred_algorithm"] is None


def test_written_pack_has_json_and_markdown_in_ignored_outputs(tmp_path, tool):
    built = tool.build_pack(["open_space", "malformed_constraint"])
    json_path, markdown_path = tool.write_pack(built, tmp_path / "route_baseline")
    assert json_path.is_file() and markdown_path.is_file()
    document = json.loads(json_path.read_text(encoding="utf-8"))
    assert document["artifacts"]["json"].endswith("route_planning_baseline.json")
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "不做自动排名、评分或算法推荐" in markdown
    assert "当前规划器 Manifest" in markdown
    assert "malformed_constraint" in markdown


def test_pack_is_reproducible_for_case_ordering_and_quality_values(tool):
    first = tool.build_pack(["open_space", "narrow_passage"])
    second = tool.build_pack(["open_space", "narrow_passage"])
    for left, right in zip(first["cases"], second["cases"]):
        assert left["input_fingerprint"] == right["input_fingerprint"]
        for run_id in left["planners"]:
            assert left["planners"][run_id]["status"] == right["planners"][run_id]["status"]
            assert left["planners"][run_id]["quality"] == right["planners"][run_id]["quality"]


def test_benchmark_does_not_execute_planners_for_inapplicable_runs(tool, monkeypatch):
    calls = {"v1": 0, "v2": 0}
    original_v1 = RoutePlannerV1.plan
    original_v2 = RiskAwareRoutePlannerV2.plan

    def v1_plan(self, *args, **kwargs):
        calls["v1"] += 1
        return original_v1(self, *args, **kwargs)

    def v2_plan(self, *args, **kwargs):
        calls["v2"] += 1
        return original_v2(self, *args, **kwargs)

    monkeypatch.setattr(RoutePlannerV1, "plan", v1_plan)
    monkeypatch.setattr(RiskAwareRoutePlannerV2, "plan", v2_plan)
    tool.build_pack(["malformed_constraint"], runs=2, warmup=1)
    assert calls == {"v1": 0, "v2": 0}
    tool.build_pack(["open_space"], runs=2, warmup=1)
    # warmup(1) + runs(2) invocations per planner run; V2 has one run for this case.
    assert calls["v1"] == 3 and calls["v2"] == 3
