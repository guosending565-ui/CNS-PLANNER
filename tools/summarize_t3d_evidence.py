"""汇总 Round37-T3D-BMAP 全部证据，生成交付报告所需的数值表。"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

DEL = Path(r"D:\Projects\_cns_workspaces\deliverables\round37_t3d_basemap")
DEV = Path(r"D:\Projects\_cns_workspaces\dev\round37_b0\CNS-PLANNER")

acc = json.loads((DEL / "evidence/acceptance/acceptance.json").read_text(encoding="utf-8"))
ba = json.loads((DEL / "evidence/before_after/before_after.json").read_text(encoding="utf-8"))
inv = json.loads((DEV / "_t3dprobe/invariance.json").read_text(encoding="utf-8"))

summary = {}
summary["ready_seconds"] = acc["ready_seconds"]
summary["moving_fps"] = acc["performance"]["moving_fps"]
summary["idle_fps"] = acc["performance"]["idle_fps"]
summary["gpu"] = acc["gpu"]["renderer"]
summary["dom_content_loaded_ms"] = acc["performance"]["dom_content_loaded_ms"]
summary["load_event_ms"] = acc["performance"]["load_event_ms"]
summary["resource_count"] = acc["performance"]["resource_count"]
summary["transfer_bytes"] = acc["performance"]["transfer_bytes"]
summary["transfer_mib"] = round(acc["performance"]["transfer_bytes"] / 1048576, 2)

req = acc["requests"]
summary["requests_total"] = req["count"]
summary["non_get"] = req["non_get_count"]
summary["terrain_tiles"] = req["terrain_tile_requests"]
summary["imagery_tiles"] = req["imagery_tile_requests"]
summary["workflow_requests"] = req["workflow_requests"]
summary["failed_requests"] = len(req["failed_requests"])
summary["console_errors"] = len(acc["console_errors"])

# 按路径分类请求
buckets = Counter()
for url in req["urls"]:
    path = urlsplit(url).path
    if "/relief/" in path:
        buckets["relief"] += 1
    elif path.endswith(".terrain"):
        buckets["terrain"] += 1
    elif path.startswith("/api/visualization-3d/"):
        buckets["api_other"] += 1
    elif "/vendor/" in path:
        buckets["vendor"] += 1
    else:
        buckets["page_assets"] += 1
summary["requests_by_kind"] = dict(buckets)

summary["scene"] = acc["scene"]
summary["imagery_status"] = acc["manifest_imagery"]
summary["terrain_tiles_meta"] = acc["manifest_terrain_tiles"]
summary["basemap_status"] = acc["basemap_status"]
summary["checks"] = acc["summary"]["checks"]
summary["counts"] = acc["summary"]["counts"]
summary["revision"] = acc["summary"]["revision"]
summary["scope"] = acc["summary"]["scope"]

summary["before_after_camera_matched"] = ba["camera_matched_exactly"]
summary["before_after_camera_attempts"] = ba["camera_attempts"]
summary["before_after_state_unchanged"] = ba["state_unchanged"]

summary["invariance"] = {
    name: {"match": row["match"], "bytes": row["bytes"],
           "sha256": (row["sha256"] or "")[:16]}
    for name, row in inv["targets"].items()
}
summary["geoid_match"] = inv["egm2008_geoid"].get("match")
summary["no_new_state_writer"] = inv["no_new_state_writer"]
summary["baseline_revision"] = inv.get("baseline_state_revision")

# 六视角
summary["views"] = [
    {"slug": v["slug"], "label": v["label"], "applied": v["applied"],
     "bytes": v["screenshot"]["bytes"],
     "height_m": round(v["camera"]["height"], 1),
     "pitch_deg": round(v["camera"]["pitch"] * 180 / 3.141592653589793, 1)}
    for v in acc["views"]
]

(DEL / "evidence" / "summary.json").write_text(
    json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

for key in ("ready_seconds", "moving_fps", "idle_fps", "requests_total", "non_get",
            "terrain_tiles", "imagery_tiles", "workflow_requests", "failed_requests",
            "console_errors", "transfer_mib", "revision"):
    print("%-22s %s" % (key, summary[key]))
print("requests_by_kind     ", summary["requests_by_kind"])
print("scene                ", json.dumps(summary["scene"], ensure_ascii=False))
print("invariance           ", json.dumps(summary["invariance"], ensure_ascii=False))
print("geoid_match          ", summary["geoid_match"])
print("no_new_state_writer  ", summary["no_new_state_writer"])
