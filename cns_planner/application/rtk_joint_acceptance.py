"""M2 read-only RTK joint research evaluation. No state, I/O or geometry search.

Explicit opt-in contract; legacy P17 v2.4 is not changed. Segments are already
projected along-route evidence from P14/P15, not coverage radii. Unknown evidence
is retained even when a known violation determines the aggregate verdict.
"""
from copy import deepcopy
import hashlib
import json
import math

from ..algorithms.corridor_gap.v1 import _merge_union
from ..domain.cns_continuous_service import continuous_gap_duration

VERSION = "rtk_joint_acceptance.v1"
SERVICE_KEY = "N:rtk_augmentation"
GOOD = "satisfied"
BAD = "confirmed_deficit"


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _bounds(item, length):
    a, b = item.get("start_m"), item.get("end_m")
    if not (_number(a) and _number(b) and 0 <= a < b <= length):
        raise ValueError("区段须满足 0 <= start_m < end_m <= route_length_m")
    return a, b


def gap_duration(start_m, end_m, speed_profile):
    """Integrate disjoint, confirmed constant-speed pieces; never fill a speed.

    Overlap, incomplete coverage, zero speed or absent provenance => unknown.
    """
    pieces = []
    for row in speed_profile or []:
        a, b = row.get("start_m"), row.get("end_m")
        if not (_number(a) and _number(b) and 0 <= a < b):
            return None, [], "speed_profile_invalid"
        a, b = max(a, start_m), min(b, end_m)
        if a >= b:
            continue
        speed = row.get("speed_mps")
        if (row.get("confirmed") is not True or not row.get("source_id") or not row.get("version")
                or not _number(speed) or speed <= 0):
            return None, [], "speed_evidence_missing_or_inapplicable"
        pieces.append((a, b, row))
    cursor, duration, sources = start_m, 0.0, []
    for a, b, row in sorted(pieces, key=lambda x: (x[0], x[1])):
        if a != cursor:
            return None, sources, "speed_profile_incomplete_or_overlapping"
        duration += continuous_gap_duration(b-a, row["speed_mps"])
        sources.append(deepcopy(row))
        cursor = b
    if cursor != end_m:
        return None, sources, "speed_profile_incomplete_or_overlapping"
    return duration, sources, None


def _segment_status(row, baseline, count, delivery_key):
    causes, unknown = [], []
    if row.get("evidence_status") != "confirmed" or not row.get("source_fingerprint"):
        return [], ["segment_evidence_unconfirmed"]
    geometry = row.get("geometry_status")
    if geometry == BAD:
        causes.append("reference_station_geometry_deficit")
    elif geometry != GOOD:
        unknown.append("segment_geometry_unknown")
    providers = row.get("providers") or []
    qualified, pending = set(), False
    rejected_baseline = rejected_device = rejected_eligibility = False
    for provider in providers:
        distance = provider.get("baseline_distance_m")
        identity = provider.get("distinct_site_id")
        eligibility, device = provider.get("eligibility_status"), provider.get("device_status")
        if eligibility not in (GOOD, BAD):
            unknown.append("provider_eligibility_unknown:"+str(identity))
        if device not in (GOOD, BAD):
            unknown.append("provider_device_unknown:"+str(identity))
        if not identity or not _number(distance) or distance < 0:
            pending = True
            unknown.append("provider_identity_or_baseline_unknown")
            continue
        if distance > baseline:
            rejected_baseline = True
            continue
        if eligibility == BAD:
            rejected_eligibility = True
        if device == BAD:
            rejected_device = True
        if eligibility == GOOD and device == GOOD:
            qualified.add(identity)
        elif eligibility != BAD and device != BAD:
            pending = True
    if len(qualified) < count:
        if pending or row.get("provider_inventory_status") != "complete":
            unknown.append("reference_provider_capability_or_inventory_unknown")
        else:
            causes.append("reference_station_deficit")
            if rejected_baseline:
                causes.append("reference_baseline_exceeded")
            if rejected_device:
                causes.append("device_capability_deficit")
            if rejected_eligibility:
                causes.append("reference_station_ineligible")
    if row.get("delivery_service_key") != delivery_key:
        unknown.append("correction_delivery_scope_unknown")
    elif row.get("delivery_status") == BAD:
        causes.append("correction_delivery_deficit")
    elif row.get("delivery_status") != GOOD:
        unknown.append("correction_delivery_unknown")
    if row.get("handover_status") == BAD:
        causes.append("handover_deficit")
    elif row.get("handover_status") != GOOD:
        unknown.append("handover_evidence_unknown")
    # Preserve upstream P14/P15 negative evidence even with optimistic fields.
    causes.extend(row.get("gap_causes") or [])
    unknown.extend(row.get("evidence_required_reasons") or [])
    if row.get("service_status") == BAD and not causes:
        causes.append("upstream_confirmed_service_deficit")
    if row.get("service_status") == "unknown":
        unknown.append("upstream_service_unknown")
    return sorted(set(causes)), sorted(set(unknown))


