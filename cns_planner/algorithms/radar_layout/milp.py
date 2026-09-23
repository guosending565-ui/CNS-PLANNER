"""Radar Surveillance Layout V1 — MILP（``scipy.optimize.milp`` + HiGHS）。

绝不使用 greedy 冒充最优解；大矩阵使用 ``scipy.sparse`` 结构。

变量
----

``x_p ∈ {0,1}``
    panel ``p`` 是否安装。

``y_tj ∈ {0,1}``
    不同 tower/site ``t`` 是否**通过至少一个已选择 panel** 覆盖 sample ``j``。
    ``y`` 只是辅助线性化变量；最终业务事实一律根据 selected panels 重算
    （见 :mod:`cns_planner.algorithms.radar_layout.v1`）。

约束
----

每塔最多 4 个 panel::

    sum_{p in tower t} x_p <= 4

站址覆盖 link（``A_pj`` = panel p 覆盖 sample j）::

    y_tj <= sum_{p in tower t, A_pj=1} x_p

若某 tower 不存在任何可覆盖 ``j`` 的 panel，则不创建 ``y_tj``（变量默认上界 0，
等价于固定为 0），也不产生无意义约束。

每个 sample::

    sum_t y_tj >= required_distinct_site_count_j

因此同一个塔的 Face1+Face2 同时覆盖同一点，也只贡献 **1** 个独立站址。

求解状态语义
------------

* ``optimal`` ⇒ ``optimality_proven = True``；
* ``infeasible`` ⇒ ``infeasibility_proven = True``（**只有** solver 明确证明不可行）；
* ``time_limit`` / ``iteration_limit`` / ``node_limit`` / ``solver_error`` ⇒
  ``search_incomplete`` 或 ``solver_error``：**不得**解释为"该型号不可行"，
  也不得自动进入下一阶段；
* ``solver_unavailable`` ⇒ 当前 Python 环境没有 scipy：直接返回该状态，
  **绝不退化为 greedy**。
"""

from __future__ import annotations

from ...domain.radar_surveillance_layout import (
    MAX_PANELS_PER_TOWER, RADAR_TYPE_II,
)

#: 求解器后端名称（写入结果 provenance）。
SOLVER_NAME = "scipy.optimize.milp"
SOLVER_LIBRARY = "HiGHS"

#: 求解状态词表。
SOLVER_STATUSES = (
    "optimal",
    "infeasible",
    "time_limit",
    "iteration_limit",
    "node_limit",
    "solver_error",
    "solver_unavailable",
)

#: ``optimal`` 之外的"搜索未完成"状态：既不证明可行也不证明不可行。
INCOMPLETE_STATUSES = ("time_limit", "iteration_limit", "node_limit")

#: 词典序两阶段的目标：``total_panel_count`` 恒为第一目标。
OBJECTIVES = ("total_panel_count", "radar_ii_panel_count")


def solver_available():
    """scipy 是否可用（不可用时**只**返回 ``solver_unavailable``，不退化）。"""

    try:
        from scipy.optimize import milp  # noqa: F401
        from scipy.sparse import coo_matrix  # noqa: F401
    except Exception:
        return False
    return True


def solver_backend():
    if not solver_available():
        return {"available": False, "name": SOLVER_NAME, "library": SOLVER_LIBRARY,
                "reason": "scipy_not_installed"}
    try:
        import scipy
        version = getattr(scipy, "__version__", None)
    except Exception:  # pragma: no cover - defensive
        version = None
    return {"available": True, "name": SOLVER_NAME, "library": SOLVER_LIBRARY,
            "scipy_version": version, "reason": None}


def empty_solver_block(*, stage=None, status="not_run", message=None, proven=False):
    """Solver 结果块。

    ``proven=True`` 只允许在**结论确实被证明**时使用（例如 presolve 的结构化不可行
    证据）；默认 ``proven=False``，因此任何"未证明"的状态都不会被误标为已证明。
    """

    return {
        "name": SOLVER_NAME,
        "library": SOLVER_LIBRARY,
        "stage": stage,
        "status": status,
        "optimality_proven": bool(proven and status == "optimal"),
        "infeasibility_proven": bool(proven and status == "infeasible"),
        "mip_gap": None,
        "objective": None,
        "message": message,
        "greedy_fallback_used": False,
    }


