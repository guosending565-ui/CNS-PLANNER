"""Phase4-B6X：独立 worker 进程（长计算不在 API request thread 内）。

用法（由 :class:`~cns_planner.tasks.service.HeavyTaskService` 拉起）::

    python -m cns_planner.tasks.worker \
        --task-id <task_id> --task-type <task_type> \
        --workdir <project state file> --store <task store dir>

worker 的职责严格限定为：

1. 领取任务（``queued`` → ``running``），记录 ``started_at`` / ``heartbeat_at`` /
   ``worker`` 身份；
2. **只读**项目 state，重新解析权威输入并计算输入指纹；与提交时记录的指纹不一致
   时立即把任务置为 ``stale`` 并退出（绝不使用变化后的输入继续算，也绝不发布）；
3. 定期写 ``progress`` / ``heartbeat_at`` / ``message``（心跳线程），并在 major
   phase 与 artifact publish 之前做 cooperative cancel 检查；
4. 结果**先** staged 到 ``.cns-results/tmp/``（``ArtifactStore.publish_temporary``），
   只把摘要 + staged 引用返回出去；
5. 退出。canonical 发布由主进程的短 publish 阶段完成——worker **绝不**写
   ProjectState，也绝不长时间持有 ``mutation_lock``。

取消语义：收到 ``cancel_requested`` 后 worker 在最近的检查点（或心跳线程）中止计算、
丢弃 staged 临时文件、把任务写成 ``cancelled``，**不发布任何 canonical result**。
worker 进程崩溃/被杀时任务停在 ``running``；由服务端心跳超时策略统一标记为
``stale``（明确策略：不重算、不误判 succeeded）。
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import threading
import time
import traceback

from ..algorithms.registry import build_default_algorithm_registry
from ..persistence.artifact_store import ArtifactStore
from ..process_identity import build_identity
from .task_spec import TaskCancelled, TaskInputChanged, fingerprint_payload
from .task_specs import task_spec
from .task_store import (
    CANCELLED, FAILED, QUEUED, RUNNING, STALE, SUCCEEDED, TERMINAL_STATUSES,
    DEFAULT_HEARTBEAT_INTERVAL_SECONDS, DEFAULT_HEARTBEAT_TIMEOUT_SECONDS,
    TaskStore, resolve_task_store_path,
)


class WorkerContext:
    """worker 进程内的运行时上下文（心跳 / 取消检查 / staging / 模型构造）。"""

    def __init__(self, *, store, record, spec, workdir, interval_seconds,
                 project_root=None):
        self.store = store
        self.record = record
        self.spec = spec
        self.task_id = str(record.get("task_id"))
        self.workdir = Path(workdir)
        #: ``cns_planner`` 包所在的项目根：由服务端显式传入（项目状态文件可以位于
        #: 任意目录，绝不假设它与包目录的相对位置）。
        self.project_root = Path(
            project_root or os.environ.get("CNS_PROJECT_ROOT")
            or Path(__file__).resolve().parents[2]
        )
        self.interval_seconds = float(interval_seconds)
        self._heartbeat_lock = threading.RLock()
        self._cancel_event = threading.Event()
        self._done = threading.Event()
        self._thread = None
        self._registry = None
        self._project_state = None
        self.inputs = None
        self.progress_value = float(record.get("progress") or 0.0)

    # ---- 只读项目状态 --------------------------------------------------------

    @property
    def project_state(self):
        """以只读方式读取当前项目 state（绝不写盘）。

        项目文件使用"写临时文件 + 原子替换"持久化，因此正常情况下读到的一定是
        完整版本。这里仍然做有限重试：Word/杀软/并发替换偶发会短暂占用文件句柄，
        重试一次比把任务误判为"失败"更符合语义（重试仍失败才交给失败路径）。
        """

        if self._project_state is None:
            from ..persistence.project_compaction import restore_compacted_results
            from ..persistence.project_repository import ProjectRepository

            repository = ProjectRepository(self.workdir)
            if repository.exists():
                last_error = None
                for attempt in range(5):
                    try:
                        self._project_state = restore_compacted_results(
                            repository.load(), self.workdir
                        )
                        last_error = None
                        break
                    except (OSError, ValueError) as exc:  # 含 JSONDecodeError
                        last_error = exc
                        time.sleep(0.1 * (attempt + 1))
                if last_error is not None:
                    raise last_error
            else:
                self._project_state = {}
        return self._project_state

    def default_config_path(self):
        return self.project_root / "cns_planner" / "config" / "defaults.json"

    def default_registry(self):
        if self._registry is None:
            self._registry = build_default_algorithm_registry(
                json.loads(self.default_config_path().read_text(encoding="utf-8"))
            )
        return self._registry

    def model(self, algorithm_type, fallback_id):
        """按项目当前选择重建算法实例（精确 id/version/parameters）。"""

        state = self.project_state
        selected = ((state.get("algorithm_selection") or {}).get(algorithm_type) or {})
        algorithm_id = str(selected.get("algorithm_id") or fallback_id)
        version = str(selected.get("version") or selected.get("algorithm_version") or "")
        parameters = selected.get("parameters") if isinstance(selected.get("parameters"), dict) else {}
        registry = self.default_registry()
        if version:
            return registry.create(algorithm_type, algorithm_id, version, deepcopy(parameters))
        for manifest in registry.manifests(algorithm_type):
            if manifest.algorithm_id == algorithm_id:
                return registry.create(
                    algorithm_type, manifest.algorithm_id, manifest.version,
                    deepcopy(parameters),
                )
        raise ValueError(f"未注册算法：{algorithm_type}/{algorithm_id}")

    # ---- 心跳 / 取消 / 进度 --------------------------------------------------

    def start_heartbeat(self):
        self._thread = threading.Thread(
            target=self._heartbeat_loop, name=f"heavy-task-heartbeat-{self.task_id}",
            daemon=True,
        )
        self._thread.start()
        return self._thread

    def _heartbeat_loop(self):
        while not self._done.wait(self.interval_seconds):
            current = self.store.find(self.task_id)
            if current is None:
                return
            if current.get("cancel_requested"):
                self._cancel_event.set()
                if str(current.get("status")) == RUNNING:
                    try:
                        self.store.update(
                            self.task_id, status="cancelling",
                            message="已请求取消，正在停止计算",
                        )
                    except Exception:  # noqa: BLE001 - 心跳失败不掩盖计算错误
                        pass
            try:
                self.store.heartbeat(
                    self.task_id, progress=self.progress_value,
                    message=None, interval_seconds=self.interval_seconds,
                )
            except Exception:  # noqa: BLE001
                pass

    def stop_heartbeat(self, *, finished=False):
        if finished:
            self._done.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, self.interval_seconds * 2))

    def check_cancel(self):
        """cooperative cancel 检查点：major phase / artifact publish 之前调用。"""

        if self._cancel_event.is_set():
            raise TaskCancelled()
        current = self.store.find(self.task_id)
        if current is not None and current.get("cancel_requested"):
            self._cancel_event.set()
            self.stop_heartbeat(finished=True)
            raise TaskCancelled()

    def progress(self, value, message=None):
        self.progress_value = max(self.progress_value, float(value))
        try:
            self.store.heartbeat(
                self.task_id, progress=self.progress_value, message=message,
                interval_seconds=self.interval_seconds,
            )
        except Exception:  # noqa: BLE001
            pass

    # ---- staging ------------------------------------------------------------

    def stage(self, payload, *, artifact_type):
        """把派生 payload 写进可回收区；canonical 发布留到主进程 publish 阶段。"""

        store = ArtifactStore(self.workdir)
        producer = {
            "service": "HeavyTaskService",
            "algorithm_id": getattr(self.spec, "task_type", None),
            "algorithm_version": "1",
        }
        metadata = store.publish_temporary(
            payload, artifact_type=artifact_type, producer=producer,
            input_fingerprint=self.record.get("input_fingerprint"),
            scope={"task_id": self.task_id, "task_type": self.spec.task_type},
            summary={"task_id": self.task_id, "task_type": self.spec.task_type},
        )
        return metadata

    def restart_heartbeat_with_new_interval(self, interval_seconds):
        """测试用：把心跳间隔缩短后再跑（只在任务开始时调用一次）。"""

        self.interval_seconds = float(interval_seconds)
        self.stop_heartbeat(finished=True)
        self._done = threading.Event()
        self.start_heartbeat()


def _solve_inputs(context):
    """重新解析权威输入并与提交时记录的指纹比对。"""

    worker_payload = context.record.get("worker_payload")
    snapshot = context.spec.snapshot_for(
        context.project_state, worker_payload if isinstance(worker_payload, dict) else {}
    )
    fingerprint = fingerprint_payload(snapshot)
    return snapshot, fingerprint


def _mark_stale(context, *, reason, message):
    context.store.finish(
        context.task_id, status=STALE, message=message,
        error={"code": "task_input_changed", "message": reason},
        progress=context.progress_value,
    )


def run_task(*, task_id, task_type, workdir, store_directory,
             interval_seconds=DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
             project_root=None):
    """执行一个任务；返回退出码（0 = 正常结束）。"""

    store = TaskStore(store_directory)
    record = store.find(task_id)
    if record is None:
        return 2
    if str(record.get("status")) in TERMINAL_STATUSES:
        return 0
    spec = task_spec(task_type)
    # worker 身份：只记录"哪个运行实例、哪个项目根"，绝不写入 ProjectState。
    root = Path(project_root) if project_root else Path(__file__).resolve().parents[2]
    identity = build_identity(root)
    claimed = store.claim(task_id, worker={
        "pid": os.getpid(), "kind": "heavy-task-worker",
        "identity": identity.get("project_root") if isinstance(identity, dict) else None,
        "started_at": time.time(),
    })
    if claimed is None:
        # 已被其它 worker 领取或已取消：不重复执行。
        return 0
    interval = interval_seconds
    record = claimed
    context = WorkerContext(
        store=store, record=record, spec=spec, workdir=workdir,
        interval_seconds=interval, project_root=project_root,
    )
    context.start_heartbeat()
    try:
        if record.get("cancel_requested"):
            raise TaskCancelled()
        if str(record.get("input_fingerprint") or "") == "":
            raise ValueError("任务缺少输入指纹")
        snapshot, fingerprint = _solve_inputs(context)
        context.inputs = {"snapshot": snapshot, "fingerprint": fingerprint,
                          "expected_fingerprint": record.get("input_fingerprint")}
        if fingerprint != str(record.get("input_fingerprint") or ""):
            context.stop_heartbeat(finished=True)
            _mark_stale(
                context,
                reason="提交后输入已变化，未使用变化后的输入继续计算",
                message="输入已变化，请重新运行",
            )
            return 0
        result = spec.runner(context)
        context.check_cancel()
        context.store.finish(
            context.task_id, status=SUCCEEDED,
            message=result.message or "计算完成，等待发布",
            progress=result.progress,
            result_summary=result.as_dict(),
        )
        return 0
    except TaskCancelled:
        store.finish(
            task_id, status=CANCELLED,
            message="已取消",
            error={"code": "task_cancelled", "message": "任务按请求取消，未发布任何结果"},
            progress=context.progress_value,
        )
        return 0
    except TaskInputChanged as exc:
        _mark_stale(context, reason=str(exc), message="输入已变化，请重新运行")
        return 0
    except BaseException as exc:  # noqa: BLE001 - worker 必须把失败落盘而不是静默退出
        detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        store.finish(
            task_id, status=FAILED,
            message="执行失败",
            error={"code": "task_execution_failed", "message": detail[:500]},
            progress=context.progress_value,
        )
        return 1
    finally:
        try:
            context.stop_heartbeat(finished=True)
        except Exception:  # noqa: BLE001
            pass


def main(argv=None):
    parser = argparse.ArgumentParser(description="CNS Planner heavy task worker")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--task-type", required=True)
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--store", default=None)
    parser.add_argument("--project-root", default=None)
    parser.add_argument("--interval", type=float, default=DEFAULT_HEARTBEAT_INTERVAL_SECONDS)
    # 兼容：有些环境把未知参数透传进来，不因它失败。
    args, _unknown = parser.parse_known_args(argv)
    store_directory = args.store or str(resolve_task_store_path(args.workdir))
    if not os.environ.get("CNS_TASK_HEARTBEAT_TIMEOUT"):
        os.environ["CNS_TASK_HEARTBEAT_TIMEOUT"] = str(DEFAULT_HEARTBEAT_TIMEOUT_SECONDS)
    return run_task(
        task_id=args.task_id, task_type=args.task_type, workdir=args.workdir,
        store_directory=store_directory, interval_seconds=args.interval,
        project_root=args.project_root,
    )


if __name__ == "__main__":  # pragma: no cover - 进程入口
    sys.exit(main())
