"""BUG-ROUTE-005（收口后）：规划用暴露度层 = 陆地统一相对风险基线。

语义（**只**改这一层，Theta* 核心搜索、0.8/0.1/0.1 权重、Risk Framework V2 canonical
population、WorldPop、Population NoData、shelter 数学全部不动）：

    p = canonical Risk Framework V2 归一化人口因子（[0, 1]，唯一来源）
    L = 1 if land else 0 if sea（**canonical surface facts** 判定）
    b = land_relative_risk_baseline（0 <= b < 1，显式 confirmed 才生效）

    planning_population_factor = (1 - b) * p + b * L

因此：

* 同一 ``p`` 下 land − water 差值恒等于 ``b``；
* 所有人口相对差异统一保留 ``(1 - b)``，**不是**只抬低人口区域；
* 输出天然落在 ``[0, 1]``（凸组合），**不使用 clip**，因此不会让高风险陆地饱和；
* land 内部排序、water 内部排序保持不变（同一格集内是同一个仿射映射）；
* ``risk_index = planning_population_factor × shelter_coefficient``（定义未改）。

陆海分类来源（surface source 收口）：

1. **首选**：项目已有 canonical surface facts / LandMask provider，逐格给出
   ``land | sea | coastal_uncertain | unknown``：

   * ``land`` ⇒ ``L = 1``；
   * ``sea`` ⇒ ``L = 0``；
   * ``coastal_uncertain`` ⇒ **复用项目 canonical 的
     ``effective_requirement_class = land``**（``L = 1``，与 Radar / CNS surface policy
     同一语义），disposition 显式记录为
     ``project_canonical_effective_requirement_class_land``，逐格 ``surface_class`` 仍保留
     canonical 原文；本层**不自行猜** land / sea；
   * ``unknown`` ⇒ **unresolved**（绝不猜成海或陆）；

   provenance 记为 ``surface_class_source = canonical_land_mask``。
2. **legacy 兼容回退**：只有项目**没有** canonical surface facts / LandMask provider 时，
   才允许继续使用 ``surface_elevation_max_m > land_min_surface_elevation_m`` 的
   terrain threshold 判定；provenance 记为
   ``surface_class_source = legacy_terrain_threshold_fallback``。

同一 candidate 上 LandMask 与 terrain threshold 曾出现 5 cells / 494.68 m 的分类分歧，
正是因为后者是**独立**的地形阈值口径；收口后它不再是首选分类。

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
* **绝不使用 "population NoData ⇒ 海面" 这类推断**：分类只来自 canonical surface facts /
  LandMask provider（或项目没有它们时的 legacy terrain threshold）。
* 分类不可判定的格保持 ``unresolved`` 并 **fail-closed**（``planning_population_factor = None``）：
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

#: **legacy 兼容回退**使用的方法：已确认的"地表最大高程阈值"。只在项目没有 canonical
#: surface facts / LandMask provider 时使用，并原样记录在 provenance 里。
LAND_DETECTION_METHOD = "confirmed_surface_elevation_max_threshold"

#: canonical 陆海分类来源（项目已有 surface facts / LandMask provider）。
SURFACE_CLASS_SOURCE_CANONICAL = "canonical_land_mask"

#: legacy 兼容回退来源（只有没有 canonical provider 时才允许）。
SURFACE_CLASS_SOURCE_LEGACY_TERRAIN = "legacy_terrain_threshold_fallback"

#: ``coastal_uncertain`` 的处置：**复用项目 canonical 的 ``effective_requirement_class``**
#: （海岸不确定带按 land policy 处理，与 Radar / CNS surface policy 同一语义），
#: 而不是本层自行猜测 land / sea。逐格 ``surface_class`` 仍保留 canonical 原文。
COASTAL_UNCERTAIN_DISPOSITION = (
    "project_canonical_effective_requirement_class_land"
)

#: canonical surface class 取值（与 ``domain/surface_classification`` 及 ``LandMaskSource``
#: 同一套语义，绝不新增第五类）。
CANONICAL_SURFACE_CLASSES = ("land", "sea", "coastal_uncertain", "unknown")

#: 两种来源的语义说明（进入 attribute / provenance，便于审计）。
SURFACE_CLASS_SOURCE_SEMANTICS = {
    SURFACE_CLASS_SOURCE_CANONICAL: (
        "land_sea_from_canonical_surface_facts_land_mask_provider"
    ),
    SURFACE_CLASS_SOURCE_LEGACY_TERRAIN: (
        "legacy_compatibility_fallback_surface_elevation_threshold_only_without_canonical_provider"
    ),
}

#: 本层绝不把 population NoData 当作海面代理。
USED_POPULATION_NODATA_AS_SEA_PROXY = False

#: 正式规划公式（唯一一种写法，记录在 policy / attribute / provenance）。
PLANNING_FACTOR_FORMULA = "planning_population_factor = (1-b)*p + b*L"

#: ``b`` 的合法范围：``0 <= b < 1``（``b = 1`` 会把所有相对差异抹平并被拒绝）。
LAND_RELATIVE_RISK_BASELINE_RANGE = (0.0, 1.0)

#: 首轮工程建议值。**不是默认值**：它只作为 UI 的可选预填/占位，必须显式确认才生效。
ENGINEERING_SUGGESTED_LAND_RELATIVE_RISK_BASELINE = 0.08

LAND_STATUSES = ("land", "water", "sea", "unresolved")

#: 逐格地表最大高程的字段候选（现有 terrain 映射 / layered feasibility 事实）。
#: **只**在 legacy 兼容回退里被消费（canonical 路径下仅作信息性记录）。
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
        #: 分类来源的**首选**：项目已有 canonical surface facts / LandMask provider。
        #: 只有它不可用时才回退到上面的 terrain threshold（legacy 兼容）。
        "surface_class_source_preference": SURFACE_CLASS_SOURCE_CANONICAL,
        "surface_class_source_fallback": SURFACE_CLASS_SOURCE_LEGACY_TERRAIN,
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
            "land_sea_from_canonical_surface_facts_first": True,
            "terrain_threshold_is_legacy_fallback_only": True,
            "land_detection_uses_terrain_evidence_only_when_no_canonical_surface_facts": True,
            "coastal_uncertain_uses_project_canonical_effective_requirement_class": True,
            "coastal_uncertain_disposition": COASTAL_UNCERTAIN_DISPOSITION,
            "coastal_uncertain_is_never_guessed_as_sea": True,
            "unknown_surface_is_unresolved": True,
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


def _cell_center(cell):
    """网格格心（canonical surface facts / LandMask provider 的查询点）。

    优先显式 ``coordinate``，其次 ``center``，最后 ``bbox`` 中心；都没有则 ``None``
    （fail-closed：不可判定的格保持 unresolved，绝不猜）。
    """

    if not isinstance(cell, dict):
        return None
    for field in ("coordinate", "center"):
        value = cell.get(field)
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            return [float(value[0]), float(value[1])]
    bbox = cell.get("bbox")
    if isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
        return [
            (float(bbox[0]) + float(bbox[2])) / 2.0,
            (float(bbox[1]) + float(bbox[3])) / 2.0,
        ]
    return None


def normalize_canonical_surface_class(value):
    """把 canonical provider / facts 的返回值归一到本项目的四个 surface 类别。"""

    text = str(value or "").strip().lower()
    if text in ("land",):
        return "land"
    if text in ("sea", "water"):
        return "sea"
    if text == "coastal_uncertain":
        return "coastal_uncertain"
    return "unknown"


def _provider_member(provider, name):
    if isinstance(provider, dict):
        return provider.get(name)
    return getattr(provider, name, None)


def _canonical_surface_classes(provider, grid_ids, grid_cells_by_id):
    """canonical provider 的逐格 surface class（不可判定一律 ``unknown``，fail-closed）。

    provider 的既有接口按优先级复用：``classify_many``（``LandMaskSource``）→
    ``classify_surface``（``SurfaceFactsProvider`` / 组合根闭包）→ 逐点 ``classify``。
    **不猜测**：provider 没有可用接口或缺坐标时该格就是 ``unknown``。
    """

    if provider is None:
        return None
    points = [_cell_center(grid_cells_by_id.get(grid_id)) for grid_id in grid_ids]
    batch = _provider_member(provider, "classify_many") or _provider_member(
        provider, "classify_surface"
    )
    if callable(batch) and all(point is not None for point in points):
        values = batch([(point[0], point[1]) for point in points]) or []
        classes = [normalize_canonical_surface_class(value) for value in values]
        if len(classes) != len(points):
            classes = (classes + ["unknown"] * len(points))[: len(points)]
        return dict(zip(grid_ids, classes))
    single = _provider_member(provider, "classify")
    if callable(single):
        classes = []
        for point in points:
            if point is None:
                classes.append("unknown")
                continue
            result = single(point[0], point[1])
            if isinstance(result, (tuple, list)) and result:
                result = result[0]
            classes.append(normalize_canonical_surface_class(result))
        return dict(zip(grid_ids, classes))
    return {grid_id: "unknown" for grid_id in grid_ids}


def _canonical_land_status(surface_class):
    """canonical surface class → ``(land_status, land_indicator, surface_reason)``。

    * ``land`` ⇒ ``("land", 1.0, None)``
    * ``sea`` ⇒ ``("sea", 0.0, None)``
    * ``coastal_uncertain`` ⇒ **复用项目 canonical 的
      ``effective_requirement_class = land``**（``("land", 1.0, ...)``）：海岸不确定带按陆域
      policy 处理，与 Radar / CNS surface policy 同一语义
      （``cns_planner.domain.cns_service_contract`` / ``radar_surveillance_layout`` 的
      ``coastal_uncertain → land``）。它**不是**本层自己猜的 land：逐格 ``surface_class``
      仍保留 canonical 原文 ``coastal_uncertain``，disposition 显式记录在
      ``surface_reason`` 与 provenance 上；
    * ``unknown`` ⇒ ``("unresolved", None, "surface_class_unknown_unresolved")``
      —— 不可判定即 unresolved，绝不猜成海或陆。
    """

    surface = normalize_canonical_surface_class(surface_class)
    if surface == "land":
        return "land", LAND_INDICATOR_LAND, None
    if surface == "sea":
        return "sea", LAND_INDICATOR_WATER, None
    if surface == "coastal_uncertain":
        return "land", LAND_INDICATOR_LAND, COASTAL_UNCERTAIN_DISPOSITION
    return "unresolved", None, "surface_class_unknown_unresolved"


def _legacy_land_status(elevation, threshold):
    """**legacy 兼容回退**的 terrain threshold 判定（与历史行为逐位一致）。"""

    if elevation is None:
        return "unresolved", None, "terrain_elevation_evidence_missing"
    if elevation > threshold:
        return "land", LAND_INDICATOR_LAND, None
    return "water", LAND_INDICATOR_WATER, None


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
        "surface_class_source_preference": (
            policy.get("surface_class_source_preference") or SURFACE_CLASS_SOURCE_CANONICAL
        ),
        "surface_class_source_fallback": (
            policy.get("surface_class_source_fallback") or SURFACE_CLASS_SOURCE_LEGACY_TERRAIN
        ),
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
        # 未生效时没有分类事实，因此来源是 unknown；生效后由 resolve 明确写入。
        "surface_class_source": None,
        "surface_class_source_semantics": None,
        "legacy_terrain_threshold_fallback_used": None,
        "canonical_surface_class_counts": {name: 0 for name in CANONICAL_SURFACE_CLASSES},
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
    surface_class_by_grid_id=None, surface_class_provider=None,
):
    """Derive the planning-only exposure layer and a confirmed policy.

    **surface source 收口**：陆海分类**优先**消费项目已有 canonical surface facts /
    LandMask provider：

    * ``surface_class_by_grid_id``：canonical facts 的**逐格**分类
      （``{grid_id: land|sea|coastal_uncertain|unknown}``）；
    * ``surface_class_provider``：可用的 canonical provider（``LandMaskSource`` /
      ``SurfaceFactsProvider`` / 组合根闭包），本模块用**格心代表点**批量判定；

    分类语义：``land`` ⇒ ``L=1``；``sea`` ⇒ ``L=0``；``coastal_uncertain`` ⇒ 复用项目
    canonical 的 ``effective_requirement_class = land``（``L=1``，disposition 显式记录，逐格
    ``surface_class`` 保留原文）；``unknown`` ⇒ unresolved（绝不猜）。

    两者都不可用时才回退到 **legacy terrain threshold**
    （``surface_elevation_max_m > land_min_surface_elevation_m``）。来源始终记录在
    ``surface_class_source`` 上（``canonical_land_mask`` /
    ``legacy_terrain_threshold_fallback``）。

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

    grid_cells_by_id = {
        str(cell.get("grid_id")): cell
        for cell in ((grid or {}).get("cells") or [])
        if isinstance(cell, dict) and cell.get("grid_id")
    }
    # ---- surface source：canonical 优先，legacy terrain threshold 只在没有 canonical 时 ----
    if isinstance(surface_class_by_grid_id, dict):
        canonical_classes = {
            grid_id: normalize_canonical_surface_class(
                surface_class_by_grid_id.get(grid_id)
            )
            for grid_id in grid_ids
        }
    else:
        canonical_classes = _canonical_surface_classes(
            surface_class_provider, grid_ids, grid_cells_by_id
        )
    surface_class_source = (
        SURFACE_CLASS_SOURCE_LEGACY_TERRAIN if canonical_classes is None
        else SURFACE_CLASS_SOURCE_CANONICAL
    )
    legacy_fallback = canonical_classes is None

    factors = normalized_population_factors if isinstance(
        normalized_population_factors, dict
    ) else {}
    cells = {}
    land_count = water_count = unresolved_land = 0
    applied = raised = unresolved_factor = 0
    canonical_counts = {name: 0 for name in CANONICAL_SURFACE_CLASSES}
    for grid_id in grid_ids:
        elevation, elevation_field = _terrain_elevation(terrain_attribute, grid_id)
        if legacy_fallback:
            land_status, land_indicator, surface_reason = _legacy_land_status(
                elevation, threshold
            )
            terrain_reason = surface_reason
            surface_class = None
        else:
            surface_class = canonical_classes.get(grid_id, "unknown")
            land_status, land_indicator, surface_reason = _canonical_land_status(
                surface_class
            )
            # legacy 字段在 canonical 路径下只作信息性记录（不再参与分类）。
            terrain_reason = None
            canonical_counts[surface_class] = canonical_counts.get(surface_class, 0) + 1
        if land_status == "land":
            land_count += 1
        elif land_status in ("water", "sea"):
            water_count += 1
        else:
            unresolved_land += 1

        population_cell = _cell_of(population_attribute, grid_id)
        real_density = population_cell.get(POPULATION_DENSITY_FIELD)
        real_density = float(real_density) if finite(real_density) else None
        population_factor = factors.get(grid_id)
        population_factor = float(population_factor) if finite(population_factor) else None

        # fail-closed：缺分类证据或缺 canonical 因子时 planning factor 一律 None，
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
            reason = surface_reason or "surface_class_unresolved"
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
            # canonical 路径记录原文 surface class；legacy 路径没有 canonical 分类。
            "surface_class": surface_class,
            "surface_class_source": surface_class_source,
            "surface_reason": surface_reason,
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
        # ---- surface source provenance（输入事实语义） ----
        "surface_class_source": surface_class_source,
        "surface_class_source_semantics": SURFACE_CLASS_SOURCE_SEMANTICS[
            surface_class_source
        ],
        "legacy_terrain_threshold_fallback_used": legacy_fallback,
        "land_min_surface_elevation_m_used_for_classification": legacy_fallback,
        "canonical_surface_class_counts": dict(canonical_counts),
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
        # ---- surface source 收口 ----
        "surface_class_source": surface_class_source,
        "surface_class_source_semantics": SURFACE_CLASS_SOURCE_SEMANTICS[
            surface_class_source
        ],
        "surface_class_source_priority": (
            "canonical_surface_facts_or_land_mask_first_terrain_threshold_is_legacy_fallback_only"
        ),
        "legacy_terrain_threshold_fallback_used": legacy_fallback,
        "canonical_surface_class_counts": dict(canonical_counts),
        "coastal_uncertain_disposition": COASTAL_UNCERTAIN_DISPOSITION,
        "coastal_uncertain_is_never_guessed_as_sea": True,
        "unknown_surface_is_unresolved": True,
        "land_status_vocabulary": (
            "legacy_fallback_uses_land_water_canonical_uses_land_sea_unresolved"
        ),
        "land_detection": LAND_DETECTION_METHOD,
        "land_detection_input": "per_grid_surface_elevation_max",
        "land_detection_used_for_classification": legacy_fallback,
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
        # 输入事实语义变化（分类来源/canonical 计数）必须改变指纹：旧 candidate 不得沿用。
        "surface_class_source": surface_class_source,
        "canonical_surface_class_counts": dict(canonical_counts),
        "legacy_terrain_threshold_fallback_used": legacy_fallback,
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
        # 输入事实语义：surface 分类来源与 canonical 四类计数。收口后它必然改变，因此
        # 依赖它的 candidate 会 stale —— 绝不静默沿用旧的陆海分类口径。
        "surface_class_source": item.get("surface_class_source"),
        "legacy_terrain_threshold_fallback_used": item.get(
            "legacy_terrain_threshold_fallback_used"
        ),
        "canonical_surface_class_counts": item.get("canonical_surface_class_counts"),
        "applied_count": item.get("applied_count"),
        "land_count": item.get("land_count"),
        "water_count": item.get("water_count"),
        "land_baseline_raised_count": item.get("land_baseline_raised_count"),
    }, prefix="planningexpattrv2-")


