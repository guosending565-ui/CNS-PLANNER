"""Bridge canonical Radar layout results into the CNS service chain.

No geometry is implemented here.  Evidence is adapted from the Radar
validation result, and hypothetical panels are evaluated only by the existing
``actual_site_coverage`` canonical implementation.
"""

from __future__ import annotations

from copy import deepcopy
from math import isfinite

from ..algorithms.radar_layout.v1 import actual_site_coverage, required_count_for
from ..gis.radar_layout_adapter import radar_metric_coordinate
from .cns_service_contract import SERVICE_KEY_RADAR_NONCOOPERATIVE
from .cns_service_registry import planner_family_for, service_requirement_for
from .radar_surveillance_layout import (
    METRIC_CRS, radar_geometry_parameters, route_sample_height_semantics,
)
from .route_safety_evidence_v2 import stable_fingerprint
from .site_planning import TOWER_COLOCATION_REUSE_CLASS


SERVICE_KEY = SERVICE_KEY_RADAR_NONCOOPERATIVE
CURRENT_LAYOUT_STATUSES = {"proposal_ready", "infeasible", "refinement_incomplete"}
ALTITUDE_COMPARISON_TOLERANCE_M = 1e-6


def radar_required_for(required_cns, route_id=None):
    requirements = (
        ((required_cns or {}).get("route_overrides") or {}).get(str(route_id))
        or (required_cns or {}).get("project_default") or {}
    )
    surveillance = requirements.get("surveillance") or {}
    return service_requirement_for("S", surveillance, SERVICE_KEY) is not None


def build_radar_service_evidence(required_cns, layout, *, route_ids=None):
    """Build route/sample evidence only for explicitly required Radar service."""

    routes = []
    layout = layout if isinstance(layout, dict) else {}
    for item in layout.get("items") or []:
        if not isinstance(item, dict):
            continue
        route_id = str(item.get("route_id") or "")
        if not route_id or not radar_required_for(required_cns, route_id):
            continue
        routes.append(_route_evidence(item))
    if not routes:
        # Explicit requirements with no matching layout still need unknown evidence.
        explicit_ids = _explicit_route_ids(required_cns)
        candidates = explicit_ids or [
            str(route_id) for route_id in (route_ids or [])
            if radar_required_for(required_cns, route_id)
        ]
        routes = [_missing_route(route_id, layout) for route_id in candidates]
    if not routes:
        return None
    result = {
        "service_key": SERVICE_KEY,
        "subsystem": "S",
        "service_subtype": "noncooperative_surveillance",
        "target_cooperation": "non_cooperative",
        "technology": "radar",
        "planner_family": planner_family_for(SERVICE_KEY),
        "routes": routes,
        "source_layout_fingerprint": stable_fingerprint(layout, prefix="radarlayout-"),
        "algorithm_provenance": {
            "algorithm_id": layout.get("algorithm_id"),
            "algorithm_version": layout.get("algorithm_version"),
            "model_scope": layout.get("model_scope"),
        },
    }
    result["input_fingerprint"] = stable_fingerprint(result, prefix="radarservice-")
    return result


