"""Round 30-C2A / C2A.2 —— Radar Layout → P17 首次探测距离证据接通（planning proxy）。

C2A 解决的是 P17 的 ``no_detection_evidence``（Radar layout 可行 ⇒ 3 000 m 首次探测
距离证据，``no_detection_evidence`` 消失）。C2A.2 解决的是它引入的**语义混淆**：

    "Radar layout **几何**验证通过" != "Radar **service** satisfied"。

因此代理产出物被拆成两件互不替代的事实：

* ``detection_distance_usable = true`` / ``detection_distance_status =
  "planning_proxy_validated"`` —— **只**说明"该距离可以进入 detection / T_margin 计算"；
* ``service_coverage_status`` —— 只来自 P15 ``service_redundancy`` 里
  ``S:radar_noncooperative`` **自己**的覆盖结论（绝不用复合 ``S`` subsystem 结论）。

轮次目标**不**包括让 P17 整体变绿：``service_outage_exceeds_limit`` 允许继续存在，
P17 仍可为 ``unacceptable``。

C2A.3 收口最后一处 false reason：``_surveillance_protection_probe`` 的
``probe is None`` 有两种**互不相同**的语义 ——

* 没有可用采样事实 ⇒ **证据不足**（fail-closed，仍登记 ``no_detection_evidence``，
  并给出"保护走廊逐体元覆盖证据缺失"的具体文本）；
* 采样事实存在且全部已覆盖 ⇒ **no_gap / fully_covered**（``probe_reason = None``，
  **绝不**登记 ``no_detection_evidence``）。

同时把 P17 算法版本由 ``2.1`` 升级为 ``2.2``（Radar 规划期探测代理 + 双语义 +
补充威胁逐 service 判定）；``algorithm_id`` / ``schema_version`` 不变，旧 2.1 结果由
既有 ``cns_planner.application.result_currentness`` 版本比对链判为 effective stale。

语义边界（硬约束）
------------------

* 代理是"**规划阶段首次探测距离代理**"：``source_type =
  planning_proxy_not_measured_geometry``、``authority = radar_layout_planning_proxy``、
  ``engineering_confirmed = false``。它不是实测探测概率，也不是工程确认性能；
* 代理自己的 ``coverage_status`` 是 ``planning_proxy_validated``，**绝不再用**普通
  ``satisfied``；
* Radar detection proxy 可用 + P15 Radar ``confirmed_deficit`` ⇒ 补充威胁如实
  ``unacceptable``；只有 P15 Radar 覆盖已满足时才允许 ``satisfied``；
* 数值只来自 ``RADAR_GEOMETRY_PARAMETERS[selected panel radar_type]
  ["max_slant_range_m"]``（绝不在 P17 里写雷达几何魔法常量）；
* 不改 Radar 3 km、不改 90°、不启用 Radar-II、不降低冗余要求、不改
  ``device_catalog``、不伪造 hemisphere/sphere Radar、不改 RID / Communication 语义、
  不改通用 ``requirements_met`` 判定；
* 门控不满足 ⇒ ``_radar_layout_detection_proxy`` 返回 ``None``，
  P17 继续如实保持 ``no_detection_evidence``（缺失 ≠ 0、unknown ≠ pass）。
"""

from __future__ import annotations

from copy import deepcopy
import subprocess
from pathlib import Path

import pytest

from cns_planner.algorithms.continuous_service.v1 import (
    ContinuousServiceAcceptabilityV1, PROTECTION_PROBE_EVIDENCE_REQUIRED_REASON,
    _first_detection_evidence, _radar_layout_detection_proxy,
    _surveillance_protection_probe,
)
from cns_planner.application.continuous_service_service import ContinuousServiceService
from cns_planner.application.plan_projection import PlanProjectionBuilder
from cns_planner.application.result_currentness import (
    RESULT_ALGORITHM_SEMANTICS, apply_algorithm_semantics_stale,
)
from cns_planner.domain.cns_continuous_service import (
    CONTINUOUS_SERVICE_ALGORITHM_ID, CONTINUOUS_SERVICE_ALGORITHM_VERSION,
    CONTINUOUS_SERVICE_REASONS, CONTINUOUS_SERVICE_SCHEMA_VERSION,
    default_continuous_service_policy, default_operation_scenario,
)
from cns_planner.domain.fc30_profile import FC30_AIRCRAFT_ID
from cns_planner.domain.radar_surveillance_layout import (
    RADAR_GEOMETRY_PARAMETERS, RADAR_ORIENTATION_FIRST_POLICY, RADAR_TYPE_I,
    RADAR_TYPE_II, REQUIRED_DISTINCT_SITE_COUNT,
)
from test_round26_post_plan_cns import (
    _aircraft, _assumption, _evidence_registry, _p14, _p15, _subsystem,
    _surveillance_device_catalog, _surveillance_route, _voxels,
)

MODEL = ContinuousServiceAcceptabilityV1()
V1_SOURCE = Path("cns_planner/algorithms/continuous_service/v1.py")
ROUTE_ID = "R0005"
RADAR_I_RADIUS_M = RADAR_GEOMETRY_PARAMETERS[RADAR_TYPE_I]["max_slant_range_m"]

#: R0005 的 7 块 Radar-I 面板（全部 ``radar_i``；本轮**不启用** Radar-II）。
R0005_SELECTED_PANELS = [
    {
        "panel_id": f"R0005-T{index // 2:02d}-A{(index % 2) * 90:03d}",
        "tower_id": f"T{index // 2:02d}",
        "radar_type": RADAR_TYPE_I,
        "azimuth_deg": (index % 2) * 90.0,
        "panel_half_width_deg": 45.0,
        "altitude_layer_id": "ALT-080",
    }
    for index in range(7)
]


# ---------------------------------------------------------------------------
# fixtures：Radar layout item（只读消费的当前权威 artifact）
# ---------------------------------------------------------------------------


def _r0005_layout_item(**overrides):
    """R0005 canonical item：feasible + validated + 无缺口 + 无 managed gap。"""

    item = {
        "route_id": ROUTE_ID,
        "algorithm_id": "radar_surveillance_layout",
        "algorithm_version": "1.3",
        "status": "proposal_ready",
        "altitude_layer_id": "ALT-080",
        "selected_panels": deepcopy(R0005_SELECTED_PANELS),
        "selected_panel_count": len(R0005_SELECTED_PANELS),
        "radar_i_panel_count": len(R0005_SELECTED_PANELS),
        "radar_ii_panel_count": 0,
        "coverage_summary": {
            "validation_sample_spacing_m": 25.0,
            "satisfied_sample_count": 400,
            "under_redundant_sample_count": 0,
            "uncovered_sample_count": 0,
            "unknown_sample_count": 0,
            "validated": True,
            "validation_fingerprint": "radarvalidation-test",
        },
        "gap_reason": None,
        "gap_classification": "none",
        "managed_physical_gap": False,
        "radar_gap": {
            "available": True,
            "gap_reason": None,
            "gap_classification": "none",
            "managed_physical_gap": False,
            "source": "radar_layout_algorithm_solve_layout_canonical_fields",
        },
        "solver": {
            "name": "scipy.optimize.milp", "library": "HiGHS",
            "stage": "radar_i_only", "status": "optimal",
            "optimality_proven": True, "infeasibility_proven": False,
        },
        "allowed_radar_types": [RADAR_TYPE_I],
    }
    item.update(overrides)
    return item


