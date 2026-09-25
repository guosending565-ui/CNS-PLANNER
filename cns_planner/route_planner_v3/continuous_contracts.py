"""Compatibility alias for the former V3-C contract import path."""

import sys

from ..validation import continuous_contracts as _neutral

# A real module alias (rather than copied globals) keeps monkeypatching and version
# fingerprint tests pointed at the one production-neutral implementation.
sys.modules[__name__] = _neutral
