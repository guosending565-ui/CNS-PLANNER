"""Read-only bounded C research adapter, separate from production/search.

No filesystem writes, session, RunStore or state publication. Callers supply
bytes or disposable materialized data. Hard process isolation belongs to the
external launcher; this module's counters are NOT OS resource enforcement.
Real execution stays blocked until a separately audited launcher is provided.
The current task deliberately supplies no such launcher when preflight fails.
"""
from copy import deepcopy
from hashlib import sha256
import json
import math
import time

from ..domain.geodesy import distance_m
from ..domain.research_scenario import checked_scenario
from .research_scenario_builder import build_conditional_domain
from .research_solver_envelope import build_real_solver_envelope
from .research_structural_enumeration import _digest

SCENARIO_SHA = "b51a18b16c5521fe114cd933ca0aed8b0c26a12bb26b3f80f87772bca9b3b6cb"
STATE_SHA = "25724dae269056e438181f96035ade5918e1b8bb7125eecb3e11bf6cef81ad9c"
SUBSET_FILE_SHA = "5720eb58932b404b32c97e0e2a0c401008e85ccf33c77eee39717342dbc53658"
SUBSET_SHA = "f50c26bde0e80eed66e89ed5a03c217666ed9dce589964d31689ff775d7d960d"
ACCEPTED_HEAD = "bbb07557c03358d728abceee525a0dc5024a2b53"
G4F_SHA = {
    "cns_planner/algorithms/corridor/v1.py": "b5fc5e426afeca67a04302626c7f448d21e636d76fbb41d304554e108365bb1c",
    "cns_planner/application/corridor_service.py": "9c40f753dee6ce8cad2beeb1fd165af0f779d9ec92b8a919a9850379d8b40661",
    "cns_planner/application/corridor_site_planning_service.py": "28cdeb0a8cdccbc93c59ef49c023f94860c67a576fb818cdeadaef6c18267df5",
}
SYNTHETIC_FIXTURE_SHA = "50073bb350853150d5e0796dc1ddc5918507a2dd9642763141382da9e9f8976a"
POLICY = "conditional_assumed_installation"
LIMITS = dict(actions=7, evaluations=8, nodes=64, time_ms=600000,
              memory_bytes=1073741824, output_bytes=8388608, replays=3, max_calibrations=0)
UNKNOWN = dict(activation="unknown", installed_inventory="unknown", qualification="unknown",
               capacity="unknown", cost="unknown", real_L1=None, real_L2=None, real_L3=None)
UNSUPPORTED = ["N:navigation_integrity_monitoring", "N:rtk_augmentation",
               "S:rid_cooperative", "S:radar_noncooperative", "full_required_proof",
               "L1/L2/L3_optimization", "P14_independent_geometry_proof"]


def frozen_code_identity(head, g4f_hashes):
    if head != ACCEPTED_HEAD or g4f_hashes != G4F_SHA:
        raise ValueError("G4f isolation identity mismatch")
    identity = dict(accepted_head=head, g4f_sha256=dict(g4f_hashes))
    return dict(identity, code_snapshot_fingerprint=_digest(identity))


def _decode(raw, expected):
    if not isinstance(raw, bytes) or sha256(raw).hexdigest() != expected:
        raise ValueError("frozen file SHA256 mismatch")
    return json.loads(raw.decode("utf-8-sig"))


