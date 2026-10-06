"""Phase4-B6X：task type 注册表（业务 endpoint 提交任务，不复制任务框架）。

只有**真正重**的业务动作在这里登记成 heavy task；轻任务继续走同步 POST：

* ``cns_service_corridor_evaluate`` —— ``POST /api/cns-service-corridor/evaluate``。
  服务走廊评估是当前最重的 CNS 派生计算（完整明细可达约 82 MB），原本在 HTTP
  请求里全程持有 ``mutation_lock``。
* ``planning_constraint_field_generate`` —— ``POST /api/planning-constraint-fields/evaluate``。
  逐 grid cell × terrain/building/tower/airspace/critical-site 约束判定，随工作区
  网格规模线性增长。

两个 task type 都满足同一套契约：

``submit_plan(state, payload)``
    纯函数、只读 state，返回 ``{scope_id, worker_payload, inputs}``。``inputs`` 是
    这次计算的**权威输入事实**，提交时被原样写进 immutable snapshot
    （``.cns-tasks/inputs/<sha256>.json.gz``），worker 只从它取输入。

``algorithm_types``
    Phase4-B6R：声明哪些算法选择**影响本任务结果**。它们与 ``inputs`` 一起进入同一份
    immutable snapshot 的 ``algorithms`` 段，共同构成输入指纹；worker 只用 snapshot
    里的清单重建算法，绝不回读当前 ``ProjectState`` 的算法选择。

``runner(context)``
    只在 worker 进程内运行。它不写 canonical state，只把结果 staged 成 artifact。
    调用 :meth:`WorkerContext.progress` 上报阶段进度，:meth:`WorkerContext.check_cancel`
    在 major phase 之前做 cooperative cancel 检查。
"""

from __future__ import annotations

from copy import deepcopy
import threading
import time

from ..domain.cns_corridor import (
    corridor_complexity_preflight, normalize_cns_corridor_policy,
)
from ..domain.cns_planning_objectives import normalize_cns_planning_objectives
from ..domain.corridor_site_planning import normalize_corridor_site_planning_policy
from ..domain.surface_classification import (
    surface_class_provider_for, surface_facts_fingerprint_for,
)
from .task_input import CORRIDOR_ALGORITHM_TYPES
from .task_spec import (
    TaskCancelled, TaskInputChanged, TaskPerformanceAdmissionUpgradeRequired,
    TaskRunResult, TaskSpec, WorkerResultRef,
    fingerprint_payload,
)
from .task_store import TERMINAL_STATUSES


#: 业务 endpoint → task type（router 用它判断某次 POST 是否异步提交）。
TASK_ENDPOINTS = {
    "/api/cns-service-corridor/evaluate": "cns_service_corridor_evaluate",
    "/api/planning-constraint-fields/evaluate": "planning_constraint_field_generate",
    #: Round 31-A：CNS 设施规划（P16）在真实项目上是数十分钟级的累计试算，
    #: 因此与 P14 一样登记为 heavy task：带 ``async: true`` 提交时只登记任务并
    #: 立刻返回 task_id，不带时保持原有同步语义（既有调用方不受影响）。
    "/api/cns-corridor-site-plan/evaluate": "cns_corridor_site_plan_evaluate",
    #: Round 31-D：CNS 能力缺口评估（P15）同样登记为 heavy task。它在真实项目上
    #: 逐体元判定整条走廊，且必须在发布前重新校验上游 P14 的 currentness；带
    #: ``async: true`` 提交时只登记任务并立刻返回 task_id，不带时保持原有同步语义。
    "/api/cns-corridor-gap/evaluate": "cns_corridor_gap_evaluate",
}

_REGISTRY: dict[str, TaskSpec] = {}


def register_task_spec(spec: TaskSpec) -> TaskSpec:
    if not isinstance(spec, TaskSpec):
        raise TypeError("任务注册需要 TaskSpec")
    if not spec.task_type:
        raise ValueError("任务类型不能为空")
    _REGISTRY[str(spec.task_type)] = spec
    return spec


def task_spec(task_type) -> TaskSpec:
    key = str(task_type)
    if key not in _REGISTRY:
        raise ValueError(f"未登记的 heavy task 类型：{task_type}")
    return _REGISTRY[key]


def task_specs():
    return tuple(_REGISTRY.values())


def has_task_type(task_type) -> bool:
    return str(task_type) in _REGISTRY


def task_type_for_endpoint(path):
    return TASK_ENDPOINTS.get(str(path))


# ---- corridor ----------------------------------------------------------------

#: B9R.2 性能准入策略版本（写进 immutable snapshot 的轻量元数据，不是派生估算件）。
#: 它让 worker 能如实回答"提交时用的是哪一版准入策略"，而不必把昂贵的估算结果本身
#: 当成输入事实冻结。
CORRIDOR_PERFORMANCE_ADMISSION_POLICY_VERSION = "corridor_performance_admission_v1"


def _admission_policy_record():
    """准入策略的**轻量**快照元数据（版本 + 两个阈值常量）。

    它不含任何估算结果：估算的全部源输入（routes / grid / grid_attributes /
    corridor_policy / existing_cns_facilities / device_catalog）都已经在
    immutable snapshot 的 ``inputs`` 里，因此估算本身是**可从快照确定性重算**的
    派生量，不需要（也不应该）在提交短锁内计算并冻结。
    """

    from ..algorithms.corridor.v1 import (
        SAFETY_EVALUATION_CEILING, VALIDATED_EVALUATION_LIMIT,
    )

    return {
        "performance_admission_policy_version": (
            CORRIDOR_PERFORMANCE_ADMISSION_POLICY_VERSION
        ),
        "performance_admission_policy": {
            "version": CORRIDOR_PERFORMANCE_ADMISSION_POLICY_VERSION,
            "validated_evaluation_limit": VALIDATED_EVALUATION_LIMIT,
            "safety_evaluation_ceiling": SAFETY_EVALUATION_CEILING,
        },
    }


