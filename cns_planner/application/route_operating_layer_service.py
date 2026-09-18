"""Layered Operational Route Architecture V1 use cases.

Production route = ``DepartureProcedure → fixed cruise AltitudeLayer + horizontal route →
ArrivalProcedure``.  One concrete route carries exactly one cruise altitude layer, and
vertical transitions stay in the terminal procedures: they never enter horizontal route
planning.  This module owns the explicit, traceable CRUD for the altitude layer catalogue,
the route → layer assignments and the departure/arrival procedure contracts, plus the
read-only readiness report and ``RouteOperatingPlan`` projection.

Everything here is additive.  ``RouteAltitudeProfile`` (including the advanced/V3-D locked
waypoint profile) keeps its existing semantics and is never converted into a cruise layer.
"""

from copy import deepcopy

from ..domain.spatial_3d import (
    UNRECORDED_SOURCE, normalize_altitude_layer,
    normalize_departure_arrival_procedure, normalize_route_operating_layer,
    procedure_missing_evidence,
)


PRODUCTION_ROUTE_DEFINITION = (
    "DepartureProcedure -> fixed cruise AltitudeLayer + horizontal route -> ArrivalProcedure"
)

READINESS_SEMANTICS = {
    "missing_value_is_pending_not_unsafe_and_not_zero": True,
    "no_default_altitude_or_vertical_datum_is_invented": True,
    "one_active_cruise_layer_per_route": True,
    "route_layer_is_never_matched_from_a_route_altitude_profile": True,
    "advanced_variable_profile_is_not_the_production_cruise_layer": True,
}

PLAN_SEMANTICS = {
    "production_route_definition": PRODUCTION_ROUTE_DEFINITION,
    "cruise_and_terminal_transition_are_separate_contracts": True,
    "vertical_transition_never_enters_horizontal_route_planning": True,
    "one_fixed_cruise_layer_per_route": True,
    "advanced_variable_profile_is_display_only": True,
    "read_only_projection": True,
}


def _route_ids(state):
    return [str(item.get("route_id")) for item in state.get("operational_routes") or []]


def _node_ids(state):
    return {str(item.get("node_id")) for item in state.get("nodes") or []}


def _layer_map(spatial):
    return {item["altitude_layer_id"]: item for item in spatial.get("altitude_layers") or []}


def _active_assignments(spatial):
    return {
        str(item.get("route_id")): item
        for item in spatial.get("route_operating_layers") or []
        if item.get("active", True)
    }


def _procedures(spatial, procedure_type=None):
    items = list(spatial.get("departure_arrival_procedures") or [])
    if procedure_type is None:
        return items
    return [item for item in items if item.get("procedure_type") == procedure_type]


def _layer_reasons(layer):
    """Why a catalogue entry is not production-confirmed.  Never a guessed value."""

    reasons = []
    if layer.get("nominal_altitude_m") is None:
        reasons.append("缺少显式 nominal_altitude_m：保持待工程确认，绝不取上下界中值")
    if str(layer.get("vertical_reference") or "unknown") == "unknown":
        reasons.append("vertical_reference 未确认：不猜垂向基准")
    if str(layer.get("source") or "") in ("", UNRECORDED_SOURCE):
        reasons.append("缺少 source/evidence 来源")
    if not layer.get("confirmed"):
        reasons.append("尚未显式确认")
    return reasons


def _bucket(status, reasons, **extra):
    return {"status": status, "reasons": [str(item) for item in reasons], **extra}


