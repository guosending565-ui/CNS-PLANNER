"""Versioned engineering-assumption scenario contract (R5).

This module is deliberately **pure**: it validates and normalizes a scenario
declaration, but it never reads a file, writes ``ProjectState``, or changes any
formal planning rule.  The contract separates two things that must never be
mixed:

* **formal mode** — the existing production data path.  It only accepts
  candidates whose reality facts are explicitly confirmed;
* **engineering-assumption mode** — an isolated simulation where a uniform
  assumption (EPSG:4490 horizontal CRS, FABDEM EGM2008 ground, +15 m mount) is
  applied to candidate hosts.  Every derived object carries
  ``assumption_only=true`` / ``real_world_confirmed=false``.

The scenario object produced here is *not* a candidate site that may be written
back into ``ProjectState.candidate_sites``.  It is the input to the isolated
adapter in :mod:`cns_planner.application.engineering_assumption_scenario_service`.
"""

from __future__ import annotations

import json
from copy import deepcopy
from math import isfinite

SCENARIO_SCHEMA_VERSION = "cns-engineering-assumption-scenario.v1"
ENGINEERING_ASSUMPTION_MODE = "engineering_assumption"
FORMAL_MODE = "formal"
ASSUMED_HORIZONTAL_CRS = "EPSG:4490"
ASSUMED_HORIZONTAL_CRS_STATUS = "engineering_assumed"
DEFAULT_MOUNT_HEIGHT_AGL_M = 15.0
ASSUMED_SUBSYSTEMS = ("C", "N", "S")
CANDIDATE_REUSE_CLASS = "candidate_site"
TOWER_REUSE_CLASS = "tower_colocation_host"
NEW_BUILD_REUSE_CLASS = "new_build_candidate"

#: Every scenario result must disclose these unchanged facts.  They are the
#: difference between "the simulation is consistent" and "the real world has
#: been confirmed".
ASSUMPTION_DOES_NOT_CHANGE = (
    "source_crs_confirmation",
    "installation_origin_confirmation",
    "equipment_qualification",
    "site_permission_or_land_tenure",
    "power_and_backhaul_availability",
    "physical_mount_confirmation",
    "provider_independence",
    "line_of_sight_and_clutter",
    "continuous_service_field_measurement",
    "p16_formal_eligibility",
    "project_state_facts",
)

#: Marker keys used by the isolated adapter.  Formal import rejects these
#: markers; see :func:`formal_candidate_rejection_reason`.
ASSUMPTION_ONLY_KEY = "assumption_only"
REAL_WORLD_CONFIRMED_KEY = "real_world_confirmed"
SCENARIO_MODE_KEY = "scenario_mode"


class EngineeringAssumptionError(ValueError):
    """Raised when a scenario declaration is not a valid engineering assumption."""


