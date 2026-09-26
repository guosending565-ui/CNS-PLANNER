"""Phase4-B2B-1 Production Write Authority：canonical 结果的唯一 production writer 注册表。

本模块只做三件事，刻意不扩展为框架：

1. ``WRITE_ALLOWLIST``：每个 canonical authoritative key 声明**恰好一个** production
   content owner。任何新的 production 写入点都必须先在这里登记，否则
   :func:`assert_write_authority` 直接拒绝。
2. ``DERIVED_REVOKE_ALLOWLIST``：显式登记"派生回收"写点（清空/删除自己场景派生的
   canonical route）。这类写入删除的是派生结果，不是产生权威结果，因此单独登记，
   不与 production content owner 混在一张表里。
3. runtime-only compatibility cache 读写助手：archive/research/旧算法结果只能落在
   ``WorkflowSession`` 的进程内缓存，并且必须显式携带
   ``authoritative=false / compatibility=true / deprecated=true / source_algorithm``。
   这些结果**永不**进入 ``ProjectState``、save/reload 或 workflow snapshot，也永不被
   canonical workflow 消费。

设计约束（B2B-1 裁决）：

* guard 不做 dict 代理、不做事务包装、不在运行时拦截全部 state 写入；
* guard 由 canonical 写点在写入前显式调用（``assert_write_authority``）；
* 非 owner 的旧算法仍可运行，但只能写 runtime-only compatibility cache。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from ..compatibility.catalog import capability_metadata

#: canonical authoritative result key → 唯一 production content owner 类名。
#:
#: 六个 key 覆盖 Phase4-B2B/B2C 要求收敛的全部 canonical result。B2C 已在
#: ``Spatial3DService`` 与 ``CorridorSitePlanningService`` 的实际写点接入 guard；
#: registry 继续让任何新旁路立即被 contract test 捕获。
WRITE_ALLOWLIST: dict[str, tuple[str, ...]] = {
    "operational_routes": ("LayeredOperationalAdoptionService",),
    "required_cns": ("CNSInputService",),
    "coverage_3d": ("Spatial3DService",),
    "cns_corridor_site_plan": ("CorridorSitePlanningService",),
    "confirmed_cns_plan": ("PlanReviewService",),
    "radar_surveillance_layout": ("RadarSurveillanceLayoutService",),
}

#: 允许"回收派生结果"（清空 / 删除自己场景派生的 canonical route）的写点。
#:
#: 这些写点不产生权威事实，只删除已失效的派生产物；它们被显式登记，而不是被
#: 当成 production content owner。RouteService 的 generate_scenario /
#: generate_scenario_od / delete_route / delete_node 属于这一类。
DERIVED_REVOKE_ALLOWLIST: dict[str, tuple[str, ...]] = {
    "operational_routes": ("RouteService", "LayeredOperationalAdoptionService"),
    "required_cns": ("CNSInputService",),
}

#: ``WorkflowSession`` 上的 runtime-only cache 属性；不属于 ProjectState。
RUNTIME_COMPATIBILITY_CACHE_ATTRIBUTE = "_runtime_compatibility_results"

#: compatibility 记录必须显式携带的 authority 字段（最小 schema）。
COMPATIBILITY_REQUIRED_FIELDS = (
    "authoritative",
    "compatibility",
    "deprecated",
    "source_algorithm",
)


class ProductionWriteAuthorityError(RuntimeError):
    """非 owner 试图写 canonical authoritative result，或写点未在 allowlist 登记。"""


def owner_name(owner: Any) -> str:
    """解析写入者身份：实例 / 类 / 字符串都接受。"""

    if isinstance(owner, str):
        return owner
    if isinstance(owner, type):
        return owner.__name__
    return type(owner).__name__


def _owner_mro(owner: Any) -> tuple[str, ...]:
    if isinstance(owner, str):
        return (owner,)
    if isinstance(owner, type):
        return tuple(base.__name__ for base in owner.__mro__)
    return tuple(base.__name__ for base in type(owner).__mro__)


def assert_write_authority(owner: Any, result_key: str, *, operation: str = "write") -> str:
    """canonical 写点守卫。

    ``operation="write"`` 时只接受 :data:`WRITE_ALLOWLIST` 里登记的 production owner；
    ``operation="revoke"`` 时只接受 :data:`DERIVED_REVOKE_ALLOWLIST` 里登记的派生回收
    写点。返回实际匹配到的 owner 类名，便于调用方记录 provenance。
    """

    table = WRITE_ALLOWLIST if operation == "write" else DERIVED_REVOKE_ALLOWLIST
    allowed = table.get(str(result_key))
    if not allowed:
        raise ProductionWriteAuthorityError(
            f"unknown canonical result key: {result_key!r}"
        )
    names = _owner_mro(owner)
    for candidate in allowed:
        if candidate in names:
            return candidate
    raise ProductionWriteAuthorityError(
        f"{owner_name(owner)} 不是 {result_key!r} 的 {operation} 授权写入者；"
        f"允许：{', '.join(allowed)}"
    )


def is_authorized_writer(owner: Any, result_key: str, *, operation: str = "write") -> bool:
    try:
        assert_write_authority(owner, result_key, operation=operation)
    except ProductionWriteAuthorityError:
        return False
    return True


# --------------------------------------------------------------------- compatibility


def runtime_compatibility_results(session: Any, *, create: bool = False) -> dict:
    """读取会话级 compatibility cache；绝不读取或修改 ``session.state``。"""

    value = getattr(session, RUNTIME_COMPATIBILITY_CACHE_ATTRIBUTE, None)
    if isinstance(value, dict):
        return value
    if not create:
        return {}
    value = {}
    setattr(session, RUNTIME_COMPATIBILITY_CACHE_ATTRIBUTE, value)
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_runtime_compatibility_result(
    session: Any,
    name: str,
    payload: Mapping[str, Any],
    *,
    source_algorithm: Any = None,
    note: str | None = None,
) -> dict:
    """把兼容/归档结果写入当前进程的 ``WorkflowSession`` 缓存。

    canonical state 完全不变；该记录不会落盘、不会出现在通用 workflow snapshot、
    Open/Reload 后不会恢复。返回记录的所有权字段由本函数统一补齐。
    """

    record = deepcopy(dict(payload))
    algorithm_id = (
        source_algorithm.get("algorithm_id")
        if isinstance(source_algorithm, Mapping) else None
    )
    capability_id = {
        "risk_aware_route_planner_v2": "RiskAwareRoutePlannerV2",
        "route_planner_v1": "RoutePlannerV1",
        "coverage_planner_v1": "CoveragePlannerV1",
        "cns_gap_analysis_v1": "CNSGapAnalyzerV1",
        "cns_gap_analysis_v2": "CNSGapAnalyzerV2",
        "reuse_first_site_planner_v1": "ReuseFirstSitePlannerV1",
    }.get(str(algorithm_id)) or {
        "operational_routes": "RoutePlannerV1",
        "coverage": "CoveragePlannerV1",
        "cns_gap_analysis": "CNSGapAnalyzerV1",
        "cns_gap_analysis_v2": "CNSGapAnalyzerV2",
        "cns_site_plan": "ReuseFirstSitePlannerV1",
    }.get(str(name))
    if capability_id:
        record.update(capability_metadata(capability_id))
    else:
        record.update({
            "authoritative": False, "compatibility": True, "deprecated": True,
            "persistent_write": False, "production_authority": False,
        })
    record["result_key"] = str(name)
    record["source_algorithm"] = deepcopy(
        source_algorithm if source_algorithm is not None else record.get("source_algorithm")
    )
    results = runtime_compatibility_results(session, create=True)
    # ``recorded_at`` 是该 compatibility 结果**首次**写入当前会话的时间：重跑同一
    # 结果（例如同一输入的幂等试算）不会因为时间戳漂移而产生"新记录"。
    existing = results.get(str(name))
    previous_recorded_at = (
        existing.get("recorded_at") if isinstance(existing, dict) else None
    )
    record["recorded_at"] = record.get("recorded_at") or previous_recorded_at or _utc_now()
    record["note"] = str(
        note
        or "旧版/归档兼容结果：不是正式权威结果，不驱动 canonical 覆盖、能力缺口、"
        "设施规划或方案确认。"
    )
    record.setdefault("status", "not_calculated")
    results[str(name)] = record
    return record


def runtime_compatibility_result(session: Any, name: str) -> dict:
    """读取当前会话的兼容结果，不存在时返回空 dict（不创建容器）。"""

    return runtime_compatibility_results(session).get(str(name)) or {}


def runtime_compatibility_items(session: Any, name: str) -> list:
    value = runtime_compatibility_result(session, name).get("items")
    return list(value) if isinstance(value, list) else []


def read_compatibility_result(session: Any, state: Mapping[str, Any], name: str) -> dict:
    """按 B7X 契约读取一个旧结果：当前会话 runtime-only 结果优先，其次旧项目保存值。

    这是各 service 的统一读取入口：兼容结果缺失时返回 ``{}``，绝不 ``setdefault``
    或写回 ``ProjectState``。
    """

    runtime = runtime_compatibility_result(session, name)
    if runtime:
        return deepcopy(runtime)
    value = state.get(name)
    return deepcopy(value) if isinstance(value, Mapping) else {}


def drop_runtime_compatibility_result(session: Any, name: str) -> None:
    """移除当前会话的指定兼容结果。"""

    results = runtime_compatibility_results(session)
    results.pop(str(name), None)


def mark_runtime_compatibility_stale(session: Any, name: str, reason: str) -> bool:
    """把当前会话兼容结果标 stale；canonical state 不变。"""

    record = runtime_compatibility_results(session).get(str(name))
    if not isinstance(record, dict):
        return False
    if record.get("status") == "not_calculated":
        return False
    if record.get("status") == "stale" and record.get("stale_reason") == str(reason):
        return False
    record["status"] = "stale"
    record["stale_reason"] = str(reason)
    return True


# --------------------------------------------------------------------- read views


def canonical_operational_routes(state: Mapping[str, Any]) -> list:
    """canonical authoritative operational routes（唯一 owner 写入的那一份）。"""

    value = state.get("operational_routes")
    return list(value) if isinstance(value, list) else []


def runtime_compatibility_operational_routes(session: Any) -> list:
    """旧算法兼容试算航路（runtime-only，永不权威）。"""

    record = runtime_compatibility_result(session, "operational_routes")
    items = record.get("items")
    return list(items) if isinstance(items, list) else []


def operational_routes_for_legacy_consumers(state: Mapping[str, Any], session: Any) -> list:
    """legacy/compatibility 消费者的读取视图：canonical 优先，否则回退兼容试算。

    只有 archive/legacy 链（旧二维 coverage、旧站址、兼容导出等）允许使用它。
    canonical 消费者（coverage_3d / corridor site plan / plan review / report /
    radar / 走廊与能力评估）必须直接读 :func:`canonical_operational_routes`。
    """

    canonical = canonical_operational_routes(state)
    if canonical:
        return canonical
    return runtime_compatibility_operational_routes(session)


def compatibility_route_status(session: Any) -> str:
    record = runtime_compatibility_result(session, "operational_routes")
    status = record.get("status")
    return str(status) if status else "not_calculated"


def legacy_routes_ready(state: Mapping[str, Any], session: Any) -> bool:
    """legacy 门禁用：是否存在一份可用的运行航路（canonical 或兼容试算）。"""

    routes = operational_routes_for_legacy_consumers(state, session)
    if not routes:
        return False
    canonical = canonical_operational_routes(state)
    if canonical:
        return all(item.get("status") == "passed" for item in canonical)
    return compatibility_route_status(session) == "passed" and all(
        item.get("status") == "passed" for item in routes
    )


def iter_authority_entries() -> Iterable[tuple[str, tuple[str, ...]]]:
    """稳定顺序遍历 canonical authority registry（contract test 与审计使用）。"""

    for key in sorted(WRITE_ALLOWLIST):
        yield key, WRITE_ALLOWLIST[key]
