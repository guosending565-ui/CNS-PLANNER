"""JSON-safe metadata contract for selectable algorithms."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Mapping


@dataclass(frozen=True)
class AlgorithmManifest:
    algorithm_type: str
    algorithm_id: str
    version: str
    name: str
    provider: str
    maturity: str
    description: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    parameter_schema: Mapping[str, Any] = field(default_factory=dict)
    assumptions: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    references: tuple[str, ...] = ()

    def __post_init__(self):
        for name in (
            "algorithm_type", "algorithm_id", "version", "name", "provider",
            "maturity", "description",
        ):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"AlgorithmManifest 缺少 {name}")
        if not isinstance(self.parameter_schema, Mapping):
            raise ValueError("parameter_schema 必须是对象")

    @property
    def key(self):
        return self.algorithm_type, self.algorithm_id, self.version

    def to_dict(self):
        result = asdict(self)
        for name in ("inputs", "outputs", "assumptions", "limitations", "references"):
            result[name] = list(result[name])
        return deepcopy(result)


AlgorithmFactory = Callable[[dict[str, Any]], object]