def _finite_number(value, field, *, minimum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EngineeringAssumptionError(f"{field} 必须是有限数值")
    number = float(value)
    if not isfinite(number):
        raise EngineeringAssumptionError(f"{field} 必须是有限数值")
    if minimum is not None and number < minimum:
        raise EngineeringAssumptionError(f"{field} 不得小于 {minimum}")
    return number


def _coordinate(value, field="coordinate"):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise EngineeringAssumptionError(f"{field} 必须是 [lon, lat]")
    lon = _finite_number(value[0], f"{field}[0]")
    lat = _finite_number(value[1], f"{field}[1]")
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise EngineeringAssumptionError(f"{field} 超出经纬度范围")
    return [lon, lat]


def _subsystems(value, field, *, default=None):
    if value is None:
        value = list(default or ASSUMED_SUBSYSTEMS)
    if not isinstance(value, (list, tuple)):
        raise EngineeringAssumptionError(f"{field} 必须是数组")
    result = []
    for item in value:
        code = str(item or "").strip().upper()
        if code not in ASSUMED_SUBSYSTEMS:
            raise EngineeringAssumptionError(f"{field} 只允许 C/N/S，收到 {item!r}")
        if code not in result:
            result.append(code)
    if not result:
        raise EngineeringAssumptionError(f"{field} 不得为空")
    return result


def _require_mapping(value, field):
    if not isinstance(value, dict):
        raise EngineeringAssumptionError(f"{field} 必须是对象")
    return value


def _require_text(value, field):
    text = str(value or "").strip()
    if not text:
        raise EngineeringAssumptionError(f"{field} 不得为空")
    return text


def normalize_engineering_assumption_scenario(raw):
    """Validate and normalize one versioned scenario declaration.

    The declaration is intentionally strict: a missing/false assumption flag is
    a contract error, not a silent default.  That is what prevents a formal
    project from being re-labelled as a scenario by accident.
    """

    value = _require_mapping(raw, "scenario")
    schema_version = _require_text(value.get("schema_version"), "schema_version")
    if schema_version != SCENARIO_SCHEMA_VERSION:
        raise EngineeringAssumptionError(
            f"schema_version 必须是 {SCENARIO_SCHEMA_VERSION}，收到 {schema_version}"
        )
    mode = _require_text(value.get("mode"), "mode")
    if mode != ENGINEERING_ASSUMPTION_MODE:
        raise EngineeringAssumptionError(
            "本服务只接受 engineering_assumption 模式；formal 模式必须走正式数据链"
        )

    assumptions = _require_mapping(value.get("assumptions"), "assumptions")
    horizontal = _require_mapping(assumptions.get("horizontal_crs"), "assumptions.horizontal_crs")
    horizontal_value = _require_text(horizontal.get("value"), "assumptions.horizontal_crs.value")
    if horizontal_value != ASSUMED_HORIZONTAL_CRS:
        raise EngineeringAssumptionError(
            f"engineering_assumption 本轮只允许 {ASSUMED_HORIZONTAL_CRS} 水平工程假设"
        )
    if horizontal.get("confirmed") is True or horizontal.get("confirmed_crs"):
        raise EngineeringAssumptionError(
            "engineering_assumption 不得把水平 CRS 写成 confirmed；源文件仍是 source_undeclared"
        )
    if horizontal.get("status") not in (None, ASSUMED_HORIZONTAL_CRS_STATUS):
        raise EngineeringAssumptionError(
            "assumptions.horizontal_crs.status 必须是 engineering_assumed"
        )

    ground = _require_mapping(assumptions.get("ground_elevation"), "assumptions.ground_elevation")
    ground_source = _require_text(ground.get("source"), "assumptions.ground_elevation.source")
    vertical_reference = _require_text(
        ground.get("vertical_reference"), "assumptions.ground_elevation.vertical_reference"
    )
    if vertical_reference != "egm2008_orthometric":
        raise EngineeringAssumptionError(
            "assumptions.ground_elevation.vertical_reference 必须是 egm2008_orthometric"
        )
    ground_method = _require_text(
        ground.get("method") or "nearest_pixel", "assumptions.ground_elevation.method"
    )

    mount = _finite_number(
        assumptions.get("mount_height_agl_m", DEFAULT_MOUNT_HEIGHT_AGL_M),
        "assumptions.mount_height_agl_m",
        minimum=0.0,
    )
    allowed = _subsystems(assumptions.get("allowed_subsystems"), "assumptions.allowed_subsystems")

    equipment = _require_mapping(assumptions.get("equipment"), "assumptions.equipment")
    normalized_equipment = {}
    for code in ASSUMED_SUBSYSTEMS:
        device_id = equipment.get(code)
        if device_id in (None, ""):
            normalized_equipment[code] = None
            continue
        normalized_equipment[code] = _require_text(
            device_id, f"assumptions.equipment.{code}"
        )
    if normalized_equipment.get("S") is None:
        raise EngineeringAssumptionError(
            "assumptions.equipment.S 必须指向现有 RID 工程仿真型号（本轮为 2 km 模型）"
        )

    semantics = _require_mapping(assumptions.get("evidence_semantics"), "assumptions.evidence_semantics")
    if semantics.get(ASSUMPTION_ONLY_KEY) is not True:
        raise EngineeringAssumptionError("assumptions.evidence_semantics.assumption_only 必须为 true")
    if semantics.get(REAL_WORLD_CONFIRMED_KEY) is not False:
        raise EngineeringAssumptionError(
            "assumptions.evidence_semantics.real_world_confirmed 必须为 false"
        )

    explicit_sites = value.get("explicit_sites") or []
    if not isinstance(explicit_sites, list):
        raise EngineeringAssumptionError("explicit_sites 必须是数组")
    normalized_sites = []
    seen = set()
    for index, item in enumerate(explicit_sites):
        site = _require_mapping(item, f"explicit_sites[{index}]")
        site_id = _require_text(site.get("site_id"), f"explicit_sites[{index}].site_id")
        if not site_id.startswith("EA-"):
            raise EngineeringAssumptionError(
                f"显式场景站址 id 必须以 EA- 开头（收到 {site_id!r}）；不得用坐标自动生成绕过来源登记"
            )
        if site_id in seen:
            raise EngineeringAssumptionError(f"显式场景站址 id 重复：{site_id}")
        seen.add(site_id)
        coordinate = _coordinate(site.get("coordinate"), f"explicit_sites[{index}].coordinate")
        dem = _finite_number(site.get("dem_egm2008_m"), f"explicit_sites[{index}].dem_egm2008_m")
        site_mount = _finite_number(
            site.get("mount_height_agl_m", mount),
            f"explicit_sites[{index}].mount_height_agl_m",
            minimum=0.0,
        )
        origin = site.get("service_origin_egm2008_m")
        computed = round(dem + site_mount, 6)
        if origin is None:
            origin = computed
        else:
            origin = _finite_number(origin, f"explicit_sites[{index}].service_origin_egm2008_m")
            if abs(origin - computed) > 0.01:
                raise EngineeringAssumptionError(
                    f"{site_id} 的 service_origin_egm2008_m 与 dem+mount 不一致"
                )
        site_allowed = _subsystems(
            site.get("allowed_subsystems"), f"explicit_sites[{index}].allowed_subsystems",
            default=allowed,
        )
        device_ids = site.get("device_ids")
        if device_ids is None:
            device_ids = [
                normalized_equipment[code] for code in site_allowed
                if normalized_equipment.get(code)
            ]
        if not isinstance(device_ids, (list, tuple)):
            raise EngineeringAssumptionError(f"explicit_sites[{index}].device_ids 必须是数组")
        normalized_devices = []
        for device_id in device_ids:
            text = _require_text(device_id, f"explicit_sites[{index}].device_ids")
            if text not in {item for item in normalized_equipment.values() if item}:
                raise EngineeringAssumptionError(
                    f"{site_id} 引用了未在 assumptions.equipment 中登记的型号：{text}"
                )
            if text not in normalized_devices:
                normalized_devices.append(text)
        source = _require_mapping(site.get("source"), f"explicit_sites[{index}].source")
        normalized_sites.append({
            "site_id": site_id,
            "name": str(site.get("name") or site_id),
            "coordinate": coordinate,
            "dem_egm2008_m": dem,
            "mount_height_agl_m": site_mount,
            "service_origin_egm2008_m": origin,
            "allowed_subsystems": site_allowed,
            "device_ids": normalized_devices,
            "reuse_class": str(site.get("reuse_class") or NEW_BUILD_REUSE_CLASS),
            "route_scope": site.get("route_scope"),
            "source": deepcopy(source),
        })

    return {
        "schema_version": SCENARIO_SCHEMA_VERSION,
        "scenario_id": _require_text(value.get("scenario_id"), "scenario_id"),
        "version": _require_text(value.get("version"), "version"),
        "mode": ENGINEERING_ASSUMPTION_MODE,
        "route_id": str(value.get("route_id") or "").strip() or None,
        "description": str(value.get("description") or "").strip() or None,
        "assumptions": {
            "horizontal_crs": {
                "value": ASSUMED_HORIZONTAL_CRS,
                "status": ASSUMED_HORIZONTAL_CRS_STATUS,
                "confirmed": False,
                "declared_crs": None,
                "source_crs_status": "source_undeclared",
            },
            "ground_elevation": {
                "source": ground_source,
                "vertical_reference": vertical_reference,
                "method": ground_method,
            },
            "mount_height_agl_m": mount,
            "allowed_subsystems": allowed,
            "equipment": normalized_equipment,
            "evidence_semantics": {
                ASSUMPTION_ONLY_KEY: True,
                REAL_WORLD_CONFIRMED_KEY: False,
            },
        },
        "source_policy": deepcopy(value.get("source_policy") or {}),
        "explicit_sites": normalized_sites,
        "does_not_change": list(ASSUMPTION_DOES_NOT_CHANGE),
    }


def assumption_contract(scenario):
    """Compact, result-facing disclosure block."""

    scenario = _require_mapping(scenario, "scenario")
    assumptions = scenario.get("assumptions") or {}
    return {
        "scenario_id": scenario.get("scenario_id"),
        "version": scenario.get("version"),
        "mode": ENGINEERING_ASSUMPTION_MODE,
        "semantics": "simulation_engineering_assumption_not_real_world_confirmation",
        ASSUMPTION_ONLY_KEY: True,
        REAL_WORLD_CONFIRMED_KEY: False,
        "horizontal_crs": deepcopy(assumptions.get("horizontal_crs")),
        "ground_elevation": deepcopy(assumptions.get("ground_elevation")),
        "mount_height_agl_m": assumptions.get("mount_height_agl_m"),
        "allowed_subsystems": deepcopy(assumptions.get("allowed_subsystems")),
        "equipment": deepcopy(assumptions.get("equipment")),
        "does_not_change": list(scenario.get("does_not_change") or ASSUMPTION_DOES_NOT_CHANGE),
    }


def scenario_site_marker(scenario, *, source_kind, source_site_id, inherited_confirmed=False):
    """Uniform marker attached to every scenario site/action/result."""

    return {
        SCENARIO_MODE_KEY: ENGINEERING_ASSUMPTION_MODE,
        "scenario_id": scenario.get("scenario_id"),
        "scenario_version": scenario.get("version"),
        ASSUMPTION_ONLY_KEY: True,
        "real_world_confirmed": False,
        "source_kind": source_kind,
        "source_site_id": source_site_id,
        "inherited_confirmed_facts": bool(inherited_confirmed),
        "does_not_change": list(scenario.get("does_not_change") or ASSUMPTION_DOES_NOT_CHANGE),
    }


#: Marker keys that may appear on a scenario object, its projected candidates,
#: or on any metadata nested below them.  The formal write boundary rejects all
#: of them; this is deliberately a *recursive* contract because the same marker
#: can arrive inline, through JSON/CSV/GeoJSON, or inside ``metadata``.
FORMAL_IMPORT_MARKER_KEYS = (
    ASSUMPTION_ONLY_KEY,
    REAL_WORLD_CONFIRMED_KEY,
    SCENARIO_MODE_KEY,
    "projection_only",
    "scenario_site_id",
)

#: Provenance values that identify an isolated scenario object.  They are
#: rejected even when a caller removed the boolean markers: deleting a marker
#: must not turn an assumption into a real-world fact.
FORMAL_IMPORT_REJECTED_SOURCES = (
    "engineering_assumption",
    "engineering_assumption_scenario",
    "engineering_assumption_scenario_projection",
)

#: Private annotation written by the adapter so that a marker found on a raw
#: item survives ``normalize_candidate_site`` / ``normalize_existing_facility``
#: (which only keep their known fields).  It itself is a rejection marker.
FORMAL_IMPORT_AUTHORITY_KEY = "_formal_import_authority"


def _truthy(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


def _explicitly_false(value):
    if value is False:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"0", "false", "no", "n", "off"}
    return False


def _parse_embedded_json(value):
    text = value.strip()
    if not text.startswith(("{", "[")):
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def assumption_marker_paths(value, *, _path="", _depth=0):
    """Recursively return every engineering-assumption marker in ``value``.

    Each result is ``{"path": ..., "reason": ...}``.  The scan is intentionally
    wider than the three canonical metadata keys: a scenario record may place
    the marker at the item root (as ``research/r5/out/candidate_pool.json``
    does), inside nested metadata, or inside a JSON-encoded CSV cell.  All of
    those forms must be rejected at the same write boundary.
    """

    if _depth > 12:
        return []
    found = []

    def add(path, reason):
        found.append({"path": path, "reason": reason})

    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{_path}.{key}" if _path else str(key)
            lowered = str(key).lower()
            if key == ASSUMPTION_ONLY_KEY and _truthy(item):
                add(path, "assumption_only_true")
            elif key == REAL_WORLD_CONFIRMED_KEY and not _truthy(item):
                add(path, "real_world_confirmed_false_or_missing")
            elif key == SCENARIO_MODE_KEY and str(item or "").strip().lower() == ENGINEERING_ASSUMPTION_MODE:
                add(path, "scenario_mode_engineering_assumption")
            elif key == "projection_only" and _truthy(item):
                add(path, "projection_only_true")
            elif key == "scenario_site_id" and item not in (None, ""):
                add(path, "scenario_site_id_present")
            elif key == FORMAL_IMPORT_AUTHORITY_KEY:
                add(path, "formal_import_authority_annotation")
            elif key == "source" and str(item or "").strip().lower() in FORMAL_IMPORT_REJECTED_SOURCES:
                add(path, "engineering_assumption_source")
            elif key == "source_kind" and "assumption" in str(item or "").lower():
                add(path, "engineering_assumption_source_kind")
            elif key in ("scenario", "assumption", "assumption_contract") and isinstance(item, dict):
                if (
                    str(item.get("mode") or "").strip().lower() == ENGINEERING_ASSUMPTION_MODE
                    or str(item.get("scenario_mode") or "").strip().lower() == ENGINEERING_ASSUMPTION_MODE
                    or item.get(ASSUMPTION_ONLY_KEY) is True
                ):
                    add(path, "engineering_assumption_container")
            found.extend(assumption_marker_paths(item, _path=path, _depth=_depth + 1))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(
                assumption_marker_paths(item, _path=f"{_path}[{index}]", _depth=_depth + 1)
            )
    elif isinstance(value, str):
        parsed = _parse_embedded_json(value)
        if parsed is not None:
            found.extend(
                assumption_marker_paths(parsed, _path=f"{_path}(json)", _depth=_depth + 1)
            )

    # Stable de-duplication by path/reason; a marker may be reached twice when a
    # caller keeps both ``metadata`` and a root-level copy.
    unique = []
    seen = set()
    for item in found:
        identity = (item["path"], item["reason"])
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(item)
    return unique


def formal_candidate_rejection_reason(item):
    """Return a reason when an assumption-marked item tries to enter formal state.

    This is the write-boundary counterpart of the isolated adapter.  It does not
    change any P16 eligibility rule: it only stops a scenario projection from
    being imported as if it were a real confirmed candidate site.
    """

    markers = assumption_marker_paths(item)
    if not markers:
        return None
    first = markers[0]
    return (
        "engineering_assumption_candidate_cannot_be_imported_into_formal_state: "
        f"{first['reason']} at {first['path']}"
    )


def assert_no_assumption_marker(value, *, context="value"):
    """Fail closed when ``value`` carries any engineering-assumption marker."""

    markers = assumption_marker_paths(value)
    if markers:
        first = markers[0]
        raise EngineeringAssumptionError(
            f"{context}: engineering_assumption_candidate_cannot_be_imported_into_formal_state "
            f"({first['reason']} at {first['path']})"
        )


def assert_formal_candidates(items):
    """Validate a list of formal candidates; raise on first assumption marker."""

    for index, item in enumerate(items or []):
        reason = formal_candidate_rejection_reason(item)
        if reason:
            raise EngineeringAssumptionError(f"candidate_sites[{index}]: {reason}")


def authority_marker_carrier(value):
    """Return a compact marker block when ``value`` is assumption-marked.

    Used by ``candidates_from_existing``: an existing facility that carries a
    scenario marker must not lose it when it is converted into a candidate.
    """

    markers = assumption_marker_paths(value)
    if not markers:
        return None
    return {
        ASSUMPTION_ONLY_KEY: True,
        REAL_WORLD_CONFIRMED_KEY: False,
        "markers": markers[:20],
    }


def preserve_formal_authority(raw):
    """Deep-copy one raw input item and carry marker evidence into metadata.

    ``normalize_candidate_site`` and ``normalize_existing_facility`` only keep
    their known fields.  A root-level scenario marker would therefore disappear
    *before* the service could inspect the normalized item — exactly the R5 P0
    bypass.  This helper folds the marker evidence into ``metadata`` (which both
    normalizers preserve), so the unified check still sees it after adaptation.
    """

    if not isinstance(raw, dict):
        return raw
    markers = assumption_marker_paths(raw)
    if not markers:
        return deepcopy(raw)
    item = deepcopy(raw)
    metadata = item.get("metadata")
    metadata = deepcopy(metadata) if isinstance(metadata, dict) else {}
    metadata[FORMAL_IMPORT_AUTHORITY_KEY] = {
        ASSUMPTION_ONLY_KEY: True,
        REAL_WORLD_CONFIRMED_KEY: False,
        "markers": markers[:20],
    }
    item["metadata"] = metadata
    return item


__all__ = [
    "ASSUMED_HORIZONTAL_CRS",
    "ASSUMED_HORIZONTAL_CRS_STATUS",
    "ASSUMED_SUBSYSTEMS",
    "ASSUMPTION_DOES_NOT_CHANGE",
    "ASSUMPTION_ONLY_KEY",
    "CANDIDATE_REUSE_CLASS",
    "DEFAULT_MOUNT_HEIGHT_AGL_M",
    "ENGINEERING_ASSUMPTION_MODE",
    "EngineeringAssumptionError",
    "FORMAL_IMPORT_AUTHORITY_KEY",
    "FORMAL_IMPORT_MARKER_KEYS",
    "FORMAL_IMPORT_REJECTED_SOURCES",
    "FORMAL_MODE",
    "NEW_BUILD_REUSE_CLASS",
    "REAL_WORLD_CONFIRMED_KEY",
    "SCENARIO_MODE_KEY",
    "SCENARIO_SCHEMA_VERSION",
    "TOWER_REUSE_CLASS",
    "assumption_contract",
    "assumption_marker_paths",
    "assert_formal_candidates",
    "assert_no_assumption_marker",
    "authority_marker_carrier",
    "formal_candidate_rejection_reason",
    "normalize_engineering_assumption_scenario",
    "preserve_formal_authority",
    "scenario_site_marker",
]
