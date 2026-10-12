import math
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from cns_planner.gis.terrain_tile_adapter import (
    HEIGHT_OFFSET,
    HEIGHT_SCALE,
    HEIGHTMAP_WIDTH,
    MAX_LEVEL,
    TerrainTileSource,
    TerrainTileUnavailable,
    tms_tile_bbox,
)
from cns_planner.application.visualization_3d_service import Visualization3DService
from cns_planner.api.router import ApiRouter


class FakeTerrain:
    def __init__(self, value_fn=None, extent=(121.5, 29.4, 122.9, 30.5)):
        self.value_fn = value_fn or (lambda lon, lat: 100.0 + lon - lat)
        self.extent = list(extent)
        self.window_calls = 0

    def describe(self):
        return {
            "sha256": "fabdem-sha", "extent": self.extent,
            "vertical_reference": "egm2008_orthometric", "vertical_status": "confirmed",
            "source_modified": False,
        }

    def window(self, bbox, width, height):
        self.window_calls += 1
        west, south, east, north = bbox
        values = []
        nodata = 0
        for row in range(height):
            lat = north + (south - north) * row / (height - 1)
            line = []
            for col in range(width):
                lon = west + (east - west) * col / (width - 1)
                value = self.value_fn(lon, lat)
                nodata += value is None
                line.append(value)
            values.append(line)
        return {"values": values, "nodata_count": nodata, "status": "passed" if not nodata else "partial"}


class FakeGeoid:
    def __init__(self, value_fn=None):
        self.value_fn = value_fn or (lambda lon, lat: 12.5 + lon * 0.01)

    def describe(self):
        return {"sha256": "egm2008-sha", "canonical_backend": "proj_vgridshift_bilinear"}

    def window(self, bbox, width, height):
        west, south, east, north = bbox
        values = []
        for row in range(height):
            lat = north + (south - north) * row / (height - 1)
            values.append([
                self.value_fn(west + (east - west) * col / (width - 1), lat)
                for col in range(width)
            ])
        return {"values": values, "nodata_count": 0, "status": "passed",
                "backend": "proj_vgridshift_bilinear"}


