"""Fail-closed gate: a PCF must never be published from stale grid attributes.

Round38 real acceptance found the hole this file pins. ``GenerationArguments``
reads ``grid_attributes["terrain"].cells`` / ``["buildings"].cells`` straight
from state, and ``apply_field`` then wrote ``planning_constraint_fields`` as
``passed`` unconditionally. So when an upstream source change marked
``grid_attributes["buildings"]`` as ``stale`` (Round38's CRS confirmation did
exactly that), the next PCF would silently consume outdated per-cell facts and
publish itself as current.

Boundaries this file also pins:

* the gate only *refuses* — it never recomputes, never degrades a stale value
  to zero/pass, and never rewrites any state;
* non-blocking statuses (``passed`` / ``not_calculated``) stay unaffected, so
  the gate cannot become a blanket ban on PCF generation.
"""

from pathlib import Path

import pytest

from cns_planner.application.planning_constraint_field_service import (
    PCF_INPUT_ATTRIBUTE_KINDS,
    pcf_input_attributes_guard,
    stale_pcf_input_attributes,
)
from cns_planner.application.workflow_service import WorkflowService

DEFAULTS = Path(__file__).parents[1] / "cns_planner" / "config" / "defaults.json"


def prepared(tmp_path, name="project.json"):
    service = WorkflowService(tmp_path / name, DEFAULTS)
    state = service.state
    state["grid"] = {
        "status": "passed", "level": 8, "crs": "OGC:CRS84",
        "cells": [{
            "grid_id": "G0", "level": 8, "column": 0, "row": 0,
            "bbox": [122.17, 29.8, 122.18, 29.81], "center": [122.175, 29.805],
        }],
    }
    state["result_statuses"]["grid"] = "passed"
    for kind in PCF_INPUT_ATTRIBUTE_KINDS:
        state["grid_attributes"][kind] = {
            "status": "passed", "cells": {"G0": {"data_status": "passed"}},
        }
    return service


def test_stale_input_attributes_blocks_generation(tmp_path):
    service = prepared(tmp_path)
    state = service.state
    state["grid_attributes"]["buildings"] = {
        "status": "stale", "stale_reason": "building_grid_facts_changed",
        "cells": {"G0": {"data_status": "passed"}},
    }
    before = service.state["result_statuses"]["planning_constraint_fields"]

    with pytest.raises(ValueError) as error:
        service.planning_constraint_field_service.generate({"altitude_layer_id": "ALT-100"})

    message = str(error.value)
    assert "buildings" in message
    assert "stale" in message
    # 拒绝生成绝不改变任何状态：既没有伪 passed，也没有把过期值清成 0。
    assert service.state["result_statuses"]["planning_constraint_fields"] == before
    assert service.state["grid_attributes"]["buildings"]["status"] == "stale"
    assert service.state["grid_attributes"]["buildings"]["cells"] == {
        "G0": {"data_status": "passed"},
    }


def test_needs_revalidation_is_blocking_too(tmp_path):
    service = prepared(tmp_path)
    service.state["grid_attributes"]["terrain"] = {
        "status": "needs_revalidation", "cells": {"G0": {"data_status": "passed"}},
    }
    with pytest.raises(ValueError) as error:
        service.planning_constraint_field_service.generate({"altitude_layer_id": "ALT-100"})
    assert "terrain" in str(error.value)


def test_publish_path_is_gated_as_well(tmp_path):
    """heavy task 的 publish 阶段同样经过 ``apply_field``，必须同样被堵。"""

    service = prepared(tmp_path)
    service.state["grid_attributes"]["population"] = {
        "status": "stale", "cells": {"G0": {"data_status": "passed"}},
    }
    field = {
        "altitude_layer_id": "ALT-100", "nominal_altitude_m": 100.0,
        "vertical_reference": "egm2008_orthometric",
        "constraint_field_fingerprint": "pcf-guard-fixture",
        "cells": [], "status": "completed", "counts": {}, "warnings": [],
    }
    with pytest.raises(ValueError):
        service.planning_constraint_field_service.apply_field(field)
    assert service.state["result_statuses"]["planning_constraint_fields"] != "passed"
    assert (service.state.get("planning_constraint_fields") or {}).get("status") != "passed"


def test_non_blocking_statuses_are_not_a_blanket_ban(tmp_path):
    service = prepared(tmp_path)
    service.state["grid_attributes"]["buildings"] = {
        "status": "not_calculated", "cells": {},
    }
    assert stale_pcf_input_attributes(service.state) == []
    pcf_input_attributes_guard(service.state)


def test_stale_helper_reports_only_blocked_kinds(tmp_path):
    service = prepared(tmp_path)
    service.state["grid_attributes"]["terrain"] = {
        "status": "stale", "stale_reason": "grid_attribute_source_changed", "cells": {},
    }
    service.state["grid_attributes"]["buildings"] = {
        "status": "stale", "cells": {},
    }
    blocked = stale_pcf_input_attributes(service.state)
    assert [item["kind"] for item in blocked] == ["terrain", "buildings"]
    assert blocked[0]["reason"] == "grid_attribute_source_changed"
    assert blocked[1]["reason"] is None
