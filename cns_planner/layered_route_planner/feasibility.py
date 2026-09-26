"""Neutral terrain/building/tower feasibility-mask construction for layered routing."""

from __future__ import annotations

import math

from ..domain.layered_route import (
    COARSE_ENVELOPE_SEMANTICS, default_layer_feasibility_mask,
    feasibility_policy_fingerprint, feasibility_policy_is_runnable,
    mask_cell, mask_fingerprint, request_fingerprint, stable_fingerprint,
)
from ..domain.building_clearance import (
    building_roof_elevation, evaluate_vertical_clearance,
)
#: Set on every mask produced here.
MASK_SCOPE = "coarse_strategic_vertical_envelope"

def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _round(value):
    return None if value is None else round(float(value), 9)


def _terrain_elevation(terrain_fact):
    fact = terrain_fact if isinstance(terrain_fact, dict) else {}
    value = fact.get("surface_elevation_max_egm2008_m")
    if str(fact.get("data_status") or "") == "passed" and _finite(value):
        return float(value), None
    return None, str(fact.get("reason") or "terrain_data_unavailable")


def _tower_profile_status(cells):
    """铁塔障碍物事实的整体状态（只描述事实，不做任何风险判断）。"""

    statuses = set()
    for cell in cells or []:
        if not isinstance(cell, dict):
            continue
        fact = cell.get("towers") if isinstance(cell.get("towers"), dict) else {}
        statuses.add(str(fact.get("data_status") or "no_towers"))
    if "unknown" in statuses:
        return "unresolved_tower_heights_present"
    if "passed" in statuses:
        return "resolved"
    return "no_towers_in_scope"


def _grid_audit(source_audits, role):
    """One source audit record (asset identity), or ``None`` when it is not registered."""

    items = (source_audits or {}).get("items")
    if not isinstance(items, dict):
        return None
    entry = items.get(role)
    if not isinstance(entry, dict):
        return None
    return {
        "status": entry.get("status"),
        "source_id": entry.get("source_id"),
        "version_fingerprint": entry.get("version_fingerprint"),
        "sha256": entry.get("sha256"),
        "size_bytes": entry.get("size_bytes"),
        "mtime_ns": entry.get("mtime_ns"),
    }


