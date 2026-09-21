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

SCHEMA_VERSION = "population-nodata-semantics-v1"
POLICY_KEY = "population_nodata_policy"

#: 唯一被支持的确认模式。任何其他取值都视为未确认。
MODE_NODATA_IS_ZERO_POPULATION = "nodata_is_zero_population"

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

SEMANTICS = {
    "confirmed_zero_is_not_an_observation": True,
    "applies_only_inside_source_extent": True,
    "outside_extent_stays_unknown": True,
    "never_fills_partial_nodata_pixels": True,
    "zero_population_is_not_a_safety_verdict": True,
    "does_not_change_any_algorithm_threshold_or_validation": True,
    "unconfirmed_behaviour_is_unchanged_missing_data": True,
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
        "notes": [
            "未确认时，来源范围内的 NoData 保持 missing_data：missing_data != zero。",
            "确认后只影响来源 extent 之内、全部像元均为 NoData 的格子。",
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
        and mode == MODE_NODATA_IS_ZERO_POPULATION
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
            "mode": mode if mode == MODE_NODATA_IS_ZERO_POPULATION else None,
            "source_id": value.get("source_id"),
            "source": value.get("source"),
            "evidence": value.get("evidence") if isinstance(value.get("evidence"), dict) else None,
        })
        return base
    base.update({
        "status": "confirmed",
        "status_reason": None,
        "mode": MODE_NODATA_IS_ZERO_POPULATION,
        "source_id": value.get("source_id"),
        "statement": value.get("statement") or STATEMENT,
        "source": value.get("source"),
        "evidence": deepcopy(value["evidence"]),
        "confirmed": True,
        "confirmed_at": value.get("confirmed_at"),
    })
    return base


def policy_is_confirmed(policy):
    return (
        isinstance(policy, dict)
        and policy.get("confirmed") is True
        and policy.get("status") == "confirmed"
        and policy.get("mode") == MODE_NODATA_IS_ZERO_POPULATION
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
    return "populationnodatav1-" + hashlib.sha256(raw).hexdigest()


def nodata_semantics_record(policy):
    """The provenance record embedded into a converted cell / attribute."""

    if not policy_is_confirmed(policy):
        return None
    return {
        "applied": True,
        "mode": MODE_NODATA_IS_ZERO_POPULATION,
        "coverage_status": CONFIRMED_ZERO_COVERAGE_STATUS,
        "policy_fingerprint": policy_fingerprint(policy),
        "source_id": policy.get("source_id"),
        "source": policy.get("source"),
        "statement": policy.get("statement") or STATEMENT,
        "evidence": deepcopy(policy.get("evidence")),
        "semantics": deepcopy(SEMANTICS),
        "quality_flags": ["nodata_interpreted_as_zero_population", "confirmed_zero_not_an_observation"],
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
    area = float(target_area_m2 or 0.0)
    result = {
        "status": "passed",
        "value_status": "passed",
        "coverage_status": CONFIRMED_ZERO_COVERAGE_STATUS,
        "population_count_people": 0.0,
        "population_density_people_km2": 0.0,
        "density_support_area_m2": area,
        "density_semantics": "confirmed_zero_population_exposure",
        "target_area_m2": area,
        "valid_covered_area_m2": area,
        "source_coverage_fraction": 1.0,
        "source_pixel_count": 0,
        "interpretation": "confirmed_zero_population_not_an_observation",
        "quality_flags": list(record["quality_flags"]) + list(extra_quality_flags or []),
        "nodata_semantics": {**record, "trigger": trigger or "in_footprint_nodata"},
    }
    if nodata_pixel_count is not None:
        result["nodata_pixel_count"] = int(nodata_pixel_count)
    return result


__all__ = [
    "CONFIRMED_ZERO_COVERAGE_STATUS", "MODE_NODATA_IS_ZERO_POPULATION",
    "NEVER_CONVERTED_COVERAGE_STATUSES", "POLICY_KEY", "SCHEMA_VERSION", "SEMANTICS",
    "STATEMENT", "confirmed_zero_allocation", "default_population_nodata_policy",
    "nodata_semantics_record", "normalize_population_nodata_policy", "policy_fingerprint",
    "policy_is_confirmed",
]
