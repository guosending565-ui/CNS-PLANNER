"""BUG-ROUTE-005（收口后）：规划用暴露度层 = 陆地统一相对风险基线。

语义（**只**改这一层，Theta* 核心搜索、0.8/0.1/0.1 权重、Risk Framework V2 canonical
population、WorldPop、Population NoData、shelter 数学全部不动）：

    p = canonical Risk Framework V2 归一化人口因子（[0, 1]，唯一来源）
    L = 1 if land else 0 if water（地形证据判定）
    b = land_relative_risk_baseline（0 <= b < 1，显式 confirmed 才生效）

    planning_population_factor = (1 - b) * p + b * L

因此：

* 同一 ``p`` 下 land − water 差值恒等于 ``b``；
* 所有人口相对差异统一保留 ``(1 - b)``，**不是**只抬低人口区域；
* 输出天然落在 ``[0, 1]``（凸组合），**不使用 clip**，因此不会让高风险陆地饱和；
* land 内部排序、water 内部排序保持不变（同一格集内是同一个仿射映射）；
* ``risk_index = planning_population_factor × shelter_coefficient``（定义未改）。

已废弃的旧语义 ``land_population_floor`` / ``max(population_density, floor)``：

* ``land_population_floor`` 只作为**读取兼容**字段保留并标记 ``deprecated``；
* 它**绝不**参与规划计算，也**绝不**被静默换算成任何基线值
  （"5 person/km²" 与 "0.08" 之间没有任何自动换算）；
* 旧项目里已保存的 floor 不会让本层生效：缺少显式确认的新基线时策略保持
  ``not_configured``，必须由用户重新显式确认 ``land_relative_risk_baseline``。

严格边界（本模块**只**产出规划用的派生层）：

* **不修改真实人口数据**：``grid_attributes.population`` 一个字段都不改，也不改人口报告、
  数据审计或 Population NoData 语义。
* **不修改 NoData 语义**：海面 NoData 仍按项目已确认的 ``nodata_is_zero_population`` 或
  ``unresolved`` 处理。本层绝不把 NoData 当海，也绝不补 0
  （``used_population_nodata_as_sea_proxy = false``）。
* **不重新定义 population × shelter 风险**：``risk_index`` 仍由 ``population_shelter``
  按 ``normalized_population_factor × shelter_coefficient`` 计算，本层只替换这一步规划用的
  人口因子输入。
* **海 / 陆判定只用地形证据**：既有的逐格地表最大高程（``surface_elevation_max_m``）与
  policy 显式确认的阈值比较。绝不使用"population NoData ⇒ 海面"这类推断。
* 没有地形证据的格保持 ``unresolved`` 并 **fail-closed**（``planning_population_factor = None``）：
  既不猜海面，也不退回不带基线的 canonical 因子。
"""

from __future__ import annotations

from copy import deepcopy
import math

from .risk_v2 import stable_fingerprint
from ..risk.normalization import finite

SCHEMA_VERSION = "planning-exposure-v2"

#: policy 未配置时后端自己的占位说明（不作为可编辑值回填）。
PENDING_SOURCE = "未配置；必须由项目工程依据显式确认 planning_exposure_policy"

#: 陆地判据的**唯一**方法：已确认的"地表最大高程阈值"。记录在 provenance 里，便于审计。
LAND_DETECTION_METHOD = "confirmed_surface_elevation_max_threshold"

#: 本层绝不把 population NoData 当作海面代理。
USED_POPULATION_NODATA_AS_SEA_PROXY = False

#: 正式规划公式（唯一一种写法，记录在 policy / attribute / provenance）。
PLANNING_FACTOR_FORMULA = "planning_population_factor = (1-b)*p + b*L"

#: ``b`` 的合法范围：``0 <= b < 1``（``b = 1`` 会把所有相对差异抹平并被拒绝）。
LAND_RELATIVE_RISK_BASELINE_RANGE = (0.0, 1.0)

#: 首轮工程建议值。**不是默认值**：它只作为 UI 的可选预填/占位，必须显式确认才生效。
ENGINEERING_SUGGESTED_LAND_RELATIVE_RISK_BASELINE = 0.08

LAND_STATUSES = ("land", "water", "unresolved")

#: 逐格地表最大高程的字段候选（现有 terrain 映射 / layered feasibility 事实）。
TERRAIN_ELEVATION_FIELDS = ("surface_elevation_max_m", "surface_elevation_max_egm2008_m")

