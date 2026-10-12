"""Round37-T3D-2A：三维 CNS 覆盖**只读端点**回归（全部离线）。

覆盖任务明确要求的端点行为：

1. ``GET /api/visualization-3d/communication-volumes`` 与
   ``GET /api/visualization-3d/rid-footprints`` 都是**只读 GET**，带 ETag 与
   ``If-None-Match → 304``；
2. 两条端点都**不写** ProjectState（``writes_performed = 0``、
   ``algorithms_invoked = []``），也不触碰 ``/api/workflow``；
3. 严格 / demo 状态、``operational_authorization``、当前性与输入指纹必须原样回显；
4. 未确认安装高度 ⇒ 通信包络 blocked；未配置陆域掩膜 ⇒ RID 分区 blocked；
5. 历史连续覆盖结果**只有**在 ``route_geometry_identity`` 匹配时才允许绑定
   （当前实现如实报告 ``binding_allowed = false`` 与原因）；
6. RID 端点**绝不**出现 Radar 扇区语义。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from cns_planner.api.router import ApiRouter
from cns_planner.application.visualization_3d_service import Visualization3DService
from cns_planner.domain.cns_service_contract import (
    COMMUNICATION_RADIUS_BY_SURFACE,
    RID_RADIUS_BY_SURFACE,
)


class LandMaskClassifier:
    """最小陆域分类器替身（**只**用于端点回归；生产用权威 LandMaskSource）。"""

    def __init__(self, surface="sea"):
        self.surface = surface

    def classify_many(self, points):
        return [self.surface] * len(points)


def state_with_devices(*, comm_origin=20.0, rid_origin=20.0, comm_confirmed=True,
                       rid_confirmed=True, land_mask=True, coverage_3d=None):
    devices = [
        {
            "device_id": "COMM-1", "name": "通信设备", "subsystem": "C",
            "service_key": "C:communication", "enabled": True, "confirmed": True,
            "coverage_geometry": {
                "model": "hemisphere", "slant_range_m": None,
                "model_scope": "geometric_only", "confirmed": True, "status": "confirmed",
                "radius_by_surface": dict(COMMUNICATION_RADIUS_BY_SURFACE),
            },
            "vertical_profile": {"service_origin_egm2008_m": None, "confirmed": False,
                                 "status": "pending_confirmation"},
        },
        {
            "device_id": "RID-1", "name": "RID 设备", "subsystem": "S",
            "service_key": "S:rid_cooperative",
            "type": {"technology": "network_remote_id", "target_cooperation": "cooperative"},
            "enabled": True, "confirmed": True,
            "coverage_geometry": {
                "model": "hemisphere", "slant_range_m": None,
                "model_scope": "geometric_only", "confirmed": True, "status": "confirmed",
                "radius_by_surface": dict(RID_RADIUS_BY_SURFACE),
            },
            "vertical_profile": {"service_origin_egm2008_m": None, "confirmed": False,
                                 "status": "pending_confirmation"},
        },
    ]
    facility_vertical = {
        "surface_elevation_m": None, "surface_vertical_reference": "egm2008_orthometric",
        "mount_height_agl_m": None, "service_origin_egm2008_m": comm_origin,
        "source": "TEST", "confirmed": comm_confirmed,
        "status": "confirmed" if comm_confirmed else "pending_confirmation",
    }
    state = {
        "revision": 468,
        "device_catalog": {"status": "passed", "items": devices},
        "existing_cns_facilities": {"items": [
            {"facility_id": "FAC-1", "site_id": "SITE-1", "name": "测试站",
             "coordinate": [122.191292092561, 29.9962235778595], "status": "active",
             "vertical_profile": facility_vertical,
             "devices": [
                 {"device_id": "COMM-1", "subsystem": "C",
                  "vertical_profile": {"service_origin_egm2008_m": comm_origin,
                                       "confirmed": comm_confirmed,
                                       "status": ("confirmed" if comm_confirmed
                                                  else "pending_confirmation")}},
                 {"device_id": "RID-1", "subsystem": "S",
                  "vertical_profile": {"service_origin_egm2008_m": rid_origin,
                                       "confirmed": rid_confirmed,
                                       "status": ("confirmed" if rid_confirmed
                                                  else "pending_confirmation")}},
             ]},
        ]},
        "spatial_3d": {"status": "pending_confirmation",
                       "canonical_vertical_reference": "egm2008_orthometric",
                       "site_vertical_profiles": {}},
        "surface_classification_policy": {
            "status": "confirmed", "confirmed": True,
            "land_mask_layer_name": "land", "coastal_uncertainty_buffer_m": 30.0,
            "semantics": "step5_shared_neutral_land_mask_classification_policy",
            "unknown_is_fail_closed": True,
        },
        "surface_class_facts": {
            "status": "stale", "input_fingerprint": "surfacefacts-abc",
            "surface_class_counts": {"land": 10, "sea": 20, "coastal_uncertain": 3,
                                     "unknown": 0},
            "classified_grid_cell_count": 33,
        },
    }
    if coverage_3d is not None:
        state["coverage_3d"] = coverage_3d
    return state


def service_for_state(state, *, land_mask_path="D:/fake/land_mask.gpkg",
                      classifier=None):
    service = Visualization3DService(
        state, data=SimpleNamespace(paths={"land_mask": land_mask_path}))
    if classifier is not None:
        service._surface_class_provider = lambda: (classifier, None)
    return service


def router_for(service, monkeypatch=None):
    """构造只读路由，并把 ``service_for`` 指到被测服务实例。

    真实路由通过 ``service_for(context)`` 装配服务；单元测试必须显式替换它，
    否则会去读 ``context.workflow``（测试里没有），从而拿到 503
    ``workflow_unavailable``——那是"测试装配缺失"，不是被测行为。
    """

    if monkeypatch is not None:
        from cns_planner.application import visualization_3d_service as module

        monkeypatch.setattr(module, "service_for", lambda context: service)
    router = ApiRouter.__new__(ApiRouter)
    router._visualization_3d = ApiRouter._visualization_3d.__get__(router, ApiRouter)
    context = SimpleNamespace(_visualization_3d_service=service)
    return router, context


# ------------------------------------------------------- 通信包络端点


def test_communication_volumes_endpoint_is_read_only_and_reports_identity(monkeypatch):
    state = state_with_devices()
    service = service_for_state(state)
    router, context = router_for(service, monkeypatch)
    response = router._visualization_3d(
        context, "/api/visualization-3d/communication-volumes", {}, {})
    assert response.status == 200
    payload = response.data
    assert payload["read_only"] is True
    assert payload["writes_performed"] == 0
    assert payload["algorithms_invoked"] == []
    assert payload["revision"] == 468
    assert payload["layer_id"] == "communication_volumes"
    assert payload["coverage_semantics"] == "engineering_geometric_coverage_not_measured"
    assert payload["slant_range_is_criterion"] is True
    assert payload["two_dimensional_disk_is_not_the_volume"] is True
    assert payload["default_contract_radius_m"] == 4000.0
    assert payload["volume_count"] == 1
    assert payload["available_count"] == 1
    assert response.headers["Cache-Control"] == "no-store"
    assert payload["etag"].startswith("vis3dcommunicationvolumesv1-")
    # 严格/demo 状态与运行许可必须原样回显（不得伪装）。
    assert payload["operational_authorization"] is False
    assert "engineering_demo_only" in payload
    assert "strict_safety_validation_passed" in payload


def test_communication_volumes_endpoint_supports_if_none_match(monkeypatch):
    service = service_for_state(state_with_devices())
    router, context = router_for(service, monkeypatch)
    first = router._visualization_3d(
        context, "/api/visualization-3d/communication-volumes", {}, {})
    etag = first.data["etag"]
    second = router._visualization_3d(
        context, "/api/visualization-3d/communication-volumes", {},
        {"If-None-Match": f'"{etag}"'})
    assert second.status == 304
    assert second.data == b""


def test_communication_volumes_blocks_unconfirmed_installation_height(monkeypatch):
    # 站址与设备的安装高程都未确认 ⇒ 不得生成确定三维包络。
    state = state_with_devices(comm_origin=None, comm_confirmed=False)
    service = service_for_state(state)
    router, context = router_for(service, monkeypatch)
    payload = router._visualization_3d(
        context, "/api/visualization-3d/communication-volumes", {}, {}).data
    assert payload["available_count"] == 0
    assert payload["blocked_count"] == 1
    volume = payload["volumes"][0]
    assert volume["geometry"]["status"] == "blocked"
    assert volume["geometry"]["shell_rings"] == []
    assert volume["geometry"]["required_evidence"]
    assert volume["origin_confirmed"] is False


def test_communication_volumes_honours_explicit_surface_class(monkeypatch):
    state = state_with_devices()
    service = service_for_state(state)
    router, context = router_for(service, monkeypatch)
    land = router._visualization_3d(
        context, "/api/visualization-3d/communication-volumes",
        {"surface_class": ["land"]}, {}).data
    assert land["volumes"][0]["geometry"]["radius_basis"] == "per_surface_resolution"
    assert land["volumes"][0]["geometry"]["envelope_over_surfaces"] is False


# ------------------------------------------------------- RID footprint 端点


def test_rid_footprints_endpoint_is_read_only_and_never_a_radar_sector(monkeypatch):
    service = service_for_state(state_with_devices(),
                                classifier=LandMaskClassifier("sea"))
    router, context = router_for(service, monkeypatch)
    response = router._visualization_3d(
        context, "/api/visualization-3d/rid-footprints", {}, {})
    assert response.status == 200
    payload = response.data
    assert payload["read_only"] is True
    assert payload["writes_performed"] == 0
    assert payload["algorithms_invoked"] == []
    assert payload["not_a_radar_sector"] is True
    assert payload["omnidirectional"] is True
    assert payload["horizontal_coverage_deg"] == 360.0
    assert payload["forbidden_geometry"] == [
        "radar_90_degree_panel", "sector", "horizontal_semicircle", "panel_azimuth"]
    assert payload["radius_by_surface_m"] == dict(RID_RADIUS_BY_SURFACE)
    assert payload["vertical"]["extrusion_forbidden"] is True
    assert payload["vertical"]["vertical_effective_range"] == "not_modelled"
    assert payload["land_mask"]["classification_algorithm_copied"] is False
    assert payload["land_mask"]["authority"].endswith("LandMaskSource.classify_many")
    assert payload["coverage_semantics"] == (
        "engineering_geometric_cooperative_surveillance_not_measured")
    # 全海 ⇒ 半径恒为 5000，且必须如实标注"半径不随方位角变化"。
    footprint = payload["footprints"][0]
    assert footprint["geometry"]["status"] == "available"
    assert footprint["geometry"]["boundary_radius_m"]["max"] == 5000.0
    assert footprint["geometry"]["non_circular_evidence"]["radius_varies_with_azimuth"] is False
    # **绝不**出现 Radar 扇区语义字段。注意 ``forbidden_geometry`` 是"禁止清单"
    # 本身（它列出被禁止的几何），必须先从检查对象里**递归**剔除。
    def scrub(value):
        if isinstance(value, dict):
            return {key: scrub(item) for key, item in value.items()
                    if key != "forbidden_geometry"}
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value

    blob = json.dumps(scrub(payload), ensure_ascii=False).lower()
    for forbidden in ("panel_azimuth", "beamwidth", "boresight", "sector_deg",
                      "azimuth_deg"):
        assert forbidden not in blob


def test_rid_footprints_blocks_without_land_mask_classifier(monkeypatch):
    service = service_for_state(state_with_devices(), land_mask_path=None)
    router, context = router_for(service, monkeypatch)
    payload = router._visualization_3d(
        context, "/api/visualization-3d/rid-footprints", {}, {}).data
    assert payload["land_mask"]["status"] == "blocked"
    assert payload["land_mask"]["error"]["code"] == "land_mask_not_configured"
    assert payload["available_count"] == 0
    footprint = payload["footprints"][0]
    assert footprint["geometry"]["status"] == "blocked"
    assert footprint["geometry"]["reason"] == "land_mask_classifier_unavailable"
    assert footprint["geometry"]["geojson"] is None


def test_rid_footprints_reports_rid_declaration_basis_honestly(monkeypatch):
    service = service_for_state(state_with_devices(),
                                classifier=LandMaskClassifier("sea"))
    router, context = router_for(service, monkeypatch)
    payload = router._visualization_3d(
        context, "/api/visualization-3d/rid-footprints", {}, {}).data
    footprint = payload["footprints"][0]
    assert footprint["declaration_basis"] == "explicit_rid_declaration"
    assert footprint["rid_type_declared"] is True
    assert footprint["surface_class_facts_fingerprint"] == "surfacefacts-abc"
    assert payload["land_mask"]["surface_class_facts"]["status"] == "stale"
    assert payload["land_mask"]["policy"]["unknown_is_fail_closed"] is True


def test_rid_footprints_supports_if_none_match(monkeypatch):
    service = service_for_state(state_with_devices(),
                                classifier=LandMaskClassifier("sea"))
    router, context = router_for(service, monkeypatch)
    first = router._visualization_3d(
        context, "/api/visualization-3d/rid-footprints", {}, {})
    etag = first.data["etag"]
    assert etag.startswith("vis3dridfootprintsv1-")
    second = router._visualization_3d(
        context, "/api/visualization-3d/rid-footprints", {},
        {"If-None-Match": f'"{etag}"'})
    assert second.status == 304


# ------------------------------------------------- 历史覆盖结果绑定规则


def test_historical_coverage_is_not_bound_without_matching_route_identity(monkeypatch):
    coverage = {
        "status": "stale", "algorithm_id": "geometric_coverage_3d_v1",
        "input_fingerprint": "coverage-fingerprint-xyz",
        "routes": [{"route_id": "R0005", "status": "failed"}],
    }
    service = service_for_state(state_with_devices(coverage_3d=coverage),
                                classifier=LandMaskClassifier("sea"))
    router, context = router_for(service, monkeypatch)
    payload = router._visualization_3d(
        context, "/api/visualization-3d/communication-volumes", {}, {}).data
    block = payload["route_intersection"]
    assert block["binding_allowed"] is False
    assert block["historical_continuous_coverage_binding"] == "not_bound"
    assert block["coverage_3d_status"] == "stale"
    assert block["coverage_3d_input_fingerprint"] == "coverage-fingerprint-xyz"
    assert block["coverage_3d_algorithm_id"] == "geometric_coverage_3d_v1"
    # 本状态里没有生效的 demo 航路 ⇒ 原因必须是"无航路几何身份可比"，
    # 而不是被报成"指纹不匹配"（那是另一种事实）。
    assert block["reason"] == "no_engineering_demo_route_available"
    assert block["geometry_fingerprint"] is None
    assert "route_geometry" in block["binding_rule"]
    assert block["note"].startswith("本服务**不重跑**覆盖算法")


def test_historical_coverage_reports_fingerprint_mismatch_when_route_present(monkeypatch):
    """有 demo 航路时：coverage_3d 的输入指纹**不是** route_geometry 身份 ⇒ 不绑定。"""

    state = state_with_devices(coverage_3d={
        "status": "stale", "algorithm_id": "geometric_coverage_3d_v1",
        "input_fingerprint": "coverage-fingerprint-xyz",
        "routes": [{"route_id": "R0005", "status": "failed"}],
    })
    state["layered_operational_adoptions"] = {"items": [
        {"status": "published_engineering_demo", "current_applicability": "current",
         "adoption_id": "ADOPT-1", "route_id": "R0005",
         "candidate_id": "CAND-1", "validation_id": "VAL-1",
         "projection_fingerprint": "proj-1",
         "after": {"route": {"route_id": "R0005", "status": "engineering_demo_only",
                             "path": [[122.10, 29.90], [122.20, 29.95],
                                      [122.30, 30.00]]}}},
    ]}
    service = service_for_state(state, classifier=LandMaskClassifier("sea"))
    router, context = router_for(service, monkeypatch)
    block = router._visualization_3d(
        context, "/api/visualization-3d/communication-volumes", {}, {}
    ).data["route_intersection"]
    assert block["binding_allowed"] is False
    assert block["route_id"] == "R0005"
    assert block["geometry_fingerprint"].startswith("routegeometryv1-")
    assert block["reason"] == (
        "coverage_3d_input_fingerprint_is_not_a_route_geometry_identity")
    assert block["coverage_3d_routes"] == [{"route_id": "R0005", "status": "failed"}]


def test_coverage_endpoints_never_touch_workflow_snapshot(monkeypatch):
    from cns_planner.application import visualization_3d_service as module

    before = module.workflow_snapshot_requests_observed()
    service = service_for_state(state_with_devices(),
                                classifier=LandMaskClassifier("sea"))
    router, context = router_for(service, monkeypatch)
    router._visualization_3d(
        context, "/api/visualization-3d/communication-volumes", {}, {})
    router._visualization_3d(
        context, "/api/visualization-3d/rid-footprints", {}, {})
    assert module.workflow_snapshot_requests_observed() == before


def test_manifest_lists_coverage_layers_as_available_not_reserved():
    service = service_for_state(state_with_devices())
    manifest = service.manifest()
    layers = {layer["id"]: layer for layer in manifest["layers"]}
    assert layers["communication_volumes"]["status"] == "available"
    assert layers["communication_volumes"]["count"] == 1
    assert layers["communication_volumes"]["detail_endpoint"] == (
        "/api/visualization-3d/communication-volumes")
    assert layers["rid_footprints"]["status"] == "available"
    assert layers["rid_footprints"]["detail_endpoint"] == (
        "/api/visualization-3d/rid-footprints")
    # 尚未实现的三类仍然保持 reserved，绝不假装已实现。
    for layer_id in ("radar_volumes", "protection_volumes"):
        assert layers[layer_id]["status"] == "not_implemented"
        assert layers[layer_id]["reserved"] is True
    assert layers["simulated_intrusion"]["status"] == "reserved_simulated"
    assert layers["simulated_intrusion"]["reserved"] is True
    assert manifest["object_id_prefixes"]["communication_volume"] == "comm-volume:"
    assert manifest["object_id_prefixes"]["rid_footprint"] == "rid-footprint:"
