"""Content-addressed JSON proposal files; no project/session/artifact writer."""
from pathlib import Path
import json
import os
import re
import stat
import uuid
from ..domain.joint_feasibility import json_value,digest,verify


MAX_PROPOSAL_BYTES=64*1024*1024


class JointProposalStore:
    def __init__(self, *, proposal_root):
        root=Path(proposal_root).absolute()
        if (root.name!="proposals" or {p.lower() for p in root.parts}&{"artifacts",".git","cns_planner"}
                or any(part.lower()=="projects" for part in root.parts[2:])
                or any((parent/"current_project.json").exists() for parent in root.parents)):
            raise ValueError("dedicated proposal root required; formal paths forbidden")
        self.root=root
        self._check_path(root)
        root.mkdir(parents=True,exist_ok=True)
        self.resolved=root.resolve()

    @staticmethod
    def _check_path(path):
        for part in (path,*path.parents):
            if part.exists() or part.is_symlink():
                info=part.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info,"st_file_attributes",0)&0x400:
                    raise ValueError("proposal reparse/symlink forbidden")

    def _path(self, identity):
        if not re.fullmatch(r"(?:checkpoint|result)-[0-9a-f]{64}",identity):raise ValueError("invalid proposal identity")
        self._check_path(self.root)
        if self.root.resolve()!=self.resolved:raise ValueError("proposal root changed")
        path=self.root/(identity+".json");self._check_path(path)
        return path

    def _save(self,kind,payload):
        value=json_value(payload)
        if value.get("new_site_stage_unlocked") is True or value.get("persisted_to_project_state") is True:
            raise ValueError("proposal cannot acquire project authority")
        if kind=="checkpoint":verify(value,"checkpoint_digest")
        content_digest=digest(value);identity=kind+"-"+content_digest;path=self._path(identity)
        encoded=json.dumps(dict(schema_version="joint_proposal_file.v1",payload_digest=content_digest,payload=value),
            ensure_ascii=False,sort_keys=True,allow_nan=False).encode("utf-8")
        used=sum(p.stat().st_size for p in self.root.iterdir() if p.is_file() and p!=path)
        if used+len(encoded)>MAX_PROPOSAL_BYTES:raise ValueError("proposal total storage budget exceeded (64MiB default)")
        temporary=self.root/(".staging-"+uuid.uuid4().hex)
        try:
            with temporary.open("xb") as stream:
                stream.write(encoded);stream.flush();os.fsync(stream.fileno())
            self._path(identity)
            os.replace(temporary,path)
        finally:
            if temporary.exists():temporary.unlink()
        return identity

    def save_checkpoint(self, *, payload):return self._save("checkpoint",payload)
    def save_result(self, *, payload):return self._save("result",payload)

    def load_checkpoint(self, *, checkpoint_id,expected_identity):
        if not checkpoint_id.startswith("checkpoint-"):raise ValueError("checkpoint identity required")
        path=self._path(checkpoint_id)
        if path.stat().st_size>64*1024*1024:raise ValueError("checkpoint size budget")
        def no_duplicate(pairs):
            result={}
            for key,val in pairs:
                if key in result:raise ValueError("duplicate JSON key")
                result[key]=val
            return result
        data=json.loads(path.read_text(encoding="utf-8"),object_pairs_hook=no_duplicate)
        json_value(data)
        if data.get("schema_version")!="joint_proposal_file.v1":raise ValueError("checkpoint file schema")
        payload=data.get("payload");actual=digest(payload)
        if data.get("payload_digest")!=actual or checkpoint_id!="checkpoint-"+actual:raise ValueError("checkpoint digest mismatch")
        verify(payload,"checkpoint_digest")
        if payload.get("identity")!=expected_identity:raise ValueError("checkpoint identity mismatch")
        return payload