def evidence_for_probe(evidence, route_id, probe, *, metric_projector=None):
    """Evaluate one P14 voxel probe with the selected canonical Radar layout.

    Route offset is retained only as audit context.  Coverage is always rerun
    for this probe's own coordinate, altitude, and surface class through
    :func:`actual_site_coverage`.
    """

    route = next(
        (item for item in (evidence or {}).get("routes") or []
         if str(item.get("route_id")) == str(route_id)),
        None,
    )
    if route is None:
        return None
    probe = probe if isinstance(probe, dict) else {}
    voxel_id = probe.get("voxel_id")
    coordinate = [probe.get("longitude"), probe.get("latitude")]
    altitude = probe.get("altitude_egm2008_m")
    try:
        altitude_value = float(altitude)
    except (TypeError, ValueError):
        altitude_value = None
    model_altitude = _finite_float(route.get("model_supported_altitude_egm2008_m"))
    if route.get("status") != "current" or model_altitude is None:
        return _unknown_service_entry(
            route_id, voxel_id, evidence,
            route.get("reason") or "radar_layout_altitude_evidence_required",
            probe=probe, route=route,
        )
    if (
        altitude_value is None
        or not isfinite(altitude_value)
        or abs(altitude_value - model_altitude) > ALTITUDE_COMPARISON_TOLERANCE_M
    ):
        return _unknown_service_entry(
            route_id, voxel_id, evidence, "radar_model_scope_altitude_not_supported",
            probe=probe, route=route,
        )
    inputs = route.get("service_evidence_inputs") or {}
    panels = inputs.get("selected_panels") or []
    towers = inputs.get("tower_records") or []
    if not towers:
        return _unknown_service_entry(
            route_id, voxel_id, evidence, "radar_layout_evidence_required",
            probe=probe, route=route,
        )
    projector = metric_projector or radar_metric_coordinate
    try:
        metric = projector(coordinate[0], coordinate[1], metric_crs=METRIC_CRS)
    except TypeError:
        # A narrow injection seam used by deterministic unit tests; production
        # always uses the public Radar GIS projection above.
        metric = projector(coordinate[0], coordinate[1])
    except Exception:
        metric = None
    if not isinstance(metric, (list, tuple)) or len(metric) != 2:
        return _unknown_service_entry(
            route_id, voxel_id, evidence, "radar_probe_metric_projection_unavailable",
            probe=probe,
        )
    surface_class = str(probe.get("surface_class") or "unknown")
    sample = {
        "sample_index": 0,
        "sample_id": f"voxel:{voxel_id}" if voxel_id else "voxel:unknown",
        "distance_along_route_m": probe.get("nearest_route_offset_m"),
        "metric": [float(metric[0]), float(metric[1])],
        "longitude": coordinate[0],
        "latitude": coordinate[1],
        "egm2008_m": altitude_value,
        "surface_class": surface_class,
        "required_distinct_site_count": required_count_for(surface_class),
        "refinement": False,
    }
    coverage = actual_site_coverage(
        panels=panels,
        samples=[sample],
        selected_panel_ids=[item.get("panel_id") for item in panels],
        tower_records=towers,
    )[0]
    result = _sample_evidence(str(route_id), coverage, route)
    result["voxel_id"] = voxel_id
    result["probe_altitude_egm2008_m"] = altitude_value
    result["altitude_layer_id"] = route.get("altitude_layer_id")
    result["model_supported_altitude_egm2008_m"] = model_altitude
    result["route_sample_height_semantics"] = route.get("route_sample_height_semantics")
    result["nearest_route_offset_m"] = probe.get("nearest_route_offset_m")
    return result


def radar_candidate_actions(targets, layout):
    """Create directional Radar actions from canonical candidate panels only."""

    route_targets = {
        str(item.get("route_id")) for item in targets
        if item.get("service_key") == SERVICE_KEY
    }
    actions = []
    for item in (layout or {}).get("items") or []:
        route_id = str(item.get("route_id") or "")
        if route_id not in route_targets:
            continue
        inputs = item.get("service_evidence_inputs") or {}
        selected_ids = {str(panel.get("panel_id")) for panel in inputs.get("selected_panels") or []}
        towers = {str(tower.get("tower_id")): tower for tower in inputs.get("tower_records") or []}
        for panel in inputs.get("candidate_panels") or []:
            panel_id = str(panel.get("panel_id") or "")
            if not panel_id or panel_id in selected_ids:
                continue
            tower_id = str(panel.get("tower_id") or "")
            tower = towers.get(tower_id) or {}
            geometry = radar_geometry_parameters(panel.get("radar_type"))
            actions.append({
                "action_id": f"radar:{route_id}:{panel_id}",
                "action_type": "add_radar_panel",
                "route_id": route_id,
                "subsystem": "S",
                "service_key": SERVICE_KEY,
                "planner_family": planner_family_for(SERVICE_KEY),
                "reuse_class": TOWER_COLOCATION_REUSE_CLASS,
                "site_id": f"tower:{tower_id}",
                "tower_id": tower_id,
                "distinct_site_id": f"tower:{tower_id}",
                "radar_type": panel.get("radar_type"),
                "panel": {
                    "panel_id": panel_id,
                    "azimuth_deg": panel.get("azimuth_deg"),
                    "beamwidth_deg": 2.0 * float(panel.get("panel_half_width_deg") or 45.0),
                    "elevation_center_deg": geometry.get("elevation_center_deg"),
                    "elevation_min_deg": geometry.get("elevation_min_deg"),
                    "elevation_max_deg": geometry.get("elevation_max_deg"),
                },
                "canonical_panel": deepcopy(panel),
                "eligibility": {"status": "eligible", "reasons": []},
                "confirmed": True,
                "provenance": {
                    "algorithm_id": item.get("algorithm_id"),
                    "algorithm_version": item.get("algorithm_version"),
                    "source_layout_fingerprint": item.get("input_fingerprint"),
                },
            })
    return sorted(actions, key=lambda item: item["action_id"])


