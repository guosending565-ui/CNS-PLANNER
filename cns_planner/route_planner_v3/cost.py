"""Provenance-carrying multi-component soft cost for Route Planner V3.

V3-A correctness fix (carried into V3-B) -- the previous version divided raw
population/traffic counts by hidden constants (``10000`` / ``100``) inside the
planner and then justified the heuristic with the resulting ``sum(weight) <= 1``
cap.  Both are gone.  The rules now are:

* every soft channel enters the cost as an explicitly **normalized index in
  ``[0, 1]`` that travels with its own provenance** (``source``,
  ``source_resolution_m``, ``mapping_method``,
  ``upsampled_without_new_information``).  The planner never invents a
  normalizer: if the canonical environment does not carry a provenance-bearing
  index for an enabled channel, readiness blocks the run instead of silently
  treating the field as ``0``;
* one edge's exposure for a channel is the length-integrated index

      exposure_m = length_m * (index(source) + index(target)) / 2

  which is dimensionally ``metre x normalized index``;
* the scalar cost is

      scalar = sum_edges( length_m + sum_channels( weight_c * exposure_m_c ) )

  so **every edge costs at least its own geometric length** independently of the
  weights.  The admissible heuristic is therefore the plain 3D geometric
  distance and does not depend on the weights at all;
* a weight only has to be *explicit, finite and ``>= 0``*.  There is no upper
  bound on ``sum(weight)`` any more, because the admissibility argument no
  longer needs one.

``energy`` stays ``pending_model`` / disabled (V3 does not invent an energy
formula) and CNS stays ``post_route_assessment`` /
``excluded_from_search_cost=true``.
"""

from __future__ import annotations

from .contracts import (
    BUILDING_EXPOSURE_REFERENCE_M, V3_COST_VECTOR_SCHEMA_VERSION,
    normalize_v3_cost_vector,
)

#: Channels that carry a weighted soft penalty.
SOFT_CHANNELS = ("population_risk", "traffic_risk", "building_exposure")

#: Channels whose per-cell index must be *mapped from data* and therefore must
#: arrive with an explicit provenance record.
MAPPED_SOFT_CHANNELS = ("population_risk", "traffic_risk")

#: Unit of a length-integrated normalized index.
EXPOSURE_UNIT = "m_x_normalized_index"

#: Mapping method of the building channel: it is derived from the state's own
#: vertical margin, never from a data raster.
BUILDING_EXPOSURE_INDEX_METHOD = "vertical_clearance_proximity_index_0_1"
BUILDING_EXPOSURE_INDEX_SEMANTICS = "normalized_clearance_proximity_not_collision_probability"

#: Semantics strings reused by both the planner and the reports.
CHANNEL_SEMANTICS = {
    "population_risk": "length_integrated_population_exposure_index_not_probability",
    "traffic_risk": "length_integrated_traffic_exposure_index_not_conflict_probability",
    "building_exposure": "length_integrated_building_clearance_proximity_index_not_collision_probability",
}


class SoftCostModel:
    """Validated, explicit soft-cost configuration for one planning run."""

    def __init__(self, cost_model):
        self.cost_model = cost_model or {}
        components = self.cost_model.get("components") or {}
        self.enabled = {}
        self.weights = {}
        for name in ("distance",) + SOFT_CHANNELS + ("energy",):
            entry = components.get(name) or {}
            enabled = bool(entry.get("enabled"))
            weight = entry.get("weight")
            if name == "energy":
                # V3 has no energy model; it can never be enabled.
                enabled = False
                weight = None
            elif name == "distance":
                enabled = True
                weight = 1.0
            if enabled and (weight is None or not _is_finite_nonnegative(weight)):
                raise ValueError(
                    f"启用 soft cost component {name} 必须显式提供有限且非负的 weight"
                )
            self.enabled[name] = enabled
            self.weights[name] = None if weight is None else float(weight)
        self.cap = self.cost_model.get("scalar_cost_cap")
        self.cap_is_active = self.cap is not None

    # ------------------------------------------------------------------ per edge

    @property
    def enabled_penalty_channels(self):
        return tuple(name for name in SOFT_CHANNELS if self.enabled[name])

    @property
    def penalty_weight_sum(self):
        """Reported weight sum.  It is *not* bounded and *not* used by the heuristic."""

        return float(sum(self.weights[name] for name in self.enabled_penalty_channels))

    def edge_exposure(self, channel, length_m, source_index, target_index):
        """``length_m x (index_source + index_target) / 2`` for one edge."""

        return edge_exposure(channel, length_m, source_index, target_index)

    def edge_penalty(self, length_m, indices):
        """Weighted penalty of one edge.

        ``indices`` maps each enabled channel to ``(source_index, target_index)``.
        Every enabled channel must be supplied: a missing index is a data gap,
        not a zero penalty.
        """

        total = 0.0
        for channel in self.enabled_penalty_channels:
            pair = indices.get(channel)
            if pair is None:
                raise ValueError(f"soft channel {channel} 未提供 index pair，不得当作 0 penalty")
            total += self.weights[channel] * self.edge_exposure(channel, length_m, pair[0], pair[1])
        return total

    def edge_scalar_cost(self, distance_m, penalty):
        return float(distance_m) + float(penalty)

    def heuristic_scale(self):
        """The heuristic is the geometric distance: the scale is exactly 1."""

        return 1.0


