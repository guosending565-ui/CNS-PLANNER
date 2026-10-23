import os
import struct
import uuid
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from cns_planner.application.imagery_budget import ImageryBudgetGuard
from cns_planner.application.visualization_3d_imagery_service import (
    ARCGIS_HOST,
    Imagery3DService,
    ImageryUnavailable,
)
from cns_planner.application.visualization_3d_service import Visualization3DService
from cns_planner.api.router import ApiRouter


def png_tile(width=256, height=256):
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + struct.pack(">II", width, height) + b"tile"


def isolated_budget(mode="smoke", *, run_id=None, budget_dir=None):
    """隔离的预算目录：单元测试**绝不**触碰生产共享计数文件。

    这些用例全部使用 mock fetcher（0 真实上游请求）；预算守卫在这里只是让
    "显式授权后才允许触网"的闸门在测试中同样成立。
    """

    root = budget_dir or os.path.join(
        os.environ.get("TEMP") or os.environ.get("TMP") or ".", "cns_imagery_budget_test")
    path = os.path.join(root, uuid.uuid4().hex[:10])
    os.makedirs(path, exist_ok=True)
    return ImageryBudgetGuard(budget_dir=path, mode=mode, test_run_id=run_id, environ={})


class Fetcher:
    def __init__(self, *, status=200, content_type="image/png", body=None,
                 final_url=None, error=None):
        self.status = status
        self.content_type = content_type
        self.body = png_tile() if body is None else body
        self.final_url = final_url
        self.error = error
        self.calls = []

    def __call__(self, url, *, headers, timeout_s, max_bytes):
        self.calls.append({"url": url, "headers": headers, "timeout_s": timeout_s,
                           "max_bytes": max_bytes})
        if self.error:
            raise self.error
        return self.status, {"Content-Type": self.content_type}, self.body, self.final_url or url


def arcgis_service(fetcher=None, token="top-secret", *, mode="smoke"):
    return Imagery3DService(
        environ={"CNS_ARCGIS_TOKEN": token},
        fetcher=fetcher or Fetcher(),
        max_concurrency=1,
        budget=isolated_budget(mode),
    )


def assert_code(exc, code):
    assert exc.value.code == code
    assert "top-secret" not in repr(exc.value)


def test_missing_runtime_credentials_is_explicitly_blocked():
    config = Imagery3DService(environ={}).configuration()
    assert config["default_source"] == "arcgis_world_imagery"
    # 默认离线：无凭证且未授权在线 → 仍是"不可用"，但原因码必须精确到**离线模式**，
    # 不能把"未授权在线"和"缺凭证"混成同一个码。
    assert config["status"] == "offline_mode"
    assert config["reason_code"] == "imagery_online_mode_disabled"
    assert config["online_allowed"] is False
    assert config["credential_exposed"] is False
    assert "services.arcgisonline.com" not in repr(config)


def test_credential_presence_alone_never_enables_online_access():
    # 任务硬要求：**不因发现存在 CNS_ARCGIS_TOKEN 就自动开启在线访问**。
    config = Imagery3DService(environ={"CNS_ARCGIS_TOKEN": "top-secret"}).configuration()
    assert config["status"] == "offline_mode"
    assert config["online_allowed"] is False
    assert config["credential_present"] is True
    assert config["budget"]["request_mode"] == "disabled"
    assert config["budget"]["budget_remaining"] == 0


