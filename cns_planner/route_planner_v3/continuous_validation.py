"""V3-C aggregator: geometry realization + every domain validator → one verdict.

``V3ContinuousValidator`` is the only place that turns the domain results into a
route-level status, and the mapping is fixed:

* a definite violation in **any** domain ⇒ ``failed`` (with structured violation
  intervals and ``replan_required=true``; nothing is repaired or replanned);
* missing / unconfirmed evidence ⇒ ``unresolved``;
* the V3-B source or the refined candidate is stale ⇒ ``not_ready``;
* a validation resource limit (sample budget or wall-clock budget) ⇒
  ``validation_incomplete`` -- **never** ``failed``;
* only when geometry, terrain, building, altitude and kinematics are all
  ``passed`` ⇒ ``validated_route``.

Even ``validated_route`` forces ``operational_route=false`` and
``cns_assessed=false``: V3-C produces a continuously validated route, not an
operational one, and CNS has not been assessed.
"""

from __future__ import annotations

from copy import deepcopy
import time

from .continuous_contracts import (
    CONTINUOUS_VALIDATION_RESULT_SCHEMA_VERSION, V3C_ALGORITHM_ID, V3C_ALGORITHM_VERSION,
    ACTIVE_V3C_DOMAINS, V3C_DISCLAIMER, V3C_DOMAINS, V3C_MODEL_SCOPE, V3C_RESULT_STATUSES,
    empty_domain_result, empty_v3_continuous_validation_result,
    normalize_v3_continuous_validation_problem, normalize_v3_continuous_validation_result,
    validation_fingerprint_components, violation_interval,
)
from .continuous_geometry import TURN_RADIUS_POLICY, realize_continuous_route
from .continuous_validators import (
    MetricRoute, validate_altitude_bounds, validate_buildings,
    validate_geometry, validate_kinematics, validate_terrain,
)


