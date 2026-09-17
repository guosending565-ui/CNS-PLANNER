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
    REFINEMENT_CORRIDOR_SCHEMA_VERSION, V3_MODEL_SCOPE, V3_RESULT_STATUSES,
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
    SOFT_WEIGHT_SUM_LIMIT, SoftCostModel, build_cost_vector, building_exposure_normalized,
)
from .planner import V3StrategicPlanner, v3_problem_fingerprint
from .corridor import (
    CORRIDOR_SEMANTICS, NEXT_STAGE, RING_SEMANTICS, build_candidate_refinement_corridor,
    grid_index as v3_grid_index, ring_neighbors,
)
from .readiness import (
    READINESS_STATUSES, REQUIRED_POLICY_PARAMETERS, evaluate_v3_readiness,
    readiness_overall,
)
from .synthetic import (
    BUILDING_PROFILES, SYNTHETIC_SPEC_DEFAULT, TERRAIN_PROFILES,
    build_synthetic_environment, normalize_synthetic_spec,
)

__all__ = [
    "AIRCRAFT_MOTION_LIMITS_SCHEMA_VERSION", "BUILDING_EXPOSURE_REFERENCE_M",
    "BUILDING_PROFILES", "CORRIDOR_SEMANTICS", "COST_COMPONENTS", "NEXT_STAGE",
    "READINESS_STATUSES", "REFINEMENT_CORRIDOR_SCHEMA_VERSION",
    "REQUIRED_POLICY_PARAMETERS", "RING_SEMANTICS", "SOFT_WEIGHT_SUM_LIMIT",
    "STATE_REASONS", "SYNTHETIC_SPEC_DEFAULT", "TERRAIN_PROFILES", "TRANSITION_REASONS",
    "V3_MODEL_SCOPE", "V3_RESULT_STATUSES", "V3_STATE_SCHEMA_VERSION",
    "V3_STRATEGIC_RESULT_DISCLAIMER",
    "AircraftMotionLimits", "CandidateRefinementCorridor", "HardConstraintAudit",
    "HardConstraintEvaluator", "MotionPrimitive3D", "MotionPrimitiveProvider",
    "SoftCostModel", "TransitionValidator", "V3CellEnvironment", "V3CostVector",
    "V3PlanningPolicy", "V3PlanningProblem", "V3Readiness", "V3State",
    "V3StrategicPlanner", "V3StrategicResult",
    "build_candidate_refinement_corridor", "build_cost_vector",
    "build_synthetic_environment", "build_v3_state", "building_exposure_normalized",
    "default_aircraft_motion_limits", "default_v3_cost_model", "default_v3_policy",
    "describe_altitude_states", "empty_candidate_refinement_corridor",
    "empty_v3_cell_environment", "empty_v3_experimental_session",
    "empty_v3_strategic_result", "evaluate_v3_readiness", "heading_bin_center_deg",
    "heading_bin_for_bearing", "kinematic_readiness", "normalize_aircraft_motion_limits",
    "normalize_candidate_refinement_corridor", "normalize_cost_model",
    "normalize_synthetic_spec", "normalize_v3_cell_environment",
    "normalize_v3_cost_vector", "normalize_v3_experiments",
    "normalize_v3_planning_policy", "normalize_v3_planning_problem",
    "normalize_v3_strategic_result", "readiness_overall", "ring_neighbors",
    "seeded_rejection_summary", "v3_grid_index", "v3_problem_fingerprint",
    "v3_readiness_summary", "v3_state_id",
]
