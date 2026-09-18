"""V3-D: publish a current V3-C ``validated_route`` into the operational route surface.

This module is the **only** writer that turns a V3-C validated route into an
``operational_routes`` record plus a locked ``spatial_3d`` altitude profile.  It never
plans, never validates and never reimplements CNS: it **publishes** and it orchestrates
the existing P7→P8→P9→P10 chain for assessment.

Hard rules enforced here:

* production Apply accepts **only** ``configured_real_sources`` evidence; the canonical
  synthetic exercise may be previewed (labelled test-only) but is never published to
  ``operational_routes``;
* Apply and Revoke are transactional: state is mutated on a deep copy and installed
  only after the whole operation succeeded and the result normalizes;
* a batch Apply is **atomic**: if one validation fails its gate, nothing is written;
* the V3-C validation history is read-only.  ``operational_route=false`` /
  ``cns_assessed=false`` are never written back to ``true``, and a CNS gap never
  becomes a route validation failure (nor the reverse);
* publishing does **not** call ``invalidation.workflow("route")`` -- that would
  immediately mark the new route stale.  A dedicated ``v3_operational_route_published``
  propagation keeps the published route and every V3 result intact and stales only the
  downstream CNS chain plus results that depend on the operational route.
"""

from __future__ import annotations

from copy import deepcopy
import math

from ..domain.v3_operational_adoption import (
    ADOPTION_SCHEMA_VERSION, CNS_STAGES, PATH_CRS, PATH_GEOMETRY_SEMANTICS,
    PLANNER_FAMILY, PRODUCTION_EVIDENCE_SOURCE, PROFILE_DERIVATION_SEMANTICS,
    PROFILE_DISTANCE_BASIS, PROFILE_SEMANTICS, ROUTE_KIND, ROUTE_SOURCE_TYPE,
    ROUTE_STATUS, V3D_ALGORITHM_ID, V3D_ALGORITHM_VERSION, V3D_DISCLAIMER,
    V3D_EVIDENCE_SOURCES, V3D_MODEL_SCOPE, VERTICAL_REFERENCE, adoption_fingerprint,
    bundle_fingerprint, empty_v3_cns_assessment_bundle,
    empty_v3_operational_adoption_preview, empty_v3_operational_projection,
    normalize_v3_cns_assessment_bundle, normalize_v3_operational_adoptions,
    stable_fingerprint, utc_now,
)

#: Result keys the publish propagation marks stale, in dependency order.  The published
#: route itself and every V3 result are deliberately absent.
V3_DOWNSTREAM_RESULTS = (
    "coverage_3d", "cns_service_capability", "service_timeline", "cns_gap_v2",
    "cns_site_plan", "closed_loop_assessment", "building_clearance",
    "route_vertical_profiles", "cns_corridor_assessment", "cns_corridor_gap_assessment",
    "cns_corridor_site_plan", "report",
)

V3_PUBLISH_REASON = "v3_operational_route_published"

#: Adapter identity of the library metric transform, so its provenance is auditable even
#: when the callable itself is injected by the composition root.
LIBRARY_TRANSFORM_ID = "pyproj_transformer_ogc_crs84"

_COORDINATE_DIGITS = 12

#: ``result_statuses`` keys that the publish propagation stales (the "report" key is
#: handled by the shared report helper).
_STALE_STATUS_KEYS = (
    "coverage_3d", "cns_service_capability", "service_timeline", "cns_gap_v2",
    "cns_site_plan", "closed_loop_assessment", "building_clearance",
    "route_vertical_profiles", "cns_corridor_assessment", "cns_corridor_gap_assessment",
    "cns_corridor_site_plan",
)

#: Result containers that carry their own ``status`` and must be marked stale.
_STALE_RESULTS = (
    ("coverage_3d", "coverage_3d"),
    ("cns_service_capability", "cns_service_capability"),
    ("service_timeline", "service_timeline"),
    ("cns_gap_analysis_v2", "cns_gap_v2"),
    ("cns_site_plan", "cns_site_plan"),
    ("closed_loop_assessment", "closed_loop_assessment"),
    ("building_clearance_assessment", "building_clearance"),
    ("route_vertical_profiles", "route_vertical_profiles"),
    ("cns_corridor_assessment", "cns_corridor_assessment"),
    ("cns_corridor_gap_assessment", "cns_corridor_gap_assessment"),
    ("cns_corridor_site_plan", "cns_corridor_site_plan"),
    ("encounter_3d_assessment", "encounter_3d_assessment"),
)


