"""Round32-K：snapshot read-pass 复用窗的并发隔离定向测试。

Round32-J 把复用缓存挂在共享的 ``WorkflowSession`` 上，而服务端是
``ThreadingHTTPServer``：并发的两个请求会看到对方开在 session 上的属性，
从而跨请求共享缓存。Round32-K 把它换成 ``ContextVar`` 承载的执行上下文局部存储。

这里只锁三件事（不构造大型 fixture）：

1. 同线程嵌套：factory 只执行一次；离开窗口后再次调用会重新执行；
2. 两个线程真正重叠（用 ``Barrier`` 强制交叠）：各自独立，绝不拿到对方的 factory 结果；
3. 同一窗口内不同 session 不混用缓存（不假设进程只有一个 session）。
"""

from __future__ import annotations

import threading

from cns_planner.application.snapshot_read_pass import reused, snapshot_read_pass


class _Session:
    """最小 session 替身：只需要一个可承载身份的对象。"""


def _make_counter():
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        return f"value-{calls['n']}"

    return calls, factory


def test_nested_pass_reuses_once_and_releases_on_exit():
    session = _Session()
    calls, factory = _make_counter()

    # 未开窗：每次都必须真实读取。
    assert reused(session, "k", factory) == "value-1"
    assert reused(session, "k", factory) == "value-2"
    assert calls["n"] == 2

    with snapshot_read_pass(session):
        assert reused(session, "k", factory) == "value-3"
        with snapshot_read_pass(session):
            # 嵌套复用同一份缓存：不再执行 factory。
            assert reused(session, "k", factory) == "value-3"
        # 内层退出不释放外层窗口。
        assert reused(session, "k", factory) == "value-3"
    assert calls["n"] == 3

    # 离开最外层窗口后立即释放：下次调用重新执行 factory。
    assert reused(session, "k", factory) == "value-4"
    assert calls["n"] == 4


def test_overlapping_threads_never_share_a_read_pass_cache():
    session = _Session()
    entered = threading.Barrier(2)
    observed: dict[str, str] = {}
    failures: list[BaseException] = []
    lock = threading.Lock()

    def worker(tag):
        try:
            calls = {"n": 0}

            def factory():
                calls["n"] += 1
                return f"{tag}-{calls['n']}"

            with snapshot_read_pass(session):
                first = reused(session, "shared-key", factory)
                # 两个线程都进到窗口内之后才继续，确保窗口真正重叠。
                entered.wait(timeout=5)
                second = reused(session, "shared-key", factory)
            with lock:
                observed[tag] = f"{first}|{second}|{calls['n']}"
        except BaseException as exc:  # pragma: no cover - 只在失败时记录
            with lock:
                failures.append(exc)

    threads = [threading.Thread(target=worker, args=(tag,)) for tag in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert failures == []
    assert observed["a"] == "a-1|a-1|1"
    assert observed["b"] == "b-1|b-1|1"
    # 窗口外再读一次：两个线程都已释放，必须重新执行。
    calls, factory = _make_counter()
    assert reused(session, "shared-key", factory) == "value-1"


def test_one_window_keeps_sessions_apart():
    first, second = _Session(), _Session()
    first_calls, first_factory = _make_counter()
    second_calls, second_factory = _make_counter()

    with snapshot_read_pass(first):
        with snapshot_read_pass(second):
            assert reused(first, "k", first_factory) == "value-1"
            assert reused(second, "k", second_factory) == "value-1"
            # 同 key、不同 session：各自的缓存互不影响。
            assert reused(first, "k", first_factory) == "value-1"
            assert reused(second, "k", second_factory) == "value-1"

    assert first_calls["n"] == 1
    assert second_calls["n"] == 1
