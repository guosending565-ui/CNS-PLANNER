"""Phase4-B7X delete-gate 报告生成器（只报告，绝不删除任何代码）。

用法（仓库根目录）：

    D:\\tools\\anaconda\\python.exe tools\\b7x_delete_gates.py

输出 ``docs/phase4_b7_delete_gates.json``，逐项给出：

    production_imports / production_api_actions / frontend_production_actions /
    project_state_writer / default_selection / service_fallback /
    old_fixture_read / tests_migrated

每项都是对当前工作区的真实扫描结果（不是人工填写的占位值）：

* ``ready_for_delete`` 为真 ⇔ 全部 8 项通过；
* ``blockers`` 就是未通过项的排序列表；
* B7 只报告：即使 ready 为真也不会删除任何模块，物理删除留给 B8。

扫描规则刻意保守：只要还有任何一处生产代码、前端生产动作、ProjectState 写点、
默认 selection、service fallback、旧 fixture 读取或"生产语义测试"依赖该能力，
该项即为 false。
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "docs" / "phase4_b7_delete_gates.json"

#: 每个 gate：capability → 判定用的算法 id / 代码符号 / 相关 result key。
GATES = (
    {
        "capability_id": "RoutePlannerV1",
        "algorithm_ids": ("route_planner_v1",),
        "symbols": ("RoutePlannerV1",),
        "result_keys": ("operational_routes",),
        "selection_types": ("route_planner",),
        "frontend_tokens": ("/api/compatibility/route-planner/evaluate",),
        "compat_endpoints": ("/api/compatibility/route-planner",),
    },
    {
        "capability_id": "RiskAwareRoutePlannerV2",
        "algorithm_ids": ("risk_aware_route_planner_v2",),
        "symbols": ("RiskAwareRoutePlannerV2",),
        "result_keys": ("operational_routes",),
        "selection_types": ("route_planner",),
        "frontend_tokens": ("/api/compatibility/selection",),
        "compat_endpoints": ("/api/compatibility/route-planner",),
    },
    {
        "capability_id": "LayeredRoutePlannerV1",
        "algorithm_ids": ("layered_route_planner_v1",),
        "symbols": ("LayeredRoutePlannerV1",),
        "result_keys": ("layered_route_candidate",),
        "selection_types": ("layered_route_planner",),
        "frontend_tokens": (),
        "compat_endpoints": (),
    },
    {
        "capability_id": "RoutePlannerV3",
        "algorithm_ids": ("route_planner_v3",),
        "symbols": ("RoutePlannerV3",),
        "result_keys": ("route_planner_v3_experiments",),
        "selection_types": (),
        "frontend_tokens": ("/api/research/route-planner-v3",),
        "compat_endpoints": ("/api/research/route-planner-v3",),
    },
    {
        "capability_id": "CoveragePlannerV1",
        "algorithm_ids": ("coverage_planner_v1",),
        "symbols": ("CoveragePlannerV1",),
        "result_keys": ("coverage",),
        "selection_types": ("coverage_planner",),
        "frontend_tokens": ("/api/compatibility/coverage/evaluate",),
        "compat_endpoints": ("/api/compatibility/coverage",),
    },
    {
        "capability_id": "CNSGapAnalyzerV1",
        "algorithm_ids": ("cns_gap_analysis_v1",),
        "symbols": ("CNSGapAnalyzerV1",),
        "result_keys": ("cns_gap_analysis",),
        "selection_types": ("cns_gap_analyzer",),
        "frontend_tokens": ("/api/compatibility/cns-gap-analysis-v1/evaluate",),
        "compat_endpoints": ("/api/compatibility/cns-gap-analysis-v1",),
    },
    {
        "capability_id": "ReuseFirstSitePlannerV1",
        "algorithm_ids": ("reuse_first_site_planner_v1",),
        "symbols": ("ReuseFirstSitePlannerV1",),
        "result_keys": ("cns_site_plan",),
        "selection_types": ("site_planner",),
        "frontend_tokens": ("/api/compatibility/site-plan/evaluate",),
        "compat_endpoints": ("/api/compatibility/site-plan",),
    },
    {
        "capability_id": "legacy facade/UI",
        "algorithm_ids": (),
        "symbols": (),
        "result_keys": ("coverage", "cns_gap_analysis", "cns_gap_analysis_v2", "cns_site_plan"),
        "selection_types": ("route_planner", "coverage_planner", "cns_gap_analyzer", "site_planner"),
        "frontend_tokens": ("/api/compatibility/", "/api/research/"),
        "compat_endpoints": ("/api/compatibility/",),
    },
)

#: 生产 Python 代码（不含 compatibility 适配层、registry 注册表与算法自身）。
PRODUCTION_EXCLUDES = (
    "cns_planner/compatibility/",
    "cns_planner/algorithms/registry.py",
    "cns_planner/gap/",
    "cns_planner/coverage/v1.py",
    "cns_planner/site_planner/",
    "cns_planner/route_planner/",
    "cns_planner/layered_route_planner/",
    "cns_planner/algorithms/route/v1.py",
    "cns_planner/algorithms/coverage/v1.py",
    "cns_planner/dev/",
)

TEST_DIR = "tests/"
COMPAT_TEST_ALLOWLIST = (
    "tests/test_phase4_b7x_compatibility.py",
    "tests/b7x_compatibility_ui.test.mjs",
    "tests/fixtures/compatibility/",
)


def _iter_python():
    for path in sorted(ROOT.glob("cns_planner/**/*.py")):
        yield path.relative_to(ROOT).as_posix(), path.read_text(encoding="utf-8")


def _iter_tests():
    for path in sorted(ROOT.glob("tests/**/*")):
        if path.is_file() and path.suffix in (".py", ".mjs"):
            yield path.relative_to(ROOT).as_posix(), path.read_text(encoding="utf-8")


def _iter_frontend():
    for path in sorted(ROOT.glob("cns_planner/web/**/*.js")):
        yield path.relative_to(ROOT).as_posix(), path.read_text(encoding="utf-8")


def _hits(sources, needles):
    found = []
    for name, text in sources:
        for needle in needles:
            if needle and needle in text:
                found.append({"file": name, "needle": needle})
                break
    return found


def _is_production(name):
    return not any(name.startswith(prefix) for prefix in PRODUCTION_EXCLUDES)


def _is_production_test(name):
    return name.startswith(TEST_DIR) and not any(
        name.startswith(allowed) for allowed in COMPAT_TEST_ALLOWLIST
    )


def _token_hits(sources, tokens):
    found = []
    for name, text in sources:
        for token in tokens:
            if token and token in text:
                found.append({"file": name, "needle": token})
                break
    return found


def _gate(gate):
    production = [(n, t) for n, t in _iter_python() if _is_production(n)]
    state_writers = [(n, t) for n, t in production if "/application/" in n or "/api/" in n]
    tests = list(_iter_tests())
    compat_tests = [(n, t) for n, t in tests if not _is_production_test(n)]
    production_tests = [(n, t) for n, t in tests if _is_production_test(n)]
    frontend = list(_iter_frontend())

    symbols = gate["symbols"]
    algorithm_ids = gate["algorithm_ids"]
    result_keys = gate["result_keys"]

    imports = _hits(production, symbols)
    api_actions = _hits(
        [(n, t) for n, t in production if "/api/" in n],
        gate["compat_endpoints"] or symbols,
    )
    frontend_actions = _token_hits(frontend, gate["frontend_tokens"])
    writers = _hits(
        state_writers,
        tuple(f'state["{key}"]' for key in result_keys)
        + tuple(f'state.get("{key}")' for key in result_keys),
    )
    defaults = _hits(
        [(n, t) for n, t in production if "registry" in n or "project_state" in n],
        algorithm_ids,
    )
    fallback = _hits(
        [(n, t) for n, t in production if "/application/" in n],
        symbols,
    )
    fixture_read = _hits(compat_tests, algorithm_ids + result_keys)
    migrated = _hits(production_tests, algorithm_ids + symbols)

    checks = {
        "production_imports": not imports,
        "production_api_actions": not api_actions,
        "frontend_production_actions": not frontend_actions,
        "project_state_writer": not writers,
        "default_selection": not defaults,
        "service_fallback": not fallback,
        "old_fixture_read": not fixture_read,
        "tests_migrated": not migrated,
    }
    blockers = sorted(name for name, passed in checks.items() if not passed)
    evidence = {
        "production_imports": imports[:6],
        "production_api_actions": api_actions[:6],
        "frontend_production_actions": frontend_actions[:6],
        "project_state_writer": writers[:6],
        "default_selection": defaults[:6],
        "service_fallback": fallback[:6],
        "old_fixture_read": fixture_read[:6],
        "tests_migrated": migrated[:6],
    }
    return {
        "gate_id": f"B7-DELETE-{gate['capability_id'].upper().replace('/', '-').replace(' ', '-')}",
        "capability_id": gate["capability_id"],
        "checks": checks,
        "ready_for_delete": not blockers,
        "blockers": blockers,
        "evidence": {key: value for key, value in evidence.items() if value},
    }


def main():
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.strip()
    items = [_gate(gate) for gate in GATES]
    report = {
        "schema_version": "phase4-b7x-delete-gates-v2",
        "generated_from_head": head,
        "mode": "report_only_no_deletion",
        "note": (
            "B7 只报告：即使 ready_for_delete=true 也不删除任何模块；物理删除留给 B8。"
            "每项 checks 都是对当前工作区的真实扫描结果。"
        ),
        "items": items,
    }
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(f"wrote {REPORT_PATH.relative_to(ROOT)} ({len(items)} gates)")
    for item in items:
        print(f"  {item['capability_id']}: ready={item['ready_for_delete']} blockers={item['blockers']}")


if __name__ == "__main__":
    main()
