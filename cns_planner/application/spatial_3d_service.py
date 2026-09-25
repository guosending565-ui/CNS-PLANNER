"""Use cases for additive vertical configuration and geometric 3D coverage."""

from copy import deepcopy

from ..domain.spatial_3d import (
    normalize_altitude_layer, normalize_route_altitude_profile,
)
from ..domain.altitude_layer_defaults import mark_altitude_layer_catalog_managed
from .route_operating_layer_service import (
    refresh_spatial_status, resync_operating_layer_statuses,
)
from .production_write_authority import assert_write_authority


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
        # 批量写入 = 用户显式管理过 catalog：之后不再自动补建工程默认高度层
        # （即使这次写入把目录清空），由前端对空目录给出明确提示。
        mark_altitude_layer_catalog_managed(self.session.state)
        # The catalogue write also re-evaluates the explicit cruise-layer assignments and
        # procedure statuses: a layer that loses its nominal altitude/confirmation can never
        # leave a referencing assignment silently ``confirmed``.
        resync_operating_layer_statuses(self.session.state)
        self._refresh_status()
        self.invalidation.route_operating_layer("altitude_layers_changed")
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
        assert_write_authority(self, "coverage_3d")
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
        refresh_spatial_status(self.session.state)
