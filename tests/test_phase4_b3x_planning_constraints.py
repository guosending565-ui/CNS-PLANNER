from copy import deepcopy
import collections
import inspect
from pathlib import Path

import pytest

from cns_planner.application.layered_operational_adoption_service import (
    LayeredOperationalAdoptionService,
)
from cns_planner.application.planning_constraint_field_service import (
    generate_planning_constraint_field,
)
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.planning_constraint_field import stable_constraint_fingerprint
from cns_planner.gis.fine_environment_adapter import _FabdemRasterBase
from cns_planner.gis.metric_crs import resolve_metric_crs
from cns_planner.layered_route_planner.planner import GridGraph as LayeredGridGraph
from cns_planner.layered_route_planner.theta_star_v2 import LayeredRiskAwareThetaStarV2
from cns_planner.planning.grid_graph import GridGraph
from cns_planner.persistence.project_compaction import compact_and_store, restore_compacted_results
from cns_planner.route_planner.risk_aware_v2 import GridGraph as CompatibilityGridGraph
from cns_planner.route_planner_v3.continuous_validators import (
    validate_buildings as compatibility_validate_buildings,
)
from cns_planner.validation.continuous_validators import (
    MetricRoute, validate_buildings, validate_restricted_areas, validate_towers,
)


VREF = "egm2008_orthometric"
DEFAULTS = Path(__file__).parents[1] / "cns_planner" / "config" / "defaults.json"


def confirmed_restricted_collection(items=()):
    present = {
        str(item.get("domain") or "critical_site") for item in items if isinstance(item, dict)
    }
    return {
        "source": "audited-fixture", "evidence": [{"record": "B3X"}],
        "domain_states": {
            domain: "confirmed_present" if domain in present else "confirmed_none"
            for domain in ("airspace", "critical_site")
        },
        "items": list(items),
    }


def layer(identifier="ALT-080", altitude=80.0):
    return {
        "altitude_layer_id": identifier, "nominal_altitude_m": altitude,
        "vertical_reference": VREF, "confirmed": True, "status": "confirmed",
    }


def one_cell():
    return {"level": 8, "crs": "OGC:CRS84", "cells": [{
        "grid_id": "G0", "level": 8, "column": 0, "row": 0,
        "bbox": [0.0, 0.0, 1.0, 1.0], "center": [0.5, 0.5],
    }]}


def resolved_inputs(*, terrain=10.0, building=None):
    return {
        "terrain_by_cell": {"G0": {
            "data_status": "passed", "surface_elevation_max_egm2008_m": terrain,
            "vertical_reference": VREF,
        }},
        "buildings_by_cell": {"G0": building or {
            "data_status": "passed", "building_count": 0,
        }},
        "tower_obstacle_profiles": {"status": "passed", "items": {}},
        "restricted_areas": confirmed_restricted_collection(),
        "policies": {
            "terrain_vertical_clearance_m": 10.0,
            "building_vertical_clearance_m": 10.0,
            "tower_vertical_clearance_m": 10.0,
        },
    }


def field_for(altitude_layer=None, **overrides):
    inputs = resolved_inputs()
    inputs.update(overrides)
    return generate_planning_constraint_field(
        altitude_layer=altitude_layer or layer(), grid=one_cell(),
        source_fingerprints={"fixture": "b3x"}, workspace_identity={"id": "W"},
        **inputs,
    )


def metric_route(altitude=100.0):
    return MetricRoute({
        "horizontal_geometry": {"linearized": {
            "linestring_metric": [[0.0, 0.0], [100.0, 0.0]],
            "curve_chord_error_m": 0.0,
        }},
        "primitives": [{
            "primitive_id": "s0", "kind": "line", "distance_start_m": 0.0,
            "distance_end_m": 100.0, "horizontal_length_m": 100.0,
            "z_start_egm2008_m": altitude, "z_end_egm2008_m": altitude,
        }],
    })


def test_neutral_grid_graph_is_the_single_definition():
    assert CompatibilityGridGraph is GridGraph
    assert LayeredGridGraph is GridGraph
    graph = GridGraph(one_cell()["cells"])
    assert graph.containing_cell([0.5, 0.5]) == "G0"


def test_neutral_continuous_validator_is_the_canonical_implementation():
    assert compatibility_validate_buildings is validate_buildings


