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
import math


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
SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION = "N:rtk_augmentation"
SERVICE_KEY_SURVEILLANCE = "S:surveillance"
#: 本轮新增：合作监视 / 网络远程识别。
SERVICE_KEY_RID_COOPERATIVE = "S:rid_cooperative"
SERVICE_KEY_RADAR_NONCOOPERATIVE = "S:radar_noncooperative"

KNOWN_SERVICE_KEYS = (
    SERVICE_KEY_COMMUNICATION,
    SERVICE_KEY_NAVIGATION,
    SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION,
    SERVICE_KEY_SURVEILLANCE,
    SERVICE_KEY_RID_COOPERATIVE,
    SERVICE_KEY_RADAR_NONCOOPERATIVE,
)

RADAR_SERVICE_SUBTYPE = "noncooperative_surveillance"
RADAR_TARGET_COOPERATION = "non_cooperative"
RADAR_TECHNOLOGY = "radar"
RADAR_TYPE_SEMANTICS = {
    "service_subtype": RADAR_SERVICE_SUBTYPE,
    "target_cooperation": RADAR_TARGET_COOPERATION,
    "technology": RADAR_TECHNOLOGY,
}

RTK_AUGMENTATION_SERVICE_SUBTYPE = "navigation_augmentation"
RTK_AUGMENTATION_TECHNOLOGY = "gnss_rtk"
RTK_AUGMENTATION_TYPE_SEMANTICS = {
    "service_subtype": RTK_AUGMENTATION_SERVICE_SUBTYPE,
    "technology": RTK_AUGMENTATION_TECHNOLOGY,
}

#: RID 的**类型事实**字段（profile / device ``type`` 块的规范形状）。
#: 注意：这几个字段是"类型描述"，**不是**判定谓词。
RID_SERVICE_SUBTYPE = "cooperative_surveillance"
RID_TARGET_COOPERATION = "cooperative"
RID_TECHNOLOGY = "network_remote_id"
RID_TYPE_SEMANTICS = {
    "service_subtype": RID_SERVICE_SUBTYPE,
    "target_cooperation": RID_TARGET_COOPERATION,
    "technology": RID_TECHNOLOGY,
}

# ---------------------------------------------------------------------------
# 1b. Round 2.4：**机载参与能力** vs **地面提供者能力**（角色分离契约）
# ---------------------------------------------------------------------------
#
# 旧语义缺陷（Round 2.3 遗留，本轮修正）：需求侧 ``type`` 块被**同一个**
# ``type_gate_items()`` 同时用于两侧判定，于是要求"机载 aircraft 也必须逐字段
# 重复声明地面侧的类型事实"。对 RID 这直接产生了错误谓词：
#
#   ``S:rid_cooperative`` 需求 ``sensor_mode = passive``
#
# 描述的是**地面网络 RID 接收节点**的工作模式（它只接收、不发射）。而无人机的
# 角色是 **cooperative target**：它**广播/网络上报** Remote ID，既不是"被动
# sensor"，也不该被要求声明接收端的工作模式。把地面接收方的 ``sensor_mode``
# 拿去要求 ``aircraft.sensor_mode``，属**角色错用**。
#
# 正确模型（三方分离，各自成立、各自形成 evidence）：
#
#   Required Service   ：``S:rid_cooperative`` + 半径/冗余/地表策略（需求侧）
#   Ground Provider    ：地面 RID 接收节点能力（``technology`` / ``sensor_mode`` …）
#   Aircraft Particip. ：机载是否具备**参与**该服务的合作能力（协作目标 + 上报能力）
#
# 因此：
#
#   * ``type_gate_items()``（默认）＝ 地面提供者门禁字段，语义**逐字段不变**；
#   * :func:`airborne_type_items()` ＝ 机载参与能力门禁字段，由**服务身份**裁决
#     （见 :data:`AIRBORNE_PARTICIPATION_EQUIVALENTS`）；
#   * ``sensor_mode`` **绝不**出现在任何机载参与谓词里。

