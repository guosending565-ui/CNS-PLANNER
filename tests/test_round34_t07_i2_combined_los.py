"""Round34-T07-I2：Terrain + Building 联合 LOS 只读诊断。

覆盖 Application 编排（有界预算、路径只读、失败诚实降级、明细分页、stale 判定）
与只读边界（无 ProjectState writer、生产 ``enforced`` 仍禁用）。

两个真实数据源都被替换成本地 stub：本文件**不**打开 FABDEM 或 GPKG，真实双源诊断
由 ``tools/round34_t07_i2_real_combined_los.py`` 在同一只读边界内另行证明。
"""

from __future__ import annotations

import copy
import threading
from pathlib import Path

import pytest

from cns_planner.application.radar_terrain_los_diagnostics_service import (
    COMBINED_PROBE_BUDGET_LIMIT,
    DEFAULT_COMBINED_PROBE_BUDGET,
    RadarTerrainLosDiagnosticsService,
)
from cns_planner.domain import radar_building_los as building_los_domain
from cns_planner.domain import radar_combined_los_evidence as combined_evidence
from cns_planner.gis import radar_building_los_adapter as building_adapter
from cns_planner.gis import radar_terrain_los_adapter as terrain_adapter

TERRAIN_PATH = r"D:\fixture\zhoushan_fabdem_dtm_30m.tif"
BUILDINGS_PATH = r"D:\fixture\zhoushan_buildings_v3.gpkg"
ROUTE_ID = "R-T07I2"


# --------------------------------------------------------------------------- fixtures


class FakeTransform:
    """把站址经纬度映射到与 fixture ``metric_path`` 同一米制网格。"""

    metric_crs = "EPSG:32651"
    method = "fixture_offset_metric"

    @staticmethod
    def to_metric(point):
        return [(float(point[0]) - 122.198) * 100000.0, (float(point[1]) - 30.0) * 100000.0]

    @staticmethod
    def to_geographic(point):
        return [(float(point[0]) / 100000.0) + 122.198, (float(point[1]) / 100000.0) + 30.0]


class FakeRadarLayoutService:
    """只读依赖替身：提供显式米制变换与数据源路径，**不**读任何文件。"""

    def __init__(self, paths):
        self.metric_transform = FakeTransform()
        self._paths = dict(paths)

    def _source_paths(self, *, facts_provider=None):
        return dict(self._paths)


class FakeSession:
    def __init__(self, state):
        self.state = state
        self.save_count = 0

    def save(self, *args, **kwargs):  # pragma: no cover - 只读服务绝不调用它
        self.save_count += 1
        raise AssertionError("只读联合诊断绝不能保存 ProjectState")


class DummyTerrainSource:
    role = "terrain_dtm"
    vertical_reference = "EGM2008"

    def __init__(self, path):
        self.path = path


class FakeBuildingSource:
    def __init__(self, *, identity=None, spatial_index=True, describe_fails=False,
                 identity_empty=False):
        self._identity = identity or {
            "role": "buildings", "file_name": "zhoushan_buildings_v3.gpkg",
            "layer": "buildings", "spatial_index_available": spatial_index,
            "source_version": "round35_buildings_v3", "crs": "EPSG:4326",
        }
        self._describe_fails = describe_fails
        self._identity_empty = identity_empty
        self.queries = 0

    def describe(self):
        if self._describe_fails:
            raise RuntimeError("provider 无法自证身份")
        if self._identity_empty:
            return {}
        return dict(self._identity)


def state(*, towers=True, buildings_path=BUILDINGS_PATH, terrain_path=TERRAIN_PATH,
          altitude_layers=True, routes=True):
    paths = {}
    if terrain_path:
        paths["terrain_dtm"] = terrain_path
    if buildings_path:
        paths["buildings"] = buildings_path
    return {
        "data_source_paths": paths,
        "towers": {"items": (
            [{"tower_id": "T1", "longitude": 122.20, "latitude": 30.00}] if towers else []
        )},
        "tower_obstacle_profiles": {"items": (
            {"T1": {"tower_top_orthometric_m": 30.0, "status": "passed"}} if towers else {}
        )},
        "radar_surveillance_policy": {"optimization_sample_spacing_m": 25.0},
        "spatial_3d": {"altitude_layers": (
            [{"altitude_layer_id": "ALT-080", "nominal_altitude_m": 80.0,
              "vertical_reference": "EGM2008"}] if altitude_layers else []
        )},
        "operational_routes": (
            [{"route_id": ROUTE_ID, "status": "passed",
              "altitude_layer_id": "ALT-080",
              "metric_path": [[400.0, 0.0], [1200.0, 0.0], [2000.0, 0.0]]}] if routes else []
        ),
    }


def service(*, state_value=None, building_source=None):
    session = FakeSession(state_value if state_value is not None else state())
    radar = FakeRadarLayoutService(session.state.get("data_source_paths") or {})
    source = building_source if building_source is not None else FakeBuildingSource()
    instance = RadarTerrainLosDiagnosticsService(
        session, radar, None,
        terrain_source_factory=lambda path, variant=None: DummyTerrainSource(path),
        building_source_factory=lambda path, metric_crs=None: source,
    )
    return instance, session, source


def _terrain_result(status="clear", *, sample_count=24, fingerprint="terrain-fp-1"):
    unknown = status == "unknown"
    return {
        "status": status,
        "reason": "terrain_los_evidence_unknown" if unknown else None,
        "algorithm_version": "radar_terrain_los_v1_straight_ray_sampled_profile",
        "input_fingerprint": fingerprint,
        "computation_completion": "complete",
        #: 结论 unknown 时真实适配层不会声称证据完整。
        "evidence_completion": "incomplete" if unknown else "complete",
        "terrain_sample_count": sample_count,
        "terrain_profile": {
            "samples": [], "source": {"role": "terrain_dtm", "file_name": "fabdem.tif"},
            "read_receipt": {"open_count": 1},
        },
        "probe_height": {"resolved_altitude_m": 80.0},
        "horizontal_position": {"metric": [900.0, 0.0], "longitude": 122.209, "latitude": 30.0},
        "radar_origin_metric": [200.0, 0.0],
    }


