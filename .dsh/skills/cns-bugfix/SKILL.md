---
name: cns-bugfix
description: Diagnose and fix CNS-PLANNER defects with the smallest safe change and targeted tests. Use for regressions, incorrect behavior, exceptions, or contract mismatches; not for new features or algorithm redesign.
---

# CNS Bugfix

Keep the change narrow, preserve frozen semantics, and prove the reported behavior.

## Workflow

1. Read the applicable section of repository `AGENTS.md`. Use `AI_DEV_CONTEXT.md` only to select the relevant contract or authority source; do not load unrelated project history.
2. Reproduce or characterize the defect with the smallest existing test or a focused new regression test. Use `rg` to trace the failing symbol and its callers.
3. Before changing state, identify the production writer in `cns_planner/application/` and the relevant invalidation entry in `cns_planner/application/invalidation_service.py`.
4. Fix one complete logic unit. Do not add an algorithm, broaden a state writer, silently change frozen math, or convert missing/unknown values to zero/pass.
5. Run only the directly related pytest or Node test files. Add contract/characterization coverage when state authority, staleness, schema, or frozen algorithm behavior is touched.
6. Inspect the scoped diff and run `git diff --check`. Report the root cause, changed files, and exact targeted tests.

## Test entry points

- Backend command and sandbox fallback: `README.md` §16.
- Frontend modules: run the relevant `node tests/<name>.test.mjs`; add `node --check` for changed entry modules.
- Schema/persistence: test additive backfill and old-project opening.
- QGIS/real-data paths: prefer adapter/service tests; enable HTTP/QGIS integration only when its documented runtime is available.

Never run the full pytest suite unless the user explicitly requests it or a final release gate requires it.
