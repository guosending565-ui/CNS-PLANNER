"""BUG-SHOT-005 回归：浏览器并发加载前端 ES module 时不得出现连接被拒。

现象（修复前）：浏览器打开 ``http://127.0.0.1:8765/`` 时，页面里唯二的
``<script type="module">``（``/app.js``、``/js/tasks.js``）报
``net::ERR_CONNECTION_REFUSED``，而同一页面里的普通 ``<script>`` 与 CSS 都是 200；
结果是前端 JS 一行都没执行、``window.__CNS_BOOTSTRAP_TIMING`` 为 null。

根因：``socketserver.TCPServer.request_queue_size`` 默认只有 5，也就是监听套接字的
accept backlog 只有 5。module 加载会并发拉起整张依赖图，瞬时新建连接数远超 5，
超出的连接被内核直接拒绝（Windows：``ConnectionRefusedError`` / WinError 10061），
而并发度低的普通脚本与 CSS 仍然成功——于是看起来"只有 module 加载失败"。

本测试是**黑盒**的：在随机端口起真实的 ``LocalServer``，用客户端并发建连，
断言全部连接都建立成功、且每个请求都拿到完整响应。不需要 QGIS 运行时。
"""

import json
import socket
import threading
import time

import pytest

from cns_planner.api.server import ApiHandler, LocalServer


class _Context:
    """``ApiHandler`` 只依赖 context.token；静态资源走 router 的独立分支。"""

    token = "test-token"
    static = None


class _StaticHandler(ApiHandler):
    """把静态请求直接应答成固定字节，避免在测试里引入 QGIS/router 依赖。"""

    BODY = b"// module\n"

    def do_GET(self):
        if self.path == "/api/health":
            return self.respond({"service": "cns-map", "ready": True, "data_error": ""})
        self.respond(self.BODY, "text/javascript; charset=utf-8")


@pytest.fixture
def server():
    instance = LocalServer(("127.0.0.1", 0), _StaticHandler)
    instance.context = _Context()
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield instance
    finally:
        instance.shutdown()
        instance.server_close()


def _get(host, port, path, results, index, timeout=10.0):
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            request = (
                f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
                "User-Agent: Chrome/140.0.0.0 Edg/140.0.0.0\r\n"
                "Accept: */*\r\nSec-Fetch-Dest: script\r\nSec-Fetch-Mode: cors\r\n"
                "Sec-Fetch-Site: same-origin\r\nConnection: keep-alive\r\n\r\n"
            )
            sock.sendall(request.encode("ascii"))
            sock.settimeout(timeout)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
            head, _, body = data.partition(b"\r\n\r\n")
            status = head.split(b"\r\n", 1)[0].decode("latin1")
            length = 0
            for line in head.split(b"\r\n")[1:]:
                name, _, value = line.partition(b":")
                if name.strip().lower() == b"content-length":
                    length = int(value.strip() or 0)
            while len(body) < length:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                body += chunk
            results[index] = ("OK", status, len(body))
    except Exception as exc:  # noqa: BLE001 - 测试要如实记录任何失败形态
        results[index] = ("FAIL", type(exc).__name__, str(exc))


def _burst(host, port, count, paths):
    results = [None] * count
    threads = [
        threading.Thread(
            target=_get, args=(host, port, paths[i % len(paths)], results, i)
        )
        for i in range(count)
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 30
    for thread in threads:
        thread.join(timeout=max(0.1, deadline - time.monotonic()))
    return results


def test_accept_backlog_tolerates_module_graph_concurrency(server):
    """并发建连数远超旧 backlog（5）时，不得有任何连接被内核拒绝。"""

    host, port = server.server_address[:2]
    assert server.request_queue_size >= 64, "accept backlog 必须显著高于默认的 5"

    results = _burst(host, port, 120, ["/app.js", "/js/tasks.js", "/js/main.js"])
    failures = [entry for entry in results if entry and entry[0] == "FAIL"]
    assert not failures, f"并发 module 加载出现连接被拒/失败：{failures[:5]}"
    assert all(entry == ("OK", "HTTP/1.0 200 OK", len(_StaticHandler.BODY)) for entry in results)


def test_default_backlog_is_the_regression_cause():
    """把 backlog 显式压回默认值 5，同一并发场景必须出现连接被拒。

    这条断言把"根因是 accept backlog"钉死在测试里：如果将来有人把
    ``request_queue_size`` 改回默认值，上面那条测试会失败，而这条会通过——
    两者共同说明失败来自监听队列长度，而不是别的偶发因素。
    """

    class TinyBacklogServer(LocalServer):
        request_queue_size = 5

    def burst_on(queue_size):
        instance = TinyBacklogServer(("127.0.0.1", 0), _StaticHandler)
        instance.request_queue_size = queue_size
        instance.context = _Context()
        threading.Thread(target=instance.serve_forever, daemon=True).start()
        host, port = instance.server_address[:2]
        try:
            results = _burst(host, port, 240, ["/app.js", "/js/tasks.js"])
        finally:
            instance.shutdown()
            instance.server_close()
        return [
            entry
            for entry in results
            if entry and entry[0] == "FAIL" and "Refused" in str(entry[1])
        ]

    small = burst_on(5)
    if not small:
        pytest.skip("本机内核容忍了过小的 backlog，无法稳定复现（环境相关）")
    large = burst_on(256)
    assert large == [], f"backlog=256 时仍出现被拒连接：{large[:3]}"


def test_static_module_response_headers_are_browser_loadable(server):
    """module 资源必须带正确 MIME 与长度，浏览器才会执行。"""

    host, port = server.server_address[:2]
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.sendall(
            f"GET /app.js HTTP/1.1\r\nHost: {host}:{port}\r\nConnection: close\r\n\r\n".encode()
        )
        data = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    head = data.split(b"\r\n\r\n", 1)[0].decode("latin1")
    assert "200 OK" in head
    assert "text/javascript" in head.lower()
    body = data.split(b"\r\n\r\n", 1)[1]
    assert body == _StaticHandler.BODY


def test_health_is_not_blocked_by_concurrent_module_loads(server):
    """并发 module 拉取期间，健康探测仍必须快速返回（不被队列堵死）。"""

    host, port = server.server_address[:2]
    results = [None] * 60
    threads = [
        threading.Thread(
            target=_get,
            args=(host, port, "/app.js" if i % 4 else "/api/health", results, i),
        )
        for i in range(60)
    ]
    for thread in threads:
        thread.start()
    started = time.monotonic()
    for thread in threads:
        thread.join(timeout=20)
    elapsed = time.monotonic() - started
    assert all(entry and entry[0] == "OK" for entry in results), results[:5]
    assert elapsed < 20


def test_sequential_requests_still_work_after_backlog_change(server):
    """顺序语义不受 backlog 改动影响（逐个请求仍全部成功）。"""

    host, port = server.server_address[:2]
    for _ in range(5):
        results = [None]
        _get(host, port, "/app.js", results, 0)
        assert results[0] == ("OK", "HTTP/1.0 200 OK", len(_StaticHandler.BODY))
