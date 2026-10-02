"""P17 —— 连续服务 / 不可接受事件评估的应用编排（唯一写入口）。

职责边界：

* 只**读取**上游权威结果（P14/P15/P16、机载档案、工程证据、Radar layout）并调用
  :class:`~cns_planner.algorithms.continuous_service.v1.ContinuousServiceAcceptabilityV1`；
* 评估结果落在**独立**容器 ``project_state["continuous_service_acceptability"]``，
  随项目 save / reopen 逐字保留（``unknown`` 重开后仍是 ``unknown``，绝不被升级）；
* 工程证据 / 策略变更只让 P17 失效（``invalidation.continuous_service()``），
  **不**动 P14/P15/P16/Radar；
* 写命令一律返回**完整 workflow 快照**（Round 2.4 的 BUG-PLANNING-EVIDENCE-001 契约）。
"""

from __future__ import annotations

from copy import deepcopy

from ..algorithms.continuous_service.v1 import ContinuousServiceAcceptabilityV1
from ..domain.cns_continuous_service import (
    ACCEPTABILITY_STATUSES, CONTINUOUS_SERVICE_SCHEMA_VERSION,
    default_continuous_service_policy, normalize_continuous_service_policy,
)
from ..domain.cns_continuous_service import (
    default_operation_scenario as _default_operation_scenario,
)
from ..domain.cns_continuous_service import (
    normalize_operation_scenario as _normalize_operation_scenario,
)
from ..domain.fc30_profile import FC30_AIRCRAFT_ID, fc30_declared_facts
from ..domain.planning_evidence import continuous_service_parameter_projection

ACCEPTABILITY_STATE_KEY = "continuous_service_acceptability"
POLICY_STATE_KEY = "cns_continuous_service_policy"
RESULT_STATUS_KEY = ACCEPTABILITY_STATE_KEY

#: Step6（P18 计划评审）允许继续的运行可接受性结论。
STEP6_ALLOWED_ACCEPTABILITY = ("fully_satisfied", "acceptable_with_managed_gap")


