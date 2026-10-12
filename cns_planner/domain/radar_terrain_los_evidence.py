"""Radar 地形 LOS **只读证据块**的纯计算契约（Round34-T07-B）。

定位
----
Round34-T07-A 提供了独立的几何 LOS 计算（:mod:`cns_planner.domain.radar_terrain_los`）
与只读 GIS 适配（:mod:`cns_planner.gis.radar_terrain_los_adapter`），但**没有**接线。
本模块只回答一个问题：**如何把那些结论组织成可审计、可判 stale、且绝不进入任何
规划判定的只读证据块。**

硬边界（违反即返工）
--------------------
* 本模块是**纯计算**：不 import QGIS/GDAL、不读文件、不访问 DEM、不写 ``ProjectState``。
* LOS 的 ``clear`` / ``blocked`` / ``unknown`` 三态**原样转印**，绝不重映射。
* 外层"评价状态"（未请求 / 已计算 / 失败 / 旧证据已 stale）是**另一个维度**，
  绝不混进上面三态：本模块用 :data:`EVIDENCE_STATUS_*` 单独表达。
* ``blocked`` 只表示**当前地形射线被遮挡**，不推断"雷达绝对探测不到"；
  ``unknown`` **不是** clear，也**不是** pass。
* 本模块的输出**不进入** panel_selection、Radar-I/II 方位优化、MILP、
  MapRadar 独立站址数、Required CNS、P14/P15/P16、P16 排序/cap/停止条件。
* 只有**存在可比较的旧证据**时才宣称 stale；本模块不做任何持久化，
  也不宣称实现了 ProjectState 的主动失效。
"""

from __future__ import annotations

from copy import deepcopy

from .radar_terrain_los import (
    FROZEN_RADAR_GEOMETRY_PARAMETERS_UNCHANGED,
    FROZEN_RADAR_GEOMETRY_REPLACED_BY_TERRAIN_LOS,
    FROZEN_RADAR_GEOMETRY_VERSION,
    LOS_STATUSES,
    LOS_STATUS_BLOCKED,
    LOS_STATUS_CLEAR,
    LOS_STATUS_UNKNOWN,
    ORIGIN_AVAILABILITY_STATES,
    RADAR_TERRAIN_LOS_ALGORITHM_VERSION,
    RADAR_TERRAIN_LOS_NOT_EVALUATED,
    RADAR_TERRAIN_LOS_READ_MODE,
    number_or_none,
    stable_fingerprint,
)

# ---------------------------------------------------------------- 证据块身份

#: 只读证据适配器身份。它**不是**能力算法，不参与任何判定。
TERRAIN_LOS_EVIDENCE_ADAPTER_ID = "radar_terrain_los_read_only_evidence_v1"

#: 证据块 schema 版本（证据结构变化时递增；与 LOS 算法版本正交）。
TERRAIN_LOS_EVIDENCE_SCHEMA_VERSION = "radar_terrain_los_evidence_v1"

#: 指纹 schema 版本。
TERRAIN_LOS_EVIDENCE_FINGERPRINT_SCHEMA_VERSION = (
    "radar_terrain_los_evidence_input_fingerprint_v1"
)

#: 外层评价状态（**与 LOS 三态正交**，绝不合并）。
EVIDENCE_STATUS_NOT_REQUESTED = "not_requested"
EVIDENCE_STATUS_COMPUTED = "computed"
EVIDENCE_STATUS_FAILED = "failed"
EVIDENCE_STATUS_STALE = "stale"
EVIDENCE_STATUSES = (
    EVIDENCE_STATUS_NOT_REQUESTED,
    EVIDENCE_STATUS_COMPUTED,
    EVIDENCE_STATUS_FAILED,
    EVIDENCE_STATUS_STALE,
)

#: 评价范围口径。
EVALUATION_SCOPE_TOWER_PROBE_SAMPLE = "bounded_tower_probe_sample"

#: 外层评价状态的中文文案（前端与报告共用，避免各处各写一套）。
EVIDENCE_STATUS_LABELS = {
    EVIDENCE_STATUS_NOT_REQUESTED: "尚未请求地形 LOS 计算",
    EVIDENCE_STATUS_COMPUTED: "已计算",
    EVIDENCE_STATUS_FAILED: "计算失败",
    EVIDENCE_STATUS_STALE: "旧证据已 stale",
}