def test_source_selection_prefers_tianditu_hd_and_keeps_arcgis_as_backup():
    config = Imagery3DService(
        environ={"CNS_ARCGIS_TOKEN": "top-secret",
                 "CNS_TIANDITU_SERVER_KEY": "server-secret"},
        budget=isolated_budget("smoke"),
    ).configuration()
    assert config["status"] == "configured_unverified"
    assert config["online_allowed"] is True
    # R7-D3B：高清底图优先天地图；ArcGIS 仍作为显式备用源并列披露。
    # Round38-OFFLINE-IMAGERY：本地离线源与两个在线源**并列**披露，且排在最前。
    ids = [row["id"] for row in config["sources"]]
    assert ids == ["offline_zhoushan_imagery", "arcgis_world_imagery", "tianditu_img_w"]
    assert config["default_source"] == "tianditu_img_w"
    by_id = {row["id"]: row for row in config["sources"]}
    assert by_id["tianditu_img_w"]["status"] == "configured_unverified"
    assert by_id["tianditu_img_w"]["credential_env"] == "CNS_TIANDITU_SERVER_KEY"
    assert by_id["tianditu_img_w"]["browser_key_accepted"] is False
    # 离线源**不**依赖凭证，也不消耗在线预算：这两条必须显式可验证。
    assert by_id["offline_zhoushan_imagery"]["requires_credential"] is False
    assert by_id["offline_zhoushan_imagery"]["consumes_online_budget"] is False
    assert by_id["offline_zhoushan_imagery"]["is_satellite_imagery"] is True
    assert config["default_basemap_mode"] in {"offline_imagery", "offline_relief"}
    assert config["hd_available"] is True
    assert config["hd_scope_bbox_crs84"] == [122.16, 29.79, 122.295, 29.895]
    assert config["hd_layer_bbox_crs84"] == [121.96, 29.59, 122.495, 30.095]
    # R7-D3C：内部请求预算与官方配额必须分开披露；内部阈值不是官方免费额度。
    budget = config["tianditu_internal_request_budget"]
    assert budget["session_limit"] == 60
    assert budget["daily_limit"] == 1200
    assert budget["state_file"] == "imagery_budget_tianditu.json"
    assert budget["budget_kind"] == "internal_request_budget"
    assert budget["official_quota_verified"] is False
    assert config["tianditu_official_quota"]["verified"] is False
    assert config["tianditu_official_quota"]["value"] is None
    assert "secret" not in repr(config)


def test_browser_tk_is_disclosed_but_never_used_for_the_server_proxy():
    # R7-D3C 硬约束：不得用浏览器端 tk + 伪装 UA/Referer 绕过 Key 类型权限。
    config = Imagery3DService(
        environ={"CNS_ARCGIS_TOKEN": "top-secret", "CNS_TIANDITU_TK": "browser-secret"},
        budget=isolated_budget("manual"),
    ).configuration()
    hd = next(row for row in config["sources"] if row["id"] == "tianditu_img_w")
    assert config["tianditu_server_key_present"] is False
    assert config["tianditu_browser_tk_present_not_used"] is True
    assert config["hd_status"] == "pending_configuration"
    assert config["hd_reason_code"] == "tianditu_server_key_not_configured"
    assert config["hd_available"] is False
    assert config["hd_delivered_m_per_px"] is None
    assert config["hd_authorization_status"] == "pending_configuration"
    assert hd["status"] == "pending_configuration"
    assert hd["browser_key_accepted"] is False
    assert hd["required_privilege"].startswith("服务端 Key")
    assert config["proxy_header_policy"] == "no_user_agent_or_referer_spoofing"
    assert "browser-secret" not in repr(config)


def test_browser_tk_does_not_enable_hd_tiles_and_fails_closed():
    fetcher = Fetcher(content_type="image/jpeg")
    service = Imagery3DService(
        environ={"CNS_TIANDITU_TK": "browser-secret"},
        fetcher=fetcher,
        budget=isolated_budget("manual"),
    )
    with pytest.raises(ImageryUnavailable) as exc:
        service.tile("tianditu_img_w", 18, 220075, 108286)
    assert exc.value.code == "tianditu_server_key_not_configured"
    assert fetcher.calls == []


@pytest.mark.parametrize("z,x,y", [(3, 6, 3), (21, 0, 0), (12, -1, 1691), (12, 3438, 5000)])
def test_tile_rejects_zoom_coordinates_or_tiles_outside_zhoushan(z, x, y):
    with pytest.raises(ImageryUnavailable) as exc:
        arcgis_service().tile("arcgis_world_imagery", z, x, y)
    assert exc.value.code in {"imagery_zoom_out_of_range", "imagery_tile_outside_scope",
                              "imagery_tile_coordinate_invalid"}


def test_arcgis_request_is_fixed_host_path_and_passes_token_as_query_parameter():
    # 上游 ibasemaps-api 的 tile 端点只解析 ``token`` 查询参数：同一个 URL 用
    # ``Authorization: Bearer`` 一律回 200 + text/plain 499 "Token Required."，
    # 会让代理把"凭证被忽略"误判成 imagery_response_not_image（实测满屏 503）。
    fetcher = Fetcher()
    response = arcgis_service(fetcher).tile("arcgis_world_imagery", 12, 3438, 1691)
    call = fetcher.calls[0]
    parts = urlsplit(call["url"])
    assert parts.hostname == ARCGIS_HOST
    assert parts.path == "/arcgis/rest/services/World_Imagery/MapServer/tile/12/1691/3438"
    assert parse_qs(parts.query)["token"] == ["top-secret"]
    assert "Authorization" not in call["headers"]
    assert call["timeout_s"] == 8.0
    assert response.content_type == "image/png"
    assert response.headers["Cache-Control"] == "private, no-store"
    assert "top-secret" not in repr(response)


