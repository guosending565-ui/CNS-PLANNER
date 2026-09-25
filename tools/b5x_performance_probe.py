"""Phase4-B5X 性能观测：ProjectState / workflow snapshot / save-load 的 before-after。

用法（在仓库根目录）::

    python tools/b5x_performance_probe.py

**Before** = B4X 行为（大型派生明细内联持久化、通用快照把它们整体带出浏览器）。
**After**  = B5X 行为（明细外置为 content-addressed artifact，快照只带 summary）。

两条路径使用**同一份**语义等价 fixture，因此差值来自存储 / 读取路径本身，
而不是算法输出。走廊大结果用 synthetic large detail（B5X 允许的等价替换）。
"""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cns_planner.application import workflow_service as workflow_module  # noqa: E402
from cns_planner.application.workflow_service import WorkflowService  # noqa: E402
from cns_planner.persistence.artifact_store import ArtifactStore  # noqa: E402
from cns_planner.persistence.project_repository import ProjectRepository  # noqa: E402


DEFAULTS = ROOT / "cns_planner" / "config" / "defaults.json"
GRID_CELLS = 4000
SAMPLES = 1200
VOXELS = 1500
RADAR_SAMPLES = 1200


def workspace_dir():
    target = Path(os.environ.get("B5X_PROBE_DIR") or (ROOT / "outputs" / "b5x_probe"))
    target.mkdir(parents=True, exist_ok=True)
    return target


def synthetic_fixture():
    cells = [
        {"grid_id": f"G{index}", "level": 8, "bbox": [120.0 + index * 1e-4, 30.0, 120.0001, 30.0001],
         "center": [120.0 + index * 1e-4, 30.0], "cell_size_m": 10.0}
        for index in range(GRID_CELLS)
    ]
    attribute_cells = {
        f"G{index}": {"status": "passed", "population_count_people": index % 97,
                      "coverage_status": "covered", "grid_area_m2": 100.0,
                      "quality_flags": [], "source": "synthetic"}
        for index in range(GRID_CELLS)
    }
    risk_cells = {
        f"G{index}": {"grid_id": f"G{index}", "status": "resolved", "semantics": "synthetic",
                      "factors": {f"F{factor}": {"factor_id": f"F{factor}", "status": "resolved",
                                                 "raw_value": 0.5, "normalized_index": 0.5}
                                  for factor in range(8)},
                      "domains": {"ground": {"index": 0.5}, "air_traffic": {"index": 0.4},
                                  "environment_obstacle": {"index": 0.3}}}
        for index in range(GRID_CELLS)
    }
    constraint_cells = [
        {"grid_id": f"G{index}", "outcome": "blocked" if index % 7 == 0 else "pass",
         "blocked_by": ["terrain"] if index % 7 == 0 else [],
         "unknown_reasons": [], "evidence_refs": [{"domain": "terrain", "source": "synthetic"}],
         "bbox": [120.0 + index * 1e-4, 30.0, 120.0001, 30.0001]}
        for index in range(GRID_CELLS)
    ]
    coverage_samples = [
        {"grid_id": f"G{index % GRID_CELLS}", "distance_along_route_m": index * 5.0,
         "longitude": 120.0, "latitude": 30.0, "surface_elevation_m": 10.0,
         "altitude_agl_m": 80.0, "altitude_egm2008_m": 90.0, "vertical_status": "resolved",
         "vertical_reason": None, "covered": index % 3 != 0, "nearest_slant_distance_m": 1200.0,
         "providers": [{"facility_id": "F1", "device_id": "D1", "slant_distance_m": 1200.0,
                        "horizontal_distance_m": 900.0, "vertical_delta_m": 10.0}]}
        for index in range(SAMPLES)
    ]
    voxels = [
        {"voxel_id": f"V{index}", "route_id": "R1", "vertical_status": "resolved",
         "cell_area_proxy_m2": 100.0, "discretized_volume_proxy_m3": 8000.0,
         "probe": {"longitude": 120.0, "latitude": 30.0},
         "subsystems": [{"subsystem": "C", "planning_status": "satisfied",
                         "geometry": {"slant_distance_m": 900.0},
                         "providers": [{"facility_id": "F1"}],
                         "provider_evaluations": [{"facility_id": "F1", "device_id": "D1",
                                                   "slant_distance_m": 900.0,
                                                   "evidence": [{"kind": "budget"}]}],
                         "reasons": [], "evidence": [{"kind": "geometry"}]}]}
        for index in range(VOXELS)
    ]
    radar_items = [
        {"route_id": "R1", "samples": [{"grid_id": f"G{index}", "covered": True}
                                       for index in range(RADAR_SAMPLES)],
         "validation": {"samples": [{"grid_id": f"G{index}", "covered": True}
                                    for index in range(RADAR_SAMPLES)],
                        "coverage_profile": {"entries": [{"segment": index} for index in range(400)]}},
         "selected_panels": [{"panel_id": f"P{index}", "display_geometry": {"radius_m": 5000.0}}
                             for index in range(200)],
         "refinement_rounds": [{"round_index": index} for index in range(6)]},
    ]
    return {
        "grid": {"status": "passed", "level": 8, "count": len(cells), "cells": cells},
        "grid_attributes": {
            "population": {"namespace": "population", "status": "passed",
                           "count": len(attribute_cells), "cells": attribute_cells},
        },
        "grid_risk_v2": {"status": "passed", "input_fingerprint": "probe-fp",
                         "cells": risk_cells},
        "planning_constraint_fields": {
            "schema_version": 1, "status": "passed", "count": 1,
            "items": [{"field_id": "PCF-ALT-080-probe", "altitude_layer_id": "ALT-080",
                       "nominal_altitude_m": 80.0, "vertical_reference": "egm2008_orthometric",
                       "status": "completed_with_warnings",
                       "constraint_field_fingerprint": "pcf-probe",
                       "counts": {"total": len(constraint_cells), "pass": 0, "blocked": 0,
                                  "unknown": 0,
                                  "blocked_by": {name: 0 for name in (
                                      "terrain", "building", "tower", "airspace",
                                      "critical_site")}},
                       "warnings": [], "cells": constraint_cells}],
        },
        "coverage_3d": {
            "status": "passed", "input_fingerprint": "cov-probe", "route_count": 1,
            "routes": [{"route_id": "R1", "samples": coverage_samples,
                        "subsystems": [{"subsystem": subsystem, "covered_fraction": 0.66,
                                        "samples": deepcopy(coverage_samples)}
                                       for subsystem in ("C", "N", "S")]}],
        },
        "cns_corridor_assessment": {
            "status": "passed", "input_fingerprint": "cor-probe", "route_count": 1,
            "routes": [{"route_id": "R1",
                        "corridor_geometry": {"included_grid_ids": [f"G{i}" for i in range(GRID_CELLS)]},
                        "voxels": voxels,
                        "subsystems": [{"subsystem": "C", "status": "satisfied",
                                        "deficit_voxel_ids": [f"V{i}" for i in range(200)],
                                        "unknown_voxel_ids": []}]}],
        },
        "radar_surveillance_layout": {
            "schema_version": 1, "status": "proposal_ready", "count": 1, "items": radar_items,
        },
    }


