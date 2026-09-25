"""Generate per-altitude-layer three-state Planning Constraint Fields."""

from __future__ import annotations

from copy import deepcopy

from ..domain.building_clearance import building_height_status_is_resolved
from ..domain.planning_constraint_field import (
    normalize_constraint_cell, normalize_unknown_policy,
    planning_constraint_field_summary, stable_constraint_fingerprint,
    summarize_constraint_cells,
)
from ..domain.restricted_area import altitude_applicability, normalize_restricted_area_collection
from ..gis.planning_constraint_field_adapter import restricted_areas_by_cell, towers_by_cell


def generate_planning_constraint_field(
    *, altitude_layer, grid, terrain_by_cell, buildings_by_cell,
    tower_obstacle_profiles=None, restricted_areas=None, policies=None,
    source_fingerprints=None, workspace_identity=None,
):
    """Pure generation entry point; risk scores are deliberately not accepted."""

    layer = _altitude_layer(altitude_layer)
    grid_cells = [item for item in (grid or {}).get("cells") or [] if isinstance(item, dict)]
    if not grid_cells:
        raise ValueError("Planning Constraint Field 需要非空 grid.cells")
    policy = policies if isinstance(policies, dict) else {}
    unknown_policy = normalize_unknown_policy(policy.get("unknown_policy"))
    source_fingerprints = deepcopy(source_fingerprints or {})
    grid_crs = str((grid or {}).get("crs") or (grid or {}).get("geometry_crs") or "OGC:CRS84")
    tower_map = towers_by_cell(grid_cells, tower_obstacle_profiles or {})
    tower_state = str((tower_obstacle_profiles or {}).get("status") or "not_provided")
    restricted_states, restricted_items = _restricted_collection(restricted_areas)
    area_map = restricted_areas_by_cell(grid_cells, restricted_items, grid_crs=grid_crs)
    cells = []
    for grid_cell in grid_cells:
        grid_id = str(grid_cell.get("grid_id") or "")
        blocked_by, unknown_reasons, evidence_refs = [], [], []
        terrain_fact = _fact(terrain_by_cell, grid_id, "terrain")
        building_fact = deepcopy(_fact(buildings_by_cell, grid_id, "buildings"))
        if (
            building_fact.get("ground_elevation_max_egm2008_m") is None
            and building_fact.get("surface_elevation_max_egm2008_m") is None
        ):
            building_fact["ground_elevation_max_egm2008_m"] = terrain_fact.get(
                "surface_elevation_max_egm2008_m"
            )
        _terrain(
            layer, terrain_fact, policy,
            blocked_by, unknown_reasons, evidence_refs,
        )
        _building(
            layer, building_fact, policy,
            blocked_by, unknown_reasons, evidence_refs,
        )
        if tower_state != "passed":
            unknown_reasons.append({"domain": "tower", "reason": "tower_dataset_unresolved"})
        else:
            _tower(
                layer, tower_map.get(grid_id) or [], policy,
                blocked_by, unknown_reasons, evidence_refs,
            )
        for domain, reason in (
            ("airspace", "restricted_area_dataset_unresolved"),
            ("critical_site", "protected_site_dataset_unresolved"),
        ):
            if restricted_states.get(domain) not in ("confirmed_none", "confirmed_present"):
                unknown_reasons.append({"domain": domain, "reason": reason})
        for area in area_map.get(grid_id) or []:
            _restricted(
                layer, area, policy, blocked_by, unknown_reasons, evidence_refs,
            )
        cells.append(normalize_constraint_cell({
            "grid_id": grid_id,
            "outcome": "blocked" if blocked_by else "unknown" if unknown_reasons else "pass",
            "blocked_by": blocked_by,
            "unknown_reasons": unknown_reasons,
            "evidence_refs": evidence_refs,
        }))
    policy_view = {
        "terrain_vertical_clearance_m": policy.get("terrain_vertical_clearance_m"),
        "building_vertical_clearance_m": policy.get("building_vertical_clearance_m"),
        "tower_vertical_clearance_m": policy.get("tower_vertical_clearance_m"),
        "conditional_hard_exclusion_feature_ids": sorted(
            str(item) for item in policy.get("conditional_hard_exclusion_feature_ids") or []
        ),
        "unknown_policy": unknown_policy,
    }
    grid_identity = stable_constraint_fingerprint([
        {"grid_id": item.get("grid_id"), "bbox": item.get("bbox"), "level": item.get("level")}
        for item in sorted(grid_cells, key=lambda value: str(value.get("grid_id")))
    ], prefix="pcf-grid-")
    policy_fingerprint = stable_constraint_fingerprint(policy_view, prefix="pcf-policy-")
    components = {
        "altitude_layer_id": layer["altitude_layer_id"],
        "nominal_altitude_m": layer["nominal_altitude_m"],
        "vertical_reference": layer["vertical_reference"],
        "workspace_identity": deepcopy(workspace_identity),
        "grid_identity": grid_identity,
        "policy_fingerprint": policy_fingerprint,
        "source_fingerprints": source_fingerprints,
        "cells": cells,
    }
    fingerprint = stable_constraint_fingerprint(components)
    counts = summarize_constraint_cells(cells)
    warnings = []
    if counts["unknown"]:
        warnings.append({
            "reason": "unknown_constraints_remain",
            "unknown_constraint_count": counts["unknown"],
            "provisional_traversal_allowed": unknown_policy["allow_unknown_for_provisional"],
        })
    return {
        "schema_version": 1,
        "field_id": f"PCF-{layer['altitude_layer_id']}-{fingerprint[-16:]}",
        "status": "completed_with_warnings" if warnings else "completed",
        **layer,
        "workspace_identity": deepcopy(workspace_identity),
        "grid_identity": grid_identity,
        "policy_fingerprint": policy_fingerprint,
        "source_fingerprints": source_fingerprints,
        "constraint_field_fingerprint": fingerprint,
        "unknown_policy": unknown_policy,
        "counts": counts,
        "warnings": warnings,
        "artifact_ref": None,
        "cells": cells,
        "risk_field_used": False,
        "hard_constraints_are_not_risk_cost": True,
    }


