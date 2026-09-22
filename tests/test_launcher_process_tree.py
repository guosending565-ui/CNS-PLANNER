"""进程树生命周期测试（BUG-STARTUP-001）：只结束自己拥有的树。

Windows 上这里是真进程测试，但不需要 QGIS：用 cmd.exe 模拟
``python-qgis*.bat`` 那层 wrapper，再用普通 python.exe 模拟真正的 map_server。
"""
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

import launcher_process

pytestmark = pytest.mark.skipif(
    not launcher_process.IS_WINDOWS, reason="Windows Job Object 行为"
)

CHILD = Path(__file__).with_name("_launcher_tree_child.py")


def wait_for(path, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


def alive(pid):
    try:
        return bool(subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"if (Get-Process -Id {pid} -ErrorAction SilentlyContinue) {{ 'yes' }}"],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


def wait_dead(pid, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.3)
    return not alive(pid)


def spawn_child_via_wrapper(tmp_path, seq):
    signal_file = tmp_path / f"tree{seq}.ready"
    pid_file = tmp_path / f"tree{seq}.pid"
    command = [
        os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"), "/c", sys.executable,
        str(CHILD), str(signal_file), str(pid_file),
    ]
    tree = launcher_process.ProcessTree().launch(
        command, cwd=tmp_path, env=os.environ.copy(),
        stdout=subprocess.DEVNULL, suspend=True,
    )
    assert wait_for(signal_file), "受托管的子进程没有启动"
    child_pid = int(pid_file.read_text().strip())
    return tree, child_pid, signal_file


def test_job_close_ends_the_whole_tree(tmp_path):
    """启动器被强杀（只剩内核回收 Job 句柄）时，wrapper 与后端一起消失。"""
    tree, child_pid, _ = spawn_child_via_wrapper(tmp_path, "a")
    assert alive(child_pid)
    assert tree.managed
    launcher_process.release_job_handle(tree.job)
    tree.job = None
    assert wait_dead(child_pid), "关闭 Job 句柄后子进程仍然存活"


def test_tree_close_ends_the_whole_tree(tmp_path):
    """正常关闭/异常关闭路径同样结束整棵树。"""
    tree, child_pid, _ = spawn_child_via_wrapper(tmp_path, "b")
    assert alive(child_pid)
    tree.close()
    assert not alive(tree.pid)
    assert wait_dead(child_pid)


def test_unmanaged_close_does_not_touch_other_processes(tmp_path):
    """没有 Job 退路时只结束自己的 wrapper，绝不波及其他进程。"""
    victim = subprocess.Popen(
        [sys.executable, str(CHILD), str(tmp_path / "victim.ready"), str(tmp_path / "victim.pid")]
    )
    try:
        assert victim.poll() is None
        command = [
            os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"), "/c", sys.executable,
            str(CHILD), str(tmp_path / "free.ready"), str(tmp_path / "free.pid"),
        ]
        tree = launcher_process.ProcessTree().launch(
            command, cwd=tmp_path, env=os.environ.copy(),
            stdout=subprocess.DEVNULL, suspend=False,
        )
        assert tree.job is not None  # 仍然被托管；只是跳过了挂起创建
        tree.close()
        assert victim.poll() is None, "清理自己的进程树时不得影响无关进程"
    finally:
        victim.kill()
        victim.wait(timeout=30)


def test_suspended_launch_resumes_and_runs(tmp_path):
    signal_file = tmp_path / "resume.ready"
    pid_file = tmp_path / "resume.pid"
    tree = launcher_process.ProcessTree().launch(
        [sys.executable, str(CHILD), str(signal_file), str(pid_file)],
        cwd=tmp_path, env=os.environ.copy(),
        stdout=subprocess.DEVNULL, suspend=True,
    )
    try:
        assert wait_for(signal_file), "挂起创建的进程没有被恢复"
    finally:
        tree.close()


def test_assign_to_job_reports_success_and_enrolls_process(tmp_path):
    """assign 的契约：只有真正入 Job 才返回 True/0，且该进程计入 Job。

    注意：被分配的必须是**独立**进程；把 pytest 自身放进 KILL_ON_JOB_CLOSE 的
    Job 后关闭句柄，会连测试进程一起结束。
    """
    signal_file, pid_file = tmp_path / "assign.ready", tmp_path / "assign.pid"
    victim = subprocess.Popen([sys.executable, str(CHILD), str(signal_file), str(pid_file)])
    job = launcher_process.create_kill_on_close_job()
    try:
        assert wait_for(signal_file), "测试子进程没有启动"
        assert job
        before = launcher_process.job_active_processes(job)
        assigned, error = launcher_process.assign_to_job(job, victim.pid)
        assert assigned is True and error == 0
        assert launcher_process.job_active_processes(job) == (before or 0) + 1
    finally:
        launcher_process.release_job_handle(job)
        if victim.poll() is None:
            victim.kill()
        victim.wait(timeout=30)


def test_wrapper_command_expands_batch_script():
    command = launcher_process.wrapper_command(
        Path("C:/Program Files/QGIS/bin/python-qgis.bat"), ["-m", "cns_planner.map_server"],
    )
    assert command[0].lower().endswith("cmd.exe")
    assert command[1] == "/c"
    assert command[2].replace("\\", "/").endswith("python-qgis.bat")
    assert command[-2:] == ["-m", "cns_planner.map_server"]


def test_wrapper_command_keeps_plain_executables():
    command = launcher_process.wrapper_command(
        Path("C:/python/python.exe"), ["-m", "cns_planner.map_server"],
    )
    assert len(command) == 3
    assert command[0].replace("\\", "/").endswith("python.exe")
    assert command[1:] == ["-m", "cns_planner.map_server"]
