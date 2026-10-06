"""Required CNS Adopt → canonical ``required_cns.services`` 回归。

裁定（Round 2.2 起，Round 29-H 扩展）：

【Adopt 后 authoritative required_cns 必须携带正式 service requirements】

* recommendation 必须给出 ``services``；
* adopt 原样/规范化写入 authoritative ``required_cns``；
* workflow snapshot 恢复后仍然存在；
* P14 / P15 / P16 消费这些 service requirements，**不允许**再静默回落 legacy 语义；
* Round 29-H：已确认工程要求对应的**四个** canonical 服务都必须能从
  recommendation 正式产出并通过 adopt 闭环 ——
  ``C:communication`` / ``N:navigation_integrity_monitoring`` /
  ``S:rid_cooperative`` / ``S:radar_noncooperative``；
  Surveillance 因此是 ``service_requirement_mode = all_required``；
* 合作监视（RID）与非合作监视（Radar）是**两条独立服务**：绝不合并、绝不互相顶替；
* legacy 身份（``N:navigation`` / ``S:surveillance``）**绝不**被升级成 canonical 服务。

本文件只断言"契约与数值"，不重复其它文件已覆盖的 P14/P15 几何。
"""

from copy import deepcopy
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cns_planner.application.workflow_service import WorkflowService  # noqa: E402
from cns_planner.domain.cns_inputs import normalize_required_cns, pending_required_cns  # noqa: E402
from cns_planner.domain.cns_service_contract import (  # noqa: E402
    SERVICE_KEY_COMMUNICATION, SERVICE_KEY_RADAR_NONCOOPERATIVE,
    SERVICE_KEY_RID_COOPERATIVE, SERVICE_KEY_SURVEILLANCE,
)
from cns_planner.domain.cns_service_registry import (  # noqa: E402
    canonicalize_requirement_services, normalize_service_requirements,
    required_services_for,
)

DEFAULTS = ROOT / "cns_planner" / "config" / "defaults.json"

#: 本轮裁定的冻结数值（与 cns_service_contract 的既有 canonical 常量一致）。
EXPECTED_COMMUNICATION = {
    "radius_by_surface": {"land": 4000.0, "coastal_uncertain": 4000.0, "sea": 4000.0},
    "redundancy_by_surface": {"land": 2, "sea": 1, "coastal_uncertain": 2},
}
EXPECTED_RID = {
    "radius_by_surface": {"land": 2000.0, "coastal_uncertain": 2000.0, "sea": 5000.0},
    "redundancy_by_surface": {"land": 2, "sea": 1, "coastal_uncertain": 2},
}


def context(**values):
    return {"project_default": {
        name: {"value": value, "source": "synthetic_fixture", "confirmed": True}
        for name, value in values.items()
    }}


def _confirmed_requirement(**extra):
    base = {
        "confirmed": True, "source": "engineering_assumption",
        "coverage_requirement": 0.95, "required": True,
    }
    base.update(extra)
    return base


def current_required_cns(*, communication_key=SERVICE_KEY_COMMUNICATION,
                         surveillance_key=SERVICE_KEY_RID_COOPERATIVE):
    """模拟一次已经 Adopt 过的 authoritative ``required_cns``（带显式 service_key）。"""

    value = pending_required_cns()
    communication = _confirmed_requirement(
        max_gap_m=1000.0, performance={"max_latency_s": 1.0, "min_redundancy": 1},
    )
    if communication_key:
        communication["service_key"] = communication_key
        communication["redundancy_by_surface"] = {"land": 2, "sea": 1, "coastal_uncertain": 2}
    surveillance = _confirmed_requirement(
        performance={"max_update_interval_s": 2.0, "min_redundancy": 1},
    )
    if surveillance_key:
        surveillance["service_key"] = surveillance_key
        surveillance["redundancy_by_surface"] = {"land": 2, "sea": 1, "coastal_uncertain": 2}
        if surveillance_key == SERVICE_KEY_RID_COOPERATIVE:
            surveillance["type"] = {
                "target_cooperation": "cooperative", "sensor_mode": "passive",
                "technology": "network_remote_id", "service_subtype": "cooperative_surveillance",
            }
            surveillance["service_subtype"] = "cooperative_surveillance"
    value["project_default"] = {
        "communication": communication,
        "navigation": _confirmed_requirement(
            accuracy_m=10.0, integrity=True,
            performance={"max_horizontal_error_m": 10.0, "integrity_required": True,
                         "min_redundancy": 1},
        ),
        "surveillance": surveillance,
    }
    value["source"] = "synthetic_fixture"
    return normalize_required_cns(value)


