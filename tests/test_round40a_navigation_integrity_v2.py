"""Round40-A：navigation_integrity_monitoring_v2（1000 m）合同与端点半径回归。

覆盖：
* registry v1/v2 归一、版本互斥、严格校验不放松；
* endpoint 999 / 1000 / 1001 米边界、缺坐标 unknown、v2 身份不完整 unknown；
* required_cns 显式 services 优先、无 services 时不误回退到 canonical N key；
* P14 endpoint scope 判定、P15 endpoint gap、P16 只布起降点；
* v1/v2 指纹差异；
* P17 不消费 N。

只读：不写 ProjectState、不调用任何 service 写入入口。
"""

import ast
from pathlib import Path

import pytest

from cns_planner.domain.cns_inputs import (
    normalize_required_cns as _normalize_required_cns_formal,
)
from cns_planner.domain.cns_service_contract import SERVICE_KEY_NAVIGATION_INTEGRITY_MONITORING
from cns_planner.domain.cns_service_registry import (
    _normalize_navigation_integrity_planning as _normalize_navigation_integrity_planning_formal,
    endpoint_scope_only_subsystems,
)
from cns_planner.domain import navigation_integrity_monitoring as nim
from cns_planner.domain.navigation_integrity_contract_v2 import (
    CONTRACT_VERSION_V1 as NAVIGATION_INTEGRITY_CONTRACT_VERSION_V1,
    CONTRACT_VERSION_V2 as NAVIGATION_INTEGRITY_CONTRACT_VERSION_V2,
    INVALID_V2_CONTRACT,
    RADIUS_M as NAVIGATION_INTEGRITY_V2_RADIUS_M,
    contract_from_requirement as navigation_integrity_contract,
    horizontal_distance_m,
)
from cns_planner.domain.navigation_integrity_monitoring import (
    build_navigation_integrity_endpoint_evidence,
    endpoint_gap_evidence,
    navigation_integrity_monitor_actions,
    navigation_integrity_requirement_for,
)


KEY = SERVICE_KEY_NAVIGATION_INTEGRITY_MONITORING
BASE = [122.0, 30.0]
ROUTE = {
    "route_id": "R1", "status": "passed", "start_node_id": "A", "end_node_id": "B",
    "path": [[122.0, 30.0], [122.5, 30.0]],
}


def normalize_required_cns(value):
    """研究测试包装：v2 只能经显式 ``allow_research_v2=True`` 进入。"""

    return _normalize_required_cns_formal(value, allow_research_v2=True)


def _normalize_navigation_integrity_planning(value, *, field="p"):
    """研究测试包装：直接测 v2 兼容层时显式声明研究调用点。"""

    return _normalize_navigation_integrity_planning_formal(
        value, field=field, allow_research_v2=True,
    )


def test_formal_project_state_path_rejects_v2_from_version_field_only():
    """正式路径不得仅凭 version 字段启用研究 N v2（权限负向验收）。"""

    payload = {"project_default": {
        "communication": {"required": True, "confirmed": True},
        "navigation": {"required": True, "confirmed": True, "services": {KEY: {
            "required": True, "confirmed": True,
            "planning": {"version": NAVIGATION_INTEGRITY_CONTRACT_VERSION_V2},
        }}},
        "surveillance": {"required": False},
    }}
    with pytest.raises(ValueError, match="research_isolated v2"):
        _normalize_required_cns_formal(payload)
    with pytest.raises(ValueError, match="research_isolated v2"):
        _normalize_required_cns_formal(payload, allow_research_v2=False)
    with pytest.raises(ValueError, match="research_isolated v2"):
        _normalize_navigation_integrity_planning_formal(
            {"version": NAVIGATION_INTEGRITY_CONTRACT_VERSION_V2}, field="p",
        )
    # 研究调用点显式放行后仍返回同一 v2 合同。
    research = _normalize_required_cns_formal(payload, allow_research_v2=True)
    planning = research["project_default"]["navigation"]["services"][KEY]["planning"]
    assert planning["contract_version"] == NAVIGATION_INTEGRITY_CONTRACT_VERSION_V2
    assert planning["local_monitoring_radius_m"] == 1000.0


def lon_offset_for(distance_m, *, lon=122.0, lat=30.0):
    """反解使 haversine 距离等于 ``distance_m`` 的经度增量（未取整浮点）。"""

    low, high = 0.0, 0.05
    for _ in range(90):
        mid = (low + high) / 2.0
        if horizontal_distance_m([lon, lat], [lon + mid, lat]) < distance_m:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0


