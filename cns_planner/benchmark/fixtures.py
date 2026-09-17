"""Synthetic, reproducible route-planning benchmark fixtures.

Every case is pure generated geometry: there is no real-world data, no QGIS layer
and no machine-specific path.  Cases are declared for both planners but each case
carries an explicit ``applicability`` block so a planner that cannot consume the
case reports ``not_applicable`` / ``missing_prerequisite`` instead of being forced
to pass.  Nothing in this package may change a planner to make a case succeed.
"""

from __future__ import annotations

from copy import deepcopy

# Synthetic workspace: a level-7 MH/T block of exactly 0.1 x 0.1 degrees.
# L7 = 12" x 12" cells => 30 x 30 cells, 1.103 km (E-W) x 1.110 km (N-S) per cell.
WORKSPACE_BBOX = [0.0, 0.0, 0.1, 0.1]
WORKSPACE_LEVEL = 7

# Scenario route endpoints (degrees).
CENTER_SOUTH = [0.01, 0.01]
CENTER_NORTH = [0.09, 0.09]
WEST_EAST_START = [0.01, 0.05]
WEST_EAST_END = [0.09, 0.05]

# Reference quality gates.  These are engineering expectations for a known
# synthetic geometry, never a "the planner is good" verdict.
STRAIGHT_LINE_TOLERANCE = 0.02


WORKS_BOTH = "works_both"
INPUT_GUARD = "input_guard"
APPLICATIONS = (WORKS_BOTH, INPUT_GUARD)

APPLICABILITY = {
    WORKS_BOTH: {
        "route_planner_v1": {
            "applicable": True,
            "note": "V1 直接消费 workspace bbox 与硬约束 BBOX。",
        },
        "risk_aware_route_planner_v2": {
            "applicable": True,
            "note": "V2 消费 MH/T grid 邻接图、显式合成 grid_risk 与合成 airspace eligibility。",
        },
    },
    INPUT_GUARD: {
        "route_planner_v1": {
            "applicable": False,
            "expected_status": "rejected_at_input_boundary",
            "note": "非法约束在 Application 输入边界被拒绝，V1 不会被调用。",
        },
        "risk_aware_route_planner_v2": {
            "applicable": False,
            "expected_status": "rejected_at_input_boundary",
            "note": "非法约束在 Application 输入边界被拒绝，V2 不会被调用。",
        },
    },
}


def _case(case_id, description, start, end, application=WORKS_BOTH, *,
          hard_constraints=(), blocked_boxes=(), risk=None, quality_expectation=None,
          v2_parameter_variants=(), expert_question, demonstration=None):
    return {
        "case_id": case_id,
        "description": description,
        "expert_question": expert_question,
        "demonstration": deepcopy(demonstration),
        "application": application,
        "workspace_bbox": list(WORKSPACE_BBOX),
        "workspace_level": WORKSPACE_LEVEL,
        "start": list(start),
        "end": list(end),
        "hard_constraints": [dict(item) for item in hard_constraints],
        "blocked_boxes": [list(item) for item in blocked_boxes],
        "risk": risk,
        "v2_parameter_variants": [
            {"run_id": str(run_id), "parameters": dict(parameters)}
            for run_id, parameters in v2_parameter_variants
        ],
        "quality_expectation": quality_expectation or {},
        "planner_changes_allowed": False,
        "applicability": deepcopy(APPLICABILITY[application]),
    }


def _uniform_risk(low=0.05, high=0.85, band=None):
    """Deterministic synthetic risk: low everywhere, high inside ``band``."""

    return {"low": low, "high": high, "band": list(band) if band else None}


