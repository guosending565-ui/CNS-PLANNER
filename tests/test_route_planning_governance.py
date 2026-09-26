"""Governance tests: fail-closed constraints, exact OD routing and planner metadata."""

from copy import deepcopy
import importlib
from pathlib import Path

import pytest

from cns_planner.algorithms.grid.service import WorkspaceGridService
from cns_planner.algorithms.registry import build_default_algorithm_registry, default_algorithm_selection
from cns_planner.algorithms.route_planner import RoutePlannerV1
from cns_planner.api.router import ApiRouter
from cns_planner.application.constraint_validation import validate_hard_constraints
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.compatibility.selection import CompatibilitySelectionAdapter
from cns_planner.data.mapping.airspace_eligibility import AirspaceEligibilityService
from cns_planner.route_planner.risk_aware_v2 import RiskAwareRoutePlannerV2


DEFAULTS = Path("cns_planner/config/defaults.json")
WORKSPACE = [0.0, 0.0, 0.1, 0.1]


class RecordingPlanner:
    """V1 delegate that records how often a planner was actually entered."""

    algorithm_id = "route_planner_v1"
    algorithm_version = "1.0"
    uses_canonical_grid_risk = False

    def __init__(self):
        self.delegate = RoutePlannerV1()
        self.calls = []
        self.plan_invocations = 0

    def plan(self, route, workspace, hard_constraints):
        self.plan_invocations += 1
        self.calls.append(deepcopy(hard_constraints))
        return self.delegate.plan(route, workspace, hard_constraints)


def workflow_with_routes(tmp_path, planner=None):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    workflow.state["workspace"] = {"status": "passed", "bbox": list(WORKSPACE)}
    workflow.state["nodes"] = [
        {"node_id": "N001", "name": "A", "coordinate": [0.01, 0.01]},
        {"node_id": "N002", "name": "B", "coordinate": [0.09, 0.09]},
        {"node_id": "N003", "name": "C", "coordinate": [0.09, 0.01]},
    ]
    workflow.state["node_seq"] = 3
    if planner is not None:
        workflow.route_service.planner = planner
    return workflow


# --------------------------------------------------------------------------------------
# 1. Fail-closed hard-constraint boundary
# --------------------------------------------------------------------------------------


def test_malformed_constraints_are_rejected_before_any_planner_runs(tmp_path):
    planner = RecordingPlanner()
    workflow = workflow_with_routes(tmp_path, planner)
    workflow.generate_scenario_od("N001", "N002", "ab")
    malformed = [
        {"name": "缺 bbox"},
        {"name": "三项", "bbox": [0.1, 0.1, 0.2]},
        {"name": "字符串", "bbox": "0.1,0.1,0.2,0.2"},
        {"name": "非有限", "bbox": [0.1, 0.1, float("inf"), 0.2]},
        {"name": "NaN", "bbox": [0.1, 0.1, float("nan"), 0.2]},
        {"name": "经度反转", "bbox": [0.2, 0.1, 0.1, 0.2]},
        {"name": "纬度反转", "bbox": [0.1, 0.2, 0.2, 0.1]},
        {"name": "退化", "bbox": [0.1, 0.1, 0.1, 0.1]},
        {"name": "布尔", "bbox": [True, 0.1, 0.2, 0.2]},
        ["不是对象"],
    ]
    for item in malformed:
        with pytest.raises(ValueError):
            workflow.generate_operational([item])
    with pytest.raises(ValueError):
        workflow.generate_operational({"bbox": [0.1, 0.1, 0.2, 0.2]})
    # A malformed constraint must never be silently downgraded to "no constraint".
    assert planner.plan_invocations == 0
    assert workflow.state["operational_routes"] == []


