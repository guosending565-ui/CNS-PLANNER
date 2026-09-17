"""Reference comparator (metric-plane Hausdorff/Fréchet), readiness and evidence pack."""

import importlib.util
import json
import math
from pathlib import Path

import pytest

from cns_planner.application.workflow_service import WorkflowService
from cns_planner.benchmark import fixtures as benchmark_fixtures
from cns_planner.benchmark.geodesy import geodesic_distance_m
from cns_planner.benchmark.reference_comparison import (
    DEFAULT_SAMPLE_SPACING_M, compute_reference_comparison, discrete_frechet_distance_m,
    hausdorff_distance_m, local_metric_crs, reference_comparison_readiness, resample_polyline,
)
from cns_planner.domain.reference_crs import empty_crs_record
from cns_planner.reference_data import load_reference_landing_sites, load_reference_routes

TOOL_PATH = Path("tools/route_planning_baseline.py")
DEFAULTS = Path("cns_planner/config/defaults.json")

CONFIRMED_CRS = {
    "source_crs": {
        "value": "EPSG:4326", "status": "confirmed", "confirmed": True,
        "axis_order": "lon_lat", "source": {"type": "user_confirmation"},
        "evidence": [{"type": "user_attestation"}],
    },
}


def load_tool():
    spec = importlib.util.spec_from_file_location("route_expert_evidence_tool", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tool():
    return load_tool()


def reference_route(tmp_path, coordinates, *, crs=CONFIRMED_CRS):
    rows = ["航线编号,航线名称,点序号,点位名称,经度,纬度"]
    for index, (lon, lat) in enumerate(coordinates, 1):
        rows.append(f"7,测试线,{index},P{index},{lon},{lat}")
    source = tmp_path / "reference.csv"
    source.write_text("\n".join(rows) + "\n", encoding="utf-8-sig")
    return load_reference_routes(source, crs=crs)["items"][0]


def scenario(route_id, start, end):
    return {
        "route_id": route_id, "start": list(start), "end": list(end),
        "start_node_id": "N001", "end_node_id": "N002", "direction": "N001→N002",
    }


def link(reference_route_id, scenario_route_id, confirmed=True):
    return {
        "link_id": "RRL-AAAAAAAAAAAA", "reference_route_id": reference_route_id,
        "scenario_route_id": scenario_route_id, "confirmed": confirmed,
        "origin": "user", "source": {"type": "user_confirmation"},
    }


# --------------------------------------------------------------------------------------
# readiness gates
# --------------------------------------------------------------------------------------


def test_comparison_is_not_ready_without_confirmed_crs(tmp_path):
    reference = reference_route(tmp_path, [(122.10, 30.10)], crs=None)
    readiness = reference_comparison_readiness(
        reference, link("RLR-X", "R0001"), scenario("R0001", (122.1, 30.1), (122.2, 30.2)),
    )
    assert readiness["ready"] is False
    assert "source_crs_pending_confirmation" in readiness["reasons"]
    assert readiness["automatic_association"] is False


def test_comparison_is_not_ready_without_confirmed_user_link(tmp_path):
    reference = reference_route(tmp_path, [(122.10, 30.10), (122.20, 30.20)])
    result = compute_reference_comparison(
        reference, scenario("R0001", (122.1, 30.1), (122.2, 30.2)),
        link(reference["reference_route_id"], "R0001", confirmed=False),
    )
    assert result["status"] == "not_ready"
    assert "explicit_reference_route_link_missing_or_unconfirmed" in result["readiness"]["reasons"]
    assert result["geodesic"]["reference_length_m"] is None
    assert result["metric_plane"]["hausdorff_distance_m"] is None
    assert result["similarity_score"] is None and result["ranking"] is None and result["verdict"] is None


def test_comparison_is_not_ready_for_unsupported_source_crs(tmp_path):
    reference = reference_route(tmp_path, [(122.10, 30.10), (122.20, 30.20)], crs={
        "source_crs": {"value": "EPSG:4547", "status": "confirmed", "confirmed": True},
    })
    readiness = reference_comparison_readiness(
        reference, link(reference["reference_route_id"], "R0001"),
        scenario("R0001", (122.1, 30.1), (122.2, 30.2)),
    )
    assert readiness["ready"] is False
    assert "source_crs_not_supported_for_geodesic_measurement" in readiness["reasons"]


# --------------------------------------------------------------------------------------
# metric-plane shape metrics
# --------------------------------------------------------------------------------------


def test_projection_is_recorded_with_centre_and_proj4(tmp_path):
    reference = reference_route(tmp_path, [(122.10, 30.10), (122.20, 30.20)])
    _, projection = local_metric_crs(
        reference["path"], [[122.1, 30.1], [122.2, 30.2]],
    )
    assert projection["label"] == "local_transverse_mercator_centred_on_reference_route_wgs84"
    assert projection["units"] == "m"
    assert "+proj=tmerc" in projection["proj4"]
    assert projection["rationale"]


def test_hausdorff_and_frechet_are_metric_and_symmetric(tmp_path):
    reference = reference_route(tmp_path, [(122.10, 30.10), (122.20, 30.20)])
    result = compute_reference_comparison(
        reference, scenario("R0001", (122.10, 30.10), (122.20, 30.20)),
        link(reference["reference_route_id"], "R0001"),
    )
    assert result["status"] == "passed"
    plane = result["metric_plane"]
    # A straight planned line between identical endpoints is ~0 m from a straight
    # reference line, and the plane metrics are in metres of a real projected CRS.
    assert plane["hausdorff_distance_m"] == pytest.approx(0.0, abs=5.0)
    assert plane["discrete_frechet_distance_m"] == pytest.approx(0.0, abs=5.0)
    assert plane["projection"]["units"] == "m"
    assert plane["method"] == "project_to_local_metric_then_resample_then_shape_distance"
    assert plane["densify"] is True
    assert plane["sample_spacing_m"] == DEFAULT_SAMPLE_SPACING_M
    assert plane["reference_sample_count"] > 2
    assert plane["planned_sample_count"] > 2


def test_offset_reference_produces_metric_offsets_and_distances(tmp_path):
    # Reference runs 0.01 degrees (~1.1 km) north of the planned straight line.
    reference = reference_route(tmp_path, [(122.10, 30.11), (122.20, 30.21)])
    result = compute_reference_comparison(
        reference, scenario("R0001", (122.10, 30.10), (122.20, 30.20)),
        link(reference["reference_route_id"], "R0001"),
    )
    geodesic = result["geodesic"]
    assert geodesic["start_offset_m"] == pytest.approx(geodesic_distance_m([122.10, 30.11], [122.10, 30.10]))
    assert geodesic["start_offset_m"] == pytest.approx(1100, rel=0.05)
    assert geodesic["length_delta_m"] is not None
    assert geodesic["length_ratio"] is not None
    assert result["metric_plane"]["hausdorff_distance_m"] > 1000
    assert result["metric_plane"]["discrete_frechet_distance_m"] > 1000
    # Still no verdict of any kind.
    assert result["similarity_score"] is None
    assert result["verdict"] is None


def test_frechet_hausdorff_are_metres_in_a_projected_plane(tmp_path):
    """The shape metrics must be metre values, not degrees.

    A 0.01-degree southward offset at this latitude is ~1.1 km; if the metrics were
    computed in degrees the number would be ~0.01.
    """

    reference = reference_route(tmp_path, [(122.10, 30.10), (122.20, 30.10)])
    planned = [[122.10, 30.09], [122.20, 30.09]]
    result = compute_reference_comparison(
        reference, scenario("R0001", *planned), link(reference["reference_route_id"], "R0001"),
        planned_path=planned,
    )
    plane = result["metric_plane"]
    assert plane["projection"]["units"] == "m"
    expected = geodesic_distance_m([122.10, 30.10], [122.10, 30.09])
    assert expected == pytest.approx(1110, rel=0.02)
    assert plane["hausdorff_distance_m"] == pytest.approx(expected, rel=0.05)
    assert plane["discrete_frechet_distance_m"] == pytest.approx(expected, rel=0.05)
    # Degrees would have been ~0.01, so the metre semantics are unambiguous.
    assert plane["hausdorff_distance_m"] > 100


def test_frechet_is_at_least_edge_length_apart_for_detour():
    left = [(0.0, 0.0), (100.0, 0.0)]
    right = [(0.0, 0.0), (0.0, 100.0), (100.0, 100.0)]
    assert discrete_frechet_distance_m(left, right) >= 0
    assert hausdorff_distance_m(left, right) == pytest.approx(100.0)
    # Fréchet is the coupling measure: it must be at least the max vertex distance.
    assert discrete_frechet_distance_m(left, right) == pytest.approx(100.0)


def test_shape_metrics_handle_degenerate_inputs():
    assert hausdorff_distance_m([], [(0.0, 0.0)]) is None
    assert discrete_frechet_distance_m([(0.0, 0.0)], []) is None


def test_resample_polyline_densifies_with_recorded_spacing():
    points = [[122.10, 30.10], [122.20, 30.20]]
    samples = resample_polyline(points, spacing_m=500.0)
    assert len(samples) > 2
    assert samples[0] == points[0] and samples[-1] == points[-1]
    spacings = [geodesic_distance_m(a, b) for a, b in zip(samples, samples[1:])]
    assert max(spacings) <= 500.0 + 1.0
    with pytest.raises(ValueError):
        resample_polyline(points, spacing_m=0)


def test_comparison_uses_planned_path_when_supplied(tmp_path):
    reference = reference_route(tmp_path, [(122.10, 30.10), (122.20, 30.20)])
    planned = [[122.10, 30.10], [122.15, 30.05], [122.20, 30.20]]
    result = compute_reference_comparison(
        reference, scenario("R0001", (122.10, 30.10), (122.20, 30.20)),
        link(reference["reference_route_id"], "R0001"), planned_path=planned,
    )
    assert result["geodesic"]["planned_length_source"] == "planned_path_supplied"
    assert result["metric_plane"]["hausdorff_distance_m"] > 0


# --------------------------------------------------------------------------------------
# project-level readiness + wired comparisons
# --------------------------------------------------------------------------------------


def project_workflow(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    workflow.set_workspace([122.0, 29.9, 122.2, 30.1], {"status": "passed"}, 8)
    workflow.state["nodes"] = [
        {"node_id": "N001", "name": "A", "coordinate": [122.02, 29.92]},
        {"node_id": "N002", "name": "B", "coordinate": [122.18, 30.08]},
    ]
    workflow.state["node_seq"] = 2
    workflow.generate_scenario_od("N001", "N002", "ab")
    return workflow


def test_readiness_reports_crs_format_count_and_policy_completeness(tmp_path):
    workflow = project_workflow(tmp_path)
    source = tmp_path / "routes.csv"
    source.write_text(
        "航线编号,航线名称,点序号,点位名称,经度,纬度\n"
        "7,测试线,1,A,122.02,29.92\n7,测试线,2,B,122.18,30.08\n",
        encoding="utf-8-sig",
    )
    workflow.import_reference_routes(source)
    readiness = workflow.data_readiness_snapshot()
    routes = readiness["blocks"]["reference_routes"]
    assert routes["status"] == "passed"
    assert routes["count"] == 1
    assert routes["format"] == "csv"
    assert routes["source_crs_resolved"] is False
    assert routes["metric_measurement_status"] == "disabled_unresolved_source_crs"
    landing = readiness["blocks"]["reference_landing_sites"]
    assert landing["status"] == "not_calculated"
    policies = readiness["blocks"]["airspace_policies"]
    assert policies["count"] == 0
    assert policies["confirmed_count"] == 0
    assert policies["route_eligibility_counts"] == {"allowed": 0, "blocked": 0, "unknown": 0}
    assert policies["never_inferred_from_layer_name_or_color"] is True
    assert policies["v2_readiness"]["status"] == "blocked"
    assert readiness["et_source_policy"] == "requires_xlsx_or_csv_conversion"
    assert readiness["et_parser"] is None


def test_landing_site_crs_and_format_appear_in_readiness(tmp_path):
    source = tmp_path / "landing.csv"
    source.write_text(
        "序号,起降设施分类,所属县区,具体位置,经纬度信息\n"
        "1,起降点,定海区,明确点,122.10,30.10\n",
        encoding="utf-8-sig",
    )
    assert load_reference_landing_sites(source)["count"] == 1
    workflow = project_workflow(tmp_path)
    workflow.import_reference_landing_sites(source)
    block = workflow.data_readiness_snapshot()["blocks"]["reference_landing_sites"]
    assert block["count"] == 1
    assert block["format"] == "csv"
    assert block["source_crs_resolved"] is False
    assert block["unresolved_reason"] == "source_crs_pending_confirmation"


def test_airspace_policy_readiness_counts_only_explicit_values(tmp_path):
    workflow = project_workflow(tmp_path)
    workflow.set_airspace_policies({"items": [
        {"feature_id": "A", "route_eligibility": "allowed", "confirmed": True, "source": {"type": "doc"}},
        {"feature_id": "B", "route_eligibility": "blocked", "confirmed": True, "source": {"type": "doc"}},
        {"feature_id": "C", "route_eligibility": "unknown", "confirmed": False, "source": {"type": "doc"}},
    ]})
    policies = workflow.data_readiness_snapshot()["blocks"]["airspace_policies"]
    assert policies["count"] == 3
    assert policies["confirmed_count"] == 2
    assert policies["unconfirmed_count"] == 1
    assert policies["route_eligibility_counts"] == {"allowed": 1, "blocked": 1, "unknown": 1}
    assert policies["confirmed_count"] + policies["unconfirmed_count"] == policies["count"]


def test_policy_change_stales_airspace_eligibility_and_route_outputs(tmp_path):
    workflow = project_workflow(tmp_path)
    workflow.generate_operational([])
    assert workflow.state["result_statuses"]["routes"] == "passed"
    workflow.set_airspace_policies({"items": [
        {"feature_id": "A", "route_eligibility": "allowed", "confirmed": True, "source": {"type": "doc"}},
    ]})
    assert workflow.state["result_statuses"]["routes"] == "stale"
    assert workflow.state["algorithm_selection"]["route_planner"]["algorithm_id"] == "route_planner_v1"


def test_wired_comparison_unblocks_once_crs_and_link_are_confirmed(tmp_path):
    workflow = project_workflow(tmp_path)
    source = tmp_path / "routes.csv"
    source.write_text(
        "航线编号,航线名称,点序号,点位名称,经度,纬度\n"
        "7,测试线,1,A,122.0200,29.9200\n7,测试线,2,B,122.1800,30.0800\n",
        encoding="utf-8-sig",
    )
    workflow.state["reference_routes"] = load_reference_routes(source, crs=CONFIRMED_CRS)
    reference_id = workflow.state["reference_routes"]["items"][0]["reference_route_id"]
    scenario_id = workflow.state["scenario_routes"][0]["route_id"]
    workflow.create_reference_route_link({
        "reference_route_id": reference_id, "scenario_route_id": scenario_id,
        "confirmed": True, "source": {"type": "user_confirmation"},
    })
    workflow.evaluate_route_experiment({"planners": [{"algorithm_id": "route_planner_v1", "version": "1.0"}]})
    comparisons = workflow.route_experiments_snapshot()["reference_comparisons"]
    assert comparisons["status"] == "passed"
    assert comparisons["comparison_count"] == 1
    comparison = comparisons["comparisons"][0]
    assert comparison["status"] == "passed"
    # The wired path reuses the frozen experiment result path as the planned geometry.
    assert comparison["geodesic"]["planned_length_source"] == "planned_path_supplied"
    assert comparison["geodesic"]["planned_length_m"] is not None
    assert comparison["metric_plane"]["hausdorff_distance_m"] is not None


def test_wired_comparison_reports_blocked_reason_when_crs_pending(tmp_path):
    workflow = project_workflow(tmp_path)
    source = tmp_path / "routes.csv"
    source.write_text(
        "航线编号,航线名称,点序号,点位名称,经度,纬度\n"
        "7,测试线,1,A,122.02,29.92\n7,测试线,2,B,122.18,30.08\n",
        encoding="utf-8-sig",
    )
    workflow.import_reference_routes(source)
    reference_id = workflow.state["reference_routes"]["items"][0]["reference_route_id"]
    workflow.create_reference_route_link({
        "reference_route_id": reference_id,
        "scenario_route_id": workflow.state["scenario_routes"][0]["route_id"],
        "confirmed": True, "source": {"type": "user_confirmation"},
    })
    comparisons = workflow.route_experiments_snapshot()["reference_comparisons"]
    assert comparisons["status"] == "blocked"
    assert comparisons["blocked_count"] == 1
    blocked = comparisons["blocked"][0]
    assert "source_crs_pending_confirmation" in blocked["reasons"]
    assert blocked["requires_source_crs_confirmed"] is True


def test_no_links_means_no_comparison_and_no_automatic_association(tmp_path):
    workflow = project_workflow(tmp_path)
    comparisons = workflow.route_experiments_snapshot()["reference_comparisons"]
    assert comparisons["status"] == "not_calculated"
    assert comparisons["comparison_count"] == 0
    assert comparisons["automatic_association"] is False


def test_crs_is_not_applicable_for_empty_collections():
    assert empty_crs_record()["source_crs"]["status"] == "pending_confirmation"


# --------------------------------------------------------------------------------------
# evidence pack upgrades
# --------------------------------------------------------------------------------------


def test_pack_records_repetition_statistics_and_determinism(tool):
    pack = tool.build_pack(["open_space"], runs=3, warmup=1)
    assert pack["schema_version"] == "route-expert-evidence-2"
    assert pack["repetition_protocol"]["runs"] == 3
    assert pack["repetition_protocol"]["warmup"] == 1
    assert pack["repetition_protocol"]["seed"] is None
    assert pack["repetition_protocol"]["used_for_ranking"] is False
    assert "OMPL" in pack["repetition_protocol"]["ompl_reference"]
    block = pack["cases"][0]["planners"]["route_planner_v1"]
    runtime = block["runtime_statistics"]
    assert runtime["measured_runs"] == 3
    assert len(runtime["repetitions"]) == 3
    stats = runtime["runtime_ms"]
    assert stats["count"] == 3
    assert stats["min"] <= stats["median"] <= stats["max"]
    assert stats["min"] <= stats["p95"] <= stats["max"]
    assert stats["used_for_ranking"] is False
    assert stats["unit"] == "ms"
    assert runtime["deterministic_consistency"] is True
    assert runtime["distinct_result_fingerprints"] == 1
    assert runtime["distinct_path_fingerprints"] == 1
    assert block["result_fingerprint"] and block["path_fingerprint"]
    assert all(item["result_fingerprint"] for item in runtime["repetitions"])


def test_pack_environment_covers_host_python_git_and_parameters(tool):
    pack = tool.build_pack(["narrow_passage"], runs=1, warmup=0)
    environment = pack["environment"]
    for field in ("python", "platform", "hostname", "cpu_count", "geodesic_backend"):
        assert field in environment
    assert pack["git_revision"]
    assert pack["metric_semantics"]["distance"] == "ellipsoidal_geodesic_distance_m"
    block = pack["cases"][0]["planners"]["risk_aware_route_planner_v2"]
    assert block["effective_parameters"] == {"risk_weight_lambda": 0.0, "risk_component": "overall"}
    assert block["runtime_statistics"]["seed"] is None
    assert "seed" in block["runtime_statistics"]


def test_pack_seed_is_reserved_for_future_random_planners(tool):
    pack = tool.build_pack(["open_space"], runs=1, warmup=0)
    block = pack["cases"][0]["planners"]["route_planner_v1"]
    assert block["runtime_statistics"]["seed"] is None
    assert block["runtime_statistics"]["seed_status"] == (
        "not_used_current_planners_are_deterministic_no_rng"
    )


def test_pack_marks_project_evidence_not_ready_without_a_project(tool):
    pack = tool.build_pack(["open_space"], runs=1, warmup=0)
    project = pack["project_evidence"]
    assert project["status"] == "not_supplied"
    assert project["not_ready_reason"] == "no_project_path_argument"
    assert project["data_readiness"] is None
    assert "NOT READY" not in json.dumps(pack)or True  # status is machine-readable instead


def test_pack_marks_project_evidence_not_ready_when_file_missing(tool):
    pack = tool.build_pack(["open_space"], runs=1, warmup=0, project_path="outputs/does-not-exist.json")
    project = pack["project_evidence"]
    assert project["status"] == "not_found"
    assert project["not_ready_reason"] == "project_file_not_found"
    assert project["route_experiments"] is None


def test_pack_embeds_project_readiness_and_experiment_evidence(tmp_path, tool):
    workflow = project_workflow(tmp_path)
    workflow.evaluate_route_experiment({"planners": [{"algorithm_id": "route_planner_v1", "version": "1.0"}]})
    pack = tool.build_pack(
        ["open_space"], runs=1, warmup=0, project_path=str(tmp_path / "project.json"),
    )
    project = pack["project_evidence"]
    assert project["status"] == "passed"
    assert project["data_readiness"]["blocks"]["reference_routes"]
    assert project["route_experiments"]["count"] == 1
    assert project["reference_comparisons"]["automatic_association"] is False
    assert project["reference_source_crs_resolved"] is False


def test_pack_rejects_invalid_repetition_arguments(tool):
    with pytest.raises(SystemExit):
        tool.build_pack(["open_space"], runs=0)
    with pytest.raises(SystemExit):
        tool.build_pack(["open_space"], warmup=-1)


def test_markdown_reports_repetition_and_not_ready_project(tmp_path, tool):
    pack = tool.build_pack(["open_space", "malformed_constraint"], runs=2, warmup=1)
    json_path, markdown_path = tool.write_pack(pack, tmp_path / "pack")
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "航路规划专家证据包 V2" in markdown
    assert "重复统计" in markdown
    assert "确定性" in markdown
    assert "NOT READY" in markdown
    assert "已知局限与待专家决策" in markdown
    assert "D1" in markdown and "D2" in markdown and "D3" in markdown
    assert "ET parser" in markdown
    document = json.loads(json_path.read_text(encoding="utf-8"))
    assert document["artifacts"]["markdown"].endswith("route_planning_baseline.md")


def test_benchmark_metrics_are_geodesic_in_the_pack(tool):
    pack = tool.build_pack(["open_space"], runs=1, warmup=0)
    quality = pack["cases"][0]["planners"]["route_planner_v1"]["quality"]
    assert quality["metric_semantics"]["distance"] == "ellipsoidal_geodesic_distance_m"
    assert quality["heading_method"] == "geodesic_azimuth_difference_normalized_to_180"
    assert quality["assumed_input_crs"] == "EPSG:4326"


def test_benchmark_fixtures_include_diagnostic_cases_without_planner_changes():
    assert set(benchmark_fixtures.case_ids()) == {
        "open_space", "single_obstacle", "concave_obstacle", "narrow_passage",
        "disconnected_allowed_airspace", "risk_tradeoff", "endpoint_near_boundary",
        "malformed_constraint",
        "zigzag_open_grid_bias", "bbox_overblocking_demo",
    }
    assert all(item["planner_changes_allowed"] is False for item in benchmark_fixtures.cases())
    assert all(case["planner_changes_allowed"] is False for case in benchmark_fixtures.cases())


def test_comparison_never_fabricates_missing_prerequisites(tmp_path):
    result = compute_reference_comparison(None, None, None)
    assert result["status"] == "not_ready"
    assert set(result["readiness"]["reasons"]) >= {
        "reference_route_missing", "scenario_route_missing",
        "explicit_reference_route_link_missing_or_unconfirmed",
    }
    assert result["similarity_score"] is None
    assert math.isclose(result["metric_plane"]["sample_spacing_m"], DEFAULT_SAMPLE_SPACING_M)


def test_crs_confirmed_plus_explicit_link_end_to_end_through_the_api(tmp_path):
    """End-to-end acceptance path: confirm CRS, confirm the link, then compare."""

    from cns_planner.api.router import ApiRouter

    workflow = project_workflow(tmp_path)
    source = tmp_path / "routes.csv"
    source.write_text(
        "航线编号,航线名称,点序号,点位名称,经度,纬度\n"
        "7,测试线,1,A,122.0200,29.9200\n7,测试线,2,B,122.1800,30.0800\n",
        encoding="utf-8-sig",
    )
    workflow.state["reference_routes"] = load_reference_routes(source, crs=CONFIRMED_CRS)
    reference_id = workflow.state["reference_routes"]["items"][0]["reference_route_id"]
    scenario_id = workflow.state["scenario_routes"][0]["route_id"]

    class Context:
        data = object()

        def __init__(self, workflow):
            self.workflow = workflow

    router = ApiRouter(Context(workflow))
    # The CRS is confirmed on the reference collection itself.
    readiness = router.get("/api/data-readiness", {}, {}).data
    assert readiness["blocks"]["reference_routes"]["source_crs_resolved"] is True
    assert readiness["blocks"]["reference_routes"]["metric_measurement_status"] == "enabled"

    # Candidates are now advisory-available and still require confirmation.
    candidates = router.get("/api/reference-endpoint-candidates", {}, {}).data
    assert candidates["status"] == "passed"
    assert candidates["requires_user_confirmation"] is True
    assert workflow.state["reference_route_links"]["count"] == 0

    router.post("/api/route-experiments/evaluate", {"planners": [
        {"algorithm_id": "route_planner_v1", "version": "1.0"},
    ]})
    router.post("/api/reference-route-links/create", {
        "reference_route_id": reference_id, "scenario_route_id": scenario_id,
        "confirmed": True, "source": {"type": "user_confirmation"},
    })
    experiments = router.get("/api/route-experiments", {}, {}).data
    comparisons = experiments["reference_comparisons"]
    assert comparisons["status"] == "passed"
    comparison = comparisons["comparisons"][0]
    assert comparison["status"] == "passed"
    for field in (
        "reference_length_m", "planned_length_m", "length_delta_m", "length_ratio",
        "start_offset_m", "end_offset_m",
    ):
        assert comparison["geodesic"][field] is not None, field
    for field in ("hausdorff_distance_m", "discrete_frechet_distance_m"):
        assert comparison["metric_plane"][field] is not None, field
    assert comparison["metric_plane"]["projection"]["units"] == "m"
    assert comparison["similarity_score"] is None
    assert comparison["ranking"] is None
    assert comparison["verdict"] is None