def _mip_gap(result):
    """相对 MIP gap（``|objective − dual_bound| / max(1, |objective|)``）。

    仅在 solver 明确给出 dual bound 时计算；否则保持 ``None``（绝不编造 0 gap）。
    """

    objective = getattr(result, "fun", None)
    bound = getattr(result, "mip_dual_bound", None)
    if objective is None or bound is None:
        return None
    try:
        objective = float(objective)
        bound = float(bound)
    except (TypeError, ValueError):
        return None
    if objective != objective or bound != bound:
        return None
    if abs(objective) == float("inf") or abs(bound) == float("inf"):
        return None
    scale = max(1.0, abs(objective))
    return abs(objective - bound) / scale


def build_index(panels):
    """panel / tower / sample 索引与稀疏结构（确定性：按输入顺序）。"""

    panel_index = {str(panel["panel_id"]): index for index, panel in enumerate(panels)}
    tower_ids = []
    for panel in panels:
        tower_id = str(panel["tower_id"])
        if tower_id not in tower_ids:
            tower_ids.append(tower_id)
    tower_index = {tower_id: index for index, tower_id in enumerate(tower_ids)}
    return {"panel": panel_index, "tower": tower_index, "tower_ids": tower_ids}


def tower_coverage(panels, sample_count):
    """``{tower_id: {sample_index: [panel_index, ...]}}``。"""

    coverage = {}
    for panel_index, panel in enumerate(panels):
        tower_id = str(panel["tower_id"])
        bucket = coverage.setdefault(tower_id, {})
        for sample_index in panel.get("covered_sample_indices") or []:
            bucket.setdefault(int(sample_index), []).append(panel_index)
    return coverage


def candidate_distinct_site_count(*, panels, samples, radar_types=None):
    """求解前的**独立站址候选数**（每个 sample 有多少个不同塔在物理上能覆盖它）。

    这是"求解前不可行检查"的依据：若 ``candidate_distinct_site_count <
    required_distinct_site_count``，无论怎么选 panel 都不可能满足，直接形成结构化
    infeasibility evidence，而不是返回一句"求解失败"。
    """

    allowed = set(radar_types) if radar_types else None
    per_sample = [set() for _ in samples]
    for panel in panels:
        if allowed is not None and panel["radar_type"] not in allowed:
            continue
        for sample_index in panel.get("covered_sample_indices") or []:
            index = int(sample_index)
            if 0 <= index < len(per_sample):
                per_sample[index].add(str(panel["tower_id"]))
    return [len(item) for item in per_sample]


def presolve_infeasibility(*, panels, samples, required_counts, radar_types=None):
    """结构化不可行证据：候选独立站址数不足的 sample 清单。"""

    counts = candidate_distinct_site_count(
        panels=panels, samples=samples, radar_types=radar_types,
    )
    insufficient = []
    for index, sample in enumerate(samples):
        required = required_counts[index]
        if required is None:
            insufficient.append({
                "sample_index": index,
                "sample_id": sample.get("sample_id"),
                "reason": "required_distinct_site_count_unknown",
                "surface_class": sample.get("surface_class"),
                "candidate_distinct_site_count": counts[index],
                "required_distinct_site_count": None,
                "detail": (
                    "surface_class=unknown 时要求数量未知，fail-closed："
                    "不得自动按 sea（1 站）处理"
                ),
            })
            continue
        if counts[index] < required:
            insufficient.append({
                "sample_index": index,
                "sample_id": sample.get("sample_id"),
                "reason": "candidate_distinct_site_count_below_required",
                "surface_class": sample.get("surface_class"),
                "candidate_distinct_site_count": counts[index],
                "required_distinct_site_count": required,
                "distance_along_route_m": sample.get("distance_along_route_m"),
            })
    return {
        "candidate_distinct_site_count": counts,
        "min_candidate_distinct_site_count": min(counts) if counts else None,
        "insufficient_samples": insufficient,
        "insufficient_sample_count": len(insufficient),
        "semantics": "presolve_structural_infeasibility_evidence",
    }