#: policy 只声明普通字段（**不**声明 service_key）：显式服务身份来自已经 Adopt 过的
#: authoritative ``required_cns``，这正是真实项目的形状。
def policy(requirements=None):
    return {
        "policy_id": "P-1", "version": "1.0", "name": "P-1",
        "source_type": "project_rule", "source": "synthetic_fixture",
        "reference": "TEST", "clause": "1", "confirmed": True,
        "applicability": {"all_of": [{"field": "operation_mode", "operator": "eq", "value": "bvlos"}]},
        "requirements": requirements or {
            "communication": {
                "required": True, "coverage_requirement": 0.95, "max_gap_m": 1000.0,
                "confirmed": True, "source": "engineering_assumption",
                "performance": {"max_latency_s": 1.0, "min_redundancy": 1},
            },
            "navigation": {
                "required": True, "coverage_requirement": 0.95, "confirmed": True,
                "source": "engineering_assumption",
                "performance": {"max_horizontal_error_m": 10.0, "integrity_required": True,
                                "min_redundancy": 1},
            },
            "surveillance": {
                "required": True, "coverage_requirement": 0.95, "confirmed": True,
                "source": "engineering_assumption",
                "performance": {"max_update_interval_s": 2.0, "min_redundancy": 1},
            },
        },
    }


def _workflow(tmp_path, *, current=None, routes=("R-2",)):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    workflow.select_algorithm({
        "algorithm_type": "requirement_model",
        "algorithm_id": "operational_context_required_cns_v2", "version": "2.0",
        "parameters": {},
    })
    if current is not None:
        workflow.state["required_cns"] = deepcopy(current)
    workflow.state["scenario_routes"] = [{"route_id": route_id} for route_id in routes]
    workflow.set_cns_operation_context(context(operation_mode="bvlos"))
    workflow.set_cns_requirement_policies({"items": [policy()]})
    return workflow


# ---------------------------------------------------------------------------
# 1. recommendation 必须给出 services
# ---------------------------------------------------------------------------

def test_recommendation_emits_the_four_canonical_services(tmp_path):
    """Round 29-H：recommendation 必须正式产出已确认工程要求对应的四个服务。"""

    from cns_planner.domain.cns_service_contract import (
        SERVICE_KEY_NAVIGATION_INTEGRITY_MONITORING,
    )

    workflow = _workflow(tmp_path, current=current_required_cns())
    result = workflow.evaluate_required_cns_recommendation()
    recommendation = result["required_cns_recommendation"]

    assert recommendation["status"] == "recommendation_ready"
    recommended = recommendation["recommended_required_cns"]
    defaults = recommended["project_default"]

    communication = defaults["communication"]["services"]
    navigation = defaults["navigation"]["services"]
    surveillance = defaults["surveillance"]["services"]
    assert sorted(communication) == [SERVICE_KEY_COMMUNICATION]
    assert sorted(navigation) == [SERVICE_KEY_NAVIGATION_INTEGRITY_MONITORING]
    assert sorted(surveillance) == sorted([
        SERVICE_KEY_RADAR_NONCOOPERATIVE, SERVICE_KEY_RID_COOPERATIVE,
    ])
    #: 两条监视服务并存 ⇒ 该 subsystem 必须显式声明 all_required。
    assert defaults["surveillance"]["service_requirement_mode"] == "all_required"

    assert communication[SERVICE_KEY_COMMUNICATION]["radius_by_surface"] == \
        EXPECTED_COMMUNICATION["radius_by_surface"]
    assert communication[SERVICE_KEY_COMMUNICATION]["redundancy_by_surface"] == \
        EXPECTED_COMMUNICATION["redundancy_by_surface"]
    assert surveillance[SERVICE_KEY_RID_COOPERATIVE]["radius_by_surface"] == \
        EXPECTED_RID["radius_by_surface"]
    assert surveillance[SERVICE_KEY_RID_COOPERATIVE]["redundancy_by_surface"] == \
        EXPECTED_RID["redundancy_by_surface"]

    #: 360° 全向（不存在任何扇区/方位角字段）。
    for services, key in ((communication, SERVICE_KEY_COMMUNICATION),
                          (surveillance, SERVICE_KEY_RID_COOPERATIVE)):
        geometry = services[key]["geometry"]
        assert geometry["omnidirectional"] is True
        assert geometry["horizontal_coverage_deg"] == 360.0
        assert "panel_azimuth" not in geometry and "sector" not in geometry

    #: Navigation 必须携带 Round29-E 已确认的 endpoint contract。
    planning = navigation[SERVICE_KEY_NAVIGATION_INTEGRITY_MONITORING]["planning"]
    assert planning["placement_scope"] == "route_endpoints"
    assert planning["required_endpoint_roles"] == ["origin", "destination"]
    assert planning["required_monitor_per_endpoint"] == 1
    assert planning["require_distinct_physical_site_per_endpoint"] is True
    assert planning["corridor_monitoring_required"] is False
    assert planning["local_monitoring_radius_m"] == 10000.0
    assert "not_rtk_baseline" in planning["local_monitoring_radius_semantics"]

    #: Round31-C：Radar 携带分级规划 contract（优先 Radar-I；仅在严格证明不可行时
    #: 在既有站址上升级 Radar-II），且绝不放宽 range、绝不新建站址。
    radar_policy = surveillance[SERVICE_KEY_RADAR_NONCOOPERATIVE]["planning_policy"]
    assert radar_policy["allowed_radar_types"] == ["radar_i", "radar_ii"]
    assert radar_policy["orientation_optimization"] is True
    assert radar_policy["orientation_policy"] == "bearing_derived_critical_angles"
    assert radar_policy["allow_automatic_radar_ii_escalation"] is True
    assert radar_policy["allow_range_relaxation"] is False

    #: Radar 绝不被合并进 RID：两条服务各自独立。
    assert SERVICE_KEY_RADAR_NONCOOPERATIVE not in {
        SERVICE_KEY_RID_COOPERATIVE,
    }
    assert surveillance[SERVICE_KEY_RADAR_NONCOOPERATIVE]["service_key"] == (
        SERVICE_KEY_RADAR_NONCOOPERATIVE
    )

    #: 每个 route override 也必须携带（P14 按 route 解析 requirements）。
    override = recommended["route_overrides"]["R-2"]
    assert SERVICE_KEY_COMMUNICATION in override["communication"]["services"]
    assert SERVICE_KEY_NAVIGATION_INTEGRITY_MONITORING in override["navigation"]["services"]
    assert SERVICE_KEY_RID_COOPERATIVE in override["surveillance"]["services"]
    assert SERVICE_KEY_RADAR_NONCOOPERATIVE in override["surveillance"]["services"]

    #: 派生必须留下可审计 provenance。
    provenance = recommendation["field_provenance"]
    assert "project.communication.services" in provenance
    assert provenance["project.communication.services"][0]["derived"] is True
    for name in ("navigation", "surveillance"):
        record = provenance[f"project.{name}.services"][0]
        assert record["derived"] is True
        assert record["service_keys"]


