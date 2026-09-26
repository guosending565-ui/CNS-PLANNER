"""Single application-level authority for result invalidation."""

from ..domain.status import ResultStatus
from ..domain.layered_operational_adoption import SOURCE_TYPE as LAYERED_ADOPTION_SOURCE_TYPE
from ..risk.v1 import RiskModelV1
from ..domain.risk_v2 import empty_grid_risk_v2
from ..services.invalidation import ResultLedger
from .production_write_authority import mark_runtime_compatibility_stale
from .project_state import assessment, empty_grid_attributes
from ..domain.reporting import mark_active_report_stale

#: The direct downstream consumers of an explicit Production Route3DProfile V1 change.
DOWNSTREAM_RESULTS = ("coverage_3d", "route_vertical_profiles", "route_safety_evidence_v2")

class InvalidationService:
    SOURCE_ATTRIBUTES = {
        "population": ("population",), "terrain": ("terrain",),
        "basemap": (), "airspace": (),
        "buildings": ("buildings",), "building_grid": ("buildings",),
        "terrain_dtm": (), "property": ("property_exposure",),
        "property_exposure": ("property_exposure",),
        "infrastructure": ("infrastructure",),
        # BUG-TOWERS-002：真实铁塔**不是**网格属性，也不是人口风险因子。它过去被映射到
        # ``grid_attributes["towers"]``（一个永远空的容器），于是每次 towers 源变化都会
        # 连带把 grid_risk / grid_risk_v2 / environment_risk 标 stale —— 而风险数学从未
        # 读取过铁塔。现在铁塔走自己的定向链路（``tower_data_changed``）。
        "obstacles": (), "towers": (),
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
        #: Injected by the composition root once the RouteRiskProfile service exists.  It
        #: stales the additive ``route_risk_profiles`` (and **only** those) when the candidate
        #: set, ``grid_risk_v2`` or the profile policy changes.
        self.route_risk_profile_invalidator = None
        #: Production candidate validation has its own evidence lifecycle.  Candidate/layer,
        #: native sources and clearance changes stale it without touching V3 history.
        self.layered_route_validation_invalidator = None
        #: Additive Production Route3DProfile V1.  A route/layer/procedure change stales the
        #: stored profile through this invalidator — and **never** the other way round: a
        #: profile change never stales a Theta* candidate, a validation or an adoption.
        self.route_3d_profile_invalidator = None
        #: Vertical Transition Continuous Validation V1 (climb/descent source-native geometry).
        #: Every profile/route/adoption/cruise-validation/source change stales it; a transition
        #: change only stales the Safety Evidence and the report.  It is never a *source* of
        #: upstream invalidation: it can never stale a Theta* candidate, a RouteRiskProfile, a
        #: LayeredRouteValidation, a Route3DProfile or an adoption.
        self.vertical_transition_validation_invalidator = None
        #: Route Safety Evidence V2 aggregates the existing lineage evidence.  It is staled by
        #: every dependency change below — and it is strictly downstream: a stale Safety
        #: Evidence V2 never stales a route, a candidate, a validation, an adoption or any CNS
        #: result.
        self.route_safety_evidence_invalidator = None
        #: Radar Surveillance Layout V1（additive，proposal-only）。它消费已发布运行航路、
        #: 真实铁塔站址 + 雷达原点高度、FABDEM 地形、陆域掩膜与自身策略。任何一项变化都
        #: 只把 ``radar_surveillance_layout`` 标 stale —— 绝不动 routes / CNS / coverage_3d /
        #: 走廊与站址提案，也绝不反向触发任何上游。
        self.radar_surveillance_layout_invalidator = None

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
                if (
                    route.get("status") != "not_calculated"
                    and self._should_stale_operational_route(route, changed)
                ):
                    route["status"] = "stale"
                    route["stale_reason"] = f"{changed}_changed"
        if "coverage_3d" in affected:
            self.coverage_3d()
        if "cns_service_capability" in affected:
            self.cns_service_capability()
        if "service_timeline" in affected:
            self.service_timeline()
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
        if changed in ("workspace", "route", "spatial_3d"):
            self.building_clearance(f"{changed}_changed")
        if changed in ("workspace", "spatial_3d"):
            self.planning_constraint_field(f"{changed}_changed")
        if changed == "layered_route_planner_algorithm":
            # Selecting another registered layered planner implementation stales only the
            # additive layered candidate product; legacy routes stay untouched.
            state.setdefault("result_statuses", {})["layered_route_candidate"] = "stale"
            self.layered_route(str(changed))
        if changed in ("route", "workspace", "spatial_3d"):
            # Radar Surveillance Layout V1 消费已发布运行航路与固定高度层：航路/工作区/高度层
            # 配置变化只把该 additive 产物标 stale（proposal-only，unidirectional）。
            self.radar_surveillance_layout(f"{changed}_changed")

    @staticmethod
    def _should_stale_operational_route(route, changed):
        """Keep aircraft invalidation selective without changing other route semantics.

        A production layered adoption owns its route independently of the selected aircraft
        profile.  Its downstream CNS products still invalidate through ``DEPENDENTS``; only
        the owned operational route is preserved.  Existing stale routes are left untouched
        rather than being promoted back to ``passed``.
        """

        if changed != "aircraft_profile":
            return True
        provenance = route.get("provenance") or {}
        is_layered_adoption_owned = bool(
            provenance.get("source_type") == LAYERED_ADOPTION_SOURCE_TYPE
            and str(provenance.get("adoption_id") or "").strip()
            and str(provenance.get("adoption_fingerprint") or "").strip()
        )
        return not is_layered_adoption_owned

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
        if set(changed_sources) & {"terrain_dtm", "buildings"}:
            self.planning_constraint_field(
                "source_changed:" + ",".join(sorted(changed_sources))
            )
            self.layered_route_validation("source_changed:" + ",".join(sorted(changed_sources)))
            # The transition validation binds the terrain/building source audits, so its
            # stored records are stale as soon as those sources change.
            self.vertical_transition_validation(
                "source_changed:" + ",".join(sorted(set(changed_sources) & {"terrain_dtm", "buildings"}))
            )
        if "buildings" in changed_sources or "building_grid" in changed_sources:
            # The layered feasibility mask consumes the L8 building grid facts, so only the
            # layered candidates are additionally staled here.
            self.layered_route("building_grid_facts_changed")
        if set(changed_sources) & {"towers", "obstacles"}:
            # 真实铁塔源变化：派生事实（障碍物高度 / 共塔候选）先过时，再定向失效其下游。
            # 绝不经过 risk()/risk_v2()：塔不是风险输入。
            self.tower_data_changed("tower_source_changed", include_derived=True)
        if set(changed_sources) & {"airspace"}:
            self.planning_constraint_field("restricted_area_source_changed")
        if set(changed_sources) & {"terrain_dtm", "buildings", "building_grid", "land_mask"}:
            # 雷达初步划设消费 FABDEM 地形正高与显式陆域掩膜；任一变化只 stale 它自己。
            self.radar_surveillance_layout(
                "source_changed:" + ",".join(
                    sorted(set(changed_sources) & {
                        "terrain_dtm", "buildings", "building_grid", "land_mask",
                    })
                )
            )

    def tower_data_changed(self, reason="tower_data_changed", *, include_derived=False):
        """真实铁塔数据或其派生事实变化时的**定向**失效。

        只失效真正消费铁塔的下游：

        * （可选）``tower_obstacle_profiles`` / ``tower_colocation_candidates`` 派生事实；
        * ``layered_route_candidate`` + ``LayerFeasibilityMask``（塔净空进入可行性判定），
          并沿用既有语义连带 ``route_risk_profiles`` / ``layered_route_validations`` /
          Safety Evidence —— 因为候选航路本身变了，这些剖面确实需要重算；
        * ``cns_corridor_site_plan``（共塔宿主候选变了）；
        * 当前 active report。

        **绝不**失效 ``grid_risk`` / ``grid_risk_v2`` / ``environment_risk``：
        铁塔不参与人口×遮蔽（population×shelter）、Risk Framework V2 或
        RouteRiskProfile 的**数学**，把它标 stale 属于无意义重算。
        """

        state = self.session.state
        statuses = state.setdefault("result_statuses", {})
        if include_derived:
            for name in ("tower_obstacle_profiles", "tower_colocation_candidates"):
                if statuses.get(name) not in (None, "not_calculated"):
                    statuses[name] = "stale"
        self.planning_constraint_field(str(reason))
        self.layered_route(str(reason))
        self.cns_corridor_site_plan()
        # 雷达初步划设的雷达原点依赖塔顶 EGM2008 正高（= 障碍物派生事实）。
        self.radar_surveillance_layout(str(reason))
        mark_active_report_stale(state, str(reason))

    def route_operating_layer(self, reason="route_operating_layer_changed"):
        """Minimal Layered Operational Route Architecture V1 invalidation chain.

        Altitude layer / route-layer assignment / procedure changes stale only the vertical,
        terrain-building and downstream CNS-safety derived results.  Grid risk is never
        rewritten, and horizontal routes are not invalidated: the Layered Risk-Aware Route
        Planner V1 (not the legacy planners) is the component that makes a layer selection
        affect the horizontal route planning fingerprint — it stales only its own
        candidate/mask products.

        The additive Production Route3DProfile V1 reads exactly this configuration, so it is
        staled in the same pass (its stored record is preserved as audit evidence, and a stale
        profile deliberately never falls back to the constant cruise altitude).
        """

        mark_active_report_stale(self.session.state, reason)
        self.route_3d_profile(reason, propagate=False)
        self.coverage_3d()
        self.building_clearance(reason)
        self.planning_constraint_field(reason)
        self.layered_route(reason)
        # 固定巡航高度层（ALT-080）是雷达初步划设的显式输入之一。
        self.radar_surveillance_layout(str(reason))

    def radar_surveillance_layout(self, reason="radar_surveillance_input_changed"):
        """Stale only the additive Radar Surveillance Layout V1 proposal.

        该产物是 **proposal-only** 且严格下游：它消费已发布运行航路、真实铁塔站址与雷达
        原点高度、FABDEM 地形、显式陆域掩膜和自身策略。它被这些输入的变化 stale，但它
        本身**绝不** stale 或改写 ``operational_routes`` / ``coverage_3d`` /
        ``cns_service_capability`` / 任何走廊或站址提案 / 设备目录。
        """

        invalidator = self.radar_surveillance_layout_invalidator
        if callable(invalidator):
            return invalidator(str(reason))
        return {"stale_route_ids": []}

    def route_3d_profile(self, reason="route_3d_profile_input_changed", *, propagate=True):
        """Stale only the additive Production Route3DProfile V1 records.

        Strictly downstream: it never marks a Theta* candidate, a ``LayeredRouteValidation``
        or a ``LayeredOperationalAdoption`` stale, and it never deletes a stored profile.
        ``propagate`` additionally stales the direct consumers (``coverage_3d`` and
        ``route_vertical_profiles``), which is required when the *profile set itself* changed.
        """

        invalidator = self.route_3d_profile_invalidator
        result = {"stale_profile_ids": []}
        if callable(invalidator):
            result = invalidator(str(reason))
        self.vertical_transition_validation(
            f"route_3d_profile_changed:{reason}"
        )
        if propagate:
            self.coverage_3d()
            self.route_vertical_profiles(str(reason))
        return result

    def vertical_transition_validation(self, reason="vertical_transition_input_changed"):
        """Stale only the additive Vertical Transition Continuous Validation V1 records.

        The transition validation depends on the profile, the operational route/adoption, the
        existing cruise ``LayeredRouteValidation``, the terrain/building source audits, the
        explicit metric CRS and its own validator version.  A change to any of those makes a
        stored record stale.  This is the *end* of that chain: it never triggers anything
        upstream, and a stale transition record itself is only propagated to the Safety
        Evidence V2 / report by :meth:`vertical_transition_validation_changed`.
        """

        invalidator = self.vertical_transition_validation_invalidator
        if callable(invalidator):
            return invalidator(str(reason))
        return {"stale_validation_ids": []}

    def vertical_transition_validation_changed(
        self, reason="vertical_transition_validation_changed",
    ):
        """Propagate an explicit transition evaluate to its declared downstream consumers.

        The transition verdict feeds the Route Safety Evidence V2 geometry domain and the
        report.  Nothing else.  It never stales a route, candidate, profile, validation,
        adoption, RouteRiskProfile or CNS result.
        """

        mark_active_report_stale(self.session.state, reason)
        self.route_safety_evidence(str(reason))
        return {"reason": str(reason), "downstream": ["route_safety_evidence_v2", "report"]}

    def route_3d_profile_changed(self, reason="route_3d_profile_changed"):
        """Propagate an explicit Route3DProfile evaluate/delete to its direct consumers.

        The profile is the authoritative vertical context of a route, so ``coverage_3d`` (which
        consumes it through ``effective_route_vertical_context``) and ``route_vertical_profiles``
        become stale.  The propagation is strictly downstream: no route, candidate, validation,
        adoption or CNS-upstream result is ever invalidated here.
        """

        mark_active_report_stale(self.session.state, reason)
        self.coverage_3d()
        self.route_vertical_profiles(reason)
        self.vertical_transition_validation(f"route_3d_profile_changed:{reason}")
        return {"reason": str(reason), "downstream": list(DOWNSTREAM_RESULTS)}

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
        """Stale only the additive layered route planner candidate/mask products.

        A Risk Framework V2 change, a selected layer/request change, a terrain/building or
        building-clearance policy change, and a feasibility/cost policy change only make the
        related ``LayeredRouteCandidate`` / ``LayerFeasibilityMask`` stale.  Legacy routes,
        V3 experiments/adoptions and every CNS result are deliberately untouched.

        The additive ``RouteRiskProfile`` consumes a current candidate, so it is staled in the
        same pass — and only it: ``operational_routes`` / CNS are still never touched.
        """

        invalidator = self.layered_route_invalidator
        if callable(invalidator):
            invalidator(str(reason))
        self.route_risk_profile(reason)
        self.layered_route_validation(reason)

    def planning_constraint_field(self, reason="planning_constraint_field_input_changed"):
        """Stale the new derived field, then propagate through its route consumers."""

        state = self.session.state
        collection = state.get("planning_constraint_fields")
        changed = []
        if isinstance(collection, dict):
            for item in collection.get("items") or []:
                if not isinstance(item, dict) or item.get("status") == "stale":
                    continue
                item["status"] = "stale"
                item["stale_reason"] = str(reason)
                changed.append(item.get("field_id"))
            if changed:
                collection["status"] = "stale"
                state.setdefault("result_statuses", {})["planning_constraint_fields"] = "stale"
        if changed:
            self.layered_route(f"planning_constraint_field_changed:{reason}")
        return {"stale_constraint_field_ids": changed}

    def route_risk_profile(self, reason="route_risk_profile_input_changed"):
        """Stale only the additive RouteRiskProfile product.

        A candidate change, a ``grid_risk_v2`` change and a RouteRiskProfilePolicy change only
        make the dependent ``route_risk_profiles`` stale.  Legacy routes, V3
        experiments/adoptions, ``operational_routes`` and every CNS result stay untouched, and
        a stale profile is preserved as audit evidence instead of being deleted.
        """

        invalidator = self.route_risk_profile_invalidator
        if callable(invalidator):
            invalidator(str(reason))
        self.route_safety_evidence(reason)

    def layered_route_validation(self, reason="layered_route_validation_input_changed"):
        """Stale only production LayeredRouteValidation and its owned adoption chain."""

        invalidator = self.layered_route_validation_invalidator
        result = {"stale_validation_ids": []}
        if callable(invalidator):
            result = invalidator(str(reason))
        # The transition validation binds the current cruise validation fingerprint, so a
        # cruise-validation change makes it stale too (still strictly downstream).
        self.vertical_transition_validation(f"cruise_validation_changed:{reason}")
        self.route_safety_evidence(reason)
        return result

    def route_safety_evidence(self, reason="route_safety_evidence_input_changed"):
        """Stale only the additive Route Safety Evidence V2 artifact.

        This is the single downstream sink: no route, candidate, validation, adoption or CNS
        result is ever invalidated because a Safety Evidence V2 assessment went stale.
        """

        invalidator = self.route_safety_evidence_invalidator
        if callable(invalidator):
            return invalidator(str(reason))
        return {"stale_assessment_ids": []}

    def operational_route_published(self, route_ids, *, reason, preserve_published_routes=True):
        """Dedicated publication propagation: stale downstream, never candidate/V3.

        ``workflow('route')`` is intentionally not used because it would immediately stale
        the route just published.  Evidence-outdated propagation may pass
        ``preserve_published_routes=False`` after the owner has explicitly marked its route
        stale; this method will then leave that status untouched.
        """

        state = self.session.state
        route_ids = {str(item) for item in route_ids or []}
        self.coverage_3d()
        self.building_clearance(str(reason))
        self.cns_corridor()
        mark_active_report_stale(state, str(reason))
        self.route_safety_evidence(str(reason))
        # B2B-1 补漏：正式运行航路发布/撤销后，P17 recommendation 与雷达初步划设同样
        # 消费该航路，必须一并失效。旧 Version V3-D 的 `_propagate_publish` 完全绕过
        # 本方法，因此这两条边只能在唯一发布路径上补齐。
        self.requirement_recommendation(str(reason))
        self.radar_surveillance_layout(str(reason))
        if preserve_published_routes:
            for route in state.get("operational_routes") or []:
                if str(route.get("route_id")) in route_ids:
                    route["status"] = "passed"
                    route.pop("stale_reason", None)
        return {
            "reason": str(reason), "route_ids": sorted(route_ids),
            "candidate_and_v3_untouched": True,
            "downstream": [
                "coverage_3d", "building_clearance",
                "cns_corridor_assessment", "route_safety_evidence_v2",
                "required_cns_recommendation", "radar_surveillance_layout", "report",
            ],
        }

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
        self.route_safety_evidence("coverage_3d_changed")

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
        self.route_safety_evidence("cns_service_capability_changed")

    def service_timeline(self):
        state = self.session.state
        result = state.get("service_timeline") or {}
        if result.get("status") != "not_calculated":
            result["status"] = "stale"
            state["service_timeline"] = result
            state.setdefault("result_statuses", {})["service_timeline"] = "stale"
        mark_active_report_stale(state, "service_timeline_changed")
        self.encounter_3d("service_timeline_changed")

    def protection_envelope(self):
        state = self.session.state
        result = state.get("protection_envelope") or {}
        if result.get("status") != "not_calculated":
            result["status"] = "stale"
            state["protection_envelope"] = result
            state.setdefault("result_statuses", {})["protection_envelope"] = "stale"
        mark_active_report_stale(state, "protection_envelope_changed")
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

    def closed_loop_assessment(self):
        """Invalidate only the P12 verification product."""
        state = self.session.state
        result = state.get("closed_loop_assessment") or {}
        if result.get("status") != "not_calculated":
            result["status"] = "stale"
            result["commit_status"] = "stale_assessment"
            state["closed_loop_assessment"] = result
            state.setdefault("result_statuses", {})["closed_loop_assessment"] = "stale"
        mark_runtime_compatibility_stale(
            self.session, "closed_loop_assessment", "closed_loop_input_changed"
        )

    def cns_corridor(self):
        """Stale only the P14 corridor result; never mutate centerline products."""
        state = self.session.state
        result = state.get("cns_corridor_assessment") or {}
        if result.get("status") != "not_calculated":
            result["status"] = "stale"
            state["cns_corridor_assessment"] = result
            state.setdefault("result_statuses", {})["cns_corridor_assessment"] = "stale"
        self.cns_corridor_gap()
        self.route_safety_evidence("cns_corridor_changed")

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
