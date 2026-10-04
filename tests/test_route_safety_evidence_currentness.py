"""Round29-K1：Route Safety Evidence V2 的 **corridor 维度**服从唯一 currentness authority。

裁定（与 ``application/result_currentness.py`` 完全一致）：

* ``cns_corridor_assessment``（P14）能否作为 **current** 走廊证据被消费，只由
  ``result_currentness`` 的有效投影决定；
* stored P14 的 raw ``status=passed`` / ``failed`` **不是**消费理由：若它由不同算法语义
  版本产生，有效状态即 ``stale``，此时：

  - ``corridor_consumed=false``；
  - CNS operational-support 证据如实 ``stale``（既不是 passed 也不是 failed）；
  - reference / fingerprint 绝不宣称消费了 current corridor；
  - stored P14 与 ``result_statuses`` **逐字段保留**，**不 save**；

* 本文件的 currentness 判定**不得**在 route safety 服务里重复实现：一律复用
  ``result_currentness`` 的权威函数。

本文件不运行权威 P17/P18，也不修改任何权威项目状态。
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cns_planner.algorithms.corridor.v1 import CNSServiceCorridorV1  # noqa: E402
from cns_planner.application import result_currentness  # noqa: E402
from cns_planner.application import route_safety_evidence_service as rse  # noqa: E402
from cns_planner.application.result_currentness import (  # noqa: E402
    effective_result_status, projected_result,
)

from test_route_safety_evidence_v2 import (  # noqa: E402
    DEFAULTS, evaluate, harness, passing_cns,
)


CURRENT_ID = CNSServiceCorridorV1.algorithm_id
CURRENT_VERSION = CNSServiceCorridorV1.algorithm_version

#: 与当前实现**不同**的旧算法语义版本（P14 当前为 1.2）。
OLD_VERSION = "1.0"


def _corridor(*, version=CURRENT_VERSION, status="passed"):
    return {
        "status": status,
        "algorithm_id": CURRENT_ID,
        "algorithm_version": version,
        "input_fingerprint": "corridor-fp",
        "corridor_geometry_fingerprint": "corridor-geom-fp",
        "routes": [{
            "route_id": "R-1", "status": "passed",
            "subsystems": [{
                "subsystem": code, "status": "passed", "required_voxel_count": 10,
                "deficit_voxel_ids": [], "unknown_voxel_ids": [],
            } for code in ("C", "N", "S")],
        }],
    }


def _upstream(*, version=CURRENT_VERSION, status="passed"):
    upstream = passing_cns()
    upstream["cns_corridor_assessment"] = _corridor(version=version, status=status)
    return upstream


# ---------------------------------------------------------------------------
# 1. 旧 P14：raw 看起来 current，但有效 stale ⇒ 绝不消费
# ---------------------------------------------------------------------------

def test_old_p14_effective_stale_is_never_consumed(tmp_path):
    assert OLD_VERSION != CURRENT_VERSION, "fixture 必须是真正不同的算法语义版本"
    service = harness(tmp_path, upstream=_upstream(version=OLD_VERSION))
    state = service.state
    #: raw 状态刻意写成"看起来通过"：它绝不能被当成 current 依据。
    state.setdefault("result_statuses", {})["cns_corridor_assessment"] = "passed"

    record = evaluate(service)
    cns = record["domains"]["cns_operational_support"]
    corridor = cns["evidence"]["cns_corridor_assessment"]

    assert effective_result_status(state, "cns_corridor_assessment") == "stale"
    assert corridor["consumed"] is False
    assert corridor["effective_status"] == "stale"
    assert corridor["algorithm_semantics_stale"] is True
    assert corridor["status"] == "passed"          #: stored raw status 原样转印
    assert corridor["raw_result_status"] == "passed"
    #: 绝不宣称消费了 current corridor。
    assert record["fingerprints"]["cns_corridor_fingerprint"] is None
    assert record["fingerprints"]["cns_corridor_consumed"] is False
    assert not any(
        item["role"] == "cns_corridor_assessment" for item in cns["sources"]
    )
    assert all(
        item["corridor"]["consumed"] is False for item in cns["metrics"]["subsystems"]
    )
    #: CNS operational-support 证据如实 stale：既不是 passed 也不是 failed。
    assert cns["status"] == "stale"
    assert cns["status_reason"] == "cns_upstream_evidence_stale:cns_corridor_assessment"
    assert record["evidence_summary"]["domains"]["cns_operational_support"] == "stale"
    assert cns.get("hard_constraint_failure") in (False, None)


def test_old_p14_is_not_rewritten_and_not_saved(tmp_path):
    service = harness(tmp_path, upstream=_upstream(version=OLD_VERSION))
    state = service.state
    stored_before = deepcopy(state["cns_corridor_assessment"])
    state.setdefault("result_statuses", {})["cns_corridor_assessment"] = "passed"

    evaluate(service)

    assert state["cns_corridor_assessment"] == stored_before
    assert state["cns_corridor_assessment"]["algorithm_version"] == OLD_VERSION
    assert state["cns_corridor_assessment"]["status"] == "passed"
    assert state["result_statuses"]["cns_corridor_assessment"] == "passed"
    #: 只读投影绝不触发保存。
    assert service.session.pending_save is False


def test_old_p14_stale_status_is_not_reported_as_failed(tmp_path):
    """stale 既不是 passed 也不是 failed：绝不制造一个虚假的硬约束失败。"""

    service = harness(tmp_path, upstream=_upstream(version=OLD_VERSION, status="failed"))
    state = service.state
    state.setdefault("result_statuses", {})["cns_corridor_assessment"] = "failed"

    record = evaluate(service)
    cns = record["domains"]["cns_operational_support"]
    corridor = cns["evidence"]["cns_corridor_assessment"]

    assert corridor["status"] == "failed"          #: raw 原样
    assert corridor["effective_status"] == "stale"
    assert corridor["consumed"] is False
    assert cns["status"] == "stale"
    assert cns.get("hard_constraint_failure") is False
    assert record["status"] != "hard_constraint_failed"


# ---------------------------------------------------------------------------
# 2. current P14：原行为不变
# ---------------------------------------------------------------------------

def test_current_p14_keeps_the_existing_consumption_behaviour(tmp_path):
    service = harness(tmp_path, upstream=_upstream())
    state = service.state
    state.setdefault("result_statuses", {})["cns_corridor_assessment"] = "passed"

    record = evaluate(service)
    cns = record["domains"]["cns_operational_support"]
    corridor = cns["evidence"]["cns_corridor_assessment"]

    assert corridor["consumed"] is True
    assert corridor["effective_status"] == "passed"
    assert corridor["algorithm_semantics_stale"] is False
    assert record["fingerprints"]["cns_corridor_fingerprint"] == "corridor-fp"
    assert record["fingerprints"]["cns_corridor_consumed"] is True
    assert any(item["role"] == "cns_corridor_assessment" for item in cns["sources"])
    assert all(
        item["corridor"]["consumed"] is True for item in cns["metrics"]["subsystems"]
    )
    assert cns["status"] == "supported"


def test_only_the_cns_domain_changes_between_stale_and_current_corridor(tmp_path):
    """只改 CNS evidence 这一依赖维度：其它 safety domain 逐字段一致。"""

    current = evaluate(harness(
        tmp_path, name="current.json", upstream=_upstream(),
    ))
    stale = evaluate(harness(
        tmp_path, name="stale.json", upstream=_upstream(version=OLD_VERSION),
    ))
    assert current["domains"]["geometry_obstacle"] == stale["domains"]["geometry_obstacle"]
    assert current["domains"]["ground_exposure"] == stale["domains"]["ground_exposure"]
    assert current["domains"]["regulatory"] == stale["domains"]["regulatory"]
    assert current["domains"]["cns_operational_support"] != (
        stale["domains"]["cns_operational_support"]
    )


# ---------------------------------------------------------------------------
# 3. project reopen：不写文件、result_statuses 不变
# ---------------------------------------------------------------------------

def test_project_reopen_does_not_write_files(tmp_path):
    service = harness(tmp_path, name="reopen.json", upstream=_upstream(version=OLD_VERSION))
    state = service.state
    state.setdefault("result_statuses", {})["cns_corridor_assessment"] = "passed"
    service.save()

    path = tmp_path / "reopen.json"
    before_bytes = path.read_bytes()
    before_statuses = deepcopy(state["result_statuses"])

    reopened = type(service)(path, DEFAULTS)

    assert path.read_bytes() == before_bytes
    assert hashlib.sha256(path.read_bytes()).hexdigest() == (
        hashlib.sha256(before_bytes).hexdigest()
    )
    assert (reopened.state.get("result_statuses") or {}) == before_statuses
    assert reopened.session.pending_save is False
    #: 重开后依然必须看到 stale（算法语义投影是只读的）。
    assert effective_result_status(reopened.state, "cns_corridor_assessment") == "stale"
    assert (projected_result(reopened.state, "cns_corridor_assessment") or {})[
        "stale_reason"
    ] == "algorithm_semantics_changed"


# ---------------------------------------------------------------------------
# 4. 不重复实现 stale / 版本判断
# ---------------------------------------------------------------------------

def test_corridor_currentness_delegates_to_the_single_authority(monkeypatch):
    calls = []
    original = result_currentness.apply_algorithm_semantics_stale

    def spy(result, algorithm_id, algorithm_version):
        calls.append((result.get("algorithm_id"), algorithm_version))
        return original(result, algorithm_id, algorithm_version)

    monkeypatch.setattr(
        result_currentness, "apply_algorithm_semantics_stale", spy,
    )
    state = {
        "cns_corridor_assessment": _corridor(version=OLD_VERSION),
        "result_statuses": {"cns_corridor_assessment": "passed"},
    }

    consumption = rse._corridor_consumption(state, "R-1")

    assert calls, "stale 判定必须经 result_currentness 的权威实现"
    assert consumption["algorithm_semantics_stale"] is True
    assert consumption["consumed"] is False
    #: 只读：投影绝不改写 state。
    assert state["cns_corridor_assessment"]["status"] == "passed"
    assert state["result_statuses"]["cns_corridor_assessment"] == "passed"


def test_route_safety_service_declares_no_second_currentness_implementation():
    source = (
        Path(rse.__file__).read_text(encoding="utf-8")
    )
    for forbidden in (
        "def apply_algorithm_semantics_stale",
        "algorithm_semantics_changed\", ",
        "stored_algorithm_version",
    ):
        assert forbidden not in source, (
            "route safety evidence 绝不实现第二份 stale/版本判断："
            f"发现 {forbidden!r}"
        )
    assert "from .result_currentness import effective_result_status, projected_result" in source
