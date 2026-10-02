"""P17 —— **连续服务 / 不可接受事件评估** 的 domain 契约（纯数据，无 I/O）。

核心命题
--------

**coverage gap ≠ 自动 planning failure**。P15 的缺口是"三维覆盖在航路上缺失多少"的
空间结论；它回答不了"这次缺失是否已经超出可接受的服务连续中断、以及监视/导航链路
是否还来得及介入"。P17 就是在 P15/P16 之后补上这一层判定：

* **C 通信**：区分 ``service_outage``（全失联）与 ``redundancy_degradation``
  （链路仍可用但独立 provider 不足），两者使用**互相独立**的时长阈值；
* **N 导航**：**绝不**使用"RTK 断 X 秒即失败"这种简单规则，而是显式状态机
  （RTK → GNSS 回退 → ATTI），只有"可靠导航也不可用"才是 ``unacceptable``；
* **S 监视**：不再只评估航路中心线，而是评估**水平保护走廊**上的探测-响应链时延
  （``T_margin = T_available - T_chain``）。

四种结论（fail-closed）
-----------------------

``fully_satisfied`` / ``acceptable_with_managed_gap`` / ``unacceptable`` / ``unknown``。
``unknown``（证据缺失）**必须**继续阻止下游门禁，绝不因为"没算出来"就放行。

来源纪律
--------

本模块的所有默认值都是**工程基线**（``builtin_engineering_assumption``），
不是法规阈值、也不是厂家事实。参数可由 :mod:`cns_planner.domain.planning_evidence`
的显式记录覆盖（``confirmed_source_fact`` / ``external_reference`` /
``engineering_assumption`` / ``unknown``），并且每个参数在结果里都保留出处。
"""

from __future__ import annotations

from math import isfinite

#: 契约版本与算法标识。
CONTINUOUS_SERVICE_SCHEMA_VERSION = "round2.5-continuous-service-acceptability"
CONTINUOUS_SERVICE_ALGORITHM_ID = "continuous_service_acceptability_v1"
CONTINUOUS_SERVICE_ALGORITHM_VERSION = "1.0"

#: 四种运行可接受性结论（**顺序即严重度**）。
ACCEPTABILITY_STATUSES = (
    "fully_satisfied",
    "acceptable_with_managed_gap",
    "unacceptable",
    "unknown",
)

#: 子系统级判定词汇。
SUBSYSTEM_ACCEPTABILITY = (
    "nominal",
    "acceptable_degraded",
    "acceptable_with_managed_gap",
    "unacceptable",
    "unknown",
    "not_applicable",
)

#: 子系统结论 → 全局运行可接受性结论的**显式**映射（两套词汇不混用）。
SUBSYSTEM_TO_ACCEPTABILITY = {
    "nominal": "fully_satisfied",
    "acceptable_degraded": "fully_satisfied",
    "acceptable_with_managed_gap": "acceptable_with_managed_gap",
    "unacceptable": "unacceptable",
    "unknown": "unknown",
}

#: 连续事件类型（C/N/S 通用）。
CONTINUOUS_EVENT_KINDS = (
    "service_outage",
    "redundancy_degradation",
    "navigation_degradation",
    "surveillance_detection_gap",
)

#: 保护链的五个**必需**分量（缺任何一个 ⇒ 该链路时延不可判定）。
T_CHAIN_COMPONENTS = (
    "detect_track",
    "sensor_to_platform",
    "platform_processing",
    "platform_to_aircraft",
    "aircraft_response_manoeuvre",
)

#: 操作场景的固定口径（Round 2.5 当前场景）。
OPERATION_SCENARIO_DEFAULTS = {
    "single_ownship": True,
    "intruder_scope": "other_uav_only",
    "cruise_altitude": "ALT-100",
    "design_intruder_speed_mps": 20.0,
    "nominal_closing_speed_mps": 35.0,
    "conservative_closing_speed_mps": 40.0,
    "design_intruder_speed_authority": "engineering_assumption",
    "cruise_altitude_authority": "project_route_operating_layer",
}

