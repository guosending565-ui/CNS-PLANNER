"""JSON-safe static CNS service-model contract.

This contract describes what a provider can support under a declared model. It
does not represent current availability and is never sampled as runtime state.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, TypedDict


MODEL_FAMILIES = (
    "free_space_link_budget", "declared_performance",
    "external_service", "unsupported",
)


class ServiceModelSpec(TypedDict, total=False):
    model_family: str
    version: str | None
    technology: str
    applicability: dict[str, Any]
    parameters: dict[str, Any]
    source: str
    confirmed: bool
    status: str
    assumptions: list[str]
    limitations: list[str]
    references: list[str]


def empty_service_model():
    return {
        "model_family": "unsupported", "version": None,
        "technology": "unknown", "applicability": {}, "parameters": {},
        "source": "not_defined", "confirmed": False, "status": "missing_data",
        "assumptions": [], "limitations": [], "references": [],
    }


def normalize_service_model_spec(value):
    if value is None:
        return empty_service_model()
    if not isinstance(value, dict):
        raise ValueError("ServiceModelSpec 必须是对象")
    family = str(value.get("model_family") or "unsupported").strip().lower()
    if family not in MODEL_FAMILIES:
        raise ValueError(f"ServiceModelSpec.model_family 无效：{family}")
    applicability = value.get("applicability") or {}
    parameters = value.get("parameters") or {}
    if not isinstance(applicability, dict) or not isinstance(parameters, dict):
        raise ValueError("ServiceModelSpec applicability/parameters 必须是对象")
    confirmed = _boolean(value.get("confirmed", False))
    status = str(value.get("status") or ("confirmed" if confirmed and family != "unsupported" else "pending_confirmation" if value else "missing_data"))
    if family == "unsupported" and not confirmed:
        status = "missing_data"
    return {
        "model_family": family,
        "version": None if value.get("version") in (None, "") else str(value["version"]),
        "technology": str(value.get("technology") or "unknown").strip().lower(),
        "applicability": deepcopy(applicability),
        "parameters": deepcopy(parameters),
        "source": str(value.get("source") or "未记录"),
        "confirmed": confirmed, "status": status,
        "assumptions": _strings(value.get("assumptions"), "assumptions"),
        "limitations": _strings(value.get("limitations"), "limitations"),
        "references": _strings(value.get("references"), "references"),
    }


def _strings(value, field):
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"ServiceModelSpec.{field} 必须是字符串数组")
    return list(value)


def _boolean(value):
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "否", "")
    return bool(value)
