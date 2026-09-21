"""Low-level project-state JSON file operations."""

import json
import os
from pathlib import Path
import shutil
from tempfile import NamedTemporaryFile
import threading
import time


_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS = {}


def _path_lock(path):
    """Return the process-wide lock shared by every repository for ``path``."""

    key = str(Path(path).resolve())
    with _LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


class ProjectRepository:
    """Read, atomically write, and copy one project-state JSON file."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = _path_lock(self.path)

    @property
    def backup_path(self) -> Path:
        return self.path.with_name(self.path.name + ".bak")

    def exists(self) -> bool:
        return self.path.exists()

    def is_file(self) -> bool:
        return self.path.is_file()

    def load(self):
        with self._lock:
            return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, document) -> None:
        # Serialize before touching either the current file or its backup.  In
        # particular, allow_nan=False failures must leave both versions intact.
        serialized = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._unique_temporary(self.path)
            try:
                self._write_durable(temporary, serialized)
                if self.path.is_file():
                    self._replace_backup()
                self._atomic_replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)

    def copy_to(self, target: Path) -> None:
        target = Path(target)
        document = self.load()
        artifact = self._result_artifact_path(document, self.path)
        if artifact is not None:
            relative = artifact.relative_to(self.path.parent.resolve())
            artifact_target = target.parent / relative
            ProjectRepository(artifact_target).save_bytes(artifact.read_bytes())
        target_lock = _path_lock(target)
        # Stable lock ordering avoids deadlock when two files are copied in
        # opposite directions by different request threads.
        locks = sorted({id(self._lock): self._lock, id(target_lock): target_lock}.values(), key=id)
        with locks[0]:
            with locks[-1]:
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = self._unique_temporary(target)
                try:
                    shutil.copy2(self.path, temporary)
                    self._atomic_replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)

    def save_bytes(self, content: bytes) -> None:
        """Atomically store immutable sidecar bytes without creating a backup."""

        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._unique_temporary(self.path)
            try:
                with temporary.open("wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                self._atomic_replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)

    def _replace_backup(self):
        temporary = self._unique_temporary(self.backup_path)
        try:
            shutil.copy2(self.path, temporary)
            self._atomic_replace(temporary, self.backup_path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _result_artifact_path(document, project_path):
        index = document.get("result_index") if isinstance(document, dict) else None
        relative = index.get("artifact") if isinstance(index, dict) else None
        if not relative:
            return None
        root = Path(project_path).parent.resolve()
        candidate = (root / str(relative)).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError("项目结果索引超出项目目录") from exc
        if not candidate.is_file():
            raise ValueError(f"项目结果文件缺失：{relative}")
        return candidate

    @staticmethod
    def _unique_temporary(target):
        handle = NamedTemporaryFile(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent, delete=False
        )
        handle.close()
        return Path(handle.name)

    @staticmethod
    def _write_durable(path, content):
        with path.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())

    @staticmethod
    def _atomic_replace(source, target):
        # Windows virus scanners/indexers can briefly hold a just-closed file.
        # Retrying the same atomic operation does not expose a partial target.
        for attempt in range(5):
            try:
                os.replace(source, target)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.01 * (attempt + 1))
