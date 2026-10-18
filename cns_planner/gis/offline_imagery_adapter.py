"""本地离线卫星影像瓦片源（Round38-OFFLINE-IMAGERY）。

为什么需要它
------------
三维底图此前只有两条路：ArcGIS 在线影像（每次浏览消耗瓦片请求预算）或
FABDEM 地形晕渲（**不是**卫星影像）。演示时在线影像很快耗尽 manual 会话预算，
而离线演示又完全没有真实地表纹理。

本模块从**本地磁盘**读取预先制作的 Web Mercator XYZ PNG 瓦片，提供：

* 完全离线的真彩色卫星影像底图，**不建立任何上游链路**，不消耗在线请求预算，
  不依赖 ``CNS_ARCGIS_TOKEN`` 或任何 API Key；
* 与在线源**同一**的 XYZ 瓦片契约，因此前端可复用既有 ``ImageryLayer`` 与
  模式切换状态机，不需要第二套渲染路径。

数据边界（必须如实披露，不得含糊）
----------------------------------
* 影像来源是 **Copernicus Sentinel-2 L2A TCI**（10 m 真彩色），许可是
  Copernicus Sentinel Data Terms / CC BY-SA 3.0 IGO，署名必须保留。
  它**不是** ArcGIS World Imagery，绝不能被标注成后者。
* 原始地面分辨率就是 10 m；Web Mercator 重投影后像元为 11.5373 m，在演示
  纬度折算地面分辨率恰为 10.0 m。**没有**任何升采样，因此最大瓦片层级止于
  z=14（≈8.3 m/px），不提供更细的假高清层级。
* 本模块**只读**：绝不写 ``ProjectState``，不触碰地形高度、EGM2008 垂直基准
  或任何 CNS 规划业务逻辑。

瓦片缺失的语义
--------------
图层矩形之外或数据范围之外的瓦片**不存在**。此时返回 ``None``，由调用方转成
``404``——前端据此静默跳过，而不是把 404 当成"底图故障"持续重试。这与在线源的
``503 imagery_tile_outside_scope`` 是**不同**的两件事，必须分开表达。
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

#: 瓦片源默认根目录（D 盘独立可配置目录，**不**放进 Git 仓库）。
DEFAULT_TILE_ROOT = Path(
    r"D:\Projects\_cns_workspaces\derived\round38_offline_imagery")
#: 环境变量覆盖：便于换盘符 / 换数据集而不改代码。
TILE_ROOT_ENV = "CNS_OFFLINE_IMAGERY_DIR"

#: 与在线源一致的瓦片契约。
TILE_SIZE = 256
MIN_ZOOM = 9
MAX_ZOOM = 14
#: 本地瓦片 URL 路径模板（与 manifest 对外披露的一致）。
URL_TEMPLATE = "/api/visualization-3d/imagery/offline_zhoushan_imagery/{z}/{x}/{y}"

_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


class OfflineImageryUnavailable(RuntimeError):
    """本地离线影像不可用（目录缺失 / 元信息无效）；消息面向用户，中文可读。"""

    def __init__(self, code: str, message: str, **detail):
        super().__init__(message)
        self.code = code
        self.detail = detail

    def as_dict(self):
        return {"error": str(self), "code": self.code, "detail": dict(self.detail)}


class OfflineImageryTileSource:
    """只读的本地 XYZ PNG 瓦片源。

    :param root: 瓦片根目录；``None`` 时读 :data:`TILE_ROOT_ENV` 或默认值。
    :param dataset: 数据集子目录名（默认取元信息里的 ``tiles.dataset``）。
    """

    def __init__(self, root=None, dataset=None, environ=None):
        env = os.environ if environ is None else environ
        configured = str(env.get(TILE_ROOT_ENV) or "").strip()
        self.root = Path(root) if root is not None else (
            Path(configured) if configured else DEFAULT_TILE_ROOT)
        self.dataset = dataset or "zhoushan_s2b_tci"
        self._lock = threading.Lock()
        self._metadata = None
        self._metadata_error = None

    # ------------------------------------------------------------ 元信息

    @property
    def metadata_path(self) -> Path:
        return self.root / "meta" / "offline_imagery_metadata.json"

    @property
    def tiles_dir(self) -> Path:
        return self.root / "tiles" / self.dataset

    def metadata(self):
        """读取并缓存元信息；不可用时抛 :class:`OfflineImageryUnavailable`。

        元信息是**唯一**的覆盖范围与署名来源：宁可显式不可用，也不猜范围、
        不硬编码 bbox（硬编码会在换数据集后静默错位）。
        """

        with self._lock:
            if self._metadata is not None:
                return self._metadata
            if self._metadata_error is not None:
                raise self._metadata_error
            try:
                with self.metadata_path.open("r", encoding="utf-8") as handle:
                    payload = json.load(handle)
            except FileNotFoundError:
                error = OfflineImageryUnavailable(
                    "offline_imagery_metadata_missing",
                    "本地离线影像元信息不存在；请先生成离线瓦片数据",
                    metadata_path=str(self.metadata_path))
                self._metadata_error = error
                raise error from None
            except (OSError, ValueError) as exc:
                error = OfflineImageryUnavailable(
                    "offline_imagery_metadata_invalid",
                    "本地离线影像元信息无法解析", reason=str(exc)[:200])
                self._metadata_error = error
                raise error from None
            self._metadata = payload
            return payload

    def available(self) -> bool:
        try:
            self.metadata()
        except OfflineImageryUnavailable:
            return False
        return self.tiles_dir.is_dir()

    def coverage(self):
        """数据真实覆盖范围（CRS84 ``[w, s, e, n]``）；不可用时返回 ``None``。"""

        try:
            meta = self.metadata()
        except OfflineImageryUnavailable:
            return None
        bbox = meta.get("coverage_crs84")
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            return None
        try:
            return [float(value) for value in bbox]
        except (TypeError, ValueError):
            return None

    def zoom_range(self):
        try:
            tiles = (self.metadata().get("tiles") or {})
        except OfflineImageryUnavailable:
            return (MIN_ZOOM, MAX_ZOOM)
        low = tiles.get("minzoom")
        high = tiles.get("maxzoom")
        try:
            return (int(low), int(high))
        except (TypeError, ValueError):
            return (MIN_ZOOM, MAX_ZOOM)

    def attribution(self) -> str:
        try:
            license_block = self.metadata().get("license") or {}
        except OfflineImageryUnavailable:
            return "Copernicus Sentinel-2 L2A（本地离线瓦片）"
        text = str(license_block.get("attribution_text") or "").strip()
        return text or "Contains modified Copernicus Sentinel data"

    def descriptor(self):
        """对外披露的只读描述（供 ``/imagery/config`` 使用）。

        ``available=False`` 时给出**真实原因码**，让前端能明确区分"离线影像没做"
        与"离线影像加载失败"，而不是让用户看到一片空白再猜。
        """

        try:
            meta = self.metadata()
        except OfflineImageryUnavailable as exc:
            return {
                "id": "offline_zhoushan_imagery",
                "label": "舟山离线卫星影像（Sentinel-2 L2A）",
                "status": "blocked",
                "reason_code": exc.code,
                "detail": dict(exc.detail),
                "local_url": URL_TEMPLATE,
                "tile_size": TILE_SIZE,
                "zoom": [MIN_ZOOM, MAX_ZOOM],
                "is_satellite_imagery": True,
                "requires_credential": False,
                "consumes_online_budget": False,
                "attribution": "Copernicus Sentinel-2 L2A（本地离线瓦片）",
                "attribution_status": "license_documented_in_metadata",
            }
        low, high = self.zoom_range()
        scene = meta.get("scene") or {}
        resolution = meta.get("resolution") or {}
        return {
            "id": "offline_zhoushan_imagery",
            "label": "舟山离线卫星影像（Sentinel-2 L2A）",
            "status": "available" if self.tiles_dir.is_dir() else "blocked",
            "reason_code": None if self.tiles_dir.is_dir()
            else "offline_imagery_tiles_missing",
            "interface": "本地 Web Mercator XYZ PNG 瓦片（无上游）",
            "host": None,
            "tile_size": int((meta.get("tiles") or {}).get("tile_size") or TILE_SIZE),
            "tile_order": "z/x/y (XYZ, y=0 在北)",
            "zoom": [low, high],
            "local_url": URL_TEMPLATE,
            "coverage_crs84": self.coverage(),
            # 署名必须指向**真实**数据来源，绝不能标成 ArcGIS World Imagery。
            "attribution": self.attribution(),
            "attribution_status": "license_documented_in_metadata",
            "license": (meta.get("license") or {}).get("name"),
            "is_satellite_imagery": True,
            "requires_credential": False,
            "consumes_online_budget": False,
            "native_gsd_m": resolution.get("native_gsd_m"),
            "delivered_m_per_px": resolution.get("delivered_m_per_px"),
            "upsampled": bool(resolution.get("upsampled")),
            "scene_acquired_utc": scene.get("acquired_utc"),
            "tile_count": (meta.get("tiles") or {}).get("count"),
            "tile_bytes": (meta.get("tiles") or {}).get("bytes"),
            "content_sha256": (meta.get("tiles") or {}).get("content_sha256"),
        }

    # -------------------------------------------------------------- 瓦片

    def tile_path(self, z, x, y):
        """解析瓦片文件路径；坐标非法或越界时返回 ``None``。

        路径安全：``z/x/y`` 必须是整数，且拼出的路径必须仍在瓦片根目录之内。
        本地瓦片端点对浏览器开放，绝不能因为 ``../`` 之类的输入读到目录外文件。
        """

        try:
            z, x, y = int(z), int(x), int(y)
        except (TypeError, ValueError):
            return None
        low, high = self.zoom_range()
        if not low <= z <= high:
            return None
        limit = 1 << z
        if not (0 <= x < limit and 0 <= y < limit):
            return None
        base = self.tiles_dir.resolve()
        candidate = (base / str(z) / str(x) / f"{y}.png").resolve()
        if base != candidate and base not in candidate.parents:
            return None
        return candidate

    def tile(self, z, x, y):
        """读取一张瓦片。

        :returns: ``bytes``（PNG 内容）或 ``None``（该瓦片不存在）。

        返回 ``None`` 而**不是**抛异常：瓦片不存在是本方案的正常状态（数据范围
        边缘、图层矩形外），前端会请求到它们；把它当异常会让日志与面板被噪声淹没。
        """

        path = self.tile_path(z, x, y)
        if path is None:
            return None
        try:
            return path.read_bytes()
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError:
            return None


#: 进程级单例：瓦片目录扫描与元信息解析只做一次。
_SOURCE_LOCK = threading.Lock()
_SOURCES: dict = {}


def source_for(context=None, *, environ=None):
    """取（并按需装配）进程内的离线瓦片源。

    与既有在线服务一样挂在 ``context`` 上，保证同一进程共用一份元信息缓存；
    挂载失败时退回进程级字典，绝不因为"挂不上 context"就让离线影像整体不可用。
    """

    if context is not None:
        existing = getattr(context, "_visualization_3d_offline_imagery_source", None)
        if existing is not None:
            return existing
    env = os.environ if environ is None else environ
    key = str(env.get(TILE_ROOT_ENV) or "").strip() or str(DEFAULT_TILE_ROOT)
    with _SOURCE_LOCK:
        source = _SOURCES.get(key)
        if source is None:
            source = OfflineImageryTileSource(environ=env)
            _SOURCES[key] = source
    if context is not None:
        try:
            setattr(context, "_visualization_3d_offline_imagery_source", source)
        except Exception:  # pragma: no cover - 只读上下文可能拒绝挂载
            pass
    return source


def _dataset_name_is_safe(name: str) -> bool:
    return bool(_SAFE_NAME.match(str(name or "")))


__all__ = [
    "DEFAULT_TILE_ROOT",
    "MAX_ZOOM",
    "MIN_ZOOM",
    "OfflineImageryTileSource",
    "OfflineImageryUnavailable",
    "TILE_ROOT_ENV",
    "TILE_SIZE",
    "URL_TEMPLATE",
    "source_for",
]
