"""BUG-WORKSPACE-CLEAR-001/002：清工作区不得留下悬空的 layered planning request。

清工作区确实会删除 ``nodes`` / ``scenario_routes`` / ``operational_routes``（既有业务语义不改变），
但显式 ``layered_route_planning_request`` 过去被原样保留，继续指向已删除的 scenario route，
于是 readiness 只报 ``scenario_route_not_found``（一个悬空引用），而不是如实报告
"规划请求尚未配置"。本文件锁定修复后的语义：

* request 被重置为 ``default_layered_route_request()``，不保留任何 route / node / 高度层引用；
* readiness 报 ``planning_request_not_confirmed``，不再出现 ``scenario_route_not_found``；
* 依赖它的 layered 候选 / RouteRiskProfile / layered validations / operational adoptions
  按既有 invalidation 语义被标 stale，绝不伪装成 current。
"""

from pathlib import Path

from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.layered_route import default_layered_route_request

DEFAULTS = Path("cns_planner/config/defaults.json")


def project(tmp_path, name="clear-workspace.json"):
    service = WorkflowService(tmp_path / name, DEFAULTS)
    service.state["nodes"] = [
        {"node_id": "N001", "name": "起点"},
        {"node_id": "N002", "name": "终点"},
    ]
    service.state["scenario_routes"] = [{
        "route_id": "R0001", "start_node_id": "N001", "end_node_id": "N002",
        "start": [122.0, 30.0], "end": [122.01, 30.01],
    }]
    service.state["operational_routes"] = [{
        "route_id": "R0001", "status": "passed", "kind": "layered_risk_aware_operational_route",
        "path": [[122.0, 30.0], [122.01, 30.01]],
    }]
    service.state["spatial_3d"]["altitude_layers"] = [{
        "altitude_layer_id": "L8-LOW", "name": "低层", "nominal_altitude_m": 300.0,
        "lower_altitude_m": 250.0, "upper_altitude_m": 350.0,
        "vertical_reference": "egm2008_orthometric", "source": "工程确认-测试",
        "confirmed": True, "status": "confirmed",
    }]
    # 依赖链上的四类结果：清工作区后全部必须被标 stale（既有 invalidation 语义）。
    service.state["layered_route_candidates"] = {
        "status": "passed", "count": 1,
        "items": [{"candidate_id": "LRC-TEST", "status": "candidate"}],
        "masks": {"R0001|L8-LOW": {"status": "passed"}},
    }
    service.state["route_risk_profiles"] = {
        "status": "passed", "count": 1,
        "items": [{"profile_id": "RRP-TEST", "status": "passed",
                   "current_applicability": "current"}],
    }
    service.state["layered_route_validations"] = {
        "status": "passed", "count": 1,
        "items": [{"validation_id": "LRV-TEST", "status": "validated_candidate",
                   "current_applicability": "current"}],
    }
    service.state["layered_operational_adoptions"] = {
        "status": "passed", "count": 1,
        "items": [{"adoption_id": "LRA-TEST", "validation_id": "LRV-TEST",
                   "route_id": "R0001", "status": "passed",
                   "current_applicability": "current"}],
    }
    service.set_layered_route_planning_request({
        "scenario_route_id": "R0001", "altitude_layer_id": "L8-LOW",
        "source": "工程确认-测试", "confirmed": True,
    })
    service.session.save()
    return service


def blocker_codes(readiness):
    return [str(item.get("reason_code")) for item in readiness.get("blockers") or []]


def test_confirmed_request_pointing_at_a_real_route_has_no_not_found_blocker(tmp_path):
    service = project(tmp_path)
    request = service.layered_route_planning_request()
    assert request["status"] == "confirmed"
    assert request["scenario_route_id"] == "R0001"
    codes = blocker_codes(service.layered_route_planner_readiness())
    assert "scenario_route_not_found" not in codes


def test_clear_workspace_resets_the_request_and_leaves_no_dangling_reference(tmp_path):
    service = project(tmp_path)

    cleared = service.clear_workspace()

    request = cleared["layered_route_planning_request"]
    assert request == default_layered_route_request()
    assert request["scenario_route_id"] is None
    assert request["start_node_id"] is None and request["end_node_id"] is None
    assert request["altitude_layer_id"] is None
    assert request["confirmed"] is False
    assert request["status"] == "pending_confirmation"
    # 既有业务语义不变：项目节点 / 场景航路 / 运行航路确实被删除。
    assert cleared["nodes"] == []
    assert cleared["scenario_routes"] == []
    assert cleared["operational_routes"] == []
    assert cleared["workspace"] is None and cleared["grid"] is None


def test_after_clear_readiness_reports_request_not_configured_not_route_not_found(tmp_path):
    service = project(tmp_path)

    cleared = service.clear_workspace()
    codes = blocker_codes(cleared["layered_route_planner_readiness"])

    # "request not configured" 类状态：默认 request 的 status_reason 是
    # route_identity_not_selected（没有任何 scenario route / OD 被选中）。
    assert cleared["layered_route_planning_request"]["status_reason"] == (
        "route_identity_not_selected"
    )
    assert "route_identity_not_selected" in codes
    assert "planning_request_not_confirmed" not in codes
    assert "scenario_route_not_found" not in codes


def test_after_clear_layered_dependents_are_stale_never_current(tmp_path):
    service = project(tmp_path)

    cleared = service.clear_workspace()

    candidates = cleared["layered_route_candidates"]
    assert candidates["status"] == "stale"
    assert candidates["stale_reason"] == "workspace_cleared"
    assert cleared["result_statuses"]["layered_route_candidate"] == "stale"

    profiles = cleared["route_risk_profiles"]
    assert profiles["items"][0]["status"] == "stale"
    assert profiles["items"][0].get("current_applicability") != "current"

    validations = cleared["layered_route_validations"]
    assert validations["items"][0]["status"] == "stale"
    assert validations["items"][0]["current_applicability"] == "stale"

    adoptions = cleared["layered_operational_adoptions"]
    assert adoptions["items"][0]["status"] == "stale"
    assert adoptions["items"][0]["current_applicability"] == "stale"


def test_reset_request_survives_a_reopen(tmp_path):
    service = project(tmp_path)

    service.clear_workspace()
    restored = WorkflowService(tmp_path / "clear-workspace.json", DEFAULTS)

    request = restored.layered_route_planning_request()
    assert request == default_layered_route_request()
    assert restored.state["scenario_routes"] == []
    assert restored.state["nodes"] == []
    assert "scenario_route_not_found" not in blocker_codes(
        restored.layered_route_planner_readiness()
    )