def _corridor_inputs(state, payload):
    """服务走廊评估的权威输入（只读 state，确定性）。

    B6R：**只**包含"输入事实"。算法选择（corridor_model / coverage_model /
    service_model 的 id/version/parameters）由 :mod:`cns_planner.tasks.task_input`
    作为同一份 immutable snapshot 的 ``algorithms`` 段记录，两者共同构成指纹。

    corridor policy 是随请求提交的显式输入，不是"当下 state"：提交时它被归一化后写进
    ``worker_payload``，此后任何一次重新生成（worker 变化检测 / publish compare）
    都只从 payload 取它。请求里没给 policy 时才回落到 state 里已确认的 policy。

    B9R.2（本轮修复）：这里**不再**计算 ``complexity_estimate``。规模估算是对本份输入
    的确定性函数，把它的结果再写回输入，等于在 ``mutation_lock`` 内做一次昂贵派生计算
    （实测 B production-upper-bound 档约 0.25 s），并把"派生量"当成输入事实冻结。
    现在改为只在快照里写入**轻量**的准入策略记录（版本 + 两个阈值常量），规模估算由
    worker 在拿到 immutable snapshot 后**从原始输入**计算——因此：

    * worker 仍然绝不回读当前 ``ProjectState``；
    * 规模事实仍然完全由"提交那一刻的输入"决定（估算的全部源输入都在快照里）；
    * 输入指纹仍然覆盖全部规模驱动因素（provider 目录、设施、网格、航路、policy）。
    """

    payload = payload if isinstance(payload, dict) else {}
    raw_policy = payload.get("cns_corridor_policy", payload.get("policy"))
    if raw_policy is None:
        raw_policy = deepcopy(state.get("cns_corridor_policy")) or {}
    policy = normalize_cns_corridor_policy(raw_policy)
    inputs = {
        "routes": deepcopy(state.get("operational_routes") or []),
        "spatial_3d": deepcopy(state.get("spatial_3d") or {}),
        "grid": deepcopy(state.get("grid") or {}),
        "grid_attributes": deepcopy(state.get("grid_attributes") or {}),
        "required_cns": deepcopy(state.get("required_cns") or {}),
        "aircraft_profiles": deepcopy(state.get("aircraft_profiles") or {}),
        "selected_aircraft_profile_id": state.get("selected_aircraft_profile_id") or "",
        "existing_cns_facilities": deepcopy(state.get("existing_cns_facilities") or {}),
        "device_catalog": deepcopy(state.get("device_catalog") or {}),
        "corridor_policy": policy,
        #: Round 2（P0）：surface classification 必须转成**可序列化**事实才能进
        #: immutable snapshot。这里**只**冻结纯数据（``by_grid_id`` + 语义 + 政策 +
        #: 来源身份 + 事实指纹）；provider 是 callable，**绝不**进入快照，
        #: 由 worker 侧用 :func:`surface_class_provider_for` 从这份事实重建。
        "surface_class_facts": deepcopy(state.get("surface_class_facts") or {}),
        "surface_facts_fingerprint": surface_facts_fingerprint_for(state),
        #: Round 2.4：人工工程证据 / 规划假设必须随输入一起冻结进 immutable
        #: snapshot —— 否则异步 worker 会拿"没有工程证据"的机载能力去重算，
        #: 与同步入口得到两份不同判定（同一输入必须得到同一结论）。
        "planning_evidence": deepcopy(state.get("planning_evidence") or {}),
    }
    inputs.update(_admission_policy_record())
    #: Round 2.4：机载能力**叠加工程证据后**冻结为显式输入，同步入口、重算路径与
    #: worker 因此消费同一份权威机载能力。
    from ..application.planning_evidence_service import (
        aircraft_profile_with_evidence, device_catalog_with_evidence,
    )

    inputs["aircraft_profile"] = aircraft_profile_with_evidence(inputs) or {}
    #: Round 2.7：设备侧的工程规划假设（``scope=device``）与机载同为"消费点叠加"，
    #: 因此异步 worker 冻结的必须是**叠加后**的设备目录，否则同一条 P14 在同步与
    #: 异步两条路径上会得到两份不同的判定（同一输入必须得到同一结论）。
    inputs["device_catalog"] = device_catalog_with_evidence(inputs) or {}
    return inputs


def _corridor_preflight_from_snapshot(inputs):
    """worker 侧：从 immutable snapshot 的**原始输入**计算规模准入估算。

    ``inputs["complexity_estimate"]`` 若存在（B9R.1 时期的旧快照）直接复用；不存在时
    用本份输入自己算一次。两条路径都只依赖快照内容，绝不读当前 ``ProjectState``。
    """

    inputs = inputs if isinstance(inputs, dict) else {}
    frozen = inputs.get("complexity_estimate")
    if isinstance(frozen, dict) and frozen:
        return frozen
    return corridor_complexity_preflight({
        **inputs,
        "existing_facilities": inputs.get("existing_cns_facilities") or {},
        # Round 2：估算与正式评估共用同一份 surface 事实（provider 由快照事实重建，
        # 绝不回读当前 ProjectState）。
        "surface_class_provider": surface_class_provider_for(inputs),
    })


def _corridor_scope(state, payload):
    routes = state.get("operational_routes") or []
    route_ids = sorted(str(item.get("route_id") or "") for item in routes if isinstance(item, dict))
    return "cns_service_corridor:" + ("|".join(route_ids) if route_ids else "no-route")


def _corridor_worker_payload(state, payload):
    """worker 侧重建输入所需的最小覆盖项。

    只保留请求显式给出的 policy（归一化后），请求未给出时**不**写入：这样
    "本次请求的 policy"与"state 里已有的 policy"不会被混成两个不同的输入指纹。

    B9R.1：``allow_beyond_validated_envelope`` 是请求显式给出的**风险接受**，因此与
    policy 同样只在显式给出时写入。它随 snapshot 冻结，worker 绝不回读当前 state。
    它的语义**只**覆盖 ``beyond_validated_envelope`` 一档：``beyond_safety_ceiling``
    由算法层无条件阻断，这个 flag 不得绕过。
    """

    payload = payload if isinstance(payload, dict) else {}
    compact = {}
    raw_policy = payload.get("cns_corridor_policy", payload.get("policy"))
    if raw_policy is not None:
        compact["cns_corridor_policy"] = normalize_cns_corridor_policy(raw_policy)
    if payload.get("allow_beyond_validated_envelope") is True:
        compact["allow_beyond_validated_envelope"] = True
    return compact


