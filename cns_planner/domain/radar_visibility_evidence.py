"""Pure Terrain + Building visibility composition and renderer-neutral 3D evidence."""

from __future__ import annotations

import math
from copy import deepcopy

from .radar_building_los import RAY_MODEL_ID
from .radar_terrain_los import EGM2008_ORTHOMETRIC, number_or_none, stable_fingerprint

RADAR_COMBINED_VISIBILITY_VERSION = "radar_combined_visibility_v2"
#: Upgraded with the semantics change: budgets/completion/execution receipts were removed
#: from the input fingerprint, so evidence fingerprinted under ``..._v1`` is explicitly
#: stale instead of silently matching the new one.
RADAR_COMBINED_FINGERPRINT_VERSION = "radar_combined_visibility_input_fingerprint_v2"
RADAR_COMBINED_EXECUTION_FINGERPRINT_VERSION = (
    "radar_combined_visibility_execution_fingerprint_v1"
)
NOT_EVALUATED = "not_evaluated"
#: A domain whose ray the frozen device geometry already excluded.  It is not physical
#: evidence about the ray and must never be read as ``unknown`` blockage evidence.
GEOMETRY_FILTERED = "geometry_filtered"
DOMAIN_FILTERED_STATUSES = ("out_of_equipment_range", "geometry_filtered", "disabled")


def building_domain_state(building_los):
    """Classify a building LOS result as evaluated, filtered, or not evaluated."""

    return _domain_state(building_los)


def _domain(value, name, *, state="evaluated"):
    if isinstance(value, dict):
        item = deepcopy(value)
        item.setdefault("status", "unknown")
        item.setdefault("domain", name)
        item["domain_state"] = state
        return item
    return {
        "domain": name, "status": NOT_EVALUATED, "domain_state": state,
        "computation_completion": "not_evaluated",
        "evidence_completion": "not_evaluated",
        "reason": f"{name}_not_evaluated",
    }

def _domain_state(value):
    """Classify one domain result as evaluated, geometry filtered, or not evaluated."""

    if not isinstance(value, dict):
        return NOT_EVALUATED
    status = str(value.get("status") or "")
    if status in DOMAIN_FILTERED_STATUSES:
        return GEOMETRY_FILTERED
    if status == NOT_EVALUATED:
        return NOT_EVALUATED
    return "evaluated"


_COMPLETE_TOKENS = frozenset({"complete", "completed"})
_BLOCKED = "blocked"
_CLEAR = "clear"
_UNKNOWN = "unknown"


def _domain_completion(domain):
    """Completion the domain itself reported, or ``not_evaluated`` when it is absent."""

    raw = domain.get("computation_completion")
    value = str(raw) if raw not in (None, "") else ""
    if value and value not in _COMPLETE_TOKENS:
        # The domain itself reports that its computation did not finish.
        return value
    # A domain nobody evaluated cannot make the combined computation complete, even when a
    # generic ``computation_completion: complete`` was stamped onto its placeholder.
    if str(domain.get("domain_state") or "") != "evaluated":
        return "incomplete"
    if value:
        return value
    # No statement: a domain that answered with a physical verdict finished computing, while
    # a domain that answered ``unknown`` without reporting any state did not.
    return "complete" if str(domain.get("status") or "") in (_CLEAR, _BLOCKED) else (
        "incomplete"
    )


def _domain_evidence(domain, completion):
    """Evidence completion the domain reported, falling back to its computation state.

    A domain that answered with a physical verdict and reported no evidence state of its
    own (for example a terrain evaluator that states ``clear`` without an evidence flag,
    or the building gate's synthetic result) is as sufficient as its own computation.
    Only a domain that never computed, or that evaluated this ray and explicitly reported
    insufficient evidence, may leave the combined evidence incomplete.
    """

    raw = domain.get("evidence_completion")
    value = str(raw) if raw not in (None, "") else ""
    if value:
        return value
    if completion in _COMPLETE_TOKENS and str(domain.get("status") or "") in (
        _CLEAR, _BLOCKED,
    ):
        return "complete"
    return "incomplete"


