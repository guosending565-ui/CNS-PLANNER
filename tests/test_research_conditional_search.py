"""R2.3-A finite conditional lexicographic search tests.

All inputs below are invented micro fixtures. They are not R0005, not a real GIS
read, not a real device qualification/activation baseline, and not evidence that
any real conditional action is executable. The injected runtime is test code.
"""
from copy import deepcopy
from itertools import combinations
import importlib
import importlib.util
import time

import pytest

from cns_planner.domain.joint_feasibility import digest, seal
from cns_planner.domain.research_scenario import TRACK, validate_research_scenario


FIXTURE_TRACK = "synthetic_conditional_fixture"
CONTEXT_SCHEMA = "conditional_optimization_context.v2"
DOMAIN_SCHEMA = "conditional_research_domain.v2"
ALGORITHM_VERSION = "conditional-lexicographic.2.0"


def api():
    name = "cns_planner.algorithms.existing_site_lexicographic.v2"
    assert importlib.util.find_spec(name) is not None, "R2.3-A conditional search not implemented"
    return importlib.import_module(name)


# ---------------------------------------------------------------------------
# Fixture material: R1 scenario, conditional domain, v2 frozen envelope.
# ---------------------------------------------------------------------------


def source_row(**extra):
    row = dict(source_id="towers", locator="X:/registered/fixture-towers.json", version="v1",
        sha256="a" * 64, bytes=12, horizontal_crs="OGC:CRS84", axis_order="lon_lat",
        vertical_reference="egm2008_orthometric", validity="registered",
        source_ref="fixture:source:towers", license_status="registered_internal",
        reader_version="v1", processing_lineage=[], role="computational", evidence_class="A")
    row.update(extra)
    return row


def install_entry(site_id, *, policy_version="v1", evidence_class="C", approval_ref="review:fixture:1"):
    entry = dict(parameter_id="install-" + site_id,
        parameter_kind="installation_qualification", site_id=site_id,
        evidence_class=evidence_class, source_ref="fixture:policy:install",
        value="assumed_eligible", range=None, units="eligibility",
        confidence="unassessed", evidence_tier="declared_assumption",
        validity="approved" if evidence_class == "C" else "pending_review",
        consumer="conditional_search", policy_version=policy_version,
        applicability="conditional-fixture", approval_ref=approval_ref if evidence_class == "C" else None)
    if evidence_class != "C":
        entry["missing_reason"] = "installation qualification not approved"
    return entry


def coverage_entry(policy_version="v1"):
    return dict(parameter_id="c-coverage", parameter_kind="equipment_capability",
        evidence_class="B", source_ref="fixture:proxy:c-coverage",
        method_ref="fixture.proxy.c_coverage", method_version="1.0",
        value=0.85, range=None, units="ratio", confidence="model_default",
        evidence_tier="engineering_proxy", validity="model_scope_only",
        consumer="conditional_search", policy_version=policy_version,
        applicability="conditional-fixture")


def provider_entry(policy_version="v1"):
    return dict(parameter_id="provider-inventory", parameter_kind="operation_segment_evidence",
        evidence_class="D", source_ref="fixture:unknown:provider",
        value=None, range=None, units="status", confidence="unassessed",
        evidence_tier="declared_unknown", validity="pending_review",
        consumer="conditional_search", policy_version=policy_version,
        applicability="conditional-fixture",
        missing_reason="provider inventory/device/handover evidence unavailable")


def parent_domain(site_ids, *, real_activation=None, installed=None,
                  installed_status="known", extra_sites=()):
    activation = real_activation or {}
    rows = []
    for sid in sorted(set(site_ids) | set(extra_sites)):
        rows.append(dict(physical_site_id=sid,
            qualification=dict(status="needs_evidence", source_ref="fixture:real-qualification"),
            baseline_activation_status=activation.get(sid, "unknown"),
            source_ref="fixture:real-parent"))
    parent = dict(evidence_track="engineering_research",
        site_qualification_counts=dict(Q=0, U=len(rows), E=0),
        site_ledger=rows,
        installed_device_instances=deepcopy(installed or []),
        installed_inventory_status=installed_status)
    return parent


_MISSING = object()


def action_row(aid, site, *, device=None, instance=_MISSING, slot=None,
               family="C:communication", planner="omnidirectional_site",
               action_type="install_device", effect="new_instance",
               deps=(), source_class="C", refs=None):
    return dict(action_id=aid, physical_site_id=site, device_id=device or "DEV-" + aid,
        device_instance_id=("INST-" + aid if instance is _MISSING else instance),
        slot_id=slot if slot is not None else "SLOT-" + aid,
        service_key=family, planner_family=planner, action_type=action_type,
        device_effect=effect, dependency_action_ids=list(deps),
        source_class=source_class,
        assumption_refs=(list(refs) if refs is not None else ["install-" + site]),
        applicable_models=["conditional_search"])


def scenario_raw(actions, entries, *, altitude=100, route_id="R1", path=None,
                 source_sha="a" * 64, policy_version="v1", unconsumed_ids=None):
    path = path or [[122.0, 30.0], [122.05, 30.05], [122.1, 30.1]]
    inventory = [
        dict(constraint_id="C:communication:max_gap_m", service_key="C:communication",
             max_gap_m=1000, hard=True, source_ref="fixture:original-required",
             consumer=dict(consumer_id="conditional_search", version="conditional-fixture.v1")),
        dict(constraint_id="N:rtk_augmentation", service_key="N:rtk_augmentation",
             kind="engineering_geometry", hard=True, source_ref="fixture:original-required",
             consumer=dict(consumer_id="conditional_search", version="conditional-fixture.v1")),
    ]
    unconsumed = ([row for row in inventory if row["constraint_id"] in unconsumed_ids]
                  if unconsumed_ids is not None else deepcopy(inventory))
    route = dict(scenario_id=route_id + "-scenario", version="v1", evidence_track=TRACK,
        region_bundle_ref=dict(bundle_id="shared-region", version="v1"),
        baseline_ref=dict(revision=1, sha256="b" * 64, source_ref="fixture:baseline"),
        route_id=route_id, endpoint_refs=["N005", "N006"], path=deepcopy(path),
        path_crs="OGC:CRS84", path_locator="fixture:operational", path_kind="operational",
        path_sha256=digest(path), operating_layer_id="ALT-080",
        altitude_value=altitude, altitude_units="m",
        vertical_reference="egm2008_orthometric", phase="cruise",
        scope="engineering_demo_only",
        route_evidence_refs=dict(validation_id="LRV-FIXTURE", adoption_id="LRA-FIXTURE",
            projection_fingerprint="proj-fixture"),
        policy_refs=[dict(policy_id="qualification", policy_version=policy_version)],
        full_required_snapshot_ref=dict(sha256=digest(inventory),
            source_ref="fixture:requirements"),
        consumer_dependencies=[dict(consumer_id="conditional_search",
            model_version="conditional-fixture.v1", source_ids=["towers"],
            parameter_ids=[entry["parameter_id"] for entry in entries])],
        requested_actions=deepcopy(actions),
        required_scope=dict(required_constraint_inventory=deepcopy(inventory),
            included_constraint_ids=[row["constraint_id"] for row in inventory],
            excluded_constraints=[], unconsumed_requirements=unconsumed))
    bundle = dict(bundle_id="shared-region", version="v1",
        sources=[source_row(sha256=source_sha)])
    policies = [dict(scenario_id=route["scenario_id"], policy_id="qualification",
        policy_version=policy_version, entries=deepcopy(entries))]
    return dict(regional_bundle=bundle, route_scenario=route, policies=policies)