def _layout(*items, status="pending_confirmation"):
    return {
        "status": status,
        "collection_id": "radar_surveillance_layout",
        "algorithm_id": "radar_surveillance_layout",
        "algorithm_version": "1.3",
        "items": list(items),
    }


def _r0005_layout(**overrides):
    return _layout(_r0005_layout_item(**overrides))


def _surveillance_gap_route(
    *, rid_status="not_satisfied", radar_status="confirmed_deficit",
    radar_status_counts=None,
):
    """S 子系统带 RID（合作）/ Radar（非合作）站址冗余结论的 P15 航路。

    ``radar_status`` 是 Radar 服务在 P15 ``service_redundancy`` 里**自己**的覆盖结论：

    * ``"confirmed_deficit"``（默认）—— 真实项目形态：Radar detection proxy 可用，
      但 P15 里 ``S:radar_noncooperative`` 仍是已确认缺口（真实项目 191/948）；
    * ``"satisfied"`` —— 只有此时补充威胁才允许 nominal/satisfied；
    * ``None`` —— P15 里根本没有 Radar 结论（缺失 ≠ 满足）。

    ``rid_status="not_satisfied"`` 是刻意的：让保护走廊探测能构造出**已覆盖/未覆盖**
    的样本事实（P17 在"探测事实完全缺失"时会登记另一条 ``no_detection_evidence``，
    那与本轮 Radar 首次探测距离代理无关）。本场景要隔离的正是后者。
    """

    subsystem = _surveillance_route(rid_status=rid_status, radar_status=radar_status)
    if radar_status_counts is not None:
        for item in subsystem.get("service_redundancy") or []:
            if str(item.get("service_key") or "") == "S:radar_noncooperative":
                item["status_counts"] = dict(radar_status_counts)

    return {
        "route_id": ROUTE_ID, "route_length_m": 10000.0, "status": "failed",
        "subsystems": [
            _subsystem("C", causes=["service_deficit"], length_m=60.0, service_key="C:communication"),
            _subsystem("N", causes=[], length_m=0.0, service_key="N:navigation"),
            subsystem,
        ],
    }


def _evaluate(
    *, rid_status="not_satisfied", radar_status="confirmed_deficit",
    radar_status_counts=None, **overrides,
):
    """P17 评估：默认**没有任何**机载 / 设备目录 / RID 声明几何。

    因此"接通 Radar 代理之前"，该场景如实保持 ``no_detection_evidence``。
    """

    inputs = {
        #: ``detection_range=None`` ⇒ 机载协作监视不提供候选（缺值 ⇒ 不列候选）。
        #: corridor 必须带体元事实，否则 P14 无法构造保护走廊探测（那是**另一个**
        #: ``no_detection_evidence`` 来源，与本轮 Radar 代理无关）。
        "corridor_assessment": _p14(
            voxels=_voxels(count=10, spacing=1000.0, surface="sea")
        ),
        "corridor_gap_assessment": _p15([
            _surveillance_gap_route(
                rid_status=rid_status, radar_status=radar_status,
                radar_status_counts=radar_status_counts,
            )
        ]),
        "site_plan": {},
        "aircraft_profile": _aircraft(detection_range=None),
        "planning_evidence": _evidence_registry(
            _assumption("D_separation_m", 30.0),
            _assumption("D_uncertainty_m", 10.0),
            _assumption("c_full_outage_max_s", 3.0),
            _assumption("rtk_availability", "available"),
            _assumption("gnss_availability", "available"),
        ),
        "operation_scenario": default_operation_scenario(),
        "continuous_service_policy": default_continuous_service_policy(),
        "device_catalog": {},
        "radar_surveillance_layout": {},
        "post_plan_projection": None,
    }
    inputs.update(overrides)
    return MODEL.evaluate(**inputs)


def _route_result(result, route_id=ROUTE_ID):
    return next(
        item for item in result["routes"] if str(item.get("route_id")) == route_id
    )


def _baseline_route(result, route_id=ROUTE_ID):
    #: 没有投影时顶层结果**就是** baseline；有投影时 baseline 在 ``result["baseline"]``。
    baseline = result.get("baseline") or result
    return _route_result(baseline, route_id)


def _detection(result, route_id=ROUTE_ID):
    return _route_result(result, route_id)["first_detection_evidence"]


# ---------------------------------------------------------------------------
# 0. 前提：没有 Radar 证据时确实是 no_detection_evidence（缺失 ≠ 0）
# ---------------------------------------------------------------------------


def test_without_layout_p17_keeps_no_detection_evidence_and_outage_limit():
    result = _evaluate()
    route = _baseline_route(result)
    detection = route["first_detection_evidence"]
    assert detection["first_detection_distance_m"] is None
    assert detection["usable"] is False
    assert "no_detection_evidence" in route["reason_codes"]
    #: 本轮**不**消除的既有缺陷：C 服务中断仍超限。
    assert "service_outage_exceeds_limit" in route["reason_codes"]
    assert route["status"] == "unacceptable"


# ---------------------------------------------------------------------------
# 1. R0005 7-panel Radar-I、feasible + validated + gap none ⇒ 3000 m
# ---------------------------------------------------------------------------


