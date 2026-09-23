"""Radar Surveillance Layout V1.1 — 算法编排（两阶段 MILP + 连续覆盖复核）。

流程
----

1. **航路采样**：沿实际 operational route 里程按 ``optimization_sample_spacing_m = 25``
   采样（不是"每个 MH/T 栅格取一个点"；MH/T 网格不作为雷达覆盖离散基础）；
   **V1.1：每个 sample 的 ``egm2008_m`` 恒为固定高度层 ALT-080 的 80.0 m**
   （BUG-RADAR-ALT-001：FABDEM 地面正高不再被当作航路高度）；
2. **surface_class**：每个 sample 在**自己的真实位置**上独立得到
   ``land | coastal_uncertain | sea | unknown`` 与 ``required_distinct_site_count``
   （land=2、coastal_uncertain=2（按 land）、sea=1、unknown fail-closed）；
3. **候选 panel**：见 :mod:`.candidates`（range/elevation 筛选 → bearing 派生方向 →
   去重 → dominated 剪枝，确定性）；
4. **求解前不可行检查**：``candidate_distinct_site_count`` 不足的 sample 直接形成
   结构化 infeasibility evidence；
5. **Stage A**：只用 Radar-I，``min total_panel_count``；
6. **Stage B**：**只有** Stage A 被 solver 明确证明 ``infeasible`` 才进入；
   B1 ``min total_panel_count``，B2 固定 ``N*`` 后 ``min Radar-II panel count``
   （词典序：总面阵数恒为第一目标）；
7. **连续覆盖复核**：``validation_sample_spacing_m = 5`` 对整条航路**重新生成真实 5 m
   位置**并**再次独立分类**后复核（禁止最近 25 m 点分类继承，BUG-RADAR-REFINE-003）；
   发现 ``actual_distinct_site_count < required`` 的点即并入优化输入重新求解；
   最多 ``max_refinement_rounds = 3`` 轮。

俯仰语义（V1.1，BUG-RADAR-ELEV-002）：``vertical_delta_m = sample − radar_origin``，
目标高于雷达 ⇒ 正仰角；低于雷达 ⇒ 负仰角（不下倾，不覆盖）。

最终业务事实（每个 sample 的 ``actual_distinct_site_count``）一律**根据 selected
panels 重新计算**，绝不直接使用 MILP 的辅助变量 ``y``。
"""

from __future__ import annotations

from copy import deepcopy

from ...domain.radar_surveillance_layout import (
    ALGORITHM_ID, ALGORITHM_NAME, ALGORITHM_SEMANTICS, ALGORITHM_VERSION,
    FIXED_ALTITUDE_LAYER_ID, FIXED_ALTITUDE_M, MAX_PANELS_PER_TOWER, METRIC_CRS,
    MODEL_SCOPE, NOT_EVALUATED, RADAR_TYPES, RADAR_TYPE_I, RADAR_TYPE_II,
    REQUIRED_DISTINCT_SITE_COUNT, ROUTE_SAMPLE_HEIGHT_SEMANTICS, SCHEMA_VERSION,
    SEMANTICS_FINGERPRINT, SURFACE_CLASSES, VERTICAL_REFERENCE,
    radar_geometry_parameters,
)
from . import milp as milp_module
from .candidates import build_candidates
from .geometry import (
    interpolate_metric_path, panel_coverage, plane_intersection_radii_m,
    sample_offsets_m,
)

#: 软件 baseline 参数（显式，进入 provenance）。
SOFTWARE_BASELINE = {
    "optimization_sample_spacing_m": 25.0,
    "validation_sample_spacing_m": 5.0,
    "max_refinement_rounds": 3,
    "max_panels_per_tower": MAX_PANELS_PER_TOWER,
    "source": "cns_planner_software_algorithm_baseline",
    "parameter_origin": "software_baseline",
    "engineering_confirmed": False,
}

#: 求解阶段标识。
STAGE_I_ONLY = "radar_i_only"
STAGE_MIXED = "radar_i_plus_radar_ii"

STAGE_LABELS = {
    STAGE_I_ONLY: "Stage A：仅 Radar-I",
    STAGE_MIXED: "Stage B：Radar-I + Radar-II（I-only 已被证明不可行）",
}

#: 结果状态词表。
LAYOUT_STATUSES = (
    "optimal_coverage",
    "infeasible",
    "refinement_incomplete",
    "search_incomplete",
    "solver_error",
    "solver_unavailable",
    "unresolved",
    "not_ready",
)

#: 每个 sample 的覆盖判定结果。
SAMPLE_COVERAGE_STATUSES = ("satisfied", "under_redundant", "uncovered", "unknown")

#: 连续段分类。
SEGMENT_IDS = ("uncovered", "under_redundant", "unknown")


# ------------------------------------------------------------------------------ sampling


def _surface_class_of(sample):
    value = str(sample.get("surface_class") or "unknown")
    return value if value in SURFACE_CLASSES else "unknown"


def required_count_for(surface_class):
    """``land`` ⇒ 2、``sea`` ⇒ 1、``coastal_uncertain`` ⇒ 2（按 land 处理）、
    ``unknown`` ⇒ ``None``（fail-closed）。"""

    return REQUIRED_DISTINCT_SITE_COUNT.get(str(surface_class or "unknown"))


