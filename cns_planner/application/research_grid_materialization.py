"""Source-bound read-only L8 materialization, without state/session publication.

This verifies content, not OS isolation or execution permission. A caller-supplied
expected digest is a content contract and can never authorize a real evaluation.
Airspace reference layers are deliberately excluded from computational outputs.
"""
from copy import deepcopy
import gzip
from hashlib import sha256
import io
import json
import math

MAX_COMPRESSED_BYTES = 8 * 1024 * 1024
# Frozen rev503 attributes decode to 45,223,685 B (mostly excluded population).
MAX_DECODED_BYTES = 64 * 1024 * 1024


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _artifact(state, blobs, kind, logical_key):
    entries = state.get("artifact_manifest", {}).get("entries", {})
    matches = [e for e in entries.values() if e.get("artifact_type") == kind]
    if len(matches) != 1:
        raise ValueError("artifact missing or ambiguous: " + logical_key)
    e = matches[0]
    h = e.get("sha256")
    if (not isinstance(h, str) or len(h) != 64 or e.get("artifact_id") != h
            or e.get("status") != "published" or e.get("schema_version") != 1
            or e.get("content_encoding") != "json.gz"
            or e.get("relative_path") != ".cns-results/" + h + ".json.gz"):
        raise ValueError("artifact identity mismatch")
    raw = blobs.get(h)
    if (not isinstance(raw, bytes) or len(raw) > MAX_COMPRESSED_BYTES
            or len(raw) != e.get("size_bytes") or sha256(raw).hexdigest() != h):
        raise ValueError("artifact bytes missing or SHA mismatch")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
            decoded = stream.read(MAX_DECODED_BYTES + 1)
        if len(decoded) > MAX_DECODED_BYTES:
            raise ValueError("artifact decoded size exceeded")
        document = json.loads(decoded)
        payload = document["payload"]
        if document.get("artifact_schema_version") != 1 or payload.get("logical_key") != logical_key:
            raise ValueError("artifact schema or logical scope mismatch")
        if not isinstance(payload.get("parts"), dict):
            raise ValueError("artifact parts missing")
    except (OSError, EOFError, KeyError, TypeError) as exc:
        raise ValueError("invalid artifact") from exc
    return payload["parts"], dict(artifact_type=kind, sha256=h, size_bytes=len(raw),
        decoded_sha256=sha256(decoded).hexdigest(), relative_path=e["relative_path"])


def materialize_grid(state_bytes, artifacts, *, expected_state_sha256, route_id="R0005"):
    """Return copied P14 grid/terrain/building facts and a stable manifest.

    Artifacts is {compressed_sha256: bytes}. No file I/O, grid generation,
    interpolation, CRS conversion, or state writes occur. All workspace cells
    are retained in source order: P14 performs its own corridor intersection.
    A declared CRS remains declared. NoData and absent building heights remain
    as supplied; successful association does not mean all evidence is known.
    """
    if not isinstance(state_bytes, bytes) or sha256(state_bytes).hexdigest() != expected_state_sha256:
        raise ValueError("state SHA mismatch")
    state = json.loads(state_bytes)
    routes = [v for v in state.get("operational_routes", []) if v.get("route_id") == route_id]
    if len(routes) != 1 or routes[0].get("path_crs") != "OGC:CRS84":
        raise ValueError("route CRS or scope missing")
    grid = deepcopy(state.get("grid", {}))
    attrs = state.get("grid_attributes", {})
    if (grid.get("level") != 8 or grid.get("canonical_level") != 8
            or state.get("spatial_3d", {}).get("canonical_vertical_reference") != "egm2008_orthometric"
            or attrs.get("terrain", {}).get("vertical_reference") != "egm2008_orthometric"):
        raise ValueError("L8 or EGM2008 identity missing")
    gp, gs = _artifact(state, artifacts, "workspace.grid.cells", "grid")
    ap, ats = _artifact(state, artifacts, "grid_attribute.cells", "grid_attributes")
    cells = gp.get("cells")
    if not isinstance(cells, list) or not cells or len(cells) != grid.get("count"):
        raise ValueError("grid count mismatch")
    ids = []
    for c in cells:
        identifier = c.get("grid_id")
        bbox, center = c.get("bbox"), c.get("center")
        if not isinstance(identifier, str) or not identifier.startswith("MHT4063-L08-") or c.get("level") != 8:
            raise ValueError("grid ID or level mismatch")
        for value, size in [(bbox, 4), (center, 2)]:
            if (not isinstance(value, list) or len(value) != size or
                    any(type(n) not in (int, float) or not math.isfinite(n) for n in value)):
                raise ValueError("grid geometry missing")
        if not (-180 <= bbox[0] < bbox[2] <= 180 and -90 <= bbox[1] < bbox[3] <= 90
                and bbox[0] <= center[0] <= bbox[2] and bbox[1] <= center[1] <= bbox[3]):
            raise ValueError("grid geometry invalid")
        ids.append(identifier)
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate grid ID")
    output = {}
    for name in ("terrain", "buildings"):
        detail = ap.get(name + ".cells")
        if not isinstance(detail, dict) or set(detail) != set(ids):
            raise ValueError("grid attribute association missing: " + name)
        if any(not isinstance(v, dict) for v in detail.values()):
            raise ValueError("grid attribute record missing")
        if name == "terrain":
            for v in detail.values():
                if "status" not in v:
                    raise ValueError("terrain status missing")
                if v["status"] == "passed":
                    n = v.get("surface_elevation_mean_m")
                    if type(n) not in (int, float) or not math.isfinite(n):
                        raise ValueError("passed terrain elevation missing")
        output[name] = deepcopy(attrs.get(name, {}))
        output[name]["cells"] = deepcopy(detail)
    grid["cells"] = deepcopy(cells)
    manifest = dict(schema="research_grid_input_manifest.v1", source_state_sha256=expected_state_sha256,
        source_revision=state.get("revision"), route_id=route_id, level=8,
        cell_count=len(ids), ordered_grid_ids_sha256=_digest(ids), sources=[gs, ats],
        crs="OGC:CRS84", crs_evidence_status="declared", vertical_reference="egm2008_orthometric",
        execution_authorized=False, scope="source_workspace_for_P14_corridor_intersection",
        content_fingerprint=_digest(dict(grid=grid, grid_attributes=output)))
    return dict(grid=grid, grid_attributes=output, manifest=manifest)