def test_r0005_feasible_validated_layout_yields_3000m_proxy_and_removes_no_detection():
    result = _evaluate(radar_surveillance_layout=_r0005_layout())
    route = _baseline_route(result)
    detection = route["first_detection_evidence"]

    assert detection["first_detection_distance_m"] == pytest.approx(float(RADAR_I_RADIUS_M))
    assert detection["first_detection_distance_m"] == pytest.approx(3000.0)
    assert detection["service_key"] == "S:radar_noncooperative"
    assert detection["source_type"] == "planning_proxy_not_measured_geometry"
    #: 证据权威如实指向 Radar 规划代理，绝不伪装成已声明几何。
    assert detection["authority"] == "radar_layout_planning_proxy"
    assert detection["semantics"].startswith(
        "planning_stage_first_detection_distance_proxy"
    )
    assert detection["usable"] is True
    #: Round 30-C2A.2：**双语义拆分**。代理自己的 ``coverage_status`` 不得再是普通
    #: ``satisfied``（那等于宣称 "Radar service 已满足"）。
    assert detection["coverage_status"] == "planning_proxy_validated"
    assert detection["coverage_status"] != "satisfied"
    #: 距离可用性单独披露：只说明"该距离可以进入 detection / T_margin 计算"。
    assert detection["detection_distance_usable"] is True
    assert detection["detection_distance_status"] == "planning_proxy_validated"
    assert detection["usable_semantics"].startswith("detection_distance_usable_means")
    #: 服务覆盖结论只来自 P15 该 service 自己的结论，不由代理代声明。
    assert detection["service_coverage_status"] == "confirmed_deficit"
    assert detection["service_coverage_service_key"] == "S:radar_noncooperative"

    proxy = next(
        item for item in detection["candidates"]
        if item.get("source_type") == "planning_proxy_not_measured_geometry"
    )
    assert proxy["authority"] == "radar_layout_planning_proxy"
    assert proxy["engineering_confirmed"] is False
    assert proxy["coverage_status"] == "planning_proxy_validated"
    assert proxy["coverage_status"] != "satisfied"
    assert proxy["detection_distance_usable"] is True
    assert proxy["detection_distance_status"] == "planning_proxy_validated"
    assert proxy["service_coverage_status"] == "not_declared_by_layout_proxy"
    assert proxy["route_id"] == ROUTE_ID
    assert proxy["altitude_layer_id"] == "ALT-080"
    assert proxy["selected_panel_count"] == 7
    assert proxy["radar_types"] == [RADAR_TYPE_I]
    assert proxy["radius_m"] == pytest.approx(3000.0)
    assert proxy["participates_in_site_redundancy"] is False

    #: 目标：no_detection_evidence 消失。
    assert "no_detection_evidence" not in route["reason_codes"]
    assert CONTINUOUS_SERVICE_REASONS["no_detection_evidence"] not in route["reasons"]
    #: 目标之外：service_outage_exceeds_limit **仍然存在**（本轮不解决）。
    assert "service_outage_exceeds_limit" in route["reason_codes"]
    assert route["status"] == "unacceptable"


# ---------------------------------------------------------------------------
# 2–6. 门控：任何一条不满足 ⇒ 代理不可用
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"status": "stale"}, id="stale"),
        pytest.param({"status": "infeasible"}, id="infeasible"),
        pytest.param(
            {"coverage_summary": {
                "validated": False, "uncovered_sample_count": 0,
                "under_redundant_sample_count": 0,
            }},
            id="coverage_summary_not_validated",
        ),
    ],
)
def test_proxy_gate_returns_none_for_currentness_and_feasibility(overrides):
    layout = _r0005_layout(**overrides)
    assert _radar_layout_detection_proxy(layout, ROUTE_ID) is None


def test_stale_layout_is_not_a_proxy():
    assert _radar_layout_detection_proxy(_r0005_layout(status="stale"), ROUTE_ID) is None
    #: not_calculated 同样不可用。
    assert _radar_layout_detection_proxy(
        _r0005_layout(status="not_calculated"), ROUTE_ID
    ) is None
    result = _evaluate(radar_surveillance_layout=_r0005_layout(status="stale"))
    route = _baseline_route(result)
    assert "no_detection_evidence" in route["reason_codes"]


def test_infeasible_layout_is_not_a_proxy():
    layout = _r0005_layout(
        status="infeasible", selected_panels=[], selected_panel_count=0,
        gap_reason="independent_site_count_limited",
        gap_classification="confirmed_gap", managed_physical_gap=True,
        radar_gap={
            "available": True, "gap_classification": "confirmed_gap",
            "managed_physical_gap": True,
        },
    )
    assert _radar_layout_detection_proxy(layout, ROUTE_ID) is None
    result = _evaluate(radar_surveillance_layout=layout)
    route = _baseline_route(result)
    assert "no_detection_evidence" in route["reason_codes"]


@pytest.mark.parametrize("classification", ["confirmed_gap", "suspected_gap", None])
def test_gap_classification_not_none_is_not_a_proxy(classification):
    layout = _r0005_layout(
        gap_classification=classification,
        radar_gap={
            "available": True, "gap_classification": classification,
            "managed_physical_gap": False,
        },
    )
    assert _radar_layout_detection_proxy(layout, ROUTE_ID) is None
    result = _evaluate(radar_surveillance_layout=layout)
    assert "no_detection_evidence" in _baseline_route(result)["reason_codes"]


def test_managed_physical_gap_flag_is_not_a_proxy():
    layout = _r0005_layout(
        managed_physical_gap=True,
        radar_gap={
            "available": True, "gap_classification": "none",
            "managed_physical_gap": True,
        },
    )
    assert _radar_layout_detection_proxy(layout, ROUTE_ID) is None


@pytest.mark.parametrize(
    "coverage",
    [
        pytest.param({"validated": False, "uncovered_sample_count": 0,
                      "under_redundant_sample_count": 0}, id="not_validated"),
        pytest.param({"validated": True, "uncovered_sample_count": 1,
                      "under_redundant_sample_count": 0}, id="uncovered"),
        pytest.param({"validated": True, "uncovered_sample_count": 0,
                      "under_redundant_sample_count": 2}, id="under_redundant"),
        pytest.param({"validated": True, "uncovered_sample_count": 3,
                      "under_redundant_sample_count": 4}, id="both"),
        pytest.param(None, id="missing_coverage_summary"),
    ],
)
def test_coverage_summary_gate_is_fail_closed(coverage):
    layout = _r0005_layout(coverage_summary=coverage)
    assert _radar_layout_detection_proxy(layout, ROUTE_ID) is None
    result = _evaluate(radar_surveillance_layout=layout)
    assert "no_detection_evidence" in _baseline_route(result)["reason_codes"]


def test_empty_selected_panels_is_not_a_proxy():
    layout = _r0005_layout(selected_panels=[], selected_panel_count=0)
    assert _radar_layout_detection_proxy(layout, ROUTE_ID) is None
    result = _evaluate(radar_surveillance_layout=layout)
    assert "no_detection_evidence" in _baseline_route(result)["reason_codes"]


def test_externalized_selected_panels_falls_back_to_canonical_evidence_inputs():
    """``selected_panels`` 被持久化外置（如实为空）时，回退到同一求解的几何输入快照。

    真实项目实测：``radar.layout.detail`` scope 把 ``items[].selected_panels`` 外置成
    artifact，state 内只剩 ``service_evidence_inputs.selected_panels``（同一求解的
    canonical 几何输入）。代理必须仍能拿到面板型号事实，否则 P17 会一直缺证据。
    """

    item = _r0005_layout_item(selected_panels=[], selected_panel_count=7)
    item["service_evidence_inputs"] = {
        "selected_panels": deepcopy(R0005_SELECTED_PANELS),
        "semantics": "canonical_radar_geometry_inputs_for_hypothetical_re_evaluation",
    }
    proxy = _radar_layout_detection_proxy(_layout(item), ROUTE_ID)
    assert proxy is not None
    assert proxy["radius_m"] == pytest.approx(3000.0)
    assert proxy["selected_panel_count"] == 7
    assert proxy["radar_types"] == [RADAR_TYPE_I]
    assert proxy["selected_panels_source"] == "service_evidence_inputs.selected_panels"
    #: 未外置时来源仍是 canonical ``selected_panels``。
    direct = _radar_layout_detection_proxy(_r0005_layout(), ROUTE_ID)
    assert direct["selected_panels_source"] == "selected_panels"


