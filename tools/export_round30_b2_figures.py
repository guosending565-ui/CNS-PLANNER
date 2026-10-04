"""Export the two Round30-B2 review figures through the canonical FigureSpec/QGIS path."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys


ROOT = Path(r"D:\Projects\CNS-PLANNER")
PROJECT = ROOT / "_runtime_projects" / "zhoushan_screenshot_01_20261001_1647"
OUTPUT = ROOT / "_output" / "final_cns_figures" / "R0005" / "round30_b2_review"
ROUTE_ID = "R0005"
QGIS_ROOT = Path(r"C:\Program Files\QGIS 3.44.14")

TARGETS = (
    (
        "02_RID_R0005_ALT100.png", "surveillance_layout_v1",
        {"surveillance_service": "rid_cooperative"},
    ),
    ("05_CNS_Combined_R0005_ALT100.png", "cns_combined_v1", None),
)


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _paths():
    from cns_planner.gis.map_data import DEFAULT_PATHS

    configured = json.loads((ROOT / "projects" / "map_sources.json").read_text("utf-8"))
    result = dict(DEFAULT_PATHS)
    result.update({
        key: value for key, value in configured.items()
        if isinstance(value, str) and value.strip()
    })
    return result


class _ReadOnlySession:
    def __init__(self, state):
        self.state = state
        self.saved = 0

    def save(self):  # pragma: no cover - this export must never write canonical state
        self.saved += 1
        raise AssertionError("Round30-B2 figure export must not call session.save()")


def main():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    # Python 3.12 on Windows does not always inherit dependent-DLL lookup from PATH.
    # Register the three QGIS binary directories explicitly before importing PyQt/QGIS.
    dll_handles = [
        os.add_dll_directory(str(QGIS_ROOT / relative))
        for relative in ("apps/Qt5/bin", "apps/qgis-ltr/bin", "bin")
    ]
    sys.path.insert(0, str(ROOT))

    from qgis.core import QgsApplication

    application = QgsApplication.instance() or QgsApplication([], False)
    application.initQgis()
    try:
        from cns_planner.application.map_figure_service import MapFigureService
        from cns_planner.gis.qgis_figure_renderer import QgisFigureRenderer

        state_path = PROJECT / "project_state.json"
        before_sha256 = _sha256(state_path)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        revision_before = int(state.get("revision") or 0)
        session = _ReadOnlySession(state)
        renderer = QgisFigureRenderer()
        service = MapFigureService(
            session, _paths, lambda action: action(), renderer_factory=lambda: renderer,
            project_directory=PROJECT,
        )

        OUTPUT.mkdir(parents=True, exist_ok=True)
        figures = []
        for filename, template_id, parameters in TARGETS:
            spec = service.build_figure(
                template_id=template_id, route_id=ROUTE_ID,
                parameter_overrides=parameters,
            )
            payload = renderer.render(spec, dpi=300.0)
            destination = OUTPUT / filename
            destination.write_bytes(payload)
            figures.append({
                "filename": filename,
                "path": str(destination),
                "sha256": _sha256(destination),
                "bytes": destination.stat().st_size,
                "spec_fingerprint": spec.fingerprint(),
                "metadata": spec.metadata,
                "rid_layers": {
                    layer.layer_key: {
                        "feature_count": layer.feature_count,
                        "display_name": layer.display_name,
                        "source_detail": layer.source_detail,
                    }
                    for layer in spec.layers if layer.layer_key.startswith("cns_coverage_rid")
                },
            })

        after_sha256 = _sha256(state_path)
        revision_after = int(json.loads(state_path.read_text("utf-8")).get("revision") or 0)
        report = {
            "round": "Round30-B2",
            "route_id": ROUTE_ID,
            "project_revision_before": revision_before,
            "project_revision_after": revision_after,
            "project_sha256_before": before_sha256,
            "project_sha256_after": after_sha256,
            "canonical_state_unchanged": (
                revision_before == revision_after and before_sha256 == after_sha256
                and session.saved == 0
            ),
            "session_saved": session.saved,
            "figures": figures,
        }
        (OUTPUT / "round30_b2_export_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["canonical_state_unchanged"] else 2
    finally:
        application.exitQgis()


if __name__ == "__main__":
    raise SystemExit(main())
