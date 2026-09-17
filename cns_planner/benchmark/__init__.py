"""Synthetic, reproducible route-planning benchmark support.

``fixtures`` holds the deterministic case definitions and ``quality`` holds the
independent route-quality evaluator.  Both are read-only w.r.t. planner outputs:
nothing in this package may modify V1/V2 behaviour or their result contracts.
"""

from .fixtures import CASES, WORKSPACE_BBOX, case, case_ids, cases, malformed_payloads
from .quality import evaluate_constraint_feasibility, evaluate_route_quality, polyline_metrics

__all__ = [
    "CASES",
    "WORKSPACE_BBOX",
    "case",
    "case_ids",
    "cases",
    "malformed_payloads",
    "evaluate_constraint_feasibility",
    "evaluate_route_quality",
    "polyline_metrics",
]
