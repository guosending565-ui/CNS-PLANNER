import json
from pathlib import Path

import pytest

from cns_planner.persistence.project_repository import ProjectRepository
from cns_planner.services.grid_service import WorkspaceGridService
from cns_planner.services.workflow import WorkflowService


def _defaults_path(tmp_path):
    target = tmp_path / "config" / "defaults.json"
    target.parent.mkdir()
    target.write_text(
        Path("cns_planner/config/defaults.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return target


def _health():
    return {
        "status": "passed",
        "population": {"status": "passed"},
        "airspace": {"status": "passed"},
        "terrain": {"status": "passed"},
        "buildings": {"status": "missing_data"},
        "property": {"status": "missing_data"},
        "loaded_layer_count": 3,
        "covered_layer_count": 3,
    }


def test_workspace_grid_uses_stable_ids_and_geojson_geometry():
    service = WorkspaceGridService(preferred_level=6)
    workspace = [120.001, 30.001, 120.02, 30.02]

    result = service.generate(workspace)

    assert result == service.generate(workspace)
    assert result["status"] == "passed"
    assert result["standard"] == "MH/T 4063.1-2026"
    assert result["id_scheme"] == "mht4063-global-index-v1"
    assert result["level"] == result["preferred_level"] == 6
    assert result["coarsened"] is False
    assert result["workspace_bbox"] == workspace
    assert result["count"] == 4
    assert [cell["grid_id"] for cell in result["cells"]] == [
        "MHT4063-L06-C00018000-RP00001800",
        "MHT4063-L06-C00018001-RP00001800",
        "MHT4063-L06-C00018000-RP00001801",
        "MHT4063-L06-C00018001-RP00001801",
    ]
    for cell in result["cells"]:
        west, south, east, north = cell["bbox"]
        assert cell["level"] == 6
        assert cell["geometry"] == {
            "type": "Polygon",
            "coordinates": [[
                [west, south],
                [east, south],
                [east, north],
                [west, north],
                [west, south],
            ]],
        }
        assert west < workspace[2] and east > workspace[0]
        assert south < workspace[3] and north > workspace[1]


def test_large_workspace_is_deterministically_coarsened():
    service = WorkspaceGridService(preferred_level=7, max_cells=10)

    result = service.generate([120.0, 30.0, 120.1, 30.1])

    assert result["preferred_level"] == 7
    assert result["level"] == 5
    assert result["coarsened"] is True
    assert result["count"] == 4


def test_workspace_change_regenerates_grid_and_roundtrips(tmp_path):
    defaults = _defaults_path(tmp_path)
    store = tmp_path / "project" / "project_state.json"
    service = WorkflowService(store, defaults)

    first = service.set_workspace([120.001, 30.001, 120.01, 30.01], _health())
    first_ids = [cell["grid_id"] for cell in first["grid"]["cells"]]
    second = service.set_workspace([120.021, 30.021, 120.03, 30.03], _health())
    second_ids = [cell["grid_id"] for cell in second["grid"]["cells"]]

    assert first["grid"]["status"] == "passed"
    assert second["grid"]["status"] == "passed"
    assert second["grid"]["workspace_bbox"] == second["workspace"]["bbox"]
    assert second["result_statuses"]["grid"] == "passed"
    assert second_ids != first_ids
    restored = WorkflowService(store, defaults).snapshot()
    assert restored["grid"] == second["grid"]
    assert restored["result_statuses"]["grid"] == "passed"

    cleared = service.clear_workspace()
    assert cleared["grid"] is None
    assert cleared["result_statuses"]["grid"] == "not_calculated"
    assert service.grid_snapshot()["cells"] == []


def test_legacy_schema_v2_project_without_grid_is_opened_and_backfilled(tmp_path):
    defaults = _defaults_path(tmp_path)
    store = tmp_path / "legacy-project" / "project_state.json"
    service = WorkflowService(store, defaults)
    state = service.set_workspace([120.001, 30.001, 120.01, 30.01], _health())
    project_id = state["project"]["project_id"]
    repository = ProjectRepository(store)
    legacy_document = repository.load()
    legacy_document.pop("grid")
    legacy_document["result_statuses"].pop("grid")
    repository.save(legacy_document)

    restored = WorkflowService(store, defaults)

    assert restored.state["schema_version"] == 2
    assert restored.state["project"]["project_id"] == project_id
    assert restored.state["grid"]["status"] == "passed"
    assert restored.state["grid"]["workspace_bbox"] == legacy_document["workspace"]["bbox"]
    assert restored.state["result_statuses"]["grid"] == "passed"
    restored.save()
    assert ProjectRepository(store).load()["grid"]["count"] > 0


def test_frontend_exposes_grid_layer_and_clickable_grid_id():
    html = Path("cns_planner/web/index.html").read_text(encoding="utf-8")
    javascript = Path("cns_planner/web/app.js").read_text(encoding="utf-8")

    assert 'id="gridLayer"' in html
    assert 'id="gridInfo"' in html
    assert 'id="gridNotice"' in html
    assert 'src="/grid_theme.js"' in html
    assert "drawStandardGrid()" in javascript
    assert "drawGridThemes()" in javascript
    assert "info.textContent=formatGridDetails(item)" in javascript
    assert "population.cells?.[gridId]" in javascript
    assert "terrain.cells?.[gridId]" in javascript
    assert "gridRenderCache" in javascript
    assert "api('/api/workspace/grid')" in javascript
    assert "api('/api/workspace/grid/attributes')" in javascript
    assert 'id="gridOutlineToggle"' in javascript
    assert 'type="radio" name="gridThemeMode"' in javascript
    assert 'id="gridThemeNone"' in javascript
    assert 'id="gridPopulationTheme"' in javascript
    assert 'id="gridTerrainTheme"' in javascript
    assert "valid_sample_count" in javascript
    assert "mean_elevation" in javascript
    assert "visibleLonLatBounds()" in javascript
    assert "fitLonLatBbox(flow.workspace?.bbox)" in javascript
    assert "grid:'标准网格'" in javascript
