"""Stable CNS Gap Analysis input/output boundary."""

from typing import Any, Protocol, TypedDict


class RouteSubsystemGap(TypedDict, total=False):
    route_id: str
    subsystem: str
    status: str
    required: bool | None
    aircraft_capability_satisfied: bool | None
    ground_coverage: dict[str, Any]
    uncovered_segments: list[dict[str, Any]]
    coverage_ratio: float | None
    gap_length_m: float | None
    reasons: list[str]
    input_fingerprint: str


class GapAnalysisResult(TypedDict, total=False):
    status: str
    algorithm_id: str
    algorithm_version: str
    input_fingerprint: str
    route_count: int
    routes: list[dict[str, Any]]


class GapAnalyzer(Protocol):
    def analyze(
        self,
        operational_routes: list[dict],
        required_cns: dict,
        aircraft_profile: dict | None,
        existing_facilities: dict,
        device_catalog: dict,
    ) -> GapAnalysisResult: ...