def radar_what_if_service_evidence(required_cns, layout, actions):
    """Re-evaluate added Radar panels through canonical Radar geometry."""

    working = deepcopy(layout or {})
    actions_by_route = {}
    for action in actions or []:
        if action.get("service_key") == SERVICE_KEY:
            actions_by_route.setdefault(str(action.get("route_id") or ""), []).append(action)
    for item in working.get("items") or []:
        route_id = str(item.get("route_id") or "")
        route_actions = actions_by_route.get(route_id) or []
        if not route_actions:
            continue
        inputs = item.get("service_evidence_inputs") or {}
        samples = inputs.get("samples") or []
        towers = inputs.get("tower_records") or []
        panels = list(inputs.get("selected_panels") or [])
        panels.extend(deepcopy(action["canonical_panel"]) for action in route_actions)
        inputs["selected_panels"] = deepcopy(panels)
        item["service_evidence_inputs"] = inputs
        selected_ids = [panel.get("panel_id") for panel in panels]
        if not samples or not towers:
            item["status"] = "unresolved"
            item["validation"] = None
            continue
        coverage = actual_site_coverage(
            panels=panels, samples=samples, selected_panel_ids=selected_ids,
            tower_records=towers,
        )
        item["status"] = "proposal_ready"
        item["validation"] = {"samples": coverage, "validated": all(
            sample.get("status") == "satisfied" for sample in coverage
        )}
        item["selected_panels"] = panels
        item["selected_tower_ids"] = sorted({str(panel.get("tower_id")) for panel in panels})
        item["input_fingerprint"] = stable_fingerprint(
            {"baseline": item.get("input_fingerprint"), "actions": route_actions},
            prefix="radarwhatif-",
        )
    return build_radar_service_evidence(required_cns, working)


def _route_evidence(item):
    route_id = str(item.get("route_id") or "")
    altitude, altitude_error = _resolved_layout_altitude(item)
    if altitude_error:
        return _missing_route(route_id, item, reason=altitude_error)
    current = item.get("status") in CURRENT_LAYOUT_STATUSES
    validation = item.get("validation") if isinstance(item.get("validation"), dict) else {}
    samples = validation.get("samples") or []
    if not current or not samples:
        return _missing_route(route_id, item, altitude=altitude)
    result = {
        "route_id": route_id,
        "status": "current",
        **altitude,
        "samples": [_sample_evidence(route_id, sample, item) for sample in samples],
        "service_evidence_inputs": deepcopy(item.get("service_evidence_inputs") or {}),
        "source_layout_fingerprint": item.get("input_fingerprint"),
        "algorithm_id": item.get("algorithm_id"),
        "algorithm_version": item.get("algorithm_version"),
    }
    return result


