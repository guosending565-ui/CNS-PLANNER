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
* airspace is display-only reference metadata and is never queried for planning;
* population/traffic soft indices are **reused** from the existing RiskModel
  contributor ``normalized`` values; coarse values mapped onto finer cells are
  flagged ``upsampled_without_new_information=true``.

The QGIS/GDAL imports are lazy: importing this module never requires QGIS, and the
pure assembly path (with injected sources) is fully testable without it.
"""

from __future__ import annotations

from math import atan2, cos, floor, hypot, radians, sin, sqrt
from pathlib import Path

from ..data.mapping.airspace_eligibility import (
    polygons_of, rect_covered_by_any, rect_intersects_any,
)
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
#: V3-C airspace query: the *fine cell polygon* (or the realized route's error
#: envelope) is tested against the confirmed policy union, not just its centre.
AIRSPACE_POLYGON_QUERY_MODE = "confirmed_policy_fine_cell_polygon_covered_by_allowed_union_and_disjoint_from_blocked_union"

FINE_SOURCE_ROLES = ("terrain_dtm", "buildings")

#: Horizontal-resolution provenance methods.  ``projected_linear_unit`` converts an
#: affine pixel size in a projected CRS through the CRS's own verified linear unit;
#: ``geographic_geodesic_adjacent_pixel_centres`` measures the ground distance
#: between adjacent pixel centres in the corridor/reference area.  A raw degree
#: value is **never** reported as metres.
RESOLUTION_METHODS = (
    "projected_crs_verified_linear_unit",
    "geographic_geodesic_adjacent_pixel_centres",
    "unresolved",
)

#: Metres per degree at the equator, used only as an explicitly labelled fallback
#: when no geodesic engine is available (the method string always says so).
_EARTH_RADIUS_M = 6371008.8

#: Reasons the adapter itself can report instead of building an environment.
ADAPTER_BLOCK_REASONS = (
    "fine_resolution_unresolved",
    "terrain_source_unavailable",
    "building_source_unavailable",
    "building_source_spatial_index_missing",
    "airspace_policy_unavailable",
    "metric_transform_unavailable",
    "corridor_support_cells_missing",
    "airspace_polygon_evidence_unavailable",
)


# --------------------------------------------------------------------------- resolution


def _unit_factor_to_metres(crs, osr):
    """The CRS's own verified linear unit → metre factor, or ``(None, name)``.

    A projected CRS declares its linear unit (``metre``, ``US survey foot``, ...)
    and the affine ``GeoTransform`` is expressed in *that* unit.  1.0 is returned
    only when the unit actually is the metre; any other unit uses its own
    conversion.  When the unit cannot be read, ``None`` forces the caller to fall
    back to a geodesic measurement instead of assuming metres.
    """

    if crs is None:
        return None, None
    try:
        if crs.IsGeographic():
            return None, None
    except AttributeError:
        pass
    name = None
    try:
        name = crs.GetLinearUnitsName() if hasattr(crs, "GetLinearUnitsName") else None
    except (TypeError, RuntimeError):
        name = None
    if name:
        normalized = str(name).strip().lower()
        if normalized in ("metre", "meter", "m"):
            return 1.0, str(name)
        try:
            factor = float(crs.GetLinearUnits())
        except (AttributeError, TypeError, RuntimeError):
            return None, str(name)
        if factor and factor > 0:
            return factor, str(name)
    return None, name


def _geodesic_distance_m(first, second, *, geod=None):
    """Ground distance in metres between two ``[lon, lat]`` points.

    Uses ``pyproj.Geod`` when available (the same engine the existing metric
    reference comparison uses); otherwise a documented spherical haversine that is
    labelled as such.  It never returns a degree value.
    """

    lon1, lat1 = float(first[0]), float(first[1])
    lon2, lat2 = float(second[0]), float(second[1])
    if geod is not None:
        try:
            return float(geod.inv(lon1, lat1, lon2, lat2)[2]), "pyproj_geod_wgs84_ellipsoid"
        except (TypeError, ValueError, RuntimeError):
            pass
    phi1, phi2 = radians(lat1), radians(lat2)
    delta_phi = phi2 - phi1
    delta_lambda = radians(lon2 - lon1)
    a = sin(delta_phi / 2.0) ** 2 + cos(phi1) * cos(phi2) * sin(delta_lambda / 2.0) ** 2
    return 2.0 * _EARTH_RADIUS_M * atan2(sqrt(a), sqrt(max(0.0, 1.0 - a))), "spherical_haversine_labelled_approximation"


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


class _FabdemRasterBase:
    """Shared read-only FABDEM access: CRS, transforms, native pixel size in metres.

    Both the V3-B window sampler and the V3-C native-pixel window share this base so
    the resolution/unit/projection semantics exist exactly once.
    """

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
        self._to_geographic = None
        self._crs = None
        self.resolution_detail = None
        self.last_window = None

    # ------------------------------------------------------------------ resolution

    def effective_resolution_m(self):
        """Horizontal pixel size in **metres**, with explicit provenance.

        The affine ``GeoTransform`` pixel size is expressed in the raster CRS's own
        units.  It is *not* metres in general:

        * projected CRS ⇒ convert through the CRS's own verified linear unit
          (``projected_crs_verified_linear_unit``);
        * geographic CRS ⇒ the raw values are **degrees** and are never reported as
          metres; the ground distance between adjacent pixel centres is measured
          geodesically near the corridor/reference location
          (``geographic_geodesic_adjacent_pixel_centres``).

        Anything that cannot be resolved returns ``None`` (blocked), never a
        degree-as-metre number.
        """

        detail = self.effective_resolution_detail()
        if detail.get("status") != "passed":
            return None
        return detail.get("effective_resolution_m")

    def effective_resolution_detail(self):
        stat = self.path.stat()
        detail = self.effective_resolution_detail()
        self.resolution_detail = detail
        return {
            "role": self.role, "dataset": "FABDEM", "file_name": self.path.name,
            "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            "driver": self.dataset.GetDriver().ShortName,
            "width": int(self.dataset.RasterXSize), "height": int(self.dataset.RasterYSize),
            "pixel_size": [abs(self.transform[1]), abs(self.transform[5])],
            "pixel_size_unit": detail.get("native_pixel_size_unit"),
            "effective_resolution_m": detail.get("effective_resolution_m"),
            "effective_resolution_m_x": detail.get("effective_resolution_m_x"),
            "effective_resolution_m_y": detail.get("effective_resolution_m_y"),
            "resolution_method": detail.get("method"),
            "resolution_status": detail.get("status"),
            "resolution_detail": detail,
            "nodata": self.nodata,
            "vertical_reference": self.vertical_reference,
            "vertical_status": self.vertical_status,
            "read_mode": TERRAIN_READ_MODE,
        }

    def effective_resolution_m(self):
        """Horizontal pixel size in **metres**, with explicit provenance.

        The affine ``GeoTransform`` pixel size is expressed in the raster CRS's own
        units.  It is *not* metres in general:

        * projected CRS ⇒ convert through the CRS's own verified linear unit
          (``projected_crs_verified_linear_unit``);
        * geographic CRS ⇒ the raw values are **degrees** and are never reported as
          metres; the ground distance between adjacent pixel centres is measured
          geodesically near the corridor/reference location
          (``geographic_geodesic_adjacent_pixel_centres``).

        Anything that cannot be resolved returns ``None`` (blocked), never a
        degree-as-metre number.
        """

        detail = self.effective_resolution_detail()
        if detail.get("status") != "passed":
            return None
        return detail.get("effective_resolution_m")

    def effective_resolution_detail(self):
        """Native pixel size, unit and the metric conversion method (auditable)."""

        native_x = abs(float(self.transform[1]))
        native_y = abs(float(self.transform[5]))
        detail = {
            "native_pixel_size_x": native_x,
            "native_pixel_size_y": native_y,
            "native_pixel_size_unit": None,
            "effective_resolution_m_x": None,
            "effective_resolution_m_y": None,
            "effective_resolution_m": None,
            "method": "unresolved",
            "status": "blocked",
            "reason": None,
            "reference_location": None,
            "geodesic_backend": None,
            "degree_values_never_reported_as_metres": True,
        }
        if native_x <= 0 or native_y <= 0:
            detail["reason"] = "degenerate_affine_pixel_size"
            return detail
        crs = self._raster_crs()
        geographic = None
        try:
            geographic = crs.IsGeographic() if crs is not None else None
        except AttributeError:
            geographic = None
        if geographic is False:
            factor, unit_name = _unit_factor_to_metres(crs, self.osr)
            if not factor:
                detail["reason"] = "projected_crs_linear_unit_unresolved"
                detail["native_pixel_size_unit"] = unit_name
                return detail
            detail.update({
                "native_pixel_size_unit": unit_name or "unknown_projected_linear_unit",
                "effective_resolution_m_x": native_x * factor,
                "effective_resolution_m_y": native_y * factor,
                "effective_resolution_m": max(native_x * factor, native_y * factor),
                "method": "projected_crs_verified_linear_unit",
                "status": "passed",
                "unit_factor_to_metres": factor,
            })
            return detail
        if geographic is None:
            detail["reason"] = "raster_crs_unreadable"
            return detail
        # Geographic CRS: degrees in, metres out -- measured, not relabelled.
        detail["native_pixel_size_unit"] = "degree"
        reference = self._reference_pixel_location()
        if reference is None:
            detail["reason"] = "geographic_reference_location_unresolved"
            return detail
        geod, backend = _load_geod(self.gdal)
        column, row = reference
        origin = self._geographic_of_pixel(column, row)
        if origin is None:
            detail["reason"] = "geographic_pixel_centre_transform_unavailable"
            return detail
        neighbours = {}
        # The affine basis vectors already carry the pixel size, so an adjacent pixel is
        # an offset of exactly **one** pixel step in raster space -- not the pixel size
        # expressed in the CRS's units.
        for key, (d_column, d_row) in (
            ("x", (1.0, 0.0)),
            ("y", (0.0, 1.0)),
        ):
            neighbour = self._geographic_of_pixel(column + d_column, row + d_row)
            if neighbour is None:
                detail["reason"] = "geographic_neighbour_pixel_centre_transform_unavailable"
                return detail
            value, used_backend = _geodesic_distance_m(
                list(origin), list(neighbour), geod=geod,
            )
            neighbours[key] = value
            backend = used_backend
        detail.update({
            "effective_resolution_m_x": neighbours["x"],
            "effective_resolution_m_y": neighbours["y"],
            "effective_resolution_m": max(neighbours["x"], neighbours["y"]),
            "method": "geographic_geodesic_adjacent_pixel_centres",
            "status": "passed",
            "reference_location": [float(value) for value in origin],
            "reference_pixel": [float(column), float(row)],
            "geodesic_backend": backend,
            "measured_native_pixel_size_x": native_x,
            "measured_native_pixel_size_y": native_y,
        })
        return detail

    def _raster_crs(self):
        if getattr(self, "_crs", None) is None:
            crs = self.osr.SpatialReference()
            try:
                crs.ImportFromWkt(self.projection)
            except (TypeError, RuntimeError):
                self._crs = None
                return None
            self._crs = crs
        return self._crs

    def _geographic_of_pixel(self, pixel, line):
        """Raster pixel space → (lon, lat) in traditional GIS order."""

        if self._to_geographic is None:
            target = self.osr.SpatialReference()
            target.ImportFromEPSG(4326)
            source = self.osr.SpatialReference()
            try:
                source.ImportFromWkt(self.projection)
            except (TypeError, RuntimeError):
                self._to_geographic = False
                return None
            for item in (target, source):
                if hasattr(item, "SetAxisMappingStrategy"):
                    item.SetAxisMappingStrategy(self.osr.OAMS_TRADITIONAL_GIS_ORDER)
            self._to_geographic = self.osr.CoordinateTransformation(source, target)
        if self._to_geographic is False:
            return None
        x, y = (
            self.transform[0] + self.transform[1] * float(pixel) + self.transform[2] * float(line),
            self.transform[3] + self.transform[4] * float(pixel) + self.transform[5] * float(line),
        )
        try:
            point = self._to_geographic.TransformPoint(x, y)
        except (TypeError, RuntimeError):
            return None
        return [float(point[0]), float(point[1])]

    def _reference_pixel_location(self):
        """A raster-interior reference **pixel** location for the geodesic measurement."""

        pixels = (
            (0.5, 0.5),
            (0.5, self.dataset.RasterYSize - 0.5),
            (self.dataset.RasterXSize - 0.5, 0.5),
            (self.dataset.RasterXSize - 0.5, self.dataset.RasterYSize - 0.5),
            (self.dataset.RasterXSize / 2.0, self.dataset.RasterYSize / 2.0),
        )
        for column, row in pixels:
            # Prefer a location whose ground position is known and inside the raster.
            if 0 <= column <= self.dataset.RasterXSize and 0 <= row <= self.dataset.RasterYSize:
                return column, row
        return None


class FabdemWindowTerrainSource(_FabdemRasterBase):
    """Read-only, windowed FABDEM DTM sampler (GDAL) for V3-B fine cells."""

    # ------------------------------------------------------------------ protocol

    def describe(self):
        stat = self.path.stat()
        detail = self.effective_resolution_detail()
        self.resolution_detail = detail
        return {
            "role": self.role, "dataset": "FABDEM", "file_name": self.path.name,
            "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            "driver": self.dataset.GetDriver().ShortName,
            "width": int(self.dataset.RasterXSize), "height": int(self.dataset.RasterYSize),
            "pixel_size": [abs(self.transform[1]), abs(self.transform[5])],
            "pixel_size_unit": detail.get("native_pixel_size_unit"),
            "effective_resolution_m": detail.get("effective_resolution_m"),
            "effective_resolution_m_x": detail.get("effective_resolution_m_x"),
            "effective_resolution_m_y": detail.get("effective_resolution_m_y"),
            "resolution_method": detail.get("method"),
            "resolution_status": detail.get("status"),
            "resolution_detail": detail,
            "nodata": self.nodata,
            "vertical_reference": self.vertical_reference,
            "vertical_status": self.vertical_status,
            "read_mode": TERRAIN_READ_MODE,
        }

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


def _load_geod(gdal=None):
    """A ``pyproj.Geod`` when available; otherwise ``(None, fallback_label)``.

    The existing metric reference comparison already depends on ``pyproj``; it is
    optional here so the pure-assembly/test path keeps working without it, and the
    fallback is always *labelled* in the recorded provenance.
    """

    try:
        from pyproj import Geod
    except ImportError:
        return None, "pyproj_unavailable_spherical_fallback"
    try:
        return Geod(ellps="WGS84"), "pyproj_geod_wgs84_ellipsoid"
    except (TypeError, ValueError, RuntimeError):
        return None, "pyproj_geod_unavailable_spherical_fallback"


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

    A fine cell is ``confirmed_allowed`` only when **both** confirmed checks pass:

    1. its **parent** L8 cell is in the confirmed ``allowed_grid_ids`` (the same
       coarse evidence V3-A consumes); and
    2. the cell's **full fine-cell polygon** is ``covered_by`` the confirmed allowed
       union -- i.e. fully contained in *one single* confirmed allowed polygon, with
       no polygon/hole boundary entering the cell (the same conservative rule the
       existing eligibility builder uses for L8 cells).

    The cell's polygon must additionally have **no area intersection** with any
    confirmed blocked polygon.  A mixed/boundary cell (partly allowed, partly
    outside, or crossing a policy boundary) and an unconfirmed policy both stay
    ``unknown`` -- a cell centre is never enough.  Layer names and colours are never
    consulted.

    This is still a *discrete* fine-cell decision: V3-C performs the final,
    route-level continuous vector validation of the realized trajectory's
    uncertainty envelope.
    """

    role = "airspace_policy"

    def __init__(self, eligibility, *, ring_densification=8):
        self.eligibility = eligibility if isinstance(eligibility, dict) else {}
        self.allowed_ids = {str(item) for item in self.eligibility.get("allowed_grid_ids") or []}
        self.ring_densification = max(2, int(ring_densification))
        self.allowed_polygons, self.blocked_polygons = [], []
        self.allowed_entries, self.blocked_entries = [], []
        for feature in self.eligibility.get("features") or []:
            if not isinstance(feature, dict):
                continue
            if feature.get("policy_confirmed") is not True:
                continue
            polygons = polygons_of(feature.get("geometry"))
            if not polygons:
                continue
            interval = _altitude_interval(feature)
            for polygon in polygons:
                if feature.get("route_eligibility") == "allowed":
                    self.allowed_polygons.append(polygon)
                    self.allowed_entries.append({
                        "polygon": polygon, "feature_id": feature.get("feature_id"),
                        "altitude_interval": interval, "source": feature.get("policy_source"),
                    })
                elif feature.get("route_eligibility") == "blocked":
                    self.blocked_polygons.append(polygon)
                    self.blocked_entries.append({
                        "polygon": polygon, "feature_id": feature.get("feature_id"),
                        "altitude_interval": interval, "source": feature.get("policy_source"),
                    })

    def describe(self):
        return {
            "role": self.role,
            "query_mode": AIRSPACE_POLYGON_QUERY_MODE,
            "cell_test": "fine_cell_polygon_fully_covered_by_single_confirmed_allowed_polygon_and_disjoint_from_blocked",
            "mixed_boundary_or_unconfirmed_becomes": "unknown",
            "ring_densification": self.ring_densification,
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
            "final_route_level_validation": "V3-C",
        }

    def usable(self):
        if not self.allowed_polygons:
            return False, "airspace_policy_unavailable"
        return True, None

    def metric_entries(self, transform):
        """Metric-space allowed/blocked polygon evidence for the V3-C validator."""

        allowed, blocked = [], []
        for target, source in ((allowed, self.allowed_entries), (blocked, self.blocked_entries)):
            for entry in source:
                ring, holes = entry["polygon"]
                metric_ring = _metric_ring(ring, transform)
                if metric_ring is None:
                    continue
                metric_holes = [_metric_ring(hole, transform) for hole in holes]
                if any(item is None for item in metric_holes):
                    continue
                target.append({
                    "feature_id": entry.get("feature_id"),
                    "outer_metric": metric_ring,
                    "holes_metric": metric_holes,
                    "altitude_interval": entry.get("altitude_interval"),
                    "source": entry.get("source"),
                })
        return {"allowed": allowed, "blocked": blocked}

    def classify(self, cells, *, parent_binding, transform):
        """Full fine-cell polygon test (never a centre-only test)."""

        result = {}
        for cell in cells:
            fine_cell_id = str(cell["fine_cell_id"])
            parent_grid_id = parent_binding.get(fine_cell_id)
            rectangle = self._cell_rectangle(cell, transform)
            if rectangle is None:
                result[fine_cell_id] = _airspace(
                    "unknown", parent_grid_id, None, "fine_cell_polygon_unresolved",
                )
                continue
            blocked = next(
                (entry for entry in self.blocked_entries
                 if rect_intersects_any([entry["polygon"]], rectangle)), None,
            )
            if blocked is not None:
                result[fine_cell_id] = _airspace(
                    "confirmed_restricted", parent_grid_id, blocked.get("feature_id"),
                    "fine_cell_polygon_intersects_confirmed_blocked_polygon",
                )
                continue
            parent_allowed = parent_grid_id in self.allowed_ids
            if not parent_allowed:
                result[fine_cell_id] = _airspace(
                    "unknown", parent_grid_id, None, "parent_not_confirmed_allowed",
                )
                continue
            covering = next(
                (entry for entry in self.allowed_entries
                 if rect_covered_by_any([entry["polygon"]], rectangle)), None,
            )
            if covering is None:
                result[fine_cell_id] = _airspace(
                    "unknown", parent_grid_id, None,
                    "fine_cell_polygon_not_fully_covered_by_a_confirmed_allowed_polygon",
                )
            else:
                result[fine_cell_id] = _airspace(
                    "confirmed_allowed", parent_grid_id, covering.get("feature_id"), None,
                )
        return result

    def _cell_rectangle(self, cell, transform):
        """The fine cell's polygon as a conservative densified lon/lat rectangle."""

        center = cell.get("center_metric")
        size = cell.get("cell_size_m")
        if center is None or not size:
            bbox = cell.get("bbox_metric")
            if not bbox:
                return None
            center = [(bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0]
            size = min(bbox[2] - bbox[0], bbox[3] - bbox[1])
        half = float(size) / 2.0
        west, south = center[0] - half, center[1] - half
        east, north = center[0] + half, center[1] + half
        steps = self.ring_densification
        metric_ring = []
        for index in range(steps):
            ratio = index / float(steps)
            metric_ring.append([west + (east - west) * ratio, south])
        for index in range(steps):
            ratio = index / float(steps)
            metric_ring.append([east, south + (north - south) * ratio])
        for index in range(steps):
            ratio = index / float(steps)
            metric_ring.append([east - (east - west) * ratio, north])
        for index in range(steps):
            ratio = index / float(steps)
            metric_ring.append([west, north - (north - south) * ratio])
        metric_ring.append(list(metric_ring[0]))
        geographic = []
        for point in metric_ring:
            converted = transform.to_geographic(point)
            if converted is None:
                return None
            geographic.append([float(converted[0]), float(converted[1])])
        return geographic


def _altitude_interval(feature):
    """Confirmed lower/upper altitude evidence of an airspace feature, else ``None``."""

    lower = feature.get("lower_altitude_egm2008_m", feature.get("lower_altitude_m"))
    upper = feature.get("upper_altitude_egm2008_m", feature.get("upper_altitude_m"))
    if lower is None and upper is None:
        return None
    values = {}
    for name, value in (("lower", lower), ("upper", upper)):
        if value in (None, ""):
            values[name] = None
            continue
        try:
            values[name] = float(value)
        except (TypeError, ValueError):
            return None
    if not feature.get("altitude_confirmed"):
        return None
    return {
        "lower_altitude_egm2008_m": values["lower"],
        "upper_altitude_egm2008_m": values["upper"],
        "vertical_reference": "egm2008_orthometric",
        "confirmed": True,
        "source": feature.get("altitude_source") or feature.get("policy_source"),
    }


def _metric_ring(ring, transform):
    if not ring:
        return None
    items = []
    for point in ring:
        converted = transform.to_metric([float(point[0]), float(point[1])])
        if converted is None:
            return None
        items.append([float(converted[0]), float(converted[1])])
    return items if len(items) >= 4 else None


def _airspace(status, parent_grid_id, feature_id, reason):
    return {
        "status": status,
        "feature_id": feature_id,
        "mapping_method": AIRSPACE_MAPPING_METHOD,
        "parent_grid_id": parent_grid_id,
        "policy_confirmed": status in ("confirmed_allowed", "confirmed_restricted"),
        "query_mode": AIRSPACE_POLYGON_QUERY_MODE,
        "reason": reason,
        "semantics": (
            "confirmed_policy_fine_cell_polygon_coverage_test_mixed_or_boundary_is_unknown_"
            "v3c_route_level_continuous_validation_pending"
        ),
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
            "effective_source_resolution_detail": resolution.get("effective_source_resolution_detail") or {},
            "semantics": "fine_horizontal_resolution_must_be_explicit_or_dtm_effective_never_a_30m_constant",
            "unit_semantics": (
                "native_geotransform_pixel_size_converted_through_verified_crs_linear_unit_"
                "or_measured_geodesically_never_degree_as_metre"
            ),
        }
        entries["metric_frame"] = {
            "role": "metric_frame", "status": "ready" if self.transform else "blocked",
            "reasons": [] if self.transform else ["metric_transform_unavailable"],
            "describe": self.transform.describe() if self.transform else None,
        }
        return entries

    def _resolve_resolution(self):
        detail = None
        effective = None
        if self.terrain_source is not None and hasattr(self.terrain_source, "effective_resolution_detail"):
            detail = self.terrain_source.effective_resolution_detail()
            effective = detail.get("effective_resolution_m")
        elif self.terrain_source is not None and hasattr(self.terrain_source, "effective_resolution_m"):
            effective = self.terrain_source.effective_resolution_m()
        provenance = {} if detail is None else {
            "native_pixel_size_x": detail.get("native_pixel_size_x"),
            "native_pixel_size_y": detail.get("native_pixel_size_y"),
            "native_pixel_size_unit": detail.get("native_pixel_size_unit"),
            "effective_resolution_m_x": detail.get("effective_resolution_m_x"),
            "effective_resolution_m_y": detail.get("effective_resolution_m_y"),
            "method": detail.get("method"),
            "reference_location": detail.get("reference_location"),
            "geodesic_backend": detail.get("geodesic_backend"),
            "degree_values_never_reported_as_metres": True,
        }
        if self.resolution_source == "explicit_configuration" and self.resolution_m:
            return {
                "resolution_m": float(self.resolution_m),
                "resolution_source": "explicit_configuration",
                "requested_resolution_m": float(self.resolution_m),
                "effective_source_resolution_m": effective,
                "effective_source_resolution_detail": provenance,
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
                    "effective_source_resolution_detail": provenance,
                    "reason": None,
                }
        return {
            "resolution_m": None,
            "resolution_source": None,
            "requested_resolution_m": self.resolution_m,
            "effective_source_resolution_m": effective,
            "effective_source_resolution_detail": provenance,
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
        # Terrain and buildings are required hard facts. Airspace is display-only.
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
        airspace = {
            str(cell["fine_cell_id"]): {
                "status": "not_applicable", "applicability": "display_only",
                "parent_grid_id": binding.get(str(cell["fine_cell_id"])),
                "mapping_method": "display_only_reference_layer_not_used_for_planning",
                "policy_confirmed": False,
            }
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
            "terrain_dtm": readiness["terrain_dtm"].get("describe"),
            "buildings": readiness["buildings"].get("describe"),
            "airspace": {"status": "not_applicable", "applicability": "display_only"},
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


# --------------------------------------------------------------------------- V3-C

V3C_ADAPTER_ID = "v3c_continuous_validation_adapter"
V3C_ADAPTER_VERSION = "3.2-alpha"

#: V3-C reads the **native** raster window (never a resampled or reduced copy).
V3C_TERRAIN_READ_MODE = "single_read_only_native_window_no_resample_per_request"
#: V3-C queries buildings only inside the route bbox plus the explicit clearance.
V3C_BUILDING_QUERY_MODE = "route_bbox_plus_clearance_provider_spatial_index_rtree"


class NativeTerrainWindowSource(_FabdemRasterBase):
    """Read-only **native** FABDEM window for V3-C pixel-level validation.

    The V3-B terrain floor was a fine-cell aggregate.  V3-C must not substitute that
    aggregate for the final terrain verdict, so this class hands the validator every
    native pixel the realized route touches, with its own source value, NoData status
    and CRS -- no resampling, no interpolation and no NoData filling.
    """

    def describe(self):
        stat = self.path.stat()
        return {
            "role": self.role, "dataset": "FABDEM", "file_name": self.path.name,
            "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            "width": int(self.dataset.RasterXSize), "height": int(self.dataset.RasterYSize),
            "nodata": self.nodata,
            "vertical_reference": self.vertical_reference,
            "vertical_status": self.vertical_status,
            "read_mode": V3C_TERRAIN_READ_MODE,
            "source_type": "real_sources",
        }

    def usable(self):
        if self.vertical_status != "confirmed":
            return False, "terrain_vertical_datum_unresolved"
        return True, None

    def native_window(self, *, transform, metric_line, envelope_radius_m, spacing_m):
        """Every native pixel the route envelope touches, with its source value.

        The window is a **single** read-only ``ReadAsArray`` over the union pixel
        rectangle; each touched pixel keeps its own value, NoData flag and the CRS of
        the dataset.  NoData is reported as ``unknown`` -- never filled, never zero.
        """

        if self.vertical_status != "confirmed":
            return {
                "available": False, "reason": "terrain_vertical_datum_unresolved",
                "pixels": [], "source": self.describe(),
            }
        window = self._window_for_line(transform, metric_line, envelope_radius_m)
        if window is None:
            return {
                "available": False, "reason": "route_outside_dtm_coverage",
                "pixels": [], "source": self.describe(),
            }
        x0, y0, x1, y1 = window
        array = self.band.ReadAsArray(x0, y0, x1 - x0, y1 - y0)
        if array is None:
            return {
                "available": False, "reason": "dtm_read_failed",
                "pixels": [], "source": self.describe(),
            }
        rows = array.tolist() if hasattr(array, "tolist") else array
        scale = self.band.GetScale()
        offset = self.band.GetOffset()
        scale = 1.0 if scale is None else float(scale)
        offset = 0.0 if offset is None else float(offset)
        nodata = self.nodata
        pixels = []
        for row_index in range(y1 - y0):
            for column_index in range(x1 - x0):
                raw = float(rows[row_index][column_index])
                column, row = x0 + column_index, y0 + row_index
                value = raw * scale + offset
                is_nodata = (
                    not _finite_number(value)
                    or (nodata is not None and raw == float(nodata))
                )
                corners = self._pixel_metric_bbox(transform, column, row)
                if corners is None:
                    continue
                center_geographic = self._pixel_geographic(column, row)
                center_metric = (
                    transform.to_metric(center_geographic) if center_geographic else None
                )
                pixels.append({
                    "pixel": [column, row],
                    "bbox_metric": corners,
                    "center_metric": center_metric,
                    "data_status": "unknown" if is_nodata else "passed",
                    "elevation_egm2008_m": None if is_nodata else value,
                    "source_value": None if is_nodata else value,
                    "reason": (
                        "source_pixel_nodata_never_filled_never_zeroed" if is_nodata else None
                    ),
                })
        self.last_window = {
            "window_pixels": [x0, y0, x1 - x0, y1 - y0],
            "raster_pixels": int(self.dataset.RasterXSize) * int(self.dataset.RasterYSize),
            "read_mode": V3C_TERRAIN_READ_MODE,
            "resampled": False,
            "windowed_read_only": True,
            "native_pixel_count": len(pixels),
            "no_data_filling": False,
        }
        return {
            "available": True,
            "reason": None,
            "pixels": pixels,
            "source": {
                **self.describe(),
                "crs": self._crs_authority(),
                "window": self.last_window,
                "spacing_m": spacing_m,
                "envelope_radius_m": envelope_radius_m,
            },
        }

    # ------------------------------------------------------------------ internals

    def _crs_authority(self):
        try:
            crs = self._raster_crs()
            if crs is None:
                return None
            authority = crs.GetAuthorityName(None)
            code = crs.GetAuthorityCode(None)
            return f"{authority}:{code}" if authority and code else None
        except (AttributeError, TypeError, RuntimeError):
            return None

    def _pixel_geographic(self, column, row):
        if getattr(self, "_to_geographic", None) is None:
            target = self.osr.SpatialReference()
            target.ImportFromEPSG(4326)
            source = self.osr.SpatialReference()
            try:
                source.ImportFromWkt(self.projection)
            except (TypeError, RuntimeError):
                self._to_geographic = False
                return None
            for item in (source, target):
                if hasattr(item, "SetAxisMappingStrategy"):
                    item.SetAxisMappingStrategy(self.osr.OAMS_TRADITIONAL_GIS_ORDER)
            self._to_geographic = self.osr.CoordinateTransformation(source, target)
        if self._to_geographic is False:
            return None
        transform = self.dataset.GetGeoTransform()
        x = transform[0] + transform[1] * (column + 0.5) + transform[2] * (row + 0.5)
        y = transform[3] + transform[4] * (column + 0.5) + transform[5] * (row + 0.5)
        try:
            point = self._to_geographic.TransformPoint(x, y)
        except (TypeError, RuntimeError):
            return None
        return [float(point[0]), float(point[1])]

    def _pixel_metric_bbox(self, transform, column, row):
        corners = []
        for x, y in ((column, row), (column + 1, row), (column + 1, row + 1), (column, row + 1)):
            geographic = self._pixel_geographic(x, y)
            if geographic is None:
                return None
            metric = transform.to_metric(geographic)
            if metric is None:
                return None
            corners.append([float(metric[0]), float(metric[1])])
        return [
            min(point[0] for point in corners), min(point[1] for point in corners),
            max(point[0] for point in corners), max(point[1] for point in corners),
        ]

    def _window_for_line(self, transform, metric_line, envelope_radius_m):
        pixels = []
        for point in metric_line or []:
            geographic = transform.to_geographic([float(point[0]), float(point[1])])
            if geographic is None:
                return None
            raster_crs = self._transform_to_raster_crs(geographic)
            if raster_crs is None:
                return None
            pixel, line = (
                self.inverse[0] + self.inverse[1] * raster_crs[0] + self.inverse[2] * raster_crs[1],
                self.inverse[3] + self.inverse[4] * raster_crs[0] + self.inverse[5] * raster_crs[1],
            )
            if pixel != pixel or line != line:
                return None
            pixels.append((pixel, line))
        if not pixels:
            return None
        margin = max(1.0, float(envelope_radius_m) / max(1e-9, self.effective_resolution_m() or 1.0))
        x0 = max(0, int(floor(min(item[0] for item in pixels) - margin)))
        y0 = max(0, int(floor(min(item[1] for item in pixels) - margin)))
        x1 = min(int(self.dataset.RasterXSize), int(floor(max(item[0] for item in pixels) + margin)) + 1)
        y1 = min(int(self.dataset.RasterYSize), int(floor(max(item[1] for item in pixels) + margin)) + 1)
        if x1 <= x0 or y1 <= y0:
            return None
        return x0, y0, x1, y1


class ConfirmedAirspacePolicySource:
    """The confirmed ``AirspacePolicy`` polygons as V3-C route-level metric evidence."""

    role = "airspace_policy"

    def __init__(self, eligibility):
        self.eligibility = eligibility if isinstance(eligibility, dict) else {}
        self.allowed, self.blocked, self.unconfirmed = [], [], []
        for feature in self.eligibility.get("features") or []:
            if not isinstance(feature, dict):
                continue
            polygons = polygons_of(feature.get("geometry"))
            if not polygons:
                continue
            confirmed = feature.get("policy_confirmed") is True
            bucket = None
            if confirmed and feature.get("route_eligibility") == "allowed":
                bucket = self.allowed
            elif confirmed and feature.get("route_eligibility") == "blocked":
                bucket = self.blocked
            else:
                bucket = self.unconfirmed
            for polygon in polygons:
                bucket.append({
                    "feature_id": feature.get("feature_id"),
                    "ring_geographic": polygon[0],
                    "holes_geographic": polygon[1],
                    "source": feature.get("policy_source"),
                })

    def usable(self):
        if not self.allowed:
            return False, "airspace_policy_unavailable"
        return True, None

    def describe(self):
        return {
            "role": self.role,
            "query_mode": "confirmed_policy_route_level_metric_envelope_coverage_and_blocked_disjointness",
            "eligibility_status": self.eligibility.get("status"),
            "algorithm_id": self.eligibility.get("algorithm_id"),
            "algorithm_version": self.eligibility.get("algorithm_version"),
            "feature_fingerprint": self.eligibility.get("feature_fingerprint"),
            "policy_fingerprint": self.eligibility.get("policy_fingerprint"),
            "fingerprint": self.eligibility.get("fingerprint"),
            "confirmed_allowed_polygon_count": len(self.allowed),
            "confirmed_blocked_polygon_count": len(self.blocked),
            "unconfirmed_polygon_count": len(self.unconfirmed),
            "inferred_from_name_or_color": False,
            "final_route_level_validation": "V3-C",
        }

    def metric_evidence(self, transform):
        return {
            "confirmed": self.eligibility.get("status") == "passed",
            "allowed": [_metric_entry(entry, transform) for entry in self.allowed],
            "blocked": [_metric_entry(entry, transform) for entry in self.blocked],
            "unconfirmed": [_metric_entry(entry, transform) for entry in self.unconfirmed],
            "source": self.describe(),
            "semantics": (
                "confirmed_policy_polygons_in_the_local_metric_frame_route_level_envelope_validation"
            ),
        }


class RouteCorridorBuildingSource:
    """Corridor-only building query for the V3-C building validator.

    Reuses the existing GeoPackage + provider spatial index (RTree) and the existing
    roof semantics (``ground + height``); V3-C does not invent a third roof formula and
    never modifies the source geometry.
    """

    role = "buildings"

    def __init__(self, path, *, layer_name="buildings", height_field="height_m",
                 height_status_field="height_status", crs_authority="EPSG:32651", gdal=None):
        from qgis.core import QgsCoordinateReferenceSystem, QgsVectorLayer

        self.path = Path(path).expanduser().resolve()
        if not self.path.is_file():
            raise ValueError("buildings 必须是存在的 GeoPackage 文件")
        self.layer_name = str(layer_name)
        self.layer = QgsVectorLayer(
            f"{self.path}|layername={self.layer_name}", "V3-C buildings", "ogr",
        )
        if not self.layer.isValid():
            raise ValueError(f"无法读取 buildings GeoPackage 图层 {self.layer_name}")
        fields = {field.name() for field in self.layer.fields()}
        if height_field not in fields:
            raise ValueError(f"buildings 图层缺少高度字段 {height_field}")
        self.height_field = height_field
        self.height_status_field = height_status_field if height_status_field in fields else None
        self.metric = QgsCoordinateReferenceSystem(str(crs_authority))
        self._to_metric = None

    def usable(self):
        try:
            indexed = bool(self.layer.hasSpatialIndex())
        except (AttributeError, TypeError):
            indexed = False
        if not indexed:
            return False, "building_source_spatial_index_missing"
        return True, None

    def describe(self):
        stat = self.path.stat()
        extent = self.layer.extent()
        return {
            "role": self.role, "file_name": self.path.name, "layer": self.layer_name,
            "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            "feature_count": int(self.layer.featureCount()),
            "crs": self.layer.crs().authid() if self.layer.crs().isValid() else None,
            "extent": [extent.xMinimum(), extent.yMinimum(), extent.xMaximum(), extent.yMaximum()],
            "spatial_index_available": bool(self.layer.hasSpatialIndex()),
            "query_mode": V3C_BUILDING_QUERY_MODE,
            "height_field": self.height_field,
            "height_status_field": self.height_status_field,
            "roof_semantics": "shared_building_clearance_roof_elevation_helper",
            "source_geometry_modified": False,
        }

    def query_route(self, route, *, transform, horizontal_clearance_m, curve_error_m,
                    terrain_source=None):
        from qgis.core import (
            QgsCoordinateTransform, QgsFeatureRequest, QgsGeometry, QgsProject,
            QgsRectangle,
        )

        if self._to_metric is None:
            context = QgsProject.instance().transformContext()
            self._to_metric = QgsCoordinateTransform(self.layer.crs(), self.metric, context)
        envelope = float(horizontal_clearance_m) + float(curve_error_m)
        metric_points = [
            [float(point[0]), float(point[1])]
            for point in ((route or {}).get("horizontal_geometry") or {}).get("linearized", {}).get(
                "linestring_metric",
            ) or []
        ]
        if len(metric_points) < 2:
            return {"available": False, "reason": "route_geometry_unresolved", "buildings": []}
        west = min(point[0] for point in metric_points) - envelope
        south = min(point[1] for point in metric_points) - envelope
        east = max(point[0] for point in metric_points) + envelope
        north = max(point[1] for point in metric_points) + envelope
        corners = [transform.to_geographic([west, south]), transform.to_geographic([east, north])]
        if any(item is None for item in corners):
            return {"available": False, "reason": "metric_transform_unavailable", "buildings": []}
        rectangle = QgsRectangle(
            min(corners[0][0], corners[1][0]), min(corners[0][1], corners[1][1]),
            max(corners[0][0], corners[1][0]), max(corners[0][1], corners[1][1]),
        )
        request = QgsFeatureRequest().setFilterRect(rectangle)
        buildings = []
        for feature in self.layer.getFeatures(request):
            geometry = feature.geometry()
            if geometry is None or geometry.isEmpty():
                continue
            metric = QgsGeometry(geometry)
            metric.transform(self._to_metric)
            ring = _metric_ring_of(metric)
            if len(ring) < 3:
                continue
            height = _number_or_none(feature[self.height_field])
            status = (
                str(feature[self.height_status_field] or "")
                if self.height_status_field else ("predicted" if height is not None else "unknown")
            )
            ground = None
            if terrain_source is not None and hasattr(terrain_source, "sample_footprint_ground"):
                ground = terrain_source.sample_footprint_ground(ring, transform=transform)
            buildings.append({
                "building_id": _feature_identifier(feature, self.layer),
                "source": str(feature["source"]) if "source" in {f.name() for f in self.layer.fields()} else "unknown",
                "ring_metric": ring,
                "height_m": height,
                "height_status": status or ("predicted" if height is not None else "unknown"),
                "ground_elevation_max_egm2008_m": ground,
            })
        return {
            "available": True,
            "reason": None,
            "buildings": buildings,
            "source": {
                **self.describe(),
                "query_bbox_geographic": [
                    rectangle.xMinimum(), rectangle.yMinimum(),
                    rectangle.xMaximum(), rectangle.yMaximum(),
                ],
                "envelope_m": envelope,
            },
        }


def _metric_entry(entry, transform):
    ring = _metric_ring(entry["ring_geographic"], transform)
    holes = [_metric_ring(hole, transform) for hole in entry.get("holes_geographic") or []]
    return {
        "feature_id": entry.get("feature_id"),
        "outer_metric": ring,
        "holes_metric": [hole for hole in holes if hole],
        "altitude_interval": None,
        "altitude_evidence": "not_provided_by_the_confirmed_policy_source",
        "source": entry.get("source"),
    }


def _metric_ring_of(geometry):
    try:
        polygon = geometry.asPolygon()
    except (AttributeError, TypeError):
        polygon = None
    if not polygon:
        return []
    return [[float(point.x()), float(point.y())] for point in polygon[0]]


def _feature_identifier(feature, layer):
    try:
        if "id" in {field.name() for field in layer.fields()}:
            return str(feature["id"])
    except (AttributeError, TypeError, KeyError):
        pass
    return str(feature.id())


def _finite_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return number == number and number not in (float("inf"), float("-inf"))


def real_data_source_readiness(
    paths, *, airspace_eligibility=None, policy_confirmed=False,
    source_audits=None, terrain_profile=None,
):

    terrain_path = paths.get("terrain_dtm")
    buildings_path = paths.get("buildings")
    missing = []
    if not terrain_path or not Path(terrain_path).is_file():
        missing.append("terrain_dtm_not_configured_or_missing")
    if not buildings_path or not Path(buildings_path).is_file():
        missing.append("buildings_geopackage_not_configured_or_missing")
    if source_audits is not None:
        items = (source_audits or {}).get("items") or {}
        for role in ("terrain_dtm", "buildings", "building_grid"):
            if (items.get(role) or {}).get("status") != "verified":
                missing.append(f"{role}_source_not_verified")
    if terrain_profile is not None:
        profile = terrain_profile or {}
        observed = ((profile.get("crs") or {}).get("observed_vertical") or "")
        if str(observed).lower() != "egm2008_orthometric":
            missing.append("terrain_dtm_vertical_reference_not_verified_egm2008_orthometric")
        if (profile.get("verification") or {}).get("status") != "verified_from_raster_metadata":
            missing.append("terrain_dtm_metadata_verification_missing")
    if not policy_confirmed:
        missing.append("v3_policy_not_confirmed")
    return {
        "status": "blocked" if missing else "ready",
        "adapter_id": ADAPTER_ID,
        "adapter_version": ADAPTER_VERSION,
        "roles": list(FINE_SOURCE_ROLES),
        "terrain_dtm": _basename(terrain_path),
        "buildings": _basename(buildings_path),
        "airspace": {"status": "not_applicable", "applicability": "display_only"},
        "blocking_reasons": missing,
        "resolution_policy": "explicit_configuration_or_dtm_effective_resolution_never_a_30m_constant",
        "required_before_real_run": [
            "FABDEM terrain_dtm with confirmed egm2008_orthometric vertical reference",
            "buildings GeoPackage with a provider spatial index (RTree) and a height field",
            "explicit confirmed V3 planning policy",
        ],
        "semantics": "readiness_report_only_no_data_read_no_fabricated_environment",
    }


__all__ = [
    "ADAPTER_BLOCK_REASONS", "ADAPTER_ID", "ADAPTER_VERSION", "AIRSPACE_POLYGON_QUERY_MODE",
    "AIRSPACE_QUERY_MODE", "BUILDING_QUERY_MODE", "ConfirmedAirspacePolygonSource",
    "ConfirmedAirspacePolicySource", "EquirectangularMetricTransform", "FINE_SOURCE_ROLES",
    "FabdemWindowTerrainSource", "FineEnvironmentAdapter", "NativeTerrainWindowSource",
    "QgisGpkgBuildingSource", "QgisMetricTransform", "RESOLUTION_METHODS",
    "RouteCorridorBuildingSource", "TERRAIN_READ_MODE", "V3C_ADAPTER_ID",
    "V3C_ADAPTER_VERSION", "V3C_BUILDING_QUERY_MODE", "V3C_TERRAIN_READ_MODE",
    "real_data_source_readiness",
]
