"""Pure functional-coupling evaluation without probabilistic assumptions."""

from copy import deepcopy
from math import isfinite


OBSERVATION_STATUSES = {
    "triggered", "not_triggered", "unknown", "not_applicable",
    "available", "available_degraded", "contingency", "lost",
}


def _time(value, field):
    if value is None or value == "":
        return None
    number = float(value)
    if not isfinite(number) or number < 0:
        raise ValueError(f"{field} 必须是非负有限秒数")
    return number


def normalize_event_observation(value):
    """Return the JSON-safe EventObservation contract and discard extra fields."""
    if not isinstance(value, dict):
        raise ValueError("EventObservation 必须是对象")
    reference = str(value.get("ref") or value.get("event_ref") or "").strip()
    if not reference:
        raise ValueError("EventObservation 缺少 ref")
    subsystem = str(value.get("subsystem") or "").upper()
    if subsystem not in {"C", "N", "S"}:
        raise ValueError(f"EventObservation subsystem 无效：{subsystem}")
    status = str(value.get("status") or "unknown")
    if status not in OBSERVATION_STATUSES:
        raise ValueError(f"EventObservation status 无效：{status}")
    start = _time(value.get("start_s"), "start_s")
    end = _time(value.get("end_s"), "end_s")
    if start is not None and end is not None and end < start:
        raise ValueError("EventObservation end_s 不得早于 start_s")
    source = value.get("source")
    return {
        "ref": reference,
        "subsystem": subsystem,
        "status": status,
        "start_s": start,
        "end_s": end,
        "source": str(source).strip() if source is not None and str(source).strip() else None,
    }


def _truth(status):
    if status in {"triggered", "lost"}:
        return "triggered"
    if status in {
        "not_triggered", "available", "available_degraded", "contingency",
    }:
        return "not_triggered"
    return status


def _expected_context(definition):
    expected = definition.get("operational_context")
    if expected is None:
        expected = {}
    if not isinstance(expected, dict):
        raise ValueError("operational_context 必须是对象")
    operational_condition = definition.get("operational_condition")
    if operational_condition is not None:
        expected = {**expected, "operational_condition": operational_condition}
    return expected


def _applicability(definitions, actual):
    actual = actual or {}
    if not isinstance(actual, dict):
        raise ValueError("OperationalContext 必须是对象")
    evidence = []
    for definition in definitions:
        if not definition:
            continue
        for field, expected in _expected_context(definition).items():
            if expected is None:
                continue
            if field not in actual or actual[field] is None:
                return "unknown", [f"缺少运行上下文：{field}"], evidence
            observed = actual[field]
            matches = (
                observed in expected if isinstance(expected, list)
                else str(observed).lower() == str(expected).lower()
            )
            evidence.append({
                "kind": "operational_context",
                "field": field,
                "expected": deepcopy(expected),
                "observed": deepcopy(observed),
            })
            if not matches:
                return "not_applicable", [f"运行上下文 {field} 不适用"], evidence
    return None, [], evidence


def _missing_time(observations, require_end=False):
    return any(
        item["start_s"] is None or (require_end and item["end_s"] is None)
        for item in observations
    )


def _overlap(observations):
    start = max(item["start_s"] for item in observations)
    end = min(item["end_s"] for item in observations)
    return max(0.0, end - start), start, end


def _temporal_rules(logic, observations, temporal):
    evidence = []
    max_separation = temporal.get("max_separation_s")
    min_overlap = temporal.get("min_overlap_s")
    if logic == "sequence":
        if _missing_time(observations):
            return "unknown", ["sequence 判定缺少 start_s"], evidence
        starts = [item["start_s"] for item in observations]
        evidence.append({"rule": "sequence", "start_s": starts})
        if any(current <= previous for previous, current in zip(starts, starts[1:])):
            return "not_triggered", ["事件顺序不符合 participants 定义"], evidence
        if max_separation is not None:
            gaps = [
                current - previous
                for previous, current in zip(starts, starts[1:])
            ]
            evidence.append({
                "rule": "max_separation_s",
                "limit_s": max_separation,
                "observed_s": gaps,
            })
            if any(gap > max_separation for gap in gaps):
                return "not_triggered", ["事件间隔超过 max_separation_s"], evidence
    if logic == "overlap" or min_overlap is not None:
        if _missing_time(observations, require_end=True):
            return "unknown", ["overlap 判定缺少 start_s/end_s"], evidence
        duration, start, end = _overlap(observations)
        evidence.append({
            "rule": "overlap",
            "overlap_start_s": start,
            "overlap_end_s": end,
            "overlap_duration_s": duration,
            "minimum_s": min_overlap,
        })
        if duration <= 0 or (min_overlap is not None and duration < min_overlap):
            return "not_triggered", ["事件无有效重叠或未达到 min_overlap_s"], evidence
    if logic == "all_of" and max_separation is not None:
        if _missing_time(observations):
            return "unknown", ["max_separation_s 判定缺少 start_s"], evidence
        starts = [item["start_s"] for item in observations]
        span = max(starts) - min(starts)
        evidence.append({
            "rule": "max_separation_s",
            "limit_s": max_separation,
            "observed_s": span,
        })
        if span > max_separation:
            return "not_triggered", ["事件间隔超过 max_separation_s"], evidence
    return None, [], evidence


