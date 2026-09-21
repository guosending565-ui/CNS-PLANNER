"""Reference facts that are deliberately isolated from planning inputs."""

from .equipment_catalog import (
    empty_equipment_reference_catalog,
    load_equipment_reference_catalog,
    normalize_equipment_reference_catalog,
)
from .landing_sites import (
    backfill_reference_landing_sites,
    empty_reference_landing_sites,
    load_reference_landing_sites,
    parse_coordinate,
    reference_crs_status,
)
from .routes import (
    backfill_reference_routes,
    declared_source_crs,
    empty_reference_routes,
    load_reference_routes,
)

__all__ = [
    "backfill_reference_landing_sites",
    "backfill_reference_routes",
    "declared_source_crs",
    "empty_equipment_reference_catalog",
    "empty_reference_landing_sites",
    "load_equipment_reference_catalog",
    "load_reference_landing_sites",
    "normalize_equipment_reference_catalog",
    "parse_coordinate",
    "reference_crs_status",
    "empty_reference_routes",
    "load_reference_routes",
]
