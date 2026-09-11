from unittest.mock import patch
from pathlib import Path
import map_app


def test_ready_server_is_reused():
    with patch.object(map_app, "is_ready", return_value=True), patch.object(map_app.subprocess, "Popen") as popen:
        assert map_app.ensure_server() is None
        popen.assert_not_called()


def test_missing_qgis_is_actionable(tmp_path, monkeypatch):
    monkeypatch.setenv("CNS_QGIS_PYTHON", str(tmp_path / "missing.bat"))
    import pytest
    with pytest.raises(RuntimeError, match="未找到 QGIS"):
        map_app.find_runner()


def test_launcher_uses_package_entrypoint():
    source = Path("map_app.py").read_text(encoding="utf-8")
    assert '[str(runner), "-m", "cns_planner.map_server"]' in source
