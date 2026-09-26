"""Phase4-B7R delete-gate 报告生成器（只报告，绝不删除任何代码）。

用法（仓库根目录）：

    D:\\tools\\anaconda\\python.exe tools\\b7x_delete_gates.py

输出 ``docs/phase4_b7_delete_gates.json``，逐项给出：

    production_imports / production_api_actions / frontend_production_actions /
    project_state_writer / default_selection / service_fallback /
    old_fixture_read / tests_migrated

B7R 相对 B7X 的判定方式修正（本轮唯一的改动点）：

* ``production_imports``：改用 Python ``ast`` 的 ``Import`` / ``ImportFrom``，
  只统计 production 代码对目标 legacy **模块** 或 **符号** 的真实 import；
  注释、docstring、错误消息、普通字符串一律不算。
* ``project_state_writer``：改用 ``ast`` 定位真实 mutation
  （``state["k"] = ...`` / ``del state["k"]`` / ``state["k"].update(...)`` /
  ``state.setdefault("k", ...)`` / ``state.pop("k")``），再按 capability ownership
  分类。只有目标 legacy capability 自己拥有的 persistent writer 才阻止删除；
  canonical writer、共享 writer 与 runtime-only compatibility cache 都保留为
  非阻塞审计证据。纯读取（``state.get("k")`` / ``state["k"]``）不算 writer。
* ``default_selection``：直接求值 :func:`cns_planner.algorithms.registry.default_algorithm_selection`，
  用 algorithm_id **精确相等** 判断，绝不在 ``project_state.py`` 全文做 substring 搜索，
  因此 ``route_planner_v3_experiments`` 之类的 result key 不会再误伤 RoutePlannerV3，
  生产算法 ``corridor_reuse_first_site_planner_v2`` 也不会被误判成 ReuseFirstSitePlannerV1。
* ``old_fixture_read``：语义变为"该 capability 有对应旧项目 fixture 且可
  open / read / export / roundtrip"。判定方式是**真实执行** fixture roundtrip
  （复用 WorkflowService 的 open → read → export → roundtrip 路径），而不是
  "字符串没出现"。
* ``tests_migrated``：用 ``ast`` 区分"仍然实例化/执行 legacy 算法"与
  "仅断言 legacy 不可写 / compatibility guard / 只读 manifest / 字符串提及"。
  只有前者才是 migration blocker。
* ``production_api_actions``：字段名保留（schema 兼容），但语义与证据改写为
  **runtime compute API**：只表示 api 层仍注册了可触发该 capability 运行的路由，
  绝不表示该 capability 拥有 production authority。
* ``frontend_production_actions``：字段名保留，语义为 **Advanced frontend action**：
  只统计 ``api()/resourceAction()/computeAction()/resourceMutationAndRefresh()``
  的 URL 实参，说明文字里的端点字符串不算。

B7R 只报告：即使 ``ready_for_delete`` 为真也不会删除任何模块，物理删除留给 B8。
``ready_for_delete`` 为真 ⇔ 全部 8 项通过；``blockers`` 就是未通过项的排序列表。
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import sys
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # 允许直接以脚本方式运行
    sys.path.insert(0, str(ROOT))

REPORT_PATH = ROOT / "docs" / "phase4_b7_delete_gates.json"
GENERATOR_SCHEMA_VERSION = "phase4-b7r-delete-gates-v4"
DEFAULTS_PATH = ROOT / "cns_planner" / "config" / "defaults.json"
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "compatibility" / "legacy_archive_cases.json"
COMPAT_TEST_PATH = ROOT / "tests" / "test_phase4_b7x_compatibility.py"
ROUTER_GLOB = "cns_planner/api/**/*.py"
FRONTEND_GLOB = "cns_planner/web/**/*.js"
TEST_GLOB = "tests/**/*.py"

CHECK_ORDER = (
    "production_imports",
    "production_api_actions",
    "frontend_production_actions",
    "project_state_writer",
    "default_selection",
    "service_fallback",
    "old_fixture_read",
    "tests_migrated",
)

CHECK_SEMANTICS = {
    "production_imports": (
        "production Python 代码是否仍通过真实 import 语句依赖目标 legacy 模块/符号"
        "（ast Import/ImportFrom；注释、docstring、错误消息与普通字符串不算）。"
    ),
    "production_api_actions": (
        "runtime compute API：api 层是否仍注册了可触发该 capability 运行的 HTTP 路由。"
        "该字段只表示 runtime 可触发，**不表示** production authority，也不表示"
        " compatibility endpoint 是正式权威通道。"
    ),
    "frontend_production_actions": (
        "Advanced frontend action：前端是否仍有触发该 capability 运行的动作"
        "（只统计 api()/resourceAction()/computeAction()/resourceMutationAndRefresh() 的 URL 实参；"
        "HTML 说明文字中的端点字符串不算）。"
    ),
    "project_state_writer": (
        "legacy persistent writer ownership：true = 目标 capability 已无自己拥有的 persistent "
        "ProjectState writer。AST 先识别真实 mutation（赋值 / del / update / setdefault / pop / "
        "clear / append / extend），再分类为 target_legacy_writer / canonical_writer / "
        "shared_neutral_writer / compatibility_runtime_only；只有 target_legacy_writer 阻止删除。"
        "纯读取（state.get(\"k\") 或 state[\"k\"] 取值）不算。"
    ),
    "default_selection": (
        "新项目默认 selection：capability 的 algorithm_id 是否精确出现在"
        " default_algorithm_selection() 的 algorithm_id 集合中（精确相等，不做 substring）。"
    ),
    "service_fallback": (
        "Application 服务层是否仍真实引用该 capability 的算法符号"
        "（ast Name/Attribute 引用；import 行与字符串不算）。"
    ),
    "old_fixture_read": (
        "旧项目 fixture 可用性：该 capability 是否有对应 legacy_archive_cases fixture，"
        "且 open / read / export / roundtrip 实测通过。true = 可用（即不是 blocker）。"
    ),
    "tests_migrated": (
        "测试迁移：production tests 是否仍真实实例化/执行该 capability 的算法"
        "（直接实例化或 registry 精确 (type, id, version) 构造）。断言 legacy 不可写、"
        "compatibility guard、只读 manifest 与字符串提及不算。"
    ),
}

#: 每个 gate：capability → 判定用的算法 id / 符号 / 定义模块 / fixture case。
GATES = (
    {
        "capability_id": "RoutePlannerV1",
        "algorithm_ids": ("route_planner_v1",),
        "symbols": ("RoutePlannerV1",),
        "legacy_modules": ("cns_planner.algorithms.route.v1", "cns_planner.algorithms.route"),
        "registry_entries": (("route_planner", "route_planner_v1", "1.0"),),
        "defining_prefixes": (
            "cns_planner/algorithms/route/",
            "cns_planner/algorithms/route_planner.py",
        ),
        "owned_modules": ("cns_planner.algorithms.route",),
        "owned_services": ("RoutePlannerV1",),
        "owned_writer_symbols": (),
        "result_keys": ("operational_routes",),
        "api_prefixes": ("/api/compatibility/route-planner",),
        "frontend_prefixes": ("/api/compatibility/route-planner/evaluate",),
        "fixture_cases": ("A_route_planner_v1",),
    },
    {
        "capability_id": "RiskAwareRoutePlannerV2",
        "algorithm_ids": ("risk_aware_route_planner_v2",),
        "symbols": ("RiskAwareRoutePlannerV2",),
        "legacy_modules": ("cns_planner.route_planner.risk_aware_v2", "cns_planner.route_planner"),
        "registry_entries": (("route_planner", "risk_aware_route_planner_v2", "2.0"),),
        "defining_prefixes": ("cns_planner/route_planner/",),
        "owned_modules": ("cns_planner.route_planner",),
        "owned_services": ("RiskAwareRoutePlannerV2",),
        "owned_writer_symbols": (),
        "result_keys": ("operational_routes",),
        "api_prefixes": ("/api/compatibility/selection",),
        "frontend_prefixes": ("/api/compatibility/selection",),
        "fixture_cases": ("B_risk_aware_route_planner_v2",),
    },
    {
        "capability_id": "LayeredRoutePlannerV1",
        "algorithm_ids": ("layered_route_planner_v1",),
        "symbols": ("LayeredRoutePlannerV1",),
        "legacy_modules": ("cns_planner.layered_route_planner.planner",),
        "registry_entries": (("layered_route_planner", "layered_route_planner_v1", "1.0"),),
        "defining_prefixes": ("cns_planner/layered_route_planner/",),
        "owned_modules": ("cns_planner.layered_route_planner",),
        "owned_services": ("LayeredRoutePlannerV1",),
        "owned_writer_symbols": (),
        "result_keys": ("layered_route_candidates",),
        "api_prefixes": ("/api/compatibility/layered-route-planner",),
        "frontend_prefixes": (),
        "fixture_cases": ("C_layered_route_planner_v1",),
    },
    {
        "capability_id": "RoutePlannerV3",
        "algorithm_ids": ("route_planner_v3_strategic", "route_planner_v3_corridor_refinement"),
        "symbols": ("V3StrategicPlanner", "V3RefinementPlanner", "V3ContinuousValidator"),
        "legacy_modules": ("cns_planner.route_planner_v3",),
        "registry_entries": (),
        "defining_prefixes": ("cns_planner/route_planner_v3/",),
        "owned_modules": (
            "cns_planner.route_planner_v3",
            "cns_planner.application.route_planner_v3_service",
        ),
        "owned_services": ("RoutePlannerV3ExperimentService", "V3OperationalAdoptionService"),
        "owned_writer_symbols": (),
        "result_keys": ("route_planner_v3_experiments",),
        "api_prefixes": ("/api/research/route-planner-v3",),
        "frontend_prefixes": ("/api/research/route-planner-v3",),
        "fixture_cases": ("G_v3_experiment_adoption_history",),
    },
    {
        "capability_id": "CoveragePlannerV1",
        "algorithm_ids": ("coverage_planner_v1",),
        "symbols": ("CoveragePlannerV1",),
        "legacy_modules": ("cns_planner.algorithms.coverage.v1",),
        "registry_entries": (("coverage_planner", "coverage_planner_v1", "1.0"),),
        "defining_prefixes": (
            "cns_planner/algorithms/coverage/v1.py",
            "cns_planner/algorithms/coverage/__init__.py",
            "cns_planner/algorithms/coverage_planner.py",
        ),
        "owned_modules": (
            "cns_planner.algorithms.coverage.v1",
            "cns_planner.algorithms.coverage_planner",
        ),
        "owned_services": ("CoveragePlannerV1",),
        "owned_writer_symbols": (),
        "result_keys": ("coverage",),
        "api_prefixes": ("/api/compatibility/coverage",),
        "frontend_prefixes": ("/api/compatibility/coverage/evaluate",),
        "fixture_cases": ("D_inline_legacy_coverage",),
    },
    {
        "capability_id": "CNSGapAnalyzerV1",
        "algorithm_ids": ("cns_gap_analysis_v1",),
        "symbols": ("CNSGapAnalyzerV1",),
        "legacy_modules": ("cns_planner.gap.v1",),
        "registry_entries": (("cns_gap_analyzer", "cns_gap_analysis_v1", "1.0"),),
        "defining_prefixes": ("cns_planner/gap/",),
        "owned_modules": ("cns_planner.gap.v1",),
        "owned_services": ("CNSGapAnalyzerV1",),
        "owned_writer_symbols": (),
        "result_keys": ("cns_gap_analysis",),
        "api_prefixes": ("/api/compatibility/cns-gap-analysis-v1",),
        "frontend_prefixes": ("/api/compatibility/cns-gap-analysis-v1/evaluate",),
        "fixture_cases": ("E_cns_gap_analyzer_v1",),
    },
    {
        "capability_id": "ReuseFirstSitePlannerV1",
        "algorithm_ids": ("reuse_first_site_planner_v1",),
        "symbols": ("ReuseFirstSitePlannerV1",),
        "legacy_modules": ("cns_planner.site_planner.reuse_first_v1",),
        "registry_entries": (("site_planner", "reuse_first_site_planner_v1", "1.0"),),
        "defining_prefixes": ("cns_planner/site_planner/",),
        "owned_modules": ("cns_planner.site_planner.reuse_first_v1",),
        "owned_services": ("ReuseFirstSitePlannerV1",),
        "owned_writer_symbols": (),
        "result_keys": ("cns_site_plan",),
        "api_prefixes": ("/api/compatibility/site-plan",),
        "frontend_prefixes": ("/api/compatibility/site-plan/evaluate",),
        "fixture_cases": ("F_reuse_first_site_planner_v1",),
    },
    {
        "capability_id": "legacy facade/UI",
        "algorithm_ids": (),
        "symbols": (),
        "legacy_modules": (),
        "registry_entries": (),
        "defining_prefixes": (),
        "owned_modules": ("cns_planner.compatibility",),
        "owned_services": (),
        "owned_writer_symbols": (),
        "result_keys": ("coverage", "cns_gap_analysis", "cns_gap_analysis_v2", "cns_site_plan"),
        "api_prefixes": ("/api/compatibility/", "/api/research/"),
        "frontend_prefixes": ("/api/compatibility/", "/api/research/"),
        "fixture_cases": (
            "A_route_planner_v1",
            "D_inline_legacy_coverage",
            "E_cns_gap_analyzer_v1",
            "F_reuse_first_site_planner_v1",
        ),
    },
)

#: gate policy 允许排除的扫描目标（兼容适配层、注册表、开发工具）。
GLOBAL_EXCLUDES = (
    "cns_planner/compatibility/",
    "cns_planner/algorithms/registry.py",
    "cns_planner/dev/",
)

#: 允许排除的 compatibility 测试资源（它们本身就是 B7 兼容层的一部分）。
COMPAT_TEST_ALLOWLIST = (
    "tests/test_phase4_b7x_compatibility.py",
    "tests/test_phase4_b7r_delete_gates.py",
    "tests/b7x_compatibility_ui.test.mjs",
    "tests/fixtures/compatibility/",
)

#: guard 测试 allowlist：这些 production test 为了断言"legacy 不可写 / 不再拥有
#: production authority"必须实例化 legacy 算法，属于 compatibility guard 而非
#: legacy production characterization，因此不计入 tests_migrated blocker。
GUARD_TEST_ALLOWLIST = {
    "tests/test_production_write_authority.py": (
        "B2B/B7X production write authority guard：显式构造 legacy planner / registry 条目，"
        "只为断言 canonical ProjectState 不被旧算法改写。"
    ),
}

#: 识别为 state mutation 的方法名。
STATE_MUTATOR_METHODS = (
    "update", "setdefault", "pop", "popitem", "clear",
    "append", "extend", "insert", "remove", "sort", "reverse", "add", "discard",
)

#: state 容器在代码里的常见绑定名。
#:
#: 刻意保持保守：只承认明确的 ProjectState 绑定名。``value`` / ``data`` 之类
#: 通用局部变量名被排除，避免把普通 dict 操作误判成 ProjectState 写入。
STATE_CONTAINER_NAMES = ("state", "_state", "project_state")

#: runtime compatibility helpers mutate only the process-local WorkflowSession cache.  They
#: are reported for audit, but can never be a persistent ProjectState writer blocker.
RUNTIME_COMPATIBILITY_MUTATORS = (
    "write_runtime_compatibility_result",
    "drop_runtime_compatibility_result",
    "mark_runtime_compatibility_stale",
)

WRITER_OWNERSHIP_CLASSES = (
    "target_legacy_writer",
    "canonical_writer",
    "shared_neutral_writer",
    "compatibility_runtime_only",
)

#: 前端真实发起请求的 helper（只有它们的 URL 实参才算 frontend action）。
FRONTEND_CALL_HELPERS = (
    "api", "resourceAction", "computeAction", "resourceMutationAndRefresh", "resourceRead",
)

_FRONTEND_CALL_RE = re.compile(
    r"\b(" + "|".join(FRONTEND_CALL_HELPERS) + r")\s*\(\s*(['\"])([^'\"\n]+)\2"
)

#: 受保护文件：本轮（以及后续轮次）必须保证工作区 SHA256 前后一致。
PROTECTED_FILES = (
    "cns_planner/algorithms/corridor/v1.py",
    "cns_planner/algorithms/coverage/geometric_3d.py",
    "cns_planner/algorithms/service_capability/v1.py",
    "tests/test_cns_corridor.py",
)


# --------------------------------------------------------------------------- 基础设施


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def _glob_sources(pattern):
    for path in sorted(ROOT.glob(pattern)):
        if path.is_file() and "__pycache__" not in path.parts:
            yield path.relative_to(ROOT).as_posix(), path.read_text(encoding="utf-8")


def _excluded(name, prefixes):
    return any(name.startswith(prefix) for prefix in prefixes)


def _production_python_sources():
    return [
        (name, text)
        for name, text in _glob_sources("cns_planner/**/*.py")
        if not _excluded(name, GLOBAL_EXCLUDES)
    ]


def _parse(text):
    return ast.parse(text)


def _module_matches(module, prefixes):
    return any(module == prefix or module.startswith(prefix + ".") for prefix in prefixes)


def _resolve_import_from(rel, level, module):
    """把相对 ``from ... import ...`` 解析成绝对模块名。"""

    parts = rel.split("/")[:-1]  # 文件所在包（对 __init__.py 同样成立）
    if level:
        base = parts[: len(parts) - (level - 1)] if level > 1 else list(parts)
    else:
        base = []
    if module:
        base = base + module.split(".")
    return ".".join(base)


def _is_state_container(node):
    if isinstance(node, ast.Name):
        return node.id in STATE_CONTAINER_NAMES
    if isinstance(node, ast.Attribute):
        return node.attr in STATE_CONTAINER_NAMES
    return False


def _subscript_key(node):
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        value = node.slice.value
        if isinstance(value, str):
            return value
    return None


def _module_name(rel):
    value = rel[:-3] if rel.endswith(".py") else rel
    if value.endswith("/__init__"):
        value = value[:-9]
    return value.replace("/", ".")


def _module_owned(rel, owned_modules):
    module = _module_name(rel)
    for configured in owned_modules:
        prefix = str(configured).replace("/", ".").removesuffix(".py").rstrip(".")
        if module == prefix or module.startswith(prefix + "."):
            return True
    return False


def _authority_owners(result_key):
    """Return registered canonical content/revoke owners for one result key."""

    from cns_planner.application.production_write_authority import (
        DERIVED_REVOKE_ALLOWLIST,
        WRITE_ALLOWLIST,
    )

    return set(WRITE_ALLOWLIST.get(result_key, ())) | set(
        DERIVED_REVOKE_ALLOWLIST.get(result_key, ())
    )


def _catalog_writer_policy(capability_id):
    """Read the compatibility catalog's declared authority/persistence policy."""

    if capability_id == "legacy facade/UI":
        return {}
    from cns_planner.compatibility.catalog import capability_metadata

    try:
        metadata = capability_metadata(capability_id)
    except KeyError:
        return {}
    return {
        "lifecycle": metadata["lifecycle"],
        "persistent_write": metadata["persistent_write"],
        "production_authority": metadata["production_authority"],
        "replacement": metadata["replacement"],
    }


