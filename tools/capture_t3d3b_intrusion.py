"""Round37-T3D-3B：真实 Microsoft Edge GPU 下的模拟轨迹回放与三类事件语义验收。

覆盖任务书 §7 的 A/B/C/D 四个场景：
  A. 当前 demo 航路 + 用户示意保护体 + 目标穿越；
  B. 只有历史 P17 水平走廊时，无法得出三维入侵结论；
  C. 目标垂直穿越保护体；
  D. 暂停、拖动、重播、清除、重新选择对象。

同时导出事件时间轴 JSON、网络请求统计与权威状态哈希。
"""

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


def stats(page):
    return page.evaluate("window.__CNS_3D__.intrusionStats()")


def assess(page, expected_instance_id):
    """点击判定并**等到新结果真的就位**（按判定自增号，而不是 configured 标志）。"""
    before = stats(page)["evaluation_revision"]
    page.locator("#intrusionAssess").click()
    page.wait_for_function(
        f"window.__CNS_3D__.intrusionStats().evaluation_revision > {before}",
        timeout=180_000)
    current = stats(page)
    if current["render_instance_id"] != expected_instance_id:
        raise RuntimeError(
            f"assessment is bound to {current['render_instance_id']!r}, "
            f"expected {expected_instance_id!r}")
    return current


def pick_option(page, selector, needle):
    value = page.locator(selector).evaluate(
        """(select, text) => {
          const option = [...select.options].find((row) => row.textContent.includes(text));
          return option ? option.value : '';
        }""", needle)
    if not value:
        raise RuntimeError(f"no option matching {needle!r} in {selector}")
    page.locator(selector).select_option(value)
    return value


