"""只读 Terrain + Building 联合 LOS 诊断的证据契约（Round34-T07-I2）。

纯函数、无 IO、**无 ProjectState writer**。它只做两件事：

1. 把 :mod:`cns_planner.domain.radar_visibility_evidence` 的逐射线联合代数
   （terrain / building / combined / completion）整理成**有界、可分页、可判 stale**
   的只读证据块；
2. 把"语义输入身份"（input fingerprint）与"执行收据"（execution receipt）
   **严格分开**：预算、完成度、耗时、缓存命中、截断与失败原因都属于收据，
   绝不进入输入指纹。

硬约定（与 T07-A / T07-B / D2-C0 一致，不得放宽）：

* ``unknown`` **不等于** ``clear``：证据不足永远保持 ``unknown`` 并 fail-closed；
* 设备冻结几何排除的射线是 ``geometry_filtered``，**不是**物理 ``unknown``；
* 建筑 ``predicted`` 高度证据只能作为**预测性遮挡**披露，绝不混入可信遮挡；
* 本模块不产生任何探测概率、RCS、雷达方程、衍射、多径或杂波结论。
"""

from __future__ import annotations

from copy import deepcopy

from .radar_building_los import (
    HEIGHT_EVIDENCE_POLICY_ID,
    RADAR_BUILDING_LOS_ALGORITHM_VERSION,
    RADAR_BUILDING_LOS_FINGERPRINT_VERSION,
)
from .radar_terrain_los import (
    LOS_STATUSES,
    RADAR_TERRAIN_LOS_ALGORITHM_VERSION,
    number_or_none,
    stable_fingerprint,
)
from .radar_terrain_los_evidence import READ_ONLY_DECLARATION
from .radar_visibility_evidence import (
    GEOMETRY_FILTERED,
    NOT_EVALUATED,
    RADAR_COMBINED_FINGERPRINT_VERSION,
    RADAR_COMBINED_VISIBILITY_VERSION,
    building_domain_state,
    obstruction_hits_3d,
    ray_geometry_3d,
)

COMBINED_LOS_EVIDENCE_ADAPTER_ID = "radar_combined_los_read_only_evidence_v1"
COMBINED_LOS_EVIDENCE_SCHEMA_VERSION = "radar_combined_los_evidence_v1"
COMBINED_LOS_FINGERPRINT_SCHEMA_VERSION = "radar_combined_los_input_fingerprint_v2"
COMBINED_LOS_EXECUTION_FINGERPRINT_VERSION = "radar_combined_los_execution_fingerprint_v1"

EVIDENCE_STATUS_NOT_REQUESTED = "not_requested"
EVIDENCE_STATUS_COMPUTED = "computed"
EVIDENCE_STATUS_FAILED = "failed"
EVIDENCE_STATUS_STALE = "stale"
COMBINED_LOS_EVIDENCE_STATUSES = (
    EVIDENCE_STATUS_NOT_REQUESTED, EVIDENCE_STATUS_COMPUTED,
    EVIDENCE_STATUS_FAILED, EVIDENCE_STATUS_STALE,
)
COMBINED_LOS_EVIDENCE_STATUS_LABELS = {
    EVIDENCE_STATUS_NOT_REQUESTED: "尚未请求联合视线诊断",
    EVIDENCE_STATUS_COMPUTED: "已计算",
    EVIDENCE_STATUS_FAILED: "计算失败",
    EVIDENCE_STATUS_STALE: "旧证据已 stale",
}

#: 评价范围标识。它进入输入指纹：范围变化必须显式体现为 stale，不得静默复用。
EVALUATION_SCOPE_COMBINED = "bounded_single_site_probe_combined_los"

COMPLETION_VOCABULARY = ("complete", "incomplete", "not_evaluated")
BUILDING_DOMAIN_STATE_VOCABULARY = ("evaluated", GEOMETRY_FILTERED, NOT_EVALUATED)

#: 只有这些高度证据等级才构成"可信遮挡"（实测 / 来源标注 / 推导）。
TRUSTWORTHY_OBSTRUCTION_LEVELS = ("verified", "source_tagged", "derived")
#: 预测性高度只能作为"预测性遮挡"披露。
PREDICTIVE_OBSTRUCTION_LEVELS = ("predicted",)

