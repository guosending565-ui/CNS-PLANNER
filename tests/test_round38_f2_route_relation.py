"""Round38 F-2：三维 CNS 站址与航线关联的最小回归（全离线）。

真实缺陷
--------
``Visualization3DService._route_related_site_ids`` 原先只用
``route_id in action_id`` 判断 P16 规划设备是否属于某航线。真实
rev494/rev503 的 6 条 ``selected_actions`` 中，4 个塔设备动作的
``action_id`` 不含航路、也没有 ``route_id`` 字段（连键都不存在），
因此被整体跳过；按航线过滤只剩两个端点站址 ``node:N005`` / ``node:N006``。

本文件使用**真实动作字段**（只读抽取自两份 ProjectState，见
``p1b_f2_route_relation_20261018/02_real_action_reproduction.md``），
并分别验证 rev494 与 rev503，绝不把两套状态混合。

关联优先级（本任务裁决）
------------------------
A. 动作自身显式 ``route_ids[]``；
B. 动作自身显式 ``route_id``；
C. 经证据验证、唯一且可追溯的 P16 计划级航路身份（``targets[].route_id``
   与 ``before/after.routes[].route_id`` 的唯一集合）；
D. 旧 ``action_id`` 命名契约兼容（deprecated，按 token 边界匹配）。

没有可信关系时 fail-closed：不按距离、站名、设备 ID 或当前界面选择猜测。
"""

from __future__ import annotations

import pytest

from cns_planner.application.visualization_3d_service import Visualization3DService

# ------------------------------------------------------------------ 真实身份

TOWER_081 = "tower-colocation:330903908000000081"
TOWER_137 = "tower-colocation:330903908000000137"
TOWER_628 = "tower-colocation:330903500010001628"
NODE_N005 = "node:N005"
NODE_N006 = "node:N006"

REAL_SITES = {TOWER_081, TOWER_137, TOWER_628, NODE_N005, NODE_N006}


def _endpoint(role: str, site: str) -> dict:
    """真实端点动作字段（``route_id`` 显式存在；无 ``host`` / ``planning_origin``）。"""

    return {
        "action_id": f"add_navigation_integrity_monitor:R0005:{role}:{site}",
        "action": "add_navigation_integrity_monitor",
        "route_id": "R0005",
        "distinct_site_id": f"takeoff_landing_site:{site}",
        "device_id": f"NAVINT-R0005-{role}-{site}",
        "device_service_key": "N:navigation_integrity_monitoring",
        "takeoff_landing_site_id": site,
        "endpoint_role": role,
        "coordinate": [122.20, 29.95],
    }


def _tower(tower_id: str, device_suffix: str, service_key: str) -> dict:
    """真实塔设备动作字段（**没有** ``route_id`` / ``route_ids`` 键）。"""

    return {
        "action_id": (
            f"tower_colocation_host:tower-colocation:{tower_id}:{device_suffix}"
        ),
        "action_type": "add_device_to_explicit_site",
        "confirmed": True,
        "distinct_site_id": f"tower:{tower_id}",
        "device_id": device_suffix,
        "device_service_key": service_key,
        "host": {"host_type": "tower", "host_tower_id": tower_id},
        "planning_origin": {"origin": "tower_colocation", "host_tower_id": tower_id},
    }


def _real_actions() -> list:
    """rev494 与 rev503 逐字相同的 6 条动作（4 个塔设备动作 → 3 座塔站址）。"""

    return [
        _endpoint("destination", "N006"),
        _endpoint("origin", "N005"),
        _tower("330903908000000081", "RID-BASELINE-2-5KM", "S:rid_cooperative"),
        _tower("330903908000000081", "COMM-BASELINE-4KM", "C:communication"),
        _tower("330903908000000137", "COMM-BASELINE-4KM", "C:communication"),
        _tower("330903500010001628", "COMM-BASELINE-4KM", "C:communication"),
    ]


#: 真实 P16 计划级 target 前缀：rev494 的 voxel 段是 RP00026839，rev503 是 RP00026836。
_REV494_TARGETS = [
    {"target_id": "R0005|MHT4063-L08-C00271961-RP00026839@ALT-080|C:communication",
     "route_id": "R0005", "service_key": "C:communication"},
    {"target_id": "R0005|MHT4063-L08-C00271961-RP00026839@ALT-080|S:rid_cooperative",
     "route_id": "R0005", "service_key": "S:rid_cooperative"},
    {"target_id": "R0005|MHT4063-L08-C00271961-RP00026839@ALT-100|C:communication",
     "route_id": "R0005", "service_key": "C:communication"},
]
_REV503_TARGETS = [
    {"target_id": "R0005|MHT4063-L08-C00271964-RP00026836@ALT-080|C:communication",
     "route_id": "R0005", "service_key": "C:communication"},
    {"target_id": "R0005|MHT4063-L08-C00271964-RP00026836@ALT-080|S:rid_cooperative",
     "route_id": "R0005", "service_key": "S:rid_cooperative"},
    {"target_id": "R0005|MHT4063-L08-C00271964-RP00026836@ALT-100|C:communication",
     "route_id": "R0005", "service_key": "C:communication"},
]


