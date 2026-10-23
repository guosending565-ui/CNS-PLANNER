"""P16-V2：工程假设候选池上的**只读**快速设施规划预算服务。

本模块是 R6-B 研究成果的生产化封装。它只做三件事：

1. 在**只读** ProjectState 副本上运行既有的 P14/P15 基线与精确重算；
2. 用三态 provider 覆盖矩阵 + ``FastPlannerV2`` 搜索得到候选动作集合；
3. 把选中方案交给**未修改的**正式 P14/P15 链复算一次，给出真实覆盖结论。

硬边界（与 R5/R6 的工程假设语义一致）：

* 绝不写 ``ProjectState`` / ``candidate_sites`` / ``cns_corridor_site_plan``；
* 绝不调用 ``session.save()``（会话由 :class:`ReadOnlyProjectSession` 提供，调用即报错）；
* 不修改 V1 的 P16 算法、P14/P15 算法或任何 authority；
* 结果一律带 ``assumption_only=True`` / ``formal_write=False``，**不得**进入正式候选或 Apply。

缺失即失败：输入结构不满足 V2 的前置条件时抛 :class:`P16V2Unavailable`，
并带明确 ``code``，绝不猜测默认坐标、默认覆盖要求或默认阈值。
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import time

import numpy as np

from ..algorithms.corridor.v1 import CNSServiceCorridorV1
from ..algorithms.corridor_gap.v1 import CNSCorridorGapAnalyzerV1
from ..algorithms.coverage.geometric_3d import (
    build_geometric_providers, evaluate_geometry_point,
)
from ..algorithms.service_capability.v1 import (
    build_provider_devices, evaluate_capability_point, prepare_capability_context,
)
from ..domain.corridor_site_planning import normalize_corridor_site_planning_policy
from ..domain.geodesy import distance_m
from ..site_planner.corridor_reuse_first_v2 import CorridorReuseFirstSitePlannerV2
from ..site_planner.fast_coverage_v2 import (
    VERSION as COVERAGE_VERSION, CoverageMatrix, max_continuous_deficit,
)
from ..site_planner.fast_search_v2 import (
    COST_POLICY_VERSION, REUSE_COST, FastPlannerV2,
)
from .corridor_site_planning_service import (
    _apply_cumulative_action, _continuous_service_acceptance, _impact, _targets,
)
from .engineering_assumption_p16_adapter import (
    P16_ADAPTER_SCHEMA_VERSION, ReadOnlyProjectSession, ScenarioP16PlanningService,
    _scenario_source_statistics, dedupe_candidate_pool_by_physical_site,
    validate_scenario_p16_inputs,
)
from .planning_evidence_service import (
    aircraft_profile_with_evidence, device_catalog_with_evidence,
)


V2_SERVICE_VERSION = "p16-v2-service-1.0"
V2_SCHEMA_VERSION = "p16-fast-plan-v2-service/v1"

#: 真实执行阶段（顺序即前端展示顺序）。阶段键必须稳定，前端按它渲染。
STAGES = (
    ("preflight", "预检"),
    ("baseline", "基线 P14/P15 与门槛解析"),
    ("coverage_matrix", "覆盖矩阵准备"),
    ("search", "候选组合搜索"),
    ("exact_eval", "精确 P14/P15 评价"),
    ("result", "结果生成"),
)
STAGE_LABELS = dict(STAGES)

#: 每个阶段在总进度中的起点（终点 = 下一阶段起点）。这些是**阶段边界**，
#: 不是"估算的完成度"：阶段内部进度一律由真实已完成量 / 总量决定。
STAGE_START = {
    "preflight": 0.0,
    "baseline": 0.03,
    "coverage_matrix": 0.10,
    "search": 0.70,
    "exact_eval": 0.88,
    "result": 0.97,
}

#: 矩阵缓存指纹只覆盖真正影响矩阵的源码（逐字节），避免整仓哈希的开销。
_MATRIX_SOURCE_FILES = (
    "cns_planner/site_planner/fast_coverage_v2.py",
    "cns_planner/site_planner/fast_search_v2.py",
    "cns_planner/application/p16_v2_service.py",
    "cns_planner/application/engineering_assumption_p16_adapter.py",
    "cns_planner/application/corridor_site_planning_service.py",
    "cns_planner/algorithms/coverage/geometric_3d.py",
    "cns_planner/algorithms/service_capability/v1.py",
)
_SERVICE_CODES = ("C", "S")
_SERVICE_NAMES = {"C": "communication", "S": "surveillance"}
_SERVICE_KEYS = {"C": "C:communication", "S": "S:rid_cooperative"}


class P16V2Unavailable(ValueError):
    """V2 无法在本份输入上运行（缺失 / 结构不支持 / 超预算）。"""

    def __init__(self, message, *, code="p16_v2_unavailable", detail=None):
        super().__init__(message)
        self.message = message
        self.code = str(code)
        self.detail = deepcopy(detail or {})


def _repo_root():
    return Path(__file__).resolve().parents[2]


def _sha256_file(path):
    digest = sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_sha256(value):
    payload = json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str,
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def source_fingerprints(root=None):
    """影响矩阵的源码指纹（只读、确定性）。"""

    base = Path(root) if root else _repo_root()
    result = {}
    for relative in _MATRIX_SOURCE_FILES:
        path = base / relative
        result[relative] = _sha256_file(path) if path.is_file() else None
    return result


def physical_groups(actions, distance_limit_m=2.0):
    """保守近邻计数分组：坐标不改、身份不改，只把"未证明独立"的站址并组。"""

    sites = {str(a["distinct_site_id"]): a["coordinate"] for a in actions}
    parent = {site: site for site in sites}
    near = []

    def find(value):
        while parent[value] != value:
            value = parent[value]
        return value

    ids = sorted(sites)
    for index, left in enumerate(ids):
        for right in ids[index + 1:]:
            a, b = sites[left], sites[right]
            # 便宜的角包围盒预筛，再走 canonical 距离。
            if abs(a[0] - b[0]) > 0.00003 or abs(a[1] - b[1]) > 0.00003:
                continue
            value = distance_m(a, b)
            if value <= float(distance_limit_m):
                parent[find(right)] = find(left)
                near.append({"left": left, "right": right, "distance_m": value})
    return [find(str(a["distinct_site_id"])) for a in actions], near


class P16V2Service:
    """一次 V2 只读规划运行。实例只持有一份只读 state 与候选池。"""

    algorithm_id = FastPlannerV2.algorithm_id
    algorithm_version = FastPlannerV2.algorithm_version

    def __init__(
        self, state, pool, *, cache_dir=None, exact_evaluation_budget=50,
        near_identity_distance_m=2.0, scarcity_powers=(0.0, 0.5, 1.0),
    ):
        if not isinstance(state, dict) or not state:
            raise P16V2Unavailable(
                "项目状态不可读，无法运行 P16-V2", code="p16_v2_state_unavailable",
            )
        if not isinstance(pool, dict) or not isinstance(pool.get("actions"), list):
            raise P16V2Unavailable(
                "缺少工程假设候选池（或结构不支持），无法运行 P16-V2",
                code="p16_v2_candidate_pool_unavailable",
            )
        self.state = state
        self.pool = pool
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.exact_evaluation_budget = int(exact_evaluation_budget)
        self.near_identity_distance_m = float(near_identity_distance_m)
        self.scarcity_powers = tuple(float(item) for item in scarcity_powers)
        self._on_progress = None
        self._on_detail = None
        self._cancel_check = None
        self._stage = None
        self._stage_started = None
        self._started = None
        self._timings = {}
        self._stage_timings = []
        self.performance = {
            "full_corridor_rerun_count": 0, "p14_seconds": 0.0, "p15_seconds": 0.0,
        }
        self.exact_cache = {}

    # ---- 进度 / 取消 ---------------------------------------------------------

    def _enter(self, stage, message, **detail):
        now = time.perf_counter()
        if self._stage is not None and self._stage_started is not None:
            self._stage_timings.append({
                "stage": self._stage, "label": STAGE_LABELS.get(self._stage, self._stage),
                "seconds": round(now - self._stage_started, 3),
            })
        self._stage = stage
        self._stage_started = now
        self._timings[stage] = 0.0
        self._report(message, **detail)

    def _report(self, message, *, completed=None, total=None, unit=None, **extra):
        detail = {
            "stage": self._stage,
            "stage_label": STAGE_LABELS.get(self._stage, self._stage),
            "stage_started_at_progress": STAGE_START.get(self._stage, 0.0),
            "elapsed_seconds": round(
                (time.perf_counter() - self._started) if self._started else 0.0, 3,
            ),
            "stage_elapsed_seconds": round(
                (time.perf_counter() - self._stage_started) if self._stage_started else 0.0, 3,
            ),
        }
        if completed is not None:
            detail["completed"] = int(completed)
        if total is not None:
            detail["total"] = int(total)
        if unit:
            detail["unit"] = str(unit)
        detail.update(extra)
        if callable(self._on_detail):
            try:
                self._on_detail(deepcopy(detail))
            except Exception:  # noqa: BLE001 - 进度上报失败不掩盖计算
                pass
        if callable(self._on_progress):
            value = self._value_for(detail)
            try:
                self._on_progress(value, str(message))
            except TypeError:
                self._on_progress(value)
            except Exception:  # noqa: BLE001
                pass
        if callable(self._cancel_check):
            self._cancel_check()

    def _value_for(self, detail):
        stage = detail.get("stage") or "preflight"
        start = STAGE_START.get(stage, 0.0)
        end = STAGE_START.get(
            STAGES[min(len(STAGES) - 1, [item[0] for item in STAGES].index(stage) + 1)][0],
            1.0,
        )
        completed, total = detail.get("completed"), detail.get("total")
        ratio = 0.0
        if isinstance(completed, (int, float)) and isinstance(total, (int, float)) and total > 0:
            ratio = max(0.0, min(1.0, float(completed) / float(total)))
        return max(0.0, min(1.0, start + (end - start) * ratio))

    # ---- 入口 ---------------------------------------------------------------

    def run(self, *, on_progress=None, on_progress_detail=None, cancel_check=None):
        self._started = time.perf_counter()
        self._on_progress = on_progress
        self._on_detail = on_progress_detail
        self._cancel_check = cancel_check if callable(cancel_check) else None
        self._enter("preflight", "正在检查工程假设候选池与 P14/P15 前置条件")

        deduped, validation = self._preflight()
        self._enter(
            "baseline", "正在运行基线 P14/P15 并解析连续服务门槛",
            total=3, unit="基线步骤", completed=0,
        )
        baseline = self._baseline(deduped, validation)
        self._enter(
            "coverage_matrix", "正在构建三态覆盖矩阵",
            total=len(self.actions) + len(self._base_rows), unit="矩阵行", completed=0,
        )
        matrix, matrix_info = self._coverage_matrix(baseline)
        self._enter("search", "正在搜索候选组合", total=0, unit="组合")
        search = self._search(matrix)
        self._enter("exact_eval", "正在用正式 P14/P15 复核选中方案", total=1, unit="方案", completed=0)
        exact = self._exact_evaluate(matrix, search["selected"], baseline)
        self._enter("result", "正在生成只读结果")
        result = self._assemble(
            deduped, validation, baseline, matrix, matrix_info, search, exact,
        )
        self._timings["total_s"] = round(time.perf_counter() - self._started, 3)
        self._stage_timings.append({
            "stage": "result", "label": STAGE_LABELS["result"],
            "seconds": round(time.perf_counter() - self._stage_started, 3),
        })
        result["timings"] = deepcopy(self._timings)
        result["stages"] = deepcopy(self._stage_timings)
        result["timings"]["total_s"] = round(time.perf_counter() - self._started, 3)
        if callable(self._on_progress):
            try:
                self._on_progress(1.0, "P16-V2 计算完成（工程假设，只读）")
            except Exception:  # noqa: BLE001
                pass
        return result

    # ---- 预检 ---------------------------------------------------------------

    def _preflight(self):
        deduped = dedupe_candidate_pool_by_physical_site(self.pool)
        validation = validate_scenario_p16_inputs(self.pool, deduped, self.state)
        if validation.get("status") != "passed":
            raise P16V2Unavailable(
                "工程假设候选池未通过 P16 输入契约校验："
                + "；".join(str(item) for item in (validation.get("errors") or [])[:3]),
                code="p16_v2_preflight_blocked",
                detail={"contract_validation": validation, "dedupe": deduped.get("counts")},
            )
        actions = [
            item for item in deduped.get("actions") or []
            if item.get("scenario_eligible") and (item.get("eligibility") or {}).get("status") == "eligible"
        ]
        if not actions:
            raise P16V2Unavailable(
                "工程假设候选池中没有可用于 P16 的合格 C/S 候选动作",
                code="p16_v2_no_eligible_actions",
                detail={"dedupe": deduped.get("counts")},
            )
        policy = normalize_corridor_site_planning_policy(
            self.state.get("corridor_site_planning_policy") or {},
        )
        cap = ((policy.get("parameters") or {}).get("max_selected_interventions"))
        if not isinstance(cap, (int, float)) or int(cap) <= 0:
            raise P16V2Unavailable(
                "走廊复用优先规划策略未给出有效的最大动作数（cap）",
                code="p16_v2_cap_unavailable",
            )
        self.actions = actions
        self.deduped = deduped
        self.validation = validation
        self.policy = policy
        self.cap = int(cap)
        return deduped, validation

    # ---- 基线 ---------------------------------------------------------------

    def _baseline(self, deduped, validation):
        self.session = ReadOnlyProjectSession(self.state)
        self.service = ScenarioP16PlanningService(
            self.session,
            CorridorReuseFirstSitePlannerV2(),
            CNSServiceCorridorV1(),
            CNSCorridorGapAnalyzerV1(),
            actions=self.actions,
            candidate_source_statistics=_scenario_source_statistics(deduped, validation),
        )
        self._report("正在准备工作区网格单元", completed=1, total=3)
        self.prepared = self.service._prepared_cells(self.state)
        self.continuous_inputs = self.service._continuous_inputs(self.state) or {}
        threshold = self.continuous_inputs.get("threshold_m")
        speed = self.continuous_inputs.get("route_speed_mps")
        if threshold is None or speed is None:
            raise P16V2Unavailable(
                "连续服务门槛或航路速度不可判定，P16-V2 不猜测默认值",
                code="p16_v2_continuous_threshold_unavailable",
                detail={"continuous_inputs": self.continuous_inputs},
            )
        self.threshold_m = float(threshold)
        self.route_speed_mps = float(speed)
        self.aircraft_id = str(
            self.continuous_inputs.get("aircraft_id")
            or (self.state.get("selected_aircraft_profile_id") or "")
        )
        self.base = self.state.get("existing_cns_facilities") or {}
        self.catalog = device_catalog_with_evidence(self.state)
        self.aircraft = aircraft_profile_with_evidence(self.state)
        self.profiles = (
            (self.state.get("spatial_3d") or {}).get("site_vertical_profiles") or {}
        )
        self._report("正在运行基线 P14/P15", completed=2, total=3)
        started = time.perf_counter()
        corridor, gap = self.service._rerun(
            self.base, prepared_cells=self.prepared, performance=self.performance,
        )
        self._timings["baseline_s"] = round(time.perf_counter() - started, 3)
        routes = corridor.get("routes") or []
        if len(routes) != 1:
            raise P16V2Unavailable(
                "P16-V2 当前只支持单航路场景（与 R6-B 的验收口径一致），"
                f"当前 P14 结果包含 {len(routes)} 条航路",
                code="p16_v2_multi_route_unsupported",
                detail={"route_count": len(routes)},
            )
        self.baseline_corridor = corridor
        self.baseline_gap = gap
        self.voxels = list(routes[0].get("voxels") or [])
        gap_routes = gap.get("routes") or []
        if not gap_routes:
            raise P16V2Unavailable(
                "P15 能力缺口结果不含航路，无法构造覆盖矩阵",
                code="p16_v2_gap_routes_unavailable",
            )
        self.gap_voxels = list(gap_routes[0].get("voxels") or [])
        self.route_length_m = float(routes[0].get("route_length_m") or 0.0)
        if not self.voxels or not self.gap_voxels:
            raise P16V2Unavailable(
                "P14/P15 未给出走廊体元，无法构造覆盖矩阵",
                code="p16_v2_voxels_unavailable",
            )
        self.requirements = routes[0].get("required_cns")
        if not isinstance(self.requirements, dict):
            required = self.state.get("required_cns") or {}
            route_id = routes[0].get("route_id")
            self.requirements = (
                (required.get("route_overrides") or {}).get(route_id)
                or required.get("project_default")
                or {}
            )
        if not isinstance(self.requirements, dict) or not self.requirements:
            raise P16V2Unavailable(
                "P14 未给出 C/S 服务要求，无法构造覆盖矩阵",
                code="p16_v2_requirements_unavailable",
            )
        self.targets, _target_meta = _targets(gap)
        self.base_providers = build_geometric_providers(self.base, self.catalog, self.profiles)
        self._base_rows = [
            provider for code in _SERVICE_CODES for provider in self.base_providers[code]
        ]
        self.base_indices = tuple(
            range(len(self.actions), len(self.actions) + len(self._base_rows))
        )
        identity_actions = self.actions + [
            {"distinct_site_id": provider["distinct_site_id"],
             "coordinate": provider["coordinate"]}
            for provider in self._base_rows
        ]
        if any(not item.get("distinct_site_id") for item in identity_actions):
            raise P16V2Unavailable(
                "存在无法判定物理站址身份的候选动作或既有设施",
                code="p16_v2_site_identity_unknown",
            )
        self.groups, self.near = physical_groups(
            identity_actions, distance_limit_m=self.near_identity_distance_m,
        )
        required, weights = self._coverage_requirements()
        self.matrix_required = required
        self.matrix_weights = weights
        self.identity = self._matrix_identity()
        self._report("基线 P14/P15 完成", completed=3, total=3, **{
            "p14_fingerprint": corridor.get("input_fingerprint"),
            "p15_fingerprint": gap.get("input_fingerprint"),
            "near_identity_pairs": len(self.near),
        })
        return {
            "corridor": corridor, "gap": gap,
            "prepared": self.prepared, "requirements": self.requirements,
            "targets": self.targets, "threshold_m": self.threshold_m,
            "route_speed_mps": self.route_speed_mps, "route_length_m": self.route_length_m,
        }

    def _coverage_requirements(self):
        #: 列顺序必须与矩阵完全一致：**先 C 的全部体元，再 S 的全部体元**。
        #: 这里若按体元外层遍历，required/weights 会与签名列错位，快速结论就会
        #: 与正式 P14/P15 出现无法解释的差异。
        required, weights = [], []
        for code in _SERVICE_CODES:
            for voxel in self.gap_voxels:
                entry = next(
                    (item for item in (voxel.get("subsystems") or [])
                     if item.get("subsystem") == code),
                    None,
                )
                services = (entry or {}).get("services") or []
                count = services[0].get("required_distinct_site_count") if services else None
                if len(services) != 1 or count is None:
                    raise P16V2Unavailable(
                        "P16-V2 的快速层要求每个缺口体元恰好声明一个 C/S 服务，"
                        "且该服务的 required_distinct_site_count 已知",
                        code="p16_v2_requirements_unsupported",
                        detail={"voxel_id": voxel.get("voxel_id"), "subsystem": code,
                                "service_count": len(services)},
                    )
                if not isinstance(count, (int, float)) or int(count) < 1:
                    raise P16V2Unavailable(
                        "P15 缺口体元的 required_distinct_site_count 不是正整数",
                        code="p16_v2_requirements_unsupported",
                        detail={"voxel_id": voxel.get("voxel_id"), "subsystem": code,
                                "required_distinct_site_count": count},
                    )
                required.append(int(count))
                weights.append(float(voxel.get("discretized_volume_proxy_m3") or 0.0))
        if not required:
            raise P16V2Unavailable(
                "P15 缺口体元没有可用的覆盖要求", code="p16_v2_requirements_empty",
            )
        return required, weights

    def _matrix_identity(self):
        return _canonical_sha256({
            "schema": V2_SCHEMA_VERSION,
            "coverage_version": COVERAGE_VERSION,
            "algorithm_id": self.algorithm_id,
            "algorithm_version": self.algorithm_version,
            "cost_policy": COST_POLICY_VERSION,
            "sources": source_fingerprints(),
            "p14_fingerprint": (self.baseline_corridor or {}).get("input_fingerprint"),
            "p15_fingerprint": (self.baseline_gap or {}).get("input_fingerprint"),
            "actions": self.actions,
            "base_rows": self._base_rows,
            "catalog": self.catalog,
            "aircraft": self.aircraft,
            "profiles": self.profiles,
            "required": self.matrix_required,
            "weights": self.matrix_weights,
            "groups": self.groups,
            "near_limit_m": self.near_identity_distance_m,
            "voxels": [
                {"voxel_id": item.get("voxel_id"),
                 "offset": item.get("nearest_route_offset_m"),
                 "half_diagonal": item.get("cell_half_diagonal_m")}
                for item in self.voxels
            ],
        })

    # ---- 矩阵 ---------------------------------------------------------------

    def _cache_paths(self, identity):
        if self.cache_dir is None:
            return None, None
        return (
            self.cache_dir / f"matrix-{identity[:24]}.npz",
            self.cache_dir / f"matrix-{identity[:24]}.json",
        )

    def _load_cached_matrix(self, identity):
        matrix_path, manifest_path = self._cache_paths(identity)
        if matrix_path is None or not matrix_path.is_file() or not manifest_path.is_file():
            return None
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(manifest, dict) or manifest.get("fingerprint") != identity:
            return None
        if manifest.get("sha256") != _sha256_file(matrix_path):
            return None
        try:
            data = np.load(matrix_path, allow_pickle=False)["signatures"]
        except (OSError, ValueError, KeyError):
            return None
        if data.shape != (len(self.actions) + len(self._base_rows), len(self.gap_voxels) * 2):
            return None
        return data

    def _store_cached_matrix(self, identity, data):
        matrix_path, manifest_path = self._cache_paths(identity)
        if matrix_path is None:
            return None
        try:
            matrix_path.parent.mkdir(parents=True, exist_ok=True)
            np.savez_compressed(matrix_path, signatures=data)
            manifest_path.write_text(json.dumps({
                "schema_version": V2_SCHEMA_VERSION,
                "fingerprint": identity,
                "sha256": _sha256_file(matrix_path),
                "shape": list(data.shape),
                "bytes": int(data.nbytes),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            return matrix_path
        except OSError:
            return None

    def _coverage_matrix(self, baseline):
        started = time.perf_counter()
        cached = self._load_cached_matrix(self.identity)
        if cached is not None:
            data = cached
            hit = True
        else:
            hit = False
            data = self._build_signatures(
                self.actions, self.voxels, self.base,
            )
            self._store_cached_matrix(self.identity, data)
        matrix_seconds = time.perf_counter() - started
        self._timings["matrix_s"] = round(matrix_seconds, 3)
        matrix = CoverageMatrix(
            data, self.groups, self.matrix_required, self.matrix_weights,
        )
        raw = CoverageMatrix(
            data,
            [item["distinct_site_id"] for item in (
                self.actions + [
                    {"distinct_site_id": provider["distinct_site_id"]}
                    for provider in self._base_rows
                ]
            )],
            self.matrix_required, self.matrix_weights,
        )
        self.matrix = matrix
        self.raw_matrix = raw
        self._report(
            "覆盖矩阵已就绪", completed=len(data), total=len(data),
            cache="warm" if hit else "cold",
            matrix_shape=list(data.shape),
            matrix_bytes=int(data.nbytes),
        )
        return matrix, {
            "hit": hit, "cold_cache": not hit,
            "seconds": round(matrix_seconds, 3),
            "shape": list(data.shape),
            "bytes": int(data.nbytes),
        }

    def _build_signatures(self, actions, voxels, base):
        """三态 provider 覆盖矩阵（纯函数：不读文件、不写状态）。"""

        base_providers = build_geometric_providers(base, self.catalog, self.profiles)
        base_rows = [p for code in _SERVICE_CODES for p in base_providers[code]]
        rows = [(action, None) for action in actions] + [(None, provider) for provider in base_rows]
        result = np.zeros((len(rows), len(voxels) * len(_SERVICE_CODES)), dtype=np.int8)
        contexts = {
            code: prepare_capability_context(
                code, self.requirements[_SERVICE_NAMES[code]], self.aircraft,
            )
            for code in _SERVICE_CODES
        }
        total = len(rows)
        for index, (action, provider) in enumerate(rows):
            facilities = _apply_cumulative_action({"items": []}, action) if action else base
            code = action["subsystem"] if action else provider["subsystem"]
            providers = (
                build_geometric_providers(facilities, self.catalog, self.profiles)[code]
                if action else [provider]
            )
            devices = build_provider_devices(self.catalog, facilities)
            service_key = _SERVICE_KEYS[code]
            code_index = _SERVICE_CODES.index(code)
            for column, voxel in enumerate(voxels):
                probe = voxel["probe"]
                geometry = evaluate_geometry_point(probe, providers)
                if geometry.get("covered") is False:
                    continue
                capability = evaluate_capability_point(
                    code, {**probe, **geometry}, self.requirements[_SERVICE_NAMES[code]],
                    self.aircraft, devices, context=contexts[code],
                )
                evaluations = capability.get("provider_evaluations") or []
                qualified = any(
                    item.get("status") == "meets_under_model"
                    and item.get("stage") != "provider_type_compatibility"
                    and item.get("service_key") == service_key
                    for item in evaluations
                )
                result[index, code_index * len(voxels) + column] = (
                    1 if qualified else (-1 if capability.get("status") == "unknown" else 0)
                )
            if (index + 1) % 16 == 0 or index + 1 == total:
                self._report(
                    "正在构建三态覆盖矩阵", completed=index + 1, total=total,
                    unit="矩阵行",
                )
        return result

    # ---- 搜索 ---------------------------------------------------------------

    def _search(self, matrix):
        started = time.perf_counter()
        planners = []
        solutions = []
        coverage_cache = {}
        base_costs = [REUSE_COST.get(item.get("reuse_class"), 9.0) for item in self.actions]
        for power in self.scarcity_powers:
            planner = FastPlannerV2(
                matrix, len(self.actions), self.base_indices, base_costs,
                cap=self.cap, scarcity_power=power, coverage_cache=coverage_cache,
            )

            def step(done, total, phase, pairs, local_count, steps, power=power):
                self._report(
                    "正在搜索候选组合", completed=done, total=total,
                    unit="已选动作", scarcity_power=power, search_phase=phase,
                    pair_comparisons=pairs, local_comparisons=local_count,
                    search_steps=steps, coverage_combinations=len(coverage_cache),
                )

            selected = planner.search(
                on_step=step,
                cancel_check=self._cancel_check,
            )
            planners.append(planner)
            counts = planner.counts(selected)
            rank = (
                int((counts >= matrix.required).sum()),
                float(np.minimum(counts, matrix.required) @ matrix.weights),
                -len(selected),
                -sum(base_costs[index] for index in selected),
            )
            solutions.append((rank, selected, power))
            self._report(
                "贪心搜索完成一轮", completed=1, total=1,
                scarcity_power=power, selected_actions=len(selected),
                coverage_combinations=len(coverage_cache),
            )
            if planner.complete(selected):
                break
        best_rank, selected, power = max(solutions, key=lambda item: item[0])
        self._timings["search_s"] = round(time.perf_counter() - started, 3)
        return {
            "selected": selected,
            "scarcity_power": power,
            "rank": list(best_rank),
            "planners": planners,
            "combinations": len(coverage_cache),
            "combination_cache_bytes": int(sum(item.nbytes for item in coverage_cache.values())),
            "pair_comparisons": int(sum(item.pair_count for item in planners)),
            "pair_steps": int(sum(item.pair_steps for item in planners)),
            "coverage_builds": int(sum(item.coverage_build_count for item in planners)),
            "local_comparisons": int(sum(item.local_count for item in planners)),
        }

    # ---- 精确评价 -----------------------------------------------------------

    def _exact_evaluate(self, matrix, selected, baseline):
        started = time.perf_counter()
        key = tuple(sorted(set(selected)))
        if not key:
            raise P16V2Unavailable(
                "V2 搜索没有产生任何候选动作，无法给出可复核方案",
                code="p16_v2_no_solution",
            )
        actions = [self.actions[index] for index in key]
        facilities = deepcopy(self.base)
        for action in actions:
            facilities = _apply_cumulative_action(facilities, action)
        self._report(
            "正在用正式 P14/P15 复核选中方案", completed=0, total=1, unit="方案",
            selected_actions=len(actions),
        )
        before = self.performance["full_corridor_rerun_count"]
        corridor, gap = self.service._rerun(
            facilities, prepared_cells=self.prepared, performance=self.performance,
        )
        exact_seconds = time.perf_counter() - started
        self._timings["exact_s"] = round(exact_seconds, 3)
        reruns = self.performance["full_corridor_rerun_count"] - before
        if reruns > self.exact_evaluation_budget:
            raise P16V2Unavailable(
                "精确 P14/P15 评价次数超出本轮预算，已停止",
                code="p16_v2_exact_budget_exhausted",
                detail={"reruns": reruns, "budget": self.exact_evaluation_budget},
            )
        exact = self._exact_voxel_statuses(gap)
        predicted = self.raw_matrix.evaluate((*key, *self.base_indices))
        expected = np.where(
            predicted["satisfied"], "satisfied",
            np.where(predicted["unknown"], "unknown", "confirmed_gap"),
        )
        mismatch = np.flatnonzero(exact != expected)
        if int(mismatch.size) > 0:
            raise P16V2Unavailable(
                "快速矩阵与正式 P14/P15 出现无法解释的逐体元差异，已停止（不发布不一致结论）",
                code="p16_v2_fast_exact_mismatch",
                detail={
                    "mismatch_count": int(mismatch.size),
                    "indices": [int(item) for item in mismatch[:20]],
                },
            )
        summary = self._summarize(actions, gap)
        unmet_voxels, unmet_truncated = self._unmet_voxels(gap)
        conservative = self.matrix.evaluate((*key, *self.base_indices))
        for code in _SERVICE_CODES:
            count = len(self.voxels)
            start = _SERVICE_CODES.index(code) * count
            statuses = expected[start:start + count]
            length = max_continuous_deficit(
                statuses == "confirmed_gap",
                [item.get("nearest_route_offset_m") for item in self.voxels],
                [item.get("cell_half_diagonal_m") for item in self.voxels],
                self.route_length_m,
            )
            formal_length = summary["continuous"].get(f"{code}_max_continuous_deficit_m")
            if formal_length is None or abs(length - float(formal_length)) > 1e-7:
                raise P16V2Unavailable(
                    "快速矩阵与正式连续失保语义不一致，已停止",
                    code="p16_v2_fast_continuous_mismatch",
                    detail={"subsystem": code, "fast": length, "formal": formal_length},
                )
        feasible = bool(
            self._feasible(summary) and bool(conservative["satisfied"].all())
        )
        self._report(
            "正式 P14/P15 复核完成", completed=1, total=1, unit="方案",
            feasible=feasible, exact_reruns=reruns,
        )
        return {
            "gap": gap, "corridor": corridor, "summary": summary,
            "feasible": feasible, "seconds": round(exact_seconds, 3),
            "reruns": int(reruns),
            "conservative_satisfied": int(conservative["satisfied"].sum()),
            "p14_fingerprint": corridor.get("input_fingerprint"),
            "p15_fingerprint": gap.get("input_fingerprint"),
            "unmet_voxels": unmet_voxels,
            "unmet_truncated": bool(unmet_truncated),
        }

    def _grid_centers(self):
        """``grid_id → [lon, lat]``（只读；缺失坐标的体元如实不画）。"""

        centers = {}
        for cell in (self.state.get("grid") or {}).get("cells") or []:
            if not isinstance(cell, dict):
                continue
            grid_id = str(cell.get("grid_id") or "")
            center = cell.get("center")
            if not grid_id or not isinstance(center, (list, tuple)) or len(center) < 2:
                continue
            try:
                centers[grid_id] = [float(center[0]), float(center[1])]
            except (TypeError, ValueError):
                continue
        return centers

    def _unmet_voxels(self, gap):
        """精确评价后仍未满足的体元（含网格中心坐标，供二维地图如实标注）。

        这是"覆盖结果"在地图上的唯一来源：满足的体元不返回，避免把 2727×2 个
        点塞进任务记录；数量超过上限时明确截断并置 ``unmet_truncated``。
        """

        centers = self._grid_centers()
        rows = []
        truncated = False
        limit = 500
        for route in gap.get("routes") or []:
            for voxel in route.get("voxels") or []:
                grid_id = str(voxel.get("grid_id") or "")
                center = centers.get(grid_id)
                for entry in voxel.get("subsystems") or []:
                    code = str(entry.get("subsystem") or "")
                    if code not in _SERVICE_CODES:
                        continue
                    status = str(entry.get("combined_status") or "missing")
                    if status == "satisfied":
                        continue
                    if len(rows) >= limit:
                        truncated = True
                        continue
                    rows.append({
                        "voxel_id": voxel.get("voxel_id"),
                        "grid_id": grid_id,
                        "subsystem": code,
                        "status": status,
                        "coordinate": deepcopy(center),
                    })
        return rows, truncated

    def _exact_voxel_statuses(self, gap):
        statuses = []
        for code in _SERVICE_CODES:
            for route in gap.get("routes") or []:
                for voxel in route.get("voxels") or []:
                    entry = next(
                        (item for item in (voxel.get("subsystems") or [])
                         if item.get("subsystem") == code),
                        None,
                    )
                    statuses.append(str((entry or {}).get("combined_status") or "missing"))
        return np.asarray(statuses, dtype=object)

    # ---- 汇总 ---------------------------------------------------------------

    def _summarize(self, actions, gap):
        acceptance = _continuous_service_acceptance(
            gap, self.threshold_m, self.route_speed_mps,
            self.aircraft_id or None,
        )
        by_subsystem = {
            item["subsystem"]: item for item in acceptance.get("subsystems") or []
        }
        impact = _impact(
            {"action_id": "p16-v2-solution"}, self.baseline_gap, gap, self.targets,
            continuous_threshold_m=self.threshold_m,
            route_speed_mps=self.route_speed_mps,
            aircraft_id=self.aircraft_id or None,
        )
        discrete = {}
        for code in _SERVICE_CODES:
            values = [
                str((entry or {}).get("combined_status") or "missing")
                for route in gap.get("routes") or []
                for voxel in route.get("voxels") or []
                for entry in (voxel.get("subsystems") or [])
                if entry.get("subsystem") == code
            ]
            counts = {name: values.count(name) for name in ("satisfied", "confirmed_gap", "unknown")}
            counts["other"] = len(values) - sum(counts.values())
            discrete[code] = counts
        residual = {
            "voxel_count": len(self.gap_voxels),
            "satisfied": int(discrete["S"]["satisfied"]),
            "confirmed_gap": int(discrete["S"]["confirmed_gap"]),
            "unknown": int(discrete["S"]["unknown"]),
        }
        return {
            "discrete": discrete,
            "residual": residual,
            "continuous": {
                "threshold_m": self.threshold_m,
                "route_speed_mps": self.route_speed_mps,
                "S_max_continuous_deficit_m": (by_subsystem.get("S") or {}).get(
                    "max_continuous_deficit_projection_m"),
                "S_duration_s": (by_subsystem.get("S") or {}).get("longest_outage_duration_s"),
                "C_max_continuous_deficit_m": (by_subsystem.get("C") or {}).get(
                    "max_continuous_deficit_projection_m"),
                "C_duration_s": (by_subsystem.get("C") or {}).get("longest_outage_duration_s"),
                "acceptable": acceptance.get("acceptable"),
                "status": acceptance.get("status"),
                "longest_deficit_key": acceptance.get("longest_deficit_key"),
                "all_subsystems": deepcopy(acceptance.get("subsystems")),
            },
            "confirmed_requirement_unit_volume_gain": impact.get(
                "confirmed_requirement_unit_volume_gain"),
            "impact_status": impact.get("status"),
            "regressions": impact.get("regressions"),
        }

    def _feasible(self, summary):
        discrete = summary["discrete"]
        continuous = summary["continuous"]
        for code in _SERVICE_CODES:
            if discrete[code]["confirmed_gap"] != 0 or discrete[code]["unknown"] != 0:
                return False
        if continuous.get("acceptable") is not True:
            return False
        worst = 0.0
        for code in _SERVICE_CODES:
            value = continuous.get(f"{code}_max_continuous_deficit_m")
            if value is None:
                return False
            worst = max(worst, float(value))
        return worst <= self.threshold_m + 1e-9

    def _assemble(self, deduped, validation, baseline, matrix, matrix_info, search, exact):
        selected = sorted(search["selected"])
        actions = []
        for index in selected:
            action = self.actions[index]
            actions.append({
                "action_id": action.get("action_id"),
                "subsystem": action.get("subsystem"),
                "device_id": action.get("device_id"),
                "reuse_class": action.get("reuse_class"),
                "site_id": action.get("site_id"),
                "distinct_site_id": action.get("distinct_site_id"),
                "name": action.get("name"),
                "coordinate": deepcopy(action.get("coordinate")),
                "physical_site_key": deepcopy(action.get("physical_site_key")),
                "cost_proxy": REUSE_COST.get(action.get("reuse_class")),
                "cost_semantics": "declared_planning_proxy_not_money",
            })
        sites = {}
        for item in actions:
            key = str(item.get("distinct_site_id") or "")
            if key and key not in sites:
                sites[key] = {
                    "distinct_site_id": key,
                    "coordinate": deepcopy(item.get("coordinate")),
                    "name": item.get("name"),
                    "subsystems": sorted({
                        str(entry.get("subsystem")) for entry in actions
                        if str(entry.get("distinct_site_id")) == key
                    }),
                }
        return {
            "schema_version": V2_SCHEMA_VERSION,
            "service_version": V2_SERVICE_VERSION,
            "status": "computed",
            "semantics": "p16_v2_engineering_assumption_readonly_proposal",
            "assumption_only": True,
            "real_world_confirmed": False,
            "formal_write": False,
            "formal_project_state_write": False,
            "formal_candidate_sites_write": False,
            "not_formal_p16_plan": True,
            "formal_p16_algorithm_modified": False,
            "algorithm_id": self.algorithm_id,
            "algorithm_version": self.algorithm_version,
            "coverage_version": COVERAGE_VERSION,
            "cost_policy": COST_POLICY_VERSION,
            "cost_semantics": "declared_planning_proxy_not_money",
            "version": "p16-v2",
            "input_switch": "explicit_v2",
            "inputs": {
                "cap": self.cap,
                "action_count": len(self.actions),
                "physical_site_count": len({str(item.get("distinct_site_id")) for item in self.actions}),
                "candidate_pool_sha256": deduped.get("identity_sha256"),
                "scenario_id": (self.pool.get("scenario") or {}).get("scenario_id"),
                "p14_fingerprint": baseline["corridor"].get("input_fingerprint"),
                "p15_fingerprint": baseline["gap"].get("input_fingerprint"),
                "aircraft_id": self.aircraft_id or None,
                "continuous_threshold_m": self.threshold_m,
                "route_speed_mps": self.route_speed_mps,
                "route_count": 1,
                "route_length_m": self.route_length_m,
                "voxel_count": len(self.voxels),
                "gap_voxel_count": len(self.gap_voxels),
                "state_revision": self.state.get("revision"),
            },
            "matrix": deepcopy(matrix_info),
            "matrix_identity": self.identity,
            "search": {
                "scarcity_power": search["scarcity_power"],
                "rank": deepcopy(search["rank"]),
                "unique_combinations": search["combinations"],
                "combination_cache_bytes": search["combination_cache_bytes"],
                "pair_comparisons": search["pair_comparisons"],
                "pair_steps": search["pair_steps"],
                "coverage_builds": search["coverage_builds"],
                "local_comparisons": search["local_comparisons"],
                "cap": self.cap,
            },
            "selected_actions": actions,
            "selected_sites": list(sites.values()),
            "selected_action_ids": [item.get("action_id") for item in actions],
            "feasible": exact["feasible"],
            "coverage": deepcopy(exact["summary"]),
            "unmet_voxels": deepcopy(exact.get("unmet_voxels") or []),
            "unmet_truncated": bool(exact.get("unmet_truncated")),
            "exact": {
                "reruns": exact["reruns"],
                "seconds": exact["seconds"],
                "conservative_satisfied_voxel_subsystems": exact["conservative_satisfied"],
                "p14_fingerprint": exact["p14_fingerprint"],
                "p15_fingerprint": exact["p15_fingerprint"],
            },
            "near_identities": deepcopy(self.near),
            "near_identity_distance_m": self.near_identity_distance_m,
            "contract_validation": deepcopy(validation),
            "adapter": {
                "schema_version": P16_ADAPTER_SCHEMA_VERSION,
                "assumption_only": True,
                "formal_write": False,
                "session": "read_only",
            },
        }

__all__ = [
    "P16V2Service", "P16V2Unavailable", "STAGES", "STAGE_LABELS",
    "V2_SCHEMA_VERSION", "V2_SERVICE_VERSION", "physical_groups",
    "source_fingerprints",
]
