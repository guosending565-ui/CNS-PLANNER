"""Single application-level authority for result invalidation."""

from ..domain.status import ResultStatus
from ..risk.v1 import RiskModelV1
from ..domain.risk_v2 import empty_grid_risk_v2
from ..services.invalidation import ResultLedger
from .project_state import assessment, empty_grid_attributes
from ..domain.reporting import mark_active_report_stale


class InvalidationService:
    SOURCE_ATTRIBUTES = {
        "population": ("population",), "terrain": ("terrain",),
        "basemap": (), "airspace": (),
        "buildings": ("buildings",), "building_grid": ("buildings",),
        "terrain_dtm": (), "property": ("property_exposure",),
        "property_exposure": ("property_exposure",),
        "infrastructure": ("infrastructure",),
        "obstacles": ("towers",), "towers": ("towers",),
        "traffic_simulation": ("traffic", "conflict"),
        "traffic": ("traffic", "conflict"), "conflict": ("conflict",),
    }

    def __init__(self, session):
        self.session = session
        #: Injected by the composition root once the layered planner service exists.  It
        #: stales the additive ``layered_route_candidates`` / ``LayerFeasibilityMask`` and
        #: **only** those: legacy ``routes``, V3 and CNS results are never touched by a
        #: Risk Framework V2 or layered-policy change.
        self.layered_route_invalidator = None

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
        if "routes" in affected:
            for route in state.get("operational_routes") or []:
                if route.get("status") != "not_calculated":
                    route["status"] = "stale"
                    route["stale_reason"] = f"{changed}_changed"
        if "cns_gap" in affected and state.get("cns_gap_analysis", {}).get("status") != "not_calculated":
            state["cns_gap_analysis"]["status"] = "stale"
            state["result_statuses"]["cns_gap"] = "stale"
        if "coverage" in affected and state.get("coverage"):
            state["coverage"]["status"] = "stale"
        if "coverage_3d" in affected:
            self.coverage_3d()
        if "cns_service_capability" in affected:
            self.cns_service_capability()
        if "service_timeline" in affected:
            self.service_timeline()
        if "cns_gap_v2" in affected:
            self.cns_gap_v2()
        if "cns_site_plan" in affected:
            self.cns_site_plan()
        if "protection_envelope" in affected:
            self.protection_envelope()
        if "cns_corridor_assessment" in affected:
            self.cns_corridor()
        if "cns_corridor_gap_assessment" in affected:
            self.cns_corridor_gap()
        if "cns_corridor_site_plan" in affected:
            self.cns_corridor_site_plan()
        if "required_cns_recommendation" in affected:
            self.requirement_recommendation(f"{changed}_changed")
        if affected:
            mark_active_report_stale(state, f"{changed}_changed")
        if "environment_risk" in affected:
            # Risk Framework V2 consumes the same canonical grid attributes, so a
            # workspace/grid-attribute change makes the additive V2 result stale.
            # This never stales routes/CNS: the current planner still consumes the
            # legacy Risk V1 ``grid_risk`` only.
            self.risk_v2(f"{changed}_changed")
        if changed in ("workspace", "route", "route_algorithm", "spatial_3d"):
            self.building_clearance(f"{changed}_changed")
        if changed == "layered_route_planner_algorithm":
            # Selecting another registered layered planner implementation stales only the
            # additive layered candidate product; legacy routes stay untouched.
            state.setdefault("result_statuses", {})["layered_route_candidate"] = "stale"
            self.layered_route(str(changed))

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
            mark_active_report_stale(state, "data_source_profiles_changed")
            self.risk()
            self.risk_v2("grid_attribute_source_changed")
        if "terrain" in changed_sources:
            self.coverage_3d()
            self.cns_corridor()
        if set(changed_sources) & {"terrain_dtm", "buildings", "building_grid"}:
            self.building_clearance("building_source_changed")
        if "buildings" in changed_sources or "building_grid" in changed_sources:
            # The layered feasibility mask consumes the L8 building grid facts, so only the
            # layered candidates are additionally staled here.
            self.layered_route("building_grid_facts_changed")

    def route_operating_layer(self, reason="route_operating_layer_changed"):
        """Minimal Layered Operational Route Architecture V1 invalidation chain.

        Altitude layer / route-layer assignment / procedure changes stale only the vertical,
        terrain-building and downstream CNS-safety derived results.  Grid risk is never
        rewritten, and horizontal routes are not invalidated: the Layered Risk-Aware Route
        Planner V1 (not the legacy planners) is the component that makes a layer selection
        affect the horizontal route planning fingerprint — it stales only its own
        candidate/mask products.
        """

        mark_active_report_stale(self.session.state, reason)
        self.coverage_3d()
        self.building_clearance(reason)
        self.layered_route(reason)

    def building_clearance(self, reason="building_clearance_input_changed"):
        state = self.session.state
        result = state.get("building_clearance_assessment") or {}
        if result.get("status") != "not_calculated":
            result["status"] = "stale"
            result["stale_reason"] = str(reason)
            state["building_clearance_assessment"] = result
            state.setdefault("result_statuses", {})["building_clearance"] = "stale"
        mark_active_report_stale(state, reason)
        self.route_vertical_profiles(reason)

    def route_vertical_profiles(self, reason="route_vertical_profile_input_changed"):
        state = self.session.state
        result = state.get("route_vertical_profiles") or {}
        if result.get("status") != "not_calculated":
            result["status"] = "stale"
            result["stale_reason"] = str(reason)
            state["route_vertical_profiles"] = result
            state.setdefault("result_statuses", {})["route_vertical_profiles"] = "stale"
        mark_active_report_stale(state, reason)

    def report(self, reason):
        mark_active_report_stale(self.session.state, reason)

    def risk(self):
        state = self.session.state
        mark_active_report_stale(state, "grid_risk_input_changed")
        result = state.setdefault("grid_risk", RiskModelV1.empty())
        if result.get("status") == "not_calculated":
            return
        result["status"] = "stale"
        state["result_statuses"]["environment_risk"] = "stale"
        state["risks"]["environment"] = assessment("stale", "网格风险输入属性已变化")
        self.grid_risk_routes()

    def risk_v2(self, reason="risk_v2_input_changed"):
        """Stale only the additive Risk Framework V2 result.

        No confirmed V2 aggregation policy exists yet and no planner consumes
        ``grid_risk_v2``, so a V2 change must never stale the current routes,
        CNS results, legacy ``grid_risk`` or ``environment_risk``.
        """

        state = self.session.state
        result = state.get("grid_risk_v2")
        if not isinstance(result, dict):
            result = empty_grid_risk_v2()
            state["grid_risk_v2"] = result
        if result.get("status") == "not_calculated":
            return
        result["status"] = "stale"
        result["stale_reason"] = str(reason)
        state.setdefault("result_statuses", {})["grid_risk_v2"] = "stale"
        self.layered_route(reason)

    def layered_route(self, reason="layered_route_input_changed"):
        """Stale only the additive Layered Route Planner V1 candidate/mask products.

        A Risk Framework V2 change, a selected layer/request change, a terrain/building or
        building-clearance policy change, and a feasibility/cost policy change only make the
        related ``LayeredRouteCandidate`` / ``LayerFeasibilityMask`` stale.  Legacy routes,
        V3 experiments/adoptions and every CNS result are deliberately untouched.
        """

        invalidator = self.layered_route_invalidator
        if callable(invalidator):
            invalidator(str(reason))

    def grid_risk_routes(self):
        """Only Risk-Aware Route Planner V2 makes routes depend on grid_risk."""
        selection = (self.session.state.get("algorithm_selection") or {}).get("route_planner") or {}
        if (
            selection.get("algorithm_id") == "risk_aware_route_planner_v2"
            and selection.get("version") == "2.0"
        ):
            self.workflow("route_algorithm")

    def safety_policy(self):
        """Invalidate only future safety/technical/report products."""
        state = self.session.state
        mark_active_report_stale(state, "safety_policy_changed")
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
        mark_active_report_stale(state, "coverage_3d_changed")
        self.cns_service_capability()
        self.cns_corridor()

    def cns_service_capability(self):
        state = self.session.state
        result = state.get("cns_service_capability") or {}
        if result.get("status") != "not_calculated":
            result["status"] = "stale"
            state["cns_service_capability"] = result
            state.setdefault("result_statuses", {})["cns_service_capability"] = "stale"
        mark_active_report_stale(state, "cns_service_capability_changed")
        self.service_timeline()
        self.cns_corridor()

    def service_timeline(self):
        state = self.session.state
        result = state.get("service_timeline") or {}
        if result.get("status") != "not_calculated":
            result["status"] = "stale"
            state["service_timeline"] = result
            state.setdefault("result_statuses", {})["service_timeline"] = "stale"
        mark_active_report_stale(state, "service_timeline_changed")
        self.cns_gap_v2()
        self.encounter_3d("service_timeline_changed")

    def protection_envelope(self):
        state = self.session.state
        result = state.get("protection_envelope") or {}
        if result.get("status") != "not_calculated":
            result["status"] = "stale"
            state["protection_envelope"] = result
            state.setdefault("result_statuses", {})["protection_envelope"] = "stale"
        mark_active_report_stale(state, "protection_envelope_changed")
        parameters = (state.get("cns_gap_analysis_v2") or {}).get("parameters") or {}
        if parameters.get("evaluate_protection_margin") is True:
            self.cns_gap_v2()
        self.encounter_3d("protection_envelope_changed")

    def encounter_3d(self, reason="encounter_3d_input_changed"):
        state = self.session.state
        result = state.get("encounter_3d_assessment") or {}
        if result.get("status") != "not_calculated":
            result["status"] = "stale"
            result["stale_reason"] = str(reason)
            state["encounter_3d_assessment"] = result
            state.setdefault("result_statuses", {})["encounter_3d_assessment"] = "stale"
        mark_active_report_stale(state, reason)

    def cns_gap_v2(self):
        """Stale additive Gap V2 and report without touching upstream or Gap V1."""
        state = self.session.state
        result = state.get("cns_gap_analysis_v2") or {}
        if result.get("status") != "not_calculated":
            result["status"] = "stale"
            state["cns_gap_analysis_v2"] = result
            state.setdefault("result_statuses", {})["cns_gap_v2"] = "stale"
        mark_active_report_stale(state, "cns_gap_v2_changed")
        self.cns_site_plan()

    def cns_site_plan(self):
        """Stale only the P11 proposal and report; never mutate evaluated inputs."""
        state = self.session.state
        result = state.get("cns_site_plan") or {}
        if result.get("status") != "not_calculated":
            result["status"] = "stale"
            state["cns_site_plan"] = result
            state.setdefault("result_statuses", {})["cns_site_plan"] = "stale"
        mark_active_report_stale(state, "cns_site_plan_changed")
        self.closed_loop_assessment()

    def closed_loop_assessment(self):
        """Invalidate only the P12 verification product."""
        state = self.session.state
        result = state.get("closed_loop_assessment") or {}
        if result.get("status") != "not_calculated":
            result["status"] = "stale"
            result["commit_status"] = "stale_assessment"
            state["closed_loop_assessment"] = result
            state.setdefault("result_statuses", {})["closed_loop_assessment"] = "stale"

    def cns_corridor(self):
        """Stale only the P14 corridor result; never mutate centerline products."""
        state = self.session.state
        result = state.get("cns_corridor_assessment") or {}
        if result.get("status") != "not_calculated":
            result["status"] = "stale"
            state["cns_corridor_assessment"] = result
            state.setdefault("result_statuses", {})["cns_corridor_assessment"] = "stale"
        self.cns_corridor_gap()

    def cns_corridor_gap(self):
        """Stale only P15 and preserve every P1-P14 result."""
        state = self.session.state
        result = state.get("cns_corridor_gap_assessment") or {}
        if result.get("status") != "not_calculated":
            result["status"] = "stale"
            state["cns_corridor_gap_assessment"] = result
            state.setdefault("result_statuses", {})["cns_corridor_gap_assessment"] = "stale"
        self.cns_corridor_site_plan()

    def cns_corridor_site_plan(self):
        """Stale only the P16 proposal; preserve ExistingCNS and all P7-P15 products."""
        state = self.session.state
        result = state.get("cns_corridor_site_plan") or {}
        if result.get("status") != "not_calculated":
            result["status"] = "stale"
            state["cns_corridor_site_plan"] = result
            state.setdefault("result_statuses", {})["cns_corridor_site_plan"] = "stale"
        self.cns_plan_review("p16_or_review_baseline_changed")

    def cns_plan_review(self, reason="review_baseline_changed"):
        """Stale an active P18 review while retaining confirmed history snapshots."""
        state = self.session.state
        review = state.get("cns_plan_review") or {}
        if review.get("status") not in (None, "not_initialized", "stale"):
            review["status"] = "stale"
            review["stale_reason"] = reason
            state["cns_plan_review"] = review
            state.setdefault("result_statuses", {})["cns_plan_review"] = "stale"
        confirmed = state.get("confirmed_cns_plan") or {}
        if confirmed.get("status") in ("confirmed", "applied"):
            confirmed["current_applicability"] = "stale"
            confirmed["stale_reason"] = reason
            state["confirmed_cns_plan"] = confirmed
        mark_active_report_stale(state, reason)

    def requirement_recommendation(self, reason="recommendation_input_changed"):
        """Stale only P17; preserve formal RequiredCNS and every planning result."""
        state = self.session.state
        result = state.get("required_cns_recommendation") or {}
        if result.get("status") != "not_calculated":
            result["status"] = "stale"
            result["stale_reason"] = reason
            state["required_cns_recommendation"] = result
            state.setdefault("result_statuses", {})["required_cns_recommendation"] = "stale"
        mark_active_report_stale(state, reason)
