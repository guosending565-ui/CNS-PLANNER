"""Future replaceable C/N/S site-planning contract."""

from typing import Any, Protocol


class CNSSitePlanner(Protocol):
    def plan(self, route: Any, required_cns: dict, candidate_sites: list, device_catalog: dict, parameters: dict) -> dict: ...
