from copy import deepcopy
from pathlib import Path

import pytest

from cns_planner.algorithms.corridor.v1 import (
    CNSServiceCorridorV1, aggregate_required_service_status,
)
from cns_planner.algorithms.corridor_gap.v1 import CNSCorridorGapAnalyzerV1
from cns_planner.algorithms.radar_layout.v1 import actual_site_coverage
from cns_planner.application.corridor_site_planning_service import _targets
from cns_planner.application.radar_surveillance_layout_service import RadarSurveillanceLayoutService
from cns_planner.application.site_candidate_actions import candidate_actions
from cns_planner.domain.cns_service_contract import (
    SERVICE_KEY_RADAR_NONCOOPERATIVE,
    SERVICE_KEY_RID_COOPERATIVE,
)
from cns_planner.domain.cns_service_registry import planner_family_for
from cns_planner.domain.cns_corridor import normalize_cns_corridor_policy
from cns_planner.domain.radar_service_evidence import (
    build_radar_service_evidence,
    evidence_for_probe,
    radar_candidate_actions,
    radar_what_if_service_evidence,
)
from cns_planner.services.invalidation import DEPENDENTS


RADAR = SERVICE_KEY_RADAR_NONCOOPERATIVE


def required(*, radar=True, rid=False):
    services = {}
    if rid:
        services[SERVICE_KEY_RID_COOPERATIVE] = {
            "required": True, "service_key": SERVICE_KEY_RID_COOPERATIVE,
            "confirmed": True,
        }
    if radar:
        services[RADAR] = {"required": True, "service_key": RADAR, "confirmed": True}
    surveillance = {"required": True, "services": services}
    if len(services) > 1:
        surveillance["service_requirement_mode"] = "all_required"
    return {"project_default": {
        "communication": {"required": False},
        "navigation": {"required": False},
        "surveillance": surveillance,
    }, "route_overrides": {}}


def tower(tower_id, x):
    return {
        "tower_id": tower_id, "name": tower_id, "metric": [float(x), 0.0],
        "longitude": 122.0, "latitude": 30.0, "origin_egm2008_m": 0.0,
    }


def panel(panel_id, tower_id, azimuth=90.0):
    return {
        "panel_id": panel_id, "tower_id": tower_id, "radar_type": "radar_i",
        "azimuth_deg": float(azimuth), "panel_half_width_deg": 45.0,
    }


def sample(surface="land"):
    return {
        "sample_index": 0, "sample_id": "V000000", "distance_along_route_m": 0.0,
        "metric": [1000.0, 0.0], "longitude": 122.01, "latitude": 30.0,
        "egm2008_m": 80.0, "surface_class": surface,
        "required_distinct_site_count": {"land": 2, "coastal_uncertain": 2,
                                         "sea": 1, "unknown": None}[surface],
    }


def layout(*, surface="land", selected=None, candidates=None, status="proposal_ready"):
    selected = list(selected or [])
    candidates = list(candidates if candidates is not None else selected)
    towers = [tower("A", 0.0), tower("B", 0.0)]
    coverage = actual_site_coverage(
        panels=selected, samples=[sample(surface)],
        selected_panel_ids=[item["panel_id"] for item in selected], tower_records=towers,
    )
    return {
        "status": "passed", "algorithm_id": "radar_surveillance_layout",
        "algorithm_version": "1.1", "model_scope": "geometric_initial_radar_layout",
        "items": [{
            "route_id": "R1", "status": status, "input_fingerprint": "layout-fp",
            "algorithm_id": "radar_surveillance_layout", "algorithm_version": "1.1",
            "validation": {"samples": coverage, "validated": all(
                item["status"] == "satisfied" for item in coverage
            )},
            "service_evidence_inputs": {
                "samples": [sample(surface)], "tower_records": towers,
                "selected_panels": selected, "candidate_panels": candidates,
            },
        }],
    }


def radar_entry(evidence):
    return evidence["routes"][0]["samples"][0]


def voxel_probe(x, y, *, surface="sea", altitude=80.0, voxel_id="V1", offset=0.0):
    return {
        "voxel_id": voxel_id,
        "longitude": float(x),
        "latitude": float(y),
        "altitude_egm2008_m": altitude,
        "surface_class": surface,
        "nearest_route_offset_m": offset,
    }


