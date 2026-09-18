"""JSON-safe Route Planner **V3-D** contracts: validated route -> operational adoption.

V3-D does not plan.  It takes a **current V3-C ``validated_route``** and publishes it, in
an explicit and traceable way, to the *existing* operational route surface
(``operational_routes`` + the ``spatial_3d`` altitude-profile interface) so the existing
P7→P8→P9→P10 CNS chain can assess it.  Route planning and CNS assessment stay strictly
**serial**: CNS never feeds back into V3 cost or search.

Two invariants dominate this module:

* **the V3-C validation history is immutable.**  Its result keeps
  ``operational_route=false`` and ``cns_assessed=false`` forever; adoption and CNS
  outcomes live in *separate* containers and are never written back into the
  validation.  A stored validation is therefore never rewritten -- only read.
* **route safety validation and CNS compliance never merge.**  A V3-C
  ``validated_route`` whose CNS requirement verdict is ``does_not_meet`` stays a
  ``validated_route``; a CNS gap/coverage failure is never reported as a route
  validation failure, and vice versa.

Representation rules encoded here:

* ``ContinuousRoute3D`` remains the **authoritative** geometry source.  The published
  route uses its already-validated **linearized** metric geometry -- it is never
  simplified again;
* the published ``path`` is **two-dimensional ``[lon, lat]`` only**.  The EGM2008
  orthometric altitude is *never* written into a GeoJSON third coordinate: a third
  coordinate would be read as a WGS84 ellipsoidal height, which it is not.  Altitude
  lives exclusively in the locked ``route_altitude_profile``;
* the full analytic geometry is **not** copied into ``operational_routes``.  Only a
  light provenance block travels with the route.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from typing import Any, TypedDict

V3D_MODEL_SCOPE = "v3c_validated_route_operational_adoption_and_cns_bridge_v3d"
V3D_ALGORITHM_ID = "route_planner_v3_operational_adoption"
V3D_ALGORITHM_VERSION = "3.3-alpha"

PROJECTION_SCHEMA_VERSION = "3.3-operational-projection"
ADOPTION_SCHEMA_VERSION = "3.3-operational-adoption"
PREVIEW_SCHEMA_VERSION = "3.3-operational-adoption-preview"
BUNDLE_SCHEMA_VERSION = "3.3-cns-assessment-bundle"
ADOPTION_COLLECTION_SCHEMA_VERSION = "3.3-operational-adoptions"

#: The only sources a V3-D projection may read.  ``canonical_synthetic`` is a test
#: harness only: it can be *previewed* but never published to ``operational_routes``.
V3D_EVIDENCE_SOURCES = ("canonical_synthetic", "configured_real_sources")
PRODUCTION_EVIDENCE_SOURCE = "configured_real_sources"

#: The only statuses an adoption may carry.
ADOPTION_STATUSES = ("published", "stale", "revoked")

#: The only statuses a projection/preview may carry.
PROJECTION_STATUSES = ("ready", "blocked", "not_ready")

#: CNS bundle statuses.
ASSESSMENT_STATUSES = ("not_started", "incomplete", "complete", "stale")

#: CNS requirement verdicts.  ``unknown`` is never a pass.
REQUIREMENT_VERDICTS = ("meets", "does_not_meet", "unknown")

#: The CNS stages the bridge reuses, in their fixed order.
CNS_STAGES = ("P7", "P8", "P9", "P10")

V3D_DISCLAIMER = (
    "V3-D operational adoption：把 current V3-C validated_route 以显式、可追溯方式发布到既有 "
    "operational_routes + spatial_3d 高度剖面接口，并复用既有 P7/P8/P9/P10 做 CNS Assessment。"
    "route planning 与 CNS assessment 严格串行：CNS 绝不反馈 V3 cost/search。"
    "V3-C validation 历史不可变；CNS 不满足不等于 route unsafe，route validation 通过也不等于 CNS 合规。"
)

ROUTE_KIND = "operational"
ROUTE_STATUS = "passed"
ROUTE_SOURCE_TYPE = "v3c_validated_route"
PLANNER_FAMILY = "route_planner_v3"
VERTICAL_REFERENCE = "egm2008_orthometric"
PATH_CRS = "OGC:CRS84"
#: Fixed semantics strings (never re-labelled by a consumer).
PATH_GEOMETRY_SEMANTICS = (
    "validated_v3c_linearized_metric_geometry_transformed_to_ogc_crs84_two_dimensional_only"
)
PROFILE_DERIVATION_SEMANTICS = (
    "v3c_z_s_sampled_at_the_published_path_vertices_and_interpolated_linearly_on_the_"
    "published_profile_distance_basis"
)
PROFILE_DISTANCE_BASIS = "cumulative_2d_baseline_path_distance_matching_path_vertex_order"
#: The profile is not re-validated terrain clearance; it is the V3-C realized vertical.
PROFILE_SEMANTICS = "v3c_validated_realized_vertical_profile_not_an_independent_safety_verdict"


def _finite(value):
    return (
        isinstance(value, (int, float)) and not isinstance(value, bool)
        and float(value) == float(value)
    )


def _optional_number(value, field, *, nonnegative=False, positive=False):
    if value in (None, ""):
        return None
    if not _finite(value):
        raise ValueError(f"{field} 必须是有限数值")
    number = float(value)
    if nonnegative and number < 0:
        raise ValueError(f"{field} 不得小于零")
    if positive and number <= 0:
        raise ValueError(f"{field} 必须大于零")
    return number


def _optional_int(value, field, *, minimum=None):
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) != int(value):
        raise ValueError(f"{field} 必须是整数")
    number = int(value)
    if minimum is not None and number < minimum:
        raise ValueError(f"{field} 不得小于 {minimum}")
    return number


def _text(value, default=""):
    return str(value if value is not None else default)


def stable_fingerprint(value, *, prefix=""):
    return prefix + sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ).encode()
    ).hexdigest()


def finite_number(value):
    return _finite(value)


def utc_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- projection


class V3OperationalProjection(TypedDict, total=False):
    """The V3-C validated route projected into the legacy operational representation.

    ``route`` is the ``operational_routes`` record, ``profile`` the locked
    ``RouteAltitudeProfile``; ``path_metrics`` documents the metric<->geographic length
    relationship, and ``compatibility`` records the P7 projection semantics.
    """

    schema_version: str
    status: str
    reason: str | None
    projection_id: str
    projection_fingerprint: str
    validation_id: str
    refinement_id: str
    experiment_id: str | None
    route_id: str
    route: dict[str, Any] | None
    profile: dict[str, Any] | None
    path_metrics: dict[str, Any]
    path_crs: str
    horizontal_crs: str | None
    horizontal_crs_source: str | None
    transform: dict[str, Any]
    two_dimensional_path_only: bool
    altitude_representation: dict[str, Any]
    profile_vertex_count: int
    compatibility: dict[str, Any]
    downstream_invalidation: list[str]
    route_identity: dict[str, Any]
    issues: list[dict[str, Any]]
    semantics: dict[str, Any]


def empty_v3_operational_projection(status="not_ready"):
    return {
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "status": status if status in PROJECTION_STATUSES else "not_ready",
        "reason": None,
        "projection_id": None,
        "projection_fingerprint": None,
        "validation_id": None,
        "refinement_id": None,
        "experiment_id": None,
        "route_id": None,
        "route": None,
        "profile": None,
        "path_metrics": {
            "v3_metric_length_m": None,
            "legacy_geodesic_length_m": None,
            "length_delta_m": None,
            "distance_basis": PROFILE_DISTANCE_BASIS,
            "metric_vertex_count": 0,
            "published_vertex_count": 0,
            "curve_chord_error_m": None,
        },
        "path_crs": PATH_CRS,
        "horizontal_crs": None,
        "horizontal_crs_source": None,
        "transform": {
            "method": None,
            "authority": None,
            "geodetic": None,
            "target_crs": PATH_CRS,
        },
        #: Hard representation boundary: the published path never carries a z value.
        "two_dimensional_path_only": True,
        "altitude_representation": {
            "vertical_reference": VERTICAL_REFERENCE,
            "in_geojson_third_coordinate": False,
            "carried_by": "locked_route_altitude_profile_waypoints",
            "reason": (
                "GeoJSON 第三坐标会被解释为 WGS84 ellipsoidal height，"
                "而 V3 高度是 EGM2008 正高；因此只写二维 [lon, lat]"
            ),
        },
        "profile_vertex_count": 0,
        "compatibility": {
            "projection_semantics": PATH_GEOMETRY_SEMANTICS,
            "profile_semantics": PROFILE_SEMANTICS,
            "profile_derivation": PROFILE_DERIVATION_SEMANTICS,
            "path_and_profile_share_vertex_order": True,
            "path_and_profile_share_distance_basis": True,
            "simplification_applied": False,
            "crs_mixing": False,
        },
        "downstream_invalidation": [],
        "route_identity": {
            "route_id": None,
            "start": None,
            "end": None,
            "start_node_id": None,
            "end_node_id": None,
            "identity_preserved": False,
        },
        "issues": [],
        "semantics": {
            "scope": V3D_MODEL_SCOPE,
            "authoritative_geometry_source": "v3c_continuous_route_linearized_metric_geometry",
            "no_simplification": True,
            "two_dimensional_path_only": True,
            "egm2008_never_in_geojson_third_coordinate": True,
            "cns_not_in_route_cost": True,
        },
    }


# --------------------------------------------------------------------------- adoption


class V3OperationalAdoption(TypedDict, total=False):
    """One published adoption of a V3-C validated route into ``operational_routes``."""

    schema_version: str
    adoption_id: str
    route_id: str
    status: str
    current_applicability: str
    applied_at: str
    experiment_id: str | None
    refinement_id: str
    validation_ids: list[str]
    validation_fingerprints: dict[str, Any]
    refinement_fingerprint: str | None
    projection_fingerprint: str
    evidence_source: str
    route_provenance: dict[str, Any]
    profile: dict[str, Any]
    path_metrics: dict[str, Any]
    compatibility: dict[str, Any]
    before: dict[str, Any]
    after: dict[str, Any]
    cns_assessment: dict[str, Any]
    stale_reason: str | None
    provenance: dict[str, Any]
    note: str


def empty_v3_operational_adoption():
    return {
        "schema_version": ADOPTION_SCHEMA_VERSION,
        "adoption_id": None,
        "route_id": None,
        "status": "published",
        "current_applicability": "current",
        "applied_at": None,
        "experiment_id": None,
        "refinement_id": None,
        "validation_ids": [],
        "validation_fingerprints": {},
        "refinement_fingerprint": None,
        "projection_fingerprint": None,
        "evidence_source": None,
        "route_provenance": {},
        "profile": {},
        "path_metrics": {},
        "compatibility": {},
        "before": {"present": False, "route": None},
        "after": {"present": False, "route": None},
        "cns_assessment": {
            "bundle_id": None,
            "assessment_status": "not_started",
            "requirement_verdict": "unknown",
        },
        "stale_reason": None,
        "provenance": {},
        "note": V3D_DISCLAIMER,
    }


def empty_v3_operational_adoptions():
    return {
        "schema_version": ADOPTION_COLLECTION_SCHEMA_VERSION,
        "status": "not_calculated",
        "count": 0,
        "items": [],
    }


def normalize_v3_operational_adoptions(value=None):
    source = value if isinstance(value, dict) else {}
    items = []
    for raw in source.get("items") or []:
        if not isinstance(raw, dict):
            continue
        items.append(normalize_v3_operational_adoption(raw))
    return {
        "schema_version": ADOPTION_COLLECTION_SCHEMA_VERSION,
        "status": _text(source.get("status") or ("passed" if items else "not_calculated")),
        "count": len(items),
        "items": items,
    }


def normalize_v3_operational_adoption(value=None):
    result = empty_v3_operational_adoption()
    if not isinstance(value, dict):
        return result
    result.update(deepcopy(value))
    status = _text(value.get("status") or "published")
    result["status"] = status if status in ADOPTION_STATUSES else "published"
    result["schema_version"] = ADOPTION_SCHEMA_VERSION
    result["current_applicability"] = _text(value.get("current_applicability") or "current")
    result["validation_ids"] = [_text(item) for item in value.get("validation_ids") or []]
    result["validation_fingerprints"] = deepcopy(value.get("validation_fingerprints") or {})
    result["route_provenance"] = deepcopy(value.get("route_provenance") or {})
    result["profile"] = deepcopy(value.get("profile") or {})
    result["path_metrics"] = deepcopy(value.get("path_metrics") or {})
    result["compatibility"] = deepcopy(value.get("compatibility") or {})
    result["provenance"] = deepcopy(value.get("provenance") or {})
    result["note"] = V3D_DISCLAIMER
    # The two representation boundaries can never be relaxed by a payload.
    result["route_provenance"]["source_type"] = ROUTE_SOURCE_TYPE
    result["route_provenance"]["planner_family"] = PLANNER_FAMILY
    result["route_provenance"]["vertical_reference"] = VERTICAL_REFERENCE
    return result


def adoption_fingerprint(adoption):
    """A stable identity for one adoption's evidence (not its timestamps)."""

    source = adoption if isinstance(adoption, dict) else {}
    return stable_fingerprint({
        "route_id": source.get("route_id"),
        "validation_ids": sorted(str(item) for item in source.get("validation_ids") or []),
        "validation_fingerprints": source.get("validation_fingerprints") or {},
        "refinement_fingerprint": source.get("refinement_fingerprint"),
        "projection_fingerprint": source.get("projection_fingerprint"),
        "evidence_source": source.get("evidence_source"),
    }, prefix="V3DADOPT-")


