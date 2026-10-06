"""Round32-D targeted contracts for background layered-route validation."""

from copy import deepcopy
import os
from pathlib import Path

import pytest

from cns_planner.api.router import ApiRouter
from cns_planner.gis import layered_validation_adapter
from cns_planner.tasks.handlers import RESULT_SCOPE_TEXT, current_input_fingerprint
from cns_planner.tasks.task_specs import (
    LAYERED_ROUTE_VALIDATION_TASK_TYPE,
    TASK_ENDPOINTS,
    _WorkerReadOnlySession,
    _layered_route_validation_inputs,
    task_spec,
)

from test_layered_route_validation_adoption import evidence, prepared
from test_round32c_step03_background_tasks import Step03Runtime, _InlineWorkerProcess


ENDPOINT = "/api/layered-route-validations/evaluate-real"


class _Data:
    def __init__(self, paths):
        self.paths = paths


class _ApiContext:
    def __init__(self, workflow, heavy_tasks, paths):
        self.workflow = workflow
        self.heavy_tasks = heavy_tasks
        self.data = _Data(paths)

        class _Qgis:
            @staticmethod
            def call(callback):
                return callback()

        self.qgis = _Qgis()


def _runtime_api(runtime, paths):
    return ApiRouter(_ApiContext(runtime.workflow, runtime.service, paths))


def _background_project(tmp_path):
    workflow, candidate, profile = prepared(tmp_path)
    workflow.state["layered_route_candidates"] = {
        "schema_version": "layered-route-candidate-v1",
        "status": "passed",
        "count": 1,
        "active_candidate_id": candidate["candidate_id"],
        "items": [deepcopy(candidate)],
        "masks": {},
    }
    profile = {
        **deepcopy(profile),
        "fingerprints": {
            "profile_fingerprint": "profile-fp",
            "candidate_fingerprint": candidate["candidate_fingerprint"],
        },
    }
    workflow.state["route_risk_profiles"] = {
        "schema_version": "route-risk-profile-v1",
        "status": "passed",
        "count": 1,
        "items": [profile],
    }
    workflow.state["restricted_areas"] = {
        "schema_version": 1,
        "status": "resolved",
        "source": None,
        "domain_states": {
            "airspace": {"status": "confirmed_none", "source": "test"},
            "critical_site": {"status": "confirmed_none", "source": "test"},
        },
        "count": 0,
        "items": [],
    }
    terrain = tmp_path / "fabdem.tif"
    buildings = tmp_path / "buildings.gpkg"
    terrain.write_bytes(b"terrain-v1")
    buildings.write_bytes(b"buildings-v1")
    workflow.save()
    return workflow, candidate, profile, {
        "terrain_dtm": str(terrain),
        "buildings": str(buildings),
    }


def _stub_factory(**_frozen):
    return evidence()


def _start_runtime(tmp_path, workflow, monkeypatch):
    monkeypatch.setattr(
        layered_validation_adapter,
        "build_layered_validation_evidence_adapter",
        _stub_factory,
    )
    return Step03Runtime(tmp_path, workflow, spawn=_InlineWorkerProcess).start()


def _current_validation(workflow):
    items = workflow.layered_route_validation_service.result_snapshot()["items"]
    return next(item for item in reversed(items) if item.get("current_applicability") == "current")


def test_validation_endpoint_is_registered_as_a_heavy_task():
    assert TASK_ENDPOINTS[ENDPOINT] == LAYERED_ROUTE_VALIDATION_TASK_TYPE
    spec = task_spec(LAYERED_ROUTE_VALIDATION_TASK_TYPE)
    assert spec.task_name == "航路连续安全验证"
    assert RESULT_SCOPE_TEXT[LAYERED_ROUTE_VALIDATION_TASK_TYPE] == "航路连续安全验证"


def test_validation_plan_has_zero_canonical_side_effects(tmp_path):
    workflow, _candidate, _profile, _paths = _background_project(tmp_path)
    before = deepcopy(workflow.state)
    service = workflow.layered_route_validation_service

    class _FailOnSave:
        def __init__(self, state):
            self.state = state

        def save(self):
            raise AssertionError("plan must not save")

    original = service.session
    service.session = _FailOnSave(original.state)
    try:
        record = service.plan(
            {"horizontal_crs": "EPSG:32651"}, evidence_adapter=evidence(),
        )
    finally:
        service.session = original
    assert record["status"] == "validated_candidate"
    assert workflow.state == before
    with pytest.raises(RuntimeError, match="绝不写 canonical"):
        _WorkerReadOnlySession({}).save()


