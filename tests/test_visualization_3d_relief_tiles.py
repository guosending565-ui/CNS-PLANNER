"""``ReliefTileSource``（FABDEM 派生地形晕渲）的语义与边界测试。

关键不可退让的语义：

1. 晕渲**不是卫星影像** —— 任何对外结构都必须能区分，绝不能被当成影像验收证据；
2. 垂直夸张**恒为 1.0** —— 不得用放大起伏伪造地形（本轮硬约束）；
3. 只读 —— 不改 FABDEM/geoid 原件，不写 ``ProjectState``；
4. 海面与 NoData 不伪造陆地；FABDEM 覆盖范围外不提供晕渲。
"""

import math
import struct
import zlib

import pytest

from cns_planner.gis.relief_tile_adapter import (
    LIGHT_ALTITUDE_DEG,
    LIGHT_AZIMUTH_DEG,
    VERTICAL_EXAGGERATION,
    ReliefTileSource,
    ReliefTileUnavailable,
    _xyz_tile_bbox,
)


class FakeTerrain:
    def __init__(self, value_fn=None, extent=(121.5, 29.4, 122.9, 30.5)):
        # 默认：经度方向线性升高，制造稳定坡向，便于断言明暗差异。
        self.value_fn = value_fn or (lambda lon, lat: (lon - 121.5) * 400.0)
        self.extent = list(extent)

    def describe(self):
        return {
            "sha256": "fabdem-sha", "extent": self.extent,
            "vertical_reference": "egm2008_orthometric", "source_modified": False,
        }

    def window(self, bbox, width, height):
        west, south, east, north = bbox
        rows = []
        for row in range(height):
            lat = north + (south - north) * (row / max(1, height - 1))
            rows.append([
                self.value_fn(west + (east - west) * (col / max(1, width - 1)), lat)
                for col in range(width)
            ])
        return {"values": rows, "status": "passed"}


class FakeGeoid:
    def describe(self):
        return {"sha256": "egm2008-sha", "canonical_backend": "proj_vgridshift_bilinear"}

    def window(self, bbox, width, height):
        return {"values": [[12.5] * width for _ in range(height)], "status": "passed"}


def source(**kwargs):
    return ReliefTileSource(kwargs.pop("terrain", None) or FakeTerrain(),
                            kwargs.pop("geoid", None) or FakeGeoid(), **kwargs)


def decode_png_gray(body):
    """最小 PNG 灰度解码（只支持本模块写出的 8-bit 灰度、filter 0）。"""

    assert body.startswith(b"\x89PNG\r\n\x1a\n"), "not a PNG"
    offset = 8
    width = height = None
    idat = b""
    while offset < len(body):
        length = struct.unpack(">I", body[offset:offset + 4])[0]
        tag = body[offset + 4:offset + 8]
        payload = body[offset + 8:offset + 8 + length]
        if tag == b"IHDR":
            width, height, depth, color = struct.unpack(">IIBB", payload[:10])
            assert depth == 8 and color == 0, "expected 8-bit grayscale"
        elif tag == b"IDAT":
            idat += payload
        offset += 12 + length
    raw = zlib.decompress(idat)
    pixels = bytearray()
    for row in range(height):
        start = row * (width + 1)
        assert raw[start] == 0, "unexpected filter type"
        pixels.extend(raw[start + 1:start + 1 + width])
    return width, height, bytes(pixels)


# ------------------------------------------------------------ 瓦片约定与基本形态


def test_relief_y_is_top_origin_xyz_not_tms():
    """回归：relief 的 y 必须是**顶原点**（XYZ），与 Cesium 地理影像网格一致。

    实测背景：z=6、目标范围 121-123E/29-32N 时，Cesium 请求 x=107、y=20-21；
    地理网格顶原点 y(29-32N) = 20-21，而 TMS y = 42-43。早期实现按 TMS 取 bbox，
    导致请求落到另一个半球、服务端一律判越界、底图空白且控制台刷满 4xx。
    """

    # z=6：瓦片宽高均为 2.8125 度；x=107 覆盖 120.9375-123.75E，
    # y=20 覆盖 30.9375-33.75N（顶原点，y 自北向南递增）
    west, south, east, north = _xyz_tile_bbox(6, 107, 20)
    assert 120.9 < west < 121.0 and 123.7 < east < 123.8
    assert 30.9 < south < 31.0 and 33.7 < north < 33.8
    # y=0 必须在最北：其北界为 90 度
    assert _xyz_tile_bbox(6, 107, 0)[3] == 90.0
    # 目标经度必须落在这张瓦片内
    assert west <= 122.2 <= east
    # 且与 TMS 的同一 y 明显不同（确认两套约定确实分离）
    from cns_planner.gis.terrain_tile_adapter import tms_tile_bbox
    assert _xyz_tile_bbox(6, 107, 20) != tms_tile_bbox(6, 107, 20)
    # TMS 的 y 自南向北递增，因此同一 y 的 TMS 瓦片明显更靠南
    assert tms_tile_bbox(6, 107, 20)[1] < south


