"""JSON-safe contracts for independent building-clearance engineering assessment."""

from __future__ import annotations

from copy import deepcopy
from math import isfinite


def default_building_clearance_policy():
    return {
        "status": "pending_confirmation",
        "horizontal_clearance_m": None,
        "vertical_clearance_m": None,
        "min_building_height_m": None,
        "terrain_relief_review_m": None,
        "source": "未配置；必须由项目工程依据确认",
        "confirmed": False,
        "vertical_reference": "egm2008_orthometric",
    }


def normalize_building_clearance_policy(value):
    source = value if isinstance(value, dict) else {}
    result = default_building_clearance_policy()
    source_text = str(source.get("source") or "").strip()
    result.update({
        "horizontal_clearance_m": _optional_nonnegative(
            source.get("horizontal_clearance_m"), "horizontal_clearance_m"
        ),
        "vertical_clearance_m": _optional_nonnegative(
            source.get("vertical_clearance_m"), "vertical_clearance_m"
        ),
        "min_building_height_m": _optional_nonnegative(
            source.get("min_building_height_m"), "min_building_height_m"
        ),
        "terrain_relief_review_m": _optional_nonnegative(
            source.get("terrain_relief_review_m"), "terrain_relief_review_m"
        ),
        "source": source_text or result["source"],
        "confirmed": bool(source.get("confirmed", False)),
    })
    complete = (
        result["horizontal_clearance_m"] is not None
        and result["vertical_clearance_m"] is not None
        and bool(source_text)
    )
    result["status"] = "confirmed" if complete and result["confirmed"] else "pending_confirmation"
    return result


def empty_building_clearance_assessment(status="not_calculated"):
    return {
        "status": status,
        "algorithm_id": "building_clearance_v1",
        "algorithm_version": "1.0",
        "input_fingerprint": None,
        "route_count": 0,
        "routes": [],
        "breach_segments": [],
        "critical_buildings": [],
        "statistics": {
            "breach_count": 0,
            "safe_route_count": 0,
            "unknown_route_count": 0,
            "unresolved_building_count": 0,
        },
        "provenance": {},
        "semantics": {
            "scope": "engineering_building_clearance_only",
            "not_accident_probability": True,
            "not_regulatory_compliance": True,
            "unknown_is_never_safe": True,
        },
    }


def normalize_building_clearance_assessment(value):
    if not isinstance(value, dict):
        return empty_building_clearance_assessment()
    result = empty_building_clearance_assessment(str(value.get("status") or "not_calculated"))
    result.update(deepcopy(value))
    result.setdefault("routes", [])
    result.setdefault("breach_segments", [])
    result.setdefault("critical_buildings", [])
    result.setdefault("statistics", empty_building_clearance_assessment()["statistics"])
    result.setdefault("provenance", {})
    result.setdefault("semantics", empty_building_clearance_assessment()["semantics"])
    return result


def _optional_nonnegative(value, field):
    if value in (None, ""):
        return None
    number = float(value)
    if not isfinite(number) or number < 0:
        raise ValueError(f"{field} 必须是有限非负数")
    return number


# --------------------------------------------------------------------------- shared semantics
#
# The roof elevation and the vertical-clearance test are the *single* definition of
# the EGM2008 building-clearance semantics.  ``BuildingClearanceV1`` and the V3-C
# ``BuildingPolygonValidator`` both consume them, so a third, drifting roof formula
# cannot appear.  Both helpers are fail-closed: anything unresolved stays unresolved
# and is never reported as safe.

RESOLVED_BUILDING_HEIGHT_STATUSES = frozenset({
    "accepted", "confirmed", "measured", "predicted", "resolved", "valid", "verified",
})


def building_height_status_is_resolved(value):
    """Whether a source explicitly marks its height value as usable.

    A numeric height without this status is still unknown.  In particular, missing,
    invalid, pending, unknown and unresolved values never become a zero-height building
    and never silently pass a constraint gate.

    .. warning::

       "resolved" means the value may be **used in a computation**, not that it is
       trustworthy.  In particular the upstream GBA ``height_status='valid'`` rule is
       only ``height_m > 0``.  Reliability is a separate axis; see
       :func:`building_height_value_is_usable` and
       :func:`building_height_evidence_status`.  Adding evidence gating *here* would
       change the semantics of every existing consumer at once, so this predicate keeps
       its historical meaning on purpose.
    """

    return str(value or "").strip().lower() in RESOLVED_BUILDING_HEIGHT_STATUSES


