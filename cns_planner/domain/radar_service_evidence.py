"""Bridge canonical Radar layout results into the CNS service chain.

No geometry is implemented here.  Evidence is adapted from the Radar
validation result, and hypothetical panels are evaluated only by the existing
``actual_site_coverage`` canonical implementation.
"""

from __future__ import annotations

from copy import deepcopy
from math import isfinite

from ..algorithms.radar_layout.v1 import actual_site_coverage, required_count_for
from ..gis.radar_layout_adapter import radar_metric_coordinate
from .cns_service_contract import SERVICE_KEY_RADAR_NONCOOPERATIVE
from .cns_service_registry import planner_family_for, service_requirement_for
from .radar_surveillance_layout import (
    METRIC_CRS, radar_geometry_parameters, route_sample_height_semantics,
)
from .route_safety_evidence_v2 import stable_fingerprint
from .site_planning import TOWER_COLOCATION_REUSE_CLASS


SERVICE_KEY = SERVICE_KEY_RADAR_NONCOOPERATIVE
CURRENT_LAYOUT_STATUSES = {"proposal_ready", "infeasible", "refinement_incomplete"}
ALTITUDE_COMPARISON_TOLERANCE_M = 1e-6

#: ``S:radar_noncooperative`` 的**垂向适用范围**（Round 29-N 正式裁定）。
#:
#: 水平服务范围 = corridor；垂向适用范围 = ``operational_route_altitude_layer``，
#: 即 canonical Radar layout 已解析出的那一层（``altitude_layer_id`` +
#: ``model_supported_altitude_egm2008_m``）。Radar 是**固定巡航高度层**模型，
#: 它只在该层内有意义；离开该层的走廊体元**既不是 satisfied、也不是
#: confirmed_deficit，更不是 unknown** —— 它们根本不属于该模型的垂向适用范围。
RADAR_VERTICAL_SCOPE_OPERATIONAL_ALTITUDE_LAYER = "operational_route_altitude_layer"

#: off-layer 体元的 canonical 原因码（显式 ``not_applicable``，不是缺口也不是证据不足）。
RADAR_NOT_APPLICABLE_REASON = (
    "radar_not_applicable_outside_operational_altitude_layer"
)


def radar_required_for(required_cns, route_id=None):
    requirements = (
        ((required_cns or {}).get("route_overrides") or {}).get(str(route_id))
        or (required_cns or {}).get("project_default") or {}
    )
    surveillance = requirements.get("surveillance") or {}
    return service_requirement_for("S", surveillance, SERVICE_KEY) is not None


def build_radar_service_evidence(required_cns, layout, *, route_ids=None):
    """Build route/sample evidence only for explicitly required Radar service."""

    routes = []
    layout = layout if isinstance(layout, dict) else {}
    for item in layout.get("items") or []:
        if not isinstance(item, dict):
            continue
        route_id = str(item.get("route_id") or "")
        if not route_id or not radar_required_for(required_cns, route_id):
            continue
        routes.append(_route_evidence(item))
    if not routes:
        # Explicit requirements with no matching layout still need unknown evidence.
        explicit_ids = _explicit_route_ids(required_cns)
        candidates = explicit_ids or [
            str(route_id) for route_id in (route_ids or [])
            if radar_required_for(required_cns, route_id)
        ]
        routes = [_missing_route(route_id, layout) for route_id in candidates]
    if not routes:
        return None
    result = {
        "service_key": SERVICE_KEY,
        "subsystem": "S",
        "service_subtype": "noncooperative_surveillance",
        "target_cooperation": "non_cooperative",
        "technology": "radar",
        "planner_family": planner_family_for(SERVICE_KEY),
        "routes": routes,
        "source_layout_fingerprint": stable_fingerprint(layout, prefix="radarlayout-"),
        "algorithm_provenance": {
            "algorithm_id": layout.get("algorithm_id"),
            "algorithm_version": layout.get("algorithm_version"),
            "model_scope": layout.get("model_scope"),
        },
    }
    result["input_fingerprint"] = stable_fingerprint(result, prefix="radarservice-")
    return result


