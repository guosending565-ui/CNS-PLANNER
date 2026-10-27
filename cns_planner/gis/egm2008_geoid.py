"""EGM2008 大地水准面（geoid）网格的只读适配。

工程唯一权威垂直基准是 EGM2008 正高。把正高换算成 WGS84 椭球高需要 geoid
网格给出的大地水准面差距 ``N``：

    h_ellipsoidal = H_orthometric + N

硬约束：

1. **NoData / 越界 / 网格缺失 / 网格自述不符一律显式失败**（fail-closed），
   绝不返回 0、绝不外插、绝不静默换网格；
2. **禁止回退 EGM96 / EGM84**：它们与 EGM2008 不是同一基准；
3. 网格来自外部共享只读数据目录，**不进 Git 仓库**；
4. **canonical 后端 = PROJ**（EPSG 注册方法 ``Geographic3D to GravityRelatedHeight
   (EGM2008)``，即 ``+proj=vgridshift``）。本模块**只**接受这一个后端；PROJ 不可用
   时整条换算显式失败，绝不静默回退到 GDAL 双线性（两者在本机存在约 0.07–0.10 m
   的系统差，混用会让同一高程出现两个答案）。
5. 自述校验同时支持 GDAL 与 tifffile 两种只读读取方式；两者都没有时无法证明这是
   EGM2008 网格，同样 fail-closed。

本模块不 import QGIS：只依赖 ``pyproj``（采样）与可选的 ``osgeo`` / ``tifffile``
（仅用于读取网格自述与几何范围）。
"""

from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
from threading import Lock

#: EGM2008 geoid 网格的默认共享只读位置（D 盘共享数据目录，不进 Git）。
DEFAULT_GEOID_DIR = Path(r"D:\aaa2026project\UOM\舟山\规划系统\geoid")
GEOID_FILENAME = "us_nga_egm08_25.tif"

#: 显式指定 geoid 网格的环境变量（优先级最高）。
ENV_VAR = "CNS_EGM2008_GEOID"

_REQUIRED_SELF_DESCRIPTION = "egm2008"

#: 禁止作为 EGM2008 替代品的网格名片段（出现即拒绝，避免静默降级）。
FORBIDDEN_SUBSTITUTES = ("egm96", "egm84", "egm1996", "egm1984")

CANONICAL_BACKEND = "proj_vgridshift_bilinear"

#: 同一进程内只解析/校验一次网格身份（按路径 + mtime + size 失效）。
_CACHE = {}
_CACHE_LOCK = Lock()


class GeoidUnavailable(RuntimeError):
    """geoid 网格不可用或本次查询不可判定（fail-closed）。"""

    def __init__(self, code: str, message: str, **detail):
        super().__init__(message)
        self.code = code
        self.detail = detail or {}

    def as_dict(self):
        return {"code": self.code, "message": str(self), "detail": self.detail}


def resolve_geoid_path(explicit=None):
    """解析 EGM2008 geoid 网格路径；不存在时返回 ``None``。绝不搜索替代网格。"""

    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    env = os.environ.get(ENV_VAR)
    if env:
        candidates.append(Path(env))
    candidates.append(DEFAULT_GEOID_DIR / GEOID_FILENAME)
    for candidate in candidates:
        try:
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
        except OSError:
            continue
    return None


