"""Round38-SITE-2 — 统一 CNS 候选站址与 Radar 起降点布设支持（最小确定性夹具）。

覆盖任务要求的七项验证：

1. Radar 可以接收合格的已登记起降点候选；
2. Radar 原有 373 座铁塔候选能力不丢失（塔顶高度建模不变）；
3. 起降点 CRS 未确认时仍被阻断；
4. 起降点**不会**错误继承塔顶高度；
5. 合成或未确认设施不能绕过真实性门禁；
6. Radar 原有定向覆盖 / LOS 算法没有被普通圆形覆盖替换；
7. C/N/RID 等服务仍按各自适用性筛选站址（共享站址 ≠ 共享覆盖）。
"""

from copy import deepcopy
from types import SimpleNamespace

from cns_planner.application.cns_shared_site_candidate_service import (
    CNSSharedSiteCandidateService, SYNTHETIC_DATASET_CLASSES,
)
from cns_planner.application.radar_shared_site_adapter import (
    INSTALLATION_POSITION_GROUND_SITE, INSTALLATION_POSITION_TOWER_TOP,
    PLANNING_ESTIMATE_ORIGIN_SOURCE,
    installation_height_evidence, radar_candidate_sites, radar_site_inputs,
)
from cns_planner.domain.tower_colocation import tower_planning_service_origin_estimates
from cns_planner.gis.radar_layout_adapter import resolve_radar_origins


# ----------------------------------------------------------------------------- fixtures


def _crs(value=None, confirmed=False):
    return {
        "source_crs": {
            "value": value,
            "status": "confirmed" if confirmed else "pending_confirmation",
            "confirmed": confirmed,
        },
        "representation_crs": {"value": "OGC:CRS84", "status": "declared"},
    }


def _landing_collection(*, confirmed=False):
    crs = _crs("EPSG:4326" if confirmed else None, confirmed)
    return {
        "status": "passed", "source": {"file": "landing.xlsx"}, "crs": crs, "count": 2,
        "items": [
            {"reference_site_id": "RLS-1", "name": "起降点1", "site_type": "起降点",
             "coordinate": [122.10, 30.10], "quality": "parsed",
             "source": {"file": "landing.xlsx", "row": 2}, "crs": deepcopy(crs)},
            {"reference_site_id": "RLS-2", "name": "起降点2", "site_type": "起降点",
             "coordinate": [122.40, 30.40], "quality": "parsed",
             "source": {"file": "landing.xlsx", "row": 3}, "crs": deepcopy(crs)},
        ],
    }


def _tower_collection(*, confirmed=True):
    return {
        "status": "passed", "source": {"file": "towers.xlsx"},
        "data_source": "towers.xlsx", "crs": _crs("EPSG:4490", confirmed), "count": 2,
        "items": [
            {"tower_id": "T-001", "name": "一号塔", "longitude": 122.20, "latitude": 30.20,
             "site_type": "地面塔", "source": {"file": "towers.xlsx", "row": 2}},
            {"tower_id": "T-002", "name": "二号塔", "longitude": 122.30, "latitude": 30.30,
             "site_type": "楼面塔", "source": {"file": "towers.xlsx", "row": 3}},
        ],
    }


def _existing_collection():
    return {"status": "passed", "count": 3, "items": [
        {"facility_id": "F-REAL", "name": "已确认真实设施", "coordinate": [122.25, 30.25],
         "site_type": "既有通信站", "devices": [{"device_id": "D-1"}],
         "metadata": {"real_world_facility": True, "dataset_class": "real_operation"},
         "planning_profile": {"confirmed": True}},
        {"facility_id": "F-SYNTH", "name": "合成设施", "coordinate": [122.26, 30.26],
         "metadata": {"dataset_class": SYNTHETIC_DATASET_CLASSES[0],
                      "real_world_facility": False},
         "planning_profile": {"confirmed": True}},
        {"facility_id": "F-UNCONFIRMED", "name": "未确认设施", "coordinate": [122.27, 30.27],
         "metadata": {"real_world_facility": True}, "planning_profile": {}},
    ]}


def _shared_snapshot(*, landing_confirmed=True, session_nodes=None):
    session = SimpleNamespace(state={"nodes": session_nodes if session_nodes is not None else []})
    service = CNSSharedSiteCandidateService(
        session,
        lambda: deepcopy(_landing_collection(confirmed=landing_confirmed)),
        lambda: deepcopy(_tower_collection()),
        lambda: deepcopy(_existing_collection()),
    )
    return service.snapshot()


