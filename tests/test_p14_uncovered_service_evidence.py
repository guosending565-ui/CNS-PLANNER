"""P14 服务证据回归：**已确认无覆盖**不得被降级成"证据不足"。

本轮闭环缺陷（Round 2.2 定位）：

1. Required CNS Adopt 后 ``required_cns`` 显式携带 ``services``（A 项）；
2. P14 因此走 service-aware 分支，并用 service 证据**覆盖** subsystem 的
   ``planning_status``；
3. 但若某采样点**完全没有**任何 provider 覆盖，``summarize_service_redundancy`` 不会为
   该 service 生成任何条目（它只汇总出现过的 provider）⇒ 状态被记成 ``unknown``；
4. P15 于是把**已确认缺口**（P7 覆盖门控明确不通过）判成"证据不足"，P16 也不再把它
   当作 confirmed target —— 规划链因此永远拿不到候选动作。

本文件锁定修复后的语义边界：

* P7 ``covered is False`` + 该 service 的要求站址数**可解析** ⇒ ``confirmed_deficit``；
* 要求站址数不可解析（surface 未知等）⇒ 仍然 ``unknown``（绝不升级）；
* ``covered`` 未知（垂直证据不足）⇒ 仍然 ``unknown``；
* legacy 项目（无显式 ``services``）输出逐项不变。
"""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_cns_corridor import (  # noqa: E402
    GRID, ROUTE, SPATIAL, aircraft, policy, requirement_set,
)

from cns_planner.algorithms.corridor.v1 import CNSServiceCorridorV1  # noqa: E402
from cns_planner.domain.cns_inputs import normalize_device  # noqa: E402

SERVICE_KEY = "C:communication"
SERVICE_RADIUS_M = 4000.0


def _service_device():
    """显式声明 ``service_key`` 的 Communication 设备（surface-aware 几何）。"""

    return normalize_device({
        "device_id": "D1", "name": "D1", "subsystem": "C", "role": "existing",
        "radius_m": SERVICE_RADIUS_M, "mtbf_h": 1000,
        "service_key": SERVICE_KEY,
        "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
        "performance": {"max_latency_s": 0.2, "min_redundancy": 1},
        "coverage_geometry": {
            "model": "hemisphere", "source": "synthetic_fixture", "confirmed": True,
            "radius_by_surface": {
                "land": SERVICE_RADIUS_M, "sea": SERVICE_RADIUS_M,
                "coastal_uncertain": SERVICE_RADIUS_M,
            },
        },
        "service_model": {
            "model_family": "declared_performance", "version": "1",
            "technology": "dedicated_radio", "parameters": {"performance": {"max_latency_s": 0.2}},
            "source": "synthetic_fixture", "confirmed": True,
        },
    })


def _terrain(surface_class="sea"):
    return {"terrain": {"status": "passed", "cells": {
        str(cell.get("grid_id")): {
            "status": "passed", "surface_elevation_mean_m": 0.0,
            "surface_class": surface_class,
        } for cell in GRID["cells"]
    }}}


def _service_requirement_set():
    value = requirement_set("C")
    value["project_default"]["communication"]["services"] = {
        SERVICE_KEY: {
            "service_key": SERVICE_KEY, "required": True, "confirmed": True,
            "source": "synthetic_fixture", "coverage_requirement": 0.95,
            "geometry": {
                "model": "hemisphere", "omnidirectional": True,
                "horizontal_coverage_deg": 360.0,
            },
            "radius_by_surface": {"land": 40.0, "sea": 40.0, "coastal_uncertain": 40.0},
            "redundancy_by_surface": {"land": 2, "sea": 1, "coastal_uncertain": 2},
        },
    }
    return value


def _facilities(*, coordinate, confirmed_origin=True):
    return {"status": "passed", "items": [{
        "facility_id": "F1", "coordinate": list(coordinate), "status": "active",
        "vertical_profile": {
            "service_origin_egm2008_m": 100.0 if confirmed_origin else None,
            "confirmed": confirmed_origin,
        },
        "devices": [{"device_id": "D1", "subsystem": "C", "status": "active"}],
    }]}


