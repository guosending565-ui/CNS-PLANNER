"""Conservative dependency invalidation for later result-producing stages."""
from dataclasses import dataclass, field
try:
    from ..domain.status import ResultStatus
except ImportError:
    from models.status import ResultStatus

DEPENDENTS = {
    "data": ("workspace", "environment_risk", "routes", "coverage_3d", "cns_service_capability", "service_timeline", "cns_corridor_assessment", "report"),
    "workspace": ("environment_risk", "routes", "coverage_3d", "cns_service_capability", "service_timeline", "cns_corridor_assessment", "report"),
    "route": ("coverage_3d", "cns_service_capability", "service_timeline", "cns_corridor_assessment", "required_cns_recommendation", "report"),
    "rules": ("routes", "coverage_3d", "cns_service_capability", "service_timeline", "technical_risk", "report"),
    "aircraft_profile": ("routes", "cns_service_capability", "service_timeline", "cns_corridor_assessment", "technical_risk", "report"),
    "required_cns": ("coverage_3d", "cns_service_capability", "service_timeline", "cns_corridor_assessment", "cns_corridor_gap_assessment", "cns_corridor_site_plan", "report"),
    "devices": ("coverage_3d", "cns_service_capability", "service_timeline", "cns_corridor_assessment", "cns_corridor_site_plan", "technical_risk", "report"),
    "existing_cns": ("coverage_3d", "cns_service_capability", "service_timeline", "cns_corridor_assessment", "cns_corridor_site_plan", "technical_risk", "report"),
    "candidate_sites": ("cns_corridor_site_plan", "technical_risk", "report"),
    "sites": ("coverage_3d", "technical_risk", "report"),
    # The production layered selection only stales its candidate product and report.
    "layered_route_planner_algorithm": ("report",),
    "airspace_policy": (),
    "coverage_model": ("coverage_3d", "cns_service_capability", "service_timeline", "cns_corridor_assessment", "report"),
    "service_model": ("cns_service_capability", "service_timeline", "cns_corridor_assessment", "report"),
    "motion_profile": ("service_timeline", "report"),
    "service_scenario": ("service_timeline", "report"),
    "timeline_model": ("service_timeline", "report"),
    "coverage_3d_result": ("cns_service_capability", "cns_corridor_assessment", "report"),
    "cns_service_capability_result": ("cns_corridor_assessment", "report"),
    "service_timeline_result": ("report",),
    "site_planner": ("cns_corridor_site_plan", "report"),
    "response_time_budget": ("protection_envelope", "report"),
    "encounter_scenario": ("protection_envelope", "report"),
    "protection_model": ("protection_envelope", "report"),
    "corridor_policy": ("cns_corridor_assessment",),
    "corridor_model": ("cns_corridor_assessment",),
    "corridor_result": ("cns_corridor_gap_assessment",),
    "planning_objectives": ("cns_corridor_gap_assessment",),
    "corridor_gap_analyzer": ("cns_corridor_gap_assessment",),
    # P15 result is a one-way input to the P16 proposal.
    "cns_corridor_gap_result": ("cns_corridor_site_plan",),
    "corridor_site_planning_policy": ("cns_corridor_site_plan",),
    "cns_corridor_site_plan_result": ("cns_plan_review", "report"),
    "cns_plan_review_result": ("report",),
    "requirement_context": ("required_cns_recommendation",),
    "requirement_policies": ("required_cns_recommendation",),
    "requirement_model": ("required_cns_recommendation",),
    "safety_policy": ("safety_assessment", "technical_risk", "report"),
}

# Minimal contract surface between the canonical DAG vocabulary and this legacy-shaped
# executor.  The canonical registry remains the dependency authority; these aliases only
# document which executor trigger/result keys implement its critical production edges.
CANONICAL_EXECUTOR_CONTRACT = {
    "operational_route": ("route", {"coverage": "coverage_3d", "service_corridor": "cns_corridor_assessment"}),
    "required_cns": ("required_cns", {"coverage": "coverage_3d", "service_capability": "cns_service_capability", "service_corridor": "cns_corridor_assessment", "capability_gap": "cns_corridor_gap_assessment", "facility_plan": "cns_corridor_site_plan", "report": "report"}),
    "coverage": ("coverage_3d_result", {"service_capability": "cns_service_capability", "service_corridor": "cns_corridor_assessment", "report": "report"}),
    "service_capability": ("cns_service_capability_result", {"service_corridor": "cns_corridor_assessment", "report": "report"}),
    "service_corridor": ("corridor_result", {"capability_gap": "cns_corridor_gap_assessment"}),
    "capability_gap": ("cns_corridor_gap_result", {"facility_plan": "cns_corridor_site_plan"}),
    "facility_plan": ("cns_corridor_site_plan_result", {"plan_review": "cns_plan_review", "report": "report"}),
    "plan_review": ("cns_plan_review_result", {"report": "report"}),
}


@dataclass
class ResultLedger:
    statuses: dict[str, ResultStatus] = field(default_factory=lambda: {name: ResultStatus.NOT_CALCULATED for names in DEPENDENTS.values() for name in names})

    def invalidate(self, changed: str) -> tuple[str, ...]:
        affected = DEPENDENTS.get(changed, ())
        for name in affected:
            if self.statuses.get(name) != ResultStatus.NOT_CALCULATED:
                self.statuses[name] = ResultStatus.STALE
        return affected