def evidence_for_probe(evidence, route_id, probe, *, metric_projector=None):
    """Evaluate one P14 voxel probe with the selected canonical Radar layout.

    Round 29-N 裁定 —— Radar 是**固定巡航高度层**服务：

    * 水平适用范围 = ``corridor``；
    * 垂向适用范围 = ``operational_route_altitude_layer``（canonical layout 的
      ``altitude_layer_id`` / ``model_supported_altitude_egm2008_m``）。

    因此本函数**绝不**用 P14 generic voxel probe 的 vertical-overlap midpoint 去判定
    Radar 适用性。该 midpoint 服务的是 C / RID / legacy 几何覆盖与体积代理语义
    （``corridor/v1.py`` 的 ``voxel["probe"]`` 保持原样），与 Radar 的 fixed-cruise
    高度无关：例如真实项目 R0005 的 ALT-100 preview midpoint 是 107.5 m，而 canonical
    Radar model altitude 是 100.0 m。把 generic midpoint 当作 Radar 评估高度，会连
    合法的 ALT-100 voxel 都误判成 ``radar_model_scope_altitude_not_supported``。

    dispatch 规则（严格按 ``altitude_layer_id``，绝不按高度数值近似）：

    1. route evidence 非 current、或 canonical altitude metadata 缺失/冲突
       → ``unknown`` / evidence_required（**不得**推导 not_applicable）；
    2. ``probe.altitude_layer_id`` != canonical radar ``altitude_layer_id``
       → ``not_applicable`` / ``applicable = false``（不调用 ``actual_site_coverage``）；
    3. 同层时先验证 canonical model altitude 确实落在该 voxel 的
       ``overlap_height_egm2008_m`` 内（极小浮点容差）；不在 → ``unknown``；
       在 → 用 **Radar-specific** 采样高度 ``model_supported_altitude_egm2008_m``
       调用 canonical ``actual_site_coverage``（绝不用 generic midpoint）。

    路由偏移只作为审计上下文保留；覆盖结论始终由本 probe 自己的坐标、Radar 评估
    高度与 surface class 经 :func:`actual_site_coverage` 重新得出。
    """

    route = next(
        (item for item in (evidence or {}).get("routes") or []
         if str(item.get("route_id")) == str(route_id)),
        None,
    )
    if route is None:
        return None
    probe = probe if isinstance(probe, dict) else {}
    voxel_id = probe.get("voxel_id")
    coordinate = [probe.get("longitude"), probe.get("latitude")]
    generic_altitude = _finite_float(probe.get("altitude_egm2008_m"))
    voxel_layer_id = _nonempty_string(probe.get("altitude_layer_id"))
    model_altitude = _finite_float(route.get("model_supported_altitude_egm2008_m"))
    canonical_layer_id = _nonempty_string(route.get("altitude_layer_id"))
    if route.get("status") != "current" or model_altitude is None or canonical_layer_id is None:
        return _unknown_service_entry(
            route_id, voxel_id, evidence,
            route.get("reason") or "radar_layout_altitude_evidence_required",
            probe=probe, route=route,
        )
    if voxel_layer_id is None:
        return _unknown_service_entry(
            route_id, voxel_id, evidence, "radar_voxel_altitude_layer_evidence_required",
            probe=probe, route=route,
        )
    if voxel_layer_id != canonical_layer_id:
        return _not_applicable_service_entry(
            route_id, voxel_id, evidence, probe=probe, route=route,
            voxel_layer_id=voxel_layer_id,
        )
    overlap = _overlap_bounds(probe.get("overlap_height_egm2008_m"))
    if overlap is None:
        return _unknown_service_entry(
            route_id, voxel_id, evidence, "radar_voxel_vertical_overlap_evidence_required",
            probe=probe, route=route,
        )
    if not (
        overlap[0] - ALTITUDE_COMPARISON_TOLERANCE_M
        <= model_altitude
        <= overlap[1] + ALTITUDE_COMPARISON_TOLERANCE_M
    ):
        return _unknown_service_entry(
            route_id, voxel_id, evidence,
            "radar_model_altitude_outside_voxel_vertical_overlap",
            probe=probe, route=route,
        )
    inputs = route.get("service_evidence_inputs") or {}
    panels = inputs.get("selected_panels") or []
    towers = inputs.get("tower_records") or []
    if not towers:
        return _unknown_service_entry(
            route_id, voxel_id, evidence, "radar_layout_evidence_required",
            probe=probe, route=route,
        )
    projector = metric_projector or radar_metric_coordinate
    try:
        metric = projector(coordinate[0], coordinate[1], metric_crs=METRIC_CRS)
    except TypeError:
        # A narrow injection seam used by deterministic unit tests; production
        # always uses the public Radar GIS projection above.
        metric = projector(coordinate[0], coordinate[1])
    except Exception:
        metric = None
    if not isinstance(metric, (list, tuple)) or len(metric) != 2:
        return _unknown_service_entry(
            route_id, voxel_id, evidence, "radar_probe_metric_projection_unavailable",
            probe=probe,
        )
    surface_class = str(probe.get("surface_class") or "unknown")
    sample = {
        "sample_index": 0,
        "sample_id": f"voxel:{voxel_id}" if voxel_id else "voxel:unknown",
        "distance_along_route_m": probe.get("nearest_route_offset_m"),
        "metric": [float(metric[0]), float(metric[1])],
        "longitude": coordinate[0],
        "latitude": coordinate[1],
        #: Radar-specific 评估高度：canonical model supported altitude，
        #: 绝不是 generic voxel midpoint。
        "egm2008_m": model_altitude,
        "surface_class": surface_class,
        "required_distinct_site_count": required_count_for(surface_class),
        "refinement": False,
    }
    coverage = actual_site_coverage(
        panels=panels,
        samples=[sample],
        selected_panel_ids=[item.get("panel_id") for item in panels],
        tower_records=towers,
    )[0]
    result = _sample_evidence(str(route_id), coverage, route)
    result["voxel_id"] = voxel_id
    result["voxel_altitude_layer_id"] = voxel_layer_id
    result["altitude_layer_id"] = route.get("altitude_layer_id")
    result["vertical_scope"] = RADAR_VERTICAL_SCOPE_OPERATIONAL_ALTITUDE_LAYER
    result["applicable"] = True
    result["corridor_voxel_representative_altitude_egm2008_m"] = generic_altitude
    result["radar_evaluation_altitude_egm2008_m"] = model_altitude
    result["model_supported_altitude_egm2008_m"] = model_altitude
    result["vertical_overlap_height_egm2008_m"] = overlap
    result["route_sample_height_semantics"] = route.get("route_sample_height_semantics")
    result["nearest_route_offset_m"] = probe.get("nearest_route_offset_m")
    return result


