"""Round 2.3 —— 设备服务身份贯穿 P8 → P14 → P15 的端到端回归。

锁定本轮修复的真实语义：当**设备已经用 canonical ``service_key`` 声明了服务身份**
（``C:communication`` / ``S:rid_cooperative``，带枚举校验、已确认），而需求侧额外声明了
``type.service_type``（前端自由文本"服务类型"，设备目录**没有**这个字段）时：

* P8 的 ``provider_type_compatibility`` 门禁**不得**因为设备缺少 ``service_type`` 判
  ``unknown``（那是同一事实被要求两次）；
* P8 的 ``required_performance`` 层（``safety/service_state.py::_satisfies``）同样不得；
* 于是 P14 的该体元拿到 ``satisfied`` 的服务证据，P15 不再把已确认事实降级成 unknown。

反向边界同时锁定：``technology`` / ``network_scope`` / ``interfaces`` 仍然是真门禁。
"""

from copy import deepcopy
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_cns_corridor import (  # noqa: E402
    GRID, ROUTE, SPATIAL, aircraft, policy, requirement_set,
)
from test_p14_uncovered_service_evidence import (  # noqa: E402
    SERVICE_KEY, SERVICE_RADIUS_M, _service_device, _terrain,
)

from cns_planner.algorithms.corridor.v1 import CNSServiceCorridorV1  # noqa: E402
from cns_planner.algorithms.corridor_gap.v1 import CNSCorridorGapAnalyzerV1  # noqa: E402
from cns_planner.domain.cns_inputs import normalize_device  # noqa: E402

#: 真实项目 R0005 的口径：需求侧**四字段全齐**，设备侧只有 technology / interfaces。
REQUIRED_TYPE_WITH_SERVICE_TYPE = {
    "service_type": "command_control", "technology": "dedicated_radio",
    "network_scope": "dedicated", "interfaces": ["ip"],
}


def _aircraft_with_network_scope():
    """机载通信能力必须与需求同类声明（``network_scope`` 是**真门禁**）。

    机载能力同样**不声明** ``service_type``——这正是本轮修复要覆盖的形态。
    """

    profile = aircraft("C")
    profile["communication"] = {
        **profile["communication"],
        "type": {
            "technology": "dedicated_radio", "network_scope": "dedicated",
            "interfaces": ["ip"],
        },
    }
    return profile


def _facilities(*, coordinate, confirmed_origin=True, device=None):
    """既有 CNS 设施 + 其**内联设备**。

    真实项目（``_runtime_projects/zhoushan_screenshot_01_20261001_1647``）的既有设施内联设备
    携带 ``engineering_planning_baseline_from_project_device_catalog`` 的**已确认**
    ``service_model``，因此这里必须同样提供，否则测的就不是本轮的契约问题。
    """

    installed = device if device is not None else _service_device()
    return {"status": "passed", "items": [{
        "facility_id": "F1", "coordinate": list(coordinate), "status": "active",
        "vertical_profile": {
            "service_origin_egm2008_m": 100.0 if confirmed_origin else None,
            "confirmed": confirmed_origin,
        },
        "devices": [{
            "device_id": installed["device_id"], "subsystem": installed["subsystem"],
            "status": "active", "service_key": installed.get("service_key"),
            "type": deepcopy(installed.get("type") or {}),
            "service_model": deepcopy(installed.get("service_model") or {}),
        }],
    }]}


def _requirement(*, required_type, device_network_scope="dedicated", device_type_extra=None):
    value = requirement_set("C")
    communication = value["project_default"]["communication"]
    communication["type"] = dict(required_type)
    communication["services"] = {
        SERVICE_KEY: {
            "service_key": SERVICE_KEY, "required": True, "confirmed": True,
            "source": "synthetic_fixture", "coverage_requirement": 0.95,
            "geometry": {
                "model": "hemisphere", "omnidirectional": True,
                "horizontal_coverage_deg": 360.0,
            },
            "radius_by_surface": {
                "land": SERVICE_RADIUS_M, "sea": SERVICE_RADIUS_M,
                "coastal_uncertain": SERVICE_RADIUS_M,
            },
            "redundancy_by_surface": {"land": 2, "sea": 1, "coastal_uncertain": 2},
        },
    }
    device = _service_device()
    device["type"] = {
        "service_type": None, "technology": "dedicated_radio",
        "network_scope": device_network_scope, "interfaces": ["ip"],
        **(device_type_extra or {}),
    }
    return value, device


def _p14(required, device, *, facilities=None, aircraft_profile=None):
    return CNSServiceCorridorV1().evaluate(
        [ROUTE], SPATIAL, GRID, _terrain(),
        required, aircraft_profile or _aircraft_with_network_scope(),
        facilities if facilities is not None else _facilities(coordinate=[0.0, 0.0], device=device),
        {"status": "passed", "items": [device]}, policy(),
    )


def _communication_entry(result):
    route = result["routes"][0]
    voxel = route["voxels"][0]
    return next(item for item in voxel["subsystems"] if item["subsystem"] == "C")


