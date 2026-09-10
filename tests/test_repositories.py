import json

from cns_planner.persistence.data_source_repository import DataSourceRepository
from cns_planner.persistence.project_repository import ProjectRepository


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
    assert not path.with_suffix(".tmp").exists()


def test_project_repository_copy_preserves_saved_document(tmp_path):
    source = ProjectRepository(tmp_path / "source" / "project_state.json")
    target = tmp_path / "target" / "project_state.json"
    target.parent.mkdir()
    document = {"schema_version": 2, "project": {"name": "复制测试"}}
    source.save(document)

    source.copy_to(target)

    assert ProjectRepository(target).load() == document
    assert target.read_bytes() == source.path.read_bytes()


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
