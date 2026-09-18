"""Additive JSON-safe contracts for vertical coordinates and lazy 3D references.

Layered Operational Route Architecture V1 lives here as well: a production route is
``DepartureProcedure -> fixed cruise AltitudeLayer + horizontal route -> ArrivalProcedure``.
One concrete route carries exactly one cruise altitude layer; vertical transitions belong to
the terminal procedures and never enter horizontal route planning.  The contracts stay
additive: ``RouteAltitudeProfile`` (including the advanced/V3-D waypoint profile) keeps its
existing semantics and is never converted into a fixed cruise layer.
"""

from __future__ import annotations

from copy import deepcopy
from json import dumps, loads
from math import isfinite
from typing import Literal, TypedDict


VerticalReference = Literal[
    "agl", "egm2008_orthometric", "wgs84_ellipsoidal", "unknown",
]
VERTICAL_REFERENCES = {
    "agl", "egm2008_orthometric", "wgs84_ellipsoidal", "unknown",
}

#: The only operating mode implemented by Layered Operational Route Architecture V1.
ROUTE_OPERATING_MODES = {"fixed_cruise_layer"}
PROCEDURE_TYPES = {"departure", "arrival"}
TRANSITION_MODES = {
    "climb_to_cruise_layer", "descend_from_cruise_layer", "level_transition",
    "not_specified",
}
JOIN_LEAVE_KINDS = {"join", "leave"}
#: The placeholder this code base already uses for "no recorded source/evidence".
UNRECORDED_SOURCE = "未记录"


class AltitudeLayer(TypedDict, total=False):
    altitude_layer_id: str
    name: str
    #: Explicitly declared nominal cruise altitude.  ``None`` means "not yet confirmed by
    #: engineering": it is never derived from the bounds, never defaulted, and it keeps the
    #: layer ``pending_confirmation``.
    nominal_altitude_m: float | None
    lower_altitude_m: float
    upper_altitude_m: float
    vertical_reference: VerticalReference
    source: str
    evidence: dict
    confirmed: bool
    status: str


class RouteOperatingLayer(TypedDict, total=False):
    """One active cruise-altitude-layer assignment for one route (JSON-safe)."""

    route_id: str
    altitude_layer_id: str
    operating_mode: str
    vertical_reference: VerticalReference
    source: str
    evidence: dict
    confirmed: bool
    status: str
    active: bool


class DepartureArrivalProcedure(TypedDict, total=False):
    """Terminal transition contract (departure/arrival) for one route.

    V1 implements the contract, readiness and CRUD only: no procedure path optimizer, and no
    default climb/descent rate, turn radius or join/leave point is ever invented.  Missing
    evidence keeps the procedure ``pending_confirmation``.
    """

    procedure_id: str
    procedure_type: str
    route_id: str
    node_id: str | None
    site_reference: str | None
    altitude_layer_id: str | None
    transition_mode: str
    horizontal_geometry: dict
    vertical_profile: dict
    join_leave_point: dict | None
    source: str
    evidence: dict
    confirmed: bool
    status: str
    missing_evidence: list[str]


class VoxelRef(TypedDict):
    voxel_id: str
    grid_id: str
    altitude_layer_id: str


class RouteAltitudeProfile(TypedDict, total=False):
    route_id: str
    mode: str
    vertical_reference: VerticalReference
    constant_altitude_m: float | None
    waypoints: list[dict]
    source: str
    confirmed: bool
    status: str
    # ---- additive V3-D provenance (never required, never inferred) ----------------
    #: True when the profile was derived from a V3-C validated route rather than typed
    #: in by a user.  A derived profile is locked against manual altitude edits until its
    #: operational adoption is revoked.
    derived: bool
    locked: bool
    locked_by_adoption: bool
    adoption_owned: bool
    distance_basis: str
    profile_derivation: str
    profile_semantics: str
    vertex_order_semantics: str
    v3_metric_length_m: float | None
    legacy_geodesic_length_m: float | None
    length_delta_m: float | None
    curve_chord_error_m: float | None


class Route3DSample(TypedDict, total=False):
    distance_along_route_m: float
    longitude: float
    latitude: float
    grid_id: str | None
    surface_elevation_m: float | None
    altitude_agl_m: float | None
    altitude_egm2008_m: float | None
    vertical_status: str


