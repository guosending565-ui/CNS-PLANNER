"""Read-only end-to-end check of the repaired building path through QGIS.

Builds a real corridor route for 桃花岛 -> 函景湾, runs the *production* corridor
building source, and reports the geometry quality it now returns.
"""
import sys

sys.path.insert(0, r"C:\Users\yiding\Documents\ChatGPT\CNS规划系统")

from qgis.core import QgsApplication  # noqa: E402

QgsApplication.setPrefixPath(r"C:\Program Files\QGIS 3.44.14\apps\qgis-ltr", True)
app = QgsApplication([], False)
app.initQgis()

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from cns_planner.domain.building_geometry_quality import (  # noqa: E402
    empty_geometry_quality_report,
)
from cns_planner.gis.fine_environment_adapter import (  # noqa: E402
    QgisMetricTransform, RouteCorridorBuildingSource,
)

BUILDINGS = r"D:/aaa2026project/UOM/舟山/规划系统/building/processed/zhoushan_buildings.gpkg"
CRS = "EPSG:32651"
transform = QgisMetricTransform(CRS)

# 桃花岛 RLS-FA13218F1A7F -> 函景湾 RLS-30E29238CD3D (real landing sites)
start = [122.2841666667, 29.8430555556]
end = [122.2763888889, 29.9452777778]
metric_line = [
    list(transform.to_metric(start)), list(transform.to_metric(end)),
]
route = {"horizontal_geometry": {"linearized": {
    "linestring_metric": metric_line, "curve_chord_error_m": 0.0,
}}}

source = RouteCorridorBuildingSource(BUILDINGS, crs_authority=CRS)
print("usable:", source.usable())
evidence = source.query_route(
    route, transform=transform, horizontal_clearance_m=0.0, curve_error_m=0.0,
)
buildings = evidence.get("buildings") or []
print("available:", evidence.get("available"), "buildings in corridor:", len(buildings))

empty_ring = 0
statuses = {}
repair_applied = 0
multipart = 0
ring_parts = 0
for building in buildings:
    if not building.get("ring_metric"):
        empty_ring += 1
    status = building.get("geometry_status")
    statuses[status] = statuses.get(status, 0) + 1
    if (building.get("geometry_quality") or {}).get("repair_applied"):
        repair_applied += 1
    if (building.get("geometry_quality") or {}).get("multi_part_result"):
        multipart += 1
    ring_parts += int(building.get("part_count") or 0)
print("empty ring_metric:", empty_ring)
print("geometry_status counts:", statuses)
print("repair_applied:", repair_applied, "multi_part_result:", multipart,
      "total valid parts:", ring_parts)
sample = buildings[0] if buildings else None
if sample:
    print("sample id:", sample["building_id"], "part_count:", sample["part_count"],
          "ring_vertices:", len(sample["ring_metric"]),
          "height_m:", sample["height_m"], "source:", sample["source"])
    print("sample geometry_quality:", {
        key: value for key, value in sample["geometry_quality"].items()
        if key != "part_records"
    })
print("production building source: OK" if empty_ring == 0 else "STILL BROKEN")

app.exitQgis()
