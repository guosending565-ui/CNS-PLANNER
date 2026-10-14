# Round38-N1 Navigation Monitoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only shared CNS site-candidate view and deterministic navigation-monitoring simulation, plus the smallest safe P17 unknown-time fix.

**Architecture:** Application services adapt authoritative hydrated project facts into stable candidate and route/risk identities; a pure Domain module performs metric interval, gap, timing, greedy selection, and redundancy calculations. `ApiRouter` exposes two lightweight calls without saving state or invoking the formal CNS pipeline. P17 remains a separate formal algorithm and advances from 2.1 to non-conflicting version 2.3.

**Tech Stack:** Python 3, existing `WorkflowService`/`ApiRouter`, `pyproj` at the Application boundary, pure-Python metric geometry, pytest.

**Spec:** `docs/superpowers/specs/2026-10-13-round38-n1-navigation-monitoring-design.md`

## Global Constraints

- Work only in `D:\Projects\_cns_workspaces\dev\round38_n1_navigation\CNS-PLANNER`; never modify the Round37-T3D-3B worktree.
- Do not reset, clean, rebase, merge, commit, or push. Commit steps are intentionally omitted from this plan.
- Do not modify or save the formal Zhoushan ProjectState; expected identity remains revision 468 and SHA256 `51432a8b7a401fa314b723314188da08e9582684d8115247dd2b1017321910fa`.
- Read compacted data only through existing authoritative hydration/artifact interfaces; an empty on-disk array is not evidence that data is absent.
- Do not invoke P14-P17/P16/Radar/RTK planning from simulation calls and keep ArcGIS upstream requests at 0.
- Do not change C, RID, Radar, RTK, P16, or continuous-service rules except the specified P17 unknown-time defect.
- No N2, FIG, SITE, FC30, T3D4, new towers, free-coordinate sites, MILP, or formal CNS full-flow work.
- All test commands use `D:\tools\anaconda\python.exe`, short TEMP/basetemp paths, `-q -x --tb=short -p no:cacheprovider`.

## Review Focus

- A node inside the workspace but backed by unresolved reference CRS must remain visible and spatially blocked; Task 1 pins this case.
- `force_endpoint_monitors=false` must be rejected and `max_optional_monitor_sites=0` must be accepted; Task 2 pins both boundaries.
- Demo adoption and canonical operational route records sharing IDs/endpoints must not share geometry or risk evidence; Task 3 pins cross-version rejection.
- A reusable device may satisfy only an exact stable site identity and must not be counted twice; Task 3 pins deduplication.
- P17 GNSS precision acceptance must survive while unknown duration/limit remains tri-state null and fail-closed through Step6; Task 5 pins the entire chain.

---

### Task 1: Shared CNS candidate view

**Files:**
- Create: `cns_planner/application/cns_shared_site_candidate_service.py`
- Modify: `cns_planner/application/workflow_service.py`
- Test: `tests/test_cns_shared_site_candidates.py`

**Interfaces:**
- Consumes: `session.state["nodes"]`, `ReferenceDataService.landing_sites_snapshot()`, `ReferenceDataService.towers_snapshot()`, explicit node/reference provenance and source-audit CRS evidence.
- Produces: `CNSSharedSiteCandidateService.snapshot() -> dict` and `WorkflowService.cns_shared_site_candidates() -> dict`.

- [ ] **Step 1: Write failing candidate-contract tests**

  Add tests asserting stable IDs (`node:<id>`, `reference-landing-site:<id>`, `tower-colocation:<id>`), deterministic ordering, explicit `reference_site_id` deduplication, no coordinate-proximity deduplication, preserved provenance/quality/CRS/site/data-source fields, and per-service C/N/RID/Radar/RTK applicability that never implies qualification.

- [ ] **Step 2: Write failing CRS and tower-boundary tests**

  Assert that a reference-derived node located inside the workspace but lacking confirmed CRS/traceable conversion has `spatial_use_status="blocked"`; its identity remains in output but it is absent from `spatial_candidates`. Assert that confirmed evidence enables it, invalid reference records remain listed, and the fixture's existing tower count is unchanged with no synthesized tower IDs.

