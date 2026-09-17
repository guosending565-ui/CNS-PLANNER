"""GIS/GDAL boundary for V3-B corridor-local fine environment acquisition.

This module is the **only** place where V3-B touches real data.  The algorithm
package (``cns_planner/route_planner_v3``) keeps its hard boundary: it never
imports QGIS/GDAL and never reads a file, it only consumes the canonical
``FineCellEnvironment`` this adapter returns.

What the adapter guarantees:

* the fine grid is built **only inside the corridor support cells' metric
  bounding box** -- never a full-workspace or full-raster resample;
* the horizontal resolution comes from an **explicit configuration** or from the
  DTM's own effective resolution.  There is no ``30 m`` constant anywhere: a
  resolution that cannot be traced blocks the run;
* the local CRS / metric transform / effective resolution are recorded;
* terrain hard facts use the **maximum** EGM2008 elevation of the valid FABDEM
  pixels intersecting each fine cell plus the explicit ``terrain_clearance_m``.
  NoData means ``unknown``, which is blocked.  The raster is read through a single
  read-only window per corridor (``ReadAsArray``), not resampled and not modified;
* building hard facts come from the existing GeoPackage through the provider's
  own spatial index (RTree) via ``QgsFeatureRequest.setFilterRect`` -- only
  corridor buildings are queried.  The footprint is buffered by the explicit
  horizontal clearance and mapped to fine cells as a **conservative envelope**;
  the required floor is ``ground + height + vertical_clearance``.  A missing
  height or missing DTM means ``unknown``, which is blocked.  Source geometry is
  never modified -- the exact polygon clearance is V3-C;
* airspace consumes **confirmed** ``AirspacePolicy`` only (never a layer name or
  colour): a fine cell is feasible only when its parent cell is confirmed allowed
  *and* the fine cell centre is inside a confirmed allowed polygon, and it must not
  be inside a confirmed blocked polygon;
* population/traffic soft indices are **reused** from the existing RiskModel
  contributor ``normalized`` values; coarse values mapped onto finer cells are
  flagged ``upsampled_without_new_information=true``.

The QGIS/GDAL imports are lazy: importing this module never requires QGIS, and the
pure assembly path (with injected sources) is fully testable without it.
"""

from __future__ import annotations

from math import cos, floor, radians
from pathlib import Path

from ..data.mapping.airspace_eligibility import point_covered_by_polygon, polygons_of
from ..route_planner_v3.fine_contracts import AIRSPACE_MAPPING_METHOD, contract_fingerprint
from ..route_planner_v3.fine_grid import (
    assemble_fine_environment, bind_fine_cells_to_parents, build_fine_grid_spec,
    build_local_frame, building_facts_for_cells, fine_cells_from_spec,
    parent_soft_fields_from_risk_contributors, terrain_fact_from_pixels,
    upsample_soft_fields,
)

ADAPTER_ID = "v3b_fine_environment_adapter"
ADAPTER_VERSION = "3.1-alpha"

#: Terraform of the fine hard facts.  Recorded verbatim in the source audit.
TERRAIN_READ_MODE = "single_read_only_window_per_corridor_read_as_array_no_resample"
BUILDING_QUERY_MODE = "provider_spatial_index_rtree_filter_rect_then_metric_buffer"
AIRSPACE_QUERY_MODE = "confirmed_policy_coarse_cell_and_fine_centre_point_test"

FINE_SOURCE_ROLES = ("terrain_dtm", "buildings", "airspace_policy")

#: Reasons the adapter itself can report instead of building an environment.
ADAPTER_BLOCK_REASONS = (
    "fine_resolution_unresolved",
    "terrain_source_unavailable",
    "building_source_unavailable",
    "building_source_spatial_index_missing",
    "airspace_policy_unavailable",
    "metric_transform_unavailable",
    "corridor_support_cells_missing",
)


def _basename(value):
    return Path(value).name if value else None


# --------------------------------------------------------------------------- transform


class EquirectangularMetricTransform:
    """Explicit local equirectangular metric frame (not a geodetic CRS).

    Used for synthetic exercises and for tests.  It is labelled as a *declared
    local definition* so no consumer can mistake it for EPSG-grade geodesy; real
    data must go through :class:`QgisMetricTransform`.
    """

    def __init__(self, *, reference_point):
        self.longitude_0 = float(reference_point[0])
        self.latitude_0 = float(reference_point[1])
        self.metres_lat = 111132.0
        self.metres_lon = 111320.0 * max(0.05, cos(radians(self.latitude_0)))
        self.authority = "synthetic:local_equirectangular_m"

    def describe(self):
        return {
            "method": "declared_local_equirectangular_definition",
            "authority": self.authority,
            "reference_point": [self.longitude_0, self.latitude_0],
            "geodetic": False,
            "display_only": True,
        }

    def to_metric(self, point):
        return [
            (float(point[0]) - self.longitude_0) * self.metres_lon,
            (float(point[1]) - self.latitude_0) * self.metres_lat,
        ]

    def to_geographic(self, point):
        return [
            self.longitude_0 + float(point[0]) / self.metres_lon,
            self.latitude_0 + float(point[1]) / self.metres_lat,
        ]


