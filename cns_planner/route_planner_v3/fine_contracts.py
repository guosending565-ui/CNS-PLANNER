"""JSON-safe Route Planner V3-B contracts: corridor-local metric fine refinement.

V3-B is the stage that *follows* a V3-A strategic candidate.  It never runs on an
arbitrary route: it consumes one **selected and current** ``strategic_candidate``
plus that candidate's ``CandidateRefinementCorridor``, and it refines the search
inside a **corridor-local metric fine grid**.

Design rules encoded here:

* ``LocalMetricPlanningFrame`` makes the horizontal metric frame explicit: local
  CRS, origin, axis convention, resolution and how a local index maps back to
  geographic coordinates.  A fine cell is never identified by an assumed CRS;
* ``FineGridSpec`` records the horizontal resolution **and where it came from**
  (``explicit_configuration`` or the DTM's own effective resolution).  A literal
  ``"30 m"`` constant is forbidden: it would silently claim a safety clearance
  the data never provided;
* ``FineCellEnvironment`` carries only hard facts (terrain floor / building
  required clearance / confirmed airspace) plus the coarse-upsampled soft fields.
  Everything is fail-closed: ``unknown``/NoData is never "clear";
* a coarse value mapped onto finer cells always carries
  ``upsampled_without_new_information=true`` -- V3-B never claims finer *source*
  accuracy than the source actually has;
* results may only ever be ``refined_candidate`` / ``failed`` / ``not_ready`` /
  ``missing_data`` / ``search_incomplete``, and force
  ``final_validation_performed=false`` / ``operational_route=false``: the exact
  polygon / terrain / continuous-clearance validation is **V3-C**.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json

from .contracts import (
    AIRSPACE_STATUSES, ENVIRONMENT_DATA_STATUSES, SEARCH_COMPLETENESS,
    default_v3_policy, normalize_aircraft_motion_limits, normalize_cost_model,
    normalize_v3_planning_policy,
)
from .contracts import normalize_candidate_refinement_corridor

FINE_MODEL_SCOPE = "corridor_local_3d_refinement_v3b"
FINE_ALGORITHM_ID = "route_planner_v3_corridor_refinement"
FINE_ALGORITHM_VERSION = "3.1-alpha"

LOCAL_FRAME_SCHEMA_VERSION = "3.1-local-metric-frame"
FINE_GRID_SPEC_SCHEMA_VERSION = "3.1-fine-grid-spec"
FINE_ENVIRONMENT_SCHEMA_VERSION = "3.1-fine-environment"
REFINEMENT_PROBLEM_SCHEMA_VERSION = "3.1-refinement-problem"
REFINEMENT_RESULT_SCHEMA_VERSION = "3.1-refinement-result"
FINE_MOTION_PRIMITIVE_SCHEMA_VERSION = "3.1-fine-motion-primitive"
FINE_REFINEMENT_POLICY_SCHEMA_VERSION = "3.1-fine-refinement-policy"

V3B_DISCLAIMER = (
    "V3-B refined candidate：corridor-local 米制细网格工程精化结果，"
    "不是 final safe / validated operational route；"
    "尚未执行 V3-C exact polygon / terrain / continuous clearance 验证，"
    "也未进入 V3-D operational adapter 与 CNS 评估。"
)

#: The only statuses a V3-B result may carry.
V3B_RESULT_STATUSES = (
    "refined_candidate", "failed", "not_ready", "missing_data", "search_incomplete",
)

#: Where a fine horizontal resolution may come from.  There is no default value.
FINE_RESOLUTION_SOURCES = ("explicit_configuration", "dtm_effective_resolution")

#: Hard-fact semantics.  These strings are contract constants so no consumer can
#: re-label a conservative envelope as an exact validation.
TERRAIN_SAMPLING_METHOD = "intersecting_valid_fabdem_pixels_max_egm2008_plus_explicit_terrain_clearance"
TERRAIN_FORBIDDEN_METHODS = ("center_sample", "average", "mean", "bilinear_resample")
BUILDING_MAPPING_METHOD = "footprint_buffered_by_explicit_horizontal_clearance_to_fine_cell_envelope"
BUILDING_FORBIDDEN_METHODS = ("center_inside_polygon_only", "exact_polygon_clearance")
AIRSPACE_MAPPING_METHOD = "confirmed_airspace_policy_only_never_inferred_from_layer_name_or_color"
SOFT_FIELD_MAPPING_METHOD = "coarse_cell_index_upsampled_to_fine_cells"
REFINEMENT_TURN_MODEL = (
    "engineering_arc_length_proxy_min_turn_radius_times_heading_change_"
    "not_exact_curvature_v3c_will_validate_continuously"
)
V3C_PENDING = (
    "exact_polygon_membership",
    "exact_terrain_profile_clearance",
    "continuous_clearance_along_the_full_trajectory",
    "monotone_turn_curvature_verification",
)

#: Refinement fingerprint components; a change in any of them makes a stored
#: refinement stale.
REFINEMENT_FINGERPRINT_COMPONENTS = (
    "strategic_fingerprint", "corridor_fingerprint", "policy_fingerprint",
    "source_fingerprint", "frame_fingerprint", "fine_grid_fingerprint",
)


# --------------------------------------------------------------------------- helpers


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and float(value) == float(value)


def _optional_number(value, field, *, nonnegative=False, positive=False):
    if value in (None, ""):
        return None
    if not _finite(value):
        raise ValueError(f"{field} 必须是有限数值")
    number = float(value)
    if nonnegative and number < 0:
        raise ValueError(f"{field} 不得小于零")
    if positive and number <= 0:
        raise ValueError(f"{field} 必须大于零")
    return number


def _optional_int(value, field, *, minimum=None):
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) != int(value):
        raise ValueError(f"{field} 必须是整数")
    number = int(value)
    if minimum is not None and number < minimum:
        raise ValueError(f"{field} 不得小于 {minimum}")
    return number


def _text(value, default=""):
    return str(value if value is not None else default)


def _optional_pair(value, field):
    if value in (None, ""):
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{field} 必须是 [x, y]")
    numbers = [float(item) for item in value]
    if not all(_finite(item) for item in numbers):
        raise ValueError(f"{field} 必须是有限数值")
    return numbers


def _optional_bbox(value, field):
    if value in (None, ""):
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError(f"{field} 必须是 4 项 bbox")
    numbers = [float(item) for item in value]
    if not all(_finite(item) for item in numbers):
        raise ValueError(f"{field} 必须是有限数值")
    return numbers


def contract_fingerprint(value, *, prefix=""):
    return prefix + sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


# --------------------------------------------------------------------------- policy


def default_v3_fine_refinement_policy():
    """Explicit V3-B fine-refinement configuration.  No resolution default exists.

    The default is the *normalization* of an empty input, so a project round-trip
    (save -> load -> normalize) is byte-stable: there is no state where the stored
    default differs from the normalized one.
    """

    return normalize_v3_fine_refinement_policy(None)


def _evaluate_fine_policy(result):
    """Reasons / missing parameters / status of one fine-refinement policy."""

    if result["resolution_source"] not in (None,) + FINE_RESOLUTION_SOURCES:
        raise ValueError(
            "resolution_source 必须是 explicit_configuration 或 dtm_effective_resolution"
        )
    reasons = []
    if result["horizontal_crs"] is None:
        reasons.append("未配置 local metric CRS：fine grid 的米制帧不可追溯")
    if result["resolution_source"] is None:
        reasons.append(
            "未配置 resolution_source：不得把 30 m 当作安全常数；"
            "必须显式选择 explicit_configuration 或 dtm_effective_resolution"
        )
    if result["resolution_source"] == "explicit_configuration" and result["resolution_m"] is None:
        reasons.append("resolution_source=explicit_configuration 时必须给出 resolution_m")
    blocking = list(reasons)
    if not result["confirmed"]:
        reasons.append("fine refinement policy 未经项目工程依据确认（confirmed=false）")
    result["reasons"] = reasons
    result["missing_parameters"] = sorted({
        "horizontal_crs" if result["horizontal_crs"] is None else "",
        "resolution_source" if result["resolution_source"] is None else "",
        "resolution_m" if (
            result["resolution_source"] == "explicit_configuration" and result["resolution_m"] is None
        ) else "",
    } - {""})
    result["status"] = (
        "confirmed" if result["confirmed"] and not blocking
        else "blocked" if blocking
        else "pending_confirmation"
    )
    return result


def normalize_v3_fine_refinement_policy(value=None):
    source = value if isinstance(value, dict) else {}
    result = {
        "schema_version": FINE_REFINEMENT_POLICY_SCHEMA_VERSION,
        "horizontal_crs": None,
        "resolution_m": None,
        "resolution_source": None,
        "max_stride_cells": 1,
        "source": "未配置；fine resolution 与 local metric CRS 必须显式给出，禁止写死 30 m",
        "confirmed": False,
        "status": "pending_confirmation",
        "missing_parameters": [],
        "reasons": [],
    }
    result.update({
        "horizontal_crs": None if source.get("horizontal_crs") in (None, "") else _text(source.get("horizontal_crs")),
        "resolution_m": _optional_number(source.get("resolution_m"), "resolution_m", positive=True),
        "resolution_source": (
            None if source.get("resolution_source") in (None, "")
            else _text(source.get("resolution_source"))
        ),
        "max_stride_cells": _optional_int(source.get("max_stride_cells"), "max_stride_cells", minimum=1) or 1,
        "source": _text(source.get("source") or result["source"]),
        "confirmed": bool(source.get("confirmed", False)),
    })
    return _evaluate_fine_policy(result)


# --------------------------------------------------------------------------- frame


def empty_local_metric_frame():
    return {
        "schema_version": LOCAL_FRAME_SCHEMA_VERSION,
        "frame_id": None,
        "horizontal_crs": None,
        "horizontal_crs_source": None,
        "vertical_reference": "egm2008_orthometric",
        "origin_metric": None,
        "axis": {"column_axis": "east", "row_axis": "north", "index_origin": "south_west_corner"},
        "resolution_m": None,
        "metric_bounds": None,
        "local_to_geographic": {
            "method": None,
            "authority": None,
            "display_only": True,
            "interpolated_from_parent_cells": False,
            "note": "局部 index → 经纬度 的映射方式必须显式记录；算法层不做投影换算",
        },
        "provenance": {},
    }


def normalize_local_metric_frame(value=None):
    source = value if isinstance(value, dict) else {}
    result = empty_local_metric_frame()
    local = source.get("local_to_geographic") if isinstance(source.get("local_to_geographic"), dict) else {}
    axis = source.get("axis") if isinstance(source.get("axis"), dict) else {}
    result.update({
        "frame_id": None if source.get("frame_id") in (None, "") else _text(source.get("frame_id")),
        "horizontal_crs": None if source.get("horizontal_crs") in (None, "") else _text(source.get("horizontal_crs")),
        "horizontal_crs_source": None if source.get("horizontal_crs_source") in (None, "") else _text(source.get("horizontal_crs_source")),
        "origin_metric": _optional_pair(source.get("origin_metric"), "origin_metric"),
        "resolution_m": _optional_number(source.get("resolution_m"), "resolution_m", positive=True),
        "metric_bounds": _optional_bbox(source.get("metric_bounds"), "metric_bounds"),
        "axis": {
            "column_axis": _text(axis.get("column_axis") or "east"),
            "row_axis": _text(axis.get("row_axis") or "north"),
            "index_origin": _text(axis.get("index_origin") or "south_west_corner"),
        },
        "local_to_geographic": {
            "method": None if local.get("method") in (None, "") else _text(local.get("method")),
            "authority": None if local.get("authority") in (None, "") else _text(local.get("authority")),
            "display_only": bool(local.get("display_only", True)),
            "interpolated_from_parent_cells": bool(local.get("interpolated_from_parent_cells", False)),
            "note": _text(local.get("note") or result["local_to_geographic"]["note"]),
        },
        "provenance": deepcopy(source.get("provenance") or {}),
    })
    reference = _text(source.get("vertical_reference") or "egm2008_orthometric")
    if reference != "egm2008_orthometric":
        raise ValueError("V3-B 只接受 canonical vertical reference egm2008_orthometric")
    result["vertical_reference"] = reference
    return result


def frame_fingerprint(frame):
    return contract_fingerprint(frame, prefix="V3BFRAME-")


# --------------------------------------------------------------------------- fine grid


def empty_fine_grid_spec():
    return {
        "schema_version": FINE_GRID_SPEC_SCHEMA_VERSION,
        "spec_id": None,
        "resolution_m": None,
        "requested_resolution_m": None,
        "resolution_source": None,
        "effective_source_resolution_m": None,
        "nx": 0,
        "ny": 0,
        "cell_count": 0,
        "local_frame": empty_local_metric_frame(),
        "parent_grid_ids": [],
        "corridor_id": None,
        "corridor_ring_n": 0,
        "mapping_method": "corridor_support_cells_to_local_metric_index_grid",
        "grid_index": "row_major_from_south_west_origin",
        "cell_id_format": "F{level}-{row:04d}-{column:04d}",
        "parent_grid_resolution_m": None,
        "not_a_safety_clearance": True,
        "semantics": "corridor_local_fine_search_grid_not_a_safety_volume",
        "provenance": {},
    }


def normalize_fine_grid_spec(value=None):
    source = value if isinstance(value, dict) else {}
    result = empty_fine_grid_spec()
    resolution = _optional_number(source.get("resolution_m"), "resolution_m", positive=True)
    resolution_source = None if source.get("resolution_source") in (None, "") else _text(source.get("resolution_source"))
    if resolution is None and (source.get("nx") or source.get("ny") or source.get("cell_count")):
        raise ValueError(
            "FineGridSpec 声明了 cell 但没有 resolution_m："
            "不允许使用任何默认水平分辨率（例如写死的 30 m）"
        )
    if resolution is not None and resolution_source not in FINE_RESOLUTION_SOURCES:
        raise ValueError(
            "FineGridSpec.resolution_source 必须是 " + " / ".join(FINE_RESOLUTION_SOURCES)
            + "（水平分辨率必须有可追溯来源，不得写死为安全常数）"
        )
    result.update({
        "spec_id": None if source.get("spec_id") in (None, "") else _text(source.get("spec_id")),
        "resolution_m": resolution,
        "requested_resolution_m": _optional_number(
            source.get("requested_resolution_m"), "requested_resolution_m", positive=True,
        ),
        "resolution_source": resolution_source,
        "effective_source_resolution_m": _optional_number(
            source.get("effective_source_resolution_m"), "effective_source_resolution_m", positive=True,
        ),
        "nx": _optional_int(source.get("nx"), "nx", minimum=0) or 0,
        "ny": _optional_int(source.get("ny"), "ny", minimum=0) or 0,
        "cell_count": _optional_int(source.get("cell_count"), "cell_count", minimum=0) or 0,
        "local_frame": normalize_local_metric_frame(source.get("local_frame")),
        "parent_grid_ids": [_text(item) for item in source.get("parent_grid_ids") or []],
        "corridor_id": None if source.get("corridor_id") in (None, "") else _text(source.get("corridor_id")),
        "corridor_ring_n": _optional_int(source.get("corridor_ring_n"), "corridor_ring_n", minimum=0) or 0,
        "parent_grid_resolution_m": _optional_number(
            source.get("parent_grid_resolution_m"), "parent_grid_resolution_m", positive=True,
        ),
        "provenance": deepcopy(source.get("provenance") or {}),
    })
    if result["nx"] and result["ny"]:
        expected = result["nx"] * result["ny"]
        if result["cell_count"] and result["cell_count"] != expected:
            raise ValueError("FineGridSpec.cell_count 与 nx * ny 不一致")
        result["cell_count"] = expected
    if resolution is not None and result["requested_resolution_m"] is not None:
        result["resolution_deviation_m"] = round(
            result["requested_resolution_m"] - resolution, 9,
        )
    else:
        result["resolution_deviation_m"] = None
    result["not_a_safety_clearance"] = True
    result["semantics"] = result["semantics"]
    return result


def fine_grid_fingerprint(spec):
    return contract_fingerprint(spec, prefix="V3BGRID-")


# --------------------------------------------------------------------------- environment


def empty_fine_cell_environment(status="missing_data", reason="没有可用的 corridor-local fine environment"):
    return {
        "schema_version": FINE_ENVIRONMENT_SCHEMA_VERSION,
        "status": status,
        "reason": reason,
        "canonical_vertical_reference": "egm2008_orthometric",
        "source_type": None,
        "spec": empty_fine_grid_spec(),
        "source_audit": {},
        "properties": {
            "resolution_m": None,
            "cell_size_m": None,
            "terrain_clearance_m": None,
            "building_horizontal_clearance_m": None,
            "building_vertical_clearance_m": None,
            "soft_field_sources": {},
        },
        "cells": [],
    }


def normalize_fine_cell_environment(value=None):
    if not isinstance(value, dict):
        return empty_fine_cell_environment()
    reference = _text(value.get("canonical_vertical_reference") or "egm2008_orthometric")
    if reference != "egm2008_orthometric":
        raise ValueError("V3-B fine environment 只接受 egm2008_orthometric")
    cells = [_normalize_fine_cell(raw) for raw in value.get("cells") or []]
    return {
        "schema_version": FINE_ENVIRONMENT_SCHEMA_VERSION,
        "status": _text(value.get("status") or ("passed" if cells else "missing_data")),
        "reason": value.get("reason"),
        "canonical_vertical_reference": reference,
        "source_type": _text(value.get("source_type") or "unspecified"),
        "spec": normalize_fine_grid_spec(value.get("spec")) if value.get("spec") else empty_fine_grid_spec(),
        "source_audit": deepcopy(value.get("source_audit") or {}),
        "properties": deepcopy(value.get("properties") or empty_fine_cell_environment()["properties"]),
        "cells": cells,
    }


def _normalize_fine_cell(raw):
    if not isinstance(raw, dict):
        raise ValueError("FineCellEnvironment cell 必须是对象")
    fine_cell_id = _text(raw.get("fine_cell_id")).strip()
    if not fine_cell_id:
        raise ValueError("FineCellEnvironment cell 缺少 fine_cell_id")
    terrain = raw.get("terrain") if isinstance(raw.get("terrain"), dict) else {}
    buildings = raw.get("buildings") if isinstance(raw.get("buildings"), dict) else {}
    airspace = raw.get("airspace") if isinstance(raw.get("airspace"), dict) else {}
    terrain_status = _text(terrain.get("data_status") or "unknown")
    building_status = _text(buildings.get("data_status") or "unknown")
    airspace_status = _text(airspace.get("status") or "unknown")
    if terrain_status not in ENVIRONMENT_DATA_STATUSES:
        raise ValueError(f"fine cell {fine_cell_id} terrain.data_status 无效：{terrain_status}")
    if building_status not in ENVIRONMENT_DATA_STATUSES:
        raise ValueError(f"fine cell {fine_cell_id} buildings.data_status 无效：{building_status}")
    if airspace_status not in AIRSPACE_STATUSES:
        raise ValueError(f"fine cell {fine_cell_id} airspace.status 无效：{airspace_status}")
    sampling = terrain.get("sampling")
    if sampling is not None and str(sampling) in TERRAIN_FORBIDDEN_METHODS:
        raise ValueError(
            f"fine cell {fine_cell_id} terrain.sampling={sampling} 被禁止："
            "hard floor 必须是相交有效像元的最大高程，不得使用 center/average/bilinear"
        )
    mapping = buildings.get("mapping_method")
    if mapping is not None and str(mapping) in BUILDING_FORBIDDEN_METHODS:
        raise ValueError(
            f"fine cell {fine_cell_id} buildings.mapping_method={mapping} 被禁止："
            "V3-B 只做 conservative envelope，不做 exact polygon validation"
        )
    return {
        "fine_cell_id": fine_cell_id,
        "row": _optional_int(raw.get("row"), "row"),
        "column": _optional_int(raw.get("column"), "column"),
        "center_metric": _optional_pair(raw.get("center_metric"), "center_metric"),
        "center": _optional_pair(raw.get("center"), "center"),
        "bbox_metric": _optional_bbox(raw.get("bbox_metric"), "bbox_metric"),
        "cell_size_m": _optional_number(raw.get("cell_size_m"), "cell_size_m", positive=True),
        "parent_grid_id": None if raw.get("parent_grid_id") in (None, "") else _text(raw.get("parent_grid_id")),
        "parent_binding": _text(raw.get("parent_binding") or "nearest_parent_cell_center"),
        "terrain": {
            "data_status": terrain_status,
            "surface_elevation_max_egm2008_m": _optional_number(
                terrain.get("surface_elevation_max_egm2008_m"), "surface_elevation_max_egm2008_m",
            ),
            "surface_clearance_egm2008_m": _optional_number(
                terrain.get("surface_clearance_egm2008_m"), "surface_clearance_egm2008_m",
            ),
            "valid_pixel_count": _optional_int(
                terrain.get("valid_pixel_count"), "terrain.valid_pixel_count", minimum=0,
            ),
            "nodata_pixel_count": _optional_int(
                terrain.get("nodata_pixel_count"), "terrain.nodata_pixel_count", minimum=0,
            ),
            "sampling": None if sampling is None else _text(sampling),
            "reason": terrain.get("reason"),
        },
        "buildings": {
            "data_status": building_status,
            "roof_elevation_max_egm2008_m": _optional_number(
                buildings.get("roof_elevation_max_egm2008_m"), "roof_elevation_max_egm2008_m",
            ),
            "required_clearance_egm2008_m": _optional_number(
                buildings.get("required_clearance_egm2008_m"), "required_clearance_egm2008_m",
            ),
            "ground_elevation_max_egm2008_m": _optional_number(
                buildings.get("ground_elevation_max_egm2008_m"), "ground_elevation_max_egm2008_m",
            ),
            "horizontal_clearance_m": _optional_number(
                buildings.get("horizontal_clearance_m"), "horizontal_clearance_m", nonnegative=True,
            ),
            "building_ids": [_text(item) for item in buildings.get("building_ids") or []],
            "mapping_method": None if mapping is None else _text(mapping),
            "reason": buildings.get("reason"),
        },
        "airspace": {
            "status": airspace_status,
            "feature_id": None if airspace.get("feature_id") in (None, "") else _text(airspace.get("feature_id")),
            "mapping_method": _text(airspace.get("mapping_method") or AIRSPACE_MAPPING_METHOD),
            "parent_grid_id": None if airspace.get("parent_grid_id") in (None, "") else _text(airspace.get("parent_grid_id")),
            "policy_confirmed": bool(airspace.get("policy_confirmed", False)),
        },
        "soft_fields": deepcopy(raw.get("soft_fields") or {}),
    }


# --------------------------------------------------------------------------- problem


def empty_v3_refinement_problem():
    return {
        "schema_version": REFINEMENT_PROBLEM_SCHEMA_VERSION,
        "problem_id": "v3b-problem",
        "route_id": None,
        "strategic_candidate": {
            "status": None,
            "experiment_id": None,
            "strategic_fingerprint": None,
            "current_applicability": "unknown",
            "corridor_id": None,
            # Endpoint binding evidence.  The fine grid is metric while the
            # strategic candidate is geographic, so the binding must be explicit
            # (or resolved from these recorded points) and its method recorded.
            "start_point": None,
            "goal_point": None,
            "start_metric": None,
            "goal_metric": None,
            "start_fine_cell_id": None,
            "goal_fine_cell_id": None,
            "start_altitude_egm2008_m": None,
            "goal_altitude_egm2008_m": None,
            "explicit_goal_altitude": False,
            "state_count": 0,
        },
        "corridor": None,
        "policy": default_v3_policy(),
        "aircraft_motion_limits": normalize_aircraft_motion_limits(None),
        "frame": empty_local_metric_frame(),
        "fine_grid": empty_fine_grid_spec(),
        "environment": empty_fine_cell_environment(),
        "max_stride_cells": 1,
        "heading_bin_count": 8,
        "soft_cost_weights": {},
        "source_audit": {},
        "expected_refinement_fingerprint": None,
        "refinement_fingerprint": None,
        "provenance": {},
    }


def normalize_v3_refinement_problem(value=None):
    source = value if isinstance(value, dict) else {}
    policy = normalize_v3_planning_policy(source.get("policy"))
    corridor = (
        normalize_candidate_refinement_corridor(source.get("corridor"))
        if source.get("corridor") else None
    )
    fine_grid = normalize_fine_grid_spec(source.get("fine_grid"))
    frame = normalize_local_metric_frame(source.get("frame") or fine_grid.get("local_frame"))
    if fine_grid.get("resolution_m") is not None and frame.get("resolution_m") is None:
        frame["resolution_m"] = fine_grid["resolution_m"]
    environment = normalize_fine_cell_environment(source.get("environment"))
    strategic = source.get("strategic_candidate") if isinstance(source.get("strategic_candidate"), dict) else {}
    weights = {}
    for name, raw in (source.get("soft_cost_weights") or {}).items():
        weights[_text(name)] = _optional_number(raw, f"soft_cost_weights.{name}", nonnegative=True)
    result = empty_v3_refinement_problem()
    result.update({
        "problem_id": _text(source.get("problem_id") or "v3b-problem"),
        "route_id": None if source.get("route_id") in (None, "") else _text(source.get("route_id")),
        "strategic_candidate": {
            "status": None if strategic.get("status") in (None, "") else _text(strategic.get("status")),
            "experiment_id": None if strategic.get("experiment_id") in (None, "") else _text(strategic.get("experiment_id")),
            "strategic_fingerprint": (
                None if strategic.get("strategic_fingerprint") in (None, "")
                else _text(strategic.get("strategic_fingerprint"))
            ),
            "current_applicability": _text(strategic.get("current_applicability") or "unknown"),
            "corridor_id": None if strategic.get("corridor_id") in (None, "") else _text(strategic.get("corridor_id")),
            "start_point": _optional_pair(strategic.get("start_point"), "strategic_candidate.start_point"),
            "goal_point": _optional_pair(strategic.get("goal_point"), "strategic_candidate.goal_point"),
            "start_metric": _optional_pair(strategic.get("start_metric"), "strategic_candidate.start_metric"),
            "goal_metric": _optional_pair(strategic.get("goal_metric"), "strategic_candidate.goal_metric"),
            "start_fine_cell_id": (
                None if strategic.get("start_fine_cell_id") in (None, "")
                else _text(strategic.get("start_fine_cell_id"))
            ),
            "goal_fine_cell_id": (
                None if strategic.get("goal_fine_cell_id") in (None, "")
                else _text(strategic.get("goal_fine_cell_id"))
            ),
            "start_altitude_egm2008_m": _optional_number(
                strategic.get("start_altitude_egm2008_m"), "start_altitude_egm2008_m",
            ),
            "goal_altitude_egm2008_m": _optional_number(
                strategic.get("goal_altitude_egm2008_m"), "goal_altitude_egm2008_m",
            ),
            "explicit_goal_altitude": bool(strategic.get("explicit_goal_altitude", False)),
            "state_count": _optional_int(strategic.get("state_count"), "state_count", minimum=0) or 0,
            "status_counts": deepcopy(strategic.get("status_counts") or {}),
        },
        "corridor": corridor,
        "policy": policy,
        "aircraft_motion_limits": normalize_aircraft_motion_limits(source.get("aircraft_motion_limits")),
        "frame": frame,
        "fine_grid": fine_grid,
        "environment": environment,
        "max_stride_cells": _optional_int(source.get("max_stride_cells"), "max_stride_cells", minimum=1) or 1,
        "heading_bin_count": _optional_int(source.get("heading_bin_count"), "heading_bin_count", minimum=4)
        or int(policy.get("heading_bin_count") or 8),
        "soft_cost_weights": weights,
        "source_audit": deepcopy(source.get("source_audit") or environment.get("source_audit") or {}),
        "provenance": deepcopy(source.get("provenance") or {}),
    })
    if result["heading_bin_count"] % 1 or 360 % result["heading_bin_count"] != 0:
        raise ValueError("heading_bin_count 必须整除 360")
    result["expected_refinement_fingerprint"] = (
        None if source.get("expected_refinement_fingerprint") in (None, "")
        else _text(source.get("expected_refinement_fingerprint"))
    )
    result["refinement_fingerprint"] = refinement_fingerprint(result)
    return result


def refinement_fingerprint_components(problem):
    """The evidence a stored refinement depends on.

    A change in **any** component makes the stored refinement stale:
    the strategic candidate, its corridor, the planning policy, the source
    audits (terrain/buildings/airspace version evidence) and the local frame /
    fine grid that were actually used.
    """

    grid = problem.get("fine_grid") or {}
    return {
        "strategic_fingerprint": (problem.get("strategic_candidate") or {}).get("strategic_fingerprint"),
        "corridor_fingerprint": contract_fingerprint(
            problem.get("corridor") or {}, prefix="V3BCORR-",
        ) if problem.get("corridor") else None,
        "policy_fingerprint": contract_fingerprint(
            problem.get("policy") or {}, prefix="V3BPOL-",
        ),
        "source_fingerprint": (problem.get("source_audit") or {}).get("fingerprint")
        or contract_fingerprint(problem.get("source_audit") or {}, prefix="V3BSRC-"),
        "frame_fingerprint": frame_fingerprint(problem.get("frame") or {}),
        "fine_grid_fingerprint": fine_grid_fingerprint(grid),
    }


def refinement_fingerprint(problem):
    components = refinement_fingerprint_components(problem)
    return contract_fingerprint(components, prefix="V3BREF-")


def evaluate_refinement_applicability(recorded, current):
    """Compare a stored refinement's components against current evidence.

    ``recorded`` / ``current`` are component mappings as produced by
    :func:`refinement_fingerprint_components`.  Anything that changed is reported
    by name; the verdict is ``current`` only when every component matches.
    """

    recorded = recorded if isinstance(recorded, dict) else {}
    current = current if isinstance(current, dict) else {}
    changed = [
        name for name in REFINEMENT_FINGERPRINT_COMPONENTS
        if recorded.get(name) != current.get(name)
    ]
    missing = [name for name in REFINEMENT_FINGERPRINT_COMPONENTS if current.get(name) is None]
    status = "current" if not changed else "stale"
    return {
        "status": status,
        "changed_components": changed,
        "missing_current_components": missing,
        "reasons": (
            [] if status == "current"
            else [
                "refinement 依赖的证据已变化（source/corridor/policy/strategic/frame/grid）："
                + ", ".join(changed)
            ]
        ),
        "semantics": "stale_when_strategic_corridor_policy_or_source_audit_changes",
        "recorded_fingerprint": contract_fingerprint(recorded, prefix="V3BREF-") if recorded else None,
        "current_fingerprint": contract_fingerprint(current, prefix="V3BREF-") if current else None,
    }


# --------------------------------------------------------------------------- result


def empty_v3_refinement_result(status="not_ready"):
    return {
        "schema_version": REFINEMENT_RESULT_SCHEMA_VERSION,
        "status": status if status in V3B_RESULT_STATUSES else "not_ready",
        "algorithm_id": FINE_ALGORITHM_ID,
        "algorithm_version": FINE_ALGORITHM_VERSION,
        "model_scope": FINE_MODEL_SCOPE,
        "problem_id": None,
        "route_id": None,
        "reason": None,
        "readiness": None,
        "refinement_fingerprint": None,
        "fingerprint_components": {},
        "strategic_fingerprint": None,
        "corridor_id": None,
        "frame": None,
        "fine_grid": None,
        "source_audit": {},
        "effective_policy": None,
        "motion_model": {},
        "state_path": [],
        "horizontal_projection": [],
        "metric_projection": [],
        "distance_m": None,
        "cost_vector": None,
        "search_statistics": {
            "expanded_states": 0, "generated_states": 0, "expanded_transitions": 0,
            "runtime_ms": 0.0, "expansion_cap": None, "expansion_cap_reached": False,
            "search_complete": False,
            "search_completeness": SEARCH_COMPLETENESS["not_run"],
            "resource_limited": False, "resource_limit": None, "resource_limit_reason": None,
            "optimality_proven": False,
        },
        "hard_constraint_summary": {
            "state_rejections": {}, "transition_rejections": {},
            "traversed_cell_rejections": {},
            "total_state_rejections": 0, "total_transition_rejections": 0,
            "total_traversed_cell_rejections": 0,
        },
        "fine_grid_evidence": {
            "resolution_m": None,
            "resolution_source": None,
            "cell_count": 0,
            "environment_cell_count": 0,
            "corridor_support_cell_count": 0,
        },
        "final_validation_performed": False,
        "operational_route": False,
        "v3c_validation_pending": True,
        "disclaimer": V3B_DISCLAIMER,
        "semantics": {
            "scope": FINE_MODEL_SCOPE,
            "not_final_safe": True,
            "not_validated_operational_route": True,
            "exact_validation_is_v3c": True,
            "unknown_is_never_safe": True,
            "terrain_floor_is_intersecting_pixel_max": True,
            "building_envelope_is_conservative_not_exact": True,
            "airspace_consumes_confirmed_policy_only": True,
            "coarse_soft_fields_are_upsampled_without_new_information": True,
            "turn_model": REFINEMENT_TURN_MODEL,
            "v3c_pending": list(V3C_PENDING),
        },
    }


def normalize_v3_refinement_result(value=None):
    result = empty_v3_refinement_result()
    if not isinstance(value, dict):
        return result
    result.update(deepcopy(value))
    status = _text(value.get("status") or "not_ready")
    result["status"] = status if status in V3B_RESULT_STATUSES else "not_ready"
    result["schema_version"] = REFINEMENT_RESULT_SCHEMA_VERSION
    result["algorithm_id"] = FINE_ALGORITHM_ID
    result["algorithm_version"] = FINE_ALGORITHM_VERSION
    result["model_scope"] = FINE_MODEL_SCOPE
    result["state_path"] = deepcopy(value.get("state_path") or [])
    result["horizontal_projection"] = deepcopy(value.get("horizontal_projection") or [])
    result["metric_projection"] = deepcopy(value.get("metric_projection") or [])
    result["disclaimer"] = V3B_DISCLAIMER
    result["final_validation_performed"] = False
    result["operational_route"] = False
    result["v3c_validation_pending"] = True
    result["frame"] = normalize_local_metric_frame(value.get("frame")) if value.get("frame") else None
    result["fine_grid"] = normalize_fine_grid_spec(value.get("fine_grid")) if value.get("fine_grid") else None
    result.setdefault("fingerprint_components", {})
    return result


__all__ = [
    "AIRSPACE_MAPPING_METHOD", "BUILDING_FORBIDDEN_METHODS", "BUILDING_MAPPING_METHOD",
    "FINE_ALGORITHM_ID", "FINE_ALGORITHM_VERSION", "FINE_ENVIRONMENT_SCHEMA_VERSION",
    "FINE_GRID_SPEC_SCHEMA_VERSION", "FINE_MODEL_SCOPE", "FINE_MOTION_PRIMITIVE_SCHEMA_VERSION",
    "FINE_REFINEMENT_POLICY_SCHEMA_VERSION", "FINE_RESOLUTION_SOURCES",
    "LOCAL_FRAME_SCHEMA_VERSION",
    "REFINEMENT_FINGERPRINT_COMPONENTS", "REFINEMENT_PROBLEM_SCHEMA_VERSION",
    "REFINEMENT_RESULT_SCHEMA_VERSION", "REFINEMENT_TURN_MODEL",
    "SOFT_FIELD_MAPPING_METHOD", "TERRAIN_FORBIDDEN_METHODS", "TERRAIN_SAMPLING_METHOD",
    "V3B_DISCLAIMER", "V3B_RESULT_STATUSES", "V3C_PENDING",
    "contract_fingerprint", "default_v3_fine_refinement_policy",
    "empty_fine_cell_environment", "empty_fine_grid_spec", "empty_local_metric_frame",
    "empty_v3_refinement_problem", "empty_v3_refinement_result",
    "evaluate_refinement_applicability", "fine_grid_fingerprint", "frame_fingerprint",
    "normalize_fine_cell_environment", "normalize_fine_grid_spec",
    "normalize_local_metric_frame", "normalize_v3_fine_refinement_policy",
    "normalize_v3_refinement_problem", "normalize_v3_refinement_result",
    "refinement_fingerprint", "refinement_fingerprint_components",
]
