"""R7-A1：P16-V2 池缓存结构、外部来源门禁与提交绑定。

这些测试不跑真实 22 s 规划，也不依赖 GDAL：

* 用 fake sampler / fake scenario service 验证"冷构建 → 热命中 → 来源变化失效"；
* 用真实哈希与临时文件验证 land_mask 必需、参考设施"读取失败 ≠ 通过"；
* 用 fake heavy-task service 验证专用 endpoint 与通用 ``/api/tasks/submit``
  共用同一 canonical 来源门禁。
"""

from __future__ import annotations

from copy import deepcopy
import gzip
import json

import pytest

from cns_planner.api.router import ApiRouter
from cns_planner.application import p16_v2_pool
from cns_planner.tasks import task_specs


def _facility_identity():
    return {
        "status": "passed",
        "registry_id": "test-registry",
        "version": "v1",
        "integrity_status": "passed",
        "files": [
            {"key": key, "file": f"{key}.json", "sha256": "A" * 64, "status": "passed"}
            for key in ("landing", "nest", "base_station", "5g_a")
        ],
    }


class _FakeSampler:
    def __init__(self, path):
        self.path = path

    def describe(self):
        return {"source": "fake_dem", "path": str(self.path), "method": "test"}


class _FakeScenarioService:
    calls = 0
    last_kwargs = {}

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        type(self).last_kwargs = kwargs

    def build_candidate_pool(self, scenario):
        type(self).calls += 1
        return {
            "status": "passed",
            "scenario": deepcopy(scenario),
            "actions": [{"action_id": "a-1", "scenario_site_id": "s-1"}],
            "sites": [{"scenario_site_id": "s-1", "coordinate": [122.0, 30.0]}],
            "counts": {"canonical_site_count": 1, "action_count": 1},
            "sources": {"dem": "fake"},
        }


def _fake_land_mask_context(path, state):
    return object(), {"policy": "test"}, {
        "path": str(path), "sha256": "mask-sha", "semantics": "test_land_mask",
    }


def _patch_pool_dependencies(monkeypatch):
    _FakeScenarioService.calls = 0
    monkeypatch.setattr(p16_v2_pool, "_GdalNearestSampler", _FakeSampler)
    monkeypatch.setattr(p16_v2_pool, "_land_mask_context", _fake_land_mask_context)
    monkeypatch.setattr(
        p16_v2_pool, "EngineeringAssumptionScenarioService", _FakeScenarioService,
    )
    monkeypatch.setattr(
        p16_v2_pool, "reference_facilities_identity", lambda: _facility_identity(),
    )


def _pool_state():
    return {
        "surface_classification_policy": {},
        "tower_colocation_candidates": {"counts": {}},
        "reference_landing_sites": {"items": []},
        "device_catalog": {"items": []},
        "revision": 7,
    }


def _ovobj_file(tmp_path, records=None):
    """写一个真实可解析的 .ovobj JSON 缓存（解析器本身支持 .json 导出）。"""

    path = tmp_path / "sites.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"records": records if records is not None else [{
            "name": "ZZ_测试地面站", "longitude": 122.1, "latitude": 30.1,
            "site_type": "地面站",
        }]}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _data_paths(tmp_path, *, ovobj=None):
    terrain = tmp_path / "dem.tif"
    terrain.write_bytes(b"dem-v1")
    mask = tmp_path / "mask.gpkg"
    mask.write_bytes(b"mask-v1")
    return {
        "terrain_dtm": str(terrain),
        "land_mask": str(mask),
        "tower_ovobj": str(ovobj or _ovobj_file(tmp_path)),
    }