def default_cost_basis(prices, *, shared=None):
    items = [dict(work_item_id="wi-" + aid, action_ids=[aid], quantity=1,
        unit_price=prices[aid], source_ref="fixture:price") for aid in sorted(prices)]
    for index, (aids, price) in enumerate(shared or []):
        items.append(dict(work_item_id="shared-" + str(index), action_ids=list(aids),
            quantity=1, unit_price=price, source_ref="fixture:shared-price"))
    return dict(unit="fixture_credit", pricing_date="2026-10-18", tax="included",
        discount_rate=0, horizon_years=1, coverage=["device", "shared"],
        quantity_basis="work_item", source_ref="fixture:cost-basis",
        uncertainty="exact", work_items=items)


def objective_policy(cost_basis):
    return dict(schema_version="existing_site_objective_policy.v1",
        decision_id="DEC-M5-ACTIVATION-001", model_version="1.0", version="v1",
        source_ref="fixture:objective-policy",
        prediction_policy_ref="fixture:prediction-policy",
        dependency_fingerprint=digest(dict(fixture="dependency")),
        activation_policy="conditional_research_only", cost_basis=cost_basis,
        combination_constraints=[])


def default_navigation():
    return dict(provider_inventory_status="unknown", device_status="satisfied",
        handover_status="unknown", speed_profile_status="unknown",
        backup_integrity_status="unknown", source_ref="fixture:navigation")


def build_fixture(actions, *, baselines=None, real_activation=None, installed=None,
                  installed_status="known", prices=None, shared=None, cost_basis=True,
                  altitude=100, route_id="R1", path=None, source_sha="a" * 64,
                  policy_version="v1", unknown_sites=(), no_entry_sites=(),
                  unsupported_families=(), unsupported_planners=(),
                  navigation=None, unconsumed_ids=None,
                  pruning_pairs=(), max_selected_actions=None, parent_extra_sites=(),
                  budget_audit=None, evaluator_version="conditional-fixture.v1",
                  fixture_block=True):
    actions = sorted(deepcopy(actions), key=lambda row: row["action_id"])
    sites = sorted({row["physical_site_id"] for row in actions})
    baselines = baselines or {sid: ("unknown" if sid in unknown_sites else "inactive")
                               for sid in sites}
    entries = []
    for sid in sites:
        if sid in no_entry_sites:
            continue
        entries.append(install_entry(sid, policy_version=policy_version,
            evidence_class="D" if sid in unknown_sites else "C"))
    entries.append(coverage_entry(policy_version))
    entries.append(provider_entry(policy_version))
    auto_installed = deepcopy(installed or [])
    seen_instances = {row["device_instance_id"] for row in auto_installed}
    for row in actions:
        if row["device_effect"] == "reconfigure_existing":
            if row.get("device_instance_id") not in seen_instances:
                auto_installed.append(dict(
                    device_instance_id=row["device_instance_id"],
                    physical_site_id=row["physical_site_id"],
                    device_id=row["device_id"], slot_id=row.get("slot_id")))
                seen_instances.add(row["device_instance_id"])
    parent = parent_domain(sites, real_activation=real_activation, installed=auto_installed,
        installed_status=installed_status, extra_sites=parent_extra_sites)
    scenario = validate_research_scenario(**scenario_raw(actions, entries,
        altitude=altitude, route_id=route_id, path=path, source_sha=source_sha,
        policy_version=policy_version, unconsumed_ids=unconsumed_ids))
    from cns_planner.application.research_scenario_builder import build_conditional_domain
    conditional = build_conditional_domain(parent_domain_review=parent, scenario=scenario,
        assumption_registry=scenario["policies"][0]["entries"])
    conditional_sites = []
    for row in conditional["conditional_qualification"]:
        sid = row["physical_site_id"]
        qualified = row["status"] == "conditional_qualified_under_policy"
        baseline = baselines.get(sid, "inactive" if qualified else "unknown")
        conditional_sites.append(dict(physical_site_id=sid,
            conditional_qualification="qualified_under_policy" if qualified else "unknown",
            conditional_activation_baseline=baseline,
            baseline_source_ref=("fixture:conditional-baseline:" + sid
                                 if baseline != "unknown" else None),
            baseline_assumption_refs=[row["parameter_id"]] if qualified else [],
            missing_reason=(None if baseline != "unknown"
                            else "conditional activation baseline not approved")))
    if prices is None:
        prices = {row["action_id"]: 10 + index for index, row in enumerate(actions)}
    policy = objective_policy(default_cost_basis(prices, shared=shared) if cost_basis else None)
    route = scenario["route_scenario"]
    proof_scope = dict(kind="frozen_model_action_domain",
        domain_scope=dict(fixture="conditional-finite", conditional_track=True,
            action_count=len(actions),
            conditional_domain_fingerprint=conditional["domain_fingerprint"]),
        constraint_scope=dict(objective_policy_fp=digest(policy),
            required_inventory_fp=digest(route["required_scope"]["required_constraint_inventory"])))
    context = seal(dict(schema_version=CONTEXT_SCHEMA, evidence_track=FIXTURE_TRACK,
        evaluator_version=evaluator_version, objective_policy=policy,
        proof_scope=proof_scope), "optimization_context_fp")
    binding = dict(consumers=[dict(consumer_id="conditional_search",
            method_ref="test:conditional_fixture_evaluator",
            method_version=evaluator_version)],
        required_inventory=deepcopy(route["required_scope"]["required_constraint_inventory"]),
        included_constraint_ids=deepcopy(route["required_scope"]["included_constraint_ids"]),
        excluded_constraints=deepcopy(route["required_scope"]["excluded_constraints"]),
        unconsumed_requirements=deepcopy(route["required_scope"]["unconsumed_requirements"]),
        unsupported_action_families=[dict(family=family, reason="unsupported_action_family",
            source_ref="fixture:consumer-capability") for family in unsupported_families],
        unsupported_planner_families=[dict(planner=planner, reason="unsupported_action_family",
            source_ref="fixture:consumer-capability") for planner in unsupported_planners],
        navigation_evidence=deepcopy(navigation or default_navigation()))
    enumeration = dict(kind="explicit_complete_finite", complete=True,
        action_count=len(actions), subset_count=2 ** len(actions),
        basis_ref="fixture:explicit-action-catalog")
    if len(actions) > 12 or budget_audit:
        enumeration["budget_audit"] = dict(leaf_evaluations=2 ** len(actions),
            node_upper_bound=2 ** (len(actions) + 1) - 1,
            basis_ref="fixture:budget-audit")
    if budget_audit is False:
        enumeration.pop("budget_audit", None)
    pruning = dict(incompatible_action_pairs=deepcopy(list(pruning_pairs)),
        structural_source_ref=("fixture:structural-rules" if pruning_pairs else None),
        max_selected_actions=max_selected_actions,
        max_selected_actions_source_ref=("fixture:explicit-action-limit"
                                          if max_selected_actions is not None else None))
    manifest = dict(fixture_id="r23a-conditional-micro", fixture_version="v1",
        evidence_track=FIXTURE_TRACK,
        scenario_fingerprint=scenario["scenario_fingerprint"],
        source_bundle_fingerprint=scenario["source_bundle_fingerprint"],
        computational_dependency_fingerprint=conditional["computational_dependency_fingerprint"],
        parent_real_domain_fp=digest(parent),
        objective_policy_fp=digest(policy), action_order_digest=digest(actions),
        route_id=route["route_id"], path_sha256=route["path_sha256"],
        altitude_value=route["altitude_value"], altitude_units=route["altitude_units"],
        vertical_reference=route["vertical_reference"],
        operating_layer_id=route["operating_layer_id"],
        proof_scope_fp=digest(proof_scope))
    fixture = (dict(fixture_id="r23a-conditional-micro", fixture_version="v1",
        evidence_track=FIXTURE_TRACK, manifest=manifest,
        fixture_sha256=digest(manifest)) if fixture_block else None)
    domain = seal(dict(schema_version=DOMAIN_SCHEMA, evidence_track=TRACK,
        execution_enabled=False, status="static_contract_passed",
        scenario=scenario, conditional_domain=conditional, real_parent_domain=parent,
        conditional_sites=conditional_sites,
        installed_inventory_status=installed_status,
        installed_device_instances=auto_installed, actions=actions,
        objective_policy=policy, consumer_binding=binding, enumeration=enumeration,
        pruning_rules=pruning, fixture=fixture,
        m3_independent_verification_status="not_verified", m6_status="closed"),
        "domain_fingerprint")
    return dict(domain=domain, context=context, actions=actions, scenario=scenario,
        conditional_domain=conditional, parent=parent, baselines=deepcopy(baselines),
        sites=sites, prices=deepcopy(prices), shared=deepcopy(shared or []),
        installed=auto_installed, installed_status=installed_status,
        cost_basis=policy["cost_basis"], navigation=binding["navigation_evidence"])


