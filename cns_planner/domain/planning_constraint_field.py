"""Three-state fixed-cruise-layer Planning Constraint Field contract."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json


CONSTRAINT_OUTCOMES = ("pass", "blocked", "unknown")
BLOCKER_DOMAINS = ("terrain", "building", "tower", "airspace", "critical_site")
SCHEMA_VERSION = 1

#: 内部 canonical 垂向基准（terrain fact / AltitudeLayer / TowerObstacleProfile 同一语义）。
EGM2008_ORTHOMETRIC = "egm2008_orthometric"

#: unknown_policy 的**恒定**语义：无论用户如何配置都不改变。
#: ``unknown`` 永远是 unknown；provisional 穿越永远不等于"可发布"。
UNKNOWN_POLICY_INVARIANTS = {
    "unknown_remains_unknown": True,
    "operational_adoption_allowed": False,
}

#: 候选航路的适用性取值：``provisional_only`` 表示"仅供规划试算"。
CANDIDATE_APPLICABILITY = ("current", "provisional_only")

#: 用户开启 provisional 穿越时必须同时填写的依据字段。
PROVISIONAL_POLICY_AUTHORITY_FIELDS = ("source", "evidence")

#: 前端/后端共用的固定中文声明（逐字一致，避免两处措辞漂移）。
PROVISIONAL_POLICY_STATEMENT = (
    "仅允许生成候选航路用于规划试算，不代表安全通过；"
    "包含证据不足单元的候选不得发布为运行航路。"
)

#: 含证据不足单元的候选/航路的**阻断**文案（``{count}`` 为穿越的证据不足格数）。
PROVISIONAL_ROUTE_BLOCK_STATEMENT = (
    "候选航路包含 {count} 个证据不足单元，仅可用于规划试算，不能发布为运行航路。"
)

#: 候选/validation/adoption 三处共用的 reason_code。
CONTAINS_UNKNOWN_CONSTRAINTS_REASON = "contains_unknown_constraints"


def stable_constraint_fingerprint(value, *, prefix="pcf-"):
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return prefix + sha256(raw).hexdigest()


def normalize_unknown_policy(value=None):
    raw = value if isinstance(value, dict) else {}
    return {
        "allow_unknown_for_provisional": raw.get("allow_unknown_for_provisional") is True,
        **UNKNOWN_POLICY_INVARIANTS,
    }


def _authority_list(value):
    if isinstance(value, dict):
        return [deepcopy(value)]
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [deepcopy(item) for item in (value or [])]


def normalize_constraint_unknown_policy_configuration(value=None):
    """允许"候选航路试算穿越证据不足单元"的**显式确认**配置。

    语义（与用户契约逐字一致）：

    * 默认 ``false``；只有用户**显式请求** (``allow_unknown_for_provisional = true``)
      **并且同时提供** ``source`` 与 ``evidence`` 时才生效；
    * ``unknown_remains_unknown`` 与 ``operational_adoption_allowed = false`` 是恒定
      不变量：生效后 unknown 仍是 unknown，也**绝不**因此允许 operational adoption；
    * 未确认 / 缺依据 → 恒为 ``false``（fail-closed），PCF 保持 unknown 且不可穿越。
    """

    raw = value if isinstance(value, dict) else {}
    # ProjectState normalization may receive the already-normalized persisted
    # shape, where the effective flag lives under ``unknown_policy``.  Treat
    # normalization as idempotent; otherwise every session save silently
    # turns an explicitly confirmed Candidate Trial Mode back off.
    normalized_policy = (
        raw.get("unknown_policy")
        if isinstance(raw.get("unknown_policy"), dict)
        else {}
    )
    requested = (
        raw.get("allow_unknown_for_provisional") is True
        or raw.get("requested") is True
        or normalized_policy.get("allow_unknown_for_provisional") is True
    )
    source = raw.get("source")
    evidence = _authority_list(raw.get("evidence"))
    confirmed_request = raw.get("confirmed") is True or (
        requested and bool(source) and bool(evidence)
    )
    authority_complete = bool(source) and bool(evidence)
    allow = bool(requested and confirmed_request and authority_complete)
    if allow:
        status = "confirmed"
    elif requested:
        status = "pending_confirmation"
    else:
        status = "not_configured"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "requested": requested,
        "confirmed": allow,
        "authority_complete": authority_complete,
        "source": str(source) if allow and source else None,
        "evidence": evidence if allow else [],
        "unknown_policy": {
            "allow_unknown_for_provisional": allow,
            **UNKNOWN_POLICY_INVARIANTS,
        },
        "statement": PROVISIONAL_POLICY_STATEMENT,
        "semantics": {
            "explicit_user_confirmation_required": True,
            "provisional_only_never_publishable": True,
            "unknown_is_never_a_safe_pass": True,
            "operational_adoption_stays_blocked": True,
            "continuous_validation_stays_fail_closed": True,
            "not_configured_never_enables_traversal": True,
            "risk_mathematics_unchanged": True,
            "objective_weights_unchanged": True,
        },
    }


def empty_planning_constraint_field_collection():
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "not_calculated",
        "collection_id": "planning_constraint_fields",
        "count": 0,
        "items": [],
    }


def normalize_constraint_cell(value):
    raw = value if isinstance(value, dict) else {}
    outcome = str(raw.get("outcome") or "unknown")
    if outcome not in CONSTRAINT_OUTCOMES:
        outcome = "unknown"
    blocked_by = sorted({
        str(item) for item in raw.get("blocked_by") or [] if str(item) in BLOCKER_DOMAINS
    })
    reasons = [deepcopy(item) for item in raw.get("unknown_reasons") or []]
    if blocked_by:
        outcome = "blocked"
    elif reasons:
        outcome = "unknown"
    return {
        "grid_id": str(raw.get("grid_id") or ""),
        "outcome": outcome,
        "blocked_by": blocked_by,
        "unknown_reasons": reasons,
        "evidence_refs": [deepcopy(item) for item in raw.get("evidence_refs") or []],
    }


def summarize_constraint_cells(cells):
    values = [normalize_constraint_cell(item) for item in cells or []]
    counts = {name: sum(1 for item in values if item["outcome"] == name) for name in CONSTRAINT_OUTCOMES}
    blockers = {
        domain: sum(1 for item in values if domain in item["blocked_by"])
        for domain in BLOCKER_DOMAINS
    }
    return {"total": len(values), **counts, "blocked_by": blockers}


def normalize_planning_constraint_field(value):
    raw = value if isinstance(value, dict) else {}
    cells = [normalize_constraint_cell(item) for item in raw.get("cells") or []]
    summary = deepcopy(raw.get("counts")) if isinstance(raw.get("counts"), dict) else summarize_constraint_cells(cells)
    return {
        "schema_version": SCHEMA_VERSION,
        "field_id": str(raw.get("field_id") or ""),
        "status": str(raw.get("status") or "not_calculated"),
        "altitude_layer_id": str(raw.get("altitude_layer_id") or ""),
        "nominal_altitude_m": raw.get("nominal_altitude_m"),
        "vertical_reference": str(raw.get("vertical_reference") or ""),
        "workspace_identity": deepcopy(raw.get("workspace_identity")),
        "grid_identity": deepcopy(raw.get("grid_identity")),
        "policy_fingerprint": raw.get("policy_fingerprint"),
        "source_fingerprints": deepcopy(raw.get("source_fingerprints") or {}),
        "constraint_field_fingerprint": raw.get("constraint_field_fingerprint"),
        "unknown_policy": normalize_unknown_policy(raw.get("unknown_policy")),
        "counts": summary,
        "warnings": [deepcopy(item) for item in raw.get("warnings") or []],
        "stale_reason": raw.get("stale_reason"),
        "artifact_ref": deepcopy(raw.get("artifact_ref")),
        "cells": cells,
    }


def normalize_planning_constraint_field_collection(value):
    raw = value if isinstance(value, dict) else {}
    items = [
        normalize_planning_constraint_field(item)
        for item in raw.get("items") or [] if isinstance(item, dict)
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "status": str(raw.get("status") or ("passed" if items else "not_calculated")),
        "collection_id": "planning_constraint_fields",
        "count": len(items),
        "items": items,
    }


def planning_constraint_field_summary(field, *, artifact_ref=None):
    item = normalize_planning_constraint_field(field)
    item.pop("cells", None)
    if artifact_ref is not None:
        item["artifact_ref"] = deepcopy(artifact_ref)
    return item


__all__ = [
    "BLOCKER_DOMAINS", "CANDIDATE_APPLICABILITY", "CONSTRAINT_OUTCOMES",
    "CONTAINS_UNKNOWN_CONSTRAINTS_REASON", "EGM2008_ORTHOMETRIC",
    "PROVISIONAL_POLICY_AUTHORITY_FIELDS", "PROVISIONAL_POLICY_STATEMENT",
    "PROVISIONAL_ROUTE_BLOCK_STATEMENT", "SCHEMA_VERSION", "UNKNOWN_POLICY_INVARIANTS",
    "empty_planning_constraint_field_collection", "normalize_constraint_cell",
    "normalize_constraint_unknown_policy_configuration",
    "normalize_planning_constraint_field", "normalize_planning_constraint_field_collection",
    "normalize_unknown_policy", "planning_constraint_field_summary",
    "stable_constraint_fingerprint", "summarize_constraint_cells",
]