POPULATION_DENSITY_FIELD = "population_density_people_km2"

#: ``population_shelter`` 上标记"本层已替换规划用人口因子"的来源标识。
PLANNING_EXPOSURE_POPULATION_FACTOR_SOURCE = "planning_exposure_land_relative_risk_baseline"

LAND_INDICATOR_LAND = 1.0
LAND_INDICATOR_WATER = 0.0


def default_planning_exposure_policy():
    """No land relative risk baseline is assumed without an explicit, confirmed policy."""

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "not_configured",
        "status_reason": "planning_exposure_policy_not_configured",
        "enabled": False,
        "land_relative_risk_baseline": None,
        "land_relative_risk_baseline_range": list(LAND_RELATIVE_RISK_BASELINE_RANGE),
        "unit": "dimensionless_relative_risk_baseline_0_to_just_below_1",
        "formula": PLANNING_FACTOR_FORMULA,
        # --- deprecated：只读取兼容，绝不参与规划，绝不换算成 baseline ---
        "land_population_floor": None,
        "land_population_floor_unit": "person/km2",
        "land_population_floor_deprecated": True,
        "land_population_floor_used_for_planning": False,
        "land_min_surface_elevation_m": None,
        "land_detection_method": LAND_DETECTION_METHOD,
        "used_population_nodata_as_sea_proxy": USED_POPULATION_NODATA_AS_SEA_PROXY,
        "source": PENDING_SOURCE,
        "evidence": None,
        "confirmed": False,
        "provenance": "not_configured",
        "semantics": {
            "planning_only": True,
            "changes_population_report": False,
            "changes_population_nodata_semantics": False,
            "changes_population_shelter_definition": False,
            "land_detection_uses_terrain_evidence_only": True,
            "missing_evidence_is_unresolved_not_sea": True,
            "relative_baseline_is_not_an_absolute_density": True,
            "population_difference_retention_is_one_minus_b": True,
            "land_water_gap_equals_b_at_equal_population_factor": True,
            "output_stays_in_unit_interval_without_clip": True,
            "never_converts_land_population_floor_to_a_baseline": True,
            "land_population_floor_is_deprecated": True,
        },
    }


def _optional_text(value):
    text = value if isinstance(value, str) else None
    text = text.strip() if text is not None else None
    return text or None


def normalize_planning_exposure_policy(value):
    """Validate one explicit planning-exposure policy.

    ``enabled`` 只有在 ``confirmed`` 为真、``land_relative_risk_baseline`` 落在
    ``0 <= b < 1``、且 ``land_min_surface_elevation_m`` 是有限数时才可能生效；缺任何一项都保持
    ``not_configured`` / ``pending_confirmation``，绝不猜阈值、绝不猜基线。

    旧字段 ``land_population_floor`` 只被读取记录（``deprecated``），既不校验成规划参数，
    也不会被换算成 ``land_relative_risk_baseline``。
    """

    source = value if isinstance(value, dict) else {}
    result = default_planning_exposure_policy()

    raw_baseline = source.get("land_relative_risk_baseline")
    baseline = None
    if raw_baseline not in (None, ""):
        baseline = float(raw_baseline)
        if not math.isfinite(baseline) or not 0.0 <= baseline < 1.0:
            raise ValueError(
                "land_relative_risk_baseline 必须是 0 ≤ b < 1 内的有限数（无量纲陆地相对风险基线）"
            )

    # deprecated：只读取兼容。绝不换算，绝不参与规划。
    raw_floor = source.get("land_population_floor")
    floor = None
    if raw_floor not in (None, ""):
        floor = float(raw_floor)
        if not math.isfinite(floor) or floor < 0.0:
            raise ValueError("land_population_floor 必须是非负有限数（person/km2，该字段已废弃）")

    raw_threshold = source.get("land_min_surface_elevation_m")
    threshold = None
    if raw_threshold not in (None, ""):
        threshold = float(raw_threshold)
        if not math.isfinite(threshold):
            raise ValueError("land_min_surface_elevation_m 必须是有限数（m）")

    confirmed = bool(source.get("confirmed"))
    enabled = bool(source.get("enabled"))
    source_text = _optional_text(source.get("source"))
    provenance = _optional_text(source.get("provenance"))

    result.update({
        "enabled": enabled,
        "land_relative_risk_baseline": baseline,
        "land_population_floor": floor,
        "land_min_surface_elevation_m": threshold,
        "source": source_text or result["source"],
        "evidence": source.get("evidence") if isinstance(source.get("evidence"), dict) else None,
        "confirmed": confirmed,
        "provenance": provenance or result["provenance"],
    })

    if not enabled:
        result["status"] = "not_configured"
        result["status_reason"] = "planning_exposure_disabled"
        return result
    if baseline is None:
        # 旧项目的 land_population_floor 只停留在 deprecated 字段里：不会让本层生效。
        result["status"] = "not_configured"
        result["status_reason"] = "land_relative_risk_baseline_not_configured"
        return result
    if threshold is None:
        result["status"] = "not_configured"
        result["status_reason"] = "land_detection_threshold_not_configured"
        return result
    if not confirmed:
        result["status"] = "pending_confirmation"
        result["status_reason"] = "planning_exposure_policy_not_confirmed"
        return result
    result["status"] = "confirmed"
    result["status_reason"] = None
    return result