def _evaluate(requirements, *, facilities_value, spatial=None, terrain=None, catalog=None):
    return CNSServiceCorridorV1().evaluate(
        [ROUTE], spatial or SPATIAL, GRID,
        terrain if terrain is not None else _terrain(),
        requirements, aircraft("C"), facilities_value,
        catalog or {"status": "passed", "items": [_service_device()]}, policy(),
    )


def _communication_entry(result):
    route = result["routes"][0]
    voxel = route["voxels"][0]
    return next(item for item in voxel["subsystems"] if item["subsystem"] == "C")


def test_uncovered_explicit_service_is_a_confirmed_deficit_not_unknown():
    #: 既有设施远在 7 km 之外：P7 几何门控明确判定"本采样点没有覆盖 provider"。
    result = _evaluate(
        _service_requirement_set(),
        facilities_value=_facilities(coordinate=[0.05, 0.05]),
    )
    entry = _communication_entry(result)

    assert entry["geometry"]["covered"] is False, "本用例必须建立在已确认无覆盖之上"
    services = entry.get("service_redundancy") or []
    assert [item["service_key"] for item in services] == [SERVICE_KEY]
    service = services[0]
    assert service["status"] == "confirmed_deficit"
    assert service["counting_basis"] == "distinct_site_id"
    assert service["distinct_site_count"] == 0
    #: sea ⇒ Communication 的 canonical 要求是 1 个独立物理站址。
    assert service["required_distinct_site_count"] == 1
    #: 关键：subsystem 的规划状态不再被"没有 provider 可汇总"降级成 unknown。
    assert entry["planning_status"] == "confirmed_deficit"


def test_covered_provider_never_synthesizes_an_uncovered_service_entry():
    """有真实覆盖时，服务证据必须来自 provider 汇总，绝不走"无覆盖"合成路径。"""

    result = _evaluate(
        _service_requirement_set(),
        facilities_value=_facilities(coordinate=[0.0, 0.0005]),
    )
    entry = _communication_entry(result)
    assert entry["geometry"]["covered"] is True
    services = entry.get("service_redundancy") or []
    assert [item["service_key"] for item in services] == [SERVICE_KEY]
    assert services[0]["counting_basis"] == "distinct_site_id"
    assert not any(
        "已确认本采样点没有任何覆盖 provider" in str(reason)
        for reason in services[0].get("reasons") or []
    ), "有覆盖的体元不得出现无覆盖合成证据"


def test_unknown_surface_never_upgrades_an_uncovered_service_to_confirmed():
    """覆盖确认不存在，但"要求多少站址"未知 ⇒ 必须保持 unknown（fail-closed）。"""

    result = _evaluate(
        _service_requirement_set(),
        facilities_value=_facilities(coordinate=[0.05, 0.05]),
        terrain={"terrain": {"status": "passed", "cells": {}}},
    )
    entry = _communication_entry(result)
    assert entry["geometry"]["covered"] is False
    services = entry.get("service_redundancy") or []
    assert services == [], "surface 未知时不得合成 confirmed_deficit 服务证据"
    assert entry["planning_status"] == "unknown"


def test_unknown_coverage_state_never_upgrades_to_confirmed():
    """垂直证据不足（覆盖状态未知）⇒ 仍然 unknown。"""

    spatial = {**SPATIAL, "route_altitude_profiles": {"R1": {
        **SPATIAL["route_altitude_profiles"]["R1"], "vertical_reference": "unknown",
    }}}
    result = _evaluate(
        _service_requirement_set(),
        facilities_value=_facilities(coordinate=[0.05, 0.05]),
        spatial=spatial,
    )
    entry = _communication_entry(result)
    assert entry["geometry"]["covered"] is None or entry["geometry"].get("coverage_status") == "unknown"
    services = entry.get("service_redundancy") or []
    assert services == []
    assert entry["planning_status"] == "unknown"


def test_legacy_requirement_without_services_is_unchanged():
    """legacy 项目（无显式 services）绝不产生 service 证据。"""

    result = _evaluate(
        requirement_set("C"), facilities_value=_facilities(coordinate=[0.05, 0.05]),
    )
    entry = _communication_entry(result)
    assert entry.get("service_redundancy") in (None, [])
    assert entry["planning_status"] == "confirmed_deficit", (
        "legacy 口径下 capability 的既有判定必须逐项不变"
    )
