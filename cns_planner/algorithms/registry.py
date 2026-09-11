"""Exact-match algorithm registration, discovery and instantiation."""

from __future__ import annotations

from copy import deepcopy

from ..domain.algorithm_manifest import AlgorithmFactory, AlgorithmManifest
from .coverage.v1 import CoveragePlannerV1
from .route.v1 import RoutePlannerV1
from ..gap.v1 import CNSGapAnalyzerV1
from ..risk.v1 import RiskModelV1


ALGORITHM_TYPES = ("risk_model", "route_planner", "coverage_planner", "cns_gap_analyzer")


class AlgorithmNotFoundError(ValueError):
    pass


class AlgorithmRegistry:
    """Keep executable factories private while exposing JSON-safe manifests."""

    def __init__(self):
        self._entries: dict[tuple[str, str, str], tuple[AlgorithmManifest, AlgorithmFactory]] = {}
        self._legacy: dict[str, object] = {}

    def register(self, manifest, factory):
        # Compatibility with the original route-only registry API.
        if isinstance(manifest, str):
            if not manifest or manifest in self._legacy:
                raise ValueError("算法 ID 为空或重复")
            self._legacy[manifest] = factory
            return self
        if not isinstance(manifest, AlgorithmManifest) or not callable(factory):
            raise ValueError("算法注册需要 AlgorithmManifest 和 factory")
        if manifest.algorithm_type not in ALGORITHM_TYPES:
            raise ValueError(f"不支持的算法类型：{manifest.algorithm_type}")
        if manifest.key in self._entries:
            raise ValueError(f"算法已注册：{manifest.algorithm_id}@{manifest.version}")
        self._entries[manifest.key] = (manifest, factory)
        return self

    def get(self, key):
        if key not in self._legacy:
            raise ValueError(f"算法尚未注册：{key}")
        return self._legacy[key]

    def keys(self):
        return tuple(self._legacy)

    def manifest(self, algorithm_type, algorithm_id, version):
        key = str(algorithm_type), str(algorithm_id), str(version)
        if key not in self._entries:
            raise AlgorithmNotFoundError(
                f"未注册精确算法：{key[0]}/{key[1]}@{key[2]}"
            )
        return self._entries[key][0]

    def create(self, algorithm_type, algorithm_id, version, parameters=None):
        manifest = self.manifest(algorithm_type, algorithm_id, version)
        instance = self._entries[manifest.key][1](deepcopy(parameters or {}))
        if getattr(instance, "algorithm_id", None) != manifest.algorithm_id:
            raise ValueError(f"算法实例 ID 与 Manifest 不一致：{manifest.algorithm_id}")
        if getattr(instance, "algorithm_version", None) != manifest.version:
            raise ValueError(f"算法实例版本与 Manifest 不一致：{manifest.algorithm_id}")
        return instance

    def manifests(self, algorithm_type=None):
        items = [entry[0] for entry in self._entries.values()]
        if algorithm_type is not None:
            items = [item for item in items if item.algorithm_type == algorithm_type]
        return tuple(sorted(items, key=lambda item: item.key))

    def catalog(self):
        return [manifest.to_dict() for manifest in self.manifests()]


def default_algorithm_selection():
    return {
        "risk_model": _selection("risk_model", RiskModelV1),
        "route_planner": _selection("route_planner", RoutePlannerV1),
        "coverage_planner": _selection("coverage_planner", CoveragePlannerV1),
        "cns_gap_analyzer": _selection("cns_gap_analyzer", CNSGapAnalyzerV1),
    }