#: 内置工程基线的**逐项**出处与说明。值本身不是法规事实。
BASELINE_SOURCES = {
    "c_full_outage_max_s": {
        "source": (
            "Round 2.5 v1 engineering baseline：取 FC30 failsafe 触发门限（遥控信号丢失"
            "超过 3 s 触发 RTH）作为**设备故障保护事实**驱动的工程基线，"
            "**不是**法规阈值。"
        ),
        "statement": "通信全失联可接受最长时长＝3 s（工程基线，可由规划证据显式覆盖）。",
        "report_disclosure": (
            "本阈值为工程基线，其依据是 FC30 设备 failsafe 触发事实；"
            "不代表任何法规或适航要求。"
        ),
        "basis": "fc30_failsafe_trigger_fact",
    },
    "c_redundancy_degradation_max_s": {
        "source": "Round 2.5 v1 engineering baseline（冗余退化专用，独立于全失联阈值）。",
        "statement": "通信冗余退化可接受最长时长＝10 s（工程基线，可由规划证据显式覆盖）。",
        "report_disclosure": "本阈值为工程基线；链路仍可用但独立 provider 不足的时间上限尚未由权威资料确认。",
        "basis": "round2_5_v1_engineering_baseline",
    },
    "T_chain_detect_track_s": {
        "source": "Round 2.5 v1 engineering baseline（探测/跟踪 3 s）。",
        "statement": "保护链探测/跟踪分量＝3 s（工程基线）。",
        "report_disclosure": "保护链时延分量为工程基线，未由厂家或权威资料确认。",
        "basis": "round2_5_v1_engineering_baseline",
    },
    "T_chain_sensor_to_platform_s": {
        "source": "Round 2.5 v1 engineering baseline（传感器→平台 1 s）。",
        "statement": "保护链传感器→平台分量＝1 s（工程基线）。",
        "report_disclosure": "保护链时延分量为工程基线，未由厂家或权威资料确认。",
        "basis": "round2_5_v1_engineering_baseline",
    },
    "T_chain_platform_processing_s": {
        "source": "Round 2.5 v1 engineering baseline（平台处理 2 s）。",
        "statement": "保护链平台处理分量＝2 s（工程基线）。",
        "report_disclosure": "保护链时延分量为工程基线，未由厂家或权威资料确认。",
        "basis": "round2_5_v1_engineering_baseline",
    },
    "T_chain_platform_to_aircraft_s": {
        "source": "Round 2.5 v1 engineering baseline（平台→航空器 1 s）。",
        "statement": "保护链平台→航空器分量＝1 s（工程基线）。",
        "report_disclosure": "保护链时延分量为工程基线，未由厂家或权威资料确认。",
        "basis": "round2_5_v1_engineering_baseline",
    },
    "T_chain_aircraft_response_manoeuvre_s": {
        "source": "Round 2.5 v1 engineering baseline（航空器响应机动 3 s）。",
        "statement": "保护链航空器响应机动分量＝3 s（工程基线）。",
        "report_disclosure": "保护链时延分量为工程基线，未由厂家或权威资料确认。",
        "basis": "round2_5_v1_engineering_baseline",
    },
    "D_safety_m": {
        "source": "Round 2.5 v1 engineering baseline：安全边界取 0 m（即保护走廊内边界＝航路中心线）。",
        "statement": "D_safety＝0 m（工程基线；保守性来自链时延与不确定度项）。",
        "report_disclosure": "安全边界距离为工程基线；未由法规或权威资料确认。",
        "basis": "round2_5_v1_engineering_baseline",
    },
    "D_uncertainty_m": {
        "source": "Round 2.5 v1 engineering baseline：不确定度附加量取 0 m（无显式证据时不留白）。",
        "statement": "D_uncertainty＝0 m（工程基线）。",
        "report_disclosure": "不确定度距离为工程基线；定位/航迹不确定度尚未由权威资料确认。",
        "basis": "round2_5_v1_engineering_baseline",
    },
    "design_intruder_speed_mps": {
        "source": "Round 2.5 操作场景（engineering_assumption）：设计入侵者速度 20 m/s。",
        "statement": "设计入侵者速度＝20 m/s（工程假设）。",
        "report_disclosure": "设计入侵者速度为工程假设，不代表实际运行交通统计。",
        "basis": "operation_scenario_engineering_assumption",
    },
    "nominal_closing_speed_mps": {
        "source": "Round 2.5 操作场景：标称接近速度＝本机航路速度 15 + 设计入侵者 20 = 35 m/s。",
        "statement": "标称接近速度＝35 m/s（由操作场景推导）。",
        "report_disclosure": "接近速度为工程假设推导，不代表实测交会速度分布。",
        "basis": "operation_scenario_engineering_assumption",
    },
    "conservative_closing_speed_mps": {
        "source": "Round 2.5 操作场景（engineering_assumption）：保守接近速度 40 m/s。",
        "statement": "保守接近速度＝40 m/s（工程保守取值，高于标称 35 m/s）。",
        "report_disclosure": "保守接近速度为工程假设，不是实测值。",
        "basis": "operation_scenario_engineering_assumption",
    },
    "relative_speed_basis": {
        "source": "Round 2.5 v1 engineering baseline：保护链时延判定默认采用**保守**接近速度。",
        "statement": "相对速度口径＝conservative（工程基线）。",
        "report_disclosure": "相对速度口径为工程基线；可由工程依据显式改为 nominal。",
        "basis": "round2_5_v1_engineering_baseline",
    },
    "navigation_route_containment_accuracy_m": {
        "source": "Round 2.5 v1 engineering baseline：航路保持所需水平精度 10 m（工程基线）。",
        "statement": "航路保持所需水平精度＝10 m（工程基线）。",
        "report_disclosure": "航路保持精度要求为工程基线；未由法规或权威资料确认。",
        "basis": "round2_5_v1_engineering_baseline",
    },
    "rtk_availability": {
        "source": "Round 2.5：RTK 可用性必须有显式证据（机载声明或工程证据），否则保持 unknown。",
        "statement": "RTK 可用性未提供显式依据时不做任何假设。",
        "report_disclosure": "RTK 可用性属于需要证据的输入，不采用内置假设。",
        "basis": "evidence_required",
    },
    "rtk_unavailable_along_route": {
        "source": "Round 2.5：航路内 RTK 不可用区间必须有显式证据，否则保持 unknown。",
        "statement": "RTK 不可用区间形态未提供显式依据时不做任何假设。",
        "report_disclosure": "RTK 不可用区间属于需要证据的输入，不采用内置假设。",
        "basis": "evidence_required",
    },
    "gnss_availability": {
        "source": "Round 2.5：GNSS 可用性必须有显式证据，否则保持 unknown。",
        "statement": "GNSS 可用性未提供显式依据时不做任何假设。",
        "report_disclosure": "GNSS 可用性属于需要证据的输入，不采用内置假设。",
        "basis": "evidence_required",
    },
    "gnss_accuracy_along_route_m": {
        "source": "Round 2.5：航路内 GNSS 水平精度必须有显式证据，否则保持 unknown。",
        "statement": "GNSS 水平精度未提供显式依据时不做任何假设。",
        "report_disclosure": "GNSS 水平精度属于需要证据的输入，不采用内置假设。",
        "basis": "evidence_required",
    },
    "navigation_degradation_time_s": {
        "source": (
            "Round 2.5：外部研究参考（公开资料中 RTK 恢复约 9–13 s）。"
            "**仅登记**，不进入任何硬门判定。"
        ),
        "statement": "导航降级允许时长为外部研究参考，不作为本评估的硬门。",
        "report_disclosure": "本项为外部研究参考值，不是法规阈值，也不参与硬门判定。",
        "basis": "external_reference_not_a_gate",
    },
}