class PlanningConstraintFieldService:
    def __init__(self, session, invalidation, snapshot):
        self.session = session
        self.invalidation = invalidation
        self.snapshot = snapshot

    def result_snapshot(self, altitude_layer_id=None):
        collection = self.session.state.get("planning_constraint_fields") or {}
        artifact = _artifact_ref(self.session.state)
        items = [
            planning_constraint_field_summary(item, artifact_ref=artifact)
            for item in collection.get("items") or []
            if not altitude_layer_id or item.get("altitude_layer_id") == altitude_layer_id
        ]
        return {
            "schema_version": 1, "collection_id": "planning_constraint_fields",
            "status": collection.get("status") or "not_calculated",
            "count": len(items), "items": items,
        }

    # ---- B4X：只读展示读取路径 ------------------------------------------------
    #
    # B3X 已把 cell 明细放进内容寻址 sidecar（``.cns-results/<sha256>.json.gz``），
    # 浏览器**不得**接触任何文件系统路径。这里因此提供一条最薄的、只读的读取路径：
    #
    #   * summary 走既有 ``result_snapshot()``（不含 cells，快照保持 slim）；
    #   * 地图只拿到 ``grid_id`` / ``outcome`` / ``blocked_by``；
    #   * 复用前端已有的 ``grid_id → geometry``（``/api/workspace/grid`` 的
    #     ``cell.bbox`` / ``cell.geometry``），因此**不重复下发 GeoJSON**；
    #   * 绝不返回 ``unknown_reasons`` 明细、``evidence_refs`` 或完整 raw artifact。
    #
    # 数据来源顺序：内存 state（打开项目时由 ``restore_compacted_results`` 从 sidecar
    # 水合）→ 项目 sidecar 文件。两者都拿不到时如实返回 ``cells_unavailable``，绝不编造。

    def field_map(self, altitude_layer_id=None, bbox=None):
        """地图友好的紧凑约束结果。只读；不产生任何失效或写入。"""

        layer_id = str(altitude_layer_id or "").strip()
        if not layer_id:
            return {
                "schema_version": 1, "status": "altitude_layer_required",
                "reason": "必须显式给出 altitude_layer_id",
                "altitude_layer_id": None, "counts": None, "cells": [],
            }
        field = self._field(layer_id)
        if field is None:
            return {
                "schema_version": 1, "status": "not_calculated",
                "reason": f"高度层 {layer_id} 尚无 Planning Constraint Field",
                "altitude_layer_id": layer_id, "counts": None, "cells": [],
            }
        cells = field.get("cells")
        if not isinstance(cells, list) or not cells:
            # 摘要存在但明细不可读：绝不把"读不到"当成"全部可通行"。
            return {
                "schema_version": 1, "status": "cells_unavailable",
                "reason": "约束场明细尚未水合，且项目结果 sidecar 不可读",
                "altitude_layer_id": layer_id,
                "field_id": field.get("field_id"),
                "counts": deepcopy(field.get("counts")),
                "cells": [],
            }
        box = _normalize_bbox(bbox)
        selected = [item for item in cells if _cell_in_bbox(item, box)] if box else cells
        counts = summarize_constraint_cells(selected)
        return {
            "schema_version": 1,
            "status": "passed",
            "altitude_layer_id": layer_id,
            "field_id": field.get("field_id"),
            "nominal_altitude_m": field.get("nominal_altitude_m"),
            "vertical_reference": field.get("vertical_reference"),
            "grid_identity": field.get("grid_identity"),
            "constraint_field_fingerprint": field.get("constraint_field_fingerprint"),
            "bbox": box,
            "geometry_source": "frontend_grid_index",
            "counts": counts,
            "total_count": len(cells),
            "truncated": False,
            "cells": [
                {
                    "grid_id": str(item.get("grid_id") or ""),
                    "outcome": str(item.get("outcome") or "unknown"),
                    "blocked_by": list(item.get("blocked_by") or []),
                }
                for item in selected
            ],
        }

    def _field(self, altitude_layer_id):
        """按 altitude_layer_id 找约束场；内存优先，其次项目 sidecar。"""

        collection = self.session.state.get("planning_constraint_fields") or {}
        for item in collection.get("items") or []:
            if isinstance(item, dict) and str(item.get("altitude_layer_id") or "") == altitude_layer_id:
                return item
        sidecar = self._sidecar_fields()
        for item in sidecar:
            if str(item.get("altitude_layer_id") or "") == altitude_layer_id:
                return item
        return None

    def _sidecar_fields(self):
        """从项目结果 sidecar 读取约束场（只读；失败时返回空列表，不抛异常）。"""

        state = self.session.state
        store_path = getattr(self.session, "store_path", None)
        if not store_path:
            return []
        collection = state.get("planning_constraint_fields") or {}
        if not (collection.get("items") or []):
            return []
        try:
            from ..persistence.project_compaction import read_result_artifact

            payload = read_result_artifact(state, store_path)
        except Exception:  # noqa: BLE001 - 损坏 / 缺失 / 指纹不符都按"明细不可用"处理
            return []
        cell_payload = payload.get("planning_constraint_field_cells") or {}
        return [
            {**item, "cells": cell_payload.get(str(item.get("field_id"))) or []}
            for item in collection.get("items") or []
            if isinstance(item, dict)
        ]

    def generate(self, payload=None):
        payload = payload if isinstance(payload, dict) else {}
        state = self.session.state
        layer = _find_layer(state, payload.get("altitude_layer_id"))
        attributes = state.get("grid_attributes") or {}
        feasibility = state.get("layered_route_feasibility_policy") or {}
        building_policy = state.get("building_clearance_policy") or {}
        tower_policy = state.get("tower_clearance_policy") or {}
        supplied_policy = payload.get("policies") or {}
        effective_policy = {
            "terrain_vertical_clearance_m": feasibility.get("terrain_vertical_clearance_m"),
            "building_vertical_clearance_m": building_policy.get("vertical_clearance_m"),
            "tower_vertical_clearance_m": tower_policy.get("tower_vertical_clearance_m"),
            **deepcopy(supplied_policy),
        }
        field = generate_planning_constraint_field(
            altitude_layer=layer,
            grid=payload.get("grid") or state.get("grid") or {},
            terrain_by_cell=payload.get("terrain_by_cell") if "terrain_by_cell" in payload else (
                (attributes.get("terrain") or {}).get("cells") or {}
            ),
            buildings_by_cell=payload.get("buildings_by_cell") if "buildings_by_cell" in payload else (
                (attributes.get("buildings") or {}).get("cells") or {}
            ),
            tower_obstacle_profiles=(
                payload.get("tower_obstacle_profiles")
                if "tower_obstacle_profiles" in payload else state.get("tower_obstacle_profiles")
            ),
            restricted_areas=(
                payload.get("restricted_areas")
                if "restricted_areas" in payload else state.get("restricted_areas")
            ),
            policies=effective_policy,
            source_fingerprints=payload.get("source_fingerprints") or _source_fingerprints(state),
            workspace_identity=payload.get("workspace_identity") or _workspace_identity(state),
        )
        collection = state.setdefault("planning_constraint_fields", {
            "schema_version": 1, "status": "not_calculated",
            "collection_id": "planning_constraint_fields", "count": 0, "items": [],
        })
        previous = next((
            item for item in collection.get("items") or []
            if item.get("altitude_layer_id") == field["altitude_layer_id"]
        ), None)
        collection["items"] = [
            item for item in collection.get("items") or []
            if item.get("altitude_layer_id") != field["altitude_layer_id"]
        ] + [field]
        collection["count"] = len(collection["items"])
        collection["status"] = "passed"
        state.setdefault("result_statuses", {})["planning_constraint_fields"] = "passed"
        if previous is None or previous.get("constraint_field_fingerprint") != field["constraint_field_fingerprint"]:
            self.invalidation.layered_route("planning_constraint_field_changed")
        self.session.save()
        return self.result_snapshot(field["altitude_layer_id"])


