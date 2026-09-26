from cns_planner.domain.canonical_workflow import CANONICAL_NODE_REGISTRY
from cns_planner.services.invalidation import (
    CANONICAL_EXECUTOR_CONTRACT,
    DEPENDENTS,
)


def test_canonical_registry_and_invalidation_executor_critical_edges_agree():
    for source_node, (executor_trigger, edge_map) in CANONICAL_EXECUTOR_CONTRACT.items():
        canonical_downstream = set(CANONICAL_NODE_REGISTRY[source_node].invalidates)
        executor_downstream = set(DEPENDENTS[executor_trigger])
        assert set(edge_map) <= canonical_downstream
        assert set(edge_map.values()) <= executor_downstream


def test_invalidation_executor_has_no_removed_legacy_result_edges():
    removed = {"coverage", "cns_gap", "cns_gap_v2", "cns_site_plan"}
    assert not removed.intersection(DEPENDENTS)
    assert not any(removed.intersection(results) for results in DEPENDENTS.values())
    assert "route_algorithm" not in DEPENDENTS
    assert "coverage_algorithm" not in DEPENDENTS
    assert "gap_algorithm" not in DEPENDENTS
