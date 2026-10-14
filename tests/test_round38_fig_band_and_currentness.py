"""Round38-FIG 定向测试：六类专题图与固定 1000 m 保障宽带的统一适配。

覆盖本轮四条硬契约（全部为最小确定性夹具，不重跑任何业务算法、不启动 QGIS）：

1. **六图共享同一条固定 1000 m 保障目标带**：图层键 / 数据源角色 / 样式键 / 图例定义
   完全一致，且几何只由 ``build_fixed_corridor_geometry`` 构造；
2. **currentness 身份门禁**：只有 P14 条目指纹与本次重建指纹一致、且来源航路
   ``status == passed`` 时才是 ``current``；stale 时该层**不绘制**并给出原因，
   绝不改写成 current，也绝不画近似带；
3. **正式成果图门禁**：600 DPI 导出在非 current 时被明确拒绝（带逐图层原因），
   预览与常规导出仍可用；
4. **导航监测身份**：N1/N2 只读仿真结果按 ``route_geometry_fingerprint`` 绑定，
   指纹不一致即 stale；强制端点设备与 P16 规划提案是不同图层、不同来源。
"""

from __future__ import annotations

import json

import pytest

from cns_planner.application.map_figure_service import (
    MapFigureCurrentnessGate, MapFigureService, FORMAL_DELIVERABLE_MIN_DPI,
)
from cns_planner.gis.corridor_band import FIXED_CORRIDOR_LAYER_KEY
from cns_planner.gis.corridor_geometry import build_fixed_corridor_geometry
from cns_planner.gis.figure_currentness import (
    production_gate, route_geometry_fingerprint,
)
from cns_planner.gis.figure_style import FIGURE_STYLES, LEGEND_GROUP_OF, legend_order_key
from cns_planner.reporting.map_templates import (
    CNS_FIGURE_TEMPLATE_VERSION, COMMERCIAL_TEMPLATES, FIXED_CORRIDOR_LAYER_KEY as
    TEMPLATE_BAND_KEY, catalog,
)

ROUTE_ID = "R0005"
ROUTE_PATH = [
    [122.2672222222, 29.8666666667], [122.26722222222222, 29.86611111111111],
    [122.26611111111112, 29.865000000000002], [122.265, 29.863888888888887],
    [122.195, 29.831666666666663], [122.19388888888889, 29.830555555555556],
    [122.19277777777778, 29.829444444444444], [122.185, 29.82277777777778],
    [122.1861111111111, 29.821666666666665], [122.18722222222223, 29.820555555555558],
    [122.1883333333, 29.8194444444],
]

ALL_SIX_TEMPLATES = (
    "route_overview_v1", "communication_layout_v1", "navigation_layout_v1",
    "surveillance_layout_v1", "cns_combined_v1",
)


#: 航路权威身份（与 state 里的 provenance 同源；身份口径必须与接口实现一致）。
ROUTE_AUTHORITY = {
    "source_type": "layered_operational_adoption",
    "adoption_fingerprint": "adoption-fingerprint",
    "candidate_id": None, "candidate_fingerprint": None,
    "validation_id": None, "validation_fingerprint": None,
}


def _built():
    """真实构造一次固定宽带几何（唯一权威构造器，不用任何近似几何做夹具）。

    ``route_authority`` 与 canonical 航路的 provenance 必须给同一口径：它是几何身份的
    输入之一，两处不同会让"重建指纹 != P14 条目指纹"，把一条 current 的带误判为 stale。
    """

    return build_fixed_corridor_geometry(
        ROUTE_PATH, route_id=ROUTE_ID, route_authority=ROUTE_AUTHORITY,
    )


class _Session:
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
        self.calls.append((spec.template_id, spec.route_id, float(dpi)))
        return self.payload


class _FakeFixedCorridorService:
    """固定宽带身份服务的最小替身（只提供只读投影入口）。"""

    def __init__(self, projected=None, entry=None):
        self.projected = projected
        self.entry = entry

    def _corridor_route_entry(self, route_id):
        return self.projected, self.entry


