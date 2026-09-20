"""Vertical Transition Continuous Validation V1 — application service.

An explicit, user-triggered, read-only orchestration boundary.  It resolves the current
production lineage of one published route, truncates the **original** operational polyline
by route distance, hands each transition phase to the configured real-source evidence
adapter and delegates every verdict to the unchanged production validators.

Strict separation of lifecycle objects (never collapsed)::

    LayeredRouteCandidate
      != LayeredRouteValidation (fixed cruise altitude only)
      != Route3DProfile (thin derivation, no verdict)
      != VerticalTransitionValidation (this artifact)
      != operational route

Nothing here is evaluated in the background: only an explicit ``evaluate`` writes a record.
Nothing here rewrites the stored ``Route3DProfile`` or ``LayeredRouteValidation``; the profile
keeps ``terminal_transition_validation = not_evaluated`` and the *projection* of the profile
reports the current transition verdict next to it.
"""

from __future__ import annotations

from copy import deepcopy
from math import hypot

from ..domain.route_3d_profile import CRUISE_VALIDATION_CURRENT
from ..domain.layered_route_validation import normalize_layered_route_validation_collection
from ..domain.route_safety_evidence_v2 import stable_fingerprint, utc_now
from ..domain.vertical_transition_validation import (
    ARTIFACT_TYPE, BOUNDARIES, CLEARANCE_SEMANTICS, GEOMETRY_INTERSECTION_THRESHOLD_M,
    SEMANTICS, TRANSITION_DOMAIN_IDS, TRANSITION_PHASE_IDS, TRUNCATION_SEMANTICS,
    VALIDATOR_VERSION, assemble_record,
    empty_vertical_transition_validation, empty_vertical_transition_validation_collection,
    evaluate_phase, metric_route_from_phase,
    normalize_vertical_transition_validation_collection,
    transition_fingerprint, transition_geometry, truncate_waypoints,
)

#: Where the explicit local metric CRS of one evaluation may come from during ``evaluate-real``.
#: It must be an *explicit* value: when neither the request nor the confirmed V3 fine policy
#: declares one, the evaluation stays ``not_ready``.
CRS_SOURCES = ("payload.horizontal_crs", "v3_fine_refinement_policy.horizontal_crs")

READINESS_SEMANTICS = {
    "passed_current_layered_operational_adoption_required": True,
    "current_passed_production_route_3d_profile_required": True,
    "existing_current_cruise_layered_route_validation_required": True,
    "explicit_local_metric_horizontal_crs_required": True,
    "verified_configured_real_fabdem_terrain_dtm_required": True,
    "verified_real_building_footprint_source_required": True,
    "missing_item_is_not_ready_and_never_zero": True,
    "nodata_or_unknown_is_never_zero": True,
    "no_default_clearance_is_invented": True,
    "explicit_user_evaluation_required": True,
    "automatic_evaluation": False,
}

#: The bounded downstream set of an explicit transition change.  Strictly downstream: a
#: transition change never stales a Theta* candidate, a RouteRiskProfile, a
#: LayeredRouteValidation or an adoption.
DOWNSTREAM_RESULTS = ("route_safety_evidence_v2", "report")

_VALIDATOR_VERSIONS = {
    "transition_geometry": VALIDATOR_VERSION,
    "terrain": "source_native_terrain_validator_v1@vertical_transition_clearance_zero",
    "building": "real_footprint_geometry_intersection_validator_v1@vertical_transition",
    "native_pixel_intervals": "native_pixel_interval_v1",
}


