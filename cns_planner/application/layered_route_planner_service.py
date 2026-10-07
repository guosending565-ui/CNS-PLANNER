"""Use cases for the layered risk-aware route planner (Theta* V2 default, V1 legacy).

Owns exactly three additive products:

* ``layered_route_planning_request`` — the explicit scenario/OD + AltitudeLayer request;
* ``layered_route_feasibility_policy`` / ``layered_route_cost_policy`` — the explicit
  engineering policies (no default clearance and no default lambda);
* ``layered_route_candidates`` — the independent candidate container (candidates +
  per-(route, layer) feasibility masks).

The service is additive by construction: it never writes ``operational_routes``,
``algorithm_selection``, ``spatial_3d``, ``route_operating_layers``, ``grid_risk_v2`` or any
CNS result, and it never switches the project's default route planner.
"""

from __future__ import annotations

from copy import deepcopy

from ..domain.layered_route import (
    COST_DOMAIN_IDS, active_cost_domains, cost_lambdas, cost_policy_fingerprint,
    cost_policy_is_runnable, default_layered_route_candidate,
    feasibility_policy_fingerprint, feasibility_policy_is_runnable,
    normalize_layered_route_candidate_collection, normalize_layered_route_cost_policy,
    normalize_layered_route_feasibility_policy, normalize_layered_route_request,
    request_fingerprint, resolve_cruise_altitude, stable_fingerprint,
)
from ..domain.communication_planning_field import (
    communication_readiness, normalize_communication_planning_field,
)
from ..domain.layered_theta_v2 import (
    default_risk_density_constraint, default_theta_v2_objective_policy,
    normalize_risk_density_constraint, normalize_theta_v2_objective_policy,
    theta_v2_search_parameter_view,
)
from ..domain.population_shelter import (
    default_shelter_coefficient_policy, normalize_population_shelter_attribute,
    normalize_shelter_coefficient_policy, population_shelter_fingerprint,
    resolve_population_shelter, shelter_policy_fingerprint, user_defined_baseline_policy,
)
from ..domain.planning_exposure import (
    PLANNING_EXPOSURE_POPULATION_FACTOR_SOURCE, default_planning_exposure_policy,
    normalize_planning_exposure_policy, planning_exposure_factors,
    planning_exposure_policy_fingerprint, resolve_planning_exposure,
)
from ..domain.surface_classification import surface_facts_input_fingerprint
from ..domain.population_nodata import CONFIRMED_ZERO_COVERAGE_STATUS
from ..domain.regulatory_constraints import (
    default_regulatory_constraints, is_configured as regulatory_is_configured,
    normalize_regulatory_constraints, regulatory_compliance_record,
)

from ..algorithms.grid.service import WorkspaceGridService
from ..data.mapping.buildings import BuildingGridService
from ..layered_route_planner.feasibility import build_layer_feasibility_mask
from ..layered_route_planner.theta_star_v2 import (
    ENDPOINT_TRANSITION_ADMISSIBILITY_DEG, PLANNER_CAPABILITY, POPULATION_FACTOR_ID,
    LayeredRiskAwareThetaStarV2, endpoint_transition_admissibility_provenance,
    grid_bearing_deg,
)
from ..domain.layered_theta_v2 import (
    ALGORITHM_ID, ALGORITHM_VERSION,
)
from ..risk.accessors_v2 import cell_factor_index
from .snapshot_read_pass import reused

POLICY_SEMANTICS = {
    "no_default_clearance_or_lambda": True,
    "null_is_not_zero": True,
    "explicit_zero_is_legal": True,
    "confirmed_requires_explicit_source": True,
    "altitude_layer_must_be_explicitly_selected": True,
    "airspace_is_display_only": True,
}

REQUEST_SEMANTICS = {
    "altitude_layer_is_explicit": True,
    "never_inferred_from_route_altitude_profile": True,
    "never_inferred_from_route_operating_layer": True,
    "route_operating_layer_is_an_operational_contract_not_a_planning_input": True,
}

#: 惰性缓存哨兵：区分"还没装配过"与"装配过但项目未配置 land-mask（null）"。
_UNSET = object()


def _route_ids(state):
    return [str(item.get("route_id")) for item in state.get("operational_routes") or []]


def _scenario_routes(state):
    return list(state.get("scenario_routes") or [])


def _candidate_key(route_id, altitude_layer_id):
    return f"{route_id or 'od'}@{altitude_layer_id or 'layer'}"


#: Round32-C：mask 里**唯一**的逐 cell 明细字段。实测 ``cells`` 约 37 MB（9,752 格），
#: 占 ``GET /api/layered-route-candidates`` 的 95%；其余字段都是主界面需要的**有界**摘要。
MASK_DETAIL_FIELD = "cells"
#: 逐 cell mask 明细的专用只读读取入口（按需 hydrate，不进入通用刷新路径）。
MASK_DETAIL_ENDPOINT = "/api/layered-route-candidates/masks"


def _mask_summary(mask):
    """把一个 coarse feasibility mask 压成有界摘要。

    保留 status / counts / current_applicability / 指纹 / adapter 等**有界**字段
    （图例与 overlay 的可用性判据都只读这些），把逐 cell ``cells`` 换成
    ``cells_count`` + ``cells_detail`` 声明。缺失 ≠ 0：没有 cells 时只如实报告
    "明细已外置"，绝不用 0 冒充"已评估且全部可行"。
    """

    if not isinstance(mask, dict):
        return mask
    summary = {name: value for name, value in mask.items() if name != MASK_DETAIL_FIELD}
    cells = mask.get(MASK_DETAIL_FIELD)
    summary["cells_count"] = len(cells) if isinstance(cells, (dict, list)) else 0
    summary["cells_detail"] = MASK_DETAIL_ENDPOINT
    return summary


def _confirmed_zero_population_cells(population_attribute):
    """当前人口属性里被显式确认为"已知 0 人口暴露"的格子集合。

    只认 population 映射自己写下的逐格结论 ``nodata_confirmed_zero_population``，该标签由
    ``population_nodata`` 契约在**已确认**的 ``nodata_is_zero_population`` 策略下产出，因此它
    是"来源 extent 之内、全 NoData = 已知 0"的逐格证据；未确认的 NoData、``missing_data`` 与
    ``outside_extent`` 都不会带上它，绝不补 0。

    **结论的权威位置是 cell 里的 ``nodata_semantics.coverage_status``**（人口映射写入的
    provenance，见 ``population_nodata.confirmed_zero_allocation``）；cell 顶层的
    ``coverage_status`` 只是兼容回退。原因是 ``PopulationGridService.backfill_legacy`` 会给旧
    项目重算顶层覆盖状态，而它的已知覆盖集合曾经不含 confirmed zero，于是历史 confirmed-zero
    格的顶层 ``coverage_status`` 被改写成 ``full``/``partial``（舟山现场实测：6052 格的顶层
    状态全是 ``full``，``nodata_semantics`` 仍完整保留）。只读顶层字段会得到**空集合**，派生场
    便继续把这些已确认的 0 当成未解析。
    """

    attribute = population_attribute if isinstance(population_attribute, dict) else {}
    cells = attribute.get("cells")
    if not isinstance(cells, dict):
        return frozenset()
    found = set()
    for grid_id, cell in cells.items():
        if not isinstance(cell, dict):
            continue
        semantics = cell.get("nodata_semantics")
        status = (
            semantics.get("coverage_status") if isinstance(semantics, dict) else None
        ) or cell.get("coverage_status")
        if str(status or "") == CONFIRMED_ZERO_COVERAGE_STATUS:
            found.add(str(grid_id))
    return frozenset(found)


def _confirmed_zero_population_signature(population_attribute, confirmed_zero_ids):
    """Fingerprint of the shelter field's 人口来源依赖：属性状态 + 已确认 0 的格子集合。"""

    attribute = population_attribute if isinstance(population_attribute, dict) else {}
    ids = sorted(str(grid_id) for grid_id in confirmed_zero_ids or ())
    status = str(attribute.get("status") or "")
    if not ids and not status:
        return None
    return stable_fingerprint({
        "population_attribute_status": status or None,
        "confirmed_zero_population_cells": ids,
    }, prefix="popzerov1-")


def _shelter_input_fingerprint(
    grid, policy, grid_risk_v2, planning_exposure_policy=None, population_attribute=None,
    planning_exposure_surface_source_fingerprint=None,
):
    """Fingerprint of everything the derived per-grid shelter field depends on.

    Population source/normalization, the grid identity, the confirmed shelter policy and the
    planning-exposure policy — so a change in any of them rebuilds the field instead of
    silently reusing it.

    ``confirmed_zero_population_signature`` 记录当前人口属性里被显式确认为"已知 0 人口暴露"
    的格子集合：该集合是 shelter 人口因子的直接输入（见 ``_population_factors``），
    ``grid_risk_v2`` 的指纹在风险结果只被标 stale、尚未重算时**不会**反映人口属性的更新，
    因此这一个依赖必须显式参与，派生场才不会复用旧的自 0 集合。
    """

    return stable_fingerprint({
        "grid_level": (grid or {}).get("level"),
        "grid_cells": sorted(
            str(cell.get("grid_id")) for cell in (grid or {}).get("cells") or []
            if isinstance(cell, dict)
        ),
        "grid_risk_v2_input_fingerprint": (grid_risk_v2 or {}).get("input_fingerprint"),
        "grid_risk_v2_policy_fingerprint": (grid_risk_v2 or {}).get("policy_fingerprint"),
        "shelter_policy_fingerprint": shelter_policy_fingerprint(policy),
        "planning_exposure_policy_fingerprint": planning_exposure_policy_fingerprint(
            planning_exposure_policy or default_planning_exposure_policy()
        ),
        # planning_exposure 的**surface 分类来源**（canonical facts / land-mask provider /
        # legacy fallback）：它改变时本层的人口因子输入语义也改变，派生场必须重建。
        "planning_exposure_surface_source_fingerprint": (
            planning_exposure_surface_source_fingerprint
        ),
        "confirmed_zero_population_signature": _confirmed_zero_population_signature(
            population_attribute, _confirmed_zero_population_cells(population_attribute)
        ),
    }, prefix="shelterinputv1-")


