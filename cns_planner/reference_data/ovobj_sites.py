"""Read-only parser for the Zhoushan ``.ovobj`` tower/site annotation file.

The file is an OviO object export.  It declares **no CRS**, no collection date
and no tower-height/equipment fields.  This module therefore only extracts what
the bytes actually contain: a label offset, a coordinate, a UTF-8 name, and the
``站址性质`` / ``站址类型`` tags.  It never infers a ground/roof height, never
merges same-name labels at different coordinates, and never writes a file.

The parser is a small, dependency-free version of the R1 read-only inspection
script (``research/r1/parse_ovobj.py``).  It is reused by the R5 scenario
builder so the supplementary sites are not hard-coded to one route.
"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
import struct

FIELD_NATURE = "站址性质"
FIELD_TYPE = "站址类型"
GROUND_TYPE = "地面站"
ROOF_TYPE = "楼面站"
INDOOR_TYPE = "室内站"
MICRO_TYPE = "微站"
VIRTUAL_TYPE = "虚拟站"


def _looks_like_name(raw: bytes) -> bool:
    if not raw:
        return False
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    if "\ufffd" in text:
        return False
    if not any("\u4e00" <= char <= "\u9fff" for char in text):
        return False
    allowed = set("()（）_-· 、.．#—–…～&＋+")
    for char in text:
        if "\u4e00" <= char <= "\u9fff":
            continue
        if char.isascii() and (char.isalnum() or char in allowed):
            continue
        if char in allowed:
            continue
        return False
    return True


def _read_name(buffer: bytes, position: int, max_length: int = 160):
    if position >= len(buffer):
        return None
    length = buffer[position]
    if length < 2 or length > max_length:
        return None
    if position + 1 + length >= len(buffer):
        return None
    raw = buffer[position + 1:position + 1 + length]
    if buffer[position + 1 + length] != 0:
        return None
    return raw


def _parse_tags(tags: str) -> dict:
    result = {}
    for line in tags.split("\n"):
        line = line.strip().lstrip("●").strip()
        if not line:
            continue
        for separator in ("：", ":"):
            if separator in line:
                key, value = line.split(separator, 1)
                result[key.strip()] = value.strip()
                break
        else:
            result[line] = ""
    return result


def parse_ovobj_bytes(data: bytes):
    """Parse raw ``.ovobj`` bytes into label records (read-only, deterministic)."""

    records = []
    for offset in range(0, len(data) - 40):
        try:
            latitude, longitude = struct.unpack_from("<dd", data, offset)
        except struct.error:
            continue
        if not (29.0 <= latitude <= 31.0 and 121.0 <= longitude <= 123.0):
            continue
        for position in range(offset + 16, min(offset + 48, len(data))):
            raw = _read_name(data, position)
            if not raw or not _looks_like_name(raw):
                continue
            try:
                name = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            tag_position = position + 1 + len(raw) + 1
            tags = ""
            tag_length = None
            tag_ok = False
            if tag_position + 4 <= len(data):
                tag_length = struct.unpack_from("<I", data, tag_position)[0]
                if 0 < tag_length <= 400 and tag_position + 4 + tag_length <= len(data):
                    raw_tag = data[tag_position + 4:tag_position + 4 + tag_length]
                    try:
                        tags = raw_tag.decode("utf-8")
                        tag_ok = True
                    except UnicodeDecodeError:
                        tags = raw_tag.decode("utf-8", errors="replace")
            fields = _parse_tags(tags)
            records.append({
                "offset": offset,
                "longitude": float(longitude),
                "latitude": float(latitude),
                "name": name,
                "tags": tags,
                "tag_utf8_ok": tag_ok,
                "tag_length": tag_length,
                "fields": fields,
                "site_nature": fields.get(FIELD_NATURE),
                "site_type": fields.get(FIELD_TYPE),
            })
            break
    records.sort(key=lambda item: item["offset"])
    seen = set()
    unique = []
    for record in records:
        if record["offset"] in seen:
            continue
        seen.add(record["offset"])
        unique.append(record)
    for index, record in enumerate(unique):
        record["label_index"] = index
    return unique


def normalize_ovobj_name(name):
    """Normalize only formatting, never identity.

    ``ZZ_`` is the export prefix observed in the source; punctuation variants
    are unified so the same official name can be matched.  The original name is
    preserved on every record.
    """

    text = str(name or "").strip()
    if text.startswith("ZZ_"):
        text = text[3:]
    text = (
        text.replace("—", "-").replace("–", "-").replace("～", "~")
        .replace("（", "(").replace("）", ")")
    )
    return text.strip()


def load_ovobj_records(path):
    """Load records from raw ``.ovobj`` bytes or a cached R1 ``.json`` export."""

    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"ovobj 文件不存在：{source}")
    if source.suffix.lower() == ".json":
        payload = json.loads(source.read_text(encoding="utf-8"))
        records = payload.get("records") if isinstance(payload, dict) else payload
        if not isinstance(records, list):
            raise ValueError("ovobj JSON 缓存缺少 records 数组")
        normalized = []
        for index, record in enumerate(records):
            item = dict(record)
            fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
            item.setdefault("site_nature", fields.get(FIELD_NATURE))
            item.setdefault("site_type", fields.get(FIELD_TYPE))
            item.setdefault("label_index", index)
            item.setdefault("tags", "")
            item.setdefault("tag_utf8_ok", True)
            normalized.append(item)
        return {
            "status": "passed",
            "source": str(source),
            "source_sha256": sha256(source.read_bytes()).hexdigest().upper(),
            "record_count": len(normalized),
            "unique_name_count": len({item.get("name") for item in normalized}),
            "records": normalized,
        }
    raw = source.read_bytes()
    records = parse_ovobj_bytes(raw)
    return {
        "status": "passed",
        "source": str(source),
        "source_sha256": sha256(raw).hexdigest().upper(),
        "record_count": len(records),
        "unique_name_count": len({item.get("name") for item in records}),
        "records": records,
    }


__all__ = [
    "FIELD_NATURE",
    "FIELD_TYPE",
    "GROUND_TYPE",
    "INDOOR_TYPE",
    "MICRO_TYPE",
    "ROOF_TYPE",
    "VIRTUAL_TYPE",
    "load_ovobj_records",
    "normalize_ovobj_name",
    "parse_ovobj_bytes",
]
