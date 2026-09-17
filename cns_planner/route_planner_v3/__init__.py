"""Route Planner V3-A: native 3D strategic planning contract and hard-constraint kernel.

This package is additive.  It does not read, rewrite or reimplement the existing
``route_planner/risk_aware_v2`` search core, and it never writes
``operational_routes`` or ``algorithm_selection``.  V3-A produces a *strategic
candidate* inside an independent experimental container; it is explicitly not a
final safe or validated operational route.

Stage scope (V3-A): L8 horizontal x altitude x heading strategic search with
hard-constraint edge generation, a multi-component soft cost vector, and a
refinement-corridor proposal.  The 30 m local refinement, the exact
polygon/terrain final validator and CNS joint optimization are **not** in this
stage and are deliberately absent.
"""

from .contracts import (
    AIRCRAFT_MOTION_LIMITS_SCHEMA_VERSION, BUILDING_EXPOSURE_REFERENCE_M, COST_COMPONENTS,
    MAPPED_SOFT_CHANNELS, REFINEMENT_CORRIDOR_SCHEMA_VERSION, SEARCH_COMPLETENESS,
    SOFT_FIELD_STATUSES, V3_MODEL_SCOPE, V3_NEXT_STAGE_AFTER_STRATEGIC, V3_RESULT_STATUSES,
    V3_STATE_SCHEMA_VERSION, V3_STRATEGIC_RESULT_DISCLAIMER,
    V3CellEnvironment, V3CostVector, V3PlanningPolicy, V3PlanningProblem,
    V3Readiness, V3State, V3StrategicResult, AircraftMotionLimits,
    MotionPrimitive3D, CandidateRefinementCorridor, build_v3_state,
    default_aircraft_motion_limits, default_v3_cost_model, default_v3_policy,
    describe_altitude_states, empty_candidate_refinement_corridor,
    empty_v3_cell_environment, empty_v3_experimental_session, empty_v3_strategic_result,
    heading_bin_center_deg, heading_bin_for_bearing, normalize_aircraft_motion_limits,
    normalize_candidate_refinement_corridor, normalize_cost_model,
    normalize_v3_cell_environment, normalize_v3_cost_vector, normalize_v3_experiments,
    normalize_v3_planning_policy, normalize_v3_planning_problem,
    normalize_v3_strategic_result, v3_readiness_summary, v3_state_id,
)
from .motion import MotionPrimitiveProvider, TransitionValidator, kinematic_readiness
from .hard_constraints import (
    STATE_REASONS, TRANSITION_REASONS, HardConstraintAudit, HardConstraintEvaluator,
    seeded_rejection_summary,
)
from .cost import (
    BUILDING_EXPOSURE_INDEX_METHOD, BUILDING_EXPOSURE_INDEX_SEMANTICS,
    CHANNEL_SEMANTICS, EXPOSURE_UNIT, SOFT_CHANNELS, SoftCostModel,
    build_cost_vector, building_exposure_normalized, channel_index_values,
    edge_exposure, soft_channel_provenance, summarize_channel_exposures,
)
from .planner import (
    OUTCOME_CAP_REACHED, OUTCOME_EXHAUSTED, V3StrategicPlanner, v3_problem_fingerprint,
)
from .corridor import (
    CORRIDOR_SEMANTICS, NEXT_STAGE, RING_SEMANTICS, build_candidate_refinement_corridor,
    grid_index as v3_grid_index, ring_neighbors,
)
from .readiness import (
    READINESS_STATUSES, REQUIRED_POLICY_PARAMETERS, cost_enabled_channels,
    evaluate_v3_readiness, readiness_overall, unresolved_soft_channel_cells,
)
from .synthetic import (
    BUILDING_PROFILES, SYNTHETIC_SOFT_FIELD_PROVENANCE, SYNTHETIC_SPEC_DEFAULT,
    TERRAIN_PROFILES, build_synthetic_environment, normalize_synthetic_spec,
)
from .fine_contracts import (
    FINE_ALGORITHM_ID, FINE_ALGORITHM_VERSION, FINE_ENVIRONMENT_SCHEMA_VERSION,
    FINE_GRID_SPEC_SCHEMA_VERSION, FINE_MODEL_SCOPE, FINE_MOTION_PRIMITIVE_SCHEMA_VERSION,
    FINE_REFINEMENT_POLICY_SCHEMA_VERSION, FINE_RESOLUTION_SOURCES,
    LOCAL_FRAME_SCHEMA_VERSION,
    REFINEMENT_FINGERPRINT_COMPONENTS, REFINEMENT_PROBLEM_SCHEMA_VERSION,
    REFINEMENT_RESULT_SCHEMA_VERSION, REFINEMENT_TURN_MODEL, TERRAIN_SAMPLING_METHOD,
    V3B_DISCLAIMER, V3B_RESULT_STATUSES, V3C_PENDING,
    default_v3_fine_refinement_policy,
    empty_fine_cell_environment, empty_fine_grid_spec, empty_local_metric_frame,
    empty_v3_refinement_problem, empty_v3_refinement_result,
    evaluate_refinement_applicability, fine_grid_fingerprint, frame_fingerprint,
    normalize_fine_cell_environment, normalize_fine_grid_spec,
    normalize_local_metric_frame, normalize_v3_fine_refinement_policy,
    normalize_v3_refinement_problem, normalize_v3_refinement_result,
    refinement_fingerprint, refinement_fingerprint_components,
)
from .fine_grid import (
    assemble_fine_environment, bind_fine_cells_to_parents, build_fine_grid_spec,
    build_local_frame, building_facts_for_cells, cell_id as fine_cell_id,
    fine_adjacency, fine_cells_from_spec, fine_grid_summary, fine_index,
    map_airspace_to_fine, parent_soft_fields_from_risk_contributors,
    ring_rect_distance, terrain_fact_from_pixels, terrain_floor, upsample_soft_fields,
)
from .fine_search import (
    TRAVERSED_REASONS, FinePrimitiveProvider, V3RefinementPlanner,
    evaluate_refinement_readiness, grid_bearing_deg, supercover_line,
)
from .fine_synthetic import (
    SYNTHETIC_FINE_SPEC_DEFAULT, build_synthetic_fine_environment,
    normalize_synthetic_fine_spec,
)

