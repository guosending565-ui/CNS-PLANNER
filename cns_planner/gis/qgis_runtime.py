"""Marshal GIS work back to the Qt/QGIS owner thread."""

from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
import os
from queue import Empty, Queue

from qgis.core import QgsApplication

#: Real-data planning is not a bounded interactive call: building the L7/L8 feasibility mask
#: reads a verified FABDEM window per grid cell, and the Theta* V2 search runs afterwards.
#: The previous hard 90 s budget aborted such a run *after* the work had already started
#: mutating state, and surfaced as an empty ``str(TimeoutError())`` error message.
DEFAULT_TASK_TIMEOUT_SECONDS = 1800
TASK_TIMEOUT_ENV = "CNS_QGIS_TASK_TIMEOUT_S"


def _resolve_timeout(timeout_seconds=None):
    if timeout_seconds is not None:
        return float(timeout_seconds)
    raw = os.environ.get(TASK_TIMEOUT_ENV)
    if raw is None or not str(raw).strip():
        return float(DEFAULT_TASK_TIMEOUT_SECONDS)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(DEFAULT_TASK_TIMEOUT_SECONDS)
    return value if value > 0 else float(DEFAULT_TASK_TIMEOUT_SECONDS)


class QgisRuntime:
    def __init__(self, timeout_seconds=None):
        self.timeout_seconds = _resolve_timeout(timeout_seconds)
        self.tasks = Queue()

    def call(self, action):
        future = Future()
        self.tasks.put((future, action))
        try:
            return future.result(timeout=self.timeout_seconds)
        except FutureTimeoutError as exc:
            # ``str(TimeoutError())`` is empty, which made the HTTP layer report a bare 400
            # with no explanation.  A timeout is not a "nothing happened" outcome either: the
            # action keeps running on the QGIS thread, so say so explicitly.
            raise TimeoutError(
                f"GIS 任务超过 {self.timeout_seconds:g} 秒未返回（可通过环境变量 "
                f"{TASK_TIMEOUT_ENV} 调整预算）：该任务可能仍在 QGIS 线程上继续执行，"
                "请刷新项目状态确认结果，或缩小工作区后重试"
            ) from exc

    def process_once(self, timeout=0.03):
        QgsApplication.processEvents()
        try:
            future, action = self.tasks.get(timeout=timeout)
        except Empty:
            return False
        if future.set_running_or_notify_cancel():
            try:
                future.set_result(action())
            except Exception as exc:
                future.set_exception(exc)
        return True
