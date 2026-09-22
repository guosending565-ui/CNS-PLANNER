"""BUG-PERSIST-REUSE-TIER-001 回归：官方 legacy 4 层 reuse tier 项目必须可加载。

冻结语义
--------
共塔 tier（``tower_colocation_host``）把 P11/P16 的 ``REUSE_TIERS`` 从旧官方 4 层升级为
当前 5 层，但持久化政策原先做严格全等检查，导致**合法旧项目**（P11 报
"P11 reuse tier 顺序不可改变"、随后 P16 报 "P16 reuse tier 顺序不可改变"）完全打不开。

本文件的回归边界：

* 旧 4 层项目可 open，且 P11/P16 两条政策都规范化到当前 5 层；
* 迁移只改这两条政策（以及迁移派生 proposal 的失效标记）：workspace / grid /
  grid_attributes / population / terrain / buildings / routes / operational_routes /
  device_catalog / existing_cns / towers 等事实与上游结果逐字段保持；
* 非官方顺序（缺项、乱序、重复、自定义）仍然被拒绝 —— 保护不被放宽；
* 当前 5 层项目完全不变；
* 有旧 P11/P16 proposal 时，迁移后不会被错误地当作新策略下的 current 派生；
* 迁移后的项目 save/reopen 仍成功，且不发生二次漂移。
"""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from cns_planner.application.project_state import normalize_project
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.cns_inputs import backfill_device_contract
from cns_planner.domain.corridor_site_planning import (
    default_corridor_site_planning_policy,
)
from cns_planner.domain.site_planning import (
    LEGACY_REUSE_TIERS_V1, REUSE_TIERS, TOWER_COLOCATION_REUSE_CLASS,
    canonicalize_reuse_tiers, default_site_planning_policy,
)


DEFAULTS = Path("cns_planner/config/defaults.json")

#: 迁移必须逐字段保持的“事实与上游结果”。故意包含 workspace / grid / routes /
#: operational_routes / population / terrain / buildings / device_catalog /
#: existing_cns / towers。
#: 说明：``spatial_3d`` 刻意不在此列 —— 旧项目恢复时会按既有契约补建工程默认高度层目录
#: （``_restore_altitude_layer_catalog``），那是与本 BUG 无关的既有加载行为，且不写盘。
FACT_FIELDS = (
    "workspace",
    "grid",
    "grid_attributes",
    "nodes",
    "node_seq",
    "route_seq",
    "retired_route_ids",
    "scenario_routes",
    "operational_routes",
    "aircraft",
    "rules",
    "devices",
    "device_catalog",
    "existing_cns_facilities",
    "candidate_sites",
    "towers",
    "cns_gap_analysis_v2",
)


def legacy_reuse_tiers():
    """官方旧 4 层顺序（与当前 5 层的唯一差别是缺 ``tower_colocation_host``）。"""

    return list(LEGACY_REUSE_TIERS_V1)


def _legacy_policy(policy, *, status, reuse_tiers=None):
    value = deepcopy(policy)
    value["reuse_tiers"] = legacy_reuse_tiers() if reuse_tiers is None else list(reuse_tiers)
    value["status"] = status
    value["confirmed"] = True
    value["source"] = "legacy-v1-engineering"
    return value


