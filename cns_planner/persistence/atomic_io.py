"""Round32-I：正式状态落盘的原子替换原语 + **有界**瞬时失败重试。

Round32-I 在真实 288 MB 项目副本上复现到的根因（Windows，本机实测）：

* ``os.replace``（= ``MoveFileExW(MOVEFILE_REPLACE_EXISTING)``）**无法替换一个
  正被其它句柄打开的目标文件**：目标被任何读句柄打开时，替换立刻返回
  ``PermissionError [WinError 5] Access is denied``；句柄一关，同样的替换立刻成功。
* 实测**读句柄带不带 ``FILE_SHARE_DELETE`` 都一样失败**（同进程与跨进程各测一次），
  所以"让读者用共享删除模式打开"这条路线在本机 Windows 上被实验证伪，本模块因此
  **不**引入 ctypes / CreateFileW：它既无效，又会增加无谓的复杂度。
* 真实 288 MB ``project_state.json`` 的读取窗口实测约 **1.09 s**（读一次 = 持句柄
  1.09 s）。CNS 自己的 worker 子进程会读同一份正式状态，杀软 / 索引器 / 云同步也会。
  而发布重试原先只有 5 次 × 10~40 ms ≈ **100 ms**：一旦保存与该占用窗口发生重叠，
  该碰撞场景下的失败是可预测的；但并不是每一次保存都会发生该碰撞，也不声称已复现
  用户现场的生产 traceback。

因此本轮的修复边界就是：**把有界重试的预算做到能覆盖真实读取窗口**，同时保持
"只对明确瞬时失败重试、次数很少、绝不无限重试、绝不吞掉其它 IO 错误"。

不涉及 ProjectState 结构、revision、fingerprint、失效图或任何算法语义。
"""

from __future__ import annotations

import errno
import os
import time


# ---- 瞬时失败判定 ------------------------------------------------------------

#: Windows ``ERROR_ACCESS_DENIED`` / ``ERROR_SHARING_VIOLATION`` /
#: ``ERROR_LOCK_VIOLATION``。CPython 把三者都归一成 ``PermissionError``（errno 13），
#: 因此判定必须看 ``winerror``：目标被别人打开着是 5，来源（临时文件）被占是 32。
_TRANSIENT_WINERRORS = frozenset({5, 32, 33})

#: 非 Windows 的等价瞬时错误（POSIX 上没有 winerror）。
_TRANSIENT_ERRNOS = frozenset({errno.EACCES, errno.EPERM, errno.EBUSY})

#: 有界重试预算：**5 次尝试**（= 4 次重试），退避 0.1 / 0.2 / 0.4 / 0.8 s（合计 1.5 s）。
#: 依据：真实 288 MB 正式状态的单次读取窗口实测 1.09 s，1.5 s 覆盖它并留出余量；
#: 次数保持"很少"，不是无限重试，最坏情况 1.5 s 后按失败上报。
REPLACE_ATTEMPTS = 5
REPLACE_BACKOFF_SECONDS = (0.1, 0.2, 0.4, 0.8)


def is_transient_replace_error(exc: BaseException) -> bool:
    """该异常是否属于"稍后重试同一原子操作可能成功"的瞬时失败。

    Windows 上 ``winerror`` 是**排他**判据：只有 ``ERROR_ACCESS_DENIED(5)`` /
    ``ERROR_SHARING_VIOLATION(32)`` / ``ERROR_LOCK_VIOLATION(33)`` 才可重试。
    带其它 ``winerror`` 的异常（路径不存在、磁盘满……）即使 ``errno`` 恰好也是
    ``EACCES`` 也**不**重试——不能因为 Windows 把多种失败都归一成 errno 13 就把
    非共享冲突误判成瞬时。只有在完全没有 ``winerror`` 时才回落到 ``errno`` 白名单
    （POSIX 语义）。

    注意 Windows 上"文件正被占用"与"ACL 真的不给权限"同为 ``winerror 5``，无法区分：
    两者都会走完有界重试后失败。这只影响失败前的等待时间（最多 1.5 s），不影响
    正确性，也绝不会把失败伪报成成功。
    """

    if not isinstance(exc, OSError):
        return False
    winerror = getattr(exc, "winerror", None)
    if winerror is not None:
        return int(winerror) in _TRANSIENT_WINERRORS
    return getattr(exc, "errno", None) in _TRANSIENT_ERRNOS


def transient_reason(exc: BaseException) -> str:
    """给用户/日志看的原因摘要：优先 Windows ``winerror``，否则 ``errno``。"""

    winerror = getattr(exc, "winerror", None)
    if winerror:
        return f"Windows WinError {int(winerror)}"
    errno_value = getattr(exc, "errno", None)
    if errno_value:
        return f"errno {int(errno_value)}"
    return type(exc).__name__


# ---- 写：原子替换 + 有界短退避 ------------------------------------------------


def atomic_replace(source, target) -> None:
    """``os.replace`` 的原子发布，只对瞬时共享/权限失败做有界短退避重试。

    不变量：每次尝试都是**整文件原子替换**，因此重试不会暴露半截目标文件；重试耗尽时
    抛出**最后一次的原始 OSError**（保留 errno / winerror），由调用方决定怎么翻译成
    用户可见的消息。非瞬时错误（磁盘满 / 路径不存在 / 来源缺失……）立即抛出，不重试。
    """

    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(source, target)
            return
        except OSError as exc:
            if not is_transient_replace_error(exc):
                raise
            if attempt == REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(REPLACE_BACKOFF_SECONDS[attempt])


__all__ = [
    "REPLACE_ATTEMPTS", "REPLACE_BACKOFF_SECONDS", "atomic_replace",
    "is_transient_replace_error", "transient_reason",
]