COMBINED_LOS_USER_DISCLAIMER_ZH = (
    "地形与建筑联合视线诊断只表达同一条射线上地形与建筑几何的直达遮挡，"
    "不包含雷达方程、RCS、探测概率、衍射、多径或杂波；它不写任何项目状态，"
    "也不进入 Radar 划设、MILP、Required CNS 或 P16。"
)

#: 本轮**未评估**的维度（与 T07-A 同口径，前端必须原样展示）。
COMBINED_LOS_NOT_EVALUATED = (
    "detection_probability", "radar_equation", "rcs", "diffraction", "multipath",
    "clutter", "interference", "fresnel_zone_clearance",
    "full_tower_inventory_evaluation", "full_route_sample_evaluation",
    "engineering_estimate_height_used_in_strict_verdict",
    "project_state_persistence", "los_writer",
)


# ---------------------------------------------------------------- 小工具


def _text(value):
    return None if value is None else str(value)


def _status_counts(records, key):
    counts = {status: 0 for status in LOS_STATUSES}
    for record in records:
        value = _text(record.get(key))
        if value in counts:
            counts[value] += 1
    return counts


def _vocabulary_counts(records, key, vocabulary):
    counts = {name: 0 for name in vocabulary}
    for record in records:
        value = _text(record.get(key))
        if value in counts:
            counts[value] += 1
    return counts


def _bounded_int(value, default, hard_limit, *, minimum=0):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed < minimum:
        return default
    return min(parsed, hard_limit)


# ---------------------------------------------------------------- 遮挡分类


def classify_obstruction_hits(building_los):
    """按高度证据等级把建筑遮挡拆成"可信"与"预测性"两类（互不混合）。"""

    hits = obstruction_hits_3d(terrain_los=None, building_los=building_los)
    building_hits = [item for item in hits if item.get("type") == "building"]
    trustworthy, predictive = [], []
    for item in building_hits:
        level = _text(item.get("height_evidence_level"))
        if level in PREDICTIVE_OBSTRUCTION_LEVELS:
            predictive.append(item)
        elif level in TRUSTWORTHY_OBSTRUCTION_LEVELS:
            trustworthy.append(item)
    return {
        "building_obstruction_hit_count": len(building_hits),
        "trustworthy_obstruction_count": len(trustworthy),
        "predictive_obstruction_count": len(predictive),
        "unclassified_obstruction_count": (
            len(building_hits) - len(trustworthy) - len(predictive)
        ),
        "trustworthy_obstruction": bool(trustworthy),
        "predictive_obstruction": bool(predictive),
        "trustworthy_levels": list(TRUSTWORTHY_OBSTRUCTION_LEVELS),
        "predictive_levels": list(PREDICTIVE_OBSTRUCTION_LEVELS),
        "trustworthy_and_predictive_never_merged": True,
        "hits": building_hits,
    }


def _height_evidence_counts(building_los, classification):
    raw = (building_los or {}).get("height_evidence_counts")
    if isinstance(raw, dict) and raw:
        return {str(key): int(value) for key, value in raw.items()}
    counts = {}
    for item in classification["hits"]:
        level = _text(item.get("height_evidence_level")) or "unknown"
        counts[level] = counts.get(level, 0) + 1
    return counts


# ---------------------------------------------------------------- 逐射线记录