class ContinuousServiceService:
    def __init__(self, session, model, invalidation, snapshot=None):
        self.session, self.model = session, model
        self.invalidation = invalidation
        self.snapshot_fn = snapshot or (lambda: self.session.state)

    # ------------------------------------------------------------------ 读取
    def result_snapshot(self):
        return deepcopy(
            self.session.state.get(ACCEPTABILITY_STATE_KEY)
            or self.model.empty()
        )

    def policy_snapshot(self):
        return normalize_continuous_service_policy(
            self.session.state.get(POLICY_STATE_KEY)
        )

    def parameters_snapshot(self):
        """P17 参数的只读投影（逐项带 authority / source_type / 出处）。

        投影与评估器使用**同一套优先级**：显式策略覆盖 → 显式工程证据 →
        内置工程基线；否则前端会显示"内置基线"，而实际判定用的是策略覆盖值。
        """

        from ..domain.cns_continuous_service import (
            DEFAULT_SERVICE_ACCEPTABILITY_LIMITS,
        )

        state = self.session.state
        parameters = {
            item["field"]: item
            for item in continuous_service_parameter_projection(
                state.get("planning_evidence")
            )
        }
        policy = self.policy_snapshot()
        overrides = dict(policy.get("parameter_overrides") or {})
        limits = policy.get("service_acceptability_limits") or {}
        for code, entry in limits.items():
            for kind, parameter in (
                ("service_outage", "c_full_outage_max_s"),
                ("redundancy_degradation", "c_redundancy_degradation_max_s"),
            ):
                if code != "C":
                    continue
                value = (entry or {}).get(kind)
                if value is not None:
                    overrides[parameter] = value
        for name, override in overrides.items():
            if name in parameters and override is not None:
                parameters[name] = {
                    **parameters[name],
                    "value": override,
                    "authority": "policy_override",
                    "source_type": "engineering_assumption",
                    "participating": True,
                    "reason": "由 cns_continuous_service_policy 显式覆盖",
                }
        return {
            "schema_version": CONTINUOUS_SERVICE_SCHEMA_VERSION,
            "semantics": (
                "continuous_service_parameters_with_per_parameter_authority_"
                "confirmed_source_fact_external_reference_engineering_assumption_or_builtin_baseline"
            ),
            "limits_resolution_order": (
                "cns_continuous_service_policy → planning_evidence 显式记录 → "
                "DEFAULT_SERVICE_ACCEPTABILITY_LIMITS 内置工程基线"
            ),
            "builtin_limits": {
                key: dict(value)
                for key, value in DEFAULT_SERVICE_ACCEPTABILITY_LIMITS.items()
            },
            "parameters": parameters,
        }

    def operation_scenario_snapshot(self):
        return deepcopy(
            self.session.state.get("cns_operation_scenario")
            or default_operation_scenario()
        )

    def step6_gate(self):
        """Step6（P18 计划评审）的 P17 门禁（fail-closed）。"""

        result = self.result_snapshot()
        status = str(result.get("status") or "not_calculated")
        allowed = status in STEP6_ALLOWED_ACCEPTABILITY
        disclosure = list(result.get("disclosure_lines") or [])
        return {
            "status": status,
            "confirmation_allowed": allowed,
            "allowed_statuses": list(STEP6_ALLOWED_ACCEPTABILITY),
            "managed_gap_count": int(result.get("managed_gap_count") or 0),
            "unacceptable_count": int(result.get("unacceptable_count") or 0),
            "unknown_count": int(result.get("unknown_count") or 0),
            "unacceptable_subsystem_count": int(
                result.get("unacceptable_subsystem_count") or 0
            ),
            "unknown_subsystem_count": int(result.get("unknown_subsystem_count") or 0),
            "routes": [
                {
                    "route_id": route.get("route_id"), "status": route.get("status"),
                    "unacceptable_subsystems": deepcopy(route.get("unacceptable_subsystems") or []),
                    "unknown_subsystems": deepcopy(route.get("unknown_subsystems") or []),
                }
                for route in result.get("routes") or []
            ],
            "requires_managed_gap_disclosure": bool(disclosure),
            "disclosure_lines": disclosure,
            "input_fingerprint": result.get("input_fingerprint"),
            "reasons": deepcopy(result.get("reasons") or []),
        }

    # ------------------------------------------------------------------ 写入
    def set_policy(self, payload):
        raw = (payload or {}).get(POLICY_STATE_KEY, payload)
        normalized = normalize_continuous_service_policy(raw)
        if normalized != self.session.state.get(POLICY_STATE_KEY):
            self.session.state[POLICY_STATE_KEY] = normalized
            self.invalidation.continuous_service()
            self.session.save()
        return self.snapshot_fn()

    def set_operation_scenario(self, payload):
        raw = (payload or {}).get("cns_operation_scenario", payload)
        normalized = normalize_operation_scenario(raw)
        if normalized != self.session.state.get("cns_operation_scenario"):
            self.session.state["cns_operation_scenario"] = normalized
            self.invalidation.continuous_service()
            self.session.save()
        return self.snapshot_fn()

    def evaluate(self, payload=None):
        state = self.session.state
        if isinstance(payload, dict) and "cns_operation_scenario" in payload:
            normalized = normalize_operation_scenario(payload["cns_operation_scenario"])
            if normalized != state.get("cns_operation_scenario"):
                state["cns_operation_scenario"] = normalized
                self.invalidation.continuous_service()
        if isinstance(payload, dict) and POLICY_STATE_KEY in payload:
            normalized = normalize_continuous_service_policy(payload[POLICY_STATE_KEY])
            if normalized != state.get(POLICY_STATE_KEY):
                state[POLICY_STATE_KEY] = normalized
                self.invalidation.continuous_service()

        inputs = self.compute_inputs()
        result = self.model.evaluate(**inputs)
        state[ACCEPTABILITY_STATE_KEY] = result
        state.setdefault("result_statuses", {})[RESULT_STATUS_KEY] = _result_status(
            result.get("status")
        )
        self.session.save()
        return self.snapshot_fn()

    # -------------------------------------------------------------- 输入组装
    def compute_inputs(self):
        """组装 P17 的**全部**输入（只读；不写任何状态）。"""

        state = self.session.state
        profile = _selected_profile(state)
        return {
            "corridor_assessment": deepcopy(state.get("cns_corridor_assessment") or {}),
            "corridor_gap_assessment": deepcopy(state.get("cns_corridor_gap_assessment") or {}),
            "site_plan": deepcopy(state.get("cns_corridor_site_plan") or {}),
            "aircraft_profile": deepcopy(profile),
            "planning_evidence": deepcopy(state.get("planning_evidence") or {}),
            "operation_scenario": self.operation_scenario_snapshot(),
            "continuous_service_policy": self.policy_snapshot(),
            "device_catalog": deepcopy(state.get("device_catalog") or {}),
            "radar_surveillance_layout": deepcopy(state.get("radar_surveillance_layout") or {}),
        }

    def fc30_facts_snapshot(self):
        """FC30 canonical 事实（含 provenance）与"3 s 是设备 failsafe 事实"的定位。"""

        from ..domain.fc30_profile import FC30_FAILSAFE_TRIGGER_SEMANTICS

        profile = _selected_profile(self.session.state) or {}
        return {
            "aircraft_id": FC30_AIRCRAFT_ID,
            "selected_aircraft_id": profile.get("aircraft_id"),
            "is_selected": str(profile.get("aircraft_id") or "") == FC30_AIRCRAFT_ID,
            "facts": fc30_declared_facts(),
            "failsafe_trigger_semantics": FC30_FAILSAFE_TRIGGER_SEMANTICS,
            "not_a_regulatory_threshold": True,
            "disclosure": (
                "FC30 的 3 s 是**设备 failsafe 触发门限**（遥控信号丢失超过 3 s 触发 RTH），"
                "不是法规阈值；P17 的 C 全失联阈值以该事实为依据，但它是可覆盖的工程基线。"
            ),
        }


#: 当前操作场景（Round 2.5）。定义在 domain 层（与 `project_state` 共用同一份真值），
#: 这里只做再导出，避免出现"第二种默认场景"。
default_operation_scenario = _default_operation_scenario
normalize_operation_scenario = _normalize_operation_scenario


def _selected_profile(state):
    from ..catalogs import AircraftCNSProfileCatalog

    return AircraftCNSProfileCatalog.find(
        state.get("aircraft_profiles") or {},
        state.get("selected_aircraft_profile_id") or "",
    )


def _result_status(status):
    return {
        "fully_satisfied": "passed",
        "acceptable_with_managed_gap": "passed_with_disclosure",
        "unacceptable": "failed",
        "unknown": "evidence_required",
        #: 没有可评估的航路对象（空项目）⇒ 本层没有可拒绝的对象。
        "not_applicable": "not_applicable",
        "not_calculated": "not_calculated",
    }.get(str(status), "evidence_required")


__all__ = [
    "ACCEPTABILITY_STATE_KEY", "ContinuousServiceService", "POLICY_STATE_KEY",
    "STEP6_ALLOWED_ACCEPTABILITY",
    "default_operation_scenario", "normalize_operation_scenario",
]
