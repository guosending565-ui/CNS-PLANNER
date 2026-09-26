"""Phase4-B6X/B6R：heavy task 的提交 / 发布适配层。

本模块是**唯一**把 task 运行时接到业务 use case 的地方：

* 提交（HTTP 线程，短）：解析权威输入 → **真正生成并持久化 immutable input
  snapshot**（``.cns-tasks/inputs/<sha256>.json.gz``）→ 写 task store → 返回
  ``202`` + ``task_id``。它**不**执行任何长计算，也**不**在计算期间持锁。
* 发布（publish 阶段，``mutation_lock`` 内极短）：用当前 canonical state 重新生成
  同一份 snapshot 并比较指纹 → 一致才原子 ``commit_staged`` → 调用对应业务 use
  case 的强制应用入口（该 use case 仍是 canonical result 的唯一 production
  owner）→ 立即释放锁。

业务 owner 没有被替换，也没有被复制：``CNSCorridorService.apply_computed`` 与
``PlanningConstraintFieldService.apply_field`` 就是同步路径本身使用的收尾逻辑。
"""

from __future__ import annotations

from copy import deepcopy
import os

from ..persistence.artifact_store import ArtifactStore
from .task_input import InputSnapshotStore, snapshot_reference
from .task_specs import (
    CORRIDOR_TASK_TYPE, PCF_TASK_TYPE, PROBE_STATE_KEY, PROBE_TASK_TYPE,
    has_task_type, task_spec,
)


class TaskInputChangedError(ValueError):
    code = "task_input_changed"


class TaskPublishError(ValueError):
    code = "task_publish_failed"


#: 各 task type 的 canonical result 名称（供 UI 展示"已更新哪个正式结果"）。
RESULT_SCOPE_TEXT = {
    CORRIDOR_TASK_TYPE: "服务走廊评估",
    PCF_TASK_TYPE: "规划约束场",
    PROBE_TASK_TYPE: "运行时探针",
}


def result_scope_text(task_type) -> str:
    return RESULT_SCOPE_TEXT.get(str(task_type), "业务结果")


def build_submission_snapshot(workflow, task_type, payload, *, registry=None):
    """在一致 state 上组装 immutable snapshot（只读 state，不写盘）。"""

    spec = task_spec(task_type)
    registry = registry if registry is not None else workflow.algorithm_registry
    plan = spec.plan_for(workflow.state, payload if isinstance(payload, dict) else {}, registry)
    return spec, plan, _snapshot_from_plan(spec, plan, workflow.state, registry)


def _snapshot_from_plan(spec, plan, state, registry):
    """按提交计划组装 snapshot（``inputs`` 与 ``worker_payload`` 与计划完全一致）。"""

    from .task_input import build_snapshot

    return build_snapshot(
        task_type=spec.task_type,
        payload=deepcopy(plan.get("worker_payload") or {}),
        state=state,
        registry=registry,
        algorithm_types=spec.algorithm_types,
        inputs=deepcopy(plan.get("inputs") or spec.inputs_for(state, {})),
    )


def plan_submission(workflow, task_type, payload, *, registry=None, store=None,
                    scope_id=None):
    """HTTP 线程内解析提交计划：scope + **immutable snapshot** + 输入指纹 + worker 覆盖项。

    它做了三件必须**原子**（对 state 一致快照）完成的事：解析 scope、组装 snapshot、
    持久化 snapshot。调用方（:class:`HeavyTaskService`）在 ``mutation_lock`` 内调用它，
    随后立即释放锁——长计算期间绝不持锁。不做任何长计算。
    """

    spec = task_spec(task_type)
    registry = registry if registry is not None else workflow.algorithm_registry
    store = store if store is not None else InputSnapshotStore(workflow.store_path)
    plan = spec.plan_for(
        workflow.state, payload if isinstance(payload, dict) else {}, registry,
        scope_id=scope_id,
    )
    snapshot = _snapshot_from_plan(spec, plan, workflow.state, registry)
    metadata = store.store(snapshot)
    reference = snapshot_reference(metadata.get("relative_path"))
    return {
        "task_type": spec.task_type,
        "scope_id": str(plan.get("scope_id") or ""),
        "input_fingerprint": str(metadata.get("artifact_id") or ""),
        "input_snapshot": snapshot,
        "input_snapshot_metadata": metadata,
        "worker_payload": deepcopy(plan.get("worker_payload") or {}),
        "input_revision": int(workflow.state.get("revision") or 0),
        "input_snapshot_ref": reference,
        "message": spec.message,
    }


def current_input_fingerprint(workflow, record, *, registry=None):
    """用当前 canonical state 重新生成输入指纹（publish compare-and-publish 用）。"""

    spec = task_spec(str(record.get("task_type")))
    worker_payload = record.get("worker_payload")
    return spec.fingerprint_for(
        workflow.state,
        worker_payload if isinstance(worker_payload, dict) else {},
        registry if registry is not None else workflow.algorithm_registry,
    )