def _state(**overrides):
    state = {
        "revision": 471,
        "operational_routes": [{
            "route_id": ROUTE_ID, "status": "stale", "path_crs": "OGC:CRS84",
            "path": [list(point) for point in ROUTE_PATH],
            "start": list(ROUTE_PATH[0]), "end": list(ROUTE_PATH[-1]),
            "start_node_id": "N005", "end_node_id": "N006",
            "provenance": {"source_type": "layered_operational_adoption",
                           "adoption_fingerprint": "adoption-fingerprint"},
        }],
        "result_statuses": {"cns_corridor_assessment": "stale"},
        "source_audits": {"items": {}},
        "nodes": [
            {"node_id": "N005", "name": "直升机场起降点",
             "coordinate": list(ROUTE_PATH[0])},
            {"node_id": "N006", "name": "桃花岛泰隆低空起降点",
             "coordinate": list(ROUTE_PATH[-1])},
        ],
        # 走廊评估结果的**当前**算法语义身份：语义不匹配会被判 stale（这是既有门禁，
        # 不是本轮的宽松点），因此夹具必须给出真实身份。
        "cns_corridor_assessment": {
            "algorithm_id": "cns_service_corridor_v1", "algorithm_version": "1.4",
            "status": "passed", "routes": [],
        },
    }
    state.update(overrides)
    return state


def _service(tmp_path, *, state=None, fixed_corridor_service=None,
             navigation_monitoring_provider=None, shared_site_candidates_provider=None,
             renderer=None):
    session = _Session(_state() if state is None else state,
                       tmp_path / "project_state.json")
    renderer = renderer or _FakeRenderer()
    service = MapFigureService(
        session, lambda: {}, lambda action: action(),
        renderer_factory=lambda: renderer, project_directory=tmp_path,
        fixed_corridor_service=fixed_corridor_service,
        navigation_monitoring_provider=navigation_monitoring_provider,
        shared_site_candidates_provider=shared_site_candidates_provider,
    )
    return service, session, renderer


def _layers(spec):
    return {layer.layer_key: layer for layer in spec.layers}


# --------------------------------------------------------------------------- 模板与图例


def test_six_templates_declare_the_shared_band_and_bump_their_version():
    payload = catalog()
    assert payload["fixed_corridor_layer"]["layer_key"] == "cns_fixed_corridor"
    assert payload["fixed_corridor_layer"]["corridor_half_width_m"] == 500.0
    assert payload["fixed_corridor_layer"]["corridor_total_width_m"] == 1000.0
    assert payload["fixed_corridor_layer"]["not_a_device_coverage_area"] is True
    assert payload["cns_figure_template_version"] == 2
    # 六类专题图（overview + 四张 CNS + 监视图的两个 variant）都声明了同一条宽带。
    for template_id in ALL_SIX_TEMPLATES:
        item = COMMERCIAL_TEMPLATES[template_id]
        assert TEMPLATE_BAND_KEY in item["layer_keys"], template_id
        assert TEMPLATE_BAND_KEY in item["legend_order"], template_id
        # 含固定宽带语义的模板必须升版：旧图件因此在 metadata 中可区分。
        assert item["template_version"] == CNS_FIGURE_TEMPLATE_VERSION, template_id
    # 未实现的模板不动。
    assert COMMERCIAL_TEMPLATES["route_detail_v1"]["template_version"] == 1


def test_band_style_is_light_translucent_and_under_the_route():
    item = FIGURE_STYLES[FIXED_CORRIDOR_LAYER_KEY]
    assert item["kind"] == "fill"
    assert item["opacity"] <= 0.35
    assert item["outline_style"] == "dash"
    # 必须压在障碍物之上、航路线与站址之下：否则会遮挡航路线 / 站址 / 覆盖结果。
    assert FIGURE_STYLES["land"]["z"] < item["z"] < FIGURE_STYLES["planned_route"]["z"]
    assert item["z"] < FIGURE_STYLES["tower_existing"]["z"]
    # 独立图例分组：混进 coverage 会被读成"设备已实现的覆盖区域"。
    assert LEGEND_GROUP_OF[FIXED_CORRIDOR_LAYER_KEY] == "service_band"


# --------------------------------------------------------------------------- 宽带绘制与门禁


