"""Round 2.6 —— **P16 方案实施后的投影态** 构建器（唯一实现）。

核心命题（用户裁定）
--------------------

P17 连续服务可接受性**必须**评估 **P16 方案实施后的 projected state**，而不是当前
ExistingCNS。原来的错误逻辑是::

    existing state → P17

本轮改为::

    existing state + P16 selected_actions
        → projected CNS state（hypothetical / 绝不写入 ExistingCNS）
        → 重新评估必要的 P14 / P15 service state
        → P17（baseline + post_plan 两层）
        → Step6

职责边界
--------

* 本模块**只构建投影**（假想态），绝不写 ``existing_cns_facilities``、绝不写任何
  上游容器、绝不保存项目；
* 投影的设施应用语义与 P16 cumulative what-if **完全一致**
  （``_apply_cumulative_action`` 是唯一实现，绝不出现第二套假设）；
* 投影所需的机载档案**必须与正式 P14 同源**（``aircraft_profile_with_evidence``），
  否则投影态会用一个与权威状态不同的机务假设，造成"投影比基线更宽松/更严格"的假象；
* ``confirmation``/``apply`` 之前，投影态**绝不**写进真实 ExistingCNS —— 这是本模块的
  第一原则，也是 Step6 门禁必须基于投影结论而不是现网结论的原因。
"""

from __future__ import annotations

from copy import deepcopy

from ..domain.cns_service_contract import SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION
from ..domain.cns_service_registry import planner_family_for
from ..domain.navigation_augmentation import build_navigation_service_evidence
from ..domain.navigation_middle_reuse import POLICY_ID as NAVIGATION_MIDDLE_PLANNER_FAMILY
from ..domain.radar_service_evidence import radar_what_if_service_evidence
from .navigation_reference_station_planning_service import hypothetical_navigation_sites

#: 投影态的显式语义（前端 / 报告逐字使用）。
PROJECTION_SEMANTICS = (
    "post_plan_projection_hypothetical_state_never_written_into_existing_cns"
)

#: P16 / P18 共用同一条设施应用语义。
PLANNER_FAMILY_DIRECTIONAL_RADAR = "directional_radar"
NAVIGATION_PLANNER_FAMILY = planner_family_for(SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION)


def project_facilities(existing, actions):
    """把一组 action 累积应用到**工作副本**的既有设施集合。

    与 P16 cumulative what-if **逐字段一致**（直接复用
    :func:`...corridor_site_planning_service._apply_cumulative_action`），
    因此"P16 的内部投影"与"P17 消费的投影"不会漂移成两套假设。
    """

    from .corridor_site_planning_service import _apply_cumulative_action

    facilities = deepcopy(existing or {})
    facilities.setdefault("items", [])
    for action in sorted(
        actions or [], key=lambda item: str(item.get("action_id") or "")
    ):
        family = action.get("planner_family")
        if family in (
            PLANNER_FAMILY_DIRECTIONAL_RADAR, NAVIGATION_PLANNER_FAMILY,
            NAVIGATION_MIDDLE_PLANNER_FAMILY,
        ):
            #: 定向雷达、导航站与 typed middle N 动作**绝不**作为普通设施累加：
            #: 它们分别经 Radar / 导航 / 冻结 NM3 supplement 进入 P14（与 P16
            #: what-if 同一口径）。
            continue
        facilities = _apply_cumulative_action(facilities, action)
    return facilities


def split_plan_actions(actions):
    """把 action 拆成 ``(facilities, radar, navigation, middle)`` 四组。

    typed middle N 动作必须有**显式**分支；否则它会落入默认设施桶，被
    ``project_facilities`` 写成假想设施，污染 ``written_into_existing_cns``。
    """

    facilities, radar, navigation, middle = [], [], [], []
    for action in actions or []:
        family = action.get("planner_family")
        if family == PLANNER_FAMILY_DIRECTIONAL_RADAR:
            radar.append(deepcopy(action))
        elif family == NAVIGATION_PLANNER_FAMILY:
            navigation.append(deepcopy(action))
        elif family == NAVIGATION_MIDDLE_PLANNER_FAMILY:
            middle.append(deepcopy(action))
        else:
            facilities.append(deepcopy(action))
    return facilities, radar, navigation, middle