def build_layer_feasibility_mask(
    *, request, cruise_altitude, cells, feasibility_policy, building_clearance_policy,
    source_audits=None, grid_level=None, adapter=None, tower_clearance_policy=None,
):
    """Build the coarse strategic vertical envelope for one explicitly selected layer.

    ``cells`` items are GIS-boundary canonical facts::

        {"grid_id": str, "terrain": {...}, "buildings": {...}, "towers": {...}}

    Rules (fail-closed, missing is never zero):

    * no confirmed ``terrain_vertical_clearance_m`` ⇒ every cell is ``unknown``;
    * terrain elevation missing / NoData ⇒ ``unknown``;
    * ``building_count == 0`` ⇒ no vertical building constraint at all;
    * a building cell with ``valid_height_fraction < 1`` or a missing height / missing
      terrain elevation ⇒ ``unknown``, never 0;
    * otherwise ``building_floor = terrain_cell_max + height_max + existing confirmed
      building vertical clearance`` (the existing ``BuildingClearanceV1`` semantics);
    * a cell containing real towers with a confirmed ``tower_clearance_policy`` ⇒
      ``tower_floor = max(tower_top_egm2008) + explicit tower vertical clearance``; a cell
      with towers but an unconfirmed policy or an unresolved tower top is ``unknown``
      (never "no obstacle"), and it is **never** a risk-factor input.
    """

    cruise = cruise_altitude if isinstance(cruise_altitude, dict) else {}
    layer_id = cruise.get("altitude_layer_id") or (request or {}).get("altitude_layer_id")
    altitude = cruise.get("altitude_egm2008_m") if cruise.get("status") == "confirmed" else None
    terrain_clearance = (feasibility_policy or {}).get("terrain_vertical_clearance_m")
    runnable, policy_reason = feasibility_policy_is_runnable(feasibility_policy)
    building_policy = building_clearance_policy if isinstance(building_clearance_policy, dict) else {}
    building_confirmed = (
        str(building_policy.get("status") or "") == "confirmed"
        and building_policy.get("vertical_clearance_m") is not None
    )
    building_vertical = (
        float(building_policy["vertical_clearance_m"]) if building_confirmed else None
    )
    tower_policy = tower_clearance_policy if isinstance(tower_clearance_policy, dict) else {}
    tower_clearance_vertical = tower_policy.get("tower_vertical_clearance_m")
    tower_clearance_horizontal = tower_policy.get("tower_horizontal_clearance_m")
    tower_policy_confirmed = (
        str(tower_policy.get("status") or "") == "confirmed"
        and tower_clearance_vertical is not None
        and tower_clearance_horizontal is not None
    )
    tower_vertical = float(tower_clearance_vertical) if tower_policy_confirmed else None
    records = {}
    for raw in cells or []:
        item = raw if isinstance(raw, dict) else {}
        grid_id = str(item.get("grid_id") or "")
        if not grid_id:
            continue
        records[grid_id] = item

    provenance = {
        "adapter": adapter,
        "feasibility_policy_fingerprint": feasibility_policy_fingerprint(feasibility_policy),
        "building_clearance_policy": {
            "status": building_policy.get("status"),
            "vertical_clearance_m": building_policy.get("vertical_clearance_m"),
            "horizontal_clearance_m": building_policy.get("horizontal_clearance_m"),
            "source": building_policy.get("source"),
            "confirmed": bool(building_policy.get("confirmed")),
            "definition": "domain/building_clearance.py::building_roof_elevation",
            "semantics": "existing_confirmed_building_clearance_policy_reused_not_redefined",
        },
        "terrain_sampling": COARSE_ENVELOPE_SEMANTICS["terrain_floor"],
        "cruise_altitude_conversion": cruise.get("conversion"),
    }

    result_cells = {}
    counts = {"feasible": 0, "blocked": 0, "unknown": 0}
    for grid_id in sorted(records):
        item = records[grid_id]
        elevation, terrain_reason = _terrain_elevation(item.get("terrain"))
        terrain_floor = (
            None if elevation is None or terrain_clearance is None
            else elevation + float(terrain_clearance)
        )
        building_fact = item.get("buildings") if isinstance(item.get("buildings"), dict) else {}
        building_status = str(building_fact.get("data_status") or "unknown")
        building_count = building_fact.get("building_count")
        building_count = int(building_count) if isinstance(building_count, (int, float)) and not isinstance(building_count, bool) else None
        building_height = building_fact.get("height_max_m")
        building_height = float(building_height) if _finite(building_height) else None
        fraction = building_fact.get("valid_height_fraction")
        fraction = float(fraction) if _finite(fraction) else None
        building_floor = None
        reason_code, reason = None, None

        if not runnable:
            status, reason_code, reason = "unknown", "terrain_clearance_not_confirmed", (
                "terrain_vertical_clearance_m 未确认（无默认值）：该格垂向可行性未知，既不是 "
                "feasible 也不是 blocked"
            )
        elif altitude is None:
            status, reason_code, reason = "unknown", "terrain_clearance_not_confirmed", (
                "selected AltitudeLayer 无法解析为 canonical EGM2008 巡航高度"
            )
        elif elevation is None or terrain_floor is None:
            status, reason_code, reason = "unknown", "terrain_data_unavailable", (
                f"FABDEM cell max 不可用：{terrain_reason or 'terrain_data_unavailable'}"
            )
        elif altitude < terrain_floor:
            status = "blocked"
            reason_code = "altitude_below_terrain_floor"
            reason = (
                f"巡航高度 {_round(altitude)} m 低于 terrain floor {_round(terrain_floor)} m "
                f"（= FABDEM cell max {_round(elevation)} + 显式 terrain clearance "
                f"{_round(terrain_clearance)}）"
            )
        elif building_status != "passed" or building_count is None:
            status, reason_code, reason = "unknown", "building_grid_missing_or_outside_coverage", (
                "L8 building grid fact 缺失或超出覆盖范围：建筑垂向约束未知，绝不当 0"
            )
        elif building_count == 0:
            status = "feasible"
            reason_code = None
            reason = "该 L8 格 building_count=0：没有建筑垂向约束"
        elif fraction is None or fraction < 1.0 or building_height is None:
            status, reason_code, reason = (
                "unknown", "building_height_or_ground_elevation_unresolved",
                "valid_height_fraction<1 或 height_max/terrain 缺失：建筑垂向约束未知，绝不当 0",
            )
        elif not building_confirmed:
            status, reason_code, reason = (
                "unknown", "building_vertical_clearance_not_confirmed",
                "building_clearance_policy 未确认：复用既有确认策略，不提供第二套默认净空",
            )
        else:
            roof = building_roof_elevation(elevation, building_height)
            roof_elevation = (
                roof.get("roof_elevation_egm2008_m")
                if roof.get("status") == "resolved" else None
            )
            if roof_elevation is None:
                status, reason_code, reason = (
                    "unknown", "building_height_or_ground_elevation_unresolved",
                    "building roof elevation 无法解析（既有 BuildingClearanceV1 语义）：未知，绝不当 0",
                )
            else:
                building_floor = float(roof_elevation) + building_vertical
                clearance = evaluate_vertical_clearance(
                    minimum_altitude_egm2008_m=altitude,
                    roof_elevation_egm2008_m=roof_elevation,
                    required_clearance_m=building_vertical,
                    ground_elevation_m=elevation,
                )
                if (
                    clearance.get("status") == "resolved"
                    and clearance.get("vertical_margin_m") is not None
                    and float(clearance["vertical_margin_m"]) >= 0.0
                ):
                    status, reason_code, reason = "feasible", None, "满足 coarse 建筑垂向包络"
                elif clearance.get("reason") == "aircraft_below_building_ground":
                    status = "blocked"
                    reason_code = "altitude_below_terrain_floor"
                    reason = (
                        f"巡航高度 {_round(altitude)} m 低于该格建筑地面高程 "
                        f"{_round(elevation)} m"
                    )
                else:
                    status = "blocked"
                    reason_code = "altitude_below_building_clearance_floor"
                    reason = (
                        f"巡航高度 {_round(altitude)} m 低于 building required clearance "
                        f"floor {_round(building_floor)} m（= terrain cell max + height_max + "
                        "既有建筑垂直净空）"
                    )

        # ---- 真实铁塔净空（Obstacle / Clearance，**不是** risk factor） --------------
        # 顺序与 COARSE_ENVELOPE_SEMANTICS 一致：terrain → building → tower。
        # 塔是点几何事实，网格只把它索引到相关 cell；同一 cell 内取塔顶最高值（保守）。
        # 有任何一塔高度未解析 ⇒ unknown（绝不当作"没有塔"）。
        tower_fact = item.get("towers") if isinstance(item.get("towers"), dict) else {}
        tower_count = tower_fact.get("tower_count")
        tower_count = int(tower_count) if _finite(tower_count) else 0
        tower_unresolved = tower_fact.get("unresolved_count")
        tower_unresolved = int(tower_unresolved) if _finite(tower_unresolved) else 0
        tower_top = tower_fact.get("tower_top_max_egm2008_m")
        tower_top = float(tower_top) if _finite(tower_top) else None
        tower_status, tower_reason_code, tower_reason = "not_applicable", None, None
        if tower_count > 0:
            if not tower_policy_confirmed:
                tower_status = "unknown"
                tower_reason_code = "tower_clearance_not_configured"
                tower_reason = (
                    f"该格有 {tower_count} 个真实铁塔，但 tower_clearance_policy 未确认"
                    "（垂直/水平净空都没有默认值）：塔净空未知，既不是 feasible 也不是 blocked"
                )
            elif tower_fact.get("data_status") != "passed" or tower_top is None:
                # resolved 只是解析状态；**未确认**的塔顶（或未解析）不构成 hard obstacle：
                # 该格保持 unknown 并在搜索中 fail-closed，绝不静默当作"净空足够"。
                tower_status = "unknown"
                tower_reason_code = str(
                    tower_fact.get("reason") or "tower_height_unresolved"
                )
                if tower_reason_code == "tower_top_not_confirmed":
                    tower_reason = (
                        f"该格有 {tower_count} 个真实铁塔，塔顶正高虽已解析（最高 "
                        f"{_round(tower_top)} m 仅作诊断）但**未确认**："
                        "未确认塔顶不是硬障碍证据，该格保持 unknown，fail-closed"
                    )
                else:
                    tower_reason = (
                        f"该格有 {tower_count} 个真实铁塔，其中 {tower_unresolved} 个塔顶 EGM2008 "
                        "正高未解析：不生成具体 tower clearance floor，该格保持 unknown，"
                        "在路径搜索中 fail-closed，不得作为已验证安全可通行区域"
                    )
            else:
                tower_floor = tower_top + tower_clearance_vertical
                if altitude is None:
                    tower_status = "unknown"
                    tower_reason_code = "tower_clearance_not_configured"
                    tower_reason = "巡航高度无法解析为 canonical EGM2008，塔净空未知"
                elif altitude < tower_floor:
                    tower_status = "blocked"
                    tower_reason_code = "altitude_below_tower_clearance_floor"
                    tower_reason = (
                        f"巡航高度 {_round(altitude)} m 低于 tower clearance floor "
                        f"{_round(tower_floor)} m（= 该格真实塔顶最高 "
                        f"{_round(tower_top)} m + 显式 tower 垂直净空 "
                        f"{_round(tower_clearance_vertical)}）"
                    )
                else:
                    tower_status = "passed"
        tower_resolved_top = tower_top if tower_status in ("passed", "blocked") else None
        if tower_status == "blocked" and status != "blocked":
            status, reason_code, reason = "blocked", tower_reason_code, tower_reason
        elif tower_status == "unknown" and status == "feasible":
            status, reason_code, reason = "unknown", tower_reason_code, tower_reason

        counts[status] += 1
        cell = mask_cell(
            grid_id, status=status, cruise_altitude_egm2008_m=altitude,
            terrain_elevation_m=_round(elevation), terrain_floor_egm2008_m=_round(terrain_floor),
            terrain_clearance_m=None if terrain_clearance is None else float(terrain_clearance),
            building_count=building_count,
            building_height_max_m=_round(building_height),
            building_required_clearance_egm2008_m=_round(building_floor),
            reason_code=reason_code, reason=reason,
            tower_count=(tower_count or None),
            tower_unresolved_count=(tower_unresolved or None),
            tower_top_max_egm2008_m=_round(tower_resolved_top),
            tower_required_clearance_egm2008_m=(
                _round(tower_resolved_top + tower_clearance_vertical)
                if tower_resolved_top is not None and tower_policy_confirmed else None
            ),
            provenance={
                **provenance,
                "terrain_data_status": (item.get("terrain") or {}).get("data_status"),
                "building_data_status": building_fact.get("data_status"),
                "valid_height_fraction": fraction,
                "building_clearance_policy_status": building_policy.get("status"),
                "tower_data_status": tower_fact.get("data_status"),
                "tower_clearance_policy_status": tower_policy.get("status"),
            },
        )
        result_cells[grid_id] = cell

    mask = default_layer_feasibility_mask(layer_id)
    mask.update({
        "status": "passed" if result_cells else "missing_data",
        "altitude_layer_id": layer_id,
        "grid_level": grid_level,
        "cell_count": len(result_cells),
        "counts": counts,
        "cells": result_cells,
        "terrain_vertical_clearance_m": (
            None if terrain_clearance is None else float(terrain_clearance)
        ),
        "building_vertical_clearance_m": building_vertical,
        "building_clearance_policy_status": building_policy.get("status"),
        "building_clearance_source": building_policy.get("source"),
        # ---- 真实铁塔净空（障碍物，不是风险因子） --------------------------------
        "tower_vertical_clearance_m": tower_vertical,
        "tower_horizontal_clearance_m": (
            float(tower_clearance_horizontal) if tower_policy_confirmed else None
        ),
        "tower_clearance_policy_status": tower_policy.get("status") or "not_configured",
        "tower_clearance_source": tower_policy.get("source"),
        "tower_obstacle_profile_status": _tower_profile_status(cells),
        "tower_cell_count": sum(
            1 for cell in result_cells.values() if (cell.get("tower_count") or 0) > 0
        ),
        "tower_unresolved_cell_count": sum(
            1 for cell in result_cells.values() if (cell.get("tower_unresolved_count") or 0) > 0
        ),
        "tower_obstacle_semantics": "obstacle_clearance_not_a_risk_factor",
        "cruise_altitude": {
            "status": cruise.get("status"),
            "altitude_egm2008_m": altitude,
            "vertical_reference": cruise.get("vertical_reference"),
            "nominal_altitude_m": cruise.get("nominal_altitude_m"),
            "conversion": cruise.get("conversion"),
            "reason": cruise.get("reason"),
        },
        "feasibility_policy_fingerprint": feasibility_policy_fingerprint(feasibility_policy),
        "request_fingerprint": request_fingerprint(request),
        "input_fingerprint": stable_fingerprint({
            "request_fingerprint": request_fingerprint(request),
            "feasibility_policy": feasibility_policy_fingerprint(feasibility_policy),
            "building_clearance_policy": {
                "status": building_policy.get("status"),
                "vertical_clearance_m": building_policy.get("vertical_clearance_m"),
                "source": building_policy.get("source"),
            },
            "tower_clearance_policy": {
                "status": tower_policy.get("status"),
                "tower_vertical_clearance_m": tower_policy.get("tower_vertical_clearance_m"),
                "tower_horizontal_clearance_m": tower_policy.get("tower_horizontal_clearance_m"),
                "source": tower_policy.get("source"),
            },
            "cruise_altitude": cruise.get("altitude_egm2008_m"),
            "cells": {
                grid_id: {
                    "terrain": (records[grid_id].get("terrain") or {}),
                    "buildings": (records[grid_id].get("buildings") or {}),
                    "towers": (records[grid_id].get("towers") or {}),
                }
                for grid_id in sorted(records)
            },
        }, prefix="layeredmaskinputv1-"),
        "source_audits": {
            role: _grid_audit(source_audits, role)
            for role in ("terrain_dtm", "buildings", "building_grid")
        },
        "adapter": adapter,
        "feasibility_policy_reason": policy_reason,
    })
    mask["mask_fingerprint"] = mask_fingerprint(mask)
    return mask

