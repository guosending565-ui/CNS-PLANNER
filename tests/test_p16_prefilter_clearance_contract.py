"""P16 保守预筛的**净空契约**（Round 29-K 从已撤销的 Round29-I 差分文件中回收）。

Round 29-I 的 local impact 引擎已从 production 撤销，其差分等价性测试不再适用；
但同一文件里的**预筛边界方向**断言只依赖 HEAD 已有的
``_build_prefilter_context`` / ``_prefilter_impact``，与 local engine 无关，因此
按"不留死代码、也不丢守卫"的原则单独保留：

1. ``required_clearance = envelope + corridor_max_half_width + max_cell_half_diagonal``
   （三项之和，缺一项都会让预筛过于激进）；
2. ``max_cell_half_diagonal`` 越大 ⇒ 净空越大 ⇒ 越**难**预筛 ⇒ 进入 full rerun 的
   候选越多。方向写反会**静默漏掉**本应重算的候选，因此单独设测。

本文件不运行真实权威 P16 的 final 结论，也不改写任何权威项目状态。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cns_planner.application.corridor_site_planning_service import (  # noqa: E402
    _action_planner_family, _build_prefilter_context, _prefilter_impact,
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

#: C 类全向站址的 canonical planner family（``_action_planner_family`` 的唯一来源）。
OMNIDIRECTIONAL_SITE_FAMILY = "omnidirectional_site"


def _vertical():
    return {
        "surface_elevation_m": 0.0, "surface_vertical_reference": "egm2008_orthometric",
        "mount_height_agl_m": 100.0, "service_origin_egm2008_m": 100.0,
        "source": "test", "confirmed": True,
    }


def _planning_profile(reuse_class):
    return {
        "reuse_class": reuse_class, "add_device_allowed": True,
        "source": "test", "confirmed": True,
    }


def _comm_device(identifier="COMM-1", *, radii=None, technology="dedicated_radio", radius=4000.0):
    if radii is None:
        geometry = {
            "model": "sphere", "slant_range_m": radius,
            "source": "test", "confirmed": True,
        }
    else:
        geometry = {
            "model": "hemisphere", "radius_by_surface": dict(radii),
            "source": "test", "confirmed": True,
        }
    return normalize_device({
        "device_id": identifier, "name": identifier, "subsystem": "C", "role": "existing",
        "radius_m": float(radius), "mtbf_h": 1000,
        "type": {"technology": technology, "interfaces": ["ip"]},
        "performance": {"max_latency_s": 0.2, "min_redundancy": 1},
        "coverage_geometry": geometry,
        "service_model": {
            "model_family": "declared_performance", "version": "1", "technology": technology,
            "parameters": {"performance": {"max_latency_s": 0.2},
                           "independence_confirmed": True, "independence_group": identifier},
            "source": "test", "confirmed": True,
        },
    })


def _required(redundancy=1):
    empty = {"required": False, "status": "passed", "type": {}, "performance": {}}
    return {
        "status": "passed",
        "project_default": {
            "communication": {
                "required": True, "status": "passed",
                "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
                "performance": {"max_latency_s": 0.5, "min_redundancy": redundancy},
            },
            "navigation": dict(empty),
            "surveillance": dict(empty),
        },
        "route_overrides": {},
    }


def _aircraft():
    return {
        "aircraft_id": "A1", "name": "A1", "navigation": {}, "surveillance": {},
        "cruise_speed_mps": 20.0, "max_speed_mps": 25.0,
        "communication": {
            "confirmed": True, "status": "confirmed", "capabilities": ["radio"],
            "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
            "performance": {"max_latency_s": 0.2, "min_redundancy": 1},
        },
    }


def _candidate(identifier, *, reuse="candidate_site", coordinate=None):
    return normalize_candidate_site({
        "site_id": identifier, "name": identifier,
        "coordinate": list(coordinate or [0.0, 0.0005]),
        "vertical_profile": _vertical(), "site_type": "tower", "available_subsystems": ["C"],
        "usable": True, "locked": False, "source": "test",
        "planning_profile": _planning_profile(reuse),
    })


def _build_state(tmp_path, *, devices=None, candidates=None):
    """最小但完整的 P14/P15 项目状态（1 个走廊格 / 1 条航路）。"""

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
    state["aircraft_profiles"] = {"status": "passed", "items": [_aircraft()]}
    state["selected_aircraft_profile_id"] = "A1"
    state["device_catalog"] = {"status": "passed", "items": devices or [_comm_device()]}
    state["existing_cns_facilities"] = {"status": "passed", "items": [], "count": 0}
    selected = candidates if candidates is not None else [_candidate("S1")]
    state["candidate_sites"] = {
        "status": "passed", "items": selected, "count": len(selected),
    }
    state["cns_corridor_policy"] = normalize_cns_corridor_policy({"routes": {"R1": {
        "route_id": "R1", "horizontal_half_width_m": 0,
        "vertical_lower_margin_m": 10, "vertical_upper_margin_m": 10,
        "source": "test", "confirmed": True,
    }}})
    state["cns_planning_objectives"] = normalize_cns_planning_objectives(None)
    workflow.evaluate_cns_corridor()
    workflow.evaluate_cns_corridor_gap()
    return workflow


def _p16_ordinary_actions(workflow):
    """运行一次 P16，取回**真实**的普通全向站址候选动作（绝不自己重造映射）。"""

    state = workflow.state
    workflow.evaluate_cns_corridor_site_plan()
    plan = state.get("cns_corridor_site_plan") or {}
    return [
        action for action in plan.get("candidate_actions") or []
        if action.get("device_id")
        and _action_planner_family(action) == OMNIDIRECTIONAL_SITE_FAMILY
    ]


def test_prefilter_half_diagonal_direction(tmp_path):
    """half_diagonal 越大 ⇒ required_clearance 越大 ⇒ 越**难** prefilter。"""

    small_radii = {"land": 40.0, "coastal_uncertain": 40.0, "sea": 40.0}
    workflow = _build_state(tmp_path, devices=[_comm_device(radii=small_radii, radius=40.0)])
    state = workflow.state
    actions = _p16_ordinary_actions(workflow)
    assert actions, "候选必须产生至少一个普通站点动作"
    action = actions[0]

    small = _build_prefilter_context(state, [{"half_diagonal": 0.0}])
    large = _build_prefilter_context(state, [{"half_diagonal": 100_000.0}])
    assert _prefilter_impact(action, 1, small) is not None, "净空小 ⇒ 应被预筛"
    assert _prefilter_impact(action, 1, large) is None, "净空大 ⇒ 不得被预筛"


def test_prefilter_required_clearance_is_sum_of_three_terms(tmp_path):
    workflow = _build_state(
        tmp_path, candidates=[_candidate("S1", coordinate=[0.0, 0.2])],
    )
    state = workflow.state
    actions = _p16_ordinary_actions(workflow)
    assert actions
    context = _build_prefilter_context(state, [{"half_diagonal": 12.5}])
    impact = _prefilter_impact(actions[0], 1, context)
    assert impact is not None
    prefilter = impact["prefilter"]
    assert prefilter["required_clearance_m"] == pytest.approx(
        prefilter["provider_envelope_m"]
        + prefilter["corridor_max_half_width_m"]
        + prefilter["max_cell_half_diagonal_m"]
    )
    assert prefilter["semantics"].startswith("conservative_prefilter_provably_zero_impact")
