"""Replaceable route-planning protocol; V1 remains behavior-compatible."""

from typing import Any, Protocol


class RoutePlanner(Protocol):
    def plan(self, start: Any, end: Any, grid: dict, risk: dict, constraints: dict) -> dict: ...
