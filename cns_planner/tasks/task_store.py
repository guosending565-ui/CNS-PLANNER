"""Phase4-B6X：持久化 Heavy Task store（独立于 ProjectState）。

存储位置（**不在** ProjectState 里）::

    <workdir>/.cns-tasks/
        tasks/<task_id>.json      # 每个任务一个文件：状态、进度、心跳、指纹、引用
        tasks/<task_id>.publish.json  # 可选：publish 后的 canonical artifact 引用

设计要点：

* **一任务一文件**：状态转移与心跳写入互相独立，避免长任务写盘时把整个任务历史
  重写一遍；worker 的每次心跳只重写自己那一个文件，并且原子替换。
* **跨进程可见**：worker 是独立进程，读任务只依赖 store 目录本身（不依赖任何
  进程内对象），因此不存在"worker 内存状态与 API 内存状态不一致"的问题。
  首次访问同一个 store 路径的 ``TaskStore`` 实例会从磁盘重新读取一次，之后由
  进程内的读写锁串行化（同一进程内的多线程 API server 用同一把锁）。
* **失败即失败**：损坏的任务文件不会被当成"成功"，读取时抛结构化错误或按显式
  ``stale`` 处理；``recover_orphans`` 只把心跳超时的 running / cancelling 任务
  标记为 ``stale``，**绝不**标记为 ``succeeded``。

本模块不含任何业务计算。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import threading
import time
from uuid import uuid4

from ..persistence.project_repository import _path_lock


TASK_SCHEMA_VERSION = 1
#: 项目工作区旁的独立任务目录；与 ``.cns-results``（artifact sidecar）并列。
TASK_DIRECTORY = ".cns-tasks"
_TASKS_SUBDIRECTORY = "tasks"
_METADATA_FILENAME = "store.json"

#: B6X Task Contract：至少这七个状态。
QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLING = "cancelling"
CANCELLED = "cancelled"
STALE = "stale"
TASK_STATUSES = (QUEUED, RUNNING, SUCCEEDED, FAILED, CANCELLING, CANCELLED, STALE)

#: 尚未结束的状态（"同 scope 只允许一个 active heavy task" 用它判定）。
ACTIVE_STATUSES = (QUEUED, RUNNING, CANCELLING)
#: 已结束、不再改变的状态。
TERMINAL_STATUSES = (SUCCEEDED, FAILED, CANCELLED, STALE)
#: worker 可以领取的状态。
CLAIMABLE_STATUSES = (QUEUED,)

#: Task Contract 里每条任务必须保存的字段（测试与审计按它校验）。
TASK_REQUIRED_FIELDS = (
    "task_id", "task_type", "status", "created_at", "started_at", "finished_at",
    "progress", "message", "heartbeat_at", "cancel_requested", "input_revision",
    "input_fingerprint", "input_snapshot_ref", "result_artifact_ref", "error",
)

#: 心跳超时（秒）。超过它仍处于 running / cancelling 的任务判定为"worker 已失联"。
DEFAULT_HEARTBEAT_TIMEOUT_SECONDS = 120.0
#: worker 心跳间隔（秒）。
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 5.0
#: worker 被要求停止后，服务端等待它自行收尾的宽限期（秒）。
DEFAULT_CANCEL_GRACE_SECONDS = 15.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_task_store_path(workdir=None, store_path=None) -> Path:
    """解析 task store 的根目录：``<workdir>/.cns-tasks``。"""

    root = store_path or (
        Path(workdir).parent / TASK_DIRECTORY if workdir else Path.cwd() / TASK_DIRECTORY
    )
    return Path(root)


class TaskNotFoundError(ValueError):
    code = "task_not_found"


class TaskConflictError(ValueError):
    code = "task_conflict"


class TaskStoreCorrupt(ValueError):
    code = "task_store_corrupt"


def _iso_plus(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=float(seconds))).isoformat()


def _parse_iso(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _sort_key(record) -> tuple:
    return (str(record.get("created_at") or ""), str(record.get("task_id") or ""))


class TaskStore:
    """项目目录旁的独立持久任务存储（多进程安全，一任务一文件）。"""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.tasks_directory = self.directory / _TASKS_SUBDIRECTORY
        self._lock = _path_lock(self.directory)
        # 进程内缓存 + 磁盘变更检测：``_stamp`` 是任务目录的
        # ``(文件数, max(mtime_ns))``，任何进程写入都会改变它。
        self._stamp = None
        self._records = {}

    # ---- 基本信息 -----------------------------------------------------------

    @property
    def metadata_path(self) -> Path:
        return self.directory / _METADATA_FILENAME

    def ensure(self):
        self.tasks_directory.mkdir(parents=True, exist_ok=True)
        if not self.metadata_path.is_file():
            self._write_json(self.metadata_path, {
                "schema_version": TASK_SCHEMA_VERSION,
                "kind": "cns_planner.heavy_task_store",
                "created_at": utc_now(),
            })
        return self

    def _directory_stamp(self):
        try:
            entries = list(self.tasks_directory.glob("*.json"))
        except OSError:  # pragma: no cover - 目录刚被清理
            return None
        return (len(entries), max((item.stat().st_mtime_ns for item in entries), default=0))

    def _task_path(self, task_id) -> Path:
        return self.tasks_directory / f"{task_id}.json"

    def _publish_path(self, task_id) -> Path:
        return self.tasks_directory / f"{task_id}.publish.json"

    def _write_json(self, path: Path, document):
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(document, ensure_ascii=False, sort_keys=True, allow_nan=False)
        temporary = path.with_name(f".{path.name}.{uuid4().hex[:12]}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            for attempt in range(5):
                try:
                    os.replace(temporary, path)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.01 * (attempt + 1))
        finally:
            temporary.unlink(missing_ok=True)

    # ---- 读 ------------------------------------------------------------------

    def _refresh(self):
        stamp = self._directory_stamp()
        if stamp == self._stamp and self._records:
            return
        records = {}
        for path in sorted(self.tasks_directory.glob("*.json")):
            if path.name.endswith(".publish.json"):
                continue
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                # 损坏的任务文件绝不当作成功：跳过它，但保留其它任务可读。
                continue
            if isinstance(document, dict) and document.get("task_id"):
                records[str(document["task_id"])] = document
        self._records = records
        self._stamp = stamp

    def get(self, task_id):
        with self._lock:
            self._refresh()
            record = self._records.get(str(task_id))
            if record is None:
                raise TaskNotFoundError(f"任务不存在：{task_id}")
            return deepcopy(record)

    def find(self, task_id):
        try:
            return self.get(task_id)
        except TaskNotFoundError:
            return None

    def list(self, *, status=None, task_type=None, scope_id=None, limit=None):
        with self._lock:
            self._refresh()
            items = [deepcopy(item) for item in self._records.values()]
        wanted = None if status is None else (
            {str(status)} if isinstance(status, str) else {str(item) for item in status}
        )
        if wanted is not None:
            items = [item for item in items if str(item.get("status")) in wanted]
        if task_type:
            items = [item for item in items if str(item.get("task_type")) == str(task_type)]
        if scope_id:
            items = [item for item in items if str(item.get("scope_id")) == str(scope_id)]
        items.sort(key=_sort_key)
        if limit is not None:
            items = items[-int(limit):]
        return items

    def find_active(self, scope_id):
        """同一 canonical result scope 的 active heavy task（最早创建的那个）。"""

        items = self.list(status=ACTIVE_STATUSES, scope_id=scope_id)
        return items[0] if items else None

    def publish_record(self, task_id):
        with self._lock:
            path = self._publish_path(task_id)
            if not path.is_file():
                return None
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
            return document if isinstance(document, dict) else None

    # ---- 写 ------------------------------------------------------------------

    def create(self, *, task_type, scope_id, message, input_fingerprint,
               input_revision, input_snapshot_ref=None, worker=None,
               worker_payload=None):
        self.ensure()
        task_id = f"task-{uuid4().hex[:16]}"
        record = {
            "schema_version": TASK_SCHEMA_VERSION,
            "task_id": task_id,
            "task_type": str(task_type),
            "scope_id": str(scope_id),
            "status": QUEUED,
            "created_at": utc_now(),
            "started_at": None,
            "finished_at": None,
            "progress": 0.0,
            "message": str(message or ""),
            "heartbeat_at": None,
            "cancel_requested": False,
            "cancel_requested_at": None,
            "input_revision": input_revision,
            "input_fingerprint": str(input_fingerprint or ""),
            "input_snapshot_ref": input_snapshot_ref,
            # worker 重建输入所需的**最小**覆盖项（不含整份 grid 等只读输入）。
            "worker_payload": deepcopy(worker_payload) if isinstance(worker_payload, dict) else {},
            "result_artifact_ref": None,
            "result_summary": None,
            "error": None,
            "worker": deepcopy(worker) if isinstance(worker, dict) else None,
        }
        with self._lock:
            self._refresh()
            self._write_json(self._task_path(task_id), record)
            self._records[task_id] = record
            self._stamp = None
        return deepcopy(record)

    def update(self, task_id, **changes):
        with self._lock:
            self._refresh()
            path = self._task_path(str(task_id))
            if not path.is_file():
                raise TaskNotFoundError(f"任务不存在：{task_id}")
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise TaskStoreCorrupt(f"任务文件损坏：{task_id}") from exc
            record.update(deepcopy(changes))
            self._write_json(path, record)
            self._records[str(task_id)] = record
            self._stamp = None
            return deepcopy(record)

    def set_publish(self, task_id, reference):
        with self._lock:
            self._write_json(self._publish_path(task_id), {
                "schema_version": TASK_SCHEMA_VERSION,
                "task_id": str(task_id),
                "published_at": utc_now(),
                "result_artifact_ref": deepcopy(reference),
            })

    # ---- 状态转移 ------------------------------------------------------------

    def claim(self, task_id, *, worker=None):
        """queued → running（worker 领取任务）。返回记录或 ``None``。"""

        with self._lock:
            self._refresh()
            path = self._task_path(str(task_id))
            if not path.is_file():
                return None
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
            if record.get("status") != QUEUED:
                return None
            now = utc_now()
            record.update({
                "status": RUNNING, "started_at": record.get("started_at") or now,
                "heartbeat_at": now, "progress": 0.0,
                "cancel_requested": bool(record.get("cancel_requested")),
                "worker": deepcopy(worker) if isinstance(worker, dict) else record.get("worker"),
            })
            self._write_json(path, record)
            self._records[str(task_id)] = record
            self._stamp = None
            return deepcopy(record)

    def next_claimable(self, *, task_type=None, exclude=()):
        items = self.list(status=CLAIMABLE_STATUSES, task_type=task_type)
        skipped = {str(item) for item in exclude or ()}
        for item in items:
            if str(item.get("task_id")) not in skipped:
                return item
        return None

    def heartbeat(self, task_id, *, progress=None, message=None, interval_seconds=None):
        changes = {"heartbeat_at": utc_now()}
        if progress is not None:
            changes["progress"] = float(progress)
        if message is not None:
            changes["message"] = str(message)
        if interval_seconds is not None:
            changes["heartbeat_interval_seconds"] = float(interval_seconds)
        return self.update(task_id, **changes)

    def request_cancel(self, task_id):
        """请求取消：queued 直接终态；running 进入 cancelling（cooperative）。"""

        with self._lock:
            self._refresh()
            record = self._records.get(str(task_id))
            if record is None:
                raise TaskNotFoundError(f"任务不存在：{task_id}")
            status = str(record.get("status"))
            if status in TERMINAL_STATUSES:
                return deepcopy(record), "already_finished"
            if status == QUEUED:
                return self.update(
                    task_id, status=CANCELLED, cancel_requested=True,
                    cancel_requested_at=utc_now(), finished_at=utc_now(),
                    message="任务已取消（尚未开始计算）",
                    error={"code": "task_cancelled", "message": "用户取消了排队中的任务"},
                ), "cancelled_queued"
            if status == RUNNING:
                return self.update(
                    task_id, status=CANCELLING, cancel_requested=True,
                    cancel_requested_at=utc_now(),
                    message="已请求取消，正在停止计算",
                ), "cancel_requested"
            # 已经是 cancelling：重复请求是幂等的。
            return deepcopy(record), "cancel_requested"

    def finish(self, task_id, *, status, message=None, error=None, progress=None,
               result_summary=None, result_artifact_ref=None):
        changes = {
            "status": str(status), "finished_at": utc_now(), "heartbeat_at": utc_now(),
        }
        if message is not None:
            changes["message"] = str(message)
        if error is not None:
            changes["error"] = deepcopy(error)
        if progress is not None:
            changes["progress"] = float(progress)
        if result_summary is not None:
            changes["result_summary"] = deepcopy(result_summary)
        if result_artifact_ref is not None:
            changes["result_artifact_ref"] = deepcopy(result_artifact_ref)
        return self.update(task_id, **changes)

    # ---- 重启恢复 ------------------------------------------------------------

    def recover_orphans(self, *, timeout_seconds=DEFAULT_HEARTBEAT_TIMEOUT_SECONDS,
                        exclude=()):
        """启动恢复：心跳超时的 running / cancelling 任务显式标记 ``stale``。

        明确策略（B6X）：**不重算、不误判 succeeded**。失联任务进入 ``stale`` 终态，
        业务文案为"计算进程已中断，请重新运行"；历史终态原样保留。``queued`` 任务
        不动，仍可被 worker 正常领取。
        """

        cutoff = datetime.now(timezone.utc) - timedelta(seconds=float(timeout_seconds))
        skipped = {str(item) for item in exclude or ()}
        recovered = []
        for record in self.list(status=(RUNNING, CANCELLING)):
            task_id = str(record.get("task_id"))
            if task_id in skipped:
                continue
            heartbeat = _parse_iso(record.get("heartbeat_at")) or _parse_iso(record.get("started_at"))
            if heartbeat is not None and heartbeat > cutoff:
                continue
            was_cancelling = str(record.get("status")) == CANCELLING
            recovered.append(self.finish(
                task_id,
                status=CANCELLED if was_cancelling else STALE,
                message=(
                    "任务已取消（计算进程在重试前已停止）" if was_cancelling
                    else "计算进程已中断，请重新运行"
                ),
                error={
                    "code": "task_worker_lost",
                    "message": "worker 心跳超时，任务被标记为中断；未发布任何结果",
                },
                progress=float(record.get("progress") or 0.0),
            ))
        return recovered

    def find_stalled(self, *, timeout_seconds):
        """仍处于 running / cancelling 且心跳已过期的任务（供强制收尾）。"""

        cutoff = datetime.now(timezone.utc) - timedelta(seconds=float(timeout_seconds))
        stalled = []
        for record in self.list(status=(RUNNING, CANCELLING)):
            heartbeat = _parse_iso(record.get("heartbeat_at")) or _parse_iso(record.get("started_at"))
            if heartbeat is None or heartbeat <= cutoff:
                stalled.append(record)
        return stalled


__all__ = [
    "ACTIVE_STATUSES", "CANCELLED", "CANCELLING", "CLAIMABLE_STATUSES",
    "DEFAULT_CANCEL_GRACE_SECONDS", "DEFAULT_HEARTBEAT_INTERVAL_SECONDS",
    "DEFAULT_HEARTBEAT_TIMEOUT_SECONDS", "FAILED", "QUEUED", "RUNNING", "STALE",
    "SUCCEEDED", "TASK_DIRECTORY", "TASK_REQUIRED_FIELDS", "TASK_SCHEMA_VERSION",
    "TASK_STATUSES", "TERMINAL_STATUSES", "TaskConflictError", "TaskNotFoundError",
    "TaskStore", "TaskStoreCorrupt", "resolve_task_store_path", "utc_now",
]
