"""M5 immutable proposal storage; reuse M4 path and JSON load protections."""
from pathlib import Path
import json
import os
import uuid
from .joint_feasibility_store import JointProposalStore
from ..domain.joint_feasibility import json_value, digest, verify

APPROVED_ROOT = Path("D:/Projects/_cns_round38_global_recovery/m5_lexicographic_optimization/proposals/m5")
MAX_TOTAL_BYTES = 67108864


class M5ProposalStore(JointProposalStore):
    def __init__(self, *, root: Path, max_total_bytes: int = MAX_TOTAL_BYTES):
        root = Path(root).absolute()
        self._check_path(root)
        if type(max_total_bytes) is not int or not 0 < max_total_bytes <= MAX_TOTAL_BYTES:
            raise ValueError("proposal storage budget must be within 64MiB")
        # Production output is one fixed approved root. Tests may use code-provided
        # paths beneath their explicitly configured M5 TEMP, never a JSON root.
        temp = os.environ.get("TEMP")
        controlled_test = bool(temp and Path(temp).name == "m5_c" and root.is_relative_to(Path(temp).absolute()))
        forbidden = {"artifacts", ".git", "cns_planner"}
        if (root.name != "m5" or root.parent.name != "proposals" or
                (root != APPROVED_ROOT and not controlled_test) or
                forbidden.intersection(p.lower() for p in root.parts) or
                any(p.lower()=="projects" for p in root.parts[2:]) or
                any((p/"current_project.json").exists() for p in (root,*root.parents))):
            raise ValueError("dedicated approved M5 proposal root required")
        self.root=root;root.mkdir(parents=True,exist_ok=True);self.resolved=root.resolve()
        self.max_total_bytes=max_total_bytes;self.run_limit=max_total_bytes

    def _check_budget_floor(self, value):
        # Immutable, content-addressed checkpoints form the durable budget high
        # water mark for this exact context. A stale/resealed lower-budget branch
        # may not erase work already recorded in this dedicated proposal store.
        current=value.get("metrics", {})
        for path in self.root.glob("checkpoint-*.json"):
            self._check_path(path)
            data=json.loads(path.read_text(encoding="utf-8"))
            json_value(data)
            old_identity=(data.get("payload") or {}).get("identity")
            old=super().load_checkpoint(checkpoint_id=path.stem,expected_identity=old_identity)
            if old_identity!=value["identity"]:continue
            for key,used in old["metrics"].items():
                if type(used) not in (int,float) or used<0 or type(current.get(key)) not in (int,float) or current[key]<used:
                    raise ValueError("checkpoint cumulative budget rollback")

    def _save(self, kind, payload):
        value=json_value(payload)
        if value.get("new_site_stage_unlocked") is True or value.get("persisted_to_project_state") is True:
            raise ValueError("proposal cannot acquire project authority")
        if kind=="checkpoint":
            verify(value,"checkpoint_digest")
            if value.get("schema_version")!="existing_site_checkpoint.v1":raise ValueError("M5 checkpoint schema")
            self._check_budget_floor(value)
        content_digest=digest(value);identity=kind+"-"+content_digest;path=self._path(identity)
        encoded=json.dumps(dict(schema_version="joint_proposal_file.v1",payload_digest=content_digest,payload=value),
            ensure_ascii=False,sort_keys=True,allow_nan=False).encode("utf-8")
        used=0
        for item in self.root.iterdir():
            self._check_path(item)
            if not item.is_file():raise ValueError("unexpected proposal directory")
            used+=item.stat().st_size
        if path.exists():
            if path.read_bytes()!=encoded:raise ValueError("immutable proposal collision")
            return identity
        if used+len(encoded)>min(self.max_total_bytes,self.run_limit):
            raise ValueError("proposal total storage budget exceeded (maximum 64MiB)")
        temporary=self.root/(".staging-"+uuid.uuid4().hex)
        try:
            with temporary.open("xb") as stream:
                stream.write(encoded);stream.flush();os.fsync(stream.fileno())
            self._path(identity)
            os.replace(temporary,path)
        finally:
            if temporary.exists():temporary.unlink()
        return identity

    def load_checkpoint(self, *, checkpoint_id, expected_identity):
        value=super().load_checkpoint(checkpoint_id=checkpoint_id,expected_identity=expected_identity)
        if value.get("schema_version")!="existing_site_checkpoint.v1":raise ValueError("M5 checkpoint schema")
        self._check_budget_floor(value)
        return value
