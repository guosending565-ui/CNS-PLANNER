"""Pure effective CNS service-state evaluation.

Reliability statistics are intentionally absent: a statistical failure metric
must never be sampled to manufacture a current service state.
"""

from __future__ import annotations

from copy import deepcopy
from math import isfinite


EXTERNAL_STATES = ("available", "degraded", "unavailable", "unknown")
EFFECTIVE_STATES = ("available", "available_degraded", "contingency", "lost", "unknown")


def evaluate_service_state(
    required_cns: dict | None,
    aircraft_capability: dict | None,
    external_service_snapshot: dict | None,
    confirmed_fallback: dict | None = None,
) -> dict:
    required = required_cns or {}
    if required.get("required") is False:
        return _result("not_applicable", ["该分系统未被 RequiredCNS 要求"])
    if required.get("required") is not True or required.get("status") == "pending_confirmation":
        return _result("unknown", ["RequiredCNS 未确认"])

    snapshot = normalize_external_service_snapshot(external_service_snapshot)
    capability = aircraft_capability or {}
    main_capability, capability_evidence = _satisfies(required, capability, require_capability=True)
    external, external_evidence = _satisfies(required, snapshot)
    evidence = [
        {"kind": "aircraft_capability", **capability_evidence},
        {"kind": "external_service", "snapshot_status": snapshot["status"], **external_evidence},
    ]
    duration_ok, duration_evidence = _main_duration_satisfied(required, snapshot)
    evidence.append({"kind": "duration", **duration_evidence})
    if snapshot["status"] in ("available", "degraded") and main_capability is True and external is True and duration_ok is True:
        state = "available_degraded" if snapshot["status"] == "degraded" else "available"
        return _result(state, ["主服务满足 RequiredCNS"], evidence=evidence)

    fallback_candidates = list(capability.get("fallbacks") or [])
    if confirmed_fallback is not None:
        fallback_candidates.append(confirmed_fallback)
    fallback_result = _select_fallback(required, fallback_candidates, snapshot)
    evidence.extend(fallback_result["evidence"])
    if fallback_result["fallback"] is not None:
        return _result(
            "contingency", ["主服务不可用或不满足要求，已由确认的 fallback 接替"],
            evidence=evidence, fallback_used=fallback_result["fallback"],
        )

    reasons = []
    if snapshot["status"] == "unknown":
        reasons.append("外部服务当前状态未知")
    if main_capability is None:
        reasons.append("机载主能力信息不足")
    if (external is None or duration_ok is None) and snapshot["status"] != "unavailable":
        reasons.append("外部服务性能信息不足")
    reasons.extend(fallback_result["reasons"])
    known_failure = snapshot["status"] == "unavailable" or main_capability is False or external is False or duration_ok is False
    if known_failure:
        reasons.insert(0, "主服务不满足 RequiredCNS 且无有效 fallback")
        return _result("lost", reasons, evidence=evidence)
    return _result("unknown", reasons or ["有效服务状态证据不足"], evidence=evidence)


def normalize_external_service_snapshot(value: dict | None) -> dict:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("ExternalServiceSnapshot 必须是对象")
    status = str(value.get("status") or "unknown").strip().lower()
    if status not in EXTERNAL_STATES:
        raise ValueError(f"ExternalServiceSnapshot.status 无效：{status}")
    type_data, performance = value.get("type") or {}, value.get("performance") or {}
    if not isinstance(type_data, dict) or not isinstance(performance, dict):
        raise ValueError("ExternalServiceSnapshot type/performance 必须是对象")
    redundancy = _optional_positive_integer(value.get("redundancy"), "ExternalServiceSnapshot.redundancy")
    return {
        "status": status, "type": deepcopy(type_data), "performance": deepcopy(performance),
        "outage_duration_s": _optional_nonnegative(value.get("outage_duration_s"), "ExternalServiceSnapshot.outage_duration_s"),
        "degradation_duration_s": _optional_nonnegative(value.get("degradation_duration_s"), "ExternalServiceSnapshot.degradation_duration_s"),
        "redundancy": redundancy, "source": value.get("source"),
    }


