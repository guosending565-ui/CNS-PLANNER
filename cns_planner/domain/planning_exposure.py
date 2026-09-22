"""BUG-ROUTE-005：规划用暴露度层（planning exposure layer）。

目标：让"海面大量 NoData、陆地人口稀疏"的工作区（例如舟山）在航路规划里自然偏好海面走廊。

严格边界（本模块**只**产出规划用的派生层，绝不修改任何既有语义）：

* **不修改真实人口数据**：``grid_attributes.population`` 一个字段都不改，也不改人口报告、
  数据审计或 Population NoData Semantics。
* **不修改 NoData 语义**：海面 NoData 仍按项目已确认的 ``nodata_is_zero_population`` 或
  ``unresolved`` 处理。本层绝不把 NoData 当海，也绝不补 0
  （``used_population_nodata_as_sea_proxy = false``）。
* **不重新定义 population × shelter 风险**：``risk_index`` 仍然是
  ``normalized_population_factor × shelter_coefficient``。本层只是把**陆地**格的有效人口抬到
  不低于 ``land_population_floor``，并用 Risk Framework V2 **完全相同**的
  ``log1p_quantile_index`` 把它换算成归一化因子。由于 ``log1p`` 与 ``clip`` 都单调不减，
  ``max(density, floor)`` 的因子恰好等于 ``max(factor(density), factor(floor))`` —— 也就是
  说"先抬升密度再归一化"与"因子取下限"数学等价，本层没有引入第二种归一化定义。
* **海 / 陆判定只用地形证据**：既有的逐格地表最大高程（``surface_elevation_max_m``）与
  policy 显式确认的阈值比较。绝不使用"population NoData ⇒ 海面"这类推断。
* 没有地形证据的格保持 ``unresolved``：既不抬高，也不当成海面。
"""

from __future__ import annotations

from copy import deepcopy
import math

from .risk_v2 import stable_fingerprint
from ..risk.normalization import finite, log1p_quantile_index

SCHEMA_VERSION = "planning-exposure-v1"

#: policy 未配置时后端自己的占位说明（不作为可编辑值回填）。
PENDING_SOURCE = "未配置；必须由项目工程依据显式确认 planning_exposure_policy"

#: 陆地判据的**唯一**方法：已确认的"地表最大高程阈值"。记录在 provenance 里，便于审计。
LAND_DETECTION_METHOD = "confirmed_surface_elevation_max_threshold"

#: 本层绝不把 population NoData 当作海面代理。
USED_POPULATION_NODATA_AS_SEA_PROXY = False

LAND_STATUSES = ("land", "water", "unresolved")

#: 逐格地表最大高程的字段候选（现有 terrain 映射 / layered feasibility 事实）。
TERRAIN_ELEVATION_FIELDS = ("surface_elevation_max_m", "surface_elevation_max_egm2008_m")

POPULATION_DENSITY_FIELD = "population_density_people_km2"


def default_planning_exposure_policy():
    """No planning exposure floor is assumed without an explicit, confirmed policy."""

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "not_configured",
        "status_reason": "planning_exposure_policy_not_configured",
        "enabled": False,
        "land_population_floor": None,
        "land_min_surface_elevation_m": None,
        "unit": "person/km2",
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
            "reuses_the_risk_v2_log1p_normalization": True,
        },
    }


def _optional_text(value):
    text = value if isinstance(value, str) else None
    text = text.strip() if text is not None else None
    return text or None


def normalize_planning_exposure_policy(value):
    """Validate one explicit planning-exposure policy.

    ``enabled`` 只有在 ``confirmed`` 为真、且 ``land_population_floor`` 与
    ``land_min_surface_elevation_m`` 都是有限数时才可能生效；缺任何一项都保持
    ``not_configured`` / ``pending_confirmation``，绝不猜阈值、猜下限。
    """

    source = value if isinstance(value, dict) else {}
    result = default_planning_exposure_policy()

    raw_floor = source.get("land_population_floor")
    floor = None
    if raw_floor not in (None, ""):
        floor = float(raw_floor)
        if not math.isfinite(floor) or floor < 0.0:
            raise ValueError("land_population_floor 必须是非负有限数（person/km2）")

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
    if floor is None:
        result["status"] = "not_configured"
        result["status_reason"] = "land_population_floor_not_configured"
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
    policy = policy if isinstance(policy, dict) else {}
    return stable_fingerprint({
        "status": policy.get("status"),
        "enabled": bool(policy.get("enabled")),
        "land_population_floor": policy.get("land_population_floor"),
        "land_min_surface_elevation_m": policy.get("land_min_surface_elevation_m"),
        "land_detection_method": policy.get("land_detection_method"),
        "source": policy.get("source"),
        "confirmed": bool(policy.get("confirmed")),
        "provenance": policy.get("provenance"),
    }, prefix="planningexpv1-")


