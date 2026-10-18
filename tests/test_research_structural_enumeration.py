"""Structural oracle checks; no model invocation."""
from copy import deepcopy
from math import comb
import pytest
from cns_planner.application.research_structural_enumeration import (
    structural_subset, enumerate_structural, independent_oracle, verify_structural,
    a0_masks,
)


def actions():
    return [dict(action_id=f"a{i}", physical_site_id=f"s{i//2}",
                 device_instance_id=f"d{i}", service_key="C:communication",
                 route_ids=["R0005"]) for i in range(7)]


def test_complete_bitmask_and_independent_combinations_agree():
    source = actions(); before = deepcopy(source)
    rows = list(enumerate_structural(source))
    assert len(rows) == 128
    assert [r["mask"] for r in rows] == list(range(128))
    assert verify_structural(source, rows) == {"status": "verified", "subset_count": 128,
        "scope": "structural_combinations_only", "p14_geometry_verified": False}
    assert len({r["subset_id"] for r in rows}) == 128
    assert {r["subset_id"] for r in rows} == {r["subset_id"] for r in independent_oracle(source)}
    for n in range(8):
        assert sum(r["service_action_count"] == n for r in rows) == comb(7, n)
    assert source == before


def test_structural_counts_are_not_real_l1_l2():
    r = structural_subset(actions(), 3)
    assert r["distinct_physical_sites"] == 1
    assert r["conditional_device_instances"] == 2
    assert r["service_action_count"] == 2
    assert r["real_L1"] is None and r["real_L2"] is None
    assert r["activation_policy"] == "H0"


@pytest.mark.parametrize("mask", [-1, 128, True, 1.5])
def test_invalid_mask(mask):
    with pytest.raises(ValueError): structural_subset(actions(), mask)


def test_identity_bound_to_order_and_domain():
    a = actions(); changed = deepcopy(a); changed[0]["device_instance_id"] = "other"
    assert structural_subset(a, 0)["subset_id"] != structural_subset(changed, 0)["subset_id"]
    assert structural_subset(a, 1)["subset_id"] != structural_subset(a[::-1], 1)["subset_id"]


def test_reject_duplicate_ids_and_bad_oracle():
    a = actions(); a[1]["action_id"] = a[0]["action_id"]
    with pytest.raises(ValueError): list(enumerate_structural(a))
    rows = list(enumerate_structural(actions())); rows[0]["conditional_device_instances"] = 1
    with pytest.raises(ValueError): verify_structural(actions(), rows)
    with pytest.raises(ValueError): verify_structural(actions(), rows[1:])


def test_a0_is_only_empty_and_singletons():
    assert a0_masks() == (0, 1, 2, 4, 8, 16, 32, 64)
