from pathlib import Path

import pytest

from cns_planner.algorithms.coverage.v1 import CoveragePlannerV1
from cns_planner.algorithms.coverage_planner import CoveragePlannerV1 as LegacyCoveragePlannerV1
from cns_planner.algorithms.route.v1 import RoutePlannerV1
from cns_planner.algorithms.route_planner import RoutePlannerV1 as LegacyRoutePlannerV1
from cns_planner.application.project_state import blank_project, normalize_project
from cns_planner.api.security import same_origin
from cns_planner.data.registry import DEFINITIONS


def test_legacy_algorithm_imports_are_compatibility_aliases():
    assert LegacyRoutePlannerV1 is RoutePlannerV1
    assert LegacyCoveragePlannerV1 is CoveragePlannerV1


def test_project_state_rejects_unknown_schema_without_mutation():
    project = blank_project({})
    invalid = {**project, "schema_version": 999}
    with pytest.raises(ValueError, match="schema"):
        normalize_project(invalid, {})
    assert invalid["schema_version"] == 999


def test_registry_declares_present_and_future_source_categories():
    source_ids = {item.id for item in DEFINITIONS}
    assert {"basemap", "airspace", "population", "terrain", "buildings", "property_exposure", "obstacles", "infrastructure", "towers", "traffic", "existing_cns", "candidate_sites", "reference_landing_sites", "equipment_reference_catalog"} <= source_ids


def test_local_security_is_port_independent_but_same_origin():
    assert same_origin({"Host": "127.0.0.1:54321"})
    assert same_origin({"Host": "localhost:54321", "Origin": "http://localhost:54321"})
    assert not same_origin({"Host": "localhost:54321", "Origin": "https://example.com"})


def test_central_entrypoints_remain_thin_and_frontend_is_module_based():
    root = Path("cns_planner")
    assert len((root / "map_server.py").read_text(encoding="utf-8").splitlines()) <= 190
    assert len((root / "services/workflow.py").read_text(encoding="utf-8").splitlines()) <= 10
    assert len((root / "web/js/main.js").read_text(encoding="utf-8").splitlines()) <= 450
    assert "import './js/main.js'" in (root / "web/app.js").read_text(encoding="utf-8")
    for relative in ("api/client.js", "state/store.js", "map/renderer.js", "map/interaction.js", "sources/source_center.js"):
        assert (root / "web/js" / relative).is_file()