def planning_exposure_policy_fingerprint(policy):
    """Fingerprint of the **planning-relevant** policy fields.

    ``land_population_floor`` 刻意不参与：它已废弃且不影响任何规划输出，改它不应使派生缓存失效。
    """

    policy = policy if isinstance(policy, dict) else {}
    return stable_fingerprint({
        "schema_version": SCHEMA_VERSION,
        "status": policy.get("status"),
        "enabled": bool(policy.get("enabled")),
        "land_relative_risk_baseline": policy.get("land_relative_risk_baseline"),
        "land_min_surface_elevation_m": policy.get("land_min_surface_elevation_m"),
        "land_detection_method": policy.get("land_detection_method") or LAND_DETECTION_METHOD,
        "formula": PLANNING_FACTOR_FORMULA,
        "source": policy.get("source"),
        "confirmed": bool(policy.get("confirmed")),
        "provenance": policy.get("provenance"),
    }, prefix="planningexpv2-")


def planning_exposure_is_active(policy):
    """A policy only takes effect when it is explicitly ``enabled`` **and** ``confirmed``."""

    if not isinstance(policy, dict):
        return False
    baseline = policy.get("land_relative_risk_baseline")
    return (
        policy.get("enabled") is True
        and policy.get("confirmed") is True
        and finite(baseline)
        and 0.0 <= float(baseline) < 1.0
        and finite(policy.get("land_min_surface_elevation_m"))
    )


def _round_or_none(value):
    return None if not finite(value) else round(float(value), 9)


def _grid_ids(grid):
    return [
        str(cell.get("grid_id"))
        for cell in ((grid or {}).get("cells") or [])
        if isinstance(cell, dict) and cell.get("grid_id")
    ]


def _cell_of(attribute, grid_id):
    cells = (attribute or {}).get("cells")
    if not isinstance(cells, dict):
        return {}
    cell = cells.get(grid_id)
    return cell if isinstance(cell, dict) else {}


def _terrain_elevation(terrain_attribute, grid_id):
    cell = _cell_of(terrain_attribute, grid_id)
    for field in TERRAIN_ELEVATION_FIELDS:
        value = cell.get(field)
        if finite(value):
            return float(value), field
    return None, None


def _population_reference_value(grid_risk_v2, explicit_reference):
    """Risk Framework V2 的 population 归一化参考值（**仅信息性记录**，本层不再用它换算）。

    新公式只需要 canonical 归一化因子本身，因此参考值缺失不会阻塞本层；记录它只是为了让
    provenance 能追溯"因子来自哪个归一化参考"。
    """

    if finite(explicit_reference):
        return float(explicit_reference)
    item = grid_risk_v2 if isinstance(grid_risk_v2, dict) else {}
    for container in (
        (item.get("factor_status") or {}).get("population_exposure") or {},
        (item.get("references") or {}).get("population_exposure") or {},
    ):
        normalization = container.get("normalization")
        if isinstance(normalization, dict):
            reference = normalization.get("reference")
            if isinstance(reference, dict) and finite(reference.get("value")):
                return float(reference["value"])
        if finite(container.get("value")):
            return float(container["value"])
    return None