class QgisMetricTransform:
    """Metric frame from an explicit projected CRS, using QGIS coordinate transforms."""

    def __init__(self, crs_authority, *, geographic_crs="EPSG:4326"):
        from qgis.core import (
            QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY, QgsProject,
        )

        self._QgsPointXY = QgsPointXY
        self.authority = str(crs_authority)
        self.metric = QgsCoordinateReferenceSystem(self.authority)
        if not self.metric.isValid():
            raise ValueError(f"无效的 local metric CRS：{self.authority}")
        self.geographic = QgsCoordinateReferenceSystem(geographic_crs)
        context = QgsProject.instance().transformContext()
        self.to_metric_transform = QgsCoordinateTransform(self.geographic, self.metric, context)
        self.to_geographic_transform = QgsCoordinateTransform(self.metric, self.geographic, context)

    def describe(self):
        return {
            "method": "qgis_projected_crs_coordinate_transform",
            "authority": self.authority,
            "geodetic": True,
            "display_only": False,
        }

    def to_metric(self, point):
        result = self.to_metric_transform.transform(self._QgsPointXY(float(point[0]), float(point[1])))
        return [result.x(), result.y()]

    def to_geographic(self, point):
        result = self.to_geographic_transform.transform(self._QgsPointXY(float(point[0]), float(point[1])))
        return [result.x(), result.y()]


# --------------------------------------------------------------------------- terrain


