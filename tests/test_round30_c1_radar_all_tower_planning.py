from copy import deepcopy

from cns_planner.application.radar_surveillance_layout_service import (
    normalize_radar_surveillance_policy,
)
from cns_planner.domain.radar_surveillance_layout import (
    RADAR_PLANNING_ORIGIN_POLICY_ID,
    RADAR_TYPE_I,
    radar_geometry_parameters,
)
from cns_planner.gis.radar_layout_adapter import resolve_radar_origins


def _towers(count=373):
    return [
        {
            "tower_id": f"T{index:03d}",
            "longitude": 122.0 + index / 10000,
            "latitude": 30.0,
            "site_type": "ground" if index % 2 else "rooftop",
            "elevation_m": float(index),
            "height_m": 10.0 + (index % 5),
            "source": {"file_name": "航路航线规划-铁塔数据.xlsx"},
        }
        for index in range(count)
    ]


def test_all_373_towers_get_radar_planning_origins_without_changing_obstacle_profiles():
    towers = _towers()
    obstacle_profiles = {
        "status": "partial",
        "resolved_count": 77,
        "unresolved_count": 296,
        "items": {
            tower["tower_id"]: {
                "status": "resolved" if index < 77 else "unresolved",
                "tower_top_orthometric_m": 999.0 if index < 77 else None,
                "vertical_status": "resolved" if index < 77 else "building_height_unresolved",
            }
            for index, tower in enumerate(towers)
        },
    }
    before = deepcopy(obstacle_profiles)

    origins = resolve_radar_origins(
        towers=towers,
        obstacle_profiles=obstacle_profiles,
        planning_policy_id=RADAR_PLANNING_ORIGIN_POLICY_ID,
    )

    assert len(origins["records"]) == 373
    assert origins["unresolved_count"] == 0
    assert all(item["origin_egm2008_m"] is not None for item in origins["records"])
    assert obstacle_profiles == before
    assert obstacle_profiles["resolved_count"] == 77
    assert obstacle_profiles["unresolved_count"] == 296


def test_planning_origin_is_source_elevation_plus_tower_height_and_never_confirmed():
    tower = _towers(1)[0]
    tower.update({"site_type": "ground", "elevation_m": 23.5, "height_m": 31.0})
    result = resolve_radar_origins(
        towers=[tower],
        obstacle_profiles={"items": {tower["tower_id"]: {"status": "unresolved"}}},
        planning_policy_id=RADAR_PLANNING_ORIGIN_POLICY_ID,
    )
    origin = result["records"][0]

    assert origin["origin_egm2008_m"] == 54.5
    assert origin["source_elevation_m"] == 23.5
    assert origin["tower_height_m"] == 31.0
    assert origin["planning_site_type"] == "rooftop"
    assert origin["origin_status"] == "planning_estimate"
    assert origin["origin_method"] == "source_elevation_plus_tower_height"
    assert origin["assumption"] == "all_towers_treated_as_rooftop"
    assert origin["engineering_confirmed"] is False
    assert origin["origin_confirmed"] is False
    assert result["engineering_confirmed"] is False


def test_formal_radar_i_constraints_remain_frozen():
    policy = normalize_radar_surveillance_policy({
        "radar_planning_origin_policy_id": RADAR_PLANNING_ORIGIN_POLICY_ID,
    })
    radar_i = radar_geometry_parameters(RADAR_TYPE_I)

    assert policy["radar_planning_origin_policy_id"] == RADAR_PLANNING_ORIGIN_POLICY_ID
    assert policy["radar_origin_basis"] == "source_elevation_m_plus_tower_height_m"
    assert "unconfirmed_source_elevation_m" in policy["radar_origin_semantics"]
    assert policy["allowed_radar_types"] == ["radar_i"]
    assert policy["allow_automatic_radar_ii_escalation"] is False
    assert policy["allow_mixed_radar_types"] is False
    assert policy["allow_range_relaxation"] is False
    assert policy["max_panels_per_tower"] == 4
    assert radar_i["max_slant_range_m"] == 3000.0
    assert radar_i["azimuth_beamwidth_deg"] == 90.0
