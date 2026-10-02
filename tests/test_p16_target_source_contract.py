"""Round 2.4 —— P16 target 的**来源可解释性**与 legacy 目标污染回归。

裁定原文：

* 本轮之后每一个 P16 **建站 target** 都必须回答"为什么需要它"，来源只能是
  canonical service（``C:communication`` / ``S:rid_cooperative`` /
  仅在明确要求时的 ``N:rtk_augmentation``）；
* **不得**再存在无法解释的 legacy target 作为 Step6 blocker。legacy 兼容数据
  可以显示，但**不得**凭空成为新的 planning objective；
* 服务注册表把 ``N:navigation`` / ``S:surveillance`` 的
  ``supports_site_planning`` 明确置为 ``False``（它们没有 canonical planner
  family，设备目录也不承载这类设备），因此它们**绝不产生建站目标**——
  否则会造出一批永远无法被任何候选动作满足的目标，把 Step6 永久卡在 ``not_met``。
"""

from copy import deepcopy
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cns_planner.application.corridor_site_planning_service import (  # noqa: E402
    _targets,
)
from cns_planner.domain.cns_service_contract import (  # noqa: E402
    SERVICE_KEY_COMMUNICATION, SERVICE_KEY_NAVIGATION,
    SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION, SERVICE_KEY_RID_COOPERATIVE,
    SERVICE_KEY_SURVEILLANCE,
)
from cns_planner.domain.cns_service_registry import service_registry_entry  # noqa: E402
from cns_planner.domain.corridor_site_planning import corridor_target_id  # noqa: E402
from cns_planner.site_planner.corridor_reuse_first_v2 import (  # noqa: E402
    CorridorReuseFirstSitePlannerV2,
)

ROUTE = "R1"
VOXEL = "VOXEL-1"


def _subsystem_entry(code, *, services=None, combined="confirmed_gap"):
    entry = {
        "subsystem": code, "combined_status": combined,
        "required_redundancy": 1, "qualified_provider_count": 0,
        "confirmed_independent_provider_count": 0,
        "causes": ["subsystem_deficit"], "reasons": [],
        "geometry": {"covered": False},
    }
    if services is not None:
        entry["services"] = list(services)
    return entry


def _assessment(subsystems, *, confirmed_codes=(), unknown_codes=()):
    """构造一份 P15 assessment。

    ``route["subsystems"]`` 汇总里的 ``confirmed_target_voxel_ids`` /
    ``unknown_voxel_ids`` 是 ``_targets`` 判定"该体元是不是 confirmed target"的
    权威输入，因此必须与 ``voxels[].subsystems[]`` 同时给出。
    """

    codes = [entry.get("subsystem") for entry in subsystems]
    summaries = [{
        "subsystem": code,
        "confirmed_target_voxel_ids": [VOXEL] if code in confirmed_codes else [],
        "unknown_voxel_ids": [VOXEL] if code in unknown_codes else [],
    } for code in ("C", "N", "S") if code in codes]
    return {"status": "failed", "routes": [{
        "route_id": ROUTE, "voxel_count": 1, "subsystems": summaries,
        "voxels": [{"voxel_id": VOXEL, "grid_id": "G1", "altitude_layer_id": "ALT-100",
                    "surface_class": "land", "discretized_volume_proxy_m3": 1000.0,
                    "nearest_route_offset_m": 100.0, "subsystems": list(subsystems)}],
    }]}


def _surface_service(key, status="confirmed_deficit", required=1, current=0, **extra):
    item = {
        "service_key": key, "status": status, "surface_class": "land",
        "surface_dependent": True, "supports_site_planning": True,
        "counting_basis": "distinct_site_id",
        "required_distinct_site_count": required, "distinct_site_count": current,
        "distinct_site_ids": [f"site-{index}" for index in range(current or 0)],
    }
    item.update(extra)
    return item


