"""定位冻结记录中的权威状态（sha256 前缀 92a0000f，286,829,234 B）。

CONTROL.md 明确警告：主仓库与 integration 的 ``projects/current_project.json`` 是
**revision 21 的空项目**（``e8548f28…``），不是权威状态。因此必须按**指纹**定位，
不能按路径猜测。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

TARGET_PREFIX = "92a0000f"
TARGET_BYTES = 286829234

ROOTS = [
    Path(r"D:\Projects\_cns_workspaces"),
    Path(r"D:\Projects"),
]
SKIP = {"_diag", "_diag2", "_out", "_output", "_work", "outputs", "node_modules",
        ".git", "__pycache__"}


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 22), b""):
            digest.update(block)
    return digest.hexdigest()


found = []
size_matches = []
seen = set()
for root in ROOTS:
    if not root.exists():
        continue
    for path in root.rglob("current_project.json"):
        if any(part in SKIP for part in path.parts):
            continue
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size == TARGET_BYTES:
            size_matches.append(path)
            digest = sha256_of(path)
            if digest.startswith(TARGET_PREFIX):
                found.append((path, size, digest))

print("candidates with exact byte size %d: %d" % (TARGET_BYTES, len(size_matches)))
for path in size_matches:
    digest = sha256_of(path)
    marker = "  <== MATCH" if digest.startswith(TARGET_PREFIX) else ""
    print("  %s  %s%s" % (digest[:16], path, marker))

print()
if found:
    print("AUTHORITATIVE STATE FOUND:")
    for path, size, digest in found:
        print("  path   : %s" % path)
        print("  bytes  : %d" % size)
        print("  sha256 : %s" % digest)
else:
    print("NOT FOUND: no current_project.json matches %s / %d B"
          % (TARGET_PREFIX, TARGET_BYTES))
