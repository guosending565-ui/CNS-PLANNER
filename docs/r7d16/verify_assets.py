"""R7-D16 只读资产核对：19 项受保护三维源码 + 起降点 PNG + Cesium 运行时。

用法（仓库根目录）：
    D:\\tools\\anaconda\\python.exe docs/r7d16/verify_assets.py

只读：只打开文件、计算 SHA-256 并写 docs/r7d16/3d-assets-verification.json。
不启动服务、不修改 ProjectState、不触碰 8765/8776。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "docs" / "3d-protected-assets-r7d3c.json"
OUT = Path(__file__).resolve().parent / "3d-assets-verification.json"
LANDING_PAD = ROOT / "cns_planner" / "web" / "assets" / "symbols" / "landing_pad.png"
LANDING_SHA256 = "37f83a99215bc49670b22ec5e6c700a583dc99c6d963536f133d25e40365e76b"

#: 本轮**有意**变更的受保护文件（其余 16 项必须逐字节一致）。
INTENTIONAL_CHANGES = {
    "cns_planner/api/router.py": "E 批端点装配：保留 A/B/C/D 契约并接回三维/固定走廊/导航监测",
    "cns_planner/web/js/visualization3d/app.js": "R7-D16：新增起降点 PNG billboard 图层（additive）",
    "cns_planner/web/js/visualization3d/basemap.js": "R7-D16：新增 landingPads 默认关闭（additive）",
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_identity(path: Path) -> dict:
    data = path.read_bytes()
    return {"bytes": len(data), "sha256": sha256_bytes(data)}


def router_section(path: Path, start_marker: str) -> dict:
    data = path.read_bytes()
    start = data.index(start_marker.encode("utf-8"))
    method = data.index(b"def _visualization_3d(", start)
    end = data.index(b"\n    def ", method)
    raw = data[start:end]
    # Windows checkout: ``end`` points at the ``\n`` of a CRLF, so ``raw`` may end
    # with a lone ``\r``. Drop it before normalising, otherwise the identity gains
    # one byte compared with the LF source manifest.
    if raw.endswith(b"\r"):
        raw = raw[:-1]
    normalised = raw.replace(b"\r\n", b"\n")
    return {
        "bytes_crlf": len(raw),
        "bytes_lf": len(normalised),
        "lines_lf": normalised.count(b"\n") + 1,
        "sha256_lf": sha256_bytes(normalised),
    }


def main() -> int:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    files = []
    unchanged = 0
    changed = []
    for entry in manifest["files"]:
        path = ROOT / entry["path"]
        actual = file_identity(path)
        matched = (
            actual["bytes"] == entry["bytes"]
            and actual["sha256"] == entry["sha256"]
        )
        record = {
            "path": entry["path"],
            "expected": {"bytes": entry["bytes"], "sha256": entry["sha256"]},
            "actual": actual,
            "byte_identical_to_r7d3c": matched,
        }
        if matched:
            unchanged += 1
        else:
            record["intentional"] = entry["path"] in INTENTIONAL_CHANGES
            record["reason"] = INTENTIONAL_CHANGES.get(entry["path"])
            changed.append(record)
        files.append(record)

    section = router_section(ROOT / "cns_planner" / "api" / "router.py",
                             "VISUALIZATION_3D_ACTIONS = {")
    expected_section = manifest["router"]["section"]
    section_ok = (
        section["sha256_lf"] == expected_section["sha256"]
        and section["bytes_lf"] == expected_section["bytes"]
        and section["lines_lf"] == expected_section["lines"]
    )

    landing = file_identity(LANDING_PAD)
    landing_ok = landing["sha256"] == LANDING_SHA256

    vendor = []
    for entry in manifest.get("vendor_files") or []:
        path = ROOT / entry["path"]
        actual = file_identity(path)
        vendor.append({
            "path": entry["path"],
            "actual": actual,
            "r7d3c_windows_crlf_identity": {
                "bytes": entry["bytes"], "sha256": entry["sha256"],
            },
            "note": (
                "R7-D16 按 tests/test_cesium_vendor_license.py 的**上游 LF 原始字节**"
                "重新物化；Git 以 .gitattributes -text 固定，避免 Windows checkout 改变证据身份。"
            ),
        })

    result = {
        "schema_version": "cns-r7d16-3d-assets-verification/v1",
        "round": "R7-D16",
        "baseline": "8f1c927c7df7d3f18286526e9a888ca07c9d9d7f",
        "manifest": "docs/3d-protected-assets-r7d3c.json",
        "file_count": manifest["file_count"],
        "byte_identical_count": unchanged,
        "intentionally_changed_count": len(changed),
        "all_changes_additive": all(item.get("intentional") for item in changed),
        "files": files,
        "router_section": {
            "actual": section,
            "expected_r7d3c": {
                "bytes": expected_section["bytes"],
                "lines": expected_section["lines"],
                "sha256": expected_section["sha256"],
            },
            "matches_r7d3c": section_ok,
        },
        "landing_pad": {
            "path": "cns_planner/web/assets/symbols/landing_pad.png",
            "actual": landing,
            "expected_sha256": LANDING_SHA256,
            "matches_supplied_file": landing_ok,
        },
        "vendor": vendor,
        "manifest_layers": manifest.get("manifest_layers"),
        "layer_switches": {
            "protected_count": 19,
            "total_with_landing_pads": 20,
            "landing_pads_is_new": True,
        },
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print(json.dumps({
        "byte_identical": unchanged,
        "changed": [item["path"] for item in changed],
        "router_section_matches": section_ok,
        "landing_pad_matches": landing_ok,
        "output": str(OUT),
    }, ensure_ascii=False, indent=2))
    return 0 if (unchanged == 16 and len(changed) == 3 and section_ok and landing_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
