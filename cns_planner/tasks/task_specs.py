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
import time

from ..domain.cns_corridor import normalize_cns_corridor_policy
from .task_input import CORRIDOR_ALGORITHM_TYPES
from .task_spec import (
    TaskCancelled, TaskInputChanged, TaskRunResult, TaskSpec, WorkerResultRef,
    fingerprint_payload,
)


#: 业务 endpoint → task type（router 用它判断某次 POST 是否异步提交）。
TASK_ENDPOINTS = {
    "/api/cns-service-corridor/evaluate": "cns_service_corridor_evaluate",
    "/api/planning-constraint-fields/evaluate": "planning_constraint_field_generate",
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


def _corridor_inputs(state, payload):
    """服务走廊评估的权威输入（只读 state，确定性）。

    B6R：**只**包含"输入事实"。算法选择（corridor_model / coverage_model /
    service_model 的 id/version/parameters）由 :mod:`cns_planner.tasks.task_input`
    作为同一份 immutable snapshot 的 ``algorithms`` 段记录，两者共同构成指纹。

    corridor policy 是随请求提交的显式输入，不是"当下 state"：提交时它被归一化后写进
    ``worker_payload``，此后任何一次重新生成（worker 变化检测 / publish compare）
    都只从 payload 取它。请求里没给 policy 时才回落到 state 里已确认的 policy。
    """

    payload = payload if isinstance(payload, dict) else {}
    raw_policy = payload.get("cns_corridor_policy", payload.get("policy"))
    if raw_policy is None:
        raw_policy = deepcopy(state.get("cns_corridor_policy")) or {}
    policy = normalize_cns_corridor_policy(raw_policy)
    return {
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
    }


def _corridor_scope(state, payload):
    routes = state.get("operational_routes") or []
    route_ids = sorted(str(item.get("route_id") or "") for item in routes if isinstance(item, dict))
    return "cns_service_corridor:" + ("|".join(route_ids) if route_ids else "no-route")


def _corridor_worker_payload(state, payload):
    """worker 侧重建输入所需的最小覆盖项。

    只保留请求显式给出的 policy（归一化后），请求未给出时**不**写入：这样
    "本次请求的 policy"与"state 里已有的 policy"不会被混成两个不同的输入指纹。
    """

    payload = payload if isinstance(payload, dict) else {}
    compact = {}
    raw_policy = payload.get("cns_corridor_policy", payload.get("policy"))
    if raw_policy is not None:
        compact["cns_corridor_policy"] = normalize_cns_corridor_policy(raw_policy)
    return compact


def _corridor_plan(state, payload):
    worker_payload = _corridor_worker_payload(state, payload)
    return {
        "scope_id": _corridor_scope(state, payload),
        "worker_payload": worker_payload,
        "inputs": _corridor_inputs(state, worker_payload),
    }


def _corridor_runner(context):
    snapshot = context.inputs["snapshot"]
    inputs = snapshot.get("inputs") or {}
    algorithms = context.algorithms()
    context.check_cancel()
    context.progress(0.05, "正在准备服务走廊评估输入")
    from ..catalogs import AircraftCNSProfileCatalog

    profile = AircraftCNSProfileCatalog.find(
        inputs.get("aircraft_profiles") or {},
        inputs.get("selected_aircraft_profile_id") or "",
    )
    # B6R：算法只从 snapshot 的 manifest 重建，绝不读当前 ProjectState 的算法选择。
    model = algorithms.create("corridor_model")
    coverage_parameters = algorithms.manifest("coverage_model").get("parameters") or {}
    capability_parameters = algorithms.manifest("service_model").get("parameters") or {}
    context.check_cancel()
    context.progress(0.15, "正在计算服务走廊（体素探测与服务能力判定）")
    result = model.evaluate(
        inputs.get("routes") or [], inputs.get("spatial_3d") or {},
        inputs.get("grid") or {}, inputs.get("grid_attributes") or {},
        inputs.get("required_cns") or {}, profile,
        inputs.get("existing_cns_facilities") or {}, inputs.get("device_catalog") or {},
        inputs.get("corridor_policy") or {},
        coverage_parameters=coverage_parameters,
        capability_parameters=capability_parameters,
    )
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
    components = _pcf_components(state, payload)
    layer_id = str((components.get("altitude_layer") or {}).get("altitude_layer_id") or "unresolved")
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
        "scope_id": _pcf_scope(state, payload),
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
        "scope_id": _probe_scope(state, worker_payload),
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

for _spec in (_CORRIDOR_SPEC, _PCF_SPEC, _PROBE_SPEC):
    register_task_spec(_spec)


__all__ = [
    "CORRIDOR_TASK_TYPE", "PCF_TASK_TYPE", "PROBE_STATE_KEY", "PROBE_TASK_TYPE",
    "TASK_ENDPOINTS", "TaskCancelled", "TaskInputChanged", "fingerprint_payload",
    "has_task_type", "register_task_spec", "task_spec", "task_specs",
    "task_type_for_endpoint",
]
