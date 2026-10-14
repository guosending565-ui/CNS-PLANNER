"""Round38-FC30：进离场航段与巡航航路衔接的统一几何身份（最小定向测试）。

覆盖本轮**实际修改**的几何身份、发布状态与 currentness 判据：

1. 写入进离场航段时显式绑定当前巡航航路的**完整几何指纹**；
2. 巡航几何版本变化后，旧进离场航段不再属于 current 航路（绝不与新媒体拼接）；
3. 不同高度层不得被拼接（写入拒绝 + 衔接判据）；
4. 缺少正式进离场航段时**不触碰**已发布的巡航航路；
5. 固定 1000 m 保障宽带只在来源航路为 ``passed`` 时才可能 current；
6. FIG / T3D4 统一消费接口给出 canonical 几何指纹与衔接状态，且不重算第二套中心线。

不重算 P14～P17、不运行 Theta*、不做全量回归。
"""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from cns_planner.algorithms.coverage.geometric_3d import path_length_m
from cns_planner.application.fixed_corridor_geometry_service import (
    FixedCorridorGeometryService,
)
from cns_planner.application.visualization_3d_service import Visualization3DService
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.layered_route_validation import path_fingerprint
from cns_planner.domain.spatial_3d import (
    TERMINAL_TRANSITION_IDENTITY_VERSION, normalize_departure_arrival_procedure,
    terminal_transition_identity_reasons,
)
from cns_planner.gis.corridor_geometry import build_fixed_corridor_geometry


DEFAULTS = Path("cns_planner/config/defaults.json")
ROUTE_ID = "R0005"
LAYER_ID = "ALT-100"
OTHER_LAYER_ID = "ALT-150"

PATH = [[122.10, 30.00], [122.12, 30.02], [122.16, 30.05]]
NEW_PATH = [[122.10, 30.00], [122.14, 30.03], [122.20, 30.09]]
LENGTH_M = path_length_m(PATH)
NEW_LENGTH_M = path_length_m(NEW_PATH)
JOIN_M = LENGTH_M / 5.0
LEAVE_M = LENGTH_M * 4.0 / 5.0

ROUTE = {
    "route_id": ROUTE_ID, "status": "passed", "distance_m": LENGTH_M,
    "path": deepcopy(PATH), "path_crs": "OGC:CRS84",
    "provenance": {
        "source_type": "layered_candidate_operational_adoption_v1",
        "candidate_id": "LRC2-R0005-ALT-100-layeredcandv1-d5",
        "validation_id": "LRV-EF3A9B0D7CB7",
    },
}


# --------------------------------------------------------------------------- helpers


def workflow(tmp_path):
    service = WorkflowService(tmp_path / "project.json", DEFAULTS)
    service.state["operational_routes"] = [deepcopy(ROUTE)]
    service.state["result_statuses"]["routes"] = "passed"
    return service


def layer_payload(layer_id, nominal_m):
    return {
        "altitude_layer_id": layer_id, "name": f"{nominal_m:.0f} m 层",
        "nominal_altitude_m": nominal_m, "lower_altitude_m": nominal_m - 10.0,
        "upper_altitude_m": nominal_m + 25.0,
        "vertical_reference": "egm2008_orthometric",
        "source": "工程确认-测试", "evidence": {"note": "fc30 test"},
        "confirmed": True,
    }


def assignment_payload(layer_id):
    return {
        "route_id": ROUTE_ID, "altitude_layer_id": layer_id,
        "source": "工程确认-测试", "confirmed": True,
    }


def procedure_payload(kind, layer_id=LAYER_ID, **overrides):
    if kind == "departure":
        payload = {
            "procedure_id": "DP-1", "procedure_type": "departure", "route_id": ROUTE_ID,
            "site_reference": "REF-1", "altitude_layer_id": layer_id,
            "transition_mode": "climb_to_cruise_layer",
            "horizontal_geometry": {"turn_radius_m": 50.0},
            "vertical_profile": {
                "climb_rate_mps": 2.5,
                "terminal_altitude_egm2008_m": 20.0,
                "terminal_altitude_source": "manual_engineering_review",
                "terminal_altitude_evidence": "评审记录 DP-1",
            },
            "join_leave_point": {"kind": "join", "distance_along_route_m": JOIN_M},
            "source": "工程确认-测试", "confirmed": True,
        }
    else:
        payload = {
            "procedure_id": "AR-1", "procedure_type": "arrival", "route_id": ROUTE_ID,
            "site_reference": "REF-2", "altitude_layer_id": layer_id,
            "transition_mode": "descend_from_cruise_layer",
            "horizontal_geometry": {"turn_radius_m": 60.0},
            "vertical_profile": {
                "descent_rate_mps": 2.0,
                "terminal_altitude_egm2008_m": 40.0,
                "terminal_altitude_source": "verified_site_survey",
                "terminal_altitude_evidence": "survey AR-1",
            },
            "join_leave_point": {"kind": "leave", "distance_along_route_m": LEAVE_M},
            "source": "工程确认-测试", "confirmed": True,
        }
    payload.update(overrides)
    return payload


