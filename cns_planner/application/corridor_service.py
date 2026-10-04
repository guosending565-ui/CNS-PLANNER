"""Application use case for persisted P14 corridor policy and assessment.

Phase4-B6X 把"计算"与"写入"拆成两段，**同步语义完全不变**：

* :meth:`CNSCorridorService.compute_input` / :meth:`compute` —— 纯计算，不写 state；
  供 heavy task worker 在**独立进程**里调用。
* :meth:`CNSCorridorService.apply_computed` —— 唯一写入路径：写
  ``cns_corridor_assessment``、跑失效、更新 ``result_statuses``、``session.save()``。
* :meth:`CNSCorridorService.evaluate` —— 原来的同步入口，行为与拆分前逐字等价
  （「先 evaluate、后取 snapshot」的既有契约保持不变，因此同步返回的是**上一次**
  的评估结果，而不是刚算出来的新结果）。

Phase4-B9R.1 把**规模准入**收口成一条规则：``complexity_estimate`` 在组装输入时冻结，
同步入口、``compute`` 与异步 worker 都只消费这份冻结值（绝不重新估算）；三档语义
（within / beyond_validated / beyond_safety_ceiling）在算法层与这里保持同一份实现，
``allow_beyond_validated_envelope`` **只**解锁 beyond_validated 一档，不得绕过硬天花板。
"""

from __future__ import annotations

from copy import deepcopy

from ..catalogs import AircraftCNSProfileCatalog
from .planning_evidence_service import (
    aircraft_profile_with_evidence, device_catalog_with_evidence,
)
from ..algorithms.corridor.v1 import COMPLEXITY_MESSAGE_BEYOND, COMPLEXITY_MESSAGE_CEILING
from ..algorithms.corridor.v1 import COMPLEXITY_TIER_BEYOND, COMPLEXITY_TIER_CEILING
from ..domain.cns_corridor import (
    corridor_complexity_preflight, normalize_cns_corridor_policy,
)
from ..domain.surface_classification import (
    surface_class_provider_for, surface_facts_fingerprint_for,
)
from ..domain.navigation_augmentation import build_navigation_service_evidence
from ..domain.radar_service_evidence import build_radar_service_evidence
#: Round 29-J：算法语义 stale 的**唯一**实现已迁到 ``result_currentness``（currentness
#: authority）。这里只做兼容再导出，既有 import 路径与语义逐字不变。
from .result_currentness import apply_algorithm_semantics_stale  # noqa: F401


class CorridorScaleNotAccepted(ValueError):
    """准入拒绝：当前版本不承担这份工作量（同步 / 异步共用同一文案）。

    这不是数据错误，也不是安全失败——它只表示"当前版本不承担这份工作量"。
    ``estimate`` 携带结构化规模事实与中文原因，供 API / UI 如实展示。
    ``tier`` 区分两档拒绝（``beyond_validated_envelope`` / ``beyond_safety_ceiling``）。
    """

    def __init__(self, estimate, message=None):
        self.estimate = dict(estimate or {})
        detail = message or self.estimate.get("message") or COMPLEXITY_MESSAGE_CEILING
        super().__init__(detail)
        self.tier = str(self.estimate.get("tier") or "")


def _complexity_preflight(inputs):
    """对一份已组装输入做规模估算（纯函数，不写 state、不改 result）。

    B9R.1：估算件的唯一实现归属 domain 层，异步提交路径与同步入口共用同一份，
    避免"同一规模、两种准入"。估算的输入口径见
    :func:`cns_planner.domain.cns_corridor.corridor_complexity_preflight`。
    """

    return corridor_complexity_preflight(inputs)


def _admission_rejection(preflight, allow_beyond_validated_envelope=False):
    """统一准入规则（sync / async / worker 共用同一份判定）。

    返回 ``None`` 表示放行，否则返回拒绝原因（中文文案）：

    * ``within_validated_envelope`` → 放行；
    * ``beyond_validated_envelope`` → 只有显式 ``allow_beyond_validated_envelope``
      才放行，默认拒绝；
    * ``beyond_safety_ceiling`` → **无条件**拒绝，
      ``allow_beyond_validated_envelope`` 不得绕过硬天花板。
    """

    if not isinstance(preflight, dict) or not preflight.get("tier"):
        return None
    tier = str(preflight.get("tier"))
    if tier == COMPLEXITY_TIER_CEILING:
        return COMPLEXITY_MESSAGE_CEILING
    if tier == COMPLEXITY_TIER_BEYOND and not allow_beyond_validated_envelope:
        return COMPLEXITY_MESSAGE_BEYOND
    return None


def _reject_if_beyond(preflight, allow_beyond_validated_envelope=False):
    reason = _admission_rejection(preflight, allow_beyond_validated_envelope)
    if reason is None:
        return
    raise CorridorScaleNotAccepted(preflight, reason)