def _corridor_plan(state, payload):
    worker_payload = _corridor_worker_payload(state, payload)
    return {
        "worker_payload": worker_payload,
        "inputs": _corridor_inputs(state, worker_payload),
    }


def _corridor_cancel_probe(context, interval_seconds=1.0):
    """B9/P14：把"取消请求"从长循环内部暴露给 worker 而不阻塞 worker 线程。

    ``WorkerContext.check_cancel()`` 每次都读 task store（磁盘）。P14 的体素循环
    在本规模下要跑数十秒到数百秒，若在循环里直接调用它，取消在最坏情况下要等到
    整个 ``model.evaluate`` 返回才生效。这里用一个低频守护线程轮询 store，取消一旦
    被观察到就置位事件；长循环里的 ``cancel_check`` 只做一次 ``is_set()`` 判断，
    因此既不引入明显的每单元 I/O，也不改变任何数学结果。

    返回 ``(cancel_check, stop)``：``cancel_check`` 在已请求取消时抛
    ``TaskCancelled``（由 worker 映射为 task ``cancelled`` 且不发布结果）。
    """

    event = threading.Event()

    def loop():
        while not event.wait(interval_seconds):
            try:
                current = context.store.find(context.task_id)
            except Exception:  # noqa: BLE001 - 取消探测失败不掩盖计算错误
                continue
            if current is None:
                event.set()
                return
            if current.get("cancel_requested") or str(current.get("status")) in TERMINAL_STATUSES:
                event.set()
                return

    worker = threading.Thread(
        target=loop, name=f"corridor-cancel-probe-{context.task_id}", daemon=True,
    )
    worker.start()

    def cancel_check():
        if event.is_set():
            raise TaskCancelled()

    def stop():
        event.set()
        if worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=max(1.0, interval_seconds * 2))

    return cancel_check, stop


def _corridor_runner(context):
    snapshot = context.inputs["snapshot"]
    inputs = snapshot.get("inputs") or {}
    # 极旧快照既没有 B9R.1 冻结估算、也没有新版策略版本时必须 fail-closed：
    # 不从当前 ProjectState 补数据、不套用当前 policy、更不能绕过准入继续计算。
    admission_known = (
        isinstance(inputs.get("performance_admission_policy_version"), str)
        and bool(inputs.get("performance_admission_policy_version").strip())
    ) or (
        isinstance(inputs.get("complexity_estimate"), dict)
        and bool(inputs.get("complexity_estimate"))
    )
    if not admission_known:
        raise TaskPerformanceAdmissionUpgradeRequired()
    preflight = _corridor_preflight_from_snapshot(inputs)
    algorithms = context.algorithms()
    context.check_cancel()
    context.progress(0.05, "正在准备服务走廊评估输入")
    from ..catalogs import AircraftCNSProfileCatalog

    #: Round 2.4：快照里若有**已叠加工程证据**的机载能力，一律优先 —— 人工补录的
    #: 工程证据不得在异步路径上被静默忽略。缺失时才按旧口径从 catalog 解析。
    profile = inputs.get("aircraft_profile") or AircraftCNSProfileCatalog.find(
        inputs.get("aircraft_profiles") or {},
        inputs.get("selected_aircraft_profile_id") or "",
    )
    # B6R：算法只从 snapshot 的 manifest 重建，绝不读当前 ProjectState 的算法选择。
    model = algorithms.create("corridor_model")
    coverage_parameters = algorithms.manifest("coverage_model").get("parameters") or {}
    capability_parameters = algorithms.manifest("service_model").get("parameters") or {}
    context.check_cancel()
    context.progress(0.15, "正在按提交时输入判定规模准入")
    cancel_check, stop_probe = _corridor_cancel_probe(context)
    worker_payload = snapshot.get("worker_payload") or {}
    allow_beyond = worker_payload.get("allow_beyond_validated_envelope") is True
    # B9R.2：规模准入在 worker 侧、**从 immutable snapshot 的原始输入**计算，
    # 然后先做准入判定、再进入 corridor 主计算。worker 绝不回读当前 ProjectState，
    # 估算的全部源输入都来自提交那一刻冻结的快照。
    # 统一准入规则（与同步入口 / ``CNSCorridorService.compute`` 同一份实现）：
    # beyond_validated 默认拒绝、仅显式接受可继续；beyond_safety_ceiling 无条件阻断。
    # worker 侧保留 B9R.1 的异常语义（``CorridorComplexityBlocked`` 携带估算件），
    # worker 进程把它落盘成明确的业务拒绝（code=task_scale_not_accepted）。
    from ..algorithms.corridor.v1 import CorridorComplexityBlocked
    from ..application.corridor_service import _admission_rejection

    reason = _admission_rejection(preflight, allow_beyond)
    if reason is not None:
        stop_probe()
        raise CorridorComplexityBlocked(dict(preflight, message=reason))
    context.check_cancel()
    context.progress(0.2, "正在计算服务走廊（体素探测与服务能力判定）")
    try:
        result = model.evaluate(
            inputs.get("routes") or [], inputs.get("spatial_3d") or {},
            inputs.get("grid") or {}, inputs.get("grid_attributes") or {},
            inputs.get("required_cns") or {}, profile,
            inputs.get("existing_cns_facilities") or {}, inputs.get("device_catalog") or {},
            inputs.get("corridor_policy") or {},
            coverage_parameters=coverage_parameters,
            capability_parameters=capability_parameters,
            cancel_check=cancel_check,
            progress_callback=lambda value, message: context.progress(value / 100.0, message),
            preflight=preflight,
            # B9R.1：显式风险接受**只**解锁 beyond_validated_envelope 一档；
            # beyond_safety_ceiling 由算法层无条件阻断，任何 flag 都不得绕过。
            allow_beyond_validated_envelope=allow_beyond,
            #: Round 2（P0）：worker 从 immutable snapshot 的**可序列化 surface 事实**
            #: 重建 provider（绝不回读 ProjectState，也绝不把 callable 放进快照），
            #: 事实指纹随快照一起进入 P14 输入指纹。
            surface_class_provider=surface_class_provider_for(inputs),
            surface_facts_fingerprint=inputs.get("surface_facts_fingerprint"),
        )
    except TaskCancelled:
        stop_probe()
        context.stop_heartbeat(finished=True)
        raise
    stop_probe()
    context.check_cancel()
    context.progress(0.9, "正在写入临时结果明细")
    payload = {"logical_key": "cns_corridor_assessment", "value": result}
    metadata = context.stage(payload, artifact_type="corridor.assessment.detail")
    context.progress(0.96, "临时结果明细已写入，等待发布")
    return TaskRunResult(
        summary=_corridor_summary(result),
        staged=WorkerResultRef(
            artifact_id=str(metadata.get("artifact_id")),
            relative_path=str(metadata.get("relative_path")),
            sha256=str(metadata.get("sha256")),
            size_bytes=int(metadata.get("size_bytes") or 0),
            artifact_type="corridor.assessment.detail",
            summary=_corridor_summary(result),
        ),
        message="服务走廊计算完成，等待发布",
        progress=0.96,
    )


