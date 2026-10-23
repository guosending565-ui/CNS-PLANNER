"""Isolated transport regressions; no QGIS, project files or listening ports."""
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
import threading
from types import SimpleNamespace
import pytest
from cns_planner.api.server import ApiHandler


def post(context, revision, path="/api/project/open"):
    handler = object.__new__(ApiHandler)
    handler.server = SimpleNamespace(context=context)
    handler.path = path
    raw = json.dumps({"project_dir": "fixture"}).encode()
    handler.headers = {"Content-Length": str(len(raw)), "X-CNS-Token": "token",
                       "X-CNS-Revision": str(revision)}
    handler.rfile = BytesIO(raw)
    handler.allowed = lambda: True
    result = []
    handler.respond = lambda data, *args, **kw: result.append((
        data, kw.get("status", args[1] if len(args)>1 else 200), kw.get("headers", {})))
    handler.do_POST()
    return result[0]


@pytest.fixture
def context():
    with ThreadPoolExecutor(max_workers=1) as gis:
        ctx = SimpleNamespace(token="token", mutation_lock=threading.RLock(),
                              workflow=SimpleNamespace(state={"revision": 7, "project": "old"}))
        ctx.qgis = SimpleNamespace(call=lambda action: gis.submit(action).result(timeout=3))
        def switch(folder):
            with ctx.mutation_lock:
                ctx.workflow = SimpleNamespace(state={"revision": 8, "project": folder})
                return dict(ctx.workflow.state)
        ctx.open_project = ctx.save_project_as = switch
        yield ctx


@pytest.mark.parametrize("path", ["/api/project/open", "/api/project/save-as"])
def test_switch_snapshot_reenters_lock_and_response_is_consistent(context, path):
    data, status, headers = post(context, 7, path)
    assert status == 200
    assert data == context.workflow.state == {"revision": 8, "project": "fixture"}
    assert headers["X-CNS-Revision"] == data["revision"]


def test_stale_revision_has_no_project_side_effect(context):
    old = context.workflow
    context.open_project = lambda _: pytest.fail("stale request reached project writer")
    data, status, _ = post(context, 6)
    assert status == 409 and data["revision"] == 7
    assert context.workflow is old


def test_revision_is_checked_after_queued_gis_work(context):
    original = context.qgis.call
    def intervening(action):
        with context.mutation_lock:
            context.workflow.state["revision"] = 9
        return original(action)
    context.qgis.call = intervening
    context.open_project = lambda _: pytest.fail("accepted stale queued switch")
    data, status, _ = post(context, 7)
    assert status == 409 and data["revision"] == 9
    assert context.workflow.state["project"] == "old"


def test_concurrent_switches_only_one_accepts_same_revision(context):
    start = threading.Barrier(2)
    def run():
        start.wait(timeout=3)
        return post(context, 7)
    with ThreadPoolExecutor(max_workers=2) as clients:
        results = list(clients.map(lambda _: run(), range(2)))
    assert sorted(r[1] for r in results) == [200, 409]
    accepted = next(r for r in results if r[1] == 200)
    assert accepted[0] == context.workflow.state
    assert accepted[2]["X-CNS-Revision"] == 8


def test_failed_switch_preserves_project(context):
    old = context.workflow
    def fail(_):
        raise ValueError("invalid fixture project")
    context.open_project = fail
    _, status, _ = post(context, 7)
    assert status == 400
    assert context.workflow is old