def requirement(*, version=None):
    planning = {"version": version} if version else {}
    return normalize_required_cns({"project_default": {
        "communication": {"required": True, "confirmed": True},
        "navigation": {"required": True, "confirmed": True, "services": {KEY: {
            "required": True, "confirmed": True, "planning": planning,
        }}},
        "surveillance": {"required": False},
    }})


def facility(site, coordinate, *, service_key=KEY):
    return {
        "facility_id": f"F-{site}", "site_id": site,
        "takeoff_landing_site_id": site,
        "distinct_site_id": f"takeoff_landing_site:{site}",
        "coordinate": coordinate, "status": "active",
        "devices": [{"device_id": f"NAV-{site}", "service_key": service_key, "status": "active"}],
    }


def delivery(status="satisfied"):
    return {"R1": {"origin": status, "destination": status}}


def evidence(*, version=None, items=None, route=ROUTE, comm=None):
    return build_navigation_integrity_endpoint_evidence(
        requirement(version=version), [route],
        existing_facilities={"items": items or []},
        communication_delivery=delivery() if comm is None else comm,
    )


# ---------------------------------------------------------------------------
# 1) registry：v1 保留 / v2 可解析 / 版本互斥 / 严格校验不放松
# ---------------------------------------------------------------------------


def test_v1_contract_is_unchanged():
    planning = _normalize_navigation_integrity_planning(None, field="p")
    assert planning["local_monitoring_radius_m"] == 10000.0
    assert planning["confirmed"] is True
    assert "contract_version" not in planning
    assert planning["source"].startswith("Round29-E")


def test_v2_contract_is_1000m_and_not_confirmed():
    planning = _normalize_navigation_integrity_planning(
        {"version": NAVIGATION_INTEGRITY_CONTRACT_VERSION_V2}, field="p",
    )
    assert planning["contract_version"] == NAVIGATION_INTEGRITY_CONTRACT_VERSION_V2
    assert planning["local_monitoring_radius_m"] == 1000.0
    assert planning["radius_basis"] == "engineering_assumption"
    assert planning["placement_scope"] == "route_endpoints"
    assert planning["required_monitor_per_endpoint"] == 1
    assert planning["corridor_monitoring_required"] is False
    assert planning["confirmed"] is False
    assert "用户正式假设" in planning["source"]


def test_explicit_v1_still_returns_10000m():
    planning = _normalize_navigation_integrity_planning(
        {"version": NAVIGATION_INTEGRITY_CONTRACT_VERSION_V1}, field="p",
    )
    assert planning["local_monitoring_radius_m"] == 10000.0
    assert planning["confirmed"] is True


def test_versions_are_mutually_exclusive():
    with pytest.raises(ValueError, match="必须一致"):
        _normalize_navigation_integrity_planning({
            "version": NAVIGATION_INTEGRITY_CONTRACT_VERSION_V1,
            "contract_version": NAVIGATION_INTEGRITY_CONTRACT_VERSION_V2,
        }, field="p")
    with pytest.raises(ValueError, match="版本互斥"):
        _normalize_navigation_integrity_planning({
            "version": NAVIGATION_INTEGRITY_CONTRACT_VERSION_V2,
            "local_monitoring_radius_m": 10000.0,
        }, field="p")
    with pytest.raises(ValueError, match="必须是"):
        _normalize_navigation_integrity_planning({"version": "navigation_v3"}, field="p")


def test_v2_strict_validation_is_not_relaxed():
    with pytest.raises(ValueError, match="corridor_monitoring_required"):
        _normalize_navigation_integrity_planning({
            "version": NAVIGATION_INTEGRITY_CONTRACT_VERSION_V2,
            "corridor_monitoring_required": True,
        }, field="p")
    with pytest.raises(ValueError, match="Round29-E"):
        _normalize_navigation_integrity_planning({"local_monitoring_radius_m": 1000.0}, field="p")


def test_missing_version_never_implies_v2():
    planning = _normalize_navigation_integrity_planning(
        {"local_monitoring_radius_m": 10000.0}, field="p",
    )
    assert planning["local_monitoring_radius_m"] == 10000.0
    assert "contract_version" not in planning


def test_v1_and_v2_planning_round_trip_is_stable():
    v1 = _normalize_navigation_integrity_planning(None, field="p")
    assert _normalize_navigation_integrity_planning(v1, field="p") == v1
    v2 = _normalize_navigation_integrity_planning(
        {"version": NAVIGATION_INTEGRITY_CONTRACT_VERSION_V2}, field="p",
    )
    assert _normalize_navigation_integrity_planning(v2, field="p") == v2


