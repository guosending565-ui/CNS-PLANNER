"""Marshal GIS work back to the Qt/QGIS owner thread."""

from concurrent.futures import Future
from queue import Empty, Queue

from qgis.core import QgsApplication


class QgisRuntime:
    def __init__(self, timeout_seconds=90):
        self.timeout_seconds = timeout_seconds
        self.tasks = Queue()

    def call(self, action):
        future = Future()
        self.tasks.put((future, action))
        return future.result(timeout=self.timeout_seconds)

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
