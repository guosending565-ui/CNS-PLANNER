"""Read-only access to the checked-in Phase4-B7 delete-gate report."""

from __future__ import annotations

import json
from pathlib import Path


REPORT_PATH = Path(__file__).resolve().parents[2] / "docs" / "phase4_b7_delete_gates.json"


def load_delete_gate_report():
    return json.loads(REPORT_PATH.read_text(encoding="utf-8"))


def validate_delete_gate_report(report=None):
    value = report or load_delete_gate_report()
    required = {
        "production_imports", "production_api_actions", "frontend_production_actions",
        "project_state_writer", "default_selection", "service_fallback",
        "old_fixture_read", "tests_migrated",
    }
    for item in value.get("items") or []:
        checks = item.get("checks") or {}
        if set(checks) != required:
            raise ValueError(f"delete gate checks mismatch: {item.get('gate_id')}")
        failed = sorted(name for name, passed in checks.items() if not passed)
        if bool(item.get("ready_for_delete")) != (not failed):
            raise ValueError(f"delete gate readiness mismatch: {item.get('gate_id')}")
        if sorted(item.get("blockers") or []) != failed:
            raise ValueError(f"delete gate blockers mismatch: {item.get('gate_id')}")
    return value
