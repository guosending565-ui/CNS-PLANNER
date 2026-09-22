"""工程默认巡航高度层：AltitudeLayer 目录的初始化与恢复契约。

背景（Phase 3.5 人工验收）
--------------------------
真实项目恢复后 ``spatial_3d.altitude_layers`` 为空，Step03 的
``altitude layer catalog`` 报告 ``not_configured · 共 0 层``，于是
``resolve_cruise_altitude`` 直接给出 ``altitude_layer_missing``，Theta* V2 无法执行。
根因是**配置生命周期**缺陷：``AltitudeLayer`` 只有显式手工写入（``set_altitude_layer``）
一条产生路径，工作区/项目恢复时目录永远不会被补建。

本模块只解决该生命周期问题，绝不放宽任何高度检查
------------------------------------------------
* 这里补建的是 **catalog 条目**（“工程里有哪些可选巡航高度层”），
  而**不是**为某次规划选择高度层。planning request 的
  ``altitude_layer_id`` 仍然必须由用户显式选择
  （``parameter_status = "no_default_altitude_layer"`` 语义完全不变）；
* 条目仍走既有 ``normalize_altitude_layer`` 合同：显式 ``nominal_altitude_m``、
  显式 ``vertical_reference``、显式 ``source``、``confirmed``。缺任何一项都会
  保持 ``pending_confirmation``，绝不猜值；
* 只写入 ``spatial_3d.altitude_layers``，不触碰 ``route_operating_layers``、
  ``departure_arrival_procedures``、planning request、feasibility/cost policy、
  candidate、validation 或 adoption；
* 初始化是**一次性**的：首次补建后 ``altitude_layer_defaults_initialized`` 置位，
  用户显式删除高度层（直到目录为空）后不会被再次补回，空目录因此仍可被
  前端显式提示，而不是被静默填满。
"""

from copy import deepcopy

from .spatial_3d import normalize_altitude_layer

#: 目录初始化一次的持久化标记（顶层 project-state 键）。
INITIALIZED_KEY = "altitude_layer_defaults_initialized"

#: 工程默认高度层的来源标识：可追溯、非“未记录”，并且明确不是项目实测来源。
DEFAULT_ALTITUDE_LAYER_SOURCE = (
    "工程默认高度层（软件基线 ALT-060/080/100/150/200；非项目实测来源，可由工程师改写）"
)

DEFAULT_ALTITUDE_LAYER_SEMANTICS = {
    "scope": "altitude_layer_catalog_initialization_and_recovery",
    "creates_catalog_entries_only": True,
    "never_selects_a_cruise_layer_for_a_route": True,
    "planning_request_still_requires_explicit_layer_selection": True,
    "confirmed_requires_explicit_nominal_datum_and_source": True,
    "nominal_is_never_derived_from_bounds": True,
    "vertical_datum_is_never_guessed": True,
    "one_time_initialization": True,
    "empty_catalog_after_explicit_deletion_is_preserved": True,
}

#: 工程默认高度层。``nominal`` 是巡航高度；``lower`` / ``upper`` 是该层的带边界，
#: 在相邻 nominal 之间取中点（首层下界 = nominal − 20 m，末层按对称半带宽 +50 m），
#: 因此各层**互不重叠**、也不改变任何 nominal。全部为 EGM2008 正高；``lower`` / ``upper``
#: 只是工程默认的带边界（可被工程师显式改写），而非实测结论。
DEFAULT_ALTITUDE_LAYER_SPECS = (
    {"altitude_layer_id": "ALT-060", "name": "60 m 巡航高度层",
     "nominal_altitude_m": 60.0, "lower_altitude_m": 40.0, "upper_altitude_m": 70.0},
    {"altitude_layer_id": "ALT-080", "name": "80 m 巡航高度层",
     "nominal_altitude_m": 80.0, "lower_altitude_m": 70.0, "upper_altitude_m": 90.0},
    {"altitude_layer_id": "ALT-100", "name": "100 m 巡航高度层",
     "nominal_altitude_m": 100.0, "lower_altitude_m": 90.0, "upper_altitude_m": 125.0},
    {"altitude_layer_id": "ALT-150", "name": "150 m 巡航高度层",
     "nominal_altitude_m": 150.0, "lower_altitude_m": 125.0, "upper_altitude_m": 175.0},
    {"altitude_layer_id": "ALT-200", "name": "200 m 巡航高度层",
     "nominal_altitude_m": 200.0, "lower_altitude_m": 175.0, "upper_altitude_m": 250.0},
)

#: 规范未显式要求、但为可追溯性记录在 ``evidence`` 中的工程基线说明。
DEFAULT_ALTITUDE_LAYER_EVIDENCE = {
    "basis": "software_project_default",
    "engineering_confirmed": False,
    "vertical_reference": "egm2008_orthometric",
    "note": "工程默认高度层基线；不是实测或项目来源，工程师可显式改写或删除。",
}


def default_altitude_layer_payloads():
    """默认高度层的原始 payload（未经 normalize，供 normalize 与测试共用）。"""

    return [
        {
            **spec,
            "vertical_reference": "egm2008_orthometric",
            "source": DEFAULT_ALTITUDE_LAYER_SOURCE,
            "evidence": deepcopy(DEFAULT_ALTITUDE_LAYER_EVIDENCE),
            "confirmed": True,
        }
        for spec in DEFAULT_ALTITUDE_LAYER_SPECS
    ]


def default_altitude_layers():
    """默认高度层的规范化记录（catalog 的实际形态）。"""

    return [normalize_altitude_layer(item) for item in default_altitude_layer_payloads()]


def should_initialize_default_altitude_layers(state):
    """是否应在当前项目状态上补建默认高度层目录（只读判定）。

    只有“已有工作区与网格、但从未初始化过 catalog 且目录为空”的项目才补建：
    空项目（新建/清除工作区后）保持空目录，用户显式清空过的目录也保持为空。
    """

    if not isinstance(state, dict):
        return False
    if state.get(INITIALIZED_KEY):
        return False
    if not isinstance(state.get("workspace"), dict):
        return False
    if not ((state.get("grid") or {}).get("cells") or []):
        return False
    return not ((state.get("spatial_3d") or {}).get("altitude_layers") or [])


def ensure_default_altitude_layers(state, spatial=None):
    """幂等地补建默认高度层目录；返回是否发生了写入。

    发生了写入时同时把该项目的目录标记为已初始化，因此显式删除之后不会再被补回。
    """

    target = spatial if spatial is not None else state.get("spatial_3d")
    if target is None:
        state["spatial_3d"] = target = {}
    if not should_initialize_default_altitude_layers(state):
        return False
    target["altitude_layers"] = default_altitude_layers()
    state[INITIALIZED_KEY] = True
    return True


def mark_altitude_layer_catalog_managed(state):
    """记录“用户已显式管理过 catalog”，据此不再自动补建默认高度层。"""

    if isinstance(state, dict) and not state.get(INITIALIZED_KEY):
        state[INITIALIZED_KEY] = True
        return True
    return False