def main() -> int:  # noqa: C901 - 验收脚本线性记录四个场景
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url")
    parser.add_argument("state_path", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    args.output_dir.mkdir(parents=True, exist_ok=True)
    state_before = sha256_of(args.state_path)
    requests: list[dict] = []
    console: list[dict] = []
    screenshots: dict = {}
    scenarios: dict = {}

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
                  wait_until="load", timeout=180_000)
        page.wait_for_function("window.__CNS_3D__ && window.__CNS_3D__.ready === true",
                               timeout=300_000)

        default_state = page.evaluate("""() => ({
          protection_layer: window.__CNS_3D__.layerCheckbox('protection'),
          intrusion_layer: window.__CNS_3D__.layerCheckbox('intrusion'),
          entity_count: window.__CNS_3D__.entityCount(),
          intrusion_configured: window.__CNS_3D__.intrusionStats().configured,
          viewer_stable: window.__CNS_3D__.viewerIdentityStable(),
          banner: document.getElementById('simulatedNotice').textContent.trim(),
        })""")

        # ---- 前置：用户主动创建工程示意保护体（250 m 半宽 / 90–125 m） ---------
        page.locator("#protectionPreviewLoad").click()
        page.wait_for_function(
            "document.querySelectorAll('#protectionPreviewRoute option').length > 2",
            timeout=120_000)
        pick_option(page, "#protectionPreviewRoute", "engineering_demo_projection")
        page.locator("#protectionPreviewWidth").fill("250")
        page.locator("#protectionPreviewLower").fill("90")
        page.locator("#protectionPreviewUpper").fill("125")
        page.locator("#protectionPreviewBuild").click()
        page.wait_for_function(
            "window.__CNS_3D__.protectionStats().preview !== null", timeout=180_000)

        # ---- 加载保护体实例（实例键驱动，不使用数组下标） ----------------------
        page.locator("#intrusionLoadTargets").click()
        page.wait_for_function(
            "window.__CNS_3D__.intrusionStats().targets.length >= 2", timeout=180_000)
        targets = page.evaluate("window.__CNS_3D__.intrusionStats().targets")
        preview_key = next(row["key"] for row in targets if row["mode"] ==
                           "engineering_scenario_preview")
        historical_key = next(
            row["key"] for row in targets
            if row["mode"] == "project_result" and "canonical_operational_routes" in row["label"])

        # =====================================================================
        # A. 当前 demo 航路 + 用户示意保护体 + 目标穿越
        # =====================================================================
        page.locator("#intrusionTarget").select_option(preview_key)
        page.locator("#intrusionPreset").click()
        preset_nodes = json.loads(page.locator("#intrusionTrack").input_value())
        stats_a = assess(page, preview_key.split(":", 1)[1])
        page.evaluate("window.__CNS_3D__.flyToPreset('r0005_overhead')")
        page.wait_for_timeout(2500)
        stats_a = stats(page)
        if stats_a["volume_status"] != "intrudes":
            raise RuntimeError(f"scenario A expected intrudes, got {stats_a['volume_status']}")

        # 播放 → 暂停，确认时间轴推进
        page.locator("#intrusionPlay").click()
        page.wait_for_timeout(2200)
        page.locator("#intrusionPlay").click()
        played = stats(page)
        # 拖动到中点：与播放使用同一时间基准
        midpoint = round(played["duration_s"] / 2, 3)
        page.evaluate("(t) => window.__CNS_3D__.intrusion('seek', t)", midpoint)
        scrubbed = stats(page)
        page.evaluate("(t) => window.__CNS_3D__.intrusion('seek', t)",
                      scrubbed["duration_s"])
        page.wait_for_timeout(600)
        screenshot_a = args.output_dir / "01_scenario_a_crossing.png"
        page.screenshot(path=str(screenshot_a))
        screenshots["scenario_a_crossing"] = {
            "path": str(screenshot_a), "bytes": screenshot_a.stat().st_size,
            "sha256": sha256_of(screenshot_a), "stats": stats(page),
        }

        # 事件时间轴：逐事件 seek，记录真实显示状态（同一时间基准下的纯函数结果）
        samples = []
        event_times = sorted({round(row["time_s"], 6) for row in stats_a["events"]})
        sample_times = {0.0, float(stats_a["duration_s"])}
        for index, moment in enumerate(event_times):
            sample_times.add(moment)
            sample_times.add(max(0.0, round(moment - 0.5, 3)))
            sample_times.add(min(float(stats_a["duration_s"]), round(moment + 0.5, 3)))
            if index + 1 < len(event_times):
                sample_times.add(round((moment + event_times[index + 1]) / 2, 3))
        for moment in sorted(sample_times):
            page.evaluate("(t) => window.__CNS_3D__.intrusion('seek', t)", moment)
            snapshot = stats(page)
            samples.append({
                "time_s": snapshot["time_s"],
                "position": snapshot["position"],
                "horizontal_inside": snapshot["horizontal_inside"],
                "volume_inside": snapshot["volume_inside"],
                "horizontal_label": snapshot["labels"]["horizontalText"],
                "volume_label": snapshot["labels"]["volumeText"],
            })

        # =====================================================================
        # B. 只有历史 P17 水平走廊 ⇒ 不能得出三维入侵结论
        # =====================================================================
        page.evaluate("window.__CNS_3D__.setLayer('protection', true)")
        page.wait_for_function(
            "window.__CNS_3D__.protectionStats().project_groups.length > 0", timeout=180_000)
        page.locator("#intrusionTarget").select_option(historical_key)
        page.locator("#intrusionPreset").click()
        history_nodes = json.loads(page.locator("#intrusionTrack").input_value())
        stats_b = assess(page, historical_key.split(":", 1)[1])
        page.evaluate("window.__CNS_3D__.flyToPreset('r0005_overhead')")
        page.wait_for_timeout(2500)
        page.evaluate("(t) => window.__CNS_3D__.intrusion('seek', t)", 0.0)
        stats_b = stats(page)
        if stats_b["horizontal_status"] != "intersects":
            raise RuntimeError(f"scenario B expected horizontal intersects, got {stats_b['horizontal_status']}")
        if stats_b["volume_status"] != "blocked":
            raise RuntimeError(f"scenario B expected blocked, got {stats_b['volume_status']}")
        if "入侵" in stats_b["labels"]["volumeText"]:
            raise RuntimeError("scenario B must not call the horizontal case an intrusion")
        screenshot_b = args.output_dir / "02_scenario_b_historical_horizontal_only.png"
        page.screenshot(path=str(screenshot_b))
        screenshots["scenario_b_historical_horizontal_only"] = {
            "path": str(screenshot_b), "bytes": screenshot_b.stat().st_size,
            "sha256": sha256_of(screenshot_b), "stats": stats_b,
        }

        # =====================================================================
        # C. 目标垂直穿越保护体
        # =====================================================================
        page.locator("#intrusionTarget").select_option(preview_key)
        page.evaluate("window.__CNS_3D__.intrusion('preset', 'vertical')")
        vertical_nodes = json.loads(page.locator("#intrusionTrack").input_value())
        vertical_stats = assess(page, preview_key.split(":", 1)[1])
        if vertical_stats["volume_status"] != "intrudes":
            raise RuntimeError(
                f"scenario C expected intrudes, got {vertical_stats['volume_status']}")
        middle = round(vertical_stats["duration_s"] / 2, 3)
        page.evaluate("(t) => window.__CNS_3D__.intrusion('seek', t)", middle)
        # 垂直穿越在正俯视下会退化成一点：用相机跟随的斜视偏移才能看见剖面竖线。
        page.evaluate(
            "() => window.__CNS_3D__.intrusion('follow', "
            "{enabled: true, headingDeg: 305, pitchDeg: -16, rangeM: 1100})")
        page.wait_for_timeout(2500)
        vertical_stats = stats(page)
        if not vertical_stats["volume_bar_shown"]:
            raise RuntimeError("scenario C expected the in-volume section bar to be visible")
        screenshot_c = args.output_dir / "03_scenario_c_vertical_crossing.png"
        page.screenshot(path=str(screenshot_c))
        screenshots["scenario_c_vertical_crossing"] = {
            "path": str(screenshot_c), "bytes": screenshot_c.stat().st_size,
            "sha256": sha256_of(screenshot_c), "stats": stats(page),
        }

        # =====================================================================
        # D. 暂停 / 拖动 / 重播 / 清除 / 重新选择对象
        # =====================================================================
        page.evaluate("window.__CNS_3D__.intrusion('pause')")
        paused = stats(page)
        page.evaluate("(t) => window.__CNS_3D__.intrusion('seek', t)", 0.0)
        seeked_zero = stats(page)
        page.evaluate("window.__CNS_3D__.intrusion('reset')")
        reset = stats(page)
        page.evaluate("window.__CNS_3D__.intrusion('follow', true)")
        follow_on = page.evaluate("document.getElementById('intrusionFollow').checked")
        follow_stats = stats(page)
        page.wait_for_timeout(900)
        page.evaluate("window.__CNS_3D__.intrusion('play')")
        page.wait_for_timeout(1600)
        replay = stats(page)
        page.evaluate("window.__CNS_3D__.intrusion('pause')")
        page.evaluate("window.__CNS_3D__.intrusion('follow', false)")
        follow_off = page.evaluate("document.getElementById('intrusionFollow').checked")
        # 展示：回到俯视，停在"三维进入"时刻，并按事件定位到空间位置。
        page.evaluate("window.__CNS_3D__.flyToPreset('r0005_overhead')")
        page.wait_for_timeout(2200)
        volume_entry_index = next(
            index for index, row in enumerate(replay["events"])
            if row["scope"] == "three_dimensional_volume_intrusion" and row["kind"] == "entry")
        page.evaluate("(t) => window.__CNS_3D__.intrusion('seek', t)",
                      replay["events"][volume_entry_index]["time_s"])
        page.evaluate("(t) => window.__CNS_3D__.intrusion('focusEvent', t)",
                      volume_entry_index)
        page.wait_for_timeout(1600)
        screenshot_d = args.output_dir / "04_scenario_d_playback_controls.png"
        page.screenshot(path=str(screenshot_d))
        screenshots["scenario_d_playback_controls"] = {
            "path": str(screenshot_d), "bytes": screenshot_d.stat().st_size,
            "sha256": sha256_of(screenshot_d), "stats": stats(page),
        }

        before_clear_visibility = page.evaluate("window.__CNS_3D__.coverageVisibility()")
        cleared = page.evaluate("window.__CNS_3D__.intrusion('clear')")
        page.wait_for_timeout(500)
        after_clear = stats(page)
        after_clear_visibility = page.evaluate("window.__CNS_3D__.coverageVisibility()")

        # 重新选择对象：切回历史实例并重新判定
        page.locator("#intrusionTarget").select_option(historical_key)
        page.locator("#intrusionPreset").click()
        reselected = assess(page, historical_key.split(":", 1)[1])
        viewer_stable = page.evaluate("window.__CNS_3D__.viewerIdentityStable()")
        entity_count_total = page.evaluate("window.__CNS_3D__.entityCount()")

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

    scenarios = {
        "A_crossing": {
            "target_key": preview_key, "preset_nodes": preset_nodes,
            "horizontal_status": stats_a["horizontal_status"],
            "volume_status": stats_a["volume_status"],
            "surveillance_status": stats_a["surveillance_status"],
            "events": stats_a["events"],
            "played_time_s": played["time_s"],
            "scrubbed_time_s": scrubbed["time_s"],
            "midpoint_s": midpoint,
            "labels": scrubbed["labels"],
        },
        "B_historical_horizontal_only": {
            "target_key": historical_key, "preset_nodes": history_nodes,
            "horizontal_status": stats_b["horizontal_status"],
            "volume_status": stats_b["volume_status"],
            "volume_reason": stats_b["volume_reason"],
            "surveillance_status": stats_b["surveillance_status"],
            "events": stats_b["events"],
            "labels": stats_b["labels"],
            "eligible_for_current_warning": stats_b["eligible_for_current_warning"],
        },
        "C_vertical_crossing": {
            "target_key": preview_key, "preset_nodes": vertical_nodes,
            "horizontal_status": vertical_stats["horizontal_status"],
            "volume_status": vertical_stats["volume_status"],
            "events": vertical_stats["events"],
            "labels": vertical_stats["labels"],
        },
        "D_playback_controls": {
            "paused_time_s": paused["time_s"], "paused_playing": paused["playing"],
            "seek_zero_time_s": seeked_zero["time_s"],
            "reset_time_s": reset["time_s"], "reset_playing": reset["playing"],
            "follow_enabled": follow_on,
            "follow_stats": {
                "camera_following": follow_stats["camera_following"],
                "time_s": follow_stats["time_s"],
            },
            "follow_disabled": follow_off is False,
            "focused_event_index": volume_entry_index,
            "focused_event_time_s": replay["events"][volume_entry_index]["time_s"],
            "replay_time_s": replay["time_s"], "replay_playing": replay["playing"],
            "before_clear_visibility": before_clear_visibility,
            "after_clear_entity_count": after_clear["entity_count"],
            "after_clear_visibility": after_clear_visibility,
            "reselected_render_instance_id": reselected["render_instance_id"],
            "reselected_mode_key": historical_key,
            "viewer_identity_stable": viewer_stable,
            "entity_count_total": entity_count_total,
            "cleared": cleared,
        },
    }

    timeline = {
        "schema": "round37-t3d-3b-intrusion-timeline/1",
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "mode": "engineering_scenario_preview",
        "render_instance_id": stats_a["render_instance_id"],
        "track_id": stats_a["track_id"],
        "simulated": stats_a["simulated"],
        "eligible_for_current_warning": stats_a["eligible_for_current_warning"],
        "duration_s": stats_a["duration_s"],
        "node_count": stats_a["node_count"],
        "nodes": preset_nodes,
        "events": stats_a["events"],
        "samples": samples,
        "semantics": {
            "horizontal_projection_intersection": stats_a["labels"]["horizontalText"],
            "three_dimensional_volume_intrusion": stats_a["labels"]["volumeText"],
            "surveillance_detection": stats_a["labels"]["surveillanceText"],
            "banner": stats_a["banner"],
        },
        "scenarios": scenarios,
    }
    (args.output_dir / "intrusion_timeline.json").write_text(
        json.dumps(timeline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    request_paths = [urlsplit(row["url"]).path for row in requests]
    report = {
        "schema": "round37-t3d-3b-intrusion-browser-acceptance/1",
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "browser": "Microsoft Edge (Playwright channel=msedge, headful)",
        "viewport": VIEWPORT,
        "gpu": gpu,
        "default_state": default_state,
        "targets": targets,
        "screenshots": screenshots,
        "scenarios": scenarios,
        "requests": {
            "count": len(requests),
            "non_get_count": sum(1 for row in requests if row["method"].upper() != "GET"),
            "workflow_requests": request_paths.count("/api/workflow"),
            "arcgis_proxy_requests": sum(
                1 for path in request_paths if "/visualization-3d/imagery/arcgis" in path),
            "intrusion_assessment_requests": sum(
                1 for path in request_paths
                if path == "/api/visualization-3d/intrusion-assessment"),
        },
        "console": console,
        "state_sha256_before": state_before,
        "state_sha256_after": sha256_of(args.state_path),
    }
    report["state_unchanged"] = report["state_sha256_before"] == report["state_sha256_after"]
    (args.output_dir / "browser_acceptance.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    summary = {
        "gpu": gpu.get("renderer"),
        "default_state": default_state,
        "A": {"horizontal": stats_a["horizontal_status"], "volume": stats_a["volume_status"],
              "events": len(stats_a["events"])},
        "B": {"horizontal": stats_b["horizontal_status"], "volume": stats_b["volume_status"],
              "reason": stats_b["volume_reason"]},
        "C": {"horizontal": vertical_stats["horizontal_status"],
              "volume": vertical_stats["volume_status"],
              "events": len(vertical_stats["events"])},
        "D": {"after_clear_entities": after_clear["entity_count"],
              "viewer_stable": viewer_stable,
              "follow_enabled": follow_on},
        "requests": report["requests"],
        "state_unchanged": report["state_unchanged"],
        "console_messages": len(console),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    ok = (
        gpu.get("available") is True
        and default_state["intrusion_layer"] is False
        and default_state["entity_count"] == 0
        and stats_a["volume_status"] == "intrudes"
        and stats_b["volume_status"] == "blocked"
        and vertical_stats["volume_status"] == "intrudes"
        and after_clear["entity_count"] == 0
        and after_clear_visibility["intrusion"]["shown"] == 0
        and report["requests"]["non_get_count"] == 0
        and report["requests"]["workflow_requests"] == 0
        and report["requests"]["arcgis_proxy_requests"] == 0
        and report["state_unchanged"]
        and viewer_stable
    )
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
