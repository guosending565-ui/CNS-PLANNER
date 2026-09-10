"""UI-independent routing contract. Grid generation belongs to GIS services."""
from dataclasses import dataclass, field
from typing import Literal, Mapping, Protocol

Cell = tuple[int, int]


@dataclass(frozen=True)
class RouteRequest:
    route_id: str
    start: Cell
    goal: Cell
    width: int
    height: int
    cell_size_m: float
    blocked: frozenset[Cell]
    risk: Mapping[Cell, float]
    analysis_crs: str
    origin_xy_m: tuple[float, float]
    input_fingerprint: str
    config: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class RouteResult:
    route_id: str
    status: Literal["success", "no_path", "invalid_input", "not_implemented"]
    path: tuple[Cell, ...] = ()
    reason: str = ""
    algorithm_id: str = ""
    algorithm_version: str = ""
    input_fingerprint: str = ""


class RouteAlgorithm(Protocol):
    def plan(self, request: RouteRequest) -> RouteResult: ...


class AlgorithmRegistry:
    def __init__(self):
        self._algorithms: dict[str, RouteAlgorithm] = {}

    def register(self, key: str, algorithm: RouteAlgorithm):
        if not key or key in self._algorithms:
            raise ValueError("算法 ID 为空或重复")
        self._algorithms[key] = algorithm

    def get(self, key: str) -> RouteAlgorithm:
        if key not in self._algorithms:
            raise ValueError(f"算法尚未注册：{key}")
        return self._algorithms[key]

    def keys(self) -> tuple[str, ...]:
        return tuple(self._algorithms)