# --------------------------------------------------------------------------- height value semantics
#
# F1-C：把"高度有值"与"高度可信"彻底分开表达。
#
# 三个互不相同的概念，绝不允许再用一个字段（尤其是 ``height_status``）同时表示：
#
# 1. ``height_present``       —— 该建筑有正的高度数值；
# 2. ``height_value_usable``  —— 该数值可以参与计算（不是官方 NoData / 不可用哨兵）；
# 3. ``height_trusted``       —— 有独立证据支持该数值可信（证据分级，未来才启用）。
#
# 上游 GBA 的官方哨兵（Round35-E §Q4 实测：``var=-1.0`` 261 条为"方差不可用"；
# ``(height, var)=(-999,-999)`` 为官方 NoData）必须在**消费端**防御：本地
# ``clean_gba_nodata.py`` 只清理了 ``-999``，因此 ``-1.0`` 哨兵会以"有值"的形态流入。

#: 上游官方 NoData 哨兵（GBA LoD1：height 与 var 成对出现）。
HEIGHT_NODATA_SENTINELS = frozenset({-999.0, -9999.0})
#: 上游"不可用但高度本身有效"的哨兵。GBA 用它表示**方差不可用**，与高度无关；
#: 任何把它当数值参与计算的做法（例如求标准差、做可信度门禁）都是错的。
HEIGHT_UNAVAILABLE_SENTINELS = frozenset({-1.0})


def height_value_is_sentinel(value):
    """Whether a raw source value is a known upstream sentinel (NoData / unavailable)."""

    number = _finite_or_none(value)
    if number is None:
        return False
    return number in HEIGHT_NODATA_SENTINELS or number in HEIGHT_UNAVAILABLE_SENTINELS


def height_value_is_nodata_sentinel(value):
    """Whether a raw source value is the official NoData sentinel."""

    number = _finite_or_none(value)
    return number is not None and number in HEIGHT_NODATA_SENTINELS


def building_height_present(value):
    """``height_present``：有正的高度数值（哨兵与 0 都不算"有值"）。"""

    number = _finite_or_none(value)
    return number is not None and number > 0.0 and not height_value_is_sentinel(number)


def building_height_value_is_usable(value):
    """``height_value_usable``：数值可用（有限、正、且不是任何已知上游哨兵）。

    这是**允许参与计算**的门槛，不是可信度门槛。它比历史上"字段非空"更严，
    但不会因为证据不足而阻断任何现有建筑：本地 538,203 栋 ``height_m`` 都是正数，
    因此行为对真实数据保持不变；只有哨兵/零/负数才被拦下。
    """

    return building_height_present(value)


def building_height_var_is_usable(value):
    """``height_var`` 是否可用于数值计算 —— 哨兵值一律不可用。

    GBA 的 ``height_var`` 是**方差**（单位 m²，论文 §4.4.3 / §4.5.2），不是标准差。
    官方在"方差不可用"时写 ``-1.0``（Round35-E 实测 261 条，且这些记录的 ``height``
    都是正常正值），因此：

    * 负数（含 ``-1.0`` 与 ``-999``）一律**不可用**，既不能当数值代入任何公式，
      也不能被当成 0（"方差为 0"是另一回事，官方数据里并不存在）；
    * 该判据只回答"能否计算"，不回答"能否据此相信高度"。

    官方哨兵的确切触发条件未在任何官方文档中说明，本项目**不推断**它，
    只把它当作"不可用"处理。
    """

    number = _finite_or_none(value)
    if number is None or number < 0.0:
        return False
    return not height_value_is_sentinel(number)


def height_var_semantics(value):
    """``height_var`` 的显式语义标签（供审计与未来三维视图使用）。"""

    number = _finite_or_none(value)
    if number is None:
        return "missing"
    if height_value_is_nodata_sentinel(number):
        return "nodata_sentinel"
    if number < 0.0:
        return "unavailable_sentinel"
    return "variance_m2_usable"


#: F3-B 起正式产生的建筑高度证据等级（H0–H4，Round35-F2 `02` §1 重排后的命名）。
#: 语义**绝不**再与 ``height_status``（数据存在性）或 ``height_value_usable``（数值可用）
#: 混同：证据等级回答"这个高度数值有没有独立依据支撑"。
HEIGHT_EVIDENCE_LEVELS = frozenset({
    "verified", "source_tagged", "derived", "predicted", "unknown",
})

#: 只有真正经独立核验（测绘/现场测量/可靠独立参照）的证据等级。OSM ``height`` 标签
#: 只是"来源可追溯"，**不是**已核实，因此 ``source_tagged`` 不在此集合内。
VERIFIED_HEIGHT_EVIDENCE_LEVELS = frozenset({"verified"})

