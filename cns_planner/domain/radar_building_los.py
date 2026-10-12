"""Pure 2.5D building line-of-sight geometry for read-only Radar diagnostics.

D2-C0 changes (isolated pre-integration only, not production):

* The building ray reuses the **frozen** Radar device geometry gate
  (:func:`frozen_geometry_gate`) instead of a bare maximum-slant-range check.  A
  combination that the device minimum/maximum slant range or the elevation interval
  already excludes never queries buildings, never opens DEM and is never counted as
  building-LOS physical ``unknown``.
* The input fingerprint only carries **semantic input identity**; run receipts
  (elapsed seconds, cache hits, returned counts, ordering, performance) live in a
  separate execution identity (:func:`building_los_execution_identity`).
* The first building hit is the first *trusted entry* along ``origin -> probe``,
  resolved with a bounded root find over the shared ray-height model.
"""

from __future__ import annotations

import math
from copy import deepcopy

from .building_clearance import (
    building_roof_elevation,
    normalize_height_evidence_level,
)
from .building_geometry_quality import prepare_polygonal_geometry
from .radar_surveillance_layout import (
    RADAR_GEOMETRY_PARAMETERS,
    RADAR_TYPES,
    radar_geometry_parameters,
)
from .radar_terrain_los import (
    EGM2008_ORTHOMETRIC,
    FROZEN_RADAR_GEOMETRY_VERSION,
    LOS_STATUS_BLOCKED,
    LOS_STATUS_CLEAR,
    LOS_STATUS_UNKNOWN,
    curvature_radius_scale,
    effective_curvature_radius_m,
    number_or_none,
    probe_geometry,
    ray_height_m,
    stable_fingerprint,
)
# The pure-metric geometry helpers of the frozen Radar layout are the single source of
# the slant-range / elevation semantics; importing them here (instead of copying the
# formulas) keeps the building gate bit-identical to the frozen device evaluation.
from ..algorithms.radar_layout.geometry import geometry_evaluation  # noqa: F401

RADAR_BUILDING_LOS_ALGORITHM_VERSION = "radar_building_los_v1_2_5d_prism"
#: v2 stabilised the input fingerprint: frozen geometry gate identity is bound in and
#: run receipts moved out to the execution identity.  The 2.5D prism physics did not
#: change, so the algorithm version stays v1.
RADAR_BUILDING_LOS_FINGERPRINT_VERSION = "radar_building_los_input_fingerprint_v2"
RADAR_BUILDING_LOS_EXECUTION_FINGERPRINT_VERSION = (
    "radar_building_los_execution_fingerprint_v1"
)
RADAR_BUILDING_LOS_GEOMETRY_GATE_VERSION = "radar_building_los_frozen_geometry_gate_v1"
RAY_MODEL_ID = "radar_los_shared_ray_height_v1"
HEIGHT_EVIDENCE_POLICY_ID = "radar_building_los_height_evidence_policy_v1"
#: Device is inside the frozen equipment geometry; a building LOS verdict is allowed.
BUILDING_LOS_GATE_WITHIN_DEVICE = "within_frozen_device_geometry"
#: Slant range (or elevation) excludes the ray; reported as its own filtered status.  The
#: two filtered spellings are the frozen lookup vocabulary, so candidates, MILP coverage
#: sets, ``actual_site_coverage`` and the 5 m validation all read one status value.
BUILDING_LOS_GATE_OUTSIDE_EQUIPMENT_RANGE = "out_of_equipment_range"
BUILDING_LOS_GATE_OUTSIDE_ELEVATION_INTERVAL = "geometry_filtered"
BUILDING_LOS_GATE_GEOMETRY_UNRESOLVED = "geometry_inputs_incomplete"
GATE_OUTSIDE_STATUS = BUILDING_LOS_GATE_OUTSIDE_EQUIPMENT_RANGE
GATE_GEOMETRY_FILTERED_STATUS = BUILDING_LOS_GATE_OUTSIDE_ELEVATION_INTERVAL
GATE_UNRESOLVED_STATUS = LOS_STATUS_UNKNOWN
CONFIRMED_HEIGHT_EVIDENCE_LEVELS = frozenset({"verified", "source_tagged", "derived"})
_FRACTION_TOLERANCE = 1e-9
_HEIGHT_TOLERANCE_M = 1e-6
_ROOT_FUNCTION_FLAT_TOLERANCE = 1e-12
_ROOT_FUNCTION_ZERO_TOLERANCE = 1e-9
_QUERY_POLICY_KEYS = (
    "ray_bbox_metric", "max_features", "query_mode", "boundary_contact",
)


# ------------------------------------------------------------------ semantic identity


def _canonical_query_policy(query_policy):
    """Keep the explicit computation parameters; drop every run receipt field."""

    if not isinstance(query_policy, dict):
        return query_policy
    return {
        key: deepcopy(query_policy[key]) for key in _QUERY_POLICY_KEYS
        if key in query_policy
    }