class V3ContinuousValidator:
    """Deterministic continuous-geometry and source-native route validation."""

    algorithm_id = V3C_ALGORITHM_ID
    algorithm_version = V3C_ALGORITHM_VERSION
    model_scope = V3C_MODEL_SCOPE
    domains = ACTIVE_V3C_DOMAINS

    def validate(self, problem):
        started = time.perf_counter()
        normalized = normalize_v3_continuous_validation_problem(problem)
        fingerprint_components = validation_fingerprint_components(normalized)
        normalized["fingerprint_components"] = fingerprint_components
        fingerprint = normalized["validation_fingerprint"]
        readiness = self._readiness(normalized)

        if readiness["status"] != "ready":
            return self._finalize(self._blocked_result(
                normalized, readiness, fingerprint, readiness["reason"],
                status=readiness["result_status"],
            ), started)

        policy = normalized["policy"]
        evidence = normalized["domain_evidence"] or {}
        refinement = normalized["refinement"] or {}
        limits = normalized["aircraft_motion_limits"] or {}

        collected = int(evidence.get("sample_count") or 0)
        budget_limit = int(policy["max_validation_samples"])
        if collected > budget_limit:
            return self._finalize(self._resource_limited_result(
                normalized, readiness, fingerprint,
                f"源证据样本数 {collected} 超过 max_validation_samples={budget_limit}："
                "返回 validation_incomplete（资源上限不是 failed，也不构成不安全结论）",
                sample_count=collected, budget_limit=budget_limit, runtime_s=None,
            ), started)

        realization = realize_continuous_route(
            metric_points=refinement.get("metric_projection") or [],
            altitudes=[state.get("altitude_egm2008_m") for state in refinement.get("state_path") or []],
            min_turn_radius_m=policy.get("aircraft_min_turn_radius_m"),
            curve_chord_error_m=policy.get("curve_chord_error_m"),
            route_id=normalized.get("route_id"),
            frame=refinement.get("frame") or {},
        )
        route = realization["route"]
        domains = {}
        if realization["status"] != "realized":
            domains["geometry"] = _failed_geometry(route, realization)
            for domain in V3C_DOMAINS:
                if domain == "geometry":
                    continue
                domains[domain] = _airspace_not_applicable() if domain == "airspace" else _skipped(domain, "geometry_realization_failed")
            result = self._assemble(
                normalized, readiness, fingerprint, fingerprint_components, route, domains,
                sample_count=collected, runtime_s=time.perf_counter() - started,
                realization=realization,
            )
            return self._finalize(result, started)

        metric_route = MetricRoute(route)
        to_geographic = _geographic_converter(evidence)
        domains["geometry"] = validate_geometry(route, policy=policy)
        domains["airspace"] = _airspace_not_applicable()
        domains["terrain"] = validate_terrain(
            metric_route, evidence=evidence.get("terrain") or {}, policy=policy,
            to_geographic=to_geographic,
        )
        domains["building"] = validate_buildings(
            metric_route, evidence=evidence.get("buildings") or {}, policy=policy,
            to_geographic=to_geographic,
        )
        domains["altitude"] = validate_altitude_bounds(route, policy=policy)
        domains["kinematics"] = validate_kinematics(
            route, policy=policy, aircraft_motion_limits=limits,
        )
        result = self._assemble(
            normalized, readiness, fingerprint, fingerprint_components, route, domains,
            sample_count=collected, runtime_s=time.perf_counter() - started,
            realization=realization,
        )
        return self._finalize(result, started)

    # ------------------------------------------------------------------ readiness

    def _readiness(self, problem):
        refinement = problem.get("refinement") or {}
        policy = problem.get("policy") or {}
        evidence = problem.get("domain_evidence") or {}
        entry = {
            "status": "ready",
            "reason": None,
            "result_status": "not_ready",
            "refinement_source": {
                "status": refinement.get("status"),
                "current_applicability": refinement.get("current_applicability"),
                "refinement_fingerprint": refinement.get("refinement_fingerprint"),
                "metric_projection_count": len(refinement.get("metric_projection") or []),
                "semantics": "v3c_only_validates_a_selected_current_v3b_refined_candidate",
            },
            "policy": {
                "status": policy.get("validation_policy_status") or policy.get("status"),
                "curve_chord_error_m": policy.get("curve_chord_error_m"),
                "min_turn_radius_m": policy.get("aircraft_min_turn_radius_m"),
                "missing_parameters": list(
                    policy.get("validation_missing_parameters")
                    or policy.get("missing_parameters") or []
                ),
            },
            "evidence_available": bool(evidence),
            "reasons": [],
        }
        reasons = []
        if refinement.get("status") != "refined_candidate":
            entry["result_status"] = "not_ready"
            reasons.append(
                f"V3-C 只能在 V3-B refined_candidate 上运行，当前 refinement status="
                f"{refinement.get('status') or 'unknown'}"
            )
        if str(refinement.get("current_applicability") or "unknown") != "current":
            entry["result_status"] = "not_ready"
            reasons.append(
                "V3-B refinement 不是 current（stale 的 refined candidate 不得进入 V3-C，必须重跑 V3-B）"
            )
        if len(refinement.get("metric_projection") or []) < 2:
            entry["result_status"] = "not_ready"
            reasons.append("V3-B refined candidate 缺少 metric 轨迹（至少两个点）")
        if policy.get("status") != "confirmed":
            entry["result_status"] = "not_ready"
            reasons.extend(policy.get("validation_reasons") or policy.get("reasons")
                           or ["V3-C validation policy 未确认"])
        if policy.get("aircraft_min_turn_radius_m") is None:
            entry["result_status"] = "not_ready"
            reasons.append(
                "缺少显式 aircraft_min_turn_radius_m：V3-C 圆弧 fillet 的 R 必须有工程依据，"
                "禁止猜默认转弯半径"
            )
        expected = problem.get("expected_validation_fingerprint")
        if expected is not None and expected != problem.get("validation_fingerprint"):
            entry["result_status"] = "not_ready"
            reasons.append(
                "expected_validation_fingerprint 与当前 refinement/policy/curve tolerance/source/"
                "CRS/validator versions 不一致：validation 已 stale，必须重跑"
            )
        if reasons:
            entry["status"] = "blocked"
            entry["reasons"] = reasons
            entry["reason"] = "V3-C readiness blocked：" + "；".join(reasons)
        return entry

    # ------------------------------------------------------------------ assembly

    def _assemble(self, problem, readiness, fingerprint, components, route, domains,
                  *, sample_count, runtime_s, realization):
        result = empty_v3_continuous_validation_result()
        policy = problem["policy"]
        refinement = problem["refinement"] or {}
        result.update({
            "problem_id": problem.get("problem_id"),
            "route_id": problem.get("route_id"),
            "experiment_id": refinement.get("experiment_id"),
            "refinement_id": refinement.get("refinement_id"),
            "readiness": readiness,
            "validation_fingerprint": fingerprint,
            "fingerprint_components": dict(components),
            "refinement_fingerprint": refinement.get("refinement_fingerprint"),
            "continuous_route": route,
            "frame": deepcopy(refinement.get("frame")),
            "source_audit": deepcopy(problem.get("source_audit") or {}),
            "effective_policy": deepcopy(policy),
            "domains": domains,
            "domain_statuses": {name: domains[name]["status"] for name in V3C_DOMAINS if name in domains},
            "violations": [
                item for name in V3C_DOMAINS for item in (domains.get(name) or {}).get("violations") or []
            ],
            "unresolved_evidence": [
                item for name in V3C_DOMAINS for item in (domains.get(name) or {}).get("unresolved") or []
            ],
            "min_margins": _min_margins(domains),
            "kinematics": _kinematics(domains, policy),
            "resource_limits": {
                "max_validation_samples": policy.get("max_validation_samples"),
                "sample_count": sample_count,
                "max_runtime_s": policy.get("max_runtime_s"),
                "runtime_s": round(float(runtime_s), 6),
                "resource_limited": False,
                "resource_limit_reason": None,
            },
            "statistics": _statistics(domains, route),
            "curve_error": deepcopy(realization.get("curve_error")),
        })
        status, reason = _route_status(domains, policy)
        result["status"] = status
        result["reason"] = reason
        result["verdicts"] = {
            "all_domains_passed": all(
                (domains.get(name) or {}).get("status") == "passed" for name in ACTIVE_V3C_DOMAINS
            ),
            "replan_required": status == "failed",
            "automatic_repair_performed": False,
            "automatic_replan_performed": False,
            "validated_route_is_operational_route": False,
            "cns_assessed": False,
            "unknown_is_never_safe": True,
        }
        result["operational_route"] = False
        result["cns_assessed"] = False
        return result

    def _blocked_result(self, problem, readiness, fingerprint, reason, *, status="not_ready"):
        result = empty_v3_continuous_validation_result(status)
        refinement = problem["refinement"] or {}
        result.update({
            "problem_id": problem.get("problem_id"),
            "route_id": problem.get("route_id"),
            "experiment_id": refinement.get("experiment_id"),
            "refinement_id": refinement.get("refinement_id"),
            "reason": reason,
            "readiness": readiness,
            "validation_fingerprint": fingerprint,
            "fingerprint_components": dict(problem.get("fingerprint_components") or {}),
            "refinement_fingerprint": refinement.get("refinement_fingerprint"),
            "frame": deepcopy(refinement.get("frame")),
            "source_audit": deepcopy(problem.get("source_audit") or {}),
            "effective_policy": deepcopy(problem.get("policy")),
            "continuous_route": None,
            "domains": {
                name: (_airspace_not_applicable() if name == "airspace" else _skipped(name, "validation_not_run_readiness_blocked")) for name in V3C_DOMAINS
            },
            "domain_statuses": {name: "skipped" for name in V3C_DOMAINS},
            "min_margins": _min_margins({}),
            "kinematics": _kinematics({}, problem.get("policy") or {}),
        })
        result["verdicts"]["all_domains_passed"] = False
        result["verdicts"]["replan_required"] = False
        result["violations"] = []
        return result

    def _resource_limited_result(self, problem, readiness, fingerprint, reason, *,
                                 sample_count, budget_limit, runtime_s):
        result = self._blocked_result(
            problem, readiness, fingerprint, reason, status="validation_incomplete",
        )
        result["resource_limits"] = {
            "max_validation_samples": budget_limit,
            "sample_count": sample_count,
            "max_runtime_s": (problem.get("policy") or {}).get("max_runtime_s"),
            "runtime_s": None if runtime_s is None else round(float(runtime_s), 6),
            "resource_limited": True,
            "resource_limit_reason": reason,
        }
        result["semantics"]["resource_limited_not_failed"] = True
        result["semantics"]["resource_limited_not_infeasible"] = True
        return result

    def _finalize(self, result, started):
        result = normalize_v3_continuous_validation_result(result)
        result["algorithm_id"] = self.algorithm_id
        result["algorithm_version"] = self.algorithm_version
        result["model_scope"] = self.model_scope
        result["schema_version"] = CONTINUOUS_VALIDATION_RESULT_SCHEMA_VERSION
        result["disclaimer"] = V3C_DISCLAIMER
        result["operational_route"] = False
        result["cns_assessed"] = False
        if result.get("status") not in V3C_RESULT_STATUSES:
            result["status"] = "not_ready"
        runtime = float((result.get("resource_limits") or {}).get("runtime_s") or 0.0)
        if runtime <= 0.0:
            runtime = time.perf_counter() - started
        result["resource_limits"]["runtime_s"] = round(runtime, 6)
        policy = result.get("effective_policy") or {}
        max_runtime = policy.get("max_runtime_s")
        if (
            max_runtime is not None and result["status"] not in ("validation_incomplete",)
            and result["resource_limits"]["runtime_s"] > float(max_runtime)
        ):
            result = _downgrade_to_resource_limited(
                result,
                f"验证耗时 {result['resource_limits']['runtime_s']}s 超过 max_runtime_s={max_runtime}："
                "返回 validation_incomplete（资源上限不是 failed）",
            )
        semantics = result.setdefault("semantics", {})
        semantics.update({
            "scope": self.model_scope,
            "continuous_geometry_realization": True,
            "source_native_validation": True,
            "unknown_is_never_safe": True,
            "never_repairs_or_replans": True,
            "validated_route_is_not_operational_route": True,
            "cns_not_assessed": True,
            "turn_radius_policy": TURN_RADIUS_POLICY,
            "validation_fingerprint_components": list(components_keys()),
        })
        return result


