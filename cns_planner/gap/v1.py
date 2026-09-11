"""Deterministic horizontal-coverage CNS Gap Analysis V1."""

from __future__ import annotations

from hashlib import sha256
import json
from math import cos, hypot, radians, sqrt


EARTH_RADIUS_M = 6_371_008.8
SUBSYSTEM_NAMES = {"C": "communication", "N": "navigation", "S": "surveillance"}


class CNSGapAnalyzerV1:
    algorithm_id = "cns_gap_analysis_v1"
    algorithm_version = "1.0"

    @classmethod
    def empty(cls):
        return {
            "status": "not_calculated", "algorithm_id": cls.algorithm_id,
            "algorithm_version": cls.algorithm_version, "input_fingerprint": None,
            "route_count": 0, "routes": [],
        }

    def analyze(self, operational_routes, required_cns, aircraft_profile, existing_facilities, device_catalog):
        inputs = [operational_routes, required_cns, aircraft_profile, existing_facilities, device_catalog]
        fingerprint = self._fingerprint(inputs)
        route_results = []
        for route in operational_routes or []:
            requirements = (required_cns.get("route_overrides") or {}).get(route.get("route_id")) or required_cns.get("project_default") or {}
            subsystems = [
                self._subsystem(route, code, requirements.get(name) or {}, aircraft_profile, existing_facilities, device_catalog)
                for code, name in SUBSYSTEM_NAMES.items()
            ]
            route_results.append({
                "route_id": str(route.get("route_id") or ""),
                "status": self._aggregate([item["status"] for item in subsystems]),
                "subsystems": subsystems,
            })
        return {
            "status": self._aggregate([item["status"] for item in route_results]) if route_results else "missing_data",
            "algorithm_id": self.algorithm_id, "algorithm_version": self.algorithm_version,
            "input_fingerprint": fingerprint, "route_count": len(route_results), "routes": route_results,
        }

    def _subsystem(self, route, subsystem, requirement, aircraft_profile, facilities, catalog):
        route_id = str(route.get("route_id") or "")
        fingerprint = self._fingerprint([route, subsystem, requirement, aircraft_profile, facilities, catalog])
        required = requirement.get("required")
        base = {
            "route_id": route_id, "subsystem": subsystem, "required": required,
            "aircraft_capability_satisfied": None,
            "ground_coverage": {"status": "not_calculated", "covered_length_m": None, "route_length_m": None, "facility_count": 0, "required_redundancy": requirement.get("redundancy")},
            "uncovered_segments": [], "coverage_ratio": None, "gap_length_m": None,
            "reasons": [], "input_fingerprint": fingerprint,
        }
        if required is False:
            return {**base, "status": "not_applicable", "coverage_ratio": 1.0, "gap_length_m": 0.0,
                    "ground_coverage": {**base["ground_coverage"], "status": "not_applicable"},
                    "reasons": ["该航路未要求此分系统"]}
        path = route.get("path") or []
        if route.get("status") != "passed" or len(path) < 2:
            return {**base, "status": "missing_data", "reasons": ["运行航路缺少有效 geometry"]}

        capability, capability_reason = self._aircraft_capability(aircraft_profile, subsystem)
        coverage = self._coverage(path, subsystem, requirement, facilities, catalog)
        reasons = [capability_reason] if capability_reason else []
        reasons.extend(coverage.pop("reasons"))
        result = {
            **base, "aircraft_capability_satisfied": capability,
            "ground_coverage": coverage["ground_coverage"],
            "uncovered_segments": coverage["uncovered_segments"],
            "coverage_ratio": coverage["coverage_ratio"], "gap_length_m": coverage["gap_length_m"],
            "reasons": reasons,
        }
        if required is None or requirement.get("status") == "pending_confirmation":
            result["status"] = "pending_confirmation"
            result["reasons"].insert(0, "RequiredCNS 尚未确认")
        elif capability is None:
            result["status"] = "pending_confirmation" if aircraft_profile else "missing_data"
        elif coverage["status"] in ("missing_data", "pending_confirmation"):
            result["status"] = coverage["status"]
        else:
            target = float(requirement.get("coverage_requirement") or 0) / 100.0
            max_gap = requirement.get("max_gap_m")
            gap_ok = max_gap is None or coverage["max_gap_length_m"] <= float(max_gap) + 1e-6
            satisfied = capability and coverage["coverage_ratio"] + 1e-12 >= target and gap_ok
            result["status"] = "passed" if satisfied else "gap"
            if not capability:
                result["reasons"].append("飞行器缺少已确认的机载能力")
            if coverage["coverage_ratio"] + 1e-12 < target:
                result["reasons"].append("地面覆盖率低于 RequiredCNS")
            if not gap_ok:
                result["reasons"].append("最大连续缺口超过 RequiredCNS")
        return result

    def _coverage(self, path, subsystem, requirement, facilities, catalog):
        route_xy, reference = self._project(path)
        catalog_by_id = {item.get("device_id"): item for item in catalog.get("items", [])}
        sources, unresolved, performance_unknown, performance_rejected = [], False, False, False
        collection_ready = facilities.get("status") == "passed"
        for facility in facilities.get("items", []):
            if facility.get("status", "active") != "active":
                continue
            for installed in facility.get("devices", []):
                if installed.get("subsystem") != subsystem or installed.get("status", "active") != "active":
                    continue
                catalog_device = catalog_by_id.get(installed.get("device_id")) or {}
                device = {**catalog_device, **installed}
                device["performance"] = {
                    **(catalog_device.get("parameter_metadata") or {}), **(catalog_device.get("metadata") or {}),
                    **(installed.get("metadata") or {}), **(installed.get("parameter_metadata") or {}),
                }
                if device.get("enabled") is False:
                    continue
                radius = device.get("radius_m") or (device.get("metadata") or {}).get("coverage_radius_m")
                if radius is None or float(radius) <= 0:
                    unresolved = True
                    continue
                performance = self._device_performance(device, requirement, subsystem)
                if performance is None:
                    performance_unknown = True
                    continue
                if performance is False:
                    performance_rejected = True
                    continue
                sources.append((self._project_point(facility["coordinate"], reference), float(radius), facility.get("facility_id")))
        redundancy = requirement.get("redundancy")
        required_count = max(1, int(redundancy)) if redundancy is not None else 1
        offset, covered_length, uncovered, max_gap = 0.0, 0.0, [], 0.0
        open_gap = None
        for index, (start, end) in enumerate(zip(route_xy, route_xy[1:])):
            segment_length = hypot(end[0] - start[0], end[1] - start[1])
            raw = [interval for center, radius, _ in sources if (interval := self._circle_interval(start, end, center, radius))]
            covered = self._at_least(raw, required_count)
            covered_length += sum((right - left) * segment_length for left, right in covered)
            gaps = self._complement(covered)
            for left, right in gaps:
                gap_start, gap_end = offset + left * segment_length, offset + right * segment_length
                coordinates = [self._interpolate(path[index], path[index + 1], left), self._interpolate(path[index], path[index + 1], right)]
                if open_gap and abs(open_gap["route_offset_end_m"] - gap_start) < 1e-6:
                    open_gap["path"].append(coordinates[1]); open_gap["end"] = coordinates[1]
                    open_gap["route_offset_end_m"] = gap_end; open_gap["length_m"] += gap_end - gap_start
                else:
                    open_gap = {"start": coordinates[0], "end": coordinates[1], "path": coordinates,
                                "route_offset_start_m": gap_start, "route_offset_end_m": gap_end,
                                "length_m": gap_end - gap_start}
                    uncovered.append(open_gap)
                max_gap = max(max_gap, open_gap["length_m"])
            if not gaps or gaps[-1][1] < 1.0 - 1e-12:
                open_gap = None
            offset += segment_length
        route_length = offset
        gap_length = max(0.0, route_length - covered_length)
        reasons = []
        status = "passed"
        if not collection_ready:
            status = "missing_data"; reasons.append("已有 CNS 设施数据未加载")
        elif (unresolved or performance_unknown) and not sources:
            status = "missing_data"; reasons.append("匹配设备缺少 coverage radius")
            if performance_unknown:
                reasons[-1] = "匹配设备缺少 RequiredCNS 所需性能元数据"
        elif (unresolved or performance_unknown) and gap_length > 1e-6:
            status = "pending_confirmation"; reasons.append("部分匹配设备缺少 coverage radius，缺口仍待确认")
            if performance_unknown:
                reasons[-1] = "部分匹配设备性能元数据不完整，缺口仍待确认"
        elif not sources:
            reasons.append("没有该分系统的可用已有设施")
        if performance_rejected:
            reasons.append("部分已有设备不满足 RequiredCNS 性能条件")
        return {
            "status": status, "coverage_ratio": covered_length / route_length if route_length else 0.0,
            "gap_length_m": gap_length, "max_gap_length_m": max_gap,
            "uncovered_segments": uncovered, "reasons": reasons,
            "ground_coverage": {"status": status, "covered_length_m": covered_length,
                                "route_length_m": route_length, "facility_count": len({item[2] for item in sources}),
                                "required_redundancy": required_count, "model": "horizontal_radius"},
        }

    @staticmethod
    def _device_performance(device, requirement, subsystem):
        fields = {
            "C": (("latency_ms", "latency_ms", "maximum"),),
            "N": (("accuracy_m", "accuracy_m", "maximum"), ("integrity", "integrity", "equal")),
            "S": (("update_interval_s", "update_interval_s", "maximum"),),
        }[subsystem]
        performance = device.get("performance") or {}
        for required_key, device_key, comparison in fields:
            required = requirement.get(required_key)
            if required in (None, ""):
                continue
            actual = device.get(device_key, performance.get(device_key))
            if actual in (None, ""):
                return None
            if comparison == "maximum" and float(actual) > float(required):
                return False
            if comparison == "equal" and str(actual).casefold() != str(required).casefold():
                return False
        return True

    @staticmethod
    def _aircraft_capability(profile, subsystem):
        if not profile:
            return None, "未选择 AircraftCNSProfile"
        capability = profile.get(SUBSYSTEM_NAMES[subsystem]) or {}
        if capability.get("status") not in ("confirmed", "passed"):
            return None, "飞行器机载能力尚未确认"
        return bool(capability.get("capabilities")), None

    @staticmethod
    def _project(path):
        reference = sum(point[1] for point in path) / len(path)
        return [CNSGapAnalyzerV1._project_point(point, reference) for point in path], reference

    @staticmethod
    def _project_point(point, reference_lat):
        return [EARTH_RADIUS_M * radians(point[0]) * cos(radians(reference_lat)), EARTH_RADIUS_M * radians(point[1])]

    @staticmethod
    def _circle_interval(start, end, center, radius):
        dx, dy = end[0] - start[0], end[1] - start[1]
        fx, fy = start[0] - center[0], start[1] - center[1]
        a = dx * dx + dy * dy
        if a <= 0:
            return None
        b, c = 2 * (fx * dx + fy * dy), fx * fx + fy * fy - radius * radius
        discriminant = b * b - 4 * a * c
        if discriminant < 0:
            return (0.0, 1.0) if c <= 0 else None
        root = sqrt(max(0.0, discriminant))
        left, right = max(0.0, (-b - root) / (2 * a)), min(1.0, (-b + root) / (2 * a))
        return (left, right) if right > left else None

    @staticmethod
    def _at_least(intervals, count):
        events = []
        for left, right in intervals:
            events.extend(((left, 1), (right, -1)))
        events.sort(key=lambda item: (item[0], -item[1]))
        result, active, start = [], 0, None
        for position, delta in events:
            before = active; active += delta
            if before < count <= active:
                start = position
            elif before >= count > active and start is not None:
                result.append((start, position)); start = None
        return result

    @staticmethod
    def _complement(intervals):
        result, cursor = [], 0.0
        for left, right in intervals:
            if left > cursor + 1e-12:
                result.append((cursor, left))
            cursor = max(cursor, right)
        if cursor < 1.0 - 1e-12:
            result.append((cursor, 1.0))
        return result

    @staticmethod
    def _interpolate(start, end, value):
        return [start[0] + (end[0] - start[0]) * value, start[1] + (end[1] - start[1]) * value]

    @staticmethod
    def _fingerprint(value):
        return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @staticmethod
    def _aggregate(statuses):
        active = [status for status in statuses if status != "not_applicable"]
        if not active:
            return "not_applicable"
        for status in ("missing_data", "pending_confirmation", "gap", "failed", "stale"):
            if status in active:
                return status
        return "passed" if all(status == "passed" for status in active) else "not_calculated"
