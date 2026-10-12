"""同尺寸、同相机姿态的 before/after 对照采集（Round37-T3D-BMAP 硬性要求）。

before 使用 T3D-1 冻结的 ``t3d1_before_camera.json``（heading/pitch/roll 为**弧度**，
与 ``setCameraState`` 的入参一致），after 在同一服务上用 ``setCameraState`` 精确复位，
因此两张 PNG 的相机姿态逐字段相同，可直接做视觉对比。
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


def redact(url: str) -> str:
    parts = urlsplit(str(url))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url")
    parser.add_argument("before_camera", type=Path)
    parser.add_argument("state_path", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--settle-ms", type=int, default=9000)
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    args.output_dir.mkdir(parents=True, exist_ok=True)
    camera = json.loads(args.before_camera.read_text(encoding="utf-8"))
    state_before = sha256_of(args.state_path)
    requests: list[dict] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            channel="msedge", headless=False,
            args=["--ignore-gpu-blocklist", "--enable-gpu-rasterization"])
        context = browser.new_context(viewport=VIEWPORT, device_scale_factor=1)
        page = context.new_page()
        page.on("request", lambda r: requests.append({"method": r.method, "url": r.url}))
        page.goto(f"{args.base_url.rstrip('/')}/visualization-3d.html",
                  wait_until="load", timeout=120_000)
        page.wait_for_function("window.__CNS_3D__ && window.__CNS_3D__.ready === true",
                               timeout=300_000)
        page.wait_for_timeout(args.settle_ms)

        applied = page.evaluate(
            "(state) => window.__CNS_3D__.setCameraState(state)", camera)

        # 相机复位可能被页面内的异步收尾（自检、首帧 flyTo）覆盖。这里**轮询到真正
        # 匹配**再截图，并对每次尝试重新施加，避免用一张姿态不符的图冒充对照证据。
        #
        # 容差说明：Cesium 的 setView 会把经纬高转成 ECEF 再转回 Cartographic，存在
        # 浮点往返误差。实测最大偏差约 1.3e-9 m（纳米级），远小于任何像素对应的地面
        # 尺度，因此用 1e-6（微米级）作为"同一相机姿态"的判定阈值：它比往返噪声大
        # 三个数量级，又比任何真实漂移小九个数量级。
        tolerance = 1e-6
        fields = ("longitude", "latitude", "height", "heading", "pitch", "roll")
        actual = None
        attempts = 0
        for attempts in range(1, 13):
            page.wait_for_timeout(1500)
            actual = page.evaluate("window.__CNS_3D__.cameraState()")
            if actual and all(
                    abs(actual[f] - camera[f]) <= tolerance for f in fields):
                break
            page.evaluate(
                "(state) => window.__CNS_3D__.setCameraState(state)", camera)
        # 匹配后仍需等瓦片细化完成，否则截图里地形是粗 LOD。
        # 固定等待**不可靠**：加入影像/晕渲图层后瓦片总数大增，同样的 9 s 可能只加载
        # 到粗层级（实测首版重采的 after 图整片模糊、岛屿轮廓不可辨，而 report 的
        # scene 块显示截图时仍有 15 个瓦片在 loading）。这里改为轮询 Cesium 的**实际**
        # 加载状态，等 tiles_loading 归零后再截图，并留时间让最后一批瓦片真正画完。
        settle_deadline = time.monotonic() + max(args.settle_ms, 1000) / 1000.0 * 8
        try:
            page.wait_for_function(
                "() => { const p = window.__CNS_3D__.sceneProbe(); "
                "return !!p && p.tiles_loading === 0 && p.tiles_to_render > 0; }",
                timeout=max(1000, int((settle_deadline - time.monotonic()) * 1000)))
        except Exception:
            pass          # 超时也照常截图，但 report 的 scene 块会如实记录 tiles_loading
        page.wait_for_timeout(2500)
        final = page.evaluate("window.__CNS_3D__.cameraState()")
        matched = bool(final) and all(
            abs(final[f] - camera[f]) <= tolerance for f in fields)
        actual = final

        after_png = args.output_dir / "t3d_basemap_after_same_camera.png"
        page.screenshot(path=str(after_png))
        scene = page.evaluate("window.__CNS_3D__.sceneProbe()")
        basemap = page.evaluate("window.__CNS_3D__.basemapStatus()")
        browser.close()

    report = {
        "schema": "round37-t3d-basemap-before-after/1",
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "viewport": VIEWPORT,
        "before_camera_requested": camera,
        "after_camera_actual": actual,
        "camera_applied": bool(applied),
        "camera_attempts": attempts,
        "camera_matched_exactly": matched,
        "camera_tolerance": tolerance,
        "camera_match": {
            field: (actual or {}).get(field) == camera.get(field)
            for field in ("longitude", "latitude", "height", "heading", "pitch", "roll")
        },
        "after_screenshot": {
            "path": str(after_png), "bytes": after_png.stat().st_size,
            "sha256": sha256_of(after_png),
        },
        "scene": scene,
        "basemap_status": basemap,
        "requests": {
            "count": len(requests),
            "non_get_count": sum(1 for r in requests
                                 if str(r["method"]).upper() != "GET"),
            "workflow_requests": sum(1 for r in requests
                                     if urlsplit(r["url"]).path == "/api/workflow"),
        },
        "state_sha256_before": state_before,
        "state_sha256_after": sha256_of(args.state_path),
    }
    report["state_unchanged"] = report["state_sha256_before"] == report["state_sha256_after"]
    (args.output_dir / "before_after.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "camera_applied": report["camera_applied"],
        "camera_matched_exactly": report["camera_matched_exactly"],
        "camera_attempts": report["camera_attempts"],
        "camera_match": report["camera_match"],
        "after_screenshot": report["after_screenshot"]["sha256"][:16],
        "non_get_count": report["requests"]["non_get_count"],
        "workflow_requests": report["requests"]["workflow_requests"],
        "state_unchanged": report["state_unchanged"],
    }, ensure_ascii=False, indent=2))
    # 相机未精确匹配 => 该对照证据无效，必须以非零退出码显式失败。
    return 0 if report["camera_matched_exactly"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
