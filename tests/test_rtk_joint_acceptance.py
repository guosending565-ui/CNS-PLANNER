"""M2 synthetic contracts; never loads or saves ProjectState."""
from copy import deepcopy
import pytest
from test_ue_rule_adapter import inputs, build

RTK = "N:rtk_augmentation"

def snapshot(adopted=True):
    data = inputs()
    data["route_id"] = "fixture-route"
    data["operation_scenario"]["space_scope"] = {"route_id": "fixture-route"}
    if adopted:
        data["required_cns"]["project_default"]["navigation"]["services"][RTK] = {
            "required": True, "confirmed": True, "planning": {
                "confirmed": True, "max_reference_baseline_m": 1000,
                "required_distinct_site_count": 1, "delivery_service_key": "C:communication",
                "source": "synthetic_fixture", "version": "fixture-v1"}}
    return build(**data)

def segment(start=0, end=100, **changes):
    return dict(dict(start_m=start, end_m=end, geometry_status="satisfied",
        providers=[dict(distinct_site_id="a", baseline_distance_m=100,
            eligibility_status="satisfied", device_status="satisfied")],
        provider_inventory_status="complete", delivery_status="satisfied",
        delivery_service_key="C:communication", handover_status="satisfied",
        evidence_status="confirmed", source_fingerprint="synthetic_fixture-p14-p15"), **changes)

def evaluate(segments=None, **kwargs):
    from cns_planner.application.rtk_joint_acceptance import evaluate_p17_rtk_joint
    return evaluate_p17_rtk_joint(frozen_snapshot=kwargs.pop("frozen_snapshot", snapshot()),
        route_length_m=100, segments=segments if segments is not None else [segment()],
        service_evidence_track=kwargs.pop("service_evidence_track", "synthetic_fixture"),
        synthetic_fixture=kwargs.pop("synthetic_fixture", True),
        speed_profile=kwargs.pop("speed_profile", [dict(start_m=0,end_m=100,speed_mps=10,
            confirmed=True,source_id="synthetic_fixture",version="1")]), **kwargs)

def test_missing_duration_and_tolerance_cannot_manage():
    result = evaluate([segment(delivery_status="confirmed_deficit")], speed_profile=[])
    assert result["rtk_status"] == "rtk_degraded_or_outage"
    assert result["navigation_status"] == "unknown"
    assert result["gaps"][0]["gap_duration_s"] is None
    assert "gap_time_unknown" in result["gaps"][0]["reason_codes"]

def test_one_qualified_reference_station():
    r = evaluate()
    assert r["rtk_status"] == "rtk_normal"
    assert r["navigation_status"] == "satisfied"
    assert r["persisted_to_project_state"] is False

def test_declared_availability_does_not_override_delivery_outage():
    snap = snapshot()
    snap["parameters"]["rtk_availability"] = {"value": "available"}
    r = evaluate([segment(delivery_status="confirmed_deficit")], frozen_snapshot=snap)
    assert r["rtk_status"] == "rtk_degraded_or_outage"
    assert r["navigation_status"] == "unknown"
    assert r["gaps"][0]["gap_cause"] == ["correction_delivery_deficit"]

@pytest.mark.parametrize("speed", [None, 0])
def test_missing_or_zero_speed(speed):
    r=evaluate([segment(delivery_status="confirmed_deficit")],speed_profile=[dict(
        start_m=0,end_m=100,speed_mps=speed,confirmed=True,source_id="synthetic_fixture",version="1")])
    assert r["gaps"][0]["gap_duration_s"] is None

def test_not_adopted_stays_unknown_and_inputs_untouched():
    snap=snapshot(False); before=deepcopy(snap)
    r=evaluate(frozen_snapshot=snap)
    assert r["navigation_status"] == "unknown"
    assert "required_service_not_adopted" in r["reason_codes"]
    assert snap == before


def backup():
    snap=snapshot()
    scope={k:snap[k] for k in ("route_id","scenario_id","operating_phase")}
    scope.update({k:snap["frozen_inputs"]["operation_scenario"][k]
                  for k in ("space_scope","cruise_altitude")})
    values={"outage_limit_s":(12,"s"), "valid_duration_s":(60,"s"),
        "horizontal_error_m":(1,"m"), "horizontal_drift_mps":(0.1,"m/s"),
        "horizontal_bound_m":(5,"m"), "detection_time_s":(1,"s"),
        "alert_time_s":(1,"s"),"switch_time_s":(1,"s"),"response_time_s":(1,"s"),
        "max_transition_time_s":(5,"s")}
    return dict(schema_version="navigation_backup_envelope.v1", evidence_track="synthetic_fixture",
        scenario_id="synthetic-navigation",model_id="fixture-envelope",source_id="synthetic_fixture",
        version="1",scope=scope,assumptions=["constant worst-case drift in fixture environment"],
        uncertainty="synthetic exact bounds; not measured",review_status="verified_for_scope",
        vertical_required=False,parameters={k:dict(value=v,unit=u,source_ref="synthetic_fixture",
            locator="test fixture",version="1",conversion="identity",reliability="test-only",
            uncertainty_range="synthetic exact value") for k,(v,u) in values.items()},
        conditions={k:dict(status="satisfied",source_ref="synthetic_fixture") for k in
            ("availability","integrity","protection","response","hard_constraints")})


