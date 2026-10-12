"""Capture an immutable T3D browser baseline using GET requests only."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


VIEWPORT = {"width": 1440, "height": 900}


def redact_url(url: str) -> str:
    """Keep only the URL identity needed for evidence; discard all secrets."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def summarize_requests(rows: list[dict[str, str]]) -> dict[str, object]:
    methods = Counter(str(row.get("method", "")).upper() for row in rows)
    return {
        "count": len(rows),
        "non_get_count": sum(count for method, count in methods.items() if method != "GET"),
        "by_method": dict(sorted(methods.items())),
        "urls": [redact_url(str(row.get("url", ""))) for row in rows],
    }


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def capture(base_url: str, state_path: Path, output_dir: Path) -> dict[str, object]:
    from playwright.sync_api import sync_playwright

    output_dir.mkdir(parents=True, exist_ok=True)
    screenshot_path = output_dir / "t3d1_before.png"
    camera_path = output_dir / "t3d1_before_camera.json"
    report_path = output_dir / "t3d1_before_network.json"
    state_before = sha256_of(state_path)
    requests: list[dict[str, str]] = []
    report: dict[str, object] = {
        "schema": "round37-t3d-basemap-before/1",
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "base_url": redact_url(base_url),
        "viewport": VIEWPORT,
        "state_sha256_before": state_before,
    }

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            channel="msedge",
            headless=False,
            args=["--ignore-gpu-blocklist", "--enable-gpu-rasterization"],
        )
        context = browser.new_context(viewport=VIEWPORT, device_scale_factor=1)
        page = context.new_page()
        page.on("request", lambda request: requests.append({
            "method": request.method,
            "url": request.url,
        }))
        page.goto(f"{base_url.rstrip('/')}/visualization-3d.html", wait_until="load", timeout=90_000)
        page.wait_for_function(
            "window.__CNS_3D__ && window.__CNS_3D__.ready === true", timeout=180_000)
        page.wait_for_timeout(3000)
        camera = page.evaluate("window.__CNS_3D__.cameraState()")
        summary = page.evaluate("window.__CNS_3D__.summary()")
        page.screenshot(path=str(screenshot_path))
        browser.close()

    camera_path.write_text(json.dumps(camera, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report.update({
        "camera": camera,
        "revision_before": summary.get("revision"),
        "requests": summarize_requests(requests),
        "screenshot": {
            "path": str(screenshot_path),
            "bytes": screenshot_path.stat().st_size,
            "sha256": sha256_of(screenshot_path),
        },
        "state_sha256_after": sha256_of(state_path),
    })
    report["state_unchanged"] = report["state_sha256_before"] == report["state_sha256_after"]
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url")
    parser.add_argument("state_path", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    report = capture(args.base_url, args.state_path, args.output_dir)
    print(json.dumps({
        "state_unchanged": report["state_unchanged"],
        "non_get_count": report["requests"]["non_get_count"],
        "screenshot": report["screenshot"],
    }, ensure_ascii=False))
    return 0 if report["state_unchanged"] and report["requests"]["non_get_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
