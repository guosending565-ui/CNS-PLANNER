import pytest
from cns_planner.application.engineering_assumption_p16_adapter import _scenario_devices


def test_current_catalog_resolves_declared_2km_without_version_special_case():
    scenario = {"version": "future-research-revision", "assumptions": {"equipment": {"C": "COMM-BASELINE-2KM", "S": "RID-BASELINE-2-5KM"}}}
    catalog = {key: {"device_id": key, "subsystem": code, "enabled": True} for code, key in scenario["assumptions"]["equipment"].items()}
    assert _scenario_devices(scenario["assumptions"], catalog) == scenario["assumptions"]["equipment"]
    catalog["COMM-BASELINE-2KM"]["enabled"] = False
    with pytest.raises(ValueError):
        _scenario_devices(scenario["assumptions"], catalog)


def test_missing_catalog_entry_cannot_fall_back_to_4km():
    with pytest.raises(ValueError):
        _scenario_devices({"equipment": {"C": "COMM-BASELINE-2KM"}}, {})
