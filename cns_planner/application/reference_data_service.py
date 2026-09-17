"""Application boundary for reference-only facts, previews and CRS confirmation."""

from copy import deepcopy
import csv
from hashlib import sha256
import json
from pathlib import Path

from ..algorithms.coverage.v1 import distance_m
from ..domain.source_audit import provenance_record, source_manifest
from ..domain.geometry_health import inspect_geojson_geometries
from ..reference_data import (
    load_equipment_reference_catalog, load_reference_landing_sites, load_reference_routes,
)


class ReferenceDataService:
    def __init__(self, session, route_service, snapshot):
        self.session = session
        self.route_service = route_service
        self.snapshot = snapshot

    def ensure_equipment_catalog(self):
        current = self.session.state.get("equipment_reference_catalog") or {}
        if current.get("status") != "passed":
            self.session.state["equipment_reference_catalog"] = load_equipment_reference_catalog()

    def landing_sites_snapshot(self):
        return deepcopy(self.session.state.get("reference_landing_sites") or {})

    def equipment_catalog_snapshot(self):
        return deepcopy(self.session.state.get("equipment_reference_catalog") or {})

    def routes_snapshot(self):
        return deepcopy(self.session.state.get("reference_routes") or {})

    def import_landing_sites(self, path, save=True):
        previous = self.session.state.get("reference_landing_sites") or {}
        result = load_reference_landing_sites(path)
        self._migrate_landing_site_references(previous, result)
        self.session.state["reference_landing_sites"] = result
        valid = [item["coordinate"] for item in result.get("items") or [] if item.get("coordinate")]
        self._store_audit("reference_landing_sites", path, {
            "row_count": result.get("count"), "feature_count": result.get("count"),
            "extent": ([min(p[0] for p in valid), min(p[1] for p in valid),
                        max(p[0] for p in valid), max(p[1] for p in valid)] if valid else None),
            "geometry_health": {
                "status": "warning" if (result.get("metadata") or {}).get("quality_counts", {}).get("invalid") else "passed",
                "feature_count": result.get("count"), "null": 0, "empty": 0,
                "invalid": (result.get("metadata") or {}).get("quality_counts", {}).get("invalid", 0),
                "unsupported": 0,
            },
        })
        if save:
            self.session.save()
        return self.snapshot()

    def _migrate_landing_site_references(self, previous, current):
        old_by_id = {
            item.get("reference_site_id"): item
            for item in previous.get("items") or [] if item.get("reference_site_id")
        }
        new_items = current.get("items") or []
        for node in self.session.state.get("nodes") or []:
            old_id = node.get("reference_site_id")
            old = old_by_id.get(old_id)
            if not old:
                continue
            candidates = [item for item in new_items if self._same_landing_site(old, item)]
            if len(candidates) != 1 or candidates[0].get("reference_site_id") == old_id:
                continue
            new_id = candidates[0]["reference_site_id"]
            node["reference_site_id"] = new_id
            provenance = node.setdefault("provenance", {})
            provenance.setdefault("legacy_reference_site_ids", []).append(old_id)
            provenance["reference_site_id"] = new_id

    @staticmethod
    def _same_landing_site(left, right):
        left_coordinate, right_coordinate = left.get("coordinate"), right.get("coordinate")
        return (
            str(left.get("name") or "").strip().casefold() == str(right.get("name") or "").strip().casefold()
            and left_coordinate == right_coordinate
        )

    def import_routes(self, path, save=True):
        result = load_reference_routes(path)
        self.session.state["reference_routes"] = result
        self._store_audit("reference_routes", path, {
            "row_count": result.get("point_count", 0) + int((result.get("metadata") or {}).get("invalid_row_count") or 0),
            "feature_count": result.get("count"),
            "geometry_health": {
                "status": "warning" if (result.get("metadata") or {}).get("invalid_row_count") else "passed",
                "feature_count": result.get("count"), "null": 0, "empty": 0,
                "invalid": int((result.get("metadata") or {}).get("invalid_row_count") or 0),
                "unsupported": 0,
            },
        })
        if save:
            self.session.save()
        return self.snapshot()

    def preview_routes(self, path, conversion=None):
        source = Path(str(path or "")).expanduser()
        if not source.is_file():
            raise ValueError("参考航线源路径必须是存在的具体文件")
        if source.suffix.lower() == ".et":
            preview = {
                "status": "requires_xlsx_or_csv_conversion",
                "reason": "requires_xlsx_or_csv_conversion", "route_count": 0,
                "point_count": 0, "warnings": ["requires_xlsx_or_csv_conversion"],
                "source_audit": source_manifest("reference_routes", source),
            }
            self.session.state["reference_route_import_preview"] = preview
            self.session.save()
            return self.snapshot()
        parsed = load_reference_routes(source)
        columns = self._source_columns(source)
        coordinates = [
            item.get("coordinate") for item in parsed.get("points") or []
            if isinstance(item.get("coordinate"), list)
        ]
        gaps = []
        conflicts = self._duplicate_route_number_conflicts(source)
        for route in parsed.get("items") or []:
            sequences = sorted({int(item["sequence"]) for item in route.get("ordered_points") or []
                                if isinstance(item.get("sequence"), (int, float))})
            missing = sorted(set(range(sequences[0], sequences[-1] + 1)) - set(sequences)) if sequences else []
            if missing:
                gaps.append({"route_number": route["route_number"], "missing_sequences": missing})
        invalid = int((parsed.get("metadata") or {}).get("invalid_row_count") or 0)
        bounds = (
            [min(p[0] for p in coordinates), min(p[1] for p in coordinates),
             max(p[0] for p in coordinates), max(p[1] for p in coordinates)]
            if coordinates else None
        )
        preview_basis = {
            "file_name": source.name, "size": source.stat().st_size,
            "mtime_ns": source.stat().st_mtime_ns, "routes": parsed.get("items"),
        }
        preview_id = "RIP-" + sha256(json.dumps(
            preview_basis, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()[:16].upper()
        geometry_health = {
            "status": "passed" if not invalid else "warning",
            "feature_count": parsed.get("route_count", parsed.get("count", 0)),
            "null": 0, "empty": 0, "invalid": invalid, "unsupported": 0,
            "extent": bounds,
        }
        if source.suffix.lower() in (".geojson", ".json"):
            document = json.loads(source.read_text(encoding="utf-8-sig"))
            geometry_health = inspect_geojson_geometries(
                document.get("features") or [], {"Point", "LineString"},
            )
        preview = {
            "status": (
                "ready_for_confirmation"
                if parsed.get("items") and geometry_health.get("status") != "blocked"
                else "blocked"
            ),
            "preview_id": preview_id,
            "file_name": source.name,
            "format": source.suffix.lower().lstrip("."),
            "route_count": parsed.get("count", 0),
            "point_count": parsed.get("point_count", 0),
            "valid_coordinate_count": len(coordinates),
            "invalid_coordinate_count": invalid + sum(
                1 for item in parsed.get("points") or [] if not item.get("coordinate")
            ),
            "duplicate_route_numbers": sorted(conflicts),
            "sequence_gaps": gaps,
            "columns": columns,
            "bounds": bounds,
            "warnings": sorted(set((parsed.get("warnings") or []) +
                                   (["sequence_gaps"] if gaps else []) +
                                   (["duplicate_route_number_conflict"] if conflicts else []) +
                                   (["invalid_geometry_fail_closed"] if geometry_health.get("status") == "blocked" else []))),
            "source_audit": source_manifest("reference_routes", source, details={
                "schema": {"columns": columns}, "row_count": len(coordinates) + invalid,
                "feature_count": parsed.get("count", 0), "extent": bounds,
                "geometry_health": geometry_health,
            }),
            "confirmation_required": True,
            "replacement_semantics": "replace_reference_routes_only_after_user_confirmation",
            "conversion_record": deepcopy(conversion) if conversion else None,
        }
        self.session.state["reference_route_import_preview"] = preview
        self._store_audit("reference_routes", source, {
            "schema": {"columns": columns}, "row_count": len(coordinates) + invalid,
            "feature_count": parsed.get("count", 0), "extent": bounds,
            "geometry_health": geometry_health,
        })
        self.session.save()
        return self.snapshot()

    def confirm_routes_import(self, path, preview_id):
        preview = self.session.state.get("reference_route_import_preview") or {}
        if not preview_id or preview.get("preview_id") != preview_id:
            raise ValueError("导入预览已变化或不存在，请重新预览")
        if preview.get("status") != "ready_for_confirmation":
            raise ValueError("导入预览未通过，不能替换 reference_routes")
        source = Path(str(path or "")).expanduser()
        current = self.preview_routes(source, preview.get("conversion_record"))["reference_route_import_preview"]
        if current.get("preview_id") != preview_id:
            raise ValueError("源文件在预览后发生变化，请重新确认")
        result = load_reference_routes(source)
        result["import_preview_id"] = preview_id
        result["provenance"] = provenance_record(
            source_entity=preview["source_audit"]["source_id"],
            processing_activity="confirmed_reference_route_import",
            derived_entity=result["collection_id"], method="CSV/XLSX/GeoJSON parser",
            note="用户确认预览后替换 reference_routes；未进入规划结果。",
        )
        result["provenance"]["conversion_record"] = deepcopy(preview.get("conversion_record"))
        self.session.state["reference_routes"] = result
        self.session.state["reference_route_import_preview"] = None
        self.session.save()
        return self.snapshot()

    def confirm_crs(self, role, payload):
        if role not in ("reference_landing_sites", "reference_routes"):
            raise ValueError("只支持 reference_landing_sites/reference_routes CRS 确认")
        if not isinstance(payload, dict):
            raise ValueError("CRS 确认请求必须是对象")
        value = str(payload.get("value") or "").strip()
        source = payload.get("source")
        evidence = payload.get("evidence")
        if not value or source in (None, "") or not isinstance(evidence, list) or not evidence:
            raise ValueError("CRS 确认必须包含 value/source/evidence")
        try:
            from pyproj import CRS, Transformer
            parsed = CRS.from_user_input(value)
        except ImportError as exc:
            raise ValueError("CRS 验证需要安装 geo extra 中的 pyproj") from exc
        except Exception as exc:
            raise ValueError(f"CRS 无效：{value}") from exc
        canonical = parsed.to_authority()
        canonical_value = f"{canonical[0]}:{canonical[1]}" if canonical else parsed.to_string()
        axis_order = " / ".join(
            f"{axis.abbrev or axis.name}:{axis.direction}" for axis in parsed.axis_info
        ) or "unspecified"
        collection = self.session.state.get(role) or {}
        if not collection.get("items"):
            raise ValueError("参考数据为空，无法确认 CRS")
        previous_value = (((collection.get("crs") or {}).get("source_crs") or {}).get("value"))
        transformer = Transformer.from_crs(parsed, "OGC:CRS84", always_xy=True)

        def transformed(point):
            if not isinstance(point, list) or len(point) < 2:
                return None
            lon, lat = transformer.transform(float(point[0]), float(point[1]))
            if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                raise ValueError("CRS 转换后的坐标超出经纬度范围")
            return [float(lon), float(lat)]

        crs_record = deepcopy(collection.get("crs") or {})
        crs_record["source_crs"] = {
            "value": canonical_value, "status": "confirmed", "confirmed": True,
            "axis_order": axis_order, "source": deepcopy(source),
            "evidence": deepcopy(evidence),
        }
        crs_record["representation_crs"] = {
            "value": "OGC:CRS84", "status": "declared", "axis_order": "lon_lat",
            "declared_by_format": False,
            "source": {"type": "coordinate_transformation"},
            "evidence": [{"type": "pyproj_transformer", "always_xy": True}],
        }
        collection["crs"] = crs_record
        if role == "reference_landing_sites":
            for item in collection.get("items") or []:
                source_coordinate = item.setdefault("source_coordinate", deepcopy(item.get("coordinate")))
                item["coordinate"] = transformed(source_coordinate)
                item["crs"] = deepcopy(crs_record)
                item["crs_status"] = "confirmed"
                item["metric_measurement_status"] = "enabled"
        else:
            point_by_id = {}
            for point in collection.get("points") or []:
                source_coordinate = point.setdefault("source_coordinate", deepcopy(point.get("coordinate")))
                point["coordinate"] = transformed(source_coordinate)
                point["crs"] = deepcopy(crs_record)
                point["crs_status"] = "confirmed"
                point_by_id[point.get("reference_route_point_id")] = point
            for route in collection.get("items") or []:
                original = route.setdefault("source_numeric_path", deepcopy(route.get("path") or []))
                route["path"] = [transformed(point) for point in original if point]
                route["ordered_points"] = [
                    deepcopy(point_by_id[point_id]) for point_id in route.get("ordered_point_ids") or []
                    if point_id in point_by_id
                ]
                route["length_m"] = sum(
                    distance_m(left, right) for left, right in zip(route["path"], route["path"][1:])
                )
                route["length_status"] = "passed"
                route["length_unresolved_reason"] = None
                route["metric_geometry_similarity_available"] = True
                route["crs"] = deepcopy(crs_record)
                route["crs_status"] = "confirmed"
        collection.setdefault("metadata", {})["metric_measurement_status"] = "enabled"
        collection["metadata"]["transformation"] = {
            "source_crs": canonical_value, "target_crs": "OGC:CRS84",
            "method": "pyproj.Transformer.from_crs", "always_xy": True,
        }
        audit = ((self.session.state.get("source_audits") or {}).get("items") or {}).get(role)
        collection["crs_confirmation_provenance"] = provenance_record(
            source_entity=(audit or {}).get("source_id") or f"{role}:configured_source",
            processing_activity="manual_crs_confirmation_and_representation_transform",
            derived_entity=collection.get("collection_id") or role,
            method="pyproj.Transformer.from_crs(source, OGC:CRS84, always_xy=True)",
            note="CRS 值由用户提供证据并确认；系统仅校验和转换，不推测。",
        )
        collection["metric_comparison_status"] = "ready"
        collection["warnings"] = [
            warning for warning in collection.get("warnings") or []
            if "crs_pending" not in warning and "unresolved_source_crs" not in warning
        ]
        if audit is not None:
            audit["confirmed_crs"] = deepcopy(crs_record["source_crs"])
        if previous_value and previous_value != canonical_value:
            links = self.session.state.get("reference_route_links") or {}
            if role == "reference_routes" and links.get("items"):
                links["status"] = "stale_crs_changed"
                for item in links["items"]:
                    item["current_applicability"] = "stale_crs_changed"
                    item["confirmed"] = False
            for experiment in (self.session.state.get("route_planning_experiments") or {}).get("records") or []:
                experiment["reference_comparison_status"] = "stale_crs_changed"
        self.session.save()
        return self.snapshot()

    @staticmethod
    def _source_columns(source):
        suffix = source.suffix.lower()
        if suffix == ".csv":
            for encoding in ("utf-8-sig", "gb18030"):
                try:
                    with source.open("r", encoding=encoding, newline="") as handle:
                        return list(csv.DictReader(handle).fieldnames or [])
                except UnicodeDecodeError:
                    continue
        if suffix == ".xlsx":
            from openpyxl import load_workbook
            workbook = load_workbook(source, read_only=True, data_only=True)
            try:
                columns = []
                for sheet in workbook.worksheets:
                    for values in sheet.iter_rows(values_only=True):
                        current = [str(value) for value in values if value not in (None, "")]
                        if current:
                            columns.extend(value for value in current if value not in columns)
                            break
                return columns
            finally:
                workbook.close()
        if suffix in (".geojson", ".json"):
            document = json.loads(source.read_text(encoding="utf-8-sig"))
            return sorted({
                key for feature in document.get("features") or []
                for key in ((feature or {}).get("properties") or {})
            })
        return []

    def _store_audit(self, role, path, details):
        audits = self.session.state.setdefault("source_audits", {
            "status": "not_calculated", "schema_version": 1, "count": 0, "items": {},
        })
        previous = audits.setdefault("items", {}).get(role)
        audits["items"][role] = source_manifest(role, path, previous=previous, details=details)
        audits["count"] = len(audits["items"])
        audits["status"] = "passed"

    @staticmethod
    def _duplicate_route_number_conflicts(source):
        from ..reference_data.routes import (
            _field, _geojson_rows, _read_csv, _read_xlsx,
        )

        suffix = source.suffix.lower()
        if suffix == ".csv":
            rows = [(source.stem, row, values) for row, values in _read_csv(source)]
        elif suffix == ".xlsx":
            rows = _read_xlsx(source)
        elif suffix in (".geojson", ".json"):
            rows = [(source.stem, row, values) for row, values in _geojson_rows(source)]
        else:
            return []
        identities = {}
        last_number = None
        for _sheet, _row, values in rows:
            number = _field(values, "route_number")
            if number not in (None, ""):
                last_number = str(number).strip()
            elif last_number:
                number = last_number
            if number in (None, ""):
                continue
            identity = (
                str(_field(values, "route_name") or "").strip().casefold(),
                str(_field(values, "category") or "").strip().casefold(),
            )
            if any(identity):
                identities.setdefault(str(number).strip(), set()).add(identity)
        return sorted(number for number, values in identities.items() if len(values) > 1)

    def add_landing_site_to_project(self, reference_site_id):
        collection = self.session.state.get("reference_landing_sites") or {}
        site = next(
            (item for item in collection.get("items") or [] if item.get("reference_site_id") == reference_site_id),
            None,
        )
        if not site:
            raise ValueError("参考起降点不存在")
        if site.get("quality") == "invalid" or not site.get("coordinate"):
            raise ValueError("参考起降点坐标无效，不能加入项目")
        return self.route_service.add_reference_site(site)