def _writer_ownership(rel, gate, *, owner, symbol, runtime_only=False):
    if runtime_only:
        return "compatibility_runtime_only"
    if owner in _authority_owners(gate.get("_current_result_key", "")):
        return "canonical_writer"
    if (
        owner in set(gate.get("owned_services", ()))
        or symbol in set(gate.get("owned_writer_symbols", ()))
        or _module_owned(rel, gate.get("owned_modules", ()))
    ):
        return "target_legacy_writer"
    # Synthetic/unit scanner calls that predate ownership configuration still model their
    # only writer as the target.  Real gates always declare all three ownership fields.
    if not any(name in gate for name in ("owned_modules", "owned_services", "owned_writer_symbols")):
        return "target_legacy_writer"
    return "shared_neutral_writer"


def _module_string_constants(tree):
    constants = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        constants[target.id] = node.value.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                constants[node.target.id] = node.value.value
    return constants


def _static_string(node, constants):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    return None


# --------------------------------------------------------------------------- 各项扫描


def scan_production_imports(sources, gate):
    """真实 import 命中的 legacy 模块/符号（AST）。"""

    items = []
    for rel, text in sources:
        if _excluded(rel, gate["defining_prefixes"]):
            continue
        try:
            tree = _parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _module_matches(alias.name, gate["legacy_modules"]):
                        items.append({
                            "file": rel, "line": node.lineno, "kind": "import",
                            "module": alias.name, "name": alias.asname or alias.name,
                        })
            elif isinstance(node, ast.ImportFrom):
                module = _resolve_import_from(rel, node.level, node.module)
                hit_module = bool(module) and _module_matches(module, gate["legacy_modules"])
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    if hit_module:
                        items.append({
                            "file": rel, "line": node.lineno, "kind": "from_import",
                            "module": module, "name": alias.name,
                        })
                    elif alias.name in gate["symbols"]:
                        items.append({
                            "file": rel, "line": node.lineno, "kind": "legacy_symbol_import",
                            "module": module or "<relative>", "name": alias.name,
                        })
    return items


