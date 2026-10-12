"""BUG-R37A-001 回归：装配期的 currentness 投影不得被快照缓存固化。

Round37-A 冷启动实测（`checkpoint/phase36-pre3d` @ `2566275`）：

* ``GET /api/workflow`` → ``layered_route_validations[LRV-DE5BB14AA74B]
  .current_applicability = stale_candidate``、``layered_operational_adoptions
  [LRA-2ECF2DBAF402] = stale_validation``；
* ``GET /api/layered-route-validations`` / ``GET /api/layered-operational-adoptions``
  → **同一条记录** ``current``；
* 做一次保守保存（revision +1 ⇒ 缓存键变化）后三条路径才一致。

根因：``ApplicationContext`` 在 runtime GIS 绑定（``_bind_runtime_services``）**之前**
就已经构建过一份快照 —— ``MapData`` 的 ``workflow_provider`` 与数据源登记
（``register_source_paths``）都会触发 ``WorkflowService.snapshot()``。这些绑定是
currentness 只读投影的真实输入，却**不进入 ProjectState**：既不递增 ``revision``，
也不改变 ``_snapshot_structure_key``（容器类型 / 长度 / 身份都没变），于是缓存继续
下发"装配期"算出的 ``stale_*``。

修复：``_bind_runtime_services()`` 在全部绑定完成后显式
``WorkflowService.invalidate_snapshot_cache()``（只丢弃缓存，不写字段、不递增 revision、
不做手动保存）。

本文件锁定四件事：

1. 真实装配点必须在绑定收尾丢弃快照缓存，且绑定清单与代码同步；
2. 绑定就位后，通用快照与专用只读接口对同一条记录给出一致的 currentness；
3. ``_install_in_place`` 这类"身份不变的原地安装"不会伪装成"内容没变"；
4. 工程演示（``engineering_demo_only``）作用域保持独立：不被严格模式失败改写，
   也绝不写进 canonical ``operational_routes``。
"""

from __future__ import annotations

import ast
import re
from copy import deepcopy
from pathlib import Path

import pytest

from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.layered_operational_adoption import (
    normalize_layered_operational_adoptions,
)
from cns_planner.domain.layered_route_validation import (
    normalize_layered_route_validation_collection,
)

#: 组合根源文件（``ApplicationContext`` 顶层 import 需要 QGIS 运行时，因此装配点契约
#: 用 AST 断言而不是 import 该类 —— 这样这条回归在无 QGIS 的解释器里也会真正执行）。
APP_CONTEXT_PATH = (
    Path(__file__).parents[1] / "cns_planner" / "application" / "app_context.py"
)

DEFAULTS = Path(__file__).parents[1] / "cns_planner" / "config" / "defaults.json"

#: ``_bind_runtime_services`` 必须完成的全部 runtime 绑定（顺序即依赖顺序）。
RUNTIME_BINDING_HOOKS = (
    "configure_route_planner_v3_sources",
    "configure_route_planner_v3_adoption",
    "configure_layered_route_planner_sources",
    "configure_layered_route_validation_sources",
    "configure_vertical_transition_validation_sources",
    "configure_radar_surveillance_layout_sources",
    "configure_surface_classification_sources",
)

#: 一份"绑定就位后才可判定为 current"的候选集合（candidate 只由运行时服务提供）。
CANDIDATES = {
    "status": "passed",
    "count": 1,
    "active_candidate_id": "LC-1",
    "items": [{
        "candidate_id": "LC-1",
        "route_id": "R-1",
        "altitude_layer_id": "L-100",
        "status": "candidate",
        "current_applicability": "current",
        "candidate_fingerprint": "candidate-fp",
        "path": [[122.0, 30.0], [122.001, 30.0]],
    }],
    "masks": {},
}


def _currentness(collection):
    return [
        item.get("current_applicability")
        for item in (collection or {}).get("items") or []
    ]


def _pick(collection, key, value):
    for item in (collection or {}).get("items") or []:
        if item.get(key) == value:
            return item
    return None


