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
        if family in (PLANNER_FAMILY_DIRECTIONAL_RADAR, NAVIGATION_PLANNER_FAMILY):
            #: 定向雷达与导航站**绝不**作为普通设施累加：它们分别经 Radar / 导航
            #: 服务证据进入 P14（与 P16 what-if 同一口径）。
            continue
        facilities = _apply_cumulative_action(facilities, action)
    return facilities


def split_plan_actions(actions):
    """把 action 拆成 ``(facilities_actions, radar_actions, navigation_actions)``。"""

    facilities, radar, navigation = [], [], []
    for action in actions or []:
        family = action.get("planner_family")
        if family == PLANNER_FAMILY_DIRECTIONAL_RADAR:
            radar.append(deepcopy(action))
        elif family == NAVIGATION_PLANNER_FAMILY:
            navigation.append(deepcopy(action))
        else:
            facilities.append(deepcopy(action))
    return facilities, radar, navigation


def projected_already_applied(actions):
    """按 action_id 判定该 action 是否**已经**存在于现网（Apply 之后再投影会重复计入）。"""

    return sorted(
        str(item.get("action_id") or "") for item in actions or []
        if str(item.get("status") or "") in ("applied", "already_installed")
    )


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
        _facility_actions, radar_actions, navigation_actions = split_plan_actions(selected)

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
        }

    # ------------------------------------------------------- P17 评估输入封装
    def p17_projection_input(self, actions, **kwargs):
        """P17 评估器直接消费的投影输入（只保留评估需要的键）。"""

        projection = self.project(actions, **kwargs)
        if projection.get("available") is not True:
            return projection
        return {
            key: projection[key]
            for key in (
                "available", "unavailable_reason", "projection_id",
                "applied_action_ids", "original_action_ids", "skipped_action_ids",
                "corridor_assessment", "corridor_gap_assessment",
                "projected_corridor_fingerprint", "projected_corridor_gap_fingerprint",
                "projection_semantics", "persisted_as_upstream",
                "radar_service_evidence", "navigation_service_evidence",
            )
            if key in projection
        }