def solve(*, panels, samples, required_counts, radar_types=None,
          max_panels_per_tower=MAX_PANELS_PER_TOWER, objectives=("total_panel_count",),
          stage=None, options=None):
    """求解一个 MILP。

    ``objectives`` 是词典序目标序列（仅用于把多个目标**依次**编码；本函数每次只
    优化 ``objectives`` 的第一个目标，调用方负责加入等式约束后再次调用）。
    ``required_counts[j]`` 为 ``None`` 表示该 sample 的要求数量未知（fail-closed：
    不生成约束，由 presolve 证据承载，且最终状态不可能为 passed）。
    """

    objective_name = str(objectives[0]) if objectives else "total_panel_count"
    if objective_name not in OBJECTIVES:
        raise ValueError(f"不支持的目标：{objective_name}")

    if not solver_available():
        return {
            "solver": empty_solver_block(
                stage=stage, status="solver_unavailable",
                message=(
                    "当前 Python 环境没有可用的 scipy.optimize.milp；"
                    "本模型绝不退化为 greedy。请安装 optional extra："
                    "pip install -e .[optimization]（或 pip install \"scipy>=1.11,<2\"）"
                ),
            ),
            "selected_panel_indices": [],
            "selected_panel_ids": [],
            "selected_y": {},
            "panel_count": None,
            "radar_ii_panel_count": None,
            "objective_name": objective_name,
            "variable_count": 0,
            "constraint_count": 0,
            "infeasibility_evidence": None,
        }

    from scipy.optimize import Bounds, LinearConstraint, milp
    from scipy.sparse import coo_matrix

    allowed = set(radar_types) if radar_types else None
    panel_indices = [
        index for index, panel in enumerate(panels)
        if allowed is None or panel["radar_type"] in allowed
    ]
    active_panels = [panels[index] for index in panel_indices]

    index = build_index(active_panels)
    coverage = tower_coverage(active_panels, len(samples))

    # ---- variables: x (panels) then y (tower × sample) -----------------------------
    y_variables = []
    y_lookup = {}
    for tower_id in index["tower_ids"]:
        for sample_index in sorted(coverage.get(tower_id, {})):
            y_lookup[(tower_id, sample_index)] = len(active_panels) + len(y_variables)
            y_variables.append((tower_id, sample_index))
    total_variables = len(active_panels) + len(y_variables)

    rows, cols, data, lower, upper = [], [], [], [], []

    def add_row(entries, low, high):
        row = len(lower)
        for column, value in entries:
            rows.append(row)
            cols.append(column)
            data.append(value)
        lower.append(low)
        upper.append(high)

    # 每塔最多 4 个 panel。
    for tower_id in index["tower_ids"]:
        entries = [
            (local, 1.0) for local, panel in enumerate(active_panels)
            if str(panel["tower_id"]) == tower_id
        ]
        if entries:
            add_row(entries, -float("inf"), float(max_panels_per_tower))

    # 站址覆盖 link：y_tj <= sum(A_pj * x_p)  ⇔  y_tj − sum(A_pj x_p) <= 0
    for (tower_id, sample_index), column in y_lookup.items():
        entries = [(column, 1.0)]
        for local in coverage[tower_id][sample_index]:
            entries.append((local, -1.0))
        add_row(entries, -float("inf"), 0.0)

    # 每个 sample：sum_t y_tj >= required_j
    for sample_index in range(len(samples)):
        required = required_counts[sample_index] if sample_index < len(required_counts) else None
        if required is None:
            continue
        entries = [
            (y_lookup[(tower_id, sample_index)], 1.0)
            for tower_id in index["tower_ids"]
            if (tower_id, sample_index) in y_lookup
        ]
        add_row(entries, float(required), float("inf"))

    matrix = coo_matrix(
        (data, (rows, cols)), shape=(len(lower), max(1, total_variables)),
    ).tocsr()
    constraints = LinearConstraint(matrix, lower, upper) if lower else None

    # ---- objective ----------------------------------------------------------------
    cost = [0.0] * total_variables
    if objective_name == "radar_ii_panel_count":
        for local, panel in enumerate(active_panels):
            if panel["radar_type"] == RADAR_TYPE_II:
                cost[local] = 1.0
    else:
        for local in range(len(active_panels)):
            cost[local] = 1.0

    integrality = [1] * total_variables
    bounds = Bounds([0.0] * total_variables, [1.0] * total_variables)

    solve_options = {"presolve": True}
    if isinstance(options, dict):
        limit = options.get("time_limit_s")
        if isinstance(limit, (int, float)) and limit and limit > 0:
            solve_options["time_limit"] = float(limit)
        gap = options.get("mip_rel_gap")
        if isinstance(gap, (int, float)) and gap is not None:
            solve_options["mip_rel_gap"] = float(gap)

    try:
        result = milp(
            c=cost, constraints=constraints, integrality=integrality, bounds=bounds,
            options=solve_options,
        )
    except Exception as exc:  # solver 自身抛错 ⇒ solver_error，绝不冒充 infeasible
        return {
            "solver": {
                **empty_solver_block(
                    stage=stage, status="solver_error",
                    message=f"solver_exception:{type(exc).__name__}:{exc}",
                ),
                "variable_count": total_variables,
                "constraint_count": len(lower),
                "objective_name": objective_name,
            },
            "selected_panel_indices": [],
            "selected_panel_ids": [],
            "selected_y": {},
            "panel_count": None,
            "radar_ii_panel_count": None,
            "objective_name": objective_name,
            "variable_count": total_variables,
            "constraint_count": len(lower),
            "infeasibility_evidence": None,
        }
    status = getattr(result, "status", None)
    success = bool(getattr(result, "success", False))
    message = str(getattr(result, "message", "") or "")

    if status == 0 and success:
        mapped = "optimal"
    elif status == 2:
        mapped = "infeasible"
    elif status == 1:
        mapped = "iteration_limit" if "iteration" in message.lower() else "time_limit"
    elif status == 3:
        mapped = "node_limit"
    elif status == 4:
        mapped = "solver_error"
    else:  # pragma: no cover - defensive
        mapped = "solver_error"

    solver_block = {
        "name": SOLVER_NAME,
        "library": SOLVER_LIBRARY,
        "stage": stage,
        "status": mapped,
        "optimality_proven": mapped == "optimal",
        "infeasibility_proven": mapped == "infeasible",
        "mip_gap": _mip_gap(result) if mapped == "optimal" else None,
        "objective": (
            float(getattr(result, "fun")) if getattr(result, "fun", None) is not None else None
        ),
        "message": message,
        "greedy_fallback_used": False,
        "variable_count": total_variables,
        "constraint_count": len(lower),
        "panel_variable_count": len(active_panels),
        "site_coverage_variable_count": len(y_variables),
        "objective_name": objective_name,
    }

    selected_local, selected_y = [], {}
    solution = getattr(result, "x", None)
    if mapped == "optimal" and solution is not None:
        for local, value in enumerate(solution):
            if local < len(active_panels):
                if round(float(value)) == 1:
                    selected_local.append(local)
            else:
                tower_id, sample_index = y_variables[local - len(active_panels)]
                selected_y[f"{tower_id}@{sample_index}"] = int(round(float(value)))
        selected_local.sort()

    selected_panel_ids = [active_panels[local]["panel_id"] for local in selected_local]
    selected_global = [panel_indices[local] for local in selected_local]
    radar_ii_count = sum(
        1 for local in selected_local if active_panels[local]["radar_type"] == RADAR_TYPE_II
    )

    return {
        "solver": solver_block,
        "selected_panel_indices": selected_global,
        "selected_panel_ids": selected_panel_ids,
        "selected_y": selected_y,
        "panel_count": len(selected_local) if mapped == "optimal" else None,
        "radar_ii_panel_count": radar_ii_count if mapped == "optimal" else None,
        "objective_name": objective_name,
        "variable_count": total_variables,
        "constraint_count": len(lower),
        "infeasibility_evidence": None,
    }


