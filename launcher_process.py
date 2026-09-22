"""启动器拥有的 map_server 进程生命周期（BUG-STARTUP-001）。

本模块只服务本仓库的启动器 ``map_app.py``，它解决一个具体的 Windows 事实：

* 后端由 ``python-qgis*.bat -m cns_planner.map_server`` 启动，真实结构是
  ``cmd.exe``（运行 .bat）→ ``cmd.exe /c`` → ``python.exe``（监听 8765）；
* 启动器 ``Popen`` 拿到的是最外层 wrapper 的句柄，对它调用 ``terminate()``
  只会结束 wrapper，真正监听 8765 的 python 进程会变成孤儿继续存活；
* 启动窗口被直接关闭（CTRL_CLOSE_EVENT）时，解释器可能来不及执行任何
  ``finally``，因此"退出时主动清理"这类纯 Python 方案先天不可靠。

采用的最小可靠方案是 Windows 内核的 **Job Object**：
启动器创建 Job 并设置 ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``，随后把 wrapper
进程（连同它后续创建的全部子进程）放进该 Job。Job 句柄只由启动器持有，
因此无论启动器是正常退出、Ctrl+C、被强杀还是窗口被关闭，系统都会回收句柄，
Job 内的整棵进程树被内核终止，8765 必然释放。

约束（与本 BUG 的验收条件一致）：
* 只管理本启动器自己创建的进程：一切动作都经由 ``subprocess.Popen`` 返回的对象
  和该进程被放入的 Job，绝不按进程名扫描或批量结束 python 进程；
* 不新增第三方依赖：只用标准库 ``ctypes`` 调用 kernel32；
* 非 Windows 平台退回 POSIX 进程组方案，语义相同（只结束自己创建的会话）。
"""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import time

IS_WINDOWS = os.name == "nt"

# Windows 创建标志：挂起创建避免"分配 Job 之前子进程已经开始工作"的竞态。
CREATE_SUSPENDED = 0x00000004
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200

# Job Object 常量
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_THREAD_SUSPEND_RESUME = 0x0002
_TH32CS_SNAPTHREAD = 0x00000004
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


def _debug(message):
    """仅在显式打开调试开关时输出，避免污染正常启动流程。"""
    if os.environ.get("CNS_LAUNCHER_DEBUG") == "1":
        print(f"[launcher] {message}", flush=True)


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_void_p),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _ThreadEntry32(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ThreadID", ctypes.c_uint32),
        ("th32OwnerProcessID", ctypes.c_uint32),
        ("tpBasePri", ctypes.c_long),
        ("tpDeltaPri", ctypes.c_long),
        ("dwFlags", ctypes.c_uint32),
    ]


class _JobBasicAccounting(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", ctypes.c_uint32),
        ("TotalProcesses", ctypes.c_uint32),
        ("ActiveProcesses", ctypes.c_uint32),
        ("TotalTerminatedProcesses", ctypes.c_uint32),
    ]


def _kernel32():
    library = ctypes.WinDLL("kernel32", use_last_error=True)
    library.CreateJobObjectW.restype = ctypes.c_void_p
    library.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    library.SetInformationJobObject.restype = ctypes.c_int
    library.SetInformationJobObject.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
    ]
    library.OpenProcess.restype = ctypes.c_void_p
    library.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    library.AssignProcessToJobObject.restype = ctypes.c_int
    library.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    library.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    library.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    library.Thread32First.restype = ctypes.c_int
    library.Thread32First.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    library.Thread32Next.restype = ctypes.c_int
    library.Thread32Next.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    library.OpenThread.restype = ctypes.c_void_p
    library.OpenThread.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    library.ResumeThread.restype = ctypes.c_uint32
    library.ResumeThread.argtypes = [ctypes.c_void_p]
    library.CloseHandle.restype = ctypes.c_int
    library.CloseHandle.argtypes = [ctypes.c_void_p]
    library.QueryInformationJobObject.restype = ctypes.c_int
    library.QueryInformationJobObject.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    return library