# Every number has its own unit/provenance. No literature defaults are embedded.
_UNITS = {
    "outage_limit_s": "s", "valid_duration_s": "s",
    "horizontal_error_m": "m", "horizontal_drift_mps": "m/s",
    "horizontal_bound_m": "m", "detection_time_s": "s", "alert_time_s": "s",
    "switch_time_s": "s", "response_time_s": "s", "max_transition_time_s": "s",
}


def _backup(gap, model, snapshot, *, synthetic_fixture):
    model = model or {}
    unknown, violations = [], []
    track = model.get("evidence_track")
    synthetic = track == "synthetic_fixture"
    permitted = track in ("real_aircraft_evidence", "literature_grounded_simulation")
    if synthetic and synthetic_fixture:
        permitted = True
    if not permitted:
        unknown.append("backup_evidence_track_missing_or_disallowed")
    for field in ("scenario_id", "model_id", "source_id", "version", "assumptions", "uncertainty"):
        if not model.get(field):
            unknown.append("backup_"+field+"_missing")
    scope = model.get("scope") or {}
    for field in ("route_id", "scenario_id", "operating_phase"):
        if not snapshot.get(field) or scope.get(field) != snapshot.get(field):
            unknown.append("backup_scope_"+field+"_mismatch")
    operation = snapshot["frozen_inputs"].get("operation_scenario") or {}
    for field in ("space_scope", "cruise_altitude"):
        if not operation.get(field) or scope.get(field) != operation.get(field):
            unknown.append("backup_scope_"+field+"_mismatch")
    if track == "real_aircraft_evidence":
        aircraft = snapshot["frozen_inputs"].get("aircraft_profile") or {}
        if not aircraft.get("aircraft_id") or model.get("aircraft_id") != aircraft.get("aircraft_id"):
            unknown.append("real_aircraft_identity_mismatch")
    if track == "literature_grounded_simulation" and (
            model.get("scenario_id") == snapshot.get("scenario_id") or model.get("aircraft_id")):
        unknown.append("simulation_identity_not_independent")
    if model.get("schema_version") != "navigation_backup_envelope.v1":
        unknown.append("backup_model_version_unsupported")
    if model.get("review_status") != "verified_for_scope":
        unknown.append("backup_source_applicability_unverified")
    # An envelope is valid only when caller explicitly establishes whether vertical
    # containment is necessary. Nothing silently assumes a 2-D flight problem.
    vertical = model.get("vertical_required")
    if not isinstance(vertical, bool):
        unknown.append("vertical_requirement_unknown")
    units = dict(_UNITS)
    if vertical is True:
        units.update(vertical_error_m="m", vertical_drift_mps="m/s", vertical_bound_m="m")
    # Bibliographic/device evidence is a caller-reviewed input registry, not a
    # claim that a source string proves applicability. Test-only evidence cannot
    # be promoted by changing evidence_track.
    sources = model.get("sources") or {}
    if not synthetic:
        refs = [model.get("source_id")]
        refs.extend((p or {}).get("source_ref") for p in (model.get("parameters") or {}).values())
        refs.extend((c or {}).get("source_ref") for c in (model.get("conditions") or {}).values())
        allowed = ("manufacturer", "device_measurement") if track == "real_aircraft_evidence" else ("paper", "standard", "technical_report", "manufacturer")
        for ref in refs:
            source = sources.get(ref) or {}
            if (not ref or "synthetic" in str(ref).lower()
                    or source.get("kind") not in allowed
                    or not all(source.get(k) for k in ("title", "authors", "year", "version", "applicability"))
                    or not (source.get("doi") or source.get("url"))):
                unknown.append("backup_source_registry_missing_or_cross_track")
        if any((p or {}).get("reliability") == "test-only" for p in (model.get("parameters") or {}).values()):
            unknown.append("synthetic_parameter_cross_track_rejected")
    trusted = not unknown
    parameters, values = model.get("parameters") or {}, {}
    for name, unit in units.items():
        item = parameters.get(name) or {}
        valid = (_number(item.get("value")) and item["value"] >= 0 and item.get("unit") == unit
                 and all(item.get(k) for k in ("source_ref", "locator", "version", "conversion",
                                               "reliability", "uncertainty_range")))
        if valid:
            values[name] = item["value"]
        else:
            unknown.append("backup_parameter_missing:"+name)
    # Do not treat unscoped / unreviewed figures as confirmed violations or passes.
    for name in ("availability", "integrity", "protection", "response", "hard_constraints"):
        item = (model.get("conditions") or {}).get(name) or {}
        if not item.get("source_ref"):
            unknown.append("backup_condition_source_missing:"+name)
        elif item.get("status") == BAD and trusted:
            violations.append("backup_condition_failed:"+name)
        elif item.get("status") != GOOD:
            unknown.append("backup_condition_unknown:"+name)
    duration = gap["gap_duration_s"]
    if duration is None:
        unknown.append("gap_time_unknown")
    if trusted and duration is not None:
        if "outage_limit_s" in values and duration > values["outage_limit_s"]:
            violations.append("rtk_outage_tolerance_exceeded")
        if "valid_duration_s" in values and duration > values["valid_duration_s"]:
            unknown.append("backup_duration_outside_model_scope")
        elif "valid_duration_s" in values:
            for axis in ("horizontal", "vertical") if vertical is True else ("horizontal",):
                if not all(axis+suffix in values for suffix in ("_error_m", "_drift_mps", "_bound_m")):
                    continue
                error = values[axis+"_error_m"] + values[axis+"_drift_mps"] * duration
                if error > values[axis+"_bound_m"]:
                    violations.append(axis+"_containment_exceeded")
        timing = ("detection_time_s", "alert_time_s", "switch_time_s", "response_time_s")
        if all(k in values for k in (*timing, "max_transition_time_s")):
            if sum(values[k] for k in timing) > values["max_transition_time_s"]:
                violations.append("backup_transition_time_exceeded")
    unknown.extend(gap["reason_codes"])
    status = "unacceptable" if violations else "unknown" if unknown else "managed"
    return dict(status=status, reason_codes=sorted(set(unknown+violations)),
        unknowns=sorted(set(unknown)), violations=violations,
        management_basis=("managed_under_simulation" if track != "real_aircraft_evidence"
                          else "managed_under_real_evidence") if status == "managed" else None,
        validation_status="illustrative_not_validated" if synthetic else "source_review_required_by_caller",
        evidence_track=track, scenario_id=model.get("scenario_id"), model_id=model.get("model_id"),
        source_id=model.get("source_id"), version=model.get("version"), input_fingerprint=_fingerprint(model))


