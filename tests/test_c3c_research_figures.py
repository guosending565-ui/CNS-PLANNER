"""C3-C：QGIS 六类研究专题图的 FigureSpec 契约（不启动 QGIS 渲染）。"""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import render_r0005_research_figures as figures  # noqa: E402


def _fixture(n_actions=4):
    route = [[122.2672, 29.8666], [122.2236, 29.8417], [122.1883, 29.8194]]
    sites = [
        {"site_id": "NEW-422750", "kind": "planned_new_site",
         "coordinate": [122.2004, 29.8326], "subsystems": ["C", "N", "S"],
         "devices": [{"subsystem": "C", "radius_m": 2000.0},
                     {"subsystem": "N", "radius_m": 1000.0},
                     {"subsystem": "S", "radius_m": 5000.0}]},
        {"site_id": "takeoff_landing_site:N006", "kind": "existing_host",
         "coordinate": [122.1883, 29.8194], "subsystems": ["C", "S"],
         "devices": [{"subsystem": "C", "radius_m": 2000.0},
                     {"subsystem": "S", "radius_m": 5000.0}]},
    ]
    actions = [
        {"action_id": "add_navigation_integrity_monitor:R0005:origin:N005",
         "site_id": "N005", "coordinate": [122.2672, 29.8666],
         "planner_family": "endpoint_integrity_monitor", "segment_index": None},
        {"action_id": "add_navigation_integrity_monitor:R0005:destination:N006",
         "site_id": "N006", "coordinate": [122.1883, 29.8194],
         "planner_family": "endpoint_integrity_monitor", "segment_index": None},
        {"action_id": "middle-1", "site_id": "NEW-422750",
         "coordinate": [122.2004, 29.8326],
         "planner_family": "navigation_integrity_endpoint_middle_reuse_v1",
         "segment_index": 4},
        {"action_id": "middle-2", "site_id": "NEW-425000",
         "coordinate": [122.2236, 29.8417],
         "planner_family": "navigation_integrity_endpoint_middle_reuse_v1",
         "segment_index": 3},
    ][:n_actions]
    manifest = {"run_id": "run-test", "route_id": "R0005"}
    identity = {
        "run_id": "run-test", "input_version": "in-test",
        "result_list_sha256": "r" * 64,
        "counts": {"sites": 2, "devices": 5, "existing_hosts": 1,
                   "planned_new_sites": 1, "n_actions": len(actions)},
    }
    land = [[[122.18, 29.80], [122.28, 29.80], [122.28, 29.88],
             [122.18, 29.88], [122.18, 29.80]]]
    sea = [[[122.10, 29.75], [122.35, 29.75], [122.35, 29.95],
            [122.10, 29.95], [122.10, 29.75]]]
    return manifest, identity, land, {}, sea, {}, route, sites, actions


def test_builds_six_research_figures_with_identity_and_a4_layout():
    manifest, identity, land, holes, sea, sea_holes, route, sites, actions = _fixture(4)
    specs = figures.build_specs(manifest, identity, land, holes, sea, sea_holes,
                                route, sites, actions)
    assert set(specs) == {
        "cns_communication_v1", "cns_navigation_v1", "cns_surveillance_rid_v1",
        "cns_surveillance_radar_v1", "cns_combined_v1", "route_overview_v1",
    }
    for spec in specs.values():
        assert spec.layout["document_width_mm"] == 210.0
        assert spec.layout["document_height_mm"] == 297.0
        assert spec.metadata["run_identity"]["run_id"] == "run-test"
        assert "4 个 N 动作" in spec.subtitle
        assert spec.metadata["formal_release_allowed"] is False


def test_radar_figure_has_no_coverage_geometry():
    manifest, identity, land, holes, sea, sea_holes, route, sites, actions = _fixture(4)
    specs = figures.build_specs(manifest, identity, land, holes, sea, sea_holes,
                                route, sites, actions)
    radar = specs["cns_surveillance_radar_v1"]
    assert not any(layer.geometry_type == "polygon" and layer.layer_key.startswith(
        "cns_coverage") for layer in radar.layers)
    declared = [layer for layer in radar.layers
                if layer.layer_key == "cns_radar_limitation"]
    assert len(declared) == 1
    assert declared[0].geometry_type == "none" and declared[0].feature_count == 0
    assert any("not_calculated" in annotation.title or "能力限制" in annotation.title
               for annotation in radar.annotations)


def test_n_action_count_changes_with_run_input():
    four = _fixture(4)
    three = _fixture(3)
    specs_four = figures.build_specs(*four)
    specs_three = figures.build_specs(*three)
    nav_four = [layer for layer in specs_four["cns_navigation_v1"].layers
                if layer.layer_key == "cns_nav_proposal"][0]
    nav_three = [layer for layer in specs_three["cns_navigation_v1"].layers
                 if layer.layer_key == "cns_nav_proposal"][0]
    assert nav_four.feature_count == 4
    assert nav_three.feature_count == 3
    assert "3 个 N 动作" in specs_three["cns_navigation_v1"].subtitle