def _state(actions, *, targets, algorithm_version="2.2", status="stale",
           before_routes=None, after_routes=None, radar_items=None):
    """最小内存 state：只保留关联判定与 canonical site key 所需的真实字段。"""

    return {
        "cns_corridor_site_plan": {
            "status": status,
            "algorithm_version": algorithm_version,
            "proposal_only": True,
            "targets": targets,
            "before": {"status": "stale",
                       "routes": [{"route_id": "R0005"}] if before_routes is None else before_routes},
            "after": {"status": "stale",
                      "routes": [{"route_id": "R0005"}] if after_routes is None else after_routes},
            "selected_actions": actions,
        },
        "radar_surveillance_layout": {"items": radar_items or []},
    }


def _rev494_state():
    return _state(_real_actions(), targets=_REV494_TARGETS,
                  algorithm_version="2.2", status="stale")


def _rev503_state():
    return _state(_real_actions(), targets=_REV503_TARGETS,
                  algorithm_version="2.3", status="proposal_ready")


def _related(state, route_id="R0005"):
    return Visualization3DService(state, shared_sites={})._route_related_site_ids(route_id)


def _no_plan_state(actions):
    """完全没有计划级航路身份的 state（用于检验 A/B/D 与 fail-closed）。"""

    return _state(actions, targets=[], before_routes=[], after_routes=[])


# ------------------------------------------------- 1. 真实六动作的现有遗漏/修复


def test_rev494_real_six_actions_recover_three_towers_and_two_nodes():
    """rev494：4 个塔设备动作 → 3 座塔站址，加 2 个端点，共 5 个不同站址。"""

    related = _related(_rev494_state())

    assert related == REAL_SITES
    assert len(related) == 5
    assert len({key for key in related if key.startswith("tower-colocation:")}) == 3


def test_rev503_real_six_actions_recover_three_towers_and_two_nodes():
    """rev503 单独验证；不读取、不混用 rev494 的 targets/状态。"""

    related = _related(_rev503_state())

    assert related == REAL_SITES
    # 两套状态各自成立，且修复不依赖请求上下文里"只有一条航线"的假设。
    assert related == _related(_rev494_state())


def test_same_tower_c_and_rid_actions_collapse_to_one_canonical_site():
    """同塔 C/RID 两个动作只产生一个 canonical 站址，不能误写成 4 座塔。"""

    related = _related(_rev494_state())
    tower_sites = {key for key in related if key.startswith("tower-colocation:")}

    assert tower_sites == {TOWER_081, TOWER_137, TOWER_628}
    assert len(tower_sites) == 3
    assert related == REAL_SITES


def test_endpoint_actions_still_resolve_to_node_keys():
    related = _related(_rev494_state())

    assert {NODE_N005, NODE_N006} <= related


# --------------------------------------------- 2. 显式 route_ids / route_id 优先


def test_explicit_route_ids_on_tower_action_is_honoured_without_action_id():
    """塔动作只要显式 ``route_ids=["R0005"]``，无需 action_id 命名兼容。"""

    action = _tower("330903908000000137", "COMM-BASELINE-4KM", "C:communication")
    action["route_ids"] = ["R0005"]
    state = _no_plan_state([action])

    assert _related(state) == {TOWER_137}


def test_explicit_route_id_wins_over_legacy_action_id():
    """显式字段优先于 action_id：显式 R0006 的动作不得被 R0005 的旧字符串命中。"""

    action = _endpoint("origin", "N005")
    action["route_id"] = "R0006"
    state = _no_plan_state([action])

    assert _related(state, "R0005") == set()
    assert _related(state, "R0006") == {NODE_N005}


def test_explicit_route_id_without_route_named_action_id_is_still_related():
    action = _tower("330903500010001628", "COMM-BASELINE-4KM", "C:communication")
    action["route_id"] = "R0005"
    state = _no_plan_state([action])

    assert _related(state) == {TOWER_628}


# --------------------------------------------------------- 3. 计划级继承与 fail-closed


def test_plan_level_route_scope_is_inherited_only_when_unique_and_matching():
    """无显式字段时，唯一计划级航路身份可继承；动作同属该单航线 P16 计划。"""

    state = _state(_real_actions(), targets=_REV494_TARGETS,
                   algorithm_version="2.2", status="stale")

    assert _related(state, "R0005") == REAL_SITES
    assert _related(state, "R0006") == set()


