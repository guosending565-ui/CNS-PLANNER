"""Round 29-K：旧项目算法选择的**只读**兼容回落（``AlgorithmNotFound`` 单版本 fallback）。

裁定：

* 只有当 stored ``algorithm_id`` 仍注册、stored ``version`` 已不再注册，且**同一 id
  恰好只有一个**注册版本时，才允许回落到该当前版本实例；
* 回落**绝不**改写 ``state["algorithm_selection"]``、**绝不** save；
* 同 id 有多个可选版本 ⇒ **fail closed**，绝不猜；
* stored id 本身就未注册（算法被移除）⇒ 不回退；
* 旧结果的 currentness 仍由 ``result_currentness`` 只读投影为 ``stale``，
  重新 evaluate 才产生 current 新结果。

本文件不修改任何权威项目状态。
"""

from __future__ import annotations

from copy import deepcopy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cns_planner.algorithms.corridor.v1 import CNSServiceCorridorV1  # noqa: E402
from cns_planner.algorithms.registry import AlgorithmNotFoundError  # noqa: E402
from cns_planner.domain.algorithm_manifest import AlgorithmManifest  # noqa: E402

from test_p16_prefilter_clearance_contract import _build_state  # noqa: E402

LEGACY_VERSION = "0.0-legacy-unregistered"


def _workflow(tmp_path):
    workflow = _build_state(tmp_path)
    workflow.session.save()
    return workflow


def _legacy_corridor_selection(workflow):
    selection = workflow.state["algorithm_selection"]["corridor_model"]
    selection["version"] = LEGACY_VERSION
    return selection


# ---------------------------------------------------------------------------
# 1. 单版本回落：旧项目仍能打开
# ---------------------------------------------------------------------------

def test_single_registered_version_falls_back_read_only(tmp_path):
    workflow = _workflow(tmp_path)
    _legacy_corridor_selection(workflow)
    before = deepcopy(workflow.state["algorithm_selection"])

    instance = workflow._selected_algorithm("corridor_model")

    assert instance.algorithm_id == CNSServiceCorridorV1.algorithm_id
    assert instance.algorithm_version == CNSServiceCorridorV1.algorithm_version
    assert workflow.state["algorithm_selection"] == before, "绝不改写 algorithm_selection"

    projection = workflow.algorithm_compatibility_projection()["corridor_model"]
    assert projection["stored_algorithm_id"] == CNSServiceCorridorV1.algorithm_id
    assert projection["stored_algorithm_version"] == LEGACY_VERSION
    assert projection["effective_algorithm_id"] == CNSServiceCorridorV1.algorithm_id
    assert projection["effective_algorithm_version"] == CNSServiceCorridorV1.algorithm_version
    assert projection["compatibility_fallback_used"] is True
    assert projection["requires_reevaluation"] is True
    assert projection["writes_algorithm_selection"] is False
    assert projection["persists"] is False


def test_legacy_selection_still_evaluates_and_keeps_stored_selection(tmp_path):
    workflow = _workflow(tmp_path)
    _legacy_corridor_selection(workflow)

    #: 模拟"加载旧项目后按旧 selection 重建算法实例"。
    workflow.corridor_model = workflow._selected_algorithm("corridor_model")
    workflow.evaluate_cns_corridor()

    result = workflow.state["cns_corridor_assessment"]
    assert result["algorithm_id"] == CNSServiceCorridorV1.algorithm_id
    assert result["algorithm_version"] == CNSServiceCorridorV1.algorithm_version
    assert (
        workflow.state["algorithm_selection"]["corridor_model"]["version"] == LEGACY_VERSION
    ), "重算之后 stored selection 仍保持旧值（回落是只读的）"


# ---------------------------------------------------------------------------
# 2. 回落绝不 save
# ---------------------------------------------------------------------------

def test_fallback_does_not_save_project_state(tmp_path, monkeypatch):
    workflow = _workflow(tmp_path)
    _legacy_corridor_selection(workflow)
    path = tmp_path / "project.json"
    before_mtime = path.stat().st_mtime_ns
    calls = []
    monkeypatch.setattr(
        type(workflow.session), "save",
        lambda self, *args, **kwargs: calls.append(1), raising=True,
    )

    workflow._selected_algorithm("corridor_model")

    assert calls == [], "兼容回落绝不 save"
    assert path.stat().st_mtime_ns == before_mtime


# ---------------------------------------------------------------------------
# 3. fail closed：多版本同 id / stored id 已移除
# ---------------------------------------------------------------------------

def test_multiple_registered_versions_same_id_fail_closed(tmp_path):
    workflow = _workflow(tmp_path)
    _legacy_corridor_selection(workflow)
    registry = workflow.algorithm_registry
    base = registry.manifest(
        "corridor_model", CNSServiceCorridorV1.algorithm_id,
        CNSServiceCorridorV1.algorithm_version,
    )
    registry.register(
        AlgorithmManifest(
            algorithm_type="corridor_model", algorithm_id=base.algorithm_id,
            version="9.9-experimental", name=base.name, provider=base.provider,
            maturity="experimental", description="Round29K fail-closed fixture",
            inputs=base.inputs, outputs=base.outputs,
        ),
        lambda parameters: CNSServiceCorridorV1(parameters),
    )

    with pytest.raises(AlgorithmNotFoundError):
        workflow._selected_algorithm("corridor_model")

    projection = workflow.algorithm_compatibility_projection()["corridor_model"]
    assert projection["compatibility_fallback_used"] is False
    assert projection["requires_reevaluation"] is True
    assert "fail_closed" in projection["reason"]
    assert projection["effective_algorithm_version"] is None


def test_removed_algorithm_id_does_not_fall_back(tmp_path):
    workflow = _workflow(tmp_path)
    workflow.state["algorithm_selection"]["corridor_model"]["algorithm_id"] = (
        "removed_corridor_algorithm_v0"
    )

    with pytest.raises(AlgorithmNotFoundError):
        workflow._selected_algorithm("corridor_model")

    projection = workflow.algorithm_compatibility_projection()["corridor_model"]
    assert projection["reason"] == "stored_algorithm_id_not_registered"
    assert projection["compatibility_fallback_used"] is False
    assert projection["requires_reevaluation"] is True


def test_current_selection_reports_no_fallback(tmp_path):
    workflow = _workflow(tmp_path)

    projection = workflow.algorithm_compatibility_projection()["corridor_model"]
    assert projection["compatibility_fallback_used"] is False
    assert projection["requires_reevaluation"] is False
    assert projection["stored_algorithm_version"] == CNSServiceCorridorV1.algorithm_version
    assert (
        projection["effective_algorithm_version"] == CNSServiceCorridorV1.algorithm_version
    )


# ---------------------------------------------------------------------------
# 4. 只读投影进入通用快照（/api/state）
# ---------------------------------------------------------------------------

def test_snapshot_exposes_read_only_compatibility_projection(tmp_path):
    workflow = _workflow(tmp_path)
    _legacy_corridor_selection(workflow)

    snapshot = workflow.snapshot()

    entry = snapshot["algorithm_compatibility"]["corridor_model"]
    assert entry["stored_algorithm_version"] == LEGACY_VERSION
    assert entry["effective_algorithm_version"] == CNSServiceCorridorV1.algorithm_version
    assert entry["compatibility_fallback_used"] is True
    assert entry["requires_reevaluation"] is True
    assert (
        workflow.state["algorithm_selection"]["corridor_model"]["version"] == LEGACY_VERSION
    )