def radar_candidate_actions(targets, layout):
    """Create directional Radar actions from canonical candidate panels only."""

    route_targets = {
        str(item.get("route_id")) for item in targets
        if item.get("service_key") == SERVICE_KEY
    }
    actions = []
    for item in (layout or {}).get("items") or []:
        route_id = str(item.get("route_id") or "")
        if route_id not in route_targets:
            continue
        inputs = item.get("service_evidence_inputs") or {}
        selected_ids = {str(panel.get("panel_id")) for panel in inputs.get("selected_panels") or []}
        towers = {str(tower.get("tower_id")): tower for tower in inputs.get("tower_records") or []}
        for panel in inputs.get("candidate_panels") or []:
            panel_id = str(panel.get("panel_id") or "")
            if not panel_id or panel_id in selected_ids:
                continue
            tower_id = str(panel.get("tower_id") or "")
            tower = towers.get(tower_id) or {}
            geometry = radar_geometry_parameters(panel.get("radar_type"))
            actions.append({
                "action_id": f"radar:{route_id}:{panel_id}",
                "action_type": "add_radar_panel",
                "route_id": route_id,
                "subsystem": "S",
                "service_key": SERVICE_KEY,
                "planner_family": planner_family_for(SERVICE_KEY),
                "reuse_class": TOWER_COLOCATION_REUSE_CLASS,
                "site_id": f"tower:{tower_id}",
                "tower_id": tower_id,
                "distinct_site_id": f"tower:{tower_id}",
                "radar_type": panel.get("radar_type"),
                "panel": {
                    "panel_id": panel_id,
                    "azimuth_deg": panel.get("azimuth_deg"),
                    "beamwidth_deg": 2.0 * float(panel.get("panel_half_width_deg") or 45.0),
                    "elevation_center_deg": geometry.get("elevation_center_deg"),
                    "elevation_min_deg": geometry.get("elevation_min_deg"),
                    "elevation_max_deg": geometry.get("elevation_max_deg"),
                },
                "canonical_panel": deepcopy(panel),
                "eligibility": {"status": "eligible", "reasons": []},
                "confirmed": True,
                "provenance": {
                    "algorithm_id": item.get("algorithm_id"),
                    "algorithm_version": item.get("algorithm_version"),
                    "source_layout_fingerprint": item.get("input_fingerprint"),
                },
            })
    return sorted(actions, key=lambda item: item["action_id"])


