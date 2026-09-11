"""Aircraft capability catalog; capabilities are never treated as requirements."""

from copy import deepcopy
from pathlib import Path

from ..domain.cns_inputs import normalize_aircraft_profile
from .base import load_catalog


class AircraftCNSProfileCatalog:
    filename = "aircraft_profiles.json"

    @classmethod
    def load(cls, config_directory):
        return cls.load_path(Path(config_directory) / cls.filename)

    @staticmethod
    def load_path(path):
        return load_catalog(path, normalize_aircraft_profile, "aircraft-cns-profile-catalog")

    @staticmethod
    def from_defaults(library):
        items = []
        for index, raw in enumerate((library or {}).get("items", [])):
            item = {**raw, "aircraft_id": raw.get("aircraft_id") or f"AIRCRAFT-DEFAULT-{index + 1:03d}", "name": raw.get("name") or raw.get("model"), "source": raw.get("source") or (library or {}).get("source")}
            items.append(normalize_aircraft_profile(item))
        return {"status": "passed" if items else "missing_data", "catalog_id": "aircraft-cns-profile-catalog", "source": (library or {}).get("source") or "defaults.json", "metadata": {"fallback": True}, "count": len(items), "items": items}

    @staticmethod
    def find(catalog, aircraft_id):
        return next((deepcopy(item) for item in catalog.get("items", []) if item.get("aircraft_id") == aircraft_id), None)