def solve_with_total_panel_count(*, panels, samples, required_counts, total_panel_count,
                                 radar_types=None, max_panels_per_tower=MAX_PANELS_PER_TOWER,
                                 options=None, stage=None):
    """在 ``total_panel_count == N*`` 的等式约束下最小化 II 型面阵数量（Stage B2）。

    实现方式：把"总面阵数"作为额外的等式约束加入同一 MILP。
    """

    objective_name = "radar_ii_panel_count"
    if not solver_available():
        return solve(
            panels=panels, samples=samples, required_counts=required_counts,
            radar_types=radar_types, max_panels_per_tower=max_panels_per_tower,
            objectives=(objective_name,), stage=stage, options=options,
        )

    from scipy.optimize import Bounds, LinearConstraint, milp
    from scipy.sparse import coo_matrix

    allowed = set(radar_types) if radar_types else None
    panel_indices = [
        index for index, panel in enumerate(panels)
        if allowed is None or panel["radar_type"] in allowed
    ]
    active_panels = [panels[index] for index in panel_indices]
    index = build_index(active_panels)
    coverage = tower_coverage(active_panels, len(samples))

    y_variables = []
    y_lookup = {}
    for tower_id in index["tower_ids"]:
        for sample_index in sorted(coverage.get(tower_id, {})):
            y_lookup[(tower_id, sample_index)] = len(active_panels) + len(y_variables)
            y_variables.append((tower_id, sample_index))
    total_variables = max(1, len(active_panels) + len(y_variables))

    rows, cols, data, lower, upper = [], [], [], [], []

    def add_row(entries, low, high):
        row = len(lower)
        for column, value in entries:
            rows.append(row)
            cols.append(column)
            data.append(value)
        lower.append(low)
        upper.append(high)

    for tower_id in index["tower_ids"]:
        entries = [
            (local, 1.0) for local, panel in enumerate(active_panels)
            if str(panel["tower_id"]) == tower_id
        ]
        if entries:
            add_row(entries, -float("inf"), float(max_panels_per_tower))

    for (tower_id, sample_index), column in y_lookup.items():
        entries = [(column, 1.0)]
        for local in coverage[tower_id][sample_index]:
            entries.append((local, -1.0))
        add_row(entries, -float("inf"), 0.0)

    for sample_index in range(len(samples)):
        required = required_counts[sample_index] if sample_index < len(required_counts) else None
        if required is None:
            continue
        entries = [
            (y_lookup[(tower_id, sample_index)], 1.0)
            for tower_id in index["tower_ids"]
            if (tower_id, sample_index) in y_lookup
        ]
        add_row(entries, float(required), float("inf"))

    # 词典序第二目标的前置等式约束：总面阵数固定为 N*。
    add_row(
        [(local, 1.0) for local in range(len(active_panels))],
        float(total_panel_count), float(total_panel_count),
    )

    matrix = coo_matrix((data, (rows, cols)), shape=(len(lower), total_variables)).tocsr()
    constraints = LinearConstraint(matrix, lower, upper) if lower else None

    cost = [0.0] * total_variables
    for local, panel in enumerate(active_panels):
        if panel["radar_type"] == RADAR_TYPE_II:
            cost[local] = 1.0

    solve_options = {"presolve": True}
    if isinstance(options, dict):
        limit = options.get("time_limit_s")
        if isinstance(limit, (int, float)) and limit and limit > 0:
            solve_options["time_limit"] = float(limit)
        gap = options.get("mip_rel_gap")
        if isinstance(gap, (int, float)) and gap is not None:
            solve_options["mip_rel_gap"] = float(gap)

    try:
        result = milp(
            c=cost, constraints=constraints,
            integrality=[1] * total_variables,
            bounds=Bounds([0.0] * total_variables, [1.0] * total_variables),
            options=solve_options,
        )
    except Exception as exc:  # solver 自身抛错 ⇒ solver_error
        return {
            "solver": {
                **empty_solver_block(
                    stage=stage, status="solver_error",
                    message=f"solver_exception:{type(exc).__name__}:{exc}",
                ),
                "variable_count": total_variables,
                "constraint_count": len(lower),
                "objective_name": objective_name,
            },
            "selected_panel_indices": [],
            "selected_panel_ids": [],
            "selected_y": {},
            "panel_count": None,
            "radar_ii_panel_count": None,
            "objective_name": objective_name,
            "variable_count": total_variables,
            "constraint_count": len(lower),
            "infeasibility_evidence": None,
        }
    status = getattr(result, "status", None)
    success = bool(getattr(result, "success", False))
    message = str(getattr(result, "message", "") or "")
    if status == 0 and success:
        mapped = "optimal"
    elif status == 2:
        mapped = "infeasible"
    elif status == 1:
        mapped = "iteration_limit" if "iteration" in message.lower() else "time_limit"
    elif status == 3:
        mapped = "node_limit"
    elif status == 4:
        mapped = "solver_error"
    else:  # pragma: no cover - defensive
        mapped = "solver_error"

    solution = getattr(result, "x", None)
    selected_local, selected_y = [], {}
    if mapped == "optimal" and solution is not None:
        for local, value in enumerate(solution):
            if local < len(active_panels):
                if round(float(value)) == 1:
                    selected_local.append(local)
            else:
                tower_id, sample_index = y_variables[local - len(active_panels)]
                selected_y[f"{tower_id}@{sample_index}"] = int(round(float(value)))
        selected_local.sort()

    return {
        "solver": {
            "name": SOLVER_NAME, "library": SOLVER_LIBRARY, "stage": stage,
            "status": mapped,
            "optimality_proven": mapped == "optimal",
            "infeasibility_proven": mapped == "infeasible",
            "mip_gap": _mip_gap(result) if mapped == "optimal" else None,
            "objective": (
                float(getattr(result, "fun")) if getattr(result, "fun") is not None else None
            ),
            "message": message,
            "greedy_fallback_used": False,
            "variable_count": total_variables,
            "constraint_count": len(lower),
            "panel_variable_count": len(active_panels),
            "site_coverage_variable_count": len(y_variables),
            "objective_name": objective_name,
            "lexicographic_constraint": {
                "total_panel_count_fixed_to": float(total_panel_count),
            },
        },
        "selected_panel_indices": [panel_indices[local] for local in selected_local],
        "selected_panel_ids": [active_panels[local]["panel_id"] for local in selected_local],
        "selected_y": selected_y,
        "panel_count": len(selected_local) if mapped == "optimal" else None,
        "radar_ii_panel_count": (
            sum(1 for local in selected_local if active_panels[local]["radar_type"] == RADAR_TYPE_II)
            if mapped == "optimal" else None
        ),
        "objective_name": objective_name,
        "variable_count": total_variables,
        "constraint_count": len(lower),
        "infeasibility_evidence": None,
    }


