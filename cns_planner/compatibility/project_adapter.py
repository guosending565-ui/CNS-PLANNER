"""Read-only projections for legacy ProjectState fields."""

from __future__ import annotations

from copy import deepcopy

from .catalog import capability_metadata


def empty_compatibility_view(result_key, capability_id):
    return {
        "status": "not_calculated",
        "result_key": str(result_key),
        "source": "empty_compatibility_view",
        "existing": False,
        **capability_metadata(capability_id),
    }


def read_existing_legacy(state, result_key, capability_id):
    """Read an old value verbatim into a detached non-authoritative view.

    A missing key produces an empty view without ``setdefault`` or any other mutation.
    Empty legacy values are not interpreted as confirmed facts.
    """

    if result_key not in state or state.get(result_key) is None:
        return empty_compatibility_view(result_key, capability_id)
    value = deepcopy(state[result_key])
    result = value if isinstance(value, dict) else {"value": value}
    result.update({
        "result_key": str(result_key),
        "source": "existing_legacy_project",
        "existing": True,
        **capability_metadata(capability_id),
    })
    return result