def test_building_height_status_unknown_never_passes():
    result = validate_buildings(
        metric_route(),
        evidence={"available": True, "buildings": [{
            "building_id": "B", "height_m": 10.0, "height_status": "unknown",
            "ground_elevation_max_egm2008_m": 0.0,
            "ring_metric": [[40, -2], [60, -2], [60, 2], [40, 2], [40, -2]],
        }]},
        policy={"building_horizontal_clearance_m": 0.0, "building_vertical_clearance_m": 5.0},
    )
    assert result["status"] == "unresolved"
    assert result["unresolved"][0]["reason_id"] == "building_height_status_unresolved"


def test_multipolygon_later_part_ground_can_fail_clearance():
    result = validate_buildings(
        metric_route(),
        evidence={"available": True, "buildings": [{
            "building_id": "MP", "height_m": 5.0, "height_status": "confirmed",
            "ring_parts_metric": [
                [[10, -2], [20, -2], [20, 2], [10, 2], [10, -2]],
                [[50, -2], [60, -2], [60, 2], [50, 2], [50, -2]],
            ],
            "ground_elevation_by_part_egm2008_m": [0.0, 90.0],
        }]},
        policy={"building_horizontal_clearance_m": 0.0, "building_vertical_clearance_m": 10.0},
    )
    assert result["status"] == "failed"
    assert result["violations"][0]["evidence"]["footprint_part_index"] == 1


def test_metric_crs_resolver_is_extent_aware_and_never_falls_back_to_32651():
    new_york = resolve_metric_crs(geographic_bounds=[-74.1, 40.6, -73.8, 40.9])
    assert new_york["status"] == "resolved"
    assert new_york["authority"] == "EPSG:32618"
    wrong = resolve_metric_crs(
        explicit_crs="EPSG:32651", geographic_bounds=[-74.1, 40.6, -73.8, 40.9],
    )
    assert wrong["status"] == "unknown" and wrong["authority"] is None


def test_fabdem_base_has_one_non_recursive_resolution_implementation():
    source = inspect.getsource(_FabdemRasterBase)
    assert source.count("def effective_resolution_m(") == 1
    assert source.count("def effective_resolution_detail(") == 1
    assert "return self.effective_resolution_m()" not in source


def test_resolved_but_unconfirmed_tower_is_unknown_not_blocked_or_passed():
    towers = {"status": "passed", "items": {"T1": {
        "tower_id": "T1", "longitude": 0.5, "latitude": 0.5,
        "status": "resolved", "confirmed": False,
        "tower_top_status": "resolved_unconfirmed", "tower_top_orthometric_m": 75.0,
    }}}
    field = field_for(tower_obstacle_profiles=towers)
    assert field["cells"][0]["outcome"] == "unknown"
    assert field["cells"][0]["blocked_by"] == []
    continuous = validate_towers(
        metric_route(100.0),
        evidence={"status": "passed", "towers": [{
            **towers["items"]["T1"], "point_metric": [50.0, 0.0],
        }]},
        policy={"tower_horizontal_clearance_m": 5.0, "tower_vertical_clearance_m": 10.0},
    )
    assert continuous["status"] == "unresolved"


def test_confirmed_tower_blocks_field_and_continuous_route():
    tower = {
        "tower_id": "T1", "longitude": 0.5, "latitude": 0.5,
        "status": "resolved", "confirmed": True, "tower_top_status": "confirmed",
        "tower_top_orthometric_m": 75.0, "confirmation_authority": "engineering",
    }
    field = field_for(tower_obstacle_profiles={"status": "passed", "items": {"T1": tower}})
    assert field["cells"][0]["outcome"] == "blocked"
    assert "tower" in field["cells"][0]["blocked_by"]
    continuous = validate_towers(
        metric_route(80.0), evidence={"status": "passed", "towers": [{
            **tower, "point_metric": [50.0, 0.0],
        }]}, policy={"tower_horizontal_clearance_m": 5.0, "tower_vertical_clearance_m": 10.0},
    )
    assert continuous["status"] == "failed"


def test_restricted_point_without_protection_geometry_does_not_invent_radius():
    point = {
        "feature_id": "P", "name": "airport point", "category": "airport",
        "domain": "critical_site", "geometry": {"type": "Point", "coordinates": [0.5, 0.5]},
        "geometry_crs": "OGC:CRS84", "constraint_type": "hard_exclusion",
        "lower_altitude_m": 0.0, "upper_altitude_m": 200.0,
        "vertical_reference": VREF, "confirmed": True,
        "source": "audited-fixture", "evidence": [{"record": "P"}],
    }
    field = field_for(restricted_areas=confirmed_restricted_collection([point]))
    assert field["cells"][0]["outcome"] == "unknown"
    assert field["cells"][0]["blocked_by"] == []


