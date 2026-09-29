"""Reliable launcher for the local QGIS workbench.

BUG-STARTUP-001：不再"端口上有 cns-map 就直接复用"。
复用前必须用 ``/api/health`` 的运行身份确认对方就是**本项目**启动的后端；
端口被其他项目或未知服务占用时明确报错，既不静默复用也不误杀。
本启动器创建的整棵进程树由 ``launcher_process`` 用 Windows Job Object 托管，
启动窗口关闭（包括被强杀）时 8765 由内核释放。

BUG-STARTUP-003：**同一仓库的旧版本后端**不再要求人工杀进程，而是受控自动替换。
四种情形各自的行为被冻结为：

* CASE A 8765 空闲            → 启动当前 HEAD；
* CASE B 身份完全一致          → 直接复用，绝不重启；
* CASE C 同仓库、旧 git_commit → 只结束 health 明确指认的那一个 pid，
                                等 8765 真正释放，再启动当前 HEAD，
                                等 health 确认新身份后才允许打开浏览器；
* CASE D 其它 project_root     → 拒绝，绝不结束；
* CASE E 其它 service / 非 HTTP / 身份不完整 → 拒绝（fail-closed），绝不结束。

``git_commit`` 比较本身保持不变：绝不允许"只要目录相同就复用"，否则改过代码却仍在
运行的旧进程会冒充新版本；区别只在于同仓库旧版本从"报错要求人工处理"变成"安全自动重启"。
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
# 自动替换旧版本后端时，等待 8765 真正释放的上限（秒）。
RESTART_PORT_WAIT = 10.0


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


def same_project_root(probe, identity=None):
    """对方上报的 project_root 是否就是本仓库根目录。"""

    identity = identity or health_identity()
    expected, actual = normalized(identity.get("project_root")), normalized(probe.project_root)
    return bool(expected) and expected == actual


def commit_identity_matches(expected, actual):
    """git_commit 身份比较（BUG-STARTUP-002）。

    * 双方都读不到 commit（例如都不是 git 工作树）：无法区分版本，退化为"不比较"，
      这样没有 git 的环境仍然可以正常启动/复用；
    * 只有一方能给出 commit：身份未知，一律按"版本可能过旧"处理，绝不静默复用；
    * 双方都给出：必须逐字相同，否则同目录下的旧后端不得被复用。
    """

    left = str(expected or "").strip()
    right = str(actual or "").strip()
    if not left and not right:
        return True
    return bool(left) and left == right


def health_matches(probe, identity=None):
    """对方是否就是**本项目当前 HEAD** 的 cns-map 服务。

    只接受同时满足四条的响应：service 名为 cns-map、ready 为真、project_root
    与本仓库根目录一致、git_commit 与当前 HEAD 一致。缺 project_root 的旧版本后端
    按"不属于本项目"处理；project_root 相同但 git_commit 不同的旧后端同样**不得**
    被静默复用（否则同目录里改过代码却仍在跑的旧进程会被当成新版本）。
    """

    identity = identity or health_identity()
    if probe is None or not probe.occupied or probe.service != SERVICE:
        return False
    if probe.ready is not True:
        return False
    if not same_project_root(probe, identity):
        return False
    return commit_identity_matches(identity.get("git_commit"), probe.git_commit)


def stale_backend_message(probe, identity=None):
    """同项目目录、但版本身份不匹配时的明确诊断（BUG-STARTUP-002/003）。

    BUG-STARTUP-003 起，这种情况的默认路径是**受控自动替换**
    （``replace_stale_backend``）；本函数提供那条诊断本身，用于自动替换不可用时
    （身份不足以安全结束进程）如实告知用户到底检测到了什么。
    """

    identity = identity or health_identity()
    running = str(probe.git_commit or "").strip() or "未上报"
    current = str(identity.get("git_commit") or "").strip() or "未上报"
    return (
        f"8765 上运行的是本项目目录（{identity.get('project_root')}）下的**旧版本**后端："
        f"后端 git_commit={running}，当前 HEAD={current}。"
    )


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


def short_commit(value):
    """诊断日志里的短提交号；读不到就如实说"未上报"。"""
    text = str(value or "").strip()
    return text[:7] if text else "未上报"


def verified_pid(probe, identity=None):
    """返回**允许被自动结束**的旧后端 pid；任何一项身份不满足都返回 None。

    BUG-STARTUP-003：自动结束旧进程是这套生命周期里唯一有破坏性的动作，所以它只有
    一个入口判据，且四条必须同时成立：

    * ``service == "cns-map"``；
    * ``ready is True``（对方自报已就绪，不是半启动状态）；
    * ``project_root`` 归一化后就是本仓库根目录；
    * ``pid`` 是有效正整数，且不是本启动器自己。

    任何一条不成立都 fail-closed 返回 None：宁可要求人工处理，也不结束一个身份
    不明确的进程（不让"自动重启"退化成按名字杀进程）。
    """

    if probe is None or not probe.occupied or probe.service != SERVICE:
        return None
    if probe.ready is not True:
        return None
    if not same_project_root(probe, identity):
        return None
    if isinstance(probe.pid, bool):
        return None
    try:
        pid = int(str(probe.pid).strip())
    except (TypeError, ValueError):
        return None
    if pid <= 0 or pid == os.getpid():
        return None
    return pid


def wait_for_port_release(timeout=RESTART_PORT_WAIT, interval=0.2):
    """轮询等待 8765 真正释放；返回是否已释放。

    "结束 pid"与"端口释放"不是同一件事：wrapper（cmd.exe）、TIME_WAIT、以及被
    终止进程的清理都需要时间，所以必须真的看到端口空闲才允许启动新服务。
    """

    deadline = time.monotonic() + max(float(timeout), 0.0)
    while True:
        if not port_in_use():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def start_managed_server(timeout=90):
    """启动当前 HEAD 的受托管后端，并等到 ``/api/health`` 确认身份一致。

    调用方必须先保证 8765 空闲。返回的 ``ProcessTree`` 归本启动器所有，
    调用方负责 ``close()``；启动失败或身份不符时本函数自己关掉刚创建的树。
    """

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
            # readiness 只看轻量的 /api/health：绝不在这里调用 /api/state、
            # /api/workflow 或任何 artifact，否则启动耗时会退化成"等完整 workflow"。
            if is_ready():
                return tree
            if tree.poll() is not None:
                raise RuntimeError(f"地图服务启动失败（退出码 {tree.poll()}）。请查看日志：{LOG}")
            time.sleep(0.4)
        raise RuntimeError(f"地图服务启动超时，请查看日志后重试：{LOG}")
    except BaseException:
        tree.close()
        raise


def replace_stale_backend(probe, timeout=90):
    """CASE C：同仓库目录、旧版本身份 → 受控自动替换（BUG-STARTUP-003）。

    严格串行，顺序不可交换，也不存在"先开浏览器、再异步启动后端"的窗口：

    1. 记录旧后端的 pid / started_at / git_commit；
    2. 只结束 ``/api/health`` 明确指认的那一个 pid；
    3. 轮询等待 8765 真正释放（最长 ``RESTART_PORT_WAIT`` 秒）；
    4. 端口释放后才启动当前 HEAD 的新 ``ProcessTree``；
    5. 等 ``/api/health`` 报出"当前 ROOT + 当前 HEAD + ready"，本函数才返回；
    6. 调用方（``main``）在返回之后才打开浏览器。

    端口在等待窗口内没有释放时**不再结束任何进程**：只如实报告现状并报错。
    """

    identity = health_identity()
    running, current = short_commit(probe.git_commit), short_commit(identity.get("git_commit"))
    pid = verified_pid(probe, identity)
    if pid is None:
        raise RuntimeError(
            stale_backend_message(probe, identity)
            + "但它的运行身份不足以安全自动重启"
            f"（service={probe.service!r}，ready={probe.ready!r}，pid={probe.pid!r}）。"
            "本启动器不会结束身份不明确的进程，请人工处理后重试。"
        )
    print(
        f"检测到本项目旧版本后端 {running}"
        f"（pid {pid}，started_at {probe.started_at or '未上报'}），正在自动重启…",
        flush=True,
    )
    if not launcher_process.terminate_verified_process(pid):
        raise RuntimeError(
            f"无法结束旧版本后端（pid {pid}，git_commit={running}）。"
            "本启动器只会结束 /api/health 明确指认的那一个进程，请人工检查后重试。"
        )
    if not wait_for_port_release():
        # 端口仍被占用：重新探测一次**只为如实报告**发生了什么。
        # 无论探测结果如何，都绝不再结束任何进程。
        again = probe_health()
        if not again.occupied:
            detail = "8765 上已经没有服务响应 /api/health"
        elif again.service == SERVICE and same_project_root(again, identity):
            detail = (
                f"8765 上的 cns-map 服务仍然存在"
                f"（pid={again.pid!r}，git_commit={short_commit(again.git_commit)}）"
            )
        else:
            detail = f"8765 现由其它进程占用（service={again.service!r}）"
        raise RuntimeError(
            f"旧 CNS 后端已请求关闭，但 8765 仍被占用：{detail}。"
            "本启动器不会结束身份不明的进程，请人工释放 8765 后重试。"
        )
    print(f"旧后端已关闭，正在启动 {current}…", flush=True)
    tree = start_managed_server(timeout=timeout)
    print("地图服务已就绪。", flush=True)
    return tree


def ensure_server(timeout=90):
    """复用本项目的健康服务，受控替换本项目的旧版本后端，或启动一个新的受托管服务。

    生命周期规则（BUG-STARTUP-003）：

    * CASE A：8765 空闲 → 启动当前 HEAD；
    * CASE B：身份完全一致（同一 project_root + 同一 git_commit + ready）→ 直接复用，
      不重启；
    * CASE C：同一 project_root、git_commit 不是当前 HEAD → 本项目旧版本后端，
      自动替换（见 ``replace_stale_backend``）；
    * CASE D：是 cns-map 但 project_root 是别的项目 → 明确报错，绝不结束；
    * CASE E：其它 service / 非 HTTP / 身份不完整 → 明确报错（fail-closed），绝不结束。

    返回值：``None`` 表示复用了已有的健康服务；否则返回本启动器刚创建、需要由调用方
    ``close()`` 的 ``ProcessTree``。
    """

    if not port_in_use():
        return start_managed_server(timeout=timeout)   # CASE A
    probe = probe_health()
    if health_matches(probe):
        return None                                    # CASE B：身份一致，绝不重启
    if probe.service == SERVICE:
        if same_project_root(probe):
            return replace_stale_backend(probe, timeout=timeout)   # CASE C
        owner = probe.project_root or "未知（该后端未上报 project_root）"
        raise RuntimeError(                                # CASE D
            f"8765 已被另一个 CNS 地图服务占用（项目目录：{owner}）。"
            f"本启动器不会复用它，也不会结束它。请先关闭该服务，或改用其他端口。"
        )
    if probe.service:
        raise RuntimeError(                                # CASE E
            f"8765 已被其他服务占用（service={probe.service!r}），不是 CNS 地图服务。"
            "本启动器不会复用它，也不会结束它。请释放该端口后重试。"
        )
    raise RuntimeError(                                    # CASE E
        "8765 已被其他程序占用（对方不是 CNS 地图服务）。"
        "本启动器不会复用它，也不会结束它。请释放该端口后重试。"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-browser", action="store_true")
    options = parser.parse_args()
    tree = None
    try:
        print("正在检查并启动 CNS 地图服务，请稍候…", flush=True)
        # BUG-STARTUP-003：ensure_server 内部严格串行完成
        # 「结束旧后端 → 等 8765 释放 → 启动当前 HEAD → 等 health 报出当前身份」；
        # 只有它成功返回后才打开浏览器，因此浏览器永远不会打开在一个"后端刚被替换、
        # 新后端还没开始监听"的空窗上（这正是 ERR_CONNECTION_REFUSED 的成因）。
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