def test_relief_serves_the_tile_cesium_actually_requests():
    """用 Cesium 实测请求的坐标验证可服务性（z=6 x=107 y=20/21）。"""

    src = source(terrain=FakeTerrain(extent=(121.0, 29.0, 123.0, 32.0)))
    for y in (20, 21):
        body, stats = src.tile(6, 107, y)
        assert body.startswith(b"\x89PNG")
        assert stats["z"] == 6 and stats["y"] == y


def test_relief_tile_is_valid_png_with_expected_size():
    body, stats = source().tile(10, 1719, 341)
    width, height, pixels = decode_png_gray(body)
    assert (width, height) == (256, 256)
    assert len(pixels) == 256 * 256
    assert stats["size"] == 256


# ------------------------------------------------------------------ 语义边界


def test_relief_is_never_declared_as_satellite_imagery():
    """最重要的一条：晕渲不得被当成卫星影像。"""

    body, stats = source().tile(10, 1719, 341)
    assert stats["is_satellite_imagery"] is False
    assert stats["derived_from"] == "fabdem_30m"
    assert stats["vertical_reference"] == "egm2008_orthometric"
    assert stats["source_modified"] is False


def test_vertical_exaggeration_is_exactly_one():
    """禁止用垂直夸张伪造起伏。"""

    body, stats = source().tile(10, 1719, 341)
    assert VERTICAL_EXAGGERATION == 1.0
    assert stats["vertical_exaggeration"] == 1.0


def test_relief_light_parameters_are_recorded():
    body, stats = source().tile(10, 1719, 341)
    assert stats["light_azimuth_deg"] == LIGHT_AZIMUTH_DEG == 315.0
    assert stats["light_altitude_deg"] == LIGHT_ALTITUDE_DEG == 45.0


def _mean_land_gray(value_fn):
    from cns_planner.gis.relief_tile_adapter import SEA_LEVEL_GRAY

    body, _stats = source(terrain=FakeTerrain(value_fn=value_fn)).tile(10, 1719, 341)
    _w, _h, pixels = decode_png_gray(body)
    land = [v for v in pixels if v != SEA_LEVEL_GRAY]
    assert land, "expected land pixels"
    return sum(land) / len(land)


def test_relief_light_is_north_west_not_south_west():
    """回归（真实缺陷）：光源必须来自**西北**，南北轴不得镜像。

    实测背景：``TerrainWindowSource.window`` 的行序是**北→南**，因此晕渲里的
    ``row + 1`` 是**南**邻点，``(south - here) / cell_y`` 等于 ``-∂f/∂y``。
    法线的 y 分量按 y 轴朝北应为 ``-∂f/∂y``，早期实现却写成 ``-dzdy``
    （即 ``+∂f/∂y``），使南北光照整体镜像——实际光源变成**西南 225°**。

    单轴实测（真实代码路径，坡度约 45°）：朝南坡 lambert 0.8557、朝北坡 0.1443，
    与声明的 315° 恰好相反；而东西轴是正确的（朝东 0.0825、朝西 0.8454）。
    因为东西轴正确，在对角坡面上 x/y 贡献部分抵消，这个缺陷在真实地形截图里
    **肉眼无法察觉**，只能靠单轴语义断言拦住——所以这条用例必须存在。
    """

    def north_high(lon, lat):
        return (lat - 29.4) * 20000.0     # 北高南低 => 坡面朝南 => 西北光下应偏暗

    def south_high(lon, lat):
        return (30.5 - lat) * 20000.0     # 南高北低 => 坡面朝北 => 西北光下应偏亮

    def west_high(lon, lat):
        return (122.9 - lon) * 20000.0    # 西高东低 => 坡面朝东 => 应偏暗

    def east_high(lon, lat):
        return (lon - 121.5) * 20000.0    # 东高西低 => 坡面朝西 => 应偏亮

    south_facing = _mean_land_gray(north_high)   # 坡面朝南
    north_facing = _mean_land_gray(south_high)   # 坡面朝北
    east_facing = _mean_land_gray(west_high)     # 坡面朝东
    west_facing = _mean_land_gray(east_high)     # 坡面朝西

    assert north_facing > south_facing + 5.0, (
        "南北轴光照镜像：朝北坡 %.1f 必须明显亮于朝南坡 %.1f（光源应在西北）"
        % (north_facing, south_facing))
    assert west_facing > east_facing + 5.0, (
        "东西轴光照方向错误：朝西坡 %.1f 必须明显亮于朝东坡 %.1f"
        % (west_facing, east_facing))