def _semantic_query_policy(*, query_policy, query_receipt):
    """Canonical computation parameters of one ray query.

    ``query_receipt`` is the **run receipt** of a concrete execution: it carries
    elapsed seconds, cache hits, returned counts, candidate counts and query ordering.
    Those never belong to the semantic input identity.  Only the explicit computation
    parameters that change which buildings the frozen policy returns are kept, so two
    equivalent read-only runs stay semantically identical.
    """

    if isinstance(query_policy, dict):
        return _canonical_query_policy(query_policy)
    receipt = query_receipt if isinstance(query_receipt, dict) else {}
    policy = {}
    bbox = receipt.get("ray_bbox_metric")
    if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        policy["ray_bbox_metric"] = [number_or_none(item) for item in bbox]
    max_features = receipt.get("max_features")
    if isinstance(max_features, int) and not isinstance(max_features, bool):
        policy["max_features"] = max_features
    for key in ("query_mode", "boundary_contact"):
        if key in receipt:
            policy[key] = deepcopy(receipt[key])
    return policy


def building_los_input_fingerprint(
    *, origin_metric, target_metric, origin_egm2008_m, target_egm2008_m,
    building_source_identity, dem_source_identity=None, earth_radius_m=None,
    k_factor=1.0, query_policy=None, geometry_policy=None, route_probe_identity=None,
    radar_geometry=None, query_receipt=None,
):
    payload = {
        "fingerprint_schema_version": RADAR_BUILDING_LOS_FINGERPRINT_VERSION,
        "algorithm_version": RADAR_BUILDING_LOS_ALGORITHM_VERSION,
        "geometry_gate_version": RADAR_BUILDING_LOS_GEOMETRY_GATE_VERSION,
        "route_probe_identity": deepcopy(route_probe_identity),
        "origin": {"metric": origin_metric, "egm2008_m": number_or_none(origin_egm2008_m)},
        "target": {"metric": target_metric, "egm2008_m": number_or_none(target_egm2008_m)},
        "radar_geometry": deepcopy(radar_geometry),
        "horizontal_crs": "explicit_local_metric_crs",
        "vertical_crs": EGM2008_ORTHOMETRIC,
        "building_source_identity": deepcopy(building_source_identity),
        "dem_source_identity": deepcopy(dem_source_identity),
        "height_evidence_policy": {
            "id": HEIGHT_EVIDENCE_POLICY_ID,
            "confirmed_levels": sorted(CONFIRMED_HEIGHT_EVIDENCE_LEVELS),
            "derived_requires_assumption": True,
            "predicted_unknown_are_estimates_only": True,
        },
        "footprint_geometry_policy": geometry_policy or {
            "polygon_and_multipolygon": True, "holes_preserved": True,
            "make_valid_in_memory_only": True, "boundary_contact": "unknown",
        },
        "ray_model": {
            "id": RAY_MODEL_ID, "earth_radius_m": number_or_none(earth_radius_m),
            "k_factor": curvature_radius_scale(k_factor), "endpoints_preserved": True,
        },
        "query_policy": _semantic_query_policy(
            query_policy=query_policy, query_receipt=query_receipt,
        ),
    }
    return stable_fingerprint(payload)


def building_los_execution_identity(
    *, query_receipt=None, query_complete=None, query_reason=None,
    budget=None, computation_completion=None, evidence_completion=None,
    input_fingerprint=None,
):
    """Read-only run receipt of one building LOS execution.

    Budget exhaustion and truncation stay distinguishable here even though two
    equivalent semantic inputs share one input fingerprint.
    """

    receipt = deepcopy(query_receipt) if isinstance(query_receipt, dict) else {}
    return {
        "execution_fingerprint_version": RADAR_BUILDING_LOS_EXECUTION_FINGERPRINT_VERSION,
        "algorithm_version": RADAR_BUILDING_LOS_ALGORITHM_VERSION,
        "input_fingerprint": input_fingerprint,
        "query_complete": (
            bool(query_complete) if query_complete is not None else None
        ),
        "query_reason": query_reason,
        "truncated": bool(receipt.get("truncated")) if "truncated" in receipt else None,
        "budget": deepcopy(budget) if isinstance(budget, dict) else deepcopy(budget),
        "computation_completion": computation_completion,
        "evidence_completion": evidence_completion,
        "run_receipt": receipt,
        "elapsed_seconds_is_receipt_only": True,
        "elapsed_seconds": number_or_none(receipt.get("elapsed_seconds")),
    }


# ------------------------------------------------------------- frozen device geometry


