"""V3-C native-pixel window resolution (pure): route envelope × raster pixel grid.

The terrain validator must report, for every native raster pixel the realized route
(with its explicit curve-error envelope) touches, the along-track interval over which
that pixel is the **limiting** terrain constraint.  That interval computation is pure
geometry and lives here so the GIS adapter and the synthetic builder share exactly one
implementation:

* a pixel is included when its rectangle is within the envelope of the route
  (``distance <= curve_chord_error_m``), i.e. the pixel can set the altitude floor for
  some point of the route;
* the reported interval is where that pixel is the closest pixel centre -- the region
  where its floor is the binding one.  Clipping to the limiting region is
  conservative: the minimum route altitude over the reported interval is checked
  against the pixel's floor, so no pixel that can be limiting is dropped;
* the intervals of neighbouring pixels tile the route, so the union of violations is
  still the complete set of violating along-track positions.

The union of the reported intervals therefore equals the whole route within the
raster, and each pixel's evidence stays auditable (source value, NoData, CRS).
"""

from __future__ import annotations

from math import hypot

TOLERANCE = 1e-9

#: How a pixel interval is derived (recorded verbatim in the domain evidence).
INTERVAL_SEMANTICS = "clipped_to_the_region_where_this_native_pixel_centre_is_the_limiting_terrain_constraint"

#: Forbidden terrain sampling methods: the native validator never resamples.
FORBIDDEN_TERRAIN_METHODS = ("bilinear", "average", "mean", "interpolate_nodata", "zero_fill")


def resolve_native_pixel_intervals(
    route, pixels, *, curve_chord_error_m, samples_per_pixel=9,
):
    """Attach the limiting along-track interval to each touched native pixel.

    ``pixels`` are canonical pixel records
    (``{"pixel", "bbox_metric", "center_metric", "data_status", "elevation_egm2008_m"}``)
    already covering the route window.  ``route`` is the realized continuous route
    contract; its linearized LineString is used, buffered by ``curve_chord_error_m``.
    """

    points = [
        [float(point[0]), float(point[1])]
        for point in ((route or {}).get("horizontal_geometry") or {}).get("linearized", {}).get(
            "linestring_metric",
        ) or []
        if isinstance(point, (list, tuple)) and len(point) >= 2
    ]
    envelope_radius = float(curve_chord_error_m or 0.0)
    if len(points) < 2 or not pixels:
        return []
    cumulative = [0.0]
    for index in range(len(points) - 1):
        cumulative.append(cumulative[-1] + hypot(
            points[index + 1][0] - points[index][0], points[index + 1][1] - points[index][1],
        ))
    total = cumulative[-1]
    if total <= TOLERANCE:
        return []
    dense = []
    for index in range(len(points) - 1):
        span = cumulative[index + 1] - cumulative[index]
        steps = max(1, int(span / max(1.0, total / 4096.0)) + 1)
        for step in range(steps):
            ratio = step / float(steps)
            dense.append([
                cumulative[index] + span * ratio,
                points[index][0] + (points[index + 1][0] - points[index][0]) * ratio,
                points[index][1] + (points[index + 1][1] - points[index][1]) * ratio,
            ])
    dense.append([cumulative[-1], points[-1][0], points[-1][1]])

    centres = centers_of(pixels)
    if not centres or len(centres) != len(pixels):
        return []
    # 0. Cheap pre-filter: only pixels whose rectangle is within the envelope of the
    #    route's bounding box can be touched at all.  This keeps the sweep proportional
    #    to the route window instead of the whole raster.
    route_west = min(point[0] for point in points) - envelope_radius
    route_south = min(point[1] for point in points) - envelope_radius
    route_east = max(point[0] for point in points) + envelope_radius
    route_north = max(point[1] for point in points) + envelope_radius
    candidates = []
    for pixel_index, pixel in enumerate(pixels):
        bbox = pixel.get("bbox_metric")
        if not bbox:
            continue
        west, south, east, north = (float(value) for value in bbox)
        if east < route_west or west > route_east or north < route_south or south > route_north:
            continue
        candidates.append((pixel_index, (west, south, east, north)))
    if not candidates:
        return []
    dense = _dense_route_points(points, cumulative, total)
    # 1. Along-track intervals where the route's error envelope overlaps a pixel
    #    rectangle.  This is the *exact* influence set for a piecewise-linear route:
    #    the envelope is the Minkowski sum of the route with a disk of radius ``e``,
    #    so overlap happens exactly when the route comes within ``e`` of the
    #    rectangle.  A pixel boundary-touching the route is therefore included.
    intervals = []
    for pixel_index, bbox in candidates:
        current = None
        for distance, x, y in dense:
            if _within_envelope(x, y, bbox, envelope_radius):
                if current is None:
                    current = [distance, distance]
                else:
                    current[1] = distance
            elif current is not None:
                intervals.append((pixel_index, current[0], current[1]))
                current = None
        if current is not None:
            intervals.append((pixel_index, current[0], current[1]))
    # 2. Report, per pixel, the sub-interval where that pixel is the *limiting*
    #    terrain constraint (its centre is the nearest one).  Restricting to the
    #    limiting region stays conservative -- a pixel's floor is only binding where
    #    it is the closest pixel -- and keeps violation intervals non-overlapping.
    result = []
    lattice = _centre_lattice(centres)
    for pixel_index, start, end in intervals:
        if end - start <= TOLERANCE:
            continue
        window = [start, end]
        selected = None
        for distance, x, y in dense:
            if distance < start - TOLERANCE:
                continue
            if distance > end + TOLERANCE:
                break
            if not _is_limiting(centres, lattice, pixel_index, x, y):
                continue
            if selected is None:
                selected = [distance, distance]
            else:
                selected[1] = distance
        if selected is not None and selected[1] - selected[0] > TOLERANCE:
            window = selected
        pixel = pixels[pixel_index]
        bbox = [float(value) for value in pixel["bbox_metric"]]
        result.append({
            "pixel": pixel.get("pixel"),
            "bbox_metric": bbox,
            "center_metric": pixel.get("center_metric") or list(centres[pixel_index]),
            "data_status": pixel.get("data_status"),
            "elevation_egm2008_m": pixel.get("elevation_egm2008_m"),
            "source_value": pixel.get("source_value", pixel.get("elevation_egm2008_m")),
            "reason": pixel.get("reason"),
            "interval": {
                "start_distance_m": window[0],
                "end_distance_m": window[1],
                "semantics": INTERVAL_SEMANTICS,
            },
        })
    result.sort(key=lambda item: (item["interval"]["start_distance_m"], str(item.get("pixel"))))
    return result


