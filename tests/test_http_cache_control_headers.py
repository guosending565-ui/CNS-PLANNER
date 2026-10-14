"""Round37-T3D-2B：HTTP 响应头的**真实 socket** 回归（Cache-Control 不重复）。

背景（``07_open_data_limitations.md`` D3）：``ApiHandler.respond`` 旧实现先无条件发送
默认 ``Cache-Control``，再追加调用方显式 headers，于是自带该头的端点会发出**两个**
``Cache-Control``：三维状态端点得到 ``['no-store','no-store']``，而
``terrain/layer.json`` 得到 ``['no-store','private, max-age=86400']`` —— 意图与实现
矛盾，最终取值取决于客户端实现。

本测试**不是**断言 Python 字典：它启动真实 ``cns_planner.api.server``，用
``http.client`` 读**原始响应头列表**并统计 ``Cache-Control`` 的出现次数。
"""

from __future__ import annotations

import http.client
import socket
import threading
from types import SimpleNamespace

import pytest

from cns_planner.api.server import create_server


class StubService:
    """只读三维服务的替身：只提供路由会用到的响应面（不触任何数据源）。"""

    TERRAIN = "private, max-age=86400"

    def identity_seed(self):
        return {"revision": 468, "stub": True}

    def terrain_layer_json(self):
        return {"tilejson": "2.1.0", "stub": True}

    def terrain_tile(self, z, x, y):
        return SimpleNamespace(
            body=b"\x00\x01", etag="terrainetag",
            headers={"Cache-Control": self.TERRAIN, "Content-Encoding": "none"},
        )

    def relief_tile(self, z, x, y):
        return b"png-bytes", {"vertical_exaggeration": 1.0}

    def terrain_window(self, query):
        return b"f32-frame", "application/octet-stream", "windowetag"

    def communication_volumes(self, query):
        return {"volumes": [], "volume_count": 0}

    def rid_footprints(self, query):
        return {"footprints": [], "footprint_count": 0}

    def radar_volumes(self, query):
        return {"volumes": [], "geometry_count": 0}

    def manifest(self, query):
        return {"layer_ids": [], "load_budget": {"manifest_bytes": 0}}

    def routes(self, query):
        return {"routes": []}


class StubImageryTile:
    body = b"jpeg-bytes"
    content_type = "image/jpeg"
    #: 商业影像代理**继续** no-store（不允许任何中间缓存）。
    headers = {"Cache-Control": "private, no-store"}


class StubImageryService:
    def tile(self, source_id, z, x, y):
        return StubImageryTile()

    def configuration(self):
        return {"status": "blocked", "online_allowed": False}


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture()
def http_server(monkeypatch):
    """真实 HTTP server + 真实路由 + 替身三维服务（无外部数据依赖）。"""

    from cns_planner.application import (
        visualization_3d_imagery_service, visualization_3d_service,
    )

    monkeypatch.setattr(visualization_3d_service, "service_for",
                        lambda context: StubService())
    monkeypatch.setattr(visualization_3d_imagery_service, "service_for",
                        lambda context: StubImageryService())

    context = SimpleNamespace(
        root=None,
        token="cache-token",
        workflow=SimpleNamespace(state={"revision": 468}),
        data=SimpleNamespace(paths={}, error=None, metadata=lambda: {}),
        qgis=SimpleNamespace(call=lambda function: function()),
        mutation_lock=threading.RLock(),
    )
    port = _free_port()
    server = create_server(context, address=("127.0.0.1", port))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        server.shutdown()
        server.server_close()