def scan_state_writers(sources, gate):
    """Locate and ownership-classify writes associated with the gate's result keys.

    Persistent ProjectState mutations and runtime-only compatibility helper calls are both
    visible in the audit stream.  ``evaluate_gate`` treats only ``target_legacy_writer`` as a
    blocker; the other three classes are retained as non-blocking evidence.
    """

    keys = set(gate["result_keys"])
    if not keys:
        return []
    items = []

    for rel, text in sources:
        try:
            tree = _parse(text)
        except SyntaxError:
            continue
        constants = _module_string_constants(tree)

        class WriterVisitor(ast.NodeVisitor):
            def __init__(self):
                self.classes = []
                self.functions = []

            @property
            def owner(self):
                if self.classes:
                    return self.classes[-1]
                if self.functions:
                    return self.functions[-1]
                return _module_name(rel)

            @property
            def symbol(self):
                parts = []
                if self.classes:
                    parts.append(self.classes[-1])
                if self.functions:
                    parts.append(self.functions[-1])
                return ".".join(parts) or _module_name(rel)

            def record(self, node, kind, key, detail, *, runtime_only=False):
                configured = dict(gate, _current_result_key=key)
                ownership_class = _writer_ownership(
                    rel, configured, owner=self.owner, symbol=self.symbol,
                    runtime_only=runtime_only,
                )
                items.append({
                    "file": rel,
                    "line": node.lineno,
                    "kind": kind,
                    "result_key": key,
                    "owner": self.owner,
                    "ownership_class": ownership_class,
                    "detail": detail,
                })

            def visit_ClassDef(self, node):  # noqa: N802 - ast visitor protocol
                self.classes.append(node.name)
                self.generic_visit(node)
                self.classes.pop()

            def visit_FunctionDef(self, node):  # noqa: N802 - ast visitor protocol
                self.functions.append(node.name)
                self.generic_visit(node)
                self.functions.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Assign(self, node):  # noqa: N802 - ast visitor protocol
                for target in node.targets:
                    self._assignment(node, target)
                self.generic_visit(node)

            def visit_AnnAssign(self, node):  # noqa: N802 - ast visitor protocol
                self._assignment(node, node.target)
                self.generic_visit(node)

            def visit_AugAssign(self, node):  # noqa: N802 - ast visitor protocol
                self._assignment(node, node.target)
                self.generic_visit(node)

            def _assignment(self, node, target):
                key = _subscript_key(target)
                if key in keys and _is_state_container(target.value):
                    self.record(node, "state_key_assignment", key, ast.unparse(target))

            def visit_Delete(self, node):  # noqa: N802 - ast visitor protocol
                for target in node.targets:
                    key = _subscript_key(target)
                    if key in keys and _is_state_container(target.value):
                        self.record(node, "state_key_delete", key, ast.unparse(target))
                self.generic_visit(node)

            def visit_Call(self, node):  # noqa: N802 - ast visitor protocol
                called = _called_name(node.func)
                if called in RUNTIME_COMPATIBILITY_MUTATORS:
                    key_node = node.args[1] if len(node.args) >= 2 else next(
                        (item.value for item in node.keywords if item.arg in ("name", "result_key")),
                        None,
                    )
                    key = _static_string(key_node, constants)
                    if key in keys:
                        self.record(
                            node, "runtime_compatibility_cache_mutation", key,
                            f"{called}(..., {key!r}, ...)", runtime_only=True,
                        )

                if isinstance(node.func, ast.Attribute):
                    method = node.func.attr
                    inner = node.func.value
                    key = _subscript_key(inner)
                    if key in keys and _is_state_container(inner.value):
                        if method in STATE_MUTATOR_METHODS:
                            self.record(
                                node, "state_key_method_mutation", key,
                                f"{ast.unparse(inner)}.{method}(...)",
                            )
                    elif method in ("setdefault", "pop") and _is_state_container(inner):
                        if node.args:
                            key = _static_string(node.args[0], constants)
                            if key in keys:
                                self.record(
                                    node, "state_container_method_mutation", key,
                                    f"{ast.unparse(inner)}.{method}({key!r}, ...)",
                                )
                    elif method == "update" and _is_state_container(inner):
                        updated_keys = []
                        if node.args and isinstance(node.args[0], ast.Dict):
                            updated_keys.extend(
                                _static_string(item, constants) for item in node.args[0].keys
                            )
                        updated_keys.extend(item.arg for item in node.keywords if item.arg)
                        for key in sorted(set(updated_keys) & keys):
                            self.record(
                                node, "state_container_method_mutation", key,
                                f"{ast.unparse(inner)}.update(... {key!r} ...)",
                            )
                self.generic_visit(node)

        WriterVisitor().visit(tree)
    return sorted(items, key=lambda item: (item["file"], item["line"], item["kind"]))


