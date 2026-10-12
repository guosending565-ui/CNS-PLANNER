# -*- coding: utf-8 -*-
"""检查权威文档里引用的仓库内路径是否真实存在。

动机：文档漂移最隐蔽的一种形式是指向已改名/已删除的文件。本脚本把
AGENTS.md / AI_DEV_CONTEXT.md / README.md 里所有反引号包裹的、看起来像
仓库内相对路径的字符串抽出来，逐一检查存在性。

只读。用法：python tools/verify_doc_paths.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOCS = ["AGENTS.md", "AI_DEV_CONTEXT.md", "README.md"]

# 反引号里的内容
TICK = re.compile(r"`([^`\n]+)`")
# 像仓库内路径：含 / 且以已知顶层目录开头，或指向具体文件名
TOP_DIRS = ("cns_planner/", "tests/", "tools/", "docs/", "outputs/", ".dsh/")
SUFFIXES = (".py", ".js", ".mjs", ".md", ".json", ".yml", ".yaml", ".cmd", ".ps1", ".toml")
# 解析候选路径时依次尝试的前缀：文档里常以 cns_planner 内的子目录相对书写
PREFIXES = ("", "cns_planner/", "cns_planner/web/", "cns_planner/web/js/")
# 明确位于工作区之外（DSH 家目录等）的引用，不做存在性检查
OUTSIDE_MARKERS = ("profiles/", "dsh-home", "dsh-web", "AppData", "\\", ":/")


def looks_like_repo_path(text: str) -> bool:
    t = text.strip()
    if not t or " " in t or t.startswith(("http", "-", "|")):
        return False
    # 明显在工作区之外的（绝对路径、DSH profile 等）跳过
    if any(mk in t for mk in OUTSIDE_MARKERS):
        return False
    if t.startswith(TOP_DIRS):
        return True
    # 形如 application/foo.py 的裸相对路径（文档里通常相对 cns_planner/）
    return "/" in t and t.endswith(SUFFIXES)


def resolve(cand: str) -> bool:
    for prefix in PREFIXES:
        if (REPO / (prefix + cand)).exists():
            return True
    return False


def main() -> int:
    missing: list[tuple[str, int, str]] = []
    skipped: list[str] = []
    checked = 0

    for name in DOCS:
        p = REPO / name
        if not p.is_file():
            print(f"[!] 文档不存在：{name}")
            return 1
        for lineno, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            for m in TICK.finditer(line):
                cand = m.group(1)
                if not looks_like_repo_path(cand):
                    continue
                # 去掉锚点与行号后缀
                clean = cand.split("#")[0].strip()
                if any(mk in clean for mk in OUTSIDE_MARKERS):
                    skipped.append(clean)
                    continue
                checked += 1
                if not resolve(clean):
                    missing.append((name, lineno, cand))

    print(f"检查了 {checked} 处仓库内路径引用；跳过 {len(skipped)} 处工作区外引用。")
    if missing:
        print(f"\n找不到的引用（{len(missing)} 处）：")
        for name, lineno, cand in missing:
            print(f"  {name}:{lineno}  ->  {cand}")
        return 1
    print("全部路径存在。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
