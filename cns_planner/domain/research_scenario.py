"""Bounded offline research descriptors. No model, file or state access."""
from copy import deepcopy
import json
import math
import re
from .joint_feasibility import digest, seal, verify

TRACK = "engineering_research_conditional"
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_NODES = 50000
MAX_DEPTH = 32
MAX_STRING = 16384
FORBIDDEN = {"runtime", "module_path", "callback", "script", "pickle", "factory", "execute", "function", "callable"}


def bounded_json(value):
    """Accept built-in finite JSON only, before copying/hashing recursive inputs."""
    count = 0
    encoded_budget = 0
    active = set()
    def visit(v, depth):
        nonlocal count, encoded_budget
        count += 1
        # Charge JSON delimiters conservatively while traversing. Never materialize
        # an unbounded serialized value just to discover that it exceeds the cap.
        if type(v) is str:
            encoded_budget += len(v.encode("utf-8")) + 3 + sum(
                1 if c in '\\"' else 5 if ord(c) < 32 else 0 for c in v)
        elif type(v) in (int, float, bool) or v is None:
            encoded_budget += len(str(v)) + 1
        else:
            encoded_budget += 3
        if encoded_budget > MAX_JSON_BYTES: raise ValueError("JSON byte limit")
        if count > MAX_NODES or depth > MAX_DEPTH:
            raise ValueError("JSON resource limit")
        t = type(v)
        if v is None or t is bool: return
        if t is int:
            if v.bit_length() > 256: raise ValueError("JSON integer limit")
            return
        if t is float and math.isfinite(v): return
        if t is str:
            if len(v) > MAX_STRING: raise ValueError("JSON string limit")
            return
        if t not in (dict, list): raise ValueError("finite JSON required")
        if id(v) in active: raise ValueError("cyclic JSON")
        active.add(id(v))
        if t is dict:
            for k, item in v.items():
                if type(k) is not str or k in FORBIDDEN: raise ValueError("JSON execution field")
                visit(k, depth + 1); visit(item, depth + 1)
        else:
            for item in v: visit(item, depth + 1)
        active.remove(id(v))
    visit(value, 0)
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_JSON_BYTES: raise ValueError("JSON byte limit")
    return deepcopy(value)


