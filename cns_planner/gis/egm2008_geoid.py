"""EGM2008 大地水准面（geoid）网格的**只读**适配。

工程唯一权威垂直基准是 EGM2008 正高（``spatial_3d.canonical_vertical_reference``）。
把正高换算成 WGS84 椭球高需要 geoid 网格给出的大地水准面差距 ``N``：

    h_ellipsoidal = H_orthometric + N

硬约束：

1. **NoData / 越界 / 网格缺失 / 网格自述不符一律显式失败**（fail-closed），
   绝不返回 0、绝不外插、绝不静默换网格；
2. **禁止回退 EGM96 / EGM84**：它们与 EGM2008 不是同一基准；
3. 网格来自外部共享只读数据目录，**不进 Git 仓库**；
4. **canonical 后端 = PROJ**（EPSG 注册方法 ``Geographic3D to GravityRelatedHeight
   (EGM2008)``，即 ``+proj=vgridshift``）。GDAL 2×2 双线性只作为**独立对照**：
   两者在本机实测存在约 0.07–0.10 m 的系统差（栅格原点解释不同），因此全链路
   一律使用同一个 canonical 后端，绝不混用。

本模块不 import QGIS：只依赖 GDAL(Python) 与 pyproj。
"""

from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path

#: EGM2008 geoid 网格的默认共享只读位置（D 盘共享数据目录，不进 Git）。
DEFAULT_GEOID_DIR = Path(r"D:\aaa2026project\UOM\舟山\规划系统\geoid")
GEOID_FILENAME = "us_nga_egm08_25.tif"

#: 显式指定 geoid 网格的环境变量（优先级最高）。
ENV_VAR = "CNS_EGM2008_GEOID"

_REQUIRED_SELF_DESCRIPTION = "egm2008"

#: 禁止作为 EGM2008 替代品的网格名片段（出现即拒绝，避免静默降级）。
FORBIDDEN_SUBSTITUTES = ("egm96", "egm84", "egm1996", "egm1984")

#: 单次窗口采样的硬上限（宽×高）。
MAX_WINDOW_CELLS = 1024 * 1024

#: EPSG:3855 是**纯垂直** CRS，必须与水平 CRS 组成复合 CRS 才能参与转换。
COMPOUND_EGM2008 = "EPSG:4326+3855"
ELLIPSOIDAL_3D = "EPSG:4979"

CANONICAL_BACKEND = "proj_vgridshift_bilinear"


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
    sidecar = path.with_name(path.name + ".sha256")
    if sidecar.is_file():
        try:
            token = sidecar.read_text(encoding="utf-8").split()[0].strip()
            if len(token) == 64:
                return token
        except (OSError, IndexError, ValueError):
            pass
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 22), b""):
            digest.update(block)
    return digest.hexdigest()


def proj_pipeline(path):
    """PROJ 竖直网格位移管线（EPSG 方法 ``Geographic3D to GravityRelatedHeight``）。"""

    return f"+proj=vgridshift +grids={Path(path).as_posix()} +multiplier=1"