def test_empty_restricted_list_is_not_silently_promoted_to_confirmed_none():
    field = field_for(restricted_areas=[])
    assert field["cells"][0]["outcome"] == "unknown"
    domains = {item["domain"] for item in field["cells"][0]["unknown_reasons"]}
    assert {"airspace", "critical_site"} <= domains


def restricted_polygon(constraint_type="hard_exclusion"):
    polygon = {
        "type": "Polygon", "coordinates": [[[0.4, 0.4], [0.6, 0.4], [0.6, 0.6],
                                                 [0.4, 0.6], [0.4, 0.4]]],
    }
    return {
        "feature_id": "A", "name": "protected", "category": "audited",
        "domain": "airspace", "geometry": polygon, "protection_geometry": polygon,
        "geometry_crs": "OGC:CRS84", "constraint_type": constraint_type,
        "lower_altitude_m": 0.0, "upper_altitude_m": 200.0,
        "vertical_reference": VREF, "confirmed": True,
        "source": "audited-fixture", "evidence": [{"record": "A"}],
    }


def test_confirmed_hard_polygon_blocks_field_and_independent_continuous_validation():
    area = restricted_polygon()
    field = field_for(restricted_areas=confirmed_restricted_collection([area]))
    assert "airspace" in field["cells"][0]["blocked_by"]
    metric_area = deepcopy(area)
    metric_area["planning_geometry_metric"] = {
        "type": "Polygon", "coordinates": [[[40, -5], [60, -5], [60, 5], [40, 5], [40, -5]]],
    }
    result = validate_restricted_areas(
        metric_route(80.0), evidence={"status": "passed", "areas": [metric_area]},
        policy={}, vertical_reference=VREF,
    )
    assert result["status"] == "failed"


def test_advisory_polygon_never_hard_blocks():
    area = restricted_polygon("advisory")
    field = field_for(restricted_areas=confirmed_restricted_collection([area]))
    assert field["cells"][0]["outcome"] == "pass"
    metric_area = deepcopy(area)
    metric_area["planning_geometry_metric"] = {
        "type": "Polygon", "coordinates": [[[40, -5], [60, -5], [60, 5], [40, 5], [40, -5]]],
    }
    result = validate_restricted_areas(
        metric_route(80.0), evidence={"status": "passed", "areas": [metric_area]},
        policy={}, vertical_reference=VREF,
    )
    assert result["status"] == "passed"


def test_terrain_nodata_is_unknown_never_zero():
    field = field_for(terrain_by_cell={"G0": {
        "data_status": "unknown", "surface_elevation_max_egm2008_m": None,
        "reason": "nodata",
    }})
    assert field["cells"][0]["outcome"] == "unknown"
    assert field["cells"][0]["unknown_reasons"][0]["domain"] == "terrain"


def test_multiple_and_custom_altitude_layers_are_parameterized_and_fingerprinted():
    inputs = resolved_inputs(terrain=75.0)
    fields = {
        identifier: generate_planning_constraint_field(
            altitude_layer=layer(identifier, altitude), grid=one_cell(),
            source_fingerprints={"terrain": "same"}, workspace_identity={"id": "same"},
            **inputs,
        )
        for identifier, altitude in (
            ("ALT-060", 60.0), ("ALT-080", 80.0), ("ALT-100", 100.0), ("ALT-135", 135.0),
        )
    }
    assert fields["ALT-060"]["cells"][0]["outcome"] == "blocked"
    assert fields["ALT-080"]["cells"][0]["outcome"] == "blocked"
    assert fields["ALT-100"]["cells"][0]["outcome"] == "pass"
    assert fields["ALT-135"]["cells"][0]["outcome"] == "pass"
    assert len({item["constraint_field_fingerprint"] for item in fields.values()}) == 4


def theta_grid(columns=3, rows=3):
    cells = []
    for column in range(columns):
        for row in range(rows):
            cells.append({
                "grid_id": f"MHT-L8-C{column}-RP{row}", "level": 8,
                "column": column, "row": row,
                "bbox": [float(column), float(row), float(column + 1), float(row + 1)],
                "center": [column + 0.5, row + 0.5],
            })
    return {"level": 8, "cells": cells}


