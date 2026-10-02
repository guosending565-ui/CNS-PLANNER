"""Round 2.4 —— RID **机载参与能力** 与 **地面提供者能力** 的角色分离回归。

Round 2.3 的结论"机载 ``sensor_mode: active`` ≠ 需求 ``passive`` 因此不兼容"是
**角色错用**：``sensor_mode`` 描述的是**地面网络 RID 接收节点**只接收、不发射的
工作模式，而无人机在 RID 服务里的角色是 **cooperative target**（广播 / 网络上报），
它不是"被动 sensor"，也不该被要求声明接收端的工作模式。

本文件锁定修正后的三方语义：

1. **Required Service** 是否成立（需求侧声明）；
2. **Ground Provider** 是否能提供该服务（地面设备类型门禁，语义逐字段不变）；
3. **Aircraft Participation** 是否具备参与该服务的合作能力（机载参与谓词）。

并且锁定"缺机载参与声明 ⇒ unknown（evidence required），**绝不**自动降级为
does_not_meet_under_model"。
"""

from copy import deepcopy
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cns_planner.domain.cns_service_contract import (  # noqa: E402
    AIRBORNE_COOPERATIVE_SURVEILLANCE_FIELD, airborne_type_items,
    cooperative_surveillance_declarations,
)
from cns_planner.domain.cns_performance import (  # noqa: E402
    TYPE_GATE_FIELDS, airborne_type_items as performance_airborne_items,
    normalize_subsystem_contract, type_gate_items,
)
from cns_planner.safety.service_state import evaluate_required_performance  # noqa: E402


RID_REQUIRED = {
    "status": "passed", "required": True, "confirmed": True,
    "service_key": "S:rid_cooperative",
    "type": {
        "target_cooperation": "cooperative", "sensor_mode": "passive",
        "technology": "network_remote_id", "service_subtype": "cooperative_surveillance",
    },
    "performance": {},
}


def _aircraft(**type_overrides):
    """合成机载监视能力：与真实项目 AIRCRAFT-SYN-E2E-01 同形（synthetic ADS-B）。"""

    type_block = {
        "target_cooperation": "cooperative", "sensor_mode": "active", "technology": "adsb",
    }
    type_block.update(type_overrides)
    return {
        "status": "confirmed", "confirmed": True, "capabilities": ["synthetic_adsb"],
        "type": type_block, "performance": {},
    }


# --------------------------------------------------------------------------- 1
def test_sensor_mode_is_a_ground_receiver_property_and_never_reaches_the_aircraft_gate():
    """``sensor_mode`` 只描述地面接收节点，**绝不**进入机载参与谓词。"""

    items = airborne_type_items(RID_REQUIRED["type"], service_key="S:rid_cooperative")
    fields = [name for name, _ in items]
    assert "sensor_mode" not in fields, items
    assert "target_cooperation" not in fields, items
    #: 机载谓词只有一条：参与服务声明（由 ``technology`` 承载）。
    assert fields == [AIRBORNE_COOPERATIVE_SURVEILLANCE_FIELD], items
    #: 该条期望只含判定谓词 ``technology``；``service_subtype`` 是描述性字段，
    #: 绝不成为机载门禁（否则又造出一个假 unknown）。
    assert items[0][1] == {"technology": "network_remote_id"}, items
    #: 地面提供者谓词**逐字段不变**：仍然要求 sensor_mode。
    ground = [name for name, _ in type_gate_items(RID_REQUIRED["type"])]
    assert "sensor_mode" in ground
    assert "target_cooperation" in ground


def test_aircraft_sensor_mode_active_no_longer_produces_a_role_mismatch_reason():
    """旧结论（``sensor_mode`` 不匹配）不再出现；缺参与声明如实报"缺少声明"。"""

    satisfied, evidence = evaluate_required_performance(
        RID_REQUIRED, _aircraft(), require_capability=True, airborne=True,
    )
    assert satisfied is None
    assert "sensor_mode" not in str(evidence.get("reason")), evidence
    assert evidence["reason"] == "缺少机载参与能力声明"
    assert evidence["required_airborne_fields"] == [AIRBORNE_COOPERATIVE_SURVEILLANCE_FIELD]


def test_ground_provider_side_semantics_unchanged_including_sensor_mode():
    """地面侧判定**逐字段不变**：``sensor_mode`` 仍然是真门禁。"""

    wrong = {"status": "confirmed", "confirmed": True, "capabilities": ["x"], "type": {
        "target_cooperation": "cooperative", "sensor_mode": "active",
        "technology": "network_remote_id", "service_subtype": "cooperative_surveillance",
    }, "performance": {}}
    satisfied, evidence = evaluate_required_performance(RID_REQUIRED, wrong)
    assert satisfied is False
    assert evidence["reason"] == "sensor_mode 不匹配"

    right = deepcopy(wrong)
    right["type"]["sensor_mode"] = "passive"
    satisfied, evidence = evaluate_required_performance(RID_REQUIRED, right)
    assert satisfied is True, evidence