def test_validation_snapshot_is_minimal_and_freezes_runtime_sources(tmp_path):
    workflow, candidate, profile, paths = _background_project(tmp_path)
    workflow.state["layered_route_candidates"]["masks"] = {
        "R-1@L-100": {"cells": {str(i): {"value": i} for i in range(1000)}}
    }
    inputs = _layered_route_validation_inputs(
        workflow.state,
        {"horizontal_crs": "EPSG:32651", "data_source_paths": paths},
    )
    assert "layered_route_candidates" not in inputs
    assert inputs["candidate"]["candidate_id"] == candidate["candidate_id"]
    assert inputs["candidate"]["path"] == candidate["path"]
    assert inputs["route_risk_profile"] == {
        "profile_id": profile["profile_id"],
        "profile_fingerprint": "profile-fp",
        "candidate_id": candidate["candidate_id"],
        "candidate_fingerprint": candidate["candidate_fingerprint"],
        "status": "passed",
        "current_applicability": "current",
    }
    assert inputs["data_source_paths"] == paths
    assert inputs["source_identities"]["terrain_dtm"]["size_bytes"] == len(b"terrain-v1")
    assert inputs["source_identities"]["buildings"]["size_bytes"] == len(b"buildings-v1")
    assert inputs["restricted_areas"] == workflow.state["restricted_areas"]


def test_validation_async_submit_publishes_current_and_matches_sync(
    tmp_path, monkeypatch,
):
    workflow, _candidate, _profile, paths = _background_project(tmp_path)
    sync_record = workflow.layered_route_validation_service.plan(
        {"horizontal_crs": "EPSG:32651"}, evidence_adapter=evidence(),
    )
    runtime = _start_runtime(tmp_path, workflow, monkeypatch)
    try:
        response = _runtime_api(runtime, paths).post(
            ENDPOINT, {"async": True, "horizontal_crs": "EPSG:32651"},
        )
        assert response.status == 202, response.data
        final = runtime.wait(response.data["task_id"])
        assert final["advanced"]["status"] == "succeeded", final
        assert final["heartbeat_at"]
    finally:
        runtime.stop()
    current = _current_validation(workflow)
    assert current["status"] == sync_record["status"] == "validated_candidate"
    assert current["fingerprints"] == sync_record["fingerprints"]
    assert current["current_applicability"] == "current"


@pytest.mark.parametrize("changed", ["candidate", "profile", "source"])
def test_changed_validation_input_is_stale_and_never_published(
    tmp_path, monkeypatch, changed,
):
    workflow, _candidate, _profile, paths = _background_project(tmp_path)
    before = deepcopy(workflow.state["layered_route_validations"])
    runtime = _start_runtime(tmp_path, workflow, monkeypatch)
    try:
        response = _runtime_api(runtime, paths).post(
            ENDPOINT, {"async": True, "horizontal_crs": "EPSG:32651"},
        )
        assert response.status == 202, response.data
        record = runtime.store.find(response.data["task_id"])
        if changed == "candidate":
            workflow.state["layered_route_candidates"]["items"][0]["candidate_fingerprint"] = "changed"
        elif changed == "profile":
            workflow.state["route_risk_profiles"]["items"][0]["fingerprints"]["profile_fingerprint"] = "changed"
        else:
            Path(paths["terrain_dtm"]).write_bytes(b"terrain-v2-longer")
            os.utime(paths["terrain_dtm"], None)
        workflow.save()
        assert current_input_fingerprint(workflow, record) != record["input_fingerprint"]
        final = runtime.wait(response.data["task_id"])
        assert final["advanced"]["status"] == "stale", final
    finally:
        runtime.stop()
    assert workflow.state["layered_route_validations"] == before


