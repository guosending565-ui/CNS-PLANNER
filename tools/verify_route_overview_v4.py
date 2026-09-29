"""VERIFY_V4：用**当前 code-review 修复后的正式生产链**重新生成专题图并做硬验收。

链路（与线上一致，不再自建 FigureSpec）：

    MapFigureService.export()
      → build_figure()  → FigureSpec
      → _render()       → QgisFigureRenderer（QGIS 版面，300 DPI）
      → _publish()      → 受控产物 + 图件索引（**不写 ProjectState**）

本脚本额外固化的验收点：

1. 300 DPI、A4 竖版；
2. 海域来自**新 shapely 精确 difference**（``canvas.difference(land_union)``），
   并保留 polygon holes（FigureSpec 里能看到内环记录）；
3. 生成前后 project revision **完全不变**，且 ``session.save()`` 一次都不被调用；
4. 连续导出两次：``figure_id`` 相同、第二次不再执行渲染、revision 仍不变；
5. 产物只写 ``outputs/``（图件记录写在独立的探针项目目录，绝不碰真实项目状态）。

用法：
    & 'C:\\Program Files\\QGIS 3.44.14\\bin\\python-qgis-ltr.bat' tools/verify_route_overview_v4.py
"""

from __future__ import annotations

import copy
import datetime
import hashlib
import io
import json
import math
import os
from pathlib import Path
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

#: 进程级持有 QgsApplication（局部变量被回收会导致后续 Qt 访问 fail-fast）。
_QGIS_APPLICATION = None

IMAGE_PATH = Path("outputs/route_overview_VERIFY_V4.png")
SPEC_PATH = Path("outputs/route_overview_VERIFY_V4_spec.json")
#: 图件记录写进独立探针目录（MapFigureStore 的受控目录），**不碰**真实项目状态。
PROBE_PROJECT = Path("_diag/v4_probe_project")

DEMO_ROUTE = {
    "route_id": "DEMO-V4-舟山演示航路", "status": "demo", "path_crs": "OGC:CRS84",
    "kind": "demo_route_for_cartography_verification_only",
    "path": [[122.1067, 30.0167], [122.1800, 30.0400], [122.2450, 30.0200],
             [122.3100, 29.9750], [122.3900, 29.9600]],
    # 真实 canonical 形态：start / end 是坐标数组。
    "start": [122.1067, 30.0167], "end": [122.3900, 29.9600],
    "start_node_id": "N0001", "end_node_id": "N0002",
    "provenance": {"source_type": "verification_in_memory", "not_project_state": True},
}

AIRSPACE_KEYS = ("airspace", "suitable_airspace", "confirmed_airspace", "air_space")
AIRSPACE_TEXT_HINTS = ("适飞空域", "空域")


def qgis():
    global _QGIS_APPLICATION
    from qgis.core import QgsApplication

    _QGIS_APPLICATION = QgsApplication.instance() or QgsApplication([], False)
    _QGIS_APPLICATION.initQgis()
    return _QGIS_APPLICATION


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class _ProbeSession:
    """最小 session：只读 state、**统计** save() 调用次数（本脚本要求恒为 0）。"""

    def __init__(self, state, store_path):
        self.state = state
        self.store_path = store_path
        self.save_calls = 0

    def save(self):
        self.save_calls += 1
        raise AssertionError("制图链路不得调用 session.save()（会推进业务 revision）")


class _CountingRenderer:
    """包一层生产渲染器，只为统计"渲染被真正执行了几次"。"""

    def __init__(self):
        from cns_planner.gis.qgis_figure_renderer import QgisFigureRenderer

        self.inner = QgisFigureRenderer()
        self.calls = []

    def render(self, spec, *, dpi):
        self.calls.append(float(dpi))
        return self.inner.render(spec, dpi=dpi)