def _terrain(layer, fact, policy, blocked, unknown, refs):
    clearance = _number(policy.get("terrain_vertical_clearance_m"))
    elevation = _number((fact or {}).get("surface_elevation_max_egm2008_m"))
    status = str((fact or {}).get("data_status") or (fact or {}).get("status") or "")
    reference = str((fact or {}).get("vertical_reference") or layer["vertical_reference"])
    if clearance is None or status not in ("passed", "resolved") or elevation is None or reference != layer["vertical_reference"]:
        unknown.append({"domain": "terrain", "reason": "terrain_evidence_or_clearance_unresolved"})
        return
    refs.append({"domain": "terrain", "source": (fact or {}).get("source")})
    if layer["nominal_altitude_m"] < elevation + clearance:
        blocked.append("terrain")


def _building(layer, fact, policy, blocked, unknown, refs):
    item = fact or {}
    count = item.get("building_count")
    if _number(count) == 0:
        return
    clearance = _number(policy.get("building_vertical_clearance_m"))
    ground = _number(item.get("ground_elevation_max_egm2008_m"))
    if ground is None:
        ground = _number(item.get("surface_elevation_max_egm2008_m"))
    height = _number(item.get("height_m"))
    if height is None:
        height = _number(item.get("height_max_m"))
    height_status = item.get("height_status")
    if height_status is None and _number(item.get("valid_height_fraction")) == 1.0:
        height_status = "valid"
    status = str(item.get("data_status") or item.get("status") or "")
    if (
        clearance is None or status not in ("passed", "resolved") or ground is None
        or height is None or not building_height_status_is_resolved(height_status)
    ):
        unknown.append({"domain": "building", "reason": "building_ground_height_or_status_unresolved"})
        return
    refs.append({"domain": "building", "source": item.get("source")})
    if layer["nominal_altitude_m"] < ground + height + clearance:
        blocked.append("building")


