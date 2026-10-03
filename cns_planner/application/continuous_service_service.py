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
    def __init__(self, session, model, invalidation, snapshot=None,
                 plan_projection=None):
        self.session, self.model = session, model
        self.invalidation = invalidation
        self.snapshot_fn = snapshot or (lambda: self.session.state)
        #: Round 2.6：P16 方案实施后的**投影态**构建器。为 None 时 P17 只评估 baseline
        #: （旧装配），绝不因此伪造一个"已经考虑方案"的结论。
        self.plan_projection = plan_projection

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
        """Step6（P18 计划评审）的**权威** P17 门禁（fail-closed）。

        Round 2.6：``status`` 已经是 **post-plan 投影态**结论（存在投影时）；这里额外
        把两层结论、威胁分层与能力限制一并披露，供 Step6 与报告强制显示。
        """

        result = self.result_snapshot()
        status = str(result.get("status") or "not_calculated")
        allowed = status in STEP6_ALLOWED_ACCEPTABILITY
        disclosure = list(result.get("disclosure_lines") or [])
        projection = result.get("post_plan_projection") or {}
        return {
            "status": status,
            "confirmation_allowed": allowed,
            "allowed_statuses": list(STEP6_ALLOWED_ACCEPTABILITY),
            "variant_specific": False,
            "evaluates": "authoritative_post_plan_projection"
            if projection.get("available") is True else "authoritative_baseline_only",
            "projected_status": status if projection.get("available") is True else None,
            "baseline_status": result.get("baseline_status"),
            "post_plan_status": result.get("post_plan_status"),
            "managed_gap_count": int(result.get("managed_gap_count") or 0),
            "unacceptable_count": int(result.get("unacceptable_count") or 0),
            "unknown_count": int(result.get("unknown_count") or 0),
            "unacceptable_subsystem_count": int(
                result.get("unacceptable_subsystem_count") or 0
            ),
            "unknown_subsystem_count": int(result.get("unknown_subsystem_count") or 0),
            "primary_threat_status": result.get("primary_threat_status"),
            "supplementary_threat_status": result.get("supplementary_threat_status"),
            "limitations": deepcopy(result.get("limitations") or []),
            "limitation_disclosure": [
                item.get("disclosure") for item in (result.get("limitations") or [])
                if item.get("disclosure")
            ],
            "applied_action_ids": deepcopy(projection.get("applied_action_ids") or []),
            "projection_available": projection.get("available") is True,
            "routes": [
                {
                    "route_id": route.get("route_id"), "status": route.get("status"),
                    "unacceptable_subsystems": deepcopy(route.get("unacceptable_subsystems") or []),
                    "unknown_subsystems": deepcopy(route.get("unknown_subsystems") or []),
                    "primary_threat_status": route.get("primary_threat_status"),
                    "supplementary_threat_status": route.get("supplementary_threat_status"),
                }
                for route in result.get("routes") or []
            ],
            "requires_managed_gap_disclosure": bool(disclosure),
            "disclosure_lines": disclosure,
            "estimated_origin_dependency_count": int(
                result.get("estimated_origin_dependency_count") or 0
            ),
            "estimated_origin_tower_ids": deepcopy(
                result.get("estimated_origin_tower_ids") or []
            ),
            "site_survey_required": result.get("site_survey_required") is True,
            "planning_height_assumptions": result.get("planning_height_assumptions") is True,
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

        inputs = self.build_evaluation_inputs()
        result = self.model.evaluate(**inputs)
        _add_estimated_origin_disclosure(result, self.selected_plan_actions())
        state[ACCEPTABILITY_STATE_KEY] = result
        state.setdefault("result_statuses", {})[RESULT_STATUS_KEY] = _result_status(
            result.get("status")
        )
        self.session.save()
        return self.snapshot_fn()

    # ------------------------------------------------------ 单一变体的投影评估
    def evaluate_actions(self, actions, *, projection_label="plan_variant"):
        """对**任意一组 action** 做 baseline + post_plan 两层 P17 评估（只读，不写 state）。

        Round 2.6：每个 Plan Variant 必须绑定**自己的** P17 投影结论；本方法就是那条
        路径。它绝不写 ``continuous_service_acceptability``（那是权威容器），
        也绝不写 ExistingCNS。

        返回 ``(projection_input, result)``；投影不可用时 ``result`` 为 ``None``。
        """

        if self.plan_projection is None:
            return None, None
        projection = self.plan_projection.p17_projection_input(
            actions, projection_id=projection_label,
            original_action_ids=[
                str(item.get("action_id") or "") for item in actions or []
            ],
        )
        if projection.get("available") is not True:
            return projection, None
        inputs = self.compute_inputs()
        inputs["post_plan_projection"] = projection
        result = self.model.evaluate(**inputs)
        _add_estimated_origin_disclosure(result, actions)
        return projection, result

    def projected_step6_gate(self, result):
        """从**任意** P17 结果推导 Step6 门禁（与权威门禁同一套允许值）。"""

        if not isinstance(result, dict):
            return {
                "status": "unknown", "confirmation_allowed": False,
                "blocking_reason": "本变体没有可用的 P17 投影结论（fail-closed）",
                "variant_specific": True,
            }
        status = str(result.get("status") or "not_calculated")
        allowed = status in STEP6_ALLOWED_ACCEPTABILITY
        return {
            "status": status,
            "confirmation_allowed": allowed,
            "allowed_statuses": list(STEP6_ALLOWED_ACCEPTABILITY),
            "variant_specific": True,
            "projected_status": status,
            "baseline_status": result.get("baseline_status"),
            "post_plan_status": result.get("post_plan_status"),
            "managed_gap_count": int(result.get("managed_gap_count") or 0),
            "unacceptable_count": int(result.get("unacceptable_count") or 0),
            "unknown_count": int(result.get("unknown_count") or 0),
            "primary_threat_status": result.get("primary_threat_status"),
            "supplementary_threat_status": result.get("supplementary_threat_status"),
            "limitations": deepcopy(result.get("limitations") or []),
            "requires_managed_gap_disclosure": bool(result.get("disclosure_lines")),
            "disclosure_lines": deepcopy(result.get("disclosure_lines") or []),
            "estimated_origin_dependency_count": int(
                result.get("estimated_origin_dependency_count") or 0
            ),
            "estimated_origin_tower_ids": deepcopy(
                result.get("estimated_origin_tower_ids") or []
            ),
            "site_survey_required": result.get("site_survey_required") is True,
            "planning_height_assumptions": result.get("planning_height_assumptions") is True,
            "input_fingerprint": result.get("input_fingerprint"),
            "reasons": deepcopy(result.get("reasons") or []),
            "blocking_reason": (
                None if allowed else
                "本变体实施后的 P17 投影结论为 unknown（证据缺失）——fail-closed"
                if status in ("unknown", "stale", "not_calculated")
                else f"本变体实施后的 P17 投影结论为 {status}——不可接受"
            ),
        }

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

    # ---------------------------------------------------------- P16 投影态输入
    def selected_plan_actions(self):
        """当前权威 P16 的 ``selected_actions``（严格只读）。"""

        plan = self.session.state.get("cns_corridor_site_plan") or {}
        return list(plan.get("selected_actions") or [])

    def post_plan_projection_input(self):
        """构建 P16 ``selected_actions`` 的**投影态** P17 输入。

        Round 2.6 最高优先级架构修复：P17 不能再只看当前 ExistingCNS。

        * 没有 ``selected_actions``（或未配置投影构建器）⇒ 返回
          ``{"available": False, ...}``，评估器如实保持 **只评估 baseline**，
          绝不把"没有方案"说成"方案已评估通过"；
        * 有方案 ⇒ 用与 P16 cumulative what-if **同一套设施应用语义**重跑 P14/P15，
          产出投影态输入；投影态**绝不**写入 ExistingCNS。
        """

        actions = self.selected_plan_actions()
        if not actions:
            return {
                "available": False,
                "unavailable_reason": "no_selected_actions",
                "projection_semantics": "post_plan_projection_requires_p16_selected_actions",
                "persisted_as_upstream": False,
                "written_into_existing_cns": False,
            }
        if self.plan_projection is None:
            return {
                "available": False,
                "unavailable_reason": "projection_builder_not_configured",
                "applied_action_ids": sorted(
                    str(item.get("action_id") or "") for item in actions
                ),
                "projection_semantics": "post_plan_projection_builder_missing",
                "persisted_as_upstream": False,
                "written_into_existing_cns": False,
            }
        plan = self.session.state.get("cns_corridor_site_plan") or {}
        return self.plan_projection.p17_projection_input(
            actions,
            projection_id="p16_selected_actions",
            original_action_ids=[
                str(item.get("action_id") or "") for item in actions
            ],
        ) | {
            "p16_status": plan.get("status"),
            "p16_input_fingerprint": plan.get("input_fingerprint"),
        }

    def build_evaluation_inputs(self):
        """``compute_inputs()`` + 投影态输入（评估入口使用的完整输入）。"""

        inputs = self.compute_inputs()
        inputs["post_plan_projection"] = self.post_plan_projection_input()
        return inputs

    def fc30_facts_snapshot(self):
        """FC30 canonical 事实（含 provenance）与"3 s 是设备 failsafe 事实"的定位。

        Round 2.6 关键裁定（用户）：FC30 的 ``remote signal lost > 3 s → Failsafe RTH``
        **只**保存为**设备 failsafe 事实**，**不再**自动成为
        ``c_full_outage_max_s`` 的默认值。前端必须把两件事**分开**显示：

        * :attr:`device_failsafe_fact` —— 厂家/档案层面的设备行为事实；
        * :attr:`project_planning_threshold` —— 本项目规划阈值（用户显式登记，身份为
          ``engineering_assumption``）；**未登记时为 None**。
        """

        from ..domain.fc30_profile import FC30_FAILSAFE_TRIGGER_SEMANTICS

        state = self.session.state
        profile = _selected_profile(state) or {}
        parameters = self.parameters_snapshot().get("parameters") or {}
        outage = parameters.get("c_full_outage_max_s") or {}
        redundancy = parameters.get("c_redundancy_degradation_max_s") or {}
        return {
            "aircraft_id": FC30_AIRCRAFT_ID,
            "selected_aircraft_id": profile.get("aircraft_id"),
            "is_selected": str(profile.get("aircraft_id") or "") == FC30_AIRCRAFT_ID,
            "facts": fc30_declared_facts(),
            "failsafe_trigger_semantics": FC30_FAILSAFE_TRIGGER_SEMANTICS,
            "not_a_regulatory_threshold": True,
            #: Round 2.6：两件事**分开**、各有身份，绝不合并成一个"允许中断时间"。
            "device_failsafe_fact": {
                "parameter": "rc_loss_failsafe_trigger_s",
                "value_s": 3.0,
                "authority": "confirmed_source_fact",
                "source_type": "confirmed_source_fact",
                "semantics": FC30_FAILSAFE_TRIGGER_SEMANTICS,
                "kind": "device_failsafe_fact",
                "is_planning_threshold": False,
                "statement": (
                    "FC30 设备事实：在 Failsafe RTH 已配置的前提下，遥控（RC）信号丢失"
                    "超过 3 s 触发自动返航。它**不是**法规阈值，也**不是**本项目的规划"
                    "阈值。"
                ),
            },
            "project_planning_threshold": {
                "parameter": "c_full_outage_max_s",
                "value_s": outage.get("value"),
                "authority": outage.get("authority"),
                "source_type": outage.get("source_type"),
                "kind": "project_planning_threshold",
                "is_planning_threshold": True,
                "must_be_engineering_assumption": True,
                "evidence_required": outage.get("value") in (None, ""),
                "statement": (
                    "本项目的「最大允许完全通信中断时间」由用户显式登记，身份必须是"
                    "**工程规划假设**。未登记时 P17 的通信判定保持 evidence_required / "
                    "unknown（fail-closed），绝不自动采用设备 failsafe 的 3 s。"
                ),
                "source": outage.get("source"),
                "reason": outage.get("reason"),
            },
            "redundancy_degradation_threshold": {
                "parameter": "c_redundancy_degradation_max_s",
                "value_s": redundancy.get("value"),
                "authority": redundancy.get("authority"),
                "source_type": redundancy.get("source_type"),
                "kind": "project_planning_threshold",
                "is_planning_threshold": True,
                "separate_from_full_outage": True,
                "statement": (
                    "冗余退化阈值与完全中断阈值是**两个独立阈值**，绝不合并。"
                ),
            },
            "thresholds_are_separate": True,
            "threshold_merge_forbidden": True,
            "disclosure": (
                "FC30 的 3 s 是**设备 failsafe 触发门限**（遥控信号丢失超过 3 s 触发 RTH），"
                "不是法规阈值，也**不自动**作为本项目的规划阈值；本项目的最大允许完全"
                "通信中断时间由用户以 engineering_assumption 显式登记（未登记时判定保持"
                "evidence_required / unknown）。"
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


def _add_estimated_origin_disclosure(result, actions):
    """Keep Tier-B planning usable without presenting it as deployed physical truth."""

    if not isinstance(result, dict):
        return result
    ids = sorted({
        str(item.get("tower_id")) for item in actions or []
        if item.get("origin_status") == "estimated" and item.get("tower_id")
    })
    result["estimated_origin_dependency_count"] = len(ids)
    result["estimated_origin_tower_ids"] = ids
    result["site_survey_required"] = bool(ids) or any(
        item.get("requires_site_survey") is True for item in actions or []
    )
    result["planning_height_assumptions"] = bool(ids)
    if ids:
        line = "部分共塔站址高程为工程估计，实施前需现场勘察确认。"
        disclosures = result.setdefault("disclosure_lines", [])
        if line not in disclosures:
            disclosures.append(line)
        result.setdefault("planning_limitations", []).append({
            "kind": "planning_height_assumption",
            "authority": "engineering_estimate",
            "tower_ids": ids,
            "physical_deployment_confirmed": False,
            "site_survey_required": True,
            "unconfirmed_items": [
                "承重", "供电", "传输", "结构安全", "安装空间", "电磁兼容",
                "真实安装高度", "业主许可",
            ],
        })
    return result


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
