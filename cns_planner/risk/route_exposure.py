"""Layered Route Planner V1 与 RouteRiskProfile V1 共用的纯路径暴露积分。

这个模块是 backend-only 的**唯一**积分定义：planner 的 edge cost / ``cost_breakdown`` 与
RouteRiskProfile 的 ``exposure_index_m``、``route_length_m``、segment 积分必须逐值一致，
否则两套数学会在后续演进中悄悄漂移。

积分语义（与 Layered Route Planner V1 的 ``_path_metrics`` 完全一致，逐值不变）：

* ``points = [start, *grid centers, end]``；
* 端点 connector（``start -> 首 cell center`` 与 ``末 cell center -> end``）复用**首/尾 cell**
  的 domain index；
* 每个 segment 的 domain index 取 ``(left + right) / 2``；
* ``exposure = Σ(length * mean_index)``，按 segment 顺序用普通浮点累加。

没有任何默认值：index 缺失/未确认时该 segment 对该 domain 记为 ``resolved = False``，其长度
进入 unresolved 长度，绝不用 0 代替。本模块不 import QGIS/GDAL，也不读任何文件。
"""

from __future__ import annotations

from ..algorithms.coverage.v1 import distance_m
from ..domain.risk_v2 import DOMAIN_IDS
from .accessors_v2 import (
    RESOLVED_DOMAIN_STATUS, cell_domain_index, domain_record_path, finite,
)

#: ``grid_risk_v2`` 中每个 domain 在 cell 记录里的 **canonical 路径**。
#: planner 的 cost 与 profiler 使用**同一**读取规则（:mod:`.accessors_v2`），
#: 不允许出现第二套映射，也不保留历史错误的 flat ``cell[domain_id]`` 兼容。
DOMAIN_CELL_CONTAINER_KEY = {
    domain_id: domain_record_path(domain_id) for domain_id in DOMAIN_IDS
}

ENDPOINT_CONNECTOR_SEMANTICS = {
    "start_connector": "route_start_endpoint_to_first_grid_cell_center",
    "end_connector": "last_grid_cell_center_to_route_end_endpoint",
    "endpoint_index_source": "first_and_last_grid_cell_domain_index",
    "segment_index": "(left_index + right_index) / 2",
    "exposure": "sum_over_segments(length_m * mean_index)",
    "missing_index_is_never_zero": True,
}


def resolve_cell_domain_indices(grid_risk_v2, cell_ids, domain_ids):
    """解析每个 cell 每个 domain 的 index。

    读取规则完全来自 canonical accessor :func:`cns_planner.risk.accessors_v2.cell_domain_index`
    （即 ``cells[gid]["domains"][domain_id]``），因此 planner 的 edge cost 与 RouteRiskProfile
    的 exposure 积分逐值一致。

    返回 ``(resolved, unresolved)``，结构与 Layered Route Planner V1 原先的
    ``resolve_lambda_domain_indices`` 完全一致：``resolved[grid_id][domain_id]`` 是 index 或
    ``None``，``unresolved[grid_id]`` 是 ``"domain:status"`` 原因码列表。
    """

    cells = {}
    for grid_id, item in ((grid_risk_v2 or {}).get("cells") or {}).items():
        if isinstance(item, dict):
            cells[str(grid_id)] = item
    resolved, unresolved = {}, {}
    for grid_id in cell_ids:
        record = cells.get(str(grid_id)) or {}
        entry, reasons = {}, []
        for domain_id in domain_ids:
            index, status = cell_domain_index(record, domain_id)
            entry[domain_id] = index
            if index is None:
                reasons.append(f"{domain_id}:{status}")
        resolved[str(grid_id)] = entry
        if reasons:
            unresolved[str(grid_id)] = reasons
    return resolved, unresolved


def path_points(start, end, grid_path, centers):
    """``[start, *grid centers, end]`` —— planner 使用的同一个点列。"""

    return [list(start), *[centers[item] for item in grid_path], list(end)]


def index_source_grid_ids(grid_path):
    """每个点位的 domain index 来源 cell：``[首 cell, *cells, 尾 cell]``。"""

    return [grid_path[0], *grid_path, grid_path[-1]]