def _terrain_sampler(elevations):
    """最小确定性地表正高采样器（真实链路由 FABDEM 适配器提供）。"""

    def sampler(points):
        return {
            str(point["key"]): {
                "status": "passed", "elevation_m": elevations.get(str(point["key"])),
                "vertical_reference": "egm2008_orthometric", "reason": None,
            }
            for point in points
        }
    return sampler


def _installation_evidence(source="user_confirmed", *, confirmed=True, height=12.0):
    return {"installation_height_m": height, "source": source, "confirmed": confirmed}


# ------------------------------------------------------- 1. qualifed landing site


def test_radar_accepts_qualified_registered_landing_site_with_evidenced_origin():
    snapshot = _shared_snapshot(landing_confirmed=True)
    result = radar_candidate_sites(
        shared_index=snapshot,
        installation_evidence={
            "RLS-1": _installation_evidence(), "RLS-2": _installation_evidence(height=20.0),
        },
        terrain_sampler=_terrain_sampler({"RLS-1": 5.0, "RLS-2": 8.0}),
    )
    landing = next(item for item in result["usable"]
                   if item["site_id"] == "RLS-1" and item["candidate_kind"] == "reference_landing_site")
    assert landing["installation_position"] == INSTALLATION_POSITION_GROUND_SITE
    assert landing["radar_candidate"] is True
    assert landing["radar_origin_egm2008_m"] == 17.0  # 5.0 地表 + 12.0 显式安装高度
    assert landing["service_qualification_status"] == "not_evaluated"
    assert landing["device_installed"] is False
    # 同一批站址里铁塔仍在，且语义是不同的安装位置。
    tower = next(item for item in result["usable"] if item["site_id"] == "T-001")
    assert tower["installation_position"] == INSTALLATION_POSITION_TOWER_TOP
    assert result["statistics"]["ground_site_inherits_tower_top_height"] is False


# ------------------------------------------------------- 2. towers are not lost


def test_tower_candidates_and_tower_top_model_are_preserved():
    snapshot = _shared_snapshot()
    resolved = resolve_radar_origins(
        towers=_tower_collection()["items"], obstacle_profiles={"items": {
            "T-001": {"status": "resolved", "tower_top_orthometric_m": 55.0},
            "T-002": {"status": "resolved", "tower_top_orthometric_m": 61.0},
        }},
        mount_assumption=None,
    )
    assert resolved["by_tower"]["T-001"]["origin_egm2008_m"] == 55.0
    assert resolved["by_tower"]["T-001"]["origin_source"] == "tower_top_orthometric_m"
    assert resolved["by_tower"]["T-001"]["origin_basis"] == "tower_top_orthometric_m"
    assert resolved["by_tower"]["T-001"]["installation_position"] == "tower_top"
    assert resolved["unresolved_count"] == 0
    result = radar_site_inputs(
        shared_snapshot=snapshot, terrain_sampler=_terrain_sampler({}),
        obstacle_profiles={"items": {
            "T-001": {"tower_top_orthometric_m": 55.0},
            "T-002": {"tower_top_orthometric_m": 61.0},
        }},
    )
    tower_ids = [item["site_id"] for item in result["usable"]
                 if item["installation_position"] == INSTALLATION_POSITION_TOWER_TOP]
    assert tower_ids == ["T-001", "T-002"]
    assert result["resolved"]["by_tower"]["T-002"]["origin_egm2008_m"] == 61.0


