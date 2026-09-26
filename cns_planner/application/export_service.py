"""Read-only legacy and confirmed-plan delivery exports."""

from copy import deepcopy
from hashlib import sha256
import json

from ..domain.reporting import sanitize_report_value
from .production_write_authority import (
    canonical_operational_routes, runtime_compatibility_operational_routes,
    runtime_compatibility_result,
)
from ..compatibility.catalog import capability_metadata
from ..compatibility.project_adapter import read_existing_legacy


class ExportService:
    def __init__(self, session, snapshot):
        self.session, self.snapshot = session, snapshot

    def project(self):
        document = self.snapshot()
        selection = (self.session.state.get("algorithm_selection") or {}).get("route_planner") or {}
        route_capability = {
            "route_planner_v1": "RoutePlannerV1",
            "risk_aware_route_planner_v2": "RiskAwareRoutePlannerV2",
        }.get(selection.get("algorithm_id"))
        if route_capability:
            metadata = capability_metadata(route_capability)
            document["operational_routes"] = [
                {**deepcopy(route), **metadata}
                for route in self.session.state.get("operational_routes") or []
            ]
        compatibility_views = {}
        for key, capability_id in (
            ("coverage", "CoveragePlannerV1"),
            ("cns_gap_analysis", "CNSGapAnalyzerV1"),
            ("cns_gap_analysis_v2", "CNSGapAnalyzerV2"),
            ("cns_site_plan", "ReuseFirstSitePlannerV1"),
        ):
            if key in self.session.state:
                compatibility_views[key] = read_existing_legacy(
                    self.session.state, key, capability_id,
                )
        if compatibility_views:
            document["compatibility_views"] = compatibility_views
        return json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")

    def routes(self):
        """兼容导出：canonical 正式航路优先；没有正式航路时才导出旧算法兼容试算。

        兼容试算的 feature 显式标注 ``authoritative=false / compatibility=true /
        deprecated=true``，避免被下游当成正式运行航路。
        """

        state = self.session.state
        saved_route_selection = (state.get("algorithm_selection") or {}).get("route_planner") or {}
        saved_legacy_route = saved_route_selection.get("algorithm_id") in {
            "route_planner_v1", "risk_aware_route_planner_v2",
        }
        canonical = [] if saved_legacy_route else canonical_operational_routes(state)
        compatibility = saved_legacy_route or not canonical
        routes = canonical if canonical else runtime_compatibility_operational_routes(self.session)
        if saved_legacy_route and not routes:
            routes = list(state.get("operational_routes") or [])
        features = []
        for route in routes:
            properties = {
                key: route.get(key)
                for key in ("route_id", "status", "reason", "algorithm_id", "algorithm_version")
            }
            if compatibility:
                properties.update({
                    "authoritative": False, "compatibility": True, "deprecated": True,
                    "warning": "旧版兼容试算航路，不是正式运行航路",
                })
            features.append({
                "type": "Feature",
                "properties": properties,
                "geometry": {"type": "LineString", "coordinates": route.get("path", [])},
            })
        return self._collection(features)

    def sites(self):
        runtime = runtime_compatibility_result(self.session, "coverage")
        coverage = runtime or self.session.state.get("coverage") or {}
        features = []
        for subsystem, layer in coverage.get("layers", {}).items():
            for station in layer.get("stations", []):
                properties = {key: value for key, value in station.items() if key != "coordinate"}
                properties["subsystem"] = subsystem
                properties.update({
                    "authoritative": False,
                    "compatibility": True,
                    "deprecated": True,
                    "warning": "旧版二维覆盖试算站点，不用于正式规划",
                })
                features.append({"type": "Feature", "properties": properties, "geometry": {"type": "Point", "coordinates": station["coordinate"]}})
        return self._collection(features)

    def confirmed_facilities(self):
        """Final/proposed P18 facilities; deliberately ignores legacy coverage stations."""
        state = self.session.state
        plan = state.get("confirmed_cns_plan") or {}
        facilities = deepcopy((state.get("existing_cns_facilities") or {}).get("items") or [])
        if plan.get("status") == "confirmed":
            for action in plan.get("confirmed_actions") or []:
                facility_id = action.get("facility_id")
                facility = next((item for item in facilities if str(item.get("facility_id")) == str(facility_id)), None) if facility_id else None
                if facility is None:
                    coordinate = deepcopy(action.get("coordinate"))
                    if not isinstance(coordinate, list) or len(coordinate) < 2:
                        continue
                    identity = sha256(f"{action.get('reuse_class')}|{action.get('site_id')}".encode()).hexdigest()[:16]
                    facility = {
                        "facility_id": f"confirmed-proposal:{identity}", "site_id": action.get("site_id"),
                        "name": f"已确认待应用 {action.get('site_id')}", "coordinate": coordinate,
                        "vertical_profile": deepcopy(action.get("vertical") or {}), "devices": [],
                        "status": "confirmed_not_applied", "source": "P18 confirmed plan",
                    }
                    facilities.append(facility)
                if not any(str(item.get("device_id")) == str(action.get("device_id")) for item in facility.get("devices") or []):
                    facility.setdefault("devices", []).append({
                        "device_id": action.get("device_id"), "subsystem": action.get("subsystem"),
                        "status": "confirmed_not_applied", "source_action_id": action.get("action_id"),
                    })
        features = []
        for facility in facilities:
            coordinate = facility.get("coordinate")
            if not isinstance(coordinate, list) or len(coordinate) < 2:
                continue
            properties = sanitize_report_value({key: deepcopy(value) for key, value in facility.items() if key != "coordinate"})
            properties["plan_id"] = plan.get("plan_id")
            properties["plan_status"] = plan.get("status")
            features.append({"type": "Feature", "properties": properties,
                             "geometry": {"type": "Point", "coordinates": coordinate}})
        return self._collection(features)

    def algorithms(self, catalog):
        return json.dumps(sanitize_report_value({
            "selection": deepcopy(self.session.state.get("algorithm_selection") or {}),
            "manifests": deepcopy(catalog or []),
        }), ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")

    @staticmethod
    def _collection(features):
        return json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False, indent=2).encode("utf-8")
