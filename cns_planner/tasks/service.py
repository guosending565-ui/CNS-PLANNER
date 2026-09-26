"""Phase4-B6X：HeavyTaskService —— 提交 / 监控 / 取消 / compare-and-publish。

职责边界（严格保持）：

* **提交路径**（HTTP 线程，极短）：解析输入指纹与 scope → 写 task store →
  ``queued`` → 返回 ``task_id``。不执行长计算、不持有 ``mutation_lock``。
* **worker 路径**（独立进程）：领取 ``queued`` → ``running``，定期写
  progress/heartbeat，在 major phase 与 artifact publish 前做 cooperative cancel
  检查，结果 staged 成 artifact。worker **绝不**写 ProjectState。
* **publish 路径**（本进程，``mutation_lock`` 内极短）：校验输入指纹 → 原子
  ``commit_staged`` → 调用唯一 production owner 写 canonical result → 立即释放。
  输入已变化时任务变成 ``stale``，当前 canonical result 保持不变。
* **恢复路径**：服务/worker 重启后 ``queued`` 继续可领取；``running`` /
  ``cancelling`` 且心跳超时的任务被显式标记为 ``stale`` / ``cancelled``（明确策略：
  不重算、绝不误判 succeeded）；历史终态原样保留可读。

业务语义（中文文案、release scope、业务 endpoint）全部来自
:mod:`cns_planner.tasks.task_specs` 与 :mod:`cns_planner.tasks.handlers`；
本模块只做编排。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from .handlers import (
    TaskInputChangedError, TaskPublishError, result_scope_text,
)
from .task_spec import ADVANCED_STATUSES, TASK_STATUS_TEXT, status_message
from .task_specs import has_task_type, task_spec, task_specs
from .task_store import (
    ACTIVE_STATUSES, CANCELLED, CANCELLING, FAILED, QUEUED, RUNNING, STALE,
    SUCCEEDED, TERMINAL_STATUSES, TASK_REQUIRED_FIELDS,
    DEFAULT_CANCEL_GRACE_SECONDS, DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    DEFAULT_HEARTBEAT_TIMEOUT_SECONDS, TaskConflictError, TaskNotFoundError,
    TaskStore, resolve_task_store_path,
)


#: 同 scope 重复提交时的处理策略。
CONFLICT_RETURN_EXISTING = "return_existing"
CONFLICT_REJECT = "conflict"

#: worker 心跳超时后，服务端最多再等多久才强制收尾（秒）。
_ORPHAN_GRACE_SECONDS = DEFAULT_CANCEL_GRACE_SECONDS


def _now():
    return datetime.now(timezone.utc)


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


def _seconds_since(value):
    moment = _parse_iso(value)
    if moment is None:
        return None
    return (_now() - moment).total_seconds()


class HeavyTaskService:
    def __init__(self, workflow, project_file, *, max_workers=2,
                 heartbeat_timeout=DEFAULT_HEARTBEAT_TIMEOUT_SECONDS,
                 heartbeat_interval=DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
                 poll_interval=0.5, autostart_workers=True,
                 store_directory=None, spawn=None, project_root=None):
        self.workflow = workflow
        self.project_file = Path(project_file)
        self.workdir = Path(project_file)
        #: worker 子进程的工作目录（必须是 ``cns_planner`` 包所在的项目根）。
        self.project_root = Path(project_root) if project_root else Path(__file__).resolve().parents[2]
        self.store = TaskStore(store_directory or resolve_task_store_path(self.workdir))
        self.max_workers = max(1, int(max_workers))
        self.heartbeat_timeout = float(heartbeat_timeout)
        self.heartbeat_interval = float(heartbeat_interval)
        self.poll_interval = float(poll_interval)
        self.autostart_workers = bool(autostart_workers)
        self._spawn = spawn
        self._lock = threading.RLock()
        self._processes: dict[str, subprocess.Popen] = {}
        self._thread = None
        self._stop = threading.Event()
        self._last_drive = 0.0
        self._started = False
        self._recovered = []
        self._published: dict[str, dict] = {}

    # ---- 生命周期 -----------------------------------------------------------

    def bind_project(self, project_file):
        """项目切换（Open / Save As）后重新绑定 task store 位置。"""

        with self._lock:
            self._terminate_all()
            self.project_file = Path(project_file)
            self.workdir = Path(project_file)
            self.store = TaskStore(resolve_task_store_path(self.workdir))
            self._published = {}
            self._recovered = []
        return self

    def start(self, *, recover=True):
        """启动编排线程，并按明确策略恢复重启前的任务。"""

        with self._lock:
            if recover and not self._started:
                self._recovered = self._recover_on_start()
            self._started = True
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(
                    target=self._loop, name="heavy-task-service", daemon=True,
                )
                self._thread.start()
        self.drive()
        return self

    def stop(self, *, timeout=5.0, terminate_workers=True):
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        if terminate_workers:
            with self._lock:
                self._terminate_all()
        return self

    def _loop(self):
        while not self._stop.wait(self.poll_interval):
            try:
                self.drive()
            except Exception:  # noqa: BLE001 - 编排循环绝不因单点异常退出
                continue

    def _recover_on_start(self):
        """重启恢复：心跳超时的 running/cancelling → stale/cancelled。"""

        self.store.ensure()
        return self.store.recover_orphans(timeout_seconds=self.heartbeat_timeout)

    # ---- 提交 --------------------------------------------------------------

    def submit(self, task_type, payload=None, *, conflict_policy=CONFLICT_RETURN_EXISTING):
        """提交 heavy task：返回 ``(task_record, created)``。"""

        if not has_task_type(task_type):
            raise TaskNotFoundError(f"未登记的 heavy task 类型：{task_type}")
        from .handlers import plan_submission

        with self._lock:
            self._drive_locked()
            plan = plan_submission(self.workflow, task_type, payload)
            existing = self.store.find_active(plan["scope_id"])
            if existing is not None:
                if conflict_policy == CONFLICT_REJECT:
                    raise TaskConflictError(
                        f"该范围已有进行中的任务（{existing.get('task_id')}），请先等待或取消它"
                    )
                return existing, False
            record = self.store.create(
                task_type=plan["task_type"], scope_id=plan["scope_id"],
                message=plan["message"], input_fingerprint=plan["input_fingerprint"],
                input_revision=plan["input_revision"],
                input_snapshot_ref=plan["input_snapshot_ref"],
                worker_payload=plan["worker_payload"],
            )
            self._recovered = []
        self.drive()
        return record, True

    # ---- 查询 --------------------------------------------------------------

    def get(self, task_id):
        return self._business_view(self.store.get(task_id))

    def find(self, task_id):
        record = self.store.find(task_id)
        return self._business_view(record) if record else None

    def list(self, *, status=None, task_type=None, limit=50):
        records = self.store.list(status=status, task_type=task_type, limit=limit)
        return [self._business_view(item) for item in records]

    def active(self):
        return self.list(status=ACTIVE_STATUSES, limit=50)

    def publish_record(self, task_id):
        return self.store.publish_record(task_id)

    def task_catalog(self):
        return [spec.describe() for spec in task_specs()]

    def _business_view(self, record):
        """业务视图：中文状态文案在前，技术枚举只在 ``advanced`` 里。"""

        if record is None:
            return None
        status = str(record.get("status"))
        task_type = str(record.get("task_type"))
        run_result = record.get("result_summary") if isinstance(record.get("result_summary"), dict) else {}
        result_payload = self._published.get(str(record.get("task_id"))) or {}
        view = {
            "task_id": record.get("task_id"),
            "task_type": task_type,
            "task_name": self._task_name(task_type),
            "status_text": status_message(status),
            "message": record.get("message") or status_message(status),
            "progress": float(record.get("progress") or 0.0),
            "created_at": record.get("created_at"),
            "started_at": record.get("started_at"),
            "finished_at": record.get("finished_at"),
            "heartbeat_at": record.get("heartbeat_at"),
            "heartbeat_age_seconds": _seconds_since(record.get("heartbeat_at")),
            "cancel_requested": bool(record.get("cancel_requested")),
            "can_cancel": status in ACTIVE_STATUSES,
            "scope_id": record.get("scope_id"),
            "result_scope": result_scope_text(task_type),
            "result_summary": deepcopy(run_result.get("summary") or {}),
            "release": deepcopy(result_payload.get("release") or {}),
            "business_error": self._business_error(record),
            "advanced": {
                "status": status,
                "status_values": list(ADVANCED_STATUSES),
                "task_id": record.get("task_id"),
                "task_type": task_type,
                "input_revision": record.get("input_revision"),
                "input_fingerprint": record.get("input_fingerprint"),
                "input_snapshot_ref": record.get("input_snapshot_ref"),
                "result_artifact_ref": record.get("result_artifact_ref"),
                "worker": deepcopy(record.get("worker")),
                "error": deepcopy(record.get("error")),
                "store_directory": str(self.store.directory),
                "contract_fields": list(TASK_REQUIRED_FIELDS),
            },
        }
        return view

    @staticmethod
    def _task_name(task_type):
        if has_task_type(task_type):
            return task_spec(task_type).task_name or task_type
        return task_type

    @staticmethod
    def _business_error(record):
        """技术错误只保留 code，用户可见的永远是中文业务说明。"""

        error = record.get("error")
        if not isinstance(error, dict) or not error:
            return None
        code = str(error.get("code") or "")
        text = {
            "task_cancelled": "任务已取消，未产生正式结果",
            "task_input_changed": "输入已变化，请重新运行",
            "task_worker_lost": "计算进程已中断，请重新运行",
            "task_execution_failed": "计算未能完成，请检查输入后重试",
            "task_publish_failed": "结果发布失败，原有正式结果未被替换",
        }.get(code, "计算未能完成，请重试")
        return {"text": text, "code": code or "task_error"}

    # ---- 取消 --------------------------------------------------------------

    def cancel(self, task_id):
        """请求取消。cooperative：worker 在检查点/心跳处停止并丢弃 staged 结果。"""

        with self._lock:
            record, outcome = self.store.request_cancel(task_id)
            if outcome == "cancel_requested":
                self._signal_cancel(record)
        self.drive()
        view = self._business_view(self.store.get(task_id))
        view["cancel_outcome"] = outcome
        return view

    def _signal_cancel(self, record):
        """通知 worker 进程停止（协作信号；宽限期后才强制终止）。"""

        process = self._processes.get(str(record.get("task_id")))
        self._safe_terminate(process, force=False)

    # ---- 编排 --------------------------------------------------------------

    def drive(self):
        with self._lock:
            return self._drive_locked()

    def _drive_locked(self):
        self._last_drive = time.time()
        self.store.ensure()
        self._collect_finished()
        self._publish_orphan_successes()
        self._reap_stalled()
        started = self._start_workers()
        return started

    def _publish_orphan_successes(self):
        """worker 已算出结果、但没有被本进程收集到的任务：补齐短 publish 阶段。

        场景：服务重启、或编排线程被替换（worker 子进程仍在磁盘上留下了
        ``succeeded`` 记录）。这类任务的结果已经 staged，只差 compare-and-publish。
        """

        for record in self.store.list(status=SUCCEEDED):
            task_id = str(record.get("task_id"))
            if task_id in self._processes or task_id in self._published:
                continue
            if self.store.publish_record(task_id) is not None:
                self._published[task_id] = {"release": {}}
                continue
            if not isinstance(record.get("result_summary"), dict):
                continue
            self._publish_task(task_id)

    def maybe_drive(self, *, min_interval=0.5):
        """按需驱动：HTTP 读取路径不会因为轮询而无限增长 worker 数量。"""

        if time.time() - self._last_drive < float(min_interval):
            return 0
        return self.drive()

    def _collect_finished(self):
        """收集已结束的 worker 进程，并在短 publish 阶段完成 canonical 发布。"""

        for task_id, process in list(self._processes.items()):
            if process.poll() is None:
                continue
            del self._processes[task_id]
            record = self.store.find(task_id)
            if record is None:
                continue
            status = str(record.get("status"))
            if status == SUCCEEDED:
                if task_id in self._published:
                    continue
                if self.store.publish_record(task_id) is not None:
                    # 服务重启前已经发布过：不再重复发布，只记住它已经完成。
                    self._published[task_id] = {"release": {}}
                    continue
                self._publish_task(task_id)
            elif status in (CANCELLED, STALE, FAILED):
                pass
            elif status == QUEUED and not record.get("started_at"):
                # worker 还没领取任务就退出了：这是启动失败，不是"长任务中断"，
                # 也绝不可能是成功。只在没有其它 worker 接手时收尾。
                self._finalize_unclaimed(task_id, record)
            else:
                # 进程已退出但任务没有终态：worker 被强杀或异常退出。
                self._finalize_orphan(task_id, record)

    def _finalize_unclaimed(self, task_id, record):
        if bool(record.get("cancel_requested")):
            self.store.finish(
                task_id, status=CANCELLED, message="已取消（尚未开始计算）",
                error={"code": "task_cancelled", "message": "任务在开始计算前被取消"},
            )
            return
        self.store.finish(
            task_id, status=FAILED, message="执行失败",
            error={"code": "task_execution_failed",
                   "message": "本地计算进程未能启动或未领取任务；未产生任何正式结果"},
        )

    def _publish_task(self, task_id):
        """短 publish 阶段：``mutation_lock`` 内只做 compare-and-publish。"""

        lock = getattr(self.workflow, "mutation_lock", None)
        acquired = False
        if lock is not None:
            lock.acquire()
            acquired = True
        try:
            record = self.store.get(task_id)
            if bool(record.get("cancel_requested")):
                self.store.finish(
                    task_id, status=CANCELLED, message="已取消",
                    error={"code": "task_cancelled",
                           "message": "取消请求到达时结果尚未发布，任务未产生正式结果"},
                )
                return
            from .handlers import publish_task_result

            outcome = publish_task_result(self.workflow, record, workdir=self.workdir)
            reference = outcome.get("artifact_ref")
            if reference:
                self.store.set_publish(task_id, reference)
            self.store.finish(
                task_id, status=SUCCEEDED, message="已完成",
                result_artifact_ref=reference, progress=1.0,
            )
            self._published[task_id] = {"release": outcome}
        except TaskInputChangedError as exc:
            self.store.finish(
                task_id, status=STALE, message="输入已变化，请重新运行",
                error={"code": "task_input_changed", "message": str(exc)},
            )
        except (TaskPublishError, Exception) as exc:  # noqa: BLE001 - 发布失败绝不替换旧结果
            self.store.finish(
                task_id, status=FAILED, message="执行失败",
                error={"code": "task_publish_failed", "message": str(exc)[:500]},
            )
        finally:
            if acquired:
                lock.release()

    def _finalize_orphan(self, task_id, record):
        """worker 进程没了但任务没有终态：显式标记，绝不当作成功。"""

        if bool(record.get("cancel_requested")):
            self.store.finish(
                task_id, status=CANCELLED, message="已取消",
                error={"code": "task_cancelled",
                       "message": "取消后 worker 进程结束，任务未产生正式结果"},
            )
            return
        self.store.finish(
            task_id, status=STALE, message="计算进程已中断，请重新运行",
            error={"code": "task_worker_lost",
                   "message": "worker 进程异常结束；未发布任何结果，也不会被标记为成功"},
        )

    def _reap_stalled(self):
        """心跳超时的任务与僵死的取消请求：按明确策略收尾。"""

        for record in self.store.find_stalled(timeout_seconds=self.heartbeat_timeout):
            task_id = str(record.get("task_id"))
            process = self._processes.get(task_id)
            alive = process is not None and process.poll() is None
            if str(record.get("status")) == CANCELLING and alive:
                # 已经请求取消但 worker 迟迟不返回：超过宽限期强制终止。
                if (_seconds_since(record.get("cancel_requested_at")) or 0) >= _ORPHAN_GRACE_SECONDS:
                    self._safe_terminate(process, force=True)
                continue
            if alive:
                if (_seconds_since(record.get("heartbeat_at")) or 0) >= (
                    self.heartbeat_timeout + _ORPHAN_GRACE_SECONDS
                ):
                    self._safe_terminate(process, force=True)
                    self._processes.pop(task_id, None)
                    self._finalize_orphan(task_id, record)
                continue
            self._processes.pop(task_id, None)
            self._finalize_orphan(task_id, record)

    def _start_workers(self):
        started = 0
        while len(self._processes) < self.max_workers:
            claimable = self.store.next_claimable(exclude=set(self._processes))
            if claimable is None:
                break
            task_id = str(claimable.get("task_id"))
            if self._launch_worker(task_id, str(claimable.get("task_type"))):
                started += 1
            else:
                break
        return started

    def _launch_worker(self, task_id, task_type):
        argv = self.worker_argv(task_id, task_type)
        try:
            if self._spawn is not None:
                process = self._spawn(argv)
            else:
                process = subprocess.Popen(
                    argv, cwd=str(self.project_root),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=self.worker_environment(),
                )
        except OSError:
            self.store.finish(
                task_id, status=FAILED, message="执行失败",
                error={"code": "task_execution_failed",
                       "message": "无法启动本地计算进程，请检查 Python 运行环境"},
            )
            return False
        self._processes[task_id] = process
        return True

    def worker_argv(self, task_id, task_type):
        return [
            sys.executable, "-m", "cns_planner.tasks.worker",
            "--task-id", str(task_id),
            "--task-type", str(task_type),
            "--workdir", str(self.workdir),
            "--store", str(self.store.directory),
            "--project-root", str(self.project_root),
            "--interval", str(self.heartbeat_interval),
        ]

    def worker_environment(self):
        environment = dict(os.environ)
        environment.setdefault("PYTHONUNBUFFERED", "1")
        environment["CNS_PROJECT_ROOT"] = str(self.project_root)
        root = str(self.project_root)
        existing = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = root if not existing else os.pathsep.join([root, existing])
        return environment

    # ---- 进程工具 ----------------------------------------------------------

    @staticmethod
    def _safe_terminate(process, *, force):
        if process is None or process.poll() is not None:
            return False
        try:
            if force:
                process.kill()
            else:
                process.terminate()
        except OSError:
            return False
        return True

    def _terminate_all(self):
        for process in list(self._processes.values()):
            self._safe_terminate(process, force=True)
        self._processes.clear()

    # ---- 测试/运维辅助 ------------------------------------------------------

    def wait_for_terminal(self, task_id, *, timeout=30.0, interval=0.05):
        """轮询驱动直到任务**不再需要本进程处理**（供测试与本地运维使用）。

        ``succeeded`` 只有在 canonical publish 完成后才算真正结束：worker 先写
        ``succeeded``（表示计算完成），publish 阶段随后把它收尾为最终形态。
        """

        deadline = time.time() + float(timeout)
        while time.time() < deadline:
            self.drive()
            record = self.store.find(task_id)
            if record is None:
                return None
            if self.is_terminal(record):
                return self._business_view(record)
            time.sleep(interval)
        return self.find(task_id)

    def is_terminal(self, record):
        """任务是否已经完成本进程的全部处理（含 compare-and-publish）。"""

        if not isinstance(record, dict):
            return False
        task_id = str(record.get("task_id"))
        status = str(record.get("status"))
        if status not in TERMINAL_STATUSES:
            return False
        if status != SUCCEEDED:
            return True
        return task_id in self._published

    @property
    def recovered_tasks(self):
        return list(self._recovered)


__all__ = [
    "CONFLICT_REJECT", "CONFLICT_RETURN_EXISTING", "HeavyTaskService",
    "TASK_STATUS_TEXT",
]