def _physical_verdict(state, status):
    """Physical verdict of one domain, or ``None`` when the domain did not evaluate."""

    if state != "evaluated":
        return None
    if status == _BLOCKED:
        return _BLOCKED
    if status == _CLEAR:
        return _CLEAR
    return _UNKNOWN


def _not_evaluated_domains(terrain, building):
    """Names of the domains that never answered this ray."""

    return [
        item.get("domain")
        for item in (terrain, building)
        if item.get("domain_state") == NOT_EVALUATED
    ]


def combine_visibility(terrain_los, building_los):
    """Combine independent domains without hiding either domain's completion.

    A ray that the frozen device geometry excludes (minimum slant range, maximum slant
    range or the elevation interval) is reported as ``geometry_filtered``, not as
    ``unknown``: no building query and no DEM read happened for it.

    The four answers below are **orthogonal** and are never derived from one another:

    ``combined_visibility``
        Is this straight ray known to be obstructed (``blocked``), known to be free
        (``clear``), or neither (``unknown`` / ``not_evaluated`` / ``geometry_filtered``)?
    ``computation_completion``
        Did every domain that this ray needs finish its computation?
    ``evidence_completion``
        Is the reported verdict backed by sufficient evidence?  A verdict can be
        ``blocked`` while the evidence stays incomplete, and a domain that did not
        compute at all can never make the combined evidence complete.
    ``search_incomplete``
        Were all domains that actually had evidence fully evaluated?  A domain that was
        never evaluated leaves the search incomplete without turning it into a physical
        ``unknown`` obstruction.
    """

    terrain = _domain(terrain_los, "terrain_los", state=_domain_state(terrain_los))
    building = _domain(building_los, "building_los", state=building_domain_state(building_los))
    terrain_status = str(terrain.get("status") or _UNKNOWN)
    building_status = str(building.get("status") or _UNKNOWN)
    # Classify by the standardised domain state, not by the raw status spelling, so a
    # filtered ray can never fall through into the physical ``unknown`` branch.
    terrain_state = _domain_state(terrain_los)
    building_state = building_domain_state(building_los)
    terrain_verdict = _physical_verdict(terrain_state, terrain_status)
    building_verdict = _physical_verdict(building_state, building_status)
    verdicts = (terrain_verdict, building_verdict)
    # A credible physical block already answers "is the direct ray obstructed" even when
    # the other domain was never evaluated.  That sufficiency is *not* evidence
    # completeness: the unevaluated domain still keeps the completion incomplete.
    credible_block = _BLOCKED in verdicts
    not_evaluated_domains = _not_evaluated_domains(terrain, building)
    if credible_block:
        combined = _BLOCKED
    elif GEOMETRY_FILTERED in (terrain_state, building_state):
        # The frozen device geometry already excluded this ray, so the answer is "no
        # physical LOS verdict", never an unevaluated-domain placeholder.  This branch is
        # checked before the unevaluated branch because a caller may legitimately not ask
        # the other domain at all once the geometry gate has already decided the ray.
        combined = GEOMETRY_FILTERED
    elif not_evaluated_domains:
        # An absent domain is not a physical statement, so it may not answer ``unknown``.
        combined = NOT_EVALUATED
    elif _UNKNOWN in verdicts:
        combined = _UNKNOWN
    elif verdicts == (_CLEAR, _CLEAR):
        combined = _CLEAR
    else:
        combined = _UNKNOWN

    completions = [_domain_completion(terrain), _domain_completion(building)]
    evidence_states = [
        _domain_evidence(terrain, completions[0]),
        _domain_evidence(building, completions[1]),
    ]
    if GEOMETRY_FILTERED in (terrain_state, building_state):
        # A geometry-filtered domain did not compute this ray and contributes no evidence.
        completions = ["incomplete", "incomplete"]
        evidence_states = ["incomplete", "incomplete"]
    elif not_evaluated_domains:
        # The domain that did answer may keep its own result, but the combined evidence
        # can never be complete while a domain is still unevaluated.
        evidence_states = ["incomplete", "incomplete"]
    computation_completion = (
        "complete" if all(item in _COMPLETE_TOKENS for item in completions) else "incomplete"
    )
    evidence_completion = (
        "complete" if all(item in _COMPLETE_TOKENS for item in evidence_states) else "incomplete"
    )
    geometry_status = "passed" if any(
        isinstance(item.get("probe_geometry"), dict) for item in (terrain, building)
    ) else "not_reported"
    return {
        "algorithm_version": RADAR_COMBINED_VISIBILITY_VERSION,
        "geometry_status": geometry_status,
        "terrain_los": terrain,
        "building_los": building,
        "combined_visibility": combined,
        "computation_completion": computation_completion,
        "evidence_completion": evidence_completion,
        "building_los_physical_unknown": (
            building_status == _UNKNOWN and building_state == "evaluated"
        ),
        "domains_not_evaluated": not_evaluated_domains,
        "search_incomplete": bool(not_evaluated_domains) or computation_completion != "complete",
        # A credible physical block is sufficient to call *this straight ray* obstructed.
        # It is explicitly not a proof about the complete physical scenario, which is why
        # the flag stays separate from ``evidence_completion``.
        "credible_block_is_sufficient_for_direct_ray_block": credible_block,
        "verdict_is_orthogonal_to_completion": True,
        "other_domain_completion_preserved": True,
        "is_detection_probability": False,
    }