def test_legacy_service_keys_never_derive_canonical_services(tmp_path):
    """legacy 身份（``N:navigation`` / ``S:surveillance``）绝不被升级成 canonical 服务。"""

    from cns_planner.domain.cns_service_contract import (
        SERVICE_KEY_NAVIGATION, SERVICE_KEY_SURVEILLANCE,
    )

    current = current_required_cns()
    current["project_default"]["navigation"]["service_key"] = SERVICE_KEY_NAVIGATION
    current["project_default"]["surveillance"]["service_key"] = SERVICE_KEY_SURVEILLANCE

    workflow = _workflow(tmp_path, current=current)
    recommendation = workflow.evaluate_required_cns_recommendation()["required_cns_recommendation"]
    defaults = recommendation["recommended_required_cns"]["project_default"]

    assert "services" not in defaults["navigation"]
    assert "services" not in defaults["surveillance"]
    #: C 的 legacy 身份就是 canonical 身份本身，因此照常派生。
    assert sorted(defaults["communication"]["services"]) == [SERVICE_KEY_COMMUNICATION]


def _derivation_requirement(**extra):
    requirement = {
        "confirmed": True, "required": True, "source": "synthetic_fixture",
        "coverage_requirement": 0.95, "accuracy_m": 10.0, "integrity": True,
        "redundancy": 1,
        "performance": {"max_horizontal_error_m": 10.0, "integrity_required": True,
                        "min_redundancy": 1},
    }
    requirement.update(extra)
    return requirement


def test_derivation_is_fail_closed_on_unconfirmed_or_not_required():
    """未确认 / 不要求 / 显式指向规则表外的服务时绝不派生。"""

    from cns_planner.domain.cns_service_registry import (
        derive_canonical_service_requirement,
    )

    for overrides in (
        {"confirmed": False},
        {"required": False},
        {"required": None},
        {"confirmed": None},
    ):
        assert derive_canonical_service_requirement(
            "N", _derivation_requirement(**overrides)
        ) is None

    #: 显式声明本规则表之外的 canonical 服务（RTK augmentation）⇒ 绝不擅自扩大服务集。
    from cns_planner.domain.cns_service_contract import (
        SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION,
    )

    assert derive_canonical_service_requirement(
        "N", _derivation_requirement(service_key=SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION)
    ) is None


