"""Round32-H：Step05 正式业务顺序（Radar 基线先于 P14）与业务失败时的诊断草稿报告。

覆盖（与 Round32-H §10 的后端定向清单逐条对应）：

1. Radar noncooperative 被正式要求 ⇒ readiness 权威字段 = required；
2. 只有 RID cooperative required ⇒ Radar **不得**被误判 required；
3. Radar 变化后 P14/P15/P16 继续 stale（真实失效依赖保留）；
4. Radar→P14 的 invalidation 未被删除，且仍由 canonical 判据门控；
5. P17 unacceptable + 未初始化 review 时草稿报告仍可成功生成，并给出可读诊断；
6. 正式报告仍拒绝未确认方案（门禁未放宽）。

本文件只做定向验证，不重跑全量回归。
"""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from cns_planner.application.radar_surveillance_layout_service import (
    RadarSurveillanceLayoutService,
)
from cns_planner.application import radar_surveillance_layout_service as radar_service_module
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.cns_service_contract import (
    SERVICE_KEY_RADAR_NONCOOPERATIVE,
    SERVICE_KEY_RID_COOPERATIVE,
)
from cns_planner.domain.radar_service_evidence import radar_required_for
from cns_planner.reporting.html_renderer import HtmlReportRenderer

ROOT = Path(__file__).parents[1]
DEFAULTS = Path("cns_planner/config/defaults.json")
RADAR = SERVICE_KEY_RADAR_NONCOOPERATIVE
RID = SERVICE_KEY_RID_COOPERATIVE

ROUTE_ID = "R0005"


def radar_requirement(*, radar=True, rid=True, route_id=ROUTE_ID):
    """正式 CNS 需求：RID 与 Radar 分属**两条**服务，必须能单独声明。"""

    services = {}
    if rid:
        services[RID] = {"required": True, "service_key": RID, "confirmed": True}
    if radar:
        services[RADAR] = {"required": True, "service_key": RADAR, "confirmed": True}
    surveillance = {
        "required": True, "coverage_requirement": 0.95,
        "performance": {"max_update_interval_s": 2.0},
        "services": services,
    }
    if len(services) > 1:
        surveillance["service_requirement_mode"] = "all_required"
    return {
        "project_default": {
            "communication": {"required": False},
            "navigation": {"required": False},
            "surveillance": surveillance,
        },
        "route_overrides": {},
    }


def rid_only_surveillance_requirement():
    """**过宽判据的回归夹具**：surveillance.required / coverage / performance 全部为真，
    但唯一的 canonical 服务是 ``S:rid_cooperative``。"""

    return {
        "project_default": {
            "communication": {"required": False},
            "navigation": {"required": False},
            "surveillance": {
                "required": True, "coverage_requirement": 0.95,
                "performance": {"min_detection_range_m": 2000.0, "max_update_interval_s": 2.0},
                "services": {RID: {"required": True, "service_key": RID, "confirmed": True}},
            },
        },
        "route_overrides": {},
    }


class Session:
    def __init__(self, state):
        self.state = state

    def save(self):
        pass


class Invalidation:
    def __init__(self):
        self.calls = []

    def cns_corridor(self):
        self.calls.append("cns_corridor")


def radar_service(state):
    return RadarSurveillanceLayoutService(Session(state), Invalidation(), lambda: {})


def state_with_requirement(requirement):
    return {
        "required_cns": requirement,
        "operational_routes": [{"route_id": ROUTE_ID, "status": "passed"}],
    }


# ---------------------------------------------------------------- Radar 必需性权威


def test_radar_readiness_reports_canonical_requirement_for_radar_routes():
    state = state_with_requirement(radar_requirement(radar=True, rid=True))
    readiness = radar_service(state).readiness_snapshot()

    assert readiness["required_for_current_routes"] is True
    assert readiness["required_route_ids"] == [ROUTE_ID]
    assert readiness["required_basis"] == "domain.radar_service_evidence.radar_required_for"
    # 与 canonical 判据逐条一致（不是第二套规则）
    for route_id in readiness["required_route_ids"]:
        assert radar_required_for(state["required_cns"], route_id) is True


