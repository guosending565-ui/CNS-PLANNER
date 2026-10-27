"""C3-C：研究报告的加粗渲染、Radar 四段与 NM3 基线/规划投影区分。"""
from __future__ import annotations

import json

from cns_planner.application.research_report_service import (
    RADAR_CHAPTER_ORDER, build_report, report_model,
)


def _manifest():
    return {
        "run_id": "run-report", "task": "C3-C", "route_id": "R0005",
        "authority": "research_isolated", "contract_version": "contract-test",
        "created_at": "2026-10-27T00:00:00+0800",
        "input_version": {
            "id": "in-report", "input_fingerprint": "f" * 64,
            "parameter_fingerprint": "p" * 64,
            "communication_radius": {
                "communication_is_2km_by_surface": True,
                "semantics": "per_service_effective_radius_by_surface_from_device_catalog",
                "by_service": {"C:communication": {"land": 2000.0}},
            },
        },
        "authoritative_result_list_sha256": "r" * 64,
        "stages": {
            "P14": {"result_status": "failed", "result_sha256": "1" * 64,
                    "payload_sha256": "a" * 64},
            "P15": {"result_status": "failed", "result_sha256": "2" * 64,
                    "payload_sha256": "b" * 64},
            "P16": {"result_status": "proposal_ready", "result_sha256": "3" * 64,
                    "payload_sha256": "c" * 64},
            "P17": {"result_status": "fully_satisfied", "result_sha256": "4" * 64,
                    "payload_sha256": "d" * 64},
            "P18": {"result_status": "review_initialized", "result_sha256": "5" * 64,
                    "payload_sha256": "e" * 64, "confirmed": False, "applied": False,
                    "variants": [
                        {"source": "baseline", "not_applied_action_ids": []},
                        {"source": "p16_auto",
                         "selected_action_ids": ["a1", "a2", "a3", "a4"],
                         "not_applied_action_ids": ["middle-1", "middle-2"]},
                    ]},
        },
        "business_state": {
            "P14": {"status": "failed", "subsystems": {"C:passed": 1, "S:pending": 1}},
            "P15": {"status": "failed", "subsystems": {"C:passed": 1, "S:failed": 1}},
            "P16": {"status": "proposal_ready", "selected_action_count": 4,
                    "candidate_action_count": 11,
                    "cost_summary": {"cross_unit_total": None}},
            "communication_C": {"verdict": "satisfied"},
            "navigation_RID": {"verdict": "satisfied"},
            "navigation_N": {"verdict": "unknown"},
            "surveillance_Radar": {
                "formal_status": "not_calculated", "verdict": "unknown",
                "radar_surveillance_layout_present": False,
                "reason": "radar_layout_missing", "semantics": "缺 layout ⇒ 不计算",
            },
            "surveillance_S": {
                "verdict": "unknown",
                "voxel_counts": {"satisfied": 0, "unknown": 2772, "confirmed_deficit": 0},
            },
        },
        "gates": {
            "p17_step6_gate": {
                "status": "fully_satisfied", "post_plan_status": "fully_satisfied",
                "applied_action_ids": ["a1", "a2", "a3", "a4"],
            },
            "p18_initialize": {"status": "review_initialized", "allowed": True,
                               "blocking_reason": None},
        },
        "admission": {
            "p18_confirmation_allowed": False,
            "p18_confirm_gate": {"PV-1": "evidence_required", "PV-2": "evidence_required"},
        },
        "nm3_supplement": {
            "contract_version": "navigation_integrity_supplement_v1",
            "policy_id": "navigation_integrity_endpoint_middle_reuse_v1",
            "policy_version": "1.0", "input_fingerprint": "nav-1",
            "actual_delivery": "unknown", "proposal_only": True,
            "responsibilities": [
                {"responsibility_id": "R0005:origin", "role": "origin",
                 "segment_index": None, "planning_status": "unknown",
                 "installed_providers": [], "hypothetical_providers": []},
                {"responsibility_id": "R0005:destination", "role": "destination",
                 "segment_index": None, "planning_status": "unknown",
                 "installed_providers": [], "hypothetical_providers": []},
                {"responsibility_id": "R0005:middle:0", "role": "middle",
                 "segment_index": 3, "planning_status": "unknown",
                 "installed_providers": [], "hypothetical_providers": []},
                {"responsibility_id": "R0005:middle:1", "role": "middle",
                 "segment_index": 4, "planning_status": "unknown",
                 "installed_providers": [], "hypothetical_providers": []},
            ],
            "residual_gaps": [],
        },
        "map_projection": {
            "counts": {"sites": 7, "devices": 18, "existing_hosts": 5,
                       "planned_new_sites": 2, "n_actions": 4},
            "n_actions": [
                {"action_id": "add_navigation_integrity_monitor:R0005:origin:N005",
                 "site_id": "N005", "segment_index": None, "kind": "other",
                 "planner_family": "endpoint_integrity_monitor",
                 "planning_status": "proposal_only"},
                {"action_id": "add_navigation_integrity_monitor:R0005:destination:N006",
                 "site_id": "N006", "segment_index": None, "kind": "other",
                 "planner_family": "endpoint_integrity_monitor",
                 "planning_status": "proposal_only"},
                {"action_id": "middle-1", "site_id": "NEW-422750", "segment_index": 4,
                 "kind": "middle",
                 "planner_family": "navigation_integrity_endpoint_middle_reuse_v1",
                 "planning_status": "proposal_only"},
                {"action_id": "middle-2", "site_id": "NEW-425000", "segment_index": 3,
                 "kind": "middle",
                 "planner_family": "navigation_integrity_endpoint_middle_reuse_v1",
                 "planning_status": "proposal_only"},
            ],
        },
        "research_parameters": {"contract_version": "c3b-research-parameters-1",
                                "fingerprint": "params", "values": {}},
        "research_limitations": [],
        "provenance": {"source_run_id": "source", "source_state_sha256": "s" * 64},
    }