def legacy_project_document(tmp_path):
    """一个**真实形态**的旧项目文档：用当前写入器生成，再降级为旧 4 层政策。

    这样保留的 workspace / grid / grid_attributes / routes / operational_routes /
    device_catalog / existing_cns / towers 等事实都是真实结构，而 P11/P16 政策与
    proposal 是旧版本形态。
    """

    workflow = WorkflowService(tmp_path / "seed.json", DEFAULTS)
    workflow.state["project"]["name"] = "旧项目（共塔 tier 之前）"
    workflow.state["workspace"] = {
        "bbox": [120.0, 30.0, 120.01, 30.01],
        "crs": "EPSG:4326",
        "revision": 2,
        "health": {"status": "passed", "loaded_layer_count": 3},
    }
    workflow.state["grid"] = {
        "status": "passed", "level": 3,
        "cells": [
            {"grid_id": "G1", "bbox": [120.0, 30.0, 120.005, 30.005]},
            {"grid_id": "G2", "bbox": [120.005, 30.005, 120.01, 30.01]},
        ],
    }
    workflow.state["grid_attributes"]["terrain"] = {
        "status": "passed", "source": "legacy/terrain.tif", "cells": {
            "G1": {"status": "passed", "surface_elevation_mean_m": 12.5},
            "G2": {"status": "passed", "surface_elevation_mean_m": 30.0},
        },
    }
    workflow.state["grid_attributes"]["population"] = {
        "status": "passed", "source": "legacy/population.tif",
        "cells": {"G1": {"status": "passed", "value": 120.0, "value_status": "passed"}},
    }
    workflow.state["grid_attributes"]["buildings"] = {
        "status": "passed", "source": "legacy/buildings.gpkg", "cells": {
            "G1": {"status": "passed", "max_height_m": 42.0},
        },
    }
    workflow.state["grid_attributes"]["towers"] = {
        "status": "passed", "source": "legacy/towers.csv", "cells": {
            "G1": {"status": "passed", "tower_count": 3, "max_tower_height_m": 55.0},
        },
    }
    workflow.state["nodes"] = [
        {"node_id": "N1", "name": "A", "coordinate": [120.002, 30.002]},
        {"node_id": "N2", "name": "B", "coordinate": [120.008, 30.008]},
    ]
    workflow.state["route_seq"] = 1
    workflow.state["scenario_routes"] = [
        {"route_id": "R0001", "status": "passed", "path": [[120.002, 30.002], [120.008, 30.008]]},
    ]
    workflow.state["operational_routes"] = [
        {
            "route_id": "R0001", "status": "passed",
            "path": [[120.002, 30.002], [120.005, 30.005], [120.008, 30.008]],
        },
    ]
    workflow.state["devices"] = [
        {"device_id": "D1", "name": "旧设备", "subsystem": "C", "role": "existing"},
    ]
    legacy_device_catalog_item = {
        "device_id": "C-LEGACY", "name": "Legacy C", "subsystem": "C",
        "role": "existing", "radius_m": 700.0, "mtbf_h": 1000,
        "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
        "performance": {"max_latency_s": 0.2, "min_redundancy": 1},
    }
    workflow.state["device_catalog"] = {
        "status": "passed",
        # 设备目录的“旧契约 → 新契约”回填由既有兼容层（ensure_catalogs）在本函数之外完成，
        # 与本 BUG 无关；这里直接使用规范形态，从而把差异隔离到 tier 顺序迁移本身。
        "items": [backfill_device_contract(legacy_device_catalog_item)], "count": 1,
    }
    workflow.state["existing_cns_facilities"] = {
        "status": "passed", "count": 1, "items": [{
            "facility_id": "F1", "site_id": "F1", "name": "旧既有站",
            "coordinate": [120.004, 30.004], "status": "active", "source": "legacy",
            "devices": [], "planning_profile": {
                "reuse_class": "existing_cns_facility", "confirmed": True,
                "source": "legacy", "add_device_allowed": True,
            },
        }],
    }
    workflow.state["candidate_sites"] = {
        "status": "passed", "count": 1, "items": [{
            "site_id": "S1", "name": "旧候选站", "coordinate": [120.006, 30.006],
            "site_type": "tower", "usable": True, "locked": False, "source": "legacy",
            "available_subsystems": ["C"],
            "planning_profile": {
                "reuse_class": "candidate_site", "confirmed": True,
                "source": "legacy", "add_device_allowed": True,
            },
        }],
    }
    workflow.state["towers"] = {
        "status": "passed", "collection_id": "towers", "schema_version": 1,
        "source": "legacy/towers.csv",
        "items": [{
            "tower_id": "T1", "longitude": 120.004, "latitude": 30.004,
            "height_m": 55.0, "site_type": "communication_tower",
        }],
    }
    workflow.state["cns_gap_analysis_v2"] = {
        "status": "confirmed_gap", "algorithm_id": "cns_gap_analysis_v2",
        "algorithm_version": "2.0", "input_fingerprint": "legacy-gap-fingerprint",
        "routes": [{
            "route_id": "R0001", "route_length_m": 1000.0,
            "subsystems": [{"subsystem": "C", "segments": [{
                "segment_id": "R0001:C:001", "route_id": "R0001", "subsystem": "C",
                "length_m": 1000.0, "planning_status": "confirmed_gap",
                "runtime_status": "unknown", "combined_status": "confirmed_gap",
                "gap_causes": ["geometry_gap"], "remediation_scope": "ground_service_candidate",
            }]}],
        }],
    }

    # ---- 旧版本形态：两条政策都是官方 4 层，并且各自带着按旧顺序派生的 proposal。
    workflow.state["site_planning_policy"] = _legacy_policy(
        default_site_planning_policy(), status="confirmed"
    )
    workflow.state["corridor_site_planning_policy"] = _legacy_policy(
        default_corridor_site_planning_policy(), status="confirmed"
    )
    workflow.state["cns_site_plan"].update({
        "status": "proposal_ready",
        "planning_policy": deepcopy(workflow.state["site_planning_policy"]),
        "input_fingerprint": "legacy-p11-fingerprint",
        "target_segment_count": 1,
        "candidate_actions": [{"action_id": "candidate_site:S1:C-LEGACY", "reuse_class": "candidate_site"}],
        "selected_actions": [{"action_id": "candidate_site:S1:C-LEGACY", "reuse_class": "candidate_site"}],
        "reuse_counts": {
            "existing_cns_facility": 0, "existing_shared_site": 0,
            "candidate_site": 1, "new_build_candidate": 0,
        },
    })
    workflow.state["cns_corridor_site_plan"].update({
        "status": "proposal_ready",
        "planning_policy": deepcopy(workflow.state["corridor_site_planning_policy"]),
        "input_fingerprint": "legacy-p16-fingerprint",
        "target_voxel_count": 1,
        "candidate_actions": [{"action_id": "candidate_site:S1:C-LEGACY", "reuse_class": "candidate_site"}],
        "selected_actions": [{"action_id": "candidate_site:S1:C-LEGACY", "reuse_class": "candidate_site"}],
    })
    workflow.state["result_statuses"].update({
        "cns_site_plan": "passed",
        "cns_corridor_site_plan": "passed",
    })

    document = deepcopy(workflow.state)
    document.pop("_population_shelter_cache", None)
    document.pop("_planning_exposure_cache", None)
    # 旧 4 层形态的两条政策 + 两个旧 proposal 的原文（规范化前先留档）。
    legacy_policies = {
        key: deepcopy(document[key])
        for key in ("site_planning_policy", "corridor_site_planning_policy",
                    "cns_site_plan", "cns_corridor_site_plan")
    }
    # 基线规范化：device_catalog / existing_cns / 人口等“旧格式 → 新格式”的回填由既有
    # 兼容层负责，与本 BUG 无关。因此旧项目文档先取规范化后的真实形态，再把 P11/P16
    # 政策与 proposal 降级为旧 4 层形态：这样后面的逐字段比较只剩“tier 顺序迁移”的差异。
    # 注意返回的就是写盘内容，否则比较的会是“未规范化的手写形态 vs 加载后形态”。
    document = normalize_project(document, workflow.grid_service)
    document.pop("_population_shelter_cache", None)
    document.pop("_planning_exposure_cache", None)
    assert document["site_planning_policy"]["reuse_tiers"] == list(REUSE_TIERS)
    assert document["corridor_site_planning_policy"]["reuse_tiers"] == list(REUSE_TIERS)
    for key in ("site_planning_policy", "corridor_site_planning_policy",
                "cns_site_plan", "cns_corridor_site_plan"):
        document[key] = deepcopy(legacy_policies[key])
    document["result_statuses"]["cns_site_plan"] = "passed"
    document["result_statuses"]["cns_corridor_site_plan"] = "passed"
    assert document["site_planning_policy"]["reuse_tiers"] == legacy_reuse_tiers()
    assert document["corridor_site_planning_policy"]["reuse_tiers"] == legacy_reuse_tiers()
    return document