def evaluate_p17_rtk_joint(*, frozen_snapshot, route_length_m, segments,
                          speed_profile=None, backup_scenario=None, synthetic_fixture=False,
                          service_evidence_track=None):
    """Controlled M2/P17 opt-in, proposal only; M1 frozen_ue_rules.v1 required.

    Caller projects P14/P15 geometry into segments and explicitly supplies device,
    eligibility and handover evidence. Incomplete partitions stay unknown. This
    function never infers service from aircraft availability or runs upstream work.
    """
    snapshot, rows, speeds, model = deepcopy((frozen_snapshot, segments, speed_profile, backup_scenario))
    if snapshot.get("schema_version") != "frozen_ue_rules.v1":
        raise ValueError("需要M1 frozen_ue_rules.v1快照")
    if not _number(route_length_m) or route_length_m <= 0:
        raise ValueError("route_length_m须为有限正数")
    payload = dict(algorithm_version=VERSION, frozen_snapshot=snapshot,
        route_length_m=route_length_m, segments=rows, speed_profile=speeds,
        backup_scenario=model, synthetic_fixture=synthetic_fixture,
        service_evidence_track=service_evidence_track)
    fingerprint = _fingerprint(payload)
    reasons, cells = [], []
    if service_evidence_track not in ("real_service_evidence", "engineering_research", "synthetic_fixture"):
        reasons.append("service_evidence_track_missing")
    fixture = synthetic_fixture or service_evidence_track == "synthetic_fixture" or (model or {}).get("evidence_track") == "synthetic_fixture"
    # Guard known fixture markers as well as the explicit identity; a caller must
    # not promote a marked test container by changing only its track label.
    if "synthetic_fixture" in json.dumps([snapshot, rows, model], ensure_ascii=False):
        fixture = True
    if fixture and not synthetic_fixture:
        reasons.append("synthetic_fixture_requires_opt_in")
    rules = {r["failure_mode"]: r for r in snapshot.get("rules", []) if r["service_key"] == SERVICE_KEY}
    modes = ("reference_baseline_exceeded", "reference_station_deficit")
    policy_ready = all(rules.get(mode, {}).get("engineering_rule_status") == "sourced_and_applicable" for mode in modes)
    requirements = snapshot["frozen_inputs"]["required_cns"]["requirements"]
    declaration = ((requirements.get("navigation") or {}).get("services") or {}).get(SERVICE_KEY) or {}
    adopted = declaration.get("required") is True and declaration.get("confirmed") is True
    if not adopted:
        reasons.append("required_service_not_adopted")
    baseline = (rules.get(modes[0], {}).get("threshold") or {}).get("value")
    count = (rules.get(modes[1], {}).get("threshold") or {}).get("value")
    delivery_key = (declaration.get("planning") or {}).get("delivery_service_key")
    if not (_number(baseline) and baseline > 0 and _number(count) and count >= 1 and int(count) == count
            and delivery_key == "C:communication" and policy_ready):
        policy_ready = False
        reasons.append("rtk_policy_missing_or_inapplicable")
    bounds = {0, route_length_m}
    for row in rows or []:
        a, b = _bounds(row, route_length_m)
        bounds.update((a, b))
        if row.get("route_id", snapshot["route_id"]) != snapshot["route_id"]:
            raise ValueError("RTK区段route_id不匹配")
    ordered = sorted(bounds)
    for a, b in zip(ordered, ordered[1:]):
        active = [r for r in rows or [] if r["start_m"] <= a and r["end_m"] >= b]
        causes, unknown = [], []
        if not active:
            unknown.append("segment_evidence_missing")
        elif policy_ready:
            for row in active:
                c, u = _segment_status(row, baseline, count, delivery_key)
                causes.extend(c); unknown.extend(u)
        else:
            unknown.append("rtk_policy_missing_or_inapplicable")
            for row in active:
                if row.get("evidence_status") == "confirmed" and row.get("source_fingerprint"):
                    causes.extend(row.get("gap_causes") or [])
                    if row.get("delivery_status") == BAD and row.get("delivery_service_key") == "C:communication":
                        causes.append("correction_delivery_deficit")
        cells.append(dict(start_m=a, end_m=b, gap_cause=sorted(set(causes)),
            reason_codes=sorted(set(unknown)), evidence_status=BAD if causes else "unknown" if unknown else GOOD))
    gaps = []
    # Reuse P15 along-route union; all causes remain in cells. Adjacent changes of
    # cause must not reset the outage clock, and every disjoint outage is retained.
    for a, b in _merge_union([r for r in cells if r["gap_cause"]]):
        selected = [r for r in cells if r["start_m"] < b and r["end_m"] > a]
        duration, speed_source, time_reason = gap_duration(a, b, speeds)
        unknown = sorted({u for r in selected for u in r["reason_codes"]})
        if time_reason:
            unknown.extend(("gap_time_unknown", time_reason))
        neighbors = [r for r in cells if r["end_m"] == a or r["start_m"] == b]
        if any(r["reason_codes"] for r in neighbors):
            unknown.append("gap_extent_unknown")
        gap = dict(route_id=snapshot["route_id"], service_key=SERVICE_KEY,
            start_m=a, end_m=b, gap_length_m=b-a, gap_duration_s=duration,
            gap_cause=sorted({c for r in selected for c in r["gap_cause"]}),
            speed_source=speed_source, evidence_status="confirmed", source_fingerprint=fingerprint,
            time_status="known" if duration is not None else "unknown", reason_codes=unknown)
        gap["navigation_evaluation"] = _backup(gap, model, snapshot, synthetic_fixture=synthetic_fixture)
        gaps.append(gap)
    unknowns = sorted(set(reasons + [u for r in cells for u in r["reason_codes"]]
                         + [u for g in gaps for u in g["navigation_evaluation"]["unknowns"]]))
    statuses = [g["navigation_evaluation"]["status"] for g in gaps]
    rtk = "rtk_degraded_or_outage" if gaps else "rtk_unknown" if unknowns else "rtk_normal"
    navigation = ("unacceptable" if "unacceptable" in statuses else "unknown"
                  if unknowns or "unknown" in statuses else "managed" if gaps else "satisfied")
    if not adopted:
        rtk, navigation = "rtk_unknown", "unknown"
    return dict(schema_version=VERSION, algorithm_version=VERSION,
        legacy_p17_algorithm_version=snapshot["p17_parser"]["algorithm_version"],
        route_id=snapshot["route_id"], rtk_status=rtk, navigation_status=navigation,
        segments=cells, gaps=gaps, unknowns=unknowns, reason_codes=sorted(set(reasons+unknowns)),
        input_fingerprint=fingerprint, m1_input_fingerprint=snapshot.get("input_fingerprint"),
        evidence_track="synthetic_fixture" if fixture else (model or {}).get("evidence_track", service_evidence_track),
        service_evidence_track=service_evidence_track,
        validation_status="illustrative_not_validated" if fixture else "source_review_required_by_caller",
        violations=sorted({v for g in gaps for v in g["navigation_evaluation"]["violations"]}),
        ue_link_status="ue_link_unresolved", confirmed_device_capability=False,
        real_aircraft_evidence=deepcopy(snapshot.get("navigation_evidence")),
        maturity="proposal", persisted_to_project_state=False,
        synthetic_fixture=synthetic_fixture)


