"""Round38-OFFLINE-IMAGERY 后端回归。

覆盖离线瓦片源的**独立**契约：

1. 元信息/覆盖范围/署名来自数据本身，不硬编码；
2. 瓦片路径解析拒绝越界与目录穿越；
3. 离线瓦片**不**读凭证、**不**查预算、**不**触网（上游计数恒为 0）；
4. 瓦片缺失是正常的 404 语义，与在线越界 503 严格分开；
5. ``configuration()`` 并列披露离线源，且默认底图为离线影像。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cns_planner.application.visualization_3d_imagery_service import (  # noqa: E402
    Imagery3DService, ImageryUnavailable,
)
from cns_planner.gis.offline_imagery_adapter import (  # noqa: E402
    OfflineImageryTileSource, OfflineImageryUnavailable, source_for,
)

#: 真实离线数据目录（本轮已生成）。不存在时相关用例 skip，而不是伪造数据。
REAL_ROOT = Path(r"D:\Projects\_cns_workspaces\derived\round38_offline_imagery")
REAL_TILES = REAL_ROOT / "tiles" / "zhoushan_s2b_tci"


def make_source(tmp_path, *, coverage=(122.0, 29.7, 122.5, 30.1)):
    """构造一个最小但结构**真实**的离线数据目录（含元信息与一张瓦片）。"""
    meta_dir = tmp_path / "meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    tiles = tmp_path / "tiles" / "zhoushan_s2b_tci" / "14" / "13748"
    tiles.mkdir(parents=True, exist_ok=True)
    (tiles / "6758.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"payload")
    (meta_dir / "offline_imagery_metadata.json").write_text(json.dumps({
        "schema_version": "offline-imagery-source/v1",
        "coverage_crs84": list(coverage),
        "license": {"name": "Copernicus Sentinel Data Terms",
                    "attribution_text": "Contains modified Copernicus Sentinel data 2024"},
        "resolution": {"native_gsd_m": 10.0, "delivered_m_per_px": 10.0, "upsampled": False},
        "scene": {"acquired_utc": "2024-11-30T02:39:21Z"},
        "tiles": {"tile_size": 256, "minzoom": 9, "maxzoom": 14,
                  "count": 1, "bytes": 15, "content_sha256": "deadbeef"},
    }, ensure_ascii=False), encoding="utf-8")
    return OfflineImageryTileSource(root=tmp_path)


def test_descriptor_reports_real_coverage_and_attribution(tmp_path):
    source = make_source(tmp_path)
    row = source.descriptor()
    assert row["id"] == "offline_zhoushan_imagery"
    assert row["status"] == "available"
    # 覆盖范围必须来自元信息，不能硬编码。
    assert row["coverage_crs84"] == [122.0, 29.7, 122.5, 30.1]
    assert row["attribution"] == "Contains modified Copernicus Sentinel data 2024"
    # 离线源的三个关键性质：不触网、不占预算、不需要凭证。
    assert row["requires_credential"] is False
    assert row["consumes_online_budget"] is False
    assert row["is_satellite_imagery"] is True
    # 分辨率必须如实：10 m 就是 10 m，且**没有**升采样。
    assert row["native_gsd_m"] == 10.0
    assert row["upsampled"] is False


def test_missing_metadata_is_disclosed_with_a_real_reason(tmp_path):
    source = OfflineImageryTileSource(root=tmp_path)
    assert source.available() is False
    row = source.descriptor()
    assert row["status"] == "blocked"
    # 原因码必须精确到"元信息缺失"，而不是笼统的"不可用"。
    assert row["reason_code"] == "offline_imagery_metadata_missing"
    with pytest.raises(OfflineImageryUnavailable):
        source.metadata()


def test_tile_path_rejects_traversal_and_out_of_range(tmp_path):
    source = make_source(tmp_path)
    assert source.tile_path(14, 13748, 6758) is not None
    # 目录穿越：绝不能读到瓦片根目录之外的文件。
    assert source.tile_path(14, "..", 1) is None
    assert source.tile_path(14, 13748, "../../../../windows/win.ini") is None
    assert source.tile_path("14; rm -rf", 1, 1) is None
    # 层级越界（数据只有 9–14）。
    assert source.tile_path(8, 1, 1) is None
    assert source.tile_path(15, 1, 1) is None
    # 行列越界。
    assert source.tile_path(14, -1, 1) is None
    assert source.tile_path(14, 1 << 14, 1) is None


def test_tile_returns_none_instead_of_raising_for_absent_tiles(tmp_path):
    source = make_source(tmp_path)
    assert source.tile(14, 13748, 6758) is not None
    # 瓦片不存在是**正常**状态（数据边界），必须返回 None 而不是抛异常。
    assert source.tile(14, 13748, 6759) is None
    assert source.tile(14, 1, 1) is None


def test_offline_tile_never_touches_upstream_or_budget(tmp_path):
    """离线瓦片必须完全绕开凭证与预算闸门。"""

    calls = []

    def fetcher(url, *, headers, timeout_s, max_bytes):
        calls.append(url)
        raise AssertionError("离线瓦片绝不允许发起任何上游请求")

    source = make_source(tmp_path)
    service = Imagery3DService(
        environ={},                    # 无任何凭证
        fetcher=fetcher,
        offline_source=source,
    )
    tile = service.tile("offline_zhoushan_imagery", 14, 13748, 6758)
    assert tile.content_type == "image/png"
    assert tile.body.startswith(b"\x89PNG")
    assert calls == [], "离线瓦片不得触网"
    # 上游尝试计数必须恒为 0。
    metrics = service.metrics()
    assert metrics["process_lifetime_upstream_attempts"] == 0
    assert metrics["offline_tile_requests"] == 1
    assert metrics["offline_upstream_attempts"] == 0
    # 响应头必须声明真实来源，避免被误读为 ArcGIS World Imagery。
    assert tile.headers["X-CNS-Imagery-Source"] == "offline_zhoushan_imagery"
    assert tile.headers["X-CNS-Imagery-Origin"] == "copernicus_sentinel2_l2a_tci"
    assert tile.headers["X-CNS-Imagery-Online-Budget"] == "not_consumed"


def test_absent_offline_tile_raises_a_distinct_code(tmp_path):
    source = make_source(tmp_path)
    service = Imagery3DService(environ={}, offline_source=source)
    with pytest.raises(ImageryUnavailable) as exc:
        service.tile("offline_zhoushan_imagery", 14, 1, 1)
    # 必须与在线越界（imagery_tile_outside_scope）分开：语义不同，处理也不同。
    assert exc.value.code == "offline_imagery_tile_missing"


def test_configuration_discloses_offline_source_even_when_online_disabled(tmp_path):
    source = make_source(tmp_path)
    # 默认（无凭证、在线 disabled）：离线影像必须**依然可用**。
    service = Imagery3DService(environ={}, offline_source=source)
    config = service.configuration()
    assert config["online_allowed"] is False
    assert config["default_basemap_mode"] == "offline_imagery"
    offline = config["offline_imagery"]
    assert offline["status"] == "available"
    ids = [row["id"] for row in config["sources"]]
    assert ids[0] == "offline_zhoushan_imagery"
    # 默认**在线**源保持不变（前端据此解析在线图层），默认底图另由
    # default_basemap_mode 表达。
    assert config["default_source"] == "arcgis_world_imagery"


def test_configuration_falls_back_to_relief_when_offline_data_absent(tmp_path):
    service = Imagery3DService(
        environ={}, offline_source=OfflineImageryTileSource(root=tmp_path))
    config = service.configuration()
    assert config["default_basemap_mode"] == "offline_relief"
    assert config["offline_imagery"]["status"] == "blocked"
    assert config["offline_imagery"]["reason_code"] == "offline_imagery_metadata_missing"


@pytest.mark.skipif(not REAL_TILES.is_dir(), reason="本轮离线瓦片数据尚未生成")
def test_real_dataset_matches_its_published_metadata():
    """对**真实**数据核对：覆盖范围、层级、瓦片数、来源与许可。"""

    source = OfflineImageryTileSource(root=REAL_ROOT)
    assert source.available() is True
    meta = source.metadata()
    row = source.descriptor()

    # 覆盖范围必须真的覆盖全部相机预设与 R0005 航路。
    west, south, east, north = row["coverage_crs84"]
    assert west <= 122.10 and south <= 29.76
    assert east >= 122.31 and north >= 29.99

    # 分辨率如实：10 m，无升采样。
    assert row["native_gsd_m"] == 10.0
    assert row["upsampled"] is False
    # 层级上限必须与 10 m 原始分辨率匹配（不虚报更高层级）。
    assert row["zoom"] == [7, 14]
    assert meta["tiles"]["maxzoom"] == 14

    # 署名必须指向 Sentinel/Copernicus，**绝不能**出现 ArcGIS/Esri。
    assert "Copernicus Sentinel" in row["attribution"]
    assert "Esri" not in json.dumps(row, ensure_ascii=False)

    # 关键地物所在瓦片真实存在。
    for z, x, y in [(14, 13748, 6758), (13, 6874, 3379), (12, 3437, 1689)]:
        assert source.tile(z, x, y) is not None, f"缺少关键瓦片 {z}/{x}/{y}"


@pytest.mark.skipif(not REAL_TILES.is_dir(), reason="本轮离线瓦片数据尚未生成")
def test_real_offline_tiles_are_valid_png_and_online_budget_untouched():
    from io import BytesIO

    from PIL import Image

    source = OfflineImageryTileSource(root=REAL_ROOT)
    service = Imagery3DService(environ={}, offline_source=source)
    body = service.tile("offline_zhoushan_imagery", 14, 13748, 6758).body
    image = Image.open(BytesIO(body))
    image.load()
    assert image.size == (256, 256)
    assert image.mode in {"RGB", "RGBA"}
    assert service.metrics()["process_lifetime_upstream_attempts"] == 0
