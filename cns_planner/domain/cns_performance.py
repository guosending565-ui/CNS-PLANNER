"""C/N/S taxonomy and performance contracts with legacy alias support.

The structures in this module deliberately separate airborne capability,
operational requirement and ground-device performance.  It only normalizes
JSON-safe data; planning algorithms continue to consume their established V1
fields through aliases maintained by the caller.
"""

from __future__ import annotations

from copy import deepcopy
from math import isfinite


COMMUNICATION_TECHNOLOGIES = (
    "4g", "5g", "dedicated_radio", "satellite", "wifi", "other", "unknown",
)
COMMUNICATION_NETWORK_SCOPES = (
    "public", "private", "dedicated", "managed_service", "other", "unknown",
)
NAVIGATION_TECHNOLOGIES = (
    "gnss", "gnss_rtk", "inertial", "visual", "terrestrial", "hybrid", "other", "unknown",
)
SURVEILLANCE_TARGET_COOPERATION = ("cooperative", "non_cooperative", "mixed")
SURVEILLANCE_SENSOR_MODES = ("active", "passive", "mixed")
SURVEILLANCE_TECHNOLOGIES = (
    "adsb", "radar", "multilateration", "eo_ir", "acoustic",
    "network_remote_id", "other", "unknown",
)

#: 参与「类型资格」判定的 ``type`` 字段 = 被判定方**必须能够声明的物理/技术事实**。
#:
#: Round 2.3 业务裁定（收敛"结构字段缺失造成的假 unknown"）：
#:
#: * 下面这些**有枚举约束的结构化维度**才构成类型门禁；它们表达的是"这一侧在物理上 /
#:   技术上能不能承担该要求"，设备目录**可以**声明也必须声明，未声明即证据不足
#:   （``unknown``，fail-closed）。
#: * ``service_type`` **不在**此列。它是**需求侧的用途/任务描述**（前端 RequiredCNS 的
#:   「服务类型」自由文本输入，例如 ``command_control``），既无枚举约束，也不是设备目录
#:   的字段：``catalogs/device_catalog.py`` 与 ``domain/cns_inputs.py::normalize_device``
#:   都**不写入** ``device.type.service_type``，因此设备侧永远无法声明它。设备的服务身份
#:   由 canonical ``service_key``（``C:communication`` / ``S:rid_cooperative`` …）承载；
#:   再要求一个语义重复、设备侧永远为空的字段，属于**同一事实被要求两次**，只会产生
#:   假 ``unknown``。该字段仍完整保留在 RequiredCNS、前端与结果里（纯描述性，不参与门禁）。
TYPE_GATE_FIELDS = (
    "technology", "network_scope", "interfaces",
    "target_cooperation", "sensor_mode", "service_subtype",
)


def type_gate_items(type_value: dict | None):
    """产出**参与类型门禁**的 ``(字段名, 要求值)``，保持声明顺序、跳过空值。

    需求侧声明的非门禁字段（当前只有 ``service_type``）被如实忽略 —— 调用方若需要
    登记它们，可自行对比 ``type_value`` 与 :data:`TYPE_GATE_FIELDS`。
    """

    if not isinstance(type_value, dict):
        return []
    return [
        (name, value) for name, value in type_value.items()
        if name in TYPE_GATE_FIELDS and value not in (None, "", "unknown", [])
    ]


def descriptive_type_fields(type_value: dict | None):
    """需求侧声明了、但**不参与**类型门禁的字段名（当前只有 ``service_type``）。"""

    if not isinstance(type_value, dict):
        return []
    return sorted(str(name) for name in type_value if name not in TYPE_GATE_FIELDS)


