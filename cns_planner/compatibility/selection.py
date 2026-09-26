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

#: 会话级缓存键前缀：显式 compatibility 调用设置的**运行期** selection 覆盖。
#: 它绝不进入 ``ProjectState``，也不改变旧项目已保存的 selection。
RUNTIME_SELECTION_PREFIX = "compatibility_selection:"

_EXPLICIT_VERSIONS = {
    "cns_gap_analysis_v1": "1.0", "cns_gap_analysis_v2": "2.0",
    "reuse_first_site_planner_v1": "1.0", "coverage_planner_v1": "1.0",
    "route_planner_v1": "1.0", "risk_aware_route_planner_v2": "2.0",
}


def set_runtime_compatibility_selection(session, algorithm_type, parameters, *, algorithm_id=None):
    """记录一次运行期 compatibility 参数覆盖，并返回权威的运行期 selection。

    旧 ``/api/algorithms/select`` 写入 ``algorithm_selection`` 的通道已按 B7X 关闭；
    兼容区内仍然可编辑的参数（例如旧版 Risk-Aware Route Planner V2 的 trial 参数）
    只能写在这个进程内缓存里，并只影响后续 compatibility 试算。
    """

    from ..application.production_write_authority import runtime_compatibility_results

    if str(algorithm_type) not in FROZEN_COMPATIBILITY_SELECTIONS:
        raise ValueError(f"{algorithm_type} 不是 compatibility/archive 选择类型")
    frozen = FROZEN_COMPATIBILITY_SELECTIONS[str(algorithm_type)]
    resolved_id = str(algorithm_id or frozen["algorithm_id"])
    if resolved_id not in _ALLOWED[str(algorithm_type)]:
        raise ValueError(
            f"{resolved_id} 不是 {algorithm_type} compatibility capability"
        )
    if not isinstance(parameters, dict):
        raise ValueError("parameters 必须是对象")
    record = {
        "algorithm_type": str(algorithm_type),
        "algorithm_id": resolved_id,
        "version": str(frozen["version"]) if resolved_id == frozen["algorithm_id"] else _EXPLICIT_VERSIONS[resolved_id],
        "parameters": deepcopy(parameters),
        "source": "runtime_compatibility_selection",
        "persistent_write": False,
    }
    results = runtime_compatibility_results(session, create=True)
    results[f"{RUNTIME_SELECTION_PREFIX}{algorithm_type}"] = record
    return deepcopy(record)


def runtime_compatibility_selection(session, algorithm_type):
    """读取会话级 selection 覆盖；不存在时返回 ``{}``（不创建容器）。"""

    from ..application.production_write_authority import runtime_compatibility_result

    value = runtime_compatibility_result(
        session, f"{RUNTIME_SELECTION_PREFIX}{algorithm_type}"
    )
    return deepcopy(value) if isinstance(value, dict) else {}


class CompatibilitySelectionAdapter:
    """READ OLD, or the session override, or a frozen baseline; never write ``algorithm_selection``."""

    def __init__(self, state, registry, session=None):
        self.state = state
        self.registry = registry
        self.session = session

    def selection(self, algorithm_type):
        runtime = (
            runtime_compatibility_selection(self.session, algorithm_type)
            if self.session is not None else {}
        )
        saved = (self.state.get("algorithm_selection") or {}).get(algorithm_type)
        if (
            isinstance(runtime, dict)
            and str(runtime.get("algorithm_id")) in _ALLOWED.get(algorithm_type, set())
        ):
            result, source = deepcopy(runtime), "runtime_compatibility_selection"
        elif (
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
        """只读投影：四个 compatibility 选择类型当前实际生效的 selection。"""

        return {
            algorithm_type: self.selection(algorithm_type)
            for algorithm_type in FROZEN_COMPATIBILITY_SELECTIONS
        }

    def create(self, algorithm_type, *, algorithm_id=None):
        selection = self.selection(algorithm_type)
        if algorithm_id is not None:
            selection = self._explicit(algorithm_type, algorithm_id)
        return self.registry.create(
            selection["algorithm_type"], selection["algorithm_id"],
            selection["version"], selection.get("parameters") or {},
        )

    @staticmethod
    def _explicit(algorithm_type, algorithm_id):
        if algorithm_id not in _ALLOWED.get(algorithm_type, set()):
            raise ValueError(f"{algorithm_id} 不是 {algorithm_type} compatibility capability")
        return {
            "algorithm_type": algorithm_type, "algorithm_id": algorithm_id,
            "version": _EXPLICIT_VERSIONS[algorithm_id], "parameters": {},
        }