def _is_finite_nonnegative(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return float(value) == float(value) and float(value) not in (float("inf"), float("-inf")) and float(value) >= 0.0


def building_exposure_normalized(altitude_m, required_clearance_m):
    """Dimensionless ``[0, 1]`` building proximity of one state.

    ``1`` at the required vertical clearance, ``0`` at or above
    ``BUILDING_EXPOSURE_CLEARANCE_M`` beyond it.  This is a documented
    engineering proxy for "how tight the vertical margin is"; it is **not** a
    collision probability and not an accident rate.  The index is derived from
    the state's own altitude and the cell's required clearance, so it needs no
    external normalizer.
    """

    if required_clearance_m is None or altitude_m is None:
        return 0.0
    margin = float(altitude_m) - float(required_clearance_m)
    if margin <= 0:
        return 1.0
    if margin >= BUILDING_EXPOSURE_CLEARANCE_M:
        return 0.0
    return round(1.0 - margin / BUILDING_EXPOSURE_CLEARANCE_M, 12)


def channel_index_values(edges):
    """Index statistics of the per-edge (source, target) index pairs."""

    values = []
    for edge in edges:
        values.append(float(edge["source_index"]))
        values.append(float(edge["target_index"]))
    if not values:
        return {"count": 0, "min": None, "max": None, "mean": None}
    return {
        "count": len(values),
        "min": round(min(values), 12),
        "max": round(max(values), 12),
        "mean": round(sum(values) / len(values), 12),
    }


def build_cost_vector(*, distances, channel_exposures, model, weight_sum=None):
    """Assemble the JSON-safe multi-component cost vector for one candidate.

    ``channel_exposures`` maps a soft channel to

    ```text
    {"edges": [{"length_m", "source_index", "target_index", "exposure_m"}, ...],
     "source", "source_resolution_m", "mapping_method",
     "upsampled_without_new_information", "provenance_sources": [...]}
    ```
    """

    scalar = float(sum(distances))
    penalty_total = 0.0
    entries = {}

    distance_raw = round(scalar, 9)
    entries["distance"] = _entry(
        raw=distance_raw, normalized=1.0, weight=1.0, unit="m",
        source="v3_planner_internal_geodesic_edge_sum",
        semantics="真实三维航段长度（唯一具有物理单位的直接项）",
        enabled=True, status="passed",
        contribution=round(scalar, 9),
    )

    for channel in SOFT_CHANNELS:
        spec = channel_exposures.get(channel) or {}
        edges = list(spec.get("edges") or [])
        total_exposure = float(sum(float(edge["exposure_m"]) for edge in edges))
        statistics = channel_index_values(edges)
        enabled = bool(model.enabled[channel])
        weight = model.weights[channel]
        contribution = (float(weight) * total_exposure) if enabled else None
        if enabled:
            penalty_total += float(contribution or 0.0)
        entry = _entry(
            raw=round(total_exposure, 12),
            normalized=statistics["mean"],
            weight=weight,
            unit=EXPOSURE_UNIT,
            source=spec.get("source") or "environment.soft_fields",
            semantics=CHANNEL_SEMANTICS[channel],
            enabled=enabled,
            status="passed" if enabled else "disabled_no_explicit_weight",
            contribution=None if contribution is None else round(contribution, 12),
            reason=None if enabled else "未在 policy.cost_model 中显式启用并给定非负 weight",
        )
        entry.update({
            "exposure_m": round(total_exposure, 12),
            "exposure_definition": "length_m_x_mean_index_of_edge_endpoints",
            "normalized_index_statistics": statistics,
            "source_resolution_m": spec.get("source_resolution_m"),
            "mapping_method": spec.get("mapping_method"),
            "upsampled_without_new_information": bool(
                spec.get("upsampled_without_new_information", False)
            ),
            "provenance_sources": list(spec.get("provenance_sources") or []),
            "provenance_note": spec.get("provenance_note"),
            "edge_count": len(edges),
        })
        entries[channel] = entry

    entries["energy"] = _entry(
        raw=None, normalized=None, weight=None, unit="not_modelled",
        source="not_available",
        semantics="pending_model_disabled",
        enabled=False, status="pending_model",
        reason="V3 不发明 energy 公式；energy 默认 pending_model 且禁用，未进入 scalar cost。",
    )

    scalar_total = round(scalar + penalty_total, 9)
    cap_active = getattr(model, "cap_is_active", False)
    return normalize_v3_cost_vector({
        "schema_version": V3_COST_VECTOR_SCHEMA_VERSION,
        "components": entries,
        "scalar_cost_available": True,
        "scalar_cost": scalar_total,
        "scalar_cost_semantics": (
            "sum_over_edges(length_m + sum_channels(weight_x_exposure_m)); "
            "exposure_m = length_m x (index_source + index_target) / 2，index 必须带 provenance 且位于 [0,1]；"
            "每条边代价 >= 边长，因此 h = 3D 几何距离即为下界"
        ),
        "scalar_weight_sum": round(
            float(model.penalty_weight_sum if weight_sum is None else weight_sum), 9
        ),
        "scalar_weight_sum_is_not_bounded": True,
        "scalar_weight_sum_not_used_by_the_heuristic": True,
        "excluded_components": ["energy"],
        "scalar_cost_cap": model.cap,
        "scalar_cost_cap_active": cap_active,
        "cns_integration": {
            "integration_mode": "post_route_assessment",
            "excluded_from_search_cost": True,
            "semantics": "V3 第一阶段 CNS 不进搜索：Route Planning → CNS Assessment",
            "energy_and_cns_are_not_evaluated": True,
        },
        "total_raw_is_not_a_sum_of_units": True,
    })


def soft_channel_provenance(cells, channel, properties=None):
    """Provenance of one soft channel over the cells a candidate actually used.

    A mapped channel aggregates the per-cell ``source`` /
    ``source_resolution_m`` / ``mapping_method`` /
    ``upsampled_without_new_information`` records.  If those records disagree the
    result says so (``mixed``) instead of picking one silently.
    """

    properties = properties or {}
    if channel == "building_exposure":
        upsample = any(
            bool(((cell.get("buildings") or {}).get("upsampled_without_new_information")))
            for cell in cells
        )
        return {
            "source": "environment.buildings.required_clearance_egm2008_m",
            "source_resolution_m": properties.get("cell_size_m"),
            "mapping_method": BUILDING_EXPOSURE_INDEX_METHOD,
            "upsampled_without_new_information": upsample,
            "provenance_sources": ["environment.buildings.required_clearance_egm2008_m"],
            "provenance_note": (
                "index 由状态高度与该格 required clearance 的垂直余量导出，不是数据栅格；"
                "语义为 clearance proximity，不是碰撞概率"
            ),
            "semantics": BUILDING_EXPOSURE_INDEX_SEMANTICS,
        }
    sources, resolutions, methods, flags = set(), set(), set(), []
    for cell in cells:
        entry = ((cell.get("soft_fields") or {}).get(channel)) or {}
        if entry.get("source"):
            sources.add(str(entry["source"]))
        if entry.get("source_resolution_m") is not None:
            resolutions.add(float(entry["source_resolution_m"]))
        if entry.get("mapping_method"):
            methods.add(str(entry["mapping_method"]))
        flags.append(bool(entry.get("upsampled_without_new_information")))
    return {
        "source": sorted(sources)[0] if len(sources) == 1 else ("mixed" if sources else None),
        "source_resolution_m": max(resolutions) if resolutions else None,
        "mapping_method": sorted(methods)[0] if len(methods) == 1 else ("mixed" if methods else None),
        "upsampled_without_new_information": any(flags),
        "provenance_sources": sorted(sources),
        "provenance_note": (
            "coarse 数据映射到更细网格时不获得新的原始精度："
            "upsampled_without_new_information 为 true 表示细网格 index 只是 coarse index 的复制"
            if any(flags) else None
        ),
        "semantics": CHANNEL_SEMANTICS[channel],
    }


def summarize_channel_exposures(channel, edge_records, *, provenance=None):
    """``channel_exposures[channel]`` entry for :func:`build_cost_vector`."""

    edges = []
    for record in edge_records:
        pair = (record.get("channel_indices") or {}).get(channel)
        if pair is None:
            continue
        edges.append({
            "length_m": record["length_m"],
            "source_index": pair[0],
            "target_index": pair[1],
            "exposure_m": edge_exposure(channel, record["length_m"], pair[0], pair[1]),
        })
    return {"edges": edges, **(provenance or {})}


def edge_exposure(channel, length_m, source_index, target_index):
    """Module-level ``length_m x (index_source + index_target) / 2``."""

    if source_index is None or target_index is None:
        raise ValueError(f"soft channel {channel} 缺少 provenance normalized index，不得当作 0")
    return float(length_m) * (float(source_index) + float(target_index)) / 2.0


def _entry(*, raw, normalized, weight, unit, source, semantics, enabled, status,
           contribution=None, reason=None):
    return {
        "raw": raw, "normalized": normalized, "weight": weight, "contribution": contribution,
        "unit": unit, "source": source, "semantics": semantics,
        "enabled": enabled, "status": status, "reason": reason,
    }


__all__ = [
    "BUILDING_EXPOSURE_INDEX_METHOD", "BUILDING_EXPOSURE_INDEX_SEMANTICS",
    "BUILDING_EXPOSURE_REFERENCE_M", "CHANNEL_SEMANTICS", "EXPOSURE_UNIT",
    "MAPPED_SOFT_CHANNELS", "SOFT_CHANNELS", "SoftCostModel",
    "build_cost_vector", "building_exposure_normalized", "channel_index_values",
    "edge_exposure", "soft_channel_provenance", "summarize_channel_exposures",
]
