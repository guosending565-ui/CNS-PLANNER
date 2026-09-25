"""Shared project-state session used by application services."""

import json
from json import JSONDecodeError
from pathlib import Path
from copy import deepcopy
import threading

from ..persistence.project_repository import ProjectRepository
from ..persistence.project_compaction import (
    compact_and_store, restore_compacted_results, sync_artifact_refs_to_state,
)
from ..domain.altitude_layer_defaults import ensure_default_altitude_layers
from .project_state import blank_project, normalize_project, utc_now
from .route_operating_layer_service import (
    refresh_spatial_status, resync_operating_layer_statuses,
)


class WorkflowSession:
    def __init__(self, store_path: Path, defaults_path: Path, grid_service):
        self.store_path = Path(store_path)
        self.defaults_path = Path(defaults_path)
        self.repository = ProjectRepository(self.store_path)
        self.defaults = json.loads(self.defaults_path.read_text(encoding="utf-8"))
        self.grid_service = grid_service
        self.lock = threading.RLock()
        #: Deferred commit support: one user action that legitimately consists of several
        #: internal steps (e.g. re-map the workspace and then recompute its grid attributes)
        #: must reach the disk — and therefore the revision counter — as **one** commit.
        #: While ``defer_save`` is set, ``save()`` only records that a commit is pending.
        self.defer_save = False
        self.pending_save = False
        self.state = self._load()
        self._workspace_signature = self._signature(self.state.get("workspace"))

    def _load(self):
        if not self.repository.exists():
            return blank_project(self.defaults)
        try:
            document = restore_compacted_results(self.repository.load(), self.store_path)
        except JSONDecodeError as exc:
            raise ValueError("项目 JSON 损坏，未打开") from exc
        state = normalize_project(document, self.grid_service)
        self._strip_persistent_compatibility_results(state)
        self._restore_altitude_layer_catalog(state)
        return state

    @staticmethod
    def _strip_persistent_compatibility_results(state):
        """Keep compatibility metadata, but never restore result payloads as ProjectState."""

        compatibility = state.get("compatibility")
        if isinstance(compatibility, dict):
            compatibility.pop("results", None)

    def _restore_altitude_layer_catalog(self, state):
        """项目恢复：旧项目没有巡航高度层目录时补建工程默认高度层。

        只补 ``spatial_3d.altitude_layers`` 的 catalog 条目（ALT-060/080/100/150/200），不选择高度层、
        不触碰任何规划输入。**恢复本身保持只读**：这里不写盘，``pending_save`` 只记录“这次
        加载产生了需要落盘的差异”，由下一次真实提交一并写入；因此打开项目不会因为恢复而
        修改项目文件（既有持久化契约不变），而任何后续写操作都会把目录固化下来。
        """

        if not ensure_default_altitude_layers(state):
            return
        resync_operating_layer_statuses(state)
        refresh_spatial_status(state)
        self.pending_save = True

    def save(self):
        with self.lock:
            if self.defer_save:
                self.pending_save = True
                return
            self._commit()

    def commit_deferred(self):
        """Commit once, if any deferred ``save()`` happened.  Returns whether it committed."""

        with self.lock:
            if not self.pending_save:
                return False
            self.pending_save = False
            self._commit()
            return True

    def _commit(self):
        with self.lock:
            previous = {
                "revision": self.state.get("revision", 0),
                "project_revision": self.state["project"].get("revision", 0),
                "project_updated_at": self.state["project"].get("updated_at"),
                "last_saved_at": self.state.get("last_saved_at"),
                "workspace_revision": (
                    self.state.get("workspace", {}).get("revision")
                    if isinstance(self.state.get("workspace"), dict) else None
                ),
            }
            workspace = self.state.get("workspace")
            workspace_signature = self._signature(workspace)
            self.state["revision"] = int(previous["revision"] or 0) + 1
            self.state["project"]["revision"] = int(previous["project_revision"] or 0) + 1
            if isinstance(workspace, dict) and workspace_signature != self._workspace_signature:
                workspace["revision"] = int(previous["workspace_revision"] or 0) + 1
            now = utc_now()
            self.state["project"]["updated_at"] = now
            self.state["last_saved_at"] = now
            try:
                # Defense in depth for callers/old branches that still inject result payloads
                # under compatibility: metadata may persist, result bodies may not.
                self._strip_persistent_compatibility_results(self.state)
                document = compact_and_store(self.state, self.store_path)
                self.repository.save(document)
            except Exception:
                self.state["revision"] = previous["revision"]
                self.state["project"]["revision"] = previous["project_revision"]
                self.state["project"]["updated_at"] = previous["project_updated_at"]
                self.state["last_saved_at"] = previous["last_saved_at"]
                if isinstance(workspace, dict):
                    if previous["workspace_revision"] is None:
                        workspace.pop("revision", None)
                    else:
                        workspace["revision"] = previous["workspace_revision"]
                raise
            sync_artifact_refs_to_state(self.state, document)
            self._workspace_signature = self._signature(self.state.get("workspace"))

    @staticmethod
    def _signature(value):
        if not isinstance(value, dict):
            return None
        comparable = {key: item for key, item in value.items() if key != "revision"}
        return json.dumps(comparable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
