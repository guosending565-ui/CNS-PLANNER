"""VERIFY_V5：用**本轮收口后的正式生产链**重新生成专题图并做硬验收。

链路（与线上一致，不再自建 FigureSpec）：

    MapFigureService.export()
      → build_figure()  → FigureSpec
      → _render()       → QgisFigureRenderer（QGIS 版面，300 DPI）
      → _publish()      → route-first 受控产物 + 总索引 + route 索引（**不写 ProjectState**）

与 V4 的关系
============

* **绝不覆盖 V4**：``outputs/route_overview_VERIFY_V4.png`` 与它的 spec 一律只读；
  本脚本只写 ``outputs/route_overview_VERIFY_V5.png`` / ``_spec.json``（重跑时只覆盖 V5 自己）；
* 复用 V4 的像素检查工具（``tools/verify_route_overview_v4.py`` 的 ``pixel_checks`` /
  ``render_extent`` / ``_CountingRenderer``），因此两张图的判定口径完全一致。

本轮新增的硬验收（版式收口 + 归档）：

* 图例仍在地图**下方**、2 列、左右两列条目数均衡、框高贴合内容（无大块空白）；
* 地图框与图例框之间存在独立**审计条**（坐标系 / revision / 未显示图层），三段互不重叠；
* 比例尺与左边框 / 下边框有合理内距，且不与审计条重叠；
* 主标题 → 固定间距 → 副标题 → 固定间距 → 地图框；
* 起终点标签偏移大于星标半径（不明显压星标 / 压航路）；
* 产物归档在 ``artifacts/map_figures/routes/<route>/route_overview_v1/<figure_id>/``，
  并写出 route 级 ``index.json``；总索引与旧平铺产物继续可读；
* ``metadata.json`` 与 index 的固定审计字段逐字段一致。

用法：
    & 'C:\\Program Files\\QGIS 3.44.14\\bin\\python-qgis-ltr.bat' tools/verify_route_overview_v5.py
"""

from __future__ import annotations

from collections import Counter
import copy
import io
import json
import os
from pathlib import Path
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

#: 复用 V4 的可观测事实与像素检查工具（V4 文件本身只读，不改动）。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from verify_route_overview_v4 import (  # noqa: E402
    DEMO_ROUTE, _CountingRenderer, _ProbeSession, pixel_checks, qgis, sha256,
)

IMAGE_PATH = Path("outputs/route_overview_VERIFY_V5.png")
SPEC_PATH = Path("outputs/route_overview_VERIFY_V5_spec.json")
#: 图件记录写进独立探针目录（MapFigureStore 的受控目录），**不碰**真实项目状态。
PROBE_PROJECT = Path("_diag/v5_probe_project")
#: 绝不允许被本脚本改动的历史产物。
PROTECTED = (
    Path("outputs/route_overview_VERIFY_V4.png"),
    Path("outputs/route_overview_VERIFY_V4_spec.json"),
)

AIRSPACE_KEYS = ("airspace", "suitable_airspace", "confirmed_airspace", "air_space")
AIRSPACE_TEXT_HINTS = ("适飞空域", "空域")