def identity_radar_projection(longitude, latitude):
    return [float(longitude), float(latitude)]


def test_existing_layout_is_not_injected_without_explicit_radar_requirement():
    legacy = {"project_default": {
        "communication": {"required": False}, "navigation": {"required": False},
        "surveillance": {"required": True, "service_key": SERVICE_KEY_RID_COOPERATIVE},
    }}
    assert build_radar_service_evidence(legacy, layout(selected=[panel("A1", "A")])) is None


def test_current_layout_adapts_to_canonical_radar_service_evidence():
    evidence = build_radar_service_evidence(
        required(), layout(surface="sea", selected=[panel("A1", "A")]),
    )
    entry = radar_entry(evidence)
    assert entry["service_key"] == RADAR
    assert entry["planner_family"] == "directional_radar"
    assert entry["technology"] == "radar"
    assert entry["status"] == "satisfied"
    assert evidence["input_fingerprint"]


def test_four_panels_on_one_tower_count_as_one_distinct_site():
    panels = [panel(f"A{i}", "A", azimuth) for i, azimuth in enumerate((0, 90, 180, 270))]
    entry = radar_entry(build_radar_service_evidence(required(), layout(selected=panels)))
    assert entry["distinct_site_count"] == 1
    assert entry["distinct_site_ids"] == ["tower:A"]
    assert entry["status"] == "confirmed_deficit"


def test_two_towers_count_as_two_distinct_sites_on_land():
    entry = radar_entry(build_radar_service_evidence(
        required(), layout(selected=[panel("A1", "A"), panel("B1", "B")]),
    ))
    assert entry["distinct_site_count"] == 2
    assert entry["status"] == "satisfied"


def test_one_tower_is_satisfied_at_sea():
    entry = radar_entry(build_radar_service_evidence(
        required(), layout(surface="sea", selected=[panel("A1", "A")]),
    ))
    assert entry["required_distinct_site_count"] == 1
    assert entry["status"] == "satisfied"


def test_unknown_surface_remains_unknown_not_sea():
    entry = radar_entry(build_radar_service_evidence(
        required(), layout(surface="unknown", selected=[panel("A1", "A")]),
    ))
    assert entry["required_distinct_site_count"] is None
    assert entry["status"] == "unknown"


@pytest.mark.parametrize(
    "statuses, expected",
    [
        (["satisfied", "confirmed_deficit"], "confirmed_deficit"),
        (["confirmed_deficit", "satisfied"], "confirmed_deficit"),
        (["satisfied", "satisfied"], "satisfied"),
        (["satisfied", "unknown"], "unknown"),
    ],
)
def test_all_required_aggregation_never_allows_one_channel_to_replace_the_other(statuses, expected):
    assert aggregate_required_service_status(statuses) == expected


def test_stale_or_missing_layout_becomes_unknown_evidence_not_confirmed_deficit():
    evidence = build_radar_service_evidence(
        required(), layout(selected=[], status="stale"), route_ids=["R1"],
    )
    entry = evidence_for_probe(
        evidence, "R1", voxel_probe(1000.0, 0.0),
        metric_projector=identity_radar_projection,
    )
    assert entry["status"] == "unknown"
    assert entry["distinct_site_count"] is None


def test_same_offset_lateral_voxels_are_evaluated_at_their_own_probe_coordinates():
    evidence = build_radar_service_evidence(
        required(), layout(surface="sea", selected=[panel("A1", "A", 90.0)]),
    )
    east = evidence_for_probe(
        evidence, "R1", voxel_probe(1000.0, 0.0, voxel_id="EAST", offset=40.0),
        metric_projector=identity_radar_projection,
    )
    west = evidence_for_probe(
        evidence, "R1", voxel_probe(-1000.0, 0.0, voxel_id="WEST", offset=40.0),
        metric_projector=identity_radar_projection,
    )
    assert east["status"] == "satisfied"
    assert west["status"] == "confirmed_deficit"
    assert east["nearest_route_offset_m"] == west["nearest_route_offset_m"] == 40.0
    assert east["distinct_site_ids"] == ["tower:A"]
    assert west["distinct_site_ids"] == []


