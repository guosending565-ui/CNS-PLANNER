"""Explicit, versioned research-only parameters for the R0005 fresh run.

The only editable parameter is the pair of N middle-monitor host bindings.  It is
an *assumption* already present in the R0005 input, never a business threshold:
changing it changes the real N planning input and therefore the research input
fingerprint.  A caller cannot pass arbitrary keys, duplicate sites, a formal
project value or an unlisted site.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

PARAMETER_CONTRACT_VERSION = "c3b-research-parameters-1"
PARAMETER_ID = "navigation_middle_monitor_sites"
MIDDLE_SEGMENTS = (3, 4)
AUTHORITY = "research_isolated"


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def parameter_fingerprint(values):
    payload = {"contract_version": PARAMETER_CONTRACT_VERSION, "values": values}
    return "research-params-" + hashlib.sha256(
        canonical_json(payload).encode("utf-8")).hexdigest()


def _option_site(option):
    if not isinstance(option, dict):
        return None
    site_id = str(option.get("site_id") or "").strip()
    if not site_id:
        return None
    return {
        "site_id": site_id,
        "label": str(option.get("label") or site_id),
        "host_kind": str(option.get("host_kind") or "existing_host"),
        "subsystems": [str(value).upper() for value in option.get("subsystems") or []],
        "coordinate": list(option.get("coordinate") or []),
    }


def normalize_schema(schema):
    """Return the allowlisted schema or ``None`` when the runtime has no schema."""

    if not isinstance(schema, dict):
        return None
    if schema.get("contract_version") != PARAMETER_CONTRACT_VERSION:
        raise ValueError("research_parameter_contract_mismatch")
    if list(schema.get("ids") or []) != [PARAMETER_ID]:
        raise ValueError("research_parameter_allowlist_mismatch")
    options = [_option_site(item) for item in schema.get("options") or []]
    options = [item for item in options if item]
    if len(options) < 2 or len({item["site_id"] for item in options}) != len(options):
        raise ValueError("research_parameter_options_invalid")
    default = [str(value) for value in schema.get("default") or []]
    if (len(default) != 2 or len(set(default)) != 2
            or any(value not in {item["site_id"] for item in options} for value in default)):
        raise ValueError("research_parameter_default_invalid")
    segments = [int(value) for value in schema.get("middle_segments") or MIDDLE_SEGMENTS]
    if segments != list(MIDDLE_SEGMENTS):
        raise ValueError("research_parameter_segments_invalid")
    return {
        "contract_version": PARAMETER_CONTRACT_VERSION,
        "ids": [PARAMETER_ID],
        "middle_segments": list(MIDDLE_SEGMENTS),
        "options": options,
        "default": default,
        "label": str(schema.get("label") or "N 中段职责站点绑定（2 个）"),
        "description": str(schema.get("description") or (
            "研究用 N 中段监视宿主假设。改动会写入导航规划输入并生成新的输入指纹；"
            "它不改变任何业务门限，也不代表已安装或已批准。")),
        "authority": AUTHORITY,
    }


def default_values(schema):
    schema = normalize_schema(schema)
    if schema is None:
        raise ValueError("research_parameter_schema_missing")
    return {PARAMETER_ID: list(schema["default"])}


def validate_values(schema, supplied):
    """Validate request parameters and return a canonical value mapping.

    ``supplied`` may be ``None``/empty to request the frozen default.  Unknown
    keys, unknown sites and duplicates fail closed.
    """

    schema = normalize_schema(schema)
    if schema is None:
        if supplied in (None, {}):
            return None
        raise ValueError("research_parameter_schema_missing")
    supplied = supplied if supplied is not None else {}
    if not isinstance(supplied, dict):
        raise ValueError("research_parameters_must_be_object")
    unknown = set(supplied) - {PARAMETER_ID}
    if unknown:
        raise ValueError("unknown_research_parameter: " + ",".join(sorted(unknown)))
    values = supplied.get(PARAMETER_ID, schema["default"])
    if not isinstance(values, list) or len(values) != 2:
        raise ValueError("research_parameter_requires_two_sites")
    values = [str(value).strip() for value in values]
    if len(set(values)) != 2:
        raise ValueError("research_parameter_sites_must_be_distinct")
    allowed = {item["site_id"] for item in schema["options"]}
    if any(value not in allowed for value in values):
        raise ValueError("research_parameter_site_not_allowlisted")
    return {PARAMETER_ID: values}


def describe(values, schema, *, source_default=None):
    schema = normalize_schema(schema)
    values = deepcopy(values or {})
    canonical = values.get(PARAMETER_ID) or (schema or {}).get("default") or []
    default = list(source_default or (schema or {}).get("default") or [])
    return {
        "contract_version": PARAMETER_CONTRACT_VERSION,
        "authority": AUTHORITY,
        "values": values,
        "fingerprint": parameter_fingerprint(values),
        "middle_segments": list(MIDDLE_SEGMENTS),
        "source_default": default,
        "changed": bool(default) and list(canonical) != default,
        "semantics": (
            "research assumption only; changes the N planning input and its fingerprint, "
            "not a business threshold, not installed, not approved"),
    }


def apply_values(state, values):
    """Write the validated bindings into the research input (in place)."""

    values = values or {}
    sites = values.get(PARAMETER_ID)
    if not isinstance(sites, list) or len(sites) != 2:
        raise ValueError("research_parameter_requires_two_sites")
    planning = state.get("navigation_integrity_planning")
    if not isinstance(planning, dict):
        raise ValueError("navigation_integrity_planning_missing")
    selection = planning.get("selection")
    if not isinstance(selection, dict):
        raise ValueError("navigation_integrity_selection_missing")
    parameters = selection.setdefault("parameters", {})
    if not isinstance(parameters, dict):
        raise ValueError("navigation_integrity_parameters_invalid")
    parameters["middle_segment_indices"] = list(MIDDLE_SEGMENTS)
    planning["middle_bindings"] = [
        {"segment_index": segment, "site_id": site}
        for segment, site in zip(MIDDLE_SEGMENTS, sites)
    ]
    state["research_parameters"] = {
        "contract_version": PARAMETER_CONTRACT_VERSION,
        "authority": AUTHORITY,
        "values": deepcopy(values),
        "fingerprint": parameter_fingerprint(values),
        "semantics": "research_input_override_not_formal_project_state",
    }
    return verify_applied(state, values)


def verify_applied(state, values):
    sites = values.get(PARAMETER_ID) or []
    planning = state.get("navigation_integrity_planning") or {}
    bindings = planning.get("middle_bindings") or []
    expected = [
        {"segment_index": segment, "site_id": site}
        for segment, site in zip(MIDDLE_SEGMENTS, sites)
    ]
    if bindings != expected:
        raise ValueError("research_parameter_binding_mismatch")
    if ((planning.get("selection") or {}).get("parameters") or {}).get(
            "middle_segment_indices") != list(MIDDLE_SEGMENTS):
        raise ValueError("research_parameter_segment_mismatch")
    marker = state.get("research_parameters") or {}
    if marker.get("fingerprint") != parameter_fingerprint(values):
        raise ValueError("research_parameter_marker_mismatch")
    return {
        "contract_version": PARAMETER_CONTRACT_VERSION,
        "values": deepcopy(values),
        "fingerprint": parameter_fingerprint(values),
        "bindings": deepcopy(bindings),
        "verified": True,
    }


__all__ = [
    "MIDDLE_SEGMENTS", "PARAMETER_CONTRACT_VERSION", "PARAMETER_ID", "apply_values",
    "default_values", "describe", "normalize_schema", "parameter_fingerprint",
    "validate_values", "verify_applied",
]