# --------------------------------------------------------------------------- preview


class V3OperationalAdoptionPreview(TypedDict, total=False):
    """A read-only preview of what an Apply would publish (no writes)."""

    schema_version: str
    status: str
    reason: str | None
    preview_id: str
    preview_fingerprint: str
    publication_allowed: bool
    evidence_source: str
    production_publication: bool
    synthetic_test_only: bool
    synthetic_notice: str | None
    requested_validation_ids: list[str]
    projections: list[dict[str, Any]]
    blocked: list[dict[str, Any]]
    route_ids: list[str]
    downstream_invalidation: list[str]
    operational_routes_untouched: bool
    spatial_3d_untouched: bool
    cns_not_run: bool
    boundaries: dict[str, Any]
    note: str


def empty_v3_operational_adoption_preview(status="not_ready"):
    return {
        "schema_version": PREVIEW_SCHEMA_VERSION,
        "status": status if status in PROJECTION_STATUSES else "not_ready",
        "reason": None,
        "preview_id": None,
        "preview_fingerprint": None,
        "publication_allowed": False,
        "evidence_source": None,
        "production_publication": False,
        "synthetic_test_only": False,
        "synthetic_notice": None,
        "requested_validation_ids": [],
        "projections": [],
        "blocked": [],
        "route_ids": [],
        "downstream_invalidation": [],
        "operational_routes_untouched": True,
        "spatial_3d_untouched": True,
        "cns_not_run": True,
        "boundaries": {
            "never_writes_operational_routes_on_preview": True,
            "v3c_validation_history_immutable": True,
            "egm2008_never_in_geojson_third_coordinate": True,
            "cns_excluded_from_v3_cost": True,
            "route_safety_and_cns_compliance_are_separate": True,
        },
        "note": V3D_DISCLAIMER,
    }