def ray_geometry_3d(
    *, tower_id, radar_device_id, probe_id, origin_metric, origin_egm2008_m,
    target_metric, target_egm2008_m, horizontal_crs, terrain_status,
    building_status, combined_status, input_fingerprint,
):
    origin = [number_or_none(item) for item in (origin_metric or [])[:2]]
    target = [number_or_none(item) for item in (target_metric or [])[:2]]
    origin_z = number_or_none(origin_egm2008_m)
    target_z = number_or_none(target_egm2008_m)
    horizontal = None
    if len(origin) == len(target) == 2 and None not in origin + target:
        horizontal = math.hypot(target[0] - origin[0], target[1] - origin[1])
    slant = None if horizontal is None or origin_z is None or target_z is None else math.hypot(
        horizontal, target_z - origin_z,
    )
    return {
        "tower_id": tower_id, "radar_device_id": radar_device_id, "probe_id": probe_id,
        "origin_x": origin[0] if len(origin) == 2 else None,
        "origin_y": origin[1] if len(origin) == 2 else None,
        "origin_z": origin_z,
        "target_x": target[0] if len(target) == 2 else None,
        "target_y": target[1] if len(target) == 2 else None,
        "target_z": target_z,
        "horizontal_crs": horizontal_crs,
        "vertical_crs": EGM2008_ORTHOMETRIC,
        "slant_distance_m": slant,
        "ray_model_id": RAY_MODEL_ID,
        "terrain_status": terrain_status,
        "building_status": building_status,
        "combined_status": combined_status,
        "input_fingerprint": input_fingerprint,
        "egm2008_is_not_ellipsoidal_height": True,
    }