# --------------------------------------------------------------------------- 1
def test_legacy_navigation_gap_never_becomes_a_station_building_target():
    """没有显式 ``N:rtk_augmentation`` 时，legacy N 缺口**不得**成为建站目标。"""

    assessment = _assessment([_subsystem_entry("N")], confirmed_codes=["N"])
    targets, unknown = _targets(assessment)

    assert targets == [], "legacy N 缺口不得产生建站 target"
    assert len(unknown) == 1, unknown
    gap = unknown[0]
    assert gap["kind"] == "non_site_plannable_gap"
    assert gap["service_key"] == SERVICE_KEY_NAVIGATION
    assert gap["subsystem"] == "N"
    assert gap["requires_canonical_service_requirement"] is True
    assert any("不属于站址规划形态" in reason for reason in gap["reasons"])
    assert any("N:rtk_augmentation" in reason for reason in gap["reasons"])


def test_legacy_surveillance_gap_is_registered_not_planned():
    """同理：legacy S 缺口也不得建站（RID / Radar 必须显式声明 canonical 服务）。"""

    assessment = _assessment([_subsystem_entry("S")], confirmed_codes=["S"])
    targets, unknown = _targets(assessment)
    assert targets == []
    assert unknown[0]["service_key"] == SERVICE_KEY_SURVEILLANCE
    assert unknown[0]["kind"] == "non_site_plannable_gap"


def test_legacy_registry_entries_explicitly_refuse_site_planning():
    """语义来源：注册表把两个 legacy 身份的站址规划能力显式置为 False。"""

    for key in (SERVICE_KEY_NAVIGATION, SERVICE_KEY_SURVEILLANCE):
        entry = service_registry_entry(key)
        assert entry["supports_site_planning"] is False, key
        assert entry["planner_family"] == "legacy_subsystem", key


# --------------------------------------------------------------------------- 2
def test_every_station_building_target_has_a_canonical_service_source():
    """本轮之后**每个建站 target 都有明确 canonical 服务来源**。"""

    assessment = _assessment([
        _subsystem_entry("C", services=[_surface_service(SERVICE_KEY_COMMUNICATION)]),
        _subsystem_entry("S", services=[_surface_service(SERVICE_KEY_RID_COOPERATIVE)]),
        _subsystem_entry("N"),
    ], confirmed_codes=["C", "S", "N"])
    targets, unknown = _targets(assessment)

    assert {item["service_key"] for item in targets} == {
        SERVICE_KEY_COMMUNICATION, SERVICE_KEY_RID_COOPERATIVE,
    }
    for target in targets:
        assert target["target_id"] == corridor_target_id(
            ROUTE, VOXEL, target["service_key"],
        )
        assert target["target_scope"] == "service"
        assert service_registry_entry(target["service_key"])[
            "supports_site_planning"
        ] is True
    #: legacy N 只出现在 unknown 登记里，绝不混进 targets。
    assert all(item.get("service_key") != SERVICE_KEY_NAVIGATION for item in targets)
    assert any(
        item.get("kind") == "non_site_plannable_gap" and item["subsystem"] == "N"
        for item in unknown
    )


def test_rtk_augmentation_still_produces_a_reference_station_target_when_required():
    """显式要求 ``N:rtk_augmentation`` 时，站址缺口仍然产生导航参考站目标。"""

    assessment = _assessment([
        _subsystem_entry("N", services=[{
            "service_key": SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION,
            "status": "confirmed_deficit", "surface_class": "land",
            "surface_dependent": False, "supports_site_planning": True,
            "counting_basis": "distinct_site_id",
            "required_distinct_site_count": 2, "distinct_site_count": 0,
            "distinct_site_ids": [],
            "gap_causes": ["reference_station_deficit"],
            "delivery_status": "confirmed_deficit",
            "delivery_service_key": SERVICE_KEY_COMMUNICATION,
        }]),
    ], confirmed_codes=["N"])
    targets, unknown = _targets(assessment)

    assert len(targets) == 1, (targets, unknown)
    target = targets[0]
    assert target["service_key"] == SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION
    assert target["planner_family"] == "navigation_reference_station"
    assert target["remaining_units"] == 2
    #: ``delivery_resolved`` 的语义是"**不存在** correction-delivery 缺口"；
    #: 本用例的 gap_causes 只有站址缺口 ⇒ 投递已解决，无需再依赖通信动作。
    assert target["delivery_resolved"] is True
    assert all(item.get("kind") != "non_site_plannable_gap" for item in unknown)


