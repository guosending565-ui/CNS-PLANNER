"""Use cases for additive vertical configuration and geometric 3D coverage."""

from copy import deepcopy

from ..domain.spatial_3d import (
    normalize_altitude_layer, normalize_route_altitude_profile,
)


def is_locked_v3_profile(profile):
    """A V3-D derived profile is locked against manual altitude edits.

    The lock is owned by the operational adoption: revoking the adoption removes the
    profile (or leaves it unlocked), after which a user may edit or replace it normally.
    """

    if not isinstance(profile, dict):
        return False
    if str(profile.get("source") or "") != "v3c_validated_route":
        return False
    return bool(profile.get("derived")) and bool(profile.get("locked_by_adoption"))


class Spatial3DService:
    def __init__(self, session, model, invalidation, snapshot):
        self.session, self.model = session, model
        self.invalidation, self.snapshot = invalidation, snapshot

    def spatial_snapshot(self):
        return deepcopy(self.session.state["spatial_3d"])

    def coverage_snapshot(self):
        return deepcopy(self.session.state["coverage_3d"])

    def set_altitude_layers(self, payload):
        items = payload.get("altitude_layers") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise ValueError("altitude_layers 必须是数组")
        layers = [normalize_altitude_layer(item) for item in items]
        identifiers = [item["altitude_layer_id"] for item in layers]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("altitude_layer_id 不得重复")
        self.session.state["spatial_3d"]["altitude_layers"] = layers
        self._refresh_status()
        self.invalidation.coverage_3d()
        self.invalidation.building_clearance("altitude_layers_changed")
        return self._save()

    def set_route_profile(self, payload):
        profile = normalize_route_altitude_profile(payload)
        route_ids = {str(item.get("route_id")) for item in self.session.state.get("operational_routes") or []}
        if profile["route_id"] not in route_ids:
            raise ValueError("航路高度剖面对应的运行航路不存在")
        existing = (self.session.state["spatial_3d"]["route_altitude_profiles"] or {}).get(
            profile["route_id"]
        )
        if is_locked_v3_profile(existing):
            raise ValueError(
                "该运行航路由 V3-D operational adoption 管理，其高度剖面由 V3-C validated route "
                "导出并锁定：撤销 adoption 后才允许手工修改高度"
            )
        self.session.state["spatial_3d"]["route_altitude_profiles"][profile["route_id"]] = profile
        self._refresh_status()
        self.invalidation.coverage_3d()
        self.invalidation.building_clearance("route_altitude_profile_changed")
        return self._save()

    def evaluate(self, payload=None):
        state = self.session.state
        parameters = (payload or {}).get("parameters") if isinstance(payload, dict) else None
        if parameters:
            model = self.model.__class__(parameters)
        else:
            model = self.model
        facilities = deepcopy(state.get("existing_cns_facilities") or {})
        for facility in facilities.get("items") or []:
            if isinstance(facility, dict):
                # P11 planning metadata is not a P7 geometric input.  Keep it
                # out of the established P7 input fingerprint.
                facility.pop("planning_profile", None)
        result = model.evaluate(
            state.get("operational_routes"), state.get("spatial_3d"),
            state.get("grid"), state.get("grid_attributes"),
            facilities, state.get("device_catalog"),
        )
        state["coverage_3d"] = result
        self.invalidation.cns_service_capability()
        state["result_statuses"]["coverage_3d"] = result["status"]
        state["result_statuses"]["report"] = "not_calculated"
        return self._save()

    def _save(self):
        self.session.save()
        return self.snapshot()

    def _refresh_status(self):
        spatial = self.session.state["spatial_3d"]
        configured = [
            *(spatial.get("altitude_layers") or []),
            *(spatial.get("route_altitude_profiles") or {}).values(),
            *(spatial.get("site_vertical_profiles") or {}).values(),
        ]
        spatial["status"] = "passed" if configured and all(item.get("status") in ("passed", "confirmed") for item in configured) else "pending_confirmation"