def route_operating_readiness(state):
    """Four independently reported readiness buckets.

    Missing values are reported as ``pending_confirmation``; they are never reported as
    unsafe and never silently read as a zero/nominal value.
    """

    spatial = state.get("spatial_3d") or {}
    routes = _route_ids(state)
    nodes = _node_ids(state)
    layers = list(spatial.get("altitude_layers") or [])
    layer_map = _layer_map(spatial)
    assignments = _active_assignments(spatial)
    catalog = []
    for layer in layers:
        reasons = _layer_reasons(layer)
        catalog.append({
            "altitude_layer_id": layer["altitude_layer_id"],
            "name": layer.get("name"),
            "nominal_altitude_m": layer.get("nominal_altitude_m"),
            "lower_altitude_m": layer.get("lower_altitude_m"),
            "upper_altitude_m": layer.get("upper_altitude_m"),
            "vertical_reference": layer.get("vertical_reference"),
            "source": layer.get("source"),
            "confirmed": bool(layer.get("confirmed")),
            "status": "confirmed" if not reasons else "pending_confirmation",
            "reasons": reasons,
        })
    catalog_reasons = []
    if not catalog:
        catalog_reasons.append("尚未配置任何 AltitudeLayer：待工程确认，不提供默认高度")
    else:
        catalog_reasons.extend(
            f"{item['altitude_layer_id']}：{reason}" for item in catalog for reason in item["reasons"]
        )
    catalog_bucket = _bucket(
        "confirmed" if catalog and not catalog_reasons else "pending_confirmation",
        catalog_reasons,
        configured_count=len(catalog),
        confirmed_count=sum(1 for item in catalog if item["status"] == "confirmed"),
        missing_nominal_count=sum(1 for item in layers if item.get("nominal_altitude_m") is None),
        unknown_vertical_reference_count=sum(
            1 for item in layers if str(item.get("vertical_reference") or "unknown") == "unknown"
        ),
        layers=catalog,
    )

    assignment_entries = []
    for route_id in routes:
        assignment = assignments.get(route_id)
        if assignment is None:
            assignment_entries.append({
                "route_id": route_id, "altitude_layer_id": None, "operating_mode": None,
                "vertical_reference": None, "nominal_altitude_m": None,
                "status": "pending_confirmation",
                "reasons": ["尚未为该运行航路显式选择巡航高度层；不从 RouteAltitudeProfile 数值推断"],
            })
            continue
        layer = layer_map.get(assignment["altitude_layer_id"])
        reasons = []
        if layer is None:
            reasons.append(f"引用的 AltitudeLayer 不存在：{assignment['altitude_layer_id']}")
        else:
            reasons.extend(
                f"AltitudeLayer {layer['altitude_layer_id']}：{reason}" for reason in _layer_reasons(layer)
            )
            if assignment.get("vertical_reference") != layer.get("vertical_reference"):
                reasons.append("vertical_reference 与引用的 AltitudeLayer 不一致")
        if str(assignment.get("source") or "") in ("", UNRECORDED_SOURCE):
            reasons.append("缺少 source/evidence 来源")
        if not assignment.get("confirmed"):
            reasons.append("尚未显式确认")
        assignment_entries.append({
            "route_id": route_id,
            "altitude_layer_id": assignment["altitude_layer_id"],
            "operating_mode": assignment.get("operating_mode"),
            "vertical_reference": assignment.get("vertical_reference"),
            "nominal_altitude_m": (layer or {}).get("nominal_altitude_m"),
            "status": "confirmed" if not reasons else "pending_confirmation",
            "reasons": reasons,
        })
    unassigned = [item["route_id"] for item in assignment_entries if item["status"] != "confirmed"]
    assignment_reasons = []
    if not routes:
        assignment_reasons.append("当前没有运行航路")
    else:
        assignment_reasons.extend(
            f"{item['route_id']}：{reason}" for item in assignment_entries for reason in item["reasons"]
        )
    assignment_bucket = _bucket(
        "confirmed" if routes and not assignment_reasons else "pending_confirmation",
        assignment_reasons,
        route_count=len(routes),
        assigned_count=sum(1 for item in assignment_entries if item["altitude_layer_id"]),
        confirmed_count=sum(1 for item in assignment_entries if item["status"] == "confirmed"),
        unassigned_route_ids=unassigned,
        routes=assignment_entries,
    )

    buckets = {
        "altitude_layer_catalog": catalog_bucket,
        "route_layer_assignment": assignment_bucket,
    }
    for procedure_type in ("departure", "arrival"):
        items = _procedures(spatial, procedure_type)
        entries = []
        for item in items:
            reasons = []
            if item.get("route_id") not in routes:
                reasons.append("绑定的运行航路不存在")
            layer = layer_map.get(item.get("altitude_layer_id"))
            if layer is None:
                reasons.append(f"引用的 AltitudeLayer 不存在：{item.get('altitude_layer_id')}")
            else:
                reasons.extend(
                    f"AltitudeLayer {layer['altitude_layer_id']}：{reason}"
                    for reason in _layer_reasons(layer)
                )
            missing = procedure_missing_evidence(item)
            reasons.extend(f"缺少显式证据：{name}（不补默认值）" for name in missing)
            if item.get("node_id") and item["node_id"] not in nodes:
                reasons.append(f"绑定的 node 不存在：{item['node_id']}")
            if str(item.get("source") or "") in ("", UNRECORDED_SOURCE):
                reasons.append("缺少 source/evidence 来源")
            if not item.get("confirmed"):
                reasons.append("尚未显式确认")
            entries.append({
                "procedure_id": item["procedure_id"],
                "route_id": item.get("route_id"),
                "altitude_layer_id": item.get("altitude_layer_id"),
                "transition_mode": item.get("transition_mode"),
                "node_id": item.get("node_id"),
                "site_reference": item.get("site_reference"),
                "missing_evidence": missing,
                "status": "confirmed" if not reasons else "pending_confirmation",
                "reasons": reasons,
            })
        covered = {item.get("route_id") for item in items}
        uncovered = [route_id for route_id in routes if route_id not in covered]
        reasons = []
        if not routes:
            reasons.append("当前没有运行航路")
        elif not items:
            reasons.append(f"尚未配置任何 {procedure_type} procedure")
        reasons.extend(
            f"{item['procedure_id']}：{reason}" for item in entries for reason in item["reasons"]
        )
        reasons.extend(f"{route_id}：缺少 {procedure_type} procedure" for route_id in uncovered)
        buckets[f"{procedure_type}_procedure"] = _bucket(
            "confirmed" if routes and not reasons else "pending_confirmation",
            reasons,
            procedure_count=len(entries),
            confirmed_count=sum(1 for item in entries if item["status"] == "confirmed"),
            uncovered_route_ids=uncovered,
            procedures=entries,
        )

    overall = "passed" if all(
        item["status"] == "confirmed" for item in buckets.values()
    ) else "pending_confirmation"
    return {
        "status": overall,
        "production_route_definition": PRODUCTION_ROUTE_DEFINITION,
        "semantics": deepcopy(READINESS_SEMANTICS),
        **buckets,
    }


