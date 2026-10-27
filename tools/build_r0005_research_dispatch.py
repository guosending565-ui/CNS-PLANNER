"""Build an isolated C3-B dispatch descriptor from an existing R0005 state.

This tool only reads the source state and writes a new runtime root.  It never
modifies the source, the formal ``ProjectState`` or Git.  The descriptor freezes
the editable research-parameter allowlist so request payloads cannot select an
arbitrary site or filesystem path.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

TREE = Path(__file__).resolve().parents[1]
PARAMETER_CONTRACT_VERSION = "c3b-research-parameters-1"
MIDDLE_SEGMENTS = [3, 4]
AUTHORITY = "research_isolated"


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def communication_is_2km(state):
    for device in (state.get("device_catalog") or {}).get("items") or []:
        if device.get("service_key") != "C:communication":
            continue
        geometry = device.get("coverage_geometry") or {}
        mapping = geometry.get("radius_by_surface")
        if isinstance(mapping, dict) and isinstance(mapping.get("resolved"), dict):
            mapping = mapping["resolved"]
        if not isinstance(mapping, dict) or not mapping:
            return False
        if any(abs(float(value) - 2000.0) > 1e-9
               for value in mapping.values() if isinstance(value, (int, float))):
            return False
    return True


def build_descriptor(state_path, route_id="R0005"):
    state = json.loads(Path(state_path).read_text(encoding="utf-8"))
    facilities = (state.get("existing_cns_facilities") or {}).get("items") or []
    options = []
    for facility in facilities:
        subsystems = sorted({
            str(device.get("subsystem") or "").upper()
            for device in facility.get("devices") or []
            if str(device.get("subsystem") or "").upper() in ("C", "N", "S")
        })
        if "N" not in subsystems:
            continue
        metadata = facility.get("metadata") or {}
        planned = metadata.get("new_physical_site") is True
        options.append({
            "site_id": facility.get("site_id"),
            "label": facility.get("name") or facility.get("site_id"),
            "host_kind": "planned_new_site" if planned else "existing_host",
            "subsystems": subsystems,
            "coordinate": facility.get("coordinate"),
        })
    planning = state.get("navigation_integrity_planning") or {}
    bindings = planning.get("middle_bindings") or []
    by_segment = {int(item["segment_index"]): item["site_id"] for item in bindings}
    default = [by_segment.get(segment) for segment in MIDDLE_SEGMENTS]
    if any(value is None for value in default) or len(set(default)) != 2:
        raise ValueError("source middle_bindings do not define segments 3 and 4")
    option_ids = {item["site_id"] for item in options}
    if any(value not in option_ids for value in default):
        raise ValueError("source bindings are not N-capable sites")
    options.sort(key=lambda item: (
        0 if item["site_id"] in default else 1,
        default.index(item["site_id"]) if item["site_id"] in default else 0,
        item["site_id"],
    ))
    devices = sum(len(facility.get("devices") or []) for facility in facilities)
    planned = sum((facility.get("metadata") or {}).get("new_physical_site") is True
                  for facility in facilities)
    p16 = state.get("cns_corridor_site_plan") or {}
    n_actions = sum(
        1 for action in p16.get("selected_actions") or []
        if "navigation_integrity" in str(action.get("action_id") or "")
        or "navigation_integrity" in str(action.get("planner_family") or "")
    ) if p16.get("selected_actions") else None
    source_state_sha = sha256(state_path)
    return {
        "schema_version": 2,
        "revision": 0,
        "research_fresh_input": {
            "authority": AUTHORITY,
            "root": str(Path(state_path).resolve().parents[1]),
            "source_run_id": "c2-nm3-research-input",
            "source_state_sha256": source_state_sha,
            "route_id": route_id,
            "contract_version": "round2.6-post-plan-continuous-service-acceptability",
            "input_summary": {
                "route_id": route_id,
                "sites": len(facilities),
                "devices": devices,
                "existing_hosts": len(facilities) - planned,
                "planned_new_sites": planned,
                "n_actions": n_actions,
                "two_km_contract": communication_is_2km(state),
            },
            "parameter_schema": {
                "contract_version": PARAMETER_CONTRACT_VERSION,
                "ids": ["navigation_middle_monitor_sites"],
                "middle_segments": MIDDLE_SEGMENTS,
                "options": options,
                "default": default,
                "label": "N 中段职责站点绑定（2 个）",
                "description": (
                    "研究用 N 中段监视宿主假设。改动写入导航规划输入并生成新输入指纹；"
                    "不改变业务门限，不代表已安装或已批准。"),
            },
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--route-id", default="R0005")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    source = Path(args.source).resolve()
    if root == source or root in source.parents or source in root.parents:
        raise SystemExit("dispatch root must be separate from source")
    root.mkdir(parents=True, exist_ok=True)
    descriptor = build_descriptor(source, args.route_id)
    target = root / "dispatch_state.json"
    target.write_text(
        json.dumps(descriptor, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n")
    print(json.dumps({
        "dispatch_state": str(target), "source": str(source),
        "source_state_sha256": descriptor["research_fresh_input"]["source_state_sha256"],
        "input_summary": descriptor["research_fresh_input"]["input_summary"],
        "parameter_default": descriptor["research_fresh_input"]["parameter_schema"]["default"],
        "parameter_options": len(descriptor["research_fresh_input"]["parameter_schema"]["options"]),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