def test_fail_closed_without_any_provable_route_relation():
    """无显式字段、无计划级身份、action_id 也不含航路 → 空集，不按距离补。"""

    tower = _tower("330903908000000081", "COMM-BASELINE-4KM", "C:communication")
    endpoint = _endpoint("destination", "N006")
    # 去掉唯一的 action_id 航路 token 与显式 route_id，模拟"没有可证明关系"。
    endpoint.pop("route_id")
    endpoint["action_id"] = "add_navigation_integrity_monitor:destination:N006"
    state = _no_plan_state([tower, endpoint])

    assert _related(state, "R0005") == set()
    assert _related(state, "R0006") == set()


def test_explicit_empty_route_ids_is_not_treated_as_missing():
    """``route_ids=[]`` 是显式空关联：不得继承计划级，也不得解释为任意航线。"""

    explicit_empty = _tower("330903908000000081", "COMM-BASELINE-4KM", "C:communication")
    explicit_empty["route_ids"] = []
    missing = _tower("330903908000000137", "COMM-BASELINE-4KM", "C:communication")
    state = _state([explicit_empty, missing], targets=_REV494_TARGETS,
                   algorithm_version="2.2", status="stale")

    related = _related(state)
    assert TOWER_081 not in related      # 显式空 → 不关联
    assert TOWER_137 in related          # 字段缺失 → 继承唯一计划级身份


# ------------------------------------- 4. 相似 route_id / 跨航线 / 父子身份不一致


@pytest.mark.parametrize("action_id", [
    "add_navigation_integrity_monitor:R00050:destination:N006",
    "add_navigation_integrity_monitor:R000:destination:N006",
    "add_navigation_integrity_monitor:R0005X:destination:N006",
])
def test_legacy_action_id_fallback_uses_token_boundaries(action_id):
    """旧 ``in action_id`` 会把 R00050 / R000 误当成 R0005；token 边界不得命中。"""

    action = _endpoint("destination", "N006")
    action.pop("route_id")
    action["action_id"] = action_id
    state = _no_plan_state([action])

    assert _related(state, "R0005") == set()


def test_legacy_action_id_fallback_still_supports_the_historical_contract():
    """历史命名 ``...:R0005:...`` 仍可兼容，但只作为最低优先级回退。"""

    action = _endpoint("origin", "N005")
    action.pop("route_id")
    state = _no_plan_state([action])

    assert _related(state, "R0005") == {NODE_N005}


def test_explicit_and_plan_level_conflict_fails_closed_for_both_routes():
    """显式 R0006 与唯一计划级 R0005 冲突：两边的请求都不得强行通过。"""

    action = _tower("330903908000000137", "COMM-BASELINE-4KM", "C:communication")
    action["route_id"] = "R0006"
    state = _state([action], targets=_REV494_TARGETS,
                   algorithm_version="2.2", status="stale")

    assert _related(state, "R0005") == set()
    assert _related(state, "R0006") == set()


def test_inconsistent_parent_child_route_identity_is_ambiguous_and_not_inherited():
    """targets 与 before/after routes 的航路身份不一致 → 计划级不唯一，不继承。"""

    actions = _real_actions()
    for action in actions[:2]:
        # 去掉端点自身的显式 route_id，确保只能依赖计划级身份。
        action.pop("route_id")
    state = _state(
        actions,
        targets=_REV494_TARGETS,
        before_routes=[{"route_id": "R0005"}],
        after_routes=[{"route_id": "R0006"}],
        algorithm_version="2.2",
        status="stale",
    )

    assert _related(state, "R0005") == set()
    assert _related(state, "R0006") == set()


def test_multi_route_plan_scope_is_not_inherited_without_explicit_relation():
    """计划级出现多条航线 → 不得把全部动作默认归给当前请求的航线。"""

    actions = _real_actions()
    for index, action in enumerate(actions):
        # 去掉端点显式 route_id，保证所有动作都只能依赖计划级身份。
        if index < 2:
            action.pop("route_id")
    targets = list(_REV494_TARGETS) + [
        {"target_id": "R0007|TARGET@ALT-100|C:communication", "route_id": "R0007",
         "service_key": "C:communication"},
    ]
    state = _state(actions, targets=targets, algorithm_version="2.2", status="stale")

    assert _related(state, "R0005") == set()
    assert _related(state, "R0007") == set()


def test_radar_panels_remain_an_independent_explicit_route_source():
    """Radar 自身的显式 route_id 仍可补充站址；这是既有独立证据路径。"""

    radar_items = [{
        "route_id": "R0005",
        "selected_panels": [{"panel_id": "RP-1", "tower_id": "330903908000000137"}],
    }]
    state = _state([], targets=[], before_routes=[], after_routes=[], radar_items=radar_items)

    assert _related(state) == {TOWER_137}