def radar_what_if_service_evidence(required_cns, layout, actions):
    """Re-evaluate added Radar panels through canonical Radar geometry."""

    working = deepcopy(layout or {})
    actions_by_route = {}
    for action in actions or []:
        if action.get("service_key") == SERVICE_KEY:
            actions_by_route.setdefault(str(action.get("route_id") or ""), []).append(action)
    for item in working.get("items") or []:
        route_id = str(item.get("route_id") or "")
        route_actions = actions_by_route.get(route_id) or []
        if not route_actions:
            continue
        inputs = item.get("service_evidence_inputs") or {}
        samples = inputs.get("samples") or []
        towers = inputs.get("tower_records") or []
        panels = list(inputs.get("selected_panels") or [])
        panels.extend(deepcopy(action["canonical_panel"]) for action in route_actions)
        inputs["selected_panels"] = deepcopy(panels)
        item["service_evidence_inputs"] = inputs
        selected_ids = [panel.get("panel_id") for panel in panels]
        if not samples or not towers:
            item["status"] = "unresolved"
            item["validation"] = None
            continue
        coverage = actual_site_coverage(
            panels=panels, samples=samples, selected_panel_ids=selected_ids,
            tower_records=towers,
        )
        item["status"] = "proposal_ready"
        item["validation"] = {"samples": coverage, "validated": all(
            sample.get("status") == "satisfied" for sample in coverage
        )}
        item["selected_panels"] = panels
        item["selected_tower_ids"] = sorted({str(panel.get("tower_id")) for panel in panels})
        item["input_fingerprint"] = stable_fingerprint(
            {"baseline": item.get("input_fingerprint"), "actions": route_actions},
            prefix="radarwhatif-",
        )
    return build_radar_service_evidence(required_cns, working)


def _route_evidence(item):
    route_id = str(item.get("route_id") or "")
    altitude, altitude_error = _resolved_layout_altitude(item)
    if altitude_error:
        return _missing_route(route_id, item, reason=altitude_error)
    current = item.get("status") in CURRENT_LAYOUT_STATUSES
    validation = item.get("validation") if isinstance(item.get("validation"), dict) else {}
    samples = validation.get("samples") or []
    if not samples:
        #: Round 29-N：项目持久化的 external-scope 裁剪会移除
        #: ``items[].validation.samples``（见 ``project_compaction`` 的
        #: ``radar.layout.detail``）。layout **自己**仍然保留同一次评估产生的 canonical
        #: 路线样本副本（``service_evidence_inputs.samples``）。Radar service evidence 的
        #: currentness 必须消费仍然存在的 canonical 样本，否则真实项目上 Radar 会永久停在
        #: ``evidence_required``：连 ALT-100 都无法评估，off-layer 更永远得不到
        #: ``not_applicable``。这里只做**等价回退**——绝不合成、绝不插值任何样本。
        inputs = item.get("service_evidence_inputs")
        if isinstance(inputs, dict):
            samples = inputs.get("samples") or []
    if not current or not samples:
        return _missing_route(route_id, item, altitude=altitude)
    result = {
        "route_id": route_id,
        "status": "current",
        **altitude,
        "samples": [_sample_evidence(route_id, sample, item) for sample in samples],
        "service_evidence_inputs": deepcopy(item.get("service_evidence_inputs") or {}),
        "source_layout_fingerprint": item.get("input_fingerprint"),
        "algorithm_id": item.get("algorithm_id"),
        "algorithm_version": item.get("algorithm_version"),
    }
    return result