#: 内置工程基线的**数值**（``None`` = 不提供内置假设，必须由显式证据提供）。
BASELINE_VALUES = {
    "c_full_outage_max_s": 3.0,
    "c_redundancy_degradation_max_s": 10.0,
    "T_chain_detect_track_s": 3.0,
    "T_chain_sensor_to_platform_s": 1.0,
    "T_chain_platform_processing_s": 2.0,
    "T_chain_platform_to_aircraft_s": 1.0,
    "T_chain_aircraft_response_manoeuvre_s": 3.0,
    "D_safety_m": 0.0,
    "D_uncertainty_m": 0.0,
    "design_intruder_speed_mps": 20.0,
    "nominal_closing_speed_mps": 35.0,
    "conservative_closing_speed_mps": 40.0,
    "relative_speed_basis": "conservative",
    "navigation_route_containment_accuracy_m": 10.0,
    "rtk_availability": None,
    "rtk_unavailable_along_route": None,
    "gnss_availability": None,
    "gnss_accuracy_along_route_m": None,
    "navigation_degradation_time_s": None,
}

#: 默认的 C 服务门限（**服务中断**与**冗余退化**各自独立）。
DEFAULT_SERVICE_ACCEPTABILITY_LIMITS = {
    "C": {"service_outage": 3.0, "redundancy_degradation": 10.0},
    "S": {"service_outage": 3.0, "redundancy_degradation": 10.0},
}

