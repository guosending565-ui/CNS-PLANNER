"""Phase4 target DAG and pure canonical readiness projection.

This registry is additive.  It intentionally does not replace the current
``services/invalidation.py`` graph or project workflow services in B1.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from .assumptions import (
    active_assumption_ids,
    find_active_assumption_for_field,
    normalize_assumption_registry,
)
from .workflow_contract import (
    AssessmentOutcome,
    OutputMaturity,
    ReadinessState,
    WorkflowStatus,
    normalize_readiness,
)


CANONICAL_WORKFLOW_SCHEMA_VERSION = "phase4-b1"
CANONICAL_NODE_ORDER = (
    "environment",
    "risk_field",
    "route_candidate",
    "route_validation",
    "operational_route",
    "required_cns",
    "coverage",
    "service_capability",
    "service_corridor",
    "capability_gap",
    "facility_plan",
    "plan_review",
    "report",
)


@dataclass(frozen=True)
class CanonicalNodeDefinition:
    node_id: str
    dependencies: Mapping[str, tuple[str, ...]]
    invalidates: tuple[str, ...]
    next: tuple[str, ...]
    target_maturity: OutputMaturity

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "dependencies": {
                level: list(items) for level, items in self.dependencies.items()
            },
            "invalidates": list(self.invalidates),
            "next": list(self.next),
            "target_maturity": self.target_maturity.value,
        }


_DEPENDENCIES = {
    "environment": {
        "required": ("workspace", "grid", "population"),
        "assumable": ("population_nodata_policy",),
        "optional": ("terrain_evidence", "building_evidence"),
        "enhancement": ("source_checksums",),
    },
    "risk_field": {
        "required": ("environment", "population"),
        "assumable": ("shelter_coefficient", "risk_aggregation_policy"),
        "optional": ("traffic_conflict", "property_exposure", "infrastructure"),
        "enhancement": (),
    },
    "route_candidate": {
        "required": (
            "risk_field", "scenario_route", "fixed_cruise_altitude",
            "planning_request", "population", "route_cost_policy",
        ),
        "assumable": (
            "shelter_coefficient", "search_parameters", "objective_policy",
            "risk_density",
        ),
        "optional": ("terrain_evidence", "building_evidence"),
        "enhancement": ("precise_building_footprints",),
    },
    "route_validation": {
        "required": (
            "route_candidate", "route_risk_profile", "terrain_evidence",
            "building_applicability_decided",
        ),
        "assumable": ("validation_sampling_policy",),
        "optional": (),
        "enhancement": ("precise_building_footprints",),
    },
    "operational_route": {
        "required": (
            "route_validation", "terrain_validation_passed",
            "building_applicability_decided", "confirmation",
            "expected_fingerprint",
        ),
        "assumable": (),
        "optional": (),
        "enhancement": (),
    },
    "required_cns": {
        "required": ("operational_route", "required_cns_adopted"),
        "assumable": ("aircraft_profile", "requirement_source"),
        "optional": ("requirement_recommendation",),
        "enhancement": (),
    },
    "coverage": {
        "required": ("operational_route", "required_cns"),
        "assumable": (
            "existing_cns_baseline", "coverage_sampling", "device_catalog",
        ),
        "optional": (),
        "enhancement": (),
    },
    "service_capability": {
        "required": ("coverage", "required_cns", "service_model"),
        "assumable": ("service_model_baseline",),
        "optional": (),
        "enhancement": ("provider_independence_evidence",),
    },
    "service_corridor": {
        "required": ("operational_route", "coverage", "service_capability"),
        "assumable": ("corridor_sampling_policy",),
        "optional": (),
        "enhancement": (),
    },
    "capability_gap": {
        "required": ("service_corridor", "required_cns"),
        "assumable": ("existing_cns_baseline",),
        "optional": (),
        "enhancement": ("provider_independence_evidence",),
    },
    "facility_plan": {
        "required": ("capability_gap",),
        "assumable": ("existing_cns_baseline",),
        "optional": ("candidate_sites", "towers"),
        "enhancement": ("verified_tower_height",),
    },
    "plan_review": {
        "required": ("facility_plan", "review_confirmation"),
        "assumable": (),
        "optional": (),
        "enhancement": (),
    },
    "report": {
        "required": ("confirmed_plan",),
        "assumable": (),
        "optional": ("radar_layout",),
        "enhancement": (),
    },
}


def _build_registry():
    result = {}
    for index, node_id in enumerate(CANONICAL_NODE_ORDER):
        target = (
            OutputMaturity.PROVISIONAL
            if node_id in {"environment", "risk_field", "route_candidate", "route_validation"}
            else OutputMaturity.AUTHORITATIVE
        )
        result[node_id] = CanonicalNodeDefinition(
            node_id=node_id,
            dependencies=MappingProxyType(_DEPENDENCIES[node_id]),
            invalidates=CANONICAL_NODE_ORDER[index + 1 :],
            next=CANONICAL_NODE_ORDER[index + 1 : index + 2],
            target_maturity=target,
        )
    return MappingProxyType(result)


CANONICAL_NODE_REGISTRY = _build_registry()


def canonical_node_registry_snapshot() -> dict:
    return {
        node_id: definition.to_dict()
        for node_id, definition in CANONICAL_NODE_REGISTRY.items()
    }


def empty_canonical_workflow_state() -> dict:
    return {
        "schema_version": CANONICAL_WORKFLOW_SCHEMA_VERSION,
        "nodes": {
            node_id: _empty_node_state(node_id)
            for node_id in CANONICAL_NODE_ORDER
        },
    }


def normalize_canonical_workflow_state(
    value: dict | None,
    *,
    assumption_registry: dict | None = None,
) -> dict:
    if value is None:
        return empty_canonical_workflow_state()
    if not isinstance(value, dict):
        raise ValueError("canonical_workflow 必须是对象")
    raw_nodes = value.get("nodes") or {}
    if not isinstance(raw_nodes, dict):
        raise ValueError("canonical_workflow.nodes 必须是对象")
    active_ids = active_assumption_ids(assumption_registry)
    result = empty_canonical_workflow_state()
    for node_id in CANONICAL_NODE_ORDER:
        raw = raw_nodes.get(node_id)
        if raw is None:
            continue
        if not isinstance(raw, dict):
            raise ValueError(f"canonical_workflow.nodes.{node_id} 必须是对象")
        try:
            status = WorkflowStatus(raw.get("status") or "not_started").value
            maturity = OutputMaturity(
                raw.get("output_maturity") or "provisional"
            ).value
        except ValueError as exc:
            raise ValueError(f"canonical_workflow.nodes.{node_id} enum 无效") from exc
        readiness = normalize_readiness(
            raw.get("readiness"), active_assumption_ids=active_ids
        )
        result["nodes"][node_id] = {
            "status": status,
            "output_maturity": maturity,
            "readiness": readiness,
            "warnings": _strings(raw.get("warnings"), f"{node_id}.warnings"),
        }
    return result


def canonical_node_summary(
    node_id: str,
    context: dict | None = None,
    *,
    assumption_registry: dict | None = None,
    workflow_status: WorkflowStatus | str | None = None,
    assessment_outcome: AssessmentOutcome | str | None = None,
) -> dict:
    """Evaluate a node against synthetic/current facts without mutating state."""

    if node_id not in CANONICAL_NODE_REGISTRY:
        raise KeyError(node_id)
    facts = context or {}
    if not isinstance(facts, dict):
        raise ValueError("canonical context 必须是对象")
    registry = normalize_assumption_registry(assumption_registry)
    definition = CANONICAL_NODE_REGISTRY[node_id]
    blockers = []
    warnings = []
    used_assumption_ids = []
    provisional_only_assumption_ids = []

    for dependency in definition.dependencies["required"]:
        if not _fact_present(facts.get(dependency)):
            blockers.append(f"{dependency}_required")

    for dependency in definition.dependencies["assumable"]:
        if _fact_present(facts.get(dependency)):
            continue
        assumption = find_active_assumption_for_field(
            registry, dependency, node_id=node_id
        )
        if assumption is None:
            blockers.append(f"{dependency}_or_assumption_required")
            continue
        used_assumption_ids.append(assumption["assumption_id"])
        if assumption["authority_effect"] == "provisional_only":
            provisional_only_assumption_ids.append(assumption["assumption_id"])
        warnings.append(assumption["report_disclosure"])

    for dependency in (
        *definition.dependencies["optional"],
        *definition.dependencies["enhancement"],
    ):
        if not _fact_present(facts.get(dependency)):
            warnings.append(_missing_warning(dependency))

    if node_id in {"route_validation", "operational_route"}:
        building_required = facts.get("building_validation_required") is True
        if building_required and not _fact_present(facts.get("building_evidence")):
            blockers.append("building_evidence_required")
        if (
            node_id == "operational_route"
            and building_required
            and not _fact_present(facts.get("building_validation_passed"))
        ):
            blockers.append("building_validation_passed_required")

    blockers = _unique(blockers)
    warnings = _unique(warnings)
    used_assumption_ids = _unique(used_assumption_ids)
    if blockers:
        readiness_state = ReadinessState.BLOCKED
    elif used_assumption_ids:
        readiness_state = ReadinessState.READY_WITH_ASSUMPTIONS
    else:
        readiness_state = ReadinessState.READY
    readiness = normalize_readiness(
        {
            "state": readiness_state.value,
            "blockers": blockers,
            "assumption_ids": used_assumption_ids,
            "warnings": warnings,
        },
        active_assumption_ids=active_assumption_ids(registry),
    )

    if workflow_status is None:
        if blockers:
            status = WorkflowStatus.BLOCKED
        elif facts.get("executed") is True:
            status = (
                WorkflowStatus.COMPLETED_WITH_WARNINGS
                if warnings or used_assumption_ids
                else WorkflowStatus.COMPLETED
            )
        else:
            status = WorkflowStatus(readiness_state.value)
    else:
        status = WorkflowStatus(workflow_status)

    maturity = OutputMaturity.PROVISIONAL
    if (
        definition.target_maturity == OutputMaturity.AUTHORITATIVE
        and not blockers
        and not provisional_only_assumption_ids
        and status in {
            WorkflowStatus.COMPLETED,
            WorkflowStatus.COMPLETED_WITH_WARNINGS,
        }
    ):
        maturity = OutputMaturity.AUTHORITATIVE

    result = {
        "node_id": node_id,
        "status": status.value,
        "output_maturity": maturity.value,
        "readiness": readiness,
        "warnings": warnings,
    }
    if assessment_outcome is not None:
        result["assessment"] = {
            "outcome": AssessmentOutcome(assessment_outcome).value,
        }
    return result


def can_write_authoritative_result(summary: dict) -> bool:
    return (
        isinstance(summary, dict)
        and summary.get("output_maturity") == OutputMaturity.AUTHORITATIVE.value
        and summary.get("status") in {
            WorkflowStatus.COMPLETED.value,
            WorkflowStatus.COMPLETED_WITH_WARNINGS.value,
        }
        and (summary.get("readiness") or {}).get("state")
        != ReadinessState.BLOCKED.value
    )


def _empty_node_state(node_id):
    return {
        "status": WorkflowStatus.NOT_STARTED.value,
        "output_maturity": OutputMaturity.PROVISIONAL.value,
        "readiness": {
            "state": ReadinessState.BLOCKED.value,
            "blockers": ["not_evaluated"],
            "assumption_ids": [],
            "warnings": [],
        },
        "warnings": [],
    }


def _fact_present(value):
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in {
            "", "missing", "missing_data", "not_calculated", "not_configured",
            "blocked", "unknown", "not_evaluated",
        }
    if isinstance(value, dict):
        status = value.get("status")
        if status is not None and not _fact_present(status):
            return False
        return bool(value)
    if isinstance(value, (list, tuple, set)):
        return bool(value)
    return True


def _missing_warning(dependency):
    if dependency == "terrain_evidence":
        return "terrain_not_evaluated"
    if dependency == "building_evidence":
        return "building_not_evaluated"
    return f"{dependency}_not_evaluated"


def _strings(value, field):
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field} 必须是数组")
    return _unique([str(item) for item in value if str(item)])


def _unique(items):
    return list(dict.fromkeys(items))
