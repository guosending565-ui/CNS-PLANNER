"""Schema-v2 safety assessment policy contracts and additive backfill.

These records are engineering-assessment inputs, not certification findings.
No severity classification or probability objective is inferred.
"""

from copy import deepcopy
from math import isfinite


FAILURE_MODES = {"loss", "degradation", "erroneous", "untimely", "inadvertent", "unknown"}
SEVERITIES = {"no_safety_effect", "minor", "major", "hazardous", "catastrophic", "unknown"}
SUBSYSTEMS = {"C", "N", "S"}
POLICY_STATUSES = {"missing_data", "pending_confirmation", "passed", "stale"}
DEPENDENCY_TYPES = {"functional", "information", "recovery"}
COUPLED_LOGICS = {"all_of", "sequence", "overlap"}


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


def _template_functional_dependency(
    identifier, name, high_level_function, dependency_type, subsystems, stages,
):
    return {
        "dependency_id": identifier,
        "name": name,
        "high_level_function": high_level_function,
        "dependency_type": dependency_type,
        "subsystems": subsystems,
        "stages": stages,
        "time_constraints": {
            "max_stage_separation_s": None,
            "min_stage_overlap_s": None,
        },
        "operational_condition": None,
        "severity": "unknown",
        "source": "project_template",
        "confirmed": False,
        "status": "pending_confirmation",
    }


def _template_coupled_condition(identifier, dependency_id, name, participants, logic):
    return {
        "coupled_condition_id": identifier,
        "functional_dependency_ref": dependency_id,
        "name": name,
        "participants": participants,
        "logic": logic,
        "temporal": {"max_separation_s": None, "min_overlap_s": None},
        "operational_context": {},
        "severity": "unknown",
        "source": "project_template",
        "confirmed": False,
        "status": "pending_confirmation",
    }


def _template_coupled_unacceptable_event(
    identifier, condition_id, name, subsystems,
):
    return {
        "coupled_unacceptable_event_id": identifier,
        "name": name,
        "subsystems": subsystems,
        "coupled_condition_refs": [condition_id],
        "operational_context": {},
        "effect": None,
        "severity": "unknown",
        "probability": None,
        "probability_status": "not_calculated",
        "source": "project_template",
        "confirmed": False,
        "status": "pending_confirmation",
    }


def _coupling_templates():
    dependencies = [
        _template_functional_dependency(
            "FD-CS-01",
            "Tactical conflict mitigation depends on surveillance and communication",
            "tactical_conflict_mitigation",
            "information",
            ["S", "C"],
            [
                {
                    "stage_id": "CS-S-DETECTION",
                    "event_ref": "OBS-S-CONFLICT-DETECTED",
                    "subsystem": "S",
                    "order": 1,
                    "max_delay_s": None,
                },
                {
                    "stage_id": "CS-C-ALERT",
                    "event_ref": "OBS-C-ALERT-DELIVERY-FAILURE",
                    "subsystem": "C",
                    "order": 2,
                    "max_delay_s": None,
                },
            ],
        ),
        _template_functional_dependency(
            "FD-CN-01",
            "Navigation recovery may depend on communication",
            "navigation_recovery",
            "recovery",
            ["N", "C"],
            [
                {
                    "stage_id": "CN-N-RECOVERY",
                    "event_ref": "OBS-N-RECOVERY-REQUIRED",
                    "subsystem": "N",
                    "order": 1,
                    "max_delay_s": None,
                },
                {
                    "stage_id": "CN-C-INTERVENTION",
                    "event_ref": "OBS-C-REMOTE-INTERVENTION-UNAVAILABLE",
                    "subsystem": "C",
                    "order": 2,
                    "max_delay_s": None,
                },
            ],
        ),
        _template_functional_dependency(
            "FD-NS-01",
            "Navigation information may affect surveillance or DAA",
            "surveillance_daa_state_estimation",
            "information",
            ["N", "S"],
            [
                {
                    "stage_id": "NS-N-STATE",
                    "event_ref": "OBS-N-STATE-QUALITY-DEGRADED",
                    "subsystem": "N",
                    "order": 1,
                    "max_delay_s": None,
                },
                {
                    "stage_id": "NS-S-DAA",
                    "event_ref": "OBS-S-DAA-DEPENDENCY-AFFECTED",
                    "subsystem": "S",
                    "order": 2,
                    "max_delay_s": None,
                },
            ],
        ),
    ]
    conditions = [
        _template_coupled_condition(
            "CC-CS-01", "FD-CS-01",
            "Conflict detection followed by ineffective alert delivery",
            ["OBS-S-CONFLICT-DETECTED", "OBS-C-ALERT-DELIVERY-FAILURE"],
            "sequence",
        ),
        _template_coupled_condition(
            "CC-CN-01", "FD-CN-01",
            "Navigation recovery required while remote intervention is unavailable",
            ["OBS-N-RECOVERY-REQUIRED", "OBS-C-REMOTE-INTERVENTION-UNAVAILABLE"],
            "all_of",
        ),
        _template_coupled_condition(
            "CC-NS-01", "FD-NS-01",
            "Navigation state degradation affects surveillance or DAA",
            ["OBS-N-STATE-QUALITY-DEGRADED", "OBS-S-DAA-DEPENDENCY-AFFECTED"],
            "all_of",
        ),
    ]
    coupled_events = [
        _template_coupled_unacceptable_event(
            "CUE-CS-01", "CC-CS-01",
            "Tactical conflict mitigation ineffective", ["C", "S"],
        ),
        _template_coupled_unacceptable_event(
            "CUE-CN-01", "CC-CN-01",
            "Navigation recovery unavailable", ["C", "N"],
        ),
        _template_coupled_unacceptable_event(
            "CUE-NS-01", "CC-NS-01",
            "Navigation uncertainty affects surveillance or DAA", ["N", "S"],
        ),
    ]
    return dependencies, conditions, coupled_events