class V3OperationalAdoptionService:
    """Explicit, traceable and transactional V3-C -> operational publication."""

    algorithm_id = V3D_ALGORITHM_ID
    algorithm_version = V3D_ALGORITHM_VERSION
    model_scope = V3D_MODEL_SCOPE

    def __init__(self, session, v3_service, invalidation, snapshot, *,
                 metric_transform=None, transform_resolver=None,
                 spatial_3d_service=None, cns_service_capability_service=None,
                 operational_timing_service=None, gap_analysis_v2_service=None,
                 require_production_sources=True):
        self.session = session
        self.v3_service = v3_service
        self.invalidation = invalidation
        self.snapshot = snapshot
        #: Adapter exposing ``to_geographic(metric_point)`` + ``describe()``.
        self.metric_transform = metric_transform
        #: Optional ``(horizontal_crs) -> adapter``; the composition root supplies the
        #: real CRS transform lazily.  Never a hidden identity default.
        self.transform_resolver = transform_resolver
        self.spatial_3d_service = spatial_3d_service
        self.cns_service_capability_service = cns_service_capability_service
        self.operational_timing_service = operational_timing_service
        self.gap_analysis_v2_service = gap_analysis_v2_service
        #: Production default.  A local walkthrough may disable it to exercise the
        #: Apply/Revoke plumbing on a synthetic validation; it is **never** settable from
        #: an HTTP payload, so production Apply always requires the real source chain.
        self.require_production_sources = bool(require_production_sources)
        self._grid_service = None

    # ------------------------------------------------------------------ queries

    def adoptions_snapshot(self):
        collection = normalize_v3_operational_adoptions(
            self.session.state.get("v3_operational_adoptions")
        )
        items = []
        for item in collection["items"]:
            current = self.adoption_applicability(item)
            items.append({
                **deepcopy(item),
                "current_applicability": current["status"],
                "applicability_reasons": current["reasons"],
                "ownership": self.ownership(item),
            })
        return {
            "schema_version": collection["schema_version"],
            "status": "passed" if items else "not_calculated",
            "count": len(items),
            "current_count": sum(1 for item in items if item["current_applicability"] == "current"),
            "stale_count": sum(1 for item in items if item["current_applicability"] == "stale"),
            "revoked_count": sum(1 for item in items if item["current_applicability"] == "revoked"),
            "items": items,
            "allowed_statuses": ["published", "stale", "revoked"],
            "downstream_invalidation": list(V3_DOWNSTREAM_RESULTS),
            "semantics": {
                "scope": V3D_MODEL_SCOPE,
                "v3c_validation_history_immutable": True,
                "route_safety_and_cns_compliance_are_separate": True,
                "cns_excluded_from_route_cost": True,
                "two_dimensional_path_only": True,
            },
            "note": V3D_DISCLAIMER,
        }

    def validation_index(self):
        """Every stored V3-C validation by id (read-only)."""

        records = (self.session.state.get("route_planner_v3_experiments") or {}).get("records") or []
        index = {}
        for record in records:
            for refinement in record.get("refinements") or []:
                for validation in refinement.get("validations") or []:
                    validation_id = str(validation.get("validation_id") or "")
                    if not validation_id:
                        continue
                    index[validation_id] = {
                        "validation_id": validation_id,
                        "experiment": record,
                        "refinement": refinement,
                        "validation": validation,
                        "result": validation.get("result") or {},
                        "experiment_id": record.get("experiment_id"),
                        "refinement_id": refinement.get("refinement_id"),
                        "refinement_fingerprint": (
                            refinement.get("refinement_fingerprint")
                            or (refinement.get("result") or {}).get("refinement_fingerprint")
                        ),
                        "route_id": str(
                            validation.get("route_id") or refinement.get("route_id") or ""
                        ),
                    }
        return index

    def current_validation_status(self):
        snapshot = self.v3_service.continuous_validation_snapshot()
        return {
            str(item.get("validation_id")): item
            for item in snapshot.get("items") or []
            if item.get("validation_id")
        }

    def eligible_validation_options(self):
        """Every stored validation with its gate verdict (UI/audit read-only view)."""

        options = []
        for validation_id, entry in sorted(self.validation_index().items()):
            gate = self.publish_gate(entry, require_production=False)
            production = self.publish_gate(entry, require_production=True)
            options.append({
                "validation_id": validation_id,
                "route_id": entry["route_id"],
                "refinement_id": entry["refinement_id"],
                "experiment_id": entry["experiment_id"],
                "status": entry["result"].get("status"),
                "evidence_source": entry["validation"].get("evidence_source"),
                "eligible": gate["eligible"],
                "reasons": gate["reasons"],
                "production_eligible": production["eligible"],
                "production_reasons": production["reasons"],
            })
        return options

    def publish_readiness(self):
        """Read-only snapshot: reference UNAVAILABLE at import time; see instance method."""

        return self.publish_status()

    def publish_status(self):
        options = self.eligible_validation_options()
        readiness = self.v3_service.real_data_readiness()
        blocking = list(((readiness.get("v3c") or {}).get("blocking_reasons")) or [])
        if not any(item["eligible"] for item in options):
            blocking.append("no_eligible_current_v3c_validated_route")
        if not any(item["production_eligible"] for item in options):
            blocking.append("no_production_eligible_v3c_validated_route")
        return {
            "status": "passed" if not blocking else "blocked",
            "stage": "V3-D",
            "model_scope": V3D_MODEL_SCOPE,
            "algorithm": {
                "algorithm_id": self.algorithm_id,
                "algorithm_version": self.algorithm_version,
                "registered_in_algorithm_registry": False,
            },
            "options": options,
            "adoptions": self.adoptions_snapshot(),
            "real_data_readiness": readiness,
            "blocking_reasons": blocking,
            "boundaries": {
                "production_apply_requires_configured_real_sources": True,
                "synthetic_preview_only": True,
                "cns_excluded_from_route_cost": True,
                "route_safety_and_cns_compliance_are_separate": True,
                "v3c_validation_history_immutable": True,
            },
            "note": V3D_DISCLAIMER,
        }

    # ------------------------------------------------------------------ preview

    def preview(self, payload=None):
        """Read-only preview of what Apply would publish.  Never writes state."""

        payload = payload if isinstance(payload, dict) else {}
        evidence_source = str(payload.get("evidence_source") or PRODUCTION_EVIDENCE_SOURCE)
        requested = self._requested_validation_ids(payload)
        preview = empty_v3_operational_adoption_preview()
        preview.update({
            "evidence_source": evidence_source,
            "production_publication": evidence_source == PRODUCTION_EVIDENCE_SOURCE,
            "synthetic_test_only": evidence_source != PRODUCTION_EVIDENCE_SOURCE,
            "synthetic_notice": (
                None if evidence_source == PRODUCTION_EVIDENCE_SOURCE
                else "canonical synthetic 证据仅用于测试：不可正式发布到 operational_routes"
            ),
            "requested_validation_ids": [str(item) for item in requested],
            "downstream_invalidation": list(V3_DOWNSTREAM_RESULTS),
        })
        if evidence_source not in V3D_EVIDENCE_SOURCES:
            preview["status"] = "blocked"
            preview["reason"] = f"unknown_evidence_source:{evidence_source}"
            preview["eligible_options"] = self.eligible_validation_options()
            _seal_preview(preview)
            return preview
        try:
            entries = self._resolve_entries(requested)
        except ValueError as error:
            preview["status"] = "blocked"
            preview["reason"] = str(error)
            preview["eligible_options"] = self.eligible_validation_options()
            _seal_preview(preview)
            return preview
        projections, blocked = [], []
        for entry in entries:
            gate = self.publish_gate(entry, require_production=False)
            if not gate["eligible"]:
                blocked.append({
                    "validation_id": entry.get("validation_id"),
                    "route_id": entry.get("route_id"),
                    "reasons": gate["reasons"],
                })
                continue
            projection = self.project(entry)
            if projection["status"] != "ready":
                blocked.append({
                    "validation_id": entry.get("validation_id"),
                    "route_id": entry.get("route_id"),
                    "reasons": [projection.get("reason") or "projection_not_ready"],
                })
                continue
            projections.append(projection)
        preview["projections"] = projections
        preview["blocked"] = blocked
        preview["route_ids"] = [item["route_id"] for item in projections]
        preview["status"] = (
            "blocked" if blocked and not projections
            else "not_ready" if not projections
            else "ready"
        )
        # A synthetic projection is previewable but can never be published.
        preview["publication_allowed"] = bool(projections) and evidence_source == PRODUCTION_EVIDENCE_SOURCE
        preview["eligible_options"] = self.eligible_validation_options()
        _seal_preview(preview)
        return preview

    def _requested_validation_ids(self, payload):
        supplied = payload.get("validation_ids")
        if supplied in (None, "", []):
            single = payload.get("validation_id")
            return [str(single)] if single not in (None, "") else []
        if not isinstance(supplied, (list, tuple)):
            raise ValueError("validation_ids 必须是数组")
        return [str(item) for item in supplied if str(item)]

    def _resolve_entries(self, requested):
        index = self.validation_index()
        if requested:
            missing = [item for item in requested if item not in index]
            if missing:
                raise ValueError("validation_id 不存在：" + ", ".join(missing))
            return [index[item] for item in requested]
        eligible = [
            entry for entry in index.values()
            if self.publish_gate(entry, require_production=False)["eligible"]
        ]
        eligible.sort(key=lambda item: str(item.get("validation_id")))
        return eligible

    # ------------------------------------------------------------------ gate

    def publish_gate(self, entry, *, require_production):
        """The complete publish gate for one stored V3-C validation."""

        reasons = []
        validation = entry.get("validation") or {}
        result = entry.get("result") or {}
        route_id = entry.get("route_id")
        evidence_source = str(validation.get("evidence_source") or "")
        if result.get("status") != "validated_route":
            reasons.append(f"validation_status_not_validated_route:{result.get('status')}")
        status = self.current_validation_status().get(str(entry.get("validation_id")))
        if status is None:
            reasons.append("validation_not_found_in_current_snapshot")
        else:
            if status.get("current_applicability") != "current":
                reasons.append("validation_is_stale")
            if status.get("status") != "validated_route":
                reasons.append(f"current_validation_status:{status.get('status')}")
        if evidence_source not in V3D_EVIDENCE_SOURCES:
            reasons.append(f"unknown_evidence_source:{evidence_source or 'none'}")
        if require_production and self.require_production_sources and (
            evidence_source != PRODUCTION_EVIDENCE_SOURCE
        ):
            reasons.append("production_apply_requires_configured_real_sources")
        scenario = self._scenario_route(route_id)
        if scenario is None:
            reasons.append("route_id_is_not_a_current_scenario_route")
        else:
            identity = self._identity_check(scenario, entry)
            reasons.extend(identity["reasons"])
        readiness = self.v3_service.real_data_readiness()
        if (require_production and self.require_production_sources
                and (readiness.get("v3c") or {}).get("status") != "ready"):
            reasons.append("configured_real_source_chain_not_ready")
        realization = self._realization_for(entry)
        if realization is None:
            reasons.append("v3c_continuous_route_unavailable")
        else:
            if self._resolve_transform(realization) is None:
                reasons.append("crs_transform_unavailable")
        if not (entry.get("refinement_fingerprint")
                or result.get("refinement_fingerprint")):
            reasons.append("missing_refinement_fingerprint")
        return {"eligible": not reasons, "reasons": reasons, "entry": entry}

    def _identity_check(self, scenario, entry):
        reasons = []
        realization = self._realization_for(entry)
        if realization is None:
            return {"reasons": ["v3c_continuous_route_unavailable"]}
        transform = self._resolve_transform(realization)
        if transform is None:
            return {"reasons": ["crs_transform_unavailable"]}
        line = _metric_line(realization["route"])
        if len(line) < 2:
            return {"reasons": ["v3c_linearized_geometry_unresolved"]}
        start = transform.to_geographic(line[0])
        end = transform.to_geographic(line[-1])
        if start is None or end is None:
            return {"reasons": ["crs_transform_unavailable"]}
        for label, expected, actual in (
            ("start", scenario.get("start"), start), ("end", scenario.get("end"), end),
        ):
            if not _points_close(expected, actual):
                reasons.append(f"projected_{label}_does_not_match_scenario_route")
        for label in ("start_node_id", "end_node_id"):
            if scenario.get(label) in (None, ""):
                reasons.append(f"scenario_route_missing_{label}")
        return {"reasons": reasons}

    # ------------------------------------------------------------------ projection

    def project(self, entry):
        """Build the legacy operational route + locked profile from a V3-C validation."""

        route_id = str(entry.get("route_id") or "")
        projection = empty_v3_operational_projection("not_ready")
        projection.update({
            "validation_id": entry.get("validation_id"),
            "refinement_id": entry.get("refinement_id"),
            "experiment_id": entry.get("experiment_id"),
            "route_id": route_id,
        })
        realization = self._realization_for(entry)
        if realization is None:
            projection["reason"] = "v3c_continuous_route_unavailable"
            return projection
        route = realization["route"]
        analytics = (route.get("horizontal_geometry") or {}).get("analytic") or {}
        linearized = (route.get("horizontal_geometry") or {}).get("linearized") or {}
        metric_line = _metric_line(route)
        if ((route.get("status") != "realized")
                or (route.get("turn_realization_status") != "realized")
                or len(metric_line) < 2):
            projection["reason"] = "v3c_continuous_route_not_realized"
            return projection
        transform = self._resolve_transform(realization)
        if transform is None:
            projection["reason"] = "crs_transform_unavailable"
            return projection
        scenario = self._scenario_route(route_id)
        if scenario is None:
            projection["reason"] = "route_id_is_not_a_current_scenario_route"
            return projection
        metric_z = self._metric_vertex_altitudes(route, metric_line)
        if metric_z is None:
            projection["reason"] = "v3c_vertex_altitude_unresolved"
            return projection
        path, valid = _project_path(metric_line, transform)
        if not valid:
            projection["reason"] = "crs_transform_unavailable"
            return projection
        # A metric vertex may collapse onto the same published coordinate after rounding;
        # dedupe and keep the first altitude so path and profile stay aligned.
        path, metric_line, metric_z = _dedupe_published_path(path, metric_line, metric_z)
        if len(path) < 2:
            projection["reason"] = "published_path_degenerate_after_crs_conversion"
            return projection
        cumulative = _cumulative_baseline_distance(path)
        metric_length = _polyline_length(metric_line)
        # The published path is lon/lat, so its true length is the geodesic one; the
        # V3 metric length is the authoritative planning-frame length.  Both are reported
        # so the representation change is auditable and never silently conflated.
        geodesic_length = _geodesic_length(path)
        if geodesic_length <= 0:
            projection["reason"] = "published_path_has_zero_length"
            return projection
        frame = realization.get("frame") or {}
        describe = transform.describe() if hasattr(transform, "describe") else {}
        curve_error = linearized.get("curve_chord_error_m")
        provenance = {
            "source_type": ROUTE_SOURCE_TYPE,
            "planner_family": PLANNER_FAMILY,
            "validation_id": entry.get("validation_id"),
            "validation_fingerprint": _validation_fingerprint(entry),
            "refinement_id": entry.get("refinement_id"),
            "refinement_fingerprint": (
                entry.get("refinement_fingerprint") or (entry.get("result") or {}).get(
                    "refinement_fingerprint"
                )
            ),
            "experiment_id": entry.get("experiment_id"),
            "curve_chord_error_m": curve_error,
            "horizontal_crs": frame.get("horizontal_crs"),
            "horizontal_crs_source": frame.get("horizontal_crs_source"),
            "crs_transform": {
                "method": describe.get("method"),
                "authority": describe.get("authority"),
                "target_crs": PATH_CRS,
                "geodetic": describe.get("geodetic"),
                "transform_id": describe.get("transform_id"),
                "path_crs": PATH_CRS,
            },
            "vertical_reference": VERTICAL_REFERENCE,
            "horizontal_representation": "two_dimensional_lon_lat_only",
            "vertical_representation": "locked_route_altitude_profile_waypoints",
            "geometry_semantics": PATH_GEOMETRY_SEMANTICS,
            "simplification_applied": False,
            "analytic_geometry_copied": False,
            "cns_integration_mode": "post_route_assessment",
            "cns_excluded_from_search_cost": True,
        }
        profile = {
            "route_id": route_id,
            "mode": "waypoint_linear",
            "vertical_reference": VERTICAL_REFERENCE,
            "constant_altitude_m": None,
            "waypoints": [
                {
                    "distance_along_route_m": cumulative[index],
                    "altitude_m": round(float(metric_z[index]), 9),
                }
                for index in range(len(path))
            ],
            "geoid_undulation_m": None,
            "source": ROUTE_SOURCE_TYPE,
            "confirmed": True,
            "derived": True,
            "locked": True,
            "locked_by_adoption": True,
            "adoption_owned": True,
            "distance_basis": PROFILE_DISTANCE_BASIS,
            "profile_derivation": PROFILE_DERIVATION_SEMANTICS,
            "profile_semantics": PROFILE_SEMANTICS,
            "vertex_order_semantics": "index_matched_to_published_path_vertices",
            "v3_metric_length_m": round(metric_length, 9),
            "legacy_geodesic_length_m": round(geodesic_length, 9),
            "length_delta_m": round(geodesic_length - metric_length, 9),
            "profile_length_m": round(cumulative[-1], 12),
            "curve_chord_error_m": curve_error,
            "requested_by_user": False,
        }
        profile["status"] = "confirmed"
        operational_route = {
            "route_id": route_id,
            "start": [round(float(value), _COORDINATE_DIGITS) for value in (
                scenario.get("start") or path[0]
            )[:2]],
            "end": [round(float(value), _COORDINATE_DIGITS) for value in (
                scenario.get("end") or path[-1]
            )[:2]],
            "start_node_id": scenario.get("start_node_id"),
            "end_node_id": scenario.get("end_node_id"),
            "direction": scenario.get("direction"),
            "kind": ROUTE_KIND,
            "status": ROUTE_STATUS,
            "path": path,
            "provenance": provenance,
        }
        projection.update({
            "status": "ready",
            "reason": None,
            "route": operational_route,
            "profile": profile,
            "path_metrics": {
                "v3_metric_length_m": round(metric_length, 9),
                "legacy_geodesic_length_m": round(geodesic_length, 9),
                "length_delta_m": round(geodesic_length - metric_length, 9),
                "distance_basis": PROFILE_DISTANCE_BASIS,
                "profile_length_m": round(cumulative[-1], 12),
                "length_semantics": (
                    "v3_metric_length_m 是 V3 规划帧的权威长度；legacy_geodesic_length_m 是"
                    "发布后的 CRS84 lon/lat 二维 path 的 WGS84 测地长度；"
                    "profile_length_m 是 profile 与 path 顶点共用的二维基线累计距离基准"
                ),
                "metric_vertex_count": len(metric_line),
                "published_vertex_count": len(path),
                "curve_chord_error_m": curve_error,
                "simplification_applied": False,
            },
            "horizontal_crs": frame.get("horizontal_crs"),
            "horizontal_crs_source": frame.get("horizontal_crs_source"),
            "transform": {
                "method": describe.get("method"),
                "authority": describe.get("authority"),
                "geodetic": describe.get("geodetic"),
                "target_crs": PATH_CRS,
            },
            "profile_vertex_count": len(profile["waypoints"]),
            "compatibility": {
                "projection_semantics": PATH_GEOMETRY_SEMANTICS,
                "profile_semantics": PROFILE_SEMANTICS,
                "profile_derivation": PROFILE_DERIVATION_SEMANTICS,
                "path_and_profile_share_vertex_order": True,
                "path_and_profile_share_distance_basis": True,
                "simplification_applied": False,
                "crs_mixing": False,
                "published_metric_length_m": round(metric_length, 9),
                "published_geodesic_length_m": round(geodesic_length, 9),
                "length_delta_m": round(geodesic_length - metric_length, 9),
                "profile_length_m": round(cumulative[-1], 12),
                "vertex_count": len(path),
                "min_altitude_egm2008_m": min(metric_z),
                "max_altitude_egm2008_m": max(metric_z),
            },
            "downstream_invalidation": list(V3_DOWNSTREAM_RESULTS),
            "route_identity": {
                "route_id": route_id,
                "start": operational_route["start"],
                "end": operational_route["end"],
                "start_node_id": scenario.get("start_node_id"),
                "end_node_id": scenario.get("end_node_id"),
                "identity_preserved": True,
                "analytics_arc_count": analytics.get("arc_count"),
                "linearized_point_count": linearized.get("point_count"),
            },
        })
        projection["projection_fingerprint"] = stable_fingerprint({
            "route_id": route_id,
            "validation_id": entry.get("validation_id"),
            "refinement_fingerprint": provenance["refinement_fingerprint"],
            "path": path,
            "waypoints": profile["waypoints"],
            "transform": projection["transform"],
            "curve_chord_error_m": curve_error,
        }, prefix="V3DPROJ-")
        projection["projection_id"] = "V3DPROJ-" + projection["projection_fingerprint"][-20:]
        return projection

    def _metric_vertex_altitudes(self, route, metric_line):
        """EGM2008 altitude at every linearized metric vertex (V3-C remains the source)."""

        from ..route_planner_v3.continuous_validators import MetricRoute

        try:
            metric_route = MetricRoute(route)
        except (ValueError, TypeError, AttributeError):
            return None
        values = []
        for point in metric_line:
            value = metric_route.vertical_z(metric_route.distance_of(point))
            values.append(None if value is None else float(value))
        if any(value is None for value in values):
            resolved = [value for value in values if value is not None]
            if not resolved:
                return None
            # Piecewise-linear altitude along the realized route: an unresolved vertex
            # keeps its position via its metric distance, never a fabricated constant.
            cumulative = _cumulative_baseline_distance(metric_line)
            total = cumulative[-1]
            known = [
                (cumulative[index], values[index])
                for index in range(len(values)) if values[index] is not None
            ]
            values = [
                _interpolate_known(cumulative[index] / total if total else 0.0, known)
                for index in range(len(values))
            ]
        return values

    # ------------------------------------------------------------------ apply

    def apply(self, payload=None):
        """Publish the selected current validations to ``operational_routes``.

        Transactional and atomic: everything is computed on a deep copy and installed
        only when the whole batch succeeded.  Any failure leaves project state untouched.
        """

        payload = payload if isinstance(payload, dict) else {}
        if payload.get("confirmed") is not True:
            raise ValueError("V3-D Apply 必须显式 confirmed=true")
        evidence_source = str(payload.get("evidence_source") or PRODUCTION_EVIDENCE_SOURCE)
        if self.require_production_sources and evidence_source != PRODUCTION_EVIDENCE_SOURCE:
            raise ValueError(
                "V3-D production Apply 只接受 configured_real_sources；"
                "canonical_synthetic 只能 Preview，绝不写入 operational_routes"
            )
        if evidence_source not in V3D_EVIDENCE_SOURCES:
            raise ValueError(f"未知 evidence_source：{evidence_source}")
        requested = self._requested_validation_ids(payload)
        entries = self._resolve_entries(requested)
        if not requested:
            entries = [
                entry for entry in entries
                if self.publish_gate(entry, require_production=True)["eligible"]
            ]
        if not entries:
            raise ValueError("没有满足 production publish gate 的 V3-C validated_route")
        projections, fingerprints, failures = [], {}, []
        for entry in entries:
            validation_id = str(entry.get("validation_id"))
            fingerprints[validation_id] = _validation_fingerprint(entry)
            gate = self.publish_gate(entry, require_production=True)
            if not gate["eligible"]:
                failures.append({"validation_id": validation_id, "reasons": gate["reasons"]})
                continue
            projection = self.project(entry)
            if projection["status"] != "ready":
                failures.append({
                    "validation_id": validation_id,
                    "reasons": [projection.get("reason") or "projection_not_ready"],
                })
                continue
            projections.append({"entry": entry, "projection": projection})
        if failures:
            raise ValueError(
                "V3-D Apply 被拒绝（batch atomic，未写入任何 route）："
                + "; ".join(
                    f"{item['validation_id']}:{','.join(item['reasons'])}" for item in failures
                )
            )
        expected = payload.get("expected_validation_fingerprint")
        if expected not in (None, "") and str(expected) not in set(fingerprints.values()):
            raise ValueError(
                "expected_validation_fingerprint 与当前 validation fingerprint 不一致："
                "证据在 Preview 之后已变化（TOCTOU），拒绝 Apply"
            )
        working = deepcopy(self.session.state)
        original = self.session.state
        self.session.state = working
        try:
            applied = self._publish_all(projections, evidence_source)
            installed = self._normalize(working)
        except Exception:
            self.session.state = original
            raise
        # The service graph binds ``session.state`` by reference, so the normalized
        # result must be installed *in place* before saving.
        installed = _install_in_place(original, installed)
        self.session.state = installed
        self.session.save()
        return {
            "status": "passed",
            "adoption_ids": [item["adoption_id"] for item in applied],
            "route_ids": [item["route_id"] for item in applied],
            "count": len(applied),
            "downstream_invalidation": list(V3_DOWNSTREAM_RESULTS),
            "operational_route_ids": sorted(
                str(item.get("route_id"))
                for item in self.session.state.get("operational_routes") or []
            ),
            "v3c_validation_history_unchanged": True,
            "snapshot": self.snapshot(),
        }

    def _publish_all(self, projections, evidence_source):
        state = self.session.state
        collection = normalize_v3_operational_adoptions(state.get("v3_operational_adoptions"))
        items = collection["items"]
        routes = list(state.get("operational_routes") or [])
        spatial = state.setdefault("spatial_3d", {})
        profiles = spatial.setdefault("route_altitude_profiles", {})
        applied, published_ids = [], set()
        for item in projections:
            entry, projection = item["entry"], item["projection"]
            route_id = projection["route_id"]
            previous_route = next(
                (route for route in routes if str(route.get("route_id")) == route_id), None,
            )
            routes = [route for route in routes if str(route.get("route_id")) != route_id]
            routes.append(deepcopy(projection["route"]))
            previous_profile = profiles.get(route_id)
            profiles[route_id] = deepcopy(projection["profile"])
            previous_adoption = next(
                (adoption for adoption in items if str(adoption.get("route_id")) == route_id),
                None,
            )
            adoption = self._adoption_record(
                entry, projection, evidence_source,
                previous_route=previous_route, previous_profile=previous_profile,
                previous_adoption=previous_adoption,
            )
            items = [
                other for other in items if str(other.get("route_id")) != route_id
            ]
            items.append(adoption)
            published_ids.add(route_id)
            applied.append(adoption)
        # Upsert only the adopted route_ids: unrelated operational routes are untouched.
        routes.sort(key=lambda route: str(route.get("route_id")))
        state["operational_routes"] = routes
        state["spatial_3d"]["route_altitude_profiles"] = profiles
        collection["items"] = items
        collection["count"] = len(items)
        collection["status"] = "passed"
        state["v3_operational_adoptions"] = collection
        state.setdefault("result_statuses", {})["routes"] = "passed"
        self._propagate_publish(published_ids)
        return applied

    def _adoption_record(self, entry, projection, evidence_source, *, previous_route,
                         previous_profile, previous_adoption):
        route_id = projection["route_id"]
        validation_fingerprint = _validation_fingerprint(entry)
        identity = adoption_fingerprint({
            "route_id": route_id,
            "validation_ids": [str(entry.get("validation_id"))],
            "validation_fingerprints": {str(entry.get("validation_id")): validation_fingerprint},
            "refinement_fingerprint": (
                entry.get("refinement_fingerprint")
                or (entry.get("result") or {}).get("refinement_fingerprint")
            ),
            "projection_fingerprint": projection["projection_fingerprint"],
            "evidence_source": evidence_source,
        })
        return {
            "schema_version": ADOPTION_SCHEMA_VERSION,
            "adoption_id": "V3D-" + identity[-12:].upper(),
            "route_id": route_id,
            "status": "published",
            "current_applicability": "current",
            "applied_at": utc_now(),
            "experiment_id": entry.get("experiment_id"),
            "refinement_id": entry.get("refinement_id"),
            "validation_ids": [str(entry.get("validation_id"))],
            "validation_fingerprints": {str(entry.get("validation_id")): validation_fingerprint},
            "refinement_fingerprint": (
                entry.get("refinement_fingerprint")
                or (entry.get("result") or {}).get("refinement_fingerprint")
            ),
            "projection_fingerprint": projection["projection_fingerprint"],
            "evidence_source": evidence_source,
            "route_provenance": deepcopy((projection["route"] or {}).get("provenance") or {}),
            "profile": deepcopy(projection["profile"]),
            "path_metrics": deepcopy(projection["path_metrics"]),
            "compatibility": deepcopy(projection["compatibility"]),
            "before": {
                "present": previous_route is not None,
                "route": _route_summary(previous_route),
                "profile_source": (previous_profile or {}).get("source"),
                "adoption_id": (previous_adoption or {}).get("adoption_id"),
            },
            "after": {
                "present": True,
                "route": _route_summary(projection["route"]),
                "profile_source": ROUTE_SOURCE_TYPE,
                "profile_locked": True,
            },
            "cns_assessment": {
                "bundle_id": None, "assessment_status": "not_started",
                "requirement_verdict": "unknown",
            },
            "stale_reason": None,
            "provenance": {
                "adopted_by": "V3OperationalAdoptionService.apply",
                "algorithm_id": self.algorithm_id,
                "algorithm_version": self.algorithm_version,
                "model_scope": self.model_scope,
                "v3c_validation_history_untouched": True,
                "route_safety_and_cns_compliance_are_separate": True,
                "cns_excluded_from_route_cost": True,
                "invalidation_channel": V3_PUBLISH_REASON,
            },
            "note": V3D_DISCLAIMER,
        }

    def _propagate_publish(self, published_ids):
        """Dedicated propagation: never ``workflow("route")``, never stale the new route."""

        state = self.session.state
        statuses = state.setdefault("result_statuses", {})
        for name in _STALE_STATUS_KEYS:
            if name in statuses and statuses[name] != "not_calculated":
                statuses[name] = "stale"
        for key, status_key in _STALE_RESULTS:
            result = state.get(key)
            if isinstance(result, dict) and result.get("status") not in (None, "not_calculated"):
                result["status"] = "stale"
                result["stale_reason"] = V3_PUBLISH_REASON
        from ..domain.reporting import mark_active_report_stale
        mark_active_report_stale(state, V3_PUBLISH_REASON)
        # The published routes stay passed; every V3 result is untouched.
        for route in state.get("operational_routes") or []:
            if str(route.get("route_id")) in published_ids:
                route["status"] = ROUTE_STATUS
                route.pop("stale_reason", None)

    # ------------------------------------------------------------------ revoke

    def revoke(self, payload=None):
        """Remove one adoption and only the route/profile it still owns."""

        payload = payload if isinstance(payload, dict) else {}
        if payload.get("confirmed") is not True:
            raise ValueError("V3-D Revoke 必须显式 confirmed=true")
        adoption_id = str(payload.get("adoption_id") or "")
        route_id = str(payload.get("route_id") or "")
        collection = normalize_v3_operational_adoptions(
            self.session.state.get("v3_operational_adoptions")
        )
        target = None
        for item in collection["items"]:
            if adoption_id and str(item.get("adoption_id")) != adoption_id:
                continue
            if route_id and str(item.get("route_id")) != route_id:
                continue
            if item.get("status") == "revoked":
                continue
            target = item
            break
        if target is None:
            raise ValueError("V3 operational adoption 不存在或已撤销")
        working = deepcopy(self.session.state)
        original = self.session.state
        self.session.state = working
        try:
            outcome = self._revoke_in_working_copy(target)
            installed = self._normalize(working)
        except Exception:
            self.session.state = original
            raise
        installed = _install_in_place(original, installed)
        self.session.state = installed
        self.session.save()
        return {**outcome, "snapshot": self.snapshot()}

    def _revoke_in_working_copy(self, target):
        state = self.session.state
        route_id = str(target.get("route_id"))
        route = next(
            (item for item in state.get("operational_routes") or []
             if str(item.get("route_id")) == route_id), None,
        )
        removed_route = False
        if route is not None and self._route_is_owned_by(route, target):
            state["operational_routes"] = [
                item for item in state.get("operational_routes") or []
                if str(item.get("route_id")) != route_id
            ]
            removed_route = True
        profiles = (state.get("spatial_3d") or {}).get("route_altitude_profiles") or {}
        profile = profiles.get(route_id)
        removed_profile = False
        if profile is not None and self._profile_is_owned_by(profile, target):
            state["spatial_3d"]["route_altitude_profiles"].pop(route_id, None)
            removed_profile = True
        collection = normalize_v3_operational_adoptions(state.get("v3_operational_adoptions"))
        retained = []
        for item in collection["items"]:
            if str(item.get("adoption_id")) != str(target.get("adoption_id")):
                retained.append(item)
                continue
            revoked = deepcopy(item)
            revoked["status"] = "revoked"
            revoked["current_applicability"] = "revoked"
            revoked["stale_reason"] = "revoked_by_user"
            revoked["provenance"] = {
                **(revoked.get("provenance") or {}),
                "revoked_at": utc_now(),
                "revoke_removed_route": removed_route,
                "revoke_removed_profile": removed_profile,
                "foreign_routes_preserved": True,
            }
            retained.append(revoked)
        collection["items"] = retained
        collection["count"] = len(retained)
        state["v3_operational_adoptions"] = collection
        bundles = state.get("v3_cns_assessment_bundle") or {}
        bundles["items"] = [
            item for item in bundles.get("items") or []
            if str(item.get("adoption_id")) != str(target.get("adoption_id"))
        ]
        bundles["count"] = len(bundles["items"])
        state["v3_cns_assessment_bundle"] = bundles
        self._propagate_publish({route_id} if removed_route else set())
        return {
            "status": "passed",
            "adoption_id": target.get("adoption_id"),
            "route_id": route_id,
            "removed_route": removed_route,
            "removed_profile": removed_profile,
            "preserved_foreign_routes": True,
            "downstream_invalidation": list(V3_DOWNSTREAM_RESULTS),
            "v3c_validation_history_unchanged": True,
        }

    def _route_is_owned_by(self, route, adoption):
        provenance = route.get("provenance") or {}
        expected = {
            str(item) for item in adoption.get("validation_ids") or []
        }
        recorded = {
            str(key): str(value)
            for key, value in (adoption.get("validation_fingerprints") or {}).items()
        }
        return (
            provenance.get("source_type") == ROUTE_SOURCE_TYPE
            and str(provenance.get("validation_id") or "") in expected
            and str(provenance.get("validation_fingerprint") or "")
            == recorded.get(str(provenance.get("validation_id") or ""))
        )

    def _profile_is_owned_by(self, profile, adoption):
        return (
            profile.get("source") == ROUTE_SOURCE_TYPE
            and bool(profile.get("locked_by_adoption"))
            and str(profile.get("route_id")) == str(adoption.get("route_id"))
        )

    # ------------------------------------------------------------------ applicability

    def adoption_applicability(self, adoption):
        """Whether a stored adoption's evidence is still current."""

        reasons = []
        if adoption.get("status") == "revoked":
            return {"status": "revoked", "reasons": ["revoked_by_user"]}
        fingerprints = adoption.get("validation_fingerprints") or {}
        index = self.validation_index()
        current = self.current_validation_status()
        for validation_id, recorded in fingerprints.items():
            entry = index.get(str(validation_id))
            if entry is None:
                reasons.append(f"validation_missing:{validation_id}")
                continue
            if _validation_fingerprint(entry) != str(recorded):
                reasons.append(f"validation_fingerprint_changed:{validation_id}")
            status = current.get(str(validation_id))
            if status is None:
                reasons.append(f"validation_not_current:{validation_id}")
            elif status.get("current_applicability") != "current":
                reasons.append(f"validation_stale:{validation_id}")
        if adoption.get("evidence_source") == PRODUCTION_EVIDENCE_SOURCE:
            readiness = self.v3_service.real_data_readiness()
            if (readiness.get("v3c") or {}).get("status") != "ready":
                reasons.append("configured_real_source_chain_not_ready")
        route = next(
            (item for item in self.session.state.get("operational_routes") or []
             if str(item.get("route_id")) == str(adoption.get("route_id"))), None,
        )
        if route is None or route.get("status") != ROUTE_STATUS:
            reasons.append("adopted_operational_route_missing_or_not_passed")
        return {"status": "current" if not reasons else "stale", "reasons": reasons}

    def ownership(self, adoption):
        route = next(
            (item for item in self.session.state.get("operational_routes") or []
             if str(item.get("route_id")) == str(adoption.get("route_id"))), None,
        )
        profiles = (self.session.state.get("spatial_3d") or {}).get("route_altitude_profiles") or {}
        profile = profiles.get(str(adoption.get("route_id")))
        return {
            "route_owned": bool(route is not None and self._route_is_owned_by(route, adoption)),
            "profile_owned": bool(profile is not None and self._profile_is_owned_by(profile, adoption)),
            "revocable": adoption.get("status") != "revoked",
        }

    def stale_for_sources(self, changed_sources):
        """Mark V3 adoptions/adopted routes stale on their tracked source changes.

        Only V3 adoptees are touched: a V1/V2 operational route is never affected by a
        V3 source change.
        """

        relevant = sorted(set(changed_sources or []) & {
            "terrain_dtm", "buildings", "building_grid",
        })
        if not relevant:
            return {"status": "not_applicable", "stale_adoption_ids": [], "stale_route_ids": []}
        collection = normalize_v3_operational_adoptions(
            self.session.state.get("v3_operational_adoptions")
        )
        stale_ids, owned = [], set()
        reason = "v3_source_changed:" + ",".join(relevant)
        for item in collection["items"]:
            if item.get("status") == "revoked":
                continue
            item["status"] = "stale"
            item["current_applicability"] = "stale"
            item["stale_reason"] = reason
            stale_ids.append(item.get("adoption_id"))
            owned.add(str(item.get("route_id")))
        self.session.state["v3_operational_adoptions"] = collection
        for route in self.session.state.get("operational_routes") or []:
            if str(route.get("route_id")) in owned:
                route["status"] = "stale"
                route["stale_reason"] = reason
        if stale_ids:
            self._propagate_publish(set())
        return {
            "status": "passed", "stale_adoption_ids": stale_ids,
            "stale_route_ids": sorted(owned), "reason": reason,
        }

    # ------------------------------------------------------------------ CNS bridge

    def cns_bundle_snapshot(self):
        bundles = self.session.state.get("v3_cns_assessment_bundle") or {}
        items = [normalize_v3_cns_assessment_bundle(item) for item in bundles.get("items") or []]
        return {
            "schema_version": bundles.get("schema_version"),
            "status": "passed" if items else "not_calculated",
            "count": len(items),
            "items": items,
            "stage_order": list(CNS_STAGES),
            "complete_count": sum(1 for item in items if item["assessment_status"] == "complete"),
            "verdict_counts": {
                verdict: sum(1 for item in items if item["requirement_verdict"] == verdict)
                for verdict in ("meets", "does_not_meet", "unknown")
            },
            "semantics": empty_v3_cns_assessment_bundle()["semantics"],
            "note": V3D_DISCLAIMER,
        }

    def assess_route(self, payload=None):
        """Run the existing P7→P8→P9→P10 chain for one adopted V3 route.

        Missing prerequisites yield ``incomplete`` plus a blocking reason -- no default is
        ever invented.  The V3-C validation history is read, never written.
        """

        payload = payload if isinstance(payload, dict) else {}
        adoption = self._selected_adoption(payload)
        if adoption is None:
            raise ValueError("没有可评估的 V3 operational adoption")
        route_id = str(adoption.get("route_id") or "")
        route = next(
            (item for item in self.session.state.get("operational_routes") or []
             if str(item.get("route_id")) == route_id), None,
        )
        bundle = empty_v3_cns_assessment_bundle()
        bundle.update({
            "route_id": route_id,
            "adoption_id": adoption.get("adoption_id"),
            "validation_id": (adoption.get("validation_ids") or [None])[0],
            "requested_stages": self._requested_stages(payload),
            "computed_at": utc_now(),
            "provenance": {
                "bridge": "existing_p7_p8_p9_p10_orchestration",
                "services": [
                    "Spatial3DService.evaluate", "CNSServiceCapabilityService.evaluate",
                    "OperationalTimingService.evaluate_timeline", "GapAnalysisV2Service.evaluate",
                ],
                "no_formula_copied": True,
                "cns_excluded_from_route_cost": True,
                "v3c_validation_history_untouched": True,
            },
        })
        applicability = self.adoption_applicability(adoption)
        if applicability["status"] != "current":
            bundle["assessment_status"] = "stale"
            bundle["blocking_reasons"] = list(applicability["reasons"]) or ["adoption_is_stale"]
        elif route is None or route.get("status") != ROUTE_STATUS:
            bundle["assessment_status"] = "incomplete"
            bundle["blocking_reasons"] = ["adopted_operational_route_missing_or_not_passed"]
        else:
            prerequisite = self._stage_prerequisites()
            bundle["stage_results"] = prerequisite["stage_results"]
            bundle["blocking_reasons"] = prerequisite["blocking_reasons"]
            if prerequisite["blocking_reasons"]:
                bundle["assessment_status"] = "incomplete"
            else:
                self._run_stages(bundle)
                bundle["assessment_status"] = self._assessment_status(bundle)
        bundle["route_validation_status"] = self._stored_validation_status(
            adoption.get("validation_ids") or []
        )
        bundle["route_validation_unchanged"] = True
        bundle["requirement_verdict"] = self._requirement_verdict(bundle)
        bundle["assessment_fingerprint"] = bundle_fingerprint(bundle)
        bundle["bundle_id"] = "V3CNS-" + (bundle["assessment_fingerprint"] or "")[:12].upper()
        bundle = normalize_v3_cns_assessment_bundle(bundle)
        self._link_bundle(adoption, bundle)
        self.session.save()
        return deepcopy(bundle)

    def _requested_stages(self, payload):
        requested = payload.get("stages") if isinstance(payload, dict) else None
        if requested in (None, "", []):
            return list(CNS_STAGES)
        values = {str(item) for item in requested}
        return [name for name in CNS_STAGES if name in values]

    def _stage_prerequisites(self):
        """Check each stage's *real* prerequisites before running any of them."""

        state = self.session.state
        stage_results = {
            name: {"stage": name, "status": "not_run", "reason": None, "fingerprint": None,
                   "route_statuses": {}, "evidence": {}}
            for name in CNS_STAGES
        }
        blocking = []
        routes = [
            item for item in state.get("operational_routes") or []
            if item.get("status") == ROUTE_STATUS
        ]
        if not routes:
            blocking.append("no_passed_operational_route")
        if not (state.get("spatial_3d") or {}).get("route_altitude_profiles"):
            blocking.append("missing_route_altitude_profile")
        if not _has_resolved_requirement(state.get("required_cns") or {}):
            blocking.append("missing_required_cns")
        if _selected_aircraft_profile(state) is None:
            blocking.append("missing_selected_aircraft_profile")
        if not ((state.get("existing_cns_facilities") or {}).get("items")):
            blocking.append("missing_existing_cns_facilities")
        if blocking:
            for name in CNS_STAGES:
                stage_results[name]["reason"] = "prerequisites_incomplete"
        return {"stage_results": stage_results, "blocking_reasons": blocking}

    def _run_stages(self, bundle):
        """Reuse the existing services in order; never reimplement their formulas.

        Each ``evaluate()`` persists its own canonical result and returns a workflow
        snapshot, so the stage entry is read back from the persisted container rather
        than from the return value.
        """

        stages = bundle["stage_results"]
        requested = set(bundle.get("requested_stages") or CNS_STAGES)
        if self.spatial_3d_service is None:
            bundle["blocking_reasons"] = ["p7_service_unavailable"]
            return
        self.spatial_3d_service.evaluate()
        coverage = self.session.state.get("coverage_3d") or {}
        route_statuses = _required_subsystem_statuses(coverage, self.session.state)
        stages["P7"] = _stage_entry("P7", coverage, "coverage_3d",
                                    route_statuses=route_statuses)
        if "P7" not in requested:
            bundle["blocking_reasons"] = ["p7_stage_not_requested"]
            return
        if any(value != "passed" for value in route_statuses.values()):
            bundle["blocking_reasons"] = ["p7_geometric_coverage_not_passed"]
            return
        if self.cns_service_capability_service is None:
            bundle["blocking_reasons"] = ["p8_service_unavailable"]
            return
        self.cns_service_capability_service.evaluate()
        capability = self.session.state.get("cns_service_capability") or {}
        stages["P8"] = _stage_entry(
            "P8", capability, "cns_service_capability",
            route_statuses=_required_capability_statuses(capability, self.session.state),
        )
        if "P8" not in requested:
            bundle["blocking_reasons"] = ["p8_stage_not_requested"]
            return
        if self.operational_timing_service is None:
            bundle["blocking_reasons"] = ["p9_service_unavailable"]
            return
        self.operational_timing_service.evaluate_timeline()
        timeline = self.session.state.get("service_timeline") or {}
        stages["P9"] = _stage_entry(
            "P9", timeline, "service_timeline",
            route_statuses=_required_timeline_statuses(timeline, self.session.state),
        )
        if "P9" not in requested:
            bundle["blocking_reasons"] = ["p9_stage_not_requested"]
            return
        if self.gap_analysis_v2_service is None:
            bundle["blocking_reasons"] = ["p10_service_unavailable"]
            return
        self.gap_analysis_v2_service.evaluate()
        gap = self.session.state.get("cns_gap_analysis_v2") or {}
        stages["P10"] = _stage_entry(
            "P10", gap, "cns_gap_v2",
            route_statuses=_required_gap_statuses(gap, self.session.state),
        )
        if "P10" not in requested:
            bundle["blocking_reasons"] = ["p10_stage_not_requested"]

    def _assessment_status(self, bundle):
        """Completeness of the CNS evidence -- never the requirement verdict.

        Completeness means: every requested stage ran, and every **required subsystem**
        reached a determinate verdict at that stage.  A subsystem the project does not
        require may legitimately be missing/unresolved without making the assessment
        look incomplete, which is exactly why the per-stage route statuses are computed
        over the required subsystems only.
        """

        if bundle.get("blocking_reasons"):
            return "incomplete"
        stages = bundle.get("stage_results") or {}
        for name in bundle.get("requested_stages") or CNS_STAGES:
            entry = stages.get(name) or {}
            if str(entry.get("status") or "not_run") in ("not_run", "not_calculated"):
                return "incomplete"
            route_statuses = entry.get("route_statuses") or {}
            if not route_statuses:
                return "incomplete"
            if any(_is_undetermined(value) for value in route_statuses.values()):
                return "incomplete"
        return "complete"

    def _requirement_verdict(self, bundle):
        """Requirement satisfaction, orthogonal to assessment completeness."""

        if bundle.get("assessment_status") in ("not_started", "incomplete", "stale"):
            return "unknown"
        signals = _verdict_signals(bundle)
        if "does_not_meet" in signals:
            return "does_not_meet"
        if "unknown" in signals:
            return "unknown"
        return "meets"

    def _stored_validation_status(self, validation_ids):
        """The stored V3-C verdict, read only (never rewritten by V3-D)."""

        index = self.validation_index()
        for validation_id in validation_ids or []:
            entry = index.get(str(validation_id))
            if entry is not None:
                return entry["result"].get("status")
        return None

    def _link_bundle(self, adoption, bundle):
        collection = self.session.state.setdefault("v3_cns_assessment_bundle", {
            "schema_version": "3.3-cns-assessment-bundle-collection",
            "status": "not_calculated", "count": 0, "items": [],
        })
        retained = [
            item for item in collection.get("items") or []
            if str(item.get("adoption_id")) != str(adoption.get("adoption_id"))
        ]
        collection["items"] = [deepcopy(bundle), *retained][:20]
        collection["count"] = len(collection["items"])
        collection["status"] = "passed"
        for item in (self.session.state.get("v3_operational_adoptions") or {}).get("items") or []:
            if str(item.get("adoption_id")) != str(adoption.get("adoption_id")):
                continue
            item["cns_assessment"] = {
                "bundle_id": bundle.get("bundle_id"),
                "assessment_status": bundle.get("assessment_status"),
                "requirement_verdict": bundle.get("requirement_verdict"),
                "computed_at": bundle.get("computed_at"),
            }

    def _selected_adoption(self, payload):
        collection = normalize_v3_operational_adoptions(
            self.session.state.get("v3_operational_adoptions")
        )
        adoption_id = str(payload.get("adoption_id") or "")
        route_id = str(payload.get("route_id") or "")
        for item in collection["items"]:
            if adoption_id and str(item.get("adoption_id")) != adoption_id:
                continue
            if route_id and str(item.get("route_id")) != route_id:
                continue
            if item.get("status") == "revoked":
                continue
            return item
        return None

    # ------------------------------------------------------------------ internals

    def _scenario_route(self, route_id):
        for route in self.session.state.get("scenario_routes") or []:
            if str(route.get("route_id")) == str(route_id):
                return route
        return None

    def _realization_for(self, entry):
        result = entry.get("result") or {}
        route = result.get("continuous_route")
        if not isinstance(route, dict) or not route:
            return None
        return {"route": route, "frame": result.get("frame") or {}}

    def _resolve_transform(self, realization):
        """The recorded metric → OGC:CRS84 transform, or ``None`` (never invented)."""

        if self.metric_transform is not None:
            return _normalize_transform(self.metric_transform)
        crs = ((realization or {}).get("frame") or {}).get("horizontal_crs")
        if not crs:
            return None
        if self.transform_resolver is not None:
            try:
                return _normalize_transform(self.transform_resolver(str(crs)))
            except Exception:
                return None
        return _library_transform(crs)

    def _normalize(self, state):
        from .project_state import normalize_project
        if self._grid_service is None:
            from ..algorithms.grid.service import WorkspaceGridService
            self._grid_service = WorkspaceGridService()
        return normalize_project(state, self._grid_service)