#: 导航状态机的显式词汇。
NAVIGATION_STATES = (
    "nominal",
    "acceptable_degraded",
    "unacceptable",
    "unknown",
)

#: 判定原因码（前端 / 报告使用；**绝不**是自由文本）。
CONTINUOUS_SERVICE_REASONS = {
    "no_upstream_p15": "P14/P15 走廊缺口评估不是 current 且可评估状态",
    "no_applicable_route_scope": "存在航路但没有任何可适用的连续服务评估范围",
    "no_p14_route": "P15 中存在 P14 无法对应的航路",
    "no_route_speed": "选定机载档案未提供航路速度，无法把缺口长度换算为持续时间",
    "no_p13_geometry": "P14 未提供航路几何，保护走廊无法构造",
    "no_time_chain": "保护链时延分量不完整，T_chain 不可判定",
    "no_detection_evidence": "监视探测证据（首次探测距离）不足",
    "detection_range_below_safety_distance": "声明的探测范围小于安全边界距离（无法在安全边界之外探测）",
    "no_navigation_evidence": "导航证据（RTK/GNSS 可用性与精度）不足",
    "service_outage_exceeds_limit": "连续全失联时长超过服务中断阈值",
    "redundancy_degradation_exceeds_limit": "连续冗余退化时长超过冗余退化阈值",
    "navigation_gnss_unavailable": "GNSS/可靠导航不可用（ATTI 降落，无航路保持）",
    "navigation_evidence_missing": "RTK/GNSS 可用性或精度证据缺失",
    "tmargin_negative": "保护链时间余量为负（监视来不及介入）",
    "no_continuous_gap": "航路上没有连续服务缺口",
    "managed_gap_within_limits": "连续缺口在阈值内，可作为 managed_gap",
}


def parameter_baseline(name: str) -> dict:
    """返回一个 P17 参数的**内置工程基线**记录（无内置假设时 ``value=None``）。"""

    if name not in BASELINE_VALUES:
        raise ValueError(f"不是合法的连续服务参数：{name}")
    meta = BASELINE_SOURCES[name]
    return {
        "field": name,
        "value": BASELINE_VALUES[name],
        "source": meta["source"],
        "statement": meta["statement"],
        "report_disclosure": meta["report_disclosure"],
        "basis": meta["basis"],
        "source_type": (
            "internal_baseline"
            if BASELINE_VALUES[name] is not None
            else "unknown"
        ),
        "authority": (
            "builtin_engineering_assumption"
            if BASELINE_VALUES[name] is not None
            else "unknown"
        ),
    }


def default_continuous_service_policy() -> dict:
    """默认策略：**没有**用户覆盖，全部参数由证据 / 内置基线解析。

    这个容器本身也进 ``project_state``（可保存、可重开）。``service_acceptability_limits``
    默认是**空覆盖**（``{}``）——"没有用户覆盖"必须与"用户把阈值设成默认值"在语义上
    可区分，否则显式工程证据会被内置基线悄悄压过。真正生效的阈值取值顺序是：

    1. ``parameter_overrides`` / ``service_acceptability_limits``（显式策略覆盖）；
    2. :func:`cns_planner.domain.planning_evidence.resolve_continuous_parameter`
       的显式工程证据（``confirmed_source_fact`` / ``external_reference`` /
       ``engineering_assumption``）；
    3. :data:`DEFAULT_SERVICE_ACCEPTABILITY_LIMITS` 内置工程基线。
    """

    return {
        "status": "default",
        "semantics": "explicit_override_container_defaults_from_evidence_or_builtin_baseline",
        "service_acceptability_limits": {},
        "parameter_overrides": {},
        "source": "Round 2.5 v1 engineering baseline",
        "confirmed": False,
    }