def _corridor_summary(result):
    """canonical result 的**摘要**：ProjectState 只留这些，明细进 artifact。"""

    result = result if isinstance(result, dict) else {}
    return {
        "status": result.get("status"),
        "algorithm_id": result.get("algorithm_id"),
        "algorithm_version": result.get("algorithm_version"),
        "input_fingerprint": result.get("input_fingerprint"),
        "corridor_geometry_fingerprint": result.get("corridor_geometry_fingerprint"),
        "route_count": result.get("route_count"),
        "deficit_voxel_count": len(result.get("deficit_voxel_ids") or []),
        "unknown_voxel_count": len(result.get("unknown_voxel_ids") or []),
    }


# ---- planning constraint field ----------------------------------------------


def _pcf_components(state, payload):
    """约束场生成的权威输入（复用 application 层的唯一输入组装函数）。"""

    from ..application.planning_constraint_field_service import generation_arguments

    return deepcopy(generation_arguments(state, payload if isinstance(payload, dict) else {}))


def _pcf_scope(state, payload):
    # scope 只需要高度层身份；完整 generation_arguments 留到通过
    # active-task 查重后再组装，避免重复提交制造无用大 snapshot。
    layer_id = str((payload if isinstance(payload, dict) else {}).get(
        "altitude_layer_id"
    ) or "unresolved")
    return f"planning_constraint_field:{layer_id}"


def _pcf_worker_payload(payload):
    """只保留影响输入指纹的显式覆盖项，避免把整份 grid 再存一遍。"""

    payload = payload if isinstance(payload, dict) else {}
    compact = {}
    for key in ("altitude_layer_id", "policies", "source_fingerprints", "workspace_identity"):
        if payload.get(key) is not None:
            compact[key] = deepcopy(payload[key])
    return compact


def _pcf_plan(state, payload):
    return {
        "worker_payload": _pcf_worker_payload(payload),
        "inputs": _pcf_components(state, payload),
    }


def _pcf_runner(context):
    from ..application.planning_constraint_field_service import (
        generate_planning_constraint_field,
    )

    inputs = context.inputs["snapshot"]["inputs"]
    context.check_cancel()
    context.progress(0.1, "正在判定逐格约束（地形 / 建筑 / 铁塔 / 空域 / 要地）")
    field = generate_planning_constraint_field(
        altitude_layer=inputs["altitude_layer"],
        grid=inputs["grid"],
        terrain_by_cell=inputs["terrain_by_cell"],
        buildings_by_cell=inputs["buildings_by_cell"],
        tower_obstacle_profiles=inputs["tower_obstacle_profiles"],
        restricted_areas=inputs["restricted_areas"],
        policies=inputs["policies"],
        source_fingerprints=inputs["source_fingerprints"],
        workspace_identity=inputs["workspace_identity"],
    )
    context.check_cancel()
    context.progress(0.9, "正在写入临时结果明细")
    payload = {"logical_key": "planning_constraint_fields", "value": field}
    metadata = context.stage(payload, artifact_type="planning_constraint_field.cells")
    context.progress(0.96, "临时结果明细已写入，等待发布")
    return TaskRunResult(
        summary=_pcf_summary(field),
        staged=WorkerResultRef(
            artifact_id=str(metadata.get("artifact_id")),
            relative_path=str(metadata.get("relative_path")),
            sha256=str(metadata.get("sha256")),
            size_bytes=int(metadata.get("size_bytes") or 0),
            artifact_type="planning_constraint_field.cells",
            summary=_pcf_summary(field),
        ),
        message="约束场计算完成，等待发布",
        progress=0.96,
    )


def _pcf_summary(field):
    field = field if isinstance(field, dict) else {}
    return {
        "field_id": field.get("field_id"),
        "altitude_layer_id": field.get("altitude_layer_id"),
        "status": field.get("status"),
        "counts": deepcopy(field.get("counts")),
        "constraint_field_fingerprint": field.get("constraint_field_fingerprint"),
        "policy_fingerprint": field.get("policy_fingerprint"),
    }


