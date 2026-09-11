"""JSON-safe CNS planning input contracts and validation helpers."""

from __future__ import annotations

from copy import deepcopy
from math import isfinite
from typing import Any, TypedDict

from .cns_performance import (
    empty_subsystem_contract, normalize_subsystem_contract, sync_aliases,
)


SUBSYSTEMS = ("C", "N", "S")


class AircraftCNSProfile(TypedDict, total=False):
    aircraft_id: str
    name: str
    manufacturer: str
    model: str
    communication: dict[str, Any]
    navigation: dict[str, Any]
    surveillance: dict[str, Any]
    source: str
    metadata: dict[str, Any]


class CNSDevice(TypedDict, total=False):
    device_id: str
    name: str
    subsystem: str
    role: str
    radius_m: float
    latency_ms: float | None
    mtbf_h: float
    enabled: bool
    source: str
    parameter_metadata: dict[str, Any]


class RequiredCNS(TypedDict, total=False):
    status: str
    source: str
    project_default: dict[str, dict[str, Any]]
    route_overrides: dict[str, dict[str, dict[str, Any]]]
    metadata: dict[str, Any]


class ExistingCNSFacility(TypedDict, total=False):
    facility_id: str
    site_id: str
    name: str
    coordinate: list[float]
    elevation_m: float | None
    devices: list[dict[str, Any]]
    status: str
    source: str
    metadata: dict[str, Any]


class CandidateSite(TypedDict, total=False):
    site_id: str
    name: str
    coordinate: list[float]
    elevation_m: float | None
    site_type: str
    available_subsystems: list[str]
    usable: bool
    locked: bool
    source: str
    metadata: dict[str, Any]


def pending_required_cns() -> RequiredCNS:
    communication = {
        "status": "pending_confirmation", "required": None,
        "coverage_requirement": None, "max_gap_m": None,
        "latency_ms": None, "redundancy": None,
        **empty_subsystem_contract("C"),
    }
    navigation = {
        "status": "pending_confirmation", "required": None,
        "coverage_requirement": None, "accuracy_m": None,
        "integrity": None, "redundancy": None,
        **empty_subsystem_contract("N"),
    }
    surveillance = {
        "status": "pending_confirmation", "required": None,
        "coverage_requirement": None, "update_interval_s": None,
        "redundancy": None,
        **empty_subsystem_contract("S"),
    }
    return {
        "status": "pending_confirmation",
        "source": "项目默认值，待确认",
        "project_default": {
            "communication": communication,
            "navigation": navigation,
            "surveillance": surveillance,
        },
        "route_overrides": {},
        "metadata": {"semantics": "mission_requirement_not_aircraft_capability"},
    }


def normalize_required_cns(value: dict | None) -> RequiredCNS:
    result = pending_required_cns()
    if value is None:
        return result
    if not isinstance(value, dict):
        raise ValueError("RequiredCNS 必须是对象")
    result.update({key: deepcopy(current) for key, current in value.items() if key not in ("project_default", "route_overrides")})
    result["project_default"] = _normalize_requirement_set(value.get("project_default") or result["project_default"])
    overrides = value.get("route_overrides") or {}
    if not isinstance(overrides, dict):
        raise ValueError("RequiredCNS route_overrides 必须是对象")
    result["route_overrides"] = {str(route_id): _normalize_requirement_set(item) for route_id, item in overrides.items()}
    statuses = [item["status"] for item in result["project_default"].values()]
    result["status"] = "passed" if all(item == "passed" for item in statuses) else "pending_confirmation"
    return result


def normalize_aircraft_profile(item: dict) -> AircraftCNSProfile:
    if not isinstance(item, dict):
        raise ValueError("飞行器能力条目必须是对象")
    aircraft_id = _identifier(item.get("aircraft_id"), "aircraft_id")
    result: AircraftCNSProfile = {
        "aircraft_id": aircraft_id,
        "name": str(item.get("name") or item.get("model") or aircraft_id),
        "manufacturer": str(item.get("manufacturer") or ""),
        "model": str(item.get("model") or ""),
        "communication": _capability("C", item.get("communication"), "aircraft.communication"),
        "navigation": _capability("N", item.get("navigation"), "aircraft.navigation"),
        "surveillance": _capability("S", item.get("surveillance"), "aircraft.surveillance"),
        "source": str(item.get("source") or "未记录"),
        "metadata": deepcopy(item.get("metadata") or {}),
    }
    for key in ("cruise_speed_mps", "max_speed_mps", "mtbf_h"):
        if item.get(key) is not None:
            result[key] = _positive(item[key], key)
    return result


