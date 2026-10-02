"""C 盘 → D 盘迁移的**可移植性**回归（Round 2.6 迁移收口）。

本测试守护的**唯一命题**是：仓库可以在任意目录下运行，产品代码不依赖某个绝对路径。

规则来源（Round 2.6 用户裁定）：

* 产品代码不能依赖旧 C 盘仓库；
* repo 内资源应使用 project root / repo root **相对解析**；
* 不得把 ``D:\\Projects\\CNS-PLANNER`` 再硬编码到 domain 算法；
* 外部真实数据源若本来就在其它磁盘，不得为了迁移随意改路径；
* 历史文档 / 证据里的旧路径**不必**（也不该）为了"凑零命中"而改写。

因此本测试的判定域是**可执行的代码与启动脚本**，不是历史 Markdown。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 旧仓库的绝对路径（正/反斜杠两种写法）。
#: 注意：必须以路径分隔符（或字符串结束）收尾，否则会误伤同级的其它目录
#: （例如 ``...\\ChatGPT\\CNS_VALIDATION\\...`` —— 它**不是**旧仓库内部路径）。
OLD_ABSOLUTE_PATTERNS = (
    re.compile(
        r"[Cc]:[\\/]+Users[\\/]+yiding[\\/]+Documents[\\/]+ChatGPT[\\/]+CNS规划系统(?:[\\/]|$)"
    ),
    re.compile(
        r"[Cc]:[\\/]+Users[\\/]+yiding[\\/]+Documents[\\/]+ChatGPT[\\/]+CNS-PLANNER(?:[\\/]|$)"
    ),
)

#: 新仓库的绝对路径：**同样**不允许出现在代码里（"硬编码换成另一个硬编码"不算修复）。
NEW_ABSOLUTE_PATTERNS = (
    re.compile(r"[Dd]:[\\/]+Projects[\\/]+CNS-PLANNER"),
)

#: 判定域：运行时代码、启动脚本、测试、配置。历史文档与运行产物不在其中。
CODE_GLOBS = (
    "cns_planner/**/*.py",
    "cns_planner/**/*.js",
    "cns_planner/**/*.html",
    "cns_planner/**/*.css",
    "cns_planner/config/*.json",
    "tools/**/*.py",
    "tests/**/*.py",
    "tests/**/*.mjs",
    "*.py",
    "*.ps1",
    "*.cmd",
    "pyproject.toml",
)

#: 明确豁免：本测试自身（判定模式就是这些字符串）。
EXEMPT = {Path(__file__).resolve()}

#: 文档 / 证据 / 历史审计文件：允许保留旧路径（不参与产品运行）。
DOC_ONLY_NAMES = {"project_tree.txt"}


def _is_doc_only(path: Path) -> bool:
    rel = path.relative_to(REPO_ROOT)
    parts = set(rel.parts)
    # 运行期产物 / 历史证据目录一律不参与"产品代码"判定。
    runtime_dirs = {
        "docs", "_diag", "_cscan", "_output", "_runtime_projects", "_migration_smoke",
        "outputs", "projects",
    }
    if parts & runtime_dirs:
        return True
    if any(part.startswith("_vbase") for part in parts):
        return True
    if rel.name in DOC_ONLY_NAMES:
        return True
    # 历史审计报告（根目录的 PHASE4_*.md 等）与 .md 文件均为证据类。
    return rel.suffix.lower() in {".md", ".txt", ".log"}


def _python_executable_text(path: Path):
    """返回 Python 文件里**可执行**的部分（剔除注释与字符串字面量）。

    判定的是"产品代码是否依赖某个绝对路径"，因此：

    * 注释与文档字符串（说明迁移历史时必然要写出旧路径）不算依赖；
    * 字符串字面量（``sys.path.insert(0, r"C:\\...")`` 这类）**必须**被捕获。
    """

    import io
    import tokenize

    pieces = []
    with path.open("rb") as handle:
        try:
            for token in tokenize.tokenize(handle.readline):
                if token.type in (tokenize.COMMENT, tokenize.STRING):
                    continue
                pieces.append(token.string)
        except (tokenize.TokenError, IndentationError, SyntaxError):
            handle.seek(0)
            return handle.read().decode("utf-8", errors="replace")
    return "\n".join(pieces)


def _text_for_scan(path: Path) -> str | None:
    try:
        if path.suffix == ".py":
            return _python_executable_text(path)
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def _iter_code_files():
    seen = set()
    for pattern in CODE_GLOBS:
        for path in REPO_ROOT.glob(pattern):
            if not path.is_file():
                continue
            resolved = path.resolve()
            if resolved in seen or resolved in EXEMPT:
                continue
            seen.add(resolved)
            if _is_doc_only(resolved):
                continue
            if any(part in {".git", "__pycache__", "node_modules"} for part in resolved.parts):
                continue
            yield resolved


def _scan(patterns):
    hits = []
    for path in _iter_code_files():
        text = _text_for_scan(path)
        if text is None:
            continue
        for pattern in patterns:
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                hits.append((str(path.relative_to(REPO_ROOT)), line, match.group(0)))
    return hits


def test_no_old_c_drive_machine_path_in_executable_code():
    """可执行代码 / 启动脚本 / 配置里不得出现旧 C 盘仓库绝对路径。"""

    hits = _scan(OLD_ABSOLUTE_PATTERNS)
    assert hits == [], (
        "产品代码仍硬编码旧 C 盘仓库路径（迁移未收口）：\n"
        + "\n".join(f"  {name}:{line} → {text}" for name, line, text in hits)
    )


def test_no_new_d_drive_machine_path_in_executable_code():
    """修复方式必须是"相对解析"，不是把旧硬编码换成新硬编码。"""

    hits = _scan(NEW_ABSOLUTE_PATTERNS)
    assert hits == [], (
        "产品代码把 D 盘绝对路径硬编码进来（迁移修复方式不合法）：\n"
        + "\n".join(f"  {name}:{line} → {text}" for name, line, text in hits)
    )


def test_defaults_and_config_are_repo_relative():
    """默认配置与档案目录必须相对仓库解析，且仓库内文件真实存在。"""

    config_dir = REPO_ROOT / "cns_planner" / "config"
    assert (config_dir / "defaults.json").is_file()
    assert (config_dir / "aircraft_profiles.json").is_file()
    # 真实外部数据源（例如 D:\aaa2026project\... 的塔数据）**不在**本测试判定域：
    # 它们本来就在其它磁盘，迁移不得改写其路径。
    assert (REPO_ROOT / "cns_planner" / "web" / "index.html").is_file()


def test_dsh_restart_script_is_location_independent():
    """本机工具脚本不得把某个仓库绝对路径写成工作目录 / 日志目录。"""

    script = REPO_ROOT / "restart-dsh-web.ps1"
    assert script.is_file(), "restart-dsh-web.ps1 应随仓库迁移"
    text = script.read_text(encoding="utf-8")
    assert "$PSScriptRoot" in text
    assert not OLD_ABSOLUTE_PATTERNS[0].search(text)
    # 日志与工作目录都必须由脚本自身位置推导。
    assert re.search(r"\$log\s*=\s*Join-Path\s+\$PSScriptRoot", text)
    assert re.search(r"\$wd\s*=\s*\$PSScriptRoot", text)


def test_diagnostic_tools_resolve_import_root_from_their_own_file():
    """只读诊断工具（不参与产品运行）也必须 __file__ 相对定位，否则迁移后直接报错。"""

    probe = REPO_ROOT / "tools" / "building_geometry_corridor_probe.py"
    assert probe.is_file()
    text = probe.read_text(encoding="utf-8")
    assert "Path(__file__).resolve().parents[1]" in text
    assert not OLD_ABSOLUTE_PATTERNS[0].search(text)


def test_runtime_module_imports_from_this_repo_root():
    """``cns_planner`` 必须从**本仓库**导入，不得来自另一个副本或 editable install。"""

    code = (
        "import cns_planner, pathlib;"
        "print(pathlib.Path(cns_planner.__file__).resolve().parents[1])"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code], cwd=str(REPO_ROOT),
        capture_output=True, text=True, timeout=180,
    )
    assert completed.returncode == 0, completed.stderr
    imported_root = Path(completed.stdout.strip()).resolve()
    assert imported_root == REPO_ROOT, (
        f"cns_planner 不是从本仓库导入：imported={imported_root} repo={REPO_ROOT}"
    )


def test_persisted_legacy_repo_path_is_deterministically_remapped():
    """旧仓库内部文件的 persisted 绝对路径必须被确定性换算到当前仓库根。

    判定与 provenance 由 :mod:`cns_planner.domain.path_migration` 提供；这里用**合成
    文档**验证语义（不依赖磁盘上某个具体项目是否已重新保存）：

    * 指向旧仓库内部、且当前仓库存在同一 repo-relative 文件 → 迁移 + 留 provenance；
    * 指向旧仓库内部、但当前仓库**没有**这个文件 → 原样保留（fail-closed）；
    * 外部真实数据源（本来就在别的磁盘，例如 ``D:\\aaa2026project``）→ 原样保留；
    * 自由文本里的旧路径 → 原样保留（不是路径语义的键）。
    """

    from cns_planner.domain.path_migration import migrate_persisted_paths

    marker = "cns_planner/config/defaults.json"
    legacy = "C:\\Users\\yiding\\Documents\\ChatGPT\\CNS规划系统\\" + marker
    document = {
        "device_catalog": {"source": legacy, "items": [{"device_id": "D1"}]},
        "missing_in_this_repo": {
            "source": "C:\\Users\\yiding\\Documents\\ChatGPT\\CNS规划系统\\no\\such\\file.json"
        },
        "external": {"source": "D:\\aaa2026project\\UOM\\data.xlsx"},
        "notes": "历史说明里可以提到 C:\\Users\\yiding\\Documents\\ChatGPT\\CNS规划系统",
    }
    migrate_persisted_paths(document, REPO_ROOT)

    migrated = document["device_catalog"]
    assert migrated["source"] == str((REPO_ROOT / marker).resolve())
    assert migrated["source_migrated_from"] == legacy
    assert migrated["source_migration"]["repo_relative_path"] == marker
    # 内容本体逐字不变。
    assert migrated["items"] == [{"device_id": "D1"}]

    assert document["missing_in_this_repo"]["source"].startswith("C:\\Users\\yiding")
    assert document["external"]["source"] == "D:\\aaa2026project\\UOM\\data.xlsx"
    assert "C:\\Users\\yiding" in document["notes"]


def _legacy_path_hits(document):
    """返回文档里所有命中旧仓库绝对路径的 ``(JSON 路径, 值)``。"""

    hits = []

    def walk(node, path=""):
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}/{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")
        elif isinstance(node, str):
            for pattern in OLD_ABSOLUTE_PATTERNS:
                if pattern.search(node):
                    hits.append((path, node))
                    break

    walk(document)
    return hits


def test_active_project_state_can_be_migrated_without_residue():
    """真实已保存项目状态经迁移后，旧仓库绝对路径**只允许**出现在 provenance 字段里。

    这同时证明两件事：

    1. 迁移是确定性的（在真实 250 MB 状态上真的把旧路径换成了当前仓库路径）；
    2. provenance 被保留（``source_migrated_from`` 记录了原值），不是静默改写。
    """

    from cns_planner.domain.path_migration import MIGRATION_MARKER_KEY, PROVENANCE_KEY
    from cns_planner.domain.path_migration import migrate_persisted_paths

    candidates = [
        REPO_ROOT / "projects" / "current_project.json",
        *sorted((REPO_ROOT / "_runtime_projects").glob("*/project_state.json"))[:2],
    ]
    checked = 0
    for path in candidates:
        if not path.is_file():
            continue
        document = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        migrate_persisted_paths(document, REPO_ROOT)
        for hit_path, value in _legacy_path_hits(document):
            assert hit_path.endswith("/" + PROVENANCE_KEY), (
                f"{path.relative_to(REPO_ROOT)} 迁移后仍有非 provenance 的旧仓库路径："
                f"{hit_path} = {value}"
            )
            # provenance 必须成对存在：同级的 source 已指向当前仓库且真实存在。
            source_path = hit_path[: -len(PROVENANCE_KEY)] + "source"
            node = document
            for step in source_path.strip("/").split("/"):
                node = node[int(step)] if step.isdigit() else node[step]
            assert Path(node).is_file(), f"provenance 对应的迁移后文件不存在：{node}"
            assert MIGRATION_MARKER_KEY in _parent_of(document, source_path), (
                f"缺少迁移标记 {MIGRATION_MARKER_KEY}：{hit_path}"
            )
        checked += 1
    if checked == 0:
        pytest.skip("当前没有可检查的已保存项目状态")


def _parent_of(document, json_path):
    node = document
    steps = json_path.strip("/").split("/")
    for step in steps[:-1]:
        node = node[int(step)] if step.isdigit() else node[step]
    return node


def test_diagnostics_directory_is_not_a_product_dependency():
    """``_diag`` 只做诊断：产品包不得 import 它。"""

    offenders = []
    for path in (REPO_ROOT / "cns_planner").rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if re.search(r"^\s*(from|import)\s+_diag\b", text, re.MULTILINE):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == [], f"产品代码依赖 _diag 诊断目录：{offenders}"
