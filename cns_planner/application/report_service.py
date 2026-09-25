"""Transactional P19 report generation and artifact access."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import shutil
import tempfile
import zipfile

from ..domain.reporting import (
    REPORT_SCHEMA_VERSION, TEMPLATE_VERSION, deterministic_report_id,
    empty_report_collection, report_source_fingerprint, sanitize_report_value,
)
from ..persistence.project_compaction import artifact_references
from ..reporting import HtmlReportRenderer, PlaywrightPdfRenderer, ReportBuilder


ARTIFACT_NAMES = {"html": "report.html", "pdf": "report.pdf", "json": "report.json", "package": "planning-package.zip"}
ARTIFACT_MIME = {"html": "text/html; charset=utf-8", "pdf": "application/pdf", "json": "application/json; charset=utf-8", "package": "application/zip"}


class PlanningReportService:
    def __init__(self, session, export_service, algorithm_catalog, snapshot,
                 builder=None, html_renderer=None, pdf_renderer=None):
        self.session, self.export_service = session, export_service
        self.algorithm_catalog, self.snapshot = algorithm_catalog, snapshot
        self.builder = builder or ReportBuilder()
        self.html_renderer = html_renderer or HtmlReportRenderer()
        self.pdf_renderer = pdf_renderer or PlaywrightPdfRenderer()

    def result_snapshot(self):
        return deepcopy(self.session.state.get("cns_planning_reports") or empty_report_collection())

    def preview(self, payload=None):
        now = _utc_now()
        model = self.builder.build(self.session.state, self.algorithm_catalog(), now, final=False)
        return {"status": "draft", "persisted": False, "report_data": model,
                "source_artifacts": artifact_references(self.session.state),
                "html": self.html_renderer.render(model)}

    def generate(self, payload=None):
        state = self.session.state
        plan = state.get("confirmed_cns_plan") or {}
        if plan.get("status") not in ("confirmed", "applied"):
            raise ValueError("无法生成正式报告：请先在方案审查中确认一个规划方案")
        if plan.get("current_applicability") == "stale":
            raise ValueError("无法生成正式报告：已确认方案对应旧项目状态，请重新初始化并确认方案")
        catalog = self.algorithm_catalog()
        source_fp = report_source_fingerprint(state, catalog)
        report_id = deterministic_report_id(plan.get("plan_id"), source_fp, TEMPLATE_VERSION)
        collection = state.setdefault("cns_planning_reports", empty_report_collection())
        existing = next((item for item in collection.get("records") or [] if item.get("report_id") == report_id), None)
        if existing:
            collection["active_report_id"] = report_id
            collection["status"] = "passed"
            state.setdefault("result_statuses", {})["report"] = "passed"
            self.session.save()
            return self.snapshot()
        root = self.session.store_path.parent.resolve()
        reports_root = (root / "reports").resolve()
        reports_root.mkdir(parents=True, exist_ok=True)
        target = (reports_root / report_id).resolve()
        if target.parent != reports_root or target.exists():
            raise ValueError("报告目标目录冲突，未覆盖任何历史报告")
        now = _utc_now()
        model = self.builder.build(state, catalog, now, final=True)
        html = self.html_renderer.render(model)
        # Phase4-B5X：报告 manifest 登记本次报告引用的 canonical artifact（ID/指纹/
        # 相对路径）。生成报告因此**不需要**把任何大型明细重新塞回 ProjectState。
        source_artifacts = artifact_references(state)
        original = deepcopy(state)
        moved = False
        try:
            with tempfile.TemporaryDirectory(prefix=".p19-", dir=reports_root) as temporary:
                stage = Path(temporary)
                _write(stage / "report.json", json.dumps(model, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8"))
                _write(stage / "report.html", html.encode("utf-8"))
                self.pdf_renderer.render(stage / "report.html", stage / "report.pdf")
                routes = sanitize_report_value(json.loads(self.export_service.routes()))
                _write(stage / "routes.geojson", json.dumps(routes, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8"))
                _write(stage / "facilities.geojson", self.export_service.confirmed_facilities())
                _write(stage / "algorithms.json", self.export_service.algorithms(catalog))
                provenance = deepcopy(((model.get("sections") or {}).get("audit") or {}).get("provenance_chain") or {})
                provenance["report_data_fingerprint"] = model["report_data_fingerprint"]
                if provenance.get("derivation"):
                    provenance["derivation"][-1]["input_fingerprint"] = source_fp
                    provenance["derivation"][-1]["output_fingerprint"] = model["report_data_fingerprint"]
                _write(stage / "provenance.json", json.dumps(provenance, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8"))
                _write(
                    stage / "artifact-manifest.json",
                    json.dumps(source_artifacts, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8"),
                )
                payload_names = ["report.html", "report.pdf", "report.json", "routes.geojson", "facilities.geojson", "algorithms.json", "provenance.json", "artifact-manifest.json"]
                manifest = "".join(f"{_sha(stage / name)}  {name}\n" for name in payload_names)
                _write(stage / "manifest-sha256.txt", manifest.encode("utf-8"))
                with zipfile.ZipFile(stage / "planning-package.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    for name in [*payload_names, "manifest-sha256.txt"]:
                        archive.write(stage / name, arcname=name)
                stage.replace(target); moved = True
            relative_root = target.relative_to(root).as_posix()
            record = {
                "report_id": report_id, "schema_version": REPORT_SCHEMA_VERSION,
                "template_version": TEMPLATE_VERSION, "source_plan_id": plan.get("plan_id"),
                "source_plan_status": plan.get("status"), "source_fingerprint": source_fp,
                "report_data_fingerprint": model["report_data_fingerprint"], "generated_at": now,
                "status": "passed", "current_applicability": "current",
                "artifacts": {kind: f"{relative_root}/{name}" for kind, name in ARTIFACT_NAMES.items()},
                # B5X：报告 manifest 记录它引用的 canonical artifact（只含相对路径）。
                "source_artifacts": source_artifacts,
            }
            old_active = collection.get("active_report_id")
            for prior in collection.get("records") or []:
                if prior.get("report_id") == old_active and prior.get("current_applicability") == "current":
                    prior["current_applicability"] = "stale_current_project"
            collection.setdefault("records", []).append(record)
            collection["active_report_id"] = report_id; collection["status"] = "passed"
            state.setdefault("result_statuses", {})["report"] = "passed"
            self.session.save()
        except Exception:
            state.clear(); state.update(original)
            if moved and target.parent == reports_root and target.exists():
                shutil.rmtree(target)
            raise
        return self.snapshot()

    def artifact(self, report_id, kind):
        if kind not in ARTIFACT_NAMES:
            raise ValueError("不支持的报告产物类型；请选择 html、pdf、package 或 json")
        record = next((item for item in (self.session.state.get("cns_planning_reports") or {}).get("records") or [] if item.get("report_id") == report_id), None)
        if record is None:
            raise ValueError("报告记录不存在；请刷新报告列表后重试")
        relative = (record.get("artifacts") or {}).get(kind)
        if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError("报告产物路径无效，已拒绝访问")
        root = self.session.store_path.parent.resolve()
        expected_root = (root / "reports" / str(report_id)).resolve()
        target = (root / relative).resolve()
        if expected_root not in target.parents or target.name != ARTIFACT_NAMES[kind] or not target.is_file():
            raise ValueError("报告产物路径不在白名单目录或文件已丢失")
        return target.read_bytes(), ARTIFACT_MIME[kind]


def _write(path, data):
    path.write_bytes(data)


def _sha(path):
    return sha256(path.read_bytes()).hexdigest()


def _utc_now():
    return datetime.now(timezone.utc).isoformat()
