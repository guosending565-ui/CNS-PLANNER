"""R2.2-B: controlled Windows worker supervision.

The parent creates every child suspended, binds it to a Job Object that carries
``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` and terminates that job on a hard
deadline, cancellation, memory overrun or output overrun.  Termination is by
handle/job only; no process is ever selected by executable name, so unrelated
business services are untouched.  A failure to start, bind or reclaim the tree
returns ``blocked`` instead of degrading into an unsupervised execution.

The ``-m`` entry accepts only the fixed callback registry below.  JSON may
choose a registry key, never a module path, executable, Python function or
dynamic import.
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

if __package__ in (None, ""):
    # Direct script entry: make the repository root importable without executing
    # cns_planner.application.__init__ (which eagerly imports GIS services).
    _ROOT = str(Path(__file__).resolve().parents[2])
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)

from cns_planner.domain.joint_feasibility import digest, json_value
from cns_planner.domain.research_scenario import bounded_json

WORKER_SCHEMA = "research_worker_request.v1"
RESULT_SCHEMA = "research_worker_result.v1"
MAX_REQUEST_BYTES = 2 * 1024 * 1024
SOURCE_ROOT = Path(__file__).resolve().parents[2]

CREATE_SUSPENDED = 0x00000004
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_UNICODE_ENVIRONMENT = 0x00000400
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_ASSOCIATE_COMPLETION_PORT_INFORMATION = 7
JOB_OBJECT_MSG_JOB_MEMORY_LIMIT = 10
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
STILL_ACTIVE = 259
MAX_ACTIVE_PROCESSES = 32


class WorkerBlocked(RuntimeError):
    """Controlled worker could not be started or supervised safely."""


class _LARGE_INTEGER(ctypes.Union):
    _fields_ = [("QuadPart", ctypes.c_longlong), ("LowPart", wt.DWORD), ("HighPart", wt.LONG)]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", _LARGE_INTEGER),
        ("PerJobUserTimeLimit", _LARGE_INTEGER),
        ("LimitFlags", wt.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wt.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wt.DWORD),
        ("SchedulingClass", wt.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", _LARGE_INTEGER),
        ("TotalKernelTime", _LARGE_INTEGER),
        ("ThisPeriodTotalUserTime", _LARGE_INTEGER),
        ("ThisPeriodTotalKernelTime", _LARGE_INTEGER),
        ("TotalPageFaultCount", wt.DWORD),
        ("TotalProcesses", wt.DWORD),
        ("ActiveProcesses", wt.DWORD),
        ("TotalTerminatedProcesses", wt.DWORD),
    ]


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD),
        ("lpReserved", wt.LPWSTR),
        ("lpDesktop", wt.LPWSTR),
        ("lpTitle", wt.LPWSTR),
        ("dwX", wt.DWORD),
        ("dwY", wt.DWORD),
        ("dwXSize", wt.DWORD),
        ("dwYSize", wt.DWORD),
        ("dwXCountChars", wt.DWORD),
        ("dwYCountChars", wt.DWORD),
        ("dwFillAttribute", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("wShowWindow", wt.WORD),
        ("cbReserved2", wt.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wt.HANDLE),
        ("hStdOutput", wt.HANDLE),
        ("hStdError", wt.HANDLE),
    ]


class _JOBOBJECT_ASSOCIATE_COMPLETION_PORT(ctypes.Structure):
    _fields_ = [("CompletionKey", ctypes.c_void_p), ("CompletionPort", wt.HANDLE)]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wt.HANDLE),
        ("hThread", wt.HANDLE),
        ("dwProcessId", wt.DWORD),
        ("dwThreadId", wt.DWORD),
    ]


def _kernel32():
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateIoCompletionPort.argtypes = [wt.HANDLE, wt.HANDLE, ctypes.c_size_t, wt.DWORD]
    api.CreateIoCompletionPort.restype = wt.HANDLE
    api.GetQueuedCompletionStatus.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD),
        ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_void_p), wt.DWORD]
    api.GetQueuedCompletionStatus.restype = wt.BOOL
    api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
    api.CreateJobObjectW.restype = wt.HANDLE
    api.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
    api.SetInformationJobObject.restype = wt.BOOL
    api.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
    api.AssignProcessToJobObject.restype = wt.BOOL
    api.TerminateJobObject.argtypes = [wt.HANDLE, wt.UINT]
    api.TerminateJobObject.restype = wt.BOOL
    api.QueryInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                              wt.DWORD, ctypes.POINTER(wt.DWORD)]
    api.QueryInformationJobObject.restype = wt.BOOL
    api.CreateProcessW.argtypes = [wt.LPCWSTR, wt.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
                                   wt.BOOL, wt.DWORD, ctypes.c_void_p, wt.LPCWSTR,
                                   ctypes.POINTER(_STARTUPINFOW),
                                   ctypes.POINTER(_PROCESS_INFORMATION)]
    api.CreateProcessW.restype = wt.BOOL
    api.ResumeThread.argtypes = [wt.HANDLE]
    api.ResumeThread.restype = wt.DWORD
    api.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    api.WaitForSingleObject.restype = wt.DWORD
    api.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
    api.GetExitCodeProcess.restype = wt.BOOL
    api.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]
    api.TerminateProcess.restype = wt.BOOL
    api.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    api.OpenProcess.restype = wt.HANDLE
    api.CloseHandle.argtypes = [wt.HANDLE]
    api.CloseHandle.restype = wt.BOOL
    return api


def _close(handle):
    if handle:
        try:
            _kernel32().CloseHandle(handle)
        except OSError:
            pass


def _create_job_object(memory_bytes):
    api = _kernel32()
    handle = api.CreateJobObjectW(None, None)
    if not handle:
        return None
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = (
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_ACTIVE_PROCESS |
        JOB_OBJECT_LIMIT_JOB_MEMORY)
    info.BasicLimitInformation.ActiveProcessLimit = MAX_ACTIVE_PROCESSES
    info.JobMemoryLimit = memory_bytes
    if not api.SetInformationJobObject(handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                       ctypes.byref(info), ctypes.sizeof(info)):
        api.CloseHandle(handle)
        return None
    # Read back the kernel limit before any child can be resumed. This is
    # job-wide committed memory, not RSS or a per-process working-set target.
    actual = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    if (not api.QueryInformationJobObject(handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(actual), ctypes.sizeof(actual), None)
            or not actual.BasicLimitInformation.LimitFlags & JOB_OBJECT_LIMIT_JOB_MEMORY
            or actual.JobMemoryLimit != memory_bytes):
        api.CloseHandle(handle)
        return None
    return handle


def _memory_completion_port(job):
    api = _kernel32()
    port = api.CreateIoCompletionPort(wt.HANDLE(-1), None, 0, 1)
    if not port:
        raise WorkerBlocked("job_completion_port_failed")
    info = _JOBOBJECT_ASSOCIATE_COMPLETION_PORT(1, port)
    if not api.SetInformationJobObject(job, JOB_OBJECT_ASSOCIATE_COMPLETION_PORT_INFORMATION,
                                       ctypes.byref(info), ctypes.sizeof(info)):
        _close(port)
        raise WorkerBlocked("job_completion_port_failed")
    return port


def _memory_limit_message(port):
    # Peak accounting alone does not distinguish a rejected allocation.
    # Consume the kernel event instead of mistaking the resulting crash for a
    # successful run. Windows does not guarantee delivery of every job message;
    # a missing message remains a crash/deadline, never a memory success claim.
    api = _kernel32()
    for _ in range(256):
        message, key, overlapped = wt.DWORD(), ctypes.c_size_t(), ctypes.c_void_p()
        if not api.GetQueuedCompletionStatus(port, ctypes.byref(message), ctypes.byref(key),
                                             ctypes.byref(overlapped), 0):
            if ctypes.get_last_error() == WAIT_TIMEOUT:
                return False
            raise WorkerBlocked("job_completion_read_failed")
        if message.value == JOB_OBJECT_MSG_JOB_MEMORY_LIMIT:
            return True
    return False


def _assign_process_to_job(job_handle, process_handle):
    return bool(_kernel32().AssignProcessToJobObject(job_handle, process_handle))


def _terminate_job_object(job_handle):
    return bool(_kernel32().TerminateJobObject(job_handle, 1))


def _terminate_process(process_handle):
    return bool(_kernel32().TerminateProcess(process_handle, 1))


def _resume_thread(thread_handle):
    return _kernel32().ResumeThread(thread_handle) != 0xFFFFFFFF


def _wait_process(process_handle, milliseconds):
    return _kernel32().WaitForSingleObject(process_handle, int(milliseconds)) == WAIT_OBJECT_0


def _exit_code(process_handle):
    code = wt.DWORD()
    if not _kernel32().GetExitCodeProcess(process_handle, ctypes.byref(code)):
        return None
    return None if code.value == STILL_ACTIVE else int(code.value)


def _job_peak_memory(job_handle):
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    if not _kernel32().QueryInformationJobObject(job_handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                                 ctypes.byref(info), ctypes.sizeof(info), None):
        return 0
    return int(info.PeakJobMemoryUsed)


def _job_active_processes(job_handle):
    info = _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
    if not _kernel32().QueryInformationJobObject(job_handle,
                                                 JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
                                                 ctypes.byref(info), ctypes.sizeof(info), None):
        return MAX_ACTIVE_PROCESSES
    return int(info.ActiveProcesses)


def _job_process_ids(job_handle):
    size = 8 + 8 * 128
    buffer = ctypes.create_string_buffer(size)
    needed = wt.DWORD()
    if not _kernel32().QueryInformationJobObject(job_handle, JOB_OBJECT_BASIC_PROCESS_ID_LIST,
                                                 buffer, size, ctypes.byref(needed)):
        return []
    count = ctypes.cast(ctypes.addressof(buffer) + 4,
                        ctypes.POINTER(wt.DWORD)).contents.value
    count = min(int(count), 128)
    if count <= 0:
        return []
    values = (ctypes.c_size_t * count).from_address(ctypes.addressof(buffer) + 8)
    return [int(value) for value in values]


def _environment_block(environment):
    if environment is None:
        return None
    items = [f"{key}={value}" for key, value in environment.items()]
    return ctypes.create_unicode_buffer("\0".join(items) + "\0\0")


def _spawn_suspended(argv, cwd, environment):
    command = subprocess.list2cmdline([str(item) for item in argv])
    buffer = ctypes.create_unicode_buffer(command)
    startup = _STARTUPINFOW()
    startup.cb = ctypes.sizeof(_STARTUPINFOW)
    process = _PROCESS_INFORMATION()
    block = _environment_block(environment)
    pointer = ctypes.cast(block, ctypes.c_void_p) if block is not None else None
    created = _kernel32().CreateProcessW(
        None, buffer, None, None, False,
        CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP |
        (CREATE_UNICODE_ENVIRONMENT if environment is not None else 0),
        pointer, str(cwd), ctypes.byref(startup), ctypes.byref(process))
    if not created:
        raise WorkerBlocked("process_start_failed")
    return {"handle": process.hProcess, "thread": process.hThread,
            "pid": int(process.dwProcessId)}


def _wait_tree_reclaimed(job_handle, process_handle, timeout_ms=5000):
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if _job_active_processes(job_handle) == 0 and _wait_process(process_handle, 0):
            return True
        time.sleep(0.02)
    return _job_active_processes(job_handle) == 0 and _wait_process(process_handle, 0)


def _tree_bytes(root):
    total = 0
    try:
        for path in Path(root).rglob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


def minimal_environment(*, extra=None):
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR",
               "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "PATHEXT"}
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() in allowed and value}
    environment["PYTHONPATH"] = str(SOURCE_ROOT)
    for key, value in (extra or {}).items():
        if type(key) is not str or type(value) is not str or not key or "\x00" in key + value:
            raise ValueError("invalid environment override")
        if key.upper() in {"PYTHONPATH", "PATH", "SYSTEMROOT"}:
            raise ValueError("protected environment variable")
        environment[key] = value
    return environment


def _blocked(reason, **extra):
    result = {"status": "blocked", "exit_code": None, "pid": None, "elapsed_ms": 0,
              "peak_memory_bytes": 0, "output_bytes": 0, "process_tree_reclaimed": False,
              "job_processes": [], "termination_reason": None,
              "blockers": [reason]}
    result.update(extra)
    return result


def run_worker(*, argv, cwd=None, deadline_ms, memory_bytes, output_bytes_limit=None,
               output_root=None, cancel_event=None, poll_ms=25, env=None):
    """Supervise one freshly created process tree with a hard deadline."""
    started = time.monotonic()
    if (type(argv) not in (list, tuple) or not argv or
            not all(type(item) is str and item for item in argv)):
        return _blocked("invalid_argv")
    if type(deadline_ms) is not int or deadline_ms <= 0:
        return _blocked("invalid_deadline")
    if type(memory_bytes) is not int or memory_bytes <= 0:
        return _blocked("invalid_memory_budget")
    if type(poll_ms) is not int or not 5 <= poll_ms <= 1000:
        return _blocked("invalid_poll_interval")
    if output_bytes_limit is not None:
        if type(output_bytes_limit) is not int or output_bytes_limit < 0:
            return _blocked("invalid_output_budget")
        if output_root is None or not Path(output_root).is_dir():
            return _blocked("output_root_invalid")
    cwd = Path(cwd or SOURCE_ROOT)
    if not cwd.is_dir():
        return _blocked("working_directory_invalid")
    job = None
    port = None
    process = None
    try:
        job = _create_job_object(memory_bytes)
        if not job:
            return _blocked("job_creation_failed")
        port = _memory_completion_port(job)
        process = _spawn_suspended(argv, cwd, env)
        if not _assign_process_to_job(job, process["handle"]):
            _terminate_process(process["handle"])
            _wait_process(process["handle"], 2000)
            return _blocked("job_assignment_failed", pid=process["pid"])
        if not _resume_thread(process["thread"]):
            _terminate_job_object(job)
            return _blocked("process_resume_failed", pid=process["pid"])
        _close(process["thread"])
        process["thread"] = None
        observed = {process["pid"]}
        peak = 0
        reason = None
        while True:
            exited = _wait_process(process["handle"], poll_ms)
            peak = max(peak, _job_peak_memory(job))
            try:
                observed.update(_job_process_ids(job))
            except OSError:
                pass
            if output_bytes_limit is not None:
                if _tree_bytes(output_root) > output_bytes_limit:
                    reason = "output_budget_exceeded"
                    break
            if _memory_limit_message(port) or peak > memory_bytes:
                reason = "memory_exceeded"
                break
            if cancel_event is not None and cancel_event.is_set():
                reason = "cancelled"
                break
            if (time.monotonic() - started) * 1000 > deadline_ms:
                reason = "deadline_exceeded"
                break
            if exited:
                break
        blockers = []
        if reason is not None:
            if not _terminate_job_object(job):
                blockers.append("job_termination_failed")
                _terminate_process(process["handle"])
        else:
            exit_code = _exit_code(process["handle"])
            peak = max(peak, _job_peak_memory(job))
            _terminate_job_object(job)
        if not _wait_tree_reclaimed(job, process["handle"]):
            blockers.append("process_tree_not_reclaimed")
        if reason is not None:
            status = "blocked" if blockers else reason
        else:
            status = "crashed" if exit_code != 0 else "completed"
            if peak > memory_bytes:
                status = "memory_exceeded"
            if blockers:
                status = "blocked"
        elapsed = int((time.monotonic() - started) * 1000)
        output_bytes = _tree_bytes(output_root) if output_root is not None else 0
        return {"status": status, "exit_code": None if reason is not None else exit_code,
                "pid": process["pid"], "elapsed_ms": elapsed,
                "peak_memory_bytes": peak, "output_bytes": output_bytes,
                "process_tree_reclaimed": "process_tree_not_reclaimed" not in blockers,
                "job_processes": sorted(observed), "termination_reason": reason,
                "blockers": blockers}
    except WorkerBlocked as exc:
        if process is not None:
            _terminate_job_object(job)
            reclaimed = _wait_tree_reclaimed(job, process["handle"])
            return _blocked(str(exc), pid=process["pid"], process_tree_reclaimed=reclaimed)
        return _blocked(str(exc))
    finally:
        if process is not None:
            _close(process.get("thread"))
            _close(process.get("handle"))
        if job:
            _close(job)
        if port:
            _close(port)


def _identity_digest(payload):
    value = json_value(payload if payload is not None else {})
    return {"input_digest": digest(value), "worker_version": "controlled_research_worker.v1",
            "model_calls": 0}


MODEL_CALLBACK_REGISTRY = {"identity_digest": _identity_digest}


def _atomic_write_result(path, data):
    target = Path(path)
    temporary = target.parent / f".tmp-{uuid.uuid4().hex[:8]}"
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="controlled_research_worker")
    parser.add_argument("--request", required=True)
    parser.add_argument("--result", required=True)
    arguments = parser.parse_args(argv)
    try:
        raw = Path(arguments.request).read_bytes()
        if not raw or len(raw) > MAX_REQUEST_BYTES:
            return 2
        request = bounded_json(json.loads(raw.decode("utf-8")))
        if type(request) is not dict or request.get("schema") != WORKER_SCHEMA:
            return 2
        handler = request.get("handler")
        if type(handler) is not str or handler not in MODEL_CALLBACK_REGISTRY:
            return 2
        result = MODEL_CALLBACK_REGISTRY[handler](request.get("payload"))
        record = {"schema": RESULT_SCHEMA, "handler": handler, "result": json_value(result),
                  "request_digest": digest(request), "model_calls": 0}
        _atomic_write_result(arguments.result,
                             json.dumps(record, ensure_ascii=False, sort_keys=True,
                                        separators=(",", ":"), allow_nan=False).encode("utf-8"))
        return 0
    except (OSError, ValueError, TypeError):
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["run_worker", "main", "minimal_environment", "WORKER_SCHEMA", "RESULT_SCHEMA",
           "MODEL_CALLBACK_REGISTRY", "WorkerBlocked"]