# --------------------------------------------------------------------------- helpers


def _install_in_place(target, replacement):
    """Replace a state dict's contents without changing its identity.

    ``WorkflowService`` and every service hold ``session.state`` by reference, so a
    normalized result must be installed in place to stay visible everywhere.
    """

    target.clear()
    target.update(replacement)
    return target


def _seal_preview(preview):
    preview["preview_fingerprint"] = stable_fingerprint({
        "evidence_source": preview.get("evidence_source"),
        "status": preview.get("status"),
        "route_ids": preview.get("route_ids") or [],
        "projection_fingerprints": [
            item.get("projection_fingerprint") for item in preview.get("projections") or []
        ],
        "blocked": preview.get("blocked") or [],
    }, prefix="V3DPREV-")
    preview["preview_id"] = "V3DPREV-" + preview["preview_fingerprint"][-20:]
    # A preview never writes and never runs CNS.
    preview["operational_routes_untouched"] = True
    preview["spatial_3d_untouched"] = True
    preview["cns_not_run"] = True
    return preview


def _metric_line(route):
    linearized = ((route or {}).get("horizontal_geometry") or {}).get("linearized") or {}
    points = []
    for raw in linearized.get("linestring_metric") or []:
        if not isinstance(raw, (list, tuple)) or len(raw) < 2:
            continue
        point = [float(raw[0]), float(raw[1])]
        if not points or point != points[-1]:
            points.append(point)
    return points