def normalize_continuous_service_policy(value=None) -> dict:
    """规范化显式覆盖容器（fail-closed：数值必须非负有限，键必须合法）。"""

    result = default_continuous_service_policy()
    if value in (None, ""):
        return result
    if not isinstance(value, dict):
        raise ValueError("cns_continuous_service_policy 必须是对象")
    limits = value.get("service_acceptability_limits")
    if limits is not None:
        if not isinstance(limits, dict):
            raise ValueError("service_acceptability_limits 必须是对象")
        for subsystem, entry in limits.items():
            code = str(subsystem).upper()
            if code not in ("C", "S"):
                raise ValueError("Round 2.5 仅支持 C / S 两个服务的阈值覆盖")
            if not isinstance(entry, dict):
                raise ValueError(f"service_acceptability_limits.{code} 必须是对象")
            target = result["service_acceptability_limits"].setdefault(
                code, dict(DEFAULT_SERVICE_ACCEPTABILITY_LIMITS[code])
            )
            for kind in ("service_outage", "redundancy_degradation"):
                if kind in entry and entry[kind] is not None:
                    target[kind] = _nonnegative(entry[kind], f"{code}.{kind}")
    overrides = value.get("parameter_overrides")
    if overrides is not None:
        if not isinstance(overrides, dict):
            raise ValueError("parameter_overrides 必须是对象")
        for name, raw in overrides.items():
            if name not in BASELINE_VALUES:
                raise ValueError(f"parameter_overrides 含未知参数：{name}")
            if raw is None:
                continue
            if name == "relative_speed_basis":
                text = str(raw).strip().lower()
                if text not in ("nominal", "conservative"):
                    raise ValueError("relative_speed_basis 必须是 nominal / conservative")
                result["parameter_overrides"][name] = text
            elif name in ("rtk_availability", "gnss_availability"):
                text = str(raw).strip().lower()
                if text not in ("available", "not_available"):
                    raise ValueError(f"{name} 必须是 available / not_available")
                result["parameter_overrides"][name] = text
            elif name == "rtk_unavailable_along_route":
                text = str(raw).strip().lower()
                if text not in ("none", "whole_route", "explicit_segments", "unknown"):
                    raise ValueError(
                        "rtk_unavailable_along_route 必须是 none / whole_route / "
                        "explicit_segments / unknown"
                    )
                result["parameter_overrides"][name] = text
            else:
                result["parameter_overrides"][name] = _nonnegative(raw, name)
    if value.get("source") not in (None, ""):
        result["source"] = str(value["source"])
    result["confirmed"] = value.get("confirmed") is True
    result["status"] = "configured" if (
        result["parameter_overrides"] or value.get("service_acceptability_limits")
    ) else "default"
    return result


def time_chain_total(components):
    """保护链总时延：五个分量**全部**存在才算可判定（否则 ``None``）。"""

    total = 0.0
    for name in T_CHAIN_COMPONENTS:
        value = (components or {}).get(name)
        if value in (None, ""):
            return None
        total += _nonnegative(value, f"T_chain.{name}")
    return total


def protection_distance(d_safety_m, relative_speed_mps, t_chain_s, d_uncertainty_m):
    """保护走廊：``D_protection = D_safety + V_relative × T_chain + D_uncertainty``。

    三个输入中任何一个不可判定 ⇒ 返回 ``None``（调用方必须保持 unknown）。
    """

    if d_safety_m in (None, "") or relative_speed_mps in (None, "") or t_chain_s in (None, ""):
        return None
    if d_uncertainty_m in (None, ""):
        return None
    return (
        _nonnegative(d_safety_m, "D_safety_m")
        + _nonnegative(relative_speed_mps, "V_relative")
        * _nonnegative(t_chain_s, "T_chain")
        + _nonnegative(d_uncertainty_m, "D_uncertainty_m")
    )