def _policy_summary(policy):
    return {
        "status": policy.get("status"),
        "status_reason": policy.get("status_reason"),
        "enabled": bool(policy.get("enabled")),
        "confirmed": bool(policy.get("confirmed")),
        "land_relative_risk_baseline": policy.get("land_relative_risk_baseline"),
        "land_relative_risk_baseline_range": list(LAND_RELATIVE_RISK_BASELINE_RANGE),
        "formula": PLANNING_FACTOR_FORMULA,
        "land_min_surface_elevation_m": policy.get("land_min_surface_elevation_m"),
        "land_detection_method": LAND_DETECTION_METHOD,
        "land_population_floor": policy.get("land_population_floor"),
        "land_population_floor_deprecated": True,
        "land_population_floor_used_for_planning": False,
        "source": policy.get("source"),
        "evidence": deepcopy(policy.get("evidence")),
        "provenance": policy.get("provenance"),
    }


def empty_planning_exposure_attribute(policy=None, *, status="not_configured", reason=None):
    policy = normalize_planning_exposure_policy(
        policy if policy is not None else default_planning_exposure_policy()
    )
    baseline = policy.get("land_relative_risk_baseline")
    return {
        "schema_version": SCHEMA_VERSION,
        "attribute": "planning_exposure",
        "status": status,
        "reason": reason,
        "enabled": bool(policy.get("enabled")),
        "applied": False,
        "policy": _policy_summary(policy),
        "policy_fingerprint": planning_exposure_policy_fingerprint(policy),
        "formula": PLANNING_FACTOR_FORMULA,
        "land_relative_risk_baseline": baseline,
        "land_relative_risk_baseline_range": list(LAND_RELATIVE_RISK_BASELINE_RANGE),
        "land_water_gap": baseline,
        "population_difference_retention": (
            None if not finite(baseline) else _round_or_none(1.0 - float(baseline))
        ),
        "land_population_floor": policy.get("land_population_floor"),
        "land_population_floor_deprecated": True,
        "land_population_floor_used_for_planning": False,
        "land_min_surface_elevation_m": policy.get("land_min_surface_elevation_m"),
        "land_detection_method": LAND_DETECTION_METHOD,
        "used_population_nodata_as_sea_proxy": USED_POPULATION_NODATA_AS_SEA_PROXY,
        "population_reference": None,
        "population_factor_source": "canonical_risk_v2_population_factor",
        "grid_level": None,
        "count": 0,
        "land_count": 0,
        "water_count": 0,
        "unresolved_land_status_count": 0,
        "unresolved_population_factor_count": 0,
        "unresolved_cell_count": 0,
        "applied_count": 0,
        "land_baseline_raised_count": 0,
        "cells": {},
        "provenance": {
            "definition": "domain/planning_exposure.py::empty_planning_exposure_attribute",
            "planning_only": True,
            "formula": PLANNING_FACTOR_FORMULA,
            "changed_population_report": False,
            "changed_population_shelter_definition": False,
            "no_clip_used": True,
            "land_population_floor": "deprecated_not_used_for_planning",
        },
        "field_fingerprint": stable_fingerprint(
            {"policy": planning_exposure_policy_fingerprint(policy), "status": status},
            prefix="planningexpfieldv2-",
        ),
    }