def _sample_evidence(route_id, sample, item):
    tower_ids = sorted({str(panel.get("tower_id")) for panel in sample.get("panels") or [] if panel.get("tower_id")})
    status = {
        "satisfied": "satisfied",
        "under_redundant": "confirmed_deficit",
        "uncovered": "confirmed_deficit",
        "unknown": "unknown",
    }.get(str(sample.get("status") or "unknown"), "unknown")
    providers = []
    beamwidth = (((item.get("parameters") or {}).get("azimuth_preset") or {}).get(
        "azimuth_beamwidth_deg"
    ))
    for panel in sample.get("panels") or []:
        tower_id = str(panel.get("tower_id") or "")
        providers.append({
            "distinct_site_id": f"tower:{tower_id}" if tower_id else None,
            "tower_id": tower_id or None,
            "radar_type": panel.get("radar_type"),
            "panel_id": panel.get("panel_id"),
            "azimuth_deg": panel.get("azimuth_deg"),
            "beamwidth_deg": beamwidth,
            "elevation_deg": panel.get("elevation_deg"),
            "slant_distance_m": panel.get("slant_distance_m"),
            "coverage_evidence": "radar_actual_site_coverage",
        })
    result = {
        "route_id": route_id,
        "sample_id": sample.get("sample_id"),
        "coordinate": [sample.get("longitude"), sample.get("latitude")],
        "distance_along_route_m": sample.get("distance_along_route_m"),
        "surface_class": sample.get("surface_class") or "unknown",
        "service_key": SERVICE_KEY,
        "subsystem": "S",
        "service_subtype": "noncooperative_surveillance",
        "target_cooperation": "non_cooperative",
        "technology": "radar",
        "planner_family": planner_family_for(SERVICE_KEY),
        "surface_dependent": False,
        "supports_site_planning": True,
        "required_distinct_site_count": sample.get("required_distinct_site_count"),
        "covered_distinct_site_ids": [f"tower:{tower_id}" for tower_id in tower_ids],
        "distinct_site_ids": [f"tower:{tower_id}" for tower_id in tower_ids],
        "distinct_site_count": len(tower_ids),
        "counting_basis": "distinct_site_id",
        "status": status,
        "providers": providers,
        "reasons": [] if status != "unknown" else ["radar_surface_or_layout_evidence_unknown"],
        "source_layout_fingerprint": item.get("input_fingerprint"),
        "algorithm_provenance": {
            "algorithm_id": item.get("algorithm_id"),
            "algorithm_version": item.get("algorithm_version"),
        },
    }
    result["input_fingerprint"] = stable_fingerprint(result, prefix="radarsample-")
    return result


def _missing_route(route_id, source, *, altitude=None, reason=None):
    altitude = altitude if isinstance(altitude, dict) else {
        "altitude_layer_id": None,
        "model_supported_altitude_egm2008_m": None,
        "route_sample_height_semantics": None,
    }
    return {
        "route_id": str(route_id), "status": "evidence_required", "samples": [],
        **altitude,
        "reason": reason or f"radar_layout_{(source or {}).get('status') or 'missing'}",
        "source_layout_fingerprint": (source or {}).get("input_fingerprint"),
    }


