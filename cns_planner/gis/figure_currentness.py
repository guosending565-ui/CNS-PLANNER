"""专题成果图的 currentness 词表与身份门禁（Round38-FIG）。

本模块是**图件消费方唯一允许使用的时效性判据来源**，回答两个问题：

1. 上游结果的保存状态，翻译成制图语义的五种 ``currentness``：``current`` /
   ``stale`` / ``blocked`` / ``unknown`` / ``historical``；
2. 一张图是否能作为**正式 current 成果图**：由 :func:`production_gate` 汇总全部图层
   后裁定，并把"不满足的原因"原样给出。

严格边界（与 :mod:`cns_planner.application.result_currentness` 一致）：

* 只读投影：不写 ``ProjectState``、不 ``save()``、不推进 revision、不触发失效传播；
* **不把 stale 改写成 current**：本模块只做规范化，绝不"升级"任何状态；
* 缺失不等于通过：上层没有给出状态时落到 :data:`UNKNOWN`，绝不落到 :data:`CURRENT`；
* 航路几何指纹只有**一个**权威构造器 :func:`route_geometry_fingerprint`，它就是
  ``cns_planner.domain.layered_route_validation.path_fingerprint``（FC30 统一暴露）。
  图件绝不用 ``route_id`` 自行推导或重建第二套航路几何身份。
"""

from __future__ import annotations

import math

from ..domain.cns_corridor import (
    CORRIDOR_HALF_WIDTH_M,
    CORRIDOR_TOTAL_WIDTH_M,
    GEOMETRY_ALGORITHM_ID,
    GEOMETRY_ALGORITHM_VERSION,
    WIDTH_POLICY_VERSION,
    WIDTH_SEMANTICS,
)
from ..domain.layered_route_validation import path_fingerprint

# ---- 词表 -------------------------------------------------------------------

CURRENT = "current"
STALE = "stale"
BLOCKED = "blocked"
UNKNOWN = "unknown"
HISTORICAL = "historical"

#: 五态固定顺序（报告与测试按此顺序展开）。
CURRENTNESS_VALUES = (CURRENT, STALE, BLOCKED, UNKNOWN, HISTORICAL)

#: 只有 ``current`` 才允许被当作"当前有效结果"制图；其余四态一律不得冒充。
DRAWABLE_AS_CURRENT = (CURRENT,)

#: ``route_geometry_fingerprint`` 的稳定前缀（由 ``path_fingerprint`` 产出）。
ROUTE_GEOMETRY_FINGERPRINT_PREFIX = "layeredpathv1-"

#: 归一化来源标识（写进 FigureSpec，说明该状态由哪一条判据得出）。
TRUTH_SOURCE_RESULT_STATUS = "result_statuses_effective"
TRUTH_SOURCE_GEOMETRY_RESULT = "geometry_construction_result"
TRUTH_SOURCE_ROUTE_STATUS = "operational_route_status"
TRUTH_SOURCE_ABSENT = "upstream_status_absent"

#: 上游"明确未评估"的状态取值（缺失不等于通过，这里必须显式列出）。
NOT_EVALUATED_STATUSES = ("not_calculated", "not_evaluated", "missing_data", "pending")

#: 上游"已阻断/已失败"的状态取值。
BLOCKED_STATUSES = ("blocked", "failed", "refused", "infeasible")

#: 上游"历史结果"的状态取值（只能按历史审计语义使用）。
HISTORICAL_STATUSES = ("historical", "superseded", "archived")


def route_geometry_fingerprint(path):
    """权威航路几何指纹（``path_fingerprint(path)`` 的只读再导出）。

    GIS 层只做再导出、不重新实现：任何图件都必须消费这一个指纹，
    绝不按 ``route_id`` 另造第二套航路几何身份。
    """

    return path_fingerprint(path)


def route_geometry_identity(route, *, path=None):
    """正式航路身份的**图件侧摘要**（至少含需求要求的全部绑定项）。

    ``route`` 是 canonical ``operational_routes[]`` 条目；缺失项一律显式为 ``None``，
    绝不猜测、绝不用 ``route_id`` 顶替几何指纹。
    """

    route = route if isinstance(route, dict) else {}
    cleaned = _clean_path(path if path is not None else route.get("path"))
    provenance = route.get("provenance") if isinstance(route.get("provenance"), dict) else {}
    return {
        "route_id": None if route.get("route_id") in (None, "") else str(route.get("route_id")),
        "route_authority": _route_authority(route),
        "route_geometry_fingerprint": route_geometry_fingerprint(cleaned),
        "route_geometry_fingerprint_prefix": ROUTE_GEOMETRY_FINGERPRINT_PREFIX,
        "route_point_count": len(cleaned),
        "path_crs": str(route.get("path_crs") or "") or None,
        "altitude_layer_id": _altitude_layer_id(route),
        "route_operational_status": _text(route.get("status")),
        "candidate_id": _text(provenance.get("candidate_id")),
        "candidate_fingerprint": _text(provenance.get("candidate_fingerprint")),
        "validation_id": _text(provenance.get("validation_id")),
        "validation_fingerprint": _text(provenance.get("validation_fingerprint")),
        "adoption_id": _text(provenance.get("adoption_id")),
        "adoption_fingerprint": _text(provenance.get("adoption_fingerprint")),
        "provenance_source_type": _text(provenance.get("source_type")),
        "route_result_version": _route_result_version(route),
    }