def _sha256_of(path: Path) -> str:
    """文件 SHA-256；若存在 ``.sha256`` 旁车文件则先核对，不一致直接失败。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 22), b""):
            digest.update(block)
    actual = digest.hexdigest()
    sidecar = path.with_name(path.name + ".sha256")
    if sidecar.is_file():
        token = ""
        try:
            token = sidecar.read_text(encoding="utf-8").split()[0].strip().lower()
        except (OSError, IndexError, ValueError):
            token = ""
        if len(token) == 64 and token != actual:
            raise GeoidUnavailable(
                "egm2008_grid_sha256_mismatch",
                "EGM2008 geoid 网格与旁车 SHA-256 不一致，拒绝使用",
                expected=token, actual=actual,
            )
        if len(token) == 64:
            return token
    return actual


def proj_pipeline(path):
    """PROJ 竖直网格位移管线（EPSG 方法 ``Geographic3D to GravityRelatedHeight``）。"""

    return f"+proj=vgridshift +grids={Path(path).as_posix()} +multiplier=1"


def _tiff_metadata(path: Path):
    """用可用的只读 TIFF 读取器取网格自述与几何范围；都不可用时返回 ``None``。"""

    try:
        from osgeo import gdal  # type: ignore
    except ImportError:
        gdal = None
    if gdal is not None:
        dataset = gdal.Open(str(path), gdal.GA_ReadOnly)
        if dataset is None:
            return None
        transform = dataset.GetGeoTransform()
        metadata = dataset.GetMetadata() or {}
        width = int(dataset.RasterXSize)
        height = int(dataset.RasterYSize)
        dataset = None
        return {
            "reader": "gdal",
            "metadata": metadata,
            "transform": transform,
            "width": width,
            "height": height,
        }
    try:
        import tifffile  # type: ignore
    except ImportError:
        return None
    try:
        with tifffile.TiffFile(path) as tiff:
            page = tiff.pages[0]
            tags = {tag.name: tag.value for tag in page.tags.values()}
            description = str(tags.get("ImageDescription") or "")
            gdal_metadata = str(tags.get("GDAL_METADATA") or "")
            scale = tags.get("ModelPixelScaleTag")
            tiepoint = tags.get("ModelTiepointTag")
            if not (isinstance(scale, (list, tuple)) and len(scale) >= 2
                    and isinstance(tiepoint, (list, tuple)) and len(tiepoint) >= 6):
                return None
            metadata = {
                "TIFFTAG_IMAGEDESCRIPTION": description,
                "GDAL_METADATA": gdal_metadata,
            }
            return {
                "reader": "tifffile",
                "metadata": metadata,
                "transform": (
                    float(tiepoint[3]), float(scale[0]), 0.0,
                    float(tiepoint[4]), 0.0, -float(scale[1]),
                ),
                "width": int(page.imagewidth),
                "height": int(page.imagelength),
            }
    except Exception:  # noqa: BLE001 - 读取失败即视为无法证明网格身份
        return None


def _self_description(metadata):
    return " ".join(
        str(metadata.get(key) or "")
        for key in ("TIFFTAG_IMAGEDESCRIPTION", "GDAL_METADATA", "TYPE", "source")
    )


class Egm2008Geoid:
    """EGM2008 geoid 网格的只读句柄（canonical 后端 = PROJ ``vgridshift``）。"""

    def __init__(self, path, *, verify_sha=True):
        self.path = Path(path).resolve()
        if not self.path.is_file():
            raise GeoidUnavailable(
                "egm2008_grid_missing", f"EGM2008 geoid 网格不存在：{self.path}")
        lowered = self.path.name.lower()
        for banned in FORBIDDEN_SUBSTITUTES:
            if banned in lowered:
                raise GeoidUnavailable(
                    "egm2008_grid_is_forbidden_substitute",
                    f"拒绝把 {self.path.name} 当作 EGM2008 使用（禁止静默回退 EGM96/EGM84）",
                    filename=self.path.name,
                )
        info = _tiff_metadata(self.path)
        if info is None:
            raise GeoidUnavailable(
                "egm2008_grid_metadata_unreadable",
                "无法读取 geoid 网格自述（既无 GDAL 也无 tifffile），"
                "无法证明它是 EGM2008，拒绝用于工程级椭球高换算",
                path=str(self.path),
            )
        described = _self_description(info["metadata"])
        if _REQUIRED_SELF_DESCRIPTION not in described.lower():
            raise GeoidUnavailable(
                "egm2008_grid_self_description_mismatch",
                "geoid 网格未自述为 EGM2008，拒绝用于工程级椭球高换算",
                described=described[:400],
            )
        self.metadata_reader = info["reader"]
        self.metadata = info["metadata"]
        self.transform = info["transform"]
        self.width = int(info["width"])
        self.height = int(info["height"])
        self.self_description = described
        self.sha256 = _sha256_of(self.path) if verify_sha else None
        self.size_bytes = self.path.stat().st_size
        self.mtime_ns = self.path.stat().st_mtime_ns
        try:
            import pyproj
        except ImportError as exc:  # pragma: no cover - 环境级失败
            raise GeoidUnavailable(
                "egm2008_pyproj_not_installed",
                "本环境无 pyproj，无法执行 EGM2008 椭球高换算（不做 GDAL 回退）",
            ) from exc
        self._pyproj = pyproj
        try:
            self._transformer = pyproj.Transformer.from_pipeline(
                proj_pipeline(self.path))
        except Exception as exc:  # noqa: BLE001
            raise GeoidUnavailable(
                "egm2008_proj_pipeline_failed", str(exc)) from exc

    # ------------------------------------------------------------------ 元数据

    def describe(self):
        west, north = float(self.transform[0]), float(self.transform[3])
        xres, yres = abs(float(self.transform[1])), abs(float(self.transform[5]))
        east = west + xres * self.width
        south = north - yres * self.height
        return {
            "path": str(self.path),
            "filename": self.path.name,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "mtime_ns": self.mtime_ns,
            "width": self.width,
            "height": self.height,
            "pixel_size_deg": [xres, yres],
            "extent": [min(west, east), min(north, south), max(west, east), max(north, south)],
            "self_description": self.self_description,
            "metadata_reader": self.metadata_reader,
            "canonical_backend": CANONICAL_BACKEND,
            "proj_pipeline": proj_pipeline(self.path),
            "vertical_reference": "EGM2008_orthometric -> WGS84_ellipsoidal",
            "formula": "h_ellipsoidal = H_orthometric + N",
            "shared_readonly": True,
            "in_git_repository": False,
        }

    # ------------------------------------------------------------------- 采样

    def undulation_array(self, lons, lats):
        """批量采样 ``N``（米）。不可判定处为 ``None``（绝不补 0）。"""

        lons = [float(value) for value in lons]
        lats = [float(value) for value in lats]
        if len(lons) != len(lats):
            raise GeoidUnavailable("egm2008_batch_length_mismatch", "经纬度数组长度不一致")
        for lon, lat in zip(lons, lats):
            if not (math.isfinite(lon) and math.isfinite(lat)):
                raise GeoidUnavailable("egm2008_invalid_coordinate", "经纬度非有限值")
            if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
                raise GeoidUnavailable("egm2008_coordinate_out_of_range", "经纬度超出地理范围")
        zeros = [0.0] * len(lons)
        try:
            _, _, values = self._transformer.transform(
                lons, lats, zeros, errcheck=False)
        except Exception as exc:  # noqa: BLE001
            raise GeoidUnavailable("egm2008_proj_transform_failed", str(exc)) from exc
        output = []
        for value in values:
            value = float(value)
            if not math.isfinite(value) or abs(value) > 1.0e5:
                output.append(None)
            else:
                output.append(value)
        return output

    def undulation(self, lon, lat):
        """单点 ``N``；返回 ``{"status", "value", "reason"}``（fail-closed）。"""

        try:
            lon_f, lat_f = float(lon), float(lat)
        except (TypeError, ValueError):
            return {"status": "invalid_request", "value": None, "reason": "经纬度不是数值"}
        if not (math.isfinite(lon_f) and math.isfinite(lat_f)):
            return {"status": "invalid_request", "value": None, "reason": "经纬度非有限值"}
        if not (-180.0 <= lon_f <= 180.0 and -90.0 <= lat_f <= 90.0):
            return {"status": "outside_geographic_range", "value": None,
                    "reason": "经纬度超出地理范围"}
        value = self.undulation_array([lon_f], [lat_f])[0]
        if value is None:
            return {"status": "outside_geoid_grid", "value": None,
                    "reason": "查询点落在 EGM2008 geoid 网格可插值范围之外（PROJ 返回非有限值）",
                    "backend": CANONICAL_BACKEND}
        return {"status": "passed", "value": value, "reason": None,
                "backend": CANONICAL_BACKEND,
                "interpolation": "proj_vgridshift_bilinear"}

    def undulation_strict(self, lon, lat):
        result = self.undulation(lon, lat)
        if result["status"] != "passed":
            raise GeoidUnavailable(
                "egm2008_undulation_" + str(result["status"]),
                f"无法取得 EGM2008 大地水准面差距：{result.get('reason')}",
                lon=lon, lat=lat,
            )
        return float(result["value"])

    # --------------------------------------------------------------- 高度换算

    def orthometric_to_ellipsoidal(self, orthometric_m, lon, lat):
        """正高 → 椭球高：``h = H + N``（逐点 N，不是全场常数）。"""

        if orthometric_m is None:
            raise GeoidUnavailable("egm2008_height_missing",
                                   "正高缺失，不能换算椭球高", lon=lon, lat=lat)
        value = float(orthometric_m)
        if not math.isfinite(value):
            raise GeoidUnavailable("egm2008_height_not_finite", "正高不是有限值")
        return value + self.undulation_strict(lon, lat)

    def ellipsoidal_to_orthometric(self, ellipsoidal_m, lon, lat):
        """椭球高 → 正高：``H = h - N``。"""

        value = float(ellipsoidal_m)
        if not math.isfinite(value):
            raise GeoidUnavailable("egm2008_height_not_finite", "椭球高不是有限值")
        return value - self.undulation_strict(lon, lat)

    def window(self, bbox, width, height):
        """按 bbox 采样 ``N`` 窗口（行序北→南）。"""

        west, south, east, north = (float(value) for value in bbox)
        width, height = int(width), int(height)
        if not (1 <= width <= 2048 and 1 <= height <= 2048):
            raise GeoidUnavailable("egm2008_window_size_invalid",
                                   f"窗口尺寸非法：{width}x{height}")
        if not (west < east and south < north):
            raise GeoidUnavailable("egm2008_window_bbox_invalid",
                                   "bbox 非法（要求 west<east、south<north）")
        lons, lats = [], []
        for row in range(height):
            lat = north + (south - north) * (row / max(height - 1, 1))
            for col in range(width):
                lon = west + (east - west) * (col / max(width - 1, 1))
                lons.append(lon)
                lats.append(lat)
        flat = self.undulation_array(lons, lats)
        values = []
        nodata = 0
        for row in range(height):
            line = flat[row * width:(row + 1) * width]
            nodata += sum(1 for value in line if value is None)
            values.append(line)
        return {
            "status": "passed" if nodata == 0 else ("nodata" if nodata == width * height
                                                     else "partial"),
            "width": width, "height": height, "bbox": [west, south, east, north],
            "row_order": "north_to_south", "nodata_count": nodata,
            "backend": CANONICAL_BACKEND, "values": values,
        }


def load_geoid(explicit=None):
    """返回 ``(Egm2008Geoid | None, 原因字典 | None)``；进程内缓存已校验的网格。"""

    path = resolve_geoid_path(explicit)
    if path is None:
        return None, {
            "code": "egm2008_grid_missing",
            "message": "本机未找到 EGM2008 geoid 网格（us_nga_egm08_25.tif）",
            "searched": [str(DEFAULT_GEOID_DIR / GEOID_FILENAME)],
            "env_var": ENV_VAR,
        }
    try:
        stat = path.stat()
    except OSError as exc:
        return None, {"code": "egm2008_grid_missing", "message": str(exc)}
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            return cached, None
    try:
        geoid = Egm2008Geoid(path)
    except GeoidUnavailable as exc:
        return None, exc.as_dict()
    except Exception as exc:  # noqa: BLE001 - 环境级失败
        return None, {"code": "egm2008_grid_open_failed", "message": str(exc)}
    with _CACHE_LOCK:
        _CACHE.clear()
        _CACHE[key] = geoid
    return geoid, None


__all__ = [
    "CANONICAL_BACKEND", "DEFAULT_GEOID_DIR", "ENV_VAR", "Egm2008Geoid",
    "GeoidUnavailable", "GEOID_FILENAME", "load_geoid", "proj_pipeline",
    "resolve_geoid_path",
]
