"""Reliable launcher for the local QGIS workbench."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import urlopen
import webbrowser

ROOT = Path(__file__).resolve().parent
URL = "http://127.0.0.1:8765"
LOG = ROOT / "outputs" / "map-server.log"


def is_ready():
    try:
        with urlopen(URL + "/api/health", timeout=2) as response:
            return json.load(response).get("service") == "cns-map"
    except (OSError, ValueError):
        return False


def find_runner():
    override = os.environ.get("CNS_QGIS_PYTHON")
    candidates = sorted(Path(os.environ.get("ProgramFiles", "C:/Program Files")).glob("QGIS */bin/python-qgis*.bat"))
    runner = Path(override) if override else (candidates[-1] if candidates else None)
    if runner is None or not runner.is_file():
        raise RuntimeError("未找到 QGIS。请安装 QGIS，或设置 CNS_QGIS_PYTHON 为 python-qgis*.bat 路径。")
    return runner


def ensure_server(timeout=90):
    """Reuse a healthy server or start one; never report success before ready."""
    if is_ready():
        return None
    runner = find_runner()
    env = os.environ.copy()
    env.update(QT_QPA_PLATFORM="offscreen", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a", encoding="utf-8") as logfile:
        logfile.write("\n--- CNS start " + time.strftime("%Y-%m-%d %H:%M:%S") + " ---\n")
        logfile.flush()
        process = subprocess.Popen(
            [str(runner), "-m", "cns_planner.map_server"],
            cwd=str(ROOT), env=env, stdout=logfile, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0,
        )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_ready():
            return process
        if process.poll() is not None:
            raise RuntimeError(f"地图服务启动失败（退出码 {process.returncode}）。请查看日志：{LOG}")
        time.sleep(0.4)
    raise RuntimeError(f"地图服务启动超时，请查看日志后重试：{LOG}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-browser", action="store_true")
    options = parser.parse_args()
    try:
        print("正在检查并启动 CNS 地图服务，请稍候…", flush=True)
        process = ensure_server()
        print(f"地图已就绪：{URL}\n日志：{LOG}", flush=True)
        if not options.no_browser:
            webbrowser.open(URL)
        if process:
            print("本窗口保持打开即可使用地图。", flush=True)
            process.wait()
    except KeyboardInterrupt:
        pass
    except (RuntimeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