def readable_ray_record(*, evaluation, tower=None, probe=None, radar_type=None,
                        horizontal_crs=None, detailed=False):
    """一条联合射线的可读记录（摘要与明细共用同一构造，明细多带完整 hits）。

    ``evidence_classification`` 与 ``ray_geometry`` 一律原样转印领域结论，
    本函数**不重算**任何三态、净空或遮挡判定。
    """

    evaluation = evaluation if isinstance(evaluation, dict) else {}
    tower = tower if isinstance(tower, dict) else {}
    probe = probe if isinstance(probe, dict) else {}
    terrain = evaluation.get("terrain_los")
    terrain = terrain if isinstance(terrain, dict) else {}
    building = evaluation.get("building_los")
    building = building if isinstance(building, dict) else {}
    combined = evaluation.get("combined_visibility")
    combined = combined if isinstance(combined, dict) else {}
    classification = classify_obstruction_hits(building)
    probe_id = (
        probe.get("voxel_id") or probe.get("probe_id") or probe.get("grid_id")
        or evaluation.get("sample_id")
    )
    geometry = ray_geometry_3d(
        tower_id=tower.get("tower_id") or evaluation.get("tower_id"),
        radar_device_id=tower.get("device_instance_id"),
        probe_id=probe_id,
        origin_metric=(tower.get("metric") or evaluation.get("radar_origin_metric")),
        origin_egm2008_m=tower.get("origin_egm2008_m", evaluation.get("radar_origin_egm2008_m")),
        target_metric=probe.get("metric") or (
            (terrain.get("horizontal_position") or {}).get("metric")
        ),
        target_egm2008_m=number_or_none(
            probe.get("altitude_egm2008_m")
            if probe.get("altitude_egm2008_m") is not None
            else probe.get("egm2008_m")
        ),
        horizontal_crs=horizontal_crs,
        terrain_status=terrain.get("status"),
        building_status=building.get("status"),
        combined_status=evaluation.get("combined_visibility_status"),
        input_fingerprint=evaluation.get("cache_key"),
    )
    record = {
        "ray_id": evaluation.get("cache_key"),
        "tower_id": tower.get("tower_id") or evaluation.get("tower_id"),
        "probe_id": probe_id,
        "route_id": evaluation.get("route_id") or probe.get("route_id"),
        "radar_type": radar_type or evaluation.get("radar_type"),
        # ---- 三态与完成度（原样转印，前端不重算） ----
        "terrain_los_status": terrain.get("status"),
        "building_los_status": building.get("status"),
        "combined_los_status": evaluation.get("combined_visibility_status"),
        "building_domain_state": building_domain_state(building),
        "geometry_lookup_status": evaluation.get("status"),
        "within_frozen_device_range": building.get("within_frozen_device_range"),
        "computation_completion": evaluation.get("computation_completion"),
        "evidence_completion": evaluation.get("evidence_completion"),
        "search_incomplete": combined.get("search_incomplete"),
        "building_los_physical_unknown": combined.get("building_los_physical_unknown"),
        "domains_not_evaluated": deepcopy(combined.get("domains_not_evaluated") or []),
        #: 决定性与完成度是两个正交维度：可信阻断能回答"这条射线是否被遮挡"，
        #: 但它**不**让证据变完整，也**不**证明完整的物理场景。
        "credible_block_is_sufficient_for_direct_ray_block": combined.get(
            "credible_block_is_sufficient_for_direct_ray_block"
        ),
        "verdict_is_orthogonal_to_completion": combined.get(
            "verdict_is_orthogonal_to_completion"
        ),
        "other_domain_completion_preserved": combined.get(
            "other_domain_completion_preserved"
        ),
        "geometry_status": combined.get("geometry_status"),
        # ---- 几何与净空 ----
        "ray_geometry": geometry,
        "obstruction_margin_m": (
            None if not classification["hits"]
            else classification["hits"][0].get("obstruction_margin_m")
        ),
        # ---- 遮挡证据等级 ----
        "building_obstruction_hit_count": classification["building_obstruction_hit_count"],
        "trustworthy_obstruction": classification["trustworthy_obstruction"],
        "predictive_obstruction": classification["predictive_obstruction"],
        "trustworthy_obstruction_count": classification["trustworthy_obstruction_count"],
        "predictive_obstruction_count": classification["predictive_obstruction_count"],
        "unclassified_obstruction_count": classification["unclassified_obstruction_count"],
        "height_evidence_counts": _height_evidence_counts(building, classification),
        "trustworthy_and_predictive_never_merged": True,
        # ---- 建筑查询与地形采样收据 ----
        "building_candidate_count": building.get("candidate_building_count"),
        "building_exact_intersection_count": building.get("exact_intersection_count"),
        "terrain_sample_count": evaluation.get("terrain_sample_count"),
        "building_query_receipt": deepcopy(building.get("query_receipt")),
        # ---- 原因与算法版本 ----
        "reason": evaluation.get("reason") or building.get("reason") or terrain.get("reason"),
        "terrain_reason": terrain.get("reason"),
        "building_reason": building.get("reason"),
        "terrain_algorithm_version": terrain.get("algorithm_version"),
        "building_algorithm_version": building.get("algorithm_version"),
        "combined_algorithm_version": combined.get("algorithm_version"),
        # ---- 输入身份（语义）与执行收据 ----
        "terrain_input_fingerprint": terrain.get("input_fingerprint"),
        "building_input_fingerprint": building.get("input_fingerprint"),
        "combined_input_fingerprint": evaluation.get("cache_key"),
        "building_execution_identity": deepcopy(building.get("execution_identity")),
        "building_source_identity": deepcopy(building.get("building_source_identity")),
        "terrain_source_identity": deepcopy(
            (terrain.get("terrain_profile") or {}).get("source")
        ),
        "unknown_never_treated_as_clear": True,
        "geometry_filtered_is_not_physical_unknown": True,
    }
    if detailed:
        record["obstruction_hits"] = deepcopy(classification["hits"])
        record["combined_visibility"] = deepcopy(combined)
    return record


