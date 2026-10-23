"""Native pixel influence intervals for the complete original polyline.

All touched rectangles contribute their source values. Analytic intersections
with rectangle + disk include boundary tangencies, without sampled gaps or
nearest-centre clipping. Overlap is intentional and conservative. No resampling,
NoData filling or source modification occurs. Version v2 fixes sampled omissions.
"""

from __future__ import annotations

from math import hypot

TOLERANCE = 1e-9

#: How a pixel interval is derived (recorded verbatim in the domain evidence).
INTERVAL_SEMANTICS = "native_pixel_exact_segment_rounded_rectangle_intersection_v2"

#: Forbidden terrain sampling methods: the native validator never resamples.
FORBIDDEN_TERRAIN_METHODS = ("bilinear", "average", "mean", "interpolate_nodata", "zero_fill")


def _rectangle_interval(a, b, bbox):
    low, high = 0.0, 1.0
    for value, delta, minimum, maximum in zip(a, (b[0]-a[0], b[1]-a[1]), bbox[:2], bbox[2:]):
        if abs(delta) <= 1e-15:
            if value < minimum or value > maximum:
                return None
        else:
            left, right = sorted(((minimum-value)/delta, (maximum-value)/delta))
            low, high = max(low, left), min(high, right)
            if low > high + TOLERANCE:
                return None
    return max(0.0, low), min(1.0, high)


def _rounded_rectangle_intervals(a, b, bbox, radius):
    """Analytic segment intersection with rectangle + disk; includes tangencies."""
    from math import sqrt
    w, s, e, n = bbox
    rectangles = [bbox] if radius == 0 else [
        (w-radius, s, e+radius, n), (w, s-radius, e, n+radius)]
    spans = [span for rect in rectangles if (span := _rectangle_interval(a,b,rect)) is not None]
    if radius > 0:
        dx, dy = b[0]-a[0], b[1]-a[1]
        aa = dx*dx+dy*dy
        for x,y in ((w,s),(w,n),(e,s),(e,n)):
            ox,oy = a[0]-x,a[1]-y
            bb=2*(ox*dx+oy*dy); cc=ox*ox+oy*oy-radius*radius
            discriminant=bb*bb-4*aa*cc
            if discriminant < 0:
                continue
            root=sqrt(discriminant)
            low=max(0.0,(-bb-root)/(2*aa)); high=min(1.0,(-bb+root)/(2*aa))
            if low<=high:
                spans.append((low,high))
    merged=[]
    for low,high in sorted(spans):
        if merged and low<=merged[-1][1]+TOLERANCE:
            merged[-1]=(merged[-1][0],max(high,merged[-1][1]))
        else:
            merged.append((low,high))
    return merged


def resolve_native_pixel_intervals(route, pixels, *, curve_chord_error_m, samples_per_pixel=9):
    """Exact piecewise-linear influence intervals, retaining every touched pixel.

    Radius uses analytic rectangle/disk intersections, not an inscribed polygon
    buffer or sampled nearest-centre clipping. Intervals may overlap; every pixel
    retains its original elevation/NoData. samples_per_pixel is API compatibility.
    """
    from math import isfinite
    points=((route or {}).get('horizontal_geometry') or {}).get('linearized',{}).get('linestring_metric') or []
    if len(points)<2 or not pixels:
        return []
    radius=float(curve_chord_error_m)
    if not isfinite(radius) or radius<0:
        raise ValueError('curve_chord_error_m must be finite and nonnegative')
    result=[]; cumulative=0.0
    for segment_index,(a,b) in enumerate(zip(points,points[1:]),1):
        length=hypot(b[0]-a[0],b[1]-a[1])
        if length<=TOLERANCE:
            continue
        for pixel in pixels:
            bbox=pixel.get('bbox_metric')
            if not bbox:
                continue
            w,s,e,n=map(float,bbox)
            if e+radius<min(a[0],b[0]) or w-radius>max(a[0],b[0]) or n+radius<min(a[1],b[1]) or s-radius>max(a[1],b[1]):
                continue
            for low,high in _rounded_rectangle_intervals(a,b,(w,s,e,n),radius):
                result.append({**pixel,'segment_index':segment_index,'interval':{
                    'start_distance_m':cumulative+low*length,
                    'end_distance_m':cumulative+high*length,'semantics':INTERVAL_SEMANTICS}})
        cumulative+=length
    result.sort(key=lambda item:(item['interval']['start_distance_m'],str(item.get('pixel'))))
    return result


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