class FabdemWindowTerrainSource:
    """Read-only, windowed FABDEM DTM sampler (GDAL)."""

    role = "terrain_dtm"

    def __init__(self, path, *, gdal=None, osr=None):
        if gdal is None or osr is None:
            from osgeo import gdal as runtime_gdal, osr as runtime_osr
            gdal, osr = runtime_gdal, runtime_osr
        self.gdal, self.osr = gdal, osr
        self.path = Path(path).expanduser().resolve()
        if not self.path.is_file():
            raise ValueError("terrain_dtm 必须是存在的具体文件")
        self.dataset = gdal.Open(str(self.path), gdal.GA_ReadOnly)
        if self.dataset is None or self.dataset.RasterCount < 1:
            raise ValueError("无法以只读方式打开 FABDEM terrain_dtm")
        self.band = self.dataset.GetRasterBand(1)
        self.transform = self.dataset.GetGeoTransform()
        inverse = gdal.InvGeoTransform(self.transform)
        if isinstance(inverse, tuple) and len(inverse) == 2 and isinstance(inverse[0], (bool, int)):
            ok, inverse = inverse
            if not ok:
                raise ValueError("FABDEM 仿射变换不可逆")
        self.inverse = inverse
        self.nodata = self.band.GetNoDataValue()
        metadata = self.dataset.GetMetadata() or {}
        self.vertical_reference = str(metadata.get("vertical_reference") or "unknown")
        self.vertical_status = (
            "confirmed" if self.vertical_reference.lower() == "egm2008_orthometric" else "unresolved"
        )
        self.projection = self.dataset.GetProjection()
        self._to_raster = None

    # ------------------------------------------------------------------ protocol

    def describe(self):
        stat = self.path.stat()
        return {
            "role": self.role, "dataset": "FABDEM", "file_name": self.path.name,
            "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            "driver": self.dataset.GetDriver().ShortName,
            "width": int(self.dataset.RasterXSize), "height": int(self.dataset.RasterYSize),
            "pixel_size": [abs(self.transform[1]), abs(self.transform[5])],
            "nodata": self.nodata,
            "vertical_reference": self.vertical_reference,
            "vertical_status": self.vertical_status,
            "read_mode": TERRAIN_READ_MODE,
        }

    def effective_resolution_m(self):
        pixel_x = abs(float(self.transform[1]))
        pixel_y = abs(float(self.transform[5]))
        if pixel_x <= 0 or pixel_y <= 0:
            return None
        return max(pixel_x, pixel_y)

    def usable(self):
        if self.vertical_status != "confirmed":
            return False, "terrain_vertical_datum_unresolved"
        return True, None

    def sample_cells(self, cells, *, transform):
        """Per-cell max elevation from the pixels intersecting the cell.

        One read-only window covers the whole corridor; each fine cell slices the
        window it intersects (rectangular index window, no resampling).  A cell
        with no valid pixel becomes ``unknown``.
        """

        if self.vertical_status != "confirmed":
            return {
                str(cell["fine_cell_id"]): _unknown_terrain("terrain_vertical_datum_unresolved")
                for cell in cells
            }
        windows = {}
        for cell in cells:
            windows[str(cell["fine_cell_id"])] = self._pixel_window(cell["bbox_metric"], transform)
        usable = {key: value for key, value in windows.items() if value}
        if not usable:
            return {
                str(cell["fine_cell_id"]): _unknown_terrain("outside_dtm_coverage")
                for cell in cells
            }
        # Union window of the whole corridor, clamped to the raster once.
        union_x0 = min(window[0] for window in usable.values())
        union_y0 = min(window[1] for window in usable.values())
        union_x1 = max(window[0] + window[2] for window in usable.values())
        union_y1 = max(window[1] + window[3] for window in usable.values())
        x0 = max(0, union_x0)
        y0 = max(0, union_y0)
        x1 = min(int(self.dataset.RasterXSize), union_x1)
        y1 = min(int(self.dataset.RasterYSize), union_y1)
        self.last_window = {
            "window_pixels": [x0, y0, max(0, x1 - x0), max(0, y1 - y0)],
            "corridor_union_pixels": [union_x0, union_y0, union_x1 - union_x0, union_y1 - union_y0],
            "raster_pixels": int(self.dataset.RasterXSize) * int(self.dataset.RasterYSize),
            "read_fraction": round(
                (max(0, x1 - x0) * max(0, y1 - y0))
                / max(1, int(self.dataset.RasterXSize) * int(self.dataset.RasterYSize)), 9,
            ),
            "read_mode": TERRAIN_READ_MODE,
            "resampled": False,
            "windowed_read_only": True,
        }
        if x1 <= x0 or y1 <= y0:
            return {
                str(cell["fine_cell_id"]): _unknown_terrain("outside_dtm_coverage")
                for cell in cells
            }
        array = self.band.ReadAsArray(x0, y0, x1 - x0, y1 - y0)
        if array is None:
            return {
                str(cell["fine_cell_id"]): _unknown_terrain("dtm_read_failed")
                for cell in cells
            }
        rows_list = array.tolist() if hasattr(array, "tolist") else array
        scale = self.band.GetScale()
        offset = self.band.GetOffset()
        scale = 1.0 if scale is None else float(scale)
        offset = 0.0 if offset is None else float(offset)
        result = {}
        for cell in cells:
            fine_cell_id = str(cell["fine_cell_id"])
            window = windows.get(fine_cell_id)
            if not window:
                result[fine_cell_id] = _unknown_terrain("outside_dtm_coverage")
                continue
            cell_x0 = max(x0, window[0])
            cell_y0 = max(y0, window[1])
            cell_x1 = min(x1, window[0] + window[2])
            cell_y1 = min(y1, window[1] + window[3])
            if cell_x1 <= cell_x0 or cell_y1 <= cell_y0:
                result[fine_cell_id] = _unknown_terrain("outside_dtm_coverage")
                continue
            values = [
                rows_list[row][column] * scale + offset
                for row in range(cell_y0 - y0, cell_y1 - y0)
                for column in range(cell_x0 - x0, cell_x1 - x0)
            ]
            result[fine_cell_id] = terrain_fact_from_pixels(values, nodata_value=self.nodata)
        return result

    def sample_footprint_ground(self, ring_metric, *, transform):
        """Maximum valid EGM2008 elevation over a footprint (windowed, read-only)."""

        if self.vertical_status != "confirmed":
            return None
        xs = [point[0] for point in ring_metric]
        ys = [point[1] for point in ring_metric]
        window = self._pixel_window([min(xs), min(ys), max(xs), max(ys)], transform)
        if not window:
            return None
        x0, y0, width, height = window
        x0 = max(0, x0)
        y0 = max(0, y0)
        x1 = min(int(self.dataset.RasterXSize), window[0] + width)
        y1 = min(int(self.dataset.RasterYSize), window[1] + height)
        if x1 <= x0 or y1 <= y0:
            return None
        array = self.band.ReadAsArray(x0, y0, x1 - x0, y1 - y0)
        if array is None:
            return None
        rows = array.tolist() if hasattr(array, "tolist") else array
        scale = self.band.GetScale() or 1.0
        offset = self.band.GetOffset() or 0.0
        values = [row[column] * float(scale) + float(offset) for row in rows for column in range(len(row))]
        fact = terrain_fact_from_pixels(values, nodata_value=self.nodata)
        return fact.get("surface_elevation_max_egm2008_m")

    # ------------------------------------------------------------------ internals

    def _pixel_window(self, metric_bbox, transform):
        """Conservative pixel index window covering a metric rectangle."""

        west, south, east, north = (float(value) for value in metric_bbox)
        corners = [(west, south), (east, south), (east, north), (west, north)]
        pixels = []
        for x, y in corners:
            geographic = transform.to_geographic([x, y])
            if geographic is None:
                return None
            pixels.append(self._to_pixel(geographic))
        if any(item is None for item in pixels):
            return None
        columns = [item[0] for item in pixels]
        rows = [item[1] for item in pixels]
        x0 = int(floor(min(columns)))
        y0 = int(floor(min(rows)))
        x1 = int(floor(max(columns))) + 1
        y1 = int(floor(max(rows))) + 1
        return x0, y0, x1 - x0, y1 - y0

    def _to_pixel(self, coordinate):
        try:
            x, y, *_ = self._transform_to_raster_crs(coordinate)
        except (TypeError, ValueError, RuntimeError):
            return None
        pixel, line = (
            self.inverse[0] + self.inverse[1] * x + self.inverse[2] * y,
            self.inverse[3] + self.inverse[4] * x + self.inverse[5] * y,
        )
        if pixel != pixel or line != line:
            return None
        return pixel, line

    def _transform_to_raster_crs(self, coordinate):
        if self._to_raster is None:
            target = self.osr.SpatialReference()
            target.ImportFromWkt(self.projection)
            source = self.osr.SpatialReference()
            source.ImportFromEPSG(4326)
            for item in (source, target):
                if hasattr(item, "SetAxisMappingStrategy"):
                    item.SetAxisMappingStrategy(self.osr.OAMS_TRADITIONAL_GIS_ORDER)
            self._to_raster = self.osr.CoordinateTransformation(source, target)
        return self._to_raster.TransformPoint(float(coordinate[0]), float(coordinate[1]))


def _unknown_terrain(reason):
    return {
        "data_status": "unknown",
        "surface_elevation_max_egm2008_m": None,
        "valid_pixel_count": 0,
        "nodata_pixel_count": 0,
        "sampling": "intersecting_valid_fabdem_pixels_max_egm2008_plus_explicit_terrain_clearance",
        "reason": reason,
    }


