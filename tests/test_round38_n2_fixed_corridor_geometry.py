"""Round38-N2：固定 1000 m CNS 保障宽带只读几何投影的定向测试。

覆盖：
* 几何只来自 W1A 唯一构造器（半宽恒 500 m、总宽 1000 m、策略版本 fixed-1000m-v1）；
* P14 结果 stale ⇒ 几何 currentness 必须是 stale（绝不把 stale 当 current）；
* 固定几何指纹与 P14 条目不一致 ⇒ stale（**不得**拿离散评估身份当固定几何身份）；
* 构造被阻断 ⇒ 不给出任何几何（fail-closed）；
* 航路缺失 / 多条未指定 ⇒ unavailable；
* 只读：不 save、不改 state；
* 2000 m 监测关联与 500 m 固定半宽严格分离（固定几何接口绝不输出关联半径语义）。
"""

from copy import deepcopy

import pytest

from cns_planner.application.fixed_corridor_geometry_service import (
    GEOMETRY_SEMANTICS, WIDTH_VS_ASSOCIATION_DISCLAIMER, FixedCorridorGeometryService,
)
from cns_planner.gis.corridor_geometry import build_fixed_corridor_geometry


ROUTE_PATH = [[122.10, 30.00], [122.20, 30.05], [122.26, 30.12]]


class ReadOnlySession:
    def __init__(self, state):
        self.state = state
        self.save_calls = 0

    def save(self):
        self.save_calls += 1
        raise AssertionError("固定宽带只读投影不得保存 ProjectState")


class StubCorridorService:
    """只读 P14 投影桩：``result_snapshot()`` 返回给定结果。"""

    def __init__(self, result):
        self._result = result

    def result_snapshot(self):
        return deepcopy(self._result)


def _expected(route=None):
    """直接调用 W1A 唯一构造器得到期望几何与指纹（身份测试的对照口径）。"""

    route = _route() if route is None else route
    return build_fixed_corridor_geometry(
        route["path"], route_id=str(route["route_id"]),
        route_authority={
            "source_type": "layered_operational_adoption",
            "candidate_id": None,
            "candidate_fingerprint": "layeredcandv1-abc",
            "validation_id": None, "validation_fingerprint": None,
            "adoption_fingerprint": None,
        },
        source_crs=str(route.get("path_crs") or "OGC:CRS84"),
        construction_crs=route.get("construction_crs") or route.get("metric_crs"),
    )


def _rebuilt_fingerprint(route=None):
    return _expected(route)["geometry_fingerprint"]


def _route(**overrides):
    route = {
        "route_id": "R0005", "status": "passed", "path_crs": "OGC:CRS84",
        "path": ROUTE_PATH, "start_node_id": "N005", "end_node_id": "N006",
        "provenance": {"source_type": "layered_operational_adoption",
                       "candidate_fingerprint": "layeredcandv1-abc"},
    }
    route.update(overrides)
    return route


def _corridor_result(*, status="passed", fixed_fingerprint, fixed_status="available",
                     algorithm_version="1.4", with_entry=True):
    entry = {
        "route_id": "R0005",
        "corridor_geometry_fingerprint": "discrete-eval-identity-DO-NOT-USE",
    }
    if with_entry:
        entry["fixed_corridor_geometry_status"] = fixed_status
        entry["fixed_corridor_geometry"] = {"geometry_fingerprint": fixed_fingerprint}
    return {
        "status": status,
        "algorithm_id": "cns_service_corridor_v1",
        "algorithm_version": algorithm_version,
        "routes": [entry] if with_entry else [],
    }


def _service(*, route=None, corridor=None, result_statuses=None, state_extra=None):
    route = _route() if route is None else route
    state = {
        "revision": 468,
        "operational_routes": [route],
        "cns_corridor_assessment": corridor,
        #: 真实项目一定带 result_statuses；它是 currentness 的持久化 raw 来源。
        "result_statuses": (
            {"cns_corridor_assessment": "passed"}
            if result_statuses is None else result_statuses
        ),
    }
    state.update(state_extra or {})
    session = ReadOnlySession(state)
    return FixedCorridorGeometryService(session, StubCorridorService(corridor)), session


# ---- 1. 几何契约 ------------------------------------------------------------

