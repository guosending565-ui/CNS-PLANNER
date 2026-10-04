"""Round 29-N —— Radar 固定巡航高度层的 P14/P15/P16 dispatch 正确性回归。

背景（真实 blocker）：

* ``S:radar_noncooperative`` 是**固定巡航高度层**服务：水平适用范围 = corridor，
  垂向适用范围 = ``operational_route_altitude_layer``（canonical Radar layout 的
  ``altitude_layer_id`` + ``model_supported_altitude_egm2008_m``）；
* 旧 P14 1.2 让 Radar 复用 generic voxel probe 的 vertical-overlap midpoint
  （真实 R0005：ALT-100 = 107.5 m），于是连合法的 ALT-100 voxel 都被判成
  ``radar_model_scope_altitude_not_supported``；
* 本文件用**不改写 midpoint** 的真实 fixture 锁定修复：generic probe 保持 107.5，
  而 Radar evaluation altitude = 100.0；off-layer 体元是显式 ``not_applicable``，
  既不是 satisfied、也不是 confirmed_deficit、更不是 unknown。

本文件只使用 canonical 实现（P14 / P15 / P16 target 抽取 / Radar adapter），
不复制任何几何。
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from cns_planner.algorithms.corridor.v1 import (
    CNSServiceCorridorV1, aggregate_required_service_status,
)
from cns_planner.algorithms.corridor_gap.v1 import CNSCorridorGapAnalyzerV1
from cns_planner.application.corridor_site_planning_service import (
    _radar_gap_authority, _targets,
)
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.cns_corridor import (
    empty_cns_corridor_assessment, normalize_cns_corridor_policy,
)
from cns_planner.domain.cns_inputs import normalize_device
from cns_planner.domain.cns_planning_objectives import empty_cns_corridor_gap_assessment
from cns_planner.domain.cns_service_contract import (
    RID_RADIUS_BY_SURFACE, RID_URBAN_RADIUS_M,
    SERVICE_KEY_NAVIGATION_INTEGRITY_MONITORING,
    SERVICE_KEY_RADAR_NONCOOPERATIVE, SERVICE_KEY_RID_COOPERATIVE,
)
from cns_planner.domain.corridor_site_planning import empty_cns_corridor_site_plan
from cns_planner.domain.radar_service_evidence import (
    RADAR_NOT_APPLICABLE_REASON, RADAR_VERTICAL_SCOPE_OPERATIONAL_ALTITUDE_LAYER,
    build_radar_service_evidence, evidence_for_probe,
)
from cns_planner.domain.radar_surveillance_layout import (
    ALGORITHM_ID as RADAR_ALGORITHM_ID, ALGORITHM_VERSION as RADAR_ALGORITHM_VERSION,
)
from cns_planner.site_planner.corridor_reuse_first_v2 import CorridorReuseFirstSitePlannerV2

from test_corridor_site_planner_v2 import DEFAULTS
from test_radar_noncooperative_service_chain import (
    identity_radar_projection, layout as radar_layout, panel,
)


RADAR = SERVICE_KEY_RADAR_NONCOOPERATIVE
RID = SERVICE_KEY_RID_COOPERATIVE

#: 真实 bug 的几何：route 固定 100 m，ALT-100 层的 actual bounds 使
#: generic overlap midpoint = 107.5 m（≠ canonical model altitude 100.0）。
ALT_080_ID = "ALT-080"
ALT_100_ID = "ALT-100"
ALT_150_ID = "ALT-150"
ROUTE_ALTITUDE_M = 100.0
GENERIC_ALT_100_MIDPOINT_M = 107.5
RADAR_MODEL_ALTITUDE_M = 100.0

ROUTE = {"route_id": "R1", "status": "passed", "path": [[-0.002, 0.0], [0.002, 0.0]]}
GRID = {"cells": [{"grid_id": "G1", "bbox": [-0.001, 0.0, 0.001, 0.002]}]}
TERRAIN = {"terrain": {"status": "passed", "cells": {
    "G1": {"status": "passed", "surface_elevation_mean_m": 0.0, "surface_class": "land"},
}}}


def _layer(layer_id, lower, upper):
    return {
        "altitude_layer_id": layer_id, "lower_altitude_m": float(lower),
        "upper_altitude_m": float(upper), "vertical_reference": "egm2008_orthometric",
        "confirmed": True, "status": "confirmed",
    }


def _spatial():
    return {
        "altitude_layers": [
            _layer(ALT_080_ID, 75.0, 85.0),
            #: 关键：ALT-100 的 bounds 与 route band [70, 160] 求交后是 [100, 115]，
            #: 因此 generic midpoint = 107.5，而 model altitude = 100.0。
            _layer(ALT_100_ID, 100.0, 115.0),
            _layer(ALT_150_ID, 145.0, 155.0),
        ],
        "route_altitude_profiles": {"R1": {
            "route_id": "R1", "mode": "constant",
            "vertical_reference": "egm2008_orthometric",
            "constant_altitude_m": ROUTE_ALTITUDE_M, "waypoints": [],
            "confirmed": True, "status": "confirmed", "geoid_undulation_m": None,
        }},
        "site_vertical_profiles": {},
    }


def _policy():
    return normalize_cns_corridor_policy({"routes": {"R1": {
        "route_id": "R1", "horizontal_half_width_m": 0.0,
        #: band = [100-30, 100+60] = [70, 160]：三层都产生 voxel，且
        #: ALT-100 overlap = [100, 115] ⇒ midpoint 107.5。
        "vertical_lower_margin_m": 30.0, "vertical_upper_margin_m": 60.0,
        "confirmed": True, "source": "round29n_fixture",
    }}})


RID_SERVICE_MODEL = {
    "model_family": "declared_performance", "version": "1",
    "technology": "network_remote_id", "parameters": {"performance": {}},
    "source": "round29n_fixture", "confirmed": True,
}
COMMUNICATION_SERVICE_MODEL = {
    "model_family": "declared_performance", "version": "1",
    "technology": "dedicated_radio", "parameters": {"performance": {"max_latency_s": 0.2}},
    "source": "round29n_fixture", "confirmed": True,
}


def _rid_device(device_id):
    return normalize_device({
        "device_id": device_id, "name": device_id, "subsystem": "S", "role": "existing",
        "radius_m": 2000.0, "mtbf_h": 1000, "service_key": RID,
        "type": {
            "service_subtype": "cooperative_surveillance",
            "technology": "network_remote_id", "target_cooperation": "cooperative",
        },
        "performance": {"min_redundancy": 1},
        "coverage_geometry": {
            "model": "hemisphere", "source": "round29n_fixture", "confirmed": True,
            "radius_by_surface": dict(RID_RADIUS_BY_SURFACE),
            "urban_radius_m": RID_URBAN_RADIUS_M, "urban_enabled": False,
        },
        "service_model": deepcopy(RID_SERVICE_MODEL),
    })


def _communication_device(device_id):
    return normalize_device({
        "device_id": device_id, "name": device_id, "subsystem": "C", "role": "existing",
        "radius_m": 4000.0, "mtbf_h": 1000,
        "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
        "performance": {"max_latency_s": 0.2, "min_redundancy": 1},
        "coverage_geometry": {
            "model": "sphere", "slant_range_m": 4000.0,
            "source": "round29n_fixture", "confirmed": True,
        },
        "service_model": deepcopy(COMMUNICATION_SERVICE_MODEL),
    })


def _installed(device_id, subsystem, service_model):
    """设施上**已安装**的设备条目（其 ``service_model`` 是设施侧权威证据）。"""

    return {
        "device_id": device_id, "subsystem": subsystem, "status": "active",
        "service_model": deepcopy(service_model),
    }


def _facilities():
    """两个独立 RID 站址（land 要求 2 站址）+ 一个 Communication 站址，都在航路旁。"""

    return {"status": "passed", "items": [
        {"facility_id": "F1", "site_id": "SITE-A", "coordinate": [0.0, 0.0], "status": "active",
         "vertical_profile": {"service_origin_egm2008_m": 0.0, "confirmed": True},
         "devices": [
             _installed("RID-1", "S", RID_SERVICE_MODEL),
             _installed("C-1", "C", COMMUNICATION_SERVICE_MODEL),
         ]},
        {"facility_id": "F2", "site_id": "SITE-B", "coordinate": [0.001, 0.0], "status": "active",
         "vertical_profile": {"service_origin_egm2008_m": 0.0, "confirmed": True},
         "devices": [_installed("RID-2", "S", RID_SERVICE_MODEL)]},
    ]}


def _catalog():
    return {"status": "passed", "items": [
        _rid_device("RID-1"), _rid_device("RID-2"), _communication_device("C-1"),
    ]}


def _aircraft():
    return {
        "aircraft_id": "A1",
        "communication": {
            "confirmed": True, "status": "confirmed", "capabilities": ["radio"],
            "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
            "performance": {"max_latency_s": 0.5, "min_redundancy": 1},
        },
        "navigation": {},
        "surveillance": {
            "confirmed": True, "status": "confirmed", "capabilities": ["rid"],
            "type": {"technology": "network_remote_id", "target_cooperation": "cooperative"},
            "performance": {},
        },
    }


def _required(*, rid=True, radar=True, navigation_endpoint=False):
    services = {}
    if rid:
        services[RID] = {"required": True, "service_key": RID, "confirmed": True}
    if radar:
        services[RADAR] = {"required": True, "service_key": RADAR, "confirmed": True}
    navigation = {"required": False, "status": "passed", "type": {}, "performance": {}}
    if navigation_endpoint:
        navigation = {
            "required": True, "status": "passed", "type": {}, "performance": {},
            "services": {
                SERVICE_KEY_NAVIGATION_INTEGRITY_MONITORING: {
                    "required": True, "service_key": SERVICE_KEY_NAVIGATION_INTEGRITY_MONITORING,
                    "confirmed": True,
                },
            },
        }
    surveillance = {
        "required": True, "status": "passed",
        "type": {"technology": "network_remote_id", "target_cooperation": "cooperative"},
        "performance": {"min_redundancy": 1},
        "redundancy_by_surface": {"land": 2, "coastal_uncertain": 2, "sea": 1},
        "services": services,
    }
    if len(services) > 1:
        surveillance["service_requirement_mode"] = "all_required"
    return {
        "status": "passed",
        "project_default": {
            "communication": {
                "required": True, "status": "passed",
                "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
                "performance": {"max_latency_s": 1.0, "min_redundancy": 1},
            },
            "navigation": navigation,
            "surveillance": surveillance,
        },
        "route_overrides": {},
    }


def _radar_evidence(*, altitude=ROUTE_ALTITUDE_M, altitude_layer_id=ALT_100_ID,
                    surface="land", selected=None):
    return build_radar_service_evidence(
        _required(),
        radar_layout(
            surface=surface, selected=selected or [panel("A1", "A", 90.0)],
            altitude=altitude, altitude_layer_id=altitude_layer_id,
        ),
    )


def _evaluate(*, requirements=None, facilities=None, catalog=None,
              radar_evidence=None, spatial=None):
    return CNSServiceCorridorV1().evaluate(
        [ROUTE], spatial or _spatial(), GRID, TERRAIN,
        requirements or _required(),
        _aircraft(), _facilities() if facilities is None else facilities,
        _catalog() if catalog is None else catalog, _policy(),
        radar_service_evidence=(
            _radar_evidence() if radar_evidence is None else radar_evidence
        ),
        radar_metric_projector=identity_radar_projection,
    )


def _voxel(result, layer_id):
    route = result["routes"][0]
    return next(
        item for item in route["voxels"]
        if item.get("altitude_layer_id") == layer_id
    )


def _service(voxel, code, service_key):
    entry = next(item for item in voxel["subsystems"] if item["subsystem"] == code)
    return next(
        item for item in entry.get("service_redundancy") or []
        if str(item.get("service_key")) == service_key
    ), entry


def _probe(x, y, *, layer_id, overlap, altitude, voxel_id="V1", surface="land"):
    return {
        "voxel_id": voxel_id, "longitude": float(x), "latitude": float(y),
        "altitude_egm2008_m": altitude, "altitude_layer_id": layer_id,
        "overlap_height_egm2008_m": overlap, "surface_class": surface,
        "nearest_route_offset_m": 0.0,
    }


# ---------------------------------------------------------------------------
# 1-6：P14 真实 fixture —— generic probe 不变，Radar 用 service-specific 高度
# ---------------------------------------------------------------------------


def test_shared_generic_voxel_midpoint_is_never_rewritten_to_the_radar_altitude():
    result = _evaluate()
    voxel = _voxel(result, ALT_100_ID)
    #: generic probe 仍然服务 C / RID / legacy 几何覆盖与体积代理：107.5 不得被改写。
    assert voxel["layer_height_egm2008_m"] == [100.0, 115.0]
    assert voxel["overlap_height_egm2008_m"] == [100.0, 115.0]
    assert voxel["probe"]["altitude_egm2008_m"] == GENERIC_ALT_100_MIDPOINT_M
    assert _voxel(result, ALT_080_ID)["probe"]["altitude_egm2008_m"] == 80.0
    assert _voxel(result, ALT_150_ID)["probe"]["altitude_egm2008_m"] == 150.0


def test_alt_100_voxel_dispatches_on_the_canonical_radar_altitude_not_the_midpoint():
    result = _evaluate()
    radar, _ = _service(_voxel(result, ALT_100_ID), "S", RADAR)
    assert radar["status"] in ("satisfied", "confirmed_deficit")
    assert "radar_model_scope_altitude_not_supported" not in radar["reasons"]
    assert radar["applicable"] is True
    assert radar["vertical_scope"] == RADAR_VERTICAL_SCOPE_OPERATIONAL_ALTITUDE_LAYER
    #: 两种高度必须并列可审计：
    assert radar["corridor_voxel_representative_altitude_egm2008_m"] == (
        GENERIC_ALT_100_MIDPOINT_M
    )
    assert radar["radar_evaluation_altitude_egm2008_m"] == RADAR_MODEL_ALTITUDE_M
    assert radar["model_supported_altitude_egm2008_m"] == RADAR_MODEL_ALTITUDE_M
    assert radar["voxel_altitude_layer_id"] == ALT_100_ID
    assert radar["altitude_layer_id"] == ALT_100_ID


def test_canonical_site_coverage_actually_receives_the_radar_model_altitude(monkeypatch):
    import cns_planner.domain.radar_service_evidence as adapter

    calls = []
    canonical = adapter.actual_site_coverage

    def recording(**kwargs):
        calls.append(deepcopy(kwargs))
        return canonical(**kwargs)

    monkeypatch.setattr(adapter, "actual_site_coverage", recording)
    _evaluate()
    #: 只有 ALT-100 体元适用 Radar；它必须收到 100.0 m，而**不是** 107.5 m。
    assert calls, "ALT-100 Radar probe 必须调用 canonical actual_site_coverage"
    assert [item["samples"][0]["egm2008_m"] for item in calls] == [RADAR_MODEL_ALTITUDE_M]
    assert calls[0]["samples"][0]["sample_id"] == f"voxel:G1@{ALT_100_ID}"


@pytest.mark.parametrize("layer_id", [ALT_080_ID, ALT_150_ID])
def test_off_layer_radar_entries_are_not_applicable(layer_id):
    result = _evaluate()
    radar, entry = _service(_voxel(result, layer_id), "S", RADAR)
    assert radar["status"] == "not_applicable"
    assert radar["applicable"] is False
    assert radar["reasons"] == [RADAR_NOT_APPLICABLE_REASON]
    assert radar["vertical_scope"] == RADAR_VERTICAL_SCOPE_OPERATIONAL_ALTITUDE_LAYER
    assert radar["radar_evaluation_altitude_egm2008_m"] is None
    assert radar["model_supported_altitude_egm2008_m"] == RADAR_MODEL_ALTITUDE_M
    assert radar["voxel_altitude_layer_id"] == layer_id
    #: off-layer 不得把 S 子系统拖成 unknown：该体元由 RID 如实决定。
    assert entry["planning_status"] == "satisfied"


def test_off_layer_radar_never_calls_canonical_site_coverage(monkeypatch):
    import cns_planner.domain.radar_service_evidence as adapter

    calls = []
    canonical = adapter.actual_site_coverage

    def recording(**kwargs):
        calls.append(deepcopy(kwargs))
        return canonical(**kwargs)

    monkeypatch.setattr(adapter, "actual_site_coverage", recording)
    result = _evaluate()
    assert len(calls) == 1
    #: 唯一一次调用属于 ALT-100；ALT-080 / ALT-150 从未进入几何评估。
    assert calls[0]["samples"][0]["sample_id"] == f"voxel:G1@{ALT_100_ID}"
    assert len(result["routes"][0]["voxels"]) == 3


# ---------------------------------------------------------------------------
# 7-8：缺失 / 冲突证据一律 fail-closed 为 unknown（绝不是 not_applicable）
# ---------------------------------------------------------------------------


def test_missing_voxel_altitude_layer_metadata_is_unknown_not_not_applicable():
    evidence = _radar_evidence()
    result = evidence_for_probe(
        evidence, "R1", _probe(0.0, 0.0, layer_id=None, overlap=[95.0, 105.0], altitude=100.0),
        metric_projector=identity_radar_projection,
    )
    assert result["status"] == "unknown"
    assert result["reasons"] == ["radar_voxel_altitude_layer_evidence_required"]
    assert result["applicable"] is None


def test_missing_radar_layout_layer_metadata_is_unknown_not_not_applicable():
    source = radar_layout(
        surface="land", selected=[panel("A1", "A", 90.0)],
        altitude=RADAR_MODEL_ALTITUDE_M, altitude_layer_id=ALT_100_ID,
    )
    del source["items"][0]["altitude_layer_id"]
    evidence = build_radar_service_evidence(_required(), source)
    assert evidence["routes"][0]["status"] == "evidence_required"
    result = evidence_for_probe(
        evidence, "R1",
        _probe(0.0, 0.0, layer_id=ALT_150_ID, overlap=[145.0, 155.0], altitude=150.0),
        metric_projector=identity_radar_projection,
    )
    assert result["status"] == "unknown"
    assert result["reasons"]
    assert result["status"] != "not_applicable"


def test_same_layer_but_model_altitude_outside_voxel_overlap_is_unknown():
    evidence = _radar_evidence()
    result = evidence_for_probe(
        evidence, "R1",
        _probe(0.0, 0.0, layer_id=ALT_100_ID, overlap=[130.0, 140.0], altitude=135.0),
        metric_projector=identity_radar_projection,
    )
    assert result["status"] == "unknown"
    assert result["reasons"] == ["radar_model_altitude_outside_voxel_vertical_overlap"]
    assert result["radar_evaluation_altitude_egm2008_m"] is None


# ---------------------------------------------------------------------------
# 9-13：C / RID 完全不受影响；S all_required 正确消化 not_applicable
# ---------------------------------------------------------------------------


def test_rid_is_still_evaluated_on_off_layer_voxels():
    result = _evaluate()
    for layer_id in (ALT_080_ID, ALT_100_ID, ALT_150_ID):
        rid, _ = _service(_voxel(result, layer_id), "S", RID)
        assert rid["status"] == "satisfied", layer_id
        assert rid["distinct_site_count"] == 2


def test_communication_is_still_evaluated_on_off_layer_voxels():
    result = _evaluate()
    for layer_id in (ALT_080_ID, ALT_150_ID):
        entry = next(
            item for item in _voxel(result, layer_id)["subsystems"]
            if item["subsystem"] == "C"
        )
        assert entry["geometry"]["covered"] is True
        assert entry["p8_status"] not in ("not_applicable", None)


def test_s_subsystem_rid_satisfied_plus_radar_not_applicable_is_satisfied():
    result = _evaluate()
    _, entry = _service(_voxel(result, ALT_080_ID), "S", RADAR)
    assert entry["planning_status"] == "satisfied"
    #: 同一体元上 ALT-100 的 Radar 缺口不属于它。
    assert entry["planning_status"] == aggregate_required_service_status(
        ["satisfied", "not_applicable"]
    )


def test_s_subsystem_rid_deficit_plus_radar_not_applicable_is_confirmed_deficit():
    #: 移除全部既有设施 ⇒ RID 无覆盖 ⇒ 已确认缺口；Radar 仍然 not_applicable。
    result = _evaluate(facilities={"status": "passed", "items": []})
    _, entry = _service(_voxel(result, ALT_080_ID), "S", RADAR)
    rid, entry = _service(_voxel(result, ALT_080_ID), "S", RID)
    assert rid["status"] == "confirmed_deficit"
    assert entry["planning_status"] == "confirmed_deficit"


def test_s_subsystem_rid_satisfied_plus_radar_deficit_on_alt_100_is_confirmed_deficit():
    result = _evaluate()
    radar, entry = _service(_voxel(result, ALT_100_ID), "S", RADAR)
    rid, _ = _service(_voxel(result, ALT_100_ID), "S", RID)
    #: 单个 Radar 站址在 land 上不满足 2 站址要求 ⇒ 已确认缺口。
    assert radar["status"] == "confirmed_deficit"
    assert rid["status"] == "satisfied"
    assert entry["planning_status"] == "confirmed_deficit"


# ---------------------------------------------------------------------------
# 14-20：P15 / P16 service-level not_applicable
# ---------------------------------------------------------------------------


def _gap_and_targets():
    requirements = _required()
    corridor = _evaluate(requirements=requirements)
    gap = CNSCorridorGapAnalyzerV1().evaluate(corridor, requirements, {})
    targets, unknown = _targets(gap)
    return corridor, gap, targets, unknown


def _s_summary(gap):
    return next(
        item for item in gap["routes"][0]["subsystems"] if item["subsystem"] == "S"
    )


def test_p15_reports_radar_applicable_and_not_applicable_voxel_counts():
    _, gap, _, _ = _gap_and_targets()
    radar = next(
        item for item in _s_summary(gap)["service_redundancy"]
        if item["service_key"] == RADAR
    )
    assert radar["applicable_voxel_count"] == 1
    assert radar["not_applicable_voxel_count"] == 2
    assert radar["voxel_count"] == 3
    assert radar["status_counts"] == {
        "satisfied": 0, "confirmed_deficit": 1, "unknown": 0, "not_applicable": 2,
    }
    assert radar["status"] == "confirmed_deficit"


def test_p15_off_layer_radar_never_enters_continuous_deficit_or_objectives():
    _, gap, _, _ = _gap_and_targets()
    summary = _s_summary(gap)
    #: S 子系统的 corridor requirement 仍由 RID 覆盖全部 3 个体元（不因 Radar off-layer 缩水）。
    assert summary["required_voxel_count"] == 3
    voxel_ids = [item["voxel_id"] for item in gap["routes"][0]["voxels"]]
    assert set(summary["confirmed_target_voxel_ids"]) == {f"G1@{ALT_100_ID}"}
    assert summary["unknown_voxel_ids"] == []
    for segment in summary["continuous_deficit_segments"]:
        assert segment["voxel_ids"] == [f"G1@{ALT_100_ID}"]
    #: off-layer 体元本身是 satisfied（由 RID 决定），既不进入缺口也不进入 unknown。
    for voxel in gap["routes"][0]["voxels"]:
        entry = next(item for item in voxel["subsystems"] if item["subsystem"] == "S")
        if voxel["altitude_layer_id"] == ALT_100_ID:
            assert entry["combined_status"] == "confirmed_gap"
        else:
            assert entry["combined_status"] == "satisfied"
    assert voxel_ids  # 三个体元都参与聚合


def test_p16_off_layer_radar_produces_zero_targets_and_zero_unknown_evidence():
    _, _, targets, unknown = _gap_and_targets()
    radar_targets = [item for item in targets if item.get("service_key") == RADAR]
    assert [item["voxel_id"] for item in radar_targets] == [f"G1@{ALT_100_ID}"]
    assert [item for item in unknown if item.get("service_key") == RADAR] == []
    off_layer_ids = {f"G1@{ALT_080_ID}", f"G1@{ALT_150_ID}"}
    assert not [
        item for item in targets if str(item.get("voxel_id") or "") in off_layer_ids
    ]


def test_p16_alt_100_radar_gap_becomes_a_terminal_managed_gap():
    _, _, targets, _ = _gap_and_targets()
    radar_targets = [item for item in targets if item.get("service_key") == RADAR]
    assert radar_targets
    state = {
        "radar_surveillance_layout": {
            "status": "infeasible",
            "items": [{
                "route_id": "R1", "status": "infeasible",
                "gap_reason": "independent_site_count_limited",
                "gap_classification": "confirmed_gap", "managed_physical_gap": True,
                "solver": {"infeasibility_proven": True},
            }],
        },
        "result_statuses": {"radar_surveillance_layout": "infeasible"},
    }
    terminal, unknowns, actionable = _radar_gap_authority(radar_targets, state)
    assert len(terminal) == 1
    gap_entry = terminal[0]
    assert gap_entry["gap_kind"] == "non_actionable_terminal_managed_gap"
    assert gap_entry["p16_actionable"] is False
    assert gap_entry["managed_physical_gap"] is True
    assert gap_entry["source"] == "radar_surveillance_layout"
    assert gap_entry["satisfied"] is False
    assert unknowns == []
    assert actionable == []


def test_p16_never_reintroduces_a_directional_radar_action():
    from cns_planner.application.site_candidate_actions import candidate_actions

    _, _, targets, _ = _gap_and_targets()
    radar_targets = [item for item in targets if item.get("service_key") == RADAR]
    catalog = {"status": "passed", "items": [{
        "device_id": "RADAR", "subsystem": "S", "service_key": RADAR, "enabled": True,
    }]}
    assert candidate_actions(radar_targets, {"items": []}, {"items": []}, catalog) == []


def test_navigation_endpoint_scope_still_makes_corridor_n_not_applicable():
    requirements = _required(rid=False, radar=False, navigation_endpoint=True)
    result = _evaluate(requirements=requirements, radar_evidence=None)
    entry = next(
        item for item in _voxel(result, ALT_100_ID)["subsystems"]
        if item["subsystem"] == "N"
    )
    assert entry["planning_status"] == "not_applicable"
    assert entry.get("service_redundancy") == []


# ---------------------------------------------------------------------------
# 21-24：版本 / stale / Radar V1.3 不受影响
# ---------------------------------------------------------------------------


def test_round29n_algorithm_versions_are_bumped():
    from cns_planner.algorithms.corridor_gap.v1 import CNSCorridorGapAnalyzerV1 as P15

    assert CNSServiceCorridorV1.algorithm_version == "1.3"
    assert P15.algorithm_version == "1.2"
    assert CorridorReuseFirstSitePlannerV2.algorithm_version == "2.2"
    assert empty_cns_corridor_assessment()["algorithm_version"] == "1.3"
    assert empty_cns_corridor_gap_assessment()["algorithm_version"] == "1.2"
    assert empty_cns_corridor_site_plan()["algorithm_version"] == "2.2"


def test_wrong_voxel_altitude_layer_is_never_satisfied_by_the_same_100m_result():
    """绝不能把 ALT-100 的结果复用到 off-layer：off-layer 根本没有评估高度。"""

    evidence = _radar_evidence()
    result = evidence_for_probe(
        evidence, "R1",
        _probe(0.0, 0.0, layer_id=ALT_150_ID, overlap=[145.0, 155.0], altitude=150.0),
        metric_projector=identity_radar_projection,
    )
    assert result["status"] == "not_applicable"
    assert result["distinct_site_count"] is None
    assert result["distinct_site_ids"] == []
    assert result["required_distinct_site_count"] is None


@pytest.mark.parametrize(
    "result_key, factory, stale_version, snapshot_name",
    [
        ("cns_corridor_assessment", empty_cns_corridor_assessment, "1.2",
         "cns_corridor_snapshot"),
        ("cns_corridor_gap_assessment", empty_cns_corridor_gap_assessment, "1.1",
         "cns_corridor_gap_snapshot"),
        ("cns_corridor_site_plan", empty_cns_corridor_site_plan, "2.1",
         "cns_corridor_site_plan_snapshot"),
    ],
)
def test_previous_algorithm_versions_are_reported_stale(
    tmp_path, result_key, factory, stale_version, snapshot_name,
):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    workflow.state[result_key] = {
        **factory("proposal_ready"), "algorithm_version": stale_version,
        "input_fingerprint": f"round29n-{result_key}",
    }
    snapshot = getattr(workflow, snapshot_name)()
    assert snapshot["status"] == "stale"
    assert snapshot["stale_reason"] == "algorithm_semantics_changed"


def test_compacted_validation_samples_fall_back_to_canonical_layout_samples():
    """持久化裁剪掉 ``validation.samples`` 后，Radar 仍必须能从 layout 自己的
    canonical 路线样本副本（``service_evidence_inputs.samples``）判定 currentness。

    否则真实项目（``radar.layout.detail`` external scope 裁剪）上 Radar 会永久停在
    ``evidence_required``：ALT-100 无法评估，off-layer 也永远得不到 not_applicable。
    """

    source = radar_layout(
        surface="land", selected=[panel("A1", "A", 90.0)],
        altitude=RADAR_MODEL_ALTITUDE_M, altitude_layer_id=ALT_100_ID,
    )
    #: 模拟 project_compaction 的 external-scope 裁剪（validation.samples 被移除）。
    source["items"][0]["validation"]["samples"] = []
    evidence = build_radar_service_evidence(_required(), source)
    route = evidence["routes"][0]
    assert route["status"] == "current"
    assert route["model_supported_altitude_egm2008_m"] == RADAR_MODEL_ALTITUDE_M

    applicable = evidence_for_probe(
        evidence, "R1",
        _probe(0.0, 0.0, layer_id=ALT_100_ID, overlap=[100.0, 115.0], altitude=107.5),
        metric_projector=identity_radar_projection,
    )
    assert applicable["status"] in ("satisfied", "confirmed_deficit")
    assert applicable["radar_evaluation_altitude_egm2008_m"] == RADAR_MODEL_ALTITUDE_M
    off_layer = evidence_for_probe(
        evidence, "R1",
        _probe(0.0, 0.0, layer_id=ALT_150_ID, overlap=[145.0, 155.0], altitude=150.0),
        metric_projector=identity_radar_projection,
    )
    assert off_layer["status"] == "not_applicable"


def test_radar_layout_v1_3_geometry_is_untouched_and_read_only():
    assert RADAR_ALGORITHM_ID == "radar_surveillance_layout"
    assert RADAR_ALGORITHM_VERSION == "1.3"
    source = radar_layout(
        surface="land", selected=[panel("A1", "A", 90.0)],
        altitude=RADAR_MODEL_ALTITUDE_M, altitude_layer_id=ALT_100_ID,
    )
    before = deepcopy(source)
    evidence = build_radar_service_evidence(_required(), source)
    evidence_for_probe(
        evidence, "R1",
        _probe(0.0, 0.0, layer_id=ALT_100_ID, overlap=[100.0, 115.0], altitude=107.5),
        metric_projector=identity_radar_projection,
    )
    assert source == before