def _frozen_geometry_identity(radar_type, parameters):
    """Identity of the frozen device geometry actually used by this ray."""

    return {
        "radar_type": parameters["radar_type"],
        "label": parameters["label"],
        "parameters": deepcopy(parameters),
        "device_source": parameters.get("source"),
        "device_confirmed": parameters.get("confirmed"),
        "parameter_origin": parameters.get("parameter_origin"),
        "frozen_geometry_version": FROZEN_RADAR_GEOMETRY_VERSION,
        "gate_version": RADAR_BUILDING_LOS_GEOMETRY_GATE_VERSION,
        "geometry_table_types": list(RADAR_TYPES),
        "parameters_copied_locally": False,
    }


def _finite_metric(value):
    if not (isinstance(value, (list, tuple)) and len(value) >= 2):
        return None
    x, y = number_or_none(value[0]), number_or_none(value[1])
    if x is None or y is None:
        return None
    return [x, y]


def frozen_device_geometry(radar_type):
    """Identity of one device's frozen geometry table entry (no local parameter copy)."""

    if radar_type in (None, ""):
        return None
    return _frozen_geometry_identity(radar_type, radar_geometry_parameters(radar_type))


def _geometry_values(*, origin_metric, target_metric, origin_egm2008_m,
                     target_egm2008_m, geometry):
    required = (
        "horizontal_distance_m", "vertical_delta_m", "slant_distance_m",
        "elevation_deg", "bearing_deg",
    )
    if isinstance(geometry, dict):
        if all(number_or_none(geometry.get(key)) is not None for key in required):
            return {key: float(geometry[key]) for key in required}
        return None
    points = (_finite_metric(origin_metric), _finite_metric(target_metric))
    origin_z = number_or_none(origin_egm2008_m)
    target_z = number_or_none(target_egm2008_m)
    if None in points or origin_z is None or target_z is None:
        return None
    values = geometry_evaluation(
        origin_egm2008_m=origin_z, sample_egm2008_m=target_z,
        sample_metric=points[1], tower_metric=points[0],
    )
    if not all(number_or_none(values.get(key)) is not None for key in required):
        return None
    return {key: float(values[key]) for key in required}


def classify_frozen_device_geometry(
    *, radar_type, origin_metric=None, target_metric=None, origin_egm2008_m=None,
    target_egm2008_m=None, geometry=None,
):
    """Shared decision core of the frozen device geometry gate.

    ``TerrainLosLookup`` and the building LOS adapter both consume this single
    classification, so a ray can never be out of equipment range for one domain and
    inside it for the other.
    """

    if radar_type in (None, ""):
        return {
            "status": BUILDING_LOS_GATE_GEOMETRY_UNRESOLVED,
            "reason": "radar_type_missing_for_frozen_geometry_gate",
            "geometry": None, "device_geometry": None,
            "horizontal_distance_m": None, "vertical_delta_m": None,
            "slant_distance_m": None, "elevation_deg": None,
            "within_equipment_range": None, "within_device_geometry": False,
        }
    try:
        parameters = radar_geometry_parameters(radar_type)
    except ValueError as error:
        return {
            "status": BUILDING_LOS_GATE_GEOMETRY_UNRESOLVED,
            "reason": "unsupported_radar_type_for_frozen_geometry_gate",
            "geometry": None,
            "device_geometry": {
                "radar_type": str(radar_type),
                "gate_version": RADAR_BUILDING_LOS_GEOMETRY_GATE_VERSION,
                "message": str(error), "known_radar_types": list(RADAR_TYPES),
                "parameters_copied_locally": False,
            },
            "horizontal_distance_m": None, "vertical_delta_m": None,
            "slant_distance_m": None, "elevation_deg": None,
            "within_equipment_range": None, "within_device_geometry": False,
        }
    identity = _frozen_geometry_identity(radar_type, parameters)
    values = _geometry_values(
        origin_metric=origin_metric, target_metric=target_metric,
        origin_egm2008_m=origin_egm2008_m, target_egm2008_m=target_egm2008_m,
        geometry=geometry,
    )
    common = {
        "geometry": values, "device_geometry": identity,
        "horizontal_distance_m": None if values is None else values["horizontal_distance_m"],
        "vertical_delta_m": None if values is None else values["vertical_delta_m"],
        "slant_distance_m": None if values is None else values["slant_distance_m"],
        "elevation_deg": None if values is None else values["elevation_deg"],
        "within_equipment_range": None,
        "within_device_geometry": False,
    }
    if values is None:
        # Invalid origin (non-finite or missing metric/height) or invalid probe: fail
        # closed instead of silently declaring the ray device-reachable.
        return {
            **common, "status": BUILDING_LOS_GATE_GEOMETRY_UNRESOLVED,
            "reason": "origin_or_probe_geometry_invalid",
        }
    within_range = (
        float(parameters["min_slant_range_m"])
        <= values["slant_distance_m"]
        <= float(parameters["max_slant_range_m"])
    )
    common["within_equipment_range"] = within_range
    if not within_range:
        return {
            **common, "status": BUILDING_LOS_GATE_OUTSIDE_EQUIPMENT_RANGE,
            "reason": "outside_inclusive_slant_range",
        }
    if not (
        float(parameters["elevation_min_deg"])
        <= values["elevation_deg"]
        <= float(parameters["elevation_max_deg"])
    ):
        return {
            **common, "status": BUILDING_LOS_GATE_OUTSIDE_ELEVATION_INTERVAL,
            "reason": "outside_elevation_interval",
        }
    return {
        **common, "status": BUILDING_LOS_GATE_WITHIN_DEVICE, "reason": None,
        "within_equipment_range": True, "within_device_geometry": True,
    }


