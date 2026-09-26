from copy import deepcopy
import json
from pathlib import Path

import pytest

from cns_planner.algorithms.registry import (
    AlgorithmNotFoundError, AlgorithmRegistry, build_default_algorithm_registry,
    default_algorithm_selection,
)
from cns_planner.api.router import ApiRouter
from cns_planner.application.project_state import normalize_project
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.algorithm_manifest import AlgorithmManifest
from cns_planner.persistence.project_repository import ProjectRepository


DEFAULTS_PATH = Path("cns_planner/config/defaults.json")


def defaults():
    return json.loads(DEFAULTS_PATH.read_text(encoding="utf-8"))


def dummy_manifest(algorithm_type, suffix="dummy"):
    return AlgorithmManifest(
        algorithm_type=algorithm_type,
        algorithm_id=f"{algorithm_type}_{suffix}",
        version="9.0",
        name=f"Dummy {algorithm_type}",
        provider="tests",
        maturity="test_fixture",
        description="Deterministic test fixture",
        inputs=("fixture_input",),
        outputs=("fixture_output",),
        parameter_schema={"type": "object"},
        assumptions=("fixture only",),
        limitations=("not production",),
        references=(),
    )


class DummyAlgorithm:
    def __init__(self, algorithm_id, algorithm_version, parameters):
        self.algorithm_id = algorithm_id
        self.algorithm_version = algorithm_version
        self.parameters = parameters


def register_dummy(registry, algorithm_type):
    manifest = dummy_manifest(algorithm_type)
    registry.register(
        manifest,
        lambda parameters: DummyAlgorithm(manifest.algorithm_id, manifest.version, parameters),
    )
    return manifest


def test_default_registry_has_exact_v1_manifests_and_no_python_paths():
    registry = build_default_algorithm_registry(defaults())
    actual = {(item.algorithm_type, item.algorithm_id, item.version) for item in registry.manifests()}
    assert actual == {
        ("risk_model", "risk-model-v1-relative-index", "1.1"),
        ("route_planner", "route_planner_v1", "1.0"),
        ("route_planner", "risk_aware_route_planner_v2", "2.0"),
        ("layered_route_planner", "layered_route_planner_v1", "1.0"),
        ("layered_route_planner", "layered_risk_aware_theta_star_v2", "2.0"),
        ("coverage_planner", "coverage_planner_v1", "1.0"),
        ("cns_gap_analyzer", "cns_gap_analysis_v1", "1.0"),
        ("cns_gap_analyzer", "cns_gap_analysis_v2", "2.0"),
        ("coverage_model", "geometric_coverage_3d_v1", "1.0"),
        ("service_model", "cns_service_capability_v1", "1.0"),
        ("timeline_model", "route_service_timeline_v1", "1.0"),
        ("protection_model", "tactical_protection_envelope_v1", "1.0"),
            ("site_planner", "reuse_first_site_planner_v1", "1.0"),
            ("site_planner", "corridor_reuse_first_site_planner_v2", "2.0"),
        ("corridor_model", "cns_service_corridor_v1", "1.0"),
        ("corridor_gap_analyzer", "cns_corridor_gap_v1", "1.0"),
        ("requirement_model", "manual_required_cns_v1", "1.0"),
        ("requirement_model", "operational_context_required_cns_v2", "2.0"),
    }
    for item in registry.catalog():
        assert set(item) == {
            "algorithm_type", "algorithm_id", "version", "name", "provider",
            "maturity", "description", "inputs", "outputs", "parameter_schema",
            "assumptions", "limitations", "references",
        }
        assert "python" not in item and "module" not in item and "class" not in item


def test_registry_requires_exact_id_and_version_without_fallback():
    registry = build_default_algorithm_registry(defaults())
    with pytest.raises(AlgorithmNotFoundError, match="未注册精确算法"):
        registry.create("route_planner", "route_planner_v1", "9.9")
    with pytest.raises(AlgorithmNotFoundError, match="missing"):
        registry.create("risk_model", "missing", "1.0")


def test_schema_v2_backfills_default_selection_and_roundtrips(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS_PATH)
    document = deepcopy(workflow.state)
    document.pop("algorithm_selection")
    normalized = normalize_project(document, workflow.grid_service)
    assert normalized["algorithm_selection"] == default_algorithm_selection()
    repository = ProjectRepository(tmp_path / "roundtrip.json")
    repository.save(normalized)
    restored = normalize_project(repository.load(), workflow.grid_service)
    assert restored["algorithm_selection"] == default_algorithm_selection()


def test_unknown_saved_selection_is_resolved_by_the_compatibility_adapter(tmp_path):
    """未知的旧 ``route_planner`` selection 不能让旧项目打不开，也不得被改写。

    B7X 之后 assembly 走 CompatibilitySelectionAdapter：无法解析的旧值回退到冻结
    兼容基线（并如实标注来源），而**绝不**抛 AlgorithmNotFoundError 或写回 state。
    """

    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS_PATH)
    workflow.state["algorithm_selection"]["route_planner"] = {
        "algorithm_type": "route_planner", "algorithm_id": "missing",
        "version": "3.0", "parameters": {},
    }
    workflow.save()
    reopened = WorkflowService(tmp_path / "project.json", DEFAULTS_PATH)
    selection = reopened.compatibility_selection.selection("route_planner")
    assert selection["algorithm_id"] == "route_planner_v1"
    assert selection["selection_source"] == "frozen_compatibility_baseline_unresolvable_saved_selection"
    assert reopened.state["algorithm_selection"]["route_planner"]["algorithm_id"] == "missing"
    # 非 compatibility 类型的未知算法仍然 fail-closed。
    reopened.state["algorithm_selection"]["risk_model"] = {
        "algorithm_type": "risk_model", "algorithm_id": "missing", "version": "3.0",
        "parameters": {},
    }
    reopened.save()
    with pytest.raises(AlgorithmNotFoundError, match="missing@3.0"):
        WorkflowService(tmp_path / "project.json", DEFAULTS_PATH)


