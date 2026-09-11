"""Read JSON, CSV and point GeoJSON into standard CNS facility/site inputs."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from ..domain.cns_inputs import normalize_candidate_site, normalize_existing_facility


class CNSInputAdapter:
    formats = {".json", ".geojson", ".csv"}

    def load_existing(self, payload):
        raw, source, metadata = self._read(payload)
        items = [normalize_existing_facility(item, index) for index, item in enumerate(raw)]
        items = self._merge_existing_sites(items)
        return self._collection("existing-cns-facilities", items, source, metadata)

    def load_candidates(self, payload):
        raw, source, metadata = self._read(payload)
        items = [normalize_candidate_site(item, index) for index, item in enumerate(raw)]
        self._unique(items, "site_id", "候选站址")
        return self._collection("candidate-sites", items, source, metadata)

    def _read(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("导入请求必须是对象")
        if isinstance(payload.get("items"), list):
            return payload["items"], {"type": "inline", "path": None}, payload.get("metadata") or {}
        raw_path = str(payload.get("path") or "").strip()
        if not raw_path:
            raise ValueError("请提供 path 或 items")
        path = Path(raw_path).expanduser().resolve()
        if not path.is_file() or path.suffix.lower() not in self.formats:
            raise ValueError("CNS 输入文件不存在或格式不支持；支持 JSON/CSV/GeoJSON")
        if path.suffix.lower() == ".csv":
            with path.open("r", encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            return [self._csv_row(row) for row in rows], {"type": "file", "path": str(path), "format": "CSV"}, {}
        document = json.loads(path.read_text(encoding="utf-8-sig"))
        raw, metadata = self._json_items(document)
        return raw, {"type": "file", "path": str(path), "format": path.suffix.lstrip(".").upper()}, metadata

    @staticmethod
    def _json_items(document):
        if isinstance(document, list):
            return document, {}
        if not isinstance(document, dict):
            raise ValueError("CNS JSON 必须是对象或数组")
        if document.get("type") == "FeatureCollection":
            items = []
            for feature in document.get("features") or []:
                geometry = feature.get("geometry") or {}
                if geometry.get("type") != "Point":
                    raise ValueError("CNS GeoJSON 仅支持 Point 要素")
                properties = dict(feature.get("properties") or {})
                properties["coordinate"] = geometry.get("coordinates")
                properties.setdefault("metadata", {})["feature_id"] = feature.get("id")
                items.append(properties)
            return items, document.get("metadata") or {}
        items = document.get("items")
        if not isinstance(items, list):
            raise ValueError("CNS JSON 必须包含 items 数组")
        return items, document.get("metadata") or {}

    @staticmethod
    def _csv_row(row):
        value = {key: current for key, current in row.items() if current not in (None, "")}
        if "longitude" in value and "latitude" in value:
            value["coordinate"] = [value.pop("longitude"), value.pop("latitude")]
        elif "lon" in value and "lat" in value:
            value["coordinate"] = [value.pop("lon"), value.pop("lat")]
        if "available_subsystems" in value:
            value["available_subsystems"] = [item.strip() for item in value["available_subsystems"].replace(";", ",").split(",") if item.strip()]
        return value

    @classmethod
    def _merge_existing_sites(cls, items):
        merged = {}
        for item in items:
            identifier = item["facility_id"]
            if identifier not in merged:
                merged[identifier] = item
                continue
            current = merged[identifier]
            if current["coordinate"] != item["coordinate"]:
                raise ValueError(f"已有设施 {identifier} 坐标冲突")
            known = {(device.get("device_id"), device.get("subsystem")) for device in current["devices"]}
            current["devices"].extend(device for device in item["devices"] if (device.get("device_id"), device.get("subsystem")) not in known)
        return list(merged.values())

    @staticmethod
    def _unique(items, key, label):
        identifiers = [item[key] for item in items]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError(f"{label} ID 重复")

    @staticmethod
    def _collection(collection_id, items, source, metadata):
        return {
            "status": "passed" if items else "missing_data", "collection_id": collection_id,
            "source": source, "metadata": metadata, "count": len(items), "items": items,
        }