def planning_exposure_is_active(policy):
    """A policy only takes effect when it is explicitly ``enabled`` **and** ``confirmed``."""

    return (
        isinstance(policy, dict)
        and policy.get("enabled") is True
        and policy.get("confirmed") is True
        and finite(policy.get("land_population_floor"))
        and finite(policy.get("land_min_surface_elevation_m"))
    )


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
    """Risk Framework V2 的 population 归一化参考值（唯一来源，绝不自己造）。"""

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


def empty_planning_exposure_attribute(policy=None, *, status="not_configured", reason=None):
    policy = normalize_planning_exposure_policy(
        policy if policy is not None else default_planning_exposure_policy()
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "attribute": "planning_exposure",
        "status": status,
        "reason": reason,
        "enabled": bool(policy.get("enabled")),
        "applied": False,
        "policy": policy,
        "policy_fingerprint": planning_exposure_policy_fingerprint(policy),
        "land_population_floor": policy.get("land_population_floor"),
        "land_min_surface_elevation_m": policy.get("land_min_surface_elevation_m"),
        "land_detection_method": LAND_DETECTION_METHOD,
        "used_population_nodata_as_sea_proxy": USED_POPULATION_NODATA_AS_SEA_PROXY,
        "population_reference": None,
        "floor_factor": None,
        "grid_level": None,
        "count": 0,
        "land_count": 0,
        "water_count": 0,
        "unresolved_land_status_count": 0,
        "floor_applied_count": 0,
        "cells": {},
        "provenance": {
            "definition": "domain/planning_exposure.py",
            "planning_only": True,
            "changes_population_report": False,
            "changes_population_shelter_definition": False,
            "reuses_the_risk_v2_log1p_normalization": True,
        },
        "field_fingerprint": stable_fingerprint(
            {"policy": planning_exposure_policy_fingerprint(policy), "status": status},
            prefix="planningexpfield-",
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
        attribute["grid_level"] = (grid or {}).get("level")
        return attribute

    reference = _population_reference_value(grid_risk_v2, population_reference)
    floor = float(policy["land_population_floor"])
    threshold = float(policy["land_min_surface_elevation_m"])
    floor_factor = log1p_quantile_index(floor, reference) if reference else None

    if floor_factor is None:
        # 没有人口归一化参考值就无法把"人口下限"换算成因子：fail-closed，本层不生效，
        # 也绝不退化成"直接给一个猜的因子"。
        attribute = empty_planning_exposure_attribute(
            policy, status="missing_data", reason="population_reference_unavailable",
        )
        attribute["count"] = len(grid_ids)
        attribute["grid_level"] = (grid or {}).get("level")
        return attribute

    factors = normalized_population_factors if isinstance(
        normalized_population_factors, dict
    ) else {}
    cells = {}
    land_count = water_count = unresolved_land = applied = 0
    for grid_id in grid_ids:
        elevation, elevation_field = _terrain_elevation(terrain_attribute, grid_id)
        if elevation is None:
            land_status, reason = "unresolved", "terrain_elevation_evidence_missing"
        elif elevation > threshold:
            land_status, reason = "land", None
        else:
            land_status, reason = "water", None
        if land_status == "land":
            land_count += 1
        elif land_status == "water":
            water_count += 1
        else:
            unresolved_land += 1

        population_cell = _cell_of(population_attribute, grid_id)
        real_density = population_cell.get(POPULATION_DENSITY_FIELD)
        real_density = float(real_density) if finite(real_density) else None
        base_factor = factors.get(grid_id)
        base_factor = float(base_factor) if finite(base_factor) else None

        effective_density = real_density
        density_source = "unchanged"
        planning_factor = base_factor
        updated = False
        cell_reason = reason
        if land_status == "land" and base_factor is not None:
            # max(density, floor) 的 log1p 归一化因子 == max(factor(density), factor(floor))。
            if floor_factor > base_factor:
                planning_factor = floor_factor
                updated = True
            if real_density is not None and real_density < floor:
                effective_density = floor
                density_source = "max(real_density, floor)"
                updated = True
            elif real_density is None:
                # 真实密度缺失时只报告**下限值**，绝不声称观测到该人口。
                effective_density = floor
                density_source = "floor_lower_bound_real_density_unknown"
                updated = True
        if base_factor is None:
            cell_reason = cell_reason or "population_factor_unresolved"
            planning_factor = None
        if updated:
            applied += 1

        cells[grid_id] = {
            "grid_id": grid_id,
            "status": "passed" if planning_factor is not None else "unresolved",
            "land_status": land_status,
            "surface_elevation_max_m": None if elevation is None else round(elevation, 9),
            "surface_elevation_field": elevation_field,
            "real_population_density_people_km2": (
                None if real_density is None else round(real_density, 9)
            ),
            "effective_population_density_people_km2": (
                None if effective_density is None else round(effective_density, 9)
            ),
            "effective_population_density_source": density_source,
            "population_factor": None if base_factor is None else round(base_factor, 9),
            "planning_population_factor": (
                None if planning_factor is None else round(planning_factor, 9)
            ),
            "floor_applied": updated,
            "reason": cell_reason,
        }

    statuses = {cell["status"] for cell in cells.values()} or {"not_calculated"}
    if statuses == {"passed"}:
        status = "passed"
    elif statuses == {"unresolved"}:
        status = "missing_data"
    else:
        status = "partial"

    return {
        "schema_version": SCHEMA_VERSION,
        "attribute": "planning_exposure",
        "status": status,
        "reason": None,
        "enabled": True,
        "applied": True,
        "policy": policy,
        "policy_fingerprint": planning_exposure_policy_fingerprint(policy),
        "land_population_floor": floor,
        "land_min_surface_elevation_m": threshold,
        "land_detection_method": LAND_DETECTION_METHOD,
        "used_population_nodata_as_sea_proxy": USED_POPULATION_NODATA_AS_SEA_PROXY,
        "population_reference": round(float(reference), 9),
        "floor_factor": round(float(floor_factor), 9),
        "grid_level": (grid or {}).get("level"),
        "count": len(grid_ids),
        "land_count": land_count,
        "water_count": water_count,
        "unresolved_land_status_count": unresolved_land,
        "floor_applied_count": applied,
        "cells": cells,
        "provenance": {
            "definition": "domain/planning_exposure.py",
            "planning_only": True,
            "changes_population_report": False,
            "changes_population_nodata_semantics": False,
            "changes_population_shelter_definition": False,
            "risk_index_definition_unchanged": "normalized_population_factor × shelter_coefficient",
            "land_detection": LAND_DETECTION_METHOD,
            "land_detection_input": "per_grid_surface_elevation_max",
            "used_population_nodata_as_sea_proxy": USED_POPULATION_NODATA_AS_SEA_PROXY,
            "reuses_the_risk_v2_log1p_normalization": True,
            "equivalence": "max(density, floor) normalizes to max(factor(density), factor(floor))",
            "missing_terrain_evidence": "unresolved_never_sea",
        },
        "field_fingerprint": stable_fingerprint({
            "policy": planning_exposure_policy_fingerprint(policy),
            "reference": reference,
            "threshold": threshold,
            "floor": floor,
            "land_count": land_count,
            "water_count": water_count,
            "unresolved_land_status_count": unresolved_land,
            "floor_applied_count": applied,
        }, prefix="planningexpfield-"),
    }


def planning_exposure_factors(attribute):
    """The per-grid population factors the planner should use, or ``None`` when inactive."""

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
        "land_population_floor": item.get("land_population_floor"),
        "land_min_surface_elevation_m": item.get("land_min_surface_elevation_m"),
        "floor_applied_count": item.get("floor_applied_count"),
    }, prefix="planningexpattr-")


__all__ = [
    "LAND_DETECTION_METHOD", "LAND_STATUSES", "PENDING_SOURCE", "SCHEMA_VERSION",
    "TERRAIN_ELEVATION_FIELDS", "USED_POPULATION_NODATA_AS_SEA_PROXY",
    "default_planning_exposure_policy", "empty_planning_exposure_attribute",
    "normalize_planning_exposure_policy", "planning_exposure_factors",
    "planning_exposure_fingerprint", "planning_exposure_is_active",
    "planning_exposure_policy_fingerprint", "resolve_planning_exposure",
]
