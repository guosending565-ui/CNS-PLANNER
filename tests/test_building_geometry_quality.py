"""建筑 footprint 几何质量与 ``make_valid`` 修复（Phase 3.5 稳定化）。

覆盖要点：

* 有效环直接通过；顶点不足 / 共线退化保持 ``invalid``（unknown 语义）；
* 自交（bowtie）几何用 ``shapely.make_valid()`` 修复且源几何不被修改；
* 修复结果多部件时**每个部件**都保留（不允许取最大部件而漏判穿透）；
* 质量报告累加 passed / repaired / invalid 与修复统计；
* 两个连续验证器（cruise 与 vertical transition）共用同一质量门：
  修复后可判定 ⇒ 不再因为几何问题 unresolved；无法解释 ⇒ 仍然 unresolved。
"""

from __future__ import annotations

import pytest

from cns_planner.domain.building_geometry_quality import (
    GEOMETRY_QUALITY_STATUSES, annotate_footprint_geometry, assess_footprint_geometry,
    empty_geometry_quality_report, merge_geometry_quality, prepare_footprint_polygons,
    summarize_geometry_quality,
)

VALID_RING = [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0], [0.0, 0.0]]
BOWTIE_RING = [[0.0, 0.0], [10.0, 10.0], [10.0, 0.0], [0.0, 10.0], [0.0, 0.0]]
DEGENERATE_RING = [[0.0, 0.0], [1.0, 1.0], [2.0, 2.0], [0.0, 0.0]]


# --------------------------------------------------------------------------- single ring


def test_valid_ring_passes_without_any_repair():
    quality = assess_footprint_geometry("B1", VALID_RING)
    assert quality["status"] == "passed"
    assert quality["record"]["repair_method"] == "not_needed"
    assert quality["record"]["repair_applied"] is False
    assert quality["record"]["part_count"] == 1
    assert quality["record"]["source_area_m2"] == pytest.approx(100.0)
    assert quality["record"]["source_geometry_modified"] is False


def test_ring_with_too_few_vertices_stays_unknown():
    quality = assess_footprint_geometry("B2", [[0.0, 0.0], [1.0, 1.0]])
    assert quality["status"] == "invalid"
    assert quality["record"]["validity_reason"] == "ring_vertex_count_below_3"
    assert quality["parts"] == []


def test_degenerate_collinear_ring_is_unrepairable_and_stays_unknown():
    quality = assess_footprint_geometry("B3", DEGENERATE_RING)
    assert quality["status"] == "invalid"
    assert quality["record"]["repair_method"] == "unrepairable"
    assert quality["record"]["repair_applied"] is False
    assert quality["parts"] == []
    assert "make_valid_result_not_polygonal" in quality["record"]["validity_reason"]


def test_non_finite_coordinates_are_rejected_not_coerced():
    quality = assess_footprint_geometry("B4", [[0.0, 0.0], ["x", 1.0], [2.0, 2.0]])
    assert quality["status"] == "invalid"
    assert quality["record"]["validity_reason"] == "ring_vertex_count_below_3"


def test_self_intersecting_ring_is_repaired_in_memory_only():
    quality = assess_footprint_geometry("B5", BOWTIE_RING)
    assert quality["status"] == "repaired"
    assert quality["record"]["repair_applied"] is True
    assert quality["record"]["make_valid_applied"] is True
    assert quality["record"]["repair_method"] == "shapely_make_valid"
    assert quality["record"]["source_geometry_modified"] is False
    assert quality["parts"], quality["record"]
    # Every returned part is a valid, positively-sized polygon.
    for polygon in quality["parts"]:
        assert polygon.is_valid and polygon.area > 0
    # The source ring is untouched: the caller's list is exactly what was passed in.
    ring = [list(point) for point in BOWTIE_RING]
    assess_footprint_geometry("B5", ring)
    assert ring == BOWTIE_RING


def test_quality_status_vocabulary_is_closed():
    assert set(GEOMETRY_QUALITY_STATUSES) == {"passed", "repaired", "invalid"}


# --------------------------------------------------------------------------- footprints


