import json

from cns_planner.algorithms.route_planner import RoutePlannerV1
from cns_planner.application.production_write_authority import runtime_compatibility_result
from cns_planner.services.workflow import WorkflowService


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


def test_complete_workflow_persists_exports_and_never_reuses_route_ids(tmp_path):
    store = tmp_path / "current_project.json"
    defaults = tmp_path / "defaults.json"
    defaults.write_text(
        open("cns_planner/config/defaults.json", encoding="utf-8").read(),
        encoding="utf-8",
    )
    service = WorkflowService(store, defaults)
    service.set_project({"name": "端到端测试"})
    # GRID-L8-UNIFICATION：正式工作区网格恒为 canonical L8，且默认资源上限为 12000 格；
    # 这里用一个 L8 下约 8.3k 格的矩形（仍然完整包含后面两个节点），使本测试继续走**正式**
    # 默认路径而不是被上限阻断。
    service.set_workspace([121.94, 29.96, 122.04, 30.04], health())
    service.add_node([121.96, 29.98], "A")
    service.add_node([122.02, 30.02], "B")
    state = service.generate_scenario("both")
    assert [route["route_id"] for route in state["scenario_routes"]] == ["R0001", "R0002"]
    service.delete_route("R0001")
    state = service.generate_scenario("both")
    assert "R0001" in state["retired_route_ids"]
    assert {route["route_id"] for route in state["scenario_routes"]} == {"R0002", "R0003"}
    state = service.generate_operational([])
    # Phase4-B2B-1：旧版 RoutePlannerV1 只是兼容试算，不再写 canonical operational_routes。
    assert state["operational_routes"] == []
    compatibility = state["compatibility_operational_routes"]
    assert compatibility["authoritative"] is False
    assert compatibility["compatibility"] is True
    assert compatibility["deprecated"] is True
    assert compatibility["source_algorithm"]["algorithm_id"] == "route_planner_v1"
    assert all(route["status"] == "passed" for route in compatibility["items"])
    state = service.set_rules({
        "manufacturer": "测试厂家", "model": "T1", "cruise_speed": 25,
        "max_speed": 40, "mtbf": 10000, "route_id": "R0002",
        "height_ab": 120, "height_ba": 150, "height_mode": "different",
        "horizontal_separation": 100, "direction_rule": "按航向分层",
        "delay_sensor": 500, "delay_command": 500,
    })
    assert state["rules"]["status"] == "passed"
    # 兼容试算随 route 语义失效；canonical result_statuses 不再由兼容试算决定。
    assert runtime_compatibility_result(service.session, "operational_routes")["status"] == "stale"
    try:
        service.plan_coverage()
    except ValueError as exc:
        assert "运行航路已失效" in str(exc)
    else:
        raise AssertionError("布站不得使用 stale 运行航路")
    state = service.generate_operational([])
    assert state["compatibility_operational_routes"]["status"] == "passed"
    assert state["operational_routes"] == []
    devices = [{**item, "mtbf": item["mtbf_h"]} for item in state["devices"]]
    service.set_devices(devices)
    state = service.plan_coverage()
    assert state["coverage"]["status"] == "passed"
    assert state["coverage"]["authoritative"] is False
    assert service.state["coverage"] is None
    assert set(state["coverage"]["layers"]) == {"C", "N", "S"}
    assert all(layer["statistics"]["stations"] > 0 for layer in state["coverage"]["layers"].values())
    assert state["review"]["overall_pass"] is None
    assert json.loads(service.export_project())["project"]["name"] == "端到端测试"
    assert len(json.loads(service.export_routes())["features"]) == 2
    assert json.loads(service.export_sites())["features"]
    restored = WorkflowService(store, defaults).snapshot()
    assert restored["project"]["name"] == "端到端测试"
    assert restored["coverage"] is None


def test_a_star_fails_when_hard_constraint_blocks_the_workspace():
    planner = RoutePlannerV1(grid_size=20)
    route = {"route_id": "R1", "start": [0.1, 0.5], "end": [0.9, 0.5]}
    result = planner.plan(route, [0, 0, 1, 1], [{"name": "管制区", "bbox": [0.45, 0, 0.55, 1]}])
    assert result["status"] == "failed"
    assert result["path"] == []
