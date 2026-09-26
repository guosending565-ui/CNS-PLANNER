"""Replaceable deterministic C/N/S coverage planner for workflow integration."""
from __future__ import annotations

from hashlib import sha256
import json
import math

from ...domain.geodesy import distance_m


def interpolate_path(path, spacing_m):
    if len(path) < 2:
        return path
    points = [path[0]]
    for a, b in zip(path, path[1:]):
        length = distance_m(a, b)
        count = max(1, math.ceil(length / spacing_m))
        points.extend([[a[0] + (b[0] - a[0]) * i / count, a[1] + (b[1] - a[1]) * i / count] for i in range(1, count + 1)])
    return points


class CoveragePlannerV1:
    algorithm_id = "coverage_planner_v1"
    algorithm_version = "1.0"

    def __init__(self, defaults: dict):
        parameters = defaults["engineering_parameters"]
        self.spacing_factor = float(parameters["primary_spacing_factor"]["value"])
        self.colocation_radius_m = float(parameters["co_location_search_radius_m"]["value"])
        self.parameter_source = {
            "primary_spacing_factor": parameters["primary_spacing_factor"],
            "co_location_search_radius_m": parameters["co_location_search_radius_m"],
        }

    def plan(self, routes: list[dict], devices: list[dict]) -> dict:
        if not routes or any(route.get("status") != "passed" for route in routes):
            raise ValueError("所有运行航路必须先成功生成")
        layers, physical_sites = {}, []
        for subsystem in ("C", "N", "S"):
            available = [item for item in devices if item["subsystem"] == subsystem and item.get("enabled", True)]
            primary = next((item for item in available if item["role"] == "primary"), None)
            gap = next((item for item in available if item["role"] == "gap"), primary)
            if primary is None:
                layers[subsystem] = {"status": "missing_data", "stations": [], "statistics": self._empty_stats(), "message": "缺少主站设备"}
                continue
            stations = []
            for route in routes:
                samples = interpolate_path(route["path"], max(250.0, primary["radius_m"] * self.spacing_factor))
                for index, coordinate in enumerate(samples):
                    role = "primary"
                    station = self._station(subsystem, route["route_id"], coordinate, primary, role, index, physical_sites)
                    stations.append(station)
            coverage_samples = []
            for route in routes:
                samples = interpolate_path(route["path"], 500.0)
                for index, coordinate in enumerate(samples):
                    count = sum(distance_m(coordinate, station["coordinate"]) <= station["radius_m"] for station in stations)
                    if count == 0 and gap is not None:
                        station = self._station(subsystem, route["route_id"], coordinate, gap, "gap", len(stations), physical_sites)
                        stations.append(station)
                        count = 1
                    coverage_samples.append({"route_id": route["route_id"], "index": index, "coordinate": coordinate, "count": count})
            uncovered = [item for item in coverage_samples if item["count"] == 0]
            stats = {
                "stations": len(stations),
                "primary": sum(item["role"] == "primary" for item in stations),
                "gap": sum(item["role"] == "gap" for item in stations),
                "colocated": sum(item["colocated"] for item in stations),
                "average_multiplicity": round(sum(item["count"] for item in coverage_samples) / max(1, len(coverage_samples)), 2),
                "uncovered_samples": len(uncovered),
                "uncovered_segments": self._uncovered_segments(uncovered),
            }
            layers[subsystem] = {"status": "passed" if not uncovered else "failed", "stations": stations, "statistics": stats, "message": "初版覆盖计算完成"}
        fingerprint = sha256(json.dumps([routes, devices, self.parameter_source], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return {"status": "passed" if all(value["status"] == "passed" for value in layers.values()) else "failed", "layers": layers,
                "physical_sites": physical_sites, "algorithm_id": self.algorithm_id, "algorithm_version": self.algorithm_version,
                "input_fingerprint": fingerprint, "parameters": self.parameter_source}

    def _station(self, subsystem, route_id, coordinate, device, role, index, physical_sites):
        nearest = min(physical_sites, key=lambda site: distance_m(coordinate, site["coordinate"]), default=None)
        colocated = bool(nearest and distance_m(coordinate, nearest["coordinate"]) <= self.colocation_radius_m)
        if colocated:
            site_id, final_coordinate = nearest["site_id"], nearest["coordinate"]
        else:
            site_id = f"SITE-{len(physical_sites) + 1:04d}"
            final_coordinate = [round(coordinate[0], 7), round(coordinate[1], 7)]
            physical_sites.append({"site_id": site_id, "coordinate": final_coordinate, "source": "CoveragePlannerV1 拟建点"})
        return {"station_id": f"{subsystem}-{route_id}-{index + 1:03d}", "site_id": site_id, "subsystem": subsystem,
                "route_id": route_id, "coordinate": final_coordinate, "role": role, "colocated": colocated,
                "device_id": device["device_id"], "radius_m": device["radius_m"], "mtbf_h": device["mtbf_h"]}

    @staticmethod
    def _empty_stats():
        return {"stations": 0, "primary": 0, "gap": 0, "colocated": 0, "average_multiplicity": 0, "uncovered_samples": 0, "uncovered_segments": []}

    @staticmethod
    def _uncovered_segments(samples):
        groups = []
        for item in samples:
            if not groups or groups[-1][-1]["route_id"] != item["route_id"] or groups[-1][-1]["index"] + 1 != item["index"]:
                groups.append([])
            groups[-1].append(item)
        return [{"route_id": group[0]["route_id"], "start": group[0]["coordinate"], "end": group[-1]["coordinate"]} for group in groups]
