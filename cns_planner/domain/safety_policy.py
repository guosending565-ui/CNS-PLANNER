"""Schema-v2 safety assessment policy contracts and additive backfill.

These records are engineering-assessment inputs, not certification findings.
No severity classification or probability objective is inferred.
"""

from copy import deepcopy


FAILURE_MODES = {"loss", "degradation", "erroneous", "untimely", "inadvertent", "unknown"}
SEVERITIES = {"no_safety_effect", "minor", "major", "hazardous", "catastrophic", "unknown"}
SUBSYSTEMS = {"C", "N", "S"}
POLICY_STATUSES = {"missing_data", "pending_confirmation", "passed", "stale"}


def _template_failure_condition(subsystem, function):
    return {
        "failure_condition_id": f"FC-{subsystem}-01",
        "name": f"Loss of {function} service",
        "subsystem": subsystem,
        "function": function,
        "failure_mode": "loss",
        "service_state_trigger": "lost",
        "operational_condition": None,
        "flight_phase": None,
        "local_effect": None,
        "system_effect": None,
        "operation_effect": None,
        "severity": "unknown",
        "mitigations": [],
        "safety_objective": None,
        "source": "project_template",
        "confirmed": False,
        "status": "pending_confirmation",
    }


def _template_unacceptable_event(subsystem, function):
    return {
        "unacceptable_event_id": f"UE-{subsystem}-01",
        "name": f"Unacceptable consequence associated with {function} service loss",
        "subsystem": subsystem,
        "failure_condition_refs": [f"FC-{subsystem}-01"],
        "operational_condition": None,
        "flight_phase": None,
        "effect": None,
        "severity": "unknown",
        "mitigations": [],
        "safety_objective": None,
        "probability": None,
        "source": "project_template",
        "confirmed": False,
        "status": "pending_confirmation",
    }


def default_safety_policy():
    functions = {"C": "communication", "N": "navigation", "S": "surveillance"}
    return {
        "status": "pending_confirmation",
        "source": "project_template",
        "confirmed": False,
        "failure_conditions": [
            _template_failure_condition(key, value) for key, value in functions.items()
        ],
        "unacceptable_events": [
            _template_unacceptable_event(key, value) for key, value in functions.items()
        ],
        "fault_trees": [],
        "fmea_records": [],
    }


def _optional_text(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _string_list(value, field):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} 必须是数组")
    return [str(item).strip() for item in value if str(item).strip()]


def _probability(value, field="probability"):
    if value is None or value == "":
        return None
    number = float(value)
    if not 0 <= number <= 1:
        raise ValueError(f"{field} 必须在 0..1")
    return number


def _status(value, confirmed):
    if not confirmed:
        return value if value in {"missing_data", "pending_confirmation", "stale"} else "pending_confirmation"
    return value if value in POLICY_STATUSES else "passed"


def normalize_failure_condition(value):
    if not isinstance(value, dict):
        raise ValueError("failure condition 必须是对象")
    identifier = str(value.get("failure_condition_id") or value.get("id") or "").strip()
    if not identifier:
        raise ValueError("failure condition 缺少 failure_condition_id")
    subsystem = str(value.get("subsystem") or "").upper()
    if subsystem not in SUBSYSTEMS:
        raise ValueError(f"failure condition subsystem 无效：{subsystem}")
    failure_mode = str(value.get("failure_mode") or "unknown")
    if failure_mode not in FAILURE_MODES:
        raise ValueError(f"failure_mode 无效：{failure_mode}")
    severity = str(value.get("severity") or "unknown")
    if severity not in SEVERITIES:
        raise ValueError(f"severity 无效：{severity}")
    confirmed = bool(value.get("confirmed", False))
    return {
        "failure_condition_id": identifier,
        "name": str(value.get("name") or identifier),
        "subsystem": subsystem,
        "function": _optional_text(value.get("function")),
        "failure_mode": failure_mode,
        "service_state_trigger": str(
            value.get("service_state_trigger") or ("lost" if failure_mode == "loss" else failure_mode)
        ),
        "operational_condition": _optional_text(value.get("operational_condition")),
        "flight_phase": _optional_text(value.get("flight_phase")),
        "local_effect": _optional_text(value.get("local_effect")),
        "system_effect": _optional_text(value.get("system_effect")),
        "operation_effect": _optional_text(value.get("operation_effect")),
        "severity": severity,
        "mitigations": _string_list(value.get("mitigations"), "mitigations"),
        "safety_objective": deepcopy(value.get("safety_objective")),
        "source": _optional_text(value.get("source")),
        "confirmed": confirmed,
        "status": _status(value.get("status"), confirmed),
    }


def normalize_unacceptable_event(value):
    if not isinstance(value, dict):
        raise ValueError("unacceptable event 必须是对象")
    identifier = str(value.get("unacceptable_event_id") or value.get("id") or "").strip()
    if not identifier:
        raise ValueError("unacceptable event 缺少 unacceptable_event_id")
    subsystem = str(value.get("subsystem") or "").upper()
    if subsystem not in SUBSYSTEMS:
        raise ValueError(f"unacceptable event subsystem 无效：{subsystem}")
    severity = str(value.get("severity") or "unknown")
    if severity not in SEVERITIES:
        raise ValueError(f"severity 无效：{severity}")
    confirmed = bool(value.get("confirmed", False))
    return {
        "unacceptable_event_id": identifier,
        "name": str(value.get("name") or identifier),
        "subsystem": subsystem,
        "failure_condition_refs": _string_list(
            value.get("failure_condition_refs"), "failure_condition_refs"
        ),
        "operational_condition": _optional_text(value.get("operational_condition")),
        "flight_phase": _optional_text(value.get("flight_phase")),
        "effect": _optional_text(value.get("effect")),
        "severity": severity,
        "mitigations": _string_list(value.get("mitigations"), "mitigations"),
        "safety_objective": deepcopy(value.get("safety_objective")),
        "probability": _probability(value.get("probability")),
        "source": _optional_text(value.get("source")),
        "confirmed": confirmed,
        "status": _status(value.get("status"), confirmed),
    }


