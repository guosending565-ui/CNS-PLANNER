"""AltitudeLayer 目录生命周期回归：工程默认高度层、工作区初始化与项目恢复。

覆盖 Phase 3.5 人工验收发现的缺陷：
workspace 恢复后 ``spatial_3d.altitude_layers`` 为空 ⇒
``altitude layer catalog = not_configured · 共 0 层`` ⇒
``resolve_cruise_altitude`` 报 ``altitude_layer_missing`` ⇒ Theta* V2 无法执行。

本文件只验证**配置生命周期**，绝不放宽高度检查：
默认高度层只是 catalog 条目，planning request 仍必须由用户显式选择高度层。
"""

from copy import deepcopy
from pathlib import Path

import pytest

from cns_planner.algorithms.grid.service import WorkspaceGridService
from cns_planner.api.router import ApiRouter
from cns_planner.application.session import WorkflowSession
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.altitude_layer_defaults import (
    DEFAULT_ALTITUDE_LAYER_SOURCE, INITIALIZED_KEY, default_altitude_layers,
    default_altitude_layer_payloads, ensure_default_altitude_layers,
    should_initialize_default_altitude_layers,
)
from cns_planner.domain.spatial_3d import normalize_altitude_layer


DEFAULTS = Path("cns_planner/config/defaults.json")
WORKSPACE = [120.001, 30.001, 120.01, 30.01]
DEFAULT_LAYER_IDS = ("ALT-060", "ALT-080", "ALT-100", "ALT-150", "ALT-200")
DEFAULT_LAYER_NOMINALS = [60.0, 80.0, 100.0, 150.0, 200.0]


def health():
    return {
        "status": "passed",
        "population": {"status": "passed"},
        "airspace": {"status": "passed"},
        "terrain": {"status": "passed"},
        "buildings": {"status": "passed"},
        "property": {"status": "missing_data"},
        "loaded_layer_count": 3,
        "covered_layer_count": 3,
    }


def workflow(tmp_path, name="project.json"):
    return WorkflowService(tmp_path / name, DEFAULTS)


def layers_of(state):
    return (state.get("spatial_3d") or {}).get("altitude_layers") or []


def cruise_of(service, request=None):
    if request is not None:
        service.layered_route_planner_service.set_planning_request(request)
    readiness = service.layered_route_planner_service.readiness_snapshot()
    return readiness


# ------------------------------------------------------------------ 默认高度层合同


def test_default_layers_are_the_declared_engineering_layers():
    layers = default_altitude_layers()
    assert [item["altitude_layer_id"] for item in layers] == list(DEFAULT_LAYER_IDS)
    assert [item["nominal_altitude_m"] for item in layers] == DEFAULT_LAYER_NOMINALS
    for item in layers:
        assert item["vertical_reference"] == "egm2008_orthometric"
        assert item["confirmed"] is True
        # confirmed 只在 nominal / datum / source 三者都显式给出时成立。
        assert item["status"] == "confirmed"
        assert item["source"] == DEFAULT_ALTITUDE_LAYER_SOURCE
        assert item["lower_altitude_m"] < item["nominal_altitude_m"] < item["upper_altitude_m"]
    # 60 / 80 m 两层必须在内，且层带互不重叠（重叠会带来 voxel 归属歧义）。
    bounds = [(item["lower_altitude_m"], item["upper_altitude_m"]) for item in layers]
    assert [item["nominal_altitude_m"] for item in layers[:2]] == [60.0, 80.0]
    for (_, upper), (next_lower, _) in zip(bounds, bounds[1:]):
        assert upper <= next_lower


def test_default_layer_payloads_survive_the_existing_normalizer_unchanged():
    payloads = default_altitude_layer_payloads()
    assert [normalize_altitude_layer(item) for item in payloads] == default_altitude_layers()


def test_default_layers_never_replace_a_shipped_default_or_relax_the_contract():
    # 只补 catalog：绝不写 route 分配 / procedure / planning request。
    assert default_altitude_layers() == [normalize_altitude_layer(item) for item in default_altitude_layer_payloads()]
    assert not any("route_id" in item for item in default_altitude_layer_payloads())


# ------------------------------------------------------------------ 初始化判定


def test_initialization_requires_a_mapped_workspace():
    assert should_initialize_default_altitude_layers({}) is False
    assert should_initialize_default_altitude_layers({"workspace": {"bbox": WORKSPACE}}) is False
    assert should_initialize_default_altitude_layers({
        "workspace": {"bbox": WORKSPACE}, "grid": {"cells": []},
    }) is False
    assert should_initialize_default_altitude_layers({
        "workspace": {"bbox": WORKSPACE}, "grid": {"cells": [{"grid_id": "G1"}]},
    }) is True
    # 已经有目录 / 已经初始化过：都不再补建。
    assert should_initialize_default_altitude_layers({
        "workspace": {"bbox": WORKSPACE}, "grid": {"cells": [{"grid_id": "G1"}]},
        "spatial_3d": {"altitude_layers": [{"altitude_layer_id": "L1"}]},
    }) is False
    assert should_initialize_default_altitude_layers({
        "workspace": {"bbox": WORKSPACE}, "grid": {"cells": [{"grid_id": "G1"}]},
        INITIALIZED_KEY: True,
    }) is False


