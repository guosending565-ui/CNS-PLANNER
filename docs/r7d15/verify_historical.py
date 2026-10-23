"""R7-D15：只读核对恢复的 R0005 历史 P14/P15/P16 证据。

该脚本**只读**下列本机研究目录，不连接产品 API、不写 ProjectState、不重跑规划：

* ``research/r7d10/p16_result.json``（263,909,253 字节的历史 P16 proposal）
* ``research/r7d10/evidence_manifest.json``（原件 SHA256 / 字节数）
* ``research/r7d10/identity.json``（结果身份与 proposal-only 分类）

运行：``python docs/r7d15/verify_historical.py``。
输出：``docs/r7d15/historical_verification.json``（与本脚本同目录）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
EVIDENCE = Path(r"D:\Projects\CNS-PLANNER\research\r7d10")

REQUIRED_BEFORE = {
    "C": {"confirmed_gap": 2727, "unknown": 0, "max_continuous_m": 9404.937615150231},
    "N": {"confirmed_gap": 0, "unknown": 2727, "max_continuous_m": 0.0},
    "S": {"confirmed_gap": 2727, "unknown": 0, "max_continuous_m": 9404.937615150231},
}
REQUIRED_AFTER = {
    "C": {"confirmed_gap": 0, "satisfied": 2727, "unknown": 0, "max_continuous_m": 0.0},
    "N": {"confirmed_gap": 0, "satisfied": 0, "unknown": 2727, "max_continuous_m": 0.0},
    "S": {"confirmed_gap": 219, "satisfied": 2508, "unknown": 0, "max_continuous_m": 590.6034530275047},
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _subsystem(route: dict, code: str) -> dict:
    for item in route.get("subsystems") or []:
        if str(item.get("subsystem") or "") == code:
            return item
    raise KeyError(code)


def _stage_summary(stage: dict) -> dict:
    route = (stage.get("routes") or [{}])[0]
    result = {}
    for code in ("C", "N", "S"):
        item = _subsystem(route, code)
        combined = item.get("combined") or {}
        counts = combined.get("voxel_counts") or {}
        result[code] = {
            "confirmed_gap": int(counts.get("confirmed_gap") or 0),
            "satisfied": int(counts.get("satisfied") or 0),
            "unknown": int(counts.get("unknown") or 0),
            "max_continuous_m": float(item.get("max_continuous_deficit_projection_m") or 0.0),
            "total_confirmed_projection_m": float(
                item.get("total_confirmed_deficit_projection_m") or 0.0
            ),
        }
    return result


def main() -> int:
    p16_path = EVIDENCE / "p16_result.json"
    manifest = json.loads((EVIDENCE / "evidence_manifest.json").read_text(encoding="utf-8"))
    identity = json.loads((EVIDENCE / "identity.json").read_text(encoding="utf-8"))
    expected = manifest["p16_result.json"]
    actual_sha = _sha256(p16_path)
    checks = {
        "p16_sha256_matches_manifest": actual_sha == expected["sha256"],
        "p16_bytes_match_manifest": p16_path.stat().st_size == expected["bytes"],
        "identity_result_sha256_matches": identity.get("result_sha256") == actual_sha,
        "identity_is_proposal_only": identity.get("classification")
        == "identity_matched_historical_proposal_only",
    }

    result = json.loads(p16_path.read_text(encoding="utf-8"))
    before = _stage_summary(result["before"])
    after = _stage_summary(result["after"])
    actions = result.get("selected_actions") or []
    stop = result.get("stop_diagnostics") or {}

    checks.update({
        "target_voxel_count_5454": int(result.get("target_voxel_count") or 0) == 5454,
        "before_matches_historical_2727_baseline": all(
            before[code].get(key) == value
            for code, expected in REQUIRED_BEFORE.items()
            for key, value in expected.items()
        ),
        "after_matches_historical_cs_improvement": all(
            after[code].get(key) == value
            for code, expected in REQUIRED_AFTER.items()
            for key, value in expected.items()
        ),
        "n_unknown_2727_never_passed": (
            after["N"]["unknown"] == 2727 and after["N"]["confirmed_gap"] == 0
        ),
        "selected_actions_13": len(actions) == 13,
        "no_action_promoted_to_installed": all(
            action.get("physical_mount_confirmed") is False
            and action.get("requires_site_survey") is True
            and str(action.get("subsystem_mount_status") or "") == "unverified"
            for action in actions
        ),
        "stop_diagnostics_present": bool(stop.get("version")),
        "stop_selection_limit_13": int(stop.get("selection_limit") or 0) == 13,
        "stop_reason_present": bool(result.get("stop_reason")),
        "terminal_managed_gap_count_zero": int(
            result.get("terminal_managed_gap_count") or 0
        ) == 0,
        "residual_confirmed_targets_219": len(
            result.get("residual_confirmed_targets") or []
        ) == 219,
        "unknown_evidence_required_2727": len(
            result.get("unknown_evidence_required") or []
        ) == 2727,
    })
    payload = {
        "mode": "read_only_historical_evidence_no_recompute",
        "evidence_dir": str(EVIDENCE),
        "checks": checks,
        "passed": all(checks.values()),
        "before": before,
        "after": after,
        "selected_action_count": len(actions),
        "stop_reason": result.get("stop_reason"),
        "terminal_managed_gap_count": result.get("terminal_managed_gap_count"),
        "residual_confirmed_target_count": len(result.get("residual_confirmed_targets") or []),
        "unknown_evidence_required_count": len(result.get("unknown_evidence_required") or []),
        "p16_result_sha256": actual_sha,
        "p16_result_bytes": p16_path.stat().st_size,
    }
    out = HERE / "historical_verification.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: payload[k] for k in ("passed", "checks", "before", "after")},
                     ensure_ascii=False, indent=2))
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