def sha256_text(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def has_synthetic(value):
    if type(value) is dict:
        if (value.get("evidence_track") == "synthetic_fixture" or value.get("synthetic_fixture") is True or
                value.get("equipment_selection_status") == "synthetic_fixture_selected"):
            return True
        return any(has_synthetic(v) for v in value.values())
    return type(value) is list and any(has_synthetic(v) for v in value)


def required(row, fields, errors, label):
    if type(row) is not dict:
        errors.append(label + ":object_required"); return False
    for key in fields:
        if row.get(key) is None or row.get(key) == "": errors.append(label + ":missing:" + key)
    return True


def checked_scenario(scenario):
    sc = bounded_json(scenario)
    verify(sc, "scenario_fingerprint")
    rebuilt = validate_research_scenario(regional_bundle=sc["regional_bundle"],
        route_scenario=sc["route_scenario"], policies=sc["policies"])
    if sc != rebuilt: raise ValueError("scenario canonical identity mismatch")
    return sc


def validate_research_scenario(*, regional_bundle: dict, route_scenario: dict, policies: list[dict]) -> dict:
    bundle, route, policies = bounded_json([regional_bundle, route_scenario, policies])
    if type(bundle) is not dict or type(route) is not dict or type(policies) is not list:
        raise ValueError("bundle/route/policies shape")
    if any(type(p) is not dict for p in policies): raise ValueError("policy objects required")
    errors, gaps = [], []
    required(bundle, ["bundle_id", "version", "sources"], errors, "bundle")
    required(route, ["scenario_id", "version", "region_bundle_ref", "baseline_ref", "route_id",
        "endpoint_refs", "path", "path_crs", "path_locator", "path_kind", "path_sha256", "operating_layer_id",
        "altitude_value", "altitude_units", "vertical_reference", "phase", "scope", "route_evidence_refs",
        "policy_refs", "full_required_snapshot_ref", "consumer_dependencies"], errors, "route")
    for field in ("region_bundle_ref", "baseline_ref", "route_evidence_refs", "full_required_snapshot_ref"):
        if type(route.get(field)) is not dict: raise ValueError(field + ":object_required")
    if type(route.get("policy_refs")) is not list: errors.append("policy_references_list_required")
    if route.get("evidence_track") != TRACK or has_synthetic([bundle, route, policies]):
        errors.append("conditional_track_required_no_synthetic_relabeling")
    if route.get("region_bundle_ref") != {"bundle_id":bundle.get("bundle_id"), "version":bundle.get("version")}:
        errors.append("regional_bundle_reference_mismatch")
    path = route.get("path")
    if (type(path) is not list or len(path) < 2 or any(type(p) is not list or len(p) != 2 or
            any(type(x) not in (int, float) for x in p) or not (-180 <= p[0] <= 180 and -90 <= p[1] <= 90) for p in path)):
        errors.append("invalid_route_path")
    elif route.get("path_sha256") != digest(path): errors.append("route_path_fingerprint_mismatch")
    if route.get("path_crs") != "OGC:CRS84": errors.append("unsupported_route_crs")
    if (type(route.get("altitude_value")) not in (int, float) or route.get("altitude_units") != "m" or
            route.get("vertical_reference") != "egm2008_orthometric"):
        errors.append("explicit_fixed_egm2008_height_required")
    if type(route.get("endpoint_refs")) is not list or len(route.get("endpoint_refs", [])) != 2:
        errors.append("two_endpoint_references_required")
    for field in ("baseline_ref", "full_required_snapshot_ref"):
        row = route.get(field)
        if not required(row, ["sha256", "source_ref"], errors, field) or not sha256_text((row or {}).get("sha256")):
            errors.append(field + ":invalid_sha256")
    sources, source_map = bundle.get("sources"), {}
    if type(sources) is not list: sources = []; errors.append("sources_list_required")
    for row in sources:
        if not required(row, ["source_id", "locator", "version", "sha256", "bytes", "horizontal_crs", "axis_order",
                "vertical_reference", "validity", "source_ref", "license_status", "reader_version", "processing_lineage", "role"], errors, "source"):
            continue
        sid = row.get("source_id")
        if type(sid) is not str or sid in source_map: errors.append("duplicate_or_invalid_source_id"); continue
        source_map[sid] = row
        if not sha256_text(row.get("sha256")) or type(row.get("bytes")) is not int or row["bytes"] < 0:
            errors.append("invalid_source_identity:" + sid)
        if row.get("role") not in ("computational", "display_only_reference_layer"):
            errors.append("invalid_source_role:" + sid)
        if row.get("evidence_class") != "A": errors.append("real_source_class_required:" + sid)
        if row.get("horizontal_crs") == "unknown" or row.get("vertical_reference") == "unknown":
            gaps.append("source_reference_unknown:" + sid)
    register = {k:[] for k in "ABCD"}
    parameters, refs = {}, []
    for policy in policies:
        if not required(policy, ["scenario_id", "policy_id", "policy_version", "entries"], errors, "policy"): continue
        if policy.get("scenario_id") != route.get("scenario_id"): errors.append("policy_scenario_mismatch")
        ref = {k:policy.get(k) for k in ("policy_id", "policy_version")}
        if ref in refs: errors.append("duplicate_policy_identity")
        refs.append(ref)
        if type(policy.get("entries")) is not list: errors.append("policy_entries_list_required"); continue
        for entry in policy["entries"]:
            if not required(entry, ["parameter_id", "evidence_class", "source_ref", "units", "confidence", "evidence_tier",
                    "validity", "consumer", "policy_version", "applicability"], errors, "parameter"): continue
            pid = entry.get("parameter_id"); kind = entry.get("evidence_class")
            if type(pid) is not str or pid in parameters or type(kind) is not str or kind not in register:
                errors.append("duplicate_parameter_or_invalid_class"); continue
            parameters[pid] = entry
            if entry.get("policy_version") != policy.get("policy_version"): errors.append("parameter_policy_version_mismatch")
            if "value" not in entry or "range" not in entry: errors.append("explicit_value_and_range_required:" + pid)
            if entry.get("value") is not None and entry.get("range") is not None: errors.append("value_range_conflict:" + pid)
            if kind == "B" and not (entry.get("method_ref") and entry.get("method_version")):
                errors.append("proxy_method_required:" + pid)
            if kind == "D" or (kind == "C" and not (entry.get("approval_ref") and entry.get("validity") == "approved")):
                for field in ("value", "range"):
                    if entry.get(field) is not None: entry["declared_" + field] = entry[field]
                    entry[field] = None
                entry["missing_reason"] = entry.get("missing_reason") or "unknown_or_unapproved"
            if entry.get("value") is None and entry.get("range") is None:
                gaps.append("parameter_unknown:" + pid)
                if not entry.get("missing_reason"): errors.append("missing_reason_required:" + pid)
            register[kind].append(entry)
    if route.get("policy_refs") != refs: errors.append("policy_references_mismatch")
    dependencies = route.get("consumer_dependencies")
    if type(dependencies) is not list or not dependencies:
        errors.append("consumer_dependencies_required"); dependencies = []
    consumer_fps, used_parameters = {}, set()
    for dep in dependencies:
        if not required(dep, ["consumer_id", "model_version", "source_ids", "parameter_ids"], errors, "dependency"): continue
        cid = dep.get("consumer_id")
        if type(cid) is not str or cid in consumer_fps: errors.append("duplicate_consumer_id"); continue
        ids, pids = dep.get("source_ids"), dep.get("parameter_ids")
        if type(ids) is not list or type(pids) is not list or any(type(x) is not str for x in ids+pids):
            errors.append("dependency_ids_required"); continue
        selected, values = [], []
        for sid in ids:
            src = source_map.get(sid)
            if not src or src.get("role") == "display_only_reference_layer":
                errors.append("missing_or_display_computational_source:" + sid)
            else: selected.append(src)
        for pid in pids:
            entry = parameters.get(pid)
            if not entry or entry.get("consumer") != cid: errors.append("parameter_consumer_mismatch:" + pid)
            else: values.append(entry); used_parameters.add(pid)
        consumer_fps[cid] = digest(dict(model_version=dep.get("model_version"), sources=selected, parameters=values))
    if set(parameters) != used_parameters: errors.append("parameter_dependency_closure_incomplete")
    # Bundle identity is an audit identity. It must never become the CNS cache key.
    computational_route = {k:v for k,v in route.items() if k not in ("region_bundle_ref", "policy_refs", "version", "scenario_id")}
    comp = digest(dict(route=computational_route, consumers=consumer_fps,
        policies=[{k:p.get(k) for k in ("policy_id", "policy_version")} for p in policies]))
    return seal(dict(schema_version="research_scenario.v1", evidence_track=TRACK,
        execution_enabled=False, status="blocked" if errors else "static_contract_passed",
        blockers=sorted(set(errors)), evidence_gaps=sorted(set(gaps)), regional_bundle=bundle,
        route_scenario=route, policies=policies, evidence_register=register,
        source_bundle_fingerprint=digest(bundle), consumer_dependency_fingerprints=consumer_fps,
        computational_dependency_fingerprint=comp), "scenario_fingerprint")