def test_ensure_is_idempotent_and_never_overwrites_an_existing_catalog():
    state = {
        "workspace": {"bbox": WORKSPACE}, "grid": {"cells": [{"grid_id": "G1"}]},
        "spatial_3d": {"altitude_layers": []},
    }
    assert ensure_default_altitude_layers(state) is True
    first = deepcopy(state["spatial_3d"]["altitude_layers"])
    assert [item["altitude_layer_id"] for item in first] == list(DEFAULT_LAYER_IDS)
    assert state[INITIALIZED_KEY] is True
    assert ensure_default_altitude_layers(state) is False
    assert state["spatial_3d"]["altitude_layers"] == first


# ------------------------------------------------------------------ workspace 生命周期


def test_new_project_has_no_catalog_until_a_workspace_is_mapped(tmp_path):
    service = workflow(tmp_path)
    assert layers_of(service.state) == []
    assert service.layered_route_planner_service.readiness_snapshot()["altitude_layer_catalog"] == {
        "status": "not_configured", "count": 0, "altitude_layer_ids": [],
        "selected_altitude_layer_id": None,
        "cruise_altitude": {
            "altitude_layer_id": None, "status": "blocked", "altitude_egm2008_m": None,
            "nominal_altitude_m": None, "vertical_reference": "unknown",
            "declared_status": None, "conversion": None, "reason": "altitude_layer_missing",
        },
    }


def test_mapping_a_workspace_creates_the_default_engineering_altitude_layers(tmp_path):
    service = workflow(tmp_path)
    service.set_workspace(WORKSPACE, health())
    layers = layers_of(service.state)
    assert [item["altitude_layer_id"] for item in layers] == list(DEFAULT_LAYER_IDS)
    assert all(item["status"] == "confirmed" for item in layers)
    # 目录状态从 not_configured 变为 configured，但对齐的是 EGM2008 正高。
    readiness = service.layered_route_planner_service.readiness_snapshot()
    assert readiness["altitude_layer_catalog"]["status"] == "configured"
    assert readiness["altitude_layer_catalog"]["count"] == 5
    assert readiness["altitude_layer_catalog"]["altitude_layer_ids"] == list(DEFAULT_LAYER_IDS)
    # 未显式选择时仍不解析任何巡航高度：默认目录不是默认选择。
    assert readiness["altitude_layer_catalog"]["selected_altitude_layer_id"] is None
    # 目录已持久化，且 workspace 的 grid 事实没有被动过。
    assert service.state["grid"]["cells"]
    document = __import__("json").loads((tmp_path / "project.json").read_text(encoding="utf-8"))
    assert [item["altitude_layer_id"] for item in document["spatial_3d"]["altitude_layers"]] == list(DEFAULT_LAYER_IDS)
    assert document[INITIALIZED_KEY] is True


def test_explicitly_deleting_every_layer_keeps_the_catalog_empty(tmp_path):
    service = workflow(tmp_path)
    service.set_workspace(WORKSPACE, health())
    for layer_id in DEFAULT_LAYER_IDS:
        service.delete_altitude_layer({"altitude_layer_id": layer_id})
    assert layers_of(service.state) == []
    # 显式清空的目录在项目恢复时不得被再次填满。
    restored = WorkflowSession(tmp_path / "project.json", DEFAULTS, WorkspaceGridService())
    assert layers_of(restored.state) == []
    assert restored.state[INITIALIZED_KEY] is True


# ------------------------------------------------------------------ 项目恢复生命周期


def test_project_recovery_restores_the_altitude_layer_catalog(tmp_path):
    store = tmp_path / "project.json"
    service = workflow(tmp_path)
    service.set_workspace(WORKSPACE, health())
    # 新增一层自定义高度层后恢复：工程默认层与自定义层必须全部保留。
    service.set_altitude_layer({
        "altitude_layer_id": "ALT-ZS-080-EGM2008", "name": "舟山 80 m",
        "nominal_altitude_m": 80.0, "lower_altitude_m": 50.0, "upper_altitude_m": 120.0,
        "vertical_reference": "egm2008_orthometric", "source": "工程确认-测试",
        "confirmed": True,
    })
    expected = deepcopy(service.state["spatial_3d"]["altitude_layers"])
    assert [item["altitude_layer_id"] for item in expected] == [
        "ALT-060", "ALT-080", "ALT-100", "ALT-150", "ALT-200", "ALT-ZS-080-EGM2008",
    ]

    restored = WorkflowService(store, DEFAULTS)
    assert restored.state["spatial_3d"]["altitude_layers"] == expected