def empty_spatial_3d():
    return {
        "status": "pending_confirmation",
        "canonical_vertical_reference": "egm2008_orthometric",
        "altitude_layers": [],
        "route_operating_layers": [],
        "departure_arrival_procedures": [],
        "route_altitude_profiles": {},
        "site_vertical_profiles": {},
    }


def normalize_spatial_3d(value):
    source = value if isinstance(value, dict) else {}
    layers = [normalize_altitude_layer(item) for item in source.get("altitude_layers") or []]
    assignments = [
        normalize_route_operating_layer(item)
        for item in source.get("route_operating_layers") or []
    ]
    _assert_single_active_assignment(assignments)
    procedures = [
        normalize_departure_arrival_procedure(item)
        for item in source.get("departure_arrival_procedures") or []
    ]
    profiles = source.get("route_altitude_profiles") or {}
    sites = source.get("site_vertical_profiles") or {}
    if not isinstance(profiles, dict) or not isinstance(sites, dict):
        raise ValueError("spatial_3d profile 必须是对象")
    route_profiles = {
        str(key): normalize_route_altitude_profile({**item, "route_id": str(key)})
        for key, item in profiles.items()
    }
    site_profiles = {str(key): normalize_vertical_profile(item) for key, item in sites.items()}
    configured = [
        *layers, *assignments, *procedures,
        *route_profiles.values(), *site_profiles.values(),
    ]
    return {
        "status": "passed" if configured and all(item.get("status") in ("passed", "confirmed") for item in configured) else "pending_confirmation",
        "canonical_vertical_reference": "egm2008_orthometric",
        "altitude_layers": layers,
        "route_operating_layers": assignments,
        "departure_arrival_procedures": procedures,
        "route_altitude_profiles": route_profiles,
        "site_vertical_profiles": site_profiles,
    }


def normalize_altitude_layer(value):
    if not isinstance(value, dict):
        raise ValueError("高度层必须是对象")
    layer_id = str(value.get("altitude_layer_id") or "").strip()
    if not layer_id:
        raise ValueError("altitude_layer_id 不能为空")
    reference = _reference(value.get("vertical_reference"))
    lower = _number(value.get("lower_altitude_m"), "lower_altitude_m")
    upper = _number(value.get("upper_altitude_m"), "upper_altitude_m")
    if upper < lower:
        raise ValueError("高度层上界不得低于下界")
    nominal = _optional_number(value.get("nominal_altitude_m"), "nominal_altitude_m")
    if nominal is not None and not lower <= nominal <= upper:
        raise ValueError("nominal_altitude_m 必须满足 lower_altitude_m <= nominal <= upper_altitude_m")
    source = _source(value.get("source"))
    confirmed = bool(value.get("confirmed", False))
    # A layer without an explicit nominal altitude, without a recorded source or with an
    # unknown vertical datum stays pending.  The midpoint is never substituted, and no
    # vertical datum is ever guessed.
    resolvable = nominal is not None and reference != "unknown" and source != UNRECORDED_SOURCE
    return {
        "altitude_layer_id": layer_id,
        "name": str(value.get("name") or layer_id),
        "nominal_altitude_m": nominal,
        "lower_altitude_m": lower,
        "upper_altitude_m": upper,
        "vertical_reference": reference,
        "source": source,
        "evidence": _json_object(value.get("evidence"), "evidence"),
        "confirmed": confirmed,
        "status": "confirmed" if confirmed and resolvable else "pending_confirmation",
        "geoid_undulation_m": _optional_number(value.get("geoid_undulation_m"), "geoid_undulation_m"),
    }


