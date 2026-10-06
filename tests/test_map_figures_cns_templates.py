"""CNS 专题制图模板（Presentation / Cartographic Export）定向测试。

覆盖 Round30-A 的五条硬契约：

1. **每张图只消费自己的服务**：通信图绝不画 RID/Radar，RID 图绝不画 Radar 扇区，
   Radar 图在 R0005 上绝不产生站址 / panel / 扇区；
2. **规划提案 ≠ 已确认设施**：``confirmed_cns_plan.status=not_confirmed`` 时所有
   ``selected_actions`` 一律是「规划提案（未确认）」，绝不上色成已建设施；
3. **五图统一**：五张 CNS 图共用同一个 extent、同一套图例符号定义与同一个版面档位；
4. **只读**：制图链路不调用 ``session.save()``、不推进业务 revision；
5. **`route_detail_v1` 仍是 planned**：调用一律被拒绝，绝不生成占位结果。

这些测试**不重跑**任何业务算法：所有 session 都是最小替身，所有 state 都是
显式构造的 canonical 结果形态。
"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest

from cns_planner.application.map_figure_service import (
    MapFigureError, MapFigureParameterError, MapFigureService,
    MapFigureTemplateUnavailable,
)
from cns_planner.gis.figure_style import CNS_LEGEND_GROUP_COLUMNS, FIGURE_STYLES, LEGEND_GROUP_OF
from cns_planner.reporting.map_templates import CNS_LAYOUT_PROFILE, catalog

#: R0005 的**真实**航路几何（11 个顶点，来自权威项目 canonical state）。
ROUTE_R0005 = {
    "route_id": "R0005", "status": "passed", "path_crs": "OGC:CRS84",
    "kind": "layered_risk_aware_operational_route",
    "path": [
        [122.2672222222, 29.8666666667], [122.26722222222222, 29.86611111111111],
        [122.26611111111112, 29.865000000000002], [122.265, 29.863888888888887],
        [122.195, 29.831666666666663], [122.19388888888889, 29.830555555555556],
        [122.19277777777778, 29.829444444444444], [122.185, 29.82277777777778],
        [122.1861111111111, 29.821666666666665], [122.18722222222223, 29.820555555555558],
        [122.1883333333, 29.8194444444],
    ],
    "start": [122.2672222222, 29.8666666667], "end": [122.1883333333, 29.8194444444],
    "start_node_id": "N005", "end_node_id": "N006",
    "provenance": {"source_type": "layered_operational_adoption"},
}

#: R0003 的 stale Radar 结果（**7 个 panel**）：R0005 的图绝不能使用它。
RADAR_R0003_STALE = {
    "route_id": "R0003", "altitude_layer_id": "ALT-080", "altitude_m": 80.0,
    "algorithm_version": "1.1", "status": "stale", "stale_reason": "layered_validation_evidence_outdated",
    "selected_panel_count": 7, "selected_tower_count": 7,
    "selected_panels": [{"panel_id": f"P{index}"} for index in range(7)],
    "selected_tower_ids": [f"tower-r0003-{index}" for index in range(7)],
    "radar_i_panel_count": 7, "radar_ii_panel_count": 0,
    "input_fingerprint": "radarlayout-r0003-stale",
}

#: R0005 的真实 Radar-I 结论：solver 已证明 infeasible，0 个 panel。
RADAR_R0005_INFEASIBLE = {
    "route_id": "R0005", "altitude_layer_id": "ALT-100", "altitude_m": 100.0,
    "algorithm_id": "radar_surveillance_layout", "algorithm_version": "1.3",
    "status": "infeasible", "gap_reason": "independent_site_count_limited",
    "gap_classification": "confirmed_gap", "managed_physical_gap": True,
    "selected_panel_count": 0, "selected_panels": [],
    "selected_tower_count": 0, "selected_tower_ids": [],
    "radar_i_panel_count": 0, "radar_ii_panel_count": 0,
    "candidate_tower_count": 2, "candidate_panel_count": 12,
    "candidate_statistics": {"per_tower": [
        {"tower_id": "tower-comm-001"}, {"tower_id": "tower-comm-002"},
    ]},
    "solver": {
        "status": "infeasible", "infeasibility_proven": True, "greedy_fallback_used": False,
    },
    "infeasibility_reasons": ["reason-1", "reason-2"],
    "input_fingerprint": "radarlayout-r0005-infeasible",
    "radar_gap": {
        "gap_reason": "independent_site_count_limited", "gap_classification": "confirmed_gap",
        "managed_physical_gap": True,
    },
}

#: P16 ``selected_actions``：3 × 通信 + 1 × RID + 2 × 导航完整性（真实形态）。
def _selected_actions():
    return [
        {
            "action_id": "add_navigation_integrity_monitor:R0005:destination:N006",
            "action": "add_navigation_integrity_monitor",
            "planner_family": "endpoint_integrity_monitor",
            "service_key": "N:navigation_integrity_monitoring", "subsystem": "N",
            "route_id": "R0005", "endpoint_role": "destination", "site_id": "N006",
            "takeoff_landing_site_id": "N006",
            "distinct_site_id": "takeoff_landing_site:N006",
            "coordinate": [122.1883333333, 29.8194444444],
            "site_binding": "takeoff_landing_site", "reuse_class": "existing_shared_site",
            "device_id": "NAVINT-R0005-destination-N006",
            "device_service_key": "N:navigation_integrity_monitoring",
            "planning_unit": "navigation_integrity_monitor_engineering_planning_unit",
            "equipment_selection_status": "not_selected", "proposal_only": True,
            "coverage_radius_m": None,
            "eligibility": {"status": "eligible", "reasons": []},
        },
        {
            "action_id": "add_navigation_integrity_monitor:R0005:origin:N005",
            "action": "add_navigation_integrity_monitor",
            "planner_family": "endpoint_integrity_monitor",
            "service_key": "N:navigation_integrity_monitoring", "subsystem": "N",
            "route_id": "R0005", "endpoint_role": "origin", "site_id": "N005",
            "takeoff_landing_site_id": "N005",
            "distinct_site_id": "takeoff_landing_site:N005",
            "coordinate": [122.2672222222, 29.8666666667],
            "site_binding": "takeoff_landing_site", "reuse_class": "existing_shared_site",
            "device_id": "NAVINT-R0005-origin-N005",
            "device_service_key": "N:navigation_integrity_monitoring",
            "equipment_selection_status": "not_selected", "proposal_only": True,
            "coverage_radius_m": None,
        },
        {
            "action_id": "tower_colocation_host:tower-colocation:tower-comm-001:RID-BASELINE-2-5KM",
            "action": "add_device_to_explicit_site", "action_type": "add_device_to_explicit_site",
            "service_key": "S:rid_cooperative", "subsystem": "S", "route_id": "R0005",
            "site_id": "tower-colocation:tower-comm-001",
            "distinct_site_id": "tower:tower-comm-001", "tower_id": "tower-comm-001",
            "coordinate": [122.228521, 29.831441], "longitude": 122.228521, "latitude": 29.831441,
            "device_id": "RID-BASELINE-2-5KM", "device_service_key": "S:rid_cooperative",
            "reuse_class": "tower_colocation_host", "coverage_radius_m": 5000.0,
            "confirmed": True, "source_confirmed_note": "宿主档案层面",
            "physical_mount_confirmed": False, "proposal_only": True,
            "surface_class": "mixed", "surface_classes": ["coastal_uncertain", "land", "sea"],
            "host": {"host_tower_name": "普陀桃花沙岙村H杆站"},
        },
        {
            "action_id": "tower_colocation_host:tower-colocation:tower-comm-001:COMM-BASELINE-4KM",
            "action": "add_device_to_explicit_site", "action_type": "add_device_to_explicit_site",
            "service_key": "C:communication", "subsystem": "C", "route_id": "R0005",
            "site_id": "tower-colocation:tower-comm-001",
            "distinct_site_id": "tower:tower-comm-001", "tower_id": "tower-comm-001",
            "coordinate": [122.228521, 29.831441], "longitude": 122.228521, "latitude": 29.831441,
            "device_id": "COMM-BASELINE-4KM", "device_service_key": "C:communication",
            "reuse_class": "tower_colocation_host", "coverage_radius_m": 4000.0,
            "confirmed": True, "physical_mount_confirmed": False, "proposal_only": True,
            "surface_class": "mixed", "surface_classes": ["coastal_uncertain", "land", "sea"],
            "host": {"host_tower_name": "普陀桃花沙岙村H杆站"},
        },
        {
            "action_id": "tower_colocation_host:tower-colocation:tower-comm-002:COMM-BASELINE-4KM",
            "action": "add_device_to_explicit_site", "action_type": "add_device_to_explicit_site",
            "service_key": "C:communication", "subsystem": "C", "route_id": "R0005",
            "site_id": "tower-colocation:tower-comm-002",
            "distinct_site_id": "tower:tower-comm-002", "tower_id": "tower-comm-002",
            "coordinate": [122.258139, 29.844355],
            "device_id": "COMM-BASELINE-4KM", "device_service_key": "C:communication",
            "reuse_class": "tower_colocation_host", "coverage_radius_m": 4000.0,
            "proposal_only": True, "surface_class": "land",
            "host": {"host_tower_name": "普陀桃花涂村基站"},
        },
        {
            "action_id": "tower_colocation_host:tower-colocation:tower-comm-003:COMM-BASELINE-4KM",
            "action": "add_device_to_explicit_site", "action_type": "add_device_to_explicit_site",
            "service_key": "C:communication", "subsystem": "C", "route_id": "R0005",
            "site_id": "tower-colocation:tower-comm-003",
            "distinct_site_id": "tower:tower-comm-003", "tower_id": "tower-comm-003",
            "coordinate": [122.187705, 29.822895],
            "device_id": "COMM-BASELINE-4KM", "device_service_key": "C:communication",
            "reuse_class": "tower_colocation_host", "coverage_radius_m": 4000.0,
            "proposal_only": True, "surface_class": "sea",
            "host": {"host_tower_name": "虾峙岛大岙站"},
        },
    ]


#: 合成 / 工程验证既有设施（真实项目里就是这一条）。
SYNTHETIC_EXISTING = {
    "facility_id": "EXISTING-SYN-001", "site_id": "EXISTING-SYN-001",
    "name": "SYNTHETIC Initial CNS Site",
    "coordinate": [122.2300, 29.8400],
    "metadata": {"dataset_class": "synthetic_engineering_validation", "real_world_facility": False},
    "devices": [{
        "device_id": "C-PRIMARY-SYN", "subsystem": "C", "status": "active",
        "type": {"technology": "dedicated_radio"},
        "coverage_geometry": {"radius_by_surface": {"land": 4000.0, "sea": 4000.0}},
    }],
}

#: 一条**真实**既有 CNS 设施（用于验证"实心 / 实线"身份确实会被画出来）。
REAL_EXISTING = {
    "facility_id": "REAL-CNS-001", "site_id": "REAL-CNS-001", "name": "真实既有通信站",
    "coordinate": [122.2650, 29.8650],
    "metadata": {"dataset_class": "operational", "real_world_facility": True},
    "devices": [{
        "device_id": "C-REAL-01", "subsystem": "C", "status": "active",
        "type": {"technology": "dedicated_radio"},
        "coverage_geometry": {"radius_by_surface": {"land": 4000.0, "sea": 4000.0}},
    }],
}


class _Session:
    """最小 session 替身；``save()`` 计数用于断言制图链路不写业务状态。"""

    def __init__(self, state, store_path):
        self.state = state
        self.store_path = store_path
        self.saved = 0

    def save(self):
        self.saved += 1


class _FakeRenderer:
    def __init__(self, payload=b"\x89PNG\r\n\x1a\n" + b"0" * 64):
        self.payload = payload
        self.calls = []

    def render(self, spec, *, dpi):
        self.calls.append((spec.template_id, spec.route_id, float(dpi), spec.fingerprint()))
        return self.payload


def _state(**overrides):
    state = {
        "revision": 410,
        "operational_routes": [json.loads(json.dumps(ROUTE_R0005))],
        "source_audits": {"items": {}},
        "nodes": [
            {"node_id": "N005", "name": "直升机场起降点",
             "coordinate": [122.2672222222, 29.8666666667]},
            {"node_id": "N006", "name": "桃花岛泰隆低空起降点",
             "coordinate": [122.1883333333, 29.8194444444]},
        ],
        "towers": {"items": [
            {"tower_id": "tower-comm-001", "name": "普陀桃花沙岙村H杆站",
             "coordinate": [122.228521, 29.831441], "height_m": 6.0, "site_type": "H杆塔"},
            {"tower_id": "tower-comm-002", "name": "普陀桃花涂村基站",
             "coordinate": [122.258139, 29.844355], "height_m": 9.0, "site_type": "楼面抱杆"},
            {"tower_id": "tower-comm-003", "name": "虾峙岛大岙站",
             "coordinate": [122.187705, 29.822895], "height_m": 15.0, "site_type": "落地塔"},
        ]},
        "confirmed_cns_plan": {"status": "not_confirmed", "plan_id": None},
        "cns_plan_review": {"confirmation_allowed": False, "status": "not_confirmed"},
        "cns_existing_baseline": {
            "knowledge_status": "not_declared", "planning_mode": "assume_empty_for_planning",
            "evidence_ref": "assumption:cns_existing_baseline=empty",
        },
        "existing_cns_facilities": {
            "status": "passed", "collection_id": "existing-cns-facilities", "count": 1,
            "items": [json.loads(json.dumps(SYNTHETIC_EXISTING))],
        },
        "cns_corridor_site_plan": {
            "status": "proposal_ready", "algorithm_id": "corridor_reuse_first_site_planner_v2",
            "algorithm_version": "2.2", "proposal_only": True,
            "requires_user_confirmation_and_apply": True,
            "input_fingerprint": "p16-input-fingerprint",
            "selected_actions": _selected_actions(),
            "candidate_actions": [{"action_id": "never-drawn-candidate"}],
        },
        "radar_surveillance_layout": {
            "status": "not_calculated", "count": 2,
            "items": [json.loads(json.dumps(RADAR_R0003_STALE)),
                      json.loads(json.dumps(RADAR_R0005_INFEASIBLE))],
        },
        "continuous_service_acceptability": {
            "algorithm_id": "continuous_service_acceptability_v1", "algorithm_version": "2.1",
            "status": "unacceptable", "primary_threat_status": "unacceptable",
            "supplementary_threat_status": "limitation", "plan_stage": "baseline",
            "input_fingerprint": "p17-input-fingerprint",
            "routes": [{"route_id": "R0005", "status": "unacceptable"}],
            "limitations": [{
                "limitation_id": "noncooperative_surveillance_limitation", "route_id": "R0005",
                "layer": "noncooperative", "status": "limitation",
                "no_relaxation_applied": "没有为覆盖率放宽 90° panel 或半径",
                "source": {"algorithm_version": "1.3", "source_status": "infeasible",
                           "gap_reason": "independent_site_count_limited",
                           "gap_classification": "confirmed_gap", "managed_physical_gap": True},
            }],
        },
    }
    state.update(overrides)
    return state


def _service(tmp_path, state=None, renderer=None):
    state = _state() if state is None else state
    session = _Session(state, tmp_path / "project_state.json")
    renderer = renderer or _FakeRenderer()
    service = MapFigureService(
        session, lambda: {}, lambda action: action(),
        renderer_factory=lambda: renderer, project_directory=tmp_path,
    )
    return service, session, renderer


def _layers(spec):
    return {layer.layer_key: layer for layer in spec.layers}


def _legend_keys(spec):
    return [item.layer_key for item in spec.legend_items]


def _circle_radii(spec):
    radii = {}
    for layer in spec.layers:
        if layer.layer_key.startswith("cns_coverage_"):
            radii[layer.layer_key] = (layer.data or {}).get("radius_m")
    return radii


# ---- 1. 模板目录 -------------------------------------------------------------

def test_catalog_declares_four_new_available_templates():
    payload = catalog()
    assert payload["available_template_ids"] == [
        "route_overview_v1", "communication_layout_v1", "navigation_layout_v1",
        "surveillance_layout_v1", "cns_combined_v1",
    ]
    statuses = {item["template_id"]: item["status"] for item in payload["templates"]}
    assert statuses["route_detail_v1"] == "planned"
    assert payload["surveillance_service_values"] == ["rid_cooperative", "radar_noncooperative"]
    assert payload["cns_layout_profile"] == CNS_LAYOUT_PROFILE


def test_route_detail_is_still_refused(tmp_path):
    service, _, _ = _service(tmp_path)
    with pytest.raises(MapFigureTemplateUnavailable):
        service.build_figure(template_id="route_detail_v1", route_id="R0005")


# ---- 2. 监视布设图的 variant 契约 ---------------------------------------------

def test_surveillance_requires_an_explicit_variant(tmp_path):
    service, _, _ = _service(tmp_path)
    with pytest.raises(MapFigureParameterError) as error:
        service.build_figure(template_id="surveillance_layout_v1", route_id="R0005")
    assert "surveillance_service" in str(error.value)
    assert error.value.detail["missing_parameters"] == ["surveillance_service"]


def test_illegal_surveillance_variant_is_rejected_not_defaulted(tmp_path):
    service, _, _ = _service(tmp_path)
    with pytest.raises(MapFigureParameterError) as error:
        service.build_figure(
            template_id="surveillance_layout_v1", route_id="R0005",
            parameter_overrides={"surveillance_service": "radar"},
        )
    assert "rid_cooperative" in str(error.value)
    assert error.value.detail["parameter"] == "surveillance_service"


def test_two_surveillance_variants_have_distinct_spec_fingerprints(tmp_path):
    service, _, _ = _service(tmp_path)
    rid = service.build_figure(
        template_id="surveillance_layout_v1", route_id="R0005",
        parameter_overrides={"surveillance_service": "rid_cooperative"},
    )
    radar = service.build_figure(
        template_id="surveillance_layout_v1", route_id="R0005",
        parameter_overrides={"surveillance_service": "radar_noncooperative"},
    )
    assert rid.fingerprint() != radar.fingerprint()
    assert rid.parameters["surveillance_service"] == "rid_cooperative"
    assert radar.parameters["surveillance_service"] == "radar_noncooperative"


# ---- 3. Communication -------------------------------------------------------

def test_communication_consumes_only_communication_actions(tmp_path):
    service, _, _ = _service(tmp_path)
    spec = service.build_figure(template_id="communication_layout_v1", route_id="R0005")
    layers = _layers(spec)
    assert set(layers["cns_comm_proposal"].data["points"][index]["service_key"]
               for index in range(layers["cns_comm_proposal"].feature_count)) == {"C:communication"}
    assert layers["cns_comm_proposal"].feature_count == 3
    # RID / 导航完整性 / Radar 图层**不得**出现在通信图里。
    assert "cns_rid_proposal" not in layers
    assert "cns_nav_proposal" not in layers
    assert "cns_radar_limitation" not in layers
    assert "cns_radar_context" not in layers
    for key in _legend_keys(spec):
        assert key not in ("cns_rid_proposal", "cns_nav_proposal", "cns_radar_limitation")


def test_communication_draws_the_4km_planning_radius(tmp_path):
    service, _, _ = _service(tmp_path)
    spec = service.build_figure(template_id="communication_layout_v1", route_id="R0005")
    assert _circle_radii(spec) == {"cns_coverage_comm": 4000.0}
    layer = _layers(spec)["cns_coverage_comm"]
    assert layer.feature_count == 3
    detail = layer.source_detail
    assert detail["is_measured_propagation_contour"] is False
    assert detail["radius_semantics"] == "communication_omnidirectional_planning_service_radius_4km"


def test_communication_reports_proposal_not_confirmed(tmp_path):
    service, _, _ = _service(tmp_path)
    spec = service.build_figure(template_id="communication_layout_v1", route_id="R0005")
    layer = _layers(spec)["cns_comm_proposal"]
    detail = layer.source_detail
    assert detail["confirmed_flags"] == [False]
    assert detail["proposal_not_confirmed"] is True
    assert detail["confirmation"]["confirmed_plan_status"] == "not_confirmed"
    # 上游 action 自带的 confirmed=true 只登记为宿主层面，绝不改变方案确认状态。
    assert detail["source_confirmed_semantics"].startswith("上游记录的 confirmed 只表示")
    assert any("规划提案" in warning for warning in spec.warnings)


def test_synthetic_existing_facility_is_not_drawn_by_default(tmp_path):
    service, _, _ = _service(tmp_path)
    spec = service.build_figure(template_id="communication_layout_v1", route_id="R0005")
    assert "cns_existing" not in _layers(spec)
    detail = spec.source_status["cns_comm_proposal"]["detail"]
    assert detail["confirmation"]["confirmed_plan_status"] == "not_confirmed"
    # 合成 / 工程验证设施被如实排除，并在 warnings 里给出中文原因（不静默丢弃）。
    assert any("合成" in warning for warning in spec.warnings)
    evidence = spec.layers[0].source_detail if spec.layers else {}
    assert evidence is not None


def test_real_existing_facility_is_drawn_and_opt_in_synthetic_is_disclosed(tmp_path):
    state = _state()
    state["existing_cns_facilities"] = {
        "status": "passed", "collection_id": "existing-cns-facilities", "count": 2,
        "items": [json.loads(json.dumps(SYNTHETIC_EXISTING)),
                  json.loads(json.dumps(REAL_EXISTING))],
    }
    service, _, _ = _service(tmp_path, state=state)
    spec = service.build_figure(template_id="communication_layout_v1", route_id="R0005")
    layer = _layers(spec)["cns_existing"]
    assert layer.feature_count == 1
    assert layer.style_key == "cns_existing"
    assert layer.data["points"][0]["identity"] == "existing"
    # 合成设施只有显式打开开关才会进入图面，并且永远带着披露字段。
    opt_in = service.build_figure(
        template_id="communication_layout_v1", route_id="R0005",
        parameter_overrides={"show_synthetic_existing_facilities": True},
    )
    assert _layers(opt_in)["cns_existing"].feature_count == 2


# ---- 4. RID -----------------------------------------------------------------

def test_rid_consumes_only_rid_and_fails_closed_to_2km_without_surface_facts(tmp_path):
    service, _, _ = _service(tmp_path)
    spec = service.build_figure(
        template_id="surveillance_layout_v1", route_id="R0005",
        parameter_overrides={"surveillance_service": "rid_cooperative"},
    )
    layers = _layers(spec)
    assert layers["cns_rid_proposal"].feature_count == 1
    assert layers["cns_rid_proposal"].data["points"][0]["service_key"] == "S:rid_cooperative"
    assert "cns_comm_proposal" not in layers
    assert "cns_nav_proposal" not in layers
    assert "cns_radar_context" not in layers
    assert "cns_radar_limitation" not in layers
    assert _circle_radii(spec) == {
        "cns_coverage_rid_land": 2000.0, "cns_coverage_rid_sea": 5000.0,
    }
    assert FIGURE_STYLES["cns_coverage_rid_land"]["outline_style"] == "solid"
    assert FIGURE_STYLES["cns_coverage_rid_sea"]["kind"] == "line"
    assert FIGURE_STYLES["cns_coverage_rid_sea"]["line_style"] == "dash"
    # RID ≠ Radar：RID 图上没有任何 90° 扇区 / panel / azimuth 表达。
    for layer in spec.layers:
        assert "panel" not in layer.layer_key
        assert "sector" not in layer.layer_key
        assert "azimuth" not in json.dumps(layer.source_detail, ensure_ascii=False).lower()
    assert "RID 陆地/沿海规划范围 2 km" in [item.display_name for item in spec.legend_items]
    assert "RID 海上延伸规划范围 2–5 km" not in [
        item.display_name for item in spec.legend_items
    ]
    assert layers["cns_coverage_rid_sea"].feature_count == 0
    assert spec.metadata["surface_aware_visualization"] is True
    assert spec.metadata["affects_planning"] is False
    assert spec.metadata["rid_extension_fail_closed"] is True


# ---- 5. Radar ---------------------------------------------------------------

def test_radar_route_result_is_zero_panels_and_never_uses_other_routes(tmp_path):
    service, _, _ = _service(tmp_path)
    spec = service.build_figure(
        template_id="surveillance_layout_v1", route_id="R0005",
        parameter_overrides={"surveillance_service": "radar_noncooperative"},
    )
    detail = _layers(spec)["cns_radar_limitation"].source_detail
    assert detail["selected_panel_count"] == 0
    assert detail["selected_tower_count"] == 0
    assert detail["algorithm_version"] == "1.3"
    assert detail["status"] == "infeasible"
    assert detail["gap_reason"] == "independent_site_count_limited"
    assert detail["gap_classification"] == "confirmed_gap"
    assert detail["managed_physical_gap"] is True
    assert detail["infeasibility_proven"] is True
    assert detail["manufactured_radar_sites"] == 0
    assert detail["manufactured_radar_panels"] == 0
    assert detail["drawn_radar_sectors"] == 0
    assert detail["other_route_items_used"] is False
    # R0003 的 stale 7 panel 绝不进入 R0005。
    evidence = detail["radar_algorithm_version"]
    assert evidence == "1.3"
    assert "R0003" not in json.dumps(detail, ensure_ascii=False)
    assert "7" != str(detail["selected_panel_count"])


def test_radar_draws_no_manufactured_site_or_sector(tmp_path):
    service, _, _ = _service(tmp_path)
    spec = service.build_figure(
        template_id="surveillance_layout_v1", route_id="R0005",
        parameter_overrides={"surveillance_service": "radar_noncooperative"},
    )
    layers = _layers(spec)
    # 唯一带几何的 Radar 图层是**评估过的候选塔上下文**，且它不是任何已选/已建设施。
    radar_layers = [key for key in layers if key.startswith("cns_radar_")]
    assert sorted(radar_layers) == ["cns_radar_context", "cns_radar_limitation"]
    context = layers["cns_radar_context"]
    assert context.feature_count == 2
    assert context.source_detail["selected_tower_count"] == 0
    assert context.source_detail["semantics"].startswith("radar_evaluated_candidate_tower_context")
    for point in context.data["points"]:
        assert point["radar_selected"] is False
    # 没有任何 Radar 站址 / panel 图层，也没有覆盖扇区多边形。
    assert not any("radar" in key and "coverage" in key for key in layers)
    assert layers["cns_radar_limitation"].geometry_type == "none"
    assert layers["cns_radar_limitation"].feature_count == 0
    # Round30-B1：Radar 能力限制从"地图内部大说明框"改为 FigureSpec.metadata 披露 +
    # 正式 legend item；内容**逐字保留**，地图主体里不再有大白框。
    assert spec.annotations == []
    titles = [item["title"] for item in spec.metadata["disclosures"]]
    assert "Radar-I 能力限制" in titles
    body = json.dumps(spec.metadata["disclosures"], ensure_ascii=False)
    assert "未形成可行布设" in body
    assert "3 km" in body and "90°" in body
    #: Round31-C：能力限制回退时不再声称"Radar-II 未启用"（分级规划已正式启用），
    #: 而是明确"未放宽 range + 未新建站址"；若既有站址的 Radar-II 升级同样不可行，
    #: 标题与文案会切换为「Radar-I + Radar-II 能力限制」。
    assert "未放宽 range" in body and "未新建站址" in body
    assert "不表示任何 Radar 站址" in body
    # 地图框**之外**仍保留一句短披露（能力限制不许因为删除说明框而消失）。
    assert "无可行布设" in spec.metadata["map_disclosure"]
    assert "无可行布设" in spec.layout["map_disclosure"]
    # 正式 legend item 仍在。
    assert any(
        item.layer_key == "cns_radar_limitation" for item in spec.legend_items
    )


def test_feasible_radar_draws_selected_proposal_sites_and_90_degree_sectors(tmp_path):
    state = _state()
    feasible = json.loads(json.dumps(RADAR_R0005_INFEASIBLE))
    feasible.update({
        "status": "proposal_ready", "gap_reason": None, "gap_classification": "none",
        "managed_physical_gap": False, "selected_panel_count": 2,
        "selected_tower_count": 2,
        "selected_tower_ids": ["tower-comm-001", "tower-comm-002"],
        "radar_i_panel_count": 2,
        "selected_panels": [
            {
                "panel_id": "P1", "tower_id": "tower-comm-001", "radar_type": "radar_i",
                "tower_name": "塔1", "longitude": 122.228521, "latitude": 29.831441,
                "azimuth_deg": 45.0, "panel_half_width_deg": 45.0,
                "horizontal_inner_radius_m": 100.0, "horizontal_outer_radius_m": 2990.0,
            },
            {
                "panel_id": "P2", "tower_id": "tower-comm-002", "radar_type": "radar_i",
                "tower_name": "塔2", "longitude": 122.258139, "latitude": 29.844355,
                "azimuth_deg": 225.0, "panel_half_width_deg": 45.0,
                "horizontal_inner_radius_m": 100.0, "horizontal_outer_radius_m": 2990.0,
            },
        ],
        "solver": {"status": "optimal", "infeasibility_proven": False},
    })
    state["radar_surveillance_layout"]["items"][1] = feasible
    service, _, _ = _service(tmp_path, state=state)
    spec = service.build_figure(
        template_id="surveillance_layout_v1", route_id="R0005",
        parameter_overrides={"surveillance_service": "radar_noncooperative"},
    )
    layers = _layers(spec)

    assert layers["cns_radar_proposal"].feature_count == 2
    assert layers["cns_radar_sector"].feature_count == 2
    assert all(len(ring) >= 4 for ring in layers["cns_radar_sector"].data["polygons"])
    assert "cns_radar_limitation" not in layers
    assert "规划提案（未确认）" in spec.metadata["map_disclosure"]
    assert "90° panel" in json.dumps(spec.metadata["disclosures"], ensure_ascii=False)


# ---- 6. Navigation Integrity ------------------------------------------------

def test_navigation_monitors_land_on_the_real_endpoint_sites(tmp_path):
    service, _, _ = _service(tmp_path)
    spec = service.build_figure(template_id="navigation_layout_v1", route_id="R0005")
    layer = _layers(spec)["cns_nav_proposal"]
    points = {(round(point["longitude"], 10), round(point["latitude"], 10))
              for point in layer.data["points"]}
    assert points == {
        (122.2672222222, 29.8666666667),   # N005 origin（真实起降点坐标）
        (122.1883333333, 29.8194444444),   # N006 destination
    }
    assert layer.feature_count == 2
    detail = layer.source_detail
    assert sorted(detail["site_ids"]) == ["N005", "N006"]
    # endpoint 服务没有 tower_id：监测点画在真实起降点上，不是铁塔。
    assert detail["tower_ids"] == []
    assert detail["confirmed_flags"] == [False]
    assert "cns_comm_proposal" not in _layers(spec)
    assert "cns_rid_proposal" not in _layers(spec)
    # 不出现 RTK / GBAS / corridor coverage station 这类错误命名（披露文本仍逐字保留，
    # 只是从地图内部说明框移到了 FigureSpec.metadata）。
    body = json.dumps(
        list(spec.metadata["disclosures"]) + [layer.source_detail],
        ensure_ascii=False,
    )
    assert "RTK" in body and "不是 RTK station" in body
    assert "不是 GBAS" in body


def test_navigation_discloses_the_delivery_deficit_and_omits_the_10km_radius(tmp_path):
    service, _, _ = _service(tmp_path)
    spec = service.build_figure(template_id="navigation_layout_v1", route_id="R0005")
    # 披露内容仍在，只是从地图内部说明框改为 FigureSpec.metadata（地图主体只留给地图）。
    assert spec.annotations == []
    body = json.dumps(spec.metadata["disclosures"], ensure_ascii=False)
    assert "confirmed_deficit" in body
    assert "integrity_monitor_delivery_deficit" in body
    assert "Communication delivery 仍 residual deficit" in body
    # 默认**不**画 10 km 本地监测半径（避免被误读成认证覆盖范围）。
    assert "cns_coverage_nav" not in _layers(spec)
    assert not any(layer.layer_key.startswith("cns_coverage_") for layer in spec.layers)


# ---- 7. CNS Combined --------------------------------------------------------

def test_combined_overlays_only_real_existing_and_selected_actions(tmp_path):
    state = _state()
    state["existing_cns_facilities"] = {
        "status": "passed", "collection_id": "existing-cns-facilities", "count": 1,
        "items": [json.loads(json.dumps(REAL_EXISTING))],
    }
    service, _, _ = _service(tmp_path, state=state)
    spec = service.build_figure(template_id="cns_combined_v1", route_id="R0005")
    layers = _layers(spec)
    assert layers["cns_existing"].feature_count == 1
    assert layers["cns_comm_proposal"].feature_count == 3
    assert layers["cns_rid_proposal"].feature_count == 1
    assert layers["cns_nav_proposal"].feature_count == 2
    # Radar：没有任何新建设施，只有声明型限制项。
    assert "cns_radar_context" not in layers
    assert layers["cns_radar_limitation"].feature_count == 0
    assert layers["cns_radar_limitation"].source_detail["manufactured_radar_sites"] == 0
    # 未选中的 candidate_actions 绝不进入任何图层。
    body = json.dumps([layer.to_dict() for layer in spec.layers], ensure_ascii=False)
    assert "never-drawn-candidate" not in body


def test_colocated_services_share_identical_coordinates(tmp_path):
    service, _, _ = _service(tmp_path)
    spec = service.build_figure(template_id="cns_combined_v1", route_id="R0005")
    layers = _layers(spec)
    comm = [point for point in layers["cns_comm_proposal"].data["points"]
            if point["site_id"] == "tower-colocation:tower-comm-001"]
    rid = [point for point in layers["cns_rid_proposal"].data["points"]
           if point["site_id"] == "tower-colocation:tower-comm-001"]
    assert len(comm) == 1 and len(rid) == 1
    assert (comm[0]["longitude"], comm[0]["latitude"]) == (
        rid[0]["longitude"], rid[0]["latitude"],
    ) == (122.228521, 29.831441)


# ---- 8. 五图统一 ------------------------------------------------------------

def _five_specs(tmp_path, state=None):
    service, session, renderer = _service(tmp_path, state=state)
    targets = [
        ("communication_layout_v1", None),
        ("surveillance_layout_v1", {"surveillance_service": "rid_cooperative"}),
        ("surveillance_layout_v1", {"surveillance_service": "radar_noncooperative"}),
        ("navigation_layout_v1", None),
        ("cns_combined_v1", None),
    ]
    specs = [
        service.build_figure(template_id=template_id, route_id="R0005",
                             parameter_overrides=overrides)
        for template_id, overrides in targets
    ]
    return service, session, renderer, specs


def test_five_cns_figures_share_one_extent_and_one_map_frame(tmp_path):
    _, _, _, specs = _five_specs(tmp_path)
    extents = {tuple(round(value, 9) for value in spec.extent.as_list()) for spec in specs}
    assert len(extents) == 1
    frames = {
        (round(spec.layout["map_width_mm"], 6), round(spec.layout["map_height_mm"], 6))
        for spec in specs
    }
    assert len(frames) == 1
    for spec in specs:
        assert spec.layout["layout_profile"] == CNS_LAYOUT_PROFILE
        assert spec.extent_evidence["layout_profile"] == CNS_LAYOUT_PROFILE
        assert spec.extent_evidence["cns_five_figure_shared_extent"] is True
        assert spec.extent_evidence["extent_uniform_across_cns_templates"] is True
        assert spec.extent_evidence["coverage_circles_inside_extent"] in (True, None)


def test_legend_symbols_are_the_same_style_keys_as_the_map(tmp_path):
    _, _, _, specs = _five_specs(tmp_path)
    for spec in specs:
        map_styles = {layer.style_key for layer in spec.layers}
        for item in spec.legend_items:
            assert item.style_key in FIGURE_STYLES, item.style_key
            assert item.style_key in map_styles, (spec.template_id, item.style_key)
            assert LEGEND_GROUP_OF.get(item.layer_key) == item.legend_group
            assert item.legend_group in CNS_LEGEND_GROUP_COLUMNS


def test_audit_strip_and_scale_bar_stay_inside_the_page(tmp_path):
    """比例尺 / 北箭头 / 图例都在页面内；**正式图默认不显示工程审计条**。

    Round30-B1.1：审计条（地图 CRS / 经纬网 CRS / revision / 未显示图层）改为
    ``audit_footer`` 开关控制，正式图默认 false；审计信息仍完整保存在
    FigureSpec.metadata 与导出报告里。
    """

    _, _, _, specs = _five_specs(tmp_path)
    for spec in specs:
        plan = spec.layout
        assert plan["audit_footer"] is False
        assert plan["footer_strip_top_mm"] is None
        # 审计信息仍在 metadata（正式图隐藏、审计不丢）。
        assert spec.metadata["map_crs"] == plan["map_crs"]
        assert spec.metadata["grid_crs"] == plan["grid_crs"]
        # 比例尺与北箭头都按地图框内距定位，因此永远在图框内。
        assert plan["scale_bar_margin_mm"] > 0
        assert plan["north_arrow_margin_mm"] > 0
        assert plan["north_arrow_size_mm"] < plan["map_width_mm"] / 4
        assert plan["north_arrow_size_mm"] < plan["map_height_mm"] / 4
        # 图例框贴合内容且完全落在页脚之上。
        assert plan["legend_top_mm"] + plan["legend_height_mm"] <= (
            plan["document_height_mm"] - plan["footer_band_mm"]
        )
        # review 模式（审计条打开）时，审计条仍严格位于地图框与图例框之间。
        review_plan = _layout_plan_with_audit(spec)
        assert review_plan["footer_strip_top_mm"] >= (
            review_plan["map_top_mm"] + review_plan["map_height_mm"]
        )
        assert review_plan["legend_top_mm"] > review_plan["footer_strip_top_mm"]


def _layout_plan_with_audit(spec):
    """用同一套参数 + ``audit_footer=True`` 重新规划一次版面（review 模式）。"""

    from cns_planner.application.map_figure_service import (
        _layout_plan, _legend_layout_entries,
    )

    parameters = dict(spec.parameters or {})
    parameters["audit_footer"] = True
    parameters["map_disclosure"] = spec.layout.get("map_disclosure") or ""
    return _layout_plan(parameters, _legend_layout_entries(spec.legend_items))


def test_grid_annotation_is_outside_the_map_frame_and_off_the_audit_band():
    """经度标注放在地图框**外侧**下沿（Round30-B1.2），纬度仍在外侧左沿。

    框外经度标注不再占用任何地图内容空间；为避免它与地图框下方的披露 / 审计薄带逐字
    重叠，``footer_map_gap_mm`` 同时被放宽（见 ``LAYOUT``），本用例一并断言这个间距
    足够容纳一行 8.5 pt 的经度文字。
    """

    from cns_planner.gis.figure_style import LAYOUT

    source = (
        Path(__file__).resolve().parents[1]
        / "cns_planner" / "gis" / "qgis_figure_renderer.py"
    ).read_text(encoding="utf-8")
    assert "OutsideMapFrame, QgsLayoutItemMapGrid.Bottom" in source
    assert "OutsideMapFrame, QgsLayoutItemMapGrid.Left" in source
    # 绝不回到"经度画在图框内侧"的旧版式。
    assert "InsideMapFrame, QgsLayoutItemMapGrid.Bottom" not in source
    # 框外经度带 + 文字高度必须完全落在"地图框 → 披露条"的间距之内。
    label_band_mm = float(LAYOUT["grid_font_size"]) / 72.0 * 25.4 + 0.9
    assert float(LAYOUT["footer_map_gap_mm"]) >= label_band_mm


def test_five_figures_never_call_session_save_and_keep_the_revision(tmp_path):
    service, session, renderer, specs = _five_specs(tmp_path)
    for spec in specs:
        service._render(spec, dpi=300.0)  # noqa: SLF001 - 断言渲染路径同样只读
    assert session.saved == 0
    assert session.state["revision"] == 410
    assert [call[0] for call in renderer.calls][-5:] == [
        "communication_layout_v1", "surveillance_layout_v1", "surveillance_layout_v1",
        "navigation_layout_v1", "cns_combined_v1",
    ]


def test_rid_and_combined_build_leave_p14_to_p17_revision_and_state_sha_unchanged(tmp_path):
    state = _state(
        cns_corridor_assessment={"status": "passed", "fingerprint": "p14"},
        cns_corridor_gap_assessment={"status": "failed", "fingerprint": "p15"},
    )
    service, session, _ = _service(tmp_path, state=state)
    keys = (
        "cns_corridor_assessment", "cns_corridor_gap_assessment",
        "cns_corridor_site_plan", "continuous_service_acceptability",
    )
    before_values = {key: json.loads(json.dumps(state[key])) for key in keys}
    before_payload = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    before_sha256 = sha256(before_payload.encode("utf-8")).hexdigest()

    service.build_figure(
        template_id="surveillance_layout_v1", route_id="R0005",
        parameter_overrides={"surveillance_service": "rid_cooperative"},
    )
    service.build_figure(template_id="cns_combined_v1", route_id="R0005")

    after_payload = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert {key: state[key] for key in keys} == before_values
    assert state["revision"] == 410
    assert sha256(after_payload.encode("utf-8")).hexdigest() == before_sha256
    assert session.saved == 0


def test_five_figures_land_in_one_route_first_directory(tmp_path):
    service, session, _, specs = _five_specs(tmp_path)
    figure_ids = []
    for spec in specs:
        service._render(spec, dpi=300.0)  # noqa: SLF001
        exported = service.export(
            template_id=spec.template_id, route_id="R0005",
            parameter_overrides={
                "surveillance_service": spec.parameters["surveillance_service"],
            } if spec.template_id == "surveillance_layout_v1" else None,
        )
        figure_ids.append(exported["figure_id"])
    assert len(set(figure_ids)) == 5
    # route-first 目录名由服务端派生：``<cleaned route_id>-<sha256 前 8 位>``。
    from cns_planner.application.map_figure_service import MapFigureStore

    route_directory = (
        tmp_path / "artifacts" / "map_figures" / "routes"
        / MapFigureStore.sanitize_route_id("R0005")
    )
    assert route_directory.is_dir()
    index = json.loads((route_directory / "index.json").read_text(encoding="utf-8"))
    assert set(index["templates"]) == {
        "communication_layout_v1", "surveillance_layout_v1", "navigation_layout_v1",
        "cns_combined_v1",
    }
    assert session.saved == 0
    assert session.state["revision"] == 410
    # metadata.json / 总索引 / 规格 JSON 的 spec_fingerprint 必须逐字节一致。
    top_index = json.loads(
        (tmp_path / "artifacts" / "map_figures" / "index.json").read_text(encoding="utf-8")
    )
    by_id = {item["figure_id"]: item for item in top_index["items"]}
    for figure_id in figure_ids:
        record = by_id[figure_id]
        spec_path = tmp_path / record["spec_relative_path"]
        metadata_path = tmp_path / record["record_relative_path"]
        payload = json.loads(spec_path.read_text(encoding="utf-8"))
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        from cns_planner.gis.figure_spec import FigureSpec

        assert FigureSpec.from_dict(payload).fingerprint() == record["spec_fingerprint"]
        assert metadata["spec_fingerprint"] == record["spec_fingerprint"]


def test_cns_figures_record_the_full_audit_trail(tmp_path):
    _, _, _, specs = _five_specs(tmp_path)
    for spec in specs:
        assert spec.route_id == "R0005"
        assert spec.template_id != "route_overview_v1"
        boundaries = spec.boundaries
        assert boundaries["read_only"] is True
        assert boundaries["writes_state"] is False
        assert boundaries["recomputes_business_results"] is False
        assert boundaries["uses_unselected_candidates"] is False
        assert boundaries["manufactures_radar_sites"] is False
        assert boundaries["proposal_never_drawn_as_confirmed"] is True
        layer_detail = next(
            layer.source_detail for layer in spec.layers
            if layer.layer_key in (
                "cns_comm_proposal", "cns_rid_proposal", "cns_nav_proposal",
                "cns_radar_limitation",
            )
        )
        assert layer_detail["p16_input_fingerprint"] == "p16-input-fingerprint"
        assert layer_detail["p17_input_fingerprint"] == "p17-input-fingerprint"
        assert layer_detail["p17_algorithm_version"] == "2.1"
        assert layer_detail["radar_input_fingerprint"] == "radarlayout-r0005-infeasible"
        assert layer_detail["radar_algorithm_version"] == "1.3"


def test_navigation_and_communication_annotations_disclose_the_step6_gate(tmp_path):
    """五图都必须披露 Step6 门禁；Round30-B1 起披露位置是 metadata（不是地图内部说明框）。"""

    _, _, _, specs = _five_specs(tmp_path)
    for spec in specs:
        # 地图内部**不再**有任何大说明框，披露内容全部进入 metadata。
        assert spec.annotations == [], spec.template_id
        assert spec.metadata["disclosures"], spec.template_id
        assert spec.metadata["map_internal_annotations"] is False
        body = json.dumps(spec.metadata["disclosures"], ensure_ascii=False)
        assert "未通过 Step6 正式确认" in body
        assert "P16 proposal_ready" in body


def test_unknown_template_is_refused(tmp_path):
    service, _, _ = _service(tmp_path)
    with pytest.raises(MapFigureError):
        service.build_figure(template_id="no_such_template", route_id="R0005")
