"""Credential-isolated, fixed-upstream imagery proxy for the local 3D view."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import socket
import struct
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass, field

from .imagery_budget import (
    ImageryBudgetExhausted, ImageryBudgetGuard, ONLINE_MODE_DISABLED,
    REASON_DIAGNOSE, REASON_METADATA, REASON_TILE,
)


ARCGIS_HOST = "ibasemaps-api.arcgis.com"
ARCGIS_PATH = "/arcgis/rest/services/World_Imagery/MapServer"
TIANDITU_HOST = "t0.tianditu.gov.cn"
TIANDITU_PATH = "/img_w/wmts"
ZHOU_SHAN_BBOX = (121.45, 29.30, 123.05, 30.65)
MIN_ZOOM = 4
MAX_ZOOM = 20
MAX_TILE_BYTES = 2 * 1024 * 1024


class ImageryUnavailable(RuntimeError):
    """Sanitized user-facing proxy failure; never carries an upstream URL or token."""

    def __init__(self, code: str, message: str, **detail):
        super().__init__(message)
        self.code = code
        self.detail = detail

    def as_dict(self):
        # ``detail`` 是本异常自带的脱敏结构（上游状态码 / ArcGIS 错误码等），必须投递出去，
        # 否则 401/403/498/499 与「上游不是图像」在前端看起来完全一样，排查会被引向错误方向。
        return {"error": str(self), "code": self.code, "detail": dict(self.detail)}


@dataclass
class ImageryTileResponse:
    body: bytes = field(repr=False)
    content_type: str
    headers: dict[str, str]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _default_fetcher(url, *, headers, timeout_s, max_bytes):
    request = urllib.request.Request(url, headers=headers, method="GET")
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=timeout_s) as response:
            body = response.read(max_bytes + 1)
            return response.status, dict(response.headers.items()), body, response.geturl()
    except urllib.error.HTTPError as exc:
        body = exc.read(max_bytes + 1)
        return exc.code, dict(exc.headers.items()), body, exc.geturl()


def _tile_bounds(z: int, x: int, y: int):
    n = 2.0 ** z
    west = x / n * 360.0 - 180.0
    east = (x + 1) / n * 360.0 - 180.0
    north = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * (y + 1) / n))))
    return west, south, east, north


def _intersects_zhoushan(z: int, x: int, y: int) -> bool:
    west, south, east, north = _tile_bounds(z, x, y)
    min_lon, min_lat, max_lon, max_lat = ZHOU_SHAN_BBOX
    return east >= min_lon and west <= max_lon and north >= min_lat and south <= max_lat


def _upstream_error_code(body: bytes):
    """取上游「HTTP 200 + JSON 错误体」里的 ArcGIS 错误码；取不到返回 ``None``。

    只回传整数错误码，绝不复述 body——上游错误体可能回显凭证。
    """
    try:
        payload = json.loads(body[:2048].decode("utf-8", "replace"))
    except ValueError:
        return None
    error = payload.get("error") if isinstance(payload, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    try:
        return int(code)
    except (TypeError, ValueError):
        return None


#: 关键词后允许**任意**分隔符（``=`` / ``:`` / JSON 的 ``":`` / 空白），
#: 但不允许跨越 ``;``、`&`、`,`、`}`、`]``，避免把无关内容一起吃掉。
#:
#: 只有满足「含 ``=``/``:`` 分隔 或 值长度 ≥ 20」才判为凭证：这样
#: ``Token Required.`` 这类**普通错误文案**保持可读（诊断价值所在），
#: 而 ``token=AA...``、``"token": "AA..."``、裸的长访问令牌都会被抓到。
_CREDENTIAL_VALUE = re.compile(
    r"""(?i)\b(token|tk|access[_-]?token|api[_-]?key|apikey|secret|password|bearer)\b"""
    r"""[^=:;&,}\]]{0,8}"""
    r"""(?P<sep>[=:]\s*)?"""
    r"""(?:"[^"]*"|'[^']*'|[^\s,;&"'}\]]+)"""
)


def _scrub_credential_material(text: str) -> str:
    """把文本里形如 ``key=value`` / ``"key": "value"`` 的凭证片段替换为 ``[redacted]``。

    诊断与错误路径**只**回传上游错误消息本身，其余内容一律丢弃；本函数是最后一道
    防线，确保即使上游在消息里回显了凭证，也不会进入 API 响应、日志或交付证据。
    两种形态都必须覆盖：``token=XXX``（查询串风格）与 ``"token": "XXX"``（JSON 风格），
    后者曾被漏过——实测 ``{"token": "..."}`` 未被脱敏。
    """
    def replace(match: re.Match) -> str:
        value = match.group(0)
        # 值与关键词之间必须出现分隔符，或者值本身就长得像凭证（≥20 字符）。
        has_separator = match.group("sep") is not None
        if not has_separator and len(value) < 20:
            return value
        return f"{match.group(1)}=[redacted]"

    return _CREDENTIAL_VALUE.sub(replace, str(text))


