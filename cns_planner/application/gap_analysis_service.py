"""Application use case for persisted CNS Gap Analysis."""

from copy import deepcopy

from ..catalogs import AircraftCNSProfileCatalog
from .production_write_authority import write_runtime_compatibility_result


class GapAnalysisService:
    def __init__(self, session, analyzer, snapshot):
        self.session, self.analyzer, self.snapshot = session, analyzer, snapshot

    def analyze(self):
        state = self.session.state
        profile = AircraftCNSProfileCatalog.find(
            state.get("aircraft_profiles") or {}, state.get("selected_aircraft_profile_id") or ""
        )
        catalog = state.get("device_catalog") or {}
        facilities = state.get("existing_cns_facilities") or {}
        if getattr(self.analyzer, "algorithm_id", None) == "cns_gap_analysis_v1":
            profile, catalog, facilities = self._v1_inputs(profile, catalog, facilities)
        result = self.analyzer.analyze(
            state.get("operational_routes") or [], state.get("required_cns") or {}, profile,
            facilities, catalog,
        )
        record = write_runtime_compatibility_result(
            self.session, "cns_gap_analysis", result,
            source_algorithm={
                "algorithm_type": "cns_gap_analyzer",
                "algorithm_id": getattr(self.analyzer, "algorithm_id", None),
                "algorithm_version": getattr(self.analyzer, "algorithm_version", None),
                "class": type(self.analyzer).__name__,
            },
            note="旧版 CNS 缺口分析仅在当前会话运行，不写项目、不触发 canonical 失效。",
        )
        response = self.snapshot()
        response["compatibility_cns_gap_analysis"] = deepcopy(record)
        response["cns_gap_analysis"] = deepcopy(record)
        return response

    @staticmethod
    def _v1_inputs(profile, catalog, facilities=None):
        """Exclude post-V1 additive fields from GapV1 behavior and fingerprints."""
        profile_view = deepcopy(profile)
        if profile_view:
            for name in ("communication", "navigation", "surveillance"):
                capability = profile_view.get(name)
                if isinstance(capability, dict):
                    capability.pop("reliability", None)
                    capability.pop("fallbacks", None)
        catalog_view = deepcopy(catalog)
        for device in catalog_view.get("items", []):
            if isinstance(device, dict):
                device.pop("reliability", None)
                device.pop("vertical_profile", None)
                device.pop("coverage_geometry", None)
                device.pop("service_model", None)
        facilities_view = deepcopy(facilities or {})
        for facility in facilities_view.get("items", []):
            if not isinstance(facility, dict):
                continue
            facility.pop("vertical_profile", None)
            facility.pop("planning_profile", None)
            facility.pop("planning_origin", None)
            for device in facility.get("devices", []):
                if isinstance(device, dict):
                    device.pop("planning_origin", None)
                    device.pop("vertical_profile", None)
                    device.pop("coverage_geometry", None)
                    device.pop("service_model", None)
        return profile_view, catalog_view, facilities_view