__all__ = ["evaluate_p17_rtk_joint", "gap_duration", "VERSION"]


def adapt_p14_p15_rtk_segments(*, route_id, route_length_m, assessed_voxels,
                             source_fingerprint):
    """Project P14/P15 evidence using existing offset +/- half-diagonal semantics.

    No geometry intersection or upstream computation. Device, inventory and
    handover fields absent from existing geometry evidence remain unknown.
    C delivery is the existing RTK dependency result, not generic C coverage.
    """
    rows = []
    for voxel in assessed_voxels or []:
        if voxel.get("route_id", route_id) != route_id:
            raise ValueError("P14/P15 voxel route_id不匹配")
        offset, half = voxel.get("nearest_route_offset_m"), voxel.get("cell_half_diagonal_m")
        if not (_number(offset) and _number(half) and half > 0):
            continue
        a, b = max(0, offset-half), min(route_length_m, offset+half)
        if a >= b:
            continue
        services = [entry for sub in voxel.get("subsystems", []) if sub.get("subsystem") == "N"
                    for entry in sub.get("service_redundancy", sub.get("services", []))
                    if entry.get("service_key") == SERVICE_KEY]
        for entry in services:
            rows.append(dict(route_id=route_id, start_m=a, end_m=b,
                geometry_status=entry.get("geometry_status"), providers=deepcopy(entry.get("providers") or []),
                provider_inventory_status=entry.get("provider_inventory_status"),
                delivery_status=entry.get("delivery_status"), delivery_service_key=entry.get("delivery_service_key"),
                handover_status=entry.get("handover_status"), service_status=entry.get("status"),
                gap_causes=deepcopy(entry.get("gap_causes") or []),
                evidence_required_reasons=deepcopy(entry.get("evidence_required_reasons") or []),
                evidence_status="confirmed" if source_fingerprint else "unknown",
                source_fingerprint=source_fingerprint, voxel_id=voxel.get("voxel_id")))
    return rows


__all__.append("adapt_p14_p15_rtk_segments")