# ---- corridor site plan（P16） -------------------------------------------------
#
# P16 与 P14 / 约束场的**输入形态不同**：它的 baseline 是**上游 canonical 结论**
# （P14 服务走廊评估 + P15 能力缺口），而这些结论的明细是十几 MB 量级、并以
# content-addressed artifact 外置在项目目录里（见 ``project_compaction``）。把明细
# 整份复制进 immutable snapshot 既昂贵又冗余，因此本 task type 的契约是：
#
# * snapshot 冻结**全部基础事实**（航路 / 网格 / 设施 / 设备目录 / 策略 / 工程证据 /
#   算法选择）与上游 canonical 的**身份**（status + 输入指纹 + 几何指纹）；
# * worker 只读**当前项目文件**（含 artifact 明细恢复）作为运行载体 —— 它绝不写盘
#   （``session.save()`` 只由主进程的 publish 阶段调用），因此 worker 永远不是第二个
#   canonical 写入者；
# * 提交之后 state 一旦变化即作废：worker 侧 ``_load_immutable_inputs`` 与主进程
#   publish 的 compare-and-publish 是两道独立闸门，两边用同一份 snapshot 指纹。

P16_TASK_TYPE = "cns_corridor_site_plan_evaluate"

#: P16 结果真正依赖的基础事实（只读 state，逐项深拷贝进 immutable snapshot）。
#: 与 ``_corridor_inputs`` 同源：P16 的每一次 what-if 都用它们重建 P14/P15。
P16_INPUT_STATE_KEYS = (
    "operational_routes", "spatial_3d", "grid", "grid_attributes",
    "required_cns", "existing_cns_facilities", "device_catalog",
    "candidate_sites", "tower_colocation_candidates",
    "cns_corridor_policy", "cns_planning_objectives",
    "cns_continuous_service_policy", "algorithm_selection",
    "aircraft_profiles", "selected_aircraft_profile_id",
    "planning_evidence", "surface_class_facts",
)

#: 进入 P16 结果的上游 canonical 结论（只冻结身份，不冻结明细）。
P16_UPSTREAM_RESULTS = (
    "cns_corridor_assessment", "cns_corridor_gap_assessment",
    "radar_surveillance_layout",
)

#: 影响 P16 结果的算法类型：站点规划器本身，以及它 what-if 里重建 P14/P15 所用的模型。
P16_ALGORITHM_TYPES = (
    "site_planner", "corridor_model", "corridor_gap_analyzer",
    "coverage_model", "service_model",
)

#: 上游结论的"身份"字段：内容寻址指纹 + 产生它的算法身份（供 worker 自证一致）。
P16_UPSTREAM_IDENTITY_KEYS = (
    "status", "input_fingerprint", "corridor_geometry_fingerprint",
    "algorithm_id", "algorithm_version",
)


def _upstream_identity(state, key):
    container = state.get(key) if isinstance(state, dict) else None
    if not isinstance(container, dict):
        return {}
    return {
        name: deepcopy(container.get(name)) for name in P16_UPSTREAM_IDENTITY_KEYS
    }


def _p16_inputs(state, payload):
    """CNS 设施规划的权威输入（只读 state，确定性）。"""

    state = state if isinstance(state, dict) else {}
    payload = payload if isinstance(payload, dict) else {}
    raw_policy = payload.get("corridor_site_planning_policy")
    if raw_policy is None:
        raw_policy = state.get("corridor_site_planning_policy")
    inputs = {key: deepcopy(state.get(key)) for key in P16_INPUT_STATE_KEYS}
    #: 规划策略始终以**归一化后的显式输入**进入指纹：请求给出时用请求值，
    #: 否则用 state 里已确认的值（与 :meth:`CorridorSitePlanningService.plan` 同口径）。
    inputs["corridor_site_planning_policy"] = normalize_corridor_site_planning_policy(
        raw_policy
    )
    inputs["upstream_canonical"] = {
        key: _upstream_identity(state, key) for key in P16_UPSTREAM_RESULTS
    }
    return inputs


def _p16_scope(state, payload):
    routes = (state if isinstance(state, dict) else {}).get("operational_routes") or []
    route_ids = sorted(
        str(item.get("route_id") or "") for item in routes if isinstance(item, dict)
    )
    return "cns_corridor_site_plan:" + ("|".join(route_ids) if route_ids else "no-route")


def _p16_worker_payload(state, payload):
    """worker 侧重建输入所需的最小覆盖项（只保留请求显式给出的规划策略）。

    请求未给出策略时**不**写入：这样"本次请求的策略"与"state 里已确认的策略"
    不会分裂成两个不同指纹（与 ``_corridor_worker_payload`` 同一口径）。
    """

    payload = payload if isinstance(payload, dict) else {}
    compact = {}
    raw_policy = payload.get("corridor_site_planning_policy")
    if raw_policy is not None:
        compact["corridor_site_planning_policy"] = normalize_corridor_site_planning_policy(
            raw_policy
        )
    return compact


def _p16_plan(state, payload):
    worker_payload = _p16_worker_payload(state, payload)
    return {
        "worker_payload": worker_payload,
        "inputs": _p16_inputs(state, worker_payload),
    }


class _WorkerReadOnlySession:
    """worker 侧的只读 state 载体：**绝不落盘**。

    P16 的 canonical 写入只由主进程的 publish 阶段
    （``CorridorSitePlanningService.apply_computed``）完成，因此这里一旦被要求
    保存就必须立刻失败，绝不给后台进程留下第二个写 canonical state 的机会。
    """

    def __init__(self, state):
        self.state = state

    def save(self):  # pragma: no cover - 只有 canonical 写入路径才会调用
        raise RuntimeError("后台任务绝不写 canonical state")


class _WorkerInvalidation:
    """worker 侧不提供任何失效入口（``getattr(..., None)`` 因此回落为"不调用"）。"""

    def __getattr__(self, name):
        raise AttributeError(name)


def _worker_snapshot_passthrough():
    """worker 侧不需要 slim 快照投影：结果由主进程发布后再投影。"""

    return None


class P16UpstreamNotCurrent(TaskInputChanged):
    """P16 的上游 canonical 结论已变化：任务作废，绝不据此发布新提案。

    ``code`` 独立登记，让服务端能给出**具体**的中文业务原因（而不是笼统的
    "输入已变化"）：P16 的 baseline 是上游规划结论，用户需要知道"先重算上游"。
    """

    code = "task_p16_upstream_not_current"

    def __init__(self, message="上游规划结论已变化，请先重算上游结果，再重新运行设施规划"):
        super().__init__(message)