# ---------------------------------------------------------------- 身份与收据


def combined_los_input_fingerprint(*, radar_type=None, altitude_layer=None,
                                   terrain_source_identity=None,
                                   building_source_identity=None, tower_identities=None,
                                   probe_identities=None, sampling=None,
                                   ray_input_fingerprints=None, horizontal_crs=None,
                                   vertical_crs=None, ray_model=None,
                                   tower_filter=None, route_filter=None):
    """本次联合诊断的**语义输入身份**。

    只有确定性语义输入进入指纹：数据源身份、站址与 probe 身份、高度层、采样参数、
    雷达型号、逐射线语义输入指纹与 CRS / 射线模型。预算、完成度、耗时、缓存命中、
    截断与失败原因属于执行收据，**绝不**进入本指纹。
    """

    unordered_tower_identities = {}
    for item in tower_identities or []:
        identity = deepcopy(item)
        unordered_tower_identities[stable_fingerprint(identity)] = identity

    return stable_fingerprint({
        "fingerprint_schema_version": COMBINED_LOS_FINGERPRINT_SCHEMA_VERSION,
        "adapter_id": COMBINED_LOS_EVIDENCE_ADAPTER_ID,
        "combined_visibility_fingerprint_schema": RADAR_COMBINED_FINGERPRINT_VERSION,
        "algorithm_version": RADAR_COMBINED_VISIBILITY_VERSION,
        "terrain_algorithm_version": RADAR_TERRAIN_LOS_ALGORITHM_VERSION,
        "building_algorithm_version": RADAR_BUILDING_LOS_ALGORITHM_VERSION,
        "building_fingerprint_schema": RADAR_BUILDING_LOS_FINGERPRINT_VERSION,
        "height_evidence_policy": HEIGHT_EVIDENCE_POLICY_ID,
        "evaluation_scope": EVALUATION_SCOPE_COMBINED,
        "radar_type": _text(radar_type),
        "altitude_layer": deepcopy(altitude_layer),
        "terrain_source_identity": deepcopy(terrain_source_identity),
        "building_source_identity": deepcopy(building_source_identity),
        "tower_identities": [
            unordered_tower_identities[key]
            for key in sorted(unordered_tower_identities)
        ],
        "probe_identities": deepcopy(probe_identities),
        "sampling": deepcopy(sampling),
        "ray_input_fingerprints": sorted({
            str(item) for item in (ray_input_fingerprints or []) if item
        }),
        "crs": {"horizontal": _text(horizontal_crs), "vertical": _text(vertical_crs)},
        "ray_model": _text(ray_model),
        "tower_filter": sorted({_text(item) for item in (tower_filter or []) if _text(item)}),
        "route_filter": sorted({_text(item) for item in (route_filter or []) if _text(item)}),
        "budgets_excluded_from_input_fingerprint": True,
        "execution_receipt_excluded_from_input_fingerprint": True,
    })


