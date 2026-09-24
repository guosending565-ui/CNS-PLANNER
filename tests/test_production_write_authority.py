"""Phase4-B2B-1 Production Write Authority contract tests.

覆盖本轮验收要求：

1. ``LayeredOperationalAdoptionService`` 可以写 ``operational_routes``；
2. ``RouteService.generate_operational`` 对 V1/V2 都不改变 canonical ``operational_routes``；
3. V3-D Apply 不写 ``operational_routes``；
4. ``RequirementRecommendationService.evaluate`` 永不写 ``required_cns``；
5. recommendation Adopt 由 ``CNSInputService`` 命令 owner 落盘；
6. manual direct ``required_cns`` 会正确处理此前 adoption 的 current/stale 语义；
7. ``ClosedLoopService`` 不直接写 confirmed plan；
8. Select / Confirm / Apply 仍然彼此分离；
9. Router 不直接写 canonical state；
10. ``WRITE_ALLOWLIST`` 覆盖六类 canonical result，每类恰好一个 production owner；
11. 正式 route publish/revoke 后 ``required_cns_recommendation`` 与
    ``radar_surveillance_layout`` 正确 stale；
12. compatibility operational result 不能使 canonical ``operational_route`` 节点
    变 authoritative/completed；
13. 旧项目已有 ``operational_routes`` 仍可读取/导出。
"""

from copy import deepcopy
import inspect
import json
from pathlib import Path
import re

import pytest

from cns_planner.application.layered_operational_adoption_service import (
    LayeredOperationalAdoptionService,
)
from cns_planner.application.production_write_authority import (
    DERIVED_REVOKE_ALLOWLIST, ProductionWriteAuthorityError, WRITE_ALLOWLIST,
    assert_write_authority, canonical_operational_routes, is_authorized_writer,
    runtime_compatibility_operational_routes, runtime_compatibility_result,
)
from cns_planner.application.route_service import RouteService
from cns_planner.application.v3_operational_adoption_service import (
    V3OperationalAdoptionService,
)
from cns_planner.domain.plan_review import review_baseline_fingerprint
from cns_planner.services.workflow import WorkflowService

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = ROOT / "cns_planner" / "config" / "defaults.json"


def health():
    return {
        "status": "passed",
        "population": {"status": "passed"},
        "airspace": {"status": "passed"},
        "terrain": {"status": "missing_data"},
        "buildings": {"status": "missing_data"},
        "property": {"status": "missing_data"},
        "loaded_layer_count": 31,
        "covered_layer_count": 4,
    }


@pytest.fixture
def workflow(tmp_path):
    defaults = tmp_path / "defaults.json"
    defaults.write_text(DEFAULTS.read_text(encoding="utf-8"), encoding="utf-8")
    service = WorkflowService(tmp_path / "project.json", defaults)
    service.set_project({"name": "B2B-1 authority"})
    service.set_workspace([120.0, 30.0, 120.01, 30.01], health())
    service.add_node([120.002, 30.002], "A")
    service.add_node([120.008, 30.008], "B")
    service.generate_scenario("both")
    return service


def module_source(target):
    if isinstance(target, str):
        return (ROOT / target).read_text(encoding="utf-8")
    return Path(inspect.getfile(target)).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. operational_routes：唯一 production content owner
# ---------------------------------------------------------------------------


def test_layered_adoption_service_is_the_only_operational_routes_writer(workflow):
    service = workflow.layered_operational_adoption_service
    assert is_authorized_writer(service, "operational_routes")
    assert WRITE_ALLOWLIST["operational_routes"] == ("LayeredOperationalAdoptionService",)
    assert_write_authority(service, "operational_routes")
    # 非 owner 写 canonical 必须被拒绝。
    for forbidden in (RouteService, V3OperationalAdoptionService):
        with pytest.raises(ProductionWriteAuthorityError):
            assert_write_authority(forbidden, "operational_routes")
    source = module_source(LayeredOperationalAdoptionService)
    assert 'assert_write_authority(self, "operational_routes")' in source


def test_route_service_operational_is_compatibility_only(workflow):
    state = workflow.state
    state["operational_routes"] = [
        {"route_id": "CANONICAL-1", "status": "passed", "path": [[120.002, 30.002], [120.008, 30.008]]}
    ]
    before = deepcopy(state["operational_routes"])
    response = workflow.generate_operational([])
    assert state["operational_routes"] == before
    record = runtime_compatibility_result(workflow.session, "operational_routes")
    assert record["items"]
    assert response["compatibility_operational_routes"]["authoritative"] is False
    assert response["authoritative_operational_routes_unchanged"] is True
    assert "compatibility_operational_routes" not in workflow.snapshot()
    assert "results" not in (state.get("compatibility") or {})


