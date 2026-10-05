# -*- coding: utf-8 -*-
"""去除 JSON/YAML 配置文件开头的 UTF-8 BOM。

背景：Harness 0.2.0-rc.2 对 profiles/desktop/package.json 直接 JSON.parse、不去 BOM，
开头带 EF BB BF 会在第 0 个字符解析失败，导致应用无法启动。PowerShell 5.1 的
`Set-Content / Out-File -Encoding UTF8` 会写出 BOM，所以凡是这类命令改过的
JSON/YAML 都可能在文件开头带上 BOM。

用法：
    python tools/remove_config_bom.py --check      # 只报告，不修改
    python tools/remove_config_bom.py              # 就地去除 BOM

安全性：
    * 只处理 .json / .yaml / .yml；
    * 只删开头 3 字节，其余字节原样保留（换行符、缩进、键顺序都不变）；
    * 不触碰 .ps1（PowerShell 脚本反而需要 BOM，否则 5.1 会按 ANSI 解码中文）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BOM = b"\xef\xbb\xbf"
TARGET_SUFFIXES = {".json", ".yaml", ".yml"}

DEFAULT_ROOTS = [
    Path(r"D:\tools\dsh-home"),
]

SKIP_DIR_PARTS = {
    "node_modules", ".pnpm", "sessions", "cache", "logs", "attachments",
    "dsh-session-archive", "dsh-runtimes", "dsh-usage", "llm-deepseek",
    "skin-center", "skins", "whale-audio", "whale-bubble-imgs", "whale-roles",
}


def iter_candidates(root: Path):
    if root.is_file():
        if root.suffix.lower() in TARGET_SUFFIXES:
            yield root
        return
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel_parts = set(path.relative_to(root).parts)
        except ValueError:
            continue
        if rel_parts & SKIP_DIR_PARTS:
            continue
        if path.suffix.lower() in TARGET_SUFFIXES:
            yield path


def verify_after(raw_without_bom: bytes, path: Path) -> str:
    """去掉 BOM 之后做一次可解析性自检。"""
    if path.suffix.lower() != ".json":
        return "yaml(未自检)"
    try:
        json.loads(raw_without_bom.decode("utf-8"))
        return "json ok"
    except Exception as exc:  # pragma: no cover - 仅报告
        return f"json 解析失败: {type(exc).__name__}"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="去除 JSON/YAML 配置文件的 UTF-8 BOM")
    parser.add_argument("--check", action="store_true", help="只报告，不修改")
    parser.add_argument("--root", action="append", default=None,
                        help="扫描根目录，可重复；默认 D:\\tools\\dsh-home")
    args = parser.parse_args(argv)

    roots = [Path(r) for r in (args.root or DEFAULT_ROOTS)]
    found = []
    for root in roots:
        for path in iter_candidates(root):
            try:
                raw = path.read_bytes()
            except OSError as exc:
                print(f"  跳过（读取失败）{path}: {exc}")
                continue
            if raw.startswith(BOM):
                found.append((path, raw))

    if not found:
        print("未发现带 BOM 的 JSON/YAML 文件。")
        return 0

    print(f"发现 {len(found)} 个带 BOM 的文件：")
    changed = 0
    for path, raw in found:
        stripped = raw[len(BOM):]
        note = verify_after(stripped, path)
        if args.check:
            print(f"  [待修复] {path}  ({note})")
            continue
        path.write_bytes(stripped)
        after = path.read_bytes()[:3] == BOM
        changed += 1
        print(f"  [已修复] {path}  ({note}) BOM={after}")
    if args.check:
        print(f"\n共 {len(found)} 个待修复；去掉 --check 即就地去除。")
    else:
        print(f"\n共修复 {changed} 个。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
