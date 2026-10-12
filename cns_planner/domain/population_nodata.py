"""显式、可审计的「来源 NoData 表示零人口」工程确认。

背景
----
真实人口产品（例如 WorldPop Population Counts R2025A）在**无人定居的海域与无人区**
使用 NoData 像元。项目默认原则是 ``missing_data != zero``，因此这些像元在
``grid_attributes.population`` 里保持 ``missing_data``；而 Layered Risk-Aware Theta* V2 的
population × shelter 风险对任一未解析格子 fail-closed。两者叠加的结果是：**任何跨海航线
都无法规划**（舟山 workspace 实测 1900 格中 1395 格为海上 NoData）。

本模块只提供**显式工程确认**的契约与规范化。只有当项目显式确认"该来源的 NoData 表示零
人口（无居住人口），而不是未知"时，population 映射才把**完全落在来源范围内、且全部像元
均为 NoData** 的格子记录为**已知的 0**，并保留完整 provenance 与 quality flag。

明确的边界
----------
* 只适用于来源 ``extent`` **之内**的 NoData；``outside_extent`` 永远是 unknown，绝不转换；
* 只适用于 population 这一个来源与角色，不适用于任何其他因子；
* **未确认时行为与既有实现完全一致**（``missing_data``，绝不补 0）；
* 这不是"人口为 0 即安全"的判断，不改变任何算法、阈值、验证或采用语义；
* 该确认参与 population 属性指纹与下游失效，因此可被审计、可被撤回。
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

SCHEMA_VERSION = "population-nodata-semantics-v2"
POLICY_KEY = "population_nodata_policy"

#: 模式一（既有，语义不变）：来源 extent 之内、全部像元均为 NoData 的格 ⇒ 已知 0。
MODE_NODATA_IS_ZERO_POPULATION = "nodata_is_zero_population"

#: 模式二（Round35-D 新增）：**开阔海域且无陆域证据**的 NoData 格 ⇒ 工程假设的零人口。
#:
#: 它比模式一严格得多：不是"到处都判 0"，而是要求**逐格的陆域缺席证据**。与陆域
#: 多边形相交、或落在海岸不确定带内的 NoData 格**仍然**保持 ``missing_data``。
MODE_OPEN_SEA_NO_SETTLEMENT_ZERO = "open_sea_no_settlement_zero_population"

#: 唯一被支持的确认模式集合。任何其他取值都视为未确认。
CONFIRMED_MODES = (MODE_NODATA_IS_ZERO_POPULATION, MODE_OPEN_SEA_NO_SETTLEMENT_ZERO)

#: 新模式逐格必须同时满足的适用条件（全部可审计，缺一即拒绝）。
OPEN_SEA_ELIGIBILITY_CONDITIONS = (
    "cell_wholly_inside_population_raster_footprint",
    "canonical_land_mask_available",
    "cell_does_not_intersect_any_land_polygon",
    "cell_outside_coastal_uncertainty_buffer",
    "no_valid_population_observation_in_cell",
    "evidence_is_an_engineering_assumption_not_an_observation",
)

#: 转换后使用的 coverage_status。它**不在** Risk Framework V2 的不可用集合
#: （``nodata_only`` / ``outside_extent``）内，因为该格子的人口暴露已被显式解释为 0。
CONFIRMED_ZERO_COVERAGE_STATUS = "nodata_confirmed_zero_population"

#: 永远不转换的 coverage_status。
NEVER_CONVERTED_COVERAGE_STATUSES = ("outside_extent",)

STATEMENT = (
    "该项目显式确认：该人口来源在其自身 extent 之内使用 NoData 像元表示"
    "「无居住人口（零人口）」，而不是「未知」。因此范围内、全部像元均为 NoData 的"
    "网格被记录为已知的 0 人口暴露，并保留 nodata 语义 provenance。"
)

OPEN_SEA_STATEMENT = (
    "该项目显式确认：**整个格子位于人口栅格有效覆盖范围内、与 canonical 陆域掩膜中的"
    "任何陆域多边形都不相交、且不在海岸不确定带保护范围内**时，该格缺少有效人口观测"
    "被解释为「开阔海域无居住人口」的**工程假设零人口**。这是规划假设，不是原始观测；"
    "与陆域相交或落在海岸不确定带内的缺失人口格**永不**被解释为零。"
)

SEMANTICS = {
    "confirmed_zero_is_not_an_observation": True,
    "applies_only_inside_source_extent": True,
    "outside_extent_stays_unknown": True,
    "never_fills_partial_nodata_pixels": True,
    "zero_population_is_not_a_safety_verdict": True,
    "does_not_change_any_algorithm_threshold_or_validation": True,
    "unconfirmed_behaviour_is_unchanged_missing_data": True,
    # Round35-D：
    "open_sea_zero_requires_land_absence_evidence": True,
    "land_missing_population_never_becomes_zero": True,
    "estimated_values_must_be_distinguishable": True,
}


def default_population_nodata_policy():
    """未确认的默认状态：绝不补 0。"""

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "not_configured",
        "status_reason": "no_confirmed_population_nodata_semantics",
        "mode": None,
        "role": "population",
        "source_id": None,
        "statement": None,
        "source": None,
        "evidence": None,
        "confirmed": False,
        "confirmed_at": None,
        "semantics": deepcopy(SEMANTICS),
        "never_converts": list(NEVER_CONVERTED_COVERAGE_STATUSES),
        "confirmed_modes": list(CONFIRMED_MODES),
        "open_sea_eligibility_conditions": list(OPEN_SEA_ELIGIBILITY_CONDITIONS),
        "notes": [
            "未确认时，来源范围内的 NoData 保持 missing_data：missing_data != zero。",
            "模式一确认后只影响来源 extent 之内、全部像元均为 NoData 的格子。",
            "模式二（开阔海域）额外要求逐格的陆域缺席证据；与陆域相交的缺失人口格绝不判 0。",
            "该确认不改变任何算法、阈值、预测/验证或采用语义。",
        ],
    }


def normalize_population_nodata_policy(value):
    """Normalize a stored policy without ever inventing a confirmation."""

    base = default_population_nodata_policy()
    if not isinstance(value, dict):
        return base
    mode = value.get("mode")
    confirmed = value.get("confirmed") is True
    # A confirmation is only honoured when it carries the explicit mode *and*
    # traceable source/evidence; anything else is reported as not confirmed.
    valid = (
        confirmed
        and mode in CONFIRMED_MODES
        and bool(str(value.get("source") or "").strip())
        and isinstance(value.get("evidence"), dict)
        and bool(value["evidence"])
    )
    if not valid:
        base.update({
            "status": "not_configured",
            "status_reason": (
                "confirmation_requires_explicit_mode_source_and_evidence"
                if confirmed or mode else "no_confirmed_population_nodata_semantics"
            ),
            "mode": mode if mode in CONFIRMED_MODES else None,
            "source_id": value.get("source_id"),
            "source": value.get("source"),
            "evidence": value.get("evidence") if isinstance(value.get("evidence"), dict) else None,
        })
        return base
    base.update({
        "status": "confirmed",
        "status_reason": None,
        "mode": mode,
        "source_id": value.get("source_id"),
        "statement": value.get("statement") or (
            OPEN_SEA_STATEMENT if mode == MODE_OPEN_SEA_NO_SETTLEMENT_ZERO else STATEMENT
        ),
        "source": value.get("source"),
        "evidence": deepcopy(value["evidence"]),
        "confirmed": True,
        "confirmed_at": value.get("confirmed_at"),
    })
    return base


def policy_mode(policy):
    """确认后的模式；未确认或非法模式返回 ``None``（绝不猜）。"""

    if not policy_is_confirmed(policy):
        return None
    mode = policy.get("mode") if isinstance(policy, dict) else None
    return mode if mode in CONFIRMED_MODES else None


def open_sea_evidence_required(policy):
    """该确认是否要求**逐格陆域缺席证据**（模式二）。"""

    return policy_mode(policy) == MODE_OPEN_SEA_NO_SETTLEMENT_ZERO


def open_sea_zero_permitted(policy, evidence_item):
    """模式二下逐格裁定：``(permitted, reason)``。

    ``evidence_item`` 由 gis/application 层按 :data:`OPEN_SEA_ELIGIBILITY_CONDITIONS`
    逐格探测得到（来自 **canonical 陆域掩膜**，不是格心分类的投影）。任一条件不满足
    即拒绝 —— 与陆域相交、或落在海岸不确定带内的 NoData 格因此**永远不会**变成 0。
    """

    if policy_mode(policy) != MODE_OPEN_SEA_NO_SETTLEMENT_ZERO:
        return False, "policy_mode_is_not_open_sea"
    item = evidence_item if isinstance(evidence_item, dict) else {}
    relation = str(item.get("relation") or "").strip()
    if relation != "open_sea":
        return False, relation or "missing_open_sea_evidence"
    for key in (
        "land_mask_available",
        "inside_population_footprint",
        "does_not_intersect_land",
        "outside_coastal_uncertainty_buffer",
        "no_valid_population_observation",
    ):
        if item.get(key) is not True:
            return False, f"open_sea_condition_failed:{key}"
    return True, "open_sea_no_settlement_zero"


def policy_is_confirmed(policy):
    return (
        isinstance(policy, dict)
        and policy.get("confirmed") is True
        and policy.get("status") == "confirmed"
        and policy.get("mode") in CONFIRMED_MODES
    )


def policy_fingerprint(policy):
    """Stable fingerprint of the *effective* confirmation (identity only)."""

    item = policy if isinstance(policy, dict) else {}
    payload = {
        "schema_version": SCHEMA_VERSION,
        "status": item.get("status"),
        "mode": item.get("mode"),
        "role": item.get("role"),
        "source_id": item.get("source_id"),
        "source": item.get("source"),
        "confirmed": bool(item.get("confirmed")),
        "evidence": item.get("evidence"),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "populationnodatav2-" + hashlib.sha256(raw).hexdigest()


def nodata_semantics_record(policy):
    """The provenance record embedded into a converted cell / attribute."""

    mode = policy_mode(policy)
    if mode is None:
        return None
    open_sea = mode == MODE_OPEN_SEA_NO_SETTLEMENT_ZERO
    return {
        "applied": True,
        "mode": mode,
        "coverage_status": CONFIRMED_ZERO_COVERAGE_STATUS,
        "policy_fingerprint": policy_fingerprint(policy),
        "source_id": policy.get("source_id"),
        "source": policy.get("source"),
        "statement": policy.get("statement") or (OPEN_SEA_STATEMENT if open_sea else STATEMENT),
        "evidence": deepcopy(policy.get("evidence")),
        "semantics": deepcopy(SEMANTICS),
        "is_engineering_assumption_not_observation": True if open_sea else False,
        "quality_flags": (
            ["open_sea_no_settlement_engineering_assumption", "confirmed_zero_not_an_observation"]
            if open_sea else
            ["nodata_interpreted_as_zero_population", "confirmed_zero_not_an_observation"]
        ),
    }


def confirmed_zero_allocation(
    target_area_m2, policy, *, nodata_pixel_count=None, extra_quality_flags=None, trigger=None,
):
    """Canonical allocation record for a cell whose population exposure is a **known zero**.

    Returns ``None`` unless the policy is explicitly confirmed, so an unconfirmed project can
    never reach this path.
    """

    record = nodata_semantics_record(policy)
    if record is None:
        return None
    from .population_density import (
        DENSITY_SEMANTICS_CONFIRMED_ZERO, DENSITY_SEMANTICS_VERSION,
        density_semantics_record,
    )

    area = float(target_area_m2 or 0.0)
    result = {
        "status": "passed",
        "value_status": "passed",
        "coverage_status": CONFIRMED_ZERO_COVERAGE_STATUS,
        "population_count_people": 0.0,
        "population_density_people_km2": 0.0,
        # 已确认零人口：整格与"有效覆盖区"两个分母上都是 0，因此两个密度字段同为 0，
        # 且都不冒充观测（见 ``interpretation``）。
        "population_density_observed_area_people_km2": 0.0,
        "density_support_area_m2": area,
        "observed_density_support_area_m2": area,
        "density_semantics": DENSITY_SEMANTICS_CONFIRMED_ZERO,
        "density_semantics_version": DENSITY_SEMANTICS_VERSION,
        "observed_density_semantics": DENSITY_SEMANTICS_CONFIRMED_ZERO,
        "observed_density_confidence": "not_applicable_confirmed_zero",
        "density_semantics_record": density_semantics_record(
            grid_area_m2=area, coverage_status=CONFIRMED_ZERO_COVERAGE_STATUS,
            coverage_fraction=1.0, observed_area_m2=area,
        ),
        "target_area_m2": area,
        "valid_covered_area_m2": area,
        "source_coverage_fraction": 1.0,
        "source_pixel_count": 0,
        "interpretation": (
            "open_sea_no_settlement_engineering_assumption"
            if record.get("mode") == MODE_OPEN_SEA_NO_SETTLEMENT_ZERO
            else "confirmed_zero_population_not_an_observation"
        ),
        "quality_flags": list(record["quality_flags"]) + list(extra_quality_flags or []),
        "nodata_semantics": {**record, "trigger": trigger or "in_footprint_nodata"},
    }
    if nodata_pixel_count is not None:
        result["nodata_pixel_count"] = int(nodata_pixel_count)
    return result


__all__ = [
    "CONFIRMED_MODES", "CONFIRMED_ZERO_COVERAGE_STATUS", "MODE_NODATA_IS_ZERO_POPULATION",
    "MODE_OPEN_SEA_NO_SETTLEMENT_ZERO", "NEVER_CONVERTED_COVERAGE_STATUSES",
    "OPEN_SEA_ELIGIBILITY_CONDITIONS", "OPEN_SEA_STATEMENT", "POLICY_KEY", "SCHEMA_VERSION",
    "SEMANTICS", "STATEMENT", "confirmed_zero_allocation",
    "default_population_nodata_policy", "nodata_semantics_record",
    "normalize_population_nodata_policy", "open_sea_evidence_required",
    "open_sea_zero_permitted", "policy_fingerprint", "policy_is_confirmed", "policy_mode",
]