def test_tianditu_request_is_fixed_wmts_and_never_returns_server_key():
    fetcher = Fetcher()
    service = Imagery3DService(
        environ={"CNS_ARCGIS_TOKEN": "top-secret",
                 "CNS_TIANDITU_SERVER_KEY": "server-secret"},
        fetcher=fetcher,
        budget=isolated_budget("smoke"),
    )
    response = service.tile("tianditu_img_w", 12, 3438, 1691)
    parts = urlsplit(fetcher.calls[0]["url"])
    query = parse_qs(parts.query)
    assert parts.hostname == "t0.tianditu.gov.cn"
    assert parts.path == "/img_w/wmts"
    assert query["TILEMATRIX"] == ["12"]
    assert query["TILEROW"] == ["1691"]
    assert query["TILECOL"] == ["3438"]
    assert query["tk"] == ["server-secret"]
    assert "server-secret" not in repr(response)


def test_cross_host_redirect_is_refused():
    fetcher = Fetcher(final_url="https://evil.example/tile.png")
    with pytest.raises(ImageryUnavailable) as exc:
        arcgis_service(fetcher).tile("arcgis_world_imagery", 12, 3438, 1691)
    assert_code(exc, "imagery_redirect_refused")


@pytest.mark.parametrize(
    "fetcher,code",
    [
        (Fetcher(status=401, content_type="application/json", body=b'{"token":"top-secret"}'),
         "imagery_credential_rejected"),
        (Fetcher(status=403, content_type="text/html", body=b"top-secret"),
         "imagery_credential_rejected"),
        (Fetcher(error=TimeoutError("request to ?token=top-secret timed out")),
         "imagery_upstream_timeout"),
        (Fetcher(content_type="application/json", body=b"{}"), "imagery_response_not_image"),
        # 真实故障（浏览器实测）：上游只认 ``token`` 查询参数，改用 Bearer 时回
        # HTTP 200 + text/plain 499，必须报"凭证被拒"而不是"上游不是图片"。
        (Fetcher(content_type="text/plain",
                 body=b'{"error":{"code":499,"message":"Token Required.","details":[]}}'),
         "imagery_credential_rejected"),
        (Fetcher(content_type="image/png", body=b"not-an-image"), "imagery_image_invalid"),
        (Fetcher(content_type="image/png", body=png_tile(512, 512)), "imagery_dimensions_invalid"),
    ],
)
def test_upstream_failures_are_sanitized(fetcher, code):
    with pytest.raises(ImageryUnavailable) as exc:
        arcgis_service(fetcher).tile("arcgis_world_imagery", 12, 3438, 1691)
    assert_code(exc, code)


def test_non_image_upstream_error_reports_sanitized_detail():
    # 失败必须可诊断：detail 要能区分"上游不是图片"与"凭证被拒"，且绝不回显凭证。
    fetcher = Fetcher(content_type="text/plain", body=b"<html>gateway</html>")
    with pytest.raises(ImageryUnavailable) as exc:
        arcgis_service(fetcher).tile("arcgis_world_imagery", 12, 3438, 1691)
    assert_code(exc, "imagery_response_not_image")
    assert exc.value.detail["content_type"] == "text/plain"
    assert exc.value.detail["upstream_status"] == 200
    assert exc.value.detail["upstream_code"] is None


def test_concurrency_limit_waits_bounded_then_fails_without_calling_upstream(monkeypatch):
    # 语义在 T3D-BMAP-2 明确为"**有界等待**后拒绝"：直接 fail-fast 会让 Cesium 占满槽位
    # 期间的所有外部探针（含本仓库验收工具）拿到随机 503，实测被误判成"上游不可用"。
    # 这里把等待时间缩短到 0.05 s 以保持用例秒级，并断言等待期间**绝不**调用上游。
    monkeypatch.setattr(Imagery3DService, "SLOT_WAIT_SECONDS", 0.05)
    fetcher = Fetcher()
    service = arcgis_service(fetcher)
    assert service._slots.acquire(blocking=False)
    try:
        with pytest.raises(ImageryUnavailable) as exc:
            service.tile("arcgis_world_imagery", 12, 3438, 1691)
    finally:
        service._slots.release()
    assert_code(exc, "imagery_proxy_busy")
    assert fetcher.calls == []
    assert service.metrics()["concurrency_rejections"] == 1
    assert service.metrics()["slot_wait_seconds"] == 0.05


