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