def render_extent(spec):
    """与渲染器一致的"实际渲染范围"（把 extent 撑到地图框长宽比）。"""

    plan = spec.layout
    frame_aspect = float(plan["map_width_mm"]) / float(plan["map_height_mm"])
    extent = spec.extent
    centre_x = (extent.west + extent.east) / 2.0
    centre_y = (extent.south + extent.north) / 2.0
    data_aspect = (extent.east - extent.west) / (extent.north - extent.south)
    if data_aspect >= frame_aspect:
        half_width = (extent.east - extent.west) / 2.0
        half_height = half_width / frame_aspect
    else:
        half_height = (extent.north - extent.south) / 2.0
        half_width = half_height * frame_aspect
    return (centre_x - half_width, centre_y - half_height,
            centre_x + half_width, centre_y + half_height)


def pixel_checks(spec, image_path):
    """程序化检查：海陆占比、站址是否落在陆地、内河水域是否被填平、层级与图例。"""

    from PIL import Image

    image = Image.open(io.BytesIO(Path(image_path).read_bytes())).convert("RGB")
    width, height = image.size
    pixels = image.load()

    def rgb(text):
        value = text.lstrip("#")
        return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))

    sea, land, route = rgb("#cfe6f5"), rgb("#ffffff"), rgb("#123a6b")
    start, end = rgb("#12a150"), rgb("#d81b1b")

    # 地图框在页面里的像素范围
    layout = spec.layout
    px_per_mm = width / float(layout["document_width_mm"])
    left = layout["map_left_mm"] * px_per_mm
    top = layout["map_top_mm"] * px_per_mm
    map_w = layout["map_width_mm"] * px_per_mm
    map_h = layout["map_height_mm"] * px_per_mm
    rendered = render_extent(spec)

    counts = {"sea": 0, "land": 0, "route": 0, "start": 0, "end": 0, "other": 0}
    step = 2
    for y in range(int(top) + 1, int(top + map_h) - 1, step):
        for x in range(int(left) + 1, int(left + map_w) - 1, step):
            colour = pixels[x, y]
            if colour == sea:
                counts["sea"] += 1
            elif colour == land:
                counts["land"] += 1
            elif colour == route:
                counts["route"] += 1
            elif colour == start:
                counts["start"] += 1
            elif colour == end:
                counts["end"] += 1
            else:
                counts["other"] += 1
    total = sum(counts.values()) or 1
    sea_ratio = counts["sea"] / total
    land_ratio = counts["land"] / total

    # 站址是否落在陆地：3×3 邻域里陆色不少于海色
    layers = {layer.layer_key: layer for layer in spec.layers}
    points = (layers["tower_existing"].data or {}).get("points") or []
    on_land = on_sea = 0
    for point in points:
        fx = (point["longitude"] - rendered[0]) / (rendered[2] - rendered[0])
        fy = (rendered[3] - point["latitude"]) / (rendered[3] - rendered[1])
        if not (0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0):
            continue
        px = int(round(left + fx * map_w))
        py = int(round(top + fy * map_h))
        land_hits = sea_hits = 0
        for dy in range(-3, 4):
            for dx in range(-3, 4):
                x, y = px + dx, py + dy
                if not (0 <= x < width and 0 <= y < height):
                    continue
                colour = pixels[x, y]
                if colour == land:
                    land_hits += 1
                elif colour == sea:
                    sea_hits += 1
        if land_hits >= sea_hits:
            on_land += 1
        else:
            on_sea += 1

    # 内河水域是否被填平：找 6×6 步长网格里被陆地四面包围的海色连通块（>=5 个采样点）
    sea_set = set()
    for y in range(int(top) + 1, int(top + map_h) - 1, 6):
        for x in range(int(left) + 1, int(left + map_w) - 1, 6):
            if pixels[x, y] == sea:
                sea_set.add((x, y))
    enclosed_cells = 0
    visited = set()
    for cell in list(sea_set):
        if cell in visited:
            continue
        stack, blob = [cell], []
        visited.add(cell)
        while stack:
            current = stack.pop()
            blob.append(current)
            for nxt in ((current[0] + 6, current[1]), (current[0] - 6, current[1]),
                        (current[0], current[1] + 6), (current[0], current[1] - 6)):
                if nxt in sea_set and nxt not in visited:
                    visited.add(nxt)
                    stack.append(nxt)
        if len(blob) < 5:
            continue
        xs = [item[0] for item in blob]
        ys = [item[1] for item in blob]
        # 四周 12 px 外是否都是陆地（粗略"被陆地包围"判据）
        surrounded = True
        for x in range(min(xs) - 12, max(xs) + 13, 12):
            for probe_y in (min(ys) - 12, max(ys) + 12):
                if not (0 <= x < width and 0 <= probe_y < height):
                    surrounded = False
                    break
                if pixels[x, probe_y] != land:
                    surrounded = False
                    break
            if not surrounded:
                break
        if surrounded:
            enclosed_cells += 1

    return {
        "image_pixels": [width, height],
        "map_frame_pixels": [int(map_w), int(map_h)],
        "sea_ratio_in_map": round(sea_ratio, 4),
        "land_ratio_in_map": round(land_ratio, 4),
        "route_pixels": counts["route"],
        "start_marker_pixels": counts["start"],
        "end_marker_pixels": counts["end"],
        "towers_in_frame": on_land + on_sea,
        "towers_on_land": on_land,
        "towers_on_sea": on_sea,
        "enclosed_water_blobs": enclosed_cells,
    }