def action_map(fx):
    return {row["action_id"]: row for row in fx["actions"]}


def action_set_fingerprint(actions):
    rows = sorted(deepcopy(actions), key=lambda row: row["action_id"])
    return digest(dict(schema_version="candidate_action_set.v1", actions=rows))


class Runtime:
    """Trusted test runtime. It never imports or calls P14/P15/P17/Radar."""

    def __init__(self, fx, feasible, activation, *, unknown_sets=(), unknown_activation=(),
                 drop_site=None, invalid_actions=False):
        self.fx = fx
        self.feasible = feasible
        self.activation = activation
        self.unknown_sets = {frozenset(item) for item in unknown_sets}
        self.unknown_activation = {frozenset(item) for item in unknown_activation}
        self.drop_site = drop_site
        self.invalid_actions = invalid_actions
        self.calls = []
        self.now = 0.0
        self.memory = 1.0
        self.cancel = False
        self.after_call = None

    def evaluate_action_set(self, *, actions):
        ids = frozenset(row["action_id"] for row in actions)
        self.calls.append(ids)
        if self.after_call is not None:
            self.after_call()
        if ids in self.unknown_sets:
            status = "unknown"
        elif self.feasible(ids):
            status = "model_feasible"
        else:
            status = "unacceptable"
        predictions = {}
        if status == "model_feasible" and ids not in self.unknown_activation:
            active = self.activation(ids)
            for sid in sorted({row["physical_site_id"] for row in actions}):
                if sid == self.drop_site or sid not in active:
                    continue
                predictions[sid] = dict(
                    status=("predicted_active_under_conditional_model" if active[sid]
                            else "predicted_inactive_under_conditional_model"),
                    action_set_fp=action_set_fingerprint(actions),
                    domain_fingerprint=self.fx["domain"]["domain_fingerprint"],
                    model_version=self.fx["domain"]["objective_policy"]["model_version"],
                    prediction_policy_ref=self.fx["domain"]["objective_policy"]["prediction_policy_ref"],
                    dependency_fingerprint=self.fx["domain"]["objective_policy"]["dependency_fingerprint"],
                    source_ref="fixture:prediction",
                    conditions={key: dict(status="satisfied", source_ref="fixture:condition")
                                for key in ("power", "backhaul", "equipment", "qualification")})
        return dict(evidence_track=FIXTURE_TRACK,
            evaluator_version=self.fx["context"]["evaluator_version"],
            actions=deepcopy(actions), scoped_model_status=status,
            conditional_activation_predictions=predictions,
            participation_evidence=dict(status="unknown", physical_site_ids=[],
                source_ref="fixture:participation"))

    def evaluate_uncached_action_set(self, *, actions):
        return self.evaluate_action_set(actions=actions)

    def monotonic_seconds(self):
        return self.now

    def cancel_requested(self):
        return self.cancel

    def resident_memory_mb(self):
        return self.memory


def default_policy(**kw):
    policy = dict(algorithm_version=ALGORITHM_VERSION, order_policy="action_id",
        max_visited_nodes=20000, max_joint_replays=30000, max_elapsed_seconds=120,
        max_memory_mb=512, retain_feasible_limit=8192, max_storage_bytes=67108864)
    policy.update(kw)
    return policy


def run(fx, runtime, **kw):
    events = list(api().iter_conditional_lexicographic(
        frozen_conditional_domain=deepcopy(fx["domain"]),
        optimization_context=deepcopy(fx["context"]),
        search_policy=default_policy(**kw), runtime=runtime))
    return events[-1]["result"], events


