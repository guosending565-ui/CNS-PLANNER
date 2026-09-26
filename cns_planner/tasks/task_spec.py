"""Phase4-B6X/B6R：Task Contract 运行时契约（状态文案、取消、提交与运行协议）。

本模块只定义**协议与共享小工具**，不含任何业务算法：

* :class:`TaskSpec` —— 一个 task type 的声明式定义（提交计划 / 计算输入 / 运行器）。
* :class:`TaskCancelled` —— worker 内部用于中止计算的受控异常。
* :class:`WorkerInputs` / :class:`TaskRunResult` —— worker 与 service 之间的数据契约。
* :func:`fingerprint_payload` / :func:`status_message` —— 两侧共用的摘要与中文文案。

关键不变量：

* **输入指纹只覆盖输入（含影响结果的算法选择）**。它是 immutable snapshot 的
  content-addressed 摘要（见 :mod:`cns_planner.tasks.task_input`）：提交时生成快照
  并得到指纹，publish 阶段用同一实现重新生成当前指纹，两者相等才允许发布；
  用户改动了不相干的 state（例如 project.name、revision 自增）**不会**让任务 stale。
* **worker 不写 canonical state，也不回读业务 state 组装输入**。它只按 snapshot 计算
  并返回结果；state 由 publish 阶段在 ``mutation_lock`` 内交给唯一 production owner 写入。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from hashlib import sha256
import json


TASK_STATUS_TEXT = {
    "queued": "正在排队",
    "running": "正在计算",
    "succeeded": "已完成",
    "failed": "执行失败",
    "cancelling": "正在取消",
    "cancelled": "已取消",
    "stale": "输入已变化，请重新运行",
}

#: 高级信息才显示的技术状态值（主界面只显示中文业务文案）。
ADVANCED_STATUSES = ("queued", "running", "succeeded", "failed", "cancelling",
                     "cancelled", "stale")


def status_message(status) -> str:
    return TASK_STATUS_TEXT.get(str(status), "状态未知")


def fingerprint_payload(payload) -> str:
    """确定性 JSON → SHA-256。

    注意：B6R 起 heavy task 的**输入指纹**是 immutable snapshot 的 content-addressed
    摘要（:func:`cns_planner.tasks.task_input.fingerprint_of`），本函数只保留给轻量的
    确定性摘要需求，不再用于 submit/publish 的 compare-and-publish。
    """

    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False, default=str,
    ).encode("utf-8")
    return sha256(raw).hexdigest()


class TaskCancelled(Exception):
    """受控取消：worker 中止计算，绝不发布 canonical result。"""

    code = "task_cancelled"

    def __init__(self, message="任务已按请求取消"):
        super().__init__(message)
        self.message = message


class TaskInputChanged(Exception):
    """计算期间输入已变化：任务进入 ``stale``，不覆盖当前 canonical result。"""

    code = "task_input_changed"

    def __init__(self, message="计算期间输入已变化，任务作废"):
        super().__init__(message)
        self.message = message


@dataclass
class WorkerInputs:
    """worker 侧解析出的权威输入。"""

    snapshot: dict
    fingerprint: str
    expected_fingerprint: str

    @property
    def matches(self) -> bool:
        return str(self.fingerprint) == str(self.expected_fingerprint)


@dataclass
class WorkerResultRef:
    """worker 完成后返回给 service 的 staged artifact 引用（不含 payload 本体）。"""

    artifact_id: str
    relative_path: str
    sha256: str
    size_bytes: int
    artifact_type: str
    summary: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "artifact_id": self.artifact_id,
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "size_bytes": int(self.size_bytes),
            "artifact_type": self.artifact_type,
            "status": "staged",
            "summary": deepcopy(self.summary),
        }


@dataclass
class TaskRunResult:
    """worker 计算产物：结果引用 + 结果摘要 + 进度终值。"""

    summary: dict = field(default_factory=dict)
    staged: WorkerResultRef | None = None
    message: str = "计算完成"
    progress: float = 1.0

    def as_dict(self) -> dict:
        return {
            "summary": deepcopy(self.summary),
            "staged": self.staged.as_dict() if self.staged else None,
            "message": self.message,
            "progress": float(self.progress),
        }


@dataclass
class TaskSpec:
    """一个 task type 的声明式定义。

    ``input_snapshot`` 必须是**纯函数**且只读 state：它决定输入指纹，因此两侧
    （HTTP 提交 / publish 校验）必须得到一致结果。``runner`` 只在 worker 进程内运行。

    Phase4-B6R：``algorithm_types`` 声明**哪些算法选择影响本任务结果**。它们会和
    ``input_snapshot`` 的输入一起进入 immutable snapshot，worker 只用 snapshot 里的
    清单重建算法（绝不回读当前 ``ProjectState`` 的 ``corridor_model`` 选择）。
    """

    task_type: str
    message: str
    business_endpoint: str
    scope_key: object
    input_snapshot: object
    runner: object
    task_name: str = ""
    revision_key: str = "revision"
    release: object = None
    #: 影响结果的算法类型（进入 snapshot 的 ``algorithms`` 段）。
    algorithm_types: tuple = ()
    #: 把一次提交折叠成 ``{scope_id, fingerprint, worker_payload}``。默认实现直接
    #: 用请求 payload；需要缩小持久化体积的 task type 可以覆写成"只保留影响输入
    #: 指纹的字段"。worker 只用 ``worker_payload`` 重建输入，因此变小**不会**让
    #: 指纹失去含义。
    submit_plan: object = None

    def inputs_for(self, state, payload):
        """本任务的计算输入（纯函数，只读 state）。"""

        snapshot = self.input_snapshot(state if isinstance(state, dict) else {}, payload or {})
        return snapshot if isinstance(snapshot, dict) else {"value": snapshot}

    def build_snapshot(self, state, payload, registry):
        """组装 immutable input snapshot（提交与 publish 两侧共用同一实现）。"""

        from .task_input import build_snapshot

        return build_snapshot(
            task_type=self.task_type,
            payload=payload or {},
            state=state if isinstance(state, dict) else {},
            registry=registry,
            algorithm_types=self.algorithm_types,
            inputs=self.inputs_for(state, payload),
        )

    def fingerprint_for(self, state, payload, registry=None):
        """输入指纹 = immutable snapshot 的内容寻址摘要。

        ``registry`` 由调用方（service / worker）提供：它是 runtime 配置（工程默认
        算法目录），不是业务输入；两侧必须用同一个 registry 才可比。
        """

        from .task_input import fingerprint_of

        if registry is None:
            raise ValueError("计算输入指纹需要算法注册表")
        return fingerprint_of(self.build_snapshot(state, payload, registry))

    def plan_for(self, state, payload, registry=None):
        if self.submit_plan is not None:
            plan = self.submit_plan(state if isinstance(state, dict) else {}, payload or {})
            return {
                "scope_id": str(plan.get("scope_id") or ""),
                "input_fingerprint": str(plan.get("input_fingerprint") or ""),
                "worker_payload": deepcopy(plan.get("worker_payload") or {}),
                # B6R：计算输入由 task type 自己组装（避免从 payload 二次推导出
                # 与 runner 实际使用不一致的输入）。
                "inputs": deepcopy(plan.get("inputs") or {}),
            }
        payload = payload if isinstance(payload, dict) else {}
        return {
            "scope_id": str(self.scope_key(state if isinstance(state, dict) else {}, payload)),
            "input_fingerprint": self.fingerprint_for(state, payload, registry),
            "worker_payload": deepcopy(payload),
            "inputs": self.inputs_for(state, payload),
        }

    def describe(self) -> dict:
        return {
            "task_type": self.task_type,
            "task_name": self.task_name or self.task_type,
            "business_endpoint": self.business_endpoint,
            "message": self.message,
        }


__all__ = [
    "ADVANCED_STATUSES", "TASK_STATUS_TEXT", "TaskCancelled", "TaskInputChanged",
    "TaskRunResult", "TaskSpec", "WorkerInputs", "WorkerResultRef",
    "fingerprint_payload", "status_message",
]