- [ ] **Step 3: Run the new test file and confirm behavioral failures**

  Run: `$env:TEMP='D:\Projects\CNS-PLANNER\outputs\r38t'; D:\tools\anaconda\python.exe -m pytest -q -x --tb=short -p no:cacheprovider --basetemp=D:\Projects\CNS-PLANNER\outputs\r38b tests\test_cns_shared_site_candidates.py`

  Expected: FAIL because the service and workflow facade do not exist.

- [ ] **Step 4: Implement the minimal read-only adapter**

  Implement `CNSSharedSiteCandidateService(session, landing_sites_provider, towers_provider)` and `snapshot()`. Use only explicit association keys for deduplication; centralize coordinate/CRS evidence classification in a private helper; return counts and reason-code counts. Wire the service into `WorkflowService` without adding a writer, invalidator, or save call.

- [ ] **Step 5: Run candidate tests**

  Expected: all `tests/test_cns_shared_site_candidates.py` tests PASS.

### Task 2: Pure navigation-monitoring simulation kernel

**Files:**
- Create: `cns_planner/domain/navigation_monitoring_simulation.py`
- Test: `tests/test_navigation_monitoring_simulation.py`

**Interfaces:**
- Consumes: metric route points, metric candidate points, endpoint site identities, optional risk chainage intervals, optional reliable segment speeds, reusable-device facts, and request parameters.
- Produces: `validate_navigation_monitoring_parameters(payload: dict | None) -> dict` and `simulate_navigation_monitoring(*, route, candidates, endpoint_site_ids, parameters, risk_intervals=None, segment_speeds=None, reusable_devices=None) -> dict`.

- [ ] **Step 1: Write failing parameter and endpoint tests**

  Assert defaults `2000/10/15/3/4/true`; positive-finite validation for radius/tolerance/speed/weight; nonnegative integer validation allowing optional-site limit 0; explicit false/null/non-boolean `force_endpoint_monitors` rejection; blocked result when either mandatory endpoint lacks usable coordinate/CRS evidence.

- [ ] **Step 2: Write failing geometry/gap/timing tests**

  Pin exact line-circle chainage intervals, clipping, overlap/touch merging, multiple complements, total coverage, longest gap, and the independent numeric case `150 m / 15 mps = 10 s`. Assert reliable segment speeds override the route assumption and any missing/zero/non-finite/unreliable crossed speed yields `duration_s=null` and `tolerance_status="undetermined"`.

- [ ] **Step 3: Write failing selection and deduplication tests**

  Assert mandatory endpoint devices, exact reusable-device identity, no double counting, risk-weighted deterministic greedy ordering, landing-site tie priority, stable-ID tie break, `max_optional_monitor_sites=0`, cap exhaustion with remaining reasons, and removable optional-site pruning without silently widening the candidate set.

- [ ] **Step 4: Confirm tests fail for the missing kernel**

  Run the new test file with the fixed pytest command. Expected: FAIL on missing imports/functions.

- [ ] **Step 5: Implement the minimal pure-domain kernel**

  Reuse `metric_path_length_m` where applicable; implement analytic segment-circle intersection and stable interval operations locally because no existing helper provides the required interval contract. Keep the module file/QGIS/GDAL/state independent. Include normalized parameters, parameter sources, semantic disclaimer, and deterministic result fingerprint in output.

- [ ] **Step 6: Run kernel tests**

  Expected: all simulation-kernel tests PASS.

### Task 3: Authoritative route, risk, and candidate orchestration

**Files:**
- Create: `cns_planner/application/navigation_monitoring_simulation_service.py`
- Modify: `cns_planner/application/workflow_service.py`
- Test: `tests/test_navigation_monitoring_simulation_service.py`

**Interfaces:**
- Consumes: `CNSSharedSiteCandidateService.snapshot()`, hydrated workflow state, `ArtifactReadService`, current route/adoption services, `route_risk_profiles`, explicit request route selector/fingerprint, and Domain simulation functions.
- Produces: `NavigationMonitoringSimulationService.simulate(payload: dict | None) -> dict` and `WorkflowService.simulate_navigation_monitoring(payload=None) -> dict`.

