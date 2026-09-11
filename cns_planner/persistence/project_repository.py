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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        try:
            temporary.write_text(
                json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False),
                encoding="utf-8",
            )
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def copy_to(self, target: Path) -> None:
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        try:
            shutil.copy2(self.path, temporary)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
