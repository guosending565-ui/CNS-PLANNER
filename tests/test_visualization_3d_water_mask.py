"""``LandMaskWaterMaskSource`` 的语义与失败关闭测试（Round37-T3D-BMAP）。

重点不是"能跑"，而是三条不可退让的语义：

1. **语义反转**：陆域掩膜是 land-truthy，heightmap water mask 是 water-truthy，
   必须取反；搞反就是把海洋反光刷到陆地上。
2. **不确定按陆地**：``coastal_uncertain`` 与 ``unknown`` 绝不能被当成水面。
3. **覆盖范围守卫**：数据覆盖范围之外是"未知"，不是"海"（Round35-A D-01）。
"""

import pytest

from cns_planner.gis.water_mask_adapter import (
    LandMaskWaterMaskSource,
    WaterMaskUnavailable,
    build_water_mask_source,
)


class FakeLandMask:
    """可控的陆域掩膜替身：``land_west_of`` 以西为陆地，以东为水面。"""

    def __init__(self, *, land_west_of=122.0, footprint=(120.0, 28.0, 124.0, 32.0),
                 surfaces=None, coastal_buffer_m=0.0):
        self.land_west_of = land_west_of
        self.footprint = footprint
        self.surfaces = surfaces or {}
        self.data_footprint_margin_m = 0.0
        self.coastal_uncertainty_buffer_m = coastal_buffer_m
        self.prepared = True

    def to_metric(self, lon, lat):
        # 简化的等距投影：经度/纬度 -> 米（仅用于测试判定，不追求精度）
        return (float(lon) * 100000.0, float(lat) * 111000.0)

    def footprint_bounds(self):
        if self.footprint is None:
            return None
        west, south, east, north = self.footprint
        return (west * 100000.0, south * 111000.0, east * 100000.0, north * 111000.0)

    def prepare(self):
        # 陆地多边形：经度 <= land_west_of 的整条带状区域
        from shapely.geometry import box
        return [(box(-180.0 * 100000.0, -90.0 * 111000.0,
                     self.land_west_of * 100000.0, 90.0 * 111000.0), None)]


def window(source, bbox=(121.9, 29.9, 122.1, 30.1), width=8, height=8):
    return source.window(bbox, width, height)


def test_land_is_one_and_sea_is_zero():
    """核心语义：陆 -> 1，海 -> 0，绝不反转。"""

    source = LandMaskWaterMaskSource(FakeLandMask(land_west_of=122.0))
    result = window(source, bbox=(121.95, 29.95, 122.05, 30.05), width=8, height=8)
    rows = result["values"]
    assert len(rows) == 8 and all(len(row) == 8 for row in rows)
    flat = [value for row in rows for value in row]
    assert set(flat) <= {0, 1, None}
    # 西侧应为陆地(1)，东侧应为水面(0)
    assert rows[0][0] == 1, "最西侧列必须是陆地"
    assert rows[0][-1] == 0, "最东侧列必须是水面"


def test_coastal_uncertainty_band_is_not_painted_as_sea():
    """海岸不确定带必须真实生效并按陆地处理（fail toward land）。

    回归背景：``land_mask`` 的权威常量 ``LAND_MASK_SEMANTICS`` 声明的是
    "显式多边形包含 **+ 海岸不确定带**"，但本适配器早期只做 ``contains_xy``，
    缓冲带内的点因此落进"明确水面"分支——海岸带（滩涂、堤坝、港池边缘）会被
    刷上海面反光，与模块自身的 fail-toward-land 承诺正好相反。
    """

    mask = FakeLandMask(land_west_of=122.0, coastal_buffer_m=3000.0)
    source = LandMaskWaterMaskSource(mask)
    result = window(source, bbox=(121.95, 29.95, 122.05, 30.05), width=8, height=8)
    row = result["values"][0]
    # 列经度：121.950 / 122.007 / 122.021 / 122.036 / 122.050
    # 3000 m 缓冲带（经度方向 0.03°）= 122.000 – 122.030
    assert row[0] == 1, "缓冲带以西仍是明确陆地"
    assert row[4] is None, "缓冲带内（122.007）必须是不确定，不得判成水面"
    assert row[5] is None, "缓冲带内（122.021）必须是不确定，不得判成水面"
    assert row[6] == 0, "缓冲带之外（122.036）才是明确水面"
    assert row[7] == 0, "缓冲带之外（122.050）才是明确水面"
    assert result["coastal_uncertainty_buffer_m"] == 3000.0
    assert result["coastal_uncertain_cells"] == 2 * 8, (
        "每行 2 个缓冲带像元，共 8 行")
    assert result["coastal_is_water"] is False


