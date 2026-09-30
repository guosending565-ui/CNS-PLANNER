import json
import math

import pytest

from cns_planner.domain.closed_loop import stable_fingerprint
from cns_planner.domain.cns_inputs import normalize_required_cns
from cns_planner.domain.cns_service_contract import (
    KNOWN_SERVICE_KEYS,
    SERVICE_KEY_COMMUNICATION,
    SERVICE_KEY_NAVIGATION,
    SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION,
    SERVICE_KEY_RADAR_NONCOOPERATIVE,
    SERVICE_KEY_RID_COOPERATIVE,
    SERVICE_KEY_SURVEILLANCE,
    SERVICE_SURFACE_POLICY,
    validated_service_key,
)
from cns_planner.domain.cns_service_registry import (
    planner_family_for,
    required_services_for,
    service_registry_entry,
    service_requirement_for,
)


EXPECTED_KEYS = {
    SERVICE_KEY_COMMUNICATION,
    SERVICE_KEY_NAVIGATION,
    SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION,
    SERVICE_KEY_SURVEILLANCE,
    SERVICE_KEY_RID_COOPERATIVE,
    SERVICE_KEY_RADAR_NONCOOPERATIVE,
}


def base_requirements():
    return {
        "communication": {"required": False},
        "navigation": {"required": False},
        "surveillance": {"required": False},
    }


def normalize_with(name, requirement, *, route=False):
    requirements = base_requirements()
    requirements[name] = requirement
    value = {"project_default": base_requirements()}
    if route:
        value["route_overrides"] = {"R1": requirements}
        return normalize_required_cns(value)["route_overrides"]["R1"][name]
    value["project_default"] = requirements
    return normalize_required_cns(value)["project_default"][name]


def dual_surveillance_requirement():
    return {
        "required": True,
        "services": {
            SERVICE_KEY_RID_COOPERATIVE: {
                "required": True,
                "service_key": SERVICE_KEY_RID_COOPERATIVE,
                "confirmed": True,
                "performance": {"min_redundancy": 2},
            },
            SERVICE_KEY_RADAR_NONCOOPERATIVE: {
                "required": True,
                "service_key": SERVICE_KEY_RADAR_NONCOOPERATIVE,
                "confirmed": True,
                "performance": {"min_redundancy": 3},
            },
        },
    }


def rtk_requirement(planning=None):
    return {
        "required": True,
        "services": {
            SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION: {
                "required": True,
                "service_key": SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION,
                "planning": {} if planning is None else planning,
            },
        },
    }


def test_known_service_keys_include_legacy_and_four_formal_services():
    assert EXPECTED_KEYS <= set(KNOWN_SERVICE_KEYS)


def test_service_identity_is_fail_closed_for_typo():
    with pytest.raises(ValueError):
        validated_service_key("S:radar_noncooperativ")
    with pytest.raises(ValueError):
        normalize_with("surveillance", {
            "required": True,
            "services": {"S:radar_noncooperativ": {"required": True}},
        })


def test_registry_dispatches_each_formal_service_to_its_planner_family():
    assert planner_family_for(SERVICE_KEY_COMMUNICATION) == "omnidirectional_site"
    assert planner_family_for(SERVICE_KEY_RID_COOPERATIVE) == "omnidirectional_site"
    assert planner_family_for(SERVICE_KEY_RADAR_NONCOOPERATIVE) == "directional_radar"
    assert planner_family_for(SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION) == "navigation_reference_station"


def test_radar_registry_has_type_facts_but_no_omnidirectional_surface_geometry():
    entry = service_registry_entry(SERVICE_KEY_RADAR_NONCOOPERATIVE)
    assert entry["type"] == {
        "service_subtype": "noncooperative_surveillance",
        "target_cooperation": "non_cooperative",
        "technology": "radar",
    }
    assert entry["surface_dependent"] is False
    assert SERVICE_KEY_RADAR_NONCOOPERATIVE not in SERVICE_SURFACE_POLICY
    assert not ({"radius_by_surface", "hemisphere", "geometry", "omnidirectional"} & set(entry))


def test_rtk_registry_contains_no_invented_distance_or_site_count():
    entry = service_registry_entry(SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION)
    assert entry["type"] == {
        "service_subtype": "navigation_augmentation", "technology": "gnss_rtk",
    }
    assert not ({"radius_m", "radius_by_surface", "baseline_m", "max_reference_baseline_m",
                 "required_distinct_site_count"} & set(entry))
    assert entry["planning_maturity"] == "engineering_planning_baseline"


def test_legacy_required_cns_shape_does_not_gain_services_or_new_requirements():
    original = {"project_default": base_requirements(), "route_overrides": {}}
    normalized = normalize_required_cns(original)
    for requirement in normalized["project_default"].values():
        assert "services" not in requirement
        assert "service_requirement_mode" not in requirement
    assert required_services_for("S", normalized["project_default"]["surveillance"]) == []
    assert required_services_for("N", normalized["project_default"]["navigation"]) == []


def test_explicit_dual_surveillance_is_all_required_and_keeps_independent_buckets():
    normalized = normalize_with("surveillance", dual_surveillance_requirement())
    assert normalized["service_requirement_mode"] == "all_required"
    assert set(normalized["services"]) == {
        SERVICE_KEY_RID_COOPERATIVE, SERVICE_KEY_RADAR_NONCOOPERATIVE,
    }
    rid = normalized["services"][SERVICE_KEY_RID_COOPERATIVE]
    radar = normalized["services"][SERVICE_KEY_RADAR_NONCOOPERATIVE]
    assert rid["performance"]["min_redundancy"] == 2
    assert radar["performance"]["min_redundancy"] == 3
    assert rid is not radar and rid["service_key"] != radar["service_key"]
    assert radar["type"]["target_cooperation"] == "non_cooperative"
    assert radar["service_subtype"] == "noncooperative_surveillance"
    required = required_services_for("S", normalized)
    assert [item["service_key"] for item in required] == [
        SERVICE_KEY_RID_COOPERATIVE, SERVICE_KEY_RADAR_NONCOOPERATIVE,
    ]


