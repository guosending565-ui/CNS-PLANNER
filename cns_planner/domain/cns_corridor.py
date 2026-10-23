"""Schema-v2 additive contracts for an engineering CNS requirement corridor.

Round38-W1A：水平宽度不再是"按航路可编辑的参数"，而是**唯一固定契约**
``fixed-1000m-v1``（中心线左右各 500 m、总横向宽度 1000 m）。本模块是该契约在
Domain 层的唯一归属地：

* 生产算法（P14/P15/P16…）一律通过 :func:`corridor_half_width_m` 取宽度，
  **绝不**再读取按航路可编辑的历史字段；
* 历史 ``cns_corridor_policy.routes[].horizontal_half_width_m`` 原值逐字节保留，
  只作为 legacy 审计证据；不等于 500 m 时被标记 ``stale``，但**不覆盖原值**；
* 垂向上下余量仍是独立、必须由用户显式确认的字段，不因水平契约固定而获得默认值
  或确认状态。
"""

from __future__ import annotations

from math import isfinite
from typing import TypedDict


#: 固定 1000 m CNS 服务保障宽带的唯一宽度策略版本。
WIDTH_POLICY_VERSION = "fixed-1000m-v1"
#: 中心线左右各 500 m。
CORRIDOR_HALF_WIDTH_M = 500.0
#: 总横向宽度 1000 m。
CORRIDOR_TOTAL_WIDTH_M = 1000.0
#: 该宽带描述"CNS 服务需要保障的二维目标区域"，不是法定空域或三维运行体。
WIDTH_SEMANTICS = "cns_service_target_band"
WIDTH_CAP_STYLE = "round"
WIDTH_JOIN_STYLE = "round"
WIDTH_REGULATORY_OPERATIONAL_VOLUME = "not_evaluated"
#: 几何构造算法身份（与 :mod:`cns_planner.gis.corridor_geometry` 保持同步）。
GEOMETRY_ALGORITHM_ID = "cns_fixed_corridor_buffer_v1"
GEOMETRY_ALGORITHM_VERSION = "1.0"

#: legacy 水平半宽的状态值。
HALF_WIDTH_STATUS_FIXED = "fixed_by_policy"
HALF_WIDTH_STATUS_LEGACY_STALE = "stale_legacy_value"
HALF_WIDTH_STATUS_ABSENT = "not_recorded"


class CNSCorridorSpec(TypedDict, total=False):
    route_id: str
    horizontal_half_width_m: float | None
    vertical_lower_margin_m: float | None
    vertical_upper_margin_m: float | None
    source: str | None
    confirmed: bool
    status: str


def fixed_corridor_width_contract():
    """返回固定宽度契约的只读副本（唯一权威，任何调用方都不得就地修改）。"""

    return {
        "width_policy_version": WIDTH_POLICY_VERSION,
        "corridor_half_width_m": CORRIDOR_HALF_WIDTH_M,
        "corridor_total_width_m": CORRIDOR_TOTAL_WIDTH_M,
        "width_semantics": WIDTH_SEMANTICS,
        "cap_style": WIDTH_CAP_STYLE,
        "join_style": WIDTH_JOIN_STYLE,
        "regulatory_operational_volume": WIDTH_REGULATORY_OPERATIONAL_VOLUME,
        "geometry_algorithm_id": GEOMETRY_ALGORITHM_ID,
        "geometry_algorithm_version": GEOMETRY_ALGORITHM_VERSION,
    }


def corridor_half_width_m(spec=None):
    """生产算法取水平半宽的**唯一入口**：永远返回固定契约值。

    ``spec`` 参数只为调用点可读性保留；**任何** legacy 字段都不会改变返回值。
    """

    return CORRIDOR_HALF_WIDTH_M


#: 共享米制 buffer 身份的**可序列化字段清单**（不含完整 ring 坐标）。
#:
#: canonical 结果与指纹只携带身份：完整边界坐标体积大且随 revision 变化，而身份
#: （fingerprint + 全部绑定项）已足以判定 current/stale。展示层需要时按身份重建。
CORRIDOR_GEOMETRY_IDENTITY_FIELDS = (
    "status", "reason", "geometry_algorithm_id", "geometry_algorithm_version",
    "width_policy_version", "width_semantics", "corridor_half_width_m",
    "corridor_total_width_m", "cap_style", "join_style", "quad_segs",
    "source_crs", "construction_crs", "metric_crs_source", "point_count",
    "route_length_projected_m", "route_length_geodesic_m",
    "projected_to_geodesic_length_ratio", "projected_area_m2",
    "projected_bounds_m", "boundary_ring_count", "is_valid",
    "geometry_fingerprint", "fingerprint_inputs",
)