def _polyline_length(points):
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(points, points[1:]))


def _geodesic_length(path):
    """WGS84 geodesic length of a lon/lat path (the published representation)."""

    from ..benchmark.geodesy import geodesic_distance_m
    return sum(
        geodesic_distance_m(list(a), list(b)) for a, b in zip(path, path[1:])
    )


def _cumulative_baseline_distance(path):
    cumulative = [0.0]
    for left, right in zip(path, path[1:]):
        cumulative.append(cumulative[-1] + math.hypot(
            float(right[0]) - float(left[0]), float(right[1]) - float(left[1]),
        ))
    return cumulative


def _project_path(metric_line, transform):
    path = []
    for point in metric_line:
        converted = transform.to_geographic(point)
        if converted is None:
            return [], False
        path.append([
            round(float(converted[0]), _COORDINATE_DIGITS),
            round(float(converted[1]), _COORDINATE_DIGITS),
        ])
    return path, len(path) >= 2


def _dedupe_published_path(path, metric_line, z_values):
    """Collapse published vertices that rounding made coincident.

    The published profile is generated from the *deduped* vertex list with the path's own
    cumulative distance, so path and profile stay one-to-one by construction.
    """

    unique_path, unique_metric, unique_z = [], [], []
    for index, point in enumerate(path):
        if unique_path and point == unique_path[-1]:
            continue
        unique_path.append(point)
        unique_metric.append(metric_line[index] if index < len(metric_line) else point)
        unique_z.append(z_values[index] if index < len(z_values) else None)
    return unique_path, unique_metric, unique_z