def solve_with_refinement_constraints(*, panels, samples, required_counts, extra_requirements,
                                      radar_types=None,
                                      max_panels_per_tower=MAX_PANELS_PER_TOWER,
                                      options=None, stage=None):
    """带"复核补点"附加约束的求解。

    ``extra_requirements`` 是 ``{sample_index: required_distinct_site_count}``；这些
    sample 已经包含在 ``samples`` 中（复核点被并入优化输入），因此这里只需要覆盖
    默认的 ``required_counts``。该函数存在的意义是把「25m 初解 → 5m 复核 → 回到优化
    约束重解」这条链路的语义**显式化**并保持可测试。

    实现上等价于把 ``required_counts`` 与 ``extra_requirements`` 逐 sample 取最大
    （更严格者胜），不会放宽任何既有要求。
    """

    merged = []
    for index in range(len(samples)):
        base = required_counts[index] if index < len(required_counts) else None
        extra = (extra_requirements or {}).get(index)
        if base is None:
            merged.append(extra)
            continue
        if extra is None:
            merged.append(base)
            continue
        merged.append(max(int(base), int(extra)))
    result = solve(
        panels=panels, samples=samples, required_counts=merged, radar_types=radar_types,
        max_panels_per_tower=max_panels_per_tower,
        objectives=("total_panel_count",), stage=stage, options=options,
    )
    result["merged_required_counts"] = merged
    return result


__all__ = [
    "INCOMPLETE_STATUSES", "OBJECTIVES", "SOLVER_LIBRARY", "SOLVER_NAME", "SOLVER_STATUSES",
    "build_index", "candidate_distinct_site_count", "empty_solver_block",
    "presolve_infeasibility", "solve", "solve_with_refinement_constraints",
    "solve_with_total_panel_count", "solver_available", "solver_backend", "tower_coverage",
]
