from copy import deepcopy
import hashlib
import json

import pytest

from cns_planner.application.research_map_service import build_map_projection
from cns_planner.application.research_parameters import (
    PARAMETER_CONTRACT_VERSION, PARAMETER_ID, apply_values, parameter_fingerprint,
    validate_values,
)
from cns_planner.application.research_report_service import build_report
from cns_planner.application.research_run_service import fresh_inputs


def schema():
    return {
        "contract_version": PARAMETER_CONTRACT_VERSION,
        "ids": [PARAMETER_ID],
        "middle_segments": [3, 4],
        "default": ["SITE-A", "SITE-B"],
        "options": [
            {"site_id": "SITE-A", "label": "A", "host_kind": "planned_new_site",
             "subsystems": ["C", "N", "S"], "coordinate": [122.1, 29.8]},
            {"site_id": "SITE-B", "label": "B", "host_kind": "planned_new_site",
             "subsystems": ["C", "N", "S"], "coordinate": [122.2, 29.8]},
            {"site_id": "SITE-C", "label": "C", "host_kind": "existing_host",
             "subsystems": ["C", "N", "S"], "coordinate": [122.3, 29.8]},
        ],
    }


def state():
    return {
        "navigation_integrity_planning": {
            "selection": {"parameters": {}},
            "middle_bindings": [
                {"segment_index": 3, "site_id": "SITE-A"},
                {"segment_index": 4, "site_id": "SITE-B"},
            ],
        }
    }


def test_parameter_change_is_explicit_and_changes_identity():
    default = validate_values(schema(), None)
    assert default[PARAMETER_ID] == ["SITE-A", "SITE-B"]
    changed = validate_values(schema(), {PARAMETER_ID: ["SITE-C", "SITE-B"]})
    assert changed != default
    assert parameter_fingerprint(changed) != parameter_fingerprint(default)
    prepared = state()
    applied = apply_values(prepared, changed)
    assert applied["verified"] is True
    assert prepared["navigation_integrity_planning"]["middle_bindings"][0] == {
        "segment_index": 3, "site_id": "SITE-C"}
    assert prepared["navigation_integrity_planning"]["selection"]["parameters"][
        "middle_segment_indices"] == [3, 4]


@pytest.mark.parametrize("value", [
    {PARAMETER_ID: ["SITE-A"]},
    {PARAMETER_ID: ["SITE-A", "SITE-A"]},
    {PARAMETER_ID: ["SITE-A", "SITE-X"]},
    {"unknown": ["SITE-A", "SITE-B"]},
])
def test_unknown_or_duplicate_parameters_fail_closed(value):
    with pytest.raises(ValueError):
        validate_values(schema(), value)


def test_fresh_inputs_require_allowlisted_parameter_and_source(tmp_path):
    source = tmp_path / "source"
    (source / "runtime").mkdir(parents=True)
    (source / "runtime" / "project_state.json").write_text("{}", encoding="utf-8")
    dispatch = {"research_fresh_input": {
        "authority": "research_isolated", "root": str(source),
        "source_run_id": "source", "route_id": "R0005",
        "parameter_schema": schema(),
    }}
    payload = {"mode": "fresh_compute", "run_id": "run-1",
               "parameters": {PARAMETER_ID: ["SITE-C", "SITE-B"]}}
    result = fresh_inputs(dispatch, payload)
    assert result["research_parameters"]["values"][PARAMETER_ID] == ["SITE-C", "SITE-B"]
    assert result["fresh_input"]["root"] == str(source.resolve())
    with pytest.raises(ValueError):
        fresh_inputs(dispatch, {**payload, "parameters": {PARAMETER_ID: ["SITE-A", "SITE-X"]}})
    with pytest.raises(ValueError):
        fresh_inputs(dispatch, {**payload, "run_id": "../escape"})


def _facility(site_id, coordinate, devices, planned):
    return {
        "site_id": site_id, "facility_id": "F-" + site_id,
        "name": site_id, "coordinate": coordinate,
        "metadata": {"new_physical_site": planned, "proposal_only": planned},
        "planning_profile": {"reuse_class": "greenfield" if planned else "existing_shared_site"},
        "devices": devices,
    }