def build_route_samples(*, metric_path, spacing_m, route_id,
                        fixed_altitude_m=FIXED_ALTITUDE_M,
                        surface_by_offset=None, surface_resolver=None,
                        sample_id_prefix="O", coordinate_resolver=None):
    """沿米制航路里程采样并逐点取高度、地表分类与（可选）地理坐标。

    高度语义（V1.1，BUG-RADAR-ALT-001）
    -----------------------------------

    航路是 **80 m 固定巡航高度航路**，因此 ``sample.egm2008_m`` **恒等于**
    ``fixed_altitude_m``（默认 ``80.0``）。FABDEM 地面正高**绝不**再被当作航路高度：
    "route terrain elevation" 这个错误输入通道已从本函数签名中删除。

    ``surface_by_offset`` 是回调：``offset_m -> surface_class``（25 m 优化 / 5 m 复核
    各自独立的采样回调；调用方必须为两套采样**分别**建立回调，禁止以"最近 25 m 点"
    继承分类）。
    ``surface_resolver`` 是可选回调：``(index, offset_m, metric) -> 分类明细 dict``，
    给定时其 ``surface_class`` / ``required_distinct_site_count`` 优先于
    ``surface_by_offset``（用于逐个真实采样点独立分类的场景）。

    ``coordinate_resolver`` 是**可选**回调：``[x_m, y_m] -> [lon, lat]``。算法本身不
    做投影；这里只是把调用方提供的投影结果原样记录到 sample 上（供地图使用），
    绝不参与任何距离/角度计算。
    """

    points = interpolate_metric_path(metric_path, spacing_m)
    offsets = sample_offsets_m(metric_path, spacing_m)
    samples, unresolved = [], []
    for index, (point, offset) in enumerate(zip(points, offsets)):
        # V1.1：航路高度是固定高度层常量，与任何地形采样无关。
        altitude = float(fixed_altitude_m)
        detail = None
        if callable(surface_resolver):
            try:
                detail = surface_resolver(index, offset, [float(point[0]), float(point[1])])
            except Exception:
                detail = None
        if isinstance(detail, dict) and detail.get("surface_class") is not None:
            surface_class = _surface_class_of(detail)
            required = detail.get("required_distinct_site_count")
            if required is None and detail.get("effective_requirement_class") is not None:
                required = required_count_for(detail.get("effective_requirement_class"))
        else:
            surface_class = _surface_class_of(
                {"surface_class": surface_by_offset(offset) if callable(surface_by_offset) else None}
            )
            required = required_count_for(surface_class)
        longitude = latitude = None
        if callable(coordinate_resolver):
            try:
                geographic = coordinate_resolver([float(point[0]), float(point[1])])
                longitude, latitude = float(geographic[0]), float(geographic[1])
            except Exception:
                longitude = latitude = None
        samples.append({
            "sample_index": len(samples),
            "sample_id": f"{sample_id_prefix}{index:06d}",
            "geometry_index": index,
            "distance_along_route_m": float(offset),
            "metric": [float(point[0]), float(point[1])],
            "longitude": longitude,
            "latitude": latitude,
            "egm2008_m": altitude,
            "surface_class": surface_class,
            "required_distinct_site_count": required,
            "refinement": False,
        })
    return {
        "samples": samples,
        # V1.1：高度恒为固定层，因此"缺少 EGM2008 正高"这一失败模式已不存在。
        "unresolved_samples": unresolved,
        "spacing_m": float(spacing_m),
        "route_length_m": float(offsets[-1]) if offsets else 0.0,
        "fixed_altitude_m": float(fixed_altitude_m),
        "altitude_semantics": ROUTE_SAMPLE_HEIGHT_SEMANTICS,
        "terrain_elevation_used_as_route_height": False,
    }


# ------------------------------------------------------------------------------ coverage


def actual_site_coverage(*, panels, samples, selected_panel_ids, tower_records):
    """根据 **selected panels** 重算每个 sample 的实际独立站址覆盖。

    这是最终业务事实；MILP 的 ``y`` 变量只作线性化辅助，绝不作为结论。
    """

    selected = [
        panel for panel in panels if panel["panel_id"] in set(selected_panel_ids)
    ]
    tower_by_id = {tower["tower_id"]: tower for tower in tower_records}
    per_sample = []
    for sample in samples:
        sites, matching = set(), []
        for panel in selected:
            tower = tower_by_id.get(str(panel["tower_id"]))
            if tower is None:
                continue
            geometry = panel_coverage(
                origin_egm2008_m=tower["origin_egm2008_m"],
                sample_egm2008_m=sample["egm2008_m"],
                sample_metric=sample["metric"],
                tower_metric=tower["metric"],
                panel_azimuth_deg=panel["azimuth_deg"],
                parameters=radar_geometry_parameters(panel["radar_type"]),
            )
            if not geometry["covered"]:
                continue
            sites.add(str(panel["tower_id"]))
            matching.append({
                "panel_id": panel["panel_id"],
                "tower_id": panel["tower_id"],
                "radar_type": panel["radar_type"],
                "azimuth_deg": panel["azimuth_deg"],
                "slant_distance_m": geometry["slant_distance_m"],
                "elevation_deg": geometry["elevation_deg"],
                "azimuth_delta_deg": geometry["azimuth_delta_deg"],
            })
        required = sample.get("required_distinct_site_count")
        count = len(sites)
        if required is None:
            status = "unknown"
        elif count == 0:
            status = "uncovered"
        elif count < int(required):
            status = "under_redundant"
        else:
            status = "satisfied"
        per_sample.append({
            "sample_index": sample["sample_index"],
            "sample_id": sample["sample_id"],
            "distance_along_route_m": sample["distance_along_route_m"],
            "metric": list(sample.get("metric") or []),
            "egm2008_m": sample.get("egm2008_m"),
            "longitude": sample.get("longitude"),
            "latitude": sample.get("latitude"),
            "surface_class": sample["surface_class"],
            "required_distinct_site_count": required,
            "actual_distinct_site_count": count,
            "distinct_site_ids": sorted(sites),
            "status": status,
            "panels": matching,
            "refinement": bool(sample.get("refinement")),
        })
    return per_sample