#: 本轮**能力边界**声明：这些效应一律 NOT_EVALUATED，绝不由本证据块推断。
TERRAIN_LOS_OUT_OF_SCOPE = (
    "building_occlusion", "clutter", "multipath", "radar_equation",
    "detection_probability_pd", "rcs", "diffraction", "atmospheric_loss",
    "antenna_gain_pattern", "terrain_scattering_coefficient",
    "fresnel_zone_clearance", "earth_curvature_formally_enabled",
    "full_tower_inventory_evaluation", "full_route_sample_evaluation",
    "project_state_persistence", "los_writer",
)

#: 只读声明（前端与审计共用的固定语义）。
READ_ONLY_DECLARATION = {
    "read_only": True,
    "writes_project_state": False,
    "writes_artifacts": False,
    "has_project_state_writer": False,
    "not_evaluated_in_planning": True,
    "participates_in_panel_selection": False,
    "participates_in_azimuth_optimisation": False,
    "participates_in_milp": False,
    "participates_in_required_cns": False,
    "participates_in_p14_p15_p16": False,
    "participates_in_p16_ordering_or_cap": False,
    "stales_upstream_planning_state": False,
    "terrain_los_replaces_beam_geometry": False,
    "is_detection_probability": False,
    "blocked_means_absolute_no_detection": False,
    "unknown_treated_as_clear": False,
    "unknown_removes_candidate_tower": False,
}

#: 用户可见的中文声明（前端必须原样或等价展示）。
TERRAIN_LOS_USER_DISCLAIMER_ZH = (
    "地形视距分析仅为几何可视性证据，不代表实际雷达探测概率或设备可安装确认。"
)


# ---------------------------------------------------------------- 小工具


def _text(value):
    return None if value is None else str(value)


def _counts(values, vocabulary):
    counter = {str(item): 0 for item in vocabulary}
    for value in values:
        key = str(value)
        if key in counter:
            counter[key] += 1
        else:
            counter[key] = counter.get(key, 0) + 1
    return counter


