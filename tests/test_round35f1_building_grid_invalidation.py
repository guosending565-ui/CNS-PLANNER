"""Round35-F1 / F1-B regression: the ``building_grid`` invalidation chain.

Round35-E (`07_fix_plan.md` §4.1) found a hole: ``invalidation_service.grid_sources``
handled ``{"terrain_dtm", "buildings"}`` for the PCF / validation edges, so changing
**only** the L8 building grid (``building_grid``) left the production feasibility
authority (``planning_constraint_fields``) and the two continuous validations holding
results computed from now-outdated building facts.

This file pins the real dependency: the L8 building grid is a **direct** input of

* ``PlanningConstraintFieldService._building`` (reads ``grid_attributes["buildings"].cells``,
  which ``SOURCE_ATTRIBUTES`` maps from ``building_grid``);
* ``LayeredRouteValidation`` and ``VerticalTransitionValidation`` (their stored records
  bind the terrain/building **source audits**).

It also pins the two boundaries that must NOT change: nothing building-unrelated may be
staled, and evidence is preserved (``stale``, never deleted).
"""

from copy import deepcopy
from pathlib import Path

from cns_planner.application.workflow_service import WorkflowService

DEFAULTS = Path(__file__).parents[1] / "cns_planner" / "config" / "defaults.json"


def prepared(tmp_path, name="project.json"):
    service = WorkflowService(tmp_path / name, DEFAULTS)
    state = service.state
    state["planning_constraint_fields"] = {
        "status": "passed", "count": 1,
        "items": [{
            "field_id": "PCF-ALT-100", "altitude_layer_id": "ALT-100",
            "status": "passed", "current_applicability": "current",
            "constraint_field_fingerprint": "pcf-fp",
        }],
    }
    state["result_statuses"]["planning_constraint_fields"] = "passed"
    state["layered_route_validations"] = {
        "schema_version": "layered-route-validation-collection",
        "status": "passed", "count": 1, "active_validation_id": "LRV-1",
        "items": [{
            "validation_id": "LRV-1", "status": "validated_candidate",
            "current_applicability": "current", "candidate": {"candidate_id": "LC-1"},
        }],
    }
    state["result_statuses"]["layered_route_validation"] = "validated_candidate"
    state["vertical_transition_validations"] = {
        "schema_version": "vertical-transition-validation-collection",
        "status": "passed", "count": 1, "active_validation_id": "VTV-1",
        "items": [{
            "validation_id": "VTV-1", "status": "validated",
            "current_applicability": "current", "full_3d_geometry_validated": True,
        }],
    }
    state["result_statuses"]["vertical_transition_validation"] = "validated"
    state["layered_route_candidates"] = {
        "status": "passed", "count": 1, "active_candidate_id": "LC-1",
        "items": [{
            "candidate_id": "LC-1", "status": "candidate",
            "current_applicability": "current",
        }],
        "masks": {},
    }
    state["result_statuses"]["layered_route_candidate"] = "candidate"
    # 与建筑无关的容器：本轮不得被建筑失效波及。
    state["grid_risk"] = {"status": "passed", "algorithm_id": "risk-v1"}
    state["result_statuses"]["environment_risk"] = "passed"
    state["grid_risk_v2"] = {"status": "passed"}
    state["result_statuses"]["grid_risk_v2"] = "passed"
    state["operational_routes"] = [{"route_id": "OP-1", "status": "published"}]
    state["route_safety_evidence_v2"] = {
        "schema_version": "route-safety-evidence-v2-collection",
        "status": "passed", "count": 1, "active_assessment_id": "RSE-1",
        "items": [{"assessment_id": "RSE-1", "status": "passed"}],
    }
    state["result_statuses"]["route_safety_evidence_v2"] = "passed"
    state["radar_surveillance_layout"] = {
        "status": "passed", "collection_id": "radar_surveillance_layout",
        "items": [{"route_id": "R-1", "status": "passed"}],
    }
    state["result_statuses"]["radar_surveillance_layout"] = "passed"
    return service


def stale_flags(service):
    state = service.state
    return {
        "planning_constraint_fields": (
            (state.get("planning_constraint_fields") or {}).get("status"),
            [(item.get("status"), item.get("reason")) for item in
             ((state.get("planning_constraint_fields") or {}).get("items") or [])],
        ),
        "layered_route_validation": (
            state.get("result_statuses", {}).get("layered_route_validation"),
            (state.get("layered_route_validations") or {}).get("status"),
        ),
        "vertical_transition_validation": (
            state.get("result_statuses", {}).get("vertical_transition_validation"),
            (state.get("vertical_transition_validations") or {}).get("status"),
        ),
        "layered_route_candidate": state.get("result_statuses", {}).get(
            "layered_route_candidate"
        ),
        "radar_surveillance_layout": (
            state.get("result_statuses", {}).get("radar_surveillance_layout"),
            [
                item.get("status")
                for item in ((state.get("radar_surveillance_layout") or {}).get("items") or [])
            ],
        ),
        "grid_risk": state.get("result_statuses", {}).get("environment_risk"),
        "grid_risk_v2": state.get("result_statuses", {}).get("grid_risk_v2"),
        "operational_routes": [
            item.get("status") for item in (state.get("operational_routes") or [])
        ],
        "route_safety_evidence_v2": state.get("result_statuses", {}).get(
            "route_safety_evidence_v2"
        ),
    }


