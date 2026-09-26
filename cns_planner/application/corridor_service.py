"""Application use case for persisted P14 corridor policy and assessment.

Phase4-B6X 把"计算"与"写入"拆成两段，**同步语义完全不变**：

* :meth:`CNSCorridorService.compute_input` / :meth:`compute` —— 纯计算，不写 state；
  供 heavy task worker 在**独立进程**里调用。
* :meth:`CNSCorridorService.apply_computed` —— 唯一写入路径：写
  ``cns_corridor_assessment``、跑失效、更新 ``result_statuses``、``session.save()``。
* :meth:`CNSCorridorService.evaluate` —— 原来的同步入口，行为与拆分前逐字等价
  （「先 evaluate、后取 snapshot」的既有契约保持不变，因此同步返回的是**上一次**
  的评估结果，而不是刚算出来的新结果）。
"""

from __future__ import annotations

from copy import deepcopy

from ..catalogs import AircraftCNSProfileCatalog
from ..domain.cns_corridor import normalize_cns_corridor_policy


class CNSCorridorService:
    def __init__(self, session, model, invalidation, snapshot):
        self.session, self.model = session, model
        self.invalidation, self.snapshot = invalidation, snapshot

    def result_snapshot(self):
        return deepcopy(self.session.state.get("cns_corridor_assessment") or self.model.empty())

    # ---- B6X：计算（纯） -----------------------------------------------------

    def compute_input(self, payload=None):
        """归一化策略并组装 ``model.evaluate`` 的完整输入（不写 state）。"""

        state = self.session.state
        raw_policy = payload.get("cns_corridor_policy", payload.get("policy")) if isinstance(payload, dict) else None
        policy = (
            normalize_cns_corridor_policy(raw_policy)
            if raw_policy is not None
            else state.get("cns_corridor_policy") or normalize_cns_corridor_policy()
        )
        profile = AircraftCNSProfileCatalog.find(
            state.get("aircraft_profiles") or {},
            state.get("selected_aircraft_profile_id") or "",
        )
        selections = state.get("algorithm_selection") or {}
        return {
            "routes": state.get("operational_routes") or [],
            "spatial_3d": state.get("spatial_3d") or {},
            "grid": state.get("grid") or {},
            "grid_attributes": state.get("grid_attributes") or {},
            "required_cns": state.get("required_cns") or {},
            "aircraft_profile": profile,
            "existing_facilities": state.get("existing_cns_facilities") or {},
            "device_catalog": state.get("device_catalog") or {},
            "corridor_policy": policy,
            "coverage_parameters": ((selections.get("coverage_model") or {}).get("parameters") or {}),
            "capability_parameters": ((selections.get("service_model") or {}).get("parameters") or {}),
        }

    def compute(self, inputs):
        """纯计算：返回 canonical assessment result（不写 state）。"""

        return self.model.evaluate(
            inputs.get("routes") or [], inputs.get("spatial_3d") or {},
            inputs.get("grid") or {}, inputs.get("grid_attributes") or {},
            inputs.get("required_cns") or {}, inputs.get("aircraft_profile") or {},
            inputs.get("existing_facilities") or {}, inputs.get("device_catalog") or {},
            inputs.get("corridor_policy") or {},
            coverage_parameters=inputs.get("coverage_parameters") or {},
            capability_parameters=inputs.get("capability_parameters") or {},
        )

    # ---- B6X：唯一写入路径 ---------------------------------------------------

    def apply_policy_input(self, payload=None):
        """把请求里的 corridor policy 落到 state（这是用户输入，不是派生结果）。"""

        state = self.session.state
        raw_policy = payload.get("cns_corridor_policy", payload.get("policy")) if isinstance(payload, dict) else None
        if raw_policy is None:
            return None
        policy = normalize_cns_corridor_policy(raw_policy)
        if policy != state.get("cns_corridor_policy"):
            state["cns_corridor_policy"] = policy
            self.invalidation.cns_corridor()
        return policy

    def apply_computed(self, result, *, progress=None):
        """唯一写入路径：调用方（同步 use case 或 heavy task publish 阶段）已持锁。"""

        state = self.session.state
        state["cns_corridor_assessment"] = result
        self.invalidation.cns_corridor_gap()
        state.setdefault("result_statuses", {})["cns_corridor_assessment"] = _result_status(
            (result or {}).get("status")
        )
        self.session.save()
        if progress is not None:
            progress("服务走廊正式结果已发布")
        return self.snapshot()

    def result_artifact_ref(self):
        """B5X：服务走廊逐 cell 明细的 canonical artifact 引用（只含相对路径）。"""

        from ..persistence.project_compaction import artifact_reference_for

        return artifact_reference_for(self.session.state, "cns_corridor_assessment")

    # ---- 同步入口（行为不变） ------------------------------------------------

    def evaluate(self, payload=None):
        self.apply_policy_input(payload)
        state = self.session.state
        policy = state.get("cns_corridor_policy") or normalize_cns_corridor_policy()
        profile = AircraftCNSProfileCatalog.find(
            state.get("aircraft_profiles") or {},
            state.get("selected_aircraft_profile_id") or "",
        )
        selections = state.get("algorithm_selection") or {}
        result = self.model.evaluate(
            state.get("operational_routes") or [], state.get("spatial_3d") or {},
            state.get("grid") or {}, state.get("grid_attributes") or {},
            state.get("required_cns") or {}, profile,
            state.get("existing_cns_facilities") or {}, state.get("device_catalog") or {},
            policy,
            coverage_parameters=((selections.get("coverage_model") or {}).get("parameters") or {}),
            capability_parameters=((selections.get("service_model") or {}).get("parameters") or {}),
        )
        state["cns_corridor_assessment"] = result
        self.invalidation.cns_corridor_gap()
        state.setdefault("result_statuses", {})["cns_corridor_assessment"] = _result_status(result.get("status"))
        self.session.save()
        return self.snapshot()


def _result_status(status):
    return {
        "passed": "passed", "failed": "failed", "not_applicable": "not_applicable",
        "pending_confirmation": "pending_confirmation", "missing_data": "missing_data",
        "unresolved": "missing_data", "stale": "stale",
    }.get(status, "pending_confirmation")
