#!/usr/bin/env python
"""CNS-PLANNER 三维受保护资产校验（R7-A，只读）。

用途：在**任意**恢复出来的工作树上，一次性回答三个问题：

1. 15 个受保护三维文件是否逐字节等于 ``docs/3d-protected-assets.json`` 记录的身份；
2. ``cns_planner/api/router.py`` 的三维路由段是否仍然是同一段（全文件 SHA 允许因
   非三维改动而不同，但三维段必须逐字节一致）；
3. 页面声明的资源依赖（Cesium、三维模块之间的相对 import）是否都存在——恢复不是
   复制 15 个文件就结束，缺一个 vendor 或模块页面就打不开。

可选 ``--probe http://127.0.0.1:8778`` 在服务已启动时追加只读 HTTP 验收（health /
manifest / layer.json / imagery config / routes / 页面与静态资源）。

本脚本只读：不写任何文件、不启动/停止服务、不触碰 8776。仅用标准库，可直接用
系统 Python 或 QGIS Python 运行。
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request

MANIFEST_RELATIVE = Path("docs") / "3d-protected-assets.json"


def sha256_bytes(value):
    return sha256(value).hexdigest()


def read_manifest(root, manifest_path=None):
    path = Path(manifest_path).resolve() if manifest_path else root / MANIFEST_RELATIVE
    if not path.is_file():
        raise SystemExit(f"[FAIL] 缺少保护清单：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def router_section(text, start_marker, method_marker="def _visualization_3d("):
    index = text.find(start_marker)
    if index < 0:
        return None
    method = text.find(method_marker, index)
    if method < 0:
        return None
    end = text.find("\n    def ", method)
    if end < 0:
        return None
    return text[index:end]


def check_files(root, manifest, notes):
    expected = {item["path"]: item for item in manifest["files"]}
    alternates = {}
    for item in manifest.get("alternate_full_sha256") or []:
        alternates.setdefault(item["path"], set()).add(item["sha256"])
    #: router.py 的全文件身份允许是任何一棵已登记树的身份（三维段仍单独校验）。
    router_path = manifest["router"]["path"]
    for identity in manifest["router"].get("full_file_identities") or []:
        alternates.setdefault(router_path, set()).add(identity["sha256"])
    lf_variants = {item["path"]: item["lf_sha256"] for item in manifest.get("line_ending_variants") or []}
    failures = []
    for relative, item in expected.items():
        path = root / relative
        if not path.is_file():
            failures.append(f"[MISS] {relative}")
            continue
        raw = path.read_bytes()
        digest = sha256_bytes(raw)
        if digest == item["sha256"]:
            continue
        if digest in alternates.get(relative, set()):
            notes.append(f"[ALT ] {relative} 命中已登记的同源变体（{digest[:12]}）")
            continue
        if relative in lf_variants and digest == lf_variants[relative]:
            notes.append(f"[EOL ] {relative} 行尾变体（LF），行内容一致")
            continue
        failures.append(
            f"[SHA ] {relative} 期望 {item['sha256'][:12]}… 实际 {digest[:12]}…（{len(raw)} B / 期望 {item['bytes']} B）"
        )
    return failures


def check_router(root, manifest, notes):
    router = manifest["router"]
    path = root / router["path"]
    if not path.is_file():
        return [f"[MISS] {router['path']}"]
    text = path.read_text(encoding="utf-8")
    section = router_section(text, router["section"]["start_marker"])
    if section is None:
        return [f"[FAIL] {router['path']} 找不到三维路由段（白名单/代理被移除？）"]
    digest = sha256_bytes(section.encode("utf-8"))
    if digest != router["section"]["sha256"]:
        return [
            f"[SHA ] {router['path']} 三维路由段变化：期望 {router['section']['sha256'][:12]}…"
            f" 实际 {digest[:12]}…（{len(section.encode('utf-8'))} B）"
        ]
    full = sha256_bytes(path.read_bytes())
    known = {item["sha256"] for item in router["full_file_identities"]}
    if full in known:
        notes.append(f"[OK  ] {router['path']} 全文件命中已登记树身份（{full[:12]}）")
    else:
        notes.append(
            f"[NOTE] {router['path']} 全文件 SHA {full[:12]} 未登记，但三维段一致——"
            "非三维改动不属于本清单范围"
        )
    return []


def check_vendor(root, manifest):
    failures = []
    for item in manifest.get("vendor_files") or []:
        path = root / item["path"]
        if not path.is_file():
            failures.append(f"[MISS] {item['path']}（页面会在无 Cesium 时黑屏）")
            continue
        digest = sha256_bytes(path.read_bytes())
        if digest != item["sha256"]:
            failures.append(f"[SHA ] {item['path']} 期望 {item['sha256'][:12]}… 实际 {digest[:12]}…")
    return failures


def check_runtime_files(root, manifest):
    failures = []
    for relative in manifest.get("runtime_files") or []:
        if not (root / relative).is_file():
            failures.append(f"[MISS] {relative}（启动/静态服务依赖）")
    return failures


IMPORT_PATTERN = re.compile(r"""from\s+['"](\./[^'"]+)['"]|import\(\s*['"](\./[^'"]+)['"]""")


def check_asset_graph(root):
    """页面 → Cesium / 三维模块 → 模块间相对 import 的静态可达性。"""

    failures = []
    web = root / "cns_planner" / "web"
    page = web / "visualization-3d.html"
    if not page.is_file():
        return ["[MISS] cns_planner/web/visualization-3d.html"]
    html = page.read_text(encoding="utf-8")
    for match in re.finditer(r"""(?:src|href)="(\./[^"]+)\"""", html):
        target = (web / match.group(1)[2:]).resolve()
        if not target.is_file():
            failures.append(f"[MISS] visualization-3d.html → {match.group(1)}")
    modules = sorted((web / "js" / "visualization3d").glob("*.js"))
    if not modules:
        return ["[MISS] cns_planner/web/js/visualization3d/*.js 全部缺失"]
    for module in modules:
        text = module.read_text(encoding="utf-8")
        for match in IMPORT_PATTERN.finditer(text):
            relative = match.group(1) or match.group(2)
            target = (module.parent / relative).resolve()
            if not target.is_file():
                failures.append(f"[MISS] {module.name} → {relative}")
    return failures


def http_get(url, timeout=15.0):
    request = urllib.request.Request(url, headers={"Accept": "*/*"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:  # 4xx/5xx 也要算作"有响应"
        return exc.code, exc.read()
    except (urllib.error.URLError, OSError) as exc:
        return None, str(exc).encode("utf-8", "replace")


def probe(base_url, manifest, notes):
    base = base_url.rstrip("/")
    failures = []
    for item in manifest.get("probes") or []:
        path = item["path"]
        status, body = http_get(base + path)
        if status is None:
            failures.append(f"[HTTP] {path} 无法连接：{body.decode('utf-8', 'replace')[:120]}")
            continue
        expect_status = int(item.get("expect_status") or 200)
        if status != expect_status:
            failures.append(f"[HTTP] {path} 期望 {expect_status} 实际 {status}")
            continue
        expect = item.get("expect") or {}
        if expect:
            try:
                payload = json.loads(body.decode("utf-8"))
            except ValueError:
                failures.append(f"[JSON] {path} 返回不是 JSON")
                continue
            for key, value in expect.items():
                if payload.get(key) != value:
                    failures.append(
                        f"[JSON] {path} {key}={payload.get(key)!r} 期望 {value!r}"
                    )
        notes.append(f"[HTTP] {path} {status} ok")
    status, body = http_get(base + "/api/visualization-3d/manifest")
    if status == 200:
        try:
            payload = json.loads(body.decode("utf-8"))
            layers = [item.get("layer_id") or item.get("id") for item in payload.get("layers") or []]
            missing = [name for name in manifest["manifest_layers"] if name not in layers]
            if missing:
                failures.append(f"[JSON] manifest 缺少图层：{', '.join(missing)}")
            else:
                notes.append(f"[HTTP] manifest 13 个图层齐全（{len(layers)}）")
        except ValueError:
            failures.append("[JSON] manifest 不是 JSON")
    return failures


def main(argv=None):
    parser = argparse.ArgumentParser(description="CNS-PLANNER 三维受保护资产校验（只读）")
    parser.add_argument("--root", default=None, help="恢复后工作树根目录，默认本脚本上一级")
    parser.add_argument("--manifest", default=None, help="可选的保护清单路径（默认 <root>/docs/3d-protected-assets.json）")
    parser.add_argument("--probe", default=None, help="可选：已启动服务的基址，如 http://127.0.0.1:8778")
    parser.add_argument("--quiet", action="store_true", help="只输出失败项")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parents[1]
    manifest = read_manifest(root, args.manifest)
    notes, failures = [], []
    failures += check_files(root, manifest, notes)
    failures += check_router(root, manifest, notes)
    failures += check_vendor(root, manifest)
    failures += check_runtime_files(root, manifest)
    failures += check_asset_graph(root)
    if args.probe:
        failures += probe(args.probe, manifest, notes)

    if not args.quiet:
        print(f"工作树：{root}")
        print(f"保护文件：{manifest['file_count']}（清单 {MANIFEST_RELATIVE}）")
        print(f"三维路由段：{manifest['router']['section']['bytes']} B / "
              f"{manifest['router']['section']['lines']} 行 / {manifest['router']['section']['sha256'][:16]}…")
        for note in notes:
            print(f"  {note}")
    if failures:
        print(f"\n[FAIL] 共 {len(failures)} 项：")
        for item in failures:
            print(f"  {item}")
        return 1
    print("\n[PASS] 三维受保护资产完整：15 个文件 + 三维路由段 + Cesium 依赖 + 资源图可达")
    return 0


if __name__ == "__main__":
    sys.exit(main())
