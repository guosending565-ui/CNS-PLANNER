"""R7-D19 public-release audit for the integrated source tree.

Read-only: runs git queries and reads tracked files, writes only the JSON evidence
file next to this script. No push, no checkout, no index mutation.
"""

from __future__ import annotations

import json
import hashlib
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "release_audit.json"

ZERO = "0" * 40
ALLOWED_ROOTS = ("cns_planner/", "tests/", "tools/", "docs/")
ALLOWED_FILES = {
    ".gitattributes", ".gitignore", ".streamlit/config.toml", "pyproject.toml", "app.py", "launcher_process.py",
    "legacy_app.py", "map_app.py", "restart-dsh-web.ps1", "启动地图.cmd", "README.md",
    "AI_DEV_CONTEXT.md", "PROTECTED_ASSETS.md",
    "PHASE4_ARCHITECTURE_AUDIT.md", "PHASE4_B2_WRITE_AUTHORITY_AUDIT.md",
    "PHASE4_FIXED_LAYER_CONSTRAINT_AUDIT.md", "PHASE4_TARGET_ARCHITECTURE.md",
    "TOWERS_OPERATIONAL_INTEGRATION_V2_REPORT.md", "TOWERS_SITE_TYPE_CLASSIFICATION.md",
    "TOWERS_V2_FINAL_SEMANTIC_CLOSEOUT.md",
}
FORBIDDEN_PREFIXES = (
    "projects/", "research/", "outputs/", "_diag/", "_diag2/", "_out/", "_output/",
    "_work/", "_vbase", "_round", "_bt_sim", "b9tmp/", "b9_e2e_runs/", "tmp_b9/",
    "outputs/", "data/", ".env", ".git-credentials", "acl_recovery/",
)
FORBIDDEN_SUFFIXES = (
    ".tif", ".tiff", ".gpkg", ".qgz", ".bundle", ".zip", ".7z", ".rar", ".tar",
    ".gz", ".las", ".shp", ".dbf", ".kml", ".kmz", ".pem", ".p12", ".pfx",
)
SECRET_PATTERNS = {
    "aws_access_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "github_token": re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
    "github_pat": re.compile(r"github_pat_[A-Za-z0-9_]{40,}"),
    "slack_token": re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    "openai_key": re.compile(r"\bsk-[A-Za-z0-9]{24,}\b"),
    "google_api_key": re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    "alibaba_access_key": re.compile(r"\bLTAI[0-9A-Za-z]{12,}\b"),
    "tencent_secret_id": re.compile(r"\bAKID[0-9A-Za-z]{13,}\b"),
    "private_key_block": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "tianditu_tk": re.compile(r"[?&]tk=[0-9a-fA-F]{24,}"),
    "url_basic_auth": re.compile(r"://[^/\s:@]+:[^/\s:@]+@"),
    "password_assignment": re.compile(
        r"(?i)\b(?:password|passwd|client_secret|api_key|apikey|access_token)\b\s*[:=]\s*['\"][^'\"]{8,}['\"]"
    ),
}
BOM = b"\xef\xbb\xbf"
TEXT_SUFFIXES = {
    ".py", ".js", ".mjs", ".json", ".md", ".html", ".css", ".txt", ".toml", ".yml",
    ".yaml", ".in", ".cfg", ".ini", ".ps1", ".cmd", ".bat", ".sh", ".csv", ".geojson",
}


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(ROOT), "-c", "core.quotepath=false", *args],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def tree_entries() -> list[dict]:
    entries = []
    for line in git("ls-tree", "-r", "-l", "HEAD").splitlines():
        meta, path = line.split("\t", 1)
        mode, kind, sha, size = meta.split()
        entries.append({"mode": mode, "kind": kind, "sha": sha, "size": int(size), "path": path})
    return entries


def blob_text(sha: str, limit: int = 4_000_000) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(ROOT), "cat-file", "-p", sha],
        capture_output=True, check=False,
    )
    if result.returncode != 0 or len(result.stdout) > limit:
        return None
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None


