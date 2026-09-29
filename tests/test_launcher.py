"""launcher/health 的独立测试（BUG-STARTUP-001 / -002 / -003），不需要真实 QGIS。"""
import json
import os
import subprocess
import sys

import launcher_process
import map_app


def make_probe(**overrides):
    fields = {
        "occupied": True, "service": "cns-map", "ready": True,
        "project_root": str(map_app.ROOT), "pid": 4321,
        "started_at": "2026-01-01T00:00:00+00:00", "git_commit": map_app.git_commit(),
        "data_error": None,
    }
    fields.update(overrides)
    return map_app.HealthProbe(**fields)


def identity(commit):
    """显式身份，避免测试结果依赖当前 git 工作树的真实状态。"""

    return {"project_root": str(map_app.ROOT), "git_commit": commit}


def test_identity_match_allows_reuse():
    assert map_app.health_matches(make_probe())


def test_identity_match_is_case_and_separator_insensitive():
    weird = str(map_app.ROOT).replace("\\", "/").swapcase()
    assert map_app.health_matches(make_probe(project_root=weird))


def test_identity_mismatch_is_rejected():
    assert not map_app.health_matches(make_probe(project_root=r"C:\other\project"))


def test_missing_project_root_is_rejected():
    # 旧版本后端不上报 project_root：不得仅凭 service 名复用。
    assert not map_app.health_matches(make_probe(project_root=None))


def test_foreign_service_and_not_ready_are_rejected():
    assert not map_app.health_matches(make_probe(service="other-map"))
    assert not map_app.health_matches(make_probe(ready=False))


def test_free_port_is_not_occupied():
    assert not map_app.health_matches(make_probe(occupied=False))