def _p16_summary(result):
    """canonical 结果的**摘要**（明细进 artifact，状态只留这些可读计数）。"""

    result = result if isinstance(result, dict) else {}
    return {
        "status": result.get("status"),
        "stop_reason": result.get("stop_reason"),
        "selected_action_count": len(result.get("selected_actions") or []),
        "candidate_action_count": len(result.get("candidate_actions") or []),
        "target_count": len(result.get("targets") or []),
        "continuous_service_acceptable": result.get("continuous_service_acceptable"),
        "input_fingerprint": result.get("input_fingerprint"),
    }


def _p16_runner(context):
    """worker 侧：在项目状态的**内存副本**上重算 P16，只 stage 结果、绝不发布。"""

    from ..application.corridor_site_planning_service import (
        CorridorSitePlanningService,
    )

    snapshot = context.inputs.get("snapshot") if isinstance(context.inputs, dict) else {}
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    inputs = snapshot.get("inputs") or {}
    worker_payload = snapshot.get("worker_payload") or {}
    algorithms = context.algorithms()
    #: 先按声明清单预解析：任一算法版本在提交后不可用时立刻失败，绝不部分运行。
    algorithms.resolve_all(P16_ALGORITHM_TYPES)
    context.check_cancel()
    context.progress(0.01, "正在准备 CNS 设施规划输入")
    state = deepcopy(context.project_state)
    if not isinstance(state, dict) or not state:
        raise TaskInputChanged("项目状态不可读，设施规划任务作废")
    #: 自证 baseline 身份：提交时冻结的上游指纹必须与当前 canonical 结论一致。
    #: 这是 worker 侧闸门；主进程 publish 阶段还会用同一份 snapshot 再做一次
    #: compare-and-publish，两边都不允许"用漂移过的上游结论"发布结果。
    expected = inputs.get("upstream_canonical") or {}
    for key in P16_UPSTREAM_RESULTS:
        current = state.get(key)
        current_fingerprint = (
            current.get("input_fingerprint") if isinstance(current, dict) else None
        )
        expected_fingerprint = (expected.get(key) or {}).get("input_fingerprint")
        if str(current_fingerprint or "") != str(expected_fingerprint or ""):
            raise P16UpstreamNotCurrent()
    service = CorridorSitePlanningService(
        _WorkerReadOnlySession(state),
        algorithms.create("site_planner"),
        algorithms.create("corridor_model"),
        algorithms.create("corridor_gap_analyzer"),
        _WorkerInvalidation(),
        _worker_snapshot_passthrough,
    )
    outcome = service.plan(
        worker_payload,
        on_progress=lambda value, message=None: context.progress(value, message),
        cancel_check=context.check_cancel,
    )
    reason = outcome.get("missing_reason")
    if reason is not None:
        raise P16UpstreamNotCurrent()
    result = outcome.get("result")
    if not isinstance(result, dict) or not result:
        raise RuntimeError("设施规划计算没有产出结果")
    context.check_cancel()
    context.progress(0.99, "正在写入临时结果明细")
    metadata = context.stage(
        {"logical_key": "cns_corridor_site_plan", "value": result},
        artifact_type="corridor.site_plan.detail",
    )
    summary = _p16_summary(result)
    return TaskRunResult(
        summary=summary,
        staged=WorkerResultRef(
            artifact_id=str(metadata.get("artifact_id")),
            relative_path=str(metadata.get("relative_path")),
            sha256=str(metadata.get("sha256")),
            size_bytes=int(metadata.get("size_bytes") or 0),
            artifact_type="corridor.site_plan.detail",
            summary=summary,
        ),
        message="CNS 设施规划计算完成，等待发布",
        progress=0.99,
    )


# ---- P15 能力缺口评估（Round 31-D） ---------------------------------------------
#
# P15 的输入形态与 P14 / P16 不同：它消费**上游 canonical 结论**（P14 服务走廊评估）
# + 需求事实（required_cns / cns_planning_objectives）+ 算法选择。P14 的逐体元明细是
# 十几 MB 量级、以 content-addressed artifact 外置在项目目录里（见
# ``project_compaction`` 的 ``corridor.assessment.detail``），整份复制进 immutable
# snapshot 既昂贵又冗余。因此本 task type 沿用 P16 的契约：
#
# * snapshot 冻结**需求事实**（required_cns / cns_planning_objectives）与上游
#   canonical 的**身份**（status + 输入指纹 + 几何指纹 + 算法身份）；
# * worker 只读**当前项目文件**（含 artifact 明细恢复）作为运行载体 —— 它绝不写盘
#   （``session.save()`` 只由主进程的 publish 阶段调用），因此 worker 永远不是第二个
#   canonical 写入者；
# * 提交之后 state 一旦变化即作废：worker 侧的身份校验与主进程 publish 的
#   compare-and-publish 是两道独立闸门，两边用同一份 snapshot 指纹。

P15_TASK_TYPE = "cns_corridor_gap_evaluate"

#: P15 结果真正依赖的输入事实（只读 state，逐项深拷贝进 immutable snapshot）。
P15_INPUT_STATE_KEYS = ("required_cns",)

#: 进入 P15 结果的上游 canonical 结论（只冻结身份，不冻结明细）。
P15_UPSTREAM_RESULTS = ("cns_corridor_assessment",)

#: 影响 P15 结果的算法类型：能力缺口分析器本身。
P15_ALGORITHM_TYPES = ("corridor_gap_analyzer",)


def _p15_inputs(state, payload):
    """CNS 能力缺口评估的权威输入（只读 state，确定性）。"""

    state = state if isinstance(state, dict) else {}
    payload = payload if isinstance(payload, dict) else {}
    inputs = {key: deepcopy(state.get(key)) for key in P15_INPUT_STATE_KEYS}
    #: 规划目标始终以**归一化后的显式输入**进入指纹：请求给出时用请求值（它将在发布
    #: 阶段与结论同事务写入），否则用 state 里已确认的值（与
    #: :meth:`CNSCorridorGapService.plan` 同口径）。
    raw_objectives = payload.get("cns_planning_objectives")
    inputs["cns_planning_objectives"] = (
        normalize_cns_planning_objectives(raw_objectives)
        if raw_objectives is not None
        else deepcopy(state.get("cns_planning_objectives") or {})
    )
    inputs["upstream_canonical"] = {
        key: _upstream_identity(state, key) for key in P15_UPSTREAM_RESULTS
    }
    return inputs