def write_legacy_project(target, document):
    """把旧项目文档写到 ``target/project_state.json``（``target`` 不存在则创建）。"""

    project_dir = Path(target)
    project_dir.mkdir(parents=True, exist_ok=True)
    store = project_dir / "project_state.json"
    store.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    return store


def _legacy_store(tmp_path, name):
    """每个用例都生成**独立**的旧项目文件（避免用例之间共享可变字典）。"""

    return write_legacy_project(tmp_path / name, legacy_project_document(tmp_path))


@pytest.fixture
def legacy_store(tmp_path):
    return _legacy_store(tmp_path, "legacy-project")


def test_legacy_four_tier_project_opens_and_policies_migrate(tmp_path, legacy_store):
    """旧 4 层项目必须能 open（修复前这里就是 "P11 reuse tier 顺序不可改变"）。"""

    document = json.loads(legacy_store.read_text(encoding="utf-8"))
    before = deepcopy(document)

    workflow = WorkflowService(legacy_store, DEFAULTS)
    state = workflow.state

    for key in ("site_planning_policy", "corridor_site_planning_policy"):
        assert state[key]["reuse_tiers"] == list(REUSE_TIERS)
        assert state[key]["reuse_tiers"].index(TOWER_COLOCATION_REUSE_CLASS) == (
            state[key]["reuse_tiers"].index("existing_shared_site") + 1
        )
        # 迁移只重写 tier 顺序：旧政策自己的已确认语义保持不变。
        assert state[key]["status"] == "confirmed"
        assert state[key]["source"] == "legacy-v1-engineering"

    # 只读打开：不写盘，也不静默改写旧项目文件。
    assert json.loads(legacy_store.read_text(encoding="utf-8")) == before