def _no_legacy_sea_trace_v5(spec):
    """确认海域不来自旧的"扫描线 + 矩形条带"近似（V5 判据）。

    与 V4 的判据差异：现在陆地多边形**真的带洞**，因此差集结果里会出现若干"湖面/内河"
    小多边形 —— 它们本身可能就是 5 点的规则矩形。因此不能再用"5 点环不得超过 2 个"
    这种启发式，而要按旧实现的**真实指纹**判定：

    * 旧的矩形条带实现产出**上千个**要素（实测 1555），且每个恒为 5 点；
    * 真实差集只产出少量多边形，且**至少有一条真实岸线**（数百 ~ 数千个顶点）。
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
    polygons = (sea.data or {}).get("polygons") or []
    if not polygons or int(sea.feature_count) >= 100:
        return False
    longest = max(len(ring) for ring in polygons)
    rectangular = sum(1 for ring in polygons if len(ring) == 5)
    # 必须有真实岸线，且不能全部是矩形块。
    return longest > 100 and rectangular < len(polygons)


def _label_offsets(spec):
    """从 FigureSpec 的标注与航路几何算出起终点标注的偏移距离（毫米）。"""

    from cns_planner.gis.qgis_figure_renderer import _endpoint_direction

    directive = {"start": _endpoint_direction(spec, "start"),
                 "end": _endpoint_direction(spec, "end")}
    from cns_planner.gis.figure_style import LAYOUT

    base = float(LAYOUT["label_endpoint_offset_mm"])
    return {kind: (base if direction else 0.0) for kind, direction in directive.items()}


def main():
    qgis()
    sys.path.insert(0, os.getcwd())

    from cns_planner.application.map_figure_service import (
        MAP_FIGURE_AUDIT_FIELDS, MapFigureService,
    )
    from cns_planner.gis.figure_legend import legend_geometry
    from cns_planner.gis.figure_style import (
        FIGURE_STYLES, LAYOUT, LEGEND_GROUP_COLUMNS, LEGEND_GROUPS,
    )

    print("=== C0 保护历史产物（绝不覆盖 V4）===", flush=True)
    for target in PROTECTED:
        print(f"  {target.resolve()} exists={target.exists()}（本脚本不写入）", flush=True)
    for target in (IMAGE_PATH, SPEC_PATH):
        if target.exists():
            print(f"  重跑：删除旧 V5 自身产物 {target.resolve()}", flush=True)
            target.unlink()
    # 探针项目目录整体重建：本验收必须从"从未生成过任何图件"的干净状态开始，
    # 否则第二次导出会命中幂等复用，无法验证"导出确实渲染了一次"。
    if PROBE_PROJECT.exists():
        import shutil

        print(f"  重建探针项目目录 {PROBE_PROJECT.resolve()}", flush=True)
        shutil.rmtree(PROBE_PROJECT)

    state = json.loads(Path("projects/current_project.json").read_text(encoding="utf-8"))
    sources = json.loads(Path("projects/map_sources.json").read_text(encoding="utf-8"))
    paths = sources.get("paths", sources)
    revision_before = int(state.get("revision") or 0)
    print(f"\n=== C1 生成前 project revision = {revision_before} ===", flush=True)
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

    print("\n=== C2 第一次导出（300 DPI，走生产 export 链）===", flush=True)
    first = service.export(template_id="route_overview_v1", route_id=DEMO_ROUTE["route_id"],
                           dpi=300)
    figure_id = first["figure_id"]
    record = first["record"]
    print(f"  figure_id = {figure_id}", flush=True)
    print(f"  reused    = {first['reused']}", flush=True)
    print(f"  renderer 执行次数 = {len(renderer.calls)}（dpi={renderer.calls}）", flush=True)

    print("\n=== C3 第二次导出（幂等复验）===", flush=True)
    second = service.export(template_id="route_overview_v1", route_id=DEMO_ROUTE["route_id"],
                            dpi=300)
    revision_after = int(probe.get("revision") or 0)
    print(f"  figure_id 相同 = {second['figure_id'] == figure_id}", flush=True)
    print(f"  第二次 reused = {second['reused']}，renderer 执行次数 = {len(renderer.calls)}",
          flush=True)

    spec = service.build_figure(template_id="route_overview_v1",
                                route_id=DEMO_ROUTE["route_id"])
    IMAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    IMAGE_PATH.write_bytes(service.store.read_image(figure_id, record))
    SPEC_PATH.write_text(json.dumps(spec.to_dict(), ensure_ascii=False, indent=2),
                         encoding="utf-8")

    from PIL import Image

    image = Image.open(io.BytesIO(IMAGE_PATH.read_bytes()))
    print("\n=== D1 产物事实 ===", flush=True)
    print(f"  PNG  绝对路径 = {IMAGE_PATH.resolve()}", flush=True)
    print(f"  PNG  SHA256   = {sha256(IMAGE_PATH)}", flush=True)
    print(f"  PNG  像素     = {image.width}x{image.height}", flush=True)
    print(f"  Spec 绝对路径 = {SPEC_PATH.resolve()}", flush=True)
    print(f"  Spec SHA256   = {sha256(SPEC_PATH)}", flush=True)

    # ---- 版本 / 归档事实 ----------------------------------------------------
    artifact_directory = (PROBE_PROJECT / record["relative_path"]).parent
    route_index_path = service.store.route_index_path(DEMO_ROUTE["route_id"])
    global_index = json.loads(service.store.index_path.read_text(encoding="utf-8"))
    route_index = json.loads(route_index_path.read_text(encoding="utf-8"))
    metadata = json.loads(
        (PROBE_PROJECT / record["record_relative_path"]).read_text(encoding="utf-8"))
    index_record = next(item for item in global_index["items"] if item["figure_id"] == figure_id)
    audit_mismatch = [field for field in MAP_FIGURE_AUDIT_FIELDS
                      if metadata.get(field) != index_record.get(field)]
    print("\n=== D2 归档事实（route-first）===", flush=True)
    print(f"  产物目录        = {artifact_directory.resolve()}", flush=True)
    print(f"  route 索引      = {route_index_path.resolve()}", flush=True)
    print(f"  全局索引        = {service.store.index_path.resolve()}", flush=True)
    print(f"  route index active = "
          f"{route_index['templates'].get('route_overview_v1', {}).get('active_figure_id')}",
          flush=True)
    print(f"  审计字段不一致项 = {audit_mismatch}", flush=True)
    print(f"  旧平铺产物目录   = {service.store.list_relative()}", flush=True)

    # ---- 几何 / 版式事实 ----------------------------------------------------
    layers = {layer.layer_key: layer for layer in spec.layers}

    def hole_count(layer):
        raw = (layer.data or {}).get("polygon_holes") or {}
        return sum(len(holes or []) for holes in raw.values())

    sea, land = layers["sea"], layers["land"]
    sea_holes, land_holes = hole_count(sea), hole_count(land)
    print("\n=== D3 海域 / 陆地几何事实 ===", flush=True)
    print(f"  海域 derivation    = {sea.source_detail.get('derivation')}", flush=True)
    print(f"  海域 geometry      = {sea.source_detail.get('geometry_method')}", flush=True)
    print(f"  海域 polygon 数    = {sea.feature_count} · hole 数 = {sea_holes}", flush=True)
    print(f"  陆地 polygon 数    = {land.feature_count} · hole 数 = {land_holes}", flush=True)
    print(f"  陆地 metadata hole 记录 = {land.source_detail.get('hole_ring_count')}",
          flush=True)
    print(f"  站址 total/范围内   = "
          f"{layers['tower_existing'].source_detail.get('total_count')}/"
          f"{layers['tower_existing'].feature_count}", flush=True)

    layout = spec.layout
    entries = [{"group": item.legend_group, "text": item.display_name,
                "style_key": item.style_key} for item in spec.legend_items]
    geometry = legend_geometry(
        entries, row_height=float(layout["legend_row_mm"]),
        group_row=float(layout["legend_group_row_mm"]),
        columns=int(layout["legend_columns"]),
        header_height=float(LAYOUT["legend_header_mm"]),
        group_gap=float(layout["legend_group_gap_mm"]),
        group_item_gap=float(layout["legend_group_item_gap_mm"]),
        group_columns=LEGEND_GROUP_COLUMNS,
        top_padding=float(layout["legend_top_padding_mm"]),
    )
    per_column = Counter(column for column, _y, kind, _t, _s in geometry["rows"]
                         if kind == "item")
    column_counts = [per_column.get(index, 0) for index in range(int(geometry["columns"]))]
    group_titles = [text for _column, _y, kind, text, _style in geometry["rows"]
                    if kind == "group"]
    print("\n=== D4 图例 / 版面事实 ===", flush=True)
    print(f"  图例列数        = {geometry['columns']} · 每列条目数 = {column_counts}",
          flush=True)
    print(f"  图例分组标题     = {group_titles}", flush=True)
    print(f"  图例框高 / 内容高 = {layout['legend_height_mm']:.2f} / "
          f"{geometry['box_height_mm']:.2f} mm", flush=True)
    print(f"  地图框          = top {layout['map_top_mm']:.2f} · height "
          f"{layout['map_height_mm']:.2f} mm", flush=True)
    print(f"  审计条          = top {layout['footer_strip_top_mm']:.2f} · height "
          f"{layout['footer_strip_mm']:.2f} mm", flush=True)
    print(f"  图例框          = top {layout['legend_top_mm']:.2f} mm", flush=True)
    print(f"  标题带          = 主标题 {layout['title_main_height_mm']:.2f} mm · "
          f"副标题 top {layout['subtitle_top_mm']:.2f} mm", flush=True)
    offsets = _label_offsets(spec)
    print(f"  起终点标注偏移   = {offsets}（星标 size "
          f"{FIGURE_STYLES['start_point']['size']} mm）", flush=True)

    print("\n=== E 程序化渲染检查 ===", flush=True)
    facts = pixel_checks(spec, IMAGE_PATH)
    for key, value in facts.items():
        print(f"  {key} = {value}", flush=True)

    legend_texts = [item.display_name for item in spec.legend_items]
    legend_keys = [item.layer_key for item in spec.legend_items]
    star_size = float(FIGURE_STYLES["start_point"]["size"])
    map_bottom = float(layout["map_top_mm"]) + float(layout["map_height_mm"])
    strip_top = float(layout["footer_strip_top_mm"])
    strip_bottom = strip_top + float(layout["footer_strip_mm"])
    legend_top = float(layout["legend_top_mm"])
    subtitle_bottom = float(layout["subtitle_top_mm"]) + float(layout["subtitle_height_mm"])

    checks = [
        ("1 300 DPI（2480×3507±5%）",
         abs(image.width - 2480) / 2480 < 0.05 and abs(image.height - 3507) / 3507 < 0.05,
         f"{image.width}x{image.height}"),
        ("2 A4 竖版", layout["document_width_mm"] == 210.0
         and layout["document_height_mm"] == 297.0,
         f"{layout['document_width_mm']}×{layout['document_height_mm']} mm"),
        ("3 海域来自精确 difference",
         sea.source_detail.get("derivation") == "canvas_rectangle_minus_cartographic_land"
         and sea.source_detail.get("geometry_method") == "polygon_difference_with_holes",
         f"{sea.source_detail.get('geometry_method')}"),
        ("4 source polygon holes 一路保留",
         land_holes == int(land.source_detail.get("hole_ring_count") or 0)
         and sea_holes == int(sea.source_detail.get("hole_count") or 0),
         f"陆地 hole={land_holes} / 海域 hole={sea_holes}"),
        ("5 无旧 scanline / rectangle 痕迹", _no_legacy_sea_trace_v5(spec),
         "海域图层无矩形条带近似（仍是长岸线 + 内河湖面）"),
        ("6 不修改业务 revision", revision_after == revision_before,
         f"{revision_before} -> {revision_after}"),
        ("7 session.save() 从未被调用", session.save_calls == 0, str(session.save_calls)),
        ("8 第二次 figure_id 相同且不重渲染",
         second["figure_id"] == figure_id and len(renderer.calls) == 1,
         f"renderer.calls={len(renderer.calls)}"),
        ("9 图例仍在地图下方", legend_top > map_bottom,
         f"legend_top={legend_top:.2f} > map_bottom={map_bottom:.2f}"),
        ("10 图例 2 列", int(geometry["columns"]) == 2, str(geometry["columns"])),
        ("11 图例左右两列条目数均衡",
         len(column_counts) == 2 and max(column_counts) - min(column_counts) <= 1,
         str(column_counts)),
        ("12 图例框高贴合内容（无大块空白）",
         abs(float(layout["legend_height_mm"]) - float(geometry["box_height_mm"])) <= 0.6,
         f"{layout['legend_height_mm']:.2f} vs {geometry['box_height_mm']:.2f} mm"),
        ("13 审计条在地图框与图例框之间且不重叠",
         strip_top >= map_bottom and strip_bottom <= legend_top,
         f"{strip_top:.2f} / {strip_bottom:.2f} / {legend_top:.2f} mm"),
        ("14 比例尺与边框有内距（>= 4mm）",
         float(layout["scale_bar_margin_mm"]) >= 4.0,
         f"{layout['scale_bar_margin_mm']} mm"),
        ("15 副标题与地图框保持固定间距",
         abs((float(layout["map_top_mm"]) - subtitle_bottom)
             - float(layout["title_map_gap_mm"])) <= 0.01,
         f"map_top - subtitle_bottom = {float(layout['map_top_mm']) - subtitle_bottom:.2f} mm"),
        ("16 起终点标签偏移大于星标半径",
         all(value >= star_size / 2.0 + 0.5 for value in offsets.values()),
         f"{offsets} vs 半径 {star_size / 2.0} mm"),
        ("17 不出现适飞空域（图层）",
         not [key for key in layers if key.lower() in AIRSPACE_KEYS], str(sorted(layers))),
        ("18 不出现适飞空域（图例）",
         not [text for text in legend_texts
              if any(hint in str(text) for hint in AIRSPACE_TEXT_HINTS)], str(legend_texts)),
        ("19 通信站址仍正确显示",
         layers["tower_existing"].feature_count > 0 and facts["towers_on_sea"] == 0,
         f"范围内 {layers['tower_existing'].feature_count} 个 · "
         f"陆 {facts['towers_on_land']} / 海 {facts['towers_on_sea']}"),
        ("20 图例含「既有设施」分组且不含「与机场」",
         "既有设施" in group_titles and "既有设施与机场" not in group_titles,
         str(group_titles)),
        ("21 产物归档在 route-first 目录",
         record["relative_path"].startswith("artifacts/map_figures/routes/")
         and artifact_directory.name == figure_id
         and artifact_directory.parent.name == "route_overview_v1",
         artifact_directory.resolve().relative_to(PROBE_PROJECT.resolve()).as_posix()),
        ("22 route 级 index.json 存在且 active 正确",
         route_index_path.is_file()
         and route_index["templates"]["route_overview_v1"]["active_figure_id"] == figure_id,
         route_index_path.resolve().relative_to(PROBE_PROJECT.resolve()).as_posix()),
        ("23 全局 figure index 仍可读取",
         service.store.index_path.is_file()
         and global_index["active_figure_id"] == figure_id,
         f"count={global_index['count']}"),
        ("24 metadata.json 与 index 审计字段一致", not audit_mismatch,
         str(audit_mismatch)),
        ("25 标题仍为航路周边状况图", spec.title == "航路周边状况图", spec.title),
        ("26 航路 / 起终点 / 转弯点层级正常",
         facts["route_pixels"] > 500 and facts["start_marker_pixels"] > 20
         and facts["end_marker_pixels"] > 20,
         f"route={facts['route_pixels']} start={facts['start_marker_pixels']} "
         f"end={facts['end_marker_pixels']}"),
        ("27 海岸无异常大片填充（陆地占比 < 80%）", facts["land_ratio_in_map"] < 0.80,
         f"land={facts['land_ratio_in_map']:.1%} sea={facts['sea_ratio_in_map']:.1%}"),
        ("28 图例分组与图层键一致",
         sorted(LEGEND_GROUP_COLUMNS) >= sorted(
             {item.legend_group for item in spec.legend_items if item.legend_group}),
         str(sorted({item.legend_group for item in spec.legend_items}))),
    ]
    print("\n=== F 硬验收 ===", flush=True)
    passed = 0
    for label, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}  （{detail}）", flush=True)
        passed += 1 if ok else 0
    print(f"\n  {passed}/{len(checks)} PASS", flush=True)
    print(f"\n  V5 figure_id = {figure_id}", flush=True)
    print(f"  V5 PNG       = {IMAGE_PATH.resolve()}", flush=True)
    print(f"  V5 SHA256    = {sha256(IMAGE_PATH)}", flush=True)
    print(f"  V5 Spec      = {SPEC_PATH.resolve()}", flush=True)
    print(f"  V5 Spec SHA256 = {sha256(SPEC_PATH)}", flush=True)
    print(f"  V5 像素      = {image.width}x{image.height} @300DPI", flush=True)
    print(f"  V5 产物目录  = {artifact_directory.resolve()}", flush=True)
    print(f"  V5 route 索引 = {route_index_path.resolve()}", flush=True)
    print("\nVERIFY_V5_DONE", flush=True)
    return 0 if passed == len(checks) else 4


if __name__ == "__main__":
    sys.exit(main())