def freeze_research_inputs(scenario_bytes, subset_bytes):
    scenario = checked_scenario(_decode(scenario_bytes, SCENARIO_SHA))
    subset = _decode(subset_bytes, SUBSET_FILE_SHA)
    route = scenario["route_scenario"]
    parents = route["conditional_actions"]
    included, excluded = subset["included_actions"], subset["excluded_actions"]
    if len(parents) != 42 or len(included) != 7 or len(excluded) != 35:
        raise ValueError("frozen domain counts mismatch")
    by_id = {a["action_id"]:a for a in parents}
    ids = [a["action_id"] for a in included]
    ex_ids = [a["action_id"] for a in excluded]
    if len(set(ids + ex_ids)) != 42 or set(ids + ex_ids) != set(by_id):
        raise ValueError("excluded ledger mismatch")
    identity_keys = ("action_id", "physical_site_id", "device_instance_id", "route_ids")
    if _digest([{k:a[k] for k in identity_keys} for a in included]) != SUBSET_SHA:
        raise ValueError("subset identity mismatch")
    for a in included:
        source = by_id[a["action_id"]]
        for k in (*identity_keys, "coordinate", "device_id", "service_key"):
            if a[k] != source[k]: raise ValueError("frozen action field mismatch: " + k)
        if a["service_key"] != "C:communication" or a["route_ids"] != ["R0005"]:
            raise ValueError("C R0005 scope required")
    if len({a["physical_site_id"] for a in included}) != 7:
        raise ValueError("seven distinct sites required")
    # Original rule: nearest VERTEX, then cumulative distance, vertex offset,
    # site and action ID. This is systematic sampling, not equal mileage.
    path = route["path"]; cumulative = [0.0]
    for left, right in zip(path, path[1:]):
        cumulative.append(cumulative[-1] + distance_m(left, right))
    def ordering(a):
        distances = [distance_m(a["coordinate"], p) for p in path]
        nearest = min(range(len(path)), key=lambda i:distances[i])
        return cumulative[nearest], distances[nearest], a["physical_site_id"], a["action_id"]
    ordered = sorted((a for a in parents if a["service_key"] == "C:communication"), key=ordering)
    ranks = [int(math.floor(i * (len(ordered)-1) / 6 + .5)) for i in range(7)]
    if ids != [ordered[i]["action_id"] for i in ranks] or ranks != [0,3,6,10,13,16,19]:
        raise ValueError("sampling rule not reproduced")
    domain = build_conditional_domain(parent_domain_review=route["parent_real_domain"],
        scenario=scenario, assumption_registry=route["assumption_registry"])
    envelope = build_real_solver_envelope(scenario=scenario, conditional_domain=domain,
                                         expected_source_sha256=STATE_SHA)
    if route["model_versions"].get("p14") != "1.4" or route["model_versions"].get("p15") != "1.3":
        raise ValueError("P14/P15 version mismatch")
    return dict(schema_version="research_real_c_inputs.v1", scenario=scenario,
        scenario_sha256=SCENARIO_SHA, envelope=envelope, actions=deepcopy(included),
        excluded_actions=deepcopy(excluded), subset_fingerprint=SUBSET_SHA,
        sampling_rule="p2a3a-subsample-v1", strict_equal_mileage_sampling=False,
        experiment_fingerprint=_digest([SCENARIO_SHA, SUBSET_SHA, POLICY, "H0", LIMITS]))


class EvaluationStopped(RuntimeError):
    pass


class EvaluationBudget:
    """A0 logical counters only; no claim of hard resource isolation."""
    def __init__(self):
        self.calls = 0
        self.nodes = 0
        self.output_bytes = 0
        self.stopped = None
        self.started = time.monotonic()

    def stop(self, reason):
        self.stopped = reason

    def record_output(self, size):
        if self.stopped: raise EvaluationStopped(self.stopped)
        if type(size) is not int or size < 0: raise ValueError("invalid output size")
        if self.output_bytes + size > LIMITS["output_bytes"]:
            self.stop("output_limit"); raise EvaluationStopped("output_limit")
        self.output_bytes += size

    def consume(self, selected):
        if self.stopped: raise EvaluationStopped(self.stopped)
        reason = ("a0_only" if len(selected) > 1 else
                  "evaluation_limit" if self.calls >= LIMITS["evaluations"] else
                  "node_limit" if self.nodes >= LIMITS["nodes"] else
                  "time_limit" if (time.monotonic()-self.started)*1000 >= LIMITS["time_ms"] else None)
        if reason:
            self.stop(reason); raise EvaluationStopped(reason)
        self.calls += 1
        self.nodes += 1


