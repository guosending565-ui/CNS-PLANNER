"""后端进程的最小运行身份（BUG-STARTUP-001）。

启动器判断"8765 上这个服务是不是我这次启动、属于本项目"时，只依赖这一组字段。
放在这个不导入 QGIS 的薄模块里，是为了让身份逻辑可以被独立测试。

字段语义：
* ``pid``：当前监听进程的真实 PID（重启后必然变化）；
* ``started_at``：进程启动时刻（UTC ISO 8601）；
* ``project_root``：本后端的仓库根目录，启动器据此拒绝跨项目复用；
* ``git_commit``：启动器通过 ``CNS_LAUNCHER_GIT_COMMIT`` 注入的 HEAD，读不到时留空。
"""

from datetime import datetime, timezone
import os
from pathlib import Path

ENV_GIT_COMMIT = "CNS_LAUNCHER_GIT_COMMIT"


def timestamp():
    """当前 UTC 时刻（ISO 8601，秒精度）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_PROCESS_STARTED_AT = timestamp()
_IDENTITY = None


def build_identity(project_root, started=None, git_commit=None):
    """构造一份运行身份；``project_root`` 必须由调用方给出真实仓库根目录。"""
    commit = git_commit
    if commit is None:
        commit = os.environ.get(ENV_GIT_COMMIT) or ""
    return {
        "pid": os.getpid(),
        "started_at": started or _PROCESS_STARTED_AT,
        "project_root": str(Path(project_root).resolve()),
        "git_commit": commit,
    }


def process_identity(project_root):
    """进程内稳定、跨进程唯一的身份字典（同进程重复调用返回同一份）。"""
    global _IDENTITY
    if _IDENTITY is None:
        _IDENTITY = build_identity(project_root)
    return _IDENTITY
