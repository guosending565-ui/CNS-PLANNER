"""Pure, deterministic evaluation of P5 safety-event definitions."""

from copy import deepcopy


SERVICE_STATES = {
    "available", "available_degraded", "contingency", "lost", "unknown",
    "not_applicable",
}


def _service_state(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("service_state", value.get("status", "unknown"))
    return "unknown"


def _context_applicability(definition, context):
    context = context or {}
    evidence = []
    for key in ("subsystem", "operational_condition", "flight_phase"):
        expected = definition.get(key)
        actual = context.get(key)
        if not expected:
            continue
        if actual is None:
            if key == "subsystem":
                continue
            return "unknown", [f"缺少用于判断 {key} 的运行上下文"], evidence
        evidence.append({"kind": "operational_context", "field": key, "value": actual})
        if str(actual).lower() != str(expected).lower():
            return "not_applicable", [f"{key} 不适用于当前运行上下文"], evidence
    return None, [], evidence


def evaluate_failure_condition(definition, service_state, operational_context=None):
    """Evaluate one FailureCondition without inferring severity or acceptability."""
    if not isinstance(definition, dict):
        raise ValueError("failure_condition 必须是对象")
    state = str(_service_state(service_state))
    if state not in SERVICE_STATES:
        state = "unknown"
    applicability, reasons, context_evidence = _context_applicability(
        definition, operational_context
    )
    evidence = context_evidence + [{"kind": "service_state", "value": state}]
    if applicability:
        return {
            "event_id": definition.get("failure_condition_id"),
            "status": applicability,
            "reasons": reasons,
            "evidence": evidence,
        }
    if state == "not_applicable":
        status, reasons = "not_applicable", ["上游服务状态不适用"]
    elif state == "unknown":
        status, reasons = "unknown", ["上游服务状态信息不足"]
    else:
        mode = definition.get("failure_mode", "unknown")
        observed = set((operational_context or {}).get("observed_failure_modes") or [])
        if mode == "loss":
            triggered = state == "lost"
            status = "triggered" if triggered else "not_triggered"
            reasons = [
                "服务状态为 lost，触发 loss failure condition"
                if triggered
                else f"服务状态为 {state}，不触发 loss failure condition"
            ]
        elif mode == "degradation":
            triggered = state == "available_degraded"
            status = "triggered" if triggered else "not_triggered"
            reasons = [
                "服务状态为 available_degraded，触发 degradation failure condition"
                if triggered
                else f"服务状态为 {state}，不触发 degradation failure condition"
            ]
        elif mode in observed:
            status, reasons = "triggered", [f"运行上下文确认发生 {mode} failure mode"]
        elif mode in {"erroneous", "untimely", "inadvertent"}:
            status, reasons = "unknown", [f"缺少 {mode} failure mode 的可观测证据"]
        else:
            status, reasons = "unknown", ["failure mode 未确认"]
    return {
        "event_id": definition.get("failure_condition_id"),
        "status": status,
        "reasons": reasons,
        "evidence": evidence,
    }


def evaluate_unacceptable_event(definition, failure_condition_results, operational_context=None):
    """Evaluate a UE only from explicit, confirmed UE policy and FC results."""
    if not isinstance(definition, dict):
        raise ValueError("unacceptable_event 必须是对象")
    applicability, reasons, evidence = _context_applicability(definition, operational_context)
    if applicability:
        return {
            "event_id": definition.get("unacceptable_event_id"),
            "status": applicability,
            "severity": definition.get("severity", "unknown"),
            "reasons": reasons,
            "evidence": evidence,
        }
    if not definition.get("confirmed") or definition.get("status") != "passed":
        return {
            "event_id": definition.get("unacceptable_event_id"),
            "status": "unknown",
            "severity": definition.get("severity", "unknown"),
            "reasons": ["unacceptable event 定义尚未确认，不能形成安全结论"],
            "evidence": evidence,
        }
    if isinstance(failure_condition_results, dict) and "status" in failure_condition_results:
        results = {failure_condition_results.get("event_id"): failure_condition_results}
    elif isinstance(failure_condition_results, dict):
        results = failure_condition_results
    else:
        results = {}
    refs = definition.get("failure_condition_refs") or []
    selected_results = [results.get(ref) for ref in refs]
    evidence.extend({
        "kind": "failure_condition",
        "event_ref": ref,
        "status": (result or {}).get("status", "unknown"),
    } for ref, result in zip(refs, selected_results))
    if not refs or any(result is None for result in selected_results):
        status, reasons = "unknown", ["缺少被引用 failure condition 的求值结果"]
    elif any(result.get("status") == "triggered" for result in selected_results):
        status, reasons = "triggered", ["已确认 UE 的关联 failure condition 被触发"]
    elif all(result.get("status") == "not_triggered" for result in selected_results):
        status, reasons = "not_triggered", ["关联 failure condition 均未触发"]
    elif all(result.get("status") == "not_applicable" for result in selected_results):
        status, reasons = "not_applicable", ["关联 failure condition 均不适用"]
    else:
        status, reasons = "unknown", ["关联 failure condition 结果不足"]
    return {
        "event_id": definition.get("unacceptable_event_id"),
        "status": status,
        "severity": definition.get("severity", "unknown"),
        "reasons": reasons,
        "evidence": evidence,
    }


def evaluate_safety_events(
    failure_condition, service_state, operational_context=None, unacceptable_event=None,
):
    """Return a pure preview; callers must not persist it as an assessment."""
    fc_result = evaluate_failure_condition(
        failure_condition, service_state, operational_context
    )
    result = {"failure_condition": fc_result, "unacceptable_event": None}
    if unacceptable_event is not None:
        result["unacceptable_event"] = evaluate_unacceptable_event(
            unacceptable_event,
            {fc_result.get("event_id"): deepcopy(fc_result)},
            operational_context,
        )
    return result
