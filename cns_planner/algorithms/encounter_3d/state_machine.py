"""Strict engineering DAA event progression; never produces a SafetyEvent/UE."""

from copy import deepcopy


class DAAEventStateMachineV1:
    algorithm_id = "daa_event_state_machine_v1"
    algorithm_version = "1.0"
    ALLOWED = {
        "NO_TRAFFIC": {"DETECTED", "LOST_TRACK"},
        "DETECTED": {"TRACKED", "LOST_TRACK"},
        "TRACKED": {"PREDICTED_CONFLICT", "CLEARED", "LOST_TRACK"},
        "PREDICTED_CONFLICT": {"WARNING", "ALERT_DELIVERY_FAILED", "LOST_TRACK"},
        "WARNING": {"ACTION_REQUIRED", "ALERT_DELIVERY_FAILED", "LOST_TRACK"},
        "ACTION_REQUIRED": {"COMMAND_SENT", "COMMAND_UNAVAILABLE", "MANEUVER_UNRESOLVED", "LOST_TRACK"},
        "COMMAND_SENT": {"EXECUTING", "COMMAND_UNAVAILABLE", "MANEUVER_UNRESOLVED", "LOST_TRACK"},
        "EXECUTING": {"CLEARED", "MANEUVER_UNRESOLVED", "LOST_TRACK"},
        "CLEARED": set(), "LOST_TRACK": set(), "ALERT_DELIVERY_FAILED": set(),
        "COMMAND_UNAVAILABLE": set(), "MANEUVER_UNRESOLVED": set(),
    }

    def __init__(self):
        self.current_state = "NO_TRAFFIC"
        self.transitions = []

    def transition(self, target, time_s, reason, evidence=None):
        if target not in self.ALLOWED.get(self.current_state, set()):
            raise ValueError(f"非法 DAA transition: {self.current_state} -> {target}")
        transition_time = float(time_s)
        if self.transitions and transition_time < self.transitions[-1]["time_s"]:
            raise ValueError("DAA transition time_s 必须单调不减")
        item = {
            "from_state": self.current_state, "to_state": target,
            "time_s": transition_time, "reason": str(reason),
            "input_evidence": deepcopy(evidence or {}),
        }
        self.current_state = target
        self.transitions.append(item)
        return item

    def result(self):
        return {
            "status": "passed" if self.current_state == "CLEARED" else "engineering_event",
            "current_state": self.current_state,
            "transitions": deepcopy(self.transitions),
            "algorithm_id": self.algorithm_id,
            "algorithm_version": self.algorithm_version,
            "safety_event": "not_evaluated", "unacceptable_event": "not_evaluated",
        }