def obstruction_hits_3d(*, terrain_los, building_los):
    """Normalize hits; terrain samples remain intervals, never invented points."""

    hits = []
    terrain = terrain_los if isinstance(terrain_los, dict) else {}
    samples = ((terrain.get("terrain_profile") or {}).get("samples") or [])
    for sample in samples:
        if str(sample.get("status")) != "blocked":
            continue
        distance = number_or_none(sample.get("distance_m"))
        interval = sample.get("distance_interval_m")
        if not (isinstance(interval, (list, tuple)) and len(interval) == 2):
            interval = [distance, distance]
        hits.append({
            "type": "terrain", "building_id": None, "footprint_part_index": None,
            "hit_x": None, "hit_y": None, "hit_z": None,
            "along_ray_distance_m": distance,
            "along_ray_interval_m": list(interval),
            "ray_fraction": None,
            "ground_elevation_egm2008_m": number_or_none(sample.get("terrain_max_m")),
            "roof_elevation_egm2008_m": None,
            "height_evidence_level": None,
            "obstruction_margin_m": (
                None if number_or_none(sample.get("clearance_to_terrain_max_m")) is None
                else -float(sample["clearance_to_terrain_max_m"])
            ),
            "evidence_classification": "confirmed" if sample.get("terrain_max_m") is not None else "unknown",
            "source_version": ((terrain.get("terrain_profile") or {}).get("source") or {}).get("source_version"),
            "hit_derivation_status": "sample_interval_not_exact_intersection",
        })
    building = building_los if isinstance(building_los, dict) else {}
    for item in building.get("obstruction_hits") or []:
        hits.append({
            "type": "building", "building_id": item.get("building_id"),
            "footprint_part_index": item.get("footprint_part_index"),
            "hit_x": item.get("hit_x"), "hit_y": item.get("hit_y"),
            "hit_z": item.get("hit_z"),
            "along_ray_distance_m": item.get("along_ray_distance_m"),
            "along_ray_interval_m": None, "ray_fraction": item.get("ray_fraction"),
            "ground_elevation_egm2008_m": item.get("ground_elevation_egm2008_m"),
            "roof_elevation_egm2008_m": item.get("roof_elevation_egm2008_m"),
            "height_evidence_level": item.get("height_evidence_level"),
            "obstruction_margin_m": item.get("obstruction_margin_m"),
            "evidence_classification": item.get("evidence_classification"),
            "source_version": item.get("source_version"),
            "hit_derivation_status": item.get("hit_derivation_status"),
        })
    return hits


def combined_visibility_input_fingerprint(
    *, terrain_input_fingerprint=None, building_input_fingerprint=None,
    route_probe_identity=None, radar_origin=None, radar_geometry=None,
    terrain_source_identity=None, building_source_identity=None, crs=None, ray_model=None,
):
    """Semantic input identity of one combined Terrain + Building ray.

    Only deterministic semantic inputs belong here: the two domain input fingerprints,
    the ray/probe and tower geometry, both source identities, the CRS and the ray model.
    Budgets, completion, elapsed seconds, cache hits, query statistics, truncation and
    failure reasons are execution evidence and live in
    :func:`combined_visibility_execution_identity`, so two runs that read the same world
    with the same policy stay on one fingerprint no matter how they were executed.

    The version identifiers are part of the payload, so evidence produced under an older
    combined visibility or fingerprint schema can never silently present itself as
    current: it hashes differently and is therefore explicitly stale/incompatible.
    """

    return stable_fingerprint({
        "fingerprint_schema_version": RADAR_COMBINED_FINGERPRINT_VERSION,
        "algorithm_version": RADAR_COMBINED_VISIBILITY_VERSION,
        "terrain_input_fingerprint": deepcopy(terrain_input_fingerprint),
        "building_input_fingerprint": deepcopy(building_input_fingerprint),
        "route_probe_identity": deepcopy(route_probe_identity),
        "radar_origin": deepcopy(radar_origin),
        "radar_geometry": deepcopy(radar_geometry),
        "terrain_source_identity": deepcopy(terrain_source_identity),
        "building_source_identity": deepcopy(building_source_identity),
        "crs": deepcopy(crs), "ray_model": deepcopy(ray_model),
        "height_evidence_policy": "radar_building_los_height_evidence_policy_v1",
        "footprint_geometry_policy": "polygon_multipolygon_holes_make_valid_v1",
    })


