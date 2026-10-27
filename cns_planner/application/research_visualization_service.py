"""C3-C 三维只读投影：把**已冻结**的 run manifest 投影成 Cesium 可消费的载荷。

边界（与 ``research_map_service`` 同一口径）：

* 只读：不调用求解器、不写 ProjectState、不写 artifact、不递增 revision；
* 不重算：航路、站址、设备、N 动作、C/RID/Radar/S 结论全部逐字段来自 manifest；
* 垂直基准：EGM2008 正高 → WGS84 椭球高 ``h = H + N``，逐点使用真实 geoid 网格；
  垂直基准无法证明时整条投影 fail-closed（不画"假高度"的航路）；
* 地形：FABDEM 只作显示用途，NoData 逐点计数并披露，不当作业务海平面；
* Radar 不生成任何覆盖几何：``features=[]`` + ``display_rule`` 明确 unknown。
"""

from __future__ import annotations

import base64
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import struct

from ..gis.egm2008_geoid import GeoidUnavailable, load_geoid

SCHEMA_VERSION = "c3c-visualization-3d-1"
TERRAIN_RELATIVE = Path("c3c") / "terrain" / "r0005_terrain_100m.json"
CANONICAL_ORTHOMETRIC = "EGM2008_orthometric"
CESIUM_ELLIPSOIDAL = "WGS84_ellipsoidal"


class Visualization3DUnavailable(RuntimeError):
    """三维投影不可用（fail-closed）；``code`` 供 HTTP 层转成稳定错误码。"""

    def __init__(self, code: str, message: str, **detail):
        super().__init__(message)
        self.code = code
        self.detail = detail or {}

    def as_dict(self):
        return {"code": self.code, "message": str(self), "detail": self.detail}


def canonical_sha256(value) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 22), b""):
            digest.update(block)
    return digest.hexdigest()


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


# --------------------------------------------------------------------------- 垂直基准


def _resolve_route_vertical_reference(manifest):
    """从 manifest 钉住的**源研究状态**解析 R0005 的垂直基准。

    为什么读源状态而不是 run 的 ``runtime/project_state.json``：后者会被后续运行
    （P18 评审投影）改写，不再与某一次已发布 run 逐字节对应；源状态则由 manifest 的
    ``provenance.source_state_sha256`` 钉死，任一 run 都能独立复核。
    """

    provenance = manifest.get("provenance") or {}
    source_root = provenance.get("source_root")
    expected_sha = provenance.get("source_state_sha256")
    route_id = manifest.get("route_id")
    if not source_root or not expected_sha:
        raise Visualization3DUnavailable(
            "route_vertical_reference_unresolved",
            "manifest 未钉住源研究状态，无法证明 R0005 的垂直基准")
    state_path = Path(source_root) / "runtime" / "project_state.json"
    if not state_path.is_file():
        raise Visualization3DUnavailable(
            "route_vertical_reference_unresolved",
            f"源研究状态不存在：{state_path}")
    actual_sha = _file_sha256(state_path)
    if actual_sha != expected_sha:
        raise Visualization3DUnavailable(
            "route_vertical_reference_source_sha_mismatch",
            "源研究状态 SHA 与 manifest 不一致，拒绝换算椭球高",
            expected=expected_sha, actual=actual_sha)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    spatial = state.get("spatial_3d") or {}
    layers = spatial.get("altitude_layers") or []
    operating = spatial.get("route_operating_layers") or []
    row = next((item for item in operating
                if item.get("route_id") == route_id and item.get("active") is not False), None)
    if row is None:
        raise Visualization3DUnavailable(
            "route_vertical_reference_unresolved",
            f"源研究状态没有 {route_id} 的生效高度层绑定")
    reference = str(row.get("vertical_reference") or "").strip()
    if reference.lower() != CANONICAL_ORTHOMETRIC.lower():
        raise Visualization3DUnavailable(
            "route_vertical_reference_not_egm2008",
            f"{route_id} 的垂直基准为 {reference or 'unknown'}，不是 EGM2008 正高，"
            "不能按 h = H + N 换算", reference=reference)
    layer_id = row.get("altitude_layer_id")
    layer = next((item for item in layers if item.get("altitude_layer_id") == layer_id), None)
    if layer is None:
        raise Visualization3DUnavailable(
            "route_altitude_layer_missing",
            f"源研究状态缺少高度层 {layer_id}")
    route = ((state.get("operational_routes") or [{}])[0])
    manifest_route = (manifest.get("map_projection") or {}).get("route") or {}
    nominal = _number(manifest_route.get("nominal_altitude_m"))
    layer_nominal = _number(layer.get("nominal_altitude_m"))
    if nominal is None or layer_nominal is None or abs(nominal - layer_nominal) > 1e-9:
        raise Visualization3DUnavailable(
            "route_altitude_mismatch",
            "run manifest 的航路高度与源研究状态高度层不一致，拒绝换算",
            manifest_nominal=nominal, layer_nominal=layer_nominal)
    return {
        "status": "resolved",
        "canonical_reference": CANONICAL_ORTHOMETRIC,
        "route_id": route_id,
        "altitude_layer_id": layer_id,
        "altitude_layer_name": layer.get("name"),
        "nominal_altitude_orthometric_m": nominal,
        "lower_altitude_m": _number(layer.get("lower_altitude_m")),
        "upper_altitude_m": _number(layer.get("upper_altitude_m")),
        "layer_source": layer.get("source"),
        "operating_layer_source": row.get("source"),
        "operating_layer_research_only": row.get("research_only") is True,
        "route_algorithm_compatibility": (route.get("algorithm_compatibility") or {}),
        "route_vertical_reference": route.get("vertical_reference"),
        "source_state_path": str(state_path),
        "source_state_sha256": expected_sha,
        "source_state_sha256_verified": True,
    }


