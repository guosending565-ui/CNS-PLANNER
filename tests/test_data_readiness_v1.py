"""DATA-1/2/3 source trust and planning-input readiness contracts."""

import importlib.util
import json
from pathlib import Path

import pytest

from cns_planner.application.project_state import blank_project, normalize_project
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.geometry_health import inspect_geojson_geometries
from cns_planner.domain.source_audit import source_manifest
from cns_planner.data.mapping.airspace import AirspaceGridService


DEFAULTS = Path("cns_planner/config/defaults.json")


def workflow(tmp_path):
    return WorkflowService(tmp_path / "project.json", DEFAULTS)


def landing_csv(tmp_path):
    path = tmp_path / "landing.csv"
    path.write_text(
        "序号,名称,起降设施分类,所属县区,经纬度信息\n1,测试点,起降点,定海区,\"122.1,30.1\"\n",
        encoding="utf-8-sig",
    )
    return path


def route_csv(tmp_path):
    path = tmp_path / "routes.csv"
    path.write_text(
        "航线编号,航线名称,航线类别,点序号,点位名称,经度,纬度\n"
        "1,甲线,运输,1,A,122.1,30.1\n"
        "1,乙线,运输,3,C,122.3,30.3\n"
        "1,乙线,运输,4,D,999,30.4\n",
        encoding="utf-8-sig",
    )
    return path


def test_sha_verification_unchanged_then_changed_and_identity_ignores_absolute_path(tmp_path):
    source = tmp_path / "facts.csv"
    source.write_text("a,b\n1,2\n", encoding="utf-8")
    service = workflow(tmp_path)
    service.register_source_paths({"reference_routes": source})
    first = service.verify_source("reference_routes", source)["source_audits"]["items"]["reference_routes"]
    assert first["status"] == "verified"
    assert first["sha256"]
    assert str(tmp_path) not in json.dumps(first, ensure_ascii=False)
    assert set(first["provenance"]) == {
        "source_entity", "processing_activity", "derived_entity", "derived_from",
        "method", "timestamp", "note",
    }
    unchanged = service.verify_source("reference_routes", source)["source_audits"]["items"]["reference_routes"]
    assert unchanged["sha256"] == first["sha256"]
    source.write_text("a,b\n3,4\n", encoding="utf-8")
    changed = service.verify_source("reference_routes", source)["source_audits"]["items"]["reference_routes"]
    assert changed["status"] == "source_changed"
    assert changed["sha256"] != first["sha256"]
    assert "dependent_results_stale" in changed["reasons"]
    other = source_manifest("reference_routes", tmp_path / "elsewhere" / source.name)
    assert other["source_id"] == first["source_id"]
    directory = source_manifest("reference_routes", tmp_path)
    assert directory["status"] == "invalid_source_path"
    assert directory["reasons"] == ["source_must_be_concrete_file"]


def test_snapshot_stat_check_never_rehashes_large_file(tmp_path, monkeypatch):
    source = tmp_path / "large.bin"
    source.write_bytes(b"x" * 2_000_000)
    service = workflow(tmp_path)
    service.verify_source("terrain", source)
    calls = {"count": 0}

    def forbidden(*args, **kwargs):
        calls["count"] += 1
        raise AssertionError("snapshot must not hash")

    monkeypatch.setattr("cns_planner.application.source_audit_service.sha256_file", forbidden)
    for _ in range(4):
        snapshot = service.source_audits_snapshot()
        assert snapshot["items"]["terrain"]["status"] == "verified"
    assert calls["count"] == 0


def test_manual_crs_confirmation_validates_and_transforms_without_guessing(tmp_path):
    pytest.importorskip("pyproj")
    service = workflow(tmp_path)
    service.import_reference_landing_sites(landing_csv(tmp_path))
    before = service.state["reference_landing_sites"]
    assert before["crs"]["source_crs"]["confirmed"] is False
    with pytest.raises(ValueError, match="CRS 无效"):
        service.confirm_reference_crs("reference_landing_sites", {
            "value": "NOT-A-CRS", "source": {"type": "user"},
            "evidence": [{"type": "document"}],
        })
    result = service.confirm_reference_crs("reference_landing_sites", {
        "value": "EPSG:4326", "source": {"type": "user_confirmation"},
        "evidence": [{"type": "authority_document", "note": "test evidence"}],
    })["reference_landing_sites"]
    assert result["crs"]["source_crs"]["confirmed"] is True
    assert result["crs"]["source_crs"]["value"] == "EPSG:4326"
    assert result["crs"]["representation_crs"]["value"] == "OGC:CRS84"
    assert result["metadata"]["transformation"]["always_xy"] is True
    assert result["items"][0]["source_coordinate"] == [122.1, 30.1]
    reopened = service.configure_reference_sources({
        "reference_landing_sites": tmp_path / "landing.csv",
    })["reference_landing_sites"]
    assert reopened["crs"]["source_crs"]["confirmed"] is True


