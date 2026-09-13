from copy import deepcopy
from hashlib import sha256
import io
import json
from pathlib import Path
import zipfile

import pytest

from cns_planner.api.router import ApiRouter
from cns_planner.application.project_state import normalize_project
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.reporting import deterministic_report_id, sanitize_report_value
from test_corridor_site_planner_v2 import configured


class FakePdfRenderer:
    def __init__(self): self.html = None
    def render(self, html_path, pdf_path):
        self.html = html_path.read_text(encoding="utf-8")
        pdf_path.write_bytes(b"%PDF-1.4\nFAKE EXACT HTML RENDER\n")


class FailingPdfRenderer:
    def render(self, html_path, pdf_path):
        raise RuntimeError("pdf_renderer_unavailable")


class Context:
    def __init__(self, workflow): self.workflow, self.data = workflow, None


def confirmed_workflow(tmp_path, applied=False):
    workflow = configured(tmp_path)
    workflow.evaluate_cns_corridor_site_plan()
    review = workflow.initialize_cns_plan_review()["cns_plan_review"]
    auto = review["variants"][-1]
    confirmed = workflow.confirm_cns_plan({
        "variant_id": auto["variant_id"], "confirm_without_objectives": True,
        "source": "test", "reason": "P19 synthetic report fixture",
    })["confirmed_cns_plan"]
    if applied:
        workflow.apply_confirmed_cns_plan({"plan_id": confirmed["plan_id"]})
    return workflow


