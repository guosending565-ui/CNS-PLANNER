"""Deterministic reliability formula helpers; no random service-state draws."""

from __future__ import annotations

from math import exp, isfinite

from ..domain.cns_reliability import normalize_reliability_spec


def evaluate_reliability(spec: dict | None, mission_duration_h=None) -> dict:
    normalized = normalize_reliability_spec(spec)
    duration = _optional_nonnegative(mission_duration_h, "mission_duration_h")
    result = {
        "status": normalized["status"], "model": normalized["model"],
        "mission_duration_h": duration, "failure_rate_per_h": None,
        "reliability": None, "failure_probability": None,
        "inherent_availability": None, "reported_availability": None,
        "direct_availability": None,
        "empirical_failure_probability": normalized["empirical_failure_probability"],
        "assumptions": [], "source": normalized["source"],
        "confirmed": normalized["confirmed"],
    }
    mtbf, rate = normalized["mtbf_h"], normalized["failure_rate_per_h"]
    if normalized["model"] == "constant_rate_exponential" and (mtbf is not None or rate is not None):
        rate = rate if rate is not None else 1.0 / mtbf
        result["failure_rate_per_h"] = rate
        result["assumptions"].append("constant_rate_exponential")
        if duration is not None:
            reliability = exp(-rate * duration)
            result["reliability"] = reliability
            result["failure_probability"] = 1.0 - reliability
    if mtbf is not None and normalized["mttr_h"] is not None:
        result["inherent_availability"] = mtbf / (mtbf + normalized["mttr_h"])
        result["assumptions"].append("inherent_availability_from_mtbf_mttr")
    if normalized["availability"] is not None:
        reported = {
            "value": normalized["availability"],
            "availability_type": normalized["availability_type"],
        }
        result["reported_availability"] = reported
        if normalized["availability_type"] in ("operational", "empirical"):
            result["direct_availability"] = reported
    return result


def _optional_nonnegative(value, field):
    if value in (None, ""):
        return None
    number = float(value)
    if not isfinite(number) or number < 0:
        raise ValueError(f"{field} 不得小于零")
    return number
