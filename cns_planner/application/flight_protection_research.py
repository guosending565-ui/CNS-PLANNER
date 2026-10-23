"""Read-only projection of signed R7-D2 research artifacts. No planning/state writes."""
import hashlib
import json
import math
from pathlib import Path


def flight_protection_research(geoid, *, root=None):
    root = Path(root or Path(__file__).resolve().parents[2])
    folder = root / 'research/r7d2_protection'
    base = dict(evidence_level='engineering_research_assumption',
                formal_admission_evaluated=False, overall_status='unknown',
                route_id='R0005', default_visible=False)
    try:
        manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
        names = ['volume_geometry.json', 'protection_volume.geojson', 'result.json', 'obstacles.json']
        data = {}
        hashes = {}
        for name in names:
            raw = (folder / name).read_bytes()
            sha = hashlib.sha256(raw).hexdigest()
            if manifest['research\\r7d2_protection\\' + name] != sha:
                raise ValueError('artifact_identity_mismatch:' + name)
            data[name] = json.loads(raw)
            hashes[name] = sha
        volume, result = data['volume_geometry.json'], data['result.json']
        path = result['subject']['path']
        footprint = data['protection_volume.geojson']['geometry']
        if footprint != volume['footprint'] or len(path) != 14:
            raise ValueError('geometry_identity_mismatch')
        if volume['floor_m'] != 80 or volume['ceiling_m'] != 120:
            raise ValueError('vertical_extent_mismatch')
        base.update(input_fingerprint=result['fingerprint'], source_identity=result['source_identity'],
                    artifact_sha256=hashes, geometry_sha256=result['subject']['geometry_sha256'],
                    footprint=footprint, path=path, counts_by_type=result['counts_by_type'])
        if geoid is None:
            raise ValueError('egm2008_grid_missing')
        def position(point, height):
            h = geoid.orthometric_to_ellipsoidal(height, point[0], point[1])
            if not math.isfinite(h):
                raise ValueError('egm2008_conversion_nonfinite')
            return [point[0], point[1], h]
        polygons = [footprint['coordinates']] if footprint['type'] == 'Polygon' else footprint['coordinates']
        converted = []
        for polygon in polygons:
            converted.append({'lower': [[position(p, 80) for p in ring] for ring in polygon],
                              'upper': [[position(p, 120) for p in ring] for ring in polygon]})
        rows = data['obstacles.json']
        minimum = min((r for r in rows if r['obstacle_type'] == 'terrain'
                       and r.get('protection_floor_clearance_m') is not None),
                      key=lambda r: r['protection_floor_clearance_m'])
        unknown = next(r for r in rows if r['obstacle_id'] == '4tCa7')
        markers = []
        for row in [minimum, unknown]:
            row = dict(row)
            p = [row['longitude'], row['latitude']]
            row['top_position'] = position(p, row['top_egm2008_m'])
            row['floor_position'] = position(p, 80)
            markers.append(row)
        return dict(base, status='available', reason=None, polygons=converted,
                    centerline=[position(p, 100) for p in path], markers=markers,
                    vertical_conversion='h=H+N; canonical EGM2008 per vertex',
                    geoid_identity=geoid.describe())
    except Exception as exc:
        return dict(base, status='blocked', reason=str(exc), polygons=[], centerline=[], markers=[])