def components_keys():
    from .continuous_contracts import VALIDATION_FINGERPRINT_COMPONENTS
    return VALIDATION_FINGERPRINT_COMPONENTS


def _downgrade_to_resource_limited(result, reason):
    result["status"] = "validation_incomplete"
    result["reason"] = reason
    result["resource_limits"]["resource_limited"] = True
    result["resource_limits"]["resource_limit_reason"] = reason
    result["verdicts"]["replan_required"] = False
    result["verdicts"]["all_domains_passed"] = False
    return result


def _geographic_converter(evidence):
    """Optional metric → geographic converter taken from the recorded frame."""

    converter = (evidence or {}).get("to_geographic")
    return converter if callable(converter) else None


def _failed_geometry(route, realization):
    result = empty_domain_result("geometry", "failed")
    result["evaluated"] = True
    result["reason"] = (route or {}).get("reason") or "turn_realization_failed"
    result["violations"] = [violation_interval(
        domain="geometry", reason_id=result["reason"],
        required="realized_C1_geometry", observed="turn_realization_failed",
        evidence={
            "turn_failures": deepcopy(realization.get("turn_failures") or []),
            "turn_realization_reasons": list((route or {}).get("turn_realization_reasons") or []),
            "radius_never_reduced": True,
            "semantics": "replan_required_no_automatic_repair",
        },
    )]
    result["failed_interval_count"] = 1
    result["evidence"] = {
        "turn_realization_status": (route or {}).get("turn_realization_status"),
        "primitives_built": len((route or {}).get("primitives") or []),
        "semantics": (route or {}).get("semantics") or {},
    }
    result["semantics"] = {"replan_required": True, "radius_never_reduced": True}
    return result


