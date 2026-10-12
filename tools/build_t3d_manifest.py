"""生成 Round37-T3D-BMAP 交付 manifest（SHA256 清单）。

不复制任何商业瓦片、DEM、geoid、buildings、工程状态、缓存或凭证；
只对交付目录内的**报告与证据**计算指纹。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

DEL = Path(r"D:\Projects\_cns_workspaces\deliverables\round37_t3d_basemap")

#: 明确排除的类别（即使误入交付目录也不列入 manifest）
EXCLUDE_SUFFIX = {".tif", ".gpkg", ".qgz", ".zip", ".bak"}
EXCLUDE_NAME_HINTS = ("token", "secret", "credential", "key")


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


entries = []
excluded = []
for path in sorted(DEL.rglob("*")):
    if not path.is_file():
        continue
    rel = path.relative_to(DEL).as_posix()
    if rel == "MANIFEST.sha256.json":
        continue
    lowered = rel.lower()
    if (path.suffix.lower() in EXCLUDE_SUFFIX
            or any(hint in lowered for hint in EXCLUDE_NAME_HINTS)):
        excluded.append(rel)
        continue
    entries.append({
        "path": rel,
        "bytes": path.stat().st_size,
        "sha256": sha256_of(path),
    })

manifest = {
    "schema": "round37-t3d-basemap-manifest/1",
    "deliverable": "Round37-T3D-BMAP",
    "delivery_dir": str(DEL),
    "file_count": len(entries),
    "total_bytes": sum(e["bytes"] for e in entries),
    "excluded_large_or_secret": excluded,
    "note": (
        "只包含报告与证据；不含商业瓦片、DEM、geoid、buildings、工程状态、"
        "缓存或凭证"),
    "files": entries,
}

out = DEL / "MANIFEST.sha256.json"
out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
               encoding="utf-8")

print("files      : %d" % manifest["file_count"])
print("total      : %.2f MiB" % (manifest["total_bytes"] / 1048576))
print("excluded   : %s" % (excluded or "none"))
print("manifest   : %s" % out)
print()
for entry in entries:
    print("  %-52s %8d  %s" % (entry["path"], entry["bytes"], entry["sha256"][:12]))