def frozen_geometry_gate(
    *, radar_type, origin_metric, target_metric, origin_egm2008_m, target_egm2008_m,
    device_geometry=None, geometry=None,
):
    """Apply the frozen device geometry gate of one radar type to one ray.

    The gate reads the device table through :func:`radar_geometry_parameters`; it never
    copies or relaxes a range/pitch parameter.  ``within_frozen_device_range`` is true
    only when the device type is known **and** the ray passes both the inclusive slant
    range and the elevation interval.
    """

    classified = classify_frozen_device_geometry(
        radar_type=radar_type, origin_metric=origin_metric, target_metric=target_metric,
        origin_egm2008_m=origin_egm2008_m, target_egm2008_m=target_egm2008_m,
        geometry=geometry,
    )
    identity = classified["device_geometry"]
    if device_geometry is not None and identity is not None:
        identity = {**identity, "caller_device_geometry": deepcopy(device_geometry)}
    parameters = None if identity is None else identity.get("parameters")
    values = classified["geometry"]
    return {
        "gate_version": RADAR_BUILDING_LOS_GEOMETRY_GATE_VERSION,
        "radar_type": None if radar_type in (None, "") else str(radar_type),
        "device_geometry": identity,
        "status": classified["status"],
        "reason": classified["reason"],
        "geometry": values,
        "horizontal_distance_m": classified["horizontal_distance_m"],
        "vertical_delta_m": classified["vertical_delta_m"],
        "slant_distance_m": classified["slant_distance_m"],
        "elevation_deg": classified["elevation_deg"],
        "bearing_deg": None if values is None else values["bearing_deg"],
        "device_geometry_resolved": classified["status"] != BUILDING_LOS_GATE_GEOMETRY_UNRESOLVED,
        "within_frozen_device_range": classified["within_device_geometry"],
        "within_equipment_range": classified["within_equipment_range"],
        "evaluated_slant_range_m": None if values is None else values["slant_distance_m"],
        "evaluated_elevation_deg": None if values is None else values["elevation_deg"],
        "frozen_parameters_modified": False,
        "parameters_copied_locally": False,
        "parameters_resolved": parameters is not None,
    }


# ------------------------------------------------------------------- first prism hit


def _segments(geometry):
    if geometry is None or geometry.is_empty:
        return []
    if geometry.geom_type == "LineString":
        return [geometry]
    if geometry.geom_type == "MultiLineString":
        return list(geometry.geoms)
    if geometry.geom_type == "GeometryCollection":
        answer = []
        for item in geometry.geoms:
            answer.extend(_segments(item))
        return answer
    return []


def _point_contacts(geometry):
    if geometry is None or geometry.is_empty:
        return []
    if geometry.geom_type == "Point":
        return [geometry]
    if geometry.geom_type == "MultiPoint":
        return list(geometry.geoms)
    if geometry.geom_type == "GeometryCollection":
        answer = []
        for item in geometry.geoms:
            answer.extend(_point_contacts(item))
        return answer
    return []


def _ray_height(*, origin_m, target_m, horizontal_m, fraction, earth_radius_m, k_factor):
    return ray_height_m(
        origin_egm2008_m=origin_m, target_egm2008_m=target_m, horizontal_m=horizontal_m,
        sample_distance_m=horizontal_m * fraction,
        earth_radius_m=earth_radius_m, k_factor=k_factor,
    )


def _in_prism_range(z, ground_m, roof_m, tolerance):
    return ground_m - tolerance <= z <= roof_m + tolerance


