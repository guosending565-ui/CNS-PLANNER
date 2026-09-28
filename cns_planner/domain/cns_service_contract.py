"""Phase 4 Round 1 新增的 **CNS service contract**（纯 domain，无 I/O）。

本模块只做四件事，全部是 additive 的：

1. **service_key**：在既有 ``subsystem ∈ {C, N, S}`` 之上增加一个显式服务维度，
   使 ``C:communication`` 与 ``S:rid_cooperative`` 能够在 provider 分桶、能力判定、
   coverage evidence 与 corridor/gap 中彼此隔离。**不新增第四个 subsystem**。
2. **surface policy**：``land | sea | coastal_uncertain | unknown`` 的
   ``radius_by_surface`` / ``redundancy_by_surface`` 解析（含 ``unknown`` fail-closed）。
3. **physical distinct-site identity**：``distinct_site_id`` 的唯一解析顺序
   （TowerColocation → Existing CNS → Candidate/New Site → 无法确认即 ``None``），
   **禁止**经纬度猜测与坐标取整回落。
4. **surface_class adapter**：最薄地消费既有 ``classify_surface`` /
   ``classify_surface_detailed``；本模块**不**复制任何 polygon / shapely / land-mask
   分类算法，也不进入 ``radar_layout``。

兼容性铁律（本轮回归的红线）：

* 旧项目缺 ``service_key`` 时，设备的覆盖半径仍只用 legacy ``slant_range_m``，
  冗余仍只用 legacy ``min_redundancy`` / ``independence_group``；
* 只有**显式**声明了新服务契约（显式 ``service_key``，或 RID 的显式类型标识）
  的设备才启用 surface-dependent 规则；
* 几何半径只能被称为"几何规划半径"，绝不表述为实测覆盖/保证距离。
"""

from __future__ import annotations

from copy import deepcopy


# ---------------------------------------------------------------------------
# 1. service_key
# ---------------------------------------------------------------------------

SUBSYSTEMS = ("C", "N", "S")

#: 旧项目（无 ``service_key``）的子系统级默认 service_key。
LEGACY_SERVICE_KEYS = {
    "C": "C:communication",
    "N": "N:navigation",
    "S": "S:surveillance",
}

SERVICE_KEY_COMMUNICATION = "C:communication"
SERVICE_KEY_NAVIGATION = "N:navigation"
SERVICE_KEY_SURVEILLANCE = "S:surveillance"
#: 本轮新增：合作监视 / 网络远程识别。
SERVICE_KEY_RID_COOPERATIVE = "S:rid_cooperative"

KNOWN_SERVICE_KEYS = (
    SERVICE_KEY_COMMUNICATION,
    SERVICE_KEY_NAVIGATION,
    SERVICE_KEY_SURVEILLANCE,
    SERVICE_KEY_RID_COOPERATIVE,
)

#: RID 的显式类型标识：任一命中即可判定为 ``S:rid_cooperative``（这些字段本身就是
#: 显式声明，旧项目不会出现，因此不改变 legacy 行为）。
RID_SERVICE_SUBTYPE = "cooperative_surveillance"
RID_TARGET_COOPERATION = "cooperative"
RID_TECHNOLOGY = "network_remote_id"
RID_TYPE_SEMANTICS = {
    "service_subtype": RID_SERVICE_SUBTYPE,
    "target_cooperation": RID_TARGET_COOPERATION,
    "technology": RID_TECHNOLOGY,
}

#: RID 明确禁止的几何形态：雷达 90° 面阵 / sector / 水平半圆 / panel azimuth。
#: 该元组是契约的一部分，供测试与 Round 2 接线断言，绝不描述为实测性能。
RID_FORBIDDEN_GEOMETRY = (
    "radar_90_degree_panel",
    "sector",
    "horizontal_semicircle",
    "panel_azimuth",
)

#: Communication 与 RID 的**全向半球**几何契约（geometric_only）。
#: 全向 = 水平 360°，因此不存在任何方位角/扇区字段。
OMNIDIRECTIONAL_HEMISPHERE = {
    "model": "hemisphere",
    "model_scope": "geometric_only",
    "omnidirectional": True,
    "horizontal_coverage_deg": 360.0,
    "geometry_semantics": "geometric_only_omnidirectional_hemisphere",
}


# ---------------------------------------------------------------------------
# 2. surface policy
# ---------------------------------------------------------------------------

