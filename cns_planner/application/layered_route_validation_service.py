"""Production continuous validation for the current LayeredRouteCandidate.

The service is an orchestration boundary.  It builds a straight-segment, constant-altitude
metric representation of the candidate's *unchanged* two-dimensional path and delegates the
actual terrain/building verdicts to the production-neutral pure validators shared with V3-C.
"""

from __future__ import annotations

from copy import deepcopy
from math import hypot

from ..domain.layered_route import resolve_cruise_altitude
from ..domain.layered_route_validation import (
    ALGORITHM_ID, ALGORITHM_VERSION, ROUTE_SEMANTICS, VALIDATOR_VERSIONS,
    empty_layered_route_validation, normalize_layered_route_validation_collection,
    path_fingerprint, stable_fingerprint, utc_now, validation_fingerprint,
)
from ..route_planner_v3.continuous_validators import (
    MetricRoute, validate_buildings, validate_terrain,
)


class LayeredRouteValidationService:
    """Validate current production candidates without planning or operational publication."""

    def __init__(self, session, invalidation, snapshot, layered_service, risk_profile_service,
                 *, evidence_adapter=None):
        self.session = session
        self.invalidation = invalidation
        self.snapshot = snapshot
        self.layered = layered_service
        self.risk_profiles = risk_profile_service
        self.evidence_adapter = evidence_adapter
        self.adoption_invalidator = None
        self.ensure_state()

    # ------------------------------------------------------------------ snapshots

    def ensure_state(self):
        state = self.session.state
        state["layered_route_validations"] = normalize_layered_route_validation_collection(
            state.get("layered_route_validations")
        )
        state.setdefault("result_statuses", {}).setdefault(
            "layered_route_validation", "not_calculated",
        )
        return state

    def result_snapshot(self):
        state = self.ensure_state()
        collection = normalize_layered_route_validation_collection(
            state.get("layered_route_validations")
        )
        collection["items"] = [self._project_current(item) for item in collection["items"]]
        collection["count"] = len(collection["items"])
        return collection

    def readiness_snapshot(self, payload=None):
        payload = payload if isinstance(payload, dict) else {}
        state = self.ensure_state()
        candidate = self._select_current_candidate(payload)
        layer = self._layer_for(candidate)
        cruise = resolve_cruise_altitude(layer)
        feasibility = state.get("layered_route_feasibility_policy") or {}
        buildings = state.get("building_clearance_policy") or {}
        profile = self._current_risk_profile(candidate)
        blockers = []
        if candidate is None:
            blockers.append(_block("current_candidate_missing", "缺少 current LayeredRouteCandidate"))
        elif candidate.get("status") != "candidate" or candidate.get("current_applicability") != "current":
            blockers.append(_block("candidate_not_current", "LayeredRouteCandidate 不是 current candidate"))
        if cruise.get("status") != "confirmed":
            blockers.append(_block(
                "altitude_layer_not_direct_egm2008",
                "AltitudeLayer 必须已确认且 nominal_altitude_m 可直接解析为 EGM2008 orthometric",
            ))
        if feasibility.get("status") != "confirmed" or feasibility.get(
            "terrain_vertical_clearance_m"
        ) is None:
            blockers.append(_block(
                "terrain_clearance_not_confirmed",
                "layered_route_feasibility_policy.terrain_vertical_clearance_m 未确认",
            ))
        if buildings.get("status") != "confirmed" or any(
            buildings.get(name) is None
            for name in ("horizontal_clearance_m", "vertical_clearance_m")
        ):
            blockers.append(_block(
                "building_clearance_not_confirmed",
                "building_clearance_policy 的 horizontal/vertical clearance 未确认",
            ))
        if profile is None:
            blockers.append(_block(
                "current_route_risk_profile_missing",
                "必须先存在该 current candidate 的 current RouteRiskProfile；classification 不作为 hard gate",
            ))
        source_status = self._source_readiness()
        blockers.extend(source_status["blockers"])
        path = (candidate or {}).get("path") or []
        if len(path) < 2:
            blockers.append(_block("candidate_path_unavailable", "candidate 二维 path 至少需要两个点"))
        return {
            "status": "ready" if not blockers else "not_ready",
            "algorithm": {"algorithm_id": ALGORITHM_ID, "algorithm_version": ALGORITHM_VERSION},
            "candidate": _candidate_ref(candidate),
            "altitude_layer": deepcopy(layer),
            "cruise_altitude": cruise,
            "route_risk_profile": _profile_ref(profile),
            "source_chain": source_status,
            "blockers": blockers,
            "airspace": {
                "status": "not_applicable", "applicability": "display_only",
                "used_in_validation": False, "used_in_fingerprint": False,
            },
            "semantics": {
                "route": ROUTE_SEMANTICS,
                "no_replan_refine_rounding_or_cross_layer": True,
                "constant_egm2008_cruise_altitude": True,
                "risk_profile_is_audit_prerequisite_not_a_classification_gate": True,
            },
        }

    # ------------------------------------------------------------------ validation

    def validate(self, payload=None, *, evidence_adapter=None, save=True):
        payload = payload if isinstance(payload, dict) else {}
        state = self.ensure_state()
        readiness = self.readiness_snapshot(payload)
        candidate = self._select_current_candidate(payload)
        if readiness["status"] != "ready":
            record = self._not_ready_record(candidate, readiness)
            return self._store(record, save=save)
        layer = self._layer_for(candidate)
        cruise = resolve_cruise_altitude(layer)
        terrain_clearance = float(
            state["layered_route_feasibility_policy"]["terrain_vertical_clearance_m"]
        )
        building_policy = state["building_clearance_policy"]
        policy = {
            "terrain_clearance_m": terrain_clearance,
            "building_horizontal_clearance_m": float(building_policy["horizontal_clearance_m"]),
            "building_vertical_clearance_m": float(building_policy["vertical_clearance_m"]),
            # The production candidate has no curve realization error: its original polyline
            # is the validation geometry, not an approximation of another curve.
            "use_curve_error_envelope": False,
        }
        adapter = evidence_adapter or self.evidence_adapter
        if adapter is None:
            record = self._not_ready_record(candidate, {
                **readiness,
                "blockers": [_block("evidence_adapter_unavailable", "未配置 production source adapter")],
            })
            return self._store(record, save=save)
        try:
            evidence = adapter(
                candidate=deepcopy(candidate), path=deepcopy(candidate.get("path") or []),
                altitude_layer=deepcopy(layer),
                nominal_altitude_m=float(cruise["altitude_egm2008_m"]),
                policy=deepcopy(policy), payload=deepcopy(payload),
            )
        except (TypeError, ValueError, RuntimeError) as exc:
            record = self._not_ready_record(candidate, {
                **readiness,
                "blockers": [_block("real_source_evidence_unavailable", str(exc))],
            })
            return self._store(record, save=save)
        evidence = evidence if isinstance(evidence, dict) else {}
        source_type = str(evidence.get("source_type") or "")
        if source_type != "configured_real_sources":
            record = self._not_ready_record(candidate, {
                **readiness,
                "blockers": [_block(
                    "configured_real_sources_required",
                    "production validation 只接受 verified configured real FABDEM/buildings source chain",
                )],
            })
            return self._store(record, save=save)
        route = evidence.get("route")
        if not isinstance(route, dict):
            route = _constant_metric_route(
                evidence.get("metric_path") or [], float(cruise["altitude_egm2008_m"]),
            )
        try:
            metric_route = MetricRoute(route)
        except (TypeError, ValueError, RuntimeError) as exc:
            record = self._not_ready_record(candidate, {
                **readiness,
                "blockers": [_block("projection_not_ready", str(exc))],
            })
            return self._store(record, save=save)

        sources = deepcopy(evidence.get("sources") or {})
        metric_crs = evidence.get("metric_crs") or (sources.get("metric_frame") or {}).get(
            "horizontal_crs"
        ) or (sources.get("metric_frame") or {}).get("crs")
        components = self._fingerprint_components(
            candidate, layer, policy, metric_crs, sources,
        )
        fingerprint = validation_fingerprint(components)
        observed = int(evidence.get("sample_count") or (
            len((evidence.get("terrain") or {}).get("pixels") or [])
            + len((evidence.get("buildings") or {}).get("buildings") or [])
        ))
        limit = _resource_limit(payload)
        limit_reached = bool(evidence.get("resource_limit_exceeded")) or (
            limit is not None and observed > limit
        )
        if limit_reached:
            record = self._record_base(
                candidate, layer, route, evidence, policy, components, fingerprint,
                status="validation_incomplete", reason="resource_limit_reached",
            )
            record["resource_limits"] = {
                "max_evidence_items": limit, "observed_evidence_items": observed,
                "limit_reached": True, "safety_parameter": False,
            }
            return self._store(record, save=save)

        terrain = validate_terrain(
            metric_route, evidence=evidence.get("terrain") or {}, policy=policy,
            to_geographic=evidence.get("to_geographic"),
        )
        building = validate_buildings(
            metric_route, evidence=evidence.get("buildings") or {}, policy=policy,
            to_geographic=evidence.get("to_geographic"),
        )
        statuses = {terrain.get("status"), building.get("status")}
        status = (
            "failed" if "failed" in statuses
            else "unresolved" if "unresolved" in statuses
            else "validated_candidate"
        )
        record = self._record_base(
            candidate, layer, route, evidence, policy, components, fingerprint,
            status=status, reason=None if status == "validated_candidate" else f"{status}_domain_evidence",
        )
        record["domains"]["terrain"] = deepcopy(terrain)
        record["domains"]["building"] = deepcopy(building)
        record["minimum_margins"] = {
            "terrain_vertical_m": terrain.get("minimum_margin"),
            "building_vertical_m": building.get("minimum_margin"),
        }
        record["failed_intervals"] = [
            deepcopy(item) for domain in (terrain, building)
            for item in domain.get("violations") or []
        ]
        record["unresolved_intervals"] = [
            deepcopy(item) for domain in (terrain, building)
            for item in domain.get("unresolved") or []
        ]
        record["critical_evidence"] = _critical_evidence(terrain, building)
        record["resource_limits"] = {
            "max_evidence_items": limit, "observed_evidence_items": observed,
            "limit_reached": False, "safety_parameter": False,
        }
        return self._store(record, save=save)

    # ------------------------------------------------------------------ invalidation

    def stale_for_reason(self, reason="layered_validation_input_changed"):
        state = self.ensure_state()
        collection = normalize_layered_route_validation_collection(
            state.get("layered_route_validations")
        )
        changed = []
        for item in collection["items"]:
            if item.get("status") in ("stale",):
                continue
            item["status"] = "stale"
            item["current_applicability"] = "stale"
            item["stale_reason"] = str(reason)
            changed.append(str(item.get("validation_id")))
        if changed:
            collection["status"] = "stale"
            state["layered_route_validations"] = collection
            state.setdefault("result_statuses", {})["layered_route_validation"] = "stale"
            if callable(self.adoption_invalidator):
                self.adoption_invalidator(changed, str(reason))
        return {"stale_validation_ids": changed}

    # ------------------------------------------------------------------ helpers

    def _record_base(self, candidate, layer, route, evidence, policy, components, fingerprint,
                     *, status, reason):
        record = empty_layered_route_validation(status)
        validation_id = "LRV-" + fingerprint[-12:].upper()
        record.update({
            "validation_id": validation_id, "status": status, "status_reason": reason,
            "validated_at": utc_now(), "candidate": _candidate_ref(candidate),
            "route": {
                "path_crs": "OGC:CRS84", "path": deepcopy(candidate.get("path") or []),
                "metric_path": deepcopy(
                    ((route.get("horizontal_geometry") or {}).get("linearized") or {}).get(
                        "linestring_metric"
                    ) or []
                ),
                "metric_crs": components.get("metric_crs"),
                "nominal_altitude_m": float(layer["nominal_altitude_m"]),
                "vertical_reference": "egm2008_orthometric",
                "altitude_model": "constant_cruise_altitude", "semantics": ROUTE_SEMANTICS,
                "two_dimensional_source_path": True,
            },
            "source_type": str(evidence.get("source_type") or ""),
            "source_audits": deepcopy(components.get("source_audits") or {}),
            "policies": {
                "terrain_vertical_clearance_m": policy["terrain_clearance_m"],
                "building_horizontal_clearance_m": policy["building_horizontal_clearance_m"],
                "building_vertical_clearance_m": policy["building_vertical_clearance_m"],
            },
            "fingerprints": {
                "validation_fingerprint": fingerprint,
                "candidate_fingerprint": candidate.get("candidate_fingerprint"),
                "path_fingerprint": path_fingerprint(candidate.get("path") or []),
                "components": deepcopy(components),
            },
            "current_applicability": "current",
            "provenance": {
                "source_adapter": evidence.get("adapter_id"),
                "source_native_terrain": True, "real_building_footprints": True,
                "shared_validators": [
                    "route_planner_v3.continuous_validators.validate_terrain",
                    "route_planner_v3.continuous_validators.validate_buildings",
                    "domain.building_clearance.building_roof_elevation",
                    "domain.building_clearance.evaluate_vertical_clearance",
                ],
                "planning_or_geometry_mutation": False,
            },
        })
        return record

    def _not_ready_record(self, candidate, readiness):
        record = empty_layered_route_validation("not_ready")
        identity = stable_fingerprint({
            "candidate": _candidate_ref(candidate),
            "blockers": readiness.get("blockers") or [],
        }, prefix="layeredvalidationattemptv1-")
        record.update({
            "validation_id": "LRV-" + identity[-12:].upper(),
            "status_reason": "validation_prerequisites_not_ready",
            "blocking_reasons": deepcopy(readiness.get("blockers") or []),
            "validated_at": utc_now(), "candidate": _candidate_ref(candidate),
            "current_applicability": "not_ready",
        })
        return record

    def _store(self, record, *, save):
        state = self.ensure_state()
        collection = normalize_layered_route_validation_collection(
            state.get("layered_route_validations")
        )
        stale_ids = []
        items = []
        new_candidate_id = (record.get("candidate") or {}).get("candidate_id")
        new_candidate_fingerprint = (record.get("candidate") or {}).get("candidate_fingerprint")
        new_fingerprint = (record.get("fingerprints") or {}).get("validation_fingerprint")
        # A not_ready attempt has no evidence fingerprint yet, so a changed candidate
        # fingerprint is resolved through the candidate reference instead: the previous
        # validation of a *different* candidate fingerprint must never stay ``current``.
        for existing in collection["items"]:
            same_id = existing.get("validation_id") == record.get("validation_id")
            same_candidate = (
                new_candidate_id
                and (existing.get("candidate") or {}).get("candidate_id") == new_candidate_id
            )
            old_fingerprint = (existing.get("fingerprints") or {}).get("validation_fingerprint")
            if same_id:
                if (existing.get("status") == "validated_candidate"
                        and record.get("status") != "validated_candidate"):
                    stale_ids.append(str(existing.get("validation_id")))
                continue
            if same_candidate and existing.get("status") != "stale":
                old_candidate_fingerprint = (existing.get("candidate") or {}).get(
                    "candidate_fingerprint"
                )
                candidate_changed = (
                    new_candidate_fingerprint is not None
                    and old_candidate_fingerprint is not None
                    and old_candidate_fingerprint != new_candidate_fingerprint
                )
                if (old_fingerprint != new_fingerprint) or candidate_changed:
                    existing["status"] = "stale"
                    existing["current_applicability"] = "stale"
                    existing["stale_reason"] = "validation_dependencies_changed"
                    stale_ids.append(str(existing.get("validation_id")))
            items.append(existing)
        items.append(record)
        collection["items"] = items
        collection["count"] = len(items)
        collection["status"] = record["status"]
        if record["status"] == "validated_candidate":
            collection["active_validation_id"] = record["validation_id"]
        state["layered_route_validations"] = collection
        state.setdefault("result_statuses", {})["layered_route_validation"] = record["status"]
        if stale_ids and callable(self.adoption_invalidator):
            self.adoption_invalidator(stale_ids, "validation_replaced_or_no_longer_validated")
        if save:
            self.session.save()
        return self.result_snapshot()

    def _select_current_candidate(self, payload):
        collection = self.layered.result_snapshot()
        items = list(collection.get("items") or [])
        wanted = str(payload.get("candidate_id") or "")
        if wanted:
            candidate = next((item for item in items if str(item.get("candidate_id")) == wanted), None)
        else:
            active = collection.get("active_candidate_id")
            candidate = next((item for item in items if item.get("candidate_id") == active), None)
            candidate = candidate or next((
                item for item in reversed(items)
                if item.get("status") == "candidate" and item.get("current_applicability") == "current"
            ), None)
        return candidate

    def _layer_for(self, candidate):
        layer_id = (candidate or {}).get("altitude_layer_id")
        return next((
            item for item in ((self.session.state.get("spatial_3d") or {}).get("altitude_layers") or [])
            if str(item.get("altitude_layer_id")) == str(layer_id)
        ), None)

    def _current_risk_profile(self, candidate):
        if candidate is None:
            return None
        collection = self.risk_profiles.result_snapshot()
        for item in reversed(collection.get("items") or []):
            reference = item.get("candidate") or {}
            if (
                item.get("status") == "passed"
                and item.get("current_applicability") == "current"
                and reference.get("candidate_id") == candidate.get("candidate_id")
                and reference.get("candidate_fingerprint") == candidate.get("candidate_fingerprint")
            ):
                return item
        return None

    def _source_readiness(self):
        audits = ((self.session.state.get("source_audits") or {}).get("items") or {})
        blockers = []
        selected = {}
        for role in ("terrain_dtm", "buildings"):
            item = audits.get(role) or {}
            selected[role] = _audit_fingerprint_view(item)
            if item.get("status") != "verified":
                blockers.append(_block(
                    f"{role}_source_not_verified", f"{role} source audit 必须为 verified",
                ))
        return {
            "status": "ready" if not blockers else "not_ready",
            "source_type": "configured_real_sources", "audits": selected,
            "blockers": blockers,
        }

    def _fingerprint_components(self, candidate, layer, policy, metric_crs, sources):
        source_status = self._source_readiness()
        return {
            "candidate_id": candidate.get("candidate_id"),
            "candidate_fingerprint": candidate.get("candidate_fingerprint"),
            "path_fingerprint": path_fingerprint(candidate.get("path") or []),
            "altitude_layer": {
                "altitude_layer_id": layer.get("altitude_layer_id"),
                "nominal_altitude_m": layer.get("nominal_altitude_m"),
                "vertical_reference": layer.get("vertical_reference"),
                "confirmed": bool(layer.get("confirmed")), "source": layer.get("source"),
                "evidence": layer.get("evidence"),
            },
            "terrain_vertical_clearance_m": policy["terrain_clearance_m"],
            "building_clearance": {
                "horizontal_clearance_m": policy["building_horizontal_clearance_m"],
                "vertical_clearance_m": policy["building_vertical_clearance_m"],
            },
            "source_audits": source_status["audits"],
            "source_evidence_fingerprints": {
                "terrain": stable_fingerprint(sources.get("terrain_dtm") or {}, prefix="terrain-source-"),
                "buildings": stable_fingerprint(sources.get("buildings") or {}, prefix="building-source-"),
            },
            "metric_crs": metric_crs,
            "validator_versions": deepcopy(VALIDATOR_VERSIONS),
        }

    def _project_current(self, item):
        projected = deepcopy(item)
        if item.get("status") == "stale":
            projected["current_applicability"] = "stale"
            return projected
        candidate = self._select_current_candidate({
            "candidate_id": (item.get("candidate") or {}).get("candidate_id")
        })
        if candidate is None or candidate.get("current_applicability") != "current":
            projected["current_applicability"] = "stale_candidate"
            return projected
        layer = self._layer_for(candidate)
        policy = {
            "terrain_clearance_m": (self.session.state.get("layered_route_feasibility_policy") or {}).get(
                "terrain_vertical_clearance_m"
            ),
            "building_horizontal_clearance_m": (self.session.state.get("building_clearance_policy") or {}).get(
                "horizontal_clearance_m"
            ),
            "building_vertical_clearance_m": (self.session.state.get("building_clearance_policy") or {}).get(
                "vertical_clearance_m"
            ),
        }
        stored = (item.get("fingerprints") or {}).get("components") or {}
        expected = self._fingerprint_components(
            candidate, layer or {}, policy, stored.get("metric_crs"),
            {
                "terrain_dtm": ((stored.get("source_evidence_fingerprints") or {}).get("terrain")),
                "buildings": ((stored.get("source_evidence_fingerprints") or {}).get("buildings")),
            },
        )
        # Source evidence was already fingerprinted.  Preserve those two stored digests while
        # recomputing every live dependency (candidate/layer/clearance/audits/versions).
        expected["source_evidence_fingerprints"] = deepcopy(
            stored.get("source_evidence_fingerprints") or {}
        )
        current = validation_fingerprint(expected)
        projected["current_applicability"] = (
            "current" if current == (item.get("fingerprints") or {}).get("validation_fingerprint")
            else "stale_inputs_changed"
        )
        return projected


