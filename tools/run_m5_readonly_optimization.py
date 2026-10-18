"""D1 admission display only. No real worker or optimization dispatch."""
import argparse
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cns_planner.application.existing_site_optimization_evidence import build_readonly_run


def main(argv=None):
    parser = argparse.ArgumentParser(description="M5-D1 只读运行准入；不启动真实评价")
    parser.add_argument("--snapshot", required=True, help="用户明确指定的冻结输入快照 JSON 路径")
    options = parser.parse_args(argv)
    try:
        path = Path(options.snapshot)
        if not path.is_absolute() or path.is_symlink() or path.is_junction():
            raise ValueError("snapshot must be an explicit absolute regular path")
        if path.stat().st_size > 64*1024*1024:
            raise ValueError("snapshot exceeds 64MiB input limit")
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        result = build_readonly_run(**snapshot)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        result = dict(status="blocked", blockers=[str(exc)])
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if result["status"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