def test_no_confirmed_plan_only_allows_zero_pollution_draft(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", Path("cns_planner/config/defaults.json"))
    before = deepcopy(workflow.state)
    preview = workflow.preview_cns_planning_report()
    assert preview["status"] == "draft" and preview["persisted"] is False
    assert preview["report_data"]["plan_status_label"] == "草稿预览（尚无已确认方案）"
    assert workflow.state == before
    with pytest.raises(ValueError, match="确认一个规划方案"):
        workflow.generate_cns_planning_report()


@pytest.mark.parametrize("applied,label", [(False, "已确认方案（尚未应用）"), (True, "已确认并应用")])
def test_report_model_has_complete_source_sections_and_plan_label(tmp_path, applied, label):
    workflow = confirmed_workflow(tmp_path, applied=applied)
    model = workflow.preview_cns_planning_report()["report_data"]
    assert model["plan_status_label"] == label
    assert model["source"]["plan_id"] == workflow.state["confirmed_cns_plan"]["plan_id"]
    assert model["source"]["fingerprint"]
    sections = model["sections"]
    for key in ("operation_and_requirement_basis", "centerline_gap_p10", "spatial_service_p14",
                "corridor_gap_objectives_p15", "plan_review_p18", "data_foundation", "audit"):
        assert key in sections
    assert sections["limitations"]["common_cause"] == "not_evaluated"
    assert model["statistics"]["classification_semantics"] == "unknown_is_neither_pass_nor_fail"


def test_sanitizer_and_html_escape_paths_secrets_and_external_assets(tmp_path):
    workflow = confirmed_workflow(tmp_path)
    workflow.state["project"]["name"] = '<script>alert("x")</script>'
    workflow.state["data_source_profiles"]["population"].update({
        "path": r"C:\private\population.tif", "api_key": "SECRET", "source_mode": "synthetic",
    })
    preview = workflow.preview_cns_planning_report()
    model, html = preview["report_data"], preview["html"]
    population = model["sections"]["data_foundation"]["population"]
    assert population["path"] == "population.tif" and population["api_key"] == "REDACTED"
    assert "<script>alert" not in html and "&lt;script&gt;" in html
    assert "C:\\private" not in html and "SECRET" not in html
    assert "<style>" in html and "<svg" in html
    assert "<script" not in html.lower() and " src=" not in html.lower() and "@import" not in html.lower()
    assert sanitize_report_value({"authorization": "Bearer X"})["authorization"] == "REDACTED"


def test_generate_is_deterministic_idempotent_and_package_checksums_verify(tmp_path):
    workflow = confirmed_workflow(tmp_path)
    fake = FakePdfRenderer(); workflow.report_service.pdf_renderer = fake
    first = workflow.generate_cns_planning_report()["cns_planning_reports"]
    record = first["records"][0]
    assert record["report_id"] == deterministic_report_id(record["source_plan_id"], record["source_fingerprint"], record["template_version"])
    second = workflow.generate_cns_planning_report()["cns_planning_reports"]
    assert len(second["records"]) == 1 and second["active_report_id"] == record["report_id"]
    html_bytes, _ = workflow.cns_planning_report_artifact(record["report_id"], "html")
    pdf_bytes, _ = workflow.cns_planning_report_artifact(record["report_id"], "pdf")
    package, mime = workflow.cns_planning_report_artifact(record["report_id"], "package")
    assert fake.html == html_bytes.decode("utf-8") and pdf_bytes.startswith(b"%PDF") and mime == "application/zip"
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        required = {"report.html", "report.pdf", "report.json", "routes.geojson", "facilities.geojson", "algorithms.json", "provenance.json", "manifest-sha256.txt"}
        assert required <= set(archive.namelist())
        manifest = archive.read("manifest-sha256.txt").decode().splitlines()
        for line in manifest:
            digest, name = line.split("  ", 1)
            assert sha256(archive.read(name)).hexdigest() == digest
        provenance = json.loads(archive.read("provenance.json"))
        assert provenance["semantics"] == "w3c_prov_inspired_not_full_prov_compliance"


def test_pdf_failure_rolls_back_record_and_final_directory(tmp_path):
    workflow = confirmed_workflow(tmp_path)
    workflow.report_service.pdf_renderer = FailingPdfRenderer()
    before = deepcopy(workflow.state)
    with pytest.raises(RuntimeError, match="pdf_renderer_unavailable"):
        workflow.generate_cns_planning_report()
    assert workflow.state == before
    report_root = workflow.store_path.parent / "reports"
    assert not report_root.exists() or not any(item.name.startswith("RPT-") for item in report_root.iterdir())


def test_final_facility_export_ignores_legacy_coverage_and_artifact_is_whitelisted(tmp_path):
    workflow = confirmed_workflow(tmp_path)
    workflow.state["coverage"] = {"layers": {"C": {"stations": [{"station_id": "LEGACY", "coordinate": [9, 9]}]}}}
    data = json.loads(workflow.export_confirmed_facilities())
    assert all(item["properties"].get("station_id") != "LEGACY" for item in data["features"])
    assert any(item["properties"].get("plan_status") == "confirmed" for item in data["features"])
    workflow.report_service.pdf_renderer = FakePdfRenderer()
    workflow.generate_cns_planning_report()
    record = workflow.state["cns_planning_reports"]["records"][0]
    record["artifacts"]["html"] = "../project.json"
    with pytest.raises(ValueError, match="路径无效"):
        workflow.cns_planning_report_artifact(record["report_id"], "html")


def test_report_history_becomes_stale_but_artifact_remains_and_schema_backfills(tmp_path):
    workflow = confirmed_workflow(tmp_path)
    workflow.report_service.pdf_renderer = FakePdfRenderer()
    workflow.generate_cns_planning_report()
    report_id = workflow.state["cns_planning_reports"]["active_report_id"]
    workflow.invalidation_service.cns_plan_review("synthetic source change")
    record = workflow.state["cns_planning_reports"]["records"][0]
    assert record["current_applicability"] == "stale_current_project"
    assert workflow.state["result_statuses"]["report"] == "stale"
    assert workflow.cns_planning_report_artifact(report_id, "json")[0]
    workflow.session.save()
    restored = WorkflowService(workflow.store_path, Path("cns_planner/config/defaults.json"))
    restored_record = restored.state["cns_planning_reports"]["records"][0]
    assert restored_record["report_id"] == report_id
    assert restored_record["current_applicability"] == "stale_current_project"
    assert restored.cns_planning_report_artifact(report_id, "json")[0]
    legacy = deepcopy(workflow.state); legacy.pop("cns_planning_reports")
    normalized = normalize_project(legacy, workflow.grid_service)
    assert normalized["cns_planning_reports"]["records"] == []


def test_report_api_preview_generate_get_and_artifact(tmp_path):
    workflow = confirmed_workflow(tmp_path)
    workflow.report_service.pdf_renderer = FakePdfRenderer()
    api = ApiRouter(Context(workflow))
    assert api.post("/api/cns-planning-report/preview", {}).data["persisted"] is False
    generated = api.post("/api/cns-planning-report/generate", {}).data
    report_id = generated["cns_planning_reports"]["active_report_id"]
    assert api.get("/api/cns-planning-report", {}, {}).data["active_report_id"] == report_id
    artifact = api.get("/api/cns-planning-report/artifact", {"report_id": [report_id], "kind": ["html"]}, {}).data
    assert artifact.startswith(b"<!doctype html>")
