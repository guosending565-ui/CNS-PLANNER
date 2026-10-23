import pytest
from cns_planner.domain.cns_service_registry import NAVIGATION_INTEGRITY_POLICY_SOURCE, _normalize_navigation_integrity_planning

@pytest.mark.parametrize("old_name", ["导航完好性监视", "导航完整性监测"])
def test_legacy_navigation_source_keeps_frozen_policy(old_name):
    legacy = NAVIGATION_INTEGRITY_POLICY_SOURCE.replace("导航服务监测", old_name)
    result = _normalize_navigation_integrity_planning({"source": legacy}, field="planning")
    assert result["source"] == NAVIGATION_INTEGRITY_POLICY_SOURCE
    assert result["required_monitor_per_endpoint"] == 1

def test_other_policy_source_is_rejected():
    with pytest.raises(ValueError):
        _normalize_navigation_integrity_planning({"source": "unverified"}, field="planning")
