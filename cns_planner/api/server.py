"""HTTP server and transport-only request handler."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
from urllib.parse import parse_qs, urlparse

from .router import ApiRouter, _wants_async
from .security import same_origin, valid_token


def wants_async_submission(path, payload):
    """本次 POST 是否是异步 heavy task 提交（只登记任务、不写 ProjectState）。

    * ``/api/tasks``、``/api/tasks/<id>/cancel`` 本身就是任务端点；
    * 已登记为 heavy 的业务 endpoint 必须显式带 ``async: true`` 才算异步，
      否则保持原有同步语义（不改变任何既有调用方的行为）。
    """

    if str(path) == "/api/tasks" or str(path).startswith("/api/tasks/"):
        return True
    if not _wants_async(payload):
        return False
    from ..tasks.task_specs import task_type_for_endpoint

    return task_type_for_endpoint(path) is not None


class StaleRevisionError(ValueError):
    pass


class LocalServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def get_request(self):
        connection, address = super().get_request(); connection.settimeout(15)
        return connection, address


class ApiHandler(BaseHTTPRequestHandler):
    @property
    def context(self):
        return self.server.context

    def respond(self, data, content_type="application/json; charset=utf-8", status=200, cache=False, headers=None):
        if not isinstance(data, bytes): data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=86400" if cache else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "frame-ancestors 'self' http://127.0.0.1:8501 http://localhost:8501")
        for name, value in (headers or {}).items():
            self.send_header(name, str(value))
        self.end_headers()
        try: self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError): pass

    def allowed(self): return same_origin(self.headers)

    def do_GET(self):
        if not self.allowed(): return self.respond({"error": "仅允许本机同源访问"}, status=403)
        url = urlparse(self.path)
        # 任务查询同样要求有效会话：任务记录里含有 fingerprint / artifact 引用等技术信息。
        if (
            url.path in ("/api/online-health", "/api/browse", "/api/cns-planning-report/artifact")
            or url.path == "/api/tasks" or url.path.startswith("/api/tasks/")
        ) and not valid_token(self.headers, self.context.token):
            return self.respond({"error": "无效会话"}, status=403)
        try:
            response = ApiRouter(self.context).get(url.path, parse_qs(url.query), self.headers)
            revision = int(self.context.workflow.state.get("revision") or 0)
            self.respond(
                response.data, response.content_type, response.status, response.cache,
                headers={"X-CNS-Revision": revision},
            )
        except Exception as exc: self.respond({"error": str(exc)}, status=400)

    def do_POST(self):
        if not self.allowed() or not valid_token(self.headers, self.context.token):
            return self.respond({"error": "无效会话或来源"}, status=403)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length < 16384: raise ValueError("请求大小无效")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict): raise ValueError("请求必须是 JSON 对象")
            # Phase4-B6X：异步 heavy task 提交**不写 ProjectState**，因此不参与
            # workflow revision 乐观锁（输入一致性由任务的输入指纹在 publish 阶段
            # 校验）。同步写路径的 revision 契约完全不变。
            if wants_async_submission(self.path, payload):
                response = ApiRouter(self.context).post(self.path, payload)
                current = int(self.context.workflow.state.get("revision") or 0)
                return self.respond(
                    response.data, response.content_type, response.status, response.cache,
                    headers={"X-CNS-Revision": current},
                )
            expected = self.headers.get("X-CNS-Revision")
            if expected is None:
                raise StaleRevisionError("请求缺少 workflow revision，请刷新项目状态后重试")
            try:
                expected = int(expected)
            except (TypeError, ValueError) as exc:
                raise StaleRevisionError("请求 workflow revision 无效") from exc
            lock = getattr(self.context, "mutation_lock", None)
            if lock is None:
                raise RuntimeError("服务端缺少状态写入锁")
            with lock:
                current = int(self.context.workflow.state.get("revision") or 0)
                if expected != current:
                    raise StaleRevisionError(
                        f"项目状态已更新（请求 {expected}，当前 {current}），请刷新后重试"
                    )
                response = ApiRouter(self.context).post(self.path, payload)
                current = int(self.context.workflow.state.get("revision") or 0)
            self.respond(
                response.data, response.content_type, response.status, response.cache,
                headers={"X-CNS-Revision": current},
            )
        except StaleRevisionError as exc:
            current = int(getattr(getattr(self.context, "workflow", None), "state", {}).get("revision") or 0)
            self.respond({"error": str(exc), "revision": current}, status=409)
        except Exception as exc: self.respond({"error": str(exc)}, status=400)

    def log_message(self, format, *args): pass


def create_server(context, address=("127.0.0.1", 8765), handler=ApiHandler):
    server = LocalServer(address, handler); server.context = context
    return server