def test_route_service_v2_also_cannot_change_canonical_routes(workflow):
    state = workflow.state
    workflow.select_algorithm({
        "algorithm_type": "route_planner", "algorithm_id": "risk_aware_route_planner_v2",
        "version": "2.0", "parameters": {"risk_weight_lambda": 0},
    })
    before = deepcopy(state["operational_routes"])
    try:
        workflow.generate_operational([])
    except ValueError:
        # Fail-closed on missing canonical grid risk is acceptable: the authority
        # contract only requires that canonical operational_routes is untouched.
        pass
    assert state["operational_routes"] == before


def test_v3_operational_adoption_cannot_write_canonical_routes(workflow):
    source = module_source(V3OperationalAdoptionService)
    assert not re.search(r'state\["operational_routes"\]\s*=', source)
    state = workflow.state
    state["operational_routes"] = [{"route_id": "LEGACY-KEEP", "status": "passed", "path": []}]
    before = deepcopy(state["operational_routes"])
    service = workflow.v3_operational_adoption_service
    with pytest.raises(ValueError):
        service.apply({"confirmed": True})
    assert state["operational_routes"] == before
    assert runtime_compatibility_result(workflow.session, "v3_operational_routes") == {}


# ---------------------------------------------------------------------------
# required_cns：唯一 command owner
# ---------------------------------------------------------------------------


def recommendation_context(**values):
    return {"project_default": {
        name: {"value": value, "source": "synthetic_fixture", "confirmed": True}
        for name, value in values.items()
    }}


def recommendation_policy():
    return {
        "policy_id": "P-B2B1", "version": "1.0", "name": "P-B2B1",
        "source_type": "project_rule", "source": "synthetic_fixture",
        "reference": "TEST", "clause": "1", "confirmed": True,
        "applicability": {"all_of": [{"field": "operation_mode", "operator": "eq", "value": "bvlos"}]},
        "requirements": {
            "communication": {
                "required": True, "coverage_requirement": 0.95, "max_gap_m": 1000,
                "performance": {"max_latency_s": 1.0, "min_redundancy": 1},
            },
            "navigation": {"required": False},
            "surveillance": {"required": False},
        },
    }


def prepare_recommendation(workflow):
    workflow.select_algorithm({
        "algorithm_type": "requirement_model",
        "algorithm_id": "operational_context_required_cns_v2",
        "version": "2.0", "parameters": {},
    })
    workflow.set_cns_operation_context(recommendation_context(operation_mode="bvlos"))
    workflow.set_cns_requirement_policies({"items": [recommendation_policy()]})
    return workflow.evaluate_required_cns_recommendation({})


def test_recommendation_evaluate_never_writes_required_cns(workflow):
    workflow.select_algorithm({
        "algorithm_type": "requirement_model",
        "algorithm_id": "operational_context_required_cns_v2",
        "version": "2.0", "parameters": {},
    })
    workflow.set_cns_operation_context(recommendation_context(operation_mode="bvlos"))
    workflow.set_cns_requirement_policies({"items": [recommendation_policy()]})
    before = deepcopy(workflow.state["required_cns"])
    result = workflow.evaluate_required_cns_recommendation({})["required_cns_recommendation"]
    assert result["status"] == "recommendation_ready"
    assert workflow.state["required_cns"] == before
    source = module_source("cns_planner/application/requirement_recommendation_service.py")
    assert not re.search(r'state\["required_cns"\]\s*=', source)


def test_recommendation_adopt_is_written_by_the_command_owner(workflow):
    prepare_recommendation(workflow)
    workflow.adopt_required_cns_recommendation({})
    state = workflow.state
    assert state["required_cns"]["source"] == "explicit_adoption_from_required_cns_recommendation"
    adoption = state["required_cns_adoption"]
    assert adoption["status"] == "adopted"
    assert adoption["recommendation_fingerprint"]
    assert adoption["required_cns_fingerprint"]
    source = module_source("cns_planner/application/cns_input_service.py")
    assert 'assert_write_authority(self, "required_cns")' in source


def test_manual_required_cns_supersedes_a_previous_adoption(workflow):
    prepare_recommendation(workflow)
    workflow.adopt_required_cns_recommendation({})
    assert workflow.required_cns_recommendation_snapshot()["adoption_status"] == "adopted_current"

    workflow.set_required_cns({
        "scope": "project",
        "requirements": {"communication": {"required": True}},
        "source": "用户配置",
    })
    adoption = workflow.state["required_cns_adoption"]
    assert adoption["status"] == "superseded"
    assert adoption["previous_status"] == "adopted"
    assert adoption["superseded_by"] == "manual_direct_required_cns"
    snapshot = workflow.required_cns_recommendation_snapshot()
    assert snapshot["adoption_status"] == "superseded"
    assert snapshot["adoption_status"] != "adopted_current"


