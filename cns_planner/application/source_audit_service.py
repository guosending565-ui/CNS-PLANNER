"""Application boundary for local source verification and stale propagation."""

from __future__ import annotations

from copy import deepcopy

from ..domain.source_audit import (
    empty_source_audits, sha256_file, source_manifest, utc_now,
)


class SourceAuditService:
    def __init__(self, session, invalidation, snapshot, v3_adoption_invalidator=None):
        self.session = session
        self.invalidation = invalidation
        self.snapshot = snapshot
        #: Set by the composition root once the V3-D adoption service exists (the audit
        #: service is constructed first).  It stales V3 adoptions and only V3 adoptees.
        self.v3_adoption_invalidator = v3_adoption_invalidator

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

    #: 刷新验证时可补齐的几何事实（登记层展示用；**不**进入任何 input fingerprint）。
    GEOMETRY_FACT_KEYS = ("feature_count", "extent", "declared_crs")

    def verify(self, role, path, details=None, *, probe_geometry=True):
        role = str(role or "").strip()
        if not role:
            raise ValueError("source role 缺失")
        collection = self.session.state.setdefault("source_audits", empty_source_audits())
        previous = (collection.get("items") or {}).get(role) or {}
        before_sha = (previous.get("verification") or {}).get("sha256")
        digest = sha256_file(path)
        details = self._with_geometry_facts(details, path, probe_geometry=probe_geometry)
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
        if self._same_verification(previous, manifest):
            collection["items"][role] = previous
            return self.snapshot()
        self.session.save()
        return self.snapshot()

    @staticmethod
    def _same_verification(previous, manifest):
        def stable(value):
            value = deepcopy(value)
            value.pop("verified_at", None)
            (value.get("verification") or {}).pop("verified_at", None)
            return value
        return stable(previous) == stable(manifest)

    def apply_verified(self, result):
        """Publish worker evidence through the existing audit owner, without re-scanning."""
        from ..tasks.source_audit_task import signature
        from ..tasks.task_spec import TaskInputChanged
        role, path = result["role"], result["path"]
        provider = getattr(self, "source_path_provider", None)
        if provider:
            from pathlib import Path
            configured = provider(role)
            if not configured or Path(configured).resolve() != Path(path).resolve():
                raise TaskInputChanged("数据源配置已切换，未发布旧源结果")
        if signature(path) != result["signature"]:
            raise TaskInputChanged("源签名已变化，未发布校验结果")
        collection = self.session.state.setdefault("source_audits", empty_source_audits())
        previous = (collection.get("items") or {}).get(role) or {}
        before_sha = previous.get("sha256")
        manifest = source_manifest(role, path, previous=previous, details=result["details"],
                                   verified_sha256=result["sha256"])
        changed = bool(before_sha and before_sha != result["sha256"])
        unchanged = self._same_verification(previous, manifest)
        if not unchanged:
            collection.setdefault("items", {})[role] = manifest
            collection["count"] = len(collection["items"])
            collection["status"] = "passed"
            if changed:
                self._source_changed(role)
            self.session.save()
        return {"revision": self.session.state.get("revision"), "changed": changed,
                "state_unchanged": unchanged, "reused": result["reused"],
                "previous_sha256": before_sha, "sha256": result["sha256"],
                "elapsed_seconds": result["elapsed_seconds"],
                "audit": deepcopy(previous if unchanged else manifest)}

    def confirm_crs(self, role, path, payload):
        """Confirm only building-source CRS against the exact verified asset."""
        if role not in ("buildings", "building_grid"):
            raise ValueError("只允许 buildings/building_grid CRS 确认")
        if not isinstance(payload, dict) or payload.get("confirmed") is not True:
            raise ValueError("必须明确授权 CRS 确认")
        state = self.session.state
        if payload.get("expected_revision") != state.get("revision"):
            raise ValueError("项目 revision 已变化，请重新核对")
        previous = ((state.get("source_audits") or {}).get("items") or {}).get(role) or {}
        current = source_manifest(role, path, previous=previous)
        fingerprint = payload.get("source_fingerprint")
        if (not fingerprint or fingerprint != current.get("version_fingerprint")
                or current.get("status") != "verified" or not current.get("sha256")):
            raise ValueError("数据源未验证或指纹变化，请先验证数据源并重新核对")
        if sha256_file(path) != current["sha256"]:
            raise ValueError("数据源内容变化，请重新验证")
        from pyproj import CRS
        try:
            parsed = CRS.from_user_input(payload.get("value"))
            declared = CRS.from_user_input(current.get("declared_crs"))
        except Exception as exc:
            raise ValueError("CRS 无效或缺少源声明") from exc
        if not parsed.equals(declared):
            raise ValueError("本入口只确认源声明 CRS；坐标纠正必须另行审计")
        evidence = payload.get("evidence")
        if (not isinstance(evidence, list) or not evidence
                or any(not isinstance(item, dict) or item.get("type") != "user_supplied"
                       or not str(item.get("note") or "").strip() for item in evidence)):
            raise ValueError("必须提供人工证据与限制说明")
        authority = parsed.to_authority()
        current["confirmed_crs"] = {
            "value": ":".join(authority) if authority else parsed.to_string(),
            "status": "confirmed", "confirmed": True,
            "source": {"type": "user_confirmation", "origin": "human_adjudication"},
            "evidence": deepcopy(evidence), "source_fingerprint": fingerprint,
            "sha256": current["sha256"], "revision": state["revision"] + 1,
            "confirmed_at": utc_now(),
        }
        state["source_audits"]["items"][role] = current
        self._source_changed(role)
        self.session.save()
        return self.snapshot()

    def _with_geometry_facts(self, details, path, *, probe_geometry=True):
        """只读补齐 ``feature_count`` / ``extent`` / ``declared_crs``。

        Round35-D：``verify()`` 过去只算 SHA-256；而 ``source_manifest`` 在源文件签名
        （size / mtime）变化时刻意丢弃上一次的派生事实，于是刷新后几何事实只能是
        ``null``。这里在**调用方未显式提供**时探测一次真实文件，使刷新后的登记既
        **不继承**旧来源的失效事实，也不再是空白。

        探测失败（缺后端 / 文件不可读）不阻断验证：只记录 ``geometry_probe`` 原因。
        """

        merged = dict(details) if isinstance(details, dict) else {}
        missing = [
            key for key in self.GEOMETRY_FACT_KEYS if merged.get(key) is None
        ]
        if not probe_geometry or not missing:
            return merged
        try:
            from ..gis.source_inspection import probe_vector_source_facts
        except Exception:  # noqa: BLE001 - gis 适配层不可用时保持既有行为
            return merged
        explicit_layer = merged.get("layer_name") or merged.get("layer")
        probed = probe_vector_source_facts(path, layer_name=explicit_layer)
        for key in missing:
            value = probed.get(key)
            if value is not None:
                merged[key] = value
        merged["geometry_probe"] = {
            "status": probed.get("status"),
            "backend": probed.get("backend"),
            "reason": probed.get("reason"),
            "read_only": True,
            "semantics": "probed_from_current_file_never_inherited_from_previous_source",
        }
        if merged.get("layer_name") is None and probed.get("layer"):
            merged["layer_name"] = probed["layer"]
        return merged

    def register_quick(self, role, path, details=None, save=False):
        collection = self.session.state.setdefault("source_audits", empty_source_audits())
        previous = (collection.get("items") or {}).get(role) or {}
        from ..tasks.source_audit_task import reusable_evidence, signature
        if (role in ("buildings", "building_grid") and path
                and reusable_evidence(role, previous.get("sha256"), previous)):
            from pathlib import Path
            if Path(path).is_file() and signature(path) == {
                    "size_bytes": previous.get("size_bytes"), "mtime_ns": previous.get("mtime_ns")}:
                details = dict(details or {})
                # Startup metadata is shallow and cannot erase matching deep evidence.
                details["geometry_health"] = deepcopy(previous["geometry_health"])
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
            airspace = (state.get("grid_attributes") or {}).get("airspace") or {}
            if airspace:
                airspace["planning_applicability"] = "display_only"
        if role not in ("basemap", "airspace"):
            self.invalidation.grid_sources({role})
        # V3 adoptees depend on the tracked V3 sources; a source change stales them (and
        # their adopted routes) without touching any V1/V2 operational route.
        if role not in ("basemap", "airspace") and callable(self.v3_adoption_invalidator):
            self.v3_adoption_invalidator([role])