def ids_of(row):
    return frozenset(action["action_id"] for action in row["actions"])


def tie_ids(layer):
    return {ids_of(row) for row in layer["ties"]}


# ---------------------------------------------------------------------------
# Independent oracle. It does not import the tested search, bounds or measurement.
# ---------------------------------------------------------------------------


def oracle_values(fx, selected, activation_rule):
    rows = [row for row in fx["actions"] if row["action_id"] in selected]
    baselines = fx["baselines"]
    sites = sorted({row["physical_site_id"] for row in rows})
    active = activation_rule(selected)
    activation = 0
    unknown_activation = False
    for sid in sites:
        baseline = baselines.get(sid, "unknown")
        if baseline == "active":
            continue
        if baseline != "inactive" or sid not in active:
            unknown_activation = True
            continue
        if active[sid]:
            activation += 1
    if unknown_activation:
        activation = None
    if any(row["device_effect"] == "unknown" for row in rows) or fx["installed_status"] != "known":
        devices = None
    else:
        devices = sum(1 for row in rows if row["device_effect"] == "new_instance")
    basis = fx["cost_basis"]
    if basis is None:
        cost = None
    else:
        cost = 0
        for item in basis["work_items"]:
            if selected & set(item["action_ids"]):
                cost += item["quantity"] * item["unit_price"]
    return (activation, devices, cost)


def oracle_layers(fx, feasible, activation_rule):
    names = [row["action_id"] for row in fx["actions"]]
    rows = []
    for size in range(len(names) + 1):
        for combo in combinations(names, size):
            selected = frozenset(combo)
            if not feasible(selected):
                continue
            rows.append((selected, oracle_values(fx, selected, activation_rule)))
    current = rows
    layers = {}
    for index, level in enumerate(("L1", "L2", "L3")):
        known = [row for row in current if row[1][index] is not None]
        if not known:
            layers[level] = (None, set())
            current = known
            continue
        best = min(row[1][index] for row in known)
        current = [row for row in known if row[1][index] == best]
        layers[level] = (best, {row[0] for row in current})
    return layers, rows


def covers_full_space(frontier, regions, n):
    intervals = []
    for prefix in list(frontier) + [row["prefix"] for row in regions]:
        lo = int(prefix or "0", 2) * 2 ** (n - len(prefix))
        intervals.append((lo, lo + 2 ** (n - len(prefix))))
    intervals.sort()
    cursor = 0
    for lo, hi in intervals:
        if lo != cursor:
            return False
        cursor = hi
    return cursor == 2 ** n


# ---------------------------------------------------------------------------
# Concrete fixtures.
# ---------------------------------------------------------------------------


