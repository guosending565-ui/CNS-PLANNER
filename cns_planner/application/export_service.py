"""Read-only project, route and site exports."""

import json


class ExportService:
    def __init__(self, session, snapshot):
        self.session, self.snapshot = session, snapshot

    def project(self):
        return json.dumps(self.snapshot(), ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")

    def routes(self):
        features = [{
            "type": "Feature",
            "properties": {key: route.get(key) for key in ("route_id", "status", "reason", "algorithm_id", "algorithm_version")},
            "geometry": {"type": "LineString", "coordinates": route.get("path", [])},
        } for route in self.session.state["operational_routes"]]
        return self._collection(features)

    def sites(self):
        features = []
        for subsystem, layer in (self.session.state.get("coverage") or {}).get("layers", {}).items():
            for station in layer.get("stations", []):
                properties = {key: value for key, value in station.items() if key != "coordinate"}
                properties["subsystem"] = subsystem
                features.append({"type": "Feature", "properties": properties, "geometry": {"type": "Point", "coordinates": station["coordinate"]}})
        return self._collection(features)

    @staticmethod
    def _collection(features):
        return json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False, indent=2).encode("utf-8")