def _building_hit(*, level="verified", building_id="B1"):
    return {
        "building_id": building_id, "footprint_part_index": 0,
        "hit_x": 500.0, "hit_y": 0.0, "hit_z": 20.0,
        "along_ray_distance_m": 500.0, "ray_fraction": 0.5,
        "ground_elevation_egm2008_m": 0.0, "roof_elevation_egm2008_m": 20.0,
        "height_evidence_level": level, "obstruction_margin_m": 6.0,
        "evidence_classification": "confirmed" if level != "predicted" else "predicted",
        "source_version": "fixture-v1", "hit_derivation_status": "fixture",
    }


def _building_result(status="blocked", *, level="verified", hits=None, reason=None,
                     fingerprint="building-fp-1"):
    hits = [_building_hit(level=level)] if hits is None and status == "blocked" else (hits or [])
    return {
        "status": status, "reason": reason,
        "algorithm_version": building_los_domain.RADAR_BUILDING_LOS_ALGORITHM_VERSION,
        "input_fingerprint": fingerprint,
        "candidate_building_count": len(hits), "exact_intersection_count": len(hits),
        "height_evidence_counts": ({level: len(hits)} if hits else {}),
        "obstruction_hits": hits,
        "computation_completion": "complete", "evidence_completion": "complete",
        "within_frozen_device_range": True,
        "building_los_physical_unknown": status == "unknown",
        "query_receipt": {"candidate_count": len(hits), "returned_count": len(hits)},
    }


@pytest.fixture
def stubs(monkeypatch):
    """把两个真实域替换成本地 stub，同时保留真实的联合代数与查询编排。"""

    calls = {"terrain": 0, "building": 0}
    config = {
        "terrain_status": "clear", "terrain_fingerprint": "terrain-fp-1",
        "building_status": "blocked", "building_level": "verified",
        "building_fingerprint": "building-fp-1",
        "origin_availability": "usable",
    }

    def fake_physical(records, *, terrain_source, point_terrain_source=None):
        return {
            str(item.get("tower_id")): {
                "origin_availability": config["origin_availability"],
                "origin_egm2008_m": item.get("origin_egm2008_m"),
                "terrain_m": 5.0,
            }
            for item in records
        }

    def fake_terrain(tower, probe, **kwargs):
        del tower, probe, kwargs
        calls["terrain"] += 1
        # 原点不可评价 / 证据不足时真实适配层 fail-closed 返回 unknown，这里保持同一语义。
        if config["origin_availability"] != "usable":
            return _terrain_result("unknown", fingerprint=config["terrain_fingerprint"],
                                   sample_count=0)
        return _terrain_result(
            config["terrain_status"], fingerprint=config["terrain_fingerprint"],
            sample_count=24 if config["terrain_status"] != "unknown" else 0,
        )

    def fake_building(tower, probe, **kwargs):
        del tower, probe, kwargs
        calls["building"] += 1
        return _building_result(
            config["building_status"], level=config["building_level"],
            fingerprint=config["building_fingerprint"],
        )

    monkeypatch.setattr(terrain_adapter, "radar_origin_terrain_physical_inputs", fake_physical)
    monkeypatch.setattr(terrain_adapter, "evaluate_probe_terrain_los", fake_terrain)
    monkeypatch.setattr(terrain_adapter, "terrain_source_identity", lambda source: {
        "role": "terrain_dtm", "file_name": "fabshan_fabdem.tif", "variant": "native",
    })
    monkeypatch.setattr(building_adapter, "evaluate_probe_building_los", fake_building)
    return {"config": config, "calls": calls}


# --------------------------------------------------------------------------- status


def test_combined_status_is_lightweight_and_opens_nothing():
    instance, _session, _source = service()
    status = instance.combined_status()
    assert status["evidence_status"] == "not_requested"
    assert status["available_to_compute"] is True
    assert status["open_panel_never_computes_los"] is True
    assert status["path_parameters_never_accepted"] is True
    assert status["route_consumption"]["operational_authorization"] is False
    availability = status["adapter_availability"]
    assert availability["terrain_dtm"]["opened"] is False
    assert availability["buildings"]["opened"] is False
    assert availability["buildings"]["spatial_index_checked"] is False
    assert availability["metric_transform"]["opened"] is False
    assert status["detail_available"] is False
    # 只读声明必须与 T07-B 同口径。
    assert status["has_project_state_writer"] is False
    assert status["writes_project_state"] is False
    assert status["participates_in_milp"] is False


def test_engineering_demo_route_requires_explicit_mode_and_stays_read_only():
    state_value = state(routes=False)
    state_value["layered_operational_adoptions"] = {"items": [{
        "adoption_id": "LRA-DEMO", "route_id": "R0005",
        "status": "published_engineering_demo", "current_applicability": "current",
        "strict_safety_validation_passed": False,
        "engineering_conditional_acceptance": True,
        "use_scope": "engineering_demo_only", "operational_authorization": False,
        "published_to_operational_routes": False,
        "limitations": ["工程条件接受，严格验证未通过，仅供演示"],
        "altitude_layer_id": "ALT-080",
        "after": {"route": {
            "route_id": "R0005", "status": "engineering_demo_only",
            "altitude_layer_id": "ALT-080",
            "metric_path": [[400.0, 0.0], [1200.0, 0.0], [2000.0, 0.0]],
        }},
    }]}
    instance, session, _source = service(state_value=state_value)
    strict = instance.combined_status()
    assert strict["available_to_compute"] is False
    assert strict["route_consumption"]["mode"] == "strict_operational"
    demo = instance.combined_status(engineering_demo_mode=True)
    assert demo["available_to_compute"] is True
    assert demo["route_consumption"]["mode"] == "engineering_demo_read_only"
    assert demo["route_consumption"]["operational_authorization"] is False
    assert demo["route_consumption"]["published_to_operational_routes"] is False
    assert demo["route_consumption"]["limitations"] == [
        "工程条件接受，严格验证未通过，仅供演示"
    ]
    assert session.save_count == 0