def default_selection_audit():
    """求值真实 default_algorithm_selection()，返回 (algorithm_type, algorithm_id) 快照。"""

    from cns_planner.algorithms.registry import default_algorithm_selection

    entries = []
    for algorithm_type, entry in default_algorithm_selection().items():
        entries.append({
            "algorithm_type": algorithm_type,
            "algorithm_id": str((entry or {}).get("algorithm_id") or ""),
        })
    return sorted(entries, key=lambda item: item["algorithm_type"])


def scan_default_selection(entries, gate):
    """精确相等判定：capability algorithm_id 是否出现在新项目默认 selection。"""

    if not gate["algorithm_ids"]:
        return []
    wanted = set(gate["algorithm_ids"])
    return [
        {"algorithm_type": entry["algorithm_type"], "algorithm_id": entry["algorithm_id"], "kind": "default_selection"}
        for entry in entries
        if entry["algorithm_id"] in wanted
    ]


def scan_service_fallback(sources, gate):
    """Application 服务层对 legacy 算法符号的真实运行时引用（AST）。"""

    if not gate["symbols"]:
        return []
    symbols = set(gate["symbols"])
    items = []
    for rel, text in sources:
        if "/application/" not in rel:
            continue
        if _excluded(rel, ("cns_planner/compatibility/",)):
            continue
        try:
            tree = _parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in symbols:
                items.append({
                    "file": rel, "line": node.lineno,
                    "kind": "service_symbol_reference", "symbol": node.id,
                })
            elif isinstance(node, ast.Attribute) and node.attr in symbols:
                items.append({
                    "file": rel, "line": node.lineno,
                    "kind": "service_symbol_reference", "symbol": node.attr,
                })
    return items