def _interpolate_known(ratio, known):
    if not known:
        return None
    if ratio <= known[0][0]:
        return known[0][1]
    if ratio >= known[-1][0]:
        return known[-1][1]
    for (left_distance, left_value), (right_distance, right_value) in zip(known, known[1:]):
        if ratio <= right_distance:
            span = right_distance - left_distance
            if span <= 0:
                return left_value
            value = (ratio - left_distance) / span
            return left_value + (right_value - left_value) * value
    return known[-1][1]


def _points_close(expected, actual, tolerance=1e-9):
    if not isinstance(expected, (list, tuple)) or not isinstance(actual, (list, tuple)):
        return False
    if len(expected) < 2 or len(actual) < 2:
        return False
    return (
        abs(float(expected[0]) - float(actual[0])) <= tolerance
        and abs(float(expected[1]) - float(actual[1])) <= tolerance
    )


def _route_summary(route):
    if not isinstance(route, dict):
        return None
    provenance = route.get("provenance") or {}
    return {
        "route_id": route.get("route_id"),
        "status": route.get("status"),
        "kind": route.get("kind"),
        "vertex_count": len(route.get("path") or []),
        "source_type": provenance.get("source_type"),
        "algorithm_id": route.get("algorithm_id"),
        "algorithm_version": route.get("algorithm_version"),
        "validation_id": provenance.get("validation_id"),
    }