def test_source_change_requires_crs_revalidation(tmp_path):
    pytest.importorskip("pyproj")
    source = landing_csv(tmp_path)
    service = workflow(tmp_path)
    service.import_reference_landing_sites(source)
    service.confirm_reference_crs("reference_landing_sites", {
        "value": "EPSG:4326", "source": {"type": "user"},
        "evidence": [{"type": "document"}],
    })
    service.verify_source("reference_landing_sites", source)
    source.write_text(source.read_text(encoding="utf-8-sig") + "2,新点,起降点,普陀区,\"122.2,30.2\"\n", encoding="utf-8-sig")
    service.register_source_paths({"reference_landing_sites": source})
    collection = service.state["reference_landing_sites"]
    assert collection["crs"]["source_crs"]["confirmed"] is False
    assert collection["metadata"]["metric_measurement_status"] == "needs_revalidation"


def test_geojson_representation_never_confirms_source_crs(tmp_path):
    source = tmp_path / "routes.geojson"
    source.write_text(json.dumps({"type": "FeatureCollection", "features": [{
        "type": "Feature", "properties": {"route_number": "1"},
        "geometry": {"type": "LineString", "coordinates": [[122.1, 30.1], [122.2, 30.2]]},
    }]}), encoding="utf-8")
    service = workflow(tmp_path)
    preview = service.preview_reference_routes(source)["reference_route_import_preview"]
    service.confirm_reference_routes_import(source, preview["preview_id"])
    crs = service.state["reference_routes"]["crs"]
    assert crs["representation_crs"]["value"] == "OGC:CRS84"
    assert crs["representation_crs"]["declared_by_format"] is True
    assert crs["source_crs"]["confirmed"] is False


def test_route_preview_reports_gaps_duplicates_invalid_and_does_not_replace_before_confirm(tmp_path):
    source = route_csv(tmp_path)
    service = workflow(tmp_path)
    snapshot = service.preview_reference_routes(source)
    assert snapshot["reference_routes"]["count"] == 0
    preview = snapshot["reference_route_import_preview"]
    assert preview["route_count"] == 1
    assert preview["point_count"] == 3
    assert preview["valid_coordinate_count"] == 2
    assert preview["invalid_coordinate_count"] == 1
    assert preview["duplicate_route_numbers"] == ["1"]
    assert preview["sequence_gaps"] == [{"route_number": "1", "missing_sequences": [2]}]
    assert "航线编号" in preview["columns"]
    imported = service.confirm_reference_routes_import(source, preview["preview_id"])
    assert imported["reference_routes"]["count"] == 1
    assert imported["reference_routes"]["crs"]["source_crs"]["confirmed"] is False


def test_configuring_converted_route_source_does_not_import_before_confirmation(tmp_path):
    source = route_csv(tmp_path)
    service = workflow(tmp_path)
    result = service.configure_reference_sources({"reference_routes": source})
    assert result["reference_routes"]["count"] == 0
    assert result["reference_route_import_preview"] is None
    assert result["source_audits"]["items"]["reference_routes"]["status"] == "configured_unverified"


def test_et_preview_still_refuses_parser(tmp_path):
    source = tmp_path / "routes.et"
    source.write_text("ET", encoding="utf-8")
    preview = workflow(tmp_path).preview_reference_routes(source)["reference_route_import_preview"]
    assert preview["status"] == "requires_xlsx_or_csv_conversion"
    assert preview["route_count"] == 0