def normalize_route_operating_layer(value):
    """Normalize one route → cruise AltitudeLayer assignment.

    Existence of both the route and the referenced layer is enforced by the application
    service; this contract only fixes the JSON-safe shape and the confirmation semantics.
    """

    if not isinstance(value, dict):
        raise ValueError("route operating layer 必须是对象")
    route_id = str(value.get("route_id") or "").strip()
    if not route_id:
        raise ValueError("route_id 不能为空")
    layer_id = str(value.get("altitude_layer_id") or "").strip()
    if not layer_id:
        raise ValueError("altitude_layer_id 不能为空")
    mode = str(value.get("operating_mode") or "fixed_cruise_layer")
    if mode not in ROUTE_OPERATING_MODES:
        raise ValueError(f"不支持的 operating_mode：{mode}")
    reference = _reference(value.get("vertical_reference"))
    source = _source(value.get("source"))
    confirmed = bool(value.get("confirmed", False))
    resolvable = reference != "unknown" and source != UNRECORDED_SOURCE
    return {
        "route_id": route_id,
        "altitude_layer_id": layer_id,
        "operating_mode": mode,
        "vertical_reference": reference,
        "source": source,
        "evidence": _json_object(value.get("evidence"), "evidence"),
        "confirmed": confirmed,
        "status": "confirmed" if confirmed and resolvable else "pending_confirmation",
        "active": bool(value.get("active", True)),
    }


def normalize_departure_arrival_procedure(value):
    if not isinstance(value, dict):
        raise ValueError("procedure 必须是对象")
    procedure_id = str(value.get("procedure_id") or "").strip()
    if not procedure_id:
        raise ValueError("procedure_id 不能为空")
    procedure_type = str(value.get("procedure_type") or "").strip()
    if procedure_type not in PROCEDURE_TYPES:
        raise ValueError("procedure_type 只能是 departure 或 arrival")
    route_id = str(value.get("route_id") or "").strip()
    if not route_id:
        raise ValueError("procedure 必须绑定 route_id")
    transition_mode = str(value.get("transition_mode") or "not_specified")
    if transition_mode not in TRANSITION_MODES:
        raise ValueError(f"不支持的 transition_mode：{transition_mode}")
    point = _optional_json_object(value.get("join_leave_point"), "join_leave_point")
    if isinstance(point, dict):
        kind = point.get("kind")
        if kind not in (None, "") and str(kind) not in JOIN_LEAVE_KINDS:
            raise ValueError("join_leave_point.kind 只能是 join 或 leave")
    source = _source(value.get("source"))
    confirmed = bool(value.get("confirmed", False))
    result = {
        "procedure_id": procedure_id,
        "procedure_type": procedure_type,
        "route_id": route_id,
        "node_id": _optional_text(value.get("node_id")),
        "site_reference": _optional_text(value.get("site_reference")),
        "altitude_layer_id": _optional_text(value.get("altitude_layer_id")),
        "transition_mode": transition_mode,
        "horizontal_geometry": _json_object(value.get("horizontal_geometry"), "horizontal_geometry"),
        "vertical_profile": _json_object(value.get("vertical_profile"), "vertical_profile"),
        "join_leave_point": point,
        "source": source,
        "evidence": _json_object(value.get("evidence"), "evidence"),
        "confirmed": confirmed,
        "missing_evidence": [],
        "status": "pending_confirmation",
    }
    result["missing_evidence"] = procedure_missing_evidence(result)
    result["status"] = (
        "confirmed"
        if confirmed and source != UNRECORDED_SOURCE and not result["missing_evidence"]
        else "pending_confirmation"
    )
    return result


def procedure_missing_evidence(procedure):
    """Explicit evidence a procedure still needs.  Nothing is ever defaulted in."""

    if not isinstance(procedure, dict):
        raise ValueError("procedure 必须是对象")
    kind = str(procedure.get("procedure_type") or "")
    missing = []
    if not str(procedure.get("altitude_layer_id") or "").strip():
        missing.append("altitude_layer_id")
    if not str(procedure.get("node_id") or "").strip() and not str(procedure.get("site_reference") or "").strip():
        missing.append("node_or_site_reference")
    if str(procedure.get("transition_mode") or "not_specified") == "not_specified":
        missing.append("transition_mode")
    vertical = procedure.get("vertical_profile") if isinstance(procedure.get("vertical_profile"), dict) else {}
    geometry = procedure.get("horizontal_geometry") if isinstance(procedure.get("horizontal_geometry"), dict) else {}
    if kind == "departure":
        if _optional_positive(vertical.get("climb_rate_mps")) is None:
            missing.append("climb_rate_mps")
    elif kind == "arrival":
        if _optional_positive(vertical.get("descent_rate_mps")) is None:
            missing.append("descent_rate_mps")
    if _optional_positive(geometry.get("turn_radius_m")) is None:
        missing.append("turn_radius_m")
    expected = "join" if kind == "departure" else "leave"
    point = procedure.get("join_leave_point")
    observed = str((point or {}).get("kind") or "") if isinstance(point, dict) else ""
    if observed != expected:
        missing.append("join_point" if expected == "join" else "leave_point")
    return missing


