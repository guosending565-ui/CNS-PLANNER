"""Layered Risk-Aware Route Planner V1 (production main line).

``scenario/OD route -> explicit AltitudeLayer -> terrain/building feasibility mask ->
MH/T L8 A* -> Risk Framework V2 soft cost -> LayeredRouteCandidate``.

This package is a pure algorithm package: it never imports QGIS/GDAL, never reads a file
and never writes ``operational_routes`` / CNS results.  Real terrain and building facts are
produced at the GIS boundary (``cns_planner.gis.layered_feasibility_adapter``) by reusing
the existing verified FABDEM window sampler and the existing L8 building grid facts.
"""

from .planner import (
    ALGORITHM_ID, ALGORITHM_VERSION, MASK_SCOPE, SEARCH_SEMANTICS,
    LayeredRoutePlannerV1, build_layer_feasibility_mask,
    resolve_lambda_domain_indices,
)

__all__ = [
    "ALGORITHM_ID", "ALGORITHM_VERSION", "MASK_SCOPE", "SEARCH_SEMANTICS",
    "LayeredRoutePlannerV1", "build_layer_feasibility_mask",
    "resolve_lambda_domain_indices",
]
