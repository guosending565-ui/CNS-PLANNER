"""Convert loaded QGIS layers into standard WGS84 constraint inputs."""

import math

from qgis.core import QgsCoordinateTransform


HARD_CONSTRAINT_WORDS = ("管制", "禁飞", "限制区", "硬约束", "no-fly", "restricted")


def layer_extents(layers, project, target_crs):
    result = {}
    for layer in layers:
        extent = QgsCoordinateTransform(layer.crs(), target_crs, project).transformBoundingBox(layer.extent())
        bbox = [extent.xMinimum(), extent.yMinimum(), extent.xMaximum(), extent.yMaximum()]
        if all(math.isfinite(value) for value in bbox) and not extent.isEmpty():
            result[layer.id()] = bbox
    return result


def hard_constraints(layers, extents_wgs84):
    return [{"layer_id": layer.id(), "name": layer.name(), "bbox": extents_wgs84[layer.id()]}
            for layer in layers
            if layer.id() in extents_wgs84 and any(word in layer.name().lower() for word in HARD_CONSTRAINT_WORDS)]
