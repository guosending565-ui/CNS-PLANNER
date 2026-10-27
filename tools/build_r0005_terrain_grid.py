"""构建 C3-C 三维视图使用的真实地形网格（FABDEM + EGM2008 → WGS84 椭球高）。

必须用 QGIS 自带解释器运行（需要 GDAL + pyproj）：

    & 'C:\\Program Files\\QGIS 3.44.14\\bin\\python-qgis-ltr.bat' `
        tools/build_r0005_terrain_grid.py --run-root <RESEARCH_RUNS>

语义边界
========

* 只读：FABDEM 与 EGM2008 网格都只以只读方式打开，**不改写任何源数据**；
* 地形只用于**显示**：本工具不计算航路净空、不评价可行性、不写 ProjectState；
* NoData 不当 0：网格中的 NoData 逐像元计数并写入 ``nodata_count``；三维端以
  ``display_only_nodata_below_zero`` 明确披露，且不把它读作"海平面"业务事实；
* 垂直基准：``h_ellipsoidal = H_orthometric + N``，逐点使用 EGM2008 geoid 的 N，
  绝不使用全场常数。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
import struct
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

DEFAULT_FABDEM = Path(
    r"D:\aaa2026project\UOM\舟山\规划系统\FABDEM\processed\Zhoushan_FABDEM_DTM_30m.tif")
DEFAULT_GEOID = Path(
    r"D:\aaa2026project\UOM\舟山\规划系统\geoid\us_nga_egm08_25.tif")
SCHEMA_VERSION = "c3c-terrain-grid-1"
GRID_RESOLUTION_M = 100.0
BUFFER_M = 3000.0


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 22), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_manifest(run_root: Path, run_id: str) -> dict:
    path = run_root / "runs" / run_id / "run_manifest.json"
    if not path.is_file():
        raise SystemExit(f"run manifest 不存在：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _metric_extent(route_path, buffer_m=BUFFER_M):
    from pyproj import Geod, Transformer

    transformer = Transformer.from_crs("EPSG:4326", "EPSG:32651", always_xy=True)
    inverse = Transformer.from_crs("EPSG:32651", "EPSG:4326", always_xy=True)
    xs, ys = [], []
    for lon, lat in route_path:
        x, y = transformer.transform(float(lon), float(lat))
        xs.append(x)
        ys.append(y)
    west, east = min(xs) - buffer_m, max(xs) + buffer_m
    south, north = min(ys) - buffer_m, max(ys) + buffer_m
    return {
        "metric_bounds": [west, south, east, north],
        "metric_width_m": east - west,
        "metric_height_m": north - south,
        "inverse": inverse,
    }


def _grid_geometry(metric, resolution_m=GRID_RESOLUTION_M):
    width = max(8, int(math.ceil(metric["metric_width_m"] / resolution_m)))
    height = max(8, int(math.ceil(metric["metric_height_m"] / resolution_m)))
    inverse = metric["inverse"]
    corners = [
        inverse.transform(metric["metric_bounds"][0], metric["metric_bounds"][3]),
        inverse.transform(metric["metric_bounds"][2], metric["metric_bounds"][1]),
    ]
    west, north = corners[0]
    east, south = corners[1]
    return {
        "extent": [west, south, east, north],
        "width": width,
        "height": height,
        "xres_deg": (east - west) / width,
        "yres_deg": (north - south) / height,
    }


def _sample_fabdem(fabdem_path: Path, geometry, *, resample="bilinear"):
    from osgeo import gdal

    gdal.UseExceptions()
    source = gdal.Open(str(fabdem_path), gdal.GA_ReadOnly)
    if source is None:
        raise SystemExit(f"无法打开 FABDEM：{fabdem_path}")
    west, south, east, north = geometry["extent"]
    warped = gdal.Warp(
        "", source, format="MEM", dstSRS="EPSG:4326",
        outputBounds=[west, south, east, north],
        width=geometry["width"], height=geometry["height"],
        resampleAlg=resample, srcNodata=-9999.0, dstNodata=-9999.0,
    )
    if warped is None:
        raise SystemExit("FABDEM 重采样失败")
    band = warped.GetRasterBand(1)
    import numpy as np

    array = band.ReadAsArray().astype("float64")
    nodata = band.GetNoDataValue()
    mask = ~np.isfinite(array)
    if nodata is not None:
        mask |= array == float(nodata)
    metadata = source.GetMetadata() or {}
    projection = source.GetProjection() or ""
    source = None
    warped = None
    return array, mask, metadata, projection


def _undulation_grid(geoid_path: Path, geometry):
    import pyproj
    import numpy as np

    pipeline = f"+proj=vgridshift +grids={Path(geoid_path).as_posix()} +multiplier=1"
    transformer = pyproj.Transformer.from_pipeline(pipeline)
    width, height = geometry["width"], geometry["height"]
    west, south, east, north = geometry["extent"]
    lons, lats = [], []
    for row in range(height):
        lat = north - (north - south) * ((row + 0.5) / height)
        for col in range(width):
            lon = west + (east - west) * ((col + 0.5) / width)
            lons.append(lon)
            lats.append(lat)
    _, _, values = transformer.transform(lons, lats, [0.0] * len(lons), errcheck=False)
    array = np.array([float(value) for value in values], dtype="float64").reshape(
        height, width)
    mask = ~np.isfinite(array)
    return array, mask


def _render_basemap(orthometric, ellipsoidal, nodata, resolution_m):
    """把 FABDEM 网格渲染成一张**地形晕渲底图**（不是卫星影像）。

    颜色只表达高程与坡向，不表达任何业务结论；海域按 FABDEM 正交高 ≤ 0.5 m
    识别，海岸线用一次 8 邻域膨胀勾出。
    """

    import io

    import numpy as np
    from PIL import Image

    height, width = orthometric.shape
    dzdy, dzdx = np.gradient(ellipsoidal, resolution_m, resolution_m)
    slope = np.arctan(np.hypot(dzdx, dzdy))
    aspect = np.arctan2(-dzdx, dzdy)
    azimuth = np.deg2rad(315.0)
    altitude = np.deg2rad(45.0)
    shade = (np.sin(altitude) * np.cos(slope)
             + np.cos(altitude) * np.sin(slope) * np.cos(azimuth - aspect))
    shade = np.clip(np.nan_to_num(shade, nan=0.5), 0.0, 1.0)

    stops = np.array([0.0, 5.0, 20.0, 80.0, 200.0, 400.0, 700.0])
    palette = np.array([
        [176, 190, 158],
        [164, 181, 137],
        [145, 166, 116],
        [170, 160, 116],
        [183, 149, 105],
        [176, 132, 99],
        [214, 205, 190],
    ], dtype="float64")
    color = np.zeros((height, width, 3), dtype="float64")
    for channel in range(3):
        color[..., channel] = np.interp(
            np.clip(orthometric, 0, 700), stops, palette[:, channel])
    factor = 0.52 + 0.62 * shade
    color *= factor[..., None]

    water = orthometric <= 0.5
    water_color = np.array([22, 62, 84], dtype="float64")
    water_shade = 0.82 + 0.28 * shade
    color[water] = water_color * water_shade[water][..., None]
    # 海岸线：陆地像素的 8 邻域里存在水体。
    neighbor_water = np.zeros_like(water)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy == 0 and dx == 0:
                continue
            shifted = np.roll(np.roll(water, dy, axis=0), dx, axis=1)
            neighbor_water |= shifted
    coastline = (~water) & neighbor_water
    color[coastline] = color[coastline] * 0.55 + np.array([38, 54, 58]) * 0.45
    color[nodata] = np.array([70, 82, 88], dtype="float64")

    image = Image.fromarray(np.clip(color, 0, 255).astype("uint8"), mode="RGB")
    scale = 8
    image = image.resize((width * scale, height * scale), Image.BICUBIC)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue(), image.size


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--run-id", default="c3b-20261027-d")
    parser.add_argument("--fabdem", default=str(DEFAULT_FABDEM))
    parser.add_argument("--geoid", default=str(DEFAULT_GEOID))
    parser.add_argument("--resolution-m", type=float, default=GRID_RESOLUTION_M)
    parser.add_argument("--buffer-m", type=float, default=BUFFER_M)
    args = parser.parse_args(argv)

    import numpy as np

    run_root = Path(args.run_root).resolve()
    manifest = _load_manifest(run_root, args.run_id)
    route = manifest["map_projection"]["route"]
    path = route.get("path") or []
    if len(path) < 2:
        raise SystemExit("run manifest 没有可用航路几何")
    metric = _metric_extent(path, args.buffer_m)
    geometry = _grid_geometry(metric, args.resolution_m)

    fabdem = Path(args.fabdem)
    geoid = Path(args.geoid)
    orthometric, nodata_mask, fabdem_metadata, projection = _sample_fabdem(
        fabdem, geometry)
    undulation, geoid_mask = _undulation_grid(geoid, geometry)
    nodata = nodata_mask | geoid_mask
    ellipsoidal = np.where(nodata, 0.0, orthometric + undulation)
    # Display-only fill for NoData: 0 ellipsoidal metres, disclosed below.
    ellipsoidal = ellipsoidal.astype("<f4")
    basemap_bytes, basemap_size = _render_basemap(
        np.where(nodata, 0.0, orthometric), ellipsoidal.astype("float64"),
        nodata, args.resolution_m)

    target = run_root / "c3c" / "terrain"
    target.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "semantics": (
            "FABDEM bare-earth DTM sampled for display only; "
            "h_ellipsoidal = H_orthometric + N(EGM2008); no business result derived"),
        "source_run_id": manifest.get("run_id"),
        "route_id": manifest.get("route_id"),
        "route_geometry_sha256": hashlib.sha256(
            json.dumps(path, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")).encode("utf-8")).hexdigest(),
        "created_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "extent": [round(float(value), 9) for value in geometry["extent"]],
        "width": geometry["width"],
        "height": geometry["height"],
        "xres_deg": geometry["xres_deg"],
        "yres_deg": geometry["yres_deg"],
        "grid_resolution_m": args.resolution_m,
        "buffer_m": args.buffer_m,
        "row_order": "north_to_south",
        "crs": "EPSG:4326",
        "values_dtype": "float32_le",
        "heights_ellipsoidal_m_b64": base64.b64encode(ellipsoidal.tobytes()).decode("ascii"),
        "basemap_png_b64": base64.b64encode(basemap_bytes).decode("ascii"),
        "basemap_sha256": hashlib.sha256(basemap_bytes).hexdigest(),
        "basemap_width": basemap_size[0],
        "basemap_height": basemap_size[1],
        "basemap_semantics": (
            "FABDEM 30m hypsometric hillshade for display only; not satellite imagery; "
            "no business conclusion is encoded"),
        "water_mask_b64": base64.b64encode(
            np.where(nodata, 0, (orthometric <= 0.5)).astype("uint8").tobytes()).decode("ascii"),
        "height_min_m": round(float(np.min(ellipsoidal[~nodata])), 4)
        if (~nodata).any() else None,
        "height_max_m": round(float(np.max(ellipsoidal[~nodata])), 4)
        if (~nodata).any() else None,
        "undulation_min_m": round(float(np.min(undulation[~geoid_mask])), 4)
        if (~geoid_mask).any() else None,
        "undulation_max_m": round(float(np.max(undulation[~geoid_mask])), 4)
        if (~geoid_mask).any() else None,
        "nodata_count": int(nodata.sum()),
        "nodata_policy": "display_zero_ellipsoidal_disclosed_not_business_sea_level",
        "fabdem": {
            "path": str(fabdem.resolve()),
            "filename": fabdem.name,
            "sha256": sha256(fabdem),
            "size_bytes": fabdem.stat().st_size,
            "metadata": fabdem_metadata,
            "projection_wkt_prefix": projection[:240],
            "vertical_reference": fabdem_metadata.get("vertical_reference"),
            "role": fabdem_metadata.get("role"),
        },
        "geoid": {
            "path": str(geoid.resolve()),
            "filename": geoid.name,
            "sha256": sha256(geoid),
            "size_bytes": geoid.stat().st_size,
            "formula": "h_ellipsoidal = H_orthometric + N",
        },
        "read_only": True,
        "writes_project_state": False,
        "recomputes_business_results": False,
    }
    output = target / "r0005_terrain_100m.json"
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
                      encoding="utf-8", newline="\n")
    summary = {key: payload[key] for key in (
        "schema_version", "source_run_id", "route_id", "route_geometry_sha256",
        "extent", "width", "height", "grid_resolution_m",
        "basemap_sha256", "basemap_width", "basemap_height",
        "height_min_m", "height_max_m", "undulation_min_m", "undulation_max_m",
        "nodata_count", "fabdem", "geoid")}
    summary["output"] = str(output)
    summary["output_sha256"] = sha256(output)
    summary["output_bytes"] = output.stat().st_size
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("TERRAIN_GRID_OK", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