# ---------------------------------------------------------------------------
# confirmed plan：ClosedLoop 无直接写权；Select / Confirm / Apply 分离
# ---------------------------------------------------------------------------


def test_closed_loop_never_writes_the_confirmed_plan_directly():
    source = module_source("cns_planner/application/closed_loop_service.py")
    assert not re.search(r'\["confirmed_cns_plan"\]\s*=', source)
    assert not re.search(r'current_applicability"\]\s*=', source)
    assert "cns_plan_review(reason)" in source


def seed_review(workflow):
    state = workflow.state
    review = {
        "review_id": "PR-B2B1", "status": "current", "model_scope": "engineering_review",
        "baseline_fingerprint": review_baseline_fingerprint(state),
        "selected_variant_id": "V-BASE",
        "variants": [
            {
                "variant_id": "V-BASE", "name": "baseline", "status": "evaluated",
                "selected_action_ids": [],
                "evaluation": {
                    "status": "evaluated", "actions": [],
                    "confirmation_gate": {"status": "ready_for_confirmation"},
                    "planned_p14_fingerprint": None, "planned_p15_fingerprint": None,
                },
            },
            {
                "variant_id": "V-ALT", "name": "alternative", "status": "evaluated",
                "selected_action_ids": [],
                "evaluation": {
                    "status": "evaluated", "actions": [],
                    "confirmation_gate": {"status": "ready_for_confirmation"},
                    "planned_p14_fingerprint": None, "planned_p15_fingerprint": None,
                },
            },
        ],
    }
    state["cns_plan_review"] = review
    return review


def test_select_confirm_apply_stay_separate(workflow):
    seed_review(workflow)
    state = workflow.state
    before_plan = deepcopy(state.get("confirmed_cns_plan"))

    workflow.select_cns_plan_variant({"variant_id": "V-ALT"})
    assert state["cns_plan_review"]["selected_variant_id"] == "V-ALT"
    # Select 只选择方案，绝不产生 confirmed plan。
    assert state.get("confirmed_cns_plan") == before_plan

    workflow.confirm_cns_plan({"variant_id": "V-BASE"})
    confirmed = state["confirmed_cns_plan"]
    assert confirmed["status"] == "confirmed"
    assert confirmed["variant_id"] == "V-BASE"
    # Confirm 不改变 Select 的结果。
    assert state["cns_plan_review"]["selected_variant_id"] == "V-ALT"
    plan_id = confirmed["plan_id"]

    identity = {
        key: deepcopy(confirmed.get(key))
        for key in ("plan_id", "variant_id", "confirmed_actions", "source_fingerprints", "history")
    }
    # Apply 必须指定当前 confirmed plan_id；指纹不匹配时拒绝，且绝不改写 plan identity。
    workflow.apply_confirmed_cns_plan({"plan_id": "CP-NOT-THE-CURRENT-PLAN"})
    assert state["confirmed_cns_plan"]["plan_id"] == plan_id
    workflow.apply_confirmed_cns_plan({"plan_id": plan_id})
    assert state["confirmed_cns_plan"]["plan_id"] == plan_id
    for key, value in identity.items():
        assert state["confirmed_cns_plan"].get(key) == value


# ---------------------------------------------------------------------------
# Router / allowlist
# ---------------------------------------------------------------------------


def test_router_has_no_direct_canonical_state_write():
    source = module_source("cns_planner/api/router.py")
    assert not re.search(r'\bstate\[[^\]]*\]\s*=', source)
    assert "invalidation." not in source


def test_write_allowlist_covers_six_canonical_results_with_one_owner_each():
    expected = {
        "operational_routes": "LayeredOperationalAdoptionService",
        "required_cns": "CNSInputService",
        "coverage_3d": "Spatial3DService",
        "cns_corridor_site_plan": "CorridorSitePlanningService",
        "confirmed_cns_plan": "PlanReviewService",
        "radar_surveillance_layout": "RadarSurveillanceLayoutService",
    }
    assert set(WRITE_ALLOWLIST) == set(expected)
    for key, owner in expected.items():
        assert WRITE_ALLOWLIST[key] == (owner,), key
    # 派生回收白名单只登记清空/删除语义，且不引入第二个 production writer。
    assert DERIVED_REVOKE_ALLOWLIST["operational_routes"] == (
        "RouteService", "LayeredOperationalAdoptionService",
    )


# ---------------------------------------------------------------------------
# invalidation 最小补边
# ---------------------------------------------------------------------------