def test_combined_status_fails_closed_without_each_source():
    without_buildings, _s, _src = service(state_value=state(buildings_path=None))
    status = without_buildings.combined_status()
    assert status["available_to_compute"] is False
    assert status["adapter_availability"]["buildings"]["reason"] == "buildings_not_configured"
    without_terrain, _s2, _src2 = service(state_value=state(terrain_path=None))
    assert without_terrain.combined_status()["available_to_compute"] is False


def test_combined_status_fails_closed_without_authoritative_altitude_layer():
    instance, _session, _source = service(state_value=state(altitude_layers=False))
    status = instance.combined_status()
    assert status["available_to_compute"] is False
    altitude = status["adapter_availability"]["altitude_layer"]
    assert altitude["reason"] == "altitude_layer_not_configured"
    assert altitude["altitude_layer_id"] is None
    assert altitude["nominal_altitude_m"] is None


def test_catalog_only_alt_080_is_not_authority_and_never_opens_sources(stubs):
    del stubs
    instance, _session, _source = service(state_value=state(routes=False))
    status = instance.combined_status()
    assert status["available_to_compute"] is False
    assert status["available_with_explicit_altitude_layer"] is True
    assert status["adapter_availability"]["altitude_layer"]["reason"] == (
        "altitude_layer_not_configured"
    )
    failed = instance.diagnose_combined({})
    assert failed["failure_reason"] == "altitude_layer_not_configured"
    assert failed["dem_open_count"] == 0
    assert failed["building_open_count"] == 0


def test_diagnose_combined_rejects_altitude_layer_before_opening_sources(stubs):
    del stubs
    state_value = state(altitude_layers=False)
    session = FakeSession(state_value)
    radar = FakeRadarLayoutService(state_value["data_source_paths"])
    opens = {"terrain": 0, "buildings": 0}

    def terrain_factory(path, variant=None):
        del variant
        opens["terrain"] += 1
        return DummyTerrainSource(path)

    def building_factory(path, metric_crs=None):
        del path, metric_crs
        opens["buildings"] += 1
        return FakeBuildingSource()

    instance = RadarTerrainLosDiagnosticsService(
        session, radar, None,
        terrain_source_factory=terrain_factory,
        building_source_factory=building_factory,
    )
    evidence = instance.diagnose_combined({})
    assert evidence["failure_reason"] == "altitude_layer_not_configured"
    assert evidence["request_id"]
    assert opens == {"terrain": 0, "buildings": 0}


def test_diagnose_combined_rejects_missing_and_ambiguous_layers_before_io(stubs):
    del stubs
    for payload, mutate, expected in (
        ({"altitude_layer_id": "ALT-999"}, lambda value: None,
         "altitude_layer_not_configured"),
        ({}, lambda value: value["operational_routes"].append({
            "route_id": "R-OTHER", "status": "passed", "altitude_layer_id": "ALT-100",
            "metric_path": [[0.0, 0.0], [100.0, 0.0]],
        }), "altitude_layer_ambiguous"),
    ):
        value = state()
        value["operational_routes"][0]["altitude_layer_id"] = "ALT-080"
        mutate(value)
        instance, _session, _source = service(state_value=value)
        evidence = instance.diagnose_combined(payload)
        assert evidence["failure_reason"] == expected
        assert evidence["dem_open_count"] == 0
        assert evidence["building_open_count"] == 0


def test_failed_after_opening_sources_reports_real_open_counts():
    """打开真实数据源之后才失败时，执行收据必须如实上报，绝不伪报 0。

    ``building_source_spatial_index_missing`` 发生在 DEM 与 buildings GPKG
    **都已打开**之后；收据若仍是 ``0 / 0``，前端"数据源打开次数"就会虚假陈述
    实际发生过的读取行为（违反"缺失不等于 0"）。
    """

    instance, _session, _source = service(
        building_source=FakeBuildingSource(spatial_index=False),
    )
    evidence = instance.diagnose_combined({})
    assert evidence["evidence_status"] == "failed"
    assert evidence["failure_reason"] == "building_source_spatial_index_missing"
    assert evidence["dem_open_count"] >= 1
    assert evidence["building_open_count"] == 1


def test_building_open_receipt_requires_proof_not_object_construction():
    """建筑打开次数必须由**收据**证明，不能只凭"访问器对象构造成功"。

    生产 ``QgisBuildingLosSource.__init__`` 会打开 GPKG 并校验图层/字段，因此两者通常
    等价；但把等价关系写成类型假设后，任何"构造成功却没真正取证"的实现都会让收据
    变成一句无法核实的话。这里固定：取证失败（``describe()`` 抛错）或身份为空时，
    收据必须如实返回"未知"（``None``），既不是 0 也不是 1。
    """

    for source, expected in (
        (FakeBuildingSource(spatial_index=False, describe_fails=True), None),
        (FakeBuildingSource(spatial_index=False, identity_empty=True), None),
        (FakeBuildingSource(spatial_index=False), 1),
    ):
        instance, _session, _probe = service(building_source=source)
        evidence = instance.diagnose_combined({})
        assert evidence["evidence_status"] == "failed"
        #: 取证不到时字段**缺失**（前端渲染为"—"），而不是被静默写成 0。
        assert evidence.get("building_open_count") == expected, (
            f"buildings 收据期望 {expected!r}，实际 {evidence.get('building_open_count')!r}"
        )
        if expected is None:
            assert "building_open_count" not in evidence
        # DEM 侧始终来自 TerrainSourceCache 的真实计数，与建筑取证无关。
        assert evidence["dem_open_count"] >= 1
        assert "source_access_attempted" not in evidence, "内部收据标记不得泄漏进响应"