def fixed_corridor_width_identity():
    """固定 1000 m 保障宽带的**宽度契约身份**（唯一权威：Domain 层常量）。"""

    return {
        "width_policy_version": WIDTH_POLICY_VERSION,
        "corridor_half_width_m": CORRIDOR_HALF_WIDTH_M,
        "corridor_total_width_m": CORRIDOR_TOTAL_WIDTH_M,
        "width_semantics": WIDTH_SEMANTICS,
        "geometry_algorithm_id": GEOMETRY_ALGORITHM_ID,
        "geometry_algorithm_version": GEOMETRY_ALGORITHM_VERSION,
    }


def normalize_status(value, *, default=UNKNOWN):
    """把任意上游状态字符串规范化到五态之一（**绝不**把未知升级成 current）。"""

    text = _text(value)
    if not text:
        return default
    lowered = text.lower()
    if lowered in CURRENTNESS_VALUES:
        return lowered
    if lowered in ("passed", "available", "ok", "valid", "confirmed"):
        return CURRENT
    if lowered in ("stale", "stale_input", "outdated"):
        return STALE
    if lowered in BLOCKED_STATUSES:
        return BLOCKED
    if lowered in HISTORICAL_STATUSES:
        return HISTORICAL
    if lowered in NOT_EVALUATED_STATUSES:
        return UNKNOWN
    # 未登记取值：不得当成 current，也不得当成失败 —— 如实记为未评估。
    return default


def project_currentness(*, result_status=None, geometry_status=None):
    """综合一处上游证据，给出 ``(currentness, reason, truth_source)``。

    判定优先级（fail-closed，先看"能不能用"，再谈"有没有"）：

    1. 几何构造结果存在且不是 ``available`` → 沿用证据自身的状态（``blocked`` /
       ``stale`` / …），并带上原因；
    2. 结果状态是"未评估 / 阻断 / 历史" → 如实返回对应状态；
    3. 结果状态可用且几何构造可用 → ``current``；
    4. 两者都没有 → ``unknown``（缺失不等于通过）。
    """

    result_state = _text(result_status)
    geometry_state = _text(geometry_status)
    if geometry_state:
        normalized_geometry = normalize_status(geometry_state, default=UNKNOWN)
        if normalized_geometry != CURRENT:
            return (
                normalized_geometry,
                f"geometry_construction_status={geometry_state}",
                TRUTH_SOURCE_GEOMETRY_RESULT,
            )
    if not result_state:
        return UNKNOWN, "upstream_status_absent", TRUTH_SOURCE_ABSENT
    normalized = normalize_status(result_state, default=UNKNOWN)
    if normalized != CURRENT:
        return (
            normalized,
            f"upstream_status={result_state}",
            TRUTH_SOURCE_RESULT_STATUS,
        )
    if not geometry_state:
        # 结果可用，但没有任何几何构造证据：几何身份本身未被评估。
        return (
            UNKNOWN,
            "geometry_construction_status_absent",
            TRUTH_SOURCE_GEOMETRY_RESULT,
        )
    return CURRENT, None, TRUTH_SOURCE_RESULT_STATUS


