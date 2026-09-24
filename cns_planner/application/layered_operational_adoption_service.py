"""Transactional operational adoption of a current production layered validation."""

from __future__ import annotations

from copy import deepcopy

from ..domain.layered_operational_adoption import (
    ALGORITHM_ID, ALGORITHM_VERSION, SCHEMA_VERSION, SOURCE_TYPE, adoption_fingerprint,
    normalize_layered_operational_adoptions,
)
from ..domain.layered_route_validation import stable_fingerprint, utc_now
from ..domain.spatial_3d import normalize_route_operating_layer
from .production_write_authority import assert_write_authority


class LayeredOperationalAdoptionService:
    """Preview/apply/revoke without merging candidate, validation and operational state."""

    def __init__(self, session, validation_service, invalidation, snapshot):
        self.session = session
        self.validations = validation_service
        self.invalidation = invalidation
        self.snapshot = snapshot
        self._grid_service = None
        self.ensure_state()

    def ensure_state(self):
        state = self.session.state
        state["layered_operational_adoptions"] = normalize_layered_operational_adoptions(
            state.get("layered_operational_adoptions")
        )
        return state

    def result_snapshot(self):
        collection = normalize_layered_operational_adoptions(
            self.ensure_state().get("layered_operational_adoptions")
        )
        projected = []
        for item in collection["items"]:
            entry = deepcopy(item)
            entry["ownership"] = self.ownership(item)
            if item.get("status") == "published":
                entry["current_applicability"] = self._applicability(item)
            projected.append(entry)
        collection["items"] = projected
        return collection

    def readiness_snapshot(self):
        validations = self.validations.result_snapshot()
        options = []
        for item in validations.get("items") or []:
            gate = self._gate(item)
            options.append({
                "validation_id": item.get("validation_id"),
                "route_id": (item.get("candidate") or {}).get("route_id"),
                "status": item.get("status"),
                "current_applicability": item.get("current_applicability"),
                "eligible": gate["eligible"], "reasons": gate["reasons"],
            })
        return {
            "status": "ready" if any(item["eligible"] for item in options) else "not_ready",
            "algorithm": {"algorithm_id": ALGORITHM_ID, "algorithm_version": ALGORITHM_VERSION},
            "options": options,
            "boundaries": {
                "explicit_confirmation_required": True,
                "configured_real_sources_only": True,
                "candidate_validation_operational_are_distinct": True,
                "route_altitude_profile_not_created": True,
                "departure_arrival_procedure_not_created": True,
            },
        }

    # ------------------------------------------------------------------ preview/projection

    def projection(self, payload=None):
        payload = payload if isinstance(payload, dict) else {}
        validation = self._select_validation(payload)
        if validation is None:
            return {"status": "not_ready", "reason": "validation_not_found"}
        gate = self._gate(validation)
        if not gate["eligible"]:
            return {
                "status": "not_ready", "reason": "publish_gate_blocked",
                "validation_id": validation.get("validation_id"), "reasons": gate["reasons"],
            }
        candidate = validation.get("candidate") or {}
        route_id = str(candidate.get("route_id") or "")
        path = [
            [float(point[0]), float(point[1])]
            for point in (validation.get("route") or {}).get("path") or []
            if isinstance(point, (list, tuple)) and len(point) == 2
        ]
        if len(path) < 2:
            return {"status": "not_ready", "reason": "validated_candidate_path_unavailable"}
        layer_id = str(candidate.get("altitude_layer_id") or "")
        scenario = next((
            item for item in self.session.state.get("scenario_routes") or []
            if str(item.get("route_id")) == route_id
        ), {})
        projection_fingerprint = stable_fingerprint({
            "validation_id": validation.get("validation_id"),
            "validation_fingerprint": (validation.get("fingerprints") or {}).get(
                "validation_fingerprint"
            ),
            "route_id": route_id, "path": path, "path_crs": "OGC:CRS84",
            "altitude_layer_id": layer_id,
        }, prefix="layeredprojectionv1-")
        route = {
            "route_id": route_id, "status": "passed", "path": path,
            "start": deepcopy(scenario.get("start") or path[0]),
            "end": deepcopy(scenario.get("end") or path[-1]),
            "start_node_id": scenario.get("start_node_id"),
            "end_node_id": scenario.get("end_node_id"),
            "path_crs": "OGC:CRS84",
            "kind": "layered_risk_aware_operational_route",
            "provenance": {
                "source_type": SOURCE_TYPE,
                "validation_id": validation.get("validation_id"),
                "validation_fingerprint": (validation.get("fingerprints") or {}).get(
                    "validation_fingerprint"
                ),
                "candidate_id": candidate.get("candidate_id"),
                "candidate_fingerprint": candidate.get("candidate_fingerprint"),
                "projection_fingerprint": projection_fingerprint,
                "two_dimensional_crs84_path": True,
                "egm2008_altitude_in_third_coordinate": False,
            },
        }
        assignment = normalize_route_operating_layer({
            "route_id": route_id, "altitude_layer_id": layer_id,
            "operating_mode": "fixed_cruise_layer",
            "vertical_reference": "egm2008_orthometric", "source": SOURCE_TYPE,
            "evidence": {
                "validation_id": validation.get("validation_id"),
                "validation_fingerprint": (validation.get("fingerprints") or {}).get(
                    "validation_fingerprint"
                ),
                "candidate_id": candidate.get("candidate_id"),
                "candidate_fingerprint": candidate.get("candidate_fingerprint"),
                "projection_fingerprint": projection_fingerprint,
                "planning_input_provenance_propagation": True,
                "altitude_inferred": False,
            },
            "confirmed": True, "active": True,
            "adoption_owned": True,
        })
        conflict = self._conflict(route_id)
        return {
            "status": "ready", "validation_id": validation.get("validation_id"),
            "route_id": route_id, "projection_fingerprint": projection_fingerprint,
            "route": route, "route_operating_layer": assignment,
            "conflict": conflict,
            "replacement_requested": payload.get("replace_existing") is True,
            "apply_blocked_by_conflict": bool(conflict and payload.get("replace_existing") is not True),
            "two_dimensional_path_only": True,
            "altitude_representation": {
                "carried_by": "RouteOperatingLayer -> AltitudeLayer",
                "vertical_reference": "egm2008_orthometric",
                "in_crs84_third_coordinate": False,
                "route_altitude_profile_created": False,
            },
        }

    def preview(self, payload=None):
        before = deepcopy(self.session.state)
        projection = self.projection(payload)
        result = {
            "status": "ready" if projection.get("status") == "ready" else "not_ready",
            "projection": projection,
            "publication_allowed": bool(
                projection.get("status") == "ready"
                and not projection.get("apply_blocked_by_conflict")
            ),
            "side_effects": False,
        }
        result["preview_fingerprint"] = stable_fingerprint(result, prefix="layeredpreviewv1-")
        if self.session.state != before:
            raise RuntimeError("Preview contract violated: state changed")
        return result

    # ------------------------------------------------------------------ apply

    def apply(self, payload=None):
        payload = payload if isinstance(payload, dict) else {}
        if payload.get("confirmed") is not True:
            raise ValueError("Layered operational Apply 必须显式 confirmed=true")
        projection = self.projection(payload)
        if projection.get("status") != "ready":
            raise ValueError("Layered operational Apply gate 未通过：" + str(projection.get("reason")))
        if projection.get("apply_blocked_by_conflict"):
            raise ValueError("route_id 已有非本 adoption-owned route；必须显式 replace_existing=true")
        expected = payload.get("expected_validation_fingerprint")
        validation = self._select_validation(payload)
        current_fingerprint = (validation.get("fingerprints") or {}).get("validation_fingerprint")
        if expected not in (None, "") and str(expected) != str(current_fingerprint):
            raise ValueError("validation fingerprint 在 Preview 后发生变化，拒绝 Apply")

        original = self.session.state
        working = deepcopy(original)
        self.session.state = working
        try:
            adoption = self._apply_working(projection, validation, payload)
            normalized = self._normalize(working)
        except Exception:
            self.session.state = original
            raise
        _install_in_place(original, normalized)
        self.session.state = original
        self.session.save()
        return {
            "status": "passed", "adoption_id": adoption["adoption_id"],
            "route_id": adoption["route_id"],
            "route_operating_layer_created": True,
            "route_altitude_profile_created": False,
            "departure_arrival_procedure_created": False,
            "snapshot": self.snapshot(),
        }

    def _apply_working(self, projection, validation, payload):
        state = self.session.state
        route_id = projection["route_id"]
        routes = list(state.get("operational_routes") or [])
        previous_route = next((
            item for item in routes if str(item.get("route_id")) == route_id
        ), None)
        assignments = list((state.setdefault("spatial_3d", {})).get("route_operating_layers") or [])
        previous_assignment = next((
            item for item in assignments if str(item.get("route_id")) == route_id
        ), None)
        identity = adoption_fingerprint({
            "route_id": route_id, "validation_id": validation.get("validation_id"),
            "validation_fingerprint": (validation.get("fingerprints") or {}).get(
                "validation_fingerprint"
            ),
            "projection_fingerprint": projection.get("projection_fingerprint"),
            "altitude_layer_id": (validation.get("candidate") or {}).get("altitude_layer_id"),
        })
        adoption_id = "LRA-" + identity[-12:].upper()
        route = deepcopy(projection["route"])
        route["provenance"].update({
            "adoption_id": adoption_id, "adoption_fingerprint": identity,
        })
        assignment = deepcopy(projection["route_operating_layer"])
        assignment["adoption_owned"] = True
        assignment.setdefault("evidence", {}).update({
            "adoption_id": adoption_id, "adoption_fingerprint": identity,
        })
        # Phase4-B2B-1：本服务是 canonical ``operational_routes`` 的唯一 production
        # content owner；写入前必须通过 authority guard。
        assert_write_authority(self, "operational_routes")
        state["operational_routes"] = [
            item for item in routes if str(item.get("route_id")) != route_id
        ] + [route]
        state["operational_routes"].sort(key=lambda item: str(item.get("route_id")))
        state["spatial_3d"]["route_operating_layers"] = [
            item for item in assignments if str(item.get("route_id")) != route_id
        ] + [assignment]
        adoption = {
            "schema_version": SCHEMA_VERSION, "adoption_id": adoption_id,
            "route_id": route_id, "status": "published", "current_applicability": "current",
            "applied_at": utc_now(), "revoked_at": None,
            "validation_id": validation.get("validation_id"),
            "validation_fingerprint": (validation.get("fingerprints") or {}).get(
                "validation_fingerprint"
            ),
            "candidate_id": (validation.get("candidate") or {}).get("candidate_id"),
            "candidate_fingerprint": (validation.get("candidate") or {}).get(
                "candidate_fingerprint"
            ),
            "projection_fingerprint": projection.get("projection_fingerprint"),
            "altitude_layer_id": (validation.get("candidate") or {}).get("altitude_layer_id"),
            "source_type": SOURCE_TYPE,
            "replace_existing": payload.get("replace_existing") is True,
            "before": {
                "route": deepcopy(previous_route),
                "route_operating_layer": deepcopy(previous_assignment),
                "route_provenance": deepcopy((previous_route or {}).get("provenance") or {}),
                "assignment_provenance": deepcopy((previous_assignment or {}).get("evidence") or {}),
            },
            "after": {"route": deepcopy(route), "route_operating_layer": deepcopy(assignment)},
            "ownership": {"route_owned": True, "route_operating_layer_owned": True},
            "stale_reason": None,
            "provenance": {
                "explicit_confirmation": True,
                "configured_real_sources": True,
                "route_altitude_profile_created": False,
                "departure_arrival_procedure_created": False,
            },
        }
        collection = normalize_layered_operational_adoptions(
            state.get("layered_operational_adoptions")
        )
        for old in collection["items"]:
            if str(old.get("route_id")) == route_id and old.get("status") == "published":
                old["status"] = "stale"
                old["current_applicability"] = "superseded"
                old["stale_reason"] = "superseded_by_new_layered_adoption"
        collection["items"].append(adoption)
        collection["count"] = len(collection["items"])
        collection["status"] = "passed"
        state["layered_operational_adoptions"] = collection
        state.setdefault("result_statuses", {})["routes"] = "passed"
        self.invalidation.operational_route_published(
            {route_id}, reason="layered_operational_route_published",
        )
        return adoption

    # ------------------------------------------------------------------ revoke / stale / ownership

    def revoke(self, payload=None):
        payload = payload if isinstance(payload, dict) else {}
        if payload.get("confirmed") is not True:
            raise ValueError("Layered operational Revoke 必须显式 confirmed=true")
        target = self._select_adoption(payload)
        if target is None:
            raise ValueError("Layered operational adoption 不存在或已撤销")
        original = self.session.state
        working = deepcopy(original)
        self.session.state = working
        try:
            outcome = self._revoke_working(target)
            normalized = self._normalize(working)
        except Exception:
            self.session.state = original
            raise
        _install_in_place(original, normalized)
        self.session.state = original
        self.session.save()
        return {**outcome, "snapshot": self.snapshot()}

    def _revoke_working(self, target):
        state = self.session.state
        route_id = str(target.get("route_id"))
        ownership = self.ownership(target)
        restored_route = restored_assignment = False
        if ownership["route_owned"]:
            routes = [
                item for item in state.get("operational_routes") or []
                if str(item.get("route_id")) != route_id
            ]
            previous = deepcopy((target.get("before") or {}).get("route"))
            if previous is not None:
                routes.append(previous)
                restored_route = True
            # 撤销/恢复派生运行航路同样在 canonical 写点上受 guard 约束（revoke 白名单）。
            assert_write_authority(self, "operational_routes", operation="revoke")
            state["operational_routes"] = routes
        if ownership["route_operating_layer_owned"]:
            assignments = [
                item for item in (state.get("spatial_3d") or {}).get("route_operating_layers") or []
                if str(item.get("route_id")) != route_id
            ]
            previous = deepcopy((target.get("before") or {}).get("route_operating_layer"))
            if previous is not None:
                assignments.append(previous)
                restored_assignment = True
            state["spatial_3d"]["route_operating_layers"] = assignments
        collection = normalize_layered_operational_adoptions(
            state.get("layered_operational_adoptions")
        )
        for item in collection["items"]:
            if str(item.get("adoption_id")) == str(target.get("adoption_id")):
                item["status"] = "revoked"
                item["current_applicability"] = "revoked"
                item["revoked_at"] = utc_now()
                item["stale_reason"] = "revoked_by_user"
                item["ownership"] = ownership
        state["layered_operational_adoptions"] = collection
        if ownership["route_owned"] or ownership["route_operating_layer_owned"]:
            self.invalidation.operational_route_published(
                {route_id}, reason="layered_operational_adoption_revoked",
            )
        return {
            "status": "passed", "adoption_id": target.get("adoption_id"),
            "route_id": route_id, "removed_or_restored_route": ownership["route_owned"],
            "removed_or_restored_route_operating_layer": ownership["route_operating_layer_owned"],
            "restored_previous_route": restored_route,
            "restored_previous_route_operating_layer": restored_assignment,
            "preserved_foreign_modifications": not all(ownership.values()),
        }

    def stale_for_validations(self, validation_ids, reason="validation_stale"):
        wanted = {str(item) for item in validation_ids or []}
        collection = normalize_layered_operational_adoptions(
            self.ensure_state().get("layered_operational_adoptions")
        )
        stale_adoptions, stale_routes = [], set()
        for item in collection["items"]:
            if str(item.get("validation_id")) not in wanted or item.get("status") == "revoked":
                continue
            item["status"] = "stale"
            item["current_applicability"] = "stale"
            item["stale_reason"] = str(reason)
            stale_adoptions.append(str(item.get("adoption_id")))
            ownership = self.ownership(item)
            if ownership["route_owned"]:
                route = self._route(item.get("route_id"))
                route["status"] = "stale"
                route["stale_reason"] = str(reason)
                stale_routes.add(str(item.get("route_id")))
            if ownership["route_operating_layer_owned"]:
                assignment = self._assignment(item.get("route_id"))
                assignment["status"] = "stale"
                assignment["current_applicability"] = "stale_evidence"
                assignment["stale_reason"] = str(reason)
        if stale_adoptions:
            collection["status"] = "stale"
            self.session.state["layered_operational_adoptions"] = collection
            self.session.state.setdefault("result_statuses", {})["routes"] = "stale"
            self.invalidation.operational_route_published(
                stale_routes, reason="layered_validation_evidence_outdated",
                preserve_published_routes=False,
            )
        return {"stale_adoption_ids": stale_adoptions, "stale_route_ids": sorted(stale_routes)}

    def ownership(self, adoption):
        route = self._route(adoption.get("route_id"))
        assignment = self._assignment(adoption.get("route_id"))
        expected_id = str(adoption.get("adoption_id") or "")
        expected_fingerprint = adoption_fingerprint({
            "route_id": adoption.get("route_id"),
            "validation_id": adoption.get("validation_id"),
            "validation_fingerprint": adoption.get("validation_fingerprint"),
            "projection_fingerprint": adoption.get("projection_fingerprint"),
            "altitude_layer_id": adoption.get("altitude_layer_id"),
        })
        route_provenance = (route or {}).get("provenance") or {}
        assignment_evidence = (assignment or {}).get("evidence") or {}
        return {
            "route_owned": bool(
                route is not None and route_provenance.get("source_type") == SOURCE_TYPE
                and str(route_provenance.get("adoption_id") or "") == expected_id
                and route_provenance.get("adoption_fingerprint") == expected_fingerprint
            ),
            "route_operating_layer_owned": bool(
                assignment is not None and assignment.get("source") == SOURCE_TYPE
                and str(assignment_evidence.get("adoption_id") or "") == expected_id
                and assignment_evidence.get("adoption_fingerprint") == expected_fingerprint
            ),
        }

    # ------------------------------------------------------------------ helpers

    def _gate(self, validation):
        reasons = []
        if validation.get("status") != "validated_candidate":
            reasons.append(f"validation_status:{validation.get('status')}")
        if validation.get("current_applicability") != "current":
            reasons.append("validation_not_current")
        if validation.get("source_type") != "configured_real_sources":
            reasons.append("configured_real_sources_required")
        candidate = self.validations._select_current_candidate({
            "candidate_id": (validation.get("candidate") or {}).get("candidate_id")
        })
        if candidate is None or candidate.get("current_applicability") != "current":
            reasons.append("referenced_candidate_not_current")
        if self.validations._current_risk_profile(candidate) is None:
            reasons.append("current_route_risk_profile_missing")
        if not self._projection_ready(validation):
            reasons.append("projection_not_ready")
        source = self.validations._source_readiness()
        if source["status"] != "ready":
            reasons.append("configured_real_source_chain_not_ready")
        return {"eligible": not reasons, "reasons": reasons}

    @staticmethod
    def _projection_ready(validation):
        route = validation.get("route") or {}
        path = route.get("path") or []
        return bool(
            route.get("path_crs") == "OGC:CRS84" and len(path) >= 2
            and all(isinstance(point, (list, tuple)) and len(point) == 2 for point in path)
        )

    def _select_validation(self, payload):
        collection = self.validations.result_snapshot()
        wanted = str((payload or {}).get("validation_id") or "")
        if wanted:
            return next((
                item for item in collection.get("items") or []
                if str(item.get("validation_id")) == wanted
            ), None)
        active = collection.get("active_validation_id")
        return next((
            item for item in collection.get("items") or []
            if item.get("validation_id") == active
        ), None)

    def _select_adoption(self, payload):
        adoption_id = str((payload or {}).get("adoption_id") or "")
        route_id = str((payload or {}).get("route_id") or "")
        for item in reversed(self.ensure_state()["layered_operational_adoptions"]["items"]):
            if item.get("status") == "revoked":
                continue
            if adoption_id and str(item.get("adoption_id")) != adoption_id:
                continue
            if route_id and str(item.get("route_id")) != route_id:
                continue
            return item
        return None

    def _conflict(self, route_id):
        route = self._route(route_id)
        if route is None:
            return None
        provenance = route.get("provenance") or {}
        if provenance.get("source_type") == SOURCE_TYPE:
            return None
        return {
            "route_id": route_id, "existing_status": route.get("status"),
            "existing_source_type": provenance.get("source_type") or "manual_or_other_planner",
            "replacement_requires_explicit_confirmation": True,
        }

    def _route(self, route_id):
        return next((
            item for item in self.session.state.get("operational_routes") or []
            if str(item.get("route_id")) == str(route_id)
        ), None)

    def _assignment(self, route_id):
        return next((
            item for item in ((self.session.state.get("spatial_3d") or {}).get(
                "route_operating_layers"
            ) or []) if str(item.get("route_id")) == str(route_id)
        ), None)

    def _applicability(self, adoption):
        validation = self._select_validation({"validation_id": adoption.get("validation_id")})
        if validation is None or validation.get("current_applicability") != "current":
            return "stale_validation"
        ownership = self.ownership(adoption)
        return "current" if all(ownership.values()) else "ownership_changed"

    def _normalize(self, state):
        from .project_state import normalize_project
        if self._grid_service is None:
            from ..algorithms.grid.service import WorkspaceGridService
            self._grid_service = WorkspaceGridService()
        return normalize_project(state, self._grid_service)


def _install_in_place(target, replacement):
    target.clear()
    target.update(replacement)
    return target


__all__ = ["LayeredOperationalAdoptionService"]