def projected_already_applied(actions):
    """按 action_id 判定该 action 是否**已经**存在于现网（Apply 之后再投影会重复计入）。"""

    return sorted(
        str(item.get("action_id") or "") for item in actions or []
        if str(item.get("status") or "") in ("applied", "already_installed")
    )


def _project_middle_supplement(state, middle_actions):
    """Replay P16 typed middle actions on the frozen baseline responsibilities.

    P17 投影**不重新评价** NM3，也不重新生成第二份 ``nav-planning-`` 身份：它只消费
    P16 canonical 结果里冻结的 baseline responsibilities + candidate_actions，并继续
    通过既有 ``dispatch_middle_actions`` 累计。任何候选缺失 / 资格拒绝都 fail-closed
    为 ``available=False``，绝不让 P17 消费一份未经本次评价的动作。
    """

    base = (state.get("cns_corridor_assessment") or {}).get(
        "navigation_integrity_supplement"
    )
    if not middle_actions:
        return {
            "available": False,
            "unavailable_reason": "no_middle_actions",
            "written_into_existing_cns": False,
            "persisted_as_upstream": False,
        }
    from .result_currentness import projected_result

    plan = projected_result(state, "cns_corridor_site_plan") or {}
    block = plan.get("navigation_integrity_middle")
    unavailable = {
        "available": False,
        "applied_action_ids": [
            str(item.get("action_id")) for item in middle_actions
        ],
        "written_into_existing_cns": False,
        "persisted_as_upstream": False,
    }
    #: 旧 / stale 的 P16 middle 块**不是**当前证据：绝不把旧结果投影成新结论。
    if plan.get("status") in (None, "stale", "not_calculated", "missing_data"):
        return {
            **unavailable,
            "unavailable_reason": "p16_middle_state_not_current",
        }
    if not isinstance(block, dict) or block.get("available") is not True:
        return {
            **unavailable,
            "unavailable_reason": "p16_middle_state_unavailable",
        }
    from .navigation_planning_integration import dispatch_middle_actions

    candidates = [
        deepcopy(item) for item in plan.get("candidate_actions") or []
        if item.get("planner_family") == NAVIGATION_MIDDLE_PLANNER_FAMILY
    ]
    by_id = {str(item.get("action_id")): item for item in candidates}
    rows = deepcopy(block.get("baseline_responsibilities") or [])
    residual = deepcopy(block.get("baseline_residual_gaps") or [])
    applied_ids = []
    for action in middle_actions:
        candidate = by_id.get(str(action.get("action_id")))
        if candidate is None:
            return {
                **unavailable,
                "unavailable_reason": "middle_action_not_in_frozen_candidates",
            }
        try:
            applied = dispatch_middle_actions(rows, [candidate], candidates)
        except ValueError as exc:
            return {
                **unavailable,
                "unavailable_reason": "middle_action_qualification_rejected",
                "reason": str(exc),
            }
        rows = applied["responsibilities"]
        residual = applied["residual_gaps"]
        applied_ids.append(str(action.get("action_id")))
    supplement = deepcopy(block.get("supplement") or base or {})
    if not isinstance(supplement, dict) or not supplement:
        return {
            **unavailable,
            "unavailable_reason": "middle_supplement_missing",
        }
    supplement["responsibilities"] = deepcopy(rows)
    supplement["residual_gaps"] = deepcopy(residual)
    return {
        "available": True,
        "applied_action_ids": applied_ids,
        "baseline_responsibilities": deepcopy(
            block.get("baseline_responsibilities") or []
        ),
        "baseline_residual_gaps": deepcopy(
            block.get("baseline_residual_gaps") or []
        ),
        "responsibilities": deepcopy(rows),
        "residual_gaps": deepcopy(residual),
        "middle_gap_count": len(residual),
        "provider_semantics": "hypothetical:",
        "written_into_existing_cns": False,
        "persisted_as_upstream": False,
        "supplement": supplement,
        "supplement_fingerprint": supplement.get("input_fingerprint"),
        "facility_authority": supplement.get("facility_authority"),
        "proposal_only": True,
        "actual_delivery": "unknown",
        "canonical_published": False,
    }