def _validation_fingerprint(entry):
    result = entry.get("result") or {}
    validation = entry.get("validation") or {}
    return str(
        result.get("validation_fingerprint")
        or validation.get("validation_fingerprint")
        or entry.get("validation_id")
    )


def _normalize_transform(adapter):
    if adapter is None:
        return None
    if not callable(getattr(adapter, "to_geographic", None)):
        return None

    class _RecordedTransform:
        def __init__(self, source):
            self._source = source

        def to_geographic(self, point):
            try:
                return self._source.to_geographic(point)
            except Exception:
                return None

        def describe(self):
            try:
                described = self._source.describe()
            except Exception:
                described = {}
            described = dict(described) if isinstance(described, dict) else {}
            described.setdefault("target_crs", PATH_CRS)
            described.setdefault("geodetic", True)
            described.setdefault("transform_id", LIBRARY_TRANSFORM_ID)
            return described

    return _RecordedTransform(adapter)


def _library_transform(crs):
    """A real CRS transform from the recorded projected CRS (pyproj).

    There is no silent identity fallback: an unusable CRS yields ``None`` and the publish
    gate fails closed with ``crs_transform_unavailable``.
    """

    authority = str(crs or "")
    if not authority or authority.startswith("synthetic:"):
        return None
    try:
        from pyproj import Transformer
    except ImportError:
        return None
    try:
        transformer = Transformer.from_crs(authority, "OGC:CRS84", always_xy=True)
    except Exception:
        return None

    class _PyprojTransform:
        def to_geographic(self, point):
            try:
                x, y = transformer.transform(float(point[0]), float(point[1]))
            except Exception:
                return None
            if x != x or y != y:
                return None
            return [float(x), float(y)]

        def describe(self):
            return {
                "method": "explicit_projected_crs_to_ogc_crs84",
                "authority": authority,
                "target_crs": PATH_CRS,
                "geodetic": True,
                "transform_id": LIBRARY_TRANSFORM_ID,
            }

    return _PyprojTransform()


