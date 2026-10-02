"""C 盘 → D 盘迁移：**已持久化项目状态**里的确定性地路径迁移。

背景
----

项目状态里有一类字段记录的是"这份数据是从哪个文件导入的"（``source`` /
``configured_path`` 等）。它们保存的是**绝对路径**。仓库从
``C:\\Users\\yiding\\Documents\\ChatGPT\\CNS规划系统`` 复制到
``D:\\Projects\\CNS-PLANNER`` 之后，这些历史绝对路径仍然指向旧位置。

按 Round 2.6 的迁移规则：

* 如果 persisted path **明确指向旧仓库内部的文件**，且同一 *repo-relative* 文件在
  **当前仓库**里确实存在，就做**确定性迁移**（把前缀换成当前仓库根）；
* 必须**保存 provenance**：原值记入 ``source_migrated_from``，并显式标注这是一次
  迁移（``source_migration``），绝不静默改写；
* 任何一条不满足（不是旧仓库路径、或当前仓库没有对应文件）→ **原样保留**，
  绝不猜测（fail-closed）。

本模块只做"字符串前缀的确定性重写"，不做任何领域判定，不写 state、不读网络。
"""

from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path

#: 迁移记录写在哪个键上（只对带 ``source`` 的目录式条目生效）。
PROVENANCE_KEY = "source_migrated_from"
MIGRATION_MARKER_KEY = "source_migration"

#: 旧仓库的目录名（两个候选都是"旧仓库根"的稳定标识）。
_LEGACY_ROOT_NAMES = ("CNS规划系统", "CNS-PLANNER")

#: 形如 ``C:\...\CNS规划系统\rest`` 或 ``D:/.../CNS-PLANNER/rest``。
#: 仓库根名必须是一个**完整路径段**（后面紧跟分隔符或字符串结束），否则会误伤同级目录
#: （例如 ``...\ChatGPT\CNS_VALIDATION\...`` **不是**旧仓库内部路径）。
_LEGACY_ROOT_ALTERNATIVES = "|".join(re.escape(name) for name in _LEGACY_ROOT_NAMES)

#: 取"仓库根名 + 分隔符"之前的最长前缀（保留盘符与盘内目录，供 provenance 使用）。
_LEGACY_PREFIX_RE = re.compile(
    r"^(?P<prefix>[A-Za-z]:[\\/]+.*?[\\/](?:" + _LEGACY_ROOT_ALTERNATIVES + r"))"
    r"(?P<sep>[\\/])(?P<rest>.*)$"
)

#: 仅判定"是否指向旧仓库内部"（不取值）。
_LEGACY_ROOT_RE = re.compile(
    r"^[A-Za-z]:[\\/]+.*?[\\/](?:" + _LEGACY_ROOT_ALTERNATIVES + r")(?:[\\/]|$)"
)

#: 判定域键名：只有这些键上的字符串才可能被当作"文件来源路径"。
PATH_KEYS = frozenset({
    "source", "configured_path", "path", "file", "source_path", "catalog_path",
    "import_path", "absolute_path",
})


def split_legacy_repo_path(value, repo_root):
    """把一条旧仓库绝对路径拆成 ``(相对仓库根的路径, 原始值)``。

    返回 ``None`` 表示**不是**旧仓库内部路径（或无法安全解析），调用方必须原样保留。
    """

    if not isinstance(value, str) or not value:
        return None
    match = _LEGACY_PREFIX_RE.match(value)
    if match is None:
        return None
    rest = (match.group("rest") or "").strip("\\/")
    if not rest:
        return None
    relative = rest.replace("\\", "/")
    # 防御：不得出现 ``..`` 逃逸（那就不再是"repo 内文件"）。
    if any(part in ("..", "") for part in relative.split("/")):
        return None
    candidate = Path(repo_root) / relative
    if not candidate.exists():
        return None
    return relative, value


def migrate_path_value(value, repo_root):
    """对单个字符串做确定性迁移；不适用时**原样返回**。"""

    split = split_legacy_repo_path(value, repo_root)
    if split is None:
        return value
    relative, _original = split
    return str((Path(repo_root) / relative).resolve())


def migrate_persisted_paths(document, repo_root):
    """递归迁移一个已保存状态里的旧仓库绝对路径（原地修改并返回同一对象）。

    只对**路径语义明确的键**（见 :data:`PATH_KEYS`）做迁移，避免把用户自由文本、
    报告正文或外部数据源路径（本来就在别的磁盘）改掉。
    """

    if not isinstance(document, dict) or repo_root in (None, ""):
        return document
    root = Path(repo_root)
    for key, value in list(document.items()):
        if key in PATH_KEYS and isinstance(value, str):
            split = split_legacy_repo_path(value, root)
            if split is None:
                continue
            relative, original = split
            document[key] = str((root / relative).resolve())
            #: provenance 只写在"带 source 的目录/条目"上，且只在键自身就叫
            #: ``source`` 时标注，避免给每个 ``path`` 键都塞一个兄弟键。
            if key == "source":
                document.setdefault(PROVENANCE_KEY, original)
                document.setdefault(MIGRATION_MARKER_KEY, {
                    "kind": "legacy_repo_absolute_path_remapped_to_current_repo_root",
                    "repo_relative_path": relative,
                    "semantics": "path_identity_preserved_content_unchanged",
                })
            continue
        if isinstance(value, dict):
            migrate_persisted_paths(value, root)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    migrate_persisted_paths(item, root)
    return document


def migrated_copy(document, repo_root):
    """返回迁移后的**副本**（调用方需要保留原件时使用）。"""

    return migrate_persisted_paths(deepcopy(document), repo_root)


__all__ = [
    "MIGRATION_MARKER_KEY", "PATH_KEYS", "PROVENANCE_KEY",
    "migrate_persisted_paths", "migrate_path_value", "migrated_copy",
    "split_legacy_repo_path",
]
