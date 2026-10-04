"""Deterministic explicit-policy resolver for RequiredCNS recommendations."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json

from ...domain.cns_inputs import normalize_required_cns
from ...domain.cns_service_registry import canonicalize_requirement_services
from ...domain.requirement_policy import (
    effective_context, empty_required_cns_recommendation,
    normalize_operation_context, normalize_requirement_policies,
)

#: RequiredCNS 的三个子系统字段与其 subsystem 码。
_SUBSYSTEM_FIELDS = (("communication", "C"), ("navigation", "N"), ("surveillance", "S"))


class OperationalContextRequiredCNSV2:
    algorithm_id = "operational_context_required_cns_v2"
    algorithm_version = "2.0"

    def __init__(self, parameters=None):
        self.parameters = deepcopy(parameters or {})

    def evaluate(self, required_cns, operation_context, policies, route_ids=None):
        current = normalize_required_cns(required_cns)
        context = normalize_operation_context(operation_context)
        policy_set = normalize_requirement_policies(policies)
        route_ids = sorted(set(str(item) for item in (route_ids or [])))
        input_value = [current, context, policy_set, route_ids, self.parameters]
        result = empty_required_cns_recommendation()
        scopes = [("project", None)] + [(route_id, route_id) for route_id in route_ids]
        recommended = deepcopy(current)
        recommended["source"] = "operational_context_required_cns_v2 recommendation"
        all_matched, all_not_matched, all_unknown = [], [], []
        all_conflicts, all_missing, provenance, scope_results = [], [], {}, {}
        for scope_name, route_id in scopes:
            base = (
                deepcopy(current.get("route_overrides", {}).get(route_id) or current["project_default"])
                if route_id else deepcopy(current["project_default"])
            )
            resolved = self._resolve_scope(
                base, effective_context(context, route_id), policy_set["items"], scope_name
            )
            all_matched.extend(resolved["matched"])
            all_not_matched.extend(resolved["not_matched"])
            all_unknown.extend(resolved["unknown"])
            all_conflicts.extend(resolved["conflicts"])
            all_missing.extend(resolved["missing_evidence"])
            provenance.update(resolved["field_provenance"])
            scope_results[scope_name] = resolved
            if resolved["matched"] and not resolved["conflicts"]:
                if route_id:
                    recommended.setdefault("route_overrides", {})[route_id] = resolved["requirements"]
                else:
                    recommended["project_default"] = resolved["requirements"]
        matched_ids = sorted({item["policy_id"] for item in all_matched})
        recommended = normalize_required_cns(recommended) if matched_ids and not all_conflicts else None
        if all_conflicts:
            status, result_status = "conflict", "pending_confirmation"
        elif all_unknown:
            status, result_status = "pending_confirmation", "pending_confirmation"
        elif not matched_ids:
            status, result_status = "not_configured", "not_calculated"
        elif recommended.get("status") != "passed":
            status, result_status = "pending_confirmation", "pending_confirmation"
            all_missing.append({
                "scope": "recommendation", "reason": "recommended_required_cns_incomplete",
            })
        else:
            status, result_status = "recommendation_ready", "passed"
        result.update({
            "status": status, "result_status": result_status,
            "algorithm_id": self.algorithm_id, "algorithm_version": self.algorithm_version,
            "parameters": deepcopy(self.parameters), "input_fingerprint": _fingerprint(input_value),
            "context_snapshot": deepcopy(context),
            "matched_policies": _unique_records(all_matched),
            "not_matched_policies": _unique_records(all_not_matched),
            "unknown_policies": _unique_records(all_unknown),
            "recommended_required_cns": recommended,
            "field_provenance": dict(sorted(provenance.items())),
            "conflicts": sorted(all_conflicts, key=lambda item: (item["scope"], item["field_path"])),
            "missing_evidence": _unique_records(all_missing),
            "current_vs_recommended_diff": _diff(current, recommended) if recommended else [],
            "route_recommendations": scope_results,
            "input_fingerprints": {
                "current_required_cns": _fingerprint(current),
                "operation_context": _fingerprint(context),
                "requirement_policies": _fingerprint(policy_set),
                "route_ids": _fingerprint(route_ids),
            },
            "proposal_only": True, "requires_user_adoption": True,
            "completeness_status": recommended.get("status") if recommended else "not_available",
            "semantics": "operational_context_driven_requirement_recommendation_not_automatic_regulatory_compliance",
        })
        return result

    def _resolve_scope(self, base, context, policies, scope):
        writes, matched, not_matched, unknown, missing = {}, [], [], [], []
        for policy in policies:
            record = {"scope": scope, "policy_id": policy["policy_id"], "version": policy["version"]}
            if not policy.get("confirmed"):
                unknown.append({**record, "reason": "policy_unconfirmed"})
                continue
            applicability = _match(policy["applicability"], context)
            if applicability["status"] == "unknown":
                unknown.append({**record, "reason": "applicability_unknown", "evidence": applicability["evidence"]})
                missing.extend({"scope": scope, "policy_id": policy["policy_id"], **item} for item in applicability["evidence"])
                continue
            if applicability["status"] == "not_matched":
                not_matched.append(record)
                continue
            matched.append(record)
            source = {
                "policy_id": policy["policy_id"], "version": policy["version"],
                "source_type": policy["source_type"], "source": policy["source"],
                "reference": policy["reference"], "clause": policy["clause"],
            }
            for path, value in _leaves(policy["requirements"]):
                writes.setdefault(path, []).append({"value": deepcopy(value), "provenance": source})
        conflicts, field_provenance, requirements = [], {}, deepcopy(base)
        for path, entries in sorted(writes.items()):
            distinct = {_stable(item["value"]) for item in entries}
            qualified_path = f"{scope}.{path}"
            field_provenance[qualified_path] = [item["provenance"] for item in entries]
            if len(distinct) > 1:
                conflicts.append({
                    "scope": scope, "field_path": path,
                    "values": [item["value"] for item in entries],
                    "policy_ids": [item["provenance"]["policy_id"] for item in entries],
                    "reason": "conflicting_explicit_policy_values_no_automatic_precedence",
                })
            else:
                _set_path(requirements, path.split("."), entries[0]["value"])
        if not conflicts:
            requirements, service_provenance = _attach_canonical_services(requirements, scope)
            field_provenance.update(service_provenance)
            requirements = normalize_required_cns({"project_default": requirements})["project_default"]
        return {
            "requirements": requirements, "matched": matched, "not_matched": not_matched,
            "unknown": unknown, "conflicts": conflicts, "missing_evidence": missing,
            "field_provenance": field_provenance,
        }


def _attach_canonical_services(requirements, scope):
    """给每个子系统要求补上正式 ``services``（显式优先，其次确定性派生）。

    这是"Required CNS recommendation 给出 services、Adopt 后 authoritative
    ``required_cns`` 必须携带正式 service requirements"的**唯一**接线点：

    * 只**新增** ``services`` 字段，绝不改写任何既有字段或数值；
    * 派生规则、半径/冗余与全向几何全部取自 ``cns_service_registry`` /
      ``cns_service_contract`` 的既有 canonical 常量；
    * 无法确定性派生时保持原样（绝不猜测，legacy 项目形状完全不变）；
    * 派生与显式声明都在 ``field_provenance`` 上留下可审计记录。
    """

    provenance = {}
    for name, code in _SUBSYSTEM_FIELDS:
        entry = requirements.get(name)
        if not isinstance(entry, dict):
            continue
        services, record = canonicalize_requirement_services(
            code, entry, field=f"required_cns.{name}",
        )
        if services is None or record is None:
            continue
        if "services" not in entry:
            entry["services"] = deepcopy(services)
            if len([key for key, item in services.items() if item.get("required") is True]) > 1:
                entry.setdefault("service_requirement_mode", "all_required")
        provenance[f"{scope}.{name}.services"] = [{
            "policy_id": None, "version": None,
            "source_type": record["source"], "source": record["source"],
            "reference": None, "clause": None,
            "derived": record["derived"], "reason": record["reason"],
            #: Round 29-H additive：派生依据与派生出的服务集合必须可审计 ——
            #: 否则"为什么 Surveillance 同时得到 RID 与 Radar"在结果里不可核查。
            "basis": record.get("basis"),
            "service_keys": deepcopy(record.get("service_keys") or []),
        }]
    return requirements, provenance


def _match(applicability, context):
    evidence = []
    for condition in applicability.get("all_of", []):
        field = condition["field"]
        actual = context.get(field) or {}
        if actual.get("status") != "confirmed" or actual.get("confirmed") is not True:
            evidence.append({"field": field, "reason": "context_missing_or_unconfirmed"})
            continue
        actual_value, expected = actual.get("value"), condition.get("value")
        operator = condition["operator"]
        matched = (
            actual_value == expected if operator == "eq"
            else actual_value in expected if operator == "in"
            else expected in actual_value if isinstance(actual_value, (str, list, tuple, set, dict))
            else False
        )
        if not matched:
            return {"status": "not_matched", "evidence": [{"field": field, "actual": actual_value, "operator": operator, "expected": expected}]}
    return {"status": "unknown" if evidence else "matched", "evidence": evidence}


def _leaves(value, prefix=""):
    for key in sorted(value):
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value[key], dict):
            yield from _leaves(value[key], path)
        else:
            yield path, deepcopy(value[key])


def _set_path(target, parts, value):
    current = target
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = deepcopy(value)


def _diff(current, recommended):
    before, after = dict(_leaves(current)), dict(_leaves(recommended))
    return [
        {"field_path": path, "current": before.get(path), "recommended": after.get(path)}
        for path in sorted(set(before) | set(after))
        if _stable(before.get(path)) != _stable(after.get(path))
    ]


def _unique_records(records):
    result, seen = [], set()
    for item in sorted(records, key=_stable):
        key = _stable(item)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _stable(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