# --------------------------------------------------------------------------- buildings


class QgisGpkgBuildingSource:
    """Corridor-only building query through the GeoPackage provider spatial index."""

    role = "buildings"

    def __init__(self, path, *, layer_name="buildings", height_field="height_m",
                 height_status_field="height_status", gdal=None):
        from qgis.core import QgsCoordinateReferenceSystem, QgsVectorLayer

        self.path = Path(path).expanduser().resolve()
        if not self.path.is_file():
            raise ValueError("buildings 必须是存在的 GeoPackage 文件")
        self.layer_name = str(layer_name)
        self.layer = QgsVectorLayer(
            f"{self.path}|layername={self.layer_name}", "V3B buildings", "ogr",
        )
        if not self.layer.isValid():
            raise ValueError(f"无法读取 buildings GeoPackage 图层 {self.layer_name}")
        fields = {field.name() for field in self.layer.fields()}
        if height_field not in fields:
            raise ValueError(f"buildings 图层缺少高度字段 {height_field}")
        self.height_field = height_field
        self.height_status_field = height_status_field if height_status_field in fields else None
        self.metric = QgsCoordinateReferenceSystem("EPSG:32651")
        self._transform = None

    def describe(self):
        stat = self.path.stat()
        extent = self.layer.extent()
        return {
            "role": self.role, "file_name": self.path.name, "layer": self.layer_name,
            "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            "feature_count": int(self.layer.featureCount()),
            "crs": self.layer.crs().authid() if self.layer.crs().isValid() else None,
            "extent": [extent.xMinimum(), extent.yMinimum(), extent.xMaximum(), extent.yMaximum()],
            "spatial_index_available": self.has_spatial_index(),
            "query_mode": BUILDING_QUERY_MODE,
            "height_field": self.height_field,
            "height_status_field": self.height_status_field,
        }

    def has_spatial_index(self):
        try:
            return bool(self.layer.hasSpatialIndex())
        except (AttributeError, TypeError):
            return False

    def usable(self):
        if not self.has_spatial_index():
            return False, "building_source_spatial_index_missing"
        return True, None

    def query_corridor(self, metric_bounds, *, transform, horizontal_clearance_m):
        """Footprints whose metric rectangle is within the explicit clearance."""

        from qgis.core import (
            QgsCoordinateTransform, QgsFeatureRequest, QgsGeometry, QgsProject, QgsRectangle,
        )

        if self._transform is None:
            context = QgsProject.instance().transformContext()
            self._transform = QgsCoordinateTransform(self.layer.crs(), self.metric, context)
        west, south, east, north = (float(value) for value in metric_bounds)
        margin = float(horizontal_clearance_m)
        corners = [
            transform.to_geographic([west - margin, south - margin]),
            transform.to_geographic([east + margin, north + margin]),
        ]
        if any(item is None for item in corners):
            return {"status": "missing_data", "reason": "metric_transform_unavailable", "footprints": []}
        rectangle = QgsRectangle(
            min(corners[0][0], corners[1][0]), min(corners[0][1], corners[1][1]),
            max(corners[0][0], corners[1][0]), max(corners[0][1], corners[1][1]),
        )
        request = QgsFeatureRequest().setFilterRect(rectangle)
        footprints = []
        for feature in self.layer.getFeatures(request):
            geometry = feature.geometry()
            if geometry is None or geometry.isEmpty():
                continue
            metric = QgsGeometry(geometry)
            metric.transform(self._transform)
            height = _number_or_none(feature[self.height_field])
            status = (
                str(feature[self.height_status_field] or "")
                if self.height_status_field else ("predicted" if height is not None else "unknown")
            )
            footprints.append({
                "building_id": str(feature["id"]) if "id" in {f.name() for f in self.layer.fields()}
                else str(feature.id()),
                "ring_metric": _ring_metric(metric),
                "height_m": height,
                "height_status": status or ("predicted" if height is not None else "unknown"),
            })
        return {
            "status": "passed", "footprints": footprints,
            "query": {
                "rectangle": [rectangle.xMinimum(), rectangle.yMinimum(),
                              rectangle.xMaximum(), rectangle.yMaximum()],
                "mode": BUILDING_QUERY_MODE,
                "horizontal_clearance_m": margin,
                "spatial_index_available": self.has_spatial_index(),
            },
        }


def _ring_metric(geometry):
    try:
        polygon = geometry.asPolygon()
    except (AttributeError, TypeError):
        polygon = None
    if not polygon:
        return []
    return [[float(point.x()), float(point.y())] for point in polygon[0]]


def _number_or_none(value):
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


# --------------------------------------------------------------------------- airspace