#: 机载参与能力的**唯一权威字段**：本机载平台支持的合作监视参与/上报服务列表。
#:
#: 形状（每一项是一条**显式声明**的合作监视参与能力）：:
#:
#:     cooperative_surveillance_services: [
#:       {"technology": "network_remote_id", "service_subtype": "cooperative_surveillance"},
#:     ]
#:
#: * 元素必须显式声明 ``technology``；
#: * ``service_subtype`` 可选，只在需求侧也要求该字段时才参与判定；
#: * 该列表是**机载事实/声明的载体**，绝不由 device_id / 机型名 / subsystem 推断；
#: * 缺该声明时，机载参与能力**保持 unknown（evidence_required）**，
#:   绝不自动降级为 "does_not_meet_under_model"。
AIRBORNE_COOPERATIVE_SURVEILLANCE_FIELD = "cooperative_surveillance_services"

#: 只有**门禁字段**才可能参与机载参与谓词。
#
#: ``service_type`` 是需求侧的用途/任务描述（自由文本），Round 2.3 已裁定它**不是**
#: 类型门禁 —— 因此它同样不得出现在机载谓词里（机载 profile 也不承载该键）。
AIRBORNE_PARTICIPATION_SOURCE_FIELDS = (
    "technology", "network_scope", "interfaces",
    "target_cooperation", "sensor_mode", "service_subtype",
)

#: 机载参与谓词里**禁止出现**的需求侧 ``type`` 字段。
#:
#: ``sensor_mode`` 描述"传感器/接收节点本身的工作模式"（雷达主动发射 = active，
#: 网络 RID 接收节点只接收 = passive）。它对 cooperative target **没有意义**：
#: 把地面接收端的 ``passive`` 拿去要求机载，等于要求"飞机必须是地面被动传感器"。
AIRBORNE_PARTICIPATION_FORBIDDEN_FIELDS = ("sensor_mode",)

#: ``type`` 字段 → 机载侧承载它的参与能力字段（等价载体映射），**按子系统**区分。
#:
#: **只有合作监视（S）的机载能力具有"参与"语义**：地面 RID 接收节点要求机载
#: 具备某种合作监视**参与/上报**能力。通信与导航的 ``type`` 字段描述的是机载
#: 自身的通信/导航装备事实，由机载 profile 直接承载，不需要任何重映射。
#:
#: * ``technology``：地面侧要求"提供者用何技术"，S 侧对应"本机支持哪种参与服务"；
#: * ``target_cooperation``：机载**就是**该服务的目标方，其目标属性由**需求侧**
#:   声明承载（机载不重复声明）。
#:
#: **``service_subtype`` 不在此列**：它是合作监视服务的**类型描述**
#: （Round 2.3 已裁定它本身不是判定谓词 —— ADS-B 亦可声称
#: ``cooperative_surveillance``）。判定 RID 的排他事实是 ``technology =
#: network_remote_id``；若再要求机载重复声明 ``service_subtype``，就又把一个
#: **描述性**字段变成了门禁，会产生新的假 unknown。
AIRBORNE_PARTICIPATION_EQUIVALENTS_BY_SUBSYSTEM = {
    "S": {
        "technology": AIRBORNE_COOPERATIVE_SURVEILLANCE_FIELD,
    },
}

#: **描述性**字段：机载侧可以如实保留（供审计），但**绝不参与**参与能力门禁。
AIRBORNE_PARTICIPATION_DESCRIPTIVE_FIELDS = ("service_subtype",)

#: 兼容投影：S 的等价映射（旧断言使用）。
AIRBORNE_PARTICIPATION_EQUIVALENTS = dict(
    AIRBORNE_PARTICIPATION_EQUIVALENTS_BY_SUBSYSTEM["S"]
)

#: ``type`` 字段 → 由需求侧自身承载、机载侧**不参与判定**的字段，**按子系统**区分。
#:
#: ``target_cooperation``：需求说"目标是合作的"，机载就是这个目标本身；要求它
#: 再声明一次是同一事实被要求两次（且无人机会被要求声明 non_cooperative 等
#: 与自身角色无关的值）。
AIRBORNE_PARTICIPATION_SELF_CARRIED_FIELDS_BY_SUBSYSTEM = {
    "S": ("target_cooperation",),
}

#: 兼容投影：S 的自承载字段。
AIRBORNE_PARTICIPATION_SELF_CARRIED_FIELDS = tuple(
    AIRBORNE_PARTICIPATION_SELF_CARRIED_FIELDS_BY_SUBSYSTEM["S"]
)

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