def test_pool_cache_cold_warm_source_change_and_corruption(tmp_path, monkeypatch):
    _patch_pool_dependencies(monkeypatch)
    terrain = tmp_path / "dem.tif"
    terrain.write_bytes(b"dem-v1")
    mask = tmp_path / "mask.gpkg"
    mask.write_bytes(b"mask-v1")
    data_paths = {
        "terrain_dtm": str(terrain), "land_mask": str(mask),
        "tower_ovobj": str(_ovobj_file(tmp_path)),
    }
    state = _pool_state()
    scenario = {"scenario_id": "TEST-R7A1"}
    cache = tmp_path / "cache"

    pool_cold, meta_cold = p16_v2_pool.build_scenario_pool(
        state=state, scenario=scenario, data_paths=data_paths, cache_dir=cache,
    )
    assert meta_cold["cache"] == "cold"
    assert _FakeScenarioService.calls == 1
    assert meta_cold["counts"] == pool_cold["counts"]
    assert meta_cold["external_sources"]["land_mask"]["sha256"]
    assert meta_cold["reference_facilities"]["status"] == "passed"
    cache_files = list(cache.glob("pool-*.json.gz"))
    assert len(cache_files) == 1
    assert meta_cold["path"] == str(cache_files[0])

    #: 热命中：不重新构建，但 meta 必须保留 counts / dem / 来源身份（旧实现会丢）。
    checked = []
    pool_warm, meta_warm = p16_v2_pool.build_scenario_pool(
        state=state, scenario=scenario, data_paths=data_paths, cache_dir=cache,
        cancel_check=lambda: checked.append(True),
    )
    assert checked, "热命中也必须经过取消检查"
    assert meta_warm["cache"] == "warm"
    assert _FakeScenarioService.calls == 1
    assert pool_warm == pool_cold
    assert meta_warm["counts"] == meta_cold["counts"]
    assert meta_warm["dem"]["source"] == "fake_dem"
    assert meta_warm["identity"] == meta_cold["identity"]

    #: 损坏缓存必须被结构校验拒绝并重建，绝不半读半用。
    cache_files[0].write_bytes(b"not-a-gzip")
    _, meta_corrupt = p16_v2_pool.build_scenario_pool(
        state=state, scenario=scenario, data_paths=data_paths, cache_dir=cache,
    )
    assert meta_corrupt["cache"] == "cold"
    assert _FakeScenarioService.calls == 2
    _, meta_after_repair = p16_v2_pool.build_scenario_pool(
        state=state, scenario=scenario, data_paths=data_paths, cache_dir=cache,
    )
    assert meta_after_repair["cache"] == "warm"
    assert _FakeScenarioService.calls == 2

    #: 旧读取器检查顶层 actions；这里显式放一个"顶层有 actions、没有 pool/meta"
    #: 的缓存，必须被视为不完整并重建。
    identity = p16_v2_pool.scenario_identity(state, scenario, data_paths)
    stale_structure = cache / f"pool-{identity[:24]}.json.gz"
    with gzip.open(stale_structure, "wt", encoding="utf-8") as handle:
        json.dump({"actions": [{"action_id": "old"}]}, handle)
    _, meta_incomplete = p16_v2_pool.build_scenario_pool(
        state=state, scenario=scenario, data_paths=data_paths, cache_dir=cache,
    )
    assert meta_incomplete["cache"] == "cold"
    assert _FakeScenarioService.calls == 3

    #: 来源内容变化 ⇒ identity 变化 ⇒ 新缓存文件、重新构建。
    mask.write_bytes(b"mask-v2")
    _, meta_changed = p16_v2_pool.build_scenario_pool(
        state=state, scenario=scenario, data_paths=data_paths, cache_dir=cache,
    )
    assert meta_changed["cache"] == "cold"
    assert _FakeScenarioService.calls == 4
    assert meta_changed["identity"] != meta_cold["identity"]


