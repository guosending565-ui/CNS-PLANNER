"""Layered planners for the MH/T L8 strategic grid.

Two registered implementations of the ``layered_route_planner`` algorithm type live here:

* ``LayeredRiskAwareThetaStarV2`` (``theta_star_v2``) — the **production layered planning
  baseline** and the project default;
* ``LayeredRoutePlannerV1`` (``planner``) — the legacy/baseline layered planner, kept
  registered, tested and explicitly selectable, never silently migrated to V2.

Both pipelines are pure algorithm packages: they never import QGIS/GDAL, never read a file
and never write ``operational_routes`` / CNS results.  Real terrain and building facts are
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