def _constant_metric_route(metric_path, altitude_m):
    points = [
        [float(point[0]), float(point[1])]
        for point in metric_path or [] if isinstance(point, (list, tuple)) and len(point) >= 2
    ]
    primitives, distance = [], 0.0
    for index, (left, right) in enumerate(zip(points, points[1:])):
        length = hypot(right[0] - left[0], right[1] - left[1])
        primitives.append({
            "primitive_id": f"candidate-segment-{index}", "kind": "line",
            "distance_start_m": distance, "distance_end_m": distance + length,
            "horizontal_length_m": length,
            "z_start_egm2008_m": float(altitude_m),
            "z_end_egm2008_m": float(altitude_m),
        })
        distance += length
    return {
        "status": "realized",
        "horizontal_geometry": {"linearized": {
            "linestring_metric": points, "curve_chord_error_m": 0.0,
        }},
        "primitives": primitives,
        "semantics": ROUTE_SEMANTICS,
    }


def _candidate_ref(candidate):
    value = candidate or {}
    return {
        "candidate_id": value.get("candidate_id"), "route_id": value.get("route_id"),
        "altitude_layer_id": value.get("altitude_layer_id"),
        "candidate_fingerprint": value.get("candidate_fingerprint"),
        "path_fingerprint": path_fingerprint(value.get("path") or []) if value.get("path") else None,
        "status": value.get("status"),
        "current_applicability": value.get("current_applicability"),
    }


