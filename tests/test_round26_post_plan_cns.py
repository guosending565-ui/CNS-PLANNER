"""Round 2.6 —— **post-plan CNS 可接受性 / FC30 正式机型** 的定向回归。

本文件守护 Round 2.6 的四条核心业务语义（用户裁定）：

1. **P17 必须评估 P16 方案实施后的 projected state**，而不是当前 ExistingCNS；
   ``baseline`` 与 ``post_plan`` 两层结论都必须保留；
2. **Route Protection 新公式**：``D_protection = D_separation + V_relative × T_chain +
   D_maneuver + D_uncertainty``；``D_separation`` / ``D_uncertainty`` 没有显式工程依据时
   **必须** evidence_required（不得静默取 0）；``D_maneuver`` 是 engineering_baseline 的
   50 m 接口，不是法规值；
3. **监视威胁分层**：合作无人机（RID）是主要威胁、非合作无人机（Radar）是补充威胁；
   两者**分开**判定与披露；Radar 不可行是**能力限制**，不改变主要威胁结论、不阻塞它，
   也不得显示成"监视完全满足"；
4. **Step6 依据【该 variant 实施后的 P17】**；FC30 必须是**正式可选**的 canonical 机型。

这些测试**不做大规模重写**：只覆盖 Round 2.6 新增 / 改变的契约。
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from cns_planner.algorithms.continuous_service.v1 import ContinuousServiceAcceptabilityV1
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.cns_continuous_service import (
    D_MANEUVER_BASELINE_M, NONCOOPERATIVE_LIMITATION_DISCLOSURE,
    ROUTE_PROTECTION_FORMULA, compare_plan_stages, default_continuous_service_policy,
    default_operation_scenario, noncooperative_limitations, protection_distance,
    surveillance_threat_layers,
)
from cns_planner.domain.fc30_profile import FC30_AIRCRAFT_ID, fc30_aircraft_profile
from cns_planner.domain.planning_evidence import resolve_continuous_parameter

DEFAULTS = Path("cns_planner/config/defaults.json")
MODEL = ContinuousServiceAcceptabilityV1()


# ---------------------------------------------------------------------------
# fixtures：复用既有 P17 / P16 合成场景（不复制第二套语义）
# ---------------------------------------------------------------------------


def _evidence_registry(*items):
    from cns_planner.domain.planning_evidence import normalize_planning_evidence_registry

    return normalize_planning_evidence_registry({"items": list(items)})


def _assumption(field, value, *, scope="project"):
    return {
        "field": field, "scope": scope, "value": value,
        "source_type": "engineering_assumption",
        "source": "round2.6 test fixture",
        "statement": f"本测试场景显式声明 {field}。",
        "reason": "使 Round 2.6 判定可进行",
        "report_disclosure": "本项为测试工程假设。",
        "declared_by": "test", "confirmed": True, "confirmed_by_user": True,
        "evidence_id": f"R26-{scope}-{field}",
    }


def _aircraft(*, route_speed=15.0, detection_range=3000.0):
    return {
        "aircraft_id": FC30_AIRCRAFT_ID, "cruise_speed_mps": route_speed,
        "max_speed_mps": 20.0, "max_horizontal_speed_mps": 20.0,
        "surveillance": {
            "confirmed": True, "status": "confirmed",
            "type": {"technology": "adsb", "target_cooperation": "cooperative"},
            "performance": {"min_detection_range_m": detection_range},
            "source": "declared airborne surveillance geometry",
        },
    }


def _p14(*, route_length_m=10000.0, half_width_m=500.0, voxels=None, path=None):
    return {
        "status": "passed", "algorithm_id": "corridor_v1", "input_fingerprint": "p14-fp",
        "routes": [{
            "route_id": "R0005", "route_length_m": route_length_m,
            "corridor_geometry": {
                "route_path": path if path is not None else [[122.0, 30.0], [122.1, 30.1]],
                "horizontal_half_width_m": half_width_m,
            },
            "voxels": voxels if voxels is not None else [],
        }],
    }


def _voxels(*, count=10, spacing=1000.0, surface="sea", covered=False):
    items = []
    for index in range(count):
        entry = {
            "voxel_id": f"V{index}", "surface_class": surface,
            "nearest_route_offset_m": (index + 0.5) * spacing,
            "nearest_route_distance_m": 0.0 if covered else 1e6,
            "cell_half_diagonal_m": 500.0,
            "subsystems": [],
        }
        items.append(entry)
    return items


def _subsystem(code, *, causes, length_m, status="failed", service_key=None,
               surplus=None, segments=None):
    entry = {
        "subsystem": code, "status": status, "service_redundancy": surplus or [],
        "causes": list(causes),
        "continuous_deficit_segments": segments if segments is not None else ([{
            "segment_id": f"SEG-{code}", "route_id": "R0005", "causes": list(causes),
            "start_offset_m": 0.0, "end_offset_m": length_m, "length_m": length_m,
            "voxel_ids": ["V0"],
        }] if length_m else []),
    }
    if service_key:
        entry["service_key"] = service_key
    return entry


def _p15(routes_):
    return {
        "status": "failed", "algorithm_id": "corridor_gap_v1",
        "input_fingerprint": "p15-fp", "routes": routes_,
    }


def _route(*, length_m=0.0, gap_causes=("service_deficit",), surplus=None,
           declared_improvements=None, service_key=None):
    route = {
        "route_id": "R0005", "route_length_m": 10000.0, "status": "failed",
        "subsystems": [
            _subsystem("C", causes=list(gap_causes), length_m=length_m,
                       service_key=service_key, surplus=surplus or []),
            _subsystem("N", causes=[], length_m=0.0),
            _subsystem("S", causes=[], length_m=0.0, surplus=surplus or []),
        ],
        "declared_improvements": declared_improvements or [],
    }
    return route


def _corridor_evidence():
    return (
        _assumption("D_separation_m", 30.0),
        _assumption("D_uncertainty_m", 10.0),
        _assumption("c_full_outage_max_s", 3.0),
        _assumption("rtk_availability", "available"),
        _assumption("gnss_availability", "available"),
    )


def _evaluate(**overrides):
    inputs = {
        "corridor_assessment": _p14(),
        "corridor_gap_assessment": _p15([_route(length_m=60.0)]),
        "site_plan": {},
        "aircraft_profile": _aircraft(),
        "planning_evidence": _evidence_registry(*_corridor_evidence()),
        "operation_scenario": default_operation_scenario(),
        "continuous_service_policy": default_continuous_service_policy(),
        "device_catalog": {},
        "radar_surveillance_layout": {},
        "post_plan_projection": None,
    }
    inputs.update(overrides)
    return MODEL.evaluate(**inputs)


# ---------------------------------------------------------------------------
# 1. Route Protection：四分量公式与 fail-closed
# ---------------------------------------------------------------------------


def test_route_protection_formula_has_four_components():
    #: D_separation + V_relative×T_chain + D_maneuver + D_uncertainty
    assert protection_distance(30.0, 40.0, 10.0, 10.0, 50.0) == pytest.approx(
        30.0 + 400.0 + 50.0 + 10.0
    )
    #: 四分量中任何一个缺失 ⇒ 不可判定（返回 None，调用方必须保持 unknown）。
    assert protection_distance(None, 40.0, 10.0, 10.0, 50.0) is None
    assert protection_distance(30.0, 40.0, None, 10.0, 50.0) is None
    assert protection_distance(30.0, 40.0, 10.0, None, 50.0) is None
    assert protection_distance(30.0, 40.0, 10.0, 10.0, None) is None


def test_d_maneuver_is_an_engineering_baseline_interface():
    """``D_maneuver`` 只提供接口：50 m、身份 engineering_baseline、可被工程依据替换。"""

    baseline = resolve_continuous_parameter(None, "D_maneuver_m")
    assert baseline["value"] == pytest.approx(D_MANEUVER_BASELINE_M) == pytest.approx(50.0)
    assert baseline["authority"] == "builtin_engineering_assumption"
    assert baseline["source_type"] == "internal_baseline"
    #: 绝不称法规值、也绝不说成 FC30 的普遍制动距离事实。
    statement = str(baseline.get("statement") or "")
    assert "engineering_baseline" in statement
    assert "法规" not in statement.replace("不是法规值", "")

    override = resolve_continuous_parameter(
        _evidence_registry(_assumption("D_maneuver_m", 120.0)), "D_maneuver_m",
    )
    assert override["value"] == pytest.approx(120.0)
    assert override["authority"] == "explicit_evidence"


def test_d_separation_and_d_uncertainty_are_evidence_required_not_zero():
    """用户裁定：``D_separation`` / ``D_uncertainty`` 不得长期静默默认 0。"""

    for name in ("D_separation_m", "D_uncertainty_m"):
        resolution = resolve_continuous_parameter(None, name)
        assert resolution["value"] is None, name
        assert resolution["authority"] == "evidence_required", name
        assert resolution["participating"] is False, name

    #: 没有依据 ⇒ 保护走廊不可判定（corridor.status = evidence_required），
    #: 且 S 子系统的监视验收如实保持 unknown（fail-closed）。
    result = _evaluate(planning_evidence=_evidence_registry(
        _assumption("c_full_outage_max_s", 3.0),
        _assumption("rtk_availability", "available"),
        _assumption("gnss_availability", "available"),
    ))
    corridor = result["routes"][0]["corridor"]
    assert corridor["D_protection_m"] is None
    assert corridor["status"] == "evidence_required"
    assert corridor["formula"] == ROUTE_PROTECTION_FORMULA
    assert "protection_distance_evidence_required" in result["reason_codes"]
    assert result["routes"][0]["surveillance_acceptance"]["status"] == "unknown"


def test_c_gap_without_registered_outage_threshold_is_unknown_fail_closed():
    """有真实 C 缺口、但没有用户登记的完全中断阈值 ⇒ 该子系统 unknown（fail-closed）。"""

    result = _evaluate(
        corridor_gap_assessment=_p15([_route(length_m=800.0)]),
        planning_evidence=_evidence_registry(
            _assumption("D_separation_m", 30.0),
            _assumption("D_uncertainty_m", 10.0),
            _assumption("rtk_availability", "available"),
            _assumption("gnss_availability", "available"),
        ),
    )
    assert result["service_acceptability_limits"]["C"]["service_outage"] is None
    c_entry = next(
        item for item in result["routes"][0]["subsystems"] if item["subsystem"] == "C"
    )
    assert c_entry["status"] == "unknown"
    assert "no_outage_threshold_evidence" in result["reason_codes"]
    assert result["status"] == "unknown"


def test_round25_d_safety_legacy_field_still_resolves_the_new_parameter():
    """Round 2.5 旧字段名仍可读（兼容），但新名才是正式参数。"""

    legacy = resolve_continuous_parameter(
        _evidence_registry(_assumption("D_safety_m", 80.0)), "D_separation_m",
    )
    assert legacy["value"] == pytest.approx(80.0)
    assert legacy["read_from_legacy_field"] == "D_safety_m"

    #: 新名有记录时优先，旧名不得覆盖它。
    both = resolve_continuous_parameter(
        _evidence_registry(
            _assumption("D_safety_m", 80.0), _assumption("D_separation_m", 25.0),
        ),
        "D_separation_m",
    )
    assert both["value"] == pytest.approx(25.0)
    assert both["read_from_legacy_field"] is None


# ---------------------------------------------------------------------------
# 2. 监视威胁分层：合作（主要）与非合作（补充）**分开**
# ---------------------------------------------------------------------------


def _surveillance_route(*, rid_status="satisfied", radar_status=None, radar_radius=3000.0):
    surplus = [{
        "service_key": "S:rid_cooperative", "subsystem": "S", "status": rid_status,
        "required_distinct_site_count_by_surface": {"sea": 1},
        "distinct_site_count_by_surface": {
            "sea": 1 if rid_status == "satisfied" else 0,
        },
    }]
    if radar_status is not None:
        surplus.append({
            "service_key": "S:radar_noncooperative", "subsystem": "S", "status": radar_status,
            "required_distinct_site_count_by_surface": {"sea": 1},
            "distinct_site_count_by_surface": {
                "sea": 1 if radar_status == "satisfied" else 0,
            },
        })
    return {
        "subsystem": "S", "status": "failed", "causes": [],
        "continuous_deficit_segments": [], "service_redundancy": surplus,
    }


def _surveillance_device_catalog(*, rid_radius=5000.0, radar_radius=3000.0):
    return {"items": [
        {
            "device_id": "RID-1", "subsystem": "S", "service_key": "S:rid_cooperative",
            "coverage_geometry": {"radius_by_surface": {"sea": rid_radius}, "source": "test"},
        },
        {
            "device_id": "RADAR-1", "subsystem": "S",
            "service_key": "S:radar_noncooperative",
            "coverage_geometry": {"radius_by_surface": {"sea": radar_radius}, "source": "test"},
        },
    ]}


def test_threat_layers_are_separated_cooperative_primary_radar_supplementary():
    """合作（RID/机载协作）与非合作（Radar）必须**分开**成两层，绝不合并。"""

    result = _evaluate(
        corridor_gap_assessment=_p15([{
            "route_id": "R0005", "route_length_m": 10000.0, "status": "failed",
            "subsystems": [
                _subsystem("C", causes=[], length_m=0.0),
                _subsystem("N", causes=[], length_m=0.0),
                _surveillance_route(rid_status="satisfied", radar_status="satisfied"),
            ],
        }]),
        #: 提供真实的 RID / Radar 地面几何（补齐站址冗余），两层才都能判定。
        device_catalog=_surveillance_device_catalog(),
    )
    route = result["routes"][0]
    layers = route["threat_layers"]
    assert set(layers) == {"cooperative", "noncooperative"}
    #: 机载 ADS-B in（`S:airborne_cooperative`）与地面 RID 都属于**合作**分层。
    assert set(layers["cooperative"]["service_keys"]) <= {
        "S:rid_cooperative", "S:airborne_cooperative", "S:surveillance",
    }
    assert layers["cooperative"]["service_keys"], "合作分层必须有 service"
    #: 非合作分层只允许出现 Radar 服务（复合串必须被拆开，绝不整串塞进某一层）。
    assert layers["noncooperative"]["service_keys"] == ["S:radar_noncooperative"]
    #: 两者**绝不合并**成单一 surveillance 结论。
    assert route["primary_threat_layer"] == "cooperative"
    assert route["supplementary_threat_layer"] == "noncooperative"
    assert result["threat_layer_semantics"].startswith("cooperative_rid_primary")
    #: 合作分层（主要威胁）在给定几何下判定为 satisfied。
    assert result["primary_threat_status"] == "satisfied"
    #: 补充分层如实给出**它自己的**结论：本场景的 Radar 探测几何没有构成可用的
    #: 首次探测证据（机载 ADS-B 的 3000 m 被当作更优候选），因此如实保持 unknown，
    #: 绝不因为"合作分层满足"就顺手把非合作分层也算成满足。
    assert result["supplementary_threat_status"] == "unknown"
    assert result["limitations"] == []


def test_supplementary_threat_without_radar_evidence_is_unknown_not_satisfied():
    """没有 Radar 证据时补充威胁如实保持 unknown —— 绝不显示成"已满足"。"""

    result = _evaluate(corridor_gap_assessment=_p15([{
        "route_id": "R0005", "route_length_m": 10000.0, "status": "failed",
        "subsystems": [
            _subsystem("C", causes=[], length_m=0.0),
            _subsystem("N", causes=[], length_m=0.0),
            _surveillance_route(rid_status="satisfied"),
        ],
    }]), device_catalog=_surveillance_device_catalog())
    assert result["primary_threat_status"] in ("nominal", "satisfied")
    assert result["supplementary_threat_status"] == "unknown"
    assert result["limitations"] == []

def test_radar_infeasible_is_a_limitation_and_does_not_block_the_primary_threat():
    """用户裁定：``cooperative 满足 + Radar infeasible`` ⇒ 主要威胁仍 acceptable。"""

    result = _evaluate(
        corridor_gap_assessment=_p15([{
            "route_id": "R0005", "route_length_m": 10000.0, "status": "failed",
            "subsystems": [
                _subsystem("C", causes=[], length_m=0.0),
                _subsystem("N", causes=[], length_m=0.0),
                _surveillance_route(rid_status="satisfied", radar_status="confirmed_deficit"),
            ],
        }]),
        #: 提供真实几何：机载协作监视（合作）与地面 Radar（非合作）。
        device_catalog=_surveillance_device_catalog(),
        #: Round 29-Q：能力限制是 **route-aware** 的 —— 只认该航路自己的 canonical
        #: Radar item（collection 顶层状态不构成任何航路的证明）。
        radar_surveillance_layout={
            "status": "pending_confirmation",
            "items": [{
                "route_id": "R0005", "algorithm_id": "radar_surveillance_layout",
                "algorithm_version": "1.3", "status": "infeasible",
                "gap_reason": "independent_site_count_limited",
                "gap_classification": "confirmed_gap", "managed_physical_gap": True,
                "solver": {"status": "infeasible", "infeasibility_proven": True},
            }],
        },
    )
    route = result["routes"][0]
    assert route["supplementary_threat_status"] == "limitation"
    assert route["supplementary_threat_is_limitation"] is True
    assert result["primary_threat_status"] == "satisfied"
    assert result["supplementary_threat_status"] == "limitation"
    limitations = result["limitations"]
    assert len(limitations) == 1
    assert limitations[0]["limitation_id"] == "noncooperative_surveillance_limitation"
    assert limitations[0]["blocking_primary_threat"] is False
    #: 强制披露原文逐字保留。
    assert NONCOOPERATIVE_LIMITATION_DISCLOSURE in (limitations[0]["disclosure"] or "")
    assert any("能力限制" in line for line in result["disclosure_lines"])
    #: 报告里绝不能出现"监视完全满足"这种说法。
    joined = "\n".join(result["disclosure_lines"])
    assert "监视已完全满足" not in joined
    assert "非合作无人机补充监视能力因 Radar 布局不可行尚未闭合" in joined


def test_radar_not_calculated_is_unknown_not_a_limitation():
    """证据缺失（not_calculated）不是"能力限制"，而是 unknown —— 两者必须分开。"""

    assert noncooperative_limitations({"status": "not_calculated"}) == []
    assert noncooperative_limitations({}) == []
    assert noncooperative_limitations({"status": "failed"})[0]["status"] == "limitation"


def test_surveillance_threat_layers_helper_never_merges():
    layers = surveillance_threat_layers({"subsystems": [
        {"subsystem": "S", "service": "S:rid_cooperative", "status": "nominal",
         "acceptance": {"t_margin_s": 12.0}, "first_detection_evidence": {
             "usable": True, "first_detection_distance_m": 5000.0}},
        {"subsystem": "S", "service": "S:radar_noncooperative", "status": "unknown",
         "acceptance": {"t_margin_s": None}, "first_detection_evidence": {}},
    ]})
    assert layers["cooperative"]["status"] == "nominal"
    assert layers["cooperative"]["t_margin_s"] == pytest.approx(12.0)
    assert layers["noncooperative"]["status"] == "unknown"


# ---------------------------------------------------------------------------
# 3. post-plan 投影：baseline vs post_plan 两层
# ---------------------------------------------------------------------------


def test_post_plan_projection_keeps_baseline_and_projected_layers():
    """给定投影态输入时，结果必须同时保留 baseline 与 post_plan 两层结论。"""

    baseline_gap = _p15([_route(length_m=800.0, gap_causes=("service_deficit",))])
    projected_gap = _p15([_route(length_m=0.0, gap_causes=())])
    projected_gap["input_fingerprint"] = "p15-projected"
    result = _evaluate(
        corridor_gap_assessment=baseline_gap,
        post_plan_projection={
            "available": True,
            "projection_id": "unit-test",
            "applied_action_ids": ["candidate_site:S1:C1"],
            "corridor_assessment": _p14(),
            "corridor_gap_assessment": projected_gap,
            "projection_semantics": "unit_test_projection",
            "persisted_as_upstream": False,
        },
    )
    #: 顶层跟随**投影态**（Step6 判定对象）；baseline 原样保留。
    #: baseline 必须如实是"有缺口"的那一层（这里断言"不是 fully_satisfied"，
    #: 具体是可接受带缺口还是不可接受由阈值决定，两者都属于"有缺口"）。
    assert result["baseline"] is not None
    assert result["baseline"]["status"] != "fully_satisfied"
    assert result["baseline"]["routes"][0]["status"] != "fully_satisfied"
    assert result["baseline_status"] == result["baseline"]["status"]
    assert result["post_plan_status"] == "fully_satisfied"
    assert result["status"] == "fully_satisfied"
    projection = result["post_plan_projection"]
    assert projection["available"] is True
    assert projection["status"] == "fully_satisfied"
    assert projection["persisted_as_upstream"] is False
    assert projection["comparison"]["improved_service_count"] >= 1
    assert projection["comparison"]["remaining_gap_count"] == 0
    assert result["post_plan_projection_semantics"].startswith(
        "post_plan_projection_is_hypothetical"
    )


def test_post_plan_projection_without_projection_reports_baseline_only():
    result = _evaluate(corridor_gap_assessment=_p15([_route(length_m=60.0)]))
    assert result["post_plan_projection"] is None
    assert result["post_plan_status"] is None
    assert result["baseline_status"] == result["status"]
    assert result["baseline"] is None


def test_compare_plan_stages_reports_improvements_and_remaining_gaps():
    baseline = {
        "routes": [{"route_id": "R0005", "subsystems": [
            {"subsystem": "C", "service": "C:communication", "status": "unacceptable",
             "longest_event": {"kind": "service_outage", "length_m": 900.0, "duration_s": 60.0,
                               "limit_s": 3.0, "exceeds_limit": True}},
        ]}],
    }
    #: 改进项必须**可核查**：要么事件在投影态消失，要么 P15 显式声明缓解量。
    projected = {
        "routes": [{"route_id": "R0005", "subsystems": [
            {"subsystem": "C", "service": "C:communication", "status": "acceptable_with_managed_gap",
             "longest_event": {"kind": "service_outage", "length_m": 30.0, "duration_s": 2.0,
                               "limit_s": 3.0, "exceeds_limit": False}},
        ], "declared_improvements": [
            {"subsystem": "C", "declared_max_continuous_deficit_reduction_m": 870.0},
        ]}],
    }
    comparison = compare_plan_stages(baseline, projected, {"applied_action_ids": ["A1"]})
    assert comparison["remaining_gap_count"] == 1
    gap = comparison["remaining_gaps"][0]
    assert gap["improvement_kind"] == "declared_partial_improvement"
    assert gap["declared_improvement_m"] == pytest.approx(870.0)
    assert gap["baseline_length_m"] == pytest.approx(900.0)


# ---------------------------------------------------------------------------
# 4. 端到端：FC30 正式可选 + P17 消费 P16 投影 + Step6 变体门禁
# ---------------------------------------------------------------------------


def _workflow(tmp_path):
    return WorkflowService(tmp_path / "project.json", DEFAULTS)


def _p17_ready_workflow(tmp_path, *, register_outage_threshold=False):
    """把合成场景准备到"P17 可判定"状态（走**正式**入口）。

    Round 2.6：``c_full_outage_max_s`` 没有内置基线（设备 failsafe 的 3 s 不等于规划
    阈值），因此这里默认**不登记**它 —— 用于验证"缺阈值 ⇒ fail-closed"；需要可判定结论
    的场景再显式登记。
    """

    from test_corridor_site_planner_v2 import configured, p17_evidence

    workflow = configured(tmp_path)
    workflow.cns_input_service.ensure_canonical_aircraft_profiles()
    for field, value in (
        ("rtk_availability", "available"), ("gnss_availability", "available"),
        ("D_separation_m", 30.0), ("D_uncertainty_m", 10.0),
    ):
        workflow.add_planning_evidence(p17_evidence(field, value, f"round2.6 {field}"))
    if register_outage_threshold:
        workflow.add_planning_evidence(p17_evidence(
            "c_full_outage_max_s", 30.0,
            "本测试场景由用户显式登记的完全通信中断规划阈值。",
        ))
        workflow.set_cns_continuous_service_policy({
            "service_acceptability_limits": {"C": {"redundancy_degradation": 20.0}},
            "source": "round2.6 fixture", "confirmed": True,
        })
    workflow.evaluate_cns_corridor_site_plan()
    workflow.evaluate_cns_continuous_service()
    return workflow


def test_fc30_is_available_through_the_formal_catalog_and_selection_api(tmp_path):
    workflow = _workflow(tmp_path)
    catalog = workflow.aircraft_profiles_snapshot()
    ids = [item["aircraft_id"] for item in catalog["items"]]
    assert FC30_AIRCRAFT_ID in ids
    #: 幂等：重复补齐不会产生重复条目。
    before = catalog["count"]
    workflow.cns_input_service.ensure_canonical_aircraft_profiles()
    workflow.cns_input_service.ensure_canonical_aircraft_profiles()
    assert workflow.aircraft_profiles_snapshot()["count"] == before
    #: 内置档案与 config/aircraft_profiles.json 的 FC30 条目逐字一致。
    entry = next(
        item for item in workflow.aircraft_profiles_snapshot()["items"]
        if item["aircraft_id"] == FC30_AIRCRAFT_ID
    )
    assert entry == fc30_aircraft_profile()
    assert entry["cruise_speed_mps"] == pytest.approx(15.0)
    assert entry["max_horizontal_speed_mps"] == pytest.approx(20.0)
    assert entry["mtow_kg"] == pytest.approx(95.0)


def test_selecting_fc30_uses_the_formal_api_and_persists(tmp_path):
    workflow = _workflow(tmp_path)
    workflow.select_aircraft_profile(FC30_AIRCRAFT_ID)
    assert workflow.state["selected_aircraft_profile_id"] == FC30_AIRCRAFT_ID
    facts = workflow.fc30_profile_facts()
    assert facts["is_selected"] is True
    #: 设备事实与规划阈值**分开**，且默认没有规划阈值（evidence_required）。
    assert facts["device_failsafe_fact"]["value_s"] == pytest.approx(3.0)
    assert facts["device_failsafe_fact"]["is_planning_threshold"] is False
    assert facts["project_planning_threshold"]["is_planning_threshold"] is True
    assert facts["project_planning_threshold"]["must_be_engineering_assumption"] is True
    assert facts["thresholds_are_separate"] is True
    assert facts["threshold_merge_forbidden"] is True
    #: 重开后仍保持 FC30。
    reopened = WorkflowService(tmp_path / "project.json", DEFAULTS)
    assert reopened.state["selected_aircraft_profile_id"] == FC30_AIRCRAFT_ID


def test_selecting_an_unknown_aircraft_profile_is_refused(tmp_path):
    workflow = _workflow(tmp_path)
    with pytest.raises(ValueError):
        workflow.select_aircraft_profile("NOT-A-REAL-PROFILE")


def test_round26_report_section_carries_the_mandatory_disclosures(tmp_path):
    """报告必须逐字包含 P17 分段、managed gap 与能力限制披露。"""

    from cns_planner.reporting.builder import ReportBuilder
    from cns_planner.reporting.html_renderer import HtmlReportRenderer

    workflow = _p17_ready_workflow(tmp_path, register_outage_threshold=True)
    model = ReportBuilder().build(
        workflow.state, algorithm_catalog=[], generated_at="2026-10-02T00:00:00Z",
    )
    section = model["sections"]["continuous_service_acceptability_p17"]
    assert section["semantics"].startswith("post_plan_projection_is_hypothetical")
    assert section["no_full_coverage_claim"] is True
    assert section["no_surveillance_fully_satisfied_claim"] is True
    assert "thresholds_are_separate" in section["communication_thresholds"]
    #: 报告必须显式登记"设备 failsafe 事实不等于规划阈值"。
    assert (
        section["communication_thresholds"]["device_failsafe_fact_is_not_a_planning_threshold"]
        is True
    )
    assert section["route_protection"]["formula"].startswith("D_protection = D_separation")
    #: 工程假设必须随报告披露（身份不能被升级成事实）。
    assert section["engineering_assumptions"], "报告必须披露工程假设"
    for item in section["engineering_assumptions"]:
        assert item["source_type"] == "engineering_assumption"

    html = HtmlReportRenderer().render(model)
    assert "P17 连续服务可接受性（含 post-plan 投影态）" in html
    assert "设备 failsafe 事实" in html
    assert "D_maneuver" in html
    assert "主要威胁（合作无人机 / RID）" in html
    assert "补充威胁（非合作无人机 / Radar）" in html


def test_radar_infeasible_limitation_reaches_the_report(tmp_path):
    """Radar 不可行必须在报告里作为**能力限制**披露（不是系统错误）。"""

    result = _evaluate(
        corridor_gap_assessment=_p15([{
            "route_id": "R0005", "route_length_m": 10000.0, "status": "failed",
            "subsystems": [
                _subsystem("C", causes=[], length_m=0.0),
                _subsystem("N", causes=[], length_m=0.0),
                _surveillance_route(rid_status="satisfied", radar_status="confirmed_deficit"),
            ],
        }]),
        device_catalog=_surveillance_device_catalog(),
        radar_surveillance_layout={
            "status": "pending_confirmation",
            "items": [{
                "route_id": "R0005", "algorithm_id": "radar_surveillance_layout",
                "algorithm_version": "1.3", "status": "infeasible",
                "gap_reason": "independent_site_count_limited",
                "gap_classification": "confirmed_gap", "managed_physical_gap": True,
                "solver": {"status": "infeasible", "infeasibility_proven": True},
            }],
        },
    )
    from cns_planner.reporting.builder import _continuous_service_section

    section = _continuous_service_section({"continuous_service_acceptability": result})
    assert section["limitations"], "能力限制必须进入报告分段"
    assert any(
        NONCOOPERATIVE_LIMITATION_DISCLOSURE in (item.get("disclosure") or "")
        for item in section["limitations"]
    )
    assert any("能力限制" in line for line in section["disclosure_lines"])


def test_missing_communication_outage_threshold_blocks_step6_until_registered(tmp_path):
    """用户裁定：设备 failsafe 的 3 s **不**自动成为规划阈值 ⇒ 未登记则 fail-closed。"""

    from test_corridor_site_planner_v2 import configured, p17_evidence

    workflow = _p17_ready_workflow(tmp_path, register_outage_threshold=False)
    #: 合成场景自带的档案不是 FC30；本轮不在这里断言"未选中"，只断言 FC30 未被自动选中。
    assert workflow.state["selected_aircraft_profile_id"] != FC30_AIRCRAFT_ID
    result = workflow.state["continuous_service_acceptability"]
    #: 没有用户确认的完全中断阈值：参数保持 evidence_required，生效阈值不可判定。
    assert result["parameters"]["c_full_outage_max_s"]["authority"] == "evidence_required"
    assert result["parameters"]["c_full_outage_max_s"]["value"] is None
    assert result["service_acceptability_limits"]["C"]["service_outage"] is None
    #: 冗余退化阈值**独立**保留（未与完全中断阈值合并）。
    assert result["service_acceptability_limits"]["C"]["redundancy_degradation"] == pytest.approx(10.0)
    #: 该合成场景本身没有 C 缺口，因此本层没有可拒绝的对象（fully_satisfied）；
    #: "缺阈值导致 unknown" 的语义由 domain 级测试覆盖（有真实 C 缺口时）。
    assert "no_outage_threshold_evidence" not in result["reason_codes"]
    #: 设备事实仍然存在，但**不是**规划阈值。
    facts = workflow.fc30_profile_facts()
    assert facts["device_failsafe_fact"]["value_s"] == pytest.approx(3.0)
    assert facts["device_failsafe_fact"]["is_planning_threshold"] is False
    assert facts["project_planning_threshold"]["evidence_required"] is True
    assert facts["project_planning_threshold"]["value_s"] is None
    assert facts["thresholds_are_separate"] is True
    assert facts["threshold_merge_forbidden"] is True

    #: 用户通过正式入口显式登记之后才可判定。
    workflow.add_planning_evidence(p17_evidence(
        "c_full_outage_max_s", 30.0,
        "本测试场景由用户显式登记的完全通信中断规划阈值。",
    ))
    assert workflow.state["continuous_service_acceptability"]["status"] == "stale"
    after = workflow.evaluate_cns_continuous_service()["continuous_service_acceptability"]
    assert after["parameters"]["c_full_outage_max_s"]["authority"] == "explicit_evidence"
    assert after["parameters"]["c_full_outage_max_s"]["source_type"] == "engineering_assumption"
    assert after["service_acceptability_limits"]["C"]["service_outage"] == pytest.approx(30.0)
    #: 冗余退化阈值**独立**保留（内置 10 s；绝不与完全中断阈值合并成同一个数）。
    assert after["service_acceptability_limits"]["C"]["redundancy_degradation"] == pytest.approx(10.0)
    assert (
        after["service_acceptability_limits"]["C"]["redundancy_degradation"]
        != after["service_acceptability_limits"]["C"]["service_outage"]
    )
    assert workflow.fc30_profile_facts()["thresholds_are_separate"] is True