def theta_plan(constraint_field=None):
    grid = theta_grid()
    cells = grid["cells"]
    return LayeredRiskAwareThetaStarV2().plan(
        request={
            "status": "confirmed", "scenario_route_id": "R", "altitude_layer_id": "ALT-100",
            "source": "test", "confirmed": True,
        },
        scenario_route={"route_id": "R", "start": [0.5, 1.5], "end": [2.5, 1.5]},
        grid=grid,
        layer_mask={
            "status": "passed", "mask_fingerprint": "legacy-mask",
            "cruise_altitude": {
                "status": "confirmed", "altitude_egm2008_m": 100.0,
                "vertical_reference": VREF,
            },
            "cells": {cell["grid_id"]: {"status": "feasible"} for cell in cells},
        },
        grid_risk_v2={}, feasibility_policy={}, building_clearance_policy={},
        population_shelter={
            "cells": {cell["grid_id"]: {"risk_index": 0.5} for cell in cells},
        },
        constraint_field=constraint_field,
    )


def theta_field(outcomes, *, allow_unknown=False, fingerprint="field-a"):
    return {
        "altitude_layer_id": "ALT-100", "nominal_altitude_m": 100.0,
        "vertical_reference": VREF, "constraint_field_fingerprint": fingerprint,
        "unknown_policy": {"allow_unknown_for_provisional": allow_unknown},
        "cells": [{
            "grid_id": cell["grid_id"], "outcome": outcomes.get(cell["grid_id"], "pass"),
            "blocked_by": ["terrain"] if outcomes.get(cell["grid_id"]) == "blocked" else [],
            "unknown_reasons": ([{"domain": "terrain", "reason": "fixture"}]
                                if outcomes.get(cell["grid_id"]) == "unknown" else []),
        } for cell in theta_grid()["cells"]],
    }


def test_theta_star_and_supercover_never_cross_blocked_constraint_cells():
    middle_column = {f"MHT-L8-C1-RP{row}": "blocked" for row in range(3)}
    blocked = theta_plan(theta_field(middle_column))
    assert blocked["status"] == "no_path"

    detour = theta_plan(theta_field({"MHT-L8-C1-RP1": "blocked"}))
    assert detour["status"] == "candidate"
    crossed = {
        entry["grid_id"] for segment in detour["los_segments"]
        for entry in segment["traversed_cells"]
    }
    assert "MHT-L8-C1-RP1" not in crossed
    assert detour["search_statistics"]["rejected_terrain"] > 0


def test_unknown_provisional_policy_warns_and_operational_adoption_rejects():
    candidate = theta_plan(theta_field(
        {"MHT-L8-C1-RP1": "unknown"}, allow_unknown=True,
    ))
    assert candidate["status"] == "candidate"
    assert candidate["completion_status"] == "completed_with_warnings"
    assert candidate["unknown_constraint_count"] > 0
    assert candidate["operational_adoption_allowed"] is False
    candidate["current_applicability"] = "current"

    class ValidationFacade:
        def _select_current_candidate(self, payload):
            return candidate

        def _current_risk_profile(self, selected):
            return {"status": "passed"}

        def _source_readiness(self):
            return {"status": "ready"}

    adoption = object.__new__(LayeredOperationalAdoptionService)
    adoption.validations = ValidationFacade()
    gate = adoption._gate({
        "status": "validated_candidate", "current_applicability": "current",
        "source_type": "configured_real_sources", "candidate": {"candidate_id": "x"},
        "route": {"path_crs": "OGC:CRS84", "path": [[0, 0], [1, 1]]},
    })
    assert gate["eligible"] is False
    assert "candidate_traverses_unknown_constraints" in gate["reasons"]


def test_all_pass_field_preserves_path_cost_but_changes_candidate_fingerprint():
    legacy = theta_plan(None)
    first = theta_plan(theta_field({}, fingerprint="field-a"))
    second = theta_plan(theta_field({}, fingerprint="field-b"))
    assert legacy["status"] == first["status"] == "candidate"
    assert first["path"] == legacy["path"]
    assert first["optimization_cost"] == pytest.approx(legacy["optimization_cost"])
    assert first["turn_statistics"] == legacy["turn_statistics"]
    assert first["distance_m"] == pytest.approx(legacy["distance_m"])
    assert first["candidate_fingerprint"] != legacy["candidate_fingerprint"]
    assert first["candidate_fingerprint"] != second["candidate_fingerprint"]


