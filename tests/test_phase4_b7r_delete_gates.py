"""Phase4-B7R delete-gate evaluator 的定向测试。

覆盖 B7R.2 writer ownership，并自校验 `docs/phase4_b7_delete_gates.json`：

1. fixture 存在且真 open/read/export/roundtrip → ``old_fixture_read`` 通过；
2. canonical writer 写同一 result key 不阻止 legacy capability 删除；
3. target legacy service 的真实 persistent state writer 仍阻塞；
4. runtime compatibility cache 不算 persistent writer；
5. RoutePlannerV1/RiskAwareRoutePlannerV2 不被 canonical adoption writer 误报；
6. LayeredRoutePlannerV1 的共享 candidate writer 正确归属；
7. RoutePlannerV3 专属 writer 仍阻塞；
8. writer evidence 包含 owner / ownership_class；
9. 报告与实时重扫一致；
10. P14 受保护四文件 SHA256 前后不变。

同时保留 B7R 已有的 fixture、AST mutation、default selection、production import 与
tests_migrated 回归覆盖。
"""

from __future__ import annotations

import importlib.util
import json
from hashlib import sha256
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = ROOT / "tools" / "b7x_delete_gates.py"
REPORT_PATH = ROOT / "docs" / "phase4_b7_delete_gates.json"
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "compatibility" / "legacy_archive_cases.json"