def surveillance_acceptance(first_detection_distance_m, d_safety_m, relative_speed_mps, t_chain_s):
    """监视验收计算（逐项、可核查）。

    ``T_available = (first_detection_distance - D_safety) / V_relative``
    ``T_margin    = T_available - T_chain``

    ``margin >= 0`` ⇒ acceptable；``margin < 0`` ⇒ unacceptable；缺值 ⇒ unknown。
    """

    if (
        first_detection_distance_m in (None, "") or d_safety_m in (None, "")
        or relative_speed_mps in (None, "") or t_chain_s in (None, "")
    ):
        return {"status": "unknown", "t_available_s": None, "t_margin_s": None,
                "reason": "监视验收所需的输入不完整"}
    detection = _nonnegative(first_detection_distance_m, "first_detection_distance_m")
    safety = _nonnegative(d_safety_m, "D_safety_m")
    speed = _nonnegative(relative_speed_mps, "V_relative")
    chain = _nonnegative(t_chain_s, "T_chain")
    if speed <= 0:
        return {"status": "unknown", "t_available_s": None, "t_margin_s": None,
                "reason": "接近速度必须大于零"}
    available = (detection - safety) / speed
    margin = available - chain
    return {
        "status": "acceptable" if margin >= 0 else "unacceptable",
        "t_available_s": available,
        "t_margin_s": margin,
        "reason": (
            "保护链时间余量非负：监视可在进入安全边界前完成探测-响应"
            if margin >= 0
            else "保护链时间余量为负：监视来不及在进入安全边界前完成探测-响应"
        ),
    }


def continuous_gap_duration(gap_length_m, route_speed_mps):
    """连续 gap 的持续时间：``duration = along_route_gap_m / current_route_speed_mps``。"""

    if gap_length_m in (None, "") or route_speed_mps in (None, ""):
        return None
    speed = _nonnegative(route_speed_mps, "route_speed_mps")
    if speed <= 0:
        return None
    return _nonnegative(gap_length_m, "gap_length_m") / speed


def aggregate_acceptability(statuses) -> str:
    """把逐子系统结论聚合为路线 / 全局结论（**已知不可接受优先于未知**，fail-closed）。

    严重度：``unacceptable`` > ``unknown`` > ``acceptable_with_managed_gap`` >
    ``fully_satisfied``。``not_applicable`` 不参与聚合。

    为什么"已知不可接受"排在 ``unknown`` 之前：两者都**阻止**下游门禁
    （fail-closed 不变），但当系统已经**确认**某条服务链不可接受时，把它显示成
    "不可判定"会掩盖真实结论；如实显示"不可接受"并同时保留未知子系统清单，
    才能让用户看到真正需要处理的缺口。

    子系统词汇到全局词汇的映射由 :data:`SUBSYSTEM_TO_ACCEPTABILITY` 显式给出，
    因此"子系统 nominal"与"全局 fully_satisfied"不会被混成两套真值。
    """

    values = []
    for value in statuses:
        if value in (None, "not_applicable"):
            continue
        values.append(SUBSYSTEM_TO_ACCEPTABILITY.get(str(value), str(value)))
    if not values:
        return "not_applicable"
    if "unacceptable" in values:
        return "unacceptable"
    if "unknown" in values:
        return "unknown"
    if "acceptable_with_managed_gap" in values:
        return "acceptable_with_managed_gap"
    return "fully_satisfied"


def subsystem_status_from_events(*, events, outage_limit_s, degradation_limit_s) -> str:
    """C/S 子系统的连续事件判定（**已知超限优先于未知**，缺证据 fail-closed）。

    * 任何超过对应阈值的已确认事件 ⇒ ``unacceptable``；
    * 否则任何时长不可判定的事件 ⇒ ``unknown``；
    * 否则有事件在阈值内 ⇒ ``acceptable_with_managed_gap``；
    * 没有任何事件 ⇒ ``fully_satisfied``。
    """

    exceeded = False
    undecidable = False
    managed = False
    for event in events or []:
        kind = str(event.get("kind") or "")
        duration = event.get("duration_s")
        limit = {
            "service_outage": outage_limit_s,
            "redundancy_degradation": degradation_limit_s,
        }.get(kind)
        if limit is None:
            undecidable = True
            continue
        if duration in (None, ""):
            undecidable = True
            continue
        if float(duration) > float(limit):
            exceeded = True
        else:
            managed = True
    if exceeded:
        return "unacceptable"
    if undecidable:
        return "unknown"
    if managed:
        return "acceptable_with_managed_gap"
    return "fully_satisfied"