def test_tower_without_strict_top_still_enters_radar_candidates_via_planning_estimate():
    """Round38-INT-A：严格塔顶未解析时，**显式 opt-in** 的既有工程估算原点仍可作候选。

    覆盖三条硬边界：① 估算必须在输入适配层就生效（否则 373 座里 296 座被提前排除）；
    ② 估算来源必须逐条标注、且绝不标成已确认塔顶正高；③ 未显式 opt-in 时完全失效。
    """

    snapshot = _shared_snapshot()
    profiles = {"items": {
        "T-001": {"tower_top_orthometric_m": 55.0,
                  "vertical_status": "egm2008_orthometric_resolved"},
        "T-002": {"tower_top_orthometric_m": None, "vertical_status": "building_height_unresolved"},
    }}
    estimates = {"T-002": 47.6}

    opted_in = radar_site_inputs(
        shared_snapshot=snapshot, terrain_sampler=_terrain_sampler({}),
        obstacle_profiles=profiles, planning_origin_estimates=estimates,
    )
    by_id = {item["site_id"]: item for item in opted_in["usable"]}
    assert by_id["T-001"]["origin_source"] == "tower_top_orthometric_m"
    assert by_id["T-001"]["origin_estimate"] is False
    estimated = by_id["T-002"]
    assert estimated["origin_egm2008_m"] == 47.6
    assert estimated["origin_source"] == PLANNING_ESTIMATE_ORIGIN_SOURCE
    assert estimated["origin_estimate"] is True
    # 严格塔顶事实**仍然**是未解析：估算绝不冒充已确认塔顶正高。
    assert opted_in["resolved"]["by_tower"]["T-002"]["tower_top_orthometric_m"] is None
    assert opted_in["resolved"]["by_tower"]["T-002"]["origin_confirmed"] is False
    stats = opted_in["statistics"]
    assert stats["tower_strict_origin_count"] == 1
    assert stats["tower_planning_estimate_origin_count"] == 1
    assert stats["planning_estimate_is_not_confirmed_tower_top"] is True

    # 未显式 opt-in（不传估算）⇒ 该塔退回既有的 fail-closed 行为。
    closed = radar_site_inputs(
        shared_snapshot=snapshot, terrain_sampler=_terrain_sampler({}),
        obstacle_profiles=profiles,
    )
    closed_by_id = {item["site_id"]: item for item in closed["usable"]}
    assert closed_by_id["T-002"]["origin_egm2008_m"] is None
    assert closed_by_id["T-002"]["origin_source"] is None
    assert closed["resolved"]["by_tower"]["T-002"]["origin_reason"].startswith(
        "tower_top_egm2008_unresolved")


def test_planning_estimate_accessor_is_gated_by_the_existing_colocation_policy():
    """估算访问器只认既有的 ``tower_colocation_policy`` 显式 opt-in（无第二套开关）。"""

    base = {
        "tower_colocation_policy": {
            "planning_host_use_confirmed": True,
            "planning_service_origin_policy": "terrain_plus_source_tower_height",
        },
        "tower_colocation_candidates": {
            "items": [
                {"site_id": "tower-colocation:T-A",
                 "vertical_profile": {"planning_origin_estimated": True,
                                      "planning_service_origin_egm2008_m": 41.5},
                 "metadata": {"host": {"host_tower_id": "T-A"}}},
                # 未声明 estimated（严格解析或不可用）⇒ 绝不能被当成估算来源。
                {"site_id": "tower-colocation:T-B",
                 "vertical_profile": {"planning_origin_estimated": False,
                                      "planning_service_origin_egm2008_m": 60.0},
                 "metadata": {"host": {"host_tower_id": "T-B"}}},
            ],
        },
    }
    assert tower_planning_service_origin_estimates(base) == {"T-A": 41.5}

    off = deepcopy(base)
    off["tower_colocation_policy"]["planning_service_origin_policy"] = None
    assert tower_planning_service_origin_estimates(off) == {}

    unconfirmed = deepcopy(base)
    unconfirmed["tower_colocation_policy"]["planning_host_use_confirmed"] = False
    assert tower_planning_service_origin_estimates(unconfirmed) == {}


# ------------------------------------------------------- 3. CRS gate still blocks


def test_unconfirmed_landing_site_crs_keeps_radar_candidate_blocked():
    snapshot = _shared_snapshot(landing_confirmed=False)
    result = radar_candidate_sites(
        shared_index=snapshot,
        installation_evidence={"RLS-1": _installation_evidence()},
        terrain_sampler=_terrain_sampler({"RLS-1": 5.0}),
    )
    blocked = [item for item in result["blocked"]
               if item["candidate_kind"] == "reference_landing_site"]
    assert {item["site_id"] for item in blocked} == {"RLS-1", "RLS-2"}
    assert all(item["block_reason"] == "reference_source_crs_unresolved" for item in blocked)
    assert not [item for item in result["usable"]
                if item["candidate_kind"] == "reference_landing_site"]
    # 铁塔的 CRS 已确认，因此不受起降点 CRS 阻断影响。
    assert {item["site_id"] for item in result["usable"]} >= {"T-001", "T-002"}


# ------------------------------------------------------- 4. never inherits tower top


