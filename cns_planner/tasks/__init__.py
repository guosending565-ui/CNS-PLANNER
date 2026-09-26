"""Phase4-B6X：本地持久 Heavy Task 运行时（task contract / store / service / worker）。

本包是 Phase4 里**唯一**的长计算异步执行机制，边界严格限定：

* **独立 task store**：任务持久化在项目目录旁的 ``.cns-tasks/``，**绝不**塞进
  ``ProjectState``；canonical 结果仍然只由原本的 production owner 写入 state。
* **独立 worker 进程**：长计算不在 API request thread 内运行，也不在 HTTP 请求
  期间持有 ``mutation_lock``。worker 只做纯计算并把结果 staged 成 artifact。
* **短 publish 阶段**：只有 compare-and-publish 那一小段在主进程 ``mutation_lock``
  内执行：校验输入指纹 → 原子发布 artifact → 调用唯一 production owner 写入 state
  → 立即释放锁。
* **cooperative cancel**：worker 在 major phase / artifact publish 前检查
  ``cancel_requested``；取消中的任务绝不发布 canonical result。
* **重启恢复**：queued 可继续被领取；running 但 heartbeat 超时的任务显式标记为
  ``stale``（不重算、不误判 succeeded）；历史终态保留可读。

本包不包含任何业务算法，也不改变 P14 算法语义。
"""

from .task_store import (
    ACTIVE_STATUSES,
    CANCELLED,
    CANCELLING,
    CLAIMABLE_STATUSES,
    FAILED,
    QUEUED,
    RUNNING,
    STALE,
    SUCCEEDED,
    TASK_DIRECTORY,
    TASK_SCHEMA_VERSION,
    TASK_STATUSES,
    TERMINAL_STATUSES,
    TaskConflictError,
    TaskNotFoundError,
    TaskStore,
    resolve_task_store_path,
)

__all__ = [
    "ACTIVE_STATUSES", "CANCELLED", "CANCELLING", "CLAIMABLE_STATUSES", "FAILED",
    "QUEUED", "RUNNING", "STALE", "SUCCEEDED", "TASK_DIRECTORY",
    "TASK_SCHEMA_VERSION", "TASK_STATUSES", "TERMINAL_STATUSES",
    "TaskConflictError", "TaskNotFoundError", "TaskStore", "resolve_task_store_path",
]
