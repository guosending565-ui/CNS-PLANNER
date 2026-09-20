"""Route Safety Evidence Assessment V2 — application service.

The service is a **read-only aggregation boundary**.  It walks the published layered
operational lineage

    OperationalRoute -> LayeredOperationalAdoption -> LayeredRouteValidation
      -> LayeredRouteCandidate -> RouteRiskProfile

and transcribes the existing evidence of four domains.  It never replans, never re-adopts,
never recomputes the terrain/building validator, the Risk Framework V2 domains, the CNS
coverage / capability / gap models or the RouteRiskProfile maths, and it never writes any
upstream container.

The only live evaluation it performs is a *replay of the existing pure regulatory geometry
function* (:func:`evaluate_regulatory_intersection`) against the currently published route
path and the currently configured ``regulatory_constraints``.  That is required to answer
"is there a confirmed regulatory violation **now**": the candidate's recorded regulatory
record is a snapshot of the dataset state at planning time, so it can never detect that a
constraint dataset was configured afterwards.  The replay adds no new algorithm.
"""

from __future__ import annotations

from copy import deepcopy

from ..domain.layered_route_validation import stable_fingerprint, utc_now
from ..domain.regulatory_constraints import (
    default_regulatory_constraints, evaluate_regulatory_intersection,
    is_configured as regulatory_is_configured, regulatory_constraints_fingerprint,
)
from ..domain.route_safety_evidence_v2 import (
    ALGORITHM_ID, ALGORITHM_VERSION, CNS_GAP_SEMANTICS, CNS_SUBSYSTEMS, DOMAIN_IDS,
    DOMAIN_LABELS, assessment_fingerprint, empty_domain,
    empty_route_safety_evidence_v2, normalize_route_safety_evidence_v2_collection,
    overall_status_for,
)

#: The existing validator verdict -> geometry evidence status.
_GEOMETRY_STATUS = {
    "validated_candidate": "validated",
    "failed": "failed",
    "unresolved": "unresolved",
    "validation_incomplete": "validation_incomplete",
    "not_ready": "not_ready",
    "stale": "stale",
}

_UPSTREAM_RESULT_IDS = (
    "coverage_3d", "cns_service_capability", "cns_gap_analysis_v2", "cns_corridor_assessment",
)


