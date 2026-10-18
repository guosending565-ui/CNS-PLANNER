from copy import deepcopy
from pathlib import Path
import pytest
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.layered_route import stable_fingerprint


def service(tmp_path):
    return WorkflowService(tmp_path / "project.json", Path("cns_planner/config/defaults.json"))


def test_building_confirmation_requires_authorization_and_current_evidence(tmp_path):
    s = service(tmp_path)
    source = tmp_path / "source.csv"
    source.write_text("x,y\n122,30\n", encoding="utf-8")
    s.verify_source("buildings", source, {"declared_crs": "EPSG:4326"})
    audit = s.state["source_audits"]["items"]["buildings"]
    payload = {"value": "EPSG:4326", "confirmed": True, "expected_revision": s.state["revision"],
               "source_fingerprint": audit["version_fingerprint"], "evidence": [{"type": "user_supplied", "note": "isolated test"}]}
    before = deepcopy(s.state)
    with pytest.raises(ValueError):
        s.source_audit_service.confirm_crs("terrain", source, payload)
    with pytest.raises(ValueError):
        s.source_audit_service.confirm_crs("buildings", source, {**payload, "confirmed": False})
    with pytest.raises(ValueError):
        s.source_audit_service.confirm_crs("buildings", source, {**payload, "source_fingerprint": "old"})
    assert s.state == before
    s.state["planning_constraint_fields"].update({"status": "passed", "items": [{"field_id": "PCF-test", "status": "passed"}]})
    s.source_audit_service.confirm_crs("buildings", source, payload)
    record = s.state["source_audits"]["items"]["buildings"]["confirmed_crs"]
    assert record["source"]["origin"] == "human_adjudication"
    assert record["source_fingerprint"] == audit["version_fingerprint"]
    assert s.state["revision"] == payload["expected_revision"] + 1
    assert s.state["planning_constraint_fields"]["status"] == "stale"
    assert s.state["source_audits"]["items"]["buildings"]["sha256"] == audit["sha256"]
    source.write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError):
        s.source_audit_service.confirm_crs("buildings", source, {**payload, "expected_revision": s.state["revision"]})


def test_provider_selection_is_intent_only_and_never_promotes_stale(tmp_path):
    s = service(tmp_path)
    s.state["coverage_3d"]["status"] = "stale"
    previous = deepcopy(s.state["coverage_3d"])
    payload = {"provider_selection": "coverage_3d", "confirmed": True,
               "evidence": [{"type": "user_supplied", "note": "isolated test"}],
               "expected_revision": s.state["revision"], "source_fingerprint": stable_fingerprint(previous)}
    with pytest.raises(ValueError):
        s.set_communication_planning_field({**payload, "confirmed": False})
    with pytest.raises(ValueError):
        s.set_communication_planning_field({**payload, "provider_selection": "invented"})
    s.set_communication_planning_field(payload)
    field = s.state["communication_planning_field"]
    assert field["provider"] == "coverage_3d"
    assert field["provider_selection"]["source_status"] == "stale"
    assert field["status"] == "not_configured" and field["cells"] == {}
    assert field["provenance"]["used_in_cost"] is False
    assert s.state["coverage_3d"] == previous


def test_confirmation_survives_reopen_and_source_change_revokes(tmp_path):
    s = service(tmp_path)
    path = tmp_path / "grid.csv"
    path.write_text("a,b\n122,30", encoding="utf-8")
    s.verify_source("building_grid", path, {"declared_crs": "EPSG:4326"})
    audit = s.state["source_audits"]["items"]["building_grid"]
    payload = {"value": "EPSG:4326", "confirmed": True, "expected_revision": s.state["revision"],
               "source_fingerprint": audit["version_fingerprint"], "evidence": [{"type": "user_supplied", "note": "test limits"}]}
    before = deepcopy(s.state)
    for bad in ({"value": "EPSG:4490"}, {"evidence": []}, {"expected_revision": -1}):
        with pytest.raises(ValueError):
            s.source_audit_service.confirm_crs("building_grid", path, {**payload, **bad})
        assert s.state == before
    s.source_audit_service.confirm_crs("building_grid", path, payload)
    reopened = service(tmp_path)
    reopened.register_source_paths({"building_grid": path})
    assert reopened.state["source_audits"]["items"]["building_grid"]["confirmed_crs"]["confirmed"]
    path.write_text("changed content", encoding="utf-8")
    reopened.register_source_paths({"building_grid": path})
    assert reopened.state["source_audits"]["items"]["building_grid"]["confirmed_crs"] is None


from test_project_restore_bootstrap import map_server_module, FakeMapData


def test_http_confirmation_and_provider_use_existing_session_revision_gate(tmp_path, map_server_module):
    from io import BytesIO
    import json
    module = map_server_module
    s = service(tmp_path)
    source = tmp_path / "building.csv"
    source.write_text("x,y\n122,30", encoding="utf-8")
    module.WORKFLOW = s
    module.DATA = FakeMapData({"buildings": str(source)})
    s.verify_source("buildings", source, {"declared_crs": "EPSG:4326"})

    def post(endpoint, payload, token=True, revision=None):
        body = json.dumps(payload).encode()
        handler = object.__new__(module.Handler)
        handler.path = endpoint
        handler.headers = {"Content-Length": str(len(body)), "X-CNS-Revision": str(s.state["revision"] if revision is None else revision)}
        if token:
            handler.headers["X-CNS-Token"] = module.TOKEN
        handler.rfile = BytesIO(body)
        handler.allowed = lambda: True
        responses = []
        handler.respond = lambda data, *args, **kwargs: responses.append((data, kwargs.get("status", 200)))
        handler.do_POST()
        return responses[-1]

    payload = {"role": "buildings", "value": "EPSG:4326", "confirmed": True,
               "expected_revision": s.state["revision"],
               "source_fingerprint": s.state["source_audits"]["items"]["buildings"]["version_fingerprint"],
               "evidence": [{"type": "user_supplied", "note": "HTTP isolated test"}]}
    endpoint = "/api/data-sources/crs/confirm"
    assert post(endpoint, payload, token=False)[1] == 403
    assert post(endpoint, payload, revision=-1)[1] == 409
    assert post(endpoint, {**payload, "confirmed": False})[1] == 400
    assert post(endpoint, payload)[1] == 200
    assert s.state["source_audits"]["items"]["buildings"]["confirmed_crs"]["confirmed"]
    source = s.state["coverage_3d"]
    request = {"provider_selection": "coverage_3d", "confirmed": True,
               "expected_revision": s.state["revision"], "source_fingerprint": stable_fingerprint(source),
               "evidence": [{"type": "user_supplied", "note": "HTTP isolated test"}]}
    assert post("/api/communication-planning-field", request)[1] == 200
    assert s.state["communication_planning_field"]["status"] == "not_configured"
