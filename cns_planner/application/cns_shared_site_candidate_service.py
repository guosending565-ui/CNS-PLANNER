"""Read-only shared candidate-site projection for CNS simulations.

Round38-SITE-2 把第四类真实站址纳入同一张只读投影：**已确认真实既有 CNS 设施**。
它们此前只在 P16 的 :func:`registered_p16_site_sources` 里被间接过滤，因此
Radar / 导航等拥有独立规划算法的服务看不到它们。这里仍然只是"站址资源视图"：

* 合成 / 工程验证设施（``dataset_class`` 或显式 ``real_world_facility=False``）
  与未显式确认（``planning_profile.confirmed is not True``）的设施**不进入**候选；
* 进入候选**不代表**已安装设备，也不代表任何服务资格
  （``service_applicability`` 一律 ``not_evaluated``）。
"""

from copy import deepcopy
from math import isfinite


_SERVICE_KEYS = ("C", "N", "RID", "Radar", "RTK")

#: 与 :mod:`cns_planner.application.cns_facility_assembler` **同一套**合成/工程验证登记标记。
#: 这里按值重复一份常量，目的是不让共享站址投影依赖图面装配模块；一致性由测试断言。
SYNTHETIC_DATASET_CLASSES = ("synthetic_engineering_validation", "synthetic")


def _service_applicability():
    return {
        key: {
            "status": "not_evaluated",
            "qualification_status": "not_evaluated",
            "installation_status": "not_evaluated",
        }
        for key in _SERVICE_KEYS
    }


