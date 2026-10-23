"""Isolated engineering-assumption candidate pool and scenario evaluation (R5).

This module is the **only** place where a potential Zhoushan facility, tower,
landing site or ``.ovobj`` label is converted into a scenario candidate site and
fed to the existing candidate-action constructor.  It is deliberately separate
from :mod:`cns_planner.application.corridor_site_planning_service` and from the
formal ``candidate_sites`` writer:

* no method writes ``ProjectState``, ``candidate_sites`` or a project file;
* no method changes P16's greedy order, reuse tiers, coverage formula, device
  catalog, continuous-service threshold, or formal eligibility gates;
* every scenario site/action/result carries ``assumption_only=true`` and
  ``real_world_confirmed=false``;
* the scenario projection sets ``planning_profile.confirmed=true`` **only on a
  disposable copy** so the existing ``candidate_actions`` contract can be
  exercised.  The canonical scenario record keeps ``confirmed=false`` and the
  projection is marked ``projection_only=true``.

The only expensive operation exposed here is
:meth:`EngineeringAssumptionScenarioService.evaluate_explicit_actions`, which
re-runs the existing P14→P15 corridor chain exactly once on a caller-owned state
copy.  It is intended for a single, explicit B+C what-if, not for a search loop.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from math import hypot, isfinite
from pathlib import Path

from pyproj import Geod

from ..algorithms.corridor.v1 import CNSServiceCorridorV1
from ..algorithms.corridor_gap.v1 import CNSCorridorGapAnalyzerV1
from ..domain.engineering_assumption import (
    ASSUMPTION_ONLY_KEY,
    CANDIDATE_REUSE_CLASS,
    DEFAULT_MOUNT_HEIGHT_AGL_M,
    ENGINEERING_ASSUMPTION_MODE,
    NEW_BUILD_REUSE_CLASS,
    REAL_WORLD_CONFIRMED_KEY,
    SCENARIO_SCHEMA_VERSION,
    TOWER_REUSE_CLASS,
    EngineeringAssumptionError,
    assumption_contract,
    normalize_engineering_assumption_scenario,
    scenario_site_marker,
)
from ..reference_data.ovobj_sites import (
    GROUND_TYPE,
    INDOOR_TYPE,
    MICRO_TYPE,
    ROOF_TYPE,
    VIRTUAL_TYPE,
    normalize_ovobj_name,
)
from .corridor_site_planning_service import (
    _apply_cumulative_action,
    _continuous_residual_segments,
    _continuous_service_acceptance,
    rerun_corridor_chain,
)
from .site_candidate_actions import candidate_actions

GEOD = Geod(ellps="WGS84")

#: The scenario action inventory asks for all devices the scenario declares.
#: ``N`` is declared as an allowed subsystem but has no engineering device
#: model in the current catalog, so it correctly produces zero actions.
SCENARIO_TARGETS = (
    {"subsystem": "C", "service_key": "C:communication"},
    {"subsystem": "S", "service_key": "S:rid_cooperative"},
)

#: The fixed P16/P17 continuous-service comparison threshold.  The evaluator
#: still reads the threshold and speed from the project state; these values are
#: only used to assert that the frozen scenario is the one the user specified.
EXPECTED_THRESHOLD_M = 45.0
EXPECTED_ROUTE_SPEED_MPS = 15.0

GROUND_TYPE_VALUES = (GROUND_TYPE, ROOF_TYPE)
EXCLUDED_OVOBJ_TYPES = (INDOOR_TYPE, MICRO_TYPE, VIRTUAL_TYPE)

#: R5-H：陆海筛选版本。它只改变“场景候选池要不要为海上身份生成陆基动作”，
#: 不改变 P16 正式资格、设备半径、选择上限或 P14–P17 结果。
SURFACE_SCREENING_SCHEMA_VERSION = "r5h-surface-screening-v1"
SURFACE_CLASSES = ("land", "coastal_uncertain", "sea", "unknown")
SURFACE_DECISIONS = {
    "land": "retain_engineering_assumption",
    "coastal_uncertain": "retain_pending_surface_review_not_buildability_confirmation",
    "sea": "exclude_land_assumption_no_offshore_platform_evidence",
    "unknown": "blocked_surface_unknown_fail_closed",
}
SURFACE_RETAINED_CLASSES = ("land", "coastal_uncertain")
#: 本轮没有登记的海上平台证据：sea 一律按“无证据”排除陆基假设，绝不臆造平台。
OFFSHORE_PLATFORM_EVIDENCE = {
    "available": False,
    "source": None,
    "semantics": "no_registered_offshore_platform_evidence_in_project_state_or_scenario",
}


def _coord(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    try:
        lon, lat = float(value[0]), float(value[1])
    except (TypeError, ValueError):
        return None
    if not (isfinite(lon) and isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
        return None
    return [lon, lat]


def _distance_m(a, b):
    return float(GEOD.inv(a[0], a[1], b[0], b[1])[2])


def _round_coord(coord, digits=6):
    return (round(float(coord[0]), digits), round(float(coord[1]), digits))


def _sha256_file(path):
    digest = sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 22), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _merge_scenario_files(base, overlay):
    merged = deepcopy(base)
    for key, value in overlay.items():
        if key == "extends":
            continue
        if key == "assumptions" and isinstance(value, dict):
            merged.setdefault("assumptions", {}).update(deepcopy(value))
        else:
            merged[key] = deepcopy(value)
    return merged


def _compact_source(item, **extra):
    record = {
        "site_id": extra.get("site_id"),
        "name": item.get("name"),
        "coordinate": deepcopy(item.get("coordinate") or item.get("source_coordinate")),
        "source": deepcopy(item.get("source") or {}),
        "record_ids": deepcopy(item.get("record_ids") or []),
        "source_rows": deepcopy(item.get("source_rows") or []),
    }
    record.update({key: deepcopy(value) for key, value in extra.items() if key != "site_id"})
    return record


class EngineeringAssumptionScenarioService:
    """Build a reusable scenario pool and run one explicit B+C what-if."""

    def __init__(
        self,
        *,
        state_provider,
        standardized_facilities_provider,
        landing_sites_provider,
        tower_colocation_provider,
        device_catalog_provider=None,
        dem_sampler=None,
        dem_descriptor=None,
        ovobj_records_provider=None,
        config_dir=None,
        land_mask_source=None,
        surface_classification_policy=None,
        land_mask_context=None,
    ):
        self._state_provider = state_provider
        self._standardized_facilities_provider = standardized_facilities_provider
        self._landing_sites_provider = landing_sites_provider
        self._tower_colocation_provider = tower_colocation_provider
        self._device_catalog_provider = device_catalog_provider
        self._dem_sampler = dem_sampler
        self._dem_descriptor = deepcopy(dem_descriptor or {})
        self._ovobj_records_provider = ovobj_records_provider
        self._config_dir = Path(config_dir) if config_dir else (
            Path(__file__).resolve().parents[1] / "config" / "engineering_assumptions"
        )
        #: R5-H：唯一允许的陆海分类实现是 ``LandMaskSource.classify_detailed``。
        #: 注入 ``None`` 时保持 R5-F/R5-G 之前的 700 口径，供历史证据复算；
        #: 真实生成入口（research/r5/scripts/build_candidate_pool.py）必须注入。
        self._land_mask_source = land_mask_source
        self._surface_policy = (
            deepcopy(surface_classification_policy)
            if isinstance(surface_classification_policy, dict) else None
        )
        self._land_mask_context = (
            deepcopy(land_mask_context) if isinstance(land_mask_context, dict) else {}
        )

    # ------------------------------------------------------------------ config
    def load_scenario(self, scenario_id_or_path):
        """Load a versioned scenario, resolving ``extends`` relative to the dir."""

        candidate = Path(scenario_id_or_path)
        if candidate.is_file():
            overlay_path = candidate
        else:
            matches = []
            for path in sorted(self._config_dir.glob("*.json")):
                payload = _load_json(path)
                if str(payload.get("scenario_id")) == str(scenario_id_or_path):
                    matches.append(path)
            if not matches:
                raise EngineeringAssumptionError(f"未找到场景：{scenario_id_or_path}")
            overlay_path = matches[0]
        overlay = _load_json(overlay_path)
        base_name = overlay.get("extends")
        if base_name:
            base_path = (overlay_path.parent / str(base_name)).resolve()
            base = _load_json(base_path)
            merged = _merge_scenario_files(base, overlay)
        else:
            merged = overlay
        return normalize_engineering_assumption_scenario(merged)

    # ---------------------------------------------------------------- providers
    def _state(self):
        state = self._state_provider()
        if not isinstance(state, dict):
            raise EngineeringAssumptionError("state_provider 必须返回只读 state 对象")
        return state

    def _registry(self):
        value = self._standardized_facilities_provider()
        return value if isinstance(value, dict) else {}

    def _references(self):
        value = self._landing_sites_provider()
        return value if isinstance(value, dict) else {}

    def _tower_collection(self):
        value = self._tower_colocation_provider()
        return value if isinstance(value, dict) else {}

    def _catalog(self, state):
        if callable(self._device_catalog_provider):
            value = self._device_catalog_provider()
            if isinstance(value, dict):
                return value
        value = state.get("device_catalog") or {}
        return value if isinstance(value, dict) else {}

    def _ovobj(self):
        if not callable(self._ovobj_records_provider):
            return {"status": "not_configured", "records": [], "source_sha256": None}
        value = self._ovobj_records_provider()
        return value if isinstance(value, dict) else {"status": "unavailable", "records": []}

    # ------------------------------------------------------------------- pool
    def build_candidate_pool(self, scenario):
        """Build the full, route-independent engineering-assumption candidate pool."""

        scenario = normalize_engineering_assumption_scenario(scenario)
        state = self._state()
        assumptions = scenario["assumptions"]
        mount = float(assumptions["mount_height_agl_m"])
        allowed = list(assumptions["allowed_subsystems"])
        equipment = assumptions["equipment"]
        device_ids = [item for item in equipment.values() if item]

        excluded = []
        conflicts = []
        records = []
        source_counts = {
            "towers": 0,
            "reference_landing_sites": 0,
            "standardized_facilities": 0,
            "standardized_by_collection": {},
            "ovobj_labels": 0,
            "ovobj_unique_names": 0,
            "project_nodes": 0,
            "explicit_sites": 0,
        }

        # --- 1) 373 real towers: keep the existing confirmed/estimated origin.
        tower_items = [
            item for item in (self._tower_collection().get("items") or [])
            if isinstance(item, dict)
        ]
        source_counts["towers"] = len(tower_items)
        for item in tower_items:
            site_id = str(item.get("site_id") or "")
            coordinate = _coord(item.get("coordinate"))
            vertical = item.get("vertical_profile") if isinstance(item.get("vertical_profile"), dict) else {}
            origin = vertical.get("planning_service_origin_egm2008_m")
            origin_status = str(vertical.get("planning_origin_status") or "")
            if coordinate is None:
                excluded.append(self._excluded(
                    "tower_colocation", site_id, item.get("name"), None,
                    "coordinate_missing_or_invalid", "真实铁塔坐标不可定位",
                ))
                continue
            if (
                origin_status not in ("confirmed", "estimated")
                or not isinstance(origin, (int, float))
            ):
                excluded.append(self._excluded(
                    "tower_colocation", site_id, item.get("name"), coordinate,
                    "unusable_existing_origin", "既有铁塔原点不可用；不可以用 DEM+15 m 覆盖真实铁塔数据",
                ))
                continue
            components = vertical.get("planning_origin_components") or {}
            ground = components.get("terrain_elevation_egm2008_m")
            structure_height = components.get("source_tower_height_m")
            record = self._base_record(
                scenario_site_id=self._tower_scenario_id(item),
                name=item.get("name") or site_id,
                coordinate=coordinate,
                source_kind="tower_colocation",
                source_class="tower",
                source_category=item.get("site_type") or "communication_tower",
                source_site_ids=[site_id, str((item.get("metadata") or {}).get("host", {}).get("host_tower_id") or "")],
                source_records=[_compact_source(item, site_id=site_id, source_kind="tower_colocation")],
                inherited_confirmed_facts=True,
                horizontal_crs_status="confirmed_inherited",
                horizontal_crs_source="towers.crs (project human adjudication; EPSG:4490)",
                ground=ground,
                ground_status="inherited_source_terrain",
                ground_source="tower_colocation_profile / FABDEM EGM2008 terrain component",
                mount=structure_height,
                origin=float(origin),
                origin_status=origin_status,
                origin_method=vertical.get("planning_origin_method"),
                origin_authority=vertical.get("planning_origin_authority"),
                origin_components=deepcopy(components),
                allowed=allowed,
                device_ids=device_ids,
                reuse_class=TOWER_REUSE_CLASS,
                source_allowed_subsystems=deepcopy(item.get("available_subsystems") or []),
                uncertainty=[
                    "铁塔原点沿用既有 confirmed/estimated 口径；本轮不使用 DEM+15 m 覆盖",
                    "physical_mount_confirmed=false；真实挂高与安装条件仍需场勘",
                ],
            )
            record["vertical_profile_source"] = deepcopy(vertical)
            records.append(record)

        # --- 2) 98 reference landing sites + 3) 241 standardized facilities.
        landing = self._references()
        reference_items = [
            item for item in (landing.get("items") or []) if isinstance(item, dict)
        ]
        source_counts["reference_landing_sites"] = len(reference_items)
        registry = self._registry()
        collections = registry.get("collections") or {}
        facility_items = []
        for collection_key, collection in collections.items():
            if not isinstance(collection, dict):
                continue
            items = [item for item in (collection.get("items") or []) if isinstance(item, dict)]
            source_counts["standardized_facilities"] += len(items)
            source_counts["standardized_by_collection"][collection_key] = len(items)
            for item in items:
                item = dict(item)
                item["collection"] = collection_key
                facility_items.append(item)

        landing_family = []
        for item in reference_items:
            site_id = str(item.get("reference_site_id") or "")
            coordinate = _coord(item.get("coordinate"))
            if coordinate is None:
                excluded.append(self._excluded(
                    "reference_landing_site", site_id, item.get("name"), None,
                    "coordinate_missing_or_invalid", "98 条参考起降点坐标不可定位",
                ))
                continue
            dem, dem_error = self._sample_dem(coordinate)
            if dem_error:
                excluded.append(self._excluded(
                    "reference_landing_site", site_id, item.get("name"), coordinate,
                    dem_error, "FABDEM 无法提供该点地面正高",
                ))
                continue
            landing_family.append(self._base_record(
                scenario_site_id="EA-RLS-" + site_id.replace("RLS-", ""),
                name=item.get("name") or site_id,
                coordinate=coordinate,
                source_kind="reference_landing_site",
                source_class="landing",
                source_category=item.get("site_type") or "landing_site",
                source_site_ids=[site_id],
                source_records=[_compact_source(item, site_id=site_id, source_kind="reference_landing_site")],
                inherited_confirmed_facts=(
                    str(item.get("crs_status") or "").lower() == "confirmed"
                    or (item.get("crs") or {}).get("source_crs", {}).get("confirmed") is True
                ),
                horizontal_crs_status=(
                    "confirmed_inherited"
                    if (
                        str(item.get("crs_status") or "").lower() == "confirmed"
                        or (item.get("crs") or {}).get("source_crs", {}).get("confirmed") is True
                    )
                    else "engineering_assumed"
                ),
                horizontal_crs_source="reference_landing_sites.crs (project human adjudication; EPSG:4490)",
                ground=dem,
                ground_status="sampled",
                ground_source=self._dem_descriptor.get("source", "FABDEM_DTM_30m"),
                mount=mount,
                origin=round(float(dem) + mount, 6),
                origin_status="assumption_only",
                origin_method="fabdem_nearest_plus_15m_engineering_assumption",
                origin_authority="engineering_assumption",
                allowed=allowed,
                device_ids=device_ids,
                reuse_class=CANDIDATE_REUSE_CLASS,
                source_allowed_subsystems=[],
                uncertainty=[
                    "无真实挂高/安装原点；DEM + 15 m 只是统一仿真假设",
                    "不证明屋顶、塔顶或实际挂高；不证明产权、许可、供电或回传",
                ],
                extra={"reference_site_id": site_id},
            ))

        for item in facility_items:
            collection_key = str(item.get("collection") or "")
            site_id = str(item.get("facility_id") or "")
            coordinate = _coord(item.get("coordinate"))
            source_counts["standardized_facilities"] = source_counts.get("standardized_facilities", 0)
            if coordinate is None:
                reason = (
                    "coordinate_invalid"
                    if item.get("coordinate_status") in ("invalid", "dms_out_of_range")
                    else "coordinate_missing"
                )
                excluded.append(self._excluded(
                    "standardized_facility", site_id, item.get("name"), None,
                    reason, "标准化设施缺/非法坐标；不因工程假设而变成可用",
                ))
                continue
            dem, dem_error = self._sample_dem(coordinate)
            if dem_error:
                excluded.append(self._excluded(
                    "standardized_facility", site_id, item.get("name"), coordinate,
                    dem_error, "FABDEM 无法提供该点地面正高",
                ))
                continue
            record = self._base_record(
                scenario_site_id="EA-FAC-" + site_id.replace("ZS-", ""),
                name=item.get("name") or site_id,
                coordinate=coordinate,
                source_kind="standardized_facility",
                source_class=collection_key,
                source_category=item.get("facility_category") or collection_key,
                source_site_ids=[site_id],
                source_records=[_compact_source(item, site_id=site_id, source_kind="standardized_facility")],
                inherited_confirmed_facts=False,
                horizontal_crs_status="engineering_assumed",
                horizontal_crs_source="R4-B-CRS engineering_assumed (source_undeclared)",
                ground=dem,
                ground_status="sampled",
                ground_source=self._dem_descriptor.get("source", "FABDEM_DTM_30m"),
                mount=mount,
                origin=round(float(dem) + mount, 6),
                origin_status="assumption_only",
                origin_method="fabdem_nearest_plus_15m_engineering_assumption",
                origin_authority="engineering_assumption",
                allowed=allowed,
                device_ids=device_ids,
                reuse_class=CANDIDATE_REUSE_CLASS,
                source_allowed_subsystems=[],
                uncertainty=[
                    "源 .et 未声明 CRS；EPSG:4490 是工程假设",
                    "无真实挂高；DEM + 15 m 只是统一仿真假设",
                    "不证明产权、许可、供电或回传",
                ],
                extra={
                    "collection": collection_key,
                    "facility_id": site_id,
                    "registry_match_id": item.get("registry_match_id"),
                },
            )
            if collection_key == "landing":
                landing_family.append(record)
            else:
                records.append(record)

        # Merge only exact co-located landing-family records.  Same reference ID
        # at a different coordinate is a conflict, never a merge.
        merged_landing, landing_conflicts = self._merge_exact_landing(landing_family)
        records.extend(merged_landing)
        conflicts.extend(landing_conflicts)

        # --- 4) project nodes: aliases of their reference record when co-located.
        node_items = [item for item in (state.get("nodes") or []) if isinstance(item, dict)]
        source_counts["project_nodes"] = len(node_items)
        reference_by_id = {
            str(item.get("reference_site_id")): item
            for item in reference_items if item.get("reference_site_id")
        }
        landing_by_ref = {}
        for record in merged_landing:
            for source_id in record.get("source_site_ids") or []:
                landing_by_ref.setdefault(str(source_id), record)
        for node in node_items:
            node_id = str(node.get("node_id") or "")
            reference_id = str(node.get("reference_site_id") or "")
            coordinate = _coord(node.get("coordinate"))
            target = landing_by_ref.get(reference_id)
            if target is not None and coordinate is not None:
                if _distance_m(target["coordinate"], coordinate) <= 1.0:
                    target["source_site_ids"].append(node_id)
                    target["source_records"].append(_compact_source(node, site_id=node_id, source_kind="project_node"))
                    target.setdefault("project_node_ids", []).append(node_id)
                    continue
            if coordinate is None:
                excluded.append(self._excluded(
                    "project_node", node_id, node.get("name"), None,
                    "coordinate_missing_or_invalid", "项目节点坐标不可定位",
                ))
                continue
            dem, dem_error = self._sample_dem(coordinate)
            if dem_error:
                excluded.append(self._excluded(
                    "project_node", node_id, node.get("name"), coordinate,
                    dem_error, "FABDEM 无法提供该点地面正高",
                ))
                continue
            records.append(self._base_record(
                scenario_site_id="EA-NODE-" + node_id,
                name=node.get("name") or node_id,
                coordinate=coordinate,
                source_kind="project_node",
                source_class="project_node",
                source_category="landing_site",
                source_site_ids=[node_id],
                source_records=[_compact_source(node, site_id=node_id, source_kind="project_node")],
                inherited_confirmed_facts=False,
                horizontal_crs_status="engineering_assumed",
                horizontal_crs_source="project node coordinate contract; scenario uses EPSG:4490",
                ground=dem,
                ground_status="sampled",
                ground_source=self._dem_descriptor.get("source", "FABDEM_DTM_30m"),
                mount=mount,
                origin=round(float(dem) + mount, 6),
                origin_status="assumption_only",
                origin_method="fabdem_nearest_plus_15m_engineering_assumption",
                origin_authority="engineering_assumption",
                allowed=allowed,
                device_ids=device_ids,
                reuse_class=CANDIDATE_REUSE_CLASS,
                source_allowed_subsystems=[],
                uncertainty=["项目节点不是已确认安装站址", "DEM + 15 m 只是统一仿真假设"],
            ))

        # --- 5) .ovobj supplementary ground/roof labels.
        ovobj = self._ovobj()
        ovobj_records = [item for item in (ovobj.get("records") or []) if isinstance(item, dict)]
        source_counts["ovobj_labels"] = int(ovobj.get("record_count") or len(ovobj_records))
        source_counts["ovobj_unique_names"] = int(
            ovobj.get("unique_name_count") or len({item.get("name") for item in ovobj_records})
        )
        tower_names = {
            normalize_ovobj_name(item.get("name"))
            for item in (self._tower_collection().get("items") or [])
            if isinstance(item, dict)
        }
        tower_coords = [
            _coord(item.get("coordinate"))
            for item in (self._tower_collection().get("items") or [])
            if isinstance(item, dict)
        ]
        tower_coords = [item for item in tower_coords if item is not None]

        ovobj_groups = {}
        for item in ovobj_records:
            name = str(item.get("name") or "")
            normalized = normalize_ovobj_name(name)
            site_type = str(item.get("site_type") or (item.get("fields") or {}).get("站址类型") or "")
            coordinate = _coord([item.get("longitude"), item.get("latitude")])
            if coordinate is None:
                excluded.append(self._excluded(
                    "ovobj_label", str(item.get("label_index")), name, None,
                    "coordinate_missing_or_invalid", "ovobj 标签坐标不可定位",
                ))
                continue
            if normalized in tower_names or any(_distance_m(coordinate, tower) <= 5.0 for tower in tower_coords):
                excluded.append(self._excluded(
                    "ovobj_label", str(item.get("label_index")), name, coordinate,
                    "co_located_with_real_tower", "与 373 座真实铁塔同名或 5 m 内同址，按重复排除",
                ))
                continue
            if site_type in EXCLUDED_OVOBJ_TYPES:
                excluded.append(self._excluded(
                    "ovobj_label", str(item.get("label_index")), name, coordinate,
                    "site_type_not_outdoor_ground_or_roof",
                    f"站址类型={site_type}；本轮只接入地面站/楼面站补充站址",
                ))
                continue
            if site_type not in GROUND_TYPE_VALUES:
                excluded.append(self._excluded(
                    "ovobj_label", str(item.get("label_index")), name, coordinate,
                    "site_type_unresolved", f"站址类型无法确认：{site_type or 'empty'}",
                ))
                continue
            key = (normalized, site_type, _round_coord(coordinate, 6))
            ovobj_groups.setdefault(key, []).append(item)

        canonical_so_far = records
        for (normalized, site_type, _), group in sorted(ovobj_groups.items(), key=lambda pair: pair[0]):
            representative = group[0]
            coordinate = _coord([representative.get("longitude"), representative.get("latitude")])
            if any(_distance_m(coordinate, item["coordinate"]) <= 5.0 for item in canonical_so_far):
                excluded.append(self._excluded(
                    "ovobj_label", str(representative.get("label_index")), representative.get("name"), coordinate,
                    "co_located_duplicate", "与已接入候选站址 5 m 内同址，按重复排除",
                ))
                continue
            dem, dem_error = self._sample_dem(coordinate)
            if dem_error:
                excluded.append(self._excluded(
                    "ovobj_label", str(representative.get("label_index")), representative.get("name"), coordinate,
                    dem_error, "FABDEM 无法提供该点地面正高",
                ))
                continue
            records.append(self._base_record(
                scenario_site_id="EA-OV-" + sha256(
                    f"{normalized}|{site_type}|{coordinate[0]:.6f}|{coordinate[1]:.6f}".encode("utf-8")
                ).hexdigest()[:12].upper(),
                name=representative.get("name") or normalized,
                coordinate=coordinate,
                source_kind="ovobj_supplement",
                source_class="ovobj_ground" if site_type == GROUND_TYPE else "ovobj_roof",
                source_category=site_type,
                source_site_ids=[str(item.get("label_index")) for item in group],
                source_records=[
                    _compact_source(item, site_id=str(item.get("label_index")), source_kind="ovobj_label")
                    for item in group
                ],
                inherited_confirmed_facts=False,
                horizontal_crs_status="engineering_assumed",
                horizontal_crs_source="ovobj has no CRS declaration; EPSG:4490 engineering assumption",
                ground=dem,
                ground_status="sampled",
                ground_source=self._dem_descriptor.get("source", "FABDEM_DTM_30m"),
                mount=mount,
                origin=round(float(dem) + mount, 6),
                origin_status="assumption_only",
                origin_method="fabdem_nearest_plus_15m_engineering_assumption",
                origin_authority="engineering_assumption",
                allowed=allowed,
                device_ids=device_ids,
                reuse_class=CANDIDATE_REUSE_CLASS,
                source_allowed_subsystems=[],
                uncertainty=[
                    "ovobj 无 CRS、无塔高/挂高/设备字段",
                    f"站址类型={site_type}；'DEM + 15 m' 不代表真实屋顶或实际挂高",
                    "楼面站的建筑高度不确定性未消除",
                ],
                extra={
                    "ovobj_site_type": site_type,
                    "ovobj_site_nature": representative.get("site_nature"),
                    "ovobj_label_count": len(group),
                    "ovobj_source_sha256": ovobj.get("source_sha256"),
                },
            ))

        # --- 6) user-explicit scenario sites (B and C are registered by id).
        explicit_ids = []
        for item in scenario.get("explicit_sites") or []:
            explicit_ids.append(item["site_id"])
            coordinate = _coord(item.get("coordinate"))
            sampled, dem_error = self._sample_dem(coordinate)
            dem = float(item["dem_egm2008_m"])
            records.append(self._base_record(
                scenario_site_id=item["site_id"],
                name=item.get("name") or item["site_id"],
                coordinate=coordinate,
                source_kind="explicit_assumption_site",
                source_class="new_build",
                source_category="engineering_assumption_new_build",
                source_site_ids=[item["site_id"]],
                source_records=[{
                    "site_id": item["site_id"],
                    "source": deepcopy(item.get("source") or {}),
                    "registered_dem_egm2008_m": dem,
                    "sampled_dem_egm2008_m": sampled,
                    "dem_delta_m": None if sampled is None else round(sampled - dem, 6),
                }],
                inherited_confirmed_facts=False,
                horizontal_crs_status="engineering_assumed",
                horizontal_crs_source="user-explicit engineering assumption; EPSG:4490",
                ground=dem,
                ground_status="registered_explicit_evidence",
                ground_source=(item.get("source") or {}).get("evidence") or self._dem_descriptor.get("source"),
                mount=float(item["mount_height_agl_m"]),
                origin=float(item["service_origin_egm2008_m"]),
                origin_status="assumption_only_explicit_registration",
                origin_method="registered_dem_plus_registered_mount",
                origin_authority="engineering_assumption",
                origin_components={
                    "terrain_elevation_egm2008_m": dem,
                    "source_tower_height_m": None,
                    "mount_offset_m": float(item["mount_height_agl_m"]),
                    "sampled_fabdem_m": sampled,
                    "sampled_fabdem_delta_m": None if sampled is None else round(sampled - dem, 6),
                },
                allowed=list(item["allowed_subsystems"]),
                device_ids=list(item["device_ids"]),
                reuse_class=str(item.get("reuse_class") or NEW_BUILD_REUSE_CLASS),
                source_allowed_subsystems=list(item["allowed_subsystems"]),
                uncertainty=[
                    "用户显式登记的工程假设站址；不是真实产权、许可或安装",
                    "13/15 m 挂高与 DEM 只是仿真输入；未做场勘",
                ],
                extra={
                    "route_scope": deepcopy(item.get("route_scope")),
                    "explicit_registration": True,
                },
            ))
            source_counts["explicit_sites"] += 1

        # Collapse same-name/different-coordinate conflicts for disclosure only.
        conflicts.extend(self._name_conflicts(records))
        conflicts.extend(self._reference_link_conflicts(records))

        # Attach the real scenario identity after all records (including merged
        # landing records) exist.  ``_base_record`` cannot know the scenario at
        # construction time because merging happens later.
        for record in records:
            record["assumption"] = scenario_site_marker(
                scenario,
                source_kind=record["source_kind"],
                source_site_id=record["scenario_site_id"],
                inherited_confirmed=record["horizontal_crs"].get("inherited_confirmed_facts"),
            )
            record["assumption_contract"] = assumption_contract(scenario)

        # --- project every record through the existing candidate-action contract.
        #: R5-H：先在**完整 700 身份**上生成审计前动作（``pre_actions``），再按
        #: canonical land_mask 把 sea 身份移出陆基假设。主 pool 的动作由同一批
        #: ``pre_actions`` 过滤得到，因此 648/1,296/1,294 是生成结果，而不是事后
        #: 改写的统计；原始 700/1,400 保留在 ``pre_surface`` 审计块。
        projected, projections = self._project_sites(records, scenario)
        catalog = self._catalog(state)
        pre_actions = candidate_actions(
            list(SCENARIO_TARGETS),
            {},
            {"items": [item for item in projected if item.get("planning_profile", {}).get("reuse_class") != TOWER_REUSE_CLASS]},
            catalog,
            {"items": [item for item in projected if item.get("planning_profile", {}).get("reuse_class") == TOWER_REUSE_CLASS]},
        )
        by_site = {record["scenario_site_id"]: record for record in records}
        for action in pre_actions:
            record = by_site.get(str(action.get("site_id")))
            if record is None:
                continue
            marker = record["assumption"]
            action.update({
                "scenario": deepcopy(marker),
                "scenario_site_id": record["scenario_site_id"],
                "source_kind": record["source_kind"],
                "source_site_ids": deepcopy(record["source_site_ids"]),
                "physical_site_key": deepcopy(record["physical_site_key"]),
                ASSUMPTION_ONLY_KEY: True,
                REAL_WORLD_CONFIRMED_KEY: False,
                "scenario_eligible": action.get("eligibility", {}).get("status") == "eligible",
            })
            # Canonical ``confirmed`` means "eligible in the formal P16 sense".
            # The action is scenario-qualified, not reality-confirmed.
            action["confirmed"] = False

        actions_by_site = {}
        for action in pre_actions:
            actions_by_site.setdefault(action["scenario_site_id"], []).append(action)
        for record in records:
            site_actions = actions_by_site.get(record["scenario_site_id"], [])
            record["actions"] = deepcopy(site_actions)
            record["action_counts"] = {
                "generated": len(site_actions),
                "eligible": sum(1 for item in site_actions if item.get("scenario_eligible")),
                "by_subsystem": {
                    code: {
                        "generated": sum(1 for item in site_actions if item.get("subsystem") == code),
                        "eligible": sum(
                            1 for item in site_actions
                            if item.get("subsystem") == code and item.get("scenario_eligible")
                        ),
                    }
                    for code in ("C", "N", "S")
                },
            }
            record["formal_eligible"] = False
            record["p16_projection"] = projections[record["scenario_site_id"]]

        records.sort(key=lambda item: item["scenario_site_id"])
        pre_actions.sort(key=lambda item: item["action_id"])
        pre_records = records
        pre_excluded = deepcopy(excluded)
        screening = self._apply_surface_screening(records, excluded)
        retained_ids = set(screening.get("retained_ids") or ())
        if screening["status"] == "blocked":
            #: unknown 不得当作通过：不生成任何陆基动作，也不把身份升级为候选。
            records = []
            actions = []
        elif screening["status"] == "applied":
            records = [
                record for record in records
                if record["scenario_site_id"] in retained_ids
            ]
            actions = [
                action for action in pre_actions
                if str(action.get("scenario_site_id")) in retained_ids
            ]
        else:
            records = list(records)
            actions = list(pre_actions)
        actions.sort(key=lambda item: item["action_id"])
        pre_counts = self._pool_counts(pre_records, pre_excluded, source_counts, pre_actions)
        counts = self._pool_counts(records, excluded, source_counts, actions)
        counts["surface_screening"] = self._surface_screening_counts(
            screening, pre_counts, counts,
        )
        catalog = self._catalog(state)
        device_models = {}
        for item in catalog.get("items") or []:
            device_id = str(item.get("device_id") or "")
            if device_id not in device_ids:
                continue
            device_models[device_id] = {
                "subsystem": item.get("subsystem"),
                "coverage_geometry": deepcopy(item.get("coverage_geometry")),
                "service_model": deepcopy(item.get("service_model")),
            }
        for device_id, bucket in (counts.get("by_device") or {}).items():
            model = device_models.get(device_id) or {}
            bucket["radius_by_surface"] = deepcopy(
                (model.get("coverage_geometry") or {}).get("radius_by_surface")
            )
            bucket["model_family"] = (model.get("service_model") or {}).get("model_family")
        valid_landing_facilities = int(
            (((registry.get("counts") or {}).get("by_collection_coordinate") or {})
             .get("landing") or {}).get("valid_coordinate") or 0
        )
        landing_valid_source_records = (
            source_counts.get("reference_landing_sites", 0) + valid_landing_facilities
        )
        canonical_landing_records = sum(
            1 for item in pre_records
            if item["source_kind"] == "reference_landing_site"
            or (item["source_kind"] == "standardized_facility" and item.get("source_class") == "landing")
        )
        #: 这些是**来源记录层面**的纳入统计，必须按审计前 700 身份计算；
        #: 陆海筛选只决定哪些身份进入陆基假设，不改变来源纳入/去重事实。
        counts["source_record_inclusion"] = {
            "reference_landing_sites": {
                "source_records": source_counts.get("reference_landing_sites", 0),
                "included_identities": len({
                    str(sid)
                    for item in pre_records for sid in item["source_site_ids"]
                    if str(sid).startswith("RLS-")
                }),
            },
            "standardized_facilities": {
                "source_records": source_counts.get("standardized_facilities", 0),
                "valid_coordinate": int((registry.get("counts") or {}).get("valid_coordinate") or 0),
                "included_identities": len({
                    str(sid)
                    for item in pre_records for sid in item["source_site_ids"]
                    if str(sid).startswith("ZS-")
                }),
                "invalid_or_missing_coordinate_excluded": int(
                    (registry.get("counts") or {}).get("missing_or_invalid_coordinate") or 0
                ),
            },
            "landing_family": {
                "source_records_total": int(
                    source_counts.get("reference_landing_sites", 0)
                    + (source_counts.get("standardized_by_collection", {}) or {}).get("landing", 0)
                ),
                "valid_source_records": landing_valid_source_records,
                "canonical_sites": canonical_landing_records,
                "exact_coordinate_merge_or_dedupe": (
                    landing_valid_source_records - canonical_landing_records
                ),
                "standardized_identities_merged_into_reference": (
                    valid_landing_facilities
                    - sum(
                        1 for item in pre_records
                        if item["source_kind"] == "standardized_facility"
                        and item.get("source_class") == "landing"
                    )
                ),
                "reference_ids_same_coordinate_merged": (
                    source_counts.get("reference_landing_sites", 0)
                    - sum(
                        1 for item in pre_records
                        if item["source_kind"] == "reference_landing_site"
                    )
                ),
                "invalid_landing_facilities_excluded": int(
                    ((source_counts.get("standardized_by_collection", {}) or {}).get("landing", 0)
                     - valid_landing_facilities)
                ),
            },
            "project_nodes": {
                "source_records": source_counts.get("project_nodes", 0),
                "separate_sites": sum(
                    1 for item in pre_records if item["source_kind"] == "project_node"
                ),
                "aliased_to_existing_sites": source_counts.get("project_nodes", 0) - sum(
                    1 for item in pre_records if item["source_kind"] == "project_node"
                ),
            },
            "ovobj": {
                "total_labels": source_counts.get("ovobj_labels", 0),
                "unique_names": source_counts.get("ovobj_unique_names", 0),
                "tower_matched_or_5m_labels": sum(
                    1 for item in pre_excluded
                    if item["source_kind"] == "ovobj_label"
                    and item["reason"] == "co_located_with_real_tower"
                ),
                "extra_labels_beyond_real_towers": (
                    source_counts.get("ovobj_labels", 0) - sum(
                        1 for item in pre_excluded
                        if item["source_kind"] == "ovobj_label"
                        and item["reason"] == "co_located_with_real_tower"
                    )
                ),
                "non_outdoor_or_virtual_labels": sum(
                    1 for item in pre_excluded
                    if item["source_kind"] == "ovobj_label"
                    and item["reason"] == "site_type_not_outdoor_ground_or_roof"
                ),
                "canonical_ground_or_roof_sites": sum(
                    1 for item in pre_records if item["source_kind"] == "ovobj_supplement"
                ),
                "duplicate_or_unresolved_excluded": sum(
                    1 for item in pre_excluded
                    if item["source_kind"] == "ovobj_label"
                    and item["reason"] in (
                        "co_located_duplicate", "site_type_unresolved",
                        "coordinate_missing_or_invalid",
                    )
                ),
            },
            "explicit_sites": {
                "source_records": source_counts.get("explicit_sites", 0),
                "included_identities": sum(
                    1 for item in pre_records if item["source_kind"] == "explicit_assumption_site"
                ),
            },
            "semantics": (
                "source_record_inclusion_is_pre_surface_screening_700_identity_scope; "
                "land_sea_screening_is_reported_separately_in_surface_screening"
            ),
        }
        counts["device_models"] = device_models
        counts["conflict_summary"] = _conflict_summary(conflicts)
        included_ovobj_labels = (
            source_counts.get("ovobj_labels", 0)
            - sum(1 for item in pre_excluded if item["source_kind"] == "ovobj_label")
        )
        included_identities = (
            source_counts.get("towers", 0)
            + source_counts.get("reference_landing_sites", 0)
            + int((registry.get("counts") or {}).get("valid_coordinate") or 0)
            + included_ovobj_labels
            + source_counts.get("project_nodes", 0)
            + source_counts.get("explicit_sites", 0)
        )
        counts["reviewed_source_records"] = {
            "total": (
                source_counts.get("towers", 0)
                + source_counts.get("reference_landing_sites", 0)
                + source_counts.get("standardized_facilities", 0)
                + source_counts.get("ovobj_labels", 0)
                + source_counts.get("project_nodes", 0)
                + source_counts.get("explicit_sites", 0)
            ),
            "included_identities": included_identities,
            "included_ovobj_labels": included_ovobj_labels,
            "excluded_records": len(pre_excluded),
            "canonical_sites": len(pre_records),
            "dedupe_or_alias_collapses": included_identities - len(pre_records),
            "semantics": (
                "source-identity scope (pre-surface-screening); after land/sea screening "
                "the retained identity count is in counts.canonical_site_count"
            ),
        }
        return {
            "schema_version": "r5h-engineering-assumption-candidate-pool-v1",
            "status": (
                "blocked" if screening["status"] == "blocked" else "passed"
            ),
            "semantics": "simulation_engineering_assumption_candidate_pool_not_formal_state",
            "authority": "none",
            "scenario": {
                "scenario_id": scenario["scenario_id"],
                "version": scenario["version"],
                "mode": ENGINEERING_ASSUMPTION_MODE,
                "route_id": scenario.get("route_id"),
            },
            "assumption_contract": assumption_contract(scenario),
            "write_boundary": {
                "writes_project_state": False,
                "writes_candidate_sites": False,
                "writes_towers_or_facilities": False,
                "adopt_or_apply": False,
                "formal_p16_consumes_this_pool": False,
            },
            "sites": records,
            "actions": actions,
            "pre_surface": {
                "semantics": (
                    "pre-land/sea-screening audit scope: all 700 scenario identities and "
                    "their generated actions, retained for traceability; it is not a "
                    "land-based candidate pool"
                ),
                "sites": screening.get("pre_surface_sites") or [],
                "actions": pre_actions,
                "counts": {
                    "canonical_site_count": pre_counts["canonical_site_count"],
                    "unique_coordinate_count": pre_counts["unique_coordinate_count"],
                    "generated_action_count": pre_counts["generated_action_count"],
                    "eligible_scenario_action_count": pre_counts[
                        "eligible_scenario_action_count"
                    ],
                    "same_coordinate_group_count": pre_counts[
                        "same_coordinate_group_count"
                    ],
                    "same_coordinate_surplus_site_id_count": pre_counts[
                        "same_coordinate_surplus_site_id_count"
                    ],
                },
            },
            "excluded": excluded,
            "conflicts": conflicts,
            "counts": counts,
            "sources": {
                "standardized_registry": {
                    "registry_id": registry.get("registry_id"),
                    "version": registry.get("version"),
                    "crs_adjudication": (
                        ((registry.get("crs") or {}).get("adjudication") or {}).get("record_id")
                        or (registry.get("crs_adjudication") or {}).get("record_id")
                    ),
                },
                "dem": deepcopy(self._dem_descriptor),
                "land_mask": deepcopy(screening.get("land_mask")),
                "ovobj": {
                    "status": ovobj.get("status"),
                    "source_sha256": ovobj.get("source_sha256"),
                    "record_count": source_counts["ovobj_labels"],
                    "unique_name_count": source_counts["ovobj_unique_names"],
                },
            },
        }

    # --------------------------------------------------------------- L1 geometry
    def l1_geometry(self, pool, scenario, voxels, *, route_id=None):
        """Lightweight 219-voxel geometry for 1628 + B + C (no P14/P15)."""

        records = {item["scenario_site_id"]: item for item in pool.get("sites") or []}
        explicit_ids = [item["site_id"] for item in scenario.get("explicit_sites") or []]
        tower_1628 = None
        for record in pool.get("sites") or []:
            if "330903500010001628" in {str(item) for item in record.get("source_site_ids") or []}:
                tower_1628 = record
                break
        providers = []
        if tower_1628 is not None:
            providers.append({
                "provider_id": "tower:330903500010001628",
                "label": "既有 1628 铁塔",
                "coordinate": tower_1628["coordinate"],
                "origin": tower_1628["installation_origin"]["service_origin_egm2008_m"],
                "site_id": "tower:330903500010001628",
            })
        for site_id in explicit_ids:
            record = records.get(site_id)
            if record is None:
                raise EngineeringAssumptionError(f"L1 评价缺少显式站址：{site_id}")
            providers.append({
                "provider_id": site_id,
                "label": record["name"],
                "coordinate": record["coordinate"],
                "origin": record["installation_origin"]["service_origin_egm2008_m"],
                "site_id": site_id,
            })
        if len(providers) < 3:
            raise EngineeringAssumptionError("L1 评价需要 1628 + B + C 三个站址")

        probe_by_alt = {"ALT-080": 80.0, "ALT-100": 107.5, "ALT-150": 127.5}
        per_provider = {
            provider["provider_id"]: {
                "provider_id": provider["provider_id"],
                "label": provider["label"],
                "coordinate": provider["coordinate"],
                "origin": provider["origin"],
                "covered": 0,
                "by_alt": {},
                "max_slant_m": 0.0,
                "min_horizontal_m": None,
                "max_horizontal_m": None,
            }
            for provider in providers
        }
        rows = []
        for voxel in voxels:
            coordinate = _coord([voxel.get("lon"), voxel.get("lat")])
            alt = str(voxel.get("alt") or "")
            probe = probe_by_alt.get(alt)
            if coordinate is None or probe is None:
                continue
            covered_by = []
            slant_by = {}
            for provider in providers:
                horizontal = _distance_m(provider["coordinate"], coordinate)
                delta = float(probe) - float(provider["origin"])
                vertical_ok = delta >= 0.0
                slant = hypot(horizontal, delta)
                covered = bool(vertical_ok and slant <= 2000.0)
                slant_by[provider["provider_id"]] = round(slant, 3)
                if covered:
                    covered_by.append(provider["site_id"])
                    summary = per_provider[provider["provider_id"]]
                    summary["covered"] += 1
                    summary["by_alt"][alt] = summary["by_alt"].get(alt, 0) + 1
                    summary["max_slant_m"] = max(summary["max_slant_m"], slant)
                    summary["min_horizontal_m"] = (
                        horizontal if summary["min_horizontal_m"] is None
                        else min(summary["min_horizontal_m"], horizontal)
                    )
                    summary["max_horizontal_m"] = (
                        horizontal if summary["max_horizontal_m"] is None
                        else max(summary["max_horizontal_m"], horizontal)
                    )
            rows.append({
                "voxel_id": voxel.get("voxel_id"),
                "section": voxel.get("section"),
                "alt": alt,
                "lon": coordinate[0],
                "lat": coordinate[1],
                "probe_egm2008_m": probe,
                "covered_by": covered_by,
                "distinct_site_count": len(set(covered_by)),
                "slant_m": slant_by,
            })
        double = sum(1 for row in rows if row["distinct_site_count"] >= 2)
        single = sum(1 for row in rows if row["distinct_site_count"] == 1)
        zero = sum(1 for row in rows if row["distinct_site_count"] == 0)
        by_alt = {}
        for alt in ("ALT-080", "ALT-100", "ALT-150"):
            subset = [row for row in rows if row["alt"] == alt]
            by_alt[alt] = {
                "voxels": len(subset),
                "double": sum(1 for row in subset if row["distinct_site_count"] >= 2),
                "single": sum(1 for row in subset if row["distinct_site_count"] == 1),
                "zero": sum(1 for row in subset if row["distinct_site_count"] == 0),
                "providers": {
                    provider["provider_id"]: sum(
                        1 for row in subset if provider["site_id"] in row["covered_by"]
                    )
                    for provider in providers
                },
            }
        probe_gate = {}
        for site_id in explicit_ids:
            record = records[site_id]
            origin = float(record["installation_origin"]["service_origin_egm2008_m"])
            probe_gate[site_id] = {
                "origin_egm2008_m": origin,
                "ALT-080": origin <= 80.0,
                "ALT-100": origin <= 107.5,
                "ALT-150": origin <= 127.5,
            }
        pairwise = []
        for index, left in enumerate(providers):
            for right in providers[index + 1:]:
                pairwise.append({
                    "a": left["provider_id"],
                    "b": right["provider_id"],
                    "distance_m": round(_distance_m(left["coordinate"], right["coordinate"]), 3),
                })
        expected = {
            "voxels": len(rows),
            "double": len(rows),
            "single": 0,
            "zero": 0,
        }
        matches_expected = (
            len(rows) == 219 and double == 219 and single == 0 and zero == 0
        )
        return {
            "status": "passed",
            "semantics": "l1_discrete_geometric_only_not_continuous_service_not_confirmed",
            "route_id": route_id or scenario.get("route_id"),
            "voxel_count": len(rows),
            "providers": providers,
            "provider_counts": list(per_provider.values()),
            "probe_gate": probe_gate,
            "pairwise_separation_m": pairwise,
            "double_site": double,
            "single_site": single,
            "zero_site": zero,
            "by_alt": by_alt,
            "expected_from_r2": expected,
            "matches_expected": matches_expected,
            "distinct_site_count_semantics": (
                "两个不同 site_id 只证明模型中的 distinct_site_count=2；"
                "B/C 相距约 96.7 m、同处东白莲罐区，不代表真实独立 provider、"
                "供电/回传冗余或共因失效已被排除。"
            ),
            "rows": rows,
        }

    # ------------------------------------------------------------- B+C evaluation
    def evaluate_explicit_actions(
        self, pool, scenario, state, selected_actions, *, baseline_summary=None,
        l1_geometry=None,
    ):
        """Run the existing P14→P15 chain once for the explicit scenario sites.

        The baseline P16 selection is applied first (the 13 tower actions from the
        frozen rev523 artifact), then the scenario's explicit B/C actions.  This
        keeps the comparison on the same facilities projection as the frozen
        baseline while adding only the requested what-if.
        """

        explicit_ids = [item["site_id"] for item in scenario.get("explicit_sites") or []]
        if not explicit_ids:
            raise EngineeringAssumptionError("评价需要 scenario.explicit_sites")
        actions_by_site = {}
        for action in pool.get("actions") or []:
            actions_by_site.setdefault(action.get("scenario_site_id"), []).append(action)
        explicit_actions = []
        for site_id in explicit_ids:
            candidates = [
                action for action in actions_by_site.get(site_id, [])
                if action.get("subsystem") == "S" and action.get("scenario_eligible")
            ]
            if len(candidates) != 1:
                raise EngineeringAssumptionError(f"{site_id} 缺少唯一的 RID 场景动作")
            explicit_actions.append(candidates[0])

        facilities = deepcopy(state.get("existing_cns_facilities") or {})
        for action in selected_actions or []:
            facilities = _apply_cumulative_action(facilities, action)
        baseline_facility_count = len(facilities.get("items") or [])
        for action in explicit_actions:
            facilities = _apply_cumulative_action(facilities, action)

        working = dict(state)
        working["existing_cns_facilities"] = facilities
        corridor, gap = rerun_corridor_chain(
            working, CNSServiceCorridorV1(), CNSCorridorGapAnalyzerV1(), facilities,
        )
        threshold = self._continuous_threshold(state)
        acceptance = _continuous_service_acceptance(
            gap, threshold["threshold_m"], threshold["route_speed_mps"],
            threshold["aircraft_id"],
        )
        baseline_acceptance = deepcopy(
            (baseline_summary or {}).get("final_continuous_service_acceptance") or {}
        )
        baseline_deficit = baseline_acceptance.get("longest_deficit_m")
        scenario_deficit = acceptance.get("longest_deficit_m")
        delta = (
            None if baseline_deficit is None or scenario_deficit is None
            else float(baseline_deficit) - float(scenario_deficit)
        )
        residual_segments = _continuous_residual_segments(gap, threshold["threshold_m"])
        s_summary = self._subsystem_summary(gap, "S")
        c_summary = self._subsystem_summary(gap, "C")
        achieved = (
            None if scenario_deficit is None
            else bool(float(scenario_deficit) <= float(threshold["threshold_m"]) + 1e-9)
        )
        return {
            "status": "passed",
            "semantics": (
                "single_explicit_bc_scenario_service_evaluation_on_frozen_rev523_copy_"
                "not_formal_p16_not_p17_full_run"
            ),
            "scenario_id": scenario["scenario_id"],
            "route_id": scenario.get("route_id"),
            "assumption_contract": assumption_contract(scenario),
            "baseline_projection": {
                "source_selected_action_count": len(selected_actions or []),
                "facilities_after_selected_actions": baseline_facility_count,
                "continuous_service_acceptance": baseline_acceptance,
            },
            "explicit_actions": [
                {
                    "action_id": action.get("action_id"),
                    "scenario_site_id": action.get("scenario_site_id"),
                    "device_id": action.get("device_id"),
                    "service_origin_egm2008_m": action.get("service_origin_egm2008_m"),
                    "distinct_site_id": action.get("distinct_site_id"),
                }
                for action in explicit_actions
            ],
            "threshold": threshold,
            "scenario_continuous_service_acceptance": acceptance,
            "longest_deficit_comparison": {
                "baseline_m": baseline_deficit,
                "scenario_m": scenario_deficit,
                "reduction_m": delta,
                "baseline_s": baseline_acceptance.get("longest_outage_duration_s"),
                "scenario_s": acceptance.get("longest_outage_duration_s"),
                "threshold_m": threshold["threshold_m"],
                "threshold_s": (
                    None if threshold.get("route_speed_mps") in (None, 0)
                    else float(threshold["threshold_m"]) / float(threshold["route_speed_mps"])
                ),
                "achieved_45m_3s": achieved,
            },
            "scenario_discrete_summary": {
                "S": s_summary,
                "C": c_summary,
            },
            "residual_continuous_segments": residual_segments,
            "corridor_edge_and_width": {
                "centerline_service_status": gap.get("centerline_service_status"),
                "full_width_service_status": gap.get("full_width_service_status"),
                "full_width_width_contract": deepcopy(gap.get("full_width_width_contract")),
                "full_width_semantics": gap.get("full_width_semantics"),
                "residual_segment_count": len(residual_segments),
                "note": (
                    "full_width_service_status 是 P15 的离散面积代理，"
                    "不等同于连续服务；连续服务只由 max_continuous_deficit_projection_m 判定。"
                ),
            },
            "l1_geometry": deepcopy(l1_geometry),
            "still_not_proven": [
                "line_of_sight_and_clutter_not_evaluated",
                "provider_independence_not_evaluated",
                "shared_power_shared_backhaul_common_cause_not_evaluated",
                "non_discrete_corridor_edge_and_voxel_gaps_not_proven_zero",
                "field_continuous_service_measurement_not_performed",
                "equipment_installation_and_permission_not_confirmed",
                "formal_p16_p17_not_rerun",
            ],
            "input_fingerprints": {
                "p14": corridor.get("input_fingerprint"),
                "p15": gap.get("input_fingerprint"),
            },
            "baseline_corridor_gap_fingerprint": (baseline_summary or {}).get(
                "final_hypothetical_corridor_gap_fingerprint"
            ),
            "grid_used": {
                "grid_cells": len((state.get("grid") or {}).get("cells") or []),
                "terrain_cells": len((((state.get("grid_attributes") or {}).get("terrain") or {}).get("cells")) or {}),
            },
        }

    # ---------------------------------------------------------------- internals
    def _sample_dem(self, coordinate):
        if coordinate is None or not callable(self._dem_sampler):
            return None, "dem_sampler_unavailable"
        try:
            value = self._dem_sampler(float(coordinate[0]), float(coordinate[1]))
        except Exception:
            return None, "dem_sampling_failed"
        if value is None or isinstance(value, bool):
            return None, "dem_nodata"
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None, "dem_nodata"
        if not isfinite(number):
            return None, "dem_nodata"
        return number, None

    def _tower_scenario_id(self, item):
        tower_id = str(
            (item.get("metadata") or {}).get("host", {}).get("host_tower_id")
            or item.get("tower_id")
            or str(item.get("site_id") or "").replace("tower-colocation:", "")
        )
        return "EA-TWR-" + tower_id

    def _base_record(
        self, *, scenario_site_id, name, coordinate, source_kind, source_class,
        source_category, source_site_ids, source_records, inherited_confirmed_facts,
        horizontal_crs_status, horizontal_crs_source, ground, ground_status,
        ground_source, mount, origin, origin_status, origin_method,
        origin_authority, allowed, device_ids, reuse_class, source_allowed_subsystems,
        uncertainty, origin_components=None, extra=None,
    ):
        record = {
            "scenario_site_id": scenario_site_id,
            "name": name,
            "coordinate": coordinate,
            #: 同坐标不同 ID 的物理站址归并键（R5-H 冗余门禁）：只用于统计/下游
            #: 去重，绝不改写 scenario_site_id 或 source_site_ids。
            "physical_site_key": list(_round_coord(coordinate, 6)),
            "source_kind": source_kind,
            "source_class": source_class,
            "source_category": source_category,
            "source_site_ids": [item for item in source_site_ids if item],
            "source_records": deepcopy(source_records),
            "horizontal_crs": {
                "value": "EPSG:4490",
                "status": horizontal_crs_status,
                "confirmed": horizontal_crs_status == "confirmed_inherited",
                "inherited_confirmed_facts": bool(inherited_confirmed_facts),
                "source_crs_status": "source_undeclared",
                "assumption_only": horizontal_crs_status != "confirmed_inherited",
                "source": horizontal_crs_source,
            },
            "ground_elevation": {
                "value_m": None if ground is None else float(ground),
                "status": ground_status,
                "source": ground_source,
                "vertical_reference": "egm2008_orthometric",
                "method": self._dem_descriptor.get("method", "nearest_pixel"),
            },
            "installation_origin": {
                "ground_egm2008_m": None if ground is None else float(ground),
                "mount_height_agl_m": None if mount is None else float(mount),
                "service_origin_egm2008_m": float(origin),
                "status": origin_status,
                "method": origin_method,
                "authority": origin_authority,
                "assumption_only": origin_status != "confirmed",
                "real_world_confirmed": False,
                "components": deepcopy(origin_components or {}),
                "uncertainty": list(uncertainty),
            },
            "allowed_subsystems": list(allowed),
            "source_allowed_subsystems": list(source_allowed_subsystems),
            "device_ids": list(device_ids),
            "reuse_class": reuse_class,
            "assumption": scenario_site_marker(
                {"scenario_id": None, "version": None, "does_not_change": []},
                source_kind=source_kind,
                source_site_id=scenario_site_id,
                inherited_confirmed=inherited_confirmed_facts,
            ),
            "formal_eligible": False,
            "real_world_confirmed": False,
            ASSUMPTION_ONLY_KEY: True,
        }
        if extra:
            record.update(deepcopy(extra))
        return record

    def _excluded(self, source_kind, source_id, name, coordinate, reason, detail):
        return {
            "source_kind": source_kind,
            "source_id": str(source_id or ""),
            "name": name,
            "coordinate": deepcopy(coordinate),
            "reason": reason,
            "detail": detail,
            "assumption_only": True,
            "real_world_confirmed": False,
        }

    def _merge_exact_landing(self, records):
        groups = {}
        conflicts = []
        for record in records:
            key = _round_coord(record["coordinate"], 6)
            groups.setdefault(key, []).append(record)
        merged = []
        for key, group in sorted(groups.items(), key=lambda pair: pair[0]):
            if len(group) == 1:
                merged.append(group[0])
                continue
            references = [item for item in group if item["source_kind"] == "reference_landing_site"]
            facilities = [item for item in group if item["source_kind"] == "standardized_facility"]
            primary = references[0] if references else group[0]
            record = deepcopy(primary)
            record["source_records"] = []
            record["source_site_ids"] = []
            record["source_kinds"] = []
            for item in group:
                record["source_records"].extend(deepcopy(item["source_records"]))
                record["source_site_ids"].extend(item["source_site_ids"])
                record["source_kinds"].append(item["source_kind"])
                if item.get("reference_site_id"):
                    record["reference_site_id"] = item["reference_site_id"]
                if item.get("facility_id") and item["source_kind"] == "standardized_facility":
                    record.setdefault("facility_ids", []).append(item["facility_id"])
            record["source_site_ids"] = list(dict.fromkeys(record["source_site_ids"]))
            record["source_kinds"] = list(dict.fromkeys(record["source_kinds"]))
            record["source_records"] = _dedupe_source_records(record["source_records"])
            record["dedupe"] = {
                "reason": "exact_coordinate_merge_same_landing_family",
                "coordinate_key": list(key),
                "merged_record_count": len(group),
                "source_kinds": list(record["source_kinds"]),
                "auto_merge": True,
                "different_reference_ids_merged": len({
                    item.get("reference_site_id") for item in group if item.get("reference_site_id")
                }) > 1,
            }
            if record["dedupe"]["different_reference_ids_merged"]:
                conflicts.append({
                    "kind": "same_coordinate_multiple_reference_ids",
                    "coordinate": list(key),
                    "source_site_ids": deepcopy(record["source_site_ids"]),
                    "action": "merged_by_exact_coordinate",
                    "note": "同一坐标上的多个参考 ID 合并为一个场景站址；身份仍全部保留",
                })
            merged.append(record)
        # Same reference ID, different coordinate: explicit conflict, no merge.
        by_ref = {}
        for record in merged:
            ref = record.get("reference_site_id")
            if ref:
                by_ref.setdefault(ref, []).append(record)
        for ref, items in by_ref.items():
            if len({_round_coord(item["coordinate"], 6) for item in items}) > 1:
                conflicts.append({
                    "kind": "same_reference_id_different_location",
                    "reference_site_id": ref,
                    "scenario_site_ids": [item["scenario_site_id"] for item in items],
                    "action": "kept_separate",
                })
        return merged, conflicts

    def _name_conflicts(self, records):
        groups = {}
        for record in records:
            groups.setdefault(normalize_ovobj_name(record.get("name")), []).append(record)
        conflicts = []
        for name, items in groups.items():
            if len({_round_coord(item["coordinate"], 6) for item in items}) > 1:
                conflicts.append({
                    "kind": "same_name_different_location",
                    "name": name,
                    "scenario_site_ids": [item["scenario_site_id"] for item in items],
                    "action": "kept_separate",
                })
        return conflicts

    def _reference_link_conflicts(self, records):
        """Disclose shared reference IDs at different coordinates without merging.

        The R4-B review found, for example, N006 and the standardized
        ``东白莲岛无人机起降点`` sharing ``RLS-2597716FD302`` while being about
        1051 m apart.  A shared reference ID is **not** an identity proof; this
        check keeps both records and reports the conflict.
        """

        by_reference = {}
        for record in records:
            for source_id in record.get("source_site_ids") or []:
                text = str(source_id)
                if text.startswith("RLS-"):
                    by_reference.setdefault(text, []).append(record)
        by_registry_link = {}
        for record in records:
            link = record.get("registry_match_id")
            if link:
                by_registry_link.setdefault(str(link), []).append(record)
        conflicts = []
        seen_pairs = set()
        for link, linked in by_registry_link.items():
            for reference in by_reference.get(link, []):
                for record in linked:
                    if record is reference:
                        continue
                    pair = tuple(sorted((record["scenario_site_id"], reference["scenario_site_id"])))
                    if pair in seen_pairs:
                        continue
                    distance = _distance_m(record["coordinate"], reference["coordinate"])
                    if distance <= 1.0:
                        continue
                    seen_pairs.add(pair)
                    conflicts.append({
                        "kind": "same_reference_site_id_different_location",
                        "reference_site_id": link,
                        "scenario_site_ids": list(pair),
                        "distance_m": round(distance, 3),
                        "action": "kept_separate",
                        "note": "相同参考 ID 不等于相同实际位置；未自动合并",
                    })
        # Multiple standardized records sharing the same reference ID at
        # different coordinates are also a conflict, even without a direct
        # reference-landing record.
        for link, linked in by_registry_link.items():
            for index, left in enumerate(linked):
                for right in linked[index + 1:]:
                    pair = tuple(sorted((left["scenario_site_id"], right["scenario_site_id"])))
                    if pair in seen_pairs:
                        continue
                    distance = _distance_m(left["coordinate"], right["coordinate"])
                    if distance <= 1.0:
                        continue
                    seen_pairs.add(pair)
                    conflicts.append({
                        "kind": "shared_registry_link_different_location",
                        "registry_match_id": link,
                        "scenario_site_ids": list(pair),
                        "distance_m": round(distance, 3),
                        "action": "kept_separate",
                    })
        return conflicts

    def _project_sites(self, records, scenario):
        projected = []
        by_id = {}
        for record in records:
            vertical = {
                "surface_elevation_m": record["ground_elevation"]["value_m"],
                "surface_vertical_reference": "egm2008_orthometric",
                "mount_height_agl_m": record["installation_origin"]["mount_height_agl_m"],
                "service_origin_egm2008_m": record["installation_origin"]["service_origin_egm2008_m"],
                "confirmed": True,
                "status": "confirmed",
                "source": "engineering_assumption_scenario_projection",
                "assumption_only": True,
                "real_world_confirmed": False,
                "projection_only": True,
                "uncertainty": deepcopy(record["installation_origin"]["uncertainty"]),
                "planning_origin_components": deepcopy(record["installation_origin"]["components"]),
            }
            if record["source_kind"] == "tower_colocation":
                source_vertical = record.get("vertical_profile_source") or {}
                vertical.update({
                    "planning_service_origin_egm2008_m": record["installation_origin"]["service_origin_egm2008_m"],
                    "planning_origin_status": record["installation_origin"]["status"],
                    "planning_origin_method": record["installation_origin"]["method"],
                    "planning_origin_authority": record["installation_origin"]["authority"],
                })
                if source_vertical.get("planning_origin_limitations"):
                    vertical["planning_origin_limitations"] = deepcopy(
                        source_vertical["planning_origin_limitations"]
                    )
            planning = {
                "reuse_class": record["reuse_class"],
                "add_device_allowed": True,
                "planning_cost": None,
                "cost_unit": None,
                "source": "engineering_assumption_scenario_projection",
                "confirmed": True,
                "status": "confirmed",
                "assumption_only": True,
                "real_world_confirmed": False,
                "projection_only": True,
            }
            candidate = {
                "site_id": record["scenario_site_id"],
                "name": record["name"],
                "coordinate": deepcopy(record["coordinate"]),
                "site_type": record["source_class"],
                "available_subsystems": deepcopy(record["allowed_subsystems"]),
                "usable": True,
                "locked": False,
                "source": "engineering_assumption_scenario",
                "vertical_profile": vertical,
                "planning_profile": planning,
                "metadata": {
                    "scenario_mode": ENGINEERING_ASSUMPTION_MODE,
                    "scenario_id": scenario["scenario_id"],
                    "scenario_version": scenario["version"],
                    ASSUMPTION_ONLY_KEY: True,
                    REAL_WORLD_CONFIRMED_KEY: False,
                    "projection_only": True,
                    "source_kind": record["source_kind"],
                    "source_site_ids": deepcopy(record["source_site_ids"]),
                    "source_records": deepcopy(record["source_records"]),
                    "does_not_change": list(scenario.get("does_not_change") or []),
                },
            }
            projected.append(candidate)
            by_id[record["scenario_site_id"]] = candidate
        return projected, by_id

    def _pool_counts(self, records, excluded, source_counts, actions):
        by_source = {}
        for record in records:
            key = record["source_kind"]
            bucket = by_source.setdefault(key, {
                "sites": 0,
                "generated_actions": 0,
                "eligible_actions": 0,
                "by_subsystem": {
                    code: {"generated": 0, "eligible": 0} for code in ("C", "N", "S")
                },
            })
            bucket["sites"] += 1
            counts = record.get("action_counts") or {}
            bucket["generated_actions"] += int(counts.get("generated") or 0)
            bucket["eligible_actions"] += int(counts.get("eligible") or 0)
            for code in ("C", "N", "S"):
                bucket["by_subsystem"][code]["generated"] += int(
                    ((counts.get("by_subsystem") or {}).get(code) or {}).get("generated") or 0
                )
                bucket["by_subsystem"][code]["eligible"] += int(
                    ((counts.get("by_subsystem") or {}).get(code) or {}).get("eligible") or 0
                )
        generated = len(actions)
        eligible = sum(1 for item in actions if item.get("scenario_eligible"))
        by_subsystem = {}
        for code in ("C", "N", "S"):
            subset = [item for item in actions if item.get("subsystem") == code]
            by_subsystem[code] = {
                "generated": len(subset),
                "eligible": sum(1 for item in subset if item.get("scenario_eligible")),
                "devices": sorted({str(item.get("device_id")) for item in subset}),
                "qualification": (
                    "scenario_assumption_allowed_no_engineering_device_model"
                    if code == "N" else "scenario_assumption_qualified"
                ),
            }
        by_device = {}
        for item in actions:
            device_id = str(item.get("device_id"))
            bucket = by_device.setdefault(device_id, {
                "subsystem": item.get("subsystem"),
                "generated": 0,
                "eligible": 0,
                "radius_by_surface": deepcopy(
                    ((item.get("coverage_geometry") or {}).get("radius_by_surface"))
                ),
            })
            bucket["generated"] += 1
            if item.get("scenario_eligible"):
                bucket["eligible"] += 1
        exclusion_counts = {}
        for item in excluded:
            exclusion_counts[item["reason"]] = exclusion_counts.get(item["reason"], 0) + 1
        coordinate_groups = {}
        dem_zero_ids = []
        for record in records:
            coordinate = tuple(round(float(value), 6) for value in record["coordinate"])
            coordinate_groups.setdefault(coordinate, []).append(record["scenario_site_id"])
            ground = (record.get("ground_elevation") or {}).get("value_m")
            if ground is not None and float(ground) == 0.0:
                dem_zero_ids.append(record["scenario_site_id"])
        same_coordinate_groups = [
            {
                "coordinate": list(coordinate),
                #: 显式的物理站址归并键：下游冗余/独立性计算按它去重，而不是按
                #: ``scenario_site_id`` 的数量。身份与来源记录仍原样保留。
                "physical_site_key": list(coordinate),
                "site_count": len(site_ids),
                "surplus_site_id_count": len(site_ids) - 1,
                "scenario_site_ids": list(site_ids),
            }
            for coordinate, site_ids in coordinate_groups.items()
            if len(site_ids) > 1
        ]
        same_coordinate_groups.sort(key=lambda item: (-item["site_count"], item["coordinate"]))
        return {
            "canonical_site_count": len(records),
            "unique_coordinate_count": len(coordinate_groups),
            "same_coordinate_group_count": len(same_coordinate_groups),
            "same_coordinate_surplus_site_id_count": sum(
                item["site_count"] - 1 for item in same_coordinate_groups
            ),
            "same_coordinate_groups": same_coordinate_groups,
            "physical_site_identity": {
                "identity_count": len(records),
                "physical_site_count": len(coordinate_groups),
                "surplus_identity_count": len(records) - len(coordinate_groups),
                "same_coordinate_group_count": len(same_coordinate_groups),
                "same_coordinate_surplus_site_id_count": sum(
                    item["site_count"] - 1 for item in same_coordinate_groups
                ),
                "groups": deepcopy(same_coordinate_groups),
                "semantics": (
                    "同坐标多 ID 是已披露的 same-site/multi-ID 组：冗余或独立性计算"
                    "必须按 physical_site_key/坐标去重，不得把同址 ID 当作多个独立"
                    "物理站址；动作仍按身份生成，scenario_site_id 与来源记录不改。"
                ),
            },
            "coordinate_identity_semantics": (
                "scenario_site_id counts source identities; equal coordinates are "
                "disclosed same-site/multi-ID groups and must not be counted as "
                "additional physical sites"
            ),
            "dem_zero_site_count": len(dem_zero_ids),
            "terrain_pending_review_site_ids": dem_zero_ids,
            "terrain_pending_review_semantics": (
                "DEM=0 m is a terrain/land-sea source value pending verification; "
                "it is not evidence that the site is buildable or that the mount "
                "origin is confirmed"
            ),
            "generated_action_count": generated,
            "eligible_scenario_action_count": eligible,
            "ineligible_generated_action_count": generated - eligible,
            "by_source_kind": dict(sorted(by_source.items())),
            "by_subsystem": by_subsystem,
            "by_device": dict(sorted(by_device.items())),
            "excluded_count": len(excluded),
            "excluded_by_reason": dict(sorted(exclusion_counts.items())),
            "source_record_counts": deepcopy(source_counts),
            "radar": {
                "generated": 0,
                "eligible": 0,
                "reason": "radar_planning_authority_is_radar_surveillance_layout_not_scenario_pool",
            },
            "navigation": {
                "generated": 0,
                "eligible": 0,
                "reason": "navigation_reference_stations_use_independent_suitability_contract",
            },
            "semantics": "counts_are_scenario_generation_not_formal_p16_eligibility",
        }

    # --------------------------------------------------- R5-H surface screening
    def _apply_surface_screening(self, records, excluded):
        """R5-H：把 canonical ``land_mask`` 接入候选身份，sea 只移出陆基假设。

        * 分类实现唯一：注入的 ``LandMaskSource.classify_detailed``（与 R5-G 核验
          脚本同一实现），绝不在本模块另写海岸线/缓冲带判定；
        * ``sea``：不参与陆基假设，但完整来源（``source_site_ids`` /
          ``source_records``）与分类证据保留在 ``excluded``；
        * ``coastal_uncertain``：保留，但只作待核，不升级为可建；
        * DEM=0 m 只是地形采样值，单独标记 ``terrain_pending_review``，不据此判海；
        * ``unknown``：fail-closed，不生成陆基动作。
        """

        source = self._land_mask_source
        if source is None:
            return {
                "status": "not_configured",
                "retained_ids": {record["scenario_site_id"] for record in records},
                "blocked_reason": None,
                "land_mask": None,
                "surface_class_counts": None,
                "dem_zero_by_surface": None,
                "pre_surface_sites": [
                    self._surface_audit_record(record) for record in records
                ],
            }

        prepared = source.prepare()
        describe = source.describe() if hasattr(source, "describe") else {}
        land_mask = {
            "source_role": "land_mask",
            "layer_name": describe.get("layer_name"),
            "source_crs": describe.get("source_crs"),
            "metric_crs": describe.get("metric_crs"),
            "crs_status": describe.get("crs_status"),
            "layer_resolution": describe.get("layer_resolution"),
            "polygon_count": describe.get("polygon_count"),
            "policy": deepcopy(self._surface_policy),
            "context": deepcopy(self._land_mask_context),
            "classification_implementation": "LandMaskSource.classify_detailed",
            "semantics": (
                "canonical_land_mask_point_classification_not_buildability_or_"
                "continuous_coastline"
            ),
        }
        blocked_reason = None
        if prepared is None:
            blocked_reason = "land_mask_unavailable:" + str(
                describe.get("reason") or "unknown"
            )

        surface_counts = {name: 0 for name in SURFACE_CLASSES}
        dem_zero_counts = {name: 0 for name in SURFACE_CLASSES}
        retained_ids = set()
        for record in records:
            coordinate = record["coordinate"]
            if blocked_reason:
                surface = "unknown"
                confidence = "unknown"
                evidence = {"reason": blocked_reason}
            else:
                detail = source.classify_detailed(coordinate[0], coordinate[1])
                surface = str(detail.get("surface_class") or "unknown")
                confidence = detail.get("classification_confidence")
                evidence = deepcopy(detail.get("evidence") or {})
            if surface not in SURFACE_CLASSES:
                surface = "unknown"
            dem_value = (record.get("ground_elevation") or {}).get("value_m")
            dem_zero = dem_value is not None and float(dem_value) == 0.0
            decision = SURFACE_DECISIONS[surface]
            retained = surface in SURFACE_RETAINED_CLASSES and not blocked_reason
            record["surface_class"] = surface
            record["surface_classification"] = {
                "surface_class": surface,
                "classification_confidence": confidence,
                "decision": decision,
                "evidence": deepcopy(evidence),
                "land_assumption_retained": retained,
                "requires_surface_review": surface == "coastal_uncertain",
                "offshore_platform_evidence": deepcopy(OFFSHORE_PLATFORM_EVIDENCE),
                "buildable": False,
                "formal_eligible": False,
                "semantics": (
                    "canonical_land_mask_point_classification_not_buildability"
                ),
            }
            record["land_assumption"] = {
                "decision": decision,
                "retained": retained,
                "excluded_from_land_assumption": surface == "sea",
                "blocked": surface == "unknown" or bool(blocked_reason),
                "requires_surface_review": surface == "coastal_uncertain",
                "offshore_platform_evidence": deepcopy(OFFSHORE_PLATFORM_EVIDENCE),
                "buildable": False,
                "real_world_confirmed": False,
                "assumption_only": True,
            }
            record["requires_surface_review"] = surface == "coastal_uncertain"
            record["terrain_pending_review"] = dem_zero
            record["buildable"] = False
            record["formal_eligible"] = False
            surface_counts[surface] = surface_counts.get(surface, 0) + 1
            if dem_zero:
                dem_zero_counts[surface] = dem_zero_counts.get(surface, 0) + 1
            if retained:
                retained_ids.add(record["scenario_site_id"])
                continue
            #: 不删除来源身份：sea/unknown 以排除项保留全部来源与分类证据。
            reason = (
                "surface_class_unknown_fail_closed"
                if surface == "unknown"
                else "surface_class_sea_no_offshore_platform_evidence"
            )
            detail_text = (
                "canonical land_mask 无法给出可信分类；按 fail-closed 不进入陆基假设。"
                if surface == "unknown"
                else "canonical land_mask 判定为 sea；无登记的海上平台证据，默认不参与"
                     "陆基假设。原始身份与来源记录保留，不判定该坐标永久不可用。"
            )
            entry = self._excluded(
                record["source_kind"], record["scenario_site_id"], record.get("name"),
                coordinate, reason, detail_text,
            )
            entry.update({
                "scenario_site_id": record["scenario_site_id"],
                "surface_class": surface,
                "surface_evidence": deepcopy(evidence),
                "land_mask_layer_name": describe.get("layer_name"),
                "source_site_ids": deepcopy(record["source_site_ids"]),
                "source_records": deepcopy(record["source_records"]),
                "land_assumption_retained": False,
                "requires_surface_review": False,
                "offshore_platform_evidence": deepcopy(OFFSHORE_PLATFORM_EVIDENCE),
                "buildable": False,
            })
            excluded.append(entry)

        if blocked_reason is None and surface_counts.get("unknown"):
            blocked_reason = "surface_class_unknown_fail_closed"
        status = "blocked" if blocked_reason else "applied"
        return {
            "status": status,
            "retained_ids": retained_ids,
            "blocked_reason": blocked_reason,
            "land_mask": land_mask,
            "surface_class_counts": surface_counts,
            "dem_zero_by_surface": dem_zero_counts,
            "pre_surface_sites": [
                self._surface_audit_record(record) for record in records
            ],
        }

    def _surface_audit_record(self, record):
        """筛选前审计摘要：保留 700 个原始身份的可追溯字段，不复制完整 action。"""

        item = {
            "scenario_site_id": record["scenario_site_id"],
            "name": record.get("name"),
            "coordinate": deepcopy(record.get("coordinate")),
            "physical_site_key": deepcopy(record.get("physical_site_key")),
            "source_kind": record.get("source_kind"),
            "source_class": record.get("source_class"),
            "source_site_ids": deepcopy(record.get("source_site_ids") or []),
            "ground_elevation": deepcopy(record.get("ground_elevation") or {}),
            "actions": {
                "generated": int((record.get("action_counts") or {}).get("generated") or 0),
                "eligible": int((record.get("action_counts") or {}).get("eligible") or 0),
            },
        }
        if "surface_class" in record:
            item.update({
                "surface_class": record["surface_class"],
                "decision": (record.get("land_assumption") or {}).get("decision"),
                "land_assumption_retained": bool(
                    (record.get("land_assumption") or {}).get("retained")
                ),
                "requires_surface_review": bool(record.get("requires_surface_review")),
                "terrain_pending_review": bool(record.get("terrain_pending_review")),
                "dem_zero": (
                    (record.get("ground_elevation") or {}).get("value_m") == 0.0
                ),
            })
        return item

    def _surface_screening_counts(self, screening, pre_counts, post_counts):
        """把筛选结果与 R5-G 的目标口径写成可审计的计数块（不硬编码数字）。"""

        surface_counts = screening.get("surface_class_counts") or {}
        dem_zero = screening.get("dem_zero_by_surface") or {}
        status = screening["status"]
        pre_summary = {
            "canonical_site_count": pre_counts["canonical_site_count"],
            "unique_coordinate_count": pre_counts["unique_coordinate_count"],
            "generated_action_count": pre_counts["generated_action_count"],
            "eligible_scenario_action_count": pre_counts[
                "eligible_scenario_action_count"
            ],
            "same_coordinate_group_count": pre_counts["same_coordinate_group_count"],
            "same_coordinate_surplus_site_id_count": pre_counts[
                "same_coordinate_surplus_site_id_count"
            ],
        }
        post_summary = {
            "canonical_site_count": post_counts["canonical_site_count"],
            "unique_coordinate_count": post_counts["unique_coordinate_count"],
            "generated_action_count": post_counts["generated_action_count"],
            "eligible_scenario_action_count": post_counts[
                "eligible_scenario_action_count"
            ],
            "same_coordinate_group_count": post_counts["same_coordinate_group_count"],
            "same_coordinate_surplus_site_id_count": post_counts[
                "same_coordinate_surplus_site_id_count"
            ],
        }
        return {
            "schema_version": SURFACE_SCREENING_SCHEMA_VERSION,
            "status": status,
            "land_mask": deepcopy(screening.get("land_mask")),
            "surface_class_counts": (
                {name: int(surface_counts.get(name) or 0) for name in SURFACE_CLASSES}
                if screening.get("surface_class_counts") is not None else None
            ),
            "dem_zero_by_surface": (
                {name: int(dem_zero.get(name) or 0) for name in SURFACE_CLASSES}
                if screening.get("dem_zero_by_surface") is not None else None
            ),
            "pre_surface": pre_summary,
            "post_surface": post_summary,
            "retained_pending_review": {
                "coastal_uncertain_site_count": int(
                    (screening.get("surface_class_counts") or {}).get(
                        "coastal_uncertain"
                    ) or 0
                ),
                "land_dem_zero_site_count": int(dem_zero.get("land") or 0),
                "coastal_uncertain_dem_zero_site_count": int(
                    dem_zero.get("coastal_uncertain") or 0
                ),
                "semantics": (
                    "coastal_uncertain 与 DEM=0 的 land/coastal 身份保留在陆基假设中，"
                    "但只作待核，不升级为可建，也不代表安装原点已确认"
                ),
            },
            "offshore_platform_evidence": deepcopy(OFFSHORE_PLATFORM_EVIDENCE),
            "blocked_reason": screening.get("blocked_reason"),
            "semantics": (
                "canonical land_mask screening applied before the candidate pool is "
                "emitted; sea identities keep their source records in excluded and do "
                "not generate land-based actions; coastal_uncertain and DEM=0 remain "
                "pending review, not buildability"
            ),
            "does_not_change": [
                "p16_formal_eligibility", "p16_greedy_order", "p16_selection_limit",
                "device_radius_by_surface", "rid_radius", "p14_p15_p16_p17_results",
                "project_state_facts", "source_site_ids",
            ],
        }

    def _continuous_threshold(self, state):
        from ..algorithms.continuous_service.v1 import (
            ContinuousServiceAcceptabilityV1, _limit as _resolve_limit,
        )
        from ..domain.fc30_profile import fc30_planning_speeds
        from .planning_evidence_service import aircraft_profile_with_evidence

        profile = aircraft_profile_with_evidence(state)
        policy = state.get("cns_continuous_service_policy") or {}
        parameters = ContinuousServiceAcceptabilityV1._parameters(
            state.get("planning_evidence") or {}, policy,
        )
        seconds = _resolve_limit(parameters, "C", "service_outage", policy)
        speeds = fc30_planning_speeds(profile)
        speed = speeds.get("route_speed_mps")
        speed_value = float(speed) if isinstance(speed, (int, float)) and float(speed) > 0 else None
        threshold_m = (
            None if (seconds is None or speed_value is None or float(seconds) <= 0)
            else float(seconds) * speed_value
        )
        if threshold_m is None:
            raise EngineeringAssumptionError("连续服务阈值不可判定；不得静默回落到 45 m")
        return {
            "threshold_m": threshold_m,
            "threshold_s": float(seconds),
            "route_speed_mps": speed_value,
            "aircraft_id": (profile or {}).get("aircraft_id"),
            "threshold_parameter": "c_full_outage_max_s",
            "authority": (parameters.get("c_full_outage_max_s") or {}).get("authority"),
        }

    def _subsystem_summary(self, gap, code):
        for route in gap.get("routes") or []:
            for item in route.get("subsystems") or []:
                if item.get("subsystem") != code:
                    continue
                return {
                    "route_id": route.get("route_id"),
                    "subsystem": code,
                    "status": item.get("status"),
                    "max_continuous_deficit_projection_m": item.get(
                        "max_continuous_deficit_projection_m"
                    ),
                    "total_confirmed_deficit_projection_m": item.get(
                        "total_confirmed_deficit_projection_m"
                    ),
                    "service": _compact_nested(item.get("service")),
                    "redundancy": _compact_nested(item.get("redundancy")),
                    "combined": _compact_nested(item.get("combined")),
                    "service_redundancy": _compact_nested(item.get("service_redundancy")),
                    "continuous_deficit_segments": _compact_segments(
                        item.get("continuous_deficit_segments")
                    ),
                    "causes": deepcopy(item.get("causes") or []),
                }
        return None


def _conflict_summary(conflicts):
    by_kind = {}
    by_band = {}
    by_action = {}
    for item in conflicts:
        kind = item.get("kind") or "unknown"
        by_kind[kind] = by_kind.get(kind, 0) + 1
        action = item.get("action") or "kept_separate"
        by_action[action] = by_action.get(action, 0) + 1
        distance = item.get("distance_m")
        if not isinstance(distance, (int, float)):
            band = "unknown"
        elif distance <= 200:
            band = "le_200m"
        elif distance <= 1000:
            band = "200m_1km"
        else:
            band = "gt_1km"
        by_band.setdefault(kind, {})
        by_band[kind][band] = by_band[kind].get(band, 0) + 1
    examples = sorted(
        (item for item in conflicts if isinstance(item.get("distance_m"), (int, float))),
        key=lambda item: float(item["distance_m"]),
        reverse=True,
    )[:12]
    return {
        "total": len(conflicts),
        "by_kind": dict(sorted(by_kind.items())),
        "by_action": dict(sorted(by_action.items())),
        "kept_separate_count": by_action.get("kept_separate", 0),
        "merged_by_exact_coordinate_count": by_action.get("merged_by_exact_coordinate", 0),
        "by_distance_band": {key: dict(sorted(value.items())) for key, value in by_band.items()},
        "notable_examples": examples,
        "action": "kept_separate_unless_exact_coordinate_merge",
    }


def _dedupe_source_records(records):
    seen = set()
    result = []
    for record in records:
        key = json.dumps(record, sort_keys=True, ensure_ascii=False)
        if key in seen:
            continue
        seen.add(key)
        result.append(record)
    return result


def _compact_nested(value):
    if not isinstance(value, dict):
        return deepcopy(value)
    result = {}
    for key, item in value.items():
        if isinstance(item, list):
            result[key] = item[:20]
            if len(item) > 20:
                result[f"{key}_truncated_count"] = len(item) - 20
        elif isinstance(item, dict):
            result[key] = _compact_nested(item)
        else:
            result[key] = deepcopy(item)
    return result


def _compact_segments(segments):
    result = []
    for item in segments or []:
        if not isinstance(item, dict):
            continue
        result.append({
            key: deepcopy(value)
            for key, value in item.items()
            if key in (
                "start_route_offset_m", "end_route_offset_m", "length_m",
                "causes", "exceeds_threshold", "start_chainage_m", "end_chainage_m",
            )
        })
    return result


__all__ = [
    "EngineeringAssumptionScenarioService",
    "EXPECTED_ROUTE_SPEED_MPS",
    "EXPECTED_THRESHOLD_M",
    "SCENARIO_TARGETS",
]
