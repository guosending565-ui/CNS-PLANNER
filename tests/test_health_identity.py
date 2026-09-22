"""/api/health 运行身份与身份构造的独立测试（BUG-STARTUP-001），不需要 QGIS。"""
from datetime import datetime
import json
import os
from pathlib import Path

from cns_planner.api.router import ApiRouter
from cns_planner import process_identity


class FakeData:
    error = ""


class FakeWorkflow:
    state = {"revision": 3}


class FakeContext:
    """只提供 /api/health 需要的字段，避免引入 QGIS 运行时。"""

    def __init__(self, identity=None):
        self.workflow = FakeWorkflow()
        self.data = FakeData()
        if identity is not None:
            self.health_identity = identity


def health_payload(context):
    response = ApiRouter(context).get("/api/health", {}, {})
    body = response.data
    return json.loads(body) if isinstance(body, (str, bytes)) else body


def test_health_keeps_existing_contract_and_adds_identity(tmp_path):
    identity = process_identity.build_identity(tmp_path)
    payload = health_payload(FakeContext(identity))
    assert payload["service"] == "cns-map"
    assert payload["ready"] is True
    assert payload["data_error"] == ""
    assert payload["pid"] == os.getpid()
    assert Path(payload["project_root"]) == tmp_path
    assert {"pid", "started_at", "project_root", "git_commit"} <= set(payload)


def test_health_works_without_identity_provider():
    payload = health_payload(FakeContext())
    assert payload == {"service": "cns-map", "ready": True, "data_error": ""}


def test_identity_uses_real_process_data(tmp_path):
    identity = process_identity.build_identity(tmp_path)
    assert identity["pid"] == os.getpid()
    assert datetime.fromisoformat(identity["started_at"]).tzinfo is not None
    assert Path(identity["project_root"]) == tmp_path


def test_identity_commit_comes_from_launcher_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(process_identity.ENV_GIT_COMMIT, "deadbeef")
    assert process_identity.build_identity(tmp_path)["git_commit"] == "deadbeef"
    monkeypatch.delenv(process_identity.ENV_GIT_COMMIT)
    assert process_identity.build_identity(tmp_path)["git_commit"] == ""


def test_process_identity_is_stable_within_one_process(tmp_path):
    first = process_identity.process_identity(tmp_path)
    assert process_identity.process_identity(tmp_path) is first
    assert first["pid"] == os.getpid()