def test_diagnose_combined_accepts_explicit_current_authoritative_layer(stubs):
    del stubs
    instance, _session, _source = service()
    evidence = instance.diagnose_combined({"altitude_layer_id": "ALT-080"})
    assert evidence["evidence_status"] == "computed"
    assert evidence["altitude_layer"]["altitude_layer_id"] == "ALT-080"
    assert evidence["altitude_layer"]["nominal_altitude_m"] == 80.0


# --------------------------------------------------------------------------- diagnose


def test_diagnose_combined_requires_both_real_sources(stubs):
    del stubs
    for state_value, reason in (
        (state(terrain_path=None), "terrain_dtm_not_configured"),
        (state(buildings_path=None), "buildings_not_configured"),
    ):
        instance, _session, _source = service(state_value=state_value)
        evidence = instance.diagnose_combined({})
        assert evidence["evidence_status"] == "failed"
        assert evidence["failure_reason"] == reason
        assert evidence["rays"] == []
        assert evidence["failure_never_rendered_as_clear"] is True
        assert evidence["unknown_treated_as_clear"] is False


def test_diagnose_combined_ignores_caller_supplied_paths(stubs):
    instance, _session, source = service()
    evidence = instance.diagnose_combined({
        "buildings_path": r"D:\attacker\evil.gpkg",
        "terrain_path": r"D:\attacker\evil.tif",
        "path": r"D:\attacker",
    })
    assert evidence["evidence_status"] == "computed"
    provenance = evidence["source_provenance"]
    assert provenance["buildings_file_name"] == "zhoushan_buildings_v3.gpkg"
    assert provenance["terrain_dtm_file_name"] == "zhoushan_fabdem_dtm_30m.tif"
    assert set(provenance["ignored_path_parameters"]) >= {
        "buildings_path", "terrain_path", "path",
    }
    assert source.queries == 0  # 建筑源 stub 自己从不被直接查询


def test_diagnose_combined_produces_bounded_read_only_evidence(stubs):
    instance, session, _source = service()
    evidence = instance.diagnose_combined({})
    assert evidence["evidence_status"] == "computed"
    assert evidence["adapter_id"] == combined_evidence.COMBINED_LOS_EVIDENCE_ADAPTER_ID
    assert evidence["authoritative_project_result"] is False
    assert evidence["production_enforced_enabled"] is False
    assert evidence["strict_and_engineering_estimate_separate"] is True
    assert evidence["unknown_fail_closed"] is True
    assert evidence["full_terrain_profile_included"] is False
    assert evidence["evaluated_tower_count"] == 1
    assert evidence["candidate_tower_count"] == 1
    assert evidence["evaluated_ray_count"] <= DEFAULT_COMBINED_PROBE_BUDGET
    aggregate = evidence["aggregate"]
    assert aggregate["combined_los_status_counts"]["blocked"] == evidence["evaluated_ray_count"]
    assert aggregate["terrain_los_status_counts"]["clear"] == evidence["evaluated_ray_count"]
    assert aggregate["building_los_status_counts"]["blocked"] == evidence["evaluated_ray_count"]
    # 语义输入身份与执行收据必须分离。
    assert evidence["input_fingerprint"]
    receipt = evidence["execution_receipt"]
    assert receipt["execution_receipt_excluded_from_input_fingerprint"] is True
    assert receipt["elapsed_seconds_is_receipt_only"] is True
    assert receipt["bounds"]["probe_budget"] == DEFAULT_COMBINED_PROBE_BUDGET
    assert evidence["dem_open_count"] >= 1
    assert evidence["building_open_count"] == 1
    # 只读：绝不写 ProjectState。
    assert session.save_count == 0


def test_combined_evidence_separates_trustworthy_and_predictive_obstruction(stubs):
    instance, _session, _source = service()
    stubs["config"]["building_level"] = "predicted"
    predicted = instance.diagnose_combined({})
    record = predicted["rays"][0]
    assert record["predictive_obstruction"] is True
    assert record["trustworthy_obstruction"] is False
    assert record["predictive_obstruction_count"] == 1
    assert record["trustworthy_and_predictive_never_merged"] is True
    assert predicted["aggregate"]["predictive_obstruction_ray_count"] == (
        predicted["evaluated_ray_count"]
    )
    assert predicted["aggregate"]["trustworthy_obstruction_ray_count"] == 0

    stubs["config"]["building_level"] = "source_tagged"
    trustworthy = instance.diagnose_combined({})
    record = trustworthy["rays"][0]
    assert record["trustworthy_obstruction"] is True
    assert record["predictive_obstruction"] is False
    assert trustworthy["aggregate"]["trustworthy_obstruction_ray_count"] == (
        trustworthy["evaluated_ray_count"]
    )


def test_unknown_terrain_keeps_combined_unknown_and_never_clear(stubs):
    instance, _session, _source = service()
    stubs["config"]["terrain_status"] = "unknown"
    stubs["config"]["building_status"] = "unknown"
    evidence = instance.diagnose_combined({})
    counts = evidence["aggregate"]["combined_los_status_counts"]
    assert counts["clear"] == 0
    assert counts["blocked"] == 0
    assert counts["unknown"] == evidence["evaluated_ray_count"]
    assert evidence["aggregate"]["unknown_combination_count"] == evidence["evaluated_ray_count"]
    for record in evidence["rays"]:
        assert record["unknown_never_treated_as_clear"] is True
        assert record["combined_los_status"] == "unknown"


