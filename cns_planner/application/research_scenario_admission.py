"""Static research inspection and explicit bounded path checks; never an executor."""
import hashlib
from copy import deepcopy
from pathlib import Path
import stat
from ..domain.joint_feasibility import digest, verify, seal
from ..domain.research_scenario import bounded_json, checked_scenario, sha256_text, MAX_JSON_BYTES
from .research_scenario_builder import build_conditional_domain


PROTECTED_PARTS = {".git", "projects", ".acceptance_setup", "local_recovery_20261018_100555", ".cns-results"}
UNSUPPORTED_ACTIONS = {"S:radar_noncooperative", "N:navigation_integrity_monitoring"}
ALLOWED_ACTION_FAMILIES = {"C:communication", "S:rid_cooperative",
                           "N:navigation_integrity_monitoring", "N:rtk_augmentation"}
EXECUTION_UNSUPPORTED_FAMILIES = {"S:radar_noncooperative", "S:rid_cooperative",
                                  "N:navigation_integrity_monitoring"}
REQUIRED_EXECUTION_BLOCKERS = (
    "device_qualification_unknown",
    "installed_inventory_unknown",
    "device_capacity_unknown",
    "cost_unknown",
    "research_execution_not_authorized",
)


def _plain_path(value):
    if type(value) is not str or not value or "\x00" in value: raise ValueError("explicit path required")
    p = Path(value)
    if not p.is_absolute() or ".." in p.parts or value.startswith(("\\\\", "//")):
        raise ValueError("absolute local path without traversal required")
    # Reject NTFS alternate data streams and noncanonical drive-relative paths.
    if any(":" in part for part in p.parts[1:]): raise ValueError("alternate stream forbidden")
    for ancestor in reversed([p, *p.parents]):
        try: st = ancestor.lstat()
        except FileNotFoundError: continue
        if stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & 0x400:
            raise ValueError("symlink/junction/reparse path forbidden")
    return p


def _controlled_path(value, allowed_roots):
    p = _plain_path(value)
    if type(allowed_roots) not in (list, tuple) or not allowed_roots: raise ValueError("explicit allowed roots required")
    roots = [_plain_path(str(x)) for x in allowed_roots]
    if any(not x.is_dir() for x in roots): raise ValueError("allowed root must be an existing directory")
    resolved = p.resolve(strict=False)
    if not any(resolved.is_relative_to(x.resolve(strict=True)) for x in roots):
        raise ValueError("path outside allowed roots")
    return p, resolved


def validate_input_path(path, *, allowed_roots, expected_sha256, max_bytes=MAX_JSON_BYTES):
    """Read only an explicit small regular file; caller supplies trusted root/hash."""
    if not sha256_text(expected_sha256) or type(max_bytes) is not int or not 0 < max_bytes <= MAX_JSON_BYTES:
        raise ValueError("bounded file/hash required")
    p, resolved = _controlled_path(path, allowed_roots)
    before = p.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes: raise ValueError("bounded regular file required")
    h = hashlib.sha256(); size = 0
    with p.open("rb") as f:
        for chunk in iter(lambda:f.read(65536), b""):
            size += len(chunk)
            if size > max_bytes: raise ValueError("input size limit")
            h.update(chunk)
    _controlled_path(path, allowed_roots)
    after = p.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError("input changed during check")
    if h.hexdigest() != expected_sha256: raise ValueError("input SHA256 mismatch")
    return dict(resolved_path=str(resolved), sha256=h.hexdigest(), bytes=size, path_status="static_path_checked", execution_enabled=False)