def test_route_validation_coverage_is_not_copied_to_same_offset_voxel():
    evidence = build_radar_service_evidence(
        required(), layout(surface="sea", selected=[panel("A1", "A", 90.0)]),
    )
    assert radar_entry(evidence)["status"] == "satisfied"
    west = evidence_for_probe(
        evidence, "R1", voxel_probe(-1000.0, 0.0, offset=0.0),
        metric_projector=identity_radar_projection,
    )
    assert west["status"] == "confirmed_deficit"
    assert west["distinct_site_count"] == 0


def test_voxel_surface_not_route_validation_surface_controls_required_site_count():
    evidence = build_radar_service_evidence(
        required(), layout(surface="sea", selected=[panel("A1", "A", 90.0)]),
    )
    land = evidence_for_probe(
        evidence, "R1", voxel_probe(1000.0, 0.0, surface="land"),
        metric_projector=identity_radar_projection,
    )
    sea = evidence_for_probe(
        evidence, "R1", voxel_probe(1000.0, 0.0, surface="sea"),
        metric_projector=identity_radar_projection,
    )
    assert (land["required_distinct_site_count"], land["status"]) == (2, "confirmed_deficit")
    assert (sea["required_distinct_site_count"], sea["status"]) == (1, "satisfied")


def test_alt_080_probe_is_evaluated_but_other_altitude_fails_closed():
    evidence = build_radar_service_evidence(
        required(), layout(surface="sea", selected=[panel("A1", "A", 90.0)]),
    )
    supported = evidence_for_probe(
        evidence, "R1", voxel_probe(1000.0, 0.0, altitude=80.0),
        metric_projector=identity_radar_projection,
    )
    unsupported = evidence_for_probe(
        evidence, "R1", voxel_probe(1000.0, 0.0, altitude=100.0),
        metric_projector=identity_radar_projection,
    )
    assert supported["status"] == "satisfied"
    assert unsupported["status"] == "unknown"
    assert unsupported["reasons"] == ["radar_model_scope_altitude_not_supported"]
    assert unsupported["probe_altitude_egm2008_m"] == 100.0
    assert unsupported["model_supported_altitude_egm2008_m"] == 80.0


def test_p14_voxel_contains_radar_service_evidence_only_when_explicitly_required():
    spatial = {
        "altitude_layers": [{
            "altitude_layer_id": "ALT-080", "lower_altitude_m": 70.0,
            "upper_altitude_m": 90.0, "vertical_reference": "egm2008_orthometric",
            "confirmed": True, "status": "confirmed",
        }],
        "route_altitude_profiles": {"R1": {
            "route_id": "R1", "mode": "constant",
            "vertical_reference": "egm2008_orthometric", "constant_altitude_m": 80.0,
            "waypoints": [], "confirmed": True, "status": "confirmed",
            "geoid_undulation_m": None,
        }},
        "site_vertical_profiles": {},
    }
    route = {"route_id": "R1", "status": "passed", "path": [[-0.001, 0.0], [0.001, 0.0]]}
    grid = {"cells": [{"grid_id": "G1", "bbox": [-0.0005, 0.0, 0.0005, 0.001]}]}
    corridor_policy = normalize_cns_corridor_policy({"routes": {"R1": {
        "route_id": "R1", "horizontal_half_width_m": 0.0,
        "vertical_lower_margin_m": 5.0, "vertical_upper_margin_m": 5.0,
        "confirmed": True, "source": "test",
    }}})
    radar_evidence = build_radar_service_evidence(
        required(), layout(selected=[panel("A1", "A")]),
    )
    result = CNSServiceCorridorV1().evaluate(
        [route], spatial, grid, {"terrain": {"status": "passed", "cells": {
            "G1": {"status": "passed", "surface_class": "land",
                   "surface_elevation_mean_m": 0.0},
        }}},
        required(), {"surveillance": {}}, {"items": []}, {"items": []},
        corridor_policy, radar_service_evidence=radar_evidence,
        radar_metric_projector=identity_radar_projection,
    )
    s_entry = next(item for item in result["routes"][0]["voxels"][0]["subsystems"]
                   if item["subsystem"] == "S")
    assert [item["service_key"] for item in s_entry["service_redundancy"]] == [RADAR]
    assert s_entry["planning_status"] == "confirmed_deficit"


