from io import BytesIO
from unittest.mock import patch
import pytest
from cns_planner.tile_cache import TileCache


def test_tile_cache_reuses_download_and_is_bounded():
    png = b"\x89PNG\r\n\x1a\n" + b"x" * 100
    cache = TileCache(max_bytes=150)
    with patch("cns_planner.tile_cache.urlopen", side_effect=lambda *a, **k: BytesIO(png)) as request:
        assert cache.get("https://example.test/{z}/{x}/{y}", 2, 1, 1)[0] == png
        cache.get("https://example.test/{z}/{x}/{y}", 2, 1, 1)
        assert request.call_count == 1
        cache.get("https://example.test/{z}/{x}/{y}", 2, 2, 1)
        assert cache.size <= 150


def test_failure_does_not_expose_token():
    with patch("cns_planner.tile_cache.urlopen", side_effect=OSError("token=secret")):
        with pytest.raises(ValueError, match="本地图层仍可操作") as exc:
            TileCache().get("https://example.test/{z}/{x}/{y}?token=secret", 2, 1, 1)
    assert "secret" not in str(exc.value)


def test_invalid_tile_is_rejected_before_network():
    with patch("cns_planner.tile_cache.urlopen") as request:
        with pytest.raises(ValueError):
            TileCache().get("https://example.test/{z}/{x}/{y}", 2, 5, 0)
        request.assert_not_called()
