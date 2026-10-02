"""Round 2.4 / 2.5 —— **工程证据 / 规划假设** 的正式输入契约（纯 domain，无 I/O）。

背景（本轮要解决的真实产品缺口）
--------------------------------

Round 2.3 为止，一旦主链缺一条工程证据（例如"地面通信设备的网络归属范围"或
"机载是否具备网络远程识别参与能力"），唯一的处理方式是**让开发者改数据 / JSON**。
这不满足"人工可使用系统"。本模块把这类缺口变成一等公民：

* **事实与假设严格分开**：每条记录必须显式声明
  :data:`EVIDENCE_SOURCE_TYPES` 之一（Round 2.5 起共四种，新增
  ``external_reference`` 用于外部研究参考值），且 `engineering_assumption` 必须携带
  ``statement`` / ``source`` / ``reason`` / ``confirmed_by_user`` /
  ``report_disclosure``；
* **绝不写入 device catalog**：工程假设不是厂家设备事实，因此它落在**独立的**
  ``project_state["planning_evidence"]`` 里，并由
  :func:`apply_aircraft_evidence` 在**规划消费点**叠加到机载能力上
  （叠加结果必须带 ``airborne_evidence`` 来源标注供下游披露）；
* **可审计**：每条记录带 ``source`` / ``source_type`` / ``confirmed`` /
  ``confirmation_status`` / ``provenance`` / ``report_disclosure``；
* **绝不按 device_id 硬编码**：``field`` 是唯一路由键，值域由
  :data:`PLANNING_EVIDENCE_FIELDS` **枚举校验**（非法值直接 ``ValueError``）。

Round 2.5 扩展（**additive**）
------------------------------

* 新增 ``kind = continuous_service_parameter`` 的数值/枚举参数域（C 全失联与冗余
  降级阈值、保护走廊链时延分量、``D_safety`` / ``D_uncertainty``、入侵者与接近速度、
  导航 RTK/GNSS 证据）。它们的**唯一**消费入口是
  :func:`resolve_continuous_parameter`：显式记录优先，缺失时回落到
  :mod:`cns_planner.domain.cns_continuous_service` 的内置工程基线，并在结果里把
  权威来源标注为 ``builtin_engineering_assumption`` —— **绝不当成法规事实**。
* ``external_reference`` 允许参与判定，但权威效应是
  ``allowed_as_external_reference``，披露行必须写明"外部研究参考"。
* ``schema_version`` 常量保持不变：旧项目保存的 registry 会被规范化到同一形状，
  不做数据迁移，也不需要重开时补齐字段。

与既有 ``assumptions``（Phase4-B1）的关系：这是**另一个**、更窄的容器，专门承载
"参与 CNS 判定的工程证据"，因此它有自己的 field 注册表与披露语义；
``assumptions`` 继续服务 workflow readiness 等既有用途，两者互不覆盖。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone


PLANNING_EVIDENCE_SCHEMA_VERSION = "round2.4-engineering-evidence"

#: 来源类型 —— **事实与假设必须严格分开**。
#:
#: * ``confirmed_source_fact``：有正式资料支持（本模块只登记，绝不代填）；
#: * ``external_reference``（Round 2.5 新增）：**外部研究 / 公开资料**的参考值。
#:   它既不是本项目确认的正式资料，也不是本项目的工程假设；参与判定时**必须**在
#:   报告里与"法规 / 厂家事实"逐字区分（例如 RTK 恢复 9–13 s 的外部研究参考）；
#: * ``engineering_assumption``：为规划目的采用的工程假设，**必须**带
#:   ``statement`` / ``source`` / ``reason`` / ``confirmed_by_user`` /
#:   ``report_disclosure``；
#: * ``unknown``：尚无依据（保持 unknown，绝不升级为事实或假设）。
EVIDENCE_SOURCE_TYPES = (
    "confirmed_source_fact",
    "external_reference",
    "engineering_assumption",
    "unknown",
)

#: 来源类型的权威效应：只有它决定该记录**是否被允许参与判定**。
#:
#: ``unknown`` 永不参与判定（它的作用是如实登记"这里确实没有依据"）；
#: ``external_reference`` 允许参与，但带**外部参考**披露语义。
EVIDENCE_AUTHORITY_EFFECT = {
    "confirmed_source_fact": "allowed_with_disclosure",
    "external_reference": "allowed_as_external_reference",
    "engineering_assumption": "allowed_with_disclosure",
    "unknown": "not_participating",
}

#: 允许参与判定的权威效应（``unknown`` 是唯一不参与的）。
PARTICIPATING_AUTHORITY_EFFECTS = (
    "allowed_with_disclosure", "allowed_as_external_reference",
)

PROJECT_EVIDENCE_SCOPES = ("project", "aircraft")

#: 子系统证据的合法取值（枚举校验，绝不是自由文本）。
COMMUNICATION_AIRBORNE_INTERFACES = ("ip",)
COOPERATIVE_SURVEILLANCE_TECHNOLOGIES = (
    "network_remote_id", "adsb", "multilateration",
)

#: ---------------------------------------------------------------------------
#: 允许人工补录的工程证据字段（本轮只解决真正阻塞主链的字段）。
#: ---------------------------------------------------------------------------
#:
#: 每个字段声明：
#:
#: * ``subsystem`` / ``kind``：它叠加到机载能力的哪一层；
#: * ``value_type``：``enum`` 逐项校验（列表）或 ``enum_scalar`` 单值校验；
#: * ``allowed``：合法值域（**fail-closed**，绝不放任自由文本）；
#: * ``demand_field``：它在需求侧由哪个 ``type`` 字段表达（用于"为什么需要它"）；
#: * ``label`` / ``semantics``：前端与报告的可读说明。
#: ---------------------------------------------------------------------------
#: Round 2.5：**P17 连续服务可接受性**的显式参数（可由规划证据 UI 修改）。
#: ---------------------------------------------------------------------------
#:
#: 这些字段与机载"能力叠加"无关（``kind = continuous_service_parameter``），
#: 它们的值是**评估参数**：C 全失联 / 冗余降级阈值、保护走廊链时延分量、
#: 安全边界距离、不确定度距离、入侵者速度与接近速度、导航证据。
#:
#: 每一条都必须是显式记录（``confirmed_source_fact`` / ``external_reference`` /
#: ``engineering_assumption`` / ``unknown``）；**未提供记录时**，评估使用
#: :mod:`cns_planner.domain.cns_continuous_service` 的内置工程基线，并在结果里
#: 逐项标注 ``authority = builtin_engineering_assumption``（绝不是法规事实）。
CONTINUOUS_SERVICE_PARAMETER_NAMES = (
    # C 通信
    "c_full_outage_max_s",
    "c_redundancy_degradation_max_s",
    # 保护走廊（Round 2.6 正式公式的四项）
    "T_chain_detect_track_s",
    "T_chain_sensor_to_platform_s",
    "T_chain_platform_processing_s",
    "T_chain_platform_to_aircraft_s",
    "T_chain_aircraft_response_manoeuvre_s",
    "D_separation_m",
    "D_maneuver_m",
    "D_uncertainty_m",
    "design_intruder_speed_mps",
    "nominal_closing_speed_mps",
    "conservative_closing_speed_mps",
    "relative_speed_basis",
    # 导航
    "navigation_route_containment_accuracy_m",
    "rtk_availability",
    "rtk_unavailable_along_route",
    "gnss_availability",
    "gnss_accuracy_along_route_m",
    "navigation_degradation_time_s",
)

#: Round 2.5 旧参数名（仍可被显式记录）：正式名没有记录时作为**回退**读取。
#: 它们不出现在 :data:`CONTINUOUS_SERVICE_PARAMETER_NAMES` 里（因此不会在 UI 中出现
#: 第二个输入框），但旧项目保存过的证据仍然有效。
LEGACY_CONTINUOUS_SERVICE_PARAMETER_NAMES = (
    "D_safety_m",
)

#: 布尔型枚举（fail-closed：只接受这两个字面量之一，绝不接受自由文本）。
_CONTINUOUS_BOOLEAN_ALLOWED = ("available", "not_available")


def _continuous_fields():
    fields = {}
    #: 兼容读取用的 Round 2.5 旧名：**必须**仍在清单里，否则旧项目保存过的证据
    #: 会在规范化时被判为"非法字段"而拒绝加载（这是迁移可用性问题，不是策略问题）。
    #: 它不出现在 CONTINUOUS_SERVICE_PARAMETER_NAMES 中，因此前端不会出现第二个输入框。
    for name in LEGACY_CONTINUOUS_SERVICE_PARAMETER_NAMES:
        fields[name] = {
            "label": _CONTINUOUS_LABELS[name],
            "subsystem": _CONTINUOUS_SUBSYSTEMS[name],
            "kind": "continuous_service_parameter",
            "value_type": "number",
            "allowed": "非负有限数值",
            "unit": _CONTINUOUS_UNITS[name],
            "demand_field": None,
            "declared_field": name,
            "legacy_alias": True,
            "semantics": _CONTINUOUS_SEMANTICS[name],
        }
    for name in CONTINUOUS_SERVICE_PARAMETER_NAMES:
        if name in ("rtk_availability", "gnss_availability"):
            fields[name] = {
                "label": _CONTINUOUS_LABELS[name],
                "subsystem": _CONTINUOUS_SUBSYSTEMS[name],
                "kind": "continuous_service_parameter",
                "value_type": "enum_scalar",
                "allowed": _CONTINUOUS_BOOLEAN_ALLOWED,
                "demand_field": None,
                "declared_field": name,
                "semantics": _CONTINUOUS_SEMANTICS[name],
            }
        elif name in ("relative_speed_basis", "rtk_unavailable_along_route"):
            fields[name] = {
                "label": _CONTINUOUS_LABELS[name],
                "subsystem": _CONTINUOUS_SUBSYSTEMS[name],
                "kind": "continuous_service_parameter",
                "value_type": "enum",
                "allowed": _CONTINUOUS_ENUMS[name],
                "demand_field": None,
                "declared_field": name,
                "semantics": _CONTINUOUS_SEMANTICS[name],
            }
        else:
            fields[name] = {
                "label": _CONTINUOUS_LABELS[name],
                "subsystem": _CONTINUOUS_SUBSYSTEMS[name],
                "kind": "continuous_service_parameter",
                "value_type": "number",
                "allowed": "非负有限数值",
                "unit": _CONTINUOUS_UNITS[name],
                "demand_field": None,
                "declared_field": name,
                "semantics": _CONTINUOUS_SEMANTICS[name],
            }
    return fields


_CONTINUOUS_LABELS = {
    "c_full_outage_max_s": "通信全失联可接受最长时长",
    "c_redundancy_degradation_max_s": "通信冗余退化可接受最长时长",
    "T_chain_detect_track_s": "保护链：探测/跟踪",
    "T_chain_sensor_to_platform_s": "保护链：传感器→平台",
    "T_chain_platform_processing_s": "保护链：平台处理",
    "T_chain_platform_to_aircraft_s": "保护链：平台→航空器",
    "T_chain_aircraft_response_manoeuvre_s": "保护链：航空器响应机动",
    "D_safety_m": "分隔距离（Round 2.5 旧名，兼容读取）",
    "D_separation_m": "分隔距离（D_separation）",
    "D_maneuver_m": "机动附加距离（D_maneuver，工程基线 50 m）",
    "D_uncertainty_m": "不确定度距离（D_uncertainty）",
    "design_intruder_speed_mps": "设计入侵者速度",
    "nominal_closing_speed_mps": "标称接近速度",
    "conservative_closing_speed_mps": "保守接近速度",
    "relative_speed_basis": "链路时延计算采用的接近速度口径",
    "navigation_route_containment_accuracy_m": "航路保持所需水平精度",
    "rtk_availability": "RTK 可用性",
    "rtk_unavailable_along_route": "航路内 RTK 不可用区间",
    "gnss_availability": "GNSS 可用性",
    "gnss_accuracy_along_route_m": "航路内 GNSS 水平精度",
    "navigation_degradation_time_s": "导航降级允许时长（外部参考，不作为硬门）",
}

_CONTINUOUS_SUBSYSTEMS = {
    "c_full_outage_max_s": "C",
    "c_redundancy_degradation_max_s": "C",
    "T_chain_detect_track_s": "S",
    "T_chain_sensor_to_platform_s": "S",
    "T_chain_platform_processing_s": "S",
    "T_chain_platform_to_aircraft_s": "S",
    "T_chain_aircraft_response_manoeuvre_s": "S",
    "D_safety_m": "S",
    "D_separation_m": "S",
    "D_maneuver_m": "S",
    "D_uncertainty_m": "S",
    "design_intruder_speed_mps": "S",
    "nominal_closing_speed_mps": "S",
    "conservative_closing_speed_mps": "S",
    "relative_speed_basis": "S",
    "navigation_route_containment_accuracy_m": "N",
    "rtk_availability": "N",
    "rtk_unavailable_along_route": "N",
    "gnss_availability": "N",
    "gnss_accuracy_along_route_m": "N",
    "navigation_degradation_time_s": "N",
}

_CONTINUOUS_UNITS = {
    "c_full_outage_max_s": "s",
    "c_redundancy_degradation_max_s": "s",
    "T_chain_detect_track_s": "s",
    "T_chain_sensor_to_platform_s": "s",
    "T_chain_platform_processing_s": "s",
    "T_chain_platform_to_aircraft_s": "s",
    "T_chain_aircraft_response_manoeuvre_s": "s",
    "D_safety_m": "m",
    "D_separation_m": "m",
    "D_maneuver_m": "m",
    "D_uncertainty_m": "m",
    "design_intruder_speed_mps": "m/s",
    "nominal_closing_speed_mps": "m/s",
    "conservative_closing_speed_mps": "m/s",
    "navigation_route_containment_accuracy_m": "m",
    "gnss_accuracy_along_route_m": "m",
    "navigation_degradation_time_s": "s",
}

_CONTINUOUS_ENUMS = {
    "relative_speed_basis": ("nominal", "conservative"),
    "rtk_unavailable_along_route": (
        "none", "whole_route", "explicit_segments", "unknown",
    ),
}

#: 单值枚举（``enum`` 的多值语义会把一个标量包成长度 1 的列表，P17 需要标量比较）。
_CONTINUOUS_SCALAR_ENUMS = ("relative_speed_basis", "rtk_unavailable_along_route")

_CONTINUOUS_SEMANTICS = {
    "c_full_outage_max_s": (
        "**服务中断**（service_outage）的独立阈值：连续全失联时长不超过它时可作为"
        "``managed_gap``。Round 2.5 内置工程基线取 3 s，其依据是 FC30 的 failsafe 触发"
        "事实（遥控信号丢失>3 s 触发 RTH）——它是**设备 failsafe 事实**，不是法规阈值。"
    ),
    "c_redundancy_degradation_max_s": (
        "**冗余退化**（redundancy_degradation）的独立阈值：链路仍可用但独立 provider"
        "数量不足的连续时长。它与全失联阈值**互相独立**，绝不共用同一个数。"
    ),
    "T_chain_detect_track_s": "保护链分量：探测并建立跟踪所需时间（可配置，非法规事实）。",
    "T_chain_sensor_to_platform_s": "保护链分量：传感器到平台的数据传递时间。",
    "T_chain_platform_processing_s": "保护链分量：平台处理/判定时间。",
    "T_chain_platform_to_aircraft_s": "保护链分量：平台到航空器的指令传递时间。",
    "T_chain_aircraft_response_manoeuvre_s": "保护链分量：航空器响应与机动完成时间。",
    "D_safety_m": (
        "**Round 2.5 旧名**，等同 Round 2.6 的 ``D_separation_m``。正式参数请用新名；"
        "旧项目里以旧名登记的证据仍会被读取（仅在新名没有显式记录时回退使用）。"
    ),
    "D_separation_m": (
        "分隔距离：从航路中心线起算的内边界。它同时进入保护走廊宽度与监视可用时间"
        "``T_available``。**必须提供显式工程依据**：没有依据时保持 evidence_required，"
        "不再静默按 0 m 计算。"
    ),
    "D_maneuver_m": (
        "机动附加距离 ``D_maneuver``：本轮不研究机动模型，只保留算法接口。"
        "内置取 50 m，身份是 **engineering_baseline**（不是法规值，也不是某机型的普遍"
        "制动距离事实），可由显式工程依据替换。"
    ),
    "D_uncertainty_m": (
        "不确定度距离 ``D_uncertainty``：定位/航迹/航路几何不确定度的保守附加量。"
        "**必须提供显式工程依据**：没有依据时保持 evidence_required，不再静默按 0 m 计算。"
    ),
    "design_intruder_speed_mps": "设计入侵者速度（同时是操作场景输入）。",
    "nominal_closing_speed_mps": "标称接近速度（本机航路速度 + 设计入侵者速度）。",
    "conservative_closing_speed_mps": "保守接近速度（工程保守取值，用于保护链时延判定）。",
    "relative_speed_basis": "保护链时延判定采用标称还是保守接近速度（默认保守）。",
    "navigation_route_containment_accuracy_m": "维持航路保持所需的机载水平精度上限。",
    "rtk_availability": "机载 RTK 增强是否可用（available / not_available）。",
    "rtk_unavailable_along_route": "航路内 RTK 不可用区间的形态（none / whole_route / explicit_segments / unknown）。",
    "gnss_availability": "机载 GNSS 定位是否可用（available / not_available）。",
    "gnss_accuracy_along_route_m": "航路内 GNSS 水平精度（用于与航路保持要求逐项比较）。",
    "navigation_degradation_time_s": (
        "**外部研究参考**（例如公开研究中的 RTK 恢复 9–13 s），Round 2.5 **不**把它"
        "作为硬门；这里只登记来源可追溯的参考值。"
    ),
}


PLANNING_EVIDENCE_FIELDS = {
    #: Round 2.5：连续服务可接受性参数（数值 / 枚举；无 demand_field）。
    **_continuous_fields(),
    "communication_network_scope": {
        "label": "通信网络归属范围（机载侧）",
        "subsystem": "C",
        "kind": "aircraft_type_field",
        "value_type": "enum_scalar",
        "allowed": ("public", "private", "dedicated", "managed_service", "other"),
        "demand_field": "network_scope",
        "declared_field": "network_scope",
        "semantics": (
            "需求侧 ``type.network_scope`` 是**真实类型门禁**（有枚举约束）。机载能力档案"
            "未声明该字段时判定保持 unknown；本记录为该机载平台显式提供该声明。"
        ),
    },
    "communication_airborne_interfaces": {
        "label": "通信机载接口（机载侧）",
        "subsystem": "C",
        "kind": "aircraft_type_interfaces",
        "value_type": "enum",
        "allowed": COMMUNICATION_AIRBORNE_INTERFACES,
        "demand_field": "interfaces",
        "declared_field": "interfaces",
        "semantics": (
            "需求侧 ``type.interfaces`` 是**真实类型门禁**，且要求机载与地面提供者接口"
            "**有交集**才算兼容。本记录显式追加该机载平台的接口声明（不删除既有声明）。"
        ),
    },
    "remote_id_participation": {
        "label": "机载网络远程识别参与能力",
        "subsystem": "S",
        "kind": "airborne_cooperative_surveillance_services",
        "value_type": "enum",
        "allowed": COOPERATIVE_SURVEILLANCE_TECHNOLOGIES,
        #: 需求侧用 ``technology`` 表达"要求哪种合作监视参与技术"
        #: （``network_remote_id``）；机载侧由参与服务声明列表承载同一事实。
        "demand_field": "technology",
        "declared_field": "cooperative_surveillance_services",
        "semantics": (
            "机载在 RID 服务里的角色是 **cooperative target**（广播 / 网络上报 Remote ID），"
            "**不是**被动 sensor。需求侧 ``sensor_mode`` 描述的是**地面网络 RID 接收节点**"
            "的工作模式，绝不用于机载判定。本记录显式声明该机载平台**支持参与**的合作监视"
            "技术；机载未声明时判定保持 unknown（evidence required），"
            "绝不自动降级为 does_not_meet_under_model。"
        ),
    },
}


# ---------------------------------------------------------------------------
# 规范化
# ---------------------------------------------------------------------------


def empty_planning_evidence() -> dict:
    return {
        "schema_version": PLANNING_EVIDENCE_SCHEMA_VERSION,
        "items": [],
    }


def normalize_engineering_evidence(value: dict, *, now: str | None = None) -> dict:
    """规范化一条工程证据 / 规划假设（fail-closed 枚举校验 + 强制披露）。"""

    if not isinstance(value, dict):
        raise ValueError("工程证据条目必须是对象")
    evidence_id = _required_text(value, "evidence_id")
    field = _required_text(value, "field")
    spec = PLANNING_EVIDENCE_FIELDS.get(field)
    if spec is None:
        raise ValueError(
            "field 不在允许的人工工程证据清单内：" + ", ".join(sorted(PLANNING_EVIDENCE_FIELDS))
        )
    source_type = _required_text(value, "source_type")
    if source_type not in EVIDENCE_SOURCE_TYPES:
        raise ValueError(
            "source_type 必须是 " + " / ".join(EVIDENCE_SOURCE_TYPES)
        )
    scope = str(value.get("scope") or "aircraft").strip()
    if scope not in PROJECT_EVIDENCE_SCOPES:
        raise ValueError("scope 必须是 project / aircraft")
    target_id = _optional_text(value.get("target_id"))
    if scope == "aircraft" and not target_id:
        raise ValueError("scope=aircraft 的工程证据必须声明 target_id（机载档案 ID）")

    normalized_value = _normalize_value(spec, value.get("value"))
    statement = _optional_text(value.get("statement"))
    source = _optional_text(value.get("source"))
    reason = _optional_text(value.get("reason"))
    confirmed = value.get("confirmed") is True
    confirmed_by_user = value.get("confirmed_by_user") is True
    report_disclosure = _optional_text(value.get("report_disclosure"))

    if source_type == "unknown":
        #: ``unknown`` 的作用是**如实登记缺口**，因此它绝不携带参与判定的值。
        if normalized_value is not None:
            raise ValueError("source_type=unknown 的工程证据不得携带 value")
        confirmed_by_user = value.get("confirmed_by_user") is True
    elif source_type == "confirmed_source_fact":
        if not source:
            raise ValueError("confirmed_source_fact 必须声明 source（正式资料出处）")
        if not confirmed:
            raise ValueError("confirmed_source_fact 必须显式 confirmed=true")
    elif source_type == "external_reference":
        #: Round 2.5：外部研究 / 公开资料的参考值。它必须能追溯到出处，且
        #: **明确**声明它不是本项目的正式资料、也不是本项目的工程假设。
        missing = [
            name for name, current in (
                ("source", source), ("statement", statement),
                ("report_disclosure", report_disclosure),
            ) if not current
        ]
        if missing:
            raise ValueError("external_reference 缺少必填字段：" + ", ".join(missing))
        if not _optional_text(value.get("external_reference")):
            raise ValueError("external_reference 必须声明 external_reference（外部资料名称/编号）")
    else:  # engineering_assumption
        missing = [
            name for name, current in (
                ("statement", statement), ("source", source),
                ("reason", reason), ("report_disclosure", report_disclosure),
            ) if not current
        ]
        if missing:
            raise ValueError("engineering_assumption 缺少必填字段：" + ", ".join(missing))
        if confirmed_by_user is not True:
            raise ValueError("engineering_assumption 必须 confirmed_by_user=true（由用户确认）")
        if value.get("declared_by") in (None, ""):
            raise ValueError("engineering_assumption 必须声明 declared_by")

    created_at = _optional_text(value.get("created_at")) or now or _utc_now()
    return {
        "evidence_id": evidence_id,
        "schema_version": PLANNING_EVIDENCE_SCHEMA_VERSION,
        "scope": scope,
        "target_id": target_id,
        "field": field,
        "subsystem": spec["subsystem"],
        "kind": spec["kind"],
        "value": normalized_value,
        "source_type": source_type,
        "source": source,
        "external_reference": _optional_text(value.get("external_reference")),
        "statement": statement,
        "reason": reason,
        "declared_by": _optional_text(value.get("declared_by")),
        "confirmed": confirmed,
        "confirmed_by_user": confirmed_by_user,
        "confirmation_status": "confirmed" if confirmed else "pending_confirmation",
        "authority_effect": EVIDENCE_AUTHORITY_EFFECT[source_type],
        "report_disclosure": report_disclosure,
        "provenance": _provenance(value.get("provenance"), spec, source_type, source),
        "created_at": created_at,
        "status": _status(value.get("status")),
    }


def normalize_planning_evidence_registry(value: dict | None) -> dict:
    if value is None:
        return empty_planning_evidence()
    if not isinstance(value, dict):
        raise ValueError("planning_evidence registry 必须是对象")
    items = value.get("items") or []
    if not isinstance(items, list):
        raise ValueError("planning_evidence.items 必须是数组")
    normalized = [normalize_engineering_evidence(item) for item in items]
    ids = [item["evidence_id"] for item in normalized]
    if len(ids) != len(set(ids)):
        raise ValueError("evidence_id 重复")
    return {
        "schema_version": PLANNING_EVIDENCE_SCHEMA_VERSION,
        "items": normalized,
    }


def active_evidence_items(
    registry: dict | None, *, target_id: str | None = None, field: str | None = None,
) -> list[dict]:
    """参与判定的记录（``status=active`` 且权威效应允许参与）。"""

    result = []
    for item in normalize_planning_evidence_registry(registry)["items"]:
        if item["status"] != "active":
            continue
        if item["authority_effect"] == "not_participating":
            continue
        if target_id is not None and item["scope"] == "aircraft" and item["target_id"] != target_id:
            continue
        if field is not None and item["field"] != field:
            continue
        result.append(deepcopy(item))
    return result


# ---------------------------------------------------------------------------
# 消费：把工程证据叠加到机载能力（绝不写回 aircraft profile 本身）
# ---------------------------------------------------------------------------


def apply_aircraft_evidence(profile: dict | None, registry: dict | None) -> dict | None:
    """把工程证据叠加到**选定机载档案**的一份副本上。

    * 输入 ``profile`` / ``registry`` 都**不被修改**；返回新对象；
    * 只叠加 ``scope=aircraft`` 且 ``target_id`` 与该档案匹配的记录；
    * ``source_type=unknown`` 的记录**不参与**（它们只登记缺口）；
    * 叠加结果在 ``aircraft_evidence`` 上保留逐字段来源，供 P8/P14/P15/P16
      与报告披露"这条能力来自事实还是工程假设"；
    * 没有任何有效记录时**逐字节返回原档案的深拷贝**（旧项目行为完全不变）。
    """

    result = deepcopy(profile) if isinstance(profile, dict) else profile
    if not isinstance(result, dict):
        return result
    target_id = str(result.get("aircraft_id") or "")
    items = active_evidence_items(registry, target_id=target_id) if target_id else []
    items = [
        item for item in items
        if item["scope"] == "aircraft" and item["target_id"] == target_id
    ]
    if not items:
        return result
    sources = {}
    for item in items:
        spec = PLANNING_EVIDENCE_FIELDS[item["field"]]
        subsystem = _subsystem_name(spec["subsystem"])
        block = result.setdefault(subsystem, {})
        if not isinstance(block, dict):
            continue
        type_block = block.setdefault("type", {})
        if not isinstance(type_block, dict):
            continue
        if spec["kind"] == "aircraft_type_field":
            type_block[spec["declared_field"]] = item["value"]
        elif spec["kind"] == "aircraft_type_interfaces":
            existing = [str(entry) for entry in type_block.get("interfaces") or []]
            for entry in item["value"]:
                if entry not in existing:
                    existing.append(entry)
            type_block["interfaces"] = existing
        elif spec["kind"] == "airborne_cooperative_surveillance_services":
            from .cns_service_contract import AIRBORNE_COOPERATIVE_SURVEILLANCE_FIELD

            declared = list(type_block.get(AIRBORNE_COOPERATIVE_SURVEILLANCE_FIELD) or [])
            for technology in item["value"]:
                entry = {"technology": technology}
                if technology == "network_remote_id":
                    entry["service_subtype"] = "cooperative_surveillance"
                if entry not in declared:
                    declared.append(entry)
            type_block[AIRBORNE_COOPERATIVE_SURVEILLANCE_FIELD] = declared
        sources[item["field"]] = {
            "evidence_id": item["evidence_id"],
            "source_type": item["source_type"],
            "source": item["source"],
            "confirmed": item["confirmed"],
            "confirmed_by_user": item["confirmed_by_user"],
            "report_disclosure": item["report_disclosure"],
        }
    if not sources:
        return result
    summary = {
        "source_types": sorted({item["source_type"] for item in items}),
        "source_type": (
            items[0]["source_type"] if len({item["source_type"] for item in items}) == 1
            else "mixed"
        ),
        "fields": sources,
        "semantics": "engineering_planning_input_disclosed_not_manufacturer_fact",
        "planning_input_only": any(
            item["source_type"] == "engineering_assumption" for item in items
        ),
    }
    result["airborne_evidence"] = deepcopy(summary)
    #: P8 的机载判定只收到**子系统 capability 子树**，因此来源标注必须同时写到
    #: 每个被叠加的子系统块上，否则"这条能力来自工程假设"会在那一层丢失。
    for field, entry in sources.items():
        spec = PLANNING_EVIDENCE_FIELDS[field]
        block = result.get(_subsystem_name(spec["subsystem"]))
        if isinstance(block, dict):
            block["airborne_evidence"] = deepcopy(summary)
    return result


def evidence_disclosure_lines(registry: dict | None) -> list[str]:
    """给报告 / 前端用的披露行（逐条原文，绝不改写）。"""

    lines = []
    for item in normalize_planning_evidence_registry(registry)["items"]:
        if item["status"] != "active":
            continue
        spec = PLANNING_EVIDENCE_FIELDS[item["field"]]
        if item["source_type"] == "unknown":
            lines.append(f"{spec['label']}：尚无依据（unknown），不参与判定。")
            continue
        prefix = {
            "confirmed_source_fact": "有正式资料支持",
            "external_reference": "外部研究参考（非本项目正式资料、非法规事实、非工程假设）",
            "engineering_assumption": "工程规划假设（不代表厂家既有设备事实）",
        }.get(item["source_type"], "来源未分类")
        reference = item.get("external_reference")
        lines.append(
            f"{spec['label']}＝{_render_value(item['value'])}（{prefix}；"
            f"来源：{item['source'] or '未记录'}"
            + (f"；外部参考：{reference}" if reference else "")
            + "）。"
            f"{item['report_disclosure'] or ''}"
        )
    return lines


def evidence_satisfies_requirement(
    field: str, value, required_value, spec: dict | None = None,
):
    """该字段的当前取值是否**真的满足需求**（逐项核对，绝不含糊）。

    返回 ``(满足?, 原因)``：

    * ``True``  —— 满足需求（可以作为一条成立的能力事实/假设参与判定）；
    * ``False`` —— **确认不满足**（取值本身与需求冲突，例如机载接口与地面提供者
      无交集）——这不是"缺证据"，而是真实不兼容，必须如实报出；
    * ``None``  —— **无法确认**（缺值 / 缺证据），保持 ``evidence_required``。

    ``field_status`` 若只判断"机载有没有给值"，就会把"机载写了 ``validation_radio_v1``、
    地面要求 ``ip``"这种**明确冲突**误报成"已具备可用依据"，从而让用户看不到真实阻塞。
    """

    spec = spec or PLANNING_EVIDENCE_FIELDS[field]
    kind = spec["kind"]
    if kind == "aircraft_type_interfaces":
        demanded = [str(item) for item in (required_value or []) if str(item)]
        declared = [str(item) for item in (value or []) if str(item)]
        if not demanded:
            return None, "需求未声明接口要求"
        if not declared:
            return None, "机载档案未声明任何接口"
        return bool(set(demanded) & set(declared)), (
            "机载接口与需求接口有交集" if set(demanded) & set(declared)
            else "机载接口与需求接口无交集（地面提供者要求 " + "、".join(demanded) + "）"
        )
    if kind == "airborne_cooperative_surveillance_services":
        if required_value in (None, "", "unknown"):
            return None, "需求未声明参与技术"
        declared = [str(item) for item in (value or []) if str(item)]
        if not declared:
            return None, "机载未声明参与能力"
        return required_value in declared, (
            "机载参与能力覆盖需求技术" if required_value in declared
            else "机载参与能力不覆盖需求技术 " + str(required_value)
        )
    if required_value in (None, "", "unknown", []):
        return None, "需求未声明该字段"
    if value in (None, "", "unknown", []):
        return None, "尚未提供取值"
    if spec["value_type"] == "number":
        #: Round 2.5：数值型工程参数的逐项核对（例如导航证据要求 GNSS 精度
        #: **满足**航路保持要求）。绝不把"数值写反了"当成"已具备可用依据"。
        actual, target = _optional_number(value), _optional_number(required_value)
        if actual is None or target is None:
            return None, "数值不可比较"
        if spec.get("requirement_operator") == "<=":
            return actual <= target, (
                f"取值 {actual} 满足上限 {target}" if actual <= target
                else f"取值 {actual} 超过上限 {target}"
            )
        return actual >= target, (
            f"取值 {actual} 满足下限 {target}" if actual >= target
            else f"取值 {actual} 低于下限 {target}"
        )
    return value == required_value, (
        "取值与需求一致" if value == required_value else "取值与需求不一致"
    )


# ---------------------------------------------------------------------------
# Round 2.5：连续服务可接受性参数的解析（**唯一**消费入口）
# ---------------------------------------------------------------------------


def continuous_service_parameter_items(registry: dict | None) -> list[dict]:
    """registry 中所有 ``kind = continuous_service_parameter`` 的参与记录。"""

    return [
        item for item in active_evidence_items(registry)
        if item.get("kind") == "continuous_service_parameter"
    ]


def is_continuous_service_field(field: str) -> bool:
    """该字段是否是 P17 连续服务参数（而不是机载能力叠加字段）。"""

    spec = PLANNING_EVIDENCE_FIELDS.get(str(field or ""))
    return bool(spec) and spec.get("kind") == "continuous_service_parameter"


def param_targets_continuous_service(scope: str, field: str) -> bool:
    """一条证据记录的变化是否只应让 **P17** 失效（而不是 P8/P14/P15/P16）。

    ``scope=aircraft`` 的连续服务参数（例如航路保持精度要求）会被 P17 直接消费，
    **不**参与机载能力叠加，因此它绝不触发 ``aircraft_profile`` 全链失效。
    """

    if not is_continuous_service_field(field):
        return False
    return str(scope or "aircraft") in ("aircraft", "project")


def _parameter_aliases(name: str) -> tuple:
    """该参数的**兼容别名**（Round 2.5 旧名 → Round 2.6 正式名）。

    只在正式名**没有任何显式记录**时才回退读旧名；两者绝不合并，也绝不互相覆盖。
    """

    from .cns_continuous_service import PARAMETER_ALIASES

    return tuple(PARAMETER_ALIASES.get(name) or ())


def resolve_continuous_parameter(registry: dict | None, name: str) -> dict:
    """解析一个 P17 评估参数：**显式记录优先，否则内置工程基线**。

    返回（绝不抛异常，缺证据就如实返回 unknown）：

    ``value``           实际参与评估的数值 / 枚举（无记录且有内置基线时 = 内置值）
    ``authority``       ``explicit_evidence`` / ``builtin_engineering_assumption`` / ``unknown``
    ``source_type``     ``confirmed_source_fact`` / ``external_reference`` /
                        ``engineering_assumption`` / ``internal_baseline`` / ``unknown``
    ``participating``   是否允许参与判定（``unknown`` 永不参与）
    ``reason``          可读原因（前端 / 报告逐字使用）

    Round 2.6：内置基线为 ``None`` 的参数（``D_separation_m`` / ``D_uncertainty_m``）在
    没有显式记录时返回 ``evidence_required`` —— 调用方必须 fail-closed，**不得**用 0 代替。
    """

    if name not in PLANNING_EVIDENCE_FIELDS:
        raise ValueError(f"不是合法的证据字段：{name}")
    spec = PLANNING_EVIDENCE_FIELDS[name]
    if spec.get("kind") != "continuous_service_parameter":
        raise ValueError(f"{name} 不是连续服务可接受性参数")
    items = [
        item for item in continuous_service_parameter_items(registry)
        if item["field"] == name
    ]
    alias_used = None
    if not items:
        for alias in _parameter_aliases(name):
            items = [
                item for item in continuous_service_parameter_items(registry)
                if item["field"] == alias
            ]
            if items:
                alias_used = alias
                break
    baseline = continuous_service_parameter_baseline(name)
    base = {
        "field": name, "label": spec["label"], "subsystem": spec["subsystem"],
        "unit": spec.get("unit"), "value_type": spec["value_type"],
        "semantics": spec["semantics"],
        "alias_of": None,
        "read_from_legacy_field": alias_used,
    }
    if items:
        item = items[-1]
        if item["source_type"] == "unknown" or item["value"] is None:
            return {
                **base, "value": None, "authority": "unknown",
                "source_type": "unknown", "participating": False,
                "evidence_id": item["evidence_id"],
                "reason": "已显式登记为 unknown（无依据），保持 unknown 并 fail-closed",
            }
        return {
            **base, "value": item["value"], "authority": "explicit_evidence",
            "source_type": item["source_type"], "participating": True,
            "evidence_id": item["evidence_id"], "source": item["source"],
            "external_reference": item.get("external_reference"),
            "statement": item.get("statement"),
            "report_disclosure": item.get("report_disclosure"),
            "confirmed_by_user": item.get("confirmed_by_user"),
            "reason": (
                "采用显式登记的外部研究参考值（external_reference）"
                if item["source_type"] == "external_reference"
                else "采用显式登记的工程证据 / 规划假设"
                if item["source_type"] == "engineering_assumption"
                else "采用显式登记的正式资料事实"
            ) + (
                f"（读自 Round 2.5 旧字段名 {alias_used}）" if alias_used else ""
            ),
        }
    if baseline["value"] is None:
        return {
            **base, "value": None, "authority": "evidence_required",
            "source_type": "unknown", "participating": False,
            "statement": baseline["statement"],
            "report_disclosure": baseline["report_disclosure"],
            "reason": (
                "尚无任何依据（无显式记录、也无内置工程基线）：必须由用户 / 工程依据"
                "显式提供，系统保持 evidence_required（fail-closed），"
                "**绝不用 0 或其它默认值代替**"
            ),
        }
    return {
        **base, "value": baseline["value"],
        "authority": "builtin_engineering_assumption",
        "source_type": "internal_baseline",
        "participating": True,
        "source": baseline["source"],
        "statement": baseline["statement"],
        "report_disclosure": baseline["report_disclosure"],
        "confirmed_by_user": False,
        "reason": (
            "未提供显式工程依据：采用内置工程基线（默认值），"
            "**不代表法规或厂家事实**，可在工程依据入口显式覆盖"
        ),
    }


def continuous_service_parameter_baseline(name: str) -> dict:
    """P17 参数的内置工程基线（延迟导入，避免 domain 层循环依赖）。"""

    from .cns_continuous_service import parameter_baseline

    return parameter_baseline(name)


def continuous_service_parameter_projection(registry: dict | None) -> list[dict]:
    """全部 P17 参数的只读投影（前端 / 报告用；逐项带来源与出处）。

    Round 2.5 的旧名（``D_safety_m``）**不**出现在投影里：它只是兼容读取路径，
    不应在前端出现第二个输入框。旧记录被读取时会通过 ``read_from_legacy_field``
    在结果里如实标注。
    """

    return [resolve_continuous_parameter(registry, name) for name in CONTINUOUS_SERVICE_PARAMETER_NAMES]


def _optional_number(value):
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def is_continuous_service_scalar_enum(field: str) -> bool:
    """该连续服务参数是否是**单值枚举**（返回值是标量，不是长度 1 的列表）。"""

    return str(field or "") in _CONTINUOUS_SCALAR_ENUMS


def _normalize_value(spec, value):
    if spec["value_type"] == "number":
        number = _optional_number(value)
        if number is None:
            return None
        if number < 0:
            raise ValueError("value 必须是非负有限数值")
        return number
    if spec["value_type"] == "enum_scalar" or (
        spec["value_type"] == "enum"
        and is_continuous_service_scalar_enum(spec.get("declared_field"))
    ):
        #: 单值枚举：返回**标量**（P17 的 ``relative_speed_basis`` 等需要直接比较）。
        if value in (None, ""):
            return None
        if isinstance(value, (list, tuple)):
            items = [str(item).strip().lower() for item in value if str(item or "").strip()]
            if len(items) != 1:
                raise ValueError("该字段是单值枚举，只能声明一个取值")
            text = items[0]
        else:
            text = str(value).strip().lower()
        if text not in spec["allowed"]:
            raise ValueError(
                f"value 无效：{text}；必须是 {', '.join(spec['allowed'])} 之一"
            )
        return text
    if spec["value_type"] == "enum_scalar":
        if value in (None, ""):
            return None
        text = str(value).strip().lower()
        if text not in spec["allowed"]:
            raise ValueError(
                f"value 无效：{text}；必须是 {', '.join(spec['allowed'])} 之一"
            )
        return text
    if value in (None, ""):
        #: 未给出取值 ⇒ 返回 ``None``（而不是空列表），这样 ``source_type=unknown``
        #: 的"绝不携带取值"判定与"确实没填"在语义上完全一致。
        return None
    items = value if isinstance(value, (list, tuple)) else [value]
    result = []
    for raw in items:
        text = str(raw or "").strip().lower()
        if not text:
            continue
        if text not in spec["allowed"]:
            raise ValueError(
                f"value 无效：{text}；必须是 {', '.join(spec['allowed'])} 之一"
            )
        if text not in result:
            result.append(text)
    if not result:
        raise ValueError("value 至少需要一个合法取值")
    return result


def _provenance(value, spec, source_type, source):
    result = deepcopy(value) if isinstance(value, dict) else {}
    result.setdefault("field", None)
    result.update({
        "evidence_field": spec["label"],
        "demand_field": spec["demand_field"],
        "source_type": source_type,
        "source": source,
        "collector": "user_input_engineering_evidence",
        "container": "project_state.planning_evidence",
        "never_written_to_device_catalog": True,
    })
    if result.get("field") is None:
        result.pop("field")
    return result


def _subsystem_name(code):
    return {"C": "communication", "N": "navigation", "S": "surveillance"}.get(code, code)


def _render_value(value):
    if isinstance(value, list):
        return "、".join(str(item) for item in value)
    return str(value)


def _status(value):
    text = str(value or "active").strip().lower()
    if text not in ("active", "withdrawn", "superseded"):
        raise ValueError("工程证据 status 必须是 active / withdrawn / superseded")
    return text


def _required_text(value, field):
    text = _optional_text(value.get(field))
    if not text:
        raise ValueError(f"工程证据.{field} 必填")
    return text


def _optional_text(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "COMMUNICATION_AIRBORNE_INTERFACES", "CONTINUOUS_SERVICE_PARAMETER_NAMES",
    "COOPERATIVE_SURVEILLANCE_TECHNOLOGIES",
    "EVIDENCE_AUTHORITY_EFFECT", "EVIDENCE_SOURCE_TYPES",
    "PARTICIPATING_AUTHORITY_EFFECTS",
    "PLANNING_EVIDENCE_FIELDS", "PLANNING_EVIDENCE_SCHEMA_VERSION",
    "PROJECT_EVIDENCE_SCOPES",
    "active_evidence_items", "apply_aircraft_evidence",
    "continuous_service_parameter_baseline", "continuous_service_parameter_items",
    "continuous_service_parameter_projection", "empty_planning_evidence",
    "evidence_disclosure_lines", "evidence_satisfies_requirement",
    "is_continuous_service_field", "normalize_engineering_evidence",
    "normalize_planning_evidence_registry", "param_targets_continuous_service",
    "resolve_continuous_parameter",
]