def test_manifest_includes_only_sanitized_imagery_identity():
    service = Visualization3DService(
        {},
        imagery_service=Imagery3DService(
            environ={"CNS_ARCGIS_TOKEN": "top-secret"},
            budget=isolated_budget("smoke")),
    )
    manifest = service.manifest()
    assert manifest["imagery"]["default_source"] == "arcgis_world_imagery"
    assert manifest["imagery"]["status"] == "configured_unverified"
    assert manifest["imagery"]["online_allowed"] is True
    assert "top-secret" not in repr(manifest)
    assert "imagery" in service.identity_seed()


def test_router_exposes_only_fixed_imagery_config_and_tile_paths(monkeypatch):
    tile = png_tile()

    class Stub:
        def configuration(self):
            return {"status": "blocked", "reason_code": "arcgis_token_missing"}

        def tile(self, source_id, z, x, y):
            assert (source_id, z, x, y) == ("arcgis_world_imagery", 12, 3438, 1691)
            from cns_planner.application.visualization_3d_imagery_service import ImageryTileResponse
            return ImageryTileResponse(tile, "image/png", {"Cache-Control": "private, no-store"})

    import cns_planner.application.visualization_3d_imagery_service as imagery_module
    monkeypatch.setattr(imagery_module, "service_for", lambda context: Stub())
    context = SimpleNamespace(workflow=SimpleNamespace(state={}), data=None)
    router = ApiRouter(context)

    config = router.get("/api/visualization-3d/imagery/config", {}, {})
    assert config.data["status"] == "blocked"
    assert config.headers["Cache-Control"] == "no-store"
    response = router.get(
        "/api/visualization-3d/imagery/arcgis_world_imagery/12/3438/1691", {}, {})
    assert response.data == tile
    assert response.content_type == "image/png"
    unknown = router.get("/api/visualization-3d/imagery/evil/12/3438/1691", {}, {})
    assert unknown.status == 404


def hd_service(fetcher):
    return Imagery3DService(
        environ={"CNS_TIANDITU_SERVER_KEY": "server-secret"},
        fetcher=fetcher,
        max_concurrency=1,
        budget=isolated_budget("smoke"),
    )


def test_hd_tianditu_uses_official_server_key_without_browser_header_spoofing():
    fetcher = Fetcher(content_type="image/jpeg")
    service = hd_service(fetcher)
    # R0005 内：z18 / x220075 / y108286（WebMercator）。
    response = service.tile("tianditu_img_w", 18, 220075, 108286)
    assert response.content_type == "image/jpeg"
    call = fetcher.calls[0]
    assert call["url"].startswith("https://t0.tianditu.gov.cn/img_w/wmts?")
    assert "tk=server-secret" in call["url"]
    # R7-D3C：不注入浏览器 UA / Referer；服务端 Key 由官方授权类型决定来源。
    assert "User-Agent" not in call["headers"]
    assert "Referer" not in call["headers"]
    assert call["headers"]["Accept"] == "image/*"
    assert "server-secret" not in repr(response)

    # R0005 外（且超出 0.2° 渲染缓冲）：高清源必须本地拒绝，且不能触网。
    with pytest.raises(ImageryUnavailable) as exc:
        service.tile("tianditu_img_w", 18, 219000, 108286)
    assert exc.value.code == "imagery_tile_outside_scope"
    assert len(fetcher.calls) == 1


def test_hd_tianditu_rejects_z19_placeholder_tiles():
    fetcher = Fetcher(content_type="image/jpeg")
    service = hd_service(fetcher)
    with pytest.raises(ImageryUnavailable) as exc:
        service.tile("tianditu_img_w", 19, 440150, 216572)
    assert exc.value.code == "imagery_zoom_out_of_range"
    assert fetcher.calls == []


def test_hd_tianditu_memory_cache_avoids_second_upstream_request():
    fetcher = Fetcher(content_type="image/jpeg")
    service = hd_service(fetcher)
    first = service.tile("tianditu_img_w", 18, 220075, 108286)
    second = service.tile("tianditu_img_w", 18, 220075, 108286)
    assert first.body == second.body
    assert len(fetcher.calls) == 1
    assert service.metrics()["online_cache_hits"] == 1
    assert service.metrics()["tile_requests"] == {"tianditu_img_w": 1}
