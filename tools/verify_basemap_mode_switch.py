"""定向复验：三维底图模式开关（离线开发 / 在线演示）。

只读探测 + **仅**切换底图模式；不写 ProjectState、不发起任何业务 POST、
不重算任何规划结果。所有证据写入 ``--output`` 指定目录。

用法：
    python tools/verify_basemap_mode_switch.py http://127.0.0.1:8765 <state_path> <out_dir>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


VIEWPORT = {"width": 1600, "height": 900}


def redact_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def verify(base_url: str, state_path: Path, output_dir: Path) -> dict[str, object]:
    from playwright.sync_api import sync_playwright

    output_dir.mkdir(parents=True, exist_ok=True)
    state_before = sha256_of(state_path)
    requests: list[dict[str, str]] = []
    console_errors: list[str] = []
    report: dict[str, object] = {
        "schema": "round38-e2e-precheck-basemap-mode/1",
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "base_url": redact_url(base_url),
        "viewport": VIEWPORT,
        "state_sha256_before": state_before,
    }

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport=VIEWPORT, device_scale_factor=1)
        page = context.new_page()
        page.on("request", lambda r: requests.append(
            {"method": r.method, "url": r.url}))
        page.on("console", lambda m: console_errors.append(m.text)
                if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append(f"pageerror: {e}"))

        page.goto(f"{base_url.rstrip('/')}/visualization-3d.html",
                  wait_until="load", timeout=120_000)
        page.wait_for_function(
            "window.__CNS_3D__ && window.__CNS_3D__.ready === true", timeout=240_000)
        page.wait_for_timeout(2500)

        # 1) 初次打开默认离线
        report["initial"] = page.evaluate("""() => ({
            mode: window.__CNS_3D__.basemapModeState(),
            offlineChecked: document.getElementById('bmOffline').checked,
            onlineChecked: document.getElementById('bmOnline').checked,
            onlineDisabled: document.getElementById('bmOnline').disabled,
            onlineTitle: document.getElementById('bmOnline').title,
            statusText: document.getElementById('basemapModeStatus').textContent,
            imageryCheckbox: document.getElementById('ly_imagery').checked,
            imageryDisabled: document.getElementById('ly_imagery').disabled,
        })""")

        # 2) 基线：相机 / 实体 / 图层统计 / viewer 身份
        report["before"] = page.evaluate("""() => ({
            camera: window.__CNS_3D__.cameraState(),
            layerStats: window.__CNS_3D__.layerStats(),
            viewerIdentityStable: window.__CNS_3D__.viewerIdentityStable(),
        })""")
        report["entities_before"] = page.evaluate(
            "() => window.__CNS_3D__.summary().counts")

        # 3) 尝试切到在线（真实发起加载尝试）
        report["online_attempt"] = page.evaluate(
            "async () => await window.__CNS_3D__.setBasemapMode('online')")
        page.wait_for_timeout(3000)
        report["after_online"] = page.evaluate("""() => ({
            mode: window.__CNS_3D__.basemapModeState(),
            offlineChecked: document.getElementById('bmOffline').checked,
            onlineChecked: document.getElementById('bmOnline').checked,
            statusText: document.getElementById('basemapModeStatus').textContent,
            camera: window.__CNS_3D__.cameraState(),
            layerStats: window.__CNS_3D__.layerStats(),
            viewerIdentityStable: window.__CNS_3D__.viewerIdentityStable(),
        })""")
        online_shot = output_dir / "basemap_mode_online.png"
        page.screenshot(path=str(online_shot))
        report["online_screenshot"] = {
            "path": str(online_shot),
            "bytes": online_shot.stat().st_size,
            "sha256": sha256_of(online_shot),
        }

        # 4) 连续 离线→在线→离线，检查不重复添加图层
        cycles = []
        for index in range(3):
            page.evaluate(
                "async () => { await window.__CNS_3D__.setBasemapMode('online'); }")
            page.wait_for_timeout(600)
            online_stats = page.evaluate("() => window.__CNS_3D__.layerStats()")
            page.evaluate(
                "async () => { await window.__CNS_3D__.setBasemapMode('offline'); }")
            page.wait_for_timeout(600)
            offline_stats = page.evaluate("() => window.__CNS_3D__.layerStats()")
            cycles.append({
                "cycle": index,
                "online_imagery_show": bool(online_stats and online_stats.get("imagery")
                                            and online_stats["imagery"].get("show")),
                "offline_imagery_show": bool(offline_stats and offline_stats.get("imagery")
                                             and offline_stats["imagery"].get("show")),
                "offline_relief_show": bool(offline_stats and offline_stats.get("relief")
                                            and offline_stats["relief"].get("show")),
            })
        report["cycles"] = cycles

        # 5) 切换后：相机与业务实体必须保留
        report["after"] = page.evaluate("""() => ({
            mode: window.__CNS_3D__.basemapModeState(),
            camera: window.__CNS_3D__.cameraState(),
            layerStats: window.__CNS_3D__.layerStats(),
            viewerIdentityStable: window.__CNS_3D__.viewerIdentityStable(),
            stats: window.__CNS_3D__.summary().stats,
        })""")
        report["entities_after"] = page.evaluate(
            "() => window.__CNS_3D__.summary().counts")

        page.screenshot(path=str(output_dir / "basemap_mode_offline.png"))
        browser.close()

    methods = Counter(str(row.get("method", "")).upper() for row in requests)
    imagery_requests = [redact_url(r["url"]) for r in requests
                        if "/api/visualization-3d/imagery/" in r["url"]]
    report["requests"] = {
        "count": len(requests),
        "by_method": dict(sorted(methods.items())),
        "non_get_count": sum(c for m, c in methods.items() if m != "GET"),
        "imagery_proxy_requests": imagery_requests,
        "imagery_proxy_request_count": len(imagery_requests),
    }
    report["console_errors"] = console_errors
    report["state_sha256_after"] = sha256_of(state_path)
    report["state_unchanged"] = report["state_sha256_before"] == report["state_sha256_after"]

    (output_dir / "basemap_mode_switch.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url")
    parser.add_argument("state_path", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    report = verify(args.base_url, args.state_path, args.output_dir)
    print(json.dumps({
        "state_unchanged": report["state_unchanged"],
        "non_get_count": report["requests"]["non_get_count"],
        "imagery_proxy_request_count": report["requests"]["imagery_proxy_request_count"],
        "console_errors": report["console_errors"],
        "initial_mode": (report["initial"]["mode"] or {}).get("mode"),
        "online_attempt": report["online_attempt"],
    }, ensure_ascii=False, indent=2))
    return 0 if report["state_unchanged"] and report["requests"]["non_get_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