def test_navigation_without_explicit_integrity_requirement_never_derives_monitor():
    """只要求水平精度、未显式要求完整性 ⇒ 绝不派生 integrity monitoring。"""

    from cns_planner.domain.cns_service_registry import (
        derive_canonical_service_requirement,
    )
    from cns_planner.domain.cns_service_contract import (
        SERVICE_KEY_NAVIGATION_INTEGRITY_MONITORING,
    )

    requirement = _derivation_requirement(
        integrity=None, performance={"max_horizontal_error_m": 10.0, "min_redundancy": 1},
    )
    assert derive_canonical_service_requirement("N", requirement) is None

    #: 任一显式完整性声明成立即可派生。
    for granted in (
        {"integrity": True},
        {"integrity": None,
         "performance": {"max_horizontal_error_m": 10.0, "integrity_required": True,
                         "min_redundancy": 1}},
    ):
        requirement = _derivation_requirement(integrity=None)
        requirement.update(granted)
        derived = derive_canonical_service_requirement("N", requirement)
        assert sorted(derived) == [SERVICE_KEY_NAVIGATION_INTEGRITY_MONITORING]
        assert derived[SERVICE_KEY_NAVIGATION_INTEGRITY_MONITORING]["planning"][
            "placement_scope"] == "route_endpoints"


# ---------------------------------------------------------------------------
# 2. adopt 原样/规范化写入 authoritative required_cns，且重开后仍在
# ---------------------------------------------------------------------------

def test_adopt_writes_authoritative_services_and_survives_reopen(tmp_path):
    workflow = _workflow(tmp_path, current=current_required_cns())
    workflow.evaluate_required_cns_recommendation()
    response = workflow.adopt_required_cns_recommendation({"source": "test_user"})

    assert response["required_cns_adoption"]["status"] == "adopted"
    authoritative = response["required_cns"]
    assert authoritative["source"] == "explicit_adoption_from_required_cns_recommendation"
    assert authoritative["project_default"]["communication"]["services"][SERVICE_KEY_COMMUNICATION][
        "radius_by_surface"] == EXPECTED_COMMUNICATION["radius_by_surface"]
    assert authoritative["project_default"]["surveillance"]["services"][SERVICE_KEY_RID_COOPERATIVE][
        "redundancy_by_surface"] == EXPECTED_RID["redundancy_by_surface"]

    #: snapshot（前端读取路径）必须携带同一份 services。
    snapshot = workflow.required_cns_snapshot()
    assert SERVICE_KEY_COMMUNICATION in snapshot["project_default"]["communication"]["services"]

    reopened = WorkflowService(tmp_path / "project.json", DEFAULTS)
    restored = reopened.state["required_cns"]
    assert restored["project_default"]["communication"]["services"] == \
        authoritative["project_default"]["communication"]["services"]
    assert restored["project_default"]["surveillance"]["services"] == \
        authoritative["project_default"]["surveillance"]["services"]
    assert restored["status"] == "passed"


# ---------------------------------------------------------------------------
# 3. P14/P15/P16 消费的是正式 services，不再回落 legacy 视图
# ---------------------------------------------------------------------------