def _skipped(domain, reason):
    result = empty_domain_result(domain, "skipped")
    result["reason"] = reason
    return result


def _airspace_not_applicable():
    result = empty_domain_result("airspace", "skipped")
    result.update({
        "evaluated": False,
        "applicability": "not_applicable",
        "reason": "display_only_airspace_not_used_for_route_constraints",
        "semantics": {
            "applicability": "not_applicable",
            "layer_role": "display_only_reference_layer",
            "not_a_planning_input": True,
        },
    })
    return result


def _min_margins(domains):
    airspace = domains.get("airspace") or {}
    terrain = domains.get("terrain") or {}
    building = domains.get("building") or {}
    altitude = domains.get("altitude") or {}
    kinematics = domains.get("kinematics") or {}
    airspace_evidence = airspace.get("evidence") or {}
    building_evidence = building.get("evidence") or {}
    altitude_evidence = altitude.get("evidence") or {}
    kinematics_evidence = kinematics.get("evidence") or {}
    return {
        "airspace_horizontal_m": airspace_evidence.get("horizontal_clearance_margin_m"),
        "terrain_vertical_m": terrain.get("minimum_margin"),
        "building_horizontal_m": building_evidence.get("minimum_horizontal_distance_m"),
        "building_vertical_m": building.get("minimum_margin"),
        "altitude_lower_m": (
            None if altitude_evidence.get("min_altitude_egm2008_m") is None
            or altitude_evidence.get("observed_min_z_egm2008_m") is None
            else float(altitude_evidence["observed_min_z_egm2008_m"])
            - float(altitude_evidence["min_altitude_egm2008_m"])
        ),
        "altitude_upper_m": (
            None if altitude_evidence.get("max_altitude_egm2008_m") is None
            or altitude_evidence.get("observed_max_z_egm2008_m") is None
            else float(altitude_evidence["max_altitude_egm2008_m"])
            - float(altitude_evidence["observed_max_z_egm2008_m"])
        ),
        "turn_radius_m": (
            None if kinematics_evidence.get("minimum_turn_radius_observed_m") is None
            or kinematics_evidence.get("required_minimum_turn_radius_m") is None
            else float(kinematics_evidence["minimum_turn_radius_observed_m"])
            - float(kinematics_evidence["required_minimum_turn_radius_m"])
        ),
        "climb_gradient_margin": (
            None if kinematics_evidence.get("max_allowed_climb_gradient") is None
            else float(kinematics_evidence["max_allowed_climb_gradient"])
            - float(kinematics_evidence.get("max_climb_gradient_observed") or 0.0)
        ),
        "descent_gradient_margin": (
            None if kinematics_evidence.get("max_allowed_descent_gradient") is None
            else float(kinematics_evidence["max_allowed_descent_gradient"])
            - abs(float(kinematics_evidence.get("max_descent_gradient_observed") or 0.0))
        ),
    }