class CNSCorridorService:
    def __init__(self, session, model, invalidation, snapshot):
        self.session, self.model = session, model
        self.invalidation, self.snapshot = invalidation, snapshot

    def result_snapshot(self):
        """只读投影：算法语义版本变化时如实标注 stale（绝不误判 current）。"""

        result = self.session.state.get("cns_corridor_assessment") or self.model.empty()
        return apply_algorithm_semantics_stale(
            result, self.model.algorithm_id, self.model.algorithm_version,
        )

    # ---- B6X：计算（纯） -----------------------------------------------------

    def _assemble_inputs(self, payload=None):
        """组装 ``model.evaluate`` 的完整输入 + **冻结**规模估算（不写 state）。

        同步入口与异步提交路径（:func:`cns_planner.tasks.task_specs._corridor_inputs`）
        共用同一份 domain 层估算实现；差别只在"policy 从哪来"：这里在请求未显式给出
        时回落到 state 中已确认的 policy。B9R.1 起 ``complexity_estimate`` 与输入一起
        生成，之后**不再重算**。
        """

        state = self.session.state
        payload = payload if isinstance(payload, dict) else {}
        raw_policy = payload.get("cns_corridor_policy", payload.get("policy"))
        policy = (
            normalize_cns_corridor_policy(raw_policy)
            if raw_policy is not None
            else state.get("cns_corridor_policy") or normalize_cns_corridor_policy()
        )
        profile = aircraft_profile_with_evidence(state)
        selections = state.get("algorithm_selection") or {}
        inputs = {
            "routes": state.get("operational_routes") or [],
            "spatial_3d": state.get("spatial_3d") or {},
            "grid": state.get("grid") or {},
            "grid_attributes": state.get("grid_attributes") or {},
            "required_cns": state.get("required_cns") or {},
            "aircraft_profile": profile,
            "existing_facilities": state.get("existing_cns_facilities") or {},
            #: Round 2.7：设备侧的工程规划假设（``scope=device``）只在**消费点**叠加到
            #: 设备目录副本上；``state["device_catalog"]`` 本身逐字节不变。
            "device_catalog": device_catalog_with_evidence(state),
            "corridor_policy": policy,
            "coverage_parameters": ((selections.get("coverage_model") or {}).get("parameters") or {}),
            "capability_parameters": ((selections.get("service_model") or {}).get("parameters") or {}),
            #: Round 2 接线：P14 的 voxel surface_class 与 P7 共用唯一 surface facts。
            #: 这里只冻结**可序列化事实 + 事实指纹**（provider 是 callable，既不能进
            #: immutable snapshot，也不进稳定指纹）；provider 由 ``compute`` 统一从
            #: 这份事实重建，因此同步路径、重算路径与 worker 路径口径完全一致。
            "surface_class_facts": deepcopy(state.get("surface_class_facts") or {}),
            "surface_facts_fingerprint": surface_facts_fingerprint_for(state),
        }
        radar_evidence = build_radar_service_evidence(
            state.get("required_cns") or {}, state.get("radar_surveillance_layout") or {},
            route_ids=[item.get("route_id") for item in state.get("operational_routes") or []],
        )
        if radar_evidence is not None:
            inputs["radar_service_evidence"] = radar_evidence
        #: Round C：地面导航增强证据（``N:rtk_augmentation``）**只**在项目显式声明该
        #: 服务时生成；legacy 项目下为 ``None``，P14 shape/fingerprint 逐项不变。
        navigation_evidence = build_navigation_service_evidence(
            state.get("required_cns") or {},
            route_ids=[item.get("route_id") for item in state.get("operational_routes") or []],
            existing_facilities=state.get("existing_cns_facilities") or {},
            candidate_sites=state.get("candidate_sites") or {},
            tower_colocation=state.get("tower_colocation_candidates") or {},
        )
        if navigation_evidence is not None:
            inputs["navigation_service_evidence"] = navigation_evidence
        # B9R.1：把规模估算固化进输入（它是输入相关的确定性事实，因此属于输入指纹
        # 的一部分，调用方——无论同步还是 worker——都只消费这份冻结值）。
        inputs["complexity_estimate"] = _complexity_preflight(inputs)
        return inputs

    def compute_input(self, payload=None):
        """组装 ``model.evaluate`` 的完整输入（不写 state；请求给出的 policy 覆盖 state）。"""

        return self._assemble_inputs(payload)

    def compute(self, inputs):
        """纯计算：返回 canonical assessment result（不写 state）。

        B9R.1：一律消费 inputs 里**冻结**的 ``complexity_estimate``，绝不重新估算。
        完全超界（``beyond_safety_ceiling``）无条件下拒绝；``beyond_validated_envelope``
        只有 inputs 里带着显式 ``allow_beyond_validated_envelope=true`` 才继续。
        """

        from ..algorithms.corridor.v1 import CorridorComplexityBlocked

        inputs = inputs if isinstance(inputs, dict) else {}
        preflight = inputs.get("complexity_estimate")
        allow = inputs.get("allow_beyond_validated_envelope") is True
        # 应用层先按统一规则判定（与算法层同一份语义），保证拒绝时连
        # ``model.evaluate`` 都不会被调用。
        _reject_if_beyond(preflight if isinstance(preflight, dict) else None, allow)
        try:
            return self.model.evaluate(
                inputs.get("routes") or [], inputs.get("spatial_3d") or {},
                inputs.get("grid") or {}, inputs.get("grid_attributes") or {},
                inputs.get("required_cns") or {}, inputs.get("aircraft_profile") or {},
                inputs.get("existing_facilities") or {}, inputs.get("device_catalog") or {},
                inputs.get("corridor_policy") or {},
                coverage_parameters=inputs.get("coverage_parameters") or {},
                capability_parameters=inputs.get("capability_parameters") or {},
                preflight=preflight if isinstance(preflight, dict) else None,
                allow_beyond_validated_envelope=allow,
                #: Round 2：provider 统一由 inputs 里的**可序列化 surface 事实**重建
                #: （同步 / 重算 / worker 三条路径同一份口径）；显式传入的 provider
                #: 优先，供单进程内的定制化调用。
                surface_class_provider=(
                    inputs.get("surface_class_provider")
                    or surface_class_provider_for(inputs)
                ),
                surface_facts_fingerprint=(
                    inputs.get("surface_facts_fingerprint")
                    or surface_facts_fingerprint_for(inputs)
                ),
                radar_service_evidence=inputs.get("radar_service_evidence"),
                navigation_service_evidence=inputs.get("navigation_service_evidence"),
            )
        except CorridorComplexityBlocked as exc:
            raise CorridorScaleNotAccepted(exc.estimate, str(exc)) from exc

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
        # 只有结论真的变化时才让 P15/P16 失效：重算得到同一个 input_fingerprint
        # 时下游的基线仍然成立，无条件失效会让 P18 永远拿不到 current P16。
        invalidate = conclusion_changed(state.get("cns_corridor_assessment") or {}, result)
        state["cns_corridor_assessment"] = result
        if invalidate:
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

    def complexity_estimate(self, payload=None):
        """只读规模估算：供 API / UI 在提交前如实展示规模与准入分档。

        只做组装与纯估算，**不**写 state（因此不改变任何 canonical 结果）。
        """

        return self._assemble_inputs(payload)["complexity_estimate"]

    def evaluate(self, payload=None):
        """同步入口（三档准入，B9R.1 统一语义）。

        * ``within_validated_envelope`` → 照常计算与写入；
        * ``beyond_validated_envelope`` → 计算前抛
          :class:`CorridorScaleNotAccepted`（``COMPLEXITY_MESSAGE_BEYOND``）；
        * ``beyond_safety_ceiling`` → 计算前抛
          :class:`CorridorScaleNotAccepted`（``COMPLEXITY_MESSAGE_CEILING``）。

        同步路径**不存在**任何 override：超过已验证包线一律改用后台计算。
        拒绝时既不产生 canonical result，也不附加只读元数据。
        """

        self.apply_policy_input(payload)
        inputs = self._assemble_inputs(payload)
        preflight = inputs.get("complexity_estimate")
        # 同步入口永不接受超包线运行（allow 恒为 False）。
        _reject_if_beyond(preflight, False)
        result = self.compute(inputs)
        state = self.session.state
        invalidate = conclusion_changed(state.get("cns_corridor_assessment") or {}, result)
        state["cns_corridor_assessment"] = result
        if invalidate:
            self.invalidation.cns_corridor_gap()
        state.setdefault("result_statuses", {})["cns_corridor_assessment"] = _result_status(result.get("status"))
        self.session.save()
        snapshot = self.snapshot()
        # 只读元数据（不属于 canonical assessment，不进 result schema）。
        snapshot["corridor_complexity_estimate"] = preflight
        return snapshot


def conclusion_changed(previous, current) -> bool:
    """上游派生链是否需要失效：同一 ``input_fingerprint`` 不算变化。

    重算得到完全相同的结论时，下游（P15/P16/P18）记录的基线指纹仍然成立；
    若在这里无条件失效，P18 的"需要 current P16"门禁就会永远无法满足。
    """

    before = str((previous or {}).get("input_fingerprint") or "")
    if not before:
        return True
    return before != str((current or {}).get("input_fingerprint") or "")


def _result_status(status):
    return {"passed": "passed", "failed": "failed", "not_applicable": "not_applicable",
        "pending_confirmation": "pending_confirmation", "missing_data": "missing_data",
        "unresolved": "missing_data", "stale": "stale",
    }.get(status, "pending_confirmation")