def test_credible_building_block_answers_the_ray_without_hiding_incomplete_evidence(stubs):
    """一个域的可信阻断足以回答"这条射线是否被遮挡"，但**不**等于证据完整。"""

    instance, _session, _source = service()
    stubs["config"]["terrain_status"] = "unknown"
    stubs["config"]["building_status"] = "blocked"
    evidence = instance.diagnose_combined({})
    assert evidence["aggregate"]["combined_los_status_counts"]["blocked"] == (
        evidence["evaluated_ray_count"]
    )
    for record in evidence["rays"]:
        assert record["credible_block_is_sufficient_for_direct_ray_block"] is True
        assert record["verdict_is_orthogonal_to_completion"] is True
        assert record["other_domain_completion_preserved"] is True
        assert record["evidence_completion"] != "complete"


def test_missing_origin_evidence_is_not_treated_as_a_clear_ray(stubs):
    instance, _session, _source = service()
    stubs["config"]["origin_availability"] = "not_evaluable"
    evidence = instance.diagnose_combined({})
    assert evidence["evidence_status"] == "computed"
    assert evidence["aggregate"]["combined_los_status_counts"]["clear"] == 0


def test_probe_budget_is_bounded_and_truncation_is_disclosed(stubs):
    del stubs
    instance, _session, _source = service()
    evidence = instance.diagnose_combined({"probe_budget": 2, "max_tower_count": 1})
    assert evidence["evaluated_ray_count"] <= 2
    assert evidence["truncation"]["probe_budget_per_tower"] >= 1
    assert evidence["bounds"]["probe_budget"] == 2
    oversized = instance.diagnose_combined({"probe_budget": 100000})
    assert oversized["bounds"]["probe_budget"] <= COMBINED_PROBE_BUDGET_LIMIT


def test_tower_filter_that_matches_nothing_fails_closed(stubs):
    del stubs
    instance, _session, _source = service()
    evidence = instance.diagnose_combined({"tower_ids": ["T-DOES-NOT-EXIST"]})
    assert evidence["evidence_status"] == "failed"
    assert evidence["failure_reason"] == "tower_filter_matched_nothing"


def test_building_source_without_spatial_index_is_refused(stubs):
    del stubs
    instance, _session, _source = service(
        building_source=FakeBuildingSource(spatial_index=False),
    )
    evidence = instance.diagnose_combined({})
    assert evidence["evidence_status"] == "failed"
    assert evidence["failure_reason"] == "building_source_spatial_index_missing"


def test_default_building_source_factory_uses_the_real_qgis_source(monkeypatch):
    """生产路径必须真正引用 buildings v3 的只读 QGIS 源（拼错名字要在这里失败）。"""

    created = {}

    class FakeQgisSource:
        def __init__(self, path, *, metric_crs=None):
            created["path"] = str(path)
            created["metric_crs"] = metric_crs

        def describe(self):
            return {"spatial_index_available": True}

    #: 属性必须**已经存在**：monkeypatch 会在名字拼错时直接失败。
    monkeypatch.setattr(building_adapter, "QgisBuildingLosSource", FakeQgisSource)
    instance, _session, _source = service()
    instance.building_source_factory = None
    built = instance._building_source(BUILDINGS_PATH, metric_crs="EPSG:32651")
    assert isinstance(built, FakeQgisSource)
    assert created == {"path": BUILDINGS_PATH, "metric_crs": "EPSG:32651"}


def test_geometry_filtered_ray_is_not_physical_unknown(stubs):
    """设备冻结几何排除的射线必须报 geometry_filtered，而不是建筑物理 unknown。"""
    del stubs
    state_value = state()
    # 100 m / 75 m 均小于 Radar-I 最小斜距 120 m，且站址原点在 (200, 0)。
    state_value["operational_routes"] = [{
        "route_id": ROUTE_ID, "status": "passed",
        "altitude_layer_id": "ALT-080",
        "metric_path": [[100.0, 0.0], [140.0, 0.0]],
    }]
    short, _session, _source = service(state_value=state_value)
    evidence = short.diagnose_combined({"probe_budget": 4})
    assert evidence["evidence_status"] == "computed"
    assert evidence["evaluated_ray_count"] >= 1
    assert evidence["aggregate"]["geometry_filtered_ray_count"] >= 1
    for record in evidence["rays"]:
        assert record["building_domain_state"] == "geometry_filtered"
        assert record["geometry_filtered_is_not_physical_unknown"] is True
        assert record["building_los_physical_unknown"] is not True
        assert record["combined_los_status"] != "clear"


# --------------------------------------------------------------------------- details


def test_combined_details_paginate_without_recomputing(stubs):
    instance, _session, _source = service()
    evidence = instance.diagnose_combined({"probe_budget": 4})
    before = stubs["calls"]["terrain"], stubs["calls"]["building"]
    identity = {key: evidence[key] for key in (
        "request_id", "input_fingerprint", "generation"
    )}
    page = instance.combined_details({**identity, "limit": 2})
    assert page["recomputed"] is False
    assert page["detail_page"]["limit"] == 2
    assert page["detail_page"]["returned_record_count"] <= 2
    assert page["detail_page"]["matched_record_count"] == evidence["evaluated_ray_count"]
    assert page["input_fingerprint"] == evidence["input_fingerprint"]
    assert (stubs["calls"]["terrain"], stubs["calls"]["building"]) == before
    oversized = instance.combined_details({**identity, "limit": 100000})
    assert oversized["detail_page"]["limit"] <= 50