def raw_get(port, path, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        body = response.read()
        status = response.status
        response_headers = list(response.getheaders())
    finally:
        connection.close()
    return status, response_headers, body


def header_values(header_list, name):
    lowered = name.lower()
    return [value for key, value in header_list if key.lower() == lowered]


# ---------------------------------------------------------------- 无重复头


@pytest.mark.parametrize("path", [
    "/api/visualization-3d/communication-volumes",
    "/api/visualization-3d/rid-footprints",
    "/api/visualization-3d/radar-volumes",
    "/api/visualization-3d/protection-volumes",
    "/api/visualization-3d/intrusion-assessment",
    "/api/visualization-3d/manifest",
    "/api/visualization-3d/routes",
    "/api/visualization-3d/terrain-window",
    "/api/visualization-3d/terrain/layer.json",
    "/api/visualization-3d/terrain/12/3400/1600.terrain",
    "/api/visualization-3d/relief/12/3400/1600.png",
    "/api/visualization-3d/imagery/config",
    "/api/visualization-3d/imagery/arcgis_world_imagery/12/3400/1600",
])
def test_every_response_carries_exactly_one_cache_control(http_server, path):
    status, header_list, _body = raw_get(http_server, path)
    values = header_values(header_list, "Cache-Control")
    assert len(values) == 1, f"{path} 发出了 {len(values)} 个 Cache-Control：{values}"
    assert status in (200, 304, 400, 404, 500, 503), (path, status)


def test_state_and_diagnostic_endpoints_are_not_cached(http_server):
    for path in ("/api/visualization-3d/communication-volumes",
                 "/api/visualization-3d/rid-footprints",
                 "/api/visualization-3d/radar-volumes",
                 "/api/visualization-3d/protection-volumes",
                 "/api/visualization-3d/intrusion-assessment",
                 "/api/visualization-3d/imagery/config"):
        _status, header_list, _body = raw_get(http_server, path)
        assert header_values(header_list, "Cache-Control") == ["no-store"], path


def test_commercial_imagery_proxy_stays_no_store(http_server):
    _status, header_list, _body = raw_get(
        http_server, "/api/visualization-3d/imagery/arcgis_world_imagery/12/3400/1600")
    # 显式响应头优先：代理声明的 ``private, no-store`` 必须原样生效，**不是**默认值。
    assert header_values(header_list, "Cache-Control") == ["private, no-store"]


def test_terrain_derived_resources_use_one_consistent_policy(http_server):
    for path in ("/api/visualization-3d/terrain/layer.json",
                 "/api/visualization-3d/terrain/12/3400/1600.terrain",
                 "/api/visualization-3d/relief/12/3400/1600.png"):
        status, header_list, _body = raw_get(http_server, path)
        assert status == 200, (path, status)
        assert header_values(header_list, "Cache-Control") == ["private, max-age=86400"], path


def test_etag_and_304_keep_the_same_cache_policy(http_server):
    # 地形元数据：200 → 304，缓存策略必须逐字一致（否则浏览器会丢弃缓存副本，
    # ETag 校验形同虚设：每个瓦片都被重新完整传输）。
    status, headers_200, _body = raw_get(
        http_server, "/api/visualization-3d/terrain/layer.json")
    assert status == 200
    # 元数据端点的 ETag 来自 stable_fingerprint（stub 身份），先取回真实值。
    etag = header_values(headers_200, "ETag")[0]
    status, headers_304, body = raw_get(
        http_server, "/api/visualization-3d/terrain/layer.json",
        {"If-None-Match": etag})
    assert status == 304
    assert body == b""
    assert header_values(headers_304, "ETag") == [etag]
    assert (header_values(headers_304, "Cache-Control")
            == header_values(headers_200, "Cache-Control") == ["private, max-age=86400"])

    # 地形瓦片：tile.headers 自带的策略同样必须在 304 上原样保留。
    status, headers_200, _body = raw_get(
        http_server, "/api/visualization-3d/terrain/12/3400/1600.terrain")
    assert status == 200
    tile_etag = header_values(headers_200, "ETag")[0]
    assert tile_etag == '"terrainetag"'
    status, headers_304, body = raw_get(
        http_server, "/api/visualization-3d/terrain/12/3400/1600.terrain",
        {"If-None-Match": tile_etag})
    assert status == 304
    assert body == b""
    assert (header_values(headers_304, "Cache-Control")
            == header_values(headers_200, "Cache-Control") == ["private, max-age=86400"])


def test_read_only_3d_geometry_endpoints_are_not_cached_even_on_304(http_server):
    status, headers_200, _body = raw_get(
        http_server, "/api/visualization-3d/radar-volumes")
    assert status == 200
    etag = header_values(headers_200, "ETag")[0]
    status, headers_304, _body = raw_get(
        http_server, "/api/visualization-3d/radar-volumes", {"If-None-Match": etag})
    assert status == 304
    assert header_values(headers_304, "Cache-Control") == ["no-store"]


def test_revision_header_is_still_sent_once(http_server):
    _status, header_list, _body = raw_get(
        http_server, "/api/visualization-3d/radar-volumes")
    assert header_values(header_list, "X-CNS-Revision") == ["468"]
    assert header_values(header_list, "X-Content-Type-Options") == ["nosniff"]