def fill(service):
    service.state.update(deepcopy(synthetic_fixture()))
    return service


def snapshot_without_b5x(service):
    """B4X 行为的快照：不做 B5X 明细投影（大结果随通用快照整体下发）。"""

    original = workflow_module._snapshot_detail_projection
    workflow_module._snapshot_detail_projection = lambda result: {}
    try:
        return service.snapshot()
    finally:
        workflow_module._snapshot_detail_projection = original


def measure():
    folder = workspace_dir()
    report = {}

    # ---------- After（B5X） ----------
    after_path = folder / "after" / "project_state.json"
    service = fill(WorkflowService(after_path, DEFAULTS))
    start = time.perf_counter()
    service.save()
    after_save_ms = (time.perf_counter() - start) * 1000
    after_state_bytes = after_path.stat().st_size
    start = time.perf_counter()
    after_snapshot = service.snapshot()
    after_snapshot_ms = (time.perf_counter() - start) * 1000
    after_snapshot_bytes = len(json.dumps(after_snapshot, ensure_ascii=False).encode("utf-8"))
    start = time.perf_counter()
    reloaded = WorkflowService(after_path, DEFAULTS)
    after_load_ms = (time.perf_counter() - start) * 1000
    artifacts = sorted((after_path.parent / ".cns-results").glob("*.json.gz"))
    report["after"] = {
        "project_state_bytes": after_state_bytes,
        "snapshot_bytes": after_snapshot_bytes,
        "snapshot_ms": round(after_snapshot_ms, 1),
        "save_ms": round(after_save_ms, 1),
        "load_ms": round(after_load_ms, 1),
        "artifact_count": len(artifacts),
        "artifact_bytes": sum(path.stat().st_size for path in artifacts),
        "artifact_bytes_max": max((path.stat().st_size for path in artifacts), default=0),
        "restored_grid_cells": len((reloaded.state.get("grid") or {}).get("cells") or []),
        "restored_corridor_voxels": len(
            (((reloaded.state.get("cns_corridor_assessment") or {}).get("routes") or [{}])[0]
             .get("voxels") or [])
        ),
    }

    # ---------- Before（B4X 行为） ----------
    before_path = folder / "before" / "project_state.json"
    inline = fill(WorkflowService(before_path, DEFAULTS))
    document = {
        key: value for key, value in inline.state.items()
        if key not in ("_population_shelter_cache", "_planning_exposure_cache")
    }
    repository = ProjectRepository(before_path)
    start = time.perf_counter()
    repository.save(document)
    before_save_ms = (time.perf_counter() - start) * 1000
    before_state_bytes = before_path.stat().st_size
    start = time.perf_counter()
    before_snapshot = snapshot_without_b5x(inline)
    before_snapshot_ms = (time.perf_counter() - start) * 1000
    before_snapshot_bytes = len(json.dumps(before_snapshot, ensure_ascii=False).encode("utf-8"))
    start = time.perf_counter()
    inline_load = WorkflowService(before_path, DEFAULTS)
    before_load_ms = (time.perf_counter() - start) * 1000
    report["before"] = {
        "project_state_bytes": before_state_bytes,
        "snapshot_bytes": before_snapshot_bytes,
        "snapshot_ms": round(before_snapshot_ms, 1),
        "save_ms": round(before_save_ms, 1),
        "load_ms": round(before_load_ms, 1),
        "restored_grid_cells": len((inline_load.state.get("grid") or {}).get("cells") or []),
    }
    report["fixture"] = {
        "grid_cells": GRID_CELLS, "coverage_samples_per_subsystem": SAMPLES,
        "corridor_voxels": VOXELS, "radar_samples": RADAR_SAMPLES,
    }
    report["ratios"] = {
        "project_state": round(before_state_bytes / max(1, after_state_bytes), 2),
        "snapshot": round(before_snapshot_bytes / max(1, after_snapshot_bytes), 2),
    }
    return report


def main():
    report = measure()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