def test_adopted_services_are_consumed_as_explicit_not_legacy_view(tmp_path):
    workflow = _workflow(tmp_path, current=current_required_cns())
    workflow.evaluate_required_cns_recommendation()
    authoritative = workflow.adopt_required_cns_recommendation()["required_cns"]

    expected = {
        "communication": [SERVICE_KEY_COMMUNICATION],
        "surveillance": [SERVICE_KEY_RADAR_NONCOOPERATIVE, SERVICE_KEY_RID_COOPERATIVE],
    }
    from cns_planner.domain.cns_service_contract import (
        SERVICE_KEY_NAVIGATION_INTEGRITY_MONITORING,
    )

    expected["navigation"] = [SERVICE_KEY_NAVIGATION_INTEGRITY_MONITORING]
    for name, keys in expected.items():
        code = {"communication": "C", "navigation": "N", "surveillance": "S"}[name]
        requirement = authoritative["project_default"][name]
        #: P14 走 service-aware 分支的唯一条件：``services`` 存在且能被规范化。
        assert normalize_service_requirements(code, requirement) is not None
        views = required_services_for(code, requirement)
        assert sorted(item["service_key"] for item in views) == sorted(keys)
        assert all(item.get("legacy_compatible_view") is not True for item in views)

    #: 有 surface policy 的服务必须自带冻结几何，否则下游只能拿到 unknown。
    for name, key in (("communication", SERVICE_KEY_COMMUNICATION),
                      ("surveillance", SERVICE_KEY_RID_COOPERATIVE)):
        views = required_services_for(
            {"communication": "C", "surveillance": "S"}[name],
            authoritative["project_default"][name],
        )
        entry = next(item for item in views if item["service_key"] == key)
        assert entry["radius_by_surface"] == (
            EXPECTED_COMMUNICATION if key == SERVICE_KEY_COMMUNICATION else EXPECTED_RID
        )["radius_by_surface"]
        assert entry["redundancy_by_surface"] == (
            EXPECTED_COMMUNICATION if key == SERVICE_KEY_COMMUNICATION else EXPECTED_RID
        )["redundancy_by_surface"]

    #: adapter 服务（Navigation integrity / Radar）必须自带 canonical 契约。
    navigation = next(
        item for item in required_services_for(
            "N", authoritative["project_default"]["navigation"],
        ) if item["service_key"] == SERVICE_KEY_NAVIGATION_INTEGRITY_MONITORING
    )
    assert navigation["planning"]["placement_scope"] == "route_endpoints"
    radar = next(
        item for item in required_services_for(
            "S", authoritative["project_default"]["surveillance"],
        ) if item["service_key"] == SERVICE_KEY_RADAR_NONCOOPERATIVE
    )
    #: Round31-C 分级规划：Radar-II 是"证明不可行后的既有站址升级方案"，
    #: 与 RID cooperative 仍是两条互不替代的服务。
    assert radar["planning_policy"]["allowed_radar_types"] == ["radar_i", "radar_ii"]
    assert radar["planning_policy"]["allow_automatic_radar_ii_escalation"] is True


def test_legacy_surveillance_requirement_never_derives_rid_services():
    """legacy ``S:surveillance`` 绝不被升级成 RID（也不产生任何 services）。"""

    requirement = _confirmed_requirement(
        performance={"max_update_interval_s": 2.0, "min_redundancy": 1},
        service_key=SERVICE_KEY_SURVEILLANCE,
    )
    services, provenance = canonicalize_requirement_services("S", requirement)
    assert services is None and provenance is None


def test_unconfirmed_requirement_never_derives_services():
    requirement = _confirmed_requirement(service_key=SERVICE_KEY_COMMUNICATION)
    requirement["confirmed"] = False
    services, provenance = canonicalize_requirement_services("C", requirement)
    assert services is None and provenance is None

    requirement["confirmed"] = True
    requirement["required"] = False
    services, provenance = canonicalize_requirement_services("C", requirement)
    assert services is None and provenance is None


def test_explicit_services_win_over_derivation():
    """显式 ``services`` 优先：派生绝不覆盖调用方显式声明的 canonical 值。"""

    requirement = _confirmed_requirement(
        service_key=SERVICE_KEY_COMMUNICATION,
        services={SERVICE_KEY_COMMUNICATION: {
            "service_key": SERVICE_KEY_COMMUNICATION, "required": True, "confirmed": True,
            "source": "explicit_declaration", "coverage_requirement": 0.9,
            "radius_by_surface": {"land": 3500.0, "sea": 3500.0, "coastal_uncertain": 3500.0},
            "redundancy_by_surface": {"land": 3, "sea": 1, "coastal_uncertain": 3},
        }},
    )
    services, provenance = canonicalize_requirement_services("C", requirement)
    assert provenance["derived"] is False
    assert services[SERVICE_KEY_COMMUNICATION]["radius_by_surface"]["land"] == 3500.0

    normalized = normalize_required_cns({"project_default": {"communication": requirement}})
    entry = normalized["project_default"]["communication"]["services"][SERVICE_KEY_COMMUNICATION]
    assert entry["radius_by_surface"]["land"] == 3500.0
    assert entry["redundancy_by_surface"] == {"land": 3, "sea": 1, "coastal_uncertain": 3}


def test_services_survive_normalize_round_trip_byte_for_byte(tmp_path):
    workflow = _workflow(tmp_path, current=current_required_cns())
    workflow.evaluate_required_cns_recommendation()
    authoritative = workflow.adopt_required_cns_recommendation()["required_cns"]

    assert normalize_required_cns(authoritative) == authoritative


def test_adopt_requires_a_recommendation_that_carries_services(tmp_path):
    """recommendation 未跑过时仍然拒绝 adopt（既有门禁不得放宽）。"""

    workflow = _workflow(tmp_path, current=current_required_cns())
    with pytest.raises(ValueError):
        workflow.adopt_required_cns_recommendation()
