"""Risk Framework V2 top-level model.

``GridRiskModelV2`` turns the single legacy relative ``overall`` risk of
``RiskModelV1`` into a layered, explainable decomposition::

    Risk Factor / Exposure  →  ground / air_traffic / environment_obstacle

The output is still a **relative engineering index**.  The model refuses to
compute absolute risk, SORA GRC and SORA ARC, and it keeps the airspace layer
display-only.  It is additive: ``RiskModelV1`` / ``grid_risk`` and the current
``RiskAwareRoutePlannerV2`` consumption of ``grid_risk`` are untouched, and no
default production weight is introduced.
"""

from __future__ import annotations

from copy import deepcopy
import math

from ..domain.risk_v2 import (
    DOMAIN_IDS, FACTOR_IDS, empty_grid_risk_v2, normalize_risk_policy_v2,
    policy_fingerprint, stable_fingerprint,
)
from .domains_v2 import (
    STATUS_NOT_CALCULATED, STATUS_PASSED, STATUS_PENDING, STATUS_STALE,
    STATUS_UNRESOLVED, aggregate_domains, domain_summary,
)
from .factors_v2 import FactorExtraction

_STATUS_PRIORITY = (
    STATUS_STALE, STATUS_PENDING, STATUS_UNRESOLVED, STATUS_PASSED,
)


def combine_statuses(statuses):
    """Conservative status join: stale > pending > unresolved > passed."""

    unique = {status for status in statuses if status}
    if not unique:
        return STATUS_NOT_CALCULATED
    for candidate in _STATUS_PRIORITY:
        if candidate in unique:
            return candidate
    return sorted(unique)[0]


class GridRiskModelV2:
    algorithm_id = "risk-framework-v2-domains"
    algorithm_version = "2.0"

    def evaluate(self, grid, grid_attributes, parameters=None):
        settings = dict(parameters or {})
        policy = normalize_risk_policy_v2(settings.get("policy"))
        levels_by_domain = settings.get("risk_levels_by_domain") or {}
        extraction = FactorExtraction(grid, grid_attributes).extract()

        result = empty_grid_risk_v2()
        result["policy"] = policy
        result["policy_fingerprint"] = policy_fingerprint(policy)
        result["input_status"] = extraction["input_status"]
        result["source_versions"] = extraction["source_versions"]
        result["references"] = extraction["references"]
        result["factor_status"] = extraction["factor_status"]
        result["input_fingerprint"] = stable_fingerprint({
            "policy_fingerprint": result["policy_fingerprint"],
            "factor_input_fingerprint": extraction["input_fingerprint"],
            "grid_level": (grid or {}).get("level"),
        }, prefix="riskframeworkv2-")

        cells = list((grid or {}).get("cells") or [])
        if not cells or (grid or {}).get("status") != "passed":
            result["status"] = STATUS_NOT_CALCULATED if not cells else "missing_data"
            return result

        result["grid_level"] = (grid or {}).get("level")
        result["count"] = len(cells)
        cell_results, per_domain = {}, {domain_id: {} for domain_id in DOMAIN_IDS}
        for cell in cells:
            grid_id = cell["grid_id"]
            factor_records = {
                factor_id: (extraction["factors"].get(factor_id) or {}).get(grid_id)
                for factor_id in FACTOR_IDS
            }
            domains = aggregate_domains(
                factor_records, policy, levels_by_domain=levels_by_domain,
            )
            cell_results[grid_id] = {
                "grid_id": grid_id,
                "status": combine_statuses(
                    domain.get("status") for domain in domains.values()
                ),
                "factors": factor_records,
                "domains": domains,
                "data_completeness": _mean(
                    domain.get("data_completeness") for domain in domains.values()
                ),
                "semantics": "relative_engineering_index",
            }
            for domain_id, domain in domains.items():
                per_domain[domain_id][grid_id] = domain

        result["cells"] = cell_results
        result["domains"] = {
            domain_id: domain_summary(
                domain_id, per_domain[domain_id], policy["domains"][domain_id],
            )
            for domain_id in DOMAIN_IDS
        }
        result["data_completeness"] = _mean(
            cell.get("data_completeness") for cell in cell_results.values()
        )
        result["status"] = combine_statuses(
            cell.get("status") for cell in cell_results.values()
        )
        # Cross-domain ``overall`` is never defaulted: cost weights belong to the
        # next-stage Layered Risk-Aware Route Planner policy, not to this framework.
        result["overall"] = deepcopy(empty_grid_risk_v2()["overall"])
        return result


def _mean(values):
    clean = [float(value) for value in values if isinstance(value, (int, float)) and math.isfinite(float(value))]
    return sum(clean) / len(clean) if clean else 0.0


__all__ = ["GridRiskModelV2", "combine_statuses"]