def airborne_type_items(type_value: dict | None, *, subsystem=None, service_key=None):
    """需求侧 ``type`` → **机载参与能力**的 ``(机载字段名, 期望声明属性)``。

    Round 2.4 修正（角色分离）：需求侧 ``type`` 块此前被**同一个**门禁同时用于
    地面提供者与机载两侧，于是机载被要求逐字段重复声明地面接收节点的事实。
    RID 的 ``sensor_mode = passive`` 描述的是**地面网络 RID 接收节点**只接收、
    不发射的工作模式；把它拿去要求 ``aircraft.sensor_mode`` 等于要求"无人机必须
    是地面被动传感器"，属角色错用。

    机载参与谓词只由**服务身份**裁决（见
    :func:`cns_service_contract.airborne_type_items`）：

    * ``sensor_mode`` / ``target_cooperation`` 绝不参与机载判定
      （前者是地面接收节点属性，后者由需求侧自身承载——无人机**就是**该目标）；
    * ``technology`` / ``service_subtype`` 由 ``cooperative_surveillance_services``
      列表承载，匹配语义是"列表中**存在**一条声明"。

    返回值第二项是**期望的声明属性字典**，调用方按成员匹配判定，**不做等值比较**。
    """

    from .cns_service_contract import airborne_type_items as _airborne_items

    return _airborne_items(type_value, subsystem=subsystem, service_key=service_key)


def empty_subsystem_contract(subsystem: str) -> dict:
    """Return a JSON-safe contract without inventing performance thresholds."""
    code = subsystem.upper()
    if code == "C":
        type_data = {
            "service_type": None, "technology": "unknown",
            "network_scope": "unknown", "interfaces": [],
        }
        performance = {
            "max_latency_s": None, "lost_link_threshold_s": None,
            "max_continuous_outage_s": None, "max_cumulative_outage_s": None,
            "min_availability": None, "min_redundancy": None,
        }
    elif code == "N":
        type_data = {"technology": "unknown"}
        performance = {
            "max_horizontal_error_m": None, "max_vertical_error_m": None,
            "integrity_required": None, "max_time_to_alert_s": None,
            "max_degradation_time_s": None, "min_availability": None,
            "min_redundancy": None,
        }
    elif code == "S":
        type_data = {
            "target_cooperation": None, "sensor_mode": None,
            "technology": "unknown",
        }
        performance = {
            "min_detection_range_m": None, "min_detection_probability": None,
            "max_update_interval_s": None, "max_track_loss_s": None,
            "max_alert_latency_s": None, "min_availability": None,
            "min_redundancy": None,
        }
    else:
        raise ValueError("subsystem 必须是 C/N/S")
    return {
        "type": type_data, "performance": performance, "contingency": None,
        "source": None, "confirmed": False,
        "confirmation_status": "pending_confirmation",
    }