def configured_service(tmp_path, *, with_procedures=True):
    """航路 + 高度层 +（可选）已绑定几何身份的进离场航段。"""

    service = workflow(tmp_path)
    service.set_altitude_layer(layer_payload(LAYER_ID, 100.0))
    service.set_route_operating_layer(assignment_payload(LAYER_ID))
    if with_procedures:
        service.set_departure_arrival_procedure(procedure_payload("departure"))
        service.set_departure_arrival_procedure(procedure_payload("arrival"))
    return service


def stored_procedure(service, procedure_id):
    return next(
        item for item in service.state["spatial_3d"]["departure_arrival_procedures"]
        if item["procedure_id"] == procedure_id
    )


def readiness_blockers(report):
    return " ".join(report.get("blockers") or [])


# --------------------------------------------------- 1. 写入时绑定巡航几何身份


def test_written_procedure_binds_the_current_cruise_geometry(tmp_path):
    service = configured_service(tmp_path)

    procedure = stored_procedure(service, "DP-1")
    assert procedure["route_geometry_fingerprint"] == path_fingerprint(PATH)
    # 绑定的是**完整**中心线几何身份，而不是 route_id 的别名。
    assert procedure["route_geometry_fingerprint"].startswith("layeredpathv1-")
    assert procedure["route_geometry_fingerprint"] != path_fingerprint(NEW_PATH)
    # 绑定成立时不得出现衔接身份 blocker。
    assert "衔接身份" not in readiness_blockers(service.route_3d_profile_readiness())


def test_normalizer_keeps_the_binding_additively_and_never_invents_one():
    without = normalize_departure_arrival_procedure(procedure_payload("departure"))
    assert without["route_geometry_fingerprint"] is None

    with_binding = normalize_departure_arrival_procedure(
        procedure_payload("departure", route_geometry_fingerprint=path_fingerprint(PATH))
    )
    assert with_binding["route_geometry_fingerprint"] == path_fingerprint(PATH)


def test_explicit_foreign_geometry_fingerprint_is_rejected(tmp_path):
    service = workflow(tmp_path)
    service.set_altitude_layer(layer_payload(LAYER_ID, 100.0))
    service.set_route_operating_layer(assignment_payload(LAYER_ID))

    with pytest.raises(ValueError, match="route_geometry_fingerprint"):
        service.set_departure_arrival_procedure(
            procedure_payload("departure", route_geometry_fingerprint="layeredpathv1-deadbeef")
        )
    assert service.state["spatial_3d"]["departure_arrival_procedures"] == []


# --------------------------------------------------- 2. 巡航几何/高度层变化 ⇒ 衔接失效


def test_cruise_geometry_change_breaks_the_terminal_transition_identity(tmp_path):
    service = configured_service(tmp_path)
    assert service.route_operating_readiness()["departure_procedure"]["status"] == "confirmed"

    # 巡航航路被替换成另一份几何（同一个 route_id）：旧进离场航段不得继续冒充 current。
    service.state["operational_routes"] = [{**deepcopy(ROUTE), "path": deepcopy(NEW_PATH),
                                            "distance_m": NEW_LENGTH_M}]
    service.set_route_operating_layer(assignment_payload(LAYER_ID))

    assert stored_procedure(service, "DP-1")["status"] == "pending_confirmation"
    report = service.route_operating_readiness()
    assert report["departure_procedure"]["status"] == "pending_confirmation"
    assert any("几何身份" in reason for reason in report["departure_procedure"]["reasons"])

    profile_report = service.route_3d_profile_readiness()
    assert profile_report["status"] == "not_ready"
    assert "衔接身份" in readiness_blockers(profile_report)
    assert profile_report["blockers"]