class ConfirmedAirspacePolygonSource:
    """Confirmed ``AirspacePolicy`` classification for fine cells.

    Two independent confirmed checks must both pass before a fine cell is
    ``confirmed_allowed``:

    1. its **parent** L8 cell is in the confirmed ``allowed_grid_ids`` (the same
       coarse evidence V3-A consumes), and
    2. its centre is inside a **confirmed allowed** polygon.

    A centre inside a confirmed blocked polygon is ``confirmed_restricted``.
    Anything else -- including an unconfirmed policy or a missing geometry -- stays
    ``unknown`` and is therefore infeasible.  Layer names and colours are never
    consulted.  A fine cell is a *point* test, not a full-cell coverage proof:
    the exact polygon membership is V3-C.
    """

    role = "airspace_policy"

    def __init__(self, eligibility):
        self.eligibility = eligibility if isinstance(eligibility, dict) else {}
        self.allowed_ids = {str(item) for item in self.eligibility.get("allowed_grid_ids") or []}
        self.allowed_polygons, self.blocked_polygons = [], []
        for feature in self.eligibility.get("features") or []:
            if not isinstance(feature, dict):
                continue
            if feature.get("policy_confirmed") is not True:
                continue
            polygons = polygons_of(feature.get("geometry"))
            if not polygons:
                continue
            entry = [(polygon, feature.get("feature_id")) for polygon in polygons]
            if feature.get("route_eligibility") == "allowed":
                self.allowed_polygons.extend(entry)
            elif feature.get("route_eligibility") == "blocked":
                self.blocked_polygons.extend(entry)

    def describe(self):
        return {
            "role": self.role,
            "query_mode": AIRSPACE_QUERY_MODE,
            "eligibility_status": self.eligibility.get("status"),
            "algorithm_id": self.eligibility.get("algorithm_id"),
            "algorithm_version": self.eligibility.get("algorithm_version"),
            "feature_fingerprint": self.eligibility.get("feature_fingerprint"),
            "policy_fingerprint": self.eligibility.get("policy_fingerprint"),
            "fingerprint": self.eligibility.get("fingerprint"),
            "allowed_grid_cell_count": len(self.allowed_ids),
            "confirmed_allowed_polygon_count": len(self.allowed_polygons),
            "confirmed_blocked_polygon_count": len(self.blocked_polygons),
            "inferred_from_name_or_color": False,
        }

    def usable(self):
        if not self.allowed_polygons:
            return False, "airspace_policy_unavailable"
        return True, None

    def classify(self, cells, *, parent_binding, transform):
        result = {}
        for cell in cells:
            fine_cell_id = str(cell["fine_cell_id"])
            parent_grid_id = parent_binding.get(fine_cell_id)
            geographic = cell.get("center")
            metric = cell.get("center_metric")
            if geographic is None and metric is not None:
                geographic = transform.to_geographic(metric)
            if geographic is None:
                result[fine_cell_id] = _airspace("unknown", parent_grid_id, None, "fine_cell_center_unresolved")
                continue
            point = [float(geographic[0]), float(geographic[1])]
            blocked = next(
                (entry for entry in self.blocked_polygons
                 if point_covered_by_polygon(point, entry[0])), None,
            )
            if blocked is not None:
                result[fine_cell_id] = _airspace(
                    "confirmed_restricted", parent_grid_id, blocked[1],
                    "fine_center_inside_confirmed_blocked_polygon",
                )
                continue
            parent_allowed = parent_grid_id in self.allowed_ids
            allowed = next(
                (entry for entry in self.allowed_polygons
                 if point_covered_by_polygon(point, entry[0])), None,
            )
            if parent_allowed and allowed is not None:
                result[fine_cell_id] = _airspace(
                    "confirmed_allowed", parent_grid_id, allowed[1], None,
                )
            else:
                result[fine_cell_id] = _airspace(
                    "unknown", parent_grid_id, None,
                    "parent_not_confirmed_allowed" if not parent_allowed
                    else "fine_center_not_inside_confirmed_allowed_polygon",
                )
        return result


def _airspace(status, parent_grid_id, feature_id, reason):
    return {
        "status": status,
        "feature_id": feature_id,
        "mapping_method": AIRSPACE_MAPPING_METHOD,
        "parent_grid_id": parent_grid_id,
        "policy_confirmed": status in ("confirmed_allowed", "confirmed_restricted"),
        "query_mode": AIRSPACE_QUERY_MODE,
        "reason": reason,
        "semantics": "confirmed_policy_only_point_test_at_fine_center_not_full_cell_coverage_v3c_exact_pending",
    }


# --------------------------------------------------------------------------- adapter


