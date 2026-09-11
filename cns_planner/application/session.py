"""Shared project-state session used by application services."""

import json
from json import JSONDecodeError
from pathlib import Path

from ..persistence.project_repository import ProjectRepository
from .project_state import blank_project, normalize_project, utc_now


class WorkflowSession:
    def __init__(self, store_path: Path, defaults_path: Path, grid_service):
        self.store_path = Path(store_path)
        self.defaults_path = Path(defaults_path)
        self.repository = ProjectRepository(self.store_path)
        self.defaults = json.loads(self.defaults_path.read_text(encoding="utf-8"))
        self.grid_service = grid_service
        self.state = self._load()

    def _load(self):
        if not self.repository.exists():
            return blank_project(self.defaults)
        try:
            document = self.repository.load()
        except JSONDecodeError as exc:
            raise ValueError("项目 JSON 损坏，未打开") from exc
        return normalize_project(document, self.grid_service)

    def save(self):
        self.state["project"]["updated_at"] = utc_now()
        self.state["last_saved_at"] = utc_now()
        self.repository.save(self.state)