def _bounded_ray_height_root(
    *, origin_m, target_m, horizontal_m, left, right, bound_m,
    earth_radius_m, k_factor, tolerance,
):
    """Earliest fraction in ``[left, right]`` where the shared ray height equals a plane.

    The height difference to a horizontal plane is linear in the ray fraction when no
    curvature is applied and convex when it is, so the interval can be split at its
    stationary point and solved monotonically.  No second height model is introduced.
    """

    def height(fraction):
        return _ray_height(
            origin_m=origin_m, target_m=target_m, horizontal_m=horizontal_m,
            fraction=fraction, earth_radius_m=earth_radius_m, k_factor=k_factor,
        )

    right_z = height(right)
    if right_z is None:
        return None
    values = [height(left), right_z]
    radius = effective_curvature_radius_m(earth_radius_m, k_factor)
    pieces = [(left, right)]
    if radius > 0.0:
        quadratic = horizontal_m * horizontal_m / (2.0 * radius)
        if quadratic > 0.0:
            vertex = (quadratic - (target_m - origin_m)) / (2.0 * quadratic)
            if left < vertex < right:
                vertex_z = height(vertex)
                if vertex_z is not None:
                    values.append(vertex_z)
                    pieces = [(left, vertex), (vertex, right)]
    finite = [value for value in values if value is not None]
    if not finite or min(finite) > bound_m + tolerance or max(finite) < bound_m - tolerance:
        return None
    entries = []
    for piece_left, piece_right in pieces:
        piece_left_z = height(piece_left)
        piece_right_z = height(piece_right)
        if piece_left_z is None or piece_right_z is None:
            continue
        left_delta = piece_left_z - bound_m
        right_delta = piece_right_z - bound_m
        if left_delta == 0.0:
            # A piece edge that sits exactly on the plane is a tangent contact, not a
            # crossing, when the piece retreats to the same side of the plane.
            if right_delta > 0.0:
                continue
            entries.append(piece_left)
            continue
        if right_delta == 0.0:
            entries.append(piece_right)
            continue
        if left_delta * right_delta > 0.0:
            # Same side on both ends: monotone (or convex with both ends positive),
            # therefore no crossing inside this piece.
            continue
        low, high = piece_left, piece_right
        low_delta = left_delta
        for _ in range(64):
            middle = (low + high) / 2.0
            middle_z = height(middle)
            if middle_z is None:
                break
            middle_delta = middle_z - bound_m
            if low_delta * middle_delta > 0.0:
                low, low_delta = middle, middle_delta
            else:
                high = middle
        entries.append((low + high) / 2.0)
    if not entries:
        return None
    return min(entries)


def _first_prism_hit(*, start_fraction, end_fraction, origin_m, target_m,
                     horizontal_m, ground_m, roof_m, earth_radius_m, k_factor):
    """First trusted entry of the shared ray into one 2.5D building prism.

    ``start_fraction`` / ``end_fraction`` bound the horizontal (xy) intersection of the
    ray with one footprint part.  The returned fraction is the earliest position along
    ``origin -> probe`` where the shared ray height first lies inside the vertical
    interval ``[ground, roof]`` of that part, and the returned height is the shared ray
    height exactly at that fraction.  Entering from above the roof, entering from below
    the ground, entering/leaving inside the interval, a tangent contact and an origin
    that already sits on the surface or inside the prism are all resolved here.
    """

    if end_fraction <= start_fraction:
        return None
    start = 0.0 if start_fraction <= _FRACTION_TOLERANCE else start_fraction

    def height(fraction):
        return _ray_height(
            origin_m=origin_m, target_m=target_m, horizontal_m=horizontal_m,
            fraction=fraction, earth_radius_m=earth_radius_m, k_factor=k_factor,
        )

    grid = {start, end_fraction}
    radius = effective_curvature_radius_m(earth_radius_m, k_factor)
    if radius > 0.0:
        quadratic = horizontal_m * horizontal_m / (2.0 * radius)
        if quadratic > 0.0:
            vertex = (quadratic - (target_m - origin_m)) / (2.0 * quadratic)
            if start < vertex < end_fraction:
                grid.add(vertex)
    bounds = sorted(grid)
    candidates = []

    def consider(fraction):
        if fraction is None:
            return
        if fraction < start - _FRACTION_TOLERANCE or fraction > end_fraction + _FRACTION_TOLERANCE:
            return
        # Endpoints of a root-find piece are exact by construction, so a small clamp
        # keeps the returned fraction inside the evaluated xy interval.
        bound = min(max(fraction, start), end_fraction)
        value = height(bound)
        if value is None:
            return
        if _in_prism_range(value, ground_m, roof_m, _HEIGHT_TOLERANCE_M):
            candidates.append((bound, value))

    def enters_immediately(edge_z):
        # The ray leaves a horizontal prism face into the prism body right after the
        # edge, and only the face this edge actually sits on counts.  Comparing the
        # distances to the two faces keeps a roof contact from being read as a ground
        # departure when the two tolerances overlap.
        to_ground = abs(edge_z - ground_m)
        to_roof = abs(edge_z - roof_m)
        if to_ground <= _HEIGHT_TOLERANCE_M and to_ground < to_roof:
            return target_m > edge_z
        if to_roof <= _HEIGHT_TOLERANCE_M and to_roof < to_ground:
            return target_m < edge_z
        return False

    start_z = height(start)
    if start_z is not None and (
        ground_m < start_z < roof_m
        or _in_prism_range(start_z, ground_m, roof_m, _HEIGHT_TOLERANCE_M)
        and (start > 0.0 or enters_immediately(start_z))
    ):
        # The origin is strictly inside the prism, or the evaluated interval starts
        # inside it because the xy intersection begins at the interval boundary: the
        # first entry is then the start itself.  An origin that merely touches a
        # horizontal prism face is a boundary contact, so it only counts when the ray
        # immediately leaves that face into the prism body.
        consider(start)

    for left, right in zip(bounds, bounds[1:]):
        left_z = height(left)
        right_z = height(right)
        if left_z is None or right_z is None:
            continue
        # A grid point strictly inside the band is an entry; a grid point only touching
        # a face is left to the root search so the reported point stays the entry.
        if _in_prism_range(left_z, ground_m, roof_m, 0.0) and not (
            left <= _FRACTION_TOLERANCE and not enters_immediately(left_z)
        ):
            consider(left)
        elif left_z > roof_m and right_z > roof_m:
            # Both above the roof: a convex height profile only descends below its
            # endpoints at an interior stationary point, which is already in the grid.
            continue
        elif left_z < ground_m and right_z < ground_m:
            continue
        for plane in (roof_m, ground_m):
            crossing = _bounded_ray_height_root(
                origin_m=origin_m, target_m=target_m, horizontal_m=horizontal_m,
                left=left, right=right, bound_m=plane,
                earth_radius_m=earth_radius_m, k_factor=k_factor,
                tolerance=_HEIGHT_TOLERANCE_M,
            )
            if crossing is not None:
                consider(crossing)

    if not candidates:
        return None
    return min(candidates, key=lambda item: item[0])


