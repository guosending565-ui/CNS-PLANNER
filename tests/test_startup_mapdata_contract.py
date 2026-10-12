"""BUG-STARTUP-006 回归：启动期的 MapData 接口契约与数据源状态接口。

本轮修复的根因是**失败路径的接口契约不完整**，而不是某一个数据源路径配置错误：

* ``MapData.load()`` 在 ``loader.load()`` 内部抛错时，只会把异常字符串记进
  ``self.error``，**不会**执行把 ``LoadedSources`` 投影到自身的 ``setattr`` 循环，
  于是 ``raster_info`` / ``terrain_info`` / ``terrain_dtm_info`` 等属性根本不存在；
* ``ApplicationContext.__init__`` 紧接着无条件读取 ``self.data.raster_info``，
  因此**任何**加载失败（数据源缺失、路径损坏、QGIS 工程坏掉）都会在 ``app_context``
  初始化时退化成 ``AttributeError: 'MapData' object has no attribute 'raster_info'``，
  把真实原因（例如"陆域掩膜：配置的路径不存在"）彻底掩盖掉。

这些用例断言的是**契约**而非具体数据源：

1. 加载失败时全部装载属性仍然存在，且缺省值只表示"未加载"；
2. ``metadata()`` 如实上报失败（fail-closed），绝不把失败伪装成"空数据"；
3. ``ApplicationContext`` 能构造成功、数据源状态接口可响应；
4. 初始化不写 ``ProjectState`` / artifact（任务元数据存储除外，单独断言）；
5. Terrain-only 与 Combined LOS 两个接口在缺真实数据时都 fail-closed；
6. 独立验收端口默认值不变，仅显式设置时才改。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytest.importorskip("qgis", reason="启动契约需要 PyQGIS（MapData / ApplicationContext）")

from cns_planner.gis.map_data import (  # noqa: E402
    LOAD_FAILED_ATTRIBUTE_DEFAULTS, LOADED_SOURCE_ATTRIBUTES, MapData,
)

_MISSING = str(Path("D:/nonexistent-cns-h2/does-not-exist"))


def _isolated_settings(tmp_path):
    """指向不存在文件的 ``map_sources.json``。

    不这样做就会读到仓库里那份**有效**配置（其路径覆盖 ``defaults``），
    于是"加载失败"这条路径根本不会被走到。
    """

    return tmp_path / "settings" / "map_sources.json"


def _broken_defaults():
    """把每个被校验的角色都指向不存在的文件（缺省路径同时也是失败面）。"""

    return {
        "basemap": _MISSING + ".qgz",
        "population": _MISSING + "_pop.tif",
        "terrain": _MISSING + "_dsm.tif",
        "terrain_dtm": _MISSING + "_dtm.tif",
        "buildings": _MISSING + "_buildings.gpkg",
        "building_grid": _MISSING + "_grid.gpkg",
        "land_mask": _MISSING + "_land.gpkg",
    }


def test_load_failure_keeps_the_full_attribute_contract(tmp_path):
    """加载失败后属性集必须完整；缺省值只表示"未加载"，不是"空数据"。"""

    data = MapData(settings_path=_isolated_settings(tmp_path), defaults=_broken_defaults())
    assert data.error, "加载失败必须如实记录原因，不能静默成功"
    missing = [name for name in LOADED_SOURCE_ATTRIBUTES if not hasattr(data, name)]
    assert missing == [], f"加载失败后属性契约必须完整，实际缺失：{missing}"
    # 关键：raster_info 这类"元数据"缺省是空字典（未加载），而不是被伪造成某个来源。
    for name in ("raster_info", "terrain_info", "terrain_dtm_info", "vector_info"):
        assert getattr(data, name) == {} == LOAD_FAILED_ATTRIBUTE_DEFAULTS[name]
    for name in ("project", "population", "terrain", "terrain_dtm"):
        assert getattr(data, name) is None
    for name in ("layers", "local_layers", "bounds", "hard_constraints"):
        assert getattr(data, name) == []
    assert data.loaded is None
    assert data.revision == 0, "失败不得推进 revision"


def test_load_failure_metadata_is_fail_closed_not_empty_data(tmp_path):
    """``metadata()`` 必须如实报错，并且数据源状态接口对缺失来源给出问题清单。"""

    data = MapData(settings_path=_isolated_settings(tmp_path), defaults=_broken_defaults())
    metadata = data.metadata()
    assert metadata["error"] == data.error and metadata["error"]
    assert metadata["layers"] == [] and not metadata["bounds"]
    assert metadata["population"] == {} and metadata["terrain"] == {}
    checks = metadata["path_checks"]
    assert checks["status"] == "warning"
    assert checks["problem_count"] > 0
    # 失败原因必须精确到角色，不能退化成"某个属性不存在"。
    assert all(problem["reason"] for problem in checks["problems"])
    assert "raster_info" not in json.dumps(checks, ensure_ascii=False)


def test_missing_loader_result_is_reported_not_silently_accepted(tmp_path):
    """加载器返回空结果时必须抛错，绝不被当成"已加载且为空"。"""

    data = MapData(settings_path=_isolated_settings(tmp_path), defaults=_broken_defaults())
    data.loader.load = lambda paths, persist=True: None
    with pytest.raises(ValueError):
        data.load({}, persist=False)


# --------------------------------------------------------------------- 端口契约

def test_listen_address_defaults_to_the_official_port(monkeypatch):
    """默认监听地址必须保持 127.0.0.1:8765（正式工作台行为不变）。"""

    from cns_planner import map_server

    monkeypatch.delenv("CNS_MAP_HOST", raising=False)
    monkeypatch.delenv("CNS_MAP_PORT", raising=False)
    assert map_server.listen_address() == ("127.0.0.1", 8765)


def test_listen_address_honours_explicit_acceptance_port(monkeypatch):
    """显式要求独立端口时才改地址；非法端口 fail-closed，绝不静默退回 8765。"""

    from cns_planner import map_server

    monkeypatch.setenv("CNS_MAP_PORT", "8801")
    assert map_server.listen_address() == ("127.0.0.1", 8801)
    monkeypatch.setenv("CNS_MAP_HOST", "127.0.0.2")
    assert map_server.listen_address() == ("127.0.0.2", 8801)
    for bad in ("abc", "0", "70000", "-1"):
        monkeypatch.setenv("CNS_MAP_PORT", bad)
        with pytest.raises(RuntimeError):
            map_server.listen_address()


# ------------------------------------------------------------- ApplicationContext

def _isolated_root(tmp_path):
    """搭一个只依赖仓库静态资源与默认配置的隔离根（不复制任何业务数据）。

    ``projects/`` 下一律**不留** ``current_project.json`` / ``data_sources.json``：
    这样 ApplicationContext 走的是"空项目 + 默认数据源建议值"的真实失败路径，
    而它的所有写入都落在 tmp_path 里。
    """

    root = tmp_path / "root"
    (root / "cns_planner" / "web").mkdir(parents=True)
    (root / "cns_planner" / "config").mkdir(parents=True)
    (root / "cns_planner" / "config" / "defaults.json").write_text(
        (ROOT / "cns_planner" / "config" / "defaults.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (root / "projects").mkdir(parents=True)
    # 隔离根显式登记一份"全部缺失"的来源配置：否则 MapData 会回落到仓库默认建议值，
    # 其中相对路径恰好能解析到真实文件，隔离根就不再是"没有真实数据"了。
    (root / "projects" / "map_sources.json").write_text(
        json.dumps(_broken_defaults(), ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return root


def _snapshot(root):
    return {
        str(path.relative_to(root)): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in root.rglob("*") if path.is_file()
    }


def test_application_context_starts_and_reports_fail_closed_data_sources(tmp_path):
    """启动必须成功；数据源状态接口必须可响应，且缺数据时 fail-closed。"""

    from cns_planner.application.app_context import ApplicationContext

    root = _isolated_root(tmp_path)
    before = _snapshot(root)
    context = ApplicationContext(root)

    # 1) 初始化成功，且 MapData 保留真实失败原因（不是 AttributeError）。
    assert context.data is not None
    assert context.data.error, "隔离根没有任何真实数据源，必须如实报错"
    assert "has no attribute" not in context.data.error
    for name in LOADED_SOURCE_ATTRIBUTES:
        assert hasattr(context.data, name)

    # 2) 数据源状态接口正常响应（这正是启动时崩溃的那个读取点）。
    assert context.data.raster_info == {} and context.data.terrain_info == {}
    assert context.data.terrain_dtm_info == {}
    details = context._source_details()
    # 三个栅格角色的 schema 必须存在且为"未加载"（None），而不是被伪造成 0。
    for role in ("population", "terrain", "terrain_dtm"):
        assert role in details, f"数据源状态接口必须包含 {role}"
        assert details[role]["schema"]["width"] is None

    # 3) 初始化不写 ProjectState / artifact：项目目录里不得出现状态文件或 artifacts。
    after = _snapshot(root)
    changed = {key for key in set(before) | set(after) if before.get(key) != after.get(key)}
    state_files = {
        key for key in changed
        if key.endswith("project_state.json") or key.endswith("current_project.json")
        or "/artifacts/" in key.replace("\\", "/")
    }
    assert state_files == set(), f"初始化不得写 ProjectState/artifact：{sorted(state_files)}"
    # 任务元数据存储是 heavy task 服务的独立存储，不属于 ProjectState/artifact。
    assert all(
        key.replace("\\", "/").startswith("projects/.cns-tasks/") for key in changed
    ), f"初始化只允许写任务元数据存储，实际改动：{sorted(changed)}"


def test_terrain_only_and_combined_los_endpoints_are_wired_and_fail_closed(tmp_path):
    """Terrain-only 与 Combined LOS 两个接口都必须可用；缺数据时一律 fail-closed。"""

    from cns_planner.application.app_context import ApplicationContext

    root = _isolated_root(tmp_path)
    context = ApplicationContext(root)
    service = context.workflow.radar_terrain_los_diagnostics_service

    terrain_status = service.status()
    assert isinstance(terrain_status, dict) and terrain_status

    combined_status = service.combined_status()
    assert combined_status["open_panel_never_computes_los"] is True
    assert combined_status["path_parameters_never_accepted"] is True

    combined = service.diagnose_combined({})
    assert combined["failure_never_rendered_as_clear"] is True
    assert combined["production_enforced_enabled"] is False
    assert combined["participates_in_milp"] is False
    details = service.combined_details({})
    assert details["recomputed"] is False
    # 隔离根里没有真实 DEM / GPKG / 航路：必须 failed，且打开前失败的收据是准确的 0。
    if combined["evidence_status"] == "failed":
        assert combined["failure_reason"]
        assert combined["rays"] == []
        assert combined["dem_open_count"] == 0
        assert combined["building_open_count"] == 0
        assert details["failure_reason"] == "detail_not_available"
        assert details["detail_page"] is None
