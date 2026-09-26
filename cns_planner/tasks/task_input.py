"""Phase4-B6R：heavy task 的 **immutable submitted input snapshot**。

B6X 的 ``input_snapshot_ref`` 只是一个字符串标签：worker 仍然回头读**当前**
``ProjectState`` 重新组装计算输入。只要提交之后有人改了项目（换算法、改参数、
改输入），worker 就会拿"变化后的新输入"去算，任务语义因此漂移。B6R 把这件事收口：

* **提交时**真正生成并持久化一份 immutable snapshot（提交那一刻的权威输入 +
  影响结果的算法选择清单），位置 ``<project>/.cns-tasks/inputs/<sha256>.json.gz``。
* **worker** 只从 ``input_snapshot_ref`` 指向的 snapshot 加载计算输入，
  **绝不**再按当前 ``ProjectState`` 组装计算输入。
* **publish** 时用当前 canonical state 重新生成同一份 snapshot，比较指纹：
  相同才发布，不同即 ``stale``（不覆盖 canonical result）。

存储与序列化复用 B5X 的 primitive（:func:`serialize_payload` /
:func:`deserialize_payload` / :func:`content_digest`），不另造通用 Artifact Store：

* content-addressed：``artifact_id`` = snapshot 确定性 JSON 的 SHA-256，相同逻辑
  输入必然命中同一个文件，必然复用；
* deterministic JSON/gzip：``ensure_ascii=False, sort_keys=True`` + ``gzip mtime=0``；
* 写入 = 临时文件 + fsync + 校验摘要 + ``os.replace`` 原子发布，绝不原地覆盖；
* 读取逐项校验 exists / 内容寻址名 / schema_version，缺失或损坏一律抛结构化异常，
  **绝不**静默返回空结果（那会让任务用错误输入"成功"算完）。

本模块只做"存 / 取 / 校验 / 指纹 / 算法清单解析"，不含任何业务算法。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from uuid import uuid4

from ..persistence.artifact_store import (
    ArtifactCorrupt, ArtifactPublishError, ArtifactUnavailable,
    CONTENT_ENCODING_JSON_GZ, content_digest, deserialize_payload, serialize_payload,
)
from ..persistence.project_repository import _path_lock
from .task_store import TASK_DIRECTORY


#: snapshot 信封 schema（与 artifact schema 分开：它是任务输入契约，不是结果契约）。
INPUT_SNAPSHOT_SCHEMA_VERSION = 1
#: task store 下的 immutable 输入区；与 ``.cns-tasks/tasks/`` 并列。
INPUT_SNAPSHOT_SUBDIRECTORY = "inputs"
#: snapshot 的逻辑文件名（写进信封，使"同一份输入"逐字节可比）。
INPUT_SNAPSHOT_FILENAME = "input_snapshot.json.gz"

_JSON_KWARGS = {
    "ensure_ascii": False, "sort_keys": True, "separators": (",", ":"),
    "allow_nan": False,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def snapshot_reference(relative_path) -> str:
    """task record 里的 ``input_snapshot_ref``：指向真实文件的相对路径。"""

    return Path(str(relative_path)).as_posix()


class InputSnapshotError(Exception):
    """snapshot 相关错误的基类（携带稳定 ``code`` 供上层翻译为业务语义）。"""

    code = "input_snapshot_error"

    def __init__(self, message, *, code=None, detail=None):
        super().__init__(message)
        if code:
            self.code = code
        self.detail = detail or {}


class InputSnapshotUnavailable(InputSnapshotError):
    """snapshot 不存在 / 引用缺失 / schema 不支持：属"读不到"。"""

    code = "input_snapshot_unavailable"


class InputSnapshotCorrupt(InputSnapshotError):
    """snapshot 存在但与记录不符（内容寻址名、编码、JSON 结构）。"""

    code = "input_snapshot_corrupt"


# ---- 内容寻址存储 ------------------------------------------------------------


class InputSnapshotStore:
    """``<project>/.cns-tasks/inputs/<sha256>.json.gz`` 的 immutable 内容寻址存储。"""

    def __init__(self, workdir):
        #: ``workdir`` 与 :class:`~cns_planner.tasks.task_store.TaskStore` 一样传
        #: **项目文件路径**（不是目录）。
        self.project_path = Path(workdir)
        self.root = self.project_path.parent
        self.directory = self.root / TASK_DIRECTORY / INPUT_SNAPSHOT_SUBDIRECTORY
        self.temporary_directory = self.root / TASK_DIRECTORY / "tmp"
        self._lock = _path_lock(self.directory)

    # ---- 引用解析 -----------------------------------------------------------

    def resolve(self, reference) -> Path:
        """把 ``input_snapshot_ref`` 解析成实际文件路径；越界 / 缺失一律抛异常。"""

        value = str(reference or "").strip()
        if not value:
            raise InputSnapshotUnavailable(
                "任务缺少输入快照引用", code="input_snapshot_ref_missing",
            )
        candidate = (self.root / value).resolve()
        try:
            candidate.relative_to(self.root.resolve())
        except ValueError as exc:
            raise InputSnapshotUnavailable(
                "输入快照路径超出项目目录", code="input_snapshot_outside_project",
            ) from exc
        if not candidate.is_file():
            raise InputSnapshotUnavailable(
                "输入快照文件缺失", code="input_snapshot_missing",
                detail={"input_snapshot_ref": value},
            )
        return candidate

    def relative_for_digest(self, digest) -> str:
        return (
            Path(TASK_DIRECTORY) / INPUT_SNAPSHOT_SUBDIRECTORY / f"{digest}.json.gz"
        ).as_posix()

    # ---- 写 ------------------------------------------------------------------

    def store(self, snapshot: dict) -> dict:
        """持久化 snapshot（幂等 content-addressed），返回引用描述。

        ``artifact_id`` = snapshot 确定性 JSON 的 SHA-256（内容身份，= 输入指纹）；
        ``sha256`` = 落盘文件字节的 SHA-256（文件完整性）。两者都进 task record。
        """

        content = serialize_payload(snapshot, schema_version=INPUT_SNAPSHOT_SCHEMA_VERSION)
        file_digest = content_digest(content)
        payload_digest = fingerprint_of(snapshot)
        relative = self.relative_for_digest(payload_digest)
        target = self.root / relative
        with self._lock:
            if target.is_file():
                self._verify_file(target, file_digest)
            else:
                self._write_atomic(target, content, file_digest)
        return {
            "artifact_id": payload_digest,
            "sha256": file_digest,
            "relative_path": relative,
            "content_encoding": CONTENT_ENCODING_JSON_GZ,
            "schema_version": INPUT_SNAPSHOT_SCHEMA_VERSION,
            "size_bytes": len(content),
        }

    def _write_atomic(self, target: Path, content: bytes, digest: str):
        target.parent.mkdir(parents=True, exist_ok=True)
        staged = self._stage(content)
        try:
            if content_digest(staged.read_bytes()) != digest:
                raise ArtifactPublishError(
                    "输入快照写入前摘要校验失败", code="input_snapshot_stage_hash_mismatch",
                )
            if target.is_file():
                self._verify_file(target, digest)
                return
            os.replace(staged, target)
        finally:
            staged.unlink(missing_ok=True)

    def _stage(self, content: bytes) -> Path:
        self.temporary_directory.mkdir(parents=True, exist_ok=True)
        staged = self.temporary_directory / f".{uuid4().hex[:16]}.snapshot.tmp"
        with staged.open("wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        return staged

    @staticmethod
    def _verify_file(path: Path, digest: str):
        actual = content_digest(path.read_bytes())
        if actual != str(digest):
            raise InputSnapshotCorrupt(
                "已存在的同 ID 输入快照内容不符", code="input_snapshot_sha256_mismatch",
                detail={"relative_path": path.name, "actual_sha256": actual},
            )

    # ---- 读 ------------------------------------------------------------------

    def load(self, reference) -> dict:
        """读取并**逐项校验** snapshot；缺失 / 损坏 / 版本不支持一律抛异常。"""

        path = self.resolve(reference)
        content = path.read_bytes()
        digest = content_digest(content)
        name = path.name
        if name.endswith(".json.gz"):
            expected = name[: -len(".json.gz")]
            if expected != digest:
                raise InputSnapshotCorrupt(
                    "输入快照文件与内容寻址名称不符",
                    code="input_snapshot_sha256_mismatch",
                    detail={"relative_path": str(reference), "expected_sha256": expected,
                            "actual_sha256": digest},
                )
        try:
            payload = deserialize_payload(
                content, expected_schema_version=INPUT_SNAPSHOT_SCHEMA_VERSION,
            )
        except ArtifactUnavailable as exc:
            raise InputSnapshotUnavailable(
                str(exc), code="input_snapshot_schema_unsupported", detail=exc.detail,
            ) from exc
        except ArtifactCorrupt as exc:
            raise InputSnapshotCorrupt(str(exc), code=exc.code, detail=exc.detail) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("inputs"), dict):
            raise InputSnapshotCorrupt(
                "输入快照结构无效", code="input_snapshot_structure_invalid",
            )
        return payload


# ---- 算法选择捕获 ------------------------------------------------------------


#: 真正影响 corridor 计算结果的算法类型；三者都必须进入 immutable snapshot。
CORRIDOR_ALGORITHM_TYPES = ("corridor_model", "coverage_model", "service_model")


def algorithm_manifests(registry, selection, algorithm_types):
    """把 ``algorithm_selection`` 收敛成自包含的算法清单（id / version / parameters）。

    只记录 selected 值（缺失时回落到工程默认 id/version），不静默注入 registry 里
    未选择的默认参数：快照必须逐字反映"提交那一刻的算法选择"，否则快照与同步路径
    的计算参数会出现看不见的差异。
    """

    manifests = {}
    for algorithm_type in algorithm_types:
        entry = _effective_selection(registry, selection, algorithm_type)
        manifests[str(algorithm_type)] = entry
    return manifests


def _effective_selection(registry, selection, algorithm_type):
    default = _default_selection(registry, algorithm_type)
    raw = selection.get(algorithm_type) if isinstance(selection, dict) else None
    entry = deepcopy(raw) if isinstance(raw, dict) else {}
    algorithm_id = str(entry.get("algorithm_id") or default["algorithm_id"])
    version = str(
        entry.get("version") or entry.get("algorithm_version") or default["version"]
    )
    parameters = entry.get("parameters") if isinstance(entry.get("parameters"), dict) else {}
    return {
        "algorithm_type": str(algorithm_type),
        "algorithm_id": algorithm_id,
        "version": version,
        "parameters": deepcopy(parameters),
    }


def _default_selection(registry, algorithm_type):
    manifests = registry.manifests(algorithm_type)
    if not manifests:
        raise InputSnapshotUnavailable(
            f"未注册算法类型：{algorithm_type}", code="input_snapshot_algorithm_missing",
        )
    manifest = manifests[0]
    return {"algorithm_id": str(manifest.algorithm_id), "version": str(manifest.version)}


def normalized_selection(state):
    """state 里的算法选择（缺失/坏值时回落到工程默认），绝不修改 state。"""

    from ..algorithms.registry import normalize_algorithm_selection

    value = state.get("algorithm_selection") if isinstance(state, dict) else None
    try:
        return normalize_algorithm_selection(value)
    except ValueError:
        return normalize_algorithm_selection(None)


class AlgorithmResolver:
    """只按 snapshot 里的算法清单重建实例（绝不读当前 ProjectState）。"""

    def __init__(self, manifests, registry):
        self.manifests = manifests if isinstance(manifests, dict) else {}
        self.registry = registry
        self._instances = {}

    def manifest(self, algorithm_type):
        key = str(algorithm_type)
        entry = self.manifests.get(key)
        if not isinstance(entry, dict) or not entry.get("algorithm_id"):
            raise InputSnapshotUnavailable(
                f"输入快照缺少算法清单：{key}", code="input_snapshot_algorithm_missing",
            )
        return entry

    def create(self, algorithm_type):
        key = str(algorithm_type)
        if key in self._instances:
            return self._instances[key]
        entry = self.manifest(key)
        parameters = entry.get("parameters") if isinstance(entry.get("parameters"), dict) else {}
        algorithm_id = str(entry["algorithm_id"])
        version = str(entry.get("version") or "")
        try:
            instance = self.registry.create(key, algorithm_id, version, deepcopy(parameters))
        except Exception:
            # 精确版本不可用时的唯一兜底：仍只用 snapshot 记录的 id + parameters，
            # 只是换用注册表里同 id 的版本；绝不回读当前算法选择。
            candidates = [
                item for item in self.registry.manifests(key)
                if str(item.algorithm_id) == algorithm_id
            ]
            if not candidates:
                raise
            instance = self.registry.create(
                key, algorithm_id, str(candidates[0].version), deepcopy(parameters),
            )
        self._instances[key] = instance
        return instance


# ---- snapshot 组装与指纹 ------------------------------------------------------


def build_snapshot(*, task_type, payload, state, registry, algorithm_types=(),
                   inputs):
    """提交 / publish 两侧共用的 snapshot 组装（只读 state，确定性）。"""

    selection = normalized_selection(state)
    # 故意不写任何时间戳：snapshot 必须**逐字节可复现**。提交时刻属于 task record
    # （``created_at``），不属于输入；否则 publish 阶段重新生成的"当前"snapshot
    # 永远不可能与提交快照相等，compare-and-publish 会退化成"必然 stale"。
    return {
        "schema_version": INPUT_SNAPSHOT_SCHEMA_VERSION,
        "filename": INPUT_SNAPSHOT_FILENAME,
        "task_type": str(task_type),
        "worker_payload": deepcopy(payload if isinstance(payload, dict) else {}),
        "algorithms": algorithm_manifests(registry, selection, algorithm_types),
        "inputs": inputs if isinstance(inputs, dict) else {},
    }


def fingerprint_of(snapshot) -> str:
    """snapshot 的输入指纹（content-addressed 身份）：确定性 JSON → SHA-256。"""

    return content_digest(
        serialize_payload(snapshot, schema_version=INPUT_SNAPSHOT_SCHEMA_VERSION)
    )


def snapshot_store_for(workdir) -> InputSnapshotStore:
    return InputSnapshotStore(workdir)


__all__ = [
    "AlgorithmResolver", "CORRIDOR_ALGORITHM_TYPES", "INPUT_SNAPSHOT_FILENAME",
    "INPUT_SNAPSHOT_SCHEMA_VERSION", "INPUT_SNAPSHOT_SUBDIRECTORY",
    "InputSnapshotCorrupt", "InputSnapshotError", "InputSnapshotStore",
    "InputSnapshotUnavailable", "algorithm_manifests", "build_snapshot",
    "fingerprint_of", "normalized_selection", "snapshot_reference",
    "snapshot_store_for", "utc_now",
]
