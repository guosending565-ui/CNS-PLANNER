"""Canonical risk review aggregation based on domain status types."""

from copy import deepcopy

from ..domain.status import Assessment, ResultStatus, SafetyResult


class ReviewService:
    @staticmethod
    def review(state):
        risks = state["risks"]
        assessments = {
            name: Assessment(
                f"{name}_risk",
                ReviewService._status(risks[name].get("status")),
                risks[name].get("value"), risks[name].get("threshold"),
                risks[name].get("unit"),
                tuple(filter(None, [risks[name].get("source")])),
            )
            for name in ("environment", "technical", "life", "property")
        }
        aggregate = SafetyResult(**assessments)
        return {
            "risks": deepcopy(risks),
            "overall_status": aggregate.overall_status.value,
            "overall_pass": aggregate.overall_pass,
        }

    @staticmethod
    def _status(value):
        try:
            return ResultStatus(value)
        except ValueError:
            return ResultStatus.NOT_CALCULATED
