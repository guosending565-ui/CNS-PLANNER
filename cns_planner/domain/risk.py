"""Risk state aliases kept independent from source attributes."""

from typing import Any, TypeAlias

GridRisk: TypeAlias = dict[str, Any]
GridAttributes: TypeAlias = dict[str, dict[str, Any]]