def test_both_panel_facts_empty_is_not_a_proxy():
    item = _r0005_layout_item(selected_panels=[], selected_panel_count=0)
    item["service_evidence_inputs"] = {"selected_panels": []}
    assert _radar_layout_detection_proxy(_layout(item), ROUTE_ID) is None
    item["service_evidence_inputs"] = {"selected_panels": None}
    assert _radar_layout_detection_proxy(_layout(item), ROUTE_ID) is None


def test_unknown_panel_radar_type_is_rejected_wholesale():
    """面板型号无法解析 ⇒ **整体拒绝**，绝不部分取值凑一个距离。"""

    panels = deepcopy(R0005_SELECTED_PANELS)
    panels.append({**panels[0], "panel_id": "X", "radar_type": "radar_ix"})
    layout = _r0005_layout(selected_panels=panels)
    assert _radar_layout_detection_proxy(layout, ROUTE_ID) is None


def test_route_id_is_required_and_matched_exactly():
    layout = _r0005_layout()
    assert _radar_layout_detection_proxy(layout, "") is None
    assert _radar_layout_detection_proxy(layout, "R0003") is None
    assert _radar_layout_detection_proxy({}, ROUTE_ID) is None
    assert _radar_layout_detection_proxy(None, ROUTE_ID) is None


# ---------------------------------------------------------------------------
# 7. engineering_confirmed = false（代理绝不冒充工程确认性能）
# ---------------------------------------------------------------------------


def test_proxy_is_never_engineering_confirmed():
    proxy = _radar_layout_detection_proxy(_r0005_layout(), ROUTE_ID)
    assert proxy is not None
    assert proxy["engineering_confirmed"] is False
    assert proxy["source_type"] == "planning_proxy_not_measured_geometry"
    assert proxy["authority"] == "radar_layout_planning_proxy"
    assert proxy["semantics"].startswith("planning_stage_first_detection_distance_proxy")
    assert proxy["geometry_source"] == (
        "radar_surveillance_layout_selected_panel_geometry_preset"
    )
    #: 数值出处逐型号可追溯，且确实是固定几何参数里的 max_slant_range_m。
    assert proxy["radar_type_geometry"][RADAR_TYPE_I]["max_slant_range_m"] == 3000.0
    assert proxy["radar_type_geometry"][RADAR_TYPE_I]["confirmed"] is True


# ---------------------------------------------------------------------------
# 8. Radar-II 未启用
# ---------------------------------------------------------------------------


def test_radar_ii_is_still_disabled_in_policy():
    policy = RADAR_ORIENTATION_FIRST_POLICY
    assert policy["allowed_radar_types"] == [RADAR_TYPE_I]
    assert policy["allow_automatic_radar_ii_escalation"] is False
    assert policy["allow_range_relaxation"] is False
    assert policy["gap_after_proven_infeasibility"] is True


def test_selected_layout_uses_radar_i_only():
    item = _r0005_layout_item()
    assert item["allowed_radar_types"] == [RADAR_TYPE_I]
    assert item["radar_ii_panel_count"] == 0
    assert {panel["radar_type"] for panel in item["selected_panels"]} == {RADAR_TYPE_I}
    proxy = _radar_layout_detection_proxy(_layout(item), ROUTE_ID)
    assert proxy["radar_types"] == [RADAR_TYPE_I]
    #: 真实项目实测：Radar-I 3 km 未被改动。
    assert RADAR_GEOMETRY_PARAMETERS[RADAR_TYPE_I]["max_slant_range_m"] == 3000.0
    assert proxy["radius_m"] == 3000.0


def test_proxy_does_not_invent_radar_geometry_for_unselected_types():
    proxy = _radar_layout_detection_proxy(_r0005_layout(), ROUTE_ID)
    assert RADAR_TYPE_II not in proxy["radar_types"]
    assert RADAR_TYPE_II not in proxy["radar_type_geometry"]
    assert proxy["radius_m"] != RADAR_GEOMETRY_PARAMETERS[RADAR_TYPE_II]["max_slant_range_m"]


# ---------------------------------------------------------------------------
# 9. 通用 requirements_met 代码未改变
# ---------------------------------------------------------------------------


def _function_source(source, name):
    """用 AST 精确抽出某个顶层函数的源码段（含装饰器之外的全部函数体）。"""

    import ast

    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node)
    raise AssertionError(f"源码中找不到函数 {name}")


def _git_head_source(path):
    """取 ``HEAD:<path>`` 的原始文本（git 不可用时跳过比对）。"""

    head = subprocess.run(
        ["git", "show", f"HEAD:{path}"],
        capture_output=True, text=True, check=False,
        encoding="utf-8", errors="replace",
    )
    if head.returncode != 0:
        pytest.skip("git 不可用，无法比对 HEAD 版本")
    return head.stdout


def _source_block(source, start_marker, end_marker):
    """按起止锚点切出一段源码（end_marker 本身不含在结果内）。"""

    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


def test_generic_requirements_met_judgement_is_untouched():
    """通用 ``requirements_met`` / 声明几何候选行为：逐行**原样保留**。

    Round 30-C2A / C2A.2 只允许三类改动：

    1. 在候选列表**末尾追加** Radar 规划代理候选（新增行）；
    2. 候选**距离可用性**判定由内联 ``coverage_status == "satisfied"`` 改为
       ``_detection_distance_usable``（普通候选的返回值与原来逐项一致）；
    3. ``first_detection_evidence`` 的 ``authority`` / ``semantics`` 与新增披露块
       如实取自被选中的候选（这是**证据出处披露**，不是 requirements_met 判定）。

    三类都不得触碰"逐 service 读取 P15 ``service_redundancy`` → 计算
    ``requirements_met`` / ``coverage_status``"的循环，也不得触碰声明几何的辅助函数
    （RID / ``device_catalog`` / 机载协作监视）。因此这里直接断言这些源码段在当前
    版本中**逐字符存在**。
    """

    head = _git_head_source("cns_planner/algorithms/continuous_service/v1.py")
    current_source = V1_SOURCE.read_text(encoding="utf-8")

    #: ① 逐 service 的 ``requirements_met`` / ``coverage_status`` 判定循环：逐字未改。
    requirements_block = _source_block(
        head,
        '    for subsystem in gap_route.get("subsystems") or []:\n'
        '        if subsystem.get("subsystem") != "S":',
        "    airborne_detection = _airborne_detection_range(corridor_route)",
    )
    assert '"requirements_met": requirements_met,' in requirements_block
    assert '"coverage_status": (' in requirements_block
    assert '"satisfied" if requirements_met and serviced_surfaces' in requirements_block
    assert requirements_block in current_source

    #: ② 声明几何辅助函数（RID / ``device_catalog`` / 机载协作监视）：逐字未改。
    for name in (
        "_service_geometry", "_airborne_detection_range", "_aircraft_detection_range",
    ):
        assert _function_source(head, name) in current_source, (
            f"声明几何辅助函数 {name} 不得被本轮修改"
        )


