"""Conservative dependency invalidation for later result-producing stages."""
from dataclasses import dataclass, field
try:
    from ..domain.status import ResultStatus
except ImportError:
    from models.status import ResultStatus

DEPENDENTS = {
    "data": ("workspace", "environment_risk", "routes", "coverage", "cns_gap", "coverage_3d", "cns_service_capability", "service_timeline", "report"),
    "workspace": ("environment_risk", "routes", "coverage", "cns_gap", "coverage_3d", "cns_service_capability", "service_timeline", "report"),
    "route": ("coverage", "cns_gap", "coverage_3d", "cns_service_capability", "service_timeline", "report"),
    "rules": ("routes", "coverage", "cns_gap", "coverage_3d", "cns_service_capability", "service_timeline", "technical_risk", "report"),
    "aircraft_profile": ("routes", "coverage", "cns_gap", "cns_service_capability", "service_timeline", "technical_risk", "report"),
    "required_cns": ("coverage", "cns_gap", "cns_service_capability", "service_timeline", "report"),
    "devices": ("coverage", "cns_gap", "coverage_3d", "cns_service_capability", "service_timeline", "technical_risk", "report"),
    "existing_cns": ("coverage", "cns_gap", "coverage_3d", "cns_service_capability", "service_timeline", "technical_risk", "report"),
    "candidate_sites": ("coverage", "technical_risk", "report"),
    "sites": ("coverage", "technical_risk", "report"),
    "route_algorithm": ("routes", "coverage", "cns_gap", "coverage_3d", "cns_service_capability", "service_timeline", "report"),
    "coverage_algorithm": ("coverage", "report"),
    "gap_algorithm": ("cns_gap", "report"),
    "coverage_model": ("coverage_3d", "cns_service_capability", "service_timeline", "report"),
    "service_model": ("cns_service_capability", "service_timeline", "report"),
    "motion_profile": ("service_timeline", "report"),
    "service_scenario": ("service_timeline", "report"),
    "timeline_model": ("service_timeline", "report"),
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