def main_actions():
    return [action_row("a%d" % i, "T%d" % (i // 2 + 1), device="C%d" % i,
                       instance="I%d" % i) for i in range(12)]


def main_prices():
    prices = {("a%d" % i): 10 for i in range(3)}
    prices.update({("a%d" % i): 14 + i for i in range(3, 12)})
    prices["a10"], prices["a11"] = 50, 51
    return prices


def main_activation(selected):
    active = {}
    for site, first in (("T1", 0), ("T2", 2), ("T3", 4), ("T4", 6), ("T5", 8), ("T6", 10)):
        pair = {"a%d" % first, "a%d" % (first + 1)}
        active[site] = bool(pair & selected)
    return active


def main_feasible(selected):
    return 1 <= len(selected) <= 4 and not {"a10", "a11"} <= selected


def main_fixture():
    return build_fixture(main_actions(), prices=main_prices(),
        real_activation={"T1": "unknown", "T2": "active_verified", "T3": "unknown",
                         "T4": "inactive_verified", "T5": "unknown", "T6": "unknown"},
        pruning_pairs=[["a10", "a11"]])


def small_fixture():
    actions = [action_row("a%d" % i, "T%d" % (i + 1), device="C%d" % i,
                          instance="I%d" % i) for i in range(5)]
    prices = {"a0": 10, "a1": 10, "a2": 10, "a3": 20, "a4": 20}
    return build_fixture(actions, prices=prices,
        real_activation={"T1": "unknown", "T2": "unknown", "T3": "unknown",
                         "T4": "unknown", "T5": "unknown"})


def small_feasible(selected):
    return 1 <= len(selected) <= 2


def small_activation(selected):
    return {("T%d" % (i + 1)): (("a%d" % i) in selected) for i in range(5)}


# ---------------------------------------------------------------------------
# RED/GREEN: core oracle, layers, ties, unknown retention.
# ---------------------------------------------------------------------------


def test_small_fixture_matches_independent_oracle():
    fx = small_fixture()
    runtime = Runtime(fx, small_feasible, small_activation)
    result, _ = run(fx, runtime)
    expected, rows = oracle_layers(fx, small_feasible, small_activation)
    assert result["coverage_complete"] is True
    assert result["optimal_proven"] is True
    assert result["optimization_status"] == "conditional_lexicographic_optimal"
    assert {ids_of(row) for row in result["feasible_combinations"]} == {row[0] for row in rows}
    for level in ("L1", "L2", "L3"):
        best, ties = expected[level]
        layer = result["layers"][level]
        assert layer["upper_bound"] == best
        assert layer["lower_bound"] == best
        assert layer["status"] == "proven_optimal"
        assert layer["verification_status"] == "verified"
        assert tie_ids(layer) == ties


def test_twelve_action_fixture_matches_independent_oracle():
    fx = main_fixture()
    runtime = Runtime(fx, main_feasible, main_activation)
    start = time.monotonic()
    result, _ = run(fx, runtime)
    duration = time.monotonic() - start
    expected, rows = oracle_layers(fx, main_feasible, main_activation)
    assert duration <= 60, "12-action conditional fixture exceeded 60s: %.3f" % duration
    assert result["search_scope"]["action_count"] == 12
    assert result["search_scope"]["subset_count"] == 4096
    assert result["coverage_complete"] is True
    assert result["optimal_proven"] is True
    assert {ids_of(row) for row in result["feasible_combinations"]} == {row[0] for row in rows}
    for level in ("L1", "L2", "L3"):
        best, ties = expected[level]
        layer = result["layers"][level]
        assert layer["upper_bound"] == best, (level, layer["upper_bound"], best)
        assert tie_ids(layer) == ties, (level, tie_ids(layer), ties)
    assert result["layers"]["L1"]["upper_bound"] == 1
    assert result["layers"]["L2"]["upper_bound"] == 1
    assert result["layers"]["L3"]["upper_bound"] == 10
    # Every pruned region is independently expanded to its true complete leaves.
    ordered = [row["action_id"] for row in fx["actions"]]
    for region in result["pruned_regions"]:
        covered = [row for row in rows
                   if "".join("1" if name in row[0] else "0" for name in ordered)
                   .startswith(region["prefix"])]
        expanded = []
        for size in range(13):
            for combo in combinations(ordered, size):
                selected = frozenset(combo)
                if selected and "".join("1" if name in selected else "0" for name in ordered).startswith(region["prefix"]):
                    expanded.append(selected)
        assert expanded
        assert not any(main_feasible(selected) for selected in expanded)
    assert covers_full_space(result["checkpoint"]["frontier"], result["checkpoint"]["regions"], 12)
    assert result["metrics"]["visited_nodes"] <= 8191
    print("C3 conditional: reference_subsets=4096 reference_nodes=8191 search_nodes=%d actual=%d seconds=%.3f"
          % (result["metrics"]["visited_nodes"], result["metrics"]["actual_replays"], duration))


def test_single_action_without_benefit_but_combination_effective():
    actions = [action_row("a0", "T1", device="C0", instance="I0"),
               action_row("a1", "T1", device="C1", instance="I1"),
               action_row("a2", "T2", device="C2", instance="I2")]
    fx = build_fixture(actions, prices={"a0": 10, "a1": 11, "a2": 12})
    feasible = lambda selected: selected == frozenset({"a0", "a1"}) or selected == frozenset({"a2"})
    activation = lambda selected: {"T1": ({"a0", "a1"} <= selected),
                                   "T2": ("a2" in selected)}
    runtime = Runtime(fx, feasible, activation)
    result, _ = run(fx, runtime)
    combos = {ids_of(row) for row in result["feasible_combinations"]}
    assert frozenset({"a0", "a1"}) in combos
    assert frozenset({"a0"}) not in combos
    assert result["coverage_complete"] is True
    # The non-monotone prefix for a0 must not be pruned by a benefit bound.
    assert not any(region["prefix"] == "1" for region in result["pruned_regions"])


def test_l1_l2_l3_ties_and_equality_branches():
    fx = small_fixture()
    runtime = Runtime(fx, small_feasible, small_activation)
    result, _ = run(fx, runtime)
    assert result["layers"]["L1"]["prefix"] == {}
    assert result["layers"]["L2"]["prefix"] == {"activation": 1}
    assert result["layers"]["L3"]["prefix"] == {"activation": 1, "devices": 1}
    assert tie_ids(result["layers"]["L1"]) == {
        frozenset({"a0"}), frozenset({"a1"}), frozenset({"a2"}),
        frozenset({"a3"}), frozenset({"a4"})}
    assert tie_ids(result["layers"]["L2"]) == {
        frozenset({"a0"}), frozenset({"a1"}), frozenset({"a2"}),
        frozenset({"a3"}), frozenset({"a4"})}
    assert tie_ids(result["layers"]["L3"]) == {
        frozenset({"a0"}), frozenset({"a1"}), frozenset({"a2"})}
    assert result["incumbent"]["objective_measurement"]["values"]["cost"] == 10


def test_unknown_activation_count_is_retained_not_zero():
    fx = small_fixture()
    runtime = Runtime(fx, small_feasible, small_activation, drop_site="T4")
    result, _ = run(fx, runtime)
    assert result["unranked_model_feasible"]
    assert result["layers"]["L1"]["status"] != "proven_optimal"
    assert result["layers"]["L2"]["status"] != "proven_optimal"
    assert result["optimal_proven"] is False
    assert any(measurement["values"]["activation"] is None
               for measurement in [row["objective_measurement"]
                                   for row in result["unranked_model_feasible"]])


def test_unknown_device_count_is_not_action_count():
    actions = [action_row("a0", "T1", device="C0", instance=None, effect="unknown")]
    fx = build_fixture(actions, prices={"a0": 10})
    runtime = Runtime(fx, lambda selected: bool(selected),
                      lambda selected: {"T1": True})
    result, _ = run(fx, runtime)
    values = [row["objective_measurement"]["values"] for row in result["feasible_combinations"]]
    assert any(value["devices"] is None for value in values)
    assert result["layers"]["L1"]["status"] == "proven_optimal"
    assert result["layers"]["L2"]["status"] != "proven_optimal"
    assert result["incumbent"] is None
    assert result["optimal_proven"] is False


def test_unknown_cost_is_cost_unresolved_and_keeps_two_layers():
    actions = [action_row("a%d" % i, "T%d" % (i + 1), device="C%d" % i,
                          instance="I%d" % i) for i in range(5)]
    prices = {"a0": 10, "a1": 10, "a2": 10, "a3": 20, "a4": 20}
    fx = build_fixture(actions, prices=prices, cost_basis=False)
    runtime = Runtime(fx, small_feasible, small_activation)
    result, _ = run(fx, runtime)
    assert result["layers"]["L1"]["status"] == "proven_optimal"
    assert result["layers"]["L2"]["status"] == "proven_optimal"
    assert result["layers"]["L3"]["status"] == "cost_unresolved"
    assert result["layers"]["L3"]["upper_bound"] is None
    assert result["optimization_status"] == "conditional_first_two_levels_optimal"


def test_shared_cost_is_charged_once():
    actions = [action_row("a0", "T1", device="C0", instance="I0"),
               action_row("a1", "T1", device="C1", instance="I1")]
    fx = build_fixture(actions, prices={"a0": 10, "a1": 10},
                       shared=[(["a0", "a1"], 5)])
    runtime = Runtime(fx, lambda selected: bool(selected),
                      lambda selected: {"T1": True})
    result, _ = run(fx, runtime)
    by_ids = {ids_of(row): row["objective_measurement"]["values"]["cost"]
              for row in result["feasible_combinations"]}
    assert by_ids[frozenset({"a0"})] == 15
    assert by_ids[frozenset({"a1"})] == 15
    assert by_ids[frozenset({"a0", "a1"})] == 25


def test_same_tower_multi_device_and_addon_at_active_site():
    actions = [action_row("a0", "T1", device="C0", instance="I0"),
               action_row("a1", "T1", device="C1", instance="I1"),
               action_row("a2", "T2", device="C2", instance="I2")]
    fx = build_fixture(actions, prices={"a0": 10, "a1": 11, "a2": 12},
                       baselines={"T1": "inactive", "T2": "active"})
    runtime = Runtime(fx, lambda selected: bool(selected),
                      lambda selected: {"T1": True, "T2": True})
    result, _ = run(fx, runtime)
    by_ids = {ids_of(row): row["objective_measurement"]["values"]
              for row in result["feasible_combinations"]}
    assert by_ids[frozenset({"a0"})]["devices"] == 1
    assert by_ids[frozenset({"a1"})]["devices"] == 1
    assert by_ids[frozenset({"a0", "a1"})]["devices"] == 2
    assert by_ids[frozenset({"a0", "a1"})]["activation"] == 1
    assert by_ids[frozenset({"a2"})]["activation"] == 0
    assert by_ids[frozenset({"a2"})]["devices"] == 1


# ---------------------------------------------------------------------------
# Domain identity, scope binding, unsupported families and evidence gaps.
# ---------------------------------------------------------------------------


def test_unsupported_family_and_endpoint_actions_are_preserved_not_dropped():
    actions = [action_row("a0", "T1"),
               action_row("a1", "T2", family="S:radar_noncooperative",
                          planner="directional_radar"),
               action_row("a2", "T3", family="N:navigation_integrity_monitoring",
                          planner="endpoint_integrity_monitor"),
               action_row("a3", "T4", family="N:rtk_augmentation",
                          planner="navigation_reference_station", source_class="B")]
    fx = build_fixture(actions,
        unsupported_families=["S:radar_noncooperative", "N:navigation_integrity_monitoring"],
        unsupported_planners=["directional_radar", "endpoint_integrity_monitor"])
    runtime = Runtime(fx, lambda selected: 1 <= len(selected) <= 2,
                      lambda selected: {("T%d" % (i + 1)): ("a%d" % i in selected)
                                        for i in range(4)})
    result, _ = run(fx, runtime)
    assert result["search_scope"]["action_count"] == 4
    assert {row["action_id"] for row in result["excluded_actions"]} == {"a1", "a2"}
    for row in result["feasible_combinations"]:
        assert not ({"a1", "a2"} & ids_of(row))
    binding = result["proof_binding"]
    assert binding["unsupported_action_families"]
    assert binding["unsupported_planner_families"]
    assert result["layers"]["L1"]["proof_binding"] == binding


def test_unconsumed_1000m_and_rtk_gaps_stay_visible():
    fx = small_fixture()
    runtime = Runtime(fx, small_feasible, small_activation)
    result, _ = run(fx, runtime)
    assert result["communication_1000m_unconsumed"] is True
    assert result["navigation_status"] == "unknown"
    assert result["complete_required_cns_status"] == "unknown"
    assert result["real_domain_proof_status"] == "not_verified"
    assert result["source_authentication_status"] != "verified"
    binding = result["proof_binding"]
    assert any(row["constraint_id"] == "C:communication:max_gap_m"
               for row in binding["unconsumed_requirements"])
    assert binding["navigation_evidence"] is not None
    assert "communication_1000m_unconsumed" in result["evidence_gaps"]
    assert "navigation_evidence_incomplete" in result["evidence_gaps"]


def test_real_qualification_and_activation_are_separate_from_conditional():
    actions = [action_row("a0", "T1"), action_row("a1", "T2")]
    fx = build_fixture(actions, prices={"a0": 10, "a1": 11},
        baselines={"T1": "active", "T2": "inactive"},
        real_activation={"T1": "active_verified", "T2": "unknown"})
    runtime = Runtime(fx, lambda selected: bool(selected),
                      lambda selected: {"T1": True, "T2": True})
    result, _ = run(fx, runtime)
    by_ids = {ids_of(row): row["objective_measurement"]["values"]
              for row in result["feasible_combinations"]}
    assert by_ids[frozenset({"a0"})]["activation"] == 0
    assert by_ids[frozenset({"a1"})]["activation"] == 1
    real = {row["physical_site_id"]: row for row in result["real_activation_facts"]}
    assert real["T1"]["real_activation_status"] == "active_verified"
    assert real["T2"]["real_activation_status"] == "unknown"
    conditional = {row["physical_site_id"]: row for row in result["conditional_sites"]}
    assert conditional["T1"]["conditional_activation_baseline"] == "active"
    assert conditional["T2"]["conditional_activation_baseline"] == "inactive"
    assert result["real_qualification_snapshot"] == fx["parent"]
    assert result["measurement_basis"] == "conditional_site_baseline_projection"


def test_unlisted_sites_do_not_default_to_device_qualification():
    actions = [action_row("a0", "T1")]
    fx = build_fixture(actions, parent_extra_sites={"T2", "T3"},
                       real_activation={"T2": "unknown", "T3": "unknown"})
    assert {row["physical_site_id"] for row in fx["domain"]["conditional_sites"]} == {"T1"}
    runtime = Runtime(fx, lambda selected: bool(selected), lambda selected: {"T1": True})
    result, _ = run(fx, runtime)
    assert {row["physical_site_id"] for row in result["conditional_sites"]} == {"T1"}
    unlisted = build_fixture([action_row("a0", "T1")], no_entry_sites={"T1"},
                             parent_extra_sites={"T2", "T3"})
    assert unlisted["domain"]["conditional_sites"] == []
    with pytest.raises(ValueError):
        api().identity(unlisted["domain"], unlisted["context"])


def test_unknown_qualification_is_retained_not_defaulted():
    actions = [action_row("a0", "T1"), action_row("a1", "T2")]
    fx = build_fixture(actions, prices={"a0": 10, "a1": 11},
                       unknown_sites={"T2"})
    assert {row["physical_site_id"]: row["conditional_activation_baseline"]
            for row in fx["domain"]["conditional_sites"]} == {"T1": "inactive",
                                                              "T2": "unknown"}
    runtime = Runtime(fx, lambda selected: bool(selected),
                      lambda selected: {"T1": True, "T2": True})
    result, _ = run(fx, runtime)
    assert result["unknown_leaves"]
    assert result["layers"]["L1"]["status"] != "proven_optimal"
    assert result["optimal_proven"] is False
    assert any(row["physical_site_id"] == "T2" and
               row["conditional_qualification"] == "unknown"
               for row in result["conditional_sites"])


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "outside", "identity"])
def test_frozen_action_x_violations_are_rejected(mutation):
    fx = small_fixture()
    domain = deepcopy(fx["domain"])
    if mutation == "missing":
        domain["actions"].pop()
    elif mutation == "duplicate":
        domain["actions"][1]["action_id"] = domain["actions"][0]["action_id"]
    elif mutation == "outside":
        domain["actions"][0]["physical_site_id"] = "T-unknown"
    else:
        domain["actions"][0]["device_id"] = "CHANGED"
    domain = seal(domain, "domain_fingerprint")
    with pytest.raises(ValueError):
        api().identity(domain, fx["context"])


