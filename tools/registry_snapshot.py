# -*- coding: utf-8 -*-
"""导出当前真实的算法默认选择与注册表，用于校正文档。

背景：README 的算法注册表曾是手工维护的清单，2026-10-05 实测发现它有 4 处过时
（`site_planner` 2.0→2.2、`corridor_model` 1.0→1.3、`corridor_gap_analyzer` 1.0→1.2、
`requirement_model` 的真实默认不是 V2）。文档会漂移，代码不会 —— 所以把"当前注册表"
做成可执行的事实来源：这个脚本直接运行 registry，输出可粘贴进文档的清单。

只读：仅导入 `cns_planner.algorithms.registry`，不加载 QGIS、不读写 ProjectState。

用法：
    python tools/registry_snapshot.py              # 打印到 stdout
    python tools/registry_snapshot.py --out r.txt   # 同时写入文件
    python tools/registry_snapshot.py --check       # 只校验默认选择是否都能解析（CI 友好）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cns_planner.algorithms.registry import (  # noqa: E402
    ALGORITHM_TYPES,
    build_default_algorithm_registry,
    default_algorithm_selection,
)


def build_report() -> tuple[str, list[tuple[str, str, str]]]:
    defaults = default_algorithm_selection()
    registry = build_default_algorithm_registry(defaults)
    manifests = sorted(registry.manifests(), key=lambda m: (m.algorithm_type, m.algorithm_id))

    registered = {(m.algorithm_type, m.algorithm_id, m.version) for m in manifests}
    unresolved = [
        (t, e["algorithm_id"], e["version"])
        for t, e in defaults.items()
        if (t, e["algorithm_id"], e["version"]) not in registered
    ]
    not_default = [
        (m.algorithm_type, m.algorithm_id, m.version)
        for m in manifests
        if (m.algorithm_type, m.algorithm_id, m.version)
        not in {(t, e["algorithm_id"], e["version"]) for t, e in defaults.items()}
    ]

    lines: list[str] = []
    lines.append(f"ALGORITHM_TYPES（代码定义的全部类型，共 {len(ALGORITHM_TYPES)} 个）")
    lines.append("  " + "  ".join(ALGORITHM_TYPES))
    lines.append("")

    lines.append("新建项目的默认选择（default_algorithm_selection()）")
    lines.append(f"  {'algorithm_type':<24}{'algorithm_id':<44}{'version'}")
    for t, e in defaults.items():
        lines.append(f"  {t:<24}{e['algorithm_id']:<44}{e['version']}")
    lines.append("")

    lines.append("已注册但没有默认选择的组合（只能由项目显式选择）")
    if not_default:
        for t, aid, ver in not_default:
            lines.append(f"  {t:<24}{aid:<44}{ver}")
    else:
        lines.append("  （无）")
    lines.append("")

    lines.append("已注册的 Manifest（可执行工厂）")
    lines.append(f"  {'algorithm_type':<24}{'algorithm_id':<44}{'version'}")
    for m in manifests:
        lines.append(f"  {m.algorithm_type:<24}{m.algorithm_id:<44}{m.version}")
    lines.append("")

    lines.append(f"默认选择中无法解析的组合：{len(unresolved)} 个（应为 0）")
    for t, aid, ver in unresolved:
        lines.append(f"  [!] {t} / {aid} @ {ver}")

    return "\n".join(lines), unresolved


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="导出真实算法注册表快照")
    parser.add_argument("--out", help="同时写入该文件（UTF-8 无 BOM）")
    parser.add_argument("--check", action="store_true",
                        help="只校验：默认选择是否全部可解析；有无法解析的返回非零退出码")
    args = parser.parse_args(argv)

    report, unresolved = build_report()

    if args.check:
        if unresolved:
            print(f"默认选择中有 {len(unresolved)} 个组合无法解析：")
            for t, aid, ver in unresolved:
                print(f"  {t} / {aid} @ {ver}")
            return 1
        print("默认选择全部可解析。")
        return 0

    print(report)
    if args.out:
        # 必须无 BOM：Harness 对部分配置直接 JSON.parse / 按 UTF-8 读取，BOM 会造成解析失败
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"\n[已写入] {args.out}")
    return 1 if unresolved else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
