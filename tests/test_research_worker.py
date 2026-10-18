"""R2.2-B: controlled Windows worker hard deadline and process-tree isolation.

Fixture only: every child here is a short-lived python process created by this
module. No real GIS/model call, no project state and no service restart.
"""
import ctypes
import ctypes.wintypes as wt
import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object required")


@pytest.fixture
def tmp_path():
    # Keep this module's controlled temporary paths short; do not touch conftest.
    import uuid
    root = Path(os.environ["TEMP"]) / ("r22w_" + uuid.uuid4().hex[:8])
    root.mkdir(parents=True)
    return root

STILL_ACTIVE = 259
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001


def _kernel32():
    return ctypes.WinDLL("kernel32", use_last_error=True)


def pid_alive(pid):
    k32 = _kernel32()
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        code = wt.DWORD()
        return bool(k32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def kill_pid(pid):
    k32 = _kernel32()
    handle = k32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
    if handle:
        k32.TerminateProcess(handle, 1)
        k32.CloseHandle(handle)


def api():
    name = "cns_planner.application.research_worker"
    assert importlib.util.find_spec(name) is not None, "R2.2 controlled worker not implemented"
    return importlib.import_module(name)


def source_root():
    return Path(api().__file__).resolve().parents[2]


def entry_path():
    return [sys.executable, str(source_root() / "cns_planner" / "application" / "research_worker.py")]


def write_script(tmp_path, name, code):
    path = tmp_path / f"{name}.py"
    path.write_text(code, encoding="utf-8")
    return path


def run_script(tmp_path, name, code, **kwargs):
    mod = api()
    path = write_script(tmp_path, name, code)
    options = dict(argv=[sys.executable, str(path)], cwd=str(tmp_path),
                   deadline_ms=5000, memory_bytes=256 * 1024 * 1024,
                   poll_ms=20)
    options.update(kwargs)
    return mod.run_worker(**options)


def test_worker_completes_and_reports_isolated_identity(tmp_path):
    done = tmp_path / "done.json"
    result = run_script(tmp_path, "ok", (
        "import json, os, sys, time\n"
        "json.dump({'pid': os.getpid()}, open(r'%s', 'w'))\n"
        "time.sleep(0.05)\n" % done))
    assert result["status"] == "completed" and result["exit_code"] == 0
    assert result["pid"] and pid_alive(result["pid"]) is False
    assert result["process_tree_reclaimed"] is True
    assert result["job_processes"] and result["pid"] in result["job_processes"]
    assert 0 <= result["elapsed_ms"] < 5000 and result["peak_memory_bytes"] > 0
    assert result["blockers"] == []


def test_hung_worker_is_hard_deadline_terminated(tmp_path):
    result = run_script(tmp_path, "hang", "import time\ntime.sleep(60)\n", deadline_ms=800)
    assert result["status"] == "deadline_exceeded"
    assert result["termination_reason"] == "deadline_exceeded"
    assert result["elapsed_ms"] < 8000
    assert result["process_tree_reclaimed"] is True
    assert pid_alive(result["pid"]) is False


def test_deadline_reclaims_grandchild_process_tree(tmp_path):
    log = tmp_path / "pids.json"
    code = (
        "import json, os, subprocess, sys, time\n"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "json.dump({'child': os.getpid(), 'grand': g.pid}, open(r'%s', 'w'))\n"
        "time.sleep(60)\n" % log)
    result = run_script(tmp_path, "tree", code, deadline_ms=1600)
    assert result["status"] == "deadline_exceeded"
    pids = json.loads(log.read_text(encoding="utf-8"))
    assert pids["child"] in result["job_processes"]
    assert pids["grand"] in result["job_processes"]
    assert pid_alive(pids["child"]) is False and pid_alive(pids["grand"]) is False
    assert result["process_tree_reclaimed"] is True


def test_cancel_event_terminates_worker(tmp_path):
    event = threading.Event()
    threading.Timer(0.5, event.set).start()
    result = run_script(tmp_path, "cancel", "import time\ntime.sleep(30)\n",
                        deadline_ms=20000, cancel_event=event)
    assert result["status"] == "cancelled"
    assert result["termination_reason"] == "cancelled"
    assert result["elapsed_ms"] < 8000 and pid_alive(result["pid"]) is False


def test_crash_reports_exit_code_and_never_completes(tmp_path):
    result = run_script(tmp_path, "crash", "import os\nos._exit(7)\n")
    assert result["status"] == "crashed" and result["exit_code"] == 7
    assert result["process_tree_reclaimed"] is True


def test_memory_budget_exceeded_terminates_and_reclaims(tmp_path):
    code = (
        "import time\n"
        "chunks = []\n"
        "while True:\n"
        "    chunks.append(bytearray(4 * 1024 * 1024))\n"
        "    time.sleep(0.02)\n")
    result = run_script(tmp_path, "mem", code, deadline_ms=20000,
                        memory_bytes=96 * 1024 * 1024)
    assert result["status"] == "memory_exceeded"
    assert result["peak_memory_bytes"] >= 96 * 1024 * 1024
    assert result["process_tree_reclaimed"] is True and pid_alive(result["pid"]) is False


def test_output_budget_exceeded_terminates_and_reclaims(tmp_path):
    out = tmp_path / "worker_out"
    out.mkdir()
    mod = api()
    env = mod.minimal_environment(extra={"OUT_DIR": str(out)})
    code = (
        "import os, time\n"
        "open(os.path.join(os.environ['OUT_DIR'], 'big.bin'), 'wb').write(b'x' * (2 * 1024 * 1024))\n"
        "time.sleep(30)\n")
    result = run_script(tmp_path, "out", code, deadline_ms=20000,
                        output_root=str(out), output_bytes_limit=256 * 1024,
                        env=env)
    assert result["status"] == "output_budget_exceeded"
    assert result["output_bytes"] >= 2 * 1024 * 1024
    assert pid_alive(result["pid"]) is False


def test_process_start_failure_is_blocked(tmp_path):
    mod = api()
    result = mod.run_worker(argv=[str(tmp_path / "no-such-python.exe")], cwd=str(tmp_path),
                            deadline_ms=1000, memory_bytes=64 * 1024 * 1024)
    assert result["status"] == "blocked"
    assert "process_start_failed" in result["blockers"]
    assert result["pid"] is None and result["exit_code"] is None


def test_job_assignment_failure_is_blocked_and_child_reclaimed(tmp_path, monkeypatch):
    mod = api()
    monkeypatch.setattr(mod, "_assign_process_to_job", lambda *a, **k: False)
    result = run_script(tmp_path, "assign", "import time\ntime.sleep(15)\n", deadline_ms=30000)
    assert result["status"] == "blocked"
    assert "job_assignment_failed" in result["blockers"]
    assert result["pid"] and pid_alive(result["pid"]) is False


def test_job_termination_failure_returns_blocked_without_claiming_success(tmp_path, monkeypatch):
    mod = api()
    monkeypatch.setattr(mod, "_terminate_job_object", lambda *a, **k: False)
    result = run_script(tmp_path, "term", "import time\ntime.sleep(10)\n", deadline_ms=400)
    assert result["status"] == "blocked"
    assert result["status"] != "completed"
    assert "job_termination_failed" in result["blockers"]
    assert pid_alive(result["pid"]) is False


def test_existing_unrelated_process_is_never_terminated(tmp_path):
    sentinel = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        result = run_script(tmp_path, "iso", "import time\ntime.sleep(30)\n", deadline_ms=600)
        assert result["status"] == "deadline_exceeded"
        assert pid_alive(result["pid"]) is False
        assert sentinel.poll() is None and pid_alive(sentinel.pid) is True
    finally:
        sentinel.kill()
        sentinel.wait(timeout=10)


def test_worker_source_has_no_name_based_termination():
    text = Path(api().__file__).read_text(encoding="utf-8")
    assert "taskkill" not in text and "psutil" not in text
    assert "TerminateJobObject" in text and "TerminateProcess" in text
    assert "/IM" not in text


def request_file(tmp_path, payload):
    path = tmp_path / "request.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_worker_entry_runs_only_registered_callback(tmp_path):
    mod = api()
    request = request_file(tmp_path, {"schema": mod.WORKER_SCHEMA,
                                      "handler": "identity_digest",
                                      "payload": {"action": "a0", "value": 1}})
    result = tmp_path / "result.json"
    proc = subprocess.run(entry_path() + [
        "--request", str(request), "--result", str(result)],
                          cwd=str(source_root()), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(result.read_text(encoding="utf-8"))
    assert data["schema"] == mod.RESULT_SCHEMA and data["handler"] == "identity_digest"
    assert len(data["result"]["input_digest"]) == 64 and data["result"]["model_calls"] == 0
    assert data["request_digest"] and [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []


@pytest.mark.parametrize("handler", [
    "os.system", "__import__('os').system('echo pwned')", "subprocess.Popen",
    "cns_planner.application.research_worker", "", None, ["identity_digest"]])
def test_worker_entry_rejects_callback_injection(tmp_path, handler):
    mod = api()
    request = request_file(tmp_path, {"schema": mod.WORKER_SCHEMA, "handler": handler,
                                      "payload": {"value": 1}})
    result = tmp_path / "result.json"
    proc = subprocess.run(entry_path() + [
        "--request", str(request), "--result", str(result)],
                          cwd=str(source_root()), capture_output=True, text=True)
    assert proc.returncode == 2 and not result.exists()


def test_worker_entry_rejects_malformed_or_executable_payload(tmp_path):
    mod = api()
    for payload in ({"schema": mod.WORKER_SCHEMA, "handler": "identity_digest",
                     "payload": {"runtime": {"module": "os"}}},
                    {"schema": mod.WORKER_SCHEMA, "handler": "identity_digest",
                     "payload": {"value": float("nan")}},
                    {"schema": "other", "handler": "identity_digest", "payload": {}}):
        request = request_file(tmp_path, payload)
        result = tmp_path / "result.json"
        if result.exists():
            result.unlink()
        proc = subprocess.run(entry_path() + [
            "--request", str(request), "--result", str(result)],
                              cwd=str(source_root()), capture_output=True, text=True)
        assert proc.returncode == 2 and not result.exists()
    big = tmp_path / "big.json"
    big.write_bytes(b"{" + b" " * (2 * 1024 * 1024 + 1) + b"}")
    proc = subprocess.run(entry_path() + [
        "--request", str(big), "--result", str(tmp_path / "result.json")],
                          cwd=str(source_root()), capture_output=True, text=True)
    assert proc.returncode == 2 and not (tmp_path / "result.json").exists()


def test_job_one_gib_limit_is_visible_before_child_computation(tmp_path):
    # The child queries its actual current Job, then asks Windows for a commit
    # larger than its remaining allowance. No >1 GiB allocation is touched.
    record = tmp_path / "job.json"
    code = (
        "import ctypes, json, time\n"
        "from ctypes import wintypes as wt\n"
        "import runpy\n"
        "_JOBOBJECT_EXTENDED_LIMIT_INFORMATION = runpy.run_path(%r)['_JOBOBJECT_EXTENDED_LIMIT_INFORMATION']\n" % str(source_root() / "cns_planner/application/research_worker.py") +
        "k = ctypes.WinDLL('kernel32', use_last_error=True)\n"
        "k.QueryInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p]\n"
        "k.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wt.DWORD, wt.DWORD]\n"
        "k.VirtualAlloc.restype = ctypes.c_void_p\n"
        "k.VirtualFree.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wt.DWORD]\n"
        "i = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()\n"
        "assert k.QueryInformationJobObject(None, 9, ctypes.byref(i), ctypes.sizeof(i), None)\n"
        "p = k.VirtualAlloc(None, 1073741824, 0x3000, 4)\n"
        "denied = not bool(p)\n"
        "if p: k.VirtualFree(p, 0, 0x8000)\n"
        "json.dump({'flags': int(i.BasicLimitInformation.LimitFlags), 'limit': int(i.JobMemoryLimit), 'denied': denied}, open(%r, 'w'))\n"
        "time.sleep(0.1)\n" % str(record))
    result = run_script(tmp_path, "job_limit", code, memory_bytes=1073741824,
                        env=api().minimal_environment())
    observed = json.loads(record.read_text('utf-8'))
    assert observed['flags'] & 0x200
    assert observed['limit'] == 1073741824
    assert observed['denied'] is True
    assert result['status'] == 'memory_exceeded'
    assert result['termination_reason'] == 'memory_exceeded'
    assert result['process_tree_reclaimed'] is True
    # Keep raw peak accounting without claiming it is RSS or that it proves
    # allocation success. VirtualAlloc failure above is observed separately.
    assert result['peak_memory_bytes'] > 0
    print(json.dumps({'job_readback': observed, 'worker': result}, sort_keys=True))


def test_memory_limit_is_aggregate_and_reclaims_allocating_children(tmp_path):
    code = (
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; x=bytearray(64*1024*1024); time.sleep(30)'])\n"
        "time.sleep(0.3)\n"
        "x=bytearray(64*1024*1024)\n"
        "time.sleep(30)\n")
    result = run_script(tmp_path, "aggregate_mem", code, deadline_ms=5000,
                        memory_bytes=96 * 1024 * 1024)
    assert result["status"] == "memory_exceeded"
    assert len(result["job_processes"]) >= 2
    assert result["process_tree_reclaimed"] is True
    assert all(not pid_alive(pid) for pid in result["job_processes"])