def create_kill_on_close_job():
    """创建 KILL_ON_JOB_CLOSE 的 Job，返回句柄；平台不支持时返回 None。"""
    if not IS_WINDOWS:
        return None
    library = _kernel32()
    job = library.CreateJobObjectW(None, None)
    if not job:
        _debug(f"CreateJobObject 失败：{ctypes.get_last_error()}")
        return None
    limits = _ExtendedLimitInformation()
    limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not library.SetInformationJobObject(
        ctypes.c_void_p(job), _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(limits), ctypes.sizeof(limits),
    ):
        _debug(f"SetInformationJobObject 失败：{ctypes.get_last_error()}")
        library.CloseHandle(ctypes.c_void_p(job))
        return None
    return job


def assign_to_job(job, pid):
    """把指定 pid 放进 Job。

    返回值：(是否成功, 错误码)。错误码 0 表示成功；Windows 上
    ``AssignProcessToJobObject`` 在失败时会设置 last error。
    """
    if not IS_WINDOWS or not job:
        return False, None
    library = _kernel32()
    handle = library.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
    if not handle:
        return False, ctypes.get_last_error()
    try:
        result = library.AssignProcessToJobObject(ctypes.c_void_p(job), ctypes.c_void_p(handle))
        return bool(result), (0 if result else ctypes.get_last_error())
    finally:
        library.CloseHandle(ctypes.c_void_p(handle))


def process_thread_ids(pid):
    """返回该 pid 当前的线程 ID 列表（只用于恢复我们自己挂起创建的进程）。"""
    library = _kernel32()
    snapshot = library.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
    if snapshot in (None, 0, _INVALID_HANDLE_VALUE):
        return []
    entry = _ThreadEntry32()
    entry.dwSize = ctypes.sizeof(_ThreadEntry32)
    threads = []
    try:
        if not library.Thread32First(ctypes.c_void_p(snapshot), ctypes.byref(entry)):
            return []
        while True:
            if entry.th32OwnerProcessID == pid:
                threads.append(int(entry.th32ThreadID))
            if not library.Thread32Next(ctypes.c_void_p(snapshot), ctypes.byref(entry)):
                break
    finally:
        library.CloseHandle(ctypes.c_void_p(snapshot))
    return threads


def resume_process(pid):
    """恢复挂起创建的进程；返回成功恢复的线程数。"""
    if not IS_WINDOWS:
        return 0
    library = _kernel32()
    resumed = 0
    for thread_id in process_thread_ids(pid):
        handle = library.OpenThread(_THREAD_SUSPEND_RESUME, False, thread_id)
        if not handle:
            continue
        try:
            if library.ResumeThread(ctypes.c_void_p(handle)) != 0xFFFFFFFF:
                resumed += 1
        finally:
            library.CloseHandle(ctypes.c_void_p(handle))
    return resumed


def wrapper_command(script, arguments):
    """把 ``*.bat`` 展开成显式的 ``cmd.exe /c`` 命令行。

    显式给出解释器是为了让"挂起创建 wrapper，放入 Job，再恢复"这条路径完全可控，
    同时保留原先 ``python-qgis*.bat -m cns_planner.map_server`` 的语义不变。
    """
    script = str(script)
    if os.name != "nt" or not script.lower().endswith((".bat", ".cmd")):
        return [script, *arguments]
    interpreter = os.environ.get("COMSPEC") or r"C:\Windows\System32\cmd.exe"
    return [interpreter, "/c", script, *arguments]