def managed(segments=None, **kwargs):
    return evaluate(segments or [segment(delivery_status="confirmed_deficit")],
        backup_scenario=kwargs.pop("backup_scenario",backup()),synthetic_fixture=True,**kwargs)

@pytest.mark.parametrize("missing", ["horizontal_error_m","detection_time_s","switch_time_s","outage_limit_s"])
def test_missing_backup_parameter_never_managed(missing):
    model=backup(); del model["parameters"][missing]
    assert managed(backup_scenario=model)["navigation_status"] == "unknown"

@pytest.mark.parametrize("condition", ["integrity","availability","protection","response","hard_constraints"])
def test_missing_backup_condition_never_managed(condition):
    model=backup(); del model["conditions"][condition]
    assert managed(backup_scenario=model)["navigation_status"] == "unknown"


def test_managed_simulation_keeps_actual_rtk_outage_and_real_evidence_missing():
    snap=snapshot(); original=deepcopy(snap); model=backup(); original_model=deepcopy(model)
    r=managed(frozen_snapshot=snap,backup_scenario=model)
    assert r["navigation_status"] == "managed"
    assert r["rtk_status"] == "rtk_degraded_or_outage"
    e=r["gaps"][0]["navigation_evaluation"]
    assert e["management_basis"] == "managed_under_simulation"
    assert e["validation_status"] == "illustrative_not_validated"
    assert all(e[k] for k in ("scenario_id","model_id","source_id","version","input_fingerprint"))
    assert r["real_aircraft_evidence"]["status"] == "missing"
    assert snap == original and model == original_model


def test_synthetic_fixture_requires_explicit_opt_in():
    r=evaluate([segment(delivery_status="confirmed_deficit")],backup_scenario=backup(),synthetic_fixture=False)
    assert r["navigation_status"] == "unknown"


def test_over_limit_beats_other_unknown_and_gnss_available():
    model=backup(); model["parameters"]["outage_limit_s"]["value"]=9
    del model["conditions"]["integrity"]
    r=managed(backup_scenario=model)
    assert r["navigation_status"] == "unacceptable"
    assert "backup_condition_source_missing:integrity" in r["unknowns"]


def test_multiple_gaps_not_only_longest_and_varying_speed():
    rows=[segment(0,20,delivery_status="confirmed_deficit"),segment(20,50),
          segment(50,100,delivery_status="confirmed_deficit")]
    speeds=[dict(start_m=0,end_m=50,speed_mps=10,confirmed=True,source_id="synthetic_fixture",version="1"),
            dict(start_m=50,end_m=100,speed_mps=2,confirmed=True,source_id="synthetic_fixture",version="1")]
    r=managed(rows,speed_profile=speeds)
    assert [g["gap_duration_s"] for g in r["gaps"]] == [2,25]
    assert [g["navigation_evaluation"]["status"] for g in r["gaps"]] == ["managed","unacceptable"]
    assert r["navigation_status"] == "unacceptable"
    r=managed(speed_profile=speeds)
    assert r["gaps"][0]["gap_duration_s"] == 30


def test_adjacent_different_causes_share_one_outage_clock():
    a=segment(0,50,delivery_status="confirmed_deficit")
    b=segment(50,100,handover_status="confirmed_deficit")
    r=managed([a,b]); gap=r["gaps"][0]
    assert len(r["gaps"]) == 1 and gap["gap_duration_s"] == 10
    assert set(gap["gap_cause"]) == {"correction_delivery_deficit","handover_deficit"}


def test_multi_station_handover_and_missing_handover():
    a,b=segment(0,50),segment(50,100)
    b["providers"][0]["distinct_site_id"]="b"
    assert evaluate([a,b])["rtk_status"] == "rtk_normal"
    b["handover_status"]=None
    assert evaluate([a,b])["navigation_status"] == "unknown"

@pytest.mark.parametrize("field,value,cause", [
    ("baseline_distance_m",1001,"reference_baseline_exceeded"),
    ("device_status","confirmed_deficit","device_capability_deficit"),
    ("eligibility_status","confirmed_deficit","reference_station_ineligible")])