def normalize_algorithm_selection(value):
    defaults = default_algorithm_selection()
    if value is None:
        return defaults
    if not isinstance(value, dict):
        raise ValueError("algorithm_selection 格式无效")
    result = {}
    for algorithm_type, default in defaults.items():
        entry = deepcopy(value.get(algorithm_type, default))
        if not isinstance(entry, dict):
            raise ValueError(f"{algorithm_type} 算法选择格式无效")
        entry.setdefault("algorithm_type", algorithm_type)
        entry.setdefault("parameters", {})
        if entry.get("algorithm_type") != algorithm_type:
            raise ValueError(f"{algorithm_type} 的 algorithm_type 不一致")
        if not all(str(entry.get(name) or "").strip() for name in ("algorithm_id", "version")):
            raise ValueError(f"{algorithm_type} 算法选择缺少 id/version")
        if not isinstance(entry["parameters"], dict):
            raise ValueError(f"{algorithm_type} parameters 必须是对象")
        result[algorithm_type] = {
            "algorithm_type": algorithm_type,
            "algorithm_id": str(entry["algorithm_id"]),
            "version": str(entry["version"]),
            "parameters": deepcopy(entry["parameters"]),
        }
    return result


def build_default_algorithm_registry(defaults):
    registry = AlgorithmRegistry()
    registry.register(_risk_manifest(), lambda parameters: RiskModelV1())
    registry.register(_route_manifest(), lambda parameters: RoutePlannerV1(**parameters))
    registry.register(_coverage_manifest(), lambda parameters: CoveragePlannerV1(defaults))
    registry.register(_gap_manifest(), lambda parameters: CNSGapAnalyzerV1())
    return registry


def _selection(algorithm_type, implementation):
    return {
        "algorithm_type": algorithm_type,
        "algorithm_id": implementation.algorithm_id,
        "version": implementation.algorithm_version,
        "parameters": {},
    }


def _risk_manifest():
    return AlgorithmManifest(
        "risk_model", RiskModelV1.algorithm_id, RiskModelV1.algorithm_version,
        "Relative Grid Risk V1", "CNS-PLANNER", "engineering_baseline",
        "参数化组合地面、运行交通/冲突及空域约束的相对风险指数。",
        ("grid", "grid_attributes", "parameters"), ("grid_risk",),
        {"type": "object", "additionalProperties": True},
        ("输入指数可按有效分量重归一化",),
        ("输出不是事故概率或碰撞概率", "人口兼容输入仍由 V1 读取 value_mean"),
        (),
    )


def _route_manifest():
    return AlgorithmManifest(
        "route_planner", RoutePlannerV1.algorithm_id, RoutePlannerV1.algorithm_version,
        "Route Planner V1", "CNS-PLANNER", "engineering_baseline",
        "在固定离散工作区中使用硬约束 BBOX 的确定性 A* 航路规划。",
        ("scenario_route", "workspace_bbox", "hard_constraints"),
        ("operational_route", "distance_m", "statistics"),
        {"type": "object", "properties": {"grid_size": {"type": "integer", "minimum": 2}}, "additionalProperties": False},
        ("经纬度工作区离散为规则网格",),
        ("硬约束使用图层 BBOX", "不是最终三维或风险感知规划器"),
        (),
    )


def _coverage_manifest():
    return AlgorithmManifest(
        "coverage_planner", CoveragePlannerV1.algorithm_id, CoveragePlannerV1.algorithm_version,
        "Coverage Planner V1", "CNS-PLANNER", "demo",
        "按设备水平覆盖半径生成 C/N/S 主站、补盲站与共址结果。",
        ("operational_routes", "device_catalog_coverage_v1"), ("coverage_plan",),
        {"type": "object", "additionalProperties": False},
        ("二维圆覆盖", "设备参数由 DeviceCatalog 兼容转换提供"),
        ("不计算传播、遮挡、干扰或三维服务体积",),
        (),
    )


def _gap_manifest():
    return AlgorithmManifest(
        "cns_gap_analyzer", CNSGapAnalyzerV1.algorithm_id, CNSGapAnalyzerV1.algorithm_version,
        "CNS Gap Analysis V1", "CNS-PLANNER", "engineering_baseline",
        "按航路长度比较 RequiredCNS、机载能力和既有设施水平覆盖。",
        ("operational_routes", "required_cns", "aircraft_profile", "existing_cns", "device_catalog"),
        ("route_cns_gaps", "uncovered_segments", "coverage_ratio"),
        {"type": "object", "additionalProperties": False},
        ("既有设施使用设备 coverage radius 水平覆盖",),
        ("不计算传播、遮挡、干扰或三维性能",),
        (),
    )
