"""Round35-F3 / F3-A + F3-D regression: H0–H4 height evidence gate.

The defect being pinned down (Round35-F2 risk point 3): a GBA ``predicted`` height
could reach the continuous building validator and — because ``predicted`` sits inside
``RESOLVED_BUILDING_HEIGHT_STATUSES`` — be cleared as a formal clearance pass, i.e. a
``validated_candidate``.  A real ``way/1191326829`` (GBA 0.254 m vs OSM 100 m) could
therefore be "cleared" at ALT-100.

This file asserts the contract, not an implementation detail:

* H0–H4 evidence levels are first-class and never auto-promoted to ``verified``;
* ``predicted`` remains usable for planning, but **never** alone supports a clearance
  ``pass``; it yields ``unresolved`` with ``requires_survey`` (and the numeric verdict
  recorded as ``estimated_clear`` / ``estimated_conflict``, never as a verified fact);
* ``source_tagged`` / ``derived`` may pass but are recorded ``verified=False`` /
  ``derived_assumption``;
* ``unknown`` stays ``unresolved`` (no zero-fill, no auto-pass);
* a confirmed zero-footprint cell still fast-passes;
* the RESOLVED whitelist still contains ``predicted`` (planning keeps working);
* the algorithm/validator version bump changes the validation fingerprint.
"""

from pathlib import Path

import pytest

from cns_planner.domain.building_clearance import (
    CLEARANCE_PASS_HEIGHT_EVIDENCE_LEVELS, HEIGHT_EVIDENCE_LEVELS,
    VERIFIED_HEIGHT_EVIDENCE_LEVELS, building_height_evidence_status,
    building_height_evidence_supports_clearance_pass,
    building_height_status_is_resolved, normalize_height_evidence_level,
)
from cns_planner.domain.layered_route_validation import (
    ALGORITHM_VERSION, VALIDATOR_VERSIONS,
)
from cns_planner.validation.continuous_validators import MetricRoute, validate_buildings


VREF = "egm2008_orthometric"


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


def footprint(*, building_id="B", height_m=10.0, height_status="valid", estimate=None,
              evidence_level=None, ground=0.0):
    fp = {
        "building_id": building_id, "height_m": height_m, "height_status": height_status,
        "ground_elevation_max_egm2008_m": ground,
        "ring_metric": [[40, -2], [60, -2], [60, 2], [40, 2], [40, -2]],
    }
    if estimate is not None:
        fp["height_estimate_m"] = estimate
    if evidence_level is not None:
        fp["height_evidence_level"] = evidence_level
    return fp


def building_policy(*, vertical=5.0):
    return {"building_horizontal_clearance_m": 0.0, "building_vertical_clearance_m": vertical}


# --------------------------------------------------------------- 1) H0–H4 contract


def test_evidence_levels_are_the_five_h_levels():
    assert HEIGHT_EVIDENCE_LEVELS == {"verified", "source_tagged", "derived", "predicted", "unknown"}
    assert VERIFIED_HEIGHT_EVIDENCE_LEVELS == {"verified"}
    assert CLEARANCE_PASS_HEIGHT_EVIDENCE_LEVELS == {"verified", "source_tagged", "derived"}


def test_legacy_levels_downgrade_and_never_become_verified():
    assert normalize_height_evidence_level("authoritative", height_present=True) == "source_tagged"
    assert normalize_height_evidence_level("osm_tag", height_present=True) == "source_tagged"
    assert normalize_height_evidence_level("conservative", height_present=True) == "predicted"
    # 缺失 / 未知字符串：有正高度 → predicted，否则 → unknown；绝不回填 verified。
    assert normalize_height_evidence_level(None, height_present=True) == "predicted"
    assert normalize_height_evidence_level("", height_present=False) == "unknown"
    assert normalize_height_evidence_level("garbage", height_present=True) == "predicted"
    assert normalize_height_evidence_level("verified", height_present=False) == "verified"


def test_two_ml_predictions_do_not_auto_become_verified():
    # H0 verified 需要独立核验依据；任何旧命名/缺失都不会落到 verified。
    for raw in (None, "", "predicted", "conservative", "authoritative", "osm_tag"):
        assert normalize_height_evidence_level(raw, height_present=True) != "verified"


def test_clearance_pass_gate_only_accepts_evidence_backed_levels():
    for level in ("verified", "source_tagged", "derived", "authoritative", "osm_tag"):
        assert building_height_evidence_supports_clearance_pass(level) is True
    for level in ("predicted", "unknown", None, "", "conservative"):
        assert building_height_evidence_supports_clearance_pass(level) is False


def test_source_tagged_is_trusted_but_not_verified():
    assert building_height_evidence_status(100.0, "source_tagged") == "trusted"
    assert building_height_evidence_status(100.0, "predicted") == "present_unverified"
    assert building_height_evidence_status(100.0, "verified") == "trusted"
    assert building_height_evidence_status(None) == "unknown"


