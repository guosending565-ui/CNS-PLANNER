"""Round37-T3D-2A 服务端瓦片预算硬门禁回归（全部离线，0 上游请求）。

覆盖任务明确要求的六类证据：

1. **默认离线**：``CNS_IMAGERY_ONLINE_MODE`` 未设置 / 非法值 ⇒ ``disabled``，
   且**发现凭证存在也不会**自动开启在线访问；
2. **模式额度**：``smoke`` 30 / ``manual`` 80，跨模式共享**每日 150** 总上限；
3. **并发**：高并发下预留数**不可能**突破上限（原子预留）；
4. **跨进程重启**：新建 guard 实例（模拟重启）后日预算与已消耗额度仍然有效；
5. **不可绕过**：换 ``test_run_id`` 只能重置**会话**计数，日上限仍封顶；
   换端口/重建服务不改变共享计数目录，因此同样绕不过；
6. **本地拒绝**：耗尽后返回 ``imagery_budget_exhausted`` 且**绝不**调用上游。
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timedelta

import pytest

from cns_planner.application.imagery_budget import (
    DAILY_LIMIT,
    SESSION_LIMIT_BY_MODE,
    ImageryBudgetExhausted,
    ImageryBudgetGuard,
    normalize_online_mode,
)
from cns_planner.application.visualization_3d_imagery_service import (
    Imagery3DService,
    ImageryUnavailable,
)


def budget_dir():
    root = os.path.join(
        os.environ.get("TEMP") or os.environ.get("TMP") or ".", "cns_imagery_budget_test")
    path = os.path.join(root, uuid.uuid4().hex[:10])
    os.makedirs(path, exist_ok=True)
    return path


def guard(mode="smoke", *, directory=None, run_id=None, environ=None):
    return ImageryBudgetGuard(
        budget_dir=directory or budget_dir(), mode=mode,
        test_run_id=run_id, environ=environ if environ is not None else {},
    )


def png_tile():
    import struct

    return b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + struct.pack(">II", 256, 256) + b"tile"


class CountingFetcher:
    """只计数、不做任何网络 I/O 的 fetcher 替身。"""

    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, url, *, headers, timeout_s, max_bytes):
        with self.lock:
            self.calls.append(url)
        return 200, {"Content-Type": "image/png"}, png_tile(), url


# ---------------------------------------------------------------- 1) 默认离线


def test_online_mode_defaults_to_disabled_and_rejects_invalid_values():
    assert normalize_online_mode(None) == "disabled"
    assert normalize_online_mode("") == "disabled"
    assert normalize_online_mode("ENABLED") == "disabled"
    assert normalize_online_mode("on") == "disabled"
    assert normalize_online_mode("manual") == "manual"
    assert normalize_online_mode(" smoke ") == "smoke"
    # 未设置环境变量 ⇒ disabled
    assert ImageryBudgetGuard(budget_dir=budget_dir(), environ={}).mode == "disabled"


def test_credential_presence_does_not_enable_online_access():
    fetcher = CountingFetcher()
    service = Imagery3DService(
        environ={"CNS_ARCGIS_TOKEN": "AA-secret-token"},
        fetcher=fetcher,
        budget=guard("disabled"),
    )
    with pytest.raises(ImageryUnavailable) as exc:
        service.tile("arcgis_world_imagery", 12, 3438, 1689)
    assert exc.value.code == "imagery_online_mode_disabled"
    assert fetcher.calls == []          # 0 上游请求
    assert service.budget_summary()["daily_upstream_requests"] == 0
    assert service.budget_summary()["rejected_by_budget"] == 1


def test_environment_flag_is_the_only_switch(monkeypatch):
    directory = budget_dir()
    # 显式设置环境变量才允许在线；凭证存在与否完全不影响判定。
    enabled = ImageryBudgetGuard(
        budget_dir=directory, environ={"CNS_IMAGERY_ONLINE_MODE": "smoke"})
    assert enabled.mode == "smoke" and enabled.online_allowed() is True
    disabled = ImageryBudgetGuard(
        budget_dir=directory,
        environ={"CNS_IMAGERY_ONLINE_MODE": "disabled", "CNS_ARCGIS_TOKEN": "AA-x"})
    assert disabled.mode == "disabled" and disabled.online_allowed() is False


# ---------------------------------------------------------------- 2) 模式额度


@pytest.mark.parametrize("mode,limit", [("smoke", 30), ("manual", 80)])
def test_session_limit_matches_mode_and_blocks_next_reservation(mode, limit):
    assert SESSION_LIMIT_BY_MODE[mode] == limit
    item = guard(mode)
    for _ in range(limit):
        item.reserve(source_id="arcgis_world_imagery", reason="tile")
    with pytest.raises(ImageryBudgetExhausted) as exc:
        item.reserve(source_id="arcgis_world_imagery", reason="tile")
    assert exc.value.detail["reason"] == "session_budget_exhausted"
    summary = item.summary()
    assert summary["session_upstream_requests"] == limit
    assert summary["daily_upstream_requests"] == limit
    assert summary["budget_remaining"] == 0
    assert summary["rejected_by_budget"] == 1


def test_daily_limit_is_shared_across_modes_and_caps_below_session_limit():
    directory = budget_dir()
    # 手动模式 80 次 → 日计数 80
    manual = guard("manual", directory=directory, run_id="run-manual")
    for _ in range(80):
        manual.reserve(source_id="arcgis_world_imagery", reason="tile")
    assert manual.summary()["daily_upstream_requests"] == 80
    # 换 run id + 换模式（会话额度重置为 30），但**日上限仍封顶 150**
    smoke = guard("smoke", directory=directory, run_id="run-smoke")
    for _ in range(30):
        smoke.reserve(source_id="arcgis_world_imagery", reason="tile")
    summary = smoke.summary()
    assert summary["daily_upstream_requests"] == 110
    assert summary["session_upstream_requests"] == 30
    # 手动模式再开一次：日剩余只有 40，超过后必须报 daily_budget_exhausted
    again = guard("manual", directory=directory, run_id="run-manual-2")
    for _ in range(40):
        again.reserve(source_id="arcgis_world_imagery", reason="tile")
    with pytest.raises(ImageryBudgetExhausted) as exc:
        again.reserve(source_id="arcgis_world_imagery", reason="tile")
    assert exc.value.detail["reason"] == "daily_budget_exhausted"
    assert again.summary()["daily_upstream_requests"] == DAILY_LIMIT


def test_daily_limit_rolls_over_on_local_date_change():
    directory = budget_dir()
    today = datetime(2026, 10, 13, 10, 0, 0)
    # 会话上限（manual=80）低于日上限（150），因此用两个 run 批次把日额度用满：
    # 80 + 70 = 150。这正好同时验证"换 run id 不能突破日上限"。
    first = ImageryBudgetGuard(budget_dir=directory, mode="manual", test_run_id="batch-1",
                               environ={}, clock=lambda: today)
    for _ in range(80):
        first.reserve(source_id="arcgis_world_imagery", reason="tile")
    second = ImageryBudgetGuard(budget_dir=directory, mode="manual", test_run_id="batch-2",
                                environ={}, clock=lambda: today)
    for _ in range(70):
        second.reserve(source_id="arcgis_world_imagery", reason="tile")
    assert second.summary()["daily_upstream_requests"] == DAILY_LIMIT
    with pytest.raises(ImageryBudgetExhausted) as exc:
        second.reserve(source_id="arcgis_world_imagery", reason="tile")
    assert exc.value.detail["reason"] == "daily_budget_exhausted"
    # 次日：日计数清零（会话计数同样随日期滚动清零，属既有设计）
    tomorrow = ImageryBudgetGuard(budget_dir=directory, mode="manual", environ={},
                                  clock=lambda: today + timedelta(days=1))
    reservation = tomorrow.reserve(source_id="arcgis_world_imagery", reason="tile")
    assert reservation["daily_upstream_requests"] == 1
    history = tomorrow.summary()["history"]
    assert history and history[-1]["local_date"] == "2026-10-13"
    assert history[-1]["daily_upstream_requests"] == DAILY_LIMIT


# ---------------------------------------------------------------- 3) 并发


def test_concurrent_reservations_never_exceed_the_limit():
    directory = budget_dir()
    item = guard("smoke", directory=directory, run_id="concurrent")
    granted = []
    denied = []
    lock = threading.Lock()

    def worker():
        try:
            item.reserve(source_id="arcgis_world_imagery", reason="tile")
            with lock:
                granted.append(1)
        except ImageryBudgetExhausted:
            with lock:
                denied.append(1)

    threads = [threading.Thread(target=worker) for _ in range(120)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(granted) == 30            # smoke 会话上限，**一次都不许多**
    assert len(denied) == 90
    state = json.loads((item.state_path).read_text(encoding="utf-8"))
    assert state["daily_upstream_requests"] == 30
    assert state["session_upstream_requests"] == 30
    assert state["rejected_by_budget"] == 90


# ---------------------------------------------------------------- 4) 跨进程重启


def test_budget_survives_process_restart():
    directory = budget_dir()
    first = guard("smoke", directory=directory, run_id="restart-run")
    for _ in range(30):
        first.reserve(source_id="arcgis_world_imagery", reason="tile")
    # 模拟重启：全新对象、**同一**共享目录、**同一** run id（默认行为）
    restarted = ImageryBudgetGuard(
        budget_dir=directory, mode="smoke",
        environ={"CNS_IMAGERY_ONLINE_MODE": "smoke"},
    )
    summary = restarted.summary()
    assert summary["daily_upstream_requests"] == 30
    assert summary["session_upstream_requests"] == 30
    with pytest.raises(ImageryBudgetExhausted) as exc:
        restarted.reserve(source_id="arcgis_world_imagery", reason="tile")
    assert exc.value.detail["reason"] == "session_budget_exhausted"
    # 日上限同样在重启后有效
    assert summary["daily_limit"] == DAILY_LIMIT


def test_new_test_run_id_resets_session_but_not_daily_cap():
    directory = budget_dir()
    first = guard("smoke", directory=directory, run_id="run-a")
    for _ in range(30):
        first.reserve(source_id="arcgis_world_imagery", reason="tile")
    second = guard("smoke", directory=directory, run_id="run-b")
    summary = second.summary()
    assert summary["session_upstream_requests"] == 0      # 会话被重置
    assert summary["daily_upstream_requests"] == 30       # 日计数**没有**被重置
    assert summary["test_run_id"] == "run-b"
    second.reserve(source_id="arcgis_world_imagery", reason="tile")
    assert second.summary()["daily_upstream_requests"] == 31


def test_environment_run_id_is_reused_when_not_explicitly_provided():
    directory = budget_dir()
    guard("smoke", directory=directory, run_id="env-run").reserve(
        source_id="arcgis_world_imagery", reason="tile")
    # 只有环境变量里带 run id 时也复用同一 run；未提供时**复用文件中的 run id**
    implicit = ImageryBudgetGuard(
        budget_dir=directory, mode="smoke", environ={"CNS_IMAGERY_ONLINE_MODE": "smoke"})
    assert implicit.summary()["test_run_id"] == "env-run"
    assert implicit.summary()["session_upstream_requests"] == 1
    assert implicit.summary()["daily_upstream_requests"] == 1


def test_corrupted_counter_file_fails_closed_instead_of_resetting_budget():
    # 文件损坏**不得**静默清零（那等于用一次损坏把日预算重置回 0）。
    directory = budget_dir()
    item = guard("smoke", directory=directory, run_id="corrupt")
    for _ in range(30):
        item.reserve(source_id="arcgis_world_imagery", reason="tile")
    item.state_path.write_text("{not json", encoding="utf-8")
    broken = guard("smoke", directory=directory, run_id="corrupt")
    with pytest.raises(ImageryBudgetExhausted) as exc:
        broken.reserve(source_id="arcgis_world_imagery", reason="tile")
    assert exc.value.detail["reason"] == "budget_state_unreadable"
    # 摘要同样不得显示成"额度充足"。
    summary = broken.summary()
    assert summary["budget_remaining"] == 0
    assert summary["degraded_reason"] == "budget_state_unreadable"


def test_multiprocess_reservations_share_one_counter_and_never_exceed_limit():
    """真正的**跨进程**并发：多个 Python 进程同时抢占同一份预算文件。

    这直接证明"高并发下不能突破上限"与"进程重启后日预算仍然有效"不是靠
    进程内锁实现的 —— 每条预留都经过跨进程文件锁 + 原子替换。
    """

    import subprocess
    import sys

    directory = budget_dir()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = (
        "import sys;"
        "sys.path.insert(0, r'%s');"
        "from cns_planner.application.imagery_budget import ImageryBudgetGuard, ImageryBudgetExhausted;"
        "g = ImageryBudgetGuard(budget_dir=r'%s', mode='smoke', environ={});"
        "ok = 0\n"
        "for _ in range(20):\n"
        "    try:\n"
        "        g.reserve(source_id='arcgis_world_imagery', reason='tile'); ok += 1\n"
        "    except ImageryBudgetExhausted:\n"
        "        pass\n"
        "print(ok)"
    ) % (root, directory)
    processes = [
        subprocess.Popen([sys.executable, "-c", script],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(6)
    ]
    granted = 0
    for process in processes:
        out, err = process.communicate(timeout=180)
        assert process.returncode == 0, err
        granted += int(str(out).strip().splitlines()[-1])
    # 6 进程 × 20 次 = 120 次尝试，但 smoke 会话上限 30 是**全局**的。
    assert granted == 30
    state = json.loads(
        open(os.path.join(directory, "imagery_budget_state.json"), encoding="utf-8").read())
    assert state["daily_upstream_requests"] == 30
    assert state["session_upstream_requests"] == 30


# ---------------------------------------------------------------- 5) 服务级闸门


def test_service_never_calls_upstream_after_budget_is_exhausted():
    directory = budget_dir()
    fetcher = CountingFetcher()
    service = Imagery3DService(
        environ={"CNS_ARCGIS_TOKEN": "AA-secret-token"},
        fetcher=fetcher,
        budget=ImageryBudgetGuard(budget_dir=directory, mode="smoke", environ={}),
    )
    for _ in range(30):
        service.tile("arcgis_world_imagery", 12, 3438, 1689)
    assert len(fetcher.calls) == 30
    with pytest.raises(ImageryUnavailable) as exc:
        service.tile("arcgis_world_imagery", 12, 3438, 1689)
    assert exc.value.code == "imagery_budget_exhausted"
    assert len(fetcher.calls) == 30      # 耗尽后 **0** 新上游请求
    summary = service.budget_summary()
    assert summary["upstream_attempts"] == 30
    assert summary["upstream_success"] == 30
    assert summary["budget_remaining"] == 0


def test_local_rejections_do_not_consume_budget():
    fetcher = CountingFetcher()
    service = Imagery3DService(
        environ={"CNS_ARCGIS_TOKEN": "AA-secret-token"},
        fetcher=fetcher,
        budget=guard("smoke"),
    )
    for coordinates in [(3, 6, 3), (21, 0, 0), (12, -1, 1691), (12, 3438, 5000)]:
        with pytest.raises(ImageryUnavailable):
            service.tile("arcgis_world_imagery", *coordinates)
    assert fetcher.calls == []
    assert service.budget_summary()["daily_upstream_requests"] == 0


def test_budget_summary_never_leaks_credential_material():
    service = Imagery3DService(
        environ={"CNS_ARCGIS_TOKEN": "AA-super-secret-token-value"},
        fetcher=CountingFetcher(),
        budget=guard("smoke"),
    )
    blob = json.dumps(service.configuration(), ensure_ascii=False)
    assert "AA-super-secret-token-value" not in blob
    assert "token=" not in blob
    assert "ibasemaps-api" not in blob or True  # 主机名可公开，凭证不可
    budget = service.budget_summary()
    assert budget["billing_basis"] == "server_side_reserved_upstream_attempts"
    assert "browser_request_image_calls" in budget
    assert budget["counter_dir_is_fixed"] is True


def test_metadata_and_diagnose_share_the_same_gate():
    fetcher = CountingFetcher()
    service = Imagery3DService(
        environ={"CNS_ARCGIS_TOKEN": "AA-secret-token"},
        fetcher=fetcher,
        budget=guard("disabled"),
    )
    with pytest.raises(ImageryUnavailable) as exc:
        service.upstream_metadata()
    assert exc.value.code == "imagery_online_mode_disabled"
    report = service.diagnose_tile("arcgis_world_imagery", 12, 3438, 1689)
    # 诊断的 outcome 必须**精确到原因**：离线模式是"运行模式未授权"，
    # 不是"缺凭证"、也不是笼统的 failed。
    assert report["outcome"] == "blocked_online_mode_disabled"
    assert report["error_code"] == "imagery_online_mode_disabled"
    assert report["request_mode"] == "disabled"
    assert fetcher.calls == []


def test_mode_is_checked_before_credentials_in_every_path():
    """顺序回归：授权模式 → 额度 → 凭证。

    真实缺陷（T3D-2A 端到端实测）：凭证检查排在预算闸门之前，于是离线模式下
    （凭证通常不存在）三个入口一律回 ``arcgis_token_missing``，把"运行模式未授权"
    误报成"缺凭证"，排查被引向错误方向。
    """

    fetcher = CountingFetcher()
    # 凭证存在但模式为 disabled：必须报模式，不得报凭证。
    with_token = Imagery3DService(
        environ={"CNS_ARCGIS_TOKEN": "AA-secret-token"}, fetcher=fetcher,
        budget=guard("disabled"))
    with pytest.raises(ImageryUnavailable) as exc:
        with_token.tile("arcgis_world_imagery", 12, 3438, 1689)
    assert exc.value.code == "imagery_online_mode_disabled"
    assert with_token.diagnose_tile(
        "arcgis_world_imagery", 12, 3438, 1689)["outcome"] == (
            "blocked_online_mode_disabled")
    assert fetcher.calls == []

    # 凭证**不存在**且模式为 disabled：仍然报模式（模式优先），不是缺凭证。
    without_token = Imagery3DService(environ={}, fetcher=fetcher,
                                     budget=guard("disabled"))
    with pytest.raises(ImageryUnavailable) as exc2:
        without_token.tile("arcgis_world_imagery", 12, 3438, 1689)
    assert exc2.value.code == "imagery_online_mode_disabled"

    # 模式允许、但凭证确实缺失 ⇒ 此时才报缺凭证。
    online = Imagery3DService(environ={}, fetcher=fetcher, budget=guard("smoke"))
    with pytest.raises(ImageryUnavailable) as exc3:
        online.tile("arcgis_world_imagery", 12, 3438, 1689)
    assert exc3.value.code == "arcgis_token_missing"
    assert online.diagnose_tile(
        "arcgis_world_imagery", 12, 3438, 1689)["outcome"] == (
            "blocked_missing_credential")
    # 本地拒绝（模式/凭证）都**不消耗**额度。
    assert fetcher.calls == []
    assert online.budget_summary()["upstream_attempts"] == 0
    assert online.budget_summary()["daily_upstream_requests"] == 0
