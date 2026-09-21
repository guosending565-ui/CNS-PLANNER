"""Building footprint geometry **quality report** generator (Phase 3.5, read-only).

它回答一个问题：*"这次建筑验证为什么 unresolved？"*

* 只读打开 GeoPackage（``mode=ro``），绝不写回源数据；
* 用与生产一致的顺序做几何质量检查：**先投影到显式米制 CRS，再检查/修复**
  （投影本身可能把有效 polygon 变成 MultiPolygon 或引入自交）；
* 修复只用 ``shapely.make_valid()``，且结果必须自身有效、面积为正；
* 修复失败保持 unknown 语义（报告为 ``invalid``，绝不当作"没有建筑"）。

用法::

    python tools/building_geometry_quality_report.py \
        --source "D:/.../zhoushan_buildings.gpkg" --metric-crs EPSG:32651 \
        --bbox 122.268 29.835 122.300 29.955 \
        --out outputs/building_geometry_quality.json

不带 ``--bbox`` 时扫描全量（真实舟山 53 万条，耗时较长）。
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cns_planner.domain.building_geometry_quality import (  # noqa: E402
    GEOMETRY_QUALITY_NOTES, assess_footprint_geometry, empty_geometry_quality_report,
    merge_geometry_quality,
)

GPKG_MAGIC = b"GP"
_GPKG_ENVELOPE_SIZES = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}


def gpkg_geometry_blob_to_wkb(blob):
    """Strip the GeoPackage binary header so shapely can parse the geometry."""

    raw = bytes(blob)
    if raw[0:2] != GPKG_MAGIC:
        return raw
    flags = raw[3]
    offset = 8 + _GPKG_ENVELOPE_SIZES.get((flags >> 1) & 0b111, 0)
    return raw[offset:]


def iter_geometries(source, *, bbox=None, rtree=True, limit=None):
    """Yield ``(fid, identifier, wkb)`` for the footprints intersecting ``bbox``."""

    connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        columns = {row[1] for row in connection.execute('PRAGMA table_info("buildings")')}
        identifier = "id" if "id" in columns else "fid"
        sql = f'SELECT fid, "{identifier}", geom FROM "buildings"'
        params = ()
        if bbox and rtree:
            try:
                sql += (
                    ' WHERE fid IN (SELECT id FROM rtree_buildings_geom '
                    "WHERE maxx>=? AND minx<=? AND maxy>=? AND miny<=?)"
                )
                params = (bbox[0], bbox[2], bbox[1], bbox[3])
            except sqlite3.DatabaseError:
                sql = f'SELECT fid, "{identifier}", geom FROM "buildings"'
                params = ()
        if limit:
            sql += f" LIMIT {int(limit)}"
        for fid, ident, geom in connection.execute(sql, params):
            if geom is None:
                continue
            yield fid, ident, gpkg_geometry_blob_to_wkb(geom)
    finally:
        connection.close()


def build_report(source, *, metric_crs, bbox=None, rtree=True, limit=None):
    from pyproj import CRS, Transformer
    from shapely import wkb as shapely_wkb
    from shapely.ops import transform as shapely_transform

    transformer = Transformer.from_crs(
        CRS.from_epsg(4326), CRS.from_user_input(metric_crs), always_xy=True,
    )
    report = empty_geometry_quality_report()
    report["source"] = {
        "path": str(source), "layer": "buildings", "source_crs": "EPSG:4326",
        "metric_crs": str(metric_crs), "bbox_geographic": list(bbox) if bbox else None,
        "query": "provider_rtree" if (bbox and rtree) else "full_scan",
        "read_only": True, "source_geometry_modified": False,
    }
    report["method"] = {
        "order": "project_to_metric_crs_then_check_then_make_valid",
        "why": (
            "投影本身会改变几何类型（真实数据实测：单部件 polygon 投影后成为 "
            "MultiPolygon），因此质量检查必须发生在米制几何上"
        ),
        "repair": "shapely.make_valid",
        "repair_acceptance": "result_must_be_polygonal_valid_and_positive_area",
        "unrepairable": "stays_unknown_never_safe_never_zero",
    }
    report["parts"] = {"source_parts_total": 0, "metric_parts_total": 0,
                       "multi_part_footprint_count": 0}
    invalid_examples = report["invalid"]
    for fid, ident, blob in iter_geometries(source, bbox=bbox, rtree=rtree, limit=limit):
        geometry = shapely_wkb.loads(blob)
        parts = (
            list(geometry.geoms) if geometry.geom_type == "MultiPolygon"
            else [geometry] if geometry.geom_type == "Polygon" else []
        )
        report["parts"]["source_parts_total"] += max(1, len(parts))
        metric_rings = []
        for part in parts:
            metric = shapely_transform(transformer.transform, part)
            metric_rings.append([[float(x), float(y)] for x, y in metric.exterior.coords])
        report["parts"]["metric_parts_total"] += max(1, len(metric_rings))
        if len(metric_rings) > 1:
            report["parts"]["multi_part_footprint_count"] += 1
        for ring in metric_rings or [[]]:
            quality = assess_footprint_geometry(str(ident), ring)
            record = quality["record"]
            record["fid"] = fid
            merge_geometry_quality(report, record)
    report["invalid_examples_truncated"] = bool(
        invalid_examples and report["counts"]["invalid"] > len(invalid_examples)
    )
    report["notes"] = list(GEOMETRY_QUALITY_NOTES)
    return report


def markdown_summary(report):
    counts = report["counts"]
    repair = report["repair"]
    total = report["evaluated_footprint_count"]
    lines = [
        "# Building footprint geometry quality report",
        "",
        f"- 来源：`{report['source']['path']}`（只读，源几何未修改）",
        f"- 源 CRS → 米制 CRS：`{report['source']['source_crs']}` → `{report['source']['metric_crs']}`",
        f"- 查询范围：`{report['source']['bbox_geographic'] or '全量'}`（{report['source']['query']}）",
        f"- 检查的 footprint：**{total}**",
        f"- 判定：passed **{counts['passed']}** · repaired **{counts['repaired']}** · invalid **{counts['invalid']}**",
        f"- `make_valid` 应用：**{repair['applied_count']}**，修复失败/不可解释：**{repair['failed_count']}**",
        f"- 修复后仍多部件：**{repair['multi_part_result_count']}**",
        f"- 源部件总数 {report['parts']['source_parts_total']} → 米制部件总数 "
        f"{report['parts']['metric_parts_total']}（多部件 footprint {report['parts']['multi_part_footprint_count']}）",
        "",
        "## 结论",
        "",
    ]
    if counts["invalid"]:
        lines.append(
            f"存在 **{counts['invalid']}** 个无法解释的 footprint：它们在建筑验证中保持 "
            "`unresolved`（`unknown != safe`），不会当成没有建筑、也不会当成已通过。"
        )
    elif counts["repaired"]:
        lines.append(
            f"**{counts['repaired']}** 个 footprint 在内存中被 `make_valid` 修复后参与净空判定；"
            "源数据未被修改。"
        )
    else:
        lines.append("所有 footprint 的米制几何直接有效，无需修复。")
    lines += ["", "## 说明", ""]
    lines += [f"- {note}" for note in report["notes"]]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Building footprint geometry quality report")
    parser.add_argument("--source", required=True, help="buildings GeoPackage 路径")
    parser.add_argument("--metric-crs", default="EPSG:32651", help="米制 CRS（默认 EPSG:32651）")
    parser.add_argument("--bbox", nargs=4, type=float, metavar=("W", "S", "E", "N"),
                        help="地理范围（OGC:CRS84）；省略则全量扫描")
    parser.add_argument("--limit", type=int, default=None, help="最多扫描多少条（调试用）")
    parser.add_argument("--no-rtree", action="store_true", help="忽略 RTree，直接全表过滤")
    parser.add_argument("--out", default=None, help="JSON 报告输出路径")
    parser.add_argument("--markdown", default=None, help="Markdown 摘要输出路径")
    args = parser.parse_args(argv)

    source = Path(args.source).expanduser()
    if not source.is_file():
        print(f"source not found: {source}", file=sys.stderr)
        return 2
    report = build_report(
        source, metric_crs=args.metric_crs, bbox=args.bbox,
        rtree=not args.no_rtree, limit=args.limit,
    )
    summary = markdown_summary(report)
    print(summary)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON  -> {out}")
    if args.markdown:
        out = Path(args.markdown)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(summary, encoding="utf-8")
        print(f"MD    -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