class VerticalTransitionValidationService:
    """Readiness + explicit evaluation + lifecycle of Vertical Transition Validation V1."""

    def __init__(self, session, invalidation, snapshot, *, layered_service=None,
                 validation_service=None, risk_profile_service=None, profile_service=None,
                 evidence_adapter=None):
        self.session = session
        self.invalidation = invalidation
        self.snapshot = snapshot
        self.layered = layered_service
        self.validations = validation_service
        self.risk_profiles = risk_profile_service
        self.profiles = profile_service
        self.evidence_adapter = evidence_adapter
        self.ensure_state()

    # ------------------------------------------------------------------ container

    def ensure_state(self):
        state = self.session.state
        state["vertical_transition_validations"] = (
            normalize_vertical_transition_validation_collection(
                state.get("vertical_transition_validations")
            )
        )
        state.setdefault("result_statuses", {}).setdefault(
            "vertical_transition_validation", "not_calculated",
        )
        return state

    def result_snapshot(self):
        state = self.ensure_state()
        collection = normalize_vertical_transition_validation_collection(
            state.get("vertical_transition_validations")
        )
        collection["items"] = [self._project_current(item) for item in collection["items"]]
        collection["count"] = len(collection["items"])
        collection["active_validation_id"] = next((
            item.get("validation_id") for item in reversed(collection["items"])
            if item.get("current_applicability") == "current"
        ), None)
        collection["artifact_type"] = ARTIFACT_TYPE
        collection["validator_version"] = VALIDATOR_VERSION
        collection["semantics"] = deepcopy(SEMANTICS)
        collection["boundaries"] = deepcopy(BOUNDARIES)
        return collection

    # ------------------------------------------------------------------ readiness

    def readiness_snapshot(self, payload=None):
        payload = payload if isinstance(payload, dict) else {}
        state = self.ensure_state()
        selection = self._resolve_route(payload)
        route = selection["route"]
        route_id = selection["route_id"]
        blockers = list(selection["blockers"])

        profile, profile_applicability = self._current_profile(route_id)
        if not route_id:
            blockers.append(_block("route_id_missing", "必须先存在已发布的运行航路（route_id）"))
        elif route is None:
            blockers.append(_block(
                "operational_route_missing", f"运行航路不存在：{route_id}",
            ))
        elif str(route.get("status")) != "passed":
            blockers.append(_block(
                "operational_route_not_passed",
                f"运行航路状态不是 passed（{route.get('status')}）：只接受 passed 航路",
            ))

        adoption = self._current_adoption(route_id)
        if route_id and adoption is None:
            blockers.append(_block(
                "current_layered_operational_adoption_missing",
                "必须先存在该航路的 passed/current layered operational adoption",
            ))

        if profile is None:
            blockers.append(_block(
                "current_route_3d_profile_missing",
                "必须先存在该航路的 current passed ProductionRoute3DProfile",
            ))
        elif profile.get("status") != "passed" or profile_applicability != "current":
            blockers.append(_block(
                "route_3d_profile_not_current",
                "ProductionRoute3DProfile 不是 passed/current："
                f"status={profile.get('status')} applicability={profile_applicability}",
            ))

        cruise = self._current_cruise_validation(route_id)
        if route_id and cruise is None:
            blockers.append(_block(
                "current_cruise_layered_route_validation_missing",
                "必须先存在该航路的 current LayeredRouteValidation（固定巡航高度连续验证）",
            ))

        horizontal_crs, crs_source = self._resolve_horizontal_crs(payload)
        if not horizontal_crs:
            blockers.append(_block(
                "horizontal_crs_missing",
                "必须显式提供局部米制 horizontal_crs（payload.horizontal_crs 或已确认的 "
                "V3 fine policy）；不猜默认投影",
            ))

        source_status = self._source_readiness()
        blockers.extend(source_status["blockers"])

        geometry = transition_geometry(profile, route_id) if profile is not None else {
            "status": "unresolved", "reasons": ["缺少 ProductionRoute3DProfile"],
        }
        return {
            "status": "ready" if not blockers else "not_ready",
            "artifact_type": ARTIFACT_TYPE,
            "algorithm": {
                "algorithm_id": ARTIFACT_TYPE, "validator_version": VALIDATOR_VERSION,
            },
            "route": {
                "route_id": route_id,
                "status": (route or {}).get("status"),
                "vertex_count": len((route or {}).get("path") or []),
                "distance_m": (route or {}).get("distance_m"),
                # The full record travels with the readiness entry because the fingerprint of
                # one evaluation binds the *path* fingerprint of the operational route.
                "record": deepcopy(route),
            },
            "route_resolution": {
                "source": selection["source"],
                "candidate_id": selection["candidate_id"],
            },
            "profile": {
                "profile_id": (profile or {}).get("profile_id"),
                "status": (profile or {}).get("status"),
                "current_applicability": profile_applicability,
                "profile_version": (profile or {}).get("profile_version"),
                "read_only": True,
                "never_rewritten_by_this_validator": True,
            },
            "cruise_validation": {
                "validation_id": (cruise or {}).get("validation_id"),
                "status": (cruise or {}).get("status"),
                "current_applicability": (cruise or {}).get("current_applicability"),
                "candidate_id": ((cruise or {}).get("candidate") or {}).get("candidate_id"),
                "candidate_fingerprint": ((cruise or {}).get("candidate") or {}).get(
                    "candidate_fingerprint"
                ),
                "path_fingerprint": ((cruise or {}).get("fingerprints") or {}).get(
                    "path_fingerprint"
                ),
                "validation_fingerprint": ((cruise or {}).get("fingerprints") or {}).get(
                    "validation_fingerprint"
                ),
                "read_only": True,
                "never_rewritten_by_this_validator": True,
            },
            "adoption": {
                "adoption_id": (adoption or {}).get("adoption_id"),
                "status": (adoption or {}).get("status"),
                "current_applicability": (adoption or {}).get("current_applicability"),
            },
            "horizontal_crs": horizontal_crs,
            "horizontal_crs_source": crs_source,
            "horizontal_crs_sources": list(CRS_SOURCES),
            "source_chain": source_status,
            "transition_geometry": {
                "status": geometry.get("status"),
                "reasons": geometry.get("reasons") or [],
                "route_length_m": geometry.get("route_length_m"),
                "cruise_altitude_m": geometry.get("cruise_altitude_m"),
                "phases": deepcopy(geometry.get("phases") or {}),
            },
            "clearance_usage": {
                "terrain_clearance_m": GEOMETRY_INTERSECTION_THRESHOLD_M,
                "building_horizontal_clearance_m": GEOMETRY_INTERSECTION_THRESHOLD_M,
                "building_vertical_clearance_m": GEOMETRY_INTERSECTION_THRESHOLD_M,
                "curve_chord_error_m": 0.0,
                "semantics": CLEARANCE_SEMANTICS,
                "new_default_clearance_introduced": False,
            },
            "blockers": blockers,
            "semantics": deepcopy(READINESS_SEMANTICS),
            "boundaries": deepcopy(BOUNDARIES),
            "explicit_evaluation_required": True,
            "automatic_evaluation": False,
            "produces_safe_or_unsafe_verdict": False,
        }

    # ------------------------------------------------------------------ evaluation

    def evaluate(self, payload=None, *, evidence_adapter=None, save=True):
        payload = payload if isinstance(payload, dict) else {}
        state = self.ensure_state()
        readiness = self.readiness_snapshot(payload)
        route_id = readiness["route"]["route_id"]
        profile = self._current_profile(route_id)[0]
        if readiness["status"] != "ready":
            record = self._not_ready_record(readiness, profile)
            return self._store(record, save=save)

        geometry = transition_geometry(profile, route_id)
        if geometry["status"] != "resolved":
            record = self._not_ready_record({
                **readiness,
                "blockers": list(readiness["blockers"]) + [
                    _block("transition_geometry_unresolved", reason)
                    for reason in geometry.get("reasons") or []
                ],
            }, profile, status="unresolved")
            return self._store(record, save=save)

        adapter = evidence_adapter or self.evidence_adapter
        if adapter is None:
            record = self._not_ready_record({
                **readiness,
                "blockers": [_block(
                    "real_source_evidence_adapter_unavailable",
                    "未配置 production 真实源证据适配器；本服务绝不使用合成环境",
                )],
            }, profile)
            return self._store(record, save=save)

        route = readiness["route"]["record"]
        path = self._route_path(route_id)
        # Keep the projection CRS explicit and identical to the one the evidence adapter uses.
        if hasattr(adapter, "horizontal_crs"):
            adapter.horizontal_crs = readiness["horizontal_crs"]
        metric_path = self._metric_path(
            path, readiness["horizontal_crs"], payload=payload, adapter=adapter,
        )
        if metric_path is None:
            record = self._not_ready_record({
                **readiness,
                "blockers": [_block(
                    "metric_projection_not_ready",
                    f"运行航路 path 无法投影到显式 metric CRS {readiness['horizontal_crs']}",
                )],
            }, profile)
            return self._store(record, save=save)

        cruise_validation = self._current_cruise_validation(route_id)
        # The profile's ``s`` is a route distance along the operational polyline; the metric
        # projection of the *same* polyline carries its own geometric length.  For a correct
        # local metric CRS the two agree to within the projection/geodesic difference, so the
        # window is transferred by the exact ratio of the two measured polyline lengths rather
        # than assumed identical.  This moves *stations*, never the polyline: the truncated
        # geometry is still the original vertex sequence.
        metric_length_m = _polyline_length_m(metric_path)
        route_length_m = float(geometry["route_length_m"])
        if metric_length_m <= 0 or route_length_m <= 0:
            record = self._not_ready_record({
                **readiness,
                "blockers": [*readiness["blockers"], _block(
                    "metric_projection_degenerate",
                    "运行航路在 horizontal_crs 下的米制长度为零：无法截取 transition 区间",
                )],
            }, profile)
            return self._store(record, save=save)
        metric_per_route = metric_length_m / route_length_m

        components = self._fingerprint_components(
            route_id, route, profile, cruise_validation, geometry,
            readiness["horizontal_crs"], readiness["source_chain"],
        )
        fingerprint = transition_fingerprint(components)

        phases = []
        phase_records = {}
        adapter_failures = []
        for phase_id in TRANSITION_PHASE_IDS:
            phase_geometry = geometry["phases"][phase_id]
            if float(phase_geometry["length_m"]) <= 0:
                # A zero-length transition is legitimate **only** when it carries no altitude
                # change at all (``z0 == H`` with ``s_join == 0``, or ``z1 == H`` with
                # ``s_leave == L``): there is no horizontal geometry to validate and no vertical
                # step to represent.  ``transition_geometry`` already rejects the cases where a
                # non-cruise altitude would need a zero-horizontal vertical jump.
                phase_records[phase_id] = {
                    "metric_length_m": 0.0,
                    "truncation": {
                        "truncation_semantics": TRUNCATION_SEMANTICS,
                        "straight_start_to_end_chord_used": False,
                    },
                }
                phases.append(_degenerate_phase(phase_id, phase_geometry, geometry))
                continue
            metric_range = {
                "start_distance_along_route_m": (
                    phase_geometry["start_distance_along_route_m"] * metric_per_route
                ),
                "end_distance_along_route_m": (
                    phase_geometry["end_distance_along_route_m"] * metric_per_route
                ),
            }
            truncated, truncation = truncate_waypoints(
                metric_path,
                metric_range["start_distance_along_route_m"],
                metric_range["end_distance_along_route_m"],
                tolerance_m=max(1e-6, 1e-6 * metric_length_m),
            )
            if len(truncated) < 2:
                adapter_failures.append(_block(
                    "transition_phase_geometry_too_short",
                    f"{phase_id}：截取后的 polyline 顶点不足（需要 >= 2）",
                ))
                continue
            try:
                metric_route, metric_route_record, metric_meta = metric_route_from_phase(
                    truncated, z_start_m=phase_geometry["z_start_m"],
                    z_end_m=phase_geometry["z_end_m"],
                    waypoints=(profile or {}).get("waypoints") or [],
                    route_length_m=geometry["route_length_m"],
                    vertex_distances_along_route_m=truncation.get(
                        "vertex_distances_along_route_m"
                    ),
                    distance_scale=metric_per_route,
                )
            except (TypeError, ValueError, RuntimeError) as exc:
                adapter_failures.append(_block(
                    "transition_phase_geometry_unresolved", f"{phase_id}：{exc}",
                ))
                continue
            try:
                evidence = adapter(
                    phase=deepcopy(phase_geometry), phase_id=phase_id,
                    route_id=route_id, profile=deepcopy(profile),
                    metric_line=deepcopy(truncated), metric_route=metric_route_record,
                    horizontal_crs=readiness["horizontal_crs"], payload=deepcopy(payload),
                )
            except (TypeError, ValueError, RuntimeError) as exc:
                adapter_failures.append(_block(
                    "real_source_evidence_unavailable", f"{phase_id}：{exc}",
                ))
                continue
            evidence = evidence if isinstance(evidence, dict) else {}
            source_type = str(evidence.get("source_type") or "")
            if source_type != "configured_real_sources":
                adapter_failures.append(_block(
                    "configured_real_sources_required",
                    f"{phase_id}：只接受 verified configured real FABDEM/buildings source chain",
                ))
                continue
            phase = evaluate_phase(
                phase_id, metric_route=metric_route,
                terrain_evidence=evidence.get("terrain") or {},
                building_evidence=evidence.get("buildings") or {},
                to_geographic=evidence.get("to_geographic"),
            )
            phase["range"] = {
                "start_distance_along_route_m": phase_geometry["start_distance_along_route_m"],
                "end_distance_along_route_m": phase_geometry["end_distance_along_route_m"],
                "length_m": phase_geometry["length_m"],
                "range_source": phase_geometry["range_source"],
                "truncation": truncation,
            }
            phase["geometry"] = {
                **metric_meta,
                "z_start_m": phase_geometry["z_start_m"],
                "z_end_m": phase_geometry["z_end_m"],
                "cruise_altitude_m": geometry["cruise_altitude_m"],
                "route_length_m": geometry["route_length_m"],
                "truncated_polyline_used": True,
                "straight_start_to_end_chord_used": False,
            }
            phase["evidence"] = {
                **(phase.get("evidence") or {}),
                "adapter_id": evidence.get("adapter_id"),
                "source_type": source_type,
                "metric_crs": evidence.get("metric_crs") or readiness["horizontal_crs"],
            }
            phases.append(phase)
            phase_records[phase_id] = {
                "metric_length_m": metric_meta["metric_length_m"],
                "truncation": truncation,
            }

        if adapter_failures:
            record = self._not_ready_record({
                **readiness,
                "blockers": list(readiness["blockers"]) + adapter_failures,
            }, profile)
            return self._store(record, save=save)

        record = assemble_record(
            route_id=route_id,
            profile_id=(profile or {}).get("profile_id"),
            phase_results=phases,
            geometry=geometry,
            components=components,
            fingerprint=fingerprint,
            source_audits=deepcopy(readiness["source_chain"].get("audits") or {}),
            horizontal_crs=readiness["horizontal_crs"],
            source_type="configured_real_sources",
            provenance=self._provenance(
                route_id, profile, cruise_validation, readiness, geometry, phase_records,
            ),
            profile_applicability="current",
        )
        record["validator_versions"] = deepcopy(_VALIDATOR_VERSIONS)
        return self._store(record, save=save)

    # ------------------------------------------------------------------ invalidation

    def stale_for_reason(self, reason="vertical_transition_input_changed"):
        """Stale only the stored Vertical Transition Validation records.

        Strictly downstream: a profile / route / adoption / cruise-validation / source change
        makes a stored transition validation stale.  It **never** marks a Theta* candidate, a
        ``RouteRiskProfile``, a ``LayeredRouteValidation``, a ``Route3DProfile`` or an adoption
        stale — the transition validation is the end of that chain.
        """

        state = self.ensure_state()
        collection = normalize_vertical_transition_validation_collection(
            state.get("vertical_transition_validations")
        )
        changed = []
        for item in collection["items"]:
            if item.get("status") == "stale":
                continue
            item["status"] = "stale"
            item["current_applicability"] = "stale"
            item["stale_reason"] = str(reason)
            item["full_3d_geometry_validated"] = False
            changed.append(str(item.get("validation_id")))
        if changed:
            collection["status"] = "stale"
            collection["active_validation_id"] = None
            state["vertical_transition_validations"] = collection
            state.setdefault("result_statuses", {})[
                "vertical_transition_validation"
            ] = "stale"
        return {"stale_validation_ids": changed}

    def stale_for_transition_change(self, reason="vertical_transition_validation_changed"):
        """Propagate an explicit transition evaluate to its declared downstream consumers.

        The transition verdict feeds Route Safety Evidence V2 (geometry domain) and the
        report; nothing else.  Never upstream.
        """

        return {
            "reason": str(reason),
            "downstream": list(DOWNSTREAM_RESULTS),
        }

    # ------------------------------------------------------------------ internals

    def _route_records(self):
        return {
            str(item.get("route_id")): item
            for item in self.session.state.get("operational_routes") or []
            if isinstance(item, dict)
        }

    def _resolve_route(self, payload):
        """Resolve the target route without inventing one.

        ``payload.route_id`` wins.  Otherwise the route of the current cruise validation (and
        its candidate) is used, which is what the "完整 3D 航迹" panel already shows.
        """

        wanted = str(_scalar(payload.get("route_id")) or "").strip()
        routes = self._route_records()
        if wanted:
            return {
                "route_id": wanted, "route": routes.get(wanted), "source": "payload.route_id",
                "candidate_id": None,
                "blockers": [] if wanted in routes else [],
            }
        route_ids = list(routes)
        if len(route_ids) == 1:
            route_id = route_ids[0]
            return {
                "route_id": route_id, "route": routes.get(route_id),
                "source": "sole_operational_route", "candidate_id": None, "blockers": [],
            }
        validation = self._latest_cruise_validation()
        route_id = str(((validation or {}).get("candidate") or {}).get("route_id") or "")
        if route_id:
            return {
                "route_id": route_id, "route": routes.get(route_id),
                "source": "current_cruise_validation_candidate",
                "candidate_id": ((validation or {}).get("candidate") or {}).get("candidate_id"),
                "blockers": [],
            }
        return {
            "route_id": "", "route": None, "source": "unresolved", "candidate_id": None,
            "blockers": [_block(
                "route_selection_ambiguous",
                "无法唯一确定目标航路：请显式提供 route_id（当前既没有唯一运行航路，"
                "也没有 current LayeredRouteValidation）",
            )],
        }

    def _validation_items(self):
        """The production cruise validations, with recomputed applicability when possible."""

        service = self.validations
        if service is not None and hasattr(service, "result_snapshot"):
            try:
                items = list((service.result_snapshot() or {}).get("items") or [])
            except (TypeError, ValueError, RuntimeError):  # pragma: no cover - defensive
                items = []
            if items:
                return items
        return list(normalize_layered_route_validation_collection(
            self.session.state.get("layered_route_validations")
        )["items"])

    def _latest_cruise_validation(self):
        items = self._validation_items()
        return items[-1] if items else None

    def _current_cruise_validation(self, route_id):
        """Resolve the current cruise ``LayeredRouteValidation`` of one route.

        ``current_applicability == 'current'`` is required.  Both the **projected** value and
        the **stored** value are honoured: the projection additionally depends on the layered
        candidate container being present in this session, so a fully recorded
        ``validated_candidate`` + ``current`` record whose candidate is not in this session's
        state is still a real recorded current validation.  The transition validation never
        re-derives the cruise verdict itself; it only binds the fingerprint of what it reads.
        """

        if not route_id:
            return None
        projection = {
            str(item.get("validation_id") or ""): item
            for item in self._validation_items()
        }
        stored = {
            str(item.get("validation_id") or ""): item
            for item in normalize_layered_route_validation_collection(
                self.session.state.get("layered_route_validations")
            )["items"]
        }
        identifiers = list(dict.fromkeys([*projection, *stored]))
        for identifier in reversed(identifiers):
            projected = projection.get(identifier) or {}
            recorded = stored.get(identifier) or projected
            candidate = recorded.get("candidate") or projected.get("candidate") or {}
            if str(candidate.get("route_id") or "") != str(route_id):
                continue
            if str(recorded.get("status") or projected.get("status") or "") != "validated_candidate":
                continue
            applicabilities = {
                str(projected.get("current_applicability") or ""),
                str(recorded.get("current_applicability") or ""),
            }
            if "current" in applicabilities:
                return {**recorded, "current_applicability": "current"}
        return None

    def _current_adoption(self, route_id):
        if not route_id:
            return None
        collection = self.session.state.get("layered_operational_adoptions") or {}
        for item in collection.get("items") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("route_id") or "") != str(route_id):
                continue
            if item.get("status") != "published":
                continue
            if str(item.get("current_applicability") or "") != "current":
                continue
            return item
        return None

    def _profile_items(self):
        """The *projected* profiles (``current_applicability`` recomputed), when available."""

        service = self.profiles
        if service is not None and hasattr(service, "result_snapshot"):
            try:
                return list((service.result_snapshot() or {}).get("items") or [])
            except (TypeError, ValueError, RuntimeError):  # pragma: no cover - defensive
                return None
        return None

    def _current_profile(self, route_id):
        """Return ``(profile, current_applicability)`` for one route.

        Never repairs a stored profile: the *projection* of the profile service is used so a
        stored-but-not-current profile is reported as such instead of being read as current.
        """

        if not route_id:
            return None, "not_ready"
        items = self._profile_items()
        if items is None:
            from ..application.route_3d_profile_service import current_applicability
            from ..domain.route_3d_profile import normalize_route_3d_profiles

            stored = normalize_route_3d_profiles(
                (self.session.state.get("spatial_3d") or {}).get("route_3d_profiles")
            )
            profile = next((
                item for item in stored.values() if str(item.get("route_id")) == str(route_id)
            ), None)
            if profile is None:
                return None, "not_ready"
            applicability, reason = current_applicability(self.session.state, profile)
            if applicability == "stale":
                profile = {**deepcopy(profile), "status": "stale", "stale_reason": reason}
            return profile, applicability
        profile = next((
            item for item in items if str(item.get("route_id") or "") == str(route_id)
        ), None)
        if profile is None:
            return None, "not_ready"
        return profile, str(profile.get("current_applicability") or "not_ready")

    def _route_path(self, route_id):
        route = self._route_records().get(str(route_id)) or {}
        return [
            [float(point[0]), float(point[1])]
            for point in route.get("path") or []
            if isinstance(point, (list, tuple)) and len(point) >= 2
        ]

    def _metric_path(self, path, horizontal_crs, *, payload, adapter):
        """Project the *original* polyline to the explicit metric CRS.

        The preferred path is the adapter's own declared transform (it is the same object the
        evidence raster/building queries use, so the geometry and the evidence share one
        frame).  Without one, an explicit ``to_metric`` callable in the payload is accepted.
        """

        if len(path) < 2:
            return None
        converter = getattr(adapter, "to_metric", None)
        if not callable(converter):
            converter = payload.get("to_metric")
        if not callable(converter):
            return None
        result = []
        for point in path:
            converted = converter([float(point[0]), float(point[1])])
            if converted is None:
                return None
            result.append([float(converted[0]), float(converted[1])])
        return result

    def _resolve_horizontal_crs(self, payload):
        value = str(_scalar(payload.get("horizontal_crs")) or "").strip()
        if value:
            return value, "payload.horizontal_crs"
        policy = self.session.state.get("v3_fine_refinement_policy") or {}
        value = str(policy.get("horizontal_crs") or "").strip()
        if value:
            return value, "v3_fine_refinement_policy.horizontal_crs"
        return None, None

    def _source_readiness(self):
        audits = ((self.session.state.get("source_audits") or {}).get("items") or {})
        blockers = []
        selected = {}
        for role in ("terrain_dtm", "buildings"):
            item = audits.get(role) or {}
            selected[role] = _audit_fingerprint_view(item)
            if item.get("status") != "verified":
                blockers.append(_block(
                    f"{role}_source_not_verified",
                    f"{role} source audit 必须为 verified（当前 {item.get('status') or 'missing'}）",
                ))
        return {
            "status": "ready" if not blockers else "not_ready",
            "source_type": "configured_real_sources",
            "audits": selected,
            "blockers": blockers,
        }

    def _fingerprint_components(self, route_id, route, profile, cruise_validation, geometry,
                                horizontal_crs, source_readiness):
        """The declared dependency set of one transition validation.

        Binds the Route3DProfile fingerprint, the operational route/adoption, the existing
        cruise validation fingerprint, the terrain/building source audits, the explicit metric
        CRS and the validator version.  Any change to those makes a stored record stale.
        """

        adoption = self._current_adoption(route_id) or {}
        corridor = deepcopy((profile or {}).get("join_leave") or {})
        return {
            "validator_version": VALIDATOR_VERSION,
            "route_id": str(route_id),
            "route_3d_profile_fingerprint": ((profile or {}).get("fingerprints") or {}).get(
                "profile_fingerprint"
            ),
            "route_3d_profile_id": (profile or {}).get("profile_id"),
            "route_3d_profile_applicability": str(
                (profile or {}).get("current_applicability") or "not_ready"
            ),
            "route_3d_profile_geometry": {
                "route_length_m": (profile or {}).get("route_length_m"),
                "cruise_altitude_m": (profile or {}).get("cruise_altitude_m"),
                "join_distance_along_route_m": corridor.get("join_distance_along_route_m"),
                "leave_distance_along_route_m": corridor.get("leave_distance_along_route_m"),
                "terminal_altitude_m": deepcopy((profile or {}).get("terminal_altitude_m") or {}),
                "waypoints": deepcopy((profile or {}).get("waypoints") or []),
            },
            "operational_route": {
                "route_id": str(route_id),
                "status": (route or {}).get("status"),
                "distance_m": (route or {}).get("distance_m"),
                "vertex_count": len((route or {}).get("path") or []),
                "path_fingerprint": stable_fingerprint(
                    (route or {}).get("path") or [], prefix="routepath-",
                ),
            },
            "operational_adoption": {
                "adoption_id": adoption.get("adoption_id"),
                "status": adoption.get("status"),
                "current_applicability": adoption.get("current_applicability"),
                "validation_fingerprint": adoption.get("validation_fingerprint"),
                "candidate_fingerprint": adoption.get("candidate_fingerprint"),
                "projection_fingerprint": adoption.get("projection_fingerprint"),
            },
            "cruise_validation_fingerprint": (
                ((cruise_validation or {}).get("fingerprints") or {}).get(
                    "validation_fingerprint"
                )
            ),
            "cruise_validation_id": (cruise_validation or {}).get("validation_id"),
            "cruise_candidate_fingerprint": (
                ((cruise_validation or {}).get("candidate") or {}).get("candidate_fingerprint")
            ),
            "cruise_path_fingerprint": (
                ((cruise_validation or {}).get("fingerprints") or {}).get("path_fingerprint")
            ),
            "source_audits": deepcopy((source_readiness or {}).get("audits") or {}),
            "metric_crs": horizontal_crs,
            "transition_geometry": {
                "route_length_m": (geometry or {}).get("route_length_m"),
                "cruise_altitude_m": (geometry or {}).get("cruise_altitude_m"),
                "phases": deepcopy((geometry or {}).get("phases") or {}),
            },
            "validator_versions": deepcopy(_VALIDATOR_VERSIONS),
            "clearance_usage": {
                "terrain_clearance_m": GEOMETRY_INTERSECTION_THRESHOLD_M,
                "building_horizontal_clearance_m": GEOMETRY_INTERSECTION_THRESHOLD_M,
                "building_vertical_clearance_m": GEOMETRY_INTERSECTION_THRESHOLD_M,
                "curve_chord_error_m": 0.0,
                "semantics": CLEARANCE_SEMANTICS,
            },
        }

    def _provenance(self, route_id, profile, cruise_validation, readiness, geometry,
                    phase_records):
        return {
            "derivation": "source_native_truncated_original_polyline_transition_geometry",
            "validator_version": VALIDATOR_VERSION,
            "reads": {
                "operational_route": route_id,
                "route_3d_profile": (profile or {}).get("profile_id"),
                "route_3d_profile_fingerprint": (
                    ((profile or {}).get("fingerprints") or {}).get("profile_fingerprint")
                ),
                "cruise_validation": (cruise_validation or {}).get("validation_id"),
                "layered_operational_adoption": readiness.get("adoption", {}).get("adoption_id"),
            },
            "geometry": {
                "range_source": "route_3d_profile_join_leave_distance_along_route",
                "truncated_polyline_used": True,
                "straight_start_to_end_chord_used": False,
                "curve_chord_error_m": 0.0,
                "horizontal_crs": readiness.get("horizontal_crs"),
                "phases": deepcopy(phase_records),
            },
            "clearance_usage": {
                "terrain_clearance_m": GEOMETRY_INTERSECTION_THRESHOLD_M,
                "building_horizontal_clearance_m": GEOMETRY_INTERSECTION_THRESHOLD_M,
                "building_vertical_clearance_m": GEOMETRY_INTERSECTION_THRESHOLD_M,
                "semantics": CLEARANCE_SEMANTICS,
                "note": (
                    "建筑的 horizontal clearance 只用 0 做**几何相交**判定，"
                    "不是工程净空参数；本验证不引入任何新的默认净空值。"
                ),
            },
            "shared_validators": [
                "route_planner_v3.continuous_validators.MetricRoute",
                "route_planner_v3.continuous_validators.validate_terrain",
                "route_planner_v3.continuous_validators.validate_buildings",
                "domain.building_clearance.building_roof_elevation",
                "route_planner_v3.continuous_raster_window.resolve_native_pixel_intervals",
            ],
            "upstream_modified": False,
            "recomputed_algorithms": [],
            "rewrites_route_3d_profile": False,
            "rewrites_layered_route_validation": False,
            "display_only_airspace_used": False,
        }

    def _not_ready_record(self, readiness, profile, *, status="not_ready"):
        record = empty_vertical_transition_validation(status)
        blockers = deepcopy(readiness.get("blockers") or [])
        identity = stable_fingerprint({
            "route_id": readiness.get("route", {}).get("route_id"),
            "blockers": blockers,
            "validator_version": VALIDATOR_VERSION,
        }, prefix="verticaltransitionattemptv1-")
        record.update({
            "validation_id": "VTV-" + identity[-12:].upper(),
            "route_id": readiness.get("route", {}).get("route_id"),
            "profile_id": (profile or {}).get("profile_id"),
            "status": status,
            "status_reason": (
                "validation_prerequisites_not_ready" if status == "not_ready"
                else "transition_geometry_unresolved"
            ),
            "blocking_reasons": blockers,
            "current_applicability": "not_ready" if status == "not_ready" else "unresolved",
            "created_at": utc_now(),
            "route_length_m": (profile or {}).get("route_length_m"),
            "source_type": (readiness.get("source_chain") or {}).get("source_type"),
            "source_audits": deepcopy(
                (readiness.get("source_chain") or {}).get("audits") or {}
            ),
            "horizontal_crs": readiness.get("horizontal_crs"),
            "fingerprints": {
                "transition_fingerprint": identity,
                "profile_fingerprint": (
                    ((profile or {}).get("fingerprints") or {}).get("profile_fingerprint")
                ),
                "validation_fingerprint": (
                    (readiness.get("cruise_validation") or {}).get("validation_fingerprint")
                ),
                "components": {"not_ready": identity},
            },
            "provenance": {
                "derivation": "readiness_blocked_no_source_native_evidence_was_evaluated",
                "validator_version": VALIDATOR_VERSION,
                "upstream_modified": False,
            },
        })
        record["full_3d_geometry_validated"] = False
        from ..domain.vertical_transition_validation import transition_limitations

        record["limitations"] = transition_limitations(record)
        return record

    def _store(self, record, *, save):
        state = self.ensure_state()
        collection = normalize_vertical_transition_validation_collection(
            state.get("vertical_transition_validations")
        )
        stale_ids = []
        items = []
        new_id = record.get("validation_id")
        new_fingerprint = (record.get("fingerprints") or {}).get("transition_fingerprint")
        new_route = str(record.get("route_id") or "")
        items.append(record)
        for existing in collection["items"]:
            if str(existing.get("validation_id")) == str(new_id):
                continue
            same_route = new_route and str(existing.get("route_id") or "") == new_route
            if same_route and existing.get("status") != "stale":
                old_fingerprint = (existing.get("fingerprints") or {}).get(
                    "transition_fingerprint"
                )
                if old_fingerprint != new_fingerprint:
                    existing["status"] = "stale"
                    existing["current_applicability"] = "stale"
                    existing["stale_reason"] = "transition_validation_dependencies_changed"
                    existing["full_3d_geometry_validated"] = False
                    stale_ids.append(str(existing.get("validation_id")))
            items.append(existing)
        collection["items"] = items
        collection["count"] = len(items)
        collection["status"] = record["status"]
        collection["active_validation_id"] = (
            record.get("validation_id")
            if record.get("current_applicability") == "current" else None
        )
        state["vertical_transition_validations"] = collection
        state.setdefault("result_statuses", {})[
            "vertical_transition_validation"
        ] = record["status"]
        if save:
            self.session.save()
        return self.result_snapshot()

    def _project_current(self, item):
        """Recompute ``current_applicability`` of one stored record against live facts.

        A record that never produced any evidence (``not_ready``) is reported verbatim: there is
        nothing to re-validate, and a blocked attempt is never upgraded to a stale *evidence*
        record.
        """

        projected = deepcopy(item)
        if item.get("status") in ("stale", "not_ready"):
            projected["current_applicability"] = (
                "stale" if item.get("status") == "stale" else "not_ready"
            )
            projected["full_3d_geometry_validated"] = False
            return projected
        route_id = str(item.get("route_id") or "")
        if not route_id:
            projected["current_applicability"] = "stale_inputs_changed"
            projected["full_3d_geometry_validated"] = False
            return projected
        profile, applicability = self._current_profile(route_id)
        if profile is None or profile.get("status") != "passed" or applicability != "current":
            projected["current_applicability"] = "stale_profile_not_current"
            projected["full_3d_geometry_validated"] = False
            return projected
        geometry = transition_geometry(profile, route_id)
        if geometry.get("status") != "resolved":
            projected["current_applicability"] = "stale_inputs_changed"
            projected["full_3d_geometry_validated"] = False
            return projected
        cruise = self._current_cruise_validation(route_id)
        stored = (item.get("fingerprints") or {}).get("components") or {}
        source_status = self._source_readiness()
        expected = self._fingerprint_components(
            route_id, self._route_records().get(route_id), profile, cruise, geometry,
            stored.get("metric_crs") or item.get("horizontal_crs"), source_status,
        )
        current = transition_fingerprint(expected)
        projected["current_applicability"] = (
            "current"
            if current == (item.get("fingerprints") or {}).get("transition_fingerprint")
            else "stale_inputs_changed"
        )
        projected["full_3d_geometry_validated"] = bool(
            projected["current_applicability"] == "current"
            and item.get("status") == "validated"
        )
        return projected