def test_new_project_rejects_persisting_a_compatibility_route_selection(tmp_path):
    registry = build_default_algorithm_registry(defaults())
    manifest = register_dummy(registry, "route_planner")
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS_PATH, algorithm_registry=registry)
    workflow.state["result_statuses"].update({"routes": "passed", "coverage": "passed", "cns_gap": "passed", "report": "passed"})
    workflow.state["coverage"] = {"status": "passed"}
    workflow.state["cns_gap_analysis"] = {"status": "passed"}

    with pytest.raises(ValueError, match="Compatibility/Archive"):
        workflow.select_algorithm({
            "algorithm_type": "route_planner", "algorithm_id": manifest.algorithm_id,
            "version": manifest.version, "parameters": {"fixture": 1},
        })
    assert "route_planner" not in workflow.state["algorithm_selection"]


def test_same_selection_is_noop_and_invalid_selection_preserves_current(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS_PATH)
    current = deepcopy(workflow.state["algorithm_selection"]["risk_model"])
    before_saved = workflow.state["last_saved_at"]
    workflow.select_algorithm(current)
    assert workflow.state["last_saved_at"] == before_saved
    with pytest.raises(AlgorithmNotFoundError):
        workflow.select_algorithm({**current, "version": "missing"})
    assert workflow.state["algorithm_selection"]["risk_model"] == current


@pytest.mark.parametrize(
    ("algorithm_type", "expected"),
    [
        ("layered_route_planner", {"layered_route_candidate", "report"}),
        ("coverage_model", {"coverage_3d", "cns_service_capability", "service_timeline", "report"}),
        ("service_model", {"cns_service_capability", "service_timeline", "report"}),
        ("timeline_model", {"service_timeline", "report"}),
        ("protection_model", {"protection_envelope", "report"}),
        # B7X：P11 站址试算是只读 compatibility 结果，定向失效只发生在 runtime-only
        # cache，不再改写遗留的 result_statuses。
        ("site_planner", {"report"}),
    ],
)
def test_dummy_selection_uses_directed_invalidation(tmp_path, algorithm_type, expected):
    registry = build_default_algorithm_registry(defaults())
    manifest = register_dummy(registry, algorithm_type)
    workflow = WorkflowService(tmp_path / f"{algorithm_type}.json", DEFAULTS_PATH, algorithm_registry=registry)
    workflow.state["result_statuses"].update({name: "passed" for name in ("routes", "coverage", "cns_gap", "coverage_3d", "cns_service_capability", "service_timeline", "protection_envelope", "cns_site_plan", "technical_risk", "report")})
    workflow.state["coverage"] = {"status": "passed"}
    workflow.state["cns_gap_analysis"] = {"status": "passed"}
    workflow.state["cns_service_capability"]["status"] = "meets_under_model"
    workflow.state["service_timeline"]["status"] = "passed"
    workflow.state["protection_envelope"]["status"] = "passed"
    workflow.state["cns_site_plan"] = {"status": "proposal_ready"}
    workflow.select_algorithm({
        "algorithm_type": algorithm_type, "algorithm_id": manifest.algorithm_id,
        "version": manifest.version, "parameters": {},
    })
    for name in expected:
        assert workflow.state["result_statuses"][name] == "stale"
    assert workflow.state["result_statuses"]["routes"] == "passed"
    assert workflow.state["result_statuses"]["technical_risk"] == "passed"


def test_dummy_risk_selection_only_stales_risk(tmp_path):
    registry = build_default_algorithm_registry(defaults())
    manifest = register_dummy(registry, "risk_model")
    workflow = WorkflowService(tmp_path / "risk.json", DEFAULTS_PATH, algorithm_registry=registry)
    workflow.state["grid_risk"]["status"] = "passed"
    workflow.state["result_statuses"].update({"environment_risk": "passed", "routes": "passed", "coverage": "passed"})
    workflow.select_algorithm({
        "algorithm_type": "risk_model", "algorithm_id": manifest.algorithm_id,
        "version": manifest.version, "parameters": {"fixture": True},
    })
    assert workflow.state["grid_risk"]["status"] == "stale"
    assert workflow.state["result_statuses"]["environment_risk"] == "stale"
    assert workflow.state["result_statuses"]["routes"] == "passed"
    assert workflow.state["result_statuses"]["coverage"] == "passed"
    assert workflow.risk_service.risk_model.parameters == {"fixture": True}


class ApiWorkflow:
    def algorithms_snapshot(self): return {"status": "passed", "items": [], "selection": {}}
    def select_registered_algorithm(self, payload): return {"selected": payload}
    def candidate_sites_from_existing(self): return {}
    def analyze_cns_gaps(self): return {}


class ApiContext:
    workflow = ApiWorkflow()
    data = object()


def test_algorithm_api_routes_are_additive_and_forward_payload():
    router = ApiRouter(ApiContext())
    assert router.get("/api/algorithms", {}, {}).data["status"] == "passed"
    payload = {"algorithm_type": "risk_model", "algorithm_id": "fixture", "version": "1"}
    assert router.post("/api/algorithms/select", payload).data == {"selected": payload}