@pytest.mark.parametrize("template_id,parameters", [
    ("route_overview_v1", None),
    ("communication_layout_v1", None),
    ("navigation_layout_v1", None),
    ("surveillance_layout_v1", {"surveillance_service": "rid_cooperative"}),
    ("surveillance_layout_v1", {"surveillance_service": "radar_noncooperative"}),
    ("cns_combined_v1", None),
])
def test_band_is_current_and_shared_by_every_template(tmp_path, template_id, parameters):
    """P14 指纹一致 + 航路 passed 时，六类图都画出同一条 current 保障目标带。"""

    built = _built()
    assert built["status"] == "available", built.get("reason")
    entry = {
        "route_id": ROUTE_ID,
        "fixed_corridor_geometry_status": "available",
        "fixed_corridor_geometry": {
            "geometry_fingerprint": built["geometry_fingerprint"],
        },
    }
    state = _state(result_statuses={"cns_corridor_assessment": "passed"})
    state["operational_routes"][0]["status"] = "passed"
    service, session, _ = _service(
        tmp_path, state=state,
        fixed_corridor_service=_FakeFixedCorridorService(
            projected={"algorithm_id": "cns_service_corridor_v1",
                       "algorithm_version": "1.0", "routes": [entry]},
            entry=entry,
        ),
    )
    spec = service.build_figure(
        template_id=template_id, route_id=ROUTE_ID,
        parameter_overrides=parameters,
    )
    layer = _layers(spec)[FIXED_CORRIDOR_LAYER_KEY]
    assert layer.style_key == FIXED_CORRIDOR_LAYER_KEY
    assert layer.geometry_type == "polygon"
    assert layer.feature_count == 1
    polygons = layer.data["polygons"]
    assert polygons and len(polygons[0]) >= 4
    detail = layer.source_detail
    assert detail["corridor_half_width_m"] == 500.0
    assert detail["corridor_total_width_m"] == 1000.0
    assert detail["geometry_fingerprint"] == built["geometry_fingerprint"]
    assert detail["not_device_coverage_area"] is True
    assert detail["not_certified_airspace"] is True
    assert detail["currentness"]["currentness"] == "current"
    # 图例与地图共用同一份样式定义。
    assert FIXED_CORRIDOR_LAYER_KEY in [item.layer_key for item in spec.legend_items]
    assert spec.metadata["production_gate"]["fixed_corridor_band"]["drawn"] is True
    # 只读：不写业务状态。
    assert session.saved == 0


def test_band_currentness_requires_route_passed_and_matching_fingerprint(tmp_path):
    """来源航路 stale → 宽带几何不得作为 current 保障边界（也不会被改写成 current）。"""

    built = _built()
    entry = {
        "route_id": ROUTE_ID,
        "fixed_corridor_geometry_status": "available",
        "fixed_corridor_geometry": {
            "geometry_fingerprint": built["geometry_fingerprint"],
        },
    }
    state = _state(result_statuses={"cns_corridor_assessment": "passed"})
    state["operational_routes"][0]["status"] = "stale"
    service, _, _ = _service(
        tmp_path, state=state,
        fixed_corridor_service=_FakeFixedCorridorService(
            projected={"algorithm_id": "x", "algorithm_version": "1.0", "routes": [entry]},
            entry=entry,
        ),
    )
    spec = service.build_figure(template_id="cns_combined_v1", route_id=ROUTE_ID)
    layer = _layers(spec)[FIXED_CORRIDOR_LAYER_KEY]
    assert layer.source_status == "unavailable"
    assert layer.feature_count == 0
    assert layer.data == {}
    assert layer.source_detail["currentness"]["currentness"] == "stale"
    assert (
        layer.source_detail["currentness"]["identity"]["route_operational_status"] == "stale"
    )
    # 原因可理解，而不是静默空图。
    assert "不予" in layer.source_reason or "未按 current" in layer.source_reason \
        or "不是 current" in layer.source_reason
    gate = spec.metadata["production_gate"]
    assert gate["is_current_figure"] is False
    assert gate["fixed_corridor_band"]["drawn"] is False
    assert FIXED_CORRIDOR_LAYER_KEY not in [item.layer_key for item in spec.legend_items]


