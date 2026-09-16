"""Reference facts that are deliberately isolated from planning inputs."""

from .equipment_catalog import (
    empty_equipment_reference_catalog,
    load_equipment_reference_catalog,
    normalize_equipment_reference_catalog,
)
from .landing_sites import (
    empty_reference_landing_sites,
    load_reference_landing_sites,
    parse_coordinate,
)

__all__ = [
    "empty_equipment_reference_catalog",
    "empty_reference_landing_sites",
    "load_equipment_reference_catalog",
    "load_reference_landing_sites",
    "normalize_equipment_reference_catalog",
    "parse_coordinate",
]