def scan_runtime_api(sources, gate):
    """api 层真实注册的路由字符串常量（AST）。"""

    if not gate["api_prefixes"]:
        return []
    items = []
    for rel, text in sources:
        try:
            tree = _parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                endpoint = node.value
                if not endpoint.startswith("/api/"):
                    continue
                if endpoint.startswith(gate["api_prefixes"]):
                    items.append({
                        "file": rel, "line": node.lineno,
                        "kind": "runtime_api_route", "endpoint": endpoint,
                    })
    return items


def scan_frontend_actions(sources, gate):
    """前端真实请求 helper 的 URL 实参（正则提取，非注释、非说明文字）。"""

    if not gate["frontend_prefixes"]:
        return []
    items = []
    for rel, text in sources:
        for match in _FRONTEND_CALL_RE.finditer(text):
            endpoint = match.group(3)
            if endpoint.startswith(gate["frontend_prefixes"]):
                items.append({
                    "file": rel, "line": text.count("\n", 0, match.start()) + 1,
                    "kind": "frontend_action", "helper": match.group(1), "endpoint": endpoint,
                })
    return items


def _called_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def scan_test_execution(sources, gate):
    """production test 对 legacy 算法的真实实例化/执行（AST）。"""

    symbols = set(gate["symbols"])
    registry_entries = set(gate["registry_entries"])
    if not symbols and not registry_entries:
        return []
    items = []
    for rel, text in sources:
        if _excluded(rel, COMPAT_TEST_ALLOWLIST):
            continue
        if rel in GUARD_TEST_ALLOWLIST:
            continue
        try:
            tree = _parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _called_name(node.func)
            if name in symbols:
                items.append({
                    "file": rel, "line": node.lineno,
                    "kind": "legacy_algorithm_instantiation", "symbol": name,
                })
                continue
            if name in ("create", "instantiate"):
                positional = [
                    arg.value for arg in node.args
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                ]
                for entry in registry_entries:
                    if tuple(positional[:3]) == entry:
                        items.append({
                            "file": rel, "line": node.lineno,
                            "kind": "legacy_algorithm_registry_construction",
                            "registry_entry": list(entry),
                        })
                        break
    return items


