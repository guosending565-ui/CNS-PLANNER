"""Minimal real communication-tower (CNS site reference) contract.

本契约只描述**真实站址位置**。位置与通信性能严格分离：

* 本模块**不允许**出现 ``coverage_radius`` / ``transmit_power`` / ``frequency`` /
  ``antenna_height`` / ``capacity`` / ``cost`` / ``reliability`` / ``coverage``
  这类通信性能字段。一个真实铁塔站址 ≠ 一台已配置参数的 CNS 设备，本阶段
  绝不把 373 个铁塔自动转成 C/N/S existing site，也绝不生成覆盖。
* 只有源文件中**真实存在**的列才会被保留（海拔、塔身高度、铁塔细分类型、区域），
  缺失即保持 ``None``，不做任何推断或补默认值。

坐标系语义完全复用 :mod:`cns_planner.domain.reference_crs`：源文件声明什么就是
什么，解析本身永远不产生 CRS 证据。
"""

from __future__ import annotations

from copy import deepcopy

from .reference_crs import empty_crs_record, normalize_crs_record

TOWER_SCHEMA_VERSION = 1
COLLECTION_ID = "towers"

#: 除 ``tower_id`` 之外的必填空间字段（缺任一项该行即为 invalid）。
REQUIRED_SPATIAL_FIELDS = ("longitude", "latitude")

#: 源文件中真实存在、允许保留的原始属性（其余一律不写入契约）。
SOURCE_ATTRIBUTE_FIELDS = (
    "elevation_m", "height_m", "site_type", "district", "operator", "address", "remarks",
)

#: 明确禁止进入本契约的通信性能字段（防止"用起来"时被塞进来）。
FORBIDDEN_PERFORMANCE_FIELDS = (
    "coverage", "coverage_radius", "coverage_radius_m", "transmit_power",
    "frequency", "antenna_height", "antenna_gain", "capacity", "cost",
    "reliability", "availability", "cns_performance", "subsystems",
    "available_subsystems", "service_model",
)


def empty_towers(*, note=None):
    """A fully unresolved tower collection; nothing is measured against it yet."""

    return {
        "status": "not_calculated",
        "collection_id": COLLECTION_ID,
        "schema_version": TOWER_SCHEMA_VERSION,
        "source": None,
        "data_source": None,
        "metadata": {
            "source_type": "real",
            "source_mode": "real",
            # 站址位置与通信性能分离：本集合永不承载 CNS 能力结论。
            "semantics": "real_site_locations_only",
            "planning_integration": "reference_only",
            "cns_capability_derived": False,
        },
        "crs": empty_crs_record(note=note or (
            "源表未声明 CRS；坐标仅按源数值展示，不得自动假定 WGS84/CGCS2000。"
        )),
        "count": 0,
        "items": [],
        "skipped": [],
        "warnings": [],
    }


def normalize_crs(value, *, default=None):
    """Normalize a tower-collection CRS record through the shared semantics."""

    return normalize_crs_record(value, default=default or empty_crs_record())


def _optional_text(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_number(value, field):
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field} 不能是布尔值")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是数值") from exc


def _coordinate(item):
    """Accept ``coordinate`` or explicit ``longitude``/``latitude``; never guess axis order."""

    value = item.get("coordinate")
    if value is None and item.get("longitude") is not None and item.get("latitude") is not None:
        value = [item.get("longitude"), item.get("latitude")]
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        raise ValueError("坐标必须包含 longitude、latitude")
    longitude = _optional_number(value[0], "longitude")
    latitude = _optional_number(value[1], "latitude")
    if longitude is None or latitude is None:
        raise ValueError("坐标必须包含 longitude、latitude")
    return [longitude, latitude]


def validate_coordinate(coordinate):
    """Range validation only — this proves nothing about the CRS/datum."""

    if not isinstance(coordinate, (list, tuple)) or len(coordinate) < 2:
        return {"valid": False, "reason": "missing_coordinate"}
    longitude, latitude = coordinate[0], coordinate[1]
    if not -180.0 <= longitude <= 180.0:
        return {"valid": False, "reason": "longitude_out_of_range"}
    if not -90.0 <= latitude <= 90.0:
        return {"valid": False, "reason": "latitude_out_of_range"}
    if longitude == 0.0 and latitude == 0.0:
        return {"valid": False, "reason": "null_island_coordinate"}
    return {"valid": True, "reason": None}


def normalize_tower(item, index=0, *, source=None):
    """Normalize one tower row into the minimal, performance-free contract."""

    if not isinstance(item, dict):
        raise ValueError("铁塔条目必须是对象")
    raw_id = item.get("tower_id") or item.get("site_id")
    tower_id = _optional_text(raw_id)
    if not tower_id:
        raise ValueError("铁塔条目缺少 tower_id")
    coordinate = _coordinate(item)
    record = {
        "tower_id": tower_id,
        "name": _optional_text(item.get("name")) or tower_id,
        "longitude": coordinate[0],
        "latitude": coordinate[1],
        "coordinate": list(coordinate),
        "source_crs": _optional_text(item.get("source_crs")),
        "crs_confirmed": item.get("crs_confirmed") is True,
        "source": deepcopy(item.get("source")) if item.get("source") is not None else (
            deepcopy(source) if source is not None else None
        ),
        "evidence": deepcopy(item.get("evidence") or []),
    }
    for field in SOURCE_ATTRIBUTE_FIELDS:
        if field in ("elevation_m", "height_m"):
            record[field] = _optional_number(item.get(field), field)
        else:
            record[field] = _optional_text(item.get(field))
    # 源文件真实存在但契约暂不承载的原始列，原样留档以便追溯。
    record["raw_attributes"] = {
        str(key): (None if value is None else str(value))
        for key, value in (item.get("raw_attributes") or {}).items()
    }
    return record


def normalize_towers(items, *, source=None):
    """Normalize a row list; invalid rows are reported, never silently dropped."""

    records, skipped = [], []
    for index, item in enumerate(items or []):
        try:
            records.append(normalize_tower(item, index, source=source))
        except ValueError as exc:
            skipped.append({"index": index, "reason": str(exc)})
    return records, skipped
