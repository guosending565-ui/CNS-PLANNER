"""Deterministic research subset identities; counts are structural under H0.

The second enumeration uses combinations and separately computes its counts.
Neither implementation evaluates, validates or proves P14 geometry.
"""
from collections import Counter
from hashlib import sha256
from itertools import combinations
import json


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _domain(actions):
    if not isinstance(actions, list) or len(actions) > 7:
        raise ValueError("at most seven structural actions required")
    keys = ("action_id", "physical_site_id", "device_instance_id", "service_key", "route_ids")
    rows = []
    for a in actions:
        if not isinstance(a, dict) or any(not a.get(k) for k in keys):
            raise ValueError("complete action identity required")
        if any(not isinstance(a[k], str) for k in keys[:-1]):
            raise ValueError("string action identities required")
        if not isinstance(a["route_ids"], list) or any(not isinstance(x, str) for x in a["route_ids"]):
            raise ValueError("route identities required")
        rows.append({k: a[k] for k in keys})
    for key in ("action_id", "device_instance_id"):
        if len({a[key] for a in rows}) != len(rows):
            raise ValueError("duplicate " + key)
    return rows, _digest(rows)


def structural_subset(actions, mask):
    rows, domain = _domain(actions)
    if type(mask) is not int or not 0 <= mask < 2 ** len(rows):
        raise ValueError("invalid bitmask")
    chosen = [r for i, r in enumerate(rows) if mask & (1 << i)]
    ids = [a["action_id"] for a in chosen]
    return dict(schema_version="research_structural_enumeration.v1", mask=mask,
        domain_fingerprint=domain, subset_id=_digest([domain, ids]), action_ids=ids,
        distinct_physical_sites=len({a["physical_site_id"] for a in chosen}),
        conditional_device_instances=len({a["device_instance_id"] for a in chosen}),
        service_action_count=len(chosen), service_counts=dict(Counter(a["service_key"] for a in chosen)),
        activation_policy="H0", real_L1=None, real_L2=None)


def enumerate_structural(actions):
    rows, _ = _domain(actions)
    for mask in range(2 ** len(rows)):
        yield structural_subset(actions, mask)


def independent_oracle(actions):
    rows, domain = _domain(actions)
    for size in range(len(rows) + 1):
        for positions in combinations(range(len(rows)), size):
            ids, sites, devices, services = [], set(), set(), {}
            mask = 0
            for position in positions:
                a = rows[position]
                ids.append(a["action_id"]); sites.add(a["physical_site_id"])
                devices.add(a["device_instance_id"])
                services[a["service_key"]] = services.get(a["service_key"], 0) + 1
                mask += 2 ** position
            yield dict(schema_version="research_structural_enumeration.v1", mask=mask,
                domain_fingerprint=domain, subset_id=_digest([domain, ids]), action_ids=ids,
                distinct_physical_sites=len(sites), conditional_device_instances=len(devices),
                service_action_count=size, service_counts=services,
                activation_policy="H0", real_L1=None, real_L2=None)


def verify_structural(actions, observed):
    actual = list(observed)
    expected = list(independent_oracle(actions))
    if len(actual) != len(expected) or sorted(actual, key=lambda r:r["mask"]) != sorted(expected, key=lambda r:r["mask"]):
        raise ValueError("structural oracle mismatch")
    return dict(status="verified", subset_count=len(actual),
                scope="structural_combinations_only", p14_geometry_verified=False)


def a0_masks():
    """Eight inputs; no A1 evaluation authorization is implied."""
    return (0, 1, 2, 4, 8, 16, 32, 64)
