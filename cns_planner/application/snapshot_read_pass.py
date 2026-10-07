"""Round32-J：通用 workflow 快照构建期的**只读复用窗**。

Round32-J0 归因确认：冷 ``workflow.snapshot()`` 会在**同一份** state 上反复读取同一批
"当前分层候选 / 当前风险画像 / 当前验证集合"。真实项目里一次冷构建中：

* ``layered_route_planner.result_snapshot`` 被调用 14 次（各约 1.28 s）；
* ``layered_route_validation.result_snapshot`` 6 次（各约 1.49 s）；
* ``route_risk_profile.result_snapshot`` 4 次（各约 1.10 s）……

合计约 31 s 只是重复读取同一份未被写入的 state。

本窗口只做一件事：把这几项**纯读**结果在一次快照构建内复用一次。

硬约束：

* **不写** ``state``、不 save、不重算任何业务结论，也不改变任何返回值；
* 生命周期只有调用栈（``with`` 退出即丢弃）；既不跨请求，也不参与
  ``WorkflowService`` 的快照缓存键 —— 因此它**不能**掩盖"冷构建本身很慢"：
  窗口外的每一次读取仍然是完整的全量读取；
* 窗口内不得写入 state；窗口内复用的集合只允许只读消费（实现方一律返回浅拷贝
  顶层，避免调用方改写缓存对象）；
* 未开窗时，所有读取行为与既有实现逐字一致。
"""

from __future__ import annotations

from contextlib import contextmanager

_CACHE_ATTRIBUTE = "_snapshot_read_pass_cache"


@contextmanager
def snapshot_read_pass(session):
    """在 ``session`` 上开启一次只读复用窗。

    嵌套时**复用外层窗口**（退出内层不做任何事），因此外层窗口内的所有读取共享同一份
    复用缓存 —— 否则内层窗口会把已经复用到的结果又丢掉。
    """

    if getattr(session, _CACHE_ATTRIBUTE, None) is not None:
        yield
        return
    setattr(session, _CACHE_ATTRIBUTE, {})
    try:
        yield
    finally:
        try:
            delattr(session, _CACHE_ATTRIBUTE)
        except AttributeError:  # pragma: no cover - 属性已不存在即可
            pass


def reused(session, key, factory):
    """窗口内复用 ``key`` 的读取结果；未开窗时总是重新读取（``factory``）。"""

    cache = getattr(session, _CACHE_ATTRIBUTE, None)
    if cache is None:
        return factory()
    if key not in cache:
        cache[key] = factory()
    return cache[key]


__all__ = ["snapshot_read_pass", "reused"]