def _prism_hit_derivation():
    return {
        "derivation": "earliest_fraction_in_frozen_vertical_interval_of_xy_intersection",
        "root_solver": "bounded_bisection_on_shared_ray_height_per_monotone_piece",
        "bisection_iterations": 64,
        "fraction_tolerance": _FRACTION_TOLERANCE,
        "height_tolerance_m": _HEIGHT_TOLERANCE_M,
        "height_model": RAY_MODEL_ID,
        "second_height_model_introduced": False,
        "exact": False,
        "approximate": True,
    }


# ------------------------------------------------------------------------ evaluation


def evaluate_building_los(
    *, origin_metric, target_metric, origin_egm2008_m, target_egm2008_m, buildings,
    query_complete, building_source_identity, dem_source_identity=None,
    query_reason=None, query_receipt=None, query_policy=None, budget=None,
    earth_radius_m=None, k_factor=1.0, route_probe_identity=None, radar_geometry=None,
):
    """Evaluate a shared Radar ray against real 2.5D building prisms."""

    from shapely.geometry import LineString, Point

    geometry = probe_geometry(
        origin_egm2008_m=origin_egm2008_m, target_egm2008_m=target_egm2008_m,
        origin_metric=origin_metric, target_metric=target_metric,
        earth_radius_m=earth_radius_m, k_factor=k_factor,
    )
    horizontal = number_or_none(geometry.get("horizontal_distance_m"))
    origin_z = number_or_none(origin_egm2008_m)
    target_z = number_or_none(target_egm2008_m)
    fingerprint = building_los_input_fingerprint(
        origin_metric=origin_metric, target_metric=target_metric,
        origin_egm2008_m=origin_z, target_egm2008_m=target_z,
        building_source_identity=building_source_identity,
        dem_source_identity=dem_source_identity, earth_radius_m=earth_radius_m,
        k_factor=k_factor, query_policy=query_policy, query_receipt=query_receipt,
        route_probe_identity=route_probe_identity, radar_geometry=radar_geometry,
    )
    base = {
        "algorithm_version": RADAR_BUILDING_LOS_ALGORITHM_VERSION,
        "ray_model_id": RAY_MODEL_ID,
        "height_evidence_policy_id": HEIGHT_EVIDENCE_POLICY_ID,
        "height_evidence_confirmed_levels": sorted(CONFIRMED_HEIGHT_EVIDENCE_LEVELS),
        "vertical_reference": EGM2008_ORTHOMETRIC,
        "probe_geometry": geometry,
        "input_fingerprint": fingerprint,
        "building_source_identity": deepcopy(building_source_identity),
        "dem_source_identity": deepcopy(dem_source_identity),
        "query_complete": bool(query_complete),
        "query_reason": query_reason,
        "query_receipt": deepcopy(query_receipt),
        "query_policy": _semantic_query_policy(
            query_policy=query_policy, query_receipt=query_receipt,
        ),
        "source_geometry_modified": False,
        "is_detection_probability": False,
    }

    def finish(payload, *, computation_completion, evidence_completion):
        return {
            **base,
            **payload,
            "computation_completion": computation_completion,
            "evidence_completion": evidence_completion,
            "execution_identity": building_los_execution_identity(
                query_receipt=query_receipt, query_complete=query_complete,
                query_reason=query_reason, budget=budget,
                computation_completion=computation_completion,
                evidence_completion=evidence_completion,
                input_fingerprint=fingerprint,
            ),
        }

    if horizontal is None or horizontal <= 0.0 or origin_z is None or target_z is None:
        return finish({
            "status": LOS_STATUS_UNKNOWN,
            "engineering_estimate_status": LOS_STATUS_UNKNOWN,
            "reason": "ray_horizontal_geometry_unresolved",
            "candidate_building_count": len(buildings or []), "exact_intersection_count": 0,
            "obstruction_hits": [], "unknown_reasons": ["ray_horizontal_geometry_unresolved"],
            "geometry_quality_counts": {"passed": 0, "repaired": 0, "invalid": 0},
        }, computation_completion="incomplete", evidence_completion="incomplete")

    line = LineString([origin_metric, target_metric])
    quality_counts = {"passed": 0, "repaired": 0, "invalid": 0}
    hits = []
    unknown_reasons = []
    exact_count = 0
    boundary_contact = False
    for footprint in buildings or []:
        prepared = prepare_polygonal_geometry(
            footprint.get("building_id"), footprint.get("geometry_metric"),
        )
        quality_counts[prepared["status"]] += 1
        if prepared["status"] == "invalid":
            unknown_reasons.append("building_footprint_geometry_invalid")
            continue
        estimate = footprint.get("height_estimate_m")
        ground = footprint.get("ground_elevation_egm2008_m")
        if ground is None:
            ground = footprint.get("ground_elevation_max_egm2008_m")
        roof = building_roof_elevation(ground, estimate)
        level = normalize_height_evidence_level(
            footprint.get("height_evidence_level"), height_present=number_or_none(estimate) is not None,
        )
        supports_main_verdict = (
            level in {"verified", "source_tagged"}
            or (level == "derived" and bool(footprint.get("derived_assumption")))
        )
        evidence_classification = (
            "confirmed" if supports_main_verdict
            else "estimated" if level == "predicted" else "unknown"
        )
        if not supports_main_verdict:
            # The candidate itself is only an estimate or has no usable height evidence,
            # so the main verdict cannot claim sufficient evidence for this ray even
            # when the candidate query itself was complete.
            unknown_reasons.append(
                "building_height_evidence_estimated_not_confirmed"
                if level == "predicted" else "building_height_evidence_missing_or_unconfirmed"
            )
        for part_index, part in enumerate(prepared["parts"]):
            intersection = line.intersection(part)
            interior_segments = []
            for segment in _segments(intersection):
                if segment.length <= _FRACTION_TOLERANCE:
                    continue
                midpoint = segment.interpolate(0.5, normalized=True)
                if part.contains(midpoint):
                    interior_segments.append(segment)
                else:
                    boundary_contact = True
            contacts = _point_contacts(intersection)
            if any(line.project(point) / horizontal > _FRACTION_TOLERANCE for point in contacts):
                boundary_contact = True
            for segment in interior_segments:
                # Derive the along-ray interval from the segment length instead of
                # projecting each vertex: projection of a vertex that lies far from the
                # origin accumulates a few ulps and can push the interval end slightly
                # past 1.0, which would fake a tangential roof contact.
                start = max(0.0, line.project(Point(segment.coords[0])) / horizontal)
                end = min(1.0, start + segment.length / horizontal)
                if end - start <= _FRACTION_TOLERANCE:
                    boundary_contact = True
                    continue
                exact_count += 1
                if roof.get("status") != "resolved":
                    unknown_reasons.append(str(roof.get("reason") or "building_prism_unresolved"))
                    continue
                hit = _first_prism_hit(
                    start_fraction=start, end_fraction=end, origin_m=origin_z,
                    target_m=target_z, horizontal_m=horizontal,
                    ground_m=roof["ground_elevation_m"], roof_m=roof["roof_elevation_egm2008_m"],
                    earth_radius_m=earth_radius_m, k_factor=k_factor,
                )
                if hit is None:
                    if evidence_classification != "confirmed":
                        unknown_reasons.append("building_height_evidence_insufficient_for_clear")
                    continue
                fraction, hit_z = hit
                x = float(origin_metric[0]) + (float(target_metric[0]) - float(origin_metric[0])) * fraction
                y = float(origin_metric[1]) + (float(target_metric[1]) - float(origin_metric[1])) * fraction
                hits.append({
                    "type": "building", "building_id": footprint.get("building_id"),
                    "footprint_part_index": part_index, "hit_x": x, "hit_y": y,
                    "hit_z": hit_z, "along_ray_distance_m": horizontal * fraction,
                    "ray_fraction": fraction,
                    "ground_elevation_egm2008_m": roof["ground_elevation_m"],
                    "roof_elevation_egm2008_m": roof["roof_elevation_egm2008_m"],
                    "height_evidence_level": level,
                    "derived_assumption": footprint.get("derived_assumption"),
                    "obstruction_margin_m": roof["roof_elevation_egm2008_m"] - hit_z,
                    "evidence_classification": evidence_classification,
                    "source_version": footprint.get("source_version"),
                    "hit_derivation_status": "exact_xy_polygon_interval_first_entry_bounded_root",
                    "hit_derivation": _prism_hit_derivation(),
                    "horizontal_intersection_fraction": [start, end],
                })
    hits.sort(key=lambda item: (item["ray_fraction"], str(item.get("building_id") or "")))
    confirmed_hits = [item for item in hits if item["evidence_classification"] == "confirmed"]
    estimated_hits = [item for item in hits if item["evidence_classification"] != "confirmed"]
    if confirmed_hits:
        status, reason = LOS_STATUS_BLOCKED, "confirmed_building_prism_intersection"
    elif not query_complete:
        status, reason = LOS_STATUS_UNKNOWN, "building_query_incomplete"
    elif estimated_hits:
        status, reason = LOS_STATUS_UNKNOWN, "building_height_evidence_insufficient"
    elif unknown_reasons:
        status, reason = LOS_STATUS_UNKNOWN, unknown_reasons[0]
    elif boundary_contact:
        status, reason = LOS_STATUS_UNKNOWN, "building_boundary_contact_unresolved"
    else:
        status, reason = LOS_STATUS_CLEAR, None
    engineering = LOS_STATUS_BLOCKED if hits else (
        LOS_STATUS_UNKNOWN if unknown_reasons or boundary_contact or not query_complete
        else LOS_STATUS_CLEAR
    )
    computation_completion = "complete" if query_complete else "incomplete"
    # ``evidence_completion`` answers "is the evidence sufficient for the reported main
    # verdict?", not "did the query finish?".
    #
    #   * ``clear`` from a finished query needs no obstruction evidence at all.
    #   * ``blocked`` is sufficient exactly when every obstruction in the verdict is
    #     *confirmed*: a hit that rests on ``predicted`` / unknown height evidence keeps
    #     the main verdict at ``unknown`` (see the branches above), so a confirmed
    #     ``blocked`` cannot be resting on an unconfirmed estimate.
    #   * ``unknown`` is by construction insufficient evidence.
    #
    # Anything else (an estimate mixed into a confirmed verdict, an unresolved input or a
    # truncated query) stays incomplete.  This is what lets a fully evaluated ray reach
    # ``evidence_completion == "complete"`` instead of being declared incomplete forever.
    if status == LOS_STATUS_CLEAR:
        evidence_completion = "complete" if query_complete else "incomplete"
    elif status == LOS_STATUS_BLOCKED:
        evidence_completion = (
            "complete" if query_complete and not estimated_hits else "incomplete"
        )
    else:
        evidence_completion = "incomplete"
    return finish({
        "status": status, "reason": reason,
        "engineering_estimate_status": engineering,
        "candidate_building_count": len(buildings or []),
        "exact_intersection_count": exact_count,
        "obstruction_hits": hits,
        "confirmed_obstruction_count": len(confirmed_hits),
        "estimated_obstruction_count": len(estimated_hits),
        "unknown_reasons": sorted(set(unknown_reasons)),
        "boundary_contact_present": boundary_contact,
        "geometry_quality_counts": quality_counts,
    }, computation_completion=computation_completion,
        evidence_completion=evidence_completion)


__all__ = [
    "BUILDING_LOS_GATE_GEOMETRY_UNRESOLVED", "BUILDING_LOS_GATE_OUTSIDE_ELEVATION_INTERVAL",
    "BUILDING_LOS_GATE_OUTSIDE_EQUIPMENT_RANGE", "BUILDING_LOS_GATE_WITHIN_DEVICE",
    "CONFIRMED_HEIGHT_EVIDENCE_LEVELS", "GATE_GEOMETRY_FILTERED_STATUS",
    "GATE_OUTSIDE_STATUS", "GATE_UNRESOLVED_STATUS",
    "HEIGHT_EVIDENCE_POLICY_ID", "RADAR_BUILDING_LOS_ALGORITHM_VERSION",
    "RADAR_BUILDING_LOS_EXECUTION_FINGERPRINT_VERSION",
    "RADAR_BUILDING_LOS_FINGERPRINT_VERSION", "RADAR_BUILDING_LOS_GEOMETRY_GATE_VERSION",
    "RAY_MODEL_ID", "building_los_execution_identity", "building_los_input_fingerprint",
    "classify_frozen_device_geometry", "evaluate_building_los", "frozen_device_geometry",
    "frozen_geometry_gate",
]
