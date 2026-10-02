"""TOWER-ORIGIN-DIAG-001 回归：塔原点未解析的**原因可审计**且 round-trip 不丢证据。

本轮审计结论（真实项目 revision 218）：

* total 373 / resolved 77 / unresolved 296；
* 296 座中 **0 座**缺坐标、缺地面正高或缺塔高，**0 座** CRS 问题；
* **115 座** ``missing_origin_type``（源数据无法判定地面/楼面）；
* **181 座** ``rooftop_not_in_building_footprint``（第三方建筑足迹与塔坐标几何不对应）；
* 另有 4 座 ``base_type=unknown`` 但坐标已命中建筑足迹、1 座楼面塔几何容差 0.408 m、
  3 座楼面塔容差 ≤1 m —— 这些属于**规则性推断**（命中足迹是强证据但非充要条件），
  按本轮裁定"不得自动生成假塔基"**保持 unresolved**，只如实分类与披露。

因此本文件锁定两件事：

1. 未解析原因分类**互斥且完备**（可回归审计数字）；
2. 塔事实评估的诊断证据（``facts_status`` / ``fact_method`` / ``building_source`` /
   ``tower_source``）在 save/reopen 往返中**不再丢失**。
"""

from copy import deepcopy
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cns_planner.domain.tower_obstacle import (  # noqa: E402
    TOWER_ORIGIN_UNRESOLVED_REASONS, build_tower_obstacle_profile,
    empty_tower_obstacle_profiles, normalize_tower_obstacle_profiles,
    tower_origin_unresolved_reason, tower_origin_unresolved_summary,
)

PROJECT_STATE = (
    ROOT / "_runtime_projects" / "zhoushan_screenshot_01_20261001_1647" / "project_state.json"
)


def _tower(**updates):
    tower = {
        "tower_id": "T1", "name": "T1", "longitude": 122.2, "latitude": 29.9,
        "height_m": 30.0, "site_type": "角钢塔", "source": "synthetic_fixture",
    }
    tower.update(updates)
    return tower


def _terrain(status="passed", elevation=10.0, reason=None):
    return {
        "status": status, "elevation_m": elevation if status == "passed" else None,
        "vertical_reference": "egm2008_orthometric", "reason": reason,
    }


def _building(status="passed", height=20.0, reason=None):
    return {
        "status": status, "building_height_m": height if status == "passed" else None,
        "source": "synthetic_fixture", "reason": reason,
    }


# ---------------------------------------------------------------------------
# 1. 诊断证据必须 round-trip
# ---------------------------------------------------------------------------

def test_empty_profiles_expose_diagnostic_keys_as_none():
    empty = empty_tower_obstacle_profiles()
    for key in ("facts_status", "fact_method", "building_source", "tower_source"):
        assert key in empty and empty[key] is None


def test_normalize_preserves_tower_origin_diagnostic_evidence():
    profiles = empty_tower_obstacle_profiles("passed")
    profiles.update({
        "facts_status": "provided",
        "fact_method": "fabdem_dtm_22m_window_plus_real_building_footprint_intersects",
        "building_source": "zhoushan_buildings.gpkg",
        "tower_source": "towers.xlsx",
    })
    restored = normalize_tower_obstacle_profiles(profiles)

    assert restored["facts_status"] == "provided"
    assert restored["fact_method"] == profiles["fact_method"]
    assert restored["building_source"] == "zhoushan_buildings.gpkg"
    assert restored["tower_source"] == "towers.xlsx"
    #: 二次往返必须稳定（save → reopen → save 不再漂移）。
    assert normalize_tower_obstacle_profiles(restored) == restored


def test_normalize_keeps_missing_diagnostics_as_none_and_does_not_invent():
    restored = normalize_tower_obstacle_profiles({"status": "passed", "items": {}})
    assert restored["facts_status"] is None
    assert restored["fact_method"] is None
    assert restored["building_source"] is None
    assert restored["tower_source"] is None


# ---------------------------------------------------------------------------
# 2. 未解析原因分类：互斥、完备、不伪造
# ---------------------------------------------------------------------------