def test_cruise_layer_change_breaks_the_terminal_transition_identity(tmp_path):
    service = configured_service(tmp_path)
    service.set_altitude_layer(layer_payload(OTHER_LAYER_ID, 150.0))
    service.set_route_operating_layer(assignment_payload(OTHER_LAYER_ID))

    # procedure 仍声明 ALT-100，航路当前巡航层已是 ALT-150：不同高度层不得拼接。
    assert stored_procedure(service, "DP-1")["status"] == "pending_confirmation"
    report = service.route_operating_readiness()
    assert any("高度层" in reason for reason in report["departure_procedure"]["reasons"])
    assert "高度层" in readiness_blockers(service.route_3d_profile_readiness())


def test_procedure_layer_must_match_the_active_cruise_layer_on_write(tmp_path):
    service = workflow(tmp_path)
    service.set_altitude_layer(layer_payload(LAYER_ID, 100.0))
    service.set_altitude_layer(layer_payload(OTHER_LAYER_ID, 150.0))
    service.set_route_operating_layer(assignment_payload(LAYER_ID))

    with pytest.raises(ValueError, match="高度层"):
        service.set_departure_arrival_procedure(
            procedure_payload("departure", layer_id=OTHER_LAYER_ID)
        )
    assert service.state["spatial_3d"]["departure_arrival_procedures"] == []


# --------------------------------------------------- 3. 缺失进离场航段不得破坏巡航航路


def test_missing_terminal_segments_never_touch_the_published_cruise_route(tmp_path):
    service = configured_service(tmp_path, with_procedures=False)
    before = deepcopy(service.state["operational_routes"])

    report = service.route_3d_profile_readiness()
    assert report["status"] == "not_ready"
    assert "缺少 confirmed departure procedure" in readiness_blockers(report)
    assert "缺少 confirmed arrival procedure" in readiness_blockers(report)

    # 已合法发布的巡航航路原样保留：既不被改写，也不被清空。
    assert service.state["operational_routes"] == before
    assert service.state["operational_routes"][0]["status"] == "passed"
    assert service.state["operational_routes"][0]["path"] == PATH
    assert service.state["spatial_3d"]["route_3d_profiles"] == {}


# --------------------------------------------------- 4. 衔接判据本体（domain 纯函数）


def test_terminal_transition_identity_reasons_are_fail_closed():
    fingerprint = path_fingerprint(PATH)
    base = normalize_departure_arrival_procedure(
        procedure_payload("departure", route_geometry_fingerprint=fingerprint)
    )
    assert terminal_transition_identity_reasons(
        base,
        expected_route_geometry_fingerprint=fingerprint,
        route_altitude_layer_id=LAYER_ID,
    ) == []

    unbound = normalize_departure_arrival_procedure(procedure_payload("departure"))
    assert any("未绑定" in item for item in terminal_transition_identity_reasons(
        unbound,
        expected_route_geometry_fingerprint=fingerprint,
        route_altitude_layer_id=LAYER_ID,
    ))

    stale_binding = normalize_departure_arrival_procedure(
        procedure_payload("departure", route_geometry_fingerprint=fingerprint)
    )
    assert any("不一致" in item for item in terminal_transition_identity_reasons(
        stale_binding,
        expected_route_geometry_fingerprint=path_fingerprint(NEW_PATH),
        route_altitude_layer_id=LAYER_ID,
    ))

    wrong_layer = normalize_departure_arrival_procedure(
        procedure_payload("departure", route_geometry_fingerprint=fingerprint,
                          layer_id=OTHER_LAYER_ID)
    )
    assert any("高度层" in item for item in terminal_transition_identity_reasons(
        wrong_layer,
        expected_route_geometry_fingerprint=fingerprint,
        route_altitude_layer_id=LAYER_ID,
    ))


# --------------------------------------------------- 5. 固定 1000 m 保障宽带的来源门禁


class ReadOnlySession:
    def __init__(self, state):
        self.state = state
        self.save_calls = 0

    def save(self):
        self.save_calls += 1
        raise AssertionError("固定宽带只读投影不得保存 ProjectState")


class StubCorridorService:
    def __init__(self, result):
        self._result = result

    def result_snapshot(self):
        return deepcopy(self._result)