def test_proof_binds_scope_requirements_and_consumers():
    fx = small_fixture()
    runtime = Runtime(fx, small_feasible, small_activation)
    result, _ = run(fx, runtime)
    binding = result["proof_binding"]
    assert binding["required_inventory"] == fx["scenario"]["route_scenario"]["required_scope"]["required_constraint_inventory"]
    assert binding["included_constraint_ids"] == fx["scenario"]["route_scenario"]["required_scope"]["included_constraint_ids"]
    assert binding["excluded_constraints"] == fx["scenario"]["route_scenario"]["required_scope"]["excluded_constraints"]
    assert binding["unconsumed_requirements"] == fx["scenario"]["route_scenario"]["required_scope"]["unconsumed_requirements"]
    assert binding["consumers"][0]["consumer_id"] == "conditional_search"
    assert binding["binding_fp"] == digest({key: value for key, value in binding.items() if key != "binding_fp"})
    assert result["layers"]["L1"]["binding_fp"] == binding["binding_fp"]
    broken = deepcopy(fx["domain"])
    broken["consumer_binding"]["required_inventory"][0]["hard"] = False
    broken = seal(broken, "domain_fingerprint")
    with pytest.raises(ValueError):
        api().identity(broken, fx["context"])


def test_pruning_rules_require_explicit_source():
    actions = [action_row("a0", "T1"), action_row("a1", "T2")]
    fx = build_fixture(actions, prices={"a0": 10, "a1": 11})
    broken = deepcopy(fx["domain"])
    broken["pruning_rules"]["incompatible_action_pairs"] = [["a0", "a1"]]
    broken["pruning_rules"]["structural_source_ref"] = None
    broken = seal(broken, "domain_fingerprint")
    with pytest.raises(ValueError):
        api().identity(broken, fx["context"])