__all__ = [
    "CANONICAL_SURFACE_CLASSES", "COASTAL_UNCERTAIN_DISPOSITION",
    "ENGINEERING_SUGGESTED_LAND_RELATIVE_RISK_BASELINE", "LAND_DETECTION_METHOD",
    "LAND_INDICATOR_LAND", "LAND_INDICATOR_WATER", "LAND_RELATIVE_RISK_BASELINE_RANGE",
    "LAND_STATUSES", "PENDING_SOURCE", "PLANNING_EXPOSURE_POPULATION_FACTOR_SOURCE",
    "PLANNING_FACTOR_FORMULA", "POPULATION_DENSITY_FIELD", "SCHEMA_VERSION",
    "SURFACE_CLASS_SOURCE_CANONICAL", "SURFACE_CLASS_SOURCE_LEGACY_TERRAIN",
    "SURFACE_CLASS_SOURCE_SEMANTICS",
    "TERRAIN_ELEVATION_FIELDS", "USED_POPULATION_NODATA_AS_SEA_PROXY",
    "default_planning_exposure_policy", "empty_planning_exposure_attribute",
    "normalize_canonical_surface_class", "normalize_planning_exposure_policy",
    "planning_exposure_factors",
    "planning_exposure_fingerprint", "planning_exposure_is_active",
    "planning_exposure_policy_fingerprint", "resolve_planning_exposure",
]