def test_geometry_is_built_by_the_single_w1a_constructor():
    fingerprint = _rebuilt_fingerprint()
    service, session = _service(corridor=_corridor_result(fixed_fingerprint=fingerprint))
    before = deepcopy(session.state)

    result = service.geometry("R0005")

    assert result["status"] == "available"
    assert result["width_policy_version"] == "fixed-1000m-v1"
    assert result["corridor_half_width_m"] == 500.0
    assert result["corridor_total_width_m"] == 1000.0
    assert result["width_semantics"] == "cns_service_target_band"
    assert result["geometry_algorithm_id"] == "cns_fixed_corridor_buffer_v1"
    assert result["geometry_fingerprint"] == fingerprint
    assert result["geometry"]["type"] in {"Polygon", "MultiPolygon"}
    assert result["currentness"] == "current"
    assert result["currentness_reason"] is None
    assert result["semantics"] == GEOMETRY_SEMANTICS
    assert result["writes_performed"] == 0
    assert session.save_calls == 0
    assert session.state == before


def test_geometry_is_byte_identical_to_a_direct_constructor_call():
    """接口不得引入第二套 buffer：几何必须与直接调用 W1A 构造器逐坐标一致。"""

    service, _ = _service(corridor=_corridor_result(fixed_fingerprint=_rebuilt_fingerprint()))
    result = service.geometry("R0005")
    direct = _expected()
    assert result["geometry"] == direct["geometry"]
    assert result["geometry_fingerprint"] == direct["geometry_fingerprint"]
    assert result["projected_area_m2"] == pytest.approx(direct["projected_area_m2"])


def test_half_width_cannot_be_changed_by_route_or_policy():
    """固定契约下 500 m 半宽不可被任何输入改写（含 legacy 半宽字段）。"""

    route = _route(horizontal_half_width_m=900.0, construction_crs=None)
    service, _ = _service(
        route=route, corridor=_corridor_result(fixed_fingerprint=_rebuilt_fingerprint()),
    )
    result = service.geometry("R0005")
    assert result["corridor_half_width_m"] == 500.0
    assert result["corridor_total_width_m"] == 1000.0


# ---- 2. currentness 门禁 -----------------------------------------------------

def test_stale_corridor_result_projects_stale_geometry_never_current():
    """P14 有效状态为 stale ⇒ 几何 currentness 必须是 stale，绝不标 current。

    currentness 的权威来源是 ``result_currentness`` 的持久化 raw 状态 + 算法语义
    投影；本服务**不自行发明**第二套判据，因此 fixture 必须如实给出该 raw 状态。
    """

    fingerprint = _rebuilt_fingerprint()
    service, _ = _service(
        corridor=_corridor_result(status="stale", fixed_fingerprint=fingerprint),
        result_statuses={"cns_corridor_assessment": "stale"},
    )
    result = service.geometry("R0005")
    assert result["currentness"] == "stale"
    assert result["currentness_reason"] == "corridor_result_not_current"
    assert result["geometry_identity"]["corridor_result_status"] == "stale"
    # 几何本身仍如实给出（它由航路确定性构造），但身份上不得被当成 current 边界。
    assert result["status"] == "available"


def test_algorithm_semantics_stale_also_blocks_currentness():
    """P14 存储版本与当前实现不同 ⇒ 有效状态 stale ⇒ 几何不得标 current。"""

    service, _ = _service(
        corridor=_corridor_result(
            fixed_fingerprint=_rebuilt_fingerprint(), algorithm_version="1.0",
        ),
    )
    result = service.geometry("R0005")
    assert result["currentness"] == "stale"
    assert result["currentness_reason"] == "corridor_result_not_current"


def test_fingerprint_mismatch_projects_stale():
    service, _ = _service(
        corridor=_corridor_result(fixed_fingerprint="corridorgeometryv1-deadbeef"),
    )
    result = service.geometry("R0005")
    assert result["currentness"] == "stale"
    assert result["currentness_reason"] == "fixed_corridor_geometry_fingerprint_mismatch"


def test_discrete_evaluation_identity_is_not_accepted_as_fixed_geometry_identity():
    """离散评估身份（corridor_geometry_fingerprint）不得被当成固定几何身份。

    P14 条目里的 ``corridor_geometry_fingerprint`` 与固定宽带几何指纹是两个不同的
    物理量；若把它当固定几何身份，会让"离散评估身份相同"被误判成"固定宽带 current"。
    """

    corridor = _corridor_result(fixed_fingerprint=_rebuilt_fingerprint())
    entry = corridor["routes"][0]
    entry["corridor_geometry_fingerprint"] = _rebuilt_fingerprint()
    entry["fixed_corridor_geometry"] = {}
    entry["fixed_corridor_geometry_status"] = "available"
    service, _ = _service(corridor=corridor)

    result = service.geometry("R0005")
    assert result["currentness"] == "stale"
    assert result["currentness_reason"] == "fixed_corridor_geometry_identity_absent"