def publish_staged_artifact(workdir, staged):
    """把 worker staged 的结果原子发布为 canonical artifact。"""

    store = ArtifactStore(workdir)
    metadata = staged if isinstance(staged, dict) else {}
    reference = {
        "artifact_id": metadata.get("artifact_id"),
        "sha256": metadata.get("sha256") or metadata.get("artifact_id"),
        "relative_path": metadata.get("relative_path"),
        "content_encoding": "json.gz",
        "schema_version": metadata.get("schema_version", 1),
        "artifact_type": metadata.get("artifact_type"),
        "size_bytes": metadata.get("size_bytes"),
        "status": "staged",
    }
    committed = store.commit_staged(reference, summary=deepcopy(metadata.get("summary") or {}))
    return committed


def _load_staged_payload(workdir, staged, committed):
    store = ArtifactStore(workdir)
    reference = dict(committed)
    reference["relative_path"] = committed.get("relative_path")
    payload = store.read(reference)
    if os.environ.get("CNS_TASK_DEBUG"):
        import sys

        print("[b6x] staged payload", type(payload).__name__,
              sorted(payload) if isinstance(payload, dict) else repr(payload)[:200],
              file=sys.stderr, flush=True)
    return payload


def publish_task_result(workflow, record, *, workdir):
    """publish 阶段（调用方已持有 ``mutation_lock``）。

    返回业务发布结果字典。**任何**失败都必须让当前 canonical result 保持不变：
    staged artifact 只有在真正写 state 之前才会被 commit，写入本身沿用同步路径
    的 ``session.save()`` 原子替换语义。

    B6R compare-and-publish：用**当前 canonical state** 重新生成输入指纹（与提交
    时同一实现、含算法选择清单），与 task record 里记录的 submitted 指纹比较；
    不同即 ``stale``，绝不覆盖当前 canonical result。
    """

    task_type = str(record.get("task_type"))
    if not has_task_type(task_type):
        raise TaskPublishError(f"未登记的 heavy task 类型：{task_type}")
    spec = task_spec(task_type)
    current_fingerprint = current_input_fingerprint(workflow, record)
    if current_fingerprint != str(record.get("input_fingerprint") or ""):
        raise TaskInputChangedError("输入已变化，未覆盖当前正式结果")

    run_result = record.get("result_summary") if isinstance(record.get("result_summary"), dict) else {}
    staged = run_result.get("staged") if isinstance(run_result.get("staged"), dict) else None
    summary = deepcopy(run_result.get("summary") or {})

    if task_type == PROBE_TASK_TYPE:
        workflow.state[PROBE_STATE_KEY] = {
            "task_id": record.get("task_id"),
            "probe_id": (summary or {}).get("probe_id"),
            "marker": (summary or {}).get("marker"),
            "input_fingerprint": record.get("input_fingerprint"),
            "published_at_revision": int(workflow.state.get("revision") or 0),
        }
        workflow.save()
        return {"task_type": task_type, "scope": result_scope_text(task_type),
                "status": "published", "summary": summary, "artifact_ref": None}

    if staged is None:
        raise TaskPublishError("任务没有可发布的 staged 结果")
    committed = publish_staged_artifact(workdir, staged)
    if task_type == CORRIDOR_TASK_TYPE:
        result = _staged_value(_load_staged_payload(workdir, staged, committed))
        if not isinstance(result, dict):
            raise TaskPublishError("staged 结果结构无效")
        workflow.cns_corridor_apply_computed(result)
        artifact_ref = workflow.cns_corridor_result_artifact_ref() or {
            key: committed.get(key) for key in
            ("artifact_id", "sha256", "relative_path", "content_encoding", "schema_version")
        }
        return {"task_type": task_type, "scope": result_scope_text(task_type),
                "status": "published", "summary": summary, "artifact_ref": artifact_ref}

    if task_type == PCF_TASK_TYPE:
        field = _staged_value(_load_staged_payload(workdir, staged, committed))
        if not isinstance(field, dict) or not field.get("altitude_layer_id"):
            raise TaskPublishError("staged 约束场结构无效")
        workflow.planning_constraint_field_apply_field(field)
        artifact_ref = workflow.planning_constraint_field_result_artifact_ref(field.get("altitude_layer_id")) or {
            key: committed.get(key) for key in
            ("artifact_id", "sha256", "relative_path", "content_encoding", "schema_version")
        }
        return {"task_type": task_type, "scope": result_scope_text(task_type),
                "status": "published", "summary": summary, "artifact_ref": artifact_ref}

    raise TaskPublishError(f"task type 没有发布路径：{task_type}")


def _staged_value(payload):
    """staged payload 的统一信封：``{"logical_key": ..., "value": ...}``。"""

    if isinstance(payload, dict) and "value" in payload and "logical_key" in payload:
        return payload.get("value")
    return payload


__all__ = [
    "RESULT_SCOPE_TEXT", "TaskInputChangedError", "TaskPublishError",
    "build_submission_snapshot", "current_input_fingerprint", "plan_submission",
    "publish_staged_artifact", "publish_task_result", "result_scope_text",
]