def prepare_inputs(state, *, route_id, source_identity):
    """Copy only computational facts; never hydrate through a writing session."""
    keys = ("operational_routes", "spatial_3d", "grid", "grid_attributes", "required_cns",
        "aircraft_profile", "device_catalog", "existing_cns_facilities", "cns_corridor_policy",
        "cns_planning_objectives", "algorithm_selection", "surface_class_facts",
        "planning_assumptions", "aircraft_profiles", "selected_aircraft_id")
    data = {k:deepcopy(state[k]) for k in keys if k in state}
    blockers = []
    for k in keys[:9]:
        if k not in state: blockers.append("missing_input:" + k)
    routes = [r for r in data.get("operational_routes", []) if r.get("route_id") == route_id]
    if len(routes) != 1: blockers.append("route_identity_missing_or_duplicate")
    if any(r.get("path_crs") != "OGC:CRS84" for r in routes): blockers.append("path_crs_unsupported")
    data["operational_routes"] = routes
    if data.get("spatial_3d",{}).get("canonical_vertical_reference") != "egm2008_orthometric":
        blockers.append("vertical_reference_unsupported")
    grid = data.get("grid", {})
    if not grid.get("cells") or grid.get("count",len(grid.get("cells",[]))) != len(grid.get("cells",[])):
        blockers.append("grid_cells_not_materialized")
    terrain = data.get("grid_attributes",{}).get("terrain",{})
    if "cells" not in terrain: blockers.append("terrain_cells_not_materialized")
    if source_identity.get("kind") != "synthetic_fixture":
        # No OS lease is fabricated by an in-process boolean or caller token.
        blockers.append("verified_hard_isolation_launcher_unavailable")
    elif _digest(data) != SYNTHETIC_FIXTURE_SHA:
        blockers.append("unregistered_synthetic_fixture")
    return dict(state=data, route_ids=[r["route_id"] for r in routes],
        source_identity=deepcopy(source_identity), activation_policy="H0", blockers=blockers)


def project_facilities(baseline, selected, source_actions, *, policy):
    if policy != POLICY: raise ValueError("explicit conditional installation policy required")
    working = deepcopy(baseline)
    if not isinstance(working.get("items"), list): raise ValueError("baseline facilities missing")
    conditional = []
    seen = set()
    for a in selected:
        if a["action_id"] in seen: raise ValueError("duplicate selection")
        seen.add(a["action_id"])
        source = source_actions.get(a["action_id"])
        if not source: raise ValueError("source action missing")
        if (a["service_key"] != "C:communication" or source.get("subsystem") != "C"
            or a["device_id"] != source.get("device_id") or a["coordinate"] != source.get("coordinate")):
            raise ValueError("conditional projection identity mismatch")
        vertical = deepcopy(source.get("vertical") or {})
        origin = vertical.get("service_origin_egm2008_m")
        if not isinstance(origin,(int,float)) or isinstance(origin,bool) or not math.isfinite(origin):
            raise ValueError("conditional service origin missing")
        metadata = dict(conditional_assumed_installation=True, hypothetical_action_id=a["action_id"],
                        physical_site_id=a["physical_site_id"], real_activation_status="unknown")
        installed = dict(device_id=a["device_id"], device_instance_id=a["device_instance_id"],
            subsystem="C", status="active", service_model={"status":"missing_data"}, metadata=metadata)
        # Same catalog fallback semantics as the existing P16 projection.
        if source.get("device_service_key"): installed["service_key"] = source["device_service_key"]
        if source.get("device_type"): installed["type"] = deepcopy(source["device_type"])
        if source.get("distinct_site_id"): installed["distinct_site_id"] = source["distinct_site_id"]
        facility = dict(facility_id="research-conditional:"+a["device_instance_id"],
            site_id=a["physical_site_id"], coordinate=deepcopy(a["coordinate"]),
            vertical_profile=vertical, devices=[installed], status="active", metadata=metadata)
        for key in ("planning_origin", "distinct_site_id"):
            if source.get(key): facility[key]=deepcopy(source[key])
        if source.get("host"): facility["metadata"]["host"]=deepcopy(source["host"])
        conditional.append(facility)
    working["items"].extend(conditional); working["count"]=len(working["items"])
    layers = dict(policy=policy, activation_policy="H0", activation_baseline_status="unknown",
        installed_inventory_status="unknown", qualification_status="unknown",
        baseline_facilities=deepcopy(baseline), conditional_facilities=deepcopy(conditional),
        unknown_inventory=None, baseline_is_real_installed_inventory=False)
    return working, layers