def normalize_fault_tree(value):
    if not isinstance(value, dict):
        raise ValueError("fault tree 必须是对象")
    identifier = str(value.get("fault_tree_id") or value.get("id") or "").strip()
    if not identifier:
        raise ValueError("fault tree 缺少 fault_tree_id")
    root = value.get("top_event", value.get("root"))
    if not isinstance(root, dict):
        raise ValueError("fault tree 缺少 top_event")
    confirmed = bool(value.get("confirmed", False))
    return {
        "fault_tree_id": identifier,
        "name": str(value.get("name") or identifier),
        "subsystem": _optional_text(value.get("subsystem")),
        "top_event": deepcopy(root),
        "source": _optional_text(value.get("source")),
        "confirmed": confirmed,
        "status": _status(value.get("status"), confirmed),
    }


def normalize_fmea_record(value):
    if not isinstance(value, dict):
        raise ValueError("FMEA record 必须是对象")
    identifier = str(value.get("failure_mode_id") or value.get("id") or "").strip()
    if not identifier:
        raise ValueError("FMEA record 缺少 failure_mode_id")
    subsystem = str(value.get("subsystem") or "").upper()
    if subsystem not in SUBSYSTEMS:
        raise ValueError(f"FMEA subsystem 无效：{subsystem}")
    failure_mode = str(value.get("failure_mode") or "unknown")
    if failure_mode not in FAILURE_MODES:
        raise ValueError(f"failure_mode 无效：{failure_mode}")
    confirmed = bool(value.get("confirmed", False))
    return {
        "failure_mode_id": identifier,
        "function": _optional_text(value.get("function")),
        "component": _optional_text(value.get("component")),
        "subsystem": subsystem,
        "failure_mode": failure_mode,
        "local_effect": _optional_text(value.get("local_effect")),
        "next_effect": _optional_text(value.get("next_effect")),
        "end_effect": _optional_text(value.get("end_effect")),
        "detection": deepcopy(value.get("detection")),
        "mitigation": deepcopy(value.get("mitigation")),
        "failure_condition_refs": _string_list(
            value.get("failure_condition_refs"), "failure_condition_refs"
        ),
        "unacceptable_event_refs": _string_list(
            value.get("unacceptable_event_refs"), "unacceptable_event_refs"
        ),
        "source": _optional_text(value.get("source")),
        "confirmed": confirmed,
        "status": _status(value.get("status"), confirmed),
    }


def _merge_templates(records, templates, identifier):
    current = {record[identifier]: record for record in records}
    ordered = [current.pop(template[identifier], template) for template in templates]
    ordered.extend(current.values())
    return ordered


def normalize_safety_policy(value):
    """Normalize an additive schema-v2 policy and validate traceability refs."""
    template = default_safety_policy()
    if value is None:
        return template
    if not isinstance(value, dict):
        raise ValueError("safety_policy 必须是对象")
    failure_conditions = _merge_templates(
        [normalize_failure_condition(item) for item in value.get("failure_conditions", [])],
        template["failure_conditions"],
        "failure_condition_id",
    )
    unacceptable_events = _merge_templates(
        [normalize_unacceptable_event(item) for item in value.get("unacceptable_events", [])],
        template["unacceptable_events"],
        "unacceptable_event_id",
    )
    fault_trees = [normalize_fault_tree(item) for item in value.get("fault_trees", [])]
    fmea_records = [normalize_fmea_record(item) for item in value.get("fmea_records", [])]
    fc_ids = {item["failure_condition_id"] for item in failure_conditions}
    ue_ids = {item["unacceptable_event_id"] for item in unacceptable_events}
    if len(fc_ids) != len(failure_conditions) or len(ue_ids) != len(unacceptable_events):
        raise ValueError("safety policy 记录 ID 重复")
    for event in unacceptable_events:
        unknown = set(event["failure_condition_refs"]) - fc_ids
        if unknown:
            raise ValueError(
                f"unacceptable event 引用了未知 failure condition：{sorted(unknown)}"
            )
    for record in fmea_records:
        unknown_fc = set(record["failure_condition_refs"]) - fc_ids
        unknown_ue = set(record["unacceptable_event_refs"]) - ue_ids
        if unknown_fc or unknown_ue:
            raise ValueError(
                "FMEA 引用无效："
                f"failure_conditions={sorted(unknown_fc)}, "
                f"unacceptable_events={sorted(unknown_ue)}"
            )
    confirmed = bool(value.get("confirmed", False))
    return {
        "status": _status(value.get("status"), confirmed),
        "source": _optional_text(value.get("source")) or "project_template",
        "confirmed": confirmed,
        "failure_conditions": failure_conditions,
        "unacceptable_events": unacceptable_events,
        "fault_trees": fault_trees,
        "fmea_records": fmea_records,
    }
