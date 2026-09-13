"""Backward-compatible pass-through RequiredCNS model."""

from copy import deepcopy
from hashlib import sha256
import json

from ...domain.cns_inputs import normalize_required_cns
from ...domain.requirement_policy import empty_required_cns_recommendation


class ManualRequiredCNSV1:
    algorithm_id = "manual_required_cns_v1"
    algorithm_version = "1.0"

    def __init__(self, parameters=None):
        self.parameters = deepcopy(parameters or {})

    def evaluate(self, required_cns, operation_context=None, policies=None, route_ids=None):
        current = normalize_required_cns(required_cns)
        fingerprint = _fingerprint([current, self.parameters])
        result = empty_required_cns_recommendation()
        result.update({
            "status": "manual_current", "result_status": "passed",
            "algorithm_id": self.algorithm_id, "algorithm_version": self.algorithm_version,
            "parameters": deepcopy(self.parameters), "input_fingerprint": fingerprint,
            "context_snapshot": {}, "recommended_required_cns": deepcopy(current),
            "current_vs_recommended_diff": [],
            "input_fingerprints": {"current_required_cns": _fingerprint(current)},
            "proposal_only": True, "requires_user_adoption": False,
            "reasons": ["manual_required_cns_is_authoritative"],
        })
        return result


def _fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
