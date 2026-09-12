"""Lightweight, JSON-safe CNS reliability input contract."""

from __future__ import annotations

from copy import deepcopy
from math import isfinite
from typing import Any, TypedDict


RELIABILITY_MODELS = (
    "constant_rate_exponential", "empirical", "direct_availability", "unknown",
)
AVAILABILITY_TYPES = ("inherent", "operational", "empirical", "unknown")


class ReliabilitySpec(TypedDict, total=False):
    model: str
    mtbf_h: float | None
    mttr_h: float | None
    failure_rate_per_h: float | None
    availability: float | None
    availability_type: str
    empirical_failure_probability: float | None
    failure_mode: str | None
    operating_conditions: Any
    reference_conditions: Any
    confidence: float | None
    sample_size: int | None
    source: str | None
    confirmed: bool
    status: str


def empty_reliability_spec() -> ReliabilitySpec:
    return {
        "model": "unknown", "mtbf_h": None, "mttr_h": None,
        "failure_rate_per_h": None, "availability": None,
        "availability_type": "unknown", "empirical_failure_probability": None,
        "failure_mode": None, "operating_conditions": None,
        "reference_conditions": None, "confidence": None, "sample_size": None,
        "source": None, "confirmed": False, "status": "missing_data",
    }


def normalize_reliability_spec(
    value: dict | None,
    *,
    legacy_mtbf_h=None,
    legacy_source=None,
    legacy_confirmed=False,
    field="reliability",
) -> ReliabilitySpec:
    """Normalize a spec and backfill legacy MTBF without adding assumptions."""
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError(f"{field} 必须是对象")
    result = empty_reliability_spec()
    model = str(value.get("model") or "unknown").strip().lower().replace("-", "_").replace(" ", "_")
    model = {"exponential": "constant_rate_exponential", "constant_rate": "constant_rate_exponential"}.get(model, model)
    if model not in RELIABILITY_MODELS:
        raise ValueError(f"{field}.model 无效：{model}")
    availability_type = str(value.get("availability_type") or "unknown").strip().lower()
    if availability_type not in AVAILABILITY_TYPES:
        raise ValueError(f"{field}.availability_type 无效：{availability_type}")
    explicit_mtbf = _optional_positive(value.get("mtbf_h"), f"{field}.mtbf_h")
    legacy_mtbf = _optional_positive(legacy_mtbf_h, f"{field}.legacy_mtbf_h")
    if explicit_mtbf is not None and legacy_mtbf is not None and not _close(explicit_mtbf, legacy_mtbf):
        raise ValueError(f"{field}.mtbf_h 与旧 mtbf_h 不一致")
    mtbf = explicit_mtbf if explicit_mtbf is not None else legacy_mtbf
    failure_rate = _optional_positive(value.get("failure_rate_per_h"), f"{field}.failure_rate_per_h")
    if mtbf is not None and failure_rate is not None and not _close(failure_rate, 1.0 / mtbf):
        raise ValueError(f"{field}.mtbf_h 与 failure_rate_per_h 冲突")
    source = value.get("source", legacy_source)
    source = str(source).strip() if source not in (None, "") else None
    if source and "demo" in source.casefold():
        source = "demo"
    confirmed = _boolean(value.get("confirmed", legacy_confirmed))
    metrics = (mtbf, value.get("mttr_h"), failure_rate, value.get("availability"), value.get("empirical_failure_probability"))
    has_metric = any(item not in (None, "") for item in metrics)
    interpretable = (
        model == "constant_rate_exponential" and (mtbf is not None or failure_rate is not None)
        or mtbf is not None and value.get("mttr_h") not in (None, "")
        or value.get("availability") not in (None, "") and availability_type != "unknown"
        or model == "empirical" and value.get("empirical_failure_probability") not in (None, "")
    )
    status = "passed" if confirmed and interpretable else "pending_confirmation" if has_metric else "missing_data"
    result.update({
        "model": model,
        "mtbf_h": mtbf,
        "mttr_h": _optional_nonnegative(value.get("mttr_h"), f"{field}.mttr_h"),
        "failure_rate_per_h": failure_rate,
        "availability": _optional_probability(value.get("availability"), f"{field}.availability"),
        "availability_type": availability_type,
        "empirical_failure_probability": _optional_probability(value.get("empirical_failure_probability"), f"{field}.empirical_failure_probability"),
        "failure_mode": _optional_text(value.get("failure_mode")),
        "operating_conditions": deepcopy(value.get("operating_conditions")),
        "reference_conditions": deepcopy(value.get("reference_conditions")),
        "confidence": _optional_probability(value.get("confidence"), f"{field}.confidence"),
        "sample_size": _optional_positive_integer(value.get("sample_size"), f"{field}.sample_size"),
        "source": source, "confirmed": confirmed, "status": status,
    })
    return result


def _optional_positive(value, field):
    if value in (None, ""):
        return None
    number = float(value)
    if not isfinite(number) or number <= 0:
        raise ValueError(f"{field} 必须大于零")
    return number


def _optional_nonnegative(value, field):
    if value in (None, ""):
        return None
    number = float(value)
    if not isfinite(number) or number < 0:
        raise ValueError(f"{field} 不得小于零")
    return number


def _optional_probability(value, field):
    number = _optional_nonnegative(value, field)
    if number is not None and number > 1:
        raise ValueError(f"{field} 必须位于 0..1")
    return number


def _optional_positive_integer(value, field):
    if value in (None, ""):
        return None
    number = float(value)
    if not isfinite(number) or number <= 0 or not number.is_integer():
        raise ValueError(f"{field} 必须是正整数")
    return int(number)


def _optional_text(value):
    return None if value in (None, "") else str(value).strip()


def _boolean(value):
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "否", "")
    return bool(value)


def _close(left, right):
    return abs(left - right) <= max(1e-12, 1e-9 * max(abs(left), abs(right)))