def subsystem_acceptability_counts(statuses) -> dict:
    """逐子系统结论的计数（供路线 / 全局结果如实披露**同时存在**的结论）。

    有了它，"路线结论＝unacceptable、同时仍有 1 个 unknown 子系统"不会被隐藏。
    """

    counts = {name: 0 for name in SUBSYSTEM_ACCEPTABILITY}
    for value in statuses or []:
        key = str(value or "unknown")
        if key in counts:
            counts[key] += 1
    return counts


def managed_gap_disclosure(entry: dict) -> list[str]:
    """``managed_gap`` 的**强制披露**行（service / 位置 / 长度 / 时长 / 阈值 / 缓解 / 依据）。

    绝不把 ``managed_gap`` 表述成"全覆盖"：每一行都明确写出缺口的存在与时长。
    """

    lines = []
    service = str(entry.get("service") or entry.get("subsystem") or "")
    kind = str(entry.get("kind") or "")
    kind_label = {
        "service_outage": "服务中断（全失联）",
        "redundancy_degradation": "冗余退化（链路仍可用但独立 provider 不足）",
    }.get(kind, kind)
    start = entry.get("start_offset_m")
    end = entry.get("end_offset_m")
    position = (
        f"航路 {entry.get('route_id')} 起算 {_fmt(start)}–{_fmt(end)} m"
        if start is not None and end is not None else "位置未定"
    )
    lines.append(
        f"[managed gap] service={service} 类型={kind_label} 位置={position} "
        f"连续长度={_fmt(entry.get('length_m'))} m "
        f"预计持续时间={_fmt(entry.get('duration_s'))} s "
        f"阈值={_fmt(entry.get('limit_s'))} s "
    )
    lines.append(
        f"  缓解措施（mitigation）：{entry.get('mitigation') or '未记录'}"
    )
    lines.append(
        f"  依据/假设（basis）：{entry.get('basis') or '未记录'}"
    )
    lines.append(
        "  披露语义：本段并非全覆盖；缺口真实存在，仅因连续时长在工程阈值内被接受为"
        "「有管理的缺口」（managed gap）。"
    )
    return lines


def default_operation_scenario() -> dict:
    """Round 2.5 的操作场景（数值字段全部是**工程假设**，绝不当成实测统计）。

    场景口径：``single_ownship=true``、``intruder_scope=other_uav_only``、
    ``cruise_altitude=ALT-100``、设计入侵者速度 20 m/s、标称接近速度 35 m/s
    （15 + 20）、保守接近速度 40 m/s。
    """

    return {
        "status": "configured",
        "semantics": "engineering_operation_scenario_not_regulatory_operational_volume",
        "single_ownship": True,
        "intruder_scope": "other_uav_only",
        "cruise_altitude": "ALT-100",
        "design_intruder_speed_mps": 20.0,
        "nominal_closing_speed_mps": 35.0,
        "conservative_closing_speed_mps": 40.0,
        "intruder_speed_source_type": "engineering_assumption",
        "intruder_speed_source": (
            "Round 2.5 工程假设：设计入侵者速度 20 m/s（不宣称实测交通统计）"
        ),
        "cruise_altitude_source": "项目运行高度层（ALT-100）",
        "metadata": {
            "single_ownship": "本场景只有一架本机（ownship）",
            "intruder_scope": "只考虑其他无人机（other_uav_only），不建模有人机或地面目标",
            "nominal_closing_speed_mps": "本机航路速度 + 设计入侵者速度（15 + 20 = 35 m/s）",
            "conservative_closing_speed_mps": "工程保守取值 40 m/s（高于标称接近速度）",
            "not_regulatory": True,
        },
    }