def _sample_evidence(route_id, sample, item):
    tower_ids = sorted({str(panel.get("tower_id")) for panel in sample.get("panels") or [] if panel.get("tower_id")})
    status = {
        "satisfied": "satisfied",
        "under_redundant": "confirmed_deficit",
        "uncovered": "confirmed_deficit",
        "unknown": "unknown",
    }.get(str(sample.get("status") or "unknown"), "unknown")
    providers = []
    beamwidth = (((item.get("parameters") or {}).get("azimuth_preset") or {}).get(
        "azimuth_beamwidth_deg"
    ))
    for panel in sample.get("panels") or []:
        tower_id = str(panel.get("tower_id") or "")
        providers.append({
            "distinct_site_id": f"tower:{tower_id}" if tower_id else None,
            "tower_id": tower_id or None,
            "radar_type": panel.get("radar_type"),
            "panel_id": panel.get("panel_id"),
            "azimuth_deg": panel.get("azimuth_deg"),
            "beamwidth_deg": beamwidth,
            "elevation_deg": panel.get("elevation_deg"),
            "slant_distance_m": panel.get("slant_distance_m"),
            "coverage_evidence": "radar_actual_site_coverage",
        })
    result = {
        "route_id": route_id,
        "sample_id": sample.get("sample_id"),
        "coordinate": [sample.get("longitude"), sample.get("latitude")],
        "distance_along_route_m": sample.get("distance_along_route_m"),
        "surface_class": sample.get("surface_class") or "unknown",
        "service_key": SERVICE_KEY,
        "subsystem": "S",
        "service_subtype": "noncooperative_surveillance",
        "target_cooperation": "non_cooperative",
        "technology": "radar",
        "planner_family": planner_family_for(SERVICE_KEY),
        "surface_dependent": False,
        "supports_site_planning": True,
        "required_distinct_site_count": sample.get("required_distinct_site_count"),
        "covered_distinct_site_ids": [f"tower:{tower_id}" for tower_id in tower_ids],
        "distinct_site_ids": [f"tower:{tower_id}" for tower_id in tower_ids],
        "distinct_site_count": len(tower_ids),
        "counting_basis": "distinct_site_id",
        "status": status,
        "providers": providers,
        "reasons": [] if status != "unknown" else ["radar_surface_or_layout_evidence_unknown"],
        "source_layout_fingerprint": item.get("input_fingerprint"),
        "algorithm_provenance": {
            "algorithm_id": item.get("algorithm_id"),
            "algorithm_version": item.get("algorithm_version"),
        },
    }
    result["input_fingerprint"] = stable_fingerprint(result, prefix="radarsample-")
    return result


def _missing_route(route_id, source, *, altitude=None, reason=None):
    altitude = altitude if isinstance(altitude, dict) else {
        "altitude_layer_id": None,
        "model_supported_altitude_egm2008_m": None,
        "route_sample_height_semantics": None,
    }
    return {
        "route_id": str(route_id), "status": "evidence_required", "samples": [],
        **altitude,
        "reason": reason or f"radar_layout_{(source or {}).get('status') or 'missing'}",
        "source_layout_fingerprint": (source or {}).get("input_fingerprint"),
    }


def _unknown_service_entry(route_id, voxel_id, evidence, reason, *, probe=None, route=None):
    probe = probe if isinstance(probe, dict) else {}
    route = route if isinstance(route, dict) else {}
    result = {
        "route_id": route_id, "voxel_id": voxel_id, "sample_id": None,
        "coordinate": [probe.get("longitude"), probe.get("latitude")],
        "surface_class": probe.get("surface_class") or "unknown", "service_key": SERVICE_KEY,
        "subsystem": "S", "service_subtype": "noncooperative_surveillance",
        "target_cooperation": "non_cooperative", "technology": "radar",
        "planner_family": planner_family_for(SERVICE_KEY), "surface_dependent": False,
        "supports_site_planning": True, "required_distinct_site_count": None,
        "covered_distinct_site_ids": [], "distinct_site_ids": [],
        "distinct_site_count": None, "counting_basis": "distinct_site_id",
        "status": "unknown", "providers": [], "reasons": [reason],
        "input_fingerprint": (evidence or {}).get("input_fingerprint"),
        "probe_altitude_egm2008_m": probe.get("altitude_egm2008_m"),
        "altitude_layer_id": route.get("altitude_layer_id"),
        "model_supported_altitude_egm2008_m": route.get(
            "model_supported_altitude_egm2008_m"
        ),
        "route_sample_height_semantics": route.get("route_sample_height_semantics"),
        "nearest_route_offset_m": probe.get("nearest_route_offset_m"),
        "algorithm_provenance": (evidence or {}).get("algorithm_provenance"),
    }
    return result