def test_source_policy_route_altitude_change_invalidates_proof():
    actions = [action_row("a0", "T1"), action_row("a1", "T2")]
    base = build_fixture(actions, prices={"a0": 10, "a1": 11})
    runtime = Runtime(base, lambda selected: bool(selected),
                      lambda selected: {"T1": "a0" in selected, "T2": "a1" in selected})
    result, events = run(base, runtime)
    proof = deepcopy(result["layers"]["L1"])
    receipt = api().verify_conditional_layer_proof(
        layer_proof=proof, frozen_conditional_domain=deepcopy(base["domain"]),
        optimization_context=deepcopy(base["context"]), runtime=runtime,
        verification_budget=dict(max_joint_replays=1000, max_elapsed_seconds=60,
                                 max_memory_mb=512))
    assert receipt["verification_status"] == "verified"
    changed = {
        "altitude": build_fixture(actions, prices={"a0": 10, "a1": 11}, altitude=150),
        "source": build_fixture(actions, prices={"a0": 10, "a1": 11},
                                source_sha="c" * 64),
        "policy": build_fixture(actions, prices={"a0": 10, "a1": 11},
                                policy_version="v2"),
        "route": build_fixture(actions, prices={"a0": 10, "a1": 11},
                               path=[[123.0, 31.0], [123.05, 31.05], [123.1, 31.1]]),
    }
    for name, fx in changed.items():
        receipt = api().verify_conditional_layer_proof(
            layer_proof=deepcopy(proof),
            frozen_conditional_domain=deepcopy(fx["domain"]),
            optimization_context=deepcopy(fx["context"]), runtime=runtime,
            verification_budget=dict(max_joint_replays=1000,
                                     max_elapsed_seconds=60, max_memory_mb=512))
        assert receipt["verification_status"] == "rejected", name
    assert base["domain"]["scenario"]["route_scenario"]["altitude_value"] == 100


# ---------------------------------------------------------------------------
# Budget, interruption, checkpoint and block behavior.
# ---------------------------------------------------------------------------


