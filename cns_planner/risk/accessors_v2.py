"""Risk Framework V2 cell 读取的**唯一** canonical accessor。

``grid_risk_v2`` 的 canonical cell schema 由 :class:`cns_planner.risk.model_v2.GridRiskModelV2`
生产，自 Risk Framework V2 起即为::

    grid_risk_v2 = {
        "cells": {
            "<grid_id>": {
                "grid_id": ...,
                "status": ...,
                "domains": {"<domain_id>": {"domain_id":..., "status":..., "index":...}},
                "factors": {"<factor_id>": {"factor_id":..., "status":..., "normalized_index":...}},
                ...
            },
        },
    }

也就是说 domain 记录位于 **nested** 的 ``cell["domains"][domain_id]``，factor 记录位于
``cell["factors"][factor_id]``。Layered Route Planner 的 edge cost / ``cost_breakdown``、
RouteRiskProfile 的 exposure 积分，以及 profile 的 risk-cell fingerprint 必须复用**同一套**
读取规则；本模块是该规则的唯一实现，禁止出现第二套 flat 读取路径。

契约（全部为硬约束）::

* 未知 ``domain_id`` / ``factor_id`` 立即 ``ValueError``（fail loud，不静默）；
* 只读取 canonical nested 路径，**不**兼容历史错误的 flat ``cell[domain_id]``；
  flat / 类型非法的 cell 一律暴露为 ``missing_data`` / ``invalid_record``，让上游 fail-closed；
* 缺失就是缺失：``index`` 为 ``None``，**绝不补 0**，也不做任何默认值推断；
* 只有 ``status == "passed"`` 且 index 为 ``[0, 1]`` 内有限数时才视为已解析。

本模块不 import QGIS/GDAL，也不读任何文件。
"""

from __future__ import annotations

import math
from numbers import Real

from ..domain.risk_v2 import DOMAIN_IDS, FACTOR_IDS

#: canonical cell 内的 domain / factor 容器键。canonical schema 中它们是 nested 对象。
DOMAIN_CONTAINER_KEY = "domains"
FACTOR_CONTAINER_KEY = "factors"

#: 只有该状态的 domain 记录才携带可用的 relative engineering index。
RESOLVED_DOMAIN_STATUS = "passed"

#: cell / 容器 / 记录缺失或类型非法时使用的状态码（保持“缺失即缺失”，不补 0）。
MISSING_STATUS = "missing_data"
INVALID_STATUS = "invalid_record"


def finite(value):
    """有限实数判定：拒绝 bool、NaN 与 inf。"""

    return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(float(value))


def validate_domain_id(domain_id):
    """未知 domain 立即报错；canonical 契约不允许静默回退。"""

    if domain_id not in DOMAIN_IDS:
        raise ValueError(f"未知 Risk Framework V2 domain：{domain_id!r}（canonical：{DOMAIN_IDS}）")
    return domain_id


def validate_factor_id(factor_id):
    if factor_id not in FACTOR_IDS:
        raise ValueError(f"未知 Risk Framework V2 factor：{factor_id!r}")
    return factor_id


def domain_record_path(domain_id):
    """canonical 读取路径（仅用于 provenance / 诊断展示）。"""

    validate_domain_id(domain_id)
    return f"{DOMAIN_CONTAINER_KEY}.{domain_id}"


def factor_record_path(factor_id):
    validate_factor_id(factor_id)
    return f"{FACTOR_CONTAINER_KEY}.{factor_id}"


def cell_record(cell):
    """cell 本身；类型非法时返回空 dict（视为缺失，而不是伪造内容）。"""

    return cell if isinstance(cell, dict) else {}


def cell_container(cell, container_key):
    """读取 canonical cell 容器（``domains`` / ``factors``）；非法时返回空 dict。"""

    container = cell_record(cell).get(container_key)
    return container if isinstance(container, dict) else {}


def cell_domain_container(cell):
    """``cell["domains"]`` 的只读视图（缺失时为 ``{}``）。"""

    return cell_container(cell, DOMAIN_CONTAINER_KEY)


def cell_factor_container(cell):
    """``cell["factors"]`` 的只读视图（缺失时为 ``{}``）。"""

    return cell_container(cell, FACTOR_CONTAINER_KEY)


def cell_domain_record(cell, domain_id):
    """**只读**返回 ``cell["domains"][domain_id]``；缺失或非对象时返回 ``{}``。

    调用方不得修改返回值。这里**不**读取 ``cell[domain_id]``：历史错误的 flat schema
    必须暴露为缺失，而不是被静默兼容。
    """

    validate_domain_id(domain_id)
    record = cell_domain_container(cell).get(domain_id)
    return record if isinstance(record, dict) else {}


def cell_factor_record(cell, factor_id):
    """**只读**返回 ``cell["factors"][factor_id]``；缺失或非对象时返回 ``{}``。"""

    validate_factor_id(factor_id)
    record = cell_factor_container(cell).get(factor_id)
    return record if isinstance(record, dict) else {}


def cell_domain_index(cell, domain_id):
    """返回 canonical domain 记录的 ``(index, status)``；未解析时 ``index`` 为 ``None``。

    ``status`` 原样来自 canonical 记录（缺失时为 ``missing_data``）；index 只在
    ``status == "passed"`` 且落在 ``[0, 1]`` 内时才返回，**绝不补 0**。
    """

    record = cell_domain_record(cell, domain_id)
    status = str(record.get("status") or MISSING_STATUS)
    index = record.get("index")
    if status == RESOLVED_DOMAIN_STATUS and finite(index) and 0.0 <= float(index) <= 1.0:
        return float(index), status
    return None, status


def cell_factor_index(cell, factor_id):
    """返回 canonical factor 记录的 ``(normalized_index, status)``（缺失即 ``None``）。"""

    record = cell_factor_record(cell, factor_id)
    status = str(record.get("status") or MISSING_STATUS)
    index = record.get("normalized_index")
    return (float(index) if finite(index) else None), status


def cell_domains_view(cell):
    """cell 的 canonical domain 视图，供 fingerprint 使用。

    ``index`` / ``container_status`` 直接来自 canonical nested 记录；记录缺失时保持
    ``None``（缺失即缺失，不补 0、也不伪造状态），因此同一 canonical 数据在任何
    消费者处得到逐值一致的结果。
    """

    view = {}
    for domain_id in DOMAIN_IDS:
        record = cell_domain_record(cell, domain_id)
        view[domain_id] = {
            "index": record.get("index"),
            "container_status": record.get("status"),
        }
    return view


def cell_factors_view(cell):
    """cell 的 canonical factor 视图，供 fingerprint 使用（缺失保持 ``None``）。"""

    view = {}
    for factor_id in FACTOR_IDS:
        record = cell_factor_record(cell, factor_id)
        view[factor_id] = {
            "status": record.get("status"),
            "normalized_index": record.get("normalized_index"),
        }
    return view


__all__ = [
    "DOMAIN_CONTAINER_KEY", "FACTOR_CONTAINER_KEY", "INVALID_STATUS", "MISSING_STATUS",
    "RESOLVED_DOMAIN_STATUS",
    "cell_container", "cell_domain_container", "cell_domain_index", "cell_domain_record",
    "cell_domains_view", "cell_factor_container", "cell_factor_index", "cell_factor_record",
    "cell_factors_view", "cell_record", "domain_record_path", "factor_record_path", "finite",
    "validate_domain_id", "validate_factor_id",
]
