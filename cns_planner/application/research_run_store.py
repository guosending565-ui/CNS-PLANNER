"""R2.2-C: append-only research run ledger, cumulative budgets, atomic
checkpoints and identity-checked recovery.

This store is deliberately not a ProjectState writer: it only writes beneath an
explicitly approved output root, never into the source tree, ``projects/``,
``artifacts/``, acceptance ``stop`` files or a G3.6 recovery package.  The
append-only hash chain detects accidental edits inside one file; it does not
claim resistance to a deliberate rewrite of the whole ledger or of an old
copy of the store, and there is no external trusted high-water anchor yet.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
import re
import shutil
import stat
import uuid
from pathlib import Path

from ..domain.joint_feasibility import digest, json_value

DEFAULT_OUTPUT_LIMIT_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_LIMIT_BYTES = DEFAULT_OUTPUT_LIMIT_BYTES
BUDGET_DIMENSIONS = ("time_ms", "nodes", "replays", "memory_bytes", "output_bytes")
IDENTITY_FIELDS = ("source_bundle_fingerprint", "scenario_fingerprint", "domain_fingerprint",
                   "action_set_fingerprint", "objective_fingerprint", "model_versions",
                   "scope_fingerprint")
LEDGER_SCHEMA = "research_run_ledger.v1"
CHECKPOINT_SCHEMA = "research_run_checkpoint.v1"
RESULT_SCHEMA = "research_run_result.v1"
LEDGER_GENESIS = "0" * 64
MAX_CHECKPOINT_BYTES = 4 * 1024 * 1024
SOURCE_ROOT = Path(__file__).resolve().parents[2]
PROTECTED_COMPONENTS = {".git", ".acceptance_setup", ".cns-results", "artifacts", "projects"}


class StoreError(ValueError):
    """Rejected store operation; never a silent fallback."""


class RecoveryRejected(StoreError):
    """Recovery identity, budget or checkpoint freshness check failed."""


class BudgetExhausted(StoreError):
    def __init__(self, dimension, high_water=None):
        super().__init__(f"budget exhausted: {dimension}")
        self.dimension = dimension
        self.high_water = high_water


def _now():
    return datetime.now(timezone.utc).isoformat()


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _sha(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _plain_path(value):
    if type(value) is not str or not value or "\x00" in value:
        raise StoreError("explicit path required")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or value.startswith(("\\\\", "//")):
        raise StoreError("absolute local path without traversal required")
    if any(":" in part for part in path.parts[1:]):
        raise StoreError("alternate stream forbidden")
    for ancestor in reversed([path, *path.parents]):
        try:
            info = ancestor.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise StoreError("symlink/junction/reparse path forbidden")
    return path


def _check_protected(path, protected_roots):
    resolved = path.resolve(strict=False)
    source = SOURCE_ROOT.resolve(strict=True)
    if resolved == source or resolved.is_relative_to(source) or source.is_relative_to(resolved):
        raise StoreError("output overlaps protected source/state root")
    for index, part in enumerate(path.parts):
        folded = part.casefold()
        if index == 1 and folded in ("projects", "artifacts"):
            # Repository lives under D:\Projects; only the drive-top component is
            # tolerated, any deeper projects/artifacts entry stays protected.
            continue
        if folded in PROTECTED_COMPONENTS or folded.startswith("local_recovery_"):
            raise StoreError("protected output path component")
    for root in protected_roots or ():
        denied = _plain_path(str(root)).resolve(strict=False)
        if resolved == denied or resolved.is_relative_to(denied) or denied.is_relative_to(resolved):
            raise StoreError("output overlaps an explicitly protected root")
    return resolved


def approve_output_root(path, *, allowed_roots, protected_roots=()):
    """Trusted-code approval of one absolute output root; does not create it."""
    candidate = _plain_path(str(path))
    if type(allowed_roots) not in (list, tuple) or not allowed_roots:
        raise StoreError("explicit allowed roots required")
    roots = []
    for value in allowed_roots:
        root = _plain_path(str(value))
        if not root.is_dir():
            raise StoreError("allowed root must be an existing directory")
        roots.append(root.resolve(strict=True))
    resolved = candidate.resolve(strict=False)
    if not any(resolved == root or resolved.is_relative_to(root) for root in roots):
        raise StoreError("output root outside allowed roots")
    _check_protected(candidate, protected_roots)
    if candidate.exists() and not candidate.is_dir():
        raise StoreError("output root must be a directory")
    return {"resolved_path": str(resolved), "approved": True,
            "write_scope": "research_run_store", "execution_enabled": False}


def _assert_safe_write_path(path):
    candidate = _plain_path(str(path))
    _check_protected(candidate, ())
    return str(candidate)


def checked_identity(identity):
    value = json_value(identity)
    if type(value) is not dict:
        raise StoreError("identity mapping required")
    if set(value) != set(IDENTITY_FIELDS):
        raise StoreError("identity fields mismatch")
    for field in IDENTITY_FIELDS:
        if field == "model_versions":
            versions = value[field]
            if type(versions) is not dict or not versions:
                raise StoreError("model version mapping required")
            for key, version in versions.items():
                if type(key) is not str or type(version) is not str or not version:
                    raise StoreError("model version strings required")
        elif not _sha(value[field]):
            raise StoreError(f"invalid identity fingerprint: {field}")
    return value


def checked_budget(budget):
    value = json_value(budget)
    if type(value) is not dict or set(value) != set(BUDGET_DIMENSIONS):
        raise StoreError("budget dimensions mismatch")
    for dimension in BUDGET_DIMENSIONS:
        amount = value[dimension]
        if type(amount) is not int or amount < 0:
            raise StoreError(f"invalid budget dimension: {dimension}")
    if value["time_ms"] <= 0 or value["nodes"] <= 0 or value["memory_bytes"] <= 0:
        raise StoreError("time/nodes/memory budgets must be positive")
    if value["output_bytes"] > MAX_OUTPUT_LIMIT_BYTES:
        raise StoreError("proposal output budget exceeds the 64MiB policy limit")
    return value


def _disk_free(directory):
    probe = Path(directory)
    while not probe.exists():
        if probe.parent == probe:
            raise StoreError("no existing ancestor for disk check")
        probe = probe.parent
    return shutil.disk_usage(str(probe)).free


def _ensure_disk(directory, needed):
    if _disk_free(directory) < needed + 4096:
        raise StoreError("insufficient disk space")


def _replace(source, target):
    os.replace(source, target)


def _atomic_write(path, data):
    path = Path(path)
    _assert_safe_write_path(str(path))
    _ensure_disk(path.parent, len(data))
    temporary = path.parent / f".tmp-{uuid.uuid4().hex[:8]}"
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _replace(temporary, path)
    except OSError as exc:
        raise StoreError(f"atomic write failed: {exc}") from exc
    finally:
        if temporary.exists():
            temporary.unlink()


def _append_bytes(path, data):
    try:
        with Path(path).open("ab") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise StoreError(f"ledger append failed: {exc}") from exc


class ResearchRunStore:
    """One controlled research run: ledger, budgets, checkpoints and recovery."""

    def __init__(self, *, output_root, budget, identity, allowed_roots,
                 max_output_bytes=None, max_calibrations=0, run_id=None,
                 protected_roots=()):
        approval = approve_output_root(output_root, allowed_roots=allowed_roots,
                                       protected_roots=protected_roots)
        self.output_root = approval["resolved_path"]
        self.budget = checked_budget(budget)
        self.identity = checked_identity(identity)
        limit = self.budget["output_bytes"] if max_output_bytes is None else max_output_bytes
        if (type(limit) is not int or not 0 < limit <= self.budget["output_bytes"] or
                limit > MAX_OUTPUT_LIMIT_BYTES):
            raise StoreError("run output budget must stay within the permit and 64MiB")
        self.max_output_bytes = limit
        if type(max_calibrations) is not int or max_calibrations < 0:
            raise StoreError("calibration limit must be a non-negative integer")
        self.max_calibrations = max_calibrations
        if run_id is None:
            run_id = uuid.uuid4().hex
        if type(run_id) is not str or re.fullmatch(r"[a-z0-9_]{1,64}", run_id) is None:
            raise StoreError("invalid run id")
        self.run_id = run_id
        self.run_dir = Path(self.output_root) / f"run_{run_id}"
        self._consumed = {dimension: 0 for dimension in BUDGET_DIMENSIONS}
        self._calibrations = 0
        self._seq = 0
        self._head = LEDGER_GENESIS
        self._begun = False
        if self.run_dir.exists():
            if not (self.run_dir / "ledger.jsonl").is_file():
                raise StoreError("run directory exists without a ledger")
            self._load_ledger()

    @property
    def worker_root(self):
        return self.run_dir / "worker"

    @property
    def checkpoints_root(self):
        return self.run_dir / "checkpoints"

    def _load_ledger(self):
        self._consumed = {dimension: 0 for dimension in BUDGET_DIMENSIONS}
        self._calibrations = 0
        record_path = self.run_dir / "run.json"
        if not record_path.is_file():
            raise StoreError("run record missing")
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
            json_value(record)
        except (OSError, ValueError) as exc:
            raise StoreError(f"run record invalid: {exc}") from exc
        if record.get("run_id") != self.run_id:
            raise StoreError("run record id mismatch")
        if record.get("identity_fingerprint") != digest(self.identity):
            raise StoreError("run identity mismatch")
        rows = []
        try:
            for line in (self.run_dir / "ledger.jsonl").read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rows.append(json.loads(line))
        except (OSError, ValueError) as exc:
            raise StoreError(f"ledger unreadable: {exc}") from exc
        self._verify_rows(rows)
        for row in rows:
            self._apply_receipt(row)
        self._seq = len(rows)
        self._head = rows[-1]["hash"] if rows else LEDGER_GENESIS
        self._begun = True

    @staticmethod
    def _verify_rows(rows):
        previous = LEDGER_GENESIS
        for index, row in enumerate(rows, start=1):
            if type(row) is not dict or row.get("schema") != LEDGER_SCHEMA:
                raise StoreError(f"ledger_hash_chain_invalid:{index}")
            if row.get("seq") != index or row.get("prev_hash") != previous:
                raise StoreError(f"ledger_hash_chain_invalid:{index}")
            body = {key: row.get(key) for key in ("seq", "kind", "payload", "prev_hash")}
            if row.get("hash") != digest(body):
                raise StoreError(f"ledger_hash_chain_invalid:{index}")
            previous = row["hash"]
        return previous

    def verify_chain(self):
        path = self.run_dir / "ledger.jsonl"
        if not path.is_file():
            raise StoreError("ledger missing")
        rows = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rows.append(json.loads(line))
        except (OSError, ValueError) as exc:
            raise StoreError(f"ledger unreadable: {exc}") from exc
        head = self._verify_rows(rows)
        return {"valid": True, "seq": len(rows), "head": head, "receipts": len(rows)}

    def _apply_receipt(self, row):
        payload = row.get("payload") or {}
        kind = row.get("kind")
        if kind == "budget":
            dimension = payload.get("dimension")
            amount = payload.get("amount")
            if dimension in self._consumed and type(amount) is int and amount >= 0:
                self._consumed[dimension] += amount
        elif kind == "proof_replay_reserved":
            amount = payload.get("amount")
            if type(amount) is int and amount >= 0:
                self._consumed["replays"] += amount
        elif kind == "memory_observed":
            amount = payload.get("bytes")
            if type(amount) is int and amount >= self._consumed["memory_bytes"]:
                self._consumed["memory_bytes"] = amount
        elif kind == "calibration":
            self._calibrations += 1

    def high_water(self):
        remaining = {dimension: max(0, self.budget[dimension] - self._consumed[dimension])
                     for dimension in BUDGET_DIMENSIONS}
        return {"run_id": self.run_id, "budget": dict(self.budget),
                "consumed": dict(self._consumed), "remaining": remaining,
                "ledger_seq": self._seq, "ledger_head": self._head,
                "calibrations": self._calibrations,
                "max_calibrations": self.max_calibrations,
                "max_output_bytes": self.max_output_bytes}

    def begin(self, *, label=""):
        if self._begun:
            raise StoreError("run already begun")
        if type(label) is not str or len(label) > 256:
            raise StoreError("run label required")
        _ensure_disk(self.run_dir.parent, 4096)
        try:
            (self.run_dir / "worker").mkdir(parents=True, exist_ok=False)
            (self.run_dir / "checkpoints").mkdir(exist_ok=True)
        except OSError as exc:
            raise StoreError(f"run directory creation failed: {exc}") from exc
        record = {"schema": "research_run.v1", "run_id": self.run_id,
                  "identity": self.identity,
                  "identity_fingerprint": digest(self.identity),
                  "budget": dict(self.budget), "max_output_bytes": self.max_output_bytes,
                  "max_calibrations": self.max_calibrations,
                  "label": label, "created_at": _now()}
        _atomic_write(self.run_dir / "run.json", _encode(record))
        self._begun = True
        self.append_receipt("begin", {"identity_fingerprint": record["identity_fingerprint"],
                                      "budget": dict(self.budget),
                                      "max_output_bytes": self.max_output_bytes,
                                      "label": label})
        return {"run_id": self.run_id, "run_dir": str(self.run_dir),
                "identity_fingerprint": record["identity_fingerprint"],
                "budget": dict(self.budget), "high_water": self.high_water()}

    def append_receipt(self, kind, payload=None):
        if not self._begun:
            raise StoreError("begin required before appending receipts")
        if type(kind) is not str or not kind or len(kind) > 64:
            raise StoreError("receipt kind required")
        body = json_value(payload if payload is not None else {})
        if type(body) is not dict:
            raise StoreError("receipt payload mapping required")
        row = {"schema": LEDGER_SCHEMA, "seq": self._seq + 1, "at": _now(),
               "kind": kind, "payload": body, "prev_hash": self._head}
        row["hash"] = digest({key: row[key] for key in ("seq", "kind", "payload", "prev_hash")})
        _append_bytes(self.run_dir / "ledger.jsonl", _encode(row) + b"\n")
        self._seq += 1
        self._head = row["hash"]
        self._apply_receipt(row)
        return deepcopy(row)

    def _consume(self, dimension, amount, *, receipt_kind="budget", extra=None):
        if type(amount) is not int or amount < 0:
            raise StoreError("budget amount must be a non-negative integer")
        current = self.high_water()
        if amount > current["remaining"][dimension]:
            raise BudgetExhausted(dimension, current)
        payload = {"dimension": dimension, "amount": amount,
                   "cumulative": current["consumed"][dimension] + amount}
        if extra:
            payload.update(extra)
        self.append_receipt(receipt_kind, payload)
        return self.high_water()

    def consume_time(self, milliseconds):
        return self._consume("time_ms", milliseconds)

    def consume_nodes(self, nodes):
        return self._consume("nodes", nodes)

    def reserve_replay(self, count=1):
        return self._consume("replays", count, receipt_kind="proof_replay_reserved")

    def reserve_output(self, byte_count):
        return self._consume("output_bytes", byte_count)

    def observe_memory(self, byte_count):
        if type(byte_count) is not int or byte_count < 0:
            raise StoreError("memory observation must be non-negative")
        previous = self._consumed["memory_bytes"]
        self.append_receipt("memory_observed", {"bytes": byte_count, "previous_peak": previous})
        if byte_count > self.budget["memory_bytes"]:
            raise BudgetExhausted("memory_bytes", self.high_water())
        return self.high_water()

    def consume_calibration(self, *, time_ms=0):
        if self._calibrations + 1 > self.max_calibrations:
            raise BudgetExhausted("calibrations", self.high_water())
        if type(time_ms) is not int or time_ms < 0:
            raise StoreError("calibration time must be non-negative")
        if time_ms:
            self._consume("time_ms", time_ms)
        self.append_receipt("calibration", {"time_ms": time_ms, "count": self._calibrations + 1})
        return self.high_water()

    def checkpoint(self, payload):
        if not self._begun:
            raise StoreError("begin required before checkpoint")
        value = json_value(payload)
        if type(value) is not dict:
            raise StoreError("checkpoint payload mapping required")
        if value.get("persisted_to_project_state") is True or value.get("new_site_stage_unlocked") is True:
            raise StoreError("checkpoint cannot acquire project authority")
        try:
            if len(_encode(value)) > MAX_CHECKPOINT_BYTES:
                raise StoreError("checkpoint payload exceeds its byte limit")
        except (TypeError, ValueError) as exc:
            raise StoreError(f"checkpoint payload invalid: {exc}") from exc
        metrics = value.get("metrics")
        if metrics is not None:
            if type(metrics) is not dict:
                raise StoreError("checkpoint metrics must be a mapping")
            for dimension in BUDGET_DIMENSIONS:
                if dimension in metrics:
                    used = metrics[dimension]
                    if type(used) is not int or used < self._consumed[dimension]:
                        raise StoreError("checkpoint cumulative budget rollback")
        record = {"schema": CHECKPOINT_SCHEMA, "run_id": self.run_id, "seq": self._seq,
                  "ledger_head": self._head, "identity": self.identity,
                  "identity_fingerprint": digest(self.identity),
                  "high_water": dict(self._consumed), "budget": dict(self.budget),
                  "payload": value, "created_at": _now()}
        record["checkpoint_fingerprint"] = digest(record)
        encoded = _encode(record)
        name = f"checkpoint-{self._seq:08d}.json"
        target = self.checkpoints_root / name
        _atomic_write(target, encoded)
        latest = {"checkpoint_id": name, "seq": self._seq,
                  "ledger_head": self._head,
                  "checkpoint_fingerprint": record["checkpoint_fingerprint"],
                  "written_at": _now()}
        _atomic_write(self.checkpoints_root / "checkpoint.latest.json", _encode(latest))
        return {"checkpoint_id": name, "seq": self._seq, "ledger_head": self._head,
                "path": str(target), "checkpoint_fingerprint": record["checkpoint_fingerprint"]}

    def load_checkpoint(self):
        latest_path = self.checkpoints_root / "checkpoint.latest.json"
        if not latest_path.is_file():
            return None
        try:
            latest = json.loads(latest_path.read_text(encoding="utf-8"))
            json_value(latest)
            record = json.loads((self.checkpoints_root / latest["checkpoint_id"]).read_text(
                encoding="utf-8"))
            json_value(record)
        except (OSError, KeyError, ValueError) as exc:
            raise StoreError(f"checkpoint unreadable: {exc}") from exc
        if record.get("schema") != CHECKPOINT_SCHEMA:
            raise StoreError("checkpoint schema mismatch")
        if record.get("checkpoint_fingerprint") != digest(
                {key: value for key, value in record.items() if key != "checkpoint_fingerprint"}):
            raise StoreError("checkpoint fingerprint mismatch")
        if record.get("run_id") != self.run_id:
            raise StoreError("checkpoint run id mismatch")
        if record.get("identity_fingerprint") != digest(self.identity):
            raise StoreError("checkpoint identity mismatch")
        record["checkpoint_id"] = latest["checkpoint_id"]
        return record

    def recover(self, *, identity, budget, time_ms=0):
        if not self._begun:
            raise RecoveryRejected("ledger_missing")
        wanted = checked_identity(identity)
        allowance = checked_budget(budget)
        self.verify_chain()
        self._load_ledger()
        for field in IDENTITY_FIELDS:
            if self.identity[field] != wanted[field]:
                raise RecoveryRejected(f"identity_mismatch:{field}")
        for dimension in BUDGET_DIMENSIONS:
            if allowance[dimension] < self._consumed[dimension]:
                raise RecoveryRejected(f"budget_regression:{dimension}")
        checkpoint = self.load_checkpoint()
        if checkpoint is None:
            raise RecoveryRejected("checkpoint_missing")
        if checkpoint["ledger_head"] != self._head:
            raise RecoveryRejected("stale_checkpoint")
        if checkpoint["high_water"] != self._consumed:
            raise RecoveryRejected("checkpoint_budget_mismatch")
        if type(time_ms) is not int or time_ms < 0:
            raise StoreError("recovery time must be non-negative")
        if time_ms:
            self._consume("time_ms", time_ms)
        self.append_receipt("recovered", {"checkpoint_id": checkpoint["checkpoint_id"],
                                          "time_ms": time_ms,
                                          "ledger_seq": self._seq})
        return {"status": "recovered", "run_id": self.run_id,
                "checkpoint": {"checkpoint_id": checkpoint["checkpoint_id"],
                               "payload": checkpoint["payload"],
                               "ledger_head": checkpoint["ledger_head"],
                               "high_water": checkpoint["high_water"]},
                "high_water": self.high_water(), "time_ms": time_ms,
                "identity_fingerprint": digest(self.identity)}

    def write_worker_json(self, name, value):
        if type(name) is not str or re.fullmatch(r"[a-z0-9_.-]{1,64}", name) is None:
            raise StoreError("worker file name required")
        body = json_value(value)
        encoded = _encode(body)
        self.reserve_output(len(encoded))
        target = self.worker_root / name
        _atomic_write(target, encoded)
        return {"name": name, "path": str(target), "bytes": len(encoded),
                "sha256": digest(body)}

    def finalize(self, status, payload=None):
        if not self._begun:
            raise StoreError("begin required before finalize")
        allowed = {"completed", "aborted", "blocked", "crashed", "cancelled",
                   "deadline_exceeded", "memory_exceeded", "output_budget_exceeded",
                   "budget_exhausted"}
        if status not in allowed:
            raise StoreError("invalid finalize status")
        body = json_value(payload if payload is not None else {})
        if type(body) is not dict:
            raise StoreError("finalize payload mapping required")
        if status == "completed":
            record = {"schema": RESULT_SCHEMA, "run_id": self.run_id, "status": status,
                      "identity_fingerprint": digest(self.identity),
                      "high_water": dict(self._consumed), "payload": body,
                      "created_at": _now()}
            record["result_fingerprint"] = digest(record)
            encoded = _encode(record)
            try:
                _atomic_write(self.run_dir / "result.json", encoded)
                complete = {"schema": "research_run_complete.v1", "run_id": self.run_id,
                            "complete": True,
                            "result_fingerprint": record["result_fingerprint"],
                            "ledger_head": self._head, "high_water": dict(self._consumed),
                            "created_at": _now()}
                _atomic_write(self.run_dir / "complete.json", _encode(complete))
            except OSError as exc:
                raise StoreError(f"finalize write failed: {exc}") from exc
            self.append_receipt("finalize:completed",
                                {"result_fingerprint": record["result_fingerprint"],
                                 "high_water": dict(self._consumed)})
            return {"status": status, "complete": True, "run_id": self.run_id,
                    "result_fingerprint": record["result_fingerprint"],
                    "high_water": self.high_water(), "payload": body}
        record = {"schema": "research_run_aborted.v1", "run_id": self.run_id,
                  "status": status, "reason": status,
                  "identity_fingerprint": digest(self.identity),
                  "high_water": dict(self._consumed), "payload": body,
                  "created_at": _now()}
        record["aborted_fingerprint"] = digest(record)
        encoded = _encode(record)
        try:
            _atomic_write(self.run_dir / "aborted.json", encoded)
        except OSError as exc:
            raise StoreError(f"finalize write failed: {exc}") from exc
        self.append_receipt(f"finalize:{status}",
                            {"aborted_fingerprint": record["aborted_fingerprint"],
                             "high_water": dict(self._consumed)})
        return {"status": status, "complete": False, "run_id": self.run_id,
                "aborted_fingerprint": record["aborted_fingerprint"],
                "high_water": self.high_water(), "payload": body}


__all__ = ["ResearchRunStore", "StoreError", "RecoveryRejected", "BudgetExhausted",
           "approve_output_root", "checked_identity", "checked_budget",
           "DEFAULT_OUTPUT_LIMIT_BYTES", "BUDGET_DIMENSIONS", "IDENTITY_FIELDS"]
