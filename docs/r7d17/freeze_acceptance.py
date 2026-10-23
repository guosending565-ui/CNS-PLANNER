"""R7-D17 acceptance freeze: source SHA/revision/data sources/algorithm identity.

Read-only with respect to the formal project. The acceptance copy is the only writable
target; this script writes two small JSON evidence files (copy root and docs/r7d17).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ACCEPT = Path(r"D:\Projects\_cns_acceptance\r7d17")
BASE = ACCEPT / "R0005-rev524"
STATE = BASE / "project_state.json"
DOCS = Path(__file__).resolve().parent

SMALL_SOURCES = [
    r"D:\aaa2026project\UOM\全国适飞空域图_单省可更新.qgz",
    r"D:\aaa2026project\UOM\舟山\基础数据\各单位报送的补充材料\各单位报送的补充材料\航路航线规划-铁塔数据.xlsx",
    r"D:\aaa2026project\UOM\舟山\基础数据\空域航线起降点\空域航线起降点\舟山市低空起降点汇总260305.xlsx",
    r"D:\aaa2026project\UOM\舟山\基础数据\空域航线起降点\空域航线起降点\舟山真实航线_CNS最终导入.xlsx",
    str(BASE / "derived" / "land_mask_osm" / "zhoushan_land_osm_v2.gpkg"),
    str(BASE / "derived" / "cartographic_land" / "cartographic_land_v1.gpkg"),
    str(BASE / "derived" / "zhoushan_building_grid_L8_v3_heightstats_v1.gpkg"),
]
LARGE_SOURCES = [
    r"D:\aaa2026project\UOM\舟山\规划系统\chn_pop_2025_CN_100m_R2025A_v1.tif",
    r"D:\aaa2026project\UOM\舟山\规划系统\GLO30\output\Zhejiang_GLO30_mosaic_30m.tif",
    r"D:\aaa2026project\UOM\舟山\规划系统\FABDEM\processed\Zhoushan_FABDEM_DTM_30m.tif",
    r"D:\aaa2026project\UOM\舟山\规划系统\building\processed\zhoushan_buildings_v3.gpkg",
]


def sha256(path: Path, chunk: int = 8 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def file_fact(path_text: str, *, hash_content: bool) -> dict:
    path = Path(path_text)
    if not path.is_file():
        return {"path": path_text, "exists": False}
    stat = path.stat()
    fact = {
        "path": str(path),
        "exists": True,
        "bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }
    if hash_content:
        fact["sha256"] = sha256(path)
    return fact


def main() -> int:
    state = json.loads(STATE.read_text(encoding="utf-8"))
    snapshot = json.loads((ACCEPT / "workflow2.json").read_text(encoding="utf-8"))
    step_status = json.loads((DOCS / "step_status.json").read_text(encoding="utf-8"))
    catalog = {entry.get("algorithm_id"): entry.get("version")
               for entry in snapshot.get("algorithm_catalog") or []}

    state_sha = sha256(STATE)
    copy_manifest = json.loads((BASE / "acceptance_copy_manifest.json").read_text(encoding="utf-8"))
    data_sources = json.loads((BASE / "data_sources.json").read_text(encoding="utf-8"))
    original_sources = json.loads(
        Path(r"D:\Projects\_cns_workspaces\integration\round38_acceptance\CNS-PLANNER"
             r"\projects\round38_cns_experiment_b\rounds\S4A\copy\data_sources.json")
        .read_text(encoding="utf-8")
    ) if Path(r"D:\Projects\_cns_workspaces\integration\round38_acceptance\CNS-PLANNER"
              r"\projects\round38_cns_experiment_b\rounds\S4A\copy\data_sources.json").is_file() else {}

    head = subprocess.run(["git", "-C", str(DOCS.parents[1]), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=False).stdout.strip()
    branch = subprocess.run(["git", "-C", str(DOCS.parents[1]), "branch", "--show-current"],
                            capture_output=True, text=True, check=False).stdout.strip()

    algorithms = {}
    for name in ("cns_corridor_assessment", "cns_corridor_gap_assessment",
                 "cns_corridor_site_plan", "continuous_service_acceptability",
                 "coverage_3d", "cns_service_capability", "protection_envelope",
                 "layered_route_candidates", "route_3d_profiles"):
        container = snapshot.get(name) or {}
        algorithm_id = container.get("algorithm_id")
        algorithms[name] = {
            "algorithm_id": algorithm_id,
            "stored_version": container.get("algorithm_version")
            or container.get("profile_version"),
            "current_catalog_version": catalog.get(algorithm_id),
            "status": container.get("status"),
            "input_fingerprint": container.get("input_fingerprint"),
        }

    report = {
        "schema_version": "cns-r7d17-acceptance-freeze/v1",
        "generated_at": "2026-10-23T20:30:00+08:00",
        "formal_project_untouched": {
            "path": r"D:\Projects\CNS-PLANNER\projects\current_project.json",
            "note": "formal ProjectState is revision 21 (no R0005) and was never opened or written this round",
        },
        "source": {
            "recovered_state_zip": r"D:\Projects\CNS-PLANNER\research\r7d10\isolated_recovered_state.zip",
            "zip_sha256": "643fd8817e72ddc95c6ba3ab59715093b931a331cb7c7600b315a9768c95f745",
            "zip_entry": "project_state.json",
            "state_sha256": state_sha,
            "bytes": STATE.stat().st_size,
            "revision": state.get("revision"),
            "project": state.get("project"),
            "recovery_classification": "identity_matched_historical_proposal_only (R7-D10); "
                                        "not a newly recomputed current result",
        },
        "copy": {
            "root": str(BASE),
            "state_sha256": state_sha,
            "byte_identical_to_source": state_sha == copy_manifest["source"]["state_sha256"],
            "data_sources_sha256": sha256(BASE / "data_sources.json"),
            "artifact_count": len(list((BASE / ".cns-results").glob("*.json.gz"))),
            "original_data_sources": original_sources,
            "effective_data_sources": data_sources,
        },
        "data_source_files": {
            "small_hashed": [file_fact(path, hash_content=True) for path in SMALL_SOURCES],
            "large_size_mtime_only": [file_fact(path, hash_content=False) for path in LARGE_SOURCES],
        },
        "code_identity": {
            "worktree": str(DOCS.parents[1]),
            "branch": branch,
            "head": head,
            "python": sys.version.split()[0],
        },
        "algorithms": algorithms,
        "identity": {
            "towers": {"status": (state.get("towers") or {}).get("status"),
                        "count": (state.get("towers") or {}).get("count")},
            "operational_routes": [
                {"route_id": route.get("route_id"), "status": route.get("status"),
                 "vertex_count": len(route.get("path") or []),
                 "start": route.get("start"), "end": route.get("end")}
                for route in state.get("operational_routes") or []
            ],
            "p16": {
                "canonical_selected_actions": len(
                    (state.get("cns_corridor_site_plan") or {}).get("selected_actions") or []),
                "recovery_evidence_selected_actions": len(
                    (state.get("cns_corridor_site_plan_results") or {}).get("selected_actions") or []),
                "candidate_actions": len(
                    (state.get("cns_corridor_site_plan_results") or {}).get("candidate_actions") or []),
                "iteration_trace": len(
                    (state.get("cns_corridor_site_plan") or {}).get("iteration_trace") or []),
                "physical_mount_confirmed": sum(
                    1 for item in (state.get("cns_corridor_site_plan_results") or {}).get("selected_actions") or []
                    if item.get("physical_mount_confirmed") is True),
                "requires_site_survey": sum(
                    1 for item in (state.get("cns_corridor_site_plan_results") or {}).get("selected_actions") or []
                    if item.get("requires_site_survey") is True),
            },
            "p17": {
                "state_status": (state.get("protection_envelope") or {}).get("status"),
                "three_d_api_status": "blocked",
                "stored_algorithm_version": "2.2",
                "current_code_algorithm_version": "2.5",
            },
        },
        "step_status": step_status["steps"],
        "service": {"base_url": "http://127.0.0.1:8794", "port": 8794,
                    "independent_from": [8765, 8776]},
    }
    (BASE / "acceptance_freeze.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    (DOCS / "acceptance_freeze.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print(json.dumps({
        "state_sha256": state_sha,
        "revision": report["source"]["revision"],
        "steps": {k: v["classification"] for k, v in report["step_status"].items()},
        "p16": report["identity"]["p16"],
        "head": head,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