#: 净空正式 pass 允许的证据等级。``derived``（楼层数 × 3 m）是工程推算，必须另附
#: ``derived_assumption``；``predicted`` / ``unknown`` **不得**单独支撑 pass。
CLEARANCE_PASS_HEIGHT_EVIDENCE_LEVELS = frozenset({"verified", "source_tagged", "derived"})

#: F1 时代的旧命名 → F3 新命名的显式映射，避免旧项目/旧 sidecar 的字段被误读。
#: 旧 ``authoritative`` 语义是"来源可追溯"而非"实测已核实"，因此映射到 ``source_tagged``；
#: 旧 ``conservative``（预测异常 + 独立参照上修）仍只是预测，映射到 ``predicted``。
_LEGACY_EVIDENCE_LEVEL_MAP = {
    "authoritative": "source_tagged",
    "osm_tag": "source_tagged",
    "conservative": "predicted",
}

#: ``building_height_evidence_status`` 的 "trusted" 分支：有独立/可追溯来源支撑的证据。
#: 保留旧名是为了兼容 F1 语义；新代码请优先用 :data:`HEIGHT_EVIDENCE_LEVELS` 与
#: :func:`building_height_evidence_supports_clearance_pass`。
TRUSTED_BUILDING_HEIGHT_EVIDENCE_LEVELS = frozenset({"verified", "source_tagged"})


def normalize_height_evidence_level(value, *, height_present=False):
    """把任意来源的证据等级归一化到 H0–H4 五级，并对旧数据做保守回填。

    规则（绝不回填为 ``verified``，因为本区域没有任何经独立核实的实测高度）：

    * 新五级取值直接通过；
    * 旧命名按 :data:`_LEGACY_EVIDENCE_LEVEL_MAP` 显式降级（``authoritative``/``osm_tag``
      → ``source_tagged``，``conservative`` → ``predicted``）；
    * 其余（缺失/空/未知字符串）：有正高度数值 → ``predicted``，否则 → ``unknown``。
    """

    raw = str(value or "").strip().lower()
    mapped = _LEGACY_EVIDENCE_LEVEL_MAP.get(raw)
    if mapped is not None:
        return mapped
    if raw in HEIGHT_EVIDENCE_LEVELS:
        return raw
    return "predicted" if height_present else "unknown"


def building_height_evidence_supports_clearance_pass(evidence_level):
    """净空验证证据门禁：该证据等级能否**单独**支撑正式净空 ``pass``。

    * ``verified`` / ``source_tagged`` / ``derived`` → ``True``（``derived`` 需附
      ``derived_assumption``，由消费端另行记录）；
    * ``predicted`` / ``unknown`` → ``False``：预测高度仍可作为**数值输入**参与候选
      预筛与"工程估计满足"判断，但绝不能冒充已核实的净空安全结论。

    与 :func:`building_height_status_is_resolved` 刻意分离：后者保持历史语义
    （"可参与计算"，含 ``predicted``），候选搜索继续可用；本函数只收紧正式验证入口。
    """

    return normalize_height_evidence_level(evidence_level, height_present=False) in (
        CLEARANCE_PASS_HEIGHT_EVIDENCE_LEVELS
    )


def building_height_evidence_status(value, evidence_level=None):
    """``height_present`` / ``height_trusted`` 的显式状态（F1-C 兼容接口）。

    返回 ``unknown`` / ``present_unverified`` / ``trusted`` / ``unusable_sentinel``。

    该接口回答的是"数值是否有独立/可追溯依据"，不回答"能否支撑净空 pass"；
    净空门禁请用 :func:`building_height_evidence_supports_clearance_pass`。
    """

    number = _finite_or_none(value)
    if height_value_is_nodata_sentinel(number) or number is None or number <= 0.0:
        return "unusable_sentinel" if height_value_is_sentinel(number) else "unknown"
    level = normalize_height_evidence_level(evidence_level, height_present=True)
    if level in TRUSTED_BUILDING_HEIGHT_EVIDENCE_LEVELS:
        return "trusted"
    return "present_unverified"


# --------------------------------------------------------------------------- occupancy semantics
#
# F1-A：``building_count`` 是**建筑质心分配数量**，不是"该格有没有建筑"。
# 一栋 footprint 跨越格边界、质心落在邻格时，本格 ``building_count == 0``
# 却仍有真实建筑覆盖（Round35-E 实测 20,491 个 L8 格、25.82 km²）。
# 因此"无建筑垂向约束"必须同时要求**覆盖面积为零**。

