from copy import deepcopy
import pytest

from cns_planner.application.research_run_service import normalize_manifest, recovery_inputs


def legacy():
    return {
        "run_id": "source", "business_state": {"surveillance_Radar": {
            "verdict": "unknown", "radar_surveillance_layout_present": False}},
        "p17_threat_layers": {"supplementary_threat_status": "unknown", "layers": {
            "noncooperative": {"status": "not_applicable", "conclusion": "not_applicable"}}},
        "admission": {"p18_confirmation_allowed": False, "formal_release_allowed": False},
        "gates": {"p17_step6_gate": {"status": "fully_satisfied", "confirmation_allowed": True}},
    }


def test_unknown_projection_preserves_outage_and_original():
    source = legacy()
    before = deepcopy(source)
    view = normalize_manifest(source)
    layer = view["p17_threat_layers"]["layers"]["noncooperative"]
    assert layer["status"] == layer["conclusion"] == "unknown"
    assert layer["source_status"] == "not_applicable"
    assert "no_detection_evidence" in layer["reasons"]
    assert view["gates"]["p17_step6_gate"]["status"] == "fully_satisfied"
    assert view["confirmation_allowed"] is False
    assert source == before


def test_no_evidence_never_becomes_pass():
    source = legacy()
    source["p17_threat_layers"]["layers"]["noncooperative"]["status"] = "satisfied"
    assert normalize_manifest(source)["p17_threat_layers"]["layers"]["noncooperative"]["status"] == "unknown"


@pytest.mark.parametrize("payload", [{}, {"mode": "fresh_compute"}, {"mode": "resume_checkpoint", "run_id": "../escape"}])
def test_invalid_or_fresh_requests_fail_closed(payload):
    with pytest.raises(ValueError):
        recovery_inputs({}, payload)


def test_publication_marker_recovers_reference_before_task_finish(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from cns_planner.application.research_run_service import ResearchRunService, file_sha
    from cns_planner.persistence.artifact_store import ArtifactStore
    path = tmp_path / 'runs' / 'run1' / 'run_manifest.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'run_id': 'run1'}), encoding='utf-8')
    reference = {'artifact_id': 'artifact1', 'run_manifest_sha256': file_sha(path)}
    store = SimpleNamespace(
        list=lambda: [{'task_id': 'task1', 'status': 'succeeded', 'worker_payload': {'run_id': 'run1'}, 'result_artifact_ref': None}],
        publish_record=lambda task_id: {'result_artifact_ref': reference},
    )
    def read(_self, ref):
        assert ref == reference
        return {'run_id': 'run1'}
    monkeypatch.setattr(ArtifactStore, 'read', read)
    for _ in range(2):  # new service instance models restart in the publication window
        service = ResearchRunService(tmp_path, SimpleNamespace(store=store, workdir=tmp_path / 'dispatch.json'))
        assert service.manifest('run1') == {'run_id': 'run1'}


@pytest.mark.parametrize('key,value', [('contract_version', 'wrong'), ('authoritative_result_list_sha256', 'wrong')])
def test_checkpoint_advertised_identity_must_match_verified_source(key, value):
    from cns_planner.application.research_run_service import validate_checkpoint_identity
    source = {'run_id': 'source', 'input_version': {'id': 'input'}, 'contract_version': 'contract'}
    checkpoint = {'source_run_id': 'source', 'input_version': {'id': 'input'},
                  'contract_version': 'contract', 'authoritative_result_list_sha256': 'index-sha'}
    checkpoint[key] = value
    with pytest.raises(ValueError):
        validate_checkpoint_identity(checkpoint, source, 'index-sha')
