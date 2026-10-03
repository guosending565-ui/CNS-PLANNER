"""Round 2.4 —— 工程证据 / 规划假设的**应用服务级**闭环回归。

覆盖真实用户路径（不是 domain 单元）：

1. `add_planning_evidence` → 只写 ``planning_evidence`` 容器，**绝不**写 device catalog；
2. ``field_status`` 逐字段如实诊断"需求要求什么 / 机载是否已声明 / 是否已补录"；
3. P8 判定真的看到工程证据（``cns_service_capability`` 生效）；
4. snapshot / save / reopen 之后**仍然是 assumption**，且披露文本原样保留；
5. 撤回后回到"缺证据"状态（unknown），不是"不满足"。
"""

from copy import deepcopy
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cns_planner.application.workflow_service import WorkflowService  # noqa: E402
from cns_planner.domain.cns_service_contract import (  # noqa: E402
    AIRBORNE_COOPERATIVE_SURVEILLANCE_FIELD,
)

DEFAULTS = ROOT / "cns_planner" / "config" / "defaults.json"

DISCLOSURE = "本项为工程规划假设，仅作为本次规划的工程输入，不代表厂家既有设备事实。"


def _aircraft_catalog():
    return {
        "status": "passed", "catalog_id": "aircraft-cns-profile-catalog",
        "source": "synthetic_fixture", "count": 1,
        "items": [{
            "aircraft_id": "AIRCRAFT-SYN-E2E-01",
            "name": "SYNTHETIC E2E Validation Aircraft",
            "communication": {
                "status": "confirmed", "confirmed": True, "capabilities": ["radio"],
                "type": {"service_type": "command_control", "technology": "dedicated_radio",
                         "network_scope": "dedicated", "interfaces": ["validation_radio_v1"]},
                "performance": {"max_latency_s": 1.0, "min_redundancy": 1},
            },
            "navigation": {
                "status": "confirmed", "confirmed": True, "capabilities": ["terrestrial"],
                "type": {"technology": "terrestrial"},
                "performance": {"max_horizontal_error_m": 3.0, "min_redundancy": 1},
            },
            "surveillance": {
                "status": "confirmed", "confirmed": True, "capabilities": ["synthetic_adsb"],
                "type": {"target_cooperation": "cooperative", "sensor_mode": "active",
                         "technology": "adsb"},
                "performance": {"max_update_interval_s": 0.5, "min_redundancy": 1},
            },
            "source": "SYNTHETIC / ENGINEERING VALIDATION",
        }],
    }


def _required_cns():
    return {
        "status": "passed",
        "source": "explicit_adoption_from_required_cns_recommendation",
        "project_default": {
            "communication": {
                "status": "passed", "required": True, "confirmed": True,
                "coverage_requirement": 0.95,
                "service_key": "C:communication",
                "type": {"service_type": "command_control", "technology": "dedicated_radio",
                         "network_scope": "dedicated", "interfaces": ["ip"]},
                "performance": {"max_latency_s": 1.0, "min_redundancy": 1},
            },
            "navigation": {
                "status": "passed", "required": True, "confirmed": True,
                "coverage_requirement": 0.95, "type": {"technology": "unknown"},
                "performance": {"max_horizontal_error_m": 10.0, "integrity_required": True,
                                "min_redundancy": 1},
            },
            "surveillance": {
                "status": "passed", "required": True, "confirmed": True,
                "coverage_requirement": 0.95,
                "service_key": "S:rid_cooperative",
                "type": {"target_cooperation": "cooperative", "sensor_mode": "passive",
                         "technology": "network_remote_id",
                         "service_subtype": "cooperative_surveillance"},
                "performance": {"max_update_interval_s": 2.0, "min_redundancy": 1},
            },
        },
        "route_overrides": {},
    }


