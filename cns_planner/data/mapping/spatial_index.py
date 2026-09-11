"""Small WGS84 bbox index and segment clipping helpers for grid attributes."""

from __future__ import annotations

import math


EARTH_RADIUS_M = 6_371_008.8


class GridBboxIndex:
    def __init__(self, cells):
        self.cells = list(cells)
        self.by_id = {cell["grid_id"]: cell for cell in self.cells}
        self.bounds = [
            min(cell["bbox"][0] for cell in self.cells),
            min(cell["bbox"][1] for cell in self.cells),
            max(cell["bbox"][2] for cell in self.cells),
            max(cell["bbox"][3] for cell in self.cells),
        ]
        self.size = min(64, max(1, math.ceil(math.sqrt(len(self.cells)))))
        self.width = (self.bounds[2] - self.bounds[0]) / self.size
        self.height = (self.bounds[3] - self.bounds[1]) / self.size
        self.buckets = {}
        for cell in self.cells:
            west, south, east, north = cell["bbox"]
            for x in range(self._index(west, 0), self._index(east, 0) + 1):
                for y in range(self._index(south, 1), self._index(north, 1) + 1):
                    self.buckets.setdefault((x, y), []).append(cell)

    def query(self, bbox):
        candidates = {}
        for x in range(self._index(bbox[0], 0), self._index(bbox[2], 0) + 1):
            for y in range(self._index(bbox[1], 1), self._index(bbox[3], 1) + 1):
                for cell in self.buckets.get((x, y), []):
                    candidates[cell["grid_id"]] = cell
        return list(candidates.values())

    def find_point(self, coordinate):
        lon, lat = coordinate
        for cell in self.query([lon, lat, lon, lat]):
            west, south, east, north = cell["bbox"]
            if west <= lon < east and south <= lat < north:
                return cell
        return None

    def _index(self, value, axis):
        start = self.bounds[axis]
        span = self.width if axis == 0 else self.height
        return max(0, min(self.size - 1, math.floor((value - start) / span)))


def segment_fraction_in_bbox(start, end, bbox):
    """Return the parametric segment fraction inside a half-open bbox."""
    dx, dy = end[0] - start[0], end[1] - start[1]
    if dx == 0 and dy == 0:
        return 1.0 if bbox[0] <= start[0] < bbox[2] and bbox[1] <= start[1] < bbox[3] else 0.0
    lower, upper = 0.0, 1.0
    for p, q in ((-dx, start[0] - bbox[0]), (dx, bbox[2] - start[0]),
                 (-dy, start[1] - bbox[1]), (dy, bbox[3] - start[1])):
        if p == 0:
            if q < 0:
                return 0.0
            continue
        ratio = q / p
        if p < 0:
            lower = max(lower, ratio)
        else:
            upper = min(upper, ratio)
        if lower > upper:
            return 0.0
    if upper <= lower:
        return 0.0
    midpoint = ((start[0] + dx * (lower + upper) / 2), (start[1] + dy * (lower + upper) / 2))
    if not (bbox[0] <= midpoint[0] < bbox[2] and bbox[1] <= midpoint[1] < bbox[3]):
        return 0.0
    return upper - lower


def bbox_area_km2(bbox):
    west, south, east, north = bbox
    width = math.radians(east - west) * EARTH_RADIUS_M * math.cos(math.radians((south + north) / 2))
    height = math.radians(north - south) * EARTH_RADIUS_M
    return abs(width * height) / 1_000_000
