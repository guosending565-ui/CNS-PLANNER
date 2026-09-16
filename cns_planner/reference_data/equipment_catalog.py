"""Canonical source-fact equipment reference catalog loader."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path


CATALOG_ID = "equipment-reference-catalog"
REQUIRED_ITEM_FIELDS = (
    "equipment_id", "manufacturer", "model", "name", "subsystems",
    "technology", "performance", "conditions", "reliability", "source",
    "evidence", "planning_mapping",
)


def empty_equipment_reference_catalog():
    return {
        "status": "not_calculated", "catalog_id": CATALOG_ID,
        "source": None, "metadata": {"source_type": "real", "source_mode": "real"},
        "count": 0, "items": [], "registered_sources": [],
    }


def normalize_equipment_reference_catalog(value):
    if not isinstance(value, dict):
        raise ValueError("设备参考目录必须是 JSON 对象")
    items = value.get("items")
    if not isinstance(items, list):
        raise ValueError("设备参考目录 items 必须是数组")
    normalized = []
    identities = set()
    for raw in items:
        if not isinstance(raw, dict):
            raise ValueError("设备参考记录必须是 JSON 对象")
        missing = [field for field in REQUIRED_ITEM_FIELDS if field not in raw]
        if missing:
            raise ValueError("设备参考记录缺少字段：" + "、".join(missing))
        item = deepcopy(raw)
        equipment_id = str(item.get("equipment_id") or "").strip()
        if not equipment_id or equipment_id in identities:
            raise ValueError("设备参考记录 equipment_id 缺失或重复")
        identities.add(equipment_id)
        item["equipment_id"] = equipment_id
        if not isinstance(item["subsystems"], list) or not isinstance(item["technology"], list):
            raise ValueError(f"{equipment_id} 的 subsystems/technology 必须是数组")
        if not isinstance(item["performance"], dict) or not isinstance(item["reliability"], dict):
            raise ValueError(f"{equipment_id} 的 performance/reliability 必须是对象")
        if not isinstance(item["conditions"], (dict, list)):
            raise ValueError(f"{equipment_id} 的 conditions 必须是对象或数组")
        if not isinstance(item["source"], dict) or not isinstance(item["evidence"], list):
            raise ValueError(f"{equipment_id} 的 source/evidence 格式无效")
        mapping = item.get("planning_mapping") or {}
        if mapping.get("status") not in ("not_mapped", "pending_confirmation"):
            raise ValueError(f"{equipment_id} 不得自动映射为规划设备")
        normalized.append(item)
    result = deepcopy(value)
    result.update({
        "status": "passed" if normalized else "missing_data",
        "catalog_id": CATALOG_ID,
        "count": len(normalized),
        "items": normalized,
    })
    result.setdefault("metadata", {})
    result["metadata"].update({
        "source_type": "real", "source_mode": "real",
        "planning_integration": "reference_only_not_device_catalog",
    })
    result.setdefault("registered_sources", [])
    return result


def load_equipment_reference_catalog(path=None):
    source_path = Path(path) if path else Path(__file__).resolve().parents[1] / "config" / "equipment_reference_catalog.json"
    value = json.loads(source_path.read_text(encoding="utf-8"))
    return normalize_equipment_reference_catalog(value)