def validate_output_root(path, *, allowed_roots, forbidden_roots=()):
    """Check a proposed root, without mkdir, open for writing or run authorization."""
    p, resolved = _controlled_path(path, allowed_roots)
    roots = [Path(x).resolve(strict=True) for x in allowed_roots]
    relative_parts = [part for root in roots if resolved.is_relative_to(root) for part in resolved.relative_to(root).parts]
    source_root = Path(__file__).resolve().parents[2]
    denied_roots = [source_root / "projects", source_root / ".git", source_root / ".acceptance_setup"]
    protected_ancestors = [part.casefold() for i,part in enumerate(p.parts)
        if not (i == 1 and part.casefold() == "projects")]
    if (any(part in PROTECTED_PARTS or part.startswith("local_recovery_") for part in protected_ancestors) or
            any(part.casefold() in PROTECTED_PARTS for part in relative_parts) or
            any(resolved.is_relative_to(x) or x.is_relative_to(resolved) for x in denied_roots)):
        raise ValueError("protected output path")
    for root in forbidden_roots:
        denied = _plain_path(str(root)).resolve(strict=False)
        if resolved.is_relative_to(denied) or denied.is_relative_to(resolved):
            raise ValueError("output overlaps protected source/state/recovery")
    if p.exists() and not p.is_dir(): raise ValueError("output root must be directory")
    return dict(resolved_path=str(resolved), path_status="static_path_checked", execution_enabled=False)


def _route_errors(route, evidence):
    """Return static contract errors, execution blockers and evidence gaps.

    ``snapshot_mode=stale_declared`` is an explicitly limited research input:
    the historical chain may be stale, but its identity must still be
    internally consistent and every degraded layer must be declared.
    """
    errors, execution, gaps = [], [], []
    refs = route.get("route_evidence_refs", {})
    expected = dict(route_id=route.get("route_id"), path_sha256=route.get("path_sha256"),
        operating_layer_id=route.get("operating_layer_id"), altitude_value=route.get("altitude_value"),
        vertical_reference=route.get("vertical_reference"), baseline_sha256=route.get("baseline_ref", {}).get("sha256"),
        validation_id=refs.get("validation_id"), adoption_id=refs.get("adoption_id"),
        projection_fingerprint=refs.get("projection_fingerprint"))
    if not all(v is not None and v != "" for v in expected.values()): errors.append("route_chain_references_missing")
    stale_declared = route.get("snapshot_mode") == "stale_declared"
    if stale_declared:
        declarations = route.get("snapshot_declarations")
        if type(declarations) is not dict:
            errors.append("stale_declaration_required")
        else:
            for name in ("operational", "validation", "adoption", "operating_layer"):
                row = declarations.get(name)
                if type(row) is not dict or row.get("status") != "stale" or not row.get("reason"):
                    errors.append("stale_declaration_incomplete:" + name)
    statuses = dict(operational={"passed"}, validation={"engineering_conditionally_accepted_candidate", "passed"},
        adoption={"published_engineering_demo", "adopted"}, operating_layer={"confirmed"})
    for name, allowed in statuses.items():
        row = evidence.get(name, {})
        if type(row) is not dict:
            errors.append("route_evidence_missing:" + name); continue
        for key, value in expected.items():
            if row.get(key) != value: errors.append("route_chain_mismatch:" + name + ":" + key)
        if not row.get("source_ref"): errors.append("route_evidence_source_missing:" + name)
        if stale_declared:
            if (row.get("status") != "stale" or row.get("current_applicability") != "stale" or
                    not row.get("stale_reason")):
                errors.append("stale_declaration_incomplete:" + name)
            execution.append("route_evidence_stale_declared:" + name)
        else:
            if (row.get("current_applicability") != "current" or row.get("status") not in allowed or
                    row.get("stale_reason")):
                errors.append("route_evidence_not_current:" + name)
    operational = evidence.get("operational", {})
    if (route.get("path_kind") != "operational" or type(operational) is not dict or
            operational.get("kind") != "layered_risk_aware_operational_route"):
        errors.append("operational_polyline_required")
    validation = evidence.get("validation", {})
    if type(validation) is dict and validation.get("use_scope") == "engineering_demo_only" and route.get("scope") != "engineering_demo_only":
        errors.append("engineering_demo_scope_cannot_be_promoted")
    if stale_declared:
        for name in ("operational", "validation", "adoption", "operating_layer"):
            observed = evidence.get(name, {}) if type(evidence.get(name)) is dict else {}
            if not observed.get("observed_source_status"):
                gaps.append("stale_layer_observed_status_missing:" + name)
    return errors, execution, gaps


