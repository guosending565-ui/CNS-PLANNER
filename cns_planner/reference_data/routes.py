"""Reference-only route and waypoint imports from converted source files."""

from __future__ import annotations

import csv
from hashlib import sha256
import json
from pathlib import Path
import re

from ..algorithms.coverage.v1 import distance_m
from .landing_sites import CRS_STATUS, parse_coordinate


COLLECTION_ID = "reference-routes"
_ALIASES = {
    "route_number": ("航线编号", "航路编号", "航线号", "route_number", "route_no"),
    "route_name": ("航线名称", "航路名称", "route_name"),
    "category": ("航线类别", "航线类型", "航路类别", "category"),
    "sequence": ("点序号", "点位序号", "航点序号", "航路点序号", "点序", "序号", "sequence", "seq"),
    "point_name": ("点位名称", "航点名称", "航路点名称", "点名称", "名称", "point_name"),
    "point_type": ("点位类型", "航点类型", "航路点类型", "点类型", "point_type"),
    "coordinate": ("经纬度信息", "经纬度", "坐标信息", "坐标", "coordinate"),
    "longitude": ("经度", "经度E", "E经度", "longitude", "lon", "lng", "x"),
    "latitude": ("纬度", "纬度N", "N纬度", "latitude", "lat", "y"),
}


def empty_reference_routes():
    return {
        "status": "not_calculated",
        "collection_id": COLLECTION_ID,
        "source": None,
        "metadata": {
            "source_type": "real", "source_mode": "real",
            "crs_status": CRS_STATUS, "usage": "reference_only",
            "planning_integration": "never_automatic",
        },
        "count": 0,
        "point_count": 0,
        "items": [],
        "points": [],
        "warnings": [],
    }


def _key(value):
    return re.sub(r"[\s\n\r\t（）()：:，,/_-]+", "", str(value or "")).casefold()


def _field(row, name):
    indexed = {_key(key): value for key, value in row.items()}
    for alias in _ALIASES[name]:
        value = indexed.get(_key(alias))
        if value not in (None, ""):
            return value
    return None


def _number(value):
    try:
        text = str(value).strip()
        match = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*[°度]?", text)
        number = float(match.group(1) if match else text)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def _coordinate(row):
    lon_raw, lat_raw = _field(row, "longitude"), _field(row, "latitude")
    lon, lat = _number(lon_raw), _number(lat_raw)
    if lon is not None and lat is not None:
        if -180 <= lon <= 180 and -90 <= lat <= 90:
            return {"coordinate": [lon, lat], "quality": "parsed", "warnings": []}
        return {"coordinate": None, "quality": "invalid", "warnings": ["coordinate_out_of_range"]}
    combined = _field(row, "coordinate")
    if combined in (None, "") and lon_raw not in (None, "") and lat_raw not in (None, ""):
        combined = f"E{lon_raw} N{lat_raw}"
    return parse_coordinate(combined)


def _read_csv(path):
    last_error = None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                reader = csv.DictReader(handle)
                if not reader.fieldnames:
                    raise ValueError("CSV 缺少表头")
                return [(row_number, dict(row)) for row_number, row in enumerate(reader, 2)]
        except UnicodeDecodeError as exc:
            last_error = exc
    raise ValueError("参考航线 CSV 编码无法识别") from last_error


def _read_xlsx(path):
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover
        raise ValueError("读取 XLSX 需要安装 geo extra 中的 openpyxl") from exc
    workbook = load_workbook(path, read_only=True, data_only=True)
    rows = []
    try:
        for sheet in workbook.worksheets:
            headers = None
            for row_number, values in enumerate(sheet.iter_rows(values_only=True), 1):
                values = list(values)
                keys = {_key(value) for value in values if value not in (None, "")}
                route_aliases = {_key(value) for value in _ALIASES["route_number"]}
                sequence_aliases = {_key(value) for value in _ALIASES["sequence"]}
                if keys & route_aliases and keys & sequence_aliases:
                    headers = [str(value or "") for value in values]
                    continue
                if headers and any(value not in (None, "") for value in values):
                    rows.append((sheet.title, row_number, dict(zip(headers, values))))
    finally:
        workbook.close()
    return rows


def _geojson_rows(path):
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    features = document.get("features") if isinstance(document, dict) else None
    if not isinstance(features, list):
        raise ValueError("参考航线 GeoJSON 必须是 FeatureCollection")
    rows = []
    for feature_number, feature in enumerate(features, 1):
        properties = dict((feature or {}).get("properties") or {})
        geometry = (feature or {}).get("geometry") or {}
        coordinates = geometry.get("coordinates") or []
        if geometry.get("type") == "Point":
            properties["longitude"], properties["latitude"] = coordinates[:2]
            rows.append((feature_number, properties))
        elif geometry.get("type") == "LineString":
            route_number = _field(properties, "route_number") or feature_number
            for sequence, coordinate in enumerate(coordinates, 1):
                row = dict(properties)
                row.update({"route_number": route_number, "sequence": sequence,
                            "longitude": coordinate[0], "latitude": coordinate[1]})
                rows.append((feature_number, row))
    return rows


def _route_id(route_number):
    return "RLR-" + sha256(_key(route_number).encode("utf-8")).hexdigest()[:12].upper()