def test_malformed_constraint_after_a_valid_one_is_still_rejected(tmp_path):
    planner = RecordingPlanner()
    workflow = workflow_with_routes(tmp_path, planner)
    workflow.generate_scenario_od("N001", "N002", "ab")
    with pytest.raises(ValueError):
        workflow.generate_operational([
            {"name": "合法", "bbox": [0.045, 0.045, 0.055, 0.055]},
            {"name": "非法", "bbox": [0.045, 0.045, 0.055]},
        ])
    assert planner.plan_invocations == 0


def test_valid_constraints_are_normalized_and_reach_the_planner_unchanged_in_meaning(tmp_path):
    planner = RecordingPlanner()
    workflow = workflow_with_routes(tmp_path, planner)
    workflow.generate_scenario_od("N001", "N002", "ab")
    workflow.generate_operational([{"name": "合法", "bbox": [0.04, 0.04, 0.06, 0.06]}])
    assert planner.plan_invocations == 1
    passed = planner.calls[0]
    assert passed[0]["bbox"] == [0.04, 0.04, 0.06, 0.06]
    assert all(isinstance(value, float) for value in passed[0]["bbox"])
    assert passed[0]["name"] == "合法"


def test_no_constraints_is_a_genuine_empty_list_not_a_malformed_entry(tmp_path):
    assert validate_hard_constraints(None) == []
    assert validate_hard_constraints([]) == []
    workflow = workflow_with_routes(tmp_path)
    workflow.generate_scenario_od("N001", "N002", "ab")
    state = workflow.generate_operational([])
    # B2B-1R：兼容试算结果只在本次 response；canonical 运行航路不被旧 planner 写。
    assert state["operational_routes"] == []
    assert all(
        item["status"] == "passed"
        for item in state["compatibility_operational_routes"]["items"]
    )


def test_validator_error_is_actionable_and_indexed():
    with pytest.raises(ValueError) as failure:
        validate_hard_constraints([{"name": "管制区", "bbox": [0.5, 0.4, 0.4, 0.6]}])
    message = str(failure.value)
    assert "hard_constraints[0]" in message
    assert "管制区" in message
    assert "west" in message and "east" in message
    assert "无约束" in message or "非法输入" in message


# --------------------------------------------------------------------------------------
# 2. Exact OD scenario routing
# --------------------------------------------------------------------------------------


def test_explicit_od_creates_only_the_requested_pair(tmp_path):
    workflow = workflow_with_routes(tmp_path)
    state = workflow.generate_scenario_od("N001", "N002", "ab")
    assert [(item["start_node_id"], item["end_node_id"]) for item in state["scenario_routes"]] == [("N001", "N002")]
    assert state["scenario_routes"][0]["route_id"] == "R0001"
    assert state["scenario_routes"][0]["kind"] == "scenario"


def test_explicit_od_never_produces_an_all_pairs_mesh(tmp_path):
    workflow = workflow_with_routes(tmp_path)
    workflow.generate_scenario_od("N001", "N002", "ab")
    # Three nodes would give three undirected pairs under the legacy generator.
    assert len(workflow.state["scenario_routes"]) == 1
    assert not any(
        item["end_node_id"] == "N003" for item in workflow.state["scenario_routes"]
    )


def test_explicit_od_both_directions_get_distinct_route_ids(tmp_path):
    workflow = workflow_with_routes(tmp_path)
    state = workflow.generate_scenario_od("N001", "N003", "both")
    routes = state["scenario_routes"]
    assert len(routes) == 2
    assert len({item["route_id"] for item in routes}) == 2
    assert {(item["start_node_id"], item["end_node_id"]) for item in routes} == {("N001", "N003"), ("N003", "N001")}


def test_explicit_od_reuses_identical_direction_and_retires_the_removed_one(tmp_path):
    workflow = workflow_with_routes(tmp_path)
    first = workflow.generate_scenario_od("N001", "N002", "both")
    forward = next(item for item in first["scenario_routes"] if item["start_node_id"] == "N001")
    reverse = next(item for item in first["scenario_routes"] if item["start_node_id"] == "N002")
    second = workflow.generate_scenario_od("N001", "N002", "ab")
    assert [item["route_id"] for item in second["scenario_routes"]] == [forward["route_id"]]
    assert reverse["route_id"] in second["retired_route_ids"]
    again = workflow.generate_scenario_od("N001", "N002", "ab")
    assert [item["route_id"] for item in again["scenario_routes"]] == [forward["route_id"]]


