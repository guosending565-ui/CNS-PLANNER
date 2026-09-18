"""Descriptive route diagnostics and expert-brief generation."""

import importlib.util
import json
from pathlib import Path

import pytest

from cns_planner.benchmark import fixtures
from cns_planner.benchmark.quality import (
    RoutePlanningDiagnostics, grid_path_metrics, polyline_metrics,
)


def load_tool(filename, name):
    path = Path("tools") / filename
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_zigzag_index_has_explicit_normalized_definition():
    straight = polyline_metrics([[0, 0], [0.001, 0], [0.002, 0]])
    zigzag = polyline_metrics([[0, 0], [0.001, 0], [0.001, 0.001], [0.002, 0.001]])
    assert straight["zigzag_index"] == pytest.approx(0)
    assert zigzag["zigzag_index"] > 0
    assert "sum_absolute_heading_change_deg" in zigzag["zigzag_index_definition"]


def test_direction_histogram_counts_all_grid_step_classes():
    path = [
        "MHT4063-L07-C00000010-RP00000010",
        "MHT4063-L07-C00000011-RP00000010",  # E
        "MHT4063-L07-C00000011-RP00000011",  # N
        "MHT4063-L07-C00000010-RP00000010",  # SW
    ]
    result = grid_path_metrics(path)
    assert result["horizontal_step_count"] == 1
    assert result["vertical_step_count"] == 1
    assert result["diagonal_step_count"] == 1
    assert result["direction_histogram"]["E"] == 1
    assert result["direction_histogram"]["N"] == 1
    assert result["direction_histogram"]["SW"] == 1
    assert result["grid_level"] == 7


def test_route_planning_diagnostics_is_read_only_and_descriptive():
    result = {"route_id": "R1", "status": "passed", "path": [[0, 0], [0.01, 0]]}
    before = json.loads(json.dumps(result))
    diagnostics = RoutePlanningDiagnostics.evaluate({"route_id": "R1"}, result, [])
    assert result == before
    assert diagnostics["verdicts"]["preferred_algorithm"] is None
    assert diagnostics["constraint_input_summary"]["hard_constraint_count"] == 0


def test_new_cases_expose_grid_bias_and_bbox_expression_difference():
    zigzag = fixtures.case("zigzag_open_grid_bias")
    bbox = fixtures.case("bbox_overblocking_demo")
    assert "格网" in zigzag["description"]
    assert bbox["hard_constraints"][0]["bbox"]
    assert bbox["demonstration"]["hypothetical_polygon_not_consumed_by_planner"]
    assert bbox["demonstration"]["claim_limit"] == "does_not_assert_polygon_is_the_final_solution"


@pytest.fixture(scope="module")
def diagnostics_tool():
    return load_tool("route_planning_diagnostics.py", "route_planning_diagnostics_test")


def test_lambda_sweep_reports_required_values_without_recommendation(diagnostics_tool):
    result = diagnostics_tool.lambda_sensitivity()
    assert [item["risk_weight_lambda"] for item in result["rows"]] == [0, 0.5, 1, 2, 4, 8]
    assert result["automatic_recommendation"] is None
    for row in result["rows"]:
        for field in ("status", "length_m", "detour_factor", "turn_count",
                      "total_heading_change_deg", "runtime_ms", "path_fingerprint"):
            assert field in row


def test_grid_sensitivity_uses_requested_existing_levels(diagnostics_tool):
    result = diagnostics_tool.grid_sensitivity()
    assert [item["requested_grid_level"] for item in result["rows"]] == [6, 7, 8]
    assert [item["actual_grid_level"] for item in result["rows"]] == [6, 7, 8]
    assert len({tuple(item["cell_size_degrees"]) for item in result["rows"]}) == 3
    assert result["automatic_recommendation"] is None