# --------------------------------------------------------------------------- 地形


def load_terrain_asset(root, route_path):
    """读取 C3-C 冻结地形网格；缺失/身份不符时返回 unavailable（不阻塞三维）。"""

    root = Path(root)
    path = root / TERRAIN_RELATIVE
    if not path.is_file():
        return {"status": "unavailable", "reason": "terrain_grid_missing",
                "expected_path": str(path)}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"status": "unavailable", "reason": "terrain_grid_unreadable",
                "detail": str(exc)}
    width, height = int(payload.get("width") or 0), int(payload.get("height") or 0)
    encoded = payload.get("heights_ellipsoidal_m_b64") or ""
    try:
        buffer = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        return {"status": "unavailable", "reason": "terrain_grid_base64_invalid"}
    if width < 2 or height < 2 or len(buffer) != width * height * 4:
        return {"status": "unavailable", "reason": "terrain_grid_shape_invalid",
                "width": width, "height": height, "bytes": len(buffer)}
    geometry_sha = canonical_sha256(route_path)
    recorded = payload.get("route_geometry_sha256")
    if recorded and recorded != geometry_sha:
        return {"status": "unavailable", "reason": "terrain_route_geometry_mismatch",
                "recorded": recorded, "actual": geometry_sha}
    values = struct.unpack("<%df" % (width * height), buffer)
    return {
        "status": "available",
        "schema_version": payload.get("schema_version"),
        "source_run_id": payload.get("source_run_id"),
        "route_id": payload.get("route_id"),
        "route_geometry_sha256": recorded,
        "route_geometry_matched": recorded == geometry_sha,
        "extent": payload.get("extent"),
        "width": width,
        "height": height,
        "xres_deg": payload.get("xres_deg"),
        "yres_deg": payload.get("yres_deg"),
        "grid_resolution_m": payload.get("grid_resolution_m"),
        "row_order": payload.get("row_order") or "north_to_south",
        "crs": payload.get("crs") or "EPSG:4326",
        "values_dtype": payload.get("values_dtype"),
        "height_min_m": payload.get("height_min_m"),
        "height_max_m": payload.get("height_max_m"),
        "undulation_min_m": payload.get("undulation_min_m"),
        "undulation_max_m": payload.get("undulation_max_m"),
        "nodata_count": payload.get("nodata_count"),
        "nodata_policy": payload.get("nodata_policy"),
        "heights_ellipsoidal_m_b64": encoded,
        "basemap_png_b64": payload.get("basemap_png_b64"),
        "basemap_sha256": payload.get("basemap_sha256"),
        "basemap_width": payload.get("basemap_width"),
        "basemap_height": payload.get("basemap_height"),
        "basemap_semantics": payload.get("basemap_semantics"),
        "water_mask_b64": payload.get("water_mask_b64"),
        "fabdem": payload.get("fabdem") or {},
        "geoid": payload.get("geoid") or {},
        "asset_path": str(path),
        "asset_sha256": _file_sha256(path),
        "semantics": payload.get("semantics"),
        "_values": values,
    }