def corridor_geometry_identity_summary(geometry):
    """从一次几何构造结果提取身份摘要（**纯字典投影**，不依赖任何 GIS 库）。

    放在 Domain 层是因为"哪些字段构成几何身份"本身就是契约的一部分：算法层因此
    可以在不 import 任何几何库的前提下消费身份，避免依赖方向被破坏。
    """

    if not isinstance(geometry, dict):
        return None
    return {key: geometry.get(key) for key in CORRIDOR_GEOMETRY_IDENTITY_FIELDS}


def corridor_total_width_m():
    return CORRIDOR_TOTAL_WIDTH_M


def default_cns_corridor_policy():
    return {
        "status": "pending_confirmation",
        "semantics": "engineering_cns_service_requirement_corridor",
        "regulatory_operational_volume": "not_evaluated",
        "width_policy_version": WIDTH_POLICY_VERSION,
        "corridor_half_width_m": CORRIDOR_HALF_WIDTH_M,
        "corridor_total_width_m": CORRIDOR_TOTAL_WIDTH_M,
        "routes": {},
    }


def normalize_corridor_spec(value, route_id=None):
    """归一化一条航路的走廊规格。

    水平宽度：**不再**是用户输入。历史 ``horizontal_half_width_m`` 原值逐字段保留
    （绝不覆盖、绝不自动迁移），并按是否等于固定契约值标注
    ``horizontal_half_width_status``；生产算法一律用
    :func:`corridor_half_width_m` 取固定值。

    垂向上下余量：仍是**独立、必须显式确认**的字段 —— 水平契约固定不会给它们带来
    默认值，也不会让它们自动变成 confirmed。
    """

    if not isinstance(value, dict):
        raise ValueError("CNSCorridorSpec 必须是对象")
    identifier = str(route_id or value.get("route_id") or "").strip()
    if not identifier:
        raise ValueError("CNSCorridorSpec.route_id 不能为空")
    fields = (
        "horizontal_half_width_m", "vertical_lower_margin_m",
        "vertical_upper_margin_m",
    )
    numbers = {name: _optional_nonnegative(value.get(name), name) for name in fields}
    legacy_width = numbers["horizontal_half_width_m"]
    if legacy_width is None:
        width_status = HALF_WIDTH_STATUS_ABSENT
    elif legacy_width == CORRIDOR_HALF_WIDTH_M:
        width_status = HALF_WIDTH_STATUS_FIXED
    else:
        width_status = HALF_WIDTH_STATUS_LEGACY_STALE
    #: 确认条件只覆盖**仍然需要用户决定**的垂向余量；水平宽度已由固定契约决定。
    complete = all(
        numbers[name] is not None
        for name in ("vertical_lower_margin_m", "vertical_upper_margin_m")
    )
    confirmed = bool(value.get("confirmed", False)) and complete
    return {
        "route_id": identifier,
        **numbers,
        "source": value.get("source"),
        "confirmed": confirmed,
        "status": "confirmed" if confirmed else "pending_confirmation",
        # ---- Round38-W1A：固定宽度契约（只读派生，绝不被请求覆盖） ----
        "width_policy_version": WIDTH_POLICY_VERSION,
        "effective_corridor_half_width_m": CORRIDOR_HALF_WIDTH_M,
        "effective_corridor_total_width_m": CORRIDOR_TOTAL_WIDTH_M,
        "horizontal_half_width_status": width_status,
        "horizontal_half_width_semantics": (
            "legacy_audit_value_only_not_current_width"
            if width_status == HALF_WIDTH_STATUS_LEGACY_STALE
            else "matches_fixed_width_policy"
            if width_status == HALF_WIDTH_STATUS_FIXED
            else "not_recorded_fixed_policy_applies"
        ),
    }