def _minimum_clearance_stats(records):
    values = sorted(
        float(item["minimum_clearance_m"])
        for item in records
        if number_or_none(item.get("minimum_clearance_m")) is not None
    )
    if not values:
        return {"count": 0, "min_m": None, "median_m": None, "max_m": None}
    return {
        "count": len(values),
        "min_m": round(values[0], 3),
        "median_m": round(values[len(values) // 2], 3),
        "max_m": round(values[-1], 3),
    }


def _low_clearance_records(records, *, threshold_m, limit):
    """净空很小但仍判 clear 的样本（**只提示，绝不改阈值或结论**）。"""

    if number_or_none(threshold_m) is None:
        return []
    picked = [
        item for item in records
        if str(item.get("los_status")) == LOS_STATUS_CLEAR
        and number_or_none(item.get("minimum_clearance_m")) is not None
        and float(item["minimum_clearance_m"]) <= float(threshold_m)
    ]
    picked.sort(key=lambda item: (
        float(item["minimum_clearance_m"]), str(item.get("tower_id") or ""),
        str(item.get("probe_id") or ""),
    ))
    return picked[: max(0, int(limit))]


# ---------------------------------------------------------------- 指纹


def terrain_los_evidence_input_fingerprint(
    *, algorithm_version, dem_source_identity, dem_vertical_reference,
    sampling_interval_m, envelope_radius_m, earth_radius_m, k_factor,
    evaluation_scope, altitude_layer_id, bounds, record_fingerprints, extra=None,
):
    """LOS **证据块**的输入指纹。

    它必须覆盖一切会改变结论的真实输入：LOS 算法版本与读取模式、DEM 身份与垂向基准、
    采样参数、射线模型（曲率/折射）、评价范围与上限、真实高度层，以及**逐记录的
    T07-A 输入指纹**（后者已经把雷达原点、证据等级、probe 真实高度、DEM 身份、
    采样与射线模型各自包含在内）。

    逐记录指纹列表**有界**：超界时按 ``(tower_id, probe_id)`` 排序后取前
    :data:`_FINGERPRINT_RECORD_LIMIT` 条，并如实记录被截断的数量 —— 证据块自身
    已经有界，因此这个上限不会让指纹失去分辨力。
    """

    ordered = sorted(
        (str(item) for item in (record_fingerprints or []) if item),
    )
    limited = ordered[:_FINGERPRINT_RECORD_LIMIT]
    payload = {
        "fingerprint_schema_version": TERRAIN_LOS_EVIDENCE_FINGERPRINT_SCHEMA_VERSION,
        "adapter_id": TERRAIN_LOS_EVIDENCE_ADAPTER_ID,
        "algorithm_version": _text(algorithm_version),
        "read_mode": RADAR_TERRAIN_LOS_READ_MODE,
        "dem": {
            "source_identity": deepcopy(dem_source_identity),
            "vertical_reference": _text(dem_vertical_reference),
        },
        "sampling": {
            "sampling_interval_m": number_or_none(sampling_interval_m),
            "envelope_radius_m": number_or_none(envelope_radius_m),
        },
        "ray_model": {
            "earth_radius_m": number_or_none(earth_radius_m),
            "k_factor": number_or_none(k_factor),
            "model": "straight_ray_with_optional_bounded_curvature_correction",
        },
        "evaluation_scope": _text(evaluation_scope),
        "altitude_layer_id": _text(altitude_layer_id),
        "bounds": {
            "max_tower_count": int((bounds or {}).get("max_tower_count") or 0),
            "probe_budget": int((bounds or {}).get("probe_budget") or 0),
            "probe_stride": int((bounds or {}).get("probe_stride") or 1),
        },
        "record_fingerprints": limited,
        "record_fingerprint_count": len(ordered),
        "record_fingerprint_truncated_count": max(0, len(ordered) - len(limited)),
        "not_evaluated": list(RADAR_TERRAIN_LOS_NOT_EVALUATED),
    }
    if isinstance(extra, dict):
        for key in sorted(extra):
            payload[key] = extra[key]
    return stable_fingerprint(payload)


#: 指纹里逐记录指纹列表的上限（证据块本身有界，因此不会触及）。
_FINGERPRINT_RECORD_LIMIT = 512


# ---------------------------------------------------------------- 轻量状态（不打开 DEM）


def not_requested_status(*, adapter_availability=None):
    """**尚未请求**的轻量状态：不打开 DEM、不做任何计算。

    只返回身份、能力边界与（如实给出的）可用性前提，供前端渲染"未请求"入口。
    """

    return {
        "adapter_id": TERRAIN_LOS_EVIDENCE_ADAPTER_ID,
        "schema_version": TERRAIN_LOS_EVIDENCE_SCHEMA_VERSION,
        "read_only": True,
        "evidence_status": EVIDENCE_STATUS_NOT_REQUESTED,
        "evidence_status_label": EVIDENCE_STATUS_LABELS[EVIDENCE_STATUS_NOT_REQUESTED],
        "evaluation_scope": None,
        "algorithm_id": TERRAIN_LOS_EVIDENCE_ADAPTER_ID,
        "algorithm_version": RADAR_TERRAIN_LOS_ALGORITHM_VERSION,
        "read_mode": RADAR_TERRAIN_LOS_READ_MODE,
        "requested": False,
        "dem_opened": False,
        "los_computation_performed": False,
        "los_status_counts": {status: 0 for status in LOS_STATUSES},
        "origin_availability_counts": {state: 0 for state in ORIGIN_AVAILABILITY_STATES},
        "evaluated_tower_count": 0,
        "evaluated_probe_count": 0,
        "candidate_tower_count": 0,
        "candidate_probe_count": 0,
        "record_sample_count": 0,
        "records": [],
        "low_clearance_samples": [],
        "input_fingerprint": None,
        "currentness": {"state": "no_prior_evidence", "prior_evidence_present": False,
                        "reasons": [], "claim": "只读证据块不做持久化"},
        "not_evaluated": list(TERRAIN_LOS_OUT_OF_SCOPE),
        "user_disclaimer_zh": TERRAIN_LOS_USER_DISCLAIMER_ZH,
        "adapter_availability": deepcopy(adapter_availability),
        **READ_ONLY_DECLARATION,
    }


def failure_status(reason, *, detail=None, adapter_availability=None):
    """**计算失败**的外层状态：如实记录原因，绝不用空结论冒充"无遮挡"。"""

    return {
        "adapter_id": TERRAIN_LOS_EVIDENCE_ADAPTER_ID,
        "schema_version": TERRAIN_LOS_EVIDENCE_SCHEMA_VERSION,
        "evidence_status": EVIDENCE_STATUS_FAILED,
        "evidence_status_label": EVIDENCE_STATUS_LABELS[EVIDENCE_STATUS_FAILED],
        "evaluation_scope": None,
        "algorithm_id": TERRAIN_LOS_EVIDENCE_ADAPTER_ID,
        "algorithm_version": RADAR_TERRAIN_LOS_ALGORITHM_VERSION,
        "read_mode": RADAR_TERRAIN_LOS_READ_MODE,
        "requested": True,
        "dem_opened": False,
        "los_computation_performed": False,
        "failure_reason": _text(reason),
        "failure_detail": _text(detail),
        "los_status_counts": {status: 0 for status in LOS_STATUSES},
        "origin_availability_counts": {state: 0 for state in ORIGIN_AVAILABILITY_STATES},
        "evaluated_tower_count": 0,
        "evaluated_probe_count": 0,
        "record_sample_count": 0,
        "records": [],
        "low_clearance_samples": [],
        "input_fingerprint": None,
        "currentness": {"state": "not_comparable", "prior_evidence_present": False,
                        "reasons": [], "claim": "本次请求未产出可比对证据"},
        "not_evaluated": list(TERRAIN_LOS_OUT_OF_SCOPE),
        "user_disclaimer_zh": TERRAIN_LOS_USER_DISCLAIMER_ZH,
        "adapter_availability": deepcopy(adapter_availability),
        **READ_ONLY_DECLARATION,
    }


# ---------------------------------------------------------------- stale 比较


def los_currentness(prior_evidence, *, current_input_fingerprint, current_algorithm_version):
    """**独立**判定旧 LOS 证据是否 stale。

    规则（只有存在可比较的旧证据时才宣称 stale）：

    * 没有旧证据（或旧证据没有 ``input_fingerprint``）⇒ ``no_prior_evidence``；
    * 旧指纹 == 当前指纹 ⇒ ``current``；
    * 旧指纹 != 当前指纹 ⇒ ``stale``，并尽量给出**可区分的原因**：
      LOS 算法版本变化 / 评价范围变化 / 其它输入变化（DEM 身份、原点、高度层、
      采样或射线模型）。后者是"指纹不同但无法从指纹单独定位到唯一维度"的如实归类，
      **不**逐项猜测具体是哪一项。

    **绝不**由此推断上游规划结果 stale：本函数只读入参，不返回、不写任何上游容器。
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
            "reasons_zh": ["未提供可比较的旧 LOS 证据，因此不宣称任何旧结果 stale。"],
        }
    if _text(prior_fingerprint) == _text(current_input_fingerprint):
        return {
            **base,
            "state": "current",
            "stale": False,
            "comparable": True,
            "reasons": [],
            "reasons_zh": ["旧 LOS 证据的输入指纹与本次一致，可继续引用。"],
        }
    reasons = []
    previous_model = prior.get("algorithm_version")
    previous_scope = prior.get("evaluation_scope")
    if _text(previous_model) != _text(current_algorithm_version):
        reasons.append("los_algorithm_version_changed")
    elif _text(previous_scope) != _text(EVALUATION_SCOPE_TOWER_PROBE_SAMPLE):
        reasons.append("evaluation_scope_changed")
    else:
        reasons.append("los_inputs_changed")
    previous_bounds = prior.get("bounds") if isinstance(prior.get("bounds"), dict) else {}
    labels = {
        "los_algorithm_version_changed": "LOS 算法版本已变化，旧结论不可继续引用。",
        "evaluation_scope_changed": "评价范围变化，旧结论与本次不可直接比较。",
        "los_inputs_changed": "LOS 输入（原点 / 高度层 / DEM / 采样或射线模型）已变化。",
    }
    return {
        **base,
        "state": EVIDENCE_STATUS_STALE,
        "stale": True,
        "comparable": True,
        "reasons": reasons,
        "reasons_zh": [labels.get(reason, reason) for reason in reasons],
        "prior_bounds": deepcopy(previous_bounds),
        "prior_evaluation_scope": _text(previous_scope),
        "prior_algorithm_version": _text(previous_model),
    }


# ---------------------------------------------------------------- 证据块构建


def build_terrain_los_evidence(
    *, records, candidate_tower_count, candidate_probe_count,
    origin_availability_counts, dem_source_identity, dem_vertical_reference,
    altitude_layer, sampling_interval_m, envelope_radius_m, earth_radius_m, k_factor,
    bounds, dem_read_receipt=None, elapsed_seconds=None, prior_evidence=None,
    source_provenance=None, adapter_availability=None, low_clearance_threshold_m=5.0,
    low_clearance_limit=5, record_sample_limit=24,
):
    """把逐 (塔, probe) 的 LOS 评价记录聚合成**只读证据块**。

    ``records``：T07-A :func:`~cns_planner.gis.radar_terrain_los_adapter.
    evaluate_probe_terrain_los` 的返回值列表（已经过应用层裁剪与限量）。
    """

    items = [item for item in (records or []) if isinstance(item, dict)]
    los_counts = _counts((item.get("status") for item in items), LOS_STATUSES)
    availability_counts = {
        state: int((origin_availability_counts or {}).get(state) or 0)
        for state in ORIGIN_AVAILABILITY_STATES
    }
    layer = altitude_layer if isinstance(altitude_layer, dict) else {}
    bounds = bounds if isinstance(bounds, dict) else {}
    fingerprint = terrain_los_evidence_input_fingerprint(
        algorithm_version=RADAR_TERRAIN_LOS_ALGORITHM_VERSION,
        dem_source_identity=dem_source_identity,
        dem_vertical_reference=dem_vertical_reference,
        sampling_interval_m=sampling_interval_m,
        envelope_radius_m=envelope_radius_m,
        earth_radius_m=earth_radius_m,
        k_factor=k_factor,
        evaluation_scope=EVALUATION_SCOPE_TOWER_PROBE_SAMPLE,
        altitude_layer_id=layer.get("altitude_layer_id"),
        bounds=bounds,
        record_fingerprints=[item.get("input_fingerprint") for item in items],
    )
    currentness = los_currentness(
        prior_evidence, current_input_fingerprint=fingerprint,
        current_algorithm_version=RADAR_TERRAIN_LOS_ALGORITHM_VERSION,
    )
    evaluated_towers = sorted({
        str(item.get("tower_id") or "") for item in items if item.get("tower_id")
    })
    samples = [_sample_record(item) for item in items]
    samples.sort(key=lambda item: (
        str(item.get("tower_id") or ""), str(item.get("probe_id") or ""),
    ))
    readable = [_readable_record(item) for item in items]
    status = (
        EVIDENCE_STATUS_STALE if currentness.get("stale")
        else EVIDENCE_STATUS_COMPUTED
    )
    return {
        "adapter_id": TERRAIN_LOS_EVIDENCE_ADAPTER_ID,
        "schema_version": TERRAIN_LOS_EVIDENCE_SCHEMA_VERSION,
        "fingerprint_schema_version": TERRAIN_LOS_EVIDENCE_FINGERPRINT_SCHEMA_VERSION,
        "evidence_status": status,
        "evidence_status_label": EVIDENCE_STATUS_LABELS[status],
        "computed_at": None,
        "requested": True,
        "dem_opened": True,
        "los_computation_performed": True,
        "evaluation_scope": EVALUATION_SCOPE_TOWER_PROBE_SAMPLE,
        "evaluation_scope_note_zh": (
            "本次只评价**有界抽样**的（铁塔 × 航路 probe）组合，"
            "不代表全部候选铁塔或全部航路采样点都已被评价。"
        ),
        "algorithm_id": TERRAIN_LOS_EVIDENCE_ADAPTER_ID,
        "algorithm_version": RADAR_TERRAIN_LOS_ALGORITHM_VERSION,
        "read_mode": RADAR_TERRAIN_LOS_READ_MODE,
        "evaluated_tower_count": len(evaluated_towers),
        "evaluated_probe_count": len(items),
        "candidate_tower_count": int(candidate_tower_count or 0),
        "candidate_probe_count": int(candidate_probe_count or 0),
        "evaluated_tower_ids": evaluated_towers,
        "los_status_counts": los_counts,
        "origin_availability_counts": availability_counts,
        "clearance_stats_clear_only": _minimum_clearance_stats([
            item for item in items if str(item.get("status")) == LOS_STATUS_CLEAR
        ]),
        "dem": {
            "source_identity": deepcopy(dem_source_identity),
            "vertical_reference": _text(dem_vertical_reference),
            "read_mode": RADAR_TERRAIN_LOS_READ_MODE,
            "read_receipt": deepcopy(dem_read_receipt),
            "source_cache_policy": "one_open_per_path_and_variant_per_request",
        },
        "altitude_layer": {
            "altitude_layer_id": layer.get("altitude_layer_id"),
            "nominal_altitude_m": number_or_none(layer.get("nominal_altitude_m")),
            "lower_altitude_m": number_or_none(layer.get("lower_altitude_m")),
            "upper_altitude_m": number_or_none(layer.get("upper_altitude_m")),
            "vertical_reference": layer.get("vertical_reference"),
            "resolution_source": layer.get("resolution_source"),
            "authoritative": bool(layer.get("altitude_layer_id")),
            "probe_height_source": "authoritative_altitude_layer_nominal_altitude_m",
            "representative_height_used": False,
        },
        "sampling": {
            "sampling_interval_m": number_or_none(sampling_interval_m),
            "envelope_radius_m": number_or_none(envelope_radius_m),
            "earth_radius_m": number_or_none(earth_radius_m),
            "k_factor": number_or_none(k_factor),
            "curvature_correction_enabled": number_or_none(earth_radius_m) is not None,
            "refraction_enabled": (
                number_or_none(k_factor) is not None and float(k_factor) != 1.0
            ),
        },
        #: 冻结波束几何**只被引用，绝不改变**：地形 LOS 是并行证据，不替换它。
        "frozen_radar_geometry": {
            "version": FROZEN_RADAR_GEOMETRY_VERSION,
            "parameters_unchanged": FROZEN_RADAR_GEOMETRY_PARAMETERS_UNCHANGED,
            "replaced_by_terrain_los": FROZEN_RADAR_GEOMETRY_REPLACED_BY_TERRAIN_LOS,
        },
        "bounds": {
            **deepcopy(bounds),
            "max_tower_count": int(bounds.get("max_tower_count") or 0),
            "probe_stride": int(bounds.get("probe_stride") or 1),
            "probe_max_per_tower": int(bounds.get("probe_max_per_tower") or 0),
            "record_sample_limit": int(record_sample_limit),
            "detail_page_limit": int(bounds.get("detail_page_limit") or 0),
            "full_terrain_profile_included": False,
            "detail_requires_explicit_request": True,
        },
        "bounds_note_zh": (
            "默认摘要只含聚合统计与有限样本；逐 probe 明细需要显式请求，"
            "且受分页与总数上限约束。完整地形剖面**不**进入 API 响应。"
        ),
        "records": samples[: max(0, int(record_sample_limit))],
        "record_sample_count": min(len(samples), max(0, int(record_sample_limit))),
        "record_sample_limit": int(record_sample_limit),
        "record_sample_truncated": len(samples) > max(0, int(record_sample_limit)),
        #: 明细级记录**不进**这个键；它们由服务层单独持有并按需分页下发，
        #: 否则摘要响应会同时携带摘要样本与全部明细，体积翻倍。
        "detail_record_count": len(readable),
        "low_clearance_threshold_m": number_or_none(low_clearance_threshold_m),
        "low_clearance_samples": _low_clearance_records(
            samples, threshold_m=low_clearance_threshold_m, limit=low_clearance_limit,
        ),
        "low_clearance_note_zh": (
            "低净空提示只用于人工复核：本轮**不改变** LOS 三态阈值，也不改变算法。"
        ),
        "input_fingerprint": fingerprint,
        "currentness": currentness,
        "elapsed_seconds": (
            None if elapsed_seconds is None else round(float(elapsed_seconds), 3)
        ),
        "source_provenance": deepcopy(source_provenance),
        "adapter_availability": deepcopy(adapter_availability),
        "not_evaluated": list(TERRAIN_LOS_OUT_OF_SCOPE),
        "los_three_state_semantics_zh": {
            "clear": "沿射线在本次采样下未见地形遮挡。",
            "blocked": "当前地形射线被遮挡；**不**自动认定雷达绝对无法探测。",
            "unknown": "地形、原点或高度证据不足；**不视为** clear，也**不**自动删除候选塔。",
        },
        "dimension_separation_zh": (
            "「规划候选资格」（能否进入规划搜索）与「物理 LOS 可评价性」"
            "（原点是否高于地形、能否作为物理仿真输入）是两个**不同维度**；"
            "二者互不升级、互不删除。"
        ),
        "user_disclaimer_zh": TERRAIN_LOS_USER_DISCLAIMER_ZH,
        **READ_ONLY_DECLARATION,
    }


def readable_record_for_evaluation(item):
    """逐 probe **明细级**记录（显式请求才下发，仍需分页）。

    与摘要级样本共用同一批字段口径，只是多带几何 / 高度 / 剖面摘要等诊断细节；
    完整地形剖面**永不**包含在内。
    """

    return _readable_record(item)


def sample_record_for_evaluation(item):
    """逐 probe **摘要级**（紧凑）记录。"""

    return _sample_record(item)


def _sample_record(item):
    """摘要级（紧凑）逐 probe 记录：`T07-B` 默认样本的字段集。"""

    geometry = item.get("probe_geometry") if isinstance(item.get("probe_geometry"), dict) else {}
    height = item.get("probe_height") if isinstance(item.get("probe_height"), dict) else {}
    return {
        "tower_id": item.get("tower_id"),
        "probe_id": item.get("probe_voxel_id"),
        "route_id": item.get("route_id"),
        "altitude_layer_id": item.get("authoritative_altitude_layer_id"),
        "los_status": item.get("status"),
        "reason": item.get("reason"),
        "minimum_clearance_m": number_or_none(item.get("minimum_clearance_m")),
        "horizontal_distance_m": number_or_none(geometry.get("horizontal_distance_m")),
        "slant_range_m": number_or_none(item.get("slant_range_m")),
        "elevation_deg": number_or_none(item.get("elevation_deg")),
        "radar_origin_egm2008_m": number_or_none(item.get("radar_origin_egm2008_m")),
        "radar_origin_source": item.get("radar_origin_source"),
        "radar_origin_state": item.get("radar_origin_state"),
        "radar_origin_availability": item.get("radar_origin_availability"),
        "probe_altitude_egm2008_m": number_or_none(height.get("altitude_egm2008_m")),
        "probe_height_source": height.get("height_source"),
        "vertical_reference": item.get("vertical_reference"),
        "representative_height_used": False,
        "within_frozen_device_range": item.get("within_frozen_device_range"),
        "sample_status_counts": deepcopy(item.get("sample_status_counts")),
        "low_clearance": (
            number_or_none(item.get("minimum_clearance_m")) is not None
            and float(item["minimum_clearance_m"]) <= 5.0
        ),
        "input_fingerprint": item.get("input_fingerprint"),
    }


def _readable_record(item):
    """明细级逐 probe 记录（显式请求才下发，仍需分页）。"""

    geometry = item.get("probe_geometry") if isinstance(item.get("probe_geometry"), dict) else {}
    height = item.get("probe_height") if isinstance(item.get("probe_height"), dict) else {}
    profile = item.get("terrain_profile") if isinstance(item.get("terrain_profile"), dict) else {}
    verdict = item.get("los_verdict") if isinstance(item.get("los_verdict"), dict) else {}
    origin_physical = (
        item.get("origin_physical_input")
        if isinstance(item.get("origin_physical_input"), dict) else {}
    )
    return {
        **_sample_record(item),
        "probe_longitude": (item.get("horizontal_position") or {}).get("longitude"),
        "probe_latitude": (item.get("horizontal_position") or {}).get("latitude"),
        "radar_origin_metric": deepcopy(item.get("radar_origin_metric")),
        "probe_geometry": {
            "horizontal_distance_m": number_or_none(geometry.get("horizontal_distance_m")),
            "vertical_delta_m": number_or_none(geometry.get("vertical_delta_m")),
            "slant_distance_m": number_or_none(geometry.get("slant_distance_m")),
            "elevation_deg": number_or_none(geometry.get("elevation_deg")),
            "curvature_drop_m": number_or_none(geometry.get("curvature_drop_m")),
        },
        "probe_height": {
            "status": height.get("status"),
            "reason": height.get("reason"),
            "altitude_egm2008_m": number_or_none(height.get("altitude_egm2008_m")),
            "height_source": height.get("height_source"),
            "altitude_layer_id": height.get("altitude_layer_id"),
        },
        "origin_physical_input": {
            "terrain_egm2008_m": number_or_none(origin_physical.get("terrain_egm2008_m")),
            "terrain_consistency": origin_physical.get("terrain_consistency"),
            "origin_terrain_delta_m": number_or_none(
                origin_physical.get("origin_terrain_delta_m")
            ),
            "origin_availability": origin_physical.get("origin_availability"),
            "reason": origin_physical.get("reason"),
            "scenario_offered": origin_physical.get("scenario_offered"),
        },
        "first_obstruction_distance_m": number_or_none(item.get("first_obstruction_distance_m")),
        "first_marginal_distance_m": number_or_none(item.get("first_marginal_distance_m")),
        "first_unknown_distance_m": number_or_none(item.get("first_unknown_distance_m")),
        "terrain_profile_summary": {
            "status": profile.get("status"),
            "sample_count": profile.get("sample_count"),
            "sampling": deepcopy(profile.get("sampling")),
            "read_receipt": deepcopy(profile.get("read_receipt")),
            "samples": deepcopy(profile.get("samples")),
            "profile_detail": item.get("profile_detail"),
            "full_profile_included": False,
        },
        "los_verdict": {
            "status": verdict.get("status"),
            "reason": verdict.get("reason"),
            "counts": deepcopy(verdict.get("counts")),
            "basis": verdict.get("basis"),
        },
        "resolution_limitation": item.get("resolution_limitation"),
        "not_evaluated": deepcopy(item.get("not_evaluated")),
    }


def flatten_detail_records(evidence, *, records=None, tower_id=None, route_id=None,
                           altitude_layer_id=None, los_status=None,
                           offset=0, limit=50, max_limit=200):
    """对**已计算**的证据块做受限、可分页的逐 probe 明细查询。

    * 只读取调用方给出的明细记录（由应用层持有的**进程内**证据，不重新打开 DEM、
      不重算 LOS）；
    * ``limit`` 被硬性限制在 ``max_limit`` 以内，且响应里如实报告被裁剪的数量；
    * 完整地形剖面**永不**出现在返回值里。
    """

    items = records if isinstance(records, list) else []
    wanted_tower = _text(tower_id)
    wanted_route = _text(route_id)
    wanted_layer = _text(altitude_layer_id)
    wanted_status = _text(los_status)
    matched = [
        item for item in items
        if (wanted_tower is None or _text(item.get("tower_id")) == wanted_tower)
        and (wanted_route is None or _text(item.get("route_id")) == wanted_route)
        and (wanted_layer is None or _text(item.get("altitude_layer_id")) == wanted_layer)
        and (wanted_status is None or _text(item.get("los_status")) == wanted_status)
    ]
    try:
        start = max(0, int(offset))
    except (TypeError, ValueError):
        start = 0
    try:
        size = max(1, int(limit))
    except (TypeError, ValueError):
        size = 50
    hard_limit = max(1, int(max_limit))
    size = min(size, hard_limit)
    page = matched[start: start + size]
    return {
        "evidence_status": (evidence or {}).get("evidence_status"),
        "input_fingerprint": (evidence or {}).get("input_fingerprint"),
        "evaluation_scope": (evidence or {}).get("evaluation_scope"),
        "filters": {
            "tower_id": wanted_tower, "route_id": wanted_route,
            "altitude_layer_id": wanted_layer, "los_status": wanted_status,
        },
        "matched_record_count": len(matched),
        "available_record_count": len(items),
        "offset": start,
        "limit": size,
        "max_limit": hard_limit,
        "returned_record_count": len(page),
        "truncated": start + len(page) < len(matched),
        "records": page,
        "full_terrain_profile_included": False,
        "recomputed": False,
        "read_only": True,
        "writes_project_state": False,
        "user_disclaimer_zh": TERRAIN_LOS_USER_DISCLAIMER_ZH,
    }


__all__ = [
    "EVALUATION_SCOPE_TOWER_PROBE_SAMPLE",
    "EVIDENCE_STATUSES", "EVIDENCE_STATUS_COMPUTED", "EVIDENCE_STATUS_FAILED",
    "EVIDENCE_STATUS_LABELS", "EVIDENCE_STATUS_NOT_REQUESTED", "EVIDENCE_STATUS_STALE",
    "READ_ONLY_DECLARATION",
    "TERRAIN_LOS_EVIDENCE_ADAPTER_ID", "TERRAIN_LOS_EVIDENCE_FINGERPRINT_SCHEMA_VERSION",
    "TERRAIN_LOS_EVIDENCE_SCHEMA_VERSION", "TERRAIN_LOS_OUT_OF_SCOPE",
    "TERRAIN_LOS_USER_DISCLAIMER_ZH",
    "build_terrain_los_evidence", "failure_status", "flatten_detail_records",
    "los_currentness", "not_requested_status", "readable_record_for_evaluation",
    "sample_record_for_evaluation",
    "terrain_los_evidence_input_fingerprint",
]
