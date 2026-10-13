"""Round37-T3D-3A：使用真实 Microsoft Edge 采集三类 P17 保护区证据。"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from urllib.parse import urlsplit


VIEWPORT = {"width": 1600, "height": 1000}


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url")
    parser.add_argument("state_path", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    args.output_dir.mkdir(parents=True, exist_ok=True)
    state_before = sha256_of(args.state_path)
    requests = []
    console = []
    screenshots = {}

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            channel="msedge", headless=False,
            args=["--ignore-gpu-blocklist", "--enable-gpu-rasterization", "--enable-zero-copy"],
        )
        context = browser.new_context(viewport=VIEWPORT, device_scale_factor=1)
        page = context.new_page()
        page.on("request", lambda request: requests.append({
            "method": request.method, "url": request.url,
        }))
        page.on("console", lambda message: console.append({
            "type": message.type, "text": message.text[:400],
        }) if message.type in {"error", "warning"} else None)
        page.goto(f"{args.base_url.rstrip('/')}/visualization-3d.html",
                  wait_until="load", timeout=120_000)
        page.wait_for_function("window.__CNS_3D__ && window.__CNS_3D__.ready === true",
                               timeout=300_000)

        default_state = page.evaluate("""() => ({
          protection: window.__CNS_3D__.layerCheckbox('protection'),
          intrusion: window.__CNS_3D__.layerCheckbox('intrusion'),
          entity_count: window.__CNS_3D__.entityCount(),
          viewer_stable: window.__CNS_3D__.viewerIdentityStable(),
        })""")

        page.evaluate("window.__CNS_3D__.setLayer('protection', true)")
        page.wait_for_function(
            "window.__CNS_3D__.protectionStats().project_groups.length > 0", timeout=120_000)
        page.evaluate("window.__CNS_3D__.flyToPreset('r0005_overhead')")
        page.wait_for_timeout(3500)

        project = page.evaluate("window.__CNS_3D__.protectionStats()")
        historical_index = next(
            index for index, row in enumerate(project["project_groups"])
            if row["status"] == "historical_stale")
        conflict_index = next(
            index for index, row in enumerate(project["project_groups"])
            if row["status"] == "geometry_version_conflict")

        # 1) 历史 P17：只开 canonical stale 航路 + 历史水平切片。
        page.evaluate("window.__CNS_3D__.setLayer('routeCanonical', true)")
        page.wait_for_function("window.__CNS_3D__.layerCheckbox('routeCanonical') === true")
        page.evaluate("window.__CNS_3D__.setLayer('routeDemo', false)")
        page.evaluate("([a,b]) => { window.__CNS_3D__.setProtectionGroupVisible(a, true); "
                      "window.__CNS_3D__.setProtectionGroupVisible(b, false); }",
                      [historical_index, conflict_index])
        page.locator("#layers").evaluate("el => { el.scrollTop = el.scrollHeight * 0.48; }")
        page.wait_for_timeout(1200)
        historical_png = args.output_dir / "01_historical_p17_horizontal_slice.png"
        page.screenshot(path=str(historical_png))
        screenshots["historical_p17"] = {
            "path": str(historical_png), "bytes": historical_png.stat().st_size,
            "sha256": sha256_of(historical_png),
            "stats": page.evaluate("window.__CNS_3D__.protectionStats()"),
        }

        # 2) 当前 demo 冲突：隐藏历史 corridor，只开 14 点 demo + 冲突缺证锚点。
        page.evaluate("window.__CNS_3D__.setLayer('routeCanonical', false)")
        page.evaluate("window.__CNS_3D__.setLayer('routeDemo', true)")
        page.wait_for_function("window.__CNS_3D__.entitiesOf('route').length >= 2")
        page.evaluate("([a,b]) => { window.__CNS_3D__.setProtectionGroupVisible(a, false); "
                      "window.__CNS_3D__.setProtectionGroupVisible(b, true); }",
                      [historical_index, conflict_index])
        page.evaluate("window.__CNS_3D__.flyToEntityOfKind('route', 1)")
        page.wait_for_timeout(1200)
        conflict_png = args.output_dir / "02_current_demo_geometry_conflict.png"
        page.screenshot(path=str(conflict_png))
        screenshots["current_demo_conflict"] = {
            "path": str(conflict_png), "bytes": conflict_png.stat().st_size,
            "sha256": sha256_of(conflict_png),
            "stats": page.evaluate("window.__CNS_3D__.protectionStats()"),
        }

        # 3) 工程示意：显式选择当前 demo 身份并手工输入 250 / 90 / 125。
        page.locator("#protectionPreviewLoad").click()
        page.wait_for_function(
            "document.querySelectorAll('#protectionPreviewRoute option').length > 2")
        demo_value = page.locator("#protectionPreviewRoute").evaluate("""select => {
          const option = [...select.options].find(row => row.textContent.includes('engineering_demo_projection'));
          return option ? option.value : '';
        }""")
        if not demo_value:
            raise RuntimeError("engineering demo route option was not rendered")
        page.locator("#protectionPreviewRoute").select_option(demo_value)
        page.locator("#protectionPreviewWidth").fill("250")
        page.locator("#protectionPreviewLower").fill("90")
        page.locator("#protectionPreviewUpper").fill("125")
        page.locator("#protectionPreviewBuild").click()
        page.wait_for_function(
            "window.__CNS_3D__.protectionStats().preview !== null", timeout=120_000)
        page.evaluate("window.__CNS_3D__.setLayer('protection', false)")
        page.locator("#layers").evaluate("el => { el.scrollTop = el.scrollHeight; }")
        page.wait_for_timeout(1500)
        preview_png = args.output_dir / "03_engineering_scenario_preview.png"
        page.screenshot(path=str(preview_png))
        screenshots["engineering_preview"] = {
            "path": str(preview_png), "bytes": preview_png.stat().st_size,
            "sha256": sha256_of(preview_png),
            "stats": page.evaluate("window.__CNS_3D__.protectionStats()"),
        }

        before_clear = page.evaluate("window.__CNS_3D__.coverageVisibility()")
        page.locator("#protectionPreviewClear").click()
        page.wait_for_timeout(300)
        after_clear = page.evaluate("window.__CNS_3D__.coverageVisibility()")
        viewer_stable_after = page.evaluate("window.__CNS_3D__.viewerIdentityStable()")
        gpu = page.evaluate("""() => {
          const canvas = document.createElement('canvas');
          const gl = canvas.getContext('webgl2') || canvas.getContext('webgl');
          if (!gl) return {available: false};
          const dbg = gl.getExtension('WEBGL_debug_renderer_info');
          return {
            available: true,
            vendor: dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : gl.getParameter(gl.VENDOR),
            renderer: dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER),
            version: gl.getParameter(gl.VERSION),
            user_agent: navigator.userAgent,
          };
        }""")
        browser.close()

    request_paths = [urlsplit(row["url"]).path for row in requests]
    report = {
        "schema": "round37-t3d-3a-protection-browser-acceptance/1",
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "browser": "Microsoft Edge (Playwright channel=msedge, headful)",
        "viewport": VIEWPORT,
        "gpu": gpu,
        "default_state": default_state,
        "screenshots": screenshots,
        "preview_visibility_before_clear": before_clear,
        "preview_visibility_after_clear": after_clear,
        "viewer_identity_stable_after_all_switches": viewer_stable_after,
        "requests": {
            "count": len(requests),
            "non_get_count": sum(1 for row in requests if row["method"].upper() != "GET"),
            "workflow_requests": request_paths.count("/api/workflow"),
            "arcgis_proxy_requests": sum(
                1 for path in request_paths if "/visualization-3d/imagery/arcgis" in path),
        },
        "console": console,
        "state_sha256_before": state_before,
        "state_sha256_after": sha256_of(args.state_path),
    }
    report["state_unchanged"] = report["state_sha256_before"] == report["state_sha256_after"]
    (args.output_dir / "browser_acceptance.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "gpu": gpu,
        "default_state": default_state,
        "screenshots": {key: value["sha256"][:16] for key, value in screenshots.items()},
        "preview_before_clear": before_clear.get("protection_preview"),
        "preview_after_clear": after_clear.get("protection_preview"),
        "viewer_identity_stable": viewer_stable_after,
        "requests": report["requests"],
        "state_unchanged": report["state_unchanged"],
        "console_messages": len(console),
    }, ensure_ascii=False, indent=2))
    ok = (
        gpu.get("available") is True
        and report["requests"]["non_get_count"] == 0
        and report["requests"]["workflow_requests"] == 0
        and report["requests"]["arcgis_proxy_requests"] == 0
        and report["state_unchanged"]
        and viewer_stable_after
        and after_clear["protection_preview"]["shown"] == 0
    )
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
