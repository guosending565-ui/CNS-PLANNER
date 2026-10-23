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
    TIANDITU_INTERNAL_DAILY_REQUEST_BUDGET,
    TIANDITU_INTERNAL_SESSION_REQUEST_BUDGET_BY_MODE,
    TIANDITU_INTERNAL_BUDGET_STATE_FILENAME,
)


ARCGIS_HOST = "ibasemaps-api.arcgis.com"
ARCGIS_PATH = "/arcgis/rest/services/World_Imagery/MapServer"
TIANDITU_HOST = "t0.tianditu.gov.cn"
TIANDITU_PATH = "/img_w/wmts"
ZHOU_SHAN_BBOX = (121.45, 29.30, 123.05, 30.65)
#: R7-D3B：高清影像只服务 R0005 当前工作区，避免把请求扩散到全省。
#: 该 bbox 与 ``projects/current_project.json`` 的 workspace.bbox 一致，
#: 四周各留 0.01° 作为显示缓冲（Cesium 会请求矩形边缘的相邻瓦片）。
R0005_BBOX = (122.16, 29.79, 122.295, 29.895)
#: R7-D3B：Cesium 在斜视时会出现“父级瓦片被拉伸到整屏”的假象——根因是图层矩形
#: 太贴近工作区，视锥一旦越过矩形边界，Cesium 只能拿边缘父级瓦片补洞。这里给
#: R0005 四周各留 0.2° 渲染缓冲（约 22 km，仍属舟山本岛范围），保证斜视时
#: 有完整的父级/兄弟瓦片；工作区本身仍由 ``R0005_BBOX`` 单独披露。
HD_SCOPE_BBOX = (121.96, 29.59, 122.495, 30.095)
#: R7-D3C：天地图接入只接受**官方服务端 Key**。
#:
#: 天地图 Key 在控制台分为“浏览器端”和“服务端”两种权限类型，服务端按 Key 类型
#: 校验调用来源。R7-D3B 曾把浏览器端 tk 放进服务端代理，并注入浏览器 UA/Referer
#: 绕过 301012「Key 权限类型为浏览器端」——这属于规避 Key 类型权限，R7-D3C 明确
#: 禁止。正确路径是为服务端申请/配置服务端 Key；未配置时在线高清模式标记为
#: ``pending_configuration``，只保留接口与 Sentinel-2 离线底图，不触网。
TIANDITU_SERVER_KEY_ENV = "CNS_TIANDITU_SERVER_KEY"
#: 历史变量：QGIS 项目里读到的是浏览器端 tk。只用于披露“曾存在”，代理**绝不**
#: 读取或使用它。
TIANDITU_BROWSER_KEY_ENV = "CNS_TIANDITU_TK"
#: 代理不伪造浏览器 UA/Referer；服务端 Key 的调用来源由官方授权类型决定。
PROXY_HEADER_POLICY = "no_user_agent_or_referer_spoofing"
TIANDITU_KEY_TYPE_REQUIRED = "server_side_key"
#: 天地图影像实测最高有效层级：z18。z19/z20 返回 200，但内容是
#: “此级别下，该区域无影像”占位 PNG（4769 B），绝不能当成更高清层级。
TIANDITU_MAX_ZOOM = 18
MIN_ZOOM = 4
MAX_ZOOM = 20
MAX_TILE_BYTES = 2 * 1024 * 1024
#: 在线瓦片只做**进程内**缓存（不落盘、不改变“只测试少量瓦片”的边界）。
ONLINE_CACHE_MAX = 512


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