class FakeWaterMask:
    def window(self, bbox, width, height):
        return [[255 if col < width // 2 else 0 for col in range(width)]
                for _row in range(height)]


def decode(tile):
    count = HEIGHTMAP_WIDTH * HEIGHTMAP_WIDTH
    encoded = struct.unpack("<" + "H" * count, tile.body[:count * 2])
    return [HEIGHT_OFFSET + HEIGHT_SCALE * value for value in encoded]


def source(terrain=None, geoid=None, **kwargs):
    return TerrainTileSource(terrain or FakeTerrain(), geoid or FakeGeoid(), **kwargs)


def test_layer_json_is_standard_epsg4326_tms_heightmap_contract():
    layer = source().layer_json("/api/visualization-3d/terrain")
    assert layer["format"] == "heightmap-1.0"
    assert layer["projection"] == "EPSG:4326"
    assert layer["scheme"] == "tms"
    assert layer["maxzoom"] == MAX_LEVEL == 13
    assert layer["heightmapWidth"] == HEIGHTMAP_WIDTH == 65
    assert layer["heightmapStructure"] == {
        "heightScale": 0.2, "heightOffset": -1000.0, "elementsPerHeight": 1,
        "stride": 1, "elementMultiplier": 256.0, "isBigEndian": False,
        "lowestEncodedHeight": 0, "highestEncodedHeight": 65535,
    }
    assert layer["tiles"] == ["/api/visualization-3d/terrain/{z}/{x}/{y}.terrain"]
    assert layer["source"]["fabdem_sha256"] == "fabdem-sha"
    assert layer["source"]["egm2008_sha256"] == "egm2008-sha"


@pytest.mark.parametrize("z,x,y", [(-1, 0, 0), (14, 0, 0), (3, 16, 1), (3, 1, 8)])
def test_tile_coordinates_are_bounded(z, x, y):
    with pytest.raises(TerrainTileUnavailable):
        source().tile(z, x, y)


def test_tile_encodes_65_square_little_endian_samples_child_mask_and_etag():
    tile = source().tile(8, 429, 170)
    assert len(tile.body) == HEIGHTMAP_WIDTH * HEIGHTMAP_WIDTH * 2 + 1
    assert tile.body[-1] == 15
    assert tile.etag.startswith("terrain-heightmap-v1-")
    assert tile.stats["fabdem_sha256"] == "fabdem-sha"
    assert tile.stats["egm2008_sha256"] == "egm2008-sha"
    assert tile.stats["source_modified"] is False


def test_height_is_canonical_h_equals_orthometric_H_plus_geoid_N():
    terrain = FakeTerrain(lambda lon, lat: 120.0)
    geoid = FakeGeoid(lambda lon, lat: 15.0)
    heights = decode(source(terrain, geoid).tile(8, 429, 170))
    assert max(abs(value - 135.0) for value in heights) <= HEIGHT_SCALE / 2 + 1e-9


def test_same_level_neighbors_share_bit_identical_edge_samples():
    terrain_source = source()
    left = decode(terrain_source.tile(8, 429, 170))
    right = decode(terrain_source.tile(8, 430, 170))
    left_edge = [left[row * 65 + 64] for row in range(65)]
    right_edge = [right[row * 65] for row in range(65)]
    assert left_edge == right_edge


def test_parent_child_common_samples_match_without_cracks():
    terrain_source = source()
    parent = decode(terrain_source.tile(7, 214, 85))
    child_sw = decode(terrain_source.tile(8, 428, 170))
    # southwest child spans the parent's west half and south half in TMS.
    for child_row in range(0, 65, 2):
        parent_row = 32 + child_row // 2
        for child_col in range(0, 65, 2):
            parent_col = child_col // 2
            assert child_sw[child_row * 65 + child_col] == parent[parent_row * 65 + parent_col]


def test_steep_slope_is_preserved_without_vertical_exaggeration():
    west, _, east, _ = tms_tile_bbox(8, 429, 170)
    split = (west + east) / 2
    terrain = FakeTerrain(lambda lon, lat: 10.0 if lon < split else 310.0)
    heights = decode(source(terrain, FakeGeoid(lambda lon, lat: 0.0)).tile(8, 429, 170))
    assert min(heights) == pytest.approx(10.0, abs=HEIGHT_SCALE)
    assert max(heights) == pytest.approx(310.0, abs=HEIGHT_SCALE)
    assert max(heights) - min(heights) == pytest.approx(300.0, abs=HEIGHT_SCALE * 2)


def test_nodata_uses_declared_geoid_background_and_reports_non_authoritative_cells():
    terrain = FakeTerrain(lambda lon, lat: None)
    geoid = FakeGeoid(lambda lon, lat: 14.0)
    tile = source(terrain, geoid).tile(8, 429, 170)
    assert all(value == pytest.approx(14.0, abs=HEIGHT_SCALE) for value in decode(tile))
    assert tile.stats["nodata_background_count"] == 65 * 65
    assert tile.stats["background_orthometric_m"] == 0.0
    assert tile.stats["background_authoritative_for_planning"] is False


def test_fabdem_boundary_remains_continuous_with_geoid_background():
    west, _, east, _ = tms_tile_bbox(8, 429, 170)
    split = (west + east) / 2
    terrain = FakeTerrain(lambda lon, lat: 0.0 if lon >= split else None)
    geoid = FakeGeoid(lambda lon, lat: 13.0)
    heights = decode(source(terrain, geoid).tile(8, 429, 170))
    assert max(heights) - min(heights) <= HEIGHT_SCALE


def test_tile_cache_is_memory_only_and_reuses_sampling_result():
    terrain = FakeTerrain()
    terrain_source = source(terrain)
    first = terrain_source.tile(8, 429, 170)
    second = terrain_source.tile(8, 429, 170)
    assert first is second
    assert terrain.window_calls == 1
    assert first.headers["Cache-Control"] == "private, max-age=86400"
    assert first.headers["X-CNS-Terrain-Cache"] == "memory-lru"


def test_optional_water_mask_is_declared_and_encoded_without_guessing_from_height():
    terrain_source = source(water_mask_source=FakeWaterMask())
    layer = terrain_source.layer_json("/api/visualization-3d/terrain")
    tile = terrain_source.tile(8, 429, 170)
    assert layer["extensions"] == ["watermask"]
    assert len(tile.body) == 65 * 65 * 2 + 1 + 256 * 256
    assert tile.stats["water_mask_status"] == "available_from_cartographic_mask"


def test_visualization_manifest_declares_multilevel_terrain_identity():
    terrain_tiles = source()
    service = Visualization3DService({}, terrain_tile_source=terrain_tiles)
    manifest = service.manifest()
    assert manifest["terrain_tiles"] == {
        "status": "available",
        "format": "heightmap-1.0",
        "maxzoom": 13,
        "layer_json_endpoint": "/api/visualization-3d/terrain/layer.json",
        "automatic_lod": True,
        # 无水 mask 源时必须如实报告 blocked，前端据此关闭海面效果。
        "water_mask_status": "blocked",
    }
    assert service.identity_seed()["terrain_tiles"]["maxzoom"] == 13


def test_manifest_reports_available_water_mask_when_source_present():
    """有可用水 mask 时 manifest 必须报告 available，且与 layer.json 同真。

    探测要求窗口内**确有陆地与水面**，所以这里必须用混合源。
    """

    terrain_tiles = source(water_mask_source=FakeMaskSource(land="mixed"))
    service = Visualization3DService({}, terrain_tile_source=terrain_tiles)
    manifest = service.manifest()
    assert manifest["terrain_tiles"]["water_mask_status"] == "available"
    assert terrain_tiles.layer_json("/api/visualization-3d/terrain")["extensions"] == [
        "watermask"]


def test_water_mask_probe_requires_real_sea_land_contrast():
    """回归（真实缺陷）：探测必须要求窗口内确有海陆差异，未知不得判为可用。

    早期实现用 ``tms_tile_bbox(MAX_LEVEL, 0, 0)`` 做探测，实测该 bbox 是
    ``(-180.0, -90.0, -179.978, -89.978)``——南半球西端、远离舟山，必然整片落在
    掩膜覆盖范围之外。那种窗口被 footprint 守卫全部判成"不确定"（按陆地），结果
    **恒为单色**，于是"全不确定 / 全陆 / 全海"三种语义完全不同的源都被读成
    ``available_from_cartographic_mask``，``layer.json`` 也随之对外声明提供
    watermask。这是"未评估被当成通过"，违反 AGENTS.md 长期不变量 3。
    """

    class AllUnknownMask:
        """最坏情况：所有采样都是不确定（footprint 未知 / 覆盖范围之外）。"""

        def window(self, bbox, width, height):
            return {"values": [[None] * width for _ in range(height)]}

    for label, mask in (
            ("all-uncertain", AllUnknownMask()),
            ("all-land", FakeMaskSource(land=True)),
            ("all-sea", FakeMaskSource(land=False))):
        terrain_tiles = source(water_mask_source=mask)
        status = terrain_tiles._water_mask_status()
        assert status == "blocked_water_mask_probe_inconclusive", (
            "%s 源不得被判为可用，实际为 %s" % (label, status))
        service = Visualization3DService({}, terrain_tile_source=terrain_tiles)
        assert service.manifest()["terrain_tiles"]["water_mask_status"] == "blocked", label
        assert terrain_tiles.layer_json(
            "/api/visualization-3d/terrain")["extensions"] == [], label


def test_manifest_reports_blocked_water_mask_when_source_is_broken():
    """水 mask 源损坏时必须报告 blocked，绝不宣称可用。"""

    class Broken:
        def window(self, bbox, width, height):
            raise RuntimeError("boom")

    terrain_tiles = source(water_mask_source=Broken())
    service = Visualization3DService({}, terrain_tile_source=terrain_tiles)
    assert service.manifest()["terrain_tiles"]["water_mask_status"] == "blocked"
    assert terrain_tiles.layer_json("/api/visualization-3d/terrain")["extensions"] == []


def test_router_serves_layer_json_and_binary_terrain_with_etag(monkeypatch):
    terrain_tiles = source()
    service = Visualization3DService({}, terrain_tile_source=terrain_tiles)
    import cns_planner.application.visualization_3d_service as service_module
    monkeypatch.setattr(service_module, "service_for", lambda context: service)
    context = SimpleNamespace(workflow=SimpleNamespace(state={}), data=None)
    router = ApiRouter(context)

    layer = router.get("/api/visualization-3d/terrain/layer.json", {}, {})
    assert layer.data["format"] == "heightmap-1.0"
    assert layer.headers["Cache-Control"] == "private, max-age=86400"
    response = router.get("/api/visualization-3d/terrain/8/429/170.terrain", {}, {})
    # heightmap-1.0 is raw little-endian uint16, **not** quantized-mesh. Cesium's
    # CesiumTerrainProvider sniffs the format from layer.json, but advertising the
    # quantized-mesh media type is factually wrong for every other consumer and for
    # licence/provenance tooling, so the response must declare its real format.
    assert response.headers["X-CNS-Terrain-Format"] == "heightmap-1.0"
    assert response.content_type != "application/vnd.quantized-mesh"
    assert len(response.data) == 65 * 65 * 2 + 1
    assert response.headers["ETag"].startswith('"terrain-heightmap-v1-')
    unknown = router.get("/api/visualization-3d/terrain/8/999/170.terrain", {}, {})
    assert unknown.status in {400, 404}


def test_different_tiles_serialize_shared_gdal_source_access():
    terrain = FakeTerrain()
    terrain._active = 0
    terrain.max_active = 0
    active_lock = threading.Lock()
    original_window = terrain.window

    def concurrency_sensitive_window(*args, **kwargs):
        with active_lock:
            terrain._active += 1
            terrain.max_active = max(terrain.max_active, terrain._active)
        try:
            time.sleep(0.03)
            if terrain._active > 1:
                raise RuntimeError("shared GDAL handle used concurrently")
            return original_window(*args, **kwargs)
        finally:
            with active_lock:
                terrain._active -= 1

    terrain.window = concurrency_sensitive_window
    terrain_source = source(terrain)
    with ThreadPoolExecutor(max_workers=2) as pool:
        tiles = list(pool.map(lambda coordinates: terrain_source.tile(*coordinates), [
            (8, 429, 170), (8, 430, 170),
        ]))
    assert len(tiles) == 2
    assert terrain.max_active == 1


# --------------------------------------------------------------------- 水 mask
#
# 三个独立缺陷（Round37-T3D-BMAP 复检）：
#   1. 采样源返回 **dict**（``{"values": ...}``），而适配器按嵌套 list 迭代，
#      会抛 ``ValueError`` 而非 fail-closed；
#   2. 陆域掩膜是 **LAND-truthy**，而 heightmap water mask 是 **WATER-truthy**，
#      直接复用会把海洋反光刷到陆地上；
#   3. ``coastal_uncertain`` / ``unknown`` 必须显式处理，不得静默当成水面。


class FakeMaskSource:
    """返回 ``{"values": ...}`` 的陆域掩膜；``1`` 表示陆地，``0`` 表示水面。

    ``land="mixed"`` 时左半为陆、右半为水。可用性探测**要求窗口内既有陆地又有
    水面**——全陆 / 全海 / 全不确定都无法证明掩膜有判别力（见 ``_probe_water_mask``）。
    """

    def __init__(self, land=True, shape="dict"):
        self.land = land
        self.shape = shape
        self.calls = 0

    def window(self, bbox, width, height):
        self.calls += 1
        if self.land == "mixed":
            values = [[1 if col < width // 2 else 0 for col in range(width)]
                      for _ in range(height)]
        else:
            values = [[1 if self.land else 0] * width for _ in range(height)]
        if self.shape == "dict":
            return {"values": values, "status": "passed"}
        return values


def test_water_mask_reads_values_key_not_the_dict_itself():
    """缺陷 1：dict 返回结构必须先取 ``values``，否则 int('s') 崩溃。"""

    mask = FakeMaskSource(land=False)
    terrain_source = source(water_mask_source=mask)
    tile = terrain_source.tile(8, 429, 170)
    assert mask.calls == 1
    # 全海 -> 均匀掩膜走单字节简写（既有契约），但必须在 child mask 之后多出这一字节
    assert len(tile.body) == HEIGHTMAP_WIDTH * HEIGHTMAP_WIDTH * 2 + 1 + 1
    assert tile.body[-1] == 255
    assert tile.stats["water_mask_status"] == "available_from_cartographic_mask"


def test_water_mask_inverts_land_truthy_source_into_water_truthy_tile():
    """缺陷 2：陆域掩膜取反后才写入 water mask，否则海洋会画到陆地上。"""

    all_land = source(water_mask_source=FakeMaskSource(land=True))
    land_body = all_land.tile(8, 429, 170).body
    # 陆地 -> water mask 为 0（water-truthy 格式下 0 = 非水面）
    assert land_body[-1] == 0
    assert all_land.tile(8, 429, 170).stats["water_mask_status"] == (
        "available_from_cartographic_mask")

    all_sea = source(water_mask_source=FakeMaskSource(land=False))
    sea_body = all_sea.tile(8, 429, 170).body
    # 水面 -> water mask 为 255
    assert sea_body[-1] == 255
    # 同一瓦片的两种掩膜必须产生不同的瓦片身份指纹
    assert land_body != sea_body


def test_unshaped_water_mask_fails_closed_without_breaking_the_tile():
    """缺陷 3：结构不可识别时必须降级为「无水 mask」，绝不猜、绝不半写。"""

    class WrongShapeMask:
        def window(self, bbox, width, height):
            # 尺寸不符（128×128 而非 256×256）——不得被当成合法掩膜写入
            return {"values": [[1] * 128 for _ in range(128)]}

    terrain_source = source(water_mask_source=WrongShapeMask())
    tile = terrain_source.tile(8, 429, 170)
    assert len(tile.body) == HEIGHTMAP_WIDTH * HEIGHTMAP_WIDTH * 2 + 1
    assert tile.stats["water_mask_status"] == "blocked_water_mask_shape_unrecognized"
    # layer.json 也必须随之降级，不能继续对外宣称提供 watermask 扩展
    assert terrain_source.layer_json("http://x/terrain")["extensions"] == []


def test_flat_nested_list_water_mask_is_accepted():
    """裸嵌套 list（非 dict）同样是合法采样结构，不得被误判为不可识别。"""

    terrain_source = source(water_mask_source=FakeMaskSource(land=False, shape="nested_list"))
    tile = terrain_source.tile(8, 429, 170)
    assert tile.stats["water_mask_status"] == "available_from_cartographic_mask"
    assert tile.body[-1] == 255


def test_broken_water_mask_source_never_fails_the_terrain_tile():
    """水 mask 是**显示增强**，其失败不得让地形瓦片整体不可用。"""

    class ExplodingMask:
        def window(self, bbox, width, height):
            raise RuntimeError("mask backend down")

    terrain_source = source(water_mask_source=ExplodingMask())
    tile = terrain_source.tile(8, 429, 170)
    assert len(tile.body) == HEIGHTMAP_WIDTH * HEIGHTMAP_WIDTH * 2 + 1
    assert tile.stats["water_mask_status"] == "blocked_water_mask_source_failed"
    assert terrain_source.layer_json("http://x/terrain")["extensions"] == []


def test_water_mask_none_and_true_are_decoded_as_land_not_sea():
    """``None`` 与真值都必须落到「陆地」，绝不让不确定区域变成海面。"""

    class MixedMask:
        def window(self, bbox, width, height):
            # 左半边：陆地(1)；右半边：水面(0)；所有 4 的倍数列：None（不确定）
            rows = []
            for _row in range(height):
                rows.append([None if col % 4 == 0 else (1 if col < width // 2 else 0)
                             for col in range(width)])
            return {"values": rows}

    terrain_source = source(water_mask_source=MixedMask())
    tile = terrain_source.tile(8, 429, 170)
    assert tile.stats["water_mask_status"] == "available_from_cartographic_mask"
    # 非均匀掩膜必须是完整的 256×256
    mask = tile.body[-(256 * 256):]
    assert len(mask) == 256 * 256
    assert set(mask) <= {0, 255}
    first_row = mask[:256]
    # 陆地列 -> 0（非水面）；水面列 -> 255；None 列 -> 0（fail toward land）
    assert first_row[1] == 0, "陆地列必须映射为 0"
    assert first_row[4] == 0, "None（不确定）列必须按陆地处理"
    assert first_row[201] == 255, "水面列（非 4 倍数）必须映射为 255"


# ------------------------------------------------- 采样下标吸附（接缝回归）
#
# 回归背景（真实数据实测）：``InvGeoTransform`` 是逐块重建的，同一个经纬度在相邻
# 两块里会得到数学上相等、浮点上相差 ~1e-10 的像素坐标。例如公共边经度在两块里
# 分别是 ``634.0`` 与 ``0.9999999999417923``（块内坐标），``floor`` 后取到全幅像元
# ``4838`` 与 ``4837``——同一位置选中了**不同像元对**，于是瓦片接缝处出现 35.2 m 的
# 高度跳变（而该处真实地形梯度不支持这种跳变）。
#
# 修复：``_snap_pixel_index`` 把容差内的"几乎整数"坐标吸附为整数。


def test_pixel_index_snaps_near_integers_to_remove_seam_jitter():
    from cns_planner.gis.terrain_window_adapter import _snap_pixel_index

    # 这正是实测中出现的两个值：必须吸附到同一个整数
    assert _snap_pixel_index(0.9999999999417923) == 1.0
    assert _snap_pixel_index(634.0) == 634.0
    # 非整数坐标必须保持原样（不能被误吸附，否则会引入真实偏移）
    assert _snap_pixel_index(0.5) == 0.5
    assert _snap_pixel_index(634.37) == 634.37
    # 容差之外的小数部分不得被吞掉
    assert _snap_pixel_index(1.001) == 1.001


def test_bilinear_uses_the_same_source_pixel_on_a_shared_edge():
    """回归（真实缺陷）：相邻瓦片在公共边上必须取到**同一个源像元**。

    ``InvGeoTransform`` 是逐块重建的，同一个经纬度在相邻两块里会算出数学上相等、
    浮点上相差 ~1e-10 的**块内**像素坐标（实测 634.0 与 0.9999999999417923）。
    ``floor`` 之后两块分别取到像元 4838 与 4837——同一位置选了不同像元对，在瓦片
    公共边产生可见裂缝（实测某行差 35.2 m）。

    这条用例锁住两件事：``_snap_pixel_index`` **确实被** ``_bilinear`` 调用，以及
    多读一圈让边界采样有定义。删掉那两行 snap 后，右块会落到前一个像元、插值权重
    也不再精确为 0，于是下面的**精确相等**断言失败——这正是原测试套件缺的护栏
    （原有的接缝断言全部走 FakeTerrain 假窗口，根本不经过生产采样代码）。
    """

    from cns_planner.gis.terrain_window_adapter import _bilinear

    class JitteryGdal:
        """对指定块原点注入 ~1e-10 的"几乎整数"抖动，复现真实 InvGeoTransform 行为。"""

        def __init__(self, jitter_origin):
            self.jitter_origin = jitter_origin

        def InvGeoTransform(self, transform):
            return transform

        def ApplyGeoTransform(self, transform, x, y):
            ox, sx, _r, oy, _c, sy = transform
            px = (x - ox) / sx
            py = (y - oy) / sy
            if ox == self.jitter_origin:
                nearest = round(px)
                if abs(px - nearest) <= 1e-9:
                    px = nearest - 5.8e-11
            return px, py

    # 全局像元索引 -> 高度：每像元相差 1000 m，取错像元会立刻暴露。
    global_values = [1000.0 * i for i in range(24)]
    left_origin, right_origin, edge_lon = 3.0, 11.0, 12.0
    left_rows = [[global_values[int(left_origin) + c] for c in range(13)]
                 for _ in range(4)]
    right_rows = [[global_values[int(right_origin) + c] for c in range(13)]
                  for _ in range(4)]
    left_transform = (left_origin, 1.0, 0.0, 0.0, 0.0, -1.0)
    right_transform = (right_origin, 1.0, 0.0, 0.0, 0.0, -1.0)

    left = _bilinear(left_transform, left_rows, JitteryGdal(None), edge_lon, 0.0,
                     len(left_rows), len(left_rows[0]), None)
    right = _bilinear(right_transform, right_rows, JitteryGdal(right_origin), edge_lon, 0.0,
                      len(right_rows), len(right_rows[0]), None)

    # 公共边经度 12.0 在全幅栅格上对应像元 12：
    # 左块块内 12.0-3.0=9.0 => col 9 => 全局 3+9=12；右块 12.0-11.0=1.0 => col 1 => 全局 11+1=12
    assert left == global_values[12], "左块应取到像元 12，实际 %r" % (left,)
    assert right == global_values[12], "右块应取到像元 12，实际 %r" % (right,)
    assert left == right, "相邻瓦片在公共边上必须取到同一个源像元"


def test_bilinear_rejects_out_of_block_but_handles_shared_edge():
    """公共边（块内最后一列）必须能取到值，而不是因 col+1 越界返回 None。"""

    from cns_planner.gis.terrain_window_adapter import _bilinear

    class FakeGdal:
        @staticmethod
        def InvGeoTransform(transform):
            return (0.0, 1.0, 0.0, 0.0, 0.0, -1.0)

        @staticmethod
        def ApplyGeoTransform(inverse, x, y):
            return (x, y)

    rows = [[float(c) for c in range(4)] for _ in range(4)]
    # 块内 x=3.0 是最后一列；多读一圈后 ncols=4，col=3 时 col+1 越界 → None
    # （调用方已通过多读 2 列保证不会走到这里）
    assert _bilinear((0, 1, 0, 0, 0, -1), rows, FakeGdal, 1.5, 1.5, 4, 4, None) == 1.5
    assert _bilinear((0, 1, 0, 0, 0, -1), rows, FakeGdal, 3.0, 1.5, 4, 4, None) is None
