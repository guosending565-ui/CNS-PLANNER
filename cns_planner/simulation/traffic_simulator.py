"""Reproducible straight-line UAV traffic simulator."""

from __future__ import annotations

import hashlib
import json
import math
import random


EARTH_RADIUS_M = 6_371_008.8


class TrafficSimulator:
    algorithm_id = "uav-traffic-simulator-straight-line-v1"
    algorithm_version = "1.0"

    def simulate(self, parameters):
        config = self._validate(parameters or {})
        randomizer = random.Random(config["random_seed"])
        trajectories = []
        for index in range(config["uav_count"]):
            od = config["od_pairs"][index % len(config["od_pairs"])]
            speed = randomizer.uniform(*config["speed_range_mps"])
            trajectories.append(self._trajectory(
                f"UAV-{index + 1:04d}", od["start"], od["end"], speed,
                config["altitude_m"], config["simulation_seconds"], config["timestep_seconds"],
            ))
        fingerprint = hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        return {
            "status": "passed",
            "algorithm_id": self.algorithm_id,
            "algorithm_version": self.algorithm_version,
            "parameters": config,
            "input_fingerprint": fingerprint,
            "simulation_seconds": config["simulation_seconds"],
            "timestep_seconds": config["timestep_seconds"],
            "trajectory_count": len(trajectories),
            "trajectories": trajectories,
        }

    def _trajectory(self, uav_id, start, end, speed, altitude, duration, timestep):
        distance = self._distance_m(start, end)
        travel_seconds = distance / speed
        active_seconds = min(duration, travel_seconds)
        times = [index * timestep for index in range(int(active_seconds // timestep) + 1)]
        if not times or times[-1] < active_seconds:
            times.append(active_seconds)
        samples = []
        for time_s in times:
            ratio = min(1.0, time_s / travel_seconds)
            samples.append({
                "time_s": time_s,
                "coordinate": [
                    start[0] + (end[0] - start[0]) * ratio,
                    start[1] + (end[1] - start[1]) * ratio,
                ],
                "altitude_m": altitude,
            })
        return {
            "uav_id": uav_id,
            "origin": list(start),
            "destination": list(end),
            "speed_mps": speed,
            "altitude_m": altitude,
            "active_seconds": active_seconds,
            "samples": samples,
        }

    @staticmethod
    def _validate(parameters):
        count = int(parameters.get("uav_count", 0))
        duration = float(parameters.get("simulation_seconds", 0))
        timestep = float(parameters.get("timestep_seconds", 0))
        altitude = float(parameters.get("altitude_m", 0))
        speed_range = [float(value) for value in parameters.get("speed_range_mps", [])]
        od_pairs = parameters.get("od_pairs") or []
        if count <= 0 or duration <= 0 or timestep <= 0:
            raise ValueError("UAV数量、仿真时间和timestep必须为正数")
        if len(speed_range) != 2 or not 0 < speed_range[0] <= speed_range[1]:
            raise ValueError("速度范围必须包含两个正数")
        clean_pairs = []
        for item in od_pairs:
            start, end = item.get("start"), item.get("end")
            if not TrafficSimulator._coordinate(start) or not TrafficSimulator._coordinate(end) or start == end:
                raise ValueError("每个OD必须包含不同的WGS84起终点")
            clean_pairs.append({"start": [float(v) for v in start], "end": [float(v) for v in end]})
        if not clean_pairs:
            raise ValueError("至少需要一个OD")
        return {
            "uav_count": count,
            "od_pairs": clean_pairs,
            "speed_range_mps": speed_range,
            "altitude_m": altitude,
            "simulation_seconds": duration,
            "timestep_seconds": timestep,
            "random_seed": int(parameters.get("random_seed", 0)),
        }

    @staticmethod
    def _coordinate(value):
        return isinstance(value, (list, tuple)) and len(value) == 2 and all(
            isinstance(item, (int, float)) and math.isfinite(item) for item in value
        ) and -180 <= value[0] <= 180 and -90 <= value[1] <= 90

    @staticmethod
    def _distance_m(start, end):
        lon1, lat1, lon2, lat2 = map(math.radians, (*start, *end))
        dlon, dlat = lon2 - lon1, lat2 - lat1
        value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(value))
