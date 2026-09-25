"""Single audited resolver for horizontal metric coordinate reference systems."""

from __future__ import annotations

from math import floor, isfinite
import re


_UTM = re.compile(r"^EPSG:(326|327)(\d{2})$", re.IGNORECASE)


def resolve_metric_crs(*, explicit_crs=None, geographic_bounds=None):
    """Resolve and validate a projected metric CRS for the supplied geographic extent.

    An explicit project CRS wins, but is accepted only when it is projected, uses metre
    units, and its declared area of use contains the extent centre.  Without an explicit
    CRS a WGS84 UTM zone is derived only for a finite, compact lon/lat extent wholly inside
    one UTM zone and the UTM latitude range.  Failure is returned as ``status=unknown``;
    no regional EPSG code is used as a fallback.
    """

    bounds = _bounds(geographic_bounds)
    if explicit_crs:
        authority = str(explicit_crs).strip()
        validation = _validate_explicit(authority, bounds)
        return {
            **validation,
            "authority": authority if validation["status"] == "resolved" else None,
            "source": "explicit_project_horizontal_metric_crs",
            "geographic_bounds": bounds,
        }
    if bounds is None:
        return _unknown("metric_crs_and_geographic_bounds_missing", bounds)
    west, south, east, north = bounds
    centre_lon, centre_lat = (west + east) / 2.0, (south + north) / 2.0
    if south < -80.0 or north > 84.0:
        return _unknown("extent_outside_utm_latitude_range", bounds)
    west_zone = _utm_zone(west)
    east_zone = _utm_zone(east if east < 180.0 else 179.999999999)
    if west_zone != east_zone or east - west > 6.0:
        return _unknown("extent_crosses_utm_zone_boundary", bounds)
    zone = _utm_zone(centre_lon)
    authority = f"EPSG:{326 if centre_lat >= 0.0 else 327}{zone:02d}"
    validation = _validate_explicit(authority, bounds)
    if validation["status"] != "resolved":
        return _unknown(validation.get("reason") or "derived_metric_crs_invalid", bounds)
    return {
        **validation,
        "authority": authority,
        "source": "derived_wgs84_utm_from_geographic_extent",
        "geographic_bounds": bounds,
    }


def require_metric_crs(*, explicit_crs=None, geographic_bounds=None):
    result = resolve_metric_crs(
        explicit_crs=explicit_crs, geographic_bounds=geographic_bounds,
    )
    if result["status"] != "resolved":
        raise ValueError(f"无法确定适用的水平米制 CRS：{result['reason']}")
    return result["authority"]


def _validate_explicit(authority, bounds):
    try:
        from pyproj import CRS

        crs = CRS.from_user_input(authority)
        if not crs.is_projected:
            return {"status": "unknown", "reason": "crs_is_not_projected"}
        units = {
            str(axis.unit_name or "").strip().lower() for axis in crs.axis_info
        }
        if not units or not units <= {"metre", "meter", "m"}:
            return {"status": "unknown", "reason": "projected_crs_unit_is_not_metre"}
        if bounds is not None and crs.area_of_use is not None:
            lon, lat = (bounds[0] + bounds[2]) / 2.0, (bounds[1] + bounds[3]) / 2.0
            area = crs.area_of_use
            if not (area.west <= lon <= area.east and area.south <= lat <= area.north):
                return {"status": "unknown", "reason": "crs_area_of_use_mismatch"}
        return {"status": "resolved", "reason": None, "unit": "metre"}
    except ImportError:
        match = _UTM.fullmatch(authority)
        if match is None:
            return {"status": "unknown", "reason": "crs_validator_unavailable"}
        zone = int(match.group(2))
        if not 1 <= zone <= 60:
            return {"status": "unknown", "reason": "utm_zone_invalid"}
        if bounds is not None:
            centre_lon = (bounds[0] + bounds[2]) / 2.0
            centre_lat = (bounds[1] + bounds[3]) / 2.0
            expected = 326 if centre_lat >= 0.0 else 327
            if int(match.group(1)) != expected or _utm_zone(centre_lon) != zone:
                return {"status": "unknown", "reason": "crs_area_of_use_mismatch"}
        return {
            "status": "resolved", "reason": None, "unit": "metre",
            "validation": "utm_authority_fallback_without_pyproj",
        }
    except (TypeError, ValueError, RuntimeError):
        return {"status": "unknown", "reason": "crs_invalid_or_unreadable"}


def _bounds(value):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        west, south, east, north = (float(item) for item in value)
    except (TypeError, ValueError):
        return None
    if not all(isfinite(item) for item in (west, south, east, north)):
        return None
    if west >= east or south >= north or west < -180 or east > 180 or south < -90 or north > 90:
        return None
    return [west, south, east, north]


def _utm_zone(longitude):
    return max(1, min(60, int(floor((float(longitude) + 180.0) / 6.0)) + 1))


def _unknown(reason, bounds):
    return {
        "status": "unknown", "authority": None, "reason": reason,
        "source": None, "unit": None, "geographic_bounds": bounds,
    }


__all__ = ["require_metric_crs", "resolve_metric_crs"]
