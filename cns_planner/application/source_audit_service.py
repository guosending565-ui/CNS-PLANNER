"""Application boundary for local source verification and stale propagation."""

from __future__ import annotations

from copy import deepcopy

from ..domain.source_audit import (
    empty_source_audits, sha256_file, source_manifest,
)


class SourceAuditService:
    def __init__(self, session, invalidation, snapshot):
        self.session = session
        self.invalidation = invalidation
        self.snapshot = snapshot

    def result_snapshot(self, paths=None):
        collection = deepcopy(self.session.state.get("source_audits") or empty_source_audits())
        if paths:
            collection["items"] = {
                role: source_manifest(role, paths.get(role), previous=item)
                for role, item in (collection.get("items") or {}).items()
            }
        collection["count"] = len(collection.get("items") or {})
        collection["status"] = "passed" if collection["count"] else "not_calculated"
        return collection

    def verify(self, role, path, details=None):
        role = str(role or "").strip()
        if not role:
            raise ValueError("source role 缺失")
        collection = self.session.state.setdefault("source_audits", empty_source_audits())
        previous = (collection.get("items") or {}).get(role) or {}
        before_sha = (previous.get("verification") or {}).get("sha256")
        digest = sha256_file(path)
        manifest = source_manifest(
            role, path, previous=previous, details=details, verified_sha256=digest,
        )
        collection.setdefault("items", {})[role] = manifest
        collection["count"] = len(collection["items"])
        collection["status"] = "passed"
        if before_sha and before_sha != digest:
            self._source_changed(role)
            manifest["status"] = "source_changed"
            manifest["reasons"] = ["source_changed", "dependent_results_stale"]
        self.session.save()
        return self.snapshot()

    def register_quick(self, role, path, details=None, save=False):
        collection = self.session.state.setdefault("source_audits", empty_source_audits())
        previous = (collection.get("items") or {}).get(role) or {}
        manifest = source_manifest(role, path, previous=previous, details=details)
        collection.setdefault("items", {})[role] = manifest
        collection["count"] = len(collection["items"])
        collection["status"] = "passed" if collection["items"] else "not_calculated"
        if manifest["status"] == "needs_revalidation" and previous.get("status") != "needs_revalidation":
            self._source_changed(role)
        if save:
            self.session.save()
        return manifest

    def _source_changed(self, role):
        state = self.session.state
        if role in ("reference_landing_sites", "reference_routes"):
            collection = state.get(role) or {}
            crs = (collection.get("crs") or {}).get("source_crs") or {}
            if crs:
                crs["status"] = "pending_confirmation"
                crs["confirmed"] = False
                crs.setdefault("evidence", []).append({
                    "type": "source_changed", "note": "源内容变化后原 CRS 确认需重新验证",
                })
            collection.setdefault("metadata", {})["metric_measurement_status"] = "needs_revalidation"
            collection["metric_comparison_status"] = "stale_source_changed"
            collection.setdefault("warnings", []).append("source_changed_needs_revalidation")
            links = state.get("reference_route_links") or {}
            if role == "reference_routes" and links.get("items"):
                links["status"] = "stale_source_changed"
                for item in links["items"]:
                    item["current_applicability"] = "stale_source_changed"
            if role == "reference_routes":
                for experiment in (state.get("route_planning_experiments") or {}).get("records") or []:
                    experiment["reference_comparison_status"] = "stale_source_changed"
        if role in ("basemap", "airspace"):
            policies = state.get("airspace_policies") or {}
            if policies.get("items"):
                policies["status"] = "needs_revalidation"
                for item in policies["items"]:
                    item["current_applicability"] = "needs_revalidation"
                    item["confirmed"] = False
            self.invalidation.workflow("airspace_policy")
            airspace = (state.get("grid_attributes") or {}).get("airspace") or {}
            if airspace:
                airspace["status"] = "stale"
                airspace.setdefault("airspace_eligibility", {})["status"] = "stale"
        self.invalidation.grid_sources({role})