def test_combined_details_without_evidence_never_triggers_computation(stubs):
    instance, _session, _source = service()
    page = instance.combined_details({})
    assert page["evidence_status"] == "failed"
    assert page["failure_reason"] == "detail_not_available"
    assert page["detail_page"] is None
    assert stubs["calls"]["terrain"] == 0
    assert stubs["calls"]["building"] == 0


def test_combined_details_requires_complete_diagnosis_identity(stubs):
    del stubs
    instance, _session, _source = service()
    evidence = instance.diagnose_combined({"probe_budget": 4})
    for incomplete in (
        {},
        {"request_id": evidence["request_id"]},
        {"request_id": evidence["request_id"],
         "input_fingerprint": evidence["input_fingerprint"]},
    ):
        page = instance.combined_details(incomplete)
        assert page["failure_reason"] == "detail_not_available"
        assert page["detail_page"] is None


def test_success_then_failure_invalidates_current_details(stubs):
    del stubs
    instance, _session, _source = service()
    success = instance.diagnose_combined({"probe_budget": 4})
    success_identity = {key: success[key] for key in (
        "request_id", "input_fingerprint", "generation"
    )}
    assert instance.combined_details(success_identity)["detail_page"]
    instance.radar_layout_service._paths.pop("terrain_dtm")
    failed = instance.diagnose_combined({})
    assert failed["evidence_status"] == "failed"
    assert failed["request_id"] != success["request_id"]
    details = instance.combined_details({
        "request_id": failed["request_id"], "generation": failed["generation"],
        "input_fingerprint": failed.get("input_fingerprint"),
    })
    assert details["failure_reason"] == "detail_not_available"
    assert details["detail_page"] is None
    assert instance.combined_status()["evidence_status"] == "failed"


def test_failure_then_success_restores_details(stubs):
    del stubs
    instance, _session, _source = service(state_value=state(terrain_path=None))
    failed = instance.diagnose_combined({})
    assert failed["evidence_status"] == "failed"
    instance.radar_layout_service._paths["terrain_dtm"] = TERRAIN_PATH
    success = instance.diagnose_combined({"probe_budget": 4})
    details = instance.combined_details({key: success[key] for key in (
        "request_id", "input_fingerprint", "generation"
    )})
    assert success["evidence_status"] == "computed"
    assert details["input_fingerprint"] == success["input_fingerprint"]
    assert details["request_id"] == success["request_id"]


def test_older_backend_request_cannot_overwrite_newer_failure(stubs, monkeypatch):
    del stubs
    instance, _session, _source = service()
    old_started = threading.Event()
    release_old = threading.Event()
    old_result = {}

    def fake_diagnose(payload, *, started):
        del started
        if payload.get("marker") == "old":
            old_started.set()
            assert release_old.wait(timeout=2)
            evidence = combined_evidence.failure_status("placeholder")
            evidence.update({
                "evidence_status": "computed", "input_fingerprint": "old-fingerprint",
                "failure_reason": None, "_detail_records": [{"tower_id": "T1"}],
            })
            return evidence
        return combined_evidence.failure_status("newer_request_failed")

    monkeypatch.setattr(instance, "_diagnose_combined", fake_diagnose)

    def run_old():
        old_result.update(instance.diagnose_combined({"marker": "old", "request_id": "old"}))

    thread = threading.Thread(target=run_old)
    thread.start()
    assert old_started.wait(timeout=2)
    newer = instance.diagnose_combined({"marker": "new", "request_id": "new"})
    release_old.set()
    thread.join(timeout=2)
    assert newer["failure_reason"] == "newer_request_failed"
    assert old_result["superseded_by_newer_request"] is True
    status = instance.combined_status()
    assert status["request_id"] == "new"
    assert status["evidence_status"] == "failed"
    assert instance.combined_details({
        "request_id": "new", "input_fingerprint": "missing", "generation": 2,
    })["detail_page"] is None


def test_combined_detail_filters_are_applied(stubs):
    del stubs
    instance, _session, _source = service()
    evidence = instance.diagnose_combined({"probe_budget": 4})
    identity = {key: evidence[key] for key in (
        "request_id", "input_fingerprint", "generation"
    )}
    missed = instance.combined_details({**identity, "tower_id": "T-OTHER"})
    assert missed["detail_page"]["matched_record_count"] == 0
    hit = instance.combined_details({**identity, "tower_id": "T1"})
    assert hit["detail_page"]["matched_record_count"] >= 1


# --------------------------------------------------------------------------- stale


def test_prior_evidence_with_other_fingerprint_is_explicitly_stale(stubs):
    del stubs
    instance, _session, _source = service()
    fresh = instance.diagnose_combined({})
    stale = instance.diagnose_combined({
        "prior_evidence": {
            "input_fingerprint": "deadbeef",
            "schema_version": fresh["schema_version"],
            "algorithm_version": fresh["algorithm_version"],
            "fingerprint_schema_version": fresh["fingerprint_schema_version"],
            "evaluation_scope": fresh["evaluation_scope"],
        },
    })
    assert stale["currentness"]["stale"] is True
    assert stale["evidence_status"] == "stale"
    assert stale["currentness"]["reasons"] == ["combined_los_inputs_changed"]
    assert stale["currentness"]["upstream_planning_state_untouched"] is True


def test_matching_prior_evidence_stays_current(stubs):
    del stubs
    instance, _session, _source = service()
    first = instance.diagnose_combined({})
    second = instance.diagnose_combined({"prior_evidence": {
        "input_fingerprint": first["input_fingerprint"],
        "schema_version": first["schema_version"],
        "algorithm_version": first["algorithm_version"],
        "fingerprint_schema_version": first["fingerprint_schema_version"],
        "evaluation_scope": first["evaluation_scope"],
    }})
    assert second["currentness"]["state"] == "current"
    assert second["stale"] is False
    assert second["input_fingerprint"] == first["input_fingerprint"]


