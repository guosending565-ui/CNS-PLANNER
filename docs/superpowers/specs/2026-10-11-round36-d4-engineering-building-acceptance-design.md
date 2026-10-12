# Round36-D4 Engineering Building Acceptance Design

## Intent

Enable one auditable engineering-demo exception for the current R0005 / ALT-100
candidate and building 4tCa7 (FID 360599), while preserving the strict contract that
`predicted` height evidence is unresolved. The exception is not a verified safety finding
and never grants operational authorization.

## Policy and evidence contract

Add a versioned, default-disabled `engineering_demo_building_acceptance_policy` to
ProjectState. `LayeredRouteValidationService` is its production writer. The policy is
effective only when `enabled=true`, `confirmed=true`, an authorization record is present,
and every pinned identity matches: scenario route, altitude layer, candidate fingerprint,
building id/FID, building source SHA256, evidence fingerprint, policy version and a
non-negative minimum estimated vertical margin.

The building adapter carries the source fields without upgrading them: `predicted`, source,
method, observation period, licence, raw variance and the explicit statement that variance
is not a calibrated safety confidence bound. Missing fields, a fingerprint mismatch, an
estimated conflict, or any second insufficient-evidence building remains fail-closed.

## Validation states

Strict validation is unchanged: predicted/unknown evidence produces building `unresolved`
and overall `unresolved`.

With the effective policy, exactly one matching predicted building may produce building
`conditionally_accepted`. If terrain, tower and restricted-area domains pass, the overall
record is `engineering_conditionally_accepted_candidate`. The record retains
`strict_safety_validation_passed=false`, `engineering_conditional_acceptance=true`,
`use_scope=engineering_demo_only`, `operational_authorization=false`, authorization,
limitations and evidence/policy fingerprints. It never becomes `validated_candidate`.

The policy and all live dependencies participate in validation fingerprint/currentness.
Changing source content, evidence, height, candidate geometry, altitude layer, policy or
validator version stales the validation and its adoption through the existing invalidation
chain.

## Publication isolation

The consumer audit found many production services read canonical `operational_routes`
directly. Therefore an engineering conditional Apply must not write that container or set
the global routes result to passed. The existing `layered_operational_adoptions` record is
the authority for the published demo projection, with
`status=published_engineering_demo`, `published_to_operational_routes=false`, and the full
conditional evidence/limitations.

Strict `validated_candidate` Apply remains unchanged and continues to publish canonical
operational routes. Production planning, P14/P15/P16, MILP, enforced and formal permission
chains cannot see the engineering route because it is absent from `operational_routes`.

Step05 combined LOS gets an explicit read-only engineering-demo selector. It consumes only
a current matching published engineering adoption when `engineering_demo_mode=true` and
otherwise retains its existing strict operational-route behavior. Status and details do not
recompute evidence; diagnose performs the bounded computation.

## Surfaces and reporting

API snapshots expose the policy, conditional validation and adoption fields verbatim.
Layered validation/adoption UI, Step05, report and export projection visibly state:
“工程条件接受，严格验证未通过，仅供演示”. Three-dimensional display may render the
demo path only through the explicit demo projection and must retain the same label.

## Verification

Use targeted Python and Node tests. Cover strict unchanged behavior, explicit conditional
acceptance, disabled/mismatched policy, second predicted building, missing evidence,
estimated conflict, fingerprint/currentness changes, trusted-height regression, unaffected
terrain/tower/restricted domains, API/UI/report propagation and production-consumer
isolation. Then reuse D3 candidate `LRC2-R0005-ALT-100-layeredcandv1-91` in the isolated
acceptance snapshot for risk, validation, preview/Apply and Step05 status → diagnose →
details plus a real browser click.
