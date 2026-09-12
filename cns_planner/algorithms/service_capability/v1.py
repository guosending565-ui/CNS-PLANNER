"""Technology-aware static CNS service capability baseline."""

from __future__ import annotations

from hashlib import sha256
from copy import deepcopy
import json
import math

from ...safety.service_state import evaluate_required_performance


SUBSYSTEM_NAMES = {"C": "communication", "N": "navigation", "S": "surveillance"}
SAMPLE_STATES = (
    "meets_under_model", "does_not_meet_under_model", "unknown",
    "not_applicable", "unsupported_model",
)
NON_SITE_NAVIGATION = {"gnss", "gnss_rtk", "inertial", "visual", "hybrid"}


class CNSServiceCapabilityV1:
    algorithm_id = "cns_service_capability_v1"
    algorithm_version = "1.0"

    def __init__(self, parameters=None):
        self.parameters = dict(parameters or {})

    @classmethod
    def empty(cls, status="not_calculated"):
        return {
            "status": status, "algorithm_id": cls.algorithm_id,
            "algorithm_version": cls.algorithm_version,
            "model_scope": "static_capability", "parameters": {},
            "input_fingerprint": None, "route_count": 0, "routes": [],
            "maturity": "engineering_baseline", "not_evaluated": _not_evaluated(),
        }

    def evaluate(self, coverage_3d, required_cns, aircraft_profile, existing_facilities, device_catalog):
        devices = build_provider_devices(device_catalog, existing_facilities)
        routes = []
        for route in (coverage_3d or {}).get("routes") or []:
            route_id = str(route.get("route_id") or "")
            requirements = ((required_cns or {}).get("route_overrides") or {}).get(route_id) or (required_cns or {}).get("project_default") or {}
            subsystems = []
            for code, name in SUBSYSTEM_NAMES.items():
                geometry = next((item for item in route.get("subsystems") or [] if item.get("subsystem") == code), None)
                subsystems.append(self._subsystem(code, route, geometry, requirements.get(name) or {}, aircraft_profile or {}, devices))
            routes.append({
                "route_id": route_id, "route_length_m": route.get("route_length_m"),
                "status": _aggregate([item["status"] for item in subsystems]),
                "subsystems": subsystems,
            })
        fingerprint = sha256(json.dumps([
            coverage_3d or {}, required_cns or {}, aircraft_profile,
            existing_facilities or {}, device_catalog or {}, self.parameters,
        ], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return {
            "status": _aggregate([item["status"] for item in routes]) if routes else "missing_data",
            "algorithm_id": self.algorithm_id, "algorithm_version": self.algorithm_version,
            "model_scope": "static_capability", "parameters": dict(self.parameters),
            "input_fingerprint": fingerprint, "route_count": len(routes), "routes": routes,
            "semantics": "static_capability_not_runtime_availability",
            "maturity": "engineering_baseline", "not_evaluated": _not_evaluated(),
        }

    def _subsystem(self, code, route, geometry, required, aircraft, devices):
        total = float(route.get("route_length_m") or 0.0)
        base = {
            "subsystem": code, "route_length_m": total,
            "model_scope": "static_capability", "semantics": "not_runtime_availability",
        }
        if required.get("required") is False:
            return {**base, "status": "not_applicable", "samples": [], **_summary(total, [], "not_applicable")}
        if required.get("required") is not True or required.get("status") == "pending_confirmation":
            return {**base, "status": "unknown", "samples": [], "reasons": ["RequiredCNS 未确认"], **_summary(total, [], "unknown")}
        capability = aircraft.get(SUBSYSTEM_NAMES[code]) or {}
        match_required = _without_redundancy(required)
        aircraft_ok, aircraft_evidence = evaluate_required_performance(match_required, capability, require_capability=True)
        if code == "N" and _technology(capability) in NON_SITE_NAVIGATION:
            status = "meets_under_model" if aircraft_ok is True else "does_not_meet_under_model" if aircraft_ok is False else "unknown"
            reason = "已确认的机载导航性能满足 RequiredCNS" if aircraft_ok is True else "机载导航性能不满足 RequiredCNS" if aircraft_ok is False else "机载导航性能证据不足"
            samples = [evaluate_capability_point(code, item, required, aircraft, devices) for item in _route_samples(route, geometry)]
            return {**base, "status": status, "model_scope": "aircraft_declared_performance", "samples": samples, **_summary(total, samples)}
        samples = []
        for sample in _route_samples(route, geometry):
            samples.append(evaluate_capability_point(code, sample, required, aircraft, devices))
        summary = _summary(total, samples)
        status = _summary_status(summary, samples)
        return {**base, "status": status, "samples": samples, **summary}

    def _sample(self, code, sample, required, capability, aircraft_ok, aircraft_evidence, devices):
        return _evaluate_site_capability_point(
            code, sample, required, capability, aircraft_ok, aircraft_evidence, devices
        )


def evaluate_capability_point(code, sample, required, aircraft, devices):
    """Evaluate one P7 geometry point with the exact P8 static capability rules."""
    if required.get("required") is False:
        return _sample_result(sample, "not_applicable", ["该分系统不适用"])
    if required.get("required") is not True or required.get("status") == "pending_confirmation":
        return _sample_result(sample, "unknown", ["RequiredCNS 未确认"])
    capability = (aircraft or {}).get(SUBSYSTEM_NAMES[code]) or {}
    aircraft_ok, aircraft_evidence = evaluate_required_performance(
        _without_redundancy(required), capability, require_capability=True
    )
    if code == "N" and _technology(capability) in NON_SITE_NAVIGATION:
        status = "meets_under_model" if aircraft_ok is True else "does_not_meet_under_model" if aircraft_ok is False else "unknown"
        reason = "已确认的机载导航性能满足 RequiredCNS" if aircraft_ok is True else "机载导航性能不满足 RequiredCNS" if aircraft_ok is False else "机载导航性能证据不足"
        return _sample_result(sample, status, [reason], [{"kind": "aircraft_navigation", **aircraft_evidence}])
    return _evaluate_site_capability_point(
        code, sample, required, capability, aircraft_ok, aircraft_evidence, devices
    )


def _evaluate_site_capability_point(code, sample, required, capability, aircraft_ok, aircraft_evidence, devices):
        evidence = []
        if sample.get("covered") is False:
            return _sample_result(sample, "does_not_meet_under_model", ["P7 三维几何覆盖门控未通过"], evidence)
        if sample.get("covered") is not True:
            return _sample_result(sample, "unknown", ["P7 三维几何覆盖状态未知"], evidence)
        type_evaluations = [
            _provider_type_evaluation(required, provider, devices.get(str(provider.get("device_id"))))
            for provider in sample.get("providers") or []
        ]
        if type_evaluations and all(item["status"] == "does_not_meet_under_model" for item in type_evaluations):
            return _sample_result(sample, "does_not_meet_under_model", ["提供者技术/类型与 RequiredCNS 不兼容"], evidence, type_evaluations)
        if not type_evaluations or not any(item["status"] == "meets_under_model" for item in type_evaluations):
            return _sample_result(sample, "unknown", ["提供者技术/类型证据不足"], evidence, type_evaluations)
        evidence.append({"kind": "aircraft_capability", **aircraft_evidence})
        if aircraft_ok is False:
            return _sample_result(sample, "does_not_meet_under_model", ["机载能力与 RequiredCNS 不兼容"], evidence, type_evaluations)
        if aircraft_ok is None:
            return _sample_result(sample, "unknown", ["机载能力或关键性能证据不足"], evidence, type_evaluations)
        evaluations = []
        for provider, type_evaluation in zip(sample.get("providers") or [], type_evaluations):
            if type_evaluation["status"] != "meets_under_model":
                evaluations.append(type_evaluation)
                continue
            evaluations.append(
                _evaluate_provider(code, required, capability, provider, devices.get(str(provider.get("device_id"))))
            )
        evidence.append({
            "kind": "provider_multiplicity", "provider_count": len(evaluations),
            "independent_redundancy_count": _independent_count(evaluations),
            "independence_claimed": _independent_count(evaluations) is not None,
        })
        required_redundancy = (required.get("performance") or {}).get("min_redundancy") or required.get("redundancy") or 1
        meets = [item for item in evaluations if item["status"] == "meets_under_model"]
        unknown = [item for item in evaluations if item["status"] in ("unknown", "unsupported_model")]
        if int(required_redundancy) > 1:
            independent = _independent_count(meets)
            if independent is None:
                status, reasons = "unknown", ["提供者多重度已知，但缺少独立性证据"]
            elif independent >= int(required_redundancy):
                status, reasons = "meets_under_model", ["已确认的独立提供者数满足 RequiredCNS"]
            elif unknown:
                status, reasons = "unknown", ["独立冗余是否满足尚不可确认"]
            else:
                status, reasons = "does_not_meet_under_model", ["已确认的独立提供者数不足"]
        elif meets:
            status, reasons = "meets_under_model", ["至少一个提供者在已声明模型下满足 RequiredCNS"]
        elif unknown:
            status, reasons = "unknown" if any(item["status"] == "unknown" for item in unknown) else "unsupported_model", ["提供者模型或关键证据不足"]
        else:
            status, reasons = "does_not_meet_under_model", ["已覆盖提供者在已声明模型下不满足 RequiredCNS"]
        return _sample_result(sample, status, reasons, evidence, evaluations)


def free_space_link_budget(slant_distance_m, parameters):
    """Evaluate the ITU-R P.525 free-space reference equation."""
    distance = _finite(slant_distance_m, "slant_distance_m")
    if distance <= 0:
        raise ValueError("slant_distance_m 必须大于零，不得静默 clamp")
    required = (
        "frequency_hz", "tx_power_dbm", "tx_gain_dbi", "tx_loss_db",
        "rx_gain_dbi", "rx_loss_db", "rx_sensitivity_dbm", "required_margin_db",
    )
    missing = [name for name in required if parameters.get(name) in (None, "")]
    if missing:
        raise ValueError("链路预算缺少参数：" + ", ".join(missing))
    values = {name: _finite(parameters[name], name) for name in required}
    if values["frequency_hz"] <= 0:
        raise ValueError("frequency_hz 必须大于零")
    path_loss = 20 * math.log10(distance) + 20 * math.log10(values["frequency_hz"]) - 147.55221677811664
    received = (
        values["tx_power_dbm"] + values["tx_gain_dbi"] - values["tx_loss_db"]
        + values["rx_gain_dbi"] - values["rx_loss_db"] - path_loss
    )
    available_margin = received - values["rx_sensitivity_dbm"]
    link_margin = available_margin - values["required_margin_db"]
    return {
        "status": "meets_under_model" if link_margin >= 0 else "does_not_meet_under_model",
        "model_scope": "free_space_reference", "slant_distance_m": distance,
        "frequency_hz": values["frequency_hz"], "path_loss_db": path_loss,
        "received_power_dbm": received, "available_margin_db": available_margin,
        "required_margin_db": values["required_margin_db"], "link_margin_db": link_margin,
        "not_evaluated": {name: "not_evaluated" for name in ("line_of_sight", "diffraction", "interference", "load", "handover")},
    }


def _evaluate_provider(code, required, aircraft, geometry_provider, device):
    base = {
        "facility_id": geometry_provider.get("facility_id"),
        "device_id": geometry_provider.get("device_id"),
        "slant_distance_m": geometry_provider.get("slant_distance_m"),
    }
    if not isinstance(device, dict):
        return {**base, "status": "unknown", "reasons": ["设备目录条目缺失"], "evidence": []}
    interface = _interface_compatibility(code, aircraft, device)
    if interface is not True:
        return {**base, "status": "unknown" if interface is None else "does_not_meet_under_model", "reasons": ["机载与地面提供者接口证据不足" if interface is None else "机载与地面提供者接口不兼容"], "evidence": []}
    model = device.get("service_model") or {}
    if model.get("confirmed") is not True:
        return {**base, "status": "unknown", "model_family": model.get("model_family"), "reasons": ["ServiceModelSpec 未确认"], "evidence": []}
    family = model.get("model_family")
    base.update({
        "model_family": family, "model_version": model.get("version"),
        "technology": model.get("technology"), "source": model.get("source"),
        "assumptions": list(model.get("assumptions") or []),
        "limitations": list(model.get("limitations") or []),
        "references": list(model.get("references") or []),
    })
    if family == "unsupported":
        return {**base, "status": "unsupported_model", "model_family": family, "reasons": ["该服务模型明确不支持"], "evidence": []}
    technology = _technology(device)
    model_technology = str(model.get("technology") or "unknown")
    if technology != "unknown" and model_technology != "unknown" and technology != model_technology:
        return {**base, "status": "does_not_meet_under_model", "model_family": family, "reasons": ["ServiceModelSpec technology 与设备不一致"], "evidence": []}
    actual = _declared_actual(device, model)
    performance_ok, performance_evidence = evaluate_required_performance(_without_redundancy(required), actual)
    evidence = [{"kind": "required_performance", **performance_evidence}]
    if family == "free_space_link_budget":
        if code != "C":
            return {**base, "status": "unsupported_model", "model_family": family, "reasons": ["自由空间链路预算 V1 仅实现通信 C"], "evidence": evidence}
        try:
            link = free_space_link_budget(base["slant_distance_m"], model.get("parameters") or {})
        except (TypeError, ValueError) as exc:
            return {**base, "status": "unknown", "model_family": family, "model_scope": "free_space_reference", "reasons": [str(exc)], "evidence": evidence}
        evidence.append({"kind": "link_budget", **link})
        reference_only = technology in ("4g", "5g")
        if performance_ok is None:
            status = "unknown"
        elif performance_ok is False or link["status"] == "does_not_meet_under_model":
            status = "does_not_meet_under_model"
        else:
            status = "meets_under_model"
        return {
            **base, "status": status, "model_family": family,
            "model_scope": "free_space_reference", "reference_only": reference_only,
            "reasons": (["4G/5G 仅作自由空间参考，不代表 3GPP channel 或真实蜂窝覆盖"] if reference_only else []),
            "evidence": evidence, "link_budget": link,
            "independence_confirmed": bool((model.get("parameters") or {}).get("independence_confirmed", False)),
            "independence_group": (model.get("parameters") or {}).get("independence_group"),
        }
    if family in ("declared_performance", "external_service"):
        status = "meets_under_model" if performance_ok is True else "does_not_meet_under_model" if performance_ok is False else "unknown"
        return {
            **base, "status": status, "model_family": family,
            "model_scope": family, "reasons": [], "evidence": evidence,
            "independence_confirmed": bool((model.get("parameters") or {}).get("independence_confirmed", False)),
            "independence_group": (model.get("parameters") or {}).get("independence_group"),
        }
    return {**base, "status": "unsupported_model", "model_family": family, "reasons": ["未实现的 model_family"], "evidence": evidence}


def _provider_type_evaluation(required, geometry_provider, device):
    base = {
        "facility_id": geometry_provider.get("facility_id"),
        "device_id": geometry_provider.get("device_id"),
        "slant_distance_m": geometry_provider.get("slant_distance_m"),
        "stage": "provider_type_compatibility",
    }
    if not isinstance(device, dict):
        return {**base, "status": "unknown", "reasons": ["设备目录条目缺失"], "evidence": []}
    expected_type, actual_type = required.get("type") or {}, device.get("type") or {}
    for key, expected in expected_type.items():
        if expected in (None, "", "unknown", []):
            continue
        observed = actual_type.get(key)
        if observed in (None, "", "unknown", []):
            return {**base, "status": "unknown", "reasons": [f"缺少提供者类型字段 {key}"], "evidence": []}
        if key == "interfaces":
            if not set(expected).issubset(set(observed)):
                return {**base, "status": "does_not_meet_under_model", "reasons": ["provider interfaces 不满足"], "evidence": []}
        elif observed != expected:
            return {**base, "status": "does_not_meet_under_model", "reasons": [f"provider {key} 不匹配"], "evidence": []}
    return {**base, "status": "meets_under_model", "reasons": [], "evidence": [{"required_type": expected_type, "provider_type": actual_type}]}


def _declared_actual(device, model):
    parameters = model.get("parameters") or {}
    return {
        "type": parameters.get("type") or device.get("type") or {},
        "performance": parameters.get("performance") or device.get("performance") or {},
        "redundancy": parameters.get("redundancy", device.get("redundancy")),
        "confirmed": True, "status": "confirmed", "capabilities": ["service_model"],
    }


def build_provider_devices(catalog, facilities):
    result = {str(item.get("device_id")): deepcopy(item) for item in (catalog or {}).get("items") or []}
    for facility in (facilities or {}).get("items") or []:
        for installed in facility.get("devices") or []:
            identifier = str(installed.get("device_id") or "")
            if not identifier:
                continue
            current = result.get(identifier, {})
            merged = {**deepcopy(installed), **current}
            installed_model = installed.get("service_model") or {}
            if installed_model.get("status") != "missing_data":
                merged["service_model"] = deepcopy(installed_model)
            result[identifier] = merged
    return result


def _without_redundancy(required):
    value = deepcopy(required or {})
    value.pop("redundancy", None)
    if isinstance(value.get("performance"), dict):
        value["performance"].pop("min_redundancy", None)
    return value


def _interface_compatibility(code, aircraft, device):
    if code != "C":
        return True
    airborne = set((aircraft.get("type") or {}).get("interfaces") or [])
    ground = set((device.get("type") or {}).get("interfaces") or [])
    if not airborne or not ground:
        return None
    return bool(airborne & ground)


def _technology(value):
    return str(((value or {}).get("type") or {}).get("technology") or "unknown").lower()


def _route_samples(route, geometry):
    if geometry and geometry.get("samples"):
        return geometry["samples"]
    return route.get("samples") or []


def _sample_result(sample, status, reasons, evidence=None, providers=None):
    if status not in SAMPLE_STATES:
        raise ValueError(f"静态能力状态无效：{status}")
    return {
        "distance_along_route_m": sample.get("distance_along_route_m"),
        "longitude": sample.get("longitude"), "latitude": sample.get("latitude"),
        "grid_id": sample.get("grid_id"), "status": status,
        "reasons": list(reasons), "evidence": list(evidence or []),
        "provider_evaluations": list(providers or []),
    }


def _summary(total, samples, forced=None):
    lengths = {"meets_under_model": 0.0, "does_not_meet_under_model": 0.0, "unknown": 0.0, "not_applicable": 0.0}
    if forced:
        lengths[forced] = total
    elif not samples:
        lengths["unknown"] = total
    elif len(samples) == 1:
        lengths[_length_state(samples[0]["status"])] = total
    else:
        ordered = sorted(samples, key=lambda item: float(item.get("distance_along_route_m") or 0.0))
        for left, right in zip(ordered, ordered[1:]):
            length = max(0.0, float(right.get("distance_along_route_m") or 0.0) - float(left.get("distance_along_route_m") or 0.0))
            left_state, right_state = _length_state(left["status"]), _length_state(right["status"])
            lengths[left_state if left_state == right_state else "unknown"] += length
    denominator = total if total > 0 else None
    return {
        "meets_length_m": lengths["meets_under_model"],
        "fail_length_m": lengths["does_not_meet_under_model"],
        "unknown_length_m": lengths["unknown"],
        "not_applicable_length_m": lengths["not_applicable"],
        "meets_fraction": lengths["meets_under_model"] / denominator if denominator else None,
        "fail_fraction": lengths["does_not_meet_under_model"] / denominator if denominator else None,
        "unknown_fraction": lengths["unknown"] / denominator if denominator else None,
    }


def _length_state(status):
    return "unknown" if status in ("unknown", "unsupported_model") else status


def _summary_status(summary, samples):
    statuses = {item["status"] for item in samples}
    if not samples or "unknown" in statuses:
        return "unknown"
    if "unsupported_model" in statuses and not statuses - {"unsupported_model"}:
        return "unsupported_model"
    if summary["fail_length_m"] > 0:
        return "does_not_meet_under_model"
    if summary["unknown_length_m"] > 0:
        return "unknown"
    return "meets_under_model"


def _independent_count(evaluations):
    if not evaluations:
        return 0
    if any(item.get("independence_confirmed") is not True or not item.get("independence_group") for item in evaluations):
        return None
    return len({str(item["independence_group"]) for item in evaluations})


def _aggregate(statuses):
    if not statuses:
        return "missing_data"
    if any(item in ("unknown", "missing_data", "unsupported_model") for item in statuses):
        return "unknown"
    if any(item == "does_not_meet_under_model" for item in statuses):
        return "does_not_meet_under_model"
    if all(item == "not_applicable" for item in statuses):
        return "not_applicable"
    return "meets_under_model"


def _finite(value, field):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} 必须是有限数值")
    return number


def _not_evaluated():
    return {name: "not_evaluated" for name in (
        "runtime_availability", "service_state", "line_of_sight", "diffraction",
        "interference", "network_load", "handover", "gnss_dop_raim",
        "radar_equation", "sensor_detection_curve",
    )}
