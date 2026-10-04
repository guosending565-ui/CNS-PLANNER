"""Round 29-G —— Radar surface 长度守恒与 canonical gap 字段转印回归。

已确认根因：

* ``validation_report`` 先按 surface 过滤 sample，再用相邻 offset 差累计长度 ⇒ 跨过其它
  surface 区段，land / sea / coastal 的 ``length_m`` 各自接近整条航路；
* 算法 ``solve_layout`` 已经给出 canonical ``gap_reason`` / ``gap_classification`` /
  ``managed_physical_gap``，但 Application 层从未把它们转印到 layout item /
  result_snapshot / HTTP —— 因此 R0005 的
  ``independent_site_count_limited`` + ``managed_physical_gap=true`` 在接口上不可见。
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from cns_planner.algorithms.radar_layout.v1 import (
    _radar_gap_classification, solve_layout, validation_report,
)
from cns_planner.application.radar_surveillance_layout_service import (
    _radar_gap_block, normalize_radar_surveillance_layout,
)

from test_bug_shot_008_radar_altitude_layer import _samples, _towers
from test_radar_surveillance_layout import ROUTE_ID, _evaluate, _service


SPACING_M = 5.0
SURFACES = ("land", "sea", "coastal_uncertain", "unknown")


def _sample(index, offset, surface, status="satisfied", actual=2, required=2):
    return {
        "sample_id": f"S{index}", "sample_index": index,
        "distance_along_route_m": float(offset), "surface_class": surface,
        "status": status, "actual_distinct_site_count": actual,
        "required_distinct_site_count": required,
        "panels": [],
    }


def _sequence(count=11, *, spacing=SPACING_M, pattern=SURFACES, status_of=None):
    return [
        _sample(
            index, index * spacing, pattern[index % len(pattern)],
            status=(status_of or (lambda value: "satisfied"))(index),
        )
        for index in range(count)
    ]


def _length_sum(report):
    return sum(
        report[surface]["length_m"] for surface in SURFACES
    )


def test_surface_lengths_conserve_the_whole_route():
    """① 所有 surface 的 length_m 之和 ≈ route_length_m。"""

    samples = _sequence(count=11)
    route_length = samples[-1]["distance_along_route_m"]
    report = validation_report(per_sample=samples, route_length_m=route_length)
    conservation = report["surface_length_conservation"]

    assert _length_sum(report) == pytest.approx(route_length)
    assert conservation["surface_length_sum_m"] == pytest.approx(route_length)
    assert conservation["difference_m"] == pytest.approx(0.0)
    assert conservation["route_length_m"] == pytest.approx(route_length)
    #: 每个 surface 内部的四种状态长度也必须守恒。
    for surface in SURFACES:
        entry = report[surface]
        assert (
            entry["satisfied_length_m"] + entry["under_redundant_length_m"]
            + entry["uncovered_length_m"] + entry["unknown_length_m"]
        ) == pytest.approx(entry["length_m"])
        assert entry["length_conservation_difference_m"] == pytest.approx(0.0)


def test_alternating_surfaces_never_accumulate_across_each_other():
    """② 分类交替时中间距离绝不被重复计入两个 surface。

    修复前：先按 surface 过滤、再用剩余相邻差累计 ⇒ land / sea 的长度各自接近整条航路。
    """

    samples = _sequence(count=11, pattern=("land", "sea"))
    route_length = samples[-1]["distance_along_route_m"]
    report = validation_report(per_sample=samples, route_length_m=route_length)

    assert report["land"]["length_m"] == pytest.approx(25.0)
    assert report["sea"]["length_m"] == pytest.approx(25.0)
    assert report["land"]["length_m"] + report["sea"]["length_m"] == pytest.approx(route_length)
    #: 交替出现时 sample_count 是各自真实数量，而不是被跨越区间放大。
    assert report["land"]["sample_count"] == 6
    assert report["sea"]["sample_count"] == 5
    assert report["land"]["length_m"] <= route_length
    assert report["sea"]["length_m"] <= route_length


def test_half_intervals_are_owned_by_their_own_sample_status():
    """首尾半区间（5 m 采样）必须归属各自的 surface 与 status。"""

    samples = _sequence(
        count=11, pattern=("land", "sea"),
        status_of=lambda index: "uncovered" if index in (0, 10) else "satisfied",
    )
    route_length = samples[-1]["distance_along_route_m"]
    report = validation_report(per_sample=samples, route_length_m=route_length)

    #: 首尾半区间各 2.5 m 都落在 land 上，且都是 uncovered。
    assert report["land"]["uncovered_length_m"] == pytest.approx(5.0)
    assert report["land"]["satisfied_length_m"] == pytest.approx(20.0)
    assert report["sea"]["uncovered_length_m"] == pytest.approx(0.0)
    assert _length_sum(report) == pytest.approx(route_length)


def test_uneven_refinement_spacing_still_conserves_length():
    """补点后的不等距采样同样守恒（区间归属只依赖完整有序序列）。"""

    offsets = [0.0, 5.0, 10.0, 10.5, 11.0, 20.0]
    surfaces = ["land", "sea", "land", "coastal_uncertain", "sea", "land"]
    samples = [
        _sample(index, offset, surfaces[index])
        for index, offset in enumerate(offsets)
    ]
    report = validation_report(per_sample=samples, route_length_m=offsets[-1])
    assert _length_sum(report) == pytest.approx(offsets[-1])
    for surface in SURFACES:
        entry = report[surface]
        assert (
            entry["satisfied_length_m"] + entry["under_redundant_length_m"]
            + entry["uncovered_length_m"] + entry["unknown_length_m"]
        ) == pytest.approx(entry["length_m"])


def test_coverage_verdict_is_untouched_by_the_length_fix():
    """长度修复**不**改变任何 coverage verdict。"""

    samples = _sequence(
        count=11, pattern=("land", "sea"),
        status_of=lambda index: "uncovered" if index == 4 else "satisfied",
    )
    report = validation_report(per_sample=samples, route_length_m=50.0)
    assert report["validated"] is False
    assert [item["sample_index"] for item in report["violations"]] == [4]
    assert report["unknown_evidence_count"] == 0
    assert [segment["route_offset_start_m"] for segment in report["uncovered_segments"]] == [20.0]


# ------------------------------------------------------------------ canonical gap fields


def test_gap_fields_are_transcribed_verbatim_from_the_algorithm():
    """③ ``gap_reason`` / ``gap_classification`` / ``managed_physical_gap`` 原样转印。"""

    solved = solve_layout(
        towers=_towers(), samples=_samples(),
        options={"fixed_altitude_m": 100.0, "altitude_layer_id": "ALT-100"},
    )
    assert "gap_reason" in solved and "gap_classification" in solved
    block = _radar_gap_block(solved)
    assert block["available"] is True
    assert block["gap_reason"] == solved["gap_reason"]
    assert block["gap_classification"] == solved["gap_classification"]
    assert block["managed_physical_gap"] == solved["managed_physical_gap"]
    assert block["source"] == "radar_layout_algorithm_solve_layout_canonical_fields"

    #: 未求解时**不得**用任何默认值或二次推导填补。
    unavailable = _radar_gap_block(None)
    assert unavailable["available"] is False
    assert unavailable["gap_reason"] is None
    assert unavailable["gap_classification"] is None
    assert unavailable["managed_physical_gap"] is None


def test_presolve_independent_site_shortage_reason_is_never_renamed():
    """④ presolve 证明独立站址不足时保持独立站址原因，不降级为构型不可行。"""

    final = {"presolve": {"insufficient_samples": [{
        "sample_id": "S1", "surface_class": "land",
        "candidate_distinct_site_count": 1, "required_distinct_site_count": 2,
    }]}}
    candidates = {"unusable_towers": [], "towers": [{"tower_id": "T1"}]}
    reason, classification = _radar_gap_classification("infeasible", final, candidates, [])
    assert reason == "independent_site_count_limited"
    assert classification == "confirmed_gap"
    assert classification != "unknown"
    assert reason != "orientation_configuration_infeasible"
    assert (classification == "confirmed_gap") is True  # managed physical gap


def test_orientation_infeasibility_requires_the_solver_proof():
    """:只有 solver 明确证明构型不可行时才允许 ``orientation_configuration_infeasible``。"""

    final = {"presolve": {}, "solve": {"solver": {"infeasibility_proven": True}}}
    reason, classification = _radar_gap_classification(
        "infeasible", final, {"unusable_towers": [], "towers": [{"tower_id": "T1"}]}, [],
    )
    assert (reason, classification) == ("orientation_configuration_infeasible", "confirmed_gap")


def test_search_incomplete_is_never_a_managed_physical_gap():
    """⑤ ``search_incomplete`` 仍是未知，绝不是已管理缺口。"""

    for status in ("search_incomplete", "refinement_incomplete"):
        reason, classification = _radar_gap_classification(status, None, {}, [])
        assert (reason, classification) == ("search_incomplete", "unknown")
        assert classification != "confirmed_gap"
        assert (classification == "confirmed_gap") is False


def test_layout_item_exposes_canonical_gap_fields_and_survives_normalization(tmp_path):
    """③ 端到端：algorithm → Application item → result_snapshot → 持久化 round-trip。"""

    service, provider = _service(tmp_path, surface="sea")
    _evaluate(service, provider)
    item = service.radar_surveillance_layout(ROUTE_ID)["items"][0]

    assert item["gap_classification"] in ("none", "confirmed_gap", "unknown")
    assert item["managed_physical_gap"] == (item["gap_classification"] == "confirmed_gap")
    assert item["radar_gap"]["available"] is True
    assert item["radar_gap"]["gap_reason"] == item["gap_reason"]
    assert item["radar_gap"]["gap_classification"] == item["gap_classification"]
    assert item["radar_gap"]["managed_physical_gap"] == item["managed_physical_gap"]

    #: result_snapshot（HTTP / 只读投影）必须逐字段一致。
    snapshot = service.radar_surveillance_layout(ROUTE_ID)["items"][0]
    for field in ("gap_reason", "gap_classification", "managed_physical_gap"):
        assert snapshot[field] == item[field]
    assert snapshot["radar_gap"] == item["radar_gap"]

    #: 持久化 round-trip 绝不丢字段，也绝不重算结论。
    stored = {"status": "passed", "items": [deepcopy(item)]}
    restored = normalize_radar_surveillance_layout(stored)["items"][0]
    for field in ("gap_reason", "gap_classification", "managed_physical_gap", "radar_gap"):
        assert restored[field] == item[field]


def test_summary_snapshot_exposes_the_canonical_gap_fields(tmp_path):
    """``/api/state`` 的摘要同样必须能读到 canonical gap 结论。"""

    service, provider = _service(tmp_path, surface="sea")
    _evaluate(service, provider)
    summary = service.radar_surveillance_layout_service.summary_snapshot()["by_route"][ROUTE_ID]
    full = service.radar_surveillance_layout(ROUTE_ID)["items"][0]
    assert summary["gap_reason"] == full["gap_reason"]
    assert summary["gap_classification"] == full["gap_classification"]
    assert summary["managed_physical_gap"] == full["managed_physical_gap"]


def test_real_layout_conserves_surface_lengths_end_to_end(tmp_path):
    """真实服务链上的 surface 长度同样守恒（含 5 m 复核链）。"""

    def classify(longitude, latitude):
        #: 与既有 fixture 同构：按经度把航路切成 land / sea 两段。
        return "land" if longitude < 122.0 else "sea"

    service, provider = _service(tmp_path, surface_by_offset=classify)
    _evaluate(service, provider)
    validation = service.radar_surveillance_layout(ROUTE_ID)["items"][0]["validation"]
    conservation = validation["surface_length_conservation"]
    assert conservation["surface_length_sum_m"] == pytest.approx(
        conservation["route_length_m"], abs=1e-6
    )
    assert conservation["difference_m"] == pytest.approx(0.0, abs=1e-6)
    assert conservation["interval_basis"] == (
        "full_ordered_sample_sequence_with_half_intervals_at_both_ends"
    )
