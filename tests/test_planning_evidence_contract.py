"""Round 2.4 —— 工程证据 / 规划假设：事实与假设分离、可保存、必须披露。

产品缺口（本轮要关闭的）：缺一条工程证据时只能让开发者改数据 / JSON。
本文件锁定：

* **来源类型三分**且 fail-closed（``unknown`` 不携带取值、不参与判定；
  ``engineering_assumption`` 必须带 statement / source / reason / disclosure /
  confirmed_by_user）；
* 枚举校验（非法取值直接拒绝，绝不放任自由文本）；
* 叠加是**只读消费**：不改写机载档案本身、不写 device catalog；
* 事实与假设**严格分开**：假设的权威效应是"允许参与但必须披露"，
  且叠加结果带 ``airborne_evidence`` 来源标注；
* 持久化往返后**仍然是 assumption**，绝不升级成 fact；
* 事实/假设真的改变 P8 判定（不是只写不用）。
"""

from copy import deepcopy
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from cns_planner.domain.cns_service_contract import (  # noqa: E402
    AIRBORNE_COOPERATIVE_SURVEILLANCE_FIELD,
)
from cns_planner.domain.planning_evidence import (  # noqa: E402
    EVIDENCE_AUTHORITY_EFFECT, EVIDENCE_SOURCE_TYPES, PLANNING_EVIDENCE_FIELDS,
    active_evidence_items, apply_aircraft_evidence, empty_planning_evidence,
    evidence_disclosure_lines, normalize_engineering_evidence,
    normalize_planning_evidence_registry,
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


def aircraft_profile():
    return {
        "aircraft_id": "AIRCRAFT-SYN-E2E-01",
        "surveillance": {
            "status": "confirmed", "confirmed": True, "capabilities": ["synthetic_adsb"],
            "type": {"target_cooperation": "cooperative", "sensor_mode": "active",
                     "technology": "adsb"},
            "performance": {},
        },
        "communication": {
            "status": "confirmed", "confirmed": True, "capabilities": ["radio"],
            "type": {"technology": "dedicated_radio", "network_scope": "dedicated",
                     "interfaces": ["validation_radio_v1"]},
            "performance": {},
        },
    }


def assumption(field, value, **overrides):
    item = {
        "evidence_id": "PEV-TEST-001",
        "scope": "aircraft",
        "target_id": "AIRCRAFT-SYN-E2E-01",
        "field": field,
        "value": value,
        "source_type": "engineering_assumption",
        "source": "本测试场景的工程规划输入",
        "statement": "本测试场景假设该机载平台具备网络远程识别参与能力。",
        "reason": "为使 Step4→Step5 主链在本测试场景中可评估",
        "report_disclosure": "本项为工程规划假设，不代表厂家既有设备事实。",
        "declared_by": "user",
        "confirmed": True,
        "confirmed_by_user": True,
    }
    item.update(overrides)
    return item


# --------------------------------------------------------------------------- 1
def test_source_types_are_closed_enum_with_external_reference():
    """Round 2.5：来源类型是**四类闭枚举**（新增 external_reference）。

    ``external_reference`` 的语义是"外部研究 / 公开资料参考值"：它允许参与判定，
    但权威效应是 ``allowed_as_external_reference``，报告必须把它与"法规/厂家事实"
    逐字区分（例如 RTK 恢复 9–13 s 只作外部参考、不作硬门）。
    """

    assert EVIDENCE_SOURCE_TYPES == (
        "confirmed_source_fact", "external_reference",
        "engineering_assumption", "unknown",
    )
    assert EVIDENCE_AUTHORITY_EFFECT["external_reference"] == "allowed_as_external_reference"
    with pytest.raises(ValueError):
        normalize_engineering_evidence(assumption(
            "remote_id_participation", ["network_remote_id"], source_type="manufacturer_claim",
        ))


def test_external_reference_requires_provenance_and_disclosure():
    item = normalize_engineering_evidence(assumption(
        "navigation_degradation_time_s", 13.0,
        scope="project", target_id=None,
        source_type="external_reference",
        source="公开研究资料（RTK 恢复时长区间）",
        external_reference="公开资料 REF-001：RTK 恢复约 9–13 s",
    ))
    assert item["source_type"] == "external_reference"
    assert item["authority_effect"] == "allowed_as_external_reference"
    assert item["external_reference"].startswith("公开资料 REF-001")

    with pytest.raises(ValueError, match="external_reference 缺少必填字段"):
        normalize_engineering_evidence(assumption(
            "navigation_degradation_time_s", 13.0,
            scope="project", target_id=None,
            source_type="external_reference", source=None,
            external_reference="REF-001",
        ))
    with pytest.raises(ValueError, match="必须声明 external_reference"):
        normalize_engineering_evidence(assumption(
            "navigation_degradation_time_s", 13.0,
            scope="project", target_id=None,
            source_type="external_reference",
        ))


def test_engineering_assumption_requires_full_disclosure_bundle():
    with pytest.raises(ValueError, match="engineering_assumption 缺少必填字段"):
        normalize_engineering_evidence(assumption(
            "remote_id_participation", ["network_remote_id"], statement=None,
        ))
    with pytest.raises(ValueError, match="confirmed_by_user"):
        normalize_engineering_evidence(assumption(
            "remote_id_participation", ["network_remote_id"], confirmed_by_user=False,
        ))
    with pytest.raises(ValueError, match="declared_by"):
        normalize_engineering_evidence(assumption(
            "remote_id_participation", ["network_remote_id"], declared_by=None,
        ))


def test_confirmed_source_fact_requires_a_source_and_explicit_confirmation():
    with pytest.raises(ValueError, match="必须声明 source"):
        normalize_engineering_evidence(assumption(
            "remote_id_participation", ["network_remote_id"],
            source_type="confirmed_source_fact", source=None,
        ))
    item = normalize_engineering_evidence(assumption(
        "remote_id_participation", ["network_remote_id"],
        source_type="confirmed_source_fact", source="正式验收纪要 A-1",
    ))
    assert item["source_type"] == "confirmed_source_fact"
    assert item["confirmation_status"] == "confirmed"


def test_unknown_carries_no_value_and_never_participates():
    item = normalize_engineering_evidence({
        "evidence_id": "PEV-UNKNOWN", "scope": "aircraft",
        "target_id": "AIRCRAFT-SYN-E2E-01", "field": "remote_id_participation",
        "source_type": "unknown",
    })
    assert item["value"] is None
    assert item["authority_effect"] == "not_participating"
    registry = {"schema_version": item["schema_version"], "items": [item]}
    assert active_evidence_items(registry) == []

    with pytest.raises(ValueError, match="不得携带 value"):
        normalize_engineering_evidence({
            "evidence_id": "PEV-UNKNOWN-2", "scope": "aircraft",
            "target_id": "AIRCRAFT-SYN-E2E-01", "field": "remote_id_participation",
            "source_type": "unknown", "value": ["network_remote_id"],
        })


def test_values_are_enum_validated_per_field():
    spec = PLANNING_EVIDENCE_FIELDS["remote_id_participation"]
    before = normalize_engineering_evidence(
        assumption("remote_id_participation", ["network_remote_id"]),
    )
    assert before["value"] == ["network_remote_id"]
    with pytest.raises(ValueError, match="value 无效"):
        normalize_engineering_evidence(
            assumption("remote_id_participation", ["carrier_pigeon"]),
        )
    assert "network_remote_id" in spec["allowed"]
    with pytest.raises(ValueError, match="必须是"):
        normalize_engineering_evidence(
            assumption("communication_network_scope", "secret_network"),
        )


def test_unknown_field_is_refused():
    with pytest.raises(ValueError, match="不在允许的人工工程证据清单内"):
        normalize_engineering_evidence(assumption("device_firmware_version", "1.0"))


def test_aircraft_scope_requires_a_known_target_id():
    with pytest.raises(ValueError, match="target_id"):
        normalize_engineering_evidence({
            "evidence_id": "PEV-NO-TARGET", "scope": "aircraft",
            "field": "remote_id_participation", "source_type": "unknown",
        })
    with pytest.raises(ValueError, match="scope"):
        normalize_engineering_evidence(assumption(
            "remote_id_participation", ["network_remote_id"], scope="artifact",
        ))


# --------------------------------------------------------------------------- 2
def test_apply_is_read_only_and_declares_the_source():
    profile = aircraft_profile()
    snapshot = deepcopy(profile)
    registry = normalize_planning_evidence_registry({
        "items": [assumption("remote_id_participation", ["network_remote_id"])],
    })
    applied = apply_aircraft_evidence(profile, registry)

    assert profile == snapshot, "工程证据绝不修改机载档案本身"
    assert applied is not profile
    assert applied["surveillance"]["type"][AIRBORNE_COOPERATIVE_SURVEILLANCE_FIELD] == [{
        "technology": "network_remote_id", "service_subtype": "cooperative_surveillance",
    }]
    #: 机载的客观事实（ADS-B / active）不被改写。
    assert applied["surveillance"]["type"]["technology"] == "adsb"
    assert applied["surveillance"]["type"]["sensor_mode"] == "active"
    evidence = applied["airborne_evidence"]
    assert evidence["source_type"] == "engineering_assumption"
    assert evidence["planning_input_only"] is True
    assert evidence["semantics"] == (
        "engineering_planning_input_disclosed_not_manufacturer_fact"
    )
    assert evidence["fields"]["remote_id_participation"]["source_type"] == (
        "engineering_assumption"
    )


def test_no_evidence_returns_a_byte_equivalent_copy():
    profile = aircraft_profile()
    assert apply_aircraft_evidence(profile, empty_planning_evidence()) == profile
    assert "airborne_evidence" not in apply_aircraft_evidence(profile, None)


def test_interfaces_evidence_appends_without_removing_existing_declarations():
    registry = normalize_planning_evidence_registry({
        "items": [assumption("communication_airborne_interfaces", ["ip"])],
    })
    applied = apply_aircraft_evidence(aircraft_profile(), registry)
    assert applied["communication"]["type"]["interfaces"] == ["validation_radio_v1", "ip"]


def test_network_scope_evidence_sets_the_declared_field():
    registry = normalize_planning_evidence_registry({
        "items": [assumption("communication_network_scope", "dedicated")],
    })
    applied = apply_aircraft_evidence(aircraft_profile(), registry)
    assert applied["communication"]["type"]["network_scope"] == "dedicated"


# --------------------------------------------------------------------------- 3
def test_evidence_actually_changes_the_airborne_judgement():
    """证据不是"只写不用"：叠加前 unknown，叠加后成立。"""

    profile = aircraft_profile()
    satisfied, evidence = evaluate_required_performance(
        RID_REQUIRED, profile["surveillance"], require_capability=True, airborne=True,
    )
    assert satisfied is None, evidence

    registry = normalize_planning_evidence_registry({
        "items": [assumption("remote_id_participation", ["network_remote_id"])],
    })
    applied = apply_aircraft_evidence(profile, registry)
    satisfied, evidence = evaluate_required_performance(
        RID_REQUIRED, applied["surveillance"], require_capability=True, airborne=True,
    )
    assert satisfied is True, evidence
    assert evidence["airborne_evidence_source"] == "engineering_assumption"


def test_adsb_only_evidence_does_not_smuggle_rid_capability():
    """假设只能是"用户显式声明的取值"：写 adsb 就仍然是 ADS-B，不会被当成 RID。"""

    registry = normalize_planning_evidence_registry({
        "items": [assumption("remote_id_participation", ["adsb"])],
    })
    applied = apply_aircraft_evidence(aircraft_profile(), registry)
    satisfied, evidence = evaluate_required_performance(
        RID_REQUIRED, applied["surveillance"], require_capability=True, airborne=True,
    )
    assert satisfied is False, evidence
    assert evidence["reason"] == "机载参与能力与 RequiredCNS 不兼容"


# --------------------------------------------------------------------------- 4
def test_registry_round_trip_keeps_assumption_as_assumption():
    """save → reopen（JSON 往返）后**仍然是 assumption**，绝不升级成事实。"""

    import json

    registry = normalize_planning_evidence_registry({
        "items": [assumption("remote_id_participation", ["network_remote_id"])],
    })
    reopened = normalize_planning_evidence_registry(json.loads(json.dumps(registry)))
    item = reopened["items"][0]
    assert item["source_type"] == "engineering_assumption"
    assert item["confirmed"] is True
    assert item["authority_effect"] == "allowed_with_disclosure"
    assert "不代表厂家既有设备事实" in item["report_disclosure"]
    assert "confirmed_source_fact" not in json.dumps(reopened, ensure_ascii=False)
    #: 叠加结果在 reopen 之后仍然带假设标注。
    applied = apply_aircraft_evidence(aircraft_profile(), reopened)
    assert applied["airborne_evidence"]["source_type"] == "engineering_assumption"
    #: 子系统子树同样带来源标注 —— P8 只收到该子树，标注不能在这一层丢失。
    assert applied["surveillance"]["airborne_evidence"]["source_type"] == (
        "engineering_assumption"
    )


def test_registry_rejects_duplicate_ids():
    item = assumption("remote_id_participation", ["network_remote_id"])
    with pytest.raises(ValueError, match="evidence_id 重复"):
        normalize_planning_evidence_registry({"items": [item, deepcopy(item)]})


# --------------------------------------------------------------------------- 5
def test_disclosure_lines_never_rewrite_an_assumption_into_a_fact():
    registry = normalize_planning_evidence_registry({
        "items": [
            assumption("remote_id_participation", ["network_remote_id"]),
            normalize_engineering_evidence({
                "evidence_id": "PEV-FACT-001", "scope": "aircraft",
                "target_id": "AIRCRAFT-SYN-E2E-01",
                "field": "communication_network_scope", "value": "dedicated",
                "source_type": "confirmed_source_fact", "source": "正式验收纪要 A-1",
                "confirmed": True,
            }),
            normalize_engineering_evidence({
                "evidence_id": "PEV-UNKNOWN-001", "scope": "aircraft",
                "target_id": "AIRCRAFT-SYN-E2E-01",
                "field": "communication_airborne_interfaces", "source_type": "unknown",
            }),
        ],
    })
    lines = evidence_disclosure_lines(registry)
    assert len(lines) == 3
    text = "\n".join(lines)
    assert "工程规划假设（不代表厂家既有设备事实）" in text
    assert "有正式资料支持" in text
    assert "尚无依据（unknown），不参与判定" in text
    #: 假设那一条绝不被写成事实。
    assumption_line = next(line for line in lines if "远程识别" in line)
    assert "有正式资料支持" not in assumption_line