def test_reason_classification_follows_the_authoritative_short_circuit_chain():
    resolved_ground = build_tower_obstacle_profile(
        _tower(site_type="落地角钢塔"), terrain=_terrain(), building=_building(),
    )
    assert resolved_ground["status"] == "resolved"
    assert tower_origin_unresolved_reason(resolved_ground)["reason"] == "resolved"

    #: 楼面塔：地形 + 建筑 + 塔高。
    resolved_rooftop = build_tower_obstacle_profile(
        _tower(site_type="楼面角钢塔"), terrain=_terrain(), building=_building(),
    )
    assert resolved_rooftop["status"] == "resolved"
    assert tower_origin_unresolved_reason(resolved_rooftop)["reason"] == "resolved"

    #: 站址类型无法判定地面/楼面 ⇒ 绝不假定从地面起算。
    unknown_type = build_tower_obstacle_profile(
        _tower(site_type="角钢塔"), terrain=_terrain(), building=_building(),
    )
    assert unknown_type["status"] == "unresolved"
    assert tower_origin_unresolved_reason(unknown_type)["reason"] == "missing_origin_type"

    missing_height = build_tower_obstacle_profile(
        _tower(site_type="落地角钢塔", height_m=None), terrain=_terrain(), building=_building(),
    )
    assert tower_origin_unresolved_reason(missing_height)["reason"] == "missing_height"

    missing_ground = build_tower_obstacle_profile(
        _tower(site_type="落地角钢塔"), terrain=_terrain("missing_data", reason="no_dem"),
        building=_building(),
    )
    assert tower_origin_unresolved_reason(missing_ground)["reason"] == "missing_ground_elevation"

    rooftop_without_footprint = build_tower_obstacle_profile(
        _tower(site_type="楼面角钢塔"), terrain=_terrain(),
        building=_building("missing_data", reason="tower_coordinate_matches_no_building_footprint"),
    )
    classified = tower_origin_unresolved_reason(rooftop_without_footprint)
    assert classified["reason"] == "rooftop_not_in_building_footprint"
    assert classified["detail"] == "tower_coordinate_matches_no_building_footprint"

    #: 缺坐标是**最高优先级**的独立分类（真实数据里为 0 座，但必须可判定）。
    no_coordinate = build_tower_obstacle_profile(
        _tower(longitude=None, site_type="落地角钢塔"), terrain=_terrain(), building=_building(),
    )
    assert tower_origin_unresolved_reason(no_coordinate)["reason"] == "missing_coordinate"


def test_reason_classification_never_upgrades_unknown_to_resolved():
    """命中建筑足迹本身**不是**解析依据：base_type 未知就保持 unknown（fail-closed）。"""

    profiles = {}
    for index, site_type in enumerate(("角钢塔", "H杆塔", "单管塔", "造型景观塔")):
        tower = _tower(tower_id=f"T{index}", site_type=site_type)
        profiles[f"T{index}"] = build_tower_obstacle_profile(
            tower, terrain=_terrain(), building=_building(height=92.8),
        )
    collection = {**empty_tower_obstacle_profiles("passed"), "items": profiles}
    summary = tower_origin_unresolved_summary(collection)

    assert summary["total"] == 4
    assert summary["resolved"] == 0, "无位置前缀的塔型不得被推断成落地塔"
    assert summary["unresolved"] == 4
    assert summary["by_reason"]["missing_origin_type"] == 4
    assert all(
        item["tower_top_orthometric_m"] is None for item in profiles.values()
    ), "未解析时绝不产生塔顶高度"


def test_summary_is_mutually_exclusive_and_complete():
    profiles = empty_tower_obstacle_profiles("passed")
    items = {}
    items["R1"] = build_tower_obstacle_profile(
        _tower(tower_id="R1", site_type="落地角钢塔"), terrain=_terrain(), building=_building(),
    )
    items["U1"] = build_tower_obstacle_profile(
        _tower(tower_id="U1", site_type="角钢塔"), terrain=_terrain(), building=_building(),
    )
    items["U2"] = build_tower_obstacle_profile(
        _tower(tower_id="U2", site_type="楼面角钢塔"), terrain=_terrain(),
        building=_building("missing_data", reason="tower_coordinate_matches_no_building_footprint"),
    )
    profiles["items"] = items
    summary = tower_origin_unresolved_summary(profiles)

    assert summary["total"] == 3 and summary["resolved"] == 1 and summary["unresolved"] == 2
    assert sum(summary["by_reason"].values()) == summary["total"], "分类必须完备互斥"
    assert set(summary["by_reason"]) == set(TOWER_ORIGIN_UNRESOLVED_REASONS)
    assert summary["by_reason"]["resolved"] == 1
    assert summary["by_reason"]["missing_origin_type"] == 1
    assert summary["by_reason"]["rooftop_not_in_building_footprint"] == 1
    assert summary["by_reason"]["missing_coordinate"] == 0


# ---------------------------------------------------------------------------
# 3. 真实项目审计数字（缺项目数据时跳过）
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not PROJECT_STATE.exists(), reason="需要真实运行项目的 project_state.json")
def test_real_project_tower_origin_audit_numbers_are_reproducible():
    import json

    state = json.loads(PROJECT_STATE.read_text(encoding="utf-8"))
    profiles = state.get("tower_obstacle_profiles") or {}
    summary = tower_origin_unresolved_summary(profiles)

    assert summary["total"] == 373
    assert summary["resolved"] == 77
    assert summary["unresolved"] == 296
    assert summary["by_reason"]["missing_coordinate"] == 0
    assert summary["by_reason"]["missing_ground_elevation"] == 0
    assert summary["by_reason"]["missing_height"] == 0
    assert summary["by_reason"]["missing_origin_type"] == 115
    assert summary["by_reason"]["rooftop_not_in_building_footprint"] == 181
    assert summary["by_reason"]["other"] == 0
    assert sum(summary["by_reason"].values()) == summary["total"]

    #: 与档案自身的持久化计数一致（两套数字必须互证）。
    assert profiles.get("resolved_count") == summary["resolved"]
    assert profiles.get("unresolved_count") == summary["unresolved"]