def _tower(layer, profiles, policy, blocked, unknown, refs):
    if not profiles:
        return
    clearance = _number(policy.get("tower_vertical_clearance_m"))
    if clearance is None:
        unknown.append({"domain": "tower", "reason": "tower_clearance_unresolved"})
        return
    for profile in profiles:
        top = _number(profile.get("tower_top_orthometric_m"))
        if profile.get("confirmed") is not True or top is None:
            unknown.append({
                "domain": "tower", "reason": "tower_top_not_confirmed",
                "tower_id": profile.get("tower_id"),
            })
            continue
        refs.append({"domain": "tower", "tower_id": profile.get("tower_id")})
        if layer["nominal_altitude_m"] < top + clearance:
            blocked.append("tower")


def _restricted(layer, area, policy, blocked, unknown, refs):
    domain = "airspace" if str(area.get("domain") or "") == "airspace" else "critical_site"
    if area.get("spatial_status") != "intersects" or area.get("geometry_status") != "resolved":
        unknown.append({"domain": domain, "reason": area.get("spatial_status") or area.get("geometry_status"), "feature_id": area.get("feature_id")})
        return
    if area.get("confirmed") is not True:
        unknown.append({"domain": domain, "reason": "restricted_area_unconfirmed", "feature_id": area.get("feature_id")})
        return
    applicability = altitude_applicability(
        area, altitude_m=layer["nominal_altitude_m"],
        vertical_reference=layer["vertical_reference"],
    )
    if applicability == "not_applicable":
        return
    if applicability != "applicable":
        unknown.append({"domain": domain, "reason": "restricted_area_vertical_scope_unresolved", "feature_id": area.get("feature_id")})
        return
    constraint_type = area.get("constraint_type")
    if constraint_type == "advisory":
        refs.append({"domain": domain, "feature_id": area.get("feature_id"), "advisory": True})
        return
    if constraint_type == "conditional" and str(area.get("feature_id")) not in {
        str(item) for item in policy.get("conditional_hard_exclusion_feature_ids") or []
    }:
        unknown.append({"domain": domain, "reason": "conditional_policy_unresolved", "feature_id": area.get("feature_id")})
        return
    blocked.append(domain)
    refs.append({"domain": domain, "feature_id": area.get("feature_id")})


