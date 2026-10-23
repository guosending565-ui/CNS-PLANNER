"""Round37-T3D-3B 保护体渲染实例身份的唯一性与隔离回归。

覆盖任务书 §2 与 §6 的第 13、17 项：同一冻结业务 ``object_id`` 下的多实例
（历史 11 点 / demo 14 点 / 用户示意）必须得到**互不相同且稳定**的实例键，
不得以数组下标充当长期标识；只读端点不得新增任何 ProjectState 写入者。
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from cns_planner.api.router import ApiRouter
from cns_planner.application.visualization_3d_protection_volume import (
    build_engineering_preview,
    route_path_fingerprint,
)
from cns_planner.application.visualization_3d_service import Visualization3DService


HISTORICAL_PATH = [[122.20, 29.90], [122.21, 29.905], [122.22, 29.91]]
DEMO_PATH = [[122.20, 29.90], [122.207, 29.902], [122.214, 29.907], [122.22, 29.91]]


def _state(entries_order="demo_first"):
    routes = [{
        "route_id": "R0005", "status": "stale", "stale_reason": "workspace_changed",
        "path": HISTORICAL_PATH,
        "provenance": {"candidate_id": "historical-11", "validation_id": "VAL-11"},
    }]
    adoption = {
        "status": "published_engineering_demo", "current_applicability": "current",
        "adoption_id": "ADOPT-DEMO", "route_id": "R0005",
        "candidate_id": "demo-14", "validation_id": "VAL-14",
        "projection_fingerprint": "demo-projection",
        "strict_safety_validation_passed": False, "operational_authorization": False,
        "after": {"route": {
            "route_id": "R0005", "status": "engineering_demo_only", "path": DEMO_PATH,
        }},
    }
    state = {
        "revision": 468,
        "spatial_3d": {
            "canonical_vertical_reference": "egm2008_orthometric",
            "altitude_layers": [{
                "altitude_layer_id": "ALT-100", "nominal_altitude_m": 100.0,
                "lower_altitude_m": 90.0, "upper_altitude_m": 125.0,
                "vertical_reference": "EGM2008", "status": "confirmed", "confirmed": True,
            }],
            "route_operating_layers": [{
                "route_id": "R0005", "altitude_layer_id": "ALT-100",
                "active": True, "confirmed": False,
                "source": "software project default, non measured/project source",
            }],
            "route_3d_profiles": {"items": []},
        },
        "operational_routes": {"items": routes},
        "layered_operational_adoptions": {"items": [adoption]},
        "continuous_service_acceptability": {
            "status": "stale", "stale_reason": "p16_or_upstream_changed",
            "algorithm_id": "continuous_service_acceptability_v1",
            "algorithm_version": "2.2", "input_fingerprint": "p17-input",
            "routes": [{
                "route_id": "R0005",
                "corridor": {
                    "status": "evaluated", "route_path": HISTORICAL_PATH,
                    "route_length_m": 9904.422448544,
                    "cns_requirement_corridor_half_width_m": 500.0,
                    "D_separation_m": 30.0, "D_separation_authority": "explicit_evidence",
                    "D_maneuver_m": 50.0,
                    "D_maneuver_authority": "builtin_engineering_assumption",
                    "D_uncertainty_m": 10.0,
                    "D_uncertainty_authority": "explicit_evidence",
                    "V_relative_mps": 40.0, "relative_speed_basis": "conservative",
                    "T_chain_s": 10.0,
                    "T_chain_components_s": {
                        "detect_track": 3.0, "sensor_to_platform": 1.0,
                        "platform_processing": 2.0, "platform_to_aircraft": 1.0,
                        "aircraft_response_manoeuvre": 3.0,
                    },
                    "D_protection_m": 490.0, "outer_half_width_m": 990.0,
                },
            }],
        },
        "encounter_3d_assessment": {"status": "not_calculated", "tracks": []},
    }
    assert entries_order in ("demo_first", "canonical_first")
    return state


def _service(state=None):
    return Visualization3DService(state if state is not None else _state(),
                                  data=SimpleNamespace(paths={}))


def _router(service, monkeypatch):
    from cns_planner.application import visualization_3d_service as module

    monkeypatch.setattr(module, "service_for", lambda context: service)
    router = ApiRouter.__new__(ApiRouter)
    router._visualization_3d = ApiRouter._visualization_3d.__get__(router, ApiRouter)
    return router, SimpleNamespace(_visualization_3d_service=service)


def _hash(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _instances(payload):
    return {row["route_authority"]: row for row in payload["volumes"]}


def _track_query(payload):
    nodes = [
        {"longitude": 122.20, "latitude": 29.895, "altitude_egm2008_m": 100.0,
         "relative_time_s": 0.0},
        {"longitude": 122.21, "latitude": 29.916, "altitude_egm2008_m": 100.0,
         "relative_time_s": 10.0},
    ]
    return json.dumps(nodes)


# --------------------------------------------------- 13. 同 object_id 多实例


def test_same_object_id_yields_distinct_instance_keys_for_the_two_p17_geometries():
    payload = _service().protection_volumes({})

    rows = _instances(payload)
    historical = rows["canonical_operational_routes"]
    demo = rows["engineering_demo_projection"]

    # 业务 ID 完全相同——这正是身份歧义的来源。
    assert historical["object_id"] == demo["object_id"] == (
        "protection-corridor:R0005:ALT-100")
    # 实例键必须互不相同。
    assert historical["render_instance_id"] != demo["render_instance_id"]
    assert historical["render_instance_id"].startswith("protectioninstancev1-")
    assert len(payload["render_instance_ids"]) == len(set(payload["render_instance_ids"]))
    assert payload["render_instance_id_unique"] is True
    assert payload["duplicate_render_instance_ids"] == []


def test_historical_corridor_key_is_bound_to_its_own_route_geometry():
    """11 点历史保护区的实例键绝不等于 14 点 demo 的实例键。"""

    payload = _service().protection_volumes({})
    rows = _instances(payload)

    canonical = rows["canonical_operational_routes"]
    demo = rows["engineering_demo_projection"]
    assert canonical["p17_route_geometry_identity"]["fingerprint"] == (
        route_path_fingerprint(HISTORICAL_PATH))
    assert canonical["route_geometry_identity"]["fingerprint"] != (
        demo["route_geometry_identity"]["fingerprint"])
    assert route_path_fingerprint(HISTORICAL_PATH) != route_path_fingerprint(DEMO_PATH)


def test_instance_keys_are_stable_across_repeated_requests():
    service = _service()
    first = service.protection_volumes({})["render_instance_ids"]
    second = service.protection_volumes({})["render_instance_ids"]

    assert first == second


def test_instance_keys_do_not_depend_on_response_ordering():
    """数组下标不是身份：即便响应里 volume 顺序变化，键集合必须一致。"""

    payload = _service().protection_volumes({})
    forward = [row["render_instance_id"] for row in payload["volumes"]]
    reversed_rows = list(reversed(payload["volumes"]))

    assert sorted(forward) == sorted(row["render_instance_id"] for row in reversed_rows)
    assert len(set(forward)) == len(forward)


def test_preview_instance_is_separated_from_the_historical_instance_family():
    service = _service()
    demo = next(row for row in service.routes()["routes"]
                if row["authority"] == "engineering_demo_projection")
    preview = build_engineering_preview(
        demo, half_width_m=250.0, lower_altitude_m=90.0, upper_altitude_m=125.0)
    historical = _instances(service.protection_volumes({}))

    assert preview["render_instance_id"].startswith("protectioninstancev1-")
    for row in historical.values():
        assert row["render_instance_id"] != preview["render_instance_id"]
    assert preview["object_id"].startswith("protection-preview:")
    assert historical["canonical_operational_routes"]["object_id"].startswith(
        "protection-corridor:")


def test_route_geometry_change_changes_only_that_instance_key():
    service = _service()
    before = _instances(service.protection_volumes({}))

    changed = _state()
    changed["continuous_service_acceptability"]["routes"][0]["corridor"]["route_path"] = (
        HISTORICAL_PATH + [[122.23, 29.915]])
    after = _instances(_service(changed).protection_volumes({}))

    assert (after["canonical_operational_routes"]["render_instance_id"]
            != before["canonical_operational_routes"]["render_instance_id"])
    # demo 实例未受影响 ⇒ 身份不会"整批漂移"。
    assert (after["engineering_demo_projection"]["render_instance_id"]
            == before["engineering_demo_projection"]["render_instance_id"])


def test_switching_layers_never_references_a_cleared_instance(monkeypatch):
    """图层切换后仍可用的实例键必须能定位到实例；已不存在的键必须显式报错。"""

    service = _service()
    router, context = _router(service, monkeypatch)

    live_key = _instances(service.protection_volumes({}))[
        "canonical_operational_routes"]["render_instance_id"]
    from cns_planner.application.visualization_3d_service import Visualization3DUnavailable

    # 同一个键在不同请求之间保持可解析（这就是"切换图层不丢引用"的条件）。
    assert _instances(service.protection_volumes({}))[
        "canonical_operational_routes"]["render_instance_id"] == live_key

    with pytest.raises(Visualization3DUnavailable) as excinfo:
        service.intrusion_assessment({
            "mode": "project_result",
            "render_instance_id": "protectioninstancev1-no-longer-present",
            "track": _track_query(None),
        })
    assert excinfo.value.code == "intrusion_protection_instance_not_found"
    assert router is not None and context is not None


# ------------------------------------------------- 17. 不新增 ProjectState 写入者


def test_read_only_endpoints_leave_the_project_state_byte_identical(monkeypatch):
    state = _state()
    before = _hash(state)
    service = _service(state)
    router, context = _router(service, monkeypatch)

    manifest = router._visualization_3d(
        context, "/api/visualization-3d/manifest", {}, {})
    routes = router._visualization_3d(
        context, "/api/visualization-3d/routes", {}, {})
    protection = router._visualization_3d(
        context, "/api/visualization-3d/protection-volumes", {}, {})
    assert manifest.status == 200 and routes.status == 200 and protection.status == 200

    assert protection.data["writes_performed"] == 0
    assert protection.data["read_only"] is True
    assert _hash(state) == before


def test_visualization_3d_service_has_no_project_state_writer_entry_point():
    """结构性断言：三维只读服务不暴露任何 workflow/写入器依赖。"""

    service = _service()
    for name in ("workflow", "apply_workflow_snapshot", "mutate", "refresh_workflow",
                 "save", "write_state"):
        assert not hasattr(service, name), name


def test_intrusion_assessment_endpoint_is_read_only_and_bounded(monkeypatch):
    state = _state()
    before = _hash(state)
    service = _service(state)
    router, context = _router(service, monkeypatch)
    historical = _instances(service.protection_volumes({}))[
        "canonical_operational_routes"]

    response = router._visualization_3d(
        context, "/api/visualization-3d/intrusion-assessment", {
            "mode": ["project_result"],
            "render_instance_id": [historical["render_instance_id"]],
            "track_id": ["SIM-A"],
            "track": [_track_query(None)],
        }, {})

    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    payload = response.data
    assert payload["simulated"] is True
    assert payload["render_instance_id"] == historical["render_instance_id"]
    assert payload["read_only"] is True
    assert payload["writes_performed"] == 0
    assert payload["detection_evidence_accepted_from_client"] is False
    assert payload["surveillance_detection"]["status"] == "not_evaluated"
    assert payload["eligible_for_current_warning"] is False
    assert _hash(state) == before


def test_conflicting_protection_geometry_cannot_run_a_simulation(monkeypatch):
    """几何版本冲突（14 点 demo vs P17 corridor）绝不允许出模拟结论。"""

    service = _service()
    router, context = _router(service, monkeypatch)
    demo = _instances(service.protection_volumes({}))["engineering_demo_projection"]

    response = router._visualization_3d(
        context, "/api/visualization-3d/intrusion-assessment", {
            "mode": ["project_result"],
            "render_instance_id": [demo["render_instance_id"]],
            "track_id": ["SIM-CONFLICT"],
            "track": [_track_query(None)],
        }, {})

    assert response.status == 400
    assert response.data["code"] == "intrusion_assessment_rejected"
    assert "not available" in response.data["error"]


def test_intrusion_assessment_requires_an_explicit_instance_binding(monkeypatch):
    service = _service()
    router, context = _router(service, monkeypatch)

    response = router._visualization_3d(
        context, "/api/visualization-3d/intrusion-assessment", {
            "mode": ["project_result"],
            "track": [_track_query(None)],
        }, {})

    assert response.status == 400
    assert response.data["code"] == "intrusion_binding_required"


def test_preview_mode_simulation_is_isolated_and_bound(monkeypatch):
    state = _state()
    before = _hash(state)
    service = _service(state)
    router, context = _router(service, monkeypatch)
    demo = next(row for row in service.routes()["routes"]
                if row["authority"] == "engineering_demo_projection")
    preview_query = {
        "mode": ["engineering_scenario_preview"],
        "route_geometry_fingerprint": [demo["identity"]["fingerprint"]],
        "half_width_m": ["250"], "lower_altitude_m": ["90"], "upper_altitude_m": ["125"],
    }
    preview = router._visualization_3d(
        context, "/api/visualization-3d/protection-volumes", preview_query, {}).data
    instance_id = preview["volumes"][0]["render_instance_id"]

    response = router._visualization_3d(
        context, "/api/visualization-3d/intrusion-assessment", {
            **preview_query,
            "render_instance_id": [instance_id],
            "track_id": ["SIM-PREVIEW"],
            "track": [_track_query(None)],
        }, {})

    assert response.status == 200
    payload = response.data
    assert payload["mode"] == "engineering_scenario_preview"
    assert payload["render_instance_id"] == instance_id
    assert payload["three_dimensional_volume_intrusion"]["status"] in {
        "intrudes", "clear"}
    assert payload["isolation"]["enters_formal_p17"] is False
    assert payload["isolation"]["enters_cns_statistics"] is False
    assert _hash(state) == before

    mismatched = router._visualization_3d(
        context, "/api/visualization-3d/intrusion-assessment", {
            **preview_query,
            "render_instance_id": ["protectioninstancev1-somewhere-else"],
            "track_id": ["SIM-PREVIEW"],
            "track": [_track_query(None)],
        }, {})
    assert mismatched.status == 400
    assert mismatched.data["code"] == "intrusion_assessment_rejected"
    assert "render_instance_id" in mismatched.data["error"]


def test_manifest_marks_the_simulated_layer_available_but_default_off():
    manifest = _service().manifest()
    layer = next(row for row in manifest["layers"] if row["id"] == "simulated_intrusion")

    assert layer["status"] == "available"
    assert layer["reserved"] is False
    assert layer["default_on"] is False
    assert layer["detail_endpoint"] == "/api/visualization-3d/intrusion-assessment"
    assert layer["object_id_prefix"] == "sim-intruder:"