def combined_los_execution_receipt(*, input_fingerprint=None, bounds=None,
                                   lookup_snapshot=None, evaluated_ray_count=0,
                                   elapsed_seconds=None, truncation=None,
                                   terrain_read_receipt=None, building_open_count=None,
                                   building_source_opened=None):
    """本次联合诊断的**执行收据**（不是语义输入）。"""

    return {
        "execution_fingerprint_version": COMBINED_LOS_EXECUTION_FINGERPRINT_VERSION,
        "algorithm_version": RADAR_COMBINED_VISIBILITY_VERSION,
        "input_fingerprint": _text(input_fingerprint),
        "bounds": deepcopy(bounds),
        "lookup_snapshot": deepcopy(lookup_snapshot),
        "evaluated_ray_count": int(evaluated_ray_count or 0),
        "elapsed_seconds": number_or_none(elapsed_seconds),
        "truncation": deepcopy(truncation),
        "terrain_read_receipt": deepcopy(terrain_read_receipt),
        "building_open_count": building_open_count,
        "building_source_opened": building_source_opened,
        "execution_receipt_excluded_from_input_fingerprint": True,
        "elapsed_seconds_is_receipt_only": True,
    }


# ---------------------------------------------------------------- stale 比较


def combined_los_currentness(prior_evidence, *, current_input_fingerprint,
                             current_schema_version=COMBINED_LOS_EVIDENCE_SCHEMA_VERSION,
                             current_algorithm_version=RADAR_COMBINED_VISIBILITY_VERSION,
                             current_fingerprint_schema_version=(
                                 COMBINED_LOS_FINGERPRINT_SCHEMA_VERSION
                             ),
                             current_evaluation_scope=EVALUATION_SCOPE_COMBINED):
    """**独立**判定旧联合证据是否 stale。

    只有存在**可比较的**旧证据（带 ``input_fingerprint``）时才宣称 stale；
    没有旧证据时如实返回 ``no_prior_evidence``，绝不凭空声称任何结果过期。
    本函数不读、不写、也不推断任何上游规划容器的 stale。
    """

    prior = prior_evidence if isinstance(prior_evidence, dict) else {}
    prior_fingerprint = prior.get("input_fingerprint") or prior.get("fingerprint")
    base = {
        "prior_evidence_present": bool(prior_fingerprint),
        "prior_input_fingerprint": _text(prior_fingerprint),
        "current_input_fingerprint": _text(current_input_fingerprint),
        "upstream_planning_state_untouched": True,
        "persistence_implemented": False,
        "claim": (
            "只读诊断：仅比较客户端提交的旧证据指纹，不实现 ProjectState 主动失效"
            "或结果持久化"
        ),
    }
    if not prior_fingerprint:
        return {
            **base,
            "state": "no_prior_evidence",
            "stale": False,
            "comparable": False,
            "reasons": [],
            "reasons_zh": ["未提供可比较的旧联合 LOS 证据，因此不宣称任何旧结果 stale。"],
        }
    comparable_fields = {
        "schema_version": current_schema_version,
        "algorithm_version": current_algorithm_version,
        "fingerprint_schema_version": current_fingerprint_schema_version,
        "evaluation_scope": current_evaluation_scope,
    }
    missing_fields = [key for key in comparable_fields if not _text(prior.get(key))]
    if missing_fields:
        return {
            **base,
            "state": "not_comparable",
            "stale": False,
            "comparable": False,
            "reasons": ["prior_contract_fields_missing"],
            "reasons_zh": [
                "旧联合证据缺少可比较的契约版本字段，不能伪装为 current。"
            ],
            "missing_contract_fields": missing_fields,
        }
    reasons = []
    if _text(prior.get("schema_version")) != _text(current_schema_version):
        reasons.append("combined_evidence_schema_changed")
    if _text(prior.get("algorithm_version")) != _text(current_algorithm_version):
        reasons.append("combined_algorithm_changed")
    if _text(prior.get("fingerprint_schema_version")) != _text(
        current_fingerprint_schema_version
    ):
        reasons.append("input_fingerprint_schema_changed")
    if _text(prior.get("evaluation_scope")) != _text(current_evaluation_scope):
        reasons.append("evaluation_scope_changed")
    if reasons:
        labels = {
            "combined_evidence_schema_changed": "联合证据契约版本已变化，旧结论不可继续引用。",
            "combined_algorithm_changed": "联合可见性算法版本已变化，旧结论不可继续引用。",
            "input_fingerprint_schema_changed": "联合 LOS 输入指纹契约已变化，旧指纹不可直接比较。",
            "evaluation_scope_changed": "评价范围变化（站址 / probe / 高度层），旧结论与本次不可直接比较。",
        }
        return {
            **base,
            "state": EVIDENCE_STATUS_STALE,
            "stale": True,
            "comparable": True,
            "reasons": reasons,
            "reasons_zh": [labels[reason] for reason in reasons],
            "prior_schema_version": _text(prior.get("schema_version")),
            "prior_algorithm_version": _text(prior.get("algorithm_version")),
            "prior_fingerprint_schema_version": _text(
                prior.get("fingerprint_schema_version")
            ),
            "prior_evaluation_scope": _text(prior.get("evaluation_scope")),
        }
    if _text(prior_fingerprint) == _text(current_input_fingerprint):
        return {
            **base,
            "state": "current",
            "stale": False,
            "comparable": True,
            "reasons": [],
            "reasons_zh": ["旧联合证据的输入指纹与本次一致，可继续引用。"],
        }
    reasons = ["combined_los_inputs_changed"]
    labels = {
        "combined_los_inputs_changed": (
            "联合 LOS 输入（数据源身份 / 站址原点 / 高度层 / 采样 / 射线模型）已变化。"
        ),
    }
    return {
        **base,
        "state": EVIDENCE_STATUS_STALE,
        "stale": True,
        "comparable": True,
        "reasons": reasons,
        "reasons_zh": [labels.get(reason, reason) for reason in reasons],
        "prior_evaluation_scope": _text(prior.get("evaluation_scope")),
        "prior_algorithm_version": _text(prior.get("algorithm_version")),
    }