def _workflow(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    workflow.state["aircraft_profiles"] = _aircraft_catalog()
    workflow.state["selected_aircraft_profile_id"] = "AIRCRAFT-SYN-E2E-01"
    workflow.state["required_cns"] = _required_cns()
    return workflow


def _rid_evidence(**overrides):
    payload = {
        "planning_evidence": {
            "evidence_id": "PEV-SYN-RID-001",
            "scope": "aircraft",
            "target_id": "AIRCRAFT-SYN-E2E-01",
            "field": "remote_id_participation",
            "value": ["network_remote_id"],
            "source_type": "engineering_assumption",
            "source": "synthetic_acceptance_profile",
            "statement": "本测试场景假设该机载平台具备 network Remote ID 合作参与能力。",
            "reason": "使 Step4→Step5 主链在本测试场景中可评估",
            "report_disclosure": DISCLOSURE,
            "declared_by": "user",
            "confirmed": True,
            "confirmed_by_user": True,
        },
    }
    payload["planning_evidence"].update(overrides)
    return payload


# --------------------------------------------------------------------------- 1
def test_field_status_reports_which_evidence_is_actually_missing(tmp_path):
    workflow = _workflow(tmp_path)
    snapshot = workflow.planning_evidence_snapshot()
    by_field = {item["field"]: item for item in snapshot["field_status"]}

    rid = by_field["remote_id_participation"]
    assert rid["demanded_by_requirement"] is True
    assert rid["required_value"] == "network_remote_id"
    assert rid["declared_by_aircraft_profile"] is None
    assert rid["status"] == "evidence_required"

    communication_scope = by_field["communication_network_scope"]
    assert communication_scope["required_value"] == "dedicated"
    assert communication_scope["declared_by_aircraft_profile"] == "dedicated"
    assert communication_scope["status"] == "satisfied"
    assert communication_scope["matches_requirement"] is True

    #: 机载接口 ``validation_radio_v1`` 与需求 ``ip`` **无交集** ⇒ 这是**已确认不兼容**，
    #: 绝不是"缺证据"：把它误报成 satisfied 会让用户完全看不到真实阻塞。
    communication_interfaces = by_field["communication_airborne_interfaces"]
    assert communication_interfaces["declared_by_aircraft_profile"] == [
        "validation_radio_v1",
    ]
    assert communication_interfaces["status"] == "incompatible"
    assert communication_interfaces["matches_requirement"] is False
    assert "无交集" in communication_interfaces["status_reason"]


def test_snapshot_ships_the_field_catalogue_so_the_ui_never_guesses():
    fields = WorkflowService.planning_evidence_fields(None)
    assert fields["never_written_to_device_catalog"] is True
    assert fields["container"] == "project_state.planning_evidence"
    #: Round 2.5：`fields` 只承载**机载能力叠加**类证据；连续服务参数是另一类，
    #: 单独下发（前端不得把"评估参数"当成"机载能力声明"）。
    assert set(fields["fields"]) == {
        "communication_network_scope", "communication_airborne_interfaces",
        "remote_id_participation",
        #: Round 2.7：设备侧（提供者类型）工程假设是**能力叠加类**字段的第四个成员。
        #: 它同样只在规划消费点叠加到设备目录的**副本**上，绝不写入 device catalog。
        "provider_network_scope",
        #: Round 2.7：机载 / 运行场景的**性能声明**（链路时延上限）。P8 的机载判定会用
        #: RequiredCNS 的 performance 逐项核对，canonical 机载档案未声明时只能是 unknown。
        "communication_airborne_latency_s",
        #: 同一契约的 RID 侧性能声明（合作监视更新间隔上限）。
        "surveillance_airborne_update_interval_s",
    }
    continuous = fields["continuous_service_parameters"]
    assert continuous["c_full_outage_max_s"]["value_type"] == "number"
    assert continuous["c_full_outage_max_s"]["unit"] == "s"
    assert continuous["rtk_availability"]["value_type"] == "enum_scalar"
    assert continuous["relative_speed_basis"]["allowed"] == ("nominal", "conservative")
    assert fields["source_types"] == [
        "confirmed_source_fact", "external_reference",
        "engineering_assumption", "unknown",
    ]
    assert fields["authority_effects"]["external_reference"] == "allowed_as_external_reference"


# --------------------------------------------------------------------------- 2
def test_add_writes_only_the_evidence_container(tmp_path):
    workflow = _workflow(tmp_path)
    devices_before = deepcopy(workflow.state["device_catalog"])
    aircraft_before = deepcopy(workflow.state["aircraft_profiles"])

    result = workflow.add_planning_evidence(_rid_evidence())
    #: 写命令的响应是**完整 workflow 快照**（前端 `resourceAction` 会把它落地）；
    #: 局部聚合对象不得作为响应，否则界面既不刷新也看不到错误。
    assert "project" in result and "steps" in result, sorted(result)[:12]
    assert "planning_evidence" in result

    #: 绝不写入设备目录、绝不改写机载档案。
    assert workflow.state["device_catalog"] == devices_before
    assert workflow.state["aircraft_profiles"] == aircraft_before
    assert workflow.state["planning_evidence"]["items"][0]["evidence_id"] == \
        "PEV-SYN-RID-001"
    assert result["planning_evidence"]["active"][0]["field"] == "remote_id_participation"


def test_add_requires_a_known_aircraft_and_a_full_assumption_bundle(tmp_path):
    workflow = _workflow(tmp_path)
    with pytest.raises(ValueError, match="不是已载入的机载档案"):
        workflow.add_planning_evidence(_rid_evidence(target_id="AIRCRAFT-NOT-LOADED"))
    with pytest.raises(ValueError, match="engineering_assumption 缺少必填字段"):
        workflow.add_planning_evidence(_rid_evidence(report_disclosure=None))


def test_partial_progress_marks_the_field_as_still_missing(tmp_path):
    """机载只声明 ADS-B（不是网络远程识别）⇒ 该字段仍然需要工程依据。"""

    workflow = _workflow(tmp_path)
    workflow.state["aircraft_profiles"]["items"][0]["surveillance"]["type"][
        AIRBORNE_COOPERATIVE_SURVEILLANCE_FIELD
    ] = [{"technology": "adsb"}]
    by_field = {
        item["field"]: item
        for item in workflow.planning_evidence_snapshot()["field_status"]
    }
    rid = by_field["remote_id_participation"]
    #: adsb 声明能被读到（如实披露"机载声明了什么"），但**不等于**满足 RID 参与要求。
    assert rid["declared_by_aircraft_profile"] == ["adsb"]
    assert rid["effective_value"] == ["adsb"]
    assert rid["status"] == "incompatible"
    assert rid["matches_requirement"] is False
    assert "不覆盖需求技术" in rid["status_reason"]


def test_engineering_evidence_can_close_a_confirmed_incompatibility(tmp_path):
    """已确认不兼容可以被"与需求一致的显式声明"解决 —— 且必须带假设披露。"""

    workflow = _workflow(tmp_path)
    before = {
        item["field"]: item
        for item in workflow.planning_evidence_snapshot()["field_status"]
    }
    assert before["communication_airborne_interfaces"]["status"] == "incompatible"

    workflow.add_planning_evidence({
        "planning_evidence": {
            "evidence_id": "PEV-SYN-IF-001", "scope": "aircraft",
            "target_id": "AIRCRAFT-SYN-E2E-01",
            "field": "communication_airborne_interfaces", "value": ["ip"],
            "source_type": "engineering_assumption",
            "source": "synthetic_acceptance_profile",
            "statement": "本测试场景假设该机载平台具备 IP 机载接口。",
            "reason": "使 Step4→Step5 主链在本测试场景中可评估",
            "report_disclosure": DISCLOSURE,
            "declared_by": "user", "confirmed": True, "confirmed_by_user": True,
        },
    })
    after = {
        item["field"]: item
        for item in workflow.planning_evidence_snapshot()["field_status"]
    }
    entry = after["communication_airborne_interfaces"]
    assert entry["status"] == "satisfied"
    assert entry["effective_value"] == ["validation_radio_v1", "ip"]
    assert entry["recorded_source_type"] == "engineering_assumption"


# --------------------------------------------------------------------------- 3
def test_evidence_reaches_the_snapshot_and_disclosure():
    workflow = WorkflowService(Path("_nonexistent_tmp_dir_probe") / "p.json", DEFAULTS)
    snapshot = workflow.snapshot()
    assert "planning_evidence" in snapshot
    assert "planning_evidence_fields" in snapshot
    #: 快照给的是**只读投影**（含 field_status），不是原始 registry。
    assert snapshot["planning_evidence"]["registry"]["items"] == []
    assert snapshot["planning_evidence"]["field_status"]


# --------------------------------------------------------------------------- 4
def test_assumption_stays_an_assumption_after_reopen(tmp_path):
    workflow = _workflow(tmp_path)
    workflow.add_planning_evidence(_rid_evidence())
    workflow.save()

    reopened = WorkflowService(tmp_path / "project.json", DEFAULTS)
    registry = reopened.state["planning_evidence"]
    item = registry["items"][0]
    assert item["source_type"] == "engineering_assumption"
    assert item["confirmed"] is True
    assert item["report_disclosure"] == DISCLOSURE
    assert item["confirmation_status"] == "confirmed"
    assert item["authority_effect"] == "allowed_with_disclosure"
    assert "confirmed_source_fact" not in str(registry)

    snapshot = reopened.planning_evidence_snapshot()
    assert any("工程规划假设" in line for line in snapshot["disclosure_lines"])
    assert snapshot["aircraft_evidence"]["source_type"] == "engineering_assumption"
    #: 重开之后该字段仍然由**工程假设**支撑，绝不被读成厂家事实。
    by_field = {entry["field"]: entry for entry in snapshot["field_status"]}
    assert by_field["remote_id_participation"]["recorded_source_type"] == (
        "engineering_assumption"
    )


def test_replacing_the_same_field_supersedes_the_previous_record(tmp_path):
    workflow = _workflow(tmp_path)
    workflow.add_planning_evidence(_rid_evidence())
    workflow.add_planning_evidence(_rid_evidence(
        evidence_id="PEV-SYN-RID-002", value=["adsb"],
        source_type="confirmed_source_fact", source="正式验收纪要 A-1",
        statement=None, reason=None, report_disclosure=None, confirmed_by_user=None,
    ))
    statuses = {
        item["evidence_id"]: item["status"]
        for item in workflow.state["planning_evidence"]["items"]
    }
    assert statuses == {"PEV-SYN-RID-001": "superseded", "PEV-SYN-RID-002": "active"}
    active = workflow.planning_evidence_snapshot()["active"]
    assert [item["evidence_id"] for item in active] == ["PEV-SYN-RID-002"]


def test_withdraw_returns_the_field_to_evidence_required(tmp_path):
    workflow = _workflow(tmp_path)
    workflow.add_planning_evidence(_rid_evidence())
    workflow.withdraw_planning_evidence({"evidence_id": "PEV-SYN-RID-001"})
    snapshot = workflow.planning_evidence_snapshot()
    assert snapshot["active"] == []
    by_field = {item["field"]: item for item in snapshot["field_status"]}
    assert by_field["remote_id_participation"]["status"] == "evidence_required"
    #: 撤回是**幂等**的状态置位（记录保留、状态变 withdrawn），不删除审计痕迹。
    workflow.withdraw_planning_evidence({"evidence_id": "PEV-SYN-RID-001"})
    statuses = {
        item["evidence_id"]: item["status"]
        for item in workflow.state["planning_evidence"]["items"]
    }
    assert statuses == {"PEV-SYN-RID-001": "withdrawn"}
    with pytest.raises(KeyError):
        workflow.withdraw_planning_evidence({"evidence_id": "PEV-DOES-NOT-EXIST"})


def test_unknown_evidence_is_recorded_but_never_participates(tmp_path):
    workflow = _workflow(tmp_path)
    workflow.add_planning_evidence({
        "planning_evidence": {
            "evidence_id": "PEV-SYN-UNKNOWN", "scope": "aircraft",
            "target_id": "AIRCRAFT-SYN-E2E-01", "field": "remote_id_participation",
            "source_type": "unknown",
            "source": "尚无任何资料",
        },
    })
    snapshot = workflow.planning_evidence_snapshot()
    assert snapshot["active"] == []
    by_field = {item["field"]: item for item in snapshot["field_status"]}
    assert by_field["remote_id_participation"]["status"] == "evidence_required"
    assert any("尚无依据" in line for line in snapshot["disclosure_lines"])