def _segments(per_sample, predicate, *, segment_id):
    """连续段合并（按沿里程一维投影，确定性）。"""

    groups = []
    for item in per_sample:
        if not predicate(item):
            continue
        if groups:
            last = groups[-1]
            if abs(
                float(last["route_offset_end_m"]) - float(item["distance_along_route_m"])
            ) <= 1e-6:
                last["route_offset_end_m"] = item["distance_along_route_m"]
                last["sample_count"] += 1
                last["sample_ids"].append(item["sample_id"])
                continue
        groups.append({
            "segment_id": segment_id,
            "route_offset_start_m": item["distance_along_route_m"],
            "route_offset_end_m": item["distance_along_route_m"],
            "sample_count": 1,
            "sample_ids": [item["sample_id"]],
            "min_actual_distinct_site_count": item["actual_distinct_site_count"],
            "required_distinct_site_count": item["required_distinct_site_count"],
        })
    for group in groups:
        group["length_m"] = (
            float(group["route_offset_end_m"]) - float(group["route_offset_start_m"])
        )
    return groups


def validation_report(*, per_sample, route_length_m, spacing_m=None):
    """对整条航路做独立覆盖复核（不参与优化，只报告事实）。

    长度按**区间**累计：每个 sample 代表以自身里程为中心、宽度为采样间距的一个区间
    （首尾为半区间），因此长度守恒且不随"样本个数"漂移；补点后的不等距采样同样正确。
    """

    effective_spacing = float(
        spacing_m or SOFTWARE_BASELINE["validation_sample_spacing_m"]
    )

    def lengths_of(items):
        """返回 ``(total_m, satisfied_m, minimum_satisfied_step_m)``。"""

        if not items:
            return 0.0, 0.0, None
        ordered = sorted(items, key=lambda item: float(item["distance_along_route_m"]))
        total, satisfied = 0.0, 0.0
        for index, item in enumerate(ordered):
            if index + 1 < len(ordered):
                span = float(
                    ordered[index + 1]["distance_along_route_m"]
                ) - float(item["distance_along_route_m"])
            else:
                span = effective_spacing / 2.0
            if index == 0:
                span += effective_spacing / 2.0
            total += span
            if item["status"] == "satisfied":
                satisfied += span
        return total, satisfied, None

    summary = {}
    for surface_class in SURFACE_CLASSES:
        items = [item for item in per_sample if item["surface_class"] == surface_class]
        total, satisfied, _ = lengths_of(items)
        summary[surface_class] = {
            "sample_count": len(items),
            "length_m": total,
            "satisfied_length_m": satisfied,
            "satisfied_fraction": (satisfied / total) if total > 0 else None,
            "minimum_distinct_site_count": (
                min((item["actual_distinct_site_count"] for item in items), default=None)
            ),
            "required_distinct_site_count": (
                REQUIRED_DISTINCT_SITE_COUNT[surface_class]
            ),
            "under_redundant_sample_count": sum(
                1 for item in items if item["status"] == "under_redundant"
            ),
            "uncovered_sample_count": sum(
                1 for item in items if item["status"] == "uncovered"
            ),
            "unknown_sample_count": sum(
                1 for item in items if item["status"] == "unknown"
            ),
        }

    uncovered = _segments(
        per_sample, lambda item: item["status"] == "uncovered", segment_id="uncovered",
    )
    under = _segments(
        per_sample, lambda item: item["status"] == "under_redundant",
        segment_id="under_redundant",
    )
    unknown = _segments(
        per_sample, lambda item: item["status"] == "unknown", segment_id="unknown",
    )
    violations = [
        {
            "sample_id": item["sample_id"],
            "sample_index": item["sample_index"],
            "distance_along_route_m": item["distance_along_route_m"],
            "surface_class": item["surface_class"],
            "required_distinct_site_count": item["required_distinct_site_count"],
            "actual_distinct_site_count": item["actual_distinct_site_count"],
            "status": item["status"],
        }
        for item in per_sample
        if item["status"] in ("uncovered", "under_redundant")
    ]
    unknown_items = [
        {
            "sample_id": item["sample_id"],
            "sample_index": item["sample_index"],
            "distance_along_route_m": item["distance_along_route_m"],
            "surface_class": item["surface_class"],
            "actual_distinct_site_count": item["actual_distinct_site_count"],
            "reason": "surface_class_unknown_required_distinct_site_count_unknown",
        }
        for item in per_sample
        if item["status"] == "unknown"
    ]
    return {
        "route_length_m": route_length_m,
        "land": summary["land"],
        "sea": summary["sea"],
        "unknown": summary["unknown"],
        # V1.1：海岸不确定带单独报告（按 land 处理，要求 2 个站址），
        # 绝不被并入 sea，也绝不被当成"已确认的陆地"。
        "coastal_uncertain": summary["coastal_uncertain"],
        "surface_class_semantics": (
            "coastal_uncertain_is_treated_as_land_with_required_distinct_site_count_2"
        ),
        "uncovered_segments": uncovered,
        "under_redundant_segments": under,
        "unknown_segments": unknown,
        "violations": violations,
        "violation_count": len(violations),
        "unknown_evidence": unknown_items,
        "unknown_evidence_count": len(unknown_items),
        "validated": not violations and not unknown_items,
        "validation_sample_spacing_m": effective_spacing,
        "semantics": "independent_continuous_coverage_review_over_the_whole_route",
        "classification_independence": (
            "each_validation_sample_classified_at_its_own_real_position_no_nearest_inheritance"
        ),
    }


