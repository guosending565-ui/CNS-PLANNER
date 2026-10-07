"""Low-level project-state JSON file operations."""

import json
import os
from pathlib import Path
import shutil
from tempfile import NamedTemporaryFile
import threading

from .atomic_io import (
    atomic_replace, is_transient_replace_error, transient_reason,
)


_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS = {}


#: 正式替换被其它进程的句柄挡住时的用户可见消息（Round32-I）。
#: 要求：普通用户看得懂"被占用、原文件没坏、怎么办"，同时把 Windows
#: ``winerror`` 留在消息里供支持/日志使用；原始异常仍由 ``__cause__`` 保留。
#: 备份语义必须准确：既有顺序是 temp → 备份轮换 → 替换正式文件，因此替换最终失败时
#: 备份**可能已经**按轮换顺序更新为"本次保存前的正式状态"，不能说成"备份保持不变"。
_SAVE_BUSY_MESSAGE = (
    "项目保存失败：项目状态文件或其备份正被其它程序占用，无法安全完成原子替换"
    "（{reason}）。原有 project_state.json 未被破坏，也没有写入不完整数据。"
    "备份文件保持完整可解析；按既有备份轮换顺序，它可能已更新为本次保存前的"
    "正式项目状态。请稍后重试；若持续失败，请先关闭正在读取该项目文件的程序"
    "（杀毒扫描 / 文件索引 / 云同步 / 另一个 CNS 或 QGIS 进程）再保存。"
)


class ProjectSaveError(OSError):
    """``os.replace`` 落盘失败（面向用户的中文消息 + 保留 Windows errno/winerror）。

    继承 ``OSError``：既有的 ``except OSError`` 调用方语义不变；``detail`` 携带
    原始 ``errno`` / ``winerror`` / 异常类型，``__cause__`` 保留原始异常对象。

    **只用于原子替换本身**的瞬时占用：temp 写入、``shutil.copy2`` 读取、
    ``NamedTemporaryFile`` 创建等其它阶段的 ``OSError`` 必须原样抛出，不得被
    伪装成"replace 被占用"。
    """

    code = "project_state_save_failed"

    def __init__(self, message, *, cause=None):
        super().__init__(message)
        self.detail = {
            "reason": transient_reason(cause) if cause is not None else None,
            "errno": getattr(cause, "errno", None),
            "winerror": getattr(cause, "winerror", None),
            "exception": type(cause).__name__ if cause is not None else None,
        }


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
                # 只有下面两次**原子替换**才可能把"被别的程序占用"翻译成用户可见消息；
                # temp 写入 / 备份复制 / 临时文件创建等阶段的 OSError 原样抛出。
                self._write_durable(temporary, serialized)
                if self.path.is_file():
                    self._replace_backup()
                self._replace_for_project_save(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)

    def copy_to(self, target: Path) -> None:
        target = Path(target)
        document = self.load()
        for relative in self._result_artifact_paths(document):
            source = (self.path.parent.resolve() / relative).resolve()
            try:
                source.relative_to(self.path.parent.resolve())
            except ValueError as exc:
                raise ValueError("项目结果索引超出项目目录") from exc
            if not source.is_file():
                raise ValueError(f"项目结果文件缺失：{relative}")
            ProjectRepository(target.parent / relative).save_bytes(source.read_bytes())
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

    def _replace_for_project_save(self, source, target):
        """正式状态落盘链上的原子替换入口（唯一翻译"被占用"的地方）。

        只有真正发生在 ``os.replace`` 上的瞬时共享/权限失败才等于"项目状态文件或其
        备份被别的程序占用"。``_write_durable``、``shutil.copy2``、
        ``NamedTemporaryFile`` 等阶段的 ``OSError`` 不经过这里，因此不会被伪装成
        replace contention。
        """

        try:
            self._atomic_replace(source, target)
        except OSError as exc:
            if is_transient_replace_error(exc):
                raise ProjectSaveError(
                    _SAVE_BUSY_MESSAGE.format(reason=transient_reason(exc)),
                    cause=exc,
                ) from exc
            raise

    def _replace_backup(self):
        temporary = self._unique_temporary(self.backup_path)
        try:
            shutil.copy2(self.path, temporary)
            self._replace_for_project_save(temporary, self.backup_path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _result_artifact_paths(document):
        """项目文档引用的全部 artifact 相对路径（新 manifest + legacy result_index）。"""

        if not isinstance(document, dict):
            return []
        relatives = []
        manifest = document.get("artifact_manifest")
        entries = manifest.get("entries") if isinstance(manifest, dict) else None
        if isinstance(entries, dict):
            for entry in entries.values():
                relative = entry.get("relative_path") if isinstance(entry, dict) else None
                if relative and str(relative) not in relatives:
                    relatives.append(str(relative))
        index = document.get("result_index")
        legacy = index.get("artifact") if isinstance(index, dict) else None
        if legacy and str(legacy) not in relatives:
            relatives.append(str(legacy))
        return relatives

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
        # Round32-I：统一走 ``atomic_io.atomic_replace`` —— 只对瞬时共享/权限失败
        # （WinError 5 / 32 / 33、EACCES / EPERM / EBUSY）做**有界**短退避重试
        # （5 次尝试，退避 0.1/0.2/0.4/0.8 s），其余 OSError 立即抛出。
        # 每次尝试都是整文件原子替换，因此重试不会暴露半截目标文件。
        atomic_replace(source, target)
