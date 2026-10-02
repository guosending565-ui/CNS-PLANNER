"""受控重启本地 CNS 地图后端（开发期验收用）。

用途：把 8765 上**本项目**的后端换成当前 HEAD 的代码。

行为（与 ``map_app.py`` 的 CASE 语义一致，但允许"同 HEAD 也要重启"）：

1. 读 ``/api/health`` 确认对方是**本项目**的 ``cns-map`` 服务（service/project_root 匹配）；
2. 只结束 health 明确指认的那一个 pid（不做按名字杀进程）；
3. 等 8765 真正释放；
4. 用 ``tools/launch_map_server.py`` 启动当前 HEAD，并等 health 报出当前身份。

身份不明确（service 不是 cns-map、project_root 不是本仓库）时 fail-closed，直接报错退出。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
URL = "http://127.0.0.1:8765"
SERVICE = "cns-map"
QGIS_RUNNER = Path(
    os.environ.get("CNS_QGIS_PYTHON")
    or r"C:\Program Files\QGIS 3.44.14\bin\python-qgis-ltr.bat"
)
LOG = ROOT / "outputs" / "map-server.log"
STOP_TIMEOUT_S = 20.0
READY_TIMEOUT_S = 180.0


def health():
    try:
        with urlopen(URL + "/api/health", timeout=3) as response:
            return json.load(response)
    except (OSError, URLError, ValueError):
        return None


def normalized(value):
    try:
        return os.path.normcase(os.path.normpath(str(Path(str(value)).resolve())))
    except (OSError, ValueError):
        return os.path.normcase(os.path.normpath(str(value)))


def current_pid():
    payload = health()
    if not isinstance(payload, dict):
        return None
    if payload.get("service") != SERVICE:
        raise SystemExit(
            f"8765 上的服务不是本项目后端（service={payload.get('service')!r}），拒绝操作"
        )
    if normalized(payload.get("project_root")) != normalized(ROOT):
        raise SystemExit(
            f"8765 属于另一个项目（{payload.get('project_root')!r}），拒绝操作"
        )
    pid = payload.get("pid")
    if not isinstance(pid, int) or pid <= 0 or pid == os.getpid():
        raise SystemExit(f"health 上报的 pid 不可用于重启：{pid!r}")
    return pid


def wait_port_release(timeout=STOP_TIMEOUT_S):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if health() is None:
            return True
        time.sleep(0.3)
    return False


def kill_tree(pid):
    """结束 health 明确指认的进程树（后端由 .bat 包装器拉起）。"""

    result = subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        capture_output=True, text=True,
    )
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def main():
    pid = current_pid()
    if pid is None:
        print("8765 当前空闲，直接启动当前 HEAD。", flush=True)
    else:
        print(f"正在结束本项目旧后端 pid={pid} …", flush=True)
        code, output = kill_tree(pid)
        print(f"taskkill rc={code} {output.strip()[:400]}", flush=True)
        if not wait_port_release():
            raise SystemExit("8765 在超时内没有释放，拒绝继续（不结束其它进程）")

    env = os.environ.copy()
    env.update(
        QT_QPA_PLATFORM="offscreen", PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
        CNS_LAUNCHER_GIT_COMMIT=subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(ROOT),
            capture_output=True, text=True,
        ).stdout.strip(),
    )
    LOG.parent.mkdir(exist_ok=True)
    stream = LOG.open("a", encoding="utf-8")
    stream.write("\n--- CNS restart " + time.strftime("%Y-%m-%d %H:%M:%S") + " ---\n")
    stream.flush()
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    process = subprocess.Popen(
        [str(QGIS_RUNNER), "-m", "cns_planner.map_server"],
        cwd=str(ROOT), env=env, stdout=stream, stderr=subprocess.STDOUT,
        creationflags=creationflags,
    )
    print(f"已启动 pid={process.pid}，等待 /api/health 就绪 …", flush=True)
    deadline = time.monotonic() + READY_TIMEOUT_S
    while time.monotonic() < deadline:
        payload = health()
        if isinstance(payload, dict) and payload.get("ready") is True:
            print(json.dumps(payload, ensure_ascii=False), flush=True)
            print(f"就绪：{URL}（pid={payload.get('pid')}）", flush=True)
            return 0
        if process.poll() is not None:
            raise SystemExit(f"后端启动失败（退出码 {process.poll()}），日志：{LOG}")
        time.sleep(0.5)
    raise SystemExit(f"后端启动超时，日志：{LOG}")


if __name__ == "__main__":
    raise SystemExit(main())
