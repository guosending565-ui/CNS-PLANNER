"""Composition root for mutable runtime collaborators."""

from pathlib import Path
import secrets
import threading

from ..gis.map_data import DEFAULT_PATHS, MapData
from ..gis.qgis_runtime import QgisRuntime
from ..tile_cache import TileCache
from .project_directory_service import ProjectDirectoryService
from .workflow_service import WorkflowService


class RenderRequestTracker:
    def __init__(self):
        self.latest, self.lock = {}, threading.Lock()

    def record(self, query):
        client = query.get("client", [""])[0][:100]
        if client:
            with self.lock:
                if len(self.latest) > 100:
                    self.latest.clear()
                self.latest[client] = max(int(query.get("seq", ["0"])[0]), self.latest.get(client, 0))

    def obsolete(self, query):
        client, seq = query.get("client", [""])[0], int(query.get("seq", ["0"])[0])
        with self.lock:
            return bool(client) and seq < self.latest.get(client, 0)


class ApplicationContext:
    def __init__(self, root=None):
        self.root = Path(root or Path(__file__).resolve().parents[2])
        self.static = self.root / "cns_planner" / "web"
        self.default_config = self.root / "cns_planner" / "config" / "defaults.json"
        self.automatic_project_file = self.root / "projects" / "current_project.json"
        self.active_project_file = self.automatic_project_file
        self.token = secrets.token_urlsafe(24)
        self.qgis = QgisRuntime()
        self.tiles = TileCache()
        self.render_requests = RenderRequestTracker()
        self.workflow = WorkflowService(self.active_project_file, self.default_config)
        self.project_directories = ProjectDirectoryService(
            self.automatic_project_file, self.default_config, DEFAULT_PATHS, WorkflowService
        )
        self.data = MapData(
            settings_path=self.root / "projects" / "map_sources.json",
            defaults=DEFAULT_PATHS, default_config=self.default_config, token=self.token,
            workflow_provider=lambda: self.workflow.snapshot(),
            project_metadata_provider=lambda: self.project_directories.storage_metadata(self.active_project_file),
            stale_checker=self.render_requests.obsolete,
        )

    def save_project_as(self, project_dir):
        workflow, target = self.project_directories.save_as(
            project_dir, self.workflow, self.active_project_file, self.data
        )
        self.workflow, self.active_project_file = workflow, target
        return self.data.metadata()

    def open_project(self, project_dir):
        workflow, target = self.project_directories.open(project_dir, self.data)
        self.workflow, self.active_project_file = workflow, target
        return self.data.metadata()

    def replace_sources(self, paths):
        previous = dict(self.data.paths)
        self.data.load(paths)
        changed = {name for name in paths if previous.get(name) != self.data.paths.get(name)}
        self.workflow.invalidate_grid_attributes(changed)
        if "basemap" in changed:
            self.workflow.invalidate("data")
        self.workflow.save()
        self.project_directories.persist_sources(self.active_project_file, self.data)
        return self.data.metadata()