__all__ = [
    "AIRCRAFT_MOTION_LIMITS_SCHEMA_VERSION", "BUILDING_EXPOSURE_INDEX_METHOD",
    "BUILDING_EXPOSURE_INDEX_SEMANTICS", "BUILDING_EXPOSURE_REFERENCE_M",
    "BUILDING_PROFILES", "CHANNEL_SEMANTICS", "CORRIDOR_SEMANTICS", "COST_COMPONENTS",
    "EXPOSURE_UNIT", "MAPPED_SOFT_CHANNELS", "NEXT_STAGE", "OUTCOME_CAP_REACHED",
    "OUTCOME_EXHAUSTED", "READINESS_STATUSES", "REFINEMENT_CORRIDOR_SCHEMA_VERSION",
    "REQUIRED_POLICY_PARAMETERS", "RING_SEMANTICS", "SEARCH_COMPLETENESS",
    "SOFT_CHANNELS", "SOFT_FIELD_STATUSES",
    "STATE_REASONS", "SYNTHETIC_SOFT_FIELD_PROVENANCE", "SYNTHETIC_SPEC_DEFAULT",
    "TERRAIN_PROFILES", "TRANSITION_REASONS",
    "V3_MODEL_SCOPE", "V3_NEXT_STAGE_AFTER_STRATEGIC", "V3_RESULT_STATUSES",
    "V3_STATE_SCHEMA_VERSION", "V3_STRATEGIC_RESULT_DISCLAIMER",
    "AircraftMotionLimits", "CandidateRefinementCorridor", "HardConstraintAudit",
    "HardConstraintEvaluator", "MotionPrimitive3D", "MotionPrimitiveProvider",
    "SoftCostModel", "TransitionValidator", "V3CellEnvironment", "V3CostVector",
    "V3PlanningPolicy", "V3PlanningProblem", "V3Readiness", "V3State",
    "V3StrategicPlanner", "V3StrategicResult",
    "build_candidate_refinement_corridor", "build_cost_vector",
    "build_synthetic_environment", "build_v3_state", "building_exposure_normalized",
    "channel_index_values", "cost_enabled_channels",
    "default_aircraft_motion_limits", "default_v3_cost_model", "default_v3_policy",
    "describe_altitude_states", "edge_exposure", "empty_candidate_refinement_corridor",
    "empty_v3_cell_environment", "empty_v3_experimental_session",
    "empty_v3_strategic_result", "evaluate_v3_readiness", "heading_bin_center_deg",
    "heading_bin_for_bearing", "kinematic_readiness", "normalize_aircraft_motion_limits",
    "normalize_candidate_refinement_corridor", "normalize_cost_model",
    "normalize_synthetic_spec", "normalize_v3_cell_environment",
    "normalize_v3_cost_vector", "normalize_v3_experiments",
    "normalize_v3_planning_policy", "normalize_v3_planning_problem",
    "normalize_v3_strategic_result", "readiness_overall", "ring_neighbors",
    "seeded_rejection_summary", "soft_channel_provenance",
    "summarize_channel_exposures", "unresolved_soft_channel_cells",
    "v3_grid_index", "v3_problem_fingerprint", "v3_readiness_summary", "v3_state_id",
    # ---- V3-B: corridor-local fine refinement -------------------------------
    "FINE_ALGORITHM_ID", "FINE_ALGORITHM_VERSION", "FINE_ENVIRONMENT_SCHEMA_VERSION",
    "FINE_GRID_SPEC_SCHEMA_VERSION", "FINE_MODEL_SCOPE",
    "FINE_MOTION_PRIMITIVE_SCHEMA_VERSION", "FINE_REFINEMENT_POLICY_SCHEMA_VERSION",
    "FINE_RESOLUTION_SOURCES",
    "LOCAL_FRAME_SCHEMA_VERSION", "REFINEMENT_FINGERPRINT_COMPONENTS",
    "REFINEMENT_PROBLEM_SCHEMA_VERSION", "REFINEMENT_RESULT_SCHEMA_VERSION",
    "REFINEMENT_TURN_MODEL", "SYNTHETIC_FINE_SPEC_DEFAULT", "TERRAIN_SAMPLING_METHOD",
    "TRAVERSED_REASONS", "V3B_DISCLAIMER", "V3B_RESULT_STATUSES", "V3C_PENDING",
    "FinePrimitiveProvider", "V3RefinementPlanner",
    "assemble_fine_environment", "bind_fine_cells_to_parents", "build_fine_grid_spec",
    "build_local_frame", "build_synthetic_fine_environment", "building_facts_for_cells",
    "default_v3_fine_refinement_policy",
    "empty_fine_cell_environment", "empty_fine_grid_spec", "empty_local_metric_frame",
    "empty_v3_refinement_problem", "empty_v3_refinement_result",
    "evaluate_refinement_applicability", "evaluate_refinement_readiness",
    "fine_adjacency", "fine_cells_from_spec", "fine_cell_id", "fine_grid_fingerprint",
    "fine_grid_summary", "fine_index", "frame_fingerprint", "grid_bearing_deg",
    "map_airspace_to_fine", "normalize_fine_cell_environment",
    "normalize_fine_grid_spec", "normalize_local_metric_frame",
    "normalize_synthetic_fine_spec", "normalize_v3_fine_refinement_policy",
    "normalize_v3_refinement_problem",
    "normalize_v3_refinement_result", "parent_soft_fields_from_risk_contributors",
    "refinement_fingerprint",
    "refinement_fingerprint_components", "ring_rect_distance", "supercover_line",
    "terrain_fact_from_pixels", "terrain_floor", "upsample_soft_fields",
]
