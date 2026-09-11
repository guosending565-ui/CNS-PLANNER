"""Two-dimensional closest-point-of-approach conflict detector."""

from __future__ import annotations

import math


class ConflictDetector:
    algorithm_id = "two-dimensional-cpa-v1"
    algorithm_version = "1.0"

    def detect(self, trajectories, parameters):
        config = self._validate(parameters or {})
        states = {item["uav_id"]: self._segments(item) for item in trajectories}
        identifiers = sorted(states)
        events = []
        last_event = {}
        for left_index, left_id in enumerate(identifiers):
            for right_id in identifiers[left_index + 1:]:
                pair = (left_id, right_id)
                for left, right in self._overlapping(states[left_id], states[right_id]):
                    event = self._cpa(pair, left, right, config)
                    if not event:
                        continue
                    previous = last_event.get(pair)
                    if previous is not None and event["time_s"] - previous < config["conflict_cooldown_seconds"]:
                        continue
                    last_event[pair] = event["time_s"]
                    events.append(event)
        events.sort(key=lambda item: (item["time_s"], item["pair"]))
        return {
            "status": "passed",
            "algorithm_id": self.algorithm_id,
            "algorithm_version": self.algorithm_version,
            "parameters": config,
            "conflict_count": len(events),
            "events": events,
        }

    @staticmethod
    def _segments(trajectory):
        samples = trajectory.get("samples") or []
        return [
            {"start": left, "end": right}
            for left, right in zip(samples, samples[1:])
            if float(right["time_s"]) > float(left["time_s"])
        ]

    @staticmethod
    def _overlapping(left_segments, right_segments):
        for left in left_segments:
            for right in right_segments:
                start = max(float(left["start"]["time_s"]), float(right["start"]["time_s"]))
                end = min(float(left["end"]["time_s"]), float(right["end"]["time_s"]))
                if start < end:
                    yield ConflictDetector._segment_at(left, start, end), ConflictDetector._segment_at(right, start, end)

    @staticmethod
    def _segment_at(segment, start, end):
        t0, t1 = float(segment["start"]["time_s"]), float(segment["end"]["time_s"])
        p0, p1 = segment["start"]["coordinate"], segment["end"]["coordinate"]
        interpolate = lambda time_s: [
            p0[index] + (p1[index] - p0[index]) * ((time_s - t0) / (t1 - t0))
            for index in (0, 1)
        ]
        return {"time_s": start, "duration_s": end - start, "start": interpolate(start), "end": interpolate(end)}

    @staticmethod
    def _cpa(pair, left, right, config):
        reference_lat = (left["start"][1] + right["start"][1]) / 2
        scale_x = 111_320.0 * math.cos(math.radians(reference_lat))
        scale_y = 110_574.0
        origin = left["start"]
        project = lambda point: ((point[0] - origin[0]) * scale_x, (point[1] - origin[1]) * scale_y)
        lp0, lp1, rp0, rp1 = project(left["start"]), project(left["end"]), project(right["start"]), project(right["end"])
        duration = min(left["duration_s"], right["duration_s"])
        lv = ((lp1[0] - lp0[0]) / duration, (lp1[1] - lp0[1]) / duration)
        rv = ((rp1[0] - rp0[0]) / duration, (rp1[1] - rp0[1]) / duration)
        relative_position = (rp0[0] - lp0[0], rp0[1] - lp0[1])
        relative_velocity = (rv[0] - lv[0], rv[1] - lv[1])
        velocity_squared = relative_velocity[0] ** 2 + relative_velocity[1] ** 2
        if velocity_squared <= 1e-12:
            return None
        time_to_cpa = -(
            relative_position[0] * relative_velocity[0] + relative_position[1] * relative_velocity[1]
        ) / velocity_squared
        if not 0 < time_to_cpa < config["lookahead_seconds"] or time_to_cpa > duration:
            return None
        left_cpa = (lp0[0] + lv[0] * time_to_cpa, lp0[1] + lv[1] * time_to_cpa)
        right_cpa = (rp0[0] + rv[0] * time_to_cpa, rp0[1] + rv[1] * time_to_cpa)
        distance = math.hypot(right_cpa[0] - left_cpa[0], right_cpa[1] - left_cpa[1])
        if distance >= config["safe_distance_m"]:
            return None
        midpoint = ((left_cpa[0] + right_cpa[0]) / 2, (left_cpa[1] + right_cpa[1]) / 2)
        return {
            "pair": list(pair),
            "time_s": left["time_s"] + time_to_cpa,
            "time_to_cpa_s": time_to_cpa,
            "distance_cpa_m": distance,
            "coordinate": [origin[0] + midpoint[0] / scale_x, origin[1] + midpoint[1] / scale_y],
            "status": "potential_conflict",
        }

    @staticmethod
    def _validate(parameters):
        safe_distance = float(parameters.get("safe_distance_m", 0))
        lookahead = float(parameters.get("lookahead_seconds", 0))
        cooldown = float(parameters.get("conflict_cooldown_seconds", 0))
        if safe_distance <= 0 or lookahead <= 0 or cooldown < 0:
            raise ValueError("CPA距离和前视时间必须为正数，cooldown不得为负数")
        return {
            "safe_distance_m": safe_distance,
            "lookahead_seconds": lookahead,
            "conflict_cooldown_seconds": cooldown,
            "semantics": "potential_conflict_engineering_parameters",
        }