def _profile_ref(profile):
    value = profile or {}
    return {
        "profile_id": value.get("profile_id"), "status": value.get("status"),
        "current_applicability": value.get("current_applicability"),
        "classification_used_as_hard_gate": False,
        "thresholds_used_in_validation_fingerprint": False,
    }


def _audit_fingerprint_view(item):
    value = item if isinstance(item, dict) else {}
    verification = value.get("verification") or {}
    return {
        "role": value.get("role"), "status": value.get("status"),
        "file_name": value.get("file_name"), "size_bytes": value.get("size_bytes"),
        "mtime_ns": value.get("mtime_ns"), "sha256": verification.get("sha256"),
    }


def _block(code, reason):
    return {"reason_code": str(code), "reason": str(reason)}


def _resource_limit(payload):
    raw = (payload.get("resource_limits") or {}).get("max_evidence_items")
    if raw in (None, ""):
        raw = payload.get("max_evidence_items")
    if raw in (None, ""):
        raw = payload.get("max_validation_samples")
    if raw in (None, ""):
        return None
    value = int(raw)
    if value <= 0:
        raise ValueError("max_evidence_items 必须为正整数")
    return value


def _critical_evidence(terrain, building):
    evidence = []
    for domain_id, result in (("terrain", terrain), ("building", building)):
        evidence.append({
            "domain": domain_id, "status": result.get("status"),
            "minimum_margin": result.get("minimum_margin"),
            "reason": result.get("reason"),
            "first_failed_interval": deepcopy((result.get("violations") or [None])[0]),
            "first_unresolved_interval": deepcopy((result.get("unresolved") or [None])[0]),
            "source": deepcopy((result.get("evidence") or {}).get("source") or {}),
        })
    return evidence


__all__ = ["LayeredRouteValidationService"]