def test_route_entry_missing_projects_stale():
    service, _ = _service(
        corridor=_corridor_result(fixed_fingerprint=_rebuilt_fingerprint(), with_entry=False),
    )
    result = service.geometry("R0005")
    assert result["currentness"] == "stale"
    assert result["currentness_reason"] == "corridor_route_entry_missing"


def test_corridor_result_absent_projects_stale_not_unavailable():
    service, _ = _service(corridor=None)
    result = service.geometry("R0005")
    assert result["currentness"] == "stale"
    assert result["currentness_reason"] == "corridor_result_not_current"
    assert result["status"] == "available"


def test_not_available_geometry_status_projects_stale():
    service, _ = _service(
        corridor=_corridor_result(
            fixed_fingerprint=_rebuilt_fingerprint(), fixed_status="blocked",
        ),
    )
    result = service.geometry("R0005")
    assert result["currentness"] == "stale"
    assert result["currentness_reason"] == "fixed_corridor_geometry_not_available"


# ---- 3. fail-closed ----------------------------------------------------------

def test_blocked_construction_returns_no_geometry_at_all():
    """航路不足两点 / 坐标非法时构造阻断，必须不给出任何几何。"""

    service, _ = _service(
        route=_route(path=[[122.1, 30.0]]),
        corridor=_corridor_result(fixed_fingerprint=_rebuilt_fingerprint()),
    )
    result = service.geometry("R0005")
    assert result["status"] == "blocked"
    assert result["geometry"] is None
    assert result["geometry_fingerprint"] is None
    assert result["reason"]


def test_antimeridian_route_is_blocked_without_approximate_geometry():
    service, _ = _service(
        route=_route(path=[[179.9, 30.0], [-179.9, 30.1]]),
        corridor=_corridor_result(fixed_fingerprint=_rebuilt_fingerprint()),
    )
    result = service.geometry("R0005")
    assert result["status"] == "blocked"
    assert result["geometry"] is None
    assert "antimeridian" in result["reason"]


def test_unavailable_when_route_absent_or_ambiguous():
    service, _ = _service(route=None, corridor=None)
    service.session.state["operational_routes"] = []
    result = service.geometry("R0005")
    assert result["status"] == "unavailable"
    assert result["currentness"] == "unavailable"
    assert result["geometry"] is None

    two = _route()
    other = _route(route_id="R0006")
    service.session.state["operational_routes"] = [two, other]
    ambiguous = service.geometry(None)
    assert ambiguous["status"] == "unavailable"
    assert ambiguous["geometry"] is None


# ---- 4. 2000 m 关联与 500 m 固定半宽严格分离 ---------------------------------

def test_fixed_band_never_claims_the_monitoring_association_radius():
    """固定宽带接口不得把 2000 m 关联半径当作自己的宽度语义。

    注意：免责声明里**故意**提到 2000 m（为了显式声明"两者不同、互不替代"），
    因此这里断言的是"不存在关联半径**字段**、不存在把它当宽度的数值"，
    而不是"文本里不出现 2000"。
    """

    service, _ = _service(corridor=_corridor_result(fixed_fingerprint=_rebuilt_fingerprint()))
    result = service.geometry("R0005")

    def walk(value, path=""):
        if isinstance(value, dict):
            for key, item in value.items():
                yield f"{path}.{key}", key, item
                yield from walk(item, f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                yield from walk(item, f"{path}[{index}]")

    fields = {key for _, key, _ in walk(result)}
    assert "monitor_association_radius_m" not in fields
    assert "gap_tolerance_s" not in fields
    assert "route_speed_mps" not in fields
    assert "parameters" not in fields

    # 唯一的宽度数值必须是固定契约的 500 / 1000。
    assert result["corridor_half_width_m"] == 500.0
    assert result["corridor_total_width_m"] == 1000.0
    assert result["width_semantics"] == "cns_service_target_band"
    assert result["width_policy_version"] == "fixed-1000m-v1"

    # 分离声明必须显式存在且说明"不是认证覆盖"。
    assert result["disclaimer"] == WIDTH_VS_ASSOCIATION_DISCLAIMER
    assert "2000" in result["disclaimer"]
    assert "互不替代" in result["disclaimer"]
    assert "不是认证导航覆盖区" in result["disclaimer"]
    assert result["semantics"] == (
        "cns_service_target_band_engineering_reference_not_certified_coverage"
    )
