"""C3-C：从**同一份 run manifest** 渲染 R0005 六类 QGIS 专题图（A4 / 300 DPI）。

必须用 QGIS 自带解释器运行：

    & 'C:\\Program Files\\QGIS 3.44.14\\bin\\python-qgis-ltr.bat' `
        tools/render_r0005_research_figures.py `
        --run-root <RESEARCH_RUNS> `
        --run-id c3b-20261027-d `
        --output <FIGURES_OUTPUT>

边界
====

* 只读：不调用求解器、不写 ProjectState / Git / 原始数据；
* 单一数据源：图层、状态、计数与身份全部来自该 run 的 ``run_manifest.json``；
  逐阶段校验 ``results/<stage>.json`` 的 SHA 与 manifest 一致；
* 不重算：覆盖圈只投影设备输入半径，不重新计算业务覆盖；
* Radar：只画“能力限制”声明，**不画任何有效覆盖几何**；
* 图面身份：每张图的地图框下沿披露条写入 run_id / input_version / 结果清单 SHA /
  N 动作数，FigureSpec.metadata 保存完整身份与阶段 SHA。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
from pathlib import Path
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

A4_LAYOUT = {
    "document_width_mm": 210.0, "document_height_mm": 297.0,
    "margin_mm": 14.0, "legend_columns": 4,
    "layout_profile": "cns_five_figure_v2", "audit_footer": False,
}
TEMPLATES = (
    ("cns_communication_v1", "communication"),
    ("cns_navigation_v1", "navigation"),
    ("cns_surveillance_v1", "rid"),
    ("cns_surveillance_v1", "radar"),
    ("cns_combined_v1", "combined"),
    ("route_overview_v1", "route"),
)
SHORT = {
    "c3b-20261027-d": "4",
    "c3b-ui-mvqevc26-he7k": "3",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 22), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_qgis():
    try:
        from qgis.core import QgsApplication
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "本工具需要 PyQGIS；请用 QGIS 解释器运行：\n"
            "  \"C:\\Program Files\\QGIS 3.44.14\\bin\\python-qgis-ltr.bat\" "
            "tools/render_r0005_research_figures.py ...") from exc
    application = QgsApplication.instance()
    if application is None:
        application = QgsApplication([], False)
        application.initQgis()
    return application


def _read_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _verify_run(run_root: Path, run_id: str):
    manifest_path = run_root / "runs" / run_id / "run_manifest.json"
    if not manifest_path.is_file():
        raise SystemExit(f"run manifest 不存在：{manifest_path}")
    manifest = _read_json(manifest_path)
    if manifest.get("run_id") != run_id:
        raise SystemExit("run manifest 的 run_id 与请求不一致")
    result_shas = {}
    for stage, entry in (manifest.get("stages") or {}).items():
        result_path = run_root / "runs" / run_id / "results" / f"{stage}.json"
        if not result_path.is_file():
            raise SystemExit(f"阶段结果文件缺失：{result_path}")
        actual = sha256(result_path)
        if actual != entry.get("result_sha256"):
            raise SystemExit(
                f"{stage} 结果 SHA 与 manifest 不一致：{actual} != {entry.get('result_sha256')}")
        result_shas[stage] = actual
    identity = {
        "run_id": manifest.get("run_id"),
        "task": manifest.get("task"),
        "route_id": manifest.get("route_id"),
        "authority": manifest.get("authority"),
        "input_version": (manifest.get("input_version") or {}).get("id"),
        "input_fingerprint": (manifest.get("input_version") or {}).get("input_fingerprint"),
        "parameter_fingerprint": (manifest.get("input_version") or {}).get(
            "parameter_fingerprint"),
        "contract_version": manifest.get("contract_version"),
        "result_list_sha256": manifest.get("authoritative_result_list_sha256"),
        "manifest_file_sha256": sha256(manifest_path),
        "stage_result_shas": result_shas,
        "counts": (manifest.get("map_projection") or {}).get("counts") or {},
        "created_at": manifest.get("created_at"),
    }
    return manifest, identity, str(manifest_path)


# --------------------------------------------------------------------------- 几何


def _project_extent(points, radius_m, aspect):
    """按 WGS84 点集 + 显示半径构造米制范围，并按图框纵横比锁定。"""

    from pyproj import Transformer

    forward = Transformer.from_crs("EPSG:4326", "EPSG:32651", always_xy=True)
    inverse = Transformer.from_crs("EPSG:32651", "EPSG:4326", always_xy=True)
    xs, ys = [], []
    for longitude, latitude in points:
        x, y = forward.transform(float(longitude), float(latitude))
        xs.append(x)
        ys.append(y)
    padding = float(radius_m) + 1800.0
    west, east = min(xs) - padding, max(xs) + padding
    south, north = min(ys) - padding, max(ys) + padding
    width, height = east - west, north - south
    if width / height < aspect:
        target = height * aspect
        west, east = (west + east) / 2 - target / 2, (west + east) / 2 + target / 2
    else:
        target = width / aspect
        south, north = (south + north) / 2 - target / 2, (south + north) / 2 + target / 2
    corners = [inverse.transform(west, south), inverse.transform(east, north)]
    longitude_west, latitude_south = corners[0]
    longitude_east, latitude_north = corners[1]
    return {
        "west": round(longitude_west, 7), "south": round(latitude_south, 7),
        "east": round(longitude_east, 7), "north": round(latitude_north, 7),
        "width_km": round((east - west) / 1000.0, 3),
        "height_km": round((north - south) / 1000.0, 3),
        "metric_bounds": [west, south, east, north],
    }


def _viewport(extent):
    from qgis.core import QgsRectangle

    return QgsRectangle(extent["west"], extent["south"], extent["east"], extent["north"])


def _land_and_sea(cartographic_land, extent):
    from cns_planner.application.map_figure_service import (
        _holes_of_polygons, _read_geojson_polygons, _sea_complement_polygons,
    )
    from cns_planner.gis.figure_spec import ExtentSpec

    viewport = _viewport(extent)
    extent_spec = ExtentSpec(**{key: extent[key] for key in
                                ("west", "south", "east", "north", "width_km", "height_km")})
    polygons, reason = _read_geojson_polygons(cartographic_land, viewport)
    if not polygons:
        return [], {}, [], {}, reason or "制图陆地面不可用"
    exteriors = [item["exterior"] for item in polygons]
    holes = _holes_of_polygons(polygons)
    sea_exteriors, sea_records, _ratio, sea_reason = _sea_complement_polygons(
        extent_spec, polygons)
    sea_holes = {
        str(index): item["holes"] for index, item in enumerate(sea_records)
        if item.get("holes")
    }
    return exteriors, holes, sea_exteriors, sea_holes, sea_reason


def _coverage_rings(sites, radius_m, metric_crs="EPSG:32651"):
    from cns_planner.application.cns_facility_assembler import coverage_circle_ring

    rings = []
    for site in sites:
        coordinate = site.get("coordinate") or []
        if len(coordinate) < 2:
            continue
        ring = coverage_circle_ring(
            float(coordinate[0]), float(coordinate[1]), float(radius_m),
            metric_crs=metric_crs)
        if ring:
            rings.append(ring)
    return rings


def _site_points(sites, kind, subsystem=None):
    points = []
    for site in sites:
        if site.get("kind") != kind:
            continue
        if subsystem and subsystem not in (site.get("subsystems") or []):
            continue
        coordinate = site.get("coordinate") or []
        if len(coordinate) < 2:
            continue
        points.append({"longitude": coordinate[0], "latitude": coordinate[1],
                       "site_id": site.get("site_id")})
    return points


def _n_radius(site):
    for device in site.get("devices") or []:
        if device.get("subsystem") == "N" and device.get("radius_m"):
            return float(device["radius_m"])
    return None


def _short_site(site):
    site_id = str(site.get("site_id") or "")
    if site.get("kind") == "planned_new_site":
        parts = site_id.split("-")
        return "规划 " + "-".join(parts[:2]) if len(parts) >= 2 else f"规划 {site_id}"
    if site_id.startswith("takeoff_landing_site:"):
        return f"起降点 {site_id.split(':')[-1]}"
    if site_id.startswith("tower-colocation:"):
        return f"塔共址 …{site_id[-4:]}"
    return site_id


def _action_is_endpoint(action):
    family = str(action.get("planner_family") or "")
    return (action.get("kind") == "endpoint" or "endpoint_integrity" in family
            or (action.get("segment_index") is None and "middle" not in family))


# --------------------------------------------------------------------------- spec


def _legend(style_keys, display_names):
    from cns_planner.gis.figure_spec import LegendItem
    from cns_planner.gis.figure_style import LEGEND_GROUP_OF

    return [
        LegendItem(style_key, style_key, display_names[style_key],
                   LEGEND_GROUP_OF.get(style_key, ""))
        for style_key in style_keys
    ]


def _layer(layer_key, display_name, style_key, geometry_type, data, count,
           source_role="manifest.map_projection", *, status="available"):
    from cns_planner.gis.figure_spec import LayerSpec

    return LayerSpec(
        layer_key, display_name, style_key, geometry_type, source_role,
        source_status=status, feature_count=count, data=data)


def _spec(template_id, title, subtitle, identity, manifest, extent, layout,
          layers, labels, legend_items, annotations, route):
    from cns_planner.application.map_figure_service import (
        _layout_plan, _legend_layout_entries,
    )
    from cns_planner.gis.figure_spec import ExtentSpec, FigureSpec

    identity_line = (
        f"研究运行 {identity['run_id']} · 输入 {identity['input_version']} · "
        f"结果清单 {str(identity['result_list_sha256'])[:12]}… · "
        f"N 动作 {identity['counts'].get('n_actions')} 个 · research_isolated")
    plan = _layout_plan(
        dict(A4_LAYOUT), legend_items=_legend_layout_entries(legend_items))
    # 身份写入副标题与 FigureSpec.metadata；地图框下沿留给比例尺与能力限制说明，
    # 不再叠一条会与底部经纬度标注抢位置的长披露条。
    plan["map_disclosure"] = ""
    return FigureSpec(
        template_id=template_id, template_version=1, title=title, subtitle=subtitle,
        route_id=manifest.get("route_id"), route_source="manifest.map_projection.route",
        route_geometry=route, route_start=route[0], route_end=route[-1],
        turn_points=route[1:-1],
        extent=ExtentSpec(**{key: extent[key] for key in
                             ("west", "south", "east", "north", "width_km", "height_km")}),
        extent_mode="research_route_bbox_plus_coverage_radius_metric",
        layers=layers, labels=labels, legend_items=legend_items,
        annotations=annotations, layout=plan, generated_from_revision=0,
        source_status={layer.layer_key: layer.source_status for layer in layers},
        omitted_layers=[],
        metadata={
            "run_identity": identity,
            "source": "same run manifest consumed by 2D/3D/report",
            "authority": "research_isolated",
            "formal_release_allowed": False,
            "confirmation_allowed": False,
            "disclosures": [
                "覆盖圈只投影设备输入半径，不是业务覆盖结论。",
                "Radar 为 not_calculated / unknown，本图不绘制有效覆盖。",
                "N 动作为 P16 选中提案（proposal_only），未安装、未确认、未 Apply。",
                "实际交付 actual_delivery=unknown。",
            ],
            "identity_line": identity_line,
        },
        parameters={"route_display_name": subtitle, "identity_line": identity_line},
        warnings=[], boundaries={"authority": "research_isolated",
                                 "formal_release_allowed": False},
    )


def _common_layers(land, holes, sea, sea_holes, route, sites, *, coverage_style=None,
                   coverage_radius=None, include_route=True):
    from cns_planner.gis.figure_spec import (
        GEOMETRY_LINE, GEOMETRY_POINT, GEOMETRY_POLYGON,
    )

    layers = []
    if sea:
        layers.append(_layer("sea", "海域", "sea", GEOMETRY_POLYGON,
                             {"polygons": sea, "polygon_holes": sea_holes}, len(sea),
                             "cartographic_land"))
    if land:
        layers.append(_layer("land", "陆地区域", "land", GEOMETRY_POLYGON,
                             {"polygons": land, "polygon_holes": holes}, len(land),
                             "cartographic_land"))
    if coverage_style and coverage_radius:
        rings = _coverage_rings(sites, coverage_radius)
        if rings:
            layers.append(_layer(
                "cns_coverage_" + ("comm" if coverage_style == "cns_coverage_comm"
                                   else "rid_land"),
                f"规划服务半径 {coverage_radius / 1000:g} km", coverage_style,
                GEOMETRY_POLYGON, {"polygons": rings, "radius_m": coverage_radius},
                len(rings), "manifest.map_projection.layers"))
    if include_route and route:
        layers.append(_layer("planned_route", "R0005 研究投影航路", "planned_route",
                             GEOMETRY_LINE, {"geometry": route}, 1,
                             "manifest.map_projection.route"))
    return layers


def _labels_and_legend(context):
    """按模板生成标签与图例（统一入口，避免六图各写一套）。"""
    from cns_planner.gis.figure_spec import LabelSpec

    labels = list(context.get("labels") or [])
    route = context["route"]
    labels.append(LabelSpec(kind="start", text="R0005 起点",
                            longitude=route[0][0], latitude=route[0][1],
                            priority=100, style_key="label_endpoint"))
    labels.append(LabelSpec(kind="end", text="R0005 终点",
                            longitude=route[-1][0], latitude=route[-1][1],
                            priority=100, style_key="label_endpoint_end"))
    return labels


def _annotations(items):
    from cns_planner.gis.figure_spec import AnnotationSpec

    return [
        AnnotationSpec(annotation_id=key, title=title, lines=lines,
                       anchor="map_bottom_right", width_mm=108.0)
        for key, title, lines in items
    ]


def build_specs(manifest, identity, land, holes, sea, sea_holes, route, sites, actions):
    from cns_planner.application.map_figure_service import _layout_plan
    from cns_planner.gis.figure_spec import GEOMETRY_NONE, GEOMETRY_POINT, LabelSpec

    counts = identity["counts"]
    n_count = counts.get("n_actions")
    subtitle = (f"R0005 · 研究运行 {identity['run_id']} · {n_count} 个 N 动作 · "
                f"结果清单 {str(identity['result_list_sha256'])[:8]}…")
    base_layout = _layout_plan({**A4_LAYOUT})
    aspect = base_layout["map_width_mm"] / base_layout["map_height_mm"]
    max_radius = max((site.get("devices") or [{}])[0].get("radius_m") or 0
                     for site in sites) if sites else 0
    extent = _project_extent(
        [[site["coordinate"][0], site["coordinate"][1]] for site in sites] + route,
        max(5000.0, max_radius), aspect)
    specs = {}

    existing = _site_points(sites, "existing_host")
    planned = _site_points(sites, "planned_new_site")

    def site_layers(proposal_style, include_planned=True):
        from cns_planner.gis.figure_spec import GEOMETRY_POINT

        layers = []
        if existing:
            layers.append(_layer("cns_existing", "既有 CNS 宿主", "cns_existing",
                                 GEOMETRY_POINT, {"points": existing}, len(existing)))
        if planned and include_planned:
            layers.append(_layer("cns_planned_site", "规划新建站址（研究假设）",
                                 proposal_style, GEOMETRY_POINT,
                                 {"points": planned}, len(planned)))
        return layers

    proposed_labels = [
        LabelSpec(kind="cns_proposal", text=_short_site(site),
                  longitude=site["coordinate"][0], latitude=site["coordinate"][1],
                  priority=80, style_key="label_cns_proposal")
        for site in sites if site.get("kind") == "planned_new_site"
    ]

    # 1) 通信 C
    spec = _spec(
        "cns_communication_v1", "R0005 通信（C）服务半径专题图", subtitle,
        identity, manifest, extent, base_layout,
        _common_layers(land, holes, sea, sea_holes, route, sites,
                       coverage_style="cns_coverage_comm", coverage_radius=2000.0)
        + site_layers("cns_comm_proposal"),
        _labels_and_legend({"route": route, "labels": proposed_labels}), 
        _legend(["cns_coverage_comm", "cns_comm_proposal", "cns_existing", "planned_route"],
                {"cns_coverage_comm": "C 通信输入半径 2 km",
                 "cns_comm_proposal": "规划新建站址（未确认）",
                 "cns_existing": "既有 CNS 宿主", "planned_route": "R0005 研究投影航路"}),
        _annotations([("c_capability", "C 能力限制", [
            "覆盖圈只投影设备输入半径 2 km，不是实测传播覆盖；",
            "C 结论 satisfied 来自 manifest.business_state，本图不重算。"])]),
        route)
    specs["cns_communication_v1"] = spec

    # 2) 导航 N：只画提案点，不把 1 km 局部监测半径画成“覆盖圈”
    nav_layers = _common_layers(land, holes, sea, sea_holes, route, sites,
                                include_route=True) + site_layers("cns_nav_proposal")
    nav_points = [{"longitude": action["coordinate"][0], "latitude": action["coordinate"][1]}
                  for action in actions if action.get("coordinate")]
    if nav_points:
        nav_layers.append(_layer(
            "cns_nav_proposal", "N 动作（proposal_only，未安装）", "cns_nav_proposal",
            GEOMETRY_POINT, {"points": nav_points}, len(nav_points),
            "manifest.map_projection.n_actions"))
    nav_labels = []
    specs["cns_navigation_v1"] = _spec(
        "cns_navigation_v1", "R0005 导航（N）监测提案专题图", subtitle,
        identity, manifest, extent, base_layout, nav_layers,
        _labels_and_legend({"route": route, "labels": []}),
        _legend(["cns_nav_proposal", "cns_existing", "planned_route"],
                {"cns_nav_proposal": "N 动作 / 局部监测（提案）",
                 "cns_existing": "既有 CNS 宿主", "planned_route": "R0005 研究投影航路"}),
        _annotations([("n_proposal", "N 提案边界", [
            "N 动作为 P16 选中提案（proposal_only），未安装、未确认、未 Apply；",
            "actual_delivery=unknown；局部监测半径 1 km 只是输入半径。"])]),
        route)

    # 3) RID
    rid_layers = _common_layers(land, holes, sea, sea_holes, route, sites,
                                coverage_style="cns_coverage_rid_land",
                                coverage_radius=5000.0) + site_layers("cns_rid_proposal")
    specs["cns_surveillance_rid_v1"] = _spec(
        "cns_surveillance_v1", "R0005 RID 合作监视专题图", subtitle,
        identity, manifest, extent, base_layout, rid_layers,
        _labels_and_legend({"route": route, "labels": proposed_labels}),
        _legend(["cns_coverage_rid_land", "cns_rid_proposal", "cns_existing",
                 "planned_route"],
                {"cns_coverage_rid_land": "RID 输入半径：陆/岸 2 km · 海 5 km",
                 "cns_rid_proposal": "规划新建站址（未确认）",
                 "cns_existing": "既有 CNS 宿主", "planned_route": "R0005 研究投影航路"}),
        _annotations([("rid_capability", "RID 能力限制", [
            "RID 输入半径合同：陆地/沿海 2 km、海上 5 km；图中圆圈按海上 5 km 上限单一绘制，"
            "研究输入未按 surface 拆分。",
            "覆盖圈是输入半径投影，不是保障结果；RID satisfied 不等于 S satisfied：S 还依赖 Radar。"])]),
        route)

    # 4) Radar：只画能力限制，不画任何有效覆盖
    radar_layers = _common_layers(land, holes, sea, sea_holes, route, sites,
                                  include_route=True) + site_layers(
        "cns_existing", include_planned=False)
    radar_layers.append(_layer(
        "cns_radar_limitation", "Radar 未计算（无布局，不绘制覆盖）",
        "cns_radar_limitation", GEOMETRY_NONE, {}, 0,
        "manifest.map_projection.layers"))
    specs["cns_surveillance_radar_v1"] = _spec(
        "cns_surveillance_v1", "R0005 Radar 非合作监视能力限制图", subtitle,
        identity, manifest, extent, base_layout, radar_layers,
        _labels_and_legend({"route": route, "labels": []}),
        _legend(["cns_radar_limitation", "cns_existing", "planned_route"],
                {"cns_radar_limitation": "Radar 未计算（不得读作满足）",
                 "cns_existing": "既有 CNS 宿主", "planned_route": "R0005 研究投影航路"}),
        _annotations([("radar_limitation", "Radar 能力限制（not_calculated / unknown）", [
            "适用依据：Radar 需要已确认的 radar_surveillance_layout 与对应探测模型；",
            "缺失输入：本轮研究输入没有该容器（reason=radar_layout_missing）；",
            "受影响结论：S 维持 unknown（2772 体元），P14/P15 business_failed；",
            "后续验证条件：提供真实/授权 layout 后重跑 P14→P15→P18；",
            "本图不绘制任何 Radar 有效覆盖，unknown 不得读作 pass。"])]),
        route)

    # 5) 综合
    combined_layers = _common_layers(
        land, holes, sea, sea_holes, route, sites,
        coverage_style="cns_coverage_comm", coverage_radius=2000.0) + site_layers(
        "cns_nav_proposal")
    # 叠加 RID 5 km 与 N 1 km（同站点，不同样式）
    rid_rings = _coverage_rings(sites, 5000.0)
    if rid_rings:
        combined_layers.append(_layer(
            "cns_coverage_rid_land", "RID 输入半径（海 5 km 上限）", "cns_coverage_rid_land",
            "polygon", {"polygons": rid_rings, "radius_m": 5000.0}, len(rid_rings)))
    if nav_points:
        combined_layers.append(_layer(
            "cns_nav_proposal", "N 动作（proposal_only，未安装）", "cns_nav_proposal",
            GEOMETRY_POINT, {"points": nav_points}, len(nav_points)))
    combined_layers.append(_layer(
        "cns_radar_limitation", "Radar 未计算（无布局，不绘制覆盖）",
        "cns_radar_limitation", GEOMETRY_NONE, {}, 0,
        "manifest.map_projection.layers"))
    specs["cns_combined_v1"] = _spec(
        "cns_combined_v1", "R0005 CNS 综合布设专题图", subtitle,
        identity, manifest, extent, base_layout, combined_layers,
        _labels_and_legend({"route": route, "labels": proposed_labels}),
        _legend(["cns_coverage_comm", "cns_coverage_rid_land", "cns_nav_proposal",
                 "cns_radar_limitation", "cns_existing", "planned_route"],
                {"cns_coverage_comm": "C 通信输入半径 2 km",
                 "cns_coverage_rid_land": "RID 陆/岸 2 km·海 5 km",
                 "cns_nav_proposal": "N 动作 / 局部监测（提案）",
                 "cns_radar_limitation": "Radar 未计算（不得读作满足）",
                 "cns_existing": "既有 CNS 宿主", "planned_route": "R0005 研究投影航路"}),
        _annotations([("combined_limitation", "综合能力限制", [
            "C/RID 覆盖圈只投影设备输入半径（RID 图中按海上 5 km 上限单一绘制），不是保障结果；",
            "N 动作为提案，未安装；Radar not_calculated / unknown ⇒ S unknown（2772 体元）；",
            "不得把 unknown 读作 pass，也不得作为正式发布/Confirm/Apply 依据。"])]),
        route)

    # 6) 航路总览
    route_layers = _common_layers(land, holes, sea, sea_holes, route, sites,
                                  include_route=True) + site_layers("cns_existing")
    if nav_points:
        route_layers.append(_layer(
            "cns_nav_proposal", "N 动作（proposal_only，未安装）", "cns_nav_proposal",
            GEOMETRY_POINT, {"points": nav_points}, len(nav_points)))
    route_layers.append(_layer(
        "start_point", "起点", "start_point", GEOMETRY_POINT,
        {"points": [{"longitude": route[0][0], "latitude": route[0][1]}]}, 1,
        "manifest.map_projection.route"))
    route_layers.append(_layer(
        "end_point", "终点", "end_point", GEOMETRY_POINT,
        {"points": [{"longitude": route[-1][0], "latitude": route[-1][1]}]}, 1,
        "manifest.map_projection.route"))
    specs["route_overview_v1"] = _spec(
        "route_overview_v1", "R0005 航路总览图", subtitle,
        identity, manifest, extent, base_layout, route_layers,
        _labels_and_legend({"route": route, "labels": proposed_labels}),
        _legend(["planned_route", "cns_existing", "cns_nav_proposal", "start_point",
                 "end_point"],
                {"planned_route": "R0005 研究投影航路",
                 "cns_existing": "既有 CNS 宿主",
                 "cns_nav_proposal": "N 动作（提案）",
                 "start_point": "起点", "end_point": "终点"}),
        _annotations([("route_premise", "航路前提", [
            f"航路为冻结 9 顶点研究投影（{manifest.get('route_id')}），非正式运行航路；",
            "高度层 ALT-100，EGM2008 正高 100 m；未授予运行授权。"])]),
        route)

    return specs


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--dpi", type=float, default=300.0)
    parser.add_argument("--sources", default="projects/map_sources.json")
    args = parser.parse_args(argv)

    _require_qgis()
    sys.path.insert(0, os.getcwd())
    source_holder = _require_qgis()  # 保持 QgsApplication 存活
    _ = source_holder

    from cns_planner.gis.qgis_figure_renderer import QgisFigureRenderer

    run_root = Path(args.run_root).resolve()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest, identity, manifest_path = _verify_run(run_root, args.run_id)
    map_projection = manifest.get("map_projection") or {}
    route = [[float(point[0]), float(point[1])]
             for point in (map_projection.get("route") or {}).get("path") or []]
    sites = map_projection.get("sites") or []
    actions = map_projection.get("n_actions") or []
    if len(route) < 2 or not sites:
        raise SystemExit("manifest 缺少航路或站址几何")

    sources = _read_json(Path(args.sources))
    cartographic_land = (sources.get("paths") or sources).get("cartographic_land")
    if not cartographic_land:
        raise SystemExit("map_sources.json 未配置 cartographic_land")

    counts = identity["counts"]
    n_count = counts.get("n_actions")
    aspect = 210.0 / 297.0  # placeholder; real aspect from layout below
    from cns_planner.application.map_figure_service import _layout_plan
    layout = _layout_plan(dict(A4_LAYOUT))
    aspect = layout["map_width_mm"] / layout["map_height_mm"]
    max_radius = max([5000.0] + [
        float(device.get("radius_m") or 0)
        for site in sites for device in (site.get("devices") or [])])
    extent = _project_extent(
        [[site["coordinate"][0], site["coordinate"][1]] for site in sites] + route,
        max_radius, aspect)
    land, holes, sea, sea_holes, land_reason = _land_and_sea(cartographic_land, extent)
    if not land:
        raise SystemExit(f"制图陆地面不可用：{land_reason}")

    specs = build_specs(manifest, identity, land, holes, sea, sea_holes, route, sites,
                        actions)
    renderer = QgisFigureRenderer()
    index = {
        "run_id": args.run_id, "identity": identity, "dpi": args.dpi,
        "manifest_path": manifest_path, "extent": extent,
        "cartographic_land": cartographic_land,
        "figures": [],
    }
    for template_id, spec in specs.items():
        png = renderer.render(spec, dpi=args.dpi)
        if not png.startswith(b"\x89PNG"):
            raise SystemExit(f"{template_id} 渲染结果不是 PNG")
        name = "radar" if template_id == "cns_surveillance_v1" and "radar" in spec.title.lower() else template_id
        # 稳定的中文可读文件名：按 title 关键字归类
        if "Radar" in spec.title:
            name = "cns_radar_limitation"
        elif "RID" in spec.title:
            name = "cns_surveillance_rid"
        png_path = output / f"{name}.png"
        png_path.write_bytes(png)
        spec_path = output / f"{name}.spec.json"
        spec_path.write_text(
            json.dumps(spec.to_dict(), ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8", newline="\n")
        identity_path = output / f"{name}.identity.json"
        identity_path.write_text(json.dumps({
            **identity, "template_id": template_id, "title": spec.title,
            "spec_fingerprint": spec.fingerprint(),
            "image_sha256": sha256(png_path), "image_bytes": len(png),
            "dpi": args.dpi, "identity_line": spec.parameters.get("identity_line"),
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        index["figures"].append({
            "template_id": template_id, "name": name, "title": spec.title,
            "image": png_path.name, "image_sha256": sha256(png_path),
            "image_bytes": len(png), "spec": spec_path.name,
            "spec_fingerprint": spec.fingerprint(),
            "identity": identity_path.name,
            "layer_count": len(spec.layers),
        })
        print(f"[{name}] {spec.title} -> {png_path} ({len(png)} bytes)", flush=True)
    index_path = output / "figures_index.json"
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8", newline="\n")
    print(json.dumps({"run_id": args.run_id, "figures": len(index["figures"]),
                      "index": str(index_path), "index_sha256": sha256(index_path),
                      "n_actions": n_count}, ensure_ascii=False, indent=2))
    print("QGIS_FIGURES_OK", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
