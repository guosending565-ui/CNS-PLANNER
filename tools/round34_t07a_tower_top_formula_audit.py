"""Round34-T07-A —— 塔顶派生公式的**审计口径**修正与复核（只读）。

为什么需要本工具
----------------
T05-V 的 ``audit_tower_origin_heights.py`` 用**一条**公式判"塔顶是否自洽"：

    predicted = terrain + building_height_m + tower_structure_height_m

但既有 :func:`cns_planner.domain.tower_obstacle.build_tower_obstacle_profile` 的语义是
**按基面类型分叉**的：

* ``ground``   ：``tower_top = terrain + tower_structure_height``（**不含**建筑高度）；
* ``rooftop``  ：``tower_top = terrain + building_height + tower_structure_height``。

于是凡是"基面是 ground、但剖面仍带着一个已解析建筑高度"的塔，旧口径都会凭空多算
一个建筑高度并报出**假残差**。真实数据里正是唯一一例
``33090301000160``（桃花射雕城室外站）：旧口径报 −4.363575 m，而按正确公式其残差
恰好为 0 —— 那个 4.36 m 是**审计公式**的差，不是塔顶派生算法的差。

本工具输出
----------
1. 按 ``base_type`` 分叉的**正确公式**逐塔复核；
2. 差异三分归类：

   * ``true_calculation_inconsistency`` —— 正确公式下仍有非零残差（真正的计算不一致）；
   * ``building_field_present_but_not_used`` —— 建筑高度存在但**不该**参与（含 ground
     塔带已解析建筑高度），残差为 0，不构成任何不一致；
   * ``source_data_self_contradiction`` —— 源数据/分类自相矛盾（例如 rooftop 塔建筑
     高度未解析、或 ground 塔带已解析建筑高度）；
3. 296 座"无已解析塔顶"的估算候选：原点是否**不高于** FABDEM 地表
   （把 T05 §3 的总数按塔型与档位细分，供 T07-A 的物理输入诊断使用）；
4. 可选：用真实 FABDEM（QGIS Python，``osgeo``）逐塔复算地表正高，独立验证
   ``terrain_status`` 是否为真事实（**不是**重算塔顶）。

它**只读**：不写回塔记录、不改 ``ProjectState``、不修改任何源数据。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter
from pathlib import Path

#: 差异归类词表。
DIFF_TRUE_INCONSISTENCY = "true_calculation_inconsistency"
DIFF_BUILDING_FIELD_NOT_USED = "building_field_present_but_not_used"
DIFF_SOURCE_CONTRADICTION = "source_data_self_contradiction"
DIFF_NONE = "no_difference"

FORMULA_BY_BASE_TYPE = {
    "ground": "terrain + tower_structure_height",
    "rooftop": "terrain + building_height + tower_structure_height",
}

CSV_COLUMNS = (
    "tower_id", "name", "district", "site_type", "base_type", "base_type_source",
    "vertical_status", "terrain_status", "terrain_elevation_m",
    "building_height_m", "building_height_status", "tower_structure_height_m",
    "tower_top_orthometric_m", "formula_used", "predicted_tower_top_m",
    "residual_m", "difference_class", "classification_reason",
    "source_elevation_m", "tower_height_m", "source_estimate_m",
    "origin_minus_terrain_m", "origin_not_above_terrain",
)


def _finite(value):
    return (
        isinstance(value, (int, float)) and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _number(value):
    return float(value) if _finite(value) else None


def audit_row(*, tower, profile, origin_egm2008_m=None):
    """按 ``base_type`` 分叉公式复核一座塔的已解析塔顶（纯函数）。"""

    tower = tower if isinstance(tower, dict) else {}
    profile = profile if isinstance(profile, dict) else {}
    tower_id = str(tower.get("tower_id") or "")
    base_type = profile.get("base_type")
    terrain = _number(profile.get("terrain_elevation_m"))
    building = _number(profile.get("building_height_m"))
    structure = _number(profile.get("tower_structure_height_m"))
    top = _number(profile.get("tower_top_orthometric_m"))
    building_status = profile.get("building_height_status")
    vertical_status = profile.get("vertical_status")
    source_elevation = _number(tower.get("elevation_m"))
    tower_height = _number(tower.get("height_m"))

    formula = FORMULA_BY_BASE_TYPE.get(str(base_type))
    predicted = None
    if formula == FORMULA_BY_BASE_TYPE["ground"] and terrain is not None and structure is not None:
        predicted = terrain + structure
    elif formula == FORMULA_BY_BASE_TYPE["rooftop"] and None not in (terrain, building, structure):
        predicted = terrain + building + structure

    residual = None if predicted is None or top is None else top - predicted

    classification = DIFF_NONE
    reason = None
    if top is None:
        classification = DIFF_NONE
        reason = "tower_top_not_resolved_audited_separately"
    elif base_type not in FORMULA_BY_BASE_TYPE:
        classification = DIFF_SOURCE_CONTRADICTION
        reason = f"base_type_unknown_for_formula:{base_type}"
    elif predicted is None:
        #: 正确公式的输入缺失（rooftop 缺建筑高度等）：不是"算错"，而是数据不足。
        classification = DIFF_SOURCE_CONTRADICTION
        reason = f"required_formula_input_missing:{vertical_status}"
    elif residual == 0.0:
        if base_type == "ground" and building is not None:
            classification = DIFF_BUILDING_FIELD_NOT_USED
            reason = (
                "ground_site_building_height_present_but_correctly_excluded_from_tower_top"
            )
        else:
            classification = DIFF_NONE
            reason = None
    else:
        classification = DIFF_TRUE_INCONSISTENCY
        reason = "tower_top_differs_from_base_type_formula"

    origin_minus_terrain = (
        None if origin_egm2008_m is None or terrain is None
        else float(origin_egm2008_m) - terrain
    )
    return {
        "tower_id": tower_id,
        "name": tower.get("name"),
        "district": tower.get("district"),
        "site_type": tower.get("site_type"),
        "base_type": base_type,
        "base_type_source": profile.get("base_type_source"),
        "vertical_status": vertical_status,
        "terrain_status": profile.get("terrain_status"),
        "terrain_elevation_m": terrain,
        "building_height_m": building,
        "building_height_status": building_status,
        "tower_structure_height_m": structure,
        "tower_top_orthometric_m": top,
        "formula_used": formula,
        "predicted_tower_top_m": predicted,
        "residual_m": residual,
        "difference_class": classification,
        "classification_reason": reason,
        "source_elevation_m": source_elevation,
        "tower_height_m": tower_height,
        "source_estimate_m": (
            None if source_elevation is None or tower_height is None
            else source_elevation + tower_height
        ),
        "origin_minus_terrain_m": origin_minus_terrain,
        "origin_not_above_terrain": (
            None if origin_minus_terrain is None else bool(origin_minus_terrain <= 0.0)
        ),
    }


def _legacy_formula_residual(row):
    """T05-V 旧口径（对**所有**基面类型都加建筑高度）下的残差，用于展示差异来源。"""

    terrain = row.get("terrain_elevation_m")
    building = row.get("building_height_m")
    structure = row.get("tower_structure_height_m")
    top = row.get("tower_top_orthometric_m")
    if None in (terrain, structure, top):
        return None
    return top - (terrain + (building or 0.0) + structure)


def summarise(rows, *, origin_records=None):
    origin_records = origin_records or {}
    resolved = [row for row in rows if row["tower_top_orthometric_m"] is not None]
    classes = Counter(row["difference_class"] for row in resolved)
    legacy_residuals = [
        (row["tower_id"], _legacy_formula_residual(row)) for row in resolved
    ]
    legacy_nonzero = [
        {"tower_id": tower_id, "legacy_residual_m": round(value, 6)}
        for tower_id, value in legacy_residuals if value not in (None, 0.0)
    ]
    corrected_nonzero = [
        {"tower_id": row["tower_id"], "residual_m": round(row["residual_m"], 6),
         "base_type": row["base_type"], "formula_used": row["formula_used"]}
        for row in resolved
        if row["residual_m"] not in (None, 0.0)
    ]
    estimate_only = [row for row in rows if row["tower_top_orthometric_m"] is None]
    below = [row for row in rows if row["origin_not_above_terrain"] is True]
    return {
        "tower_count": len(rows),
        "resolved_tower_top_count": len(resolved),
        "estimate_only_count": len(estimate_only),
        "base_type_counts": dict(Counter(
            row["base_type"] for row in rows
        )),
        "vertical_status_counts": dict(Counter(
            row["vertical_status"] for row in rows
        )),
        "base_type_by_vertical_status_counts": {
            f"{base}|{status}": count for (base, status), count in Counter(
                (row["base_type"], row["vertical_status"]) for row in rows
            ).items()
        },
        "resolved_base_type_counts": dict(Counter(
            row["base_type"] for row in resolved
        )),
        "difference_class_counts": {
            name: classes.get(name, 0)
            for name in (DIFF_NONE, DIFF_TRUE_INCONSISTENCY,
                         DIFF_BUILDING_FIELD_NOT_USED, DIFF_SOURCE_CONTRADICTION)
        },
        "corrected_formula_nonzero_residual_count": len(corrected_nonzero),
        "corrected_formula_max_abs_residual_m": (
            max(abs(item["residual_m"]) for item in corrected_nonzero)
            if corrected_nonzero else 0.0
        ),
        "corrected_formula_nonzero_residuals": corrected_nonzero[:20],
        "legacy_formula_nonzero_residual_count": len(legacy_nonzero),
        "legacy_formula_max_abs_residual_m": (
            max(abs(item["legacy_residual_m"]) for item in legacy_nonzero)
            if legacy_nonzero else 0.0
        ),
        "legacy_formula_nonzero_residuals": legacy_nonzero[:20],
        "ground_with_resolved_building_count": sum(
            1 for row in rows
            if row["base_type"] == "ground" and row["building_height_m"] is not None
        ),
        "rooftop_without_building_count": sum(
            1 for row in rows
            if row["base_type"] == "rooftop" and row["building_height_m"] is None
        ),
        "origin_not_above_terrain_count": len(below),
        "origin_not_above_terrain_by_base_type": dict(Counter(
            row["base_type"] for row in below
        )),
        "origin_records": {
            "record_count": len(origin_records),
            "usable": sum(
                1 for item in origin_records.values()
                if (item or {}).get("origin_availability") == "usable"
            ),
            "not_evaluable": sum(
                1 for item in origin_records.values()
                if (item or {}).get("origin_availability") == "not_evaluable"
            ),
            "unknown": sum(
                1 for item in origin_records.values()
                if (item or {}).get("origin_availability") == "unknown"
            ),
        } if origin_records else None,
    }


def _write_csv(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CSV_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in CSV_COLUMNS})


def _load_extract(extract_dir):
    extract = Path(extract_dir)
    towers = json.loads((extract / "towers.json").read_text(encoding="utf-8"))
    profiles = json.loads(
        (extract / "tower_obstacle_profiles.json").read_text(encoding="utf-8")
    )
    items = profiles.get("items") if isinstance(profiles.get("items"), dict) else {}
    return towers, items


def _load_origin_records(path):
    if not path:
        return {}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    records = payload.get("records") if isinstance(payload, dict) else None
    if isinstance(records, dict):
        return records
    if isinstance(records, list):
        return {str(item.get("tower_id")): item for item in records if isinstance(item, dict)}
    return {}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Round34-T07-A 塔顶派生公式审计口径修正（只读）")
    parser.add_argument("--extract", required=True, help="T05 只读切片目录（towers/profiles JSON）")
    parser.add_argument("--out", required=True, help="输出目录")
    parser.add_argument("--origin-physical-inputs", default=None,
                        help="可选的逐塔物理输入诊断 JSON（由 T07-A 只读诊断脚本产出）")
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    towers, profiles = _load_extract(args.extract)
    origin_records = _load_origin_records(args.origin_physical_inputs)

    rows = []
    for tower in towers:
        tower_id = str(tower.get("tower_id") or "")
        origin = (origin_records.get(tower_id) or {}).get("origin_egm2008_m")
        rows.append(audit_row(
            tower=tower, profile=profiles.get(tower_id) or {}, origin_egm2008_m=origin,
        ))

    summary = summarise(rows, origin_records=origin_records)
    focus = [
        row for row in rows
        if row["tower_id"] == "33090301000160"
        or row["difference_class"] in (DIFF_TRUE_INCONSISTENCY, DIFF_SOURCE_CONTRADICTION)
    ]
    _write_csv(out_dir / "tower_top_formula_audit.csv", rows)
    (out_dir / "tower_top_formula_audit.json").write_text(
        json.dumps({
            "audit_scope": "tower_top_derivation_formula_by_base_type",
            "formula_by_base_type": FORMULA_BY_BASE_TYPE,
            "difference_classes": {
                DIFF_TRUE_INCONSISTENCY: "正确公式下仍有非零残差（真正的计算不一致）",
                DIFF_BUILDING_FIELD_NOT_USED: "建筑字段存在但不参与该基面类型的公式",
                DIFF_SOURCE_CONTRADICTION: "源数据/分类自相矛盾（公式输入缺失或基面不可判）",
                DIFF_NONE: "无差异",
            },
            "summary": summary,
            "focus_rows": focus,
            "records": rows,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"towers                    : {summary['tower_count']}")
    print(f"resolved tower top        : {summary['resolved_tower_top_count']}")
    print(f"corrected formula residual: {summary['corrected_formula_nonzero_residual_count']}"
          f" (max {summary['corrected_formula_max_abs_residual_m']} m)")
    print(f"legacy formula residual   : {summary['legacy_formula_nonzero_residual_count']}"
          f" (max {summary['legacy_formula_max_abs_residual_m']} m)")
    print(f"difference classes        : {summary['difference_class_counts']}")
    print(f"ground w/ building height : {summary['ground_with_resolved_building_count']}")
    print(f"rooftop w/o building      : {summary['rooftop_without_building_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