def _scope_inventory(route, capabilities):
    scope = route.get("required_scope", {})
    if type(scope) is not dict: raise ValueError("required scope object required")
    inv = scope.get("required_constraint_inventory", [])
    included = scope.get("included_constraint_ids", [])
    excluded = scope.get("excluded_constraints", [])
    unconsumed = deepcopy(scope.get("unconsumed_requirements", []))
    if any(type(v) is not list for v in (inv, included, excluded, unconsumed)):
        raise ValueError("required scope lists required")
    errors, gaps = [], []
    ids = [r.get("constraint_id") for r in inv if type(r) is dict]
    if (not inv or len(ids) != len(inv) or any(type(x) is not str for x in ids) or
            len(set(ids)) != len(ids) or digest(inv) != route["full_required_snapshot_ref"]["sha256"]):
        errors.append("original_required_inventory_mismatch")
    excluded_ids = [r.get("constraint_id") for r in excluded if type(r) is dict]
    if (any(type(x) is not str for x in included) or len(included) != len(set(included)) or
            len(excluded_ids) != len(excluded) or len(excluded_ids) != len(set(excluded_ids)) or
            set(included) & set(excluded_ids) or set(included) | set(excluded_ids) != set(ids)):
        errors.append("required_scope_partition_incomplete")
    if excluded and (not scope.get("scope_approval_ref") or any(not r.get("reason") for r in excluded)):
        errors.append("explicit_scope_exclusion_required")
    unconsumed_ids = [r.get("constraint_id") for r in unconsumed if type(r) is dict]
    if len(unconsumed_ids) != len(unconsumed) or set(unconsumed_ids) - set(ids):
        errors.append("unconsumed_inventory_mismatch")
    original_by_id = {row["constraint_id"]:row for row in inv if type(row) is dict and type(row.get("constraint_id")) is str}
    if any(row != original_by_id.get(row.get("constraint_id")) for row in unconsumed if type(row) is dict):
        errors.append("unconsumed_requirement_content_mismatch")
    for row in inv:
        if type(row) is not dict: continue
        consumer = row.get("consumer")
        cid = row.get("constraint_id")
        # A supplied capability claim records a mapping; it cannot enable execution.
        consumers = capabilities.get("consumers", {})
        consumed = (type(consumer) is dict and consumer.get("consumer_id") in consumers and
            consumer.get("version") == consumers.get(consumer.get("consumer_id")))
        if type(consumer) is dict:
            consumer_id = consumer.get("consumer_id")
            if consumer_id not in consumers:
                gaps.append("required_consumer_unknown:" + str(cid))
            elif consumer.get("version") != consumers.get(consumer_id):
                gaps.append("required_consumer_version_mismatch:" + str(cid))
        is_1000 = row.get("service_key") == "C:communication" and row.get("max_gap_m") == 1000
        if (cid in included and not consumed) or is_1000:
            if cid not in unconsumed_ids: unconsumed.append(row); unconsumed_ids.append(cid)
        if is_1000: gaps.append("communication_1000m_unconsumed")
    return inv, included, excluded, unconsumed, errors, gaps


