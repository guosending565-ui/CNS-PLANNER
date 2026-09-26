"""Phase4-B6X：Task Contract 运行时契约（状态文案、取消、提交与运行协议）。

本模块只定义**协议与共享小工具**，不含任何业务算法：

* :class:`TaskSpec` —— 一个 task type 的声明式定义（提交器 / 输入组装 / 运行器）。
* :class:`TaskCancelled` —— worker 内部用于中止计算的受控异常。
* :class:`WorkerInputs` / :class:`TaskRunResult` —— worker 与 service 之间的数据契约。
* :func:`fingerprint_payload` / :func:`status_message` —— 两侧共用的指纹与中文文案。

关键不变量：

* **输入指纹只覆盖输入**。它由 ``TaskSpec.input_snapshot(state, payload)`` 的确定性
  JSON 计算，两侧（HTTP 提交与 publish 阶段）得到同一函数与同一 state 时必须相等；
  用户改动了不相干的 state（例如 revision 自增）**不会**让任务变成 stale。
* **worker 不写 canonical state**。它只返回计算结果；state 由 publish 阶段在
  ``mutation_lock`` 内交给唯一 production owner 写入。
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
    """确定性 JSON → SHA-256。相同逻辑输入必然得到相同指纹。"""

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
    #: 把一次提交折叠成 ``{scope_id, fingerprint, worker_payload}``。默认实现直接
    #: 用请求 payload；需要缩小持久化体积的 task type 可以覆写成"只保留影响输入
    #: 指纹的字段"。worker 只用 ``worker_payload`` 重建输入，因此变小**不会**让
    #: 指纹失去含义。
    submit_plan: object = None

    def snapshot_for(self, state, payload):
        snapshot = self.input_snapshot(state if isinstance(state, dict) else {}, payload or {})
        return snapshot if isinstance(snapshot, dict) else {"value": snapshot}

    def fingerprint_for(self, state, payload):
        return fingerprint_payload(self.snapshot_for(state, payload))

    def plan_for(self, state, payload):
        if self.submit_plan is not None:
            plan = self.submit_plan(state if isinstance(state, dict) else {}, payload or {})
            return {
                "scope_id": str(plan.get("scope_id") or ""),
                "input_fingerprint": str(plan.get("input_fingerprint") or ""),
                "worker_payload": deepcopy(plan.get("worker_payload") or {}),
            }
        payload = payload if isinstance(payload, dict) else {}
        return {
            "scope_id": str(self.scope_key(state if isinstance(state, dict) else {}, payload)),
            "input_fingerprint": self.fingerprint_for(state, payload),
            "worker_payload": deepcopy(payload),
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
