"""Reliable launcher for the local QGIS workbench.

BUG-STARTUP-001：不再"端口上有 cns-map 就直接复用"。
复用前必须用 ``/api/health`` 的运行身份确认对方就是**本项目**启动的后端；
端口被其他项目或未知服务占用时明确报错，既不静默复用也不误杀。
本启动器创建的整棵进程树由 ``launcher_process`` 用 Windows Job Object 托管，
启动窗口关闭（包括被强杀）时 8765 由内核释放。
"""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import urlopen
import webbrowser

import launcher_process

ROOT = Path(__file__).resolve().parent
URL = "http://127.0.0.1:8765"
HOST, PORT = "127.0.0.1", 8765
LOG = ROOT / "outputs" / "map-server.log"
SERVICE = "cns-map"


class HealthProbe:
    """一次 /api/health 探测的结论，足以判定"能否复用"。"""

    def __init__(self, occupied, service=None, ready=None, project_root=None, pid=None,
                 started_at=None, git_commit=None, data_error=None):
        self.occupied = occupied
        self.service = service
        self.ready = ready
        self.project_root = project_root
        self.pid = pid
        self.started_at = started_at
        self.git_commit = git_commit
        self.data_error = data_error


def normalized(path):
    """统一比较用的路径形式（Windows 大小写不敏感、分隔符不一致）。"""
    if path is None:
        return None
    try:
        return os.path.normcase(os.path.normpath(str(Path(path).resolve())))
    except (OSError, ValueError):
        return os.path.normcase(os.path.normpath(str(path)))


def health_identity():
    """本启动器所属项目的身份，用于与在跑服务比对。"""
    return {"project_root": str(ROOT), "git_commit": git_commit()}


_GIT_COMMIT = None


def git_commit():
    """尽力读取当前 HEAD（只读一次并缓存）；读不到就返回空字符串。

    身份校验不依赖它，所以 git 缺失或超时都不影响启动。
    """
    global _GIT_COMMIT
    if _GIT_COMMIT is None:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=str(ROOT),
                capture_output=True, text=True, timeout=5,
            )
            _GIT_COMMIT = result.stdout.strip() if result.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            _GIT_COMMIT = ""
    return _GIT_COMMIT


def probe_health():
    """读取 8765 上的健康身份；连接失败与"不是 cns-map 服务"都如实返回。"""
    try:
        with urlopen(URL + "/api/health", timeout=2) as response:
            payload = json.load(response)
    except (OSError, ValueError):
        return HealthProbe(occupied=True)
    if not isinstance(payload, dict):
        return HealthProbe(occupied=True)
    return HealthProbe(
        occupied=True,
        service=payload.get("service"),
        ready=payload.get("ready"),
        project_root=payload.get("project_root"),
        pid=payload.get("pid"),
        started_at=payload.get("started_at"),
        git_commit=payload.get("git_commit"),
        data_error=payload.get("data_error") or None,
    )


def port_in_use(host=HOST, port=PORT):
    """端口是否已被监听（不依赖对端是不是 HTTP）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1)
        return probe.connect_ex((host, port)) == 0


def health_matches(probe, identity=None):
    """对方是否就是**本项目**的 cns-map 服务。

    只接受同时满足三条的响应：service 名为 cns-map、ready 为真、project_root
    与本仓库根目录一致。缺 project_root 的旧版本后端按"不属于本项目"处理，
    不会被静默复用。
    """
    identity = identity or health_identity()
    if probe is None or not probe.occupied or probe.service != SERVICE:
        return False
    if probe.ready is not True:
        return False
    expected, actual = normalized(identity.get("project_root")), normalized(probe.project_root)
    return bool(expected) and expected == actual


def is_ready():
    """复用判据：8765 上确实是本项目的 cns-map 后端。"""
    return health_matches(probe_health())


def find_runner():
    override = os.environ.get("CNS_QGIS_PYTHON")
    candidates = sorted(Path(os.environ.get("ProgramFiles", "C:/Program Files")).glob("QGIS */bin/python-qgis*.bat"))
    runner = Path(override) if override else (candidates[-1] if candidates else None)
    if runner is None or not runner.is_file():
        raise RuntimeError("未找到 QGIS。请安装 QGIS，或设置 CNS_QGIS_PYTHON 为 python-qgis*.bat 路径。")
    return runner


def ensure_server(timeout=90):
    """复用本项目的健康服务，或启动一个新的受托管服务。

    端口被占用时必须先确认身份：
    * 身份匹配 → 复用，返回 None；
    * 是 cns-map 但 project_root 不是本项目 → 报错指出是哪个项目；
    * 不是 cns-map / 不是 HTTP → 报错说明 8765 被未知服务占用。
    """
    if port_in_use():
        probe = probe_health()
        if health_matches(probe):
            return None
        if probe.service == SERVICE:
            owner = probe.project_root or "未知（该后端未上报 project_root）"
            raise RuntimeError(
                f"8765 已被另一个 CNS 地图服务占用（项目目录：{owner}）。"
                f"本启动器不会复用它，也不会结束它。请先关闭该服务，或改用其他端口。"
            )
        if probe.service:
            raise RuntimeError(
                f"8765 已被其他服务占用（service={probe.service!r}），不是 CNS 地图服务。"
                "本启动器不会复用它，也不会结束它。请释放该端口后重试。"
            )
        raise RuntimeError(
            "8765 已被其他程序占用（对方不是 CNS 地图服务）。"
            "本启动器不会复用它，也不会结束它。请释放该端口后重试。"
        )
    runner = find_runner()
    env = os.environ.copy()
    env.update(
        QT_QPA_PLATFORM="offscreen", PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
        CNS_LAUNCHER_GIT_COMMIT=git_commit(),
    )
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a", encoding="utf-8") as logfile:
        logfile.write("\n--- CNS start " + time.strftime("%Y-%m-%d %H:%M:%S") + " ---\n")
        logfile.flush()
        tree = launcher_process.ProcessTree().launch(
            launcher_process.wrapper_command(runner, ["-m", "cns_planner.map_server"]),
            cwd=ROOT, env=env, stdout=logfile, suspend=True,
        )
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if is_ready():
                return tree
            if tree.poll() is not None:
                raise RuntimeError(f"地图服务启动失败（退出码 {tree.poll()}）。请查看日志：{LOG}")
            time.sleep(0.4)
        raise RuntimeError(f"地图服务启动超时，请查看日志后重试：{LOG}")
    except BaseException:
        tree.close()
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-browser", action="store_true")
    options = parser.parse_args()
    tree = None
    try:
        print("正在检查并启动 CNS 地图服务，请稍候…", flush=True)
        tree = ensure_server()
        print(f"地图已就绪：{URL}\n日志：{LOG}", flush=True)
        if not options.no_browser:
            webbrowser.open(URL)
        if tree:
            print("本窗口保持打开即可使用地图。", flush=True)
            tree.wait()
    except KeyboardInterrupt:
        pass
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        # 正常退出、Ctrl+C 都会走到这里；窗口被强杀时由 Job 的
        # KILL_ON_JOB_CLOSE 兜底，两条路径都保证 8765 被释放。
        if tree:
            tree.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
