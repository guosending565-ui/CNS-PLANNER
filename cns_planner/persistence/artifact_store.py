"""Phase4-B5X：统一 Artifact Contract（内容寻址、原子发布、完整性校验、只读盘点）。

本模块是 Phase4 里**唯一**的大型派生结果外置机制。B3X 已经建立了
``.cns-results/<sha256>.json.gz`` 这一内容寻址 sidecar，B5X 不再另造第二套系统，
而是在同一目录、同一命名规则上把它收敛成正式契约：

* **内容寻址**：``artifact_id`` 就是内容（UTF-8 deterministic JSON → gzip）的
  SHA-256；相同 logical content 必然得到相同 id，因而必然复用同一文件；
  不同内容必然得到不同 id，**绝不原地覆盖**已发布 artifact。
* **原子发布**：serialize → 临时文件（可回收区）→ flush/fsync → 校验摘要 →
  ``os.replace`` 原子发布。任何一步失败，active artifact 与 manifest 引用都不变，
  临时文件留在 ``.cns-results/tmp/`` 供后续回收（B5X 只盘点，不删除）。
* **跨项目可迁移**：metadata 只记录**相对项目目录**的路径，绝不记录
  ``C:\\...`` / ``D:\\...`` 这类绝对本机路径；实际路径由项目目录解析。
* **失败即失败**：读取时逐项校验 exists / schema_version / sha256 / content
  encoding。缺失、指纹不符、gzip 损坏、schema 不支持一律抛结构化异常，
  **绝不**静默返回空结果或把它当成 0。

本模块只负责"存 / 取 / 校验 / 盘点"，不包含任何业务计算，也不决定哪个字段该外置
（那是 :mod:`cns_planner.persistence.project_compaction` 的职责）。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import time
from uuid import uuid4
import zlib


ARTIFACT_SCHEMA_VERSION = 1
#: 沿用 B3X 既有的内容寻址目录，不引入第二套 artifact 系统。
ARTIFACT_DIRECTORY = ".cns-results"
#: 可回收区：发布失败/中断留下的临时文件，只盘点不删除。
ARTIFACT_TEMP_DIRECTORY = ".cns-results/tmp"
CONTENT_ENCODING_JSON_GZ = "json.gz"
_GZIP_LEVEL = 6
_JSON_KWARGS = {
    "ensure_ascii": False, "sort_keys": True, "separators": (",", ":"),
    "allow_nan": False,
}


class ArtifactError(Exception):
    """artifact 相关错误的基类：携带稳定 ``code`` 供上层翻译为业务语义。"""

    code = "artifact_error"

    def __init__(self, message, *, code=None, detail=None):
        super().__init__(message)
        if code:
            self.code = code
        self.detail = detail or {}

    def as_dict(self):
        return {"code": self.code, "message": str(self), "detail": deepcopy(self.detail)}


class ArtifactUnavailable(ArtifactError):
    """artifact 不存在 / 索引缺失 / 版本不支持：属"读不到"，不是业务结论。"""

    code = "artifact_unavailable"


class ArtifactCorrupt(ArtifactError):
    """artifact 存在但内容与 metadata 不符（指纹、编码、JSON 结构）。"""

    code = "artifact_corrupt"


class ArtifactPublishError(ArtifactError):
    """发布失败：active artifact 未被替换，临时文件留在可回收区。"""

    code = "artifact_publish_failed"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def serialize_payload(payload, *, schema_version=ARTIFACT_SCHEMA_VERSION):
    """deterministic serialize → gzip。相同 logical content ⇒ 相同字节。"""

    envelope = {
        "artifact_schema_version": int(schema_version),
        "payload": payload,
    }
    raw = json.dumps(envelope, **_JSON_KWARGS).encode("utf-8")
    return gzip.compress(raw, compresslevel=_GZIP_LEVEL, mtime=0)


def deserialize_payload(compressed, *, expected_schema_version=None):
    """gzip → JSON → 校验 artifact 信封 schema。损坏一律抛 ``ArtifactCorrupt``。

    B3X 写下的裸 payload（没有 ``artifact_schema_version`` 信封）仍然按**兼容读取**
    返回：那是既有用户的真实项目文件，不能因为 B5X 换了信封就打不开。
    """

    try:
        raw = gzip.decompress(compressed)
    except (OSError, EOFError, zlib.error) as exc:
        raise ArtifactCorrupt(
            "约束明细文件已损坏（gzip 无法解压）", code="artifact_gzip_corrupt",
        ) from exc
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactCorrupt(
            "约束明细文件已损坏（JSON 无法解析）", code="artifact_json_corrupt",
        ) from exc
    if not isinstance(document, dict):
        raise ArtifactCorrupt("约束明细文件结构无效", code="artifact_envelope_invalid")
    if "artifact_schema_version" not in document:
        # legacy（B3X）裸 payload：兼容读取，不做版本判断。
        return document
    if "payload" not in document:
        raise ArtifactCorrupt("约束明细文件结构无效", code="artifact_envelope_invalid")
    version = document.get("artifact_schema_version")
    if expected_schema_version is not None and version != expected_schema_version:
        raise ArtifactUnavailable(
            f"不支持的结果明细版本：{version}", code="artifact_schema_unsupported",
            detail={"artifact_schema_version": version},
        )
    return document.get("payload")


def content_digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def artifact_metadata(
    *, artifact_id, artifact_type, relative_path, size_bytes, sha256,
    schema_version=ARTIFACT_SCHEMA_VERSION, content_encoding=CONTENT_ENCODING_JSON_GZ,
    created_at=None, producer=None, input_fingerprint=None, scope=None,
    status="published", summary=None,
):
    """统一 artifact metadata：ProjectState 只保存它，不保存任何绝对路径。"""

    return {
        "artifact_id": str(artifact_id),
        "artifact_type": str(artifact_type),
        "schema_version": int(schema_version),
        "sha256": str(sha256),
        "content_encoding": str(content_encoding),
        "relative_path": Path(str(relative_path)).as_posix(),
        "size_bytes": int(size_bytes),
        "created_at": created_at or utc_now(),
        "producer": {
            "service": (producer or {}).get("service"),
            "algorithm_id": (producer or {}).get("algorithm_id"),
            "algorithm_version": (producer or {}).get("algorithm_version"),
        },
        "input_fingerprint": input_fingerprint,
        "scope": deepcopy(scope) if isinstance(scope, dict) else None,
        "status": str(status),
        "summary": deepcopy(summary) if isinstance(summary, dict) else {},
    }


def artifact_ref(metadata):
    """ProjectState 中保存的精简引用（与 metadata 同源，不含任何绝对路径）。

    摘要键（``cell_count`` / ``counts`` / 指纹等）会**平铺**到引用顶层：这样单个
    canonical result 的读取路径不需要回查 manifest 就能报告规模与指纹，同时完整
    metadata（含 ``summary`` 本体）仍然保留。
    """

    if not isinstance(metadata, dict) or not metadata.get("artifact_id"):
        return None
    reference = deepcopy(metadata)
    summary = reference.get("summary")
    if isinstance(summary, dict):
        for key, value in summary.items():
            reference.setdefault(key, deepcopy(value))
    return reference


def is_artifact_ref(value) -> bool:
    return (
        isinstance(value, dict)
        and bool(value.get("artifact_id"))
        and bool(value.get("relative_path"))
    )


def _path_lock(path):
    # 与 ProjectRepository 共用同一把路径锁，避免同一进程内发布/读取互相踩踏。
    from .project_repository import _path_lock as repository_path_lock

    return repository_path_lock(path)


class ArtifactStore:
    """一个项目目录下唯一的内容寻址 artifact 存储。"""

    def __init__(self, project_path):
        self.project_path = Path(project_path)
        self.root = self.project_path.parent
        self.directory = self.root / ARTIFACT_DIRECTORY
        self.temporary_directory = self.root / ARTIFACT_TEMP_DIRECTORY
        self._lock = _path_lock(self.directory)

    # ---- 发布 ---------------------------------------------------------------

    def publish(
        self, payload, *, artifact_type, schema_version=ARTIFACT_SCHEMA_VERSION,
        producer=None, input_fingerprint=None, scope=None, status="published",
        summary=None, created_at=None, previous_entries=None,
    ):
        """内容寻址发布：相同内容复用，不同内容新建；绝不覆盖 active artifact。

        ``previous_entries`` 是上一次 manifest 的 ``entries``：命中同一 artifact_id 时
        继承其 ``created_at``，使连续两次 compaction 得到**幂等**的持久化文档。
        """

        content = serialize_payload(payload, schema_version=schema_version)
        digest = content_digest(content)
        relative = (Path(ARTIFACT_DIRECTORY) / f"{digest}.json.gz").as_posix()
        target = self.root / relative
        with self._lock:
            if target.is_file():
                self._verify_existing(target, digest)
            else:
                self._publish_bytes(target, content, digest)
            # ``created_at`` 由 artifact 文件自身决定，而不是"这次调用的时间"：
            # 相同内容 ⇒ 相同 metadata ⇒ compaction 对同一项目连续执行时幂等。
            created = self._file_created_at(target) or created_at
        return artifact_metadata(
            artifact_id=digest,
            artifact_type=artifact_type,
            relative_path=relative,
            size_bytes=len(content),
            sha256=digest,
            schema_version=schema_version,
            created_at=created,
            producer=producer,
            input_fingerprint=input_fingerprint,
            scope=scope,
            status=status,
            summary=summary,
        )

    @staticmethod
    def _file_created_at(path):
        try:
            return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
        except OSError:  # pragma: no cover - 文件系统竞态
            return None

    def publish_temporary(
        self, payload, *, artifact_type, schema_version=ARTIFACT_SCHEMA_VERSION,
        producer=None, input_fingerprint=None, scope=None, status="staged",
        summary=None,
    ):
        """B6 接口边界：先 staged 到可回收区，再由调用方决定是否 publish。

        本轮不实现 worker/task queue，只保留这条 temp → publish 的 primitive：
        返回的 metadata ``status`` 为 ``staged``，``relative_path`` 指向临时文件，
        调用方确认后再走 :meth:`commit_staged` 原子发布。
        """

        content = serialize_payload(payload, schema_version=schema_version)
        digest = content_digest(content)
        staged_name = f"{digest}.{uuid4().hex[:12]}.staged"
        staged_relative = (Path(ARTIFACT_TEMP_DIRECTORY) / staged_name).as_posix()
        staged = self.root / staged_relative
        with self._lock:
            self.temporary_directory.mkdir(parents=True, exist_ok=True)
            with staged.open("wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        return artifact_metadata(
            artifact_id=digest,
            artifact_type=artifact_type,
            relative_path=staged_relative,
            size_bytes=len(content),
            sha256=digest,
            schema_version=schema_version,
            producer=producer,
            input_fingerprint=input_fingerprint,
            scope=scope,
            status=status,
            summary=summary,
        )

    def commit_staged(self, metadata, *, summary=None, status="published"):
        """把 staged 临时文件原子发布成 canonical artifact（幂等）。"""

        if not isinstance(metadata, dict):
            raise ArtifactPublishError("staged artifact metadata 无效")
        digest = str(metadata.get("sha256") or metadata.get("artifact_id") or "")
        staged = self.resolve(str(metadata.get("relative_path") or ""))
        relative = (Path(ARTIFACT_DIRECTORY) / f"{digest}.json.gz").as_posix()
        target = self.root / relative
        with self._lock:
            if target.is_file():
                self._verify_existing(target, digest)
                staged.unlink(missing_ok=True)
            else:
                self._publish_bytes(target, staged.read_bytes(), digest)
                staged.unlink(missing_ok=True)
        committed = deepcopy(metadata)
        committed.update({
            "artifact_id": digest, "sha256": digest, "relative_path": relative,
            "status": status,
        })
        if summary is not None:
            committed["summary"] = deepcopy(summary)
        return committed

    # ---- 读取 ---------------------------------------------------------------

    def resolve(self, relative_path) -> Path:
        """把相对项目目录的路径解析成实际路径；越界/缺失一律抛异常。"""

        value = str(relative_path or "").strip()
        if not value:
            raise ArtifactUnavailable("结果明细缺少相对路径", code="artifact_path_missing")
        candidate = (self.root / value).resolve()
        try:
            candidate.relative_to(self.root.resolve())
        except ValueError as exc:
            raise ArtifactUnavailable(
                "结果明细路径超出项目目录", code="artifact_path_outside_project",
            ) from exc
        if not candidate.is_file():
            raise ArtifactUnavailable(
                "结果明细文件缺失", code="artifact_missing",
                detail={"relative_path": value},
            )
        return candidate

    def exists(self, relative_path) -> bool:
        try:
            self.resolve(relative_path)
            return True
        except ArtifactError:
            return False

    def stat(self, relative_path):
        path = self.resolve(relative_path)
        info = path.stat()
        return {"relative_path": str(relative_path), "size_bytes": info.st_size}

    def read(self, reference, *, expected_schema_version=None):
        """读取并**逐项校验** artifact；失败抛结构化异常，绝不返回空结果。"""

        if not isinstance(reference, dict):
            raise ArtifactUnavailable("缺少结果明细引用", code="artifact_ref_missing")
        relative = reference.get("relative_path")
        path = self.resolve(relative)
        content = path.read_bytes()
        expected = reference.get("sha256") or reference.get("artifact_id")
        if expected:
            actual = content_digest(content)
            if actual != str(expected):
                raise ArtifactCorrupt(
                    "结果明细指纹不匹配", code="artifact_sha256_mismatch",
                    detail={
                        "relative_path": str(relative),
                        "expected_sha256": str(expected),
                        "actual_sha256": actual,
                    },
                )
        encoding = str(reference.get("content_encoding") or CONTENT_ENCODING_JSON_GZ)
        if encoding != CONTENT_ENCODING_JSON_GZ:
            raise ArtifactUnavailable(
                f"不支持的结果明细编码：{encoding}", code="artifact_encoding_unsupported",
                detail={"content_encoding": encoding},
            )
        version = reference.get("schema_version")
        if expected_schema_version is None and isinstance(version, int):
            expected_schema_version = version
        return deserialize_payload(content, expected_schema_version=expected_schema_version)

    def read_summary(self, reference):
        """只读 metadata 摘要（不解压内容）。"""

        if not isinstance(reference, dict):
            raise ArtifactUnavailable("缺少结果明细引用", code="artifact_ref_missing")
        summary = deepcopy(reference.get("summary") or {})
        summary.update({
            "artifact_id": reference.get("artifact_id"),
            "artifact_type": reference.get("artifact_type"),
            "schema_version": reference.get("schema_version"),
            "sha256": reference.get("sha256"),
            "content_encoding": reference.get("content_encoding"),
            "relative_path": reference.get("relative_path"),
            "size_bytes": reference.get("size_bytes"),
            "created_at": reference.get("created_at"),
            "producer": deepcopy(reference.get("producer")),
            "input_fingerprint": reference.get("input_fingerprint"),
            "scope": deepcopy(reference.get("scope")),
            "status": reference.get("status"),
        })
        return summary

    # ---- 内部 ---------------------------------------------------------------

    def _publish_bytes(self, target, content, digest):
        target.parent.mkdir(parents=True, exist_ok=True)
        staged = self._stage(content)
        try:
            if content_digest(staged.read_bytes()) != digest:
                raise ArtifactPublishError(
                    "结果明细发布前指纹校验失败", code="artifact_stage_hash_mismatch",
                )
            if target.is_file():
                self._verify_existing(target, digest)
                staged.unlink(missing_ok=True)
                return
            self._atomic_replace(staged, target)
        except ArtifactPublishError:
            # 失败：active artifact 不变，临时文件留在可回收区，不静默删除。
            raise
        except OSError as exc:
            raise ArtifactPublishError(
                f"结果明细发布失败：{exc}", code="artifact_publish_failed",
                detail={"relative_path": target.name, "reason": type(exc).__name__},
            ) from exc

    def _stage(self, content: bytes) -> Path:
        self.temporary_directory.mkdir(parents=True, exist_ok=True)
        staged = self.temporary_directory / f".{uuid4().hex[:16]}.tmp"
        with staged.open("wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        return staged

    @staticmethod
    def _atomic_replace(source, target):
        for attempt in range(5):
            try:
                os.replace(source, target)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.01 * (attempt + 1))

    def _verify_existing(self, target, digest):
        actual = content_digest(target.read_bytes())
        if actual != digest:
            raise ArtifactCorrupt(
                "已存在的同 ID 结果明细内容不符", code="artifact_sha256_mismatch",
                detail={"relative_path": target.name, "actual_sha256": actual},
            )

    # ---- 只读盘点（dry-run GC，绝不删除） ------------------------------------

    def inventory(self, document, *, extra_references=()):
        """dry-run GC 清单：分类统计 reachable / stale / unreachable / temp。

        本方法**只读**：不做任何删除、不改动任何文件。物理 GC 后置到后续批次。
        """

        references = collect_artifact_references(document)
        for item in extra_references or ():
            references.append(dict(item))
        permanent_ids, stale_ids = set(), set()
        reachable_ids = set()
        by_id = {}
        for item in references:
            ref = item["ref"]
            artifact_id = str(ref.get("artifact_id") or ref.get("sha256") or "")
            if not artifact_id:
                continue
            by_id.setdefault(artifact_id, []).append(item["path"])
            if item.get("permanent"):
                permanent_ids.add(artifact_id)
            elif item.get("stale"):
                stale_ids.add(artifact_id)
            else:
                reachable_ids.add(artifact_id)

        buckets = {"permanent": [], "reachable": [], "stale_referenced": [],
                   "unreachable": [], "temp": []}
        if self.directory.is_dir():
            for path in sorted(self.directory.glob("*.json.gz")):
                digest = path.name[: -len(".json.gz")]
                entry = {
                    "artifact_id": digest,
                    "relative_path": path.relative_to(self.root).as_posix(),
                    "size_bytes": path.stat().st_size,
                    "referenced_by": by_id.get(digest, []),
                }
                if digest in permanent_ids:
                    buckets["permanent"].append(entry)
                elif digest in reachable_ids:
                    buckets["reachable"].append(entry)
                elif digest in stale_ids:
                    buckets["stale_referenced"].append(entry)
                else:
                    buckets["unreachable"].append(entry)
        if self.temporary_directory.is_dir():
            for path in sorted(self.temporary_directory.glob("*")):
                if not path.is_file():
                    continue
                buckets["temp"].append({
                    "artifact_id": None,
                    "relative_path": path.relative_to(self.root).as_posix(),
                    "size_bytes": path.stat().st_size,
                    "referenced_by": [],
                })

        def bucket(entries):
            return {
                "count": len(entries),
                "bytes": sum(int(item["size_bytes"] or 0) for item in entries),
                "artifacts": entries,
            }

        categories = {name: bucket(entries) for name, entries in buckets.items()}
        return {
            "schema_version": 1,
            "dry_run": True,
            "deletes": [],
            "note": "B5X 只输出盘点清单，不物理删除任何 artifact。",
            "categories": categories,
            "totals": {
                "artifact_count": sum(
                    categories[name]["count"]
                    for name in ("permanent", "reachable", "stale_referenced", "unreachable")
                ),
                "artifact_bytes": sum(
                    categories[name]["bytes"]
                    for name in ("permanent", "reachable", "stale_referenced", "unreachable")
                ),
                "temp_count": categories["temp"]["count"],
                "temp_bytes": categories["temp"]["bytes"],
            },
        }


#: 命中这些顶层 key 的引用属于"已确认 / 已发布 / 报告引用"，永久保留。
PERMANENT_REFERENCE_PREFIXES = (
    "confirmed_cns_plan", "cns_planning_reports", "layered_operational_adoptions",
    "v3_operational_adoptions", "cns_plan_review", "required_cns_adoption",
)
_STALE_STATUSES = ("stale", "superseded", "invalidated")


def collect_artifact_references(document):
    """递归收集 document 内的 artifact 引用及其语义（reachable / stale / permanent）。"""

    found = []

    def walk(value, path, *, permanent=False, stale=False, status=None):
        if isinstance(value, dict):
            if is_artifact_ref(value):
                found.append({
                    "ref": value,
                    "path": ".".join(path) or "$",
                    "permanent": permanent,
                    "stale": stale or str(
                        value.get("status") or status or ""
                    ) in _STALE_STATUSES,
                })
                return
            own_status = value.get("status")
            next_stale = stale or str(own_status or "") in _STALE_STATUSES
            for key, item in value.items():
                walk(
                    item, [*path, str(key)],
                    permanent=permanent or bool(path and path[0] in PERMANENT_REFERENCE_PREFIXES),
                    stale=next_stale, status=own_status,
                )
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, [*path, str(index)], permanent=permanent, stale=stale, status=status)

    walk(document, [])
    return found


__all__ = [
    "ARTIFACT_DIRECTORY", "ARTIFACT_SCHEMA_VERSION", "ARTIFACT_TEMP_DIRECTORY",
    "ArtifactCorrupt", "ArtifactError", "ArtifactPublishError", "ArtifactStore",
    "ArtifactUnavailable", "CONTENT_ENCODING_JSON_GZ", "PERMANENT_REFERENCE_PREFIXES",
    "artifact_metadata", "artifact_ref", "collect_artifact_references",
    "content_digest", "deserialize_payload", "is_artifact_ref", "serialize_payload",
    "utc_now",
]