def resolve_planning_exposure(
    *, grid, population_attribute, terrain_attribute, normalized_population_factors,
    policy, grid_risk_v2=None, population_reference=None,
):
    """Derive the planning-only exposure layer from terrain evidence and a confirmed policy.

    Returns an additive attribute.  When the policy is not active the result is
    ``not_configured`` and ``applied=False``: the caller must then keep using the original
    population factors unchanged.
    """

    policy = normalize_planning_exposure_policy(policy)
    grid_ids = _grid_ids(grid)
    if not planning_exposure_is_active(policy):
        attribute = empty_planning_exposure_attribute(
            policy, status="not_configured", reason=policy.get("status_reason"),
        )
        attribute["count"] = len(grid_ids)
        attribute["unresolved_cell_count"] = len(grid_ids)
        attribute["grid_level"] = (grid or {}).get("level")
        return attribute

    reference = _population_reference_value(grid_risk_v2, population_reference)
    baseline = float(policy["land_relative_risk_baseline"])
    threshold = float(policy["land_min_surface_elevation_m"])
    retention = 1.0 - baseline

    factors = normalized_population_factors if isinstance(
        normalized_population_factors, dict
    ) else {}
    cells = {}
    land_count = water_count = unresolved_land = 0
    applied = raised = unresolved_factor = 0
    for grid_id in grid_ids:
        elevation, elevation_field = _terrain_elevation(terrain_attribute, grid_id)
        if elevation is None:
            land_status = "unresolved"
            terrain_reason = "terrain_elevation_evidence_missing"
        elif elevation > threshold:
            land_status, terrain_reason = "land", None
        else:
            land_status, terrain_reason = "water", None
        if land_status == "land":
            land_count += 1
        elif land_status == "water":
            water_count += 1
        else:
            unresolved_land += 1

        population_cell = _cell_of(population_attribute, grid_id)
        real_density = population_cell.get(POPULATION_DENSITY_FIELD)
        real_density = float(real_density) if finite(real_density) else None
        population_factor = factors.get(grid_id)
        population_factor = float(population_factor) if finite(population_factor) else None

        if land_status == "land":
            land_indicator = LAND_INDICATOR_LAND
        elif land_status == "water":
            land_indicator = LAND_INDICATOR_WATER
        else:
            land_indicator = None

        # fail-closed：缺地形证据或缺 canonical 因子时 planning factor 一律 None，
        # 既不猜海面，也不退回"不带基线"的因子（那会让本层看起来没生效）。
        reason = None
        planning_factor = None
        if population_factor is None:
            unresolved_factor += 1
            reason = "population_factor_unresolved"
        elif not 0.0 <= population_factor <= 1.0:
            unresolved_factor += 1
            reason = "population_factor_out_of_range"
        elif land_indicator is None:
            reason = "terrain_elevation_evidence_missing"
        else:
            planning_factor = retention * population_factor + baseline * land_indicator
            applied += 1
            # ``raised`` 只统计**真正被抬高**的陆地格（p = 1 的陆地持平，不算抬高）。
            if land_status == "land" and planning_factor > population_factor + 1e-12:
                raised += 1

        cells[grid_id] = {
            "grid_id": grid_id,
            "status": "passed" if planning_factor is not None else "unresolved",
            "land_status": land_status,
            "land_indicator": land_indicator,
            "surface_elevation_max_m": _round_or_none(elevation),
            "surface_elevation_field": elevation_field,
            "real_population_density_people_km2": _round_or_none(real_density),
            "population_factor": _round_or_none(population_factor),
            "planning_population_factor": _round_or_none(planning_factor),
            "baseline_applied": planning_factor is not None,
            "land_baseline_raised": (
                planning_factor is not None and land_status == "land"
                and planning_factor > population_factor + 1e-12
            ),
            "land_water_gap": (
                None if land_indicator is None else round(baseline, 9)
            ),
            "reason": reason,
            "terrain_reason": terrain_reason,
        }

    statuses = {cell["status"] for cell in cells.values()} or {"not_calculated"}
    if statuses == {"passed"}:
        status = "passed"
    elif statuses == {"unresolved"}:
        status = "missing_data"
    else:
        status = "partial"

    attribute = empty_planning_exposure_attribute(policy, status=status, reason=None)
    attribute.update({
        "enabled": True,
        "applied": True,
        "policy": _policy_summary(policy),
        "policy_fingerprint": planning_exposure_policy_fingerprint(policy),
        "land_relative_risk_baseline": baseline,
        "land_water_gap": baseline,
        "population_difference_retention": _round_or_none(retention),
        "land_population_floor": policy.get("land_population_floor"),
        "land_min_surface_elevation_m": threshold,
        "population_reference": _round_or_none(reference),
        "population_factor_source": PLANNING_EXPOSURE_POPULATION_FACTOR_SOURCE,
        "grid_level": (grid or {}).get("level"),
        "count": len(grid_ids),
        "land_count": land_count,
        "water_count": water_count,
        "unresolved_land_status_count": unresolved_land,
        "unresolved_population_factor_count": unresolved_factor,
        "unresolved_cell_count": len(grid_ids) - applied,
        "applied_count": applied,
        "land_baseline_raised_count": raised,
        "cells": cells,
    })
    attribute["provenance"] = {
        "definition": "domain/planning_exposure.py::resolve_planning_exposure",
        "planning_only": True,
        "formula": PLANNING_FACTOR_FORMULA,
        "land_relative_risk_baseline": baseline,
        "land_water_gap": baseline,
        "population_difference_retention": attribute["population_difference_retention"],
        "counts": {
            "count": len(grid_ids),
            "land_count": land_count,
            "water_count": water_count,
            "unresolved_land_status_count": unresolved_land,
            "unresolved_population_factor_count": unresolved_factor,
            "applied_count": applied,
            "land_baseline_raised_count": raised,
        },
        "policy_fingerprint": attribute["policy_fingerprint"],
        "field_fingerprint": None,  # 见下方 field_fingerprint（此处保持同一容器内可读）
        "changes_population_report": False,
        "changes_population_nodata_semantics": False,
        "changes_population_shelter_definition": False,
        "risk_index_definition_unchanged": "normalized_population_factor × shelter_coefficient",
        "land_detection": LAND_DETECTION_METHOD,
        "land_detection_input": "per_grid_surface_elevation_max",
        "used_population_nodata_as_sea_proxy": USED_POPULATION_NODATA_AS_SEA_PROXY,
        "population_reference_is_informational_only": True,
        "population_factor_source": (
            "canonical Risk Framework V2 normalized population factor (unchanged)"
        ),
        "missing_terrain_evidence": "unresolved_never_sea_and_never_baseline",
        "land_population_floor": "deprecated_not_used_for_planning",
        "no_clip_used": True,
    }
    attribute["field_fingerprint"] = stable_fingerprint({
        "policy": attribute["policy_fingerprint"],
        "formula": PLANNING_FACTOR_FORMULA,
        "reference": reference,
        "threshold": threshold,
        "baseline": baseline,
        "land_count": land_count,
        "water_count": water_count,
        "unresolved_land_status_count": unresolved_land,
        "unresolved_population_factor_count": unresolved_factor,
        "applied_count": applied,
        "land_baseline_raised_count": raised,
    }, prefix="planningexpfieldv2-")
    attribute["provenance"]["field_fingerprint"] = attribute["field_fingerprint"]
    return attribute


