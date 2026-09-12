"""Single application-level authority for result invalidation."""

from ..domain.status import ResultStatus
from ..risk.v1 import RiskModelV1
from ..services.invalidation import ResultLedger
from .project_state import assessment, empty_grid_attributes


class InvalidationService:
    SOURCE_ATTRIBUTES = {
        "population": ("population",), "terrain": ("terrain",),
        "basemap": ("airspace",), "airspace": ("airspace",),
        "buildings": ("buildings",), "property": ("property_exposure",),
        "property_exposure": ("property_exposure",),
        "infrastructure": ("infrastructure",),
        "obstacles": ("towers",), "towers": ("towers",),
        "traffic_simulation": ("traffic", "conflict"),
        "traffic": ("traffic", "conflict"), "conflict": ("conflict",),
    }

    def __init__(self, session):
        self.session = session

    def workflow(self, changed):
        state = self.session.state
        ledger = ResultLedger()
        for name, status in state["result_statuses"].items():
            try:
                ledger.statuses[name] = ResultStatus(status)
            except ValueError:
                ledger.statuses[name] = ResultStatus.NOT_CALCULATED
        affected = ledger.invalidate(changed)
        for name in affected:
            if name in state["result_statuses"]:
                state["result_statuses"][name] = ledger.statuses[name].value
        if "cns_gap" in affected and state.get("cns_gap_analysis", {}).get("status") != "not_calculated":
            state["cns_gap_analysis"]["status"] = "stale"
            state["result_statuses"]["cns_gap"] = "stale"
        if "coverage" in affected and state.get("coverage"):
            state["coverage"]["status"] = "stale"
        if "coverage_3d" in affected:
            self.coverage_3d()

    def grid_sources(self, changed_sources):
        state = self.session.state
        attributes = state.setdefault("grid_attributes", empty_grid_attributes())
        invalidated = False
        for source_name in changed_sources:
            kinds = self.SOURCE_ATTRIBUTES.get(source_name)
            if not kinds:
                continue
            invalidated = True
            for kind in kinds:
                if attributes.get(kind, {}).get("status") != "not_calculated":
                    attributes[kind]["status"] = "stale"
            if source_name in ("traffic_simulation", "traffic") and state.get("traffic_simulation"):
                state["traffic_simulation"]["status"] = "stale"
        if invalidated:
            self.risk()
        if "terrain" in changed_sources:
            self.coverage_3d()

    def risk(self):
        state = self.session.state
        result = state.setdefault("grid_risk", RiskModelV1.empty())
        if result.get("status") == "not_calculated":
            return
        result["status"] = "stale"
        state["result_statuses"]["environment_risk"] = "stale"
        state["risks"]["environment"] = assessment("stale", "网格风险输入属性已变化")

    def safety_policy(self):
        """Invalidate only future safety/technical/report products."""
        state = self.session.state
        self.workflow("safety_policy")
        state.setdefault("safety_assessment", {})["status"] = "stale"
        for name in ("safety_assessment", "technical_risk", "report"):
            state.setdefault("result_statuses", {})[name] = "stale"
        state.setdefault("risks", {})["technical"] = assessment(
            "stale", "Safety assessment policy 已变化；技术风险尚未重新评估"
        )

    def coverage_3d(self):
        """Stale only the additive geometric 3D result and its future consumers."""
        state = self.session.state
        result = state.get("coverage_3d") or {}
        if result.get("status") != "not_calculated":
            result["status"] = "stale"
            state["coverage_3d"] = result
            state.setdefault("result_statuses", {})["coverage_3d"] = "stale"
        if state.setdefault("result_statuses", {}).get("report") != "not_calculated":
            state["result_statuses"]["report"] = "stale"
