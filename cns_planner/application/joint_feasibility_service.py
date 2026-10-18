"""Opt-in M4 application runner. Not registered in production API/tasks."""
from copy import deepcopy
import os
import time
from ..domain.joint_feasibility import digest,verify
from ..algorithms.joint_feasibility.v1 import iter_joint_feasibility,evaluate_structural_necessary_conditions,_identity
from .joint_evaluation_adapter import JointModelEvaluator
from .joint_candidate_assessment import bind_joint_candidate_assessment,assemble_joint_search_result
from .joint_feasibility_store import JointProposalStore


def _memory_mb():
    if os.name=="nt":
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_=[("cb",wintypes.DWORD),("PageFaultCount",wintypes.DWORD)]+[(n,ctypes.c_size_t) for n in (
                "PeakWorkingSetSize","WorkingSetSize","QuotaPeakPagedPoolUsage","QuotaPagedPoolUsage",
                "QuotaPeakNonPagedPoolUsage","QuotaNonPagedPoolUsage","PagefileUsage","PeakPagefileUsage")]
        count=Counters();count.cb=ctypes.sizeof(count)
        kernel=ctypes.WinDLL("kernel32",use_last_error=True);kernel.GetCurrentProcess.restype=wintypes.HANDLE
        psapi=ctypes.WinDLL("psapi",use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes=[wintypes.HANDLE,ctypes.POINTER(Counters),wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype=wintypes.BOOL
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(),ctypes.byref(count),count.cb):return None
        return count.WorkingSetSize/1024**2
    import resource
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024


class _Runtime:
    def __init__(self,domain,context):
        self.domain=deepcopy(domain);self.context=deepcopy(context)
        self.evaluator=JointModelEvaluator(frozen_context=context,frozen_domain=domain)
        self.cache={};self.cache_hits=0;self.cancelled=False
    def evaluate_action_set(self, *, actions):
        key=digest(actions)
        if key not in self.cache:self.cache[key]=self.evaluator.evaluate(actions=actions)
        else:self.cache_hits+=1
        return deepcopy(self.cache[key])
    def evaluate_necessary_conditions(self, *, node):
        return evaluate_structural_necessary_conditions(node=node,frozen_domain=self.domain,frozen_context=self.context)
    def monotonic_seconds(self):return time.monotonic()
    def cancel_requested(self):return self.cancelled
    def resident_memory_mb(self):return _memory_mb()


def run_joint_feasibility(*,frozen_domain,frozen_context,search_policy,proposal_store,checkpoint_id=None):
    if not isinstance(proposal_store,JointProposalStore):raise ValueError("trusted proposal store required")
    verify(frozen_context,"context_fingerprint");verify(frozen_domain,"domain_fingerprint")
    checkpoint=proposal_store.load_checkpoint(checkpoint_id=checkpoint_id,expected_identity=_identity(frozen_domain,frozen_context)) if checkpoint_id else None
    runtime=_Runtime(frozen_domain,frozen_context);last_checkpoint_id=None;save_error=None
    for event in iter_joint_feasibility(frozen_domain=frozen_domain,frozen_context=frozen_context,search_policy=search_policy,runtime=runtime,checkpoint=checkpoint):
        if event["kind"]=="checkpoint":
            try:last_checkpoint_id=proposal_store.save_checkpoint(payload=event["payload"])
            except (OSError,ValueError) as exc:save_error=str(exc);runtime.cancelled=True
        elif event["kind"]=="result":result=event["payload"]
    assessments=[bind_joint_candidate_assessment(model_evaluation=s["model_evaluation"],actions=s["actions"],frozen_context=frozen_context,
        evidence_registry={}) for s in result["feasible_combinations"]]
    result=assemble_joint_search_result(search_result=result,candidate_assessments=assessments,frozen_context=frozen_context)
    try:last_checkpoint_id=proposal_store.save_checkpoint(payload=result["checkpoint"])
    except (OSError,ValueError) as exc:save_error=str(exc)
    result.update(checkpoint_id=last_checkpoint_id,checkpoint_save_status="checkpoint_save_failed" if save_error else "saved",
        cache_hit_count=runtime.cache_hits,oracle_call_count=result["joint_replay_count"],actual_joint_evaluations=len(runtime.cache),
        shared_chain_replay_count=runtime.evaluator.replay_count)
    if save_error:
        result.update(checkpoint_save_error=save_error,search_status="search_incomplete",coverage_complete=False)
    try:result["result_id"]=proposal_store.save_result(payload=result)
    except (OSError,ValueError) as exc:result.update(result_save_status="failed",result_save_error=str(exc))
    return result
