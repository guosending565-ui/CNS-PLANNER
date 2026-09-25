"""Application orchestration for TowerObstacleProfile and TowerColocationCandidate.

一次显式用户动作（``/api/tower-obstacle-profiles/evaluate``）同时完成两件互不混淆的事：

1. 从真实铁塔派生**障碍物高度事实**（``state["tower_obstacle_profiles"]``）——
   只服务航路净空；
2. 从同一批铁塔派生**共塔宿主候选**（``state["tower_colocation_candidates"]``）——
   只服务 CNS 规划，形状复用既有 ``CandidateSite`` 契约。

两者都**不**进入 population×shelter、Risk Framework V2 或 RouteRiskProfile 数学。
GIS 事实（FABDEM 地形正高、真实建筑足迹高度）由注入的只读 provider 提供；没有
provider 时全部塔保持 ``unresolved``，绝不伪造高度。
"""

from __future__ import annotations

from copy import deepcopy

from ..domain.cns_inputs import normalize_candidate_site
from ..domain.tower_colocation import (
    build_tower_colocation_candidates, default_tower_colocation_policy,
    normalize_tower_colocation_policy,
)
from ..domain.tower_obstacle import (
    build_tower_obstacle_profiles, normalize_tower_clearance_policy,
    normalize_tower_obstacle_policy,
)


class TowerObstacleService:
    def __init__(self, session, invalidation, snapshot, facts_provider=None):
        self.session = session
        self.invalidation = invalidation
        self.snapshot = snapshot
        self.facts_provider = facts_provider

    # ------------------------------------------------------------------ snapshots

    def result_snapshot(self):
        return deepcopy(self.session.state.get("tower_obstacle_profiles") or {})

    def colocation_snapshot(self):
        return deepcopy(self.session.state.get("tower_colocation_candidates") or {})

    def policy_snapshot(self):
        state = self.session.state
        return {
            "tower_obstacle_policy": deepcopy(state.get("tower_obstacle_policy") or {}),
            "tower_colocation_policy": deepcopy(state.get("tower_colocation_policy") or {}),
            "tower_clearance_policy": deepcopy(state.get("tower_clearance_policy") or {}),
        }

    # ------------------------------------------------------------------ policies

    def set_clearance_policy(self, payload=None):
        """保存 Tower Clearance Policy（Step03 航路净空配置）。

        只改 ``state["tower_clearance_policy"]``，两个净空**都没有默认值**：
        没有工程依据的数值不会在这里被写入。保存后：

        * 立即进入 mask ``input_fingerprint`` / ``mask_fingerprint`` 与 readiness；
        * 通过 ``invalidation.layered_route(...)`` 让既有 layered candidate / mask /
          RouteRiskProfile / LayeredRouteValidation / Safety Evidence 按既有语义 stale；
        * **不**触碰 ``grid_risk`` / ``grid_risk_v2`` / ``environment_risk``（铁塔不是风险输入），
          也不 stale 共塔宿主候选（净空不改变宿主事实）。
        """

        state = self.session.state
        payload = payload if isinstance(payload, dict) else {}
        # 既接受 {"tower_clearance_policy": {...}}，也接受直接字段形式（前端只传四个字段）。
        raw = payload["tower_clearance_policy"] if "tower_clearance_policy" in payload else {
            key: value for key, value in payload.items()
            if key in ("tower_vertical_clearance_m", "tower_horizontal_clearance_m",
                       "source", "confirmed")
        }
        candidate = normalize_tower_clearance_policy(raw)
        if candidate != state.get("tower_clearance_policy"):
            state["tower_clearance_policy"] = candidate
            self.invalidation.planning_constraint_field("tower_clearance_policy_changed")
            self.invalidation.layered_route("tower_clearance_policy_changed")
        self.session.save()
        return self.snapshot()

    # ------------------------------------------------------------------ evaluation

    def evaluate(self, payload=None, *, facts_provider=None):
        state = self.session.state
        payload = payload if isinstance(payload, dict) else {}
        changed = []

        if "tower_obstacle_policy" in payload:
            candidate = normalize_tower_obstacle_policy(payload["tower_obstacle_policy"])
            if candidate != state.get("tower_obstacle_policy"):
                state["tower_obstacle_policy"] = candidate
                changed.append("tower_obstacle_policy")
        else:
            state.setdefault("tower_obstacle_policy", normalize_tower_obstacle_policy(None))

        if "tower_colocation_policy" in payload:
            candidate = normalize_tower_colocation_policy(payload["tower_colocation_policy"])
            if candidate != state.get("tower_colocation_policy"):
                state["tower_colocation_policy"] = candidate
                changed.append("tower_colocation_policy")
        else:
            state.setdefault(
                "tower_colocation_policy", normalize_tower_colocation_policy(None),
            )

        towers = _tower_items(state.get("towers"))
        provider = facts_provider if facts_provider is not None else self.facts_provider
        facts = {}
        facts_status = "not_configured"
        if callable(provider) and towers:
            produced = provider(towers, state) or {}
            facts = produced if isinstance(produced, dict) else {}
            facts_status = "provided"
        elif towers:
            facts_status = "facts_provider_not_configured"

        profiles = build_tower_obstacle_profiles(
            list(towers.values()),
            terrain_by_tower=facts.get("terrain") or {},
            building_by_tower=facts.get("buildings") or {},
            policy=state.get("tower_obstacle_policy"),
        )
        profiles["facts_status"] = facts_status
        profiles["fact_method"] = facts.get("method")
        profiles["building_source"] = facts.get("building_source")
        profiles["tower_source"] = _tower_source(state.get("towers"))

        colocation = build_tower_colocation_candidates(
            list(towers.values()),
            obstacle_profiles=profiles,
            policy=state.get("tower_colocation_policy"),
        )
        colocation["items"] = [
            normalize_candidate_site(item, index) for index, item in enumerate(colocation["items"])
        ]
        colocation["count"] = len(colocation["items"])
        colocation["tower_count"] = len(towers)

        state["tower_obstacle_profiles"] = profiles
        state["tower_colocation_candidates"] = colocation
        statuses = state.setdefault("result_statuses", {})
        statuses["tower_obstacle_profiles"] = (
            "passed" if profiles.get("status") == "passed" else "missing_data"
        )
        statuses["tower_colocation_candidates"] = (
            "passed" if colocation.get("status") == "passed" else "missing_data"
        )
        # towers 派生事实变化：只失效真正消费它们的下游（绝不失效 Risk V2）。
        self.invalidation.tower_data_changed(
            "tower_obstacle_profiles_evaluated" if not changed else ",".join(changed),
        )
        self.session.save()
        return self.snapshot()


def _tower_items(collection):
    """``{tower_id: tower_record}``；缺失或空集合时返回空字典（不抛错）。"""

    items = (collection or {}).get("items") if isinstance(collection, dict) else None
    result = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        tower_id = str(item.get("tower_id") or "")
        if tower_id:
            result[tower_id] = item
    return result


def _tower_source(collection):
    if not isinstance(collection, dict):
        return None
    return deepcopy(collection.get("source"))


__all__ = ["TowerObstacleService"]