def default_safety_policy():
    functions = {"C": "communication", "N": "navigation", "S": "surveillance"}
    dependencies, conditions, coupled_events = _coupling_templates()
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
        "functional_dependencies": dependencies,
        "coupled_conditions": conditions,
        "coupled_unacceptable_events": coupled_events,
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


def _nonnegative(value, field):
    if value is None or value == "":
        return None
    number = float(value)
    if not isfinite(number) or number < 0:
        raise ValueError(f"{field} 必须为非负有限数")
    return number


def _subsystems(value, field):
    result = _string_list(value, field)
    if len(result) < 2 or len(set(result)) != len(result):
        raise ValueError(f"{field} 必须包含至少两个不重复的 C/N/S 分系统")
    for item in result:
        if item not in SUBSYSTEMS:
            raise ValueError(f"{field} 包含无效分系统：{item}")
    return result


def _operational_context(value, operational_condition=None):
    if value is None:
        return (
            {"operational_condition": operational_condition}
            if operational_condition is not None else {}
        )
    if not isinstance(value, dict):
        raise ValueError("operational_context 必须是对象")
    return deepcopy(value)


def _temporal(value, max_key, min_key):
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("时间约束必须是对象")
    return {
        max_key: _nonnegative(value.get(max_key), max_key),
        min_key: _nonnegative(value.get(min_key), min_key),
    }


def normalize_functional_dependency(value):
    if not isinstance(value, dict):
        raise ValueError("functional dependency 必须是对象")
    identifier = str(value.get("dependency_id") or value.get("id") or "").strip()
    if not identifier:
        raise ValueError("functional dependency 缺少 dependency_id")
    dependency_type = str(value.get("dependency_type") or "")
    if dependency_type not in DEPENDENCY_TYPES:
        raise ValueError(f"dependency_type 无效：{dependency_type}")
    subsystems = _subsystems(value.get("subsystems"), "subsystems")
    stages_value = value.get("stages") or []
    if not isinstance(stages_value, list):
        raise ValueError("stages 必须是数组")
    stages = []
    for index, stage in enumerate(stages_value):
        if not isinstance(stage, dict):
            raise ValueError("dependency stage 必须是对象")
        event_ref = str(stage.get("event_ref") or "").strip()
        subsystem = str(stage.get("subsystem") or "").upper()
        if not event_ref:
            raise ValueError("dependency stage 缺少 event_ref")
        if subsystem not in subsystems:
            raise ValueError(f"dependency stage subsystem 不在依赖分系统中：{subsystem}")
        order = stage.get("order")
        if order is not None:
            if isinstance(order, bool) or int(order) != float(order) or int(order) < 1:
                raise ValueError("dependency stage order 必须为正整数")
            order = int(order)
        stages.append({
            "stage_id": str(stage.get("stage_id") or event_ref),
            "event_ref": event_ref,
            "subsystem": subsystem,
            "order": order,
            "max_delay_s": _nonnegative(stage.get("max_delay_s"), "max_delay_s"),
        })
    event_refs = [stage["event_ref"] for stage in stages]
    if len(event_refs) != len(set(event_refs)):
        raise ValueError("dependency stage event_ref 重复")
    severity = str(value.get("severity") or "unknown")
    if severity not in SEVERITIES:
        raise ValueError(f"severity 无效：{severity}")
    confirmed = bool(value.get("confirmed", False))
    return {
        "dependency_id": identifier,
        "name": str(value.get("name") or identifier),
        "high_level_function": _optional_text(value.get("high_level_function")),
        "dependency_type": dependency_type,
        "subsystems": subsystems,
        "stages": stages,
        "time_constraints": _temporal(
            value.get("time_constraints"),
            "max_stage_separation_s",
            "min_stage_overlap_s",
        ),
        "operational_condition": deepcopy(value.get("operational_condition")),
        "severity": severity,
        "source": _optional_text(value.get("source")),
        "confirmed": confirmed,
        "status": _status(value.get("status"), confirmed),
    }