def _unknown_service_entry(route_id, voxel_id, evidence, reason, *, probe=None, route=None):
    probe = probe if isinstance(probe, dict) else {}
    route = route if isinstance(route, dict) else {}
    result = {
        "route_id": route_id, "voxel_id": voxel_id, "sample_id": None,
        "coordinate": [probe.get("longitude"), probe.get("latitude")],
        "surface_class": probe.get("surface_class") or "unknown", "service_key": SERVICE_KEY,
        "subsystem": "S", "service_subtype": "noncooperative_surveillance",
        "target_cooperation": "non_cooperative", "technology": "radar",
        "planner_family": planner_family_for(SERVICE_KEY), "surface_dependent": False,
        "supports_site_planning": True, "required_distinct_site_count": None,
        "covered_distinct_site_ids": [], "distinct_site_ids": [],
        "distinct_site_count": None, "counting_basis": "distinct_site_id",
        "status": "unknown", "applicable": None,
        "vertical_scope": RADAR_VERTICAL_SCOPE_OPERATIONAL_ALTITUDE_LAYER,
        "providers": [], "reasons": [reason],
        "input_fingerprint": (evidence or {}).get("input_fingerprint"),
        "voxel_altitude_layer_id": _nonempty_string(probe.get("altitude_layer_id")),
        "altitude_layer_id": route.get("altitude_layer_id"),
        "model_supported_altitude_egm2008_m": route.get(
            "model_supported_altitude_egm2008_m"
        ),
        #: 两种高度必须分开披露：generic 走廊体元代表高度 vs Radar 评估高度。
        #: unknown 证据下 Radar 评估高度未被使用（绝不冒用 generic midpoint）。
        "corridor_voxel_representative_altitude_egm2008_m": _finite_float(
            probe.get("altitude_egm2008_m")
        ),
        "radar_evaluation_altitude_egm2008_m": None,
        "route_sample_height_semantics": route.get("route_sample_height_semantics"),
        "nearest_route_offset_m": probe.get("nearest_route_offset_m"),
        "algorithm_provenance": (evidence or {}).get("algorithm_provenance"),
    }
    return result


def _not_applicable_service_entry(route_id, voxel_id, evidence, *, probe, route, voxel_layer_id):
    """off-layer 体元的 Radar service entry：显式 ``not_applicable``。

    形状与正式 Radar service entry 同构（下游消费者不需要分支），但：

    * ``status = not_applicable`` / ``applicable = false``：它**不是** satisfied，
      也**不是** confirmed_deficit，更**不是** unknown；
    * ``radar_evaluation_altitude_egm2008_m = null``：从未调用 ``actual_site_coverage``，
      因此绝不存在"借用同层 100 m 结果"的可能；
    * ``distinct_site_count`` / ``required_distinct_site_count`` 均为 ``None``，
      绝不隐式计成 satisfied。
    """

    probe = probe if isinstance(probe, dict) else {}
    route = route if isinstance(route, dict) else {}
    return {
        "route_id": route_id, "voxel_id": voxel_id, "sample_id": None,
        "coordinate": [probe.get("longitude"), probe.get("latitude")],
        "surface_class": probe.get("surface_class") or "unknown", "service_key": SERVICE_KEY,
        "subsystem": "S", "service_subtype": "noncooperative_surveillance",
        "target_cooperation": "non_cooperative", "technology": "radar",
        "planner_family": planner_family_for(SERVICE_KEY), "surface_dependent": False,
        "supports_site_planning": True, "required_distinct_site_count": None,
        "covered_distinct_site_ids": [], "distinct_site_ids": [],
        "distinct_site_count": None, "counting_basis": "distinct_site_id",
        "status": "not_applicable", "applicable": False,
        "vertical_scope": RADAR_VERTICAL_SCOPE_OPERATIONAL_ALTITUDE_LAYER,
        "providers": [], "reasons": [RADAR_NOT_APPLICABLE_REASON],
        "input_fingerprint": (evidence or {}).get("input_fingerprint"),
        "voxel_altitude_layer_id": voxel_layer_id,
        "altitude_layer_id": route.get("altitude_layer_id"),
        "model_supported_altitude_egm2008_m": route.get(
            "model_supported_altitude_egm2008_m"
        ),
        "corridor_voxel_representative_altitude_egm2008_m": _finite_float(
            probe.get("altitude_egm2008_m")
        ),
        "radar_evaluation_altitude_egm2008_m": None,
        "route_sample_height_semantics": route.get("route_sample_height_semantics"),
        "nearest_route_offset_m": probe.get("nearest_route_offset_m"),
        "algorithm_provenance": (evidence or {}).get("algorithm_provenance"),
    }


