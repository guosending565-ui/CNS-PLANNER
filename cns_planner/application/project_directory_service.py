"""Transactional project-folder save/open orchestration."""

from pathlib import Path

from ..persistence.data_source_repository import DataSourceRepository
from ..persistence.project_repository import ProjectRepository


class ProjectDirectoryService:
    REFERENCE_SOURCE_KEYS = ("reference_landing_sites", "reference_routes", "equipment_reference_catalog")
    def __init__(self, automatic_file, defaults_path, default_sources, workflow_factory):
        self.automatic_file = Path(automatic_file)
        self.defaults_path = Path(defaults_path)
        self.default_sources = dict(default_sources)
        self.workflow_factory = workflow_factory

    def storage_metadata(self, active_file):
        path = Path(active_file)
        automatic = path.resolve() == self.automatic_file.resolve()
        return {"automatic": automatic, "file": str(path), "directory": "" if automatic else str(path.parent)}

    def data_sources_path(self, active_file):
        path = Path(active_file)
        return None if path.resolve() == self.automatic_file.resolve() else path.parent / "data_sources.json"

    def persist_sources(self, active_file, data):
        target = self.data_sources_path(active_file)
        if target is not None:
            DataSourceRepository(target).save(self._clean_sources(data.paths))

    def save_as(self, project_dir, workflow, active_file, data):
        folder = self._project_folder(project_dir, create=True)
        target, sources_target = folder / "project_state.json", folder / "data_sources.json"
        previous_target = target.read_bytes() if target.is_file() else None
        previous_sources = sources_target.read_bytes() if sources_target.is_file() else None
        try:
            workflow.save()
            source = ProjectRepository(active_file)
            if not source.is_file():
                raise ValueError("当前项目尚未形成可保存的项目状态文件")
            if source.path.resolve() != target.resolve():
                source.copy_to(target)
            DataSourceRepository(sources_target).save(self._clean_sources(data.paths))
            candidate = self.workflow_factory(target, self.defaults_path)
        except Exception:
            self._restore(target, previous_target)
            self._restore(sources_target, previous_sources)
            raise
        return candidate, target

    def open(self, project_dir, current_data):
        folder = self._project_folder(project_dir)
        target = folder / "project_state.json"
        if not target.is_file():
            legacy = folder / "current_project.json"
            if legacy.is_file():
                target = legacy
            else:
                raise ValueError("该目录不是有效项目：缺少 project_state.json")
        candidate = self.workflow_factory(target, self.defaults_path)
        repository = DataSourceRepository(folder / "data_sources.json")
        if repository.is_file():
            saved = repository.load()
            clean = {key: saved.get(key) or current_data.paths.get(key) or self.default_sources.get(key, "")
                     for key in ("basemap", "population", "terrain")}
            clean.update({
                key: saved.get(key) or current_data.paths.get(key) or self.default_sources.get(key, "")
                for key in self.REFERENCE_SOURCE_KEYS
                if saved.get(key) or current_data.paths.get(key) or self.default_sources.get(key)
            })
            current_data.load(clean, persist=False)
        return candidate, target

    @staticmethod
    def _project_folder(raw, create=False):
        value = str(raw or "").strip()
        if not value:
            raise ValueError("请选择项目数据存储位置" if create else "请选择项目文件夹")
        folder = Path(value).expanduser()
        if create and not folder.is_absolute():
            raise ValueError("项目存储位置必须使用完整路径")
        if create:
            folder.mkdir(parents=True, exist_ok=True)
        folder = folder.resolve()
        if not folder.is_dir():
            raise ValueError("项目文件夹不存在")
        return folder

    @staticmethod
    def _clean_sources(paths):
        result = {key: paths.get(key, "") for key in ("basemap", "population", "terrain")}
        result.update({
            key: paths[key] for key in ProjectDirectoryService.REFERENCE_SOURCE_KEYS
            if paths.get(key)
        })
        return result

    @staticmethod
    def _restore(path, previous):
        path = Path(path)
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            restore = path.with_suffix(".restore.tmp")
            try:
                restore.write_bytes(previous)
                restore.replace(path)
            finally:
                restore.unlink(missing_ok=True)