class FineEnvironmentAdapter:
    """Assemble the V3-B corridor-local fine environment from real sources."""

    adapter_id = ADAPTER_ID
    adapter_version = ADAPTER_VERSION

    def __init__(
        self, *, transform, terrain_source=None, building_source=None, airspace_source=None,
        resolution_m=None, resolution_source=None, max_stride_cells=1,
        risk_result=None, source_resolution_m=None, lineage=None,
    ):
        self.transform = transform
        self.terrain_source = terrain_source
        self.building_source = building_source
        self.airspace_source = airspace_source
        self.resolution_m = resolution_m
        self.resolution_source = resolution_source
        self.max_stride_cells = int(max_stride_cells)
        self.risk_result = risk_result or {}
        self.source_resolution_m = source_resolution_m
        self.lineage = dict(lineage or {})

    # ------------------------------------------------------------------ readiness

    def source_readiness(self):
        """What is actually available, with explicit reasons."""

        entries = {}
        for role, source in (
            ("terrain_dtm", self.terrain_source),
            ("buildings", self.building_source),
            ("airspace_policy", self.airspace_source),
        ):
            if source is None:
                entries[role] = {
                    "role": role, "status": "blocked",
                    "reasons": [f"{role}_source_unavailable"], "describe": None,
                }
                continue
            usable, reason = source.usable() if hasattr(source, "usable") else (True, None)
            entries[role] = {
                "role": role, "status": "ready" if usable else "blocked",
                "reasons": [] if usable else [reason],
                "describe": source.describe() if hasattr(source, "describe") else None,
            }
        resolution = self._resolve_resolution()
        entries["resolution"] = {
            "role": "resolution", "status": "ready" if resolution["resolution_m"] else "blocked",
            "reasons": [] if resolution["resolution_m"] else [resolution["reason"]],
            "resolution_m": resolution["resolution_m"],
            "resolution_source": resolution["resolution_source"],
            "requested_resolution_m": resolution["requested_resolution_m"],
            "effective_source_resolution_m": resolution["effective_source_resolution_m"],
            "semantics": "fine_horizontal_resolution_must_be_explicit_or_dtm_effective_never_a_30m_constant",
        }
        entries["metric_frame"] = {
            "role": "metric_frame", "status": "ready" if self.transform else "blocked",
            "reasons": [] if self.transform else ["metric_transform_unavailable"],
            "describe": self.transform.describe() if self.transform else None,
        }
        return entries

    def _resolve_resolution(self):
        effective = None
        if self.terrain_source is not None and hasattr(self.terrain_source, "effective_resolution_m"):
            effective = self.terrain_source.effective_resolution_m()
        if self.resolution_source == "explicit_configuration" and self.resolution_m:
            return {
                "resolution_m": float(self.resolution_m),
                "resolution_source": "explicit_configuration",
                "requested_resolution_m": float(self.resolution_m),
                "effective_source_resolution_m": effective,
                "reason": None,
            }
        if self.resolution_source == "dtm_effective_resolution" or (
            self.resolution_source is None and effective
        ):
            if effective:
                return {
                    "resolution_m": float(effective),
                    "resolution_source": "dtm_effective_resolution",
                    "requested_resolution_m": self.resolution_m,
                    "effective_source_resolution_m": float(effective),
                    "reason": None,
                }
        return {
            "resolution_m": None,
            "resolution_source": None,
            "requested_resolution_m": self.resolution_m,
            "effective_source_resolution_m": effective,
            "reason": "fine_resolution_unresolved",
        }

    # ------------------------------------------------------------------ build

    def build(self, *, policy, corridor, parent_cells, source_detail=None):
        """Build frame + fine grid + canonical fine environment.

        Returns ``{"status", "reason", "readiness", "frame", "fine_grid",
        "environment", "source_audit", "parent_soft_fields"}``.  A blocked source
        produces ``status="blocked"``; no environment is fabricated.
        """

        readiness = self.source_readiness()
        support_ids = list(corridor.get("support_grid_ids") or corridor.get("center_grid_ids") or [])
        parent_by_id = {str(cell["grid_id"]): cell for cell in parent_cells or []}
        selected = [parent_by_id[grid_id] for grid_id in support_ids if grid_id in parent_by_id]
        if not selected:
            return self._blocked("corridor_support_cells_missing", readiness)
        resolution = self._resolve_resolution()
        if not resolution["resolution_m"] or not self.transform:
            return self._blocked(resolution["reason"] or "metric_transform_unavailable", readiness)
        # Terrain and buildings are *required* hard facts: without them there is no
        # environment to refine in.  Airspace is different -- the environment still
        # exists, so its cells simply stay ``unknown`` (and therefore infeasible)
        # until a confirmed policy is supplied.
        for role in ("terrain_dtm", "buildings"):
            if readiness[role]["status"] != "ready":
                reason = (
                    readiness[role]["reasons"][0] if readiness[role]["reasons"]
                    else f"{role}_source_unavailable"
                )
                return self._blocked(reason, readiness)

        support_metric = []
        for cell in selected:
            center_metric = self.transform.to_metric(cell["center"])
            bbox = cell.get("bbox")
            bbox_metric = None
            if bbox:
                first = self.transform.to_metric([bbox[0], bbox[1]])
                second = self.transform.to_metric([bbox[2], bbox[3]])
                bbox_metric = [first[0], first[1], second[0], second[1]]
            support_metric.append({
                "grid_id": str(cell["grid_id"]),
                "center": [float(cell["center"][0]), float(cell["center"][1])],
                "center_metric": center_metric,
                "bbox_metric": bbox_metric,
                "soft_fields": {},
                "airspace": dict(cell.get("airspace") or {}),
            })
        with_bbox = [item for item in support_metric if item["bbox_metric"]]
        if not with_bbox:
            return self._blocked("corridor_support_cells_missing", readiness)

        parent_soft_fields = parent_soft_fields_from_risk_contributors(
            self.risk_result,
            [item["grid_id"] for item in support_metric],
            source_resolution_m=self.source_resolution_m,
        )
        for item in support_metric:
            item["soft_fields"] = parent_soft_fields.get(item["grid_id"], {})

        west = min(item["bbox_metric"][0] for item in with_bbox)
        south = min(item["bbox_metric"][1] for item in with_bbox)
        east = max(item["bbox_metric"][2] for item in with_bbox)
        north = max(item["bbox_metric"][3] for item in with_bbox)
        frame = build_local_frame(
            metric_bounds=[west, south, east, north],
            horizontal_crs=self.transform.authority,
            horizontal_crs_source=self.transform.describe().get("method"),
            resolution_m=resolution["resolution_m"],
            local_to_geographic={
                "method": self.transform.describe().get("method"),
                "authority": self.transform.authority,
                "display_only": bool(self.transform.describe().get("display_only", False)),
                "interpolated_from_parent_cells": False,
                "note": "局部 fine index → 经纬度 由 adapter 的显式投影逆变换给出，不是插值猜测",
            },
            provenance={"adapter_id": ADAPTER_ID, "adapter_version": ADAPTER_VERSION},
        )
        grid = build_fine_grid_spec(
            frame=frame,
            resolution_m=resolution["resolution_m"],
            resolution_source=resolution["resolution_source"],
            requested_resolution_m=resolution["requested_resolution_m"],
            effective_source_resolution_m=resolution["effective_source_resolution_m"],
            parent_grid_resolution_m=_parent_resolution(with_bbox),
            parent_cells_metric=support_metric,
            corridor_id=corridor.get("corridor_id"),
            corridor_ring_n=corridor.get("ring_n") or 0,
            provenance={
                "adapter_id": ADAPTER_ID,
                "source_detail": dict(source_detail or {}),
                "lineage": self.lineage,
            },
        )
        cells = fine_cells_from_spec(grid)
        for cell in cells:
            cell["center"] = self.transform.to_geographic(cell["center_metric"])
        binding = bind_fine_cells_to_parents(cells, support_metric)
        for cell in cells:
            cell["parent_grid_id"] = binding[str(cell["fine_cell_id"])]
            cell["parent_binding"] = "nearest_parent_cell_center"

        terrain = {}
        if readiness["terrain_dtm"]["status"] == "ready":
            terrain = self.terrain_source.sample_cells(cells, transform=self.transform)
        else:
            terrain = {
                str(cell["fine_cell_id"]): _unknown_terrain(
                    readiness["terrain_dtm"]["reasons"][0] if readiness["terrain_dtm"]["reasons"]
                    else "terrain_source_unavailable"
                )
                for cell in cells
            }
        buildings = self._buildings(policy, cells, readiness)
        if readiness["airspace_policy"]["status"] == "ready":
            airspace = self.airspace_source.classify(
                cells, parent_binding=binding, transform=self.transform,
            )
        else:
            airspace = {
                str(cell["fine_cell_id"]): _airspace(
                    "unknown", binding.get(str(cell["fine_cell_id"])), None,
                    readiness["airspace_policy"]["reasons"][0],
                )
                for cell in cells
            }
        soft_fields = upsample_soft_fields(
            {item["grid_id"]: item for item in support_metric}, binding,
        )
        source_audit = self._source_audit(readiness, policy)
        environment = assemble_fine_environment(
            spec=grid,
            cells=cells,
            terrain_by_cell=terrain,
            buildings_by_cell=buildings,
            airspace_by_cell=airspace,
            soft_fields_by_cell=soft_fields,
            properties={
                "resolution_m": resolution["resolution_m"],
                "cell_size_m": resolution["resolution_m"],
                "terrain_clearance_m": policy.get("terrain_clearance_m"),
                "building_horizontal_clearance_m": policy.get("building_horizontal_clearance_m"),
                "building_vertical_clearance_m": policy.get("building_vertical_clearance_m"),
                "soft_field_sources": _soft_sources(source_audit),
            },
            source_audit=source_audit,
            source_type="real_sources",
            provenance={
                "adapter_id": ADAPTER_ID,
                "adapter_version": ADAPTER_VERSION,
                "lineage": self.lineage,
                "not_a_validation": True,
                "exact_validation_is_v3c": True,
            },
        )
        environment["terrain_window"] = getattr(self.terrain_source, "last_window", None)
        return {
            "status": "passed",
            "reason": None,
            "readiness": readiness,
            "frame": frame,
            "fine_grid": grid,
            "environment": environment,
            "source_audit": source_audit,
            "parent_soft_fields": parent_soft_fields,
            "resolution": resolution,
        }

    # ------------------------------------------------------------------ internals

    def _buildings(self, policy, cells, readiness):
        if readiness["buildings"]["status"] != "ready":
            return {
                str(cell["fine_cell_id"]): {
                    "data_status": "unknown",
                    "required_clearance_egm2008_m": None,
                    "building_ids": [],
                    "reason": (
                        readiness["buildings"]["reasons"][0]
                        if readiness["buildings"]["reasons"] else "building_source_unavailable"
                    ),
                }
                for cell in cells
            }
        horizontal = policy.get("building_horizontal_clearance_m")
        vertical = policy.get("building_vertical_clearance_m")
        if horizontal is None or vertical is None:
            return None
        metric_bounds = _union_bounds(cell["bbox_metric"] for cell in cells)
        query = self.building_source.query_corridor(
            metric_bounds, transform=self.transform, horizontal_clearance_m=horizontal,
        )
        if query.get("status") != "passed":
            return None
        footprints = []
        for footprint in query.get("footprints") or []:
            ground = None
            if self.terrain_source is not None and readiness["terrain_dtm"]["status"] == "ready":
                ground = self.terrain_source.sample_footprint_ground(
                    footprint["ring_metric"], transform=self.transform,
                )
            footprints.append({**footprint, "ground_elevation_max_egm2008_m": ground})
        terrain_floor_by_cell = {}
        if self.terrain_source is not None and readiness["terrain_dtm"]["status"] == "ready":
            facts = self.terrain_source.sample_cells(cells, transform=self.transform)
            terrain_floor_by_cell = {
                fine_cell_id: fact.get("surface_elevation_max_egm2008_m")
                for fine_cell_id, fact in facts.items()
            }
        return building_facts_for_cells(
            cells, footprints,
            horizontal_clearance_m=horizontal, vertical_clearance_m=vertical,
            terrain_floor_by_cell=terrain_floor_by_cell,
        )

    def _source_audit(self, readiness, policy):
        audit = {
            "adapter_id": ADAPTER_ID,
            "adapter_version": ADAPTER_VERSION,
            "read_mode": TERRAIN_READ_MODE,
            "building_query_mode": BUILDING_QUERY_MODE,
            "airspace_query_mode": AIRSPACE_QUERY_MODE,
            "terrain_dtm": readiness["terrain_dtm"].get("describe"),
            "buildings": readiness["buildings"].get("describe"),
            "airspace_policy": readiness["airspace_policy"].get("describe"),
            "metric_frame": self.transform.describe() if self.transform else None,
            "risk_model": {
                "algorithm_id": (self.risk_result or {}).get("algorithm_id"),
                "algorithm_version": (self.risk_result or {}).get("algorithm_version"),
                "status": (self.risk_result or {}).get("status"),
                "soft_fields_reused": True,
                "reused_contributors": ["ground.population", "ground.traffic"],
            },
            "policy_fingerprint": contract_fingerprint(policy or {}, prefix="V3BPOL-"),
            "lineage": self.lineage,
            "full_raster_resample": False,
            "source_geometry_modified": False,
            "exact_validation_performed": False,
        }
        audit["fingerprint"] = contract_fingerprint(audit, prefix="V3BSRC-")
        return audit

    def _blocked(self, reason, readiness):
        return {
            "status": "blocked",
            "reason": reason,
            "readiness": readiness,
            "frame": None,
            "fine_grid": None,
            "environment": None,
            "source_audit": {},
            "parent_soft_fields": {},
        }