# ---------------------------------------------------------------------------
# 2) endpoint 半径：999 / 1000 / 1001 与缺坐标
# ---------------------------------------------------------------------------


def test_999m_monitor_is_inside_radius():
    coordinate = [BASE[0] + lon_offset_for(999.0), BASE[1]]
    result = evidence(version=NAVIGATION_INTEGRITY_CONTRACT_VERSION_V2,
                      items=[facility("A", coordinate)])
    endpoint = result["routes"][0]["endpoints"]["origin"]
    assert endpoint["status"] == "satisfied"
    assert endpoint["local_monitoring_radius_m"] == 1000.0
    assert endpoint["selected_installed_providers"][0]["horizontal_distance_m"] <= 1000.0


def test_1000m_monitor_is_inside_radius_inclusive():
    coordinate = [BASE[0] + lon_offset_for(1000.0), BASE[1]]
    result = evidence(version=NAVIGATION_INTEGRITY_CONTRACT_VERSION_V2,
                      items=[facility("A", coordinate)])
    endpoint = result["routes"][0]["endpoints"]["origin"]
    assert endpoint["status"] == "satisfied"


def test_1001m_monitor_is_out_of_radius_and_reported():
    coordinate = [BASE[0] + lon_offset_for(1001.0), BASE[1]]
    result = evidence(version=NAVIGATION_INTEGRITY_CONTRACT_VERSION_V2,
                      items=[facility("A", coordinate), facility("B", [122.5, 30.0])])
    route = result["routes"][0]
    endpoint = route["endpoints"]["origin"]
    assert endpoint["status"] == "confirmed_deficit"
    assert endpoint["reasons"] == [nim.REASON_MISSING["origin"]]
    assert endpoint["rejected_out_of_radius_providers"][0]["horizontal_distance_m"] > 1000.0
    # P16 仍只为真实起降点生成动作，绝不因为"超半径"而生成中段动作。
    actions = navigation_integrity_monitor_actions(endpoint_gap_evidence(result))
    assert {item["endpoint_role"] for item in actions} == {"origin"}
    assert actions[0]["takeoff_landing_site_id"] == "A"
    assert actions[0]["local_monitoring_radius_m"] == 1000.0


def test_v1_keeps_legacy_site_binding_semantics():
    # v1 没有 radius 判定：同起降点站址的历史行为保持不变。
    coordinate = [BASE[0] + lon_offset_for(50000.0), BASE[1]]
    result = evidence(items=[facility("A", coordinate)])
    assert result["routes"][0]["endpoints"]["origin"]["status"] == "satisfied"
    assert "local_monitoring_radius_m" not in result["routes"][0]["endpoints"]["origin"]


def test_missing_monitor_coordinate_is_unknown_not_missing():
    result = evidence(version=NAVIGATION_INTEGRITY_CONTRACT_VERSION_V2,
                      items=[facility("A", None)])
    endpoint = result["routes"][0]["endpoints"]["origin"]
    assert endpoint["status"] == "unknown"
    assert endpoint["reasons"] == [nim.REASON_EVIDENCE_UNKNOWN]
    assert endpoint["unresolved_provider_evidence"]
    # unknown 不是 confirmed gap ⇒ P16 绝不为该端点生成动作（destination 是真缺口）。
    actions = navigation_integrity_monitor_actions(endpoint_gap_evidence(result))
    assert [item for item in actions if item["endpoint_role"] == "origin"] == []
    assert {item["endpoint_role"] for item in actions} == {"destination"}


def test_missing_endpoint_coordinate_is_unknown():
    route = {"route_id": "R1", "status": "passed", "start_node_id": "A",
             "end_node_id": "B", "path": []}
    result = evidence(version=NAVIGATION_INTEGRITY_CONTRACT_VERSION_V2,
                      items=[facility("A", None)], route=route)
    assert result["routes"][0]["endpoints"]["origin"]["status"] == "unknown"
    assert result["routes"][0]["endpoints"]["destination"]["status"] == "unknown"


