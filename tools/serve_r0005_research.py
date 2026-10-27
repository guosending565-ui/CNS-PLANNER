"""Explicit isolated C3-A HTTP assembly using the existing API and worker.

Prepare dispatch_state.json with a pinned research_checkpoint descriptor first.
This entrypoint cannot load the formal workflow or its mutation services.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import sys
import threading
from types import SimpleNamespace

TREE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TREE))
sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    state_file = root / "dispatch_state.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    if "research_fresh_input" in state:
        descriptor = state["research_fresh_input"]
        source = Path(descriptor["root"]).resolve()
        if root == source or root in source.parents or source in root.parents:
            raise ValueError("API runtime must be separate from the source input")
        if descriptor.get("authority") != "research_isolated":
            raise ValueError("research authority required")
        if not (source / "runtime" / "project_state.json").is_file():
            raise ValueError("research source input missing")
        os.environ["PYTHONPATH"] = os.pathsep.join([str(TREE), str(source / "src")])
        sys.path.insert(0, str(source / "src"))
        health = {"authority": "research_isolated", "pid": os.getpid(),
                  "root": str(root), "source_run_id": descriptor.get("source_run_id"),
                  "mode": "fresh_compute"}
    else:
        checkpoint = state["research_checkpoint"]
        source = Path(checkpoint["root"]).resolve()
        if root == source or root in source.parents or source in root.parents:
            raise ValueError("API runtime must be separate from the source checkpoint")
        if checkpoint["authority"] != "research_isolated":
            raise ValueError("research authority required")
        # Reuse C2-B's exact research session implementation in API and worker.
        os.environ["PYTHONPATH"] = os.pathsep.join([str(TREE), str(source / "src")])
        sys.path.insert(0, str(source / "src"))
        health = {"authority": "research_isolated", "pid": os.getpid(),
                  "root": str(root), "source_run_id": checkpoint["source_run_id"]}
    from cns_planner.algorithms.registry import build_default_algorithm_registry
    from cns_planner.application.research_run_service import ResearchRunService
    from cns_planner.tasks.service import HeavyTaskService
    from cns_planner.api.server import create_server
    defaults = json.loads((TREE / "cns_planner/config/defaults.json").read_text(encoding="utf-8"))
    workflow = SimpleNamespace(state=state, research_only=True, mutation_lock=threading.RLock(),
                               algorithm_registry=build_default_algorithm_registry(defaults))
    tasks = HeavyTaskService(workflow, state_file, max_workers=1, project_root=TREE,
                             heartbeat_interval=1.0, poll_interval=0.2)
    context = SimpleNamespace(workflow=workflow, data=SimpleNamespace(error=None),
                              static=TREE / "cns_planner" / "web",
                              token=secrets.token_hex(24), heavy_tasks=tasks,
                              research_runs=ResearchRunService(root, tasks, state=state),
                              health_identity=health)
    server = create_server(context, ("127.0.0.1", args.port))
    server.timeout = 0.25
    tasks.start()
    (root / "ready.json").write_text(json.dumps({"port": server.server_address[1],
        "pid": os.getpid(), "token": context.token}), encoding="utf-8")
    try:
        while not (root / "STOP").exists():
            server.handle_request()
    finally:
        tasks.stop(terminate_workers=True)
        server.server_close()
        (root / "stopped.json").write_text(json.dumps({"pid": os.getpid(), "workers_stopped": True}), encoding="utf-8")


if __name__ == "__main__":
    main()
