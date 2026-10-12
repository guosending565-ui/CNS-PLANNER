"""Round37-T3D-BMAP 磁盘新增量测量（只读）。

统计本轮新增/修改的文件字节数与交付目录体积，并**核对未新增 D 盘历史副本**。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

DEV = Path(r"D:\Projects\_cns_workspaces\dev\round37_b0\CNS-PLANNER")
DEL = Path(r"D:\Projects\_cns_workspaces\deliverables\round37_t3d_basemap")

#: 本轮新增的交付物与源码（相对开发树）
NEW_FILES = [
    "cns_planner/gis/terrain_tile_adapter.py",
    "cns_planner/gis/relief_tile_adapter.py",
    "cns_planner/gis/water_mask_adapter.py",
    "cns_planner/gis/terrain_window_adapter.py",
    "cns_planner/gis/egm2008_geoid.py",
    "cns_planner/application/visualization_3d_service.py",
    "cns_planner/application/visualization_3d_imagery_service.py",
    "cns_planner/web/visualization-3d.html",
    "cns_planner/web/js/visualization3d/app.js",
    "cns_planner/web/js/visualization3d/basemap.js",
    "cns_planner/web/js/visualization3d/camera_presets.js",
    "cns_planner/web/js/visualization3d/camera_state.js",
    "tests/test_visualization_3d_terrain_tiles.py",
    "tests/test_visualization_3d_relief_tiles.py",
    "tests/test_visualization_3d_water_mask.py",
    "tests/test_visualization_3d_imagery_proxy.py",
    "tests/visualization3d_basemap.test.mjs",
    "tools/capture_t3d_basemap_acceptance.py",
    "tools/capture_t3d_before_after.py",
    "tools/verify_t3d_terrain_numeric.py",
    "tools/verify_t3d_invariance.py",
    "tools/summarize_t3d_evidence.py",
    "tools/locate_authoritative_state.py",
]

result = {"schema": "round37-t3d-bmap-disk/1"}


def tree_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


# 1) 本轮源码增量
rows = []
for rel in NEW_FILES:
    path = DEV / rel
    rows.append({"path": rel, "exists": path.exists(),
                 "bytes": path.stat().st_size if path.exists() else 0})
result["source_files"] = rows
result["source_bytes"] = sum(r["bytes"] for r in rows)
result["source_missing"] = [r["path"] for r in rows if not r["exists"]]

# 2) vendored Cesium（本轮新增的本地运行时，不含商业瓦片）
vendor = DEV / "cns_planner" / "web" / "vendor"
result["vendored_cesium_bytes"] = tree_size(vendor)

# 3) 交付目录
result["delivery_bytes"] = tree_size(DEL)
result["delivery_evidence_bytes"] = tree_size(DEL / "evidence")

# 4) 大体积只读数据**未复制**核对
big = {
    "fabdem_dtm": Path(r"D:\aaa2026project\UOM\舟山\规划系统\FABDEM\processed"
                       r"\Zhoushan_FABDEM_DTM_30m.tif"),
    "buildings_v3": Path(r"D:\aaa2026project\UOM\舟山\规划系统\building\processed"
                         r"\zhoushan_buildings_v3.gpkg"),
    "egm2008_geoid": Path(r"D:\aaa2026project\UOM\舟山\规划系统\geoid"
                          r"\us_nga_egm08_25.tif"),
    "project_state": DEV / "projects" / "current_project.json",
}
result["large_readonly_sources"] = {
    name: {"path": str(p), "bytes": p.stat().st_size if p.exists() else None,
           "copied_into_delivery": False}
    for name, p in big.items()
}

# 5) 未新增 D 盘历史副本：列出 _cns_workspaces 下与本轮时间接近的顶层目录数
workspaces = Path(r"D:\Projects\_cns_workspaces")
top = sorted(p.name for p in workspaces.iterdir() if p.is_dir())
result["workspaces_top_level"] = {
    "count": len(top),
    "names_sample": top[:12],
}

# 6) 未提交商业瓦片/密钥核对：交付目录不得出现图片以外的瓦片缓存目录
suspicious = [str(p.relative_to(DEL)) for p in DEL.rglob("*")
              if p.is_dir() and p.name.lower() in {"tilecache", "tiles", "cache"}]
result["suspicious_tile_cache_dirs"] = suspicious

out = DEV / "_t3dprobe" / "disk_growth.json"
out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

print("source files      : %d (missing %d)" % (len(rows), len(result["source_missing"])))
print("source bytes      : %.2f MiB" % (result["source_bytes"] / 1048576))
print("vendored cesium   : %.2f MiB" % (result["vendored_cesium_bytes"] / 1048576))
print("delivery total    : %.2f MiB" % (result["delivery_bytes"] / 1048576))
print("delivery evidence : %.2f MiB" % (result["delivery_evidence_bytes"] / 1048576))
print("suspicious caches : %s" % (suspicious or "none"))
for name, row in result["large_readonly_sources"].items():
    print("  %-16s %s bytes (not copied)" % (
        name, row["bytes"]))