def test_zigzag_case_exposes_eight_neighbour_direction_bias(diagnostics_tool):
    """The diagnostic scenario must actually show the 8-neighbour bias it claims.

    The route bearing is ~29 degrees from east, so the grid path decomposes into pure
    E/NE steps: two of eight directions.  Coarser grids need fewer but longer steps and
    zigzag more; finer grids track the bearing with more, shorter steps.  This is a
    regression lock on the *finding*, not a score and not a recommendation.
    """

    rows = {row["actual_grid_level"]: row for row in diagnostics_tool.grid_sensitivity()["rows"]}
    for level in (6, 7, 8):
        row = rows[level]
        histogram = row["grid_steps"]["direction_histogram"]
        used = {name for name, count in histogram.items() if count}
        assert used <= {"E", "NE"}, (level, histogram)
        assert histogram["E"] + histogram["NE"] == row["grid_steps"]["grid_step_count"]
        assert row["grid_steps"]["vertical_step_count"] == 0
        assert row["grid_steps"]["invalid_or_non_adjacent_step_count"] == 0
        assert row["status"] == "passed"
        assert row["path_fingerprint"]

    # Finer grid -> more steps, and the bearing is tracked with lower zigzag.
    assert rows[6]["grid_steps"]["grid_step_count"] < rows[7]["grid_steps"]["grid_step_count"]
    assert rows[7]["grid_steps"]["grid_step_count"] < rows[8]["grid_steps"]["grid_step_count"]
    assert rows[8]["zigzag_index"] < rows[7]["zigzag_index"] < rows[6]["zigzag_index"]


def test_grid_bias_histogram_matches_the_published_grid_path(diagnostics_tool):
    """The histogram must be derived from the real grid path, not asserted metadata."""

    baseline = diagnostics_tool.BASELINE
    case = baseline.benchmark_fixtures.case("zigzag_open_grid_bias")
    case["workspace_level"] = 7
    context = baseline._planner_context(case, max_cells=20000)
    run = {
        "planner_id": baseline.V2, "run_id": "diagnostic_v2",
        "parameters": {"risk_weight_lambda": 0.0, "risk_component": "overall"},
    }
    result = baseline._run_planner_once(run, case, context)
    indices = baseline.grid_indices(context["grid"])
    names = {
        (1, 0): "E", (-1, 0): "W", (0, 1): "N", (0, -1): "S",
        (1, 1): "NE", (-1, 1): "NW", (1, -1): "SE", (-1, -1): "SW",
    }
    expected = {name: 0 for name in names.values()}
    for left, right in zip(result["grid_path"], result["grid_path"][1:]):
        a, b = indices[left], indices[right]
        expected[names[(b[1] - a[1], b[2] - a[2])]] += 1
    histogram = grid_path_metrics(result["grid_path"], context["grid"])["direction_histogram"]
    assert histogram == expected
    assert histogram["NE"] and histogram["E"]


def test_lambda_sweep_exposes_tradeoff_without_choosing_a_value(diagnostics_tool):
    """λ must visibly move length and risk exposure, and the tool must not pick one."""

    rows = diagnostics_tool.lambda_sensitivity()["rows"]
    cheapest = rows[0]
    safest = min(
        (row for row in rows if row["risk_exposure_index_m"] is not None),
        key=lambda row: row["risk_exposure_index_m"],
    )
    assert cheapest["risk_weight_lambda"] == 0
    # Higher λ buys lower risk exposure with more length; both are stated, neither chosen.
    assert safest["risk_exposure_index_m"] < cheapest["risk_exposure_index_m"]
    assert safest["length_m"] >= cheapest["length_m"]
    assert cheapest["path_fingerprint"] != safest["path_fingerprint"]
    assert diagnostics_tool.build_diagnostics()["verdicts"]["recommended_parameter"] is None


def test_expert_brief_has_fixed_sections_questions_and_outputs(tmp_path):
    tool = load_tool("route_planning_expert_brief.py", "route_planning_expert_brief_test")
    brief = tool.build_brief()
    assert [item["id"] for item in brief["questions_for_experts"]] == [f"P{i}" for i in range(1, 9)]
    assert set(brief["open_items"]) == {
        "DATA-1", "DATA-2", "DATA-3", "EXPERT-1", "EXPERT-2", "EXPERT-3", "EXPERT-4", "EXPERT-5",
    }
    assert len(brief["synthetic_benchmark"]) == 10
    md_path, json_path = tool.write_brief(brief, tmp_path / "route_expert_brief")
    assert md_path.is_file() and json_path.is_file()
    markdown = md_path.read_text(encoding="utf-8")
    assert "待专家回答问题" in markdown
    assert "不代专家回答" in markdown
    assert "DATA-1" in markdown and "EXPERT-5" in markdown