SURFACE_CLASSES = ("land", "sea", "coastal_uncertain", "unknown")

#: 冻结的业务规则：surface → 几何规划半径（米）。
#: 这些数字**只是几何规划半径**，不是实测覆盖，也不是保证通信/探测距离。
COMMUNICATION_RADIUS_BY_SURFACE = {
    "land": 4000.0,
    "coastal_uncertain": 4000.0,
    "sea": 4000.0,
}
RID_RADIUS_BY_SURFACE = {
    "land": 2000.0,
    "coastal_uncertain": 2000.0,
    "sea": 5000.0,
}

#: 冻结的业务规则：surface → 要求的不同**物理站址**数量。
#: ``coastal_uncertain`` 始终按 land policy；``unknown`` fail-closed（``None``）。
COMMUNICATION_REDUNDANCY_BY_SURFACE = {
    "land": 2, "coastal_uncertain": 2, "sea": 1, "unknown": None,
}
RID_REDUNDANCY_BY_SURFACE = {
    "land": 2, "coastal_uncertain": 2, "sea": 1, "unknown": None,
}

#: RID 的 ``urban_radius_m`` 只作为**设备事实**保存；本轮 urban/rural 分类 disabled。
RID_URBAN_RADIUS_M = 1000.0
RID_URBAN_ENABLED = False
RID_URBAN_CLASSIFICATION = "disabled"

COMMUNICATION_NOT_EVALUATED = (
    "terrain_LOS",
    "building_blocking",
    "diffraction",
    "interference",
    "link_budget",
    "capacity",
    "throughput",
    "latency",
)
RID_NOT_EVALUATED = (
    "terrain_LOS",
    "building_blocking",
    "RF_propagation",
    "receiver_sensitivity",
    "packet_collision",
    "interference",
    "real_antenna_pattern",
)

#: 只有列在这里的 service_key 才拥有 surface-dependent 覆盖/冗余规则。
SERVICE_SURFACE_POLICY = {
    SERVICE_KEY_COMMUNICATION: {
        "service_key": SERVICE_KEY_COMMUNICATION,
        "subsystem": "C",
        "label": "通信（Communication）",
        "geometry": dict(OMNIDIRECTIONAL_HEMISPHERE),
        "radius_by_surface": dict(COMMUNICATION_RADIUS_BY_SURFACE),
        "redundancy_by_surface": dict(COMMUNICATION_REDUNDANCY_BY_SURFACE),
        "radius_basis": "geometric_planning_radius",
        "not_evaluated": COMMUNICATION_NOT_EVALUATED,
    },
    SERVICE_KEY_RID_COOPERATIVE: {
        "service_key": SERVICE_KEY_RID_COOPERATIVE,
        "subsystem": "S",
        "label": "合作监视 / 网络远程识别（Cooperative Surveillance / RID）",
        "geometry": dict(OMNIDIRECTIONAL_HEMISPHERE),
        "type": dict(RID_TYPE_SEMANTICS),
        "radius_by_surface": dict(RID_RADIUS_BY_SURFACE),
        "redundancy_by_surface": dict(RID_REDUNDANCY_BY_SURFACE),
        "urban_radius_m": RID_URBAN_RADIUS_M,
        "urban_enabled": RID_URBAN_ENABLED,
        "urban_classification": RID_URBAN_CLASSIFICATION,
        "forbidden_geometry": list(RID_FORBIDDEN_GEOMETRY),
        "radius_basis": "geometric_planning_radius",
        "not_evaluated": RID_NOT_EVALUATED,
    },
}


def normalize_surface_class(value):
    """唯一分类：``land | sea | coastal_uncertain | unknown``。

    缺失或非法一律返回 ``unknown``（不猜测、不回落 land/sea）。
    """

    text = str(value or "").strip().lower()
    return text if text in SURFACE_CLASSES else "unknown"


def service_policy(service_key):
    """返回该 service_key 的冻结 surface policy；legacy 服务返回 ``None``。"""

    return SERVICE_SURFACE_POLICY.get(str(service_key or ""))


