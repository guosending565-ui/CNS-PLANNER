"""VERIFY_V6：复用 V5 全量生产链验收，并追加冻结前第四轮检查。

只写 ``outputs/route_overview_VERIFY_V6.png`` 与 ``_spec.json``；V5 产物只读保护。
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

import verify_route_overview_v5 as v5


IMAGE_PATH = Path("outputs/route_overview_VERIFY_V6.png")
SPEC_PATH = Path("outputs/route_overview_VERIFY_V6_spec.json")


def main():
    v5.IMAGE_PATH = IMAGE_PATH
    v5.SPEC_PATH = SPEC_PATH
    v5.PROBE_PROJECT = Path("_diag/v6_probe_project")
    v5.PROTECTED = (
        Path("outputs/route_overview_VERIFY_V5.png"),
        Path("outputs/route_overview_VERIFY_V5_spec.json"),
    )
    result = v5.main()
    if result != 0:
        return result

    payload = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    tower = next(layer for layer in payload["layers"]
                 if layer["layer_key"] == "tower_existing")
    detail = tower.get("source_detail") or {}
    counts = {
        key: detail.get(key) for key in (
            "total_count", "within_extent", "within_route_buffer",
            "omitted_by_route_distance",
        )
    }
    from cns_planner.gis.figure_style import FIGURE_STYLES, LAYOUT

    checks = [
        ("站址展示缓冲固定 10 km",
         payload["parameters"].get("site_display_buffer_km") == 10.0),
        ("站址审计计数完整", all(isinstance(value, int) for value in counts.values())),
        ("距离省略数守恒",
         counts["within_extent"] - counts["within_route_buffer"]
         == counts["omitted_by_route_distance"]),
        ("footer 字号可读", 5.5 <= float(LAYOUT["footer_strip_font_size"]) <= 6.0),
        ("地图站址 marker 保持 2.8 mm",
         float(FIGURE_STYLES["tower_existing"]["size"]) == 2.8),
    ]
    print("\n=== G V6 冻结前追加验收 ===", flush=True)
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}", flush=True)
    print("  站址 total / within extent / within route buffer / omitted by distance = "
          f"{counts['total_count']} / {counts['within_extent']} / "
          f"{counts['within_route_buffer']} / {counts['omitted_by_route_distance']}",
          flush=True)
    print(f"  V6 PNG SHA256 = {v5.sha256(IMAGE_PATH)}", flush=True)
    print(f"  V6 Spec SHA256 = {v5.sha256(SPEC_PATH)}", flush=True)
    print("\nVERIFY_V6_DONE", flush=True)
    return 0 if all(passed for _label, passed in checks) else 5


if __name__ == "__main__":
    sys.exit(main())
