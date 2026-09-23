"""launcher/health 的独立测试（BUG-STARTUP-001 / BUG-STARTUP-002），不需要真实 QGIS。"""
import json
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


def test_stale_backend_of_the_same_project_is_reported_with_restart_diagnostic(monkeypatch):
    monkeypatch.setattr(map_app, "port_in_use", lambda *a, **k: True)
    monkeypatch.setattr(map_app, "probe_health", lambda: make_probe(git_commit="old-commit"))
    monkeypatch.setattr(map_app, "git_commit", lambda: "new-commit")
    launched = []
    monkeypatch.setattr(launcher_process.ProcessTree, "launch",
                        lambda self, *a, **k: launched.append(a) or self)
    try:
        map_app.ensure_server(timeout=1)
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("同项目旧版本后端不得被复用")
    assert "旧版本" in message and "重启" in message
    assert "old-commit" in message and "new-commit" in message
    assert launched == []


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