def test_band_fingerprint_mismatch_is_stale_not_drawn(tmp_path):
    """P14 条目留着旧指纹（几何已变）→ stale，绝不画出来。"""

    entry = {
        "route_id": ROUTE_ID,
        "fixed_corridor_geometry_status": "available",
        "fixed_corridor_geometry": {"geometry_fingerprint": "corridorgeometryv1-deadbeef"},
    }
    state = _state(result_statuses={"cns_corridor_assessment": "passed"})
    state["operational_routes"][0]["status"] = "passed"
    service, _, _ = _service(
        tmp_path, state=state,
        fixed_corridor_service=_FakeFixedCorridorService(
            projected={"algorithm_id": "x", "algorithm_version": "1.0", "routes": [entry]},
            entry=entry,
        ),
    )
    spec = service.build_figure(template_id="cns_combined_v1", route_id=ROUTE_ID)
    detail = _layers(spec)[FIXED_CORRIDOR_LAYER_KEY].source_detail
    assert detail["currentness"]["currentness"] == "stale"
    assert detail["currentness"]["reason"] == "fixed_corridor_geometry_fingerprint_mismatch"


def test_missing_p14_evidence_is_reported_not_silently_dropped(tmp_path):
    """没有任何 P14 证据时：该层如实登记不可用 + 原因，绝不静默输出空图。"""

    service, _, _ = _service(tmp_path)
    spec = service.build_figure(template_id="cns_combined_v1", route_id=ROUTE_ID)
    layer = _layers(spec)[FIXED_CORRIDOR_LAYER_KEY]
    assert layer.source_status == "unavailable"
    assert layer.source_detail["currentness"]["currentness"] == "stale"
    assert spec.metadata["production_gate"]["fixed_corridor_band"]["drawn"] is False
    assert spec.metadata["production_gate"]["map_disclosure"]
    omitted = {item["layer_key"] for item in spec.omitted_layers}
    assert FIXED_CORRIDOR_LAYER_KEY in omitted


def test_stale_band_disclosure_is_in_metadata_not_on_the_map(tmp_path):
    """详细原因进 metadata / 披露，图面上**不**新增诊断文字块（annotations 恒空）。"""

    service, _, _ = _service(tmp_path)
    spec = service.build_figure(template_id="communication_layout_v1", route_id=ROUTE_ID)
    assert spec.annotations == []
    entries = {item["disclosure_id"]: item for item in spec.metadata["disclosures"]}
    band = entries["fixed_corridor_service_band_disclosure"]
    text = "\n".join(band["lines"])
    assert "服务保障目标带" in text
    assert "不是认证导航覆盖区" in text
    assert "2000" in text  # 与导航关联范围的分离声明必须在册
    assert "未" in text and "绘制该带" in text


# --------------------------------------------------------------------------- 正式成果图门禁


def test_formal_600dpi_export_is_refused_while_results_are_stale(tmp_path):
    service, session, renderer = _service(tmp_path)
    with pytest.raises(MapFigureCurrentnessGate) as error:
        service.export(
            template_id="cns_combined_v1", route_id=ROUTE_ID,
            dpi=int(FORMAL_DELIVERABLE_MIN_DPI),
        )
    assert error.value.code == "map_figure_currentness_gate"
    detail = error.value.detail
    assert detail["production_gate"]["is_current_figure"] is False
    assert detail["production_gate"]["layer_states"]
    # 明确拒绝：没有渲染、也没有写任何业务状态。
    assert renderer.calls == []
    assert session.saved == 0


def test_normal_dpi_export_and_preview_still_work(tmp_path):
    service, session, renderer = _service(tmp_path)
    exported = service.export(template_id="cns_combined_v1", route_id=ROUTE_ID, dpi=300)
    assert exported["record"]["dpi"] == 300.0
    preview = service.render_preview(template_id="cns_combined_v1", route_id=ROUTE_ID)
    assert preview["image"].startswith(b"\x89PNG")
    assert session.saved == 0


# --------------------------------------------------------------------------- 导航监测身份

