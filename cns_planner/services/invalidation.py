"""Conservative dependency invalidation for later result-producing stages."""
from dataclasses import dataclass, field
try:
    from ..models.status import ResultStatus
except ImportError:
    from models.status import ResultStatus

DEPENDENTS = {
    "data": ("workspace", "environment_risk", "routes", "coverage", "report"),
    "workspace": ("environment_risk", "routes", "coverage", "report"),
    "route": ("coverage", "report"),
    "rules": ("routes", "coverage", "technical_risk", "report"),
    "devices": ("coverage", "technical_risk", "report"),
    "sites": ("coverage", "technical_risk", "report"),
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