def _select_fallback(required, candidates, snapshot):
    evidence, reasons = [], []
    elapsed = (
        snapshot["outage_duration_s"] if snapshot["status"] == "unavailable"
        else snapshot["degradation_duration_s"] if snapshot["status"] == "degraded"
        else 0.0 if snapshot["status"] == "available" else None
    )
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            evidence.append({"kind": "fallback", "index": index, "satisfied": False, "reason": "格式无效"})
            continue
        if candidate.get("confirmed") is not True:
            evidence.append({"kind": "fallback", "index": index, "satisfied": False, "reason": "fallback 未确认"})
            continue
        satisfied, detail = _satisfies(required, candidate)
        if satisfied is not True:
            evidence.append({"kind": "fallback", "index": index, **detail})
            continue
        limits = [candidate.get("max_bridge_time_s"), _required_duration_limit(required)]
        limits = [float(item) for item in limits if item not in (None, "")]
        if limits and elapsed is None:
            evidence.append({"kind": "fallback", "index": index, "satisfied": None, "reason": "缺少中断/降级持续时间"})
            reasons.append("无法确认 fallback bridge time")
            continue
        if limits and elapsed > min(limits) + 1e-12:
            evidence.append({"kind": "fallback", "index": index, "satisfied": False, "reason": "fallback bridge time 已超限", "elapsed_s": elapsed, "limit_s": min(limits)})
            reasons.append("确认的 fallback 已超过 bridge/允许时限")
            continue
        evidence.append({"kind": "fallback", "index": index, "satisfied": True, "elapsed_s": elapsed, "limit_s": min(limits) if limits else None})
        return {"fallback": {"index": index, "type": deepcopy(candidate.get("type") or {}), "performance": deepcopy(candidate.get("performance") or {}), "max_bridge_time_s": candidate.get("max_bridge_time_s"), "source": candidate.get("source")}, "evidence": evidence, "reasons": reasons}
    if candidates and not reasons:
        reasons.append("没有已确认且满足 RequiredCNS 的 fallback")
    elif not candidates:
        reasons.append("未配置 machine-readable fallback")
    return {"fallback": None, "evidence": evidence, "reasons": reasons}