def normalize_cns_corridor_policy(value=None):
    raw = value if isinstance(value, dict) else {}
    route_values = raw.get("routes") or {}
    if isinstance(route_values, list):
        route_values = {str(item.get("route_id") or ""): item for item in route_values}
    if not isinstance(route_values, dict):
        raise ValueError("cns_corridor_policy.routes 必须是对象或数组")
    routes = {
        str(route_id): normalize_corridor_spec(spec, str(route_id))
        for route_id, spec in route_values.items() if str(route_id).strip()
    }
    legacy_stale = sorted(
        route_id for route_id, item in routes.items()
        if item["horizontal_half_width_status"] == HALF_WIDTH_STATUS_LEGACY_STALE
    )
    return {
        "status": "passed" if routes and all(item["status"] == "confirmed" for item in routes.values()) else "pending_confirmation",
        "semantics": "engineering_cns_service_requirement_corridor",
        "regulatory_operational_volume": "not_evaluated",
        "routes": routes,
        # ---- Round38-W1A：唯一的固定宽度契约 ----
        "width_policy_version": WIDTH_POLICY_VERSION,
        "corridor_half_width_m": CORRIDOR_HALF_WIDTH_M,
        "corridor_total_width_m": CORRIDOR_TOTAL_WIDTH_M,
        "width_semantics": WIDTH_SEMANTICS,
        "horizontal_width_editable": False,
        "legacy_stale_route_ids": legacy_stale,
    }


def empty_cns_corridor_assessment(status="not_calculated"):
    return {
        "status": status,
        "algorithm_id": "cns_service_corridor_v1",
        #: 与 ``CNSServiceCorridorV1.algorithm_version`` 保持同步。
        #: Round38-W1A：``1.3`` → ``1.4``（固定 1000 m 宽度契约 + 共享米制几何身份）。
        "algorithm_version": "1.4",
        "model_scope": "engineering_cns_service_requirement_corridor",
        "input_fingerprint": None,
        "corridor_geometry_fingerprint": None,
        "fixed_corridor_width_contract": fixed_corridor_width_contract(),
        "route_count": 0,
        "routes": [],
        "deficit_voxel_ids": [],
        "unknown_voxel_ids": [],
        "disclaimers": _disclaimers(),
    }


def corridor_disclaimers():
    return _disclaimers()


# ---- Phase4-B9R.1：唯一的规模准入估算件 ------------------------------------------
#
# 异步提交路径（``cns_planner.tasks.task_specs``）与同步入口
# （``cns_planner.application.corridor_service``）**必须**用同一份实现估算同一份输入，
# 否则两条路径会出现"同一规模、两种准入"的第二种真值。这里只是把估算件的归属收到
# domain 层，估算本身的数学仍完全属于
# :func:`cns_planner.algorithms.corridor.v1.estimate_corridor_complexity`。


def corridor_complexity_preflight(inputs):
    """对一份已组装的服务走廊输入做规模估算（纯函数：不写 state、不改 result）。

    ``inputs`` 的字段名以 P14 ``evaluate`` 的入参为准（``existing_facilities`` /
    ``device_catalog`` / ``corridor_policy``）；估算结果里的每个量都显式标注为上界，
    不参与任何数学判定，也不进入 canonical assessment schema。
    """

    from ..algorithms.corridor.v1 import estimate_corridor_complexity

    inputs = inputs if isinstance(inputs, dict) else {}
    return estimate_corridor_complexity(
        inputs.get("routes") or [], inputs.get("spatial_3d") or {},
        inputs.get("grid") or {}, inputs.get("grid_attributes") or {},
        inputs.get("corridor_policy") or {},
        inputs.get("existing_facilities") or {},
        inputs.get("device_catalog") or {},
        #: Round 2：估算与正式评估共用同一份 surface 事实（provider 为 callable，
        #: 只在本进程内使用；它绝不进入 immutable snapshot）。
        surface_class_provider=inputs.get("surface_class_provider"),
    )


def _disclaimers():
    return {
        "jarus_operational_volume": "not_evaluated",
        "u_space_surveillance_volume": "not_evaluated",
        "regulatory_approval": "not_evaluated",
        "evaluation_semantics": "representative_voxel_probe_not_entire_voxel_guarantee",
        "horizontal_discretization": "conservative_grid_cell_inclusion_not_exact_buffer",
        "volume_semantics": "discretized_volume_proxy_not_exact_corridor_volume",
        #: Round38-W1A：固定宽带语义披露。
        "width_semantics": WIDTH_SEMANTICS,
        "width_policy_version": WIDTH_POLICY_VERSION,
        "centerline_meets_does_not_imply_full_width_meets": (
            "centerline_service_status_and_full_width_service_status_are_distinct"
        ),
    }


def _optional_nonnegative(value, field):
    if value in (None, ""):
        return None
    number = float(value)
    if not isfinite(number) or number < 0:
        raise ValueError(f"{field} 必须是非负有限数值")
    return number
