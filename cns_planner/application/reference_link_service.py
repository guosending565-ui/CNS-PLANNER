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

    def reference_comparisons_snapshot(self):
        """Read-only reference ↔ planned comparisons for explicitly linked pairs.

        Never stored and never ranked: it only describes the difference between a
        confirmed reference route and the current published operational path.
        """

        from ..benchmark.reference_comparison import (
            compute_reference_comparison, reference_comparison_readiness,
        )

        state = self.session.state
        reference_routes = {
            item.get("reference_route_id"): item
            for item in ((state.get("reference_routes") or {}).get("items") or [])
        }
        links = (state.get("reference_route_links") or {}).get("items") or []
        scenario_routes = {
            item.get("route_id"): item for item in (state.get("scenario_routes") or [])
        }
        operational = (state.get("operational_routes") or [])
        comparisons, blocked = [], []
        for link in links:
            reference = reference_routes.get(link.get("reference_route_id"))
            scenario = scenario_routes.get(link.get("scenario_route_id"))
            planned_path = self._published_path(operational, link.get("scenario_route_id"))
            readiness = reference_comparison_readiness(reference, link, scenario, planned_path)
            if not readiness["ready"]:
                blocked.append({
                    "link_id": link.get("link_id"),
                    "reference_route_id": link.get("reference_route_id"),
                    "scenario_route_id": link.get("scenario_route_id"),
                    "status": "not_ready",
                    "reasons": readiness["reasons"],
                    "requires_source_crs_confirmed": True,
                    "requires_explicit_user_link": True,
                })
                continue
            comparisons.append(compute_reference_comparison(
                reference, scenario, link, planned_path=planned_path,
            ))
        return {
            "status": "passed" if comparisons else ("blocked" if links else "not_calculated"),
            "comparison_count": len(comparisons),
            "blocked_count": len(blocked),
            "comparisons": comparisons,
            "blocked": blocked,
            "reference_route_count": len(reference_routes),
            "link_count": len(links),
            "reference_source_crs_resolved": bool(
                is_resolved((state.get("reference_routes") or {}).get("crs"), role="source_crs")
            ),
            "automatic_association": False,
            "note": "仅描述差异；不产生 similarity score、排名或“更好”结论。",
        }

    @staticmethod
    def _published_path(operational_routes, scenario_route_id):
        """已发布运行航路的路径（只读；B8X 后不再从实验记录里取路径）。"""

        for route in operational_routes:
            if route.get("route_id") == scenario_route_id and route.get("path"):
                return route.get("path")
        return None

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
        links = state.get("reference_route_links") or {}
        experiments = state.get("route_planning_experiments") or {}
        source_audits = state.get("source_audits") or {}

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
                "status": "not_applicable",
                "applicability": "display_only",
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
                "v2_readiness": {"status": "not_applicable", "reason": "display_only_airspace_not_used_for_route_constraints"},
            },
        }
        data_issues = {
            "DATA-1": {
                "label": "reference CRS",
                "status": "ready" if (
                    is_resolved(landing.get("crs"), role="source_crs")
                    and is_resolved(routes.get("crs"), role="source_crs")
                ) else "blocked",
                "reasons": [
                    label for label, collection in (
                        ("reference_landing_sites", landing), ("reference_routes", routes)
                    ) if not is_resolved(collection.get("crs"), role="source_crs")
                ],
                "action": "人工输入有证据的 CRS 并确认",
            },
            "DATA-2": {
                "label": "ET→XLSX/CSV",
                "status": "ready" if routes.get("status") == "passed" else "blocked",
                "reasons": list(routes.get("warnings") or [routes.get("status") or "not_imported"]),
                "action": "转换 ET 后预览并确认导入 CSV/XLSX/GeoJSON",
            },
        }
        return {
            "status": self._overall_status({key: value for key, value in blocks.items() if key != "airspace_policies"}),
            "blocks": blocks,
            "source_audits": deepcopy(source_audits),
            "data_issues": data_issues,
            "geometry_health": {
                "reference_landing_sites": self._reference_geometry_health(landing, "items"),
                "reference_routes": self._reference_geometry_health(routes, "points"),
                "airspace": deepcopy(((state.get("grid_attributes") or {}).get("airspace") or {}).get("geometry_health") or {"status": "not_calculated"}),
            },
            "reference_route_link_count": len(links.get("items") or []),
            "experiment_count": len(experiments.get("records") or []),
            "et_source_policy": "requires_xlsx_or_csv_conversion",
            "et_parser": None,
            "note": (
                "数据就绪面板只读取来源事实；空域图层仅供显示。ET 仍要求人工转换为 XLSX/CSV。"
            ),
        }

    @staticmethod
    def _reference_geometry_health(collection, field):
        items = collection.get(field) or []
        coordinates = [item.get("coordinate") for item in items]
        valid = [point for point in coordinates if isinstance(point, list) and len(point) >= 2]
        extent = ([min(point[0] for point in valid), min(point[1] for point in valid),
                   max(point[0] for point in valid), max(point[1] for point in valid)]
                  if valid else None)
        invalid = sum(1 for item in items if item.get("quality") == "invalid")
        null = sum(1 for item in items if item.get("coordinate") is None and item.get("quality") != "invalid")
        return {
            "status": "passed" if items and invalid + null == 0 else "warning" if items else "not_calculated",
            "feature_count": len(items), "null": null, "empty": 0,
            "invalid": invalid, "unsupported": 0, "extent": extent,
            "crs": deepcopy(collection.get("crs")), "repair_applied": False,
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
    def _overall_status(blocks):
        statuses = {block["status"] for block in blocks.values()}
        if statuses <= {"not_calculated"}:
            return "not_calculated"
        if all(block["status"] == "passed" for block in blocks.values()):
            return READINESS_READY
        return READINESS_PARTIAL


def normalize_state_links(value):
    return normalize_reference_route_links(value)