# --------------------------------------------------------------------------- fixture roundtrip


def _fixture_cases():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _case_covers(case, gate):
    for key, value in case.items():
        if key == "algorithm_selection" and isinstance(value, dict):
            for selection in value.values():
                if isinstance(selection, dict) and selection.get("algorithm_id") in gate["algorithm_ids"]:
                    return True
        elif key in gate["result_keys"]:
            return True
    return False


def run_fixture_roundtrip():
    """真实执行旧项目 fixture 的 open / read / export / roundtrip。

    返回 ``{case_name: {"present": bool, "opened": bool, "exported": bool,
    "roundtrip": bool, "detail": str}}``。任何异常都会记录为对应 case 的失败，
    绝不静默通过。
    """

    from cns_planner.application.workflow_service import WorkflowService

    cases = _fixture_cases()
    results = {}
    # 刻意使用仓库内固定临时目录：本机系统临时区的 mkdtemp 目录在沙箱 ACL 下
    # 既不能在其中建子目录也不能删除，而 roundtrip 必须落在真实可写的项目目录里。
    tmp = ROOT / "_b7r_fixture_tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        for name, overlay in cases.items():
            entry = {
                "present": True, "opened": False, "exported": False,
                "roundtrip": False, "detail": "",
            }
            try:
                target = tmp / name / "project.json"
                target.parent.mkdir(parents=True, exist_ok=True)
                workflow = WorkflowService(target, DEFAULTS_PATH)
                for key, value in overlay.items():
                    if key == "algorithm_selection":
                        workflow.state[key].update(deepcopy(value))
                    else:
                        workflow.state[key] = deepcopy(value)
                workflow.save()

                # open
                reopened = WorkflowService(target, DEFAULTS_PATH)
                entry["opened"] = True
                # export
                exported = json.loads(reopened.export_project())
                entry["exported"] = bool(exported)
                # roundtrip：save 后再打开，旧 selection 与旧结果原样保留
                reopened.save()
                again = WorkflowService(target, DEFAULTS_PATH)
                ok = True
                for key, expected in overlay.items():
                    if key == "algorithm_selection":
                        for algorithm_type, selection in expected.items():
                            if again.state[key].get(algorithm_type) != selection:
                                ok = False
                    elif key not in again.state:
                        ok = False
                entry["roundtrip"] = ok
                entry["detail"] = "open/read/export/roundtrip ok" if ok else "roundtrip 后旧值未保留"
            except Exception as exc:  # noqa: BLE001 - 报告需要记录任何失败
                entry["detail"] = f"{type(exc).__name__}: {exc}"
            results[name] = entry
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return results