def normalize_operation_scenario(value=None) -> dict:
    """规范化操作场景（fail-closed：Round 2.5 只支持当前这一套口径）。"""

    result = default_operation_scenario()
    if value in (None, ""):
        return result
    if not isinstance(value, dict):
        raise ValueError("cns_operation_scenario 必须是对象")
    for key in (
        "single_ownship", "intruder_scope", "cruise_altitude",
        "design_intruder_speed_mps", "nominal_closing_speed_mps",
        "conservative_closing_speed_mps",
    ):
        if value.get(key) is not None:
            result[key] = value[key]
    if result["intruder_scope"] != "other_uav_only":
        raise ValueError("Round 2.5 仅支持 intruder_scope=other_uav_only")
    if str(result["cruise_altitude"]).upper() != "ALT-100":
        raise ValueError("Round 2.5 仅支持 cruise_altitude=ALT-100")
    if result["single_ownship"] is not True:
        raise ValueError("Round 2.5 仅支持 single_ownship=true")
    for key in (
        "design_intruder_speed_mps", "nominal_closing_speed_mps",
        "conservative_closing_speed_mps",
    ):
        number = float(result[key])
        if not isfinite(number) or number <= 0:
            raise ValueError(f"{key} 必须大于零")
        result[key] = number
    result["status"] = "configured"
    return result


def default_continuous_service_limits() -> dict:
    return {
        key: dict(value) for key, value in DEFAULT_SERVICE_ACCEPTABILITY_LIMITS.items()
    }


def empty_continuous_service_acceptability(status="not_calculated") -> dict:
    """新项目的 P17 容器（**未评估**；不预置任何结论）。"""

    return {
        "status": status,
        "algorithm_id": CONTINUOUS_SERVICE_ALGORITHM_ID,
        "algorithm_version": CONTINUOUS_SERVICE_ALGORITHM_VERSION,
        "model_scope": "continuous_service_acceptability_and_route_protection_corridor",
        "schema_version": CONTINUOUS_SERVICE_SCHEMA_VERSION,
        "input_fingerprint": None,
        "status_vocabulary": list(ACCEPTABILITY_STATUSES),
        "operation_scenario": None,
        "route_count": 0,
        "routes": [],
        "reasons": [],
        "reason_codes": [],
        "managed_gap_count": 0,
        "unacceptable_count": 0,
        "unknown_count": 0,
        "disclosure_lines": [],
        "not_evaluated": {
            name: "not_evaluated" for name in (
                "formal_continuity_probability", "common_cause", "shared_power",
                "shared_backhaul", "tower_failure", "runtime_outage",
                "regulatory_acceptability",
            )
        },
    }


def _fmt(value):
    if value in (None, ""):
        return "未定"
    number = float(value)
    return f"{number:.1f}"


def _nonnegative(value, field):
    number = float(value)
    if not isfinite(number) or number < 0:
        raise ValueError(f"{field} 必须是非负有限数值")
    return number


__all__ = [
    "ACCEPTABILITY_STATUSES", "BASELINE_SOURCES", "BASELINE_VALUES",
    "CONTINUOUS_EVENT_KINDS", "CONTINUOUS_SERVICE_ALGORITHM_ID",
    "CONTINUOUS_SERVICE_ALGORITHM_VERSION", "CONTINUOUS_SERVICE_REASONS",
    "CONTINUOUS_SERVICE_SCHEMA_VERSION", "DEFAULT_SERVICE_ACCEPTABILITY_LIMITS",
    "NAVIGATION_STATES", "OPERATION_SCENARIO_DEFAULTS", "SUBSYSTEM_ACCEPTABILITY",
    "SUBSYSTEM_TO_ACCEPTABILITY", "T_CHAIN_COMPONENTS",
    "aggregate_acceptability", "continuous_gap_duration",
    "default_continuous_service_limits", "default_continuous_service_policy",
    "default_operation_scenario", "empty_continuous_service_acceptability",
    "managed_gap_disclosure", "normalize_continuous_service_policy",
    "normalize_operation_scenario", "parameter_baseline", "protection_distance",
    "subsystem_status_from_events", "surveillance_acceptance", "time_chain_total",
    "subsystem_acceptability_counts",
]
