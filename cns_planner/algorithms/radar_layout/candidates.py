"""Radar Surveillance Layout V1 — 候选单面阵方向生成（确定性，不逐度穷举）。

对每个 ``tower × radar_type``：

1. 先用 range / elevation 筛选**物理可达**的 sample（:func:`physically_reachable`）；
2. 计算这些 sample 相对该塔的 ``bearing``；
3. 候选 panel 中心方向 = ``bearing``、``bearing − 45°``、``bearing + 45°``；
4. normalize 到 ``[0, 360)``；
5. 数值容差去重；
6. 计算每个方向对应的 coverage sample set；
7. **相同 coverage set 只留一个确定性代表**（取归一化后方位角最小者）；
8. 删除同塔同型号中 coverage set 被另一候选**完全包含**的 dominated panel。

第 8 步的语义边界（重要）：被删除的 panel ``q`` 的 coverage set 是某个保留 panel ``p``
的**真子集**，因此把 ``q`` 换成 ``p`` 一定不会让任何 sample 失去**该塔**的覆盖，也一定
不会让"该塔覆盖某 sample"这一事实从可行变不可行。所以在"每塔最多 4 个 panel"与
"每 sample 至少 N 个不同塔站址"这两类约束下，存在一个不含任何 dominated panel 的
最优解 —— 剪枝不改变最优面阵总数，也不改变可达的覆盖集合。第 8 步只保证确定性，
不参与任何"求解"。
"""

from __future__ import annotations

from copy import deepcopy

from ...domain.radar_surveillance_layout import (
    MAX_PANELS_PER_TOWER, RADAR_TYPES, radar_geometry_parameters,
)
from .geometry import (
    circular_angle_delta_deg, dedupe_azimuths, geometry_evaluation,
    normalize_azimuth_deg, physically_reachable,
)

#: 候选方位角生成使用的固定偏移（度）：bearing、bearing − 45°、bearing + 45°。
CANDIDATE_AZIMUTH_OFFSETS_DEG = (0.0, -45.0, 45.0)

#: 方位角数值容差（度）。同一方向在浮点误差内视为同一候选。
AZIMUTH_DEDUPE_TOLERANCE_DEG = 1e-9


def build_tower_records(towers):
    """规范化塔输入。

    ``towers`` 每项只需 ``tower_id`` / ``metric`` / ``origin_egm2008_m``；
    ``origin_egm2008_m`` 缺失（``None``）的塔**不产生任何候选**（fail-closed，
    绝不填 0、绝不伪造塔高）。
    """

    records, unusable = [], []
    for index, tower in enumerate(towers or []):
        if not isinstance(tower, dict):
            continue
        tower_id = str(tower.get("tower_id") or "")
        metric = tower.get("metric")
        origin = tower.get("origin_egm2008_m")
        if not tower_id:
            unusable.append({"index": index, "reason": "missing_tower_id"})
            continue
        if not (isinstance(metric, (list, tuple)) and len(metric) >= 2):
            unusable.append({"tower_id": tower_id, "reason": "missing_metric_coordinate"})
            continue
        if not isinstance(origin, (int, float)) or isinstance(origin, bool):
            unusable.append({
                "tower_id": tower_id,
                "reason": tower.get("origin_reason") or "radar_origin_egm2008_unresolved",
            })
            continue
        records.append({
            "tower_id": tower_id,
            "name": tower.get("name") or tower_id,
            "metric": [float(metric[0]), float(metric[1])],
            "longitude": tower.get("longitude"),
            "latitude": tower.get("latitude"),
            "origin_egm2008_m": float(origin),
            "origin_source": tower.get("origin_source"),
            "origin_confirmed": tower.get("origin_confirmed"),
            "origin_parameter_origin": tower.get("origin_parameter_origin"),
            "site_type": tower.get("site_type"),
            "source": deepcopy(tower.get("source")),
        })
    return records, unusable


def build_sample_records(samples):
    """规范化 sample 输入：``metric`` + ``egm2008_m`` + ``surface_class``。"""

    records = []
    for index, sample in enumerate(samples or []):
        metric = sample.get("metric")
        if not (isinstance(metric, (list, tuple)) and len(metric) >= 2):
            raise ValueError(f"sample {index} 缺少米制坐标")
        z = sample.get("egm2008_m")
        if not isinstance(z, (int, float)) or isinstance(z, bool):
            raise ValueError(f"sample {index} 缺少 EGM2008 正高")
        records.append({
            "sample_index": index,
            "sample_id": str(sample.get("sample_id") or f"S{index:06d}"),
            "distance_along_route_m": float(sample.get("distance_along_route_m") or 0.0),
            "metric": [float(metric[0]), float(metric[1])],
            "longitude": sample.get("longitude"),
            "latitude": sample.get("latitude"),
            "egm2008_m": float(z),
            "surface_class": str(sample.get("surface_class") or "unknown"),
            "required_distinct_site_count": sample.get("required_distinct_site_count"),
        })
    return records


def _coverage_signature(covered_indices):
    return tuple(sorted(int(index) for index in covered_indices))


def _candidate_id(tower_id, radar_type, azimuth_deg):
    return f"P-{tower_id}-{radar_type}-{normalize_azimuth_deg(azimuth_deg):.6f}"