def test_rtk_augmentation_with_both_gaps_reports_unresolved_delivery():
    """站址与投递同时缺口时：仍建导航站，但如实记录投递未解决。"""

    assessment = _assessment([
        _subsystem_entry("N", services=[{
            "service_key": SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION,
            "status": "confirmed_deficit", "surface_class": "land",
            "surface_dependent": False, "supports_site_planning": True,
            "counting_basis": "distinct_site_id",
            "required_distinct_site_count": 1, "distinct_site_count": 0,
            "distinct_site_ids": [],
            "gap_causes": ["reference_station_deficit", "correction_delivery_deficit"],
            "delivery_status": "confirmed_deficit",
            "delivery_service_key": SERVICE_KEY_COMMUNICATION,
        }]),
    ], confirmed_codes=["N"])
    targets, unknown = _targets(assessment)
    assert len(targets) == 1, (targets, unknown)
    assert targets[0]["delivery_resolved"] is False
    assert targets[0]["delivery_service_key"] == SERVICE_KEY_COMMUNICATION
    assert all(item.get("kind") != "non_site_plannable_gap" for item in unknown)


def test_rtk_augmentation_without_station_deficit_never_builds_a_station():
    """只有 correction delivery 缺口时绝不建导航站（把动作交给通信 planner）。"""

    assessment = _assessment([
        _subsystem_entry("N", services=[{
            "service_key": SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION,
            "status": "confirmed_deficit", "surface_class": "land",
            "surface_dependent": False, "supports_site_planning": True,
            "counting_basis": "distinct_site_id",
            "required_distinct_site_count": 1, "distinct_site_count": 1,
            "distinct_site_ids": ["ref-1"],
            "gap_causes": ["correction_delivery_deficit"],
        }]),
    ], confirmed_codes=["N"])
    targets, unknown = _targets(assessment)
    assert targets == []
    dependency = [item for item in unknown if item.get("dependency_only")]
    assert len(dependency) == 1
    assert dependency[0]["recommended_dependency_service"] == SERVICE_KEY_COMMUNICATION


# --------------------------------------------------------------------------- 3
def test_service_group_aggregation_covers_required_confirmed_unknown_selected():
    """按 service 分组的统计必须同时覆盖 required / 缺口 / 仍缺证据 / 已选动作。"""

    comm = {
        "target_id": corridor_target_id(ROUTE, VOXEL, SERVICE_KEY_COMMUNICATION),
        "service_key": SERVICE_KEY_COMMUNICATION, "subsystem": "C",
    }
    legacy_n = {
        "target_id": corridor_target_id(ROUTE, VOXEL, "N"),
        "subsystem": "N",
    }
    baseline = _assessment([_subsystem_entry("C")], confirmed_codes=["C"])
    final = deepcopy(baseline)
    planner = CorridorReuseFirstSitePlannerV2()
    result = planner.assemble(
        baseline=baseline, final=final, policy={}, targets=[comm, legacy_n],
        actions=[], impacts=[],
        selected=[{"action_id": "A1", "service_key": SERVICE_KEY_COMMUNICATION,
                   "impact": {"unknown_targets": [legacy_n["target_id"]]}}],
        iteration_trace=[], unknown_evidence=[], consistency=True,
    )
    groups = {item["bucket"]: item for item in result["target_service_groups"]}
    assert groups[SERVICE_KEY_COMMUNICATION]["required"] == 1
    assert groups[SERVICE_KEY_COMMUNICATION]["selected"] == 1
    #: 没有 service_key 的目标归入 legacy 分组，绝不混进正式服务口径。
    assert groups["legacy:N"]["required"] == 1
    assert groups["legacy:N"]["service_key"] is None
    assert groups["legacy:N"]["legacy_subsystem"] == "N"
    #: ``final`` 与 baseline 同样缺少 legacy N 的体素条目 ⇒ 它是 confirmed 残差。
    assert groups["legacy:N"]["confirmed_gap"] == 1
