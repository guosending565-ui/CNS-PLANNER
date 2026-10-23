"""服务端**原子**瓦片预算守卫（Round37-T3D-2A）。

定位：这是 ArcGIS 上游请求的**唯一**放行闸门。任何向 ``ibasemaps-api`` /
``tianditu`` 发出的真实请求，都必须先在这里原子预留一个额度；预留失败就**绝不**
触网，只在本地返回 ``imagery_budget_exhausted``。

设计要点（逐条对应任务要求）：

1. **默认离线**：``CNS_IMAGERY_ONLINE_MODE`` ∈ ``disabled|smoke|manual``，默认
   ``disabled``。发现 ``CNS_ARCGIS_TOKEN`` **不会**自动开启在线访问。
2. **双上限**：单次会话上限（``smoke`` 30 / ``manual`` 80）与每日总上限
   （``DAILY_LIMIT`` 150，所有模式共享）。
3. **跨进程持久**：计数写在**固定的共享目录**里的 JSON 文件中，进程重启后日预算
   依然有效。换端口、重建服务、自动重启都不改变该目录，因此绕不过日上限。
4. **test_run_id 不可用于绕过**：未显式给出 ``CNS_IMAGERY_TEST_RUN_ID`` 时，
   **复用**共享文件里已有的 run id（而不是生成新的），所以重启不会重置会话计数。
   显式换 run id 会重置会话计数，但**日上限仍然封顶**。
5. **原子预留**：用 ``O_CREAT|O_EXCL`` 文件锁串行化"读-改-写"，高并发下不可能
   突破上限；锁带陈旧清理（持锁进程崩溃不会永久锁死）。
6. **不落敏感信息**：状态文件只保存计数、日期、模式与时间戳，**绝不**写入凭证、
   含 token 的 URL 或上游响应体。
7. **内部请求预算 ≠ 官方配额**（R7-D3C）：这里的 150/1200 等数字是本项目自己的
   上游请求保护上限，不是天地图/ArcGIS 公布的免费配额；官方配额未经书面确认时
   一律返回 ``official_quota_verified=false``，绝不拿内部阈值冒充合同额度。
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

#: 在线模式取值。
ONLINE_MODE_DISABLED = "disabled"
ONLINE_MODE_SMOKE = "smoke"
ONLINE_MODE_MANUAL = "manual"
ONLINE_MODES = (ONLINE_MODE_DISABLED, ONLINE_MODE_SMOKE, ONLINE_MODE_MANUAL)

#: 每种在线模式的**单次会话**上游请求上限。``disabled`` 没有额度（恒 0）。
SESSION_LIMIT_BY_MODE = {
    ONLINE_MODE_DISABLED: 0,
    ONLINE_MODE_SMOKE: 30,
    ONLINE_MODE_MANUAL: 80,
}

#: **所有模式共享**的每日上游请求总上限。
DAILY_LIMIT = 150

#: R7-D3C：天地图使用的是**本项目内部请求预算**，不是官方免费配额。
#:
#: 官方配额（每日/每秒可调用次数、是否允许服务端代理、商用/离线条款）属于服务方
#: 合同，本项目**没有**取得可引用的书面数字，因此这里绝不把内部上限冒充官方配额：
#: 1200/300/60 只是防止误触“整区批量下载”的本地保护阈值。它们与计费源**分文件
#: 计数**：浏览天地图不会消耗 ArcGIS 的付费保护额度，反之亦然。
TIANDITU_INTERNAL_DAILY_REQUEST_BUDGET = 1200
TIANDITU_INTERNAL_SESSION_REQUEST_BUDGET_BY_MODE = {
    ONLINE_MODE_DISABLED: 0,
    ONLINE_MODE_SMOKE: 60,
    ONLINE_MODE_MANUAL: 300,
}
TIANDITU_INTERNAL_BUDGET_STATE_FILENAME = "imagery_budget_tianditu.json"

#: 向后兼容别名（R7-D3B 的测试/调用方仍可能 import）。语义已由上面的注释明确：
#: 它们是**内部请求预算**，不是官方免费配额。
FREE_DAILY_LIMIT = TIANDITU_INTERNAL_DAILY_REQUEST_BUDGET
FREE_SESSION_LIMIT_BY_MODE = TIANDITU_INTERNAL_SESSION_REQUEST_BUDGET_BY_MODE
FREE_STATE_FILENAME = TIANDITU_INTERNAL_BUDGET_STATE_FILENAME

#: 固定共享计数目录（换端口 / 重建服务 / 换工作树都不会改变它）。
#: 可用 ``CNS_IMAGERY_BUDGET_DIR`` 显式覆盖（供单元测试隔离，不供绕过）。
DEFAULT_BUDGET_DIR = Path(r"D:\Projects\_cns_workspaces\derived\imagery_budget")

STATE_FILENAME = "imagery_budget_state.json"
LOCK_FILENAME = "imagery_budget.lock"

#: 陈旧锁判定（秒）：持锁进程被强杀时锁文件会残留。
STALE_LOCK_SECONDS = 30.0
LOCK_WAIT_SECONDS = 15.0

#: 计数原因码：与上游调用一一对应，便于事后归因。
REASON_TILE = "tile"
REASON_METADATA = "metadata"
REASON_DIAGNOSE = "diagnose"


def local_date_of(moment=None):
    """本地日期字符串（``YYYY-MM-DD``）。日预算按**本地**日期滚动。"""

    moment = moment or datetime.now()
    return moment.strftime("%Y-%m-%d")


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


class ImageryBudgetExhausted(RuntimeError):
    """预算耗尽；**绝不**对应任何上游请求（本地直接拒绝）。"""

    def __init__(self, message, **detail):
        super().__init__(message)
        self.code = "imagery_budget_exhausted"
        self.detail = detail or {}


def normalize_online_mode(value):
    """把任意输入归一成合法模式；非法值**回落 ``disabled``**（fail-closed）。"""

    text = str(value or "").strip().lower()
    return text if text in ONLINE_MODES else ONLINE_MODE_DISABLED


def _initial_state(mode, run_id, today, daily_limit=DAILY_LIMIT, session_limit=None):
    if session_limit is None:
        session_limit = SESSION_LIMIT_BY_MODE.get(mode, 0)
    return {
        "schema_version": "imagery-budget/v1",
        "local_date": today,
        "daily_upstream_requests": 0,
        "daily_limit": daily_limit,
        "session_upstream_requests": 0,
        "session_limit": session_limit,
        "test_run_id": run_id,
        "request_mode": mode,
        "rejected_by_budget": 0,
        "last_request_time": None,
        "upstream_attempts": 0,
        "upstream_success": 0,
        "retry_attempts": 0,
        "by_source": {},
        "by_reason": {},
        "history": [],
    }


class ImageryBudgetGuard:
    """跨进程、跨重启的原子瓦片预算守卫。

    :param budget_dir: 共享计数目录；``None`` 时用 :data:`DEFAULT_BUDGET_DIR`
        （可被 ``CNS_IMAGERY_BUDGET_DIR`` 覆盖）。
    :param environ: 环境映射（默认 ``os.environ``）；读取
        ``CNS_IMAGERY_ONLINE_MODE`` / ``CNS_IMAGERY_TEST_RUN_ID`` /
        ``CNS_IMAGERY_BUDGET_DIR``。
    :param mode: 显式模式（优先于环境变量）；供调用方注入，**不**由测试脚本改写。
    :param clock: 可注入时钟（返回 ``datetime``），用于日期滚动测试。
    """

    def __init__(self, *, budget_dir=None, environ=None, mode=None, test_run_id=None,
                 clock=None, daily_limit=None, session_limits=None, state_filename=None):
        self._environ = os.environ if environ is None else environ
        self._clock = clock or datetime.now
        if budget_dir is not None:
            self.budget_dir = Path(str(budget_dir))
        else:
            configured = str(self._environ.get("CNS_IMAGERY_BUDGET_DIR") or "").strip()
            self.budget_dir = Path(configured) if configured else DEFAULT_BUDGET_DIR
        if mode is not None:
            self.mode = normalize_online_mode(mode)
        else:
            self.mode = normalize_online_mode(
                self._environ.get("CNS_IMAGERY_ONLINE_MODE"))
        explicit_run = test_run_id
        if explicit_run is None:
            explicit_run = self._environ.get("CNS_IMAGERY_TEST_RUN_ID")
        self._explicit_run_id = str(explicit_run or "").strip() or None
        #: R7-D3B：允许为**免费公开源**（天地图）设置独立的上限与独立计数文件。
        #: 计费源（ArcGIS）继续使用模块级 ``DAILY_LIMIT`` / ``SESSION_LIMIT_BY_MODE``；
        #: 两者绝不共享计数文件，避免免费源的浏览消耗 ArcGIS 的付费保护额度。
        self.daily_limit = int(daily_limit) if daily_limit is not None else DAILY_LIMIT
        self.session_limits = dict(SESSION_LIMIT_BY_MODE)
        if session_limits:
            self.session_limits.update({str(k): int(v) for k, v in session_limits.items()})
        self.state_filename = str(state_filename or STATE_FILENAME)

    # ------------------------------------------------------------------ 路径

    @property
    def state_path(self):
        return self.budget_dir / self.state_filename

    @property
    def lock_path(self):
        return self.budget_dir / (self.state_filename + ".lock")

    def session_limit(self):
        return self.session_limits.get(self.mode, 0)

    def online_allowed(self):
        """该模式是否允许触网（与剩余额度无关）。"""

        return self.mode in (ONLINE_MODE_SMOKE, ONLINE_MODE_MANUAL)

    # ------------------------------------------------------------------ 锁

    @contextmanager
    def _file_lock(self):
        """跨进程互斥：``O_CREAT|O_EXCL`` 创建锁文件。

        Windows 实测：并发 ``O_EXCL`` 除 ``FileExistsError`` 外还会抛
        ``PermissionError``（另一进程正在创建/删除同一文件时的瞬时冲突）。
        两者都必须当作"锁被占用"重试，否则高并发下会冒出随机异常。
        持锁进程崩溃时锁文件残留，这里按 :data:`STALE_LOCK_SECONDS` 清理，
        避免永久锁死。
        """

        self.budget_dir.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        handle = None
        while handle is None:
            try:
                handle = os.open(str(self.lock_path),
                                 os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except (FileExistsError, PermissionError):
                if not self.lock_path.exists():
                    # 瞬时竞争（文件刚被释放）：立即重试，不计入陈旧判定。
                    if time.monotonic() > deadline:
                        raise ImageryBudgetExhausted(
                            "影像预算锁竞争超时",
                            lock_wait_seconds=LOCK_WAIT_SECONDS,
                        )
                    time.sleep(0.005)
                    continue
                try:
                    age = time.time() - self.lock_path.stat().st_mtime
                except OSError:
                    age = 0.0
                if age > STALE_LOCK_SECONDS:
                    try:
                        self.lock_path.unlink()
                    except OSError:
                        pass
                    continue
                if time.monotonic() > deadline:
                    raise ImageryBudgetExhausted(
                        "影像预算锁等待超时（另有进程正在写入预算）",
                        lock_wait_seconds=LOCK_WAIT_SECONDS,
                    )
                time.sleep(0.02)
        try:
            os.write(handle, f"{os.getpid()} {time.time()}".encode("ascii"))
            yield
        finally:
            try:
                os.close(handle)
            except OSError:
                pass
            try:
                self.lock_path.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------ 读写

    def _read_state_locked(self, today):
        """读状态；日期滚动、缺字段补齐、run id 复用都在这里完成。

        **fail-closed**：状态文件存在但不可解析时**绝不**静默清零（那等于用一次
        文件损坏把日预算重置回 0，是一条真实的绕过路径），而是抛
        :class:`ImageryBudgetExhausted` 让请求被本地拒绝。
        """

        raw = {}
        if self.state_path.exists():
            try:
                raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ImageryBudgetExhausted(
                    "影像预算计数文件不可读或已损坏（拒绝在未知额度下触网）",
                    reason="budget_state_unreadable",
                    state_file=self.state_filename,
                    error_type=type(exc).__name__,
                ) from None
        if not isinstance(raw, dict):
            raise ImageryBudgetExhausted(
                "影像预算计数文件结构非法（拒绝在未知额度下触网）",
                reason="budget_state_invalid",
                state_file=self.state_filename,
            )
        state = _initial_state(self.mode, self._explicit_run_id, today,
                               daily_limit=self.daily_limit,
                               session_limit=self.session_limit())
        for key in ("daily_upstream_requests", "session_upstream_requests",
                    "rejected_by_budget", "upstream_attempts", "upstream_success",
                    "retry_attempts", "daily_limit"):
            try:
                state[key] = int(raw.get(key) or 0)
            except (TypeError, ValueError):
                state[key] = 0
        for key in ("last_request_time",):
            value = raw.get(key)
            state[key] = str(value) if value else None
        for key in ("by_source", "by_reason"):
            value = raw.get(key)
            state[key] = dict(value) if isinstance(value, dict) else {}
        history = raw.get("history")
        state["history"] = list(history) if isinstance(history, list) else []
        previous_date = str(raw.get("local_date") or "").strip() or None
        previous_run = str(raw.get("test_run_id") or "").strip() or None
        previous_mode = normalize_online_mode(raw.get("request_mode"))

        if previous_date and previous_date != today:
            # 日期滚动：把昨日计数归档，当日清零。日上限因此**不可能**被跨日累计绕过。
            state["history"] = (state["history"] + [{
                "local_date": previous_date,
                "daily_upstream_requests": int(state["daily_upstream_requests"]),
                "request_mode": previous_mode,
            }])[-14:]
            state["daily_upstream_requests"] = 0
            state["session_upstream_requests"] = 0
            state["local_date"] = today
        elif previous_date:
            state["local_date"] = previous_date

        # run id：显式给出时用它；否则**复用**既有 run id（重启不重置会话计数）。
        if self._explicit_run_id:
            state["test_run_id"] = self._explicit_run_id
            if previous_run and previous_run != self._explicit_run_id:
                state["session_upstream_requests"] = 0
        else:
            state["test_run_id"] = previous_run or f"auto-{today}"

        if previous_mode != self.mode:
            # 模式变化本身不清零任何计数：降级为 disabled 时额度直接不可用，
            # 升级到 smoke/manual 时**沿用**已消耗的日计数。
            state["request_mode"] = self.mode
        state["daily_limit"] = self.daily_limit
        state["session_limit"] = self.session_limit()
        return state

    def _write_state_locked(self, state):
        payload = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(payload + "\n", encoding="utf-8")
        os.replace(str(tmp), str(self.state_path))

    # ------------------------------------------------------------------ 预留

    def precheck(self):
        """**不消耗额度**的放行前置判定（模式与剩余额度）。

        为什么需要它：凭证检查在语义上**晚于**授权判定。离线模式下即使凭证齐全，
        正确的拒绝码也必须是 ``imagery_online_mode_disabled``（"未授权联网"），
        而不是 ``arcgis_token_missing``（"缺凭证"）—— 后者会把排查引向凭证方向，
        而真实原因是**运行模式**。因此调用方必须在读凭证**之前**先做这一步。

        失败时计入 ``rejected_by_budget``（与 :meth:`reserve` 同一口径，且因为
        precheck 失败后不会走到 reserve，不存在重复计数）。
        """

        today = local_date_of(self._clock())
        with self._file_lock():
            state = self._read_state_locked(today)
            if not self.online_allowed():
                state["rejected_by_budget"] = int(state["rejected_by_budget"]) + 1
                self._write_state_locked(state)
                raise ImageryBudgetExhausted(
                    "影像在线访问未启用（默认离线开发模式）",
                    request_mode=self.mode,
                    reason="imagery_online_mode_disabled",
                    daily_upstream_requests=state["daily_upstream_requests"],
                    daily_limit=self.daily_limit,
                    budget_remaining=self.remaining_of(state),
                )
            if int(state["daily_upstream_requests"]) >= self.daily_limit:
                state["rejected_by_budget"] = int(state["rejected_by_budget"]) + 1
                self._write_state_locked(state)
                raise ImageryBudgetExhausted(
                    "影像瓦片每日预算已耗尽",
                    request_mode=self.mode, reason="daily_budget_exhausted",
                    daily_upstream_requests=state["daily_upstream_requests"],
                    daily_limit=self.daily_limit, budget_remaining=0,
                )
            limit = self.session_limit()
            if int(state["session_upstream_requests"]) >= limit:
                state["rejected_by_budget"] = int(state["rejected_by_budget"]) + 1
                self._write_state_locked(state)
                raise ImageryBudgetExhausted(
                    "影像瓦片本次会话预算已耗尽",
                    request_mode=self.mode, reason="session_budget_exhausted",
                    session_upstream_requests=state["session_upstream_requests"],
                    session_limit=limit,
                    budget_remaining=self.remaining_of(state),
                )
            return {
                "request_mode": self.mode,
                "daily_upstream_requests": state["daily_upstream_requests"],
                "daily_limit": self.daily_limit,
                "session_upstream_requests": state["session_upstream_requests"],
                "session_limit": limit,
                "budget_remaining": self.remaining_of(state),
            }

    def reserve(self, *, source_id=None, reason=REASON_TILE, retry=False):
        """在**真正发出上游请求之前**原子预留 1 个额度。

        成功返回一份**预留凭证**（含预留后的计数快照）；失败抛
        :class:`ImageryBudgetExhausted`，调用方**必须**放弃上游请求。

        无论上游成功还是失败，额度都已消耗（保守计费）——这正是"一次上游重试
        也要独立计数"的实现方式：重试路径会**再次**调用本方法。
        """

        today = local_date_of(self._clock())
        with self._file_lock():
            state = self._read_state_locked(today)
            if not self.online_allowed():
                state["rejected_by_budget"] = int(state["rejected_by_budget"]) + 1
                self._write_state_locked(state)
                raise ImageryBudgetExhausted(
                    "影像在线访问未启用（默认离线开发模式）",
                    request_mode=self.mode,
                    reason="imagery_online_mode_disabled",
                    daily_upstream_requests=state["daily_upstream_requests"],
                    daily_limit=self.daily_limit,
                    budget_remaining=self.remaining_of(state),
                )
            if int(state["daily_upstream_requests"]) >= self.daily_limit:
                state["rejected_by_budget"] = int(state["rejected_by_budget"]) + 1
                self._write_state_locked(state)
                raise ImageryBudgetExhausted(
                    "影像瓦片每日预算已耗尽",
                    request_mode=self.mode,
                    reason="daily_budget_exhausted",
                    daily_upstream_requests=state["daily_upstream_requests"],
                    daily_limit=self.daily_limit,
                    budget_remaining=0,
                )
            limit = self.session_limit()
            if int(state["session_upstream_requests"]) >= limit:
                state["rejected_by_budget"] = int(state["rejected_by_budget"]) + 1
                self._write_state_locked(state)
                raise ImageryBudgetExhausted(
                    "影像瓦片本次会话预算已耗尽",
                    request_mode=self.mode,
                    reason="session_budget_exhausted",
                    session_upstream_requests=state["session_upstream_requests"],
                    session_limit=limit,
                    budget_remaining=self.remaining_of(state),
                )
            state["daily_upstream_requests"] = int(state["daily_upstream_requests"]) + 1
            state["session_upstream_requests"] = int(state["session_upstream_requests"]) + 1
            state["upstream_attempts"] = int(state["upstream_attempts"]) + 1
            if retry:
                state["retry_attempts"] = int(state["retry_attempts"]) + 1
            state["last_request_time"] = _now_iso()
            if source_id:
                by_source = state["by_source"]
                by_source[str(source_id)] = int(by_source.get(str(source_id)) or 0) + 1
            by_reason = state["by_reason"]
            by_reason[str(reason)] = int(by_reason.get(str(reason)) or 0) + 1
            self._write_state_locked(state)
            return {
                "request_mode": self.mode,
                "test_run_id": state["test_run_id"],
                "daily_upstream_requests": state["daily_upstream_requests"],
                "daily_limit": self.daily_limit,
                "session_upstream_requests": state["session_upstream_requests"],
                "session_limit": limit,
                "budget_remaining": self.remaining_of(state),
                "reserved_at": state["last_request_time"],
            }

    def record_success(self, source_id=None):
        """记录"真的收到有效图像"。只做计数，绝不参与放行判定。"""

        today = local_date_of(self._clock())
        with self._file_lock():
            state = self._read_state_locked(today)
            state["upstream_success"] = int(state["upstream_success"]) + 1
            self._write_state_locked(state)
            return {"upstream_success": state["upstream_success"]}

    # ------------------------------------------------------------------ 汇总

    def remaining_of(self, state):
        """剩余额度 = min(日剩余, 会话剩余)；``disabled`` 恒为 0。"""

        mode = normalize_online_mode(state.get("request_mode"))
        session_limit = self.session_limits.get(mode, 0)
        daily_remaining = max(
            0, self.daily_limit - int(state.get("daily_upstream_requests") or 0))
        session_remaining = max(
            0, session_limit - int(state.get("session_upstream_requests") or 0))
        return min(daily_remaining, session_remaining)

    def summary(self, *, browser_request_image_calls=None):
        """脱敏预算摘要（供 ``/imagery/config`` 与面板展示）。

        ``browser_request_image_calls`` 是 Cesium 方法调用次数，**只作对照**；
        计费口径一律以服务端真实预留/成功计数为准。
        """

        today = local_date_of(self._clock())
        degraded_reason = None
        try:
            with self._file_lock():
                state = self._read_state_locked(today)
                self._write_state_locked(state)
        except ImageryBudgetExhausted as exc:
            degraded_reason = str(exc.detail.get("reason") or "budget_lock_timeout")
            if degraded_reason in ("budget_state_unreadable", "budget_state_invalid"):
                # **绝不**在计数文件损坏时把摘要显示成 0：那会让面板看起来"额度充足"。
                state = _initial_state(self.mode, self._explicit_run_id, today,
                               daily_limit=self.daily_limit,
                               session_limit=self.session_limit())
                state["daily_upstream_requests"] = self.daily_limit
                state["session_upstream_requests"] = self.session_limit()
            else:
                # 锁争用不该让配置接口失败：退化为"读一次现有文件"。
                try:
                    state = json.loads(self.state_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    state = _initial_state(self.mode, self._explicit_run_id, today,
                               daily_limit=self.daily_limit,
                               session_limit=self.session_limit())
        return {
            "schema_version": "imagery-budget/v1",
            "request_mode": self.mode,
            "online_allowed": self.online_allowed(),
            "online_mode_source": "CNS_IMAGERY_ONLINE_MODE",
            "credential_presence_does_not_enable_online": True,
            "degraded_reason": degraded_reason,
            "local_date": state.get("local_date"),
            "daily_upstream_requests": int(state.get("daily_upstream_requests") or 0),
            "daily_limit": self.daily_limit,
            "session_upstream_requests": int(state.get("session_upstream_requests") or 0),
            "session_limit": self.session_limit(),
            "budget_remaining": self.remaining_of(state),
            "test_run_id": state.get("test_run_id"),
            "rejected_by_budget": int(state.get("rejected_by_budget") or 0),
            "last_request_time": state.get("last_request_time"),
            # 计费口径：只按**服务端真实请求证据**估算。
            "upstream_attempts": int(state.get("upstream_attempts") or 0),
            "upstream_success": int(state.get("upstream_success") or 0),
            "retry_attempts": int(state.get("retry_attempts") or 0),
            "by_source": dict(state.get("by_source") or {}),
            "by_reason": dict(state.get("by_reason") or {}),
            "browser_request_image_calls": browser_request_image_calls,
            "billing_basis": "server_side_reserved_upstream_attempts",
            "billing_note": (
                "计费量只按服务端预留的上游请求证据估算；"
                "browser_request_image_calls 是 Cesium 方法调用次数，"
                "实测可比真实网络瓦片高两个数量级，**不得**当作官方账单"
            ),
            #: R7-D3C：预算性质必须显式披露。这里的 daily_limit/session_limit 是
            #: **内部请求预算**，不是服务方公布的免费配额；官方配额未取得书面依据。
            "budget_kind": "internal_request_budget",
            "internal_request_budget": True,
            "official_quota_verified": False,
            "official_quota": None,
            "official_quota_note": (
                "未取得天地图/ArcGIS 官方书面配额或授权条款；"
                "内部上限只用于防止误触批量下载，不能解释为官方免费额度"
            ),
            "counter_dir_is_fixed": True,
            "counter_dir_note": (
                "计数目录固定且跨进程持久；重启服务、换端口、修改 test_run_id "
                "都不能重置日上限"
            ),
            "state_file": self.state_filename,
            "history": list(state.get("history") or [])[-7:],
        }


__all__ = [
    "DAILY_LIMIT", "DEFAULT_BUDGET_DIR", "FREE_DAILY_LIMIT",
    "FREE_SESSION_LIMIT_BY_MODE", "FREE_STATE_FILENAME",
    "TIANDITU_INTERNAL_DAILY_REQUEST_BUDGET",
    "TIANDITU_INTERNAL_SESSION_REQUEST_BUDGET_BY_MODE",
    "TIANDITU_INTERNAL_BUDGET_STATE_FILENAME",
    "ImageryBudgetExhausted", "ImageryBudgetGuard",
    "ONLINE_MODES", "ONLINE_MODE_DISABLED", "ONLINE_MODE_MANUAL", "ONLINE_MODE_SMOKE",
    "REASON_DIAGNOSE", "REASON_METADATA", "REASON_TILE", "SESSION_LIMIT_BY_MODE",
    "local_date_of", "normalize_online_mode",
]