def test_canonical_service_identity_is_enough_and_service_type_is_not_demanded_twice():
    required, device = _requirement(required_type=REQUIRED_TYPE_WITH_SERVICE_TYPE)
    result = _p14(required, device)
    entry = _communication_entry(result)

    assert entry["geometry"]["covered"] is True, "本用例必须建立在真实覆盖之上"
    #: 关键：设备没有声明 ``service_type``，但类型门禁不再产生假 unknown。
    assert entry["p8_status"] == "meets_under_model", entry["reasons"]
    assert not [
        item for item in entry["provider_evaluations"] if item.get("status") == "unknown"
    ], "不得因为设备缺少 service_type 而出现 unknown provider 评估"

    service = next(
        item for item in entry["service_redundancy"] if item["service_key"] == SERVICE_KEY
    )
    assert service["status"] == "satisfied"
    assert service["counting_basis"] == "distinct_site_id"
    assert service["distinct_site_count"] == 1
    assert service["required_distinct_site_count"] == 1
    assert service["qualified_provider_count"] == 1
    assert entry["planning_status"] == "satisfied"


def test_p15_no_longer_downgrades_a_confirmed_covered_service_to_unknown():
    required, device = _requirement(required_type=REQUIRED_TYPE_WITH_SERVICE_TYPE)
    corridor = _p14(required, device)
    gap = CNSCorridorGapAnalyzerV1({}).evaluate(
        corridor, required, {"C": {"objectives": {}}},
    )
    voxel = gap["routes"][0]["voxels"][0]
    entry = next(item for item in voxel["subsystems"] if item["subsystem"] == "C")

    assert entry["service_status"] == "satisfied", entry["reasons"]
    assert entry["combined_status"] == "satisfied", entry["causes"]
    assert "unknown_service_evidence" not in (entry["causes"] or [])


def test_network_scope_remains_a_real_gate_for_the_provider():
    """**不放宽**：设备未声明 ``network_scope`` ⇒ 覆盖存在但资格证据不足。"""

    required, device = _requirement(
        required_type=REQUIRED_TYPE_WITH_SERVICE_TYPE, device_network_scope="unknown",
    )
    result = _p14(required, device)
    entry = _communication_entry(result)

    assert entry["geometry"]["covered"] is True
    assert entry["p8_status"] == "unknown"
    type_gate = [
        item for item in entry["provider_evaluations"]
        if item.get("stage") == "provider_type_compatibility"
    ]
    assert [item["status"] for item in type_gate] == ["unknown"]
    assert type_gate[0]["reasons"] == ["缺少提供者类型字段 network_scope"]
    assert entry["planning_status"] == "unknown"


def test_technology_mismatch_still_fails_closed():
    """**不放宽**：设备技术不匹配 ⇒ P8 ``does_not_meet_under_model``（不是 unknown）。"""

    required, device = _requirement(
        required_type=REQUIRED_TYPE_WITH_SERVICE_TYPE,
        device_type_extra={"technology": "satellite"},
    )
    result = _p14(required, device)
    entry = _communication_entry(result)

    assert entry["p8_status"] == "does_not_meet_under_model", entry["reasons"]
    type_gate = [
        item for item in entry["provider_evaluations"]
        if item.get("stage") == "provider_type_compatibility"
    ]
    assert [item["status"] for item in type_gate] == ["does_not_meet_under_model"]
    assert type_gate[0]["reasons"] == ["provider technology 不匹配"]
    #: 真实*不兼容*时 P14 的规划状态仍然是 unknown（既有保守语义，本轮未改）。
    assert entry["planning_status"] == "unknown"


def test_legacy_requirement_without_service_type_behaviour_is_unchanged():
    """需求侧不声明 ``service_type`` 时（既有全部回归的口径）结论逐项不变。"""

    required, device = _requirement(required_type={
        "technology": "dedicated_radio", "network_scope": "dedicated", "interfaces": ["ip"],
    })
    result = _p14(required, device)
    entry = _communication_entry(result)

    assert entry["p8_status"] == "meets_under_model", entry["reasons"]
    assert entry["planning_status"] == "satisfied"
    service = next(
        item for item in entry["service_redundancy"] if item["service_key"] == SERVICE_KEY
    )
    assert service["status"] == "satisfied"


def test_device_side_service_type_value_cannot_override_canonical_identity():
    """设备侧即便写了别的 ``service_type``，也不改变 canonical ``service_key`` 身份。"""

    required, device = _requirement(required_type=REQUIRED_TYPE_WITH_SERVICE_TYPE)
    device["type"]["service_type"] = "video_backhaul"
    result = _p14(required, device)
    entry = _communication_entry(result)

    assert entry["p8_status"] == "meets_under_model", entry["reasons"]
    service = next(
        item for item in entry["service_redundancy"] if item["service_key"] == SERVICE_KEY
    )
    assert service["status"] == "satisfied"
    assert device["service_key"] == SERVICE_KEY
