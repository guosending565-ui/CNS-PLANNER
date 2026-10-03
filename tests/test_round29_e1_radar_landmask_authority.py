"""Round29-E1: Radar consumes the project's canonical LandMask authority."""

from __future__ import annotations

import json
from copy import deepcopy

from cns_planner.application.radar_surveillance_layout_service import (
    RadarSurveillanceLayoutService,
    normalize_radar_surveillance_policy,
)
from cns_planner.domain.surface_classification import (
    empty_surface_class_facts,
    normalize_surface_classification_policy,
)


class _Session:
    def __init__(self, state):
        self.state = state

    def save(self):
        pass


class _Invalidation:
    def cns_corridor(self):
        pass


def _facts(*, layer="land", digest="a" * 64, fingerprint="facts-a"):
    facts = empty_surface_class_facts("passed")
    facts["policy"] = normalize_surface_classification_policy({
        "land_mask_layer_name": layer,
        "coastal_uncertainty_buffer_m": 75,
    })
    facts["land_mask"].update({
        "configured_path": "projects/derived/example/current.gpkg",
        "layer_name": layer,
        "source_crs": "EPSG:4326",
        "source_identity": {
            "identity_basis": "content_sha256",
            "content_sha256": digest,
            "layer_name": layer,
            "source_crs": "EPSG:4326",
        },
    })
    facts["input_fingerprint"] = fingerprint
    facts["by_grid_id"] = {"G1": "land"}
    return facts


def _service(*, include_policy=True, include_facts=True):
    state = {
        "radar_surveillance_policy": normalize_radar_surveillance_policy({
            "land_mask_layer_name": "zhejiang_boundary",
            "coastal_uncertainty_buffer_m": 30,
        }),
        "data_source_paths": {
            "land_mask": "projects/derived/example/current.gpkg",
        },
        "result_statuses": {},
        "operational_routes": [],
        "towers": {"items": []},
        "tower_obstacle_profiles": {"items": {}},
        "spatial_3d": {"altitude_layers": []},
    }
    if include_policy:
        state["surface_classification_policy"] = (
            normalize_surface_classification_policy({
                "land_mask_layer_name": "land",
                "coastal_uncertainty_buffer_m": 75,
            })
        )
    if include_facts:
        state["surface_class_facts"] = _facts()
    return RadarSurveillanceLayoutService(
        _Session(state), _Invalidation(), lambda: {},
    )


def test_canonical_layer_and_buffer_override_legacy_radar_policy():
    service = _service()

    authority = service.land_mask_authority()
    source = service.source_status()["land_mask"]

    assert authority["layer_name"] == "land"
    assert authority["coastal_uncertainty_buffer_m"] == 75.0
    assert authority["legacy_fallback_used"] is False
    assert source["layer_name"] == "land"
    assert source["coastal_uncertainty_buffer_m"] == 75.0


def test_canonical_source_identity_changes_radar_fingerprint_and_provenance():
    service = _service()
    before = service.input_fingerprint("R1")
    before_source = service.source_status()["land_mask"]

    service.session.state["surface_class_facts"] = _facts(
        digest="b" * 64, fingerprint="facts-b",
    )

    after = service.input_fingerprint("R1")
    after_source = service.source_status()["land_mask"]
    assert after != before
    assert before_source["surface_facts_fingerprint"] == "facts-a"
    assert after_source["surface_facts_fingerprint"] == "facts-b"
    assert before_source["content_sha256"] != after_source["content_sha256"]


def test_legacy_radar_policy_fallback_requires_both_canonical_containers_absent():
    service = _service(include_policy=False, include_facts=False)

    authority = service.land_mask_authority()

    assert authority["authority"] == "legacy_radar_policy_fallback"
    assert authority["legacy_fallback_used"] is True
    assert authority["layer_name"] == "zhejiang_boundary"
    assert authority["coastal_uncertainty_buffer_m"] == 30.0


def test_dynamic_snapshot_provenance_contains_no_hardcoded_old_absolute_path():
    service = _service()

    payload = {
        "source": service.source_status(),
        "provenance": service._land_mask_source_provenance(),
        "readiness": service.readiness_snapshot(),
    }
    serialized = json.dumps(payload, ensure_ascii=False)

    assert "GLO30\\\\boundary\\\\zhejiang_boundary.gpkg" not in serialized
    assert payload["provenance"]["configured_path"] == (
        "projects/derived/example/current.gpkg"
    )
    assert payload["provenance"]["layer_name"] == "land"
    assert payload["provenance"]["content_sha256"] == "a" * 64


def test_legacy_layer_cannot_override_canonical_surface_facts():
    service = _service(include_policy=False, include_facts=True)
    service.session.state["surface_class_facts"] = _facts(layer="land")
    legacy_before = deepcopy(service.session.state["radar_surveillance_policy"])

    authority = service.land_mask_authority()

    assert legacy_before["land_mask_layer_name"] == "zhejiang_boundary"
    assert authority["layer_name"] == "land"
    assert authority["facts_layer_name"] == "land"
    assert authority["legacy_fallback_used"] is False


def test_canonical_policy_facts_layer_mismatch_is_fail_closed():
    service = _service()
    service.session.state["surface_class_facts"] = _facts(layer="other_land")

    authority = service.land_mask_authority()
    readiness = service.land_mask_readiness()

    assert authority["layer_name"] == "land"
    assert authority["layer_consistent"] is False
    assert readiness["status"] == "not_ready"
    assert readiness["reason"] == "canonical_surface_layer_mismatch"
