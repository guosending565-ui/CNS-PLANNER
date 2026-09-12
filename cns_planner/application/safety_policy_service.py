"""Application service for persisted safety policy and directed invalidation."""

from copy import deepcopy

from ..domain.safety_policy import normalize_safety_policy


class SafetyPolicyService:
    def __init__(self, session, invalidation_service, snapshot):
        self.session = session
        self.invalidation_service = invalidation_service
        self.snapshot = snapshot

    def policy_snapshot(self):
        return deepcopy(self.session.state["safety_policy"])

    def set_policy(self, payload):
        source = payload.get("safety_policy", payload) if isinstance(payload, dict) else payload
        candidate = normalize_safety_policy(source)
        if candidate == self.session.state["safety_policy"]:
            return self.snapshot()
        self.session.state["safety_policy"] = candidate
        self.invalidation_service.safety_policy()
        self.session.save()
        return self.snapshot()
