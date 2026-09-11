"""Compatibility imports for unified data-source health."""

try:
    from ..data.health import LABELS, build_health
except ImportError:  # Legacy direct-script import path.
    from data.health import LABELS, build_health

__all__ = ["LABELS", "build_health"]