def test_brief_states_observed_findings_without_conclusions():
    tool = load_tool("route_planning_expert_brief.py", "route_planning_expert_brief_findings")
    brief = tool.build_brief()
    findings = {item["id"]: item for item in brief["observed_findings"]}
    assert {"OBS-LAMBDA", "OBS-GRID", "OBS-DIRECTION-BIAS"} <= set(findings)

    # Grid finding: finer grid shortens the path and reduces zigzag, stated as fact.
    grid = findings["OBS-GRID"]
    assert grid["coarse_to_fine_length_delta_m"] < 0
    assert 0 < grid["coarse_to_fine_zigzag_ratio"] < 1
    assert [row["actual_grid_level"] for row in grid["rows"]] == [6, 7, 8]

    # Direction bias: only a subset of the eight directions is ever used.
    bias = findings["OBS-DIRECTION-BIAS"]
    assert bias["used_directions"] and len(bias["used_directions"]) < 8
    assert set(bias["used_directions"]) | set(bias["unused_directions"]) == {
        "E", "W", "N", "S", "NE", "NW", "SE", "SW",
    }
    assert "P6" in bias["note"]

    # Lambda finding is a stated tradeoff with no chosen value.
    lam = findings["OBS-LAMBDA"]
    assert lam["lowest_risk_exposure"] < lam["cheapest_risk_exposure"]
    assert lam["cheapest_lambda"] == 0
    assert lam["distinct_paths"] >= 2
    assert "不推荐" in lam["note"]

    # No finding may carry a verdict, score or recommendation field.
    for item in brief["observed_findings"]:
        for forbidden in ("recommendation", "recommended", "score", "ranking", "verdict", "best"):
            assert forbidden not in item


# --------------------------------------------------------------------------------------
# real-data readiness verdict (DATA-1/DATA-2/DATA-3)
# --------------------------------------------------------------------------------------


def _project(tmp_path):
    """A project whose reference data is not ready, matching the real project shape."""

    from cns_planner.application.workflow_service import WorkflowService

    defaults = tmp_path / "defaults.json"
    defaults.write_text(
        Path("cns_planner/config/defaults.json").read_text(encoding="utf-8"), encoding="utf-8",
    )
    workflow = WorkflowService(tmp_path / "project.json", defaults)
    landing = tmp_path / "landing.csv"
    landing.write_text(
        "序号,起降设施分类,所属县区,具体位置,经纬度信息\n"
        "1,起降点,定海区,明确点,122.10,30.10\n",
        encoding="utf-8-sig",
    )
    workflow.import_reference_landing_sites(landing)
    return workflow.store_path


def test_real_data_verdict_is_not_ready_when_no_project_is_supplied():
    tool = load_tool("route_planning_expert_brief.py", "route_planning_expert_brief_readiness_a")
    verdict = tool.real_data_verdict({"status": "not_supplied", "not_ready_reason": "no_project_path_argument"})
    assert verdict["status"] == "NOT_READY"
    assert verdict["reason"] == "no_project_path_argument"
    assert set(verdict["items"]) == {"DATA-1", "DATA-2", "DATA-3"}
    for item in ("DATA-1", "DATA-2"):
        assert verdict["items"][item]["status"] == "NOT_READY"
        assert verdict["items"][item]["action"]
    # DATA-3 (current airspace) is retired by architecture decision: display-only layer.
    assert verdict["items"]["DATA-3"]["status"] == "retired"
    assert verdict["items"]["DATA-3"]["reason"] == "not_applicable_by_architecture_decision"
    assert verdict["pending_items"] == ["DATA-1", "DATA-2"]
    assert verdict["metric_measurement_enabled"] is False


def test_real_data_verdict_is_not_ready_when_project_file_is_missing():
    tool = load_tool("route_planning_expert_brief.py", "route_planning_expert_brief_readiness_b")
    verdict = tool.real_data_verdict({"status": "not_found", "not_ready_reason": "project_file_not_found"})
    assert verdict["status"] == "NOT_READY"
    assert verdict["reason"] == "project_file_not_found"