def _fact(mapping, grid_id, nested_key):
    if isinstance(mapping, dict):
        item = mapping.get(grid_id)
        if isinstance(item, dict) and isinstance(item.get(nested_key), dict):
            return item[nested_key]
        return item if isinstance(item, dict) else {}
    return {}


def _restricted_collection(value):
    if isinstance(value, list):
        value = {"items": value}
    collection = normalize_restricted_area_collection(value)
    return {
        domain: (collection.get("domain_states") or {}).get(domain, {}).get("status")
        for domain in ("airspace", "critical_site")
    }, collection.get("items") or []


def _altitude_layer(value):
    raw = value if isinstance(value, dict) else {}
    identifier = str(raw.get("altitude_layer_id") or "").strip()
    altitude = _number(raw.get("nominal_altitude_m"))
    reference = str(raw.get("vertical_reference") or "").strip()
    if not identifier or altitude is None or not reference:
        raise ValueError("Constraint Field 需要明确 altitude_layer_id/nominal_altitude_m/vertical_reference")
    if raw.get("confirmed") is False or str(raw.get("status") or "confirmed") not in ("confirmed", "passed"):
        raise ValueError("Constraint Field 只接受已确认 AltitudeLayer")
    return {
        "altitude_layer_id": identifier,
        "nominal_altitude_m": altitude,
        "vertical_reference": reference,
    }


def _find_layer(state, altitude_layer_id):
    identifier = str(altitude_layer_id or "").strip()
    for layer in ((state.get("spatial_3d") or {}).get("altitude_layers") or []):
        if str(layer.get("altitude_layer_id") or "") == identifier:
            return layer
    raise ValueError(f"AltitudeLayer 不存在：{identifier}")


def _workspace_identity(state):
    workspace = state.get("workspace") or {}
    return {
        "workspace_id": workspace.get("workspace_id"),
        "revision": workspace.get("revision"),
        "bbox": deepcopy(workspace.get("bbox")),
    }


def _source_fingerprints(state):
    audits = ((state.get("source_audits") or {}).get("items") or {})
    return {
        role: deepcopy(audits.get(role) or {})
        for role in ("terrain_dtm", "buildings", "towers", "restricted_areas")
    }


def _artifact_ref(state):
    index = state.get("result_index") or {}
    details = index.get("planning_constraint_fields") or {}
    if not index.get("artifact"):
        return None
    return {"artifact": index.get("artifact"), "sha256": index.get("sha256"), **details}


def _normalize_bbox(value):
    """把请求里的 bbox 规范成 ``[west, south, east, north]``；非法输入返回 None。

    只接受 4 个有限数值；``west > east`` 视为跨反经线以外的非法输入（本产品工作区
    从不跨越 180°，因此不做环绕推断，直接忽略该 bbox 而不是猜一个范围）。
    """

    if value in (None, ""):
        return None
    raw = value
    if isinstance(raw, str):
        raw = [item for item in raw.split(",")]
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    numbers = []
    for item in raw:
        number = _number(item)
        if number is None:
            return None
        numbers.append(number)
    west, south, east, north = numbers
    if west > east or south > north:
        return None
    return numbers


def _cell_in_bbox(cell, bbox):
    """cell 的 bbox 是否与请求 bbox 相交（半开包含，与前端命中语义一致）。"""

    raw = (cell or {}).get("bbox")
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        # 没有 bbox 的 cell 无法做视口过滤：保留它（宁可多返回一条紧凑记录，
        # 也绝不因为缺少几何就把一个 blocked cell 从结果中丢掉）。
        return True
    west, south, east, north = (_number(item) for item in raw)
    if None in (west, south, east, north):
        return True
    return not (
        east <= bbox[0] or west >= bbox[2] or north <= bbox[1] or south >= bbox[3]
    )


def _number(value):
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


__all__ = [
    "PlanningConstraintFieldService", "generate_planning_constraint_field",
]