class RouteSafetyEvidenceService:
    """Aggregate the four evidence domains of a published layered operational adoption."""

    def __init__(self, session, invalidation, snapshot, layered_service, validation_service,
                 adoption_service, risk_profile_service):
        self.session = session
        self.invalidation = invalidation
        self.snapshot = snapshot
        self.layered = layered_service
        self.validations = validation_service
        self.adoptions = adoption_service
        self.risk_profiles = risk_profile_service
        self.ensure_state()

    # ------------------------------------------------------------------ state

    def ensure_state(self):
        state = self.session.state
        state["route_safety_evidence_v2"] = normalize_route_safety_evidence_v2_collection(
            state.get("route_safety_evidence_v2")
        )
        state.setdefault("result_statuses", {}).setdefault(
            "route_safety_evidence_v2", "not_calculated"
        )
        return state

    # ------------------------------------------------------------------ snapshots

    def readiness_snapshot(self, payload=None):
        lineage = self._resolve_lineage(payload)
        blockers = list(lineage["blockers"])
        domains = {}
        for domain_id in DOMAIN_IDS:
            ready = not lineage["missing_links"] and not lineage["stale_links"]
            domains[domain_id] = {
                "domain_id": domain_id,
                "label": DOMAIN_LABELS[domain_id],
                "ready": ready,
                "status": None if ready else "not_ready",
                "blocked_by": [] if ready else (
                    lineage["missing_links"] + lineage["stale_links"]
                ),
            }
        return {
            "status": "ready" if not blockers else "not_ready",
            "algorithm": {"algorithm_id": ALGORITHM_ID, "algorithm_version": ALGORITHM_VERSION},
            "target": {
                "scope": "current_published_layered_operational_adoption",
                "adoption_id": (lineage.get("adoption") or {}).get("adoption_id"),
                "route_id": (lineage.get("operational_route") or {}).get("route_id"),
                "validation_id": (lineage.get("validation") or {}).get("validation_id"),
                "candidate_id": (lineage.get("candidate") or {}).get("candidate_id"),
                "route_risk_profile_id": (lineage.get("route_risk_profile") or {}).get("profile_id"),
            },
            "lineage": {
                "complete": lineage["complete"],
                "missing_links": list(lineage["missing_links"]),
                "stale_links": list(lineage["stale_links"]),
            },
            "domains": domains,
            "blockers": blockers,
            "upstream_results": self._upstream_result_states(),
            "regulatory_dataset": {
                "status": (self.session.state.get("regulatory_constraints") or {}).get("status"),
                "configured": regulatory_is_configured(
                    self.session.state.get("regulatory_constraints")
                ),
                "fingerprint": regulatory_constraints_fingerprint(
                    self.session.state.get("regulatory_constraints")
                ),
                "not_configured_is_not_passed": True,
            },
            "boundaries": {
                "post_planning_evidence_aggregation": True,
                "no_replanning": True,
                "no_auto_adoption": True,
                "no_overall_safety_score": True,
                "no_sora_automatic_grading": True,
                "cns_gap_is_operational_support_deficit_not_route_unsafe": True,
                "upstream_results_are_never_modified": True,
            },
            "explicit_evaluation_required": True,
        }

    def result_snapshot(self, payload=None):
        """Read-only projection: recompute the *applicability* of every stored assessment."""

        state = self.ensure_state()
        collection = normalize_route_safety_evidence_v2_collection(
            state.get("route_safety_evidence_v2")
        )
        projected = []
        cached_lineage = None
        cached_adoption_id = object()
        for item in collection["items"]:
            entry = deepcopy(item)
            if entry.get("status") == "stale":
                entry["current_applicability"] = "stale"
                projected.append(entry)
                continue
            if entry.get("status") == "not_ready":
                entry["current_applicability"] = "not_ready"
                projected.append(entry)
                continue
            if entry.get("adoption_id") != cached_adoption_id:
                cached_adoption_id = entry.get("adoption_id")
                cached_lineage = self._resolve_lineage({"adoption_id": cached_adoption_id})
            expected = self._expected_fingerprint(entry, cached_lineage)
            stored = (entry.get("fingerprints") or {}).get("assessment_fingerprint")
            if expected is not None and expected != stored:
                entry["current_applicability"] = "stale_inputs_changed"
            else:
                entry["current_applicability"] = "current"
            projected.append(entry)
        collection["items"] = projected
        return collection

    # ------------------------------------------------------------------ evaluate

    def evaluate(self, payload=None, *, save=True):
        payload = payload if isinstance(payload, dict) else {}
        state = self.ensure_state()
        lineage = self._resolve_lineage(payload)
        if lineage["adoption"] is None:
            return self._store(
                self._not_ready_record(lineage, "no_published_layered_operational_adoption"),
                save=save,
            )
        domains = {
            "geometry_obstacle": self._geometry_domain(lineage),
            "ground_exposure": self._ground_domain(lineage),
            "regulatory": self._regulatory_domain(lineage),
            "cns_operational_support": self._cns_domain(lineage),
        }
        if lineage["missing_links"] or lineage["stale_links"]:
            # An incomplete or stale lineage is never dressed up as a current assessment.  The
            # per-domain statuses still report what was actually evaluated; only the overall
            # verdict is forced to ``stale`` so it cannot masquerade as current evidence.
            status = "stale"
        else:
            statuses = {domain_id: domains[domain_id]["status"] for domain_id in DOMAIN_IDS}
            status = overall_status_for(statuses)
        fingerprint_components = self._references(lineage)
        record = empty_route_safety_evidence_v2(status)
        route_id = (lineage.get("operational_route") or {}).get("route_id")
        adoption_id = (lineage.get("adoption") or {}).get("adoption_id")
        identity = assessment_fingerprint(fingerprint_components)
        record.update({
            "assessment_id": "RSE-" + identity[-12:].upper(),
            "route_id": route_id,
            "adoption_id": adoption_id,
            "status": status,
            "status_reason": (
                "lineage_incomplete_or_stale" if lineage["missing_links"] or lineage["stale_links"]
                else None
            ),
            "current_applicability": (
                "stale" if lineage["missing_links"] or lineage["stale_links"] else "current"
            ),
            "created_at": utc_now(),
            "lineage": {
                "status": "resolved" if lineage["complete"] else "incomplete",
                "operational_route": route_id,
                "adoption_id": adoption_id,
                "validation_id": (lineage.get("validation") or {}).get("validation_id"),
                "candidate_id": (lineage.get("candidate") or {}).get("candidate_id"),
                "route_risk_profile_id": (
                    lineage.get("route_risk_profile") or {}
                ).get("profile_id"),
                "altitude_layer_id": (
                    (lineage.get("candidate") or {}).get("altitude_layer_id")
                ),
                "missing_links": list(lineage["missing_links"]),
                "stale_links": list(lineage["stale_links"]),
                "complete": lineage["complete"],
                "semantics": {
                    "lineage_must_be_complete_to_be_current": True,
                    "incomplete_lineage_is_never_reported_as_current": True,
                },
            },
            "domains": domains,
            "fingerprints": {
                **record["fingerprints"],
                **fingerprint_components,
                "assessment_fingerprint": identity,
                "components": self._audit_components(
                    lineage, domains, fingerprint_components,
                ),
            },
            "limitations": self._limitations(domains, lineage),
        })
        record["evidence_summary"] = self._evidence_summary(status, domains)
        record["provenance"] = {
            **record["provenance"],
            "lineage_links": {
                "operational_route": route_id,
                "adoption_id": adoption_id,
                "validation_id": (lineage.get("validation") or {}).get("validation_id"),
                "candidate_id": (lineage.get("candidate") or {}).get("candidate_id"),
                "route_risk_profile_id": (
                    lineage.get("route_risk_profile") or {}
                ).get("profile_id"),
            },
            "regulatory_replay": {
                "function": "domain.regulatory_constraints.evaluate_regulatory_intersection",
                "reason": (
                    "candidate 记录的是规划时的 regulatory 数据集状态，无法发现之后才配置的"
                    "约束；必须对当前已发布 route path 重放同一纯函数"
                ),
                "new_algorithm_introduced": False,
            },
        }
        return self._store(record, save=save)

    # ------------------------------------------------------------------ invalidation

    def stale_for_reason(self, reason="route_safety_evidence_input_changed"):
        """Stale **only** Route Safety Evidence V2; upstream results are never touched."""

        state = self.ensure_state()
        collection = normalize_route_safety_evidence_v2_collection(
            state.get("route_safety_evidence_v2")
        )
        changed = []
        for item in collection["items"]:
            if item.get("status") == "stale":
                continue
            item["status"] = "stale"
            item["current_applicability"] = "stale"
            item["stale_reason"] = str(reason)
            item["evidence_summary"] = {
                **(item.get("evidence_summary") or {}),
                "status": "stale",
            }
            changed.append(str(item.get("assessment_id")))
        if changed:
            collection["status"] = "stale"
            state["route_safety_evidence_v2"] = collection
            state.setdefault("result_statuses", {})["route_safety_evidence_v2"] = "stale"
        return {"stale_assessment_ids": changed}

    # ------------------------------------------------------------------ domains

    def _geometry_domain(self, lineage):
        domain = empty_domain("geometry_obstacle")
        validation = lineage.get("validation") or {}
        candidate = lineage.get("candidate") or {}
        record = validation
        status = _GEOMETRY_STATUS.get(str(record.get("status")), "not_ready")
        if not record:
            status = "not_ready"
        domain["status"] = status
        domain["status_reason"] = record.get("status_reason")
        domains = record.get("domains") or {}
        terrain = domains.get("terrain") or {}
        building = domains.get("building") or {}
        failed_intervals = record.get("failed_intervals") or []
        unresolved_intervals = record.get("unresolved_intervals") or []
        margins = record.get("minimum_margins") or {}
        resource = record.get("resource_limits") or {}
        # Only an *explicit clearance violation from the existing validator* is a geometry
        # failure.  An unresolved / incomplete / missing evidence never becomes a failure and
        # never becomes a pass either.
        hard_failure = bool(status == "failed" and (
            failed_intervals
            or str(terrain.get("status")) == "failed"
            or str(building.get("status")) == "failed"
            or terrain.get("violations") or building.get("violations")
        ))
        domain["hard_constraint_failure"] = hard_failure
        domain["evidence"] = {
            "validation_id": record.get("validation_id"),
            "validation_status": record.get("status"),
            "current_applicability": validation.get("current_applicability"),
            "source_type": record.get("source_type"),
            "source_audits": deepcopy(record.get("source_audits") or {}),
            "validator_versions": deepcopy(record.get("validator_versions") or {}),
            "policies": deepcopy(record.get("policies") or {}),
            "route_semantics": (record.get("route") or {}).get("semantics"),
            "provenance": deepcopy(record.get("provenance") or {}),
            "semantics": deepcopy(record.get("semantics") or {}),
        }
        domain["metrics"] = {
            "terrain_status": terrain.get("status"),
            "building_status": building.get("status"),
            "terrain_minimum_margin_m": (
                terrain.get("minimum_margin")
                if terrain.get("minimum_margin") is not None
                else margins.get("terrain_vertical_m")
            ),
            "building_minimum_margin_m": (
                building.get("minimum_margin")
                if building.get("minimum_margin") is not None
                else margins.get("building_vertical_m")
            ),
            "fixed_cruise_altitude_m": (record.get("route") or {}).get("nominal_altitude_m"),
            "vertical_reference": (record.get("route") or {}).get("vertical_reference"),
            "altitude_layer_id": (record.get("candidate") or {}).get("altitude_layer_id")
            or candidate.get("altitude_layer_id"),
            "failed_interval_count": len(failed_intervals),
            "unresolved_interval_count": len(unresolved_intervals),
            "critical_evidence": deepcopy(record.get("critical_evidence") or []),
            "resource_limited": bool(resource.get("limit_reached")),
            "resource_limits": deepcopy(resource),
            "validation_fingerprint": (record.get("fingerprints") or {}).get(
                "validation_fingerprint"
            ),
        }
        domain["sources"] = _source_rows(record.get("source_audits") or {})
        if status == "validated":
            domain["limitations"].append(
                "validated_candidate 只表示现有 terrain/building validator 在该固定巡航高度"
                "未发现明确净空违规；它不是 safe route，也不评估转弯、爬升、气象或运行失效。"
            )
        elif status == "failed":
            domain["limitations"].append(
                "只有已有 validator 的明确 clearance violation 才是 geometry failure；"
                "该结论不推广为整条航路的安全结论。"
            )
        elif status in ("unresolved", "not_ready", "validation_incomplete"):
            domain["limitations"].append(
                "证据未完整解析：unresolved/incomplete 既不是 failure，也不是 pass。"
            )
        domain["semantics"] = {
            "consumes_existing_layered_route_validation": True,
            "does_not_reimplement_terrain_or_building_validator": True,
            "only_explicit_clearance_violation_is_a_failure": True,
            "validated_route_is_not_a_safe_route": True,
            "unknown_is_not_safe": True,
            "resource_limit_is_computational_not_safety": True,
        }
        return domain

    def _ground_domain(self, lineage):
        domain = empty_domain("ground_exposure")
        candidate = lineage.get("candidate") or {}
        profile = lineage.get("route_risk_profile")
        if not candidate:
            domain["status"] = "not_calculated"
            domain["status_reason"] = "candidate_unavailable"
            return domain
        objective = candidate.get("planning_objective") or {}
        search_parameters = candidate.get("search_parameters") or {}
        risk_density = candidate.get("route_risk_density") or {}
        shelter_policy = self._shelter_policy(candidate)
        profile_domain = {}
        profile_policy = {}
        profile_fingerprint = None
        profile_status = "not_calculated"
        if isinstance(profile, dict):
            profile_status = str(profile.get("status") or "not_calculated")
            profile_fingerprint = (profile.get("fingerprints") or {}).get("profile_fingerprint")
            profile_domain = deepcopy((profile.get("domains") or {}).get("ground") or {})
            policy = profile.get("policy") or {}
            profile_policy = deepcopy((policy.get("domains") or {}).get("ground") or {})
        unresolved_length = profile_domain.get("unresolved_length_m")
        if profile_status != "passed":
            status = "stale" if profile_status == "stale" else "not_calculated"
        elif not profile_domain:
            status = "not_calculated"
        elif unresolved_length not in (None, 0, 0.0) or profile_domain.get("status") in (
            "unresolved", "missing_data",
        ):
            status = "unresolved"
        else:
            status = "assessed"
        domain["status"] = status
        domain["status_reason"] = (
            None if status == "assessed" else f"route_risk_profile_ground_{profile_status}"
        )
        # The Theta* planning objective and the post-hoc RouteRiskProfile domains are kept in
        # two separate blocks on purpose: merging them into one number would invent an
        # overall risk score that no model in this project defines.
        domain["evidence"] = {
            "theta_star_planning_objective": {
                "scope": "theta_star_search_objective_population_x_shelter_only",
                "risk_exposure_index_m": objective.get("risk_exposure_index_m"),
                "weighted_risk": objective.get("weighted_risk"),
                "turn_cost_m": objective.get("turn_cost_m"),
                "distance_m": objective.get("distance_m"),
                "total_cost": objective.get("total_cost"),
                "weights": {
                    "risk": objective.get("risk_weight"),
                    "turn": objective.get("turn_weight"),
                    "distance": objective.get("distance_weight"),
                },
                "weights_provenance": objective.get("weights_provenance"),
                "used_in_search": True,
                "post_hoc": False,
            },
            "route_risk_density": {
                "scope": "candidate_evaluation_constraint_not_an_objective_term",
                "value": risk_density.get("value"),
                "threshold": risk_density.get("threshold"),
                "margin": risk_density.get("margin"),
                "status": risk_density.get("status"),
                "source": risk_density.get("source"),
                "temporary_constraint": bool(risk_density.get("temporary_constraint")),
                "objective_term": False,
            },
            "shelter_policy": shelter_policy,
            "theta_search_parameters": search_parameters,
        }
        domain["metrics"] = {
            "population_shelter_risk_exposure_index_m": objective.get("risk_exposure_index_m"),
            "route_risk_density": risk_density.get("value"),
            "route_risk_density_status": risk_density.get("status"),
            "route_risk_density_threshold": risk_density.get("threshold"),
            "profile_ground_exposure_index_m": profile_domain.get("exposure_index_m"),
            "profile_ground_mean_index": profile_domain.get("mean_index"),
            "profile_ground_max_index": profile_domain.get("max_index"),
            "profile_ground_max_location": deepcopy(profile_domain.get("max_location")),
            "profile_ground_resolved_length_m": profile_domain.get("resolved_length_m"),
            "profile_ground_unresolved_length_m": profile_domain.get("unresolved_length_m"),
            "profile_ground_coverage": profile_domain.get("coverage"),
            "profile_ground_high_risk": deepcopy(profile_domain.get("high_risk") or {}),
            "profile_ground_classification": deepcopy(
                profile_domain.get("classification") or {}
            ),
            "profile_thresholds": {
                "medium_min": profile_policy.get("medium_min"),
                "high_min": profile_policy.get("high_min"),
                "status": profile_policy.get("status"),
                "source": profile_policy.get("source"),
                "provenance": profile_policy.get("provenance"),
            },
            "profile_status": profile_status,
            "profile_fingerprint": profile_fingerprint,
            "combined_overall_risk_score": None,
            "combined_overall_not_computed": True,
        }
        domain["sources"] = [
            {
                "role": "theta_star_planning_objective",
                "algorithm_id": candidate.get("algorithm_id"),
                "algorithm_version": candidate.get("algorithm_version"),
                "candidate_fingerprint": candidate.get("candidate_fingerprint"),
            },
            {
                "role": "route_risk_profile_ground_domain",
                "algorithm_id": (profile or {}).get("algorithm") or "route_risk_profile_v1",
                "profile_id": (profile or {}).get("profile_id"),
                "profile_fingerprint": profile_fingerprint,
            },
            {"role": "shelter_coefficient_policy", **deepcopy(shelter_policy)},
        ]
        domain["limitations"] = [
            "Theta* population×shelter objective 与 RouteRiskProfile 的 post-hoc ground domain"
            " 是两条独立证据：本域绝不把它们合成为一个 overall risk/safety score。",
            "relative engineering index 不是事故概率、SORA GRC/ARC 或绝对安全风险。",
            "profile 阈值未确认时 classification/high-risk 保持 not_configured / null，"
            "绝不猜 0.6 / 0.8 之类的阈值。",
        ]
        domain["semantics"] = {
            "theta_objective_and_post_hoc_domains_kept_separate": True,
            "no_combined_overall_risk_score": True,
            "missing_is_not_zero": True,
            "low_risk_is_not_safe": True,
            "relative_engineering_index_not_accident_probability": True,
            "thresholds_have_no_default": True,
        }
        return domain

    def _regulatory_domain(self, lineage):
        domain = empty_domain("regulatory")
        constraints = self.session.state.get("regulatory_constraints") or (
            default_regulatory_constraints()
        )
        configured = regulatory_is_configured(constraints)
        candidate = lineage.get("candidate") or {}
        record = ((candidate.get("provenance") or {}).get("regulatory_compliance") or {})
        route = lineage.get("operational_route") or {}
        validation = lineage.get("validation") or {}
        path = [
            [float(point[0]), float(point[1])]
            for point in route.get("path") or []
            if isinstance(point, (list, tuple)) and len(point) == 2
        ]
        altitude = (validation.get("route") or {}).get("nominal_altitude_m")
        blocked, unresolved, skipped = [], [], []
        evaluated = False
        if configured and len(path) >= 2 and isinstance(altitude, (int, float)):
            evaluated = True
            for index, (start, end) in enumerate(zip(path, path[1:])):
                segment = evaluate_regulatory_intersection(
                    constraints, start=start, end=end, altitude_egm2008_m=float(altitude),
                )
                for constraint_id in segment["blocked_by"]:
                    blocked.append({"constraint_id": constraint_id, "segment_index": index})
                for item in segment["unresolved"]:
                    unresolved.append({**deepcopy(item), "segment_index": index})
                skipped.extend(segment["skipped_not_intersecting"])
                if not segment["evaluated"]:
                    evaluated = False
        if not configured:
            status = "not_configured"
        elif not evaluated:
            status = "not_evaluated"
        elif blocked:
            status = "blocked_by_confirmed_constraint"
        elif unresolved:
            status = "unresolved"
        else:
            status = "evaluated_no_confirmed_intersection"
        domain["status"] = status
        domain["status_reason"] = (
            "no_confirmed_regulatory_constraint_dataset_configured"
            if status == "not_configured" else None
        )
        domain["hard_constraint_failure"] = status == "blocked_by_confirmed_constraint"
        domain["evidence"] = {
            "dataset_status": constraints.get("status"),
            "dataset_source": constraints.get("source"),
            "dataset_fingerprint": constraints.get("dataset_fingerprint"),
            "constraint_count": int(constraints.get("count") or 0),
            "confirmed_constraint_count": sum(
                1 for entry in constraints.get("items") or []
                if isinstance(entry, dict) and entry.get("confirmed")
                and str(entry.get("status")) == "confirmed"
            ),
            "candidate_regulatory_record": deepcopy(record),
            "display_only_airspace_used": False,
            "semantics": deepcopy(constraints.get("semantics") or {}),
        }
        domain["metrics"] = {
            "configured": configured,
            "evaluated": evaluated,
            "segment_count": max(0, len(path) - 1),
            "blocked_constraints": blocked,
            "unresolved_constraints": unresolved,
            "not_intersecting_constraint_ids": sorted(set(skipped)),
            "cruise_altitude_m": altitude,
            "constraint_dataset_fingerprint": constraints.get("dataset_fingerprint"),
        }
        domain["sources"] = [{
            "role": "regulatory_constraints",
            "status": constraints.get("status"),
            "source": constraints.get("source"),
            "fingerprint": constraints.get("dataset_fingerprint"),
            "reuses_display_only_airspace": False,
        }]
        if status == "not_configured":
            domain["limitations"].append(
                "未配置任何 regulatory constraint 数据集：status=not_configured，"
                "绝不等于 passed/合规。"
            )
        elif status == "not_evaluated":
            domain["limitations"].append(
                "已配置数据集但无法对当前已发布 route 评估（缺 path 或巡航高度）："
                "not_evaluated 既不是 violation，也不是合规结论。"
            )
        elif status == "evaluated_no_confirmed_intersection":
            domain["limitations"].append(
                "“未与已确认约束水平相交”不是法规符合性结论，也不代表 future 数据集变化。"
            )
        domain["semantics"] = {
            "consumes_formal_regulatory_constraints_only": True,
            "display_only_airspace_is_never_reused": True,
            "not_configured_is_not_passed": True,
            "not_evaluated_is_not_passed": True,
            "unresolved_evidence_is_never_safe": True,
            "no_confirmed_intersection_is_not_regulatory_compliance": True,
            "confirmed_intersection_is_a_hard_constraint_failure": True,
        }
        return domain

    def _cns_domain(self, lineage):
        domain = empty_domain("cns_operational_support")
        state = self.session.state
        route_id = str((lineage.get("operational_route") or {}).get("route_id") or "")
        upstream = self._upstream_result_states()
        coverage = state.get("coverage_3d") or {}
        capability = state.get("cns_service_capability") or {}
        gap = state.get("cns_gap_analysis_v2") or {}
        corridor = state.get("cns_corridor_assessment") or {}
        coverage_route = _route_of(coverage, route_id)
        capability_route = _route_of(capability, route_id)
        gap_route = _route_of(gap, route_id)
        corridor_route = _route_of(corridor, route_id)
        consumed_corridor = bool(
            corridor_route
            and str(corridor.get("status")) not in ("not_calculated", "stale")
            and corridor_route.get("status") not in ("missing_data", None)
        )
        if any(
            str((state.get(name) or {}).get("status")) == "stale"
            for name in _UPSTREAM_RESULT_IDS
        ):
            status = "stale"
        elif not any((coverage_route, capability_route, gap_route)):
            status = "not_run"
        else:
            gap_subsystems = _subsystem_index(gap_route)
            if any(
                str(item.get("status")) == "confirmed_gap"
                for item in gap_subsystems.values()
            ):
                status = "operational_support_deficit"
            elif any(
                str(item.get("status")) in ("unknown", "missing_data")
                for item in gap_subsystems.values()
            ) or any(
                str(item.get("status")) in ("unknown", "unsupported_model")
                for item in _subsystem_index(capability_route).values()
            ):
                status = "unknown"
            else:
                status = "supported"
        domain["status"] = status
        domain["status_reason"] = (
            None if status == "supported"
            else "cns_gap_v2_status:" + str(gap.get("status"))
        )
        subsystems = []
        for code in CNS_SUBSYSTEMS:
            coverage_subsystem = _subsystem_of(coverage_route, code) or {}
            capability_subsystem = _subsystem_of(capability_route, code) or {}
            gap_subsystem = _subsystem_of(gap_route, code) or {}
            corridor_subsystem = _subsystem_of(corridor_route, code) or {}
            subsystems.append({
                "subsystem": code,
                "coverage": {
                    "status": coverage_subsystem.get("status"),
                    "covered_length_m": coverage_subsystem.get("covered_length_m"),
                    "uncovered_length_m": coverage_subsystem.get("uncovered_length_m"),
                    "covered_fraction": coverage_subsystem.get("covered_fraction"),
                    "model_scope": coverage_subsystem.get("model_scope"),
                },
                "capability": {
                    "status": capability_subsystem.get("status"),
                    "meets_length_m": capability_subsystem.get("meets_length_m"),
                    "fail_length_m": capability_subsystem.get("fail_length_m"),
                    "unknown_length_m": capability_subsystem.get("unknown_length_m"),
                    "meets_fraction": capability_subsystem.get("meets_fraction"),
                    "model_scope": capability_subsystem.get("model_scope"),
                },
                "confirmed_gap": {
                    "status": gap_subsystem.get("status"),
                    "required": gap_subsystem.get("required"),
                    "gap_length_m": gap_subsystem.get("gap_length_m"),
                    "gap_fraction": gap_subsystem.get("gap_fraction"),
                    "unknown_length_m": gap_subsystem.get("unknown_length_m"),
                    "satisfied_length_m": gap_subsystem.get("satisfied_length_m"),
                    "continuous_deficit": {
                        "max_continuous_gap_length_m": gap_subsystem.get(
                            "max_continuous_gap_length_m"
                        ),
                        "max_continuous_gap_duration_s": gap_subsystem.get(
                            "max_continuous_gap_duration_s"
                        ),
                        "gap_segment_count": gap_subsystem.get("gap_segment_count"),
                    },
                    "planning_assessment_status": (
                        gap_subsystem.get("planning_assessment") or {}
                    ).get("status"),
                    "operational_assessment_status": (
                        gap_subsystem.get("operational_assessment") or {}
                    ).get("status"),
                },
                "corridor": {
                    "consumed": bool(consumed_corridor and corridor_subsystem),
                    "status": corridor_subsystem.get("status"),
                    "required_voxel_count": corridor_subsystem.get("required_voxel_count"),
                    "deficit_voxel_count": len(
                        corridor_subsystem.get("deficit_voxel_ids") or []
                    ),
                    "unknown_voxel_count": len(
                        corridor_subsystem.get("unknown_voxel_ids") or []
                    ),
                },
                "operational_support_verdict": _support_verdict(gap_subsystem),
            })
        domain["evidence"] = {
            "coverage_3d": {
                "status": coverage.get("status"),
                "algorithm_id": coverage.get("algorithm_id"),
                "algorithm_version": coverage.get("algorithm_version"),
                "model_scope": coverage.get("model_scope"),
                "input_fingerprint": coverage.get("input_fingerprint"),
                "route_present": bool(coverage_route),
            },
            "cns_service_capability": {
                "status": capability.get("status"),
                "algorithm_id": capability.get("algorithm_id"),
                "algorithm_version": capability.get("algorithm_version"),
                "model_scope": capability.get("model_scope"),
                "input_fingerprint": capability.get("input_fingerprint"),
                "route_present": bool(capability_route),
            },
            "cns_gap_analysis_v2": {
                "status": gap.get("status"),
                "algorithm_id": gap.get("algorithm_id"),
                "algorithm_version": gap.get("algorithm_version"),
                "model_scope": gap.get("model_scope"),
                "input_fingerprint": gap.get("input_fingerprint"),
                "semantics": gap.get("semantics"),
                "route_present": bool(gap_route),
            },
            "cns_corridor_assessment": {
                "consumed": consumed_corridor,
                "status": corridor.get("status"),
                "algorithm_id": corridor.get("algorithm_id"),
                "algorithm_version": corridor.get("algorithm_version"),
                "input_fingerprint": corridor.get("input_fingerprint"),
                "corridor_geometry_fingerprint": corridor.get("corridor_geometry_fingerprint"),
                "route_present": bool(corridor_route),
            },
            "upstream_results": upstream,
        }
        domain["metrics"] = {
            "subsystems": subsystems,
            "confirmed_gap_subsystems": [
                item["subsystem"] for item in subsystems
                if item["confirmed_gap"]["status"] == "confirmed_gap"
            ],
            "unknown_subsystems": [
                item["subsystem"] for item in subsystems
                if item["operational_support_verdict"] == "unknown"
            ],
            "operational_support_deficit": status == "operational_support_deficit",
        }
        domain["sources"] = [
            {
                "role": "coverage_3d",
                "algorithm_id": coverage.get("algorithm_id"),
                "fingerprint": coverage.get("input_fingerprint"),
                "status": coverage.get("status"),
            },
            {
                "role": "cns_service_capability",
                "algorithm_id": capability.get("algorithm_id"),
                "fingerprint": capability.get("input_fingerprint"),
                "status": capability.get("status"),
            },
            {
                "role": "cns_gap_analysis_v2",
                "algorithm_id": gap.get("algorithm_id"),
                "fingerprint": gap.get("input_fingerprint"),
                "status": gap.get("status"),
            },
        ]
        if consumed_corridor:
            domain["sources"].append({
                "role": "cns_corridor_assessment",
                "algorithm_id": corridor.get("algorithm_id"),
                "fingerprint": corridor.get("input_fingerprint"),
                "status": corridor.get("status"),
            })
        domain["limitations"] = [
            "confirmed CNS gap = operational support deficit；它不等于 route unsafe，"
            "也绝不把 geometry validation 改成 failed。",
            "coverage 只是几何覆盖，capability 只是静态模型匹配：二者都不代表运行时可用度。",
            "上游结果未运行或证据不足时 status=not_run/unknown，绝不当作 met。",
        ]
        domain["semantics"] = dict(CNS_GAP_SEMANTICS)
        return domain

    # ------------------------------------------------------------------ helpers

    def _evidence_summary(self, status, domains):
        hard = [
            domain_id for domain_id in DOMAIN_IDS
            if domains[domain_id].get("hard_constraint_failure")
        ]
        unresolved, incomplete = [], []
        from ..domain.route_safety_evidence_v2 import (
            INCOMPLETE_DOMAIN_STATUSES, UNRESOLVED_DOMAIN_STATUSES,
        )
        for domain_id in DOMAIN_IDS:
            domain_status = domains[domain_id].get("status")
            if domain_status in UNRESOLVED_DOMAIN_STATUSES[domain_id]:
                unresolved.append(domain_id)
            if domain_status in INCOMPLETE_DOMAIN_STATUSES[domain_id]:
                incomplete.append(domain_id)
        return {
            "status": status,
            "hard_constraint_domains": hard,
            "unresolved_domains": unresolved,
            "incomplete_domains": incomplete,
            "domains": {domain_id: domains[domain_id].get("status") for domain_id in DOMAIN_IDS},
            "statement": (
                "该状态表示证据完整性/明确硬约束结果，不是自动安全认证或安全评分。"
            ),
            "is_safety_certification": False,
            "produces_safety_score": False,
            "produces_safety_ranking": False,
            "cns_gap_is_operational_support_deficit_not_route_unsafe": True,
        }

    def _limitations(self, domains, lineage):
        limitations = [
            "Route Safety Evidence V2 是 post-planning evidence aggregation：它不产生 "
            "safe/unsafe 结论，不产生事故/致命概率，不产生 SORA GRC/ARC，也不产生自动安全等级。",
            "四个 domain 分开报告；不存在 overall risk/safety score 或排名。",
            "CNS confirmed gap 只表示 operational support deficit，不表示 route unsafe。",
        ]
        for domain_id in DOMAIN_IDS:
            for item in domains[domain_id].get("limitations") or []:
                limitations.append(f"[{DOMAIN_LABELS[domain_id]}] {item}")
        if lineage["missing_links"]:
            limitations.append(
                "lineage 不完整（缺 " + ", ".join(lineage["missing_links"]) + "）："
                "结果不得视为 current。"
            )
        if lineage["stale_links"]:
            limitations.append(
                "lineage 存在 stale 证据（" + ", ".join(lineage["stale_links"]) + "）："
                "结果不得视为 current。"
            )
        return limitations

    def _upstream_result_states(self):
        state = self.session.state
        return {
            name: {
                "status": (state.get(name) or {}).get("status", "not_calculated"),
                "input_fingerprint": (state.get(name) or {}).get("input_fingerprint"),
            }
            for name in _UPSTREAM_RESULT_IDS
        }

    def _resolve_lineage(self, payload=None):
        payload = payload if isinstance(payload, dict) else {}
        self.ensure_state()
        collection = self.adoptions.result_snapshot()
        published = [
            item for item in collection.get("items") or []
            if item.get("status") == "published"
        ]
        wanted = str(payload.get("adoption_id") or payload.get("route_id") or "")
        adoption = None
        if wanted:
            adoption = next((
                item for item in published
                if str(item.get("adoption_id")) == wanted
                or str(item.get("route_id")) == wanted
            ), None)
        if adoption is None and not wanted:
            adoption = next((
                item for item in reversed(published)
                if item.get("current_applicability") == "current"
            ), None)
        if adoption is None and not wanted and published:
            # A published adoption that is no longer current is still reported — as stale —
            # instead of being silently downgraded to "no adoption at all".
            adoption = published[-1]
        missing, stale, blockers = [], [], []
        if adoption is None:
            missing.append("published_layered_operational_adoption")
            blockers.append(_block(
                "published_layered_operational_adoption_missing",
                "当前项目没有已发布的 layered operational adoption：先完成 validation 与 adopt",
            ))
            return {
                "adoption": None, "validation": None, "candidate": None,
                "route_risk_profile": None, "operational_route": None,
                "missing_links": missing, "stale_links": stale, "complete": False,
                "blockers": blockers,
            }
        if adoption.get("current_applicability") != "current":
            stale.append(
                "adoption:" + str(adoption.get("current_applicability") or "not_current")
            )
        route_id = str(adoption.get("route_id") or "")
        operational_route = next((
            item for item in self.session.state.get("operational_routes") or []
            if str(item.get("route_id")) == route_id
        ), None)
        if operational_route is None:
            missing.append("operational_route")
        elif operational_route.get("status") not in ("passed", None):
            stale.append("operational_route:" + str(operational_route.get("status")))
        validation = self._select_validation(adoption.get("validation_id"))
        if validation is None:
            missing.append("layered_route_validation")
        elif validation.get("current_applicability") != "current":
            stale.append(
                "validation:" + str(validation.get("current_applicability") or "not_current")
            )
        candidate = None
        if validation is not None:
            candidate = self.validations._select_current_candidate({
                "candidate_id": (validation.get("candidate") or {}).get("candidate_id"),
            })
        if candidate is None:
            missing.append("layered_route_candidate")
        elif candidate.get("current_applicability") != "current":
            stale.append(
                "candidate:" + str(candidate.get("current_applicability") or "not_current")
            )
        profile = (
            self.validations._current_risk_profile(candidate)
            if candidate is not None else None
        )
        if profile is None:
            missing.append("current_route_risk_profile")
        if missing:
            blockers.append(_block(
                "lineage_incomplete",
                "lineage 不完整（缺 " + ", ".join(missing) + "）：结果不得伪装 current",
            ))
        if stale:
            blockers.append(_block(
                "lineage_stale",
                "lineage 存在 stale 证据（" + ", ".join(stale) + "）：结果不得伪装 current",
            ))
        return {
            "adoption": adoption, "validation": validation, "candidate": candidate,
            "route_risk_profile": profile, "operational_route": operational_route,
            "missing_links": missing, "stale_links": stale,
            "complete": not missing and not stale, "blockers": blockers,
        }

    def _select_validation(self, validation_id):
        """Resolve one stored validation record by id (or the active one)."""

        collection = self.validations.result_snapshot()
        wanted = str(validation_id or "")
        items = list(collection.get("items") or [])
        if not wanted:
            active = collection.get("active_validation_id")
            return next(
                (item for item in items if item.get("validation_id") == active), None,
            )
        return next(
            (item for item in items if str(item.get("validation_id")) == wanted), None,
        )

    def _shelter_policy(self, candidate):
        policy = (candidate.get("provenance") or {}).get("shelter_policy")
        if isinstance(policy, dict):
            return deepcopy(policy)
        state_policy = self.session.state.get("shelter_coefficient_policy") or {}
        return {
            "status": state_policy.get("status"),
            "source": state_policy.get("source"),
            "provenance": state_policy.get("provenance"),
            "default_coefficient": state_policy.get("default_coefficient"),
            "confirmed": bool(state_policy.get("confirmed")),
        }

    def _references(self, lineage):
        """The declared evidence dependencies of one assessment (the fingerprint inputs).

        Exactly the references named by the contract: operational route / adoption, validation,
        candidate, RouteRiskProfile, regulatory dataset, Coverage3D, CNS capability, Gap V2,
        corridor evidence (when consumed) and the evaluator version.
        """

        adoption = lineage.get("adoption") or {}
        validation = lineage.get("validation") or {}
        candidate = lineage.get("candidate") or {}
        profile = lineage.get("route_risk_profile") or {}
        state = self.session.state
        constraints = state.get("regulatory_constraints") or default_regulatory_constraints()
        coverage = state.get("coverage_3d") or {}
        capability = state.get("cns_service_capability") or {}
        gap = state.get("cns_gap_analysis_v2") or {}
        corridor = state.get("cns_corridor_assessment") or {}
        route_id = str((lineage.get("operational_route") or {}).get("route_id") or "")
        corridor_consumed = bool(
            route_id and _route_of(corridor, route_id)
            and str(corridor.get("status")) not in ("not_calculated", "stale")
        )
        return {
            "operational_route_adoption_fingerprint": stable_fingerprint({
                "adoption_id": adoption.get("adoption_id"),
                "route_id": adoption.get("route_id"),
                "projection_fingerprint": adoption.get("projection_fingerprint"),
                "validation_fingerprint": adoption.get("validation_fingerprint"),
                "candidate_fingerprint": adoption.get("candidate_fingerprint"),
                "operational_route_status": (
                    lineage.get("operational_route") or {}
                ).get("status"),
            }, prefix="rsa-adoption-"),
            "validation_fingerprint": (validation.get("fingerprints") or {}).get(
                "validation_fingerprint"
            ),
            "candidate_fingerprint": candidate.get("candidate_fingerprint"),
            "route_risk_profile_fingerprint": (profile.get("fingerprints") or {}).get(
                "profile_fingerprint"
            ),
            "regulatory_dataset_fingerprint": constraints.get("dataset_fingerprint"),
            "coverage_3d_fingerprint": coverage.get("input_fingerprint"),
            "coverage_3d_status": coverage.get("status"),
            "cns_service_capability_fingerprint": capability.get("input_fingerprint"),
            "cns_service_capability_status": capability.get("status"),
            "cns_gap_v2_fingerprint": gap.get("input_fingerprint"),
            "cns_gap_v2_status": gap.get("status"),
            "cns_corridor_fingerprint": (
                corridor.get("input_fingerprint") if corridor_consumed else None
            ),
            "cns_corridor_consumed": corridor_consumed,
            "evaluator_version": f"{ALGORITHM_ID}@{ALGORITHM_VERSION}",
        }

    def _audit_components(self, lineage, domains, references):
        """The recorded (non-fingerprint) audit view of one evaluation."""

        return {
            "references": deepcopy(references),
            "upstream_statuses": self._upstream_result_states(),
            "regulatory_configured": regulatory_is_configured(
                self.session.state.get("regulatory_constraints")
            ),
            "domain_statuses": {
                domain_id: (domains.get(domain_id) or {}).get("status")
                for domain_id in DOMAIN_IDS
            },
            "lineage": {
                "complete": lineage["complete"],
                "missing_links": list(lineage["missing_links"]),
                "stale_links": list(lineage["stale_links"]),
            },
        }

    def _expected_fingerprint(self, record, lineage):
        if lineage is None:
            return None
        return assessment_fingerprint(self._references(lineage))

    def _not_ready_record(self, lineage, reason_code):
        record = empty_route_safety_evidence_v2("not_ready")
        route_id = (lineage.get("operational_route") or {}).get("route_id")
        identity = assessment_fingerprint({
            "reason_code": reason_code,
            "missing_links": list(lineage["missing_links"]),
            "stale_links": list(lineage["stale_links"]),
            "route_id": route_id,
            "evaluator_version": f"{ALGORITHM_ID}@{ALGORITHM_VERSION}",
        })
        record.update({
            "assessment_id": "RSE-" + identity[-12:].upper(),
            "route_id": route_id,
            "adoption_id": (lineage.get("adoption") or {}).get("adoption_id"),
            "status": "not_ready",
            "status_reason": reason_code,
            "current_applicability": "not_ready",
            "created_at": utc_now(),
            "lineage": {
                **record["lineage"],
                "status": "incomplete",
                "missing_links": list(lineage["missing_links"]),
                "stale_links": list(lineage["stale_links"]),
                "complete": False,
            },
            "limitations": [
                "lineage 未解析（" + ", ".join(lineage["missing_links"] or [reason_code]) + "）："
                "没有可聚合的既有证据，not_ready 既不是 pass 也不是 fail。"
            ],
        })
        for domain_id in DOMAIN_IDS:
            record["domains"][domain_id]["status"] = "not_ready"
            record["domains"][domain_id]["status_reason"] = reason_code
        record["evidence_summary"]["domains"] = {
            domain_id: "not_ready" for domain_id in DOMAIN_IDS
        }
        record["fingerprints"]["assessment_fingerprint"] = identity
        record["fingerprints"]["components"] = {"not_ready": identity}
        return record

    def _store(self, record, *, save):
        state = self.ensure_state()
        collection = normalize_route_safety_evidence_v2_collection(
            state.get("route_safety_evidence_v2")
        )
        items = [
            item for item in collection["items"]
            if str(item.get("assessment_id")) != str(record.get("assessment_id"))
        ]
        items.append(record)
        collection["items"] = items
        collection["count"] = len(items)
        collection["status"] = record["status"]
        if record.get("status") != "not_ready":
            collection["active_assessment_id"] = record.get("assessment_id")
        state["route_safety_evidence_v2"] = collection
        state.setdefault("result_statuses", {})["route_safety_evidence_v2"] = record["status"]
        if save:
            self.session.save()
        return self.result_snapshot()


