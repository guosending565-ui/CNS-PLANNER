"""P17 —— 连续服务 / 不可接受事件评估（``continuous_service_acceptability_v1``）。

本模块是**纯函数**：只读入参、只产出 JSON-safe 结果，不写 state、不读文件、不联网。

输入（全部来自既有权威结果，绝不重算上游）
-----------------------------------------

* ``P14`` ``cns_corridor_assessment``：航路几何（``corridor_geometry.route_path``）、
  逐体元 ``nearest_route_offset_m`` / ``nearest_route_distance_m`` /
  ``cell_half_diagonal_m`` / ``surface_class`` / ``subsystems[]``；
* ``P15`` ``cns_corridor_gap_assessment``：``continuous_deficit_segments``（按 cause
  分组）、``service_redundancy``（按 service_key 的站址冗余结论）；
* ``P16`` ``cns_corridor_site_plan``：只用于**披露**"缺口是否已被规划动作改变"
  （其自身结论不改写 P15 的缺口事实）；
* 机载档案（FC30 canonical 或选定档案）：航路速度、最大水平速度、导航回退链；
* :mod:`cns_planner.domain.planning_evidence`：C/S 阈值、保护链分量、``D_safety``、
  ``D_uncertainty``、入侵者/接近速度、RTK/GNSS 证据；
* 操作场景（单机 / 只考虑其他无人机 / 巡航高度 / 设计入侵者速度）。

输出（要点）
------------

每条航路给出：**保护走廊参数**、**逐 service 的首次探测距离证据**、
**连续事件**（按 cause 分离的 C/S 事件、S 探测缺口）、**T_chain / T_available /
T_margin**、**导航状态机结论**，以及聚合后的
``fully_satisfied`` / ``acceptable_with_managed_gap`` / ``unacceptable`` / ``unknown``。

绝不"为了出结果"放宽上游：任何输入缺失都如实返回 ``unknown``（fail-closed）。
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json

from ...domain.cns_continuous_service import (
    ACCEPTABILITY_STATUSES, BASELINE_VALUES, CONTINUOUS_SERVICE_ALGORITHM_ID,
    CONTINUOUS_SERVICE_ALGORITHM_VERSION, CONTINUOUS_SERVICE_KIND_PARAMETER,
    CONTINUOUS_SERVICE_REASONS,
    CONTINUOUS_SERVICE_SCHEMA_VERSION, DEFAULT_SERVICE_ACCEPTABILITY_LIMITS,
    DEFICIT_CAUSE_TO_EVENT_KIND,
    LIMITATION_SEMANTICS, OPERATION_SCENARIO_DEFAULTS, PRIMARY_THREAT_LAYER,
    ROUTE_PROTECTION_FORMULA, SUPPLEMENTARY_THREAT_LAYER, T_CHAIN_COMPONENTS,
    THREAT_LAYER_LABELS, THREAT_LAYERS, aggregate_acceptability,
    compare_plan_stages, continuous_gap_duration, default_continuous_service_policy,
    layer_conclusion_status, limitation_disclosure_lines, managed_gap_disclosure,
    noncooperative_limitations, protection_distance, subsystem_acceptability_counts,
    subsystem_status_from_events, surveillance_acceptance, surveillance_threat_layers,
    time_chain_total,
)
from ...domain.fc30_profile import (
    FC30_AIRCRAFT_ID, fc30_aircraft_profile, fc30_planning_speeds,
)
from ...domain.planning_evidence import (
    PLANNING_EVIDENCE_FIELDS, resolve_continuous_parameter,
)

#: 逐 service 的连续事件参数来源（阈值只对 C / S 定义）。
_SERVICE_SUBSYSTEMS = ("C", "S")
#: Round 2.8：P15 缺口原因 → P17 事件种类的映射与 P16 **共用同一份 domain 常量**，
#: 否则"同一物理量的两个词表"会各自漂移，P16 的连续服务停止条件与 P17 的判定分叉。
_CAUSE_KINDS = dict(DEFICIT_CAUSE_TO_EVENT_KIND)
_KIND_LIMIT_PARAMETER = dict(CONTINUOUS_SERVICE_KIND_PARAMETER)

#: 监视 service 的近似优先级（仅在"多个 service 同时可用"时用于稳定排序）。
_MITIGATION_BY_KIND = {
    "service_outage": (
        "按现有规划动作/地面站复用方案缩短该段；评估期只接受该段为**有管理的缺口**，"
        "并在报告中逐段披露，不视为全覆盖。"
    ),
    "redundancy_degradation": (
        "补足独立 provider（不同站址/不同供电）或降低该段的冗余要求；"
        "评估期接受为有管理的缺口并逐段披露。"
    ),
}

#: FC30 canonical 参考航路速度（只作为**参考基线**并列展示，不覆盖选定档案的实际值）。
FC30_ROUTE_SPEED_MPS = fc30_planning_speeds(fc30_aircraft_profile())["route_speed_mps"]


class ContinuousServiceAcceptabilityV1:
    """P17 评估器（无状态；``evaluate`` 每次重新计算）。"""

    algorithm_id = CONTINUOUS_SERVICE_ALGORITHM_ID
    algorithm_version = CONTINUOUS_SERVICE_ALGORITHM_VERSION
    model_scope = "continuous_service_acceptability_and_route_protection_corridor"
    status_vocabulary = ACCEPTABILITY_STATUSES

    @classmethod
    def empty(cls, status="not_calculated", reasons=None) -> dict:
        return {
            "status": status,
            "algorithm_id": cls.algorithm_id,
            "algorithm_version": cls.algorithm_version,
            "model_scope": cls.model_scope,
            "schema_version": CONTINUOUS_SERVICE_SCHEMA_VERSION,
            "input_fingerprint": None,
            "status_vocabulary": list(ACCEPTABILITY_STATUSES),
            "operation_scenario": None,
            "route_count": 0,
            "routes": [],
            "reasons": list(reasons or []),
            "reason_codes": [],
            "managed_gap_count": 0,
            "unacceptable_count": 0,
            "unknown_count": 0,
            "disclosure_lines": [],
            #: Round 2.6：两层结论（baseline / post_plan）与强制披露。
            "baseline_status": status,
            "post_plan_status": None,
            "baseline": None,
            "post_plan_projection": None,
            "primary_threat_status": None,
            "supplementary_threat_status": None,
            "limitations": [],
            "not_evaluated": _not_evaluated(),
        }

    # ------------------------------------------------------------------ 入口
    def evaluate(
        self, *, corridor_assessment, corridor_gap_assessment, site_plan=None,
        aircraft_profile=None, planning_evidence=None, operation_scenario=None,
        continuous_service_policy=None, device_catalog=None,
        radar_surveillance_layout=None,
        post_plan_projection=None, plan_stage="baseline",
    ) -> dict:
        """P17 评估入口。

        Round 2.6 核心修复：**P17 必须评估 P16 方案实施后的 projected state**。

        * ``corridor_assessment`` / ``corridor_gap_assessment`` 传入的是
          :data:`cns_planner.domain.cns_continuous_service.PLAN_STAGES` 里
          ``plan_stage`` 对应的那一层；``baseline`` 使用当前权威 ExistingCNS，
          ``post_plan`` 使用"当前权威状态 + P16 ``selected_actions``"的投影态；
        * ``post_plan_projection`` 是**已经算好的投影层输入**（含 facilities / radar /
          navigation 证据下的 P14/P15、以及投影来源指纹）。评估器**不重算上游**，
          只消费调用方（应用层）给出的投影；
        * 顶层 ``status`` 在存在投影时**跟随投影态**（``post_plan``）——这正是 Step6
          需要判断的对象；``baseline`` 的完整结论原样保留在 ``baseline`` 字段里。

        绝不"为了出结果"放宽上游：任何输入缺失都如实返回 ``unknown``（fail-closed）。
        """

        policy = continuous_service_policy or default_continuous_service_policy()
        scenario = _operation_scenario(operation_scenario)

        baseline = self._assessment(
            corridor_assessment=corridor_assessment,
            corridor_gap_assessment=corridor_gap_assessment, site_plan=site_plan,
            aircraft_profile=aircraft_profile, planning_evidence=planning_evidence,
            scenario=scenario, policy=policy, device_catalog=device_catalog,
            radar_surveillance_layout=radar_surveillance_layout,
            plan_stage="baseline",
        )

        if isinstance(post_plan_projection, dict) and post_plan_projection.get("available") is True:
            post = self._assessment(
                corridor_assessment=post_plan_projection.get("corridor_assessment"),
                corridor_gap_assessment=post_plan_projection.get("corridor_gap_assessment"),
                site_plan=post_plan_projection.get("site_plan") or site_plan,
                aircraft_profile=aircraft_profile, planning_evidence=planning_evidence,
                scenario=scenario, policy=policy,
                device_catalog=post_plan_projection.get("device_catalog") or device_catalog,
                radar_surveillance_layout=post_plan_projection.get("radar_surveillance_layout")
                or radar_surveillance_layout,
                plan_stage="post_plan",
            )
            post_payload = self._assessment_payload(post)
            projection = {
                **{key: deepcopy(value) for key, value in post_plan_projection.items()
                   if key not in ("corridor_assessment", "corridor_gap_assessment")},
                "available": True,
                "status": post_payload.get("status"),
                "route_count": post_payload.get("route_count"),
                #: 完整的两层比较（改进项 + 剩余缺口）。
                "comparison": compare_plan_stages(
                    self._assessment_payload(baseline),
                    post_payload,
                    {
                        "baseline_fingerprint": baseline.get("input_fingerprint"),
                        "post_plan_fingerprint": post.get("input_fingerprint"),
                        "applied_action_ids": deepcopy(
                            post_plan_projection.get("applied_action_ids") or []
                        ),
                        "projection_semantics": post_plan_projection.get(
                            "projection_semantics"
                        ),
                        "persisted_as_upstream": False,
                    },
                ),
            }
            post_result = self._assemble(post, projection=projection, baseline=None)
            baseline_result = self._assemble(baseline, projection=None, baseline=None)
            result = {
                **post_result,
                #: Round 2.6：两层结论都必须**完整保留**；顶层跟随投影态（Step6 判定对象）。
                "baseline_status": baseline_result.get("status"),
                "post_plan_status": post_result.get("status"),
                "baseline": baseline_result,
            }
        else:
            result = self._assemble(baseline, projection=None, baseline=None)
        result["plan_stage"] = plan_stage
        result["post_plan_projection_semantics"] = (
            "post_plan_projection_is_hypothetical_never_written_into_existing_cns"
        )
        return result

    # -------------------------------------------------------------- 单层评估
    def _assessment(
        self, *, corridor_assessment, corridor_gap_assessment, site_plan,
        aircraft_profile, planning_evidence, scenario, policy, device_catalog,
        radar_surveillance_layout, plan_stage,
    ) -> dict:
        """对**一层**（baseline 或 post_plan）的 P14/P15 输入做完整评估（不聚合披露）。"""

        corridor_status = (corridor_assessment or {}).get("status")
        gap_status = (corridor_gap_assessment or {}).get("status")
        if corridor_status in (None, "not_calculated", "stale", "missing_data"):
            return {"__not_evaluable__": CONTINUOUS_SERVICE_REASONS["no_upstream_p15"]}
        if gap_status in (None, "not_calculated", "stale", "missing_data"):
            return {"__not_evaluable__": CONTINUOUS_SERVICE_REASONS["no_upstream_p15"]}

        speed_facts = fc30_planning_speeds(aircraft_profile)
        route_speed = speed_facts["route_speed_mps"]
        parameters = self._parameters(planning_evidence, policy)
        limits = {
            code: {
                kind: _limit(parameters, code, kind, policy)
                for kind in ("service_outage", "redundancy_degradation")
            }
            for code in _SERVICE_SUBSYSTEMS
        }
        gap_routes = {
            str(route.get("route_id") or ""): route
            for route in (corridor_gap_assessment or {}).get("routes") or []
        }
        corridor_routes = {
            str(route.get("route_id") or ""): route
            for route in (corridor_assessment or {}).get("routes") or []
        }
        requirement_sets = _requirement_index((site_plan or {}).get("route_requirements"))
        reason_codes: list[str] = []
        routes = []
        for route_id, gap_route in gap_routes.items():
            corridor_route = corridor_routes.get(route_id)
            if corridor_route is None:
                reason_codes.append("no_p14_route")
                routes.append({
                    "route_id": route_id, "status": "unknown",
                    "reason_codes": ["no_p14_route"],
                    "reasons": [CONTINUOUS_SERVICE_REASONS["no_p13_geometry"]],
                })
                continue
            routes.append(self._route(
                route_id=route_id, corridor_route=corridor_route, gap_route=gap_route,
                parameters=parameters, limits=limits, scenario=scenario,
                route_speed=route_speed, device_catalog=device_catalog or {},
                radar_layout=radar_surveillance_layout or {},
                requirement_set=requirement_sets.get(route_id) or {},
                aircraft_profile=aircraft_profile, plan_stage=plan_stage,
            ))
        return {
            "__evaluated__": True,
            "routes": routes,
            "reason_codes": reason_codes,
            "route_speed": route_speed,
            "speed_facts": speed_facts,
            "parameters": parameters,
            "limits": limits,
            "scenario": scenario,
            "policy": policy,
            "aircraft_profile": aircraft_profile,
            "input_fingerprint": _fingerprint({
                "corridor": (corridor_assessment or {}).get("input_fingerprint"),
                "gap": (corridor_gap_assessment or {}).get("input_fingerprint"),
                "site_plan": (site_plan or {}).get("input_fingerprint"),
                "aircraft": {
                    "aircraft_id": (aircraft_profile or {}).get("aircraft_id"),
                    "cruise_speed_mps": speed_facts["route_speed_mps"],
                    "max_horizontal_speed_mps": speed_facts["max_horizontal_speed_mps"],
                },
                "parameters": {
                    name: [item.get("value"), item.get("authority")]
                    for name, item in sorted(parameters.items())
                },
                "limits": limits,
                "scenario": scenario,
                "policy": _policy_identity(policy),
            }),
        }

    @staticmethod
    def _assessment_payload(assessment) -> dict:
        """把内部单层评估压成公开的两层比较所需形状（**只含 routes 级结论**）。

        完整字段（参数、阈值、速度基线、指纹）由 :meth:`_assemble` 从同一份评估里取，
        因此两层比较与公开结果**不会**出现两套聚合逻辑。
        """

        if not isinstance(assessment, dict) or assessment.get("__evaluated__") is not True:
            return {
                "status": "unknown", "routes": [], "route_count": 0,
                "reasons": [CONTINUOUS_SERVICE_REASONS["no_upstream_p15"]],
                "reason_codes": ["no_upstream_p15"], "input_fingerprint": None,
                "managed_gap_count": 0, "unacceptable_count": 0, "unknown_count": 0,
                "unacceptable_subsystem_count": 0, "unknown_subsystem_count": 0,
                "disclosure_lines": [],
            }
        routes = assessment["routes"]
        return {
            "status": _route_set_status(routes),
            "routes": routes,
            "route_count": len(routes),
            "reasons": sorted({
                reason for route in routes for reason in (route.get("reasons") or [])
            }),
            "reason_codes": sorted(set(assessment.get("reason_codes") or [])),
            "input_fingerprint": None,
            "managed_gap_count": sum(
                len(route.get("managed_gaps") or []) for route in routes
            ),
            "unacceptable_count": sum(
                int(route.get("status") == "unacceptable") for route in routes
            ),
            "unknown_count": sum(
                int(route.get("status") == "unknown") for route in routes
            ),
            "unacceptable_subsystem_count": sum(
                len(route.get("unacceptable_subsystems") or []) for route in routes
            ),
            "unknown_subsystem_count": sum(
                len(route.get("unknown_subsystems") or []) for route in routes
            ),
            "disclosure_lines": [
                line for route in routes
                for entry in (route.get("managed_gaps") or [])
                for line in managed_gap_disclosure(
                    {**entry, "route_id": route.get("route_id")}
                )
            ],
        }

    def _assemble(self, assessment, *, projection, baseline) -> dict:
        """把单层评估结果组装成公开结果（含两层比较与强制披露）。"""

        payload = self._assessment_payload(assessment)
        routes = payload.get("routes") or []
        status = payload.get("status") or "unknown"
        #: 原因码必须**同时**聚合"逐 route 的原因码"与"评估级别的原因码"：
        #: 只取后者会让"探测证据不足"这类真实原因在前端/报告里消失。
        reason_codes = list(payload.get("reason_codes") or [])
        reason_codes.extend(
            code for route in routes for code in (route.get("reason_codes") or [])
        )
        if status == "not_applicable":
            #: 空集与"评估对象存在但结论无法归属"必须分开：
            #: 没有任何航路可评估 ⇒ 本层没有可拒绝的对象（fully_satisfied）；
            #: 有航路却全部 not_applicable ⇒ 保持 unknown（fail-closed）。
            status = "fully_satisfied" if not routes else "unknown"
            if routes:
                reason_codes.append("no_applicable_route_scope")
        route_speed = assessment.get("route_speed") if isinstance(assessment, dict) else None
        speed_facts = (assessment or {}).get("speed_facts") or {}
        if route_speed in (None, ""):
            reason_codes.append("no_route_speed")
        parameters = (assessment or {}).get("parameters") or {}
        scenario = (assessment or {}).get("scenario") or {}

        limitations = [
            deepcopy(item) for route in routes for item in (route.get("limitations") or [])
        ]
        seen_limitations = set()
        unique_limitations = []
        for item in limitations:
            key = str(item.get("limitation_id") or "")
            if key in seen_limitations:
                continue
            seen_limitations.add(key)
            unique_limitations.append(item)
        disclosure = list(payload.get("disclosure_lines") or [])
        disclosure.extend(limitation_disclosure_lines(unique_limitations))

        return {
            "status": status,
            "algorithm_id": self.algorithm_id,
            "algorithm_version": self.algorithm_version,
            "model_scope": self.model_scope,
            "schema_version": CONTINUOUS_SERVICE_SCHEMA_VERSION,
            "plan_stage": "baseline",
            "input_fingerprint": (assessment or {}).get("input_fingerprint"),
            "status_vocabulary": list(ACCEPTABILITY_STATUSES),
            "operation_scenario": scenario,
            "route_speed_mps": route_speed,
            "max_horizontal_speed_mps": speed_facts.get("max_horizontal_speed_mps"),
            "speed_basis": {
                "route_speed_mps": route_speed,
                "route_speed_source": "selected_aircraft_profile.cruise_speed_mps",
                "max_horizontal_speed_mps": speed_facts.get("max_horizontal_speed_mps"),
                "selected_aircraft_id": ((assessment or {}).get("aircraft_profile") or {}).get(
                    "aircraft_id"
                ),
                #: FC30 canonical 参考值：当前项目若选了别的档案，这里如实并列，
                #: 便于"为什么 R0005 的持续时间和 FC30 的 15 m/s 不一致"可核查。
                "fc30_route_speed_mps": FC30_ROUTE_SPEED_MPS,
                "fc30_route_speed_source": "fc30_canonical_profile.route_speed（仅作为参考基线）",
                "duration_formula": "along_route_gap_m / current_route_speed_mps",
                "semantics": "duration_uses_the_selected_profile_route_speed_never_max_speed",
            },
            "service_acceptability_limits": (assessment or {}).get("limits") or {},
            "route_count": len(routes),
            "routes": routes,
            "parameters": _parameter_projection(parameters),
            "reasons": payload.get("reasons") or [],
            "reason_codes": sorted(set(reason_codes)),
            "managed_gap_count": payload.get("managed_gap_count") or 0,
            "unacceptable_count": payload.get("unacceptable_count") or 0,
            "unknown_count": payload.get("unknown_count") or 0,
            #: 子系统级计数：路线结论为 unacceptable 时仍可能有 unknown 子系统，
            #: 两者都如实披露（不隐藏任何一类）。
            "unacceptable_subsystem_count": payload.get("unacceptable_subsystem_count") or 0,
            "unknown_subsystem_count": payload.get("unknown_subsystem_count") or 0,
            "disclosure_lines": disclosure,
            "baseline_status": status,
            "post_plan_status": (
                projection.get("status") if isinstance(projection, dict) else None
            ),
            "baseline": baseline,
            "post_plan_projection": projection,
            "primary_threat_status": _merged_threat_status(routes, PRIMARY_THREAT_LAYER),
            "supplementary_threat_status": _merged_threat_status(
                routes, SUPPLEMENTARY_THREAT_LAYER
            ),
            "limitations": unique_limitations,
            "threat_layer_semantics": (
                "cooperative_rid_primary_and_noncooperative_radar_supplementary_"
                "evaluated_separately_never_merged"
            ),
            "not_evaluated": _not_evaluated(),
        }

    @staticmethod
    def _parameters(planning_evidence, policy) -> dict:
        """解析全部 P17 参数，并叠加**显式策略覆盖**（覆盖优先于证据与内置基线）。"""

        parameters = {
            name: resolve_continuous_parameter(planning_evidence, name)
            for name in PLANNING_EVIDENCE_FIELDS
            if PLANNING_EVIDENCE_FIELDS[name].get("kind") == "continuous_service_parameter"
        }
        for name, override in (policy.get("parameter_overrides") or {}).items():
            #: 显式覆盖容器优先于"证据 + 内置基线"（它在 UI 里就是一次显式动作）。
            if name in parameters and override is not None:
                parameters[name] = {
                    **parameters[name],
                    "value": override,
                    "authority": "policy_override",
                    "source_type": "engineering_assumption",
                    "participating": True,
                    "reason": "由 cns_continuous_service_policy 显式覆盖",
                }
        return parameters

    def evaluate_legacy(self, **kwargs) -> dict:
        """（保留给旧调用点的签名兼容入口；行为与 :meth:`evaluate` 一致。）"""

        return self.evaluate(**kwargs)

    # ------------------------------------------------------------------ 单航路
    def _route(
        self, *, route_id, corridor_route, gap_route, parameters, limits, scenario,
        route_speed, device_catalog, radar_layout, requirement_set, aircraft_profile=None,
        plan_stage="baseline",
    ):
        reasons: list[str] = []
        reason_codes: list[str] = []
        route_length = corridor_route.get("route_length_m")
        route_path = ((corridor_route.get("corridor_geometry") or {}).get("route_path")) or []
        half_width = (corridor_route.get("corridor_geometry") or {}).get(
            "horizontal_half_width_m"
        )

        # ---- 保护走廊（Round 2.6 四分量公式） -------------------------------
        speed_basis = str(_parameter_value(parameters, "relative_speed_basis") or "conservative")
        relative_speed = _parameter_value(
            parameters,
            "conservative_closing_speed_mps" if speed_basis == "conservative"
            else "nominal_closing_speed_mps",
        )
        t_chain = time_chain_total({
            name: _parameter_value(parameters, f"T_chain_{name}_s")
            for name in T_CHAIN_COMPONENTS
        })
        if t_chain is None:
            reason_codes.append("no_time_chain")
            reasons.append(CONTINUOUS_SERVICE_REASONS["no_time_chain"])
        d_separation = _parameter_value(parameters, "D_separation_m")
        d_maneuver = _parameter_value(parameters, "D_maneuver_m")
        d_uncertainty = _parameter_value(parameters, "D_uncertainty_m")
        d_protection = protection_distance(
            d_separation, relative_speed, t_chain, d_uncertainty, d_maneuver,
        )
        if d_protection is None and "no_time_chain" not in reason_codes:
            #: 四分量里缺任何一项都不可判定；如实区分"链时延缺失"与"距离分量缺证据"，
            #: 绝不把"没有工程依据"说成"链时延算不出来"。
            if t_chain is None:
                reason_codes.append("no_time_chain")
                reasons.append(CONTINUOUS_SERVICE_REASONS["no_time_chain"])
            else:
                reason_codes.append("protection_distance_evidence_required")
                reasons.append(
                    CONTINUOUS_SERVICE_REASONS["protection_distance_evidence_required"]
                )
        if not route_path:
            reason_codes.append("no_p13_geometry")
            reasons.append(CONTINUOUS_SERVICE_REASONS["no_p13_geometry"])
        corridor = {
            "semantics": "horizontal_route_protection_corridor_not_route_centerline_only",
            "route_path": deepcopy(route_path),
            "route_length_m": route_length,
            "cns_requirement_corridor_half_width_m": half_width,
            #: Round 2.6 四个分量逐项给出（含各自的 authority），前端逐项显示。
            "D_separation_m": d_separation,
            "D_separation_authority": _parameter_authority(parameters, "D_separation_m"),
            "D_maneuver_m": d_maneuver,
            "D_maneuver_authority": _parameter_authority(parameters, "D_maneuver_m"),
            "D_maneuver_semantics": "engineering_baseline_interface_not_regulatory_value",
            "D_uncertainty_m": d_uncertainty,
            "D_uncertainty_authority": _parameter_authority(parameters, "D_uncertainty_m"),
            #: 兼容：Round 2.5 字段名保留为**同一物理量的镜像**（不是第二个真值）。
            "D_safety_m": d_separation,
            "V_relative_mps": relative_speed,
            "relative_speed_basis": speed_basis,
            "T_chain_s": t_chain,
            "T_chain_components_s": {
                name: _parameter_value(parameters, f"T_chain_{name}_s")
                for name in T_CHAIN_COMPONENTS
            },
            "D_protection_m": d_protection,
            "outer_half_width_m": (
                None if d_protection is None or half_width in (None, "")
                else float(half_width) + float(d_protection)
            ),
            "formula": ROUTE_PROTECTION_FORMULA,
            "status": (
                "evaluated" if d_protection is not None and route_path
                else "evidence_required" if route_path
                else "unknown"
            ),
        }

        # ---- 逐 service 的首次探测距离证据 ---------------------------------
        detection, detection_reason = _first_detection_evidence(
            corridor_route=corridor_route, gap_route=gap_route,
            device_catalog=device_catalog, radar_layout=radar_layout,
            aircraft_profile=aircraft_profile,
        )
        #: 几何一致性：声明的探测范围必须真的能覆盖到安全边界之外，否则该距离
        #: 不构成"首次探测距离"证据。服务几何与 D_separation 是两套独立输入，任一变化
        #: 都不应让另一个被静默忽略（不制造结果）。
        if (
            detection.get("usable")
            and d_separation not in (None, "")
            and detection.get("first_detection_distance_m") is not None
            and float(detection["first_detection_distance_m"]) < float(d_separation)
        ):
            detection = {
                **detection, "usable": False, "coverage_status": "not_satisfied",
                "not_usable_reason": (
                    "声明的探测范围小于分隔距离：无法在分隔边界之外完成探测-响应"
                ),
            }
            detection_reason = CONTINUOUS_SERVICE_REASONS["detection_range_below_safety_distance"]
            reason_codes.append("detection_range_below_safety_distance")
            reasons.append(detection_reason)
        chain_configured = t_chain is not None
        acceptance = surveillance_acceptance(
            detection.get("first_detection_distance_m") if detection.get("usable") else None,
            d_separation, relative_speed, t_chain,
        )
        if acceptance["status"] == "unacceptable":
            reason_codes.append("tmargin_negative")
            reasons.append(CONTINUOUS_SERVICE_REASONS["tmargin_negative"])
        elif acceptance["status"] == "unknown" and chain_configured:
            reason_codes.append("no_detection_evidence")
            #: 已经有更具体的原因（例如"探测范围小于安全边界"）时保留它，不覆盖成
            #: 泛化的"缺探测证据"。
            reasons.append(detection_reason or CONTINUOUS_SERVICE_REASONS["no_detection_evidence"])

        # ---- 逐子系统 ------------------------------------------------------
        subsystems = []
        events_by_kind: dict[str, list] = {
            "service_outage": [], "redundancy_degradation": [],
            "navigation_degradation": [], "surveillance_detection_gap": [],
        }
        for code in _SERVICE_SUBSYSTEMS:
            entry = next(
                (item for item in gap_route.get("subsystems") or []
                 if item.get("subsystem") == code), None,
            )
            subsystems.append(self._service_subsystem(
                code=code, entry=entry, parameters=parameters, limits=limits[code],
                route_speed=route_speed, reason_codes=reason_codes, reasons=reasons,
                events_by_kind=events_by_kind,
            ))
        subsystems.append(self._navigation_subsystem(
            gap_route=gap_route, parameters=parameters, reason_codes=reason_codes,
            reasons=reasons, events_by_kind=events_by_kind, requirement_set=requirement_set,
        ))
        subsystems.append(self._surveillance_corridor_subsystem(
            corridor_route=corridor_route, gap_route=gap_route, acceptance=acceptance,
            detection=detection, detection_reason=detection_reason,
            reason_codes=reason_codes, reasons=reasons, events_by_kind=events_by_kind,
        ))

        status = aggregate_acceptability([item["status"] for item in subsystems])
        subsystem_counts = subsystem_acceptability_counts(
            [item["status"] for item in subsystems]
        )
        managed_gaps = [
            gap for item in subsystems for gap in (item.get("managed_gaps") or [])
        ]
        all_events = [
            event for kind in events_by_kind for event in events_by_kind[kind]
        ]
        #: Round 2.6：监视威胁**分层**结论（合作 RID 主要 / 非合作 Radar 补充）。
        #: 两者分开评估、分开披露，绝不合并成一个 surveillance 结论。
        threat_layers = surveillance_threat_layers({"subsystems": subsystems})
        limitations = noncooperative_limitations(radar_layout)
        cooperative_status = layer_conclusion_status(threat_layers[PRIMARY_THREAT_LAYER])
        noncooperative_entry = threat_layers[SUPPLEMENTARY_THREAT_LAYER]
        noncooperative_status = layer_conclusion_status(noncooperative_entry)
        if limitations:
            #: Radar 求解不可行是**真实工程结论**，登记为能力限制；
            #: 它不改变主要威胁（合作无人机）的判定。
            noncooperative_status = "limitation"
        elif noncooperative_status == "not_applicable":
            #: 没有非合作监视证据既不是"满足"也不是"限制"：如实保持 unknown。
            noncooperative_status = "unknown"
        for item in limitations:
            item["route_id"] = route_id
        return {
            "route_id": route_id,
            "status": status,
            "plan_stage": plan_stage,
            "subsystem_acceptability_counts": subsystem_counts,
            "unacceptable_subsystems": sorted(
                f"{item['subsystem']}:{item.get('service')}"
                for item in subsystems if item["status"] == "unacceptable"
            ),
            "unknown_subsystems": sorted(
                f"{item['subsystem']}:{item.get('service')}"
                for item in subsystems if item["status"] == "unknown"
            ),
            "route_length_m": route_length,
            "route_speed_mps": route_speed,
            "corridor": corridor,
            "first_detection_evidence": detection,
            "surveillance_acceptance": acceptance,
            #: 分层结果：合作（主要威胁）/ 非合作（补充威胁）。
            "threat_layers": threat_layers,
            "primary_threat_layer": PRIMARY_THREAT_LAYER,
            "primary_threat_status": cooperative_status,
            "supplementary_threat_layer": SUPPLEMENTARY_THREAT_LAYER,
            "supplementary_threat_status": noncooperative_status,
            "supplementary_threat_is_limitation": bool(limitations),
            "limitations": limitations,
            "threat_layer_note": (
                "合作无人机（RID）是主要威胁、非合作无人机（Radar）是补充威胁；"
                "两者分开判定，补充威胁的能力限制不改变主要威胁的结论。"
            ),
            "subsystems": subsystems,
            "events": all_events,
            "events_by_kind": {
                kind: events_by_kind[kind]
                for kind in ("service_outage", "redundancy_degradation", "navigation_degradation", "surveillance_detection_gap")
            },
            "longest_events": {
                kind: _longest_event(events_by_kind[kind])
                for kind in ("service_outage", "redundancy_degradation", "navigation_degradation", "surveillance_detection_gap")
            },
            "managed_gaps": managed_gaps,
            "reasons": sorted(set(reasons)),
            "reason_codes": sorted(set(reason_codes)),
        }

    # -------------------------------------------------------------- C / S 服务
    def _service_subsystem(
        self, *, code, entry, parameters, limits, route_speed, reason_codes, reasons,
        events_by_kind,
    ):
        entry = entry or {}
        service_key = _primary_service_key(entry, code)
        events = []
        for segment in entry.get("continuous_deficit_segments") or []:
            causes = [str(item) for item in segment.get("causes") or []]
            bounds = _segment_bounds(segment)
            for cause, kind in _CAUSE_KINDS.items():
                if cause not in causes:
                    continue
                limit = limits[kind]
                duration = continuous_gap_duration(bounds["length_m"], route_speed)
                if duration is None:
                    reason_codes.append("no_route_speed")
                    reasons.append(CONTINUOUS_SERVICE_REASONS["no_route_speed"])
                event = {
                    "event_id": f"{segment.get('segment_id')}:{kind}",
                    "kind": kind,
                    "service": service_key,
                    "subsystem": code,
                    "route_id": segment.get("route_id"),
                    "start_offset_m": bounds["start_offset_m"],
                    "end_offset_m": bounds["end_offset_m"],
                    "position_basis": bounds["position_basis"],
                    "length_m": bounds["length_m"],
                    "duration_s": duration,
                    "duration_formula": "along_route_gap_m / current_route_speed_mps",
                    "route_speed_mps": route_speed,
                    "limit_s": limit,
                    "exceeds_limit": (
                        None if duration is None or limit is None
                        else bool(float(duration) > float(limit))
                    ),
                    "mitigation": _MITIGATION_BY_KIND.get(kind),
                    "basis": (
                        f"threshold={_KIND_LIMIT_PARAMETER[kind]}"
                        f"（authority={parameters[_KIND_LIMIT_PARAMETER[kind]]['authority']}）"
                    ),
                    "semantics": "conservative_longitudinal_projection_of_p15_confirmed_deficit",
                    "voxel_ids": deepcopy(segment.get("voxel_ids") or []),
                }
                events.append(event)
                events_by_kind[kind].append(event)
        status = subsystem_status_from_events(
            events=events, outage_limit_s=limits["service_outage"],
            degradation_limit_s=limits["redundancy_degradation"],
        )
        if status == "unknown":
            if any(limit is None for limit in limits.values()):
                #: 阈值本身缺失（不是缺口长度不可判定）：原因必须与"缺航路速度"区分开。
                reason_codes.append("no_outage_threshold_evidence")
                reasons.append(CONTINUOUS_SERVICE_REASONS["no_outage_threshold_evidence"])
            else:
                reason_codes.append("no_route_speed")
                reasons.append(CONTINUOUS_SERVICE_REASONS["no_route_speed"])
        if status == "unacceptable":
            for event in events:
                if event.get("exceeds_limit"):
                    key = (
                        "service_outage_exceeds_limit"
                        if event["kind"] == "service_outage"
                        else "redundancy_degradation_exceeds_limit"
                    )
                    reason_codes.append(key)
                    reasons.append(CONTINUOUS_SERVICE_REASONS[key])
                    break
        managed = [event for event in events if event.get("exceeds_limit") is False]
        return {
            "subsystem": code,
            "service": service_key,
            "status": status,
            "events": events,
            "event_count": len(events),
            "longest_event": _longest_event(events),
            "limits_s": dict(limits),
            "managed_gaps": managed,
            "p15_status": entry.get("status"),
            "p15_causes": deepcopy(entry.get("causes") or []),
            "service_redundancy": deepcopy(entry.get("service_redundancy") or []),
            "reasons": deepcopy(entry.get("reasons") or [])[:8],
        }

    # -------------------------------------------------------------------- 导航
    def _navigation_subsystem(
        self, *, gap_route, parameters, reason_codes, reasons, events_by_kind,
        requirement_set,
    ):
        entry = next(
            (item for item in gap_route.get("subsystems") or []
             if item.get("subsystem") == "N"), None,
        ) or {}
        rtk = _navigation_state(
            parameters, gap_route=gap_route, entry=entry, key="rtk_availability",
        )
        gnss = _navigation_state(
            parameters, gap_route=gap_route, entry=entry, key="gnss_availability",
        )
        containment = _parameter_value(parameters, "navigation_route_containment_accuracy_m")
        accuracy = _parameter_value(parameters, "gnss_accuracy_along_route_m")
        accuracy_basis = parameters.get("gnss_accuracy_along_route_m") or {}
        containment_basis = parameters.get("navigation_route_containment_accuracy_m") or {}

        events = []
        reasons_local: list[str] = []
        codes_local: list[str] = []
        if rtk["value"] == "available":
            if gnss["value"] in (None, "", "unknown"):
                status = "unknown"
                codes_local.append("navigation_evidence_missing")
                reasons_local.append(
                    "RTK 可用，但 GNSS 回退链的可用性证据缺失（回退能力无法确认）"
                )
            else:
                status = "nominal"
        elif rtk["value"] == "not_available":
            if gnss["value"] == "not_available":
                status = "unacceptable"
                codes_local.append("navigation_gnss_unavailable")
                reasons_local.append(CONTINUOUS_SERVICE_REASONS["navigation_gnss_unavailable"])
            elif gnss["value"] == "available":
                if containment in (None, "") or accuracy in (None, ""):
                    status = "unknown"
                    codes_local.append("navigation_evidence_missing")
                    reasons_local.append(
                        "GNSS 可用，但缺少精度证据，无法判定是否满足航路保持（"
                        + (containment_basis.get("reason") or "")
                        + "）"
                    )
                elif float(accuracy) <= float(containment):
                    status = "acceptable_degraded"
                else:
                    status = "unacceptable"
                    codes_local.append("navigation_gnss_unavailable")
                    reasons_local.append(
                        f"GNSS 可用但精度 {float(accuracy):.1f} m 超过航路保持要求 "
                        f"{float(containment):.1f} m，无法维持航路包含"
                    )
            else:
                status = "unknown"
                codes_local.append("navigation_evidence_missing")
                reasons_local.append(
                    "RTK 不可用且 GNSS 可用性证据缺失（禁止用固定时长阈值替代证据）"
                )
        else:
            status = "unknown"
            codes_local.append("navigation_evidence_missing")
            reasons_local.append(
                "RTK 可用性证据缺失（禁止以「RTK 断 X 秒＝失败」这种固定阈值替代证据）"
            )

        if status in ("acceptable_degraded", "unacceptable"):
            segment = _navigation_segment(gap_route, entry, route_speed=None)
            event = {
                "event_id": f"{gap_route.get('route_id')}:N:navigation_degradation",
                "kind": "navigation_degradation",
                "service": "N:route_containment",
                "subsystem": "N",
                "route_id": gap_route.get("route_id"),
                "start_offset_m": segment.get("start_offset_m"),
                "end_offset_m": segment.get("end_offset_m"),
                "length_m": segment.get("length_m"),
                "duration_s": None,
                "limit_s": None,
                "exceeds_limit": True if status == "unacceptable" else False,
                "mitigation": (
                    "立即恢复可靠导航（RTK 或满足精度的 GNSS）或执行受控降落；"
                    "该状态**不**提供航路保持"
                    if status == "unacceptable" else
                    "以 GNSS 回退维持航路包含（精度已逐项核对满足要求）"
                ),
                "basis": (
                    f"rtk={rtk['source_type']}；gnss={gnss['source_type']}；"
                    f"containment={containment_basis['source_type']}；accuracy={accuracy_basis['source_type']}"
                ),
                "semantics": "rtk_to_gnss_fallback_state_machine_not_fixed_duration_threshold",
            }
            events.append(event)
            events_by_kind["navigation_degradation"].append(event)

        reason_codes.extend(codes_local)
        reasons.extend(reasons_local)
        return {
            "subsystem": "N",
            "service": "N:route_containment",
            "status": status,
            "navigation_state": status,
            "rtk": rtk,
            "gnss": gnss,
            "route_containment_accuracy_m": containment,
            "gnss_accuracy_m": accuracy,
            "events": events,
            "event_count": len(events),
            "longest_event": _longest_event(events),
            "rtk_recovery_external_reference": _external_reference_note(),
            "managed_gaps": [event for event in events if event.get("exceeds_limit") is False],
            "reasons": reasons_local,
            "requirement_hint": requirement_set.get("N") if requirement_set else None,
        }

    # ------------------------------------------------------------------ 监视
    def _surveillance_corridor_subsystem(
        self, *, corridor_route, gap_route, acceptance, detection, detection_reason,
        reason_codes, reasons, events_by_kind,
    ):
        half_width = (corridor_route.get("corridor_geometry") or {}).get(
            "horizontal_half_width_m"
        )
        probe, probe_reason = _surveillance_protection_probe(
            corridor_route, gap_route, service_key=detection.get("service_key"),
        )
        if probe is None:
            reasons.append(probe_reason or CONTINUOUS_SERVICE_REASONS["no_detection_evidence"])
            reason_codes.append("no_detection_evidence")
        events = []
        #: 只有**判定不通过**时才登记"保护走廊内的连续监视覆盖缺口"事件：
        #: 余量充足时它只是被覆盖状态所掩盖的同一段距离，登记成"缺口事件"会把
        #: 已经完成的保护链分析说成未完成（本模块的核心命题是"gap ≠ 自动失败"）。
        if probe is not None and probe.get("length_m") and acceptance["status"] != "acceptable":
            event = {
                "event_id": f"{corridor_route.get('route_id')}:S:surveillance_detection_gap",
                "kind": "surveillance_detection_gap",
                "service": detection.get("service_key") or "S:surveillance",
                "subsystem": "S",
                "route_id": corridor_route.get("route_id"),
                "start_offset_m": probe["start_offset_m"],
                "end_offset_m": probe["end_offset_m"],
                "length_m": probe["length_m"],
                "duration_s": None,
                "limit_s": None,
                "exceeds_limit": True,
                "mitigation": (
                    "在保护走廊内补齐监视覆盖（含走廊边界），或缩短该段走廊；"
                    "缺口存在时不得声称「全覆盖」"
                ),
                "basis": probe.get("basis"),
                "semantics": "protection_corridor_coverage_gap_not_route_centerline_gap",
                "voxel_ids": deepcopy(probe.get("voxel_ids") or []),
            }
            events.append(event)
            events_by_kind["surveillance_detection_gap"].append(event)

        if acceptance["status"] == "unacceptable":
            status = "unacceptable"
        elif acceptance["status"] == "unknown":
            status = "unknown"
        elif events:
            #: 保护走廊内的连续监视覆盖缺口本身是**已确认的不可能完成探测-响应**。
            status = "unacceptable"
        else:
            #: 使用与 C/N/S 同一套子系统状态词汇（``nominal`` 而非 ``fully_satisfied``：
            #: 后者是**路线 / 全局**结论，混用会让聚合层与前端显示两套真值）。
            status = "nominal"
        return {
            "subsystem": "S",
            "service": detection.get("service_key") or "S:surveillance",
            "status": status,
            "acceptance": deepcopy(acceptance),
            "first_detection_evidence": deepcopy(detection),
            "protection_corridor_half_width_m": half_width,
            "events": events,
            "event_count": len(events),
            "longest_event": _longest_event(events),
            "managed_gaps": [],
            "reasons": [reason for reason in [detection_reason, probe_reason] if reason],
        }


# ---------------------------------------------------------------------------
# 场景 / 参数 / 限值
# ---------------------------------------------------------------------------


def _operation_scenario(value) -> dict:
    scenario = deepcopy(OPERATION_SCENARIO_DEFAULTS)
    if isinstance(value, dict):
        for key in ("single_ownship", "intruder_scope", "cruise_altitude",
                    "design_intruder_speed_mps", "nominal_closing_speed_mps",
                    "conservative_closing_speed_mps"):
            if value.get(key) is not None:
                scenario[key] = value[key]
    return scenario


def _parameter_value(parameters, name):
    """参数值（缺参数或 ``value=None`` ⇒ 返回 ``None``，绝不猜）。"""

    item = (parameters or {}).get(name)
    return item.get("value") if isinstance(item, dict) else None


def _parameter_authority(parameters, name):
    item = (parameters or {}).get(name)
    return item.get("authority") if isinstance(item, dict) else None


def _route_set_status(routes) -> str:
    """一组 route 结论 → 单层可接受性结论（含"空集／全部不适用"的显式区分）。"""

    status = aggregate_acceptability([route.get("status") for route in routes])
    if status == "not_applicable":
        return "fully_satisfied" if not routes else "unknown"
    return status


#: 威胁分层结论的严重度（后者覆盖前者；``limitation`` 略高于"有管理的缺口"）。
_THREAT_STATUS_SEVERITY = (
    "not_applicable", "satisfied", "acceptable_with_managed_gap",
    "limitation", "unknown", "unacceptable",
)


def _merged_threat_status(routes, layer) -> str:
    """把逐 route 的某个威胁分层结论合并为全局分层结论（fail-closed，取最差）。"""

    rank = {name: index for index, name in enumerate(_THREAT_STATUS_SEVERITY)}
    worst = None
    for route in routes or []:
        value = (
            route.get("primary_threat_status") if layer == PRIMARY_THREAT_LAYER
            else route.get("supplementary_threat_status")
            if layer == SUPPLEMENTARY_THREAT_LAYER
            else None
        )
        if value in (None, "not_applicable"):
            continue
        if worst is None or rank.get(value, 0) > rank.get(worst, 0):
            worst = value
    return worst or "not_applicable"


def _limit(parameters, code, kind, policy):
    """C/S 阈值解析：显式策略覆盖 → 显式工程证据 → 内置工程基线。

    顺序在 Round 2.5 是**契约**：空策略（默认）绝不压过用户在证据入口里显式登记的
    阈值；只有用户真的覆盖了策略/参数时，覆盖才生效。

    Round 2.6（用户裁定）：``C`` 的 **服务中断阈值**不再有内置默认值。
    FC30 的"遥控信号丢失 > 3 s 触发 Failsafe RTH"只是**设备 failsafe 事实**，
    不得自动成为规划阈值；用户未显式登记时返回 ``None`` ⇒ 事件时长不可判定
    ⇒ 该子系统 ``unknown``（fail-closed），绝不放行。
    """

    override = ((policy.get("service_acceptability_limits") or {}).get(code) or {}).get(kind)
    if override is not None:
        return float(override)
    override = (policy.get("parameter_overrides") or {}).get(_KIND_LIMIT_PARAMETER[kind])
    if override is not None:
        return float(override)
    if code == "C":
        name = _KIND_LIMIT_PARAMETER[kind]
        value = parameters.get(name, {}).get("value")
        if value is not None:
            return float(value)
        #: 显式证据缺失 ⇒ 该阈值不可判定（evidence_required）。
        if DEFAULT_SERVICE_ACCEPTABILITY_LIMITS[code][kind] is None:
            return None
    return DEFAULT_SERVICE_ACCEPTABILITY_LIMITS[code][kind]


def _parameter_projection(parameters):
    return {
        name: {
            "value": item.get("value"),
            "authority": item.get("authority"),
            "source_type": item.get("source_type"),
            "participating": item.get("participating"),
            "source": item.get("source"),
            "statement": item.get("statement"),
            "report_disclosure": item.get("report_disclosure"),
            "evidence_id": item.get("evidence_id"),
            "external_reference": item.get("external_reference"),
            "reason": item.get("reason"),
            "unit": item.get("unit"),
            "label": item.get("label"),
            #: Round 2.6：`D_separation_m` 可能读自 Round 2.5 旧字段名。
            "read_from_legacy_field": item.get("read_from_legacy_field"),
        }
        for name, item in sorted(parameters.items())
    }


def _policy_identity(policy):
    return {
        "status": policy.get("status"),
        "service_acceptability_limits": deepcopy(policy.get("service_acceptability_limits") or {}),
        "parameter_overrides": deepcopy(policy.get("parameter_overrides") or {}),
        "confirmed": policy.get("confirmed") is True,
    }


def _requirement_index(route_requirements):
    result = {}
    for entry in route_requirements or []:
        route_id = str(entry.get("route_id") or "")
        if not route_id:
            continue
        result[route_id] = deepcopy(entry.get("requirements") or {})
    return result


# ---------------------------------------------------------------------------
# 首次探测距离证据（逐 service，绝不猜）
# ---------------------------------------------------------------------------


def _first_detection_evidence(
    *, corridor_route, gap_route, device_catalog, radar_layout, aircraft_profile=None,
):
    """逐 service 计算"首次探测距离"候选，并给出可用性结论。

    ``first_detection_distance_m`` 的语义是**外边界探测距离**：
    监视服务在保护走廊外边界处能够探测到入侵者的距离。它按 service 取
    "已服务半径"（``coverage_geometry.radius_by_surface`` 按该 service 在航路上的
    实际 surface 解析）与机载协作监视性能（``min_detection_range_m``）中的较大者；
    **找不到任何显式几何时保持 unknown**。

    返回 ``(evidence, reason)``；``usable=False`` 表示该距离不得用于 T_margin 判定。
    """

    surfaces = _route_surface_counts(corridor_route)
    candidates = []
    for subsystem in gap_route.get("subsystems") or []:
        if subsystem.get("subsystem") != "S":
            continue
        for item in subsystem.get("service_redundancy") or []:
            service_key = str(item.get("service_key") or "")
            if not service_key:
                continue
            required = (item.get("required_distinct_site_count_by_surface") or {})
            actual = (item.get("distinct_site_count_by_surface") or {})
            serviced_surfaces = [
                surface for surface, count in actual.items()
                if count is not None and count > 0
            ]
            requirements_met = True
            for surface, count in required.items():
                if count is None:
                    requirements_met = False
                    break
                if (actual.get(surface) or 0) < count:
                    requirements_met = False
                    break
            geometry = _service_geometry(device_catalog, service_key, surfaces)
            candidates.append({
                "service_key": service_key,
                "required_distinct_site_count_by_surface": deepcopy(required),
                "distinct_site_count_by_surface": deepcopy(actual),
                "serviced_surfaces": sorted(serviced_surfaces),
                "requirements_met": requirements_met,
                "coverage_status": (
                    "satisfied" if requirements_met and serviced_surfaces
                    else "not_satisfied"
                ),
                #: ``radius_m`` / ``geometry_source`` 提升到候选顶层：可用性筛选与
                #: "取最远可用探测距离"都按顶层字段判定，避免服务候选被静默忽略。
                **geometry,
                "geometry": geometry,
            })
    airborne_detection = _airborne_detection_range(corridor_route)
    if airborne_detection is None:
        airborne_detection = _aircraft_detection_range(aircraft_profile)
    if airborne_detection is not None:
        candidates.append(airborne_detection)

    usable = [item for item in candidates if item.get("radius_m") not in (None, "")]
    if not usable:
        return (
            {
                "first_detection_distance_m": None,
                "service_key": None,
                "candidates": candidates,
                "usable": False,
                "coverage_status": "unknown",
                "source_type": "unknown",
                "authority": "unknown",
                "semantics": "first_detection_distance_from_declared_geometry_only",
            },
            CONTINUOUS_SERVICE_REASONS["no_detection_evidence"],
        )
    best = max(
        usable,
        key=lambda item: (
            #: 首要判据是"该探测几何是否真的可用"（站址冗余满足 / 机载自探测），
            #: 其次才是距离大小：可用的最远探测能力才是权威的首次探测距离。
            1 if item.get("coverage_status") == "satisfied" else 0,
            float(item["radius_m"]),
            str(item.get("service_key") or ""),
        ),
    )
    covered = best.get("coverage_status") == "satisfied"
    return (
        {
            "first_detection_distance_m": float(best["radius_m"]),
            "service_key": best.get("service_key"),
            "geometry_source": best.get("geometry_source"),
            "coverage_status": best.get("coverage_status"),
            "candidates": candidates,
            "usable": bool(covered),
            "source_type": best.get("source_type"),
            "authority": "declared_geometry",
            "semantics": "first_detection_distance_from_declared_geometry_only",
            "not_usable_reason": (
                None if covered else
                "监视服务在航路上的独立站址数量未满足要求：探测距离只是设备标称几何，"
                "不能据此宣称保护走廊已被覆盖（不制造结果）"
            ),
        },
        None if covered else CONTINUOUS_SERVICE_REASONS["no_detection_evidence"],
    )


def _service_geometry(device_catalog, service_key, surfaces):
    """从设备目录解析某个 service 的几何半径与来源（按 surface 取值，取最大）。"""

    radii = []
    source = None
    for device in (device_catalog or {}).get("items") or []:
        if str(device.get("service_key") or "") != service_key:
            continue
        geometry = device.get("coverage_geometry") or {}
        by_surface = geometry.get("radius_by_surface") or {}
        picked = [
            by_surface.get(surface) for surface in (surfaces or list(by_surface))
            if by_surface.get(surface) not in (None, "")
        ]
        if not picked:
            picked = [
                value for value in (geometry.get("slant_range_m"), device.get("radius_m"))
                if value not in (None, "")
            ]
        if picked:
            radii.append(float(max(picked)))
            source = source or geometry.get("source") or device.get("source")
    if not radii:
        return {"radius_m": None, "geometry_source": None, "source_type": "unknown"}
    return {
        "radius_m": max(radii),
        "geometry_source": source,
        "source_type": "declared_geometry",
        "semantics": "geometric_planning_radius_not_measured_coverage",
    }


def _airborne_detection_range(corridor_route):
    """机载协作监视（例如 ADS-B in）的 ``min_detection_range_m``（缺值 ⇒ 不列候选）。"""

    for voxel in (corridor_route.get("voxels") or [])[:1]:
        for subsystem in voxel.get("subsystems") or []:
            if subsystem.get("subsystem") != "S":
                continue
            for item in (subsystem.get("evidence") or []):
                value = item.get("aircraft_surveillance") or {}
                radius = value.get("min_detection_range_m")
                if radius not in (None, ""):
                    return {
                        "service_key": "S:airborne_cooperative",
                        "radius_m": float(radius),
                        "geometry_source": value.get("source"),
                        "source_type": "declared_geometry",
                        "coverage_status": "satisfied",
                        "required_distinct_site_count_by_surface": {},
                        "distinct_site_count_by_surface": {},
                        "serviced_surfaces": [],
                    }
    return None


def _aircraft_detection_range(aircraft_profile):
    """选定机载档案的**协作监视探测距离**（``surveillance.performance.min_detection_range_m``）。

    这是"机载自身能探测到多远"的声明事实（例如 ADS-B in 的最小探测距离）；缺值 ⇒ 不列候选。
    它**不**代表地面监视服务的覆盖，因此不进入站址冗余判定。
    """

    block = ((aircraft_profile or {}).get("surveillance") or {})
    performance = block.get("performance") if isinstance(block.get("performance"), dict) else {}
    radius = performance.get("min_detection_range_m")
    if radius in (None, ""):
        return None
    return {
        "service_key": "S:airborne_cooperative",
        "radius_m": float(radius),
        "geometry_source": block.get("source"),
        "source_type": "declared_geometry",
        #: 机载自身探测是"已声明几何 + 已声明能力"：它不参与地面站址冗余判定。
        "coverage_status": "satisfied" if float(radius) > 0 else "not_satisfied",
        "required_distinct_site_count_by_surface": {},
        "distinct_site_count_by_surface": {},
        "serviced_surfaces": [],
        "coverage_semantics": "airborne_own_sensor_declared_detection_range",
    }


def _route_surface_counts(corridor_route):
    counts = {}
    for voxel in corridor_route.get("voxels") or []:
        surface = str(voxel.get("surface_class") or "unknown")
        counts[surface] = counts.get(surface, 0) + 1
    if not counts:
        return []
    return [surface for surface, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def _primary_service_key(entry, code):
    items = entry.get("service_redundancy") or []
    keys = sorted({str(item.get("service_key")) for item in items if item.get("service_key")})
    if len(keys) == 1:
        return keys[0]
    if keys:
        return "、".join(keys)
    return {"C": "C:communication", "N": "N:navigation", "S": "S:surveillance"}.get(code, code)


# ---------------------------------------------------------------------------
# 保护走廊内的监视覆盖缺口（S）
# ---------------------------------------------------------------------------


def _surveillance_protection_probe(corridor_route, gap_route, *, service_key):
    """保护走廊内的连续监视覆盖缺口（纵向投影，单位 m）。"""

    service = next(
        (item for item in _surveillance_services(gap_route)
         if service_key in (None, "", item.get("service_key"))),
        None,
    )
    covered_by_surface = {}
    if service:
        for surface, count in (service.get("distinct_site_count_by_surface") or {}).items():
            covered_by_surface[surface] = bool(count)
    samples = []
    for voxel in corridor_route.get("voxels") or []:
        offset = voxel.get("nearest_route_offset_m")
        if offset is None:
            offset = (voxel.get("probe") or {}).get("distance_along_route_m")
        if offset is None:
            continue
        surface = str(voxel.get("surface_class") or "unknown")
        samples.append({
            "offset": float(offset),
            "half": float(voxel.get("cell_half_diagonal_m") or 0.0),
            "covered": covered_by_surface.get(surface, False),
            "voxel_id": voxel.get("voxel_id"),
        })
    if not samples:
        #: 没有任何可用采样点（例如 P14 的逐体元明细被压实或缺失）⇒ **不构造**缺口：
        #: 缺少位置事实时不能反过来宣称"整条保护走廊未被覆盖"，那是制造结果。
        return None, None
    samples.sort(key=lambda item: item["offset"])
    uncovered = [item for item in samples if not item["covered"]]
    if not uncovered:
        return None, None
    groups = []
    for item in uncovered:
        start = max(0.0, item["offset"] - item["half"])
        end = item["offset"] + item["half"]
        if groups and start <= groups[-1]["end"] + 1e-9:
            groups[-1]["end"] = max(groups[-1]["end"], end)
            groups[-1]["voxel_ids"].append(item["voxel_id"])
        else:
            groups.append({"start": start, "end": end, "voxel_ids": [item["voxel_id"]]})
    longest = max(groups, key=lambda item: item["end"] - item["start"])
    return {
        "start_offset_m": longest["start"],
        "end_offset_m": longest["end"],
        "length_m": longest["end"] - longest["start"],
        "gap_count": len(groups),
        "voxel_ids": [item for item in longest["voxel_ids"] if item],
        "basis": (
            "保护走廊内按 surface 的监视服务独立站址覆盖结论（P15 service_redundancy）"
        ),
    }, None


def _surveillance_services(gap_route):
    result = []
    for subsystem in gap_route.get("subsystems") or []:
        if subsystem.get("subsystem") != "S":
            continue
        result.extend(subsystem.get("service_redundancy") or [])
    return result


# ---------------------------------------------------------------------------
# 导航状态机辅助
# ---------------------------------------------------------------------------


def _navigation_state(parameters, *, gap_route, entry, key):
    """导航证据解析：显式证据优先；RTK 另有"航路内服务缺口"这一结构证据。"""

    item = dict(parameters.get(key) or {})
    value = item.get("value")
    explicit = item.get("authority") == "explicit_evidence"
    if not explicit and key == "rtk_availability":
        derived = _rtk_from_route_service(entry)
        if derived is not None:
            return {
                **item,
                "value": derived["value"],
                "authority": "derived_from_route_service_evidence",
                "source_type": "confirmed_source_fact",
                "source": derived["source"],
                "statement": derived["statement"],
                "reason": derived["reason"],
                "participating": True,
                "route_gap_service_key": derived["service_key"],
            }
    return item


def _rtk_from_route_service(entry):
    """航路内 RTK 增强服务的**结构性**证据（只有显式 ``N:rtk_augmentation`` 才算）。"""

    for item in entry.get("service_redundancy") or []:
        if str(item.get("service_key") or "") != "N:rtk_augmentation":
            continue
        counts = item.get("status_counts") or {}
        if counts.get("confirmed_deficit"):
            return {
                "value": "not_available",
                "service_key": "N:rtk_augmentation",
                "source": (
                    "P15 航路内 N:rtk_augmentation 服务证据：航路内存在已确认的基准站服务缺口"
                ),
                "statement": (
                    "P17 按 service 语义解读该缺口：航路内 RTK 增强不可用（RTK lost）；"
                    "是否可接受取决于 GNSS 回退链的证据，**不**采用「断 X 秒即失败」的固定阈值。"
                ),
                "reason": (
                    "航路内 RTK 增强服务存在已确认缺口 ⇒ 该段 RTK 视为不可用，"
                    "导航判定进入 RTK→GNSS 回退状态机"
                ),
            }
    return None


def _navigation_segment(gap_route, entry, *, route_speed):
    segments = entry.get("continuous_deficit_segments") or []
    if not segments:
        return {"start_offset_m": None, "end_offset_m": None, "length_m": None}
    longest = max(segments, key=lambda item: float(item.get("length_m") or 0.0))
    return {
        "start_offset_m": longest.get("start_offset_m"),
        "end_offset_m": longest.get("end_offset_m"),
        "length_m": longest.get("length_m"),
    }


def _external_reference_note():
    return {
        "reference": "RTK 恢复时长 9–13 s（外部研究参考）",
        "role": "external_reference_only_not_a_gate",
        "disclosure": (
            "该区间仅作为外部研究参考登记，**不**参与 P17 的硬门判定；"
            "P17 的导航判定只看 RTK/GNSS 可用性与精度证据。"
        ),
    }


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _segment_bounds(segment):
    """读取 P15 段边界：**按长度回退**，且绝不伪造缺失的位置。

    P15 的 ``continuous_deficit_segments`` 在"体素级明细被压实/未恢复"或"体素缺少
    ``nearest_route_offset_m``"时会给出 ``start/end = null`` —— 此时长度依然权威
    （``length_m`` 或 ``end - start``），P17 如实保留"位置未定"，而不是把 0 当成起点。
    """

    start = segment.get("start_route_offset_m", segment.get("start_offset_m"))
    end = segment.get("end_route_offset_m", segment.get("end_offset_m"))
    length = segment.get("length_m")
    basis = "p15_segment_bounds"
    if length in (None, "") and start not in (None, "") and end not in (None, ""):
        length = float(end) - float(start)
    if (start in (None, "") or end in (None, "")) and length not in (None, ""):
        basis = "p15_segment_length_only_start_unknown" if start in (None, "") else (
            "p15_segment_length_only_end_unknown"
        )
    return {
        "start_offset_m": None if start in (None, "") else float(start),
        "end_offset_m": None if end in (None, "") else float(end),
        "length_m": None if length in (None, "") else float(length),
        "position_basis": basis,
    }


def _longest_event(events):
    if not events:
        return None
    return max(
        (deepcopy(item) for item in events),
        key=lambda item: (float(item.get("length_m") or 0.0)),
    )


def _not_evaluated():
    return {
        name: "not_evaluated" for name in (
            "formal_continuity_probability", "common_cause", "shared_power",
            "shared_backhaul", "tower_failure", "runtime_outage",
            "regulatory_acceptability",
        )
    }


def _fingerprint(value):
    return sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, default=str,
    ).encode()).hexdigest()


__all__ = ["ContinuousServiceAcceptabilityV1"]