# --------------------------------------------------------------------------- CNS bundle


class V3CNSAssessmentBundle(TypedDict, total=False):
    """The CNS assessment of a published V3 route, built only from the existing P7-P10.

    ``assessment_status`` describes **evidence completeness**; ``requirement_verdict``
    describes **requirement satisfaction**.  They are orthogonal on purpose: a complete
    assessment may be ``does_not_meet`` while the V3-C validation stays
    ``validated_route``.
    """

    schema_version: str
    bundle_id: str
    route_id: str
    adoption_id: str | None
    validation_id: str | None
    assessment_status: str
    requirement_verdict: str
    stage_results: dict[str, Any]
    stage_order: list[str]
    requested_stages: list[str]
    blocking_reasons: list[str]
    input_fingerprint: str | None
    output_fingerprint: str | None
    assessment_fingerprint: str | None
    route_validation_status: str | None
    route_validation_unchanged: bool
    computed_at: str | None
    provenance: dict[str, Any]
    semantics: dict[str, Any]
    note: str


def empty_v3_cns_assessment_bundle(status="not_started"):
    return {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "bundle_id": None,
        "route_id": None,
        "adoption_id": None,
        "validation_id": None,
        "assessment_status": status if status in ASSESSMENT_STATUSES else "not_started",
        "requirement_verdict": "unknown",
        "stage_results": {
            name: {"stage": name, "status": "not_run", "reason": None, "fingerprint": None}
            for name in CNS_STAGES
        },
        "stage_order": list(CNS_STAGES),
        "requested_stages": [],
        "blocking_reasons": [],
        "input_fingerprint": None,
        "output_fingerprint": None,
        "assessment_fingerprint": None,
        "route_validation_status": None,
        "route_validation_unchanged": True,
        "computed_at": None,
        "provenance": {},
        "semantics": {
            "scope": "v3c_validated_route_cns_assessment_bridge",
            "reuses_existing_p7_p8_p9_p10_only": True,
            "no_cns_formula_duplication": True,
            "cns_excluded_from_route_cost": True,
            "route_planning_and_cns_assessment_are_serial": True,
            "route_safety_is_not_cns_compliance": True,
            "cns_gap_never_rewrites_route_validation": True,
            "unknown_is_never_a_pass": True,
            "assessment_completeness_is_not_requirement_verdict": True,
        },
        "note": V3D_DISCLAIMER,
    }


