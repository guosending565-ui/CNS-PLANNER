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

from copy import deepcopy
from math import isfinite

#: 契约版本与算法标识。
CONTINUOUS_SERVICE_SCHEMA_VERSION = "round2.6-post-plan-continuous-service-acceptability"
CONTINUOUS_SERVICE_ALGORITHM_ID = "continuous_service_acceptability_v1"
CONTINUOUS_SERVICE_ALGORITHM_VERSION = "2.1"

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
            "Round 2.6（用户裁定）：**不再提供内置数值**。FC30 的 `遥控信号丢失超过 3 s "
            "触发 Failsafe RTH` 只是**设备 failsafe 事实**，不得自动成为本项目的规划阈值。"
        ),
        "statement": (
            "最大允许完全通信中断时间必须由用户显式登记（engineering_assumption）；"
            "未登记时保持 evidence_required / unknown，绝不采用 3 s。"
        ),
        "report_disclosure": (
            "本阈值必须由用户确认并登记为工程规划假设；设备 failsafe 门限（3 s）不等于"
            "本项目的规划阈值。未登记时通信判定 fail-closed。"
        ),
        "basis": "evidence_required_by_user_decision_round2_6",
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
        "source": (
            "Round 2.5 v1 engineering baseline：安全边界取 0 m（即保护走廊内边界＝航路中心线）。"
            "Round 2.6 起本参数被 **D_separation_m** 取代（见该条目）：它只在读取旧项目时作为"
            "兼容别名继续被识别，自身不再提供内置数值。"
        ),
        "statement": "D_safety 是 Round 2.5 的旧名；Round 2.6 的正式参数是 D_separation。",
        "report_disclosure": "本参数已由 D_separation 取代，仅为旧项目兼容而保留。",
        "basis": "round2_5_legacy_alias_of_d_separation",
    },
    "D_separation_m": {
        "source": (
            "Round 2.6：分隔距离（D_separation）属于**必须由用户 / 工程依据提供的输入**。"
            "系统**不再**默认它为 0 m——静默的 0 会凭空把保护走廊收窄到航路中心线，"
            "把'没有依据'显示成'已经算过'。"
        ),
        "statement": "D_separation 未提供显式工程依据时保持 evidence_required（不得静默取 0）。",
        "report_disclosure": (
            "分隔距离必须有显式工程依据；未提供时保护走廊与监视验收均如实保持 unknown"
            "（fail-closed），不得放行。"
        ),
        "basis": "evidence_required",
    },
    "D_maneuver_m": {
        "source": (
            "Round 2.6 工程基线（engineering_baseline）：机动附加距离固定取 50 m。"
            "它**不是**法规值，也**不是** FC30 的普遍制动距离事实；本轮不深入研究机动模型，"
            "只保留算法接口 ``maneuver_distance_m``，该固定值可被显式工程依据替换。"
        ),
        "statement": "D_maneuver＝50 m（engineering_baseline，可被显式工程依据替换）。",
        "report_disclosure": (
            "机动附加距离为工程基线固定值（50 m），不代表法规要求，也不代表该机型的"
            "真实制动距离；后续可按机型/场景替换。"
        ),
        "basis": "round2_6_engineering_baseline_maneuver_distance",
    },
    "D_uncertainty_m": {
        "source": (
            "Round 2.6：不确定度附加量属于**必须由用户 / 工程依据提供的输入**。"
            "系统**不再**默认它为 0 m（静默 0 会把'未知不确定度'显示成'零不确定度'）。"
        ),
        "statement": "D_uncertainty 未提供显式工程依据时保持 evidence_required（不得静默取 0）。",
        "report_disclosure": (
            "不确定度距离必须有显式工程依据；未提供时保护走廊与监视验收均如实保持 unknown"
            "（fail-closed）。"
        ),
        "basis": "evidence_required",
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
#:
#: Round 2.6 变化（用户裁定）：
#:
#: * ``c_full_outage_max_s`` 由"内置 3 s（取自 FC30 failsafe 事实）"改为 **``None``
#:   （evidence_required）**：FC30 的 ``遥控信号丢失 > 3 s → Failsafe RTH`` 只作为
#:   **设备 failsafe 事实**保存，**不再自动**成为本项目的规划阈值。用户没有确认
#:   "最大允许完全通信中断时间"时，通信判定必须保持 ``evidence_required`` / ``unknown``；
#: * ``c_redundancy_degradation_max_s`` 保持内置 10 s（它与设备 failsafe 事实无关）；
#: * ``D_separation_m`` / ``D_uncertainty_m`` 同样改为 ``None``（evidence_required）：
#:   没有用户 / 工程依据时如实 ``unknown``，绝不用 0 假装"已经算过"；
#: * 新增 ``D_maneuver_m = 50.0``，身份是 **engineering_baseline**（不是法规值，
#:   也不是 FC30 普遍制动距离事实），只提供算法接口 ``maneuver_distance_m``；
#: * ``D_safety_m`` 降级为 **兼容别名**（旧项目里可能仍以该名记录证据）。
BASELINE_VALUES = {
    "c_full_outage_max_s": None,
    "c_redundancy_degradation_max_s": 10.0,
    "T_chain_detect_track_s": 3.0,
    "T_chain_sensor_to_platform_s": 1.0,
    "T_chain_platform_processing_s": 2.0,
    "T_chain_platform_to_aircraft_s": 1.0,
    "T_chain_aircraft_response_manoeuvre_s": 3.0,
    "D_safety_m": None,
    "D_separation_m": None,
    "D_maneuver_m": 50.0,
    "D_uncertainty_m": None,
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

#: Round 2.5 → Round 2.6 的**参数改名**（同一物理量，旧名作为兼容别名）。
#:
#: ``D_safety_m`` 是 Round 2.5 的名字；Round 2.6 的正式名字是 ``D_separation_m``。
#: 两者**不合并数值**：正式参数只读新名；旧名只在"新名没有显式记录"时作为回退。
PARAMETER_ALIASES = {
    "D_separation_m": ("D_safety_m",),
}

#: Route Protection 的四个分量（缺任何一个 ⇒ D_protection 不可判定）。
#:
#: ``D_protection = D_separation + V_relative × T_chain + D_maneuver + D_uncertainty``
PROTECTION_COMPONENTS = (
    "separation", "chain", "maneuver", "uncertainty",
)

#: Round 2.6 的正式公式字符串（前端 / 报告逐字显示，与算法同源）。
ROUTE_PROTECTION_FORMULA = (
    "D_protection = D_separation + V_relative * T_chain + D_maneuver + D_uncertainty"
)

#: ``D_maneuver`` 的工程基线固定值（m）；身份是 engineering_baseline，不是法规值。
D_MANEUVER_BASELINE_M = 50.0

#: 监视威胁分层（Round 2.6 用户裁定）：两者**分开**评估，绝不合并成单一结论。
#:
#: * ``cooperative``     —— 主要威胁；主要服务 = RID 合作监视；
#: * ``noncooperative``  —— 补充威胁；补充服务 = Radar 非合作监视。
THREAT_LAYERS = ("cooperative", "noncooperative")

PRIMARY_THREAT_LAYER = "cooperative"
SUPPLEMENTARY_THREAT_LAYER = "noncooperative"

#: 威胁分层 → 中文标签（前端 / 报告共用，避免两套说法）。
THREAT_LAYER_LABELS = {
    "cooperative": "合作无人机 / RID 合作监视（主要威胁）",
    "noncooperative": "非合作无人机 / Radar 非合作监视（补充威胁）",
}

#: 分层状态词汇（与四层可接受性词汇**不同**：它描述的是"该分层自己的监视链结论"）。
THREAT_LAYER_STATUSES = (
    "satisfied",
    "acceptable_with_managed_gap",
    "limitation",
    "unacceptable",
    "unknown",
    "not_applicable",
)

#: Radar 布局不可行时的**固定披露文本**（Round 2.6 用户裁定，逐字保留）。
NONCOOPERATIVE_LIMITATION_DISCLOSURE = (
    "当前方案对合作无人机的监视链满足当前规划要求；非合作无人机补充监视能力因 Radar "
    "布局不可行尚未闭合，属于当前方案能力限制。"
)

#: 能力限制（limitation）不是系统错误，也不是 primary threat 的不通过。
LIMITATION_SEMANTICS = (
    "supplementary_capability_limitation_does_not_change_primary_threat_verdict"
)

#: 判定的两个层次：baseline（当前 ExistingCNS）与 post_plan（实施 P16 方案后的投影态）。
PLAN_STAGES = ("baseline", "post_plan")

#: 默认的 C 服务门限（**服务中断**与**冗余退化**各自独立）。
#:
#: Round 2.6：``C.service_outage`` 为 ``None`` —— 它**必须**由用户以
#: engineering_assumption 显式登记（见 :data:`BASELINE_VALUES` 的说明）；
#: 未登记时评估器 fail-closed，绝不采用设备 failsafe 的 3 s。
#: ``S`` 侧保持 Round 2.5 的内置工程基线（监视服务的连续中断口径与设备 failsafe 无关）。
DEFAULT_SERVICE_ACCEPTABILITY_LIMITS = {
    "C": {"service_outage": None, "redundancy_degradation": 10.0},
    "S": {"service_outage": 3.0, "redundancy_degradation": 10.0},
}

#: 连续服务事件种类 → 对应阈值参数（Round 2.8：P16 与 P17 共用**同一份**映射）。
#:
#: P15 的 ``continuous_deficit_segments`` 的 ``causes`` 与 P17 的事件 ``kind`` 是同一
#: 物理量的两个词表（``service_deficit`` ⇄ ``service_outage``、
#: ``redundancy_deficit`` ⇄ ``redundancy_degradation``）。P16 的连续服务停止条件必须
#: 用 **service_outage** 那一档，否则会把"冗余退化"当成"全失联"判定。
CONTINUOUS_SERVICE_KIND_PARAMETER = {
    "service_outage": "c_full_outage_max_s",
    "redundancy_degradation": "c_redundancy_degradation_max_s",
}

#: P15 缺口原因 → P17 事件种类（同一物理量的两个词表，唯一映射）。
DEFICIT_CAUSE_TO_EVENT_KIND = {
    "service_deficit": "service_outage",
    "redundancy_deficit": "redundancy_degradation",
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
    "no_outage_threshold_evidence": (
        "最大允许完全通信中断时间缺少用户显式登记：设备 failsafe 门限（FC30 为 3 s）"
        "不等于本项目的规划阈值，用户未确认前该阈值不可判定（evidence_required）"
    ),
    "no_p13_geometry": "P14 未提供航路几何，保护走廊无法构造",
    "no_time_chain": "保护链时延分量不完整，T_chain 不可判定",
    "protection_distance_evidence_required": (
        "保护走廊的分隔距离 / 不确定度距离缺少显式工程依据（D_separation / "
        "D_uncertainty 必须由用户或工程依据提供，系统不再静默按 0 计算）"
    ),
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


def protection_distance(
    d_separation_m, relative_speed_mps, t_chain_s, d_uncertainty_m,
    d_maneuver_m=0.0,
):
    """保护走廊（Round 2.6 公式）：

    ``D_protection = D_separation + V_relative × T_chain + D_maneuver + D_uncertainty``

    输入中任何一个不可判定 ⇒ 返回 ``None``（调用方**必须**保持 ``unknown``）。

    ``d_maneuver_m`` 默认 ``0.0`` 只为"旧调用点/纯公式测试"保留位置参数兼容；
    **生产路径**（``ContinuousServiceAcceptabilityV1``）一律显式传入
    ``D_maneuver_m`` 参数解析结果（工程基线 50 m，或用户的显式工程依据）。
    """

    if d_separation_m in (None, "") or relative_speed_mps in (None, "") or t_chain_s in (None, ""):
        return None
    if d_uncertainty_m in (None, "") or d_maneuver_m in (None, ""):
        return None
    return (
        _nonnegative(d_separation_m, "D_separation_m")
        + _nonnegative(relative_speed_mps, "V_relative")
        * _nonnegative(t_chain_s, "T_chain")
        + _nonnegative(d_maneuver_m, "D_maneuver_m")
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
    """新项目的 P17 容器（**未评估**；不预置任何结论）。

    Round 2.6 新增两个**显式分层**字段：

    * ``baseline`` —— 当前权威 ExistingCNS 下的结论（与原行为一致）；
    * ``post_plan_projection`` —— 实施 P16 ``selected_actions`` 之后**投影态**的结论。

    顶层 ``status`` 在有投影时跟随 ``post_plan_projection.status``（Step6 依据该结论）；
    没有投影时等于 ``baseline`` 的结论。
    """

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
        #: Round 2.6：两层结论与投影态。
        "baseline_status": status,
        "post_plan_status": None,
        "baseline": None,
        "post_plan_projection": None,
        "primary_threat_status": None,
        "supplementary_threat_status": None,
        "limitations": [],
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


# ---------------------------------------------------------------------------
# Round 2.6：监视威胁分层 + 分层结论
# ---------------------------------------------------------------------------


def _service_key_of(item) -> str:
    return str((item or {}).get("service") or (item or {}).get("service_key") or "")


def classify_surveillance_threat_layer(service_key: str) -> str:
    """把一条监视 service 归入威胁分层。

    * 只要包含 ``radar`` / ``noncooperative`` 关键字 → ``noncooperative``
      （非合作无人机 / Radar 非合作监视，补充威胁）；
    * 其余（``S:rid_*``、``S:airborne_*`` 等）→ ``cooperative``
      （合作无人机 / RID 合作监视，主要威胁）。

    Round 2.5 的 ``_primary_service_key`` 可能把多个 service_key 拼成
    ``"A、B"``；这里按分隔符拆分后逐项判定，**全部**为 radar 才算非合作分层，
    避免"同一条 subsystem 里既有 RID 又有 Radar"被误判成单一分层。
    """

    keys = [
        part.strip() for part in str(service_key or "").replace("、", "\n").splitlines()
        if part.strip()
    ] or [str(service_key or "")]
    for key in keys:
        lowered = key.lower()
        if "radar" not in lowered and "noncooperative" not in lowered and "non_cooperative" not in lowered:
            return PRIMARY_THREAT_LAYER
    return SUPPLEMENTARY_THREAT_LAYER


def classify_surveillance_service_keys(service_key: str) -> dict:
    """把一条（可能由 ``、`` 拼接的）service_key 串**逐项**分到两个威胁分层。"""

    keys = [
        part.strip() for part in str(service_key or "").replace("、", "\n").splitlines()
        if part.strip()
    ]
    return {
        name: sorted({
            key for key in keys
            if classify_surveillance_threat_layer(key) == name
        })
        for name in THREAT_LAYERS
    }


def surveillance_threat_layers(route_result) -> dict:
    """逐 route 的监视威胁分层明细（**分开**给出，绝不合并）。"""

    layers = {
        name: {
            "layer": name,
            "label": THREAT_LAYER_LABELS[name],
            "service_keys": [],
            "subsystems": [],
            "status": "not_applicable",
            "t_margin_s": None,
            "first_detection_distance_m": None,
            "limitations": [],
            "reasons": [],
        }
        for name in THREAT_LAYERS
    }
    for subsystem in (route_result or {}).get("subsystems") or []:
        if str(subsystem.get("subsystem") or "") != "S":
            continue
        key = _service_key_of(subsystem)
        by_layer = classify_surveillance_service_keys(key)
        #: 同一条 subsystem 同时涉及 RID 与 Radar 时，**两层都要登记**（绝不把复合串
        #: 当成单一分层），但"结论"只归入按该串判定的主分层，避免同一条子系统被计两次。
        layer_name = classify_surveillance_threat_layer(key)
        acceptance = subsystem.get("acceptance") or {}
        detection = subsystem.get("first_detection_evidence") or {}
        entry = layers[layer_name]
        for name, keys in by_layer.items():
            for item in keys:
                if item not in layers[name]["service_keys"]:
                    layers[name]["service_keys"].append(item)
        entry["subsystems"].append(subsystem.get("status"))
        #: 同一分层里出现多条 service 时，按严重度取最差（fail-closed）。
        candidate = subsystem.get("status")
        entry["status"] = _worst_layer_status(entry["status"], candidate)
        margin = acceptance.get("t_margin_s")
        if margin is not None:
            entry["t_margin_s"] = (
                margin if entry["t_margin_s"] is None else min(entry["t_margin_s"], margin)
            )
        distance = detection.get("first_detection_distance_m")
        if detection.get("usable") is True and distance is not None:
            entry["first_detection_distance_m"] = (
                distance if entry["first_detection_distance_m"] is None
                else max(entry["first_detection_distance_m"], distance)
            )
        for reason in subsystem.get("reasons") or []:
            if reason and reason not in entry["reasons"]:
                entry["reasons"].append(reason)
    for name, entry in layers.items():
        entry["service_keys"] = sorted(entry["service_keys"])
    return layers


#: 分层内部状态 → 分层结论的严重度顺序（后者覆盖前者）。
_LAYER_SEVERITY = (
    "not_applicable", "satisfied", "acceptable_with_managed_gap",
    "limitation", "unknown", "unacceptable",
)


def _worst_layer_status(current, candidate) -> str:
    current = str(current or "not_applicable")
    candidate = str(candidate or "not_applicable")
    rank = {name: index for index, name in enumerate(_LAYER_SEVERITY)}
    if rank.get(candidate, 0) >= rank.get(current, 0):
        return candidate
    return current


def layer_conclusion_status(layer_entry) -> str:
    """把某个威胁分层的子系统状态映射为**分层结论**。

    注意：``limitation`` 只能由调用方（Radar 布局不可行）显式置入，**绝不**由
    "没有证据"自动推出——缺证据是 ``unknown``。
    """

    status = str((layer_entry or {}).get("status") or "not_applicable")
    return {
        "nominal": "satisfied",
        "acceptable_degraded": "acceptable_with_managed_gap",
        "acceptable_with_managed_gap": "acceptable_with_managed_gap",
        "unacceptable": "unacceptable",
        "unknown": "unknown",
        "not_applicable": "not_applicable",
        "satisfied": "satisfied",
        "limitation": "limitation",
    }.get(status, "unknown")


#: 只有**已证明的物理/优化不可行**才允许登记为非合作监视能力限制；
#: 以下状态一律**保持** unknown / evidence-required，绝不升级为 limitation。
NONCOOPERATIVE_LIMITATION_NEVER_UPGRADED_STATUSES = (
    "search_incomplete",
    "refinement_incomplete",
    "unresolved",
    "solver_error",
    "solver_unavailable",
    "not_ready",
    "stale",
    "missing",
    "proposal_ready",
    "passed",
)


def radar_layout_item_for_route(radar_layout, route_id):
    """在 canonical Radar **collection** 中按 ``route_id`` 精确取出该航路的 item。

    ``radar_surveillance_layout`` 的顶层 ``status`` 只描述整批评估
    （例如 ``pending_confirmation``）；真正的逐航路 canonical 裁决在 ``items[]``。
    查不到对应航路时返回 ``None`` —— 调用方必须 fail-closed，绝不拿别的航路的结论顶替。
    """

    target = str(route_id or "")
    if not target:
        return None
    for item in (radar_layout or {}).get("items") or []:
        if isinstance(item, dict) and str(item.get("route_id") or "") == target:
            return item
    return None


def proven_managed_physical_radar_gap(item) -> bool:
    """该 Radar item 是否是**已证明的 managed physical gap**（四条必须同时成立）。

    1. ``status == "infeasible"``；
    2. ``gap_classification == "confirmed_gap"``；
    3. ``managed_physical_gap is True``；
    4. ``solver.infeasibility_proven is True``。

    缺少任何一条（含 search/refinement 未完成、无证明、managed 标志缺失）都不是
    能力限制，而是未知或未闭合。
    """

    if not isinstance(item, dict):
        return False
    if str(item.get("status") or "") != "infeasible":
        return False
    if str(item.get("gap_classification") or "") != "confirmed_gap":
        return False
    if item.get("managed_physical_gap") is not True:
        return False
    solver = item.get("solver") if isinstance(item.get("solver"), dict) else {}
    return solver.get("infeasibility_proven") is True


def _noncooperative_limitation(*, route_id, item, source_status=None) -> dict:
    """构造一条非合作监视能力限制（唯一实现，文案复用既有常量）。"""

    item = item if isinstance(item, dict) else {}
    solver = item.get("solver") if isinstance(item.get("solver"), dict) else {}
    resolved_status = str(source_status or item.get("status") or "")
    resolved_solver = str(solver.get("status") or item.get("solver_status") or "")
    return {
        "limitation_id": "noncooperative_surveillance_limitation",
        "route_id": route_id,
        "layer": SUPPLEMENTARY_THREAT_LAYER,
        "capability": "Radar 非合作监视（补充威胁分层）",
        "status": "limitation",
        "blocking_primary_threat": False,
        "semantics": LIMITATION_SEMANTICS,
        "disclosure": NONCOOPERATIVE_LIMITATION_DISCLOSURE,
        "must_disclose_in_report": True,
        "no_relaxation_applied": (
            "本轮**没有**为了得到方案而扩大覆盖半径、改动 90° 面板或使用假塔；"
            "求解不可行是真实工程结论。"
        ),
        #: Round 29-Q：canonical 证明来源（逐项转印，绝不改写上游判定）。
        "source": {
            "algorithm_id": item.get("algorithm_id"),
            "algorithm_version": item.get("algorithm_version"),
            "source_status": resolved_status or None,
            "solver_status": resolved_solver or None,
            "infeasibility_proven": solver.get("infeasibility_proven") is True,
            "gap_reason": item.get("gap_reason"),
            "gap_classification": item.get("gap_classification"),
            "managed_physical_gap": item.get("managed_physical_gap") is True,
        },
        #: 兼容字段（Round 2.6 起的既有消费者）：与 ``source`` 同源，不是第二个真值。
        "source_status": resolved_status or None,
        "solver_status": resolved_solver or None,
    }


def noncooperative_limitations(radar_layout, route_id=None) -> list[dict]:
    """从 Radar layout 结论推导**非合作监视能力限制**（不是系统错误）。

    Round 29-Q（route-aware 收口）：``radar_surveillance_layout`` 是 **collection**，
    顶层 ``status`` 只是整批评估状态（权威项目实测为 ``pending_confirmation``），
    真正的 canonical 逐航路裁决位于 ``items[]``。因此：

    * 给定 ``route_id`` ⇒ **只**读取该 route 自己的 canonical item。查不到、或不满足
      「已证明的 managed physical gap」四条 ⇒ 返回 ``[]``（fail-closed）。绝不因为
      collection 里别处存在某个历史 ``infeasible`` 就给当前 route 登记限制。
    * 未给定 ``route_id`` ⇒ 保持 Round 2.6 的 collection 级语义（只看顶层
      ``status`` / ``solver.status`` / ``solver_status``），用于既有调用点与回归测试。

    证据缺失（``not_calculated`` / ``missing_data``）与"未闭合"
    （``search_incomplete`` / ``refinement_incomplete`` / ``unresolved`` /
    ``solver_error`` / ``solver_unavailable`` / ``not_ready`` / ``stale`` /
    没有 infeasibility proof）都不是"能力限制"，而是 ``unknown``
    —— 前者是没有结论，后者是没有证明。
    """

    if route_id is not None:
        item = radar_layout_item_for_route(radar_layout, route_id)
        if not proven_managed_physical_radar_gap(item):
            return []
        return [_noncooperative_limitation(route_id=str(route_id), item=item)]

    layout = radar_layout or {}
    status = str(layout.get("status") or "")
    solver_status = str(
        (layout.get("solver") or {}).get("status")
        or layout.get("solver_status")
        or ""
    )
    if status not in ("infeasible", "failed") and solver_status not in ("infeasible", "failed"):
        return []
    return [_noncooperative_limitation(
        route_id=None, item=layout, source_status=status or solver_status,
    )]


def limitation_disclosure_lines(limitations) -> list[str]:
    """能力限制的**强制披露**行（逐字保留用户裁定的表述）。"""

    lines = []
    for item in limitations or []:
        lines.append(f"[能力限制] {item.get('capability') or item.get('layer')}")
        lines.append(f"  {item.get('disclosure') or NONCOOPERATIVE_LIMITATION_DISCLOSURE}")
        lines.append(
            "  披露语义：本限制**不改变**主要威胁（合作无人机 / RID）的判定，"
            "但报告与方案评审必须同时显示；不得把本方案表述为全覆盖或已满足。"
        )
    return lines


# ---------------------------------------------------------------------------
# Round 2.6：post-plan 投影态的比较（baseline vs post_plan）
# ---------------------------------------------------------------------------


def _service_delta(assessment) -> dict:
    """把一份评估结果压缩成 ``{(route_id, subsystem, service): 最长事件}``。"""

    result = {}
    for route in (assessment or {}).get("routes") or []:
        route_id = str(route.get("route_id") or "")
        for subsystem in route.get("subsystems") or []:
            code = str(subsystem.get("subsystem") or "")
            key = f"{route_id}|{code}|{_service_key_of(subsystem) or code}"
            event = subsystem.get("longest_event") or {}
            result[key] = {
                "route_id": route_id,
                "subsystem": code,
                "service": _service_key_of(subsystem) or code,
                "status": subsystem.get("status"),
                "kind": event.get("kind"),
                "length_m": event.get("length_m"),
                "duration_s": event.get("duration_s"),
                "limit_s": event.get("limit_s"),
                "exceeds_limit": event.get("exceeds_limit"),
                "declared_improvement_m": None,
            }
    return result


def declared_improvement_index(assessment) -> dict:
    """P15 的 ``declared_improvements`` → ``{(route_id, subsystem): 声明改善量}``。"""

    result = {}
    for route in (assessment or {}).get("routes") or []:
        route_id = str(route.get("route_id") or "")
        for item in route.get("declared_improvements") or []:
            entry = result.setdefault(
                (route_id, str(item.get("subsystem") or "")), {}
            )
            for key in ("declared_max_continuous_deficit_reduction_m", "declared_reduction_m"):
                if item.get(key) is not None:
                    entry[key] = item[key]
    return result


def compare_plan_stages(baseline_assessment, post_plan_assessment, projection) -> dict:
    """**baseline vs post_plan** 的逐 service 比较（改进项 + 剩余缺口）。

    改进（improved）的判据必须**可核查**，因此只承认两种：

    * ``confirmed_gap_resolved``：该 service 在 baseline 有连续缺口事件，而投影态**没有**
      任何缺口事件（长度与持续时间都归零）；
    * ``declared_improvement``：该 service 在 baseline 有缺口，投影态仍有缺口，但 P15 的
      ``declared_improvements`` **显式声明**了缓解量（按声明给出，不自行推导）。

    只有"事件消失但没有任何声明"的情况才记为 ``unexplained_disappearance`` —— 它仍然
    是改进，但必须如实标注"缺少声明依据"，绝不静默当成正常。
    """

    baseline_index = _service_delta(baseline_assessment)
    post_index = _service_delta(post_plan_assessment)
    declarations = declared_improvement_index(post_plan_assessment)
    improved, remaining = [], []
    for key in sorted(set(baseline_index) | set(post_index)):
        before = baseline_index.get(key)
        after = post_index.get(key)
        reference = before or after
        route_id = reference["route_id"]
        subsystem = reference["subsystem"]
        service = reference["service"]
        declared = declarations.get((route_id, subsystem)) or {}
        declared_m = (
            declared.get("declared_max_continuous_deficit_reduction_m")
            if declared.get("declared_max_continuous_deficit_reduction_m") is not None
            else declared.get("declared_reduction_m")
        )
        before_event = bool(before) and before.get("length_m") not in (None, 0)
        after_event = bool(after) and after.get("length_m") not in (None, 0)
        if after is not None:
            after = {**after, "declared_improvement_m": declared_m}
        if before_event and not after_event:
            improved.append({
                **(after or before),
                "improvement_kind": (
                    "declared_improvement" if declared_m is not None
                    else "confirmed_gap_resolved"
                ),
                "declared_improvement_m": declared_m,
                "baseline_length_m": before.get("length_m"),
                "baseline_duration_s": before.get("duration_s"),
                "reduction_m": before.get("length_m"),
                "semantics": (
                    "p15_declared_reduction" if declared_m is not None
                    else "gap_event_absent_in_post_plan_state"
                ),
            })
        elif after_event:
            remaining.append({
                **after,
                "improvement_kind": (
                    "declared_partial_improvement" if declared_m is not None
                    else "no_declared_improvement"
                ),
                "declared_improvement_m": declared_m,
                "baseline_length_m": (before or {}).get("length_m"),
                "baseline_duration_s": (before or {}).get("duration_s"),
                "semantics": "gap_event_still_present_in_post_plan_state",
            })
    return {
        "improved_service_count": len(improved),
        "remaining_gap_count": len(remaining),
        "improved_services": improved,
        "remaining_gaps": remaining,
        "projection": deepcopy(projection) if projection is not None else None,
        "comparison_basis": "longest_continuous_event_per_route_subsystem_service",
    }


__all__ = [
    "ACCEPTABILITY_STATUSES", "BASELINE_SOURCES", "BASELINE_VALUES",
    "CONTINUOUS_EVENT_KINDS", "CONTINUOUS_SERVICE_ALGORITHM_ID",
    "CONTINUOUS_SERVICE_ALGORITHM_VERSION", "CONTINUOUS_SERVICE_KIND_PARAMETER",
    "CONTINUOUS_SERVICE_REASONS",
    "CONTINUOUS_SERVICE_SCHEMA_VERSION", "DEFAULT_SERVICE_ACCEPTABILITY_LIMITS",
    "DEFICIT_CAUSE_TO_EVENT_KIND",
    "D_MANEUVER_BASELINE_M", "LIMITATION_SEMANTICS", "NAVIGATION_STATES",
    "NONCOOPERATIVE_LIMITATION_DISCLOSURE", "OPERATION_SCENARIO_DEFAULTS",
    "PARAMETER_ALIASES", "PLAN_STAGES", "PRIMARY_THREAT_LAYER",
    "PROTECTION_COMPONENTS", "ROUTE_PROTECTION_FORMULA", "SUBSYSTEM_ACCEPTABILITY",
    "SUBSYSTEM_TO_ACCEPTABILITY", "SUPPLEMENTARY_THREAT_LAYER", "T_CHAIN_COMPONENTS",
    "THREAT_LAYER_LABELS", "THREAT_LAYER_STATUSES", "THREAT_LAYERS",
    "aggregate_acceptability", "classify_surveillance_service_keys",
    "classify_surveillance_threat_layer",
    "compare_plan_stages", "continuous_gap_duration",
    "declared_improvement_index", "default_continuous_service_limits",
    "default_continuous_service_policy", "default_operation_scenario",
    "empty_continuous_service_acceptability", "layer_conclusion_status",
    "limitation_disclosure_lines", "managed_gap_disclosure",
    "NONCOOPERATIVE_LIMITATION_NEVER_UPGRADED_STATUSES",
    "noncooperative_limitations", "normalize_continuous_service_policy",
    "normalize_operation_scenario", "parameter_baseline", "protection_distance",
    "proven_managed_physical_radar_gap", "radar_layout_item_for_route",
    "subsystem_status_from_events", "surveillance_acceptance",
    "surveillance_threat_layers", "time_chain_total",
    "subsystem_acceptability_counts",
]
