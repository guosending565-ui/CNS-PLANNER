"""QGIS vector-layer adapter for workspace grid intersections."""

from __future__ import annotations

from hashlib import sha256
import json

from ..data.mapping.airspace_eligibility import AirspaceEligibilityService
from ..domain.airspace import normalize_airspace_feature


USEFUL_FIELDS = {
    "name", "名称", "category", "类别", "type", "类型", "class", "kind",
    "空域类型", "性质", "限制类型", "lower", "upper", "lower_altitude", "upper_altitude", "floor", "ceiling",
    "下限", "上限", "高度下限", "高度上限",
    "vertical_reference", "垂直基准", "valid_time", "有效时间", "生效时间",
}
CATEGORY_FIELDS = ("category", "类别", "class", "空域类型", "性质")
TYPE_FIELDS = ("type", "类型", "kind", "限制类型")
LOWER_FIELDS = ("lower_altitude", "lower", "floor", "下限", "高度下限")
UPPER_FIELDS = ("upper_altitude", "upper", "ceiling", "上限", "高度上限")
VERTICAL_FIELDS = ("vertical_reference", "垂直基准")
TIME_FIELDS = ("valid_time", "有效时间", "生效时间")


class QgisAirspaceAdapter:
    def __init__(self, layers, project, source_path):
        from qgis.core import (
            QgsCoordinateReferenceSystem,
            QgsCoordinateTransform,
            QgsFeatureRequest,
            QgsGeometry,
            QgsRectangle,
            QgsSpatialIndex,
        )

        self.layers = [layer for layer in layers if callable(getattr(layer, "getFeatures", None))]
        self.project = project
        self.source_path = str(source_path or "")
        self.QgsCoordinateReferenceSystem = QgsCoordinateReferenceSystem
        self.QgsCoordinateTransform = QgsCoordinateTransform
        self.QgsFeatureRequest = QgsFeatureRequest
        self.QgsGeometry = QgsGeometry
        self.QgsRectangle = QgsRectangle
        self.QgsSpatialIndex = QgsSpatialIndex

    def describe(self):
        return {
            "path": self.source_path,
            "layers": [
                {
                    "layer_id": layer.id(),
                    "name": layer.name(),
                    "crs": layer.crs().authid() or layer.crs().description(),
                }
                for layer in self.layers
            ],
        }

    def intersections(self, cells, workspace_bbox):
        mapped = {cell["grid_id"]: {"airspaces": []} for cell in cells}
        wgs84 = self.QgsCoordinateReferenceSystem("EPSG:4326")
        workspace = self.QgsRectangle(*workspace_bbox)
        for layer in self.layers:
            to_layer = self.QgsCoordinateTransform(wgs84, layer.crs(), self.project)
            workspace_in_layer = to_layer.transformBoundingBox(workspace)
            request = self.QgsFeatureRequest().setFilterRect(workspace_in_layer)
            features = list(layer.getFeatures(request))
            if not features:
                continue

            feature_by_id = {feature.id(): feature for feature in features}

            try:
                index = self.QgsSpatialIndex()
            except TypeError:  # lightweight test bindings and some QGIS overloads
                index = self.QgsSpatialIndex(features)
            else:
                for feature in features:
                    index.addFeature(feature)
            field_names = [field.name() for field in layer.fields()]
            useful_names = [name for name in field_names if name.lower() in {item.lower() for item in USEFUL_FIELDS}]
            for cell in cells:
                cell_geometry = self.QgsGeometry.fromRect(self.QgsRectangle(*cell["bbox"]))
                cell_geometry.transform(to_layer)
                cell_area = cell_geometry.area()
                for feature_id in index.intersects(cell_geometry.boundingBox()):
                    feature = feature_by_id.get(feature_id)
                    if feature is None:
                        continue
                    geometry = feature.geometry()
                    if geometry is None or geometry.isNull() or not geometry.intersects(cell_geometry):
                        continue
                    intersection = geometry.intersection(cell_geometry)
                    if intersection.isNull() or intersection.isEmpty():
                        continue
                    intersection_area = max(0.0, float(intersection.area()))
                    ratio = min(1.0, intersection_area / cell_area) if cell_area > 0 and intersection_area > 0 else None
                    attributes = {name: self._json_value(feature[name]) for name in useful_names if feature[name] not in (None, "")}
                    mapped[cell["grid_id"]]["airspaces"].append({
                        "layer_id": layer.id(),
                        "name": layer.name(),
                        "feature_id": self._stable_feature_id(layer, feature),
                        "source_feature_id": self._json_value(feature.id()),
                        "category": self._first(attributes, CATEGORY_FIELDS),
                        "type": self._first(attributes, TYPE_FIELDS),
                        "intersection_ratio": ratio,
                        "intersection_area": intersection_area,
                        "source_attributes": attributes,
                    })
        for value in mapped.values():
            ratios = [item["intersection_ratio"] for item in value["airspaces"] if item["intersection_ratio"] is not None]
            value["coverage_ratio"] = max(ratios) if ratios else None if value["airspaces"] else 0.0
        return mapped

    def build_eligibility(self, cells, workspace_bbox, policies=None):
        try:
            features = self.features(workspace_bbox)
        except (AttributeError, TypeError, ValueError, RuntimeError, json.JSONDecodeError):
            # Geometry facts are safety critical: unsupported extraction remains missing,
            # never a permissive fallback based on layer names or cell intersections.
            features = []
        return AirspaceEligibilityService().build(cells, features, policies)

    def features(self, workspace_bbox):
        """Return normalized polygon facts; route eligibility is deliberately absent."""
        wgs84 = self.QgsCoordinateReferenceSystem("EPSG:4326")
        workspace = self.QgsRectangle(*workspace_bbox)
        workspace_geometry = self.QgsGeometry.fromRect(workspace)
        facts = []
        for layer in self.layers:
            to_layer = self.QgsCoordinateTransform(wgs84, layer.crs(), self.project)
            to_wgs84 = self.QgsCoordinateTransform(layer.crs(), wgs84, self.project)
            request = self.QgsFeatureRequest().setFilterRect(to_layer.transformBoundingBox(workspace))
            field_names = [field.name() for field in layer.fields()]
            useful_names = [name for name in field_names if name.lower() in {item.lower() for item in USEFUL_FIELDS}]
            for feature in layer.getFeatures(request):
                source_geometry = feature.geometry()
                if source_geometry is None or source_geometry.isNull():
                    continue
                geometry = self.QgsGeometry(source_geometry)
                geometry.transform(to_wgs84)
                if not geometry.intersects(workspace_geometry):
                    continue
                clipped = geometry.intersection(workspace_geometry)
                if clipped.isNull() or clipped.isEmpty():
                    continue
                geojson = json.loads(clipped.asJson())
                if geojson.get("type") not in ("Polygon", "MultiPolygon"):
                    continue
                attributes = {
                    name: self._json_value(feature[name])
                    for name in useful_names if feature[name] not in (None, "")
                }
                feature_id = self._stable_feature_id(layer, feature)
                facts.append(normalize_airspace_feature({
                    "feature_id": feature_id,
                    "geometry": geojson,
                    "category": self._first(attributes, CATEGORY_FIELDS),
                    "type": self._first(attributes, TYPE_FIELDS),
                    "lower_altitude": self._first(attributes, LOWER_FIELDS),
                    "upper_altitude": self._first(attributes, UPPER_FIELDS),
                    "vertical_reference": self._first(attributes, VERTICAL_FIELDS),
                    "valid_time": self._first(attributes, TIME_FIELDS),
                    "crs": {
                        "source": layer.crs().authid() or layer.crs().description(),
                        "normalized_geometry": "EPSG:4326",
                    },
                    "source": {
                        "file": self.source_path,
                        "layer_id": layer.id(),
                        "layer_name": layer.name(),
                        "source_feature_id": self._json_value(feature.id()),
                    },
                    "provenance": {"source_attributes": attributes},
                }))
        return sorted(facts, key=lambda item: item["feature_id"])

    def _stable_feature_id(self, layer, feature):
        value = {
            "source": self.source_path,
            "layer_id": layer.id(),
            "layer_name": layer.name(),
            "source_feature_id": self._json_value(feature.id()),
        }
        digest = sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return f"ASF-{digest[:16]}"

    @staticmethod
    def _first(attributes, names):
        lowered = {key.lower(): value for key, value in attributes.items()}
        return next((lowered[name.lower()] for name in names if name.lower() in lowered), None)

    @staticmethod
    def _json_value(value):
        return value if value is None or isinstance(value, (bool, int, float, str)) else str(value)
