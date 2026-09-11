"""HTTP server and transport-only request handler."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
from urllib.parse import parse_qs, urlparse

from .router import ApiRouter
from .security import same_origin, valid_token


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

    def respond(self, data, content_type="application/json; charset=utf-8", status=200, cache=False):
        if not isinstance(data, bytes): data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=86400" if cache else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "frame-ancestors 'self' http://127.0.0.1:8501 http://localhost:8501")
        self.end_headers()
        try: self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError): pass

    def allowed(self): return same_origin(self.headers)

    def do_GET(self):
        if not self.allowed(): return self.respond({"error": "仅允许本机同源访问"}, status=403)
        url = urlparse(self.path)
        if url.path in ("/api/online-health", "/api/browse") and not valid_token(self.headers, self.context.token):
            return self.respond({"error": "无效会话"}, status=403)
        try:
            response = ApiRouter(self.context).get(url.path, parse_qs(url.query), self.headers)
            self.respond(response.data, response.content_type, response.status, response.cache)
        except Exception as exc: self.respond({"error": str(exc)}, status=400)

    def do_POST(self):
        if not self.allowed() or not valid_token(self.headers, self.context.token):
            return self.respond({"error": "无效会话或来源"}, status=403)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length < 16384: raise ValueError("请求大小无效")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict): raise ValueError("请求必须是 JSON 对象")
            response = ApiRouter(self.context).post(self.path, payload)
            self.respond(response.data, response.content_type, response.status, response.cache)
        except Exception as exc: self.respond({"error": str(exc)}, status=400)

    def log_message(self, format, *args): pass


def create_server(context, address=("127.0.0.1", 8765), handler=ApiHandler):
    server = LocalServer(address, handler); server.context = context
    return server