class ProcessTree:
    """本启动器拥有的进程树：唯一创建者、唯一清理者。

    生命周期规则：
    * 只在 ``launch()`` 里创建进程，且创建后立即放进自己的 Job；
    * ``close()`` 只针对自己创建的那个进程树，绝不按名字查找进程；
    * Job 句柄关闭 = 内核终止整棵树，所以即使没有机会执行 ``close()``
      （进程被强杀、窗口被关闭），8765 也不会残留占用。
    """

    def __init__(self):
        self.process = None
        self.job = None
        self._closed = False

    # ------------------------------------------------------------------ 启动

    def launch(self, command, cwd, env, stdout, suspend=False):
        """创建本树唯一的进程。

        ``suspend`` 只影响 Windows 的实现细节（先挂起创建、放入 Job、再恢复），
        生产路径与测试都走同一个入口，避免出现"测过的路径不是跑的路径"。
        """
        if IS_WINDOWS:
            return self._launch_windows(command, cwd, env, stdout, suspend)
        return self._launch_posix(command, cwd, env, stdout)

    def _launch_windows(self, command, cwd, env, stdout, suspend=False):
        self.job = create_kill_on_close_job()
        flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        if suspend and self.job:
            # 先挂起创建：此刻 wrapper 还没执行任何指令，把它放进 Job 不存在竞态，
            # 之后它派生出的 cmd.exe / python.exe 全部自动留在同一个 Job 内。
            flags |= CREATE_SUSPENDED
        elif not suspend:
            _debug("未使用挂起创建（测试或调用方显式关闭）")
        else:
            _debug("未能创建 Job，退回按句柄终止（仅能结束 wrapper 进程）")
        self.process = subprocess.Popen(
            command, cwd=str(cwd), env=env, stdout=stdout, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, creationflags=flags,
        )
        if self.job:
            assigned, error = assign_to_job(self.job, self.process.pid)
            if not assigned:
                # 极端环境（父进程已在不可嵌套的 Job 中）不允许接管时，
                # 主动放弃 Job，避免留下"以为被托管、实际没有"的假象。
                _debug(f"AssignProcessToJobObject 失败（错误码 {error}），退回按句柄终止")
                library = _kernel32()
                library.CloseHandle(ctypes.c_void_p(self.job))
                self.job = None
            elif suspend and resume_process(self.process.pid) == 0:
                raise RuntimeError("无法恢复地图服务进程（挂起状态未被解除）")
        return self

    def _launch_posix(self, command, cwd, env, stdout):
        self.process = subprocess.Popen(
            command, cwd=str(cwd), env=env, stdout=stdout, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
        return self

    # ------------------------------------------------------------------ 状态

    @property
    def pid(self):
        return self.process.pid if self.process else None

    @property
    def managed(self):
        """是否由 Job（或 POSIX 会话）整体托管。"""
        return bool(self.job) or (self.process is not None and not IS_WINDOWS)

    def poll(self):
        return self.process.poll() if self.process else None

    def wait(self):
        return self.process.wait() if self.process else 0

    # ------------------------------------------------------------------ 清理

    def terminate(self):
        """只结束本启动器创建的这棵树。"""
        if not self.process:
            return
        if self.job:
            library = _kernel32()
            library.CloseHandle(ctypes.c_void_p(self.job))
            self.job = None
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _debug("Job 关闭后 wrapper 仍在运行（Job 内进程应由内核终止）")
            return
        if not IS_WINDOWS:
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.terminate()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False


def release_job_handle(job):
    """测试辅助：关闭 Job 句柄（等价于启动器退出时内核回收句柄）。"""
    if not job or not IS_WINDOWS:
        return
    _kernel32().CloseHandle(ctypes.c_void_p(job))


def job_active_processes(job):
    """Job 内当前仍存活的进程数；查询失败返回 None（不结束任何进程）。"""
    if not job or not IS_WINDOWS:
        return None
    accounting = _JobBasicAccounting()
    ok = _kernel32().QueryInformationJobObject(
        ctypes.c_void_p(job), 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None,
    )
    return int(accounting.ActiveProcesses) if ok else None


def wait_for_exit(process, timeout=10.0):
    """轮询等待进程退出，返回是否已退出；不结束任何进程。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            return True
        time.sleep(0.05)
    return process is not None and process.poll() is not None