class PlanProjectionBuilder:
    """构建 ``state + actions`` 的投影态 P14 / P15 / P17 输入。

    ``corridor_model_prototype`` / ``corridor_gap_prototype`` 由组合根注入（与正式
    P14 / P15 同一个原型），因此投影使用的算法版本与权威链路永远一致。
    """

    def __init__(self, session, corridor_model_prototype, corridor_gap_prototype):
        self.session = session
        self.corridor_model = corridor_model_prototype
        self.corridor_gap = corridor_gap_prototype

    # ------------------------------------------------------------------ 投影态
    def project(self, actions, *, projection_id="p16_selected_actions",
                original_action_ids=None):
        """构建投影态；``actions`` 为空时返回 ``available=False``（绝不冒充投影）。

        返回结构（JSON-safe）：

        ``available``                本投影是否真的可评估
        ``unavailable_reason``       不可评估的原因码
        ``corridor_assessment``      投影态 P14
        ``corridor_gap_assessment``  投影态 P15
        ``device_catalog``           投影态的设施语义合并后的设备目录（若有新增设备）
        ``radar_surveillance_layout``投影态 Radar 视图
        ``applied_action_ids``       本次投影实际纳入的 action
        ``skipped_action_ids``       被跳过的 action（附原因）
        """

        from .corridor_site_planning_service import rerun_corridor_chain

        state = self.session.state
        selected = list(actions or [])
        if not selected:
            return {
                "available": False,
                "unavailable_reason": "no_selected_actions",
                "projection_semantics": PROJECTION_SEMANTICS,
                "applied_action_ids": [],
                "skipped_action_ids": [],
                "persisted_as_upstream": False,
            }

        facilities = project_facilities(state.get("existing_cns_facilities") or {}, selected)
        (
            _facility_actions, radar_actions, navigation_actions, middle_actions,
        ) = split_plan_actions(selected)

        #: typed middle N 动作：消费 P16 冻结的 baseline responsibilities + 候选，
        #: 经既有 ``dispatch_middle_actions`` 累计；不重新评价 NM3、不生成第二份身份。
        navigation_integrity_projection = _project_middle_supplement(
            state, middle_actions,
        )
        navigation_supplement = (
            navigation_integrity_projection.get("supplement")
            if navigation_integrity_projection.get("available") is True else None
        )

        radar_evidence = None
        if radar_actions:
            radar_evidence = radar_what_if_service_evidence(
                state.get("required_cns") or {},
                state.get("radar_surveillance_layout") or {},
                radar_actions,
            )
        navigation_evidence = None
        if navigation_actions:
            #: 与正式 P14 同一口径：只有显式要求该服务时才构造证据；随后叠加假想站址。
            navigation_evidence = build_navigation_service_evidence(
                state.get("required_cns") or {},
                route_ids=[
                    item.get("route_id") for item in state.get("operational_routes") or []
                ],
                existing_facilities=state.get("existing_cns_facilities") or {},
                candidate_sites=state.get("candidate_sites") or {},
                tower_colocation=state.get("tower_colocation_candidates") or {},
            )
            if navigation_evidence is not None:
                _, navigation_evidence = hypothetical_navigation_sites(
                    navigation_evidence, navigation_actions,
                )

        #: 投影重算必须与正式 P14 **同源**：``rerun_corridor_chain`` 内部会用
        #: ``aircraft_profile_with_evidence(state)`` 解析同一份机务叠加，因此这里直接
        #: 传权威 state，绝不另造机务假设。
        corridor, gap = rerun_corridor_chain(
            state, self.corridor_model, self.corridor_gap,
            facilities,
            radar_service_evidence=radar_evidence,
            navigation_service_evidence=navigation_evidence,
            #: 投影态必须消费与 P16 同一份冻结 typed middle supplement，否则 P15 的
            #: typed gaps / P17 披露会与 P16 选择漂移。
            navigation_integrity_supplement=navigation_supplement,
        )
        return {
            "available": True,
            "projection_id": projection_id,
            "applied_action_ids": sorted(
                str(item.get("action_id") or "") for item in selected
            ),
            "original_action_ids": deepcopy(
                original_action_ids
                if original_action_ids is not None
                else [str(item.get("action_id") or "") for item in selected]
            ),
            "skipped_action_ids": [],
            "facility_action_count": len(_facility_actions),
            "radar_action_count": len(radar_actions),
            "navigation_action_count": len(navigation_actions),
            "applied_facilities": deepcopy(facilities),
            "radar_service_evidence": deepcopy(radar_evidence),
            "navigation_service_evidence": deepcopy(navigation_evidence),
            "corridor_assessment": corridor,
            "corridor_gap_assessment": gap,
            "projected_corridor_fingerprint": corridor.get("input_fingerprint"),
            "projected_corridor_gap_fingerprint": gap.get("input_fingerprint"),
            "projection_semantics": PROJECTION_SEMANTICS,
            "persisted_as_upstream": False,
            "requires_user_confirmation_and_apply": True,
            "written_into_existing_cns": False,
            **(
                {
                    "middle_action_count": len(middle_actions),
                    "navigation_integrity_projection": navigation_integrity_projection,
                }
                if middle_actions else {}
            ),
        }

    # ------------------------------------------------------- P17 评估输入封装
    def p17_projection_input(self, actions, **kwargs):
        """P17 评估器直接消费的投影输入（只保留评估需要的键）。

        Round 30-C2A：post-plan 层必须与 baseline 消费**同一份**当前权威 Radar 规划
        artifact。Radar 的规划 authority 是 ``radar_surveillance_layout``，它**不是**
        由 P16 ``selected_actions`` 假想实施出来的设施（Radar 面板绝不写入
        ExistingCNS），因此这里把权威 layout 的只读快照显式带入 P17 投影输入：
        两层使用同一门控与同一数值来源，绝不出现"投影层另造一份 Radar 证据"。

        该键只在**评估输入**里出现（P17 的 ``post_plan_projection`` 输出块会原样
        披露它），它不写入任何 state 容器，也不改变 ``written_into_existing_cns``
        与 ``persisted_as_upstream`` 的既有语义（两者恒为 False）。
        """

        projection = self.project(actions, **kwargs)
        if projection.get("available") is not True:
            return projection
        projection = {
            **projection,
            "radar_surveillance_layout": deepcopy(
                self.session.state.get("radar_surveillance_layout") or {}
            ),
        }
        return {
            key: projection[key]
            for key in (
                "available", "unavailable_reason", "projection_id",
                "applied_action_ids", "original_action_ids", "skipped_action_ids",
                "corridor_assessment", "corridor_gap_assessment",
                "projected_corridor_fingerprint", "projected_corridor_gap_fingerprint",
                "projection_semantics", "persisted_as_upstream",
                "radar_service_evidence", "navigation_service_evidence",
                #: Round 30-C2A：P17 的 post-plan 层与 baseline 共享同一份权威
                #: Radar 规划 artifact（只读消费，绝不写入 ExistingCNS）。
                "radar_surveillance_layout",
                #: P17 投影态披露 typed middle 的动作 / 剩余职责 / 冻结 supplement。
                "navigation_integrity_projection", "middle_action_count",
            )
            if key in projection
        }