def test_rid_only_requirement_never_marks_radar_as_required():
    requirement = rid_only_surveillance_requirement()
    state = state_with_requirement(requirement)
    readiness = radar_service(state).readiness_snapshot()

    # 夹具确实"看起来"要求了监视，但 Radar 非合作监视并不在其中
    assert requirement["project_default"]["surveillance"]["required"] is True
    assert requirement["project_default"]["surveillance"]["coverage_requirement"] == 0.95
    assert radar_required_for(requirement, ROUTE_ID) is False
    assert readiness["required_for_current_routes"] is False
    assert readiness["required_route_ids"] == []


def test_legacy_subsystem_surveillance_requirement_alone_is_not_radar_required():
    """真实项目形态：R0003/R0004 的 ``surveillance.required`` 为真但没有任何 canonical
    service 声明（``services`` 为空），它们**不**构成 Radar 非合作监视需求。"""

    requirement = radar_requirement(radar=False, rid=True)
    requirement["route_overrides"] = {
        "R0003": {"surveillance": {"required": True}},
        "R0004": {"surveillance": {"required": True}},
    }
    state = state_with_requirement(requirement)
    readiness = radar_service(state).readiness_snapshot()

    assert radar_required_for(requirement, "R0003") is False
    assert radar_required_for(requirement, "R0004") is False
    assert readiness["required_for_current_routes"] is False
    assert readiness["required_route_ids"] == []


def test_required_route_overrides_alone_still_mark_radar_required():
    requirement = radar_requirement(radar=False, rid=True)
    requirement["route_overrides"] = {
        ROUTE_ID: {
            "communication": {"required": False}, "navigation": {"required": False},
            "surveillance": {
                "required": True,
                "services": {RADAR: {"required": True, "service_key": RADAR, "confirmed": True}},
            },
        }
    }
    readiness = radar_service(state_with_requirement(requirement)).readiness_snapshot()

    assert radar_required_for(requirement, ROUTE_ID) is True
    assert readiness["required_for_current_routes"] is True
    assert readiness["required_route_ids"] == [ROUTE_ID]


# ------------------------------------------------- Radar 变化 ⇒ P14/P15/P16 继续 stale


@pytest.fixture
def workflow(tmp_path):
    return WorkflowService(tmp_path / "project.json", DEFAULTS)


def _seed_downstream(state, requirement):
    state["required_cns"] = requirement
    state["operational_routes"] = [{"route_id": ROUTE_ID, "status": "passed"}]
    state["radar_surveillance_layout"] = {
        "status": "passed",
        "items": [{"route_id": ROUTE_ID, "status": "proposal_ready"}],
    }
    state["cns_corridor_assessment"] = {"status": "passed"}
    state["cns_corridor_gap_assessment"] = {"status": "passed"}
    state["cns_corridor_site_plan"] = {"status": "proposal_ready"}


def test_radar_change_keeps_p14_p15_p16_stale_when_radar_is_required(workflow):
    state = workflow.state
    _seed_downstream(state, radar_requirement(radar=True, rid=True))
    upstream = deepcopy(state["required_cns"])

    outcome = RadarSurveillanceLayoutService(
        workflow.session, workflow.invalidation_service, lambda: {},
    ).stale_for_reason("radar_surveillance_inputs_changed")

    assert state["cns_corridor_assessment"]["status"] == "stale"
    assert state["cns_corridor_gap_assessment"]["status"] == "stale"
    assert state["cns_corridor_site_plan"]["status"] == "stale"
    assert "cns_corridor_assessment" in outcome["downstream"]
    assert "cns_corridor_gap_assessment" in outcome["downstream"]
    assert "cns_corridor_site_plan" in outcome["downstream"]
    # 失效必须单向：上游需求与运行航路绝不被反向改写
    assert state["required_cns"] == upstream
    assert state["operational_routes"][0]["status"] == "passed"