def inspect_real_state(state_bytes, frozen):
    state = _decode(state_bytes, STATE_SHA)
    if len(state_bytes) != 281900343: raise ValueError("rev503 size mismatch")
    route = frozen["scenario"]["route_scenario"]
    actual = [r for r in state.get("operational_routes",[]) if r.get("route_id")=="R0005"]
    if len(actual)!=1 or actual[0].get("path") != route["path"]:
        raise ValueError("frozen route path mismatch")
    from .planning_evidence_service import aircraft_profile_with_evidence, device_catalog_with_evidence
    from .site_candidate_actions import candidate_actions
    # Resolve evidenced consumers only on disposable copies. No historical result is read.
    source = deepcopy(state)
    source["aircraft_profile"] = aircraft_profile_with_evidence(source)
    source["device_catalog"] = device_catalog_with_evidence(source)
    inputs = prepare_inputs(source, route_id="R0005", source_identity=dict(kind="real_rev503",
        state_sha256=STATE_SHA, scenario_sha256=SCENARIO_SHA,
        subset_fingerprint=SUBSET_SHA, envelope_fingerprint=frozen["envelope"]["envelope_fingerprint"]))
    sources = candidate_actions([dict(service_key="C:communication")], {}, {},
        source["device_catalog"], source.get("tower_colocation_candidates"))
    by_id = {a["action_id"]:a for a in sources}
    for a in frozen["actions"]:
        if a["action_id"] not in by_id: raise ValueError("projection source missing")
        project_facilities(source["existing_cns_facilities"],[a],by_id,policy=POLICY)
    inputs["projection_action_count"] = len(frozen["actions"])
    inputs["source_actions"] = by_id
    inputs["allowed_actions"] = deepcopy(frozen["actions"])
    return inputs


def _summary(value):
    """Retain scalar metrics/unknowns, omit voxel, event and ID detail arrays."""
    if isinstance(value, dict):
        return {k:_summary(v) for k,v in value.items() if not isinstance(v,list)}
    if isinstance(value,float) and not math.isfinite(value):
        raise ValueError("nonfinite observation")
    return value


def _c_observations(result, route_ids):
    rows=[]
    for route_id in route_ids:
        routes=[r for r in result.get("routes",[]) if r.get("route_id")==route_id]
        if len(routes)!=1: raise ValueError("missing or duplicate route output")
        subs=[s for s in routes[0].get("subsystems",[]) if s.get("subsystem")=="C"]
        if len(subs)!=1: raise ValueError("missing or duplicate C output")
        rows.append(dict(route_id=route_id, **_summary(subs[0])))
    return rows