# ---------------------------------------------------------------- 证据块


def _base_evidence(*, evidence_status, adapter_availability=None):
    return {
        "adapter_id": COMBINED_LOS_EVIDENCE_ADAPTER_ID,
        "schema_version": COMBINED_LOS_EVIDENCE_SCHEMA_VERSION,
        "fingerprint_schema_version": COMBINED_LOS_FINGERPRINT_SCHEMA_VERSION,
        "diagnostic_kind": "isolated_read_only_real_dual_source_los",
        "evidence_status": evidence_status,
        "evidence_status_label": COMBINED_LOS_EVIDENCE_STATUS_LABELS.get(evidence_status),
        "evaluation_scope": EVALUATION_SCOPE_COMBINED,
        "algorithm_version": RADAR_COMBINED_VISIBILITY_VERSION,
        "combined_visibility_algorithm_version": RADAR_COMBINED_VISIBILITY_VERSION,
        "terrain_algorithm_version": RADAR_TERRAIN_LOS_ALGORITHM_VERSION,
        "building_algorithm_version": RADAR_BUILDING_LOS_ALGORITHM_VERSION,
        "authoritative_project_result": False,
        "production_enforced_enabled": False,
        "strict_and_engineering_estimate_separate": True,
        "user_disclaimer_zh": COMBINED_LOS_USER_DISCLAIMER_ZH,
        "not_evaluated": list(COMBINED_LOS_NOT_EVALUATED),
        "adapter_availability": deepcopy(adapter_availability),
        **deepcopy(READ_ONLY_DECLARATION),
    }


def not_requested_status(*, adapter_availability=None, altitude_layer=None,
                         terrain_source_identity=None, building_source_identity=None):
    """轻量状态：**不打开 DEM，也不打开 GPKG**。"""

    return {
        **_base_evidence(
            evidence_status=EVIDENCE_STATUS_NOT_REQUESTED,
            adapter_availability=adapter_availability,
        ),
        "altitude_layer": deepcopy(altitude_layer),
        "terrain_source_identity": deepcopy(terrain_source_identity),
        "building_source_identity": deepcopy(building_source_identity),
        "rays": [],
        "ray_sample_count": 0,
        "ray_sample_truncated": False,
        "aggregate": {},
        "detail_available": False,
        "input_fingerprint": None,
        "execution_receipt": None,
        "currentness": None,
    }


