"""Minimal JSON-safe Phase4 assumption registry."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from enum import StrEnum

from .workflow_contract import AssessmentOutcome, InputRequirementLevel


ASSUMPTION_REGISTRY_SCHEMA_VERSION = "phase4-b1"
ASSUMPTION_SCOPES = frozenset({"project", "node", "artifact"})
ASSUMPTION_BASES = frozenset({
    "software_baseline",
    "engineering_baseline",
    "user_declaration",
    "source_limitation",
})


class AssumptionAuthorityEffect(StrEnum):
    ALLOWED_WITH_DISCLOSURE = "allowed_with_disclosure"
    PROVISIONAL_ONLY = "provisional_only"


class AssumptionStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    WITHDRAWN = "withdrawn"


def empty_assumption_registry() -> dict:
    return {"schema_version": ASSUMPTION_REGISTRY_SCHEMA_VERSION, "items": []}


def normalize_assumption(value: dict, *, now: str | None = None) -> dict:
    if not isinstance(value, dict):
        raise ValueError("assumption 必须是对象")
    assumption_id = _required_text(value, "assumption_id")
    scope = _required_text(value, "scope")
    if scope not in ASSUMPTION_SCOPES:
        raise ValueError("assumption.scope 必须是 project/node/artifact")
    node_id = _optional_text(value.get("node_id"))
    if scope == "node" and node_id is None:
        raise ValueError("node scope assumption 必须声明 node_id")
    basis = _required_text(value, "basis")
    if basis not in ASSUMPTION_BASES:
        raise ValueError(f"assumption.basis 无效：{basis}")
    try:
        authority_effect = AssumptionAuthorityEffect(
            value.get("authority_effect")
        ).value
    except (TypeError, ValueError) as exc:
        raise ValueError("assumption.authority_effect 无效") from exc
    try:
        status = AssumptionStatus(value.get("status") or "active").value
    except ValueError as exc:
        raise ValueError("assumption.status 无效") from exc

    created_at = _optional_text(value.get("created_at")) or now or _utc_now()
    expires_at = _optional_text(value.get("expires_at"))
    _parse_datetime(created_at, "assumption.created_at")
    if expires_at is not None:
        _parse_datetime(expires_at, "assumption.expires_at")

    invalidates_on = value.get("invalidates_on") or []
    if not isinstance(invalidates_on, (list, tuple)):
        raise ValueError("assumption.invalidates_on 必须是数组")

    return {
        "assumption_id": assumption_id,
        "scope": scope,
        "node_id": node_id,
        "field": _required_text(value, "field"),
        "value": deepcopy(value.get("value")),
        "unit": _optional_text(value.get("unit")),
        "basis": basis,
        "reason": _required_text(value, "reason"),
        "source_ref": deepcopy(value.get("source_ref")),
        "owner": _required_text(value, "owner"),
        "confirmed": value.get("confirmed") is True,
        "authority_effect": authority_effect,
        "report_disclosure": _required_text(value, "report_disclosure"),
        "created_at": created_at,
        "expires_at": expires_at,
        "invalidates_on": _unique_strings(invalidates_on, "invalidates_on"),
        "status": status,
    }


def validate_assumption(value: dict, *, now: str | None = None) -> dict:
    """Validate one item and return its normalized, JSON-safe form."""

    return normalize_assumption(value, now=now)


def normalize_assumption_registry(value: dict | None) -> dict:
    if value is None:
        return empty_assumption_registry()
    if not isinstance(value, dict):
        raise ValueError("assumptions registry 必须是对象")
    items = value.get("items") or []
    if not isinstance(items, list):
        raise ValueError("assumptions.items 必须是数组")
    normalized = [normalize_assumption(item) for item in items]
    ids = [item["assumption_id"] for item in normalized]
    if len(ids) != len(set(ids)):
        raise ValueError("assumption_id 重复")
    return {
        "schema_version": ASSUMPTION_REGISTRY_SCHEMA_VERSION,
        "items": normalized,
    }


def add_assumption(registry: dict | None, assumption: dict) -> dict:
    result = normalize_assumption_registry(registry)
    normalized = normalize_assumption(assumption)
    if any(
        item["assumption_id"] == normalized["assumption_id"]
        for item in result["items"]
    ):
        raise ValueError(f"assumption_id 已存在：{normalized['assumption_id']}")
    result["items"].append(normalized)
    return result


def supersede_assumption(registry: dict, assumption_id: str) -> dict:
    return _set_status(registry, assumption_id, AssumptionStatus.SUPERSEDED)


def withdraw_assumption(registry: dict, assumption_id: str) -> dict:
    return _set_status(registry, assumption_id, AssumptionStatus.WITHDRAWN)


def read_assumptions(
    registry: dict | None,
    assumption_ids: list[str] | tuple[str, ...] | None = None,
    *,
    active_only: bool = False,
) -> list[dict]:
    normalized = normalize_assumption_registry(registry)
    wanted = None if assumption_ids is None else {str(item) for item in assumption_ids}
    return [
        deepcopy(item)
        for item in normalized["items"]
        if (wanted is None or item["assumption_id"] in wanted)
        and (not active_only or is_assumption_active(item))
    ]


def read_assumption(
    registry: dict | None,
    assumption_id: str,
    *,
    active_only: bool = False,
) -> dict | None:
    """Read one item by ID without exposing registry-owned mutable data."""

    items = read_assumptions(
        registry, [str(assumption_id)], active_only=active_only
    )
    return items[0] if items else None


def active_assumption_ids(registry: dict | None) -> tuple[str, ...]:
    return tuple(
        item["assumption_id"]
        for item in read_assumptions(registry, active_only=True)
    )


def find_active_assumption_for_field(
    registry: dict | None,
    field: str,
    *,
    node_id: str | None = None,
) -> dict | None:
    for item in read_assumptions(registry, active_only=True):
        if item["field"] != field:
            continue
        if item["scope"] == "node" and item["node_id"] != node_id:
            continue
        return item
    return None


def validate_active_assumption_ids(
    registry: dict | None, assumption_ids: list[str] | tuple[str, ...]
) -> tuple[str, ...]:
    ids = tuple(dict.fromkeys(str(item) for item in assumption_ids))
    if not ids:
        raise ValueError("ready_with_assumptions 必须引用 assumption_id")
    active = set(active_assumption_ids(registry))
    invalid = [item for item in ids if item not in active]
    if invalid:
        raise ValueError(f"assumption 不是 active：{invalid[0]}")
    return ids


def validate_assumption_application(
    *,
    requirement_level: InputRequirementLevel | str,
    assessment_before: AssessmentOutcome | str | None = None,
    assessment_after: AssessmentOutcome | str | None = None,
) -> None:
    """Enforce the two non-negotiable assumption boundaries."""

    level = InputRequirementLevel(requirement_level)
    if level == InputRequirementLevel.REQUIRED:
        raise ValueError("REQUIRED dependency 不得通过 assumption 降级")
    if assessment_before is not None and assessment_after is not None:
        before = AssessmentOutcome(assessment_before)
        after = AssessmentOutcome(assessment_after)
        if before == AssessmentOutcome.UNKNOWN and after == AssessmentOutcome.PASSED:
            raise ValueError("assumption 不得把 unknown assessment 变为 passed")


def is_assumption_active(value: dict, *, at: str | None = None) -> bool:
    item = normalize_assumption(value)
    if item["status"] != AssumptionStatus.ACTIVE.value:
        return False
    if item["expires_at"] is None:
        return True
    moment = _parse_datetime(at or _utc_now(), "at")
    return _parse_datetime(item["expires_at"], "expires_at") > moment


def _set_status(registry, assumption_id, status):
    result = normalize_assumption_registry(registry)
    found = False
    for item in result["items"]:
        if item["assumption_id"] == str(assumption_id):
            item["status"] = status.value
            found = True
            break
    if not found:
        raise KeyError(assumption_id)
    return result


def _required_text(value, field):
    text = _optional_text(value.get(field))
    if text is None:
        raise ValueError(f"assumption.{field} 必填")
    return text


def _optional_text(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _unique_strings(value, field):
    result = []
    for raw in value:
        item = str(raw or "").strip()
        if not item:
            raise ValueError(f"assumption.{field} 不得包含空值")
        if item not in result:
            result.append(item)
    return result


def _parse_datetime(value, field):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} 必须是 RFC3339/ISO8601 时间") from exc
    return result if result.tzinfo is not None else result.replace(tzinfo=timezone.utc)


def _utc_now():
    return datetime.now(timezone.utc).isoformat()
