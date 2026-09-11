"""Compatibility imports for the unified data registry."""

try:
    from ..data.registry import DEFINITIONS, SourceDefinition, build_registry
except ImportError:  # Legacy direct-script import path.
    from data.registry import DEFINITIONS, SourceDefinition, build_registry

__all__ = ["DEFINITIONS", "SourceDefinition", "build_registry"]