def point_domain_indices(grid_path, indices, domain_ids):
    """与 :func:`path_points` 一一对应的逐点 domain index（端点 connector 复用首/尾 cell）。"""

    return [
        {domain_id: (indices[grid_id] or {}).get(domain_id) for domain_id in domain_ids}
        for grid_id in index_source_grid_ids(grid_path)
    ]


def integrate_path_exposure(
    *, start, end, grid_path, centers, indices, domain_ids, integration_domains=None,
):
    """按 planner 语义积分一条候选路径。

    ``integration_domains`` 决定哪些 domain 参与 ``exposure_index_m`` 与总 exposure 累加
    （planner 只积分 λ>0 的 active domain；profiler 积分全部三个 domain）。每个 segment 仍然
    记录全部 ``domain_ids`` 的 start/end/mean index 与 ``resolved`` 标记。
    """

    domains = tuple(domain_ids)
    integrated = tuple(domains if integration_domains is None else integration_domains)
    points = path_points(start, end, grid_path, centers)
    source_grid_ids = index_source_grid_ids(grid_path)
    point_indices = point_domain_indices(grid_path, indices, domains)

    distance = 0.0
    exposure = {domain_id: 0.0 for domain_id in domains}
    segments = []
    cumulative = 0.0
    for position, (left, right, left_index, right_index) in enumerate(
        zip(points, points[1:], point_indices, point_indices[1:])
    ):
        length = distance_m(left, right)
        start_distance, cumulative = cumulative, cumulative + length
        distance = cumulative
        domains_record = {}
        for domain_id in domains:
            resolved = finite(left_index[domain_id]) and finite(right_index[domain_id])
            mean = (
                (float(left_index[domain_id]) + float(right_index[domain_id])) / 2.0
                if resolved else None
            )
            contribution = length * mean if (mean is not None and domain_id in integrated) else None
            if contribution is not None:
                exposure[domain_id] += contribution
            domains_record[domain_id] = {
                "start_index": left_index[domain_id],
                "end_index": right_index[domain_id],
                "mean_index": mean,
                "exposure_index_m": contribution,
                "resolved": bool(resolved),
            }
        start_grid_id, end_grid_id = source_grid_ids[position], source_grid_ids[position + 1]
        segments.append({
            "index": position,
            "length_m": length,
            "start_cumulative_distance_m": start_distance,
            "end_cumulative_distance_m": cumulative,
            "start_coordinate": list(left),
            "end_coordinate": list(right),
            "start_index_grid_id": start_grid_id,
            "end_index_grid_id": end_grid_id,
            "from_grid_id": None if position == 0 else start_grid_id,
            "to_grid_id": None if position == len(points) - 2 else end_grid_id,
            "from_grid_role": (
                "route_endpoint" if position == 0 else "grid_cell"
            ),
            "to_grid_role": (
                "route_endpoint" if position == len(points) - 2 else "grid_cell"
            ),
            "connector": _connector(position, len(points) - 1),
            "domains": domains_record,
        })
    return {
        "distance_m": distance,
        "domain_exposure_index_m": exposure,
        "segments": segments,
        "points": points,
        "point_domain_indices": point_indices,
        "index_source_grid_ids": source_grid_ids,
        "connector_semantics": ENDPOINT_CONNECTOR_SEMANTICS,
    }


def _connector(position, segment_count):
    if position == 0:
        return ENDPOINT_CONNECTOR_SEMANTICS["start_connector"]
    if position == segment_count - 1:
        return ENDPOINT_CONNECTOR_SEMANTICS["end_connector"]
    return "grid_cell_center_to_grid_cell_center"


__all__ = [
    "DOMAIN_CELL_CONTAINER_KEY", "ENDPOINT_CONNECTOR_SEMANTICS", "RESOLVED_DOMAIN_STATUS",
    "cell_domain_index", "finite", "index_source_grid_ids", "integrate_path_exposure",
    "path_points", "point_domain_indices", "resolve_cell_domain_indices",
]
