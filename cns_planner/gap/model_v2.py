"""JSON-safe contracts for additive CNS Gap Analysis V2 results."""

from typing import Any, TypedDict


COMBINED_GAP_STATUSES = (
    "satisfied", "satisfied_by_contingency", "confirmed_gap", "unknown",
    "not_applicable",
)

GAP_CAUSES = (
    "geometry_gap", "static_service_mismatch", "runtime_service_loss",
    "protection_margin_gap", "unknown_evidence",
)

REMEDIATION_SCOPES = (
    "ground_service_candidate", "aircraft_or_requirement",
    "operational_scenario", "evidence_collection", "mixed", "unknown",
)


class GapSegmentV2(TypedDict, total=False):
    segment_id: str
    route_id: str
    subsystem: str
    start_route_offset_m: float
    end_route_offset_m: float
    length_m: float
    start_time_s: float | None
    end_time_s: float | None
    duration_s: float | None
    planning_status: str
    runtime_status: str
    combined_status: str
    gap_causes: list[str]
    reasons: list[str]
    evidence: list[dict[str, Any]]
    contingency_exposure: dict[str, Any]
    remediation_scope: str