# ------------------------------------------------------------------------------ solving


def _sample_inputs(samples):
    return [
        {
            "sample_id": sample["sample_id"],
            "metric": sample["metric"],
            "egm2008_m": sample["egm2008_m"],
            "distance_along_route_m": sample["distance_along_route_m"],
            "surface_class": sample["surface_class"],
            "required_distinct_site_count": sample["required_distinct_site_count"],
        }
        for sample in samples
    ]


def _required_counts(samples):
    return [sample.get("required_distinct_site_count") for sample in samples]


def _stage_a_i_only(*, towers, samples, options):
    """Stage A：只用 Radar-I。"""

    candidates = build_candidates(towers=towers, samples=_sample_inputs(samples))
    presolve = milp_module.presolve_infeasibility(
        panels=candidates["panels"], samples=candidates["samples"],
        required_counts=_required_counts(samples), radar_types=[RADAR_TYPE_I],
    )
    if presolve["insufficient_sample_count"]:
        return {
            "stage": STAGE_I_ONLY,
            "candidates": candidates,
            "presolve": presolve,
            "solve": {
                "solver": milp_module.empty_solver_block(
                    stage=STAGE_I_ONLY, status="infeasible", proven=True,
                    message=(
                        "求解前结构化检查已证明 I-only 不可行："
                        "存在 sample 的 Radar-I 候选独立站址数少于要求数量"
                    ),
                ),
                "selected_panel_ids": [],
                "panel_count": None,
                "radar_ii_panel_count": 0,
                "selected_panel_indices": [],
                "selected_y": {},
            },
            "structural_infeasibility": True,
        }
    solve_result = milp_module.solve(
        panels=candidates["panels"], samples=candidates["samples"],
        required_counts=_required_counts(samples), radar_types=[RADAR_TYPE_I],
        objectives=("total_panel_count",), stage=STAGE_I_ONLY, options=options,
    )
    return {
        "stage": STAGE_I_ONLY,
        "candidates": candidates,
        "presolve": presolve,
        "solve": solve_result,
        "structural_infeasibility": False,
    }


def _stage_b_mixed(*, towers, samples, options):
    """Stage B：Radar-I + Radar-II，词典序两阶段求解。"""

    candidates = build_candidates(towers=towers, samples=_sample_inputs(samples))
    presolve = milp_module.presolve_infeasibility(
        panels=candidates["panels"], samples=candidates["samples"],
        required_counts=_required_counts(samples), radar_types=list(RADAR_TYPES),
    )
    if presolve["insufficient_sample_count"]:
        return {
            "stage": STAGE_MIXED,
            "candidates": candidates,
            "presolve": presolve,
            "b1": {
                "solver": milp_module.empty_solver_block(
                    stage=f"{STAGE_MIXED}:b1", status="infeasible", proven=True,
                    message=(
                        "求解前结构化检查已证明 mixed 也不可行："
                        "存在 sample 的 I+II 候选独立站址数少于要求数量"
                    ),
                ),
                "selected_panel_ids": [],
                "panel_count": None,
                "radar_ii_panel_count": None,
                "selected_panel_indices": [],
                "selected_y": {},
            },
            "b2": None,
            "structural_infeasibility": True,
        }
    b1 = milp_module.solve(
        panels=candidates["panels"], samples=candidates["samples"],
        required_counts=_required_counts(samples), radar_types=list(RADAR_TYPES),
        objectives=("total_panel_count",), stage=f"{STAGE_MIXED}:b1", options=options,
    )
    b2 = None
    if b1["solver"]["status"] == "optimal":
        b2 = milp_module.solve_with_total_panel_count(
            panels=candidates["panels"], samples=candidates["samples"],
            required_counts=_required_counts(samples),
            total_panel_count=b1["panel_count"],
            radar_types=list(RADAR_TYPES), options=options, stage=f"{STAGE_MIXED}:b2",
        )
    return {
        "stage": STAGE_MIXED,
        "candidates": candidates,
        "presolve": presolve,
        "b1": b1,
        "b2": b2,
        "structural_infeasibility": False,
    }


def _refine(*, towers, samples, options, selected_panel_ids, panels, radar_types,
            extra_requirements, stage):
    """带补点约束的重解（同一 MILP + 更严格的 sample 要求）。"""

    result = milp_module.solve_with_refinement_constraints(
        panels=panels, samples=samples,
        required_counts=_required_counts(samples),
        extra_requirements=extra_requirements, radar_types=radar_types,
        options=options, stage=stage,
    )
    return result