def production_gate(layer_currentness):
    """汇总图层级 currentness，裁定这张图能否作为**正式 current 成果图**。

    ``layer_currentness`` 是 ``{layer_key: {"currentness": ..., "reason": ...}}``。

    返回：

    * ``current``   —— 全部图层都是 current：可作正式成果图；
    * ``stale``     —— 至少一个图层 stale/historical：只能按历史/非权威语义预览；
    * ``blocked``   —— 至少一个图层 blocked（几何构造被阻断）：图上必须给出阻断原因；
    * ``unknown``   —— 存在未评估图层，且没有更强的失效证据。

    规则里 **stale 优先于 blocked 之外的一切**：只要有一条链路已 stale，
    这张图就不是 current 成果，绝不因为其它图层可用而"看起来完整"。
    """

    states = {}
    reasons = {}
    for key, item in (layer_currentness or {}).items():
        value = currentness_of(item)
        states[str(key)] = value
        reason = _text((item or {}).get("reason") if isinstance(item, dict) else "")
        if reason:
            reasons[str(key)] = reason
    counts = {state: sorted(k for k, v in states.items() if v == state)
              for state in CURRENTNESS_VALUES}
    if counts[STALE] or counts[HISTORICAL]:
        status = STALE
    elif counts[BLOCKED]:
        status = BLOCKED
    elif counts[UNKNOWN]:
        status = UNKNOWN
    elif counts[CURRENT]:
        status = CURRENT
    else:
        # 没有任何图层：不是"通过"，而是没有可制图的证据。
        status = UNKNOWN
    return {
        "status": status,
        "is_current_figure": status == CURRENT,
        "drawable_as_current": status in DRAWABLE_AS_CURRENT,
        "authoritative_for_formal_deliverable": status == CURRENT,
        "stage_semantics": {
            CURRENT: "current_result_figure",
            STALE: "stale_or_historical_result_figure_not_authoritative",
            BLOCKED: "geometry_construction_blocked_not_drawable",
            UNKNOWN: "result_not_evaluated_or_evidence_absent",
            HISTORICAL: "historical_record_audit_only",
        }[status],
        "layer_states": states,
        "layer_reasons": reasons,
        "counts": {state: len(counts[state]) for state in CURRENTNESS_VALUES},
        "layers_by_state": counts,
        "no_silent_empty_figure": True,
        "never_upgrades_stale_to_current": True,
    }


def fixed_corridor_currentness(*, route_operational_status, projected, entry,
                               geometry_fingerprint, corridor_effective_status):
    """固定 1000 m 保障宽带的 currentness 判定（**唯一权威实现**）。

    判据（全部只读，顺序即优先级；任何一条不成立都不得当作 current）：

    1. 几何来源航路自身必须是有效正式运行航路（``status == "passed"``）；
    2. 走廊结果（P14）必须有只读投影，且有效状态不在 stale / 未评估集合里；
    3. P14 条目的 ``fixed_corridor_geometry`` 家族必须给出指纹，且与本次**重建**的
       指纹逐字符一致；
    4. 条目的 ``fixed_corridor_geometry_status`` 必须是 ``available`` / ``passed``。

    条目里的 ``corridor_geometry_fingerprint`` 是**离散评估几何**身份（W1A 明确保留
    其原语义），绝不能当作固定宽带几何指纹 —— 两者混用会把"离散评估身份相同"误判成
    "固定宽带几何 current"。

    返回 ``(currentness, reason, identity)``；``currentness`` 只会是 ``current`` /
    ``stale`` / ``unknown``。
    """

    fixed_geometry = (entry or {}).get("fixed_corridor_geometry") or {}
    expected = fixed_geometry.get("geometry_fingerprint")
    identity = {
        "corridor_result_status": corridor_effective_status,
        "corridor_result_algorithm_id": (projected or {}).get("algorithm_id"),
        "corridor_result_algorithm_version": (projected or {}).get("algorithm_version"),
        "route_operational_status": _text(route_operational_status),
        "expected_fixed_corridor_geometry_fingerprint": _text(expected),
        "rebuilt_fixed_corridor_geometry_fingerprint": _text(geometry_fingerprint),
        "discrete_corridor_geometry_fingerprint": (
            (entry or {}).get("corridor_geometry_fingerprint")
        ),
        "fixed_corridor_geometry_status": _text(
            (entry or {}).get("fixed_corridor_geometry_status")
        ),
    }
    if _text(route_operational_status) != "passed":
        # 来源航路本身不是 passed（stale / unresolved / 缺失）：几何不构成有效保障边界。
        return STALE, "route_operational_status_not_passed", identity
    if not projected:
        # 结果整体缺失：缺失不等于通过，绝不能因为"没有 stale 记录"就放行。
        return STALE, "corridor_result_not_current", identity
    if normalize_status(corridor_effective_status) != CURRENT:
        return STALE, "corridor_result_not_current", identity
    if entry is None:
        return STALE, "corridor_route_entry_missing", identity
    if not expected:
        return STALE, "fixed_corridor_geometry_identity_absent", identity
    if str(expected) != str(geometry_fingerprint):
        return STALE, "fixed_corridor_geometry_fingerprint_mismatch", identity
    if str((entry or {}).get("fixed_corridor_geometry_status") or "") not in (
            "available", "passed"):
        return STALE, "fixed_corridor_geometry_not_available", identity
    return CURRENT, None, identity