def _procedure_entry(procedure):
    if procedure is None:
        return {
            "procedure_id": None, "status": "pending_confirmation",
            "reasons": ["尚未配置；缺值保持 pending，不等于 unsafe"],
        }
    return {
        "procedure_id": procedure["procedure_id"],
        "procedure_type": procedure.get("procedure_type"),
        "altitude_layer_id": procedure.get("altitude_layer_id"),
        "transition_mode": procedure.get("transition_mode"),
        "missing_evidence": list(procedure.get("missing_evidence") or []),
        "status": procedure.get("status"),
    }


def route_operating_plan(state):
    """Read-only production-route projection: horizontal route + cruise layer + procedures."""

    spatial = state.get("spatial_3d") or {}
    readiness = route_operating_readiness(state)
    layer_map = _layer_map(spatial)
    assignments = _active_assignments(spatial)
    profiles = spatial.get("route_altitude_profiles") or {}
    assignment_readiness = {
        item["route_id"]: item for item in readiness["route_layer_assignment"]["routes"]
    }
    route_plan = []
    for route in state.get("operational_routes") or []:
        route_id = str(route.get("route_id"))
        assignment = assignments.get(route_id)
        layer = layer_map.get(assignment["altitude_layer_id"]) if assignment else None
        entry_readiness = assignment_readiness.get(route_id, {"reasons": []})
        if assignment is None:
            cruise = {
                "status": "pending_confirmation",
                "altitude_layer_id": None,
                "operating_mode": None,
                "nominal_altitude_m": None,
                "vertical_reference": None,
                "reasons": list(entry_readiness.get("reasons") or []),
            }
        else:
            cruise = {
                "status": entry_readiness.get("status"),
                "altitude_layer_id": assignment["altitude_layer_id"],
                "operating_mode": assignment.get("operating_mode"),
                "nominal_altitude_m": (layer or {}).get("nominal_altitude_m"),
                "vertical_reference": assignment.get("vertical_reference"),
                "reasons": list(entry_readiness.get("reasons") or []),
            }
        departure = [item for item in _procedures(spatial, "departure") if item.get("route_id") == route_id]
        arrival = [item for item in _procedures(spatial, "arrival") if item.get("route_id") == route_id]
        profile = profiles.get(route_id)
        path = route.get("path") or []
        route_plan.append({
            "route_id": route_id,
            "horizontal_route": {
                "route_id": route_id,
                "status": route.get("status"),
                "distance_m": route.get("distance_m"),
                "vertex_count": len(path),
                "source": "operational_routes",
            },
            "cruise_layer": cruise,
            "terminal_transition": {
                "departure": _procedure_entry(departure[0] if departure else None),
                "arrival": _procedure_entry(arrival[0] if arrival else None),
                "semantics": "terminal_transition_is_separate_from_the_fixed_cruise_layer",
            },
            "advanced_variable_profile": {
                "present": profile is not None,
                "mode": (profile or {}).get("mode"),
                "source": (profile or {}).get("source"),
                "derived": bool((profile or {}).get("derived")),
                "locked": bool((profile or {}).get("locked")),
                "locked_by_adoption": bool((profile or {}).get("locked_by_adoption")),
                "semantics": "advanced_variable_profile",
                "v3c_validated_route": str((profile or {}).get("source") or "") == "v3c_validated_route",
                "is_production_cruise_layer": False,
            },
            "status": (
                "confirmed"
                if cruise["status"] == "confirmed"
                and all(item["status"] == "confirmed" for item in (
                    _procedure_entry(departure[0] if departure else None),
                    _procedure_entry(arrival[0] if arrival else None),
                ))
                else "pending_confirmation"
            ),
            "reasons": list(entry_readiness.get("reasons") or []),
        })
    return {
        "status": readiness["status"],
        "production_route_definition": PRODUCTION_ROUTE_DEFINITION,
        "semantics": deepcopy(PLAN_SEMANTICS),
        "altitude_layer_catalog": deepcopy(readiness["altitude_layer_catalog"]["layers"]),
        "routes": route_plan,
        "readiness": readiness,
    }


