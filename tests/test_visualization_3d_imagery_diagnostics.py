"""T3D-BMAP-2 新增能力回归：网络层有界重试 + 脱敏诊断 + 官方元数据坐标契约。

对应生产改动（`cns_planner/application/visualization_3d_imagery_service.py`
与 `cns_planner/api/router.py`）：

1. 上游**网络层瞬态失败**只做一次有界重试，凭证/尺寸类错误**绝不**重试；
2. `diagnose_tile` 把「上游不是图像」与「凭证被拒」分清，并回传**脱敏**原因；
3. `upstream_metadata` 只回传坐标契约字段（tileInfo/spatialReference/origin/LOD）；
4. 三条只读 GET 路由：config / metadata / diagnose。

所有用例都断言凭证字符串不出现在任何返回值、异常或证据结构里。

Round37-T3D-2A 补充：这些用例全部使用 mock fetcher（**0 真实上游请求**），
并且注入**隔离的**预算目录 —— 单元测试绝不触碰生产共享计数文件，也绝不
因为"默认离线"以外的原因触网。
"""

from __future__ import annotations

import json
import os
import uuid
from urllib.parse import parse_qs, urlsplit

import pytest

from cns_planner.application.imagery_budget import ImageryBudgetGuard
from cns_planner.application.visualization_3d_imagery_service import (
    ARCGIS_HOST,
    Imagery3DService,
    ImageryUnavailable,
    _scrub_credential_material,
    _upstream_error_message,
)
from cns_planner.api.router import ApiRouter

TOKEN = "AA-top-secret-token-value"


def isolated_budget(mode="smoke"):
    """隔离预算目录：单元测试绝不触碰生产共享计数文件（0 真实上游请求）。"""

    root = os.path.join(
        os.environ.get("TEMP") or os.environ.get("TMP") or ".", "cns_imagery_budget_test")
    path = os.path.join(root, uuid.uuid4().hex[:10])
    os.makedirs(path, exist_ok=True)
    return ImageryBudgetGuard(budget_dir=path, mode=mode, environ={})


def png_tile(width=256, height=256):
    import struct

    return b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + struct.pack(">II", width, height) + b"tile"


def arcgis_error_tile(code=499, message="Token Required."):
    return json.dumps({"error": {"code": code, "message": message,
                                 "details": []}}).encode("utf-8")