def geojson_outer_rings(encoded):
    """从 GeoJSON ``Polygon`` / ``MultiPolygon`` 取出**外环**（不含内环，绝不发明环）。

    返回 ``(rings, holes)``：``rings`` 是外环列表，``holes`` 是
    ``{polygon_index: [hole, ...]}``。任何不合法（顶点不足 / 未闭合 / 非有限数）的环
    一律**丢弃并如实少画**，绝不补齐、绝不自动闭合。
    """

    rings, holes = [], {}
    polygons = []
    if not isinstance(encoded, dict):
        return rings, holes
    kind = str(encoded.get("type") or "")
    coordinates = encoded.get("coordinates")
    if kind == "Polygon" and isinstance(coordinates, list):
        polygons = [coordinates]
    elif kind == "MultiPolygon" and isinstance(coordinates, list):
        polygons = list(coordinates)
    for polygon in polygons:
        if not isinstance(polygon, list) or not polygon:
            continue
        outer = _clean_ring(polygon[0])
        if not outer:
            continue
        index = len(rings)
        rings.append(outer)
        inner = [ring for ring in (_clean_ring(item) for item in polygon[1:]) if ring]
        if inner:
            holes[index] = inner
    return rings, holes


def _clean_ring(ring):
    if not isinstance(ring, list):
        return None
    points = []
    for point in ring:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        try:
            longitude, latitude = float(point[0]), float(point[1])
        except (TypeError, ValueError):
            continue
        if not (_finite(longitude) and _finite(latitude)):
            continue
        points.append([longitude, latitude])
    if len(points) < 4:
        return None
    if points[0] != points[-1]:
        # 未闭合的环不是"小瑕疵"：GeoJSON 外环必须闭合，这里如实判为不可用。
        return None
    return points


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def layer_currentness_entry(*, currentness, reason=None, identity=None, evidence=None):
    """图层级 currentness 记录（写进 ``LayerSpec.source_detail`` 与 FigureSpec）。"""

    return {
        "currentness": normalize_status(currentness, default=UNKNOWN),
        "reason": _text(reason) or None,
        "identity": dict(identity or {}),
        "evidence": dict(evidence or {}),
        "semantics": "figure_consumes_authoritative_status_verbatim",
        "missing_is_not_pass": True,
    }


def currentness_of(item):
    """从图层记录 / 字符串里取出 currentness（未知一律落到 ``unknown``）。"""

    if isinstance(item, str):
        return normalize_status(item)
    if isinstance(item, dict):
        return normalize_status(item.get("currentness"))
    return UNKNOWN


def signature():
    """本门禁自身的口径指纹：图件记录它即可区分"门禁版本变了"。"""

    return {
        "currentness_values": list(CURRENTNESS_VALUES),
        "drawable_as_current": list(DRAWABLE_AS_CURRENT),
        "route_geometry_fingerprint_prefix": ROUTE_GEOMETRY_FINGERPRINT_PREFIX,
        "fixed_corridor_width_identity": fixed_corridor_width_identity(),
        "never_upgrades_stale_to_current": True,
        "missing_is_not_pass": True,
    }


# ---- 内部工具 ---------------------------------------------------------------


def _text(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean_path(path):
    cleaned = []
    for point in path or []:
        if isinstance(point, (list, tuple)) and len(point) >= 2:
            try:
                cleaned.append([float(point[0]), float(point[1])])
            except (TypeError, ValueError):
                continue
    return cleaned


def _route_authority(route):
    authority = route.get("authority")
    if isinstance(authority, dict):
        return dict(authority)
    provenance = route.get("provenance") if isinstance(route.get("provenance"), dict) else {}
    summary = {
        "source_type": _text(provenance.get("source_type")),
        "adoption_fingerprint": _text(provenance.get("adoption_fingerprint")),
        "candidate_fingerprint": _text(provenance.get("candidate_fingerprint")),
        "validation_fingerprint": _text(provenance.get("validation_fingerprint")),
    }
    return summary if any(value is not None for value in summary.values()) else None


def _altitude_layer_id(route):
    for key in ("altitude_layer_id", "cruise_altitude_layer_id", "layer_id"):
        value = _text(route.get(key))
        if value:
            return value
    return None


def _route_result_version(route):
    """结果版本：只读投影给出的版本字段，缺失即 ``None``（不推断）。"""

    for key in ("result_version", "algorithm_version"):
        value = _text(route.get(key))
        if value:
            return value
    return None


__all__ = [
    "BLOCKED", "CURRENT", "CURRENTNESS_VALUES", "DRAWABLE_AS_CURRENT", "HISTORICAL",
    "NOT_EVALUATED_STATUSES", "ROUTE_GEOMETRY_FINGERPRINT_PREFIX", "STALE", "UNKNOWN",
    "currentness_of", "fixed_corridor_currentness", "fixed_corridor_width_identity",
    "geojson_outer_rings", "layer_currentness_entry",
    "normalize_status", "production_gate", "project_currentness",
    "route_geometry_fingerprint", "route_geometry_identity", "signature",
]