# --------------------------------------------------------------------------- helpers


def _block(code, reason):
    return {"reason_code": str(code), "reason": str(reason)}


def _route_of(result, route_id):
    if not route_id:
        return None
    return next((
        item for item in (result or {}).get("routes") or []
        if str(item.get("route_id")) == str(route_id)
    ), None)


def _subsystem_index(route):
    return {
        str(item.get("subsystem")): item
        for item in (route or {}).get("subsystems") or [] if isinstance(item, dict)
    }


def _subsystem_of(route, code):
    return _subsystem_index(route).get(code)


def _support_verdict(gap_subsystem):
    status = str((gap_subsystem or {}).get("status") or "")
    if status == "confirmed_gap":
        return "operational_support_deficit"
    if status in ("unknown", "missing_data", ""):
        return "unknown"
    if status in ("satisfied", "satisfied_by_contingency"):
        return "supported_under_model"
    if status == "not_applicable":
        return "not_applicable"
    return "unknown"


def _source_rows(source_audits):
    rows = []
    for role, item in sorted((source_audits or {}).items()):
        value = item if isinstance(item, dict) else {}
        rows.append({
            "role": role,
            "status": value.get("status"),
            "file_name": value.get("file_name"),
            "sha256": value.get("sha256"),
        })
    return rows


__all__ = ["RouteSafetyEvidenceService"]