def test_coastal_band_is_water_only_when_explicitly_requested():
    """``coastal_is_water`` 是有效开关，而不是装饰性参数。"""

    mask = FakeLandMask(land_west_of=122.0, coastal_buffer_m=3000.0)
    source = LandMaskWaterMaskSource(mask, coastal_is_water=True)
    result = window(source, bbox=(121.95, 29.95, 122.05, 30.05), width=8, height=8)
    row = result["values"][0]
    flat = [v for r in result["values"] for v in r]
    assert None not in flat, "显式要求海岸带算水面时，不应再产生不确定值"
    assert row[4] == 0 and row[5] == 0
    assert result["coastal_is_water"] is True


def test_zero_coastal_buffer_keeps_pure_polygon_semantics():
    """显式 0 m 缓冲必须回到纯多边形语义（不引入隐式保守带）。"""

    mask = FakeLandMask(land_west_of=122.0, coastal_buffer_m=0.0)
    source = LandMaskWaterMaskSource(mask)
    result = window(source, bbox=(121.95, 29.95, 122.05, 30.05), width=8, height=8)
    row = result["values"][0]
    assert row[4] == 0, "无缓冲时 122.007 就是明确水面"
    assert result["coastal_uncertain_cells"] == 0


def test_row_order_is_north_to_south():
    """行序必须与地形窗口一致（北->南），否则水 mask 会上下翻转。"""

    result = window(LandMaskWaterMaskSource(FakeLandMask()), width=4, height=4)
    assert result["row_order"] == "north_to_south"


def test_outside_footprint_is_uncertain_never_sea():
    """覆盖范围之外必须是 None（不确定），绝不能判成水面。"""

    # footprint 只覆盖很小一块，查询 bbox 远大于它
    mask = FakeLandMask(footprint=(122.0, 30.0, 122.01, 30.01))
    source = LandMaskWaterMaskSource(mask)
    result = window(source, bbox=(121.0, 29.0, 123.0, 31.0), width=8, height=8)
    flat = [value for row in result["values"] for value in row]
    assert None in flat, "范围外必须产生不确定值"
    assert result["footprint_guard"] is True
    assert result["footprint_outside_is_unknown_not_sea"] is True


def test_unknown_footprint_yields_all_uncertain_not_sea():
    """覆盖范围未知时无法证明"范围外是海"，必须全部按不确定处理。"""

    source = LandMaskWaterMaskSource(FakeLandMask(footprint=None))
    result = window(source, width=4, height=4)
    flat = [value for row in result["values"] for value in row]
    assert set(flat) == {None}
    assert result["footprint_guard"] is False


def test_none_values_are_reported_as_uncertain_semantics():
    result = window(LandMaskWaterMaskSource(FakeLandMask()), width=4, height=4)
    assert "uncertain" in result["semantics"]
    assert result["source_modified"] is False


def test_bbox_and_size_guards_reject_invalid_input():
    source = LandMaskWaterMaskSource(FakeLandMask())
    with pytest.raises(WaterMaskUnavailable):
        source.window((122.1, 30.0, 121.9, 30.1), 8, 8)   # west >= east
    with pytest.raises(WaterMaskUnavailable):
        source.window((121.9, 30.0, 122.1, 30.1), 0, 8)    # width 为 0
    with pytest.raises(WaterMaskUnavailable):
        source.window((121.9, 30.0, 122.1, 30.1), 5000, 8)  # 超出上限


def test_missing_geometry_fails_closed():
    class NoGeometry:
        data_footprint_margin_m = 0.0

        def to_metric(self, lon, lat):
            return (lon, lat)

        def footprint_bounds(self):
            return None

        def prepare(self):
            return None

    source = LandMaskWaterMaskSource(NoGeometry())
    with pytest.raises(WaterMaskUnavailable):
        window(source)


def test_none_source_is_rejected_at_construction():
    with pytest.raises(WaterMaskUnavailable):
        LandMaskWaterMaskSource(None)


def test_build_water_mask_source_reports_error_instead_of_raising():
    """装配失败必须返回 ``(None, error)``，让上层降级而不是 500。"""

    source, error = build_water_mask_source("D:/definitely/not/here.gpkg")
    assert source is None
    assert error and "code" in error