def test_incomplete_v2_identity_is_unknown(monkeypatch):
    # registry 归一总是补全合同；这里直接验证 domain 对"未归一/被篡改"的 v2 合同
    # 必须 fail-closed 成 unknown，而不是猜 0 或回退 v1。
    raw = {"project_default": {"navigation": {"services": {KEY: {
        "required": True, "planning": {"contract_version": NAVIGATION_INTEGRITY_CONTRACT_VERSION_V2},
    }}}}}
    monkeypatch.setattr(nim, "navigation_integrity_requirement_for", lambda *a, **k: raw["project_default"]["navigation"]["services"][KEY])
    result = build_navigation_integrity_endpoint_evidence(
        raw, [ROUTE], existing_facilities={"items": [
            {"facility_id": "F-A", "takeoff_landing_site_id": "A",
             "distinct_site_id": "takeoff_landing_site:A", "coordinate": BASE,
             "devices": [{"device_id": "M", "service_key": KEY, "status": "active"}]},
        ]},
        communication_delivery=delivery(),
    )
    endpoint = result["routes"][0]["endpoints"]["origin"]
    assert endpoint["status"] == "unknown"
    assert endpoint["contract_status"] == "invalid_or_incomplete_v2_contract"
    assert endpoint["local_monitoring_radius_m"] is None


def test_contract_parser_rejects_incomplete_identity():
    assert navigation_integrity_contract({"planning": {"contract_version": NAVIGATION_INTEGRITY_CONTRACT_VERSION_V2}}) == INVALID_V2_CONTRACT
    assert navigation_integrity_contract({"planning": {"placement_scope": "route_endpoints"}}) is None
    assert isinstance(navigation_integrity_contract({"planning": {
        "contract_version": NAVIGATION_INTEGRITY_CONTRACT_VERSION_V2,
        "placement_scope": "route_endpoints",
        "local_monitoring_radius_m": 1000.0,
    }}), dict)


# ---------------------------------------------------------------------------
# 3) required_cns services：显式优先 / legacy 不误回退
# ---------------------------------------------------------------------------


def test_route_override_v2_takes_precedence_over_v1_project_default():
    required = normalize_required_cns({
        "project_default": {
            "communication": {"required": True, "confirmed": True},
            "navigation": {"required": True, "confirmed": True, "services": {KEY: {
                "required": True, "confirmed": True, "planning": {},
            }}},
            "surveillance": {"required": False},
        },
        "route_overrides": {"R1": {
            "communication": {"required": True, "confirmed": True},
            "navigation": {"required": True, "confirmed": True, "services": {KEY: {
                "required": True, "confirmed": True,
                "planning": {"version": NAVIGATION_INTEGRITY_CONTRACT_VERSION_V2},
            }}},
            "surveillance": {"required": False},
        }},
    })
    requirement = navigation_integrity_requirement_for(required, "R1")
    assert requirement["planning"]["contract_version"] == NAVIGATION_INTEGRITY_CONTRACT_VERSION_V2
    assert requirement["planning"]["local_monitoring_radius_m"] == 1000.0


def test_missing_services_does_not_fall_back_to_canonical_key():
    required = normalize_required_cns({"project_default": {
        "navigation": {"required": True, "confirmed": True},
    }})
    assert navigation_integrity_requirement_for(required, "R1") is None
    # legacy 回落身份是 N:navigation，绝不冒充 canonical N:navigation_integrity_monitoring。
    assert endpoint_scope_only_subsystems(required["project_default"]) == frozenset()


# ---------------------------------------------------------------------------
# 4) P14 endpoint scope
# ---------------------------------------------------------------------------


def test_explicit_v2_moves_n_to_endpoint_scope():
    required = normalize_required_cns({
        "project_default": {
            "communication": {"required": True, "confirmed": True},
            "navigation": {"required": True, "confirmed": True, "services": {KEY: {
                "required": True, "confirmed": True,
                "planning": {"version": NAVIGATION_INTEGRITY_CONTRACT_VERSION_V2},
            }}},
            "surveillance": {"required": False},
        },
    })
    assert endpoint_scope_only_subsystems(required["project_default"]) == frozenset({"N"})
    # v1 显式声明也是 endpoint scope（作用域从未改变，变的是半径版本）。
    assert endpoint_scope_only_subsystems(requirement()["project_default"]) == frozenset({"N"})


def test_v2_evidence_route_scope_is_endpoints_only():
    result = evidence(version=NAVIGATION_INTEGRITY_CONTRACT_VERSION_V2,
                      items=[facility("A", BASE)])
    assert result["service_scope"] == "route_endpoints"
    assert result["contract_version"] == NAVIGATION_INTEGRITY_CONTRACT_VERSION_V2
    assert result["local_monitoring_radius_m"] == 1000.0
    assert result["routes"][0]["continuous_gap_m"] is None


# ---------------------------------------------------------------------------
# 5) P16：只布起降点、不生成中段动作
# ---------------------------------------------------------------------------