def _kinematics(domains, policy):
    evidence = (domains.get("kinematics") or {}).get("evidence") or {}
    return {
        "minimum_turn_radius_observed_m": evidence.get("minimum_turn_radius_observed_m"),
        "required_minimum_turn_radius_m": evidence.get("required_minimum_turn_radius_m"),
        "max_climb_gradient_observed": evidence.get("max_climb_gradient_observed"),
        "max_descent_gradient_observed": evidence.get("max_descent_gradient_observed"),
        "max_allowed_climb_gradient": evidence.get("max_allowed_climb_gradient"),
        "max_allowed_descent_gradient": evidence.get("max_allowed_descent_gradient"),
        "tangent_heading_continuity_verified": bool(
            evidence.get("tangent_heading_continuity_verified", False)
        ),
        "self_intersection_diagnostic": evidence.get("self_intersection_diagnostic"),
        "self_intersection_is_failure": bool(policy.get("self_intersection_is_failure", False)),
        "turn_verification": evidence.get("turn_verification"),
    }


def _statistics(domains, route):
    analytic = ((route or {}).get("horizontal_geometry") or {}).get("analytic") or {}
    linearized = ((route or {}).get("horizontal_geometry") or {}).get("linearized") or {}
    statuses = [(domains.get(name) or {}).get("status") for name in V3C_DOMAINS]
    return {
        "domain_count": len(V3C_DOMAINS),
        "passed_domain_count": sum(1 for status in statuses if status == "passed"),
        "failed_domain_count": sum(1 for status in statuses if status == "failed"),
        "unresolved_domain_count": sum(1 for status in statuses if status == "unresolved"),
        "skipped_domain_count": sum(1 for status in statuses if status == "skipped"),
        "violation_interval_count": sum(
            len((domains.get(name) or {}).get("violations") or []) for name in V3C_DOMAINS
        ),
        "unresolved_interval_count": sum(
            len((domains.get(name) or {}).get("unresolved") or []) for name in V3C_DOMAINS
        ),
        "primitive_count": analytic.get("primitive_count") or 0,
        "arc_count": analytic.get("arc_count") or 0,
        "linearized_point_count": linearized.get("point_count") or 0,
    }


def _route_status(domains, policy):
    """Fixed status mapping: violation ⇒ failed, missing evidence ⇒ unresolved."""

    reasons = []
    for domain in ACTIVE_V3C_DOMAINS:
        entry = domains.get(domain) or {}
        status = entry.get("status")
        if status == "failed":
            reasons.append(f"{domain}:{entry.get('reason') or 'violation'}")
    if reasons:
        return "failed", "确定违反：" + "; ".join(reasons) + "（不自动修路/不自动 replan）"
    unresolved = [
        f"{domain}:{entry.get('reason') or 'unresolved'}"
        for domain in ACTIVE_V3C_DOMAINS
        for entry in [domains.get(domain) or {}]
        if entry.get("status") == "unresolved"
    ]
    if unresolved:
        return "unresolved", "证据不足：" + "; ".join(unresolved)
    if not all((domains.get(domain) or {}).get("status") == "passed" for domain in ACTIVE_V3C_DOMAINS):
        return "unresolved", "并非所有 domain 都 passed，且不存在确定违反：按证据不足处理"
    return "validated_route", (
        "连续几何实现 + confirmed 源几何/原生栅格验证全部 passed；"
        "仍然不是 operational route，CNS 尚未评估"
    )


def validate_continuous_route(problem):
    """Convenience wrapper around :class:`V3ContinuousValidator`."""

    return V3ContinuousValidator().validate(problem)


__all__ = ["V3ContinuousValidator", "validate_continuous_route"]