def test_ground_site_never_inherits_tower_top_height():
    """地面站原点只来自地表正高 + 显式安装高度；缺任一即 blocked，绝不填 0/塔顶。"""

    towers = [{
        "tower_id": "PAD-1", "site_id": "PAD-1", "name": "起降点", "site_type": "起降点",
        "installation_position": INSTALLATION_POSITION_GROUND_SITE,
        "longitude": 122.1, "latitude": 30.1,
    }]
    profiles = {"items": {"PAD-1": {
        "status": "resolved", "tower_top_orthometric_m": 999.0,
        "vertical_status": "egm2008_orthometric_resolved",
    }}}
    #: 既有塔顶档案里有一个 999 m 的塔顶值：地面站**绝不能**读到它。
    no_evidence = resolve_radar_origins(towers=towers, obstacle_profiles=profiles)
    record = no_evidence["by_tower"]["PAD-1"]
    assert record["origin_egm2008_m"] is None
    assert record["tower_top_orthometric_m"] is None
    assert record["inherits_tower_top_orthometric_m"] is False
    assert record["origin_reason"].startswith("ground_site_elevation_unresolved")
    assert no_evidence["status"] == "missing_data"

    #: 只有安装高度、没有地表正高 ⇒ 仍然 blocked（不填 0）。
    elevation_missing = resolve_radar_origins(
        towers=towers, obstacle_profiles=profiles,
        ground_site_installation_heights={"PAD-1": _installation_evidence()},
    )
    assert elevation_missing["by_tower"]["PAD-1"]["origin_egm2008_m"] is None

    #: 只有地表正高、没有显式安装高度 ⇒ 仍然 blocked（不套用 legacy 挂高）。
    with_ground = resolve_radar_origins(
        towers=towers, obstacle_profiles=profiles,
        ground_site_elevations={"PAD-1": {
            "elevation_m": 5.0, "vertical_reference": "egm2008_orthometric", "status": "passed",
        }},
        mount_assumption={"radar_mount_height_m": 30.0, "confirmed": True,
                          "parameter_origin": "user_confirmed"},
    )
    blocked = with_ground["by_tower"]["PAD-1"]
    assert blocked["origin_egm2008_m"] is None
    assert blocked["installation_height_m"] is None
    assert blocked["origin_reason"].startswith("ground_site_installation_height_unresolved")

    #: 两项证据都在 ⇒ 原点 = 地表正高 + 安装高度（不是塔顶 999 m）。
    resolved = resolve_radar_origins(
        towers=towers, obstacle_profiles=profiles,
        ground_site_elevations={"PAD-1": {
            "elevation_m": 5.0, "vertical_reference": "egm2008_orthometric", "status": "passed",
        }},
        ground_site_installation_heights={"PAD-1": _installation_evidence(height=15.0)},
    )
    assert resolved["by_tower"]["PAD-1"]["origin_egm2008_m"] == 20.0
    assert resolved["by_tower"]["PAD-1"]["origin_basis"] == "ground_site_antenna_installation"
    assert resolved["legacy_mount_height_used"] is False

    #: 非 orthometric 基准的地表高程不得被当成正高使用（no unknown != pass）。
    wrong_reference = resolve_radar_origins(
        towers=towers, obstacle_profiles=profiles,
        ground_site_elevations={"PAD-1": {
            "elevation_m": 5.0, "vertical_reference": "ellipsoidal_wgs84", "status": "passed",
        }},
        ground_site_installation_heights={"PAD-1": _installation_evidence()},
    )
    assert wrong_reference["by_tower"]["PAD-1"]["origin_egm2008_m"] is None


def test_engineering_assumption_is_not_installation_evidence():
    assert installation_height_evidence(
        {"installation_height_m": 30.0, "source": "engineering_assumption", "confirmed": True},
        site_id="S1",
    )[0] is None
    assert installation_height_evidence(
        {"installation_height_m": 30.0, "source": "user_confirmed", "confirmed": False},
        site_id="S1",
    )[0] is None
    assert installation_height_evidence(None, site_id="S1")[0] is None
    assert installation_height_evidence(
        {"installation_height_m": 30.0, "source": "user_confirmed", "confirmed": True},
        site_id="S1",
    ) == (30.0, None)


# ------------------------------------------------------- 5. authenticity gate