NAV_SIMULATION = {
    "status": "passed", "reason": None,
    "route_identity": {
        "route_id": ROUTE_ID, "route_authority": "canonical_operational_route",
        "route_geometry_fingerprint": route_geometry_fingerprint(ROUTE_PATH),
    },
    "parameters": {"monitor_association_radius_m": 2000.0},
    "full_width_service_assurance_status": "not_evaluated",
    "semantic_disclaimer": "中心线关联仿真，不代表左右各500米全宽CNS保障能力",
    "selected_mandatory_site_ids": ["N005", "N006"],
    "selected_optional_sites": [{"site_id": "tower-1", "gain_score": 120.0}],
    "unselected_candidate_ids": ["tower-2"],
    "devices": [{"site_id": "N005"}, {"site_id": "N006"}],
    "merged_intervals_m": [[0.0, 400.0], [1200.0, 2100.0]],
    "gaps": [{"start_m": 400.0, "end_m": 1200.0, "length_m": 800.0,
              "duration_s": 53.3, "tolerance_status": "exceeded"}],
    "longest_gap_m": 800.0, "longest_gap_duration_s": 53.3,
    "all_gaps_within_tolerance": False,
    "result_fingerprint": "navsim-result-fingerprint",
    "writes_performed": 0,
}

NAV_CANDIDATES = {
    "candidates": [
        {"candidate_id": "N005", "candidate_kind": "project_node",
         "spatial_use_status": "eligible",
         "coordinate": {"longitude": ROUTE_PATH[0][0], "latitude": ROUTE_PATH[0][1]}},
        {"candidate_id": "N006", "candidate_kind": "project_node",
         "spatial_use_status": "eligible",
         "coordinate": {"longitude": ROUTE_PATH[-1][0], "latitude": ROUTE_PATH[-1][1]}},
        {"candidate_id": "tower-1", "candidate_kind": "communication_tower",
         "spatial_use_status": "eligible",
         "coordinate": {"longitude": ROUTE_PATH[5][0], "latitude": ROUTE_PATH[5][1]}},
        {"candidate_id": "tower-2", "candidate_kind": "communication_tower",
         "spatial_use_status": "eligible",
         "coordinate": {"longitude": ROUTE_PATH[7][0], "latitude": ROUTE_PATH[7][1]}},
    ],
}


def _nav_service(tmp_path, *, simulation=None, candidates=None):
    return _service(
        tmp_path,
        navigation_monitoring_provider=lambda route: json.loads(
            json.dumps(NAV_SIMULATION if simulation is None else simulation)
        ),
        shared_site_candidates_provider=lambda: json.loads(
            json.dumps(NAV_CANDIDATES if candidates is None else candidates)
        ),
    )


def test_navigation_figure_draws_mandatory_endpoints_association_and_longest_gap(tmp_path):
    service, _, _ = _nav_service(tmp_path)
    spec = service.build_figure(template_id="navigation_layout_v1", route_id=ROUTE_ID)
    layers = _layers(spec)
    mandatory = layers["cns_nav_monitor_sites"]
    assert mandatory.feature_count == 2
    assert {point["site_id"] for point in mandatory.data["points"]} == {"N005", "N006"}
    assert all(point["planned_not_installed"] for point in mandatory.data["points"])
    assert all(point["confirmed_installed"] is False for point in mandatory.data["points"])
    # 选中的沿途站址与未选中候选严格分开。
    assert [point["site_id"] for point in layers["cns_nav_monitor_optional"].data["points"]] \
        == ["tower-1"]
    assert [point["site_id"] for point in layers["cns_nav_monitor_unselected"].data["points"]] \
        == ["tower-2"]
    # 关联段与最长缺口都是线几何，且缺口只画最长那一条。
    assert len(layers["cns_nav_association"].data["lines"]) == 2
    assert len(layers["cns_nav_gap"].data["lines"]) == 1
    metadata = spec.metadata["navigation_monitoring"]
    assert metadata["currentness"] == "current"
    assert metadata["full_width_service_assurance_status"] == "not_evaluated"
    assert metadata["longest_gap_m"] == 800.0
    # 图面不出现诊断文字块。
    assert spec.annotations == []


def test_navigation_figure_never_upgrades_centerline_pass_to_full_width(tmp_path):
    service, _, _ = _nav_service(tmp_path)
    spec = service.build_figure(template_id="navigation_layout_v1", route_id=ROUTE_ID)
    text = "\n".join(
        line for entry in spec.metadata["disclosures"] for line in entry["lines"]
    )
    assert "full_width_service_assurance_status=not_evaluated" in text
    assert "不是左右各 500 m 全宽 CNS 保障能力的结论" in text