def evaluate_coupled_condition(
    definition, observations, operational_context=None, functional_dependency=None,
):
    """Evaluate one confirmed coupled condition using event state and time only."""
    if not isinstance(definition, dict):
        raise ValueError("coupled_condition 必须是对象")
    if definition.get("logic") not in {"all_of", "sequence", "overlap"}:
        raise ValueError(f"coupled condition logic 无效：{definition.get('logic')}")
    normalized = [normalize_event_observation(item) for item in observations or []]
    by_ref = {item["ref"]: item for item in normalized}
    if len(by_ref) != len(normalized):
        raise ValueError("EventObservation ref 重复")
    result_base = {
        "event_id": definition.get("coupled_condition_id"),
        "probability": None,
        "probability_status": "not_calculated",
    }
    applicability, reasons, context_evidence = _applicability(
        [functional_dependency, definition], operational_context
    )
    if applicability:
        return {
            **result_base,
            "status": applicability,
            "reasons": reasons,
            "evidence": context_evidence,
            "temporal_evidence": [],
        }
    if not functional_dependency:
        return {
            **result_base,
            "status": "unknown",
            "reasons": ["缺少 functional dependency definition"],
            "evidence": context_evidence,
            "temporal_evidence": [],
        }
    if (
        not functional_dependency.get("confirmed")
        or functional_dependency.get("status") != "passed"
    ):
        return {
            **result_base,
            "status": "unknown",
            "reasons": ["functional dependency 尚未确认，仅可作为研究假设"],
            "evidence": context_evidence,
            "temporal_evidence": [],
        }
    if not definition.get("confirmed") or definition.get("status") != "passed":
        return {
            **result_base,
            "status": "unknown",
            "reasons": ["coupled condition 尚未确认"],
            "evidence": context_evidence,
            "temporal_evidence": [],
        }
    participants = list(definition.get("participants") or [])
    stages = functional_dependency.get("stages") or []
    stage_by_ref = {stage.get("event_ref"): stage for stage in stages}
    if definition.get("logic") == "sequence" and stages:
        ordered_stages = sorted(
            (
                stage for stage in stages
                if stage.get("event_ref") in participants
                and stage.get("order") is not None
            ),
            key=lambda stage: stage["order"],
        )
        if len(ordered_stages) == len(participants):
            participants = [stage["event_ref"] for stage in ordered_stages]
    selected = [by_ref.get(reference) for reference in participants]
    evidence = context_evidence + [{
        "kind": "event_observation",
        "event_ref": reference,
        "observation": deepcopy(observation),
    } for reference, observation in zip(participants, selected)]
    if not participants or any(item is None for item in selected):
        return {
            **result_base,
            "status": "unknown",
            "reasons": ["缺少 participant EventObservation"],
            "evidence": evidence,
            "temporal_evidence": [],
        }
    truths = [_truth(item["status"]) for item in selected]
    if any(value == "not_applicable" for value in truths):
        status, reasons = "not_applicable", ["至少一个 participant 不适用"]
    elif any(value == "not_triggered" for value in truths):
        status, reasons = "not_triggered", ["至少一个必要 participant 未触发"]
    elif any(value == "unknown" for value in truths):
        status, reasons = "unknown", ["至少一个 participant 状态未知"]
    else:
        temporal = dict(definition.get("temporal") or {})
        dependency_temporal = functional_dependency.get("time_constraints") or {}
        if temporal.get("max_separation_s") is None:
            temporal["max_separation_s"] = dependency_temporal.get(
                "max_stage_separation_s"
            )
        if temporal.get("min_overlap_s") is None:
            temporal["min_overlap_s"] = dependency_temporal.get(
                "min_stage_overlap_s"
            )
        temporal_status, temporal_reasons, temporal_evidence = _temporal_rules(
            definition.get("logic"), selected, temporal
        )
        if temporal_status:
            return {
                **result_base,
                "status": temporal_status,
                "reasons": temporal_reasons,
                "evidence": evidence,
                "temporal_evidence": temporal_evidence,
            }
        if definition.get("logic") == "sequence":
            starts = [item["start_s"] for item in selected]
            for index, reference in enumerate(participants[1:], start=1):
                stage_limit = (stage_by_ref.get(reference) or {}).get("max_delay_s")
                if stage_limit is None:
                    continue
                observed = starts[index] - starts[index - 1]
                temporal_evidence.append({
                    "rule": "stage_max_delay_s",
                    "event_ref": reference,
                    "limit_s": stage_limit,
                    "observed_s": observed,
                })
                if observed > stage_limit:
                    return {
                        **result_base,
                        "status": "not_triggered",
                        "reasons": [f"{reference} 超过 dependency stage max_delay_s"],
                        "evidence": evidence,
                        "temporal_evidence": temporal_evidence,
                    }
        status, reasons = "triggered", [
            f"{definition.get('logic')} participant 与时间约束均满足"
        ]
        return {
            **result_base,
            "status": status,
            "reasons": reasons,
            "evidence": evidence,
            "temporal_evidence": temporal_evidence,
        }
    return {
        **result_base,
        "status": status,
        "reasons": reasons,
        "evidence": evidence,
        "temporal_evidence": [],
    }