def _p15_scope(state, payload):
    routes = (state if isinstance(state, dict) else {}).get("operational_routes") or []
    route_ids = sorted(
        str(item.get("route_id") or "") for item in routes if isinstance(item, dict)
    )
    return "cns_corridor_gap:" + ("|".join(route_ids) if route_ids else "no-route")


def _p15_worker_payload(state, payload):
    """worker 侧重建输入所需的最小覆盖项（只保留请求显式给出的规划目标）。

    请求未给出目标时**不**写入：这样"本次请求的目标"与"state 里已确认的目标"不会
    分裂成两个不同指纹（与 ``_p16_worker_payload`` 同一口径）。
    """

    payload = payload if isinstance(payload, dict) else {}
    compact = {}
    raw_objectives = payload.get("cns_planning_objectives")
    if raw_objectives is not None:
        compact["cns_planning_objectives"] = normalize_cns_planning_objectives(
            raw_objectives
        )
    return compact


def _p15_plan(state, payload):
    worker_payload = _p15_worker_payload(state, payload)
    return {
        "worker_payload": worker_payload,
        "inputs": _p15_inputs(state, worker_payload),
    }


class P15UpstreamNotCurrent(TaskInputChanged):
    """P15 的上游 canonical 结论已变化：任务作废，绝不据此发布新结论。

    ``code`` 独立登记，让服务端能给出**具体**的中文业务原因（而不是笼统的
    "输入已变化"）：P15 的输入是上游走廊结论，用户需要知道"先重算上游"。
    """

    code = "task_p15_upstream_not_current"

    def __init__(self, message="上游 CNS 服务走廊结论已变化，请先重算服务走廊，再重新运行能力缺口评估"):
        super().__init__(message)


def _p15_summary(result):
    """canonical 结果的**摘要**（明细进 artifact，状态只留这些可读计数）。"""

    result = result if isinstance(result, dict) else {}
    routes = result.get("routes") if isinstance(result.get("routes"), list) else []
    return {
        "status": result.get("status"),
        "route_count": len(routes),
        "subsystem_count": sum(
            len(item.get("subsystems") or []) for item in routes if isinstance(item, dict)
        ),
        "input_fingerprint": result.get("input_fingerprint"),
    }


def _p15_runner(context):
    """worker 侧：在项目状态的**内存副本**上重算 P15，只 stage 结果、绝不发布。"""

    from ..application.corridor_gap_service import CNSCorridorGapService

    snapshot = context.inputs.get("snapshot") if isinstance(context.inputs, dict) else {}
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    inputs = snapshot.get("inputs") or {}
    worker_payload = snapshot.get("worker_payload") or {}
    algorithms = context.algorithms()
    #: 先按声明清单预解析：算法版本在提交后不可用时立刻失败，绝不部分运行。
    algorithms.resolve_all(P15_ALGORITHM_TYPES)
    context.check_cancel()
    context.progress(0.02, "正在准备 CNS 能力缺口输入")
    state = deepcopy(context.project_state)
    if not isinstance(state, dict) or not state:
        raise TaskInputChanged("项目状态不可读，能力缺口评估任务作废")
    #: 自证上游身份：提交时冻结的 P14 指纹必须与当前 canonical 结论一致。
    expected = inputs.get("upstream_canonical") or {}
    for key in P15_UPSTREAM_RESULTS:
        current = state.get(key)
        current_fingerprint = (
            current.get("input_fingerprint") if isinstance(current, dict) else None
        )
        expected_fingerprint = (expected.get(key) or {}).get("input_fingerprint")
        if str(current_fingerprint or "") != str(expected_fingerprint or ""):
            raise P15UpstreamNotCurrent()
    service = CNSCorridorGapService(
        _WorkerReadOnlySession(state),
        algorithms.create("corridor_gap_analyzer"),
        _WorkerInvalidation(),
        _worker_snapshot_passthrough,
    )
    outcome = service.plan(
        worker_payload,
        on_progress=lambda value, message=None: context.progress(value, message),
        cancel_check=context.check_cancel,
    )
    result = outcome.get("result")
    if not isinstance(result, dict) or not result:
        raise RuntimeError("CNS 能力缺口计算没有产出结果")
    context.check_cancel()
    context.progress(0.98, "正在写入临时结果明细")
    metadata = context.stage(
        {
            "logical_key": "cns_corridor_gap_assessment",
            "value": {
                "result": result,
                "objectives": outcome.get("objectives"),
                "objectives_declared": bool(outcome.get("objectives_declared")),
            },
        },
        artifact_type="corridor.gap.detail",
    )
    summary = _p15_summary(result)
    return TaskRunResult(
        summary=summary,
        staged=WorkerResultRef(
            artifact_id=str(metadata.get("artifact_id")),
            relative_path=str(metadata.get("relative_path")),
            sha256=str(metadata.get("sha256")),
            size_bytes=int(metadata.get("size_bytes") or 0),
            artifact_type="corridor.gap.detail",
            summary=summary,
        ),
        message="CNS 能力缺口计算完成，等待发布",
        progress=0.98,
    )


# ---- 测试专用 task type（不承载任何业务，只用于验证运行时契约） -----------------
#
# 它让 B6X 的取消 / 心跳 / 崩溃恢复 / 并发提交测试使用**真实**的运行时路径
# （真 worker 进程、真 store、真 artifact staging），而不是打桩。它不写任何
# canonical result，只写一个下划线前缀的探针字段。


PROBE_TASK_TYPE = "runtime_probe"
#: 测试专用状态键：下划线前缀 + 明确语义，不参与任何 production 失效链。
PROBE_STATE_KEY = "_heavy_task_probe"


