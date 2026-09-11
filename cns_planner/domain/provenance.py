"""Small JSON-safe provenance/source-profile contract without external PROV dependencies."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal, TypedDict


SourceType = Literal["real", "synthetic", "manual"]
SOURCE_TYPES = ("real", "synthetic", "manual")


class SourceProfile(TypedDict, total=False):
    source_id: str
    name: str
    source_type: SourceType
    source_mode: SourceType
    version: str
    quantity: str
    unit: str
    resolution: dict[str, Any]
    crs: dict[str, Any]
    verification: dict[str, Any]
    provenance: dict[str, Any]


def source_profile(value):
    if not isinstance(value, dict):
        raise ValueError("source profile 必须是对象")
    result = deepcopy(value)
    source_type = result.get("source_type") or result.get("source_mode")
    if source_type not in SOURCE_TYPES:
        raise ValueError("source_type 必须是 real/synthetic/manual")
    result["source_type"] = source_type
    result["source_mode"] = source_type
    for field in ("source_id", "name", "version", "quantity", "unit"):
        if not str(result.get(field) or "").strip():
            raise ValueError(f"source profile 缺少 {field}")
    result.setdefault("resolution", {})
    result.setdefault("crs", {})
    result.setdefault("verification", {"status": "unverified"})
    result.setdefault("provenance", {})
    return result