def test_proxy_candidate_does_not_declare_site_redundancy_requirements():
    proxy = _radar_layout_detection_proxy(_r0005_layout(), ROUTE_ID)
    assert proxy["required_distinct_site_count_by_surface"] == {}
    assert proxy["distinct_site_count_by_surface"] == {}
    assert proxy["serviced_surfaces"] == []
    assert proxy["participates_in_site_redundancy"] is False
    #: 站址冗余结论仍来自 P15 的 service_redundancy，不被代理改写。
    result = _evaluate(radar_surveillance_layout=_r0005_layout())
    route = _baseline_route(result)
    radar_subsystem = next(
        item for item in route["subsystems"] if item.get("service") == "S:radar_noncooperative"
    )
    assert "no_detection_evidence" not in route["reason_codes"]
    assert radar_subsystem is not None


# ---------------------------------------------------------------------------
# 10. RID detection evidence 行为不变
# ---------------------------------------------------------------------------


def test_rid_declared_geometry_still_wins_and_evidence_is_unchanged():
    result = _evaluate(
        rid_status="satisfied",
        device_catalog=_surveillance_device_catalog(rid_radius=5000.0),
        radar_surveillance_layout=_r0005_layout(),
    )
    detection = _baseline_route(result)["first_detection_evidence"]
    #: RID 声明几何（5000 m）仍是**更远**的可用候选：首次探测距离不被 Radar 代理替代。
    assert detection["first_detection_distance_m"] == pytest.approx(5000.0)
    assert detection["geometry_source"] == "test"
    assert detection["source_type"] == "declared_geometry"
    #: RID 候选的原有行为逐项不变（站址冗余满足 ⇒ 普通 satisfied）。
    rid_candidate = next(
        item for item in detection["candidates"]
        if item.get("service_key") == "S:rid_cooperative"
    )
    assert rid_candidate["coverage_status"] == "satisfied"
    assert rid_candidate["requirements_met"] is True
    assert rid_candidate["radius_m"] == pytest.approx(5000.0)
    #: 被选中证据不是 Radar 代理 ⇒ 不披露"距离可用"（那是代理自己的语义）。
    assert "detection_distance_usable" not in detection
    #: 代理仍在候选列表里如实披露，但没有被选中。
    assert any(
        item.get("source_type") == "planning_proxy_not_measured_geometry"
        for item in detection["candidates"]
    )


def test_rid_only_scenario_is_unaffected_when_no_radar_layout():
    without = _evaluate(
        rid_status="satisfied",
        device_catalog=_surveillance_device_catalog(rid_radius=5000.0),
    )
    with_layout = _evaluate(
        rid_status="satisfied",
        device_catalog=_surveillance_device_catalog(rid_radius=5000.0),
        radar_surveillance_layout=_r0005_layout(),
    )
    assert (
        _baseline_route(without)["first_detection_evidence"]["first_detection_distance_m"]
        == _baseline_route(with_layout)["first_detection_evidence"]["first_detection_distance_m"]
        == pytest.approx(5000.0)
    )
    #: RID 声明几何优先：候选排序与所选证据不因接通 Radar 而改变。
    assert _baseline_route(with_layout)["first_detection_evidence"]["service_key"] == (
        "S:rid_cooperative"
    )


def test_proxy_is_supplementary_only_and_does_not_relax_cooperative_layer():
    result = _evaluate(radar_surveillance_layout=_r0005_layout())
    route = _baseline_route(result)
    assert route["primary_threat_layer"] == "cooperative"
    assert route["supplementary_threat_layer"] == "noncooperative"
    #: 合作层（主要威胁）在本场景没有可用站址冗余 ⇒ 如实 unknown，绝不被 Radar 代理"带绿"。
    assert route["primary_threat_status"] == "unknown"
    #: 非合作层：本 fixture 的 P15 里 Radar 服务仍是已确认缺口 ⇒ 即使 Radar layout
    #: 几何代理可用，补充威胁也**不得** nominal/satisfied。
    assert route["supplementary_threat_status"] == "unacceptable"
    assert route["supplementary_threat_status"] != "satisfied"
    #: 能力限制（Radar 不可行）不得被伪造。
    assert route["limitations"] == []
    #: 分层结论旁边如实并列披露两件互不替代的事实。
    layer = route["threat_layers"]["noncooperative"]
    assert layer["detection_distance_usable"] is True
    assert layer["service_coverage_status"] == "confirmed_deficit"


# ---------------------------------------------------------------------------
# 10b. 逐 service 判定：P15 Radar 自己的结论决定补充威胁（绝不用复合 S 结论）
# ---------------------------------------------------------------------------


def test_radar_proxy_usable_but_p15_confirmed_deficit_keeps_supplementary_unacceptable():
    """本轮核心拆分：探测距离可用（geometry validated）≠ Radar service satisfied。"""

    result = _evaluate(
        radar_surveillance_layout=_r0005_layout(),
        radar_status="confirmed_deficit",
        radar_status_counts={
            "satisfied": 757, "confirmed_deficit": 191, "unknown": 0,
        },
    )
    route = _baseline_route(result)
    detection = route["first_detection_evidence"]

    #: ① detection / T_margin 照常计算，且不产生 no_detection_evidence。
    assert detection["first_detection_distance_m"] == pytest.approx(3000.0)
    assert detection["usable"] is True
    assert detection["detection_distance_usable"] is True
    assert "no_detection_evidence" not in route["reason_codes"]
    assert route["surveillance_acceptance"]["t_margin_s"] == pytest.approx(
        64.25, abs=0.05
    )

    #: ② 服务覆盖如实保持"已确认缺口"（来自 P15 里该 service **自己**的结论）。
    assert detection["service_coverage_status"] == "confirmed_deficit"
    assert detection["service_coverage_service_key"] == "S:radar_noncooperative"
    assert detection["service_coverage_basis"] == (
        "p15_service_redundancy_per_service_status"
    )
    assert detection["service_coverage_status_counts"]["confirmed_deficit"] == 191
    assert detection["service_coverage_status_counts"]["satisfied"] == 757

    #: ③ 补充威胁不得 nominal/satisfied，如实 unacceptable。
    assert route["supplementary_threat_status"] == "unacceptable"
    layer = route["threat_layers"]["noncooperative"]
    assert layer["detection_distance_usable"] is True
    assert layer["service_coverage_status"] == "confirmed_deficit"

    #: ④ 不为新增 Radar reason 改写全局 reason vocabulary。
    assert route["reason_codes"] == ["service_outage_exceeds_limit"]
    assert route["status"] == "unacceptable"