def _planning_exposure_input_fingerprint(
    grid, policy, grid_risk_v2, surface_source_fingerprint=None,
):
    """Fingerprint of everything the derived planning-exposure layer depends on.

    ``surface_source_fingerprint`` 是 canonical surface facts / Land-Mask provider 的身份
    （没有 canonical 来源时为 ``None`` = legacy terrain threshold 回退）：输入事实变了就不
    允许复用上一次派生的暴露度层。
    """

    return stable_fingerprint({
        "grid_level": (grid or {}).get("level"),
        "grid_cells": sorted(
            str(cell.get("grid_id")) for cell in (grid or {}).get("cells") or []
            if isinstance(cell, dict)
        ),
        "grid_risk_v2_input_fingerprint": (grid_risk_v2 or {}).get("input_fingerprint"),
        "grid_risk_v2_policy_fingerprint": (grid_risk_v2 or {}).get("policy_fingerprint"),
        "planning_exposure_policy_fingerprint": planning_exposure_policy_fingerprint(policy),
        "planning_exposure_surface_source_fingerprint": surface_source_fingerprint,
    }, prefix="planningexpinputv1-")


def _fingerprint_planner(planner):
    """A planner that exposes the declared dependency fingerprint (registry seam).

    A different registered ``layered_route_planner`` implementation may not expose
    ``fingerprints``; the production Theta* V2 definition is then used, so a legacy or
    malformed saved selection can never reactivate the archived V1 planner.
    """

    return (
        planner
        if callable(getattr(planner, "fingerprints", None))
        else LayeredRiskAwareThetaStarV2()
    )


def _existing_candidate(collection, key, request):
    """The most recent record for one (route, layer) lane, if this run repeats its inputs."""

    lane = [
        item for item in collection.get("items") or []
        if item.get("lane_key") == key
    ]
    candidate = lane[-1] if lane else None
    return {
        "candidate": candidate,
        "fingerprint": (candidate or {}).get("candidate_fingerprint"),
        "request_fingerprint": (candidate or {}).get("request_fingerprint"),
        "request_fingerprint_expected": request_fingerprint(request),
    }