def test_synthetic_and_unconfirmed_existing_cns_facilities_cannot_bypass_the_gate():
    snapshot = _shared_snapshot()
    facility_ids = {item["source_id"] for item in snapshot["candidates"]
                    if item["candidate_kind"] == "existing_cns_facility"}
    assert facility_ids == {"F-REAL"}
    gate = snapshot["existing_cns_source_gate"]
    assert gate["source_policy"] == "real_world_confirmed_only"
    assert gate["excluded_counts"] == {
        "synthetic_existing_cns_facility": 1, "unconfirmed_existing_cns_facility": 1,
    }
    real = next(item for item in snapshot["candidates"] if item["source_id"] == "F-REAL")
    assert real["existing_installation"]["device_ids"] == ["D-1"]
    assert all(
        value["qualification_status"] != "qualified"
        for value in real["service_applicability"].values()
    )
    #: 未显式确认的设施不会因为"没有确认记录"被静默当成已确认。
    assert "existing-cns-facility:F-UNCONFIRMED" not in {
        item["candidate_id"] for item in snapshot["candidates"]
    }
    assert "existing-cns-facility:F-SYNTH" not in {
        item["candidate_id"] for item in snapshot["candidates"]
    }


def test_shared_projection_is_read_only_and_never_invents_sites():
    session = SimpleNamespace(state={"nodes": [], "towers": {"items": []}})
    service = CNSSharedSiteCandidateService(
        session, lambda: deepcopy(_landing_collection(confirmed=True)),
        lambda: deepcopy(_tower_collection()), lambda: deepcopy(_existing_collection()),
    )
    before = deepcopy(session.state)
    snapshot = service.snapshot()
    assert session.state == before
    #: 只有真实登记来源：2 起降点 + 2 铁塔 + 1 真实设施，没有任何合成塔。
    assert snapshot["counts"]["total"] == 5
    assert snapshot["counts"]["towers"] == 2
    assert all(item["candidate_semantics"] == "potential_host_not_installed_or_qualified"
               for item in snapshot["candidates"])


# ------------------------------------------------------- 6. radar algorithm intact


def test_radar_directional_coverage_and_los_wiring_are_not_replaced_by_circles():
    """SITE-2 只改输入适配层：候选生成仍是定向单面阵 + 地形 LOS 通道。"""

    import inspect

    from cns_planner.algorithms.radar_layout import candidates as radar_candidates
    from cns_planner.algorithms.radar_layout import geometry as radar_geometry

    build_source = inspect.getsource(radar_candidates.build_candidates)
    #: 仍走既有候选生成链（tower × radar_type → bearing 派生方向），没有圆形半径替代。
    assert "candidates_for_tower_and_type" in build_source
    assert "coverage_radius" not in build_source
    candidate_source = inspect.getsource(radar_candidates.candidates_for_tower_and_type)
    assert "physically_reachable" in candidate_source
    assert "terrain_los_lookup" in candidate_source
    geometry_source = inspect.getsource(radar_geometry)
    assert "azimuth_half_width_deg" in geometry_source
    assert "elevation_min_deg" in geometry_source and "elevation_max_deg" in geometry_source
    #: 输入适配层只做站址身份/证据门禁，不产生任何覆盖几何。
    for source in (inspect.getsource(radar_candidate_sites),
                   inspect.getsource(radar_site_inputs)):
        for forbidden in ("radius", "circle", "azimuth", "beamwidth"):
            assert forbidden not in source.lower(), forbidden


