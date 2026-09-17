#!/usr/bin/env python3
"""Generate a read-only source audit and DATA-1/2/3 readiness report."""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.source_audit import source_manifest
from cns_planner.gis.source_inspection import inspect_geopackage
from cns_planner.persistence.data_source_repository import DataSourceRepository


def build_report(project_path=None, source_config_path=None):
    project = Path(project_path or ROOT / "projects" / "current_project.json")
    source_config = Path(source_config_path or ROOT / "projects" / "map_sources.json")
    workflow = WorkflowService(project, ROOT / "cns_planner" / "config" / "defaults.json")
    paths = DataSourceRepository(source_config).load() if source_config.is_file() else {}
    stored = (workflow.state.get("source_audits") or {}).get("items") or {}
    roles = sorted(set(paths) | set(stored))
    audits = {}
    for role in roles:
        details = None
        if role in ("buildings", "building_grid") and Path(str(paths.get(role) or "")).is_file():
            info = inspect_geopackage(paths[role], role, deep_geometry=True)
            details = {
                "schema": {"fields": sorted((info.get("fields") or {}).keys())},
                "feature_count": info.get("feature_count"), "extent": info.get("extent"),
                "declared_crs": info.get("crs"),
                "geometry_health": info.get("geometry_health"),
            }
        audits[role] = source_manifest(
            role, paths.get(role), previous=stored.get(role), details=details,
        )
    readiness = workflow.data_readiness_snapshot()
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "project_file": project.name,
        "local_source_config_ref": "projects/map_sources.json",
        "source_audits": audits,
        "hash_and_version": {
            role: {"sha256": item.get("sha256"), "size_bytes": item.get("size_bytes"),
                   "mtime_ns": item.get("mtime_ns"), "status": item.get("status"),
                   "version_fingerprint": item.get("version_fingerprint")}
            for role, item in audits.items()
        },
        "crs": {
            role: {"declared": item.get("declared_crs"), "confirmed": item.get("confirmed_crs")}
            for role, item in audits.items()
        },
        "geometry_health": {
            **deepcopy(readiness.get("geometry_health") or {}),
            **{
                role: deepcopy(item.get("geometry_health"))
                for role, item in audits.items()
                if role in ("buildings", "building_grid")
            },
        },
        "reference_import_readiness": deepcopy(workflow.state.get("reference_route_import_preview")),
        "airspace_policy_readiness": deepcopy((readiness.get("blocks") or {}).get("airspace_policies")),
        "data_issues": deepcopy(readiness.get("data_issues") or {}),
        "automatic_confirmation": False,
        "et_parser": None,
        "note": "报告只读取现有证据；不造数据、不猜 CRS、不解析 ET、不自动确认 policy。",
    }


def _markdown(report):
    lines = ["# 数据可信度与规划输入就绪报告", "", report["note"], "", "## Source audit", ""]
    for role, audit in report["source_audits"].items():
        lines.append(
            f"- **{role}** `{audit.get('status')}` · {audit.get('file_name') or '未配置'} · "
            f"size={audit.get('size_bytes')} · sha256={audit.get('sha256') or '未验证'} · "
            f"CRS={audit.get('confirmed_crs') or audit.get('declared_crs') or '待确认'}"
        )
    lines += ["", "## DATA-1 / DATA-2 / DATA-3", ""]
    for key, item in report["data_issues"].items():
        lines.append(
            f"- **{key}** `{item.get('status')}` {item.get('label')}；"
            f"原因：{', '.join(str(value) for value in item.get('reasons') or [])}；"
            f"人工动作：{item.get('action')}"
        )
    lines += ["", "## Geometry health", "", "```json",
              json.dumps(report["geometry_health"], ensure_ascii=False, indent=2), "```", ""]
    return "\n".join(lines)


def write_report(report, out_dir="outputs/data_readiness"):
    directory = Path(out_dir)
    if not directory.is_absolute():
        directory = ROOT / directory
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "data_readiness.json"
    md_path = directory / "data_readiness.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_markdown(report), encoding="utf-8")
    return json_path, md_path


def main(argv=None):
    parser = argparse.ArgumentParser(description="数据可信度与规划输入就绪报告")
    parser.add_argument("--project", default=None)
    parser.add_argument("--source-config", default=None)
    parser.add_argument("--out-dir", default="outputs/data_readiness")
    args = parser.parse_args(argv)
    json_path, md_path = write_report(
        build_report(args.project, args.source_config), args.out_dir,
    )
    print(json_path)
    print(md_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