def test_pool_cache_rejects_cached_pool_with_other_identity(tmp_path, monkeypatch):
    _patch_pool_dependencies(monkeypatch)
    terrain = tmp_path / "dem.tif"
    terrain.write_bytes(b"dem-v1")
    mask = tmp_path / "mask.gpkg"
    mask.write_bytes(b"mask-v1")
    data_paths = {
        "terrain_dtm": str(terrain), "land_mask": str(mask),
        "tower_ovobj": str(_ovobj_file(tmp_path)),
    }
    state = _pool_state()
    scenario = {"scenario_id": "TEST-R7A1"}
    cache = tmp_path / "cache"

    _, meta = p16_v2_pool.build_scenario_pool(
        state=state, scenario=scenario, data_paths=data_paths, cache_dir=cache,
    )
    cache_path = cache / f"pool-{meta['identity'][:24]}.json.gz"
    with gzip.open(cache_path, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    payload["pool"]["v2_pool_identity"] = "0" * 64
    with gzip.open(cache_path, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle)
    _, rebuilt = p16_v2_pool.build_scenario_pool(
        state=state, scenario=scenario, data_paths=data_paths, cache_dir=cache,
    )
    assert rebuilt["cache"] == "cold"
    assert _FakeScenarioService.calls == 2

    #: 旧缓存缺少 ovobj 身份 ⇒ 不能把 606 站口径当热命中，必须重建。
    cache_path = cache / f"pool-{rebuilt['identity'][:24]}.json.gz"
    with gzip.open(cache_path, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    payload["pool"]["v2_pool_identity"] = rebuilt["identity"]
    payload["meta"]["identity"] = rebuilt["identity"]
    payload["meta"].pop("ovobj", None)
    with gzip.open(cache_path, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle)
    _, rebuilt_again = p16_v2_pool.build_scenario_pool(
        state=state, scenario=scenario, data_paths=data_paths, cache_dir=cache,
    )
    assert rebuilt_again["cache"] == "cold"
    assert _FakeScenarioService.calls == 3


def test_pool_wires_registered_ovobj_and_invalidates_on_content_change(tmp_path, monkeypatch):
    """R7-B：V2 池入口必须真的消费 .ovobj 补充站址，而不是传 None。"""

    _patch_pool_dependencies(monkeypatch)
    data_paths = _data_paths(tmp_path)
    state = _pool_state()
    scenario = {"scenario_id": "TEST-R7B"}
    cache = tmp_path / "cache"

    pool_cold, meta_cold = p16_v2_pool.build_scenario_pool(
        state=state, scenario=scenario, data_paths=data_paths, cache_dir=cache,
    )
    assert meta_cold["cache"] == "cold"
    provider = _FakeScenarioService.last_kwargs["ovobj_records_provider"]
    assert callable(provider), "V2 池不得再把 ovobj_records_provider 固定为 None"
    records = provider()
    assert records["status"] == "passed"
    assert records["record_count"] == 1
    assert records["records"][0]["name"] == "ZZ_测试地面站"
    assert meta_cold["ovobj"]["record_count"] == 1
    assert meta_cold["ovobj"]["sha256"] == meta_cold["external_sources"]["tower_ovobj"]["sha256"]
    assert meta_cold["external_sources"]["tower_ovobj"]["sha256"]

    #: 补充站址文件内容变化 ⇒ 池身份变化 ⇒ 必须冷重建，不能复用 648/606 旧池。
    _ovobj_file(tmp_path, records=[
        {"name": "ZZ_测试地面站", "longitude": 122.1, "latitude": 30.1, "site_type": "地面站"},
        {"name": "ZZ_新增楼面站", "longitude": 122.2, "latitude": 30.2, "site_type": "楼面站"},
    ])
    _, meta_changed = p16_v2_pool.build_scenario_pool(
        state=state, scenario=scenario, data_paths=data_paths, cache_dir=cache,
    )
    assert meta_changed["cache"] == "cold"
    assert meta_changed["identity"] != meta_cold["identity"]
    assert meta_changed["ovobj"]["record_count"] == 2


def test_pool_rejects_missing_empty_or_changed_ovobj(tmp_path, monkeypatch):
    _patch_pool_dependencies(monkeypatch)
    state = _pool_state()
    scenario = {"scenario_id": "TEST-R7B"}

    absent_key = _data_paths(tmp_path)
    absent_key.pop("tower_ovobj")
    with pytest.raises(p16_v2_pool.P16V2PoolUnavailable) as missing:
        p16_v2_pool.build_scenario_pool(
            state=state, scenario=scenario, data_paths=absent_key,
        )
    assert missing.value.code == "p16_v2_ovobj_missing"

    absent = _data_paths(tmp_path)
    absent["tower_ovobj"] = str(tmp_path / "missing.ovobj")
    with pytest.raises(p16_v2_pool.P16V2PoolUnavailable) as unreadable:
        p16_v2_pool.build_scenario_pool(
            state=state, scenario=scenario, data_paths=absent,
        )
    assert unreadable.value.code == "p16_v2_ovobj_unreadable"

    empty = _data_paths(tmp_path, ovobj=_ovobj_file(tmp_path / "empty", records=[]))
    with pytest.raises(p16_v2_pool.P16V2PoolUnavailable) as no_records:
        p16_v2_pool.build_scenario_pool(
            state=state, scenario=scenario, data_paths=empty,
        )
    assert no_records.value.code == "p16_v2_ovobj_empty"

    #: 身份计算与内容读取之间文件被替换 ⇒ 拒绝混用两份内容。
    changed = _data_paths(tmp_path)
    monkeypatch.setattr(
        p16_v2_pool, "load_ovobj_records",
        lambda path: {
            "status": "passed", "source_sha256": "0" * 64,
            "record_count": 1, "unique_name_count": 1,
            "records": [{"name": "ZZ_x", "longitude": 122.0, "latitude": 30.0}],
        },
    )
    with pytest.raises(p16_v2_pool.P16V2PoolUnavailable) as mismatch:
        p16_v2_pool.build_scenario_pool(
            state=state, scenario=scenario, data_paths=changed,
        )
    assert mismatch.value.code == "p16_v2_ovobj_changed"


def test_reference_facilities_unavailable_is_not_a_pass():
    with pytest.raises(p16_v2_pool.P16V2PoolUnavailable) as unavailable:
        p16_v2_pool.require_reference_facilities(
            {"status": "unavailable", "error": "OSError", "files": []}
        )
    assert unavailable.value.code == "p16_v2_reference_facilities_unavailable"

    blocked = deepcopy(_facility_identity())
    blocked["status"] = "blocked"
    with pytest.raises(p16_v2_pool.P16V2PoolUnavailable):
        p16_v2_pool.require_reference_facilities(blocked)

    mismatch = deepcopy(_facility_identity())
    mismatch["files"][0]["status"] = "sha256_mismatch"
    with pytest.raises(p16_v2_pool.P16V2PoolUnavailable):
        p16_v2_pool.require_reference_facilities(mismatch)

    passed = _facility_identity()
    assert p16_v2_pool.require_reference_facilities(passed) is passed


def test_v2_inputs_reject_missing_land_mask_and_untrusted_facilities(tmp_path, monkeypatch):
    terrain = tmp_path / "dem.tif"
    terrain.write_bytes(b"dem-v1")
    mask = tmp_path / "mask.gpkg"
    mask.write_bytes(b"mask-v1")
    scenario_id = task_specs.DEFAULT_V2_SCENARIO_ID

    with pytest.raises(p16_v2_pool.P16V2PoolUnavailable) as missing_mask:
        task_specs._p16_v2_inputs({}, {
            "scenario_id": scenario_id,
            "data_paths": {"terrain_dtm": str(terrain)},
        })
    assert missing_mask.value.code == "p16_v2_land_mask_missing"

    #: R7-B：缺 .ovobj 来源同样是"候选空间不完整"，必须在提交阶段拒绝，
    #: 不能等到池构建时才静默少 42 站。
    with pytest.raises(p16_v2_pool.P16V2PoolUnavailable) as missing_ovobj:
        task_specs._p16_v2_inputs({}, {
            "scenario_id": scenario_id,
            "data_paths": {"terrain_dtm": str(terrain), "land_mask": str(mask)},
        })
    assert missing_ovobj.value.code == "p16_v2_ovobj_missing"

    monkeypatch.setattr(
        p16_v2_pool, "reference_facilities_identity",
        lambda: {"status": "unavailable", "error": "OSError", "files": []},
    )
    with pytest.raises(p16_v2_pool.P16V2PoolUnavailable) as facilities:
        task_specs._p16_v2_inputs({}, {
            "scenario_id": scenario_id,
            "data_paths": {
                "terrain_dtm": str(terrain), "land_mask": str(mask),
                "tower_ovobj": str(_ovobj_file(tmp_path)),
            },
        })
    assert facilities.value.code == "p16_v2_reference_facilities_unavailable"


def test_worker_fails_closed_when_reference_registry_is_unreadable(tmp_path, monkeypatch):
    monkeypatch.setattr(p16_v2_pool, "reference_facilities_identity", _facility_identity)
    terrain = tmp_path / "dem.tif"
    terrain.write_bytes(b"dem-v1")
    mask = tmp_path / "mask.gpkg"
    mask.write_bytes(b"mask-v1")
    data_paths = {
        "terrain_dtm": str(terrain), "land_mask": str(mask),
        "tower_ovobj": str(_ovobj_file(tmp_path)),
    }
    inputs = {
        "data_paths": data_paths,
        "external_sources": p16_v2_pool.external_source_identity(data_paths),
        "reference_facilities": p16_v2_pool.reference_facilities_identity(),
    }
    task_specs._p16_v2_verify_sources(inputs)

    monkeypatch.setattr(
        p16_v2_pool, "reference_facilities_identity",
        lambda: {"status": "unavailable", "error": "OSError", "files": []},
    )
    with pytest.raises(task_specs.TaskInputChanged) as current_unreadable:
        task_specs._p16_v2_verify_sources(inputs)
    assert "参考设施" in current_unreadable.value.message

    #: 提交时与执行时都不可读，也绝不能因为"两边都是空列表"而被当成未变化。
    inputs["reference_facilities"] = {"status": "unavailable", "files": []}
    with pytest.raises(task_specs.TaskInputChanged):
        task_specs._p16_v2_verify_sources(inputs)

    #: 注册表内容变化（例如某个 collection 的 SHA 变了）同样拒绝发布。
    monkeypatch.setattr(
        p16_v2_pool, "reference_facilities_identity", lambda: _facility_identity(),
    )
    changed = _facility_identity()
    changed["files"][0]["sha256"] = "B" * 64
    inputs["reference_facilities"] = changed
    with pytest.raises(task_specs.TaskInputChanged):
        task_specs._p16_v2_verify_sources(inputs)


def test_worker_rejects_ovobj_change_after_submission(tmp_path, monkeypatch):
    monkeypatch.setattr(p16_v2_pool, "reference_facilities_identity", _facility_identity)
    data_paths = _data_paths(tmp_path)
    inputs = {
        "data_paths": data_paths,
        "external_sources": p16_v2_pool.external_source_identity(data_paths),
        "reference_facilities": p16_v2_pool.reference_facilities_identity(),
    }
    task_specs._p16_v2_verify_sources(inputs)

    _ovobj_file(tmp_path, records=[
        {"name": "ZZ_另一个站", "longitude": 122.3, "latitude": 30.3, "site_type": "地面站"},
    ])
    with pytest.raises(task_specs.TaskInputChanged) as changed:
        task_specs._p16_v2_verify_sources(inputs)
    assert ".ovobj" in changed.value.message


class _FakeTaskService:
    def __init__(self):
        self.calls = []

    def maybe_drive(self):
        return None

    def submit(self, task_type, payload, conflict_policy=None):
        self.calls.append((str(task_type), deepcopy(payload)))
        return {"task_id": "task-r7a1"}, True

    def find(self, task_id):
        return {"task_id": task_id, "status_text": "正在排队"}


def _fake_router(paths):
    data = type("FakeData", (), {"paths": paths})()
    context = type("FakeContext", (), {"data": data, "workflow": object()})()
    context.heavy_tasks = _FakeTaskService()
    return ApiRouter(context), context.heavy_tasks


def test_v2_endpoint_and_generic_submit_bind_registered_canonical_sources(tmp_path):
    terrain = tmp_path / "dem.tif"
    terrain.write_bytes(b"dem-v1")
    mask = tmp_path / "mask.gpkg"
    mask.write_bytes(b"mask-v1")
    ovobj = _ovobj_file(tmp_path)
    router, service = _fake_router({
        "terrain_dtm": str(terrain), "land_mask": str(mask),
        "tower_ovobj": str(ovobj),
    })
    expected = {
        "terrain_dtm": str(terrain.resolve()), "land_mask": str(mask.resolve()),
        "tower_ovobj": str(ovobj.resolve()),
    }

    response = router.post(
        "/api/cns-corridor-site-plan/v2/evaluate", {"version": "v2"},
    )
    assert response.status == 202
    assert service.calls[-1][1]["data_paths"] == expected

    response = router.post("/api/tasks/submit", {
        "task_type": task_specs.P16_V2_TASK_TYPE,
        "data_paths": dict(expected),
    })
    assert response.status == 202
    assert service.calls[-1][1]["data_paths"] == expected

    #: 客户端显式给出非登记路径必须 fail-closed；不能"悄悄替换成正确路径"。
    other = tmp_path / "other.tif"
    other.write_bytes(b"dem-v1")
    response = router.post("/api/tasks/submit", {
        "task_type": task_specs.P16_V2_TASK_TYPE,
        "data_paths": {"terrain_dtm": str(other)},
    })
    assert response.status == 400
    assert response.data["detail"]["code"] == "p16_v2_source_not_canonical"

    #: R7-B：客户端替换 .ovobj 来源同样被拒绝；不能借"换一份补充站址文件"
    #: 让服务端算出与 648 口径不同的候选池。
    other_ovobj = _ovobj_file(tmp_path / "drift")
    response = router.post("/api/tasks/submit", {
        "task_type": task_specs.P16_V2_TASK_TYPE,
        "data_paths": {**expected, "tower_ovobj": str(other_ovobj)},
    })
    assert response.status == 400
    assert response.data["detail"]["code"] == "p16_v2_source_not_canonical"

    response = router.post("/api/tasks/submit", {
        "task_type": task_specs.P16_V2_TASK_TYPE,
        "data_paths": [],
    })
    assert response.status == 400
    assert response.data["detail"]["code"] == "p16_v2_source_invalid"


def test_generic_submit_rejects_unregistered_or_unreadable_sources(tmp_path):
    terrain = tmp_path / "dem.tif"
    terrain.write_bytes(b"dem-v1")
    mask = tmp_path / "mask.gpkg"
    mask.write_bytes(b"mask-v1")

    router, service = _fake_router({"terrain_dtm": str(terrain)})
    response = router.post("/api/tasks/submit", {
        "task_type": task_specs.P16_V2_TASK_TYPE,
    })
    assert response.status == 400
    assert response.data["detail"]["code"] == "p16_v2_source_not_registered"
    assert service.calls == []

    router, service = _fake_router({
        "terrain_dtm": str(terrain), "land_mask": str(tmp_path / "missing.gpkg"),
    })
    response = router.post("/api/tasks/submit", {
        "task_type": task_specs.P16_V2_TASK_TYPE,
    })
    assert response.status == 400
    assert response.data["detail"]["code"] == "p16_v2_source_unavailable"
    assert service.calls == []
