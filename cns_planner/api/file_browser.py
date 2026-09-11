"""Constrained local source/project picker listing."""

from pathlib import Path


def browse(path, kind):
    folder_only = kind == "project"
    extensions = (
        (".qgz", ".qgs") if kind == "basemap"
        else (".tif", ".tiff") if kind in ("population", "terrain")
        else (".json", ".csv", ".geojson") if kind in ("existing_cns", "candidate_sites")
        else ()
    )
    if not path:
        return {"path": "", "parent": "", "entries": [{"name": f"{drive}:\\", "path": f"{drive}:/", "directory": True} for drive in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if Path(f"{drive}:/").exists()]}
    directory = Path(path).expanduser().resolve()
    if not directory.is_dir():
        directory = directory.parent
    entries = []
    for item in directory.iterdir():
        try:
            folder = item.is_dir()
            if folder or (not folder_only and item.suffix.lower() in extensions):
                entries.append({"name": item.name, "path": str(item), "directory": folder})
        except OSError:
            continue
    entries.sort(key=lambda entry: (not entry["directory"], entry["name"].casefold()))
    return {"path": str(directory), "parent": str(directory.parent) if directory.parent != directory else "", "entries": entries}
