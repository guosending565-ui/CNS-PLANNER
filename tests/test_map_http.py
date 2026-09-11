"""Run against the local QGIS service with CNS_MAP_TESTS=1."""
import json
import os
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from urllib.error import HTTPError
import pytest

pytestmark = pytest.mark.skipif(os.environ.get("CNS_MAP_TESTS") != "1", reason="requires running QGIS map service")
BASE = "http://127.0.0.1:8765"


def test_idle_browser_connection_does_not_block_health():
    import socket
    import time
    with socket.create_connection(("127.0.0.1", 8765), timeout=3):
        start = time.monotonic()
        health = json.loads(call("/api/health"))
        assert health["service"] == "cns-map"
        assert time.monotonic() - start < 3


def test_stale_view_rejected_and_online_separated():
    state = json.loads(call("/api/state"))
    assert all("template" not in source for source in state["online_sources"])
    q = {"bbox": ",".join(map(str,state["bounds"])), "w": 160, "h": 100, "client": "test-cancel", "seq": 20}
    first = call("/api/render?" + urlencode(q))
    second = call("/api/render?" + urlencode({**q, "online": 1}))
    assert first == second  # Online tiles must never block or enter QGIS rendering.
    with pytest.raises(HTTPError) as result:
        call("/api/render?" + urlencode({**q, "seq": 19}))
    assert result.value.code == 400


def call(path, data=None, token=None):
    headers = {"X-CNS-Token": token} if token else {}
    if data is not None:
        headers["Content-Type"] = "application/json"
    with urlopen(Request(BASE + path, data=json.dumps(data).encode() if data is not None else None, headers=headers), timeout=40) as response:
        return response.read()


def test_real_layers_and_population_render():
    state = json.loads(call("/api/state"))
    assert not state["error"]
    assert state["layers"] and state["population"]["width"] > 0
    assert state["data_health"]["stage"] == "P1"
    assert len(state["data_sources"]) >= 13
    q = {"bbox": ",".join(map(str,state["bounds"])), "w": 420, "h": 300, "online": 0}
    air = call("/api/render?" + urlencode(q))
    pop = call("/api/render?" + urlencode({**q, "pop": 1}))
    assert air.startswith(b"\x89PNG") and pop.startswith(b"\x89PNG")
    assert len(air) > 1000 and air != pop


def test_file_browsing_and_invalid_replacement_preserves_state():
    before = json.loads(call("/api/state"))
    items = json.loads(call("/api/browse?" + urlencode({"kind": "population", "path": before["paths"]["population"]}), token=before["token"]))
    assert any(e["path"] == before["paths"]["population"] for e in items["entries"])
    with pytest.raises(HTTPError) as result:
        call("/api/sources", {**before["paths"], "basemap": "C:/nonexistent-test-map.qgz"}, before["token"])
    assert result.value.code == 400
    after = json.loads(call("/api/state"))
    assert before["revision"] == after["revision"] and before["paths"] == after["paths"]


def test_data_source_endpoints_validate_without_applying():
    before = json.loads(call("/api/state"))
    sources = json.loads(call("/api/data-sources"))
    health = json.loads(call("/api/data-health"))
    grid = json.loads(call("/api/workspace/grid"))
    attributes = json.loads(call("/api/workspace/grid/attributes"))
    assert {item["id"] for item in sources} >= {"basemap", "population", "online_map", "geocoder"}
    assert health["stage"] == "P1"
    assert grid["standard"] == "MH/T 4063.1-2026"
    assert isinstance(grid["cells"], list)
    assert set(attributes) == {
        "population", "terrain", "airspace", "buildings",
        "property_exposure", "infrastructure", "towers", "traffic", "conflict",
    }
    candidate = json.loads(call("/api/data-sources/validate", before["paths"], before["token"]))
    after = json.loads(call("/api/state"))
    assert candidate["paths"] == before["paths"]
    assert after["revision"] == before["revision"]


def test_reject_unauthenticated_file_access_and_invalid_extent():
    with pytest.raises(HTTPError) as result:
        call("/api/browse")
    assert result.value.code == 403
    with pytest.raises(HTTPError) as result:
        call("/api/render?bbox=NaN,0,1,2&w=100&h=100")
    assert result.value.code == 400