def test_radar_change_never_stales_the_corridor_when_radar_is_not_required(workflow):
    state = workflow.state
    _seed_downstream(state, rid_only_surveillance_requirement())

    RadarSurveillanceLayoutService(
        workflow.session, workflow.invalidation_service, lambda: {},
    ).stale_for_reason("radar_surveillance_inputs_changed")

    assert state["cns_corridor_assessment"]["status"] == "passed"
    assert state["cns_corridor_gap_assessment"]["status"] == "passed"
    assert state["cns_corridor_site_plan"]["status"] == "proposal_ready"


def test_radar_to_corridor_invalidation_edge_stays_in_place():
    source = (
        ROOT / "cns_planner/application/radar_surveillance_layout_service.py"
    ).read_text(encoding="utf-8")

    # 发布路径与 stale 路径都必须保留 Radar → P14 的失效边，且仍由 canonical
    # radar_required_for() 门控（绝不放宽成无条件失效，也绝不删除）。
    assert source.count("self.invalidation.cns_corridor()") >= 2
    assert 'radar_required_for(state.get("required_cns") or {}, route_id)' in source
    assert 'radar_required_for(state.get("required_cns") or {}, item.get("route_id"))' in source
    # 也不得出现"把 stale 强行改回 current / proposal_ready"的救火写法
    assert 'item["status"] = "proposal_ready"' not in source
    assert 'entry["status"] = "current"' not in source


# ----------------------------------------------- 业务失败（P17 unacceptable）草稿报告


def _model_with(sections, statistics=None):
    return {
        "report_schema_version": "1.0",
        "template_version": "cns-planning-report-zh-v1",
        "language": "zh-CN",
        "generated_at": "2026-10-07T00:00:00+00:00",
        "report_mode": "draft_preview",
        "source": {}, "plan_status_label": "草稿预览（尚无已确认方案）",
        "disclaimers": [], "sections": sections,
        "statistics": statistics or {}, "report_data_fingerprint": "fp",
    }


def test_draft_report_preview_succeeds_without_confirmed_plan_or_review(workflow):
    state = workflow.state
    state["continuous_service_acceptability"] = {
        "status": "unacceptable", "baseline_status": "unknown",
        "post_plan_status": "unacceptable", "unacceptable_count": 2,
        "unknown_count": 0, "managed_gap_count": 0,
        "reasons": ["continuous_service_gap_exceeds_threshold"],
    }
    before = deepcopy(state)

    preview = workflow.preview_cns_planning_report()

    assert preview["status"] == "draft" and preview["persisted"] is False
    # 预览响应绝不回传整个 ReportDataModel / artifact 清单（Round32-G 的 821 MB 来源）
    assert "report_data" not in preview and "source_artifacts" not in preview
    html = preview["html"]
    assert html.startswith("<!doctype html>")
    assert len(html) < 2_000_000
    for text in ("诊断草稿", "不是已确认规划方案，也不是正式报告",
                 "为什么现在不能确认", "正式 Confirm 是否允许",
                 "方案评审初始化未满足的正式前置条件", "Radar 基线"):
        assert text in html, text
    # 业务结论原样保留：unacceptable 不得被改写成可接受
    assert preview["diagnostics"]["confirm_gate"]["status"] == "unacceptable"
    assert preview["diagnostics"]["confirm_gate"]["confirmation_allowed"] is False
    assert state == before


def test_formal_report_still_requires_a_confirmed_plan(workflow):
    preview = workflow.preview_cns_planning_report()
    assert preview["status"] == "draft"
    with pytest.raises(ValueError, match="确认一个规划方案"):
        workflow.generate_cns_planning_report()
    assert (workflow.state.get("cns_planning_reports") or {}).get("active_report_id") in (None, "")


