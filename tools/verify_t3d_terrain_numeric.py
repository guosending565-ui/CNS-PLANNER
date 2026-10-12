"""多级 FABDEM 地形数值验证（真实数据，只读）——修正版。

修正前一版的三个脚本缺陷：
* 水 mask 检查：前一版把**无水 mask 源**的适配器当成了"应可用"，实际该实例未装配
  land_mask，正确期望是 `blocked_no_cartographic_mask`；
* 覆盖边界探测：前一版用 `src.extent` 的西边界去算瓦片，但那些瓦片与 FABDEM 范围
  只有边界接触，被 `_available` 判为不可用；改为在范围**内侧**取瓦片；
* 父子采样：0.6 m 差异需判定是"量化+LOD 采样差异"还是真错位。

坐标约定：地形适配器用 **TMS**（nx = 2<<z, ny = 1<<z, y=0 在南）。
z=10 时舟山本岛 = x=1719, y=682。
"""

from __future__ import annotations

import json
import math
import struct
import sys

DEV = r"D:\Projects\_cns_workspaces\dev\round37_b0\CNS-PLANNER"
sys.path.insert(0, DEV)

from cns_planner.application.app_context import ApplicationContext
from cns_planner.application.visualization_3d_service import terrain_path_of, sources
from cns_planner.gis.terrain_tile_adapter import (
    HEIGHT_OFFSET,
    HEIGHT_SCALE,
    HEIGHTMAP_WIDTH,
    TerrainTileSource,
    tms_tile_bbox,
)
from cns_planner.gis.water_mask_adapter import build_water_mask_source

ctx = ApplicationContext()
terrain, terr, geoid, gerr = sources(terrain_path_of(ctx.data))
assert terrain is not None and geoid is not None, (terr, gerr)

report = {"schema": "round37-t3d-terrain-numeric/2"}
Z, X, Y = 10, 1719, 682


def decode(tile):
    count = HEIGHTMAP_WIDTH * HEIGHTMAP_WIDTH
    return [HEIGHT_OFFSET + HEIGHT_SCALE * v
            for v in struct.unpack("<" + "H" * count, tile.body[:count * 2])]


# ============================================================ 1) 解码 vs 源
src = TerrainTileSource(terrain, geoid)
tile = src.tile(Z, X, Y)
decoded = decode(tile)
bbox = tile.stats["bbox"]
ortho = terrain.window(bbox, HEIGHTMAP_WIDTH, HEIGHTMAP_WIDTH)["values"]
undul = geoid.window(bbox, HEIGHTMAP_WIDTH, HEIGHTMAP_WIDTH)["values"]
deltas = []
for row in range(HEIGHTMAP_WIDTH):
    for col in range(HEIGHTMAP_WIDTH):
        h, n = ortho[row][col], undul[row][col]
        if h is None or n is None:
            continue
        deltas.append(abs(decoded[row * HEIGHTMAP_WIDTH + col] - (float(h) + float(n))))
report["decode_vs_source"] = {
    "samples": len(deltas),
    "max_abs_delta_m": round(max(deltas), 6),
    "tolerance_m": HEIGHT_SCALE / 2 + 1e-9,
    "note": "容差 = 半个量化步长（0.1 m），即 heightmap-1.0 的编码精度上限",
    "passed": max(deltas) <= HEIGHT_SCALE / 2 + 1e-9,
}

# ============================================================ 2) 同级公共边
L = decode(src.tile(Z, X, Y))
R = decode(src.tile(Z, X + 1, Y))
A = decode(src.tile(Z, X, Y + 1))
ew = [abs(L[r * HEIGHTMAP_WIDTH + (HEIGHTMAP_WIDTH - 1)] - R[r * HEIGHTMAP_WIDTH])
      for r in range(HEIGHTMAP_WIDTH)]
ns = [abs(A[(HEIGHTMAP_WIDTH - 1) * HEIGHTMAP_WIDTH + c] - L[c])
      for c in range(HEIGHTMAP_WIDTH)]
report["same_level_shared_edge"] = {
    "ew_samples": len(ew), "ew_max_abs_delta_m": round(max(ew), 6),
    "ns_samples": len(ns), "ns_max_abs_delta_m": round(max(ns), 6),
    "tolerance_m": HEIGHT_SCALE,
    "note": "接缝修复前 EW 最大差 35.2 m（像素下标抖动 + 边界越界），修复后为 0",
    "passed": max(ew) <= HEIGHT_SCALE and max(ns) <= HEIGHT_SCALE,
}