#: ``redundancy_by_surface`` 允许出现的 surface：只有可判定的三类。
#: ``unknown`` **不得**被配置成数字结论（fail-closed）。
REDUNDANCY_BY_SURFACE_SURFACES = ("land", "coastal_uncertain", "sea")

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
#:
#: **半径权威性铁律（Round 2 冻结）**：surface-aware service 的 authoritative geometry
#: = ``coverage_geometry.radius_by_surface``。设备条目上的单一 ``radius_m`` 只是
#: legacy CoveragePlannerV1 适配器的 compatibility-only 字段，**不得**覆盖或替代它；
#: 生产 Communication / RID 的 Coverage / Corridor / Gap / Site Planning 一律通过
#: :func:`effective_radius_m` / :func:`index_max_range_m` 按 surface 解析半径。
#:
#: **成熟度铁律**：下列参数是 Round 2 用户冻结的**工程规划基线**
#: （``engineering_planning_baseline``），**不是** manufacturer verified device
#: specification。厂家 / 频率 / 功率 / 灵敏度 / Pd / 容量 / 吞吐率 / 链路预算一律
#: 保持 ``not_evaluated``（``missing_evidence``），绝不虚构。
ENGINEERING_PLANNING_BASELINE = "engineering_planning_baseline"
PARAMETER_ORIGIN = "round2_frozen_engineering_planning_parameter"
MISSING_DEVICE_EVIDENCE = (
    "manufacturer", "model_number", "frequency", "tx_power", "sensitivity",
    "detection_probability", "capacity", "throughput", "link_budget",
)

