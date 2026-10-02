"""Round 2.3 —— provider type compatibility 契约回归。

**业务裁定（本轮）**：参与「提供者类型资格」判定的 ``type`` 字段 = 提供者**必须能够
声明的物理/技术事实**，即 `PROVIDER_TYPE_COMPATIBILITY_FIELDS`
（``technology`` / ``network_scope`` / ``interfaces`` / ``target_cooperation`` /
``sensor_mode`` / ``service_subtype``）。

``service_type`` 是**需求侧的用途/任务描述**：

* 它在前端 STEP4 是自由的「服务类型」文本输入（如 ``command_control``）；
* 设备导入链（``catalogs/device_catalog.py``）与 ``normalize_device()`` **从不写入**
  ``device.type.service_type``，因此它永远不可能被设备声明；
* 设备的服务身份由 canonical ``service_key`` 承载（``C:communication`` …）；
* 继续把它当资格门禁 = **同一事实被要求两次**，只会制造假 ``unknown``
  （Round 2.3 真实项目取证：``缺少提供者类型字段 service_type``）。

同时本文件锁定**不得放宽**的部分：``network_scope`` 未声明仍然 ``unknown``（fail-closed），
``technology`` / ``interfaces`` 仍参与判定，``service_key`` 仍是唯一权威服务身份。
"""

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_cns_service_capability import aircraft, device, requirement  # noqa: E402

from cns_planner.algorithms.service_capability.v1 import (  # noqa: E402
    PROVIDER_TYPE_COMPATIBILITY_FIELDS, _provider_type_evaluation,
)
from cns_planner.domain.cns_inputs import normalize_device  # noqa: E402
from cns_planner.domain.cns_performance import (  # noqa: E402
    TYPE_GATE_FIELDS, descriptive_type_fields, type_gate_items,
)
from cns_planner.safety.service_state import (  # noqa: E402
    evaluate_required_performance, evaluate_service_state,
)

COMMUNICATION_DEVICE_TYPE = {"technology": "dedicated_radio", "interfaces": ["ip"]}
RID_DEVICE_TYPE = {
    "technology": "network_remote_id", "target_cooperation": "cooperative",
    "sensor_mode": "passive", "service_subtype": "cooperative_surveillance",
}


def _provider(service_key, **extra):
    return {
        "facility_id": "F1", "device_id": "D1", "slant_distance_m": 1000.0,
        "service_key": service_key, "service_surface_dependent": True,
        "distinct_site_id": "site:F1", **extra,
    }


def test_contract_field_list_is_the_documented_set():
    assert PROVIDER_TYPE_COMPATIBILITY_FIELDS == (
        "technology", "network_scope", "interfaces",
        "target_cooperation", "sensor_mode", "service_subtype",
    )
    assert "service_type" not in PROVIDER_TYPE_COMPATIBILITY_FIELDS


def test_device_normalization_never_invents_service_type_on_the_provider_side():
    """设备目录**没有** ``service_type`` 这个概念：归一化后仍为 null。"""

    normalized = normalize_device({
        "device_id": "COMM-BASELINE-4KM", "name": "COMM", "subsystem": "C",
        "role": "candidate", "radius_m": 4000.0, "mtbf_h": 1000,
        "service_key": "C:communication",
        "type": dict(COMMUNICATION_DEVICE_TYPE),
    })
    assert normalized["type"]["service_type"] is None
    assert normalized["type"]["network_scope"] == "unknown"
    assert normalized["service_key"] == "C:communication"


def test_required_service_type_never_asks_the_provider_to_duplicate_the_service_identity():
    """需求声明 ``service_type`` 而设备侧不存在该键 ⇒ 不得判 unknown（同一事实只要求一次）。"""

    required = requirement("C")
    required["type"] = {
        "service_type": "command_control", "technology": "dedicated_radio",
        "network_scope": "dedicated", "interfaces": ["ip"],
    }
    provider_device = device("C")
    #: 设备侧未声明 network_scope → 只有该字段能阻止资格判定。
    provider_device["type"]["network_scope"] = "dedicated"
    evaluation = _provider_type_evaluation(
        required, _provider("C:communication"), provider_device,
    )
    assert evaluation["status"] == "meets_under_model", evaluation["reasons"]
    assert evaluation["evidence"][0]["evaluated_type_fields"] == [
        "interfaces", "network_scope", "technology",
    ]
    assert evaluation["evidence"][0]["descriptive_type_fields"] == ["service_type"]
    assert evaluation["evidence"][0]["canonical_service_identity"] == "C:communication"