def normalize_subsystem_contract(subsystem: str, value: dict | None, *, field: str) -> dict:
    """Normalize the canonical type/performance part of one subsystem."""
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError(f"{field} 必须是对象")
    result = empty_subsystem_contract(subsystem)
    type_value = value.get("type") or {}
    performance_value = value.get("performance") or {}
    if not isinstance(type_value, dict):
        raise ValueError(f"{field}.type 必须是对象")
    if not isinstance(performance_value, dict):
        raise ValueError(f"{field}.performance 必须是对象")
    code = subsystem.upper()
    if code == "C":
        result["type"] = {
            "service_type": _optional_text(type_value.get("service_type")),
            "technology": _enum(type_value.get("technology", "unknown"), COMMUNICATION_TECHNOLOGIES, f"{field}.type.technology", allow_null=False),
            "network_scope": _enum(type_value.get("network_scope", "unknown"), COMMUNICATION_NETWORK_SCOPES, f"{field}.type.network_scope", allow_null=False),
            "interfaces": _string_list(type_value.get("interfaces"), f"{field}.type.interfaces"),
        }
        result["performance"] = {
            "max_latency_s": _optional_nonnegative(performance_value.get("max_latency_s"), f"{field}.performance.max_latency_s"),
            "lost_link_threshold_s": _optional_nonnegative(performance_value.get("lost_link_threshold_s"), f"{field}.performance.lost_link_threshold_s"),
            "max_continuous_outage_s": _optional_nonnegative(performance_value.get("max_continuous_outage_s"), f"{field}.performance.max_continuous_outage_s"),
            "max_cumulative_outage_s": _optional_nonnegative(performance_value.get("max_cumulative_outage_s"), f"{field}.performance.max_cumulative_outage_s"),
            "min_availability": _optional_probability(performance_value.get("min_availability"), f"{field}.performance.min_availability"),
            "min_redundancy": _optional_positive_integer(performance_value.get("min_redundancy"), f"{field}.performance.min_redundancy"),
        }
    elif code == "N":
        result["type"] = {
            "technology": _enum(type_value.get("technology", "unknown"), NAVIGATION_TECHNOLOGIES, f"{field}.type.technology", allow_null=False),
        }
        result["performance"] = {
            "max_horizontal_error_m": _optional_nonnegative(performance_value.get("max_horizontal_error_m"), f"{field}.performance.max_horizontal_error_m"),
            "max_vertical_error_m": _optional_nonnegative(performance_value.get("max_vertical_error_m"), f"{field}.performance.max_vertical_error_m"),
            "integrity_required": _optional_value(performance_value.get("integrity_required")),
            "max_time_to_alert_s": _optional_nonnegative(performance_value.get("max_time_to_alert_s"), f"{field}.performance.max_time_to_alert_s"),
            "max_degradation_time_s": _optional_nonnegative(performance_value.get("max_degradation_time_s"), f"{field}.performance.max_degradation_time_s"),
            "min_availability": _optional_probability(performance_value.get("min_availability"), f"{field}.performance.min_availability"),
            "min_redundancy": _optional_positive_integer(performance_value.get("min_redundancy"), f"{field}.performance.min_redundancy"),
        }
    else:
        result["type"] = {
            "target_cooperation": _enum(type_value.get("target_cooperation"), SURVEILLANCE_TARGET_COOPERATION, f"{field}.type.target_cooperation"),
            "sensor_mode": _enum(type_value.get("sensor_mode"), SURVEILLANCE_SENSOR_MODES, f"{field}.type.sensor_mode"),
            "technology": _enum(type_value.get("technology", "unknown"), SURVEILLANCE_TECHNOLOGIES, f"{field}.type.technology", allow_null=False),
        }
        #: Round 2 additive：``service_subtype`` 是**合作监视服务的类型描述**，
        #: 显式给出时才保留（旧项目不会出现该键，输出形状与逐字节结果不变）。
        #: 它本身**不是** RID 判定谓词（ADS-B 亦可声称 cooperative_surveillance）。
        if "service_subtype" in type_value:
            subtype = _optional_text(type_value.get("service_subtype"))
            if subtype is not None:
                result["type"]["service_subtype"] = subtype
        #: Round 2.4 additive：``cooperative_surveillance_services`` 是**机载参与**
        #: 该合作监视服务的显式声明列表（``[{"technology": ..., "service_subtype": ...}]``）。
        #: 它由 :func:`cns_service_contract.cooperative_surveillance_declarations`
        #: 归一化，只在显式给出且至少有一条可用声明时保留；旧项目不出现该键，
        #: 因此输出形状与逐字节结果不变。
        if "cooperative_surveillance_services" in type_value:
            from .cns_service_contract import cooperative_surveillance_declarations

            declarations = cooperative_surveillance_declarations(type_value)
            if declarations:
                result["type"]["cooperative_surveillance_services"] = declarations
        result["performance"] = {
            "min_detection_range_m": _optional_nonnegative(performance_value.get("min_detection_range_m"), f"{field}.performance.min_detection_range_m"),
            "min_detection_probability": _optional_probability(performance_value.get("min_detection_probability"), f"{field}.performance.min_detection_probability"),
            "max_update_interval_s": _optional_nonnegative(performance_value.get("max_update_interval_s"), f"{field}.performance.max_update_interval_s"),
            "max_track_loss_s": _optional_nonnegative(performance_value.get("max_track_loss_s"), f"{field}.performance.max_track_loss_s"),
            "max_alert_latency_s": _optional_nonnegative(performance_value.get("max_alert_latency_s"), f"{field}.performance.max_alert_latency_s"),
            "min_availability": _optional_probability(performance_value.get("min_availability"), f"{field}.performance.min_availability"),
            "min_redundancy": _optional_positive_integer(performance_value.get("min_redundancy"), f"{field}.performance.min_redundancy"),
        }
    result["contingency"] = deepcopy(value.get("contingency"))
    result["source"] = _optional_text(value.get("source"))
    result["confirmed"] = _boolean(value.get("confirmed", False))
    result["confirmation_status"] = "confirmed" if result["confirmed"] else "pending_confirmation"
    return result