def _parent_resolution(items):
    sizes = [
        max(item["bbox_metric"][2] - item["bbox_metric"][0],
            item["bbox_metric"][3] - item["bbox_metric"][1])
        for item in items if item.get("bbox_metric")
    ]
    return round(max(sizes), 6) if sizes else None


def _union_bounds(bboxes):
    items = [list(bbox) for bbox in bboxes if bbox]
    if not items:
        return None
    return [
        min(item[0] for item in items), min(item[1] for item in items),
        max(item[2] for item in items), max(item[3] for item in items),
    ]


def _soft_sources(source_audit):
    return {
        "population_risk": {
            "source": "grid_risk.ground.contributors.population.normalized",
            "mapping_method": "reused_existing_risk_model_contributor_normalized_index_then_upsampled",
            "upsampled_without_new_information": True,
        },
        "traffic_risk": {
            "source": "grid_risk.ground.contributors.traffic.normalized",
            "mapping_method": "reused_existing_risk_model_contributor_normalized_index_then_upsampled",
            "upsampled_without_new_information": True,
        },
        "building_exposure": {
            "source": "fine_envelope.buildings.required_clearance_egm2008_m",
            "mapping_method": "vertical_clearance_proximity_index_0_1",
            "upsampled_without_new_information": False,
        },
    }