def test_explicit_od_clears_operational_routes_and_marks_routes_not_calculated(tmp_path):
    workflow = workflow_with_routes(tmp_path)
    workflow.generate_scenario_od("N001", "N002", "ab")
    response = workflow.generate_operational([])
    # B2B-1R：旧版 planner 只写 runtime-only cache / response；canonical 运行航路保持为空。
    assert workflow.state["operational_routes"] == []
    assert response["compatibility_operational_routes"]["items"]
    state = workflow.generate_scenario_od("N002", "N003", "ab")
    assert state["operational_routes"] == []
    assert "results" not in (state.get("compatibility") or {})
    assert not workflow.session._runtime_compatibility_results
    assert state["result_statuses"]["routes"] == "not_calculated"


@pytest.mark.parametrize(
    ("start", "end", "direction", "match"),
    [
        ("N404", "N002", "ab", "起点"),
        ("N001", "N404", "ab", "终点"),
        ("N001", "N001", "ab", "同一个"),
        ("N001", "N002", "sideways", "方向"),
    ],
)
def test_explicit_od_rejects_invalid_requests(tmp_path, start, end, direction, match):
    workflow = workflow_with_routes(tmp_path)
    with pytest.raises(ValueError, match=match):
        workflow.generate_scenario_od(start, end, direction)
    assert workflow.state["scenario_routes"] == []


def test_legacy_all_pairs_generator_keeps_its_existing_contract(tmp_path):
    workflow = workflow_with_routes(tmp_path)
    state = workflow.generate_scenario("both")
    # Unchanged compatibility behaviour: 3 nodes -> 3 undirected pairs -> 6 routes.
    assert len(state["scenario_routes"]) == 6
    assert len({item["route_id"] for item in state["scenario_routes"]}) == 6


class OdApiWorkflow:
    def __init__(self):
        self.calls = []

    def generate_scenario(self, direction):
        self.calls.append(("scenario", direction))
        return {"kind": "scenario", "direction": direction}

    def generate_scenario_od(self, start_node_id, end_node_id, direction):
        self.calls.append(("scenario-od", start_node_id, end_node_id, direction))
        return {"kind": "scenario-od", "start_node_id": start_node_id, "end_node_id": end_node_id, "direction": direction}

    def candidate_sites_from_existing(self):
        return {}

    def analyze_cns_gaps(self):
        return {}


class OdApiContext:
    def __init__(self):
        self.workflow = OdApiWorkflow()
        self.data = object()


def test_scenario_od_api_action_is_additive_and_forwards_payload():
    context = OdApiContext()
    router = ApiRouter(context)
    body = router.post(
        "/api/workflow/scenario-od",
        {"start_node_id": "N001", "end_node_id": "N002", "direction": "ab"},
    ).data
    assert body == {"kind": "scenario-od", "start_node_id": "N001", "end_node_id": "N002", "direction": "ab"}
    assert context.workflow.calls == [("scenario-od", "N001", "N002", "ab")]
    legacy = router.post("/api/workflow/scenario", {"direction": "both"}).data
    assert legacy == {"kind": "scenario", "direction": "both"}


# --------------------------------------------------------------------------------------
# 3. Planner manifest metadata
# --------------------------------------------------------------------------------------


def registry():
    import json

    return build_default_algorithm_registry(json.loads(DEFAULTS.read_text(encoding="utf-8")))


def test_v1_manifest_matches_real_output_and_declares_grid_size_default():
    manifest = registry().manifest("route_planner", "route_planner_v1", "1.0")
    schema = manifest.parameter_schema
    assert schema["properties"]["grid_size"]["default"] == 56
    assert schema["properties"]["grid_size"]["minimum"] == 2
    assert schema["additionalProperties"] is False
    assert RoutePlannerV1().grid_size == 56
    assert set(manifest.outputs) <= {
        "operational_route", "path", "algorithm_id", "algorithm_version",
        "input_fingerprint", "environment_risk", "reason", "status",
    }


