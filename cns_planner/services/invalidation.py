"""Conservative dependency invalidation for later result-producing stages."""
from dataclasses import dataclass, field
try:
    from ..domain.status import ResultStatus
except ImportError:
    from models.status import ResultStatus

DEPENDENTS = {
    "data": ("workspace", "environment_risk", "routes", "coverage", "cns_gap", "coverage_3d", "cns_service_capability", "service_timeline", "cns_gap_v2", "cns_site_plan", "cns_corridor_assessment", "report"),
    "workspace": ("environment_risk", "routes", "coverage", "cns_gap", "coverage_3d", "cns_service_capability", "service_timeline", "cns_gap_v2", "cns_site_plan", "cns_corridor_assessment", "report"),
    "route": ("coverage", "cns_gap", "coverage_3d", "cns_service_capability", "service_timeline", "cns_gap_v2", "cns_site_plan", "cns_corridor_assessment", "required_cns_recommendation", "report"),
    "rules": ("routes", "coverage", "cns_gap", "coverage_3d", "cns_service_capability", "service_timeline", "cns_gap_v2", "technical_risk", "report"),
    "aircraft_profile": ("routes", "coverage", "cns_gap", "cns_service_capability", "service_timeline", "cns_gap_v2", "cns_site_plan", "cns_corridor_assessment", "technical_risk", "report"),
    "required_cns": ("coverage", "cns_gap", "cns_service_capability", "service_timeline", "cns_gap_v2", "cns_site_plan", "cns_corridor_assessment", "cns_corridor_gap_assessment", "cns_corridor_site_plan", "report"),
    "devices": ("coverage", "cns_gap", "coverage_3d", "cns_service_capability", "service_timeline", "cns_gap_v2", "cns_site_plan", "cns_corridor_assessment", "cns_corridor_site_plan", "technical_risk", "report"),
    "existing_cns": ("coverage", "cns_gap", "coverage_3d", "cns_service_capability", "service_timeline", "cns_gap_v2", "cns_site_plan", "cns_corridor_assessment", "cns_corridor_site_plan", "technical_risk", "report"),
    "candidate_sites": ("coverage", "cns_site_plan", "cns_corridor_site_plan", "technical_risk", "report"),
    "sites": ("coverage", "technical_risk", "report"),
    "route_algorithm": ("routes", "coverage", "cns_gap", "coverage_3d", "cns_service_capability", "service_timeline", "cns_gap_v2", "report"),
    "airspace_policy": ("routes", "coverage", "cns_gap", "coverage_3d", "cns_service_capability", "service_timeline", "cns_gap_v2", "cns_site_plan", "cns_corridor_assessment", "report"),
    "coverage_algorithm": ("coverage", "report"),
    "gap_algorithm": ("cns_gap", "report"),
    "coverage_model": ("coverage_3d", "cns_service_capability", "service_timeline", "cns_gap_v2", "cns_site_plan", "cns_corridor_assessment", "report"),
    "service_model": ("cns_service_capability", "service_timeline", "cns_gap_v2", "cns_site_plan", "cns_corridor_assessment", "report"),
    "motion_profile": ("service_timeline", "cns_gap_v2", "report"),
    "service_scenario": ("service_timeline", "cns_gap_v2", "report"),
    "timeline_model": ("service_timeline", "cns_gap_v2", "report"),
    "coverage_3d_result": ("cns_gap_v2", "report"),
    "cns_service_capability_result": ("cns_gap_v2", "report"),
    "service_timeline_result": ("cns_gap_v2", "report"),
    "cns_gap_v2_result": ("cns_site_plan", "report"),
    "site_planning_policy": ("cns_site_plan", "report"),
    "site_planner": ("cns_site_plan", "cns_corridor_site_plan", "report"),
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
    "requirement_context": ("required_cns_recommendation",),
    "requirement_policies": ("required_cns_recommendation",),
    "requirement_model": ("required_cns_recommendation",),
    "safety_policy": ("safety_assessment", "technical_risk", "report"),
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