def test_p15_keeps_radar_service_separate_and_p16_target_uses_service_key():
    evidence = radar_entry(build_radar_service_evidence(
        required(), layout(selected=[panel("A1", "A")]),
    ))
    corridor = {
        "status": "passed", "algorithm_id": "cns_service_corridor_v1",
        "algorithm_version": "1.0", "input_fingerprint": "p14",
        "routes": [{
            "route_id": "R1", "route_length_m": 100.0, "status": "failed",
            "voxels": [{
                "voxel_id": "V1", "grid_id": "G1", "altitude_layer_id": "ALT-080",
                "nearest_route_offset_m": 0.0, "cell_half_diagonal_m": 5.0,
                "discretized_volume_proxy_m3": 10.0, "surface_class": "land",
                "subsystems": [{
                    "subsystem": "S", "planning_status": "confirmed_deficit",
                    "p8_status": "does_not_meet_under_model", "provider_evaluations": [],
                    "service_redundancy": [evidence], "reasons": [], "evidence": [],
                }],
            }], "subsystems": [],
        }],
    }
    gap = CNSCorridorGapAnalyzerV1().evaluate(corridor, required(), {})
    s_entry = next(item for item in gap["routes"][0]["voxels"][0]["subsystems"]
                   if item["subsystem"] == "S")
    assert [item["service_key"] for item in s_entry["services"]] == [RADAR]
    targets, unknown = _targets(gap)
    radar_targets = [item for item in targets if item.get("service_key") == RADAR]
    assert not [item for item in unknown if item.get("service_key") == RADAR]
    assert [item["target_id"] for item in radar_targets] == [f"R1|V1|{RADAR}"]
    assert radar_targets[0]["planner_family"] == "directional_radar"


def test_radar_target_never_enters_ordinary_site_candidate_cartesian_product():
    target = {"subsystem": "S", "service_key": RADAR}
    catalog = {"items": [{
        "device_id": "RADAR", "subsystem": "S", "service_key": RADAR,
        "enabled": True,
    }]}
    assert candidate_actions([target], {"items": []}, {"items": []}, catalog) == []
    assert planner_family_for(RADAR) == "directional_radar"


def test_radar_candidate_action_has_directional_panel_and_tower_identity():
    candidate = panel("B1", "B")
    actions = radar_candidate_actions(
        [{"route_id": "R1", "service_key": RADAR}],
        layout(selected=[panel("A1", "A")], candidates=[panel("A1", "A"), candidate]),
    )
    assert len(actions) == 1
    assert actions[0]["distinct_site_id"] == "tower:B"
    assert actions[0]["panel"]["azimuth_deg"] == 90.0
    assert actions[0]["panel"]["beamwidth_deg"] == 90.0
    assert actions[0]["panel"]["elevation_min_deg"] == 0.0
    assert actions[0]["panel"]["elevation_max_deg"] == 45.0
    assert actions[0]["radar_type"] == "radar_i"


def test_radar_what_if_calls_canonical_geometry_and_new_tower_adds_one_site(monkeypatch):
    import cns_planner.domain.radar_service_evidence as adapter

    calls = []
    canonical = adapter.actual_site_coverage

    def recording(**kwargs):
        calls.append(kwargs)
        return canonical(**kwargs)

    monkeypatch.setattr(adapter, "actual_site_coverage", recording)
    source = layout(
        selected=[panel("A1", "A")],
        candidates=[panel("A1", "A"), panel("B1", "B")],
    )
    action = radar_candidate_actions([{"route_id": "R1", "service_key": RADAR}], source)[0]
    after = radar_what_if_service_evidence(required(), source, [action])
    assert calls
    assert radar_entry(after)["distinct_site_count"] == 2
    assert radar_entry(after)["status"] == "satisfied"