def _resolved_layout_altitude(item):
    """Return canonical layout altitude metadata, or fail closed on inconsistency.

    The authority is the resolved layout item, never a software default or a
    validation sample.  Parameters, route-sampling metadata, and samples are
    independent consistency witnesses for that authority.
    """

    parameters = item.get("parameters") if isinstance(item.get("parameters"), dict) else {}
    route_sampling = (
        item.get("route_sampling") if isinstance(item.get("route_sampling"), dict) else {}
    )
    layer_id = _nonempty_string(item.get("altitude_layer_id"))
    parameter_layer_id = _nonempty_string(parameters.get("fixed_altitude_layer_id"))
    altitude_m = _finite_float(item.get("altitude_m"))
    parameter_altitude_m = _finite_float(parameters.get("fixed_altitude_m"))
    sampling_altitude_m = _finite_float(route_sampling.get("sample_egm2008_m"))
    item_semantics = _nonempty_string(item.get("route_sample_height_semantics"))
    parameter_semantics = _nonempty_string(parameters.get("route_sample_height_semantics"))
    sampling_semantics = _nonempty_string(route_sampling.get("sample_egm2008_semantics"))

    required_values = (
        layer_id, parameter_layer_id, altitude_m, parameter_altitude_m,
        sampling_altitude_m, item_semantics, parameter_semantics, sampling_semantics,
    )
    if any(value is None for value in required_values):
        return None, "radar_layout_altitude_metadata_missing"

    expected_semantics = route_sample_height_semantics(layer_id)
    if (
        parameter_layer_id != layer_id
        or not _same_altitude(altitude_m, parameter_altitude_m)
        or not _same_altitude(altitude_m, sampling_altitude_m)
        or item_semantics != expected_semantics
        or parameter_semantics != item_semantics
        or sampling_semantics != item_semantics
    ):
        return None, "radar_layout_altitude_metadata_inconsistent"

    validation = item.get("validation") if isinstance(item.get("validation"), dict) else {}
    validation_samples = validation.get("samples") or []
    optimization_samples = (
        (item.get("service_evidence_inputs") or {}).get("samples") or []
        if isinstance(item.get("service_evidence_inputs"), dict) else []
    )
    for sample in [*optimization_samples, *validation_samples]:
        sample_altitude = _finite_float(
            sample.get("egm2008_m") if isinstance(sample, dict) else None
        )
        if sample_altitude is None or not _same_altitude(altitude_m, sample_altitude):
            return None, "radar_layout_altitude_sample_inconsistent"

    return {
        "altitude_layer_id": layer_id,
        "model_supported_altitude_egm2008_m": altitude_m,
        "route_sample_height_semantics": item_semantics,
    }, None


def _same_altitude(left, right):
    return abs(float(left) - float(right)) <= ALTITUDE_COMPARISON_TOLERANCE_M


def _finite_float(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def _nonempty_string(value):
    result = str(value).strip() if value is not None else ""
    return result or None


def _explicit_route_ids(required_cns):
    result = []
    for route_id, requirements in ((required_cns or {}).get("route_overrides") or {}).items():
        if service_requirement_for("S", (requirements or {}).get("surveillance") or {}, SERVICE_KEY):
            result.append(str(route_id))
    return sorted(set(result))


__all__ = [
    "build_radar_service_evidence", "evidence_for_probe",
    "radar_candidate_actions", "radar_required_for", "radar_what_if_service_evidence",
]
