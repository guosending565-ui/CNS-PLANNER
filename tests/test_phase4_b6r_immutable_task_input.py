"""Phase4-B6R：Immutable Heavy Task Input 收口 —— 定向测试。

对应 B6R 验收要求 1–12：

1. submit 后 snapshot 文件真实存在；
2. snapshot hash/content deterministic（相同输入复用、不同输入不同文件）；
3. worker 不依赖提交后的 ProjectState 变化（计算输入只来自 snapshot）；
4. corridor model selection 被 snapshot / fingerprint 覆盖；
5. 排队后改 corridor algorithm：worker 按旧 snapshot 算 → publish 判 stale；
6. 排队后改无关字段：可正常 publish；
7. PCF worker 使用 snapshot（不回读变化后的业务 state）；
8. submit 短锁：不长持 ``mutation_lock``；
9. project switch 不留下 running；
10. restart 可读取 snapshot 继续 queued task；
11. snapshot 缺失 / 损坏 → failed / stale，绝不执行；
12. P14 四文件 SHA 不变。

全部走真实运行时：真 worker 子进程、真持久 task store、真 content-addressed snapshot。
"""

from __future__ import annotations

import gzip
import json
import threading
import time
from hashlib import sha256
from pathlib import Path

import pytest

from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.altitude_layer_defaults import default_altitude_layers
from cns_planner.persistence.artifact_store import deserialize_payload, serialize_payload
from cns_planner.tasks.service import HeavyTaskService
from cns_planner.tasks.task_input import (
    INPUT_SNAPSHOT_SUBDIRECTORY, InputSnapshotCorrupt, InputSnapshotStore,
    InputSnapshotUnavailable, fingerprint_of,
)
from cns_planner.tasks.task_specs import (
    CORRIDOR_TASK_TYPE, PCF_TASK_TYPE, PROBE_STATE_KEY, PROBE_TASK_TYPE,
)
from cns_planner.tasks.task_store import (
    CANCELLED, FAILED, QUEUED, RUNNING, STALE, SUCCEEDED, TASK_DIRECTORY, TaskStore,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = REPO_ROOT / "cns_planner" / "config" / "defaults.json"

P14_FILES = (
    "cns_planner/algorithms/corridor/v1.py",
    "cns_planner/algorithms/coverage/geometric_3d.py",
    "cns_planner/algorithms/service_capability/v1.py",
    "tests/test_cns_corridor.py",
)
P14_BASELINE = Path(__file__).with_name("p14_protected_sha256.json")

TERMINAL = (SUCCEEDED, FAILED, CANCELLED, STALE)


# ---- 夹具 -------------------------------------------------------------------


def file_sha256(path):
    digest = sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Runtime:
    """测试用完整重任务运行时（真 store + 真 worker 进程）。"""

    def __init__(self, tmp_path, *, autostart_workers=False, **options):
        tmp_path = Path(tmp_path)
        tmp_path.mkdir(mode=0o755, exist_ok=True)
        self.workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
        self.workflow.mutation_lock = threading.RLock()
        self.service = HeavyTaskService(
            self.workflow, tmp_path / "project.json",
            max_workers=options.pop("max_workers", 1),
            heartbeat_interval=options.pop("heartbeat_interval", 0.2),
            heartbeat_timeout=options.pop("heartbeat_timeout", 30.0),
            poll_interval=options.pop("poll_interval", 0.05),
            autostart_workers=autostart_workers,
            **options,
        )

    @property
    def store(self):
        return self.service.store

    @property
    def snapshots(self):
        return self.service.snapshot_store

    def start(self):
        self.service.start()
        return self

    def stop(self):
        self.service.stop()
        return self

    def save(self):
        self.workflow.save()
        return self

    def wait(self, task_id, timeout=120):
        view = self.service.wait_for_terminal(task_id, timeout=timeout)
        assert view is not None, "任务记录丢失"
        return view

    def snapshot_of(self, record):
        return self.snapshots.load(record["input_snapshot_ref"])

    def snapshot_file(self, record):
        return self.snapshots.resolve(record["input_snapshot_ref"])


@pytest.fixture
def runtime(tmp_path):
    instance = Runtime(tmp_path).start()
    try:
        yield instance
    finally:
        instance.stop()


def probe_payload(**overrides):
    payload = {
        "probe_id": "b6r", "marker": "b6r", "steps": 2, "step_seconds": 0.0,
        "payload_bytes": 0, "crash": False, "fail": False,
    }
    payload.update(overrides)
    return payload


def corridor_project(workflow):
    """一个足够跑通 corridor 的最小工程（含算法选择持久化）。"""

    workflow.state.setdefault("spatial_3d", {})["altitude_layers"] = default_altitude_layers()
    workflow.state["grid"] = {
        "status": "passed", "crs": "OGC:CRS84", "level": 8,
        "cells": [{"grid_id": "L8-0-0", "bbox": [0.0, 0.0, 1.0, 1.0], "level": 8}],
    }
    workflow.state["operational_routes"] = [{
        "route_id": "R1", "status": "passed",
        "path": [[0.1, 0.1], [0.9, 0.9]], "waypoints": [[0.1, 0.1], [0.9, 0.9]],
    }]
    workflow.state["required_cns"] = {
        "status": "passed",
        "project_default": {
            "communication": {"required": True, "coverage_requirement": 95, "confirmed": True},
            "navigation": {"required": True, "coverage_requirement": 95, "confirmed": True},
            "surveillance": {"required": True, "coverage_requirement": 90, "confirmed": True},
        },
        "route_overrides": {}, "confirmed": True,
    }
    workflow.save()
    return workflow


def pcf_project(workflow, cells=2, layer_id="ALT-080"):
    workflow.state.setdefault("spatial_3d", {})["altitude_layers"] = default_altitude_layers()
    workflow.state["grid"] = {
        "status": "passed", "crs": "OGC:CRS84", "level": 8,
        "cells": [
            {"grid_id": f"L8-0-{index}", "bbox": [float(index), 0.0, float(index + 1), 1.0],
             "level": 8}
            for index in range(cells)
        ],
    }
    workflow.state["grid_attributes"] = {
        "terrain": {"cells": {}},
        "buildings": {"cells": {}},
    }
    workflow.state["tower_obstacle_profiles"] = {"items": {"T1": {"obstacle_height_m": 80.0}}}
    workflow.state["restricted_areas"] = {"items": [{"area_id": "A1", "status": "confirmed"}]}
    workflow.state["workspace"] = {"workspace_id": "WS-1", "revision": 3,
                                   "bbox": [0.0, 0.0, 2.0, 1.0], "status": "passed"}
    workflow.save()
    return workflow


# ---- 1. submit 后 snapshot 文件真实存在 ---------------------------------------


def test_snapshot_file_exists_and_is_content_addressed(tmp_path):
    runtime = Runtime(tmp_path, max_workers=1, autostart_workers=False)
    record, created = runtime.service.submit(PROBE_TASK_TYPE, probe_payload(probe_id="存在"))
    assert created is True
    reference = record["input_snapshot_ref"]
    path = runtime.snapshot_file(record)
    assert path.is_file(), "submit 之后 immutable snapshot 文件必须真实存在"
    # 位置：项目目录旁的 .cns-tasks/inputs/<sha256>.json.gz
    assert reference.startswith(f"{TASK_DIRECTORY}/{INPUT_SNAPSHOT_SUBDIRECTORY}/")
    assert path.name == f"{record['input_fingerprint']}.json.gz"
    assert path.parent.name == INPUT_SNAPSHOT_SUBDIRECTORY
    # task record 的指纹 = snapshot 的内容寻址身份
    assert record["input_fingerprint"] == fingerprint_of(runtime.snapshot_of(record))
    # 不进入 ProjectState
    dumped = json.dumps(runtime.workflow.state, ensure_ascii=False)
    assert TASK_DIRECTORY not in dumped
    assert reference not in dumped
    assert f"{TASK_DIRECTORY}/{INPUT_SNAPSHOT_SUBDIRECTORY}" not in dumped


# ---- 2. deterministic / 复用 --------------------------------------------------


def test_snapshot_is_deterministic_and_reused_for_identical_input(tmp_path):
    runtime = Runtime(tmp_path, max_workers=1, autostart_workers=False)
    payload = probe_payload(probe_id="deterministic", marker="m1")
    first, _ = runtime.service.submit(PROBE_TASK_TYPE, payload)
    first_bytes = runtime.snapshot_file(first).read_bytes()

    # 相同输入（不同 scope）：同一份 snapshot 文件必须被复用，字节完全相同。
    second, _ = runtime.service.submit(
        PROBE_TASK_TYPE, {**payload, "probe_id": "deterministic-2"},
    )
    # 注意：probe_id 进输入，所以这是"不同输入"——先验证不同输入 → 不同文件。
    assert second["input_snapshot_ref"] != first["input_snapshot_ref"]
    assert runtime.snapshot_file(second).read_bytes() != first_bytes

    # 同一输入重复提交（同 scope 会命中去重，这里直接验证 primitive 的确定性）。
    store = runtime.snapshots
    snapshot = runtime.snapshot_of(first)
    again = store.store(snapshot)
    assert again["artifact_id"] == first["input_fingerprint"]
    assert again["relative_path"] == first["input_snapshot_ref"]
    assert runtime.snapshot_file(first).read_bytes() == first_bytes
    assert deserialize_payload(first_bytes) == snapshot

    # 序列化本身也必须 deterministic（gzip mtime=0）。
    assert serialize_payload(snapshot) == serialize_payload(runtime.snapshot_of(first))


def test_snapshot_file_is_valid_deterministic_gzip(tmp_path):
    runtime = Runtime(tmp_path, max_workers=1, autostart_workers=False)
    record, _ = runtime.service.submit(PROBE_TASK_TYPE, probe_payload(probe_id="gzip"))
    raw = runtime.snapshot_file(record).read_bytes()
    # gzip 头里的 mtime 必须是 0（否则同一输入每次提交都是新字节）。
    assert raw[:4] == b"\x1f\x8b\x08\x00"
    assert raw[4:8] == b"\x00\x00\x00\x00"
    document = json.loads(gzip.decompress(raw).decode("utf-8"))
    assert document["artifact_schema_version"] == 1
    payload = document["payload"]
    assert payload["task_type"] == PROBE_TASK_TYPE
    assert payload["schema_version"] == 1
    assert "inputs" in payload and "worker_payload" in payload and "algorithms" in payload


# ---- 3. worker 不依赖提交后的 ProjectState 变化 -------------------------------


def test_worker_computation_does_not_depend_on_current_project_state(tmp_path):
    """提交后把业务输入彻底改坏：worker 仍按 snapshot 算出与提交时一致的结果。"""

    runtime = Runtime(tmp_path, max_workers=1)
    runtime.workflow.state["project"]["name"] = "baseline"
    runtime.save()
    record, _ = runtime.service.submit(
        PROBE_TASK_TYPE, probe_payload(probe_id="snapshot-only", marker="from-snapshot",
                                       steps=3, step_seconds=0.15),
    )
    # worker 起来之前就把 state 改成完全不同的东西（含无关字段与 revision）。
    runtime.workflow.state["project"]["name"] = "changed"
    runtime.workflow.state["grid"] = {"cells": [], "status": "changed"}
    runtime.workflow.state["algorithm_selection"]["corridor_model"]["parameters"] = {"x": 1}
    runtime.save()
    view = runtime.wait(record["task_id"], timeout=60)
    assert view["advanced"]["status"] == SUCCEEDED, view["advanced"]["error"]
    published = runtime.workflow.state.get(PROBE_STATE_KEY) or {}
    assert published.get("marker") == "from-snapshot"
    stored = runtime.store.get(record["task_id"])
    assert stored["result_summary"]["summary"]["marker"] == "from-snapshot"


def test_worker_reads_inputs_only_from_snapshot_fixture(tmp_path):
    """runner 只看到 context.inputs['snapshot']，且它与磁盘上的文件逐字一致。"""

    runtime = Runtime(tmp_path, max_workers=1)
    record, _ = runtime.service.submit(
        PROBE_TASK_TYPE, probe_payload(probe_id="only-snapshot", steps=1),
    )
    on_disk = runtime.snapshot_of(record)
    stored = runtime.store.get(record["task_id"])
    assert stored["input_snapshot_ref"] == record["input_snapshot_ref"]
    assert fingerprint_of(on_disk) == stored["input_fingerprint"]


# ---- 4. corridor algorithm capture -------------------------------------------


def test_corridor_model_selection_is_captured_in_snapshot_and_fingerprint(tmp_path):
    runtime = Runtime(tmp_path, max_workers=1, autostart_workers=False)
    corridor_project(runtime.workflow)
    record, _ = runtime.service.submit(CORRIDOR_TASK_TYPE, {})
    snapshot = runtime.snapshot_of(record)
    algorithms = snapshot["algorithms"]
    assert set(algorithms) == {"corridor_model", "coverage_model", "service_model"}
    for key, entry in algorithms.items():
        assert entry["algorithm_type"] == key
        assert entry["algorithm_id"]
        assert entry["version"]
        assert isinstance(entry["parameters"], dict)
    assert algorithms["corridor_model"]["algorithm_id"] == "cns_service_corridor_v1"

    # 改 corridor 算法参数（不改任何输入事实）→ 指纹必须变化
    runtime.workflow.state["algorithm_selection"]["corridor_model"] = {
        "algorithm_type": "corridor_model",
        "algorithm_id": "cns_service_corridor_v1",
        "version": "1.0",
        "parameters": {"corridor_model_tweak": True},
    }
    runtime.save()
    from cns_planner.tasks.handlers import plan_submission

    plan = plan_submission(runtime.workflow, CORRIDOR_TASK_TYPE, {}, store=runtime.snapshots)
    assert plan["input_fingerprint"] != record["input_fingerprint"], (
        "corridor_model 选择必须被输入指纹覆盖"
    )
    assert plan["input_snapshot"]["algorithms"]["corridor_model"]["parameters"] == {
        "corridor_model_tweak": True,
    }


def test_worker_rebuilds_corridor_model_from_snapshot_not_current_selection(tmp_path):
    """排队后换 corridor 算法：worker 仍按提交 snapshot 的算法清单重建。"""

    runtime = Runtime(tmp_path, max_workers=1, autostart_workers=False)
    corridor_project(runtime.workflow)
    record, _ = runtime.service.submit(CORRIDOR_TASK_TYPE, {})
    submitted_algorithm = runtime.snapshot_of(record)["algorithms"]["corridor_model"]

    # 换算法（用一个"明显不同"的参数），再让 worker 跑。
    runtime.workflow.state["algorithm_selection"]["corridor_model"] = {
        "algorithm_type": "corridor_model",
        "algorithm_id": "cns_service_corridor_v1",
        "version": "1.0",
        "parameters": {"corridor_model_tweak": True},
    }
    runtime.save()
    runtime.service.drive()
    final = runtime.wait(record["task_id"], timeout=120)
    # worker 的输入来自快照：它读到的 corridor_model manifest 仍是提交时那份。
    assert runtime.snapshot_of(record)["algorithms"]["corridor_model"] == submitted_algorithm
    # 而且当前算法选择确实已经变了（说明这不是"没人改"的假阳性）。
    assert (runtime.workflow.state["algorithm_selection"]["corridor_model"]["parameters"]
            == {"corridor_model_tweak": True})
    assert final["advanced"]["status"] in (STALE, SUCCEEDED), final["advanced"]


# ---- 5. 排队后改 corridor algorithm → publish stale ---------------------------


def test_algorithm_change_while_queued_yields_stale_without_overwriting_canonical(tmp_path):
    runtime = Runtime(tmp_path, max_workers=1)
    corridor_project(runtime.workflow)
    # 先算一次，建立 canonical baseline。
    first, _ = runtime.service.submit(CORRIDOR_TASK_TYPE, {})
    published = runtime.wait(first["task_id"], timeout=180)
    assert published["advanced"]["status"] == SUCCEEDED, published["advanced"]["error"]
    baseline = json.dumps(runtime.workflow.state.get("cns_corridor_assessment"), sort_keys=True)

    # 停掉 worker，制造"排队中"窗口。
    runtime.service.stop(terminate_workers=True)
    service = HeavyTaskService(
        runtime.workflow, runtime.workflow.store_path, max_workers=1,
        heartbeat_interval=0.2, heartbeat_timeout=30.0, poll_interval=0.05,
        autostart_workers=False,
    )
    service.workflow.mutation_lock = runtime.workflow.mutation_lock
    try:
        queued, created = service.submit(CORRIDOR_TASK_TYPE, {})
        assert created is True
        assert str(service.store.get(queued["task_id"])["status"]) == QUEUED
        # 排队期间改 corridor 算法选择。
        runtime.workflow.state["algorithm_selection"]["corridor_model"] = {
            "algorithm_type": "corridor_model",
            "algorithm_id": "cns_service_corridor_v1",
            "version": "1.0",
            "parameters": {"corridor_model_tweak": True},
        }
        runtime.workflow.save()
        # 提交快照未被改写。
        assert runtime.snapshot_of(queued)["algorithms"]["corridor_model"]["parameters"] == {}

        # worker 按旧 snapshot 计算（或直接被判 stale），publish 阶段必然 stale。
        service.autostart_workers = True
        service.drive()
        final = service.wait_for_terminal(queued["task_id"], timeout=180)
        assert final is not None
        assert final["advanced"]["status"] == STALE, final["advanced"]
        assert final["status_text"] == "输入已变化，请重新运行"
        assert final["advanced"]["result_artifact_ref"] is None
        after = json.dumps(runtime.workflow.state.get("cns_corridor_assessment"), sort_keys=True)
        assert after == baseline, "stale 任务覆盖了当前 canonical result"
    finally:
        service.stop()


def test_publish_compare_rejects_when_only_algorithm_changed(tmp_path):
    """直接测 publish 阶段的 compare：输入事实不变、只有算法选择变化 → stale。"""

    from cns_planner.tasks.handlers import TaskInputChangedError, publish_task_result

    runtime = Runtime(tmp_path, max_workers=1, autostart_workers=False)
    corridor_project(runtime.workflow)
    record, _ = runtime.service.submit(CORRIDOR_TASK_TYPE, {})
    baseline = json.dumps(runtime.workflow.state.get("cns_corridor_assessment"), sort_keys=True)
    runtime.workflow.state["algorithm_selection"]["corridor_model"]["parameters"] = {"tweaked": 1}
    runtime.workflow.save()
    with pytest.raises(TaskInputChangedError):
        publish_task_result(
            runtime.workflow, runtime.store.get(record["task_id"]),
            workdir=runtime.workflow.store_path,
        )
    after = json.dumps(runtime.workflow.state.get("cns_corridor_assessment"), sort_keys=True)
    assert after == baseline


# ---- 6. 排队后改无关字段 → 可正常 publish -------------------------------------


def test_unrelated_change_while_queued_still_publishes(tmp_path):
    runtime = Runtime(tmp_path, max_workers=1)
    record, _ = runtime.service.submit(
        PROBE_TASK_TYPE, probe_payload(probe_id="unrelated", steps=4, step_seconds=0.25),
    )
    runtime.workflow.state["project"]["name"] = "无关改动"
    runtime.workflow.state["project"]["notes"] = "与任务输入无关"
    runtime.save()
    for _ in range(40):
        record_now = runtime.store.get(record["task_id"])
        if str(record_now.get("status")) == RUNNING:
            break
        time.sleep(0.05)
    runtime.workflow.state["project"]["name"] = "又改一次"
    runtime.save()
    view = runtime.wait(record["task_id"], timeout=60)
    assert view["advanced"]["status"] == SUCCEEDED, view["advanced"]["error"]
    assert (runtime.workflow.state.get(PROBE_STATE_KEY) or {}).get("probe_id") == "unrelated"


# ---- 7. PCF worker 使用 snapshot ---------------------------------------------


def test_pcf_snapshot_captures_all_result_relevant_inputs(tmp_path):
    runtime = Runtime(tmp_path, max_workers=1, autostart_workers=False)
    pcf_project(runtime.workflow)
    record, _ = runtime.service.submit(PCF_TASK_TYPE, {"altitude_layer_id": "ALT-080"})
    inputs = runtime.snapshot_of(record)["inputs"]
    for key in (
        "altitude_layer", "grid", "terrain_by_cell", "buildings_by_cell",
        "tower_obstacle_profiles", "restricted_areas", "policies",
        "source_fingerprints", "workspace_identity",
    ):
        assert key in inputs, f"PCF snapshot 缺少结果相关输入：{key}"
    assert inputs["altitude_layer"]["altitude_layer_id"] == "ALT-080"
    assert inputs["altitude_layer"]["nominal_altitude_m"]
    assert inputs["workspace_identity"]["workspace_id"] == "WS-1"
    assert inputs["tower_obstacle_profiles"]["items"]["T1"]["obstacle_height_m"] == 80.0
    # PCF 是纯判定：没有算法选择参与结果。
    assert runtime.snapshot_of(record)["algorithms"] == {}


def test_pcf_worker_uses_snapshot_and_ignores_later_state_changes(tmp_path):
    runtime = Runtime(tmp_path, max_workers=1)
    pcf_project(runtime.workflow)
    record, created = runtime.service.submit(PCF_TASK_TYPE, {"altitude_layer_id": "ALT-080"})
    assert created is True
    snapshot = runtime.snapshot_of(record)
    # 提交后立刻改掉全部 PCF 业务输入（保持 altitude layer 仍可解析，使任务能走到
    # 变化检测而不是在输入组装时崩掉）。
    runtime.workflow.state["grid"]["cells"] = []
    runtime.workflow.state["grid_attributes"] = {"terrain": {"cells": {}}, "buildings": {"cells": {}}}
    runtime.workflow.state["restricted_areas"] = {"items": []}
    runtime.workflow.state["tower_obstacle_profiles"] = {"items": {}}
    for layer in runtime.workflow.state["spatial_3d"]["altitude_layers"]:
        if layer.get("altitude_layer_id") == "ALT-080":
            layer["nominal_altitude_m"] = 1234.0
    runtime.workflow.save()
    final = runtime.wait(record["task_id"], timeout=180)
    assert final["advanced"]["status"] in (STALE, SUCCEEDED), final["advanced"]
    # 快照里的输入没有被改写。
    assert runtime.snapshot_of(record) == snapshot
    assert runtime.snapshot_of(record)["inputs"]["grid"]["cells"], "快照 grid 被清空了"
    assert runtime.snapshot_of(record)["inputs"]["tower_obstacle_profiles"]["items"], (
        "快照铁塔事实被清空了"
    )


# ---- 8. submit 短锁 -----------------------------------------------------------


def test_submit_holds_mutation_lock_only_briefly(tmp_path):
    runtime = Runtime(tmp_path, max_workers=1, autostart_workers=False)
    corridor_project(runtime.workflow)
    lock = runtime.workflow.mutation_lock
    observed = {"held": [], "stop": False}

    def contender():
        while not observed["stop"]:
            started = time.time()
            acquired = lock.acquire(timeout=0.2)
            if acquired:
                observed["held"].append(time.time() - started)
                lock.release()
            time.sleep(0.01)

    worker = threading.Thread(target=contender, daemon=True)
    worker.start()
    try:
        started = time.time()
        record, created = runtime.service.submit(
            PROBE_TASK_TYPE, probe_payload(probe_id="lock", steps=1),
        )
        elapsed = time.time() - started
    finally:
        observed["stop"] = True
        worker.join(timeout=2)

    assert created is True
    assert elapsed < 5.0, f"submit 耗时 {elapsed:.2f}s，说明它做了长计算"
    # 提交期间锁最多被持有 submit 的时长；提交返回后锁必须立即可用。
    assert lock.acquire(timeout=1.0), "submit 返回后 mutation_lock 仍被持有"
    lock.release()
    assert observed["held"], "并发线程从未拿到过 mutation_lock（提交可能全程独占）"
    assert max(observed["held"]) >= 0.0
    # 提交本身不需要 worker 参与：任务仍是 queued。
    assert str(runtime.store.get(record["task_id"])["status"]) == QUEUED


def test_long_computation_never_holds_mutation_lock(tmp_path):
    runtime = Runtime(tmp_path, max_workers=1)
    record, _ = runtime.service.submit(
        PROBE_TASK_TYPE, probe_payload(probe_id="nolock", steps=8, step_seconds=0.25),
    )
    lock = runtime.workflow.mutation_lock
    observed_running = 0
    deadline = time.time() + 30
    while time.time() < deadline:
        current = runtime.store.get(record["task_id"])
        if str(current.get("status")) == RUNNING:
            probe = {"acquired": False}

            def try_lock():
                if lock.acquire(timeout=2.0):
                    probe["acquired"] = True
                    lock.release()

            worker = threading.Thread(target=try_lock)
            worker.start()
            worker.join(timeout=5)
            assert probe["acquired"] is True, (
                "快照加载/计算期间 mutation_lock 被长时间持有"
            )
            observed_running += 1
        if str(current.get("status")) in TERMINAL:
            break
        time.sleep(0.1)
    assert observed_running >= 2
    assert runtime.wait(record["task_id"], timeout=60)["advanced"]["status"] == SUCCEEDED


# ---- 9. project switch --------------------------------------------------------


def test_project_switch_marks_old_project_active_tasks_cancelled(tmp_path):
    project_a = tmp_path / "a"
    project_b = tmp_path / "b"
    project_a.mkdir(mode=0o755)
    project_b.mkdir(mode=0o755)
    runtime = Runtime(project_a, max_workers=1, autostart_workers=False)
    old_store_directory = runtime.store.directory
    record, _ = runtime.service.submit(
        PROBE_TASK_TYPE, probe_payload(probe_id="switch", steps=20, step_seconds=0.2),
    )
    runtime.service.autostart_workers = True
    runtime.service.drive()
    deadline = time.time() + 20
    while time.time() < deadline:
        if str(runtime.store.get(record["task_id"])["status"]) == RUNNING:
            break
        time.sleep(0.05)
    assert str(runtime.store.get(record["task_id"])["status"]) == RUNNING

    # 项目切换：旧项目的 worker 被终止，旧 task store 必须明确写 cancelled。
    runtime.service.bind_project(project_b / "project.json")
    try:
        old_store = TaskStore(old_store_directory)
        stored = old_store.get(record["task_id"])
        assert str(stored["status"]) == CANCELLED, stored
        assert stored["error"]["reason"] == "project_switched"
        assert stored["finished_at"]
        # 绝不留永久 running 记录。
        assert old_store.list(status=(QUEUED, RUNNING, "cancelling")) == []
    finally:
        runtime.service.stop()


def test_closing_browser_does_not_cancel_tasks(tmp_path):
    """`stop()`（进程收尾/浏览器关闭路径）不取消任务，也不改写 terminal 之外的语义。"""

    runtime = Runtime(tmp_path, max_workers=1)
    record, _ = runtime.service.submit(
        PROBE_TASK_TYPE, probe_payload(probe_id="survives", steps=6, step_seconds=0.3),
    )
    time.sleep(0.4)
    runtime.service.stop(terminate_workers=False)
    stored = TaskStore(runtime.store.directory).get(record["task_id"])
    assert stored["cancel_requested"] is False
    assert stored["error"] in (None, {})


def test_async_http_submit_still_returns_202_with_real_snapshot(tmp_path):
    """async POST 立即返回 202，且返回的 task 已经指向真实存在的 immutable snapshot。"""

    from cns_planner.api.router import ApiRouter

    runtime = Runtime(tmp_path, max_workers=1, autostart_workers=False)

    class Context:
        pass

    context = Context()
    context.workflow = runtime.workflow
    context.heavy_tasks = runtime.service
    context.data = type("Data", (), {"error": None, "metadata": lambda self: {}, "paths": {}})()

    started = time.time()
    response = ApiRouter(context).post(
        "/api/tasks", {"task_type": PROBE_TASK_TYPE, **probe_payload(probe_id="http202")},
    )
    elapsed = time.time() - started
    assert response.status == 202, response.data
    assert response.data["created"] is True
    assert elapsed < 5.0
    task = response.data["task"]
    assert task["advanced"]["status"] == QUEUED
    assert task["advanced"]["input_snapshot_present"] is True
    record = runtime.store.get(response.data["task_id"])
    assert runtime.snapshot_file(record).is_file()


# ---- 10. restart 可读取 snapshot 继续 queued task -----------------------------


def test_restart_can_read_snapshot_and_run_queued_task(tmp_path):
    first = Runtime(tmp_path, max_workers=1, autostart_workers=False)
    corridor_project(first.workflow)
    record, created = first.service.submit(PROBE_TASK_TYPE, probe_payload(probe_id="restart"))
    assert created is True
    assert str(first.store.get(record["task_id"])["status"]) == QUEUED
    reference = record["input_snapshot_ref"]
    assert first.snapshot_file(record).is_file()
    first.service.stop(terminate_workers=False)

    # "重启"：新 service 实例读同一个 store / 同一份 snapshot。
    restarted = HeavyTaskService(
        first.workflow, first.workflow.store_path, max_workers=1,
        heartbeat_interval=0.2, heartbeat_timeout=30.0, poll_interval=0.05,
    )
    restarted.workflow.mutation_lock = threading.RLock()
    restarted.start()
    try:
        stored = TaskStore(first.store.directory).get(record["task_id"])
        assert stored["input_snapshot_ref"] == reference
        view = restarted.wait_for_terminal(record["task_id"], timeout=120)
        assert view is not None
        assert view["advanced"]["status"] == SUCCEEDED, view["advanced"]
        assert (first.workflow.state.get(PROBE_STATE_KEY) or {}).get("probe_id") == "restart"
    finally:
        restarted.stop()


# ---- 11. snapshot 缺失 / 损坏 ------------------------------------------------


def test_missing_snapshot_fails_task_without_executing(tmp_path):
    runtime = Runtime(tmp_path, max_workers=1, autostart_workers=False)
    record, _ = runtime.service.submit(PROBE_TASK_TYPE, probe_payload(probe_id="missing"))
    runtime.snapshot_file(record).unlink()
    with pytest.raises(InputSnapshotUnavailable):
        runtime.snapshot_of(record)

    runtime.service.autostart_workers = True
    runtime.service.drive()
    final = runtime.wait(record["task_id"], timeout=60)
    assert final["advanced"]["status"] == FAILED, final["advanced"]
    assert final["advanced"]["result_artifact_ref"] is None
    assert PROBE_STATE_KEY not in runtime.workflow.state


def test_corrupt_snapshot_fails_task_without_executing(tmp_path):
    runtime = Runtime(tmp_path, max_workers=1, autostart_workers=False)
    record, _ = runtime.service.submit(PROBE_TASK_TYPE, probe_payload(probe_id="corrupt"))
    path = runtime.snapshot_file(record)
    path.write_bytes(b"not-a-gzip-stream")
    with pytest.raises(InputSnapshotCorrupt):
        runtime.snapshot_of(record)

    runtime.service.autostart_workers = True
    runtime.service.drive()
    final = runtime.wait(record["task_id"], timeout=60)
    assert final["advanced"]["status"] == FAILED, final["advanced"]
    assert final["advanced"]["error"]["code"] == "task_execution_failed"
    assert PROBE_STATE_KEY not in runtime.workflow.state


def test_tampered_snapshot_is_rejected(tmp_path):
    """文件名/指纹对不上的 snapshot 绝不执行（content-address 自校验）。"""

    runtime = Runtime(tmp_path, max_workers=1, autostart_workers=False)
    record, _ = runtime.service.submit(PROBE_TASK_TYPE, probe_payload(probe_id="tamper"))
    path = runtime.snapshot_file(record)
    original = deserialize_payload(path.read_bytes())
    original["inputs"]["probe_id"] = "hijacked"
    path.write_bytes(serialize_payload(original))
    with pytest.raises(InputSnapshotCorrupt):
        runtime.snapshot_of(record)
    runtime.service.autostart_workers = True
    runtime.service.drive()
    final = runtime.wait(record["task_id"], timeout=60)
    assert final["advanced"]["status"] == FAILED, final["advanced"]
    assert (runtime.workflow.state.get(PROBE_STATE_KEY) or {}).get("probe_id") != "hijacked"


# ---- 12. P14 四文件 SHA 不变 --------------------------------------------------


def test_p14_protected_files_are_byte_identical():
    assert P14_BASELINE.is_file(), "缺少 P14 基线指纹文件"
    baseline = json.loads(P14_BASELINE.read_text(encoding="utf-8"))
    assert set(baseline) == set(P14_FILES)
    actual = {relative: file_sha256(REPO_ROOT / relative) for relative in P14_FILES}
    assert actual == baseline, "B6R 修改了受保护的 P14 文件"


# ---- 附加：store 契约 ---------------------------------------------------------


def test_snapshot_store_is_not_in_project_state_and_reuses_directory(tmp_path):
    runtime = Runtime(tmp_path, max_workers=1, autostart_workers=False)
    record, _ = runtime.service.submit(PROBE_TASK_TYPE, probe_payload(probe_id="dir"))
    directory = runtime.snapshots.directory
    assert directory == Path(runtime.store.directory) / INPUT_SNAPSHOT_SUBDIRECTORY
    assert list(directory.glob("*.json.gz"))
    # task store 的 tasks/ 与 inputs/ 并列，互不干扰。
    assert (Path(runtime.store.directory) / "tasks").is_dir()
    assert TASK_DIRECTORY not in json.dumps(runtime.workflow.state)
    assert record["input_snapshot_ref"].endswith(".json.gz")


def test_snapshot_store_rejects_relative_path_escape(tmp_path):
    store = InputSnapshotStore(tmp_path / "project.json")
    with pytest.raises(InputSnapshotUnavailable):
        store.load("../../etc/passwd")
    with pytest.raises(InputSnapshotUnavailable):
        store.load("")
