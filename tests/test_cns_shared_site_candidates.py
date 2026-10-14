from copy import deepcopy
from types import SimpleNamespace

from cns_planner.application.cns_shared_site_candidate_service import CNSSharedSiteCandidateService
from cns_planner.application.workflow_service import WorkflowService


def _crs(value=None, confirmed=False):
    return {"source_crs": {"value": value, "status": "confirmed" if confirmed else "pending_confirmation", "confirmed": confirmed, "evidence": ([{"type": "user_confirmation", "value": value}] if confirmed else [])}, "representation_crs": {"value": "OGC:CRS84", "status": "declared", "axis_order": "lon_lat"}}


def _landing_collection(*, confirmed=False):
    crs = _crs("EPSG:4326" if confirmed else None, confirmed)
    return {"status": "passed", "source": {"file": "landing.xlsx", "format": "xlsx"}, "metadata": {"source_type": "real", "coordinate_order": "lon_lat"}, "crs": crs, "count": 3, "items": [
        {"reference_site_id": "RLS-LINKED", "name": "关联点", "site_type": "起降点", "coordinate": [122.10, 30.10], "quality": "parsed", "source": {"file": "landing.xlsx", "sheet": "S", "row": 2}, "crs": deepcopy(crs)},
        {"reference_site_id": "RLS-NEAR", "name": "近邻但未关联", "site_type": "临时起降点", "coordinate": [122.100001, 30.100001], "quality": "parsed", "source": {"file": "landing.xlsx", "sheet": "S", "row": 3}, "crs": deepcopy(crs)},
        {"reference_site_id": "RLS-BAD", "name": "坏坐标记录", "site_type": "起降点", "coordinate": None, "quality": "invalid", "source": {"file": "landing.xlsx", "sheet": "S", "row": 4}, "crs": deepcopy(crs)},
    ]}


def _tower_collection():
    return {"status": "passed", "source": {"type": "file", "path": "towers.xlsx", "format": "xlsx"}, "data_source": "towers.xlsx", "metadata": {"planning_integration": "reference_only"}, "crs": _crs("EPSG:4490", True), "count": 2, "items": [
        {"tower_id": "T-002", "name": "二号塔", "longitude": 122.20, "latitude": 30.20, "site_type": "地面塔", "source": {"file": "towers.xlsx", "sheet": "S", "row": 3}},
        {"tower_id": "T-001", "name": "一号塔", "longitude": 122.15, "latitude": 30.15, "site_type": "楼面塔", "source": {"file": "towers.xlsx", "sheet": "S", "row": 2}},
    ]}


def _service(*, confirmed_reference=False):
    session = SimpleNamespace(state={"nodes": [
        {"node_id": "N002", "name": "手工点", "coordinate": [122.30, 30.30], "coordinate_source": "manual_entry"},
        {"node_id": "N001", "name": "关联点", "coordinate": [122.10, 30.10], "coordinate_source": "reference_site", "reference_site_id": "RLS-LINKED", "provenance": {"source_type": "real_reference", "reference_site_id": "RLS-LINKED", "coordinate_quality": "parsed", "crs_status": "confirmed" if confirmed_reference else "pending_confirmation", "source": {"file": "landing.xlsx", "sheet": "S", "row": 2}}},
    ]})
    landing, towers = _landing_collection(confirmed=confirmed_reference), _tower_collection()
    return CNSSharedSiteCandidateService(session, lambda: deepcopy(landing), lambda: deepcopy(towers))


def test_candidate_identity_ordering_and_explicit_reference_deduplication():
    snapshot = _service(confirmed_reference=True).snapshot()
    ids = [item["candidate_id"] for item in snapshot["candidates"]]
    assert ids == sorted(ids)
    assert ids == ["node:N001", "node:N002", "reference-landing-site:RLS-BAD", "reference-landing-site:RLS-NEAR", "tower-colocation:T-001", "tower-colocation:T-002"]
    assert "reference-landing-site:RLS-LINKED" not in ids
    assert snapshot["counts"] == {"total": 6, "nodes": 2, "reference_landing_sites": 2, "linked_reference_landing_sites": 1, "towers": 2, "existing_cns_facilities": 0, "spatial_candidates": 5, "spatial_blocked": 1}


def test_candidate_preserves_evidence_and_never_implies_service_qualification():
    snapshot = _service(confirmed_reference=True).snapshot()
    linked = next(item for item in snapshot["candidates"] if item["candidate_id"] == "node:N001")
    tower = next(item for item in snapshot["candidates"] if item["candidate_id"] == "tower-colocation:T-001")
    assert linked["explicit_reference_site_id"] == "RLS-LINKED"
    assert linked["coordinate_quality"] == "parsed"
    assert linked["coordinate_source"] == "reference_site"
    assert linked["source_crs"]["value"] == "EPSG:4326"
    assert linked["representation_crs"]["value"] == "OGC:CRS84"
    assert linked["provenance"]["source"]["row"] == 2
    assert tower["data_source"] == "towers.xlsx"
    assert tower["site_type"] == "楼面塔"
    assert set(tower["service_applicability"]) == {"C", "N", "RID", "Radar", "RTK"}
    assert all(value["status"] in {"not_evaluated", "unqualified"} and value["qualification_status"] != "qualified" for candidate in snapshot["candidates"] for value in candidate["service_applicability"].values())
    assert snapshot["corridor_width_policy_version"] == "fixed-1000m-v1"
    assert snapshot["corridor_half_width_m"] == 500
    assert snapshot["full_width_service_assurance_status"] == "not_evaluated"


def test_reference_crs_gate_keeps_identity_but_blocks_spatial_use():
    blocked = _service(confirmed_reference=False).snapshot()
    linked = next(item for item in blocked["candidates"] if item["candidate_id"] == "node:N001")
    invalid = next(item for item in blocked["candidates"] if item["candidate_id"] == "reference-landing-site:RLS-BAD")
    assert linked["coordinate"] == [122.10, 30.10]
    assert linked["spatial_use_status"] == "blocked"
    assert linked["spatial_block_reason"] == "reference_source_crs_unresolved"
    assert invalid["spatial_use_status"] == "blocked"
    assert invalid["spatial_block_reason"] == "invalid_coordinate"
    assert linked["candidate_id"] not in {item["candidate_id"] for item in blocked["spatial_candidates"]}
    assert blocked["reason_counts"] == {"invalid_coordinate": 1, "reference_source_crs_unresolved": 2}
    assert "node:N001" in {item["candidate_id"] for item in _service(confirmed_reference=True).snapshot()["spatial_candidates"]}


def test_tower_inventory_is_not_synthesized_and_workflow_facade_is_read_only():
    service = _service(confirmed_reference=True)
    before = deepcopy(service.session.state)
    snapshot = service.snapshot()
    tower_ids = [item["source_id"] for item in snapshot["candidates"] if item["candidate_kind"] == "tower_colocation"]
    assert tower_ids == ["T-001", "T-002"]
    assert snapshot["source_counts"]["tower_records"] == 2
    assert service.session.state == before
    workflow = object.__new__(WorkflowService)
    workflow.cns_shared_site_candidate_service = service
    assert workflow.cns_shared_site_candidates() == snapshot