def test_cancelled_validation_never_changes_canonical_state(tmp_path, monkeypatch):
    workflow, _candidate, _profile, paths = _background_project(tmp_path)
    before = deepcopy(workflow.state["layered_route_validations"])

    def blocking_factory(**_kwargs):
        def adapter(**kwargs):
            kwargs["cancel_check"]()
            return evidence()(**kwargs)
        return adapter

    monkeypatch.setattr(
        layered_validation_adapter,
        "build_layered_validation_evidence_adapter",
        blocking_factory,
    )
    runtime = Step03Runtime(tmp_path, workflow, spawn=_InlineWorkerProcess).start()
    try:
        api = _runtime_api(runtime, paths)
        response = api.post(ENDPOINT, {"async": True, "horizontal_crs": "EPSG:32651"})
        api.post("/api/tasks/cancel", {"task_id": response.data["task_id"]})
        final = runtime.wait(response.data["task_id"])
        assert final["advanced"]["status"] != "succeeded", final
    finally:
        runtime.stop()
    assert workflow.state["layered_route_validations"] == before


def test_apply_computed_stales_old_validation_and_invalidates_adoption_once(tmp_path):
    workflow, _candidate, _profile, _paths = _background_project(tmp_path)
    service = workflow.layered_route_validation_service
    first = service.plan({"horizontal_crs": "EPSG:32651"}, evidence_adapter=evidence(source_suffix="a"))
    service.apply_computed(first)
    invalidated = []
    service.adoption_invalidator = lambda ids, reason: invalidated.append((ids, reason))
    second = service.plan({"horizontal_crs": "EPSG:32651"}, evidence_adapter=evidence(source_suffix="b"))
    service.apply_computed(second)
    items = service.result_snapshot()["items"]
    old = next(item for item in items if item["validation_id"] == first["validation_id"])
    assert old["status"] == "stale"
    assert old["stale_reason"] == "validation_dependencies_changed"
    assert invalidated == [([first["validation_id"]], "validation_replaced_or_no_longer_validated")]


def test_preview_returns_the_frozen_validation_fingerprint_for_apply(tmp_path):
    """Preview 必须把当前 validation fingerprint 一并冻结：Apply 只被允许提交它。

    真实链路的 apply 用 ``None`` 跳过指纹比对时，"Preview 之后证据变化"这层防护
    实际失效；前端的 preview 缓存按同一字段读取，因此两者必须是同一个身份。
    """

    workflow, _candidate, _profile, _paths = _background_project(tmp_path)
    service = workflow.layered_route_validation_service
    service.apply_computed(service.plan({}, evidence_adapter=evidence()))
    validation = _current_validation(workflow)
    fingerprint = validation["fingerprints"]["validation_fingerprint"]

    preview = workflow.preview_layered_operational_adoption({
        "validation_id": validation["validation_id"],
    })
    assert preview["projection"]["validation_id"] == validation["validation_id"]
    assert preview["validation_fingerprint"] == fingerprint
    assert preview["projection"]["validation_fingerprint"] == fingerprint

    with pytest.raises(ValueError, match="fingerprint"):
        workflow.apply_layered_operational_adoption({
            "validation_id": validation["validation_id"], "confirmed": True,
            "expected_validation_fingerprint": "layeredvalidationv1-expired",
        })
    applied = workflow.apply_layered_operational_adoption({
        "validation_id": validation["validation_id"], "confirmed": True,
        "expected_validation_fingerprint": preview["validation_fingerprint"],
    })
    assert applied["status"] == "passed"


def test_projection_and_preview_share_the_same_frozen_identity(tmp_path):
    """projection（Apply 的 gate）与 preview 必须给出同一个指纹身份，避免两条读取路径漂移。"""

    workflow, _candidate, _profile, _paths = _background_project(tmp_path)
    service = workflow.layered_route_validation_service
    service.apply_computed(service.plan({}, evidence_adapter=evidence()))
    validation = _current_validation(workflow)
    payload = {"validation_id": validation["validation_id"]}
    projection = workflow.project_layered_operational_adoption(payload)
    preview = workflow.preview_layered_operational_adoption(payload)
    assert projection["validation_fingerprint"] == preview["projection"]["validation_fingerprint"]
    assert preview["validation_fingerprint"] == projection["validation_fingerprint"]
    assert projection["status"] == "ready" and preview["publication_allowed"] is True
    assert preview["side_effects"] is False