def _load_tool():
    spec = importlib.util.spec_from_file_location("b7r_delete_gates_tool", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tool():
    return _load_tool()


def _gate(tool, capability_id):
    for gate in tool.GATES:
        if gate["capability_id"] == capability_id:
            return gate
    raise AssertionError(f"未知 capability：{capability_id}")


def _report():
    return json.loads(REPORT_PATH.read_text(encoding="utf-8"))


# --------------------------------------------------------------- 1. fixture roundtrip


def test_fixture_roundtrip_makes_old_fixture_read_pass(tool):
    """fixture 存在 + 实测 open/read/export/roundtrip → old_fixture_read 通过。"""

    cases = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    results = tool.run_fixture_roundtrip()

    required = (
        "RoutePlannerV1",
        "RiskAwareRoutePlannerV2",
        "LayeredRoutePlannerV1",
        "CoveragePlannerV1",
        "CNSGapAnalyzerV1",
        "ReuseFirstSitePlannerV1",
        "RoutePlannerV3",
    )
    for capability_id in required:
        gate = _gate(tool, capability_id)
        assert gate["fixture_cases"], capability_id
        items = tool.scan_old_fixture_read(results, gate)
        assert items, capability_id
        for item in items:
            assert item["fixture_case"] in cases
            assert item["fixture_present"] is True
            assert item["covers_capability"] is True, (capability_id, item)
            assert item["opened"] is True, (capability_id, item)
            assert item["exported"] is True, (capability_id, item)
            assert item["roundtrip"] is True, (capability_id, item)
            assert item["passed"] is True, (capability_id, item)


def test_old_fixture_read_is_not_decided_by_missing_strings(tool):
    """判据是实测 roundtrip，而不是"字符串没出现"。"""

    gate = _gate(tool, "RoutePlannerV1")
    cases = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    # 提供一个"存在但没有真正 roundtrip 成功"的结果：必须判失败。
    broken = {
        name: {"present": True, "opened": False, "exported": False, "roundtrip": False, "detail": "open 失败"}
        for name in cases
    }
    items = tool.scan_old_fixture_read(broken, gate)
    assert items and all(item["passed"] is False for item in items)


def test_compatibility_test_really_asserts_roundtrip(tool):
    coverage = tool._compatibility_test_coverage()
    for gate in tool.GATES:
        witness = coverage[gate["capability_id"]]
        assert witness["test"] == "test_all_legacy_archive_fixtures_open_read_export_and_roundtrip"
        assert witness["compatibility_test_asserts_roundtrip"] is True


# --------------------------------------------------------------- 2./3. project_state writer


def test_state_read_is_not_a_writer(tool):
    """state.get / state[...] 读取绝不能被算成 writer。"""

    source = (
        "def read(state):\n"
        "    routes = state.get('operational_routes')\n"
        "    current = state['coverage']\n"
        "    value = state.get('cns_gap_analysis', {})\n"
        "    layered = state.get('layered_route_candidates')\n"
        "    return routes, current, value, layered\n"
    )
    gate = {
        "result_keys": (
            "operational_routes", "coverage", "cns_gap_analysis", "layered_route_candidates",
        )
    }
    assert tool.scan_state_writers([("cns_planner/application/sample.py", source)], gate) == []


def test_state_mutation_is_a_writer(tool):
    """真实 mutation（赋值 / del / update / setdefault / pop / append）必须被识别。"""

    source = (
        "def write(state):\n"
        "    state['coverage'] = {}\n"
        "    state['coverage'].update({'a': 1})\n"
        "    del state['cns_gap_analysis']\n"
        "    state.setdefault('cns_site_plan', {})\n"
        "    state['operational_routes'].append({})\n"
        "    state.pop('route_planner_v3_experiments')\n"
    )
    keys = (
        "coverage", "cns_gap_analysis", "cns_site_plan",
        "operational_routes", "route_planner_v3_experiments",
    )
    hits = tool.scan_state_writers([("cns_planner/application/sample.py", source)], {"result_keys": keys})
    assert {hit["result_key"] for hit in hits} == set(keys)
    assert {hit["kind"] for hit in hits} == {
        "state_key_assignment",
        "state_key_method_mutation",
        "state_key_delete",
        "state_container_method_mutation",
    }
    for hit in hits:
        assert hit["file"] == "cns_planner/application/sample.py"
        assert isinstance(hit["line"], int) and hit["line"] > 0
        assert hit["owner"] == "write"
        assert hit["ownership_class"] == "target_legacy_writer"
        assert hit["detail"]


def test_canonical_writer_for_same_result_key_is_not_a_legacy_blocker(tool):
    source = (
        "class LayeredOperationalAdoptionService:\n"
        "    def publish(self, state):\n"
        "        state['operational_routes'] = []\n"
    )
    hits = tool.scan_state_writers(
        [("cns_planner/application/sample.py", source)],
        _gate(tool, "RoutePlannerV1"),
    )
    assert len(hits) == 1
    assert hits[0]["owner"] == "LayeredOperationalAdoptionService"
    assert hits[0]["ownership_class"] == "canonical_writer"


def test_target_legacy_service_persistent_writer_is_a_blocker(tool):
    source = (
        "class LegacyRouteWriterService:\n"
        "    def publish(self, state):\n"
        "        state['operational_routes'] = []\n"
    )
    gate = {
        "capability_id": "SyntheticLegacyWriter",
        "algorithm_ids": (),
        "symbols": (),
        "legacy_modules": (),
        "registry_entries": (),
        "defining_prefixes": (),
        "result_keys": ("operational_routes",),
        "api_prefixes": (),
        "frontend_prefixes": (),
        "fixture_cases": (),
        "owned_modules": (),
        "owned_services": ("LegacyRouteWriterService",),
        "owned_writer_symbols": (),
    }
    sources = [("cns_planner/application/legacy.py", source)]
    hits = tool.scan_state_writers(sources, gate)
    assert len(hits) == 1
    assert hits[0]["ownership_class"] == "target_legacy_writer"
    evaluated = tool.evaluate_gate(gate, {
        "production_sources": sources,
        "api_sources": [],
        "frontend_sources": [],
        "test_sources": [],
        "default_selection": [],
        "fixture_results": {},
        "fixture_coverage": {"SyntheticLegacyWriter": {}},
    })
    assert evaluated["checks"]["project_state_writer"] is False
    assert evaluated["evidence"]["project_state_writer"]["items"] == hits


def test_runtime_compatibility_cache_is_not_a_persistent_writer(tool):
    source = (
        "def evaluate(session):\n"
        "    return write_runtime_compatibility_result(\n"
        "        session, 'operational_routes', {'items': []})\n"
    )
    hits = tool.scan_state_writers(
        [("cns_planner/application/legacy_runtime.py", source)],
        _gate(tool, "RoutePlannerV1"),
    )
    assert len(hits) == 1
    assert hits[0]["kind"] == "runtime_compatibility_cache_mutation"
    assert hits[0]["ownership_class"] == "compatibility_runtime_only"


def test_state_writer_evidence_points_at_the_real_place(tool):
    """报告里 RoutePlannerV3 的 writer evidence 必须指向真实写点。"""

    item = next(i for i in _report()["items"] if i["capability_id"] == "RoutePlannerV3")
    writer = item["evidence"]["project_state_writer"]
    assert writer["passed"] is False and writer["total"] > 0
    for hit in writer["items"]:
        assert hit["result_key"] == "route_planner_v3_experiments"
        assert hit["kind"] == "state_key_assignment"
        assert hit["owner"] == "RoutePlannerV3ExperimentService"
        assert hit["ownership_class"] == "target_legacy_writer"
        source = (ROOT / hit["file"]).read_text(encoding="utf-8").splitlines()
        line = source[hit["line"] - 1]
        assert "route_planner_v3_experiments" in line and "=" in line


@pytest.mark.parametrize("capability_id", ("RoutePlannerV1", "RiskAwareRoutePlannerV2"))
def test_operational_route_canonical_writers_do_not_block_legacy_planners(tool, capability_id):
    item = next(i for i in _report()["items"] if i["capability_id"] == capability_id)
    writer = item["evidence"]["project_state_writer"]
    assert item["checks"]["project_state_writer"] is True
    assert writer["items"] == []
    evidence = writer["non_blocking_writer_evidence"]
    assert any(
        hit["owner"] == "LayeredOperationalAdoptionService"
        and hit["ownership_class"] == "canonical_writer"
        for hit in evidence
    )
    assert any(
        hit["owner"] == "RouteService"
        and hit["ownership_class"] == "canonical_writer"
        for hit in evidence
    )


def test_layered_v1_shared_candidate_writer_is_non_blocking(tool):
    item = next(i for i in _report()["items"] if i["capability_id"] == "LayeredRoutePlannerV1")
    writer = item["evidence"]["project_state_writer"]
    assert item["checks"]["project_state_writer"] is True
    assert writer["items"] == []
    evidence = writer["non_blocking_writer_evidence"]
    assert evidence
    assert {hit["owner"] for hit in evidence} == {"LayeredRoutePlannerService"}
    assert {hit["ownership_class"] for hit in evidence} == {"shared_neutral_writer"}


def test_route_planner_v3_owned_writer_still_blocks(tool):
    item = next(i for i in _report()["items"] if i["capability_id"] == "RoutePlannerV3")
    writer = item["evidence"]["project_state_writer"]
    assert item["checks"]["project_state_writer"] is False
    assert writer["items"]
    assert {hit["owner"] for hit in writer["items"]} == {"RoutePlannerV3ExperimentService"}
    assert {hit["ownership_class"] for hit in writer["items"]} == {"target_legacy_writer"}


def test_every_writer_evidence_item_has_owner_and_class(tool):
    for gate_item in _report()["items"]:
        writer = gate_item["evidence"]["project_state_writer"]
        all_items = writer["items"] + writer["non_blocking_writer_evidence"]
        for hit in all_items:
            assert {
                "file", "line", "kind", "result_key", "owner", "ownership_class", "detail",
            } <= set(hit)
            assert hit["ownership_class"] in tool.WRITER_OWNERSHIP_CLASSES


# --------------------------------------------------------------- 4./5. default_selection


def test_v3_experiment_result_key_is_not_default_selection(tool):
    entries = tool.default_selection_audit()
    gate = _gate(tool, "RoutePlannerV3")
    assert "route_planner_v3_experiments" in gate["result_keys"]
    assert tool.scan_default_selection(entries, gate) == []
    assert "route_planner_v3_experiments" not in {entry["algorithm_id"] for entry in entries}


def test_default_selection_never_substring_matches(tool):
    """result key / 生产算法 id 的 substring 绝不能造成 default_selection 误判。"""

    synthetic = [
        {"algorithm_type": "route_planner_v3_experiments", "algorithm_id": "route_planner_v3_experiments"},
        {"algorithm_type": "site_planner", "algorithm_id": "corridor_reuse_first_site_planner_v2"},
        {"algorithm_type": "layered_route_planner", "algorithm_id": "layered_risk_aware_theta_star_v2"},
    ]
    for capability_id in ("RoutePlannerV3", "ReuseFirstSitePlannerV1", "LayeredRoutePlannerV1"):
        assert tool.scan_default_selection(synthetic, _gate(tool, capability_id)) == [], capability_id
    # 反向对照：精确命中必须识别出来。
    assert tool.scan_default_selection(
        [{"algorithm_type": "coverage_planner", "algorithm_id": "coverage_planner_v1"}],
        _gate(tool, "CoveragePlannerV1"),
    )


def test_default_selection_uses_live_default_algorithm_selection(tool):
    """直接基于 default_algorithm_selection()：7 个 legacy capability 都不在默认 selection。"""

    from cns_planner.algorithms.registry import default_algorithm_selection

    entries = tool.default_selection_audit()
    assert entries == sorted(
        (
            {"algorithm_type": key, "algorithm_id": str(value.get("algorithm_id") or "")}
            for key, value in default_algorithm_selection().items()
        ),
        key=lambda item: item["algorithm_type"],
    )
    for capability_id in (
        "RoutePlannerV1",
        "RiskAwareRoutePlannerV2",
        "LayeredRoutePlannerV1",
        "RoutePlannerV3",
        "CoveragePlannerV1",
        "CNSGapAnalyzerV1",
        "ReuseFirstSitePlannerV1",
    ):
        assert tool.scan_default_selection(entries, _gate(tool, capability_id)) == [], capability_id
    snapshot_ids = {entry["algorithm_id"] for entry in entries}
    # CorridorReuseFirstSitePlannerV2 是 production，不能被误判成 ReuseFirstSitePlannerV1。
    assert "corridor_reuse_first_site_planner_v2" in snapshot_ids
    assert "reuse_first_site_planner_v1" not in snapshot_ids


# --------------------------------------------------------------- 6. production imports


def test_comment_and_string_are_not_production_imports(tool):
    gate = _gate(tool, "CNSGapAnalyzerV1")
    text_only = (
        '"""from cns_planner.gap.v1 import CNSGapAnalyzerV1"""\n'
        "\n"
        "# from cns_planner.gap.v1 import CNSGapAnalyzerV1\n"
        "MESSAGE = 'cns_planner.gap.v1 -> CNSGapAnalyzerV1'\n"
        "ERROR = \"CNSGapAnalyzerV1 不可用：cns_planner.gap.v1\"\n"
        "\n"
        "def label():\n"
        "    return 'CNSGapAnalyzerV1'\n"
    )
    assert tool.scan_production_imports(
        [("cns_planner/application/sample.py", text_only)], gate
    ) == []


def test_real_import_is_a_production_import(tool):
    gate = _gate(tool, "CNSGapAnalyzerV1")
    relative = "from ..gap.v1 import CNSGapAnalyzerV1\n"
    hits = tool.scan_production_imports([("cns_planner/application/sample.py", relative)], gate)
    assert len(hits) == 1
    assert hits[0]["module"] == "cns_planner.gap.v1"
    assert hits[0]["name"] == "CNSGapAnalyzerV1"
    assert hits[0]["kind"] == "from_import"

    absolute = "import cns_planner.gap.v1\n"
    hits = tool.scan_production_imports([("cns_planner/application/sample.py", absolute)], gate)
    assert [hit["kind"] for hit in hits] == ["import"]


def test_defining_module_itself_is_not_a_production_import(tool):
    """legacy 模块自身的 re-export 不算 production import。"""

    gate = _gate(tool, "RoutePlannerV1")
    text = (ROOT / "cns_planner/algorithms/route/__init__.py").read_text(encoding="utf-8")
    assert tool.scan_production_imports([("cns_planner/algorithms/route/__init__.py", text)], gate) == []


# --------------------------------------------------------------- 7./8. tests_migrated


def test_guard_test_mention_is_not_a_migration_blocker(tool):
    """仅断言 legacy 不可写 / 字符串提及 / 只读 manifest 不算 migration blocker。"""

    gate = _gate(tool, "RoutePlannerV1")
    guard_like = (
        "def test_legacy_is_not_authoritative(state, registry):\n"
        "    assert 'RoutePlannerV1' not in repr(state)\n"
        "    view = read_existing_legacy(state, 'operational_routes', 'RoutePlannerV1')\n"
        "    assert view['authoritative'] is False\n"
        "    manifest = registry.manifest('route_planner', 'route_planner_v1', '1.0')\n"
        "    assert manifest.algorithm_id == 'route_planner_v1'\n"
    )
    assert tool.scan_test_execution([("tests/test_guard_sample.py", guard_like)], gate) == []


def test_guard_test_allowlist_is_explicit_and_effective(tool):
    """guard allowlist 必须显式登记、可审计，并且真的让 guard 文件免于误判。"""

    assert tool.GUARD_TEST_ALLOWLIST, "guard allowlist 不能为空"
    for path, reason in tool.GUARD_TEST_ALLOWLIST.items():
        assert reason.strip()
        assert (ROOT / path).exists(), path

    gate = _gate(tool, "RiskAwareRoutePlannerV2")
    rel = "tests/test_production_write_authority.py"
    text = (ROOT / rel).read_text(encoding="utf-8")
    # 该文件确实构造了 legacy v2 planner（否则这条 allowlist 没有意义）。
    assert '"risk_aware_route_planner_v2", "2.0"' in text
    assert tool.scan_test_execution([(rel, text)], gate) == []


def test_real_legacy_instantiation_is_a_migration_blocker(tool):
    gate = _gate(tool, "CoveragePlannerV1")
    source = "def test_characterises_v1():\n    planner = CoveragePlannerV1()\n    assert planner.algorithm_id\n"
    hits = tool.scan_test_execution([("tests/test_sample.py", source)], gate)
    assert len(hits) == 1
    assert hits[0]["kind"] == "legacy_algorithm_instantiation"
    assert hits[0]["symbol"] == "CoveragePlannerV1"

    # 真实仓库里 characterization 测试仍然算 blocker。
    rel = "tests/test_v1_algorithm_characterization.py"
    text = (ROOT / rel).read_text(encoding="utf-8")
    assert tool.scan_test_execution([(rel, text)], gate)


def test_registry_construction_requires_exact_registered_triple(tool):
    gate = _gate(tool, "RoutePlannerV1")
    real = 'def test_x(registry):\n    registry.create("route_planner", "route_planner_v1", "1.0")\n'
    hits = tool.scan_test_execution([("tests/test_sample.py", real)], gate)
    assert [hit["kind"] for hit in hits] == ["legacy_algorithm_registry_construction"]
    assert hits[0]["registry_entry"] == ["route_planner", "route_planner_v1", "1.0"]

    # 负向断言用的不存在版本不算"真的构造了 legacy 实例"。
    negative = (
        "def test_y(registry):\n"
        "    with pytest.raises(AlgorithmNotFoundError):\n"
        "        registry.create(\"route_planner\", \"route_planner_v1\", \"9.9\")\n"
    )
    assert tool.scan_test_execution([("tests/test_sample.py", negative)], gate) == []


# --------------------------------------------------------------- 9. report 自校验


def test_report_is_self_consistent_and_validates(tool):
    report = _report()
    assert report["generator_schema_version"] == tool.GENERATOR_SCHEMA_VERSION
    assert report["mode"] == "report_only_no_deletion"
    assert len(report["generated_from_head"]) == 40
    assert report["generated_from_head"].isalnum()
    assert report["check_semantics"] == tool.CHECK_SEMANTICS
    assert {item["capability_id"] for item in report["items"]} == {
        gate["capability_id"] for gate in tool.GATES
    }

    for item in report["items"]:
        assert set(item["checks"]) == set(tool.CHECK_ORDER)
        failed = sorted(name for name in tool.CHECK_ORDER if not item["checks"][name])
        assert item["blockers"] == failed, item["gate_id"]
        assert item["ready_for_delete"] == (not failed), item["gate_id"]
        assert set(item["evidence"]) == set(tool.CHECK_ORDER)
        for name in tool.CHECK_ORDER:
            evidence = item["evidence"][name]
            assert evidence["semantics"] == tool.CHECK_SEMANTICS[name]
            assert evidence["passed"] == item["checks"][name], (item["gate_id"], name)
            assert isinstance(evidence.get("items"), list)
        writer = item["evidence"]["project_state_writer"]
        assert writer["ownership_model"] == "capability_owned_persistent_writer"
        if item["capability_id"] != "legacy facade/UI":
            assert writer["catalog_writer_policy"]["persistent_write"] is False
            assert writer["catalog_writer_policy"]["production_authority"] is False
        assert isinstance(writer["non_blocking_writer_evidence"], list)
        assert writer["detected_writer_total"] == (
            len(writer["items"]) + len(writer["non_blocking_writer_evidence"])
        )

    # production 侧的只读校验器必须接受这份报告。
    from cns_planner.compatibility.delete_gates import validate_delete_gate_report

    assert validate_delete_gate_report(report) == report


def test_report_matches_a_fresh_scan(tool):
    """报告中的 checks 必须与当前工作区的重新扫描一致。"""

    report = _report()
    context = {
        "production_sources": tool._production_python_sources(),
        "api_sources": list(tool._glob_sources(tool.ROUTER_GLOB)),
        "frontend_sources": list(tool._glob_sources(tool.FRONTEND_GLOB)),
        "test_sources": list(tool._glob_sources(tool.TEST_GLOB)),
        "default_selection": tool.default_selection_audit(),
        "fixture_results": tool.run_fixture_roundtrip(),
        "fixture_coverage": tool._compatibility_test_coverage(),
    }
    for item in report["items"]:
        gate = _gate(tool, item["capability_id"])
        rescanned = tool.evaluate_gate(gate, context)
        assert rescanned["checks"] == item["checks"], item["gate_id"]
        assert (
            rescanned["evidence"]["project_state_writer"]
            == item["evidence"]["project_state_writer"]
        ), item["gate_id"]


# --------------------------------------------------------------- 10. P14 受保护文件


def test_protected_files_are_unchanged(tool):
    report = _report()
    assert report["protected_files_unchanged"] is True
    assert set(report["protected_files"]) == set(tool.PROTECTED_FILES)
    for name in tool.PROTECTED_FILES:
        digest = sha256((ROOT / name).read_bytes()).hexdigest().upper()
        assert report["protected_files"][name] == digest, name