def candidates_for_tower_and_type(*, tower, radar_type, samples, parameters):
    """一个 ``tower × radar_type`` 的候选 panel 列表（已去重、已剪枝）。"""

    geometry = []
    reachable = []
    for sample in samples:
        values = geometry_evaluation(
            origin_egm2008_m=tower["origin_egm2008_m"],
            sample_egm2008_m=sample["egm2008_m"],
            sample_metric=sample["metric"],
            tower_metric=tower["metric"],
        )
        geometry.append(values)
        if physically_reachable(geometry=values, parameters=parameters):
            reachable.append(values)

    statistics = {
        "tower_id": tower["tower_id"],
        "radar_type": radar_type,
        "sample_count": len(samples),
        "physically_reachable_sample_count": len(reachable),
        "raw_direction_count": 0,
        "deduped_direction_count": 0,
        "covering_direction_count": 0,
        "unique_coverage_set_count": 0,
        "candidate_panel_count": 0,
    }
    if not reachable:
        return [], statistics

    raw_azimuths = []
    for values in reachable:
        for offset in CANDIDATE_AZIMUTH_OFFSETS_DEG:
            raw_azimuths.append(normalize_azimuth_deg(values["bearing_deg"] + offset))
    azimuths = dedupe_azimuths(raw_azimuths, tolerance_deg=AZIMUTH_DEDUPE_TOLERANCE_DEG)
    statistics["raw_direction_count"] = len(raw_azimuths)
    statistics["deduped_direction_count"] = len(azimuths)

    # 每个方向的 coverage set。
    by_azimuth = []
    for azimuth in azimuths:
        covered = []
        for index, sample in enumerate(samples):
            values = geometry[index]
            if not physically_reachable(geometry=values, parameters=parameters):
                continue
            if circular_angle_delta_deg(values["bearing_deg"], azimuth) <= float(
                parameters["azimuth_half_width_deg"]
            ):
                covered.append(index)
        if covered:
            by_azimuth.append({
                "azimuth_deg": azimuth,
                "covered": _coverage_signature(covered),
            })
    statistics["covering_direction_count"] = len(by_azimuth)

    # 相同 coverage set 只留一个确定性代表（azimuth 已升序，取第一个）。
    unique, seen = [], set()
    for entry in by_azimuth:
        if entry["covered"] in seen:
            continue
        seen.add(entry["covered"])
        unique.append(entry)
    statistics["unique_coverage_set_count"] = len(unique)

    # 删除被另一候选完全包含的 dominated panel（真子集才删，保持语义保守与确定）。
    kept = []
    for index, entry in enumerate(unique):
        dominated_by = None
        for other_index, other in enumerate(unique):
            if other_index == index:
                continue
            if len(other["covered"]) <= len(entry["covered"]):
                continue
            if set(entry["covered"]).issubset(other["covered"]):
                if dominated_by is None or other["azimuth_deg"] < dominated_by:
                    dominated_by = other["azimuth_deg"]
        if dominated_by is None:
            kept.append(entry)
        else:
            statistics.setdefault("dominated_removed", []).append({
                "azimuth_deg": entry["azimuth_deg"],
                "covered_sample_count": len(entry["covered"]),
                "dominated_by_azimuth_deg": dominated_by,
            })
    statistics["candidate_panel_count"] = len(kept)

    panels = []
    for entry in kept:
        panels.append({
            "panel_id": _candidate_id(tower["tower_id"], radar_type, entry["azimuth_deg"]),
            "tower_id": tower["tower_id"],
            "radar_type": radar_type,
            "azimuth_deg": entry["azimuth_deg"],
            "panel_half_width_deg": float(parameters["azimuth_half_width_deg"]),
            "covered_sample_indices": list(entry["covered"]),
            "covered_sample_count": len(entry["covered"]),
            "semantics": "single_face_array_panel_candidate",
        })
    return panels, statistics


def build_candidates(*, towers, samples):
    """构造全部 ``tower × radar_type`` 候选 panel。

    返回 ``{"towers", "samples", "panels", "statistics", "unusable_towers"}``，
    结果**确定性**：同一输入必然产生同一 panel 顺序与同一 panel_id 集合。
    """

    tower_records, unusable = build_tower_records(towers)
    sample_records = build_sample_records(samples)

    panels, per_tower = [], []
    for tower in tower_records:
        tower_entry = {"tower_id": tower["tower_id"], "radar_types": {}}
        for radar_type in RADAR_TYPES:
            parameters = radar_geometry_parameters(radar_type)
            produced, statistics = candidates_for_tower_and_type(
                tower=tower, radar_type=radar_type, samples=sample_records,
                parameters=parameters,
            )
            panels.extend(produced)
            tower_entry["radar_types"][radar_type] = statistics
        per_tower.append(tower_entry)

    panels.sort(key=lambda item: (item["tower_id"], item["radar_type"], item["azimuth_deg"]))
    return {
        "towers": tower_records,
        "samples": sample_records,
        "panels": panels,
        "statistics": {
            "tower_count": len(tower_records),
            "unusable_tower_count": len(unusable),
            "sample_count": len(sample_records),
            "candidate_panel_count": len(panels),
            "per_tower": per_tower,
            "max_panels_per_tower": MAX_PANELS_PER_TOWER,
            "azimuth_offsets_deg": list(CANDIDATE_AZIMUTH_OFFSETS_DEG),
            "azimuth_dedupe_tolerance_deg": AZIMUTH_DEDUPE_TOLERANCE_DEG,
            "generation_semantics": "range_elevation_filter_then_bearing_derived_directions",
            "deterministic": True,
        },
        "unusable_towers": unusable,
    }


__all__ = [
    "AZIMUTH_DEDUPE_TOLERANCE_DEG", "CANDIDATE_AZIMUTH_OFFSETS_DEG",
    "build_candidates", "build_sample_records", "build_tower_records",
    "candidates_for_tower_and_type",
]
