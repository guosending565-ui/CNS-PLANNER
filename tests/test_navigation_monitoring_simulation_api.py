from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from cns_planner.api.router import ApiRouter


class ReadOnlyWorkflow:
    def __init__(self):
        self.state = {"revision": 468, "sentinel": {"unchanged": True}}
        self.calls = []

    def cns_shared_site_candidates(self):
        self.calls.append("candidates")
        return {
            "status": "passed",
            "candidates": [{"candidate_id": "node:A"}],
            "spatial_candidates": [{"candidate_id": "node:A"}],
            "counts": {"total": 1, "towers": 0},
            "reason_counts": {},
            "corridor_width_policy_version": "fixed-1000m-v1",
            "corridor_half_width_m": 500,
            "full_width_service_assurance_status": "not_evaluated",
        }

    def fixed_corridor_geometry(self, route_id=None):
        self.calls.append(("fixed_corridor_geometry", route_id))
        return {
            "status": "available",
            "route_id": route_id or "R0005",
            "width_policy_version": "fixed-1000m-v1",
            "corridor_half_width_m": 500,
            "corridor_total_width_m": 1000,
            "currentness": "stale",
            "currentness_reason": "corridor_result_not_current",
            "geometry": {"type": "Polygon", "coordinates": []},
            "writes_performed": 0,
        }

    def simulate_navigation_monitoring(self, payload):
        self.calls.append(("simulation", deepcopy(payload)))
        if payload.get("force_endpoint_monitors") is False:
            raise ValueError("强制端点导航服务监测设备不可关闭")
        if payload.get("blocked"):
            return {"status": "blocked", "reason": "mandatory_endpoint_spatial_evidence_unavailable", "message": "起终点坐标系证据不足，仿真已阻断", "writes_performed": 0}
        return {
            "status": "passed", "route_identity": {"route_geometry_fingerprint": "route-fp"},
            "parameter_fingerprint": "parameter-fp", "candidate_snapshot_fingerprint": "candidate-fp",
            "risk_priority_status": "available", "risk_identity": {"profile_fingerprint": "risk-fp"},
            "selected_mandatory_site_ids": ["node:A", "node:B"],
            "selected_optional_sites": [{"site_id": "tower-colocation:T1"}],
            "devices": [{"device_id": "D1"}], "merged_intervals_m": [[0, 100]],
            "associated_length_m": 100, "gaps": [], "longest_gap_m": 0,
            "longest_gap_duration_s": 0, "writes_performed": 0,
            "corridor_width_policy_version": "fixed-1000m-v1", "corridor_half_width_m": 500,
            "full_width_service_assurance_status": "not_evaluated",
        }

    def save(self):
        raise AssertionError("只读 API 不得保存")

    def evaluate_cns_corridor(self, _):
        raise AssertionError("不得触发 P14")

    def evaluate_cns_corridor_site_plan(self, _):
        raise AssertionError("不得触发 P16")

    def evaluate_cns_continuous_service(self, _):
        raise AssertionError("不得触发 P17")

    def evaluate_radar_surveillance_layout(self, _):
        raise AssertionError("不得触发 Radar")


def _router():
    workflow = ReadOnlyWorkflow()
    context = SimpleNamespace(workflow=workflow, data=SimpleNamespace(), static=Path("cns_planner/web"))
    return ApiRouter(context), workflow


def test_get_shared_candidates_exposes_counts_reasons_and_width_contract_without_writes():
    router, workflow = _router()
    before = deepcopy(workflow.state)
    response = router.get("/api/cns-shared-site-candidates", {}, {})
    assert response.status == 200
    assert response.data["candidates"][0]["candidate_id"] == "node:A"
    assert response.data["counts"] == {"total": 1, "towers": 0}
    assert response.data["reason_counts"] == {}
    assert response.data["corridor_width_policy_version"] == "fixed-1000m-v1"
    assert response.data["full_width_service_assurance_status"] == "not_evaluated"
    assert workflow.state == before
    assert workflow.calls == ["candidates"]


def test_post_simulation_exposes_identity_sites_coverage_timing_and_zero_writes():
    router, workflow = _router()
    before = deepcopy(workflow.state)
    response = router.post("/api/navigation-monitoring-simulation", {"route_id": "R0005"})
    assert response.status == 200
    data = response.data
    assert data["route_identity"]["route_geometry_fingerprint"] == "route-fp"
    assert data["parameter_fingerprint"] == "parameter-fp"
    assert data["candidate_snapshot_fingerprint"] == "candidate-fp"
    assert data["risk_identity"]["profile_fingerprint"] == "risk-fp"
    assert data["selected_mandatory_site_ids"] == ["node:A", "node:B"]
    assert data["selected_optional_sites"] == [{"site_id": "tower-colocation:T1"}]
    assert data["devices"] == [{"device_id": "D1"}]
    assert data["merged_intervals_m"] == [[0, 100]]
    assert data["gaps"] == []
    assert data["longest_gap_duration_s"] == 0
    assert data["writes_performed"] == 0
    assert workflow.state == before


def test_get_fixed_corridor_geometry_is_read_only_and_passes_route_id():
    """Round38-N2：固定宽带只读几何接口不得写状态，且必须转发 route_id。"""

    router, workflow = _router()
    before = deepcopy(workflow.state)

    response = router.get("/api/fixed-corridor-geometry", {"route_id": ["R0005"]}, {})
    assert response.status == 200
    assert response.data["corridor_half_width_m"] == 500
    assert response.data["corridor_total_width_m"] == 1000
    assert response.data["width_policy_version"] == "fixed-1000m-v1"
    assert response.data["currentness"] == "stale"
    assert response.data["writes_performed"] == 0
    assert workflow.calls == [("fixed_corridor_geometry", "R0005")]
    assert workflow.state == before

    # 不带 route_id 时如实传 None（由服务层判定"未指定"，绝不猜默认航路）。
    router.get("/api/fixed-corridor-geometry", {}, {})
    assert workflow.calls[-1] == ("fixed_corridor_geometry", None)


def test_post_maps_chinese_validation_error_and_preserves_explicit_blocked_result():
    router, workflow = _router()
    invalid = router.post("/api/navigation-monitoring-simulation", {"force_endpoint_monitors": False})
    assert invalid.status == 400
    assert invalid.data == {"status": "invalid_parameters", "error": "强制端点导航服务监测设备不可关闭", "writes_performed": 0}

    blocked = router.post("/api/navigation-monitoring-simulation", {"blocked": True})
    assert blocked.status == 200
    assert blocked.data["status"] == "blocked"
    assert "坐标系" in blocked.data["message"]
    assert workflow.state == {"revision": 468, "sentinel": {"unchanged": True}}
