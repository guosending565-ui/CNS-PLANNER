"""Read-only landing-site reference import.

The returned collection is evidence for a human selection workflow.  It is not
the route planner's ``flow.nodes`` collection and does not declare a CRS that
the source workbook itself does not declare.
"""

from __future__ import annotations

import csv
from hashlib import sha256
from math import asin, cos, radians, sin, sqrt
from pathlib import Path
import re


COLLECTION_ID = "reference-landing-sites"
CRS_STATUS = "pending_confirmation"
_COORDINATE_HINT = re.compile(r"(?:东经|北纬|[EWNS]|[°度])", re.IGNORECASE)
_DMS = re.compile(
    r"(?P<hem>东经|北纬|[EWNS])?\s*[gG]?\s*"
    r"(?P<deg>\d{1,3}(?:\.\d+)?)\s*[°度]\s*"
    r"(?P<minute>\d{1,2}(?:\.\d+)?)\s*['′’分]\s*"
    r"(?P<second>\d{1,2}(?:\.\d+)?)\s*(?:[\"″”秒])?",
    re.IGNORECASE,
)
_DECIMAL_WITH_LABEL = re.compile(
    r"(?P<hem>东经|北纬|[EWNS])\s*(?P<value>-?\d{1,3}(?:\.\d+)?)",
    re.IGNORECASE,
)
_DECIMAL_PAIR = re.compile(
    r"^\s*(?P<lon>-?\d{2,3}(?:\.\d+)?)\s*[,，/\s]+"
    r"(?P<lat>-?\d{1,2}(?:\.\d+)?)\s*$"
)


def empty_reference_landing_sites():
    return {
        "status": "not_calculated",
        "collection_id": COLLECTION_ID,
        "source": None,
        "metadata": {
            "source_type": "real",
            "source_mode": "real",
            "crs_status": CRS_STATUS,
            "planning_integration": "selection_required",
        },
        "count": 0,
        "items": [],
        "warnings": [],
    }


def _axis(hemisphere):
    value = str(hemisphere or "").upper()
    if value in ("E", "W", "东经"):
        return "lon"
    if value in ("N", "S", "北纬"):
        return "lat"
    return None


def _signed(value, hemisphere):
    return -value if str(hemisphere or "").upper() in ("W", "S") else value


def parse_coordinate(raw):
    """Parse common Chinese/E/N decimal and DMS pairs without asserting a CRS."""
    original = "" if raw is None else str(raw).strip()
    warnings = []
    if not original:
        return {"coordinate": None, "quality": "invalid", "warnings": ["missing_coordinate"]}
    estimated = "估" in original
    text = (
        original.replace("\xa0", " ").replace("，", ",").replace("：", ":")
        .replace("‘", "'").replace("’", "'").replace("＇", "'")
        .replace("“", '"').replace("”", '"').replace("＂", '"')
    )
    if re.search(r"(?:^|[^A-Za-z])[gG]\s*\d", text):
        warnings.append("unrecognized_g_prefix")

    values = {}
    unlabeled = []
    matches = list(_DMS.finditer(text))
    for match in matches:
        hemisphere = match.group("hem")
        value = (
            float(match.group("deg"))
            + float(match.group("minute")) / 60.0
            + float(match.group("second")) / 3600.0
        )
        axis = _axis(hemisphere)
        if axis:
            values[axis] = _signed(value, hemisphere)
        else:
            unlabeled.append(value)

    if len(values) + len(unlabeled) < 2:
        labeled = list(_DECIMAL_WITH_LABEL.finditer(text))
        if len(labeled) >= 2:
            values = {
                _axis(item.group("hem")): _signed(float(item.group("value")), item.group("hem"))
                for item in labeled
            }
            unlabeled = []
        else:
            pair = _DECIMAL_PAIR.match(re.sub(r"(?:估|约|东经|北纬|[EN])\s*:?　?", "", text, flags=re.IGNORECASE))
            if pair:
                values = {"lon": float(pair.group("lon")), "lat": float(pair.group("lat"))}
                unlabeled = []

    if unlabeled:
        if "lon" not in values and unlabeled:
            values["lon"] = unlabeled.pop(0)
        if "lat" not in values and unlabeled:
            values["lat"] = unlabeled.pop(0)
        warnings.append("hemisphere_inferred_from_coordinate_order")

    lon, lat = values.get("lon"), values.get("lat")
    if lon is None or lat is None:
        return {"coordinate": None, "quality": "invalid", "warnings": warnings + ["coordinate_parse_failed"]}
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        return {"coordinate": None, "quality": "invalid", "warnings": warnings + ["coordinate_out_of_range"]}
    if estimated:
        warnings.append("source_marks_coordinate_estimated")
        quality = "estimated"
    elif "hemisphere_inferred_from_coordinate_order" in warnings:
        quality = "uncertain"
    else:
        quality = "parsed"
    return {"coordinate": [round(lon, 10), round(lat, 10)], "quality": quality, "warnings": warnings}


def _site_type(value, name):
    text = " ".join(str(item or "") for item in (value, name))
    for label in ("公共无人机起降场", "公共起降场", "A1直升机起降场", "直升机场起降点", "直升机起降点", "无人机起降点", "临时起降点", "起降场", "起降点"):
        if label in text:
            return label
    return None


