import json
from concurrent.futures import ThreadPoolExecutor

from cns_planner.persistence.data_source_repository import DataSourceRepository
from cns_planner.persistence.project_repository import ProjectRepository
from cns_planner.persistence.project_compaction import compact_and_store, restore_compacted_results


def test_project_repository_roundtrip_uses_existing_json_shape(tmp_path):
    path = tmp_path / "project" / "project_state.json"
    repository = ProjectRepository(path)
    document = {
        "schema_version": 2,
        "project": {"project_id": "fixed-id", "name": "仓储测试"},
        "workspace": None,
    }

    repository.save(document)

    assert repository.exists()
    assert repository.is_file()
    assert repository.load() == document
    assert json.loads(path.read_text(encoding="utf-8")) == document
    assert not list(path.parent.glob("*.tmp"))


def test_project_repository_ten_saves_keep_latest_and_recent_backup(tmp_path):
    path = tmp_path / "project" / "project_state.json"
    repository = ProjectRepository(path)

    for revision in range(10):
        repository.save({"revision": revision, "payload": "x" * 1000})

    assert repository.load()["revision"] == 9
    assert json.loads(repository.backup_path.read_text(encoding="utf-8"))["revision"] == 8
    assert not list(path.parent.glob("*.tmp"))


def test_project_repository_concurrent_saves_are_complete_json(tmp_path):
    path = tmp_path / "project" / "project_state.json"

    def save(revision):
        # Separate instances must still coordinate on the same canonical path.
        ProjectRepository(path).save({"revision": revision, "payload": str(revision) * 5000})

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(save, range(20)))

    current = json.loads(path.read_text(encoding="utf-8"))
    backup = json.loads(ProjectRepository(path).backup_path.read_text(encoding="utf-8"))
    assert current["payload"] == str(current["revision"]) * 5000
    assert backup["payload"] == str(backup["revision"]) * 5000
    assert not list(path.parent.glob("*.tmp"))


def test_project_repository_copy_preserves_saved_document(tmp_path):
    source = ProjectRepository(tmp_path / "source" / "project_state.json")
    target = tmp_path / "target" / "project_state.json"
    target.parent.mkdir()
    document = {"schema_version": 2, "project": {"name": "复制测试"}}
    source.save(document)

    source.copy_to(target)

    assert ProjectRepository(target).load() == document
    assert target.read_bytes() == source.path.read_bytes()


def test_large_derived_results_are_indexed_and_restored_from_sidecar(tmp_path):
    path = tmp_path / "source" / "current_project.json"
    cells = {f"G{i}": {"grid_id": f"G{i}", "value": "x" * 1000} for i in range(40)}
    masks = {"R1@L1": {"fingerprint": "mask-fp", "cells": cells}}
    state = {
        "schema_version": 2,
        "project": {"name": "compact"},
        "grid_risk_v2": {
            "status": "passed", "input_fingerprint": "input-fp",
            "policy_fingerprint": "policy-fp", "cells": cells,
        },
        "layered_route_candidates": {
            "status": "passed", "count": 1, "active_candidate_id": "C1",
            "items": [{"candidate_id": "C1", "fingerprint": "candidate-fp", "grid_path": list(cells)}],
            "masks": masks,
        },
        "_population_shelter_cache": {"cells": cells},
    }

    compact = compact_and_store(state, path)
    ProjectRepository(path).save(compact)
    restored = restore_compacted_results(ProjectRepository(path).load(), path)

    assert "_population_shelter_cache" not in compact
    assert compact["grid_risk_v2"]["cells"] == {}
    assert compact["layered_route_candidates"]["items"] == []
    assert compact["layered_route_candidates"]["masks"] == {}
    # Phase4-B5X：统一 Artifact Contract（content-addressed + manifest），
    # legacy ``result_index`` 不再是写入格式。
    assert "result_index" not in compact
    manifest = compact["artifact_manifest"]
    risk_entry = manifest["entries"][manifest["refs"]["grid_risk_v2"]]
    assert risk_entry["artifact_type"] == "risk.grid_risk_v2.cells"
    assert risk_entry["content_encoding"] == "json.gz"
    assert risk_entry["sha256"] == risk_entry["artifact_id"]
    assert risk_entry["relative_path"].startswith(".cns-results/")
    assert risk_entry["summary"]["cell_count"] == 40
    candidates_entry = manifest["entries"][manifest["refs"]["layered_route_candidates"]]
    assert candidates_entry["summary"]["detail_count"] == 2
    # artifact 引用统一由 manifest 表达（不污染结果容器的业务键空间）。
    assert compact["grid_risk_v2"].get("artifact_ref") is None
    assert manifest["refs"]["grid_risk_v2"] == risk_entry["artifact_id"]
    assert restored["grid_risk_v2"]["cells"] == cells
    assert restored["layered_route_candidates"]["items"] == state["layered_route_candidates"]["items"]
    assert restored["layered_route_candidates"]["masks"] == masks
    assert path.stat().st_size < len(json.dumps(state, ensure_ascii=False).encode("utf-8")) / 4
    assert not list(path.parent.glob("**/*.tmp"))


def test_copy_project_copies_indexed_result_artifact(tmp_path):
    source = tmp_path / "source" / "current_project.json"
    target = tmp_path / "target" / "project_state.json"
    state = {
        "grid_risk_v2": {"cells": {"G1": {"value": 1}}},
        "layered_route_candidates": {"items": [], "masks": {}},
    }
    ProjectRepository(source).save(compact_and_store(state, source))

    ProjectRepository(source).copy_to(target)

    restored = restore_compacted_results(ProjectRepository(target).load(), target)
    assert restored["grid_risk_v2"]["cells"] == {"G1": {"value": 1}}


def test_data_source_repository_roundtrip_uses_existing_json_shape(tmp_path):
    path = tmp_path / "project" / "data_sources.json"
    repository = DataSourceRepository(path)
    sources = {
        "basemap": "portable/map.qgz",
        "population": "portable/population.tif",
        "terrain": "portable/terrain.tif",
    }

    repository.save(sources)

    assert repository.exists()
    assert repository.is_file()
    assert repository.load() == sources
    assert json.loads(path.read_text(encoding="utf-8")) == sources
    assert not path.with_suffix(".tmp").exists()