SERVICE_SURFACE_POLICY = {
    SERVICE_KEY_COMMUNICATION: {
        "service_key": SERVICE_KEY_COMMUNICATION,
        "subsystem": "C",
        "label": "通信（Communication）",
        #: Round 2 工程规划 profile 来源标注（绝不是厂家实测规格）。
        "maturity": ENGINEERING_PLANNING_BASELINE,
        "parameter_origin": PARAMETER_ORIGIN,
        "parameter_semantics": "engineering_planning_baseline_not_verified_device_specification",
        "missing_device_evidence": list(MISSING_DEVICE_EVIDENCE),
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
        "maturity": ENGINEERING_PLANNING_BASELINE,
        "parameter_origin": PARAMETER_ORIGIN,
        "parameter_semantics": "engineering_planning_baseline_not_verified_device_specification",
        "missing_device_evidence": list(MISSING_DEVICE_EVIDENCE),
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


def validated_service_key(value, *, subsystem=None, field="service_key"):
    """**fail-closed** 的显式 service identity 校验。

    Round 2 收口：显式声明了一个不在 :data:`KNOWN_SERVICE_KEYS` 里的
    ``service_key``（例如拼写错误 ``C:comunication``）**绝不**静默降级成 legacy
    子系统默认值 —— 那会让一次拼写错误悄悄关掉整个 surface policy。此时直接
    ``ValueError``。给出 ``subsystem`` 时还要求 key 的子系统前缀一致。

    返回 ``None`` 表示"未显式声明"（调用方保持 legacy 语义）。
    """

    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    if text not in KNOWN_SERVICE_KEYS:
        raise ValueError(f"{field} 无效：{text}；必须是 {', '.join(KNOWN_SERVICE_KEYS)} 之一")
    if subsystem not in (None, ""):
        code = str(subsystem).strip().upper()
        if code in LEGACY_SERVICE_KEYS and text.split(":", 1)[0] != code:
            raise ValueError(f"{field} 的子系统前缀与 {code} 不一致：{text}")
    return text


def validated_service_subtype(value):
    """``service_subtype`` 只做形状校验；**它本身不足以判定 RID**。

    ``cooperative_surveillance`` 是通用合作监视类型描述（ADS-B 亦可声称），
    因此这里既不把它当 RID 谓词，也不拒绝其它显式类型字符串。
    """

    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def normalize_redundancy_by_surface(value, *, field="redundancy_by_surface"):
    """规范化 ``surface -> required_distinct_site_count``（canonical validation）。

    规则（Round 2 冻结）：

    * 只接受 ``land`` / ``coastal_uncertain`` / ``sea`` 三个**可判定** surface；
      ``unknown`` 不允许被配置成数字结论（出现即拒绝，fail-closed）；
    * 每个值必须是**正整数**（``bool`` / 浮点非整数 / 0 / 负数一律拒绝）；
    * 返回 ``None`` 表示"未声明"，调用方保持 legacy ``min_redundancy`` 语义。

    旧项目只有 ``min_redundancy`` 时不会走到这里，行为完全不变。
    """

    if value in (None, ""):
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{field} 必须是对象")
    result = {}
    for name in SURFACE_CLASSES:
        if name not in value:
            continue
        item = value.get(name)
        if item in (None, ""):
            continue
        if name == "unknown":
            raise ValueError(
                f"{field}.unknown 不允许配置：unknown surface 必须保持 fail-closed"
            )
        if name not in REDUNDANCY_BY_SURFACE_SURFACES:
            raise ValueError(f"{field}.{name} 不是可判定的 surface")
        if isinstance(item, bool):
            raise ValueError(f"{field}.{name} 必须是正整数")
        try:
            number = float(item)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field}.{name} 必须是正整数") from exc
        if not math.isfinite(number) or number <= 0 or not number.is_integer():
            raise ValueError(f"{field}.{name} 必须是正整数")
        result[name] = int(number)
    for name in value:
        if str(name) not in SURFACE_CLASSES:
            raise ValueError(f"{field}.{name} 不是合法的 surface 名")
    if not result:
        raise ValueError(f"{field} 至少需要一个 land/coastal_uncertain/sea 正整数要求")
    return result


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


def airborne_participation_field_for(type_name, *, subsystem=None):
    """机载参与能力承载 ``type_name`` 的字段名；不由机载重复声明时返回 ``None``。

    只有**合作监视（S）**才有"参与能力"重映射：
    :data:`AIRBORNE_PARTICIPATION_EQUIVALENTS_BY_SUBSYSTEM`。通信与导航的
    ``type`` 描述机载自身装备事实，由机载 profile 直接承载 —— 返回 ``None``
    表示"沿用机载自身字段"，绝不是"不判定"。
    """

    name = str(type_name or "").strip()
    if name in AIRBORNE_PARTICIPATION_FORBIDDEN_FIELDS:
        return None
    if name in AIRBORNE_PARTICIPATION_DESCRIPTIVE_FIELDS:
        #: 描述性字段（``service_subtype``）**不是**判定谓词：既不重映射，也不判定。
        return None
    code = str(subsystem or "").strip().upper()
    if name in AIRBORNE_PARTICIPATION_SELF_CARRIED_FIELDS_BY_SUBSYSTEM.get(code, ()):
        return None
    return AIRBORNE_PARTICIPATION_EQUIVALENTS_BY_SUBSYSTEM.get(code, {}).get(name)


def cooperative_surveillance_declarations(value):
    """规范化机载参与服务声明列表（**只读**，不新增任何事实）。

    * 只接受**显式**声明；缺失/非列表 → ``[]``（调用方据此保持 unknown）；
    * 每一项必须显式给出非空 ``technology``，否则该项被忽略（不猜测）；
    * 保持声明顺序，相同 ``(technology, service_subtype)`` 去重；
    * **向后兼容回退**：没有该列表、但机载 ``technology`` **本身就是**
      ``network_remote_id`` 时，等价于一条"网络远程识别参与"声明
      （旧项目用类型字段表达同一事实，此时不引入任何新数据）。
    """

    block = value if isinstance(value, dict) else {}
    raw = block.get(AIRBORNE_COOPERATIVE_SURVEILLANCE_FIELD)
    result = []
    seen = set()
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if not isinstance(item, dict):
                continue
            technology = str(item.get("technology") or "").strip().lower()
            if not technology:
                continue
            subtype = item.get("service_subtype")
            subtype = str(subtype).strip() if subtype not in (None, "") else None
            key = (technology, subtype)
            if key in seen:
                continue
            seen.add(key)
            entry = {"technology": technology}
            if subtype:
                entry["service_subtype"] = subtype
            result.append(entry)
        if result:
            return result
    legacy_technology = str(block.get("technology") or "").strip().lower()
    if legacy_technology == RID_TECHNOLOGY:
        legacy_subtype = block.get("service_subtype")
        entry = {"technology": RID_TECHNOLOGY}
        if legacy_subtype not in (None, ""):
            entry["service_subtype"] = str(legacy_subtype).strip()
        return [entry]
    return []


def airborne_type_items(required_type, *, subsystem=None, service_key=None):
    """把需求侧 ``type`` 块映射为**机载参与能力**的 ``(机载字段名, 期望值)``。

    子系统解析顺序：显式 ``subsystem`` → ``service_key`` 前缀 → 无从判断时
    **保持机载自身字段**（不做任何重映射，最保守）。

    * ``sensor_mode`` / ``target_cooperation`` **绝不**进入机载谓词
      （前者是地面接收节点的属性，后者由需求侧自身承载）；
    * 只有合作监视（S）的 ``technology`` / ``service_subtype`` 被映射到
      :data:`AIRBORNE_COOPERATIVE_SURVEILLANCE_FIELD`：期望"本机参与服务声明列表
      中**存在一条**声明，其属性**同时**满足全部映射期望"（因此同一承载字段的多个
      期望被合并为一条，绝不拆成两个各自独立的谓词——那会要求两条不同的声明，
      与"一条声明同时声明技术与类型"的规范形状不符）；
    * 通信 / 导航的字段**沿用机载自身同名字段**（机载装备事实本就在那里）；
    * 保留需求侧声明顺序、跳过空值；
    * 返回值里第二项对映射字段是**期望的声明属性**字典（成员匹配），对非映射
      字段是**期望值本身**（等值匹配）。调用方据此区分判定方式。
    """

    if not isinstance(required_type, dict):
        return []
    code = str(subsystem or "").strip().upper()
    if code not in LEGACY_SERVICE_KEYS:
        code = str(service_key or "").strip().split(":", 1)[0].upper()
    items = []
    mapped_index = {}
    for name, value in required_type.items():
        if value in (None, "", "unknown", []):
            continue
        if name not in AIRBORNE_PARTICIPATION_SOURCE_FIELDS:
            #: 非门禁字段（``service_type``）绝不进入机载谓词。
            continue
        field = airborne_participation_field_for(name, subsystem=code)
        if field is None:
            if name in AIRBORNE_PARTICIPATION_FORBIDDEN_FIELDS:
                continue
            if name in AIRBORNE_PARTICIPATION_DESCRIPTIVE_FIELDS:
                #: 描述性字段只如实保留，绝不参与机载参与谓词。
                continue
            if name in AIRBORNE_PARTICIPATION_SELF_CARRIED_FIELDS_BY_SUBSYSTEM.get(code, ()):
                continue
            items.append((name, value))
            continue
        if field in mapped_index:
            #: 同一承载字段的多个期望合并为**一条**声明要求。
            position = mapped_index[field]
            merged = dict(items[position][1])
            merged[name] = value
            items[position] = (field, merged)
            continue
        mapped_index[field] = len(items)
        items.append((field, {name: value}))
    return items


#: 只有下列两个**排他**事实才足以认定 RID（Round 2 收口）：
#
#: * 显式 ``service_key = S:rid_cooperative``；
#: * ``technology = network_remote_id``（网络远程识别是 RID 的专属技术）。
#:
#: **不得**单独以 ``target_cooperation = cooperative`` 或泛化的
#: ``service_subtype = cooperative_surveillance`` 判定：ADS-B 同样是
#: cooperative 的合作监视，把它们误判成 RID 会错误启用 RID 的 surface 半径策略
#: 与独立冗余池（跨 service 凑重数的风险）。这两个字段只在与上述显式事实
#: 同时出现时，作为类型描述一并保留。
RID_DECLARATION_PREDICATES = ("service_key", "technology")


def is_rid_declaration(source):
    """RID 判定只看显式 ``service_key`` / ``technology``，绝不看设备名/子系统/坐标。

    Round 2 收口：``target_cooperation=cooperative`` 与泛化的
    ``service_subtype=cooperative_surveillance`` **单独不足以**认定 RID
    （ADS-B 等其它合作监视同样 cooperative）。
    """

    item = source if isinstance(source, dict) else {}
    type_block = item.get("type") if isinstance(item.get("type"), dict) else {}
    for container in (type_block, item):
        explicit_key = str(container.get("service_key") or "").strip()
        if explicit_key == SERVICE_KEY_RID_COOPERATIVE:
            return True
        technology = str(container.get("technology") or "").strip().lower()
        if technology == RID_TECHNOLOGY:
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

    Round 2：显式声明一律经过 :func:`normalize_redundancy_by_surface` 的 canonical
    校验（正整数、拒绝 ``unknown``、拒绝非法 key）。声明非法时**不静默回落** policy，
    而是让异常上抛（fail-closed），避免"写错的配置被当成没配置"。
    """

    override = (required or {}).get("redundancy_by_surface")
    if isinstance(override, dict) and override:
        return normalize_redundancy_by_surface(override)
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


def _provider_member(provider, name):
    """读取 provider 的成员：既支持既有 **dict 形状**，也支持对象形状。

    Round 1 的 provider 是 dict（adapter 事实块）；Round 2 的
    :class:`cns_planner.domain.surface_classification.SurfaceFactsProvider` 是对象。
    两种形状都只暴露同一组接口（``classify_surface`` /
    ``classify_surface_detailed``），因此这里统一取值、不复制任何分类算法。
    """

    if provider is None:
        return None
    if isinstance(provider, dict):
        return provider.get(name)
    return getattr(provider, name, None)


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
    detailed = _provider_member(provider, "classify_surface_detailed")
    if callable(detailed):
        try:
            value = detailed(longitude, latitude)
        except Exception:
            return "unknown"
        if isinstance(value, dict):
            return normalize_surface_class(value.get("surface_class"))
        return normalize_surface_class(value)
    batch = _provider_member(provider, "classify_surface")
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
    "AIRBORNE_COOPERATIVE_SURVEILLANCE_FIELD", "AIRBORNE_PARTICIPATION_DESCRIPTIVE_FIELDS",
    "AIRBORNE_PARTICIPATION_EQUIVALENTS", "AIRBORNE_PARTICIPATION_EQUIVALENTS_BY_SUBSYSTEM",
    "AIRBORNE_PARTICIPATION_FORBIDDEN_FIELDS",
    "AIRBORNE_PARTICIPATION_SELF_CARRIED_FIELDS",
    "AIRBORNE_PARTICIPATION_SELF_CARRIED_FIELDS_BY_SUBSYSTEM",
    "AIRBORNE_PARTICIPATION_SOURCE_FIELDS",
    "COMMUNICATION_NOT_EVALUATED", "COMMUNICATION_RADIUS_BY_SURFACE",
    "COMMUNICATION_REDUNDANCY_BY_SURFACE", "ENGINEERING_PLANNING_BASELINE",
    "KNOWN_SERVICE_KEYS",
    "LEGACY_SERVICE_KEYS", "MISSING_DEVICE_EVIDENCE", "OMNIDIRECTIONAL_HEMISPHERE",
    "PARAMETER_ORIGIN", "REDUNDANCY_BY_SURFACE_SURFACES",
    "RID_DECLARATION_PREDICATES", "RID_FORBIDDEN_GEOMETRY",
    "RID_NOT_EVALUATED", "RID_RADIUS_BY_SURFACE", "RID_REDUNDANCY_BY_SURFACE",
    "RADAR_SERVICE_SUBTYPE", "RADAR_TARGET_COOPERATION", "RADAR_TECHNOLOGY",
    "RADAR_TYPE_SEMANTICS", "RID_SERVICE_SUBTYPE", "RID_TARGET_COOPERATION", "RID_TECHNOLOGY",
    "RID_TYPE_SEMANTICS", "RID_URBAN_CLASSIFICATION", "RID_URBAN_ENABLED",
    "RTK_AUGMENTATION_SERVICE_SUBTYPE", "RTK_AUGMENTATION_TECHNOLOGY",
    "RTK_AUGMENTATION_TYPE_SEMANTICS",
    "RID_URBAN_RADIUS_M", "SERVICE_KEY_COMMUNICATION",
    "SERVICE_KEY_NAVIGATION", "SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION",
    "SERVICE_KEY_RADAR_NONCOOPERATIVE", "SERVICE_KEY_RID_COOPERATIVE",
    "SERVICE_KEY_SURVEILLANCE", "SERVICE_SURFACE_POLICY", "SUBSYSTEMS",
    "SURFACE_CLASSES", "airborne_participation_field_for", "airborne_type_items",
    "cooperative_surveillance_declarations",
    "distinct_site_id_for", "effective_radius_m",
    "geometry_usable", "index_max_range_m", "is_rid_declaration",
    "legacy_min_redundancy", "normalize_redundancy_by_surface",
    "normalize_surface_class", "not_evaluated_for",
    "radius_by_surface_of", "redundancy_by_surface_of",
    "required_distinct_site_count", "resolve_surface_class", "service_contract_for",
    "service_key_for", "service_policy", "surface_class_from_provider",
    "validated_service_key", "validated_service_subtype",
]
