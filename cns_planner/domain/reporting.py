"""P19 JSON-safe report records, source identity and disclosure controls."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import ntpath
import os
from pathlib import PurePosixPath
import re
from typing import Any, TypedDict


REPORT_SCHEMA_VERSION = "1.0"
TEMPLATE_VERSION = "cns-planning-report-zh-v1"
SECRET_KEYS = ("token", "password", "secret", "api_key", "apikey", "authorization")
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")


class ReportRecord(TypedDict, total=False):
    report_id: str
    schema_version: str
    template_version: str
    source_plan_id: str
    source_plan_status: str
    source_fingerprint: str
    report_data_fingerprint: str
    generated_at: str
    status: str
    current_applicability: str
    artifacts: dict[str, str]


def empty_report_collection():
    return {"status": "not_calculated", "active_report_id": None, "records": []}


def stable_fingerprint(value):
    return sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


def report_source_snapshot(state, algorithm_catalog):
    """Freeze only report inputs; no algorithm execution or mutable live handles."""
    keys = (
        "project", "confirmed_cns_plan", "cns_plan_review",
        "required_cns", "required_cns_recommendation", "required_cns_adoption",
        "cns_operation_context", "cns_requirement_policies",
        "operational_routes", "spatial_3d", "cns_corridor_policy",
        "cns_gap_analysis_v2", "cns_corridor_assessment",
        "cns_corridor_gap_assessment", "cns_corridor_site_plan",
        "existing_cns_facilities", "candidate_sites", "device_catalog",
        "data_source_profiles", "algorithm_selection", "grid_risk",
        "grid_attributes", "building_clearance_policy", "building_clearance_assessment",
        "encounter_3d_assessment",
        # V3-D operational adoption and its CNS assessment bundle.
        "v3_operational_adoptions", "v3_cns_assessment_bundle",
    )
    source = {key: deepcopy(state.get(key)) for key in keys}
    # Repository save timestamps are not planning evidence and must not break
    # deterministic report identity/idempotence.
    if isinstance(source.get("project"), dict):
        source["project"].pop("updated_at", None)
        source["project"].pop("revision", None)
    source["algorithm_manifests"] = deepcopy(algorithm_catalog or [])
    return sanitize_report_value(source)


def report_source_fingerprint(state, algorithm_catalog):
    return stable_fingerprint(report_source_snapshot(state, algorithm_catalog))


def deterministic_report_id(plan_id, source_fingerprint, template_version=TEMPLATE_VERSION):
    identity = stable_fingerprint([str(plan_id), str(source_fingerprint), str(template_version)])
    return f"RPT-{identity[:24]}"


def sanitize_report_value(value, key=""):
    """Redact credentials and machine paths while preserving logical provenance."""
    lower = str(key).lower()
    if any(secret in lower for secret in SECRET_KEYS):
        return "REDACTED"
    if isinstance(value, dict):
        return {str(item_key): sanitize_report_value(item, str(item_key)) for item_key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_report_value(item, key) for item in value]
    if isinstance(value, str) and _looks_absolute_path(value):
        return ntpath.basename(value.replace("/", "\\")) or PurePosixPath(value).name or "REDACTED_PATH"
    return deepcopy(value)


def mark_active_report_stale(state, reason):
    collection = state.get("cns_planning_reports") or {}
    active_id = collection.get("active_report_id")
    changed = False
    for record in collection.get("records") or []:
        if record.get("report_id") == active_id and record.get("current_applicability") != "stale_current_project":
            record["current_applicability"] = "stale_current_project"
            record["stale_reason"] = str(reason)
            changed = True
    if changed:
        collection["status"] = "stale"
        state["cns_planning_reports"] = collection
        state.setdefault("result_statuses", {})["report"] = "stale"
    return changed


def _looks_absolute_path(value):
    if value.startswith(("http://", "https://")):
        return False
    return bool(_WINDOWS_ABSOLUTE.match(value)) or value.startswith("/") or os.path.isabs(value)
