"""Workspace and standard-grid use cases."""

import math
from copy import deepcopy

from ..risk.v1 import RiskModelV1
from ..domain.population_nodata import (
    POLICY_KEY, default_population_nodata_policy, normalize_population_nodata_policy,
)
from .project_state import assessment, empty_grid_attributes


class WorkspaceService:
    def __init__(self, session, grid_service, invalidation, snapshot):
        self.session = session
        self.grid_service = grid_service
        self.invalidation = invalidation
        self.snapshot = snapshot

    def set_workspace(self, bbox, health, preferred_grid_level=None, max_cells=None):
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise ValueError("工作区必须包含西、南、东、北四个坐标")
        values = [float(value) for value in bbox]
        west, south, east, north = values
        if not (-180 <= west < east <= 180 and -85 < south < north < 85):
            raise ValueError("工作区范围无效")
        width = math.radians(east - west) * 6371008.8 * math.cos(
            math.radians((south + north) / 2)
        )
        height = math.radians(north - south) * 6371008.8
        state = self.session.state
        self.invalidation.workflow("workspace")
        state["workspace"] = {
            "bbox": values, "area_km2": round(width * height / 1_000_000, 3),
            "health": health, "status": "passed",
        }
        state["grid"] = self.grid_service.generate(values, preferred_grid_level, max_cells)
        state["grid_attributes"] = empty_grid_attributes()
        state["grid_risk"] = RiskModelV1.empty()
        state["traffic_simulation"] = None
        state["risks"]["environment"] = assessment(
            "not_calculated", "等待当前网格属性风险评估"
        )
        state["result_statuses"]["workspace"] = "passed"
        state["result_statuses"]["grid"] = "passed"
        state["result_statuses"]["environment_risk"] = "not_calculated"
        self.session.save()
        return self.snapshot()

    # ------------------------------------------------- population NoData semantics

    def population_nodata_policy_snapshot(self):
        """The stored explicit confirmation (never invented, default = not configured)."""

        return deepcopy(
            self.session.state.get(POLICY_KEY) or default_population_nodata_policy()
        )

    def set_population_nodata_policy(self, payload):
        """Confirm (or withdraw) the population source NoData semantics.

        A confirmation only changes *how source NoData inside the raster footprint is
        interpreted*; it never touches an algorithm, a threshold, a validation verdict or an
        adoption.  Because the population grid attribute is derived, the change stales that
        attribute (and only its own downstream) so it must be recomputed explicitly.
        """

        raw = payload.get(POLICY_KEY, payload) if isinstance(payload, dict) else payload
        candidate = normalize_population_nodata_policy(raw)
        state = self.session.state
        current = normalize_population_nodata_policy(state.get(POLICY_KEY))
        if candidate == current:
            return self.snapshot()
        state[POLICY_KEY] = candidate
        self.invalidation.grid_sources(["population"])
        self.invalidation.layered_route("population_nodata_policy_changed")
        # The derived per-grid shelter field caches the population factor: drop it so the
        # next read rebuilds from the recomputed attribute.
        state.pop("_population_shelter_cache", None)
        self.session.save()
        return self.snapshot()

    def clear_workspace(self):
        state = self.session.state
        self.invalidation.workflow("workspace")
        state.update({
            "workspace": None, "grid": None,
            "grid_attributes": empty_grid_attributes(),
            "grid_risk": RiskModelV1.empty(), "traffic_simulation": None,
            "nodes": [], "scenario_routes": [], "operational_routes": [],
            "coverage": None,
        })
        state["risks"]["environment"] = assessment(
            "not_calculated", "GRC 环境/航路规划风险接口"
        )
        state["result_statuses"].update({
            "workspace": "not_calculated", "grid": "not_calculated",
            "environment_risk": "not_calculated",
        })
        self.session.save()
        return self.snapshot()