def solve_layout(*, towers, samples, options=None, allow_mixed=True,
                 validation_samples=None):
    """完整求解：Stage A →（必要时）Stage B → 5 m 独立连续覆盖复核与补点重解。

    ``towers``：``[{tower_id, name, metric, origin_egm2008_m, ...}]``
    ``samples``：``[{sample_id, metric, egm2008_m, distance_along_route_m,
    surface_class, required_distinct_site_count}]``（**25 m 优化采样**）

    ``validation_samples``：**可选的 5 m 独立复核采样**（同一航路、更密间距）。
    给定时，每次求得方案都会在该采样上重新判定覆盖；发现的违反点会被并入优化输入
    重新求解（``max_refinement_rounds`` 上限）。未给定时退化为在优化采样上复核
    （此时新增点集合可能为空，循环会安全终止）。

    **V1.1 明确约束（BUG-RADAR-REFINE-003）**：``validation_samples`` 必须是
    **重新生成的、位于真实 5 m 位置**的采样点，其 ``surface_class`` /
    ``required_distinct_site_count`` 必须由调用方在该真实位置上**独立执行**
    land/sea/coastal 分类得到。禁止使用"最近 25 m 点继承分类"，也禁止在
    ``solve_layout`` 内做任何最近邻 surface 传播 —— 本函数只消费传入的分类结果。
    """

    base_samples = [deepcopy(sample) for sample in samples]
    if not towers:
        return _blocked_result(
            status="not_ready", reason="没有可用的铁塔候选站址",
            samples=base_samples,
        )
    if not base_samples:
        return _blocked_result(
            status="not_ready", reason="没有可用的航路采样点",
            samples=base_samples,
        )

    solve_options = {"presolve": True}
    if isinstance(options, dict):
        if isinstance(options.get("time_limit_s"), (int, float)) and options["time_limit_s"]:
            solve_options["time_limit"] = float(options["time_limit_s"])
        if isinstance(options.get("mip_rel_gap"), (int, float)):
            solve_options["mip_rel_gap"] = float(options["mip_rel_gap"])

    current_samples = base_samples
    refinement_rounds = []
    stage_used = None
    final = None
    extra_requirements = {}

    def run_pipeline(active_samples, extra):
        """跑一次完整两阶段管线，返回统一形状的 ``outcome``。

        统一形状：``outcome["solve"]`` 恒存在（Stage B 时等同于 B1，或补点重解的结果），
        因此上层可以无分支地读取 ``selected_panel_ids`` / ``solver``。
        """

        stage_a = _stage_a_i_only(towers=towers, samples=active_samples, options=solve_options)
        if stage_a["solve"]["solver"]["status"] == "optimal":
            if extra:
                # 补点约束下用 I-only 重解（Stage A 的语义必须保持"只用 I 型"）。
                refined = _refine(
                    towers=towers, samples=active_samples, options=solve_options,
                    selected_panel_ids=stage_a["solve"]["selected_panel_ids"],
                    panels=stage_a["candidates"]["panels"], radar_types=[RADAR_TYPE_I],
                    extra_requirements=extra, stage=f"{STAGE_I_ONLY}:refinement",
                )
                return {
                    "stage": STAGE_I_ONLY,
                    "solve": refined,
                    "candidates": stage_a["candidates"],
                    "presolve": stage_a["presolve"],
                    "structural_infeasibility": False,
                }
            return stage_a
        if stage_a["solve"]["solver"]["status"] == "infeasible":
            if not allow_mixed:
                return stage_a
            stage_b = _stage_b_mixed(towers=towers, samples=active_samples, options=solve_options)
            # 统一形状：``solve`` 恒存在（B1 或补点重解的结果），因此上层无分支可读。
            source = stage_b.get("b2") or stage_b["b1"]
            if stage_b["b1"]["solver"]["status"] == "optimal" and extra:
                refined = milp_module.solve_with_total_panel_count(
                    panels=stage_b["candidates"]["panels"],
                    samples=stage_b["candidates"]["samples"],
                    required_counts=[
                        (
                            max(
                                sample.get("required_distinct_site_count") or 0,
                                extra.get(index, 0),
                            )
                            if sample.get("required_distinct_site_count") is not None
                            else extra.get(index)
                        )
                        for index, sample in enumerate(stage_b["candidates"]["samples"])
                    ],
                    total_panel_count=source["panel_count"],
                    radar_types=list(RADAR_TYPES), options=solve_options,
                    stage=f"{STAGE_MIXED}:refinement",
                )
                stage_b = {**stage_b, "solve": refined, "refinement": refined}
            else:
                stage_b = {**stage_b, "solve": source}
            stage_b["stage"] = STAGE_MIXED
            return stage_b
        # Stage A 既未证明最优也未证明不可行 ⇒ search_incomplete / solver_error。
        return stage_a

    for round_index in range(int(SOFTWARE_BASELINE["max_refinement_rounds"]) + 1):
        outcome = run_pipeline(current_samples, extra_requirements)
        stage_used = outcome["stage"]
        final = outcome
        panel_ids = outcome["solve"].get("selected_panel_ids") or []
        solver_status = outcome["solve"]["solver"]["status"]
        if solver_status != "optimal":
            break

        coverage = actual_site_coverage(
            panels=outcome["candidates"]["panels"], samples=outcome["candidates"]["samples"],
            selected_panel_ids=panel_ids, tower_records=outcome["candidates"]["towers"],
        )
        reported = (
            actual_site_coverage(
                panels=outcome["candidates"]["panels"], samples=validation_samples,
                selected_panel_ids=panel_ids, tower_records=outcome["candidates"]["towers"],
            )
            if validation_samples else coverage
        )
        violations = _violations(reported)
        refinement_rounds.append({
            "round_index": round_index,
            "stage": stage_used,
            "spacing_m": SOFTWARE_BASELINE["validation_sample_spacing_m"],
            "panel_count": outcome["solve"].get("panel_count"),
            "violation_count": len(violations),
            "violation_sample_ids": [item["sample_id"] for item in violations],
            "reviewed_sample_count": len(reported),
            "semantics": (
                "initial_optimization_then_independent_validation_spacing_review"
                if round_index == 0 else "re_solve_with_validation_violations_added"
            ),
        })
        if not violations:
            break
        if round_index >= int(SOFTWARE_BASELINE["max_refinement_rounds"]):
            break
        # 把复核发现的新点加入优化输入（保留原 25 m 采样点，追加复核点）。
        added, seen = [], {
            (round(item["metric"][0], 6), round(item["metric"][1], 6))
            for item in current_samples
        }
        for item in violations:
            metric = _sample_metric(validation_samples, item["sample_index"]) if (
                validation_samples
            ) else _sample_metric(current_samples, item["sample_index"])
            if metric is None:
                continue
            key = (round(metric[0], 6), round(metric[1], 6))
            if key in seen:
                continue
            seen.add(key)
            source = (
                validation_samples[item["sample_index"]] if validation_samples
                else current_samples[item["sample_index"]]
            )
            sample = deepcopy(source)
            sample["sample_id"] = f"R{item['sample_index']:06d}"
            sample["refinement"] = True
            added.append(sample)
        if not added:
            break
        current_samples = current_samples + added
        extra_requirements = {}
        for index, sample in enumerate(current_samples):
            required = sample.get("required_distinct_site_count")
            if required is None:
                continue
            extra_requirements[index] = int(required)

    solve_block = final["solve"]["solver"] if final else milp_module.empty_solver_block()
    status, message = _final_status(
        final=final, refinement_rounds=refinement_rounds,
    )

    per_sample = []
    if final is not None and final["solve"].get("selected_panel_ids") is not None:
        per_sample = actual_site_coverage(
            panels=final["candidates"]["panels"],
            samples=final["candidates"]["samples"],
            selected_panel_ids=final["solve"].get("selected_panel_ids") or [],
            tower_records=final["candidates"]["towers"],
        )

    return _assemble_result(
        status=status, message=message, stage=stage_used, solve_block=solve_block,
        final=final, per_sample=per_sample, refinement_rounds=refinement_rounds,
        samples=samples, towers=towers,
        optimisation_samples=current_samples,
    )