def normalize_v3_cns_assessment_bundle(value=None):
    result = empty_v3_cns_assessment_bundle()
    if not isinstance(value, dict):
        return result
    result.update(deepcopy(value))
    status = _text(value.get("assessment_status") or value.get("status") or "not_started")
    result["assessment_status"] = status if status in ASSESSMENT_STATUSES else "not_started"
    verdict = _text(value.get("requirement_verdict") or "unknown")
    result["requirement_verdict"] = verdict if verdict in REQUIREMENT_VERDICTS else "unknown"
    result["schema_version"] = BUNDLE_SCHEMA_VERSION
    stages = result.setdefault("stage_results", {})
    for name in CNS_STAGES:
        entry = stages.get(name) if isinstance(stages.get(name), dict) else {}
        stages[name] = {
            "stage": name,
            "status": _text(entry.get("status") or "not_run"),
            "reason": entry.get("reason"),
            "fingerprint": entry.get("fingerprint"),
            "algorithm_id": entry.get("algorithm_id"),
            "algorithm_version": entry.get("algorithm_version"),
            "route_statuses": deepcopy(entry.get("route_statuses") or {}),
            "evidence": deepcopy(entry.get("evidence") or {}),
        }
    result["stage_order"] = list(CNS_STAGES)
    result["requested_stages"] = [_text(item) for item in value.get("requested_stages") or []]
    result["blocking_reasons"] = [_text(item) for item in value.get("blocking_reasons") or []]
    result["note"] = V3D_DISCLAIMER
    # Route safety is separate from CNS compliance, whatever the payload said.
    result["route_validation_unchanged"] = True
    result.setdefault("semantics", deepcopy(empty_v3_cns_assessment_bundle()["semantics"]))
    result["semantics"].update(empty_v3_cns_assessment_bundle()["semantics"])
    return result