def _service(tmp_path, *, with_demo_adoption=False):
    """最小装配：一条待判定的验证记录（+ 可选的工程演示采纳记录）。"""

    service = WorkflowService(tmp_path / "project.json", DEFAULTS)
    service.state["layered_route_validations"] = normalize_layered_route_validation_collection({
        "status": "passed",
        "items": [{
            "validation_id": "LRV-1",
            "status": "passed",
            "candidate": {
                "candidate_id": "LC-1", "route_id": "R-1", "altitude_layer_id": "L-100",
            },
            "fingerprints": {"validation_fingerprint": "stored-fp", "components": {}},
        }],
    })
    if with_demo_adoption:
        service.state["layered_operational_adoptions"] = (
            normalize_layered_operational_adoptions({
                "status": "passed",
                "items": [{
                    "adoption_id": "LRA-1", "route_id": "R-1", "validation_id": "LRV-1",
                    "status": "published_engineering_demo",
                    "engineering_conditional_acceptance": True,
                }],
            })
        )
    return service


def _bind_late(service):
    """模拟"runtime 绑定终于就位"：候选只在绑定之后才可被解析为 current。"""

    service.layered_route_planner_service.result_snapshot = lambda: deepcopy(CANDIDATES)
    return service


# ---- 1. 真实装配点 ---------------------------------------------------------