def test_constraint_fingerprint_changes_with_unknown_policy_and_grid_identity():
    base = {"layer": "ALT-X", "grid": "G", "unknown": False}
    assert stable_constraint_fingerprint(base) != stable_constraint_fingerprint({
        **base, "unknown": True,
    })
    assert stable_constraint_fingerprint(base) != stable_constraint_fingerprint({
        **base, "grid": "G2",
    })


def test_altitude_layer_change_changes_constraint_field_fingerprint_and_outcome():
    """同一批 source/policy，只改 altitude layer ⇒ 指纹与三态结果都必须变。"""

    grid = one_cell()
    shared = resolved_inputs(terrain=75.0)
    policy = shared["policies"]
    fields = {}
    for identifier, altitude in (("ALT-080", 80.0), ("ALT-100", 100.0)):
        fields[identifier] = generate_planning_constraint_field(
            altitude_layer=layer(identifier, altitude), grid=grid,
            terrain_by_cell=shared["terrain_by_cell"],
            buildings_by_cell=shared["buildings_by_cell"],
            tower_obstacle_profiles=shared["tower_obstacle_profiles"],
            restricted_areas=shared["restricted_areas"], policies=policy,
            source_fingerprints={"terrain": "same-source-v1"},
            workspace_identity={"id": "same"},
        )
    assert fields["ALT-080"]["cells"][0]["outcome"] == "blocked"
    assert fields["ALT-100"]["cells"][0]["outcome"] == "pass"
    assert (
        fields["ALT-080"]["constraint_field_fingerprint"]
        != fields["ALT-100"]["constraint_field_fingerprint"]
    )
    # 同一个高度层重复生成必须完全一致（指纹是稳定/可复现的）。
    repeat = generate_planning_constraint_field(
        altitude_layer=layer("ALT-080", 80.0), grid=grid,
        terrain_by_cell=shared["terrain_by_cell"],
        buildings_by_cell=shared["buildings_by_cell"],
        tower_obstacle_profiles=shared["tower_obstacle_profiles"],
        restricted_areas=shared["restricted_areas"], policies=policy,
        source_fingerprints={"terrain": "same-source-v1"},
        workspace_identity={"id": "same"},
    )
    assert repeat["constraint_field_fingerprint"] == fields["ALT-080"]["constraint_field_fingerprint"]


def test_los_traversal_never_crosses_a_blocked_or_uncovered_cell():
    """supercover LOS 逐格过闸：blocked 格与字段未覆盖格都必须让 LOS 失败。"""

    from cns_planner.layered_route_planner.theta_star_v2 import GridIndexMap, line_of_sight

    graph = GridGraph(theta_grid()["cells"])
    index_map = GridIndexMap(graph)
    middle = "MHT-L8-C1-RP1"
    corner_start, corner_end = "MHT-L8-C0-RP0", "MHT-L8-C2-RP2"

    def gate_for(outcome_map, *, allow_unknown):
        field_cells = {
            str(item.get("grid_id")): item
            for item in theta_field(outcome_map, allow_unknown=allow_unknown)["cells"]
        }

        def gate(grid_id):
            constraint = field_cells.get(grid_id)
            if not isinstance(constraint, dict):
                return None if allow_unknown else {
                    "domain": "unknown", "reason_code": "cell_outside_planning_constraint_field",
                }
            outcome = str(constraint.get("outcome") or "unknown")
            if outcome == "blocked":
                return {"domain": "terrain", "reason_code": "planning_constraint_field_blocked"}
            if outcome == "unknown" and not allow_unknown:
                return {"domain": "unknown", "reason_code": "planning_constraint_field_unknown"}
            return None

        return gate

    def los(grid_id, outcome_map, *, allow_unknown, statistics=None):
        # ``line_of_sight`` 的 statistics 是该函数自己的计数器契约：未知键一律取 0。
        counters = collections.defaultdict(int)
        if statistics is not None:
            counters.update(statistics)
        return line_of_sight(
            graph, index_map,
            source_id=grid_id, target_id=grid_id,
            source_point=graph.centers[grid_id],
            target_point=graph.centers[grid_id],
            gate=gate_for(outcome_map, allow_unknown=allow_unknown),
            altitude_m=100.0, regulatory={}, risk_indices={},
            statistics=counters,
        )

    # blocked 是硬约束：任何 unknown policy 都不能让穿过它的 LOS 成立。
    blocked = los(middle, {middle: "blocked"}, allow_unknown=True)
    assert blocked["ok"] is False
    assert blocked["rejection"]["grid_id"] == middle
    assert blocked["rejection"]["domain"] == "terrain"
    assert blocked["segment_length_m"] is not None
    assert not blocked["cells"], "被拒绝的 LOS 不得返回已计价 cell"

    # unknown + allow_unknown=False ⇒ fail-closed。
    strict = los(middle, {middle: "unknown"}, allow_unknown=False)
    assert strict["ok"] is False
    assert strict["rejection"]["grid_id"] == middle

    # unknown + 显式 provisional policy 时 feasibility gate 本身放行；本 fixture 不给 risk
    # evidence，因此 LOS 仍然必须在风险证据缺失处 fail-closed（另一条 unknown 语义）。
    # "unknown + provisional policy ⇒ 可生成 candidate 且必须带 warning" 由
    # ``test_unknown_provisional_policy_warns_and_operational_adoption_rejects`` 端到端证明。
    provisional = los(middle, {middle: "unknown"}, allow_unknown=True)
    assert provisional["ok"] is False
    assert provisional["rejection"]["reason_code"] == "risk_evidence_unresolved_inside_shortcut"