def test_radar_proxy_plus_satisfied_p15_service_gives_satisfied_supplementary():
    """只有 proxy 可用**且** P15 Radar 覆盖已满足时，补充威胁才允许 satisfied。"""

    result = _evaluate(
        radar_surveillance_layout=_r0005_layout(), radar_status="satisfied",
    )
    route = _baseline_route(result)
    detection = route["first_detection_evidence"]
    assert detection["first_detection_distance_m"] == pytest.approx(3000.0)
    assert detection["detection_distance_usable"] is True
    assert detection["service_coverage_status"] == "satisfied"
    #: 代理自己的 status 依旧只是 planning proxy 语义值，绝不冒充 ``satisfied``。
    assert detection["coverage_status"] == "planning_proxy_validated"
    assert route["supplementary_threat_status"] == "satisfied"
    #: Round 30-C2A.3：P15 全覆盖 ⇒ 保护走廊探针为空，但那是 **no_gap /
    #: fully_covered**，不是"缺探测证据" ⇒ 绝不登记 no_detection_evidence。
    assert "no_detection_evidence" not in route["reason_codes"]
    assert CONTINUOUS_SERVICE_REASONS["no_detection_evidence"] not in route["reasons"]


@pytest.mark.parametrize("radar_status", ["unknown", None])
def test_radar_proxy_with_unproven_p15_service_is_never_satisfied(radar_status):
    """P15 的 Radar 结论为 unknown 或缺失时：缺失 ≠ 满足，补充威胁不得 satisfied。"""

    result = _evaluate(
        radar_surveillance_layout=_r0005_layout(), radar_status=radar_status,
    )
    route = _baseline_route(result)
    detection = route["first_detection_evidence"]
    assert detection["detection_distance_usable"] is True
    assert detection["service_coverage_status"] == "unknown"
    assert route["supplementary_threat_status"] == "unknown"
    assert route["supplementary_threat_status"] != "satisfied"
    assert "no_detection_evidence" not in route["reason_codes"]


def test_rid_primary_threat_still_unacceptable_and_radar_does_not_replace_it():
    """需求 7/8：RID（主要威胁）仍如实 unacceptable；Radar 代理绝不替代 RID。"""

    result = _evaluate(
        rid_status="satisfied",
        radar_status=None,
        #: 100 m > D_separation(30 m) ⇒ 距离可用；但 (100-30)/40 - 10 < 0 ⇒
        #: 探测-响应余量为负，RID（合作层）如实 unacceptable。
        device_catalog=_surveillance_device_catalog(rid_radius=100.0),
    )
    route = _baseline_route(result)
    detection = route["first_detection_evidence"]
    #: RID 声明几何（100 m）可用，但 T_margin 为负 ⇒ 主要威胁如实 unacceptable。
    assert detection["first_detection_distance_m"] == pytest.approx(100.0)
    assert detection["service_key"] == "S:rid_cooperative"
    assert detection["source_type"] == "declared_geometry"
    assert route["primary_threat_status"] == "unacceptable"
    #: 没有 Radar 证据时补充威胁保持 unknown：Radar 不替 RID 背书，反之亦然。
    assert route["supplementary_threat_status"] == "unknown"
    #: Radar 规划代理未被选中 ⇒ 也不产生"距离可用"披露（绝不凭空声明）。
    assert "detection_distance_usable" not in detection


def test_radar_geometry_orientation_and_redundancy_parameters_are_unchanged():
    """需求 12：3 km / 90° / 站址冗余要求不得被本轮改动。"""

    parameters = RADAR_GEOMETRY_PARAMETERS[RADAR_TYPE_I]
    assert parameters["max_slant_range_m"] == 3000.0
    assert parameters["min_slant_range_m"] == 120.0
    #: 单面阵水平波束 90°（半宽 45°）。
    assert parameters["azimuth_beamwidth_deg"] == 90.0
    assert parameters["azimuth_half_width_deg"] == 45.0
    #: 逐 surface 的独立站址要求未放宽。
    assert REQUIRED_DISTINCT_SITE_COUNT == {
        "land": 2, "sea": 1, "coastal_uncertain": 2, "unknown": None,
    }
    assert RADAR_ORIENTATION_FIRST_POLICY["allow_range_relaxation"] is False
    assert RADAR_ORIENTATION_FIRST_POLICY["allow_automatic_radar_ii_escalation"] is False


# ---------------------------------------------------------------------------
# 11. baseline 与 post-plan 都能消费同一份 Radar 规划 artifact
# ---------------------------------------------------------------------------


class _StubSession:
    def __init__(self, state):
        self.state = state

    def save(self):
        raise AssertionError("P17 投影绝不保存项目")


class _StubCorridorModel:
    def evaluate(self, **kwargs):
        return {"status": "passed", "routes": []}


class _StubProjection:
    """最小投影构建器 stub：只提供 P17 需要的键。"""

    def __init__(self, projection):
        self._projection = projection

    def p17_projection_input(self, actions, **kwargs):
        return deepcopy(self._projection)


def _algorithm_identity(result_key):
    """某个权威产物的**当前**算法语义身份（避免只读投影误标 stale）。"""

    from cns_planner.application.result_currentness import current_algorithm_identity

    algorithm_id, algorithm_version = current_algorithm_identity(result_key)
    return {"algorithm_id": algorithm_id, "algorithm_version": algorithm_version}


def _projection_payload(*, rid_status="not_satisfied"):
    return {
        "available": True,
        "projection_id": "p16_selected_actions",
        "applied_action_ids": ["ACT-RADAR-PANEL-1"],
        "original_action_ids": ["ACT-RADAR-PANEL-1"],
        "skipped_action_ids": [],
        "corridor_assessment": _p14(
            voxels=_voxels(count=10, spacing=1000.0, surface="sea")
        ),
        "corridor_gap_assessment": _p15([
            _surveillance_gap_route(rid_status=rid_status)
        ]),
        "projection_semantics": "post_plan_projection_hypothetical_state_never_written_into_existing_cns",
        "persisted_as_upstream": False,
        "written_into_existing_cns": False,
        "radar_surveillance_layout": _r0005_layout(),
    }