def _overlap_bounds(value):
    """voxel ``overlap_height_egm2008_m`` → 两个有限浮点，否则 ``None``。"""

    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    lower, upper = _finite_float(value[0]), _finite_float(value[1])
    if lower is None or upper is None or upper < lower:
        return None
    return [lower, upper]



def _resolved_layout_altitude(item):
    """Return canonical layout altitude metadata, or fail closed on inconsistency.

    The authority is the resolved layout item, never a software default or a
    validation sample.  Parameters, route-sampling metadata, and samples are
    independent consistency witnesses for that authority.
    """

    parameters = item.get("parameters") if isinstance(item.get("parameters"), dict) else {}
    route_sampling = (
        item.get("route_sampling") if isinstance(item.get("route_sampling"), dict) else {}
    )
    layer_id = _nonempty_string(item.get("altitude_layer_id"))
    parameter_layer_id = _nonempty_string(parameters.get("fixed_altitude_layer_id"))
    altitude_m = _finite_float(item.get("altitude_m"))
    parameter_altitude_m = _finite_float(parameters.get("fixed_altitude_m"))
    sampling_altitude_m = _finite_float(route_sampling.get("sample_egm2008_m"))
    item_semantics = _nonempty_string(item.get("route_sample_height_semantics"))
    parameter_semantics = _nonempty_string(parameters.get("route_sample_height_semantics"))
    sampling_semantics = _nonempty_string(route_sampling.get("sample_egm2008_semantics"))

    required_values = (
        layer_id, parameter_layer_id, altitude_m, parameter_altitude_m,
        sampling_altitude_m, item_semantics, parameter_semantics, sampling_semantics,
    )
    if any(value is None for value in required_values):
        return None, "radar_layout_altitude_metadata_missing"

    expected_semantics = route_sample_height_semantics(layer_id)
    if (
        parameter_layer_id != layer_id
        or not _same_altitude(altitude_m, parameter_altitude_m)
        or not _same_altitude(altitude_m, sampling_altitude_m)
        or item_semantics != expected_semantics
        or parameter_semantics != item_semantics
        or sampling_semantics != item_semantics
    ):
        return None, "radar_layout_altitude_metadata_inconsistent"

    validation = item.get("validation") if isinstance(item.get("validation"), dict) else {}
    validation_samples = validation.get("samples") or []
    optimization_samples = (
        (item.get("service_evidence_inputs") or {}).get("samples") or []
        if isinstance(item.get("service_evidence_inputs"), dict) else []
    )
    for sample in [*optimization_samples, *validation_samples]:
        sample_altitude = _finite_float(
            sample.get("egm2008_m") if isinstance(sample, dict) else None
        )
        if sample_altitude is None or not _same_altitude(altitude_m, sample_altitude):
            return None, "radar_layout_altitude_sample_inconsistent"

    return {
        "altitude_layer_id": layer_id,
        "model_supported_altitude_egm2008_m": altitude_m,
        "route_sample_height_semantics": item_semantics,
    }, None


def _same_altitude(left, right):
    return abs(float(left) - float(right)) <= ALTITUDE_COMPARISON_TOLERANCE_M


def _finite_float(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def _nonempty_string(value):
    result = str(value).strip() if value is not None else ""
    return result or None


def _explicit_route_ids(required_cns):
    result = []
    for route_id, requirements in ((required_cns or {}).get("route_overrides") or {}).items():
        if service_requirement_for("S", (requirements or {}).get("surveillance") or {}, SERVICE_KEY):
            result.append(str(route_id))
    return sorted(set(result))


__all__ = [
    "RADAR_NOT_APPLICABLE_REASON", "RADAR_VERTICAL_SCOPE_OPERATIONAL_ALTITUDE_LAYER",
    "build_radar_service_evidence", "evidence_for_probe",
    "radar_candidate_actions", "radar_required_for", "radar_what_if_service_evidence",
]
