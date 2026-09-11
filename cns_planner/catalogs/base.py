"""Shared read-only JSON catalog loader."""

import json
from pathlib import Path


def load_catalog(path, normalizer, catalog_id):
    source = Path(path)
    document = json.loads(source.read_text(encoding="utf-8"))
    if isinstance(document, list):
        document = {"items": document}
    if not isinstance(document, dict) or not isinstance(document.get("items"), list):
        raise ValueError(f"{catalog_id} catalog 必须包含 items 数组")
    items = [normalizer(item) for item in document["items"]]
    identifiers = [item[next(key for key in ("aircraft_id", "device_id") if key in item)] for item in items]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError(f"{catalog_id} catalog ID 重复")
    return {
        "status": "passed" if items else "missing_data", "catalog_id": catalog_id,
        "source": str(source.resolve()), "metadata": document.get("metadata") or {},
        "count": len(items), "items": items,
    }
