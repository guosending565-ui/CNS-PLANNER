"""Read-only orchestration for navigation-monitoring simulations."""

from copy import deepcopy
from hashlib import sha256
import json

from pyproj import CRS, Transformer

from ..domain.layered_route_validation import path_fingerprint
from ..domain.navigation_monitoring_simulation import simulate_navigation_monitoring


_NAVIGATION_MONITOR_SERVICE_KEYS = {"N", "N:navigation_integrity_monitoring"}


def _fingerprint(value, prefix):
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return prefix + sha256(body.encode("utf-8")).hexdigest()


def _items(value):
    return value.get("items") or [] if isinstance(value, dict) else []


class NavigationMonitoringSimulationService:
    """Resolve authoritative route facts and run the pure Domain kernel."""

    def __init__(
        self, session, candidate_service, artifact_read_service, *,
        candidate_applicability=None, risk_profile_snapshot=None,
    ):
        self.session = session
        self.candidate_service = candidate_service
        self.artifact_read_service = artifact_read_service
        self.candidate_applicability = candidate_applicability
        self.risk_profile_snapshot = risk_profile_snapshot

    def simulate(self, payload=None):
        payload = {} if payload is None else payload
        if not isinstance(payload, dict):
            raise ValueError("导航监测仿真请求必须是对象")
        route = self._resolve_route(payload)
        expected = payload.get("route_geometry_fingerprint")
        if expected not in (None, "") and str(expected) != route["route_geometry_fingerprint"]:
            raise ValueError("请求航路几何指纹与当前权威几何不一致")

        candidate_snapshot = self.candidate_service.snapshot()
        candidate_snapshot_fingerprint = _fingerprint([
            {
                "candidate_id": item.get("candidate_id"),
                "coordinate": item.get("coordinate"),
                "source_crs": (item.get("source_crs") or {}).get("value"),
                "spatial_use_status": item.get("spatial_use_status"),
            }
            for item in candidate_snapshot.get("candidates") or []
        ], "navsites-")
        metric_route, metric_candidates, construction_crs = self._metric_inputs(
            route, candidate_snapshot,
        )
        route["construction_crs"] = construction_crs
        endpoint_site_ids = self._endpoint_site_ids(route)
        risk_intervals, risk_status, risk_identity, risk_reasons = self._risk_context(route)
        reusable_devices = self._reusable_navigation_devices(
            candidate_snapshot.get("candidates") or [],
        )
        result = simulate_navigation_monitoring(
            route=metric_route,
            candidates=metric_candidates,
            endpoint_site_ids=endpoint_site_ids,
            parameters=payload.get("parameters"),
            risk_intervals=risk_intervals,
            segment_speeds=payload.get("segment_speeds"),
            reusable_devices=reusable_devices,
        )
        altitude_status, altitude_reason = self._three_dimensional_status(route["route_id"])
        result.update({
            "route_identity": {
                key: route.get(key) for key in (
                    "route_authority", "route_id", "adoption_id", "candidate_id",
                    "candidate_fingerprint", "validation_id", "source_crs",
                    "construction_crs", "point_count", "route_geometry_fingerprint",
                    "currentness",
                )
            },
            "candidate_snapshot_fingerprint": candidate_snapshot_fingerprint,
            "candidate_counts": deepcopy(candidate_snapshot.get("counts") or {}),
            "risk_priority_status": risk_status,
            "risk_identity": risk_identity,
            "risk_unavailable_reasons": risk_reasons,
            "three_dimensional_assurance_status": altitude_status,
            "three_dimensional_assurance_reason": altitude_reason,
            "route_elevation_used_for_association": False,
            "writes_performed": 0,
        })
        return result

    def _resolve_route(self, payload):
        source = str(payload.get("route_source") or "").strip()
        if not source:
            source = "engineering_demo" if self._current_demo_adoption(payload.get("route_id")) else "operational_route"
        if source == "engineering_demo":
            adoption = self._current_demo_adoption(payload.get("route_id"))
            if adoption is None:
                raise ValueError("当前没有可用于仿真的工程演示采纳航路")
            record = ((adoption.get("after") or {}).get("route")
                      or (adoption.get("before") or {}).get("route") or {})
            authority = "engineering_demo_projection"
        elif source in {"operational_route", "canonical_operational_route"}:
            record = self._canonical_route(payload.get("route_id"))
            if record is None:
                raise ValueError("未找到指定的正式运行航路")
            adoption = self._current_strict_adoption(record.get("route_id")) or {}
            authority = "canonical_operational_route"
        else:
            raise ValueError("route_source 仅支持 engineering_demo 或 operational_route")
        path = self._clean_path(record.get("path"))
        route_id = str(record.get("route_id") or adoption.get("route_id") or "")
        if not route_id or len(path) < 2:
            raise ValueError("当前航路缺少可用的完整坐标几何")
        provenance = record.get("provenance") or {}
        source_crs = str(record.get("path_crs") or "OGC:CRS84")
        return {
            "route_authority": authority,
            "route_id": route_id,
            "adoption_id": adoption.get("adoption_id"),
            "candidate_id": adoption.get("candidate_id") or provenance.get("candidate_id"),
            "candidate_fingerprint": adoption.get("candidate_fingerprint") or provenance.get("candidate_fingerprint"),
            "validation_id": adoption.get("validation_id") or provenance.get("validation_id"),
            "source_crs": source_crs,
            "point_count": len(path),
            "route_geometry_fingerprint": path_fingerprint(path),
            "currentness": adoption.get("current_applicability") or "current",
            "path": path,
            "start_node_id": record.get("start_node_id"),
            "end_node_id": record.get("end_node_id"),
        }

    def _current_demo_adoption(self, route_id=None):
        for item in _items(self.session.state.get("layered_operational_adoptions")):
            if (item.get("status") == "published_engineering_demo"
                    and item.get("current_applicability") == "current"
                    and (not route_id or str(item.get("route_id")) == str(route_id))):
                return item
        return None

    def _current_strict_adoption(self, route_id):
        for item in _items(self.session.state.get("layered_operational_adoptions")):
            if (item.get("status") == "published"
                    and item.get("current_applicability") == "current"
                    and str(item.get("route_id")) == str(route_id)):
                return item
        return None

    def _canonical_route(self, route_id=None):
        routes = self.session.state.get("operational_routes") or []
        matches = [item for item in routes if not route_id or str(item.get("route_id")) == str(route_id)]
        if len(matches) != 1:
            return None
        return matches[0]

    @staticmethod
    def _clean_path(path):
        clean = []
        for point in path or []:
            if isinstance(point, (list, tuple)) and len(point) >= 2:
                clean.append([float(point[0]), float(point[1])])
        return clean

    def _endpoint_site_ids(self, route):
        start, end = route.get("start_node_id"), route.get("end_node_id")
        if not start or not end:
            scenario = next((
                item for item in self.session.state.get("scenario_routes") or []
                if str(item.get("route_id")) == route["route_id"]
            ), {})
            start, end = start or scenario.get("start_node_id"), end or scenario.get("end_node_id")
        if not start or not end:
            raise ValueError("航路缺少起终点稳定站址身份")
        return [f"node:{start}", f"node:{end}"]

    def _metric_inputs(self, route, candidate_snapshot):
        source = CRS.from_user_input(route["source_crs"])
        centroid_lon = sum(point[0] for point in route["path"]) / len(route["path"])
        centroid_lat = sum(point[1] for point in route["path"]) / len(route["path"])
        if source.is_geographic:
            zone = max(1, min(60, int((centroid_lon + 180) // 6) + 1))
            construction = CRS.from_epsg((32600 if centroid_lat >= 0 else 32700) + zone)
        elif source.axis_info and all((axis.unit_name or "").lower() in {"metre", "meter"} for axis in source.axis_info):
            construction = source
        else:
            raise ValueError("航路 CRS 不是可确认的米制或地理坐标系")
        route_transformer = Transformer.from_crs(source, construction, always_xy=True)
        metric_route = [list(route_transformer.transform(*point)) for point in route["path"]]
        metric_candidates = []
        for item in candidate_snapshot.get("candidates") or []:
            if item.get("spatial_use_status") != "eligible" or not item.get("coordinate"):
                continue
            source_value = (item.get("source_crs") or {}).get("value")
            if not source_value:
                continue
            transformer = Transformer.from_crs(source_value, construction, always_xy=True)
            x, y = transformer.transform(*item["coordinate"])
            metric_candidates.append({
                "candidate_id": item.get("candidate_id"),
                "candidate_kind": item.get("candidate_kind"),
                "coordinate": [x, y],
                "spatial_use_status": "eligible",
            })
        return metric_route, metric_candidates, construction.to_string()

    def _hydrated_candidates(self):
        container = self.session.state.get("layered_route_candidates") or {}
        inline = [item for item in container.get("items") or [] if isinstance(item, dict)]
        if not inline:
            refs = ((self.session.state.get("artifact_manifest") or {}).get("refs") or {})
            externalized = bool(
                refs.get("layered_route_candidates")
                or container.get("detail_status") == "externalized"
            )
            if externalized:
                response = self.artifact_read_service.content("layered_route_candidates")
                inline = [
                    item.get("record") for item in response.get("records") or []
                    if isinstance(item.get("record"), dict)
                    and item["record"].get("candidate_id")
                ]
        projector = self.candidate_applicability
        if not callable(projector):
            return inline
        return [
            {**deepcopy(item), "current_applicability": projector(item)}
            for item in inline
        ]

    def _risk_context(self, route):
        reasons = []
        candidate = next((
            item for item in self._hydrated_candidates()
            if item.get("candidate_id") == route.get("candidate_id")
        ), None)
        if candidate is None:
            reasons.append("current_candidate_unavailable")
        else:
            if candidate.get("current_applicability") not in (None, "current"):
                reasons.append("candidate_not_current")
            if candidate.get("candidate_fingerprint") != route.get("candidate_fingerprint"):
                reasons.append("candidate_fingerprint_mismatch")
            if path_fingerprint(self._clean_path(candidate.get("path"))) != route["route_geometry_fingerprint"]:
                reasons.append("candidate_geometry_fingerprint_mismatch")
        profile_source = (
            self.risk_profile_snapshot()
            if callable(self.risk_profile_snapshot)
            else self.session.state.get("route_risk_profiles")
        )
        profiles = _items(profile_source)
        profile = next((item for item in profiles if item.get("status") == "passed" and item.get("current_applicability") == "current"), None)
        if profile is None:
            reasons.append("current_risk_profile_unavailable")
        else:
            profile_candidate = profile.get("candidate") or {}
            profile_route = profile.get("route") or {}
            if profile_candidate.get("candidate_id") != route.get("candidate_id"):
                reasons.append("candidate_id_mismatch")
            if profile_candidate.get("candidate_fingerprint") != route.get("candidate_fingerprint"):
                reasons.append("candidate_fingerprint_mismatch")
            if str(profile_route.get("route_id")) != route["route_id"]:
                reasons.append("route_id_mismatch")
            if profile_route.get("path_fingerprint") != route["route_geometry_fingerprint"]:
                reasons.append("risk_geometry_fingerprint_mismatch")
        reasons = sorted(set(reasons))
        if reasons:
            return [], "unavailable", None, reasons
        intervals = []
        for domain in (profile.get("domains") or {}).values():
            high = domain.get("high_risk") or {}
            if high.get("status") != "available":
                continue
            for item in high.get("intervals") or []:
                start = item.get("start_distance_m")
                end = item.get("end_distance_m")
                if start is not None and end is not None:
                    intervals.append([float(start), float(end)])
        return intervals, "available", {
            "profile_id": profile.get("profile_id"),
            "profile_fingerprint": (profile.get("fingerprints") or {}).get("profile_fingerprint"),
            "candidate_id": route.get("candidate_id"),
            "candidate_fingerprint": route.get("candidate_fingerprint"),
            "route_geometry_fingerprint": route["route_geometry_fingerprint"],
        }, []

    def _reusable_navigation_devices(self, candidates):
        candidates_by_source = {str(item.get("source_id")): item.get("candidate_id") for item in candidates}
        reusable = []
        for facility in _items(self.session.state.get("existing_cns_facilities")):
            if facility.get("status") not in {"active", "passed", "installed"}:
                continue
            source_id = str(facility.get("site_id") or "")
            candidate_id = candidates_by_source.get(source_id)
            if not candidate_id:
                continue
            expected_distinct = f"takeoff_landing_site:{source_id}"
            distinct = facility.get("distinct_site_id")
            if distinct not in (None, "", expected_distinct):
                continue
            for device in facility.get("devices") or []:
                if (device.get("service_key") in _NAVIGATION_MONITOR_SERVICE_KEYS
                        and device.get("status") in {"active", "passed", "installed"}
                        and device.get("device_id")):
                    reusable.append({
                        "device_id": str(device["device_id"]), "service_key": "N",
                        "site_id": candidate_id, "status": "installed",
                    })
        return reusable

    def _three_dimensional_status(self, route_id):
        profiles = ((self.session.state.get("spatial_3d") or {}).get("route_altitude_profiles") or {})
        profile = profiles.get(route_id) if isinstance(profiles, dict) else None
        if isinstance(profile, dict) and profile.get("status") in {"confirmed", "passed"}:
            return "available", None
        return "not_evaluated", "route_elevation_evidence_unavailable"