# --------------------------------------------------------------------------- 2
def test_aircraft_declaring_network_remote_id_participation_satisfies_the_service():
    """机载声明"支持参与 network_remote_id"后，机载参与能力成立。"""

    profile = _aircraft(**{
        AIRBORNE_COOPERATIVE_SURVEILLANCE_FIELD: [
            {"technology": "network_remote_id", "service_subtype": "cooperative_surveillance"},
        ],
    })
    satisfied, evidence = evaluate_required_performance(
        RID_REQUIRED, profile, require_capability=True, airborne=True,
    )
    assert satisfied is True, evidence
    #: 机载侧**仍然**是 ADS-B 的 sensor_mode=active（该事实不被改写）。
    assert profile["type"]["sensor_mode"] == "active"
    assert profile["type"]["technology"] == "adsb"


def test_aircraft_participation_declaration_only_adsb_is_a_real_incompatibility():
    """机载显式声明了参与服务、但没有任何一条匹配需求 ⇒ 真实不兼容（fail-closed）。"""

    profile = _aircraft(**{
        AIRBORNE_COOPERATIVE_SURVEILLANCE_FIELD: [{"technology": "adsb"}],
    })
    satisfied, evidence = evaluate_required_performance(
        RID_REQUIRED, profile, require_capability=True, airborne=True,
    )
    assert satisfied is False, evidence
    assert evidence["reason"] == "机载参与能力与 RequiredCNS 不兼容"
    assert evidence["declared_airborne_services"] == [{"technology": "adsb"}]


def test_evidence_source_is_disclosed_on_both_outcomes():
    """判定结果必须带证据来源标注（事实 / 工程假设 / 声明），供下游与报告披露。"""

    profile = _aircraft(**{
        AIRBORNE_COOPERATIVE_SURVEILLANCE_FIELD: [{"technology": "network_remote_id"}],
    })
    profile["airborne_evidence"] = {"source_type": "engineering_assumption"}
    satisfied, evidence = evaluate_required_performance(
        RID_REQUIRED, profile, require_capability=True, airborne=True,
    )
    assert satisfied is True, evidence
    assert evidence["airborne_evidence_source"] == "engineering_assumption"

    missing = _aircraft()
    missing["airborne_evidence"] = {"source_type": "unknown"}
    satisfied, evidence = evaluate_required_performance(
        RID_REQUIRED, missing, require_capability=True, airborne=True,
    )
    assert satisfied is None
    assert evidence["airborne_evidence_source"] == "unknown"


# --------------------------------------------------------------------------- 3
def test_legacy_aircraft_declaring_network_remote_id_technology_is_equivalent():
    """旧式机载用 ``technology = network_remote_id`` 表达同一事实 ⇒ 不引入新数据。"""

    declarations = cooperative_surveillance_declarations({
        "target_cooperation": "cooperative", "sensor_mode": "passive",
        "technology": "network_remote_id", "service_subtype": "cooperative_surveillance",
    })
    assert declarations == [{
        "technology": "network_remote_id", "service_subtype": "cooperative_surveillance",
    }]


def test_adsb_technology_is_never_silently_read_as_remote_id():
    """ADS-B 机载**绝不**被静默当作 RID 参与能力（否则就是伪造机载事实）。"""

    assert cooperative_surveillance_declarations({
        "target_cooperation": "cooperative", "sensor_mode": "active", "technology": "adsb",
    }) == []


def test_contract_keeps_the_participation_declaration_and_drops_absent_ones():
    """``normalize_subsystem_contract`` 只在显式给出时保留参与声明（形状兼容）。"""

    with_declaration = normalize_subsystem_contract("S", {
        "type": {
            "technology": "adsb",
            "cooperative_surveillance_services": [
                {"technology": "network_remote_id", "service_subtype": "cooperative_surveillance"},
            ],
        },
    }, field="test")
    assert with_declaration["type"][AIRBORNE_COOPERATIVE_SURVEILLANCE_FIELD] == [
        {"technology": "network_remote_id", "service_subtype": "cooperative_surveillance"},
    ]

    without = normalize_subsystem_contract("S", {
        "type": {"technology": "adsb"},
    }, field="test")
    assert AIRBORNE_COOPERATIVE_SURVEILLANCE_FIELD not in without["type"]


# --------------------------------------------------------------------------- 4
def test_communication_and_navigation_keep_their_own_airborne_type_fields():
    """只有合作监视（S）才有"参与能力"重映射；C/N 沿用机载自身字段。"""

    communication = {"service_type": "command_control", "technology": "dedicated_radio",
                     "network_scope": "dedicated", "interfaces": ["ip"]}
    items = dict(performance_airborne_items(communication, service_key="C:communication"))
    assert items == {
        "technology": "dedicated_radio", "network_scope": "dedicated", "interfaces": ["ip"],
    }
    #: 需求侧的 service_type 是用途描述，Round 2.3 已裁定它不是门禁 ⇒ 两侧都不判定。
    assert "service_type" not in items
    assert "service_type" not in TYPE_GATE_FIELDS