def fixed_band_service(route_status):
    route = {**deepcopy(ROUTE), "status": route_status}
    # 与 ``FixedCorridorGeometryService._route_authority`` 同一来源口径：prior 指纹都取
    # provenance 的实际取值（此处为 None），否则重建指纹与 P14 条目无法逐字比对。
    authority = {
        "source_type": "layered_candidate_operational_adoption_v1",
        "candidate_id": "LRC2-R0005-ALT-100-layeredcandv1-d5",
        "candidate_fingerprint": None,
        "validation_id": "LRV-EF3A9B0D7CB7",
        "validation_fingerprint": None,
        "adoption_fingerprint": None,
    }
    built = build_fixed_corridor_geometry(
        route["path"], route_id=ROUTE_ID, route_authority=authority,
        source_crs="OGC:CRS84", construction_crs=None,
    )
    corridor = {
        "status": "passed",
        "algorithm_id": "cns_service_corridor_v1",
        "algorithm_version": "1.4",
        "routes": [{
            "route_id": ROUTE_ID,
            "fixed_corridor_geometry_status": "available",
            "fixed_corridor_geometry": {
                "geometry_fingerprint": built["geometry_fingerprint"],
            },
        }],
    }
    state = {
        "revision": 471,
        "operational_routes": [route],
        "cns_corridor_assessment": corridor,
        "result_statuses": {"cns_corridor_assessment": "passed"},
    }
    session = ReadOnlySession(state)
    return FixedCorridorGeometryService(session, StubCorridorService(corridor)), session


def test_fixed_band_requires_a_passed_source_route():
    service, session = fixed_band_service("passed")
    current = service.geometry(ROUTE_ID)
    assert current["currentness"] == "current"
    assert current["route_geometry_fingerprint"] == path_fingerprint(PATH)
    assert current["route_operational_status"] == "passed"

    stale_service, stale_session = fixed_band_service("stale")
    stale = stale_service.geometry(ROUTE_ID)
    assert stale["currentness"] == "stale"
    assert stale["currentness_reason"] == "route_operational_status_not_passed"
    assert stale["geometry_identity"]["route_operational_status"] == "stale"
    # 只读：两次调用都不写状态。
    assert session.save_calls == 0 and stale_session.save_calls == 0


# --------------------------------------------------- 6. FIG / T3D4 统一消费接口


def test_visualization_route_entry_exposes_canonical_identity_and_transition(tmp_path):
    service = configured_service(tmp_path)
    view = Visualization3DService(service.state, data=SimpleNamespace(paths={}))

    payload = view.routes()
    entry = next(row for row in payload["routes"] if row["route_id"] == ROUTE_ID)

    # 完整几何身份与既有 N1 / 固定宽带同一取值。
    assert entry["identity"]["route_geometry_fingerprint"] == path_fingerprint(PATH)
    # 不重算第二套中心线：直接回显权威容器路径。
    assert entry["path"] == PATH
    assert entry["authority"] == "canonical_operational_routes"

    transition = entry["terminal_transition"]
    assert transition["identity_version"] == TERMINAL_TRANSITION_IDENTITY_VERSION
    assert transition["route_geometry_fingerprint"] == path_fingerprint(PATH)
    assert transition["route_altitude_layer_id"] == LAYER_ID
    assert transition["departure"]["procedure_id"] == "DP-1"
    assert transition["departure"]["is_current_part_of_route"] is True
    assert transition["arrival"]["is_current_part_of_route"] is True
    assert transition["current_part_of_route"] is True
    assert payload["read_only"] is True


def test_visualization_route_entry_reports_a_stale_terminal_binding(tmp_path):
    service = configured_service(tmp_path)
    # 巡航几何换成另一份：投影必须如实报告衔接不再 current，而不是照 route_id 拼上。
    service.state["operational_routes"] = [
        {**deepcopy(ROUTE), "path": deepcopy(NEW_PATH), "distance_m": NEW_LENGTH_M}
    ]
    view = Visualization3DService(service.state, data=SimpleNamespace(paths={}))
    entry = next(row for row in view.routes()["routes"] if row["route_id"] == ROUTE_ID)

    assert entry["terminal_transition"]["current_part_of_route"] is False
    assert entry["terminal_transition"]["departure"]["is_current_part_of_route"] is False
    assert entry["terminal_transition"]["departure"]["identity_reasons"]
    assert entry["path"] == NEW_PATH