def failure_status(reason, *, detail=None, adapter_availability=None):
    """失败必须如实降级：绝不返回任何 ``clear``，也绝不伪造成"无遮挡"。"""

    return {
        **_base_evidence(
            evidence_status=EVIDENCE_STATUS_FAILED,
            adapter_availability=adapter_availability,
        ),
        "failure_reason": _text(reason),
        "failure_detail": _text(detail),
        "rays": [],
        "ray_sample_count": 0,
        "ray_sample_truncated": False,
        "aggregate": {},
        "detail_available": False,
        "input_fingerprint": None,
        "execution_receipt": None,
        "currentness": {
            "state": "unknown",
            "stale": False,
            "comparable": False,
            "reasons": [reason] if reason else [],
            "reasons_zh": ["诊断未完成，因此没有任何可比较的联合 LOS 证据。"],
            "upstream_planning_state_untouched": True,
            "persistence_implemented": False,
        },
        "failure_never_rendered_as_clear": True,
    }


def build_combined_los_evidence(*, records, detailed_records=None, bounds=None,
                                source_provenance=None, terrain_source_identity=None,
                                building_source_identity=None, altitude_layer=None,
                                sampling=None, input_fingerprint=None,
                                execution_receipt=None, prior_evidence=None,
                                lookup_snapshot=None, ray_sample_limit=0,
                                detail_page_limit=0, truncation=None,
                                building_source_opened=None, building_open_count=None,
                                horizontal_crs=None, vertical_crs=None):
    """组装有界、可分页的联合 LOS 只读证据块。"""

    records = [item for item in (records or []) if isinstance(item, dict)]
    currentness = combined_los_currentness(
        prior_evidence, current_input_fingerprint=input_fingerprint,
    )
    evidence_status = (
        EVIDENCE_STATUS_STALE if currentness.get("stale") is True else EVIDENCE_STATUS_COMPUTED
    )
    limit = max(0, int(ray_sample_limit or 0))
    samples = [deepcopy(item) for item in records[:limit]] if limit else []
    counts = {
        "terrain_los_status_counts": _status_counts(records, "terrain_los_status"),
        "building_los_status_counts": _status_counts(records, "building_los_status"),
        "combined_los_status_counts": _status_counts(records, "combined_los_status"),
        "computation_completion_counts": _vocabulary_counts(
            records, "computation_completion", COMPLETION_VOCABULARY,
        ),
        "evidence_completion_counts": _vocabulary_counts(
            records, "evidence_completion", COMPLETION_VOCABULARY,
        ),
        "building_domain_state_counts": _vocabulary_counts(
            records, "building_domain_state", BUILDING_DOMAIN_STATE_VOCABULARY,
        ),
    }
    height_counts = {}
    for record in records:
        for level, value in (record.get("height_evidence_counts") or {}).items():
            height_counts[str(level)] = height_counts.get(str(level), 0) + int(value or 0)
    counts["height_evidence_level_counts"] = height_counts
    counts["evaluated_ray_count"] = len(records)
    counts["trustworthy_obstruction_ray_count"] = sum(
        1 for item in records if item.get("trustworthy_obstruction") is True
    )
    counts["predictive_obstruction_ray_count"] = sum(
        1 for item in records if item.get("predictive_obstruction") is True
    )
    counts["geometry_filtered_ray_count"] = sum(
        1 for item in records if item.get("building_domain_state") == GEOMETRY_FILTERED
    )
    counts["building_not_evaluated_ray_count"] = sum(
        1 for item in records if item.get("building_domain_state") == NOT_EVALUATED
    )
    counts["unknown_combination_count"] = counts["combined_los_status_counts"].get("unknown", 0)
    detail_records = [item for item in (detailed_records or []) if isinstance(item, dict)]
    return {
        **_base_evidence(
            evidence_status=evidence_status,
            adapter_availability=None,
        ),
        "source_provenance": deepcopy(source_provenance),
        "terrain_source_identity": deepcopy(terrain_source_identity),
        "building_source_identity": deepcopy(building_source_identity),
        "altitude_layer": deepcopy(altitude_layer),
        "sampling": deepcopy(sampling),
        "crs": {"horizontal": _text(horizontal_crs), "vertical": _text(vertical_crs)},
        "bounds": deepcopy(bounds),
        "rays": samples,
        "ray_sample_limit": limit,
        "ray_sample_count": len(samples),
        "ray_sample_truncated": len(records) > len(samples),
        "aggregate": counts,
        "input_fingerprint": _text(input_fingerprint),
        "execution_fingerprint_version": COMBINED_LOS_EXECUTION_FINGERPRINT_VERSION,
        "execution_receipt": deepcopy(execution_receipt),
        "lookup_snapshot": deepcopy(lookup_snapshot),
        "truncation": deepcopy(truncation),
        "currentness": currentness,
        "stale": currentness.get("stale") is True,
        "building_source_opened": building_source_opened,
        "building_open_count": building_open_count,
        "detail_available": bool(detail_records),
        "detail_page_limit": max(0, int(detail_page_limit or 0)),
        "detail_record_total": len(detail_records),
        "building_credible_and_predictive_disclosed_separately": True,
        "unknown_fail_closed": True,
        "full_terrain_profile_included": False,
        "full_los_matrix_embedded": False,
    }


