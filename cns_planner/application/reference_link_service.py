"""Explicit reference-route association and read-only data readiness reporting.

Two responsibilities, both strictly read-only with respect to planners:

* manage user-confirmed :mod:`cns_planner.domain.reference_route_link` associations
  (the system may *suggest* endpoint-distance candidates but never decides);
* summarise CRS / format / count / AirspacePolicy completeness so an expert can see
  what is and is not ready, without any automatic interpretation of layer colour,
  layer name or file extension.
"""

from __future__ import annotations

from copy import deepcopy

from ..domain.airspace import ELIGIBILITY_VALUES
from ..domain.reference_crs import is_resolved, unresolved_reason
from ..domain.reference_route_link import (
    blocked_endpoint_candidates, endpoint_candidates, normalize_reference_route_link,
    normalize_reference_route_links,
)

ENDPOINT_CANDIDATE_THRESHOLD_M = 500.0

READINESS_BLOCKED = "blocked"
READINESS_READY = "ready"
READINESS_PARTIAL = "partial"


class ReferenceLinkService:
    def __init__(self, session, invalidation, snapshot):
        self.session = session
        self.invalidation = invalidation
        self.snapshot = snapshot

    # ------------------------------------------------------------------ links

    def links_snapshot(self):
        return deepcopy(self.session.state.get("reference_route_links") or {})

    def endpoint_candidates_snapshot(self):
        """Advisory candidate hints; blocked with a reason when preconditions fail."""

        state = self.session.state
        reference_routes = (state.get("reference_routes") or {}).get("items") or []
        scenario_routes = state.get("scenario_routes") or []
        if not reference_routes or not scenario_routes:
            return blocked_endpoint_candidates(reference_routes, "reference_route_or_scenario_route_missing")
        if not is_resolved((state.get("reference_routes") or {}).get("crs"), role="source_crs"):
            return blocked_endpoint_candidates(
                reference_routes,
                unresolved_reason(
                    (state.get("reference_routes") or {}).get("crs"), role="source_crs",
                ) or "source_crs_pending_confirmation",
            )
        links = (state.get("reference_route_links") or {}).get("items") or []
        candidates = endpoint_candidates(
            reference_routes, scenario_routes, links,
            threshold_m=ENDPOINT_CANDIDATE_THRESHOLD_M,
        )
        return {
            "status": "passed" if candidates else "missing_data",
            "reason": None if candidates else "no_unlinked_scenario_reference_pair",
            "candidate_count": len(candidates),
            "candidates": candidates,
            "requires_user_confirmation": True,
            "automatic_association": False,
            "note": "候选仅为端点距离提示；系统不会自动建立 link。",
        }

    def create_link(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("reference route link 请求格式无效")
        reference_route_id = str(payload.get("reference_route_id") or "")
        scenario_route_id = str(payload.get("scenario_route_id") or "")
        reference_routes = {
            item.get("reference_route_id")
            for item in ((self.session.state.get("reference_routes") or {}).get("items") or [])
        }
        if reference_route_id not in reference_routes:
            raise ValueError("参考航线不存在")
        scenarios = {
            item.get("route_id"): item for item in (self.session.state.get("scenario_routes") or [])
        }
        if scenario_route_id not in scenarios:
            raise ValueError("场景航路不存在")
        scenario = scenarios[scenario_route_id]
        link = normalize_reference_route_link({
            "reference_route_id": reference_route_id,
            "scenario_route_id": scenario_route_id,
            "start_node_id": scenario.get("start_node_id"),
            "end_node_id": scenario.get("end_node_id"),
            "confirmed": payload.get("confirmed", True),
            "origin": payload.get("origin", "user"),
            "source": payload.get("source") or {
                "type": "user_confirmation", "note": "用户在 Step 03 显式确认参考航线与 OD 的关联",
            },
            "evidence": payload.get("evidence") or [],
            "note": payload.get("note"),
            "candidate_hint": payload.get("candidate_hint"),
        })
        current = self.links_snapshot()
        items = [item for item in (current.get("items") or []) if item["link_id"] != link["link_id"]]
        items.append(link)
        self.session.state["reference_route_links"] = normalize_reference_route_links(items)
        self.session.save()
        return self.snapshot()

    def delete_link(self, link_id):
        current = self.links_snapshot()
        items = [item for item in (current.get("items") or []) if item.get("link_id") != link_id]
        if len(items) == len(current.get("items") or []):
            raise ValueError("参考航线关联不存在")
        self.session.state["reference_route_links"] = normalize_reference_route_links(items)
        self.session.save()
        return self.snapshot()

    # ------------------------------------------------------------------ readiness

    def data_readiness(self):
        state = self.session.state
        landing = state.get("reference_landing_sites") or {}
        routes = state.get("reference_routes") or {}
        policies = state.get("airspace_policies") or {}
        eligibility = (
            (state.get("grid_attributes") or {}).get("airspace") or {}
        ).get("airspace_eligibility") or {}
        links = state.get("reference_route_links") or {}
        experiments = state.get("route_planning_experiments") or {}

        policy_items = policies.get("items") or []
        eligibility_counts = {"allowed": 0, "blocked": 0, "unknown": 0}
        for item in policy_items:
            value = str(item.get("route_eligibility") or "unknown")
            eligibility_counts[value if value in ELIGIBILITY_VALUES else "unknown"] += 1
        confirmed_count = sum(1 for item in policy_items if item.get("confirmed") is True)

        blocks = {
            "reference_landing_sites": self._collection_block(landing, "landing_site"),
            "reference_routes": self._collection_block(routes, "reference_route"),
            "airspace_policies": {
                "status": policies.get("status") or "pending_confirmation",
                "count": len(policy_items),
                "route_eligibility_counts": eligibility_counts,
                "confirmed_count": confirmed_count,
                "unconfirmed_count": len(policy_items) - confirmed_count,
                "source_count": len({
                    str((item.get("source") or {}).get("type") if isinstance(item.get("source"), dict)
                        else item.get("source"))
                    for item in policy_items
                }) if policy_items else 0,
                "eligibility_derived_from": "confirmed_policy_only",
                "never_inferred_from_layer_name_or_color": True,
                "v2_readiness": self._v2_readiness(eligibility, eligibility_counts),
            },
        }
        return {
            "status": self._overall_status(blocks),
            "blocks": blocks,
            "reference_route_link_count": len(links.get("items") or []),
            "experiment_count": len(experiments.get("records") or []),
            "et_source_policy": "requires_xlsx_or_csv_conversion",
            "et_parser": None,
            "note": (
                "数据就绪面板只读取来源事实与 policy；ET 仍要求人工转换为 XLSX/CSV，"
                "不提供 ET parser，也不按图层名称/颜色推断 suitability。"
            ),
        }

    @staticmethod
    def _collection_block(collection, label):
        crs = collection.get("crs")
        return {
            "status": collection.get("status") or "not_calculated",
            "source": deepcopy(collection.get("source")),
            "count": collection.get("count") or 0,
            "format": (collection.get("source") or {}).get("format"),
            "source_crs": deepcopy((crs or {}).get("source_crs")),
            "representation_crs": deepcopy((crs or {}).get("representation_crs")),
            "source_crs_resolved": is_resolved(crs, role="source_crs"),
            "representation_crs_resolved": is_resolved(crs, role="representation_crs"),
            "unresolved_reason": unresolved_reason(crs, role="source_crs"),
            "metric_measurement_status": collection.get("metadata", {}).get(
                "metric_measurement_status",
                "enabled" if is_resolved(crs, role="source_crs") else "disabled_unresolved_source_crs",
            ),
            "warnings": list(collection.get("warnings") or []),
            "label": label,
        }

    @staticmethod
    def _v2_readiness(eligibility, counts):
        if eligibility.get("status") == "passed" and (eligibility.get("allowed_grid_ids") or []):
            return {
                "status": "ready",
                "reason": None,
                "allowed_grid_ids": len(eligibility.get("allowed_grid_ids") or []),
            }
        if not counts["allowed"]:
            reason = "no_confirmed_allowed_airspace_policy"
        elif counts["unknown"] and not counts["blocked"]:
            reason = "only_unknown_policies_present"
        else:
            reason = eligibility.get("message") or "airspace_eligibility_not_passed"
        return {
            "status": "blocked",
            "reason": reason,
            "allowed_grid_ids": len(eligibility.get("allowed_grid_ids") or []),
            "eligibility_status": eligibility.get("status"),
        }

    @staticmethod
    def _overall_status(blocks):
        statuses = {block["status"] for block in blocks.values()}
        if statuses <= {"not_calculated"}:
            return "not_calculated"
        if all(block["status"] == "passed" for block in blocks.values()):
            return READINESS_READY
        return READINESS_PARTIAL


def normalize_state_links(value):
    return normalize_reference_route_links(value)