def test_p16_baseline_and_hypothetical_share_voxel_probe_evaluation():
    source = layout(
        surface="land",
        selected=[panel("A1", "A")],
        candidates=[panel("A1", "A"), panel("B1", "B")],
    )
    baseline = build_radar_service_evidence(required(), source)
    action = radar_candidate_actions([{"route_id": "R1", "service_key": RADAR}], source)[0]
    hypothetical = radar_what_if_service_evidence(required(), source, [action])
    probe = voxel_probe(1000.0, 0.0, surface="land")
    before = evidence_for_probe(
        baseline, "R1", probe, metric_projector=identity_radar_projection,
    )
    after = evidence_for_probe(
        hypothetical, "R1", probe, metric_projector=identity_radar_projection,
    )
    assert before["status"] == "confirmed_deficit"
    assert before["distinct_site_ids"] == ["tower:A"]
    assert after["status"] == "satisfied"
    assert after["distinct_site_ids"] == ["tower:A", "tower:B"]


def test_same_tower_extra_panel_does_not_increase_independent_site_gain():
    source = layout(
        selected=[panel("A1", "A")],
        candidates=[panel("A1", "A"), panel("A2", "A")],
    )
    action = radar_candidate_actions([{"route_id": "R1", "service_key": RADAR}], source)[0]
    after = radar_what_if_service_evidence(required(), source, [action])
    assert radar_entry(after)["distinct_site_count"] == 1
    assert radar_entry(after)["status"] == "confirmed_deficit"


def test_invalidation_edges_are_downstream_only():
    protected = {
        "routes", "grid_risk", "grid_risk_v2", "route_risk_profiles",
        "layered_route_validations", "layered_operational_adoptions",
    }
    assert protected.isdisjoint(DEPENDENTS["required_cns"])
    assert "radar_surveillance_layout" not in DEPENDENTS["required_cns"]
    assert "radar_surveillance_layout" not in DEPENDENTS["surface_classification_policy"]
    assert {"cns_corridor_assessment", "cns_corridor_gap_assessment",
            "cns_corridor_site_plan", "report"} <= set(DEPENDENTS["required_cns"])


def test_radar_layout_stale_propagates_to_service_chain_only_when_radar_is_required():
    class Session:
        def __init__(self, required_cns):
            self.state = {
                "required_cns": required_cns,
                "radar_surveillance_layout": layout(selected=[panel("A1", "A")]),
                "result_statuses": {"radar_surveillance_layout": "passed"},
            }

        def save(self):
            pass

    class Invalidation:
        def __init__(self):
            self.calls = []

        def cns_corridor(self):
            self.calls.append("cns_corridor")

    explicit_invalidation = Invalidation()
    explicit = RadarSurveillanceLayoutService(
        Session(required()), explicit_invalidation, lambda: {},
    )
    explicit.stale_for_reason("policy_changed")
    assert explicit_invalidation.calls == ["cns_corridor"]

    legacy_invalidation = Invalidation()
    legacy_required = {"project_default": {
        "communication": {"required": False}, "navigation": {"required": False},
        "surveillance": {"required": True},
    }}
    legacy = RadarSurveillanceLayoutService(
        Session(legacy_required), legacy_invalidation, lambda: {},
    )
    legacy.stale_for_reason("policy_changed")
    assert legacy_invalidation.calls == []


def test_adapter_and_dispatch_source_guards_forbid_second_radar_geometry():
    root = Path(__file__).parents[1]
    text = "\n".join((root / path).read_text(encoding="utf-8") for path in (
        "cns_planner/domain/radar_service_evidence.py",
        "cns_planner/application/corridor_site_planning_service.py",
    ))
    for forbidden in (
        "RID_RADIUS_BY_SURFACE", "COMMUNICATION_RADIUS_BY_SURFACE",
        "OMNIDIRECTIONAL_HEMISPHERE", "atan2(", "sqrt(",
    ):
        assert forbidden not in text
    assert "actual_site_coverage" in text
    adapter_text = (root / "cns_planner/domain/radar_service_evidence.py").read_text(
        encoding="utf-8",
    )
    assert "evidence_for_route_offset" not in adapter_text
    assert "key=lambda item: abs" not in adapter_text