#: 覆盖信息本身缺失（旧数据 / 手工构造的 fact）：保持历史行为，不据此新增 unknown，
#: 因为把"没有覆盖字段"一律当未知会改变所有既有合成输入的语义。
BUILDING_OCCUPANCY_UNKNOWN = "unknown"

#: 仅用于**审计/回归对照**的显式历史语义开关（F1-A）。把某个 building fact 的
#: 该键设为 ``True``，消费端就按修复前的规则处理它（``building_count == 0`` 即视为
#: 没有建筑垂向约束）。用途只有一个：在同一份真实数据上逐格测出修复前后的差异。
#:
#: **生产路径从不设置它**；任何带该标记的结果都必须被视为审计材料，不能当作当前结果。
LEGACY_ZERO_COUNT_SHORTCUT_KEY = "legacy_zero_count_shortcut_for_audit"


def building_occupancy_state(*, building_count, building_area_m2=None,
                             building_coverage_ratio=None):
    """三值建筑占用状态：``absent`` / ``present`` / ``unknown``。

    * ``building_count > 0`` ⇒ ``present``（有质心分配的实建）；
    * ``building_count == 0`` 且覆盖面积/比例**明确为 0** ⇒ ``absent``（唯一可以声明
      "没有建筑垂向约束"的情形）；
    * ``building_count == 0`` 且覆盖面积/比例**大于 0** ⇒ ``present``（有 footprint
      但无质心高度 —— 必须 fail-closed，绝不是"无建筑"）；
    * 覆盖信息缺失 ⇒ ``unknown``，由调用方决定是否保留历史行为。
    """

    count = _finite_or_none(building_count)
    if count is not None and count > 0:
        return "present"
    area = _finite_or_none(building_area_m2)
    ratio = _finite_or_none(building_coverage_ratio)
    known = [value for value in (area, ratio) if value is not None]
    if any(value > 0.0 for value in known):
        return "present"
    if known and all(value <= 0.0 for value in known):
        return "absent"
    return BUILDING_OCCUPANCY_UNKNOWN


def building_footprint_occupancy(*, building_count, building_area_m2=None,
                                 building_coverage_ratio=None):
    """兼容别名：返回与 :func:`building_occupancy_state` 相同的三值状态。"""

    return building_occupancy_state(
        building_count=building_count, building_area_m2=building_area_m2,
        building_coverage_ratio=building_coverage_ratio,
    )


def no_building_vertical_constraint(**facts):
    """唯一的"该格确认没有建筑垂向约束"判据。

    返回 ``True`` 仅当建筑质心数量为 0 **且** footprint 覆盖被明确证实为零。
    覆盖信息缺失时返回 ``False``（fail-closed 的保守侧）。
    """

    return building_occupancy_state(**facts) == "absent"


def building_roof_elevation(ground_elevation_m, height_m):
    """Canonical EGM2008 roof elevation = ground + predicted height.

    ``ground`` is the DTM ground elevation resolved over the footprint and ``height``
    the source (for example GBA ``height_m``) value.  Missing or non-finite inputs --
    and a missing/unknown ``height_status`` semantic decided by the caller before
    calling this -- leave the roof unresolved.

    F1-C：官方哨兵（``-999`` NoData、``-1.0`` 不可用）与 ``<= 0`` 的高度**不得**参与
    计算。本地 ``clean_gba_nodata.py`` 只清理了 ``-999``，所以消费端必须自己防御：
    否则一个 ``height_m = -999`` 的漏网记录会算出"楼顶比地面低 999 m"的荒谬结论，
    并可能让净空判定反向通过。
    """

    ground = _finite_or_none(ground_elevation_m)
    height = _finite_or_none(height_m)
    if ground is None:
        return {
            "status": "unresolved",
            "reason": "building_footprint_ground_elevation_unresolved",
            "roof_elevation_egm2008_m": None,
            "ground_elevation_m": None,
            "height_m": height,
            "vertical_reference": "egm2008_orthometric",
        }
    if height is None:
        return {
            "status": "unresolved",
            "reason": "building_height_missing",
            "roof_elevation_egm2008_m": None,
            "ground_elevation_m": ground,
            "height_m": None,
            "vertical_reference": "egm2008_orthometric",
        }
    if not building_height_value_is_usable(height):
        return {
            "status": "unresolved",
            "reason": (
                "building_height_nodata_sentinel" if height_value_is_nodata_sentinel(height)
                else "building_height_sentinel_or_nonpositive"
            ),
            "roof_elevation_egm2008_m": None,
            "ground_elevation_m": ground,
            # 原值保留用于审计（不隐藏、不改成 0）。
            "height_m": height,
            "height_value_raw": height,
            "vertical_reference": "egm2008_orthometric",
        }
    return {
        "status": "resolved",
        "reason": None,
        "roof_elevation_egm2008_m": ground + height,
        "ground_elevation_m": ground,
        "height_m": height,
        "vertical_reference": "egm2008_orthometric",
    }