def test_relief_shows_directional_shading_for_curved_terrain():
    """起伏地形必须产生明暗差异，而不是一片平色。

    注意：**均匀斜坡**的梯度是常数，因此晕渲亮度也应当是常数（见下一条用例）。
    要检验明暗变化，必须用**曲率非零**的地形（这里用高斯山体）。
    """

    def hill(lon, lat):
        dx = (lon - 122.2) * 111.0
        dy = (lat - 30.0) * 111.0
        return 400.0 * math.exp(-(dx * dx + dy * dy) / (2 * 3.0 * 3.0))

    body, stats = source(terrain=FakeTerrain(value_fn=hill)).tile(10, 1719, 341)
    _w, _h, pixels = decode_png_gray(body)
    assert len(set(pixels)) > 5, "expected varied shading, got flat color"
    assert stats["shaded_cells"] > 0


def test_land_band_leaves_headroom_below_white():
    """陆地灰度上限必须留余量：打满 255 会让朝阳坡整片死白、丢失等高细节。

    回归背景：曾把上限设为 255，真实近景截图里陆地是一片纯白（亮度均值 249、
    93.8% 像素 >200），地形细节不可读。
    """

    from cns_planner.gis.relief_tile_adapter import LAND_MAX_GRAY, SEA_LEVEL_GRAY

    assert LAND_MAX_GRAY <= 230, "陆地上限过高会把朝阳坡打成纯白"
    assert LAND_MAX_GRAY - SEA_LEVEL_GRAY >= 60, "海陆灰度间隔过小会让海岸线不可读"

    def hill(lon, lat):
        dx = (lon - 122.2) * 111.0
        dy = (lat - 30.0) * 111.0
        return 400.0 * math.exp(-(dx * dx + dy * dy) / (2 * 3.0 * 3.0))

    body, _stats = source(terrain=FakeTerrain(value_fn=hill)).tile(10, 1719, 341)
    _w, _h, pixels = decode_png_gray(body)
    # 不应出现大片纯白
    white_fraction = sum(1 for v in pixels if v >= 250) / len(pixels)
    assert white_fraction < 0.05, (
        "纯白占比过高：%.3f" % white_fraction)


def test_uniform_slope_gives_uniform_shading():
    """均匀斜坡的梯度恒定 => 晕渲亮度恒定（这是正确行为，不是缺陷）。"""

    body, stats = source().tile(10, 1719, 341)   # 默认夹具即线性斜坡
    _w, _h, pixels = decode_png_gray(body)
    assert stats["land_cells"] > 0
    assert len(set(pixels)) <= 2


def test_sea_and_land_gray_bands_do_not_overlap():
    """海岸线必须可读：陆地灰度整体高于海面灰度，且两区间不重叠。

    回归背景：早期实现陆地灰度区间为 125–245、海面固定 176，两者**重叠**，
    近景截图里海陆同色、海岸轮廓不可读。现在陆地压进 [LAND_MIN_GRAY, LAND_MAX_GRAY]。
    """

    from cns_planner.gis.relief_tile_adapter import LAND_MIN_GRAY, SEA_LEVEL_GRAY

    assert SEA_LEVEL_GRAY < LAND_MIN_GRAY, "海面灰度必须严格低于陆地灰度下限"

    def hill(lon, lat):
        dx = (lon - 122.2) * 111.0
        dy = (lat - 30.0) * 111.0
        return 400.0 * math.exp(-(dx * dx + dy * dy) / (2 * 3.0 * 3.0))

    body, _stats = source(terrain=FakeTerrain(value_fn=hill)).tile(10, 1719, 341)
    _w, _h, pixels = decode_png_gray(body)
    land_values = {v for v in pixels if v != SEA_LEVEL_GRAY}
    assert land_values, "expected some land pixels"
    assert min(land_values) >= LAND_MIN_GRAY, (
        "陆地灰度不得落入海面区间：min=%d" % min(land_values))