def _bind_runtime_services_node():
    tree = ast.parse(APP_CONTEXT_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_bind_runtime_services":
            return node
    raise AssertionError("app_context.py 里找不到 ApplicationContext._bind_runtime_services")


def test_runtime_binding_hook_inventory_and_invalidation_are_wired():
    """装配点契约：绑定清单与代码同步，且**收尾必须丢弃快照缓存**（本 Bug 的修复点）。"""

    node = _bind_runtime_services_node()
    source = ast.get_source_segment(APP_CONTEXT_PATH.read_text(encoding="utf-8"), node)
    called = re.findall(r"self\.(configure_[a-z0-9_]+)\(", source)
    assert called == list(RUNTIME_BINDING_HOOKS), (
        "_bind_runtime_services 的绑定清单已变化，请同步 RUNTIME_BINDING_HOOKS 与回归"
    )

    body = node.body
    assert isinstance(body[-1], ast.Return), "装配方法必须以 return self.workflow 收尾"
    tail = body[-2]
    assert isinstance(tail, ast.Expr) and isinstance(tail.value, ast.Call), (
        "return 之前必须有一条显式的缓存失效调用"
    )
    target = tail.value.func
    assert isinstance(target, ast.Attribute) and target.attr == "invalidate_snapshot_cache", (
        "runtime 绑定完成后必须 invalidate_snapshot_cache()，否则绑定前的 currentness "
        "投影会被缓存固化（BUG-R37A-001）"
    )


def test_runtime_binding_completion_invalidates_the_snapshot_cache():
    """同一契约的行为级复核（需要 QGIS 运行时，缺失时跳过）。"""

    pytest.importorskip("qgis", reason="ApplicationContext 组合根需要 QGIS 运行时")
    from cns_planner.application.app_context import ApplicationContext

    events = []

    class _FakeWorkflow:
        def invalidate_snapshot_cache(self):
            events.append("invalidate")

    context = object.__new__(ApplicationContext)
    context.workflow = _FakeWorkflow()
    for name in RUNTIME_BINDING_HOOKS:
        setattr(
            context, name,
            (lambda hook: lambda *args, **kwargs: events.append("bind:" + hook))(name),
        )

    ApplicationContext._bind_runtime_services(context)

    assert events[:-1] == ["bind:" + name for name in RUNTIME_BINDING_HOOKS]
    assert events[-1] == "invalidate"


# ---- 2. 两读路径的 currentness 必须一致 -------------------------------------

def test_snapshot_and_dedicated_endpoint_agree_after_late_runtime_binding(tmp_path):
    """绑定就位 + 装配收尾之后，两条读路径对同一条记录必须给出同一个 currentness。"""

    service = _service(tmp_path)

    # 装配期：runtime 绑定尚未就位 ⇒ 通用快照先投影了一次并进入缓存。
    early_snapshot = _currentness(service.snapshot()["layered_route_validations"])
    assert early_snapshot == ["stale_candidate"], (
        "候选集合在绑定前不可解析时，验证必须如实判为 stale_candidate"
    )

    _bind_late(service)

    # 缺陷现场：缓存固化的旧投影与新鲜投影**此刻确实不一致**（这正是 Round37-A 实测的
    # `/api/workflow` vs `/api/layered-route-validations` 分歧）。
    assert _currentness(service.snapshot()["layered_route_validations"]) != _currentness(
        service.layered_route_validations()
    ), "该用例的场景必须能真的复现两读路径分歧，否则它证明不了任何事"

    # 装配收尾（真实装配点见 test_runtime_binding_hook_inventory_and_invalidation_are_wired）。
    service.invalidate_snapshot_cache()

    snapshot_view = _currentness(service.snapshot()["layered_route_validations"])
    endpoint_view = _currentness(service.layered_route_validations())
    assert snapshot_view == endpoint_view, (
        "通用快照与专用只读接口对同一条记录必须给出一致的 current_applicability；"
        f"快照={snapshot_view} 专用接口={endpoint_view}"
    )
    assert snapshot_view != ["stale_candidate"], (
        "绑定就位后仍下发装配期的 stale_candidate 说明缓存又被固化了"
    )


def test_snapshot_does_not_pin_the_pre_binding_projection(tmp_path):
    """反证：没有显式失效时，身份/长度不变的运行时绑定不会让缓存键变化。"""

    service = _service(tmp_path)
    service.snapshot()
    frozen = service._snapshot_cache[1]
    revision = service.state.get("revision")

    _bind_late(service)

    assert service.state.get("revision") == revision, "运行时绑定刻意不写 ProjectState"
    assert service.snapshot() is not None
    assert service._snapshot_cache[1] is frozen, (
        "缓存键只看 revision + 结构指纹，因此运行时绑定变化必须由装配点显式失效"
    )

    service.invalidate_snapshot_cache()
    assert service._snapshot_cache is None
    service.snapshot()
    assert service._snapshot_cache[1] is not frozen


# ---- 3. 身份不变的原地安装 --------------------------------------------------

def test_in_place_container_install_keeps_identity_and_needs_explicit_invalidation(tmp_path):
    """``_install_in_place`` 保持对象身份，因此缓存键看不出内容变化。"""

    service = _service(tmp_path)
    service.snapshot()
    frozen = service._snapshot_cache[1]
    revision = service.state.get("revision")

    target = service.state["layered_operational_adoptions"]
    identity = id(target)
    replacement = deepcopy(target)
    replacement["in_place_probe"] = True
    target.clear()
    target.update(replacement)          # 与 application/_install_in_place 等价

    assert id(service.state["layered_operational_adoptions"]) == identity
    assert service.state.get("revision") == revision
    assert service._snapshot_cache[1] is frozen, (
        "身份不变的原地安装不会改变结构指纹，必须由写路径显式失效缓存"
    )

    service.invalidate_snapshot_cache()
    service.snapshot()
    assert service._snapshot_cache[1] is not frozen


# ---- 4. 严格 / 工程演示作用域分离 -------------------------------------------

def test_engineering_demo_adoption_stays_out_of_canonical_routes(tmp_path):
    """工程演示采纳是显式状态：不被投影改写，也绝不写进 canonical 运行航路。"""

    service = _service(tmp_path, with_demo_adoption=True)
    _bind_late(service)
    service.invalidate_snapshot_cache()

    snapshot = service.snapshot()
    demo = _pick(snapshot["layered_operational_adoptions"], "adoption_id", "LRA-1")
    assert demo["status"] == "published_engineering_demo"
    assert demo["engineering_conditional_acceptance"] is True
    assert snapshot.get("operational_routes") == [], (
        "工程演示只读消费绝不产生 canonical operational_route"
    )

    snapshot_view = _currentness(snapshot["layered_operational_adoptions"])
    endpoint_view = _currentness(service.layered_operational_adoptions())
    assert snapshot_view == endpoint_view, (
        "工程演示采纳的 currentness 同样必须两条读路径一致"
    )