def test_draft_report_diagnostics_use_canonical_confirm_gate_and_radar_requirement(workflow):
    state = workflow.state
    state["continuous_service_acceptability"] = {
        "status": "unacceptable", "baseline_status": "unknown",
        "post_plan_status": "unacceptable", "unacceptable_count": 1,
        "unknown_count": 0, "managed_gap_count": 0,
    }
    state["required_cns"] = radar_requirement(radar=True, rid=True)
    state["operational_routes"] = [{"route_id": ROUTE_ID, "status": "passed"}]

    diagnostics = workflow.report_draft_diagnostics()

    assert diagnostics["confirm_gate"]["confirmation_allowed"] is False
    assert diagnostics["confirm_gate"]["allowed_statuses"] == [
        "fully_satisfied", "acceptable_with_managed_gap",
    ]
    # P18 前置：未初始化 P14/P15/P16 时必须给出与 fail-closed 门禁同一份原因
    assert "P18 需要 current cns_corridor_assessment" in diagnostics["plan_review_blockers"]
    radar = diagnostics["radar_baseline"]
    assert radar["required_for_current_routes"] is True
    assert radar["required_route_ids"] == [ROUTE_ID]
    assert radar["required_basis"] == "domain.radar_service_evidence.radar_required_for"


def test_radar_diagnostics_ignore_stale_items_outside_the_formal_requirement(workflow):
    """真实项目形态：已废弃的 R0003 条目是 stale，但它**不在**正式需求内；草案里的
    "Radar 基线" 结论必须只看必需航路，绝不把 R0003 的 stale 说成基线已失效。"""

    state = workflow.state
    requirement = radar_requirement(radar=True, rid=True)
    requirement["route_overrides"] = {"R0003": {"surveillance": {"required": True}}}
    state["required_cns"] = requirement
    state["operational_routes"] = [{"route_id": ROUTE_ID, "status": "passed"}]
    state["radar_surveillance_layout"] = {
        "status": "pending_confirmation",
        "items": [
            {"route_id": "R0003", "status": "stale",
             "stale_reason": "layered_validation_evidence_outdated",
             "algorithm_version": radar_service_module.ALGORITHM_VERSION},
            {"route_id": ROUTE_ID, "status": "proposal_ready",
             "algorithm_version": radar_service_module.ALGORITHM_VERSION,
             "selected_panel_count": 7, "selected_tower_count": 7,
             "solver": {"status": "optimal", "optimality_proven": True}},
        ],
    }

    radar = workflow.report_draft_diagnostics()["radar_baseline"]

    assert radar["required_for_current_routes"] is True
    assert radar["required_route_ids"] == [ROUTE_ID]
    assert radar["status"] == "passed"
    assert radar["current_applicability"] == "current"
    assert radar["stale_reason"] is None
    assert radar["stale_route_ids"] == [] and radar["missing_route_ids"] == []
    rows = {str(row["route_id"]): row for row in radar["routes"]}
    assert rows["R0003"]["required"] is False and rows["R0003"]["status"] == "stale"
    assert rows[ROUTE_ID]["required"] is True and rows[ROUTE_ID]["status"] == "proposal_ready"


def test_report_renderer_bounds_oversized_audit_json():
    huge = {
        "plan_status": "not_confirmed",
        "existing_facilities": {"items": []},
        "confirmed_actions": [
            {"action_id": "A%05d" % index, "payload": "x" * 200} for index in range(4000)
        ],
    }
    model = _model_with({"final_facility_plan": huge})
    full_json_size = len(json.dumps(huge, ensure_ascii=False, sort_keys=True, indent=2))

    html = HtmlReportRenderer().render(model)

    assert full_json_size > 262144
    assert len(html) < 400_000
    assert "超过报告内嵌上限" in html
    # 结构索引仍可读：顶层键与条目数保留，但不再内嵌原始 payload
    assert "confirmed_actions" in html
    assert "x" * 200 not in html