def test_p16_actions_are_endpoint_bound_only():
    result = evidence(version=NAVIGATION_INTEGRITY_CONTRACT_VERSION_V2, items=[])
    actions = navigation_integrity_monitor_actions(endpoint_gap_evidence(result))
    assert len(actions) == 2
    assert {item["endpoint_role"] for item in actions} == {"origin", "destination"}
    assert {item["takeoff_landing_site_id"] for item in actions} == {"A", "B"}
    assert all(item["site_binding"] == "takeoff_landing_site" for item in actions)
    assert all(item["local_monitoring_radius_m"] == 1000.0 for item in actions)
    assert all("voxel_id" not in item for item in actions)


def test_p16_what_if_uses_same_radius_and_removes_confirmed_gap():
    result = evidence(version=NAVIGATION_INTEGRITY_CONTRACT_VERSION_V2, items=[])
    gap = endpoint_gap_evidence(result)
    actions = navigation_integrity_monitor_actions(gap)
    projected = nim.hypothetical_endpoint_monitor_evidence(result, actions)
    assert all(item["status"] == "satisfied"
               for item in projected["routes"][0]["endpoints"].values())
    assert all(item["local_monitoring_radius_m"] == 1000.0
               for item in projected["routes"][0]["endpoints"].values())


# ---------------------------------------------------------------------------
# 6) 指纹
# ---------------------------------------------------------------------------


def test_v1_and_v2_fingerprints_differ():
    v1 = evidence(items=[])
    v2 = evidence(version=NAVIGATION_INTEGRITY_CONTRACT_VERSION_V2, items=[])
    assert v1["input_fingerprint"] != v2["input_fingerprint"]
    assert "local_monitoring_radius_m" not in v1["routes"][0]["endpoints"]["origin"]
    assert v2["routes"][0]["endpoints"]["origin"]["local_monitoring_radius_m"] == 1000.0


def test_v2_fingerprint_changes_when_endpoint_radius_evidence_changes():
    inside = evidence(version=NAVIGATION_INTEGRITY_CONTRACT_VERSION_V2, items=[
        facility("A", [BASE[0] + lon_offset_for(999.0), BASE[1]]),
    ])
    outside = evidence(version=NAVIGATION_INTEGRITY_CONTRACT_VERSION_V2, items=[
        facility("A", [BASE[0] + lon_offset_for(1001.0), BASE[1]]),
    ])
    assert inside["input_fingerprint"] != outside["input_fingerprint"]


def test_map_display_radius_keeps_canonical_v1_and_research_v2_separate():
    #: main 的正式默认仍是 v1（10 km）；研究专用 v2 合同（1 km）只在显式版本下使用，
    #: 制图层不得把 v2 冒充成全局默认，也不得把 4 km 旧口径留在 2 km 合同里。
    from cns_planner.reporting.map_templates.template_catalog import CNS_COVERAGE_POLICY
    assert CNS_COVERAGE_POLICY[KEY]["local_monitoring_radius_m"] == 10000.0
    assert NAVIGATION_INTEGRITY_V2_RADIUS_M == 1000.0
    assert CNS_COVERAGE_POLICY["C:communication"]["radius_by_surface_m"] == {
        "land": 2000.0, "coastal_uncertain": 2000.0, "sea": 2000.0,
    }


# ---------------------------------------------------------------------------
# 7) 回归：RID / COMM / RTK / 普通导航不受影响
# ---------------------------------------------------------------------------


def test_other_service_policies_are_untouched():
    from cns_planner.domain.cns_service_contract import (
        SERVICE_KEY_COMMUNICATION, SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION,
        SERVICE_KEY_RID_COOPERATIVE, service_policy,
    )

    comm = service_policy(SERVICE_KEY_COMMUNICATION)
    rid = service_policy(SERVICE_KEY_RID_COOPERATIVE)
    #: 2026-10-27 半径裁定：通信统一 2 km；RID 2000/5000 不变。
    assert comm["radius_by_surface"]["land"] == 2000.0
    assert rid["radius_by_surface"] == {"land": 2000.0, "coastal_uncertain": 2000.0, "sea": 5000.0}
    assert service_policy(SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION) is None
    assert service_policy(KEY) is None


# ---------------------------------------------------------------------------
# 8) P17 不消费 N
# ---------------------------------------------------------------------------


def test_p17_source_has_no_navigation_integrity_reference():
    text = Path("cns_planner/application/continuous_service_service.py").read_text(encoding="utf-8")
    lowered = text.lower()
    for token in ("navigation_integrity", "navint", "endpoint_integrity_monitor"):
        assert token not in lowered
    tree = ast.parse(text)
    imported = {
        node.module for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not any("navigation_integrity" in (module or "") for module in imported)
