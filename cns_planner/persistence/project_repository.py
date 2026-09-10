"""Low-level project-state JSON file operations."""

import json
from pathlib import Path
import shutil


class ProjectRepository:
    """Read, atomically write, and copy one project-state JSON file."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def exists(self) -> bool:
        return self.path.exists()

    def is_file(self) -> bool:
        return self.path.is_file()

    def load(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, document) -> None:
        self.path.parent.mkdir(exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def copy_to(self, target: Path) -> None:
        shutil.copy2(self.path, Path(target))