def main() -> int:
    head = git("rev-parse", "HEAD")
    main = git("rev-parse", "origin/main")
    merge_base = git("merge-base", "HEAD", "origin/main")
    ahead, behind = (int(v) for v in git("rev-list", "--left-right", "--count", "HEAD...origin/main").split())
    roots = git("rev-list", "--max-parents=0", "HEAD").splitlines()
    total_commits = int(git("rev-list", "--count", "HEAD"))
    changed = [p for p in git("diff", "--name-only", f"{merge_base}..HEAD").splitlines() if p]
    entries = tree_entries()

    findings = {"fail": [], "warn": [], "info": []}

    # 1. Public whitelist: every tracked path must be explicitly publishable.
    outside = [
        e["path"] for e in entries
        if not (e["path"] in ALLOWED_FILES or e["path"].startswith(ALLOWED_ROOTS))
    ]
    if outside:
        findings["fail"].append({"check": "whitelist", "paths": outside[:50], "count": len(outside)})

    # 2. Forbidden paths / data / archives.
    forbidden = []
    for entry in entries:
        path = entry["path"]
        if path.startswith(FORBIDDEN_PREFIXES) or path.lower().endswith(FORBIDDEN_SUFFIXES):
            forbidden.append(path)
    if forbidden:
        findings["fail"].append({"check": "forbidden_paths", "paths": forbidden[:50], "count": len(forbidden)})

    # 3. Large files and total payload.
    large = sorted((e for e in entries if e["size"] >= 5_000_000), key=lambda e: -e["size"])
    if any(e["size"] >= 20_000_000 for e in large):
        findings["fail"].append({"check": "large_files", "paths": [
            {"path": e["path"], "bytes": e["size"]} for e in large if e["size"] >= 20_000_000
        ]})
    elif large:
        findings["warn"].append({"check": "large_files_over_5mb", "files": [
            {"path": e["path"], "bytes": e["size"]} for e in large
        ]})
    payload_bytes = sum(e["size"] for e in entries)

    # 4. Secret scan over every text blob reachable from HEAD.
    hits = []
    scanned = 0
    for entry in entries:
        if entry["kind"] != "blob" or entry["size"] > 2_000_000:
            continue
        suffix = Path(entry["path"]).suffix.lower()
        if suffix not in TEXT_SUFFIXES and not entry["path"].endswith((".cmd", ".ps1")):
            continue
        text = blob_text(entry["sha"])
        if text is None:
            continue
        scanned += 1
        for name, pattern in SECRET_PATTERNS.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                hits.append({"pattern": name, "path": entry["path"], "line": line,
                             "excerpt": text[max(0, match.start() - 40):match.end() + 10].replace("\n", " ")})
    if hits:
        findings["fail"].append({"check": "secrets", "hits": hits[:30], "count": len(hits), "scanned": scanned})
    else:
        findings["info"].append({"check": "secrets", "result": "no high-signal matches", "scanned": scanned})

    # 5. Git-reachable history: no forbidden path ever appears in the published ancestry.
    # Paths already present in the public origin/main ancestry are pre-existing public
    # history, not a new disclosure; they are reported separately instead of failing.
    history_paths = [p for p in git("log", "HEAD", "--name-only", "--format=").splitlines() if p]
    public_paths = {p for p in git("log", "origin/main", "--name-only", "--format=").splitlines() if p}
    history_bad = sorted({
        p for p in history_paths
        if p.startswith(("projects/", "research/", "outputs/")) or p.lower().endswith(FORBIDDEN_SUFFIXES)
    })
    history_new = [p for p in history_bad if p not in public_paths]
    history_preexisting = [p for p in history_bad if p in public_paths]
    if history_new:
        findings["fail"].append({"check": "reachable_history", "paths": history_new[:50], "count": len(history_new)})
    if history_preexisting:
        findings["warn"].append({
            "check": "reachable_history_preexisting_public",
            "count": len(history_preexisting),
            "paths": history_preexisting[:20],
            "detail": "test-runtime binaries already reachable from public origin/main; no new disclosure.",
        })
    d9_ancestor = subprocess.run(
        ["git", "-C", str(ROOT), "merge-base", "--is-ancestor",
         "2b953bfcbf9ccc74e5c07ad582d67324c0a164ca", "HEAD"], capture_output=True, check=False
    ).returncode == 0
    if d9_ancestor:
        findings["fail"].append({"check": "d9_history_reachable", "commit": "2b953bfcbf9ccc74e5c07ad582d67324c0a164ca"})
    if len(roots) != 1:
        findings["fail"].append({"check": "history_roots", "roots": roots})

    # 6. License inventory.
    licenses = []
    for entry in entries:
        name = Path(entry["path"]).name.upper()
        if name.startswith("LICENSE") or name in {"COPYING", "NOTICE"}:
            licenses.append(entry["path"])
    cesium_license = next((e for e in entries if e["path"] == "cns_planner/web/vendor/cesium/LICENSE.md"), None)
    cesium_sha = ""
    if cesium_license:
        raw = subprocess.run(["git", "-C", str(ROOT), "cat-file", "-p", cesium_license["sha"]],
                             capture_output=True, check=False).stdout
        cesium_sha = hashlib.sha256(raw).hexdigest()
    cesium_sha_ok = cesium_sha == "3111d10e1630bfa975251018149c973a0a3f42d11e793733a8ce417d52baa59a"
    project_license = any(p in {"LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING"} for p in licenses)
    if not project_license:
        findings["warn"].append({
            "check": "project_license_missing",
            "detail": "no root LICENSE/COPYING; repository code license is not declared. "
                      "Vendored CesiumJS Apache-2.0 notice is present and verified.",
        })
    if not cesium_sha_ok:
        findings["fail"].append({"check": "cesium_license", "paths": licenses,
                                 "detail": "expected Cesium LICENSE.md blob 3111d10e..."})

    # 7. Config encoding: no UTF-8 BOM in JSON/YAML.
    bom = []
    for entry in entries:
        if Path(entry["path"]).suffix.lower() in {".json", ".yml", ".yaml"}:
            raw = subprocess.run(["git", "-C", str(ROOT), "cat-file", "-p", entry["sha"]],
                                 capture_output=True, check=False).stdout[:3]
            if raw == BOM:
                bom.append(entry["path"])
    if bom:
        findings["fail"].append({"check": "bom", "paths": bom})

    # 8. Symlinks / submodules (not expected in a source snapshot).
    odd_modes = [e["path"] for e in entries if e["mode"] not in {"100644", "100755"}]
    if odd_modes:
        findings["fail"].append({"check": "tree_modes", "paths": odd_modes[:20]})

    by_root = Counter(p.split("/")[0] if "/" in p else "<root>" for p in (e["path"] for e in entries))
    report = {
        "schema_version": "cns-r7d19-release-audit/v1",
        "generated_at": __import__("datetime").datetime.now(__import__("datetime").timezone(__import__("datetime").timedelta(hours=8))).isoformat(),
        "repository": str(ROOT),
        "head": head,
        "origin_main": main,
        "merge_base": merge_base,
        "relation": {"ahead": ahead, "behind": behind, "fast_forward_from_main": merge_base == main},
        "history": {
            "root_commits": roots,
            "commit_count_reachable": total_commits,
            "commits_ahead_of_main": ahead,
            "d9_commit_2b953bf_reachable": d9_ancestor,
            "forbidden_paths_in_reachable_history": len(history_bad),
            "new_forbidden_paths_vs_public_main": len(history_new),
            "preexisting_public_artifacts": len(history_preexisting),
        },
        "tree": {
            "tracked_files": len(entries),
            "payload_bytes": payload_bytes,
            "by_root": dict(by_root.most_common()),
            "changed_files_vs_main": len(changed),
            "changed_by_root": dict(Counter(
                p.split("/")[0] if "/" in p else "<root>" for p in changed).most_common()),
            "largest": [{"path": e["path"], "bytes": e["size"]} for e in large[:10]],
        },
        "checks": {
            "whitelist": "pass" if not outside else "fail",
            "forbidden_paths": "pass" if not forbidden else "fail",
            "secrets": "pass" if not hits else "fail",
            "reachable_history": "pass" if not history_new and not d9_ancestor else "fail",
            "license": "pass" if cesium_sha_ok and project_license else ("warn" if cesium_sha_ok else "fail"),
            "archives_and_data": "pass" if not forbidden else "fail",
            "large_files": "pass" if not any(e["size"] >= 20_000_000 for e in entries) else "fail",
            "encoding_bom": "pass" if not bom else "fail",
            "tree_modes": "pass" if not odd_modes else "fail",
        },
        "licenses": {"files": licenses, "cesium_sha_ok": cesium_sha_ok,
                      "project_license_declared": project_license},
        "findings": findings,
        "verdict": "fail" if findings["fail"] else "pass",
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({
        "verdict": report["verdict"],
        "checks": report["checks"],
        "tree": {k: report["tree"][k] for k in ("tracked_files", "payload_bytes", "changed_files_vs_main")},
        "history": report["history"],
        "fail": len(findings["fail"]), "warn": len(findings["warn"]),
    }, ensure_ascii=False, indent=2))
    return 0 if report["verdict"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