def fourteen_actions():
    return [action_row("a%d" % i, "T%d" % (i // 2 + 1), device="C%d" % i,
                       instance="I%d" % i) for i in range(14)]


def fourteen_fixture(**kw):
    actions = fourteen_actions()
    prices = {row["action_id"]: 10 + index for index, row in enumerate(actions)}
    return build_fixture(actions, prices=prices, **kw)


def fourteen_feasible(selected):
    return 1 <= len(selected) <= 4 and not {"a12", "a13"} <= selected


def fourteen_activation(selected):
    active = {}
    for index in range(14):
        site = "T%d" % (index // 2 + 1)
        active[site] = active.get(site, False) or (("a%d" % index) in selected)
    return active


def test_over_twelve_requires_budget_audit():
    fx = fourteen_fixture(budget_audit=False)
    with pytest.raises(ValueError, match="budget audit"):
        api().identity(fx["domain"], fx["context"])


def test_over_twelve_budget_stop_keeps_valid_incumbent_and_frontier():
    fx = fourteen_fixture()
    unknown_action = fx["domain"]["actions"][-1]["action_id"]
    runtime = Runtime(fx, fourteen_feasible, fourteen_activation,
                      unknown_sets=[(unknown_action,)])
    result, events = run(fx, runtime, max_visited_nodes=200,
                         max_joint_replays=1000)
    assert result["search_scope"]["action_count"] == 14
    assert result["search_scope"]["subset_count"] == 16384
    assert result["coverage_complete"] is False
    assert result["optimal_proven"] is False
    assert result["termination_reason"] == "node_budget"
    assert result["metrics"]["visited_nodes"] <= 200
    assert result["incumbent"] is not None
    assert result["checkpoint"]["frontier"]
    assert result["unknown_leaves"]
    assert result["unknown_leaf_details"]
    assert covers_full_space(result["checkpoint"]["frontier"],
                             result["checkpoint"]["regions"], 14)
    for level in ("L1", "L2", "L3"):
        assert result["layers"][level]["status"] != "proven_optimal"
        assert result["layers"][level]["verification_status"] != "verified"
    assert result["conditional_infeasible_proven"] is False
    assert result["search_scope"]["action_ids"] == [row["action_id"] for row in fx["actions"]]


def test_missing_runtime_is_blocked():
    fx = small_fixture()
    for runtime in (None, {}, object()):
        result, _ = run(fx, runtime)
        assert result["status"] == "blocked"
        assert result["optimization_status"] == "blocked"
        assert "runtime_unavailable" in result["blockers"]
        assert result["execution_enabled"] is False


def test_checkpoint_resume_revalidates_and_keeps_budget_cumulative():
    fx = small_fixture()
    runtime = Runtime(fx, small_feasible, small_activation)
    first, events = run(fx, runtime, max_visited_nodes=5)
    assert first["coverage_complete"] is False
    checkpoint = deepcopy(first["checkpoint"])
    resumed = list(api().iter_conditional_lexicographic(
        frozen_conditional_domain=deepcopy(fx["domain"]),
        optimization_context=deepcopy(fx["context"]),
        search_policy=default_policy(max_visited_nodes=1000), runtime=runtime,
        checkpoint=checkpoint))
    result = resumed[-1]["result"]
    assert result["coverage_complete"] is True
    assert result["optimal_proven"] is True
    assert result["metrics"]["visited_nodes"] >= first["metrics"]["visited_nodes"]
    with pytest.raises(ValueError):
        list(api().iter_conditional_lexicographic(
            frozen_conditional_domain=deepcopy(fx["domain"]),
            optimization_context=deepcopy(fx["context"]),
            search_policy=default_policy(max_visited_nodes=1), runtime=runtime,
            checkpoint=checkpoint))


@pytest.mark.parametrize("field,value", [("algorithm_version", "other"),
    ("order_policy", "cost"), ("max_joint_replays", -1),
    ("max_elapsed_seconds", 0), ("max_memory_mb", None),
    ("max_storage_bytes", 67108865), ("cap", 6)])
def test_strict_policy(field, value):
    fx = small_fixture()
    policy = default_policy(**{field: value})
    with pytest.raises(ValueError):
        list(api().iter_conditional_lexicographic(
            frozen_conditional_domain=fx["domain"],
            optimization_context=fx["context"], search_policy=policy,
            runtime=Runtime(fx, small_feasible, small_activation)))


# ---------------------------------------------------------------------------
# Isolation: no real model call, no old gate relaxation, no real proof upgrade.
# ---------------------------------------------------------------------------


def test_no_p14_p15_p17_radar_real_model_call(monkeypatch):
    calls = []

    def bomb(*args, **kwargs):
        calls.append(True)
        raise AssertionError("real model must not be called by R2.3-A")

    corridor = importlib.import_module(
        "cns_planner.algorithms.corridor.v1").CNSServiceCorridorV1
    gap = importlib.import_module(
        "cns_planner.algorithms.corridor_gap.v1").CNSCorridorGapAnalyzerV1
    continuous = importlib.import_module(
        "cns_planner.algorithms.continuous_service.v1").ContinuousServiceAcceptabilityV1
    radar = importlib.import_module("cns_planner.algorithms.radar_layout.v1")
    lookup = importlib.import_module("cns_planner.algorithms.radar_layout.terrain_los_lookup")
    monkeypatch.setattr(corridor, "evaluate", bomb)
    monkeypatch.setattr(gap, "evaluate", bomb)
    monkeypatch.setattr(continuous, "evaluate", bomb)
    monkeypatch.setattr(radar, "solve_layout", bomb)
    monkeypatch.setattr(lookup.TerrainLosLookup, "evaluate", bomb)
    fx = small_fixture()
    runtime = Runtime(fx, small_feasible, small_activation)
    result, _ = run(fx, runtime)
    assert result["coverage_complete"] is True
    assert calls == []
    assert result["native_model_calls"] == 0


def test_old_gates_are_unchanged():
    from cns_planner.algorithms.existing_site_lexicographic import v1
    from cns_planner.application.existing_site_optimization_evidence import build_readonly_run
    from cns_planner.application.research_evaluation_adapter import (
        evaluate_conditional_actions, M4_METHOD_VERSION)
    from cns_planner.application.research_run_service import (
        PRODUCTION_AUTHORIZATION_ENABLED, issue_production_authorization)
    fx = small_fixture()
    # v1 still requires its old identity/synthetic track/policy.
    with pytest.raises((ValueError, KeyError)):
        v1.identity(fx["domain"], fx["context"])
    with pytest.raises(ValueError):
        v1.checked_policy(default_policy())
    # R1 conditional scenario remains offline-only.
    assert fx["scenario"]["execution_enabled"] is False
    assert fx["scenario"]["evidence_track"] == TRACK
    # R2.1 still refuses non-fixture real inputs loudly.
    with pytest.raises(ValueError):
        evaluate_conditional_actions(frozen_inputs={}, actions=[], consumer_registry={})
    assert M4_METHOD_VERSION
    # R2.2 production authorization remains closed.
    assert PRODUCTION_AUTHORIZATION_ENABLED is False
    with pytest.raises(PermissionError):
        issue_production_authorization()
    # D1 still refuses real execution.
    d1 = build_readonly_run(domain_review={}, frozen_context={}, objective_policy={},
        scope_declaration={}, approved_run_spec={})
    assert d1["status"] == "blocked"
    assert any("real_execution_disabled_in_d1" in blocker for blocker in d1["blockers"])


def test_output_never_upgrades_conditional_proof_to_real_domain():
    fx = small_fixture()
    runtime = Runtime(fx, small_feasible, small_activation)
    result, _ = run(fx, runtime)
    assert result["optimal_proven"] is True
    assert result["proof_scope"]["kind"] == "frozen_model_action_domain"
    assert result["real_domain_proof_status"] == "not_verified"
    assert result["source_authentication_status"] == "not_authenticated"
    assert result["independent_verification_status"] == "not_verified"
    assert result["m3_independent_verification_status"] == "not_verified"
    assert result["m6_status"] == "closed"
    assert result["execution_enabled"] is False
    assert result["persisted_to_project_state"] is False
    assert result["final_eligible"] is False
    assert result["new_site_stage_unlocked"] is False
    assert result["fixture"]["evidence_track"] == FIXTURE_TRACK
    assert all(layer["verification_status"] == "verified"
               for layer in result["layers"].values())
    assert all(layer["proof_scope"]["kind"] == "frozen_model_action_domain"
               for layer in result["layers"].values())


def test_non_fixture_domain_is_blocked_without_identity_upgrade():
    fx = small_fixture()
    domain = deepcopy(fx["domain"])
    domain["fixture"] = None
    domain = seal(domain, "domain_fingerprint")
    result, _ = run({"domain": domain, "context": fx["context"]}, object())
    assert result["status"] == "blocked"
    assert "non_fixture_conditional_execution_not_authorized" in result["blockers"]
    assert result["real_domain_proof_status"] == "not_verified"


def test_verify_rejects_resealed_but_false_layer_claim():
    fx = small_fixture()
    runtime = Runtime(fx, small_feasible, small_activation)
    result, _ = run(fx, runtime)
    bad = deepcopy(result["layers"]["L1"])
    bad["upper_bound"] = -1
    bad = seal(bad, "proof_fp")
    receipt = api().verify_conditional_layer_proof(
        layer_proof=bad, frozen_conditional_domain=deepcopy(fx["domain"]),
        optimization_context=deepcopy(fx["context"]), runtime=runtime,
        verification_budget=dict(max_joint_replays=1000, max_elapsed_seconds=60,
                                 max_memory_mb=512))
    assert receipt["verification_status"] == "rejected"
