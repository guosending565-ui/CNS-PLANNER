"""Product contracts for the currently configured population and terrain sources."""

from copy import deepcopy

from ..domain.provenance import source_profile


WORLDPOP_R2025A = source_profile({
    "source_id": "worldpop-r2025a-population-count",
    "name": "WorldPop Population Counts R2025A",
    "source_type": "real",
    "version": "R2025A",
    "quantity": "population_count_per_source_pixel",
    "unit": "person/source_pixel",
    "resolution": {"angular_value": 3, "angular_unit": "arc-second", "nominal": "100 m"},
    "crs": {"horizontal": "EPSG:4326", "name": "WGS84"},
    "verification": {
        "status": "verified_product_contract",
        "reference": "WorldPop Population Counts, DOI 10.5258/SOTON/WP00839",
        "file_identity": "configured_assumption",
    },
    "provenance": {"publisher": "WorldPop", "product_status": "alpha"},
})

COPERNICUS_GLO30 = source_profile({
    "source_id": "copernicus-dem-glo30",
    "name": "Copernicus DEM GLO-30",
    "source_type": "real",
    "version": "GLO-30",
    "quantity": "surface_elevation",
    "unit": "m",
    "resolution": {"angular_value": 1, "angular_unit": "arc-second"},
    "crs": {
        "horizontal": "EPSG:4326", "horizontal_name": "WGS84-G1150",
        "vertical": "EPSG:3855", "vertical_name": "EGM2008",
    },
    "verification": {"status": "verified_product_contract", "file_identity": "configured_assumption"},
    "provenance": {"publisher": "Copernicus", "surface_model": "DSM"},
})

FABDEM_V12 = source_profile({
    "source_id": "fabdem-v1.2-dtm",
    "name": "FABDEM V1.2 bare-earth DTM",
    "source_type": "real",
    "version": "V1.2",
    "quantity": "bare_earth_elevation",
    "unit": "m",
    "resolution": {"angular_value": 1, "angular_unit": "arc-second", "nominal": "30 m"},
    "crs": {
        "horizontal": "EPSG:4326", "horizontal_name": "WGS84",
        "vertical": "EGM2008_orthometric", "vertical_name": "EGM2008 orthometric height",
    },
    "verification": {
        "status": "verified_from_configured_raster_metadata",
        "file_identity": "runtime_metadata_required",
    },
    "provenance": {"publisher": "FABDEM", "surface_model": "bare_earth_DTM"},
})


def default_source_profiles():
    return {
        "population": deepcopy(WORLDPOP_R2025A),
        "terrain": deepcopy(COPERNICUS_GLO30),
        "terrain_dtm": deepcopy(FABDEM_V12),
    }