def normalize_device(item: dict) -> CNSDevice:
    if not isinstance(item, dict):
        raise ValueError("设备条目必须是对象")
    subsystem = str(item.get("subsystem") or "").upper()
    if subsystem not in SUBSYSTEMS:
        raise ValueError("设备 subsystem 必须是 C/N/S")
    role = str(item.get("role") or "")
    if role not in ("primary", "gap", "existing", "candidate"):
        raise ValueError("设备 role 无效")
    device_id = _identifier(item.get("device_id"), "device_id")
    result: CNSDevice = {
        "device_id": device_id, "name": str(item.get("name") or device_id),
        "subsystem": subsystem, "role": role,
        "radius_m": _positive(item.get("radius_m", item.get("coverage_radius_m")), "radius_m"),
        "latency_ms": _optional_nonnegative(item.get("latency_ms"), "latency_ms"),
        "mtbf_h": _positive(item.get("mtbf_h", item.get("mtbf")), "mtbf_h"),
        "enabled": bool(item.get("enabled", True)),
        "source": str(item.get("source") or "未记录"),
        "parameter_metadata": deepcopy(item.get("parameter_metadata") or {}),
    }
    for key in ("accuracy_m", "integrity", "update_interval_s", "redundancy"):
        if key in item:
            result[key] = deepcopy(item.get(key))
    contract = normalize_subsystem_contract(subsystem, item, field=f"device.{device_id}")
    result.update(contract)
    result = sync_aliases(subsystem, result, field=f"device.{device_id}")
    return result


def backfill_device_contract(item: dict) -> dict:
    """Add P3 fields to a persisted V1 device without tightening V1 validity."""
    if not isinstance(item, dict):
        raise ValueError("设备条目必须是对象")
    subsystem = str(item.get("subsystem") or "").upper()
    if subsystem not in SUBSYSTEMS:
        raise ValueError("设备 subsystem 必须是 C/N/S")
    result = deepcopy(item)
    result.update(normalize_subsystem_contract(subsystem, item, field=f"device.{item.get('device_id') or 'legacy'}"))
    return sync_aliases(subsystem, result, field=f"device.{item.get('device_id') or 'legacy'}")


def normalize_existing_facility(item: dict, index: int = 0) -> ExistingCNSFacility:
    if not isinstance(item, dict):
        raise ValueError("已有 CNS 设施条目必须是对象")
    facility_id = _identifier(item.get("facility_id") or item.get("site_id") or f"EXISTING-{index + 1:04d}", "facility_id")
    devices = item.get("devices")
    if devices is None and (item.get("subsystem") or item.get("device_id")):
        devices = [{key: item.get(key) for key in ("device_id", "subsystem", "name", "status") if item.get(key) is not None}]
    if not isinstance(devices or [], list):
        raise ValueError("已有 CNS 设施 devices 必须是数组")
    normalized_devices = []
    for device in devices or []:
        subsystem = str(device.get("subsystem") or "").upper()
        if subsystem not in SUBSYSTEMS:
            raise ValueError("已有设施设备 subsystem 必须是 C/N/S")
        normalized_devices.append({
            "device_id": str(device.get("device_id") or ""), "name": str(device.get("name") or ""),
            "subsystem": subsystem, "status": str(device.get("status") or "active"),
            "metadata": deepcopy(device.get("metadata") or {}),
        })
    return {
        "facility_id": facility_id, "site_id": str(item.get("site_id") or facility_id),
        "name": str(item.get("name") or facility_id), "coordinate": _coordinate(item),
        "elevation_m": _optional_number(item.get("elevation_m", item.get("elevation")), "elevation_m"),
        "devices": normalized_devices, "status": str(item.get("status") or "active"),
        "source": str(item.get("source") or "用户导入"), "metadata": deepcopy(item.get("metadata") or {}),
    }


