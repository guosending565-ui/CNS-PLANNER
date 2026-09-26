"""Compatibility/research boundary for frozen legacy capabilities."""

from .catalog import capability_catalog, capability_metadata
from .project_adapter import empty_compatibility_view, read_existing_legacy
from .selection import CompatibilitySelectionAdapter

__all__ = [
    "CompatibilitySelectionAdapter",
    "capability_catalog",
    "capability_metadata",
    "empty_compatibility_view",
    "read_existing_legacy",
]
