---
name: cns-map-figures
description: Develop or review CNS-PLANNER thematic map and QGIS figure export code while keeping cartography read-only with respect to canonical state. Use for map templates, FigureSpec, QGIS rendering, legends, figure artifacts, and figure UI.
---

# CNS Map Figures

Treat map figures as derived presentation artifacts, never as a planning or state-authority path.

## Stable boundaries

- Read canonical facts through `cns_planner/application/map_figure_service.py`; do not mutate canonical containers, call `session.save()`, advance business `revision`, or invoke invalidation.
- Write only figure artifacts/indexes beneath `artifacts/map_figures/`. Keep the route-first layout and legacy flat layout read-only compatibility defined by `MapFigureStore`.
- Keep `cns_planner/gis/figure_spec.py` JSON-safe and free of QGIS objects or business decisions. Figure display thresholds are cartographic settings, not risk, clearance, safety, or feasibility rules.
- Keep QGIS layout/rendering in `cns_planner/gis/qgis_figure_renderer.py`; materialization belongs in the application service and styles in `figure_style.py`/`figure_legend.py`.
- Missing evidence stays absent/unknown and is disclosed in the figure. Do not infer zero/pass or use cartography to upgrade candidate/proposal output.
- `display_only_reference_layer` remains visual context only. Map layers must not affect planning, readiness, fingerprints, staleness, or CNS calculations.
- Never switch to `review/map-figures-v3` over a dirty worktree. Inspect it read-only with `git show`/`git ls-tree`, or switch only with explicit user authorization and a clean/safely preserved workspace.

## File routing

- Service/store/materialization: `cns_planner/application/map_figure_service.py`
- Stable spec/style/legend: `cns_planner/gis/figure_spec.py`, `figure_style.py`, `figure_legend.py`
- QGIS runtime/rendering: `cns_planner/gis/qgis_runtime.py`, `qgis_figure_renderer.py`, `qgis_project_layers.py`
- Template catalog: `cns_planner/reporting/map_templates/`
- Browser state/UI: `cns_planner/web/js/workflow/map_figure_state.js` and map-figure frontend tests

## Targeted validation

Choose only tests matching the changed layer:

- Service/API/state: `tests/test_map_figures_service.py`, `tests/test_map_figures_api.py`, `tests/map_figures_state_frontend.test.mjs`
- Templates/cartography: `tests/test_map_figures_cns_templates.py`, `tests/test_map_figures_cartographic_polish.py`, `tests/test_map_figures_formal_polish.py`
- Frontend: `tests/map_figures_frontend.test.mjs`, `tests/map_figures_cns_frontend.test.mjs`
- QGIS renderer: the relevant `tests/test_map_figures_*_qgis.py` or `tests/run_qgis_map_figure_tests.py`, only when the QGIS runtime is available

Always assert that figure generation leaves canonical state and business revision unchanged when the affected path could regress that boundary.