def _service_with_projection(projection):
    #: 权威 P14/P15 必须带**当前**算法身份，否则只读投影会如实标 stale，
    #: P17 随即 fail-closed（那是 correct 行为，但不是本测试要验证的对象）。
    corridor = _p14(voxels=_voxels(count=10, spacing=1000.0, surface="sea"))
    corridor.update(_algorithm_identity("cns_corridor_assessment"))
    gap = _p15([_surveillance_gap_route()])
    gap.update(_algorithm_identity("cns_corridor_gap_assessment"))
    state = {
        "cns_continuous_service_policy": default_continuous_service_policy(),
        "operation_scenario": default_operation_scenario(),
        "planning_evidence": _evidence_registry(
            _assumption("D_separation_m", 30.0),
            _assumption("D_uncertainty_m", 10.0),
            _assumption("c_full_outage_max_s", 3.0),
            _assumption("rtk_availability", "available"),
            _assumption("gnss_availability", "available"),
        ),
        #: 选定机型（P17 的速度基线来源）：catalog 形状，缺失 ⇒ no_route_speed。
        "aircraft_profiles": {
            "items": [_aircraft(detection_range=None)], "count": 1,
        },
        "selected_aircraft_profile_id": FC30_AIRCRAFT_ID,
        "device_catalog": {},
        #: baseline 层的 P14/P15（权威容器）。
        "cns_corridor_assessment": corridor,
        "cns_corridor_gap_assessment": gap,
        #: P16 当前权威方案：至少一个 selected_action，投影层才会被构建。
        "cns_corridor_site_plan": {
            "status": "proposed",
            "selected_actions": [{
                "action_id": "ACT-RADAR-PANEL-1",
                "planner_family": "directional_radar",
                "route_id": ROUTE_ID,
                "status": "proposed",
            }],
        },
        #: 关键：权威 state 里**没有** Radar layout ⇒ baseline 层必须保持
        #: no_detection_evidence；只有投影层带入的 layout 才能让 post-plan 层拿到证据。
        "radar_surveillance_layout": {},
    }
    service = ContinuousServiceService(
        _StubSession(state), MODEL, invalidation=None,
        plan_projection=_StubProjection(projection),
    )
    return service


def test_post_plan_input_explicitly_carries_authoritative_radar_layout():
    projection = _projection_payload()
    service = _service_with_projection(projection)
    inputs = service.build_evaluation_inputs()
    assert (
        inputs["post_plan_projection"]["radar_surveillance_layout"]
        == projection["radar_surveillance_layout"]
    )
    #: baseline 输入与 post-plan 输入共享同一份权威 Radar 规划 artifact。
    assert inputs["radar_surveillance_layout"] == {}
    assert inputs["post_plan_projection"]["persisted_as_upstream"] is False


def test_both_baseline_and_post_plan_consume_the_same_radar_layout():
    service = _service_with_projection(_projection_payload())
    inputs = service.build_evaluation_inputs()
    result = MODEL.evaluate(**inputs)

    baseline = _baseline_route(result)
    post_plan = _route_result(result)

    #: baseline 没有 layout ⇒ 如实保持 no_detection_evidence（不制造结果）。
    assert "no_detection_evidence" in baseline["reason_codes"]
    assert baseline["first_detection_evidence"]["first_detection_distance_m"] is None

    #: post-plan 显式带入同一份 layout ⇒ 拿到 3000 m 代理证据，no_detection_evidence 消失。
    assert post_plan["first_detection_evidence"]["first_detection_distance_m"] == pytest.approx(
        3000.0
    )
    assert (
        post_plan["first_detection_evidence"]["source_type"]
        == "planning_proxy_not_measured_geometry"
    )
    assert "no_detection_evidence" not in post_plan["reason_codes"]
    #: service_outage_exceeds_limit 允许继续存在（本轮不解决）。
    assert "service_outage_exceeds_limit" in post_plan["reason_codes"]
    assert result["post_plan_status"] == "unacceptable"


def test_plan_projection_builder_injects_authoritative_layout():
    """真实 ``PlanProjectionBuilder.p17_projection_input`` 必须显式携带权威 layout。

    ``project()`` 本体（设施重算）不在本测试范围内，只替换它以避免复制第二套 P14/P15
    合成场景；被验证的是本轮新增的注入契约本身。
    """

    layout = _r0005_layout()
    session = _StubSession({"radar_surveillance_layout": layout})
    builder = PlanProjectionBuilder(session, _StubCorridorModel(), _StubCorridorModel())
    builder.project = lambda actions, **kwargs: {
        "available": True,
        "projection_id": kwargs.get("projection_id", "p16_selected_actions"),
        "corridor_assessment": _p14(
            voxels=_voxels(count=10, spacing=1000.0, surface="sea")
        ),
        "corridor_gap_assessment": _p15([_surveillance_gap_route()]),
        "projection_semantics": (
            "post_plan_projection_hypothetical_state_never_written_into_existing_cns"
        ),
        "persisted_as_upstream": False,
        "written_into_existing_cns": False,
        "applied_action_ids": ["ACT-1"],
    }
    actions = [{"action_id": "ACT-1"}]
    p17_input = builder.p17_projection_input(actions)
    assert p17_input["radar_surveillance_layout"] == layout
    #: 投影仍然绝不写入 ExistingCNS（白名单只保留评估键，不引入任何写入语义）。
    assert p17_input["persisted_as_upstream"] is False
    assert "written_into_existing_cns" not in p17_input
    assert builder.project(actions)["written_into_existing_cns"] is False


def test_plan_projection_project_does_not_write_existing_cns():
    """投影态契约不变：``project()`` 的第 1 原则仍是"绝不写入 ExistingCNS"。"""

    layout = _r0005_layout()
    session = _StubSession({
        "radar_surveillance_layout": layout,
        "existing_cns_facilities": {},
    })
    builder = PlanProjectionBuilder(session, _StubCorridorModel(), _StubCorridorModel())
    projection = builder.project([])
    assert projection["available"] is False
    assert projection["unavailable_reason"] == "no_selected_actions"
    assert projection["persisted_as_upstream"] is False
    assert session.state["existing_cns_facilities"] == {}
    assert session.state["radar_surveillance_layout"] == layout


def test_first_detection_evidence_consumes_layout_argument_directly():
    """``_first_detection_evidence`` 真的消费 ``radar_layout`` 参数（不是被忽略）。"""

    corridor_route = _p14()["routes"][0]
    gap_route = _surveillance_gap_route()
    without, reason_without = _first_detection_evidence(
        corridor_route=corridor_route, gap_route=gap_route,
        device_catalog={}, radar_layout={}, aircraft_profile=_aircraft(detection_range=None),
    )
    assert without["usable"] is False
    assert reason_without is not None

    with_layout, reason_with_layout = _first_detection_evidence(
        corridor_route=corridor_route, gap_route=gap_route,
        device_catalog={}, radar_layout=_r0005_layout(),
        aircraft_profile=_aircraft(detection_range=None),
    )
    assert with_layout["usable"] is True
    assert with_layout["first_detection_distance_m"] == pytest.approx(3000.0)
    assert reason_with_layout is None


