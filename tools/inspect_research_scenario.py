"""Explicit bounded snapshot inspection only; never imports a model or worker."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cns_planner.application.research_scenario_admission import inspect_research_admission, validate_input_path
from cns_planner.domain.research_scenario import bounded_json, MAX_JSON_BYTES


def _unique_pairs(pairs):
    result = {}
    for key,value in pairs:
        if key in result: raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="R1 \u79bb\u7ebf\u573a\u666f\u9759\u6001\u68c0\u67e5\uff1b\u4e0d\u542f\u52a8\u8ba1\u7b97")
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--input-root", required=True, action="append")
    options = parser.parse_args(argv)
    try:
        checked = validate_input_path(options.snapshot, allowed_roots=options.input_root, expected_sha256=options.sha256)
        # Check the exact bytes actually parsed, not only the earlier path receipt.
        with Path(checked["resolved_path"]).open("rb") as stream: raw = stream.read(MAX_JSON_BYTES + 1)
        if len(raw) > MAX_JSON_BYTES or hashlib.sha256(raw).hexdigest() != options.sha256:
            raise ValueError("snapshot changed or exceeded limit")
        snapshot = bounded_json(json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs))
        result = inspect_research_admission(**snapshot)
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        result = dict(status="blocked", execution_enabled=False, blockers=[str(exc)])
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    return 0 if result["status"] == "static_contract_passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