CASES = (
    _case(
        "open_space", "无障碍开阔空域，起终点对角穿越。",
        CENTER_SOUTH, CENTER_NORTH,
        expert_question="开阔空间下应如何定义格网偏置、几何质量与可接受误差？",
        quality_expectation={
            "detour_factor_max": 1.0 + STRAIGHT_LINE_TOLERANCE,
            "turn_count": 0,
            "note": "开阔空域应接近直线；格中心离散仍可能产生折点，不构成优劣结论。",
        },
    ),
    _case(
        "single_obstacle", "单一矩形硬约束位于直线路径中部。",
        WEST_EAST_START, WEST_EAST_END,
        expert_question="单一障碍绕行时，约束几何与路径质量应采用哪些验证指标？",
        hard_constraints=[{"name": "合成单障碍", "bbox": [0.048, 0.044, 0.052, 0.056]}],
        quality_expectation={"detour_factor_min": 1.0, "note": "应绕开障碍，detour_factor 大于 1。"},
    ),
    _case(
        "concave_obstacle", "U 形（凹）硬约束组合，路径必须绕行。",
        WEST_EAST_START, WEST_EAST_END,
        expert_question="凹形约束应使用 polygon、栅格还是混合表达，并如何验证完整包含？",
        hard_constraints=[
            {"name": "合成凹形-北臂", "bbox": [0.045, 0.055, 0.055, 0.08]},
            {"name": "合成凹形-南臂", "bbox": [0.045, 0.02, 0.055, 0.045]},
            {"name": "合成凹形-底", "bbox": [0.045, 0.044, 0.055, 0.056]},
        ],
        quality_expectation={"detour_factor_min": 1.0, "note": "BBOX 组合语义，不代表真实多边形边界。"},
    ),
    _case(
        "narrow_passage", "两道纵向硬约束之间保留窄通道。",
        WEST_EAST_START, WEST_EAST_END,
        expert_question="窄通道可达性应如何处理分辨率、净空与飞行器尺度？",
        hard_constraints=[
            {"name": "合成窄通道-北", "bbox": [0.04, 0.055, 0.06, 0.09]},
            {"name": "合成窄通道-南", "bbox": [0.04, 0.01, 0.06, 0.045]},
        ],
        quality_expectation={"detour_factor_min": 1.0, "note": "通道通行性由格离散决定，不声明米制间隙保证。"},
    ),
    _case(
        "disconnected_allowed_airspace", "合成 allowed airspace 被完全分割为互不连通的两块。",
        CENTER_SOUTH, CENTER_NORTH,
        expert_question="不连通 allowed airspace 的不可达证据应如何表达与验证？",
        blocked_boxes=[[0.0, 0.035, 0.1, 0.065]],
        quality_expectation={
            "expect_v2_failed": True,
            "note": "allowed 区域不连通；V2 必须返回 failed，不得回退到旧 A*。",
        },
    ),
    _case(
        "risk_tradeoff", "工作区中段的高相对风险带；V2 以两个显式 λ 观测“绕行 vs 风险暴露”的取舍，V1 不读取风险。",
        WEST_EAST_START, WEST_EAST_END,
        expert_question="距离与风险应采用加权和、约束、分层还是 Pareto 表达？",
        risk=_uniform_risk(0.05, 0.9, band=[0.0, 0.045, 1.0, 0.055]),
        v2_parameter_variants=(
            ("risk_aware_route_planner_v2_lambda_0", {"risk_weight_lambda": 0.0, "risk_component": "overall"}),
            ("risk_aware_route_planner_v2_lambda_8", {"risk_weight_lambda": 8.0, "risk_component": "overall"}),
        ),
        quality_expectation={
            "note": "仅并列报告长度/绕行/风险暴露与 λ 效应；不作优劣结论，也不自动选择 λ。",
        },
    ),
    _case(
        "endpoint_near_boundary", "终点贴近工作区东北边界格。",
        [0.005, 0.005], [0.0995, 0.0995],
        expert_question="端点贴边时，搜索空间边界和端点连接的数值语义应如何定义？",
        quality_expectation={"note": "半开边界映射；不得越界抖动。"},
    ),
    _case(
        "malformed_constraint", "构造非法硬约束，验证 fail-closed：非法约束不得进入任何 planner。",
        WEST_EAST_START, WEST_EAST_END,
        application=INPUT_GUARD,
        expert_question="约束输入的最低验证契约和 fail-closed 边界应如何定义？",
        # Deliberately inverted longitude range: west 0.06 > east 0.04.
        hard_constraints=[{"name": "合成非法硬约束（经度反转）", "bbox": [0.06, 0.02, 0.04, 0.08]}],
        quality_expectation={"expect_input_rejected": True, "note": "期望 ValueError，且 planner 从未被调用。"},
    ),
    _case(
        "zigzag_open_grid_bias", "无障碍斜向 OD，用于暴露 8 邻域格网的阶梯与方向偏置。",
        [0.01, 0.02], [0.09, 0.065],
        expert_question="A* 格网偏置应采用 any-angle、后处理平滑还是运动学 planner 哪类思路？",
        quality_expectation={
            "note": "仅描述方向分布、heading change 与 zigzag_index，不把折线数量转成评分。",
        },
    ),
    _case(
        "bbox_overblocking_demo", "细长斜向假想约束以轴对齐 BBOX 输入，用于展示包络可能过度阻断。",
        [0.01, 0.02], [0.09, 0.08],
        hard_constraints=[{
            "name": "斜向约束的当前 BBOX 表达", "bbox": [0.035, 0.025, 0.065, 0.075],
        }],
        expert_question="BBOX 与真实 polygon 可能不等价时，应如何选择约束几何表达和保守性？",
        demonstration={
            "purpose": "show_bbox_envelope_can_differ_from_hypothetical_polygon",
            "hypothetical_polygon_not_consumed_by_planner": [
                [0.035, 0.03], [0.04, 0.025], [0.065, 0.07], [0.06, 0.075], [0.035, 0.03],
            ],
            "claim_limit": "does_not_assert_polygon_is_the_final_solution",
        },
        quality_expectation={
            "note": "planner 仍只消费现有 BBOX；假想 polygon 只作表达差异说明。",
        },
    ),
)