class Egm2008Geoid:
    """EGM2008 geoid 网格的只读句柄。

    canonical 后端 = PROJ ``vgridshift``（与 ``TransformerGroup`` 选中的注册算子
    逐值一致，见交付证据）；GDAL 2×2 双线性仅作对照。
    """

    def __init__(self, path, *, enable_proj=True):
        from osgeo import gdal

        self.gdal = gdal
        self.path = Path(path).resolve()
        if not self.path.is_file():
            raise GeoidUnavailable("egm2008_grid_missing", f"EGM2008 geoid 网格不存在：{self.path}")
        lowered = self.path.name.lower()
        for banned in FORBIDDEN_SUBSTITUTES:
            if banned in lowered:
                raise GeoidUnavailable(
                    "egm2008_grid_is_forbidden_substitute",
                    f"拒绝把 {self.path.name} 当作 EGM2008 使用（禁止静默回退 EGM96/EGM84）",
                    filename=self.path.name,
                )
        self.dataset = gdal.Open(str(self.path), gdal.GA_ReadOnly)
        if self.dataset is None or self.dataset.RasterCount < 1:
            raise GeoidUnavailable("egm2008_grid_unreadable",
                                   f"无法以只读方式打开 EGM2008 geoid：{self.path}")
        self.band = self.dataset.GetRasterBand(1)
        self.transform = self.dataset.GetGeoTransform()
        inverse = gdal.InvGeoTransform(self.transform)
        if isinstance(inverse, tuple) and len(inverse) == 2 and isinstance(inverse[0], (bool, int)):
            ok, inverse = inverse
            if not ok:
                raise GeoidUnavailable("egm2008_grid_affine_not_invertible",
                                       "EGM2008 geoid 仿射变换不可逆")
        self.inverse = inverse
        self.width = int(self.dataset.RasterXSize)
        self.height = int(self.dataset.RasterYSize)
        self.nodata = self.band.GetNoDataValue()
        self.metadata = self.dataset.GetMetadata() or {}
        self.unit = self.band.GetUnitType() or self.metadata.get("vertical_unit") or "metre"
        self._assert_self_description()

        self.proj_transformer = None
        self.proj_error = None
        if enable_proj:
            self.proj_transformer, self.proj_error = self._build_proj_transformer()

    # ---------------------------------------------------------------- 自述校验

    def _assert_self_description(self):
        described = " ".join(
            str(value) for value in (
                self.metadata.get("TIFFTAG_IMAGEDESCRIPTION"),
                self.metadata.get("TYPE"),
                self.metadata.get("target_crs_epsg_code"),
                self.metadata.get("source"),
            ) if value
        )
        if _REQUIRED_SELF_DESCRIPTION not in described.lower():
            raise GeoidUnavailable(
                "egm2008_grid_self_description_mismatch",
                "geoid 网格未自述为 EGM2008，拒绝用于工程级椭球高换算",
                described=described[:400],
            )
        self.self_description = described

    # -------------------------------------------------------------------- PROJ

    def _build_proj_transformer(self):
        try:
            import pyproj
        except ImportError:
            return None, {"code": "pyproj_not_installed", "message": "本环境无 pyproj"}
        try:
            pyproj.datadir.append_data_dir(str(self.path.parent))
        except Exception:  # pragma: no cover - 尽最大努力，失败不致命
            pass
        try:
            return pyproj.Transformer.from_pipeline(proj_pipeline(self.path)), None
        except Exception as exc:  # pragma: no cover
            return None, {"code": "proj_pipeline_failed", "message": str(exc)}

    def _proj_undulation_array(self, lons, lats):
        """PROJ 批量采样（返回与输入同形的列表；不可判定处为 ``None``）。"""

        transformer = self.proj_transformer
        zeros = [0.0] * len(lons)
        try:
            _, _, values = transformer.transform(lons, lats, zeros, errcheck=False)
        except Exception as exc:
            raise GeoidUnavailable("egm2008_proj_transform_failed", str(exc))
        out = []
        for value in values:
            value = float(value)
            if not math.isfinite(value) or abs(value) > 1.0e5:
                out.append(None)
            else:
                out.append(value)
        return out

    # ------------------------------------------------------------------ 元数据

    def describe(self):
        west, north = self.transform[0], self.transform[3]
        east = west + self.transform[1] * self.width
        south = north + self.transform[5] * self.height
        return {
            "path": str(self.path),
            "filename": self.path.name,
            "sha256": _sha256_of(self.path),
            "size_bytes": self.path.stat().st_size,
            "mtime_ns": self.path.stat().st_mtime_ns,
            "width": self.width,
            "height": self.height,
            "pixel_size_deg": [abs(self.transform[1]), abs(self.transform[5])],
            "extent": [min(west, east), min(north, south), max(west, east), max(north, south)],
            "nodata": self.nodata,
            "unit": self.unit,
            "self_description": self.self_description,
            "vertical_offset_type": self.metadata.get("TYPE"),
            "target_crs": self.metadata.get("target_crs_epsg_code"),
            "are_or_point": self.metadata.get("ARE_OR_POINT") or self.metadata.get("AREA_OR_POINT"),
            "canonical_backend": CANONICAL_BACKEND if self.proj_transformer else "gdal_bilinear_2x2",
            "proj_pipeline": proj_pipeline(self.path) if self.proj_transformer else None,
            "proj_error": self.proj_error,
            "alternate_backends": ["gdal_bilinear_2x2"],
            "shared_readonly": True,
            "in_git_repository": False,
        }

    # ------------------------------------------------------------------- 采样

    def undulation(self, lon, lat):
        """canonical 采样 ``N``（米）。返回 ``{"status", "value", "reason"}``。"""

        try:
            lon_f, lat_f = float(lon), float(lat)
        except (TypeError, ValueError):
            return {"status": "invalid_request", "value": None, "reason": "经纬度不是数值"}
        if not (math.isfinite(lon_f) and math.isfinite(lat_f)):
            return {"status": "invalid_request", "value": None, "reason": "经纬度非有限值"}
        if not (-180.0 <= lon_f <= 180.0 and -90.0 <= lat_f <= 90.0):
            return {"status": "outside_geographic_range", "value": None,
                    "reason": "经纬度超出地理范围"}
        if self.proj_transformer is None:
            result = self.undulation_gdal(lon_f, lat_f)
            result["backend"] = "gdal_bilinear_2x2"
            result["backend_note"] = "PROJ 不可用，已回退到 GDAL 双线性（与 PROJ 存在约 0.1 m 系统差）"
            return result
        values = self._proj_undulation_array([lon_f], [lat_f])
        value = values[0]
        if value is None:
            return {"status": "outside_geoid_grid", "value": None,
                    "reason": "查询点落在 EGM2008 geoid 网格可插值范围之外（PROJ 返回非有限值）",
                    "backend": CANONICAL_BACKEND}
        return {"status": "passed", "value": value, "reason": None,
                "backend": CANONICAL_BACKEND, "interpolation": "proj_vgridshift_bilinear"}

    def undulation_gdal(self, lon, lat):
        """GDAL 2×2 双线性采样（**仅作独立对照**，不是 canonical）。"""

        px, py = self.gdal.ApplyGeoTransform(self.inverse, lon, lat)
        col, row = int(math.floor(px)), int(math.floor(py))
        if not (0 <= col < self.width - 1 and 0 <= row < self.height - 1):
            return {"status": "outside_geoid_grid", "value": None,
                    "reason": "查询点落在 EGM2008 geoid 网格覆盖范围之外", "pixel": [col, row]}
        arr = self.band.ReadAsArray(col, row, 2, 2)
        if arr is None:
            return {"status": "grid_read_failed", "value": None, "reason": "geoid 栅格读取失败"}
        values = arr.tolist()
        flat = [float(values[r][c]) for r in range(2) for c in range(2)]
        for value in flat:
            if not math.isfinite(value):
                return {"status": "nodata", "value": None, "reason": "geoid 网格 NoData"}
            if self.nodata is not None and value == float(self.nodata):
                return {"status": "nodata", "value": None, "reason": "geoid 网格 NoData"}
        fx, fy = px - col, py - row
        undulation = (
            flat[0] * (1 - fx) * (1 - fy) + flat[1] * fx * (1 - fy)
            + flat[2] * (1 - fx) * fy + flat[3] * fx * fy
        )
        return {"status": "passed", "value": float(undulation), "reason": None,
                "interpolation": "bilinear_2x2", "backend": "gdal_bilinear_2x2"}

    def undulation_strict(self, lon, lat):
        result = self.undulation(lon, lat)
        if result["status"] != "passed":
            raise GeoidUnavailable(
                f"egm2008_undulation_{result['status']}",
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

    # ------------------------------------------------------------- 窗口采样

    def window(self, bbox, width, height):
        """按 bbox 采样 ``N`` 窗口（行序北→南），canonical = PROJ 批量。"""

        west, south, east, north = (float(v) for v in bbox)
        width, height = int(width), int(height)
        if not (1 <= width <= 4096 and 1 <= height <= 4096):
            raise GeoidUnavailable("egm2008_window_size_invalid",
                                   f"窗口尺寸非法：{width}x{height}")
        if width * height > MAX_WINDOW_CELLS:
            raise GeoidUnavailable(
                "egm2008_window_too_large",
                f"窗口 {width}x{height} 超过单次上限 {MAX_WINDOW_CELLS} 像元",
            )
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

        backend = CANONICAL_BACKEND if self.proj_transformer is not None else "gdal_bilinear_2x2"
        if self.proj_transformer is not None:
            flat = self._proj_undulation_array(lons, lats)
        else:
            flat = []
            for lon, lat in zip(lons, lats):
                result = self.undulation_gdal(lon, lat)
                flat.append(result["value"] if result["status"] == "passed" else None)

        values = []
        nodata_count = 0
        for row in range(height):
            line = flat[row * width:(row + 1) * width]
            nodata_count += sum(1 for value in line if value is None)
            values.append(line)
        status = "passed" if nodata_count == 0 else (
            "nodata" if nodata_count == width * height else "partial")
        return {
            "status": status,
            "width": width,
            "height": height,
            "bbox": [west, south, east, north],
            "row_order": "north_to_south",
            "nodata_count": nodata_count,
            "backend": backend,
            "values": values,
        }


def proj_transformer_available():
    try:
        import pyproj  # noqa: F401
    except ImportError:
        return False, "pyproj_not_installed"
    return True, None


def load_geoid(explicit=None):
    """返回 ``(Egm2008Geoid | None, 原因字典 | None)``。"""

    path = resolve_geoid_path(explicit)
    if path is None:
        return None, {
            "code": "egm2008_grid_missing",
            "message": "本机未找到 EGM2008 geoid 网格（us_nga_egm08_25.tif）",
            "searched": [str(DEFAULT_GEOID_DIR / GEOID_FILENAME)],
            "env_var": ENV_VAR,
        }
    try:
        return Egm2008Geoid(path), None
    except GeoidUnavailable as exc:
        return None, exc.as_dict()
    except Exception as exc:  # pragma: no cover - 环境级失败
        return None, {"code": "egm2008_grid_open_failed", "message": str(exc)}