def combined_visibility_execution_identity(
    *, input_fingerprint=None, terrain_execution_identity=None,
    building_execution_identity=None, budgets=None, completion=None,
    search_incomplete=None, incomplete_reason=None, elapsed_seconds=None,
    cache_hit_count=None, cache_miss_count=None, query_receipt=None,
):
    """Read-only execution evidence of one combined lookup run.

    This is the receipt side of the identity split: estimated budgets, completion state,
    elapsed time, cache behaviour, query statistics, truncation and failure reasons are
    kept here and are deliberately excluded from the input fingerprint.
    """

    receipt = deepcopy(query_receipt) if isinstance(query_receipt, dict) else {}
    if elapsed_seconds is None:
        elapsed_seconds = receipt.get("elapsed_seconds")
    return {
        "execution_fingerprint_version": RADAR_COMBINED_EXECUTION_FINGERPRINT_VERSION,
        "algorithm_version": RADAR_COMBINED_VISIBILITY_VERSION,
        "input_fingerprint": deepcopy(input_fingerprint),
        "terrain_execution_identity": deepcopy(terrain_execution_identity),
        "building_execution_identity": deepcopy(building_execution_identity),
        "budgets": deepcopy(budgets),
        "completion": deepcopy(completion),
        "search_incomplete": (
            None if search_incomplete is None else bool(search_incomplete)
        ),
        "incomplete_reason": incomplete_reason,
        "elapsed_seconds": number_or_none(elapsed_seconds),
        "cache_hit_count": cache_hit_count,
        "cache_miss_count": cache_miss_count,
        "query_receipt": receipt,
        "elapsed_seconds_is_receipt_only": True,
        "execution_receipt_excluded_from_input_fingerprint": True,
    }


def combined_visibility_evidence_compatibility(evidence_version, evidence_fingerprint=None):
    """Explicit staleness verdict for evidence produced under another schema version.

    Old evidence is never silently presented as current: anything that does not carry the
    exact current combined visibility and fingerprint schema versions is reported stale.
    """

    version = str(evidence_version or "")
    compatible = version == RADAR_COMBINED_VISIBILITY_VERSION
    return {
        "evidence_version": version or None,
        "current_version": RADAR_COMBINED_VISIBILITY_VERSION,
        "current_fingerprint_version": RADAR_COMBINED_FINGERPRINT_VERSION,
        "evidence_fingerprint": deepcopy(evidence_fingerprint),
        "compatible": compatible,
        "stale": not compatible,
        "silently_treated_as_current": False,
        "reason": (
            None if compatible
            else "combined_visibility_evidence_version_mismatch_explicit_stale"
        ),
    }


def combined_visibility_fingerprint(
    *, terrain_input_fingerprint, building_input_fingerprint, route_probe_identity,
    radar_origin, radar_geometry, terrain_source_identity, building_source_identity,
    crs, ray_model, budgets=None, completion=None, building_execution_identity=None,
):
    """Deprecated alias of :func:`combined_visibility_input_fingerprint`.

    The execution-only arguments are accepted so existing callers keep working, but they
    no longer influence the result: they are execution receipts, not semantic input.
    """

    return combined_visibility_input_fingerprint(
        terrain_input_fingerprint=terrain_input_fingerprint,
        building_input_fingerprint=building_input_fingerprint,
        route_probe_identity=route_probe_identity,
        radar_origin=radar_origin,
        radar_geometry=radar_geometry,
        terrain_source_identity=terrain_source_identity,
        building_source_identity=building_source_identity,
        crs=crs,
        ray_model=ray_model,
    )


__all__ = [
    "DOMAIN_FILTERED_STATUSES", "GEOMETRY_FILTERED", "NOT_EVALUATED",
    "RADAR_COMBINED_EXECUTION_FINGERPRINT_VERSION",
    "RADAR_COMBINED_FINGERPRINT_VERSION",
    "RADAR_COMBINED_VISIBILITY_VERSION", "building_domain_state",
    "combine_visibility", "combined_visibility_evidence_compatibility",
    "combined_visibility_execution_identity", "combined_visibility_fingerprint",
    "combined_visibility_input_fingerprint", "obstruction_hits_3d",
    "ray_geometry_3d",
]
