"""P16-V2 的工程假设候选池构建（只读，可审计）。

V2 的输入不是正式 ``candidate_sites``，而是 R5 的**工程假设候选池**。本模块把
``EngineeringAssumptionScenarioService`` 的 provider 装配收敛到一处，保证：

* 场景来自版本化配置（``cns_planner/config/engineering_assumptions/*.json``）；
* 陆海筛选、DEM 采样、参考设施与铁塔集合全部来自**登记来源**；
* 任何必需来源缺失时 fail-closed，绝不猜测坐标或高程；
* 只读：不写 ``ProjectState``、不写候选文件，构建结果只进 immutable snapshot。
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import gzip
import json
import os
from pathlib import Path

from ..reference_data import load_standardized_facilities
from ..domain.surface_classification import (
    default_surface_classification_policy, normalize_surface_classification_policy,
)
from ..gis.land_mask_source import (
    build_land_mask_source, land_mask_source_audit_facts, land_mask_source_identity,
)
from ..reference_data.ovobj_sites import load_ovobj_records
from .engineering_assumption_scenario_service import (
    EngineeringAssumptionScenarioService,
)

POOL_SCHEMA_VERSION = "p16-v2-scenario-pool/v1"
#: 池缓存文件的**外层**结构版本。它与池自身的 ``schema_version`` 分开：
#: 缓存是派生物，升级读取器时可以整体作废旧文件，而不影响池的语义契约。
CACHE_SCHEMA_VERSION = "p16-v2-pool-cache/v1"
#: 池构建结果进入 snapshot 的语义标记：工程假设、非正式状态。
POOL_SEMANTICS = "simulation_engineering_assumption_candidate_pool_not_formal_state"


class P16V2PoolUnavailable(ValueError):
    def __init__(self, message, *, code="p16_v2_pool_unavailable", detail=None):
        super().__init__(message)
        self.message = message
        self.code = str(code)
        self.detail = deepcopy(detail or {})


def _sha256_file(path):
    digest = sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _file_identity(path, *, code, label):
    """只读文件身份：路径 / 字节 / SHA-256。文件不可读时 fail-closed。"""

    source = Path(str(path or ""))
    if not source.is_file():
        raise P16V2PoolUnavailable(
            f"已登记的 {label} 不存在，拒绝在来源不可信时继续（P16-V2 不猜测来源）",
            code=code,
            detail={"path": str(source)},
        )
    try:
        size = source.stat().st_size
        digest = _sha256_file(source)
    except OSError as exc:
        raise P16V2PoolUnavailable(
            f"无法只读计算 {label} 的内容指纹",
            code=code,
            detail={"path": str(source), "error": type(exc).__name__},
        ) from exc
    return {"path": str(source), "bytes": int(size), "sha256": digest}


def external_source_identity(data_paths):
    """外部空间来源（FABDEM / 陆海掩膜 / .ovobj 补充站址）的提交时身份。

    提交阶段把这份身份写进 immutable snapshot，worker 在计算前**重新计算并比对**：
    源文件在提交后被替换时任务显式失败（``task_input_changed``），而不是"用新数据
    算出旧任务的结果"。这也是 :func:`scenario_identity` 与池缓存失效的依据。
    """

    paths = data_paths if isinstance(data_paths, dict) else {}
    terrain = str(paths.get("terrain_dtm") or "").strip()
    if not terrain:
        raise P16V2PoolUnavailable(
            "缺少 FABDEM terrain_dtm 配置，无法绑定外部空间来源",
            code="p16_v2_terrain_dtm_unavailable",
        )
    identity = {
        "terrain_dtm": _file_identity(
            terrain, code="p16_v2_terrain_dtm_unreadable", label="FABDEM terrain_dtm",
        )
    }
    mask = str(paths.get("land_mask") or "").strip()
    if not mask:
        #: V2 是**陆基**候选池：没有 canonical 陆海掩膜就不能把站点分成
        #: land / sea / coastal_uncertain。这里必须 fail-closed，绝不"静默关闭
        #: surface screening"——否则海上站点会以 unknown 混进可发布研究结果。
        raise P16V2PoolUnavailable(
            "缺少已登记的 canonical land_mask，拒绝静默关闭陆海筛选"
            "（P16-V2 不猜测 surface_class）",
            code="p16_v2_land_mask_missing",
            detail={"data_paths": sorted(paths)},
        )
    identity["land_mask"] = _file_identity(
        mask, code="p16_v2_land_mask_missing", label="canonical land_mask",
    )
    #: R7-B：V2 候选池的 648 身份口径包含 42 个陆地 ``.ovobj`` 地面/楼面补充站址。
    #: 该来源没有 CRS/塔高字段，但它是候选空间的一部分：文件缺失时若静默传
    #: ``ovobj_records_provider=None``，池会悄悄退化成 606 站/1212 动作，与 R5
    #: 证据口径不一致。因此把"已登记且可读"提升为构建前置条件。
    ovobj = str(paths.get("tower_ovobj") or "").strip()
    if not ovobj:
        raise P16V2PoolUnavailable(
            "缺少已登记的 .ovobj 补充站址来源，拒绝静默生成缺少 42 个补充站址的候选池",
            code="p16_v2_ovobj_missing",
            detail={"data_paths": sorted(paths)},
        )
    identity["tower_ovobj"] = _file_identity(
        ovobj, code="p16_v2_ovobj_unreadable", label=".ovobj 补充站址来源",
    )
    return identity


def reference_facilities_identity():
    """标准化参考设施注册表的版本身份（含 4 个 collection 文件的 SHA）。

    这些文件由 worker 在**计算时**读取，不属于 ProjectState 快照；不把它们的
    内容身份纳入池缓存键，就会在文件被替换后复用旧候选池。
    """

    try:
        facilities = load_standardized_facilities()
    except Exception as exc:  # noqa: BLE001 - 来源不可读时如实记录，不中断提交
        return {"status": "unavailable", "error": type(exc).__name__, "files": []}
    integrity = facilities.get("integrity") or {}
    return {
        "status": facilities.get("status"),
        "registry_id": facilities.get("registry_id"),
        "version": facilities.get("version"),
        "integrity_status": integrity.get("status"),
        "files": [
            {
                "key": item.get("key"),
                "file": item.get("file"),
                "sha256": item.get("actual_sha256"),
                "status": item.get("status"),
            }
            for item in integrity.get("files") or []
        ],
    }


def require_reference_facilities(identity):
    """参考设施注册表必须**已完成可信校验**；读取失败不等于校验通过。

    ``load_standardized_facilities`` 在目录/清单/集合文件缺失时返回
    ``status="blocked"``（而不是抛异常）。旧实现只比较文件 SHA 列表，两个
    ``unavailable`` 会得到相同的空列表，从而把"来源不可读"误判成"没有变化"。
    这里把 status / integrity / 每个文件的校验结果都变成显式前置条件。
    """

    identity = identity if isinstance(identity, dict) else {}
    status = str(identity.get("status") or "")
    integrity = str(identity.get("integrity_status") or "")
    files = identity.get("files") if isinstance(identity.get("files"), list) else []
    bad_files = sorted(
        str(item.get("key") or item.get("file") or "")
        for item in files
        if not isinstance(item, dict) or str(item.get("status") or "") != "passed"
    )
    if status != "passed" or integrity != "passed" or not files or bad_files:
        raise P16V2PoolUnavailable(
            "标准化参考设施注册表不可用或完整性校验未通过，拒绝在来源不可信时生成候选池",
            code="p16_v2_reference_facilities_unavailable",
            detail={
                "status": status or None,
                "integrity_status": integrity or None,
                "file_count": len(files),
                "bad_files": bad_files,
                "error": identity.get("error"),
            },
        )
    return identity


class _GdalNearestSampler:
    """只读 FABDEM 最近像元采样（与 R5 证据链同一方法：``nearest_pixel``）。

    为什么用 GDAL 而不是 rasterio：重任务 worker 运行在 QGIS 自带的 Python 上，
    那里**没有** rasterio，但一定有 GDAL。R5 研究脚本用 rasterio 只是为了离线复算；
    生产入口若依赖它，就会在 worker 里以 ``dem_sampler_unavailable`` 失败。
    """

    def __init__(self, path):
        try:
            from osgeo import gdal
        except ImportError as exc:  # pragma: no cover - 运行环境缺依赖
            raise P16V2PoolUnavailable(
                "运行环境缺少 GDAL，无法采样 FABDEM 地形（P16-V2 不猜测高程）",
                code="p16_v2_dem_sampler_unavailable",
            ) from exc
        self.gdal = gdal
        self.path = Path(path)
        self.dataset = gdal.Open(str(self.path), gdal.GA_ReadOnly)
        if self.dataset is None:
            raise P16V2PoolUnavailable(
                "无法以只读方式打开 FABDEM terrain_dtm",
                code="p16_v2_terrain_dtm_unreadable",
                detail={"path": str(self.path)},
            )
        self.width = int(self.dataset.RasterXSize)
        self.height = int(self.dataset.RasterYSize)
        self.band = self.dataset.GetRasterBand(1)
        self.transform = self.dataset.GetGeoTransform()
        inverse = gdal.InvGeoTransform(self.transform)
        if isinstance(inverse, tuple) and len(inverse) == 2 and isinstance(inverse[0], (bool, int)):
            ok, inverse = inverse
            if not ok:
                raise P16V2PoolUnavailable(
                    "FABDEM DTM 仿射变换不可逆",
                    code="p16_v2_terrain_dtm_unreadable",
                )
        self.inverse = inverse
        self.nodata = self.band.GetNoDataValue()

    def __call__(self, lon, lat):
        import math

        pixel, line = self.gdal.ApplyGeoTransform(
            self.inverse, float(lon), float(lat),
        )
        column, row = int(math.floor(pixel)), int(math.floor(line))
        if not (0 <= column < self.width and 0 <= row < self.height):
            return None
        block = self.band.ReadAsArray(column, row, 1, 1)
        if block is None:
            return None
        value = float(block[0][0])
        if not math.isfinite(value):
            return None
        if self.nodata is not None and value == float(self.nodata):
            return None
        return value

    def describe(self):
        return {
            "source": "FABDEM_DTM_30m",
            "path": str(self.path),
            "crs": self.dataset.GetProjectionRef() or None,
            "vertical_reference": "egm2008_orthometric",
            "method": "nearest_pixel",
            "width": self.width,
            "height": self.height,
            "nodata": self.nodata,
        }


def _resolve_land_mask(data_paths):
    path = str((data_paths or {}).get("land_mask") or "").strip()
    if not path:
        raise P16V2PoolUnavailable(
            "缺少已登记的 canonical land_mask，拒绝在静默关闭陆海筛选时生成候选池",
            code="p16_v2_land_mask_missing",
        )
    mask_path = Path(path)
    if not mask_path.is_file():
        raise P16V2PoolUnavailable(
            "已登记的 land_mask 文件不存在，拒绝在来源不可信时生成陆基候选",
            code="p16_v2_land_mask_missing",
            detail={"path": str(mask_path)},
        )
    return mask_path, None


def _load_ovobj_source(data_paths, *, source_identity):
    """只读解析已登记的 ``.ovobj`` 补充站址来源，并与提交时身份交叉校验。

    返回 ``(records, meta)``。解析失败、记录为空、或文件在"算身份"与"读内容"
    之间被替换时都 fail-closed：候选池宁可失败，也不能少 42 个站址却声称完整。
    """

    path = str((data_paths or {}).get("tower_ovobj") or "").strip()
    if not path:
        raise P16V2PoolUnavailable(
            "缺少已登记的 .ovobj 补充站址来源，拒绝静默生成缺少 42 个补充站址的候选池",
            code="p16_v2_ovobj_missing",
        )
    source_path = Path(path)
    if not source_path.is_file():
        raise P16V2PoolUnavailable(
            "已登记的 .ovobj 补充站址来源不存在，拒绝在来源不可信时继续",
            code="p16_v2_ovobj_unreadable",
            detail={"path": str(source_path)},
        )
    try:
        records = load_ovobj_records(source_path)
    except (OSError, ValueError) as exc:
        raise P16V2PoolUnavailable(
            "无法解析已登记的 .ovobj 补充站址来源",
            code="p16_v2_ovobj_unreadable",
            detail={"path": str(source_path), "error": type(exc).__name__},
        ) from exc
    expected = ((source_identity or {}).get("tower_ovobj") or {}).get("sha256")
    observed = str(records.get("source_sha256") or "").lower()
    if not expected or observed != str(expected).lower():
        raise P16V2PoolUnavailable(
            ".ovobj 补充站址来源在构建过程中发生变化，拒绝混用两份内容",
            code="p16_v2_ovobj_changed",
            detail={"path": str(source_path), "expected": expected, "observed": observed or None},
        )
    rows = [item for item in (records.get("records") or []) if isinstance(item, dict)]
    if not rows:
        raise P16V2PoolUnavailable(
            "已登记的 .ovobj 补充站址来源解析结果为空，拒绝把空来源当成完整候选池",
            code="p16_v2_ovobj_empty",
            detail={"path": str(source_path), "bytes": source_path.stat().st_size},
        )
    meta = {
        "path": str(source_path),
        "sha256": observed,
        "record_count": int(records.get("record_count") or len(rows)),
        "unique_name_count": int(
            records.get("unique_name_count") or len({item.get("name") for item in rows})
        ),
        "semantics": "read_only_ovobj_supplementary_sites_no_crs_no_tower_height",
    }
    return records, meta


def _land_mask_context(mask_path, state):
    policy = normalize_surface_classification_policy(
        state.get("surface_classification_policy") or default_surface_classification_policy()
    )
    source = build_land_mask_source(str(mask_path), policy)
    prepared = source.prepare()
    describe = source.describe() if hasattr(source, "describe") else {}
    if prepared is None:
        raise P16V2PoolUnavailable(
            "canonical land_mask 不可用，无法生成陆基候选",
            code="p16_v2_land_mask_unavailable",
            detail={"reason": (describe or {}).get("reason") or "unknown"},
        )
    mask_sha = _sha256_file(mask_path)
    return source, policy, {
        "path": str(mask_path),
        "sha256": mask_sha,
        "source_identity": land_mask_source_identity(describe, content_sha256=mask_sha),
        "audit_facts": land_mask_source_audit_facts(describe, source_path=str(mask_path)),
        "semantics": "canonical_land_mask_registered_source",
    }


def scenario_identity(
    state, scenario, data_paths, *, facility_identity=None, source_identity=None,
):
    """场景 / 来源 / 状态事实的确定性指纹（用于池缓存与审计）。

    ``facility_identity`` / ``source_identity`` 允许调用方复用已经计算并校验过的
    身份，避免同一次构建里重复读大文件；省略时按同一实现重新计算（同样 fail-closed）。
    """

    from .p16_v2_service import source_fingerprints

    if source_identity is None:
        source_identity = external_source_identity(data_paths)
    if facility_identity is None:
        facility_identity = reference_facilities_identity()

    return sha256(json.dumps({
        "schema": POOL_SCHEMA_VERSION,
        "scenario": scenario,
        "sources": source_fingerprints(),
        #: R7-A1：缓存键绑定**内容身份**（路径 / 字节 / SHA-256），而不是只绑一个
        #: 可能为 None 的摘要。DEM 或陆海掩膜被替换后必须重新构建候选池。
        "external_sources": source_identity,
        #: R7-A：标准化参考设施由 worker 在计算时读取，内容变化必须让池缓存失效。
        "reference_facilities": facility_identity,
        "towers": (state.get("tower_colocation_candidates") or {}).get("input_fingerprint")
        or (state.get("tower_colocation_candidates") or {}).get("counts"),
        "landing_sites": (state.get("reference_landing_sites") or {}).get("collection_id")
        or len((state.get("reference_landing_sites") or {}).get("items") or []),
        "device_catalog": (state.get("device_catalog") or {}).get("collection_id")
        or len((state.get("device_catalog") or {}).get("items") or []),
        "surface_policy": state.get("surface_classification_policy"),
        "revision": state.get("revision"),
    }, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def _load_cached_pool(cache_path, identity):
    """读取并**结构校验**池缓存；任何损坏/不完整/身份不符都当作缓存未命中。

    真实写入结构是 ``{"pool": pool, "meta": meta}``（外加缓存结构版本）。旧读取器
    检查顶层 ``actions``，因此永远读不到；这里改为校验真实结构，并额外要求池与
    meta 的身份、池 schema、actions/sites/counts 都存在。损坏缓存绝不部分采用，
    也绝不把异常抛给调用方——直接重新冷构建。
    """

    try:
        with gzip.open(cache_path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, EOFError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("cache_schema_version") != CACHE_SCHEMA_VERSION:
        return None
    pool = payload.get("pool")
    meta = payload.get("meta")
    if not isinstance(pool, dict) or not isinstance(meta, dict):
        return None
    if pool.get("schema_version") != POOL_SCHEMA_VERSION:
        return None
    if str(pool.get("v2_pool_identity") or "") != identity:
        return None
    if not isinstance(pool.get("actions"), list) or not isinstance(pool.get("sites"), list):
        return None
    if not isinstance(pool.get("counts"), dict):
        return None
    if str(meta.get("identity") or "") != identity:
        return None
    if not isinstance(meta.get("counts"), dict):
        return None
    if not isinstance(meta.get("land_mask"), dict) or not isinstance(meta.get("dem"), dict):
        return None
    #: R7-B：旧缓存没有 ovobj 身份，必须整体重建，绝不能把 606 站口径当热命中。
    if not isinstance(meta.get("ovobj"), dict):
        return None
    return pool, meta


def _store_cached_pool(cache_path, pool, meta):
    """原子写入池缓存：先写同目录临时文件，再 ``os.replace``。

    这样并发 worker 或进程中断只会留下完整旧缓存或完整新缓存，不会读到半截 gzip。
    """

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache_path.with_name(f"{cache_path.name}.{os.getpid()}.tmp")
    try:
        with gzip.open(temporary, "wt", encoding="utf-8") as handle:
            json.dump({
                "cache_schema_version": CACHE_SCHEMA_VERSION,
                "pool": pool,
                "meta": meta,
            }, handle, ensure_ascii=False)
        os.replace(temporary, cache_path)
        return cache_path
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass


def build_scenario_pool(
    *, state, scenario, data_paths, cache_dir=None,
    on_progress=None, on_progress_detail=None, cancel_check=None,
):
    """构建工程假设候选池。

    ``scenario`` 必须是已归一化的场景对象（由调用方从版本化配置加载）。
    返回 ``(pool, meta)``：``pool`` 结构与 R5 证据文件一致；``meta`` 记录来源、
    缓存命中与耗时，供前端如实展示"池准备"阶段。
    """

    import time

    started = time.perf_counter()
    #: R7-A1：来源身份与参考设施身份都是**构建前置条件**，不是"事后审计"。
    #: 任何必需来源缺失/不可读都在进入缓存与计算之前 fail-closed。
    source_identity = external_source_identity(data_paths)
    facility_identity = reference_facilities_identity()
    require_reference_facilities(facility_identity)
    identity = scenario_identity(
        state, scenario, data_paths,
        facility_identity=facility_identity, source_identity=source_identity,
    )
    cache_path = None
    if cache_dir is not None:
        cache_path = Path(cache_dir) / f"pool-{identity[:24]}.json.gz"
        if cache_path.is_file():
            cached = _load_cached_pool(cache_path, identity)
            if cached is not None:
                pool, cached_meta = cached
                meta = deepcopy(cached_meta)
                meta.update({
                    "cache": "warm",
                    "path": str(cache_path),
                    "identity": identity,
                    "seconds": round(time.perf_counter() - started, 3),
                })
                #: 热命中也要尊重取消：即使用缓存，也不让已取消的任务继续发布。
                if callable(cancel_check):
                    cancel_check()
                return pool, meta

    def report(message, **detail):
        if callable(on_progress_detail):
            try:
                on_progress_detail({"stage": "pool", "message": message, **detail})
            except Exception:  # noqa: BLE001
                pass
        if callable(cancel_check):
            cancel_check()

    report("正在校验工程假设场景与真实空间数据来源")

    terrain_dtm = str((data_paths or {}).get("terrain_dtm") or "").strip()
    if not terrain_dtm or not Path(terrain_dtm).is_file():
        raise P16V2PoolUnavailable(
            "缺少可用的 FABDEM terrain_dtm，无法生成工程假设候选池",
            code="p16_v2_terrain_dtm_unavailable",
            detail={"path": terrain_dtm or None},
        )
    sampler = _GdalNearestSampler(terrain_dtm)
    report("正在读取 DEM 来源身份", completed=0, total=1, unit="数据源")

    mask_path, _ = _resolve_land_mask(data_paths)
    land_mask_source, surface_policy, land_mask_context = _land_mask_context(mask_path, state)
    report("正在解析陆海筛选来源", completed=0, total=1, unit="数据源")

    ovobj_records, ovobj_context = _load_ovobj_source(
        data_paths, source_identity=source_identity,
    )
    report(
        "正在解析 .ovobj 补充站址来源", completed=0, total=1, unit="数据源",
        labels=ovobj_context["record_count"],
    )

    service = EngineeringAssumptionScenarioService(
        state_provider=lambda: state,
        standardized_facilities_provider=load_standardized_facilities,
        landing_sites_provider=lambda: state.get("reference_landing_sites") or {},
        tower_colocation_provider=lambda: state.get("tower_colocation_candidates") or {},
        device_catalog_provider=lambda: state.get("device_catalog") or {},
        dem_sampler=sampler,
        dem_descriptor=sampler.describe(),
        ovobj_records_provider=lambda: ovobj_records,
        land_mask_source=land_mask_source,
        surface_classification_policy=surface_policy,
        land_mask_context=land_mask_context,
    )
    report("正在生成工程假设候选池（塔 / 着陆点 / 标准设施 / .ovobj 补充站址 / 显式假设点）")
    pool = service.build_candidate_pool(scenario)
    if callable(cancel_check):
        cancel_check()
    counts = pool.get("counts") or {}
    pool = dict(pool)
    pool["schema_version"] = POOL_SCHEMA_VERSION
    pool["semantics"] = POOL_SEMANTICS
    pool["v2_pool_identity"] = identity
    meta = {
        "cache": "cold", "identity": identity,
        "seconds": round(time.perf_counter() - started, 3),
        "counts": deepcopy(counts),
        "land_mask": deepcopy(land_mask_context),
        "ovobj": deepcopy(ovobj_context),
        "dem": sampler.describe(),
        "source": deepcopy(pool.get("sources")),
        #: 任务记录 / 结果 provenance 可直接引用这两份内容身份。
        "external_sources": deepcopy(source_identity),
        "reference_facilities": deepcopy(facility_identity),
    }
    if cache_path is not None:
        try:
            _store_cached_pool(cache_path, pool, meta)
            meta["path"] = str(cache_path)
        except OSError:
            pass
    report(
        "工程假设候选池已就绪", completed=1, total=1, unit="候选池",
        counts=deepcopy(counts), cache="cold",
    )
    return pool, meta


__all__ = [
    "CACHE_SCHEMA_VERSION", "POOL_SCHEMA_VERSION", "P16V2PoolUnavailable",
    "build_scenario_pool", "external_source_identity",
    "reference_facilities_identity", "require_reference_facilities",
    "scenario_identity",
]