def test_draft_report_renders_readable_p16_proposal_and_unmet_objectives():
    sections = {
        "plan_review_p18": {
            "p16_decision_evidence": {
                "status": "proposal_ready",
                "stop_reason": "intervention_selection_limit_reached",
                "candidate_actions": [{"action_id": "A1"}, {"action_id": "A2"}],
                "selected_actions": [{
                    "action_id": "A1", "service_key": "C:communication",
                    "reuse_class": "tower_colocation_host", "distinct_site_id": "SITE-1",
                    "device_id": "DEV-1", "current_units": 1, "required_units": 2,
                    "host": {"host_tower_name": "塔A", "host_tower_id": "T-1"},
                    "status": "proposed",
                }],
                "residual_confirmed_targets": [{
                    "service_key": "S:rid_cooperative", "surface_class": "land",
                    "current_units": 1, "required_units": 2, "final_status": "confirmed_deficit",
                }],
                "intervention_selection_limit": 6,
                "p16_actionable_target_count": 10,
                "input_fingerprint": "fp-16",
            },
        },
        "corridor_gap_objectives_p15": {
            "routes": [{
                "route_id": ROUTE_ID,
                "subsystems": [{
                    "subsystem": "C",
                    "objective_results": [{
                        "objective": "min_satisfied_volume_fraction", "actual": 0.898,
                        "operator": ">=", "target": 0.95, "status": "not_met",
                        "confirmed": True,
                    }],
                }],
            }],
        },
    }
    html = HtmlReportRenderer().render(_model_with(sections))

    for text in ("P16 设施规划 proposal", "已选动作（提案，尚未采纳）", "A1",
                 "intervention_selection_limit_reached", "残余确认目标",
                 "规划目标逐项结论（未满足 1 项）", "最小满足体积占比", "0.898",
                 "未满足", "塔A"):
        assert text in html, text
    # 正式报告路径（无诊断）不得出现草稿横幅
    assert "诊断草稿" not in html


def test_draft_banner_and_radar_block_only_appear_for_the_draft():
    diagnostics = {
        "confirm_gate": {
            "status": "unacceptable", "confirmation_allowed": False,
            "allowed_statuses": ["fully_satisfied", "acceptable_with_managed_gap"],
            "baseline_status": "unknown", "post_plan_status": "unacceptable",
            "unacceptable_count": 2, "unknown_count": 0, "managed_gap_count": 0,
            "reasons": ["continuous_service_gap_exceeds_threshold"],
        },
        "plan_review_blockers": ["P18 需要 current cns_corridor_assessment"],
        "radar_baseline": {
            "status": "passed", "current_applicability": "current",
            "required_for_current_routes": True, "required_route_ids": [ROUTE_ID],
            "required_basis": "domain.radar_service_evidence.radar_required_for",
            "readiness_status": "passed", "readiness_blockers": [],
            "routes": [{
                "route_id": ROUTE_ID, "status": "proposal_ready",
                "stage": "stage_a_radar_i", "stage_label": "Stage A（仅 Radar-I）",
                "selected_panel_count": 7, "selected_tower_count": 7,
                "radar_ii_site_count": 0, "gap_reason": None,
                "solver": {"status": "optimal", "optimality_proven": True},
            }],
        },
    }
    html = HtmlReportRenderer().render(_model_with({}), diagnostics=diagnostics)

    for text in ("诊断草稿", "不是已确认规划方案，也不是正式报告", "为什么现在不能确认",
                 "不可接受（业务失败）", "不允许", "P18 需要 current cns_corridor_assessment",
                 "连续服务结论", "Radar 基线", ROUTE_ID, "已证明最优", "规划提案已生成"):
        assert text in html, text
    assert HtmlReportRenderer().render(_model_with({})).find("诊断草稿") < 0
