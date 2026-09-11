"""C/N/S device catalog with a behavior-preserving CoveragePlannerV1 adapter."""

from copy import deepcopy
from pathlib import Path

from ..domain.cns_inputs import normalize_device
from .base import load_catalog


class DeviceCatalog:
    filename = "device_catalog.json"

    @classmethod
    def load(cls, config_directory):
        return cls.load_path(Path(config_directory) / cls.filename)

    @staticmethod
    def load_path(path):
        return load_catalog(path, normalize_device, "cns-device-catalog")

    @staticmethod
    def from_defaults(library):
        items = [normalize_device({**item, "source": item.get("source") or (library or {}).get("source")}) for item in (library or {}).get("items", [])]
        return {"status": "passed" if items else "missing_data", "catalog_id": "cns-device-catalog", "source": (library or {}).get("source") or "defaults.json", "metadata": {"fallback": True}, "count": len(items), "items": items}

    @staticmethod
    def to_coverage_v1(catalog, active_devices=None):
        """Return the established V1 input shape without catalog-only metadata."""
        if active_devices is not None:
            return deepcopy(active_devices)
        return [
            {
                "device_id": item["device_id"], "subsystem": item["subsystem"],
                "name": item["name"], "role": item["role"],
                "radius_m": item["radius_m"], "mtbf_h": item["mtbf_h"],
                "enabled": item["enabled"],
            }
            for item in catalog.get("items", [])
        ]
