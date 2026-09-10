from cns_planner.models.status import Assessment, ResultStatus, SafetyResult
from cns_planner.services.data_health import build_health
from cns_planner.services.invalidation import ResultLedger


def sample_metadata():
    return {
        "paths": {"basemap": "map.qgz", "population": "population.tif"},
        "layers": [{"id": "air", "name": "airspace"}],
        "online_sources": [{"id": "tiles", "browser_url": "https://example.test/{z}/{x}/{y}.png"}],
        "population": {"width": 100, "height": 200, "bands": 1, "crs": "EPSG:4326", "nodata": "-9999"},
        "error": "",
    }


def test_unknown_safety_result_never_becomes_passed():
    result = SafetyResult(
        environment=Assessment("environment", ResultStatus.PASSED),
        technical=Assessment("technical", ResultStatus.PASSED),
        life=Assessment("life", ResultStatus.PENDING_CONFIRMATION),
        property=Assessment("property", ResultStatus.NOT_CALCULATED),
    )
    assert result.overall_status == ResultStatus.PENDING_CONFIRMATION
    assert result.overall_pass is None


def test_only_complete_assessments_pass_and_failure_dominates():
    passed = SafetyResult(
        environment=Assessment("environment", ResultStatus.PASSED),
        technical=Assessment("technical", ResultStatus.PASSED),
        life=Assessment("life", ResultStatus.PASSED),
        property=Assessment("property", ResultStatus.NOT_APPLICABLE),
    )
    assert passed.overall_status == ResultStatus.PASSED
    assert passed.overall_pass is True
    failed = SafetyResult(life=Assessment("life", ResultStatus.FAILED))
    assert failed.overall_status == ResultStatus.FAILED
    assert failed.overall_pass is False


def test_health_is_stage_aware_and_keeps_online_services_separate():
    health = build_health(sample_metadata())
    by_id = {item["id"]: item for item in health["items"]}
    assert health["stage"] == "P1"
    assert health["status"] == "warning"
    assert by_id["basemap"]["status"] == "ready"
    assert by_id["population"]["status"] == "ready"
    assert by_id["online_map"]["id"] != by_id["geocoder"]["id"]
    assert any(check["status"] == "pending_workspace" for check in by_id["population"]["checks"])


def test_dependency_changes_mark_existing_results_stale():
    ledger = ResultLedger()
    ledger.statuses["routes"] = ResultStatus.PASSED
    ledger.statuses["coverage"] = ResultStatus.PASSED
    affected = ledger.invalidate("rules")
    assert "routes" in affected and "coverage" in affected
    assert ledger.statuses["routes"] == ResultStatus.STALE
    assert ledger.statuses["coverage"] == ResultStatus.STALE