def test_navigation_monitoring_is_stale_when_route_geometry_differs(tmp_path):
    simulation = json.loads(json.dumps(NAV_SIMULATION))
    simulation["route_identity"]["route_geometry_fingerprint"] = "layeredpathv1-deadbeef"
    service, _, _ = _nav_service(tmp_path, simulation=simulation)
    spec = service.build_figure(template_id="navigation_layout_v1", route_id=ROUTE_ID)
    layer = _layers(spec)["cns_nav_monitor_sites"]
    assert layer.source_detail["currentness"]["currentness"] == "stale"
    gate = spec.metadata["production_gate"]
    assert gate["is_current_figure"] is False
    assert gate["layer_states"]["cns_nav_monitor_sites"] == "stale"


def test_navigation_figure_without_provider_discloses_absence(tmp_path):
    service, _, _ = _service(tmp_path)
    spec = service.build_figure(template_id="navigation_layout_v1", route_id=ROUTE_ID)
    keys = set(_layers(spec))
    assert not (keys & {"cns_nav_monitor_sites", "cns_nav_monitor_optional",
                        "cns_nav_monitor_unselected", "cns_nav_association", "cns_nav_gap"})
    metadata = spec.metadata["navigation_monitoring"]
    assert metadata["available"] is False
    assert metadata["reason"] == "navigation_monitoring_provider_not_configured"


def test_navigation_monitoring_layers_are_not_p16_proposals(tmp_path):
    """强制端点监测设备与 P16 规划提案是不同图层、不同来源，绝不互相顶替。"""

    state = _state()
    state["cns_corridor_site_plan"] = {
        "status": "proposal_ready", "proposal_only": True,
        "selected_actions": [{
            "action_id": "add_navigation_integrity_monitor:R0005:origin:N005",
            "action": "add_navigation_integrity_monitor",
            "service_key": "N:navigation_integrity_monitoring",
            "route_id": ROUTE_ID, "site_id": "N005",
            "coordinate": list(ROUTE_PATH[0]), "proposal_only": True,
        }],
    }
    service, _, _ = _service(
        tmp_path, state=state,
        navigation_monitoring_provider=lambda route: json.loads(json.dumps(NAV_SIMULATION)),
        shared_site_candidates_provider=lambda: json.loads(json.dumps(NAV_CANDIDATES)),
    )
    spec = service.build_figure(template_id="navigation_layout_v1", route_id=ROUTE_ID)
    layers = _layers(spec)
    assert "cns_nav_proposal" in layers
    assert "cns_nav_monitor_sites" in layers
    assert layers["cns_nav_proposal"].source_role == "cns_corridor_site_plan"
    assert layers["cns_nav_monitor_sites"].source_role \
        == "navigation_monitoring_simulation_readonly"
    detail = layers["cns_nav_monitor_sites"].source_detail
    assert detail["recomputes_site_selection"] is False
    assert detail["read_only_simulation"] is True


# --------------------------------------------------------------------------- Radar 站址身份


def test_radar_candidates_declare_site_identity_and_never_infer_installation(tmp_path):
    """Radar 图：站址存在 ≠ 设备已安装；且不得用圆形覆盖替代方向性模型。"""

    state = _state()
    state["towers"] = {"items": [{
        "tower_id": "tower-comm-001", "name": "普陀桃花沙岙村H杆站",
        "coordinate": [122.228521, 29.831441], "height_m": 6.0, "site_type": "H杆塔",
    }]}
    state["radar_surveillance_layout"] = {
        "status": "infeasible", "count": 1,
        "items": [{
            "route_id": ROUTE_ID, "altitude_layer_id": "ALT-100", "altitude_m": 100.0,
            "algorithm_id": "radar_surveillance_layout", "algorithm_version": "1.3",
            "status": "infeasible", "gap_reason": "independent_site_count_limited",
            "gap_classification": "confirmed_gap", "managed_physical_gap": True,
            "selected_panel_count": 0, "selected_panels": [],
            "selected_tower_count": 0, "selected_tower_ids": [],
            "radar_i_panel_count": 0, "radar_ii_panel_count": 0,
            "candidate_tower_ids": ["tower-comm-001"], "candidate_tower_count": 1,
            # 候选塔 id 的**真实**来源是 candidate_statistics.per_tower
            # （cns_facility_assembler._candidate_tower_ids 的唯一读取处）。
            "candidate_statistics": {"per_tower": [{"tower_id": "tower-comm-001"}]},
            "input_fingerprint": "radarlayout-r0005-infeasible",
        }],
    }
    service, _, _ = _service(tmp_path, state=state)
    spec = service.build_figure(
        template_id="surveillance_layout_v1", route_id=ROUTE_ID,
        parameter_overrides={"surveillance_service": "radar_noncooperative"},
    )
    layers = _layers(spec)
    # 既有表达不变：仍然只有候选上下文与能力限制，绝不出现站址 / panel / 扇区。
    assert "cns_radar_context" in layers
    assert "cns_radar_limitation" in layers
    assert spec.annotations == []
    context = layers["cns_radar_context"]
    detail = context.source_detail
    assert detail["never_infers_installation_from_site_presence"] is True
    assert detail["no_circular_coverage_substitution"] is True
    assert detail["manifested_radar_sites"] == 0
    for point in context.data["points"]:
        assert point["site_identity"] == "existing_tower_installation"
        assert point["installed_evidence_status"] == "no_installed_evidence"
        assert point["confirmed_installed"] is False
        assert point["planned_installation_height_source"] == "not_confirmed"