def test_multi_part_footprint_keeps_every_part_for_clearance_evaluation():
    # A footprint whose two parts are far apart: taking only the largest part could hide a
    # penetration over the smaller one, so both must survive.
    footprint = {
        "building_id": "M1",
        "ring_parts_metric": [
            [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0], [0.0, 0.0]],
            [[100.0, 100.0], [104.0, 100.0], [104.0, 104.0], [100.0, 104.0], [100.0, 100.0]],
        ],
    }
    prepared = prepare_footprint_polygons(footprint)
    assert prepared["quality"] == "passed"
    assert len(prepared["polygons"]) == 2
    assert len(prepared["parts_metric"]) == 2
    assert prepared["annotation"]["source_ring_count"] == 2
    assert prepared["annotation"]["valid_part_count"] == 2
    assert prepared["annotation"]["invalid_part_count"] == 0


def test_footprint_with_one_invalid_ring_is_invalid_overall():
    footprint = {
        "building_id": "M2",
        "ring_parts_metric": [VALID_RING, DEGENERATE_RING],
    }
    prepared = prepare_footprint_polygons(footprint)
    assert prepared["quality"] == "invalid"
    assert prepared["annotation"]["invalid_part_count"] == 1
    assert prepared["annotation"]["repair_method"] == "unrepairable"


def test_single_ring_footprint_is_handled_like_one_part():
    prepared = prepare_footprint_polygons({"building_id": "S1", "ring_metric": VALID_RING})
    assert prepared["quality"] == "passed"
    assert len(prepared["polygons"]) == 1


def test_legacy_ring_key_without_parts_key_still_works():
    prepared = prepare_footprint_polygons({"building_id": "S2", "ring_metric": BOWTIE_RING})
    assert prepared["quality"] == "repaired"
    assert prepared["polygons"]


def test_empty_footprint_has_no_polygon_and_stays_unknown():
    prepared = prepare_footprint_polygons({"building_id": "S3", "ring_metric": []})
    assert prepared["quality"] == "invalid"
    assert prepared["polygons"] == []


def test_annotation_never_reports_the_source_as_modified():
    annotated = annotate_footprint_geometry("A1", VALID_RING)
    annotation = annotated["annotation"]
    assert annotation["source_geometry_modified"] is False
    assert annotation["prefer_shapely_make_valid"] is True
    assert annotation["unrepairable_geometry_stays_unknown"] is True
    assert annotation["repair_succeeded"] is False
    assert annotation["status"] == "passed"


# --------------------------------------------------------------------------- report


def test_quality_report_accumulates_counts_and_repair_statistics():
    report = empty_geometry_quality_report()
    merge_geometry_quality(report, assess_footprint_geometry("P1", VALID_RING)["record"])
    merge_geometry_quality(report, assess_footprint_geometry("P2", BOWTIE_RING)["record"])
    merge_geometry_quality(report, assess_footprint_geometry("P3", DEGENERATE_RING)["record"])
    assert report["evaluated_footprint_count"] == 3
    assert report["counts"] == {"passed": 1, "repaired": 1, "invalid": 1}
    assert report["repair"]["applied_count"] == 1
    assert report["repair"]["failed_count"] == 1
    assert report["status"] == "invalid_geometry_present"
    assert [item["building_id"] for item in report["invalid"]] == ["P3"]
    assert report["semantics"]["unknown_is_never_safe"] is True


def test_quality_report_from_records_is_a_standalone_summary():
    records = [
        assess_footprint_geometry("R1", VALID_RING)["record"],
        assess_footprint_geometry("R2", VALID_RING)["record"],
    ]
    report = summarize_geometry_quality(records)
    assert report["counts"] == {"passed": 2, "repaired": 0, "invalid": 0}
    assert report["status"] == "passed"
    assert report["evaluated_footprint_count"] == 2


def test_report_records_repair_counts_and_reports_no_ratio_for_degenerate_source_area():
    report = empty_geometry_quality_report()
    for building_id in ("Q1", "Q2"):
        merge_geometry_quality(report, assess_footprint_geometry(building_id, BOWTIE_RING)["record"])
    assert report["repair"]["applied_count"] == 2
    assert report["counts"]["repaired"] == 2
    # A self-intersecting ring has no trustworthy 2D source area, so no ratio is invented.
    assert report["repair"]["repaired_area_ratio_min"] is None
    # A ring with a trustworthy source area records the ratio range.
    report = empty_geometry_quality_report()
    merge_geometry_quality(
        report, assess_footprint_geometry("Q3", [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0], [0.0, 0.0], [5.0, 5.0], [0.0, 0.0]])["record"],
    )
    assert report["repair"]["repaired_area_ratio_min"] is not None
    assert report["repair"]["repaired_area_ratio_min"] <= report["repair"]["repaired_area_ratio_max"]