def real_data_source_readiness(paths, *, airspace_eligibility=None, policy_confirmed=False):
    """Report, without side effects, whether the real V3-B sources can be used.

    ``paths`` is the project's resolved data-source mapping (``terrain_dtm`` /
    ``buildings``).  Nothing is opened here beyond a file-existence check, so the
    readiness report itself is safe to call on any project.
    """

    terrain_path = paths.get("terrain_dtm")
    buildings_path = paths.get("buildings")
    eligibility = airspace_eligibility if isinstance(airspace_eligibility, dict) else {}
    allowed = len(eligibility.get("allowed_grid_ids") or [])
    missing = []
    if not terrain_path or not Path(terrain_path).is_file():
        missing.append("terrain_dtm_not_configured_or_missing")
    if not buildings_path or not Path(buildings_path).is_file():
        missing.append("buildings_geopackage_not_configured_or_missing")
    if allowed == 0:
        missing.append("no_confirmed_allowed_airspace_cells")
    if not policy_confirmed:
        missing.append("v3_policy_not_confirmed")
    return {
        "status": "blocked" if missing else "ready",
        "adapter_id": ADAPTER_ID,
        "adapter_version": ADAPTER_VERSION,
        "roles": list(FINE_SOURCE_ROLES),
        "terrain_dtm": _basename(terrain_path),
        "buildings": _basename(buildings_path),
        "confirmed_allowed_grid_cells": allowed,
        "airspace_eligibility_status": eligibility.get("status"),
        "blocking_reasons": missing,
        "resolution_policy": "explicit_configuration_or_dtm_effective_resolution_never_a_30m_constant",
        "required_before_real_run": [
            "FABDEM terrain_dtm with confirmed egm2008_orthometric vertical reference",
            "buildings GeoPackage with a provider spatial index (RTree) and a height field",
            "confirmed AirspacePolicy with at least one allowed geometry",
            "explicit confirmed V3 planning policy",
        ],
        "semantics": "readiness_report_only_no_data_read_no_fabricated_environment",
    }


__all__ = [
    "ADAPTER_BLOCK_REASONS", "ADAPTER_ID", "ADAPTER_VERSION", "AIRSPACE_QUERY_MODE",
    "BUILDING_QUERY_MODE", "ConfirmedAirspacePolygonSource",
    "EquirectangularMetricTransform", "FINE_SOURCE_ROLES", "FabdemWindowTerrainSource",
    "FineEnvironmentAdapter", "QgisGpkgBuildingSource", "QgisMetricTransform",
    "TERRAIN_READ_MODE", "real_data_source_readiness",
]