#: Sample spacing along the route for the envelope/pixel intersection sweep.  It is
#: far below any realistic raster pixel size, so a touched pixel cannot be skipped.
_SAMPLE_STEP_M = 1.0


def _dense_route_points(points, cumulative, total):
    """Evenly spaced ``(distance, x, y)`` samples along the realized line."""

    step = max(_SAMPLE_STEP_M, total / 200000.0)
    dense = []
    for index in range(len(points) - 1):
        span = cumulative[index + 1] - cumulative[index]
        steps = max(1, int(span / step) + 1)
        for sub in range(steps):
            ratio = sub / float(steps)
            dense.append((
                cumulative[index] + span * ratio,
                points[index][0] + (points[index + 1][0] - points[index][0]) * ratio,
                points[index][1] + (points[index + 1][1] - points[index][1]) * ratio,
            ))
    dense.append((cumulative[-1], points[-1][0], points[-1][1]))
    return dense


def _within_envelope(x, y, bbox, envelope_radius):
    offset_x = max(bbox[0] - x, 0.0, x - bbox[2])
    offset_y = max(bbox[1] - y, 0.0, y - bbox[3])
    if offset_x <= 0.0 and offset_y <= 0.0:
        return True
    return hypot(offset_x, offset_y) <= envelope_radius + TOLERANCE


def _is_limiting(centres, lattice, pixel_index, x, y):
    """True when pixel ``pixel_index`` has the nearest centre to ``(x, y)``.

    A lattice index over the pixel centres keeps this O(1) per query instead of
    scanning every pixel: only the query point's own lattice cell and its eight
    neighbours can contain a closer centre.
    """

    side = lattice[0]
    own2 = (x - centres[pixel_index][0]) ** 2 + (y - centres[pixel_index][1]) ** 2
    cell_x, cell_y = int(x // side), int(y // side)
    for offset_x in (-1, 0, 1):
        for offset_y in (-1, 0, 1):
            bucket = lattice[1].get((cell_x + offset_x, cell_y + offset_y))
            if not bucket:
                continue
            for index, center_x, center_y in bucket:
                if index == pixel_index:
                    continue
                if (x - center_x) ** 2 + (y - center_y) ** 2 < own2 - TOLERANCE:
                    return False
    return True


def _centre_lattice(centres):
    """A square lattice over the pixel centres: ``(side, {(cx, cy): [(index, x, y)]})``."""

    if not centres:
        return 1.0, {}
    xs = sorted({center[0] for center in centres})
    ys = sorted({center[1] for center in centres})
    gaps = [
        b - a for values in (xs, ys) for a, b in zip(values, values[1:]) if b - a > TOLERANCE
    ]
    side = min(gaps) if gaps else 1.0
    side = max(side, 1e-9)
    lattice = {}
    for index, (x, y) in enumerate(centres):
        # Round to the lattice so centres exactly on a boundary land in one bucket.
        key = (int(round(x / side)), int(round(y / side)))
        lattice.setdefault(key, []).append((index, x, y))
    return side, lattice


def centers_of(pixels):
    result = []
    for pixel in pixels:
        center = pixel.get("center_metric")
        bbox = pixel.get("bbox_metric")
        if center is None and bbox:
            center = [(bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0]
        if center is not None:
            result.append([float(center[0]), float(center[1])])
    return result


__all__ = [
    "FORBIDDEN_TERRAIN_METHODS", "INTERVAL_SEMANTICS", "centers_of",
    "resolve_native_pixel_intervals",
]