MALFORMED_PAYLOADS = (
    ("bbox_missing", [{"name": "缺失 bbox"}]),
    ("bbox_three_items", [{"name": "三项", "bbox": [0.1, 0.1, 0.2]}]),
    ("bbox_not_a_list", [{"name": "字符串", "bbox": "0.1,0.1,0.2,0.2"}]),
    ("bbox_non_finite", [{"name": "NaN", "bbox": [0.1, 0.1, float("nan"), 0.2]}]),
    ("bbox_inverted_lon", [{"name": "经纬反转", "bbox": [0.2, 0.1, 0.1, 0.2]}]),
    ("bbox_inverted_lat", [{"name": "纬向反转", "bbox": [0.1, 0.2, 0.2, 0.1]}]),
    ("bbox_degenerate", [{"name": "退化", "bbox": [0.1, 0.1, 0.1, 0.1]}]),
    ("bbox_boolean", [{"name": "布尔", "bbox": [True, 0.1, 0.2, 0.2]}]),
    ("not_a_mapping", ["not-an-object"]),
    ("not_a_list", {"bbox": [0.1, 0.1, 0.2, 0.2]}),
    ("valid_then_malformed", [
        {"name": "合法", "bbox": [0.1, 0.1, 0.2, 0.2]},
        {"name": "非法", "bbox": [0.1, 0.1, 0.2]},
    ]),
)


def cases():
    """Return a deep copy of every case so callers can never mutate the fixtures."""

    return deepcopy(list(CASES))


def case(case_id):
    for item in CASES:
        if item["case_id"] == case_id:
            return deepcopy(item)
    raise KeyError(f"未知 benchmark case：{case_id}")


def malformed_payloads():
    return deepcopy(list(MALFORMED_PAYLOADS))


def case_ids():
    return tuple(item["case_id"] for item in CASES)