def evaluate_vertical_clearance(
    *, minimum_altitude_egm2008_m, roof_elevation_egm2008_m, required_clearance_m,
    ground_elevation_m=None,
):
    """Compare a route's minimum altitude with ``roof + required_clearance``.

    Returns the signed ``vertical_margin_m`` (positive = clear) and a status of
    ``resolved`` / ``unresolved``.  An aircraft between the building ground and the
    roof has zero clearance from the prism; an aircraft **below** the resolved ground
    (when the caller supplies it) is not a clearance figure at all and stays
    ``unresolved`` -- fail-closed, exactly as the existing assessor reads it.
    """

    altitude = _finite_or_none(minimum_altitude_egm2008_m)
    roof = _finite_or_none(roof_elevation_egm2008_m)
    required = _finite_or_none(required_clearance_m)
    ground = _finite_or_none(ground_elevation_m)
    base = {
        "vertical_reference": "egm2008_orthometric",
        "minimum_altitude_egm2008_m": altitude,
        "roof_elevation_egm2008_m": roof,
        "ground_elevation_m": ground,
        "required_clearance_m": required,
    }
    if altitude is None:
        return {**base, "status": "unresolved", "reason": "route_altitude_unresolved",
                "vertical_margin_m": None, "required_altitude_egm2008_m": None,
                "observed_clearance_m": None}
    if roof is None:
        return {**base, "status": "unresolved", "reason": "building_roof_unresolved",
                "vertical_margin_m": None, "required_altitude_egm2008_m": None,
                "observed_clearance_m": None}
    if required is None:
        return {**base, "status": "unresolved", "reason": "vertical_clearance_unresolved",
                "vertical_margin_m": None, "required_altitude_egm2008_m": None,
                "observed_clearance_m": None}
    if ground is not None and altitude < ground:
        return {**base, "status": "unresolved", "reason": "aircraft_below_building_ground",
                "vertical_margin_m": None, "required_altitude_egm2008_m": roof + required,
                "observed_clearance_m": None}
    # ``observed_clearance_m`` remains the physical non-negative gap above the roof,
    # while ``vertical_margin_m`` is the signed altitude margin used by every safety
    # gate.  Deriving the latter from the clamped gap made a route penetrating the
    # roof appear to have exactly zero margin; with a configured 0 m extra clearance
    # that incorrectly passed instead of reporting the penetration.
    roof_delta = altitude - roof
    observed_clearance = max(0.0, roof_delta)
    return {
        **base,
        "status": "resolved",
        "reason": None,
        "observed_clearance_m": observed_clearance,
        "required_altitude_egm2008_m": roof + required,
        "vertical_margin_m": roof_delta - required,
    }


def _finite_or_none(value):
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


__all__ = [
    "RESOLVED_BUILDING_HEIGHT_STATUSES", "building_height_status_is_resolved",
    "building_roof_elevation", "default_building_clearance_policy",
    "empty_building_clearance_assessment", "evaluate_vertical_clearance",
    "normalize_building_clearance_policy", "normalize_building_clearance_assessment",
    # F1-A 占用语义：质心数量 ≠ 网格内是否有建筑。
    "BUILDING_OCCUPANCY_UNKNOWN", "building_occupancy_state",
    "building_footprint_occupancy", "no_building_vertical_constraint",
    # F1-C 高度值/证据语义：有值 ≠ 可信。
    "HEIGHT_NODATA_SENTINELS", "HEIGHT_UNAVAILABLE_SENTINELS",
    "TRUSTED_BUILDING_HEIGHT_EVIDENCE_LEVELS", "height_value_is_sentinel",
    "height_value_is_nodata_sentinel", "building_height_present",
    "building_height_value_is_usable", "building_height_evidence_status",
    "building_height_var_is_usable", "height_var_semantics",
    # F3-A/B：H0–H4 证据等级与净空证据门禁。
    "HEIGHT_EVIDENCE_LEVELS", "VERIFIED_HEIGHT_EVIDENCE_LEVELS",
    "CLEARANCE_PASS_HEIGHT_EVIDENCE_LEVELS", "normalize_height_evidence_level",
    "building_height_evidence_supports_clearance_pass",
]
