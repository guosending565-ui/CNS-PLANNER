"""Round29-J：stale authority 与 stale chain 的正式契约测试。

覆盖用户裁定的三条硬语义：

1. **唯一权威** —— 任何消费方都不得直读 raw ``result_statuses`` / raw 容器 status，
   必须经 ``result_currentness`` 的只读投影看到 ``stale``；
2. **stale 不是 failed、也不是 current** —— stored payload 逐字段保留，重新
   evaluate 当前版本后才产生 current 新结果并解除门禁；
3. **加载项目只读** —— 重开项目不写 ``result_statuses``、不自动 save
   （文件 sha256 不变）。

链覆盖：旧 P14 → P16 拒绝；旧 P15 → P16 拒绝；旧 P16 → Step6（P18）拒绝；
旧 Radar → 依赖 Radar currentness 的链看到 stale；``/api/state`` 不再显示 current/pass。
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cns_planner.application.result_currentness import (  # noqa: E402
    apply_algorithm_semantics_stale, effective_result_status, projected_result,
)
from cns_planner.application.workflow_service import WorkflowService  # noqa: E402
from cns_planner.domain.cns_corridor import normalize_cns_corridor_policy  # noqa: E402
from cns_planner.domain.cns_inputs import (  # noqa: E402
    normalize_candidate_site, normalize_device,
)
from cns_planner.domain.cns_planning_objectives import (  # noqa: E402
    normalize_cns_planning_objectives,
)


DEFAULTS = Path("cns_planner/config/defaults.json")

#: 旧语义版本 fixture：与当前实现不同即可（当前 P14=1.1 / P15=1.1 / P16=2.1 / Radar=1.2）。
OLD_P14_VERSION = "1.0"
OLD_P15_VERSION = "1.0"
OLD_P16_VERSION = "2.0"
OLD_RADAR_VERSION = "1.1"


def _vertical():
    return {
        "surface_elevation_m": 0.0, "surface_vertical_reference": "egm2008_orthometric",
        "mount_height_agl_m": 100.0, "service_origin_egm2008_m": 100.0,
        "source": "test", "confirmed": True,
    }


def _comm_device(identifier="COMM-1", *, radius=4000.0):
    return normalize_device({
        "device_id": identifier, "name": identifier, "subsystem": "C", "role": "existing",
        "radius_m": float(radius), "mtbf_h": 1000,
        "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
        "performance": {"max_latency_s": 0.2, "min_redundancy": 1},
        "coverage_geometry": {
            "model": "sphere", "slant_range_m": float(radius),
            "source": "test", "confirmed": True,
        },
        "service_model": {
            "model_family": "declared_performance", "version": "1", "technology": "dedicated_radio",
            "parameters": {"performance": {"max_latency_s": 0.2},
                           "independence_confirmed": True, "independence_group": identifier},
            "source": "test", "confirmed": True,
        },
    })


def _required(redundancy=1):
    empty = {"required": False, "status": "passed", "type": {}, "performance": {}}
    values = {"communication": dict(empty), "navigation": dict(empty), "surveillance": dict(empty)}
    values["communication"] = {
        "required": True, "status": "passed",
        "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
        "performance": {"max_latency_s": 0.5, "min_redundancy": redundancy},
    }
    return {"status": "passed", "project_default": values, "route_overrides": {}}


def _build(tmp_path):
    """构造一个 current 的 P14/P15/P16 最小项目（随后按场景改成"旧版本"）。"""

    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    state = workflow.state
    state["operational_routes"] = [{
        "route_id": "R1", "status": "passed", "path": [[-0.001, 0.0], [0.001, 0.0]],
    }]
    state["grid"] = {"status": "passed", "level": 1, "cells": [
        {"grid_id": "G1", "bbox": [-0.0005, 0.0, 0.0005, 0.001]},
    ]}
    state["grid_attributes"]["terrain"] = {"status": "passed", "cells": {}}
    state["spatial_3d"] = {
        "altitude_layers": [{
            "altitude_layer_id": "L1", "lower_altitude_m": 50, "upper_altitude_m": 150,
            "vertical_reference": "egm2008_orthometric", "source": "test",
            "confirmed": True, "status": "confirmed",
        }],
        "route_altitude_profiles": {"R1": {
            "route_id": "R1", "mode": "constant", "constant_altitude_m": 100,
            "vertical_reference": "egm2008_orthometric", "source": "test",
            "confirmed": True, "status": "confirmed",
        }},
        "site_vertical_profiles": {},
    }
    state["required_cns"] = _required()
    state["aircraft_profiles"] = {"status": "passed", "items": [{
        "aircraft_id": "A1", "name": "A1", "navigation": {}, "surveillance": {},
        "cruise_speed_mps": 20.0, "max_speed_mps": 25.0,
        "communication": {
            "confirmed": True, "status": "confirmed", "capabilities": ["radio"],
            "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
            "performance": {"max_latency_s": 0.2, "min_redundancy": 1},
        },
    }]}
    state["selected_aircraft_profile_id"] = "A1"
    state["device_catalog"] = {"status": "passed", "items": [_comm_device()]}
    state["existing_cns_facilities"] = {"status": "passed", "items": [], "count": 0}
    state["candidate_sites"] = {"status": "passed", "items": [normalize_candidate_site({
        "site_id": "S1", "name": "S1", "coordinate": [0.0, 0.0005],
        "vertical_profile": _vertical(), "site_type": "tower", "available_subsystems": ["C"],
        "usable": True, "locked": False, "source": "test",
        "planning_profile": {"reuse_class": "candidate_site", "add_device_allowed": True,
                             "source": "test", "confirmed": True},
    })], "count": 1}
    state["cns_corridor_policy"] = normalize_cns_corridor_policy({"routes": {"R1": {
        "route_id": "R1", "horizontal_half_width_m": 0,
        "vertical_lower_margin_m": 10, "vertical_upper_margin_m": 10,
        "source": "test", "confirmed": True,
    }}})
    state["cns_planning_objectives"] = normalize_cns_planning_objectives(None)
    workflow.evaluate_cns_corridor()
    workflow.evaluate_cns_corridor_gap()
    workflow.evaluate_cns_corridor_site_plan()
    return workflow


def _age(container, version):
    """把存储结果的算法版本改为旧版本，并声明 raw ``result_statuses`` 为"看起来通过"。"""

    container["algorithm_version"] = version
    return container


# ---------------------------------------------------------------------------
# 1. 唯一权威 helper 本身
# ---------------------------------------------------------------------------

def test_projection_preserves_stored_payload_and_never_writes_state():
    stored = {
        "status": "proposal_ready", "algorithm_id": "corridor_reuse_first_site_planner_v2",
        "algorithm_version": OLD_P16_VERSION, "selected_actions": [{"action_id": "A"}],
    }
    state = {"cns_corridor_site_plan": stored, "result_statuses": {"cns_corridor_site_plan": "passed"}}
    projected = projected_result(state, "cns_corridor_site_plan")
    assert projected["status"] == "stale"
    assert projected["stale_reason"] == "algorithm_semantics_changed"
    assert projected["selected_actions"] == [{"action_id": "A"}]
    #: 绝不改写 state，也绝不把 stale 当 failed。
    assert state["cns_corridor_site_plan"] is stored
    assert stored["status"] == "proposal_ready"
    assert state["result_statuses"]["cns_corridor_site_plan"] == "passed"
    assert effective_result_status(state, "cns_corridor_site_plan") == "stale"
    assert effective_result_status(state, "cns_corridor_site_plan") != "failed"


def test_effective_status_falls_back_to_raw_for_unregistered_results():
    state = {"result_statuses": {"routes": "passed", "coverage": "stale"}}
    assert effective_result_status(state, "routes") == "passed"
    assert effective_result_status(state, "coverage") == "stale"
    assert effective_result_status(state, "never_seen") == "not_calculated"


# ---------------------------------------------------------------------------
# 2. 旧 P14 / 旧 P15 → P16 拒绝
# ---------------------------------------------------------------------------

def test_old_p14_version_blocks_p16_with_p14_reason(tmp_path):
    workflow = _build(tmp_path)
    state = workflow.state
    _age(state["cns_corridor_assessment"], OLD_P14_VERSION)
    state["result_statuses"]["cns_corridor_assessment"] = "passed"   #: raw 仍"看起来通过"

    result = workflow.evaluate_cns_corridor_site_plan()["cns_corridor_site_plan"]
    assert result["status"] == "missing_data"
    assert result["selected_actions"] == []
    assert any("P14" in reason for reason in result.get("reasons") or [])
    #: stored payload 原样保留：raw 容器 status 未被改写。
    assert state["cns_corridor_assessment"]["status"] != "stale"
    assert state["cns_corridor_assessment"]["algorithm_version"] == OLD_P14_VERSION


def test_old_p15_version_blocks_p16_with_p15_reason(tmp_path):
    workflow = _build(tmp_path)
    state = workflow.state
    _age(state["cns_corridor_gap_assessment"], OLD_P15_VERSION)
    state["result_statuses"]["cns_corridor_gap_assessment"] = "passed"

    result = workflow.evaluate_cns_corridor_site_plan()["cns_corridor_site_plan"]
    assert result["status"] == "missing_data"
    assert any("P15" in reason for reason in result.get("reasons") or [])


def test_old_p14_also_blocks_p15_recompute(tmp_path):
    """旧 P14 也不能被 P15 消费：P15 重算必须 fail-closed 而不是用旧 P14 得新结论。"""

    workflow = _build(tmp_path)
    state = workflow.state
    previous_p15 = deepcopy(state["cns_corridor_gap_assessment"])
    _age(state["cns_corridor_assessment"], OLD_P14_VERSION)
    state["result_statuses"]["cns_corridor_assessment"] = "passed"

    workflow.evaluate_cns_corridor_gap()
    recomputed = state["cns_corridor_gap_assessment"]
    assert recomputed["status"] == "missing_data"
    assert recomputed["input_status"]["cns_corridor_assessment"] == "stale"
    assert previous_p15["algorithm_version"] != OLD_P14_VERSION  #: 只是证明上面重算过


# ---------------------------------------------------------------------------
# 3. 旧 P16 → Step6（P18 initialize）拒绝
# ---------------------------------------------------------------------------

def test_old_p16_version_blocks_step6_initialize(tmp_path):
    workflow = _build(tmp_path)
    state = workflow.state
    _age(state["cns_corridor_site_plan"], OLD_P16_VERSION)
    state["result_statuses"]["cns_corridor_site_plan"] = "passed"

    with pytest.raises(ValueError) as error:
        workflow.initialize_cns_plan_review()
    assert "P16" in str(error.value)
    #: stored payload 原样保留。
    assert state["cns_corridor_site_plan"]["algorithm_version"] == OLD_P16_VERSION


def test_old_p14_blocks_step6_initialize(tmp_path):
    workflow = _build(tmp_path)
    state = workflow.state
    _age(state["cns_corridor_assessment"], OLD_P14_VERSION)
    state["result_statuses"]["cns_corridor_assessment"] = "passed"

    with pytest.raises(ValueError) as error:
        workflow.initialize_cns_plan_review()
    assert "cns_corridor_assessment" in str(error.value)


def test_current_versions_pass_the_currentness_gate(tmp_path):
    """反向对照：全部 current 时 currentness 门禁必须放行。

    注意此处**不**断言 Step6 一定初始化成功：P17 运行可接受性还有一条独立且既有的
    人工工程证据门禁（缺 ``c_full_outage_max_s`` 等显式登记时 fail-closed），它不是
    本轮的 currentness 权威，也不得被本轮的改动放宽。
    """

    workflow = _build(tmp_path)
    state = workflow.state
    workflow.plan_review_service._require_current_inputs(state)  #: 不抛 ⇒ currentness 放行
    with pytest.raises(ValueError) as error:
        workflow.initialize_cns_plan_review()
    assert "需要 current" not in str(error.value)


# ---------------------------------------------------------------------------
# 4. 旧 Radar → 依赖 Radar currentness 的链看到 stale
# ---------------------------------------------------------------------------

def test_old_radar_version_projects_stale(tmp_path):
    from cns_planner.domain.radar_surveillance_layout import (
        ALGORITHM_ID, ALGORITHM_VERSION,
    )

    workflow = _build(tmp_path)
    state = workflow.state
    state["radar_surveillance_layout"] = {
        "algorithm_id": ALGORITHM_ID, "algorithm_version": OLD_RADAR_VERSION,
        "status": "passed", "items": [], "count": 0,
    }
    state["result_statuses"]["radar_surveillance_layout"] = "passed"
    assert OLD_RADAR_VERSION != ALGORITHM_VERSION, "fixture 必须真的是旧版本"

    assert effective_result_status(state, "radar_surveillance_layout") == "stale"
    assert (projected_result(state, "radar_surveillance_layout") or {})["status"] == "stale"


# ---------------------------------------------------------------------------
# 5. /api/state（workflow.snapshot()）不得继续显示 current/pass
# ---------------------------------------------------------------------------

def test_generic_snapshot_hides_raw_currentness(tmp_path):
    workflow = _build(tmp_path)
    state = workflow.state
    _age(state["cns_corridor_assessment"], OLD_P14_VERSION)
    _age(state["cns_corridor_gap_assessment"], OLD_P15_VERSION)
    _age(state["cns_corridor_site_plan"], OLD_P16_VERSION)
    state["result_statuses"].update({
        "cns_corridor_assessment": "passed",
        "cns_corridor_gap_assessment": "passed",
        "cns_corridor_site_plan": "passed",
    })

    snapshot = workflow.snapshot()   #: == /api/workflow == /api/state 的 workflow 部分
    statuses = snapshot["result_statuses"]
    assert statuses["cns_corridor_assessment"] == "stale"
    assert statuses["cns_corridor_gap_assessment"] == "stale"
    assert statuses["cns_corridor_site_plan"] == "stale"
    assert snapshot["cns_corridor_assessment"]["status"] == "stale"
    assert snapshot["cns_corridor_gap_assessment"]["status"] == "stale"
    assert snapshot["cns_corridor_site_plan"]["status"] == "stale"
    #: 步骤可进入性同样不得把 stale 当 current。
    assert snapshot["steps"]["5"] is False
    #: state 本身仍保留 raw 值（只读投影）。
    assert state["result_statuses"]["cns_corridor_assessment"] == "passed"


def test_steps_are_reopened_after_reevaluating_current_versions(tmp_path):
    workflow = _build(tmp_path)
    state = workflow.state
    _age(state["cns_corridor_assessment"], OLD_P14_VERSION)
    state["result_statuses"]["cns_corridor_assessment"] = "passed"
    assert workflow.snapshot()["steps"]["5"] is False

    #: 重新 evaluate 当前版本 ⇒ 生成 current 新结果 ⇒ 门禁解除。
    workflow.evaluate_cns_corridor()
    assert state["cns_corridor_assessment"]["algorithm_version"] != OLD_P14_VERSION
    assert effective_result_status(state, "cns_corridor_assessment") != "stale"


# ---------------------------------------------------------------------------
# 6. 重开项目：文件 sha256 不变、不自动 save、result_statuses 不被写回
# ---------------------------------------------------------------------------

def test_reopen_project_keeps_bytes_and_result_statuses(tmp_path):
    workflow = _build(tmp_path)
    state = workflow.state
    _age(state["cns_corridor_assessment"], OLD_P14_VERSION)
    state["result_statuses"]["cns_corridor_assessment"] = "passed"
    workflow.save()

    path = tmp_path / "project.json"
    before_bytes = path.read_bytes()
    before_sha = hashlib.sha256(before_bytes).hexdigest()
    before_statuses = deepcopy(workflow.state["result_statuses"])

    reopened = WorkflowService(path, DEFAULTS)

    assert hashlib.sha256(path.read_bytes()).hexdigest() == before_sha
    assert path.read_bytes() == before_bytes
    assert (reopened.state.get("result_statuses") or {}) == before_statuses
    assert reopened.session.pending_save is False
    #: 打开后依然必须看到 stale（算法语义投影是只读的）。
    assert effective_result_status(reopened.state, "cns_corridor_assessment") == "stale"


def test_apply_algorithm_semantics_stale_is_the_single_implementation():
    """唯一权威：``corridor_service`` 只是 `result_currentness` 的兼容再导出。"""

    from cns_planner.application import corridor_service, result_currentness

    assert corridor_service.apply_algorithm_semantics_stale is (
        result_currentness.apply_algorithm_semantics_stale
    )