def test_resolved_whitelist_still_contains_predicted_for_planning():
    # F3-D 红线：不从全局 RESOLVED 白名单删除 predicted，否则候选搜索全面失败。
    assert building_height_status_is_resolved("predicted") is True
    assert building_height_status_is_resolved("valid") is True
    assert building_height_status_is_resolved("unknown") is False


# ------------------------------------------------------- 2) continuous validation gate


def test_predicted_height_never_passes_even_when_numerically_clear():
    result = validate_buildings(
        metric_route(100.0),
        evidence={"available": True, "buildings": [
            footprint(height_m=10.0, evidence_level="predicted")
        ]},
        policy=building_policy(vertical=5.0),
    )
    assert result["status"] == "unresolved"
    unresolved = result["unresolved"][0]
    assert unresolved["reason_id"] == "building_height_evidence_insufficient"
    assert unresolved["evidence"]["requires_survey"] is True
    assert unresolved["evidence"]["estimated_clear"] is True
    assert unresolved["evidence"]["estimated_conflict"] is False
    assert result["evidence"]["worst_evidence_level"] == "predicted"


def test_predicted_numeric_conflict_is_estimate_not_verified_collision():
    # 预测高度 110 m 与 ALT-100 冲突：只记 estimated_conflict，绝不判 failed。
    result = validate_buildings(
        metric_route(100.0),
        evidence={"available": True, "buildings": [
            footprint(height_m=110.0, evidence_level="predicted")
        ]},
        policy=building_policy(vertical=5.0),
    )
    assert result["status"] == "unresolved"
    assert result["violations"] == []
    unresolved = result["unresolved"][0]
    assert unresolved["reason_id"] == "building_height_evidence_insufficient"
    assert unresolved["evidence"]["estimated_conflict"] is True
    assert unresolved["evidence"]["estimated_clear"] is False


def test_source_tagged_conflict_is_a_verified_collision():
    # source_tagged 100 m（OSM 标签值）与 ALT-100 + 5 m clearance 冲突 → failed。
    result = validate_buildings(
        metric_route(100.0),
        evidence={"available": True, "buildings": [
            footprint(height_m=0.254, estimate=100.0, evidence_level="source_tagged")
        ]},
        policy=building_policy(vertical=5.0),
    )
    assert result["status"] == "failed"
    assert result["violations"][0]["evidence"]["height_evidence_level"] == "source_tagged"
    assert result["violations"][0]["evidence"]["verified"] is False


def test_source_tagged_roof_penetration_fails_when_required_clearance_is_zero():
    # S5 真实案例：ALT-100 穿入 ground + 100 m 屋顶时，即使项目尚未确认
    # 额外净空（0 m），也必须保留负的屋顶高度差并判为冲突，不能把碰撞钳成 0。
    result = validate_buildings(
        metric_route(100.0),
        evidence={"available": True, "buildings": [
            footprint(
                height_m=0.254,
                estimate=100.0,
                evidence_level="source_tagged",
                ground=2.6,
            )
        ]},
        policy=building_policy(vertical=0.0),
    )
    assert result["status"] == "failed"
    assert result["minimum_margin"] == pytest.approx(-2.6)
    assert result["violations"][0]["margin"] == pytest.approx(-2.6)


def test_source_tagged_numeric_clear_passes_with_verified_false():
    result = validate_buildings(
        metric_route(100.0),
        evidence={"available": True, "buildings": [
            footprint(height_m=0.254, estimate=80.0, evidence_level="source_tagged")
        ]},
        policy=building_policy(vertical=5.0),
    )
    assert result["status"] == "passed"
    assert result["evidence"]["worst_evidence_level"] == "source_tagged"


def test_unknown_height_stays_unresolved_no_zero_fill():
    result = validate_buildings(
        metric_route(100.0),
        evidence={"available": True, "buildings": [
            footprint(height_m=None, evidence_level="unknown")
        ]},
        policy=building_policy(vertical=5.0),
    )
    assert result["status"] == "unresolved"
    # 未知高度绝不填 0 计算 roof。
    assert result["unresolved"][0]["reason_id"] != "building_vertical_clearance_violated"


def test_no_building_fast_path_remains_passed():
    result = validate_buildings(
        metric_route(100.0),
        evidence={"available": True, "buildings": []},
        policy=building_policy(vertical=5.0),
    )
    assert result["status"] == "passed"
    assert result["evidence"]["worst_evidence_level"] is None


def test_predicted_without_explicit_level_defaults_to_predicted_not_passed():
    # 旧 footprint 没有 height_evidence_level 字段：回填 predicted，不得 pass。
    result = validate_buildings(
        metric_route(100.0),
        evidence={"available": True, "buildings": [footprint(height_m=10.0)]},
        policy=building_policy(vertical=5.0),
    )
    assert result["status"] == "unresolved"
    assert result["evidence"]["worst_evidence_level"] == "predicted"


# ------------------------------------------------ 3) version + fingerprint bump


def test_algorithm_and_building_validator_versions_bumped():
    assert ALGORITHM_VERSION == "1.5"
    assert VALIDATOR_VERSIONS["building"] == "real_footprint_building_validator_v6"
