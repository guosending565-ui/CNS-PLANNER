"""Round 29-H —— recommendation → adopt 四服务闭环专项回归。

裁定（A / C）：

* 已确认的工程要求必须能通过**正式** recommendation → adopt 链得到完整的四服务结构：
  ``C:communication`` / ``N:navigation_integrity_monitoring`` /
  ``S:rid_cooperative`` / ``S:radar_noncooperative``；
* Surveillance 是 ``service_requirement_mode = all_required``；
* recommendation 的 ``recommended_required_cns`` 与直接 canonical 保存后的 normalize
  结果必须**逐字段一致**；
* 不再需要 Round29-F 的 direct-save fallback（``round29f_formal_four_service_required_cns``）；
* 绝不从机载能力推断服务，绝不把 legacy 身份升级成 canonical 服务。
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from cns_planner.domain.cns_inputs import normalize_required_cns, pending_required_cns
from cns_planner.domain.cns_service_contract import (
    SERVICE_KEY_COMMUNICATION, SERVICE_KEY_NAVIGATION_INTEGRITY_MONITORING,
    SERVICE_KEY_RADAR_NONCOOPERATIVE, SERVICE_KEY_RID_COOPERATIVE,
)
from cns_planner.domain.cns_service_registry import (
    endpoint_scope_only_subsystems, normalize_service_requirements, required_services_for,
)

from test_required_cns_adoption_services import _workflow


EXPECTED = {
    "communication": [SERVICE_KEY_COMMUNICATION],
    "navigation": [SERVICE_KEY_NAVIGATION_INTEGRITY_MONITORING],
    "surveillance": [SERVICE_KEY_RADAR_NONCOOPERATIVE, SERVICE_KEY_RID_COOPERATIVE],
}


def test_new_project_recommendation_yields_the_four_service_structure(tmp_path):
    """新项目（无任何显式 service identity）也能从已确认要求得到四服务。"""

    workflow = _workflow(tmp_path, current=pending_required_cns())
    recommendation = workflow.evaluate_required_cns_recommendation()[
        "required_cns_recommendation"
    ]
    assert recommendation["status"] == "recommendation_ready"
    recommended = recommendation["recommended_required_cns"]

    for name, keys in EXPECTED.items():
        defaults = recommended["project_default"][name]
        assert sorted(defaults["services"]) == sorted(keys), name
        assert defaults["status"] == "passed", name
    assert recommended["project_default"]["surveillance"][
        "service_requirement_mode"] == "all_required"
    #: 每个 route override 同样携带四服务。
    override = recommended["route_overrides"]["R-2"]
    for name, keys in EXPECTED.items():
        assert sorted(override[name]["services"]) == sorted(keys), name


def test_recommendation_matches_a_direct_canonical_normalize(tmp_path):
    """recommendation 的输出与直接 canonical required-cns 保存后的 normalize 结果一致。"""

    workflow = _workflow(tmp_path, current=pending_required_cns())
    recommended = workflow.evaluate_required_cns_recommendation()[
        "required_cns_recommendation"
    ]["recommended_required_cns"]

    normalized = normalize_required_cns(recommended)
    assert normalized == recommended, "recommended_required_cns 必须已是 canonical 形状"
    assert normalize_required_cns(normalized) == normalized, "normalize 必须幂等"


def test_adopt_closure_produces_and_persists_the_four_services(tmp_path):
    """recommendation → adopt 闭环：authoritative required_cns 携带四服务并可重开。"""

    workflow = _workflow(tmp_path, current=pending_required_cns())
    workflow.evaluate_required_cns_recommendation()
    response = workflow.adopt_required_cns_recommendation({"source": "round29h_test"})

    assert response["required_cns_adoption"]["status"] == "adopted"
    authoritative = response["required_cns"]
    assert authoritative["source"] == "explicit_adoption_from_required_cns_recommendation"
    assert normalize_required_cns(authoritative) == authoritative

    for name, keys in EXPECTED.items():
        entry = authoritative["project_default"][name]
        assert sorted(entry["services"]) == sorted(keys), name
        code = {"communication": "C", "navigation": "N", "surveillance": "S"}[name]
        views = required_services_for(code, entry)
        assert sorted(item["service_key"] for item in views) == sorted(keys)
        assert all(item.get("legacy_compatible_view") is not True for item in views)

    #: 重开项目后仍然存在。
    from cns_planner.application.workflow_service import WorkflowService

    reopened = WorkflowService(tmp_path / "project.json", _defaults())
    restored = reopened.state["required_cns"]
    for name, keys in EXPECTED.items():
        assert sorted(restored["project_default"][name]["services"]) == sorted(keys), name


def test_adopted_four_services_drive_endpoint_scope_dispatch(tmp_path):
    """adopt 后的 N 服务必须真正驱动 P14/P15 的 endpoint 作用域 dispatch。"""

    workflow = _workflow(tmp_path, current=pending_required_cns())
    workflow.evaluate_required_cns_recommendation()
    authoritative = workflow.adopt_required_cns_recommendation()["required_cns"]
    requirements = authoritative["project_default"]

    assert endpoint_scope_only_subsystems(requirements) == frozenset({"N"})
    assert normalize_service_requirements(
        "N", requirements["navigation"],
    ) is not None
    #: Surveillance 有两条服务，且都不在 endpoint 作用域 ⇒ S 不在 dispatch 集合里。
    assert "S" not in endpoint_scope_only_subsystems(requirements)
    assert sorted(normalize_service_requirements(
        "S", requirements["surveillance"],
    )) == sorted(EXPECTED["surveillance"])


def test_no_direct_save_fallback_marker_is_required(tmp_path):
    """不再需要 Round29-F 的 direct-save fallback：source 由 adopt 正式写入。"""

    workflow = _workflow(tmp_path, current=pending_required_cns())
    workflow.evaluate_required_cns_recommendation()
    authoritative = workflow.adopt_required_cns_recommendation()["required_cns"]
    assert authoritative["source"] == "explicit_adoption_from_required_cns_recommendation"
    assert authoritative["source"] != "round29f_formal_four_service_required_cns"


def test_aircraft_capability_never_invents_services(tmp_path):
    """机载能力绝不参与服务派生：即使机载声明 RTK/GNSS，也不产生额外服务。"""

    workflow = _workflow(tmp_path, current=pending_required_cns())
    workflow.state["aircraft_profiles"] = {
        "status": "passed",
        "items": [{
            "aircraft_id": "RTK-1", "name": "RTK-1",
            "navigation": {
                "confirmed": True, "status": "confirmed",
                "type": {"technology": "gnss_rtk"}, "capabilities": ["pnt", "rtk"],
                "performance": {"max_horizontal_error_m": 0.1, "min_redundancy": 1},
            },
            "communication": {}, "surveillance": {},
        }],
    }
    workflow.state["selected_aircraft_profile_id"] = "RTK-1"

    recommended = workflow.evaluate_required_cns_recommendation()[
        "required_cns_recommendation"
    ]["recommended_required_cns"]
    navigation = recommended["project_default"]["navigation"]["services"]
    assert sorted(navigation) == [SERVICE_KEY_NAVIGATION_INTEGRITY_MONITORING]
    #: 机载 gnss_rtk 绝不产生地面 RTK augmentation 要求。
    assert "N:rtk_augmentation" not in navigation


def _defaults():
    from pathlib import Path

    return Path(__file__).resolve().parents[1] / "cns_planner" / "config" / "defaults.json"