def _sample_metric(samples, index):
    if samples and 0 <= index < len(samples):
        return samples[index].get("metric")
    return None


def _violations(coverage):
    return [
        item for item in coverage
        if item["status"] in ("uncovered", "under_redundant")
    ]


def _final_status(*, final, refinement_rounds):
    if final is None:
        return "unresolved", "求解管线未产生结果"
    stage = final["stage"]
    solver = final["solve"]["solver"]
    solver_status = solver["status"]
    if solver_status == "solver_unavailable":
        return "solver_unavailable", solver.get("message")
    if solver_status == "solver_error":
        return "solver_error", solver.get("message")
    if solver_status in milp_module.INCOMPLETE_STATUSES:
        return "search_incomplete", (
            f"{stage} 搜索未完成（{solver_status}）：可达性与最优性均未证明，"
            "绝不等同于该型号不可行，也不自动进入下一阶段"
        )
    if solver_status == "infeasible":
        return "infeasible", (
            f"{stage} 由 solver 明确证明不可行（infeasible）"
            if stage == STAGE_MIXED
            else "Radar-I only 由 solver 明确证明不可行（infeasible）"
        )
    # optimal
    last = refinement_rounds[-1] if refinement_rounds else None
    if last is not None and last["violation_count"] > 0:
        return "refinement_incomplete", (
            f"已完成 {len(refinement_rounds) - 1} 轮补点重解（上限 "
            f"{SOFTWARE_BASELINE['max_refinement_rounds']} 轮），"
            f"仍有 {last['violation_count']} 个复核点未满足要求："
            "不得报告为完整覆盖"
        )
    return "optimal_coverage", (
        f"{stage} 取得 optimal，且 "
        f"{SOFTWARE_BASELINE['validation_sample_spacing_m']}m 独立复核无违反点"
    )


def _blocked_result(*, status, reason, samples):
    return {
        "status": status,
        "message": reason,
        "solver": milp_module.empty_solver_block(status="not_run"),
        "selected_panels": [],
        "selected_panel_count": None,
        "validation": None,
        "refinement_rounds": [],
    }


