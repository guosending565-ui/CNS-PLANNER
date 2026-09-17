"""JSON-safe contracts for engineering 3D encounter simulation."""

from __future__ import annotations

from copy import deepcopy
from math import isfinite


TRACK_ROLES = ("ownship", "intruder")


def empty_encounter_policy():
    return {
        "policy_id": "default-encounter-policy", "horizontal_threshold_m": None,
        "vertical_threshold_m": None, "lookahead_s": None,
        "advisory_lead_time_s": None, "warning_lead_time_s": None,
        "source": "未配置；必须由项目工程依据确认", "confirmed": False,
        "status": "pending_confirmation", "regulatory_well_clear": "not_evaluated",
    }


def empty_encounter_3d_assessment(status="not_calculated"):
    return {
        "status": status, "algorithm_id": "encounter_assessment_3d_v1",
        "algorithm_version": "1.0", "input_fingerprint": None,
        "regulatory_well_clear": "not_evaluated",
        "engineering_predicted_conflict": None, "geometry": {},
        "tracks": [], "maneuvered_tracks": [], "state_machine": {
            "status": "not_calculated", "current_state": "NO_TRAFFIC", "transitions": [],
        },
        "cns_gating": {}, "budget_timeline": [], "reasons": [],
        "semantics": {
            "scope": "engineering_simulation_only",
            "service_state_is_not_encounter_or_safety_event": True,
            "encounter_event_is_not_safety_or_unacceptable_event": True,
            "hazard_zone": "not_created",
        },
    }


def normalize_encounter_track(value):
    if not isinstance(value, dict):
        raise ValueError("EncounterTrack 必须是对象")
    track_id = _identifier(value.get("track_id"), "track_id")
    role = str(value.get("role") or "").lower()
    if role not in TRACK_ROLES:
        raise ValueError("EncounterTrack.role 必须是 ownship/intruder")
    raw_samples = value.get("samples") or []
    if not isinstance(raw_samples, list):
        raise ValueError("EncounterTrack.samples 必须是数组")
    samples, unresolved = [], []
    for index, raw in enumerate(raw_samples):
        if not isinstance(raw, dict):
            raise ValueError("EncounterTrack sample 必须是对象")
        coordinate = raw.get("coordinate") or [raw.get("lon"), raw.get("lat")]
        time_s = _optional_number(raw.get("time_s"), f"samples[{index}].time_s")
        lon = _optional_number(coordinate[0] if len(coordinate) > 0 else None, f"samples[{index}].lon")
        lat = _optional_number(coordinate[1] if len(coordinate) > 1 else None, f"samples[{index}].lat")
        altitude = _optional_number(raw.get("altitude_egm2008_m"), f"samples[{index}].altitude_egm2008_m")
        reference = str(raw.get("vertical_reference") or "egm2008_orthometric").lower()
        if reference != "egm2008_orthometric":
            altitude = None
            unresolved.append(f"sample {index} vertical reference 不是 EGM2008")
        if None in (time_s, lon, lat, altitude):
            unresolved.append(f"sample {index} 缺少 time/lon/lat/EGM2008 altitude")
        samples.append({
            "time_s": time_s, "lon": lon, "lat": lat,
            "altitude_egm2008_m": altitude,
        })
    valid_times = [item["time_s"] for item in samples if item["time_s"] is not None]
    if valid_times != sorted(valid_times) or len(set(valid_times)) != len(valid_times):
        unresolved.append("sample time_s 必须严格递增")
    confirmed = bool(value.get("confirmed", False))
    complete = len(samples) >= 2 and not unresolved
    return {
        "track_id": track_id, "role": role, "samples": samples,
        "source": str(value.get("source") or "未记录"), "confirmed": confirmed,
        "position_uncertainty_m": _optional_nonnegative(value.get("position_uncertainty_m"), "position_uncertainty_m"),
        "velocity_uncertainty_mps": _optional_nonnegative(value.get("velocity_uncertainty_mps"), "velocity_uncertainty_mps"),
        "status": "confirmed" if confirmed and complete else "unresolved" if unresolved else "pending_confirmation",
        "reasons": unresolved,
        "vertical_reference": "egm2008_orthometric" if not unresolved else "unresolved",
    }


