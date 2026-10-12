# Round36-D4 Engineering Building Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fail-closed, single-building engineering-demo acceptance path and exercise R0005 through isolated Step05 LOS without exposing it to production route consumers.

**Architecture:** Extend the existing layered validation/adoption authority chain. Conditional Apply persists only an adoption-owned demo projection; canonical `operational_routes` remains strict-only. Step05 opts into that projection explicitly.

**Tech Stack:** Python, QGIS/GDAL adapters, native JavaScript ES modules, pytest, Node.

**Spec:** `docs/superpowers/specs/2026-10-11-round36-d4-engineering-building-acceptance-design.md`

## Global Constraints

- No commit, push, merge, reset, clean or rebase.
- Preserve strict `predicted -> unresolved` semantics and original evidence level.
- No original GPKG/TIF, main repository or authoritative Zhoushan project mutation.
- No Radar enforced, 373-tower MILP or full P16 optimization.
- Use D3 current candidate; do not rerun Theta* when current.

## Review Focus

- A second predicted building must defeat the single-building exception.
- Missing source/method/period/licence/variance/identity data must fail closed.
- Policy, source SHA, evidence, height, geometry or layer changes must stale applicability.
- Conditional adoption must never enter canonical `operational_routes` or production tasks.
- Step05 status/details must stay read-only and diagnose must require explicit demo mode.

---

### Task 1: Consumer isolation and policy contract

**Files:** domain validation contract, project state, validation service, router/workflow; new targeted pytest.

**Interfaces:** Produces normalized policy, setter/snapshot API and existing-invalidation propagation.

- [ ] Write failing policy/default/currentness and consumer-isolation tests.
- [ ] Run targeted pytest and verify expected failures.
- [ ] Implement the normalized policy and existing-service writer/API.
- [ ] Include policy/evidence identity in fingerprint/currentness and rerun tests.
- [ ] Record scoped diff and tests in the ledger; do not commit.

### Task 2: Conditional building validation

**Files:** building adapter, continuous validator, layered validation aggregation/domain; targeted pytest.

**Interfaces:** Consumes Task 1 policy; produces `conditionally_accepted` and `engineering_conditionally_accepted_candidate` with complete evidence.

- [ ] Write failing strict/conditional/disabled/second-building/missing/conflict tests.
- [ ] Verify RED, then minimally propagate evidence and conditional evaluator.
- [ ] Verify GREEN plus trusted-height and unaffected-domain regressions.
- [ ] Record scoped diff and tests in the ledger; do not commit.

### Task 3: Isolated adoption and API/UI/report propagation

**Files:** layered adoption domain/service, export/report projections, validation/adoption frontend and tests.

**Interfaces:** Consumes conditional validation; produces published engineering adoption without canonical route mutation.

- [ ] Write failing Apply isolation and field-propagation tests.
- [ ] Verify RED, implement conditional gate/projection and visible limitations.
- [ ] Verify canonical production consumers remain unchanged and tests GREEN.
- [ ] Record scoped diff and tests in the ledger; do not commit.

### Task 4: Explicit Step05 demo route consumer

**Files:** radar LOS diagnostics service/router, Step05 module and targeted Python/Node tests.

**Interfaces:** Consumes current published engineering adoption only with `engineering_demo_mode=true`; produces status/diagnose/details and labels.

- [ ] Write failing read-only status/details, explicit opt-in and browser-model tests.
- [ ] Verify RED, implement selector and UI request/labels.
- [ ] Verify GREEN and existing strict LOS behavior unchanged.
- [ ] Record scoped diff and tests in the ledger; do not commit.

### Task 5: D3 data-call consistency regression

**Files:** focused regression test and, only if proven necessary, shared harness setup.

**Interfaces:** Asserts harness/production identity for surface class, land mask, population shelter, feasibility mask, height field and vertical datum.

- [ ] Write the focused assertion test and verify RED only for a real mismatch.
- [ ] Apply the smallest wiring fix if required; otherwise keep it characterization-only.
- [ ] Run targeted tests and record the result; do not rerun Theta*.

### Task 6: Isolated real-data acceptance and delivery

**Files:** D3 acceptance snapshot state, Round36-D4 deliverables, CONTROL/BASELINE.

**Interfaces:** Executes risk → validation → preview/Apply → Step05 status/diagnose/details → real browser click; freezes evidence.

- [ ] Verify candidate/source/currentness and pre-hashes; do not replan if current.
- [ ] Run bounded real APIs and assert four-domain results and conditional identity.
- [ ] Apply through the normal endpoint and prove canonical `operational_routes` unchanged.
- [ ] Run Step05 API sequence and browser click; capture screenshots.
- [ ] Run targeted Python/Node tests, `node --check`, BOM check and scoped `git diff --check`.
- [ ] Produce delivery docs/manifest/invariance evidence, update CONTROL/BASELINE, then release the lock.
