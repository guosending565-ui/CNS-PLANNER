# -*- coding: utf-8 -*-
"""校验项目级 skill（`.dsh/skills/`）是否符合 DSH 的发现约定。

DSH 的 `@deepseek-ai/dsh-skill-filesystem` 只识别**一层**：
    <projectRoot>/.dsh/skills/<name>/SKILL.md      （目录 bundle）
    <projectRoot>/.dsh/skills/<name>.md            （平铺文件）
并且刻意**不支持**嵌套的 `**/SKILL.md`。frontmatter 必填 `name`（kebab-case）与 `description`，
可选 `whenToUse` / `metadata` / `disable-model-invocation` / `user-invocable`。
格式错误或名称非法的 skill 会被**静默跳过**（模型目录里看不到），所以需要主动自检。

用法：
    python tools/verify_project_skills.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = REPO_ROOT / ".dsh" / "skills"
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
KEBAB_HINT = "name 必须是 kebab-case（小写字母/数字，连字符分隔）"


def parse_frontmatter(text: str) -> dict | None:
    """极简 YAML frontmatter 解析：只取顶层 `key: value` 标量。"""
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    block = text[3:end]
    out: dict[str, str] = {}
    for line in block.splitlines():
        line = line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] in " \t":  # 嵌套结构（metadata 等）不解析为标量
            continue
        if ":" not in line:
            return None
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def main() -> int:
    if not SKILLS_DIR.is_dir():
        print(f"未找到 skills 目录：{SKILLS_DIR}")
        return 1

    ok: list[tuple[str, int]] = []
    bad: list[tuple[str, str]] = []

    for entry in sorted(SKILLS_DIR.iterdir()):
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            target = entry / "SKILL.md"
            if not target.is_file():
                bad.append((entry.name, "目录 bundle 缺少 SKILL.md"))
                continue
        elif entry.suffix == ".md":
            target = entry
        else:
            bad.append((entry.name, "不是目录 bundle 也不是 .md 文件"))
            continue

        text = target.read_text(encoding="utf-8")
        fm = parse_frontmatter(text)
        if fm is None:
            bad.append((entry.name, "缺少或无法解析 YAML frontmatter"))
            continue

        name = fm.get("name", "")
        if not name:
            bad.append((entry.name, "frontmatter 缺少 name"))
            continue
        if not NAME_RE.match(name):
            bad.append((entry.name, f"name 不合法：{name!r} —— {KEBAB_HINT}"))
            continue
        # 目录 bundle 的目录名应与 name 一致（便于定位；DSH 以 name 为准）
        if entry.is_dir() and entry.name != name:
            bad.append((entry.name, f"目录名与 frontmatter name 不一致（name={name!r}）"))
            continue
        if not fm.get("description"):
            bad.append((entry.name, "frontmatter 缺少 description（必填）"))
            continue

        lines = len(text.splitlines())
        ok.append((name, lines))

        # 嵌套 SKILL.md 会被 DSH 忽略：明确报出来
        for nested in sorted(entry.rglob("SKILL.md")) if entry.is_dir() else []:
            if nested.parent != entry:
                bad.append((str(nested.relative_to(SKILLS_DIR)),
                            "嵌套的 SKILL.md 不会被发现（发现深度只有一层）"))

    print(f"skills 目录：{SKILLS_DIR}")
    print(f"有效 skill：{len(ok)} 个")
    for name, lines in ok:
        print(f"  [OK]   {name:<28} {lines} 行")
    if bad:
        print(f"\n有问题的条目：{len(bad)} 个")
        for name, why in bad:
            print(f"  [BAD]  {name} —— {why}")
        return 1
    print("\n全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