def test_provider_failures(field,value,cause):
    row=segment(); row["providers"][0][field]=value
    r=evaluate([row])
    assert r["rtk_status"] == "rtk_degraded_or_outage"
    assert cause in r["gaps"][0]["gap_cause"]


def test_geometry_only_and_unknown_inventory_are_not_valid_service():
    row=segment(); row["providers"]=[dict(distinct_site_id="a",baseline_distance_m=100)]
    assert evaluate([row])["rtk_status"] == "rtk_unknown"
    assert evaluate([segment(providers=[])])["rtk_status"] == "rtk_degraded_or_outage"
    assert evaluate([segment(providers=[],provider_inventory_status=None)])["rtk_status"] == "rtk_unknown"


def test_same_site_devices_cannot_count_as_two_stations():
    snap=snapshot(); req=snap["frozen_inputs"]["required_cns"]["requirements"]
    req["navigation"]["services"][RTK]["planning"]["required_distinct_site_count"]=2
    next(r for r in snap["rules"] if r["failure_mode"]=="reference_station_deficit")["threshold"]["value"]=2
    row=segment(); row["providers"]*=2
    assert evaluate([row],frozen_snapshot=snap)["rtk_status"] == "rtk_degraded_or_outage"

@pytest.mark.parametrize("change", ["model_version","parameter_source","segment_source","scope","speed_version"])
def test_changed_sources_never_reuse_fingerprint(change):
    model=backup(); rows=[segment(delivery_status="confirmed_deficit")]
    speeds=[dict(start_m=0,end_m=100,speed_mps=10,confirmed=True,source_id="fixture",version="1")]
    before=managed(rows,backup_scenario=model,speed_profile=speeds)
    if change=="model_version": model["version"]="2"
    if change=="parameter_source": model["parameters"]["outage_limit_s"]["source_ref"]="new"
    if change=="segment_source": rows[0]["source_fingerprint"]="new"
    if change=="scope": model["scope"]["operating_phase"]="landing"
    if change=="speed_version": speeds[0]["version"]="2"
    after=managed(rows,backup_scenario=model,speed_profile=speeds)
    assert before["input_fingerprint"] != after["input_fingerprint"]
    if change=="scope": assert after["navigation_status"] == "unknown"


def test_partial_geometry_unknown_cannot_hide_in_managed():
    r=managed([segment(10,100,delivery_status="confirmed_deficit")])
    assert r["navigation_status"] == "unknown"
    assert "gap_extent_unknown" in r["gaps"][0]["reason_codes"]

@pytest.mark.parametrize("speeds", [[], [dict(start_m=0,end_m=80,speed_mps=10,confirmed=True,source_id="x",version="1")],
    [dict(start_m=0,end_m=100,speed_mps=10,confirmed=False,source_id="x",version="1")],
    [dict(start_m=0,end_m=100,speed_mps=10,confirmed=True,source_id="x",version="1")]*2])
def test_speed_gaps_overlaps_and_unconfirmed_are_unknown(speeds):
    assert managed(speed_profile=speeds)["gaps"][0]["gap_duration_s"] is None


def test_vertical_drift_and_transition_exceedance():
    model=backup(); model["vertical_required"]=True
    r=managed(backup_scenario=model)
    assert r["navigation_status"] == "unknown"
    for name,value,unit in (("vertical_error_m",1,"m"),("vertical_drift_mps",1,"m/s"),("vertical_bound_m",5,"m")):
        model["parameters"][name]=dict(model["parameters"]["horizontal_error_m"],value=value,unit=unit)
    assert managed(backup_scenario=model)["navigation_status"] == "unacceptable"
    model=backup(); model["parameters"]["switch_time_s"]["value"]=10
    assert managed(backup_scenario=model)["navigation_status"] == "unacceptable"


def test_model_duration_outside_scope_and_unreviewed_source_unknown():
    model=backup(); model["parameters"]["valid_duration_s"]["value"]=5
    assert managed(backup_scenario=model)["navigation_status"] == "unknown"
    model=backup(); model["review_status"]=None
    assert managed(backup_scenario=model)["navigation_status"] == "unknown"


def test_real_and_simulation_tracks_cannot_share_aircraft_identity():
    model=backup(); model["evidence_track"]="real_aircraft_evidence"
    assert managed(backup_scenario=model)["navigation_status"] == "unknown"


def test_nan_and_wrong_route_rejected():
    with pytest.raises(ValueError): evaluate([segment(0,float("nan"))])
    with pytest.raises(ValueError): evaluate([segment(route_id="other")])