class SequenceFetcher:
    """按顺序返回预设结果，并记录每次调用。

    耗尽后**重复最后一项**：真实 fetcher 不会因为"被调用两次"而自己失败，
    替身若在第二次调用抛 ``IndexError``，会把上游真实的 498/499 覆盖成
    "上游不可用"——那正是本轮真实踩到的诊断陷阱。
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, *, headers, timeout_s, max_bytes):
        self.calls.append({"url": url, "headers": headers, "timeout_s": timeout_s,
                           "max_bytes": max_bytes})
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item


def json_response(payload):
    return (200, {"Content-Type": "application/json"},
            json.dumps(payload).encode("utf-8"), f"https://{ARCGIS_HOST}/meta")


def test_network_layer_failure_is_retried_once_then_succeeds():
    fetcher = SequenceFetcher([
        ConnectionResetError("upstream dropped the connection"),
        (200, {"Content-Type": "image/jpeg"}, png_tile(), "https://" + ARCGIS_HOST + "/t.png"),
    ])
    service = Imagery3DService(environ={"CNS_ARCGIS_TOKEN": TOKEN}, fetcher=fetcher,
                               max_concurrency=1, budget=isolated_budget())
    response = service.tile("arcgis_world_imagery", 12, 3438, 1689)
    assert response.content_type == "image/jpeg"
    assert len(fetcher.calls) == 2
    assert service.metrics()["estimated_billable_tiles"] == 1
    # 一次上游重试**独立计数**：预算里必须看到 2 次 attempt、1 次 retry、1 次 success。
    budget = service.budget_summary()
    assert budget["upstream_attempts"] == 2
    assert budget["retry_attempts"] == 1
    assert budget["upstream_success"] == 1
    assert budget["daily_upstream_requests"] == 2
    assert TOKEN not in repr(response)
    assert TOKEN not in json.dumps(budget, ensure_ascii=False)


def test_credential_rejection_is_never_retried():
    # 498/499 是凭证问题，重试只会掩盖真实授权失败 → 必须只调用一次上游。
    fetcher = SequenceFetcher([
        (200, {"Content-Type": "text/plain"}, arcgis_error_tile(499),
         "https://" + ARCGIS_HOST + "/t.png"),
        (200, {"Content-Type": "image/jpeg"}, png_tile(), "https://" + ARCGIS_HOST + "/t.png"),
    ])
    service = Imagery3DService(environ={"CNS_ARCGIS_TOKEN": TOKEN}, fetcher=fetcher,
                               max_concurrency=1, budget=isolated_budget())
    with pytest.raises(ImageryUnavailable) as exc:
        service.tile("arcgis_world_imagery", 12, 3438, 1689)
    assert exc.value.code == "imagery_credential_rejected"
    assert exc.value.detail.get("upstream_code") == 499
    assert len(fetcher.calls) == 1
    assert TOKEN not in repr(exc.value) and TOKEN not in json.dumps(exc.value.as_dict())


def test_diagnose_reports_sanitized_cause_and_credential_shape_only():
    fetcher = SequenceFetcher([
        (200, {"Content-Type": "text/plain"},
         json.dumps({"error": {"code": 498, "message": f"Invalid token {TOKEN}"}}).encode(),
         "https://" + ARCGIS_HOST + "/t.png"),
    ])
    service = Imagery3DService(environ={"CNS_ARCGIS_TOKEN": TOKEN}, fetcher=fetcher,
                               max_concurrency=1, budget=isolated_budget())
    report = service.diagnose_tile("arcgis_world_imagery", 12, 3438, 1689)
    assert report["outcome"] == "failed"
    assert report["error_code"] == "imagery_credential_rejected"
    assert report["upstream_error_code"] == 498
    assert report["credential_format"]["length"] == len(TOKEN)
    blob = json.dumps(report, ensure_ascii=False)
    assert TOKEN not in blob
    assert "[redacted]" in blob
    # 诊断同样是真实上游请求 → 必须留下预算证据（脱敏，不含 URL/凭证）。
    assert report["budget"]["daily_upstream_requests"] == 1
    # 脱敏后的形态是 ``token=[redacted]``（关键词保留、值被抹掉），因此这里断言的是
    # **值**不外泄与**完整查询串**不出现，而不是断言字面量 ``token=`` 不存在。
    assert TOKEN not in blob
    assert "token=AA" not in blob
    assert "?token=" not in blob


def test_diagnose_outside_scope_never_calls_upstream():
    fetcher = SequenceFetcher([])
    service = Imagery3DService(environ={"CNS_ARCGIS_TOKEN": TOKEN}, fetcher=fetcher,
                               max_concurrency=1, budget=isolated_budget())
    report = service.diagnose_tile("arcgis_world_imagery", 10, 100, 500)
    assert report["outcome"] == "rejected_local_scope"
    assert fetcher.calls == []
    # 本地拒绝**不得**消耗额度。
    assert service.budget_summary()["daily_upstream_requests"] == 0


def test_metadata_records_official_tileinfo_and_never_leaks_token():
    payload = {
        "mapName": "World_Imagery",
        "copyrightText": "Esri, Maxar",
        "capabilities": "TilesOnly,Image",
        "tileInfo": {
            "rows": 256, "cols": 256, "dpi": 96, "format": "JPEG",
            "compressionQuality": 90,
            "origin": {"x": -2.0037508342787e7, "y": 2.0037508342787e7},
            "spatialReference": {"wkid": 102100, "latestWkid": 3857},
            "lods": [
                {"level": 0, "resolution": 156543.03392800014, "scale": 5.9e8},
                {"level": 23, "resolution": 0.01866138385915496, "scale": 70.0},
            ],
        },
        "fullExtent": {"xmin": -2.0e7, "ymin": -2.0e7, "xmax": 2.0e7, "ymax": 2.0e7},
    }
    fetcher = SequenceFetcher([json_response(payload)])
    service = Imagery3DService(environ={"CNS_ARCGIS_TOKEN": TOKEN}, fetcher=fetcher,
                               max_concurrency=1, budget=isolated_budget())
    report = service.upstream_metadata()
    assert report["spatial_reference"]["latestWkid"] == 3857
    assert report["origin"]["y"] == pytest.approx(2.0037508342787e7)
    assert report["rows"] == 256 and report["cols"] == 256
    assert report["lod_count"] == 2
    assert report["lod_last"]["level"] == 23
    assert report["coarsest_lod_resolution"] == pytest.approx(156543.03392800014)
    assert report["finest_lod_resolution"] == pytest.approx(0.01866138385915496)
    parts = urlsplit(fetcher.calls[0]["url"])
    assert parts.hostname == ARCGIS_HOST
    assert parse_qs(parts.query)["f"] == ["json"]
    assert parse_qs(parts.query)["token"] == [TOKEN]
    assert TOKEN not in json.dumps(report, ensure_ascii=False)


def test_metadata_error_body_is_reported_as_credential_rejection():
    fetcher = SequenceFetcher([
        (200, {"Content-Type": "application/json"}, arcgis_error_tile(499),
         "https://" + ARCGIS_HOST + "/meta"),
    ])
    service = Imagery3DService(environ={"CNS_ARCGIS_TOKEN": TOKEN}, fetcher=fetcher,
                               max_concurrency=1, budget=isolated_budget())
    with pytest.raises(ImageryUnavailable) as exc:
        service.upstream_metadata()
    assert exc.value.code == "imagery_credential_rejected"
    assert exc.value.detail.get("upstream_code") == 499


def test_scrubber_removes_any_key_value_credential_material():
    assert TOKEN not in _scrub_credential_material(f"failed for token={TOKEN} please retry")
    assert TOKEN not in _scrub_credential_material(f'{{"token": "{TOKEN}"}}')
    assert _upstream_error_message(arcgis_error_tile(499)) == "Token Required."
    assert _upstream_error_message(png_tile()) is None


def test_router_serves_config_metadata_and_diagnose_as_read_only_get():
    service = Imagery3DService(
        environ={"CNS_ARCGIS_TOKEN": TOKEN},
        fetcher=SequenceFetcher([
            json_response({"mapName": "World_Imagery", "tileInfo": {"lods": []}}),
            (200, {"Content-Type": "image/jpeg"}, png_tile(),
             "https://" + ARCGIS_HOST + "/t.png"),
        ]),
        max_concurrency=1,
        budget=isolated_budget(),
    )
    context = type("Ctx", (), {"_visualization_3d_imagery_service": service})()
    router = ApiRouter.__new__(ApiRouter)
    router._visualization_3d = ApiRouter._visualization_3d.__get__(router, ApiRouter)

    config = router._visualization_3d(
        context, "/api/visualization-3d/imagery/config", {}, {})
    assert config.status == 200
    assert config.data["budget"]["daily_limit"] == 150
    metadata = router._visualization_3d(
        context, "/api/visualization-3d/imagery/metadata", {}, {})
    assert metadata.status == 200
    diagnose = router._visualization_3d(
        context, "/api/visualization-3d/imagery/diagnose", {"z": ["12"]}, {})
    assert diagnose.status == 200
    assert TOKEN not in json.dumps(diagnose.data, ensure_ascii=False)
    assert TOKEN not in json.dumps(metadata.data, ensure_ascii=False)
    assert TOKEN not in json.dumps(config.data, ensure_ascii=False)


def test_image_success_does_not_claim_credential_authorization():
    service = Imagery3DService(
        environ={"CNS_ARCGIS_TOKEN": TOKEN}, budget=isolated_budget(),
        fetcher=SequenceFetcher([(200, {"Content-Type": "image/png"}, png_tile(),
                                  "https://" + ARCGIS_HOST + "/t.png")]))
    report = service.diagnose_tile("arcgis_world_imagery", 12, 3438, 1689)
    assert report["outcome"] == "ok"
    assert report["image_fetch_succeeded"] is True
    assert report["authorization_verified"] is False
    assert report["authorization_status"] == "not_verified"


def test_configuration_lists_missing_settings_without_upstream_calls():
    fetcher = SequenceFetcher([])
    service = Imagery3DService(environ={}, fetcher=fetcher,
                               budget=isolated_budget("disabled"))
    config = service.configuration()
    assert config["missing_configuration"] == ["CNS_IMAGERY_ONLINE_MODE", "CNS_ARCGIS_TOKEN"]
    assert config["authorization_status"] == "not_verified"
    assert fetcher.calls == []
