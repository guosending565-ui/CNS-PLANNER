"""Round37-T3D-BMAP 不变性核对：权威源、状态与工作树写入面。

只读：不写任何状态、不 commit、不 clean。核对项：

* FABDEM DTM、buildings v3、EGM2008 geoid、权威 project_state.json 的 SHA256；
* 权威状态的 revision / mtime；
* 确认本轮**没有**新增 ProjectState 写入者（只检查 3D 相关模块不 import 写入面）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

DEV = Path(r"D:\Projects\_cns_workspaces\dev\round37_b0\CNS-PLANNER")

#: 本轮工程状态的**基线**。开发树的 ``projects`` 是指向 B0 验收副本的 junction，
#: 其 ``current_project.json`` 即本轮唯一工作状态（revision 468，`51432a8b…`）。
#:
#: 为什么不用 CONTROL.md 里的 `92a0000f…` / 286,829,234 B：那是**历史 revision 447**
#: 的权威状态记录，已不在盘上（本轮实测 0 个候选匹配该字节数）。T3D-1 之后工程状态
#: 由 B0 验收副本承载，因此不变性必须针对**当前基线**核对，而不是引用一个已不存在的
#: 指纹去制造"MATCH"的假象。
BASELINE_STATE = DEV / "projects" / "current_project.json"

TARGETS = {
    "fabdem_dtm": Path(
        r"D:\aaa2026project\UOM\舟山\规划系统\FABDEM\processed"
        r"\Zhoushan_FABDEM_DTM_30m.tif"),
    "buildings_v3": Path(
        r"D:\aaa2026project\UOM\舟山\规划系统\building\processed"
        r"\zhoushan_buildings_v3.gpkg"),
}

#: 冻结记录（CONTROL.md）中的期望指纹前缀。
EXPECTED_PREFIX = {
    "fabdem_dtm": "21406bd9",
    "buildings_v3": "4f165749",
    "baseline_state": "51432a8b",
    "egm2008_geoid": "4191d471",
}
EXPECTED_BYTES = {"baseline_state": 287890185}

#: EGM2008 geoid 由 ``cns_planner/gis/egm2008_geoid.py`` 的 DEFAULT_GEOID_DIR 决定。
GEOID_PATH = Path(
    r"D:\aaa2026project\UOM\舟山\规划系统\geoid\us_nga_egm08_25.tif")


def sha256_of(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 22), b""):
            digest.update(block)
    return digest.hexdigest()


result = {"schema": "round37-t3d-bmap-invariance/1", "targets": {}}

for name, path in TARGETS.items():
    digest = sha256_of(path)
    stat = path.stat() if path.exists() else None
    result["targets"][name] = {
        "path": str(path),
        "exists": path.exists(),
        "bytes": stat.st_size if stat else None,
        "mtime": (stat.st_mtime if stat else None),
        "sha256": digest,
        "expected_prefix": EXPECTED_PREFIX.get(name),
        "match": bool(digest and digest.startswith(EXPECTED_PREFIX.get(name, ""))),
    }

# 基线工程状态单独核对（必须同时匹配 sha256 与字节数）
auth = BASELINE_STATE
auth_digest = sha256_of(auth)
auth_stat = auth.stat() if auth.exists() else None
result["targets"]["baseline_state"] = {
    "path": str(auth),
    "exists": auth.exists(),
    "bytes": auth_stat.st_size if auth_stat else None,
    "mtime": auth_stat.st_mtime if auth_stat else None,
    "sha256": auth_digest,
    "expected_prefix": EXPECTED_PREFIX["baseline_state"],
    "expected_bytes": EXPECTED_BYTES["baseline_state"],
    "match": bool(
        auth_digest
        and auth_digest.startswith(EXPECTED_PREFIX["baseline_state"])
        and auth_stat is not None
        and auth_stat.st_size == EXPECTED_BYTES["baseline_state"]),
    "note": (
        "本轮唯一工程状态（B0 验收副本，经 junction 复用）；"
        "T3D-1 记录的历史 92a0000f/286,829,234 B 已不在盘上，不作为基线"),
}

# geoid：记录指纹（只读，不复制、不改写）
geoid = {}
if GEOID_PATH.exists():
    geoid = {
        "path": str(GEOID_PATH), "bytes": GEOID_PATH.stat().st_size,
        "sha256": sha256_of(GEOID_PATH),
        "expected_prefix": EXPECTED_PREFIX["egm2008_geoid"],
        "match": bool((sha256_of(GEOID_PATH) or "").startswith(
            EXPECTED_PREFIX["egm2008_geoid"])),
    }
result["egm2008_geoid"] = geoid or {"found": False}

# 基线状态 revision
state_path = BASELINE_STATE
if state_path.exists():
    try:
        with state_path.open(encoding="utf-8") as handle:
            state = json.load(handle)
        result["baseline_state_revision"] = state.get("revision")
    except Exception as exc:
        result["baseline_state_revision"] = f"unreadable: {exc}"

# 写入面检查：3D 模块不得 import 任何 ProjectState 写入者
FORBIDDEN = re.compile(
    r"(invalidation_service|workflow_service\.|save_state|write_state|"
    r"mutate_state|ProjectState\()")
scanned = {}
for module in sorted((DEV / "cns_planner").rglob("*.py")):
    rel = module.relative_to(DEV).as_posix()
    if not any(token in rel for token in (
            "visualization_3d", "terrain_tile_adapter", "terrain_window_adapter",
            "water_mask_adapter", "egm2008_geoid")):
        continue
    text = module.read_text(encoding="utf-8", errors="replace")
    hits = sorted({m.group(0) for m in FORBIDDEN.finditer(text)})
    scanned[rel] = {"state_writer_imports": hits, "clean": not hits}
result["state_writer_scan"] = scanned
# 空真防线：一个模块都没扫到时 ``all(...)`` 会返回 True，等于"没有证据就说通过"。
# 白名单失配（例如模块被改名）必须表现为 False，而不是静默通过。
result["state_writer_scan_modules"] = len(scanned)
result["no_new_state_writer"] = bool(scanned) and all(
    row["clean"] for row in scanned.values())

out = DEV / "_t3dprobe" / "invariance.json"
out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

for name, row in result["targets"].items():
    print("%-22s match=%-5s bytes=%-12s %s" % (
        name, row["match"], row["bytes"],
        (row["sha256"] or "MISSING")[:16]))
print("geoid:", (result["egm2008_geoid"].get("sha256") or "not found")[:16],
      "match=%s" % result["egm2008_geoid"].get("match"))
print("baseline state revision:", result.get("baseline_state_revision"))
print("state_writer_scan_modules:", result["state_writer_scan_modules"])
print("no_new_state_writer:", result["no_new_state_writer"])
# 同样加非空守卫：targets 为空时不得报"全部匹配"。
print("ALL_TARGETS_MATCH:", bool(result["targets"]) and all(
    row["match"] for row in result["targets"].values()) and bool(
        result["egm2008_geoid"].get("match")))
