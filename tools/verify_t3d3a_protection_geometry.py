"""Round37-T3D-3A：P17 保护区几何与几何身份匹配的独立数值复算。

只读脚本。它**不**复用三维服务的投影路径，而是：

1. 从权威状态读取 P17 ``continuous_service_acceptability.routes[0].corridor``；
2. 用独立的 shapely/pyproj 调用重建 EPSG:32651 米制缓冲区；
3. 与 ``/api/visualization-3d/protection-volumes`` 的实际响应逐项比对；
4. 独立重算三份 R0005 几何的指纹，确认历史走廊只匹配其中一份。

不写 ProjectState、不调用 P17 算法、不发起任何在线影像请求。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pyproj import Transformer
from shapely.geometry import LineString, mapping
from shapely.ops import transform

from cns_planner.application.visualization_3d_protection_volume import (
    route_path_fingerprint,
)

CONSTRUCTION_CRS = "EPSG:32651"
RELATIVE_TOLERANCE = 1e-9
ABSOLUTE_TOLERANCE = 1e-6


def _items(container):
    if isinstance(container, list):
        return container
    if isinstance(container, dict):
        values = container.get("items")
        if isinstance(values, list):
            return values
        if isinstance(values, dict):
            return list(values.values())
    return []


def _vertices(node):
    """把 GeoJSON 坐标树展平成顶点列表（保留每个顶点的 [x, y]）。"""

    if not isinstance(node, (list, tuple)) or not node:
        return []
    if isinstance(node[0], (int, float)):
        return [node]
    vertices = []
    for item in node:
        vertices.extend(_vertices(item))
    return vertices


def _metric_line(path):
    to_metric = Transformer.from_crs("EPSG:4326", CONSTRUCTION_CRS, always_xy=True)
    return transform(
        to_metric.transform,
        LineString([(float(point[0]), float(point[1])) for point in path]),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("state_path", type=Path)
    parser.add_argument("response_path", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    state = json.loads(args.state_path.read_text(encoding="utf-8"))
    response = json.loads(args.response_path.read_text(encoding="utf-8-sig"))
    result = {"schema": "round37-t3d-3a-protection-numeric-verification/1"}

    # ---------------------------------------------------------------- P17 输入
    csa = state.get("continuous_service_acceptability") or {}
    route = (_items(csa.get("routes")) or [{}])[0]
    corridor = route.get("corridor") or {}
    path = corridor.get("route_path") or []
    width = float(corridor["outer_half_width_m"])
    result["p17_input"] = {
        "container": "continuous_service_acceptability.routes[0].corridor",
        "status": csa.get("status"),
        "algorithm_version": csa.get("algorithm_version"),
        "input_fingerprint": csa.get("input_fingerprint"),
        "route_id": route.get("route_id"),
        "route_path_points": len(path),
        "route_length_m": corridor.get("route_length_m"),
        "outer_half_width_m": width,
        "cns_requirement_corridor_half_width_m": corridor.get(
            "cns_requirement_corridor_half_width_m"),
        "D_protection_m": corridor.get("D_protection_m"),
        "formula": corridor.get("formula"),
        "route_path_fingerprint": route_path_fingerprint(path),
    }

    # ------------------------------------------------- 独立重建缓冲区并比对
    line = _metric_line(path)
    polygon = line.buffer(width, quad_segs=16, cap_style=1, join_style=1)
    served_volume = next(
        (row for row in response.get("volumes") or []
         if (row.get("geometry") or {}).get("status") == "available"), None)
    if served_volume is None:
        result["geometry_verification"] = {"status": "no_available_volume"}
    else:
        served = served_volume["geometry"]
        to_wgs84 = Transformer.from_crs(CONSTRUCTION_CRS, "EPSG:4326", always_xy=True)
        wgs84 = transform(to_wgs84.transform, polygon)
        recomputed = {
            "projected_area_m2": float(polygon.area),
            "projected_perimeter_m": float(polygon.length),
            "route_length_projected_m": float(line.length),
            "boundary_ring_count": len(polygon.interiors) + 1,
        }
        checks = {}
        for key, value in recomputed.items():
            expected = served.get(key)
            if key == "boundary_ring_count":
                delta = abs(int(value) - int(expected))
            else:
                delta = abs(float(value) - float(expected))
            checks[key] = {
                "recomputed": value,
                "served": expected,
                "abs_delta": delta,
                "match": delta <= max(ABSOLUTE_TOLERANCE,
                                      abs(float(value)) * RELATIVE_TOLERANCE),
            }
        recomputed_bounds = [float(value) for value in polygon.bounds]
        served_bounds = [float(value) for value in served.get("projected_bounds_m") or []]
        bounds_delta = max(abs(a - b) for a, b in zip(recomputed_bounds, served_bounds))
        checks["projected_bounds_m"] = {
            "recomputed": recomputed_bounds,
            "served": served_bounds,
            "abs_delta": bounds_delta,
            "match": bounds_delta <= 1e-6,
        }
        served_coords = (served.get("geometry") or {}).get("coordinates") or []
        recomputed_coords = mapping(wgs84)["coordinates"]
        served_flat = _vertices(served_coords)
        recomputed_flat = _vertices(recomputed_coords)
        coord_delta = max(
            (abs(float(a) - float(b)) for pair_a, pair_b in
             zip(served_flat, recomputed_flat) for a, b in zip(pair_a, pair_b)),
            default=None)
        checks["geometry_vertex_count"] = {
            "recomputed": len(recomputed_flat),
            "served": len(served_flat),
            "match": len(recomputed_flat) == len(served_flat),
        }
        checks["geometry_vertex_max_abs_delta_deg"] = {
            "value": coord_delta,
            "match": coord_delta is not None and coord_delta <= 1e-9,
        }
        result["geometry_verification"] = {
            "status": "recomputed_independently",
            "construction": {
                "crs": CONSTRUCTION_CRS,
                "cap_style": "round",
                "join_style": "round",
                "quad_segs": 16,
                "half_width_m": width,
            },
            "checks": checks,
            "all_match": all(row.get("match") for row in checks.values()),
            "vertical_disclosure": {
                "vertical_extent_status": served_volume.get("vertical_extent_status"),
                "nominal_altitude_slice_egm2008_m": served_volume.get(
                    "nominal_altitude_slice_egm2008_m"),
                "lower_altitude_m": served_volume.get("lower_altitude_m"),
                "upper_altitude_m": served_volume.get("upper_altitude_m"),
                "self_added_thickness": False,
            },
        }

    # ------------------------------------------- 三份 R0005 几何的指纹对照
    geometries = []
    seen = set()

    def add(label, authority, source, geometry_path):
        if not isinstance(geometry_path, list) or len(geometry_path) < 2:
            return
        fingerprint = route_path_fingerprint(geometry_path)
        if (label, fingerprint) in seen:
            return
        seen.add((label, fingerprint))
        geometries.append({
            "label": label,
            "authority": authority,
            "source": source,
            "point_count": len(geometry_path),
            "route_path_fingerprint": fingerprint,
            "projected_length_m": float(_metric_line(geometry_path).length),
        })

    for entry in _items(state.get("operational_routes")):
        if isinstance(entry, dict):
            add("canonical_operational_routes", "canonical_operational_routes",
                "operational_routes[].path", entry.get("path"))
    adoption_container = state.get("layered_operational_adoptions") or {}
    for entry in _items(adoption_container):
        if not isinstance(entry, dict):
            continue
        candidate = entry.get("candidate_id") or ""
        is_current = str(entry.get("current_applicability") or "") == "current"
        for side in ("after", "before"):
            route_row = (entry.get(side) or {}).get("route") or {}
            status = str(route_row.get("status") or "unknown")
            if side == "after" and status == "engineering_demo_only":
                label = "demo_projection_current" if is_current else "demo_projection_historical"
            elif side == "after":
                label = "adoption_after"
            else:
                label = "adoption_before"
            add(label, status,
                f"layered_operational_adoptions[{candidate}].{side}.route.path",
                route_row.get("path"))

    corridor_fingerprint = result["p17_input"]["route_path_fingerprint"]
    for row in geometries:
        row["is_p17_corridor_geometry"] = row["route_path_fingerprint"] == corridor_fingerprint
    result["route_geometry_identities"] = geometries
    result["corridor_match"] = {
        "p17_corridor_fingerprint": corridor_fingerprint,
        "matching_labels": [row["label"] for row in geometries
                            if row["is_p17_corridor_geometry"]],
        "demo_projection_shares_geometry": any(
            row["label"] == "demo_projection_current" and row["is_p17_corridor_geometry"]
            for row in geometries),
        "current_demo_point_count": next(
            (row["point_count"] for row in geometries
             if row["label"] == "demo_projection_current"), None),
    }

    # ------------------------------------------ 响应中的冲突判定与缺证披露
    served_conflict = [row for row in response.get("volumes") or []
                       if (row.get("geometry") or {}).get("status") == "blocked"]
    result["served_conflicts"] = [{
        "object_id": row.get("object_id"),
        "route_authority": row.get("route_authority"),
        "status": row.get("status"),
        "reason": (row.get("geometry") or {}).get("reason"),
        "required_evidence": row.get("required_evidence"),
    } for row in served_conflict]
    result["response_summary"] = {
        "status": response.get("status"),
        "volume_count": response.get("volume_count"),
        "available_horizontal_count": response.get("available_horizontal_count"),
        "blocked_count": response.get("blocked_count"),
        "revision": response.get("revision"),
        "writes_performed": response.get("writes_performed"),
        "algorithms_invoked": response.get("algorithms_invoked"),
        "intrusion_contract_status": (response.get("intrusion_contract") or {}).get("status"),
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")

    verification = result.get("geometry_verification") or {}
    print("p17 route_path points:", result["p17_input"]["route_path_points"])
    print("p17 outer_half_width_m:", result["p17_input"]["outer_half_width_m"])
    print("geometry verification:", verification.get("status"),
          "all_match=", verification.get("all_match"))
    for key, row in (verification.get("checks") or {}).items():
        print(f"  {key:38s} match={row.get('match')} delta={row.get('abs_delta', row.get('value'))}")
    print("corridor matches:", result["corridor_match"]["matching_labels"])
    print("demo shares corridor geometry:",
          result["corridor_match"]["demo_projection_shares_geometry"])
    for row in geometries:
        print(f"  {row['label']:26s} points={row['point_count']:3d} "
              f"len={row['projected_length_m']:.6f} {row['route_path_fingerprint'][:24]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