def scan_old_fixture_read(case_results, gate):
    """fixture 可用性：case 存在 + 覆盖该 capability + 真实 roundtrip 通过。"""

    if not gate["fixture_cases"]:
        return []
    cases = _fixture_cases()
    items = []
    for name in gate["fixture_cases"]:
        case = cases.get(name)
        result = case_results.get(name) or {}
        covered = bool(case) and _case_covers(case, gate)
        roundtrip = bool(result.get("opened")) and bool(result.get("exported")) and bool(result.get("roundtrip"))
        items.append({
            "kind": "compatibility_fixture_roundtrip",
            "fixture_case": name,
            "fixture_present": bool(case),
            "covers_capability": covered,
            "opened": bool(result.get("opened")),
            "exported": bool(result.get("exported")),
            "roundtrip": bool(result.get("roundtrip")),
            "passed": covered and roundtrip,
            "detail": result.get("detail", ""),
        })
    return items


# --------------------------------------------------------------------------- 汇总


def evaluate_gate(gate, context):
    sources = context["production_sources"]
    api_sources = context["api_sources"]
    frontend_sources = context["frontend_sources"]
    test_sources = context["test_sources"]
    default_entries = context["default_selection"]
    fixture_results = context["fixture_results"]
    fixture_covered = context["fixture_coverage"]

    writer_items = scan_state_writers(sources, gate)
    target_writer_items = [
        item for item in writer_items
        if item["ownership_class"] == "target_legacy_writer"
    ]
    non_blocking_writer_items = [
        item for item in writer_items
        if item["ownership_class"] != "target_legacy_writer"
    ]
    scans = {
        "production_imports": scan_production_imports(sources, gate),
        "production_api_actions": scan_runtime_api(api_sources, gate),
        "frontend_production_actions": scan_frontend_actions(frontend_sources, gate),
        "project_state_writer": target_writer_items,
        "default_selection": scan_default_selection(default_entries, gate),
        "service_fallback": scan_service_fallback(sources, gate),
        "tests_migrated": scan_test_execution(test_sources, gate),
    }

    checks = {}
    evidence = {}
    for name in CHECK_ORDER:
        if name == "old_fixture_read":
            items = scan_old_fixture_read(fixture_results, gate)
            expected = len(gate["fixture_cases"])
            fixture_probe_ok = bool(items) and all(item["passed"] for item in items) and expected > 0
            witness = fixture_covered.get(gate["capability_id"], {})
            checks[name] = fixture_probe_ok and bool(witness.get("compatibility_test_asserts_roundtrip"))
            evidence[name] = {
                "semantics": CHECK_SEMANTICS[name],
                "passed": checks[name],
                "fixture_cases": list(gate["fixture_cases"]),
                "compatibility_test": witness.get("test", ""),
                "compatibility_test_asserts_roundtrip": bool(
                    witness.get("compatibility_test_asserts_roundtrip")
                ),
                "items": items,
            }
            continue
        items = scans[name]
        checks[name] = not items
        evidence[name] = {
            "semantics": CHECK_SEMANTICS[name],
            "passed": checks[name],
            "total": len(items),
            "items": items,
        }
        if name == "project_state_writer":
            evidence[name].update({
                "ownership_model": "capability_owned_persistent_writer",
                "catalog_writer_policy": _catalog_writer_policy(gate["capability_id"]),
                "owned_modules": list(gate.get("owned_modules", ())),
                "owned_services": list(gate.get("owned_services", ())),
                "owned_writer_symbols": list(gate.get("owned_writer_symbols", ())),
                "detected_writer_total": len(writer_items),
                "non_blocking_writer_total": len(non_blocking_writer_items),
                "non_blocking_writer_evidence": non_blocking_writer_items,
            })

    blockers = sorted(name for name in CHECK_ORDER if not checks[name])
    return {
        "gate_id": "B7-DELETE-" + re.sub(r"[^A-Za-z0-9]+", "-", gate["capability_id"]).strip("-").upper(),
        "capability_id": gate["capability_id"],
        "checks": {name: checks[name] for name in CHECK_ORDER},
        "ready_for_delete": not blockers,
        "blockers": blockers,
        "evidence": {name: evidence[name] for name in CHECK_ORDER},
    }


