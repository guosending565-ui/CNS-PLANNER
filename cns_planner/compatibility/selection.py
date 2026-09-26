"""Resolve frozen compatibility baselines without writing ProjectState selections."""

from __future__ import annotations

from copy import deepcopy


FROZEN_COMPATIBILITY_SELECTIONS = {
    "route_planner": {
        "algorithm_type": "route_planner", "algorithm_id": "route_planner_v1",
        "version": "1.0", "parameters": {},
    },
    "coverage_planner": {
        "algorithm_type": "coverage_planner", "algorithm_id": "coverage_planner_v1",
        "version": "1.0", "parameters": {},
    },
    "cns_gap_analyzer": {
        "algorithm_type": "cns_gap_analyzer", "algorithm_id": "cns_gap_analysis_v1",
        "version": "1.0", "parameters": {},
    },
    "site_planner": {
        "algorithm_type": "site_planner", "algorithm_id": "reuse_first_site_planner_v1",
        "version": "1.0", "parameters": {},
    },
}

_ALLOWED = {
    "route_planner": {"route_planner_v1", "risk_aware_route_planner_v2"},
    "coverage_planner": {"coverage_planner_v1"},
    "cns_gap_analyzer": {"cns_gap_analysis_v1", "cns_gap_analysis_v2"},
    "site_planner": {"reuse_first_site_planner_v1"},
}


class CompatibilitySelectionAdapter:
    """READ OLD or a frozen passive baseline; never write ``algorithm_selection``."""

    def __init__(self, state, registry, session=None):
        self.state = state
        self.registry = registry
        self.session = session

    def selection(self, algorithm_type):
        saved = (self.state.get("algorithm_selection") or {}).get(algorithm_type)
        if (
            isinstance(saved, dict)
            and str(saved.get("algorithm_id")) in _ALLOWED.get(algorithm_type, set())
            and str(saved.get("algorithm_type") or algorithm_type) == algorithm_type
        ):
            result, source = deepcopy(saved), "saved_legacy_selection"
        else:
            # No saved compatibility selection, or a saved selection this adapter cannot
            # resolve: use the frozen baseline instead of raising.  An unresolvable saved
            # value is still never rewritten back into ``algorithm_selection``.
            result = deepcopy(FROZEN_COMPATIBILITY_SELECTIONS[algorithm_type])
            source = (
                "frozen_compatibility_baseline"
                if saved is None
                else "frozen_compatibility_baseline_unresolvable_saved_selection"
            )
        result["algorithm_type"] = algorithm_type
        result["selection_source"] = source
        return result

    def selection_snapshot(self):
        """只读投影：四个 compatibility 选择类型当前只读的 selection。"""

        return {
            algorithm_type: self.selection(algorithm_type)
            for algorithm_type in FROZEN_COMPATIBILITY_SELECTIONS
        }