def test_building_grid_only_change_stales_the_whole_dependent_chain(tmp_path):
    service = prepared(tmp_path)
    service.invalidation_service.grid_sources({"building_grid"})
    flags = stale_flags(service)

    # 三条真实依赖 building_grid 的结果必须 stale（F1-B 修复的正是这三条）。
    assert flags["planning_constraint_fields"][1][0][0] == "stale"
    assert flags["planning_constraint_fields"][0] == "stale"
    assert flags["layered_route_validation"][0] == "stale"
    assert flags["layered_route_validation"][1] == "stale"
    assert flags["vertical_transition_validation"][0] == "stale"
    assert flags["vertical_transition_validation"][1] == "stale"
    # 候选航路仍然按既有语义 stale。
    assert flags["layered_route_candidate"] == "stale"
    # 雷达初步划设（本来就消费 building_grid）仍然 stale。
    assert flags["radar_surveillance_layout"] == ("stale", ["stale"])
    # PCF 的失效原因必须点名 building_grid —— 否则无法审计"哪条源变化导致的"。
    assert "building_grid" in str(
        service.state["planning_constraint_fields"]["items"][0].get("stale_reason")
    )


def test_building_grid_change_does_not_stale_unrelated_results(tmp_path):
    service = prepared(tmp_path)
    service.invalidation_service.grid_sources({"building_grid"})
    flags = stale_flags(service)

    # 建筑网格变化**不直接**改写正式航路与 Safety Evidence 记录。
    assert flags["operational_routes"] == ["published"]
    # 既有语义（非本轮引入）：`SOURCE_ATTRIBUTES["building_grid"] == ("buildings",)` 使
    # grid_attributes.buildings 本身 stale，而 grid_sources 沿用既有的
    # `risk()` / `risk_v2()` 传播。本轮只补齐 PCF 与连续验证三条边，未改动这条既有耦合，
    # 因此这里如实断言 legacy risk 会 stale，避免把"没动"当成"没发生"。
    assert flags["grid_risk"] == "stale"
    assert flags["grid_risk_v2"] == "stale"
    # Safety Evidence V2 是本链的末端汇点，候选/验证变化时按既有语义 stale；
    # 关键是它**没有**被删除、也没有回到 not_calculated。
    assert flags["route_safety_evidence_v2"] in ("stale", "passed")
    assert service.state["route_safety_evidence_v2"]["items"][0]["assessment_id"] == "RSE-1"


def test_building_grid_change_preserves_evidence_and_records_a_reason(tmp_path):
    service = prepared(tmp_path)
    before = deepcopy(service.state["layered_route_validations"]["items"])
    service.invalidation_service.grid_sources({"building_grid"})
    after = service.state["layered_route_validations"]["items"]

    # 证据保留：记录数量与 id 不变，只多出 stale 标记（绝不删除历史结果）。
    assert len(after) == len(before) == 1
    assert after[0]["validation_id"] == before[0]["validation_id"]
    assert after[0]["status"] == "stale"
    assert after[0]["current_applicability"] == "stale"
    assert after[0].get("stale_reason")
    # PCF 侧的 reason 直接点名 building_grid（同一事实，两条链都必须可审计）。
    assert "building_grid" in str(
        service.state["planning_constraint_fields"]["items"][0]["stale_reason"]
    )


def test_terrain_and_building_change_still_covers_the_same_edges(tmp_path):
    # 与 terrain_dtm / buildings 同时变化时不得退化（reason 必须包含两个源）。
    service = prepared(tmp_path, name="both.json")
    service.invalidation_service.grid_sources({"terrain_dtm", "buildings", "building_grid"})
    item = service.state["planning_constraint_fields"]["items"][0]
    assert item["status"] == "stale"
    assert "buildings" in str(item["stale_reason"])
    assert service.state["result_statuses"]["layered_route_validation"] == "stale"
    assert service.state["result_statuses"]["vertical_transition_validation"] == "stale"


def test_source_attribute_mapping_still_points_building_grid_at_buildings():
    # 依赖关系必须可审计：building_grid 变化会先 stale grid_attributes.buildings。
    from cns_planner.application.invalidation_service import InvalidationService

    assert InvalidationService.SOURCE_ATTRIBUTES["building_grid"] == ("buildings",)


def test_building_grid_change_does_not_touch_airspace_display_layer(tmp_path):
    service = prepared(tmp_path, name="airspace.json")
    service.state["grid_attributes"]["airspace"] = {"status": "passed"}
    service.invalidation_service.grid_sources({"building_grid"})
    assert service.state["grid_attributes"]["airspace"]["status"] == "passed"