def evaluate_coupled_unacceptable_event(
    definition, coupled_condition_results, operational_context=None,
):
    """Promote a coupled condition only through an explicitly confirmed UE."""
    if not isinstance(definition, dict):
        raise ValueError("coupled_unacceptable_event 必须是对象")
    base = {
        "event_id": definition.get("coupled_unacceptable_event_id"),
        "severity": definition.get("severity", "unknown"),
        "probability": None,
        "probability_status": "not_calculated",
    }
    applicability, reasons, evidence = _applicability(
        [definition], operational_context
    )
    if applicability:
        return {**base, "status": applicability, "reasons": reasons, "evidence": evidence}
    if not definition.get("confirmed") or definition.get("status") != "passed":
        return {
            **base,
            "status": "unknown",
            "reasons": ["coupled unacceptable event 尚未确认，不能形成安全结论"],
            "evidence": evidence,
        }
    if (
        isinstance(coupled_condition_results, dict)
        and "status" in coupled_condition_results
    ):
        results = {
            coupled_condition_results.get("event_id"): coupled_condition_results
        }
    elif isinstance(coupled_condition_results, dict):
        results = coupled_condition_results
    else:
        results = {}
    references = definition.get("coupled_condition_refs") or []
    selected = [results.get(reference) for reference in references]
    evidence.extend({
        "kind": "coupled_condition",
        "event_ref": reference,
        "status": (result or {}).get("status", "unknown"),
    } for reference, result in zip(references, selected))
    if not references or any(result is None for result in selected):
        status, reasons = "unknown", ["缺少 coupled condition 求值结果"]
    elif any(result.get("status") == "triggered" for result in selected):
        status, reasons = "triggered", ["已确认 CoupledUE 的关联条件被触发"]
    elif all(result.get("status") == "not_triggered" for result in selected):
        status, reasons = "not_triggered", ["关联 coupled condition 均未触发"]
    elif all(result.get("status") == "not_applicable" for result in selected):
        status, reasons = "not_applicable", ["关联 coupled condition 均不适用"]
    else:
        status, reasons = "unknown", ["关联 coupled condition 结果不足"]
    return {**base, "status": status, "reasons": reasons, "evidence": evidence}


def evaluate_coupled_events(
    functional_dependency,
    coupled_condition,
    observations,
    operational_context=None,
    coupled_unacceptable_event=None,
):
    """Pure API result; no probability is calculated or persisted."""
    condition_result = evaluate_coupled_condition(
        coupled_condition,
        observations,
        operational_context,
        functional_dependency,
    )
    result = {
        "functional_dependency": {
            "dependency_id": (
                functional_dependency.get("dependency_id")
                if isinstance(functional_dependency, dict) else None
            ),
            "status": (
                functional_dependency.get("status", "unknown")
                if isinstance(functional_dependency, dict) else "unknown"
            ),
            "confirmed": bool(
                functional_dependency.get("confirmed")
                if isinstance(functional_dependency, dict) else False
            ),
        },
        "coupled_condition": condition_result,
        "coupled_unacceptable_event": None,
        "probability": None,
        "probability_status": "not_calculated",
    }
    if coupled_unacceptable_event is not None:
        result["coupled_unacceptable_event"] = evaluate_coupled_unacceptable_event(
            coupled_unacceptable_event,
            {condition_result.get("event_id"): condition_result},
            operational_context,
        )
    return result