- [ ] **Step 1: Write failing route-identity tests**

  Cover current demo adoption and canonical operational route separately. Assert each derives its own authority, currentness/adoption identity, CRS, full-coordinate geometry fingerprint, point count, and risk identity; mismatched request fingerprints and cross-version/cross-source risk reuse are rejected while base simulation remains available with `risk_priority_status="unavailable"`.

- [ ] **Step 2: Write failing evidence and endpoint tests**

  Assert authoritative artifact hydration is used where a scoped container is externalized, empty compacted placeholders are not treated as absent, missing elevation disables only 3D claims, unresolved endpoint CRS blocks the entire simulation, and an exact existing N device is reused once while name/proximity matches are ignored.

- [ ] **Step 3: Write failing parameter-recalculation and state-immutability tests**

  Call the service with changed radius, speed, tolerance, and optional-site limit; assert recomputed outputs/fingerprints change. Deep-compare `session.state` before/after and assert no `session.save()`, mutation lock, invalidation, P14-P17/P16/Radar/RTK evaluator, or network provider is called.

- [ ] **Step 4: Run service tests and confirm failures**

  Expected: FAIL because the orchestration service/facade is absent.

- [ ] **Step 5: Implement orchestration**

  Use `pyproj` only at the Application boundary to transform confirmed coordinates into an explicit metric CRS. Resolve the two route authorities through separate branches, match risk only after all currentness/candidate/geometry identities agree, pass only eligible candidates to the Domain kernel, and return blocked/unavailable diagnostics without writes.

- [ ] **Step 6: Run service and prior tests**

  Expected: Tasks 1-3 test files all PASS.

### Task 4: Lightweight read-only API

**Files:**
- Modify: `cns_planner/api/router.py`
- Test: `tests/test_navigation_monitoring_simulation_api.py`

**Interfaces:**
- Consumes: `WorkflowService.cns_shared_site_candidates()` and `WorkflowService.simulate_navigation_monitoring(payload)`.
- Produces: `GET /api/cns-shared-site-candidates` and `POST /api/navigation-monitoring-simulation`.

- [ ] **Step 1: Write failing API contract tests**

  Assert GET returns candidates/counts/reasons and POST returns route/parameter/candidate/risk fingerprints, sites/devices, coverage/gaps/timing, `writes_performed=0`, and Chinese validation/blocking errors. Assert HTTP calls leave state byte-equivalent and do not invoke high-cost or upstream services.

- [ ] **Step 2: Run API tests and confirm 404/missing behavior**

  Expected: FAIL because routes are not registered.

- [ ] **Step 3: Add minimal router dispatch**

  Add direct read-only GET/POST branches only; do not place them in formal planning resource actions, deferred-save blocks, QGIS mutation calls, or task dispatch.

- [ ] **Step 4: Run API and regression tests**

  Run API tests plus `tests/test_navigation_integrity_endpoint_planning.py`, `tests/test_reference_data.py`, and `tests/test_towers_real_data.py`. Expected: PASS with unchanged legacy behavior.

### Task 5: P17 tri-state unknown-time repair and version 2.3

**Files:**
- Modify: `cns_planner/algorithms/continuous_service/v1.py`
- Modify: `cns_planner/domain/cns_continuous_service.py`
- Modify: `tests/test_continuous_service_acceptability.py`
- Modify: `tests/test_round29q_p17_radar_limitation.py`
- Create: `tests/test_round38_p17_navigation_unknown_time.py`

**Interfaces:**
- Consumes: existing P17 navigation evidence and existing `apply_algorithm_semantics_stale`/Step6 paths.
- Produces: navigation degradation events with `exceeds_limit: true | false | null`; `CONTINUOUS_SERVICE_ALGORITHM_VERSION="2.3"`.