def normalize_encounter_policy(value):
    raw = value if isinstance(value, dict) else {}
    result = empty_encounter_policy()
    result.update({
        "policy_id": str(raw.get("policy_id") or result["policy_id"]),
        "horizontal_threshold_m": _optional_positive(raw.get("horizontal_threshold_m"), "horizontal_threshold_m"),
        "vertical_threshold_m": _optional_positive(raw.get("vertical_threshold_m"), "vertical_threshold_m"),
        "lookahead_s": _optional_positive(raw.get("lookahead_s"), "lookahead_s"),
        "advisory_lead_time_s": _optional_nonnegative(raw.get("advisory_lead_time_s"), "advisory_lead_time_s"),
        "warning_lead_time_s": _optional_nonnegative(raw.get("warning_lead_time_s"), "warning_lead_time_s"),
        "source": str(raw.get("source") or result["source"]), "confirmed": bool(raw.get("confirmed", False)),
    })
    complete = all(result[name] is not None for name in (
        "horizontal_threshold_m", "vertical_threshold_m", "lookahead_s",
        "advisory_lead_time_s", "warning_lead_time_s",
    ))
    result["status"] = "confirmed" if result["confirmed"] and complete else "pending_confirmation"
    return result


def normalize_maneuver_capability(value):
    raw = value if isinstance(value, dict) else {}
    fields = (
        "max_turn_rate_deg_s", "max_horizontal_accel_mps2", "max_vertical_speed_mps",
        "response_delay_s", "position_accuracy_m", "control_tracking_error_m",
    )
    result = {name: _optional_nonnegative(raw.get(name), name) for name in fields}
    result.update({
        "capability_id": str(raw.get("capability_id") or "default-maneuver-capability"),
        "wind_limit_mps": _optional_nonnegative(raw.get("wind_limit_mps"), "wind_limit_mps"),
        "source": str(raw.get("source") or "未记录"), "confirmed": bool(raw.get("confirmed", False)),
        "wind_limit_semantics": "stored_only_not_converted_to_safety_margin_v1",
    })
    result["status"] = "confirmed" if result["confirmed"] and all(result[name] is not None for name in fields) else "pending_confirmation"
    return result


def normalize_maneuver_command(value):
    raw = value if isinstance(value, dict) else {}
    result = {
        "command_id": str(raw.get("command_id") or "default-maneuver-command"),
        "ownship_track_id": str(raw.get("ownship_track_id") or ""),
        "issued_time_s": _optional_nonnegative(raw.get("issued_time_s"), "issued_time_s"),
        "duration_s": _optional_positive(raw.get("duration_s"), "duration_s"),
        "turn_rate_deg_s": _optional_number(raw.get("turn_rate_deg_s"), "turn_rate_deg_s"),
        "horizontal_accel_mps2": _optional_number(raw.get("horizontal_accel_mps2"), "horizontal_accel_mps2"),
        "vertical_speed_mps": _optional_number(raw.get("vertical_speed_mps"), "vertical_speed_mps"),
        "source": str(raw.get("source") or "未记录"), "confirmed": bool(raw.get("confirmed", False)),
    }
    numeric = ("issued_time_s", "duration_s", "turn_rate_deg_s", "horizontal_accel_mps2", "vertical_speed_mps")
    complete = bool(result["ownship_track_id"]) and all(result[name] is not None for name in numeric)
    result["status"] = "confirmed" if result["confirmed"] and complete else "pending_confirmation"
    return result


def normalize_encounter_3d_assessment(value):
    if not isinstance(value, dict):
        return empty_encounter_3d_assessment()
    result = empty_encounter_3d_assessment(str(value.get("status") or "not_calculated"))
    result.update(deepcopy(value))
    result.setdefault("regulatory_well_clear", "not_evaluated")
    result.setdefault("semantics", empty_encounter_3d_assessment()["semantics"])
    return result


def _identifier(value, field):
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{field} 不能为空")
    return result


def _optional_number(value, field):
    if value in (None, ""):
        return None
    number = float(value)
    if not isfinite(number):
        raise ValueError(f"{field} 必须是有限数值")
    return number


def _optional_nonnegative(value, field):
    number = _optional_number(value, field)
    if number is not None and number < 0:
        raise ValueError(f"{field} 不得小于零")
    return number


def _optional_positive(value, field):
    number = _optional_number(value, field)
    if number is not None and number <= 0:
        raise ValueError(f"{field} 必须大于零")
    return number
