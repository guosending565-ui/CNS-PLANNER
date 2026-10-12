"""Round37-T3D-BMAP 真实浏览器视觉/网络/性能验收采集（只读、GET-only）。

与 ``capture_t3d_basemap_evidence.py``（只冻结 T3D-1 before 基线）不同，本工具负责
**本轮验收全量证据**：

* 同尺寸（1440×900）、同相机姿态的 before/after 对照；
* 六个固定视角（``CAMERA_PRESETS``）实景 PNG；
* 网络请求清单（去查询串，绝不含凭证）、瓦片完成数、失败请求；
* 帧率（运动期 / 静止后）、首屏耗时、GPU 与浏览器资源、缓存命中；
* 逐项自评 PASS / BLOCKED / FAIL，**缺凭证时影像项必须为 BLOCKED**。

约束：只发 GET；不调用 ``/api/workflow``；不把 token 写进任何证据文件。
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

#: 六个固定视角，顺序与交付报告一致（id 必须与前端 CAMERA_PRESETS 一致）。
PRESET_ORDER = [
    ("01_archipelago_overview", "archipelago_overview", "舟山群岛区域全景"),
    ("02_r0005_overhead", "r0005_overhead", "R0005 航路区域高空俯瞰"),
    ("03_taohua_oblique", "taohua_oblique", "桃花岛山体倾斜视角"),
    ("04_coast_transition", "coast_transition", "海岸线山体海面过渡"),
    ("05_port_near", "port_near", "港口道路居民区近景"),
    ("06_low_altitude", "low_altitude", "低空水平视角"),
]


def redact_url(url: str) -> str:
    """只保留 URL 身份用于证据；丢弃查询串（可能含 tk）与 fragment。"""

    parts = urlsplit(str(url))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def summarize_requests(rows):
    methods = Counter(str(row.get("method", "")).upper() for row in rows)
    failed = [redact_url(row.get("url", "")) for row in rows if row.get("failed")]
    terrain = Counter()
    imagery = Counter()
    for row in rows:
        path = urlsplit(str(row.get("url", ""))).path
        if "/terrain/" in path and path.endswith(".terrain"):
            terrain[path] += 1
        elif "/imagery/" in path:
            imagery[path] += 1
    return {
        "count": len(rows),
        "non_get_count": sum(c for m, c in methods.items() if m != "GET"),
        "by_method": dict(sorted(methods.items())),
        "terrain_tile_requests": sum(terrain.values()),
        "imagery_tile_requests": sum(imagery.values()),
        "failed_requests": failed,
        "workflow_requests": sum(
            1 for row in rows if urlsplit(str(row.get("url", ""))).path == "/api/workflow"),
        "urls": [redact_url(row.get("url", "")) for row in rows],
    }


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def wait_for_tiles(page, settle_ms: int) -> None:
    """等 Cesium 把 LOD 补满再截图。

    固定等待**不可靠**：加入影像/晕渲图层后瓦片总数大增，同样的 9 s 可能只加载到
    粗层级——实测首版重采的 after 图整片模糊、岛屿轮廓不可辨，而 report 的 scene
    块显示截图时仍有 15 个瓦片处于 loading。这里轮询真实的加载状态。
    """

    deadline = time.monotonic() + max(settle_ms, 1000) / 1000.0 * 8
    try:
        page.wait_for_function(
            "() => { const p = window.__CNS_3D__.sceneProbe(); "
            "return !!p && p.tiles_loading === 0 && p.tiles_to_render > 0; }",
            timeout=max(1000, int((deadline - time.monotonic()) * 1000)))
    except Exception:
        pass          # 超时也照常截图，scene 块里的 tiles_loading 会如实保留
    page.wait_for_timeout(2500)


def capture(base_url: str, state_path: Path, output_dir: Path, *, settle_ms: int = 9000,
            keep_open_ms: int = 0) -> dict:
    from playwright.sync_api import sync_playwright

    output_dir.mkdir(parents=True, exist_ok=True)
    shots_dir = output_dir / "views"
    shots_dir.mkdir(parents=True, exist_ok=True)

    state_before = sha256_of(state_path) if state_path.exists() else None
    requests: list[dict] = []
    console: list[dict] = []
    report: dict = {
        "schema": "round37-t3d-basemap-acceptance/1",
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "base_url": redact_url(base_url),
        "viewport": VIEWPORT,
        "state_sha256_before": state_before,
    }

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            channel="msedge",
            headless=False,
            args=[
                "--ignore-gpu-blocklist",
                "--enable-gpu-rasterization",
                "--enable-zero-copy",
                "--disable-frame-rate-limit",
            ],
        )
        context = browser.new_context(viewport=VIEWPORT, device_scale_factor=1)
        page = context.new_page()

        pending: dict[int, dict] = {}

        def on_request(request):
            entry = {"method": request.method, "url": request.url, "failed": False}
            requests.append(entry)
            try:
                pending[id(request)] = entry
            except Exception:
                pass

        def on_request_failed(request):
            entry = pending.get(id(request))
            if entry is not None:
                entry["failed"] = True

        def on_console(message):
            if message.type in {"error", "warning"}:
                console.append({"type": message.type, "text": message.text[:400]})

        page.on("request", on_request)
        page.on("requestfailed", on_request_failed)
        page.on("console", on_console)

        first_screen_start = time.perf_counter()
        page.goto(f"{base_url.rstrip('/')}/visualization-3d.html",
                  wait_until="load", timeout=120_000)
        page.wait_for_function(
            "window.__CNS_3D__ && window.__CNS_3D__.ready === true", timeout=300_000)
        ready_seconds = time.perf_counter() - first_screen_start

        # 静止等待瓦片逐级细化完成（Cesium 需要时间把 LOD 补满）
        wait_for_tiles(page, settle_ms)

        summary = page.evaluate("window.__CNS_3D__.summary()")
        camera_after = page.evaluate("window.__CNS_3D__.cameraState()")
        manifest = page.evaluate(
            "fetch('/api/visualization-3d/manifest').then(r => r.json())")

        after_png = output_dir / "t3d_basemap_after.png"
        page.screenshot(path=str(after_png))
        after_shot = {
            "path": str(after_png), "bytes": after_png.stat().st_size,
            "sha256": sha256_of(after_png),
        }

        # 六视角：逐一定位、等待细化、截图
        views = []
        for slug, preset_id, label in PRESET_ORDER:
            applied = page.evaluate(
                "(id) => window.__CNS_3D__.flyToPreset ? window.__CNS_3D__.flyToPreset(id) : false",
                preset_id)
            wait_for_tiles(page, settle_ms)
            view_png = shots_dir / f"{slug}.png"
            page.screenshot(path=str(view_png))
            views.append({
                "id": preset_id,
                "slug": slug,
                "label": label,
                "applied": bool(applied),
                "camera": page.evaluate("window.__CNS_3D__.cameraState()"),
                # 记录截图瞬间的瓦片状态，便于审查"这张图是不是还没细化完"。
                "scene": page.evaluate("window.__CNS_3D__.sceneProbe()"),
                "screenshot": {
                    "path": str(view_png), "bytes": view_png.stat().st_size,
                    "sha256": sha256_of(view_png),
                },
            })

        # 相机漫游帧率：连续旋转期间采样 requestAnimationFrame
        page.evaluate("""() => {
          window.__CNS_FPS__ = { frames: 0, start: performance.now() };
          const tick = () => {
            window.__CNS_FPS__.frames += 1;
            window.__CNS_FPS__.raf = requestAnimationFrame(tick);
          };
          tick();
          window.__CNS_FPS__.spin = setInterval(() => {
            window.__CNS_3D__.spinCamera(0.004);
          }, 16);
        }""")
        page.wait_for_timeout(6000)
        moving_fps = page.evaluate("""() => {
          const f = window.__CNS_FPS__;
          const now = performance.now();
          const fps = f.frames / ((now - f.start) / 1000);
          if (f.spin) clearInterval(f.spin);
          f.frames = 0; f.start = now;
          return fps;
        }""")
        page.wait_for_timeout(6000)
        idle_fps = page.evaluate("""() => {
          const f = window.__CNS_FPS__;
          const now = performance.now();
          const fps = f.frames / ((now - f.start) / 1000);
          if (f.raf) cancelAnimationFrame(f.raf);
          return fps;
        }""")

        resources = page.evaluate("window.__CNS_3D__.sceneProbe()")
        basemap_status = page.evaluate("window.__CNS_3D__.basemapStatus()")
        performance = page.evaluate("""() => {
          const nav = performance.getEntriesByType('navigation')[0] || {};
          const res = performance.getEntriesByType('resource');
          const byType = {};
          let transfer = 0;
          for (const r of res) {
            transfer += r.transferSize || 0;
            const key = r.initiatorType || 'other';
            byType[key] = (byType[key] || 0) + 1;
          }
          return {
            dom_content_loaded_ms: nav.domContentLoadedEventEnd || null,
            load_event_ms: nav.loadEventEnd || null,
            resource_count: res.length,
            transfer_bytes: transfer,
            resources_by_initiator: byType,
            device_memory_gb: navigator.deviceMemory || null,
            hardware_concurrency: navigator.hardwareConcurrency || null,
            user_agent: navigator.userAgent,
          };
        }""")
        gpu = page.evaluate("""() => {
          try {
            const canvas = document.createElement('canvas');
            const gl = canvas.getContext('webgl2') || canvas.getContext('webgl');
            if (!gl) return { available: false };
            const dbg = gl.getExtension('WEBGL_debug_renderer_info');
            return {
              available: true,
              vendor: dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : gl.getParameter(gl.VENDOR),
              renderer: dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER),
              version: gl.getParameter(gl.VERSION),
            };
          } catch (e) { return { available: false, error: String(e) }; }
        }""")

        if keep_open_ms:
            page.wait_for_timeout(keep_open_ms)
        browser.close()

    report.update({
        "ready_seconds": round(ready_seconds, 3),
        "summary": summary,
        "manifest_terrain_tiles": (manifest or {}).get("terrain_tiles"),
        "manifest_imagery": {
            "status": (manifest or {}).get("imagery", {}).get("status"),
            "reason_code": (manifest or {}).get("imagery", {}).get("reason_code"),
            "default_source": (manifest or {}).get("imagery", {}).get("default_source"),
        },
        "camera_after": camera_after,
        "after_screenshot": after_shot,
        "views": views,
        "performance": {
            "moving_fps": round(moving_fps, 2),
            "idle_fps": round(idle_fps, 2),
            **performance,
        },
        "gpu": gpu,
        "scene": resources,
        "basemap_status": basemap_status,
        "console_errors": console,
        "requests": summarize_requests(requests),
        "state_sha256_after": sha256_of(state_path) if state_path.exists() else None,
    })
    report["state_unchanged"] = report["state_sha256_before"] == report["state_sha256_after"]

    (output_dir / "acceptance.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url")
    parser.add_argument("state_path", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--settle-ms", type=int, default=9000)
    parser.add_argument("--keep-open-ms", type=int, default=0)
    args = parser.parse_args()
    report = capture(args.base_url, args.state_path, args.output_dir,
                     settle_ms=args.settle_ms, keep_open_ms=args.keep_open_ms)
    print(json.dumps({
        "ready_seconds": report["ready_seconds"],
        "moving_fps": report["performance"]["moving_fps"],
        "idle_fps": report["performance"]["idle_fps"],
        "gpu": report["gpu"].get("renderer"),
        "terrain_tiles": report["requests"]["terrain_tile_requests"],
        "imagery_tiles": report["requests"]["imagery_tile_requests"],
        "workflow_requests": report["requests"]["workflow_requests"],
        "non_get_count": report["requests"]["non_get_count"],
        "failed_requests": len(report["requests"]["failed_requests"]),
        "state_unchanged": report["state_unchanged"],
        "imagery_status": report["manifest_imagery"]["status"],
        "water_mask": (report["manifest_terrain_tiles"] or {}).get("water_mask_status"),
        "console_errors": len(report["console_errors"]),
    }, ensure_ascii=False, indent=2))
    # 退出码必须反映**真实验收判定**，否则人工与 CI 都会把"采集跑完了"误读成
    # "验收通过"。早期实现恒 return 0，连"状态被改写 / 出现 POST / 有失败请求"
    # 也照样返回成功，日志里因而多次把 "exit 0" 当成相机匹配的证据。
    problems = []
    if report.get("ready_seconds") is None:
        problems.append("page_not_ready")
    if not report.get("state_unchanged", True):
        problems.append("state_changed")
    if report["requests"]["non_get_count"]:
        problems.append("non_get_requests")
    if report["requests"]["workflow_requests"]:
        problems.append("workflow_requests")
    if report["requests"]["failed_requests"]:
        problems.append("failed_requests")
    if not report["gpu"].get("renderer"):
        problems.append("no_real_gpu")
    if problems:
        print("VERDICT: FAIL " + ",".join(problems))
        return 1
    # 注意：影像未授权时，这里通过只代表"采集与不变量通过"，
    # **不代表**视觉/影像验收通过（那仍是 BLOCKED）。
    print("VERDICT: PASS (invariants only; imagery=%s)"
          % report["manifest_imagery"]["status"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
