"""Round 2.4 —— 工程证据 / 规划假设的应用服务（最小、可审计）。

职责边界：

* 只负责**登记 / 撤回**人工工程证据，以及把有效证据**叠加**到机载能力上；
* **绝不**写入 device catalog、aircraft profile 源文件或任何设备事实容器；
* **绝不**为缺失证据猜测取值（``source_type=unknown`` 的记录只登记缺口）；
* 任何写入都落在 ``project_state["planning_evidence"]``，因此随项目一起
  save / reopen，并且**重开后仍然是 assumption**（本模块不做任何升级）。
"""

from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

from ..domain.planning_evidence import (
    active_evidence_items, apply_aircraft_evidence, empty_planning_evidence,
    evidence_disclosure_lines, normalize_engineering_evidence,
    normalize_planning_evidence_registry, param_targets_continuous_service,
)


class PlanningEvidenceService:
    def __init__(self, session, invalidation, snapshot=None):
        self.session, self.invalidation = session, invalidation
        #: ``snapshot`` 是 workflow 快照投影（与其它业务服务同一契约）。
        #: **写命令的响应必须是完整 workflow 快照**，否则前端 `resourceAction`
        #: 会把局部对象当成状态落地，界面既不刷新也看不到错误。
        self.snapshot_fn = snapshot or (lambda: self.session.state)

    # ---- 读取 ---------------------------------------------------------------

    def snapshot(self):
        from ..domain.planning_evidence import continuous_service_parameter_projection

        state = self.session.state
        registry = normalize_planning_evidence_registry(state.get("planning_evidence"))
        return {
            "registry": deepcopy(registry),
            "active": active_evidence_items(registry),
            "disclosure_lines": evidence_disclosure_lines(registry),
            "aircraft_evidence": deepcopy(self.aircraft_evidence_summary()),
            "field_status": self.field_status(),
            #: Round 2.5：P17 的连续服务参数是**另一类**证据（kind =
            #: continuous_service_parameter），它们没有"需求侧字段"，因此不进入
            #: aircraft 逐项核对列表，而是单独下发一份带 authority 的投影。
            "continuous_parameters": continuous_service_parameter_projection(registry),
        }

    def field_status(self):
        """逐字段诊断：**需求侧要求什么 / 机载是否已声明 / 是否已补录工程证据**。

        这只是只读诊断，**不产生任何取值**；缺证据时 ``state`` 保持
        ``evidence_required``，供前端如实提示用户。
        """

        from ..domain.planning_evidence import (
            PLANNING_EVIDENCE_FIELDS, evidence_satisfies_requirement,
        )

        state = self.session.state
        profile = _selected_profile(state) or {}
        applied = aircraft_profile_with_evidence(state) or {}
        registry = normalize_planning_evidence_registry(state.get("planning_evidence"))
        entries = active_evidence_items(registry)
        requirements = _requirement_blocks(state)
        result = []
        for field, spec in PLANNING_EVIDENCE_FIELDS.items():
            #: Round 2.5：连续服务参数不是"机载能力 vs 需求类型"的核对对象，
            #: 它们由 ``continuous_parameters`` 单独投影（带 authority）。
            if spec["kind"] == "continuous_service_parameter":
                continue
            subsystem = _subsystem_name(spec["subsystem"])
            #: ``_requirement_blocks`` 返回的是 **subsystem 名** 键
            #: （``communication`` / ``navigation`` / ``surveillance``）。
            requirement = requirements.get(subsystem) or {}
            required_type = requirement.get("type") or {}
            demand_field = spec["demand_field"]
            required_value = required_type.get(demand_field)
            demanded = demand_field in required_type and required_value not in (
                None, "", "unknown", [],
            )
            declared = _declared_value(profile, subsystem, spec)
            effective = _declared_value(applied, subsystem, spec)
            recorded = next(
                (
                    item for item in entries
                    if item["field"] == field and item["scope"] == "aircraft"
                ),
                None,
            )
            #: **逐项核对**：只判断"有没有值"会把"明确冲突"误报成满足。
            matches, check_reason = evidence_satisfies_requirement(
                field, effective, required_value, spec,
            )
            if not demanded:
                status = "not_required"
            elif matches is True:
                status = "satisfied"
            elif matches is False:
                #: 真实不兼容（例如机载接口与地面提供者无交集）：必须如实报出，
                #: 不能被降级成"缺证据"。
                status = "incompatible"
            else:
                status = "evidence_required"
            result.append({
                "field": field,
                "label": spec["label"],
                "subsystem": spec["subsystem"],
                "value_type": spec["value_type"],
                "allowed": list(spec["allowed"]),
                "demand_field": demand_field,
                "semantics": spec["semantics"],
                "required_value": required_value,
                "demanded_by_requirement": demanded,
                "declared_by_aircraft_profile": declared,
                "effective_value": effective,
                "matches_requirement": matches,
                "status_reason": check_reason,
                "recorded_evidence_id": (recorded or {}).get("evidence_id"),
                "recorded_source_type": (recorded or {}).get("source_type"),
                "status": status,
            })
        return result

    def aircraft_evidence_summary(self):
        state = self.session.state
        profile = _selected_profile(state)
        applied = aircraft_profile_with_evidence(state)
        return (applied or {}).get("airborne_evidence")

    # ---- 写入 ---------------------------------------------------------------

    def add(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("工程证据请求必须是对象")
        raw = payload.get("planning_evidence", payload.get("evidence", payload))
        if not isinstance(raw, dict):
            raise ValueError("planning_evidence 必须是对象")
        item = dict(raw)
        item.setdefault("evidence_id", f"PEV-{uuid4().hex[:12].upper()}")
        normalized = normalize_engineering_evidence(item)

        if normalized["scope"] == "aircraft":
            state = self.session.state
            known = {
                str(entry.get("aircraft_id") or "")
                for entry in (state.get("aircraft_profiles") or {}).get("items") or []
            }
            if normalized["target_id"] not in known:
                raise ValueError(
                    f"target_id 不是已载入的机载档案：{normalized['target_id']}"
                )

        state = self.session.state
        registry = normalize_planning_evidence_registry(state.get("planning_evidence"))
        #: 同一 ``(scope, target_id, field)`` 只保留最新一条 active 记录：
        #: 其余同类记录被置为 ``superseded``（绝不累积互相矛盾的规划声明）。
        retained = []
        for existing in registry["items"]:
            if (
                existing["scope"] == normalized["scope"]
                and existing["target_id"] == normalized["target_id"]
                and existing["field"] == normalized["field"]
                and existing["evidence_id"] != normalized["evidence_id"]
                and existing["status"] == "active"
            ):
                existing = {**existing, "status": "superseded"}
            if existing["evidence_id"] == normalized["evidence_id"]:
                continue
            retained.append(existing)
        retained.append(normalized)
        registry["items"] = retained
        state["planning_evidence"] = registry
        #: 工程证据改变机载能力 ⇒ 下游能力/走廊/缺口/站址规划全部失效。
        #: Round 2.5 例外：**连续服务参数**不参与机载能力叠加，它只让 P17 失效，
        #: 绝不动 P8/P14/P15/P16（否则改一个阈值就要重跑 360 s 的 P16）。
        if param_targets_continuous_service(normalized["scope"], normalized["field"]):
            self.invalidation.continuous_service("continuous_service_parameters_changed")
        else:
            self.invalidation.workflow("aircraft_profile")
        #: 响应必须是**完整 workflow 快照**：前端 `resourceAction` 会把 POST 响应
        #: 当作状态落地。返回服务自己的聚合对象会让 `flow` 被替换成不含 project /
        #: workspace 的对象，界面既不刷新也看不到错误（BUG-PLANNING-EVIDENCE-001）。
        self.session.save()
        return self.snapshot_fn()

    def withdraw(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("工程证据撤回请求必须是对象")
        evidence_id = str(payload.get("evidence_id") or "").strip()
        if not evidence_id:
            raise ValueError("evidence_id 必填")
        state = self.session.state
        registry = normalize_planning_evidence_registry(state.get("planning_evidence"))
        target = None
        for item in registry["items"]:
            if item["evidence_id"] == evidence_id:
                item["status"] = "withdrawn"
                target = item
                break
        if target is None:
            raise KeyError(evidence_id)
        state["planning_evidence"] = registry
        if param_targets_continuous_service(target["scope"], target["field"]):
            self.invalidation.continuous_service("continuous_service_parameters_changed")
        else:
            self.invalidation.workflow("aircraft_profile")
        self.session.save()
        return self.snapshot_fn()

    # ---- 内部 ---------------------------------------------------------------


def _selected_profile(state):
    from ..catalogs import AircraftCNSProfileCatalog

    return AircraftCNSProfileCatalog.find(
        state.get("aircraft_profiles") or {},
        state.get("selected_aircraft_profile_id") or "",
    )


def _requirement_blocks(state):
    """当前生效的需求块（route override 优先，其次 project default）。"""

    required = state.get("required_cns") or {}
    route_ids = [str(item.get("route_id") or "") for item in state.get("operational_routes") or []]
    overrides = required.get("route_overrides") or {}
    for route_id in route_ids:
        block = overrides.get(route_id)
        if isinstance(block, dict) and block:
            return block
    return required.get("project_default") or {}


def _declared_value(profile, subsystem, spec):
    """读取机载档案里承载该字段的值（缺失时为 ``None``，绝不推断）。"""

    block = (profile or {}).get(subsystem) or {}
    type_block = block.get("type") if isinstance(block.get("type"), dict) else {}
    if spec["kind"] == "aircraft_type_field":
        return type_block.get(spec["demand_field"])
    if spec["kind"] == "aircraft_type_interfaces":
        return type_block.get("interfaces")
    if spec["kind"] == "airborne_cooperative_surveillance_services":
        from ..domain.cns_service_contract import cooperative_surveillance_declarations

        return [
            item.get("technology")
            for item in cooperative_surveillance_declarations(type_block)
        ] or None
    return None


def _subsystem_name(code):
    return {"C": "communication", "N": "navigation", "S": "surveillance"}.get(code, code)


def aircraft_profile_with_evidence(state):
    """**所有规划消费点唯一的机载档案入口**。

    P8（``cns_service_capability``）/ P14（``corridor``）/ P16 what-if 重算
    都必须经此取得机载能力，否则工程证据会在某条路径上被静默忽略，
    "同一份输入应当得到同一份判定"就不成立。
    """

    profile = _selected_profile(state)
    if profile is None:
        return None
    return apply_aircraft_evidence(profile, state.get("planning_evidence"))


__all__ = ["PlanningEvidenceService", "aircraft_profile_with_evidence"]
