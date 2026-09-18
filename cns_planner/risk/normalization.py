"""Relative-scaling normalization helpers for Risk Framework V2.

These helpers only rescale canonical quantities onto a relative ``[0, 1]``
engineering index.  They deliberately do **not** define any safety threshold,
and they never turn a missing/unknown input into zero: a missing value keeps
``normalized_index = None``.

The dataset-quantile reference is the same relative-scaling method already used
by ``RiskModelV1`` (``population_reference`` / ``terrain_relief_reference``);
V2 records the method, the resolved reference value and its fingerprint so the
scaling stays auditable.
"""

from __future__ import annotations

import math
from numbers import Real

from ..domain.risk_v2 import stable_fingerprint

REFERENCE_QUANTILE = 0.95
LOG1P_QUANTILE_METHOD = "log1p_ratio_to_dataset_quantile"
RATIO_QUANTILE_METHOD = "ratio_to_dataset_quantile"
IDENTITY_METHOD = "identity_ratio_0_1"
CANONICAL_FIELD_METHOD = "canonical_normalized_field"

RELATIVE_SCALING_SEMANTICS = "relative_scaling_only_not_a_safety_threshold"


def finite(value):
    return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(float(value))


def clip(value):
    return max(0.0, min(1.0, float(value)))


def dataset_quantile(values, quantile=REFERENCE_QUANTILE):
    clean = sorted(float(value) for value in values if finite(value))
    if not clean:
        return None
    q = clip(float(quantile)) if finite(quantile) else REFERENCE_QUANTILE
    position = (len(clean) - 1) * q
    lower, upper = math.floor(position), math.ceil(position)
    ratio = position - lower
    return clean[lower] + (clean[upper] - clean[lower]) * ratio


def dataset_reference(values, *, source_field, quantile=REFERENCE_QUANTILE, resolved_value=None):
    """Build the auditable reference record (method + value + fingerprint)."""

    resolved = float(resolved_value) if finite(resolved_value) else dataset_quantile(values, quantile)
    record = {
        "mode": "dataset_quantile",
        "quantile": float(quantile),
        "value": resolved,
        "resolved_value": resolved,
        "source_field": source_field,
        "sample_count": len([value for value in values if finite(value)]),
    }
    record["fingerprint"] = stable_fingerprint(record, prefix="riskrefv2-")
    return record


def normalization_record(method, reference, *, reason=None):
    return {
        "method": method,
        "reference": reference,
        "reference_fingerprint": (reference or {}).get("fingerprint"),
        "output_range": [0.0, 1.0],
        "semantics": RELATIVE_SCALING_SEMANTICS,
        "reason": reason,
    }


def log1p_quantile_index(value, reference):
    if not finite(value) or not finite(reference) or float(reference) <= 0:
        return None
    return clip(math.log1p(max(float(value), 0.0)) / math.log1p(float(reference)))


def ratio_quantile_index(value, reference):
    if not finite(value) or not finite(reference) or float(reference) <= 0:
        return None
    return clip(float(value) / float(reference))


def identity_ratio_index(value):
    if not finite(value):
        return None
    return clip(float(value))


__all__ = [
    "CANONICAL_FIELD_METHOD", "IDENTITY_METHOD", "LOG1P_QUANTILE_METHOD",
    "RATIO_QUANTILE_METHOD", "REFERENCE_QUANTILE", "RELATIVE_SCALING_SEMANTICS",
    "clip", "dataset_quantile", "dataset_reference", "finite",
    "identity_ratio_index", "log1p_quantile_index", "normalization_record",
    "ratio_quantile_index",
]