def sync_aliases(subsystem: str, value: dict, *, field: str) -> dict:
    """Synchronize canonical performance values with retained V1 aliases."""
    result = deepcopy(value)
    performance = result["performance"]
    code = subsystem.upper()
    if code == "C":
        canonical = performance.get("max_latency_s")
        legacy = _optional_nonnegative(result.get("latency_ms"), f"{field}.latency_ms")
        canonical = _coalesce_alias(canonical, None if legacy is None else legacy / 1000.0, f"{field}.max_latency_s/latency_ms")
        performance["max_latency_s"] = canonical
        result["latency_ms"] = None if canonical is None else canonical * 1000.0
    elif code == "N":
        canonical = performance.get("max_horizontal_error_m")
        legacy = _optional_nonnegative(result.get("accuracy_m"), f"{field}.accuracy_m")
        canonical = _coalesce_alias(canonical, legacy, f"{field}.max_horizontal_error_m/accuracy_m")
        performance["max_horizontal_error_m"] = canonical
        result["accuracy_m"] = canonical
        integrity = _coalesce_alias(performance.get("integrity_required"), _optional_value(result.get("integrity")), f"{field}.integrity_required/integrity")
        performance["integrity_required"] = integrity
        result["integrity"] = integrity
    elif code == "S":
        canonical = performance.get("max_update_interval_s")
        legacy = _optional_nonnegative(result.get("update_interval_s"), f"{field}.update_interval_s")
        canonical = _coalesce_alias(canonical, legacy, f"{field}.max_update_interval_s/update_interval_s")
        performance["max_update_interval_s"] = canonical
        result["update_interval_s"] = canonical
    else:
        raise ValueError("subsystem 必须是 C/N/S")
    canonical_redundancy = performance.get("min_redundancy")
    legacy_redundancy = _optional_positive_integer(result.get("redundancy"), f"{field}.redundancy")
    redundancy = _coalesce_alias(canonical_redundancy, legacy_redundancy, f"{field}.min_redundancy/redundancy")
    performance["min_redundancy"] = redundancy
    result["redundancy"] = redundancy
    return result


def _coalesce_alias(canonical, legacy, field):
    if canonical is None:
        return legacy
    if legacy is None:
        return canonical
    if isinstance(canonical, (int, float)) and isinstance(legacy, (int, float)):
        if abs(float(canonical) - float(legacy)) <= 1e-9:
            return canonical
    elif canonical == legacy:
        return canonical
    raise ValueError(f"{field} 别名值不一致")


def _enum(value, allowed, field, *, allow_null=True):
    if value in (None, ""):
        if allow_null:
            return None
        return "unknown"
    result = str(value).strip().lower()
    if result not in allowed:
        raise ValueError(f"{field} 无效：{result}")
    return result


def _optional_nonnegative(value, field):
    if value in (None, ""):
        return None
    number = float(value)
    if not isfinite(number) or number < 0:
        raise ValueError(f"{field} 不得小于零")
    return number


def _optional_probability(value, field):
    number = _optional_nonnegative(value, field)
    if number is not None and number > 1:
        raise ValueError(f"{field} 必须位于 0..1")
    return number


def _optional_positive_integer(value, field):
    if value in (None, ""):
        return None
    number = float(value)
    if not isfinite(number) or number <= 0 or not number.is_integer():
        raise ValueError(f"{field} 必须是正整数")
    return int(number)


def _string_list(value, field):
    if value in (None, ""):
        return []
    if isinstance(value, str):
        value = [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]
    if not isinstance(value, list):
        raise ValueError(f"{field} 必须是字符串数组")
    return [str(item).strip() for item in value if str(item).strip()]


def _optional_text(value):
    if value in (None, ""):
        return None
    return str(value).strip()


def _optional_value(value):
    return None if value in (None, "") else deepcopy(value)


def _boolean(value):
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "否", "")
    return bool(value)