def _has_resolved_requirement(required):
    """At least one confirmed requirement the CNS chain can actually assess.

    ``required_cns`` is a scoped contract (``project_default`` plus per-route
    overrides), so the requirement must be read from the scope, never from the top
    level of the container.
    """

    if not isinstance(required, dict):
        return False
    scopes = [required.get("project_default")]
    scopes.extend((required.get("route_overrides") or {}).values())
    for scope in scopes:
        if not isinstance(scope, dict):
            continue
        for code in ("communication", "navigation", "surveillance"):
            entry = scope.get(code)
            if (isinstance(entry, dict) and entry.get("required") is True
                    and entry.get("confirmed") is True):
                return True
    return False


def _selected_aircraft_profile(state):
    from ..catalogs import AircraftCNSProfileCatalog
    return AircraftCNSProfileCatalog.find(
        state.get("aircraft_profiles") or {},
        state.get("selected_aircraft_profile_id") or "",
    )


def _stage_entry(stage, result, status_key, *, route_statuses=None):
    result = result or {}
    return {
        "stage": stage,
        "status": str(result.get("status") or "unknown"),
        "reason": result.get("message") or result.get("reason"),
        "fingerprint": result.get("input_fingerprint"),
        "algorithm_id": result.get("algorithm_id"),
        "algorithm_version": result.get("algorithm_version"),
        "route_statuses": deepcopy(route_statuses or {}),
        "evidence": {
            "result_status_key": status_key,
            "route_count": result.get("route_count"),
        },
    }


