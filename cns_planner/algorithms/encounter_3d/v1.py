"""Local-ENU 3D engineering encounter assessment and explicit DAA response flow."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import math

from ...domain.encounter_3d import empty_encounter_3d_assessment
from ...domain.operational_timing import RESPONSE_COMPONENTS
from .state_machine import DAAEventStateMachineV1


EARTH_RADIUS_M = 6371008.8


class EncounterAssessment3DV1:
    algorithm_id = "encounter_assessment_3d_v1"
    algorithm_version = "1.0"

    @classmethod
    def empty(cls, status="not_calculated"):
        return empty_encounter_3d_assessment(status)

    def evaluate(self, timing, service_timeline=None, protection=None, selection=None):
        timing, selection = timing or {}, selection or (timing or {}).get("encounter_lab") or {}
        tracks, policies = timing.get("encounter_tracks") or {}, timing.get("encounter_policies") or {}
        own = tracks.get(str(selection.get("ownship_track_id") or ""))
        intruder = tracks.get(str(selection.get("intruder_track_id") or ""))
        policy = policies.get(str(selection.get("policy_id") or ""))
        capability = (timing.get("maneuver_capability_profiles") or {}).get(str(selection.get("capability_id") or ""))
        command = (timing.get("maneuver_commands") or {}).get(str(selection.get("command_id") or ""))
        fingerprint_inputs = {
            "ownship": own, "intruder": intruder, "policy": policy,
            "service_timeline": service_timeline or {}, "protection": protection or {},
            "capability": capability, "command": command, "selection": selection,
        }
        base = empty_encounter_3d_assessment()
        base["input_fingerprint"] = _fingerprint(fingerprint_inputs)
        base["selection"] = deepcopy(selection)
        base["inputs"] = {
            "policy": deepcopy(policy), "maneuver_capability": deepcopy(capability),
            "maneuver_command": deepcopy(command),
        }
        base["tracks"] = deepcopy([item for item in (own, intruder) if item])
        reasons = []
        if not own or not intruder:
            reasons.append("缺少 ownship/intruder EncounterTrack")
        for label, track in (("ownship", own), ("intruder", intruder)):
            if track and track.get("status") != "confirmed":
                reasons.append(f"{label} track 未确认或 EGM2008 高度未解析")
        if not policy or policy.get("status") != "confirmed":
            reasons.append("EncounterPolicy 未确认；未内置法规阈值")
        if reasons:
            base.update(status="pending_confirmation", reasons=reasons)
            return base
        geometry = assess_geometry(own, intruder, policy)
        base["geometry"] = geometry
        base["engineering_predicted_conflict"] = geometry.get("engineering_predicted_conflict")
        if geometry.get("status") != "passed":
            base.update(status=geometry.get("status", "unresolved"), reasons=geometry.get("reasons") or [])
            return base
        gating = _cns_gating(service_timeline or {}, selection.get("service_route_id"), geometry)
        base["cns_gating"] = gating
        machine, budget_timeline, maneuvered = self._run_state_machine(
            geometry, policy, gating, protection or {}, capability, command, own, intruder,
        )
        base["state_machine"] = machine.result()
        base["budget_timeline"] = budget_timeline
        if maneuvered:
            base["maneuvered_tracks"] = [maneuvered]
            base["post_maneuver_geometry"] = assess_geometry(maneuvered, intruder, policy)
        base["status"] = "passed" if machine.current_state == "CLEARED" else "engineering_event"
        base["reasons"] = []
        return base

    def _run_state_machine(self, geometry, policy, gating, protection, capability, command, own, intruder):
        machine, timeline = DAAEventStateMachineV1(), []
        start = float(geometry["overlap_start_s"])
        entry = geometry.get("threshold_entry_s")
        s_state = _gate_state(gating, "S", start)
        if s_state not in ("available", "available_degraded", "contingency"):
            machine.transition("LOST_TRACK", start, "S service cannot support detection/tracking", gating.get("S"))
            return machine, timeline, None
        components = _budget_components(protection)
        detect = start + components.get("detect", 0.0)
        machine.transition("DETECTED", detect, "traffic detected", {"S": gating.get("S")})
        tracked = detect + components.get("track", 0.0)
        if _gate_state(gating, "S", tracked) not in ("available", "available_degraded", "contingency"):
            machine.transition("LOST_TRACK", tracked, "S service lost before track establishment", gating.get("S"))
            return machine, _budget_rows(start, components, [detect, tracked]), None
        machine.transition("TRACKED", tracked, "track established", {"S": gating.get("S")})
        if not geometry.get("engineering_predicted_conflict"):
            machine.transition("CLEARED", tracked, "engineering thresholds are not jointly entered", {"geometry": "no_predicted_conflict"})
            return machine, _budget_rows(start, components, [detect, tracked]), None
        predicted = tracked + components.get("processing", 0.0)
        if _gate_state(gating, "S", predicted) not in ("available", "available_degraded", "contingency"):
            machine.transition("LOST_TRACK", predicted, "S service lost during conflict prediction", gating.get("S"))
            return machine, _budget_rows(start, components, [detect, tracked, predicted]), None
        machine.transition("PREDICTED_CONFLICT", predicted, "confirmed engineering policy predicts threshold entry", {"entry_time_s": entry})
        warning = max(predicted, float(entry) - float(policy["advisory_lead_time_s"]))
        if _gate_state(gating, "S", warning) not in ("available", "available_degraded", "contingency"):
            machine.transition("LOST_TRACK", warning, "S service lost before warning", gating.get("S"))
            return machine, _budget_rows(start, components, [detect, tracked, predicted, warning]), None
        c_warning = _gate_state(gating, "C", warning)
        if c_warning not in ("available", "available_degraded", "contingency"):
            machine.transition("ALERT_DELIVERY_FAILED", warning, "C service unavailable for warning delivery", gating.get("C"))
            return machine, _budget_rows(start, components, [detect, tracked, predicted, warning]), None
        machine.transition("WARNING", warning, "engineering advisory lead reached", {"C": gating.get("C")})
        action = max(warning, float(entry) - float(policy["warning_lead_time_s"]))
        if _gate_state(gating, "S", action) not in ("available", "available_degraded", "contingency"):
            machine.transition("LOST_TRACK", action, "S service lost before action deadline", gating.get("S"))
            return machine, _budget_rows(start, components, [detect, tracked, predicted, warning, action]), None
        machine.transition("ACTION_REQUIRED", action, "engineering warning lead reached", {"entry_time_s": entry})
        if protection.get("status") != "passed" or protection.get("t_pre_s") is None:
            machine.transition("MANEUVER_UNRESOLVED", action, "Protection Budget 未通过，响应截止不可确认", {"protection_status": protection.get("status")})
            return machine, _budget_rows(start, components, [detect, tracked, predicted, warning, action]), None
        if start + float(protection["t_pre_s"]) > float(entry):
            machine.transition("MANEUVER_UNRESOLVED", action, "Protection Budget exceeds threshold-entry deadline", {"t_pre_s": protection.get("t_pre_s"), "available_s": float(entry) - start})
            return machine, _budget_rows(start, components, [detect, tracked, predicted, warning, action]), None
        if not command or command.get("status") != "confirmed":
            machine.transition("COMMAND_UNAVAILABLE", action, "没有 confirmed ManeuverCommand", {})
            return machine, _budget_rows(start, components, [detect, tracked, predicted, warning, action]), None
        if not capability or capability.get("status") != "confirmed":
            machine.transition("MANEUVER_UNRESOLVED", action, "缺少 confirmed ManeuverCapabilityProfile", {})
            return machine, _budget_rows(start, components, [detect, tracked, predicted, warning, action]), None
        violation = _command_violation(command, capability, own.get("track_id"))
        if violation:
            machine.transition("MANEUVER_UNRESOLVED", action, violation, {"command_id": command.get("command_id")})
            return machine, _budget_rows(start, components, [detect, tracked, predicted, warning, action]), None
        sent = max(
            action + components.get("decision", 0.0) + components.get("communication", 0.0),
            float(command.get("issued_time_s") or 0.0),
        )
        c_command = _gate_state(gating, "C", sent)
        if _gate_state(gating, "S", sent) not in ("available", "available_degraded", "contingency"):
            machine.transition("LOST_TRACK", sent, "S service lost before command delivery", gating.get("S"))
            return machine, _budget_rows(start, components, [detect, tracked, predicted, warning, action, sent]), None
        if c_command not in ("available", "available_degraded", "contingency"):
            machine.transition("COMMAND_UNAVAILABLE", sent, "C service unavailable for command delivery", gating.get("C"))
            return machine, _budget_rows(start, components, [detect, tracked, predicted, warning, action, sent]), None
        machine.transition("COMMAND_SENT", sent, "confirmed manual/scenario command delivered", {"command_id": command.get("command_id")})
        executing = sent + float(capability.get("response_delay_s") or 0.0)
        if float(capability.get("response_delay_s") or 0.0) > components.get("aircraft_reaction", 0.0) + 1e-9:
            machine.transition("MANEUVER_UNRESOLVED", sent, "actual aircraft response delay exceeds Protection Budget", {"actual_response_delay_s": capability.get("response_delay_s"), "aircraft_reaction_budget_s": components.get("aircraft_reaction")})
            return machine, _budget_rows(start, components, [detect, tracked, predicted, warning, action, sent, executing]), None
        if executing > float(entry):
            machine.transition("MANEUVER_UNRESOLVED", sent, "command execution begins after threshold-entry deadline", {"execution_time_s": executing, "entry_time_s": entry})
            return machine, _budget_rows(start, components, [detect, tracked, predicted, warning, action, sent]), None
        n_state = _gate_state(gating, "N", executing)
        n_confidence = "nominal" if n_state == "available" else "degraded" if n_state in ("available_degraded", "contingency") else "unknown"
        machine.transition("EXECUTING", executing, "aircraft response delay elapsed", {"N_confidence": n_confidence, "N_state": n_state})
        effective_command = {**command, "issued_time_s": sent}
        maneuvered = simulate_command(own, effective_command, capability, geometry["overlap_end_s"])
        post = assess_geometry(maneuvered, intruder, policy)
        finish = min(float(geometry["overlap_end_s"]), executing + float(command["duration_s"]))
        if post.get("engineering_predicted_conflict"):
            machine.transition("MANEUVER_UNRESOLVED", finish, "given command does not clear engineering thresholds", {"post_maneuver": post})
        else:
            machine.transition("CLEARED", finish, "given command clears engineering thresholds", {"post_maneuver": post})
        times = [detect, tracked, predicted, warning, action, sent, executing, finish]
        return machine, _budget_rows(start, components, times), maneuvered


def assess_geometry(own, intruder, policy):
    own_samples, int_samples = own.get("samples") or [], intruder.get("samples") or []
    start = max(float(own_samples[0]["time_s"]), float(int_samples[0]["time_s"]))
    end = min(float(own_samples[-1]["time_s"]), float(int_samples[-1]["time_s"]), start + float(policy["lookahead_s"]))
    if end <= start:
        return {"status": "no_time_overlap", "engineering_predicted_conflict": None, "reasons": ["tracks have no overlapping time interval"]}
    lon0 = (_interp(own_samples, start)[0] + _interp(int_samples, start)[0]) / 2.0
    lat0 = (_interp(own_samples, start)[1] + _interp(int_samples, start)[1]) / 2.0
    boundaries = sorted({start, end, *[float(s["time_s"]) for s in own_samples if start < float(s["time_s"]) < end], *[float(s["time_s"]) for s in int_samples if start < float(s["time_s"]) < end]})
    best, entries = None, []
    for a, b in zip(boundaries, boundaries[1:]):
        oa, ob = _enu(_interp(own_samples, a), lon0, lat0), _enu(_interp(own_samples, b), lon0, lat0)
        ia, ib = _enu(_interp(int_samples, a), lon0, lat0), _enu(_interp(int_samples, b), lon0, lat0)
        duration = b - a
        r = [ia[i] - oa[i] for i in range(3)]
        v = [((ib[i] - ia[i]) - (ob[i] - oa[i])) / duration for i in range(3)]
        hv2 = v[0] ** 2 + v[1] ** 2
        tau = max(0.0, min(duration, -(r[0] * v[0] + r[1] * v[1]) / hv2)) if hv2 > 1e-12 else 0.0
        vector = [r[i] + v[i] * tau for i in range(3)]
        metric = {"time_s": a + tau, "horizontal_m": math.hypot(vector[0], vector[1]), "vertical_m": abs(vector[2]), "slant_m": math.sqrt(sum(x*x for x in vector))}
        if best is None or metric["horizontal_m"] < best["horizontal_m"]:
            best = metric
        joint = _joint_threshold_interval(r, v, duration, float(policy["horizontal_threshold_m"]), float(policy["vertical_threshold_m"]))
        if joint:
            entries.append([a + joint[0], a + joint[1]])
    entries = _merge_ranges(entries)
    own_start, int_start = _interp(own_samples, start), _interp(int_samples, start)
    current_h = _distance_horizontal(own_start, int_start)
    current_v = abs(own_start[2] - int_start[2])
    cpa_own, cpa_int = _interp(own_samples, best["time_s"]), _interp(int_samples, best["time_s"])
    return {
        "status": "passed", "overlap_start_s": start, "overlap_end_s": end,
        "current_horizontal_separation_m": current_h,
        "current_vertical_separation_m": current_v,
        "current_slant_separation_m": math.hypot(current_h, current_v),
        "time_to_horizontal_cpa_s": best["time_s"] - start,
        "horizontal_cpa_m": best["horizontal_m"],
        "vertical_separation_at_cpa_m": best["vertical_m"],
        "slant_cpa_m": best["slant_m"], "cpa_time_s": best["time_s"],
        "cpa_position": {"lon": (cpa_own[0] + cpa_int[0]) / 2, "lat": (cpa_own[1] + cpa_int[1]) / 2, "altitude_egm2008_m": (cpa_own[2] + cpa_int[2]) / 2},
        "engineering_predicted_conflict": bool(entries),
        "threshold_intervals": [{"entry_time_s": a, "exit_time_s": b} for a, b in entries],
        "threshold_entry_s": entries[0][0] if entries else None,
        "threshold_exit_s": entries[-1][1] if entries else None,
        "uncertainty_semantics": "recorded_not_applied_to_thresholds_v1",
    }


def simulate_command(track, command, capability, end_time):
    samples = track["samples"]
    execute = float(command["issued_time_s"]) + float(capability["response_delay_s"])
    end_time = max(execute, float(end_time))
    origin = _interp(samples, execute)
    lon0, lat0 = origin[0], origin[1]
    p0 = _enu(origin, lon0, lat0)
    before = _interp(samples, max(float(samples[0]["time_s"]), execute - 0.5))
    after = _interp(samples, min(float(samples[-1]["time_s"]), execute + 0.5))
    dtv = max(1e-6, min(float(samples[-1]["time_s"]), execute + 0.5) - max(float(samples[0]["time_s"]), execute - 0.5))
    va, vb = _enu(before, lon0, lat0), _enu(after, lon0, lat0)
    vx, vy = (vb[0]-va[0])/dtv, (vb[1]-va[1])/dtv
    speed, heading = math.hypot(vx, vy), math.atan2(vx, vy)
    points = [deepcopy(s) for s in samples if float(s["time_s"]) < execute]
    points.append({"time_s": execute, "lon": origin[0], "lat": origin[1], "altitude_egm2008_m": origin[2]})
    x, y, z, t = p0[0], p0[1], p0[2], execute
    command_end = execute + float(command["duration_s"])
    while t < end_time - 1e-9:
        dt = min(1.0, end_time - t)
        active = t < command_end
        if active:
            heading += math.radians(float(command["turn_rate_deg_s"])) * dt
            speed = max(0.0, speed + float(command["horizontal_accel_mps2"]) * dt)
        x += math.sin(heading) * speed * dt
        y += math.cos(heading) * speed * dt
        z += (float(command["vertical_speed_mps"]) if active else 0.0) * dt
        t += dt
        lon, lat, alt = _from_enu((x, y, z), lon0, lat0)
        points.append({"time_s": t, "lon": lon, "lat": lat, "altitude_egm2008_m": alt})
    return {**deepcopy(track), "samples": points, "source": f"simulated_from:{command.get('command_id')}", "maneuver_simulation": "given_command_only_no_optimization"}


def _cns_gating(timeline, route_id, geometry):
    routes = timeline.get("routes") or []
    route = next((r for r in routes if str(r.get("route_id")) == str(route_id)), routes[0] if routes else {})
    time_s = float(geometry.get("overlap_start_s") or 0.0)
    result = {}
    for code in ("C", "N", "S"):
        subsystem = next((s for s in route.get("subsystems") or [] if s.get("subsystem") == code), {})
        interval = next((i for i in subsystem.get("intervals") or [] if float(i.get("start_time_s", 0)) <= time_s < float(i.get("end_time_s", 0))), None)
        result[code] = {"state": (interval or {}).get("service_state", "unknown"), "route_id": route.get("route_id"), "time_s": time_s, "intervals": deepcopy(subsystem.get("intervals") or []), "evidence": deepcopy(interval or {})}
    n = result["N"]["state"]
    result["ownship_state_confidence"] = "nominal" if n == "available" else "degraded" if n in ("available_degraded", "contingency") else "unknown"
    result["semantics"] = "C/N/S gating only; not a SafetyEvent or UnacceptableEvent"
    return result


def _gate_state(gating, code, time_s):
    item = gating.get(code) or {}
    interval = next((value for value in item.get("intervals") or [] if float(value.get("start_time_s", 0)) <= float(time_s) < float(value.get("end_time_s", 0))), None)
    return (interval or {}).get("service_state", item.get("state", "unknown"))


def _budget_components(protection):
    result = {}
    for name in RESPONSE_COMPONENTS:
        value = ((protection.get("components") or {}).get(name) or {}).get("value_s")
        result[name] = float(value) if value is not None else 0.0
    return result


def _budget_rows(start, components, milestones):
    rows, marks = [], [float(value) for value in milestones]
    phases = {}
    if len(marks) >= 1: phases["detect"] = (float(start), marks[0])
    if len(marks) >= 2: phases["track"] = (marks[0], marks[1])
    if len(marks) >= 3: phases["processing"] = (marks[1], marks[2])
    if len(marks) >= 6:
        decision_end = marks[4] + float(components.get("decision", 0.0))
        phases["decision"] = (marks[4], decision_end)
        phases["communication"] = (decision_end, marks[5])
    if len(marks) >= 7: phases["aircraft_reaction"] = (marks[5], marks[6])
    for name in RESPONSE_COMPONENTS:
        value, phase = float(components.get(name, 0.0)), phases.get(name)
        actual = None if phase is None else max(0.0, phase[1] - phase[0])
        rows.append({
            "component": name, "budget_s": value, "actual_elapsed_s": actual,
            "start_time_s": phase[0] if phase else None,
            "end_time_s": phase[1] if phase else None,
            "status": "not_started" if actual is None else "within_budget" if actual <= value + 1e-9 else "exceeded",
        })
    return rows


def _command_violation(command, capability, ownship_id):
    if command.get("ownship_track_id") != ownship_id:
        return "ManeuverCommand ownship_track_id 不匹配"
    limits = (("turn_rate_deg_s", "max_turn_rate_deg_s"), ("horizontal_accel_mps2", "max_horizontal_accel_mps2"), ("vertical_speed_mps", "max_vertical_speed_mps"))
    for value, limit in limits:
        if abs(float(command[value])) > float(capability[limit]):
            return f"ManeuverCommand {value} 超出 confirmed capability"
    return None


def _interp(samples, time_s):
    if time_s <= float(samples[0]["time_s"]):
        s = samples[0]; return float(s["lon"]), float(s["lat"]), float(s["altitude_egm2008_m"])
    if time_s >= float(samples[-1]["time_s"]):
        s = samples[-1]; return float(s["lon"]), float(s["lat"]), float(s["altitude_egm2008_m"])
    for left, right in zip(samples, samples[1:]):
        a, b = float(left["time_s"]), float(right["time_s"])
        if a <= time_s <= b:
            f = (time_s-a)/(b-a)
            return tuple(float(left[k]) + f*(float(right[k])-float(left[k])) for k in ("lon", "lat", "altitude_egm2008_m"))
    raise ValueError("time outside track")


def _enu(point, lon0, lat0):
    lon, lat, altitude = point
    return (math.radians(lon-lon0)*EARTH_RADIUS_M*math.cos(math.radians(lat0)), math.radians(lat-lat0)*EARTH_RADIUS_M, altitude)


def _from_enu(point, lon0, lat0):
    x, y, z = point
    return (lon0 + math.degrees(x/(EARTH_RADIUS_M*math.cos(math.radians(lat0)))), lat0 + math.degrees(y/EARTH_RADIUS_M), z)


def _distance_horizontal(a, b):
    lat0 = (a[1]+b[1])/2
    return math.hypot(math.radians(a[0]-b[0])*EARTH_RADIUS_M*math.cos(math.radians(lat0)), math.radians(a[1]-b[1])*EARTH_RADIUS_M)


def _joint_threshold_interval(r, v, duration, h, z):
    a = v[0]**2 + v[1]**2; b = 2*(r[0]*v[0]+r[1]*v[1]); c = r[0]**2+r[1]**2-h**2
    if a < 1e-12:
        horizontal = (0.0, duration) if c <= 0 else None
    else:
        disc = b*b-4*a*c
        horizontal = None if disc < 0 else (max(0.0, (-b-math.sqrt(disc))/(2*a)), min(duration, (-b+math.sqrt(disc))/(2*a)))
    if abs(v[2]) < 1e-12:
        vertical = (0.0, duration) if abs(r[2]) <= z else None
    else:
        roots = sorted(((-z-r[2])/v[2], (z-r[2])/v[2]))
        vertical = (max(0.0, roots[0]), min(duration, roots[1]))
    if not horizontal or not vertical:
        return None
    result = max(horizontal[0], vertical[0]), min(horizontal[1], vertical[1])
    return result if result[0] <= result[1] else None


def _merge_ranges(ranges):
    result = []
    for start, end in sorted(ranges):
        if result and start <= result[-1][1] + 1e-9:
            result[-1][1] = max(result[-1][1], end)
        else:
            result.append([start, end])
    return result


def _fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
