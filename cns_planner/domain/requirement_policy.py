"""JSON-safe operational-context and RequiredCNS policy contracts."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, TypedDict

from .cns_inputs import normalize_required_cns, pending_required_cns


CONTEXT_ENUMS = {
    "operation_mode": {"vlos", "bvlos", "bvlos_with_airspace_observer", "other", "unknown"},
    "airspace_context": {"controlled", "uncontrolled", "u_space", "other", "unknown"},
    "uas_traffic_context": {"single_uas", "multiple_uas", "unknown"},
    "traffic_mix": {"uas_only", "mixed_manned_unmanned", "unknown"},
    "manned_traffic_density": {"low", "medium", "high", "unknown"},
}
ALLOWED_CONTEXT_PATHS = frozenset(CONTEXT_ENUMS)
POLICY_SOURCE_TYPES = {
    "regulation", "standard", "guidance", "research", "project_rule",
    "engineering_assumption",
}
APPLICABILITY_OPERATORS = {"eq", "in", "contains"}
SUBSYSTEM_NAMES = ("communication", "navigation", "surveillance")


class ContextField(TypedDict, total=False):
    value: str
    source: str
    confirmed: bool
    status: str


class CNSOperationContext(TypedDict, total=False):
    status: str
    project_default: dict[str, ContextField]
    route_overrides: dict[str, dict[str, ContextField]]
    extension_tags: list[str]
    metadata: dict[str, Any]


def empty_operation_context() -> CNSOperationContext:
    return {
        "status": "pending_confirmation",
        "project_default": {name: _context_field(None, name) for name in CONTEXT_ENUMS},
        "route_overrides": {},
        "extension_tags": [],
        "metadata": {
            "semantics": "operational_context_input_not_requirement_or_capability",
        },
    }


def normalize_operation_context(value: dict | None) -> CNSOperationContext:
    result = empty_operation_context()
    if value is None:
        return result
    if not isinstance(value, dict):
        raise ValueError("cns_operation_context 必须是对象")
    project = value.get("project_default") or {}
    if not isinstance(project, dict):
        raise ValueError("cns_operation_context.project_default 必须是对象")
    result["project_default"] = {
        name: _context_field(project.get(name), name) for name in CONTEXT_ENUMS
    }
    overrides = value.get("route_overrides") or {}
    if not isinstance(overrides, dict):
        raise ValueError("cns_operation_context.route_overrides 必须是对象")
    result["route_overrides"] = {}
    for route_id, override in overrides.items():
        if not isinstance(override, dict):
            raise ValueError("route context override 必须是对象")
        unknown = set(override) - set(CONTEXT_ENUMS) - {"extension_tags"}
        if unknown:
            raise ValueError(f"不支持的运行上下文字段：{sorted(unknown)[0]}")
        result["route_overrides"][str(route_id)] = {
            name: _context_field(raw, name) for name, raw in override.items()
            if name in CONTEXT_ENUMS
        }
        if "extension_tags" in override:
            result["route_overrides"][str(route_id)]["extension_tags"] = _tags(override["extension_tags"])
    result["extension_tags"] = _tags(value.get("extension_tags") or [])
    result["metadata"] = deepcopy(value.get("metadata") or result["metadata"])
    fields = list(result["project_default"].values())
    result["status"] = "confirmed" if fields and all(item["status"] == "confirmed" for item in fields) else "pending_confirmation"
    return result


def empty_requirement_policies() -> dict:
    return {
        "status": "not_configured", "source": None, "count": 0, "items": [],
        "metadata": {
            "semantics": "explicit_project_policy_not_automatic_regulatory_compliance",
        },
    }


def normalize_requirement_policies(value: dict | list | None) -> dict:
    result = empty_requirement_policies()
    if value is None:
        return result
    if isinstance(value, list):
        value = {"items": value}
    if not isinstance(value, dict):
        raise ValueError("cns_requirement_policies 必须是对象或数组")
    items = value.get("items") or []
    if not isinstance(items, list):
        raise ValueError("cns_requirement_policies.items 必须是数组")
    normalized = sorted(
        (_normalize_policy(item, index) for index, item in enumerate(items)),
        key=lambda item: (item["policy_id"], item["version"]),
    )
    ids = [item["policy_id"] for item in normalized]
    if len(ids) != len(set(ids)):
        raise ValueError("policy_id 重复")
    result.update({
        "source": value.get("source"), "items": normalized, "count": len(normalized),
        "metadata": deepcopy(value.get("metadata") or result["metadata"]),
        "status": "not_configured" if not normalized else (
            "confirmed" if all(item["confirmed"] and item["status"] == "confirmed" for item in normalized)
            else "pending_confirmation"
        ),
    })
    return result


def empty_required_cns_recommendation() -> dict:
    return {
        "status": "not_calculated", "result_status": "not_calculated",
        "algorithm_id": None, "algorithm_version": None, "parameters": {},
        "input_fingerprint": None, "proposal_only": True,
        "requires_user_adoption": True, "context_snapshot": {},
        "matched_policies": [], "not_matched_policies": [],
        "unknown_policies": [], "recommended_required_cns": None,
        "field_provenance": {}, "conflicts": [], "missing_evidence": [],
        "current_vs_recommended_diff": [], "route_recommendations": {},
        "input_fingerprints": {},
    }


def empty_required_cns_adoption() -> dict:
    return {
        "status": "not_adopted", "recommendation_fingerprint": None,
        "algorithm_id": None, "algorithm_version": None,
        "context_fingerprint": None, "policy_fingerprint": None,
        "required_cns_fingerprint": None, "source": None,
    }


def effective_context(context: dict, route_id: str | None = None) -> dict:
    normalized = normalize_operation_context(context)
    result = deepcopy(normalized["project_default"])
    if route_id is not None:
        for name, field in (normalized.get("route_overrides", {}).get(str(route_id)) or {}).items():
            if name in CONTEXT_ENUMS:
                result[name] = deepcopy(field)
    return result


def normalize_partial_requirements(value: dict | None) -> dict:
    """Validate a policy partial using the canonical P3 RequiredCNS contract."""
    if not isinstance(value, dict) or not value:
        raise ValueError("policy.requirements 必须是非空 RequiredCNS partial")
    unknown = set(value) - set(SUBSYSTEM_NAMES)
    if unknown:
        raise ValueError(f"policy.requirements 不支持字段：{sorted(unknown)[0]}")
    partial = deepcopy(value)
    for subsystem, item in partial.items():
        if not isinstance(item, dict):
            raise ValueError(f"policy.requirements.{subsystem} 必须是对象")
        performance = item.setdefault("performance", {}) if any(
            key in item for key in ("latency_ms", "accuracy_m", "integrity", "update_interval_s", "redundancy")
        ) else item.get("performance")
        if performance is not None and not isinstance(performance, dict):
            raise ValueError(f"policy.requirements.{subsystem}.performance 必须是对象")
        if subsystem == "communication" and item.get("latency_ms") is not None:
            performance.setdefault("max_latency_s", float(item["latency_ms"]) / 1000.0)
        if subsystem == "navigation":
            if item.get("accuracy_m") is not None: performance.setdefault("max_horizontal_error_m", item["accuracy_m"])
            if item.get("integrity") is not None: performance.setdefault("integrity_required", item["integrity"])
        if subsystem == "surveillance" and item.get("update_interval_s") is not None:
            performance.setdefault("max_update_interval_s", item["update_interval_s"])
        if item.get("redundancy") is not None:
            performance.setdefault("min_redundancy", item["redundancy"])
        for alias in ("latency_ms", "accuracy_m", "integrity", "update_interval_s", "redundancy"):
            item.pop(alias, None)
    base = pending_required_cns()
    merged = deepcopy(base["project_default"])
    _deep_update(merged, partial)
    normalized = normalize_required_cns({"project_default": merged})["project_default"]
    return _extract_shape(normalized, partial)


def _normalize_policy(value, index):
    if not isinstance(value, dict):
        raise ValueError("requirement policy 必须是对象")
    policy_id = str(value.get("policy_id") or "").strip()
    if not policy_id:
        raise ValueError(f"policy[{index}] 缺少 policy_id")
    source_type = str(value.get("source_type") or "").strip()
    if source_type not in POLICY_SOURCE_TYPES:
        raise ValueError(f"policy {policy_id} source_type 无效")
    raw_applicability = value.get("applicability") or {"all_of": []}
    conditions = raw_applicability.get("all_of") if isinstance(raw_applicability, dict) else None
    if not isinstance(conditions, list):
        raise ValueError(f"policy {policy_id} applicability 仅支持 all_of")
    applicability = {"all_of": [_normalize_condition(item, policy_id) for item in conditions]}
    confirmed = value.get("confirmed") is True
    status = str(value.get("status") or ("confirmed" if confirmed else "pending_confirmation"))
    if confirmed and status not in ("confirmed", "active"):
        confirmed = False
    return {
        "policy_id": policy_id, "version": str(value.get("version") or "1.0"),
        "name": str(value.get("name") or policy_id), "source_type": source_type,
        "source": str(value.get("source") or "未记录"),
        "reference": str(value.get("reference") or ""),
        "clause": str(value.get("clause") or ""),
        "confirmed": confirmed, "status": "confirmed" if confirmed else "pending_confirmation",
        "applicability": applicability,
        "requirements": normalize_partial_requirements(value.get("requirements")),
    }


def _normalize_condition(value, policy_id):
    if not isinstance(value, dict):
        raise ValueError(f"policy {policy_id} applicability condition 必须是对象")
    field = str(value.get("field", value.get("field_path")) or "")
    operator = str(value.get("operator") or "eq")
    if field not in ALLOWED_CONTEXT_PATHS:
        raise ValueError(f"policy {policy_id} applicability field 不在白名单")
    if operator not in APPLICABILITY_OPERATORS:
        raise ValueError(f"policy {policy_id} applicability operator 无效")
    expected = deepcopy(value.get("value"))
    if operator == "in" and not isinstance(expected, list):
        raise ValueError(f"policy {policy_id} in 条件 value 必须是数组")
    return {"field": field, "operator": operator, "value": expected}


def _context_field(value, name):
    if isinstance(value, dict):
        raw = value.get("value", "unknown")
        source = str(value.get("source") or "未记录")
        confirmed = value.get("confirmed") is True
    else:
        raw, source, confirmed = value or "unknown", "legacy_unconfirmed", False
    raw = str(raw or "unknown")
    if raw not in CONTEXT_ENUMS[name]:
        raise ValueError(f"{name} 值无效")
    status = "confirmed" if confirmed and raw != "unknown" else "pending_confirmation"
    return {"value": raw, "source": source, "confirmed": confirmed, "status": status}


def _tags(value):
    if not isinstance(value, list):
        raise ValueError("extension_tags 必须是数组")
    return list(dict.fromkeys(str(item) for item in value if str(item).strip()))


def _deep_update(target, source):
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = deepcopy(value)


def _extract_shape(source, shape):
    result = {}
    for key, value in shape.items():
        result[key] = _extract_shape(source[key], value) if isinstance(value, dict) else deepcopy(source[key])
    return result