def _degenerate_phase(phase_id, phase_geometry, geometry):
    """A zero-length transition phase: no horizontal geometry, no vertical step."""

    from ..domain.vertical_transition_validation import (
        CLEARANCE_SEMANTICS, empty_phase_result,
    )

    phase = empty_phase_result(phase_id)
    phase["status"] = "validated"
    phase["reason"] = "zero_length_transition_without_altitude_change"
    phase["domains"] = {
        name: {
            "domain": name, "status": "passed", "reason": None, "evaluated": False,
            "violations": [], "unresolved": [], "minimum_margin": None,
            "item_count": 0, "failed_interval_count": 0, "unresolved_interval_count": 0,
            "evidence": {"not_applicable": "zero_length_transition_phase"},
        }
        for name in TRANSITION_DOMAIN_IDS
    }
    phase["domain_statuses"] = {name: "passed" for name in TRANSITION_DOMAIN_IDS}
    phase["minimum_margins"] = {"terrain_vertical_m": None, "building_vertical_m": None}
    phase["failed_intervals"] = []
    phase["unresolved_evidence"] = []
    phase["penetration"] = False
    phase["range"] = {
        "start_distance_along_route_m": phase_geometry["start_distance_along_route_m"],
        "end_distance_along_route_m": phase_geometry["end_distance_along_route_m"],
        "length_m": 0.0,
        "range_source": phase_geometry["range_source"],
        "truncation": {
            "input_vertex_count": 0, "kept_interior_vertex_count": 0,
            "boundary_anchors_inserted": 0, "vertex_distances_along_route_m": [],
            "truncation_semantics": TRUNCATION_SEMANTICS,
            "straight_start_to_end_chord_used": False,
        },
    }
    phase["geometry"] = {
        "metric_length_m": 0.0, "metric_vertex_count": 0, "primitive_count": 0,
        "z_start_m": phase_geometry["z_start_m"], "z_end_m": phase_geometry["z_end_m"],
        "cruise_altitude_m": geometry["cruise_altitude_m"],
        "route_length_m": geometry["route_length_m"],
        "truncated_polyline_used": False,
        "straight_start_to_end_chord_used": False,
        "no_horizontal_extent": True,
    }
    phase["evidence"] = {
        "clearance_semantics": CLEARANCE_SEMANTICS,
        "curve_chord_error_m": 0.0,
        "not_applicable": "zero_length_transition_phase",
        "z_start_m": phase_geometry["z_start_m"],
        "z_end_m": phase_geometry["z_end_m"],
    }
    return phase


def _polyline_length_m(points):
    total = 0.0
    for left, right in zip(points or [], (points or [])[1:]):
        total += hypot(float(right[0]) - float(left[0]), float(right[1]) - float(left[1]))
    return total


def _audit_fingerprint_view(item):
    value = item if isinstance(item, dict) else {}
    verification = value.get("verification") or {}
    return {
        "role": value.get("role"), "status": value.get("status"),
        "file_name": value.get("file_name"), "size_bytes": value.get("size_bytes"),
        "mtime_ns": value.get("mtime_ns"), "sha256": verification.get("sha256"),
    }


def _scalar(value):
    """Accept the ``?key=value`` query shape the API router produces (values are lists)."""

    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    return value


def _block(code, reason):
    return {"reason_code": str(code), "reason": str(reason)}


__all__ = [
    "CRS_SOURCES", "DOWNSTREAM_RESULTS", "READINESS_SEMANTICS",
    "VerticalTransitionValidationService",
]