def inspect_research_admission(*, scenario: dict, conditional_domain: dict, route_evidence: dict,
                              consumer_capabilities: dict, run_intent: dict) -> dict:
    sc = checked_scenario(scenario)
    domain, evidence, caps, intent = bounded_json([conditional_domain, route_evidence, consumer_capabilities, run_intent])
    if any(type(v) is not dict for v in (domain, evidence, caps, intent)): raise ValueError("inspection objects required")
    if type(caps.get("consumers")) is not dict: raise ValueError("consumer mappings object required")
    verify(domain, "domain_fingerprint")
    errors = list(sc["blockers"]) + list(domain.get("blockers", []))
    if domain.get("scenario_fingerprint") != sc["scenario_fingerprint"]:
        errors.append("conditional_domain_scenario_mismatch")
    rebuilt = build_conditional_domain(parent_domain_review=domain["real_qualification_snapshot"], scenario=sc,
        assumption_registry=domain["assumption_registry"])
    if rebuilt != domain: errors.append("conditional_domain_canonical_mismatch")
    route = sc["route_scenario"]
    route_errors, route_execution, route_gaps = _route_errors(route, evidence)
    errors.extend(route_errors)
    execution_blockers = list(route_execution)
    inv, included, excluded, unconsumed, scope_errors, gaps = _scope_inventory(route, caps)
    errors.extend(scope_errors); gaps.extend(sc["evidence_gaps"]); gaps.extend(route_gaps)
    stale_declared = route.get("snapshot_mode") == "stale_declared"
    strict_action_contract = stale_declared or route.get("conditional_actions") is not None
    for a in domain.get("finite_actions", []):
        family = a.get("service_key")
        action_id = a.get("action_id")
        if strict_action_contract and not a.get("route_ids"):
            errors.append("action_route_relation_missing:" + str(action_id))
        if strict_action_contract and a.get("route_relation_status") == "blocked":
            errors.append("action_route_relation_blocked:" + str(action_id))
        if family not in ALLOWED_ACTION_FAMILIES:
            errors.append("unsupported_action_family:" + str(family))
        elif family in EXECUTION_UNSUPPORTED_FAMILIES:
            if stale_declared:
                execution_blockers.append("consumer_unsupported:" + str(family))
            elif family in UNSUPPORTED_ACTIONS:
                errors.append("unsupported_action_family:" + str(family))
    for row in unconsumed:
        if type(row) is dict and row.get("constraint_id"):
            execution_blockers.append("required_unconsumed:" + str(row["constraint_id"]))
    if stale_declared:
        execution_blockers.extend(REQUIRED_EXECUTION_BLOCKERS)
        if any(a.get("dependency_evidence_status") not in (None, "declared")
               for a in domain.get("finite_actions", [])):
            execution_blockers.append("action_dependency_evidence_unknown")
        execution_blockers.append("p16_native_action_route_ids_absent")
    if intent.get("mode") != "inspect": errors.append("r1_inspection_only")
    if intent.get("output_root") is not None: errors.append("output_authorization_unavailable_in_json")
    static_passed = not errors
    return seal(dict(schema_version="research_admission.v1", status="blocked" if errors else "static_contract_passed",
        static_contract_passed=static_passed, static_contract_blockers=sorted(set(errors)),
        execution_enabled=False, execution_authorization="not_authorized",
        execution_blockers=sorted(set(execution_blockers)), blockers=sorted(set(errors)),
        evidence_incomplete=True, evidence_gaps=sorted(set(gaps)),
        snapshot_mode=route.get("snapshot_mode"),
        route_evidence_status=("stale_declared_snapshot" if stale_declared and not route_errors
                               else "blocked" if route_errors else "static_chain_consistent"),
        scenario_fingerprint=sc["scenario_fingerprint"], domain_fingerprint=domain["domain_fingerprint"],
        source_bundle_fingerprint=sc["source_bundle_fingerprint"],
        computational_dependency_fingerprint=domain["computational_dependency_fingerprint"],
        action_contract=route.get("action_contract_stats"),
        required_inventory=inv, included_constraint_ids=included, excluded_constraints=excluded,
        unconsumed_requirements=unconsumed, original_required_scope=route.get("required_scope"),
        required_scope_status=("partition_complete" if not scope_errors else "blocked"),
        consumer_capabilities=caps, native_m2_result=intent.get("native_m2_result"),
        navigation_status="unknown", scoped_model_status="not_evaluated", complete_required_cns_status="unknown",
        complete_required_proof_status="not_proven",
        evidence_status="evidence_incomplete", independent_verification_status="not_verified",
        real_domain_proof_status="not_verified", final_eligible=False,
        legacy_model_binding_status="unsupported_conditional_binding", persisted_to_project_state=False), "admission_fingerprint")