def test_no_prior_evidence_never_claims_stale(stubs):
    del stubs
    instance, _session, _source = service()
    evidence = instance.diagnose_combined({})
    assert evidence["currentness"]["state"] == "no_prior_evidence"
    assert evidence["currentness"]["stale"] is False


# --------------------------------------------------------------------------- identity


def test_input_fingerprint_ignores_budgets_and_execution_receipts():
    common = {
        "radar_type": "radar_i",
        "altitude_layer": {"altitude_layer_id": "ALT-080", "nominal_altitude_m": 80.0},
        "terrain_source_identity": {"file_name": "fabdem.tif", "size_bytes": 1},
        "building_source_identity": {"file_name": "buildings_v3.gpkg", "size_bytes": 2},
        "tower_identities": [{"tower_id": "T1", "origin_egm2008_m": 30.0}],
        "probe_identities": [{"probe_id": "R:0", "metric": [200.0, 0.0]}],
        "ray_input_fingerprints": ["ray-a", "ray-b"],
        "horizontal_crs": "EPSG:32651",
    }
    base = combined_evidence.combined_los_input_fingerprint(**common)
    other_budget = combined_evidence.combined_los_input_fingerprint(**common)
    assert base == other_budget
    changed_source = combined_evidence.combined_los_input_fingerprint(
        **{**common, "building_source_identity": {"file_name": "other.gpkg"}},
    )
    assert changed_source != base
    changed_probe = combined_evidence.combined_los_input_fingerprint(
        **{**common, "probe_identities": [{"probe_id": "R:25", "metric": [225.0, 0.0]}]},
    )
    assert changed_probe != base


def test_input_fingerprint_normalizes_unordered_filters_only():
    common = {
        "tower_filter": ["T2", "T1", "T2"],
        "route_filter": ["R2", "R1", "R2"],
        "probe_identities": [
            {"probe_id": "R:0", "metric": [0.0, 0.0]},
            {"probe_id": "R:1", "metric": [1.0, 0.0]},
        ],
    }
    reordered_filters = combined_evidence.combined_los_input_fingerprint(
        **{**common, "tower_filter": ["T1", "T2"], "route_filter": ["R1", "R2"]},
    )
    assert combined_evidence.combined_los_input_fingerprint(**common) == reordered_filters
    reversed_route_points = combined_evidence.combined_los_input_fingerprint(
        **{**common, "probe_identities": list(reversed(common["probe_identities"]))},
    )
    assert reversed_route_points != reordered_filters


def test_input_fingerprint_normalizes_tower_identity_set_but_not_identity_changes():
    towers = [
        {"tower_id": "T2", "origin_egm2008_m": 40.0},
        {"tower_id": "T1", "origin_egm2008_m": 30.0},
    ]
    base = combined_evidence.combined_los_input_fingerprint(tower_identities=towers)
    assert base == combined_evidence.combined_los_input_fingerprint(
        tower_identities=list(reversed(towers)),
    )
    assert base != combined_evidence.combined_los_input_fingerprint(
        tower_identities=[towers[0], {**towers[1], "origin_egm2008_m": 31.0}],
    )


@pytest.mark.parametrize(("field", "old_value", "reason"), (
    ("schema_version", "old_schema", "combined_evidence_schema_changed"),
    ("algorithm_version", "old_algorithm", "combined_algorithm_changed"),
    ("fingerprint_schema_version", "old_fingerprint", "input_fingerprint_schema_changed"),
    ("evaluation_scope", "old_scope", "evaluation_scope_changed"),
))
def test_currentness_compares_semantic_versions_independently(field, old_value, reason):
    prior = {
        "input_fingerprint": "x",
        "schema_version": combined_evidence.COMBINED_LOS_EVIDENCE_SCHEMA_VERSION,
        "algorithm_version": combined_evidence.RADAR_COMBINED_VISIBILITY_VERSION,
        "fingerprint_schema_version": combined_evidence.COMBINED_LOS_FINGERPRINT_SCHEMA_VERSION,
        "evaluation_scope": combined_evidence.EVALUATION_SCOPE_COMBINED,
    }
    prior[field] = old_value
    currentness = combined_evidence.combined_los_currentness(
        prior,
        current_input_fingerprint="y",
    )
    assert currentness["state"] == "stale"
    assert currentness["reasons"] == [reason]


def test_currentness_with_missing_contract_fields_is_not_comparable():
    currentness = combined_evidence.combined_los_currentness(
        {"input_fingerprint": "x"}, current_input_fingerprint="y",
    )
    assert currentness["state"] == "not_comparable"
    assert currentness["stale"] is False
    assert currentness["comparable"] is False


def test_read_only_declaration_matches_terrain_los_boundary():
    declaration = combined_evidence.READ_ONLY_DECLARATION
    for key in (
        "writes_project_state", "writes_artifacts", "has_project_state_writer",
        "participates_in_milp", "participates_in_required_cns",
        "participates_in_p14_p15_p16", "stales_upstream_planning_state",
        "unknown_treated_as_clear", "is_detection_probability",
    ):
        assert declaration[key] is False, key
    assert declaration["read_only"] is True


def test_diagnose_combined_does_not_mutate_project_state(stubs):
    """只读：诊断**绝不**改动任何 ProjectState 字段，也不调用 save。"""

    instance, session, _source = service()
    before = copy.deepcopy(session.state)
    instance.diagnose_combined({})
    instance.combined_status()
    instance.combined_details({"limit": 5})
    assert session.state == before
    assert session.save_count == 0
    assert not hasattr(instance, "invalidation_service")
    assert not hasattr(instance, "artifact_writer")
    del stubs