def test_probe_reports_non_http_occupant(monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(map_app, "urlopen", refuse)
    probe = map_app.probe_health()
    assert probe.occupied and probe.service is None
    assert not map_app.health_matches(probe)


def test_probe_reads_identity_fields(monkeypatch):
    seen = {}
    payload = {"service": "cns-map", "ready": True, "pid": 77, "started_at": "t",
               "project_root": str(map_app.ROOT), "git_commit": map_app.git_commit()}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(payload).encode("utf-8")

    def fake_urlopen(url, timeout=None):
        seen["url"] = url
        return FakeResponse()

    monkeypatch.setattr(map_app, "urlopen", fake_urlopen)
    probe = map_app.probe_health()
    assert seen["url"].endswith("/api/health")
    assert (probe.pid, probe.started_at) == (77, "t")
    assert probe.git_commit == map_app.git_commit()
    assert map_app.health_matches(probe)


# ---------------------------------------------------------------- BUG-STARTUP-002

def test_same_root_and_same_commit_allows_reuse():
    assert map_app.health_matches(make_probe(git_commit="commit-a"), identity("commit-a")) is True


def test_same_root_with_different_commit_is_rejected():
    assert map_app.health_matches(make_probe(git_commit="commit-a"), identity("commit-b")) is False


def test_same_root_with_unknown_running_commit_is_rejected():
    # 同目录、但后端未上报 commit（旧版本后端）：身份未知，不得静默复用。
    assert map_app.health_matches(make_probe(git_commit=None), identity("commit-b")) is False
    assert map_app.health_matches(make_probe(git_commit="commit-a"), identity("")) is False


def test_both_sides_without_commit_stay_reusable():
    # 非 git 工作树：双方都没有可比较的 commit 时退化为"不比较"，保证无 git 环境可用。
    assert map_app.commit_identity_matches("", "") is True
    assert map_app.health_matches(make_probe(git_commit=""), identity("")) is True


# ------------------------------------------------- BUG-STARTUP-003：受控自动替换

def test_stale_backend_of_the_same_project_is_replaced_automatically(monkeypatch):
    """同仓库旧版本后端：只结束 health 指认的 pid → 等端口释放 → 启动当前 HEAD。"""
    events = []
    monkeypatch.setattr(map_app, "port_in_use", lambda *a, **k: True)
    monkeypatch.setattr(
        map_app, "probe_health",
        lambda: make_probe(git_commit="old-commit", pid=4321, started_at="2026-01-01T00:00:00+00:00"),
    )
    monkeypatch.setattr(map_app, "git_commit", lambda: "new-commit")
    monkeypatch.setattr(
        launcher_process, "terminate_verified_process",
        lambda pid, timeout=5.0: events.append(("terminate", pid)) or True,
    )
    monkeypatch.setattr(
        map_app, "wait_for_port_release",
        lambda *a, **k: events.append(("wait-port",)) or True,
    )

    class FakeTree:
        def close(self):
            events.append(("close",))

    tree = FakeTree()
    monkeypatch.setattr(
        map_app, "start_managed_server",
        lambda **k: events.append(("start",)) or tree,
    )
    assert map_app.ensure_server(timeout=5) is tree
    # 严格串行：终止 → 等端口释放 → 启动新服务；顺序不可交换。
    assert events == [("terminate", 4321), ("wait-port",), ("start",)]


def test_verified_pid_requires_full_identity():
    expected = identity("new-commit")
    assert map_app.verified_pid(make_probe(git_commit="old-commit", pid=4321), expected) == 4321
    assert map_app.verified_pid(make_probe(git_commit="old-commit", pid="4321"), expected) == 4321
    # 任何一条身份不成立都不允许自动结束进程（fail-closed）。
    for broken in (
        make_probe(git_commit="old-commit", pid=0),
        make_probe(git_commit="old-commit", pid=-3),
        make_probe(git_commit="old-commit", pid="abc"),
        make_probe(git_commit="old-commit", pid=None),
        make_probe(git_commit="old-commit", pid=True),
        make_probe(git_commit="old-commit", ready=False),
        make_probe(git_commit="old-commit", service="other-map"),
        make_probe(git_commit="old-commit", project_root=r"C:\other\project"),
        make_probe(git_commit="old-commit", pid=os.getpid()),
    ):
        assert map_app.verified_pid(broken, expected) is None


def test_stale_backend_without_ready_is_refused_without_terminating(monkeypatch):
    """同仓库、但对方未上报 ready：身份不足以安全重启，绝不结束它。"""
    monkeypatch.setattr(map_app, "port_in_use", lambda *a, **k: True)
    monkeypatch.setattr(map_app, "probe_health",
                        lambda: make_probe(git_commit="old-commit", ready=False, pid=4321))
    monkeypatch.setattr(map_app, "git_commit", lambda: "new-commit")
    terminated = []
    monkeypatch.setattr(launcher_process, "terminate_verified_process",
                        lambda *a, **k: terminated.append(a) or True)
    try:
        map_app.ensure_server(timeout=1)
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("身份不足以安全自动重启时必须 fail-closed")
    assert "旧版本" in message and "old-commit" in message and "new-commit" in message
    assert "不会结束身份不明确的进程" in message
    assert terminated == []


def test_stale_backend_port_not_released_stops_without_further_kills(monkeypatch):
    """端口未释放：只报错，绝不继续结束任何进程（包括新出现的占用者）。"""
    probes = [
        make_probe(git_commit="old-commit", pid=4321),
        make_probe(git_commit="another-commit", pid=9999),
    ]
    monkeypatch.setattr(map_app, "port_in_use", lambda *a, **k: True)
    monkeypatch.setattr(map_app, "probe_health", lambda: probes.pop(0) if probes else make_probe())
    monkeypatch.setattr(map_app, "git_commit", lambda: "new-commit")
    killed = []
    monkeypatch.setattr(launcher_process, "terminate_verified_process",
                        lambda pid, *a, **k: killed.append(pid) or True)
    monkeypatch.setattr(map_app, "wait_for_port_release", lambda *a, **k: False)

    def forbid_start(**k):
        raise AssertionError("8765 未释放时不得启动新服务")

    monkeypatch.setattr(map_app, "start_managed_server", forbid_start)
    try:
        map_app.ensure_server(timeout=1)
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("端口未释放必须报错")
    assert "旧 CNS 后端已请求关闭，但 8765 仍被占用" in message
    assert killed == [4321]


def test_terminate_verified_process_refuses_invalid_targets():
    """底层终止动作只接受明确的正整数 pid，且永不结束自己。"""
    assert launcher_process.terminate_verified_process(0) is False
    assert launcher_process.terminate_verified_process(-1) is False
    assert launcher_process.terminate_verified_process("abc") is False
    assert launcher_process.terminate_verified_process(None) is False
    assert launcher_process.terminate_verified_process(os.getpid()) is False


def test_terminate_verified_process_ends_only_the_named_pid():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert launcher_process.terminate_verified_process(child.pid) is True
        assert child.wait(timeout=10) is not None
    finally:
        if child.poll() is None:
            child.kill()


def test_ready_server_is_reused(monkeypatch):
    monkeypatch.setattr(map_app, "port_in_use", lambda *a, **k: True)
    monkeypatch.setattr(map_app, "probe_health", lambda: make_probe())
    launched = []
    monkeypatch.setattr(launcher_process.ProcessTree, "launch",
                        lambda self, *a, **k: launched.append(a) or self)
    assert map_app.ensure_server() is None
    assert launched == []


def test_server_of_another_project_is_rejected(monkeypatch):
    monkeypatch.setattr(map_app, "port_in_use", lambda *a, **k: True)
    monkeypatch.setattr(map_app, "probe_health",
                        lambda: make_probe(project_root=r"C:\other\project"))
    launched = []
    monkeypatch.setattr(launcher_process.ProcessTree, "launch",
                        lambda self, *a, **k: launched.append(a) or self)
    try:
        map_app.ensure_server(timeout=1)
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("必须拒绝复用其他项目的服务")
    assert r"C:\other\project" in message
    assert launched == []


def test_unknown_occupant_is_rejected(monkeypatch):
    monkeypatch.setattr(map_app, "port_in_use", lambda *a, **k: True)
    monkeypatch.setattr(map_app, "probe_health", lambda: make_probe(service=None, ready=None))
    try:
        map_app.ensure_server(timeout=1)
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("必须拒绝复用未知占用者")
    assert "8765" in message


def test_ensure_server_starts_managed_tree(monkeypatch, tmp_path):
    monkeypatch.setattr(map_app, "port_in_use", lambda *a, **k: False)
    monkeypatch.setattr(map_app, "LOG", tmp_path / "map-server.log")
    monkeypatch.setattr(map_app, "find_runner", lambda: tmp_path / "python-qgis.bat")
    monkeypatch.setattr(map_app, "git_commit", lambda: "deadbeef")
    calls = {"launch": [], "suspend": [], "env": [], "closed": 0, "ready": 0}

    class FakeTree:
        def __init__(self):
            self._poll = None

        def launch(self, command, cwd, env, stdout, suspend=False):
            calls["launch"].append([str(part) for part in command])
            calls["suspend"].append(suspend)
            calls["env"].append(dict(env))
            return self

        def poll(self):
            return self._poll

        def close(self):
            calls["closed"] += 1

    def ready():
        calls["ready"] += 1
        return calls["ready"] > 1

    monkeypatch.setattr(launcher_process, "ProcessTree", FakeTree)
    monkeypatch.setattr(map_app, "is_ready", ready)
    tree = map_app.ensure_server(timeout=10)
    assert isinstance(tree, FakeTree)
    assert calls["launch"][0][-2:] == ["-m", "cns_planner.map_server"]
    assert calls["suspend"] == [True]
    # 身份注入：后端把 HEAD 回报到 /api/health 的 git_commit
    assert calls["env"][0]["CNS_LAUNCHER_GIT_COMMIT"] == "deadbeef"
    assert calls["closed"] == 0


def test_failed_start_closes_its_own_tree(monkeypatch, tmp_path):
    monkeypatch.setattr(map_app, "port_in_use", lambda *a, **k: False)
    monkeypatch.setattr(map_app, "LOG", tmp_path / "map-server.log")
    monkeypatch.setattr(map_app, "find_runner", lambda: tmp_path / "python-qgis.bat")
    monkeypatch.setattr(map_app, "is_ready", lambda: False)
    calls = {"closed": 0}

    class FakeTree:
        def launch(self, command, cwd, env, stdout, suspend=False):
            return self

        def poll(self):
            return None

        def close(self):
            calls["closed"] += 1

    monkeypatch.setattr(launcher_process, "ProcessTree", FakeTree)
    try:
        map_app.ensure_server(timeout=0.3)
    except RuntimeError as exc:
        assert "超时" in str(exc)
    else:
        raise AssertionError("启动超时必须报错")
    assert calls["closed"] == 1


def test_launcher_uses_package_entrypoint():
    from pathlib import Path
    source = Path(map_app.__file__).read_text(encoding="utf-8")
    assert "cns_planner.map_server" in source