def _assemble_result(*, status, message, stage, solve_block, final, per_sample,
                     refinement_rounds, samples, towers, optimisation_samples=None):
    candidates = final["candidates"] if final else {"panels": [], "towers": [], "samples": []}
    selected_ids = set(final["solve"].get("selected_panel_ids") or []) if final else set()
    selected = [panel for panel in candidates["panels"] if panel["panel_id"] in selected_ids]
    tower_by_id = {tower["tower_id"]: tower for tower in candidates["towers"]}

    selected_panels = []
    for panel in sorted(selected, key=lambda item: (item["tower_id"], item["radar_type"], item["azimuth_deg"])):
        covered = [
            item for item in per_sample
            if any(entry["panel_id"] == panel["panel_id"] for entry in item["panels"])
        ]
        tower = tower_by_id.get(str(panel["tower_id"])) or {}
        origin_egm2008_m = tower.get("origin_egm2008_m")
        # V1.1（BUG-RADAR-OVERLAY-005）：斜距是 slant range，前端绝不能再把它当作
        # 80 m 平面的水平半径。这里由**后端**给出该站址与固定高度平面的真实交截半径。
        plane = (
            plane_intersection_radii_m(
                origin_egm2008_m=origin_egm2008_m,
                plane_egm2008_m=FIXED_ALTITUDE_M,
                parameters=radar_geometry_parameters(panel["radar_type"]),
            )
            if isinstance(origin_egm2008_m, (int, float))
            and not isinstance(origin_egm2008_m, bool)
            else None
        )
        selected_panels.append({
            "panel_id": panel["panel_id"],
            "tower_id": panel["tower_id"],
            "site_id": f"tower:{panel['tower_id']}",
            "tower_name": tower.get("name"),
            "longitude": tower.get("longitude"),
            "latitude": tower.get("latitude"),
            "radar_type": panel["radar_type"],
            "radar_type_label": radar_geometry_parameters(panel["radar_type"])["label"],
            "azimuth_deg": panel["azimuth_deg"],
            "panel_half_width_deg": panel["panel_half_width_deg"],
            "radar_origin_egm2008_m": origin_egm2008_m,
            # 80 m 平面交截几何（唯一允许前端消费的水平半径来源）。
            "altitude_plane_egm2008_m": FIXED_ALTITUDE_M,
            "altitude_plane_geometry": deepcopy(plane),
            "horizontal_inner_radius_m": (plane or {}).get("horizontal_inner_radius_m"),
            "horizontal_outer_radius_m": (plane or {}).get("horizontal_outer_radius_m"),
            "plane_intersection_status": (plane or {}).get("plane_intersection_status"),
            "slant_range_semantics": "slant",
            "slant_range_preset_m": {
                "min_slant_range_m": radar_geometry_parameters(
                    panel["radar_type"]
                )["min_slant_range_m"],
                "max_slant_range_m": radar_geometry_parameters(
                    panel["radar_type"]
                )["max_slant_range_m"],
            },
            "coverage": {
                "sample_count": len(covered),
                "satisfied_sample_count": sum(1 for item in covered if item["status"] == "satisfied"),
                "covered_length_m": _covered_length(covered),
                "first_distance_along_route_m": (
                    covered[0]["distance_along_route_m"] if covered else None
                ),
                "last_distance_along_route_m": (
                    covered[-1]["distance_along_route_m"] if covered else None
                ),
            },
        })

    route_length = per_sample[-1]["distance_along_route_m"] if per_sample else 0.0
    validation = validation_report(
        per_sample=per_sample, route_length_m=route_length,
        spacing_m=SOFTWARE_BASELINE["validation_sample_spacing_m"],
    )
    validation["samples"] = deepcopy(per_sample)
    validation["coverage_profile"] = coverage_profile(per_sample)
    radar_i_count = sum(1 for item in selected_panels if item["radar_type"] == RADAR_TYPE_I)
    radar_ii_count = sum(1 for item in selected_panels if item["radar_type"] == RADAR_TYPE_II)
    selected_tower_ids = sorted({item["tower_id"] for item in selected_panels})

    return {
        "status": status,
        "message": message,
        "stage": stage,
        "stage_label": STAGE_LABELS.get(stage),
        "solver": solve_block,
        "selected_panels": selected_panels,
        "selected_panel_count": len(selected_panels),
        "radar_i_panel_count": radar_i_count,
        "radar_ii_panel_count": radar_ii_count,
        "selected_tower_count": len(selected_tower_ids),
        "selected_tower_ids": selected_tower_ids,
        "candidate_tower_count": len(candidates["towers"]),
        "candidate_panel_count": len(candidates["panels"]),
        "candidate_statistics": deepcopy(candidates.get("statistics") or {}),
        "unusable_towers": deepcopy(candidates.get("unusable_towers") or []),
        "presolve": deepcopy(final.get("presolve")) if final else None,
        "stage_a": (
            deepcopy(final.get("solve")) if stage == STAGE_I_ONLY and final else None
        ),
        "stage_b": (
            {
                "b1": deepcopy(final.get("b1")) if final else None,
                "b2": deepcopy(final.get("b2")) if final else None,
                "refinement": (
                    deepcopy(final.get("refinement")) if final and final.get("refinement") else None
                ),
                "authoritative": deepcopy(final.get("solve")) if final else None,
            }
            if stage == STAGE_MIXED and final else None
        ),
        "validation": validation,
        "refinement_rounds": deepcopy(refinement_rounds),
        "optimisation_samples": deepcopy(optimisation_samples or []),
        "samples": per_sample,
        "sample_count": len(per_sample),
        "not_evaluated": deepcopy(NOT_EVALUATED),
        "model_scope": MODEL_SCOPE,
    }