def test_report_html_renders_bold_and_inline_code():
    report = build_report(_manifest())
    html = report["html"]
    assert "<strong>" in html
    assert "**" not in html
    assert "<code>" in html
    assert "Radar 四段说明" in report["markdown"]


def test_radar_chapter_has_the_required_four_parts_in_order():
    model = report_model(_manifest())
    radar = model["radar"]
    assert tuple(radar["chapter_order"]) == RADAR_CHAPTER_ORDER
    assert [item["title"] for item in radar["paragraphs"]] == list(RADAR_CHAPTER_ORDER)
    text = " ".join(item["text"] for item in radar["paragraphs"])
    assert "radar_surveillance_layout" in text
    assert "P14/P15" in text
    assert "P14→P15→P18" in text


def test_nm3_baseline_and_planning_projection_are_separate():
    model = report_model(_manifest())
    assert len(model["nm3_baseline"]["responsibilities"]) == 4
    assert all(row["planning_status"] == "unknown"
               for row in model["nm3_baseline"]["responsibilities"])
    actions = model["planning_projection"]["actions"]
    assert [row["role"] for row in actions] == ["endpoint", "endpoint", "middle", "middle"]
    assert model["planning_projection"]["p17_post_plan_status"] == "fully_satisfied"
    variants = model["planning_projection"]["p18_variants"]
    assert variants[-1]["not_applied_action_ids"] == ["middle-1", "middle-2"]
    markdown = build_report(_manifest())["markdown"]
    assert "## 4. NM3 基线（pre-plan）" in markdown
    assert "### 4.1 P16/P17/P18 规划投影（proposal only）" in markdown
    assert "**基线与投影的区别：**" in markdown


def test_api_handler_encodes_text_responses_without_json_wrapping():
    import io
    import json
    from cns_planner.api.server import ApiHandler

    class FakeHandler(ApiHandler):
        def __init__(self):
            self.wfile = io.BytesIO()
            self.status = None
            self.headers = []

        def send_response(self, status):
            self.status = status

        def send_header(self, key, value):
            self.headers.append((key, value))

        def end_headers(self):
            pass

    handler = FakeHandler()
    handler.respond("<!doctype html>\n<html>报告</html>", "text/html; charset=utf-8")
    assert handler.wfile.getvalue() == "<!doctype html>\n<html>报告</html>".encode("utf-8")
    handler.wfile = io.BytesIO()
    handler.respond({"ok": True})
    assert json.loads(handler.wfile.getvalue()) == {"ok": True}


def test_report_keeps_unknown_statuses_out_of_pass_language():
    model = report_model(_manifest())
    assert model["radar"]["formal_status"] == "not_calculated"
    assert model["radar"]["verdict"] == "unknown"
    assert model["coverage"]["S"]["verdict"] == "unknown"
    assert model["p18"]["confirmation_allowed"] is False
    assert model["confirmation_allowed"] is False
    assert model["formal_release_allowed"] is False