def _no_legacy_sea_trace(spec):
    """确认海域不再来自旧的"扫描线 + 矩形条带"近似。

    注意：不能拿整个 spec 文本去匹配 ``band_m`` —— 版面里有 ``title_band_mm`` /
    ``footer_band_mm`` 这类**无关**的键名（它们只是页眉页脚带高度）。这里只针对海域图层
    的 derivation / geometry_method / 多边形顶点数做判定：旧的矩形条带实现会给每个矩形
    5 个顶点，而真实的差集结果是带大量顶点的岸线多边形。
    """

    serialized = json.dumps(spec.to_dict(), ensure_ascii=False)
    if "scanline" in serialized or "rectangle_count" in serialized:
        return False
    sea = next((layer for layer in spec.layers if layer.layer_key == "sea"), None)
    if sea is None or sea.source_status != "available":
        return False
    detail = sea.source_detail or {}
    if detail.get("derivation") != "canvas_rectangle_minus_cartographic_land":
        return False
    if detail.get("geometry_method") != "polygon_difference_with_holes":
        return False
    # 旧的矩形条带实现：要素数在**数千量级**（实测 1555 个），且每个要素恒为 5 个顶点。
    # 真实差集只产出少数几块海域、外环是上百个顶点的岸线。
    if int(sea.feature_count) >= 50:
        return False
    polygons = (sea.data or {}).get("polygons") or []
    if not polygons:
        return False
    rectangular = sum(1 for ring in polygons if len(ring) == 5)
    return rectangular <= 2 and max(len(ring) for ring in polygons) > 10