def _map_fixture():
    devices_a = [
        {"device_id": "C-A", "service_key": "C:communication", "subsystem": "C",
         "coverage_geometry": {"radius_by_surface": {"land": 2000.0, "sea": 2000.0}}},
        {"device_id": "S-A", "service_key": "S:rid_cooperative", "subsystem": "S",
         "coverage_geometry": {"radius_by_surface": {"land": 2000.0}}},
        {"device_id": "N-A", "service_key": "N:navigation_integrity_monitoring",
         "subsystem": "N", "coverage_geometry": {"radius_by_surface": {"land": 1000.0}}},
    ]
    source = {
        "operational_routes": [{"route_id": "R0005", "path": [[122.1, 29.8], [122.3, 29.9]]}],
        "existing_cns_facilities": {"items": [
            _facility("A", [122.1, 29.8], devices_a, False),
            _facility("B", [122.3, 29.9], deepcopy(devices_a), True),
        ]},
    }
    actions = [
        {"action_id": "navigation_integrity_endpoint:R0005:origin:A", "action_type": "x",
         "site_id": "A", "coordinate": [122.1, 29.8]},
        {"action_id": "navigation_integrity_endpoint:R0005:destination:B", "action_type": "x",
         "site_id": "B", "coordinate": [122.3, 29.9]},
        {"action_id": "navigation_integrity_endpoint_middle_reuse_v1:R0005:B:4",
         "action_type": "add_navigation_integrity_middle_monitor", "site_id": "B",
         "coordinate": [122.3, 29.9], "segment_index": 4},
        {"action_id": "navigation_integrity_endpoint_middle_reuse_v1:R0005:A:3",
         "action_type": "add_navigation_integrity_middle_monitor", "site_id": "A",
         "coordinate": [122.1, 29.8], "segment_index": 3},
    ]
    manifest = {
        "run_id": "run-map", "authority": "research_isolated",
        "business_state": {
            "communication_C": {"verdict": "satisfied"},
            "navigation_RID": {"verdict": "satisfied"},
            "surveillance_Radar": {"verdict": "unknown", "formal_status": "not_calculated",
                                   "reason": "radar_layout_missing"},
            "surveillance_S": {"verdict": "unknown", "voxel_counts": {"unknown": 12}},
            "navigation_N": {"verdict": "unknown"},
        },
    }
    return source, actions, manifest


def test_map_distinguishes_hosts_actions_and_never_marks_unknown_satisfied():
    source, actions, manifest = _map_fixture()
    projection = build_map_projection(source, {"selected_actions": actions}, manifest)
    assert projection["counts"] == {
        "sites": 2, "devices": 6, "existing_hosts": 1,
        "planned_new_sites": 1, "n_actions": 4,
    }
    layers = {layer["id"]: layer for layer in projection["layers"]}
    assert layers["C"]["status"] == "satisfied"
    assert layers["Radar"]["status"] == "not_calculated"
    assert layers["Radar"]["conclusion"] == "unknown"
    assert layers["S"]["status"] == "unknown"
    assert layers["N"]["feature_count"] == 4
    assert {site["kind"] for site in projection["sites"]} == {
        "existing_host", "planned_new_site"}


def test_report_contains_required_contract_and_uses_manifest_identity():
    source, actions, manifest = _map_fixture()
    manifest.update({
        "created_at": "2026-10-27T08:00:00+0800",
        "task": "C3-B", "route_id": "R0005",
        "formal_project_state_written": False, "raw_data_written": False,
        "git_written": False,
        "input_version": {
            "id": "in-c3b-test", "input_fingerprint": "input-sha",
            "parameter_fingerprint": "param-sha",
            "communication_radius": {"communication_is_2km_by_surface": True,
                                     "semantics": "device"},
        },
        "contract_version": "contract-1",
        "contract_versions": {"p17_schema": "contract-1"},
        "authoritative_result_list_sha256": "list-sha",
        "stages": {stage: {"result_sha256": stage + "-sha", "payload_sha256": stage + "-payload",
                           "result_status": status, "authority": "research_isolated"}
                   for stage, status in (("P14", "failed"), ("P15", "failed"),
                                         ("P16", "proposal_ready"), ("P17", "fully_satisfied"),
                                         ("P18", "review_initialized"))},
        "gates": {"p17_step6_gate": {"status": "fully_satisfied"},
                  "p18_initialize": {"status": "review_initialized", "allowed": True}},
        "admission": {"p18_confirmation_allowed": False},
        "business_state": {**manifest["business_state"],
                           "P14": {"status": "failed"}, "P15": {"status": "failed"},
                           "P16": {"status": "proposal_ready",
                                   "cost_summary": {"cost_status": "unknown"}},
                           "navigation_N": {"verdict": "unknown"}},
        "research_parameters": {"contract_version": PARAMETER_CONTRACT_VERSION,
                                "fingerprint": "param-sha",
                                "values": {PARAMETER_ID: ["A", "B"]}},
        "research_limitations": [{"limitation_id": "cost_unknown", "statement": "成本未知"}],
    })
    manifest["map_projection"] = build_map_projection(source, {"selected_actions": actions}, manifest)
    report = build_report(manifest)
    text = report["markdown"]
    for expected in (
        "R0005 研究运行报告", "2 km 通信合同", "NM3 基线", "规划投影",
        "P14–P18 状态", "Radar 四段说明", "成本", "实际交付未证明", "结果清单",
        "list-sha", "P14-sha", "report_payload_sha256",
    ):
        assert expected in text
    assert "not_calculated" in text
    assert "unknown" in text
    assert report["identity"]["run_id"] == "run-map"
    assert report["identity"]["authoritative_result_list_sha256"] == "list-sha"
    assert hashlib.sha256(report["markdown"].encode("utf-8")).hexdigest() == report["report_sha256"]
    assert report["report_payload_sha256"] in text
    assert "<table>" in report["html"]