def test_provider_side_service_type_mismatch_is_not_a_gate():
    """即便设备**声明了**不同的 ``service_type``，也不参与资格判定（它不是能力事实）。"""

    required = requirement("C")
    required["type"] = {
        "service_type": "command_control", "technology": "dedicated_radio",
        "network_scope": "dedicated", "interfaces": ["ip"],
    }
    provider_device = device("C", type_data={
        **COMMUNICATION_DEVICE_TYPE, "network_scope": "dedicated",
        "service_type": "video_backhaul",
    })
    evaluation = _provider_type_evaluation(
        required, _provider("C:communication"), provider_device,
    )
    assert evaluation["status"] == "meets_under_model", evaluation["reasons"]


def test_undeclared_network_scope_still_fails_closed():
    """**不放宽**：需求要求 ``dedicated`` 而设备未声明网络范围 ⇒ 仍然 unknown。"""

    required = requirement("C")
    required["type"] = {
        "service_type": "command_control", "technology": "dedicated_radio",
        "network_scope": "dedicated", "interfaces": ["ip"],
    }
    evaluation = _provider_type_evaluation(
        required, _provider("C:communication"), device("C"),
    )
    assert evaluation["status"] == "unknown"
    assert evaluation["reasons"] == ["缺少提供者类型字段 network_scope"]


def test_declared_network_scope_mismatch_still_fails():
    """**不放宽**：设备声明了网络范围但与要求不一致 ⇒ does_not_meet_under_model。"""

    required = requirement("C")
    required["type"] = {
        "technology": "dedicated_radio", "network_scope": "dedicated", "interfaces": ["ip"],
    }
    provider_device = device("C", type_data={
        **COMMUNICATION_DEVICE_TYPE, "network_scope": "public",
    })
    evaluation = _provider_type_evaluation(
        required, _provider("C:communication"), provider_device,
    )
    assert evaluation["status"] == "does_not_meet_under_model"
    assert evaluation["reasons"] == ["provider network_scope 不匹配"]


def test_technology_and_interfaces_gates_are_unchanged():
    """**不放宽**：技术不匹配判不满足；接口缺失仍判 unknown；接口子集仍判不满足。"""

    required = requirement("C")
    required["type"] = {"technology": "dedicated_radio", "interfaces": ["ip"]}

    mismatched = device("C", type_data={"technology": "satellite", "interfaces": ["ip"]})
    assert _provider_type_evaluation(
        required, _provider("C:communication"), mismatched,
    )["status"] == "does_not_meet_under_model"

    no_interfaces = device("C", type_data={"technology": "dedicated_radio"})
    evaluation = _provider_type_evaluation(
        required, _provider("C:communication"), no_interfaces,
    )
    assert evaluation["status"] == "unknown"
    assert evaluation["reasons"] == ["缺少提供者类型字段 interfaces"]

    partial = device("C", type_data={"technology": "dedicated_radio", "interfaces": ["serial"]})
    evaluation = _provider_type_evaluation(
        required, _provider("C:communication"), partial,
    )
    assert evaluation["status"] == "does_not_meet_under_model"
    assert evaluation["reasons"] == ["provider interfaces 不满足"]


def test_rid_type_gate_is_unaffected_because_services_has_no_service_type():
    """S（RID）契约里没有 ``service_type``：四字段仍然全部参与判定。"""

    required = {"required": True, "status": "passed", "type": dict(RID_DEVICE_TYPE)}
    provider_device = device("S", type_data=dict(RID_DEVICE_TYPE))
    evaluation = _provider_type_evaluation(
        required, _provider("S:rid_cooperative"), provider_device,
    )
    assert evaluation["status"] == "meets_under_model", evaluation["reasons"]
    assert evaluation["evidence"][0]["evaluated_type_fields"] == [
        "sensor_mode", "service_subtype", "target_cooperation", "technology",
    ]
    assert evaluation["evidence"][0]["descriptive_type_fields"] == []

    wrong_mode = device("S", type_data={**RID_DEVICE_TYPE, "sensor_mode": "active"})
    assert _provider_type_evaluation(
        required, _provider("S:rid_cooperative"), wrong_mode,
    )["status"] == "does_not_meet_under_model"


def test_evaluate_provider_reaches_the_service_model_layer_after_the_type_gate():
    """类型门禁通过后，判定继续走到 ServiceModelSpec / 性能层（不再被 service_type 截断）。"""

    from cns_planner.algorithms.service_capability.v1 import (  # noqa: E402
        _evaluate_provider, _without_redundancy,
    )

    required = requirement("C")
    required["type"] = {
        "service_type": "command_control", "technology": "dedicated_radio",
        "network_scope": "dedicated", "interfaces": ["ip"],
    }
    provider_device = device("C", type_data={
        **COMMUNICATION_DEVICE_TYPE, "network_scope": "dedicated",
    })
    #: 与 P8 真实调用完全同口径：冗余不与逐 provider 性能一起判定。
    evaluation = _evaluate_provider(
        "C", required, aircraft("C"), _provider("C:communication"), provider_device,
        _without_redundancy(required),
    )
    assert evaluation["status"] == "meets_under_model", evaluation["reasons"]
    assert evaluation["model_family"] == "declared_performance"