def resync_operating_layer_statuses(state):
    """Recompute assignment/procedure status from the current catalogue and route facts.

    Only ever downgrades to ``pending_confirmation`` when a reference, vertical reference,
    confirmation or evidence fact is missing.  It never invents a value and never promotes
    anything without an explicit recorded confirmation.
    """

    spatial = state.setdefault("spatial_3d", {})
    routes = set(_route_ids(state))
    nodes = _node_ids(state)
    layer_map = _layer_map(spatial)
    for item in spatial.get("route_operating_layers") or []:
        layer = layer_map.get(item.get("altitude_layer_id"))
        resolvable = (
            bool(item.get("active", True))
            and str(item.get("route_id")) in routes
            and layer is not None
            and layer.get("vertical_reference") == item.get("vertical_reference")
            and layer.get("status") == "confirmed"
            and str(item.get("source") or "") not in ("", UNRECORDED_SOURCE)
        )
        item["status"] = (
            "confirmed" if item.get("confirmed") and resolvable else "pending_confirmation"
        )
    for item in spatial.get("departure_arrival_procedures") or []:
        layer = layer_map.get(item.get("altitude_layer_id"))
        missing = procedure_missing_evidence(item)
        item["missing_evidence"] = missing
        resolvable = (
            str(item.get("route_id")) in routes
            and layer is not None
            and layer.get("status") == "confirmed"
            and str(item.get("source") or "") not in ("", UNRECORDED_SOURCE)
            and not missing
            and (not item.get("node_id") or item.get("node_id") in nodes)
        )
        item["status"] = (
            "confirmed" if item.get("confirmed") and resolvable else "pending_confirmation"
        )