def _compatibility_test_coverage():
    """静态确认 B7 compatibility test 真的在做 open/read/export/roundtrip 验证。"""

    path = COMPAT_TEST_PATH
    result = {}
    if not path.exists():
        return {gate["capability_id"]: {"test": "", "compatibility_test_asserts_roundtrip": False} for gate in GATES}
    text = path.read_text(encoding="utf-8")
    tree = _parse(text)
    target = "test_all_legacy_archive_fixtures_open_read_export_and_roundtrip"
    found = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == target:
            body = ast.unparse(node) if hasattr(ast, "unparse") else ""
            if "export_project" in body and "save()" in body:
                found = True
    for gate in GATES:
        result[gate["capability_id"]] = {"test": target, "compatibility_test_asserts_roundtrip": found}
    return result


def _protected_hashes():
    return {name: sha256((ROOT / name).read_bytes()).hexdigest().upper() for name in PROTECTED_FILES}


def _head_revision():
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.strip()


def build_report():
    before = _protected_hashes()
    context = {
        "production_sources": _production_python_sources(),
        "api_sources": list(_glob_sources(ROUTER_GLOB)),
        "frontend_sources": list(_glob_sources(FRONTEND_GLOB)),
        "test_sources": list(_glob_sources(TEST_GLOB)),
        "default_selection": default_selection_audit(),
        "fixture_results": run_fixture_roundtrip(),
        "fixture_coverage": _compatibility_test_coverage(),
    }
    items = [evaluate_gate(gate, context) for gate in GATES]
    after = _protected_hashes()
    if before != after:
        raise RuntimeError(f"受保护文件在报告生成期间发生变化：{before} -> {after}")
    return {
        "generator_schema_version": GENERATOR_SCHEMA_VERSION,
        "schema_version": GENERATOR_SCHEMA_VERSION,
        "generated_from_head": _head_revision(),
        "mode": "report_only_no_deletion",
        "note": (
            "B7R 只报告：即使 ready_for_delete=true 也不删除任何模块，物理删除留给 B8。"
            "每项 checks 都是对当前工作区的真实扫描结果；project_state_writer 只由目标 capability "
            "自己拥有的 persistent ProjectState writer 阻塞，canonical/shared/runtime-only 写点保留为非阻塞证据；"
            "default_selection 用 default_algorithm_selection() 精确比较，"
            "old_fixture_read 由真实 open/read/export/roundtrip 实测得出。"
        ),
        "check_semantics": CHECK_SEMANTICS,
        "scanner": {
            "global_excludes": list(GLOBAL_EXCLUDES),
            "compat_test_allowlist": list(COMPAT_TEST_ALLOWLIST),
            "guard_test_allowlist": {k: v for k, v in GUARD_TEST_ALLOWLIST.items()},
            "state_mutator_methods": list(STATE_MUTATOR_METHODS),
            "runtime_compatibility_mutators": list(RUNTIME_COMPATIBILITY_MUTATORS),
            "writer_ownership_classes": list(WRITER_OWNERSHIP_CLASSES),
            "frontend_call_helpers": list(FRONTEND_CALL_HELPERS),
        },
        "protected_files": before,
        "protected_files_unchanged": before == after,
        "default_algorithm_selection_snapshot": context["default_selection"],
        "items": items,
    }


def main():
    report = build_report()
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(f"wrote {REPORT_PATH.relative_to(ROOT)} ({len(report['items'])} gates)")
    print(f"generated_from_head={report['generated_from_head']}")
    print(f"protected_files_unchanged={report['protected_files_unchanged']}")
    for item in report["items"]:
        print(f"  {item['capability_id']}: ready={item['ready_for_delete']} blockers={item['blockers']}")


if __name__ == "__main__":
    main()