- [ ] **Step 1: Write failing full-chain tests**

  For precision-supported GNSS fallback, assert `navigation_state="acceptable_degraded"` remains true while its event has `duration_s=null`, `limit_s=null`, `exceeds_limit=null`; assert it is absent from subsystem/route/top-level `managed_gaps`, counts and disclosures, and cannot by itself yield `acceptable_with_managed_gap` or `confirmation_allowed=true` in authoritative or projected Step6.

- [ ] **Step 2: Write failing stale/version tests**

  Assert current version is 2.3 and a stored `continuous_service_acceptability_v1` result at 2.2 projects as `stale` without mutating stored state; current 2.3 remains current. Preserve existing Radar limitation and C/S numeric-event expectations.

- [ ] **Step 3: Run focused P17 tests and confirm failures**

  Run: `$env:TEMP='D:\Projects\CNS-PLANNER\outputs\r38t'; D:\tools\anaconda\python.exe -m pytest -q -x --tb=short -p no:cacheprovider --basetemp=D:\Projects\CNS-PLANNER\outputs\r38b tests\test_continuous_service_acceptability.py tests\test_round29q_p17_radar_limitation.py tests\test_round38_p17_navigation_unknown_time.py`

  Expected: failures show the existing navigation event uses false and/or 2.1.

- [ ] **Step 4: Apply the smallest algorithm change**

  In `_navigation_subsystem`, emit null when duration or limit is unknown and include managed events only when both evidence fields are known and `exceeds_limit is False`. Reuse existing aggregate and currentness paths; change only the P17 version constant to 2.3. Do not edit formal project data or P16 selection semantics.

- [ ] **Step 5: Run P17 and C/S regressions**

  Run the focused P17 files plus `tests/test_round28_p16_continuous_service_stop.py`. Expected: all PASS; GNSS fallback precision remains accepted, Step6 stays fail-closed for unknown time, and C/S rules are unchanged.

### Task 6: Acceptance, invariance, and delivery

**Files:**
- Create: `D:\Projects\_cns_workspaces\deliverables\round38_n1_navigation\00_g0_protection_verification.md`
- Create: `D:\Projects\_cns_workspaces\deliverables\round38_n1_navigation\01_implementation_summary.md`
- Create: `D:\Projects\_cns_workspaces\deliverables\round38_n1_navigation\02_navigation_simulation_example.json`
- Create: `D:\Projects\_cns_workspaces\deliverables\round38_n1_navigation\03_targeted_tests.md`
- Create: `D:\Projects\_cns_workspaces\deliverables\round38_n1_navigation\04_invariance_and_n2_handoff.md`

**Interfaces:**
- Consumes: completed N1 APIs, G0 manifest, official project read-only identity, Git status/diff, and targeted-test output.
- Produces: the requested review package; no project-state or Git history changes.

- [ ] **Step 1: Run the complete targeted suite**

  Run Tasks 1-5 tests together with short TEMP/basetemp paths. Record exact passed/skipped/failed counts; do not run the full suite or formal CNS pipeline.

- [ ] **Step 2: Run static and diff checks**

  Run `git diff --check`, inspect only task-scope diff/status, and run `python tools/remove_config_bom.py --check` only if JSON/YAML configuration changed.

- [ ] **Step 3: Verify immutability and external-request invariants**

  Recompute the official ProjectState SHA256 and revision, verify source/data hashes used by the report, assert ArcGIS request count remains 0, and recheck the Round37 worktree's exact 9 tracked + 5 untracked status and G0 hashes.

- [ ] **Step 4: Capture deterministic acceptance evidence**

  Record candidate totals/unusable reasons (including all registered landing sites and exactly 373 existing towers), endpoint mandatory-device result or explicit CRS blocker, parameter/result JSON example, merged intervals, every gap/time/status, risk identity outcome, optional-site cap outcome, and no-new-tower proof.

- [ ] **Step 5: Write N2 handoff and stop**

  Document the two read-only API contracts, parameter validation/error vocabulary, fingerprint fields, simulation-only semantics, unresolved limitations, and intentionally unimplemented N2/FIG/SITE/FC30/T3D4 work. Do not commit or push.
