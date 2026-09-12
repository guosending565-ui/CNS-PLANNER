"""Conservative dependency invalidation for later result-producing stages."""
from dataclasses import dataclass, field
try:
    from ..domain.status import ResultStatus
except ImportError:
    from models.status import ResultStatus

DEPENDENTS = {
    "data": ("workspace", "environment_risk", "routes", "coverage", "cns_gap", "coverage_3d", "cns_service_capability", "service_timeline", "cns_gap_v2", "cns_site_plan", "report"),
    "workspace": ("environment_risk", "routes", "coverage", "cns_gap", "coverage_3d", "cns_service_capability", "service_timeline", "cns_gap_v2", "cns_site_plan", "report"),
    "route": ("coverage", "cns_gap", "coverage_3d", "cns_service_capability", "service_timeline", "cns_gap_v2", "cns_site_plan", "report"),
    "rules": ("routes", "coverage", "cns_gap", "coverage_3d", "cns_service_capability", "service_timeline", "cns_gap_v2", "technical_risk", "report"),
    "aircraft_profile": ("routes", "coverage", "cns_gap", "cns_service_capability", "service_timeline", "cns_gap_v2", "cns_site_plan", "technical_risk", "report"),
    "required_cns": ("coverage", "cns_gap", "cns_service_capability", "service_timeline", "cns_gap_v2", "cns_site_plan", "report"),
    "devices": ("coverage", "cns_gap", "coverage_3d", "cns_service_capability", "service_timeline", "cns_gap_v2", "cns_site_plan", "technical_risk", "report"),
    "existing_cns": ("coverage", "cns_gap", "coverage_3d", "cns_service_capability", "service_timeline", "cns_gap_v2", "cns_site_plan", "technical_risk", "report"),
    "candidate_sites": ("coverage", "cns_site_plan", "technical_risk", "report"),
    "sites": ("coverage", "technical_risk", "report"),
    "route_algorithm": ("routes", "coverage", "cns_gap", "coverage_3d", "cns_service_capability", "service_timeline", "cns_gap_v2", "report"),
    "coverage_algorithm": ("coverage", "report"),
    "gap_algorithm": ("cns_gap", "report"),
    "coverage_model": ("coverage_3d", "cns_service_capability", "service_timeline", "cns_gap_v2", "cns_site_plan", "report"),
    "service_model": ("cns_service_capability", "service_timeline", "cns_gap_v2", "cns_site_plan", "report"),
    "motion_profile": ("service_timeline", "cns_gap_v2", "report"),
    "service_scenario": ("service_timeline", "cns_gap_v2", "report"),
    "timeline_model": ("service_timeline", "cns_gap_v2", "report"),
    "coverage_3d_result": ("cns_gap_v2", "report"),
    "cns_service_capability_result": ("cns_gap_v2", "report"),
    "service_timeline_result": ("cns_gap_v2", "report"),
    "cns_gap_v2_result": ("cns_site_plan", "report"),
    "site_planning_policy": ("cns_site_plan", "report"),
    "site_planner": ("cns_site_plan", "report"),
    "response_time_budget": ("protection_envelope", "report"),
    "encounter_scenario": ("protection_envelope", "report"),
    "protection_model": ("protection_envelope", "report"),
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