def test_migration_keeps_every_fact_and_upstream_result_field_by_field(tmp_path, legacy_store):
    """迁移不得改动事实/上游结果：逐字段比较。"""

    document = json.loads(legacy_store.read_text(encoding="utf-8"))
    state = WorkflowService(legacy_store, DEFAULTS).state

    for field in FACT_FIELDS:
        assert state[field] == document[field], f"迁移改动了事实字段 {field}"
    assert state["result_statuses"]["cns_gap_v2"] == document["result_statuses"]["cns_gap_v2"]


def test_legacy_policy_migration_do_not_keep_old_proposals_current(tmp_path, legacy_store):
    """有旧 P11/P16 proposal 时，迁移后必须失效，不能冒充新策略的 current 派生。"""

    document = json.loads(legacy_store.read_text(encoding="utf-8"))
    assert document["result_statuses"]["cns_site_plan"] == "passed"
    assert document["cns_site_plan"]["status"] == "proposal_ready"
    assert document["cns_corridor_site_plan"]["status"] == "proposal_ready"

    state = WorkflowService(legacy_store, DEFAULTS).state

    for key, status_key in (("cns_site_plan", "cns_site_plan"),
                            ("cns_corridor_site_plan", "cns_corridor_site_plan")):
        proposal = state[key]
        assert proposal["status"] == "stale"
        assert proposal["stale_reason"]
        assert state["result_statuses"][status_key] == "stale"
        # 旧 proposal 的内容原样保留（不删数据、不重算），只是不再 current。
        assert proposal["selected_actions"] == document[key]["selected_actions"]
        assert proposal["candidate_actions"] == document[key]["candidate_actions"]
        assert proposal["input_fingerprint"] == document[key]["input_fingerprint"]


def test_legacy_proposal_absent_is_left_untouched(tmp_path, legacy_store):
    """空 proposal（``not_calculated``）不产生无意义的状态改写。"""

    document = json.loads(legacy_store.read_text(encoding="utf-8"))
    document["cns_site_plan"]["status"] = "not_calculated"
    document["cns_site_plan"]["selected_actions"] = []
    document["cns_site_plan"]["candidate_actions"] = []
    document["result_statuses"]["cns_site_plan"] = "not_calculated"
    store = write_legacy_project(tmp_path / "absent", document)

    state = WorkflowService(store, DEFAULTS).state

    assert state["cns_site_plan"]["status"] == "not_calculated"
    assert state["result_statuses"]["cns_site_plan"] == "not_calculated"
    assert state["site_planning_policy"]["reuse_tiers"] == list(REUSE_TIERS)
    # P16 仍是旧 proposal，因此照常失效。
    assert state["cns_corridor_site_plan"]["status"] == "stale"