def test_v1_manifest_limitations_state_all_four_declared_limits():
    manifest = registry().manifest("route_planner", "route_planner_v1", "1.0")
    joined = " ".join(manifest.limitations)
    for token in ("经纬度固定格", "非米制搜索", "BBOX 硬约束", "无风险/高度/运动学"):
        assert token in joined


def test_v2_manifest_excludes_display_airspace_and_keeps_2d_limits():
    manifest = registry().manifest("route_planner", "risk_aware_route_planner_v2", "2.0")
    assert "airspace_eligibility" not in manifest.inputs
    joined = " ".join(manifest.limitations)
    assert "二维战略水平规划" in joined
    assert "网格中心" in joined or "grid" in joined
    assert "平滑" in joined


def test_manifests_describe_the_planners_that_actually_run():
    manifest = registry().manifest("route_planner", "risk_aware_route_planner_v2", "2.0")
    planner = RiskAwareRoutePlannerV2()
    assert planner.algorithm_id == manifest.algorithm_id
    assert planner.algorithm_version == manifest.version
    assert "airspace_eligibility" not in manifest.inputs
    planner_v1 = RoutePlannerV1()
    manifest_v1 = registry().manifest("route_planner", "route_planner_v1", "1.0")
    assert manifest_v1.algorithm_id == planner_v1.algorithm_id
    assert manifest_v1.version == planner_v1.algorithm_version


def test_v1_output_contract_is_unchanged_by_manifest_edits():
    planner = RoutePlannerV1(grid_size=6)
    route = {"route_id": "R-GOLDEN-001", "start": [0.1, 0.1], "end": [0.9, 0.9]}
    result = planner.plan(route, [0.0, 0.0, 1.0, 1.0], [{"name": "中心硬约束", "bbox": [0.4, 0.4, 0.6, 0.6]}])
    assert list(result) == [
        "route_id", "status", "path", "reason", "algorithm_id", "algorithm_version",
        "input_fingerprint", "environment_risk",
    ]
    assert result["input_fingerprint"] == "ae10b604508d03f2445f36573156ba109e2ed30904d6e8ff5957f0dce8a936d4"


def test_default_route_planner_selection_is_still_v1():
    # B7X：新项目不再持久化 legacy ``route_planner`` selection；冻结的 V1 基线改由
    # CompatibilitySelectionAdapter 解析，新项目本身不再依赖它。
    selection = default_algorithm_selection()
    assert "route_planner" not in selection
    adapter = CompatibilitySelectionAdapter({"algorithm_selection": {}}, None)
    frozen = adapter.selection("route_planner")
    assert frozen["algorithm_id"] == "route_planner_v1"
    assert frozen["version"] == "1.0"
    assert frozen["selection_source"] == "frozen_compatibility_baseline"


# --------------------------------------------------------------------------------------
# 4. Low-risk cleanup guarantees
# --------------------------------------------------------------------------------------


def test_unused_domain_route_protocol_is_gone():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("cns_planner.domain.route")
    module = importlib.import_module("cns_planner.algorithms.route.v1")
    assert not hasattr(module, "RoutePlan")
    assert hasattr(module, "RoutePlannerV1")


def test_pytest_tmp_is_gitignored_and_not_tracked():
    ignored = Path(".gitignore").read_text(encoding="utf-8")
    assert ".pytest_tmp/" in ignored


def test_docs_03_is_marked_as_a_historical_schema_v1_draft():
    header = Path("docs/03-架构与数据字典.md").read_text(encoding="utf-8")[:600]
    assert "历史设计稿" in header
    assert "schema-v1" in header
    assert "AI_DEV_CONTEXT" in header
    assert "CNS_TECHNICAL_BASELINE" in header
