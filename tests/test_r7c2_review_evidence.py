from copy import deepcopy
from types import SimpleNamespace
from cns_planner.reporting.review_evidence import review_summary
from cns_planner.persistence.artifact_store import ArtifactStore
from cns_planner.application.plan_review_service import PlanReviewService

def test_summary_preserves_unknown_and_complete_artifact(tmp_path):
    evidence = {"status": "stale", "unknown": 12, "disclosure_lines": ["unknown remains"],
                "routes": [{"voxels": [{"status": "unknown"}] * 1000}]}
    original = deepcopy(evidence)
    service = object.__new__(PlanReviewService)
    service.session = SimpleNamespace(state={"cns_plan_review": evidence}, store_path=tmp_path / "project.json")
    service._p17_gate = lambda **kw: {"status": "unknown"}
    service._variant_gates = lambda: {}
    result = service.snapshot_result()
    assert result["cns_plan_review"] == review_summary(evidence)
    assert result["cns_plan_review"]["unknown"] == 12
    assert result["continuous_service_gate"]["status"] == "unknown"
    assert service.snapshot_result(full=True)["cns_plan_review"] == original
    assert ArtifactStore(service.session.store_path).read(result["evidence_artifact"])["cns_plan_review"] == original
    assert evidence == original
    assert not service.session.store_path.exists()


def test_html_omits_full_detail_but_keeps_unknown_decision():
    from cns_planner.reporting.html_renderer import _decision
    section = {"review": {"status": "stale", "variants": [{"variant_id": "V1",
        "evaluation": {"confirmation_gate": {"status": "unknown"},
            "limitations": ["unknown evidence"], "before_p15": {
                "routes": [{"voxels": [{"secret_detail": "FULL_VOXEL"}]}]}}}]}}
    html = _decision(section)
    assert "FULL_VOXEL" not in html
    assert "unknown evidence" in html
    assert "V1" in html
