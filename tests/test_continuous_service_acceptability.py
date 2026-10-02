"""Round 2.5 —— P17 连续服务 / 不可接受事件评估的契约与端到端回归。

覆盖（对应用户验收清单）：

* C 全失联时长（阈值内 ⇒ managed_gap；超阈值 ⇒ unacceptable）
* C 冗余退化（**独立阈值**，与全失联互不共用）
* RTK → GNSS 降级（acceptable_degraded，不是"断 X 秒即失败"）
* GNSS 不可用（unacceptable，ATTI 降落）
* 保护走廊 ``D_protection = D_safety + V_relative × T_chain + D_uncertainty``
* 监视验收 ``T_margin = T_available - T_chain``（正 / 负 / 缺值）
* managed_gap 的强制披露（service / 位置 / 长度 / 时长 / 阈值 / mitigation / 依据）
* unknown 继续 fail-closed
* Step6（P18）门禁：允许 fully_satisfied / acceptable_with_managed_gap，阻止其余
* 保存 → 重开后 P17 结论与来源**逐字保留**
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from cns_planner.algorithms.continuous_service.v1 import ContinuousServiceAcceptabilityV1
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.cns_continuous_service import (
    aggregate_acceptability, continuous_gap_duration, default_continuous_service_policy,
    default_operation_scenario, managed_gap_disclosure, normalize_continuous_service_policy,
    protection_distance, subsystem_status_from_events, surveillance_acceptance,
    time_chain_total,
)
from cns_planner.domain.fc30_profile import (
    FC30_AIRCRAFT_ID, FC30_FAILSAFE_TRIGGER_SEMANTICS, fc30_aircraft_profile,
    fc30_declared_facts, fc30_planning_speeds,
)


DEFAULTS = Path("cns_planner/config/defaults.json")
MODEL = ContinuousServiceAcceptabilityV1()


# ---------------------------------------------------------------------------
# 合成输入（只构造 P14/P15 的**已确认结构**；数值全部显式，绝不猜测）
# ---------------------------------------------------------------------------


def _subsystem(code, *, causes, length_m, status="failed", service_key=None,
               segments=None, service_redundancy=None):
    entry = {
        "subsystem": code, "status": status, "causes": list(causes),
        "required_voxel_count": 10,
        "max_continuous_deficit_projection_m": length_m,
        "total_confirmed_deficit_projection_m": length_m,
        "continuous_deficit_segments": segments if segments is not None else (
            [{
                "segment_id": f"R1:{code}:spatial-deficit:1",
                "route_id": "R1", "subsystem": code,
                "start_route_offset_m": 100.0, "end_route_offset_m": 100.0 + length_m,
                "length_m": length_m, "causes": list(causes), "voxel_ids": [f"G@L{code}"],
                "semantics": "conservative_longitudinal_projection_of_corridor_voxel_deficits",
            }] if length_m else []
        ),
        "service_redundancy": service_redundancy or [],
        "reasons": [],
    }
    return entry


def _p15(routes_):
    return {
        "status": "failed", "algorithm_id": "cns_corridor_gap_v1",
        "algorithm_version": "1.0", "input_fingerprint": "p15-fp",
        "route_count": len(routes_), "routes": routes_,
    }


def _p14(*, route_length_m=10000.0, half_width_m=500.0, voxels=None, path=None):
    return {
        "status": "failed", "algorithm_id": "cns_service_corridor_v1",
        "algorithm_version": "1.0", "input_fingerprint": "p14-fp",
        "route_count": 1,
        "routes": [{
            "route_id": "R1", "route_length_m": route_length_m, "status": "failed",
            "voxel_count": len(voxels or []),
            "corridor_geometry": {
                "route_path": path if path is not None else [[122.0, 30.0], [122.1, 30.1]],
                "horizontal_half_width_m": half_width_m,
            },
            "voxels": voxels if voxels is not None else _voxels(),
        }],
    }


def _voxels(*, count=10, spacing=1000.0, surface="sea", covered=False):
    """沿航路的等间距采样点（含 surface 与保护走廊探测所需的几何）。"""

    result = []
    for index in range(count):
        result.append({
            "voxel_id": f"G{index}@L1",
            "nearest_route_offset_m": spacing * (index + 1),
            "cell_half_diagonal_m": spacing / 2.0,
            "surface_class": surface,
            "probe": {"distance_along_route_m": spacing * (index + 1)},
            "subsystems": [
                {"subsystem": code, "planning_status": "confirmed_deficit"
                 if not covered else "satisfied"}
                for code in ("C", "N", "S")
            ],
        })
    return result


def _route(*, length_m=10000.0, covers=None, surplus=None, service_key=None,
           gap_causes=("service_deficit", "redundancy_deficit")):
    causes = list(gap_causes)
    if not causes:
        c_segments, s_segments = [], []
    else:
        c_segments = [{
            "segment_id": "R1:C:spatial-deficit:1", "route_id": "R1", "subsystem": "C",
            "start_route_offset_m": 100.0, "end_route_offset_m": 100.0 + length_m,
            "length_m": length_m, "causes": causes, "voxel_ids": ["G@LC"],
            "semantics": "conservative_longitudinal_projection_of_corridor_voxel_deficits",
        }]
        s_segments = [{**c_segments[0], "segment_id": "R1:S:spatial-deficit:1",
                       "subsystem": "S", "voxel_ids": ["G@LS"]}]
    return {
        "route_id": "R1", "route_length_m": length_m, "status": "failed",
        "subsystems": [
            _subsystem("C", causes=causes, length_m=length_m, segments=c_segments,
                       service_key=service_key),
            _subsystem("N", causes=[], length_m=0.0, segments=[],
                       status="not_applicable", service_redundancy=[]),
            _subsystem("S", causes=causes, length_m=length_m, segments=s_segments,
                       service_redundancy=surplus or []),
        ],
    }


def _aircraft(*, route_speed=20.0, detection_range=3000.0):
    """选定机载档案（速度 + 协作监视探测距离），避免无关子系统拖成 unknown。"""

    return {
        "aircraft_id": "SYN-01", "cruise_speed_mps": route_speed, "max_speed_mps": 25.0,
        "surveillance": {
            "confirmed": True, "status": "confirmed",
            "type": {"technology": "adsb", "target_cooperation": "cooperative"},
            "performance": {"min_detection_range_m": detection_range},
            "source": "declared airborne surveillance geometry",
        },
    }


def _evaluate(**overrides):
    inputs = {
        "corridor_assessment": _p14(),
        "corridor_gap_assessment": _p15([_route()]),
        "site_plan": {},
        "aircraft_profile": _aircraft(),
        "planning_evidence": {},
        "operation_scenario": default_operation_scenario(),
        "continuous_service_policy": default_continuous_service_policy(),
        "device_catalog": {},
        "radar_surveillance_layout": {},
    }
    inputs.update(overrides)
    return MODEL.evaluate(**inputs)


def _assumption(field, value):
    return {
        "evidence_id": f"PEV-{field}", "scope": "project", "field": field,
        "value": value, "source_type": "engineering_assumption",
        "source": "pytest fixture",
        "statement": f"本测试场景显式声明 {field}。",
        "reason": "使 P17 可判定",
        "report_disclosure": "本项为测试工程假设。",
        "declared_by": "test", "confirmed": True, "confirmed_by_user": True,
    }


def _registry(*items):
    return {"schema_version": "round2.4-engineering-evidence", "items": list(items)}


def _with_navigation(**overrides):
    """补上导航证据（RTK/GNSS 可用）后再评估，使 C/S 断言不被 N 的 unknown 掩盖。

    调用方显式传入 ``planning_evidence`` 时**以调用方为准**（不再叠加默认证据），
    否则"缺证据 ⇒ unknown"这类断言会被这里的默认证据悄悄改掉。
    """

    overrides.setdefault("planning_evidence", _registry(
        _assumption("rtk_availability", "available"),
        _assumption("gnss_availability", "available"),
    ))
    return _evaluate(**overrides)


def _route_result(result):
    return result["routes"][0]


def _subsystem_entry(result, code):
    return next(
        item for item in _route_result(result)["subsystems"] if item["subsystem"] == code
    )


# ---------------------------------------------------------------------------
# 1. 纯函数契约
# ---------------------------------------------------------------------------


def test_continuous_gap_duration_is_length_over_current_route_speed():
    assert continuous_gap_duration(1000.0, 20.0) == pytest.approx(50.0)
    #: 缺航路速度 ⇒ 不可判定（绝不猜）。
    assert continuous_gap_duration(1000.0, None) is None
    assert continuous_gap_duration(None, 20.0) is None
    assert continuous_gap_duration(1000.0, 0.0) is None


def test_protection_distance_formula_and_unknown_semantics():
    assert protection_distance(0.0, 40.0, 10.0, 0.0) == pytest.approx(400.0)
    assert protection_distance(100.0, 40.0, 10.0, 25.0) == pytest.approx(525.0)
    assert protection_distance(None, 40.0, 10.0, 0.0) is None
    assert protection_distance(0.0, 40.0, None, 0.0) is None
    assert protection_distance(0.0, 40.0, 10.0, None) is None
    with pytest.raises(ValueError):
        protection_distance(-1.0, 40.0, 10.0, 0.0)


def test_time_chain_requires_every_component():
    complete = {
        "detect_track": 3.0, "sensor_to_platform": 1.0, "platform_processing": 2.0,
        "platform_to_aircraft": 1.0, "aircraft_response_manoeuvre": 3.0,
    }
    assert time_chain_total(complete) == pytest.approx(10.0)
    assert time_chain_total({**complete, "platform_processing": None}) is None


def test_surveillance_acceptance_margin_sign_and_unknown():
    positive = surveillance_acceptance(5000.0, 0.0, 40.0, 10.0)
    assert positive["status"] == "acceptable"
    assert positive["t_available_s"] == pytest.approx(125.0)
    assert positive["t_margin_s"] == pytest.approx(115.0)

    negative = surveillance_acceptance(200.0, 0.0, 40.0, 10.0)
    assert negative["status"] == "unacceptable"
    assert negative["t_margin_s"] == pytest.approx(5.0 - 10.0)

    missing = surveillance_acceptance(None, 0.0, 40.0, 10.0)
    assert missing == {
        "status": "unknown", "t_available_s": None, "t_margin_s": None,
        "reason": "监视验收所需的输入不完整",
    }


def test_managed_gap_disclosure_never_claims_full_coverage():
    entry = {
        "service": "C:communication", "kind": "service_outage", "route_id": "R1",
        "start_offset_m": 100.0, "end_offset_m": 160.0, "length_m": 60.0,
        "duration_s": 3.0, "limit_s": 3.0,
        "mitigation": "复用既有站址缩小该段", "basis": "threshold=c_full_outage_max_s",
    }
    lines = managed_gap_disclosure(entry)
    text = "\n".join(lines)
    assert "managed gap" in text
    assert "服务中断（全失联）" in text
    assert "100.0–160.0 m" in text
    assert "60.0 m" in text
    assert "3.0 s" in text
    assert "复用既有站址缩小该段" in text
    assert "threshold=c_full_outage_max_s" in text
    assert "并非全覆盖" in text


def test_subsystem_status_known_exceedance_beats_unknown():
    assert subsystem_status_from_events(events=[], outage_limit_s=3.0,
                                        degradation_limit_s=10.0) == "fully_satisfied"
    assert subsystem_status_from_events(
        events=[{"kind": "service_outage", "duration_s": 3.0}],
        outage_limit_s=3.0, degradation_limit_s=10.0,
    ) == "acceptable_with_managed_gap"
    assert subsystem_status_from_events(
        events=[{"kind": "service_outage", "duration_s": None}],
        outage_limit_s=3.0, degradation_limit_s=10.0,
    ) == "unknown"
    #: 已知超阈值优先于未知（不能让"缺数据"掩盖已知的不可接受）。
    assert subsystem_status_from_events(
        events=[{"kind": "service_outage", "duration_s": 100.0},
                {"kind": "redundancy_degradation", "duration_s": None}],
        outage_limit_s=3.0, degradation_limit_s=10.0,
    ) == "unacceptable"


def test_aggregate_acceptability_prefers_known_unacceptable_over_unknown():
    """已知不可接受优先于未知：两者都阻止门禁，但不得把已确认结论显示成"不可判定"。"""

    assert aggregate_acceptability(["fully_satisfied", "fully_satisfied"]) == "fully_satisfied"
    assert aggregate_acceptability(["fully_satisfied", "acceptable_with_managed_gap"]) == (
        "acceptable_with_managed_gap"
    )
    assert aggregate_acceptability(["unacceptable", "unknown"]) == "unacceptable"
    assert aggregate_acceptability(["unknown", "unknown"]) == "unknown"
    assert aggregate_acceptability(["not_applicable"]) == "not_applicable"
    #: 子系统词汇也要被显式映射（nominal → fully_satisfied）。
    assert aggregate_acceptability(["nominal", "acceptable_degraded"]) == "fully_satisfied"


# ---------------------------------------------------------------------------
# 2. FC30 canonical profile
# ---------------------------------------------------------------------------


def test_fc30_canonical_profile_facts_and_semantics():
    profile = fc30_aircraft_profile()
    assert profile["aircraft_id"] == FC30_AIRCRAFT_ID
    assert profile["cruise_speed_mps"] == 15.0
    assert profile["max_horizontal_speed_mps"] == 20.0
    assert profile["mtow_kg"] == 95.0
    speeds = fc30_planning_speeds(profile)
    assert speeds == {"route_speed_mps": 15.0, "max_horizontal_speed_mps": 20.0}

    facts = {item["parameter"]: item for item in fc30_declared_facts()}
    assert facts["rtk_loss_fallback"]["value"] == "gnss"
    assert facts["gnss_loss_fallback"]["value"] == "atti_descend_asap"
    failsafe = facts["rc_loss_failsafe_trigger_s"]
    assert failsafe["value"] == 3.0
    assert failsafe["semantics"] == FC30_FAILSAFE_TRIGGER_SEMANTICS
    assert failsafe["not_a_regulatory_threshold"] is True
    #: 每一条事实都必须带出处（绝不冒充厂家/法规事实）。
    assert all(item["source"] and item["source_type"] for item in fc30_declared_facts())


def test_fc30_config_catalog_entry_matches_the_builtin_profile():
    import json

    catalog = json.loads(
        (Path("cns_planner/config/aircraft_profiles.json")).read_text(encoding="utf-8")
    )
    entry = next(
        item for item in catalog["items"] if item["aircraft_id"] == FC30_AIRCRAFT_ID
    )
    assert entry["cruise_speed_mps"] == 15.0
    assert entry["max_horizontal_speed_mps"] == 20.0
    assert entry["mtow_kg"] == 95.0
    assert entry["metadata"]["fc30_facts"] == fc30_aircraft_profile()["metadata"]["fc30_facts"]
    assert entry["metadata"]["failsafe_trigger_semantics"] == FC30_FAILSAFE_TRIGGER_SEMANTICS


def test_empty_route_scope_is_fully_satisfied_not_unknown():
    """没有任何航路可评估 ⇒ 本层没有可拒绝的对象（fully_satisfied）。

    这与"有航路却评估不了"必须分开：后者保持 unknown（fail-closed）。
    """

    result = _with_navigation(
        corridor_assessment={
            "status": "passed", "input_fingerprint": "p14-empty", "routes": [],
        },
        corridor_gap_assessment={
            "status": "passed", "input_fingerprint": "p15-empty", "routes": [],
        },
    )
    assert result["status"] == "fully_satisfied"
    assert result["route_count"] == 0


def test_route_without_voxel_detail_is_unknown_not_fully_satisfied():
    """航路存在但逐体元明细缺失 ⇒ 保护走廊覆盖**不可判定**（不能宣称没有缺口）。

    注意：C/S 的连续缺口时长只依赖 P15 段的长度与航路速度，因此它仍然可判定；
    真正无法判定的是"保护走廊内的监视覆盖缺口"。本断言同时锁定这两件事。
    """

    result = _with_navigation(
        corridor_assessment=_p14(voxels=[]),
        corridor_gap_assessment=_p15([_route(length_m=200.0)]),
    )
    route = _route_result(result)
    #: 逐体元明细缺失 ⇒ 保护走廊覆盖缺口无法构造（不制造"整条走廊未覆盖"的结论）；
    #: 此时监视验收只由"已声明探测能力 + T_chain"决定，仍然可判定。
    assert route["events_by_kind"]["surveillance_detection_gap"] == []
    corridor_subsystem = next(
        item for item in route["subsystems"] if item["subsystem"] == "S"
        and "acceptance" in item
    )
    assert corridor_subsystem["acceptance"]["status"] == "acceptable"
    #: 结论必须 fail-closed：绝不因为"缺明细"就放行为"没有缺口"。
    assert result["status"] != "fully_satisfied"
    assert result["reason_codes"]


# ---------------------------------------------------------------------------
# 3. C 通信：全失联 vs 冗余退化（**独立阈值**）
# ---------------------------------------------------------------------------


def test_c_full_outage_within_limit_is_managed_gap():
    #: 60 m ÷ 20 m/s = 3.0 s，阈值 3 s ⇒ 恰好不超过 ⇒ managed_gap。
    result = _with_navigation(corridor_gap_assessment=_p15([
        _route(length_m=60.0),
    ]))
    subsystem = _subsystem_entry(result, "C")
    event = subsystem["events"][0]
    assert event["kind"] == "service_outage"
    assert event["length_m"] == pytest.approx(60.0)
    assert event["duration_s"] == pytest.approx(3.0)
    assert event["limit_s"] == pytest.approx(3.0)
    assert event["exceeds_limit"] is False
    assert subsystem["status"] == "acceptable_with_managed_gap"
    assert result["status"] == "acceptable_with_managed_gap"
    assert result["managed_gap_count"] >= 1
    assert any("managed gap" in line for line in result["disclosure_lines"])


def test_c_full_outage_over_limit_is_unacceptable():
    result = _with_navigation(corridor_gap_assessment=_p15([_route(length_m=200.0)]))
    subsystem = _subsystem_entry(result, "C")
    event = subsystem["events"][0]
    assert event["duration_s"] == pytest.approx(10.0)
    assert event["exceeds_limit"] is True
    assert subsystem["status"] == "unacceptable"
    assert result["status"] == "unacceptable"
    assert "service_outage_exceeds_limit" in result["reason_codes"]


def test_c_redundancy_degradation_uses_an_independent_limit():
    #: 160 m ÷ 20 = 8 s，同时产生 service_outage 与 redundancy_degradation 两个事件：
    #: 全失联阈值保持默认 3 s（8 s 超限 ⇒ unacceptable），冗余退化阈值显式改为 30 s
    #: （8 s 未超限 ⇒ managed_gap）。两者**绝不共用同一个数**。
    result = _with_navigation(
        corridor_gap_assessment=_p15([_route(length_m=160.0, gap_causes=["service_deficit", "redundancy_deficit"])]),
        planning_evidence=_registry(
            _assumption("rtk_availability", "available"),
            _assumption("gnss_availability", "available"),
            _assumption("c_redundancy_degradation_max_s", 30.0),
        ),
    )
    subsystem = _subsystem_entry(result, "C")
    by_kind = {event["kind"]: event for event in subsystem["events"]}
    assert by_kind["service_outage"]["limit_s"] == pytest.approx(3.0)
    assert by_kind["service_outage"]["duration_s"] == pytest.approx(8.0)
    assert by_kind["service_outage"]["exceeds_limit"] is True
    assert by_kind["redundancy_degradation"]["limit_s"] == pytest.approx(30.0)
    assert by_kind["redundancy_degradation"]["exceeds_limit"] is False
    assert subsystem["status"] == "unacceptable"


def test_c_limits_are_explicit_evidence_and_report_the_authority():
    result = _with_navigation(planning_evidence=_registry(
        _assumption("rtk_availability", "available"),
        _assumption("gnss_availability", "available"),
        _assumption("c_full_outage_max_s", 9.0),
    ))
    assert result["service_acceptability_limits"]["C"]["service_outage"] == pytest.approx(9.0)
    parameter = result["parameters"]["c_full_outage_max_s"]
    assert parameter["value"] == pytest.approx(9.0)
    assert parameter["authority"] == "explicit_evidence"
    assert parameter["source_type"] == "engineering_assumption"

    defaulted = _evaluate()
    builtin = defaulted["parameters"]["c_full_outage_max_s"]
    assert builtin["value"] == pytest.approx(3.0)
    assert builtin["authority"] == "builtin_engineering_assumption"
    assert builtin["source_type"] == "internal_baseline"
    #: 内置基线的依据必须写明是 FC30 failsafe 触发**事实**，不是法规阈值。
    assert "failsafe" in builtin["source"]
    assert defaulted["service_acceptability_limits"]["C"]["service_outage"] == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# 4. N 导航：RTK → GNSS 状态机（禁止"断 X 秒即失败"）
# ---------------------------------------------------------------------------


def _navigation_result(*, rtk=None, gnss=None, accuracy=None, containment=None):
    items = []
    if rtk is not None:
        items.append(_assumption("rtk_availability", rtk))
    if gnss is not None:
        items.append(_assumption("gnss_availability", gnss))
    if accuracy is not None:
        items.append(_assumption("gnss_accuracy_along_route_m", accuracy))
    if containment is not None:
        items.append(_assumption("navigation_route_containment_accuracy_m", containment))
    return _evaluate(
        corridor_gap_assessment=_p15([_route(length_m=0.0)]),
        planning_evidence=_registry(*items),
    )


def test_navigation_rtk_available_is_nominal():
    result = _navigation_result(rtk="available", gnss="available")
    assert _subsystem_entry(result, "N")["status"] == "nominal"


def test_navigation_rtk_lost_with_adequate_gnss_is_acceptable_degraded():
    result = _navigation_result(
        rtk="not_available", gnss="available", accuracy=8.0, containment=10.0,
    )
    subsystem = _subsystem_entry(result, "N")
    assert subsystem["status"] == "acceptable_degraded"
    assert subsystem["navigation_state"] == "acceptable_degraded"
    event = subsystem["events"][0]
    assert event["kind"] == "navigation_degradation"
    assert event["exceeds_limit"] is False
    assert event["limit_s"] is None
    assert "not_fixed_duration_threshold" in event["semantics"]
    #: 外部研究参考只登记、不作硬门。
    reference = subsystem["rtk_recovery_external_reference"]
    assert reference["role"] == "external_reference_only_not_a_gate"
    assert "9–13 s" in reference["reference"]


def test_navigation_rtk_lost_with_insufficient_gnss_accuracy_is_unacceptable():
    result = _navigation_result(
        rtk="not_available", gnss="available", accuracy=25.0, containment=10.0,
    )
    subsystem = _subsystem_entry(result, "N")
    assert subsystem["status"] == "unacceptable"
    assert subsystem["events"][0]["exceeds_limit"] is True
    assert "navigation_gnss_unavailable" in result["reason_codes"]


def test_navigation_gnss_unavailable_is_unacceptable():
    result = _navigation_result(rtk="not_available", gnss="not_available")
    subsystem = _subsystem_entry(result, "N")
    assert subsystem["status"] == "unacceptable"
    assert any("ATTI" in reason or "GNSS" in reason for reason in subsystem["reasons"])


def test_navigation_missing_evidence_is_unknown_and_fail_closed():
    result = _navigation_result()
    assert _subsystem_entry(result, "N")["status"] == "unknown"
    assert result["status"] == "unknown"
    assert "navigation_evidence_missing" in result["reason_codes"]


def test_navigation_rtk_loss_from_route_service_evidence_is_structural():
    #: 航路内**显式** N:rtk_augmentation 的已确认缺口 ⇒ RTK lost（结构证据，不是时长阈值）。
    p15 = _p15([{
        "route_id": "R1", "route_length_m": 10000.0, "status": "failed",
        "subsystems": [
            _subsystem("C", causes=["service_deficit"], length_m=0.0),
            _subsystem(
                "N", causes=["service_deficit"], length_m=500.0,
                service_redundancy=[{
                    "service_key": "N:rtk_augmentation", "subsystem": "N",
                    "status": "confirmed_deficit",
                    "status_counts": {"satisfied": 9, "confirmed_deficit": 1, "unknown": 0},
                    "voxel_count": 10,
                }],
            ),
            _subsystem("S", causes=["service_deficit"], length_m=0.0),
        ],
    }])
    result = _with_navigation(
        corridor_gap_assessment=p15,
        planning_evidence=_registry(
            _assumption("gnss_availability", "available"),
            _assumption("gnss_accuracy_along_route_m", 5.0),
            _assumption("navigation_route_containment_accuracy_m", 10.0),
        ),
    )
    subsystem = _subsystem_entry(result, "N")
    assert subsystem["rtk"]["authority"] == "derived_from_route_service_evidence"
    assert subsystem["rtk"]["value"] == "not_available"
    assert subsystem["status"] == "acceptable_degraded"


# ---------------------------------------------------------------------------
# 5. S 监视：保护走廊（不是只评估航路中心线）+ T_margin
# ---------------------------------------------------------------------------


def test_protection_corridor_uses_explicit_parameters():
    result = _with_navigation(planning_evidence=_registry(
        _assumption("rtk_availability", "available"),
        _assumption("gnss_availability", "available"),
        _assumption("D_safety_m", 50.0),
        _assumption("D_uncertainty_m", 30.0),
        _assumption("T_chain_detect_track_s", 2.0),
        _assumption("T_chain_sensor_to_platform_s", 1.0),
        _assumption("T_chain_platform_processing_s", 1.0),
        _assumption("T_chain_platform_to_aircraft_s", 1.0),
        _assumption("T_chain_aircraft_response_manoeuvre_s", 2.0),
        _assumption("conservative_closing_speed_mps", 40.0),
        _assumption("relative_speed_basis", "conservative"),
    ))
    corridor = _route_result(result)["corridor"]
    assert corridor["T_chain_s"] == pytest.approx(7.0)
    assert corridor["V_relative_mps"] == pytest.approx(40.0)
    assert corridor["relative_speed_basis"] == "conservative"
    assert corridor["D_protection_m"] == pytest.approx(50.0 + 40.0 * 7.0 + 30.0)
    assert corridor["outer_half_width_m"] == pytest.approx(500.0 + 360.0)
    assert corridor["semantics"] == (
        "horizontal_route_protection_corridor_not_route_centerline_only"
    )
    #: 保护走廊必须带真实航路几何（前端据此绘制图层）。
    assert corridor["route_path"] == [[122.0, 30.0], [122.1, 30.1]]


def test_surveillance_tmargin_positive_accepts_but_still_reports_the_corridor():
    #: 该场景**没有** C/S 连续缺口：只验证监视保护走廊自身的验收结论。
    p15 = _p15([_route(length_m=0.0, gap_causes=[], surplus=[{
        "service_key": "S:rid_cooperative", "subsystem": "S", "status": "satisfied",
        "required_distinct_site_count_by_surface": {"sea": 1},
        "distinct_site_count_by_surface": {"sea": 1},
    }])])
    result = _evaluate(
        corridor_gap_assessment=p15,
        device_catalog={"items": [{
            "device_id": "RID-1", "subsystem": "S", "service_key": "S:rid_cooperative",
            "coverage_geometry": {"radius_by_surface": {"sea": 5000.0},
                                  "source": "test declared geometry"},
        }]},
    )
    route = _route_result(result)
    evidence = route["first_detection_evidence"]
    assert evidence["usable"] is True
    assert evidence["first_detection_distance_m"] == pytest.approx(5000.0)
    assert evidence["coverage_status"] == "satisfied"
    acceptance = route["surveillance_acceptance"]
    assert acceptance["status"] == "acceptable"
    assert acceptance["t_available_s"] == pytest.approx(125.0)
    assert acceptance["t_margin_s"] == pytest.approx(115.0)
    corridor_route = next(
        item for item in route["subsystems"] if item["subsystem"] == "S"
        and "acceptance" in item
    )
    assert corridor_route["acceptance"]["status"] == "acceptable"
    assert corridor_route["status"] == "nominal"


def test_surveillance_tmargin_negative_is_unacceptable():
    #: 同样没有 C/S 连续缺口；这里只让"监视来不及介入"成为唯一的问题。
    p15 = _p15([_route(length_m=0.0, gap_causes=[], surplus=[{
        "service_key": "S:rid_cooperative", "subsystem": "S", "status": "satisfied",
        "required_distinct_site_count_by_surface": {"sea": 1},
        "distinct_site_count_by_surface": {"sea": 1},
    }])])
    result = _evaluate(
        corridor_gap_assessment=p15,
        aircraft_profile=_aircraft(detection_range=None),
        device_catalog={"items": [{
            "device_id": "RID-1", "subsystem": "S", "service_key": "S:rid_cooperative",
            "coverage_geometry": {"radius_by_surface": {"sea": 200.0}, "source": "test"},
        }]},
    )
    acceptance = _route_result(result)["surveillance_acceptance"]
    assert acceptance["status"] == "unacceptable"
    assert acceptance["t_margin_s"] == pytest.approx(5.0 - 10.0)
    assert "tmargin_negative" in result["reason_codes"]


def test_detection_range_below_safety_distance_is_not_usable():
    p15 = _p15([_route(length_m=0.0, gap_causes=[], surplus=[{
        "service_key": "S:rid_cooperative", "subsystem": "S", "status": "satisfied",
        "required_distinct_site_count_by_surface": {"sea": 1},
        "distinct_site_count_by_surface": {"sea": 1},
    }])])
    result = _evaluate(
        corridor_gap_assessment=p15,
        aircraft_profile=_aircraft(detection_range=None),
        device_catalog={"items": [{
            "device_id": "RID-1", "subsystem": "S", "service_key": "S:rid_cooperative",
            "coverage_geometry": {"radius_by_surface": {"sea": 100.0}, "source": "test"},
        }]},
        planning_evidence=_registry(
            _assumption("rtk_availability", "available"),
            _assumption("gnss_availability", "available"),
            _assumption("D_safety_m", 200.0),
        ),
    )
    evidence = _route_result(result)["first_detection_evidence"]
    assert evidence["usable"] is False
    assert evidence["coverage_status"] == "not_satisfied"
    assert "安全边界" in evidence["not_usable_reason"]
    assert "detection_range_below_safety_distance" in result["reason_codes"]


def test_surveillance_without_evidence_is_unknown_fail_closed():
    #: 机载没有声明探测距离、也没有任何地面监视 service ⇒ 首次探测距离不可判定。
    result = _evaluate(aircraft_profile=_aircraft(detection_range=None))
    route = _route_result(result)
    assert route["first_detection_evidence"]["usable"] is False
    assert route["surveillance_acceptance"]["status"] == "unknown"
    assert "no_detection_evidence" in result["reason_codes"]


def test_surveillance_corridor_gap_is_measured_on_the_protection_corridor():
    """保护走廊内的覆盖缺口按**纵深**测量，而不是只看航路中心线。"""

    voxels = _voxels(count=10)
    p15 = _p15([_route(length_m=0.0, surplus=[{
        "service_key": "S:rid_cooperative", "subsystem": "S", "status": "confirmed_deficit",
        "required_distinct_site_count_by_surface": {"sea": 1},
        "distinct_site_count_by_surface": {"sea": 0},
    }])])
    result = _with_navigation(
        corridor_assessment=_p14(route_length_m=10500.0, voxels=voxels),
        corridor_gap_assessment=p15,
        planning_evidence=_registry(
            _assumption("rtk_availability", "available"),
            _assumption("gnss_availability", "available"),
        ),
        aircraft_profile=_aircraft(detection_range=200.0),
    )
    route = _route_result(result)
    gap = route["events_by_kind"]["surveillance_detection_gap"]
    assert len(gap) == 1
    #: 10 个采样点、间距 1000 m、半对角线 500 m ⇒ 未覆盖区间 500–10500 m，长度 10000 m。
    assert gap[0]["start_offset_m"] == pytest.approx(500.0)
    assert gap[0]["end_offset_m"] == pytest.approx(10500.0)
    assert gap[0]["length_m"] == pytest.approx(10000.0)
    assert gap[0]["semantics"] == (
        "protection_corridor_coverage_gap_not_route_centerline_gap"
    )
    corridor_subsystem = next(
        item for item in route["subsystems"] if item["subsystem"] == "S"
        and "acceptance" in item
    )
    assert corridor_subsystem["status"] == "unacceptable"
    assert route["status"] == "unacceptable"


# ---------------------------------------------------------------------------
# 6. 端到端：保存 / 重开 + Step6 门禁
# ---------------------------------------------------------------------------


def _workflow(tmp_path):
    return WorkflowService(tmp_path / "project.json", DEFAULTS)


def _ready_workflow(tmp_path, *, rtk="available", gnss="available", **overrides):
    from test_corridor_site_planner_v2 import configured

    workflow = configured(tmp_path, **overrides)
    for field, value in (("rtk_availability", rtk), ("gnss_availability", gnss)):
        if value is None:
            continue
        workflow.add_planning_evidence({**_assumption(field, value), "scope": "project"})
    workflow.set_cns_continuous_service_policy({
        "service_acceptability_limits": {
            "C": {"service_outage": 8.0, "redundancy_degradation": 20.0},
        },
        "source": "p17 test fixture",
        "confirmed": True,
    })
    #: P16 必须是 current：P18 的前置门禁先检查它。
    workflow.evaluate_cns_corridor_site_plan()
    workflow.evaluate_cns_continuous_service()
    return workflow


def test_p17_evaluate_api_and_snapshot_projection(tmp_path):
    workflow = _ready_workflow(tmp_path)
    result = workflow.state["continuous_service_acceptability"]
    assert result["status"] in ("fully_satisfied", "acceptable_with_managed_gap")

    snapshot = workflow.snapshot()
    projection = snapshot["cns_continuous_service"]
    assert projection["result"]["status"] == result["status"]
    assert projection["step6_gate"]["confirmation_allowed"] is True
    assert projection["operation_scenario"]["intruder_scope"] == "other_uav_only"
    assert projection["operation_scenario"]["cruise_altitude"] == "ALT-100"
    assert projection["fc30"]["aircraft_id"] == FC30_AIRCRAFT_ID
    assert projection["fc30"]["not_a_regulatory_threshold"] is True
    parameters = projection["parameters"]["parameters"]
    assert parameters["c_full_outage_max_s"]["authority"] == "policy_override"


def test_p17_is_strictly_downstream_and_never_stales_upstream(tmp_path):
    workflow = _ready_workflow(tmp_path)
    before = deepcopy({
        key: workflow.state[key] for key in (
            "cns_corridor_assessment", "cns_corridor_gap_assessment",
            "cns_corridor_site_plan", "radar_surveillance_layout",
        )
    })
    workflow.add_planning_evidence({
        **_assumption("D_uncertainty_m", 15.0), "scope": "project",
    })
    assert workflow.state["continuous_service_acceptability"]["status"] == "stale"
    for key, value in before.items():
        assert workflow.state[key] == value


def test_p17_save_and_reopen_preserves_verdict_and_provenance(tmp_path):
    workflow = _ready_workflow(tmp_path)
    before = deepcopy(workflow.state["continuous_service_acceptability"])
    assert before["status"] in ("fully_satisfied", "acceptable_with_managed_gap")

    reopened = WorkflowService(tmp_path / "project.json", DEFAULTS)
    after = reopened.state["continuous_service_acceptability"]
    assert after["status"] == before["status"]
    assert after["input_fingerprint"] == before["input_fingerprint"]
    assert after["disclosure_lines"] == before["disclosure_lines"]
    assert after["reason_codes"] == before["reason_codes"]

    #: 工程假设重开后**仍是假设**（绝不被升级成事实），并逐字保留披露原文。
    evidence = reopened.state["planning_evidence"]["items"]
    rtk = next(item for item in evidence if item["field"] == "rtk_availability")
    assert rtk["source_type"] == "engineering_assumption"
    assert rtk["authority_effect"] == "allowed_with_disclosure"
    assert rtk["report_disclosure"] == "本项为测试工程假设。"
    #: 策略覆盖同样随项目保存。
    assert reopened.state["cns_continuous_service_policy"]["status"] == "configured"

    #: 重开后 P17 结论仍然只让 P17 失效，不反向触碰上游。
    reopened.invalidation_service.workflow("continuous_service_parameters")
    assert reopened.state["cns_corridor_assessment"]["status"] != "stale"


def test_step6_gate_allows_satisfied_and_managed_gap_only(tmp_path):
    workflow = _ready_workflow(tmp_path)
    gate = workflow.cns_continuous_service_step6_gate()
    assert gate["confirmation_allowed"] is True
    assert gate["allowed_statuses"] == ["fully_satisfied", "acceptable_with_managed_gap"]
    review = workflow.initialize_cns_plan_review()["cns_plan_review"]
    assert review["status"] == "current"
    assert review["continuous_service_gate"]["confirmation_allowed"] is True


@pytest.mark.parametrize("status", ["unacceptable", "unknown"])
def test_step6_gate_blocks_unacceptable_and_unknown(tmp_path, status):
    workflow = _ready_workflow(tmp_path)
    workflow.state["continuous_service_acceptability"]["status"] = status
    gate = workflow.cns_continuous_service_step6_gate()
    assert gate["confirmation_allowed"] is False
    with pytest.raises(ValueError, match="P18 需要可接受的 P17 结论"):
        workflow.initialize_cns_plan_review()


def test_step6_gate_blocks_when_p17_was_never_evaluated(tmp_path):
    from test_corridor_site_planner_v2 import configured

    workflow = configured(tmp_path)
    workflow.evaluate_cns_corridor_site_plan()
    assert workflow.state["continuous_service_acceptability"]["status"] == "not_calculated"
    with pytest.raises(ValueError, match="P18 需要可接受的 P17 结论"):
        workflow.initialize_cns_plan_review()


def test_confirmed_plan_carries_the_managed_gap_disclosure(tmp_path):
    from test_corridor_site_planner_v2 import configured

    workflow = configured(tmp_path)
    for field, value in (("rtk_availability", "available"), ("gnss_availability", "available")):
        workflow.add_planning_evidence({**_assumption(field, value), "scope": "project"})
    #: 阈值刚好覆盖该场景的 C 缺口 ⇒ 结论必须是 acceptable_with_managed_gap，
    #: 而不是"全覆盖"。
    workflow.set_cns_continuous_service_policy({
        "service_acceptability_limits": {
            "C": {"service_outage": 8.0, "redundancy_degradation": 20.0},
        },
        "source": "p17 test fixture", "confirmed": True,
    })
    result = workflow.evaluate_cns_continuous_service()["continuous_service_acceptability"]
    assert result["status"] == "acceptable_with_managed_gap"
    assert result["managed_gap_count"] >= 1
    assert result["disclosure_lines"]

    workflow.evaluate_cns_corridor_site_plan()
    review = workflow.initialize_cns_plan_review()["cns_plan_review"]
    assert review["continuous_service_gate"]["requires_managed_gap_disclosure"] is True
    variants = review["variants"]
    projection = variants[0]["evaluation"]["continuous_service_projection"]
    assert projection["evaluated_for_this_variant"] is False
    assert projection["authoritative_status"] == "acceptable_with_managed_gap"


def test_operation_scenario_contract_is_fail_closed():
    from cns_planner.application.continuous_service_service import normalize_operation_scenario

    scenario = normalize_operation_scenario(None)
    assert scenario["single_ownship"] is True
    assert scenario["intruder_scope"] == "other_uav_only"
    assert scenario["cruise_altitude"] == "ALT-100"
    assert scenario["design_intruder_speed_mps"] == 20.0
    assert scenario["nominal_closing_speed_mps"] == 35.0
    assert scenario["conservative_closing_speed_mps"] == 40.0
    assert scenario["intruder_speed_source_type"] == "engineering_assumption"

    with pytest.raises(ValueError):
        normalize_operation_scenario({"intruder_scope": "manned_and_uav"})
    with pytest.raises(ValueError):
        normalize_operation_scenario({"cruise_altitude": "ALT-080"})
    with pytest.raises(ValueError):
        normalize_operation_scenario({"single_ownship": False})


def test_policy_override_normalization_is_fail_closed():
    policy = normalize_continuous_service_policy({
        "service_acceptability_limits": {"C": {"service_outage": 5.0}},
        "parameter_overrides": {"relative_speed_basis": "nominal", "D_safety_m": 10.0},
        "source": "test", "confirmed": True,
    })
    assert policy["status"] == "configured"
    assert policy["service_acceptability_limits"]["C"]["service_outage"] == 5.0
    #: 未覆盖的项保持默认（绝不因为只写了一项就把其他项清零）。
    assert policy["service_acceptability_limits"]["C"]["redundancy_degradation"] == 10.0
    assert policy["parameter_overrides"]["relative_speed_basis"] == "nominal"

    with pytest.raises(ValueError):
        normalize_continuous_service_policy({"parameter_overrides": {"unknown_param": 1.0}})
    with pytest.raises(ValueError):
        normalize_continuous_service_policy({
            "service_acceptability_limits": {"N": {"service_outage": 1.0}},
        })