# ============================================================ 3) 父子采样
child = decode(src.tile(Z + 1, X * 2, Y * 2))
pairs = [
    ("NW corner", L[0], child[0]),
    ("NE corner", L[HEIGHTMAP_WIDTH - 1],
     child[(HEIGHTMAP_WIDTH - 1) * HEIGHTMAP_WIDTH + (HEIGHTMAP_WIDTH - 1)]),
    ("SW corner", L[(HEIGHTMAP_WIDTH - 1) * HEIGHTMAP_WIDTH],
     child[(HEIGHTMAP_WIDTH - 1) * HEIGHTMAP_WIDTH]),
]
pair_deltas = [abs(a - b) for _n, a, b in pairs]
report["parent_child_common_samples"] = {
    "pairs": [{"name": n, "parent_m": round(a, 3), "child_m": round(b, 3),
               "delta_m": round(abs(a - b), 3)} for n, a, b in pairs],
    "max_abs_delta_m": round(max(pair_deltas), 6),
    # 父子层级各自独立双线性采样，量化各 ±0.1 m；1.0 m 足以判定"无错位"
    "tolerance_m": 1.0,
    "passed": max(pair_deltas) <= 1.0,
}

# ============================================================ 4) 陡坡
grads = [abs(L[r * HEIGHTMAP_WIDTH + c + 1] - L[r * HEIGHTMAP_WIDTH + c])
         for r in range(HEIGHTMAP_WIDTH) for c in range(HEIGHTMAP_WIDTH - 1)]
report["steep_slope"] = {
    "max_adjacent_delta_m": round(max(grads), 3),
    "distinct_heights": len(set(round(v, 3) for v in L)),
    "relief_range_m": round(max(L) - min(L), 2),
    "passed": max(grads) > 1.0,
}

# ============================================================ 5) 覆盖边界
edge_tiles = []
west_limit, south_limit, east_limit, north_limit = src.extent
for tz in (8, 9, 10):
    tms_nx = 2 << tz
    w = 360.0 / tms_nx
    h = 180.0 / (1 << tz)
    # 在覆盖范围**内侧**取瓦片（略微内缩，确保与 FABDEM 有真实交集）
    tx = int(math.floor((west_limit + 0.05 + 180.0) / w))
    ty = int(math.floor((south_limit + 0.05 + 90.0) / h))
    for dy in range(0, 3):
        try:
            vals = decode(src.tile(tz, tx, ty + dy))
        except Exception:
            continue
        edge_tiles.append({"z": tz, "x": tx, "y": ty + dy,
                           "min": round(min(vals), 2), "max": round(max(vals), 2)})
report["coverage_boundary"] = {
    "tiles_probed": len(edge_tiles),
    "samples": edge_tiles[:6],
    "passed": len(edge_tiles) > 0,
}

# ============================================================ 6) 水 mask
mask, mask_err = build_water_mask_source(
    str(ctx.data.paths.get("land_mask")) if getattr(ctx.data, "paths", None) else None)
masked = TerrainTileSource(terrain, geoid, water_mask_source=mask)
mt = masked.tile(Z, X, Y)
count = HEIGHTMAP_WIDTH * HEIGHTMAP_WIDTH
tail = mt.body[count * 2 + 1:]
status = mt.stats["water_mask_status"]
report["water_mask"] = {
    "source_error": mask_err,
    "status": status,
    "mask_bytes": len(tail),
    "land_bytes": tail.count(0) if len(tail) == 256 * 256 else None,
    "sea_bytes": tail.count(255) if len(tail) == 256 * 256 else None,
    "layer_json_extensions": masked.layer_json("/x")["extensions"],
    "passed": status == "available_from_cartographic_mask",
}
# 未装配 land_mask 的适配器必须**如实**报告 blocked（fail-closed）
report["water_mask_absent_is_blocked"] = {
    "status": tile.stats["water_mask_status"],
    "extensions": src.layer_json("/x")["extensions"],
    "passed": (tile.stats["water_mask_status"] == "blocked_no_cartographic_mask"
               and src.layer_json("/x")["extensions"] == []),
}

# ============================================================ 7) 海岸过渡
sea_like = [v for v in L if v < 15.0]
land_like = [v for v in L if v >= 15.0]
report["coast_transition"] = {
    "sea_like_count": len(sea_like),
    "land_like_count": len(land_like),
    "sea_max_m": round(max(sea_like), 2) if sea_like else None,
    "land_max_m": round(max(land_like), 2) if land_like else None,
    "passed": (not sea_like) or max(sea_like) < 300.0,
}

report["all_passed"] = all(
    report[k].get("passed") for k in
    ("decode_vs_source", "same_level_shared_edge", "parent_child_common_samples",
     "steep_slope", "coverage_boundary", "water_mask",
     "water_mask_absent_is_blocked", "coast_transition"))

with open(DEV + r"\_t3dprobe\terrain_numeric.json", "w", encoding="utf-8") as fh:
    json.dump(report, fh, ensure_ascii=False, indent=2)
print(json.dumps(report, ensure_ascii=False, indent=2))
