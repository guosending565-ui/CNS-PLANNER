"""R7-D18：R0005 既有 14 顶点运行航路的只读复验（D4 契约）。

不写 ProjectState、不生成 candidate、不运行 Theta*、不重算 P14/P15/P16/P17。
证据来源与生产验证同一套 GIS 适配器（FABDEM DTM + 建筑足迹 GeoPackage + 铁塔
障碍物 profile），因此不是手工构造的替身数据。

运行：
    & "C:\\Program Files\\QGIS 3.44.14\\bin\\python-qgis-ltr.bat" tools/r7d18_route_revalidation.py
"""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cns_planner.application.operational_route_clearance import (
    fingerprint, revalidate_existing_route,
)

ACCEPT = Path(r"D:\Projects\_cns_acceptance\r7d18")
BASE = ACCEPT / "R0005-rev524"
OUT = ROOT / "docs" / "r7d18"
ROUTE_ID = "R0005"
ALTITUDE_LAYER_ID = "ALT-100"


def digest(path: Path) -> str:
    h = sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    from qgis.core import QgsApplication

    application = QgsApplication([], False)
    application.initQgis()

    state_path = BASE / "project_state.json"
    before = digest(state_path)
    state = json.loads(state_path.read_text(encoding="utf-8-sig"))
    original = deepcopy(state)
    route = next(item for item in state["operational_routes"] if item["route_id"] == ROUTE_ID)
    layer = next(
        item for item in state["spatial_3d"]["altitude_layers"]
        if item.get("altitude_layer_id") == ALTITUDE_LAYER_ID
    )

    grid_snapshot = json.loads((ACCEPT / "api_grid.json").read_text(encoding="utf-8"))
    risk_snapshot = json.loads((ACCEPT / "api_risk2.json").read_text(encoding="utf-8"))
    grid = {
        "cells": grid_snapshot.get("cells") or [],
        "level": grid_snapshot.get("level") or 8,
        "crs": grid_snapshot.get("crs") or "OGC:CRS84",
    }
    if len(grid["cells"]) != int(grid_snapshot.get("count") or 0):
        raise ValueError("grid cells 与 count 不一致")
    if len(risk_snapshot.get("cells") or {}) != int(risk_snapshot.get("count") or 0):
        raise ValueError("grid_risk_v2 cells 与 count 不一致")

    from cns_planner.gis.building_footprint_aggregation import metric_crs_for_bbox
    from cns_planner.gis.layered_validation_adapter import (
        build_layered_validation_evidence_adapter,
    )

    bbox = (state.get("workspace") or {}).get("bbox")
    metric_crs = metric_crs_for_bbox(bbox)
    sources = json.loads((BASE / "data_sources.json").read_text(encoding="utf-8"))
    adapter = build_layered_validation_evidence_adapter(
        state=state,
        terrain_dtm_path=sources.get("terrain_dtm"),
        buildings_path=sources.get("buildings"),
        horizontal_crs=metric_crs,
    )
    feasibility = state.get("layered_route_feasibility_policy") or {}
    building = state.get("building_clearance_policy") or {}
    tower = state.get("tower_clearance_policy") or {}
    policy = {
        "terrain_clearance_m": feasibility.get("terrain_vertical_clearance_m"),
        "building_horizontal_clearance_m": building.get("horizontal_clearance_m"),
        "building_vertical_clearance_m": building.get("vertical_clearance_m"),
        "tower_horizontal_clearance_m": tower.get("tower_horizontal_clearance_m"),
        "tower_vertical_clearance_m": tower.get("tower_vertical_clearance_m"),
        "use_curve_error_envelope": False,
        #: 既有航路不是 candidate：工程演示的建筑证据身份不可能匹配，因此这里**不**
        #: 传递一个伪造的 context match；缺证建筑保持 unresolved（fail-closed）。
        "engineering_demo_building_acceptance_policy": deepcopy(
            state.get("engineering_demo_building_acceptance_policy") or {}
        ),
    }
    evidence = adapter(
        candidate={"route_id": ROUTE_ID, "altitude_layer_id": ALTITUDE_LAYER_ID,
                   "candidate_id": None},
        path=deepcopy(route["path"]),
        altitude_layer=deepcopy(layer),
        nominal_altitude_m=float(layer["nominal_altitude_m"]),
        policy=deepcopy(policy),
        payload={},
    )
    #: callable 不能进入身份指纹（identity 会序列化 evidence）；显式取出单独传递。
    to_geographic = evidence.pop("to_geographic", None)
    source_identity = {
        "state_sha256": before,
        "route_id": ROUTE_ID,
        "altitude_layer_id": ALTITUDE_LAYER_ID,
        "terrain_dtm": {
            "path": sources.get("terrain_dtm"),
            "sha256": digest(Path(sources["terrain_dtm"])),
        },
        "buildings": {
            "path": sources.get("buildings"),
            "sha256": digest(Path(sources["buildings"])),
        },
        "towers": {
            "path": sources.get("towers"),
            "sha256": digest(Path(sources["towers"])),
        },
        "grid": {
            "count": len(grid["cells"]),
            "artifact_sha256": "ff051389993a7ecdb6837ab568139a03eedcb21f19179a367e71612311f1761d",
            "endpoint": "/api/workspace/grid",
        },
        "grid_risk_v2": {
            "count": len(risk_snapshot.get("cells") or {}),
            "input_fingerprint": risk_snapshot.get("input_fingerprint"),
            "policy_fingerprint": risk_snapshot.get("policy_fingerprint"),
            "status": risk_snapshot.get("status"),
        },
        "metric_crs": metric_crs,
        "adapter": evidence.get("adapter_id"),
        "source_type": evidence.get("source_type"),
    }
    result = revalidate_existing_route(
        state, route_id=ROUTE_ID, altitude_layer_id=ALTITUDE_LAYER_ID,
        expected_geometry_fingerprint=fingerprint(route["path"]),
        evidence=evidence, policy=policy, source_identity=source_identity,
        to_geographic=to_geographic,
        grid=grid, grid_risk_v2=risk_snapshot,
        profile_policy=state.get("route_risk_profile_policy"),
        sampling_spacing_m=20.0,
    )
    if state != original or digest(state_path) != before:
        raise RuntimeError("D4 契约不得修改 ProjectState")
    result["state_unchanged"] = True
    result["acceptance_copy"] = {
        "path": str(BASE),
        "state_sha256": before,
        "revision": state.get("revision"),
    }
    result["theta_star_executed"] = False
    result["candidate_generated"] = False
    OUT.mkdir(parents=True, exist_ok=True)
    #: 完整证据含真实空间坐标：只留在验收副本，公开 Git 只保存身份摘要。
    full_path = ACCEPT / "d4_existing_route_validation.json"
    full_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8", newline="\n",
    )

    def _domain_summary(value):
        return {
            key: value.get(key) for key in (
                "status", "minimum_margin", "item_count", "failed_interval_count",
                "unresolved_interval_count", "reason",
            ) if key in value
        } | {
            "violation_count": len(value.get("violations") or []),
            "unresolved_count": len(value.get("unresolved") or []),
        }

    risk = result.get("risk") or {}
    profile = risk.get("profile") or {}
    slim = {
        "schema_version": "cns-r7d18-existing-route-validation/v1",
        "contract": result.get("contract"),
        "status": result.get("status"),
        "subject": {
            key: (result.get("subject") or {}).get(key) for key in (
                "kind", "route_id", "geometry_sha256", "provenance",
            )
        },
        "altitude_layer": {
            key: (result.get("altitude_layer") or {}).get(key) for key in (
                "altitude_layer_id", "nominal_altitude_m", "vertical_reference",
                "confirmed", "status",
            )
        },
        "policy": result.get("policy"),
        "domains": {
            name: _domain_summary(value)
            for name, value in (result.get("domains") or {}).items()
        },
        "risk": {
            "status": risk.get("status"),
            "status_reason": risk.get("status_reason"),
            "identity": risk.get("identity"),
            "profile": {
                "status": profile.get("status"),
                "route_length_m": profile.get("route_length_m"),
                "domains": {
                    domain_id: {
                        key: value.get(key) for key in (
                            "status", "coverage", "unresolved_length_m", "exposure_index_m",
                        )
                    }
                    for domain_id, value in (profile.get("domains") or {}).items()
                },
                "consistency": profile.get("consistency"),
            },
        },
        "fingerprint": result.get("fingerprint"),
        "source_identity": result.get("source_identity"),
        "research_only": result.get("research_only"),
        "operational_authorization": result.get("operational_authorization"),
        "formal_admission_evaluated": result.get("formal_admission_evaluated"),
        "state_unchanged": result.get("state_unchanged"),
        "acceptance_copy": result.get("acceptance_copy"),
        "theta_star_executed": result.get("theta_star_executed"),
        "candidate_generated": result.get("candidate_generated"),
        "full_evidence": {
            "location": str(full_path),
            "note": "完整证据含真实空间坐标，只保留在验收副本，不进入公开 Git。",
        },
    }
    (OUT / "existing_route_validation.json").write_text(
        json.dumps(slim, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8", newline="\n",
    )
    summary = {
        "contract": result["contract"],
        "status": result["status"],
        "route_id": ROUTE_ID,
        "altitude_layer_id": ALTITUDE_LAYER_ID,
        "vertex_count": len(route["path"]),
        "domains": {
            name: {
                "status": value.get("status"),
                "violations": len(value.get("violations") or []),
                "unresolved": len(value.get("unresolved") or []),
                "minimum_margin": value.get("minimum_margin"),
            }
            for name, value in (result.get("domains") or {}).items()
        },
        "risk": {
            "status": (result.get("risk") or {}).get("status"),
            "status_reason": (result.get("risk") or {}).get("status_reason"),
            "domains": {
                domain_id: {
                    "status": value.get("status"),
                    "coverage": value.get("coverage"),
                    "unresolved_length_m": value.get("unresolved_length_m"),
                    "exposure_index_m": value.get("exposure_index_m"),
                }
                for domain_id, value in (
                    ((result.get("risk") or {}).get("profile") or {}).get("domains") or {}
                ).items()
            },
        },
        "research_only": result.get("research_only"),
        "operational_authorization": result.get("operational_authorization"),
        "formal_admission_evaluated": result.get("formal_admission_evaluated"),
        "state_unchanged": True,
        "theta_star_executed": False,
    }
    (OUT / "existing_route_validation_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8", newline="\n",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
