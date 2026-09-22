"""Constrained local source/project picker listing."""

from pathlib import Path

from ..gis.path_resolver import SOURCE_FORMATS


def browse(path, kind):
    folder_only = kind == "project"
    if folder_only:
        extensions = ()
    elif kind in SOURCE_FORMATS and SOURCE_FORMATS[kind]:
        # 单一事实来源：文件对话框接受的扩展名与来源校验接受的一致（例如建筑类来源
        # 现在是 .gpkg / .shp / .geojson / 引用建筑图层的 .qgz 工程）。
        extensions = tuple(SOURCE_FORMATS[kind])
    else:
        extensions = ()
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