def _matches(record, *, tower_id, route_id, combined_status, building_domain_state):
    if tower_id and _text(record.get("tower_id")) != _text(tower_id):
        return False
    if route_id and _text(record.get("route_id")) != _text(route_id):
        return False
    if combined_status and _text(record.get("combined_los_status")) != _text(combined_status):
        return False
    if building_domain_state and _text(record.get("building_domain_state")) != _text(
        building_domain_state
    ):
        return False
    return True


def flatten_combined_detail_records(records, *, tower_id=None, route_id=None,
                                    combined_status=None, building_domain_state=None,
                                    offset=0, limit=0, max_limit=0):
    """受限、可分页的逐射线明细（**只读内存证据，不重算、不重开数据源**）。"""

    records = [item for item in (records or []) if isinstance(item, dict)]
    matched = [
        item for item in records
        if _matches(
            item, tower_id=tower_id, route_id=route_id, combined_status=combined_status,
            building_domain_state=building_domain_state,
        )
    ]
    hard_limit = max(0, int(max_limit or 0))
    page_limit = (
        _bounded_int(limit, hard_limit, hard_limit, minimum=1)
        if hard_limit else max(0, int(limit or 0))
    )
    offset_value = max(0, int(offset or 0))
    page = matched[offset_value: offset_value + page_limit] if page_limit else []
    return {
        "matched_record_count": len(matched),
        "offset": offset_value,
        "limit": page_limit,
        "max_limit": max_limit,
        "returned_record_count": len(page),
        "truncated": offset_value + len(page) < len(matched),
        "records": [deepcopy(item) for item in page],
        "filters": {
            "tower_id": _text(tower_id), "route_id": _text(route_id),
            "combined_los_status": _text(combined_status),
            "building_domain_state": _text(building_domain_state),
        },
    }


__all__ = [
    "BUILDING_DOMAIN_STATE_VOCABULARY", "COMBINED_LOS_EVIDENCE_ADAPTER_ID",
    "COMBINED_LOS_EVIDENCE_SCHEMA_VERSION", "COMBINED_LOS_EVIDENCE_STATUSES",
    "COMBINED_LOS_EVIDENCE_STATUS_LABELS", "COMBINED_LOS_EXECUTION_FINGERPRINT_VERSION",
    "COMBINED_LOS_FINGERPRINT_SCHEMA_VERSION", "COMBINED_LOS_NOT_EVALUATED",
    "COMBINED_LOS_USER_DISCLAIMER_ZH", "COMPLETION_VOCABULARY",
    "EVIDENCE_STATUS_COMPUTED", "EVIDENCE_STATUS_FAILED", "EVIDENCE_STATUS_NOT_REQUESTED",
    "EVIDENCE_STATUS_STALE", "EVALUATION_SCOPE_COMBINED",
    "PREDICTIVE_OBSTRUCTION_LEVELS", "TRUSTWORTHY_OBSTRUCTION_LEVELS",
    "build_combined_los_evidence", "classify_obstruction_hits",
    "combined_los_currentness", "combined_los_execution_receipt",
    "combined_los_input_fingerprint", "failure_status",
    "flatten_combined_detail_records", "not_requested_status", "readable_ray_record",
]