def test_constraint_cells_use_existing_content_addressed_sidecar(tmp_path):
    field = field_for()
    state = {
        "grid_risk_v2": {"status": "not_calculated", "cells": {}},
        "layered_route_candidates": {
            "status": "not_calculated", "items": [], "masks": {},
        },
        "planning_constraint_fields": {
            "status": "passed", "count": 1, "items": [field],
        },
    }
    project_path = tmp_path / "current_project.json"
    compact = compact_and_store(state, project_path)
    assert compact["planning_constraint_fields"]["items"][0].get("cells") is None
    assert compact["result_index"]["planning_constraint_fields"]["cell_count"] == 1
    restored = restore_compacted_results(compact, project_path)
    assert restored["planning_constraint_fields"]["items"][0]["cells"] == field["cells"]


def test_service_persists_summary_only_in_project_json_and_returns_artifact_ref(tmp_path):
    service = WorkflowService(tmp_path / "current_project.json", DEFAULTS)
    service.state["grid"] = one_cell()
    service.state["workspace"] = {
        "workspace_id": "W", "revision": 1, "bbox": [0.0, 0.0, 1.0, 1.0],
    }
    service.state["spatial_3d"]["altitude_layers"] = [layer()]
    service.state["layered_route_feasibility_policy"] = {
        "status": "confirmed", "confirmed": True, "terrain_vertical_clearance_m": 10.0,
        "source": "fixture",
    }
    service.state["building_clearance_policy"] = {
        "status": "confirmed", "confirmed": True, "vertical_clearance_m": 10.0,
        "horizontal_clearance_m": 0.0,
        "source": "fixture",
    }
    service.state["tower_clearance_policy"] = {
        "status": "confirmed", "confirmed": True,
        "tower_vertical_clearance_m": 10.0, "tower_horizontal_clearance_m": 0.0,
        "source": "fixture",
    }
    service.state["grid_attributes"]["terrain"]["cells"] = resolved_inputs()["terrain_by_cell"]
    service.state["grid_attributes"]["buildings"] = {
        "status": "passed", "cells": resolved_inputs()["buildings_by_cell"],
    }
    summary = service.generate_planning_constraint_field({
        "altitude_layer_id": "ALT-080",
        "restricted_areas": confirmed_restricted_collection(),
        "tower_obstacle_profiles": {"status": "passed", "items": {}},
    })
    item = summary["items"][0]
    assert "cells" not in item
    assert item["counts"] == {"total": 1, "pass": 1, "blocked": 0, "unknown": 0,
                              "blocked_by": {domain: 0 for domain in (
                                  "terrain", "building", "tower", "airspace", "critical_site",
                              )}}
    assert item["artifact_ref"]["cell_count"] == 1
    persisted = service.session.repository.load()
    assert "cells" not in persisted["planning_constraint_fields"]["items"][0]
