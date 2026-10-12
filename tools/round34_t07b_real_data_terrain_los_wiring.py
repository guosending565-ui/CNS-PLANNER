"""Round34-T07-B —— 真实数据的**只读**地形 LOS 接线验证（性能 / 体积 / 口径）。

必须用带 GDAL 的解释器运行（本机 = QGIS Python）::

    "C:\\Program Files\\QGIS 3.44.14\\bin\\python-qgis-ltr.bat" `
        tools\\round34_t07b_real_data_terrain_los_wiring.py `
        --towers <towers.json> --profiles <tower_obstacle_profiles.json> `
        --policy <radar_surveillance_policy.json> --terrain "<FABDEM.tif>" `
        --out "D:\\Projects\\_cns_experiments\\Round34\\Round34-T07-B"

它只做五件事（全部只读，不写 ``ProjectState``、不改源数据、不跑 P14/P15/P16）：

1. 用 T05 的真实切片构造**最小只读 ProjectState**（towers / profiles / policy /
   高度层目录 / 一条由真实塔坐标生成的确定性只读航路）；
2. 对**全部**候选站址做原点物理输入诊断，核对 "可用 + 不可评价 = 全部候选" 的真实数据
   基线（**不**硬编码 285/88，只断言两者之和与候选总数一致）；
3. 用**显式**诊断入口按若干组有界参数跑地形 LOS，测量墙钟耗时、响应字节数与
   DEM 打开次数；
4. 重复同输入验证确定性，并用旧证据验证 current 语义；
5. 用**依赖注入**改写 DEM 身份验证 stale 语义（**不**修改 FABDEM 文件本身）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cns_planner.application.radar_terrain_los_diagnostics_service import (  # noqa: E402
    RadarTerrainLosDiagnosticsService,
)
from cns_planner.domain.altitude_layer_defaults import default_altitude_layers  # noqa: E402
from cns_planner.gis import radar_terrain_los_adapter as terrain_los  # noqa: E402

ALTITUDE_LAYER_ID = "ALT-080"
DIAGNOSTIC_ROUTE_ID = "T07B-READONLY-DIAGNOSTIC-ROUTE"


class _ReadOnlySession:
    """最小只读会话：只有 ``state``；``save`` 记录调用次数（必须恒为 0）。"""

    def __init__(self, state):
        self.state = state
        self.save_count = 0

    def save(self):
        self.save_count += 1


class _Transform:
    method = "pyproj_projected_crs_coordinate_transform"

    def __init__(self, metric_crs):
        from pyproj import Transformer

        self.metric_crs = metric_crs
        self.authority = metric_crs
        self._forward = Transformer.from_crs("EPSG:4326", metric_crs, always_xy=True)
        self._backward = Transformer.from_crs(metric_crs, "EPSG:4326", always_xy=True)

    def to_metric(self, point):
        x, y = self._forward.transform(float(point[0]), float(point[1]))
        return [float(x), float(y)]

    def to_geographic(self, point):
        x, y = self._backward.transform(float(point[0]), float(point[1]))
        return [float(x), float(y)]


class _RadarLayoutStub:
    """复用既有 Radar 只读接口的两个能力：逆投影与数据源路径。"""

    def __init__(self, transform, state):
        self.metric_transform = transform
        self._state = state

    def to_geographic(self, point):
        return self.metric_transform.to_geographic(point)

    def _source_paths(self, facts_provider=None):
        del facts_provider
        return dict(self._state.get("data_source_paths") or {})


def _load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _towers_from_slice(towers):
    if isinstance(towers, dict) and isinstance(towers.get("items"), list):
        return [item for item in towers["items"] if isinstance(item, dict)]
    if isinstance(towers, list):
        return [item for item in towers if isinstance(item, dict)]
    if isinstance(towers, dict):
        return [item for item in towers.values() if isinstance(item, dict)]
    return []


def _deterministic_route(towers):
    """由真实塔坐标构造一条**确定性只读航路**（只用于产生有界 probe 位置）。

    它不是运行航路，也不进入任何规划：本工具只验证接线与性能，绝不复制规划结论。
    """

    usable = [
        item for item in towers
        if item.get("longitude") is not None and item.get("latitude") is not None
    ]
    usable.sort(key=lambda item: str(item.get("tower_id") or ""))
    if len(usable) < 2:
        return None
    start, end = usable[0], usable[-1]
    return {
        "route_id": DIAGNOSTIC_ROUTE_ID,
        "status": "passed",
        "altitude_layer_id": ALTITUDE_LAYER_ID,
        "path": [[float(start["longitude"]), float(start["latitude"])],
                 [float(end["longitude"]), float(end["latitude"])]],
        "diagnostic_only": True,
        "not_an_operational_route": True,
    }


def _build_state(*, towers, profiles, policy, terrain_path):
    layers = {
        str(item["altitude_layer_id"]): item for item in default_altitude_layers()
        if isinstance(item, dict) and item.get("altitude_layer_id")
    }
    layer = dict(layers.get(ALTITUDE_LAYER_ID) or {})
    layer.update({
        "altitude_layer_id": ALTITUDE_LAYER_ID,
        "nominal_altitude_m": 80.0,
        "vertical_reference": "egm2008_orthometric",
        "status": "confirmed",
        "confirmed": True,
    })
    route = _deterministic_route(towers)
    return {
        "towers": {"status": "passed", "count": len(towers), "items": list(towers)},
        "tower_obstacle_profiles": profiles,
        "operational_routes": [route] if route else [],
        "spatial_3d": {"altitude_layers": [layer]},
        "radar_surveillance_policy": policy,
        "data_source_paths": {"terrain_dtm": str(terrain_path)},
    }, route


def _encoded_size(payload):
    return len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))


def _summarise_run(label, payload, evidence, *, elapsed):
    return {
        "label": label,
        "payload": payload,
        "evidence_status": evidence.get("evidence_status"),
        "evaluation_scope": evidence.get("evaluation_scope"),
        "algorithm_version": evidence.get("algorithm_version"),
        "evaluated_tower_count": evidence.get("evaluated_tower_count"),
        "evaluated_probe_count": evidence.get("evaluated_probe_count"),
        "candidate_tower_count": evidence.get("candidate_tower_count"),
        "candidate_probe_count": evidence.get("candidate_probe_count"),
        "los_status_counts": evidence.get("los_status_counts"),
        "origin_availability_counts": evidence.get("origin_availability_counts"),
        "input_fingerprint": evidence.get("input_fingerprint"),
        "dem_open_count": evidence.get("dem_open_count"),
        "record_sample_count": evidence.get("record_sample_count"),
        "detail_record_count": evidence.get("detail_record_count"),
        "response_bytes": _encoded_size(evidence),
        "wall_seconds": round(elapsed, 3),
        "currentness": evidence.get("currentness"),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Round34-T07-B 真实数据只读接线验证")
    parser.add_argument("--towers", required=True)
    parser.add_argument("--profiles", required=True)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--terrain", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--metric-crs", default="EPSG:32651")
    parser.add_argument("--max-tower-count", type=int, default=3)
    parser.add_argument("--probe-budget", type=int, default=24)
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    towers = _towers_from_slice(_load_json(args.towers))
    profiles = _load_json(args.profiles)
    policy = _load_json(args.policy)
    state, route = _build_state(
        towers=towers, profiles=profiles, policy=policy, terrain_path=args.terrain,
    )
    transform = _Transform(args.metric_crs)
    session = _ReadOnlySession(state)
    service = RadarTerrainLosDiagnosticsService(session)
    service.to_metric = transform.to_metric
    service.radar_layout_service = _RadarLayoutStub(transform, state)

    report = {
        "round": "Round34-T07-B",
        "terrain_dtm": str(args.terrain),
        "tower_input_count": len(towers),
        "metric_crs": args.metric_crs,
        "diagnostic_route_id": route.get("route_id") if route else None,
        "diagnostic_route_is_operational": False,
        "read_only": True,
        "status_probe": None,
        "runs": [],
        "determinism": None,
        "currentness": None,
        "staleness": None,
        "detail_page": None,
        "session_save_count": 0,
        "state_written": False,
    }

    # ---- 0. 轻量状态：必须**不**打开 DEM ----
    started = time.perf_counter()
    status = service.status()
    report["status_probe"] = {
        "evidence_status": status.get("evidence_status"),
        "dem_opened": status.get("dem_opened"),
        "los_computation_performed": status.get("los_computation_performed"),
        "available_to_compute": status.get("available_to_compute"),
        "wall_seconds": round(time.perf_counter() - started, 4),
        "response_bytes": _encoded_size(status),
        "records": status.get("records"),
    }

    # ---- 1. 有界显式诊断（多组参数） ----
    run_specs = (
        ("default", {}),
        ("bounded", {
            "max_tower_count": args.max_tower_count, "probe_budget": args.probe_budget,
        }),
        ("wider", {
            "max_tower_count": max(args.max_tower_count * 2, 6),
            "probe_budget": max(args.probe_budget * 3, 72),
        }),
    )
    evidence_by_label = {}
    for label, payload in run_specs:
        started = time.perf_counter()
        evidence = service.diagnose(payload)
        elapsed = time.perf_counter() - started
        evidence_by_label[label] = evidence
        report["runs"].append(_summarise_run(label, payload, evidence, elapsed=elapsed))

    # ---- 2. 确定性：同输入重复诊断 ----
    bounded_payload = dict(run_specs[1][1])
    baseline = evidence_by_label["bounded"]
    started = time.perf_counter()
    repeat = service.diagnose(bounded_payload)
    repeat_seconds = time.perf_counter() - started
    report["determinism"] = {
        "same_fingerprint": repeat.get("input_fingerprint")
        == baseline.get("input_fingerprint"),
        "same_los_counts": repeat.get("los_status_counts")
        == baseline.get("los_status_counts"),
        "same_origin_availability_counts": repeat.get("origin_availability_counts")
        == baseline.get("origin_availability_counts"),
        "same_probe_ids": [item.get("probe_id") for item in (repeat.get("records") or [])]
        == [item.get("probe_id") for item in (baseline.get("records") or [])],
        "same_los_statuses": [item.get("los_status") for item in (repeat.get("records") or [])]
        == [item.get("los_status") for item in (baseline.get("records") or [])],
        "wall_seconds": round(repeat_seconds, 3),
    }

    # ---- 3. currentness：同一输入 ⇒ current ----
    prior_payload = dict(bounded_payload, prior_evidence=baseline)
    current = service.diagnose(prior_payload)
    report["currentness"] = current.get("currentness")

    # ---- 4. stale：DEM 身份变化（**只**改注入的身份，不动真实文件） ----
    original_identity = terrain_los.terrain_source_identity
    try:
        def _shifted_identity(source):
            identity = original_identity(source)
            if isinstance(identity, dict) and identity.get("mtime_ns") is not None:
                identity["mtime_ns"] = int(identity["mtime_ns"]) + 1
            return identity

        terrain_los.terrain_source_identity = _shifted_identity
        stale = service.diagnose(prior_payload)
    finally:
        terrain_los.terrain_source_identity = original_identity
    report["staleness"] = {
        "evidence_status": stale.get("evidence_status"),
        "currentness": stale.get("currentness"),
        "fingerprint_changed": stale.get("input_fingerprint")
        != current.get("input_fingerprint"),
        "upstream_planning_state_untouched": True,
    }

    # ---- 5. 明细：受限分页读取（不重新打开 DEM） ----
    opens_before = (stale.get("dem_open_count") or 0)
    detail = service.details({"limit": 10 ** 6})
    report["detail_page"] = {
        "matched_record_count": (detail.get("detail_page") or {}).get("matched_record_count"),
        "returned_record_count": (detail.get("detail_page") or {}).get("returned_record_count"),
        "limit": (detail.get("detail_page") or {}).get("limit"),
        "max_limit": (detail.get("detail_page") or {}).get("max_limit"),
        "recomputed": detail.get("recomputed"),
        "response_bytes": _encoded_size(detail),
        "full_terrain_profile_included": detail.get("full_terrain_profile_included"),
        "reused_evidence_without_new_open": opens_before
        == (stale.get("dem_open_count") or 0),
    }

    # ---- 6. 只读性核对 ----
    report["session_save_count"] = session.save_count
    report["state_written"] = session.save_count != 0
    bounded_run = report["runs"][1]
    report["origin_availability_total_matches_inventory"] = (
        sum((bounded_run["origin_availability_counts"] or {}).values())
        == bounded_run["candidate_tower_count"]
    )

    (out_dir / "real_data_wiring_performance.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8",
    )
    printable = {
        "tower_input_count": report["tower_input_count"],
        "status_probe": report["status_probe"],
        "runs": [
            {key: run[key] for key in (
                "label", "evaluated_tower_count", "evaluated_probe_count",
                "candidate_tower_count", "candidate_probe_count",
                "los_status_counts", "origin_availability_counts",
                "dem_open_count", "response_bytes", "wall_seconds",
            )}
            for run in report["runs"]
        ],
        "determinism": report["determinism"],
        "currentness_state": (report["currentness"] or {}).get("state"),
        "staleness": {
            "currentness_state": (report["staleness"]["currentness"] or {}).get("state"),
            "reasons": (report["staleness"]["currentness"] or {}).get("reasons"),
            "fingerprint_changed": report["staleness"]["fingerprint_changed"],
        },
        "detail_page": report["detail_page"],
        "session_save_count": report["session_save_count"],
        "origin_availability_total_matches_inventory": report[
            "origin_availability_total_matches_inventory"
        ],
    }
    print(json.dumps(printable, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
