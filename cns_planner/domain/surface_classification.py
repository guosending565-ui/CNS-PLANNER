"""Step5 共用、**中立**的 surface classification 策略与可序列化事实（Round 2）。

本模块解决 Round 1 留下的三个结构性问题：

1. **耦合**：Round 1 的 surface classification provider 从 ``radar_surveillance_layout``
   policy 读取 ``land_mask_layer_name`` / ``coastal_uncertainty_buffer_m``。Communication /
   RID 因此在语义上依赖"雷达已配置"，这是错误耦合。这里定义**中立策略**
   （``surface_classification_policy``）：它就是 Step5 共用的陆域分类事实设置，
   Radar 仍然可以使用它自己的 policy（其默认值与中立默认值一致），而
   Communication / RID **不需要**先运行任何 Radar layout。

   Round 2 P0 收口把这条边界做实：**运行时**只消费
   ``surface_classification_policy`` + land-mask 数据源，任何路径都不得再读
   ``radar_surveillance_policy`` 作为 fallback。旧项目的兼容参数只在
   normalize / backfill / migration 阶段由
   :func:`migrate_surface_classification_policy_from_legacy_radar_policy`
   **一次性**复制进中立策略，并留下 provenance
   （``origin = legacy_radar_policy_migration``）。
2. **不可序列化**：Round 1 只提供 callable seam（``surface_class_provider``）。callable
   不能进入 Heavy Task 的 immutable snapshot，也不能形成稳定 fingerprint。这里定义
   **可序列化的事实容器** ``surface_class_facts``：

   * 顶层：``status`` / ``source`` / ``land_mask``（source identity）/ ``semantics`` /
     ``classification_basis`` / ``coastal_uncertainty``（policy）/ ``input_fingerprint``；
   * 逐格：``by_grid_id`` = ``{grid_id: surface_class}``。

   它是 **cell representative classification**（格心代表点判定），**不是**连续精确海岸线，
   也**绝不**用 DEM NoData 推断海洋；无法判定时保持 ``unknown``（fail-closed）。
3. **运行时可消费**：:class:`SurfaceFactsProvider` 与既有 ``classify_surface`` /
   ``classify_surface_detailed`` 接口兼容，可以直接注入 P7 / P14 的
   ``surface_class_provider`` seam；它**不复制**任何 polygon / shapely / land-mask
   分类算法（分类只在 :meth:`build_surface_class_facts` 里通过既有
   ``LandMaskSource`` 做**一次**）。

独立 namespace：surface facts **不**塞进 ``grid_attributes["terrain"]`` 的
elevation 语义，避免"地表分类"与"地形高程"互相污染。
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json

from .cns_service_contract import SURFACE_CLASSES, normalize_surface_class


SCHEMA_VERSION = 1

#: 产品语义：这是"格心代表点分类"，不是连续精确海岸线。
FACTS_SEMANTICS = "per_grid_cell_representative_point_surface_classification"
CLASSIFICATION_BASIS = "explicit_land_polygon_containment_plus_coastal_uncertainty_buffer"
POLICY_SEMANTICS = "step5_shared_neutral_land_mask_classification_policy"

#: ``coastal_uncertain`` 是**显式工程参数**下的不确定带：落在陆域 polygon 外、
#: 但到陆域边界距离 ``<= buffer`` 的点。它不是数据精度声明，也不改变 land policy。
COASTAL_UNCERTAINTY_SEMANTICS = (
    "engineering_buffer_outside_land_polygon_treated_as_land_policy"
)

DEFAULT_COASTAL_UNCERTAINTY_BUFFER_M = 30.0

#: policy provenance：新项目的工程默认（**不是**从任何 Radar policy 复制来的）。
POLICY_ORIGIN_PROJECT_DEFAULT = "project_engineering_default"
#: policy provenance：旧项目在 normalize/backfill 阶段**一次性**从
#: ``radar_surveillance_policy`` 复制 land-mask 分类参数而生成的独立 policy。
POLICY_ORIGIN_LEGACY_RADAR_MIGRATION = "legacy_radar_policy_migration"

#: 迁移来源的持久化键（只在 migrate 阶段读取，运行时**绝不**读取）。
LEGACY_RADAR_POLICY_KEY = "radar_surveillance_policy"
#: 迁移复制的字段：只限"与 land-mask 分类有关"的兼容参数。
LEGACY_RADAR_MIGRATED_FIELDS = ("land_mask_layer_name", "coastal_uncertainty_buffer_m")


def default_surface_classification_policy() -> dict:
    """中立默认策略。

    ``land_mask_layer_name=None`` 表示让既有读取器按保守关键词自动选层；
    它不是"未配置"，陆域源本身由 ``data_sources["land_mask"]`` 决定。

    ``origin`` 记录该 policy 的**来源**：新项目为 ``project_engineering_default``；
    旧项目一次性迁移时为 ``legacy_radar_policy_migration``（并附迁移 provenance）。
    它只用于追溯，**不**参与任何 fingerprint。
    """

    return {
        "semantics": POLICY_SEMANTICS,
        "land_mask_layer_name": None,
        "coastal_uncertainty_buffer_m": DEFAULT_COASTAL_UNCERTAINTY_BUFFER_M,
        "coastal_uncertainty": {
            "semantics": COASTAL_UNCERTAINTY_SEMANTICS,
            "is_data_accuracy_claim": False,
            "coastal_uncertainty_buffer_m": DEFAULT_COASTAL_UNCERTAINTY_BUFFER_M,
        },
        "classification_basis": CLASSIFICATION_BASIS,
        "dem_nodata_used_to_infer_sea": False,
        "unknown_is_fail_closed": True,
        "source": "project_engineering_default",
        "origin": POLICY_ORIGIN_PROJECT_DEFAULT,
        "legacy_radar_policy_migration": None,
        "confirmed": False,
        "status": "pending_confirmation",
    }


def normalized_coastal_buffer_m(value):
    """海岸不确定带宽度（米）：有限、非负；非法直接 ``ValueError``。"""

    if value in (None, ""):
        return float(DEFAULT_COASTAL_UNCERTAINTY_BUFFER_M)
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("coastal_uncertainty_buffer_m 必须是数值") from exc
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError("coastal_uncertainty_buffer_m 必须是有限数值")
    if number < 0:
        raise ValueError("coastal_uncertainty_buffer_m 不能为负")
    return number


def normalize_surface_classification_policy(value):
    """中立 surface classification 策略的 canonical normalize（save/reopen 幂等）。"""

    empty = default_surface_classification_policy()
    if value is None:
        return empty
    if not isinstance(value, dict):
        raise ValueError("surface_classification_policy 必须是对象")
    result = deepcopy(empty)
    layer = value.get("land_mask_layer_name")
    result["land_mask_layer_name"] = str(layer).strip() if layer not in (None, "") else None
    result["coastal_uncertainty_buffer_m"] = normalized_coastal_buffer_m(
        value.get("coastal_uncertainty_buffer_m", result["coastal_uncertainty_buffer_m"])
    )
    result["coastal_uncertainty"] = {
        "semantics": COASTAL_UNCERTAINTY_SEMANTICS,
        "is_data_accuracy_claim": False,
        "coastal_uncertainty_buffer_m": result["coastal_uncertainty_buffer_m"],
    }
    result["source"] = str(value.get("source") or empty["source"])
    result["origin"] = str(value.get("origin") or empty["origin"])
    migration = value.get("legacy_radar_policy_migration")
    result["legacy_radar_policy_migration"] = (
        deepcopy(migration) if isinstance(migration, dict) else None
    )
    result["confirmed"] = value.get("confirmed") is True
    result["status"] = "confirmed" if result["confirmed"] else "pending_confirmation"
    return result


def migrate_surface_classification_policy_from_legacy_radar_policy(legacy_radar_policy):
    """旧项目**一次性**迁移：从 ``radar_surveillance_policy`` 复制 land-mask 分类参数。

    调用时机只有一个：normalize / backfill / migration 阶段，且持久化状态中
    **完全没有** ``surface_classification_policy``。它生成一个**独立**的中立
    policy，并把溯源写进 ``origin`` / ``legacy_radar_policy_migration``。

    **运行时绝不调用本函数**：一旦 ``surface_classification_policy`` 存在，任何
    运行路径（ApplicationContext / provider / 事实生成）都不得再读 Radar policy
    作为 fallback。这样旧项目的分类参数与行为保持完全一致，而新项目与迁移后的
    项目都只有一个权威来源。
    """

    legacy = legacy_radar_policy if isinstance(legacy_radar_policy, dict) else {}
    base = default_surface_classification_policy()
    buffer_m = legacy.get("coastal_uncertainty_buffer_m")
    if buffer_m in (None, ""):
        buffer_m = base["coastal_uncertainty_buffer_m"]
    payload = {
        **base,
        "land_mask_layer_name": legacy.get("land_mask_layer_name"),
        "coastal_uncertainty_buffer_m": buffer_m,
        "source": POLICY_ORIGIN_LEGACY_RADAR_MIGRATION,
        "origin": POLICY_ORIGIN_LEGACY_RADAR_MIGRATION,
        # 只有旧 Radar policy 自身被显式确认过，迁移结果才继承"已确认"。
        "confirmed": legacy.get("confirmed") is True,
        "legacy_radar_policy_migration": {
            "origin": POLICY_ORIGIN_LEGACY_RADAR_MIGRATION,
            "one_shot": True,
            "source_policy_key": LEGACY_RADAR_POLICY_KEY,
            "copied_fields": list(LEGACY_RADAR_MIGRATED_FIELDS),
        },
    }
    return normalize_surface_classification_policy(payload)


def empty_surface_class_facts(status="not_calculated") -> dict:
    policy = default_surface_classification_policy()
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "semantics": FACTS_SEMANTICS,
        "classification_basis": CLASSIFICATION_BASIS,
        "coastal_uncertainty": {
            "semantics": COASTAL_UNCERTAINTY_SEMANTICS,
            "coastal_uncertainty_buffer_m": policy["coastal_uncertainty_buffer_m"],
            "is_data_accuracy_claim": False,
        },
        "land_mask": {
            "source_role": "land_mask",
            "configured_path": None,
            "layer_name": None,
            "layer_resolution": None,
            "source_type": "real",
            "source_crs": None,
            "metric_crs": None,
            "crs_status": None,
            "reason": None,
            #: Round 2.1：**稳定**来源身份（内容 hash 优先，其次声明事实）。
            #: 它进入 input fingerprint，因此"换了一份来源但分类结果恰好相同"也会改变指纹。
            "source_identity": None,
        },
        "policy": policy,
        "source": None,
        "dem_nodata_used_to_infer_sea": False,
        "unknown_is_fail_closed": True,
        "grid_cell_count": 0,
        "classified_grid_cell_count": 0,
        "surface_class_counts": {name: 0 for name in SURFACE_CLASSES},
        "by_grid_id": {},
        "input_fingerprint": None,
    }


def surface_facts_input_fingerprint(facts) -> str | None:
    """facts 的**稳定输入指纹**：逐格分类 + 分类语义 + 海岸不确定带政策 + 来源身份。

    它只取决于"分类事实与政策"，不依赖任何 callable / 运行期对象，因此可以
    直接进入 Coverage / Corridor 的 input fingerprint。

    Round 2.1：``land_mask.source_identity`` 也进入指纹。它的 ``identity_basis``
    优先是 **内容 SHA-256**（复用既有 source audit 的 ``verification.sha256``，
    否则复用 :func:`cns_planner.domain.source_audit.sha256_file`），因此
    "换了一份来源、分类结果碰巧逐格相同"同样会改变指纹；刻意**不**使用绝对路径，
    项目搬迁不会造成假变化。
    """

    if not isinstance(facts, dict) or facts.get("status") in (None, "not_calculated", "stale"):
        return None
    land_mask = facts.get("land_mask") if isinstance(facts.get("land_mask"), dict) else {}
    payload = {
        "schema_version": facts.get("schema_version"),
        "semantics": facts.get("semantics"),
        "classification_basis": facts.get("classification_basis"),
        "coastal_uncertainty": facts.get("coastal_uncertainty") or {},
        "land_mask_source_identity": land_mask.get("source_identity"),
        "by_grid_id": facts.get("by_grid_id") or {},
    }
    return sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def normalize_surface_class_facts(value) -> dict:
    """Persistence round-trip：形状原样保留，绝不重新分类、绝不补齐猜测。"""

    empty = empty_surface_class_facts()
    if not isinstance(value, dict):
        return empty
    result = deepcopy(empty)
    result["status"] = str(value.get("status") or empty["status"])
    result["source"] = deepcopy(value.get("source"))
    if isinstance(value.get("coastal_uncertainty"), dict):
        result["coastal_uncertainty"] = deepcopy(value["coastal_uncertainty"])
    if isinstance(value.get("land_mask"), dict):
        result["land_mask"] = {**empty["land_mask"], **deepcopy(value["land_mask"])}
    result["policy"] = normalize_surface_classification_policy(value.get("policy"))
    by_grid_id = value.get("by_grid_id")
    cleaned = {}
    if isinstance(by_grid_id, dict):
        for key, item in by_grid_id.items():
            identifier = str(key).strip()
            if not identifier:
                continue
            cleaned[identifier] = normalize_surface_class(item)
    result["by_grid_id"] = cleaned
    result["grid_cell_count"] = int(value.get("grid_cell_count") or len(cleaned))
    result["classified_grid_cell_count"] = sum(
        1 for item in cleaned.values() if item != "unknown"
    )
    counts = {name: 0 for name in SURFACE_CLASSES}
    for item in cleaned.values():
        counts[item] = counts.get(item, 0) + 1
    result["surface_class_counts"] = counts
    recorded = value.get("input_fingerprint")
    result["input_fingerprint"] = (
        str(recorded) if recorded else surface_facts_input_fingerprint(result)
    )
    return result


def _cell_center(cell):
    """格心：优先显式 ``coordinate``，否则用 ``bbox`` 中心。没有坐标即 ``None``。"""

    if not isinstance(cell, dict):
        return None
    coordinate = cell.get("coordinate")
    if isinstance(coordinate, (list, tuple)) and len(coordinate) >= 2:
        longitude, latitude = coordinate[0], coordinate[1]
        if longitude is not None and latitude is not None:
            return [float(longitude), float(latitude)]
    bbox = cell.get("bbox")
    if isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
        west, south, east, north = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
        return [(west + east) / 2.0, (south + north) / 2.0]
    return None


def build_surface_class_facts(grid, *, policy=None, land_mask_source=None,
                              land_mask_describe=None, coordinate_provider=None,
                              source_identity=None) -> dict:
    """对每个 grid cell 的**代表点**做一次 surface classification。

    参数：

    * ``land_mask_source`` —— 既有 ``LandMaskSource``（唯一分类实现，本模块不复制算法）；
      为 ``None`` 时全部保持 ``unknown``（fail-closed），这**不是**"全部是海"。
    * ``land_mask_describe`` —— 可调用对象，返回既有 ``LandMaskSource.describe()`` 形状的
      陆域源身份信息（source identity）。
    * ``coordinate_provider`` —— 可选：格心经纬度解析（例如"经纬度 → 米制 → 经纬度"）。
      默认直接使用格心。
    * ``source_identity`` —— 可选：**稳定**来源身份（见
      :func:`cns_planner.gis.land_mask_source.land_mask_source_identity`）。它进入
      input fingerprint，使"来源换了一份、分类结果恰好相同"也能被识别。

    结果显式记录它是 **cell representative classification**，不声称连续精确海岸线。
    """

    normalized_policy = normalize_surface_classification_policy(policy)
    facts = empty_surface_class_facts("passed")
    facts["policy"] = normalized_policy
    facts["coastal_uncertainty"] = {
        "semantics": COASTAL_UNCERTAINTY_SEMANTICS,
        "coastal_uncertainty_buffer_m": normalized_policy["coastal_uncertainty_buffer_m"],
        "is_data_accuracy_claim": False,
    }
    cells = (grid or {}).get("cells") or []
    by_grid_id = {}
    unresolved_centers = 0
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        grid_id = str(cell.get("grid_id") or "").strip()
        if not grid_id:
            continue
        center = _cell_center(cell)
        if center is None:
            unresolved_centers += 1
            by_grid_id[grid_id] = "unknown"
            continue
        if callable(coordinate_provider):
            resolved = coordinate_provider(center)
            center = list(resolved) if isinstance(resolved, (list, tuple)) and len(resolved) >= 2 else None
        if center is None or land_mask_source is None:
            by_grid_id[grid_id] = "unknown"
            continue
        surface, _evidence = land_mask_source.classify(center[0], center[1])
        by_grid_id[grid_id] = normalize_surface_class(surface)
    describe = land_mask_describe() if callable(land_mask_describe) else None
    if isinstance(describe, dict):
        facts["land_mask"] = {
            "source_role": describe.get("source_role") or "land_mask",
            "configured_path": describe.get("configured_path"),
            "layer_name": describe.get("layer_name"),
            "layer_resolution": describe.get("layer_resolution"),
            "source_type": describe.get("source_type") or "real",
            "source_crs": describe.get("source_crs"),
            "metric_crs": describe.get("metric_crs"),
            "crs_status": describe.get("crs_status"),
            "reason": describe.get("reason"),
            "source_identity": None,
        }
    facts["land_mask"]["source_identity"] = (
        deepcopy(source_identity) if isinstance(source_identity, dict) else None
    )
    facts["source"] = {
        "kind": "surface_classification",
        "land_mask_configured": land_mask_source is not None,
        "unresolved_cell_centers": unresolved_centers,
        "dem_nodata_used_to_infer_sea": False,
    }
    facts["by_grid_id"] = by_grid_id
    facts["grid_cell_count"] = len(by_grid_id)
    facts["classified_grid_cell_count"] = sum(1 for item in by_grid_id.values() if item != "unknown")
    counts = {name: 0 for name in SURFACE_CLASSES}
    for item in by_grid_id.values():
        counts[item] = counts.get(item, 0) + 1
    facts["surface_class_counts"] = counts
    if not by_grid_id:
        facts["status"] = "missing_data"
    facts["input_fingerprint"] = surface_facts_input_fingerprint(facts)
    return facts


def grid_id_at_coordinate(grid, coordinate):
    """格心所在 grid cell 的身份（只用于把坐标映射回 cell 代表分类）。"""

    if not coordinate or len(coordinate) < 2:
        return None
    longitude, latitude = float(coordinate[0]), float(coordinate[1])
    for cell in (grid or {}).get("cells") or []:
        bbox = cell.get("bbox") if isinstance(cell, dict) else None
        if not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
            continue
        west, south, east, north = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
        if west <= longitude < east and south <= latitude < north:
            return str(cell.get("grid_id") or "") or None
    return None


class SurfaceFactsProvider:
    """从**可序列化 surface facts**重建的 provider（既有 seam 兼容）。

    * 它只消费 :mod:`cns_planner.domain.surface_classification` 的事实容器，
      不持有任何 QGIS / shapely / callable 依赖；
    * ``classify_surface(points)`` / ``classify_surface_detailed(lon, lat)`` 与
      Round 1 的 provider 接口一致，可直接注入 P7 / P14；
    * facts ``status`` 非 ``passed`` 时一律 ``unknown``（fail-closed），
      绝不用旧事实或 land/sea 回落。
    """

    #: 与 :mod:`cns_planner.gis.radar_layout_adapter` 的 ``effective_requirement_class``
    #: 保持同一份语义：``coastal_uncertain`` 按 land 处理。
    EFFECTIVE_REQUIREMENT_CLASS = {
        "land": "land", "sea": "sea", "coastal_uncertain": "land", "unknown": None,
    }
    REQUIRED_DISTINCT_SITE_COUNT = {
        "land": 2, "sea": 1, "coastal_uncertain": 2, "unknown": None,
    }

    def __init__(self, facts, *, grid=None):
        self.facts = facts if isinstance(facts, dict) else {}
        self.grid = grid if isinstance(grid, dict) else {}
        self.by_grid_id = self.facts.get("by_grid_id") if isinstance(self.facts.get("by_grid_id"), dict) else {}
        self.status = str(self.facts.get("status") or "not_calculated")
        self.descriptor = {
            "provider_id": "surface_class_facts_provider_v1",
            "status": self.status,
            "semantics": self.facts.get("semantics"),
            "classification_basis": self.facts.get("classification_basis"),
            "coastal_uncertainty": deepcopy(self.facts.get("coastal_uncertainty") or {}),
            "land_mask": deepcopy(self.facts.get("land_mask") or {}),
            "input_fingerprint": self.facts.get("input_fingerprint"),
            "dem_nodata_used_to_infer_sea": False,
            "unknown_is_fail_closed": True,
            "is_cell_representative": True,
        }

    @property
    def usable(self) -> bool:
        return self.status == "passed" and bool(self.by_grid_id)

    def classify(self, longitude, latitude):
        """单点判定：返回 ``(surface_class, evidence)``（未知一律 ``unknown``）。"""

        if not self.usable:
            return "unknown", {
                "reason": "surface_class_facts_unusable_fail_closed",
                "facts_status": self.status,
            }
        if longitude is None or latitude is None:
            return "unknown", {"reason": "query_point_missing_coordinate"}
        grid_id = grid_id_at_coordinate(self.grid, [longitude, latitude])
        if grid_id is None or grid_id not in self.by_grid_id:
            return "unknown", {"reason": "no_grid_cell_for_query_point"}
        return normalize_surface_class(self.by_grid_id[grid_id]), {
            "reason": "cell_representative_classification",
            "grid_id": grid_id,
            "semantics": FACTS_SEMANTICS,
        }

    def classify_surface(self, points):
        return [self.classify(point[0], point[1])[0] if point and len(point) >= 2 else "unknown"
                for point in points or []]

    def classify_detailed(self, longitude, latitude):
        surface, evidence = self.classify(longitude, latitude)
        effective = self.EFFECTIVE_REQUIREMENT_CLASS.get(surface)
        return {
            "surface_class": surface,
            "effective_requirement_class": effective,
            "required_distinct_site_count": (
                self.REQUIRED_DISTINCT_SITE_COUNT.get(surface) if effective is not None else None
            ),
            "classification_confidence": (
                "uncertain" if surface == "coastal_uncertain"
                else "unknown" if surface == "unknown" else "confirmed"
            ),
            "is_cell_representative": True,
            "evidence": evidence,
        }

    def classify_surface_detailed(self, longitude, latitude):
        return self.classify_detailed(longitude, latitude)


def surface_class_provider_for(state):
    """从 canonical state 组装 provider（facts 不可用时返回 ``None``，fail-closed）。

    ``None`` 表示"没有可用的 surface 事实"：P7 / P14 会保持 ``unknown``，
    Communication / RID 的 surface 冗余要求随之为 ``unknown``，绝不猜测。
    """

    if not isinstance(state, dict):
        return None
    facts = state.get("surface_class_facts")
    if not isinstance(facts, dict) or facts.get("status") != "passed":
        return None
    provider = SurfaceFactsProvider(facts, grid=state.get("grid") or {})
    return provider if provider.usable else None


def surface_facts_fingerprint_for(state) -> str | None:
    """state 中 surface facts 的输入指纹（不可用/过期时 ``None``）。"""

    if not isinstance(state, dict):
        return None
    facts = state.get("surface_class_facts")
    if not isinstance(facts, dict):
        return None
    return surface_facts_input_fingerprint(facts)


__all__ = [
    "CLASSIFICATION_BASIS", "COASTAL_UNCERTAINTY_SEMANTICS",
    "DEFAULT_COASTAL_UNCERTAINTY_BUFFER_M", "FACTS_SEMANTICS",
    "LEGACY_RADAR_MIGRATED_FIELDS", "LEGACY_RADAR_POLICY_KEY",
    "POLICY_ORIGIN_LEGACY_RADAR_MIGRATION", "POLICY_ORIGIN_PROJECT_DEFAULT",
    "POLICY_SEMANTICS", "SCHEMA_VERSION", "SurfaceFactsProvider",
    "build_surface_class_facts", "default_surface_classification_policy",
    "empty_surface_class_facts", "grid_id_at_coordinate",
    "migrate_surface_classification_policy_from_legacy_radar_policy",
    "normalize_surface_class_facts", "normalize_surface_classification_policy",
    "normalized_coastal_buffer_m", "surface_class_provider_for",
    "surface_facts_fingerprint_for", "surface_facts_input_fingerprint",
]
