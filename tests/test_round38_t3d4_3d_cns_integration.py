"""Round38-T3D4：三维 CNS 站址身份与固定 1000 m 目标带的只读投影回归（全离线）。

覆盖本轮新增的两条只读端点的**身份与门禁**行为：

1. ``GET /api/visualization-3d/cns-sites``
   - 只读（``writes_performed = 0`` / ``algorithms_invoked = []``）、带 ETag、
     ``Cache-Control: no-store``；
   - 四类站址身份严格分层：既有设施 / 规划选中 / 候选未选中 / blocked；
   - **站址存在绝不推断设备已安装**：候选站址的 ``devices`` 必须为空；
   - 塔顶安装与地面站安装语义分别表达，地面站**绝不**继承塔顶高度；
   - 缺少塔顶证据的铁塔**不**生成三维结构几何（不伪造高度）；
   - 设备类别配色与二维一致：C 绿 / N 黄 / RID 橙 / Radar 蓝。

2. ``GET /api/visualization-3d/fixed-corridor-bands``
   - 几何**只**来自 ``build_fixed_corridor_geometry``（宽度策略 ``fixed-1000m-v1``，
     左右各 500 m），三维层不重算 buffer、不使用 P17 旧 ``outer_half_width_m``；
   - 传入 P14 口径 ``route_authority`` 时不出现假阳性 stale；身份不一致时如实 stale；
   - ``currentness != current`` 时 ``drawable_as_current_band = false``；
   - 没有已确认巡航高度层时 ``vertical_extent_status = "unconfirmed"``，
     且恒不声明三维认证保障体积。

只跑定向断言，不做真实舟山重算、不启动浏览器、不触碰正式 ProjectState。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cns_planner.api.router import ApiRouter
from cns_planner.application.cns_3d_site_projection import (
    SERVICE_CATEGORY_COLORS,
    classify_service,
    site_records,
)
from cns_planner.application.cns_shared_site_candidate_service import (
    CNSSharedSiteCandidateService,
)
from cns_planner.application.fixed_corridor_geometry_service import (
    FixedCorridorGeometryService,
)
from cns_planner.application.visualization_3d_service import Visualization3DService
from cns_planner.domain.layered_route_validation import path_fingerprint

#: 一条合法的 5 点航路（与 W1A 样例同城域，能解析米制构造 CRS）。
ROUTE_PATH = [
    [122.20, 29.95], [122.22, 29.96], [122.24, 29.97],
    [122.26, 29.98], [122.28, 29.99],
]


# --------------------------------------------------------------------- 装配


def _shared_snapshot():
    """用**生产**共享候选站址服务构造只读快照（不外挂第二套站址口径）。"""

    state = {
        "nodes": [
            {
                "node_id": "N001", "name": "已确认地面站起降点",
                "coordinate": [122.21, 29.955], "coordinate_source": "reference_site",
                "reference_site_id": "RLS-N001",
                "radar_installation": {
                    "installation_height_m": 12.0, "source": "user_confirmed",
                    "confirmed": True, "device_installed": True,
                },
            },
            {
                "node_id": "N002", "name": "缺安装高度证据的起降点",
                "coordinate": [122.23, 29.965],
                "radar_installation": {
                    "installation_height_m": None, "source": "engineering_assumption",
                    "confirmed": False,
                },
            },
        ],
    }
    session = SimpleNamespace(state=state)
    landing = {
        "crs": {
            "source_crs": {"value": "EPSG:4490", "confirmed": True,
                           "status": "confirmed", "axis_order": "Lat:north / Lon:east"},
            "representation_crs": {"value": "OGC:CRS84", "status": "declared",
                                   "axis_order": "lon_lat"},
        },
        "items": [
            {"reference_site_id": "RLS-N001", "name": "与项目节点已链接的参考起降点",
             "site_type": "直升机场", "coordinate": [122.21, 29.955],
             "quality": "parsed", "source": {"file": "landing.xlsx"}},
            {"reference_site_id": "RLS-ALONE", "name": "未链接的参考起降点",
             "site_type": "临时起降点", "coordinate": [122.245, 29.972],
             "quality": "parsed", "source": {"file": "landing.xlsx"}},
        ],
    }
    service = CNSSharedSiteCandidateService(
        session,
        lambda: landing,
        lambda: {
            "crs": {
                "source_crs": {"value": "EPSG:4326", "confirmed": True,
                               "status": "confirmed"},
            },
            "data_source": "tower_inventory",
            "items": [
                {"tower_id": "T-TOP", "name": "有塔顶证据的塔",
                 "longitude": 122.25, "latitude": 29.975, "site_type": "角钢塔"},
                {"tower_id": "T-NOTOP", "name": "缺塔顶证据的塔",
                 "longitude": 122.27, "latitude": 29.985, "site_type": "角钢塔"},
            ],
        },
        lambda: {"items": [], "crs": {}},
    )
    return service.snapshot()


def _state():
    return {
        "revision": 471,
        "nodes": [],
        "operational_routes": [
            {
                "route_id": "R0005", "status": "stale", "path": ROUTE_PATH,
                "path_crs": "OGC:CRS84", "distance_m": None,
                "provenance": {
                    "source_type": "layered_candidate_operational_adoption",
                    "candidate_id": "LRC2-R0005-ALT-100",
                    "candidate_fingerprint": "layeredcandv1-aaa",
                    "validation_id": "LRV-1",
                    "validation_fingerprint": "layeredvalidationv1-bbb",
                    "adoption_fingerprint": "layeredadoptionv1-ccc",
                },
            },
        ],
        "layered_operational_adoptions": {"status": "passed", "items": []},
        "continuous_service_acceptability": {
            "status": "stale", "algorithm_version": "2.2",
            "input_fingerprint": "p17-input",
        },
        "cns_corridor_assessment": {"status": "stale", "routes": []},
        "towers": {
            "status": "passed", "count": 2, "data_source": "tower_inventory",
            "crs": {"source_crs": {"value": "EPSG:4326", "confirmed": True,
                                   "status": "confirmed"}},
            "items": [
                {"tower_id": "T-TOP", "name": "有塔顶证据的塔",
                 "longitude": 122.25, "latitude": 29.975, "elevation_m": 20.0,
                 "height_m": 25.0, "site_type": "角钢塔"},
                {"tower_id": "T-NOTOP", "name": "缺塔顶证据的塔",
                 "longitude": 122.27, "latitude": 29.985, "elevation_m": 18.0,
                 "height_m": 30.0, "site_type": "角钢塔"},
            ],
        },
        # ``items`` 是 tower_id → 档案 的字典形态（与权威 ProjectState 一致）。
        "tower_obstacle_profiles": {
            "status": "passed", "count": 2,
            "items": {
                "T-TOP": {
                    "tower_id": "T-TOP", "longitude": 122.25, "latitude": 29.975,
                    "terrain_elevation_m": 19.5,
                    "terrain_vertical_reference": "egm2008_orthometric",
                    "terrain_status": "passed",
                    "tower_structure_height_m": 25.0,
                    "tower_top_orthometric_m": 44.5,
                    "tower_top_status": "resolved_unconfirmed",
                    "vertical_status": "egm2008_orthometric_resolved",
                    "base_type": "ground", "confirmed": False,
                },
                "T-NOTOP": {
                    "tower_id": "T-NOTOP", "longitude": 122.27, "latitude": 29.985,
                    "terrain_elevation_m": 17.6,
                    "terrain_vertical_reference": "egm2008_orthometric",
                    "terrain_status": "passed",
                    "tower_structure_height_m": 30.0,
                    "tower_top_orthometric_m": None,
                    "tower_top_status": "unknown",
                    "vertical_status": "base_type_unknown",
                    "base_type": "unknown", "confirmed": False,
                },
            },
        },
        "existing_cns_facilities": {"items": [
            {"facility_id": "FAC-SYN", "site_id": "FAC-SYN", "name": "合成站（不得进候选）",
             "coordinate": [122.29, 29.99], "status": "active",
             "metadata": {"dataset_class": "synthetic_engineering_validation",
                          "real_world_facility": False},
             "planning_profile": {"confirmed": True},
             "vertical_profile": {"service_origin_egm2008_m": 20.0, "confirmed": True,
                                  "status": "confirmed",
                                  "surface_vertical_reference": "egm2008_orthometric"},
             "devices": [{"device_id": "COMM-1", "subsystem": "C"}]},
        ]},
        "device_catalog": {"status": "passed", "items": [
            {"device_id": "COMM-1", "name": "通信设备", "subsystem": "C",
             "service_key": "C:communication"},
            {"device_id": "RID-1", "name": "RID 设备", "subsystem": "S",
             "service_key": "S:rid_cooperative"},
        ]},
        "radar_surveillance_layout": {"status": "not_calculated", "items": [
            {"route_id": "R0005", "status": "stale",
             "selected_tower_ids": ["T-TOP"],
             "selected_panels": [
                 {"panel_id": "RP-1", "tower_id": "T-TOP", "radar_type": "radar_i",
                  "azimuth_deg": 45.0},
             ]},
        ]},
        "cns_corridor_site_plan": {
            "status": "stale",
            "selected_actions": [
                {"action_id": "tower_colocation_host:tower-colocation:T-TOP:COMM-BASELINE-4KM",
                 "action_type": "add_device_to_explicit_site", "confirmed": True,
                 "device_id": "COMM-BASELINE-4KM",
                 "device_service_key": "C:communication",
                 "distinct_site_id": "tower:T-TOP",
                 "physical_mount_confirmed": False,
                 "planning_origin": {"host_tower_id": "T-TOP"},
                 "host": {"host_tower_id": "T-TOP",
                          "planning_host_use_confirmed": True}},
                {"action_id": "add_navigation_integrity_monitor:R0005:destination:N001",
                 "action_type": "add_navigation_integrity_monitor", "confirmed": True,
                 "coordinate": [122.21, 29.955],
                 "device_id": "NAVINT-R0005-destination-N001",
                 "device_service_key": "N:navigation_integrity_monitoring",
                 "distinct_site_id": "takeoff_landing_site:N001",
                 "equipment_selection_status": "not_selected"},
            ],
        },
        "spatial_3d": {
            "status": "pending_confirmation",
            "canonical_vertical_reference": "egm2008_orthometric",
            "altitude_layers": [
                {"altitude_layer_id": "ALT-100", "name": "100 m 巡航高度层",
                 "nominal_altitude_m": 100.0, "lower_altitude_m": 80.0,
                 "upper_altitude_m": 120.0,
                 "vertical_reference": "egm2008_orthometric",
                 "status": "confirmed", "confirmed": True},
            ],
            "route_operating_layers": [
                {"route_id": "R0005", "altitude_layer_id": "ALT-100", "active": True,
                 "confirmed": True, "vertical_reference": "egm2008_orthometric"},
            ],
            "route_altitude_profiles": {},
            "route_3d_profiles": {},
            "departure_arrival_procedures": [],
            "site_vertical_profiles": {},
        },
    }


def _corridor_service(state):
    corridor = SimpleNamespace(
        result_snapshot=lambda: {
            "algorithm_id": "cns_service_corridor_v1", "algorithm_version": "1.3",
            "routes": [
                {
                    "route_id": "R0005",
                    "fixed_corridor_geometry": {"geometry_fingerprint": None},
                    "fixed_corridor_geometry_status": "available",
                },
            ],
        },
    )
    service = FixedCorridorGeometryService(
        SimpleNamespace(state=state), corridor)
    return service


def _service(state):
    return Visualization3DService(
        state, shared_sites=_shared_snapshot(),
        fixed_corridor_service=_corridor_service(state),
    )


def _router(service, monkeypatch):
    from cns_planner.application import visualization_3d_service as module

    monkeypatch.setattr(module, "service_for", lambda context: service)
    router = ApiRouter.__new__(ApiRouter)
    router._visualization_3d = ApiRouter._visualization_3d.__get__(router, ApiRouter)
    return router, SimpleNamespace(_visualization_3d_service=service)


# --------------------------------------------------- 设备类别与配色（二维一致）


@pytest.mark.parametrize("kwargs,expected", [
    ({"service_key": "C:communication"}, "C"),
    ({"service_key": "N:navigation_integrity_monitoring"}, "N"),
    ({"service_key": "S:rid_cooperative"}, "RID"),
    ({"service_key": "S:radar_noncooperative"}, "Radar"),
    ({"subsystem": "C"}, "C"),
    ({"device_id": "RID-BASELINE-2-5KM"}, "RID"),
    ({"device_id": "NAVINT-R0005-destination-N001"}, "N"),
    ({"device_id": "NAV-1"}, "N"),
    ({"device_id": "COMM-BASELINE-4KM"}, "C"),
    ({"service_key": "X:unknown", "device_id": "MYSTERY-1"}, "other"),
])
def test_service_category_classification_is_explicit(kwargs, expected):
    assert classify_service(**kwargs) == expected


def test_service_category_colors_match_the_required_palette():
    assert SERVICE_CATEGORY_COLORS["C"] == "#37d67a"
    assert SERVICE_CATEGORY_COLORS["N"] == "#ffd166"
    assert SERVICE_CATEGORY_COLORS["RID"] == "#ff9f43"
    assert SERVICE_CATEGORY_COLORS["Radar"] == "#4d9dff"


# ------------------------------------------------------------ 站址身份分层


def test_site_records_separate_the_four_site_states():
    snapshot = _shared_snapshot()
    projection = site_records(state=_state(), shared_snapshot=snapshot, radar_inputs=None)
    by_id = {row["site_id"]: row for row in projection["sites"]}

    tower_top = by_id["tower-colocation:T-TOP"]
    # T-TOP 有已确认安装动作（该动作 confirmed=True）与已登记面阵 ⇒ 规划选中。
    assert tower_top["site_state"] == "planned_selected"
    assert tower_top["installation_position"] == "tower_top"
    assert tower_top["structure_geometry_available"] is True
    assert tower_top["terrain_elevation_m"] == 19.5
    assert tower_top["tower_top_orthometric_m"] == 44.5
    assert {row["category"] for row in tower_top["devices"]} == {"C", "Radar"}

    # 缺塔顶证据的塔仍是**同一类**站址身份（候选），不是"已布设"。
    no_top = by_id["tower-colocation:T-NOTOP"]
    assert no_top["site_state"] == "candidate_unselected"
    assert no_top["installation_position"] == "tower_top"
    assert no_top["devices"] == []

    # 参考起降点目录中**未与项目节点链接**的记录也会进入候选（同为候选站址身份）。
    alone = by_id["reference-landing-site:RLS-ALONE"]
    assert alone["site_kind"] == "reference_landing_site"
    assert alone["installation_position"] == "ground_site"
    assert alone["site_state"] == "candidate_unselected"
    # 已链接的参考起降点不会重复成第二个候选（同一物理站址只有一份身份）。
    assert "reference-landing-site:RLS-N001" not in by_id

    assert by_id["node:N001"]["site_state"] == "planned_selected"
    assert by_id["node:N002"]["site_state"] == "candidate_unselected"

def test_site_records_never_infer_device_from_site_presence():
    projection = site_records(state=_state(), shared_snapshot=_shared_snapshot())
    for row in projection["sites"]:
        if row["site_state"] == "candidate_unselected":
            assert row["devices"] == []
            assert row["device_categories"] == []
        assert row["never_infers_device_from_site_presence"] is True


def test_ground_site_never_inherits_tower_top_height():
    projection = site_records(state=_state(), shared_snapshot=_shared_snapshot())
    for row in projection["sites"]:
        if row["installation_position"] != "ground_site":
            continue
        assert row["tower_top_orthometric_m"] is None
        assert row["tower_id"] is None
        assert row["structure_geometry_available"] is False
        assert row["structure_geometry_blocked_reason"] == \
            "ground_site_has_no_confirmed_tower_structure_evidence"


def test_tower_without_tower_top_evidence_gets_no_structure_geometry():
    projection = site_records(state=_state(), shared_snapshot=_shared_snapshot())
    by_id = {row["site_id"]: row for row in projection["sites"]}
    no_top = by_id["tower-colocation:T-NOTOP"]
    assert no_top["tower_top_orthometric_m"] is None
    assert no_top["structure_geometry_available"] is False
    assert no_top["structure_geometry_basis"] is None
    # Round38-INT-A：缺证原因显式分开"没有严格塔顶事实"与"也没有既有共塔工程估算"，
    # 因为后者是可以被显式 opt-in 复用的既有能力，不能被笼统说成"完全没有高度数据"。
    assert no_top["structure_geometry_blocked_reason"] == \
        "tower_top_orthometric_m_unresolved_and_no_planning_estimate"
    # 本 fixture 未打开共塔工程估算策略 ⇒ 估算原点和估算几何都必须缺席（fail-closed）。
    assert no_top["structure_geometry_authority"] is None
    assert no_top["planning_estimate_origin_egm2008_m"] is None
    assert no_top["planning_estimate_used_for_geometry"] is False
    # 结构高度（源文件字段）仍如实回显，但它本身不是"已确认高度"。
    assert no_top["tower_structure_height_m"] == 30.0
    assert no_top["tower_top_confirmed"] is False
    assert projection["counts"]["towers_missing_tower_top"] == 1


def _with_planning_estimate(state, *, enabled=True, estimate_m=47.6):
    """打开/关闭既有共塔工程估算策略（**唯一**来源是 ``tower_colocation_policy``）。"""

    state["tower_colocation_policy"] = {
        "planning_host_use_confirmed": True,
        "planning_service_origin_policy": (
            "terrain_plus_source_tower_height" if enabled else None
        ),
    }
    state["tower_colocation_candidates"] = {
        "count": 1,
        "items": [{
            "site_id": "tower-colocation:T-NOTOP",
            "vertical_profile": {
                "planning_origin_estimated": True,
                "planning_service_origin_egm2008_m": estimate_m,
            },
            "metadata": {"host": {"host_tower_id": "T-NOTOP", "host_type": "tower"}},
        }],
    }
    return state


def test_planning_estimate_origin_is_consumed_only_when_explicitly_opted_in():
    """Round38-INT-A：既有工程估算高度能力必须被三维识别，但必须显式 opt-in。"""

    projection = site_records(
        state=_with_planning_estimate(_state()), shared_snapshot=_shared_snapshot())
    by_id = {row["site_id"]: row for row in projection["sites"]}
    estimated = by_id["tower-colocation:T-NOTOP"]
    # 严格塔顶事实仍然缺失——绝不被估算冒充。
    assert estimated["tower_top_orthometric_m"] is None
    assert estimated["tower_top_confirmed"] is False
    # 估算只用于**明确标注**的工程示意几何。
    assert estimated["structure_geometry_available"] is True
    assert estimated["structure_geometry_authority"] == "planning_estimate"
    assert estimated["planning_estimate_used_for_geometry"] is True
    assert estimated["planning_estimate_origin_egm2008_m"] == 47.6
    assert estimated["structure_geometry_blocked_reason"] is None
    # 有严格塔顶事实的塔仍必须走 strict，绝不被估算覆盖。
    strict = by_id["tower-colocation:T-TOP"]
    assert strict["structure_geometry_authority"] == "resolved_tower_top"
    assert strict["planning_estimate_used_for_geometry"] is False
    counts = projection["counts"]
    assert counts["towers_with_resolved_tower_top_geometry"] == 1
    assert counts["towers_with_planning_estimate_geometry"] == 1
    assert counts["towers_missing_tower_top"] == 0


def test_planning_estimate_origin_stays_closed_without_opt_in():
    """未显式选择工程估算原点策略时，估算值必须完全不生效（fail-closed）。"""

    projection = site_records(
        state=_with_planning_estimate(_state(), enabled=False),
        shared_snapshot=_shared_snapshot())
    by_id = {row["site_id"]: row for row in projection["sites"]}
    closed = by_id["tower-colocation:T-NOTOP"]
    assert closed["structure_geometry_available"] is False
    assert closed["structure_geometry_authority"] is None
    assert closed["planning_estimate_origin_egm2008_m"] is None
    assert closed["planning_estimate_used_for_geometry"] is False
    assert closed["structure_geometry_blocked_reason"] == \
        "tower_top_orthometric_m_unresolved_and_no_planning_estimate"


def test_synthetic_existing_facility_stays_out_of_site_candidates():
    projection = site_records(state=_state(), shared_snapshot=_shared_snapshot())
    assert all(row["site_kind"] != "existing_cns_facility"
               for row in projection["sites"])


def test_planned_ground_action_is_marked_unconfirmed_and_never_installed():
    projection = site_records(state=_state(), shared_snapshot=_shared_snapshot())
    planned = {row["device_id"]: row for row in projection["planned_devices"]}
    row = planned["NAVINT-R0005-destination-N001"]
    assert row["installation_status"] == "planned_selected_unconfirmed"
    assert row["category"] == "N"
    # 站点规划动作的 `takeoff_landing_site:` 身份必须显式映射到候选站址的 `node:`
    # 身份，否则三维无法把"规划设备"挂到它真正的宿主站址上。
    assert row["linked_site_id"] == "node:N001"
    assert row["linked_site_available"] is True


# ------------------------------------------------------------ cns-sites 端点


def test_cns_sites_endpoint_is_read_only_and_layered(monkeypatch):
    service = _service(_state())
    router, context = _router(service, monkeypatch)
    response = router._visualization_3d(
        context, "/api/visualization-3d/cns-sites", {}, {})
    assert response.status == 200
    payload = response.data
    assert payload["read_only"] is True
    assert payload["writes_performed"] == 0
    assert payload["algorithms_invoked"] == []
    assert payload["revision"] == 471
    assert payload["shared_site_source"]["available"] is True
    assert payload["shared_site_source"]["count"] == len(payload["sites"])
    assert payload["identity_rules"]["site_presence_never_implies_device"] is True
    assert payload["identity_rules"]["tower_top_never_used_for_ground_site"] is True
    assert payload["identity_rules"]["unknown_is_not_zero"] is True
    assert response.headers["Cache-Control"] == "no-store"
    assert payload["etag"].startswith("vis3dcnssitesv1-")
    categories = {row["key"]: row for row in payload["service_categories"]}
    assert categories["C"]["color"] == "#37d67a"
    assert categories["N"]["color"] == "#ffd166"
    assert categories["RID"]["color"] == "#ff9f43"
    assert categories["Radar"]["color"] == "#4d9dff"
    assert payload["counts"]["tower_sites"] == 2
    assert payload["counts"]["ground_sites"] == 3
    # 地面站（含起降点）**不**生成任何由塔顶派生的结构几何。
    assert payload["counts"]["towers_with_structure_geometry"] == 1
    assert payload["counts"]["towers_missing_tower_top"] == 1


def test_cns_sites_endpoint_filters_by_site_state_and_route(monkeypatch):
    service = _service(_state())
    router, context = _router(service, monkeypatch)
    only_planned = router._visualization_3d(
        context, "/api/visualization-3d/cns-sites",
        {"site_state": ["planned_selected"]}, {}).data
    assert only_planned["sites"]
    assert all(row["site_state"] == "planned_selected" for row in only_planned["sites"])

    by_route = router._visualization_3d(
        context, "/api/visualization-3d/cns-sites", {"route_id": ["R0005"]}, {}).data
    assert {row["site_id"] for row in by_route["sites"]} == {
        "tower-colocation:T-TOP", "node:N001"}

    none_related = router._visualization_3d(
        context, "/api/visualization-3d/cns-sites", {"route_id": ["R9999"]}, {}).data
    assert none_related["sites"] == []


def test_cns_sites_endpoint_supports_if_none_match(monkeypatch):
    service = _service(_state())
    router, context = _router(service, monkeypatch)
    first = router._visualization_3d(
        context, "/api/visualization-3d/cns-sites", {}, {})
    etag = first.data["etag"]
    second = router._visualization_3d(
        context, "/api/visualization-3d/cns-sites", {}, {"If-None-Match": f'"{etag}"'})
    assert second.status == 304
    assert second.headers["Cache-Control"] == "no-store"


def test_site_identity_fingerprint_changes_with_the_candidate_set():
    state = _state()
    first = Visualization3DService(state, shared_sites=_shared_snapshot())
    second = Visualization3DService(state, shared_sites={})
    assert first.shared_site_identity()["fingerprint"] is not None
    assert second.shared_site_identity()["available"] is False
    assert first.identity_seed()["shared_site_identity"] != \
        second.identity_seed()["shared_site_identity"]


# ------------------------------------------------------- 固定 1000 m 目标带


def test_fixed_corridor_band_uses_the_single_authoritative_builder(monkeypatch):
    service = _service(_state())
    router, context = _router(service, monkeypatch)
    payload = router._visualization_3d(
        context, "/api/visualization-3d/fixed-corridor-bands", {}, {}).data
    assert payload["read_only"] is True
    assert payload["writes_performed"] == 0
    assert payload["algorithms_invoked"] == []
    assert payload["band_count"] == 1
    band = payload["bands"][0]
    assert band["width_policy_version"] == "fixed-1000m-v1"
    assert band["corridor_half_width_m"] == 500.0
    assert band["corridor_total_width_m"] == 1000.0
    assert band["geometry_algorithm_id"] == "cns_fixed_corridor_buffer_v1"
    assert payload["geometry_source"]["recomputes_buffer_in_3d"] is False
    assert payload["geometry_source"]["uses_p17_outer_half_width_m"] is False
    assert band["route_geometry_fingerprint"] == path_fingerprint(ROUTE_PATH)
    # 航路状态为 stale ⇒ 绝不显示为 current。
    assert band["currentness"] == "stale"
    assert band["drawable_as_current_band"] is False
    assert payload["identity_rules"]["stale_never_shown_as_current"] is True


def test_non_current_band_is_offered_only_as_labelled_historical_preview(monkeypatch):
    """非 current 时不得冒充当前保障结果，但可作**明确标注**的历史预览。"""

    service = _service(_state())
    router, context = _router(service, monkeypatch)
    band = router._visualization_3d(
        context, "/api/visualization-3d/fixed-corridor-bands", {}, {}).data["bands"][0]
    assert band["drawable_as_current_band"] is False
    assert band["preview_only"] is True
    assert band["must_be_labelled_non_authoritative"] is True
    if band["preview_allowed"]:
        # 几何构造成功（construction CRS 已解析）时才允许给历史预览几何，
        # 且必须带常驻的非权威标注与可绘制外环。
        assert band["preview_authority"] == (
            "non_authoritative_historical_preview_not_a_current_assurance_result")
        assert band["preview_polygons"]
        for ring in band["preview_polygons"]:
            assert len(ring) >= 4
            assert ring[0] == ring[-1]
        assert band["width_policy_version"] == "fixed-1000m-v1"
    else:
        # 几何构造被阻断 ⇒ fail-closed：既没有 current 带，也没有历史预览几何。
        assert band["preview_polygons"] == []
        assert band["preview_authority"] is None
        assert band["geometry_status"] != "available"


def test_fixed_corridor_band_vertical_extent_is_evidenced_or_unconfirmed(monkeypatch):
    state = _state()
    service = _service(state)
    router, context = _router(service, monkeypatch)
    band = router._visualization_3d(
        context, "/api/visualization-3d/fixed-corridor-bands", {}, {}).data["bands"][0]
    assert band["vertical_extent_status"] == "confirmed"
    assert band["nominal_altitude_m"] == 100.0
    assert band["altitude_layer_id"] == "ALT-100"
    assert band["vertical_reference"] == "egm2008_orthometric"
    assert band["may_claim_3d_certified_volume"] is False
    assert band["vertical_volume_semantics"] == \
        "nominal_cruise_layer_slice_not_a_certified_protection_volume"

    # 去掉已确认高度层 ⇒ 只给水平目标带，绝不伪造垂直上下界。
    state["spatial_3d"]["route_operating_layers"] = []
    band_without = router._visualization_3d(
        context, "/api/visualization-3d/fixed-corridor-bands", {}, {}).data["bands"][0]
    assert band_without["vertical_extent_status"] == "unconfirmed"
    assert band_without["nominal_altitude_m"] is None
    assert band_without["vertical_extent_reason"] == \
        "no_confirmed_fixed_cruise_layer_for_this_route"
    assert band_without["vertical_volume_semantics"] == \
        "horizontal_projection_only_no_vertical_guarantee"
    # 缺少垂直证据时只给水平投影：不得因此凭空造出三维体。
    assert band_without["may_claim_3d_certified_volume"] is False


def test_fixed_corridor_band_reports_not_found_instead_of_guessing(monkeypatch):
    service = _service(_state())
    router, context = _router(service, monkeypatch)
    payload = router._visualization_3d(
        context, "/api/visualization-3d/fixed-corridor-bands",
        {"route_id": ["NO-SUCH-ROUTE"]}, {}).data
    assert payload["bands"] == []
    assert payload["status"] == "unavailable"
    assert payload["reason"] == "requested_route_not_found"


def test_p14_route_authority_keeps_the_band_identity_consistent():
    """``route_authority`` 必须是 P14 口径的那一份，否则几何身份会分叉。

    ``build_fixed_corridor_band(..., route_authority=None)`` 会**回退**到它自己的
    ``_route_authority(route)``，因此"不传"并不等于"身份不同"。真正的分叉来自
    *传入了与 P14 条目记录不一致的 authority*：那时重建指纹与条目指纹不等，
    ``fixed_corridor_currentness`` 会如实判 stale。这条测试锁定这个可核验事实。
    """

    from cns_planner.gis.corridor_band import build_fixed_corridor_band

    state = _state()
    route = state["operational_routes"][0]
    authority = FixedCorridorGeometryService._route_authority(route)
    assert authority is not None
    same = build_fixed_corridor_band(
        route, projected=None, entry=None,
        corridor_effective_status="passed", route_authority=authority,
    )
    fallback = build_fixed_corridor_band(
        route, projected=None, entry=None,
        corridor_effective_status="passed", route_authority=None,
    )
    # 回退路径解析出**同一份** P14 口径 authority ⇒ 几何身份必须逐字符一致，
    # 这正是"三维与二维不可能各自造一套 identity"的证据。
    assert (same["detail"]["geometry_fingerprint"]
            == fallback["detail"]["geometry_fingerprint"])

    diverged = build_fixed_corridor_band(
        route, projected=None, entry=None,
        corridor_effective_status="passed",
        route_authority={"source_type": "other_adoption", "candidate_id": "OTHER"},
    )
    assert (diverged["detail"]["geometry_fingerprint"]
            != same["detail"]["geometry_fingerprint"])
    assert diverged["detail"]["route_authority"]["candidate_id"] == "OTHER"
    assert same["detail"]["route_authority"]["candidate_id"] == "LRC2-R0005-ALT-100"


def test_band_currentness_rejects_a_diverged_geometry_identity():
    """几何身份与 P14 条目不一致时必须 stale（绝不显示为 current）。"""

    from cns_planner.gis.figure_currentness import fixed_corridor_currentness

    state = _state()
    route = state["operational_routes"][0]
    authority = FixedCorridorGeometryService._route_authority(route)
    from cns_planner.gis.corridor_band import build_fixed_corridor_band

    rebuilt = build_fixed_corridor_band(
        route, projected=None, entry=None,
        corridor_effective_status="passed", route_authority=authority,
    )
    rebuilt_fingerprint = rebuilt["detail"]["geometry_fingerprint"]
    currentness, reason, _identity = fixed_corridor_currentness(
        route_operational_status="passed",
        projected={"algorithm_id": "cns_service_corridor_v1", "routes": []},
        entry={
            "fixed_corridor_geometry": {
                "geometry_fingerprint": rebuilt_fingerprint,
            },
            "fixed_corridor_geometry_status": "available",
        },
        geometry_fingerprint=rebuilt_fingerprint,
        corridor_effective_status="passed",
    )
    assert (currentness, reason) == ("current", None)

    mismatched, mismatched_reason, _ = fixed_corridor_currentness(
        route_operational_status="passed",
        projected={"algorithm_id": "cns_service_corridor_v1", "routes": []},
        entry={
            "fixed_corridor_geometry": {"geometry_fingerprint": "corridorgeometryv1-OTHER"},
            "fixed_corridor_geometry_status": "available",
        },
        geometry_fingerprint=rebuilt_fingerprint,
        corridor_effective_status="passed",
    )
    assert mismatched == "stale"
    assert mismatched_reason == "fixed_corridor_geometry_fingerprint_mismatch"


def test_fixed_corridor_band_identity_enters_the_etag_seed():
    service = _service(_state())
    seed = service.identity_seed()
    assert seed["fixed_corridor_identity"]["width_policy_version"] == "fixed-1000m-v1"
    assert seed["fixed_corridor_identity"]["corridor_half_width_m"] == 500.0
    assert seed["fixed_corridor_identity"]["corridor_total_width_m"] == 1000.0


def test_fixed_corridor_bands_endpoint_is_no_store(monkeypatch):
    service = _service(_state())
    router, context = _router(service, monkeypatch)
    response = router._visualization_3d(
        context, "/api/visualization-3d/fixed-corridor-bands", {}, {})
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.data["etag"].startswith("vis3dfixedcorridorbandsv1-")