def test_disabled_terrain_los_mode_never_yields_clear_or_blocked():
    """disabled parity：生产默认（LOS 关闭）时任何射线都不得得到 clear/blocked。"""

    from cns_planner.algorithms.radar_layout.terrain_los_lookup import (
        BUILDING_MODE_DISABLED, TERRAIN_LOS_MODE_DISABLED, TerrainLosLookup,
    )

    lookup = TerrainLosLookup(
        mode=TERRAIN_LOS_MODE_DISABLED, evaluator=lambda **kwargs: {"status": "clear"},
        building_mode=BUILDING_MODE_DISABLED, max_ray_count=4,
    )
    result = lookup.evaluate(
        {"tower_id": "T1", "metric": [0.0, 0.0], "origin_egm2008_m": 30.0},
        {"sample_id": "S1", "metric": [400.0, 0.0], "egm2008_m": 80.0},
        "radar_i",
    )
    assert result["status"] not in ("clear", "blocked")
    assert result["status"] == TERRAIN_LOS_MODE_DISABLED


# ------------------------------------------------------- workflow / router wiring

DEFAULTS_PATH = Path("cns_planner/config/defaults.json")


def _workflow(tmp_path):
    from cns_planner.application.workflow_service import WorkflowService

    return WorkflowService(tmp_path / "project.json", DEFAULTS_PATH)


def test_workflow_combined_status_is_lightweight_and_wired(tmp_path):
    workflow = _workflow(tmp_path)
    status = workflow.radar_combined_los_status()
    assert status["evidence_status"] == "not_requested"
    assert status["available_to_compute"] is False
    assert status["open_panel_never_computes_los"] is True
    assert status["path_parameters_never_accepted"] is True
    assert status["adapter_availability"]["terrain_dtm"]["opened"] is False
    assert status["adapter_availability"]["buildings"]["opened"] is False
    assert status["writes_project_state"] is False
    assert status["participates_in_milp"] is False
    assert status["schema_version"] == "radar_combined_los_evidence_v1"


def test_workflow_combined_diagnose_fails_closed_without_configuration(tmp_path):
    workflow = _workflow(tmp_path)
    result = workflow.diagnose_radar_combined_los({})
    assert result["evidence_status"] == "failed"
    assert result["failure_reason"] in (
        "terrain_dtm_not_configured", "buildings_not_configured",
        "metric_projector_not_injected", "radar_origin_inventory_empty",
    )
    assert result["rays"] == []
    assert result["failure_never_rendered_as_clear"] is True


def test_workflow_combined_details_without_diagnosis_never_computes(tmp_path):
    workflow = _workflow(tmp_path)
    result = workflow.radar_combined_los_details({"limit": 5})
    assert result["evidence_status"] == "failed"
    assert result["failure_reason"] == "detail_not_available"
    assert result["detail_page"] is None
    assert result["recomputed"] is False


class _RecordingWorkflow:
    """把三个联合入口替换成记录器；其它方法由真实 WorkflowService 提供。

    ``ApiRouter`` 在构造分派表时会触及大量 workflow 方法，因此这里必须建立在真实
    workflow 之上，只 monkeypatch 本次接线关心的三个入口。
    """

    def __init__(self, workflow, calls):
        self._workflow = workflow
        self.calls = calls

    def __getattr__(self, name):
        return getattr(self._workflow, name)

    def radar_combined_los_status(self):
        self.calls.append(("status", None))
        return {"evidence_status": "not_requested", "probe": "status"}

    def diagnose_radar_combined_los(self, payload=None):
        self.calls.append(("diagnose", payload))
        return {"evidence_status": "computed", "probe": "diagnose"}

    def radar_combined_los_details(self, payload=None):
        self.calls.append(("details", payload))
        return {"detail_page": None, "probe": "details"}


def _api_router(workflow):
    from cns_planner.api.router import ApiRouter

    class Context:
        data = None
        qgis = None
        static = Path("cns_planner/web")
        workflow = None

    Context.workflow = workflow
    return ApiRouter(Context())


def test_router_wires_the_combined_endpoints(tmp_path):
    workflow = _RecordingWorkflow(_workflow(tmp_path), [])
    router = _api_router(workflow)
    calls = workflow.calls

    status = router.get("/api/radar-terrain-los/combined/status", {}, {})
    assert status.data["probe"] == "status"

    diagnosed = router.post(
        "/api/radar-terrain-los/combined/diagnose",
        {"probe_budget": 4, "tower_ids": ["T1"]},
    )
    assert diagnosed.data["probe"] == "diagnose"
    assert calls[-1] == ("diagnose", {"probe_budget": 4, "tower_ids": ["T1"]})

    page = router.get(
        "/api/radar-terrain-los/combined/details",
        {"limit": ["5"], "combined_los_status": ["blocked"],
         "request_id": ["request-7"], "input_fingerprint": ["fingerprint-7"],
         "generation": ["7"]}, {},
    )
    assert page.data["probe"] == "details"
    assert calls[-1][1] == {
        "tower_id": None, "route_id": None, "combined_los_status": "blocked",
        "building_domain_state": None, "offset": None, "limit": "5",
        "request_id": "request-7", "input_fingerprint": "fingerprint-7",
        "generation": "7",
    }


def test_router_keeps_the_terrain_only_endpoints_wired(tmp_path):
    """additive：T07-B 的既有端点必须仍然可达，不被联合入口替换。"""

    router = _api_router(_workflow(tmp_path))
    status = router.get("/api/radar-terrain-los/status", {}, {})
    assert status.data["evidence_status"] == "not_requested"
    diagnosed = router.post("/api/radar-terrain-los/diagnose", {})
    assert diagnosed.data["evidence_status"] == "failed"
    assert diagnosed.data["failure_reason"] in (
        "terrain_dtm_not_configured", "metric_projector_not_injected",
        "radar_origin_inventory_empty",
    )
    details = router.get("/api/radar-terrain-los/details", {"limit": ["5"]}, {})
    assert details.data["failure_reason"] == "detail_not_available"