def test_p14_p15_adapter_preserves_delivery_deficit_without_device_invention():
    from cns_planner.application.rtk_joint_acceptance import adapt_p14_p15_rtk_segments
    voxel=dict(voxel_id="v1",nearest_route_offset_m=50,cell_half_diagonal_m=50,subsystems=[
        dict(subsystem="N",service_redundancy=[dict(service_key=RTK,status="confirmed_deficit",
            geometry_status="satisfied",delivery_status="confirmed_deficit",providers=[
                dict(distinct_site_id="a",baseline_distance_m=100)],delivery_service_key="C:communication",
            gap_causes=["correction_delivery_deficit"])])])
    rows=adapt_p14_p15_rtk_segments(route_id="fixture-route",route_length_m=100,
        assessed_voxels=[voxel],source_fingerprint="p14-synthetic-fixture")
    r=evaluate(rows)
    assert r["rtk_status"] == "rtk_degraded_or_outage"
    assert "correction_delivery_deficit" in r["gaps"][0]["gap_cause"]
    assert r["unknowns"]


def test_legacy_communication_and_surveillance_unchanged():
    from test_continuous_service_acceptability import _navigation_result
    before=_navigation_result(rtk="not_available",gnss="available",accuracy=8,containment=10)
    evaluate()
    after=_navigation_result(rtk="not_available",gnss="available",accuracy=8,containment=10)
    assert before == after
    for route in after["routes"]:
        n=next(x for x in route["subsystems"] if x["subsystem"]=="N")
        assert n["status"] == "unknown" and n["managed_gaps"] == []


def test_known_outage_exceedance_survives_missing_accuracy_parameter():
    model=backup(); model["parameters"]["outage_limit_s"]["value"]=9
    del model["parameters"]["horizontal_error_m"]
    r=managed(backup_scenario=model)
    assert r["navigation_status"] == "unacceptable"
    assert "backup_parameter_missing:horizontal_error_m" in r["unknowns"]


@pytest.mark.parametrize("track", ["literature_grounded_simulation","real_aircraft_evidence"])
def test_fixture_cannot_be_relabelled_as_real_or_literature(track):
    model=backup(); model["evidence_track"]=track
    model["aircraft_id"]=snapshot()["frozen_inputs"]["aircraft_profile"]["aircraft_id"]
    assert managed(backup_scenario=model)["navigation_status"] == "unknown"


def test_missing_upstream_fingerprint_and_optimistic_service_cannot_pass():
    assert evaluate([segment(source_fingerprint=None)])["rtk_status"] == "rtk_unknown"
    row=segment(service_status="confirmed_deficit")
    assert evaluate([row])["rtk_status"] == "rtk_degraded_or_outage"


def test_p14_adapter_nominal_geometry_keeps_device_and_handover_unknown():
    from cns_planner.application.rtk_joint_acceptance import adapt_p14_p15_rtk_segments
    voxel=dict(nearest_route_offset_m=50,cell_half_diagonal_m=50,subsystems=[dict(
        subsystem="N",services=[dict(service_key=RTK,status="satisfied",geometry_status="satisfied",
          delivery_status="satisfied",delivery_service_key="C:communication",
          providers=[dict(distinct_site_id="a",baseline_distance_m=100)])])])
    rows=adapt_p14_p15_rtk_segments(route_id="fixture-route",route_length_m=100,
        assessed_voxels=[voxel],source_fingerprint="synthetic-p15")
    assert evaluate(rows)["navigation_status"] == "unknown"


def test_no_rtk_rule_defaults_and_unknowns_survive_known_service_gap():
    snap=snapshot(); next(r for r in snap["rules"] if r["failure_mode"]=="reference_baseline_exceeded")["threshold"]["value"]=None
    r=evaluate([segment(delivery_status="confirmed_deficit")],frozen_snapshot=snap)
    assert r["navigation_status"] == "unknown"
    assert r["rtk_status"] == "rtk_degraded_or_outage"
    assert r["gaps"]



def test_normal_service_fixture_cannot_claim_real_evidence_without_opt_in():
    r=evaluate(synthetic_fixture=False)
    assert r["navigation_status"] == "unknown"
    assert r["evidence_track"] == "synthetic_fixture"
    assert r["validation_status"] == "illustrative_not_validated"


def test_baseline_exceeded_does_not_hide_missing_device_or_eligibility():
    row=segment(); row["providers"]=[dict(distinct_site_id="a",baseline_distance_m=1001)]
    r=managed([row])
    assert r["rtk_status"] == "rtk_degraded_or_outage"
    assert r["navigation_status"] == "unknown"
    assert "provider_device_unknown:a" in r["unknowns"]
    assert "provider_eligibility_unknown:a" in r["unknowns"]
