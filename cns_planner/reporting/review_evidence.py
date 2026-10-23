"""Read-only presentation projection; complete evidence remains content addressed."""
from copy import deepcopy

# Explicit detail fields only: never remove status, unknown, gates or disclosures.
DETAIL_KEYS = frozenset(("voxels", "samples", "cells", "confirmed_target_voxel_ids",
    "deficit_voxel_ids", "unknown_voxel_ids", "residual_target_voxel_ids",
    "target_voxel_ids", "satisfied_voxel_ids", "continuous_deficit_segments", "corridor_geometry"))

def review_summary(value):
    if isinstance(value, dict):
        result = {}
        counts = {}
        for key, item in value.items():
            if key in DETAIL_KEYS and isinstance(item, (list, dict)):
                counts[key] = len(item)
            else:
                result[key] = review_summary(item)
        if counts:
            result["evidence_detail_counts"] = counts
        return result
    if isinstance(value, list):
        return [review_summary(item) for item in value]
    return deepcopy(value)