def service_key_for(subsystem, source=None):
    """解析 provider/设备的 service_key（additive 维度，subsystem 不变）。

    顺序：

    1. 显式 ``service_key``（必须与 subsystem 前缀一致且属于 ``KNOWN_SERVICE_KEYS``）；
    2. RID 显式类型标识（``service_subtype`` / ``target_cooperation`` / ``technology``）；
    3. legacy 子系统默认 key（``C:communication`` / ``N:navigation`` / ``S:surveillance``）。
    """

    code = str(subsystem or "").strip().upper()
    if code not in LEGACY_SERVICE_KEYS:
        return None
    item = source if isinstance(source, dict) else {}
    explicit = str(item.get("service_key") or "").strip()
    if explicit in KNOWN_SERVICE_KEYS:
        # subsystem 与 key 前缀冲突时视为不可靠声明，回落 legacy，绝不静默改派。
        if explicit.split(":", 1)[0] == code:
            return explicit
    if code == "S" and is_rid_declaration(item):
        return SERVICE_KEY_RID_COOPERATIVE
    return LEGACY_SERVICE_KEYS[code]


def is_rid_declaration(source):
    """RID 判定只看显式类型标识，绝不看设备名/子系统/坐标。"""

    item = source if isinstance(source, dict) else {}
    type_block = item.get("type") if isinstance(item.get("type"), dict) else {}
    for container in (type_block, item):
        subtype = str(container.get("service_subtype") or "").strip().lower()
        technology = str(container.get("technology") or "").strip().lower()
        cooperation = str(container.get("target_cooperation") or "").strip().lower()
        if subtype == RID_SERVICE_SUBTYPE or technology == RID_TECHNOLOGY or cooperation == RID_TARGET_COOPERATION:
            return True
    return False


def service_contract_for(subsystem, source=None):
    """把 subsystem + 设备/安装事实解析为一个明确的服务契约块。

    ``surface_dependent`` 只在**显式**声明新服务时成立（显式 ``service_key``
    或 RID 类型标识），因此旧项目缺 ``service_key`` 时该值为 ``False``，
    coverage / redundancy 全部保持 legacy 行为。
    """

    code = str(subsystem or "").strip().upper()
    item = source if isinstance(source, dict) else {}
    explicit_key = str(item.get("service_key") or "").strip()
    explicit = explicit_key in KNOWN_SERVICE_KEYS and explicit_key.split(":", 1)[0] == code
    rid = code == "S" and is_rid_declaration(item)
    key = service_key_for(code, item)
    policy = service_policy(key)
    return {
        "subsystem": code or None,
        "service_key": key,
        "service_key_declared": explicit,
        "service_key_inferred_from_type": bool(rid and not explicit),
        "service_key_explicit": bool(explicit or rid),
        "service_surface_dependent": bool((explicit or rid) and policy is not None),
        "policy": deepcopy(policy) if policy else None,
        "type": deepcopy((item.get("type") if isinstance(item.get("type"), dict) else {})) or None,
    }


# ---------------------------------------------------------------------------
# 3. radius_by_surface / redundancy_by_surface
# ---------------------------------------------------------------------------


def radius_by_surface_of(geometry):
    """返回 geometry 中显式的 ``surface -> radius_m`` 映射（无则 ``None``）。"""

    block = (geometry or {}).get("radius_by_surface")
    if not isinstance(block, dict) or not block:
        return None
    result = {}
    for name in SURFACE_CLASSES:
        value = block.get(name)
        if value in (None, ""):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if number > 0:
            result[name] = number
    return result or None


def effective_radius_m(geometry, surface_class):
    """解析某 sample surface 下的**几何规划半径**。

    约定（与 :func:`index_max_range_m` 的分工必须分清）：

    * ``radius_by_surface`` 存在：``land`` / ``coastal_uncertain`` / ``sea`` 各取对应值；
      ``unknown`` → ``radius_m = None``（fail-closed，绝不回落 land 或 sea）；
      映射里缺该 surface 也返回 ``None``；
    * 否则 legacy：任何 surface 都用 ``slant_range_m``（旧项目行为完全不变）；
    * 两者都没有 → ``None`` / ``unknown``。
    """

    surface = normalize_surface_class(surface_class)
    mapping = radius_by_surface_of(geometry)
    if mapping is not None:
        if surface == "unknown":
            return {
                "radius_m": None, "surface_class": surface,
                "source": "radius_by_surface", "status": "unknown",
                "reason": "unknown_surface_class_is_fail_closed",
            }
        radius = mapping.get(surface)
        if radius is None:
            return {
                "radius_m": None, "surface_class": surface,
                "source": "radius_by_surface", "status": "unknown",
                "reason": f"radius_by_surface_missing_for_{surface}",
            }
        return {
            "radius_m": float(radius), "surface_class": surface,
            "source": "radius_by_surface", "status": "resolved", "reason": None,
        }
    legacy = (geometry or {}).get("slant_range_m")
    if legacy in (None, ""):
        return {
            "radius_m": None, "surface_class": surface,
            "source": None, "status": "unknown", "reason": "coverage_radius_not_declared",
        }
    return {
        "radius_m": float(legacy), "surface_class": surface,
        "source": "legacy_slant_range_m", "status": "legacy", "reason": None,
    }


