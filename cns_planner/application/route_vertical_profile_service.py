"""Application service for read-only route vertical profile derivation."""

from copy import deepcopy


class RouteVerticalProfileService:
    def __init__(self, session, model, invalidation, snapshot):
        self.session, self.model = session, model
        self.invalidation, self.snapshot = invalidation, snapshot

    def result_snapshot(self):
        return deepcopy(self.session.state["route_vertical_profiles"])

    def evaluate(self, sampler, payload=None):
        state = self.session.state
        # The profile collection is deliberately a full evaluation over every
        # current operational route, because the derived geometry, DTM evidence and
        # building-clearance evidence are shared across routes.  A per-route request
        # therefore cannot be honoured without faking a filter, so it is rejected
        # explicitly instead of being silently ignored.
        requested = (payload or {}).get("route_id") if isinstance(payload, dict) else None
        if requested not in (None, "", "all"):
            raise ValueError(
                "航路三维安全剖面始终按当前全部运行航路整体重算，不支持按 route_id 单独生成；"
                "该 route_id 请求已被拒绝，以避免无效的过滤语义。如需查看单条航路，请使用结果中的航路选择器。"
            )
        result = self.model.evaluate(
            state.get("operational_routes"), state.get("spatial_3d"),
            state.get("building_clearance_assessment"), sampler,
        )
        state["route_vertical_profiles"] = result
        state.setdefault("result_statuses", {})["route_vertical_profiles"] = result["status"]
        self.invalidation.report("route_vertical_profiles_evaluated")
        self.session.save()
        return self.snapshot()