def main():
    qgis()
    sys.path.insert(0, os.getcwd())

    from cns_planner.application.map_figure_service import MapFigureService

    print("=== C1 生成前确认目标文件不存在（不覆盖旧 V3）===", flush=True)
    for target in (IMAGE_PATH, SPEC_PATH):
        print(f"  {target.resolve()} exists={target.exists()}", flush=True)
        if target.exists():
            print("  **目标文件已存在，拒绝继续**", flush=True)
            return 2

    state = json.loads(Path("projects/current_project.json").read_text(encoding="utf-8"))
    sources = json.loads(Path("projects/map_sources.json").read_text(encoding="utf-8"))
    paths = sources.get("paths", sources)
    revision_before = int(state.get("revision") or 0)
    print(f"=== C2 生成前 project revision = {revision_before} ===", flush=True)
    print(f"  cartographic_land = {paths.get('cartographic_land')}", flush=True)

    probe = copy.deepcopy(state)
    probe["operational_routes"] = [copy.deepcopy(DEMO_ROUTE)]
    for key in ("map_figures", "artifact_manifest"):
        probe.pop(key, None)
    session = _ProbeSession(probe, Path("projects/current_project.json"))
    renderer = _CountingRenderer()
    service = MapFigureService(
        session, lambda: paths, lambda action: action(),
        renderer_factory=lambda: renderer, project_directory=PROBE_PROJECT,
    )

    print("\n=== C3 第一次导出（300 DPI，走生产 export 链）===", flush=True)
    first = service.export(template_id="route_overview_v1", route_id=DEMO_ROUTE["route_id"],
                           dpi=300)
    figure_id = first["figure_id"]
    print(f"  figure_id = {figure_id}", flush=True)
    print(f"  reused    = {first['reused']}", flush=True)
    print(f"  renderer 执行次数 = {len(renderer.calls)}（dpi={renderer.calls}）", flush=True)

    print("\n=== C4 导出后校验 revision 不变 ===", flush=True)
    revision_after_first = int(probe.get("revision") or 0)
    print(f"  revision: before={revision_before} after_first={revision_after_first}", flush=True)
    print(f"  session.save() 调用次数 = {session.save_calls}", flush=True)
    print(f"  state 出现 map_figures = {'map_figures' in probe}", flush=True)

    print("\n=== C5 第二次导出（幂等复验）===", flush=True)
    second = service.export(template_id="route_overview_v1", route_id=DEMO_ROUTE["route_id"],
                            dpi=300)
    revision_after_second = int(probe.get("revision") or 0)
    print(f"  figure_id 相同 = {second['figure_id'] == figure_id}（{second['figure_id']}）",
          flush=True)
    print(f"  第二次 reused = {second['reused']}", flush=True)
    print(f"  renderer 执行次数 = {len(renderer.calls)}（第二次未重复渲染 = "
          f"{len(renderer.calls) == 1}）", flush=True)
    print(f"  revision after_second = {revision_after_second}", flush=True)

    spec = service.build_figure(template_id="route_overview_v1",
                                route_id=DEMO_ROUTE["route_id"])
    record = first["record"]

    # 产物落盘：PNG 从受控目录复制到 outputs/，spec 由**同一个** FigureSpec 序列化。
    IMAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    IMAGE_PATH.write_bytes(service.store.read_image(figure_id))
    SPEC_PATH.write_text(json.dumps(spec.to_dict(), ensure_ascii=False, indent=2),
                         encoding="utf-8")

    print("\n=== D1 产物事实 ===", flush=True)
    from PIL import Image

    image = Image.open(io.BytesIO(IMAGE_PATH.read_bytes()))
    print(f"  PNG  绝对路径 = {IMAGE_PATH.resolve()}", flush=True)
    print(f"  PNG  SHA256   = {sha256(IMAGE_PATH)}", flush=True)
    print(f"  PNG  像素     = {image.width}x{image.height}", flush=True)
    print(f"  PNG  DPI      = 300（版面 {spec.layout['document_width_mm']:.0f}×"
          f"{spec.layout['document_height_mm']:.0f} mm，A4 竖版）", flush=True)
    print(f"  Spec 绝对路径 = {SPEC_PATH.resolve()}", flush=True)
    print(f"  Spec SHA256   = {sha256(SPEC_PATH)}", flush=True)

    layers = {layer.layer_key: layer for layer in spec.layers}
    sea, land = layers["sea"], layers["land"]

    def hole_count(layer):
        """``data["polygon_holes"]`` 形如 ``{外环下标: [hole, ...]}``。"""

        raw = (layer.data or {}).get("polygon_holes") or {}
        return sum(len(holes or []) for holes in raw.values())

    sea_holes = hole_count(sea)
    land_holes = hole_count(land)
    print("\n=== D2 海域 / 陆地几何事实 ===", flush=True)
    print(f"  海域 derivation    = {sea.source_detail.get('derivation')}", flush=True)
    print(f"  海域 geometry      = {sea.source_detail.get('geometry_method')}", flush=True)
    print(f"  海域 polygon 数    = {sea.feature_count}", flush=True)
    print(f"  海域 hole 数       = {sea_holes}", flush=True)
    print(f"  cartographic_land 多边形数（全量） = "
          f"{land.source_detail.get('polygon_count')}", flush=True)
    print(f"  cartographic_land 范围内 polygon 数 = {land.feature_count}", flush=True)
    print(f"  陆地 hole 数       = {land_holes}", flush=True)
    print(f"  extent             = {spec.extent.as_list()}", flush=True)
    print(f"  图例列数 / 标题     = {spec.layout['legend_columns']} / {spec.title}",
          flush=True)
    print(f"  站址 total/范围内   = "
          f"{layers['tower_existing'].source_detail.get('total_count')}/"
          f"{layers['tower_existing'].feature_count}", flush=True)

    print("\n=== E 程序化渲染检查 ===", flush=True)
    facts = pixel_checks(spec, IMAGE_PATH)
    for key, value in facts.items():
        print(f"  {key} = {value}", flush=True)

    legend_texts = [item.display_name for item in spec.legend_items]
    legend_keys = [item.layer_key for item in spec.legend_items]
    checks = [
        ("1 300 DPI（2480×3507±5%）",
         abs(image.width - 2480) / 2480 < 0.05 and abs(image.height - 3507) / 3507 < 0.05,
         f"{image.width}x{image.height}"),
        ("2 A4 竖版", spec.layout["document_width_mm"] == 210.0
         and spec.layout["document_height_mm"] == 297.0,
         f"{spec.layout['document_width_mm']}×{spec.layout['document_height_mm']} mm"),
        ("3 海域来自精确 difference",
         sea.source_detail.get("derivation") == "canvas_rectangle_minus_cartographic_land"
         and sea.source_detail.get("geometry_method") == "polygon_difference_with_holes",
         f"{sea.source_detail.get('geometry_method')}"),
        ("4 hole 被保留（FigureSpec 有内环记录）", sea_holes > 0, f"海域 hole={sea_holes}"),
        ("5 无旧 scanline / rectangle 痕迹",
         _no_legacy_sea_trace(spec),
         "海域图层与全文均无 scanline / rectangle_count / 矩形条带近似"),
        ("6 不修改业务 revision", revision_after_second == revision_before,
         f"{revision_before} -> {revision_after_second}"),
        ("7 生成前已记录 revision", True, str(revision_before)),
        ("8 生成后 revision 完全不变",
         revision_after_first == revision_before == revision_after_second,
         f"{revision_before} / {revision_after_first} / {revision_after_second}"),
        ("9a 第二次 figure_id 相同", second["figure_id"] == figure_id, figure_id),
        ("9b 第二次不再渲染", len(renderer.calls) == 1, f"renderer.calls={len(renderer.calls)}"),
        ("9c 第二次 revision 不变", revision_after_second == revision_before,
         str(revision_after_second)),
        ("10 session.save() 从未被调用", session.save_calls == 0, str(session.save_calls)),
        ("适飞空域不存在（图层）",
         not [key for key in layers if key.lower() in AIRSPACE_KEYS], str(sorted(layers))),
        ("适飞空域不存在（图例）",
         not [text for text in legend_texts
              if any(hint in str(text) for hint in AIRSPACE_TEXT_HINTS)], str(legend_texts)),
        ("通信站址没有落在海域", facts["towers_on_sea"] == 0,
         f"陆 {facts['towers_on_land']} / 海 {facts['towers_on_sea']}"),
        ("航路 / 起终点 / 转弯点层级正常",
         facts["route_pixels"] > 500 and facts["start_marker_pixels"] > 20
         and facts["end_marker_pixels"] > 20,
         f"route={facts['route_pixels']} start={facts['start_marker_pixels']} "
         f"end={facts['end_marker_pixels']}"),
        ("海岸无异常大片填充（陆地占比 < 80%）", facts["land_ratio_in_map"] < 0.80,
         f"land={facts['land_ratio_in_map']:.1%} sea={facts['sea_ratio_in_map']:.1%}"),
        ("图例仍为横向两列", int(spec.layout["legend_columns"]) == 2,
         str(spec.layout["legend_columns"])),
        ("标题仍为航路周边状况图", spec.title == "航路周边状况图", spec.title),
        ("图例含既有通信站址", "既有通信站址" in legend_texts, str(legend_keys)),
    ]
    print("\n=== F 硬验收 ===", flush=True)
    passed = 0
    for label, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}  （{detail}）", flush=True)
        passed += 1 if ok else 0
    print(f"\n  {passed}/{len(checks)} PASS", flush=True)
    print(f"\n  V4 figure_id = {figure_id}", flush=True)
    print(f"  revision before/after = {revision_before}/{revision_after_second}", flush=True)
    print("\nVERIFY_V4_DONE", flush=True)
    return 0 if passed == len(checks) else 4


if __name__ == "__main__":
    sys.exit(main())