def test_legacy_single_service_rid_does_not_add_radar():
    normalized = normalize_with("surveillance", {
        "required": True,
        "service_key": SERVICE_KEY_RID_COOPERATIVE,
    })
    assert "services" not in normalized
    assert service_requirement_for("S", normalized, SERVICE_KEY_RID_COOPERATIVE) is not None
    assert service_requirement_for("S", normalized, SERVICE_KEY_RADAR_NONCOOPERATIVE) is None


def test_legacy_navigation_and_aircraft_style_gnss_rtk_do_not_add_ground_service():
    normalized = normalize_with("navigation", {
        "required": True, "type": {"technology": "gnss_rtk"},
    })
    assert "services" not in normalized
    legacy = required_services_for("N", normalized)
    assert [item["service_key"] for item in legacy] == [SERVICE_KEY_NAVIGATION]
    assert service_requirement_for("N", normalized, SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION) is None


def test_route_override_uses_same_multi_service_normalizer():
    normalized = normalize_with("surveillance", dual_surveillance_requirement(), route=True)
    assert normalized["service_requirement_mode"] == "all_required"
    assert set(normalized["services"]) == {
        SERVICE_KEY_RID_COOPERATIVE, SERVICE_KEY_RADAR_NONCOOPERATIVE,
    }


def test_rtk_unconfigured_planning_is_explicitly_pending_without_distance_default():
    normalized = normalize_with("navigation", rtk_requirement())
    service = normalized["services"][SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION]
    planning = service["planning"]
    assert planning["model"] == "reference_station_baseline"
    assert planning["max_reference_baseline_m"] is None
    assert planning["required_distinct_site_count"] is None
    assert planning["delivery_service_key"] == SERVICE_KEY_COMMUNICATION
    assert planning["confirmed"] is False
    assert planning["planning_readiness"] == "pending_confirmation"
    assert planning["missing_evidence"] is True
    assert service["type"]["technology"] == "gnss_rtk"
    assert service["service_subtype"] == "navigation_augmentation"


def test_rtk_planning_confirmation_and_source_can_follow_service_level_contract():
    requirement = rtk_requirement()
    service = requirement["services"][SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION]
    service.update({"confirmed": True, "source": "approved policy"})
    planning = normalize_with("navigation", requirement)["services"][
        SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION
    ]["planning"]
    assert planning["confirmed"] is True
    assert planning["source"] == "approved policy"
    # Still pending because the two engineering variables remain unconfigured.
    assert planning["planning_readiness"] == "pending_confirmation"


def test_rtk_explicit_planning_values_round_trip():
    planning = {
        "max_reference_baseline_m": 15000,
        "required_distinct_site_count": 3,
        "delivery_service_key": SERVICE_KEY_COMMUNICATION,
        "confirmed": True,
        "source": "approved engineering policy",
        "maturity": "confirmed_source",
    }
    result = normalize_with("navigation", rtk_requirement(planning))["services"][
        SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION
    ]["planning"]
    assert result["max_reference_baseline_m"] == 15000.0
    assert result["required_distinct_site_count"] == 3
    assert result["planning_readiness"] == "ready"


@pytest.mark.parametrize("value", [0, -1, math.nan, True])
def test_rtk_baseline_rejects_nonpositive_nonfinite_and_bool(value):
    with pytest.raises(ValueError):
        normalize_with("navigation", rtk_requirement({"max_reference_baseline_m": value}))


@pytest.mark.parametrize("value", [0, -1, 1.5, True])
def test_rtk_site_count_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        normalize_with("navigation", rtk_requirement({"required_distinct_site_count": value}))


@pytest.mark.parametrize("value", [None, 1, 4])
def test_rtk_site_count_accepts_null_or_positive_integer(value):
    result = normalize_with("navigation", rtk_requirement({
        "required_distinct_site_count": value,
    }))["services"][SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION]["planning"]
    assert result["required_distinct_site_count"] == value


def test_rtk_delivery_service_is_validated_and_restricted():
    result = normalize_with("navigation", rtk_requirement({
        "delivery_service_key": SERVICE_KEY_COMMUNICATION,
    }))["services"][SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION]["planning"]
    assert result["delivery_service_key"] == SERVICE_KEY_COMMUNICATION
    with pytest.raises(ValueError):
        normalize_with("navigation", rtk_requirement({"delivery_service_key": "C:comunication"}))
    with pytest.raises(ValueError):
        normalize_with("navigation", rtk_requirement({"delivery_service_key": SERVICE_KEY_RID_COOPERATIVE}))


def test_explicit_services_change_required_cns_fingerprint_but_legacy_normalization_is_stable():
    legacy_input = {"project_default": base_requirements(), "route_overrides": {}}
    before = normalize_required_cns(legacy_input)
    after = normalize_required_cns(legacy_input)
    assert stable_fingerprint(before) == stable_fingerprint(after)
    extended_input = {"project_default": base_requirements(), "route_overrides": {}}
    extended_input["project_default"]["surveillance"] = dual_surveillance_requirement()
    extended = normalize_required_cns(extended_input)
    assert stable_fingerprint(before) != stable_fingerprint(extended)
    reopened = normalize_required_cns(json.loads(json.dumps(extended)))
    assert reopened["project_default"]["surveillance"]["services"] == extended[
        "project_default"
    ]["surveillance"]["services"]
