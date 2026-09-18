"""Route-planning experiment records: comparison evidence, never the current plan.

``operational_routes`` stays the single authority for *the current formal planner
result*.  Experiments are a separate, additive collection so an expert can run V1 and
V2 side by side without switching ``algorithm_selection`` and without overwriting any
operational route.

Every record keeps enough provenance to be re-checked later: the scenario/input
fingerprints it ran against, the planner identity, its manifest, its effective
parameters, the result snapshot, the quality measurement, runtime statistics and the
provenance of how it was grounded.  It deliberately carries no ranking, score or
recommendation.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import re

EXPERIMENT_COLLECTION_ID = "route-planning-experiments"
EXPERIMENT_SCHEMA_VERSION = 1

GROUNDING_MODES = ("current_scenario_routes", "synthetic_fixture")
SOURCE_TYPES = ("synthetic", "project", "mixed")

#: How many experiments are retained in the project state (newest kept).
MAX_EXPERIMENTS = 20

_EXPERIMENT_ID = re.compile(r"^EXP-[0-9A-F]{12}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:#-]{0,63}$")

MEASUREMENT_PLANNER_OUTPUT_UNTOUCHED = "planner_result_snapshot_not_mutated"


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def content_hash(value):
    """Deterministic JSON hash used for fingerprints inside experiment records."""

    return sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def empty_experiments():
    return {
        "status": "not_calculated",
        "collection_id": EXPERIMENT_COLLECTION_ID,
        "schema_version": EXPERIMENT_SCHEMA_VERSION,
        "count": 0,
        "active_experiment_id": None,
        "records": [],
        "note": (
            "experiments 仅用于专家比较实验，不是 current operational route；"
            "运行比较不切换 algorithm_selection，也不写入 operational_routes。"
        ),
    }


def scenario_fingerprint(scenario_routes):
    """Fingerprint the scenario routes exactly as the planner would consume them."""

    relevant = [
        {
            "route_id": item.get("route_id"),
            "start_node_id": item.get("start_node_id"),
            "end_node_id": item.get("end_node_id"),
            "start": item.get("start"),
            "end": item.get("end"),
        }
        for item in scenario_routes or []
    ]
    return content_hash(relevant)


def planner_context_fingerprint(context):
    """Fingerprint the non-scenario inputs that can change a planner outcome."""

    context = context if isinstance(context, dict) else {}
    grid = context.get("grid") or {}
    risk = context.get("grid_risk") or {}
    relevant = {
        "workspace_bbox": context.get("workspace_bbox"),
        "hard_constraints": context.get("hard_constraints") or [],
        "grid": {
            "status": grid.get("status"), "level": grid.get("level"),
            "cell_size_degrees": grid.get("cell_size_degrees"),
            "cells": [
                {"grid_id": item.get("grid_id"), "bbox": item.get("bbox"), "center": item.get("center")}
                for item in grid.get("cells") or []
            ],
        },
        "grid_risk": risk,
    }
    return content_hash(relevant)


def experiment_id_for(scenario_fingerprint_value, planner_versions, parameters_by_version,
                      context_fingerprint_value=None):
    """Deterministic identity: same scenario + same planner set + same params."""

    identity = {
        "scenario": scenario_fingerprint_value,
        "planners": planner_versions,
        "parameters": parameters_by_version,
    }
    if context_fingerprint_value is not None:
        identity["planner_context"] = context_fingerprint_value
    return "EXP-" + content_hash(identity)[:12].upper()


def build_run_record(
    run_id, algorithm_id, algorithm_version, manifest, effective_parameters,
    result, quality, runtime, *, applicable=True, status=None, reason=None,
):
    """One planner run inside an experiment. ``result`` is stored as a snapshot."""

    if not _RUN_ID.match(str(run_id or "")):
        raise ValueError("experiment run_id 无效")
    return {
        "run_id": str(run_id),
        "algorithm_id": str(algorithm_id),
        "algorithm_version": str(algorithm_version),
        "manifest": deepcopy(manifest),
        "effective_parameters": deepcopy(effective_parameters or {}),
        "applicable": bool(applicable),
        "planner_invoked": bool(applicable and result is not None),
        "status": status if status is not None else (result or {}).get("status"),
        "reason": reason if reason is not None else (result or {}).get("reason"),
        "result": deepcopy(result),
        "quality": deepcopy(quality),
        "runtime": deepcopy(runtime),
        "result_snapshot_semantics": "frozen_copy_of_planner_output",
        "planner_output_mutated": False,
    }


def build_experiment_record(
    *, scenario_fingerprint_value, planners, grounding, source_type,
    source_detail=None, experiment_id=None, runs=None, current_applicability="current",
    context_fingerprint_value=None, context_basis=None,
):
    """Assemble a record whose identity is derived only from inputs and planners."""

    versions = {item["run_id"]: f'{item["algorithm_id"]}@{item["algorithm_version"]}' for item in planners}
    parameters = {item["run_id"]: item.get("effective_parameters") or {} for item in planners}
    identifier = experiment_id or experiment_id_for(
        scenario_fingerprint_value, versions, parameters, context_fingerprint_value,
    )
    if not _EXPERIMENT_ID.match(identifier):
        raise ValueError(f"experiment_id 无效：{identifier}")
    if grounding not in GROUNDING_MODES:
        raise ValueError("grounding 必须为 current_scenario_routes/synthetic_fixture")
    if source_type not in SOURCE_TYPES:
        raise ValueError("source_type 必须为 synthetic/project/mixed")
    run_records = deepcopy(runs) if runs is not None else [
        build_run_record(
            item["run_id"], item["algorithm_id"], item["algorithm_version"],
            item.get("manifest"), item.get("effective_parameters"),
            item.get("result"), item.get("quality"), item.get("runtime"),
            applicable=item.get("applicable", True), status=item.get("status"),
            reason=item.get("reason"),
        )
        for item in planners
    ]
    return {
        "experiment_id": identifier,
        "schema_version": EXPERIMENT_SCHEMA_VERSION,
        "created_at": utc_now(),
        "source_type": source_type,
        "grounding": grounding,
        "scenario_fingerprint": scenario_fingerprint_value,
        "planner_context_fingerprint": context_fingerprint_value,
        "planner_context_basis": deepcopy(context_basis),
        "input_fingerprints": {
            item["run_id"]: (item.get("result") or {}).get("input_fingerprint")
            for item in run_records
        },
        "planners": [
            {
                "run_id": item["run_id"],
                "algorithm_id": item["algorithm_id"],
                "algorithm_version": item["algorithm_version"],
                "effective_parameters": deepcopy(item.get("effective_parameters") or {}),
                "manifest": deepcopy(item.get("manifest")),
            }
            for item in run_records
        ],
        "runs": run_records,
        "provenance": {
            "grounding": grounding,
            "source_type": source_type,
            "source_detail": deepcopy(source_detail),
            "recorded_at": utc_now(),
            "measurement_scope": MEASUREMENT_PLANNER_OUTPUT_UNTOUCHED,
            "operational_routes_untouched": True,
            "algorithm_selection_untouched": True,
        },
        "current_applicability": current_applicability,
        "verdicts": {
            "automatically_ranked": False,
            "automatically_scored": False,
            "preferred_algorithm": None,
        },
    }


def with_applicability(record, applicability):
    updated = deepcopy(record)
    updated["current_applicability"] = applicability
    return updated


def experiment_collection(records, *, active_experiment_id=None):
    """Wrap records into the additive collection, newest first, bounded size."""

    ordered = sorted(
        (deepcopy(item) for item in records or []),
        key=lambda item: (str(item.get("created_at") or ""), str(item.get("experiment_id") or "")),
        reverse=True,
    )[:MAX_EXPERIMENTS]
    return {
        "status": "passed" if ordered else "not_calculated",
        "collection_id": EXPERIMENT_COLLECTION_ID,
        "schema_version": EXPERIMENT_SCHEMA_VERSION,
        "count": len(ordered),
        "active_experiment_id": active_experiment_id,
        "records": ordered,
        "note": empty_experiments()["note"],
    }


def normalize_experiments(value):
    """Additive schema backfill: absent/legacy values become an empty collection."""

    result = empty_experiments()
    if value is None:
        return result
    if isinstance(value, list):
        raw_records = value
    elif isinstance(value, dict):
        raw_records = value.get("records") or []
        if value.get("active_experiment_id") is not None:
            result["active_experiment_id"] = value.get("active_experiment_id")
    else:
        raise ValueError("route_planning_experiments 必须是对象或数组")
    normalized = []
    for record in raw_records:
        if not isinstance(record, dict):
            raise ValueError("experiment record 必须是对象")
        identifier = str(record.get("experiment_id") or "")
        if not _EXPERIMENT_ID.match(identifier):
            raise ValueError(f"experiment_id 无效：{identifier}")
        entry = deepcopy(record)
        entry.setdefault("schema_version", EXPERIMENT_SCHEMA_VERSION)
        entry.setdefault("source_type", "project")
        entry.setdefault("grounding", "current_scenario_routes")
        entry.setdefault("input_fingerprints", {})
        entry.setdefault("planner_context_fingerprint", None)
        entry.setdefault("planner_context_basis", {"source": "legacy_unknown"})
        entry.setdefault("planners", [])
        entry.setdefault("runs", [])
        entry.setdefault("provenance", {
            "grounding": entry["grounding"],
            "source_type": entry["source_type"],
            "note": "旧项目回填：缺少原始 provenance 明细。",
        })
        entry.setdefault("current_applicability", "unknown")
        entry.setdefault("verdicts", {
            "automatically_ranked": False,
            "automatically_scored": False,
            "preferred_algorithm": None,
        })
        normalized.append(entry)
    collection = experiment_collection(normalized, active_experiment_id=result["active_experiment_id"])
    return collection