def test_real_data_verdict_keeps_pending_data_items_explicit(tmp_path):
    tool = load_tool("route_planning_expert_brief.py", "route_planning_expert_brief_readiness_c")
    project = _project(tmp_path)
    brief = tool.build_brief(str(project))
    readiness = brief["current_data_and_constraints"]["real_data_readiness"]

    # The project read fine, so project evidence is "passed"...
    assert brief["current_data_and_constraints"]["project_evidence_status"] == "passed"
    # ...but real data is still NOT READY, and each item says why.
    assert readiness["status"] == "NOT_READY"
    assert readiness["pending_items"] == ["DATA-1", "DATA-2"]
    assert readiness["items"]["DATA-1"]["status"] == "NOT_READY"
    assert readiness["items"]["DATA-1"]["reason"] == "source_crs_pending_confirmation"
    assert readiness["items"]["DATA-2"]["status"] == "NOT_READY"
    assert readiness["items"]["DATA-3"]["status"] == "retired"
    assert readiness["items"]["DATA-3"]["reason"] == "not_applicable_by_architecture_decision"
    assert readiness["metric_measurement_enabled"] is False
    assert readiness["airspace_policies"]["never_inferred_from_layer_name_or_color"] is True
    assert readiness["blocks"]["reference_landing_sites"]["count"] == 1
    # No CRS is guessed and no ET parsing happens.
    assert readiness["items"]["DATA-2"]["et_parser"] is None
    assert readiness["blocks"]["reference_landing_sites"]["source_crs_resolved"] is False


def test_real_data_verdict_becomes_ready_only_with_confirmed_crs_and_policy(tmp_path):
    tool = load_tool("route_planning_expert_brief.py", "route_planning_expert_brief_readiness_d")
    verdict = tool.real_data_verdict({
        "status": "passed",
        "data_readiness": {
            "status": "ready",
            "et_parser": None,
            "blocks": {
                "reference_landing_sites": {"status": "passed", "count": 1, "format": "geojson",
                                            "source_crs_resolved": True, "unresolved_reason": None,
                                            "metric_measurement_status": "enabled"},
                "reference_routes": {"status": "passed", "count": 2, "format": "csv",
                                     "source_crs_resolved": True, "unresolved_reason": None,
                                     "metric_measurement_status": "enabled"},
                "airspace_policies": {
                    "status": "passed", "count": 2, "confirmed_count": 2,
                    "route_eligibility_counts": {"allowed": 1, "blocked": 1, "unknown": 0},
                    "v2_readiness": {"status": "ready", "reason": None, "allowed_grid_ids": 900},
                    "never_inferred_from_layer_name_or_color": True,
                },
            },
        },
    })
    assert verdict["status"] == "READY"
    assert verdict["pending_items"] == []
    assert verdict["metric_measurement_enabled"] is True
    for item in verdict["items"].values():
        assert item["status"] != "NOT_READY"


def test_brief_markdown_prints_per_item_data_verdicts(tmp_path):
    tool = load_tool("route_planning_expert_brief.py", "route_planning_expert_brief_readiness_e")
    brief = tool.build_brief(str(_project(tmp_path)))
    md_path, _ = tool.write_brief(brief, tmp_path / "brief")
    markdown = md_path.read_text(encoding="utf-8")
    assert "NOT_READY" in markdown
    assert "DATA-1" in markdown and "DATA-2" in markdown and "DATA-3" in markdown
    assert "禁止猜 CRS" in markdown or "禁止猜测" in markdown
    assert "数据就绪面板只读取来源事实与 policy" not in markdown  # that note belongs to the pack, not the brief
    assert "真实数据 readiness" in markdown


def test_diagnostics_and_brief_never_offer_a_recommendation(tmp_path):
    diagnostics = load_tool("route_planning_diagnostics.py", "route_planning_diagnostics_reco")
    brief_tool = load_tool("route_planning_expert_brief.py", "route_planning_expert_brief_reco")
    sensitivity = diagnostics.build_diagnostics()
    assert sensitivity["verdicts"]["recommended_parameter"] is None
    assert sensitivity["lambda_sensitivity"]["automatic_recommendation"] is None
    assert sensitivity["grid_sensitivity"]["automatic_recommendation"] is None
    brief = brief_tool.build_brief()
    for question in brief["questions_for_experts"]:
        # Questions only: no answer field may sneak in.
        assert set(question) == {"id", "question"}
    assert brief["scope_guards"]