def _intersects_r0005(z: int, x: int, y: int) -> bool:
    """高清影像的**渲染范围**守卫（R0005 + 0.2° 缓冲）。

    与 ``_intersects_zhoushan`` 分开：舟山全市范围只用于既有 ArcGIS 备用源；
    高清底图只在 R0005 周边请求，避免“看一个点却缓存全省”。缓冲不是数据下载，
    只是保证 Cesium 斜视时能拿到完整的父级/兄弟瓦片，不把边缘瓦片拉成整屏。
    """

    west, south, east, north = _tile_bounds(z, x, y)
    min_lon, min_lat, max_lon, max_lat = HD_SCOPE_BBOX
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

    def __init__(self, *, environ=None, fetcher=None, max_concurrency=6, budget=None,
                 free_budget=None, offline_source=None):
        self._environ = os.environ if environ is None else environ
        self._fetcher = fetcher or _default_fetcher
        #: Round38-OFFLINE-IMAGERY：本地离线瓦片源。为 ``None`` 时按需解析
        #: （进程级单例），因此**不**要求调用方一定注入。它只读磁盘，
        #: 与凭证、预算、在线授权状态完全无关。
        self._offline_source = offline_source
        self._max_concurrency = max(1, int(max_concurrency))
        self._slots = threading.BoundedSemaphore(self._max_concurrency)
        self._metrics_lock = threading.Lock()
        self._requests = Counter()
        self._bytes = Counter()
        self._busy_rejections = 0
        #: Round38-OFFLINE-IMAGERY：本地瓦片计数**独立**于在线请求计数。
        #: 二者绝不可相加：离线瓦片不触网、不计费，混入在线计数会让"上游请求数
        #: 为 0"这条验收判据失效。
        self._offline_tiles = 0
        self._offline_bytes = 0
        #: R7-D3B：天地图高清瓦片的**进程内** LRU 缓存。只缓存已经真实请求过的
        #: 瓦片，不预取、不落盘；命中缓存不触网、不消耗预算。ArcGIS 计费源**不**
        #: 进入缓存，避免把“重复浏览”伪装成“零请求”。
        self._online_cache = {}
        self._online_cache_hits = 0
        #: **唯一**的上游放行闸门。默认离线（``disabled``）：发现凭证存在也**不**触网。
        #: 计数落在固定的共享目录里，进程重启、换端口、重建服务都绕不过日上限。
        self._budget = budget if budget is not None else ImageryBudgetGuard(
            environ=self._environ)
        #: 天地图使用**内部请求预算**（不是官方免费配额）和独立计数文件；
        #: 绝不与 ArcGIS 计费预算共享计数。测试注入 ``budget`` 时，复用其目录与模式。
        self._free_budget = free_budget if free_budget is not None else ImageryBudgetGuard(
            budget_dir=getattr(self._budget, "budget_dir", None),
            environ=self._environ,
            mode=getattr(self._budget, "mode", None),
            test_run_id=getattr(self._budget, "_explicit_run_id", None),
            daily_limit=TIANDITU_INTERNAL_DAILY_REQUEST_BUDGET,
            session_limits=TIANDITU_INTERNAL_SESSION_REQUEST_BUDGET_BY_MODE,
            state_filename=TIANDITU_INTERNAL_BUDGET_STATE_FILENAME,
        )

    # ------------------------------------------------------------------ 预算

    @property
    def budget(self):
        return self._budget

    @property
    def free_budget(self):
        return self._free_budget

    def _budget_for(self, source_id):
        """天地图走独立免费源预算；ArcGIS 仍走计费源预算。"""

        return self._free_budget if source_id == "tianditu_img_w" else self._budget

    def _reserve_upstream(self, source_id, reason, *, retry=False):
        """在**真正发出上游请求之前**原子预留额度；失败即本地拒绝、绝不触网。

        一次上游重试也要独立计数：重试路径会再次调用本方法。
        """

        try:
            return self._budget_for(source_id).reserve(
                source_id=source_id, reason=reason, retry=retry)
        except ImageryBudgetExhausted as exc:
            raise self._budget_unavailable(exc) from None

    def _precheck_upstream(self, source_id):
        """**不消耗额度**的放行前置判定（模式/额度），必须在读凭证**之前**调用。

        离线模式下正确的拒绝码是 ``imagery_online_mode_disabled``（运行模式未授权），
        而不是 ``arcgis_token_missing``（缺凭证）—— 二者混同会把排查引向凭证方向。
        """

        try:
            return self._budget_for(source_id).precheck()
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
        if source_id == "arcgis_world_imagery":
            return str(self._environ.get("CNS_ARCGIS_TOKEN") or "").strip()
        if source_id == "tianditu_img_w":
            #: R7-D3C：只读服务端 Key；浏览器端 tk 一律不参与代理。
            return str(self._environ.get(TIANDITU_SERVER_KEY_ENV) or "").strip()
        return ""

    def _browser_key_present(self):
        """只披露“曾配置浏览器端 tk”，绝不读取其值、更不用于代理。"""

        return bool(str(self._environ.get(TIANDITU_BROWSER_KEY_ENV) or "").strip())

    def _credential_env_name(self, source_id):
        if source_id == "arcgis_world_imagery":
            return "CNS_ARCGIS_TOKEN"
        if source_id == "tianditu_img_w":
            return TIANDITU_SERVER_KEY_ENV
        return None

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
            "looks_like_arcgis_access_token": (
                source_id == "arcgis_world_imagery"
                and len(token) >= 100 and " " not in token),
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

    def _intersects_hd_scope(self, z, x, y):
        """高清源（天地图）的范围判据：R0005 工作区。"""

        try:
            z, x, y = int(z), int(x), int(y)
        except (TypeError, ValueError):
            return False
        if not MIN_ZOOM <= z <= TIANDITU_MAX_ZOOM:
            return False
        limit = 1 << z
        if not (0 <= x < limit and 0 <= y < limit):
            return False
        return _intersects_r0005(z, x, y)

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
                #: 本地离线瓦片：只读磁盘，**零**上游、**零**预算消耗。
                #: 单独成项，便于验收直接证明"离线模式上游请求数 = 0"。
                "offline_tile_requests": int(self._offline_tiles),
                "offline_tile_bytes": int(self._offline_bytes),
                "offline_upstream_attempts": 0,
                #: 高清瓦片的内存缓存命中数：命中不触网、不计费、不计入上游请求。
                "online_cache_hits": int(self._online_cache_hits),
                "online_cache_entries": len(self._online_cache),
            }

    def budget_summary(self):
        """脱敏预算摘要（含真实上游尝试/成功/重试与剩余额度）。"""

        return self._budget.summary()

    def configuration(self, offline_source=None):
        arcgis = bool(self._token("arcgis_world_imagery"))
        #: R7-D3C：高清源只认官方**服务端 Key**。浏览器端 tk 即使存在也不使用，
        #: 只作为“曾配置但不能用于服务端代理”的事实披露。
        tianditu_server = bool(self._token("tianditu_img_w"))
        browser_tk_present = self._browser_key_present()
        budget = self._budget.summary()
        free_budget = self._free_budget.summary()
        online_allowed = bool(budget.get("online_allowed")
                              or free_budget.get("online_allowed"))
        # R7-D3C：只有真正配置了**服务端 Key**，天地图才能成为在线默认源；
        # 否则继续用 ArcGIS 备用源。高清在线模式本身单独标记为待配置，绝不因为
        # 浏览器端 tk 存在就假装“已配置”。
        default_source = ("tianditu_img_w" if tianditu_server
                          else ("arcgis_world_imagery" if arcgis
                                else "arcgis_world_imagery"))
        # Round38-OFFLINE-IMAGERY：本地离线卫星影像与在线源**并列**披露。
        # 它不读凭证、不占预算、不触网，因此其状态与 ``online_allowed`` 完全无关：
        # 即使在线被禁用（默认），离线影像依然可用。
        offline = None
        source = offline_source if offline_source is not None else self._offline_source
        if source is None:
            from ..gis.offline_imagery_adapter import source_for as offline_source_for
            source = offline_source_for()
        try:
            offline = source.descriptor()
        except Exception as exc:  # noqa: BLE001 - 离线源故障不得让整个 config 不可用
            offline = {
                "id": "offline_zhoushan_imagery",
                "label": "舟山离线卫星影像（Sentinel-2 L2A）",
                "status": "blocked",
                "reason_code": "offline_imagery_unavailable",
                "detail": {"reason": str(exc)[:200]},
                "local_url": "/api/visualization-3d/imagery/offline_zhoushan_imagery/{z}/{x}/{y}",
                "tile_size": 256,
                "is_satellite_imagery": True,
                "requires_credential": False,
                "consumes_online_budget": False,
                "attribution": "Copernicus Sentinel-2 L2A（本地离线瓦片）",
            }
        # 凭证存在**不等于**可用：默认离线模式下即使凭证齐全也不允许触网，
        # 因此状态必须显式区分"已配置但离线"与"已配置待验证"。
        if not online_allowed:
            status = "offline_mode"
            reason_code = "imagery_online_mode_disabled"
        elif arcgis or tianditu_server:
            status = "configured_unverified"
            reason_code = None
        else:
            status = "blocked"
            reason_code = "arcgis_token_missing"
        #: 高清在线模式的独立状态：没有服务端 Key ⇒ ``pending_configuration``，
        #: 而不是“已配置”。这条状态必须在前端置灰并显示真实缺失项。
        if not tianditu_server:
            hd_status = "pending_configuration"
            hd_reason_code = "tianditu_server_key_not_configured"
        elif not online_allowed:
            hd_status = "offline_mode"
            hd_reason_code = "imagery_online_mode_disabled"
        else:
            hd_status = "configured_unverified"
            hd_reason_code = None
        hd_available = bool(tianditu_server and online_allowed)
        hd_authorization_status = ("pending_configuration" if not tianditu_server
                                   else "not_verified")
        #: 顶层 authorization_status 仍是“是否完成权威授权核验”的既有契约：
        #: 本系统从未核验账号身份，因此恒为 not_verified；高清源的待配置状态由
        #: ``hd_status`` / ``hd_authorization_status`` 单独表达，避免改变旧语义。
        authorization_status = "not_verified"
        return {
            "schema_version": "visualization-3d-imagery/v1",
            "default_source": default_source,
            "status": status,
            "reason_code": reason_code,
            "credential_exposed": False,
            "credential_present": arcgis,
            "tianditu_credential_present": tianditu_server,
            "tianditu_server_key_present": tianditu_server,
            #: 只披露“浏览器端 tk 曾存在”，明确它不参与服务端代理。
            "tianditu_browser_tk_present_not_used": browser_tk_present,
            "authorization_verified": False,
            "authorization_status": authorization_status,
            "hd_authorization_status": hd_authorization_status,
            "authorization_note": (
                "未取得天地图服务端授权/Key，在线高清模式待配置；"
                "浏览器端 tk 不得用于服务端代理，也不得以伪装 UA/Referer 绕过 Key 类型"
            ),
            "hd_status": hd_status,
            "hd_reason_code": hd_reason_code,
            "hd_pending_configuration": hd_status == "pending_configuration",
            "official_access_method": TIANDITU_KEY_TYPE_REQUIRED,
            "proxy_header_policy": PROXY_HEADER_POLICY,
            "missing_configuration": (
                ([] if online_allowed else ["CNS_IMAGERY_ONLINE_MODE"])
                + ([] if arcgis else ["CNS_ARCGIS_TOKEN"])),
            #: 高清底图专用的缺失项：与旧 ArcGIS 缺失项分开，避免把
            #: “没有 ArcGIS 令牌”误读成“高清影像可用”。
            "hd_missing_configuration": (
                ([] if online_allowed else ["CNS_IMAGERY_ONLINE_MODE"])
                + ([] if tianditu_server else [TIANDITU_SERVER_KEY_ENV])),
            "online_mode": budget.get("request_mode"),
            "online_allowed": online_allowed,
            "budget": budget,
            "free_source_budget": free_budget,
            #: R7-D3C：内部请求预算与官方配额分开披露，绝不把内部阈值冒充官方额度。
            "tianditu_internal_request_budget": free_budget,
            "tianditu_official_quota": {
                "verified": False,
                "value": None,
                "note": "未取得天地图官方书面配额；内部请求预算仅用于防止批量下载",
            },
            "scope_bbox_crs84": list(ZHOU_SHAN_BBOX),
            #: 高清影像的**工作区级**范围：页面用它限制 Cesium 的 imagery rectangle，
            #: 服务端也用同一 bbox 拒绝范围外瓦片。两者必须同源，不能各写一份。
            "hd_scope_bbox_crs84": list(R0005_BBOX),
            "hd_layer_bbox_crs84": list(HD_SCOPE_BBOX),
            "hd_source_id": "tianditu_img_w",
            "hd_available": hd_available,
            "hd_max_zoom": TIANDITU_MAX_ZOOM,
            #: 只有在服务端授权已配置且在线模式开启时才声明交付分辨率；
            #: 待配置状态绝不引用 R7-D3B 的实测数字来暗示“已经可用”。
            "hd_delivered_m_per_px": 0.52 if hd_available else None,
            "hd_resolution_note": (
                ("天地图影像 z18、EPSG:3857；256 px 瓦片在 R0005 纬度（29.84°N）"
                 "约覆盖 132.6 m（经度 0.001373°），折合约 0.52 m/px。"
                 "z19/z20 只返回“此级别下，该区域无影像”占位图，不能宣称更高分辨率。")
                if hd_available else
                "在线高清模式待配置：未取得官方服务端 Key/授权，不声明交付分辨率；"
                "默认底图为 Sentinel-2 10 m 离线瓦片。"),
            "default_basemap_mode": (
                "offline_imagery" if offline and offline.get("status") == "available"
                else "offline_relief"),
            "offline_imagery": offline,
            "proxy": {
                "localhost_only": True,
                "persistent_cache": False,
                "memory_cache_entries": ONLINE_CACHE_MAX,
                "min_zoom": MIN_ZOOM,
                "max_zoom": MAX_ZOOM,
                "max_concurrency": self._max_concurrency,
                "upstream_budget_gate": "imagery_budget.py:ImageryBudgetGuard",
                "default_offline": True,
                #: R7-D3C：代理只注入官方要求的凭证参数；不伪造浏览器身份。
                "header_policy": PROXY_HEADER_POLICY,
                "browser_key_accepted": False,
                "credential_env": {
                    "arcgis_world_imagery": "CNS_ARCGIS_TOKEN",
                    "tianditu_img_w": TIANDITU_SERVER_KEY_ENV,
                },
            },
            "sources": [
                offline,
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
                    "budget_kind": "internal_request_budget",
                    "official_quota_verified": False,
                },
                {
                    "id": "tianditu_img_w",
                    "label": "天地图高清影像（z18）",
                    "status": hd_status,
                    "reason_code": hd_reason_code,
                    "interface": "Tianditu img_w WMTS（EPSG:3857）",
                    "host": TIANDITU_HOST,
                    "tile_size": 256,
                    "tile_order": "z/y/x translated to TILEMATRIX/TILEROW/TILECOL",
                    "zoom": [MIN_ZOOM, TIANDITU_MAX_ZOOM],
                    "coverage_crs84": list(HD_SCOPE_BBOX),
                    "work_area_crs84": list(R0005_BBOX),
                    "local_url": "/api/visualization-3d/imagery/tianditu_img_w/{z}/{x}/{y}",
                    "attribution": "天地图 © 国家基础地理信息中心 · GS(2026)4921号",
                    "attribution_status": "official_site_displays_approval_number",
                    "required_privilege": "服务端 Key（官方服务端授权类型；浏览器端 tk 不得用于代理）",
                    "credential_env": TIANDITU_SERVER_KEY_ENV,
                    "browser_key_accepted": False,
                    "proxy_header_policy": PROXY_HEADER_POLICY,
                    "authorization_status": authorization_status,
                    "billing": (
                        "内部请求预算（非官方免费配额）；官方配额与授权条款未取得书面确认"),
                    "budget_kind": "internal_request_budget",
                    "official_quota_verified": False,
                    "budget_remaining": free_budget.get("budget_remaining"),
                    "pending_configuration": hd_status == "pending_configuration",
                    "pending_reason": (
                        "未配置官方服务端 Key；在线高清模式待配置，默认使用 Sentinel-2 离线底图"
                        if hd_status == "pending_configuration" else None),
                    "delivered_m_per_px": 0.52 if hd_available else None,
                    "max_effective_zoom": TIANDITU_MAX_ZOOM,
                    "resolution_evidence": (
                        "z18 实测为 256×256 影像；z19/z20 为“无影像”占位图"
                        if hd_available else "授权未配置时不声明交付分辨率"),
                },
            ],
            "metrics": self.metrics(),
        }

    def tile(self, source_id: str, z: int, x: int, y: int) -> ImageryTileResponse:
        # Round38-OFFLINE-IMAGERY：本地离线影像走**完全独立**的一条路径——
        # 不读凭证、不查预算、不触网。把它混进下面的在线分支会让"离线"依赖
        # 在线授权状态（那正是本轮要消除的耦合）。
        if source_id == "offline_zhoushan_imagery":
            return self._offline_tile(z, x, y)
        if source_id not in {"arcgis_world_imagery", "tianditu_img_w"}:
            raise ImageryUnavailable("imagery_source_unknown", "未知影像源")
        try:
            z, x, y = int(z), int(x), int(y)
        except (TypeError, ValueError) as exc:
            raise ImageryUnavailable("imagery_tile_coordinate_invalid", "影像瓦片坐标无效") from exc
        max_zoom = (MAX_ZOOM if source_id == "arcgis_world_imagery"
                    else TIANDITU_MAX_ZOOM)
        if not MIN_ZOOM <= z <= max_zoom:
            raise ImageryUnavailable("imagery_zoom_out_of_range", "影像缩放级别超出受控范围")
        limit = 1 << z
        if not (0 <= x < limit and 0 <= y < limit):
            raise ImageryUnavailable("imagery_tile_coordinate_invalid", "影像瓦片坐标无效")
        # 天地图高清只允许 R0005 工作区；ArcGIS 备用源保留舟山全市范围。
        in_scope = (_intersects_r0005(z, x, y)
                    if source_id == "tianditu_img_w" else _intersects_zhoushan(z, x, y))
        if not in_scope:
            raise ImageryUnavailable("imagery_tile_outside_scope", "影像请求超出舟山展示范围")
        # 内存缓存命中：不触网、不占预算；返回同一字节与同一脱敏头。
        cached = self._cache_get(source_id, z, x, y)
        if cached is not None:
            return cached
        # 顺序**必须**是"授权模式 → 额度 → 凭证"：离线模式下即使凭证齐全，正确的
        # 拒绝码也是"未授权联网"，而不是"缺凭证"。
        self._precheck_upstream(source_id)
        token = self._token(source_id)
        if not token:
            if source_id == "tianditu_img_w":
                raise ImageryUnavailable(
                    "tianditu_server_key_not_configured",
                    "天地图服务端 Key 未配置（在线高清模式待配置）",
                    credential_env=TIANDITU_SERVER_KEY_ENV,
                    browser_key_accepted=False)
            raise ImageryUnavailable("arcgis_token_missing", "影像凭证未配置")
        if not self._acquire_slot():
            raise ImageryUnavailable("imagery_proxy_busy", "影像代理并发已达上限，请稍后重试")
        try:
            response = self._request_tile(source_id, z, x, y, token)
            self._cache_put(source_id, z, x, y, response)
            return response
        finally:
            self._slots.release()

    # ------------------------------------------------------------------ 缓存

    @staticmethod
    def _cache_key(source_id, z, x, y):
        return f"{source_id}/{int(z)}/{int(x)}/{int(y)}"

    def _cache_get(self, source_id, z, x, y):
        if source_id != "tianditu_img_w":
            return None
        with self._metrics_lock:
            response = self._online_cache.pop(self._cache_key(source_id, z, x, y), None)
            if response is None:
                return None
            # 命中后重新插入，维持 LRU 顺序。
            self._online_cache[self._cache_key(source_id, z, x, y)] = response
            self._online_cache_hits += 1
            return response

    def _cache_put(self, source_id, z, x, y, response):
        if source_id != "tianditu_img_w":
            return
        with self._metrics_lock:
            key = self._cache_key(source_id, z, x, y)
            self._online_cache.pop(key, None)
            self._online_cache[key] = response
            while len(self._online_cache) > ONLINE_CACHE_MAX:
                self._online_cache.pop(next(iter(self._online_cache)))

    #: 并发槽位的**有界等待**秒数。直接 ``acquire(blocking=False)`` 立即拒绝的实测后果：
    #: Cesium 加载时会持续占满全部槽位，任何外部探针（含本仓库的验收工具）在浏览期间
    #: 一律拿到 503 ``imagery_proxy_busy``——首轮探测因此把"并发占满"误判成"上游不可用"。
    #: 改为最多等待这么久，既不放大上游并发，也不再对外表现成随机失败。
    SLOT_WAIT_SECONDS = 5.0

    def _offline_tile(self, z, x, y):
        """读取本地离线瓦片；**不**读凭证、**不**查预算、**不**触网。

        瓦片不存在时抛 ``ImageryUnavailable('offline_imagery_tile_missing')``。
        该码与在线源的 ``imagery_tile_outside_scope`` 语义不同：前者是"本地数据
        本来就没有这一块"（数据范围边缘，属正常），后者是"请求超出受控展示范围"。
        前端按码分别处理，**绝不**把前者当成持续重试的理由。
        """

        try:
            z, x, y = int(z), int(x), int(y)
        except (TypeError, ValueError) as exc:
            raise ImageryUnavailable(
                "imagery_tile_coordinate_invalid", "影像瓦片坐标无效") from exc
        from ..gis.offline_imagery_adapter import source_for as offline_source_for
        source = offline_source_for()
        body = source.tile(z, x, y)
        if body is None:
            raise ImageryUnavailable(
                "offline_imagery_tile_missing", "本地离线影像无此瓦片",
                z=z, x=x, y=y)
        with self._metrics_lock:
            self._offline_tiles += 1
            self._offline_bytes += len(body)
        return ImageryTileResponse(
            body=body,
            content_type="image/png",
            headers={
                # 本地派生瓦片是确定性产物：同 z/x/y 必然同字节，可私有缓存。
                "Cache-Control": "private, max-age=86400",
                # 显式声明真实来源，避免被误读为 ArcGIS World Imagery。
                "X-CNS-Imagery-Source": "offline_zhoushan_imagery",
                "X-CNS-Imagery-Origin": "copernicus_sentinel2_l2a_tci",
                "X-CNS-Imagery-Online-Budget": "not_consumed",
            })

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
        # R7-D3C：只发官方 GetTile 参数与服务端 Key；**不**注入浏览器 UA / Referer。
        # 用浏览器端 tk 伪装浏览器身份绕过 301012 属于规避 Key 类型权限，明确禁止。
        return url, {"Accept": "image/*", "Connection": "close"}, TIANDITU_HOST

    #: 只在**网络层瞬态失败**上重试的判定：这类失败与凭证、坐标、权限无关，
    #: 同一坐标立即重试即成功（实测 24 瓦片矩阵中有 3 例）。凭证类错误
    #: （``imagery_credential_rejected``）与尺寸/格式错误**绝不**重试，
    #: 以免把真实的授权问题掩盖成"偶发抖动"。
    _RETRYABLE_UPSTREAM_CODES = {"imagery_upstream_timeout", "imagery_upstream_unavailable"}

    def _request_tile(self, source_id, z, x, y, token, diagnose=None):
        last_error = None
        # 天地图实测连接层偶发重置；免费源允许多一次瞬态重试（仍独立计预算）。
        attempts = 3 if source_id == "tianditu_img_w" else 2
        for attempt in range(1, attempts + 1):
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
        self._budget_for(source_id).record_success(source_id)
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
        self._precheck_upstream(source_id)
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
            "authorization_verified": False,
            "authorization_status": "not_verified",
            "image_fetch_succeeded": False,
            "credential_present": bool(self._token(source_id)),
            "credential_key": self._credential_env_name(source_id),
            "browser_key_accepted": False,
            "proxy_header_policy": PROXY_HEADER_POLICY,
            "credential_format": self._credential_format(source_id),
            "upstream_host": (ARCGIS_HOST if source_id == "arcgis_world_imagery"
                              else TIANDITU_HOST),
            "upstream_path_template": (ARCGIS_PATH + "/tile/{z}/{y}/{x}"
                                       if source_id == "arcgis_world_imagery" else TIANDITU_PATH),
            "request_transport": ("query_parameter_token"
                                  if source_id == "arcgis_world_imagery" else "wmts_tk_parameter"),
        }
        if source_id == "tianditu_img_w":
            in_scope = self._intersects_hd_scope(z, x, y)
        else:
            in_scope = self._intersects_scope(z, x, y)
        if not in_scope:
            report.update({"outcome": "rejected_local_scope",
                           "reason": "tile outside the approved Zhoushan display bbox"})
            return report
        # 顺序**必须**是"授权模式 → 额度 → 凭证"：离线模式下正确的 outcome 是
        # ``blocked_online_mode_disabled``，而不是 ``blocked_missing_credential``。
        # 后者会把"运行模式未授权"误报成"缺凭证"，把排查引向错误方向。
        try:
            report["budget"] = dict(self._precheck_upstream(source_id))
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
            if source_id == "tianditu_img_w":
                report.update({
                    "outcome": "blocked_pending_configuration",
                    "authorization_status": "pending_configuration",
                    "error_code": "tianditu_server_key_not_configured",
                    "credential_env": TIANDITU_SERVER_KEY_ENV,
                    "browser_key_present_not_used": self._browser_key_present(),
                })
                return report
            report.update({"outcome": "blocked_missing_credential",
                           "error_code": "arcgis_token_missing"})
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
        self._budget_for(source_id).record_success(source_id)
        # A valid image proves delivery, not account identity, privilege scope or licensing.
        report.update({"outcome": "ok", "image_fetch_succeeded": True})


def service_for(context):
    """Return one process-local proxy instance so counters and concurrency are shared."""
    service = getattr(context, "_visualization_3d_imagery_service", None)
    if service is None:
        service = Imagery3DService()
        setattr(context, "_visualization_3d_imagery_service", service)
    return service