def sample_terrain(terrain, longitude, latitude):
    """双线性采样地形网格的**椭球高**；超出范围返回 ``None``（绝不外插）。"""

    if not terrain or terrain.get("status") != "available":
        return None
    values = terrain.get("_values")
    west, south, east, north = (float(value) for value in terrain["extent"])
    width, height = int(terrain["width"]), int(terrain["height"])
    if not (west <= longitude <= east and south <= latitude <= north):
        return None
    x = (longitude - west) / (east - west) * (width - 1)
    y = (north - latitude) / (north - south) * (height - 1)
    x0, y0 = int(math.floor(x)), int(math.floor(y))
    x1, y1 = min(x0 + 1, width - 1), min(y0 + 1, height - 1)
    fx, fy = x - x0, y - y0
    position = lambda column, row: values[row * width + column]
    value = (
        position(x0, y0) * (1 - fx) * (1 - fy)
        + position(x1, y0) * fx * (1 - fy)
        + position(x0, y1) * (1 - fx) * fy
        + position(x1, y1) * fx * fy
    )
    return float(value)


def terrain_public(terrain):
    """去掉内部采样数组后的可序列化地形块。"""

    if not terrain or terrain.get("status") != "available":
        return {key: value for key, value in (terrain or {}).items()
                if key != "_values"}
    return {key: value for key, value in terrain.items() if key != "_values"}


# --------------------------------------------------------------------------- 主体


def _ground_block(terrain, geoid, longitude, latitude):
    ellipsoidal = sample_terrain(terrain, longitude, latitude)
    undulation = geoid.undulation(longitude, latitude)
    if undulation.get("status") != "passed":
        raise Visualization3DUnavailable(
            "site_undulation_unresolved",
            f"站址 ({longitude}, {latitude}) 无法取得 EGM2008 N",
            detail=undulation)
    undulation_value = float(undulation["value"])
    return {
        "terrain_ellipsoidal_m": (None if ellipsoidal is None
                                  else round(ellipsoidal, 4)),
        "undulation_m": round(undulation_value, 4),
        "terrain_orthometric_m": (None if ellipsoidal is None
                                  else round(ellipsoidal - undulation_value, 4)),
        "terrain_source": ("c3c_fabdem_grid_display_only" if ellipsoidal is not None
                           else "outside_terrain_grid"),
        "vertical_formula": "h = H + N",
    }


