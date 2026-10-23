"""R6 scenario adaptation layer: feed the R5 engineering-assumption pool to P16.

This module is deliberately an **adapter**, not a planner.  It never edits the
canonical P16 algorithm, its reuse tiers, its greedy ranking, its eligibility
gates, its intervention cap, or any canonical ``ProjectState`` writer.

What it does:

1. collapse the R5 scenario candidate pool by ``physical_site_key`` so that two
   source identities at the same physical coordinate cannot become two
   independent P16 sites (the canonical P16 ``distinct_site_id`` is derived from
   the placeholder site id, so the input itself must be de-duplicated);
2. validate the resulting scenario actions against the P16 input contract
   (C/S device, service key, installation origin, surface class, engineering
   assumption labels, scenario eligibility);
3. call the unmodified ``CorridorSitePlanningService.plan`` pure-compute path
   with a read-only session whose candidate actions are the de-duplicated
   scenario actions.

The original source identities and source records remain attached to every
merged action/site.  The output is explicitly ``assumption_only`` and is never
written into the formal project state or ``candidate_sites``.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from math import isfinite

from ..algorithms.corridor.v1 import CNSServiceCorridorV1
from ..algorithms.corridor_gap.v1 import CNSCorridorGapAnalyzerV1
from ..domain.cns_service_contract import geometry_usable, service_contract_for
from ..domain.engineering_assumption import (
    ASSUMPTION_ONLY_KEY,
    CANDIDATE_REUSE_CLASS,
    ENGINEERING_ASSUMPTION_MODE,
    NEW_BUILD_REUSE_CLASS,
    REAL_WORLD_CONFIRMED_KEY,
    TOWER_REUSE_CLASS,
)
from ..domain.site_planning import REUSE_TIERS
from ..site_planner.corridor_reuse_first_v2 import CorridorReuseFirstSitePlannerV2
from .corridor_site_planning_service import CorridorSitePlanningService
from .planning_evidence_service import device_catalog_with_evidence


P16_ADAPTER_SCHEMA_VERSION = "r6-p16-scenario-adapter-v1"

#: The R5 scenario only declares C and S engineering device models.  ``N`` is an
#: allowed subsystem but has no engineering device model, so it must not appear
#: in the P16 candidate actions.
EXPECTED_DEVICE_BY_SUBSYSTEM = {
    "C": "COMM-BASELINE-4KM",
    "S": "RID-BASELINE-2-5KM",
}
EXPECTED_SERVICE_KEY = {
    "C": "C:communication",
    "S": "S:rid_cooperative",
}
RETAINED_SURFACES = ("land", "coastal_uncertain")
SURFACE_REVIEW_REQUIRED = ("coastal_uncertain",)

#: Stable preference when several source identities share one coordinate.
#: A real tower wins over a standardized facility so that merging never replaces
#: an inherited tower origin with a DEM+15 m assumption.
_SOURCE_KIND_PRIORITY = {
    "tower_colocation": 0,
    "explicit_assumption_site": 1,
    "reference_landing_site": 2,
    "standardized_facility": 3,
    "ovobj_label": 4,
    "project_node": 5,
}


def _coord(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    try:
        lon, lat = float(value[0]), float(value[1])
    except (TypeError, ValueError):
        return None
    if not (isfinite(lon) and isfinite(lat)):
        return None
    if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
        return None
    return (lon, lat)


def physical_site_key(*candidates):
    """Return the rounded coordinate key, or ``None`` when no valid coordinate."""

    for value in candidates:
        coord = _coord(value)
        if coord is not None:
            return [round(coord[0], 6), round(coord[1], 6)]
    return None


def _source_priority(kind):
    return _SOURCE_KIND_PRIORITY.get(str(kind or ""), 9)


def _canonical_sha256(value):
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


def _merged_eligibility(actions):
    """Conservative merge: an action is eligible only if every identity is eligible."""

    reasons = []
    status = "eligible"
    scenario_eligible = True
    for action in actions:
        eligibility = action.get("eligibility") or {}
        if eligibility.get("status") != "eligible":
            status = "ineligible"
            reasons.extend(str(item) for item in eligibility.get("reasons") or [])
        if not action.get("scenario_eligible"):
            scenario_eligible = False
            if eligibility.get("status") == "eligible":
                reasons.append("scenario_eligible 标记与 eligibility 不一致")
    return {
        "status": status,
        "reasons": sorted(set(reasons)),
        "scenario_eligible": bool(status == "eligible" and scenario_eligible),
    }


def dedupe_candidate_pool_by_physical_site(pool):
    """Collapse the R5 pool to one P16 input identity per physical coordinate.

    The pool's ``scenario_site_id`` is a *source identity*, not a physical site.
    P16 derives ``distinct_site_id`` from that identity, so passing all 648
    identities through would let two same-coordinate source records inflate the
    independent-site count.  This function keeps exactly one representative per
    ``physical_site_key`` and records every merged identity on the surviving
    action/site.

    No action/device/eligibility is invented or relaxed: for the same device the
    merged action is eligible only when **all** contributing identities were
    eligible.
    """

    sites = [item for item in pool.get("sites") or [] if isinstance(item, dict)]
    actions = [item for item in pool.get("actions") or [] if isinstance(item, dict)]
    if not sites:
        raise ValueError("candidate pool 缺少 sites")
    if not actions:
        raise ValueError("candidate pool 缺少 actions")

    site_by_id = {str(item.get("scenario_site_id")): item for item in sites}
    action_site = {}
    for action in actions:
        site_id = str(action.get("scenario_site_id") or action.get("site_id") or "")
        action_site[str(action.get("action_id"))] = site_id

    def key_for_site(site):
        key = physical_site_key(site.get("physical_site_key"), site.get("coordinate"))
        if key is None:
            raise ValueError(f"站点缺少有效坐标：{site.get('scenario_site_id')}")
        return tuple(key)

    groups = {}
    for site in sites:
        groups.setdefault(key_for_site(site), []).append(site)
    site_count_by_key = {key: len(items) for key, items in groups.items()}

    actions_by_key = {}
    for action in actions:
        site_id = action_site.get(str(action.get("action_id"))) or ""
        site = site_by_id.get(site_id)
        if site is None:
            raise ValueError(f"动作引用了未知场景站址：{action.get('action_id')}")
        key = key_for_site(site)
        actions_by_key.setdefault(key, []).append(action)

    merged_sites = []
    merged_actions = []
    duplicate_groups = []
    for key in sorted(actions_by_key):
        group_sites = groups.get(key) or []
        group_actions = actions_by_key[key]
        representative_site = sorted(
            group_sites,
            key=lambda item: (_source_priority(item.get("source_kind")),
                              str(item.get("scenario_site_id") or "")),
        )[0]
        representative_id = str(representative_site.get("scenario_site_id"))
        scenario_ids = sorted(str(item.get("scenario_site_id")) for item in group_sites)
        source_ids = sorted({
            str(source_id)
            for item in group_sites
            for source_id in (item.get("source_site_ids") or [])
            if str(source_id)
        })
        source_records = []
        for item in group_sites:
            for record in item.get("source_records") or []:
                if isinstance(record, dict):
                    source_records.append(deepcopy(record))
        identity = {
            "physical_site_key": list(key),
            "representative_site_id": representative_id,
            "scenario_site_ids": scenario_ids,
            "source_site_ids": source_ids,
            "source_records": source_records,
            "surplus_identity_count": len(scenario_ids) - 1,
            "merge_policy": "one_physical_site_per_rounded_coordinate",
            "merge_semantics": (
                "same physical coordinate; original identities and source records "
                "are preserved for traceability, never counted as separate sites"
            ),
        }

        by_device = {}
        for action in group_actions:
            by_device.setdefault(str(action.get("device_id") or ""), []).append(action)
        merged_for_site = []
        for device_id in sorted(by_device):
            contributing = sorted(
                by_device[device_id],
                key=lambda item: (
                    _source_priority(
                        (site_by_id.get(str(item.get("scenario_site_id"))) or {}).get("source_kind")
                    ),
                    str(item.get("action_id") or ""),
                ),
            )
            primary = contributing[0]
            eligibility = _merged_eligibility(contributing)
            reuse_class = str(primary.get("reuse_class") or "")
            action = deepcopy(primary)
            action.update({
                "action_id": f"{reuse_class or 'unknown'}:{representative_id}:{device_id}",
                "site_id": representative_id,
                "scenario_site_id": representative_id,
                "distinct_site_id": f"site:{representative_id}",
                "physical_site_key": list(key),
                "physical_site_identity": deepcopy(identity),
                "source_site_ids": deepcopy(source_ids),
                "scenario_eligible": eligibility["scenario_eligible"],
                "eligibility": {
                    "status": eligibility["status"],
                    "reasons": eligibility["reasons"],
                },
                "merged_action_ids": sorted(str(item.get("action_id")) for item in contributing),
                "merged_action_count": len(contributing),
                #: Canonical ``confirmed`` is never set for an engineering
                #: assumption action, even when the scenario gate is eligible.
                "confirmed": False,
            })
            merged_for_site.append(action)
            merged_actions.append(action)

        site = deepcopy(representative_site)
        site.update({
            "scenario_site_id": representative_id,
            "physical_site_key": list(key),
            "physical_site_identity": deepcopy(identity),
            "source_site_ids": deepcopy(source_ids),
            "source_records": deepcopy(source_records),
            "merged_scenario_site_ids": scenario_ids,
            "merged_surplus_identity_count": len(scenario_ids) - 1,
            "actions": deepcopy(merged_for_site),
        })
        merged_sites.append(site)
        if len(scenario_ids) > 1:
            duplicate_groups.append({
                "physical_site_key": list(key),
                "representative_site_id": representative_id,
                "scenario_site_ids": scenario_ids,
                "source_site_ids": source_ids,
                "surplus_identity_count": len(scenario_ids) - 1,
                "actions_before": len(group_actions),
                "actions_after": len(merged_for_site),
                "merged_action_ids": sorted(str(item.get("action_id")) for item in group_actions),
            })

    merged_actions.sort(key=lambda item: str(item.get("action_id") or ""))
    merged_sites.sort(key=lambda item: str(item.get("scenario_site_id") or ""))

    distinct_site_ids = {str(item.get("distinct_site_id")) for item in merged_actions}
    physical_keys = [tuple(item["physical_site_key"]) for item in merged_actions]
    actions_per_key = {}
    for key in physical_keys:
        actions_per_key[key] = actions_per_key.get(key, 0) + 1
    counts = {
        "pool_identity_count": len(sites),
        "pool_physical_site_count": len(groups),
        "pool_action_count": len(actions),
        "deduped_site_count": len(merged_sites),
        "deduped_action_count": len(merged_actions),
        "dropped_action_count": len(actions) - len(merged_actions),
        "eligible_pool_action_count": sum(
            1 for item in actions if (item.get("eligibility") or {}).get("status") == "eligible"
        ),
        "eligible_deduped_action_count": sum(
            1 for item in merged_actions
            if (item.get("eligibility") or {}).get("status") == "eligible"
        ),
        "duplicate_group_count": len(duplicate_groups),
        "surplus_identity_count": sum(
            item["surplus_identity_count"] for item in duplicate_groups
        ),
        "distinct_site_id_count": len(distinct_site_ids),
        "physical_key_count": len(actions_per_key),
        "max_actions_per_physical_key": max(actions_per_key.values()) if actions_per_key else 0,
        "one_distinct_site_id_per_physical_key": (
            len(distinct_site_ids) == len(actions_per_key) == len(merged_sites)
        ),
        "semantics": (
            "R6 P16 scenario input is de-duplicated by physical_site_key; "
            "648 identities / 1,296 actions collapse to 644 sites / 1,288 actions; "
            "the original identities stay in physical_site_identity for audit only"
        ),
    }
    if counts["one_distinct_site_id_per_physical_key"] is not True:
        raise ValueError("物理站址归并后仍存在同一坐标多个 distinct_site_id")
    return {
        "schema_version": P16_ADAPTER_SCHEMA_VERSION,
        "status": "passed",
        "semantics": "scenario_pool_deduped_for_p16_physical_site_identity",
        "counts": counts,
        "duplicate_groups": duplicate_groups,
        "sites": merged_sites,
        "actions": merged_actions,
        "identity_sha256": _canonical_sha256({
            "sites": merged_sites,
            "actions": merged_actions,
        }),
    }


def _check(checks, code, ok, detail):
    checks.append({"code": code, "status": "passed" if ok else "failed", "detail": detail})
    return bool(ok)


def validate_scenario_p16_inputs(pool, deduped, state):
    """Validate the scenario->P16 contract without relaxing any formal gate.

    Returns ``status=blocked`` when any *contract* fact is wrong.  An explicitly
    ineligible scenario action (for example the B/C site's Communication action,
    because that explicit site only allows RID) is **not** an error: P16 already
    has its own eligibility gate, and the action stays visibly ineligible.
    """

    errors = []
    warnings = []
    checks = []

    def error(code, detail):
        errors.append({"code": code, "detail": detail})

    def warning(code, detail):
        warnings.append({"code": code, "detail": detail})

    if not isinstance(pool, dict) or pool.get("status") != "passed":
        error("pool_status", "R5 候选池 status 必须是 passed")
    boundary = (pool or {}).get("write_boundary") or {}
    _check(
        checks, "pool_write_boundary",
        boundary.get("writes_project_state") is False
        and boundary.get("writes_candidate_sites") is False,
        "候选池必须声明不写 ProjectState / candidate_sites",
    )
    contract = (pool or {}).get("assumption_contract") or {}
    _check(
        checks, "assumption_contract",
        contract.get("assumption_only") is True
        and contract.get("real_world_confirmed") is False
        and str(contract.get("mode") or "") == ENGINEERING_ASSUMPTION_MODE,
        "候选池必须是 engineering_assumption / assumption_only",
    )
    scenario = (pool or {}).get("scenario") or {}
    _check(
        checks, "scenario_identity",
        str(scenario.get("mode") or "") == ENGINEERING_ASSUMPTION_MODE
        and str(scenario.get("route_id") or "") == "R0005",
        "场景模式/航线必须是 engineering_assumption + R0005",
    )

    pool_counts = (pool or {}).get("counts") or {}
    dedupe_counts = (deduped or {}).get("counts") or {}
    _check(
        checks, "dedupe_reconciles_with_pool",
        int(pool_counts.get("canonical_site_count") or 0) == int(dedupe_counts.get("pool_identity_count") or -1)
        and int(pool_counts.get("unique_coordinate_count") or 0) == int(dedupe_counts.get("pool_physical_site_count") or -1)
        and len((pool or {}).get("actions") or []) == int(dedupe_counts.get("pool_action_count") or -1),
        "去重前后计数必须与候选池计数一致",
    )

    sites = (deduped or {}).get("sites") or []
    actions = (deduped or {}).get("actions") or []
    if not sites or not actions:
        error("empty_input", "去重后没有站点或动作")

    surface_counts = {}
    for site in sites:
        site_id = str(site.get("scenario_site_id") or "")
        surface = str(site.get("surface_class") or "")
        surface_counts[surface] = surface_counts.get(surface, 0) + 1
        if surface not in RETAINED_SURFACES:
            error("surface_class", f"{site_id} 的 surface_class={surface!r} 不是陆基假设保留类")
        if surface in SURFACE_REVIEW_REQUIRED:
            warning("surface_review_required", f"{site_id} 为 {surface}，仍需陆海复核")
        if site.get("formal_eligible") is True:
            error("formal_eligible", f"{site_id} 不得被升级为 formal_eligible")
        if site.get("buildable") is True and surface in SURFACE_REVIEW_REQUIRED:
            error("coastal_buildable", f"{site_id} 在 coastal_uncertain 下不得 buildable=true")
        marker = site.get("assumption") or {}
        if not (
            marker.get("assumption_only") is True
            and marker.get("real_world_confirmed") is False
            and str(marker.get("scenario_mode") or "") == ENGINEERING_ASSUMPTION_MODE
        ):
            error("site_assumption_marker", f"{site_id} 缺少工程假设标记")
        origin = site.get("installation_origin") or {}
        if origin.get("service_origin_egm2008_m") is None:
            error("site_origin", f"{site_id} 缺少安装原点")

    catalog = device_catalog_with_evidence(state) if isinstance(state, dict) else {"items": []}
    catalog_by_id = {
        str(item.get("device_id")): item for item in catalog.get("items") or []
        if isinstance(item, dict)
    }

    distinct_by_key = {}
    for action in actions:
        action_id = str(action.get("action_id") or "")
        site_id = str(action.get("site_id") or "")
        subsystem = str(action.get("subsystem") or "")
        device_id = str(action.get("device_id") or "")
        service_key = str(action.get("service_key") or "")
        if subsystem not in EXPECTED_DEVICE_BY_SUBSYSTEM:
            error("action_subsystem", f"{action_id} 的 subsystem={subsystem!r} 不在 C/S 假设内")
            continue
        if device_id != EXPECTED_DEVICE_BY_SUBSYSTEM[subsystem]:
            error(
                "action_device",
                f"{action_id} 的设备 {device_id!r} 不是场景登记的 C/S 工程型号 "
                f"{EXPECTED_DEVICE_BY_SUBSYSTEM[subsystem]!r}",
            )
        if service_key != EXPECTED_SERVICE_KEY[subsystem]:
            error(
                "action_service_key",
                f"{action_id} 的 service_key={service_key!r} 与 {subsystem} 不一致",
            )
        expected_contract = service_contract_for(subsystem, catalog_by_id.get(device_id) or {})
        if expected_contract.get("service_key") != service_key:
            error(
                "action_service_contract",
                f"{action_id} 的 service_key 与设备目录 canonical 合同不一致",
            )
        if action.get("confirmed") is not False:
            error("action_formal_confirmed", f"{action_id} 不得把场景动作标成 confirmed")
        if not (
            action.get(ASSUMPTION_ONLY_KEY) is True
            and action.get(REAL_WORLD_CONFIRMED_KEY) is False
        ):
            error("action_assumption_marker", f"{action_id} 缺少 assumption_only/real_world_confirmed 标记")
        marker = action.get("scenario") or {}
        if not (
            marker.get("assumption_only") is True
            and marker.get("real_world_confirmed") is False
            and str(marker.get("scenario_mode") or "") == ENGINEERING_ASSUMPTION_MODE
        ):
            error("action_scenario_marker", f"{action_id} 缺少场景工程假设标记")
        coordinate = _coord(action.get("coordinate"))
        if coordinate is None:
            error("action_coordinate", f"{action_id} 坐标无效")
        key = physical_site_key(action.get("physical_site_key"), coordinate)
        if key is None:
            error("action_physical_key", f"{action_id} 缺少 physical_site_key")
        else:
            existing = distinct_by_key.setdefault(tuple(key), set())
            existing.add(str(action.get("distinct_site_id") or ""))
        if str(action.get("distinct_site_id") or "") != f"site:{site_id}":
            error(
                "action_distinct_site",
                f"{action_id} 的 distinct_site_id 必须绑定归并后的物理站址 {site_id}",
            )
        vertical = action.get("vertical") or {}
        if vertical.get("confirmed") is not True:
            error("action_vertical_confirmed", f"{action_id} 的安装原点未在场景投影中确认")
        origin = vertical.get("service_origin_egm2008_m")
        if not isinstance(origin, (int, float)) or not isfinite(float(origin)):
            error("action_origin_numeric", f"{action_id} 缺少数值安装原点")
        if not (
            vertical.get(ASSUMPTION_ONLY_KEY) is True
            and vertical.get("projection_only") is True
            and vertical.get(REAL_WORLD_CONFIRMED_KEY) is False
        ):
            error("action_vertical_marker", f"{action_id} 的安装原点缺少工程假设/投影标记")
        profile = action.get("planning_profile") or {}
        if not (
            profile.get("confirmed") is True
            and profile.get("add_device_allowed") is True
            and profile.get(ASSUMPTION_ONLY_KEY) is True
            and profile.get("projection_only") is True
        ):
            error("action_planning_profile", f"{action_id} 的 planning_profile 不满足场景投影合同")
        eligibility = action.get("eligibility") or {}
        status = str(eligibility.get("status") or "")
        if status not in ("eligible", "ineligible"):
            error("action_eligibility_status", f"{action_id} 的 eligibility.status={status!r}")
        if bool(action.get("scenario_eligible")) != (status == "eligible"):
            error(
                "action_scenario_eligible_consistency",
                f"{action_id} 的 scenario_eligible 与 eligibility.status 不一致",
            )
        if status == "eligible":
            device = catalog_by_id.get(device_id) or {}
            geometry = device.get("coverage_geometry") or {}
            if geometry.get("confirmed") is not True or not geometry_usable(geometry):
                error(
                    "action_device_geometry",
                    f"{action_id} 的设备目录 coverage_geometry 不可用",
                )
        if str(action.get("reuse_class") or "") not in REUSE_TIERS:
            error("action_reuse_class", f"{action_id} 的 reuse_class 不在 REUSE_TIERS 内")

    for key, ids in distinct_by_key.items():
        if len(ids) != 1:
            error(
                "physical_key_distinct_site_inflation",
                f"physical_site_key={list(key)} 对应多个 distinct_site_id={sorted(ids)}",
            )

    policy = (state or {}).get("corridor_site_planning_policy") or {}
    cap = ((policy.get("parameters") or {}).get("max_selected_interventions"))
    _check(checks, "p16_cap_13", cap == 13, f"P16 干预上限必须保持 13，实际 {cap!r}")

    known_error_codes = {item["code"] for item in errors}
    for check in checks:
        if check["status"] == "failed" and check["code"] not in known_error_codes:
            error(check["code"], check["detail"])

    #: P16 pure-compute gate needs current P14/P15.  Stale/missing is a blocker;
    #: ``failed`` is a valid assessment (it means confirmed deficits), not a block.
    for container in ("cns_corridor_assessment", "cns_corridor_gap_assessment"):
        status = ((state or {}).get(container) or {}).get("status")
        if status in (None, "stale", "not_calculated", "missing_data"):
            error("p16_upstream_gate", f"{container}.status={status!r} 不能作为 P16 输入")

    result = {
        "schema_version": P16_ADAPTER_SCHEMA_VERSION,
        "status": "passed" if not errors else "blocked",
        "semantics": (
            "scenario candidate pool is contract-compatible with the unmodified P16 "
            "candidate-action input; no formal eligibility or cap is relaxed"
        ),
        "checks": checks,
        "errors": errors,
        "warnings": warnings,
        "counts": {
            "deduped_site_count": len(sites),
            "deduped_action_count": len(actions),
            "eligible_action_count": sum(
                1 for item in actions
                if (item.get("eligibility") or {}).get("status") == "eligible"
            ),
            "ineligible_action_count": sum(
                1 for item in actions
                if (item.get("eligibility") or {}).get("status") == "ineligible"
            ),
            "by_subsystem": {
                code: {
                    "actions": sum(1 for item in actions if item.get("subsystem") == code),
                    "eligible": sum(
                        1 for item in actions
                        if item.get("subsystem") == code
                        and (item.get("eligibility") or {}).get("status") == "eligible"
                    ),
                    "devices": sorted({
                        str(item.get("device_id")) for item in actions
                        if item.get("subsystem") == code
                    }),
                }
                for code in ("C", "S")
            },
            "surface_class_counts": surface_counts,
            "p16_cap": cap,
        },
    }
    return result


class ReadOnlyProjectSession:
    """Minimal session whose state is read but never written.

    P16's pure-compute ``plan()`` only reads ``session.state``.  Any accidental
    write attempt (``save``/attribute set on the state container) is a hard error
    so the R6 run cannot silently become a second ProjectState writer.
    """

    def __init__(self, state):
        self._state = state

    @property
    def state(self):
        return self._state

    def save(self):
        raise RuntimeError("R6 scenario adapter session is read-only: session.save() is forbidden")


class _NoWriteInvalidation:
    def cns_corridor_site_plan(self, *args, **kwargs):
        raise RuntimeError("R6 scenario adapter must not invalidate canonical ProjectState")

    def __getattr__(self, name):
        raise RuntimeError(
            f"R6 scenario adapter must not write ProjectState (attempted {name})"
        )


class ScenarioP16PlanningService(CorridorSitePlanningService):
    """P16 service with the candidate-action source replaced by scenario actions.

    Only ``_canonical_candidate_actions`` is overridden (the candidate *source*).
    The greedy loop, reuse tiers, rankers, objective evaluation, continuous
    service gate and cap are the unmodified production implementation.
    """

    def __init__(
        self, session, planner, corridor_model, corridor_gap_analyzer, *,
        actions, candidate_source_statistics,
    ):
        super().__init__(
            session, planner, corridor_model, corridor_gap_analyzer,
            _NoWriteInvalidation(), None,
        )
        self._scenario_actions = [deepcopy(item) for item in actions]
        self._scenario_source_statistics = deepcopy(candidate_source_statistics)
        self._last_rerun_gap = None
        self._rerun_count = 0

    def _canonical_candidate_actions(self, state, targets, baseline_gap):
        self._candidate_source_statistics = deepcopy(self._scenario_source_statistics)
        return [deepcopy(item) for item in self._scenario_actions], None

    def _rerun(self, *args, **kwargs):
        #: Observation only: keep the last P14/P15 gap so the adapter can report
        #: voxel-level status for the frozen R5 residual set.  The gap is not
        #: modified, and P16's own return value is passed through unchanged.
        corridor, gap = super()._rerun(*args, **kwargs)
        self._last_rerun_gap = gap
        self._rerun_count += 1
        return corridor, gap


def _scenario_source_statistics(deduped, validation):
    counts = deduped.get("counts") or {}
    return {
        "source_policy": "r5_engineering_assumption_candidate_pool_physical_site_deduped",
        "existing_cns_source_gate": "not_applicable_scenario_adapter",
        "shared_snapshot_status": "not_used_scenario_adapter",
        "allowed_source_counts": {
            "scenario_physical_sites": counts.get("deduped_site_count"),
            "scenario_actions": counts.get("deduped_action_count"),
            "scenario_eligible_actions": counts.get("eligible_deduped_action_count"),
        },
        "excluded_reason_counts": {},
        "physical_site_identity": deepcopy(deduped.get("counts") or {}),
        "surface_class_counts": deepcopy(
            (validation.get("counts") or {}).get("surface_class_counts") or {}
        ),
        "formal_candidate_sites_consumed": False,
        "non_scenario_candidate_sources_consumed": False,
    }


def run_scenario_p16(state, pool, *, on_progress=None, cancel_check=None):
    """Run the unmodified P16 pure-compute path once on scenario actions.

    ``state`` is used read-only.  The caller is responsible for loading the
    frozen rev523 copy and its cached grid artifacts; this function never calls
    ``session.save()``, never imports candidates, and never applies the result.
    """

    deduped = dedupe_candidate_pool_by_physical_site(pool)
    validation = validate_scenario_p16_inputs(pool, deduped, state)
    if validation["status"] != "passed":
        return {
            "status": "blocked",
            "semantics": "scenario_p16_preflight_blocked_no_search_executed",
            "dedupe": deduped["counts"],
            "contract_validation": validation,
            "result": None,
        }

    session = ReadOnlyProjectSession(state)
    service = ScenarioP16PlanningService(
        session,
        CorridorReuseFirstSitePlannerV2(),
        CNSServiceCorridorV1(),
        CNSCorridorGapAnalyzerV1(),
        actions=deduped["actions"],
        candidate_source_statistics=_scenario_source_statistics(deduped, validation),
    )
    outcome = service.plan(on_progress=on_progress, cancel_check=cancel_check)
    if outcome.get("missing_reason") is not None:
        return {
            "status": "missing_input",
            "semantics": "scenario_p16_upstream_gate_not_satisfied",
            "missing_reason": outcome.get("missing_reason"),
            "dedupe": deduped["counts"],
            "contract_validation": validation,
            "result": None,
        }

    result = outcome["result"]
    result["assumption_only"] = True
    result["real_world_confirmed"] = False
    result["not_formal_p16_plan"] = True
    result["formal_project_state_write"] = False
    result["formal_candidate_sites_write"] = False
    result["formal_p16_algorithm_modified"] = False
    result["engineering_assumption_adapter"] = {
        "schema_version": P16_ADAPTER_SCHEMA_VERSION,
        "assumption_only": True,
        "real_world_confirmed": False,
        "formal_project_state_write": False,
        "formal_candidate_sites_write": False,
        "formal_p16_algorithm_modified": False,
        "candidate_actions_from_scenario_pool_only": True,
        "dedupe": deepcopy(deduped["counts"]),
        "duplicate_groups": deepcopy(deduped["duplicate_groups"]),
        "contract_validation": deepcopy(validation),
        "physical_site_identity_sha256": deduped.get("identity_sha256"),
        "scenario_isolation": {
            "session": "read_only",
            "session_save_called": False,
            "project_state_written": False,
            "candidate_sites_written": False,
            "import_candidates_called": False,
            "canonical_p16_algorithm_modified": False,
        },
        "semantics": (
            "R6 engineering-assumption scenario P16 proposal; not a formal P16 plan, "
            "not a ProjectState/candidate_sites write, not a real-world confirmation"
        ),
    }
    return {
        "status": "computed",
        "semantics": "scenario_p16_proposal_computed_once_assumption_only",
        "dedupe": deduped["counts"],
        "duplicate_groups": deduped["duplicate_groups"],
        "contract_validation": validation,
        "result": result,
        "final_gap": service._last_rerun_gap,
        "rerun_count": service._rerun_count,
        "scenario_sites": deduped["sites"],
    }