def test_route_publish_stales_requirement_recommendation_and_radar(workflow):
    state = workflow.state
    state["required_cns_recommendation"] = {
        "status": "recommendation_ready", "algorithm_id": "operational_context_required_cns_v2",
    }
    state.setdefault("result_statuses", {})["required_cns_recommendation"] = "passed"
    state["radar_surveillance_layout"] = {
        "status": "passed", "items": [{"route_id": "R0001", "status": "passed"}],
    }
    state.setdefault("result_statuses", {})["radar_surveillance_layout"] = "passed"

    workflow.invalidation_service.operational_route_published(
        {"R0001"}, reason="b2b1_publish_test",
    )
    assert state["result_statuses"]["required_cns_recommendation"] == "stale"
    assert state["required_cns_recommendation"]["status"] == "stale"
    assert state["radar_surveillance_layout"]["items"][0]["status"] == "stale"
    assert state["result_statuses"]["radar_surveillance_layout"] == "stale"


def test_route_revoke_uses_the_same_publication_edges(workflow):
    state = workflow.state
    state["required_cns_recommendation"] = {"status": "recommendation_ready"}
    state.setdefault("result_statuses", {})["required_cns_recommendation"] = "passed"
    state["radar_surveillance_layout"] = {
        "status": "passed", "items": [{"route_id": "R0001", "status": "passed"}],
    }
    state.setdefault("result_statuses", {})["radar_surveillance_layout"] = "passed"

    workflow.invalidation_service.operational_route_published(
        {"R0001"}, reason="b2b1_revoke_test", preserve_published_routes=False,
    )
    assert state["required_cns_recommendation"]["status"] == "stale"
    assert state["radar_surveillance_layout"]["items"][0]["status"] == "stale"


# ---------------------------------------------------------------------------
# compatibility result 的 authority 边界 + 旧项目兼容读取
# ---------------------------------------------------------------------------


def test_compatibility_result_never_completes_the_canonical_node(workflow):
    state = workflow.state
    response = workflow.generate_operational([])
    assert response["compatibility_write"] is True
    assert canonical_operational_routes(state) == []
    assert state["result_statuses"].get("routes") != "passed"
    assert workflow._steps()["3"] is False

    items = runtime_compatibility_operational_routes(workflow.session)
    assert items
    record = runtime_compatibility_result(workflow.session, "operational_routes")
    assert record["authoritative"] is False
    assert record["compatibility"] is True
    assert record["deprecated"] is True
    assert record["source_algorithm"]["algorithm_id"] == "route_planner_v1"
    assert record["consumed_by_canonical_workflow"] is False
    assert all(item["status"] == "passed" for item in items)


def test_new_compatibility_routes_never_enter_save_reload_or_snapshot(workflow):
    response = workflow.generate_operational([])
    assert response["compatibility_operational_routes"]["items"]
    assert "results" not in (workflow.state.get("compatibility") or {})
    snapshot = workflow.snapshot()
    assert "compatibility_operational_routes" not in snapshot
    assert "results" not in (snapshot.get("compatibility") or {})

    workflow.save()
    document = json.loads(Path(workflow.store_path).read_text(encoding="utf-8"))
    assert "results" not in (document.get("compatibility") or {})
    reopened = WorkflowService(workflow.store_path, workflow.defaults_path)
    assert "results" not in (reopened.state.get("compatibility") or {})
    assert runtime_compatibility_result(reopened.session, "operational_routes") == {}


def test_compatibility_routes_never_feed_canonical_consumers(workflow):
    state = workflow.state
    keys = (
        "required_cns", "coverage_3d", "cns_service_capability",
        "cns_corridor_assessment", "cns_corridor_site_plan",
        "radar_surveillance_layout", "cns_planning_report",
    )
    before = {key: deepcopy(state.get(key)) for key in keys}
    workflow.generate_operational([])
    assert canonical_operational_routes(state) == []
    for key in keys:
        assert state.get(key) == before[key], key


def test_legacy_project_operational_routes_stay_readable_and_exportable(workflow):
    state = workflow.state
    legacy = {
        "route_id": "R0001", "status": "passed", "algorithm_id": "route_planner_v1",
        "path": [[120.002, 30.002], [120.008, 30.008]],
    }
    state["operational_routes"] = [deepcopy(legacy)]
    workflow.save()

    workflow.generate_operational([])
    # guard 绝不删除旧项目已有的 canonical 运行航路。
    assert state["operational_routes"] == [legacy]
    exported = json.loads(workflow.export_routes().decode("utf-8"))
    assert [item["properties"]["route_id"] for item in exported["features"]] == ["R0001"]
    assert exported["features"][0]["properties"]["status"] == "passed"
    assert "compatibility" not in exported["features"][0]["properties"]