class LayeredRoutePlannerService:
    def __init__(self, session, invalidation, snapshot, adapter=None, source_status=None):
        self.session = session
        self.invalidation = invalidation
        self.snapshot = snapshot
        #: Optional GIS boundary (needs QGIS/GDAL).  Without it the feasibility mask is
        #: reported blocked instead of being fabricated.
        self.adapter = adapter
        self.source_status = source_status
        #: Production execution is always Theta* V2.  A saved V1 selection remains visible
        #: to compatibility readers but is never executable.
        self.planner = LayeredRiskAwareThetaStarV2()
        #: Optional read-only communication field provider (interface only in this round).
        self.communication_provider = None
        #: BUG-SURFACE-METRIC-001：惰性装配的项目权威 land-mask provider（只用于
        #: candidate 上的 route surface distance 报告口径；不进入搜索、不改变代价）。
        self._land_mask_source_cache = _UNSET
        #: 装配失败的原因（仅诊断；``None`` = 未失败或项目本来就未配置 land-mask）。
        self._land_mask_source_error = None
        self.ensure_state()

    # ------------------------------------------------------------------ state

    def ensure_state(self):
        state = self.session.state
        state["layered_route_planning_request"] = normalize_layered_route_request(
            state.get("layered_route_planning_request")
        )
        state["layered_route_feasibility_policy"] = normalize_layered_route_feasibility_policy(
            state.get("layered_route_feasibility_policy")
        )
        state["layered_route_cost_policy"] = normalize_layered_route_cost_policy(
            state.get("layered_route_cost_policy")
        )
        #: BUG-SHOT-011：内容不变就不写回（等价重建会改变对象身份，破坏快照缓存键）。
        current = state.get("layered_route_candidates")
        normalized = normalize_layered_route_candidate_collection(current)
        if current is not normalized and normalized != current:
            state["layered_route_candidates"] = normalized
        # Additive Theta* V2 inputs.  Each ships *not configured* rather than with an assumed
        # value, except the shelter policy: the user explicitly confirmed the 1.0 baseline,
        # and that value is stored as real per-grid data in ``grid_attributes``.
        state.setdefault(
            "shelter_coefficient_policy", user_defined_baseline_policy()
        )
        state["shelter_coefficient_policy"] = normalize_shelter_coefficient_policy(
            state.get("shelter_coefficient_policy")
        )
        # The per-grid ``population_shelter`` field is derived data: it is rebuilt from the
        # canonical population factor plus this policy on demand and is deliberately kept out
        # of the persisted ``grid_attributes`` so it can never drift from its inputs.
        state["grid_attributes"].pop("population_shelter", None)
        state.setdefault("regulatory_constraints", default_regulatory_constraints())
        state["regulatory_constraints"] = normalize_regulatory_constraints(
            state.get("regulatory_constraints")
        )
        state.setdefault(
            "communication_planning_field", normalize_communication_planning_field(None)
        )
        state["communication_planning_field"] = normalize_communication_planning_field(
            state.get("communication_planning_field")
        )
        state.setdefault("theta_v2_objective_policy", default_theta_v2_objective_policy())
        state["theta_v2_objective_policy"] = normalize_theta_v2_objective_policy(
            state.get("theta_v2_objective_policy")
        )
        state.setdefault("max_route_risk_density", default_risk_density_constraint())
        state["max_route_risk_density"] = normalize_risk_density_constraint(
            state.get("max_route_risk_density")
        )
        # BUG-ROUTE-005：规划用暴露度层。默认未配置 ⇒ 完全不生效，人口因子原样使用。
        # 派生缓存 ``_planning_exposure_cache`` 刻意**不在这里**初始化：它只在真正派生过
        # 之后才存在，并与 ``_population_shelter_cache`` 一样不随项目持久化。
        state.setdefault(
            "planning_exposure_policy", default_planning_exposure_policy()
        )
        state["planning_exposure_policy"] = normalize_planning_exposure_policy(
            state.get("planning_exposure_policy")
        )
        state.setdefault("result_statuses", {}).setdefault("layered_route_candidate", "not_calculated")
        return state

    # ------------------------------------------------------------------ snapshots

    def request_snapshot(self):
        self.ensure_state()
        return deepcopy(self.session.state["layered_route_planning_request"])

    def feasibility_policy_snapshot(self):
        self.ensure_state()
        return deepcopy(self.session.state["layered_route_feasibility_policy"])

    def cost_policy_snapshot(self):
        self.ensure_state()
        return deepcopy(self.session.state["layered_route_cost_policy"])

    def result_snapshot(self):
        """Read-only projection: the persisted state never carries a derived applicability.

        Round32-J：一次快照构建内（:func:`snapshot_read_pass`）复用同一次读取结果，
        避免在同一份未被写入的 state 上重复做 14 次全量重算（真实项目约 18 s）。
        返回浅拷贝顶层，调用方改写自身返回对象不会影响复用缓存。
        """

        return dict(reused(
            self.session, "layered_route_planner.result_snapshot",
            self._build_result_snapshot,
        ))

    def _build_result_snapshot(self):
        state = self.ensure_state()
        collection = normalize_layered_route_candidate_collection(
            state.get("layered_route_candidates")
        )
        current_key, current_fingerprint = self._current_identity()
        projected = []
        for item in collection["items"]:
            entry = deepcopy(item)
            entry["current_applicability"] = self._applicability(
                item, current_key, current_fingerprint,
            )
            projected.append(entry)
        collection["items"] = projected
        collection["masks"] = {
            key: {
                **deepcopy(mask),
                "current_applicability": (
                    "current" if key == current_key and mask.get("status") != "stale" else "stale"
                ),
            }
            for key, mask in (collection.get("masks") or {}).items()
        }
        collection["current_key"] = current_key
        collection["current_candidate_fingerprint"] = current_fingerprint
        return collection

    def result_summary(self):
        """``GET /api/layered-route-candidates`` 的**有界**读投影（Round32-C）。

        与 :meth:`result_snapshot` 的唯一区别：每个 mask 的逐 cell ``cells`` 明细被压成
        ``cells_count`` + ``cells_detail`` 声明（见 :func:`_mask_summary`）。candidate
        ``items``（有界权威记录：state / 成本 / 航路点 / 指纹）原样保留。

        权威 state 与后端业务读路径**完全不变**：其他 service 一律直接消费
        :meth:`result_snapshot` / :meth:`mask_snapshot`，不经过这个交通层投影。
        """

        collection = self.result_snapshot()
        collection["masks"] = {
            key: _mask_summary(mask)
            for key, mask in (collection.get("masks") or {}).items()
        }
        collection["masks_semantics"] = (
            "bounded_projection_cells_externalized; use " + MASK_DETAIL_ENDPOINT
        )
        return collection

    def masks_snapshot(self, lane_key=None):
        """逐 cell mask 明细的按需读取入口。

        ``lane_key`` 给出时只返回该车道（地图只画 selected layer 的那一个 mask）；
        不给时返回全部 mask（审计 / 完整 hydrate）。
        """

        masks = self.result_snapshot().get("masks") or {}
        if lane_key is None:
            return deepcopy(masks)
        key = str(lane_key)
        mask = masks.get(key)
        return {key: deepcopy(mask)} if mask is not None else {}

    def current_candidate_snapshot(self, *, altitude_layer_id=None):
        """Return the authoritative current candidate without trusting stored applicability.

        ``current_applicability`` is a read-time projection, so downstream consumers must
        select from :meth:`result_snapshot` rather than taking the last persisted record.
        The active candidate wins when it is still eligible; otherwise the current lane key
        is used.  A legacy collection with neither pointer is accepted only when it has one
        unambiguous eligible candidate.
        """

        collection = self.result_snapshot()
        eligible = [
            item for item in collection.get("items") or []
            if isinstance(item, dict)
            and item.get("status") == "candidate"
            and item.get("current_applicability") == "current"
            and (
                altitude_layer_id is None
                or str(item.get("altitude_layer_id")) == str(altitude_layer_id)
            )
        ]
        active_id = collection.get("active_candidate_id")
        active = next(
            (item for item in eligible if item.get("candidate_id") == active_id), None,
        )
        if active is not None:
            return deepcopy(active)
        current_key = collection.get("current_key")
        current_lane = [item for item in eligible if item.get("lane_key") == current_key]
        if len(current_lane) == 1:
            return deepcopy(current_lane[0])
        if len(eligible) == 1:
            return deepcopy(eligible[0])
        return None

    def mask_snapshot(self, route_id=None, altitude_layer_id=None):
        collection = self.result_snapshot()
        return deepcopy((collection.get("masks") or {}).get(
            _candidate_key(route_id, altitude_layer_id)
        ))

    # ------------------------------------------------------------------ readiness

    @staticmethod
    def _workspace_grid_capability():
        """工作区网格的**软件基线**能力声明（只读，不改变任何工作区行为）。

        这里报告的是 ``WorkspaceGridService`` 的软件基线（canonical L8 /
        ``max_cells`` 资源上限 / 正式入口禁止 silent coarsening），不是当前项目网格的
        实际层级；项目实际层级始终以 ``state["grid"]["level"]`` 为准。
        """

        service = WorkspaceGridService()
        capability = service.capabilities()
        capability["capability_scope"] = "software_baseline_defaults_not_project_grid"
        capability["project_grid_level_authority"] = "project_state.grid.level"
        return capability

    def readiness_snapshot(self):
        state = self.ensure_state()
        request = state["layered_route_planning_request"]
        feasibility = state["layered_route_feasibility_policy"]
        cost = state["layered_route_cost_policy"]
        scenario_routes = _scenario_routes(state)
        layers = ((state.get("spatial_3d") or {}).get("altitude_layers") or [])
        route = self._resolve_scenario_route(request, scenario_routes)
        layer_ids = [str(item.get("altitude_layer_id")) for item in layers]
        selected_layer = next(
            (item for item in layers if str(item.get("altitude_layer_id")) == request.get("altitude_layer_id")),
            None,
        )
        cruise = resolve_cruise_altitude(selected_layer)
        source_status = self._source_status()
        collection = normalize_layered_route_candidate_collection(
            state.get("layered_route_candidates")
        )
        mask = (collection.get("masks") or {}).get(
            _candidate_key((route or {}).get("route_id"), request.get("altitude_layer_id"))
        )

        blockers = []
        if request.get("status") != "confirmed":
            blockers.append({
                "reason_code": request.get("status_reason") or "planning_request_not_confirmed",
                "reason": "尚未确认显式规划请求（scenario/OD + 显式 AltitudeLayer）",
            })
        if selected_layer is None:
            blockers.append({
                "reason_code": "altitude_layer_not_found",
                "reason": f"selected AltitudeLayer 不存在：{request.get('altitude_layer_id')}",
            })
        elif cruise.get("status") != "confirmed":
            blockers.append({
                "reason_code": str(cruise.get("reason") or "altitude_layer_not_resolvable"),
                "reason": (
                    "selected AltitudeLayer 无法解析为 canonical EGM2008 巡航高度："
                    f"{cruise.get('reason')}；V1 不做 datum/geoid 猜测或伪转换"
                ),
            })
        runnable, reason = feasibility_policy_is_runnable(feasibility)
        if not runnable:
            blockers.append({
                "reason_code": reason,
                "reason": "terrain_vertical_clearance_m 未确认（无默认值）",
            })
        runnable, reason = cost_policy_is_runnable(cost)
        if not runnable:
            blockers.append({
                "reason_code": reason,
                "reason": "LayeredRouteCostPolicy 的 λ 未全部显式确认（null != 0）",
            })
        terrain_status = (source_status or {}).get("terrain") or {}
        if not terrain_status.get("available"):
            blockers.append({
                "reason_code": "terrain_source_unavailable",
                "reason": f"verified FABDEM terrain source 不可用：{terrain_status.get('reason')}",
            })
        building_policy = state.get("building_clearance_policy") or {}
        if str(building_policy.get("status") or "") != "confirmed":
            blockers.append({
                "reason_code": "building_clearance_policy_not_confirmed",
                "reason": "既有 building_clearance_policy 未确认：不提供第二套建筑净空定义",
            })
        if not scenario_routes:
            blockers.append({
                "reason_code": "scenario_route_not_available",
                "reason": "当前项目没有 scenario/OD 航路",
            })
        if route is None and request.get("status") == "confirmed":
            blockers.append({
                "reason_code": "scenario_route_not_found",
                "reason": "planning request 指向的 scenario/OD 航路不存在",
            })
        if not (state.get("grid") or {}).get("cells"):
            blockers.append({
                "reason_code": "grid_unavailable",
                "reason": "当前项目没有 MH/T 标准网格",
            })

        return {
            "status": "ready" if not blockers else "blocked",
            # The *effective* planner bound to this project's ``layered_route_planner``
            # selection.  The frontend selects its panel from exactly this algorithm_id, so a
            # project running Theta* V2 must never be reported as the legacy V1 baseline.
            "algorithm": {
                "algorithm_id": getattr(self.planner, "algorithm_id", ALGORITHM_ID),
                "algorithm_version": getattr(
                    self.planner, "algorithm_version", ALGORITHM_VERSION
                ),
                "uses_theta_star": self._uses_theta_star(),
                "role": (
                    "production_layered_planner" if self._uses_theta_star()
                    else "legacy_baseline_layered_planner"
                ),
            },
            "request": deepcopy(request),
            "request_semantics": deepcopy(REQUEST_SEMANTICS),
            # Capability declaration (Phase 3.5, additive): which horizontal grid levels this
            # planner is declared to work on, which one it prefers, and the known gap between
            # the workspace default and the only level the L8 building facts can be mapped to.
            # Declaring this changes no search behaviour.
            "capabilities": {
                "planner": deepcopy(PLANNER_CAPABILITY),
                "building_grid": deepcopy(BuildingGridService.capabilities(
                    declared_level=(state.get("grid") or {}).get("level"),
                )),
                "workspace_grid": self._workspace_grid_capability(),
            },
            "altitude_layer_catalog": {
                "status": "configured" if layers else "not_configured",
                "count": len(layers),
                "altitude_layer_ids": layer_ids,
                "selected_altitude_layer_id": request.get("altitude_layer_id"),
                "cruise_altitude": cruise,
            },
            "scenario_route": {
                "status": "resolved" if route else "not_resolved",
                "route_id": (route or {}).get("route_id"),
                "count": len(scenario_routes),
            },
            "feasibility_policy": {
                "status": feasibility.get("status"),
                "fingerprint": feasibility_policy_fingerprint(feasibility),
                "terrain_vertical_clearance_m": feasibility.get("terrain_vertical_clearance_m"),
                "parameter_status": feasibility.get("parameter_status"),
                "source": feasibility.get("source"),
            },
            "cost_policy": {
                "status": cost.get("status"),
                "fingerprint": cost_policy_fingerprint(cost),
                "lambdas": cost_lambdas(cost),
                "active_domains": list(active_cost_domains(cost)),
                "parameter_status": cost.get("parameter_status"),
                "source": cost.get("source"),
                "domains": {
                    domain_id: {
                        "lambda": cost_lambdas(cost)[domain_id],
                        "enabled": domain_id in active_cost_domains(cost),
                        "configured": cost_lambdas(cost)[domain_id] is not None,
                    }
                    for domain_id in COST_DOMAIN_IDS
                },
            },
            "risk_framework_v2": self._risk_readiness(state),
            "theta_star_v2": self._theta_v2_readiness(state, request, cruise),
            "building_clearance_policy": {
                "status": building_policy.get("status"),
                "vertical_clearance_m": building_policy.get("vertical_clearance_m"),
                "horizontal_clearance_m": building_policy.get("horizontal_clearance_m"),
                "source": building_policy.get("source"),
                "reused_not_redefined": True,
            },
            # 真实铁塔净空：没有默认值；未配置时显式 not_configured / unresolved，
            # 绝不用一个没有工程依据的数值让航路"看起来通过了"。
            "tower_clearance_policy": _tower_clearance_readiness(state, mask),
            "sources": deepcopy(source_status or {}),
            "feasibility_mask": {
                "status": (mask or {}).get("status", "not_calculated"),
                # selected-layer mask 必须能自报属于哪一层：缺了它前端只能显示 "—"，
                # 用户无法判断「selected layer mask」是哪一层的 mask。
                "altitude_layer_id": (mask or {}).get("altitude_layer_id"),
                "counts": deepcopy((mask or {}).get("counts") or {}),
                "mask_fingerprint": (mask or {}).get("mask_fingerprint"),
                "current_applicability": (mask or {}).get("current_applicability"),
            },
            "blockers": blockers,
            "semantics": deepcopy(POLICY_SEMANTICS),
            "airspace": {
                "status": "not_applicable", "applicability": "display_only",
                "used_in_mask_search_or_fingerprint": False,
            },
            "notes": [
                "高度层必须显式选择；不从 profile 或 RouteOperatingLayer 推断。",
                "没有 confirmed clearance / λ 时一律 blocked，不提供任何默认高度、净空或权重。",
                "candidate 不是 operational route，也不自动创建 RouteOperatingLayer。",
            ],
        }

    # ------------------------------------------------------------------ writes

    def set_planning_request(self, payload):
        raw = payload.get("layered_route_planning_request", payload) if isinstance(payload, dict) else payload
        candidate = normalize_layered_route_request(raw)
        if candidate != self.session.state["layered_route_planning_request"]:
            self.session.state["layered_route_planning_request"] = candidate
            self.invalidation.layered_route("layered_route_planning_request_changed")
            self.session.save()
        return self.snapshot()

    def set_feasibility_policy(self, payload):
        raw = payload.get("layered_route_feasibility_policy", payload) if isinstance(payload, dict) else payload
        candidate = normalize_layered_route_feasibility_policy(raw)
        if candidate != self.session.state["layered_route_feasibility_policy"]:
            self.session.state["layered_route_feasibility_policy"] = candidate
            self.invalidation.planning_constraint_field("layered_route_feasibility_policy_changed")
            self.invalidation.layered_route("layered_route_feasibility_policy_changed")
            self.session.save()
        return self.snapshot()

    def set_cost_policy(self, payload):
        raw = payload.get("layered_route_cost_policy", payload) if isinstance(payload, dict) else payload
        candidate = normalize_layered_route_cost_policy(raw)
        if candidate != self.session.state["layered_route_cost_policy"]:
            self.session.state["layered_route_cost_policy"] = candidate
            self.invalidation.layered_route("layered_route_cost_policy_changed")
            self.session.save()
        return self.snapshot()

    def delete_candidate(self, candidate_id):
        state = self.ensure_state()
        collection = normalize_layered_route_candidate_collection(state["layered_route_candidates"])
        remaining = [item for item in collection["items"] if item.get("candidate_id") != candidate_id]
        if len(remaining) == len(collection["items"]):
            raise ValueError(f"未找到 layered route candidate：{candidate_id}")
        collection["items"] = remaining
        collection["count"] = len(remaining)
        if collection.get("active_candidate_id") == candidate_id:
            collection["active_candidate_id"] = None
        collection["status"] = "passed" if remaining else "not_calculated"
        state["layered_route_candidates"] = collection
        state.setdefault("result_statuses", {})["layered_route_candidate"] = collection["status"]
        self.invalidation.layered_route_validation("candidate_deleted")
        self.session.save()
        return self.snapshot()

    # ------------------------------------------------------------------ evaluate

    #: Round32-C：搜索期进度阶段值。搜索阶段的"进度"不能伪造百分比，因此阶段值恒定，
    #: 只有 message 里携带**真实**已展开 label 数。
    SEARCH_STAGE_PROGRESS = 0.35

    def evaluate(self, payload=None, *, adapter=None):
        """同步入口：**纯计算 + 唯一 production 写入**（与后台任务同源同形）。

        Round32-C 把它拆成 :meth:`plan`（只读 state 的纯计算）与
        :meth:`apply_computed`（唯一 canonical 写入者）。同步语义逐字不变：
        请求更新与候选结果仍在**同一个** ``session.save()`` 里落盘。
        """

        return self.apply_computed(self.plan(payload, adapter=adapter))

    def plan(self, payload=None, *, adapter=None, on_progress=None, cancel_check=None):
        """**纯计算**：只读 state，绝不写盘、绝不 save、绝不触发失效传播。

        ``on_progress`` / ``cancel_check`` 只在搜索期以固定低频间隔被调用，且只允许
        上报真实已展开数或抛出取消异常 —— 它们不参与任何决策，因此不可能改变结果。

        返回可发布的 outcome（不含任何不可序列化对象）：

        * ``{"outcome": "candidate", request, request_declared, key, candidate, mask}``
        * ``{"outcome": "blocked", request, request_declared, key, status,
           reason, reason_code}``（blocked 的 canonical 写法仍由 :meth:`apply_computed`
           通过既有的 ``_blocked`` 完成）。
        """

        state = self.ensure_state()
        payload = payload if isinstance(payload, dict) else {}
        request_declared = isinstance(payload.get("request"), dict)
        if request_declared:
            # 纯计算只解析出"本次生效 request"，**不写 state**：写回是 apply_computed 的事。
            request = normalize_layered_route_request(payload["request"])
        else:
            request = state["layered_route_planning_request"]
        feasibility = state["layered_route_feasibility_policy"]
        cost = state["layered_route_cost_policy"]
        grid = state.get("grid") or {}
        scenario_routes = _scenario_routes(state)
        route = self._resolve_scenario_route(request, scenario_routes)
        active_adapter = adapter or self.adapter
        key = _candidate_key((route or {}).get("route_id"), request.get("altitude_layer_id"))

        def outcome(kind, **extra):
            return {
                "outcome": kind,
                "request": request,
                "request_declared": request_declared,
                "key": key,
                **extra,
            }

        runnable, reason = feasibility_policy_is_runnable(feasibility)
        if not runnable:
            return outcome(
                "blocked", status="blocked",
                reason="LayeredRouteFeasibilityPolicy 未确认：terrain_vertical_clearance_m 无默认值",
                reason_code=reason or "feasibility_policy_not_confirmed",
            )
        if self._uses_theta_star():
            # Theta* V2 prices population × shelter risk, not the legacy Risk Framework V2
            # domain lambdas, so it does not require a LayeredRouteCostPolicy at all.
            pass
        else:
            runnable, reason = cost_policy_is_runnable(cost)
            if not runnable:
                return outcome(
                    "blocked", status="blocked",
                    reason="LayeredRouteCostPolicy 未确认：λ 无默认值（null != 0）",
                    reason_code=reason or "cost_policy_not_confirmed",
                )
        if request.get("status") != "confirmed":
            return outcome(
                "blocked", status="blocked",
                reason="显式 planning request 未确认",
                reason_code=str(request.get("status_reason") or "planning_request_not_confirmed"),
            )
        if route is None:
            return outcome(
                "blocked", status="missing_data",
                reason="planning request 指向的 scenario/OD 航路不存在",
                reason_code="scenario_route_not_found",
            )
        if not grid.get("cells"):
            return outcome(
                "blocked", status="missing_data",
                reason="当前 MH/T 标准网格不可用",
                reason_code="grid_unavailable",
            )
        layers = ((state.get("spatial_3d") or {}).get("altitude_layers") or [])
        selected_layer = next(
            (item for item in layers if str(item.get("altitude_layer_id")) == request.get("altitude_layer_id")),
            None,
        )
        cruise = resolve_cruise_altitude(selected_layer)
        if cruise.get("status") != "confirmed":
            return outcome(
                "blocked", status="blocked",
                reason="selected AltitudeLayer 无法解析为 canonical EGM2008 巡航高度；不做 datum/geoid 猜测",
                reason_code=str(cruise.get("reason") or "altitude_layer_not_resolvable"),
            )
        if active_adapter is None:
            return outcome(
                "blocked", status="missing_data",
                reason="未配置 GIS 可行性数据源（verified FABDEM + L8 building facts）：不构造假环境",
                reason_code="feasibility_adapter_not_configured",
            )

        if callable(cancel_check):
            cancel_check()
        try:
            cells = active_adapter.build_cells(list(grid["cells"]), state)
        except (TypeError, ValueError, RuntimeError) as exc:
            return outcome(
                "blocked", status="missing_data",
                reason=f"读取 coarse 地形/建筑事实失败：{exc}",
                reason_code="feasibility_source_read_failed",
            )
        building_policy = state.get("building_clearance_policy") or {}
        tower_policy = state.get("tower_clearance_policy") or {}
        describe = getattr(active_adapter, "describe", None)
        mask = build_layer_feasibility_mask(
            request=request, cruise_altitude=cruise, cells=cells,
            feasibility_policy=feasibility, building_clearance_policy=building_policy,
            source_audits=state.get("source_audits") or {}, grid_level=grid.get("level"),
            adapter=describe() if callable(describe) else None,
            tower_clearance_policy=tower_policy,
        )
        hard_constraints = payload.get("hard_constraints")
        if hard_constraints is None:
            hard_constraints = (state.get("route_constraints") or []) if isinstance(
                state.get("route_constraints"), list
            ) else []
        planner_inputs = dict(
            request=request, scenario_route=route, grid=grid, layer_mask=mask,
            grid_risk_v2=state.get("grid_risk_v2") or {},
            feasibility_policy=feasibility, cost_policy=cost,
            hard_constraints=hard_constraints,
            building_clearance_policy=building_policy,
            source_audits=state.get("source_audits") or {},
            **self._planner_specific_inputs(state, grid),
        )
        # 只读观测钩子：只在本 planner 明确声明支持时传入，绝不猜测参数名。
        if (callable(on_progress) or callable(cancel_check)) and getattr(
            self.planner, "supports_search_hook", False
        ):
            planner_inputs["search_hook"] = self._search_hook(on_progress, cancel_check)
        candidate = self.planner.plan(**planner_inputs)
        return outcome("candidate", candidate=candidate, mask=mask)

    @staticmethod
    def _search_hook(on_progress, cancel_check):
        """搜索期只读钩子：上报**真实**已展开 label 数 + cooperative cancel。

        ``on_progress`` 只收到一个整数（真实 ``statistics["expanded_labels"]``），
        由调用方决定文案与阶段进度值 —— 服务层绝不编造百分比。
        """

        def hook(expanded):
            if callable(on_progress):
                on_progress(int(expanded))
            if callable(cancel_check):
                cancel_check()

        return hook

    def apply_computed(self, outcome):
        """**唯一** canonical 写入者（同步入口与后台任务 publish 阶段共用这一步）。

        写入步骤与拆分前的 ``evaluate`` 尾部逐字相同：request 写回（若本次显式声明）、
        mask、``_store_candidate``、``result_statuses``、既有失效入口、
        **同一个** ``session.save()``。worker 永远不调用它。
        """

        state = self.ensure_state()
        outcome = outcome if isinstance(outcome, dict) else {}
        request = outcome.get("request") if isinstance(outcome.get("request"), dict) else {}
        if outcome.get("request_declared"):
            state["layered_route_planning_request"] = normalize_layered_route_request(request)
        # 以 state 里的权威 request 为准（同步路径用的就是它）。
        request = state["layered_route_planning_request"]
        key = str(outcome.get("key") or "")
        collection = normalize_layered_route_candidate_collection(state["layered_route_candidates"])
        prior = _existing_candidate(collection, key, request)
        if str(outcome.get("outcome")) == "blocked":
            return self._blocked(
                state, collection, request,
                self._resolve_scenario_route(request, _scenario_routes(state)),
                str(outcome.get("status") or "blocked"), prior,
                outcome.get("reason"), outcome.get("reason_code"), key,
            )
        candidate = outcome.get("candidate")
        if not isinstance(candidate, dict) or not candidate:
            raise ValueError("航路候选规划没有产出结果")
        mask = outcome.get("mask")
        if isinstance(mask, dict):
            collection.setdefault("masks", {})[key] = mask
        self._store_candidate(collection, key, request, candidate, prior)
        state["layered_route_candidates"] = collection
        state.setdefault("result_statuses", {})["layered_route_candidate"] = collection["status"]
        # Replacing the current candidate invalidates the frozen validation evidence only;
        # the newly created candidate itself remains current.
        self.invalidation.layered_route_validation("candidate_recomputed")
        self.session.save()
        return self.snapshot()

    # ------------------------------------------------------------------ helpers

    def _uses_theta_star(self):
        return bool(getattr(self.planner, "uses_theta_star", False))

    def _search_parameter_view(self):
        """Effective Theta* V2 search parameters + provenance for readiness/audit.

        The V1 legacy baseline has no heading/theta search parameter at all, so it reports
        ``applicable=False`` instead of inventing a value for an algorithm that never reads
        one.
        """

        if not self._uses_theta_star():
            return {
                "applicable": False,
                "algorithm_id": getattr(self.planner, "algorithm_id", ALGORITHM_ID),
                "algorithm_version": getattr(
                    self.planner, "algorithm_version", ALGORITHM_VERSION
                ),
                "reason": "legacy_layered_planner_has_no_theta_search_parameters",
                "engineering_confirmed": False,
            }
        return {
            "applicable": True,
            **theta_v2_search_parameter_view(getattr(self.planner, "search_parameters", None)),
            # 显式 planning policy（**不是** search parameter）：端点过渡可采纳性门限。
            # 它与 heading_bin_count 解耦，默认 45.0，只作 endpoint feasibility。
            "endpoint_transition": endpoint_transition_admissibility_provenance(
                getattr(
                    self.planner, "endpoint_transition_admissibility_deg",
                    ENDPOINT_TRANSITION_ADMISSIBILITY_DEG,
                )
            ),
        }

    def _land_mask_source(self):
        """BUG-SURFACE-METRIC-001：项目权威 surface classifier（land-mask provider）。

        与 Step5 的 ``surface_class_facts`` 共享**同一个**唯一分类实现
        （``gis.land_mask_source.build_land_mask_source`` → ``LandMaskSource``），但这里
        按需在内存中装配一次并缓存：它只被 planner 用于 candidate 上的
        ``route_surface_distance`` 报告字段，不进入搜索、不改变任何代价。

        未配置 land-mask 数据源时返回 ``None`` —— provider 缺失即 fail-closed，逐格分类
        保持 ``unknown``，绝不猜测成海或陆。
        """

        if self._land_mask_source_cache is not _UNSET:
            return self._land_mask_source_cache
        self._land_mask_source_cache = None
        try:
            from ..gis.land_mask_source import build_land_mask_source, land_mask_source_path

            state = self.session.state
            explicit = None
            # 正式链：ApplicationContext 把 ``surface_classification_land_mask_path`` 挂在
            # WorkflowService 上（见 ``configure_surface_classification_sources``），而本服务
            # 只拿到它的 ``snapshot`` **绑定方法** —— 因此先沿 ``__self__`` 找回宿主对象。
            # 诊断/测试链可以在 session 上放一个等价 resolver；两者都没有时回退到 canonical
            # state 里已登记的 land-mask 路径。
            holders = [self.session]
            bound_to = getattr(self.snapshot, "__self__", None)
            if bound_to is not None:
                holders.append(bound_to)
            for holder in holders:
                resolver = getattr(
                    holder, "surface_classification_land_mask_path", None,
                )
                if callable(resolver):
                    explicit = resolver()
                    if explicit:
                        break
            path = land_mask_source_path(state, explicit)
            if path:
                self._land_mask_source_cache = build_land_mask_source(
                    path, state.get("surface_classification_policy") or {},
                )
        except Exception as exc:  # noqa: BLE001 - 报告口径不可用绝不阻断规划
            self._land_mask_source_error = f"{type(exc).__name__}: {exc}"
            self._land_mask_source_cache = None
        return self._land_mask_source_cache

    def _planner_specific_inputs(self, state, grid):
        """The Theta* V2-only planning inputs; empty for the V1 A* baseline.

        Returning an empty mapping for V1 keeps a single call shape while guaranteeing that
        the V1 baseline never sees (and therefore can never be changed by) these inputs.
        """

        if not self._uses_theta_star():
            return {}
        # 规划前重新读一次 land-mask 数据源路径：项目可能在服务构造与本次规划之间（重新）
        # 配置了数据源，报告口径必须跟随当前配置，而不是启动那一刻的结论。
        self._land_mask_source_cache = _UNSET
        selected_layer_id = str(
            (state.get("layered_route_planning_request") or {}).get("altitude_layer_id") or ""
        )
        constraint_field = next((
            item for item in (state.get("planning_constraint_fields") or {}).get("items") or []
            if str(item.get("altitude_layer_id") or "") == selected_layer_id
            and str(item.get("status") or "") in ("completed", "completed_with_warnings")
        ), None)
        return {
            "population_shelter": self.population_shelter_snapshot(),
            "shelter_policy": self.shelter_policy_snapshot(),
            "regulatory_constraints": state.get("regulatory_constraints")
            or default_regulatory_constraints(),
            "communication_field": self.communication_field_snapshot(),
            "objective_policy": state.get("theta_v2_objective_policy")
            or default_theta_v2_objective_policy(),
            "risk_density_constraint": state.get("max_route_risk_density")
            or default_risk_density_constraint(),
            "constraint_field": constraint_field,
            "unknown_constraint_policy": (
                (constraint_field or {}).get("unknown_policy")
            ),
            # BUG-SURFACE-METRIC-001：只读报告口径。``planning_exposure`` 提供 terrain
            # threshold 分类，``surface_class_provider`` 提供权威 land-mask 分类；两者都
            # 不进入搜索，也不改变任何权重、代价或可行性。
            "planning_exposure": self.planning_exposure_snapshot(),
            "surface_class_provider": self._land_mask_source(),
        }

    def shelter_policy_snapshot(self):
        return deepcopy(
            self.ensure_state()["shelter_coefficient_policy"]
        )

    def set_shelter_policy(self, payload):
        raw = payload.get("shelter_coefficient_policy", payload) if isinstance(payload, dict) else payload
        candidate = normalize_shelter_coefficient_policy(raw)
        state = self.ensure_state()
        if candidate != state["shelter_coefficient_policy"]:
            state["shelter_coefficient_policy"] = candidate
            # The per-grid field is derived data: it is rebuilt from the policy below so the
            # stored coefficients can never drift from the confirmed policy.
            state["grid_attributes"]["population_shelter"] = normalize_population_shelter_attribute(None)
            self.invalidation.layered_route("shelter_coefficient_policy_changed")
            self.session.save()
        return self.snapshot()

    def _planning_exposure_surface_source(self, state):
        """planning_exposure 的**首选**陆海分类来源（canonical surface facts / LandMask）。

        返回 ``(surface_class_by_grid_id, surface_class_provider, source_fingerprint)``：

        * 项目已有 canonical ``surface_class_facts``（``status == passed`` 且有逐格分类）
          ⇒ 直接用它的**逐格**分类（与 canonical 网格同源，不做坐标查询）；
        * 否则用项目权威 land-mask provider（``LandMaskSource``，唯一分类实现）按格心判定；
        * 两者都不可用 ⇒ ``(None, None, None)``，planning_exposure 才允许走
          **legacy terrain threshold 回退**（并在 provenance 里明确记录）。

        这条链**只**决定 planning_exposure 的陆海分类来源：它不进入搜索、不改变任何代价、
        权重或可行性。
        """

        facts = state.get("surface_class_facts")
        if isinstance(facts, dict) and str(facts.get("status") or "") == "passed":
            by_grid_id = facts.get("by_grid_id")
            if isinstance(by_grid_id, dict) and by_grid_id:
                return (
                    {str(key): value for key, value in by_grid_id.items()},
                    None,
                    surface_facts_input_fingerprint(facts),
                )
        provider = self._land_mask_source()
        if provider is not None:
            describe = getattr(provider, "describe", None)
            descriptor = describe() if callable(describe) else None
            return (
                None,
                provider,
                stable_fingerprint(
                    {"source_role": "land_mask_provider", "describe": descriptor},
                    prefix="landmasksrcv1-",
                ),
            )
        return None, None, None

    def planning_exposure_snapshot(self):
        """BUG-ROUTE-005（收口后）：规划用暴露度层（**派生**，永远不进人口报告 / 审计）。

        未配置（默认）时它是 ``not_configured`` / ``applied=False``：调用方必须原样使用
        canonical 人口因子，本层绝不产生影响。启用后它对每一格套用**统一仿射映射**

            ``planning_population_factor = (1-b)*p + b*L``

        （``p`` = canonical Risk V2 归一化人口因子，``L`` = 陆地 1 / 海面 0，``b`` =
        显式确认的 ``land_relative_risk_baseline``）：同一 ``p`` 下陆海差值恒等于 ``b``，
        人口相对差异统一保留 ``(1-b)``，输出天然落在 ``[0, 1]`` 且不做 clip。真实人口密度、
        人口报告、Population NoData 与 Risk V2 canonical 因子都不受影响。

        **surface source 收口**：``L`` 的陆海分类优先消费项目已有 canonical surface facts /
        LandMask provider（``land`` / ``sea`` / ``coastal_uncertain`` / ``unknown``；
        ``coastal_uncertain`` 复用项目 canonical 的 ``effective_requirement_class = land``，
        ``unknown`` 保持 unresolved / fail-closed）。只有项目**没有**
        canonical 来源时才回退 legacy terrain threshold，并记录
        ``surface_class_source = legacy_terrain_threshold_fallback``。
        """

        state = self.ensure_state()
        policy = state["planning_exposure_policy"]
        grid = state.get("grid") or {}
        grid_risk_v2 = state.get("grid_risk_v2") or {}
        by_grid_id, provider, source_fingerprint = (
            self._planning_exposure_surface_source(state)
        )
        fingerprint = _planning_exposure_input_fingerprint(
            grid, policy, grid_risk_v2, source_fingerprint,
        )
        cached = state.get("_planning_exposure_cache")
        if isinstance(cached, dict) and cached.get("derived_from_fingerprint") == fingerprint:
            return deepcopy(cached)
        attribute = resolve_planning_exposure(
            grid=grid,
            population_attribute=state["grid_attributes"].get("population") or {},
            terrain_attribute=state["grid_attributes"].get("terrain") or {},
            normalized_population_factors=self._population_factors(state),
            policy=policy, grid_risk_v2=grid_risk_v2,
            surface_class_by_grid_id=by_grid_id,
            surface_class_provider=provider,
        )
        attribute["derived_from_fingerprint"] = fingerprint
        attribute["surface_source_fingerprint"] = source_fingerprint
        state["_planning_exposure_cache"] = attribute
        return deepcopy(attribute)

    @staticmethod
    def _population_factors(state):
        """Canonical Risk Framework V2 population factor per grid (``None`` never becomes 0).

        逐格因子 = canonical 记录里 ``status == "passed"`` 的 ``normalized_index``（含合法的
        0.0：归一化指数是 ``log1p(p)/log1p(reference)``，所以"已知 0 人口"对应真实因子 0.0）。

        补充（定向、fail-closed 保持）：canonical 结果里未解析、而**当前人口属性**已显式给出
        "已知 0 人口暴露"结论（逐格 provenance ``nodata_semantics.coverage_status ==
        nodata_confirmed_zero_population``，见 ``_confirmed_zero_population_cells``）的格子，
        其人口因子是已确认的 0.0，必须在这里按 0.0 解析。原因是人口 NoData 语义被显式确认并
        重跑人口映射之后，``grid_risk_v2`` 只被标记为 stale 而**尚未重算**，其中 6052 格仍带着
        旧的 ``missing_data`` 记录，会让 population × shelter 派生场把它们当成未解析并 fail-closed。

        边界：missing_data / nodata_only / outside_extent 一律**不**进入这个集合，仍然保持
        unresolved；本函数绝不把缺失或来源范围之外当成 0。规划用暴露度层（planning_exposure）
        已生效时，shelter 使用的是那一层的因子，本函数的补充不会被消费。
        """

        factors = {}
        grid_risk_v2 = state.get("grid_risk_v2") or {}
        for grid_id, cell in (grid_risk_v2.get("cells") or {}).items():
            index, status = cell_factor_index(cell, POPULATION_FACTOR_ID)
            if status == "passed" and index is not None:
                factors[str(grid_id)] = index
        population_attribute = (state.get("grid_attributes") or {}).get("population") or {}
        for grid_id in _confirmed_zero_population_cells(population_attribute):
            if grid_id not in factors:
                factors[grid_id] = 0.0
        return factors

    def set_planning_exposure_policy(self, payload):
        raw = payload.get("planning_exposure_policy", payload) if isinstance(
            payload, dict
        ) else payload
        candidate = normalize_planning_exposure_policy(raw)
        state = self.ensure_state()
        if candidate != state["planning_exposure_policy"]:
            state["planning_exposure_policy"] = candidate
            state.pop("_planning_exposure_cache", None)
            self.invalidation.layered_route("planning_exposure_policy_changed")
            self.session.save()
        return self.snapshot()

    def population_shelter_snapshot(self):
        """The per-grid ``population_shelter`` field, rebuilt from current canonical inputs.

        The field is *derived*: it reuses the existing canonical Risk Framework V2
        population factor and the confirmed shelter policy, and it stores a real
        ``shelter_coefficient`` per grid cell that a future confirmed shelter dataset can
        replace wholesale.  Nothing is hardcoded inside the planner.

        BUG-ROUTE-005（收口后）：当规划用暴露度层**已确认并生效**时，它提供的
        ``planning_population_factor``（``(1-b)*p + b*L``）取代 canonical 因子作为这一步规划
        的人口因子输入。地形证据缺失的格在本层里是 unresolved（``None``），因此它**不会**被
        canonical 因子回落补齐 —— fail-closed，而不是 fail-open。人口属性、人口报告、数据审计
        与 NoData 语义都不受影响。
        """

        return dict(reused(
            self.session, "layered_route_planner.population_shelter_snapshot",
            self._build_population_shelter_snapshot,
        ))

    def _build_population_shelter_snapshot(self):
        state = self.ensure_state()
        grid = state.get("grid") or {}
        policy = state["shelter_coefficient_policy"]
        grid_risk_v2 = state.get("grid_risk_v2") or {}
        population_attribute = state["grid_attributes"].get("population") or {}
        attribute = state.get("_population_shelter_cache") or {}
        cells = attribute.get("cells") or {}
        derived_from = attribute.get("derived_from_fingerprint")
        # planning_exposure 的 surface 分类来源也是本派生场的输入：来源改变时必须重建。
        _, _, surface_source_fingerprint = self._planning_exposure_surface_source(state)
        if cells and derived_from == _shelter_input_fingerprint(
            grid, policy, grid_risk_v2, state.get("planning_exposure_policy"),
            population_attribute, surface_source_fingerprint,
        ):
            return deepcopy(attribute)
        exposure = self.planning_exposure_snapshot()
        exposure_factors = planning_exposure_factors(exposure)
        # ``None`` = 本层未生效（原样用 canonical）；空 dict = 本层已生效但没有任何格算出因子
        # （全部 fail-closed）—— 后者绝不能被 ``or`` 静默回落成 canonical。
        factors = (
            exposure_factors if exposure_factors is not None
            else self._population_factors(state)
        )
        attribute = resolve_population_shelter(
            grid=grid,
            population_attribute=population_attribute,
            normalized_population_factors=factors,
            policy=policy,
        )
        attribute["planning_exposure"] = {
            "attribute": "planning_exposure",
            "status": exposure.get("status"),
            "applied": exposure.get("applied") is True,
            "policy_fingerprint": exposure.get("policy_fingerprint"),
            "formula": exposure.get("formula"),
            "land_relative_risk_baseline": exposure.get("land_relative_risk_baseline"),
            "land_water_gap": exposure.get("land_water_gap"),
            "population_difference_retention": exposure.get("population_difference_retention"),
            "land_min_surface_elevation_m": exposure.get("land_min_surface_elevation_m"),
            "surface_class_source": exposure.get("surface_class_source"),
            "surface_class_source_semantics": exposure.get("surface_class_source_semantics"),
            "legacy_terrain_threshold_fallback_used": exposure.get(
                "legacy_terrain_threshold_fallback_used"
            ),
            "canonical_surface_class_counts": exposure.get(
                "canonical_surface_class_counts"
            ),
            "land_count": exposure.get("land_count"),
            "water_count": exposure.get("water_count"),
            "unresolved_land_status_count": exposure.get("unresolved_land_status_count"),
            "applied_count": exposure.get("applied_count"),
            "land_baseline_raised_count": exposure.get("land_baseline_raised_count"),
            "land_population_floor": exposure.get("land_population_floor"),
            "land_population_floor_deprecated": True,
            "land_population_floor_used_for_planning": False,
            "used_population_nodata_as_sea_proxy": False,
        }
        attribute["population_factor_source"] = (
            PLANNING_EXPOSURE_POPULATION_FACTOR_SOURCE
            if exposure.get("applied") is True else "canonical_risk_v2_population_factor"
        )
        attribute["derived_from_fingerprint"] = _shelter_input_fingerprint(
            grid, policy, grid_risk_v2, state.get("planning_exposure_policy"),
            population_attribute, surface_source_fingerprint,
        )
        state["_population_shelter_cache"] = attribute
        return deepcopy(attribute)

    def set_regulatory_constraints(self, payload):
        raw = payload.get("regulatory_constraints", payload) if isinstance(payload, dict) else payload
        candidate = normalize_regulatory_constraints(raw)
        state = self.ensure_state()
        if candidate != state["regulatory_constraints"]:
            state["regulatory_constraints"] = candidate
            self.invalidation.planning_constraint_field("regulatory_constraints_changed")
            self.invalidation.layered_route("regulatory_constraints_changed")
            self.session.save()
        return self.snapshot()

    def communication_field_snapshot(self):
        provider = self.communication_provider
        if callable(provider):
            try:
                return normalize_communication_planning_field(provider())
            except (TypeError, ValueError, RuntimeError):
                return normalize_communication_planning_field(None)
        state = self.ensure_state()
        return deepcopy(state.get("communication_planning_field"))

    def set_communication_field(self, payload):
        raw = payload.get("communication_planning_field", payload) if isinstance(payload, dict) else payload
        candidate = normalize_communication_planning_field(raw)
        state = self.ensure_state()
        if candidate != state["communication_planning_field"]:
            state["communication_planning_field"] = candidate
            # The communication field is not a planning input in this round, so it does not
            # stale the candidates: only the readiness record changes.
            self.session.save()
        return self.snapshot()

    def set_objective_policy(self, payload):
        raw = payload.get("theta_v2_objective_policy", payload) if isinstance(payload, dict) else payload
        candidate = normalize_theta_v2_objective_policy(raw)
        state = self.ensure_state()
        if candidate != state["theta_v2_objective_policy"]:
            state["theta_v2_objective_policy"] = candidate
            self.invalidation.layered_route("theta_v2_objective_policy_changed")
            self.session.save()
        return self.snapshot()

    def set_risk_density_constraint(self, payload):
        raw = payload.get("max_route_risk_density", payload) if isinstance(payload, dict) else payload
        candidate = normalize_risk_density_constraint(raw)
        state = self.ensure_state()
        if candidate != state["max_route_risk_density"]:
            state["max_route_risk_density"] = candidate
            # A threshold change does not touch population/terrain/building raw data; it makes
            # the derived candidate evaluation stale through the layered invalidation chain.
            self.invalidation.layered_route("max_route_risk_density_changed")
            self.session.save()
        return self.snapshot()

    def refresh_for_reason(self, reason):
        """Stale the layered candidates/masks; never touch legacy routes or V3/CNS."""

        state = self.ensure_state()
        collection = normalize_layered_route_candidate_collection(state["layered_route_candidates"])
        if collection.get("status") == "not_calculated" and not collection.get("items"):
            return
        for mask in (collection.get("masks") or {}).values():
            mask["status"] = "stale"
            mask["stale_reason"] = str(reason)
        for item in collection["items"]:
            if item.get("status") in (
                "candidate", "blocked", "missing_data",
                # Phase 3.5 terminal statuses: a stale input must also stale a result that
                # ended as no_path / search_incomplete / invalid_input.
                "no_path", "search_incomplete", "invalid_input",
            ):
                item["status"] = "stale"
            item["stale_reason"] = str(reason)
        collection["status"] = "stale"
        collection["stale_reason"] = str(reason)
        state["layered_route_candidates"] = collection
        state.setdefault("result_statuses", {})["layered_route_candidate"] = "stale"

    @staticmethod
    def _resolve_scenario_route(request, scenario_routes):
        item = request if isinstance(request, dict) else {}
        route_id = item.get("scenario_route_id")
        if route_id:
            return next(
                (route for route in scenario_routes if str(route.get("route_id")) == str(route_id)),
                None,
            )
        start, end = item.get("start_node_id"), item.get("end_node_id")
        if not start or not end:
            return None
        for route in scenario_routes:
            if (
                str(route.get("start_node_id")) == str(start)
                and str(route.get("end_node_id")) == str(end)
            ):
                return route
        return None

    def _current_identity(self):
        identity = self.current_identity()
        return identity["lane_key"], identity["candidate_fingerprint"]

    def current_identity(self):
        """Re-computed identity of the current ``(route, layer)`` lane.

        Additive read-only seam for downstream analyses (e.g. RouteRiskProfile): it exposes
        the same declared-dependency fingerprints the candidate carries, so a consumer can
        prove a candidate is still current without re-deriving the fingerprint definition.
        """

        state = self.ensure_state()
        request = state["layered_route_planning_request"]
        collection = normalize_layered_route_candidate_collection(state["layered_route_candidates"])
        route = self._resolve_scenario_route(request, _scenario_routes(state))
        route_id = (route or {}).get("route_id")
        if route_id is None:
            route_id = request.get("scenario_route_id") or (
                f"{request.get('start_node_id')}->{request.get('end_node_id')}"
                if request.get("start_node_id") and request.get("end_node_id") else None
            )
        key = _candidate_key(route_id, request.get("altitude_layer_id"))
        mask = (collection.get("masks") or {}).get(key) or {}
        grid_risk_v2 = state.get("grid_risk_v2") or {}
        fingerprints = _fingerprint_planner(self.planner).fingerprints(
            request=request, scenario_route=route, grid=state.get("grid") or {},
            layer_mask=mask, grid_risk_v2=grid_risk_v2,
            cost_policy=state["layered_route_cost_policy"],
            feasibility_policy=state["layered_route_feasibility_policy"],
            hard_constraints=[], building_clearance_policy=state.get("building_clearance_policy") or {},
            source_audits=state.get("source_audits") or {},
            **self._planner_specific_inputs(state, state.get("grid") or {}),
        )
        return {
            "lane_key": key,
            "candidate_fingerprint": fingerprints["candidate_fingerprint"],
            "risk_fingerprint": fingerprints["risk_fingerprint"],
            "policy_fingerprint": fingerprints["policy_fingerprint"],
            "input_fingerprint": fingerprints["input_fingerprint"],
            "request_fingerprint": fingerprints["request_fingerprint"],
            "feasibility_mask_fingerprint": mask.get("mask_fingerprint"),
        }

    @staticmethod
    def _applicability(candidate, current_key, current_fingerprint):
        if candidate.get("status") == "stale":
            return "stale"
        if not candidate.get("lane_key"):
            return "stale"
        key = _candidate_key(candidate.get("route_id"), candidate.get("altitude_layer_id"))
        if key != current_key:
            return "stale_request_or_scenario_changed"
        if candidate.get("candidate_fingerprint") != current_fingerprint:
            return "stale_inputs_changed"
        return "current"

    def _source_status(self):
        provider = self.source_status
        if callable(provider):
            try:
                return deepcopy(provider())
            except (TypeError, ValueError, RuntimeError):
                return None
        if self.adapter is not None:
            status = getattr(self.adapter, "source_status", None)
            if callable(status):
                return deepcopy(status())
        return None

    def _theta_v2_readiness(self, state, request, cruise):
        """Readiness of the population × shelter risk and the additive interfaces."""

        policy = state["shelter_coefficient_policy"]
        attribute = self.population_shelter_snapshot()
        cells = attribute.get("cells") or {}
        unresolved = sorted(
            grid_id for grid_id, cell in cells.items()
            if (cell or {}).get("status") != "passed"
        )
        regulatory = state.get("regulatory_constraints") or default_regulatory_constraints()
        objective = state.get("theta_v2_objective_policy") or default_theta_v2_objective_policy()
        constraint = state.get("max_route_risk_density") or default_risk_density_constraint()
        blockers = []
        if self._uses_theta_star():
            if policy.get("status") != "confirmed":
                blockers.append({
                    "reason_code": str(policy.get("status_reason") or "shelter_coefficient_policy_not_confirmed"),
                    "reason": "shelter_coefficient_policy 未确认：不假设任何遮盖系数",
                })
            if not cells:
                blockers.append({
                    "reason_code": "population_shelter_field_missing",
                    "reason": "population_shelter 场缺失：risk weight>0 时 fail-closed，绝不补 0",
                })
            elif unresolved:
                blockers.append({
                    "reason_code": "population_shelter_risk_unresolved",
                    "reason": (
                        "部分 L8 cell 的 population_shelter risk_index 未解析（"
                        f"{len(unresolved)} 格）：fail-closed，绝不补 0"
                    ),
                })
            if cruise.get("status") != "confirmed":
                blockers.append({
                    "reason_code": "fixed_cruise_altitude_not_confirmed",
                    "reason": "Theta* V2 固定 z(x, y) = H，必须能解析 confirmed EGM2008 巡航高度",
                })
        return {
            "algorithm": {
                "algorithm_id": getattr(self.planner, "algorithm_id", ALGORITHM_ID),
                "algorithm_version": getattr(self.planner, "algorithm_version", ALGORITHM_VERSION),
                "uses_theta_star": self._uses_theta_star(),
            },
            "status": "ready" if not blockers else "not_ready",
            "blockers": blockers,
            # The effective search parameters *and* their provenance.  The two values are no
            # longer invisible planner constants: the software baseline is reported as such,
            # and an explicit algorithm selection override is reported as an override that is
            # still not engineering-confirmed.
            "search_parameters": self._search_parameter_view(),
            "population_shelter": {
                "status": attribute.get("status", "not_calculated"),
                "cell_count": len(cells),
                "resolved_cell_count": len(cells) - len(unresolved),
                "field_fingerprint": population_shelter_fingerprint(attribute),
                "shelter_coefficient_policy": {
                    "status": policy.get("status"),
                    "default_coefficient": policy.get("default_coefficient"),
                    "source": policy.get("source"),
                    "provenance": policy.get("provenance"),
                    "confirmed": bool(policy.get("confirmed")),
                },
                "raw_exposure_definition": "population_density_people_km2 * shelter_coefficient",
                "risk_index_definition": "normalized_population_factor * shelter_coefficient",
                "risk_index_range": [0.0, 1.0],
            },
            "objective": {
                "formula": objective.get("formula"),
                "risk_weight": objective.get("risk_weight"),
                "turn_weight": objective.get("turn_weight"),
                "distance_weight": objective.get("distance_weight"),
                "provenance": objective.get("provenance"),
                "objective_population_shelter_only": True,
            },
            "evaluation_constraint": {
                "metric": constraint.get("metric"),
                "threshold": constraint.get("threshold"),
                "source": constraint.get("source"),
                "temporary": constraint.get("temporary"),
                "role": constraint.get("role"),
                "objective_term": False,
            },
            "regulatory_constraints": {
                **regulatory_compliance_record(regulatory),
                "configured": regulatory_is_configured(regulatory),
            },
            "communication": communication_readiness(self.communication_field_snapshot()),
            "airspace": {
                "status": "not_applicable", "applicability": "display_only",
                "used_in_search": False, "used_in_hard_gate": False,
                "used_in_fingerprint": False,
            },
        }

    @staticmethod
    def _risk_readiness(state):
        result = state.get("grid_risk_v2") or {}
        policy = state.get("risk_policy_v2") or {}
        domains = {}
        for domain_id in COST_DOMAIN_IDS:
            domains[domain_id] = {
                "domain_id": domain_id,
                "status": (result.get("domains") or {}).get(domain_id, {}).get("status", "not_calculated"),
                "index_scope": "per_cell_only",
                "policy_status": (policy.get("domains") or {}).get(domain_id, {}).get(
                    "status", "pending_confirmation"
                ),
            }
        return {
            "status": result.get("status", "not_calculated"),
            "input_fingerprint": result.get("input_fingerprint"),
            "policy_fingerprint": result.get("policy_fingerprint"),
            "policy_status": policy.get("status", "pending_confirmation"),
            "domains": domains,
            "overall_used": False,
            "note": (
                "edge cost 只读取每格 domain index；domain `overall` 不使用。λ>0 的 domain 在任一"
                "候选格 missing/unresolved/pending 时该格不可遍历；λ=0 的 domain 不作为规划输入。"
            ),
        }

    def _blocked(
        self, state, collection, request, route, status, prior, reason, reason_code, key,
    ):
        candidate = default_layered_route_candidate(
            request.get("scenario_route_id") or (
                f"{request.get('start_node_id')}->{request.get('end_node_id')}"
                if request.get("start_node_id") and request.get("end_node_id") else None
            ),
            request.get("altitude_layer_id"), status,
        )
        candidate.update({
            "reason": reason,
            "blocking_reasons": [{"reason_code": reason_code, "reason": reason}],
            "candidate_id": None,
            "request_fingerprint": request_fingerprint(request),
            "feasibility_mask_fingerprint": ((collection.get("masks") or {}).get(key) or {}).get(
                "mask_fingerprint"
            ),
            "provenance": {
                "pipeline": "blocked_before_or_during_layered_planning",
                "blocked_at": reason_code,
                "airspace": {"status": "not_applicable", "applicability": "display_only"},
            },
        })
        # A blocked attempt carries the same declared dependency fingerprint as a run, so the
        # same inputs stay idempotent and a changed input both replaces it and marks the
        # previous record ``stale``.
        fingerprints = _fingerprint_planner(self.planner).fingerprints(
            request=request, scenario_route=route, grid=state.get("grid") or {},
            layer_mask=(collection.get("masks") or {}).get(key) or {},
            grid_risk_v2=state.get("grid_risk_v2") or {},
            cost_policy=state["layered_route_cost_policy"],
            feasibility_policy=state["layered_route_feasibility_policy"],
            hard_constraints=[], building_clearance_policy=state.get("building_clearance_policy") or {},
            source_audits=state.get("source_audits") or {},
            **self._planner_specific_inputs(state, state.get("grid") or {}),
        )
        candidate.update({
            "input_fingerprint": fingerprints["input_fingerprint"],
            "feasibility_fingerprint": fingerprints["feasibility_fingerprint"],
            "risk_fingerprint": fingerprints["risk_fingerprint"],
            "policy_fingerprint": fingerprints["policy_fingerprint"],
            "candidate_fingerprint": fingerprints["candidate_fingerprint"],
        })
        collection.setdefault("masks", {})
        self._store_candidate(collection, key, request, candidate, prior)
        state["layered_route_candidates"] = collection
        state.setdefault("result_statuses", {})["layered_route_candidate"] = collection["status"]
        self.invalidation.layered_route_validation("candidate_attempt_replaced_current_candidate")
        self.session.save()
        return self.snapshot()

    @staticmethod
    def _store_candidate(collection, key, request, candidate, prior):
        """Keep the result idempotent while preserving a stale record after an input change.

        A candidate is identified by its **input** fingerprint.  Re-running with unchanged
        inputs replaces that record in place; when an input changed, the previous record is
        kept and marked ``stale`` (its frozen evidence is never deleted) and its mask is
        marked ``stale`` too.
        """

        fingerprint = candidate.get("candidate_fingerprint") or candidate.get("input_fingerprint")
        candidate["lane_key"] = key
        repeated = bool(fingerprint) and prior.get("fingerprint") == fingerprint
        previous = prior.get("candidate")
        if previous is not None and not repeated:
            previous["status"] = "stale"
            previous["stale_reason"] = "inputs_changed"
        items = [
            item for item in collection["items"]
            if not (item.get("lane_key") == key and item.get("candidate_fingerprint") == fingerprint)
        ]
        items.append(candidate)
        collection["items"] = items
        collection["count"] = len(items)
        if candidate.get("status") == "candidate":
            collection["active_candidate_id"] = candidate.get("candidate_id")
        elif collection.get("active_candidate_id") == (prior.get("candidate") or {}).get(
            "candidate_id"
        ):
            collection["active_candidate_id"] = None
        collection["status"] = (
            "passed" if candidate.get("status") == "candidate" else candidate.get("status")
        )