# ---------------------------------------------------------------------------
# 第二层：性能/类型匹配器（safety/service_state.py::_satisfies）必须消费**同一份**
# 类型门禁契约，否则同一个假 unknown 会在 `required_performance` 证据里重现。
# ---------------------------------------------------------------------------


def test_type_gate_items_contract_is_shared_and_descriptive_only_for_service_type():
    assert TYPE_GATE_FIELDS == PROVIDER_TYPE_COMPATIBILITY_FIELDS

    required_type = {
        "service_type": "command_control", "technology": "dedicated_radio",
        "network_scope": "dedicated", "interfaces": ["ip"],
    }
    assert type_gate_items(required_type) == [
        ("technology", "dedicated_radio"),
        ("network_scope", "dedicated"),
        ("interfaces", ["ip"]),
    ]
    assert descriptive_type_fields(required_type) == ["service_type"]
    #: 空值/unknown 一律跳过（与修复前的循环语义一致）。
    assert type_gate_items({"technology": "unknown", "network_scope": None, "interfaces": []}) == []
    assert type_gate_items(None) == []
    assert descriptive_type_fields(None) == []


def test_required_performance_does_not_require_the_aircraft_to_repeat_service_type():
    """机载能力侧：需求声明 ``service_type`` 不得让机载能力判"缺少类型字段"。"""

    required = {
        "type": {"service_type": "command_control", "technology": "dedicated_radio", "interfaces": ["ip"]},
        "performance": {"max_latency_s": 1.0},
    }
    actual = {
        "confirmed": True, "status": "confirmed", "capabilities": ["radio"],
        "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
        "performance": {"max_latency_s": 0.5},
    }
    satisfied, evidence = evaluate_required_performance(required, actual, require_capability=True)
    assert satisfied is True, evidence
    assert evidence["reason"] == "类型与性能满足"


def test_required_performance_does_not_require_the_provider_to_repeat_service_type():
    """地面提供者侧：同一份契约在 ``_declared_actual`` 上必须给出同一结论。"""

    required = {
        "type": {"service_type": "command_control", "technology": "dedicated_radio", "interfaces": ["ip"]},
        "performance": {"max_latency_s": 1.0},
    }
    actual = {
        "confirmed": True, "status": "confirmed", "capabilities": ["service_model"],
        "type": {"service_type": None, "technology": "dedicated_radio", "interfaces": ["ip"]},
        "performance": {"max_latency_s": 0.2},
    }
    satisfied, evidence = evaluate_required_performance(required, actual)
    assert satisfied is True, evidence


def test_required_performance_still_gates_real_type_facts():
    """**不放宽**：``network_scope`` 未声明 / 不匹配仍分别判 unknown 与不满足。"""

    required = {"type": {"network_scope": "dedicated", "technology": "dedicated_radio"}}
    missing, evidence = evaluate_required_performance(
        required, {"type": {"technology": "dedicated_radio"}},
    )
    assert missing is None
    assert evidence["reason"] == "缺少类型字段 network_scope"

    mismatched, evidence = evaluate_required_performance(
        required, {"type": {"technology": "dedicated_radio", "network_scope": "public"}},
    )
    assert mismatched is False
    assert evidence["reason"] == "network_scope 不匹配"

    wrong_technology, evidence = evaluate_required_performance(
        required, {"type": {"technology": "satellite", "network_scope": "dedicated"}},
    )
    assert wrong_technology is False
    assert evidence["reason"] == "technology 不匹配"


def test_service_state_aircraft_path_uses_the_same_contract():
    """``evaluate_service_state`` 的机载能力证据同样不得要求重复声明 ``service_type``。"""

    required = {
        "required": True, "status": "passed",
        "type": {"service_type": "command_control", "technology": "dedicated_radio", "interfaces": ["ip"]},
        "performance": {"max_latency_s": 1.0},
    }
    capability = {
        "confirmed": True, "status": "confirmed", "capabilities": ["radio"],
        "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
        "performance": {"max_latency_s": 0.5}, "fallbacks": [],
    }
    result = evaluate_service_state(required, capability, None, None)
    capability_evidence = next(
        item for item in result["evidence"] if item["kind"] == "aircraft_capability"
    )
    assert capability_evidence["satisfied"] is True, capability_evidence
    assert "缺少类型字段" not in str(capability_evidence.get("reason"))
