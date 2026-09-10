"""Low-level data-source JSON file operations."""

import json
from pathlib import Path


class DataSourceRepository:
    """Read and atomically write one data-source settings JSON file."""

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
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)