def _probe_scope(state, payload):
    payload = payload if isinstance(payload, dict) else {}
    return f"runtime_probe:{payload.get('probe_id') or 'default'}"


def _probe_inputs(state, payload):
    payload = payload if isinstance(payload, dict) else {}
    return {
        "probe_id": str(payload.get("probe_id") or "default"),
        "marker": str(payload.get("marker") or ""),
        "payload_bytes": int(payload.get("payload_bytes") or 0),
        "steps": int(payload.get("steps") or 4),
        "step_seconds": float(payload.get("step_seconds") or 0.0),
        "crash": bool(payload.get("crash")),
        "fail": bool(payload.get("fail")),
    }


def _probe_plan(state, payload):
    payload = payload if isinstance(payload, dict) else {}
    worker_payload = deepcopy(payload)
    return {
        "worker_payload": worker_payload,
        # 探针的输入就是它的 payload（steps / crash 同时是控制字段与输入）。
        "inputs": _probe_inputs(state, worker_payload),
    }


def _probe_runner(context):
    inputs = context.inputs["snapshot"]["inputs"]
    steps = max(1, int(inputs["steps"]))
    for index in range(steps):
        context.check_cancel()
        time.sleep(float(inputs["step_seconds"]))
        context.progress(
            (index + 1) / float(steps + 1),
            f"探针进度 {index + 1}/{steps}",
        )
    context.check_cancel()
    if inputs["crash"]:
        import os

        # 模拟 worker 进程崩溃（不是失败返回）：用 SIGKILL 语义的强制退出。
        os._exit(17)
    if inputs["fail"]:
        raise RuntimeError("探针要求本次任务失败")
    payload = {
        "logical_key": PROBE_TASK_TYPE,
        "value": {
            "probe_id": inputs["probe_id"],
            "marker": inputs["marker"],
            "blob": "x" * int(inputs["payload_bytes"]),
        },
    }
    metadata = context.stage(payload, artifact_type="runtime.probe")
    return TaskRunResult(
        summary={"probe_id": inputs["probe_id"], "marker": inputs["marker"]},
        staged=WorkerResultRef(
            artifact_id=str(metadata.get("artifact_id")),
            relative_path=str(metadata.get("relative_path")),
            sha256=str(metadata.get("sha256")),
            size_bytes=int(metadata.get("size_bytes") or 0),
            artifact_type="runtime.probe",
            summary={"probe_id": inputs["probe_id"], "marker": inputs["marker"]},
        ),
        message="探针计算完成，等待发布",
        progress=0.9,
    )


CORRIDOR_TASK_TYPE = "cns_service_corridor_evaluate"
PCF_TASK_TYPE = "planning_constraint_field_generate"

_CORRIDOR_SPEC = TaskSpec(
    task_type=CORRIDOR_TASK_TYPE,
    task_name="服务走廊评估",
    message="服务走廊评估已提交，正在后台计算",
    business_endpoint="/api/cns-service-corridor/evaluate",
    scope_key=_corridor_scope,
    input_snapshot=_corridor_inputs,
    submit_plan=_corridor_plan,
    runner=_corridor_runner,
    release="cns_corridor_assessment",
    # B6R：这三个算法选择真正影响 corridor 结果，必须进入 immutable snapshot。
    algorithm_types=CORRIDOR_ALGORITHM_TYPES,
)

_PCF_SPEC = TaskSpec(
    task_type=PCF_TASK_TYPE,
    task_name="规划约束场生成",
    message="规划约束场生成已提交，正在后台计算",
    business_endpoint="/api/planning-constraint-fields/evaluate",
    scope_key=_pcf_scope,
    input_snapshot=_pcf_components,
    submit_plan=_pcf_plan,
    runner=_pcf_runner,
    release="planning_constraint_fields",
    # PCF 是纯判定：结果由逐格事实与 policy 决定，没有任何算法选择参与。
    algorithm_types=(),
)

_PROBE_SPEC = TaskSpec(
    task_type=PROBE_TASK_TYPE,
    task_name="运行时探针",
    message="运行时探针已提交",
    business_endpoint="/api/tasks",
    scope_key=_probe_scope,
    input_snapshot=_probe_inputs,
    submit_plan=_probe_plan,
    runner=_probe_runner,
    algorithm_types=(),
)

_P15_SPEC = TaskSpec(
    task_type=P15_TASK_TYPE,
    task_name="CNS 能力缺口评估",
    message="CNS 能力缺口评估已提交，正在后台计算",
    business_endpoint="/api/cns-corridor-gap/evaluate",
    scope_key=_p15_scope,
    input_snapshot=_p15_inputs,
    submit_plan=_p15_plan,
    runner=_p15_runner,
    release="cns_corridor_gap_assessment",
    algorithm_types=P15_ALGORITHM_TYPES,
)

_P16_SPEC = TaskSpec(
    task_type=P16_TASK_TYPE,
    task_name="CNS 设施规划",
    message="CNS 设施规划已提交，正在后台计算",
    business_endpoint="/api/cns-corridor-site-plan/evaluate",
    scope_key=_p16_scope,
    input_snapshot=_p16_inputs,
    submit_plan=_p16_plan,
    runner=_p16_runner,
    release="cns_corridor_site_plan",
    algorithm_types=P16_ALGORITHM_TYPES,
)

for _spec in (_CORRIDOR_SPEC, _PCF_SPEC, _P15_SPEC, _P16_SPEC, _PROBE_SPEC):
    register_task_spec(_spec)


__all__ = [
    "CORRIDOR_TASK_TYPE", "P15_TASK_TYPE", "P15UpstreamNotCurrent", "P16_TASK_TYPE",
    "P16UpstreamNotCurrent", "PCF_TASK_TYPE",
    "PROBE_STATE_KEY", "PROBE_TASK_TYPE", "TASK_ENDPOINTS", "TaskCancelled",
    "TaskInputChanged", "fingerprint_payload",
    "has_task_type",
    "register_task_spec", "task_spec", "task_specs", "task_type_for_endpoint",
]