def evaluate_c(inputs, selected, source_actions, *, budget, chain=None):
    """Evaluate copied inputs; real data must pass external safety/input preflight.

    A P14/P15 ``failed`` is a valid computed deficit, but stops the A0 batch.
    Unusable, absent or version-mismatched outputs never become observations.
    """
    if budget.stopped: raise EvaluationStopped(budget.stopped)
    result=dict(schema_version="research_real_c_evaluation.v1", status="blocked",
        source_identity=deepcopy(inputs["source_identity"]), algorithm_versions={"p14":"1.4","p15":"1.3"},
        projection_policy=POLICY, activation_policy="H0", unknown=deepcopy(UNKNOWN),
        excluded_unsupported=list(UNSUPPORTED), model_feasible=None,
        final_eligible=False, optimal_proven=False, scoped_c_corridor_status="unknown",
        scoped_predicate=None, observations=None, elapsed_ms=0.0,
        action_ids=[a["action_id"] for a in selected], blockers=list(inputs.get("blockers",[])))
    if result["blockers"]: return result
    if inputs["source_identity"].get("kind") != "synthetic_fixture":
        result["blockers"].append("verified_hard_isolation_launcher_unavailable")
        return result
    if _digest(inputs["state"]) != SYNTHETIC_FIXTURE_SHA:
        result["blockers"].append("unregistered_synthetic_fixture")
        return result
    state_routes=[r.get("route_id") for r in inputs["state"].get("operational_routes",[])]
    if not state_routes or inputs.get("route_ids") != state_routes:
        result["blockers"].append("route_scope_mismatch")
        return result
    if any(a.get("route_ids") != state_routes for a in selected):
        result["blockers"].append("action_route_scope_mismatch")
        return result
    allowed=inputs.get("allowed_actions")
    if allowed is not None and any(a not in allowed for a in selected):
        raise ValueError("action outside frozen seven")
    budget.consume(selected)
    started=time.perf_counter()
    try:
        working=deepcopy(inputs["state"])
        facilities,layers=project_facilities(working["existing_cns_facilities"],selected,source_actions,policy=POLICY)
        # Restrict the research consumer request to unchanged C requirements.
        required=working["required_cns"]
        working["required_cns"]={"status":required.get("status"),
            "project_default":{"communication":deepcopy(required.get("project_default",{}).get("communication",{}))},
            "route_overrides":{rid:{"communication":deepcopy(v["communication"])}
                for rid,v in required.get("route_overrides",{}).items() if "communication" in v}}
        from ..algorithms.corridor.v1 import CNSServiceCorridorV1
        from ..algorithms.corridor_gap.v1 import CNSCorridorGapAnalyzerV1
        from .corridor_site_planning_service import rerun_corridor_chain
        if CNSServiceCorridorV1.algorithm_version!="1.4" or CNSCorridorGapAnalyzerV1.algorithm_version!="1.3":
            raise ValueError("runtime algorithm version mismatch")
        p14,p15=(chain or rerun_corridor_chain)(working,CNSServiceCorridorV1(),CNSCorridorGapAnalyzerV1(),facilities)
        for output,version in [(p14,"1.4"),(p15,"1.3")]:
            if output.get("algorithm_version")!=version or output.get("status") not in ("passed","failed","pending_confirmation"):
                raise ValueError("unusable P14/P15 status or version")
        observations=dict(p14=_c_observations(p14,inputs["route_ids"]),p15=_c_observations(p15,inputs["route_ids"]))
        statuses=[r.get("status") for group in observations.values() for r in group]
        scoped=("failed" if "failed" in statuses else "passed" if all(s=="passed" for s in statuses) else "unknown")
        result.update(status="evaluated_synthetic_fixture", observations=observations,
            scoped_c_corridor_status=scoped, projection_layers=layers,
            input_fingerprints={"p14":p14.get("input_fingerprint"),"p15":p15.get("input_fingerprint")})
        if any(o.get("status")=="failed" for o in (p14,p15)): budget.stop("computed_failure_stop")
    except Exception as exc:
        budget.stop("evaluation_failed")
        result.update(status="evaluation_failed", error_type=type(exc).__name__, error=str(exc))
    result["elapsed_ms"]=(time.perf_counter()-started)*1000
    size=len(json.dumps(result,ensure_ascii=False,allow_nan=False,separators=(",",":")).encode("utf-8"))
    # Failed evaluations already stop the batch; count their audit bytes as well.
    previous_stop=budget.stopped
    budget.stopped=None
    try:
        budget.record_output(size)
    finally:
        budget.stopped=budget.stopped or previous_stop
    return result
