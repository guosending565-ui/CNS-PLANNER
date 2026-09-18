"""Additive JSON-safe contracts for vertical coordinates and lazy 3D references."""

from __future__ import annotations

from copy import deepcopy
from math import isfinite
from typing import Literal, TypedDict


VerticalReference = Literal[
    "agl", "egm2008_orthometric", "wgs84_ellipsoidal", "unknown",
]
VERTICAL_REFERENCES = {
    "agl", "egm2008_orthometric", "wgs84_ellipsoidal", "unknown",
}


class AltitudeLayer(TypedDict, total=False):
    altitude_layer_id: str
    name: str
    lower_altitude_m: float
    upper_altitude_m: float
    vertical_reference: VerticalReference
    source: str
    confirmed: bool
    status: str


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
        "route_altitude_profiles": {},
        "site_vertical_profiles": {},
    }


def normalize_spatial_3d(value):
    source = value if isinstance(value, dict) else {}
    layers = [normalize_altitude_layer(item) for item in source.get("altitude_layers") or []]
    profiles = source.get("route_altitude_profiles") or {}
    sites = source.get("site_vertical_profiles") or {}
    if not isinstance(profiles, dict) or not isinstance(sites, dict):
        raise ValueError("spatial_3d profile 必须是对象")
    route_profiles = {
        str(key): normalize_route_altitude_profile({**item, "route_id": str(key)})
        for key, item in profiles.items()
    }
    site_profiles = {str(key): normalize_vertical_profile(item) for key, item in sites.items()}
    configured = [*layers, *route_profiles.values(), *site_profiles.values()]
    return {
        "status": "passed" if configured and all(item.get("status") in ("passed", "confirmed") for item in configured) else "pending_confirmation",
        "canonical_vertical_reference": "egm2008_orthometric",
        "altitude_layers": layers,
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
    confirmed = bool(value.get("confirmed", False))
    return {
        "altitude_layer_id": layer_id,
        "name": str(value.get("name") or layer_id),
        "lower_altitude_m": lower,
        "upper_altitude_m": upper,
        "vertical_reference": reference,
        "source": str(value.get("source") or "未记录"),
        "confirmed": confirmed,
        "status": "confirmed" if confirmed and reference != "unknown" else "pending_confirmation",
        "geoid_undulation_m": _optional_number(value.get("geoid_undulation_m"), "geoid_undulation_m"),
    }


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


def _number(value, field):
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