def test_radar_service_admits_landing_sites_into_the_same_candidate_input():
    """服务接缝：注入统一站址后，铁塔与起降点进入同一个 Radar 候选站址输入。"""

    from cns_planner.application.radar_surveillance_layout_service import (
        RadarSurveillanceLayoutService,
    )

    nodes = [{
        "node_id": "N001", "name": "已登记起降点", "coordinate": [122.10, 30.10],
        "coordinate_source": "reference_site", "reference_site_id": "RLS-1",
        #: 站址自身登记的显式安装证据（缺失即 unknown ⇒ 地面站不产生候选）。
        "radar_installation": _installation_evidence(height=12.0),
    }]
    session = SimpleNamespace(
        state={
            "nodes": nodes,
            "tower_obstacle_profiles": {"items": {
                "T-001": {"status": "resolved", "tower_top_orthometric_m": 55.0},
            }},
        },
        save=lambda: None,
    )
    shared = CNSSharedSiteCandidateService(
        session, lambda: deepcopy(_landing_collection(confirmed=True)),
        lambda: deepcopy(_tower_collection()), lambda: deepcopy(_existing_collection()),
    )
    service = RadarSurveillanceLayoutService(session, None, lambda: {}, None, shared)
    service.facts_provider = {
        "sample_terrain": _terrain_sampler({"N001": 5.0}),
    }
    before = deepcopy(session.state)
    inputs = service._radar_site_inputs({"radar_mount_height": None})
    assert session.state == before, "候选站址接入必须是只读的"
    by_tower = inputs["resolved"]["by_tower"]
    assert by_tower["T-001"]["origin_egm2008_m"] == 55.0
    assert by_tower["T-001"]["installation_position"] == INSTALLATION_POSITION_TOWER_TOP
    assert by_tower["N001"]["origin_egm2008_m"] == 17.0
    assert by_tower["N001"]["installation_position"] == INSTALLATION_POSITION_GROUND_SITE
    assert by_tower["N001"]["origin_basis"] == "ground_site_antenna_installation"
    assert by_tower["N001"]["inherits_tower_top_orthometric_m"] is False
    disclosure = service._radar_site_source_disclosure(True)
    assert disclosure["candidate_site_source"] == "shared_site_candidates"
    assert disclosure["ground_site_inherits_tower_top_height"] is False
    assert disclosure["candidate_does_not_imply_installation"] is True
    #: 未提供显式安装证据 / 地表证据的站址保持 blocked（不以 0 或塔顶兜底）。
    no_evidence = [
        item for item in inputs["blocked"]
        if str(item.get("block_reason") or "").startswith(
            ("ground_site_radar_installation_height_unresolved",
             "ground_site_terrain_elevation_unresolved")
        )
    ]
    assert no_evidence, [item.get("block_reason") for item in inputs["blocked"]]
    assert all(item.get("radar_origin_egm2008_m") is None for item in no_evidence)


def test_candidate_site_set_enters_the_radar_input_fingerprint():
    """站址集合或资格一变，Radar 输入指纹必须变（旧方案 stale）。"""

    from cns_planner.application.radar_shared_site_adapter import (
        candidate_site_fingerprint_component,
    )

    blocked = candidate_site_fingerprint_component(_shared_snapshot(landing_confirmed=False))
    confirmed = candidate_site_fingerprint_component(_shared_snapshot(landing_confirmed=True))
    assert blocked["source"] == "shared_site_candidates"
    assert blocked["count"] == confirmed["count"] == 5
    assert blocked["fingerprint"] != confirmed["fingerprint"]
    #: 同一输入必须给出同一指纹（确定性）。
    assert candidate_site_fingerprint_component(_shared_snapshot()) == (
        candidate_site_fingerprint_component(_shared_snapshot())
    )
    unavailable = candidate_site_fingerprint_component(None)
    assert unavailable["fingerprint"] is None
    assert unavailable["count"] == 0


# ------------------------------------------------------- 7. per-service suitability
def test_services_keep_their_own_applicability_and_do_not_share_coverage():
    snapshot = _shared_snapshot()
    for candidate in snapshot["candidates"]:
        applicability = candidate["service_applicability"]
        assert set(applicability) == {"C", "N", "RID", "Radar", "RTK"}
        for value in applicability.values():
            assert value["status"] in {"not_evaluated", "unqualified"}
            assert value["installation_status"] in {"not_evaluated", "unqualified"}
    #: 候选站址不是已安装设备，也不携带任何覆盖半径。
    assert all("coverage" not in candidate for candidate in snapshot["candidates"])
    #: P16 的注册站址门禁仍按来源身份筛选（起降点走节点身份，设施走真实性门禁）。
    from cns_planner.application.corridor_site_planning_service import (
        registered_p16_site_sources,
    )
    from cns_planner.application.cns_facility_assembler import SYNTHETIC_DATASET_CLASSES as ASSEMBLER
    assert tuple(ASSEMBLER) == tuple(SYNTHETIC_DATASET_CLASSES)
    sources = registered_p16_site_sources(
        snapshot,
        existing_facilities=_existing_collection(),
        candidate_sites={"items": [{"site_id": "RLS-1"}]},
        tower_colocation={"items": [{"tower_id": "T-001"}]},
    )
    assert sources["statistics"]["source_policy"] == (
        "registered_landing_sites_real_towers_confirmed_existing_cns_only"
    )
    assert sources["statistics"]["allowed_source_counts"] == {
        "existing_cns_facilities": 1, "registered_landing_sites": 0, "real_tower_sites": 1,
    }