def test_invalid_geometry_is_blocked_without_repair():
    health = inspect_geojson_geometries([
        {"geometry": None},
        {"geometry": {"type": "Polygon", "coordinates": []}},
        {"geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]}},
        {"geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [0, 1]]] }},
    ], {"Polygon", "MultiPolygon"})
    assert health["status"] == "blocked"
    assert health["null"] == 1 and health["empty"] == 1
    assert health["unsupported"] == 1 and health["invalid"] == 1
    assert health["repair_applied"] is False


def test_invalid_reference_geojson_preview_cannot_be_confirmed(tmp_path):
    source = tmp_path / "invalid-routes.geojson"
    source.write_text(json.dumps({"type": "FeatureCollection", "features": [{
        "type": "Feature", "properties": {"route_number": "1"},
        "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [0, 1]]]},
    }]}), encoding="utf-8")
    service = workflow(tmp_path)
    preview = service.preview_reference_routes(source)["reference_route_import_preview"]
    assert preview["status"] == "blocked"
    assert "invalid_geometry_fail_closed" in preview["warnings"]
    with pytest.raises(ValueError, match="未通过"):
        service.confirm_reference_routes_import(source, preview["preview_id"])


def test_invalid_airspace_geometry_fails_closed_before_intersection():
    class Adapter:
        def geometry_health(self, _bbox):
            return {"status": "blocked", "feature_count": 1, "invalid": 1,
                    "null": 0, "empty": 0, "unsupported": 0,
                    "repair_applied": False}

        def describe(self):
            return {"file_name": "airspace.qgz"}

        def intersections(self, *_args):
            raise AssertionError("invalid source must not enter spatial computation")

    result = AirspaceGridService().map({
        "level": 8, "workspace_bbox": [0, 0, 1, 1],
        "cells": [{"grid_id": "A", "bbox": [0, 0, 1, 1]}],
    }, Adapter(), {"items": []})
    assert result["status"] == "failed"
    assert result["airspace_eligibility"]["status"] == "blocked"
    assert result["geometry_health"]["invalid"] == 1


def test_policy_single_batch_and_missing_evidence_contract(tmp_path):
    service = workflow(tmp_path)
    with pytest.raises(ValueError, match="evidence"):
        service.set_airspace_policy({
            "feature_id": "A", "route_eligibility": "allowed",
            "confirmed": True, "source": {"type": "user"},
        })
    service.set_airspace_policy({
        "feature_id": "A", "route_eligibility": "allowed", "confirmed": True,
        "source": {"type": "user"}, "evidence": [{"type": "review"}],
    })
    result = service.batch_set_airspace_policies({
        "feature_ids": ["B", "C"], "route_eligibility": "blocked", "confirmed": True,
        "source": {"type": "user_batch"}, "evidence": [{"type": "review"}],
    })
    assert result["airspace_policies"]["count"] == 3
    assert {item["feature_id"] for item in result["airspace_policies"]["items"]} == {"A", "B", "C"}
    with pytest.raises(ValueError, match="明确选择"):
        service.batch_set_airspace_policies({
            "feature_ids": [], "route_eligibility": "unknown", "confirmed": False,
            "source": {"type": "user"}, "evidence": [{"type": "review"}],
        })


def test_single_policy_edit_preserves_legacy_evidence_less_peer(tmp_path):
    service = workflow(tmp_path)
    service.state["airspace_policies"] = {
        "status": "passed", "count": 1,
        "items": [{"feature_id": "LEGACY", "route_eligibility": "unknown",
                   "confirmed": False, "source": {"type": "legacy"}}],
    }
    result = service.set_airspace_policy({
        "feature_id": "NEW", "route_eligibility": "allowed", "confirmed": True,
        "source": {"type": "user"}, "evidence": [{"type": "review"}],
    })
    items = {item["feature_id"]: item for item in result["airspace_policies"]["items"]}
    assert items["LEGACY"]["evidence"] == []
    assert items["NEW"]["evidence"] == [{"type": "review"}]


def test_old_project_backfills_source_audits(tmp_path):
    defaults = json.loads(DEFAULTS.read_text(encoding="utf-8"))
    legacy = blank_project(defaults)
    legacy.pop("source_audits")
    legacy.pop("reference_route_import_preview")
    normalized = normalize_project(legacy, workflow(tmp_path).grid_service)
    assert normalized["source_audits"]["count"] == 0
    assert normalized["reference_route_import_preview"] is None


def test_source_center_hides_internal_stage_numbers():
    text = Path("cns_planner/web/js/sources/source_center.js").read_text(encoding="utf-8")
    assert "P1" not in text and "P7" not in text and "P13" not in text
    assert "数据可信度/审计" in text
    for action in ("验证数据源", "确认 CRS", "预览并导入航线"):
        assert action in text
    assert "编辑 AirspacePolicy" not in text


def test_data_readiness_report_generation(tmp_path):
    path = Path("tools/data_readiness_report.py")
    spec = importlib.util.spec_from_file_location("data_readiness_report_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    project = workflow(tmp_path)
    project.save()
    report = module.build_report(project.store_path, tmp_path / "missing-map-sources.json")
    assert set(report["data_issues"]) == {"DATA-1", "DATA-2"}
    assert report["automatic_confirmation"] is False and report["et_parser"] is None
    json_path, md_path = module.write_report(report, tmp_path / "report")
    assert json_path.is_file() and md_path.is_file()
    assert "DATA-1" in md_path.read_text(encoding="utf-8")