# ---------------------------------------------------------------------------
# 12. C2A.3 收口：probe=None 两种语义分离 + 算法版本 2.2
# ---------------------------------------------------------------------------


def test_probe_without_sample_facts_is_evidence_required():
    """① 没有可用采样事实 ⇒ probe=None 且带**明确**缺证据 reason（fail-closed）。"""

    probe, reason = _surveillance_protection_probe(
        _p14(voxels=[])["routes"][0], _surveillance_gap_route(),
        service_key="S:radar_noncooperative",
    )
    assert probe is None
    assert reason == PROTECTION_PROBE_EVIDENCE_REQUIRED_REASON
    assert "证据缺失" in reason


def test_probe_fully_covered_is_no_gap_not_evidence_required():
    """② 采样事实存在且无 uncovered ⇒ probe=None 且 reason 为空（no_gap / fully_covered）。"""

    probe, reason = _surveillance_protection_probe(
        _p14(voxels=_voxels(count=10, spacing=1000.0, surface="sea"))["routes"][0],
        _surveillance_gap_route(radar_status="satisfied"),
        service_key="S:radar_noncooperative",
    )
    assert probe is None
    assert reason is None
    #: 反例对照：同一条走廊但 P15 Radar 未满足 ⇒ 有未覆盖体元，probe 非空。
    gap_probe, gap_reason = _surveillance_protection_probe(
        _p14(voxels=_voxels(count=10, spacing=1000.0, surface="sea"))["routes"][0],
        _surveillance_gap_route(radar_status="confirmed_deficit"),
        service_key="S:radar_noncooperative",
    )
    assert gap_probe is not None
    assert gap_probe["length_m"] > 0
    assert gap_reason is None


def test_missing_sample_facts_still_register_no_detection_evidence():
    """缺采样事实仍如实登记缺证据（不因为 C2A.3 而放宽 fail-closed）。"""

    result = _evaluate(
        corridor_assessment=_p14(voxels=[]),
        radar_surveillance_layout=_r0005_layout(),
    )
    route = _baseline_route(result)
    #: 探测距离本身仍然可用（代理只依赖 layout，不依赖体元事实）。
    assert route["first_detection_evidence"]["first_detection_distance_m"] == pytest.approx(
        3000.0
    )
    assert route["first_detection_evidence"]["detection_distance_usable"] is True
    assert "no_detection_evidence" in route["reason_codes"]
    assert PROTECTION_PROBE_EVIDENCE_REQUIRED_REASON in route["reasons"]


def test_fully_covered_corridor_registers_no_detection_evidence():
    """P15 Radar 满足 ⇒ 全覆盖 ⇒ 不得登记 no_detection_evidence（A/B 语义分离）。"""

    result = _evaluate(
        radar_surveillance_layout=_r0005_layout(),
        radar_status="satisfied",
        radar_status_counts={"satisfied": 948, "confirmed_deficit": 0, "unknown": 0},
    )
    route = _baseline_route(result)
    assert route["first_detection_evidence"]["first_detection_distance_m"] == pytest.approx(
        3000.0
    )
    assert route["first_detection_evidence"]["detection_distance_usable"] is True
    assert route["supplementary_threat_status"] == "satisfied"
    assert "no_detection_evidence" not in route["reason_codes"]
    assert CONTINUOUS_SERVICE_REASONS["no_detection_evidence"] not in route["reasons"]
    #: 全覆盖时 S 子系统的 reasons 里不得出现"证据缺失/探测证据"字样。
    surveillance = next(
        item for item in route["subsystems"] if item["subsystem"] == "S"
    )
    assert all("证据缺失" not in reason for reason in surveillance["reasons"])


def test_communication_subsystem_conclusion_is_unchanged():
    """Communication 结论（含 C 事件与 reason）不因 Radar 代理而改变。"""

    without = _baseline_route(_evaluate())
    with_layout = _baseline_route(_evaluate(radar_surveillance_layout=_r0005_layout()))

    def _communication(route):
        return next(item for item in route["subsystems"] if item["subsystem"] == "C")

    assert _communication(without) == _communication(with_layout)
    assert _communication(with_layout)["service"] == "C:communication"
    assert "service_outage_exceeds_limit" in with_layout["reason_codes"]


def test_algorithm_version_is_2_2_and_enters_the_currentness_chain():
    """版本升级到 2.2；``algorithm_id`` / ``schema_version`` 不变。"""

    assert CONTINUOUS_SERVICE_ALGORITHM_VERSION == "2.2"
    assert CONTINUOUS_SERVICE_ALGORITHM_ID == "continuous_service_acceptability_v1"
    assert CONTINUOUS_SERVICE_SCHEMA_VERSION == (
        "round2.6-post-plan-continuous-service-acceptability"
    )
    result = _evaluate(radar_surveillance_layout=_r0005_layout())
    assert result["algorithm_version"] == "2.2"
    assert result["algorithm_id"] == CONTINUOUS_SERVICE_ALGORITHM_ID
    assert result["schema_version"] == CONTINUOUS_SERVICE_SCHEMA_VERSION
    #: 既有 currentness 链登记的当前语义身份 == 2.2（不另造 invalidation 机制）。
    assert RESULT_ALGORITHM_SEMANTICS["continuous_service_acceptability"] == (
        CONTINUOUS_SERVICE_ALGORITHM_ID, "2.2",
    )


def test_old_2_1_p17_result_is_recognized_as_stale():
    """旧 2.1 持久化 P17 结果必须由既有版本比对链判为 effective stale。"""

    algorithm_id, algorithm_version = RESULT_ALGORITHM_SEMANTICS[
        "continuous_service_acceptability"
    ]
    assert algorithm_version == "2.2"

    stored_2_1 = {
        "algorithm_id": algorithm_id,
        "algorithm_version": "2.1",
        "status": "unacceptable",
        "routes": [],
    }
    projected = apply_algorithm_semantics_stale(
        stored_2_1, algorithm_id, algorithm_version,
    )
    assert projected["status"] == "stale"
    assert projected["stale_reason"] == "algorithm_semantics_changed"
    assert projected["algorithm_semantics_stale"]["stored_algorithm_version"] == "2.1"
    assert projected["algorithm_semantics_stale"]["current_algorithm_version"] == "2.2"
    #: 只读投影：绝不改写原对象，也不把 stale 说成 failed / passed。
    assert stored_2_1["status"] == "unacceptable"

    #: 当前 2.2 结果不被判 stale（不制造 staleness）。
    current = dict(stored_2_1, algorithm_version="2.2")
    assert apply_algorithm_semantics_stale(
        current, algorithm_id, algorithm_version,
    )["status"] == "unacceptable"