def _coordinate(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    try:
        lon, lat = (float(part) for part in value)
    except (TypeError, ValueError):
        return None
    if not (isfinite(lon) and isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
        return None
    return [lon, lat]


def _resolved_source_crs(crs):
    source = (crs or {}).get("source_crs") or {}
    return bool(source.get("value") and (source.get("confirmed") is True or source.get("status") == "confirmed"))


def _spatial_evidence(*, coordinate, reference_derived, crs, quality=None):
    """Classify spatial usability without treating representation CRS as source proof."""

    if coordinate is None or quality == "invalid":
        return "blocked", "invalid_coordinate"
    if reference_derived and not _resolved_source_crs(crs):
        return "blocked", "reference_source_crs_unresolved"
    if not reference_derived and crs and not _resolved_source_crs(crs):
        return "blocked", "source_crs_unresolved"
    return "eligible", None


class CNSSharedSiteCandidateService:
    """Project nodes, reference landing sites, real towers, confirmed existing CNS."""

    def __init__(self, session, landing_sites_provider, towers_provider,
                 existing_facilities_provider=None):
        self.session = session
        self._landing_sites_provider = landing_sites_provider
        self._towers_provider = towers_provider
        self._existing_facilities_provider = existing_facilities_provider

    @staticmethod
    def _base(*, candidate_id, source_id, name, kind, site_type, coordinate,
              coordinate_source, quality, crs, provenance, data_source,
              reference_derived=False, explicit_reference_site_id=None,
              existing_installation=None):
        coordinate = _coordinate(coordinate)
        status, reason = _spatial_evidence(
            coordinate=coordinate,
            reference_derived=reference_derived,
            crs=crs,
            quality=quality,
        )
        return {
            "candidate_id": candidate_id,
            "source_id": source_id,
            "name": name,
            "candidate_kind": kind,
            "site_type": site_type,
            "coordinate": coordinate,
            "coordinate_source": coordinate_source,
            "coordinate_quality": quality,
            "source_crs": deepcopy((crs or {}).get("source_crs")),
            "representation_crs": deepcopy((crs or {}).get("representation_crs")),
            "explicit_reference_site_id": explicit_reference_site_id,
            "provenance": deepcopy(provenance or {}),
            "data_source": deepcopy(data_source),
            "spatial_use_status": status,
            "spatial_block_reason": reason,
            "service_applicability": _service_applicability(),
            "existing_installation": deepcopy(existing_installation),
            "candidate_semantics": "potential_host_not_installed_or_qualified",
        }

    def snapshot(self):
        landing = self._landing_sites_provider() or {}
        towers = self._towers_provider() or {}
        references = {
            str(item.get("reference_site_id")): item
            for item in landing.get("items") or []
            if item.get("reference_site_id")
        }
        linked_reference_ids = {
            str(node.get("reference_site_id"))
            for node in self.session.state.get("nodes") or []
            if node.get("reference_site_id")
        }
        candidates = []
        for node in self.session.state.get("nodes") or []:
            node_id = str(node.get("node_id") or "")
            if not node_id:
                continue
            reference_id = node.get("reference_site_id")
            reference = references.get(str(reference_id)) if reference_id else None
            reference_derived = bool(reference_id or node.get("coordinate_source") == "reference_site")
            provenance = deepcopy(node.get("provenance") or {})
            if reference:
                provenance["reference_record"] = deepcopy(reference.get("source") or {})
            if reference_derived:
                crs = deepcopy((reference or {}).get("crs") or landing.get("crs") or {})
                quality = (reference or {}).get("quality") or provenance.get("coordinate_quality")
            else:
                crs = {
                    "source_crs": {"value": "EPSG:4326", "status": "confirmed", "confirmed": True,
                                   "source": {"type": "project_node_coordinate_contract"}},
                    "representation_crs": {"value": "OGC:CRS84", "status": "declared", "axis_order": "lon_lat"},
                }
                quality = node.get("coordinate_quality") or "recorded"
            candidates.append(self._base(
                candidate_id=f"node:{node_id}", source_id=node_id,
                name=node.get("name"), kind="project_node", site_type="landing_site",
                coordinate=node.get("coordinate"),
                coordinate_source=node.get("coordinate_source") or "legacy_project_node",
                quality=quality, crs=crs, provenance=provenance,
                data_source=(reference or {}).get("source") if reference else "ProjectState.nodes",
                reference_derived=reference_derived,
                explicit_reference_site_id=str(reference_id) if reference_id else None,
            ))

        for reference_id, item in references.items():
            if reference_id in linked_reference_ids:
                continue
            candidates.append(self._base(
                candidate_id=f"reference-landing-site:{reference_id}", source_id=reference_id,
                name=item.get("name"), kind="reference_landing_site",
                site_type=item.get("site_type"), coordinate=item.get("coordinate"),
                coordinate_source="reference_catalog", quality=item.get("quality"),
                crs=item.get("crs") or landing.get("crs") or {},
                provenance={"source": deepcopy(item.get("source") or {})},
                data_source=landing.get("source"), reference_derived=True,
                explicit_reference_site_id=reference_id,
            ))

        tower_crs = towers.get("crs") or {}
        for item in towers.get("items") or []:
            tower_id = str(item.get("tower_id") or "")
            if not tower_id:
                continue
            candidates.append(self._base(
                candidate_id=f"tower-colocation:{tower_id}", source_id=tower_id,
                name=item.get("name"), kind="tower_colocation",
                site_type=item.get("site_type") or "communication_tower",
                coordinate=[item.get("longitude"), item.get("latitude")],
                coordinate_source="real_tower_inventory", quality="reported",
                crs=tower_crs, provenance={"source": deepcopy(item.get("source") or {})},
                data_source=towers.get("data_source") or towers.get("source"),
            ))

        existing = self._existing_facilities()
        for facility in existing["items"]:
            candidates.append(facility)

        candidates.sort(key=lambda item: item["candidate_id"])
        spatial = [deepcopy(item) for item in candidates if item["spatial_use_status"] == "eligible"]
        reasons = {}
        for item in candidates:
            reason = item.get("spatial_block_reason")
            if reason:
                reasons[reason] = reasons.get(reason, 0) + 1
        kind_counts = {
            kind: sum(item["candidate_kind"] == kind for item in candidates)
            for kind in ("project_node", "reference_landing_site", "tower_colocation",
                         "existing_cns_facility")
        }
        return {
            "status": "passed",
            "candidate_semantics": "read_only_potential_hosts",
            "corridor_width_policy_version": "fixed-1000m-v1",
            "corridor_half_width_m": 500,
            "full_width_service_assurance_status": "not_evaluated",
            "candidates": deepcopy(candidates),
            "spatial_candidates": spatial,
            "counts": {
                "total": len(candidates),
                "nodes": kind_counts["project_node"],
                "reference_landing_sites": kind_counts["reference_landing_site"],
                "linked_reference_landing_sites": len(linked_reference_ids & set(references)),
                "towers": kind_counts["tower_colocation"],
                "existing_cns_facilities": kind_counts["existing_cns_facility"],
                "spatial_candidates": len(spatial),
                "spatial_blocked": len(candidates) - len(spatial),
            },
            "existing_cns_source_gate": existing["gate"],
            "source_counts": {
                "project_nodes": len(self.session.state.get("nodes") or []),
                "reference_landing_site_records": len(references),
                "tower_records": len(towers.get("items") or []),
                "existing_cns_records": existing["record_count"],
            },
            "reason_counts": dict(sorted(reasons.items())),
        }

    def _existing_facilities(self):
        """真实、**已显式确认**的既有 CNS 设施站址（合成/未确认一律不进候选）。

        ``confirmed`` 只有显式 ``True`` 才算确认：缺失、``None`` 与 ``False``
        三种情形同样被排除，否则"没有确认记录"会被静默当成"已确认"。
        判据与 P16 的 :func:`registered_p16_site_sources` 保持同一口径。
        """

        provider = self._existing_facilities_provider
        collection = provider() if callable(provider) else None
        collection = collection if isinstance(collection, dict) else {}
        items = [item for item in collection.get("items") or [] if isinstance(item, dict)]
        crs = collection.get("crs") or {
            "source_crs": {"value": "EPSG:4326", "status": "confirmed", "confirmed": True,
                           "source": {"type": "existing_cns_facility_coordinate_contract"}},
            "representation_crs": {"value": "OGC:CRS84", "status": "declared",
                                   "axis_order": "lon_lat"},
        }
        kept, excluded = [], {}
        for facility in items:
            metadata = facility.get("metadata") if isinstance(facility.get("metadata"), dict) else {}
            profile = (
                facility.get("planning_profile")
                if isinstance(facility.get("planning_profile"), dict) else {}
            )
            if (
                str(metadata.get("dataset_class") or "") in SYNTHETIC_DATASET_CLASSES
                or metadata.get("real_world_facility") is False
            ):
                excluded["synthetic_existing_cns_facility"] = (
                    excluded.get("synthetic_existing_cns_facility", 0) + 1
                )
                continue
            if profile.get("confirmed") is not True:
                excluded["unconfirmed_existing_cns_facility"] = (
                    excluded.get("unconfirmed_existing_cns_facility", 0) + 1
                )
                continue
            facility_id = str(facility.get("facility_id") or facility.get("site_id") or "")
            if not facility_id:
                excluded["missing_facility_identity"] = excluded.get("missing_facility_identity", 0) + 1
                continue
            coordinate = facility.get("coordinate")
            if not isinstance(coordinate, (list, tuple)):
                coordinate = [facility.get("longitude"), facility.get("latitude")]
            devices = [
                str(item.get("device_id"))
                for item in facility.get("devices") or []
                if isinstance(item, dict) and item.get("device_id")
            ]
            kept.append(self._base(
                candidate_id=f"existing-cns-facility:{facility_id}", source_id=facility_id,
                name=facility.get("name") or facility_id,
                kind="existing_cns_facility",
                site_type=(facility.get("site_type")
                           or metadata.get("site_type") or "existing_cns_facility"),
                coordinate=coordinate,
                coordinate_source="existing_cns_facility_record",
                quality=facility.get("coordinate_quality") or "recorded",
                crs=deepcopy(facility.get("crs") or crs),
                provenance={"source": deepcopy(facility.get("source") or {}),
                            "real_world_gate": "real_world_confirmed_only"},
                data_source="ProjectState.existing_cns_facilities",
                existing_installation={
                    "semantics": "existing_devices_only_do_not_imply_service_qualification",
                    "device_ids": devices,
                    "device_count": len(devices),
                    "confirmed": True,
                },
            ))
        return {
            "items": kept,
            "record_count": len(items),
            "gate": {
                "source_policy": "real_world_confirmed_only",
                "synthetic_dataset_classes": list(SYNTHETIC_DATASET_CLASSES),
                "excluded_counts": dict(sorted(excluded.items())),
            },
        }
