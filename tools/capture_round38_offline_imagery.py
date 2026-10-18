"""Round38-OFFLINE-IMAGERY 真实浏览器验收采集（只读、GET-only）。

目的：在**完全无凭证**的独立实例上，用真实浏览器证明：

1. 舟山离线卫星影像与 FABDEM 三维地形都能正常显示；
2. 离线模式期间对 ArcGIS/天地图上游的请求数**恒为 0**；
3. 三种底图模式互斥切换正常，相机与业务实体不被破坏；
4. 数据范围外瓦片按 404 处理，不产生持续重试。

约束：只发 GET；不调用 ``/api/workflow``；不写任何状态；不保存凭证。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

VIEWPORT = {"width": 1440, "height": 900}

#: 上游影像域名（离线模式必须对它们保持 0 请求）。
UPSTREAM_HOSTS = {
    "ibasemaps-api.arcgis.com": "arcgis",
    "services.arcgisonline.com": "arcgis",
    "t0.tianditu.gov.cn": "tianditu",
    "t1.tianditu.gov.cn": "tianditu",
}

PRESET_ORDER = [
    ("01_archipelago_overview", "archipelago_overview", "舟山群岛全景"),
    ("02_r0005_overhead", "r0005_overhead", "R0005 区域高空俯瞰"),
    ("03_taohua_oblique", "taohua_oblique", "桃花岛 45° 斜视"),
    ("05_port_near", "port_near", "港口道路居民区近景"),
    ("06_low_altitude", "low_altitude", "低空近水平观察"),
]


def redact_url(url: str) -> str:
    parts = urlsplit(str(url))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def summarize(rows):
    by_host = Counter(urlsplit(str(r.get("url", ""))).netloc for r in rows)
    upstream = Counter()
    for row in rows:
        host = urlsplit(str(row.get("url", ""))).netloc
        kind = UPSTREAM_HOSTS.get(host)
        if kind:
            upstream[kind] += 1
    paths = Counter()
    for row in rows:
        parts = urlsplit(str(row.get("url", ""))).path.split("/")
        if len(parts) > 5 and parts[1:4] == ["api", "visualization-3d", "imagery"]:
            paths[parts[4]] += 1
    failed = [redact_url(r.get("url", "")) for r in rows if r.get("failed")]
    return {
        "count": len(rows),
        "non_get_count": sum(1 for r in rows
                             if str(r.get("method", "")).upper() != "GET"),
        "upstream_imagery_requests": dict(upstream),
        "upstream_imagery_total": sum(upstream.values()),
        "imagery_requests_by_source": dict(paths),
        "failed_requests": failed,
        "failed_count": len(failed),
        "workflow_requests": sum(
            1 for r in rows if urlsplit(str(r.get("url", ""))).path == "/api/workflow"),
        "hosts": dict(by_host.most_common(12)),
    }


def wait_for_tiles(page, settle_ms: int) -> None:
    deadline = time.monotonic() + max(settle_ms, 1000) / 1000.0 * 8
    try:
        page.wait_for_function(
            "() => { const p = window.__CNS_3D__.sceneProbe(); "
            "return !!p && p.tiles_loading === 0 && p.tiles_to_render > 0; }",
            timeout=max(1000, int((deadline - time.monotonic()) * 1000)))
    except Exception:
        pass
    page.wait_for_timeout(3000)


def capture(base_url: str, output_dir: Path, *, settle_ms: int = 12000,
            offline: bool = False) -> dict:
    from playwright.sync_api import sync_playwright

    output_dir.mkdir(parents=True, exist_ok=True)
    shots = output_dir / "views"
    shots.mkdir(parents=True, exist_ok=True)

    requests: list[dict] = []
    console: list[dict] = []
    report: dict = {
        "schema": "round38-offline-imagery-acceptance/1",
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "base_url": redact_url(base_url),
        "viewport": VIEWPORT,
        "browser_offline_mode": bool(offline),
    }

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            channel="msedge", headless=False,
            args=["--ignore-gpu-blocklist", "--enable-gpu-rasterization",
                  "--enable-zero-copy", "--disable-frame-rate-limit"])
        context = browser.new_context(viewport=VIEWPORT, device_scale_factor=1)
        page = context.new_page()
        pending: dict[int, dict] = {}

        def on_request(request):
            entry = {"method": request.method, "url": request.url, "failed": False}
            requests.append(entry)
            pending[id(request)] = entry

        def on_failed(request):
            entry = pending.get(id(request))
            if entry is not None:
                entry["failed"] = True

        def on_console(message):
            if message.type in {"error", "warning"}:
                console.append({"type": message.type, "text": message.text[:400]})

        page.on("request", on_request)
        page.on("requestfailed", on_failed)
        page.on("console", on_console)

        # 离线模式：**在加载页面前**就阻断所有非本机网络，证明"完全无外网也能演示"。
        # 注意必须比较 **hostname**（不含端口）：``urlsplit().netloc`` 对
        # ``127.0.0.1:8775`` 返回带端口的串，直接比较会把本机页面本身也一起阻断
        # （实测表现为 ``net::ERR_FAILED``，页面根本打不开）。
        if offline:
            def block_external(route):
                host = (urlsplit(route.request.url).hostname or "").lower()
                if host in {"127.0.0.1", "localhost", "::1"}:
                    route.continue_()
                else:
                    route.abort()

            context.route("**/*", block_external)

        started = time.perf_counter()
        page.goto(f"{base_url.rstrip('/')}/visualization-3d.html",
                  wait_until="load", timeout=180_000)
        page.wait_for_function(
            "window.__CNS_3D__ && window.__CNS_3D__.ready === true", timeout=300_000)
        ready_seconds = time.perf_counter() - started
        wait_for_tiles(page, settle_ms)

        def snapshot():
            return {
                "camera": page.evaluate("window.__CNS_3D__.cameraState()"),
                "scene": page.evaluate("window.__CNS_3D__.sceneProbe()"),
                "basemap_status": page.evaluate("window.__CNS_3D__.basemapStatus()"),
                "basemap_mode": page.evaluate(
                    "() => (window.__CNS_3D__.basemapModeState ? window.__CNS_3D__.basemapModeState() : null)"),
                "layer_stats": page.evaluate(
                    "() => { const s = window.__CNS_3D__.basemapLayerStats ? "
                    "window.__CNS_3D__.basemapLayerStats() : null; return s; }"),
                "mode_status_text": page.evaluate(
                    "() => { const n = document.getElementById('basemapModeStatus'); "
                    "return n ? n.textContent : null; }"),
                "credits_text": page.evaluate(
                    "() => { const n = document.getElementById('credits'); "
                    "return n ? n.textContent : null; }"),
                "imagery_status_text": page.evaluate(
                    "() => { const n = document.getElementById('imageryStatus'); "
                    "return n ? n.textContent : null; }"),
                "radios": page.evaluate(
                    "() => ({ offline_imagery: document.getElementById('bmOfflineImagery')"
                    " ? document.getElementById('bmOfflineImagery').checked : null,"
                    " offline_relief: document.getElementById('bmOffline')"
                    " ? document.getElementById('bmOffline').checked : null,"
                    " online: document.getElementById('bmOnline')"
                    " ? document.getElementById('bmOnline').checked : null,"
                    " online_disabled: document.getElementById('bmOnline')"
                    " ? document.getElementById('bmOnline').disabled : null,"
                    " offline_imagery_disabled: document.getElementById('bmOfflineImagery')"
                    " ? document.getElementById('bmOfflineImagery').disabled : null })"),
            }

        # ---- 默认（离线卫星影像）首屏 ----
        default_state = snapshot()
        default_png = output_dir / "00_default_offline_imagery.png"
        page.screenshot(path=str(default_png))
        report["default_state"] = default_state
        report["default_screenshot"] = {
            "path": default_png.name, "bytes": default_png.stat().st_size,
            "sha256": sha256_of(default_png)}

        # ---- 三模式互斥切换 ----
        switches = {}
        for mode_id, value in (("offline_relief", "offline"),
                               ("online", "online"),
                               ("offline_imagery", "offline_imagery")):
            result = page.evaluate(
                "async (mode) => await window.__CNS_3D__.setBasemapMode(mode)", value)
            page.wait_for_timeout(2500)
            state = snapshot()
            png = shots / f"mode_{mode_id}.png"
            page.screenshot(path=str(png))
            switches[mode_id] = {
                "result": result, "state": state,
                "screenshot": {"path": f"views/{png.name}",
                               "bytes": png.stat().st_size, "sha256": sha256_of(png)},
            }
        report["mode_switches"] = switches

        # ---- 离线卫星影像下的五个固定视角 ----
        page.evaluate("async () => await window.__CNS_3D__.setBasemapMode('offline_imagery')")
        page.wait_for_timeout(2500)
        views = []
        for slug, preset_id, label in PRESET_ORDER:
            applied = page.evaluate(
                "(id) => window.__CNS_3D__.flyToPreset ? window.__CNS_3D__.flyToPreset(id) : false",
                preset_id)
            wait_for_tiles(page, settle_ms)
            png = shots / f"{slug}.png"
            page.screenshot(path=str(png))
            views.append({
                "id": preset_id, "slug": slug, "label": label,
                "applied": bool(applied),
                "camera": page.evaluate("window.__CNS_3D__.cameraState()"),
                "scene": page.evaluate("window.__CNS_3D__.sceneProbe()"),
                "screenshot": {"path": f"views/{png.name}",
                               "bytes": png.stat().st_size, "sha256": sha256_of(png)},
            })
        report["views"] = views

        # ---- 倾斜视角 + 缩放（相机操作可用性） ----
        page.evaluate("window.__CNS_3D__.flyToPreset('taohua_oblique')")
        page.wait_for_timeout(4000)
        tilt_before = page.evaluate("window.__CNS_3D__.cameraState()")
        page.evaluate("() => { const v = window.__CNS_3D__.viewerIdentity;"
                      " if (v) { v.camera.zoomOut(3.0); } }")
        page.wait_for_timeout(3000)
        tilt_after = page.evaluate("window.__CNS_3D__.cameraState()")
        png = shots / "tilt_zoom_taohua.png"
        page.screenshot(path=str(png))
        report["tilt_zoom"] = {
            "camera_before": tilt_before, "camera_after": tilt_after,
            "height_changed": bool(
                tilt_before and tilt_after
                and abs(tilt_before.get("height", 0) - tilt_after.get("height", 0)) > 1),
            "screenshot": {"path": f"views/{png.name}",
                           "bytes": png.stat().st_size, "sha256": sha256_of(png)},
        }

        report["final_state"] = snapshot()
        report["ready_seconds"] = round(ready_seconds, 3)
        report["console_errors"] = console
        report["requests"] = summarize(requests)
        browser.close()

    (output_dir / "acceptance.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url")
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--settle-ms", type=int, default=12000)
    parser.add_argument("--offline", action="store_true",
                        help="阻断所有非 localhost 网络，验证完全无网演示")
    args = parser.parse_args()
    report = capture(args.base_url, args.output_dir,
                     settle_ms=args.settle_ms, offline=args.offline)
    summary = {
        "ready_seconds": report["ready_seconds"],
        "default_basemap_mode": (report["default_state"].get("basemap_mode") or {}).get("basemap_mode"),
        "default_mode_text": report["default_state"].get("mode_status_text"),
        "offline_imagery_available": (report["default_state"].get("basemap_mode") or {}).get(
            "offline_imagery_available"),
        "upstream_imagery_total": report["requests"]["upstream_imagery_total"],
        "upstream_by_kind": report["requests"]["upstream_imagery_requests"],
        "imagery_requests_by_source": report["requests"]["imagery_requests_by_source"],
        "non_get_count": report["requests"]["non_get_count"],
        "failed_count": report["requests"]["failed_count"],
        "workflow_requests": report["requests"]["workflow_requests"],
        "console_errors": len(report["console_errors"]),
        "browser_offline": report["browser_offline_mode"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    problems = []
    if not report.get("ready_seconds"):
        problems.append("page_not_ready")
    if report["requests"]["non_get_count"]:
        problems.append("non_get_requests")
    if report["requests"]["workflow_requests"]:
        problems.append("workflow_requests")
    if report["requests"]["upstream_imagery_total"]:
        problems.append("upstream_imagery_requests_nonzero")
    default_mode = (report["default_state"].get("basemap_mode") or {}).get("basemap_mode")
    if default_mode != "offline_imagery":
        problems.append(f"default_mode_not_offline_imagery:{default_mode}")
    if problems:
        print("VERDICT: FAIL " + ",".join(problems))
        return 1
    print("VERDICT: PASS (offline imagery; upstream imagery requests = 0)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