def test_save_and_reopen_migrated_project_is_stable(tmp_path):
    """迁移后 save/reopen 仍成功，且不发生二次漂移。"""

    store = _legacy_store(tmp_path, "stability")
    facts_before = json.loads(store.read_text(encoding="utf-8"))

    opened = WorkflowService(store, DEFAULTS)
    opened.save()
    reopened = WorkflowService(store, DEFAULTS)

    assert reopened.state["site_planning_policy"]["reuse_tiers"] == list(REUSE_TIERS)
    assert reopened.state["corridor_site_planning_policy"]["reuse_tiers"] == list(REUSE_TIERS)
    assert reopened.state["cns_site_plan"]["status"] == "stale"
    assert reopened.state["cns_corridor_site_plan"]["status"] == "stale"
    assert reopened.state["result_statuses"]["cns_site_plan"] == "stale"
    assert reopened.state["result_statuses"]["cns_corridor_site_plan"] == "stale"

    for field in FACT_FIELDS:
        assert reopened.state[field] == facts_before[field], (
            f"save/reopen 改动了事实字段 {field}"
        )

    first = deepcopy(reopened.state)
    reopened.save()
    second = WorkflowService(store, DEFAULTS).state
    for key in ("site_planning_policy", "corridor_site_planning_policy",
                "cns_site_plan", "cns_corridor_site_plan", "result_statuses"):
        assert second[key] == first[key], f"二次迁移漂移：{key}"


def test_current_five_tier_project_is_unchanged(tmp_path):
    """当前 5 层项目必须完全不变：政策、proposal 与状态都不被改写。"""

    workflow = WorkflowService(tmp_path / "current.json", DEFAULTS)
    workflow.state["towers"] = {
        "status": "passed", "collection_id": "towers", "schema_version": 1,
        "source": "current/towers.csv",
        "items": [{"tower_id": "T1", "longitude": 120.004, "latitude": 30.004}],
    }
    workflow.state["cns_site_plan"].update({
        "status": "proposal_ready", "input_fingerprint": "current-p11-fingerprint",
        "selected_actions": [{"action_id": "candidate_site:S9:C1"}],
        "candidate_actions": [{"action_id": "candidate_site:S9:C1"}],
    })
    workflow.state["cns_corridor_site_plan"].update({
        "status": "proposal_ready", "input_fingerprint": "current-p16-fingerprint",
        "selected_actions": [{"action_id": "candidate_site:S9:C1"}],
        "candidate_actions": [{"action_id": "candidate_site:S9:C1"}],
    })
    workflow.state["result_statuses"]["cns_site_plan"] = "passed"
    workflow.state["result_statuses"]["cns_corridor_site_plan"] = "passed"
    snapshot = deepcopy(workflow.state)
    assert snapshot["site_planning_policy"]["reuse_tiers"] == list(REUSE_TIERS)

    normalized = normalize_project(deepcopy(snapshot), workflow.grid_service)

    for key in ("site_planning_policy", "corridor_site_planning_policy",
                "cns_site_plan", "cns_corridor_site_plan", "result_statuses",
                *FACT_FIELDS):
        assert normalized[key] == snapshot[key], f"当前 5 层项目被改动：{key}"


def test_default_policies_are_not_treated_as_legacy_migration(tmp_path):
    """默认政策（无已确认语义）在回填时同样不触发迁移失效。"""

    workflow = WorkflowService(tmp_path / "defaults.json", DEFAULTS)
    state = workflow.state
    assert state["site_planning_policy"]["reuse_tiers"] == list(REUSE_TIERS)
    assert state["cns_site_plan"]["status"] == "not_calculated"
    assert state["cns_corridor_site_plan"]["status"] == "not_calculated"
    assert state["result_statuses"]["cns_site_plan"] == "not_calculated"
    assert state["result_statuses"]["cns_corridor_site_plan"] == "not_calculated"