def refresh_spatial_status(state):
    spatial = state["spatial_3d"]
    configured = [
        *(spatial.get("altitude_layers") or []),
        *(spatial.get("route_operating_layers") or []),
        *(spatial.get("departure_arrival_procedures") or []),
        *(spatial.get("route_altitude_profiles") or {}).values(),
        *(spatial.get("site_vertical_profiles") or {}).values(),
    ]
    spatial["status"] = (
        "passed"
        if configured and all(item.get("status") in ("passed", "confirmed") for item in configured)
        else "pending_confirmation"
    )


class RouteOperatingLayerService:
    """Explicit, traceable writes for the layered operational route configuration."""

    def __init__(self, session, invalidation, snapshot):
        self.session, self.invalidation, self.snapshot = session, invalidation, snapshot

    # ---- read-only projections ----------------------------------------------------
    def readiness_snapshot(self):
        return deepcopy(route_operating_readiness(self.session.state))

    def plan_snapshot(self):
        return deepcopy(route_operating_plan(self.session.state))

    # ---- altitude layer catalogue -------------------------------------------------
    def set_altitude_layer(self, payload):
        _require_traceable(payload)
        layer = normalize_altitude_layer(payload)
        spatial = self.session.state["spatial_3d"]
        self._assert_layer_reference_consistency(layer)
        layers = [
            item for item in spatial.get("altitude_layers") or []
            if item["altitude_layer_id"] != layer["altitude_layer_id"]
        ]
        layers.append(layer)
        layers.sort(key=lambda item: item["altitude_layer_id"])
        spatial["altitude_layers"] = layers
        return self._commit("altitude_layer_changed")

    def delete_altitude_layer(self, payload):
        layer_id = str((payload or {}).get("altitude_layer_id") or "").strip()
        if not layer_id:
            raise ValueError("altitude_layer_id 不能为空")
        spatial = self.session.state["spatial_3d"]
        layers = spatial.get("altitude_layers") or []
        if layer_id not in {item["altitude_layer_id"] for item in layers}:
            raise ValueError(f"高度层不存在：{layer_id}")
        references = self._layer_references(layer_id)
        if references:
            raise ValueError(
                "高度层仍被引用，必须先显式解除引用：" + "；".join(references)
            )
        spatial["altitude_layers"] = [
            item for item in layers if item["altitude_layer_id"] != layer_id
        ]
        return self._commit("altitude_layer_deleted")

    # ---- route → cruise layer assignment ------------------------------------------
    def set_route_operating_layer(self, payload):
        _require_traceable(payload)
        data = dict(payload or {})
        route_id = str(data.get("route_id") or "").strip()
        if not route_id:
            raise ValueError("route_id 不能为空")
        if route_id not in set(_route_ids(self.session.state)):
            raise ValueError("巡航高度层配置对应的运行航路不存在")
        layer = self._layer(str(data.get("altitude_layer_id") or "").strip())
        if not data.get("vertical_reference"):
            # The datum is inherited from the referenced AltitudeLayer; it is never inferred
            # from a RouteAltitudeProfile or from any other numeric source.
            data["vertical_reference"] = layer["vertical_reference"]
        assignment = normalize_route_operating_layer({**data, "active": True})
        if assignment["vertical_reference"] != layer["vertical_reference"]:
            raise ValueError(
                "巡航高度层配置的 vertical_reference 与其 AltitudeLayer 不一致"
            )
        spatial = self.session.state["spatial_3d"]
        others = [
            item for item in spatial.get("route_operating_layers") or []
            if str(item.get("route_id")) != route_id
        ]
        # Exactly one active assignment per route: setting it again replaces the previous one.
        spatial["route_operating_layers"] = [*others, assignment]
        return self._commit("route_operating_layer_changed")

    def delete_route_operating_layer(self, payload):
        route_id = str((payload or {}).get("route_id") or "").strip()
        if not route_id:
            raise ValueError("route_id 不能为空")
        spatial = self.session.state["spatial_3d"]
        assignments = spatial.get("route_operating_layers") or []
        remaining = [item for item in assignments if str(item.get("route_id")) != route_id]
        if len(remaining) == len(assignments):
            raise ValueError(f"该运行航路尚未配置巡航高度层：{route_id}")
        spatial["route_operating_layers"] = remaining
        return self._commit("route_operating_layer_deleted")

    # ---- departure / arrival procedures -------------------------------------------
    def set_procedure(self, payload):
        _require_traceable(payload)
        procedure = normalize_departure_arrival_procedure(payload)
        state = self.session.state
        if procedure["route_id"] not in set(_route_ids(state)):
            raise ValueError("procedure 对应的运行航路不存在")
        if procedure["altitude_layer_id"]:
            self._layer(procedure["altitude_layer_id"])
        if procedure["node_id"] and procedure["node_id"] not in _node_ids(state):
            raise ValueError(f"procedure 绑定的 node 不存在：{procedure['node_id']}")
        spatial = state["spatial_3d"]
        procedures = [
            item for item in spatial.get("departure_arrival_procedures") or []
            if item["procedure_id"] != procedure["procedure_id"]
        ]
        procedures.append(procedure)
        procedures.sort(key=lambda item: (item["procedure_type"], item["procedure_id"]))
        spatial["departure_arrival_procedures"] = procedures
        return self._commit("departure_arrival_procedure_changed")

    def delete_procedure(self, payload):
        procedure_id = str((payload or {}).get("procedure_id") or "").strip()
        if not procedure_id:
            raise ValueError("procedure_id 不能为空")
        spatial = self.session.state["spatial_3d"]
        procedures = spatial.get("departure_arrival_procedures") or []
        remaining = [item for item in procedures if item["procedure_id"] != procedure_id]
        if len(remaining) == len(procedures):
            raise ValueError(f"procedure 不存在：{procedure_id}")
        spatial["departure_arrival_procedures"] = remaining
        return self._commit("departure_arrival_procedure_deleted")

    # ---- internals ----------------------------------------------------------------
    def _layer(self, layer_id):
        if not layer_id:
            raise ValueError("altitude_layer_id 不能为空")
        layer = _layer_map(self.session.state["spatial_3d"]).get(layer_id)
        if layer is None:
            raise ValueError(f"AltitudeLayer 不存在：{layer_id}")
        return layer

    def _layer_references(self, layer_id):
        spatial = self.session.state["spatial_3d"]
        references = []
        for item in spatial.get("route_operating_layers") or []:
            if item.get("altitude_layer_id") == layer_id:
                references.append(f"运行航路 {item.get('route_id')} 的巡航高度层配置")
        for item in spatial.get("departure_arrival_procedures") or []:
            if item.get("altitude_layer_id") == layer_id:
                references.append(
                    f"{item.get('procedure_type')} procedure {item.get('procedure_id')}"
                )
        return references

    def _assert_layer_reference_consistency(self, layer):
        spatial = self.session.state["spatial_3d"]
        for item in spatial.get("route_operating_layers") or []:
            if item.get("altitude_layer_id") != layer["altitude_layer_id"]:
                continue
            if item.get("vertical_reference") != layer["vertical_reference"]:
                raise ValueError(
                    "该 AltitudeLayer 已被运行航路 "
                    f"{item.get('route_id')} 显式引用，vertical_reference 变更会造成垂向基准"
                    "不一致；请先重新显式分配该航路的巡航高度层"
                )

    def _commit(self, reason):
        resync_operating_layer_statuses(self.session.state)
        refresh_spatial_status(self.session.state)
        self.invalidation.route_operating_layer(reason)
        self.session.save()
        return self.snapshot()


def _require_traceable(payload):
    data = payload if isinstance(payload, dict) else {}
    if not bool(data.get("confirmed", False)):
        return
    source = str(data.get("source") or "").strip()
    if not source or source == UNRECORDED_SOURCE:
        raise ValueError("confirmed=true 时必须提供显式 source/evidence 来源（可追溯确认）")