def test_recovery_of_a_legacy_project_without_any_layer_backfills_defaults_readonly(tmp_path):
    store = tmp_path / "legacy.json"
    service = workflow(tmp_path, "legacy.json")
    service.set_workspace(WORKSPACE, health())
    # 模拟"旧项目"：目录为空且没有目录初始化标记。
    document = __import__("json").loads(store.read_text(encoding="utf-8"))
    document["spatial_3d"]["altitude_layers"] = []
    document.pop(INITIALIZED_KEY, None)
    store.write_text(__import__("json").dumps(document, ensure_ascii=False), encoding="utf-8")
    before = store.read_bytes()

    restored = WorkflowSession(store, DEFAULTS, WorkspaceGridService())
    assert [item["altitude_layer_id"] for item in layers_of(restored.state)] == list(DEFAULT_LAYER_IDS)
    assert restored.state[INITIALIZED_KEY] is True
    assert restored.state["spatial_3d"]["status"] == "passed"
    # 恢复保持只读：打开旧项目不改写项目文件（既有持久化契约不变）。
    assert store.read_bytes() == before
    # 差异被登记为待提交，因此任何后续写操作都会把目录落到磁盘。
    assert restored.pending_save is True


def test_empty_project_stays_empty_across_recovery(tmp_path):
    store = tmp_path / "empty.json"
    service = workflow(tmp_path, "empty.json")
    service.save()
    restored = WorkflowSession(store, DEFAULTS, WorkspaceGridService())
    assert layers_of(restored.state) == []
    assert restored.pending_save is False


# ------------------------------------------------------------------ Step03 planning request


def test_planning_request_still_requires_an_explicit_layer_choice(tmp_path):
    service = workflow(tmp_path)
    service.set_workspace(WORKSPACE, health())
    service.add_node([120.002, 30.002], "A")
    service.add_node([120.008, 30.008], "B")
    service.generate_scenario("both")
    # 目录有 5 层，但请求没有选择高度层 ⇒ 保持 blocked，绝不自动代入第一层。
    service.layered_route_planner_service.set_planning_request({
        "scenario_route_id": "R0001", "source": "工程验证", "confirmed": True,
    })
    readiness = service.layered_route_planner_service.readiness_snapshot()
    assert readiness["altitude_layer_catalog"]["count"] == 5
    assert readiness["scenario_route"]["status"] == "resolved"
    assert readiness["request"]["altitude_layer_id"] is None
    assert readiness["request"]["status_reason"] == "altitude_layer_not_explicitly_selected"
    assert "altitude_layer_not_explicitly_selected" in {
        item["reason_code"] for item in readiness["blockers"]
    }
    # 目录有默认层并不会让规划请求自动变成 confirmed。
    assert readiness["status"] == "blocked"


def test_theta_star_v2_altitude_blocker_is_cleared_by_an_explicit_selection(tmp_path):
    service = workflow(tmp_path)
    service.set_workspace(WORKSPACE, health())
    codes_before = {
        item["reason_code"]
        for item in service.layered_route_planner_service.readiness_snapshot()["blockers"]
    }
    assert "altitude_layer_not_found" in codes_before

    service.layered_route_planner_service.set_planning_request({
        "scenario_route_id": "R0001", "altitude_layer_id": "ALT-100",
        "source": "工程验证", "confirmed": True,
    })
    readiness = service.layered_route_planner_service.readiness_snapshot()
    cruise = readiness["altitude_layer_catalog"]["cruise_altitude"]
    assert cruise["status"] == "confirmed"
    assert cruise["altitude_egm2008_m"] == 100.0
    assert cruise["conversion"] == "already_canonical_egm2008_orthometric"
    assert cruise["reason"] is None
    codes_after = {item["reason_code"] for item in readiness["blockers"]}
    # 高度层相关 blocker 全部解除；其余 blocker 属于既有"无默认值"语义，保持不变。
    assert not {"altitude_layer_missing", "altitude_layer_not_found",
                "altitude_layer_not_explicitly_selected"} & codes_after


# ------------------------------------------------------------------ API surface


class ApiContext:
    def __init__(self, service):
        self.workflow = service
        self.data = object()


def test_api_exposes_the_restored_catalog_without_creating_a_route_assignment(tmp_path):
    service = workflow(tmp_path)
    service.set_workspace(WORKSPACE, health())
    router = ApiRouter(ApiContext(service))
    spatial = router.get("/api/spatial-3d", {}, {}).data
    assert [item["altitude_layer_id"] for item in spatial["altitude_layers"]] == list(DEFAULT_LAYER_IDS)
    assert spatial["route_operating_layers"] == []
    assert spatial["departure_arrival_procedures"] == []
    plan = router.get("/api/route-operating-plan", {}, {}).data
    assert [item["altitude_layer_id"] for item in plan["altitude_layer_catalog"]] == list(DEFAULT_LAYER_IDS)
    assert plan["routes"] == []