def bundle_fingerprint(bundle):
    source = bundle if isinstance(bundle, dict) else {}
    return stable_fingerprint({
        "route_id": source.get("route_id"),
        "adoption_id": source.get("adoption_id"),
        "validation_id": source.get("validation_id"),
        "assessment_status": source.get("assessment_status"),
        "requirement_verdict": source.get("requirement_verdict"),
        "stage_results": source.get("stage_results") or {},
        "input_fingerprint": source.get("input_fingerprint"),
    }, prefix="V3DBUNDLE-")


__all__ = [
    "ADOPTION_COLLECTION_SCHEMA_VERSION", "ADOPTION_SCHEMA_VERSION", "ADOPTION_STATUSES",
    "ASSESSMENT_STATUSES", "BUNDLE_SCHEMA_VERSION", "CNS_STAGES",
    "PATH_CRS", "PATH_GEOMETRY_SEMANTICS", "PLANNER_FAMILY", "PREVIEW_SCHEMA_VERSION",
    "PRODUCTION_EVIDENCE_SOURCE", "PROFILE_DERIVATION_SEMANTICS", "PROFILE_DISTANCE_BASIS",
    "PROFILE_SEMANTICS", "PROJECTION_SCHEMA_VERSION", "PROJECTION_STATUSES",
    "REQUIREMENT_VERDICTS", "ROUTE_KIND", "ROUTE_SOURCE_TYPE", "ROUTE_STATUS",
    "V3D_ALGORITHM_ID", "V3D_ALGORITHM_VERSION", "V3D_DISCLAIMER", "V3D_EVIDENCE_SOURCES",
    "V3D_MODEL_SCOPE", "VERTICAL_REFERENCE",
    "V3CNSAssessmentBundle", "V3OperationalAdoption", "V3OperationalAdoptionPreview",
    "V3OperationalProjection",
    "adoption_fingerprint", "bundle_fingerprint", "empty_v3_cns_assessment_bundle",
    "empty_v3_operational_adoption", "empty_v3_operational_adoptions",
    "empty_v3_operational_adoption_preview", "empty_v3_operational_projection",
    "finite_number", "normalize_v3_cns_assessment_bundle",
    "normalize_v3_operational_adoption", "normalize_v3_operational_adoptions",
    "stable_fingerprint", "utc_now",
]