def _satisfies(required, actual, *, require_capability=False):
    if not isinstance(actual, dict):
        return None, {"satisfied": None, "reason": "输入缺失"}
    if require_capability:
        confirmed = actual.get("confirmed") is True or actual.get("status") in ("confirmed", "passed")
        if not confirmed:
            return None, {"satisfied": None, "reason": "机载能力未确认"}
        if not actual.get("capabilities") and not _has_meaningful_type(actual.get("type")):
            return False, {"satisfied": False, "reason": "机载能力未声明"}
    required_type, actual_type = required.get("type") or {}, actual.get("type") or {}
    for key, expected in required_type.items():
        if expected in (None, "", "unknown", []):
            continue
        observed = actual_type.get(key)
        if observed in (None, "", "unknown", []):
            return None, {"satisfied": None, "reason": f"缺少类型字段 {key}"}
        if key == "interfaces":
            if not set(expected).issubset(set(observed)):
                return False, {"satisfied": False, "reason": "interfaces 不满足"}
        elif observed != expected:
            return False, {"satisfied": False, "reason": f"{key} 不匹配"}
    required_performance, actual_performance = required.get("performance") or {}, actual.get("performance") or {}
    aliases = {
        "max_latency_s": ("latency_s", "max_latency_s"),
        "max_horizontal_error_m": ("horizontal_error_m", "max_horizontal_error_m"),
        "max_vertical_error_m": ("vertical_error_m", "max_vertical_error_m"),
        "max_time_to_alert_s": ("time_to_alert_s", "max_time_to_alert_s"),
        "max_degradation_time_s": ("degradation_time_s", "max_degradation_time_s"),
        "max_update_interval_s": ("update_interval_s", "max_update_interval_s"),
        "max_track_loss_s": ("track_loss_s", "max_track_loss_s"),
        "max_alert_latency_s": ("alert_latency_s", "max_alert_latency_s"),
        "max_cumulative_outage_s": ("cumulative_outage_s", "max_cumulative_outage_s"),
        "min_detection_range_m": ("detection_range_m", "min_detection_range_m"),
        "min_detection_probability": ("detection_probability", "min_detection_probability"),
        "min_availability": ("availability", "min_availability"),
        "min_redundancy": ("redundancy", "min_redundancy"),
        "integrity_required": ("integrity", "integrity_required"),
    }
    for key, expected in required_performance.items():
        if expected in (None, ""):
            continue
        if key in ("lost_link_threshold_s", "max_continuous_outage_s", "max_degradation_time_s", "max_track_loss_s"):
            continue
        if key == "min_redundancy" and actual.get("redundancy") is not None:
            observed = actual["redundancy"]
        else:
            observed = next((actual_performance.get(name) for name in aliases.get(key, (key,)) if actual_performance.get(name) not in (None, "")), None)
        if observed is None:
            return None, {"satisfied": None, "reason": f"缺少性能字段 {key}"}
        if key.startswith("max_") and float(observed) > float(expected) + 1e-12:
            return False, {"satisfied": False, "reason": f"{key} 超限"}
        if key.startswith("min_") and float(observed) + 1e-12 < float(expected):
            return False, {"satisfied": False, "reason": f"{key} 不足"}
        if key == "integrity_required" and observed != expected:
            return False, {"satisfied": False, "reason": "integrity_required 不满足"}
    return True, {"satisfied": True, "reason": "类型与性能满足"}


def _required_duration_limit(required):
    performance = required.get("performance") or {}
    values = [
        performance.get("lost_link_threshold_s"),
        performance.get("max_continuous_outage_s"),
        performance.get("max_degradation_time_s"),
        performance.get("max_track_loss_s"),
    ]
    values = [float(item) for item in values if item not in (None, "")]
    return min(values) if values else None


def _main_duration_satisfied(required, snapshot):
    if snapshot["status"] == "available":
        return True, {"satisfied": True, "elapsed_s": 0.0, "limit_s": _required_duration_limit(required)}
    if snapshot["status"] == "unavailable":
        return False, {"satisfied": False, "reason": "主服务不可用"}
    if snapshot["status"] == "unknown":
        return None, {"satisfied": None, "reason": "主服务状态未知"}
    limit = _required_duration_limit(required)
    if limit is None:
        return True, {"satisfied": True, "elapsed_s": snapshot["degradation_duration_s"], "limit_s": None}
    elapsed = snapshot["degradation_duration_s"]
    if elapsed is None:
        return None, {"satisfied": None, "reason": "缺少退化持续时间", "limit_s": limit}
    passed = elapsed <= limit + 1e-12
    return passed, {"satisfied": passed, "elapsed_s": elapsed, "limit_s": limit, "reason": None if passed else "退化持续时间超限"}


def _has_meaningful_type(value):
    return isinstance(value, dict) and any(item not in (None, "", "unknown", []) for item in value.values())


def _result(state, reasons, *, evidence=None, fallback_used=None):
    return {
        "service_state": state, "status": state, "reasons": reasons,
        "evidence": evidence or [], "fallback_used": fallback_used,
    }


def _optional_nonnegative(value, field):
    if value in (None, ""):
        return None
    number = float(value)
    if not isfinite(number) or number < 0:
        raise ValueError(f"{field} 不得小于零")
    return number


def _optional_positive_integer(value, field):
    if value in (None, ""):
        return None
    number = float(value)
    if not isfinite(number) or number <= 0 or not number.is_integer():
        raise ValueError(f"{field} 必须是正整数")
    return int(number)
