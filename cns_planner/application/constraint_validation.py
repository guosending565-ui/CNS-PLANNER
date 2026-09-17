"""Fail-closed validation for planning hard constraints (Application input boundary).

The planners consume hard constraints as ``{"bbox": [west, south, east, north]}``
in WGS84 degrees.  A malformed entry must never reach a planner and must never be
silently treated as "no constraint": the planners would either raise a bare
``KeyError``/``TypeError`` or, worse, skip the entry and return a route that looks
valid while ignoring a restricted area.

This module owns that judgement.  It deliberately does not reinterpret, repair or
reproject constraints, and it never converts an invalid constraint into an empty
one.  Callers (Application services) surface :class:`ValueError` to the API layer,
which already turns it into an explicit, actionable error response.
"""

from __future__ import annotations

from collections.abc import Mapping
import math

__all__ = ["validate_hard_constraints"]

_ACTIONABLE = (
    "请在数据源中修正该图层的范围，或从硬约束候选图层中移除后再生成运行航路。"
)


def validate_hard_constraints(constraints, *, field: str = "hard_constraints") -> list[dict]:
    """Return a normalized copy of ``constraints`` or raise ``ValueError``.

    Every entry must be a mapping carrying ``bbox`` with exactly four finite
    numbers in ``[west, south, east, north]`` order and strict ``west < east`` /
    ``south < north``.  Non-numeric, non-finite, degenerate and inverted boxes are
    rejected with an index-bearing, actionable message.
    """

    if constraints is None:
        return []
    if isinstance(constraints, (str, bytes)) or not isinstance(constraints, (list, tuple)):
        raise ValueError(
            f"{field} 必须是硬约束列表，实际得到 {type(constraints).__name__}；"
            "禁止把非法输入当作无约束继续规划。" + _ACTIONABLE
        )

    normalized: list[dict] = []
    for index, item in enumerate(constraints):
        label = f"{field}[{index}]"
        if not isinstance(item, Mapping):
            raise ValueError(
                f"{label} 必须是包含 bbox 的对象，实际得到 {type(item).__name__}；"
                "禁止把非法输入当作无约束继续规划。" + _ACTIONABLE
            )
        name = str(item.get("name") or "").strip()
        display = f"{label}（{name}）" if name else label
        bbox = item.get("bbox")
        if isinstance(bbox, (str, bytes)) or not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            raise ValueError(
                f"{display} 的 bbox 必须是 [west, south, east, north] 四项数组；"
                "禁止把非法输入当作无约束继续规划。" + _ACTIONABLE
            )
        values = []
        for axis, raw in zip(("west", "south", "east", "north"), bbox):
            if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(float(raw)):
                raise ValueError(
                    f"{display} 的 bbox {axis} 必须是有限数值，实际得到 {raw!r}；"
                    "禁止把非法输入当作无约束继续规划。" + _ACTIONABLE
                )
            values.append(float(raw))
        west, south, east, north = values
        if west >= east:
            raise ValueError(
                f"{display} 的 bbox 经度范围无效（west={west} 必须小于 east={east}）；"
                "禁止把非法输入当作无约束继续规划。" + _ACTIONABLE
            )
        if south >= north:
            raise ValueError(
                f"{display} 的 bbox 纬度范围无效（south={south} 必须小于 north={north}）；"
                "禁止把非法输入当作无约束继续规划。" + _ACTIONABLE
            )
        entry = dict(item)
        entry["bbox"] = values
        normalized.append(entry)
    return normalized


def is_well_formed_constraint(item) -> bool:
    """Non-raising predicate used by review/reporting paths (never by planning)."""

    try:
        validate_hard_constraints([item])
    except ValueError:
        return False
    return True
