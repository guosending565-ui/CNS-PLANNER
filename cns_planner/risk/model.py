"""Public contract implemented by replaceable grid-risk models."""

from __future__ import annotations

from typing import Any, Mapping, Protocol, runtime_checkable


@runtime_checkable
class RiskModel(Protocol):
    """Evaluate derived risk attributes without owning grid geometry or workflow state."""

    algorithm_id: str
    algorithm_version: str

    def evaluate(
        self,
        grid: Mapping[str, Any] | None,
        grid_attributes: Mapping[str, Any],
        parameters: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return risk results keyed by the supplied grid IDs."""
        ...
