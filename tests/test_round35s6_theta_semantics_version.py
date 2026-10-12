"""Round35-S6-A：Theta* 端点锚定 / 转向计量的**显式语义版本化**回归。

归档 ``BUG-MAP-ROUTE-END-001`` 要求：端点虚拟锚定（BUG-ROUTE-006）与转向计量拆分
（BUG-TURN-STAT-001）必须做成显式版本化变更 —— 升 ``algorithm_version``、把版本放进
candidate 输入指纹、旧 candidate/validation 不得继续 current。

本文件锁定 2.0 → 2.1 这一动作，并锁定 S5A2 的 target 锚点 aperture 基准方向修复。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cns_planner.application.layered_route_planner_service import LayeredRoutePlannerService
from cns_planner.domain.layered_route import CANDIDATE_FINGERPRINT_COMPONENTS
from cns_planner.domain.layered_theta_v2 import (
    ALGORITHM_ID as THETA_ALGORITHM_ID,
    ALGORITHM_VERSION as THETA_ALGORITHM_VERSION,
)
from cns_planner.layered_route_planner import theta_star_v2 as theta


#: 2.0 是本次显式版本化之前的静默版本（端点虚拟锚定与转向计量拆分都在它下面落地）。
PRE_S6_ALGORITHM_VERSION = "2.0"


def test_theta_semantics_version_is_explicitly_bumped_to_2_1():
    """版本号必须显式升级，且两处语义常量各自登记引入版本。"""

    assert THETA_ALGORITHM_VERSION == "2.1"
    assert theta.ALGORITHM_VERSION == THETA_ALGORITHM_VERSION
    assert theta.ENDPOINT_ANCHOR_SEMANTICS_VERSION == THETA_ALGORITHM_VERSION
    assert theta.TURN_ACCOUNTING_SEMANTICS_VERSION == THETA_ALGORITHM_VERSION

    endpoint = theta.ENDPOINT_ANCHOR_SEMANTICS
    assert endpoint["semantics_version"] == THETA_ALGORITHM_VERSION
    assert endpoint["introduced_in_algorithm_version"] == THETA_ALGORITHM_VERSION
    assert endpoint["fixed_cell_centre_connector_removed"] is True
    assert endpoint["anchor_candidates"] == "containing_cell_plus_one_ring"

    turns = theta.TURN_ACCOUNTING_SEMANTICS
    assert turns["semantics_version"] == THETA_ALGORITHM_VERSION
    assert turns["introduced_in_algorithm_version"] == THETA_ALGORITHM_VERSION
    assert turns["heading_bin_used_as_the_turn_angle"] is False

    search = theta.SEARCH_SEMANTICS
    assert search["endpoint_anchoring_semantics_version"] == THETA_ALGORITHM_VERSION
    assert search["turn_accounting_semantics_version"] == THETA_ALGORITHM_VERSION


def test_target_aperture_bearing_fix_is_recorded_in_the_semantics():
    """S5A2 的 target aperture 基准方向修复必须写进语义声明，不能再只存在于注释里。"""

    endpoint = theta.ENDPOINT_ANCHOR_SEMANTICS
    assert endpoint["target_aperture_uses_entry_direction"] is True
    assert endpoint["aperture_reference_bearing"] == (
        "flight_direction_bearing_exact_start_to_exact_end_for_both_endpoints"
    )
    assert endpoint["target_aperture_reversed_bearing_defect_fixed_in_version"] == (
        THETA_ALGORITHM_VERSION
    )


def test_objective_weights_and_search_parameters_are_unchanged_by_the_version_bump():
    """版本化**不得**顺手改规划权重、目标函数或搜索参数。"""

    from cns_planner.domain.layered_theta_v2 import (
        DEFAULT_HEADING_BIN_COUNT, DEFAULT_THETA_MIN_DEG,
        USER_DEFINED_BASELINE_WEIGHTS,
    )

    assert USER_DEFINED_BASELINE_WEIGHTS == {"risk": 0.8, "turn": 0.1, "distance": 0.1}
    assert DEFAULT_HEADING_BIN_COUNT == 8
    assert DEFAULT_THETA_MIN_DEG == 5.0
    assert theta.ENDPOINT_ANCHOR_APERTURE_DEG == 90.0
    assert theta.ENDPOINT_TRANSITION_ADMISSIBILITY_DEG == 45.0


def _minimal_fingerprint_inputs():
    return {
        "request": {}, "scenario_route": {}, "grid": {}, "layer_mask": {},
        "grid_risk_v2": {}, "objective_policy": {}, "risk_density_constraint": {},
    }


def test_planner_version_component_carries_the_current_semantics_version():
    """candidate 输入指纹必须携带 ``algorithm_id@algorithm_version``。"""

    assert "planner_version" in CANDIDATE_FINGERPRINT_COMPONENTS
    planner = theta.LayeredRiskAwareThetaStarV2()
    fingerprints = planner.fingerprints(**_minimal_fingerprint_inputs())
    assert fingerprints["components"]["planner_version"] == (
        f"{THETA_ALGORITHM_ID}@{THETA_ALGORITHM_VERSION}"
    )


def test_pre_s6_semantics_version_yields_a_different_candidate_fingerprint(monkeypatch):
    """同一输入下，2.0 与 2.1 必须得到**不同**的 candidate fingerprint。

    这是"旧 candidate 不会被错误视为 current"的结构性保证：currentness 由
    ``candidate_fingerprint`` 比较判定，而版本是该指纹的一个分量。
    """

    planner = theta.LayeredRiskAwareThetaStarV2()
    current = planner.fingerprints(**_minimal_fingerprint_inputs())
    monkeypatch.setattr(theta, "ALGORITHM_VERSION", PRE_S6_ALGORITHM_VERSION)
    legacy = planner.fingerprints(**_minimal_fingerprint_inputs())

    assert legacy["components"]["planner_version"] == (
        f"{THETA_ALGORITHM_ID}@{PRE_S6_ALGORITHM_VERSION}"
    )
    assert legacy["candidate_fingerprint"] != current["candidate_fingerprint"]
    assert legacy["input_fingerprint"] != current["input_fingerprint"]


def test_pre_s6_candidate_is_stale_inputs_changed_not_current():
    """由 2.0 产生、保留在项目里的 candidate 在当前语义下只能是 stale 证据。"""

    from cns_planner.application import layered_route_planner_service as planner_module

    route_id, layer_id = "R-1", "L-100"
    key = planner_module._candidate_key(route_id, layer_id)
    applicability = LayeredRoutePlannerService._applicability(
        {
            "status": "candidate", "route_id": route_id, "altitude_layer_id": layer_id,
            "lane_key": key,
            "candidate_fingerprint": f"{THETA_ALGORITHM_ID}@{PRE_S6_ALGORITHM_VERSION}",
        },
        key,
        f"{THETA_ALGORITHM_ID}@{THETA_ALGORITHM_VERSION}",
    )
    assert applicability == "stale_inputs_changed"

    # 只有指纹再次相等时才是 current —— 即必须用 2.1 重新规划，而不是"看着差不多"放行。
    assert LayeredRoutePlannerService._applicability(
        {
            "status": "candidate", "route_id": route_id, "altitude_layer_id": layer_id,
            "lane_key": key,
            "candidate_fingerprint": f"{THETA_ALGORITHM_ID}@{THETA_ALGORITHM_VERSION}",
        },
        key,
        f"{THETA_ALGORITHM_ID}@{THETA_ALGORITHM_VERSION}",
    ) == "current"


def test_stale_semantics_evidence_is_retained_never_deleted():
    """``_applicability`` 是只读判定：它不删除、不改写历史记录。"""

    from cns_planner.application import layered_route_planner_service as planner_module

    key = planner_module._candidate_key("R-1", "L-100")
    record = {
        "status": "candidate", "route_id": "R-1", "altitude_layer_id": "L-100",
        "lane_key": key,
        "candidate_fingerprint": f"{THETA_ALGORITHM_ID}@{PRE_S6_ALGORITHM_VERSION}",
        "algorithm_id": THETA_ALGORITHM_ID,
        "algorithm_version": PRE_S6_ALGORITHM_VERSION,
        "path": [[122.0, 30.0], [122.001, 30.0]],
    }
    before = {name: value for name, value in record.items()}
    LayeredRoutePlannerService._applicability(record, key, "different")
    assert record == before


@pytest.mark.parametrize(
    "relative_path, constant_name",
    [
        ("cns_planner/web/js/workflow/layered_route_planner.js", "THETA_STAR_V2_ALGORITHM_VERSION"),
    ],
)
def test_frontend_theta_version_matches_the_backend_contract(relative_path, constant_name):
    """前端版本常量必须与后端语义版本一致，否则视图会拒绝显示真实 candidate。"""

    source = (Path(__file__).parents[1] / relative_path).read_text(encoding="utf-8")
    assert f"export const {constant_name}='{THETA_ALGORITHM_VERSION}';" in source
    assert f"{THETA_ALGORITHM_ID}@{PRE_S6_ALGORITHM_VERSION}" not in source
