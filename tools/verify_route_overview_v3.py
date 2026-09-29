"""VERIFY_V3：用**生产链路**生成 route_overview_VERIFY_V3 并做程序化硬验收。

与 V2 的差异（本轮任务：专题图海陆底图纠错 + 航路总览范围收口）：

* 陆地底图改用**制图专用**的 ``cartographic_land``（不是业务 ``land_mask``），
  FigureSpec 的 ``source_detail`` 必须记录 role / source / layer / CRS /
  feature_count / coverage_reason；
* 海域必须是"地图画布矩形 − cartographic_land"的真实面积差集；
* extent 收口为"航路 bbox + 10 km 缓冲"，且**不得**因为站址而扩大；
* 站址图例名必须是"既有通信站址"。

用法：
    & 'C:\\Program Files\\QGIS 3.44.14\\bin\\python-qgis-ltr.bat' tools/verify_route_overview_v3.py
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

IMAGE_PATH = Path("outputs/route_overview_VERIFY_V3.png")
SPEC_PATH = Path("outputs/route_overview_VERIFY_V3_spec.json")

#: 与 render_route_overview_preview.py 相同的演示航路（仅内存，不写项目状态）。
DEMO_ROUTE = [
    [122.1067, 30.0167],
    [122.1800, 30.0400],
    [122.2450, 30.0200],
    [122.3100, 29.9750],
    [122.3900, 29.9600],
]

AIRSPACE_LAYER_KEYS = ("airspace", "suitable_airspace", "confirmed_airspace", "air_space")
AIRSPACE_TEXT_HINTS = ("适飞空域", "空域")

#: 真·空图层：状态 unknown 且 feature_count = 0 —— 只能进 metadata / omitted_layers。
UNKNOWN_EMPTY_KEYS = ("tower_obstacle", "airport", "airport_protection")


_QGIS_APPLICATION = None


def _qgis():
    """初始化 QGIS 运行时，并在**模块级持有引用**。

    QgsApplication 若只被局部变量引用，函数返回后可能被 GC；之后任何字体/版面调用都会在
    没有 QGuiApplication 的情况下访问 Qt（表现为 ``Must construct a QGuiApplication`` 与
    随之而来的 Qt fail-fast 进程中止）。因此这里必须持有到模块级全局。
    """

    global _QGIS_APPLICATION
    from qgis.core import QgsApplication

    _QGIS_APPLICATION = QgsApplication.instance() or QgsApplication([], False)
    _QGIS_APPLICATION.initQgis()
    return _QGIS_APPLICATION


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _render_extent(spec):
    """与渲染器一致的"实际渲染范围"（把 extent 撑到地图框长宽比）。"""

    plan = spec.layout or {}
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


def main():
    _qgis()
    sys.path.insert(0, os.getcwd())

    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--dump-spec", default=None,
                        help="只构建 FigureSpec 并写到该路径（不渲染），用于离线诊断")
    arguments = parser.parse_args()

    from cns_planner.application.map_figure_service import MapFigureService
    from cns_planner.gis import qgis_figure_renderer as renderer_module

    print("=== C1 生成前确认目标文件不存在 ===", flush=True)
    for target in (IMAGE_PATH, SPEC_PATH):
        if target.exists():
            print(f"  {target.resolve()} 已存在，拒绝继续（避免旧文件冒充新结果）", flush=True)
            return 2
        print(f"  {target.resolve()} exists=False", flush=True)

    state = json.loads(Path("projects/current_project.json").read_text(encoding="utf-8"))
    sources = json.loads(Path("projects/map_sources.json").read_text(encoding="utf-8"))
    paths = sources.get("paths", sources)
    print(f"  cartographic_land = {paths.get('cartographic_land')}", flush=True)
    print(f"  land_mask（业务，保持不动）= {paths.get('land_mask')}", flush=True)

    service = MapFigureService(
        _ProbeSession(state, Path("projects/current_project.json")),
        lambda: paths, lambda action: action(),
    )
    listing = service.catalog()
    print(f"  catalog.available_template_ids = {listing['available_template_ids']}", flush=True)

    state_for_build = copy.deepcopy(state)
    state_for_build["operational_routes"] = [{
        "route_id": "DEMO-舟山演示航路", "status": "demo", "path_crs": "OGC:CRS84",
        "kind": "demo_route_for_cartography_preview_only", "path": DEMO_ROUTE,
        "start": {"name": "舟山本岛（演示）"}, "end": {"name": "普陀山以东（演示）"},
        "provenance": {"source_type": "demo_route_in_memory", "not_project_state": True},
    }]
    service.session.state = state_for_build

    try:
        spec = service.build_figure(template_id="route_overview_v1")
    except Exception:
        import traceback

        traceback.print_exc()
        print("**build_figure 失败：不输出任何图件**", flush=True)
        return 4

    if arguments.dump_spec:
        Path(arguments.dump_spec).write_text(
            json.dumps(spec.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8",
        )
        print(f"  仅导出 FigureSpec（未渲染）：{Path(arguments.dump_spec).resolve()}", flush=True)
        for layer in spec.layers:
            print(f"    {layer.layer_key:20s} status={layer.source_status:12s} "
                  f"features={layer.feature_count}", flush=True)
        return 0

    layers = {layer.layer_key: layer for layer in spec.layers}
    legend_keys = [item.layer_key for item in spec.legend_items]
    legend_texts = [item.display_name for item in spec.legend_items]
    renderer_source = Path(renderer_module.__file__).resolve()

    print("\n=== D 渲染前运行时事实 ===", flush=True)
    runtime = {
        "title": spec.title,
        "renderer_source_file": str(renderer_source),
        "layer_ids": [layer.layer_key for layer in spec.layers],
        "legend_keys": legend_keys,
        "legend_texts": legend_texts,
        "legend_columns": int(spec.layout.get("legend_columns") or 0),
        "legend_height_mm": float(spec.layout.get("legend_height_mm") or 0.0),
        "map_height_mm": float(spec.layout.get("map_height_mm") or 0.0),
        "extent": spec.extent.as_list(),
        "extent_width_km": spec.extent.width_km,
        "extent_height_km": spec.extent.height_km,
        "extent_buffer_km": (spec.parameters or {}).get("extent_buffer_km"),
        "north_arrow_style": renderer_module.QgisFigureRenderer.north_arrow_style,
        "subtitle": renderer_module.spec_subtitle(spec),
    }
    for key, value in runtime.items():
        print(f"  {key} = {value}", flush=True)

    print("\n=== D2 陆海底图来源事实（FigureSpec.source_detail）===", flush=True)
    land = layers["land"]
    sea = layers["sea"]
    for label, layer in (("land", land), ("sea", sea)):
        detail = layer.source_detail or {}
        print(f"  [{label}] source_role={layer.source_role} "
              f"source_status={layer.source_status} feature_count={layer.feature_count}",
              flush=True)
        for key in ("role", "path", "crs", "derivation", "polygon_count",
                    "coverage_reason", "semantics", "is_business_surface_evidence",
                    "building_dilation_px", "threshold_m"):
            print(f"      {key} = {detail.get(key)}", flush=True)

    print("\n=== D3 extent 与站址筛选 ===", flush=True)
    extent = spec.extent
    route = [[float(point[0]), float(point[1])] for point in DEMO_ROUTE]
    route_west = min(point[0] for point in route)
    route_east = max(point[0] for point in route)
    rendered = _render_extent(spec)
    rendered_width = rendered[2] - rendered[0]
    route_span = route_east - route_west
    width_ratio = route_span / rendered_width if rendered_width else 0.0
    print(f"  航路经度跨度 = {route_span:.6f}°", flush=True)
    print(f"  实际渲染范围（与渲染器同口径）= "
          f"{[round(value, 6) for value in rendered]}", flush=True)
    print(f"  渲染宽度 = {rendered_width:.6f}°", flush=True)
    print(f"  航路占地图宽度 = {width_ratio * 100:.1f}%", flush=True)
    tower_layer = layers.get("tower_existing")
    tower_detail = (tower_layer.source_detail or {}) if tower_layer else {}
    print(f"  站址 total={tower_detail.get('total_count')} "
          f"within_extent={tower_detail.get('within_extent')} "
          f"out_of_extent={tower_detail.get('out_of_extent')}", flush=True)

    print("\n=== D4 真·空图层是否被排除在可渲染图层与图例之外 ===", flush=True)
    for key in UNKNOWN_EMPTY_KEYS:
        layer = layers.get(key)
        if layer is None:
            print(f"  {key}: 未 materialize", flush=True)
            continue
        print(f"  {key}: status={layer.source_status} feature_count={layer.feature_count} "
              f"in_legend={key in legend_keys}", flush=True)
    omitted_keys = [item["layer_key"] for item in spec.omitted_layers]
    print(f"  omitted_layers = {omitted_keys}", flush=True)

    print("\n=== D5 生成前置门禁 ===", flush=True)
    land_detail = land.source_detail or {}
    sea_detail = sea.source_detail or {}
    coverage = str(land_detail.get("coverage_reason") or "")
    gates = [
        ("title == 航路周边状况图", spec.title == "航路周边状况图"),
        ("无 airspace 图层",
         not [key for key in layers if key.lower() in AIRSPACE_LAYER_KEYS]
         and not [text for text in legend_texts
                  if any(hint in str(text) for hint in AIRSPACE_TEXT_HINTS)]),
        ("陆地层来自 cartographic_land", land.source_role == "cartographic_land"
         and land.source_status == "available" and land.feature_count > 0),
        ("陆地层不来自 land_mask", land.source_role != "land_mask"),
        ("陆地层 source_detail 记录 role/path/crs/feature_count/coverage_reason",
         land_detail.get("role") == "cartographic_land"
         and bool(land_detail.get("path")) and bool(land_detail.get("crs"))
         and bool(land_detail.get("coverage_reason")) and land.feature_count > 0),
        ("海域 = 画布矩形 − 陆地（面积差集）",
         sea.source_status == "available" and sea.feature_count > 0
         and sea_detail.get("derivation") == "canvas_rectangle_minus_cartographic_land"),
        ("海域不被当作业务证据", sea_detail.get("is_business_surface_evidence") is False
         and land_detail.get("is_business_surface_evidence") is False),
        ("图例含「既有通信站址」", "既有通信站址" in legend_texts),
        ("图例不含「既有铁塔」", "既有铁塔" not in legend_texts),
        ("真·空图层不入图例",
         not any(key in legend_keys for key in UNKNOWN_EMPTY_KEYS)),
        ("extent buffer == 10 km", float((spec.parameters or {}).get("extent_buffer_km")) == 10.0),
        ("extent 宽度 <= 航路跨度 + 2*10km + 5%",
         spec.extent.width_km <= route_span * 111.32 * math.cos(math.radians(30.0))
         + 2 * 10.0 + 2.0),
        ("航路占地图宽度 55%~75%", 0.55 <= width_ratio <= 0.75),
        ("legend_columns >= 2", int(runtime["legend_columns"]) >= 2),
        ("north_arrow_style == simple", runtime["north_arrow_style"] == "simple"),
        ("renderer 源文件属于当前工作树",
         str(renderer_source).startswith(str(Path(os.getcwd()).resolve()))),
    ]
    for label, ok in gates:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}", flush=True)
    if not all(ok for _, ok in gates):
        print("  **前置门禁未通过：停止生成，不输出 PNG**", flush=True)
        return 3

    print("\n=== C2 渲染（生产 renderer）===", flush=True)
    print("  即将渲染的图层与要素数：", flush=True)
    for layer in spec.layers:
        print(f"    {layer.layer_key:20s} {layer.source_status:12s} {layer.feature_count}",
              flush=True)
    if arguments.dump_spec:
        Path(arguments.dump_spec).write_text(
            json.dumps(spec.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  FigureSpec 已保存到 {arguments.dump_spec}（渲染前快照）", flush=True)
    # 导出 DPI：本机 QGIS 版面在 300 dpi（约 36 MB 位图 + 上千个面要素）下会触发
    # Qt 的 fail-fast（0xC0000409）间歇性中止渲染；200 dpi 是 A4 竖版仍远高于印刷
    # 需要的分辨率（1654×2339 px），因此本轮固定用 200 dpi 保证出图稳定可复现。
    render_dpi = 200.0
    image = renderer_module.QgisFigureRenderer().render(spec, dpi=render_dpi)
    print("  渲染器已返回 PNG 字节", flush=True)
    IMAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    IMAGE_PATH.write_bytes(image)
    SPEC_PATH.write_text(json.dumps(spec.to_dict(), ensure_ascii=False, indent=2),
                         encoding="utf-8")

    from PIL import Image

    pil = Image.open(io.BytesIO(image))
    print("  新 PNG 绝对路径:", IMAGE_PATH.resolve(), flush=True)
    print("  新 PNG mtime  :",
          datetime.datetime.fromtimestamp(IMAGE_PATH.stat().st_mtime).isoformat(), flush=True)
    print("  新 PNG 字节数 :", IMAGE_PATH.stat().st_size, flush=True)
    print("  新 PNG SHA256 :", sha256(IMAGE_PATH), flush=True)
    print("  新 PNG 像素   :", f"{pil.width}x{pil.height}", flush=True)
    print("  渲染 DPI      :", render_dpi, flush=True)
    print("  Spec 绝对路径 :", SPEC_PATH.resolve(), flush=True)
    print("  Spec SHA256   :", sha256(SPEC_PATH), flush=True)

    # 图例名一致性：图例文字必须与 FigureSpec 的名称逐个一致。
    legend_names = {item.layer_key: item.display_name for item in spec.legend_items}
    from cns_planner.reporting.map_templates import LAYER_DISPLAY_NAMES

    for key in legend_keys:
        expected = LAYER_DISPLAY_NAMES.get(key)
        if expected:
            matched = legend_names.get(key) == expected
            print(f"  [{'PASS' if matched else 'FAIL'}] 图例名 {key} == {expected} "
                  f"（实际 {legend_names.get(key)}）", flush=True)

    print("\nVERIFY_V3_DONE", flush=True)
    return 0


class _ProbeSession:
    """最小 session 替身：build_figure 只读 state，不写盘。"""

    def __init__(self, state, store_path):
        self.state = state
        self.store_path = store_path

    def save(self):  # pragma: no cover - 本脚本刻意不落盘
        raise AssertionError("验证脚本不得写项目状态")


if __name__ == "__main__":
    sys.exit(main())