def planning_exposure_factors(attribute):
    """The per-grid population factors the planner should use, or ``None`` when inactive.

    ``applied=False`` 时返回 ``None``（调用方必须原样使用 canonical 因子）。已生效时返回
    ``{grid_id: planning_population_factor}``：**unresolved 的格不会出现在这里**（fail-closed），
    调用方必须把它当成"因子缺失"，绝不能回落成 canonical。
    """

    item = attribute if isinstance(attribute, dict) else {}
    if item.get("applied") is not True:
        return None
    cells = item.get("cells") if isinstance(item.get("cells"), dict) else {}
    factors = {}
    for grid_id, cell in cells.items():
        value = (cell or {}).get("planning_population_factor")
        if finite(value):
            factors[str(grid_id)] = float(value)
    return factors


def planning_exposure_fingerprint(attribute):
    item = attribute if isinstance(attribute, dict) else {}
    return stable_fingerprint({
        "policy_fingerprint": item.get("policy_fingerprint"),
        "status": item.get("status"),
        "applied": bool(item.get("applied")),
        "formula": PLANNING_FACTOR_FORMULA,
        "land_relative_risk_baseline": item.get("land_relative_risk_baseline"),
        "land_min_surface_elevation_m": item.get("land_min_surface_elevation_m"),
        "applied_count": item.get("applied_count"),
        "land_count": item.get("land_count"),
        "water_count": item.get("water_count"),
        "land_baseline_raised_count": item.get("land_baseline_raised_count"),
    }, prefix="planningexpattrv2-")


__all__ = [
    "ENGINEERING_SUGGESTED_LAND_RELATIVE_RISK_BASELINE", "LAND_DETECTION_METHOD",
    "LAND_INDICATOR_LAND", "LAND_INDICATOR_WATER", "LAND_RELATIVE_RISK_BASELINE_RANGE",
    "LAND_STATUSES", "PENDING_SOURCE", "PLANNING_EXPOSURE_POPULATION_FACTOR_SOURCE",
    "PLANNING_FACTOR_FORMULA", "POPULATION_DENSITY_FIELD", "SCHEMA_VERSION",
    "TERRAIN_ELEVATION_FIELDS", "USED_POPULATION_NODATA_AS_SEA_PROXY",
    "default_planning_exposure_policy", "empty_planning_exposure_attribute",
    "normalize_planning_exposure_policy", "planning_exposure_factors",
    "planning_exposure_fingerprint", "planning_exposure_is_active",
    "planning_exposure_policy_fingerprint", "resolve_planning_exposure",
]