def voxel_ref(grid_id, altitude_layer_id):
    grid_id, layer_id = str(grid_id or "").strip(), str(altitude_layer_id or "").strip()
    if not grid_id or not layer_id:
        raise ValueError("VoxelRef 需要 grid_id 和 altitude_layer_id")
    return {"voxel_id": f"{grid_id}@{layer_id}", "grid_id": grid_id, "altitude_layer_id": layer_id}


def normalize_route_altitude_profile(value):
    if not isinstance(value, dict):
        raise ValueError("航路高度剖面必须是对象")
    route_id = str(value.get("route_id") or "").strip()
    if not route_id:
        raise ValueError("route_id 不能为空")
    mode = str(value.get("mode") or "constant")
    if mode not in ("constant", "waypoint_linear"):
        raise ValueError("高度剖面 mode 仅支持 constant/waypoint_linear")
    reference = _reference(value.get("vertical_reference"))
    constant = _optional_number(value.get("constant_altitude_m"), "constant_altitude_m")
    raw_waypoints = value.get("waypoints") or []
    if not isinstance(raw_waypoints, list):
        raise ValueError("waypoints 必须是数组")
    waypoints = []
    for item in raw_waypoints:
        if not isinstance(item, dict):
            raise ValueError("waypoint 必须是对象")
        waypoints.append({
            "distance_along_route_m": _nonnegative(item.get("distance_along_route_m"), "distance_along_route_m"),
            "altitude_m": _number(item.get("altitude_m"), "altitude_m"),
        })
    waypoints.sort(key=lambda item: item["distance_along_route_m"])
    valid = constant is not None if mode == "constant" else len(waypoints) >= 2
    confirmed = bool(value.get("confirmed", False))
    # V3-D provenance is purely additive: it is preserved when present and never
    # invented for a profile a user typed in.
    derived = bool(value.get("derived", False))
    result = {
        "route_id": route_id, "mode": mode, "vertical_reference": reference,
        "constant_altitude_m": constant, "waypoints": waypoints,
        "source": str(value.get("source") or "未记录"), "confirmed": confirmed,
        "status": "confirmed" if valid and confirmed and reference != "unknown" else "pending_confirmation",
        "geoid_undulation_m": _optional_number(value.get("geoid_undulation_m"), "geoid_undulation_m"),
        "derived": derived,
        "locked": bool(value.get("locked", False)) and derived,
        "locked_by_adoption": bool(value.get("locked_by_adoption", False)) and derived,
        "adoption_owned": bool(value.get("adoption_owned", False)) and derived,
        "requested_by_user": bool(value.get("requested_by_user", False)),
        "distance_basis": _optional_text(value.get("distance_basis")),
        "profile_derivation": _optional_text(value.get("profile_derivation")),
        "profile_semantics": _optional_text(value.get("profile_semantics")),
        "vertex_order_semantics": _optional_text(value.get("vertex_order_semantics")),
        "v3_metric_length_m": _optional_number(value.get("v3_metric_length_m"), "v3_metric_length_m"),
        "legacy_geodesic_length_m": _optional_number(
            value.get("legacy_geodesic_length_m"), "legacy_geodesic_length_m",
        ),
        "length_delta_m": _optional_number(value.get("length_delta_m"), "length_delta_m"),
        "curve_chord_error_m": _optional_number(
            value.get("curve_chord_error_m"), "curve_chord_error_m",
        ),
    }
    return result


