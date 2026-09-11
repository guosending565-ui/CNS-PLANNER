"""Lightweight physical quantity and unit helpers used at data boundaries."""

from __future__ import annotations

from math import isfinite, pi, sin
from typing import Any, TypedDict


EARTH_RADIUS_M = 6_371_008.8


class QuantityValue(TypedDict, total=False):
    value: float | None
    quantity: str
    unit: str
    source_value: float | None
    source_unit: str | None
    conversion: dict[str, Any] | None
    source: str | dict[str, Any] | None
    confirmed: bool
    status: str


def quantity_value(value, quantity, unit, *, source_value=None, source_unit=None, conversion=None,
                   source=None, confirmed=False, status=None):
    if value is not None and (not isinstance(value, (int, float)) or not isfinite(float(value))):
        raise ValueError("quantity value 必须是有限数值或 null")
    return {
        "value": None if value is None else float(value), "quantity": str(quantity), "unit": str(unit),
        "source_value": source_value, "source_unit": source_unit, "conversion": conversion,
        "source": source, "confirmed": bool(confirmed),
        "status": status or ("passed" if value is not None else "missing_data"),
    }


def geographic_bbox_area_m2(bbox):
    """Spherical WGS84 bbox area; latitude-dependent and deterministic."""
    west, south, east, north = (float(value) for value in bbox)
    if not (-180 <= west <= east <= 180 and -90 <= south <= north <= 90):
        raise ValueError("bbox 必须是有效 WGS84 范围")
    return EARTH_RADIUS_M ** 2 * abs((east - west) * pi / 180.0) * abs(sin(north * pi / 180.0) - sin(south * pi / 180.0))