def test_sea_and_nodata_are_not_painted_as_land():
    """海面（<=0 m）与 NoData 都不得产生陆地起伏。"""

    def value_fn(lon, lat):
        if lon < 122.2:
            return -5.0      # 海
        return None          # NoData

    body, stats = source(terrain=FakeTerrain(value_fn=value_fn)).tile(10, 1719, 341)
    _w, _h, pixels = decode_png_gray(body)
    assert stats["land_cells"] == 0
    assert stats["shaded_cells"] == 0
    assert len(set(pixels)) == 1


def test_flat_terrain_produces_uniform_lighting():
    """平地（无坡度）应得到接近常数的亮度，不应凭空产生纹理。"""

    body, stats = source(terrain=FakeTerrain(value_fn=lambda lon, lat: 50.0)).tile(
        10, 1719, 341)
    _w, _h, pixels = decode_png_gray(body)
    assert stats["land_cells"] == 256 * 256
    assert len(set(pixels)) <= 2


# ------------------------------------------------------------------ 失败关闭


def test_global_background_levels_do_not_fabricate_relief():
    """全球背景层没有 FABDEM 数据，不得伪造晕渲。"""

    with pytest.raises(ReliefTileUnavailable):
        source().tile(3, 6, 3)


def test_out_of_extent_tile_is_refused():
    with pytest.raises(ReliefTileUnavailable):
        source(terrain=FakeTerrain(extent=(10.0, 10.0, 11.0, 11.0))).tile(
            10, 1719, 341)


def test_coordinate_and_zoom_guards():
    src = source()
    with pytest.raises(ReliefTileUnavailable):
        src.tile(99, 0, 0)
    with pytest.raises(ReliefTileUnavailable):
        src.tile(10, -1, 0)
    with pytest.raises(ReliefTileUnavailable):
        src.tile("x", 0, 0)


def test_missing_sources_are_refused_at_construction():
    with pytest.raises(ReliefTileUnavailable):
        ReliefTileSource(None, FakeGeoid())
    with pytest.raises(ReliefTileUnavailable):
        ReliefTileSource(FakeTerrain(), None)


# ------------------------------------------------------------------ 路由契约
#
# 回归：Cesium 在低层级会请求**覆盖范围之外**的瓦片（z=0/1 等）。这属于正常的
# 越界请求，必须以 4xx 回答；早期实现未映射 ReliefTileUnavailable，导致这些请求
# 返回 500，真实浏览器里刷出 273 条控制台错误，并把"正常越界"误报成服务端故障。


def _router_with(relief_source):
    from types import SimpleNamespace

    import cns_planner.application.visualization_3d_service as service_module
    from cns_planner.api.router import ApiRouter
    from cns_planner.application.visualization_3d_service import Visualization3DService

    service = Visualization3DService({}, relief_tile_source=relief_source)
    original = service_module.service_for
    service_module.service_for = lambda context: service
    router = ApiRouter(SimpleNamespace(workflow=SimpleNamespace(state={}), data=None))
    return router, service_module, original


def test_out_of_extent_relief_tile_returns_4xx_not_500():
    router, service_module, original = _router_with(source())
    try:
        for path in ("/api/visualization-3d/relief/0/0/0.png",
                     "/api/visualization-3d/relief/1/1/0.png"):
            response = router.get(path, {}, {})
            assert response.status == 400, (
                "%s 返回 %s，越界请求不得报 500" % (path, response.status))
    finally:
        service_module.service_for = original


def test_in_extent_relief_tile_returns_png_with_provenance_headers():
    router, service_module, original = _router_with(source())
    try:
        response = router.get("/api/visualization-3d/relief/10/1719/341.png", {}, {})
        assert response.status == 200
        assert response.content_type == "image/png"
        assert response.data.startswith(b"\x89PNG")
        # 必须显式声明"不是卫星影像"
        assert response.headers["X-CNS-Relief-Kind"] == (
            "fabdem_hillshade_not_satellite_imagery")
        assert response.headers["X-CNS-Relief-Vertical-Exaggeration"] == "1.0"
    finally:
        service_module.service_for = original