# ``GridGraph`` is the existing MH/T adjacency/corner-guard implementation; the planner
# consumes it directly, so it is not re-implemented here.


def _tower_clearance_readiness(state, mask):
    """真实铁塔净空的 readiness 投影（只陈述配置与事实状态，不给任何结论）。"""

    state = state or {}
    policy = state.get("tower_clearance_policy") or {}
    profiles = state.get("tower_obstacle_profiles") or {}
    towers = state.get("towers") or {}
    vertical = policy.get("tower_vertical_clearance_m")
    horizontal = policy.get("tower_horizontal_clearance_m")
    configured = vertical is not None and horizontal is not None
    confirmed = str(policy.get("status") or "") == "confirmed"
    if configured and confirmed:
        status = "configured"
    elif not configured:
        status = "not_configured"
    else:
        status = "pending_confirmation"
    return {
        "status": status,
        "confirmed": confirmed,
        "tower_vertical_clearance_m": vertical,
        "tower_horizontal_clearance_m": horizontal,
        "source": policy.get("source"),
        "tower_count": towers.get("count"),
        "obstacle_profile_status": profiles.get("status") or "not_calculated",
        "obstacle_profile_resolved_count": profiles.get("resolved_count"),
        "obstacle_profile_unresolved_count": profiles.get("unresolved_count"),
        "mask_tower_cell_count": (mask or {}).get("tower_cell_count"),
        "mask_tower_unresolved_cell_count": (mask or {}).get("tower_unresolved_cell_count"),
        "semantics": "obstacle_clearance_not_a_risk_factor",
        "in_risk_framework_v2": False,
        "in_theta_star_objective": False,
    }


__all__ = ["LayeredRoutePlannerService", "POLICY_SEMANTICS", "REQUEST_SEMANTICS"]
