"""Thin QGIS workbench composition and launch entry.

Compatibility names remain available for existing local integrations; their
implementations are delegated to api/, application/ and gis/ modules.
"""

import os
from pathlib import Path
import secrets
import threading

from qgis.core import QgsApplication, QgsCoordinateReferenceSystem

from cns_planner.api.file_browser import browse
from cns_planner.api.server import ApiHandler, create_server
from cns_planner.application.app_context import ApplicationContext, RenderRequestTracker
from cns_planner.application.project_directory_service import ProjectDirectoryService
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.gis.map_data import DEFAULT_PATHS, MapData as _MapData
from cns_planner.gis.online_health import check_online_services as _check_online_services
from cns_planner.gis.qgis_runtime import QgisRuntime
from cns_planner.process_identity import build_identity as _build_identity
from cns_planner.tile_cache import TileCache

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "cns_planner" / "web"
SETTINGS = ROOT / "projects" / "map_sources.json"
DEFAULT_CONFIG = ROOT / "cns_planner" / "config" / "defaults.json"
AUTO_PROJECT_FILE = ROOT / "projects" / "current_project.json"
ACTIVE_PROJECT_FILE = AUTO_PROJECT_FILE
DEFAULTS = dict(DEFAULT_PATHS)
CRS = QgsCoordinateReferenceSystem("EPSG:3857")
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
TOKEN = secrets.token_urlsafe(24)

_RUNTIME = QgisRuntime()
TASKS = _RUNTIME.tasks
TILES = TileCache()
_RENDER_REQUESTS = RenderRequestTracker()
_MUTATION_LOCK = threading.RLock()
LATEST, LATEST_LOCK = _RENDER_REQUESTS.latest, _RENDER_REQUESTS.lock
WORKFLOW = DATA = APP_CONTEXT = None

#: 监听地址的**唯一**默认值：正式工作台始终是 127.0.0.1:8765。
DEFAULT_HOST, DEFAULT_PORT = "127.0.0.1", 8765


def listen_address():
    """本进程的监听地址。

    默认严格保持 ``127.0.0.1:8765``（正式工作台行为逐字节不变）。仅当显式设置
    ``CNS_MAP_PORT``（可选 ``CNS_MAP_HOST``）时才改用其它端口 —— 这样"另起一个
    **独立**验收实例"不必去抢 8765，也就不可能误连或误关正在运行的正式后端。
    端口取值非法时 fail-closed 报错，绝不静默退回 8765（那会把验收实例悄悄变成
    第二个正式实例）。
    """

    host = str(os.environ.get("CNS_MAP_HOST") or DEFAULT_HOST).strip() or DEFAULT_HOST
    raw = str(os.environ.get("CNS_MAP_PORT") or "").strip()
    if not raw:
        return host, DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"CNS_MAP_PORT 不是整数：{raw!r}") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError(f"CNS_MAP_PORT 超出合法范围：{port}")
    return host, port


def process_identity():
    """本次后端进程的最小运行身份（BUG-STARTUP-001）：pid/started_at/project_root
    取自本进程真实运行状态，git_commit 由启动器注入。
    """
    return _build_identity(ROOT)


def obsolete(query):
    return _RENDER_REQUESTS.obsolete(query)


def gis_call(action):
    return _RUNTIME.call(action)


def project_storage_metadata():
    return _project_directories().storage_metadata(ACTIVE_PROJECT_FILE)


def _active_data_sources_path():
    return _project_directories().data_sources_path(ACTIVE_PROJECT_FILE)


def persist_active_data_sources():
    _project_directories().persist_sources(ACTIVE_PROJECT_FILE, DATA)


def save_project_as(project_dir):
    global WORKFLOW, ACTIVE_PROJECT_FILE
    candidate, target = _project_directories().save_as(
        project_dir, WORKFLOW, ACTIVE_PROJECT_FILE, DATA
    )
    WORKFLOW, ACTIVE_PROJECT_FILE = candidate, target
    return DATA.metadata()


def open_project(project_dir):
    global WORKFLOW, ACTIVE_PROJECT_FILE
    candidate, target = _project_directories().open(project_dir, DATA)
    candidate.configure_reference_sources(DATA.paths)
    WORKFLOW, ACTIVE_PROJECT_FILE = candidate, target
    return DATA.metadata()


def replace_sources(paths):
    previous = dict(DATA.paths)
    DATA.load({**DATA.paths, **paths})
    WORKFLOW.register_source_paths(DATA.paths)
    WORKFLOW.configure_reference_sources(DATA.paths)
    changed = {name for name in paths if previous.get(name) != DATA.paths.get(name)}
    WORKFLOW.invalidate_grid_attributes(changed)
    if "basemap" in changed:
        WORKFLOW.invalidate("data")
    WORKFLOW.save()
    persist_active_data_sources()
    return DATA.metadata()


def check_online_services():
    return _check_online_services(DATA)


def _project_directories():
    return ProjectDirectoryService(AUTO_PROJECT_FILE, DEFAULT_CONFIG, DEFAULTS, WorkflowService)


class MapData(_MapData):
    """Compatibility constructor using the active module configuration."""

    def __init__(self):
        super().__init__(
            settings_path=SETTINGS, defaults=DEFAULTS, default_config=DEFAULT_CONFIG,
            token=TOKEN,
            workflow_provider=lambda: WORKFLOW.snapshot() if WORKFLOW else {},
            project_metadata_provider=project_storage_metadata,
            stale_checker=obsolete,
        )

    @classmethod
    def validate_candidate(cls, paths):
        return _MapData.validate_candidate(
            paths, settings_path=SETTINGS, default_config=DEFAULT_CONFIG, token=TOKEN
        )


class _CompatContext:
    """Expose legacy globals through the context expected by the new router."""

    root = ROOT
    static = STATIC
    default_config = DEFAULT_CONFIG
    token = TOKEN
    tiles = TILES
    render_requests = _RENDER_REQUESTS
    mutation_lock = _MUTATION_LOCK

    @property
    def workflow(self): return WORKFLOW

    @property
    def data(self): return DATA

    @property
    def health_identity(self): return process_identity()

    @property
    def qgis(self): return self

    def call(self, action): return gis_call(action)

    def save_project_as(self, project_dir): return save_project_as(project_dir)

    def open_project(self, project_dir): return open_project(project_dir)

    def replace_sources(self, paths): return replace_sources(paths)


class Handler(ApiHandler):
    @property
    def context(self):
        return _CompatContext()


def main():
    global APP_CONTEXT, WORKFLOW, DATA, ACTIVE_PROJECT_FILE
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    address = listen_address()
    application = QgsApplication([], False)
    application.initQgis()
    APP_CONTEXT = ApplicationContext(ROOT)
    WORKFLOW, DATA = APP_CONTEXT.workflow, APP_CONTEXT.data
    ACTIVE_PROJECT_FILE = APP_CONTEXT.active_project_file
    print("地图数据加载完成" if not DATA.error else DATA.error, flush=True)
    server = create_server(APP_CONTEXT, address=address)
    print(f"CNS 地图服务监听 http://{address[0]}:{address[1]}（pid={os.getpid()}）", flush=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        while True:
            APP_CONTEXT.qgis.process_once()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown(); server.server_close()


if __name__ == "__main__":
    main()
