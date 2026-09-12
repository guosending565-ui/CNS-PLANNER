"""Engineering tactical protection envelope from explicit confirmed inputs."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json

from ...domain.operational_timing import RESPONSE_COMPONENTS


class TacticalProtectionEnvelopeV1:
    algorithm_id = "tactical_protection_envelope_v1"
    algorithm_version = "1.0"

    def __init__(self, parameters=None):
        self.parameters = dict(parameters or {})

    @classmethod
    def empty(cls, status="not_calculated"):
        return {
            "status": status, "algorithm_id": cls.algorithm_id,
            "algorithm_version": cls.algorithm_version,
            "parameters": {}, "input_fingerprint": None,
            "model_scope": "engineering_tactical_protection_envelope",
            "regulatory_well_clear": "not_evaluated",
            "formal_detection_volume_compliance": "not_evaluated",
            "t_pre_s": None, "d_reaction_m": None, "d_protect_m": None,
            "components": {}, "assumptions": [], "sources": [],
        }

    def evaluate(self, response_time_budget, encounter_scenario):
        budget = response_time_budget or {}
        encounter = encounter_scenario or {}
        missing = []
        components = {}
        if budget.get("confirmed") is not True or budget.get("status") != "confirmed":
            missing.append("ResponseTimeBudget 未完整确认")
        for name in RESPONSE_COMPONENTS:
            item = (budget.get("components") or {}).get(name) or {}
            components[name] = deepcopy(item)
            if item.get("confirmed") is not True or item.get("value_s") is None:
                missing.append(f"{name} 时间缺失或未确认")
        if encounter.get("confirmed") is not True or encounter.get("status") != "confirmed":
            missing.append("EncounterScenario 未完整确认")
        for name in (
            "relative_closing_speed_mps", "maneuver_distance_m",
            "uncertainty_distance_m",
        ):
            if encounter.get(name) is None:
                missing.append(f"{name} 缺失")
        fingerprint = sha256(json.dumps([
            budget, encounter, self.parameters,
        ], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        base = {
            "algorithm_id": self.algorithm_id,
            "algorithm_version": self.algorithm_version,
            "parameters": deepcopy(self.parameters),
            "input_fingerprint": fingerprint,
            "model_scope": "engineering_tactical_protection_envelope",
            "regulatory_well_clear": "not_evaluated",
            "formal_detection_volume_compliance": "not_evaluated",
            "budget_id": budget.get("budget_id"),
            "encounter_id": encounter.get("encounter_id"),
            "components": components,
            "sources": [budget.get("source"), encounter.get("source")],
            "assumptions": [
                "constant relative closing speed over response time",
                "maneuver and uncertainty distances are additive engineering inputs",
            ],
        }
        if missing:
            return {
                **base, "status": "unknown", "reasons": missing,
                "t_pre_s": None, "d_reaction_m": None, "d_protect_m": None,
            }
        t_pre = sum(float(components[name]["value_s"]) for name in RESPONSE_COMPONENTS)
        relative_speed = float(encounter["relative_closing_speed_mps"])
        d_reaction = relative_speed * t_pre
        d_protect = (
            d_reaction + float(encounter["maneuver_distance_m"])
            + float(encounter["uncertainty_distance_m"])
        )
        return {
            **base, "status": "passed", "reasons": [],
            "t_pre_s": t_pre,
            "relative_closing_speed_mps": relative_speed,
            "d_reaction_m": d_reaction,
            "maneuver_distance_m": float(encounter["maneuver_distance_m"]),
            "uncertainty_distance_m": float(encounter["uncertainty_distance_m"]),
            "d_protect_m": d_protect,
        }
