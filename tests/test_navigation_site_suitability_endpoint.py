"""Round D：``POST /api/navigation-site-suitability`` 的最小写入口契约测试。

覆盖：
 1. 三类站址来源（existing_cns_facility / candidate_site / tower_colocation_host）
    都能写入既有站址条目的 ``metadata.navigation_site_suitability``；
 2. 派生结论（``eligible_for_navigation_reference_station``）**不**持久化；
 3. ``navigation_site_suitability = null`` 是"显式清除"，不是"不合格"；
 4. 非法来源 / 不存在的站址 fail-closed（ValueError），绝不静默新建站址；
 5. 写入只走既有链路：normalization → invalidation → session.save；
 6. 不新增第二套站址容器（state 键集合不变）；
 7. 共塔候选重新派生时会**过继**铁塔上的 suitability（不丢用户已确认事实）；
 8. router 里登记了该端点，且没有任何"任意 session JSON 路径"入口。
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from cns_planner.application.cns_input_service import CNSInputService
from cns_planner.domain.cns_inputs import (
    normalize_candidate_site,
    normalize_existing_facility,
)
from cns_planner.domain.navigation_augmentation import (
    collect_navigation_sites,
    normalize_navigation_site_suitability,
)
from cns_planner.domain.tower_colocation import (
    build_tower_colocation_candidates,
    normalize_tower_colocation_policy,
)
from cns_planner.services.invalidation import DEPENDENTS, ResultLedger


class _Session:
    def __init__(self, state):
        self.state = state
        self.saves = 0

    def save(self):
        self.saves += 1


class _Invalidation:
    def __init__(self):
        self.calls = []

    def workflow(self, changed):
        self.calls.append(changed)


def _service(state):
    session = _Session(state)
    invalidation = _Invalidation()
    service = CNSInputService(session, invalidation, adapter=None, snapshot=lambda: {})
    return service, session, invalidation


def _state():
    return {
        "existing_cns_facilities": {
            "items": [{
                "facility_id": "NAV-1", "site_id": "NAV-1", "name": "已有导航站",
                "coordinate": [122.2, 30.0], "devices": [],
            }],
        },
        "candidate_sites": {
            "items": [{
                "site_id": "CAND-1", "name": "候选站址", "coordinate": [122.25, 30.0],
                "available_subsystems": ["N"],
            }],
        },
        "towers": {
            "items": [{
                "tower_id": "TT-1", "name": "真实铁塔", "coordinate": [122.3, 30.0],
                "longitude": 122.3, "latitude": 30.0, "site_type": "tower",
            }],
        },
    }


SUITABILITY = {
    "confirmed": True,
    "planning_use_confirmed": True,
    "open_sky_confirmed": True,
    "surveyed_coordinate_confirmed": False,
    "stable_mount_confirmed": None,
    "backhaul_available": True,
    "reference_station_installed": False,
    "source": "site_survey_2026",
    "notes": "现场勘察记录",
}


def test_existing_facility_suitability_lands_on_the_existing_container():
    state = _state()
    service, session, invalidation = _service(state)
    before_keys = set(state)
    service.set_navigation_site_suitability({
        "site_source": "existing_cns_facility",
        "facility_id": "NAV-1",
        "navigation_site_suitability": SUITABILITY,
    })
    entry = state["existing_cns_facilities"]["items"][0]
    stored = entry["metadata"]["navigation_site_suitability"]
    assert stored["confirmed"] is True
    assert stored["planning_use_confirmed"] is True
    assert stored["reference_station_installed"] is False
    assert stored["source"] == "site_survey_2026"
    # 派生结论绝不持久化：它必须每次由 fail-closed 规则重新判定
    assert "eligible_for_navigation_reference_station" not in stored
    # 不新增第二套站址容器
    assert set(state) == before_keys
    # 走既有链路：invalidations + session.save
    assert invalidation.calls == ["navigation_site_suitability"]
    assert session.saves == 1


def test_candidate_site_and_tower_sources_are_supported():
    state = _state()
    service, _, _ = _service(state)
    service.set_navigation_site_suitability({
        "site_source": "existing_cns_facility", "facility_id": "NAV-1",
        "navigation_site_suitability": SUITABILITY,
    })
    service.set_navigation_site_suitability({
        "site_source": "candidate_site", "site_id": "CAND-1",
        "navigation_site_suitability": SUITABILITY,
    })
    service.set_navigation_site_suitability({
        "site_source": "tower_colocation_host", "tower_id": "TT-1",
        "navigation_site_suitability": SUITABILITY,
    })
    assert state["candidate_sites"]["items"][0]["metadata"][
        "navigation_site_suitability"]["confirmed"] is True
    assert state["towers"]["items"][0]["metadata"][
        "navigation_site_suitability"]["confirmed"] is True
    # 写入后立刻可被 canonical navigation 证据消费（共塔来源经既有派生进入候选）
    colocation = build_tower_colocation_candidates(
        state["towers"]["items"],
        obstacle_profiles={"items": {}},
        policy=normalize_tower_colocation_policy({"planning_host_use_confirmed": True}),
    )
    providers, _, candidates = collect_navigation_sites(
        existing_facilities=state["existing_cns_facilities"],
        candidate_sites=state["candidate_sites"],
        tower_colocation=colocation,
    )
    assert {item["planning_origin"] for item in providers + candidates} == {
        "existing_cns_facility", "candidate_site", "tower_colocation_host",
    }


def test_null_clears_the_declaration_instead_of_marking_ineligible():
    state = _state()
    service, _, _ = _service(state)
    service.set_navigation_site_suitability({
        "site_source": "candidate_site", "site_id": "CAND-1",
        "navigation_site_suitability": SUITABILITY,
    })
    service.set_navigation_site_suitability({
        "site_source": "candidate_site", "site_id": "CAND-1",
        "navigation_site_suitability": None,
    })
    entry = state["candidate_sites"]["items"][0]
    assert "navigation_site_suitability" not in (entry.get("metadata") or {})
    # 清除后回到"无证据"（既不是 eligible 也不是 ineligible）
    assert normalize_navigation_site_suitability(None) is None
    providers, _, candidates = collect_navigation_sites(
        candidate_sites=state["candidate_sites"],
    )
    assert providers == [] and candidates == []


def test_unknown_source_or_missing_site_fails_closed():
    state = _state()
    service, session, invalidation = _service(state)
    with pytest.raises(ValueError):
        service.set_navigation_site_suitability({
            "site_source": "somewhere_else", "site_id": "NAV-1",
            "navigation_site_suitability": SUITABILITY,
        })
    with pytest.raises(ValueError):
        service.set_navigation_site_suitability({
            "site_source": "existing_cns_facility", "facility_id": "NOPE",
            "navigation_site_suitability": SUITABILITY,
        })
    with pytest.raises(ValueError):
        service.set_navigation_site_suitability({
            "site_source": "existing_cns_facility", "facility_id": "NAV-1",
            "navigation_site_suitability": "not-an-object",
        })
    # 失败路径不写入、不失效、不保存
    assert session.saves == 0
    assert invalidation.calls == []
    assert "navigation_site_suitability" not in (
        state["existing_cns_facilities"]["items"][0].get("metadata") or {})


def test_suitability_survives_canonical_normalization_round_trip():
    state = _state()
    service, _, _ = _service(state)
    service.set_navigation_site_suitability({
        "site_source": "existing_cns_facility", "facility_id": "NAV-1",
        "navigation_site_suitability": SUITABILITY,
    })
    entry = state["existing_cns_facilities"]["items"][0]
    # ensure_catalogs / reopen 会重跑既有 normalization：metadata 必须原样保留
    normalized = normalize_existing_facility(entry, 0)
    assert normalized["metadata"]["navigation_site_suitability"]["confirmed"] is True
    candidate = normalize_candidate_site(
        state["candidate_sites"]["items"][0] if False else {
            "site_id": "CAND-2", "coordinate": [122.26, 30.0],
            "available_subsystems": ["N"],
            "metadata": {"navigation_site_suitability": deepcopy(SUITABILITY)},
        }, 0,
    )
    assert candidate["metadata"]["navigation_site_suitability"]["confirmed"] is True


def test_tower_colocation_derivation_inherits_declared_suitability():
    state = _state()
    service, _, _ = _service(state)
    service.set_navigation_site_suitability({
        "site_source": "tower_colocation_host", "tower_id": "TT-1",
        "navigation_site_suitability": {
            **SUITABILITY, "reference_station_installed": True,
        },
    })
    colocation = build_tower_colocation_candidates(
        state["towers"]["items"],
        obstacle_profiles={"items": {}},
        policy=normalize_tower_colocation_policy({"planning_host_use_confirmed": True}),
    )
    item = colocation["items"][0]
    # 重新派生共塔候选时绝不丢用户已确认的 suitability
    assert item["metadata"]["navigation_site_suitability"]["confirmed"] is True
    assert item["metadata"]["navigation_site_suitability"][
        "reference_station_installed"] is True
    providers, _, _ = collect_navigation_sites(tower_colocation=colocation)
    assert len(providers) == 1
    assert providers[0]["reference_station_installed"] is True


def test_suitability_change_stales_only_the_service_chain():
    # 依赖表：站址适用性只让 P14 → P15 → P16 与报告过时
    assert "navigation_site_suitability" in DEPENDENTS
    dependents = DEPENDENTS["navigation_site_suitability"]
    assert "cns_corridor_assessment" in dependents
    assert "cns_corridor_gap_assessment" in dependents
    assert "cns_corridor_site_plan" in dependents
    assert "report" in dependents
    for untouched in ("routes", "environment_risk", "technical_risk", "radar_surveillance_layout"):
        assert untouched not in dependents
    ledger = ResultLedger()
    ledger.statuses["cns_corridor_assessment"] = "passed"
    ledger.statuses["cns_corridor_gap_assessment"] = "passed"
    ledger.statuses["cns_corridor_site_plan"] = "passed"
    ledger.statuses["radar_surveillance_layout"] = "passed"
    affected = ledger.invalidate("navigation_site_suitability")
    assert "cns_corridor_assessment" in affected
    assert ledger.statuses["radar_surveillance_layout"] == "passed"


def test_router_registers_the_endpoint_and_no_free_form_path_writer():
    source = Path("cns_planner/api/router.py").read_text(encoding="utf-8")
    assert '"/api/navigation-site-suitability"' in source
    assert 'workflow.set_navigation_site_suitability(payload)' in source
    # 没有"由客户端指定 session JSON 路径"的通用写入口
    for forbidden in ('set_session_path', 'session_path', 'json_pointer', 'patch_state'):
        assert forbidden not in source
