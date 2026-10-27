"""Read-only R0005 checkpoint integration; no business solver or formal writer.

The server supplies an allowlisted checkpoint descriptor. Request paths cannot
select a filesystem root. Historical envelopes remain byte-for-byte unchanged.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

TASK_TYPE = "r0005_research_checkpoint_restore"
FRESH_TASK_TYPE = "r0005_research_fresh_compute"
STAGES = ("P14", "P15", "P16", "P17", "P18")
PROJECTION_VERSION = "c3a-read-projection-1"


def file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", value):
        raise ValueError("invalid_run_id")
    return value


def normalize_manifest(source):
    """Fix the legacy layer projection only, preserving the outage verdict."""
    view = deepcopy(source)
    radar = view.get("business_state", {}).get("surveillance_Radar", {})
    threats = view.setdefault("p17_threat_layers", {})
    layer = threats.setdefault("layers", {}).setdefault("noncooperative", {})
    if radar.get("radar_surveillance_layout_present") is not True:
        layer["source_status"] = layer.get("status")
        layer["source_conclusion"] = layer.get("conclusion")
        layer.update(status="unknown", conclusion="unknown")
        layer["reasons"] = sorted(set(layer.get("reasons", [])) | {"no_detection_evidence", "radar_layout_missing"})
        layer["normalization"] = PROJECTION_VERSION
        threats["supplementary_threat_status"] = "unknown"
        radar["verdict"] = "unknown"
        radar["p17_supplementary_threat_status"] = "unknown"
    gate = view.get("gates", {}).get("p17_step6_gate", {})
    if "confirmation_allowed" in gate:
        gate["source_confirmation_allowed"] = gate.pop("confirmation_allowed")
    gate["scope"] = "continuous_outage_events_only_not_formal_confirmation"
    gate["allows_review_initialization_only"] = bool(gate.get("source_confirmation_allowed"))
    view["confirmation_allowed"] = False
    view["formal_release_allowed"] = False
    view["projection_version"] = PROJECTION_VERSION
    view["computation_status"] = "checkpoint_restored_no_solver_execution"
    business = view.get("business_state", {})
    view["status_dimensions"] = {
        "task": "succeeded", **{stage: business.get(stage, {}).get("status", "unknown") for stage in ("P14", "P15", "P16")},
        "P17": gate.get("status", "unknown"), "P17_scope": gate["scope"],
        "P18_confirmation_allowed": False,
    }
    return view


def recovery_inputs(state, payload):
    if payload.get("mode") != "resume_checkpoint":
        raise ValueError("only_explicit_resume_checkpoint_supported; fresh_compute_not_implemented")
    run_id = identifier(payload.get("run_id"))
    checkpoint = state.get("research_checkpoint")
    if not checkpoint or checkpoint.get("authority") != "research_isolated":
        raise ValueError("research_runtime_not_enabled")
    for key in ("source_run_id", "input_version", "contract_version", "authoritative_result_list_sha256"):
        if payload.get(key) != checkpoint.get(key):
            raise ValueError("checkpoint_identity_mismatch: " + key)
    if run_id == checkpoint["source_run_id"]:
        raise ValueError("recovery_run_id_must_be_distinct")
    return {"checkpoint": deepcopy(checkpoint), "run_id": run_id, "mode": "resume_checkpoint"}


def fresh_inputs(state, payload):
    """Freeze the allowlisted R0005 *source input* descriptor for a fresh run.

    Request paths cannot select a filesystem root: the descriptor comes from the
    server-owned dispatch state, exactly like the C3-A checkpoint descriptor.
    Editable research parameters are validated against the descriptor allowlist
    here and applied by the worker; their fingerprint becomes part of the run
    identity, so a changed parameter can never reuse the previous result.
    """

    from .research_parameters import describe, validate_values

    if payload.get("mode") != "fresh_compute":
        raise ValueError("only_explicit_fresh_compute_supported")
    run_id = identifier(payload.get("run_id"))
    descriptor = state.get("research_fresh_input")
    if not descriptor or descriptor.get("authority") != "research_isolated":
        raise ValueError("research_runtime_not_enabled")
    root = Path(descriptor.get("root") or "").resolve()
    if not (root / "runtime" / "project_state.json").is_file():
        raise ValueError("research_source_input_missing")
    if (payload.get("source_run_id")
            and payload["source_run_id"] != descriptor.get("source_run_id")):
        raise ValueError("source_identity_mismatch: source_run_id")
    schema = descriptor.get("parameter_schema")
    values = validate_values(schema, payload.get("parameters"))
    described = describe(values, schema) if values is not None else None
    return {
        "fresh_input": {
            **{key: descriptor[key] for key in descriptor},
            "root": str(root),
            "parameter_schema": schema,
        },
        "research_parameters": described,
        "parameter_fingerprint": (described or {}).get("fingerprint"),
        "run_id": run_id,
        "mode": "fresh_compute",
    }


def fresh_compute(context):
    """Executed by the existing cns_planner.tasks.worker process."""

    from .research_compute_service import run_fresh_compute
    return run_fresh_compute(context)


def validate_checkpoint_identity(checkpoint, source, index_sha):
    for descriptor_key, source_key in (("source_run_id", "run_id"), ("input_version", "input_version"),
                                       ("contract_version", "contract_version")):
        if checkpoint.get(descriptor_key) != source.get(source_key):
            raise ValueError("source_identity_mismatch: " + descriptor_key)
    if checkpoint.get("authoritative_result_list_sha256") != index_sha:
        raise ValueError("authoritative_result_list_sha_mismatch")


def restore_checkpoint(context):
    """Executed by the existing cns_planner.tasks.worker process."""
    from research_session import ResearchSession, ResultStore, ArtifactStore
    from ..tasks.task_spec import TaskRunResult, WorkerResultRef

    inputs = context.inputs["snapshot"]["inputs"]
    checkpoint = inputs["checkpoint"]
    root = Path(checkpoint["root"]).resolve()
    for relative, expected in checkpoint["files"].items():
        path = (root / relative).resolve()
        if root not in path.parents or file_sha(path) != expected:
            raise ValueError("checkpoint_sha_mismatch: " + relative)
    if file_sha(checkpoint["contract_path"]) != checkpoint["contract_sha256"]:
        raise ValueError("contract_sha_mismatch")
    source = read_json(root / "run_manifest.json")
    validate_checkpoint_identity(checkpoint, source, file_sha(root / "results/index.json"))
    session = ResearchSession(root)
    session.load()  # existing research authority and state SHA checks
    if session.state_fingerprint() != checkpoint["input_version"]["input_fingerprint"]:
        raise ValueError("research_input_fingerprint_mismatch")
    if not source["input_version"]["communication_radius"]["communication_is_2km_by_surface"]:
        raise ValueError("2km_contract_required")
    del session
    results, artifacts = ResultStore(root), ArtifactStore(root)
    verified = []
    for number, stage in enumerate(STAGES):
        context.check_cancel()
        entry = source["stages"][stage]
        if entry["authority"] != "research_isolated":
            raise ValueError("stage_authority_mismatch")
        envelope = results.get(stage)  # validates file SHA and payload SHA
        index = results._index()["entries"][stage]
        if index["sha256"] != entry["result_sha256"] or envelope["payload_sha256"] != entry["payload_sha256"]:
            raise ValueError("stage_sha_mismatch: " + stage)
        if envelope["input_fingerprint"] != checkpoint["input_version"]["input_fingerprint"]:
            raise ValueError("stage_input_mismatch: " + stage)
        stage_fingerprint = (envelope["payload"].get("result") or {}).get("input_fingerprint") if stage == "P17" else envelope["input_fingerprint"]
        if stage_fingerprint != entry["input_fingerprint"]:
            raise ValueError("stage_algorithm_input_mismatch: " + stage)
        del envelope
        artifact = artifacts.read(entry.get("artifact_id") or entry["payload_sha256"])
        del artifact
        verified.append(stage)
        context.progress((number + 1) / 6, "校验并重载 " + stage + "；未重新求解")
    with sqlite3.connect((root / "tasks/tasks.sqlite").as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        for row in db.execute("SELECT * FROM tasks"):
            row = dict(row)
            if row["status"] == "done" and row["result_sha"] not in {
                e["sha256"] for e in results._index()["entries"].values()
            }:
                raise ValueError("inherited_task_result_missing")
    view = normalize_manifest(source)
    view.update(run_id=inputs["run_id"], task="C3-A", schema_version="c3a-run-manifest-1",
                research_root=str(Path(context.workdir).parent),
                authoritative_result_list_sha256=checkpoint["authoritative_result_list_sha256"],
                authoritative_results=results._index())
    view["provenance"] = {
        "mode": "resume_checkpoint", "source_run_id": source["run_id"], "source_root": str(root),
        "source_manifest_sha256": checkpoint["files"]["run_manifest.json"],
        "source_contract_sha256": checkpoint["contract_sha256"],
        "solver_executed": False, "verified_stages": verified,
    }
    view["execution"] = {"worker_pid": os.getpid(), "task_id": context.task_id,
                         "task_input_snapshot_sha256": context.record["input_fingerprint"],
                         "worker": "cns_planner.tasks.worker", "mode": "resume_checkpoint"}
    for stage, entry in view["stages"].items():
        entry.update(source_run=source["run_id"], source_result_sha256=entry["result_sha256"],
                     inherited_from_previous_run=True, recomputed_in_this_run=False,
                     reloaded_in_this_run=True,
                     run_input_fingerprint=checkpoint["input_version"]["input_fingerprint"],
                     stage_input_fingerprint=entry["input_fingerprint"])
    staged = context.stage(view, artifact_type="r0005_research_run_manifest")
    return TaskRunResult(summary={key: view[key] for key in (
        "run_id", "input_version", "contract_version", "authoritative_result_list_sha256",
        "status_dimensions", "provenance")}, staged=WorkerResultRef(
            artifact_id=staged["artifact_id"], relative_path=staged["relative_path"],
            sha256=staged["sha256"], size_bytes=staged["size_bytes"],
            artifact_type="r0005_research_run_manifest"), message="研究检查点已校验重载；未重新求解")


class ResearchRunService:
    def __init__(self, root, tasks, state=None):
        self.root = Path(root).resolve()
        self.tasks = tasks
        self.state = state if isinstance(state, dict) else {}

    def research_input(self):
        """Sanitized pre-run input contract; never exposes a filesystem root."""

        from .research_parameters import default_values, normalize_schema

        descriptor = self.state.get("research_fresh_input") or {}
        ready = (descriptor.get("authority") == "research_isolated"
                 and bool(descriptor.get("root")))
        schema = None
        defaults = None
        error = None
        if ready:
            try:
                schema = normalize_schema(descriptor.get("parameter_schema"))
                defaults = default_values(schema)
            except ValueError as exc:
                error = str(exc)
        return {
            "ready": ready and error is None,
            "authority": descriptor.get("authority"),
            "mode": "fresh_compute",
            "route_id": descriptor.get("route_id") or "R0005",
            "source_run_id": descriptor.get("source_run_id"),
            "source_state_sha256": descriptor.get("source_state_sha256"),
            "contract_version": descriptor.get("contract_version"),
            "input_summary": descriptor.get("input_summary") or {},
            "parameter_schema": schema,
            "default_parameters": defaults,
            "parameter_error": error,
            "solver": "cns_planner.tasks.worker",
            "task_type": FRESH_TASK_TYPE,
        }

    def list_runs(self, limit=20):
        """Read-only run index from the task store; no solver and no state write."""

        rows, seen = [], set()
        for record in self.tasks.store.list(limit=max(1, int(limit) * 4)):
            payload = record.get("worker_payload") or {}
            run_id = payload.get("run_id")
            if not run_id or run_id in seen:
                continue
            seen.add(run_id)
            summary = record.get("result_summary") or {}
            rows.append({
                "run_id": run_id,
                "task_id": record.get("task_id"),
                "status": record.get("status"),
                "result_ready": record.get("status") == "succeeded",
                "mode": payload.get("mode"),
                "task_type": record.get("task_type"),
                "created_at": record.get("created_at"),
                "updated_at": record.get("updated_at"),
                "input_version": summary.get("input_version"),
                "contract_version": summary.get("contract_version"),
                "authoritative_result_list_sha256": summary.get(
                    "authoritative_result_list_sha256"),
            })
            if len(rows) >= limit:
                break
        return rows

    def bootstrap(self, token):
        runs = self.list_runs()
        latest = max(
            (row for row in runs if row["result_ready"]),
            key=lambda row: str(row.get("updated_at") or row.get("created_at") or ""),
            default=None,
        )
        return {
            "service": "cns-map", "ready": True, "authority": "research_isolated",
            "read_scope": "research_isolated_read_only",
            "token": token,
            "input": self.research_input(),
            "runs": runs,
            "latest_run_id": (latest or {}).get("run_id"),
            "report_contract": "c3c-research-report-1",
            "semantics": (
                "research browser session; POST only accepts fresh_compute / checkpoint restore; "
                "no formal ProjectState mutation"),
        }

    def report(self, run_id, format_name="html"):
        from .research_report_service import build_report

        manifest = self.manifest(run_id)
        report = build_report(manifest)
        if format_name == "markdown":
            return report["markdown"], "text/markdown; charset=utf-8", manifest, report
        return report["html"], "text/html; charset=utf-8", manifest, report

    def manifest(self, run_id):
        run_id = identifier(run_id)
        record = next((r for r in self.tasks.store.list() if
                       (r.get("worker_payload") or {}).get("run_id") == run_id), None)
        if record is None:
            raise KeyError("research_run_not_found")
        publication = self.tasks.store.publish_record(record["task_id"])
        if record["status"] != "succeeded" or not publication:
            raise ValueError("research_run_not_ready: " + record["status"])
        reference = publication.get("result_artifact_ref")
        if not reference:
            raise ValueError("research_publication_reference_missing")
        from ..persistence.artifact_store import ArtifactStore
        manifest_path = self.root / "runs" / run_id / "run_manifest.json"
        if file_sha(manifest_path) != reference.get("run_manifest_sha256"):
            raise ValueError("run_manifest_sha_mismatch")
        view = ArtifactStore(self.tasks.workdir).read(reference)
        if view["run_id"] != run_id:
            raise ValueError("run_identity_mismatch")
        return view

    def visualization_3d(self, run_id):
        """只读三维投影：同一 run manifest + 冻结地形资产，不重算任何业务结论。"""

        manifest = self.manifest(run_id)
        manifest_path = self.root / "runs" / run_id / "run_manifest.json"
        from .research_visualization_service import build_visualization_3d

        return build_visualization_3d(
            manifest, root=self.root, manifest_file_sha256=file_sha(manifest_path))

    def result(self, run_id, stage):
        if stage not in STAGES:
            raise KeyError("unknown_stage")
        view = self.manifest(run_id)
        entry = view["stages"][stage]
        provenance = view.get("provenance") or {}
        result_root = provenance.get("result_root") or provenance["source_root"]
        path = Path(result_root) / "results" / (stage + ".json")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != entry["result_sha256"]:
            raise ValueError("result_sha_mismatch: " + stage)
        return raw, view
