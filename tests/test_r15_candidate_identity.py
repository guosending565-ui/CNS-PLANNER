"""Isolated adoption boundary checks; no GIS, project files or business compute."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cns_planner.application.layered_operational_adoption_service import LayeredOperationalAdoptionService
from cns_planner.application.layered_route_validation_service import _candidate_ref
from cns_planner.domain.layered_route_validation import path_fingerprint


def boundary():
    candidate = dict(candidate_id="C1", candidate_fingerprint="fp1", route_id="R1",
                     altitude_layer_id="L1", current_applicability="current",
                     operational_adoption_allowed=True, path=[[122, 30], [122.01, 30]])
    validation = dict(validation_id="V1", status="validated_candidate",
                      current_applicability="current", source_type="configured_real_sources",
                      candidate=_candidate_ref(candidate), fingerprints={"validation_fingerprint": "vf1"},
                      route={"path": deepcopy(candidate["path"]), "path_crs": "OGC:CRS84"})
    collection = {"items": [candidate], "active_candidate_id": "C1"}
    layered = SimpleNamespace(result_snapshot=Mock(return_value=collection))
    validations = SimpleNamespace(
        layered=layered,
        result_snapshot=lambda: {"items": [deepcopy(validation)], "active_validation_id": "V1"},
        _select_current_candidate=Mock(return_value=candidate),
        _current_risk_profile=Mock(return_value={"status": "passed"}),
        _source_readiness=lambda: {"status": "ready"})
    service = LayeredOperationalAdoptionService.__new__(LayeredOperationalAdoptionService)
    service.validations = validations
    service.session = SimpleNamespace(state={"operational_routes": [], "scenario_routes": []}, save=Mock())
    service.snapshot = lambda: {}
    service._apply_working = Mock(return_value={"adoption_id": "A1", "route_id": "R1"})
    service._normalize = lambda working: working
    return service, candidate, validation, collection


def decision(service, blocker=None):
    before = deepcopy(service.session.state)
    preview = service.preview({"validation_id": "V1"})
    if blocker:
        assert preview["publication_allowed"] is False
        assert blocker in preview["projection"]["reasons"]
        with pytest.raises(ValueError, match="gate"):
            service.apply({"validation_id": "V1", "confirmed": True})
        service._apply_working.assert_not_called()
        service.session.save.assert_not_called()
    else:
        assert preview["publication_allowed"] is True
        assert service.apply({"validation_id": "V1", "confirmed": True})["status"] == "passed"
    assert service.session.state == before
    return preview


@pytest.mark.parametrize("allowed,blocker", [(True, None), (False, "candidate_not_adoption_eligible"),
    (None, "candidate_missing_adoption_eligibility")])
def test_h2_eligibility_and_preview_apply(allowed, blocker):
    service, candidate, _, _ = boundary()
    if allowed is None:
        candidate.pop("operational_adoption_allowed")
    else:
        candidate["operational_adoption_allowed"] = allowed
    decision(service, blocker)


@pytest.mark.parametrize("value", [None, "", "   "])
def test_h2_no_id_never_uses_active_fallback(value):
    service, _, validation, _ = boundary()
    validation["candidate"]["candidate_id"] = value
    decision(service, "candidate_identity_missing")
    service.validations._select_current_candidate.assert_not_called()
    service.validations.layered.result_snapshot.assert_not_called()


def test_h2_absent_id_and_absent_eligibility_fail_closed():
    service, _, validation, _ = boundary()
    validation["candidate"].pop("candidate_id")
    decision(service, "candidate_identity_missing")
    service.validations._select_current_candidate.assert_not_called()


def test_h2_explicit_none_eligibility_fail_closed():
    service, candidate, _, _ = boundary()
    candidate["operational_adoption_allowed"] = None
    decision(service, "candidate_missing_adoption_eligibility")


@pytest.mark.parametrize("field,value,code", [
    ("candidate_id", "other", "candidate_identity_mismatch"),
    ("candidate_fingerprint", "other", "candidate_identity_stale"),
    ("candidate_fingerprint", None, "candidate_identity_stale"),
    ("route_id", "other", "candidate_identity_mismatch"),
    ("altitude_layer_id", "other", "candidate_identity_mismatch"),
    ("path_fingerprint", "other", "candidate_identity_stale"),
])
def test_h2_reference_mismatch(field, value, code):
    service, _, validation, _ = boundary()
    validation["candidate"][field] = value
    decision(service, code)


@pytest.mark.parametrize("where", ["live", "validation"])
def test_h2_changed_geometry_same_candidate_fingerprint(where):
    service, candidate, validation, _ = boundary()
    target = candidate if where == "live" else validation["route"]
    target["path"] = [[122, 30], [123, 31]]
    decision(service, "candidate_identity_stale")


def test_h2_duplicate_id_is_ambiguous():
    service, candidate, _, collection = boundary()
    collection["items"].append(deepcopy(candidate))
    decision(service, "candidate_identity_mismatch")


@pytest.mark.parametrize("field,value", [("operational_applicability", "provisional_only"),
    ("unknown_constraint_count", 1), ("contains_unknown_constraints", True)])
def test_h2_semantic_change_with_same_fingerprint(field, value):
    service, candidate, _, _ = boundary()
    candidate[field] = value
    decision(service, "candidate_identity_stale")


def test_h2_stale_validation_keeps_original_gate():
    service, _, validation, _ = boundary()
    validation["current_applicability"] = "stale_inputs_changed"
    decision(service, "validation_not_current")


def test_h2_real_gate_checks_same_single_resolved_object():
    service, candidate, _, _ = boundary()
    service.validations._current_risk_profile.return_value = None
    decision(service, "current_route_risk_profile_missing")
    assert service.validations.layered.result_snapshot.call_count == 2  # one per projection
    assert all(c.args[0] is candidate for c in service.validations._current_risk_profile.call_args_list)
    service.validations._select_current_candidate.assert_not_called()


def test_h2_valid_path_is_validation_geometry_and_live_digest():
    service, candidate, validation, _ = boundary()
    result = decision(service)
    path = result["projection"]["route"]["path"]
    assert path == validation["route"]["path"] == candidate["path"]
    assert path_fingerprint(path) == validation["candidate"]["path_fingerprint"]


@pytest.mark.parametrize("path", [[[122, 30], [122.01, float("nan")]],
                                  [[122, 30], [122.01, 30, 100]],
                                  [[122, 30], [None, 30]]])
def test_h2_invalid_live_geometry_fail_closed(path):
    service, candidate, _, _ = boundary()
    candidate["path"] = path
    decision(service, "candidate_identity_stale")


@pytest.mark.parametrize("case,blocker", [
    ("false", "candidate_not_adoption_eligible"),
    ("unknown", "candidate_not_adoption_eligible"),
    ("missing", "candidate_missing_adoption_eligibility"),
    ("stale_candidate", "referenced_candidate_not_current"),
    ("stale_validation", "validation_not_current"),
    ("true", None),
])
def test_backend_apply_without_preview_uses_live_authority(case, blocker):
    service, candidate, validation, _ = boundary()
    if case == "false":
        candidate["operational_adoption_allowed"] = False
    elif case == "unknown":
        candidate["operational_adoption_allowed"] = "unknown"
    elif case == "missing":
        candidate.pop("operational_adoption_allowed")
    elif case == "stale_candidate":
        candidate["current_applicability"] = "stale_inputs_changed"
    elif case == "stale_validation":
        validation["current_applicability"] = "stale_inputs_changed"
    before = deepcopy(service.session.state)
    # A caller skips Preview and claims eligibility; only the live candidate counts.
    payload = {"validation_id": "V1", "confirmed": True,
               "operational_adoption_allowed": True}
    if blocker:
        with pytest.raises(ValueError, match="gate"):
            service.apply(payload)
        service._apply_working.assert_not_called()
        service.session.save.assert_not_called()
        assert blocker in service.projection(payload)["reasons"]
    else:
        assert service.apply(payload)["status"] == "passed"
        service._apply_working.assert_called_once()
        service.session.save.assert_called_once()
    assert service.session.state == before