def _point_id(route_number, sequence, name, point_type, coordinate):
    identity = "|".join((
        _key(route_number), str(sequence), _key(name), _key(point_type),
        ",".join(str(value) for value in coordinate or []),
    ))
    return "RLP-" + sha256(identity.encode("utf-8")).hexdigest()[:12].upper()


def _build(rows, source_path):
    grouped = {}
    invalid_rows = 0
    last_route_number = None
    last_route_name = None
    last_category = None
    for sheet, row_number, row in rows:
        route_number = _field(row, "route_number")
        sequence = _number(_field(row, "sequence"))
        parsed = _coordinate(row)
        if route_number not in (None, ""):
            last_route_number = route_number
            last_route_name = _field(row, "route_name")
            last_category = _field(row, "category")
        elif sequence is not None:
            route_number = last_route_number
        if route_number in (None, "") or sequence is None:
            invalid_rows += 1
            continue
        route_number = str(route_number).strip()
        point_name = str(_field(row, "point_name") or f"点 {sequence}").strip()
        point_type = _field(row, "point_type")
        route_id = _route_id(route_number)
        point = {
            "reference_route_point_id": _point_id(
                route_number, sequence, point_name, point_type, parsed["coordinate"]
            ),
            "route_id": route_id,
            "route_number": route_number,
            "sequence": sequence,
            "name": point_name,
            "type": None if point_type in (None, "") else str(point_type),
            "coordinate": parsed["coordinate"],
            "quality": parsed["quality"],
            "source": {"file": source_path.name, "sheet": sheet, "row": row_number},
            "crs_status": CRS_STATUS,
            "position": None,
            "warnings": list(parsed["warnings"]),
        }
        grouped.setdefault(route_number, {"rows": [], "points": [], "names": [], "categories": []})
        grouped[route_number]["rows"].append(row_number)
        grouped[route_number]["points"].append(point)
        grouped[route_number]["names"].append(_field(row, "route_name") or last_route_name)
        grouped[route_number]["categories"].append(_field(row, "category") or last_category)

    routes, points = [], []
    for route_number in sorted(grouped, key=lambda value: (_number(value) is None, _number(value) or 0, value)):
        group = grouped[route_number]
        ordered = sorted(group["points"], key=lambda item: (float(item["sequence"]), item["reference_route_point_id"]))
        for index, point in enumerate(ordered):
            point["position"] = "endpoint" if index in (0, len(ordered) - 1) else "intermediate"
        path = [point["coordinate"] for point in ordered if point["coordinate"]]
        warnings = []
        if len(path) != len(ordered):
            warnings.append("invalid_route_point_coordinate")
        if len(path) < 2:
            warnings.append("insufficient_valid_points_for_polyline")
        name = next((str(value).strip() for value in group["names"] if value not in (None, "")), f"航线 {route_number}")
        category = next((str(value).strip() for value in group["categories"] if value not in (None, "")), None)
        route_id = _route_id(route_number)
        for point in ordered:
            point["route_name"] = name
            point["category"] = category
        routes.append({
            "reference_route_id": route_id,
            "route_number": route_number,
            "name": name,
            "category": category,
            "ordered_points": [dict(point) for point in ordered],
            "ordered_point_ids": [point["reference_route_point_id"] for point in ordered],
            "path": path,
            "length_m": sum(distance_m(left, right) for left, right in zip(path, path[1:])),
            "source": {"file": source_path.name, "rows": sorted(group["rows"])},
            "provenance": {"group_by": "route_number", "order_by": "sequence"},
            "crs_status": CRS_STATUS,
            "warnings": warnings,
            "usage": "reference_only",
        })
        points.extend(ordered)
    return routes, points, invalid_rows


def load_reference_routes(path):
    source_path = Path(str(path or "")).expanduser()
    if not source_path.is_file():
        raise ValueError("参考航线源路径必须是存在的具体文件")
    suffix = source_path.suffix.lower()
    if suffix == ".et":
        result = empty_reference_routes()
        result.update({
            "status": "requires_xlsx_or_csv_conversion",
            "source": {"file": source_path.name, "format": "et"},
            "warnings": ["requires_xlsx_or_csv_conversion"],
        })
        return result
    if suffix == ".csv":
        rows = [(source_path.stem, row, values) for row, values in _read_csv(source_path)]
    elif suffix == ".xlsx":
        rows = _read_xlsx(source_path)
    elif suffix in (".geojson", ".json"):
        rows = [(source_path.stem, row, values) for row, values in _geojson_rows(source_path)]
    else:
        raise ValueError("参考航线仅支持 CSV/XLSX/GeoJSON；ET 需先转换")
    routes, points, invalid_rows = _build(rows, source_path)
    return {
        "status": "passed" if routes else "missing_data",
        "collection_id": COLLECTION_ID,
        "source": {"file": source_path.name, "format": suffix.lstrip(".")},
        "metadata": {
            "source_type": "real", "source_mode": "real",
            "crs_status": CRS_STATUS, "usage": "reference_only",
            "planning_integration": "never_automatic",
            "group_by": "route_number", "order_by": "sequence",
            "invalid_row_count": invalid_rows,
        },
        "count": len(routes),
        "point_count": len(points),
        "items": routes,
        "points": points,
        "warnings": ["source_crs_pending_confirmation"],
    }