@pytest.mark.parametrize("illegal", [
    # 官方 4 层内部的乱序
    ["existing_shared_site", "existing_cns_facility", "candidate_site", "new_build_candidate"],
    # 缺项（旧 4 层少一层 / 当前 5 层少一层）
    ["existing_cns_facility", "existing_shared_site", "candidate_site"],
    list(REUSE_TIERS)[:-1],
    # 重复
    ["existing_cns_facility", "existing_shared_site", "existing_shared_site",
     "candidate_site", "new_build_candidate"],
    # 当前 5 层 + 额外项
    [*REUSE_TIERS, "custom_tier"],
    # 自定义顺序 / 自定义 tier
    ["custom_tier", "existing_cns_facility", "existing_shared_site",
     "candidate_site", "new_build_candidate"],
    [],
])
def test_illegal_reuse_tier_orders_are_still_rejected(tmp_path, illegal):
    """非官方顺序绝不放宽：缺项、乱序、重复、自定义顺序一律 raise。"""

    workflow = WorkflowService(tmp_path / "illegal.json", DEFAULTS)
    document = deepcopy(workflow.state)
    document["site_planning_policy"]["reuse_tiers"] = list(illegal)
    document["corridor_site_planning_policy"]["reuse_tiers"] = list(illegal)

    with pytest.raises(ValueError, match="P11 reuse tier 顺序不可改变"):
        normalize_project(deepcopy(document), workflow.grid_service)

    document["site_planning_policy"]["reuse_tiers"] = list(REUSE_TIERS)
    with pytest.raises(ValueError, match="P16 reuse tier 顺序不可改变"):
        normalize_project(deepcopy(document), workflow.grid_service)


def test_canonicalizer_accepts_only_current_or_official_legacy_order():
    """迁移 helper 的接受面必须恰好是「当前 5 层」+「官方旧 4 层」两种。"""

    assert canonicalize_reuse_tiers(list(REUSE_TIERS)) == (list(REUSE_TIERS), False)
    migrated, flag = canonicalize_reuse_tiers(list(LEGACY_REUSE_TIERS_V1))
    assert flag is True
    assert migrated == list(REUSE_TIERS)
    # 旧 4 层本身不等于当前 5 层：这就是修复前旧项目被严格全等检查拒绝的原因。
    assert list(LEGACY_REUSE_TIERS_V1) != list(REUSE_TIERS)

    with pytest.raises(ValueError):
        canonicalize_reuse_tiers(None)
    with pytest.raises(ValueError):
        canonicalize_reuse_tiers(list(reversed(LEGACY_REUSE_TIERS_V1)))
    with pytest.raises(ValueError):
        canonicalize_reuse_tiers(LEGACY_REUSE_TIERS_V1 + (TOWER_COLOCATION_REUSE_CLASS,))


def test_legacy_p11_rejection_no_longer_blocks_but_p16_uses_same_rule(tmp_path):
    """第 4 点：P11 修完后 P16 也必须复用同一迁移规则（不能只修 P11）。"""

    store = _legacy_store(tmp_path, "p16-only")
    document = json.loads(store.read_text(encoding="utf-8"))
    # P11 保持当前 5 层，只把 P16 还原成旧 4 层：P16 也必须能加载。
    document["site_planning_policy"]["reuse_tiers"] = list(REUSE_TIERS)
    document["corridor_site_planning_policy"]["reuse_tiers"] = legacy_reuse_tiers()
    store = write_legacy_project(tmp_path / "p16-only-rewritten", document)

    state = WorkflowService(store, DEFAULTS).state

    assert state["corridor_site_planning_policy"]["reuse_tiers"] == list(REUSE_TIERS)
    assert state["cns_corridor_site_plan"]["status"] == "stale"
    # P11 没有迁移，因此它的 proposal 保持原状。
    assert state["cns_site_plan"]["status"] == "proposal_ready"
    assert state["result_statuses"]["cns_site_plan"] == "passed"