def _route_statuses(result, key):
    return {
        str(item.get("route_id")): item.get("status")
        for item in (result or {}).get(key) or []
    }


_SUBSYSTEM_KEYS = {"C": "communication", "N": "navigation", "S": "surveillance"}


def _required_subsystem_statuses(coverage, state):
    """Per-route coverage status over the *required* subsystems only.

    P7 assesses all three C/N/S subsystems, but a subsystem the project does not
    require may legitimately be ``missing_data`` (no site provider at all).  Requiring
    it would block the bridge on an unrequired domain, so the pass criterion is taken
    over the confirmed required requirements -- and only those.
    """

    scopes = _requirement_scopes(state.get("required_cns") or {})
    result = {}
    for route in (coverage or {}).get("routes") or []:
        route_id = str(route.get("route_id"))
        required = {
            code for code, key in _SUBSYSTEM_KEYS.items()
            if any(_scope_requires(scope, key) for scope in scopes)
        }
        statuses = [
            str(item.get("status"))
            for item in route.get("subsystems") or []
            if str(item.get("subsystem")) in required
        ]
        result[route_id] = (
            "passed" if statuses and all(value == "passed" for value in statuses)
            else "failed" if any(value == "failed" for value in statuses)
            else "missing_data" if not statuses or any(
                value in ("missing_data", "unresolved") for value in statuses
            )
            else "unknown"
        )
        if not required:
            result[route_id] = str(route.get("status") or "missing_data")
    return result


def _requirement_scopes(required):
    scopes = [required.get("project_default")]
    scopes.extend((required.get("route_overrides") or {}).values())
    return [scope for scope in scopes if isinstance(scope, dict)]


def _scope_requires(scope, key):
    entry = scope.get(key)
    return (
        isinstance(entry, dict)
        and entry.get("required") is True
        and entry.get("confirmed") is True
    )


def _timeline_route_statuses(timeline):
    statuses = {}
    for route in (timeline or {}).get("routes") or []:
        subsystems = route.get("subsystems") or []
        states = [str(item.get("status")) for item in subsystems]
        statuses[str(route.get("route_id"))] = (
            "passed" if states and all(value == "passed" for value in states)
            else "failed" if any(value == "failed" for value in states)
            else "unknown"
        )
    return statuses


def _required_capability_statuses(capability, state):
    """P8 route status over the required subsystems only."""

    scopes = _requirement_scopes(state.get("required_cns") or {})
    result = {}
    for route in (capability or {}).get("routes") or []:
        statuses = _required_subsystem_states(
            route, scopes,
            lambda item: str((item.get("status") or "")),
        )
        result[str(route.get("route_id"))] = _collapse_states(statuses, {
            "does_not_meet_under_model": "does_not_meet_under_model",
            "meets_under_model": "meets_under_model",
            "not_applicable": "not_applicable",
        })
    return result


def _required_timeline_statuses(timeline, state):
    """P9 route status over the required subsystems only."""

    scopes = _requirement_scopes(state.get("required_cns") or {})
    result = {}
    for route in (timeline or {}).get("routes") or []:
        statuses = _required_subsystem_states(
            route, scopes, lambda item: str((item.get("status") or "")),
        )
        result[str(route.get("route_id"))] = _collapse_states(statuses, {
            "failed": "failed", "passed": "passed", "not_applicable": "not_applicable",
        })
    return result


def _required_gap_statuses(gap, state):
    """P10 route status over the required subsystems only.

    Gap V2 exposes its combined verdict on each subsystem (with the per-interval detail
    on ``segments``), so the subsystem status is read directly instead of being
    re-derived from the segments.
    """

    scopes = _requirement_scopes(state.get("required_cns") or {})
    result = {}
    for route in (gap or {}).get("routes") or []:
        statuses = _required_subsystem_states(
            route, scopes, lambda item: str((item.get("status") or "")),
        )
        result[str(route.get("route_id"))] = _collapse_states(statuses, {
            "confirmed_gap": "confirmed_gap",
            "satisfied": "satisfied",
            "satisfied_by_contingency": "satisfied",
            "not_applicable": "not_applicable",
        })
    return result


def _required_subsystem_states(route, scopes, reader):
    required = {
        code for code, key in _SUBSYSTEM_KEYS.items()
        if any(_scope_requires(scope, key) for scope in scopes)
    }
    return [
        reader(item) for item in route.get("subsystems") or []
        if str(item.get("subsystem")) in required
    ]


def _collapse_states(states, mapping):
    if not states:
        return "not_applicable"
    mapped = [mapping.get(value, "unknown") for value in states]
    for preferred in ("failed", "does_not_meet_under_model", "confirmed_gap", "unknown"):
        if preferred in mapped:
            return preferred
    if all(value == "not_applicable" for value in mapped):
        return "not_applicable"
    if all(value in ("passed", "meets_under_model", "satisfied") for value in mapped):
        return mapped[0]
    return "unknown"


def _gap_route_statuses(gap):
    statuses = {}
    for route in (gap or {}).get("routes") or []:
        subsystems = route.get("subsystems") or []
        combined = [
            str((item.get("combined") or {}).get("status"))
            for item in subsystems
        ]
        statuses[str(route.get("route_id"))] = (
            "confirmed_gap" if "confirmed_gap" in combined
            else "satisfied" if combined and all(value == "satisfied" for value in combined)
            else "unknown"
        )
    return statuses


def _verdict_signals(bundle):
    """Requirement satisfaction, read from the bundle's **required-subsystem** statuses.

    Route safety and CNS compliance are separate: these signals are never written back
    into the route validation, and an unrequired subsystem's gap is never a verdict.
    """

    signals = []
    stages = bundle.get("stage_results") or {}
    for name in ("P7", "P8", "P9", "P10"):
        for value in (stages.get(name) or {}).get("route_statuses", {}).values():
            signal = _verdict_signal(str(value))
            if signal:
                signals.append(signal)
    return signals


#: Route-level stage statuses ⇒ requirement verdict signal (``None`` = no signal).
_VERDICT_BY_STATUS = {
    "does_not_meet_under_model": "does_not_meet",
    "failed": "does_not_meet",
    "confirmed_gap": "does_not_meet",
    "unsupported_model": "does_not_meet",
    "unknown": "unknown",
    "missing_data": "unknown",
    "unresolved": "unknown",
    "pending_confirmation": "unknown",
}


def _verdict_signal(status):
    return _VERDICT_BY_STATUS.get(status)


def _is_undetermined(status):
    return str(status) in ("unknown", "missing_data", "unresolved", "pending_confirmation", "")


__all__ = [
    "LIBRARY_TRANSFORM_ID", "V3_DOWNSTREAM_RESULTS", "V3_PUBLISH_REASON",
    "V3OperationalAdoptionService",
]