def normalize_vertical_profile(value=None, legacy_elevation_m=None):
    raw = value if isinstance(value, dict) else {}
    reference = _reference(raw.get("surface_vertical_reference", raw.get("vertical_reference")))
    result = {
        "surface_elevation_m": _optional_number(raw.get("surface_elevation_m"), "surface_elevation_m"),
        "surface_vertical_reference": reference,
        "mount_height_agl_m": _optional_nonnegative(raw.get("mount_height_agl_m"), "mount_height_agl_m"),
        "service_origin_egm2008_m": _optional_number(raw.get("service_origin_egm2008_m"), "service_origin_egm2008_m"),
        "source": str(raw.get("source") or "未记录"),
        "confirmed": bool(raw.get("confirmed", False)),
        "status": str(raw.get("status") or "pending_confirmation"),
        "legacy_elevation_m": _optional_number(
            raw.get("legacy_elevation_m") if legacy_elevation_m in (None, "") else legacy_elevation_m,
            "legacy_elevation_m",
        ),
    }
    if result["service_origin_egm2008_m"] is None and reference == "egm2008_orthometric" and result["surface_elevation_m"] is not None and result["mount_height_agl_m"] is not None:
        result["service_origin_egm2008_m"] = result["surface_elevation_m"] + result["mount_height_agl_m"]
    if result["confirmed"] and result["service_origin_egm2008_m"] is not None:
        result["status"] = "confirmed"
    return result


def resolve_egm2008_height(height_m, reference, *, surface_elevation_m=None, geoid_undulation_m=None):
    height = _optional_number(height_m, "height_m")
    ref = _reference(reference)
    if height is None:
        return {"status": "missing_data", "altitude_egm2008_m": None, "reason": "未提供高度"}
    if ref == "egm2008_orthometric":
        return {"status": "passed", "altitude_egm2008_m": height, "reason": "已是 EGM2008 正高"}
    if ref == "agl":
        surface = _optional_number(surface_elevation_m, "surface_elevation_m")
        if surface is None:
            return {"status": "missing_data", "altitude_egm2008_m": None, "reason": "AGL 转换缺少 DEM surface_elevation_m"}
        return {"status": "passed", "altitude_egm2008_m": surface + height, "reason": "AGL + EGM2008 surface"}
    if ref == "wgs84_ellipsoidal":
        undulation = _optional_number(geoid_undulation_m, "geoid_undulation_m")
        if undulation is None:
            return {"status": "unresolved", "altitude_egm2008_m": None, "reason": "缺少 WGS84 ellipsoid 到 EGM2008 大地水准面转换"}
        return {"status": "passed", "altitude_egm2008_m": height - undulation, "reason": "ellipsoidal - geoid undulation"}
    return {"status": "unresolved", "altitude_egm2008_m": None, "reason": "vertical reference unknown"}


def _reference(value):
    reference = str(value or "unknown")
    if reference not in VERTICAL_REFERENCES:
        raise ValueError(f"不支持的垂向基准：{reference}")
    return reference


def _source(value):
    text = str(value or "").strip()
    return text or UNRECORDED_SOURCE


def _json_object(value, field):
    """Return a JSON-safe deep copy of ``value`` (empty object when absent)."""

    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field} 必须是对象")
    try:
        return loads(dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是 JSON-safe 对象") from exc


def _optional_json_object(value, field):
    if value in (None, ""):
        return None
    return _json_object(value, field)


def _assert_single_active_assignment(assignments):
    active = [item["route_id"] for item in assignments if item.get("active", True)]
    if len(active) != len(set(active)):
        raise ValueError("同一 route 最多只能有一个 active 巡航高度层配置")


def _optional_positive(value):
    if value in (None, ""):
        return None
    number = _number(value, "procedure evidence")
    return number if number > 0 else None


def _number(value, field):
    if value in (None, ""):
        raise ValueError(f"{field} 必须是有限数值")
    number = float(value)
    if not isfinite(number):
        raise ValueError(f"{field} 必须是有限数值")
    return number


def _nonnegative(value, field):
    number = _number(value, field)
    if number < 0:
        raise ValueError(f"{field} 不得小于零")
    return number


def _optional_number(value, field):
    return None if value in (None, "") else _number(value, field)


def _optional_text(value):
    if value in (None, ""):
        return None
    return str(value)


def _optional_nonnegative(value, field):
    return None if value in (None, "") else _nonnegative(value, field)
