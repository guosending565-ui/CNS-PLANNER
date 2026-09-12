"""Minimal JSON-safe qualitative and guarded quantitative fault-tree evaluator."""

from math import prod


NODE_TYPES = {"top_event", "and", "or", "basic_event", "reference"}
EVENT_STATUSES = {"triggered", "not_triggered", "unknown", "not_applicable"}


def _probability(value):
    if value is None or value == "":
        return None
    number = float(value)
    if not 0 <= number <= 1:
        raise ValueError("fault-tree probability 必须在 0..1")
    return number


def _leaf(node, event_states):
    event_ref = node.get("event_ref")
    external = event_states.get(event_ref, {}) if event_ref else {}
    if isinstance(external, str):
        external = {"status": external}
    status = str(node.get("state") or external.get("status") or "unknown")
    if status not in EVENT_STATUSES:
        status = "unknown"
    probability = _probability(
        node.get("probability") if "probability" in node else external.get("probability")
    )
    return {
        "status": status,
        "qualitative_status": status,
        "probability": probability,
        "event_ref": event_ref,
        "children": [],
    }


def _qualitative(kind, children):
    statuses = [child["qualitative_status"] for child in children]
    if not statuses:
        return "unknown"
    if kind == "and":
        if any(status == "not_triggered" for status in statuses):
            return "not_triggered"
        if all(status == "triggered" for status in statuses):
            return "triggered"
    else:
        if any(status == "triggered" for status in statuses):
            return "triggered"
        if all(status == "not_triggered" for status in statuses):
            return "not_triggered"
    if all(status == "not_applicable" for status in statuses):
        return "not_applicable"
    return "unknown"


def _evaluate(node, event_states):
    if not isinstance(node, dict):
        raise ValueError("fault-tree node 必须是对象")
    kind = str(node.get("node_type") or node.get("type") or "")
    if kind not in NODE_TYPES:
        raise ValueError(f"fault-tree node type 无效：{kind}")
    if kind in {"basic_event", "reference"}:
        return _leaf(node, event_states)
    children = node.get("children") or []
    if not isinstance(children, list):
        raise ValueError("fault-tree children 必须是数组")
    evaluated = [_evaluate(child, event_states) for child in children]
    if kind == "top_event":
        if len(evaluated) != 1:
            raise ValueError("top_event 必须且只能包含一个子节点")
        child = evaluated[0]
        return {**child, "event_ref": node.get("event_ref"), "children": evaluated}
    qualitative = _qualitative(kind, evaluated)
    probabilities = [child["probability"] for child in evaluated]
    complete = bool(evaluated) and all(value is not None for value in probabilities)
    if complete and node.get("independence_confirmed") is True:
        probability = (
            prod(probabilities)
            if kind == "and"
            else 1 - prod(1 - value for value in probabilities)
        )
        status = qualitative
        probability_status = "calculated"
    elif complete:
        probability = None
        status = "pending_dependency"
        probability_status = "pending_dependency"
    else:
        probability = None
        status = qualitative
        probability_status = "missing_data"
    return {
        "status": status,
        "qualitative_status": qualitative,
        "probability": probability,
        "probability_status": probability_status,
        "independence_confirmed": node.get("independence_confirmed") is True,
        "event_ref": node.get("event_ref"),
        "children": evaluated,
    }


def evaluate_fault_tree(tree, event_states=None):
    """Evaluate a normalized policy tree or a raw top-event node."""
    if not isinstance(tree, dict):
        raise ValueError("fault tree 必须是对象")
    root = tree.get("top_event", tree.get("root", tree))
    result = _evaluate(root, event_states or {})
    if tree is root:
        return result
    return {
        "fault_tree_id": tree.get("fault_tree_id", tree.get("id")),
        **result,
    }