def _record_from_row(values, source_file, sheet, row_number):
    cells = list(values[:8]) + [None] * max(0, 8 - len(values))
    if not isinstance(cells[0], (int, float)) and not str(cells[0] or "").strip().isdigit():
        return None
    coordinate_index = next(
        (index for index, value in enumerate(cells) if isinstance(value, str) and _COORDINATE_HINT.search(value)),
        None,
    )
    if coordinate_index not in (3, 4):
        return None
    coordinate_raw = cells[coordinate_index]
    parsed = parse_coordinate(coordinate_raw)
    warnings = list(parsed["warnings"])
    if coordinate_index == 3:
        region, name, explicit_type, location = cells[1], cells[2], None, cells[2]
        warnings.append("site_type_derived_from_name")
    else:
        region, location = cells[1 if row_number >= 55 else 2], cells[3]
        explicit_type = cells[2] if row_number >= 55 else cells[1]
        name = cells[3]
        if row_number < 28 and explicit_type and "起降" in str(explicit_type) and str(explicit_type) not in (
            "临时起降点", "临时起降点（暂无配套设施）", "起降点", "公共起降场",
        ):
            name = explicit_type
    site_type = _site_type(explicit_type, name)
    identity = "|".join((source_file, sheet, str(row_number), str(name or ""), str(coordinate_raw or "")))
    reference_site_id = "RLS-" + sha256(identity.encode("utf-8")).hexdigest()[:12].upper()
    raw = {f"column_{index + 1}": value for index, value in enumerate(cells) if value is not None}
    attributes = {}
    if row_number < 28:
        for key, value in (("area_m2", cells[5]), ("completed_time", cells[6]), ("remarks", cells[7])):
            if value is not None:
                attributes[key] = value
    return {
        "reference_site_id": reference_site_id,
        "name": str(name or location or f"来源行 {row_number}"),
        "region": None if region is None else str(region),
        "site_type": site_type,
        "location": None if location is None else str(location),
        "coordinate_raw": str(coordinate_raw),
        "coordinate": parsed["coordinate"],
        "quality": parsed["quality"],
        "crs_status": CRS_STATUS,
        "warnings": warnings,
        "possible_duplicate": False,
        "duplicate_candidates": [],
        "attributes": attributes,
        "raw": raw,
        "source": {"file": source_file, "sheet": sheet, "row": row_number},
    }


def _haversine_m(left, right):
    lon1, lat1, lon2, lat2 = map(radians, (*left, *right))
    dlon, dlat = lon2 - lon1, lat2 - lat1
    value = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 6371008.8 * 2 * asin(sqrt(value))


def _mark_duplicates(items, threshold_m=75.0):
    for left_index, left in enumerate(items):
        for right in items[left_index + 1:]:
            same_name = left["name"].strip().casefold() == right["name"].strip().casefold()
            close = bool(left["coordinate"] and right["coordinate"] and _haversine_m(left["coordinate"], right["coordinate"]) <= threshold_m)
            if not (same_name or close):
                continue
            for item, other in ((left, right), (right, left)):
                item["possible_duplicate"] = True
                item["duplicate_candidates"].append(other["reference_site_id"])
                if "possible_duplicate" not in item["warnings"]:
                    item["warnings"].append("possible_duplicate")


def _read_xlsx(path):
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - dependency contract
        raise ValueError("读取 XLSX 需要安装 geo extra 中的 openpyxl") from exc
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        for sheet in workbook.worksheets:
            for row_number, values in enumerate(sheet.iter_rows(min_col=1, max_col=8, values_only=True), 1):
                yield sheet.title, row_number, values
    finally:
        workbook.close()


def _read_csv(path):
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row_number, values in enumerate(csv.reader(handle), 1):
            yield path.stem, row_number, values


def load_reference_landing_sites(path):
    source_path = Path(str(path or "")).expanduser()
    suffix = source_path.suffix.lower()
    if suffix == ".et":
        result = empty_reference_landing_sites()
        result.update({
            "status": "requires_xlsx_or_csv_conversion",
            "source": {"file": source_path.name, "format": "et"},
            "warnings": ["requires_xlsx_or_csv_conversion"],
        })
        return result
    if suffix not in (".xlsx", ".csv"):
        raise ValueError("参考起降点仅支持 XLSX 或 CSV；ET 需先转换")
    if not source_path.is_file():
        raise ValueError("参考起降点源文件不存在")
    reader = _read_xlsx(source_path) if suffix == ".xlsx" else _read_csv(source_path)
    items = []
    for sheet, row_number, values in reader:
        item = _record_from_row(values, source_path.name, sheet, row_number)
        if item:
            items.append(item)
    _mark_duplicates(items)
    quality_counts = {name: sum(item["quality"] == name for item in items) for name in ("parsed", "estimated", "uncertain", "invalid")}
    return {
        "status": "passed" if items else "missing_data",
        "collection_id": COLLECTION_ID,
        "source": {"file": source_path.name, "format": suffix.lstrip(".")},
        "metadata": {
            "source_type": "real", "source_mode": "real",
            "crs_status": CRS_STATUS,
            "planning_integration": "selection_required",
            "coordinate_order": "lon_lat",
            "quality_counts": quality_counts,
            "possible_duplicate_count": sum(item["possible_duplicate"] for item in items),
        },
        "count": len(items),
        "items": items,
        "warnings": ["source_crs_pending_confirmation"],
    }