def index_max_range_m(geometry):
    """``GeometricProviderIndex`` 空间剪枝用的**包络**半径。

    ``max(radius_by_surface.values())`` 或 legacy ``slant_range_m``。
    该值**只能**用于 spatial pruning：最终是否覆盖必须由
    :func:`effective_radius_m` 依据 sample 的 surface_class 重新判定。
    """

    mapping = radius_by_surface_of(geometry)
    if mapping is not None:
        return max(mapping.values())
    legacy = (geometry or {}).get("slant_range_m")
    if legacy in (None, ""):
        return None
    try:
        value = float(legacy)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def geometry_usable(geometry):
    """几何是否可用：``model ∈ {sphere, hemisphere}`` 且半径声明存在。"""

    item = geometry or {}
    if item.get("model") not in ("sphere", "hemisphere"):
        return False
    return index_max_range_m(item) is not None


def redundancy_by_surface_of(required, service_key):
    """解析 ``surface -> required_distinct_site_count``（新契约）或 ``None``。

    顺序：RequiredCNS 的显式 ``redundancy_by_surface`` →
    该 service_key 的冻结 policy。两者都没有时返回 ``None``，
    由调用方回到 legacy ``min_redundancy``。
    """

    override = (required or {}).get("redundancy_by_surface")
    if isinstance(override, dict) and override:
        result = {}
        for name in SURFACE_CLASSES:
            value = override.get(name)
            if value in (None, ""):
                result[name] = None
                continue
            try:
                result[name] = int(value)
            except (TypeError, ValueError):
                return None
        return result
    policy = service_policy(service_key)
    if policy:
        return dict(policy["redundancy_by_surface"])
    return None


def legacy_min_redundancy(required):
    """legacy 固定冗余（旧项目唯一来源）。"""

    value = ((required or {}).get("performance") or {}).get("min_redundancy")
    if value in (None, ""):
        value = (required or {}).get("redundancy")
    if value in (None, ""):
        return None
    return int(value)


def required_distinct_site_count(required, service_key, surface_class):
    """该 sample surface 下要求的不同物理站址数量。

    * 新契约（``redundancy_by_surface`` 存在）：按 surface 解析；
      ``unknown`` → ``None``（fail-closed，绝不按 land 或 sea 处理）；
    * legacy：``min_redundancy`` / ``redundancy``，与 surface 无关（旧项目不变）。
    """

    surface = normalize_surface_class(surface_class)
    mapping = redundancy_by_surface_of(required, service_key)
    if mapping is not None:
        if surface == "unknown":
            return None
        return mapping.get(surface)
    return legacy_min_redundancy(required)


# ---------------------------------------------------------------------------
# 4. physical distinct-site identity
# ---------------------------------------------------------------------------


def _identifier(value):
    text = str(value or "").strip()
    return text or None


def distinct_site_id_for(record=None, installed=None):
    """``distinct_site_id``：只代表**物理站址身份**。

    解析顺序（不猜测、不取整、不用 equipment_id/device_id）：

    1. TowerColocation：``tower:<host_tower_id>``；
    2. Existing CNS：``site:<site_id>``，无 ``site_id`` 再 ``facility:<facility_id>``；
    3. Candidate / New Site：``site:<site_id>``；
    4. 无法得到可靠物理身份：``None``（绝不计入独立站址数量）。
    """

    site = record if isinstance(record, dict) else {}
    device = installed if isinstance(installed, dict) else {}

    for container in (device, site):
        metadata = container.get("metadata") if isinstance(container.get("metadata"), dict) else {}
        host = metadata.get("host") if isinstance(metadata.get("host"), dict) else {}
        tower = _identifier(host.get("host_tower_id"))
        if tower:
            return f"tower:{tower}"
    for container in (device, site):
        origin = container.get("planning_origin") if isinstance(container.get("planning_origin"), dict) else {}
        tower = _identifier(origin.get("host_tower_id"))
        if tower:
            return f"tower:{tower}"
    #: 显式声明的物理站址身份优先于任何推导（同名即同一物理站址）。
    for container in (device, site):
        explicit = _identifier(container.get("distinct_site_id"))
        if explicit:
            return explicit
    for container in (device, site):
        site_id = _identifier(container.get("site_id"))
        if site_id:
            return f"site:{site_id}"
    facility_id = _identifier(site.get("facility_id"))
    if facility_id:
        return f"facility:{facility_id}"
    return None