def _image_dimensions(body: bytes):
    if body.startswith(b"\x89PNG\r\n\x1a\n") and len(body) >= 24:
        return struct.unpack(">II", body[16:24])
    if body.startswith(b"\xff\xd8"):
        offset = 2
        while offset + 9 <= len(body):
            if body[offset] != 0xFF:
                offset += 1
                continue
            marker = body[offset + 1]
            offset += 2
            if marker in (0xD8, 0xD9):
                continue
            if offset + 2 > len(body):
                break
            length = int.from_bytes(body[offset:offset + 2], "big")
            if length < 2 or offset + length > len(body):
                break
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                height = int.from_bytes(body[offset + 3:offset + 5], "big")
                width = int.from_bytes(body[offset + 5:offset + 7], "big")
                return width, height
            offset += length
    return None


def _upstream_error_message(body: bytes, limit: int = 300):
    """取上游错误体里的 ``error.message``（脱敏、限长）；取不到返回 ``None``。

    这是「为什么被拒」的唯一可读线索；除它之外不保留上游任何字节。
    """
    try:
        payload = json.loads(body[:4096].decode("utf-8", "replace"))
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None
    message = error.get("message")
    if not isinstance(message, str):
        return None
    return _scrub_credential_material(message)[:limit]


class Imagery3DService:
    """Serve only approved Zhoushan basemap tiles; credentials remain server-side."""

    def __init__(self, *, environ=None, fetcher=None, max_concurrency=6, budget=None):
        self._environ = os.environ if environ is None else environ
        self._fetcher = fetcher or _default_fetcher
        self._max_concurrency = max(1, int(max_concurrency))
        self._slots = threading.BoundedSemaphore(self._max_concurrency)
        self._metrics_lock = threading.Lock()
        self._requests = Counter()
        self._bytes = Counter()
        self._busy_rejections = 0
        #: **唯一**的上游放行闸门。默认离线（``disabled``）：发现凭证存在也**不**触网。
        #: 计数落在固定的共享目录里，进程重启、换端口、重建服务都绕不过日上限。
        self._budget = budget if budget is not None else ImageryBudgetGuard(
            environ=self._environ)

    # ------------------------------------------------------------------ 预算

    @property
    def budget(self):
        return self._budget

    def _reserve_upstream(self, source_id, reason, *, retry=False):
        """在**真正发出上游请求之前**原子预留额度；失败即本地拒绝、绝不触网。

        一次上游重试也要独立计数：重试路径会再次调用本方法。
        """

        try:
            return self._budget.reserve(source_id=source_id, reason=reason, retry=retry)
        except ImageryBudgetExhausted as exc:
            raise self._budget_unavailable(exc) from None

    def _precheck_upstream(self):
        """**不消耗额度**的放行前置判定（模式/额度），必须在读凭证**之前**调用。

        离线模式下正确的拒绝码是 ``imagery_online_mode_disabled``（运行模式未授权），
        而不是 ``arcgis_token_missing``（缺凭证）—— 二者混同会把排查引向凭证方向。
        """

        try:
            return self._budget.precheck()
        except ImageryBudgetExhausted as exc:
            raise self._budget_unavailable(exc) from None

    @staticmethod
    def _budget_unavailable(exc):
        """把预算异常翻成**同一族**的 :class:`ImageryUnavailable`（脱敏）。"""

        detail = dict(exc.detail)
        code = str(detail.get("reason") or "")
        if code == "imagery_online_mode_disabled":
            return ImageryUnavailable(
                "imagery_online_mode_disabled",
                "影像在线访问未启用（默认离线开发模式）", **detail)
        return ImageryUnavailable(
            "imagery_budget_exhausted", "影像瓦片预算已耗尽", **detail)

    def _token(self, source_id):
        key = "CNS_ARCGIS_TOKEN" if source_id == "arcgis_world_imagery" else "CNS_TIANDITU_TK"
        return str(self._environ.get(key) or "").strip()

    def _credential_format(self, source_id):
        """**只**报告凭证的形状分类，绝不回显任何字符。"""
        token = self._token(source_id)
        if not token:
            return {"present": False}
        return {
            "present": True,
            "length": len(token),
            # 只做**保守**的形状提示：ArcGIS 访问令牌是长且无空白的串。
            # 早期实现额外要求前缀 ``AA``，实测本机有效令牌长度 281 且正是 ``AA`` 开头，
            # 但反过来"不以 AA 开头"并不代表无效——把它当作失败判据会误导排查。
            "looks_like_arcgis_access_token": (len(token) >= 100 and " " not in token),
            "has_whitespace_inside": any(ch.isspace() for ch in token),
            "has_non_ascii": any(ord(ch) > 126 for ch in token),
        }

    def _intersects_scope(self, z, x, y):
        try:
            z, x, y = int(z), int(x), int(y)
        except (TypeError, ValueError):
            return False
        if not MIN_ZOOM <= z <= MAX_ZOOM:
            return False
        limit = 1 << z
        if not (0 <= x < limit and 0 <= y < limit):
            return False
        return _intersects_zhoushan(z, x, y)

    def metrics(self):
        with self._metrics_lock:
            return {
                "tile_requests": dict(self._requests),
                "tile_bytes": dict(self._bytes),
                "estimated_billable_tiles": int(sum(self._requests.values())),
                "concurrency_rejections": int(self._busy_rejections),
                "slot_wait_seconds": self.SLOT_WAIT_SECONDS,
                "process_lifetime_upstream_attempts": int(sum(self._requests.values())),
                "billing_basis": "server_side_reserved_upstream_attempts",
            }

    def budget_summary(self):
        """脱敏预算摘要（含真实上游尝试/成功/重试与剩余额度）。"""

        return self._budget.summary()

    def configuration(self):
        arcgis = bool(self._token("arcgis_world_imagery"))
        tianditu = bool(self._token("tianditu_img_w"))
        budget = self._budget.summary()
        online_allowed = bool(budget.get("online_allowed"))
        # 凭证存在**不等于**可用：默认离线模式下即使凭证齐全也不允许触网，
        # 因此状态必须显式区分"已配置但离线"与"已配置待验证"。
        if not online_allowed:
            status = "offline_mode"
            reason_code = "imagery_online_mode_disabled"
        elif arcgis:
            status = "configured_unverified"
            reason_code = None
        else:
            status = "blocked"
            reason_code = "arcgis_token_missing"
        return {
            "schema_version": "visualization-3d-imagery/v1",
            "default_source": "arcgis_world_imagery",
            "status": status,
            "reason_code": reason_code,
            "credential_exposed": False,
            "credential_present": arcgis,
            "online_mode": budget.get("request_mode"),
            "online_allowed": online_allowed,
            "budget": budget,
            "scope_bbox_crs84": list(ZHOU_SHAN_BBOX),
            "proxy": {
                "localhost_only": True,
                "persistent_cache": False,
                "min_zoom": MIN_ZOOM,
                "max_zoom": MAX_ZOOM,
                "max_concurrency": self._max_concurrency,
                "upstream_budget_gate": "imagery_budget.py:ImageryBudgetGuard",
                "default_offline": True,
            },
            "sources": [
                {
                    "id": "arcgis_world_imagery",
                    "label": "ArcGIS World Imagery",
                    "status": status if arcgis or not online_allowed else "blocked",
                    "interface": "ArcGIS Basemap Styles image tile service / World_Imagery MapServer",
                    "host": ARCGIS_HOST,
                    "tile_size": 256,
                    "tile_order": "z/y/x",
                    "zoom": [MIN_ZOOM, MAX_ZOOM],
                    "local_url": "/api/visualization-3d/imagery/arcgis_world_imagery/{z}/{x}/{y}",
                    "attribution": "Tiles © Esri — Esri, Maxar, Earthstar Geographics, and the GIS User Community",
                    "attribution_status": "official_documentation_unverified_at_runtime",
                    "required_privilege": "basemap tile access for ibasemaps-api",
                    "billing": "ArcGIS Location Platform: 2M tiles free, then USD 0.15/1000 tiles; verify account terms",
                    "budget_remaining": budget.get("budget_remaining"),
                },
                {
                    "id": "tianditu_img_w",
                    "label": "天地图影像（备用）",
                    "status": (
                        "configured_unverified" if (tianditu and online_allowed)
                        else "offline_mode" if not online_allowed else "blocked"
                    ),
                    "interface": "Tianditu img_w WMTS",
                    "host": TIANDITU_HOST,
                    "tile_size": 256,
                    "tile_order": "z/y/x translated to TILEMATRIX/TILEROW/TILECOL",
                    "zoom": [MIN_ZOOM, 18],
                    "local_url": "/api/visualization-3d/imagery/tianditu_img_w/{z}/{x}/{y}",
                    "attribution": "天地图及其数据提供者（须使用独立合法 tk）",
                    "attribution_status": "requires_runtime_account_verification",
                    "budget_remaining": budget.get("budget_remaining"),
                },
            ],
            "metrics": self.metrics(),
        }

    def tile(self, source_id: str, z: int, x: int, y: int) -> ImageryTileResponse:
        if source_id not in {"arcgis_world_imagery", "tianditu_img_w"}:
            raise ImageryUnavailable("imagery_source_unknown", "未知影像源")
        try:
            z, x, y = int(z), int(x), int(y)
        except (TypeError, ValueError) as exc:
            raise ImageryUnavailable("imagery_tile_coordinate_invalid", "影像瓦片坐标无效") from exc
        max_zoom = MAX_ZOOM if source_id == "arcgis_world_imagery" else 18
        if not MIN_ZOOM <= z <= max_zoom:
            raise ImageryUnavailable("imagery_zoom_out_of_range", "影像缩放级别超出受控范围")
        limit = 1 << z
        if not (0 <= x < limit and 0 <= y < limit):
            raise ImageryUnavailable("imagery_tile_coordinate_invalid", "影像瓦片坐标无效")
        if not _intersects_zhoushan(z, x, y):
            raise ImageryUnavailable("imagery_tile_outside_scope", "影像请求超出舟山展示范围")
        # 顺序**必须**是"授权模式 → 额度 → 凭证"：离线模式下即使凭证齐全，正确的
        # 拒绝码也是"未授权联网"，而不是"缺凭证"。
        self._precheck_upstream()
        token = self._token(source_id)
        if not token:
            code = "arcgis_token_missing" if source_id == "arcgis_world_imagery" else "tianditu_tk_missing"
            raise ImageryUnavailable(code, "影像凭证未配置")
        if not self._acquire_slot():
            raise ImageryUnavailable("imagery_proxy_busy", "影像代理并发已达上限，请稍后重试")
        try:
            return self._request_tile(source_id, z, x, y, token)
        finally:
            self._slots.release()

    #: 并发槽位的**有界等待**秒数。直接 ``acquire(blocking=False)`` 立即拒绝的实测后果：
    #: Cesium 加载时会持续占满全部槽位，任何外部探针（含本仓库的验收工具）在浏览期间
    #: 一律拿到 503 ``imagery_proxy_busy``——首轮探测因此把"并发占满"误判成"上游不可用"。
    #: 改为最多等待这么久，既不放大上游并发，也不再对外表现成随机失败。
    SLOT_WAIT_SECONDS = 5.0

    def _acquire_slot(self) -> bool:
        if self._slots.acquire(blocking=False):
            return True
        if self._slots.acquire(timeout=self.SLOT_WAIT_SECONDS):
            return True
        with self._metrics_lock:
            self._busy_rejections += 1
        return False

    def _upstream_request(self, source_id, z, x, y, token):
        """构造固定的上游请求；凭证只出现在 URL 查询串（``ibasemaps-api`` 合同要求）。

        ``ibasemaps-api`` 的 tile 端点只解析 ``token`` **查询参数**。实测同一 URL
        用 ``Authorization: Bearer``（或 ``X-Esri-Authorization``）一律回
        HTTP 200 + text/plain ``{"error":{"code":499,"message":"Token Required."}}``。
        """
        if source_id == "arcgis_world_imagery":
            url = (f"https://{ARCGIS_HOST}{ARCGIS_PATH}/tile/{z}/{y}/{x}"
                   f"?token={urllib.parse.quote(token, safe='')}")
            return url, {"Accept": "image/*"}, ARCGIS_HOST
        query = urllib.parse.urlencode({
            "SERVICE": "WMTS", "REQUEST": "GetTile", "VERSION": "1.0.0",
            "LAYER": "img", "STYLE": "default", "TILEMATRIXSET": "w",
            "FORMAT": "tiles", "TILEMATRIX": z, "TILEROW": y,
            "TILECOL": x, "tk": token,
        })
        url = f"https://{TIANDITU_HOST}{TIANDITU_PATH}?{query}"
        return url, {"Accept": "image/*"}, TIANDITU_HOST

    #: 只在**网络层瞬态失败**上重试的判定：这类失败与凭证、坐标、权限无关，
    #: 同一坐标立即重试即成功（实测 24 瓦片矩阵中有 3 例）。凭证类错误
    #: （``imagery_credential_rejected``）与尺寸/格式错误**绝不**重试，
    #: 以免把真实的授权问题掩盖成"偶发抖动"。
    _RETRYABLE_UPSTREAM_CODES = {"imagery_upstream_timeout", "imagery_upstream_unavailable"}

    def _request_tile(self, source_id, z, x, y, token, diagnose=None):
        last_error = None
        for attempt in range(1, 3):
            try:
                # ``retry=attempt > 1``：**一次上游重试也独立计数**（预留在
                # ``_request_tile_once`` 内、紧邻真实 fetcher 调用之前完成）。
                return self._request_tile_once(source_id, z, x, y, token, diagnose,
                                               retry=attempt > 1)
            except ImageryUnavailable as exc:
                last_error = exc
                if exc.code not in self._RETRYABLE_UPSTREAM_CODES:
                    raise
                if attempt == 1 and diagnose is not None:
                    diagnose["retry_reason"] = exc.code
        raise last_error

    def _request_tile_once(self, source_id, z, x, y, token, diagnose=None, *, retry=False):
        url, headers, expected_host = self._upstream_request(source_id, z, x, y, token)
        #: 预算闸门：**必须**在 fetcher 之前。预留失败即本地拒绝，绝不触网。
        reservation = self._reserve_upstream(source_id, REASON_TILE, retry=retry)
        if diagnose is not None:
            diagnose["budget"] = dict(reservation)
        try:
            status, response_headers, body, final_url = self._fetcher(
                url, headers=headers, timeout_s=8.0, max_bytes=MAX_TILE_BYTES)
        except (TimeoutError, socket.timeout):
            if diagnose is not None:
                diagnose["network_error"] = "timeout"
            raise ImageryUnavailable("imagery_upstream_timeout", "影像上游请求超时") from None
        except Exception as exc:
            if diagnose is not None:
                diagnose["network_error"] = type(exc).__name__
            # 真实观测到的原因必须能到达前端与证据：否则「上游被网络层拒绝」与
            # 「上游明确返回错误」在 503 里完全同形，排查会被引向凭证方向。
            raise ImageryUnavailable(
                "imagery_upstream_unavailable", "影像上游不可用",
                network_error=type(exc).__name__) from None
        content_type = str(response_headers.get("Content-Type") or
                           response_headers.get("content-type") or "").split(";", 1)[0].lower()
        if diagnose is not None:
            diagnose.update({
                "upstream_http_status": int(status),
                "upstream_content_type": content_type,
                "upstream_bytes": len(body),
                "upstream_magic_hex": body[:8].hex(),
                "upstream_image_dimensions": list(_image_dimensions(body) or ()),
                "upstream_error_code": _upstream_error_code(body),
                "upstream_cache": (response_headers.get("X-Cache")
                                   or response_headers.get("x-cache")),
            })
        final = urllib.parse.urlsplit(final_url)
        if final.scheme != "https" or final.hostname != expected_host:
            raise ImageryUnavailable("imagery_redirect_refused", "影像上游发生非许可重定向")
        if status in (401, 403):
            raise ImageryUnavailable("imagery_credential_rejected", "影像凭证无效或权限不足",
                                     upstream_status=int(status))
        if status != 200:
            raise ImageryUnavailable("imagery_upstream_status", "影像上游返回异常状态",
                                     upstream_status=int(status))
        if len(body) > MAX_TILE_BYTES:
            raise ImageryUnavailable("imagery_response_too_large", "影像瓦片超过大小上限")
        if content_type not in {"image/jpeg", "image/jpg", "image/png"}:
            # ArcGIS 用 HTTP 200 + JSON 错误体表达凭证问题（实测 code 498/499）。必须
            # 显式分辨，否则"凭证被拒"会被报成"上游不是图片"，把排查引向错误方向。
            upstream_code = _upstream_error_code(body)
            if upstream_code in {498, 499}:
                raise ImageryUnavailable(
                    "imagery_credential_rejected", "影像凭证无效或权限不足",
                    upstream_status=int(status), upstream_code=upstream_code)
            raise ImageryUnavailable(
                "imagery_response_not_image", "影像上游未返回图像",
                upstream_status=int(status), content_type=content_type,
                upstream_code=upstream_code)
        dimensions = _image_dimensions(body)
        if dimensions is None:
            raise ImageryUnavailable("imagery_image_invalid", "影像瓦片格式无效")
        if dimensions != (256, 256):
            raise ImageryUnavailable("imagery_dimensions_invalid", "影像瓦片尺寸不符合服务契约",
                                     width=dimensions[0], height=dimensions[1])
        with self._metrics_lock:
            self._requests[source_id] += 1
            self._bytes[source_id] += len(body)
        #: ``upstream_success`` 只统计**真的收到有效图像**（尺寸/格式/主机全部通过）。
        self._budget.record_success(source_id)
        return ImageryTileResponse(
            body=body,
            content_type="image/jpeg" if content_type in {"image/jpeg", "image/jpg"} else "image/png",
            headers={
                "Cache-Control": "private, no-store",
                "X-CNS-Imagery-Source": source_id,
                "X-CNS-Imagery-Cache": "disabled",
            },
        )

    def upstream_metadata(self, source_id="arcgis_world_imagery"):
        """读取**官方服务元数据**并只保留坐标契约相关字段（脱敏）。

        任务要求"从官方 MapServer metadata 读取并验证实际 tileInfo、spatialReference、
        origin、LOD、行列定义"，因此不能只依赖本地约定。本方法抓一次 ``?f=json``，
        解析后**只**回传坐标契约字段（tileInfo/spatialReference/fullExtent/能力与
        版权文本），不落盘、不回传任何 URL 或凭证。
        """
        if source_id != "arcgis_world_imagery":
            raise ImageryUnavailable("imagery_source_unknown", "未知影像源")
        # 同一顺序：授权模式 → 额度 → 凭证（见 ``tile`` 的说明）。
        self._precheck_upstream()
        token = self._token(source_id)
        if not token:
            raise ImageryUnavailable("arcgis_token_missing", "影像凭证未配置")
        report = {
            "source_id": source_id,
            "metadata_url_redacted": f"https://{ARCGIS_HOST}{ARCGIS_PATH}?f=json",
            "credential_present": True,
        }
        url = (f"https://{ARCGIS_HOST}{ARCGIS_PATH}?f=json"
               f"&token={urllib.parse.quote(token, safe='')}")
        # 元数据读取同样是**上游请求**：必须走同一预算闸门（否则它会成为绕过日上限的
        # 后门）。失败时在本地拒绝，绝不触网。
        reservation = self._reserve_upstream(source_id, REASON_METADATA)
        report["budget"] = dict(reservation)
        if not self._acquire_slot():
            raise ImageryUnavailable("imagery_proxy_busy", "影像代理并发已达上限")
        try:
            try:
                status, response_headers, body, final_url = self._fetcher(
                    url, headers={"Accept": "application/json"}, timeout_s=20.0,
                    max_bytes=MAX_TILE_BYTES)
            except (TimeoutError, socket.timeout):
                raise ImageryUnavailable("imagery_upstream_timeout", "影像上游请求超时") from None
            except Exception as exc:  # noqa: BLE001
                raise ImageryUnavailable("imagery_upstream_unavailable", "影像上游不可用",
                                         network_error=type(exc).__name__) from None
        finally:
            self._slots.release()
        final = urllib.parse.urlsplit(final_url)
        if final.hostname != ARCGIS_HOST:
            raise ImageryUnavailable("imagery_redirect_refused", "影像上游发生非许可重定向")
        report["upstream_http_status"] = int(status)
        if status in (401, 403):
            raise ImageryUnavailable("imagery_credential_rejected", "影像凭证无效或权限不足",
                                     upstream_status=int(status))
        try:
            payload = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            raise ImageryUnavailable("imagery_metadata_invalid", "影像服务元数据不可解析") from None
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            code = payload["error"].get("code")
            report["upstream_error_code"] = code
            if code in (498, 499):
                raise ImageryUnavailable("imagery_credential_rejected", "影像凭证无效或权限不足",
                                         upstream_code=code)
            raise ImageryUnavailable("imagery_metadata_error", "影像服务元数据返回错误",
                                     upstream_code=code)
        tile_info = payload.get("tileInfo") or {}
        lods = tile_info.get("lods") or []
        report.update({
            "service_name": payload.get("mapName") or payload.get("name"),
            "service_description": payload.get("serviceDescription"),
            "copyright_text": (payload.get("copyrightText") or "")[:400],
            "capabilities": payload.get("capabilities"),
            "spatial_reference": tile_info.get("spatialReference"),
            "service_spatial_reference": payload.get("spatialReference"),
            "rows": tile_info.get("rows"),
            "cols": tile_info.get("cols"),
            "dpi": tile_info.get("dpi"),
            "format": tile_info.get("format"),
            "compression_quality": tile_info.get("compressionQuality"),
            "origin": tile_info.get("origin"),
            "lod_count": len(lods),
            "lod_first": lods[0] if lods else None,
            "lod_last": lods[-1] if lods else None,
            "full_extent": payload.get("fullExtent"),
            "initial_extent": payload.get("initialExtent"),
            # LOD 0 的分辨率**最大**（最粗），末级分辨率最小（最细）。
            "coarsest_lod_resolution": (lods[0].get("resolution") if lods else None),
            "finest_lod_resolution": (lods[-1].get("resolution") if lods else None),
        })
        report["lod_table"] = [
            {"level": lod.get("level"), "resolution": lod.get("resolution"),
             "scale": lod.get("scale")}
            for lod in lods
        ][:32]
        return report

    def diagnose_tile(self, source_id, z, x, y):
        """对**一个**受控瓦片做真实上游调用，返回脱敏诊断报告（不返回图像体）。

        用于区分「凭证未配置 / 凭证被拒 / 上游不是图像 / 上游不可用」——
        仅凭 HTTP 200 无法判定授权，本报告提供上游状态码、MIME、字节数、
        图像魔数与像素尺寸、ArcGIS 错误码，以及**错误消息文本**（经 scrub）。
        绝不返回令牌、查询串或原始响应体。
        """
        report = {
            "source_id": source_id,
            "z": int(z), "x": int(x), "y": int(y),
            "credential_present": bool(self._token(source_id)),
            "credential_key": ("CNS_ARCGIS_TOKEN" if source_id == "arcgis_world_imagery"
                               else "CNS_TIANDITU_TK"),
            "credential_format": self._credential_format(source_id),
            "upstream_host": (ARCGIS_HOST if source_id == "arcgis_world_imagery"
                              else TIANDITU_HOST),
            "upstream_path_template": (ARCGIS_PATH + "/tile/{z}/{y}/{x}"
                                       if source_id == "arcgis_world_imagery" else TIANDITU_PATH),
            "request_transport": ("query_parameter_token"
                                  if source_id == "arcgis_world_imagery" else "wmts_tk_parameter"),
        }
        if not self._intersects_scope(z, x, y):
            report.update({"outcome": "rejected_local_scope",
                           "reason": "tile outside the approved Zhoushan display bbox"})
            return report
        # 顺序**必须**是"授权模式 → 额度 → 凭证"：离线模式下正确的 outcome 是
        # ``blocked_online_mode_disabled``，而不是 ``blocked_missing_credential``。
        # 后者会把"运行模式未授权"误报成"缺凭证"，把排查引向错误方向。
        try:
            report["budget"] = dict(self._precheck_upstream())
        except ImageryUnavailable as exc:
            outcome = ("blocked_online_mode_disabled"
                       if exc.code == "imagery_online_mode_disabled"
                       else "blocked_budget_exhausted")
            report.update({"outcome": outcome, "error_code": exc.code,
                           "error_message": str(exc)})
            report.update({k: v for k, v in exc.detail.items()})
            return report
        token = self._token(source_id)
        if not token:
            report.update({"outcome": "blocked_missing_credential",
                           "error_code": ("arcgis_token_missing"
                                          if source_id == "arcgis_world_imagery"
                                          else "tianditu_tk_missing")})
            return report
        try:
            return self._request_tile_diagnostic(source_id, z, x, y, token, report)
        except ImageryUnavailable as exc:
            report.update({"outcome": "failed", "error_code": exc.code,
                           "error_message": str(exc)})
            report.update({k: v for k, v in exc.detail.items()})
            return report
        except Exception as exc:  # noqa: BLE001 - 诊断必须如实上报未知失败
            if os.environ.get("CNS_IMAGERY_DEBUG_TRACEBACK") == "1":
                # 只在显式开启时把真实 traceback 打到 stderr（不含凭证）。
                import traceback as _traceback
                _traceback.print_exc()
            report.update({"outcome": "failed", "error_code": type(exc).__name__})
            return report

    def _request_tile_diagnostic(self, source_id, z, x, y, token, report):
        """执行真实上游调用并把**脱敏**结果写入 ``report``；不返回任何上游字节。

        最多尝试 2 次：实测上游偶发连接层失败（同一坐标重试即成功），
        报告里保留 ``attempts``、``first_attempt_error`` 与最后一次的错误码，
        既不掩盖失败次数，也不把瞬态抖动误报成授权问题。
        """
        last_exc = None
        for attempt in range(1, 3):
            report["attempts"] = attempt
            try:
                self._request_tile_diagnostic_once(source_id, z, x, y, token, report,
                                                   retry=attempt > 1)
                last_exc = None
                break
            except ImageryUnavailable as exc:
                last_exc = exc
                if attempt == 1:
                    report["first_attempt_error"] = exc.code
                    network_error = exc.detail.get("network_error")
                    if network_error:
                        report["first_attempt_network_error"] = network_error
                if exc.code not in self._RETRYABLE_UPSTREAM_CODES:
                    # 凭证被拒 / 预算耗尽 / 本地范围拒绝一律**不重试**：重试既掩盖
                    # 真实授权问题，也会在预算耗尽后继续消耗额度。
                    break
        if last_exc is not None:
            report.update({"outcome": "failed", "error_code": last_exc.code,
                           "error_message": str(last_exc)})
            # 上游错误码/消息已由 ``_request_tile_diagnostic_once`` 写入 report：
            # 否则「498/499 凭证被拒」与「网络层失败」在诊断报告里完全同形。
            report.update(dict(last_exc.detail))
            return report
        report.setdefault("outcome", "ok")
        return report

    def _request_tile_diagnostic_once(self, source_id, z, x, y, token, report, *, retry=False):
        url, headers, expected_host = self._upstream_request(source_id, z, x, y, token)
        # 诊断同样是真实上游请求：同一预算闸门，重试独立计数。
        reservation = self._reserve_upstream(source_id, REASON_DIAGNOSE, retry=retry)
        report["budget"] = dict(reservation)
        if not self._acquire_slot():
            raise ImageryUnavailable("imagery_proxy_busy", "影像代理并发已达上限")
        try:
            try:
                status, response_headers, body, final_url = self._fetcher(
                    url, headers=headers, timeout_s=15.0, max_bytes=MAX_TILE_BYTES)
            except (TimeoutError, socket.timeout):
                raise ImageryUnavailable("imagery_upstream_timeout", "影像上游请求超时") from None
            except Exception as exc:  # noqa: BLE001
                raise ImageryUnavailable(
                    "imagery_upstream_unavailable", "影像上游不可用",
                    network_error=type(exc).__name__) from None
        finally:
            self._slots.release()
        content_type = str(response_headers.get("Content-Type") or
                           response_headers.get("content-type") or "").split(";", 1)[0].lower()
        final = urllib.parse.urlsplit(final_url)
        report.update({
            "upstream_http_status": int(status),
            "upstream_content_type": content_type,
            "upstream_bytes": len(body),
            "upstream_magic_hex": body[:8].hex(),
            "upstream_error_code": _upstream_error_code(body),
            "upstream_error_message": _upstream_error_message(body),
            "upstream_host_after_request": final.hostname,
            "redirect_refused": final.hostname != expected_host,
        })
        if status in (401, 403):
            raise ImageryUnavailable("imagery_credential_rejected", "影像凭证无效或权限不足",
                                     upstream_status=int(status))
        if status != 200:
            raise ImageryUnavailable("imagery_upstream_status", "影像上游返回异常状态",
                                     upstream_status=int(status))
        if content_type not in {"image/jpeg", "image/jpg", "image/png"}:
            upstream_code = report.get("upstream_error_code")
            if upstream_code in {498, 499}:
                raise ImageryUnavailable("imagery_credential_rejected", "影像凭证无效或权限不足",
                                         upstream_status=int(status), upstream_code=upstream_code)
            raise ImageryUnavailable("imagery_response_not_image", "影像上游未返回图像",
                                     upstream_status=int(status), content_type=content_type,
                                     upstream_code=upstream_code)
        dimensions = _image_dimensions(body)
        report.update({"upstream_image_dimensions": list(dimensions or ()),
                       "image_sha256": hashlib.sha256(body).hexdigest(),
                       "image_byte_length": len(body)})
        # 必须显式判空：早期写法直接 ``dimensions != (256, 256)`` 后取 ``dimensions[0]``，
        # 当 body 不是可解析图像时 `_image_dimensions` 返回 ``None``，
        # 于是抛出 ``IndexError: tuple index out of range`` 并被上层归一成
        # ``imagery_upstream_unavailable``——真实的「凭证被拒(498)」被覆盖成「上游不可用」。
        if dimensions is None:
            raise ImageryUnavailable("imagery_image_invalid", "影像瓦片格式无效")
        if dimensions != (256, 256):
            raise ImageryUnavailable("imagery_dimensions_invalid", "影像瓦片尺寸不符合服务契约",
                                     width=dimensions[0], height=dimensions[1])
        with self._metrics_lock:
            self._requests[source_id] += 1
            self._bytes[source_id] += len(body)
        self._budget.record_success(source_id)
        report.update({"outcome": "ok", "authorization_verified": True})


def service_for(context):
    """Return one process-local proxy instance so counters and concurrency are shared."""
    service = getattr(context, "_visualization_3d_imagery_service", None)
    if service is None:
        service = Imagery3DService()
        setattr(context, "_visualization_3d_imagery_service", service)
    return service
