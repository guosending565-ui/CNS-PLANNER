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
* 生命周期只有**当前执行上下文**（``with`` 退出即丢弃，见下）；
* 既不跨请求，也不跨线程，也不参与 ``WorkflowService`` 的快照缓存键 —— 因此它
  **不能**掩盖"冷构建本身很慢"：窗口外的每一次读取仍然是完整的全量读取；
* 窗口内不得写入 state；窗口内复用的集合只允许只读消费（实现方一律返回浅拷贝
  顶层，避免调用方改写缓存对象）；
* 未开窗时，所有读取行为与既有实现逐字一致。

Round32-K：并发隔离修复（BLOCKER）
----------------------------------

Round32-J 曾把复用缓存临时挂在**共享的 ``WorkflowSession`` 对象**上。但服务端是
``cns_planner/api/server.py`` 的 ``LocalServer(ThreadingHTTPServer)``：多个 HTTP
请求会**并发**命中同一个 workflow/session。于是可能发生

* request A 在 session 上开启窗口；
* 并发的 request B 看到该属性已存在，**误把 A 的窗口当成自己的外层窗口**，
  于是跨请求共享了 A 的缓存。

这违反本模块的设计边界。因此缓存改由 :class:`contextvars.ContextVar` 承载：

* 每个线程（threading 服务端下即每个请求）各自持有独立的 ``ContextVar`` 值，
  互不可见、互不覆盖；
* 开启最外层窗口时**新建**窗口对象并 ``set``，退出时用 ``token`` ``reset``
  复位；嵌套窗口复用同一个窗口对象，只做引用计数，不新建也不释放；
* 最外层退出即释放，窗口不会残留到下一个请求、也不会残留到线程之外；
* 窗口内按 **session** 分桶（``id(session)``），不假设进程里只有一个 session；
  每个 session 的桶在它自己的最外层退出时删除，因此不会长期持有 session 引用，
  也不会把 ``id()`` 留给后续新建对象复用。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

#: 当前线程/执行上下文的只读复用窗。``None`` 表示未开窗。
#: 值形如 ``{session_id: {"cache": {...}, "depth": int}}``——按 session 分桶，
#: 使同一窗口内不同 session 的读取结果绝不混用。
_read_pass_window: ContextVar = ContextVar("cns_snapshot_read_pass_window", default=None)


@contextmanager
def snapshot_read_pass(session):
    """在当前执行上下文中为 ``session`` 开启一次只读复用窗。

    嵌套时**复用外层窗口**（退出内层只做引用计数，不做任何释放），因此外层窗口内的
    所有读取共享同一份复用缓存 —— 否则内层窗口会把已经复用到的结果又丢掉。

    窗口只存在于当前执行上下文（threading 服务端下即当前请求线程）：
    并发请求各自独立，最外层退出即释放。
    """

    windows = _read_pass_window.get()
    token = None
    if windows is None:
        # 最外层：新建窗口对象并写入本上下文（不是复用别人对象上的属性）。
        windows = {}
        token = _read_pass_window.set(windows)

    session_id = id(session)
    entry = windows.get(session_id)
    if entry is None:
        entry = {"cache": {}, "depth": 0}
        windows[session_id] = entry
    entry["depth"] += 1
    try:
        yield
    finally:
        entry["depth"] -= 1
        if entry["depth"] <= 0:
            windows.pop(session_id, None)
            if token is not None:
                # 本函数开启的最外层窗口：复位上下文，整体释放。
                _read_pass_window.reset(token)
            elif not windows:
                # 外层窗口已把它的 session 桶全部退完，这里顺手清掉空窗口。
                _read_pass_window.set(None)


def reused(session, key, factory):
    """窗口内复用 ``key`` 的读取结果；未开窗时总是重新读取（``factory``）。"""

    windows = _read_pass_window.get()
    entry = windows.get(id(session)) if windows else None
    if entry is None:
        return factory()
    cache = entry["cache"]
    if key not in cache:
        cache[key] = factory()
    return cache[key]


__all__ = ["snapshot_read_pass", "reused"]