def coverage_profile(per_sample, *, max_entries=400):
    """按连续同状态合并的**有界**覆盖剖面（供地图按覆盖结果着色，避免逐点下发）。

    每一项是 ``{status, from_m, to_m, required, min_actual, start_coordinate, end_coordinate}``；
    超过 ``max_entries`` 时按等距抽稀并显式标记 ``truncated``，绝不静默丢段。
    """

    groups = []
    for item in per_sample:
        coordinate = [item.get("longitude"), item.get("latitude")]
        if groups and groups[-1]["status"] == item["status"]:
            last = groups[-1]
            last["to_m"] = item["distance_along_route_m"]
            last["end_coordinate"] = coordinate
            last["sample_count"] += 1
            if item["actual_distinct_site_count"] is not None:
                last["min_actual"] = (
                    item["actual_distinct_site_count"]
                    if last["min_actual"] is None
                    else min(last["min_actual"], item["actual_distinct_site_count"])
                )
            continue
        groups.append({
            "status": item["status"],
            "from_m": item["distance_along_route_m"],
            "to_m": item["distance_along_route_m"],
            "sample_count": 1,
            "required": item.get("required_distinct_site_count"),
            "min_actual": item.get("actual_distinct_site_count"),
            "start_coordinate": coordinate,
            "end_coordinate": coordinate,
        })
    truncated = False
    if max_entries and len(groups) > max_entries:
        step = len(groups) / float(max_entries)
        groups = [groups[min(len(groups) - 1, int(index * step))] for index in range(max_entries)]
        truncated = True
    return {
        "entries": groups,
        "count": len(groups),
        "truncated": truncated,
        "semantics": "contiguous_same_status_coverage_profile_along_route_distance",
    }


def _covered_length(items):
    if not items:
        return 0.0
    ordered = sorted(items, key=lambda item: item["distance_along_route_m"])
    if len(ordered) == 1:
        return float(SOFTWARE_BASELINE["validation_sample_spacing_m"])
    return float(ordered[-1]["distance_along_route_m"]) - float(ordered[0]["distance_along_route_m"])


def parameters_block(*, fixed_altitude_m=FIXED_ALTITUDE_M, extra=None):
    """写入结果的参数块（含 25m / 5m / 3 轮与俯仰·方位 preset + V1.1 语义）。"""

    block = {
        "optimization_sample_spacing_m": SOFTWARE_BASELINE["optimization_sample_spacing_m"],
        "validation_sample_spacing_m": SOFTWARE_BASELINE["validation_sample_spacing_m"],
        "max_refinement_rounds": SOFTWARE_BASELINE["max_refinement_rounds"],
        "max_panels_per_tower": SOFTWARE_BASELINE["max_panels_per_tower"],
        "fixed_altitude_layer_id": FIXED_ALTITUDE_LAYER_ID,
        "fixed_altitude_m": fixed_altitude_m,
        "vertical_reference": VERTICAL_REFERENCE,
        "metric_crs": METRIC_CRS,
        # V1.1 显式语义（同时进入 input_fingerprint，旧 V1.0 layout 因此 stale）。
        "route_altitude_semantics": SEMANTICS_FINGERPRINT["route_altitude_semantics"],
        "vertical_delta_semantics": SEMANTICS_FINGERPRINT["vertical_delta_semantics"],
        "radar_origin_semantics": SEMANTICS_FINGERPRINT["radar_origin_semantics"],
        "geometry_version": SEMANTICS_FINGERPRINT["geometry_version"],
        "route_sample_height_semantics": ROUTE_SAMPLE_HEIGHT_SEMANTICS,
        "terrain_elevation_used_as_route_height": False,
        "demo_no_down_tilt_elevation_note": (
            "0<=elevation<=45 且不下倾：目标低于雷达原点时不可覆盖"
        ),
        "elevation_preset": {
            "elevation_center_deg": 22.5,
            "elevation_min_deg": 0.0,
            "elevation_max_deg": 45.0,
        },
        "azimuth_preset": {
            "azimuth_beamwidth_deg": 90.0,
            "azimuth_half_width_deg": 45.0,
            "panel_count_per_tower_max": MAX_PANELS_PER_TOWER,
        },
        "algorithm_id": ALGORITHM_ID,
        "algorithm_version": ALGORITHM_VERSION,
        "algorithm_name": ALGORITHM_NAME,
        "algorithm_semantics": ALGORITHM_SEMANTICS,
        "schema_version": SCHEMA_VERSION,
        "model_scope": MODEL_SCOPE,
        "semantics_fingerprint": deepcopy(SEMANTICS_FINGERPRINT),
        "software_baseline": deepcopy(SOFTWARE_BASELINE),
    }
    if isinstance(extra, dict):
        block.update(extra)
    return block


__all__ = [
    "LAYOUT_STATUSES", "SAMPLE_COVERAGE_STATUSES", "SEGMENT_IDS", "SOFTWARE_BASELINE",
    "STAGE_I_ONLY", "STAGE_LABELS", "STAGE_MIXED",
    "actual_site_coverage", "build_route_samples", "coverage_profile", "parameters_block",
    "required_count_for", "solve_layout", "validation_report",
]
