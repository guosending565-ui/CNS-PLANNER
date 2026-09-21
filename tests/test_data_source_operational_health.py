"""真实数据源健康检查：样例 / 阶段性验证数据永远不能报 ready。

背景
----
``projects/map_sources.json`` 一度把人口栅格指向 ``outputs/stage_validation``
下的阶段性样例瓦片（文件自带 ``CNS_NOT_OPERATIONAL_DATA=true`` 与
``CNS_STAGE_ASSUMPTION`` 标记，且只有 63 × 190 像元）。文件可读并不代表它是运行数据，
健康检查必须拒绝把它报成 ``ready``。

本测试只覆盖健康检查本身的判定，不读取任何外部大文件。
"""

from cns_planner.data.health import (
    MIN_POPULATION_PIXELS, build_health, non_operational_markers,
    raster_non_operational_reasons,
)

STAGE_SAMPLE_METADATA = {
    "CNS_NOT_OPERATIONAL_DATA": "true",
    "CNS_STAGE_ASSUMPTION": "population_source_nodata_treated_as_zero_for_R0003_stage_validation_only",
}


def _metadata(**population):
    return {
        "paths": {"basemap": "map.qgz", "population": "population.tif"},
        "layers": [{"id": "air", "name": "airspace"}],
        "error": "",
        "population": {
            "width": 73530, "height": 45337, "bands": 1, "crs": "EPSG:4326",
            "nodata": "-99999.0", "source_metadata": {"Description": "CHN population 2025"},
            **population,
        },
    }


def _population_item(metadata):
    return {item["id"]: item for item in build_health(metadata)["items"]}["population"]


def test_stage_validation_dataset_is_never_reported_ready():
    metadata = _metadata(
        width=63, height=190, source_metadata=STAGE_SAMPLE_METADATA,
    )
    item = _population_item(metadata)
    assert item["status"] == "warning"
    assert "不是运行数据" in item["message"]
    assert "CNS_NOT_OPERATIONAL_DATA=true" in item["message"]
    check = next(check for check in item["checks"] if check["name"] == "运行数据（非样例）")
    assert check["status"] == "failed"


def test_readable_but_tiny_population_tile_is_not_ready():
    metadata = _metadata(width=63, height=190, source_metadata={})
    item = _population_item(metadata)
    assert item["status"] == "warning"
    assert "样例瓦片" in item["message"]
    width, height = 63, 190
    assert width * height < MIN_POPULATION_PIXELS


def test_real_population_raster_stays_ready():
    metadata = _metadata()
    item = _population_item(metadata)
    assert item["status"] == "ready"
    check = next(check for check in item["checks"] if check["name"] == "运行数据（非样例）")
    assert check["status"] == "passed"


def test_marker_helpers_are_pure_read_only_projections():
    assert non_operational_markers(None) == []
    assert non_operational_markers({"Description": "x"}) == []
    assert non_operational_markers(STAGE_SAMPLE_METADATA) == [
        "CNS_NOT_OPERATIONAL_DATA=true",
        "CNS_STAGE_ASSUMPTION=population_source_nodata_treated_as_zero_for_R0003_stage_validation_only",
    ]
    assert raster_non_operational_reasons("population", {
        "width": 63, "height": 190, "source_metadata": STAGE_SAMPLE_METADATA,
    })
    assert raster_non_operational_reasons("terrain", {"width": 63, "height": 190}) == []
