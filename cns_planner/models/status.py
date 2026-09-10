"""Explicit result states prevent unknown risk from being treated as zero or passed."""
from dataclasses import dataclass, field
from builtins import property as computed_property
from enum import StrEnum
from typing import Iterable


class ResultStatus(StrEnum):
    NOT_CALCULATED = "not_calculated"
    MISSING_DATA = "missing_data"
    PENDING_CONFIRMATION = "pending_confirmation"
    PASSED = "passed"
    FAILED = "failed"
    NOT_APPLICABLE = "not_applicable"
    STALE = "stale"


@dataclass(frozen=True)
class Assessment:
    name: str
    status: ResultStatus = ResultStatus.NOT_CALCULATED
    value: float | None = None
    threshold: float | None = None
    unit: str | None = None
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class SafetyResult:
    """Environment, technical, life and property risk stay separate."""
    environment: Assessment = field(default_factory=lambda: Assessment("environment_risk"))
    technical: Assessment = field(default_factory=lambda: Assessment("technical_risk"))
    life: Assessment = field(default_factory=lambda: Assessment("life_risk"))
    property: Assessment = field(default_factory=lambda: Assessment("property_risk"))

    @computed_property
    def overall_status(self) -> ResultStatus:
        required: Iterable[Assessment] = (self.environment, self.technical, self.life, self.property)
        states = [item.status for item in required]
        if ResultStatus.FAILED in states:
            return ResultStatus.FAILED
        if all(value in (ResultStatus.PASSED, ResultStatus.NOT_APPLICABLE) for value in states):
            return ResultStatus.PASSED
        if ResultStatus.STALE in states:
            return ResultStatus.STALE
        if ResultStatus.MISSING_DATA in states:
            return ResultStatus.MISSING_DATA
        if ResultStatus.PENDING_CONFIRMATION in states:
            return ResultStatus.PENDING_CONFIRMATION
        return ResultStatus.NOT_CALCULATED

    @computed_property
    def overall_pass(self) -> bool | None:
        """Unknown stays None; only a fully passed evaluation returns True."""
        status = self.overall_status
        return True if status == ResultStatus.PASSED else False if status == ResultStatus.FAILED else None
