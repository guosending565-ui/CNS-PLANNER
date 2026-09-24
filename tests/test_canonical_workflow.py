from cns_planner.domain.assumptions import add_assumption
from cns_planner.domain.canonical_workflow import (
    CANONICAL_NODE_ORDER,
    CANONICAL_NODE_REGISTRY,
    can_write_authoritative_result,
    canonical_node_registry_snapshot,
    canonical_node_summary,
)
from cns_planner.domain.workflow_contract import AssessmentOutcome, WorkflowStatus


def shelter_assumption(**overrides):
    value = {
        "assumption_id": "ASM-SHELTER-1",
        "scope": "node",
        "node_id": "route_candidate",
        "field": "shelter_coefficient",
        "value": 1.0,
        "unit": None,
        "basis": "engineering_baseline",
        "reason": "真实遮蔽数据缺失",
        "source_ref": None,
        "owner": "planner",
        "confirmed": True,
        "authority_effect": "allowed_with_disclosure",
        "report_disclosure": "采用遮蔽系数 1.0 工程基线；不是现场遮蔽事实",
        "created_at": "2026-09-24T00:00:00+00:00",
        "expires_at": None,
        "invalidates_on": ["shelter-source-change"],
        "status": "active",
    }
    value.update(overrides)
    return value


def candidate_context(**overrides):
    value = {
        "risk_field": True,
        "scenario_route": True,
        "fixed_cruise_altitude": True,
        "planning_request": True,
        "population": True,
        "route_cost_policy": True,
        "search_parameters": True,
        "objective_policy": True,
        "risk_density": True,
        "terrain_evidence": True,
        "building_evidence": True,
        "precise_building_footprints": True,
    }
    value.update(overrides)
    return value


def test_canonical_registry_declares_exact_dag_and_dependency_classes():
    assert CANONICAL_NODE_ORDER == (
        "environment", "risk_field", "route_candidate", "route_validation",
        "operational_route", "required_cns", "coverage", "service_capability",
        "service_corridor", "capability_gap", "facility_plan", "plan_review", "report",
    )
    snapshot = canonical_node_registry_snapshot()
    for index, node_id in enumerate(CANONICAL_NODE_ORDER):
        node = snapshot[node_id]
        assert set(node["dependencies"]) == {
            "required", "assumable", "optional", "enhancement",
        }
        assert node["next"] == list(CANONICAL_NODE_ORDER[index + 1:index + 2])
        assert node["invalidates"] == list(CANONICAL_NODE_ORDER[index + 1:])
    assert "population" in CANONICAL_NODE_REGISTRY["route_candidate"].dependencies["required"]
    assert "shelter_coefficient" in CANONICAL_NODE_REGISTRY["route_candidate"].dependencies["assumable"]


def test_population_missing_blocks_candidate_but_shelter_assumption_allows_readiness():
    missing_population = canonical_node_summary(
        "route_candidate", candidate_context(population=False, shelter_coefficient=True)
    )
    assert missing_population["readiness"]["state"] == "blocked"
    assert "population_required" in missing_population["readiness"]["blockers"]

    registry = add_assumption(None, shelter_assumption())
    assumed_shelter = canonical_node_summary(
        "route_candidate",
        candidate_context(shelter_coefficient=False),
        assumption_registry=registry,
    )
    assert assumed_shelter["readiness"]["state"] == "ready_with_assumptions"
    assert assumed_shelter["readiness"]["assumption_ids"] == ["ASM-SHELTER-1"]


def test_missing_terrain_allows_provisional_candidate_but_blocks_publish():
    candidate = canonical_node_summary(
        "route_candidate",
        candidate_context(
            shelter_coefficient=True,
            terrain_evidence=False,
            building_evidence=False,
            executed=True,
        ),
    )
    assert candidate["status"] == "completed_with_warnings"
    assert candidate["output_maturity"] == "provisional"
    assert candidate["readiness"]["state"] == "ready"
    assert {"terrain_not_evaluated", "building_not_evaluated"} <= set(candidate["warnings"])
    assert not can_write_authoritative_result(candidate)

    publish = canonical_node_summary("operational_route", {
        "route_validation": True,
        "terrain_validation_passed": False,
        "building_applicability_decided": True,
        "building_validation_required": False,
        "confirmation": True,
        "expected_fingerprint": True,
        "executed": False,
    })
    assert publish["readiness"]["state"] == "blocked"
    assert publish["output_maturity"] == "provisional"
    assert not can_write_authoritative_result(publish)


def test_canonical_contract_treats_altitude_layer_identity_as_opaque_metadata():
    """多高度层兼容：契约不写死 ALT-080/80 m，只把高度层当参数化元数据。

    本轮不建立 AltitudeLayer 业务模型，只证明 B1 foundation 不拒绝任意
    ``altitude_layer_id``，也不在 readiness 中把某个高度层当作默认事实。
    """

    def candidate(facts):
        return canonical_node_summary(
            "route_candidate",
            candidate_context(shelter_coefficient=True, **facts),
        )

    baseline_080 = candidate({
        "fixed_cruise_altitude": {"altitude_layer_id": "ALT-080", "nominal_altitude_m": 80.0},
    })
    for layer_id, nominal in (("ALT-060", 60.0), ("ALT-100", 100.0), ("ALT-ZS-999", 999.0)):
        other = candidate({
            "fixed_cruise_altitude": {
                "altitude_layer_id": layer_id, "nominal_altitude_m": nominal,
            },
        })
        assert other["readiness"] == baseline_080["readiness"]
        assert other["status"] == baseline_080["status"]
        assert other["output_maturity"] == "provisional"

    # 契约层没有固定层 ID、没有固定高度数值，也不把高度层身份当作 schema 枚举。
    for node in CANONICAL_NODE_REGISTRY.values():
        levels = [*node.dependencies["required"], *node.dependencies["assumable"]]
        assert not any(str(level).startswith("ALT-") for level in levels)
        assert not any(isinstance(level, (int, float)) for level in levels)

    assert "fixed_cruise_altitude" in (
        CANONICAL_NODE_REGISTRY["route_candidate"].dependencies["required"]
    )


def test_assessment_failed_is_separate_from_workflow_failed_and_provisional_cannot_publish():
    validation = canonical_node_summary(
        "route_validation",
        {
            "route_candidate": True,
            "route_risk_profile": True,
            "terrain_evidence": True,
            "building_applicability_decided": True,
            "building_validation_required": False,
            "validation_sampling_policy": True,
            "precise_building_footprints": True,
        },
        workflow_status=WorkflowStatus.COMPLETED,
        assessment_outcome=AssessmentOutcome.FAILED,
    )
    assert validation["status"] == "completed"
    assert validation["assessment"]["outcome"] == "failed"
    assert validation["output_maturity"] == "provisional"
    assert not can_write_authoritative_result(validation)

    provisional_registry = add_assumption(None, shelter_assumption(
        assumption_id="ASM-EMPTY-PROVISIONAL",
        scope="project",
        node_id=None,
        field="existing_cns_baseline",
        value="empty",
        authority_effect="provisional_only",
    ))
    facility_plan = canonical_node_summary(
        "facility_plan",
        {
            "capability_gap": True,
            "existing_cns_baseline": False,
            "executed": True,
        },
        assumption_registry=provisional_registry,
    )
    assert facility_plan["status"] == "completed_with_warnings"
    assert facility_plan["output_maturity"] == "provisional"
    assert not can_write_authoritative_result(facility_plan)