def normalize_candidate_site(item: dict, index: int = 0) -> CandidateSite:
    if not isinstance(item, dict):
        raise ValueError("候选站址条目必须是对象")
    site_id = _identifier(item.get("site_id") or f"CANDIDATE-{index + 1:04d}", "site_id")
    subsystems = item.get("available_subsystems", item.get("subsystems", []))
    if isinstance(subsystems, str):
        subsystems = [part.strip().upper() for part in subsystems.replace(";", ",").split(",") if part.strip()]
    if not isinstance(subsystems, list) or any(str(value).upper() not in SUBSYSTEMS for value in subsystems):
        raise ValueError("候选站址 available_subsystems 必须是 C/N/S 数组")
    return {
        "site_id": site_id, "name": str(item.get("name") or site_id),
        "coordinate": _coordinate(item),
        "elevation_m": _optional_number(item.get("elevation_m", item.get("elevation")), "elevation_m"),
        "site_type": str(item.get("site_type") or "other"),
        "available_subsystems": list(dict.fromkeys(str(value).upper() for value in subsystems)),
        "usable": _boolean(item.get("usable", True)), "locked": _boolean(item.get("locked", False)),
        "source": str(item.get("source") or "用户导入"), "metadata": deepcopy(item.get("metadata") or {}),
    }


def _normalize_requirement_set(value):
    if not isinstance(value, dict):
        raise ValueError("CNS 需求集合必须是对象")
    template = pending_required_cns()["project_default"]
    result = {}
    subsystem_codes = {"communication": "C", "navigation": "N", "surveillance": "S"}
    for name, default in template.items():
        current = value.get(name) or {}
        if not isinstance(current, dict):
            raise ValueError(f"{name} CNS 需求必须是对象")
        merged = {**default, **deepcopy(current)}
        required = merged.get("required")
        if required not in (True, False, None):
            raise ValueError(f"{name}.required 必须为 true、false 或 null")
        for key in ("coverage_requirement", "max_gap_m"):
            if key in merged:
                merged[key] = _optional_nonnegative(merged.get(key), f"{name}.{key}")
        contract = normalize_subsystem_contract(subsystem_codes[name], current, field=f"required_cns.{name}")
        merged.update(contract)
        merged = sync_aliases(subsystem_codes[name], merged, field=f"required_cns.{name}")
        merged["status"] = "passed" if required is False or (required is True and _requirements_complete(name, merged)) else "pending_confirmation"
        result[name] = merged
    return result


def _requirements_complete(name, value):
    keys = {
        "communication": ("coverage_requirement", "max_gap_m", "latency_ms", "redundancy"),
        "navigation": ("coverage_requirement", "accuracy_m", "integrity", "redundancy"),
        "surveillance": ("coverage_requirement", "update_interval_s", "redundancy"),
    }[name]
    return all(value.get(key) is not None and value.get(key) != "" for key in keys)


def _capability(subsystem, value, field):
    if value is None:
        value = {}
    if isinstance(value, list):
        value = {"status": "confirmed", "capabilities": deepcopy(value), "confirmed": True}
    if not isinstance(value, dict):
        raise ValueError("CNS capability 必须是对象或数组")
    result = {
        "status": str(value.get("status") or "pending_confirmation"),
        "capabilities": deepcopy(value.get("capabilities") or []),
    }
    result.update(normalize_subsystem_contract(subsystem, value, field=field))
    result = sync_aliases(subsystem, result, field=field)
    return result


def _coordinate(item):
    value = item.get("coordinate")
    if value is None and item.get("longitude") is not None and item.get("latitude") is not None:
        value = [item["longitude"], item["latitude"]]
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        raise ValueError("坐标必须包含 longitude、latitude")
    lon, lat = float(value[0]), float(value[1])
    if not all(isfinite(number) for number in (lon, lat)) or not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise ValueError("坐标超出 WGS84 范围")
    return [lon, lat]


def _identifier(value, field):
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{field} 不能为空")
    return result


def _positive(value, field):
    number = float(value)
    if not isfinite(number) or number <= 0:
        raise ValueError(f"{field} 必须大于零")
    return number


def _optional_nonnegative(value, field):
    if value in (None, ""):
        return None
    number = float(value)
    if not isfinite(number) or number < 0:
        raise ValueError(f"{field} 不得小于零")
    return number


def _optional_number(value, field):
    if value in (None, ""):
        return None
    number = float(value)
    if not isfinite(number):
        raise ValueError(f"{field} 必须是有限数值")
    return number


def _boolean(value):
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "否", "")
    return bool(value)