def build_visualization_3d(manifest, *, root, manifest_file_sha256=None,
                           geoid_loader=load_geoid):
    """从已发布 manifest 构建 Cesium 载荷（确定性；不写盘）。"""

    map_projection = manifest.get("map_projection") or {}
    if not map_projection:
        raise Visualization3DUnavailable("run_map_projection_missing", "run 没有地图投影")
    if map_projection.get("run_id") != manifest.get("run_id"):
        raise Visualization3DUnavailable("map_run_identity_mismatch", "地图投影与 run 身份不一致")
    identity = {
        "run_id": manifest.get("run_id"),
        "task": manifest.get("task"),
        "route_id": manifest.get("route_id"),
        "authority": manifest.get("authority"),
        "input_version": (manifest.get("input_version") or {}).get("id"),
        "input_fingerprint": (manifest.get("input_version") or {}).get("input_fingerprint"),
        "parameter_fingerprint": (manifest.get("input_version") or {}).get(
            "parameter_fingerprint"),
        "contract_version": manifest.get("contract_version"),
        "result_list_sha256": manifest.get("authoritative_result_list_sha256"),
        "manifest_canonical_sha256": canonical_sha256(manifest),
        "manifest_file_sha256": manifest_file_sha256,
        "stage_result_shas": {
            stage: (entry or {}).get("result_sha256")
            for stage, entry in (manifest.get("stages") or {}).items()
        },
        "execution_mode": (manifest.get("execution") or {}).get("mode"),
        "solver_executed": (manifest.get("provenance") or {}).get("solver_executed"),
        "created_at": manifest.get("created_at"),
    }
    route = deepcopy(map_projection.get("route") or {})
    path = route.get("path") or []
    if len(path) < 2:
        raise Visualization3DUnavailable("route_geometry_missing", "run 没有可用航路几何")

    vertical = _resolve_route_vertical_reference(manifest)
    geoid, geoid_error = geoid_loader()
    if geoid is None or geoid_error:
        raise Visualization3DUnavailable(
            "egm2008_grid_unavailable", "EGM2008 geoid 网格不可用，无法换算椭球高",
            detail=geoid_error)
    terrain = load_terrain_asset(root, path)
    nominal_orthometric = float(vertical["nominal_altitude_orthometric_m"])
    points = []
    undulations = []
    for index, point in enumerate(path):
        longitude, latitude = float(point[0]), float(point[1])
        undulation = geoid.undulation_strict(longitude, latitude)
        undulations.append(undulation)
        points.append({
            "index": index,
            "longitude": round(longitude, 9),
            "latitude": round(latitude, 9),
            "altitude_orthometric_m": round(nominal_orthometric, 4),
            "undulation_m": round(undulation, 4),
            "altitude_ellipsoidal_m": round(nominal_orthometric + undulation, 4),
        })
    vertical_public = {
        "status": "resolved",
        "canonical_reference": vertical["canonical_reference"],
        "cesium_reference": CESIUM_ELLIPSOIDAL,
        "formula": "h_ellipsoidal = H_orthometric + N_EGM2008",
        "altitude_layer_id": vertical["altitude_layer_id"],
        "altitude_layer_name": vertical["altitude_layer_name"],
        "nominal_altitude_orthometric_m": vertical["nominal_altitude_orthometric_m"],
        "lower_altitude_m": vertical["lower_altitude_m"],
        "upper_altitude_m": vertical["upper_altitude_m"],
        "layer_source": vertical["layer_source"],
        "operating_layer_source": vertical["operating_layer_source"],
        "operating_layer_research_only": vertical["operating_layer_research_only"],
        "route_algorithm_compatibility": vertical["route_algorithm_compatibility"],
        "source_state_path": vertical.get("source_state_path"),
        "source_state_sha256": vertical["source_state_sha256"],
        "source_state_sha256_verified": True,
        "undulation_min_m": round(min(undulations), 4),
        "undulation_max_m": round(max(undulations), 4),
        "geoid": {
            "filename": geoid.describe()["filename"],
            "sha256": geoid.describe()["sha256"],
            "canonical_backend": geoid.describe()["canonical_backend"],
            "pixel_size_deg": geoid.describe()["pixel_size_deg"],
            "self_description": geoid.describe()["self_description"],
            "shared_readonly": True,
        },
        "semantics": (
            "route altitude is the frozen ALT-100 EGM2008 orthometric research premise; "
            "Cesium positions use per-vertex h = H + N, never a constant N"),
    }

    sites = []
    for site in map_projection.get("sites") or []:
        coordinate = site.get("coordinate") or [None, None]
        longitude, latitude = _number(coordinate[0]), _number(coordinate[1])
        if longitude is None or latitude is None:
            continue
        ground = _ground_block(terrain, geoid, longitude, latitude)
        devices = []
        for device in site.get("devices") or []:
            devices.append({
                **deepcopy(device),
                "assumption_state": ("research_hypothesis_proposal_only"
                                     if site.get("proposal_only") else "existing_host_input"),
                "actual_delivery": "unknown",
                "run_id": manifest.get("run_id"),
                "evidence_source": {
                    "container": "manifest.map_projection.sites",
                    "field": "devices",
                    "site_id": site.get("site_id"),
                    "layer": "C/RID/S device input radius",
                },
            })
        sites.append({
            **deepcopy(site),
            "run_id": manifest.get("run_id"),
            "ground": ground,
            "devices": devices,
            "assumption_state": ("research_hypothesis_proposal_only"
                                 if site.get("proposal_only") else "existing_host_input"),
            "actual_delivery": "unknown",
            "evidence_source": {
                "container": "manifest.map_projection.sites",
                "run_id": manifest.get("run_id"),
                "authority": "research_isolated",
            },
        })

    actions = []
    for action in map_projection.get("n_actions") or []:
        coordinate = action.get("coordinate") or [None, None]
        longitude, latitude = _number(coordinate[0]), _number(coordinate[1])
        ground = (_ground_block(terrain, geoid, longitude, latitude)
                  if longitude is not None and latitude is not None else None)
        actions.append({
            **deepcopy(action),
            "run_id": manifest.get("run_id"),
            "ground": ground,
            "assumption_state": "p16_selected_action_proposal_only",
            "actual_delivery": "unknown",
            "evidence_source": {
                "container": "manifest.map_projection.n_actions",
                "run_id": manifest.get("run_id"),
                "planner_family": action.get("planner_family"),
            },
        })

    layers = []
    for layer in map_projection.get("layers") or []:
        entry = deepcopy(layer)
        entry["run_id"] = manifest.get("run_id")
        entry["evidence_source"] = {
            "container": "manifest.map_projection.layers",
            "layer_id": layer.get("id"),
            "status_source": "manifest.business_state",
        }
        if layer.get("id") == "Radar":
            entry["features"] = []
            entry["display_rule"] = "no_geometry_unknown_never_drawn_as_coverage"
            entry["banner"] = (
                "Radar: not_calculated / unknown · 缺 radar_surveillance_layout · "
                "不绘制有效覆盖")
        layers.append(entry)

    radar = next((item for item in layers if item.get("id") == "Radar"), {})
    if radar.get("status") != "not_calculated" or radar.get("conclusion") != "unknown":
        raise Visualization3DUnavailable(
            "radar_status_not_fail_closed", "Radar 必须保持 not_calculated / unknown",
            status=radar.get("status"), conclusion=radar.get("conclusion"))
    for layer in layers:
        if layer.get("id") == "S" and layer.get("status") != "unknown":
            raise Visualization3DUnavailable(
                "s_status_not_unknown", "S 必须保持 unknown（Radar 未计算）",
                status=layer.get("status"))

    payload = {
        "schema_version": SCHEMA_VERSION,
        "run_id": manifest.get("run_id"),
        "authority": "research_isolated",
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "identity": identity,
        "semantics": (
            "read-only projection of the frozen run manifest; no coverage verdict is "
            "recomputed; coverage rings are input-radius display"),
        "vertical": vertical_public,
        "route": {
            "route_id": route.get("route_id"),
            "label": route.get("label"),
            "name": route.get("name"),
            "altitude_layer_id": route.get("altitude_layer_id"),
            "nominal_altitude_m": route.get("nominal_altitude_m"),
            "operational_route": False,
            "operational_authorization": False,
            "authority": "research_isolated",
            "points": points,
            "point_count": len(points),
        },
        "sites": sites,
        "n_actions": actions,
        "layers": layers,
        "counts": {
            "sites": len(sites),
            "devices": sum(len(site.get("devices") or []) for site in sites),
            "existing_hosts": sum(site.get("kind") == "existing_host" for site in sites),
            "planned_new_sites": sum(site.get("kind") == "planned_new_site"
                                     for site in sites),
            "n_actions": len(actions),
        },
        "terrain": terrain_public(terrain),
        "notes": [
            "三维只消费 run manifest；不重新求解、不重算覆盖、不写正式 ProjectState。",
            "航路高度按 EGM2008 正高 100 m 逐顶点换算为 WGS84 椭球高。",
            "Radar 为 not_calculated / unknown，三维不绘制任何有效覆盖几何。",
            "C/RID 覆盖圈只投影设备输入半径，不是业务覆盖结论。",
            "N 动作为 proposal_only（未安装），actual_delivery=unknown。",
            "地形为 FABDEM 30 m 显示网格，不是卫星影像，也不构成净空业务结论。",
        ],
    }
    payload["payload_sha256"] = canonical_sha256({
        key: value for key, value in payload.items()
        if key not in ("payload_sha256", "generated_at")
    })
    return payload


__all__ = [
    "CANONICAL_ORTHOMETRIC", "CESIUM_ELLIPSOIDAL", "SCHEMA_VERSION", "TERRAIN_RELATIVE",
    "Visualization3DUnavailable", "build_visualization_3d", "canonical_sha256",
    "load_terrain_asset", "sample_terrain", "terrain_public",
]