# --------------------------------------------------------------------------- 渲染数据契约


def test_spec_serialises_with_band_rings_ready_for_the_renderer(tmp_path):
    """FigureSpec 必须能被渲染器直接消费：JSON-safe，且面图层外环 ≥ 4 个点。

    本环境没有 QGIS 运行时（``import qgis`` 不可用），因此这里验证的是**数据契约**
    而不是渲染结果：``_polygons_of`` 只接受 ``data["polygons"]`` 中 ``len(ring) >= 4``
    的环，且坐标必须已经闭合。
    """

    built = _built()
    entry = {
        "route_id": ROUTE_ID, "fixed_corridor_geometry_status": "available",
        "fixed_corridor_geometry": {"geometry_fingerprint": built["geometry_fingerprint"]},
    }
    state = _state(result_statuses={"cns_corridor_assessment": "passed"})
    state["operational_routes"][0]["status"] = "passed"
    service, _, _ = _service(
        tmp_path, state=state,
        fixed_corridor_service=_FakeFixedCorridorService(
            projected={"algorithm_id": "cns_service_corridor_v1",
                       "algorithm_version": "1.4", "routes": [entry]},
            entry=entry,
        ),
    )
    spec = service.build_figure(template_id="cns_combined_v1", route_id=ROUTE_ID)
    payload = spec.to_dict()
    # JSON-safe：不含 QGIS 对象、不含 NaN。
    text = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    assert "cns_fixed_corridor" in text
    layer = next(item for item in payload["layers"]
                 if item["layer_key"] == FIXED_CORRIDOR_LAYER_KEY)
    rings = layer["data"]["polygons"]
    assert rings and all(len(ring) >= 4 for ring in rings)
    assert all(ring[0] == ring[-1] for ring in rings)
    metadata = payload["metadata"]
    identity = metadata["currentness"]["route_geometry_identity"]
    assert identity["route_geometry_fingerprint"] == route_geometry_fingerprint(ROUTE_PATH)
    assert identity["route_geometry_fingerprint"].startswith("layeredpathv1-")
    assert metadata["currentness"]["semantics"] == "figure_currentness_projection_read_only"


# --------------------------------------------------------------------------- 门禁纯函数


def test_production_gate_prefers_stale_over_unknown_and_never_current_on_empty():
    gate = production_gate({
        "a": {"currentness": "current"},
        "b": {"currentness": "unknown", "reason": "not_evaluated"},
        "c": {"currentness": "stale", "reason": "route_operational_status_not_passed"},
    })
    assert gate["status"] == "stale"
    assert gate["is_current_figure"] is False
    assert gate["layers_by_state"]["stale"] == ["c"]
    assert gate["layer_reasons"]["c"] == "route_operational_status_not_passed"
    empty = production_gate({})
    assert empty["status"] == "unknown"
    assert empty["is_current_figure"] is False
    assert empty["no_silent_empty_figure"] is True


def test_template_band_key_matches_gis_layer_key():
    """模板目录与 GIS 层各自持有图层键常量，必须逐字符一致（否则图例会找不到样式）。"""

    assert TEMPLATE_BAND_KEY == FIXED_CORRIDOR_LAYER_KEY
    assert legend_order_key(FIXED_CORRIDOR_LAYER_KEY) is not None