# ---------------------------------------------------------------------------
# 5. surface_class adapter（最薄地消费既有 classify_surface 能力）
# ---------------------------------------------------------------------------


def surface_class_from_provider(provider, coordinate):
    """从既有 ``classify_surface`` / ``classify_surface_detailed`` 取一个 surface_class。

    * provider 缺失 → ``unknown``（fail-closed）；
    * 只消费既有接口，**不**复制任何 polygon / land-mask 分类算法；
    * 任何异常/缺字段都返回 ``unknown``，绝不猜测、绝不回落 land/sea。
    """

    if provider is None or not coordinate or len(coordinate) < 2:
        return "unknown"
    longitude, latitude = coordinate[0], coordinate[1]
    if longitude is None or latitude is None:
        return "unknown"
    detailed = provider.get("classify_surface_detailed") if isinstance(provider, dict) else None
    if callable(detailed):
        try:
            value = detailed(longitude, latitude)
        except Exception:
            return "unknown"
        if isinstance(value, dict):
            return normalize_surface_class(value.get("surface_class"))
        return normalize_surface_class(value)
    batch = provider.get("classify_surface") if isinstance(provider, dict) else None
    if callable(batch):
        try:
            values = batch([[longitude, latitude]])
        except Exception:
            return "unknown"
        if isinstance(values, (list, tuple)) and values:
            first = values[0]
            if isinstance(first, dict):
                return normalize_surface_class(first.get("surface_class"))
            return normalize_surface_class(first)
    return "unknown"


def resolve_surface_class(*, explicit=None, provider=None, coordinate=None):
    """sample/voxel 的 surface_class 唯一解析入口（不缺省猜测）。

    优先级：显式注入的事实 → 显式 provider → ``unknown``。
    """

    if explicit not in (None, ""):
        return normalize_surface_class(explicit)
    return surface_class_from_provider(provider, coordinate)


def not_evaluated_for(service_key):
    """该 service 显式声明为 ``not_evaluated`` 的能力清单。"""

    policy = service_policy(service_key)
    if policy:
        return {name: "not_evaluated" for name in policy["not_evaluated"]}
    return {}


__all__ = [
    "COMMUNICATION_NOT_EVALUATED", "COMMUNICATION_RADIUS_BY_SURFACE",
    "COMMUNICATION_REDUNDANCY_BY_SURFACE", "KNOWN_SERVICE_KEYS",
    "LEGACY_SERVICE_KEYS", "OMNIDIRECTIONAL_HEMISPHERE", "RID_FORBIDDEN_GEOMETRY",
    "RID_NOT_EVALUATED", "RID_RADIUS_BY_SURFACE", "RID_REDUNDANCY_BY_SURFACE",
    "RID_SERVICE_SUBTYPE", "RID_TARGET_COOPERATION", "RID_TECHNOLOGY",
    "RID_TYPE_SEMANTICS", "RID_URBAN_CLASSIFICATION", "RID_URBAN_ENABLED",
    "RID_URBAN_RADIUS_M", "SERVICE_KEY_COMMUNICATION",
    "SERVICE_KEY_NAVIGATION", "SERVICE_KEY_RID_COOPERATIVE",
    "SERVICE_KEY_SURVEILLANCE", "SERVICE_SURFACE_POLICY", "SUBSYSTEMS",
    "SURFACE_CLASSES", "distinct_site_id_for", "effective_radius_m",
    "geometry_usable", "index_max_range_m", "is_rid_declaration",
    "legacy_min_redundancy", "normalize_surface_class", "not_evaluated_for",
    "radius_by_surface_of", "redundancy_by_surface_of",
    "required_distinct_site_count", "resolve_surface_class", "service_contract_for",
    "service_key_for", "service_policy", "surface_class_from_provider",
]