def normalize_coupled_condition(value):
    if not isinstance(value, dict):
        raise ValueError("coupled condition 必须是对象")
    identifier = str(value.get("coupled_condition_id") or value.get("id") or "").strip()
    if not identifier:
        raise ValueError("coupled condition 缺少 coupled_condition_id")
    dependency_ref = str(value.get("functional_dependency_ref") or "").strip()
    if not dependency_ref:
        raise ValueError("coupled condition 缺少 functional_dependency_ref")
    logic = str(value.get("logic") or "")
    if logic not in COUPLED_LOGICS:
        raise ValueError(f"coupled condition logic 无效：{logic}")
    participants = _string_list(value.get("participants"), "participants")
    if len(participants) < 2 or len(set(participants)) != len(participants):
        raise ValueError("participants 必须包含至少两个不重复事件引用")
    severity = str(value.get("severity") or "unknown")
    if severity not in SEVERITIES:
        raise ValueError(f"severity 无效：{severity}")
    confirmed = bool(value.get("confirmed", False))
    return {
        "coupled_condition_id": identifier,
        "functional_dependency_ref": dependency_ref,
        "name": str(value.get("name") or identifier),
        "participants": participants,
        "logic": logic,
        "temporal": _temporal(
            value.get("temporal"), "max_separation_s", "min_overlap_s"
        ),
        "operational_context": _operational_context(
            value.get("operational_context"), value.get("operational_condition")
        ),
        "severity": severity,
        "source": _optional_text(value.get("source")),
        "confirmed": confirmed,
        "status": _status(value.get("status"), confirmed),
    }


def normalize_coupled_unacceptable_event(value):
    if not isinstance(value, dict):
        raise ValueError("coupled unacceptable event 必须是对象")
    identifier = str(
        value.get("coupled_unacceptable_event_id") or value.get("id") or ""
    ).strip()
    if not identifier:
        raise ValueError("coupled unacceptable event 缺少 ID")
    if value.get("probability") not in (None, ""):
        raise ValueError("P6 不接受 coupled probability；不得假设 C/N/S 独立")
    severity = str(value.get("severity") or "unknown")
    if severity not in SEVERITIES:
        raise ValueError(f"severity 无效：{severity}")
    confirmed = bool(value.get("confirmed", False))
    condition_refs = _string_list(
        value.get("coupled_condition_refs"), "coupled_condition_refs"
    )
    if not condition_refs:
        raise ValueError("coupled unacceptable event 至少需要一个 condition 引用")
    return {
        "coupled_unacceptable_event_id": identifier,
        "name": str(value.get("name") or identifier),
        "subsystems": _subsystems(value.get("subsystems"), "subsystems"),
        "coupled_condition_refs": condition_refs,
        "operational_context": _operational_context(
            value.get("operational_context"), value.get("operational_condition")
        ),
        "effect": _optional_text(value.get("effect")),
        "severity": severity,
        "probability": None,
        "probability_status": "not_calculated",
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
    functional_dependencies = _merge_templates(
        [
            normalize_functional_dependency(item)
            for item in value.get("functional_dependencies", [])
        ],
        template["functional_dependencies"],
        "dependency_id",
    )
    coupled_conditions = _merge_templates(
        [
            normalize_coupled_condition(item)
            for item in value.get("coupled_conditions", [])
        ],
        template["coupled_conditions"],
        "coupled_condition_id",
    )
    coupled_unacceptable_events = _merge_templates(
        [
            normalize_coupled_unacceptable_event(item)
            for item in value.get("coupled_unacceptable_events", [])
        ],
        template["coupled_unacceptable_events"],
        "coupled_unacceptable_event_id",
    )
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
    dependency_by_id = {
        item["dependency_id"]: item for item in functional_dependencies
    }
    condition_ids = {
        item["coupled_condition_id"] for item in coupled_conditions
    }
    if len(dependency_by_id) != len(functional_dependencies):
        raise ValueError("functional dependency ID 重复")
    if len(condition_ids) != len(coupled_conditions):
        raise ValueError("coupled condition ID 重复")
    coupled_ue_ids = {
        item["coupled_unacceptable_event_id"]
        for item in coupled_unacceptable_events
    }
    if len(coupled_ue_ids) != len(coupled_unacceptable_events):
        raise ValueError("coupled unacceptable event ID 重复")
    for condition in coupled_conditions:
        dependency = dependency_by_id.get(condition["functional_dependency_ref"])
        if dependency is None:
            raise ValueError(
                "coupled condition 引用了未知 functional dependency："
                f"{condition['functional_dependency_ref']}"
            )
        stage_refs = {stage["event_ref"] for stage in dependency["stages"]}
        unknown = set(condition["participants"]) - stage_refs if stage_refs else set()
        if unknown:
            raise ValueError(
                f"coupled condition participants 未在 dependency stages 定义：{sorted(unknown)}"
            )
    for event in coupled_unacceptable_events:
        unknown = set(event["coupled_condition_refs"]) - condition_ids
        if unknown:
            raise ValueError(
                f"coupled unacceptable event 引用了未知 condition：{sorted(unknown)}"
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
        "functional_dependencies": functional_dependencies,
        "coupled_conditions": coupled_conditions,
        "coupled_unacceptable_events": coupled_unacceptable_events,
    }
